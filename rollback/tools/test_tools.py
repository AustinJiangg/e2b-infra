#!/usr/bin/env python3
"""手册工具回归测试；仅使用临时目录，不修改手册或发布件。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="handbook-tools-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def links(self, *paths):
        return subprocess.run(
            [sys.executable, str(TOOLS / "mdlinks.py"), *map(str, paths)],
            capture_output=True, text=True,
        )

    def test_valid_link_and_chinese_anchor(self):
        source = self.write("docs/a.md", "# 首页\n[目标](../other/a.md#目标标题)\n")
        self.write("other/a.md", "# 目标标题\n")
        result = self.links(source)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_target_fails(self):
        source = self.write("a.md", "# 首页\n[不存在](missing.md)\n")
        result = self.links(source)
        self.assertEqual(result.returncode, 1)
        self.assertIn("MISSING-FILE", result.stdout)

    def test_missing_anchor_fails(self):
        source = self.write("a.md", "# 首页\n[不存在](#missing)\n")
        result = self.links(source)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ANCHOR-MISS", result.stdout)

    def test_code_example_links_are_ignored(self):
        source = self.write("a.md", "# 示例\n```md\n[示例](missing.md)\n```\n")
        self.assertEqual(self.links(source).returncode, 0)

    def test_empty_input_fails(self):
        self.assertNotEqual(self.links(self.root).returncode, 0)

    def test_generator_is_deterministic(self):
        docs = self.root / "docs"
        self.write("docs/README.md", "# 测试手册\n[正文](01-test.md#章节)\n")
        self.write("docs/01-test.md", "# 测试正文\n## 章节\n内容。\n```mermaid\nflowchart LR\n A --> B\n```\n")
        runtime = self.write("mermaid.js", "/* 测试用图表运行库占位 */")
        config = self.write("book.json", json.dumps({
            "title": "测试手册", "version": "v1.0.0", "date": "2026-10-15",
            "byline": "测试", "svgs": [],
        }, ensure_ascii=False))
        outputs = [self.root / "one.html", self.root / "two.html"]
        for output in outputs:
            result = subprocess.run([
                sys.executable, str(TOOLS / "build_docs_html.py"), str(docs),
                "-", str(runtime), str(output), "--config", str(config),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
        html = outputs[0].read_text(encoding="utf-8")
        self.assertIn('href="#doc--01-test--章节"', html)
        self.assertIn('<pre class="mermaid">', html)
        self.assertIn("测试手册 v1.0.0", html)
        self.assertIn(runtime.read_text(encoding="utf-8"), html)

    def test_build_usage_rejects_extra_arguments(self):
        result = subprocess.run([
            "bash", str(TOOLS / "build.sh"), "one.html", "two.html",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js")
    def test_mermaid_missing_dependencies_fail(self):
        result = subprocess.run([
            "node", str(TOOLS / "mdmermaid.mjs"), "--node-modules",
            str(self.root / "node_modules"), str(self.root / "missing.md"),
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("校验没有执行", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
