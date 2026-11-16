# vendor：生成时内嵌的第三方文件

文档里用到公式、mermaid 时，`build_docs_site.py` 才把对应文件内嵌进 HTML，所以生成的页面离线可用。

| 文件 | 来源 | 许可 |
| --- | --- | --- |
| `katex.min.js`、`katex.min.css` | KaTeX 0.16.11（npm `katex`）。css 里只保留 woff2 字体，并以 data URI 内嵌 | MIT，见 `LICENSE-katex` |
| `mermaid.min.js` | mermaid 10.9.3（npm `mermaid`，`dist/mermaid.min.js`） | MIT，见 `LICENSE-mermaid` |
| `emoji.json` | gemoji 8.1.0（npm `gemoji`），整理成「短码 → emoji」的对照表，共 1913 个 | MIT，见 `LICENSE-gemoji` |

升级时用 `npm pack katex@<版本> mermaid@<版本> gemoji@<版本>` 下载再解包，按上表重新生成这几个文件：

- KaTeX：`katex.min.css` 里每条 `src:` 换成对应 woff2 字体的 data URI。
- emoji：用 gemoji 的 `names` 字段生成对照表。
