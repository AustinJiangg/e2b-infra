"""把结果目录里的内容整理成便于复制的终端文本。950.sh report 与 test950.sh report 共用，只用标准库。

不打印含密钥的文件（nomad job 定义、dotenv、ACL 令牌、切栈基线），其余文件都可以用 show_file 按行取出。
"""
import json
from pathlib import Path
import re

CUT = 180   # 单行最长字符数，超出截断，免得一行占满屏


def secret(path):
    name = Path(path).name
    return ((name.startswith(("job-", ".job-", "baseline")) and name.endswith((".json", ".sha256")))
            or name.endswith(".env") or name == "acl.token")


def clip(text, n=CUT):
    text = str(text).replace("\n", " ")
    return text if len(text) <= n else text[:n - 1] + "…"


def show_file(path, lines=40, head=False, root=None):
    """打印文件的最后（或最前）若干行；lines=0 打印全部。"""
    path = Path(path)
    rel = path.relative_to(root) if root and path.is_relative_to(root) else path
    if secret(path):
        print("（%s 含密钥，不打印）" % rel)
        return
    if not path.is_file():
        print("（没有文件 %s）" % rel)
        return
    text = path.read_text(errors="replace").splitlines()
    part = text if not lines or len(text) <= lines else (text[:lines] if head else text[-lines:])
    where = "全部" if len(part) == len(text) else ("前 %d 行" % len(part) if head else "最后 %d 行" % len(part))
    print("---- %s（共 %d 行，%s）----" % (rel, len(text), where))
    for line in part:
        print(clip(line, 400))


def list_files(directory, root=None):
    directory = Path(directory)
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root or directory)
            print("  %s%s  %d 字节" % (rel, "（含密钥）" if secret(p) else "", p.stat().st_size))


# ---------- 沙箱级 run.sh 各档的 SUMMARY.md ----------

def run_sh_summary(step_dir):
    """run.sh 某一档的 SUMMARY.md：返回 (结果行, [(项, 结果, 耗时, 关键数字, 产物)])。"""
    found = sorted(Path(step_dir).glob("*/SUMMARY.md"))
    if not found:
        return None, []
    result, rows, in_items = "", [], False
    for line in found[-1].read_text(errors="replace").splitlines():
        if line.startswith("| 结果 |"):
            result = line.split("|")[2].strip().replace("*", "")
        if line.startswith("## "):
            in_items = line.strip() == "## 逐项"
            continue
        if in_items and line.startswith("|") and not line.startswith(("| 项 |", "|---")):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4:
                rows.append(tuple(cells[:5]) if len(cells) >= 5 else tuple(cells[:4]) + ("",))
    return result, rows


def print_run_sh(step_dir, indent="    "):
    result, rows = run_sh_summary(step_dir)
    if result is None:
        print(indent + "（没有 SUMMARY.md）")
        return
    print(indent + "结果：" + result)
    for item, status, took, key, _ in rows:
        print(indent + clip("%-5s %-6s %s：%s" % (status, took, item, key.replace("**", ""))))


def print_run_sh_detail(step_dir, lines=40):
    """逐项打印 run.sh 各项产物日志的末尾。"""
    found = sorted(Path(step_dir).glob("*/SUMMARY.md"))
    if not found:
        print("（没有 SUMMARY.md）")
        return
    tier_dir = found[-1].parent
    print_run_sh(step_dir, indent="")
    seen = set()
    for _, status, _, _, product in run_sh_summary(step_dir)[1]:
        for name in re.findall(r"[\w.@-]+\.(?:log|md|json)", product):
            if name in seen:
                continue
            seen.add(name)
            path = tier_dir / name
            if path.suffix == ".log":
                show_file(path, lines if status != "PASS" else min(lines, 25), root=tier_dir)


# ---------- 进程级 run_tests.py 的 summary.json ----------

def stress_stats(directory):
    """压测组目录里的 *-summary.json 摘成一行。"""
    found = sorted(Path(directory).glob("*-summary.json"))
    if not found:
        return ""
    try:
        data = json.loads(found[0].read_text())
    except ValueError:
        return ""
    if "gsd_ops" in data:
        ops = data["gsd_ops"]
        lat = data.get("lat_ms_p50") or {}
        parts = ["%s 轮" % data.get("rounds"), "create %s / restore %s / delete %s" % (
            ops.get("create_ok"), ops.get("restore_ok"), ops.get("delete_ok"))]
        if lat.get("create") is not None:
            parts.append("p50 create %.0f ms、restore %.0f ms" % (lat["create"], lat.get("restore") or 0))
        if ops.get("errors"):
            parts.append("错误 %s" % json.dumps(ops["errors"], ensure_ascii=False))
        return "；".join(parts)
    if "recovery" in data:
        rec = data["recovery"]
        return "填满后恢复：restore c0 %s、之后 create %s、restore %s" % (
            rec.get("restore_c0"), rec.get("create_after"), rec.get("restore_after"))
    stats = data.get("stats") or {}
    parts = ["%s %s 轮" % (data.get("verdict"), data.get("rounds")),
             "create %s / restore %s / delete %s" % (stats.get("create_ok"), stats.get("restore_ok"), stats.get("delete_ok"))]
    if "verify_ok" in stats:
        parts.append("校验 %s 通过 / %s 失败" % (stats.get("verify_ok"), stats.get("verify_fail")))
    if data.get("unexpected_errors"):
        parts.append("意外错误 %s" % json.dumps(data["unexpected_errors"], ensure_ascii=False))
    return "；".join(parts)


def criu_summary(step_dir):
    path = Path(step_dir) / "summary.json"
    return json.loads(path.read_text()) if path.is_file() else None


def print_criu(step_dir, indent="    "):
    data = criu_summary(step_dir)
    if data is None:
        print(indent + "（没有 summary.json）")
        return
    print(indent + "判定：%s%s" % (data.get("verdict"), ("；" + clip(data["error"], 300)) if data.get("error") else ""))
    for s in data.get("steps", []):
        extra = stress_stats(Path(step_dir) / s["name"]) if (Path(step_dir) / s["name"]).is_dir() else ""
        if s.get("error"):
            extra = (extra + "；" if extra else "") + s["error"]
        print(indent + clip("%-5s %s%s" % (s["status"], s["name"], ("：" + extra) if extra else "")))
    for key in ("resources_before", "resources_after", "resources_delta"):
        if key in data:
            print(indent + clip("%s：%s" % (key, json.dumps(data[key], ensure_ascii=False))))


def print_criu_detail(step_dir, lines=40):
    """验收：性能三轮的表格和各项日志末尾；压测：每组摘要，失败组再打印错误样本与日志末尾。"""
    step_dir = Path(step_dir)
    data = criu_summary(step_dir)
    print_criu(step_dir, indent="")
    if data is None:
        return
    for s in data.get("steps", []):
        log = step_dir / (s["name"] + ".log")
        if s["name"].startswith("perf-run") and log.is_file():
            text = log.read_text(errors="replace").splitlines()
            start = next((i for i, l in enumerate(text) if l.startswith("档位")), max(0, len(text) - 30))
            print("---- %s 的分档表（第 %d 行起）----" % (log.name, max(0, start - 1) + 1))
            for line in text[max(0, start - 1):]:
                print(line)
            continue
        group = step_dir / s["name"]
        if group.is_dir():
            summ = sorted(group.glob("*-summary.json"))
            if s["status"] != "PASS" and summ:
                d = json.loads(summ[0].read_text())
                samples = (d.get("error_samples") or [])[:8] + (d.get("verify_fail_samples") or [])[:4]
                if samples:
                    print("---- %s 的错误样本（前 %d 条）----" % (s["name"], len(samples)))
                    for line in samples:
                        print(clip(line, 300))
            if s["status"] != "PASS":
                show_file(log, lines, root=step_dir)
                show_file(group / "RESULT", 5, root=step_dir)
            continue
        show_file(log, lines if s["status"] != "PASS" else 6, root=step_dir)
