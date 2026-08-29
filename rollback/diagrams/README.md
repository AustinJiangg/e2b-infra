# 汇报用架构图

checkpoint / restore（950 ext4 执行路径）的展示素材。三张图各回答一个问题，
可以单独用，也可以按 01 → 02 → 03 的顺序连讲。

## 三张图

| 文件 | 回答什么问题 | 什么时候用 |
|---|---|---|
| `01-architecture.svg` | 这套东西由哪些部分组成，优化点分别落在哪一层 | 讲整体设计，一页看完 |
| `02-diff-tree.svg` | 内存产物为什么便宜，一次回滚在宿主侧是怎么算出来的 | 讲技术核心，回答「凭什么快」 |
| `03-vs-native.svg` | 和 e2b 原生 snapshot / resume 比，路径差在哪 | 讲价值与分工，回答「为什么不用现成的」 |

三张图都是**纯 SVG、无外部依赖**：浏览器直接打开、PowerPoint / WPS 可直接插入并保持矢量清晰度、
放大到任意倍数不糊。中文字体走系统栈（微软雅黑 / 苹方 / 思源黑体），在 Windows 和 macOS 上都能正常显示。

## 同样内容的 mermaid 版

`.mmd` 文件是同一批图的可编辑骨架，给需要自己改内容的场合用（贴进飞书 / 语雀 / GitHub /
slidev 的 ```mermaid 代码块都能直接渲染）。

| 文件 | 对应 |
|---|---|
| `01-architecture.mmd` | 01 的 flowchart 版 |
| `02-diff-tree.mmd` | 02 左半（树结构）的 flowchart 版 |
| `03-sequence.mmd` | 冻结窗口时序图，SVG 版里没有的一张 |

排版精度不如手写的 SVG（mermaid 自动布局），正式汇报建议用 SVG，改图用 mermaid。

## 幻灯片

slidev 工程还在 WSL 工作区 `e2b-repo/slides/`（**尚未随本仓库同步**），已经把这三张图和
优化点表格串成一套稿子：

```bash
cd ~/projects/e2b-repo/slides && npm run dev
```

然后在 **Windows 浏览器**打开 <http://localhost:3030>。`npm run dev` / `npm run build`
会自动把本目录的 SVG 同步到 `slides/public/diagrams/`（`npm run sync`），改完图直接刷新即可。

## 要 PNG / PDF 怎么办

三张 SVG 在 PPT 里是矢量的，通常不需要转。确实需要位图时：

```bash
cd ~/projects/e2b-repo/slides && npm run export
```

`slidev export` 出 PDF，加 `--format png` 出逐页 PNG（依赖 `playwright-chromium`，已装在
`slides/node_modules`）。单张图要 PNG，最省事的办法是在浏览器里打开 SVG 后直接截图或右键另存。

## 改图注意

- 图里**不含任何实测数字**，是有意为之：950 上还没有数据，920B 的数字口径不同，
  混进架构图容易被当成 950 的性能承诺。数字统一放在幻灯片的「实测覆盖与缺口」一页，并标注口径。
- `03-vs-native.svg` 里「脏页判据的三方差异」那一块**不要删**。对比基准是我们所基于的
  **ARM 适配版**，而 x86 原生的增量本来就是精确的 —— 少了这块说明，图会被读成
  「我们比 e2b 原生强」，那是不对的。
- 改完跑一次几何自检，防止中文变长后压框：脚本在 WSL 工作区 `e2b-repo/tmp/svgcheck.py`
  （估算文本宽度并比对容器边界，另检查框与框的非法重叠），**尚未随本仓库同步**。
