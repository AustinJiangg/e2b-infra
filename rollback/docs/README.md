# E2B快照回滚技术手册

> 约 19.5 万字\
> 六篇技术手册：完整教学、技术交接与评审验收共用一套内容。

本特性在同一个活 E2B 沙箱中保存和恢复虚拟机状态。正式交付为本单文件 HTML 与 KASandbox 功能代码；手册按 `KASandbox_0904/deltabox-dev@57a30632cb9cb8eb67a35dd226d9ef4b40458af4` 讲解。各轮被测版本分别记录，不从讲解 SHA 推定。

默认客户已有 E2B、配套 SDK 和自己的部署方式。第五篇说明本特性的使用、前提、配置与自检。

全书的结论、条件和已测范围先看[第 1 章](01-delivery-and-reading.md)。

## 阅读入口

| 我想做什么 | 从这里开始 |
|---|---|
| 了解结论和边界 | [项目、交付范围与阅读方法](01-delivery-and-reading.md) → [能力与边界](03-goals-and-boundaries.md) |
| 从头学习 | [第一篇：导论与基础](01-delivery-and-reading.md) → [第二篇：核心实现](04-architecture-and-flow.md) → [第三篇：原生快照](13-native-snapshot.md) → [第四篇：验证](16-test-methodology.md) |
| 评审和验收 | [测试体系](16-test-methodology.md) → [功能](17-functional-verification.md) → [950 性能与 HDBSS](18-950-performance.md) → [原生分档](19-native-pause-resume.md) → [920B 与平台差异](20-920b-performance.md) → [并发与长跑](21-concurrency-and-longrun.md) → [验收与结论边界](22-acceptance.md) |
| 使用接口 | [SDK 快速上手](23-sdk-quickstart.md) → [语义、错误与并发](24-api-semantics.md) |
| 配置和排障 | [前提与自检](25-build-and-self-check.md) → [配置与容量](26-configuration-and-capacity.md) → [观测](27-observability.md) → [排障](28-troubleshooting.md) |
| 接手开发 | [总体架构](04-architecture-and-flow.md) → [开发与验证](29-development.md) → [现状与方向](30-status-and-directions.md) |

950 是实测主线，920B 的性能与平台差异在第 20 章，详细数据在附录 B，工程修复案例在附录 C。已测、跳过和待验证范围随结果标注。

HTML 支持分篇折叠目录、章节小节展开和当前章高亮。右上角的太阳／月亮图标可切换阅读配色，并在浏览器允许本地存储时记住选择。点击流程图或附录配图可居中放大查看，滚轮或双指缩放，放大后拖动查看细节；双击切换细节与全图，点击图外空白或按 Esc 关闭。

## 全书目录

### 第一篇：导论与基础

- [1．项目、交付范围与阅读方法](01-delivery-and-reading.md)
- [2．沙箱、microVM 与快照基础](02-background.md)
- [3．设计目标、能力边界与方案选择](03-goals-and-boundaries.md)

### 第二篇：核心设计与实现

- [4．总体架构与端到端流程](04-architecture-and-flow.md)
- [5．脏页跟踪与 HDBSS](05-dirty-tracking-and-hdbss.md)
- [6．内存差分树](06-memory-diff-tree.md)
- [7．磁盘分层与回收](07-disk-layering.md)
- [8．Firecracker 接口契约](08-firecracker-contract.md)
- [9．原地回滚](09-in-place-rollback.md)
- [10．设备、网络与时间状态](10-device-network-time.md)
- [11．失败语义与正确性不变量](11-failures-and-invariants.md)
- [12．状态、并发、持久性与生命周期](12-state-and-lifecycle.md)

### 第三篇：原生快照与精确增量

- [13．原生快照的机制与成本](13-native-snapshot.md)
- [14．精确增量的设计与实现](14-native-precise-increment.md)
- [15．两种快照的分工、配合与代价](15-native-cooperation-and-cost.md)

### 第四篇：验证、实测与验收

- [16．测试体系、环境与测量口径](16-test-methodology.md)
- [17．功能正确性与覆盖范围](17-functional-verification.md)
- [18．950 性能分档与 HDBSS 实测](18-950-performance.md)
- [19．原生 pause / resume 分档实测](19-native-pause-resume.md)
- [20．920B 性能与平台差异](20-920b-performance.md)
- [21．并发与长跑](21-concurrency-and-longrun.md)
- [22．验收流程、结果判读与结论边界](22-acceptance.md)

### 第五篇：使用、部署与运维

- [23．SDK 快速上手](23-sdk-quickstart.md)
- [24．接口语义、错误、超时与并发调用](24-api-semantics.md)
- [25．构建、部署前提与能力自检](25-build-and-self-check.md)
- [26．配置与容量规划](26-configuration-and-capacity.md)
- [27．日志、指标与分段计时](27-observability.md)
- [28．按症状排障](28-troubleshooting.md)

### 第六篇：接手开发与后续方向

- [29．代码入口、修改指引与开发验证](29-development.md)
- [30．现状与后续方向](30-status-and-directions.md)

### 附录

- [附录 A．术语、协议与文件格式参考](A-reference.md)
- [附录 B．920B 详细测试结果](B-920b-results.md)
- [附录 C．工程修复案例](C-evolution-and-cases.md)
- [附录 D．代码规模与统计口径](D-code-size.md)
- [附录 E．测试环境、轮次与工具索引](E-environment-and-tools.md)
