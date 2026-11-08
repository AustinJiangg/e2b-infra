# diagrams：本手册的图在哪里

本手册的图都在 [`../docs/img/`](../docs/img/)，格式是 SVG，正文里用 `![说明](img/NN-k-名字.svg)` 引用。这个目录里只放这一页说明。

- **画图脚本：** 每张图都由 [`rollback/tools/figures/`](../../rollback/tools/figures/README.md) 里的 `draw_gsd_<章>.py` 生成。两本手册共用这套脚本、配色和公共函数。不要手改 SVG。
- **改图：** 先改脚本，再重跑脚本。数据图的数字集中写在脚本开头，并注明了出处行号。重测之后，先改正文里的表格，再改脚本开头的数字。
- **看图：** GitHub 网页和 VS Code 的 Markdown 预览会直接显示这些 SVG。单文件 HTML 版（[`../dist/`](../dist/)）把图内嵌进了文件里。
- **还没画的图：** 950 分档（第 22 章）和 950 同平台对比（第 24 章 §5）。这两处正文里的数据还是待填，等数据出来后再画。

正文里不再使用 mermaid 代码块。正文中出现 mermaid 代码块时，`bash rollback/tools/build.sh --book gsd` 会报错。
