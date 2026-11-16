# 进程级快照回滚技术手册

> 约 19.1 万字

**在沙箱不停机的前提下，把沙箱里的用户进程和根文件系统退回过去某一时刻。** 做法是在 guest 内用 CRIU 保存和恢复进程，用 overlayfs 热切层回滚文件。
这本书讲它的原理、实现、工程保障、实测结果和使用运维，既是教材，也是接手这套代码的资料。

代码基准是 KASandbox_0904 的 `criu-rollback-dev@bc3cce913`，gsd 守护进程、orchestrator 注入、Python SDK 和 guest 内核补丁同仓。新 920B 上已验证，950 未运行，只支持 arm64 guest。

在 Firecracker 层回滚整个虚拟机的方案是另一本书，即[沙箱级手册](../../rollback/docs/README.md)；两者的对比只在 [24](24-vs-sandbox-level.md)，读本书不需要先读它。

---

## 怎么读

### 按顺序读

第一篇讲问题和原理，不要求内核或 CRIU 背景；第二篇逐个机制讲实现，默认读过第一篇；第三篇讲耗时由什么构成；第四篇讲怎么测、测出了什么、哪些没有测；第五篇与沙箱级对比；第六篇是使用与运维。

每一章开头的"本章目标"列出读完应能回答的问题，结尾的"本章要点"是复习用的摘要。后面的章会用前面的概念；前面的章需要后面的结论时，写明在哪一章。

### 捷径

| 你是 | 建议路径 |
|---|---|
| **开发者**：要在代码里调用 checkpoint / restore | [25](25-quickstart.md) → [26](26-semantics-and-limits.md) → [14](14-failure-semantics.md)；想知道大概多快再看 [19](19-920b-functional.md) 的"结论先行"和 [20](20-920b-performance.md) |
| **运维**：部署、建模板、配置、排障、上机验收 | [27](27-deployment.md) → [28](28-configuration-and-observability.md) → [29](29-troubleshooting.md) → [30](30-acceptance-runbook.md)；错误含义查 [14](14-failure-semantics.md)，已知限制查 [16](16-known-limits.md) |
| **评审**：判断设计是否成立、证据是否充分 | [04](04-goals-and-tradeoffs.md) → [14](14-failure-semantics.md) → [16](16-known-limits.md) → [18](18-testing-and-debug-kernel.md) → [19](19-920b-functional.md) → [23](23-gaps-and-boundaries.md)；关心内核侧的正确性论证再读 [08](08-overlay-switch-consistency.md)、[09](09-overlay-cache-and-reclaim.md)，关心修了什么读[附录 C](C-issue-resolution.md) |
| **接手开发** | 按顺序读完第一到第三篇，再读[附录 B](B-extending.md)；动性能相关的代码前读[附录 D](D-restore-slowness-case.md)、[附录 E](E-regression-case.md) |
| **选型**：进程级和沙箱级该用哪个 | [02](02-problem-and-scope.md) → [24](24-vs-sandbox-level.md) |

---

## 目录

### 第一篇　导论与基础

| 章 | 一句话 |
|---|---|
| [01 全书概览](01-overview.md) | 这本书讲什么、一次 checkpoint 和一次 restore 各经过哪几章、当前状态 |
| [02 问题与定位](02-problem-and-scope.md) | 要退回的是什么、不退回的是什么，gsd 是什么 |
| [03 CRIU 与增量内存](03-criu-and-incremental-memory.md) | CRIU 的 dump 与 restore、按代增量与父镜像、soft-dirty、透明大页为什么必须关 |
| [04 目标、约束与取舍](04-goals-and-tradeoffs.md) | 三个目标、硬约束、四项取舍与非目标 |

### 第二篇　设计与实现

| 章 | 一句话 |
|---|---|
| [05 总体架构](05-architecture.md) | 组件分工、请求路径、鉴权、线协议、目录与层布局 |
| [06 guest 启动与注入](06-guest-boot-and-injection.md) | `overlay-init` 怎么搭出可换层的根，orchestrator 怎么注入和自检，模板固化了什么 |
| [07 热切层机制](07-overlay-hot-switch.md) | 不卸载根就换掉它下面的层：ioctl 接口、两种模式、一次切换的三段 |
| [08 热切层：切换一致性](08-overlay-switch-consistency.md) | 失败处理、切换与 overlay 入口的互斥、打开的文件跨切换的语义 |
| [09 热切层：缓存、竞争与回收](09-overlay-cache-and-reclaim.md) | 目录与 inode 缓存、rename 与删除竞争、层的回收、work 目录缓存、范围外进程看到什么 |
| [10 用户进程树与多树](10-process-trees.md) | 一次 `/proc` 扫描、什么算用户进程和根、多棵树之间的预检、怎么停与怎么杀 |
| [11 checkpoint 的实现](11-checkpoint.md) | `CreateCheckpoint` 逐步：逐棵 dump、下沉 upper、重置脏页标记、按树增量、失败路径 |
| [12 restore 的实现](12-restore.md) | 两段式与提交点：杀进程之前把能失败的做完，之后切层并逐棵恢复 |
| [13 端到端走查](13-end-to-end.md) | 一次 checkpoint 和一次 restore 的时间线，每个计时键落在哪一步 |
| [14 失败语义与 degraded](14-failure-semantics.md) | 提交点把失败切成两半，错误表、三类 degraded、异常映射、不变量清单 |
| [15 状态、并发与容量](15-state-concurrency-capacity.md) | `state.json`、增量依赖与删除、回收、操作锁、容量与层栈深度预检 |
| [16 已知限制与未闭合项](16-known-limits.md) | 设计如此的限制、见过但没查清的现象、没走到的分支，带编号 |

### 第三篇　性能原理

| 章 | 一句话 |
|---|---|
| [17 代价模型](17-cost-model.md) | 单树和多树的耗时由什么构成，可写打开的文件和 THP 的代价 |

### 第四篇　验证与实测

| 章 | 一句话 |
|---|---|
| [18 测试体系与调试内核](18-testing-and-debug-kernel.md) | 四层测试各回答什么、每个脚本的判据与局限、压测与探针、调试内核 |
| [19 920B 功能结果](19-920b-functional.md) | 结论、条件标签、功能验证结果 |
| [20 920B 性能结果](20-920b-performance.md) | 各口径的耗时、六档分档、新部署上的复测 |
| [21 920B 压测结果](21-920b-stress.md) | 四组压测、发布版与调试内核的补充证据 |
| [22 950 结果](22-950-results.md) | 950 上的验收与性能，待填 |
| [23 缺口与结论边界](23-gaps-and-boundaries.md) | 已测、跳过、待验证分别是什么，数据出处 |

### 第五篇　与沙箱级对比

| 章 | 一句话 |
|---|---|
| [24 设计差异与性能对比](24-vs-sandbox-level.md) | 两种方案的逐项对比、成本模型的差别、什么时候用哪个 |

### 第六篇　使用与运维

| 章 | 一句话 |
|---|---|
| [25 快速上手](25-quickstart.md) | 用仓库里的 SDK 跑通 checkpoint、restore、list、delete，以及第一次用会遇到的三件事 |
| [26 语义与使用边界](26-semantics-and-limits.md) | 能回滚什么、进程树怎么组织、打开的文件、历史是一棵树、容量与生命周期 |
| [27 部署](27-deployment.md) | 部件与构建、运行时布局、切到进程级栈、建模板、部署后自检、950 的部署流程 |
| [28 配置与可观测性](28-configuration-and-observability.md) | 开关总表与计时键定义、日志、`/health`、`state.json` |
| [29 排障](29-troubleshooting.md) | 按症状：看哪里、怎么处置、原理在哪一章 |
| [30 上机验收](30-acceptance-runbook.md) | 前置条件、执行顺序与每步判据、怎么对数、结果怎么留档 |

### 附录

| 章 | 一句话 |
|---|---|
| [附录 A 术语与代码地图](A-glossary-and-code-map.md) | 术语表、代码索引、文件与目录格式、结果文件里的名字对照 |
| [附录 B 继续开发](B-extending.md) | 从哪读代码、不能破坏的不变量、改动后怎么验证、未闭合项的处理顺序 |
| [附录 C 问题处理与修复历程](C-issue-resolution.md) | 问题清单按主题归并的处理结果、方向性决定、问题处理总表、修复历程对照 |
| [附录 D restore 慢的归因案例](D-restore-slowness-case.md) | 初始实现的 restore 慢在哪：方法、五项原因、被推翻的假设、修复前后 |
| [附录 E 修复后回退的归因案例](E-regression-case.md) | 修复引入的回退怎么收回、一次失效的对照、代码布局带来的那几毫秒 |
| [附录 F 代码规模](F-code-size.md) | 生产、测试、生成代码各多少，按组件分；“有效代码行”的口径 |

---

## 体例

**代码引用。** 写成"`函数名`（`文件:行号`）"，代码基准是 `criu-rollback-dev@bc3cce913`。gsd 的核心包路径统一简写为 `ckpt/`，即 `packages/gsd/internal/services/checkpoint/`。内核补丁只引用"补丁文件名 + 函数名"，不引行号：`0002-overlayfs-hot-switch-checkpoint.patch` 是初始实现的补丁，`0003-overlayfs-hot-switch-fixes.patch` 是其上的全部修复。完整的代码地图在[附录 A](A-glossary-and-code-map.md)。

**交叉引用。** 链接文字是章号，写成 `[20](20-920b-performance.md)`，附录写 `[附录 C](C-issue-resolution.md)`；需要指到节时写出节名，章内用 `§x.y`。

**数字纪律。** 判定性的实测数字只在第四篇。完整功能与压测以 2026-10-08 的确认验证为准，2026-10-16 的六档分档和 2026-11-06 的全流程复测单列。每个数字在 [23](23-gaps-and-boundaries.md) 的出处表里能找到来源文件。其他章举数时当场标明平台、日期、代码版本和口径，这些数只为说明原理或过程，不能与第四篇的数相减。不同平台、不同口径的同名指标不互相比较。容量单位用 MiB / GiB，时间用 ms。

**平台标签。** "新 920B"指 192.168.45.47 这台机器；"旧 920B"指已退役的那台，只在案例附录作为历史数据出现。950：未运行，数据待填，见 [22](22-950-results.md)、[24](24-vs-sandbox-level.md)。

**路径与命名。** 命令和路径按运行时布局写：宿主 `/fc-gsd/gsd`、`/fc-gsd/orchestrator`、`/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin`，模板名 `base-gsd`。结果文件里的开发期产物名只在 [19](19-920b-functional.md) 的条件标签、[23](23-gaps-and-boundaries.md) 的出处表和[附录 A](A-glossary-and-code-map.md) 的对照表里出现。

**措辞。** "初始实现"指修复之前的代码；"修改前 / 修复后"指同一份代码的两个阶段；"进程级"指本书的方案，"沙箱级"指沙箱级手册的方案；"回滚"即 restore；拍快照的操作写 checkpoint，只有引用接口名时才写 `CreateCheckpoint`、`create()`。过程性的叙述（先怀疑什么、后来推翻什么）只在[附录 D](D-restore-slowness-case.md)、[附录 E](E-regression-case.md) 和 [08](08-overlay-switch-consistency.md)、[09](09-overlay-cache-and-reclaim.md) 各主题的"根因"小节里，其余章只写现状。

**全书唯一的表。** 目录与层布局（[05](05-architecture.md)）、错误表（[14](14-failure-semantics.md)）、已知限制表（[16](16-known-limits.md)）、缺口表（[23](23-gaps-and-boundaries.md)）、两种方案的对比表（[24](24-vs-sandbox-level.md)）、开关总表与计时键（[28](28-configuration-and-observability.md)）、问题处理总表（[附录 C](C-issue-resolution.md)）。别的章需要时链接过去，不复述。

---

## 相关材料

| 位置 | 是什么 |
|---|---|
| [`../scripts/`](../scripts/README.md) | 随手册附带的脚本：验收（`acceptance/`）、压测（`stress/`）、探针（`probes/`）、切栈（`ops/`）、调试内核的 config 与说明（`kernel/`） |
| [`../validation/`](../validation/README.md) | 已复核的结果摘要、版本和数据依据 |
