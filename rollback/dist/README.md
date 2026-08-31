# dist —— 可直接分发的文档打包件

这三个文件是 [`../docs/`](../docs/) 的**构建产物**，内容与源文件一致，
放在这里是为了「直接发给别人」时不用现场打包。

| 文件 | 大小 | 给谁 |
|---|---|---|
| `checkpoint-restore-docs.html` | 4.1 MB | **推荐**。单文件，双击用浏览器打开，不联网、不装任何东西 |
| `checkpoint-restore-docs.zip` | 1.4 MB | 要 Markdown 原文的人（Windows 友好） |
| `checkpoint-restore-docs.tar.gz` | 1.4 MB | 同上，Linux 习惯 |

## HTML 版做了什么

- 23 篇正文 + 目录 + 编写规约拼成一页，左侧粘性目录（当前章节高亮、可输入过滤）
- **所有链接改写成页内锚点** —— 704 条，不依赖文件布局，怎么传都不会断
- 7 张 mermaid 流程图内联渲染（mermaid 11.17 打进文件里，离线可用）
- 三张汇报用 SVG 内嵌在附录
- 213 张表格各自横向滚动，正文不横向滚动
- 带打印样式：浏览器 Ctrl+P 可存 PDF，每篇自动分页，表格与代码块不跨页断开

## 压缩包里有什么

```
checkpoint-restore-docs/
├── checkpoint-restore-docs.html   # 同上，单文件版
├── docs/                          # 23 篇 Markdown 原文 + README + OUTLINE
├── diagrams/                      # 三张 SVG 与 mermaid 图源
└── 开始阅读.txt
```

包里**不含** `slides/` 与 `test-950/`（一个 575 MB、一个 223 MB，绝大部分是
`node_modules` 与二进制）。指向这两处的 2 个链接在打包件里已降级为纯文本，
所以解压之后**没有死链** —— 这一点每次打包都会校验。

## 怎么重新生成

改完 `docs/` 之后：

```bash
python3 tmp/build_docs_html.py e2b-infra/rollback/docs e2b-infra/rollback/diagrams e2b-infra/rollback/slides/node_modules/mermaid/dist/mermaid.min.js e2b-infra/rollback/dist/checkpoint-restore-docs.html
```

生成脚本与两个校验脚本（`mdlinks.py` 链接与中文锚点、`mdmermaid.mjs` mermaid 语法）
在 WSL 工作区 `e2b-repo/tmp/`，尚未随本仓库同步。

> **注意**：这些是构建产物。**只有 [`../docs/`](../docs/) 是源**，
> 改内容请改那里，然后重新生成，不要直接编辑本目录的文件。
