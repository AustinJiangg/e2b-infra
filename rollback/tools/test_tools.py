#!/usr/bin/env python3
"""手册工具回归测试；仅使用临时目录，不修改手册或发布件。"""
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from markdown_it import MarkdownIt

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

    def test_empty_link_target_fails(self):
        source = self.write("a.md", "# 首页\n[失去目标的迁移链接]()\n")
        result = self.links(source)
        self.assertEqual(result.returncode, 1)
        self.assertIn("EMPTY-TARGET", result.stdout)

    def test_code_example_links_are_ignored(self):
        source = self.write("a.md", "# 示例\n```md\n[示例](missing.md)\n```\n")
        self.assertEqual(self.links(source).returncode, 0)

    def test_empty_input_fails(self):
        self.assertNotEqual(self.links(self.root).returncode, 0)

    def test_handbook_has_no_accidental_setext_headings(self):
        # 手册标题统一用 #；正文与 --- 之间漏空行会把整段正文提升为标题并污染目录。
        parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])
        docs = sorted((TOOLS.parent / "docs").glob("*.md"))
        self.assertTrue(docs, "没有找到待检查的手册")
        for path in docs:
            for token in parser.parse(path.read_text(encoding="utf-8")):
                if token.type == "heading_open":
                    with self.subTest(path=path.name, line=token.map[0] + 1):
                        self.assertNotIn(
                            token.markup, ("-", "="),
                            f"{path.name}:{token.map[0] + 1}：正文后分隔线前可能缺少空行",
                        )

    SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'

    def book(self, config=None, chapters=("01-first.md", "02-second.md"), extra=None):
        """在临时目录里搭一本两章的小书。"""
        self.write("book/docs/README.md", "# 首页\n[第一章](01-first.md#契约)\n")
        self.write("book/docs/01-first.md",
                   "# 01 · 第一章\n## 契约\n只在正文出现的关键字。\n\n![示意](img/01-1-示意.svg)\n\n"
                   "### 细节\n```text\n</script><script>不应执行</script>\n```\n")
        self.write("book/docs/02-second.md", "# 02 · 第二章\n## 配置\n见 [`a.py`](../scripts/a.py)。\n")
        self.write("book/docs/img/01-1-示意.svg", self.SVG)
        for name, text in (extra or {}).items():
            self.write(f"book/docs/{name}", text)
        self.write("book/contents.json", json.dumps({
            "parts": [{"title": "第一篇", "chapters": list(chapters)}], "appendices": [],
        }, ensure_ascii=False))
        self.write("book/book.json", json.dumps({
            "title": "测试手册", "contents": "contents.json",
            "repo_docs_path": "book/docs", **(config or {}),
        }, ensure_ascii=False))
        return self.root / "book"

    def build(self, book, output="book.html"):
        out = self.root / output
        result = subprocess.run([sys.executable, str(TOOLS / "build_book.py"), str(book), str(out)],
                                capture_output=True, text=True)
        return result, out

    def test_generator_is_deterministic(self):
        book = self.book()
        outputs = []
        for name in ("one.html", "two.html"):
            result, out = self.build(book, name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            outputs.append(out.read_bytes())
        self.assertEqual(outputs[0], outputs[1])

    def test_navigation_images_and_escaping(self):
        result, out = self.build(self.book(chapters=("02-second.md", "01-first.md")))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        html = out.read_text(encoding="utf-8")
        self.assertIn("测试手册", html)
        self.assertLess(html.index('href="#d02"'), html.index('href="#d01"'))   # 按分篇清单排
        self.assertIn('href="#d01-契约"', html)
        self.assertIn('id="d01-细节"', html)
        self.assertIn("data:image/svg+xml;base64,", html)
        self.assertIn("图 1-1", html)       # 新版生成器去掉图号的前导零
        self.assertNotIn("</script><script>不应执行", html)
        self.assertIn("&lt;/script&gt;&lt;script&gt;不应执行", html)

    def test_outside_links_pin_to_github_or_degrade(self):
        result, out = self.build(self.book({"gh_repo": "https://github.com/o/r", "git_ref": "v9"}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('href="https://github.com/o/r/blob/v9/book/scripts/a.py"', out.read_text(encoding="utf-8"))
        result, out = self.build(self.book())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        html = out.read_text(encoding="utf-8")
        self.assertNotIn("scripts/a.py", html)
        self.assertIn("<code>a.py</code>", html)

    def test_manifest_rejects_missing_and_duplicate_chapters(self):
        for chapters in (["01-first.md"], ["01-first.md", "02-second.md", "01-first.md"]):
            with self.subTest(chapters=chapters):
                result, out = self.build(self.book(chapters=chapters))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("分篇清单", result.stderr)
                self.assertFalse(out.exists())

    def test_mermaid_block_is_rejected(self):
        book = self.book(extra={"02-second.md": "# 02 · 第二章\n```mermaid\nflowchart LR\n A --> B\n```\n"})
        result, _ = self.build(book)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mermaid", result.stderr)

    def test_missing_image_fails_and_leaves_no_output(self):
        book = self.book(extra={"02-second.md": "# 02 · 第二章\n\n![缺](img/02-1-缺.svg)\n"})
        result, out = self.build(book)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(out.exists())

    def test_build_usage_rejects_extra_arguments(self):
        result = subprocess.run([
            "bash", str(TOOLS / "build.sh"), "one.html", "two.html",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
