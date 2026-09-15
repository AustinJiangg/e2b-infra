# rollback —— checkpoint / restore 的文档、配图与验证工具箱

沙箱活着时的高频快速回退。本目录是这套功能的**文档与验证材料**；
实现代码在仓库根的 `0001-adapted-for-arm-architecture.patch` 与 `firecracker.arm` 里；
上游归宿是 openEuler [KASandbox `deltabox`](https://gitcode.com/openeuler/KASandbox/tree/deltabox)（[MR !119](https://gitcode.com/openeuler/KASandbox/pull/119)，2026-09-09 合入）。

| 路径 | 内容 | 给谁 |
|---|---|---|
| [`docs/`](docs/) | **技术手册**，32 篇，从「这是个什么问题」到「怎么证明它对、它快」再到「怎么在它上面继续开发」 | 首要入口 |
| [`diagrams/`](diagrams/) | 三张汇报用 SVG + Mermaid 图源 | 需要单独看图或改图的人 |
| [`slides/`](slides/) | Slidev 汇报稿与构建脚本 | 需要浏览器演示或导出的人 |
| [`scripts/`](scripts/) | **快照回滚的全部脚本**：`acceptance/` 交付态验收、`probes/` 开发态探针、`dev/` 开发态工具箱（原 `test-950/`）与 12 份实测报告 | 验收方（`acceptance/`）；我们自己（另两个） |
| **[Releases](https://github.com/AustinJiangg/e2b-infra/releases)** | 文档打包件与汇报稿 PDF，下载下来直接发给别人 | 要把材料发出去的人 |

> 构建产物一律不进仓库：`slides/` 只跟踪源文件（`node_modules`、`dist/`、
> 同步过来的 `public/diagrams/`、导出的 PDF 都不跟踪），`dist/`（文档打包件）整个不跟踪。
> 它们的成品在 [Releases](https://github.com/AustinJiangg/e2b-infra/releases) 里。

## 从哪读起

| 你是 | 建议路径 |
|---|---|
| 想先看个全貌 | [`docs/00-design-overview.md`](docs/00-design-overview.md) |
| 不写这类代码，但想搞懂它在干什么 | [`docs/01`](docs/01-what-and-why.md) → [`02`](docs/02-microvm-and-e2b.md) → [`03`](docs/03-snapshot-fundamentals.md) |
| 工程师，要接手或参与 | 全书；机制最短路径 [`06`](docs/06-architecture.md) → [`07`](docs/07-dirty-page-tracking.md) → [`08`](docs/08-memory-diff-tree.md) → [`10`](docs/10-disk-layering.md) → [`11`](docs/11-in-place-rollback.md) → [`13`](docs/13-end-to-end.md) |
| 系统工程师 / 运维 | [`19 鲲鹏平台`](docs/19-kunpeng-platform.md) → [`17 可观测与验证`](docs/17-observability-and-verification.md) |
| 使用方，判断什么时候用它 | [`00`](docs/00-design-overview.md) → [`16 生命周期与边界`](docs/16-lifecycle-and-portability.md) → [`20 与原生的对比`](docs/20-vs-native.md) |
| 要看测试证据 / 验收 | [`24 测试体系总览`](docs/24-test-overview.md) → [`28 实测结果与达标判定`](docs/28-results-and-compliance.md) → [`29 上机验收操作`](docs/29-acceptance-runbook.md) |

完整目录见 [`docs/README.md`](docs/README.md)。

## 要在机器上验收

| 想做什么 | 用什么 |
|---|---|
| 正确性验收 | [`scripts/acceptance/checkpoint_verify.py`](scripts/acceptance/checkpoint_verify.py) |
| 性能基准 | [`scripts/acceptance/checkpoint_bench.py`](scripts/acceptance/checkpoint_bench.py) |
| 深入排查 | [`scripts/dev/`](scripts/dev/) |
| 机理探针、跨实现对照 | [`scripts/probes/`](scripts/probes/) |

前两个是**交付态**脚本：各自单文件、零共享依赖，两条命令跑完。
`scripts/dev/`（原 `test-950/`）是**开发态**工具箱，脚本之间有共享模块、需要配路径。
凭据怎么配、950 与 920B 各自要验哪些结论，见 [`scripts/README.md`](scripts/README.md)。

> 两套测试脚本**不要混用**。宿主探针在两个交付脚本里故意重复了一份，
> 就是为了防止只拷走一半、剩下的静默变成 unknown。

**讲解见 [`docs/25`](docs/25-functional-tests.md)（每条正确性判据怎么设计的）与
[`docs/26`](docs/26-performance-methodology.md)（判定口径与负载构造），
操作见 [`docs/29`](docs/29-acceptance-runbook.md)（前置条件、三条命令、期望末行、结果回传规范）。**

## 当前状态

2026-08-29 已在 950 上通过正确性验收，**脏页后端为 HDBSS（硬件标脏）**，59 项校验全过；
920B 上全套跑完（树语义、pause 兼容、原生操作兼容矩阵、200 次连续回滚 0 失败）。
性能分档基准尚未在 950 上跑过；920B 上的全部历史数字来自软件写保护路径，两者口径不同、不能相减。

**全书的实测数字与对照客户指标的逐档判定只在
[`docs/28-results-and-compliance.md`](docs/28-results-and-compliance.md) 一处维护**，
尚未覆盖的缺口也在那里。

## 把文档发给别人

去 [Releases](https://github.com/AustinJiangg/e2b-infra/releases) 下载附件，然后把文件发过去就行 —— 对方不需要仓库权限。

当前文档版本 **v0.2.1**（2026-09-14，江路路 j30059180）；每份产物的封面与侧栏都印着版本号，
Release tag 与之一致，修订规则见 [`docs/README.md#版本`](docs/README.md#版本)。

| 附件 | 大小 | 给谁 |
|---|---|---|
| `checkpoint-restore-docs.html` | 4.1 MB | **推荐**。单文件，双击用浏览器打开，不联网、不装任何东西 |
| `checkpoint-restore-docs.zip` | 1.4 MB | 要 Markdown 原文的人（Windows 友好） |
| `checkpoint-restore-docs.tar.gz` | 1.4 MB | 同上，Linux 习惯 |
| `checkpoint-restore-slides.pdf` | 1.4 MB | 汇报稿 |

HTML 版把 704 条跨篇链接全部改写成了页内锚点，7 张 mermaid 图内联渲染、
三张 SVG 内嵌在附录，所以怎么传都不会断；浏览器 Ctrl+P 可直接存 PDF。

改完 `docs/` 之后重新生成（在 WSL 工作区 `e2b-repo/` 下跑）：

```bash
python3 tmp/build_docs_html.py e2b-infra/rollback/docs e2b-infra/rollback/diagrams e2b-infra/rollback/slides/node_modules/mermaid/dist/mermaid.min.js e2b-infra/rollback/dist/checkpoint-restore-docs.html
```

打包与两个校验脚本（`mdlinks.py` 查链接与中文锚点、`mdmermaid.mjs` 查 mermaid 语法）
也在 `e2b-repo/tmp/`，尚未随本仓库同步。生成完新建一个 Release 传上去即可。

## 关于 `scripts/dev/bin/`

四个被测二进制（两个 orchestrator 各 108 MB、两个 Firecracker）**不进本仓库** ——
它们是构建产物，可从 `infra-arm` / `KASandbox` 的对应 commit 重建，且超过 GitHub 的单文件上限。
目录里只保留 `SHA256SUMS`，用来对上「当时验的是哪一版」。二进制本身在 WSL 工作区。

## 不在这里的东西

方案设计稿、原理调研、过程日志与一次性探针仍在 WSL 工作区 `e2b-repo/`，尚未同步到本仓库。
`infra-arm` 与 `KASandbox` 的源码分支各自推到自己的 origin，不在这里重复存放。
