# diagrams —— 本手册的图在哪

本手册**不另出独立的图文件**：全部图都以 mermaid 代码块内嵌在各章的 Markdown 里，随正文一起维护。
所以这个目录里只有这一页说明，没有 `.svg`，也没有单独的 `.mmd`。

## 图在哪几章

| 章 | 图讲什么 |
|---|---|
| [04 总体架构、请求路径与鉴权](../docs/04-architecture.md) | 组件与分工：请求从 SDK 到 guest 里的 gsd 经过谁 |
| [05 guest 启动、根文件系统与模板注入](../docs/05-guest-boot-and-injection.md) | `overlay-init` 搭出来的根文件系统：ext4 根盘、基础层、upper 与 work 的关系 |
| [07 overlayfs 热切层：并发、生命周期与修复](../docs/07-overlay-concurrency-and-fixes.md) | 修复后一次层切换在内核里的阶段图（提交点前后各做什么） |
| [11 端到端走查](../docs/11-end-to-end.md) | 时序全图：一次 checkpoint 与一次 restore 在 SDK、宿主代理、gsd、CRIU、内核之间怎么走 |
| [16 restore 为什么慢：一次归因](../docs/16-restore-slowness-case.md) | 归因链：从"restore 慢且抖"的现象一步步追到根因 |
| [18 进程级与整机级：分工与对照](../docs/18-process-vs-machine-level.md) | 进程级与整机级各自工作的位置：同一个 SDK 入口，请求在宿主代理处分岔 |

以各章正文为准；章里增删了图，这张表跟着改。

## 怎么看

- **GitHub / GitCode 网页、VS Code 的 Markdown 预览**（装了 mermaid 支持的扩展）会直接把代码块画成图。
- **单文件 HTML 版**（[`../dist/`](../dist/)）把 mermaid 打进了文件里，离线打开即可看到渲染后的图。
- 纯文本编辑器里看到的是 mermaid 源码，照着读也能读懂：节点和箭头都是一行一条。

## 怎么改

直接改对应章里的 ` ```mermaid ` 代码块。依赖按[共用工具说明](../../rollback/tools/README.md)安装，在 `e2b-infra` 仓库根目录校验：

```bash
node rollback/tools/mdmermaid.mjs --node-modules rollback/tools/node_modules gsd/docs/*.md
# 或同时校验链接和图表
bash rollback/tools/build.sh --book gsd --check
```

每张图一行 `OK` 或 `FAIL`，末行 `合计 N 张图，失败 0`。退出码 0 为全部通过，1 为有图解析失败，
2 为两个 node 包装载不了、校验根本没有执行——**2 不是通过**。

## 为什么不做 SVG

姊妹手册（[`../../rollback/diagrams/`](../../rollback/diagrams/)）另有独立 SVG 配图，是为汇报准备的。
本手册不做汇报稿，图只服务于正文，内嵌的 mermaid 足够，也省去"图与正文各改一处"的同步负担。
需要把某张图单独拿出去用时，从 HTML 版里截取，或把代码块贴进任何支持 mermaid 的工具导出。

打包工具对没有 SVG 的书不生成"配图附录"（`book.json` 里 `svgs` 为空），不需要这个目录里有任何图文件。
