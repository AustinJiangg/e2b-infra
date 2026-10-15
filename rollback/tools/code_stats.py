#!/usr/bin/env python3
"""按固定 Git 对象统计功能增量，并从同一份数据维护手册表格。"""
import argparse
import ast
from bisect import bisect_right
from functools import lru_cache
from importlib.metadata import version
import hashlib
import json
from pathlib import Path
import re
import subprocess

from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, String

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCOPE = ROOT / "stats/code_stats_scope.json"
DEFAULT_REPORT = ROOT / "stats/code-volume.json"
COMPONENTS = {"go": "Go 服务端", "rust": "Firecracker Rust", "sdk": "Python SDK", "support": "手写协议、配置与构建支撑"}
SCOPES = {"rollback": "回滚实现为主的文件", "native": "原生增量配套为主的文件", "shared": "共用基础与集成文件"}
TESTS = {"unit": "单元测试与组件微基准", "integration": "集成与验收测试", "performance": "性能与稳定性测试", "support": "测试框架与诊断支撑"}
PERIODS = {"mr": "MR 版", "after_mr": "MR 后修改优化", "current": "当前版相对开发前基线"}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@lru_cache(None)
def read_blob(repo, ref, path):
    exists = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    return git(repo, "show", f"{ref}:{path}").decode("utf-8") if exists else ""


def language(path):
    if Path(path).name == "Makefile":
        return "make"
    if path.endswith("/.env"):
        return "bash"
    return {".rs": "rust", ".go": "go", ".py": "python", ".pyi": "python", ".sh": "bash",
            ".c": "c", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
            ".proto": "protobuf"}[Path(path).suffix]


def line_starts(source):
    return [0] + [m.end() for m in re.finditer("\n", source)]


@lru_cache(None)
def code_lines(source, lang):
    """有效代码物理行：整文件词法分析；Python 真正的 docstring 视为注释。"""
    mask = [False] * len(source)
    tokens = list(get_lexer_by_name(lang).get_tokens_unprocessed(source))
    assert "".join(value for _, _, value in tokens) == source, "词法器未完整保留输入"
    for index, kind, value in tokens:
        if (kind in Comment and kind not in Comment.Preproc) or (lang == "rust" and kind in String.Doc):
            continue
        for offset, char in enumerate(value, index):
            if not char.isspace():
                mask[offset] = True
    starts = line_starts(source)
    if lang == "python" and source:
        lines = source.splitlines(keepends=True)
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or not node.body:
                continue
            expr = node.body[0]
            if not (isinstance(expr, ast.Expr) and isinstance(expr.value, ast.Constant) and isinstance(expr.value.value, str)):
                continue
            # AST 的列偏移按 UTF-8 字节计；转换为 Python 字符偏移后再遮掉 docstring。
            begin_col = len(lines[expr.lineno - 1].encode()[:expr.col_offset].decode())
            end_col = len(lines[expr.end_lineno - 1].encode()[:expr.end_col_offset].decode())
            begin = starts[expr.lineno - 1] + begin_col
            end = starts[expr.end_lineno - 1] + end_col
            mask[begin:end] = [False] * (end - begin)
    return frozenset(bisect_right(starts, index) for index, marked in enumerate(mask) if marked)


@lru_cache(None)
def rust_test_lines(source, feature=False):
    masked = list(source)
    for index, kind, value in get_lexer_by_name("rust").get_tokens_unprocessed(source):
        if (kind in Comment and kind not in Comment.Preproc) or kind in String:
            masked[index:index + len(value)] = ["\n" if c == "\n" else " " for c in value]
    code = "".join(masked)
    pattern = r'#\[cfg\(feature = "rollback-fault-inject"\)\]' if feature else r"#\[cfg\(test\)\]"
    result = set()
    lines = source.splitlines()
    for match in re.finditer(pattern, source if feature else code):
        if code[match.start()] != "#":
            continue
        opening, semicolon = code.find("{", match.end()), code.find(";", match.end())
        if feature and semicolon >= 0 and (opening < 0 or semicolon < opening):
            end = semicolon + 1
        else:
            assert opening >= 0, "测试项缺少代码块"
            if not feature:
                assert re.search(r"\bmod\s+\w+\s*$", code[match.end():opening]), "需要审查非模块 cfg(test) 项"
            depth, end = 1, opening + 1
            while depth:
                if code[end] == "{":
                    depth += 1
                elif code[end] == "}":
                    depth -= 1
                end += 1
        first = source.count("\n", 0, match.start()) + 1
        last = source.count("\n", 0, end) + 1
        while first > 1 and lines[first - 2].lstrip().startswith("///"):
            first -= 1
        result.update(range(first, last + 1))
    return frozenset(result)


def classify(info, path, source, number):
    if info["kind"] == "production" and path.endswith(".rs"):
        if number in rust_test_lines(source):
            return {"kind": "test", "test_kind": "unit", "usage": "源码单测与组件微基准"}
        if number in rust_test_lines(source, True):
            return {"kind": "test", "test_kind": "support", "usage": "测试专用构建"}
    return info


def changed_lines(repo, before, after, path):
    patch = git(repo, "diff", "--no-ext-diff", "--no-renames", "--unified=0", before, after, "--", path).decode()
    active = False
    old = new = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            old, new = map(int, match.groups())
            active = True
        elif active and line.startswith("+"):
            yield "added", new
            new += 1
        elif active and line.startswith("-"):
            yield "deleted", old
            old += 1
        elif active and line.startswith(" "):
            old += 1
            new += 1


def empty_metrics():
    return {unit: {"added": 0, "deleted": 0, "net": 0} for unit in ("sloc", "physical")}


def finish(metrics):
    for counts in metrics.values():
        counts["net"] = counts["added"] - counts["deleted"]
    return metrics


def total(rows):
    result = empty_metrics()
    for row in rows:
        for unit in result:
            for side in ("added", "deleted"):
                result[unit][side] += row["metrics"][unit][side]
    return finish(result)


def grouped(rows, key):
    values = sorted({row.get(key, "未分类") for row in rows})
    return {value: total([r for r in rows if r.get(key, "未分类") == value]) for value in values}


def compare(repo, before, after, entries):
    result = []
    for entry in git(repo, "diff", "--no-ext-diff", "--no-renames", "--numstat", before, after).decode().splitlines():
        added, deleted, path = entry.split("\t", 2)
        if path not in entries:
            raise ValueError(f"范围清单缺少变更文件：{path}")
        info = entries[path]
        if added == "-":
            assert info["kind"] == "excluded", path
            result.append({"repository": "kas", "path": path, **info, "metrics": empty_metrics()})
            continue
        old = read_blob(str(repo), before, path)
        new = read_blob(str(repo), after, path)
        sets = {"deleted": code_lines(old, language(path)), "added": code_lines(new, language(path))} if info["kind"] != "excluded" else {}
        buckets = {}
        for side, number in changed_lines(repo, before, after, path):
            classification = classify(info, path, new if side == "added" else old, number)
            key = json.dumps(classification, sort_keys=True)
            row = buckets.setdefault(key, {"repository": "kas", "path": path, **classification, "metrics": empty_metrics()})
            row["metrics"]["physical"][side] += 1
            if sets and number in sets[side]:
                row["metrics"]["sloc"][side] += 1
        assert sum(r["metrics"]["physical"]["added"] for r in buckets.values()) == int(added), path
        assert sum(r["metrics"]["physical"]["deleted"] for r in buckets.values()) == int(deleted), path
        for row in buckets.values():
            finish(row["metrics"])
            result.append(row)
    return result


def measure(kas, infra, scope):
    for name in ("kas_files", "infra_files"):
        for path, info in scope[name].items():
            assert info["kind"] in ("production", "generated", "test", "excluded"), f"未知代码性质：{path}"
            if info["kind"] == "production":
                assert info["component"] in COMPONENTS and info["scope"] in SCOPES, f"未知生产分类：{path}"
            elif info["kind"] == "test":
                assert info["test_kind"] in TESTS and info["usage"], f"未知测试分类：{path}"
            elif info["kind"] == "excluded":
                assert info["reason"], f"缺少排除原因：{path}"
    refs = scope["refs"]
    for key, ref in refs.items():
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise ValueError(f"必须使用完整提交 SHA：{key}")
        repo = infra if key == "infra_current" else kas
        assert git(repo, "rev-parse", f"{ref}^{{commit}}").decode().strip() == ref
    for before, after in ((refs["baseline"], refs["mr"]), (refs["mr"], refs["current"])):
        assert git(kas, "merge-base", before, after).decode().strip() == before
    report = {"schema_version": 1, "as_of": scope["as_of"], "refs": refs,
              "scope_sha256": hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
              "counter": {"name": "code_stats.py", "pygments": version("Pygments"), "python_docstrings": "ast"},
              "periods": {}}
    for period, before, after in (("mr", refs["baseline"], refs["mr"]), ("after_mr", refs["mr"], refs["current"]),
                                   ("current", refs["baseline"], refs["current"])):
        rows = compare(kas, before, after, scope["kas_files"])
        production = [r for r in rows if r["kind"] == "production"]
        generated = [r for r in rows if r["kind"] == "generated"]
        report["periods"][period] = {"production": total(production), "by_component": grouped(production, "component"),
                                      "by_scope": grouped(production, "scope"), "generated": total(generated), "details": rows}
    expected = set(git(infra, "ls-tree", "-r", "--name-only", refs["infra_current"], "rollback/scripts").decode().splitlines())
    expected = {p for p in expected if p.endswith((".py", ".sh", ".c"))}
    assert expected == set(scope["infra_files"]), "部署仓库脚本范围已变化，先审查分类清单"
    infra_rows = []
    for path, info in scope["infra_files"].items():
        source = read_blob(str(infra), refs["infra_current"], path)
        if "identical_to" in info:
            target = info["identical_to"]
            assert source == read_blob(str(kas), refs[target["ref"]], target["path"]), "测试副本已分叉，不能按相同内容排除"
        metrics = empty_metrics()
        metrics["physical"]["added"] = len(source.splitlines())
        metrics["sloc"]["added"] = len(code_lines(source, language(path)))
        infra_rows.append({"repository": "infra", "path": path, **info, "metrics": finish(metrics),
                           "sha256": hashlib.sha256(source.encode()).hexdigest()})
    kas_rows = [r for r in report["periods"]["current"]["details"] if r["kind"] == "test"]
    tests = kas_rows + [r for r in infra_rows if r["kind"] == "test"]
    report["tests_current"] = {"total": total(tests), "by_kind": grouped(tests, "test_kind"),
                                "by_repository": grouped(tests, "repository"), "by_usage": grouped(tests, "usage"),
                                "details": tests}
    report["infra_details"] = infra_rows
    for unit in ("sloc", "physical"):
        assert report["periods"]["mr"]["production"][unit]["net"] + report["periods"]["after_mr"]["production"][unit]["net"] == report["periods"]["current"]["production"][unit]["net"], f"{unit} 两阶段净增不一致，需审查分类或词法边界"
    return report


def number(value):
    return f"{value:,}"


def delta(metrics, unit="sloc"):
    m = metrics[unit]
    return f"+{number(m['added'])} / −{number(m['deleted'])} / **{number(m['net'])}**"


def render_blocks(report):
    periods, tests = report["periods"], report["tests_current"]
    mr, post, current = [periods[key]["production"]["sloc"]["net"] for key in ("mr", "after_mr", "current")]
    blocks = {"summary": f"按固定源码版本统计，MR 版净增 **{number(mr)} 行有效生产代码**，MR 后修改优化净增 **{number(post)} 行**；当前版相对开发前基线合计净增 **{number(current)} 行**。当前测试与验证代码去重后净增 **{number(tests['total']['sloc']['net'])} 行有效代码**，包含测试框架与诊断工具。以上均排除空行、纯注释与 Python docstring；生产主合计排除自动生成代码。完整口径、分类、版本与物理行复核见 [附录 C](C-code-size-and-methodology.md)。"}
    labels = {"baseline": "功能开发前基线", "mr": "MR 版 deltabox", "current": "当前 deltabox-dev", "infra_current": "当前测试 e2b-infra"}
    blocks["versions"] = "| 对象 | 固定提交 SHA |\n|---|---|\n" + "\n".join(f"| {labels[k]} | `{v}` |" for k, v in report["refs"].items())
    lines = ["| 生产代码类别 | MR 版：新增 / 删除 / 净增 | MR 后修改优化：新增 / 删除 / 净增 |", "|---|---:|---:|"]
    for component, label in COMPONENTS.items():
        lines.append(f"| {label} | {delta(periods['mr']['by_component'].get(component, empty_metrics()))} | {delta(periods['after_mr']['by_component'].get(component, empty_metrics()))} |")
    lines.append(f"| **手写生产合计** | {delta(periods['mr']['production'])} | {delta(periods['after_mr']['production'])} |")
    lines.append(f"| 自动生成代码（单列，不并入主合计） | {delta(periods['mr']['generated'])} | {delta(periods['after_mr']['generated'])} |")
    lines.extend(["", f"当前版手写生产代码相对功能开发前基线：{delta(periods['current']['production'])} 行有效代码。这里的合计是功能引入的净增量；已有上游文件的未改动部分不计。"])
    blocks["production"] = "\n".join(lines)
    lines = ["| 文件主用途 | MR 版净增 | MR 后净增 | 当前累计净增 |", "|---|---:|---:|---:|"]
    for scope, label in SCOPES.items():
        values = [number(periods[p]["by_scope"].get(scope, empty_metrics())["sloc"]["net"]) for p in PERIODS]
        lines.append(f"| {label} | {' | '.join(values)} |")
    blocks["scope"] = "\n".join(lines)
    lines = ["| 当前测试与验证类别 | 有效代码：新增 / 删除 / 净增 |", "|---|---:|"]
    for key, label in TESTS.items():
        lines.append(f"| {label} | {delta(tests['by_kind'].get(key, empty_metrics()))} |")
    lines.extend([f"| **去重合计** | {delta(tests['total'])} |", "", "按仓库来源复核：", "", "| 来源 | 有效代码净增 | 物理行净增 |", "|---|---:|---:|"])
    for repo, label in (("kas", "KASandbox_0904"), ("infra", "e2b-infra/rollback/scripts")):
        m = tests["by_repository"][repo]
        lines.append(f"| `{label}` | {number(m['sloc']['net'])} | {number(m['physical']['net'])} |")
    lines.extend(["", "入口关系仅描述代码组织，不代表本轮执行结果：", "", "| 代码位置与使用方式 | 有效代码净增 |", "|---|---:|"])
    for usage, metrics in tests["by_usage"].items():
        lines.append(f"| {usage} | {number(metrics['sloc']['net'])} |")
    blocks["tests"] = "\n".join(lines)
    lines = ["| 物理行复核 | 新增 / 删除 / 净增 |", "|---|---:|"]
    for p, label in PERIODS.items():
        lines.append(f"| {label}：手写生产 | {delta(periods[p]['production'], 'physical')} |")
    lines.append(f"| 当前测试与验证（去重） | {delta(tests['total'], 'physical')} |")
    mr_generated = periods["mr"]["generated"]["physical"]["net"]
    current_generated = periods["current"]["generated"]["physical"]["net"]
    mr_with_generated = periods["mr"]["production"]["physical"]["net"] + mr_generated
    current_with_generated = periods["current"]["production"]["physical"]["net"] + current_generated
    lines.extend(["", f"将自动生成代码加回生产物理行后，本快照 MR 版净增 **{number(mr_with_generated)} 行**，当前累计净增 **{number(current_with_generated)} 行**；"
                  f"其中自动生成文件的物理行分别为 {number(mr_generated)}、{number(current_generated)} 行。MR 后手写物理行净增为 **{number(periods['after_mr']['production']['physical']['net'])} 行**，"
                  f"当前测试物理行净增为 **{number(tests['total']['physical']['net'])} 行**。这些值可用于与此前包含注释、空行和生成代码的快速统计核对。"])
    blocks["physical"] = "\n".join(lines)
    excluded = [r for r in periods["current"]["details"] + report["infra_details"] if r["kind"] == "excluded"]
    lines = ["| 排除文件 | 原因 | 物理行新增 / 删除 |", "|---|---|---:|"]
    for row in excluded:
        counts = row["metrics"]["physical"]
        count_text = "—（二进制不按行计）" if row["path"].endswith(".bin") else f"+{number(counts['added'])} / −{number(counts['deleted'])}"
        lines.append(f"| `{row['repository']}:{row['path']}` | {row['reason']} | {count_text} |")
    blocks["exclusions"] = "\n".join(lines)
    return blocks


def sync_docs(report, root=ROOT, check=False):
    blocks = render_blocks(report)
    for path, names in ((root / "docs/00-overview.md", ("summary",)),
                        (root / "docs/C-code-size-and-methodology.md", ("versions", "production", "scope", "tests", "physical", "exclusions"))):
        source = path.read_text(encoding="utf-8")
        updated = source
        for name in names:
            begin, end = f"<!-- code-stats:{name}:start -->", f"<!-- code-stats:{name}:end -->"
            assert updated.count(begin) == updated.count(end) == 1, f"统计块标记缺失或重复：{path}:{name}"
            updated = re.sub(re.escape(begin) + r".*?" + re.escape(end),
                             lambda _: f"{begin}\n\n{blocks[name]}\n\n{end}", updated, flags=re.S)
        if check and updated != source:
            raise ValueError(f"统计摘要或表格与归档数据不一致：{path}")
        if not check:
            path.write_text(updated, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    ap.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--kasandbox", type=Path)
    ap.add_argument("--infra", type=Path)
    ap.add_argument("--update-docs", action="store_true")
    ap.add_argument("--check-docs", action="store_true", help="只核对已归档数据和正文，不读取源码仓库")
    ap.add_argument("--verify", action="store_true", help="重算并与已归档 JSON 比较，不覆盖归档")
    args = ap.parse_args()
    if args.check_docs:
        report = load(args.output)
        scope = load(args.scope)
        digest = hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        assert report["scope_sha256"] == digest, "统计范围与归档不一致，需要重算"
        assert report["refs"] == scope["refs"] and report["as_of"] == scope["as_of"], "归档版本与范围清单不一致"
        sync_docs(report, check=True)
        print("统计范围、导读摘要与附录表格一致")
        return
    if args.kasandbox is None or args.infra is None:
        ap.error("重算时必须给 --kasandbox 与 --infra，不依赖工作区目录布局")
    scope = load(args.scope)
    report = measure(args.kasandbox.resolve(), args.infra.resolve(), scope)
    if args.verify:
        assert report == load(args.output), "重算结果与已归档 JSON 不同"
        print("固定提交重算与归档 JSON 逐项一致")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"统计结果：{args.output}")
    if args.update_docs:
        sync_docs(report)
        print("已从同一份统计更新导读摘要与附录表格")


if __name__ == "__main__":
    main()
