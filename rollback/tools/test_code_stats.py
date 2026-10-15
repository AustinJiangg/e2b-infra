#!/usr/bin/env python3
"""代码统计回归：词法边界、端点差分、上游排除、测试拆分与去重。"""
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

import code_stats as stats


class CodeLinesTests(unittest.TestCase):
    def test_python_docstrings_are_not_payload_strings(self):
        source = '''"""模块说明
第二行说明
"""
payload = """guest 程序
print("# 不是宿主注释")
"""
def f():
    """函数说明"""; return "# 保留字符串"
# 注释
'''
        self.assertEqual(stats.code_lines(source, "python"), {4, 5, 6, 7, 8})

    def test_multiline_comments_and_string_literals(self):
        source = 'package demo\n/* 注释\n第二行 */\nvar url = "http://a/#b" // 行内注释\n\n'
        self.assertEqual(stats.code_lines(source, "go"), {1, 4})
        self.assertEqual(stats.code_lines('# 宿主注释\necho "# 字符串"\n', "bash"), {2})

    def test_rust_module_and_feature_do_not_swallow_production(self):
        source = '''fn before() {}
/// 测试模块
#[cfg(test)]
mod tests {
    const BRACES: &str = "}";
    // }
}
fn between() {}
#[cfg(feature = "rollback-fault-inject")]
const NAME: &str = "{";
#[cfg(feature = "rollback-fault-inject")]
fn injected() { if true { let _ = "}"; } }
fn after() {}
'''
        self.assertEqual(stats.rust_test_lines(source), set(range(2, 8)))
        self.assertEqual(stats.rust_test_lines(source, True), {9, 10, 11, 12})
        self.assertIn(3, stats.code_lines(source, "rust"))
        self.assertNotIn(2, stats.code_lines(source, "rust"))


class FixedGitStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rollback-stats-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kas, self.infra = self.root / "kas", self.root / "infra"
        for repo in (self.kas, self.infra):
            repo.mkdir()
            self.git(repo, "init", "-q")
            self.git(repo, "config", "user.name", "stats-test")
            self.git(repo, "config", "user.email", "stats-test@example.invalid")
        self.write(self.kas, "main.go", "package demo\nvar Old = 1\n")
        self.base = self.commit(self.kas)
        self.write(self.kas, "main.go", "package demo\nvar Old = 2 // 修改上游一行\n\n// 新增注释\nvar New = 3\n")
        self.write(self.kas, "main.rs", "fn prod() {}\n#[cfg(test)]\nmod tests { #[test] fn works() {} }\n")
        self.write(self.kas, "sdk_test.py", "def test_sdk():\n    assert True\n")
        self.mr = self.commit(self.kas)
        self.write(self.kas, "main.go", "package demo\nvar Old = 2 // 修改上游一行\n\n// 新增注释\nvar New = 4\nvar Later = 5\n")
        self.current = self.commit(self.kas)
        self.write(self.infra, "rollback/scripts/check.py", "def test_sdk():\n    assert True\n")
        self.write(self.infra, "rollback/scripts/verify.py", '"""验收说明"""\nprint("verify")\n')
        infra_ref = self.commit(self.infra)
        self.scope = {
            "as_of": "2026-10-15", "refs": {"baseline": self.base, "mr": self.mr, "current": self.current, "infra_current": infra_ref},
            "kas_files": {
                "main.go": {"kind": "production", "component": "go", "scope": "rollback"},
                "main.rs": {"kind": "production", "component": "rust", "scope": "rollback"},
                "sdk_test.py": {"kind": "test", "test_kind": "unit", "usage": "源码单测"},
            },
            "infra_files": {
                "rollback/scripts/check.py": {"kind": "excluded", "reason": "测试副本", "identical_to": {"ref": "current", "path": "sdk_test.py"}},
                "rollback/scripts/verify.py": {"kind": "test", "test_kind": "integration", "usage": "验收"},
            },
        }

    def git(self, repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def write(self, repo, path, source):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    def commit(self, repo):
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "test snapshot")
        return self.git(repo, "rev-parse", "HEAD")

    def test_endpoints_exclude_upstream_and_count_duplicate_once(self):
        result = stats.measure(self.kas, self.infra, self.scope)
        # 已有 package 和 Old 声明不作为整文件新增；重写 Old 保留新增、删除两侧。
        mr = result["periods"]["mr"]["by_component"]["go"]
        self.assertEqual(mr["sloc"], {"added": 2, "deleted": 1, "net": 1})
        self.assertEqual(mr["physical"], {"added": 4, "deleted": 1, "net": 3})
        self.assertEqual(result["periods"]["current"]["production"]["sloc"]["net"], 3)
        self.assertEqual(result["tests_current"]["total"]["sloc"]["net"], 5)
        # 固定对象不读取工作区里尚未提交的内容。
        self.write(self.kas, "main.go", "这不是有效 Go 源码，也不应被统计\n")
        self.assertEqual(result, stats.measure(self.kas, self.infra, self.scope))

    def test_unclassified_file_fails(self):
        scope = copy.deepcopy(self.scope)
        del scope["kas_files"]["main.go"]
        with self.assertRaisesRegex(ValueError, "范围清单缺少"):
            stats.measure(self.kas, self.infra, scope)

    def test_generated_blocks_reject_manual_drift(self):
        result = stats.measure(self.kas, self.infra, self.scope)
        self.write(self.root, "docs/00-overview.md", "<!-- code-stats:summary:start -->\n<!-- code-stats:summary:end -->\n")
        markers = ("versions", "production", "scope", "sdk-provenance", "tests", "physical", "exclusions")
        self.write(self.root, "docs/C-code-size-and-methodology.md", "\n".join(
            f"<!-- code-stats:{name}:start -->\n<!-- code-stats:{name}:end -->" for name in markers))
        stats.sync_docs(result, root=self.root)
        stats.sync_docs(result, root=self.root, check=True)
        path = self.root / "docs/00-overview.md"
        path.write_text(path.read_text().replace("MR 版", "手工改动", 1))
        with self.assertRaisesRegex(ValueError, "统计摘要或表格"):
            stats.sync_docs(result, root=self.root, check=True)

    def test_diverged_duplicate_fails(self):
        self.write(self.infra, "rollback/scripts/check.py", "def test_sdk():\n    assert False\n")
        scope = copy.deepcopy(self.scope)
        scope["refs"]["infra_current"] = self.commit(self.infra)
        with self.assertRaisesRegex(AssertionError, "副本已分叉"):
            stats.measure(self.kas, self.infra, scope)

    def test_new_script_requires_scope_review(self):
        self.write(self.infra, "rollback/scripts/new.py", "print(1)\n")
        scope = copy.deepcopy(self.scope)
        scope["refs"]["infra_current"] = self.commit(self.infra)
        with self.assertRaisesRegex(AssertionError, "脚本范围已变化"):
            stats.measure(self.kas, self.infra, scope)

    def test_sdk_source_uses_author_and_reconciles_across_rename(self):
        scope = copy.deepcopy(self.scope)
        old, new = "py-sdk/e2b/gsd/api.py", "py-sdk/e2b/checkpointd/api.py"
        self.write(self.kas, old, "def checkpoint():\n    return 1\n")
        self.write(self.kas, "spec/checkpoint.proto", 'syntax = "proto3";\n')
        self.write(self.kas, "py-sdk/generated.py", "generated = 1\n")
        self.git(self.kas, "add", ".")
        self.git(self.kas, "commit", "-qm", "SDK foundation", "--author", "penghaozhou <phz@example.invalid>")
        foundation = self.git(self.kas, "rev-parse", "HEAD")
        (self.kas / new).parent.mkdir(parents=True)
        self.git(self.kas, "mv", old, new)
        self.write(self.kas, new, "def checkpoint():\n    return 1\nTIMEOUT = 300\n")
        scope["refs"]["mr"] = self.commit(self.kas)
        self.write(self.kas, new, "def checkpoint():\n    return 2\nTIMEOUT = 300\n")
        scope["refs"]["current"] = self.commit(self.kas)
        scope["kas_files"][new] = {"kind": "production", "component": "sdk", "scope": "rollback"}
        scope["kas_files"]["spec/checkpoint.proto"] = {"kind": "production", "component": "support", "scope": "shared"}
        scope["kas_files"]["py-sdk/generated.py"] = {"kind": "generated"}
        scope["sdk_provenance"] = {"foundation": foundation, "foundation_author_email": "phz@example.invalid",
                                   "adaptation": scope["refs"]["mr"], "path_aliases": {old: new}}
        report = stats.measure(self.kas, self.infra, scope)
        result = report["sdk_provenance"]
        self.assertEqual(result["commits"]["foundation"]["author"], "penghaozhou")
        self.assertEqual(result["commits"]["foundation"]["committer"], "stats-test")
        stages = result["stages"]
        self.assertEqual(stages["foundation"]["runtime"]["sloc"]["net"], 2)
        self.assertEqual(stages["foundation"]["support"]["sloc"]["net"], 1)
        self.assertEqual(stages["mr_adaptation"]["runtime"]["sloc"]["net"], 1)
        self.assertEqual(stages["after_mr"]["runtime"]["sloc"], {"added": 1, "deleted": 1, "net": 0})
        self.assertEqual(stages["own_current"]["runtime"]["sloc"]["net"], 1)
        self.assertEqual(report["periods"]["current"]["by_component"]["sdk"]["sloc"]["net"], 3)
        scope["sdk_provenance"]["foundation_author_email"] = "stats-test@example.invalid"
        with self.assertRaisesRegex(AssertionError, "原始作者"):
            stats.measure(self.kas, self.infra, scope)


if __name__ == "__main__":
    unittest.main(verbosity=2)
