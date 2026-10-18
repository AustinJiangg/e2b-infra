# gsd 进程级 Checkpoint / Restore 技术手册

> **v0.1.1**（2026-10-17）
>
> 代码基准：KASandbox_0904 的 `deltabox-gsd@259f1a25c`（gsd 守护进程、orchestrator 注入、Python SDK、guest 内核补丁同仓）。
> 运行部件已在新 920B（192.168.45.47）验证；2026-10-16 新版分档测试使用 SDK `dcca61266`，结果单列于 20 章。950 的 gsd 待部署验证；只支持 arm64 guest。

**在沙箱不停机的前提下，把沙箱里的用户进程和根文件系统退回过去某一时刻** —— 在 guest 内用 CRIU 加 overlayfs 热切层完成。
这本书讲它的原理、实现、工程保障、证据与使用运维。

这是一本按教学顺序写的书：从"要回滚的是什么、CRIU 的增量内存是怎么回事"讲起，到每个机制怎么实现、为什么这样实现、
出错时怎么不坏事，再到怎么证明它对、它有多快，最后是怎么用、怎么部署运维。读者按顺序读能从零读懂；
只想完成某件具体事情的读者，按下面的捷径跳读。

它与整机级回滚的手册（[`../../rollback/docs/`](../../rollback/docs/README.md)）是姊妹篇：那一本在 Firecracker 层把整台虚机退回去，
这一本只在 guest 里退回"用户进程 + 文件"。两者的分工与对照在 [18](18-process-vs-machine-level.md)。

---

## 怎么读

### 教材读者：按顺序读

[00](00-overview.md) → 第一部分 [01](01-problem-and-scope.md)–[03](03-goals-and-tradeoffs.md)
→ 第二部分 [04](04-architecture.md)–[11](11-end-to-end.md)
→ 第三部分 [12](12-failure-semantics.md)–[15](15-known-limits.md)
→ 第四部分 [16](16-restore-slowness-case.md)–[18](18-process-vs-machine-level.md)
→ 第五部分 [19](19-testing-and-debug-kernel.md)–[20](20-results-and-gaps.md)
→ 第六部分 [21](21-quickstart.md)–[26](26-acceptance-runbook.md)。

每一章开头的"本章目标"列出读完应能回答的问题，结尾的"本章要点"是复习用的摘要。
后面的章会用前面的概念；前面的章偶尔需要后面的结论时，会写明在哪一章。
第一部分不要求内核或 CRIU 背景，第二部分起默认你读过第一部分。

### 捷径

| 你是 | 建议路径 |
|---|---|
| **客户开发者**：要在代码里调用 checkpoint / restore | [21](21-quickstart.md) → [22](22-semantics-and-limits.md) → [12](12-failure-semantics.md)；想知道大概多快再看 [20](20-results-and-gaps.md) 的"结论先行" |
| **运维**：部署、建模板、配置、排障、上机验收 | [23](23-deployment.md) → [24](24-configuration-and-observability.md) → [25](25-troubleshooting.md) → [26](26-acceptance-runbook.md)；错误含义查 [12](12-failure-semantics.md)，已知限制查 [15](15-known-limits.md) |
| **评审**：判断设计是否成立、证据是否充分 | [03](03-goals-and-tradeoffs.md) → [12](12-failure-semantics.md) → [15](15-known-limits.md) → [19](19-testing-and-debug-kernel.md) → [20](20-results-and-gaps.md)；关心内核侧的正确性论证再读 [07](07-overlay-concurrency-and-fixes.md)，关心修了什么读 [14](14-issue-resolution.md) |
| **接手开发** | 按教材顺序读完第一到第三部分（[01](01-problem-and-scope.md)–[15](15-known-limits.md)），再读附录 [B](B-extending.md)；动性能相关的代码前读第四部分 |

只想知道"进程级和整机级该用哪个"的读者，读 [01](01-problem-and-scope.md) 和 [18](18-process-vs-machine-level.md) 两章就够了。

---

## 目录

### 导读

| # | 文档 | 一句话 |
|---|---|---|
| 00 | [导读](00-overview.md) | 这本书讲什么、六个部分怎么衔接、与整机级手册的分工、一次 checkpoint 和一次 restore 各经过哪几章、当前状态 |

### 第一部分　基础

讲清问题、被回滚的对象，以及进程那一半所依赖的 CRIU 与增量内存原理。

| # | 文档 | 一句话 |
|---|---|---|
| 01 | [进程级回滚：问题与定位](01-problem-and-scope.md) | 要退回的是什么、不退回的是什么；整机快照与进程快照两条路线；gsd 是什么 |
| 02 | [CRIU 与增量内存原理](02-criu-and-incremental-memory.md) | CRIU 的 dump 与 restore、按代增量与父镜像、soft-dirty、透明大页为什么必须关、"假全量"是怎么来的 |
| 03 | [设计目标、约束与取舍](03-goals-and-tradeoffs.md) | 三个目标、硬约束（只支持 arm64 guest 等）、四项取舍与非目标 |

### 第二部分　设计与实现

| # | 文档 | 一句话 |
|---|---|---|
| 04 | [总体架构、请求路径与鉴权](04-architecture.md) | 组件分工、请求怎么从 SDK 到 guest 里的 gsd、鉴权规则、线协议、状态在哪、目录与层布局（全书唯一） |
| 05 | [guest 启动、根文件系统与模板注入](05-guest-boot-and-injection.md) | `overlay-init` 怎么搭出可换层的根、`gsd.service`、orchestrator 的注入开关与自检、模板固化了什么 |
| 06 | [overlayfs 热切层：机制](06-overlay-hot-switch.md) | 不卸载根就换掉它下面的层：ioctl 接口、两种模式、一次切换的三段、gsd 一侧怎么调用 |
| 07 | [overlayfs 热切层：并发、生命周期与修复](07-overlay-concurrency-and-fixes.md) | 内核侧修复按主题逐个讲：现象、根因、改法、验证；修复之后范围外的进程会看到什么 |
| 08 | [用户进程树与多树](08-process-trees.md) | 一次 `/proc` 扫描、什么算用户进程和根、多棵树之间的预检、怎么停与怎么杀 |
| 09 | [checkpoint 的实现](09-checkpoint.md) | `CreateCheckpoint` 逐步：逐棵 dump、下沉 upper、重置脏页标记、按树增量、失败路径 |
| 10 | [restore 的实现](10-restore.md) | 两段式与提交点：杀进程之前把能失败的做完，之后切层并逐棵恢复；失败之后沙箱里有什么 |
| 11 | [端到端走查](11-end-to-end.md) | 把一次 checkpoint 和一次 restore 放在一条时间线上，看每个计时键落在哪一步、停顿有哪几种 |

### 第三部分　工程保障

| # | 文档 | 一句话 |
|---|---|---|
| 12 | [失败语义与 degraded](12-failure-semantics.md) | 提交点把失败切成两半；错误表（全书唯一）、三类 degraded、SDK 的异常映射、不变量清单 |
| 13 | [状态、并发与容量](13-state-concurrency-capacity.md) | `state.json`、增量依赖与删除、回收、一把操作锁、容量预检与层栈深度预检 |
| 14 | [问题清单的处理结果](14-issue-resolution.md) | 交接时审出的问题按主题归并后的处理结果、方向性决定及理由、问题处理总表（全书唯一） |
| 15 | [已知限制与未闭合项](15-known-limits.md) | 设计如此的限制、见过但没查清的现象、只有单测的分支、内核里记录在案的；全书唯一的限制表，带编号 |

### 第四部分　性能

两个案例章按过程写（先怀疑什么、后来推翻什么），第三章把两套回滚放在一起比较。

| # | 文档 | 一句话 |
|---|---|---|
| 16 | [restore 为什么慢：一次归因](16-restore-slowness-case.md) | 交接时 restore 慢在哪：方法、五项原因、被推翻的假设、修复前后 |
| 17 | [修复之后的性能：回退、收回与代价模型](17-performance-after-fixes.md) | 修复引入的回退怎么收回、一次失效的对照、代码布局带来的那几毫秒为什么不追、现状的代价模型 |
| 18 | [进程级与整机级：分工与对照](18-process-vs-machine-level.md) | 两套回滚的逐项对比表（全书唯一）、成本模型的差别、什么时候用哪个 |

### 第五部分　验证与实测

判定性的实测数字只在 20；别的章为建立量级感举的数字都当场标明平台、日期、代码版本与口径。

| # | 文档 | 一句话 |
|---|---|---|
| 19 | [测试体系与调试内核](19-testing-and-debug-kernel.md) | 四层测试各回答什么、每个脚本的判据与局限、压测与探针、调试内核怎么用 |
| 20 | [实测结果与缺口](20-results-and-gaps.md) | 条件标签、功能与性能结果、压测、专项数字、修复历程对照、缺口表与数据出处 |

### 第六部分　使用与运维

| # | 文档 | 一句话 |
|---|---|---|
| 21 | [快速上手](21-quickstart.md) | 用仓库里的 SDK 跑通 create / restore / list / delete，连接配置，第一次用会遇到的三件事 |
| 22 | [语义与使用边界](22-semantics-and-limits.md) | 能回滚什么、不能回滚什么；进程树怎么组织；打开的文件；历史是一棵树；容量与生命周期 |
| 23 | [部署](23-deployment.md) | 部件清单与构建、运行时布局、切到 gsd 栈、建模板、部署后自检 |
| 24 | [配置与可观测性](24-configuration-and-observability.md) | 开关总表与计时键定义（均为全书唯一）、日志、`/health`、`state.json` 怎么读 |
| 25 | [排障](25-troubleshooting.md) | 按症状：看哪里、怎么处置、原理在哪一章 |
| 26 | [上机验收](26-acceptance-runbook.md) | 前置条件、执行顺序与每步判据、怎么与 20 章对数、结果怎么留档 |

### 附录

| # | 文档 | 一句话 |
|---|---|---|
| A | [术语、代码地图与文件格式](A-glossary-and-code-map.md) | 术语表、代码索引、文件与目录格式、开发期名字与提交清单的对照 |
| B | [继续开发](B-extending.md) | 从哪读代码、不能破坏的不变量、改动后怎么验证、未闭合项的处理顺序 |

---

## 体例

**代码引用。** 写成"`函数名`（`文件:行号`）"，行号以 `deltabox-gsd@259f1a25c` 为准。gsd 的核心包路径统一简写为 `ckpt/`，
即 `packages/gsd/internal/services/checkpoint/`。内核补丁只引用"补丁文件名 + 函数名"，不引行号：
`0002-overlayfs-hot-switch-checkpoint.patch` 是交接时的原始补丁，`0003-overlayfs-hot-switch-fixes.patch` 是其上的全部修复。完整的代码地图在附录 [A](A-glossary-and-code-map.md)。

**交叉引用。** 只指到章，写成 `[07](07-overlay-concurrency-and-fixes.md)`；需要指到节时写出节名（"07 章'失败处理与发布'一节"），章内用 `§x.y`。

**数字纪律。** 判定性的实测数字只在 [20](20-results-and-gaps.md)，完整功能与压测保留 2026-10-08 的确认验证；2026-10-16 新版性能结果单列。每个数字在该章的出处表里能找到来源文件。
其他章举数时当场标明平台、日期、代码版本和口径，并注明"判定值见 20 章"；这些数只为说明原理或过程，不能与 20 章的数相减。
不同口径（不同脚本、不同负载、不同轮次的模板与内核）下的同名指标不互相比较。容量单位用 MiB / GiB，时间用 ms。

**平台标签。** 书中的"现场"一律指新 920B（192.168.45.47）；出现"旧 920B"时指已退役的那台，只在案例章作为历史数据出现。
950：未部署、未测，书里没有 950 的 gsd 实测数字；20 章单列的 950 数字来自沙箱级手册，部署与验收的 950 小节写的是"待补"。

**路径与命名。** 命令和路径按运行时布局写：宿主 `/fc-gsd/gsd`、`/fc-gsd/orchestrator`、`/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin`，模板名 `base-gsd`。
结果文件里的开发期产物名只在 20 章的条件标签和附录 A 的对照表里出现。

**措辞。** "交接前的实现 / 修改前 / 修复后"指同一份代码的两个阶段；"进程级"指本书，"整机级"指姊妹手册；"回滚"即 restore。
过程性的叙述（先怀疑什么、后来推翻什么）只在两个案例章（16、17）和 07 章各主题的"根因"小节里，其余章只写现状。

**全书唯一的表。** 目录与层布局（04）、错误表（12）、问题处理总表（14）、已知限制表（15）、两套回滚对比表（18）、判定性数字与缺口表（20）、开关总表与计时键（24）。
别的章需要时链接过去，不复述。

---

## 相关材料

| 位置 | 是什么 |
|---|---|
| [`../scripts/`](../scripts/README.md) | 随手册附带的脚本：验收（`acceptance/`）、压测（`stress/`）、探针（`probes/`）、切栈（`ops/`）、调试内核的 config 与说明（`kernel/`） |
| [`../../rollback/docs/`](../../rollback/docs/README.md) | 整机级 Checkpoint / Restore 技术手册 |
