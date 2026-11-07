# 附录 B · 920B 测试明细

本附录保存 920B 的条件标签、轮次总表、逐项表和数据出处。讲解和结论在[第 20 章](20-920b-performance.md)与[第 21 章](21-concurrency-and-longrun.md)，本附录只留查数用的明细。下列结果各自适用其原始条件，不能作为 950 性能指标或跨平台净收益。

## 1. 条件标签

下表是新 920B 的条件，2026-09-22 起的结果都跑在这台机器上。旧 920B 的条件在表后单列，950 的条件在[第 18 章 §2](18-950-performance.md#2-条件与来源)。

| 项 | 值 |
|---|---|
| 机型 | 920B（`lscpu` 型号 Kunpeng 920 7280Z），2 路，160 物理核 / 320 线程，4 个 NUMA 节点 |
| 内存 | MemTotal 1055027476 kB（约 1 TiB） |
| OS / 内核 | openEuler 24.03 LTS-SP4，`6.6.0-159.4.3.154.oe2403sp4.aarch64` |
| 产物盘 | 根分区 `/dev/mapper/openeuler-root`，**ext4 真盘**，4.3T；产物目录 `/orchestrator/build/checkpoints` 在根分区上 |
| 脏页后端 | **`kvm-wp`**（KVM 软件写保护；920B 没有 HDBSS，orchestrator 显式设 `FC_TRACK_DIRTY_PAGES=true`）。每个干净页的第一次写都要陷出一次虚机，这笔开销含在下面每个数字里 |
| orchestrator | `deltabox-dev@8ea5322bf` 构建，与它拉起的 FC 同在一个上限 256 GiB 的 memory cgroup 里（cgroup v1；页缓存记在这个账上，guest 内存走 2 MiB 大页、不记账，[第 12 章 §8](12-state-and-lifecycle.md#8-节点级效应)） |
| Firecracker | 部署件自带，sha256 `18f3faa7f47c173a5f47bfc7f578f5073cfbde2ac9bdb50bf88c434341d6d4c9`（核对身份以它为准）。放在 `/fc-versions/v1.13.1/` 下，目录名取自模板记录的版本；二进制自报 `Firecracker v1.12.1`，即分叉所基于的上游版本（[第 25 章 §4](25-build-and-self-check.md#4-版本配对)） |
| guest 内核 | 6.1.158 |
| 模板 | `base`：2 vCPU / 2048 MB，rootfs 940 MiB（[附录 E §2](E-environment-and-tools.md#2-测试环境与沙箱规格)） |

机型、内存、内核、产物盘用 `lscpu`、`/proc/meminfo`、`uname -r`、`df -hT /`、`findmnt -T /orchestrator/build/checkpoints` 核对。
**这些数字不能当作 950 的指标**：950 用 HDBSS，脏页后端、机型、盘都不同（[第 16 章 §4](16-test-methodology.md#4-条件标签)）。

2026-09-21 及以前的结果跑在旧 920B 上：

| 项 | 值 |
|---|---|
| 规模 | 128 核，2 TB 内存 |
| 产物盘 | `/mnt/ext4dev`，ext4，`/dev/loop1` 循环设备，direct-io，4K 扇区 |
| 脏页后端 | `kvm-wp`（`FC_TRACK_DIRTY_PAGES=true`） |
| 2026-09-15～16 的代码 | orchestrator `c9a92a5ab`（当时的提交号为 `de25fe4d0`），FC `3863c76`，guest 内核 6.1.158 |
| 2026-09-15 的 SDK | `e2b 2.20.0` 手工补丁版，不含 `Sandbox.connect()` 路由头修复；2026-09-16 换成带修复的 SDK 覆盖层 |

两台机器和盘都不同，跨机对比只看趋势。

## 2. 总表

| 项 | 结果 |
|---|---|
| `run.sh smoke` | 开发态部署上 4 / 5：`checkpoint_verify.py` **59 / 59**；宿主预检 FAIL 0；capabilities 行在；FC sha256 与上表一致。未过的一项是 SDK 覆盖层自检：当时的判据要求"恰好 8 个异常子类"，而当时新增了第 9 个（`CheckpointBytesLimitException`）。判据改为"原有 8 个都在、映射表全是子类"之后，在 RPM 部署件上两次重跑都是 **5 / 5**（[§5](#5-部署后回归)） |
| `run.sh func` | **PASS 17 · FAIL 0 · SKIP 4**（SKIP 的 T18 / T21 / T37 / T38 需要重启或注入，单独跑，见 [§4](#4-重启与注入类)）；RPM 部署件上两次重跑结果相同（[§5](#5-部署后回归)） |
| crtest T36 全规模（16 沙箱 × 1800 s） | 78695 次操作 0 失败、0 不一致，426 条断言通过 |
| 重启与注入类（`3539c45df`） | 10 项全过 |
| FC Rust 单测 | **1554 passed / 0 failed**（三遍：`--all` 835、`--all --examples` 3、带 `rollback-fault-inject` 特性 716） |
| Go 单测与属性测试 | `internal/checkpoint` 等包 `-race` 通过；本机固有的几项环境性失败（cgroup 只读、UFFD API、需拉容器的包）与改动前基线完全相同 |

FC 单测那次跑的源码早于 `8ea5322bf`；此后 FC 目录只多了测试内核文件和一条新单测（`vm.rs` 的
`test_full_snapshot_sidecar_is_all_ones`，单独跑过、通过），FC 产品代码没有变化。

## 3. func 档逐项

| 用例 | 断言数 | 用例 | 断言数 |
|---|---|---|---|
| T11 生命周期竞争 | 44 | T25 时间与 CPU | 81 |
| T12 同沙箱多客户端 | 9 | T26 串口 | 63 |
| T13 冻结窗口干扰 | 78 | T32 restore 随脏集 | 18 |
| T14 写密集并发 | 92 | T34 运行期读链深 | 70 |
| T15 树形分支并发 | 84 | T36 混合稳态（4 沙箱 × 120 s） | 111 |
| T22 深链随机回滚 | 77 | T39 checkpoint 后原生 pause | 26 |
| T23 文件系统边界 | 19 | T40 restore 紧跟请求 | 17 |
| T24 网络状态 | 12 | T41 干净沙箱原生 pause | 60 |
| SDK 异常语义 pytest | 60 passed | | |

## 4. 重启与注入类

每项带对应开关重启 orchestrator 后跑（`3539c45df`）：

| 用例 | 开关 | 通过断言 |
|---|---|---|
| T18 重启后账本不跨重启 | —— | 16 |
| T37 `disk_full` | `CHECKPOINT_MIN_FREE_BYTES` 设为大于盘容量 | 16 |
| T37 `too_many` | `CHECKPOINT_MAX_PER_SANDBOX=2` | 24 |
| T21 `envd_timeout` / `torn_assemble` / `seal_move` / `commit_late` | `CHECKPOINT_FAULT_INJECT=<名字>` | 9 / 9 / 7 / 8 |
| T21 `seal_move:once` / `commit_late:once` | 同上，只炸一次 | 8 / 11 |
| T38 FC 过提交点后失败 | 带 `rollback-fault-inject` 特性的 FC + `FC_ROLLBACK_FAULT_INJECT=post_commit` | 13 |

以上 10 项跑在 `3539c45df` 上；它与 `8ea5322bf` 的差别见 [§9](#9-轮次总表)，不涉及这些用例测的重启、配额与故障注入路径。

## 5. 部署后回归

前面几节跑在开发态部署上（只替换 orchestrator 二进制）。950 测试部署方式是 RPM，因此又用同一台 920B 按这种方式完整重装了两次
（orchestrator、Firecracker、SDK 覆盖层都换成部署件），各跑一遍回归：

- **第一次（2026-09-29 上午）**：部署件由 `8ea5322bf` 构建，即本附录其余各节的代码；
- **第二次（2026-09-29 下午）**：部署件由 `93ccb02` 构建。它在 `8ea5322bf` 之上多四个提交：delete 按实际情况回答
  （不再把所有错误都答成 404：条目不存在仍是 404 `not_found`；删除已生效、只是之后清理文件失败时照常返回成功并打 WARN；其他错误回 500 `internal`，
  错误码见 [第 24 章](24-api-semantics.md)）；orchestrator 启动时除了打能力行，还把同样的内容写进能力文件
  `/orchestrator/build/checkpoint-capabilities.json`（能力行在高负载下会很快被日志轮转冲掉，smoke 改读这个文件，[第 26 章 §3.1](26-configuration-and-capacity.md#31-能力文件)）；
  另两个提交只改注释（SDK 超时说明、conntrack 与全量根的代码注释）。

| 项 | 第一次（`8ea5322bf` 部署件） | 第二次（`93ccb02` 部署件） |
|---|---|---|
| `run.sh smoke` | **PASS 5 · FAIL 0 · SKIP 0** | **PASS 5 · FAIL 0 · SKIP 0** |
| 　宿主预检 | PASS 15 / WARN 4 / FAIL 0 | PASS 15 / WARN 4 / FAIL 0 |
| 　SDK 覆盖层自检 | 通过（新判据：原有 8 个子类都在、映射表全是子类） | 通过（同左；21 个文件在位） |
| 　能力 | 能力行在：`compact=true`、`compact_max_per_op=8`、`max_checkpoint_bytes_per_sandbox=0`、`debug_index=false` | 能力文件在：`track_dirty_pages=true`（来源 `FC_TRACK_DIRTY_PAGES="true"`）、`compact=true`、`compact_max_per_op=8`、`max_checkpoints_per_sandbox=0`、`max_checkpoint_bytes_per_sandbox=0`、`min_free_bytes=4294967296`、`debug_index=false`、`fault_inject=[]`，各项来源均为 `default` |
| 　Firecracker sha256 | 两处都是 `18f3faa7…` | 两处都是 `18f3faa7…` |
| 　`checkpoint_verify.py` | 59 / 59 | 59 / 59 |
| `run.sh func` | **PASS 17 · FAIL 0 · SKIP 4** | **PASS 17 · FAIL 0 · SKIP 4** |
| 　T36（4 沙箱 × 120 s） | 112 条断言 | 110 条断言 |
| 　SDK 异常语义 pytest | 60 passed | **67 passed**（新增的是 delete 按实际情况回答的用例） |
| 　跳过项 | T18 / T21 / T37 / T38（需要重启或注入） | 同左 |
| 开发态 `correctness.py`（断言已按合并语义更新） | **ALL PASS** | **ALL PASS** |

`correctness.py` 两次都验证了同样几件事：线性链上逐级回退与跨链前滚、分叉后跨分支 restore（路径经最近公共祖先）后内存、磁盘、blob 三项都回到目标时刻；
删除一个只有一个子节点的 checkpoint 时它被合并进子节点、子节点的父指针改指向它原来的父节点，跨过它的 restore 照常正确；
人为移走一份位图侧车时 restore 被拒、沙箱状态不变且照常运行，放回后又能恢复。

第一次部署后还跑了一轮 16 沙箱 × 300 s、不设个数上限的混合并发，只用来确认极限负载下部署件的正确性不受影响；它的数据只在 [附录 B §10.6](#106-部署后无上限并发)。

## 6. 分档基准与判定

混合档、附表、拟合、判定和全量单列在[第 20 章 §2](20-920b-performance.md#2-分档基准与判定)。这里只留取数细节。

- 条件：[§1](#1-条件标签) · 单沙箱串行 · 页缓存全热 · `bench_tiers.py` 全表 3 遍，1040 次迭代。
- p99 有两种取法。`analyze.py` 取 ceil(qn)，n ≤ 100 时即最大值；`compliance.py` 取 round(q(n−1))。个别档相差可达 11 ms，例如 mix256 checkpoint 118.7 对 107。手册用 `analyze.py` 的值。
- 全量 checkpoint 在 `compliance.py` 口径下 p50 653 ms，手册写服务端 433–445 ms、客户端墙钟 0.63–0.67 s。
- 2026-09-20 与 09-21 在旧 920B 上另有两轮，代码分别是 `702279c98` 和 `4af2872c6`，都是长跑修复之前的版本。后者的混合档摘录在[第 20 章 §2.7](20-920b-performance.md#27-修复前的分档不进判定)。

### 6.3 拟合与碰线点

数值在[第 20 章 §2.4](20-920b-performance.md#24-拟合与碰线点)。

## 7. 单沙箱串行与空闲后 restore

串行 5000 次、空闲后 restore 和串口的数字在[第 20 章 §3](20-920b-performance.md#3-单沙箱串行与空闲后-restore)。

### 7.2 空闲后 restore

逐臂表在[第 20 章 §3.2](20-920b-performance.md#32-空闲后-restore)。下表是原始汇总里手册没有列的 p90 与最大值（客户端墙钟 ms，`8ea5322bf`）。

| 臂 | p90 | max |
|---|---|---|
| 空闲 0 s | 19.0 | 21.0 |
| 空闲 0.5 s | 25.3 | 29.5 |
| 空闲 2 s | 26.9 | 38.4 |
| 空闲 5 s | 29.9 | 35.4 |
| 空闲 10 s | 57.5 | 86.9 |
| 空闲 2 s 后不拍 cpB | 85.8 | 91.7 |
| guest 内常驻忙循环 | 30.9 | 36.4 |

## 8. 长跑与并发（摘要）

结论在[第 21 章](21-concurrency-and-longrun.md)。这些是服务端、多沙箱并发的数字，与[第 20 章 §2](20-920b-performance.md#2-分档基准与判定)按客户端墙钟、单沙箱串行的判定表不能直接比较。

## 9. 轮次总表

本节列新 920B 上 2026-09-28～29 的全部轮次。下表的 `4af2872c6` 已包含 74 条 MR 后提交，称为“修复前”是指这一组长跑修复之前；MR 基线为 `0f9b58f92`。各轮前后对比用于评估对应改动，不直接代表相对 MR 版的加速比（[附录 C §1.1](C-evolution-and-cases.md#11-条件)）。表中“RPM”指 950 测试部署方式的部署件。

二进制与修复的对应关系（依次叠加，每项修复的原理见右列链接）：

| 二进制 | 在上一个基础上新增 | 原理 |
|---|---|---|
| RPM 部署版 `4af2872c6` | 本组长跑修复前基线，已包含 74 条 MR 后提交 | [附录 C §1.1](C-evolution-and-cases.md#11-条件) |
| `cba26f7c1` | index.json 不再每次重写（[附录 C §3.2](C-evolution-and-cases.md#32-每次-checkpoint-整份重写-indexjson)）；锁外删文件（[附录 C §3.3](C-evolution-and-cases.md#33-在全局锁里删文件)）；GC 直方图取证开关 | [第 12 章](12-state-and-lifecycle.md) |
| `baae30b56` | 代理开 full duplex（[附录 C §3.1](C-evolution-and-cases.md#31-流式命令偶发截断)） | [第 24 章 §11.3](24-api-semantics.md#113-两类流式截断要分清) |
| `5319de5dc` | 拷贝前 `MADV_POPULATE`（[附录 C §3.4](C-evolution-and-cases.md#34-gc-长暂停落进别的沙箱的冻结窗口)）；restore 自计读量（[附录 C §3.5](C-evolution-and-cases.md#35-restore-读盘量指标在并发时虚高)） | [第 12 章 §8](12-state-and-lifecycle.md#8-节点级效应) |
| `1b112fbbd` | conntrack 整表扫描与选路（[附录 C §3.6](C-evolution-and-cases.md#36-conntrack-清理占了冻结窗口)） | [第 10 章 §4](10-device-network-time.md#4-连接跟踪) |
| `df4b38336` | 层按视图引用计数回收、子节点计数表（[附录 C §3.7](C-evolution-and-cases.md#37-rootfs-层文件从不回收)） | [第 7 章 §4](07-disk-layering.md#4-删除-checkpoint先判断哪些层仍被需要) |
| `0c4c2c5ae` | 隐藏 checkpoint 合并（内存与层，[附录 C §3.8](C-evolution-and-cases.md#38-隐藏-checkpoint-无上界积累磁盘随时间线性增长)）；字节配额 | [第 6 章 §10](06-memory-diff-tree.md#10-删除隐藏与合并)、[第 7 章 §5](07-disk-layering.md#5-层合并保留读取结果减少层文件) |
| `3539c45df` | 全量根回滚因子恒为全 1（加固，长跑不涉及） | [第 6 章](06-memory-diff-tree.md) |
| `8ea5322bf` | 视图装配改位图（[附录 C §3.9](C-evolution-and-cases.md#39-视图块数增长restore-随时间变慢)）；`disk_view_read` 计时 | [第 7 章 §7](07-disk-layering.md#7-实现细节读取索引装配成本与落盘) |
| `93ccb02`（部署件，`/usr/bin/template-manager` sha256 `782d6a2a…`） | delete 按实际结果回错误码（id 不存在才回 404，已生效但清文件失败按成功答，其余回 500）；启动时写 checkpoint 能力文件；注释与 Python SDK 的说明文字。restore / checkpoint 热路径与 `8ea5322bf` 相同（`conntrack_flush.go` 只改了注释） | —— |

然后是全部轮次。除注明外都是 16 沙箱、模板 `base`、服务端开 `gctrace`；restore / checkpoint 为服务端 `total` 的 p50 / p99（ms）。
两天的日期是 2026-09-28 与 09-29，时间为北京时间。

| 轮 | 时间 | 二进制 | 形态与开关 | 时长 | restore 逐项一致 | restore p50 / p99 | checkpoint p50 / p99 | 这一轮要回答的问题 |
|---|---|---|---|---|---|---|---|---|
| long1 | 09-28 05:50–06:20 | RPM | 无上限 | 1800 s | 32547 / 32547 | 250.1 / 1148.7 | 115.6 / 738.8 | 修复前的基线长什么样 |
| long2 | 06:55–07:25 | RPM | 无上限；`PROXY_TRACE` 开 | 1800 s | 31680 / 31680 | 309.3 / 1289.4 | 126.1 / 773.5 | 流截断发生在哪一跳、在请求的哪个阶段 |
| long3 | 08:23–08:53 | `cba26f7c1` | 无上限；`CHECKPOINT_RUNTIME_METRICS=1` | 1800 s | 32340 / 32340 | 362.7 / 1535.7 | 70.2 / 680.5 | 去掉 index 热写和锁内删文件后，锁等待与 GC 怎样；长 STW 卡在哪一半 |
| long4 | 09:17–09:47 | `baae30b56` | 同 long3 | 1800 s | 30173 / 30173 | 382.3 / 1705.5 | 76.8 / 800.1 | full duplex 能否让截断归零 |
| long5 | 10:19–10:49 | `5319de5dc` | 同 long3 | 1800 s | 28331 / 28331 | 450.8 / 1698.2 | 91.1 / 759.6 | `MADV_POPULATE` 能否消掉 GC 长暂停；新读量键是否可信 |
| long6 | 10:58–11:28 | `5319de5dc` | **上限 60** | 1800 s | 70702 / 70702 | 193.1 / 337.8 | 34.0 / 128.1 | 常规用法下的基线；有没有常规负载才出现的问题 |
| ct-opt 基线 | 12:12–12:19 | `5319de5dc` + conntrack 子计时 | 上限 60 | 420 s | 17009 / 17009 | 182.7 / 313.1 | 32.3 / 114.0 | conntrack 的时间花在哪一半（不计入总数） |
| ct-opt | 12:30–12:46 | `1b112fbbd` | 上限 60 | 900 s | 41748 / 41748 | 113.5 / 333.3 | 33.7 / 253.1 | 新清扫算法能否把 conntrack 移出冻结窗口 |
| d1 | 13:33–13:48 | `df4b38336` | 上限 60（未开 gctrace） | 900 s | 43352 / 43352 | 107.3 / 192.8 | 32.1 / 93.5 | 层文件是否按视图回收、层数是否等于视图并集 |
| long7 | 16:26–16:56 | `0c4c2c5ae` | 上限 60 | 1800 s | 87973 / 87973 | 105.2 / 191.7 | 31.6 / 80.7 | 合并之后目录、磁盘、链深是否走平 |
| long8 | 17:00–17:30 | `0c4c2c5ae` | **滚动保留 10** | 1800 s | 31443 / 31443 | 106.4 / 202.8 | 36.0 / 81.9 | "保留最新 N 个"用法下是否收敛 |
| long8 对照 | 17:38–17:48 | `0c4c2c5ae` | 滚动保留 10，**`CHECKPOINT_COMPACT=false`** | 600 s | 8128 / 8128 | 405.2 / 930.5 | 41.5 / 167.5 | 不合并时同一负载会怎样 |
| long9 | 09-28 23:26 – 09-29 01:26 | `3539c45df` | 滚动保留 10，沙箱寿命 3 h | 7200 s | 112402 / 112402 | 0–10 min 90.9 / 162.7 → 110–120 min 160.7 / 274.5 | 35.0 / 67.5 → 39.4 / 107.7 | 两小时里有没有随时间劣化的量 |
| long10 | 09-29 02:42–03:43 | `8ea5322bf` | 同 long9 | 3600 s | 75817 / 75817 | 0–10 min 71.0 / 128.3 → 50–60 min 84.1 / 136.3 | 31.8 / 47.0 → 34.0 / 51.5 | 视图装配改位图后，restore 还随块数涨吗 |
| 最终长测 | 04:59–05:29 | `8ea5322bf` | 上限 60；`CHECKPOINT_RUNTIME_METRICS=1` | 1800 s | 97858 / 97858 | 87.7 / 171.3 | 30.4 / 70.8 | 最终版在常规用法下的全貌 |
| 串口段 | 05:43–06:14 | `8ea5322bf` | 单沙箱 × 10 段 × 180 s | 1888 s | 7383 / 7383 | —— | —— | 长时间反复 restore 后串口是否卡死 |
| long11 | 09-29 17:10–19:11 | `93ccb02`（部署件） | 同 long9；`CHECKPOINT_RUNTIME_METRICS=1` | 7200 s | 147557 / 147557 | 0–10 min 73.1 / 131.5 → 110–120 min 94.3 / 160.5 | 32.8 / 47.5 → 34.3 / 54.9 | 部署件跑满 2 h 滚动负载：restore 还随块数涨吗，树与盘是否走平 |

这张表的读法在[第 21 章 §4.5](21-concurrency-and-longrun.md#45-两天-15-组的轮次)。另有几项取数细节：

- 无上限各轮的链深是驱动打印的可见 checkpoint 数，不是真实链深。long3 的 cgroup 采样是中途才开的，起跑后 828 s 的第一个样本已经是满的。
- long9、long10、long11 按段给出首尾两段；逐段数字见 [§10.3](#103-滚动保留-10一小时与两小时)。
- long11 是在前 15 组之后加跑的一轮，不并进 [§10.1](#101-731877-次-restore0-不一致) 的 731877 合计。
- long5、long6 期间 client-proxy 换过带 full duplex 修复的镜像，long6 收尾恢复原镜像。ct-opt 和 long9 各有 1 次截断落在 client-proxy 一跳，推断是它未带修复所致，未做对照验证。
- 同期还有一轮 T36 基线：部署件 `4af2872c6`，07:37–08:08，16 沙箱，1800 s，76992 次操作 0 失败，客户端 restore p50 / p99 60.1 / 221.1 ms，checkpoint 26.1 / 137.8 ms。

## 10. 最终版的结果

### 10.1 731877 次 restore，0 不一致

两天里所有长跑与串口段的 restore 逐项校验数（`D.json` 的 `verified` 分子，串口段取各段 `segment.json`）：

| 轮 | 二进制 | restore 逐项一致 |
|---|---|---|
| long1 | RPM | 32547 |
| long2 | RPM | 31680 |
| long3 | `cba26f7c1` | 32340 |
| long4 | `baae30b56` | 30173 |
| long5 | `5319de5dc` | 28331 |
| long6 | `5319de5dc` | 70702 |
| ct-opt | `1b112fbbd` | 41748 |
| d1 | `df4b38336` | 43352 |
| long7 | `0c4c2c5ae` | 87973 |
| long8 | `0c4c2c5ae` | 31443 |
| long8 对照（不合并） | `0c4c2c5ae` | 8128 |
| long9 | `3539c45df` | 112402 |
| long10 | `8ea5322bf` | 75817 |
| 最终长测 | `8ea5322bf` | 97858 |
| 串口段 10 段 | `8ea5322bf` | 7383 |
| **合计** | | **731877，0 不一致** |

不计入的：ct-opt 基线 17009（它是改算法前的对照）、各功能档与分档基准里的 restore、T36（另计，[§10.5](#105-t36-全规模)）。
d1 的 43352 是驱动全程数，按 900 s 日志窗口统计是 43313，口径不同。

上表的口径是两天 15 组：14 组并发长跑加 1 组串口段。此后另跑了一轮部署件 `93ccb02` 的 2 h 滚动长测 long11（[§10.3](#103-滚动保留-10一小时与两小时)），单独列出，不并进上表合计：

| 轮 | 二进制 | restore 逐项一致 |
|---|---|---|
| long11 | `93ccb02`（部署件） | 147557 / 147557，0 不一致 |

加上它，restore 逐项校验总数为 731877 + 147557 = **879434 次，全部一致**。

这个数字的含义和不含义在[第 21 章 §5](21-concurrency-and-longrun.md#5-两天-15-组长跑731877-次-restore-逐项一致)。

### 10.2 上限 60 的最终长测：30 min 时间序列

条件：`8ea5322bf`，16 沙箱，上限 60，1800 s，起跑 netns 564。每 5 min 一段；树与盘取段内最后一次 60 s 采样。

| 段 | restore n | restore `total` p50 / p99 | restore `frozen` p50 | checkpoint n | checkpoint `total` p50 / p99 / max | `conntrack_bg` p50 | `materialize_read_mb` p50 | 目录 / 可见 / 隐藏 | 真实链深（段内最大） | df 增量 GiB |
|---|---|---|---|---|---|---|---|---|---|---|
| 0–5 min | 16241 | 80.5 / 164.6 | 71.9 | 7962 | 30.6 / 95.3 / 783.5 | 66.8 | 49.9 | 1229 / 944 / 285 | 17 | 89.2 |
| 5–10 min | 16308 | 86.2 / 170.2 | 77.6 | 7014 | 30.4 / 67.6 / 121.5 | 75.3 | 53.0 | 1232 / 954 / 278 | 18 | 87.0 |
| 10–15 min | 16330 | 86.9 / 166.7 | 77.9 | 6918 | 30.0 / 44.0 / 88.1 | 75.1 | 55.9 | 1245 / 951 / 294 | 14 | 86.5 |
| 15–20 min | 16384 | 90.3 / 171.4 | 81.0 | 7115 | 30.3 / 42.8 / 77.3 | 77.4 | 63.3 | 1225 / 950 / 275 | 17 | 86.1 |
| 20–25 min | 16286 | 90.1 / 175.1 | 80.5 | 7093 | 30.7 / 43.3 / 81.6 | 76.2 | 62.9 | 1223 / 953 / 270 | 19 | 84.3 |
| 25–30 min | 16265 | 91.2 / 177.5 | 81.7 | 6991 | 30.4 / 42.7 / 71.4 | 78.8 | 56.4 | 1240 / 947 / 293 | 17 | 85.2 |
| **全程** | 97814 | **87.67 / 171.28** | 78.56 | 43093 | **30.39 / 70.79** / 783.5 | 74.9 | 54.5 | 约 1240，后 15 min −1.02 / min | 13–19 | 峰值 90.3，后 15 min −0.13 / min |

解读、旁路指标、与 long6 的前后对照在[第 21 章 §6](21-concurrency-and-longrun.md#6-每沙箱上限-60-的-30-分钟常规负载)，根分区增长曲线在[第 21 章 §7.2](21-concurrency-and-longrun.md#72-上限-60341-gib-未收敛到约-90-gib-走平)。

### 10.3 滚动保留 10：一小时与两小时

三轮的条件、总表和曲线在[第 21 章 §7.4](21-concurrency-and-longrun.md#74-滚动保留-10一小时与两小时)。这里留逐段表和回归系数。

逐段对照（每 10 min 一段；p50 / p99，ms；块数为每沙箱"所有可见视图总块"均值，目录、链深、df 取段内最后一次采样；long10 只有前 60 min）：

| 段 | 块数 long11 / long9 | restore `total` long11 | long9 | long10 | `assemble_view_pre` p50 long11 / long9 | materialize + fc_rollback p50 long11 / long9 | 冻结内等 conntrack p50 long11 | 目录 / 链深 long11 | df 增量 GiB long11 |
|---|---|---|---|---|---|---|---|---|---|
| 0–10 min | 46742 / 44437 | 73.1 / 131.5 | 90.9 / 162.7 | 71.0 / 128.3 | 0.3 / 12.8 | 42.7 / 46.2 | 2.2 | 178 / 11 | 41.1 |
| 10–20 min | 65756 / 59697 | 78.8 / 131.9 | 109.2 / 194.7 | 78.6 / 130.3 | 0.3 / 21.3 | 48.6 / 54.6 | 1.1 | 187 / 11 | 42.2 |
| 20–30 min | 75494 / 73077 | 80.7 / 131.6 | 123.9 / 213.0 | 81.7 / 132.0 | 0.4 / 31.0 | 51.7 / 59.2 | 0.0 | 182 / 10 | 43.1 |
| 30–40 min | 79819 / 76312 | 82.5 / 131.2 | 133.6 / 225.6 | 82.1 / 133.9 | 0.4 / 36.5 | 53.8 / 61.2 | 0.0 | 183 / 11 | 43.7 |
| 40–50 min | 84289 / 79778 | 84.3 / 134.5 | 138.2 / 232.8 | 83.2 / 134.1 | 0.4 / 38.7 | 55.7 / 61.8 | 0.0 | 179 / 10 | 43.7 |
| 50–60 min | 89841 / 83251 | 85.2 / 136.8 | 141.9 / 240.8 | 84.1 / 136.3 | 0.4 / 41.7 | 56.8 / 62.5 | 0.0 | 177 / 11 | 44.1 |
| 60–70 min | 93305 / 87227 | 85.4 / 140.9 | 148.3 / 252.7 | —— | 0.4 / 44.6 | 56.7 / 63.9 | 0.0 | 173 / 11 | 44.7 |
| 70–80 min | 96516 / 89534 | 85.6 / 144.1 | 154.9 / 260.9 | —— | 0.4 / 48.2 | 54.9 / 63.6 | 0.0 | 177 / 11 | 44.3 |
| 80–90 min | 97076 / 92437 | 88.2 / 146.1 | 153.9 / 260.8 | —— | 0.4 / 48.7 | 54.5 / 63.3 | 0.0 | 181 / 10 | 44.8 |
| 90–100 min | 99629 / 94976 | 90.1 / 153.8 | 158.2 / 262.5 | —— | 0.4 / 50.9 | 55.2 / 64.8 | 0.3 | 188 / 11 | 44.8 |
| 100–110 min | 103379 / 97297 | 93.8 / 157.5 | 160.9 / 269.5 | —— | 0.5 / 52.3 | 55.0 / 63.4 | 6.0 | 176 / 10 | 45.2 |
| 110–120 min | 107016 / 98072 | 94.3 / 160.5 | 160.7 / 274.5 | —— | 0.5 / 53.3 | 54.2 / 63.8 | 7.4 | 178 / 11 | 45.2 |

各项 p50 对块数的回归（60 s 粒度，ms / 千块，括号内为 r）：

| 指标 | long11 全程 | long11 前 60 min | long11 后 60 min | long10（60 min） | long9 全程 |
|---|---|---|---|---|---|
| restore `total` | 0.296（0.90） | 0.255（0.84） | 0.744（0.87） | 0.268（0.86） | 1.190（0.99） |
| restore `total` − `conntrack`（逐条相减） | 0.283（0.92） | —— | —— | —— | 1.190（0.99） |
| `assemble_view_pre` | 0.003（0.99） | 0.003（0.99） | 0.003（0.95） | 0.003（1.00） | 0.686（0.99） |
| `disk_view_read` | 0.002（0.99） | 0.002（0.99） | 0.002（0.96） | 0.002（1.00） | —— |
| materialize + fc_rollback | 0.183（0.85） | 0.286（0.97） | −0.117（−0.39） | 0.290（0.96） | 0.278（0.93） |

这两张表的读法在[第 21 章 §7.4](21-concurrency-and-longrun.md#74-滚动保留-10一小时与两小时)与[第 21 章 §7.5](21-concurrency-and-longrun.md#75-还没有饱和的量)。

### 10.4 串口段

最终二进制，单沙箱 × 10 段 × 约 189 s，共 1888 s：

| 项 | 结果 |
|---|---|
| restore 逐项一致 | 764 + 755 + 704 + 790 + 728 + 759 + 710 + 724 + 739 + 710 = 7383，0 不一致 |
| 串口卡死 | 10 段全部没有 |
| 慢段、停顿 | 0 |
| 串口行间隔中位 / 最大 | 0.29–0.33 s / 1.595 s，没有 > 2 s 的缺口 |
| guest 命令最长一次 | 1.52 s（负载本身的命令时长） |

旧版本 Firecracker 曾出现"restore 之后 guest 串口卡死"（根因是中断控制器的 active 位残留，已在 Firecracker 侧修复，机制见 [第 9 章 §3.7](09-in-place-rollback.md#37-阶段-6中断控制器)）。
这 10 段确认部署件里的 Firecracker 在 `8ea5322bf` 的 orchestrator 下没有回退。

### 10.5 T36 全规模

crtest 的 T36 是另一套混合稳态（16 沙箱 × 1800 s，场景含 checkpoint、restore、delete、写文件、list、长命令），
判据与驱动都独立于 D 段，用来交叉验证。最终版：78695 次操作 0 失败、0 不一致，426 条断言通过。分场景（客户端，p50 / p99 / max，ms）：

| 场景 | 修复前（RPM，long2 时段） | 最终版 |
|---|---|---|
| restore | 60 / 221 / 1122（16298 次） | **43 / 62 / 287**（16658 次） |
| checkpoint（脚本场景名 `create`） | 26 / 138 / 1706 | 22 / 31 / 741 |
| delete | 4 / 57 / 209 | 11 / 37 / 422 |
| write | 75 / 112 / 706 | 73 / 88 / 653 |

一位小数的原值（ms）：最终版 restore 42.9 / 62.5 / 287.2、checkpoint 21.9 / 30.6 / 741.0、delete 11.2 / 37.0 / 422.5、write 73.2 / 88.4 / 652.9、list 1.1 / 2.3 / 230.4、exec 1512.6 / 1518.7 / 2085.1；修复前 restore 60.1 / 221.1 / 1121.8、checkpoint 26.1 / 137.8 / 1706.1、delete 4.0 / 57.0 / 208.8。

T36 的 restore 次数（16658）不计入 [§10.1](#101-731877-次-restore0-不一致) 的 731877。

### 10.6 部署后无上限并发

结果表和读法在[第 21 章 §4.6](21-concurrency-and-longrun.md#46-t36-全规模与部署后回归)。部署后的功能回归见 [§5](#5-部署后回归)。

## 11. 已知、未解决

| 项 | 现状 | 处理 |
|---|---|---|
| delete 同步合并 | delete p50 约 10 ms（上限 60）/ 约 26 ms（滚动 10），p99 基本不变 | 先不改；需要时改后台异步合并 |
| `conntrack_bg` 贴近关键路径 | p50 74.9 ms（上限 60）/ 52–55 ms（滚动 10 第一个小时），宿主表约 1.9 万条；冻结窗口 p50 60–80 ms | 下一个优化候选；可先按生产数据上调单地址阈值 |
| 第二个小时宿主 conntrack 表上涨，推高 restore（long11） | 宿主表段中位数前 80 min 约 1.9 万条，最后 20 min 约 2.38 万条，峰值 26645；`conntrack_bg` p50 52.6 → 69.4 ms，冻结内等待 p50 第 100 min 前不超过 0.3 ms、之后 6.0–7.4 ms；restore p50 第二个小时 85.4 → 94.3 ms，主要来自这里。**根因已查明**：宿主表约 95% 是非沙箱条目（lo:80 的 `TIME_WAIT`、本机 DNS），沙箱条目没有积压；宿主侧清扫每批整表 dump，代价跟着整张表走；上涨来自本机 DNS 的 `ASSURED` 条目堆积（机制为推断，未抓包）。45 min 复现 55777 次 restore 0 不一致 | 方案 A（沙箱连接打 ct mark、dump 时让内核按 mark 过滤，预期清扫约 9–10 ms、`conntrack_bg` 约 20 ms）已设计，当前不实施（[附录 C §3.6](C-evolution-and-cases.md#36-conntrack-清理占了冻结窗口) 末） |
| restore 随回滚量增长 | 滚动负载 60 min 内 `materialize_read_mb` 45 → 58 MB，写回滚内存约 0.65 ms/MB；2 h 的 long11 里 30 min 后稳定在约 57 MB | 真实工作量，不是缺陷 |
| 视图块数仍在增长 | 上界约 16 万块；long11 2 h 末约 10.7 万块，最后 30 min 仍约 2.1 万块 / h，未饱和 | 按 0.283 ms / 千块外推到上界约再 +15 ms，待更长的长跑确认；根治需改侧车为区间编码（改盘上格式） |
| 脏页跟踪关闭时不合并 | 每个 checkpoint 都是全量根，层累积 | 设计边界；950 用 HDBSS、920B 显式开跟踪，都不走这条路 |
| 16 路首个增量 checkpoint 两种形态 | 约 450 ms 与约 100–130 ms 两簇，修复前就有 | 未找到触发条件，暂缓 |
| netns 泄漏 | 每重启一次 orchestrator 漏约 290 个 | 上游问题，靠运维清理（[第 28 章 §9](28-troubleshooting.md#9-网络槽位netns泄漏)） |
| 客户端偶发 `Bad file descriptor` | 每轮 0–2 次，服务端未收到这些请求 | 客户端连接层，只记录 |
| 并发时 GC 偶发长暂停 | ct-opt 出现过 5 次 ≥ 100 ms，此后各轮未再出现 | 未归因，只记录 |

## 12. 数据的来源与条件

原生精确增量的数据分三批，批次与条件在[第 20 章 §6.5](20-920b-performance.md#65-这些数据的条件)。950 上修复后的原生路径缺口见[第 15 章 §10](15-native-cooperation-and-cost.md#10-影响面与缺口)。

用到的脚本都在 `rollback/scripts/` 下：

| 脚本 | 测什么 |
|---|---|
| `probes/pb2.py` | 同一沙箱上三列并排：`GET /memory/dirty`、写跟踪位图、实际导出的 memfile |
| `probes/pb4.py` | 把 4 KiB 写跟踪位图按 2 MiB 归并，预测"只换判据"的导出量 |
| `probes/pb3.py` | 原生 pause 先做的那次快照是否擦掉写跟踪位图 |
| `probes/pb5.py` | 三代快照改同一个 2 MiB 块里的不同 4 KiB 页，外加一页清零，逐代另起沙箱验 md5 |
| `acceptance/native_snapshot_bench.py` | 与本方案同一张档位表跑原生路径：pause 模式（同一沙箱反复 pause → resume）与 snapshot 模式（每代持久快照、另起新沙箱恢复） |
| `benchmark/native_snapshot_tiers.py` | 每档新建沙箱、只放本档负载，测 pause / resume 与 snapshot 模式耗时，并校验内容和常驻进程 |
| crtest T39 | 做过 checkpoint / restore 的沙箱原生 pause → resume，逐项核对现场与页级自校验 |
| crtest T41 | 从不 checkpoint 的沙箱原生 pause → resume，含多轮 |

## 13. 体积：修复前后

各节的数值和讲解在[第 20 章 §6](20-920b-performance.md#6-体积修复前后)，每档独立沙箱的分档在[第 19 章 §4](19-native-pause-resume.md#4-920b-分档结果与对照)。

### 13.1 `pb2.py` 三列并排

见[第 20 章 §6.1](20-920b-performance.md#61-pb2py-三列并排)。

### 13.2 `pb4.py`："只换判据"的预测

见[第 20 章 §6.2](20-920b-performance.md#62-pb4py只换判据的预测)。

### 13.3 `native_snapshot_bench.py`：六档

见[第 20 章 §6.3](20-920b-performance.md#63-native_snapshot_benchpy六档)。

### 13.4 `native_snapshot_tiers.py`：每档独立沙箱

见[第 19 章 §4](19-native-pause-resume.md#4-920b-分档结果与对照)。这一轮跑在 [§1](#1-条件标签) 的新 920B 上，模板目录是 tmpfs，代码是 `deltabox-dev@57a30632c` 构建。

## 14. 写保护的运行期代价：平台差异

平台差异、默认策略和 `FC_TRACK_DIRTY_PAGES=false` 的实测在[第 20 章 §4](20-920b-performance.md#4-写保护的运行期代价与平台差异)。

## 15. 与 checkpoint 叠加的代价

代价表在[第 20 章 §5](20-920b-performance.md#5-与-checkpoint-叠加的代价)。

## 16. 数据出处

| 数据 | 平台与日期 | 产生它的脚本或测试 |
|---|---|---|
| [第 20 章 §6.1](20-920b-performance.md#61-pb2py-三列并排) 三列并排 | 950 2026-09-10（修复前）；920B 2026-09-10（修复前）、09-11（修复后） | `probes/pb2.py` |
| [第 20 章 §6.2](20-920b-performance.md#62-pb4py只换判据的预测) 只换判据的预测 | 920B，修复前（09-10 前后） | `probes/pb4.py` |
| [第 20 章 §6.3](20-920b-performance.md#63-native_snapshot_benchpy六档) 六档体积、pause、resume | 920B，修复前 09-11、修复后 09-14；950 修复前 2026-09-09～10 | `acceptance/native_snapshot_bench.py --mode both`（950 那一轮为修复前代码） |
| [第 19 章 §4](19-native-pause-resume.md#4-920b-分档结果与对照) 每档独立沙箱的分档与对照 | 新 920B 2026-11-06 | `benchmark/native_snapshot_tiers.py --mode both` |
| [第 13 章 §6](13-native-snapshot.md#6-成本模型) 耗时拆分 | 新 920B 2026-11-06，单样本 | Firecracker 日志、文件事件与进程退出的时间线；Firecracker 内核栈采样 |
| [附录 C §5.2](C-evolution-and-cases.md#52-第一版为什么贵) `touch1` 演进 | 920B 2026-09-11，同机同 Firecracker 四轮 | 同上，pause 模式 |
| [附录 C §5.4](C-evolution-and-cases.md#54-池本身的一次无用分配) 取缓冲的分配 | 920B 2026-09-17 | `block/page_test.go` 的 `BenchmarkGetPageBuffer` |
| [附录 C §5.5](C-evolution-and-cases.md#55-量过之后决定不做的) header 与零页、perf 采样 | 920B 2026-09-11 | 修复后 `native_snapshot_bench.py` 各代 header 与导出内容；pause 模式全档位期间的 perf |
| [第 20 章 §4.1](20-920b-performance.md#41-位图由谁供数) 关闭跟踪 | 920B 2026-09-11 | `native_snapshot_bench.py`，`FC_TRACK_DIRTY_PAGES=false` |
| [第 20 章 §5](20-920b-performance.md#5-与-checkpoint-叠加的代价) `save_live_bitmap` | 920B 2026-09-18 | 并发四段脚本 `checkpoint_concurrent.py` 的 A 段计时（**推断**：原始记录没有注明由哪个脚本产生，按全量与增量次数 21 : 42 = 1 : 2、与 A 段每个沙箱 cp0 全量、cp1 / cp2 增量的结构对得上推定） |
| [第 16 章 §12.2](16-test-methodology.md#122-跨代拼接正确证明-②-③-生效) `pb5.py` | 920B 2026-09-11 | `probes/pb5.py` |
| [第 16 章 §12.2](16-test-methodology.md#122-跨代拼接正确证明-②-③-生效) T41、[第 16 章 §12.4](16-test-methodology.md#124-与-checkpoint-叠加后导出完整证明累积位图) T39 | 920B 2026-09-29，`deltabox-dev@8ea5322` | 最终回归 func 档（`run.sh func`）的 crtest T41、T39 |
| [第 16 章 §12.4](16-test-methodology.md#124-与-checkpoint-叠加后导出完整证明累积位图) 必败对照 | 920B 2026-09-18，没有累积位图的版本 | crtest T39 场景 a |
| [第 16 章 §12.5](16-test-methodology.md#125-与本方案共存) 共存 | 920B 2026-09-14 | `acceptance/checkpoint_verify.py`、`acceptance/checkpoint_bench_v2.py` |
| [第 20 章 §2](20-920b-performance.md#2-分档基准与判定)、[§3](20-920b-performance.md#3-单沙箱串行与空闲后-restore) | 新 920B 2026-09-29，`8ea5322bf` | `bench_tiers.py`、`serial_restore.py -n 5000`、空闲后 restore 复现脚本、串口段驱动 |
| [第 20 章 §2.7](20-920b-performance.md#27-修复前的分档不进判定) | 旧 920B 2026-09-21，`4af2872c6` | `bench_tiers.py`，归档 `920b-bench-tiers-20260921/` |
| [第 21 章 §4.1](21-concurrency-and-longrun.md#41-修复前2026-09-15-四轮) A / B / D 段 | 旧 920B 2026-09-15～16 | `checkpoint_concurrent.py`，归档 `920b-concurrent-20260915/`、`920b-concurrent-b-20260916/` |
| [第 21 章 §4.2](21-concurrency-and-longrun.md#42-单沙箱串行对照) 串行对照 | 旧 920B 2026-09-16 | `checkpoint_concurrent.py --stages D --soak-sandboxes 1 --soak-seconds 420 --keep-on-failure`，归档 `920b-serial-control-20260916/` |
| [第 21 章 §8.4](21-concurrency-and-longrun.md#84-验证) A/B 实验、seg012 现场、总验收 | 旧 920B 2026-09-17～18 | A/B 驱动与现场分析，归档 `920b-r4-20260917/` |
| [第 21 章 §4.5](21-concurrency-and-longrun.md#45-两天-15-组的轮次)～[§7](21-concurrency-and-longrun.md#7-产物盘走平) | 新 920B 2026-09-28～29 | `checkpoint_concurrent.py --stages D`、`rolling_keep10.py`、各轮 `D.json` 与服务端日志分析 |
| 功能与冒烟结果 | 新 920B 2026-09-22 起 | `run.sh`，归档 `920b-runsh-results-archive-20261015/` |

## 17. 首次全量的历史成本

全量的成本与单列理由在[第 20 章 §2.6](20-920b-performance.md#26-全量-checkpoint-单列)。

## 18. 历史长测的负载、采集与执行细则

所有长跑都基于 `acceptance/checkpoint_concurrent.py` 的 D 段。

### 18.1 驱动：D 段混合稳态

构造和逐项校验的 5 个字段在[第 21 章 §1.3](21-concurrency-and-longrun.md#13-测试段与负载)与[第 21 章 §1.4](21-concurrency-and-longrun.md#14-逐项校验)。

### 18.2 三种负载 + 串口段

定义在[第 21 章 §1.3](21-concurrency-and-longrun.md#13-测试段与负载)。超过 1 h 的长跑必须把建沙箱的寿命显式设大：驱动默认的沙箱 timeout 是 3600 s（[附录 C §3.12](C-evolution-and-cases.md#312-测试侧的教训)）。

### 18.3 计时口径

长跑的主口径是**服务端 `timings_ms`**：只取驱动起止标记之间的日志行，按 300 s（30 min 的轮）或 600 s（1–2 h 的轮）分段看漂移，分位数用线性插值。为什么不用分档判定用的客户端墙钟、为什么要分段，定义与理由只在 [第 16 章 §8.5](16-test-methodology.md#85-计时口径)。

两个钟的差值见[第 21 章 §1.5](21-concurrency-and-longrun.md#15-口径版本与机器)。p50 差 2–3 ms，是网络往返和代理；p99 差得多一些，是客户端调度。
所以**长跑数字与分档判定表不能直接比**：前者是服务端、多沙箱并发；后者是客户端、单沙箱串行。

### 18.4 旁路采什么

每轮除了驱动自己的 `D.json`，还按 [第 16 章 §8.3](16-test-methodology.md#83-资源判据走平怎么判) 与 [第 16 章 §8.6](16-test-methodology.md#86-旁路取证) 采旁路数据（各路回答什么问题见那两节）。各轮实际的频率：产物盘 `df` 10 s；树监视（treemon，只读 manifest，含真实链深与每沙箱视图块数）60 s；GC 每次 GC 一行（`gctrace`，需要细分时加 `CHECKPOINT_RUNTIME_METRICS=1`）；memory cgroup 用量、`failcnt` 与 D 态线程的内核栈 5 s；宿主 `nf_conntrack_count` 5 s；截断全程计数（客户端异常类与两侧 `ReverseProxy read error`）。
此外还采集的一路是**进程资源**：10 Hz 采 orchestrator 的 RSS、线程数、`nbd_inflight`、`procs_blocked`，用来看块设备层是否积压。

并发下看 restore 读了多少，用 `materialize_read_mb`，不用整个进程的 `materialize_disk_read_mb`（原因见 [附录 C §3.5](C-evolution-and-cases.md#35-restore-读盘量指标在并发时虚高) 和 [第 16 章 §8.4](16-test-methodology.md#84-读盘量用哪个键)）。

### 18.5 一轮怎么跑

1. 记录起跑状态：二进制 sha256、orchestrator 进程的环境变量、宿主 netns 数与 nat 规则数；
2. 需要时先清理泄漏的网络槽位（每重启一次 orchestrator 会漏约 290 个，[第 28 章 §9](28-troubleshooting.md#9-网络槽位netns泄漏)），
   再带本轮开关重启 orchestrator，跑一遍 smoke 确认栈正常；
3. 起旁路采样，再起驱动；
4. 结束后停旁路，按固定脚本出本轮分析，与上一轮并排；
5. 收尾恢复原栈，再跑一遍 smoke。

起跑条件（尤其 netns 数、cgroup 起始用量）会影响尾延迟，所以每轮的起跑 netns / nat 数都记在分析里；
跨轮对比时，起跑条件不同的只看量级和趋势。
