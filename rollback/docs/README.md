# Checkpoint / Restore 技术手册

> **v0.2.8** · 2026-09-23 · 江路路（j30059180）
>
> 实现已合入 openEuler [KASandbox `deltabox` 分支](https://gitcode.com/openeuler/KASandbox/tree/deltabox)（[MR !119](https://gitcode.com/openeuler/KASandbox/pull/119)，2026-09-09；orchestrator、Firecracker、Python SDK 同仓）

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
| 评审 / 客户，要看证据 | **00 → 28**（结论与逐档判定）→ **24**（方法与测试矩阵），细节按需进 25 / 26 / 27 |
| 拿到交付件，要在机器上验收 | **29**，判据讲解回看 25 / 26 |

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
| 17 | [`17-observability-and-verification.md`](17-observability-and-verification.md) | 三个时钟、静默退化的检测、交付态验收脚本的设计原则 |

### 第四部分　平台与方案选择

| # | 文档 | 内容 |
|---|---|---|
| 18 | [`18-ext4-vs-xfs.md`](18-ext4-vs-xfs.md) | 两套方案的差异、连带的接口差异、怎么选 |
| 19 | [`19-kunpeng-platform.md`](19-kunpeng-platform.md) | 鲲鹏平台：HDBSS 原理、920B 与 950、部署检查清单 |
| 20 | [`20-vs-native.md`](20-vs-native.md) | 与原生 snapshot 的完整对比，以及两者如何配合使用 |
| 21 | [`21-native-increment-diagnosis.md`](21-native-increment-diagnosis.md) | 原生 snapshot 的增量为什么不精确：现象、判 / 存 / 填三层拆解、判据塌缩 × 每代新进程 × 换入即脏、只换判据的 2 MiB 归并地板、本方案做法为何不能照抄 |
| 22 | [`22-native-increment-fix.md`](22-native-increment-fix.md) | 原生 snapshot 的精确增量：4 KiB 存、2 MiB 拼（两条路线、三层改法、追踪没开时的退路） |
| 23 | [`23-native-increment-cost-and-verification.md`](23-native-increment-cost-and-verification.md) | 原生精确增量的代价、验证与边界：恢复开销、平台策略、兼容、四类验证、影响面只在原生路径 |

### 第五部分　测试与验证

怎么证明它对、它快。方法（24–27）与数据（28、28A）分开；全书的实测数字只在这两篇里。

| # | 文档 | 内容 |
|---|---|---|
| 24 | [`24-test-overview.md`](24-test-overview.md) | 测试体系总览：四种静默失效、三层测试、测试矩阵、测量纪律 |
| 25 | [`25-functional-tests.md`](25-functional-tests.md) | 功能正确性：三重证据、59 项校验、树语义、pause 兼容、兼容矩阵、HDBSS 取证、稳定性 |
| 26 | [`26-performance-methodology.md`](26-performance-methodology.md) | 性能测试：判定口径（200 / 100 ms 怎么钉死）、负载构造、各脚本回答什么、怎么读报告 |
| 27 | [`27-cross-implementation.md`](27-cross-implementation.md) | 与进程级快照和 e2b 原生 snapshot 并排：什么能比、什么不能比 |
| 28 | [`28-results-and-compliance.md`](28-results-and-compliance.md) | **当前代码**的实测数据、对照 200 / 100 ms 的逐档判定、已知劣化、尚未覆盖 |
| 28A | [`28a-historical-results.md`](28a-historical-results.md) | 2026-08 / 09 旧栈那几轮的实测数据，原样保留；XFS 方案、33 步深集、60 秒连打、时机成本仍以此篇为准 |
| 29 | [`29-acceptance-runbook.md`](29-acceptance-runbook.md) | 拿到交付件后在目标机上怎么跑、看什么、发回什么 |

### 第六部分　继续开发

| # | 文档 | 内容 |
|---|---|---|
| 30 | [`30-extending.md`](30-extending.md) | 代码地图、改动指引、不能破坏的不变量、一次改动怎么验证 |
| 31 | [`31-glossary-and-code-map.md`](31-glossary-and-code-map.md) | 术语表、代码索引、文件格式索引、环境变量与 API 总表 |

---

## 相关材料

| 位置 | 是什么 |
|---|---|
| [`OUTLINE.md`](OUTLINE.md) | 本系列的编写计划与写作规约，给维护者看 |
| [`../diagrams/`](../diagrams/) | 三张汇报用 SVG 与 mermaid 图源 |
| [`../slides/`](../slides/) | Slidev 汇报稿（导出的 PDF 挂在 [Releases](https://github.com/AustinJiangg/e2b-infra/releases)） |
| [`../scripts/dev/`](../scripts/dev/) | 开发态验证工具箱与实测报告（原 `test-950/`） |
| [`../scripts/acceptance/`](../scripts/acceptance/) | 交付态验收脚本：`checkpoint_verify.py`（正确性）、`checkpoint_bench.py`（性能） |
| [`../scripts/probes/`](../scripts/probes/) | 开发态探针与跨实现对照脚本（第 21–23、27 篇），不属于交付态验收流程 |

---

## 版本

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.2.8 | 2026-09-23 | 更正过期的未结项：完整 `rpmbuild` 已在 920B 开发环境上离线端到端跑通（2026-09-22，约 2 分 15 秒，产物 `e2b-infra-2026.09-3.aarch64.rpm` 154,258,133 字节、sha256 `01d969d0…7614`；用它部署后 `rollback/scripts/950/run.sh` 的 smoke / func / perf 无 FAIL）。第 28 篇 §8 第 15 条按第 11 条的写法划掉并改为「已解决」，写明 sha、耗时、产物大小与三档结果目录名，并注明 950 上还没有用本期代码出包部署、以 LFS 原件为 `Source0` 的 rpmbuild 还没跑过；第 30 篇 §5.1「尚未跑到的实测」去掉 rpmbuild 一项，§1.2.1 补一句该部署方式在 920B 上走通、950 上未走；`rollback/scripts/dev/MANIFEST.md` 第三节「未验证」里的 rpmbuild 一条移入「已验证」并补第二次的实测 |
| v0.2.7 | 2026-09-23 | 第 16 篇 §7.1 补一段：本仓库 nomad 单机部署的原生 snapshot 与模板写在本机 `/tmp/templates/<buildID>/`（`Local` provider），openEuler 上 `/tmp` 是 tmpfs，宿主重启后全部丢失、模板要重新建；依据与落盘做法链到 `deploy-docs/10` §7.1（同批新写：file:line 依据表、核对命令、`tmpfiles` 10 天清理规则、可选的 `LOCAL_*_BASE_PATH` 落盘做法）；`deploy-docs/06` §8.4 重启恢复清单加第 7 步「重新建模板」 |
| v0.2.6 | 2026-09-22 | 测试用的沙箱规格进手册：第 24 篇新增 §5.0「测试环境与沙箱规格」——模板 `base` = `harbor:443/e2b-orchestration/ubuntu:22.04-custom`、**2 vCPU / 2048 MB**、磁盘约 940 MB，由 `benchmark/build_template.py` 建（`aebf028` 起写死该规格），并给出 `GET /templates` 的 `cpuCount` / `memoryMB` / `diskSizeMB` 核对命令；第五部分各篇的数据均在该规格下采得，规格变了要重采。第 26 篇 §（条件标签）、第 28 篇 §1 条件表与 §4 / §5.1 的条件标签改为引用该节；第 29 篇 §1 新增「模板」前置条件（建法 + 规格核对 + 别名占用的处理指路）。`rollback/scripts/950/README.md` 的「跑之前要确认」四件事改为五件（加模板规格一行），env 默认值改为「仓库 `benchmark/.env` 优先、找不到再退 `/opt/e2b-infra/.env`」并删掉手工拼 env 的那段，解释器写明用装了 SDK 覆盖层的那个（venv 或 conda 均可）；`run.sh` 的默认 env 逻辑同步改 |
| v0.2.5 | 2026-09-22 | 交付形态口径统一：第 30 篇 §1.2 改写为「交付形态只有两样」——代码是 `KASandbox_0904` 的 `deltabox` 分支（`firecracker/` / `packages/` / `py-sdk/` 同仓，开发在 `deltabox-dev`，测试通过后合并），文档是本手册（单个 HTML）；目标平台 950（鲲鹏、HDBSS），920B 是开发环境。原「patch + `firecracker.arm` + rpm」一段降级为新增的 §1.2.1「我们在 950 测试环境上的部署方式（不是交付形态）」并压缩；第 05 篇硬约束 5、第 09 篇 §6、第 22 篇路线表、第 28 篇 §8 缺口 15、`rollback/README.md` 抬头的同类措辞一并改正 |
| v0.2.4 | 2026-09-22 | 对照现行代码补齐使用方契约：第 14 篇 §10 改写为「错误契约」（`reason` → HTTP / Connect code → SDK 异常类 → 沙箱状态 → 调用方动作，12 行），§3 restore 四档失败按现行状态码改正（断链为 412 `chain_broken`、envd 未应答带 `guest_unresponsive`、撕裂后 `list` / `delete` 仍可用且按代标记），§9 补三处保护性检查；第 15 篇新增 §9「限额与超时：四个服务端开关」（`CHECKPOINT_MIN_FREE_BYTES` / `CHECKPOINT_MAX_PER_SANDBOX` / `CHECKPOINT_LOCK_WAIT_TIMEOUT` / `CHECKPOINT_FC_CALL_TIMEOUT` 与 507 / 429 / 503 行为、产物盘容量规划），原 §9 / §10 顺延为 §10 / §11；§5 改写为「原子提交与持久性」（承诺什么 / 不承诺什么：不 fsync、store 根启动即清空、checkpoint 不跨 orchestrator 重启），第 06 / 09 / 13 篇相应改正；第 31 篇 §4 环境变量总表加四行、`PROXY_TRACE` 取值改正，§5.1 加 SDK 默认超时（300 s）与不重放说明，§5.2 换成状态码对照表；第 17 篇 §2.2 列出能力行全部字段，第 29 篇 §1 加「核对服务端配置」；仓库表述统一为单仓库 `KASandbox_0904`（交付分支 `deltabox`），第 00 / 18 / 24 / 30 / 31 篇更新，第 30 篇 §1.1 改名「代码仓库」；第 12 篇 §4 按现行实现更新连接跟踪清理（与回滚并行、内核过滤、合并扫表、socket 常驻）、代理连接池在 restore 开始时丢弃，新增 §4.6「对使用方的结论」，第 13 篇 restore 步骤表同步（视图装配移到窗口外）；第 09 篇 §3.3 撕裂判定改为 `fault` 字段；第 11 篇 §3.6–3.8 补 vCPU 挂起 MMIO 排干、GIC 绝对写回、串口与 tap offload；第 22 篇 §3.4、第 20 篇 §5.2 补「checkpoint 之后原生 pause」的内存正确性前提 |
| v0.2.3 | 2026-09-22 | 平台口径统一：目标平台是带 HDBSS 的 950（不设 `FC_TRACK_DIRTY_PAGES`，自动硬件标脏）；920B 是开发环境（显式 `FC_TRACK_DIRTY_PAGES=true`，KVM 写保护，仍是增量）。第 19 篇 §6.3 新增该变量的取值语义（`strconv.ParseBool`，`deltabox-dev@4af2872c6`）与「950 / 无 HDBSS 的机器 / 920B 开发环境」三种情形对照，第 07 / 17 / 24 / 29 / 31 篇引用过去；第 17 篇 §2.2 的启动日志代码与 WARN 文案按现行代码更新；第 29 篇 §2.1 覆盖层文件数 16 → 21、自检输出按现行 `install.py` 更新；第 28 篇 §8 第 18 条「空闲之后的 restore 长尾」写入 2026-09-21 的调查结果（原因未定位、950 待测），新增 §7.2 |
| v0.2.2 | 2026-09-21 | 第 16 篇新增 §1.4「原生 pause / resume 与 checkpoint 的代际边界」（resume 之后是新一代：`list` 为空、旧 id restore 回 `not_found`、新一代可正常重新 checkpoint / restore）；第 19 篇新增 §7.5「orchestrator 每次重启漏掉一整池网络槽位」（成因、上游修复对照、处置与清理办法）；第 26 篇 §7 按「清连接跟踪与回滚并行」改写服务端分段结论，旧说法留作交代；第 24 篇测试矩阵补 T41 / T23 三行，第 25 篇 §5 交叉引用第 16 篇 §1.4；第 28 篇 §8 缺口表更新；**第五部分换数据**——第 28 篇 §4 / §5.1 换成 2026-09-21 `deltabox-dev@4af2872c6` 的分档基准（混合档 n=100/60/30、真实一步场景、20 层链深），2026-08 / 09 旧栈数据整体移入新增的第 28A 篇「历史实测数据」 |
| v0.2.1 | 2026-09-14 | 原生 snapshot 精确增量修复（infra-arm `jll` `c9a92a5ab`）：新增第 21–23 篇专讲原生 snapshot 精确增量（机理 / 改法 / 代价与验证）；第 27 篇新增 §5.5「原生快照口径（修复后）」，第 28 篇新增表 3-J、3-K，00/05/07/20 的「ARM 线退化成工作集」段落加已修复标注 |
| v0.2.0 | 2026-09-10 | 新增第五部分「测试与验证」六篇（现 24–29），原 21/22 移为现 30/31；实测数据收口到实测结果篇 |
| v0.1.0 | 2026-09-04 | 首个署名版本：23 篇正文 + 编写规约 |

版本号只描述**本套文档**，与代码仓库 `KASandbox_0904` 的版本不绑定 ——
文档讲的是设计与机制，代码改动未必引起文档改版。
内容修订进 0.1.x，新增或重写章节进 0.2.0，通读定稿后发 1.0.0。

发布件（单文件 HTML、zip、tar.gz）从本目录生成，挂在
[Releases](https://github.com/AustinJiangg/e2b-infra/releases)，
tag 与此处版本号一致 —— 拿到任何一份产物都能对上是哪一版。
