# 进程级快照回滚（criu-rollback）：部署入口、手册与验证工具

在沙箱不停机的前提下，把沙箱里的**用户进程和根文件系统**退回过去某一时刻：在 guest 内由守护进程 gsd 用 CRIU 保存与恢复进程，
用 overlayfs 热切层回滚文件。本目录是这套功能的**部署入口、技术手册与验证材料**；
实现代码在 KASandbox_0904 的 `criu-rollback-dev` 分支（gsd 守护进程、orchestrator 注入、Python SDK、guest 内核补丁同仓），不在这里重复存放。

> 代码基准 `criu-rollback-dev@bc3cce913`

| 路径 | 内容 | 给谁 |
|---|---|---|
| [`deploy/`](deploy/README.md) | **部署与切换入口**：在已部署沙箱级的机器上 `bash criu-rollback/deploy/950.sh all` 一条命令切到进程级、验收、切回；日常切换用 `gsd` / `sandbox`。三个运行文件、镜像的 CRIU 层、SDK wheel 压缩入库 | 运维、上机验收的人 |
| [`docs/`](docs/README.md) | **《进程级快照回滚技术手册》**，31 章加 6 个附录，分六篇：导论与基础 → 设计与实现 → 性能原理 → 验证与实测 → 与沙箱级对比 → 使用与运维 | 首要入口 |
| [`scripts/`](scripts/README.md) | **全部测试脚本**：`acceptance/` 功能验证与性能、`stress/` 压测与看门狗、`probes/` 探针、`ops/` 切栈脚本、`kernel/` 调试内核的 config 与构建说明 | 验收方（`acceptance/`）；接手开发的人（其余） |
| [`validation/`](validation/README.md) | 已复核的结果摘要、环境版本与历史修复依据。完整日志与原始结果不入库 | 要核对数字的人 |
| [`diagrams/`](diagrams/README.md) | 只有一页说明：本手册的图在 `docs/img/`，画图脚本在 `rollback/tools/figures/` | 想找图或改图的人 |
| [`dist/`](dist/README.md) | 文档打包件（单文件 HTML）与重新打包的命令。构建产物，只有 `docs/` 是源 | 要把手册发给别人的人 |

`contents.json` 是目录，`book.json` 是打包用的书目信息（书名和仓库链接）。手册不设版本号和日期，也不署名。

## 源码与脚本

| 部件 | 源码路径 | 版本 |
|---|---|---|
| gsd、CRIU 基础镜像构建定义 | `packages/gsd/` | `bc3cce913`，与 `259f1a25c` 相同 |
| orchestrator 注入、启动脚本与 service | `packages/orchestrator/`，其中 `internal/template/build/core/rootfs/gsdfiles/` 为 guest 文件 | `bc3cce913` |
| guest 内核配置、构建脚本、热切层补丁 | `fc-kernels/` | `bc3cce913`，与 `259f1a25c` 相同 |
| Python SDK | `py-sdk/` | `bc3cce913`，与 `dcca61266` 相同 |
| 功能、性能、压测和探针 | 本仓库 `criu-rollback/scripts/` | 正式性能入口为 `acceptance/demo_checkpoint_perf.py` |

`test_demo_checkpoint_perf.py` 是不连接沙箱的逻辑回归测试；`summarize_checkpoint_perf.py` 只校验已有三轮结果并生成摘要，两者均不采集性能样本。

## 从哪读起

| 你是 | 建议路径 |
|---|---|
| 想先看个全貌 | [`01 全书概览`](docs/01-overview.md) |
| 当教材从头学 | 按[目录](docs/README.md)的顺序读：第一篇 [`01`](docs/01-overview.md)–[`04`](docs/04-goals-and-tradeoffs.md) → 第二篇 [`05`](docs/05-architecture.md)–[`16`](docs/16-known-limits.md) → 第三篇 [`17`](docs/17-cost-model.md) → 第四篇 [`18`](docs/18-testing-and-debug-kernel.md)–[`23`](docs/23-gaps-and-boundaries.md) → 第五篇 [`24`](docs/24-vs-sandbox-level.md) → 第六篇 [`26`](docs/26-quickstart.md)–[`31`](docs/31-acceptance-runbook.md) |
| **开发者**：要在代码里调用 checkpoint / restore | [`26 快速上手`](docs/26-quickstart.md) → [`27 语义与使用边界`](docs/27-semantics-and-limits.md) → [`14 失败语义`](docs/14-failure-semantics.md)；想知道大概多快再看 [`19`](docs/19-920b-functional.md) 的"结论先行"和 [`20`](docs/20-920b-performance.md) |
| **运维**：部署、建模板、配置、排障、上机验收 | [`28 部署`](docs/28-deployment.md) → [`29 配置与可观测性`](docs/29-configuration-and-observability.md) → [`30 排障`](docs/30-troubleshooting.md) → [`31 上机验收`](docs/31-acceptance-runbook.md)；错误含义查 [`14`](docs/14-failure-semantics.md)，已知限制查 [`16`](docs/16-known-limits.md) |
| **评审**：判断设计是否成立、证据是否充分 | [`04 目标与取舍`](docs/04-goals-and-tradeoffs.md) → [`14`](docs/14-failure-semantics.md) → [`16`](docs/16-known-limits.md) → [`18 测试体系`](docs/18-testing-and-debug-kernel.md) → [`19`](docs/19-920b-functional.md) → [`23 缺口与结论边界`](docs/23-gaps-and-boundaries.md)；关心内核侧的正确性论证再读 [`08`](docs/08-overlay-switch-consistency.md)、[`09`](docs/09-overlay-cache-and-reclaim.md)，关心修了什么读[`附录 C`](docs/C-issue-resolution.md) |
| **接手开发** | 按顺序读完第一到第三篇（[`01`](docs/01-overview.md)–[`17`](docs/17-cost-model.md)），再读[`附录 B 继续开发`](docs/B-extending.md)；动性能相关的代码前读[`附录 D`](docs/D-restore-slowness-case.md)、[`附录 E`](docs/E-regression-case.md)；查名字和文件格式用[`附录 A`](docs/A-glossary-and-code-map.md) |

只想知道进程级和沙箱级该用哪个，读 [`02`](docs/02-problem-and-scope.md) 和 [`24`](docs/24-vs-sandbox-level.md) 两章就够了。

## 要在机器上验收

| 想做什么 | 看哪里 |
|---|---|
| 照着做一遍验收：前置条件、执行顺序、确切命令、每步的期望末行、结果怎么留档 | [`31 上机验收`](docs/31-acceptance-runbook.md) |
| 每个脚本测什么、判据怎么设计的、有什么局限 | [`18 测试体系与调试内核`](docs/18-testing-and-debug-kernel.md) |
| 跑出来的数字和谁比 | [`19 功能结果`](docs/19-920b-functional.md)、[`20 性能结果`](docs/20-920b-performance.md)、[`21 压测结果`](docs/21-920b-stress.md) |
| 每个脚本一行的速查、凭据怎么配 | [`scripts/README.md`](scripts/README.md) |
| 还没部署：部件清单、切栈、建模板、部署后自检 | [`28 部署`](docs/28-deployment.md) |

验收的骨架是六步，前一步不过就停下来查：部署自检 → `verify_gsd.py` → `verify_fixes.py` → `verify_multitree.py` →
`verify_clear_refs_race.py` → `demo_checkpoint_perf.py` 连跑三次；探针与压测可选。

> **多数脚本的退出码不是判定。** `verify_gsd.py`、`verify_clear_refs_race.py`、`stress_overlay_ckpt.py`
> 即使判定不过也可能返回 0，要看输出里指定的那一行；哪一行，见 [`scripts/README.md`](scripts/README.md) 各表的"判据"栏。`demo_checkpoint_perf.py` 失败返回非零，成功末行为 `判定: PASS`。

## 当前状态

- **代码基准**：手册代码行号以 `criu-rollback-dev@bc3cce913` 为准。它相对 2026-10-08 确认验证所用的 `259f1a25c` 多两个提交：`dcca61266`（SDK 发送流量令牌）和 `bc3cce913`（`slot.go` 的 `VethName()`），gsd 与内核补丁没有变化。
- **新 920B：已验证。** 2026-10-08 做过确认性验证（功能验证、性能、发布版内核与调试内核上的压测），2026-10-09 按当前运行时布局（`/fc-gsd/`，默认注入，模板 `base-gsd`）复核。2026-10-16 用独立分档脚本完成三轮六档测试，18 次完整恢复校验通过；回滚中位数从 9.1 ms 随负载增至 93.9 ms。2026-11-06 在新部署上跑完验收、压测和回切，全部通过，跳过 V12。结果见 [`19`](docs/19-920b-functional.md)、[`20`](docs/20-920b-performance.md)、[`21`](docs/21-920b-stress.md)，缺口见 [`23`](docs/23-gaps-and-boundaries.md)，归档摘要见 [`validation/`](validation/README.md)。
- **950：未运行。** 手册里没有 950 的进程级数字，结果填进 [`22`](docs/22-950-results.md)，部署流程见 [`28`](docs/28-deployment.md) §7。
- **只支持 arm64 guest。** x86_64 的 guest 上进程回滚不可用（[`04`](docs/04-goals-and-tradeoffs.md) 列为非目标）。

## 主要限制

完整的限制表（带编号、原因、规避办法）在 [`16 已知限制与未闭合项`](docs/16-known-limits.md)，使用方视角的说明在 [`27`](docs/27-semantics-and-limits.md)。最要紧的几条：

1. **跨树的管道和 socket 不支持。** 两棵用户进程树之间共用管道或 socket 时 checkpoint 被拒绝（`failed_precondition`，什么都没动）。需要一起回滚的进程放进同一棵树。
2. **多棵树的代价是线性的，树间不是原子的。** 每棵树各 dump、各 restore 一次，串行执行；后 dump 的树比先 dump 的多跑一小段。需要严格一致的进程同样放进同一棵树。
3. **只回滚用户进程（uid 1000）和 overlay 上的文件。** root 进程与系统服务不保存也不杀；`/run`、`/dev/shm`、`/var/log` 等不在 overlay 上的目录不回滚。
   这些范围外的进程在 restore 之后可能遇到 `ESTALE`，重新 `chdir`、重新打开即可。
4. **开着大文件写的时候做 checkpoint 会变慢。** 每个以写方式打开着的文件要整份拷贝一次，约每 MiB 0.8 ms，期间沙箱里的文件操作停顿。
5. **容量与清理由调用方负责。** 没有自动清理：进程镜像占 guest 内存、层占根盘，同一条历史上的层栈深度上限约 190 层；撞上限时 checkpoint 返回 `resource_exhausted`，不会建出恢复不了的 checkpoint。
6. **仍有未闭合项。** 三个见过但没查清的现象（新沙箱首个请求偶发约 5 秒、一组压测里 checkpoint 返回过 2 次 `internal`、极低概率的单发 `ENOTEMPTY`）都不损坏数据、都没有稳定复现；
   多棵树的异常路径与注入开关的关闭态只有单测、没有实机走过，评审时应算作未验证。

## 与沙箱级手册的关系

[`../rollback/`](../rollback/) 是另一本手册，讲在 Firecracker 层回滚整个虚拟机。本书只讲 guest 内回滚用户进程和文件，内核、系统服务和 envd 不动；两者的对比只在 [`24`](docs/24-vs-sandbox-level.md)。

两套实现同一时刻在一台宿主上只能运行一套（[`28`](docs/28-deployment.md)）；两本手册共用同一套打包工具。

## 把文档发给别人

[`dist/`](dist/) 下的 `criu-rollback-docs.html` 是单文件离线版：双击用浏览器打开，不联网、不装任何东西，
手册内的链接都改写成了页内跳转，配图以 SVG 内嵌；浏览器 Ctrl+P 可直接存 PDF。
改完 `docs/` 之后怎么重新生成，见 [`dist/README.md`](dist/README.md)。

## 不在这里的东西

- **源码**：在代码仓 KASandbox_0904 的 `criu-rollback-dev` 分支。手册里的"`文件:行号`"都指那里。
- **完整日志与原始结果**：不入库；已复核的逐轮耗时摘要、版本和历史依据在 [`validation/`](validation/README.md)。其他判定性数字的出处列在 [`23`](docs/23-gaps-and-boundaries.md) 的出处表里。
- **被测二进制与内核**：gsd、orchestrator、guest 内核都是构建产物，按 [`28`](docs/28-deployment.md) 从代码基准重建；调试内核按 [`scripts/kernel/README.md`](scripts/kernel/README.md)。
- **凭据**：`scripts/.env` 不入库，格式见 [`scripts/README.md`](scripts/README.md)。
