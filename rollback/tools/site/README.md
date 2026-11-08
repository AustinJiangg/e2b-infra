# site

`build_docs_site.py`、`theme_toggle.py` 复制自 md-docs-site skill（hdbss-dirty-tracking 仓库 `0998d95` 的 `.claude/skills/md-docs-site/scripts/`），没有修改。

它们负责排版：把一个目录下的 Markdown 合成一个单文件 HTML，图片以 data URI 内嵌。排版规则见该 skill 的 SKILL.md。

手册专用的处理不放在这里，而是放在上一级的 `build_book.py`，包括：

- 按 `contents.json` 分篇；
- 把指向手册以外的链接改写成 GitHub 地址；
- 检查生成结果。

要升级生成器时，整份替换这两个文件，并更新上面的提交号，不要在这里直接改。
