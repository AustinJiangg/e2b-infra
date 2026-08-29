# checkpoint / restore 说明与配图

950 ext4 路线的设计说明与汇报素材。

## 目录内容

| 文件 | 是什么 |
|---|---|
| **`checkpoint-restore-design.md`** | **完整技术文档**：执行路径、差分树、分层封存、脏页判据、失败语义、优化点汇总、代码索引。三张图的内容都在里面展开讲了，内嵌的 mermaid 图可直接在 GitHub / VS Code 里渲染 |
| `01-architecture.svg` | 总体结构，优化点 ①–⑨ 标在对应层 |
| `02-diff-tree.svg` | 内存差分树，以及一次回滚在宿主侧怎么算出来 |
| `03-vs-native.svg` | 与 e2b 原生 snapshot / resume 的路径对比 |
| `01/02-*.mmd`、`03-sequence.mmd` | 上述图的 mermaid 源，供需要改内容时用。`01-architecture.mmd` 比设计说明里内嵌的那版多了优化点标注气泡 —— 文档正文另有汇总表，不必重复 |

## 怎么选

- **要读懂这套东西** → 看 `checkpoint-restore-design.md`，从 §0 定位开始。
- **要汇报** → 用三张 SVG。纯 SVG、无外部依赖，PowerPoint / WPS 可直接插入并保持矢量清晰度，
  放大不糊；中文走系统字体栈（微软雅黑 / 苹方 / 思源黑体），Windows 与 macOS 都能正常显示。
  浏览器和 VS Code 内置图像预览也能直接打开，不需要装任何插件。
- **要改图** → 改 `.mmd`。排版精度不如手写的 SVG（mermaid 是自动布局），正式汇报仍建议用 SVG。
  注意 `.mmd` 文件需要支持 mermaid 语言的扩展才能预览，
  仅装「Markdown Preview Mermaid Support」是不够的 —— 它只增强 Markdown 预览里的 `mermaid` 代码块。

三张图各回答一个问题，可以单独用，也可以按 01 → 02 → 03 的顺序连讲：
组成与优化点落在哪 → 凭什么快 → 为什么不用现成的。

## 改图注意

- **图里不含任何实测数字**，是有意为之：950 上还没有数据，920B 的数字口径不同，
  混进架构图容易被当成 950 的性能承诺。数字集中在设计说明 §8，并标注了口径与缺口。
- **`03-vs-native.svg` 里「脏页判据的三方差异」那一块不要删。** 对比基准是我们所基于的
  **ARM 适配版**，而 x86 原生的增量本来就是精确的 —— 少了这块说明，图会被读成
  「我们比 e2b 原生强」，那是不对的。同一段说明在设计说明 §5.2。
- 改完建议跑一次几何自检，防止中文变长后压框或溢出画布。脚本在 WSL 工作区
  `e2b-repo/tmp/svgcheck.py`（估算文本宽度并比对容器边界，另检查框与框的非法重叠），
  **尚未随本仓库同步**。

## 幻灯片

slidev 工程在 WSL 工作区 `e2b-repo/slides/`（**尚未随本仓库同步**），已把三张图和优化点表
串成一套稿子：

```bash
cd ~/projects/e2b-repo/slides && npm run dev
```

在 Windows 浏览器打开 <http://localhost:3030>。`npm run dev` / `npm run build` 会自动把本目录的
SVG 同步到 `slides/public/diagrams/`。
