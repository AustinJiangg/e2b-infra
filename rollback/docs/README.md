# Checkpoint / Restore 技术手册

> **v0.1.0** · 2026-09-04 · 江路路（j30059180）

一套关于**在不中断沙箱的前提下，把一台正在运行的虚拟机回退到过去某一时刻**的系统文档。

主线是我们在 Firecracker 与 e2b orchestrator 上实现的 checkpoint / restore：
它怎么设计、为什么这样设计、每个决定的代价是什么、以及要在它上面继续开发需要知道什么。
为了把主线讲清楚，书里也会讲虚机快照、脏页跟踪、写时复制、virtio 设备状态这些背景知识。

本项目有两套实现，按它们依赖的文件系统能力区分：**ext4 方案**（主线）与 **XFS 方案**。
未特别说明处，全书讲的都是 ext4 方案。两者的差异集中在[第 18 篇](#第四部分平台与方案选择)。

---

## 怎么读

| 你是 | 建议路径 |
|---|---|
| 想先看个全貌 | **00** |
| 不写这类代码，但想搞懂它在干什么 | **01 → 02 → 03**，然后挑感兴趣的 |
| 工程师，要接手或参与 | 全书；机制最短路径是 **06 → 07 → 08 → 10 → 11 → 13** |
| 系统工程师 / 运维 | **02 → 05 → 19 → 17** |
| 使用方，要判断什么时候用它 | **00 → 16 → 20** |

---

## 目录

### 概览

| # | 文档 | 内容 |
|---|---|---|
| 00 | [`00-design-overview.md`](00-design-overview.md) | 一篇读完全貌：定位、结构、两类产物、执行路径、取舍与现状 |

### 第一部分　背景：这是个什么问题

不要求预备知识。

| # | 文档 | 内容 |
|---|---|---|
| 01 | [`01-what-and-why.md`](01-what-and-why.md) | 沙箱、快照与回滚：问题从哪来，「快照」在不同层次上各指什么 |
| 02 | [`02-microvm-and-e2b.md`](02-microvm-and-e2b.md) | microVM 与 e2b 的沙箱模型：Firecracker、模板、沙箱怎么起来的 |
| 03 | [`03-snapshot-fundamentals.md`](03-snapshot-fundamentals.md) | 虚拟机快照的基本原理：状态由什么构成、一致性、重建与原地回写 |
| 04 | [`04-e2b-native-snapshot.md`](04-e2b-native-snapshot.md) | e2b 原生 snapshot / resume 的实现与成本模型 |
| 05 | [`05-design-goals.md`](05-design-goals.md) | 设计目标、硬约束与明确的非目标 |

### 第二部分　核心机制：它是怎么做的

| # | 文档 | 内容 |
|---|---|---|
| 06 | [`06-architecture.md`](06-architecture.md) | 总体架构：四个层次、控制面为什么在宿主侧、存储布局 |
| 07 | [`07-dirty-page-tracking.md`](07-dirty-page-tracking.md) | 脏页跟踪：三种机制、判据差异、HDBSS 的启用与降级 |
| 08 | [`08-memory-diff-tree.md`](08-memory-diff-tree.md) | 内存差分树：每代只存本代脏页、回滚集的推导与正确性、按页解析 |
| 09 | [`09-firecracker-api-contract.md`](09-firecracker-api-contract.md) | 分叉 Firecracker 的三处扩展、FCDB 位图格式、版本配对纪律 |
| 10 | [`10-disk-layering.md`](10-disk-layering.md) | 磁盘分层与零拷贝封存：Seal、SealedView、ResetView |
| 11 | [`11-in-place-rollback.md`](11-in-place-rollback.md) | 进程内原地回滚：九个阶段与提交点 |
| 12 | [`12-rollback-pitfalls.md`](12-rollback-pitfalls.md) | 原地回滚特有的问题：设备缓存、时间、连接跟踪 |
| 13 | [`13-end-to-end.md`](13-end-to-end.md) | 端到端：一次 checkpoint 与一次 restore 的完整走查 |

### 第三部分　工程保障：怎么保证它不出错

| # | 文档 | 内容 |
|---|---|---|
| 14 | [`14-failure-semantics.md`](14-failure-semantics.md) | 失败语义与不变量：提交点、纪元不能丢、断链拒绝恢复 |
| 15 | [`15-state-and-concurrency.md`](15-state-and-concurrency.md) | 状态管理与并发：如何在运行中的机器上换零件 |
| 16 | [`16-lifecycle-and-portability.md`](16-lifecycle-and-portability.md) | 生命周期与可移植性边界：产物何时失效、为什么脱离沙箱恢复不了 |
| 17 | [`17-observability-and-verification.md`](17-observability-and-verification.md) | 三个时钟、静默退化的检测、验收脚本的设计与实测口径 |

### 第四部分　平台与方案选择

| # | 文档 | 内容 |
|---|---|---|
| 18 | [`18-ext4-vs-xfs.md`](18-ext4-vs-xfs.md) | 两套方案的差异、连带的接口差异、怎么选 |
| 19 | [`19-kunpeng-platform.md`](19-kunpeng-platform.md) | 鲲鹏平台：HDBSS 原理、920B 与 950、部署检查清单 |
| 20 | [`20-vs-native.md`](20-vs-native.md) | 与原生 snapshot 的完整对比，以及两者如何配合使用 |

### 第五部分　继续开发

| # | 文档 | 内容 |
|---|---|---|
| 21 | [`21-extending.md`](21-extending.md) | 代码地图、改动指引、不能破坏的不变量、已知缺口 |
| 22 | [`22-glossary-and-code-map.md`](22-glossary-and-code-map.md) | 术语表、代码索引、文件格式索引、环境变量与 API 总表 |

---

## 相关材料

| 位置 | 是什么 |
|---|---|
| [`OUTLINE.md`](OUTLINE.md) | 本系列的编写计划与写作规约，给维护者看 |
| [`../diagrams/`](../diagrams/) | 三张汇报用 SVG 与 mermaid 图源 |
| [`../slides/`](../slides/) | Slidev 汇报稿（导出的 PDF 挂在 [Releases](https://github.com/AustinJiangg/e2b-infra/releases)） |
| [`../test-950/`](../test-950/) | 开发态验证工具箱与实测报告 |
| `../../benchmark/` | 交付态验收脚本：`checkpoint_verify.py`（正确性）、`checkpoint_bench.py`（性能） |

---

## 版本

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.1.0 | 2026-09-04 | 首个署名版本：23 篇正文 + 编写规约 |

版本号只描述**本套文档**，与 `infra-arm` / `KASandbox` 的代码版本不绑定 ——
文档讲的是设计与机制，代码改动未必引起文档改版。
内容修订进 0.1.x，新增或重写章节进 0.2.0，通读定稿后发 1.0.0。

发布件（单文件 HTML、zip、tar.gz）从本目录生成，挂在
[Releases](https://github.com/AustinJiangg/e2b-infra/releases)，
tag 与此处版本号一致 —— 拿到任何一份产物都能对上是哪一版。
