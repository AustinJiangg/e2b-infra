# Checkpoint / Restore 技术手册

> **v0.3.1**（2026-09-29，同步到 deltabox-dev@93ccb02：delete 按结果作答、能力文件）· 江路路（j30059180）
>
> 代码基准：交付的 RPM（e2b-infra）里的 0001 补丁融合自 KASandbox_0904 的 deltabox-dev@93ccb02（orchestrator、Firecracker、Python SDK 同仓）；deltabox 交付分支待整理

**在不中断沙箱的前提下，把一台正在运行的 e2b 沙箱虚机退回到过去某一时刻** —— 这套 checkpoint / restore 的使用、部署、原理与证据。

---

## 按角色的阅读路径

| 你是 | 建议路径 |
|---|---|
| **客户开发者**：要在代码里调用 checkpoint / restore | [00](00-overview.md) → [01](01-quickstart.md) → [02](02-semantics-and-limits.md) → [03](03-errors-timeouts-concurrency.md) → [04](04-performance-expectations.md) |
| **运维**：部署、配置、监控、排障、上机验收 | [00](00-overview.md) → [05](05-deployment-prerequisites.md) → [06](06-configuration-and-capacity.md) → [07](07-observability-reference.md) → [08](08-troubleshooting.md) → [09](09-acceptance-runbook.md)；错误含义查 [03](03-errors-timeouts-concurrency.md) |
| **我方开发**：接手或继续开发 | [00](00-overview.md) → [12](12-architecture.md) → [13](13-dirty-page-tracking-and-hdbss.md) → [14](14-memory-diff-tree.md) → [15](15-firecracker-api-contract.md) → [16](16-disk-layering.md) → [17](17-in-place-rollback.md) → [18](18-rollback-pitfalls.md) → [19](19-end-to-end.md) → [20](20-failure-semantics.md) → [21](21-state-concurrency-durability.md) → [B](B-extending.md)；背景不熟先读 [10](10-background.md) |
| **评审**：判断设计是否成立、证据是否充分 | [00](00-overview.md) → [11](11-baseline-goals-and-native.md) → [04](04-performance-expectations.md) → [23](23-testing-and-functional-verification.md) → [24](24-performance-methodology.md) → [25](25-results-and-compliance.md)；关心边界再读 [22](22-lifecycle-reasoning.md) |

---

## 目录

### 导读

| # | 文档 | 内容 |
|---|---|---|
| 00 | [导读与总览](00-overview.md) | 这是什么、解决什么问题、总体结构、能力边界、当前状态 |

### 第一部分　使用指南

| # | 文档 | 内容 |
|---|---|---|
| 01 | [快速上手](01-quickstart.md) | 装 SDK 覆盖层与自检；checkpoint / restore / list / delete 示例；`mem_mode`；树形历史 |
| 02 | [语义与边界](02-semantics-and-limits.md) | 能回滚什么、回滚不了什么（外部世界、TCP、时钟）；失效条件；删除与空间回收；与原生 snapshot 配合 |
| 03 | [错误、超时与并发调用](03-errors-timeouts-concurrency.md) | 错误总表（全书唯一）、异常类层次、SDK 超时与不重放、多调用方、两类流式截断 |
| 04 | [性能预期](04-performance-expectations.md) | 成本模型、判定结论、已知长尾（数字在 25） |

### 第二部分　部署与运维

| # | 文档 | 内容 |
|---|---|---|
| 05 | [部署前提与检查清单](05-deployment-prerequisites.md) | HDBSS / 内核 / 产物盘 / 二进制配对 / 部署后自检 |
| 06 | [配置参考与容量规划](06-configuration-and-capacity.md) | 开关总表（全书唯一）、启动能力行字段、容量规划 |
| 07 | [可观测性参考](07-observability-reference.md) | 能力行、`mem_mode`、timings 全字段与计时键、取证开关 |
| 08 | [排障（按症状）](08-troubleshooting.md) | 症状 → 看哪里 → 怎么处置 → 原理链接 |
| 09 | [上机验收](09-acceptance-runbook.md) | 交付态验收：`run.sh` smoke / func / perf / long、自检、结果回传 |

### 第三部分　原理与设计

| # | 文档 | 内容 |
|---|---|---|
| 10 | [背景：沙箱、microVM 与快照原理](10-background.md) | 沙箱与 microVM、虚机状态由什么构成、一致性、重建与原地回写 |
| 11 | [基线、目标与原生对比](11-baseline-goals-and-native.md) | e2b 原生 snapshot、设计目标与非目标、与原生的对比表（全书唯一） |
| 12 | [总体架构](12-architecture.md) | 四个层次、控制面在宿主侧、目录布局（全书唯一） |
| 13 | [脏页跟踪与 HDBSS](13-dirty-page-tracking-and-hdbss.md) | 三种跟踪机制、判据差异、HDBSS 的启用与降级 |
| 14 | [内存差分树](14-memory-diff-tree.md) | 每代只存本代脏页、回滚集与内容解析、删除 / 隐藏 / 合并（compact / fold）、全量根 |
| 15 | [Firecracker 接口契约](15-firecracker-api-contract.md) | 分叉 Firecracker 的扩展端点、位图格式、版本配对 |
| 16 | [磁盘分层](16-disk-layering.md) | 写层封存、视图切换、层引用计数回收、层合并、位图装配 |
| 17 | [进程内原地回滚](17-in-place-rollback.md) | 原地回滚的各个阶段与提交点 |
| 18 | [原地回滚特有的问题](18-rollback-pitfalls.md) | 网络描述符缓存、VMGenID、连接跟踪、时间、队列页 |
| 19 | [端到端走查](19-end-to-end.md) | 一次 checkpoint 与一次 restore 的完整路径 |
| 20 | [失败语义与不变量](20-failure-semantics.md) | 提交点、纪元不能丢、断链拒绝恢复、账本污染、不变量清单 |
| 21 | [状态、并发与持久性](21-state-concurrency-durability.md) | 锁、原子提交、锁外删除、节点级效应 |
| 22 | [生命周期边界的推导](22-lifecycle-reasoning.md) | 为什么脱离活沙箱恢复不了 |

### 第四部分　测试与证据

| # | 文档 | 内容 |
|---|---|---|
| 23 | [测试体系与功能验证](23-testing-and-functional-verification.md) | 三层测试、crtest 与 `run.sh`、功能正确性证据 |
| 24 | [性能口径与方法](24-performance-methodology.md) | 判定口径、负载构造、各脚本回答什么、怎么读报告 |
| 25 | [实测结果与判定](25-results-and-compliance.md) | 当前代码的功能、分档、长跑与并发数据；950 待补；缺口清单 |

### 附录

| # | 文档 | 内容 |
|---|---|---|
| A | [术语、代码地图、文件格式](A-glossary-and-code-map.md) | 术语表、代码索引、文件格式、API 总表 |
| B | [继续开发](B-extending.md) | 改动指引、不能破坏的不变量、一次改动怎么验证 |

---

## 相关材料

| 位置 | 是什么 |
|---|---|
| [`../../deploy-docs/`](../../deploy-docs/README.md) | 部署文档：整体架构、RPM 构建、`build.sh` 安装与启动、日常运维、存储位置、资源调优 |
| [`../../single-node-offline-deploy.md`](../../single-node-offline-deploy.md) | 单机离线部署步骤，含部署后 checkpoint 能力的核对 |
| [`../../e2b-deploy/dep/e2b-sdk-checkpoint/`](../../e2b-deploy/dep/e2b-sdk-checkpoint/使用说明.md) | Python SDK 覆盖层（`install.py` 与使用说明） |
| [`../scripts/950/`](../scripts/950/README.md) | 上机验收入口 `run.sh`（smoke / func / perf / long） |
| [`../scripts/crtest/`](../scripts/crtest/README.md) | 功能与性能测试套件（定向用例、分档基准） |
| [`../scripts/acceptance/`](../scripts/acceptance/README.md) | 交付态单文件验收脚本：`checkpoint_verify.py`（正确性）、`checkpoint_bench.py`（性能） |
| [`../scripts/dev/`](../scripts/dev/) | 开发态验证工具箱 |
