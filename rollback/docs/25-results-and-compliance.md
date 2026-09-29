# 25 · 实测结果与判定

> 给评审、客户和要复测的人看。全书的实测数字只放在这一篇。读完能知道：当前代码在 920B 上功能验到了哪一步、
> 各改动量档位对照 checkpoint ≤ 200 ms / restore ≤ 100 ms 达没达标、并发长跑下正确性与资源是否稳定，
> 以及还有哪些已知项和缺口。口径定义见 [24](24-performance-methodology.md)，各脚本的判据见 [23](23-testing-and-functional-verification.md)。
>
> 本篇数据全部来自 **`deltabox-dev@8ea5322bf`** 在 920B 上的测试（2026-09-28 ～ 09-29），原始数据保存在测试工作区。
> 少数用例跑在稍早的 `3539c45df` 上，会单独注明；两者之间只差三处改动：rootfs 视图装配改用位图、字节上限拒绝信息里补充"删哪个能腾出空间"、
> 新增一个计时键，均不涉及那些用例所测的功能路径。§2.4 是 RPM 部署版（同一代码）装机之后的回归。

---

## 1. 条件标签

| 项 | 值 |
|---|---|
| 机型 | 鲲鹏 920（Kunpeng 920 7280Z），2 路，160 物理核 / 320 线程，4 个 NUMA 节点 |
| 内存 | MemTotal 1055027476 kB（约 1 TiB） |
| OS / 内核 | openEuler 24.03 LTS-SP4，`6.6.0-159.4.3.154.oe2403sp4.aarch64` |
| 产物盘 | 根分区 `/dev/mapper/openeuler-root`，**ext4 真盘**，4.3T；产物目录 `/orchestrator/build/checkpoints` 在根分区上 |
| 脏页后端 | **`kvm-wp`**（KVM 软件写保护；920B 没有 HDBSS，orchestrator 显式设 `FC_TRACK_DIRTY_PAGES=true`）。每个干净页的第一次写都要陷出一次虚机，这笔开销含在下面每个数字里 |
| orchestrator | `deltabox-dev@8ea5322bf` 构建，与它拉起的 FC 同在一个上限 256 GiB 的 memory cgroup 里（cgroup v1；页缓存记在这个账上，guest 内存走 2 MiB 大页、不记账，[21 §8](21-state-concurrency-durability.md#8-节点级效应)） |
| Firecracker | 部署件自带 v1.13.1（sha256 `18f3faa7f47c173a5f47bfc7f578f5073cfbde2ac9bdb50bf88c434341d6d4c9`） |
| guest 内核 | 6.1.158 |
| 模板 | `base`：2 vCPU / 2048 MB，rootfs 940 MiB（[23 §8](23-testing-and-functional-verification.md#8-测试环境与沙箱规格)） |

机型、内存、内核、产物盘用 `lscpu`、`/proc/meminfo`、`uname -r`、`df -hT /`、`findmnt -T /orchestrator/build/checkpoints` 核对。
**这些数字不能当作 950 的指标**：950 用 HDBSS，脏页后端、机型、盘都不同（[24 §2](24-performance-methodology.md#2-条件标签)）。

---

## 2. 功能结果

### 2.1 总表

| 项 | 结果 |
|---|---|
| `run.sh smoke` | 开发态部署上 4 / 5：`checkpoint_verify.py` **59 / 59**；宿主预检 FAIL 0；capabilities 行在；FC sha256 与上表一致。未过的一项是 SDK 覆盖层自检：当时的判据要求"恰好 8 个异常子类"，而本期新增了第 9 个（`CheckpointBytesLimitException`）。判据改为"原有 8 个都在、映射表全是子类"之后，在 RPM 部署版上重跑 **5 / 5**（§2.4） |
| `run.sh func` | **PASS 17 · FAIL 0 · SKIP 4**（SKIP 的 T18 / T21 / T37 / T38 需要重启或注入，单独跑，见 §2.3）；RPM 部署版上重跑结果相同（§2.4） |
| crtest T36 全规模（16 沙箱 × 1800 s） | 78695 次操作 0 失败、0 不一致，426 条断言通过 |
| 重启与注入类（`3539c45df`） | 10 项全过 |
| FC Rust 单测 | **1554 passed / 0 failed**（三遍：`--all` 835、`--all --examples` 3、带 `rollback-fault-inject` 特性 716） |
| Go 单测与属性测试 | `internal/checkpoint` 等包 `-race` 通过；本机固有的几项环境性失败（cgroup 只读、UFFD API、需拉容器的包）与改动前基线完全相同 |

FC 单测那次跑的源码早于 `8ea5322bf`；此后 FC 目录只多了测试内核文件和一条新单测（`vm.rs` 的
`test_full_snapshot_sidecar_is_all_ones`，单独跑过、通过），FC 产品代码没有变化。

### 2.2 func 档逐项

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

### 2.3 重启与注入类

每项带对应开关重启 orchestrator 后跑（`3539c45df`）：

| 用例 | 开关 | 通过断言 |
|---|---|---|
| T18 重启后账本不跨重启 | —— | 16 |
| T37 `disk_full` | `CHECKPOINT_MIN_FREE_BYTES` 设为大于盘容量 | 16 |
| T37 `too_many` | `CHECKPOINT_MAX_PER_SANDBOX=2` | 24 |
| T21 `envd_timeout` / `torn_assemble` / `seal_move` / `commit_late` | `CHECKPOINT_FAULT_INJECT=<名字>` | 9 / 9 / 7 / 8 |
| T21 `seal_move:once` / `commit_late:once` | 同上，只炸一次 | 8 / 11 |
| T38 FC 过提交点后失败 | 带 `rollback-fault-inject` 特性的 FC + `FC_ROLLBACK_FAULT_INJECT=post_commit` | 13 |

以上 10 项跑在 `3539c45df` 上；它与 `8ea5322bf` 的差别见篇首，不涉及这些用例测的重启、配额与故障注入路径。

### 2.4 部署后回归（RPM 部署版）

用同一代码出的 RPM 按部署文档重新部署之后（orchestrator、Firecracker、SDK 覆盖层都换成部署件；2026-09-29），在同一台 920B 上重跑：

| 项 | 结果 |
|---|---|
| `run.sh smoke` | **PASS 5 · FAIL 0 · SKIP 0**：宿主预检 PASS 15 / WARN 4 / FAIL 0；SDK 覆盖层自检通过（按新判据）；capabilities 行在，`compact=true`、`compact_max_per_op=8`、`max_checkpoint_bytes_per_sandbox=0`、`debug_index=false`；FC sha256 两处都是 `18f3faa7…`；`checkpoint_verify.py` 59 / 59 |
| `run.sh func` | **PASS 17 · FAIL 0 · SKIP 4**（跳过的仍是 T18 / T21 / T37 / T38） |
| 开发态 `correctness.py`（断言已按合并（compact / fold）语义更新） | **ALL PASS**：各分支 restore 后内存、磁盘、blob 三项都回到目标时刻；人为移走一份 sidecar 时 restore 被拒、沙箱状态不变且照常运行，放回后又能恢复 |
| 16 沙箱 × 300 s 混合并发，**不设个数上限** | restore **8594 / 8594** 逐项一致；checkpoint 12183 次、delete 3632 次全部成功；结束时 16 / 16 沙箱可用。服务端 restore `total` p50 / p99 = **81 / 958 ms**（线性插值；按本篇 §3 的最近秩法 p99 为 963 ms），`frozen` p50 72.6 ms；每沙箱同时存在的 checkpoint 数最大 **586** |

最后一行是**无上限**负载：不设每沙箱个数上限，驱动只随机删、不按"保留最新 N 个"收敛，链深涨到几百，页缓存与回收压力都远大于常规用法。
它的尾部与修复前同类无上限负载（30 min，服务端 restore p99 1.1–1.3 s）属于同一档，只说明极限负载下正确性不受影响，不代表常规负载的耗时；
常规负载看 §5 里每沙箱上限 60 的 30 min 长跑。

---

## 3. 分档基准与判定

**条件**：§1 · 单沙箱串行 · 页缓存全热（1040 次 restore 的 `materialize_disk_read_mb` 与 `fc_rollback_disk_read_mb` 全为 0）·
`bench_tiers.py` 全表 3 遍、1040 次迭代全部 `mem_mode=incremental`、现场不一致 0、离群 0、三遍之间冻结窗口 p50 比值 0.95–1.05。
数字是**客户端墙钟**（ms），p99 取 `analyze.py` 的最近秩法，n ≤ 100 时即最大值（[24 §1.3](24-performance-methodology.md#13-分位数怎么算)）。

### 3.1 混合档（内存:文件 = 3:1）

| 名义改动 | n | checkpoint p50 | p99 | restore p50 | p99 | 内存差分 MB | 盘层 MB | 判定（p50 / p99） |
|---|---|---|---|---|---|---|---|---|
| 0 MB | 100 | 14.9 | 19.5 | 18.1 | 23.8 | 19.38 | 0.71 | ✓ / ✓ |
| 4 MB | 100 | 16.2 | 23.2 | 19.0 | 24.8 | 24.54 | 1.71 | ✓ / ✓ |
| 8 MB | 100 | 17.1 | 22.7 | 19.7 | 25.2 | 28.27 | 2.70 | ✓ / ✓ |
| 16 MB | 100 | 19.2 | 23.5 | 21.4 | 27.2 | 38.72 | 4.72 | ✓ / ✓ |
| 32 MB | 100 | 23.3 | 29.1 | 25.1 | 31.1 | 54.80 | 8.73 | ✓ / ✓ |
| 64 MB | 100 | 34.4 | 40.4 | 30.8 | 36.3 | 85.85 | 16.74 | ✓ / ✓ |
| 128 MB | 60 | 55.9 | 63.3 | 43.6 | 49.5 | 153.32 | 32.78 | ✓ / ✓ |
| 192 MB | 60 | 77.1 | 83.0 | 55.1 | 63.0 | 220.42 | 48.81 | ✓ / ✓ |
| 256 MB | 60 | 97.3 | 118.7 | 66.1 | 71.8 | 285.24 | 64.82 | ✓ / ✓ |
| 512 MB（极限档） | 30 | 176.7 | 183.9 | **108.2** | **113.0** | 548.68 | 128.96 | checkpoint ✓ / ✓；**restore ✗ / ✗** |
| 全量（每遍首次，单列不判定） | 3 | 服务端 total 433–445，墙钟 630–670 | —— | —— | —— | 2048 | —— | 不参与判定 |

"内存差分"大于名义内存份，是因为文件那份经过 guest 页缓存，同时进了内存差分（[24 §3](24-performance-methodology.md#3-负载构造三种失真)）。

### 3.2 附表：纯内存 / 纯文件 / 只读

| 档 | n | checkpoint p50 / p99 | restore p50 / p99 |
|---|---|---|---|
| 纯内存 4 / 16 / 48 / 128 MB | 各 30 | 15.4 / 21.2、16.1 / 21.5、18.3 / 20.8、23.6 / 24.3 | 19.6 / 25.7、20.8 / 26.5、26.6 / 31.7、40.3 / 49.4 |
| 纯文件 4 / 16 / 64 MB（写后 `sync`） | 各 30 | 18.8 / 24.7、32.2 / 38.1、82.2 / 91.1 | 20.1 / 24.7、23.4 / 28.2、33.1 / 41.6 |
| 只读触碰 192 MB | 20 | 15.4 / 19.7 | 19.2 / 24.1 |

只读 192 MB 与 0 MB 档几乎相同：读不算脏（[13](13-dirty-page-tracking-and-hdbss.md)）。

### 3.3 拟合与碰线点

混合档名义改动 ≤ 256 MB，以单次迭代为样本最小二乘（n = 780，客户端墙钟）：

- checkpoint ≈ **14.72 + 0.3228 × MB** ms（R² 0.9919）
- restore ≈ **19.12 + 0.1876 × MB** ms（R² 0.9810）

反推碰线：checkpoint 到 200 ms 约在 **573.9 MB**，restore 到 100 ms 约在 **431.0 MB**。
用这两条线外推 512 MB 档，与实测 p50 相差 −1.8% / −6.1%。

### 3.4 判定

| 操作 | 结论（920B · kvm-wp · 单沙箱 · 热缓存） |
|---|---|
| 增量 checkpoint ≤ 200 ms | 全部 18 档 p50 与 p99 都达标，含 512 MB 极限档 |
| 原地 restore ≤ 100 ms | 除 512 MB 极限档外 17 档 p50 与 p99 都达标；512 MB 档 p50 108.2 ms 超线。按拟合，改动量约 430 MB 以内达标 |

---

## 4. 单沙箱串行与空闲后 restore

### 4.1 串行 restore 5000 次

一个沙箱、一个调用方、同一个 checkpoint 连回 5000 次（现场内存 16 MB + 文件 4 MB，每 10 次抽验一次）：

| 指标 | 值 |
|---|---|
| 客户端墙钟 p50 / p99 / 最大 / 最小 | 9.58 / 18.2 / 21.7 / 8.35 ms |
| 服务端 `frozen` p50 / p99 | 6.03 / 13.6 ms |
| 服务端 `total` p50 / p99 | 8.75 / 17.3 ms |
| 每 1000 次分段 p50 | 9.57 / 9.62 / 9.52 / 9.57 / 9.63 ms，无漂移 |
| 异常 | 0 |

### 4.2 空闲后 restore

同一沙箱串行，循环"空闲 T 秒 → checkpoint cpB → restore cpA → 删 cpB"（测法见 [24 §5](24-performance-methodology.md#5-空闲后-restore-与串口的专项测法)），客户端墙钟 ms：

| 臂 | n | restore p50 | > 100 ms | cpB checkpoint p50 |
|---|---|---|---|---|
| 空闲 0 s | 30 | 15.4 | 0 / 30 | 10.6 |
| 空闲 0.5 s | 30 | 19.0 | 0 / 30 | 12.8 |
| 空闲 2 s | 30 | 23.2 | 0 / 30 | 75.9 |
| 空闲 5 s | 30 | 25.1 | 0 / 30 | 77.9 |
| 空闲 10 s | 20 | 29.1 | 0 / 20 | 66.8 |
| 空闲 2 s 后**不拍** cpB 直接 restore | 30 | 80.9 | 0 / 30 | —— |
| guest 内常驻忙循环 | 30 | 27.1 | 0 / 30 | 76.3 |

"不拍 cpB 直接 restore"那一臂的服务端分段：`total` 77.8、`frozen` 73.7、其中 FC 的 `fc_quiesce` 47.1 ms（其他臂 `fc_quiesce` 为 0）。

### 4.3 串口

10 段 × 180 s 单沙箱负载，restore 7383 / 7383 成功且逐项一致；每段串口未卡死、无停顿、无 > 2 s 的串口行缺口，
串口行间隔最大 1.595 s。

---

## 5. 长跑与并发（摘要）

16 个沙箱并发、服务端计时（[24 §6](24-performance-methodology.md#6-并发与长跑的测量)）。"修复前"是同一台机器上尚未包含相应修复的版本，
各行的对照版本不完全相同；其中 GC 停顿与流截断两行的修复前数字取自不设上限的极限压测（链深上千），其余取自上限 60 的常规负载。
长跑与并发的数字都是服务端 `timings_ms.total`。原始数据保存在测试工作区。

| 指标 | 修复前 | 当前代码 |
|---|---|---|
| restore 逐项一致（两天 15 组长测与串口段合计） | —— | **731877 / 731877，0 不一致** |
| 常规负载（上限 60，30 min）restore p50 / p99 | 193 / 338 ms | **88 / 171 ms** |
| 常规负载 checkpoint p50 / p99 | 34 / 128 ms | **30 / 71 ms** |
| 30 min 产物盘增长 | 340.6 GiB 且仍在涨 | **约 90 GiB 后走平**（峰值 90.3 GiB，后 15 min 斜率 −0.13 GiB/min） |
| 目录数与真实链深（上限 60） | 目录 3943 且在涨 | 目录约 1240 走平，真实链深 13–19 |
| GC 最长停顿 | 609 ms；≥ 100 ms 的 180 次 | **2.8 ms**；≥ 10 ms 的 0 次 |
| 流式命令截断（16 沙箱 30 min） | 5–10 次 | **0**（orchestrator 侧） |
| `assemble_view_pre` p50 / p99（上限 60） | 8.98 / 39.26 ms | **0.31 / 2.97 ms** |
| 滚动保留 10、第 50–60 min 的 restore p50 | 141.9 ms | **84.1 ms** |

几点说明：

- **731877** 只累加各组长测 `D.json` 与串口段的逐项校验数，不含 T36、分档基准里的 restore。
  T36 全规模另计：78695 次操作，其中 restore 16658 次，0 失败。
- **磁盘走平**来自两件事：隐藏 checkpoint 合并进唯一子节点（条目数有上界 2V+1，[14](14-memory-diff-tree.md)），
  以及 rootfs 层按视图引用计数回收（[16](16-disk-layering.md)）。上限 60 的最终长测里合并 20121 次、全部连层合并。
- **GC 停顿**的根因是 block 层在文件映射上拷贝时缺页撞上 memory cgroup 回收；改为拷贝前在系统调用里预取页之后消失（[21](21-state-concurrency-durability.md)）。
- **截断归零**的是与 restore 无关的流截断（代理未开 full duplex）；**restore 造成的流截断仍是既定限制**（[03](03-errors-timeouts-concurrency.md)）。
- **视图块数增长**：guest 里 ext4 每次截断重写小文件都分配新块，所以视图里"guest 写过的块"只增不减；
  滚动保留 10 的 2 h 长跑中每沙箱底层块均值从约 4.1 万涨到约 9.4 万（设备的 16.9% → 39.3%），上限约 16 万块。
  修复前 restore 随块数线性变慢（斜率 1.19 ms / 千块）；AssembleView 改用按块号的位图、按区间批量认领之后，
  restore 对块数的斜率降到 0.27 ms / 千块，`assemble_view_pre` 在 0.3–0.4 ms、不再随块数涨。
  真正随块数增长的只剩读各层块清单（`disk_view_read`）：长跑中 p50 0.2–0.3 ms，基准测试里 1 万块 56 µs、10 万块 462 µs、20 万块 950 µs，
  到块数上限也在 1 ms 量级。那 0.27 ms / 千块的剩余斜率主要来自同期变大的回滚量（见 §7 第 5 条），不是块数本身。
- 最终长测（上限 60）其他服务端数字：`delete.total` p50 9.51 ms（含同步合并）、checkpoint `total` 最大 783.5 ms
  （>400 ms 的 16 次全是起跑时 16 个沙箱的首次全量 checkpoint），restore `total` 无 >400 ms。

---

## 6. 950

本期代码在 950（HDBSS）上的**性能数据待补**。正确性结论可以从 920B 搬过去（两种脏页后端给上层的是同一张位图），性能数字不可以。

---

## 7. 已知项与缺口

| # | 项 | 现状 |
|---|---|---|
| 1 | delete 同步做合并 | 服务端 delete p50 从约 2 ms 升到约 10 ms（上限 60）/ 约 26 ms（滚动保留 10），客户端墙钟 p50 从约 4 ms 升到约 12 / 27 ms，p99 基本不变。需要时可改后台异步合并 |
| 2 | 空闲后不先 checkpoint 直接 restore | p50 80.9 ms，大头在 FC 的 `fc_quiesce`（47.1 ms）；空闲 ≥ 2 s 后的 checkpoint 也升到 67–78 ms。根因未查，只作记录 |
| 3 | 16 路并发的首个增量 checkpoint 有两种形态 | p50 约 450 ms 一簇与约 100–130 ms 一簇都出现过，修复前就存在，同一二进制两次运行可分属两簇，未找到稳定触发条件 |
| 4 | conntrack 后台清扫贴近关键路径 | `conntrack_bg` p50 在滚动负载下 52–55 ms、上限 60 负载下 74.9 ms（宿主表约 1.9 万条），已接近冻结窗口；下一个优化候选 |
| 5 | 滚动负载下 restore 随回滚量增长 | 60 min 内 `materialize_read_mb` p50 44 → 57 MB，写回滚内存约 0.65 ms/MB，restore p50 71 → 84 ms。guest 后台写让回滚量真实变大，是工作量不是缺陷 |
| 6 | 脏页跟踪关闭时不合并 | 每个 checkpoint 都是全量根，没有"隐藏且只有一个子节点"的条目，层照样累积。950 用 HDBSS、920B 显式开跟踪，都不走这条路 |
| 7 | 每重启一次 orchestrator 泄漏约 290 个 netns | 上游问题，靠运维清理（[08](08-troubleshooting.md)） |
| 8 | 撕裂之后 SDK 的 `is_running()` | 两次运行分别得到 `TimeoutException` 和 `False`，用例注释预期为真；只记录，不判定 |
| 9 | 客户端偶发 `ReadError: [Errno 9] Bad file descriptor` | 每轮 0–2 次，服务端没收到这些请求，在客户端 httpx 连接层；只记录 |
| 10 | 非 restore 的流截断修复要两份代理都带上 | orchestrator 的沙箱代理与 client-proxy 共用同一段代理代码，部署件里两者都要是修复后的版本（[03](03-errors-timeouts-concurrency.md)） |
| 11 | 字节配额默认关 | 是否在交付件里默认开、开多大待定；crtest 没有对应用例，端到端验证只在工作区脚本里做过 |
| 12 | SDK 覆盖层自检判据已改 | 已在 RPM 部署版上重跑 smoke，5 / 5（§2.4） |
| 13 | 开发态工具 | `correctness.py`（已按合并语义更新）已在 RPM 部署版上跑过，ALL PASS（§2.4）；`pause_verify.py`、`compat_matrix.py`、`loop.py`、`timing.py` 未在本期代码上跑 |
| 14 | 950 上的全部性能数据 | 分档基准、串行长尾、空闲后 restore、并发长跑都待补 |
| 15 | HDBSS 与软件写保护的收益对照 | 要在同一台 950 上各跑一遍，其余条件不动；跨机相减得不到它 |
| 16 | 950 上的 HDBSS L3 证据落盘 | `hdbss_evidence.py` 的冷/热比待在 950 上留档 |
| 17 | `FC_HDBSS_ORDER` 取值对比 | 写密集负载下 HDBSS buffer 溢出会吃掉多少收益，未测 |
| 18 | 跨树回滚 | 有单元测试（丢失 epoch 后跨树 restore 三种 sidecar 形态），无端到端用例 |
| 19 | HDBSS 在 vCPU 创建之后武装 | 只靠调用点位置保证，没有断言 |
| 20 | 条件标签留档 | 每轮必须记全机型、脏页后端、产物盘、模板、二进制 sha、脚本参数；漏记的数据事后补不上 |

