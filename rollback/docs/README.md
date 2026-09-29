# Checkpoint / Restore 技术手册

> **v0.4.0**（2026-09-29，教材版：按教学顺序重排，恢复原生精确增量，新增长跑与并发实测）· 江路路（j30059180）
>
> 代码基准：KASandbox_0904 的 deltabox-dev@93ccb02（orchestrator、Firecracker、Python SDK 同仓）；交付的 RPM（e2b-infra）里的 0001 补丁由它融合而来；deltabox 交付分支待整理

**在不中断沙箱的前提下，把一台正在运行的 e2b 沙箱虚机退回到过去某一时刻** —— 这套 checkpoint / restore 的原理、实现、工程保障、证据与使用运维。

这是一本按教学顺序写的书：从"为什么要回退、虚机快照是什么"讲起，到每个机制怎么实现、为什么这样实现、
怎么保证不出错，再到怎么证明它对、它快，最后是怎么用、怎么部署运维。读者按顺序读能从零读懂；
只想完成某件具体事情的读者，按下面的捷径跳读。

---

## 怎么读

### 教材读者：按顺序读

[00](00-overview.md) → 第一部分 [01](01-background.md)–[03](03-goals-and-design-choices.md)
→ 第二部分 [04](04-architecture.md)–[11](11-end-to-end.md)
→ 第三部分 [12](12-failure-semantics.md)–[14](14-lifecycle-reasoning.md)
→ 第四部分 [15](15-native-increment-diagnosis.md)–[18](18-native-and-checkpoint-together.md)
→ 第五部分 [19](19-testing-and-functional-verification.md)–[22](22-long-run-and-concurrency.md)
→ 第六部分 [23](23-quickstart.md)–[30](30-acceptance-runbook.md)。

每一章开头的"本章目标"列出读完应能回答的问题，结尾的"本章要点"是复习用的摘要。
后面的章会用前面的概念；前面的章偶尔需要后面的结论时，会写明"详见第 N 章"。
第一部分不要求虚拟化背景，第二部分起默认你读过第一部分。

### 捷径

| 你是 | 建议路径 |
|---|---|
| **客户开发者**：要在代码里调用 checkpoint / restore | [00](00-overview.md) → [23](23-quickstart.md) → [24](24-semantics-and-limits.md) → [25](25-errors-timeouts-concurrency.md) → [21](21-benchmarks-and-compliance.md)（只看判定结论）；要和原生 pause / resume 配合时再读 [18](18-native-and-checkpoint-together.md) |
| **运维**：部署、配置、监控、排障、上机验收 | [00](00-overview.md) → [26](26-deployment-prerequisites.md) → [27](27-configuration-and-capacity.md) → [28](28-observability-reference.md) → [29](29-troubleshooting.md) → [30](30-acceptance-runbook.md)；错误含义查 [25](25-errors-timeouts-concurrency.md)；想知道长期运行会怎样读 [22](22-long-run-and-concurrency.md) |
| **评审**：判断设计是否成立、证据是否充分 | [00](00-overview.md) → [02](02-e2b-native-snapshot.md) → [03](03-goals-and-design-choices.md) → [18](18-native-and-checkpoint-together.md) → [19](19-testing-and-functional-verification.md) → [20](20-performance-methodology.md) → [21](21-benchmarks-and-compliance.md) → [22](22-long-run-and-concurrency.md)；关心正确性论证再读 [06](06-memory-diff-tree.md) 与 [12](12-failure-semantics.md)，关心边界读 [14](14-lifecycle-reasoning.md) |
| **接手开发** | 按教材顺序读完第一到第三部分，再读附录 [B](B-extending.md)；动原生 pause 路径前读第四部分 |

---

## 目录

### 导读

| # | 文档 | 一句话 |
|---|---|---|
| 00 | [导读](00-overview.md) | 这本书讲什么、六个部分怎么衔接、与原生快照的关系，以及一次 checkpoint 和一次 restore 各经过哪几章 |

### 第一部分　基础

不要求虚拟化背景。讲清问题、被快照的对象、快照的原理，以及作为基线的 e2b 原生 snapshot。

| # | 文档 | 一句话 |
|---|---|---|
| 01 | [沙箱、microVM 与快照原理](01-background.md) | 为什么要把运行中的机器退回过去；microVM 与 e2b 沙箱由什么拼成；快照要存什么、为什么必须同一瞬间、重建与原地回写的分野 |
| 02 | [e2b 原生 snapshot：机制与成本](02-e2b-native-snapshot.md) | 原生 `Pause` / `Checkpoint` 两个 RPC、UFFD 判据与内存搬运、`ExportDiff` 为什么必须停沙箱、恢复与成本模型 |
| 03 | [设计目标、约束与方案选择](03-goals-and-design-choices.md) | 目标场景与两个数字、五条硬约束、为什么不复用原生、明确的非目标，以及贯穿全书的四条原则 |

### 第二部分　核心设计与实现

| # | 文档 | 一句话 |
|---|---|---|
| 04 | [总体架构](04-architecture.md) | 四个层次与各自边界、请求为什么发往沙箱却由宿主应答、账本在哪、目录布局（全书唯一）、为什么交付用 ext4 |
| 05 | [脏页跟踪与 HDBSS](05-dirty-page-tracking-and-hdbss.md) | 怎么知道一页被写过；KVM 脏页日志的破坏性读；HDBSS 的启用条件与静默失效；"读也算脏"的判据差异（全书唯一定义） |
| 06 | [内存差分树](06-memory-diff-tree.md) | 每代只存本代脏页；历史为什么是树；回滚集的定义与证明；内容解析；删除、隐藏与合并及其不改变 restore 的证明 |
| 07 | [磁盘分层](07-disk-layering.md) | 写层原地封存成只读层、为什么不等落盘、读路径与视图装配、视图切换、层引用计数回收与层合并 |
| 08 | [Firecracker 接口契约](08-firecracker-api-contract.md) | 分叉 Firecracker 的三处扩展、FCDB 位图格式、版本配对与 seccomp |
| 09 | [进程内原地回滚](09-in-place-rollback.md) | 在活着的 Firecracker 进程里写回内存、vCPU、GIC 与设备的九个阶段和提交点 |
| 10 | [原地回滚特有的问题](10-rollback-pitfalls.md) | 网络描述符缓存、VMGenID 次序、连接跟踪、时间、队列页：重建路线不会遇到、原地路线必须处理的状态 |
| 11 | [端到端走查](11-end-to-end.md) | 把前几章串起来，逐步走一次 checkpoint 和一次 restore，看冻结窗口里有什么 |

### 第三部分　工程保障

| # | 文档 | 一句话 |
|---|---|---|
| 12 | [失败语义与不变量](12-failure-semantics.md) | 提交点把失败切成两半；纪元不能丢、断链拒绝恢复、账本污染；不变量清单与错误契约 |
| 13 | [状态、并发与持久性](13-state-concurrency-durability.md) | 三类状态、锁的层级、原子提交而不 fsync、锁外删除、节点级效应 |
| 14 | [生命周期边界](14-lifecycle-reasoning.md) | 为什么把目录完整拷走也恢复不了：四层依赖的推导，以及这是取舍不是缺陷 |

### 第四部分　原生快照的精确增量

对 e2b 原生 pause 路径的改进：让原生快照的内存增量也只含写过的页，并保证它与 checkpoint / restore 叠加使用时仍然正确；最后一章把两种快照放在一起，讲分工与配合。

| # | 文档 | 一句话 |
|---|---|---|
| 15 | [原生增量为什么不精确](15-native-increment-diagnosis.md) | 从"原生导出量不随改动量变化"的现象倒推：判、存、填三层，读也算脏、每代新进程、2 MiB 归并地板，以及本方案的做法为什么学不过去 |
| 16 | [精确增量的改法](16-native-increment-fix.md) | 4 KiB 存、2 MiB 拼：判据换成写跟踪位图、header 降到 4 KiB、缺页三级取源，以及跟踪没开时的退路 |
| 17 | [代价与验证](17-native-increment-cost-and-verification.md) | 恢复开销、平台差异、兼容性、四类验证，以及影响面只在原生路径 |
| 18 | [两种快照的分工与配合](18-native-and-checkpoint-together.md) | 与原生的逐项对比表和优化点表（全书唯一）、成本模型交叉点、两者怎么叠加使用 |

### 第五部分　验证与实测

判定性的实测数字只在 21、22（原生精确增量自己的代价数据在 17）；原理篇里为建立量级感举的数字都标明出处与平台。

| # | 文档 | 一句话 |
|---|---|---|
| 19 | [测试体系与功能验证](19-testing-and-functional-verification.md) | 三层测试、测试矩阵、正确性判据、crtest 用例与 `run.sh` 各档 |
| 20 | [性能口径与方法](20-performance-methodology.md) | 200 / 100 ms 怎么钉成可测口径、条件标签、负载构造的三种失真、各基准脚本回答什么 |
| 21 | [分档基准与判定](21-benchmarks-and-compliance.md) | 当前代码的功能结果、按脏页档位的基准、对照客户指标的判定、已知长尾与缺口 |
| 22 | [长跑与并发实测](22-long-run-and-concurrency.md) | 长时间、多沙箱并发下的逐项一致性校验，延迟与资源随时间的走势，以及长测中查出并修掉的问题 |

### 第六部分　使用与运维

| # | 文档 | 一句话 |
|---|---|---|
| 23 | [快速上手](23-quickstart.md) | 装 SDK 覆盖层与自检；checkpoint / restore / list / delete 示例；`mem_mode`；树形历史 |
| 24 | [语义与使用边界](24-semantics-and-limits.md) | 能回滚什么、回滚不了什么；checkpoint 何时失效；怎么删才释放空间；与原生 pause / resume 怎么配合 |
| 25 | [错误、超时与并发调用](25-errors-timeouts-concurrency.md) | 错误总表（全书唯一）、异常类层次、SDK 超时与不重放、多调用方与流式截断 |
| 26 | [部署前提与检查清单](26-deployment-prerequisites.md) | 脏页跟踪两层、平台前提、二进制版本配对、部署后自检 |
| 27 | [配置参考与容量规划](27-configuration-and-capacity.md) | 开关总表（全书唯一）、启动能力行、产物盘容量规划、改开关的步骤 |
| 28 | [可观测性参考](28-observability-reference.md) | 能力行与 `memMode`、timings 全字段与计时键（全书唯一）、metrics 与取证开关、日志关键字 |
| 29 | [排障](29-troubleshooting.md) | 按症状：看哪里、怎么处置、原理在哪一章 |
| 30 | [上机验收](30-acceptance-runbook.md) | 交付态验收 `run.sh` 的 smoke / func / perf / long 四档与结果回传 |

### 附录

| # | 文档 | 一句话 |
|---|---|---|
| A | [术语、代码地图与文件格式](A-glossary-and-code-map.md) | 术语表、代码索引、文件格式、接口与不变量速查 |
| B | [继续开发](B-extending.md) | 按任务的改动指引、不能破坏的不变量、一次改动怎么验证、已知缺口 |

---

## 相关材料

| 位置 | 是什么 |
|---|---|
| [`../../deploy-docs/`](../../deploy-docs/README.md) | 部署文档：整体架构、RPM 构建、`build.sh` 安装与启动、日常运维、存储位置、资源调优 |
| [`../../single-node-offline-deploy.md`](../../single-node-offline-deploy.md) | 单机离线部署步骤，含部署后 checkpoint 能力的核对 |
| [`../../e2b-deploy/dep/e2b-sdk-checkpoint/`](../../e2b-deploy/dep/e2b-sdk-checkpoint/使用说明.md) | Python SDK 覆盖层（`install.py` 与使用说明） |
| [`../scripts/950/`](../scripts/950/README.md) | 上机验收入口 `run.sh`（smoke / func / perf / long） |
| [`../scripts/crtest/`](../scripts/crtest/README.md) | 功能与性能测试套件（定向用例、分档基准） |
| [`../scripts/acceptance/`](../scripts/acceptance/README.md) | 交付态验收脚本：`checkpoint_verify.py`（正确性）、`checkpoint_bench.py`（性能）、`checkpoint_concurrent.py`（并发四段）、`rolling_keep10.py`（滚动保留长跑，复用同目录的 `checkpoint_concurrent.py`）等 |
| [`../scripts/dev/`](../scripts/dev/) | 开发态验证工具箱 |
