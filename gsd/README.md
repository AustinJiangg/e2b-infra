# gsd —— 进程级 checkpoint / restore 的文档与验证工具箱

在沙箱不停机的前提下，把沙箱里的**用户进程和根文件系统**退回过去某一时刻：在 guest 内由守护进程 gsd 用 CRIU 保存与恢复进程，
用 overlayfs 热切层回滚文件。本目录是这套功能的**技术手册与验证材料**；
实现代码在 KASandbox_0904 的 `deltabox-gsd` 分支（gsd 守护进程、orchestrator 注入、Python SDK、guest 内核补丁同仓），不在这里重复存放。

> **v0.1.1**（2026-10-17）　原实现由 phz 完成，jll 整理与修复　·　运行部件基准 `259f1a25c`，当前 SDK `dcca61266`

| 路径 | 内容 | 给谁 |
|---|---|---|
| [`docs/`](docs/) | **技术手册**（教材版），导读 + 28 篇（含附录 A、B），按教学顺序分六部分：基础 → 设计与实现 → 工程保障 → 性能 → 验证与实测 → 使用与运维 | 首要入口 |
| [`scripts/`](scripts/) | **全部测试脚本**：`acceptance/` 功能验证与性能、`stress/` 压测与看门狗、`probes/` 探针、`ops/` 切栈脚本、`kernel/` 调试内核的 config 与构建说明。完整日志与原始结果不入库，已复核摘要见 `validation/` | 验收方（`acceptance/`）；接手开发的人（其余） |
| [`diagrams/`](diagrams/) | 只有一页说明：本手册的图都以 mermaid 内嵌在各章里，不另出 SVG | 想找图或改图的人 |
| [`dist/`](dist/) | 文档打包件（单文件 HTML）与重新打包的命令。构建产物，只有 `docs/` 是源 | 要把手册发给别人的人 |

`book.json` 是打包用的书目信息（书名、版本、日期、署名行），改版时与 `docs/README.md` 开头的信息块一起改。
`validation/` 保存已复核的性能样本摘要、环境版本与历史修复依据。`OUTLINE.md` 是本机起草清单，不入库。

## 源码、脚本与本轮验证

实现源码位于 [KASandbox_0904](https://gitcode.com/jiangluluu/KASandbox_0904) 的 `deltabox-gsd` 分支；本目录维护说明与验证工具，不重复存放实现代码。

| 部件 | 源码路径 | 本轮版本 |
|---|---|---|
| gsd、CRIU 基础镜像构建定义 | `packages/gsd/` | `259f1a25c` |
| orchestrator 注入、启动脚本与 service | `packages/orchestrator/`，其中 `internal/template/build/core/rootfs/gsdfiles/` 为 guest 文件 | `259f1a25c` |
| guest 内核配置、构建脚本、热切层补丁 | `fc-kernels/` | `259f1a25c` |
| Python SDK | `py-sdk/` | `dcca61266`，比运行部件基准多一项流量令牌传递修复 |
| 功能、性能、压测和探针 | 本仓库 `gsd/scripts/` | 本次提交；正式性能入口为 `acceptance/demo_checkpoint_perf.py` |
| 920B 三轮结果摘要及历史修复依据 | 本仓库 [`gsd/validation/`](validation/README.md) | 2026-10-16，原始结果文件哈希和各轮耗时可追溯 |

WSL 源码、920B 实际部署的性能脚本、950 待验证离线包中的性能脚本在本轮测试时逐字节一致，sha256 为 `e0096abeec4cdde9c8f1f34df2d35609d4e92390ec860833ec495f45f3dd5e02`。
`test_demo_checkpoint_perf.py` 是不连接沙箱的逻辑回归测试；`summarize_checkpoint_perf.py` 只校验已有三轮结果并生成摘要，两者均不采集性能样本。

当前 920B 部署位置为 `/fc-gsd/`，实际测试脚本位于 `/home/j30059180/projects/deltabox/gsd-stack/per-tier-20261016-v2/gsd/scripts/acceptance/demo_checkpoint_perf.py`。
950 的离线准备材料和测试工作区保留在仓库外；本轮不将它们表述为已部署或已验证，等实测完成后再补齐记录。

## 从哪读起

| 你是 | 建议路径 |
|---|---|
| 想先看个全貌 | [`docs/00-overview.md`](docs/00-overview.md) |
| 当教材从头学 | 按顺序读：[`00`](docs/00-overview.md) → 第一部分 [`01`](docs/01-problem-and-scope.md)–[`03`](docs/03-goals-and-tradeoffs.md) → 第二部分 [`04`](docs/04-architecture.md)–[`11`](docs/11-end-to-end.md) → 第三部分 [`12`](docs/12-failure-semantics.md)–[`15`](docs/15-known-limits.md) → 第四部分 [`16`](docs/16-restore-slowness-case.md)–[`18`](docs/18-process-vs-machine-level.md) → 第五部分 [`19`](docs/19-testing-and-debug-kernel.md)–[`20`](docs/20-results-and-gaps.md) → 第六部分 [`21`](docs/21-quickstart.md)–[`26`](docs/26-acceptance-runbook.md) |
| **客户开发者**：要在代码里调用 checkpoint / restore | [`21 快速上手`](docs/21-quickstart.md) → [`22 语义与使用边界`](docs/22-semantics-and-limits.md) → [`12 失败语义`](docs/12-failure-semantics.md)；想知道大概多快再看 [`20`](docs/20-results-and-gaps.md) 的"结论先行" |
| **运维**：部署、建模板、配置、排障、上机验收 | [`23 部署`](docs/23-deployment.md) → [`24 配置与可观测性`](docs/24-configuration-and-observability.md) → [`25 排障`](docs/25-troubleshooting.md) → [`26 上机验收`](docs/26-acceptance-runbook.md)；错误含义查 [`12`](docs/12-failure-semantics.md)，已知限制查 [`15`](docs/15-known-limits.md) |
| **评审**：判断设计是否成立、证据是否充分 | [`03 目标与取舍`](docs/03-goals-and-tradeoffs.md) → [`12`](docs/12-failure-semantics.md) → [`15`](docs/15-known-limits.md) → [`19 测试体系`](docs/19-testing-and-debug-kernel.md) → [`20 实测结果与缺口`](docs/20-results-and-gaps.md)；关心内核侧的正确性论证再读 [`07`](docs/07-overlay-concurrency-and-fixes.md)，关心修了什么读 [`14`](docs/14-issue-resolution.md) |
| **接手开发** | 按顺序读完第一到第三部分（[`01`](docs/01-problem-and-scope.md)–[`15`](docs/15-known-limits.md)），再读附录 [`B 继续开发`](docs/B-extending.md)；动性能相关的代码前读第四部分；查名字和文件格式用附录 [`A`](docs/A-glossary-and-code-map.md) |

只想知道"进程级和整机级该用哪个"，读 [`01`](docs/01-problem-and-scope.md) 和 [`18`](docs/18-process-vs-machine-level.md) 两章就够了。
完整目录见 [`docs/README.md`](docs/README.md)。

## 要在机器上验收

| 想做什么 | 看哪里 |
|---|---|
| 照着做一遍验收：前置条件、执行顺序、确切命令、每步的期望末行、结果怎么留档 | [`docs/26 上机验收`](docs/26-acceptance-runbook.md) |
| 每个脚本测什么、判据怎么设计的、有什么局限 | [`docs/19 测试体系与调试内核`](docs/19-testing-and-debug-kernel.md) |
| 跑出来的数字和谁比 | [`docs/20 实测结果与缺口`](docs/20-results-and-gaps.md)（判定性的实测数字只在这一章） |
| 每个脚本一行的速查、凭据怎么配 | [`scripts/README.md`](scripts/README.md) |
| 还没部署：部件清单、切栈、建模板、部署后自检 | [`docs/23 部署`](docs/23-deployment.md) |

验收的骨架是六步，前一步不过就停下来查：部署自检 → `verify_gsd.py` → `verify_fixes.py` → `verify_multitree.py` →
`verify_clear_refs_race.py` → `demo_checkpoint_perf.py` 连跑三次；探针与压测可选。

> **多数脚本的退出码不是判定。** `verify_gsd.py`、`verify_clear_refs_race.py`、`stress_overlay_ckpt.py`
> 即使判定不过也可能返回 0，要看输出里指定的那一行；哪一行，见 [`scripts/README.md`](scripts/README.md) 各表的"判据"栏。新版 `demo_checkpoint_perf.py` 失败返回非零，成功末行为 `判定: PASS`。

## 当前状态

- **代码基准**：运行部件及手册代码行号以 `deltabox-gsd@259f1a25c` 为准；当前 SDK 为后续只改鉴权头的 `dcca61266`。源码工作区及各部件路径见下方清单。
- **新 920B：已验证。** 该提交在新 920B 上整包编译，2026-10-08 做过确认性验证（功能验证、性能、发布版内核与调试内核上的压测），
  2026-10-09 按当前运行时布局（`/fc-gsd/`，默认注入，模板 `base-gsd`）复核。2026-10-16 用新版独立分档脚本完成三轮六档测试，18 次完整恢复校验通过；回滚中位数从 9.1 ms 随负载增至 93.9 ms。结果与缺口在 [`docs/20`](docs/20-results-and-gaps.md)，归档摘要见 [`validation/`](validation/README.md)。
- **950：未部署、未测。** 手册里没有 950 的 gsd 数字；20 章引用的 950 对照仅为沙箱级数据，部署与验收的 950 小节写的是"待补"。
- **只支持 arm64 guest。** x86_64 的 guest 上进程回滚不可用（[`docs/03`](docs/03-goals-and-tradeoffs.md) 列为非目标）。

## 主要限制

完整的限制表（带编号、原因、规避办法）在 [`docs/15 已知限制与未闭合项`](docs/15-known-limits.md)，使用方视角的说明在 [`docs/22`](docs/22-semantics-and-limits.md)。最要紧的几条：

1. **跨树的管道和 socket 不支持。** 两棵用户进程树之间共用管道或 socket 时 create 被拒绝（`failed_precondition`，什么都没动）。需要一起回滚的进程放进同一棵树。
2. **多棵树的代价是线性的，树间不是原子的。** 每棵树各 dump、各 restore 一次，串行执行；后 dump 的树比先 dump 的多跑一小段。需要严格一致的进程同样放进同一棵树。
3. **只回滚用户进程（uid 1000）和 overlay 上的文件。** root 进程与系统服务不保存也不杀；`/run`、`/dev/shm`、`/var/log` 等不在 overlay 上的目录不回滚。
   这些范围外的进程在 restore 之后可能遇到 `ESTALE`，重新 `chdir`、重新打开即可。
4. **开着大文件写的时候做 checkpoint 会变慢。** 每个以写方式打开着的文件要整份拷贝一次，约每 MiB 0.8 ms，期间沙箱里的文件操作停顿。
5. **容量与清理由调用方负责。** 没有自动清理：进程镜像占 guest 内存、层占根盘，同一条历史上的层栈深度上限约 190 层；撞上限时 create 返回 `resource_exhausted`，不会建出恢复不了的 checkpoint。
6. **仍有未闭合项。** 三个见过但没查清的现象（新沙箱首个请求偶发约 5 秒、一组压测里 create 返回过 2 次 `internal`、极低概率的单发 `ENOTEMPTY`）都不损坏数据、都没有稳定复现；
   多棵树的异常路径与注入开关的关闭态只有单测、没有现场走过，评审时应算作未验证。

## 与整机级手册的关系

[`../rollback/`](../rollback/) 是姊妹手册：那一套在 Firecracker 层把**整台虚机**（全部内存与磁盘）退回去；这一套只在 guest 里退回**用户进程 + 文件**，
内核、系统服务和 envd 不动。两者解决同一类需求、成本模型不同，逐项对比与"什么时候用哪个"在 [`docs/18`](docs/18-process-vs-machine-level.md)。

两本手册体例相同（教材顺序、每章"本章目标"与"本章要点"、判定性数字只在一章维护），共用同一套打包工具；
两套实现的代码在同一个代码仓的不同分支，同一台宿主上同一时刻只能运行其中一套（[`docs/23`](docs/23-deployment.md)）。

## 把文档发给别人

[`dist/`](dist/) 下的 `gsd-checkpoint-restore-docs.html` 是单文件离线版：双击用浏览器打开，不联网、不装任何东西，
手册内的链接都改写成了页内锚点，mermaid 图内联渲染；浏览器 Ctrl+P 可直接存 PDF。
改完 `docs/` 之后怎么重新生成，见 [`dist/README.md`](dist/README.md)。

## 不在这里的东西

- **源码**：在代码仓 KASandbox_0904 的 `deltabox-gsd` 分支。手册里的"`文件:行号`"都指那里。
- **完整日志与原始结果**：不入库；已复核的逐轮耗时摘要、版本和历史依据在 [`validation/`](validation/README.md)。其他判定性数字的出处（哪个结果目录、哪个文件）列在 [`docs/20`](docs/20-results-and-gaps.md) 的出处表里。
- **被测二进制与内核**：gsd、orchestrator、guest 内核都是构建产物，按 [`docs/23`](docs/23-deployment.md) 从代码基准重建；调试内核按 [`scripts/kernel/README.md`](scripts/kernel/README.md)。
- **凭据**：`scripts/.env` 不入库，格式见 [`scripts/README.md`](scripts/README.md)。
