# rollback —— checkpoint / restore 的文档、配图与验证工具箱

沙箱活着时的高频快速回退。本目录是这套功能的**文档与验证材料**；
实现代码在仓库根的 `0001-adapted-for-arm-architecture.patch` 与 `firecracker.arm` 里。

| 路径 | 内容 | 给谁 |
|---|---|---|
| [`docs/`](docs/) | **技术文档系列**，23 篇，从「这是个什么问题」到「怎么在它上面继续开发」 | 首要入口 |
| [`diagrams/`](diagrams/) | 三张汇报用 SVG + Mermaid 图源 | 需要单独看图或改图的人 |
| [`slides/`](slides/) | Slidev 汇报稿与构建脚本 | 需要浏览器演示或导出的人 |
| [`test-950/`](test-950/) | 开发态验证工具箱：宿主自检 / 造数据卷 / 两套之间切换 / 穷举与劣化排查脚本，以及 11 份实测报告 | 我们自己，做上机验证与排查 |
| [`dist/`](dist/) | 文档的打包件：单文件 HTML（双击即看）与 zip / tar.gz | 要把文档发给别人的人 |

## 从哪读起

| 你是 | 建议路径 |
|---|---|
| 想先看个全貌 | [`docs/00-design-overview.md`](docs/00-design-overview.md) |
| 不写这类代码，但想搞懂它在干什么 | [`docs/01`](docs/01-what-and-why.md) → [`02`](docs/02-microvm-and-e2b.md) → [`03`](docs/03-snapshot-fundamentals.md) |
| 工程师，要接手或参与 | 全书；机制最短路径 [`06`](docs/06-architecture.md) → [`07`](docs/07-dirty-page-tracking.md) → [`08`](docs/08-memory-diff-tree.md) → [`10`](docs/10-disk-layering.md) → [`11`](docs/11-in-place-rollback.md) → [`13`](docs/13-end-to-end.md) |
| 系统工程师 / 运维 | [`19 鲲鹏平台`](docs/19-kunpeng-platform.md) → [`17 可观测与验证`](docs/17-observability-and-verification.md) |
| 使用方，判断什么时候用它 | [`00`](docs/00-design-overview.md) → [`16 生命周期与边界`](docs/16-lifecycle-and-portability.md) → [`20 与原生的对比`](docs/20-vs-native.md) |

完整目录见 [`docs/README.md`](docs/README.md)。

## 要在机器上验收

| 想做什么 | 用什么 |
|---|---|
| 正确性验收 | 仓库根的 `benchmark/checkpoint_verify.py` |
| 性能基准 | 仓库根的 `benchmark/checkpoint_bench.py` |
| 深入排查 | [`test-950/`](test-950/) |

前两个是**交付态**脚本：各自单文件、零共享依赖，两条命令跑完。
`test-950/` 是**开发态**工具箱，脚本之间有共享模块、需要配路径。

> 两套测试脚本**不要混用**。宿主探针在两个交付脚本里故意重复了一份，
> 就是为了防止只拷走一半、剩下的静默变成 unknown。

设计原则与口径说明见 [`docs/17-observability-and-verification.md`](docs/17-observability-and-verification.md)。

## 当前状态

2026-08-29 已在 950 上通过正确性验收，**脏页后端为 HDBSS（硬件标脏）**，59 项校验全过。
性能分档基准尚未在 950 上跑过；920B 上的全部历史数字来自软件写保护路径，两者口径不同。

详见 [`docs/17-observability-and-verification.md §4`](docs/17-observability-and-verification.md#4-当前实测状态)。

## 关于 `test-950/bin/`

四个被测二进制（两个 orchestrator 各 108 MB、两个 Firecracker）**不进本仓库** ——
它们是构建产物，可从 `infra-arm` / `KASandbox` 的对应 commit 重建，且超过 GitHub 的单文件上限。
目录里只保留 `SHA256SUMS`，用来对上「当时验的是哪一版」。二进制本身在 WSL 工作区。

## 不在这里的东西

方案设计稿、原理调研、过程日志与一次性探针仍在 WSL 工作区 `e2b-repo/`，尚未同步到本仓库。
`infra-arm` 与 `KASandbox` 的源码分支各自推到自己的 origin，不在这里重复存放。
