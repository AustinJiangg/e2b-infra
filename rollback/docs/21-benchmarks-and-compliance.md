# 21 · 分档基准与判定

## 本章目标

读完本章，你应该能：

1. 用"固定开销 + 与改动量成正比"的成本模型，估出自己业务里一次 checkpoint、一次 restore 大致要多久；
2. 读懂分档判定表：当前代码在 920B 上哪些档位达标、哪一档不达标、按拟合在多大改动量碰线；
3. 说清为什么每个沙箱的第一次（全量）checkpoint 单列、不参与判定；
4. 知道功能验证做到了哪一步，包括部署件装机之后的回归结果；
5. 知道还有哪些已知的长尾和缺口，950 上的数据为什么待补。

上一章（[20](20-performance-methodology.md)）把客户的两个数字（checkpoint ≤ 200 ms、restore ≤ 100 ms）翻译成了可测的口径，也讲了负载怎么造才不失真。
本章给出按这套口径测出来的数：先用一节把结论讲完，再依次给条件标签、功能验证结果、分档基准与判定、单沙箱串行与空闲后 restore 的专项数字，
最后是已知项与缺口。多沙箱并发、长时间运行的实测单独放在下一章 [22](22-long-run-and-concurrency.md)。

本章数据全部来自 **`deltabox-dev@8ea5322bf`** 在 920B 上的测试（2026-09-28 ～ 09-29），原始数据保存在测试工作区。
少数用例跑在稍早的 `3539c45df` 上，会单独注明；两者之间只差三处改动：rootfs 视图装配改用位图、字节上限拒绝信息里补充"删哪个能腾出空间"、
新增一个计时键，均不涉及那些用例所测的功能路径。§2.4 是用 RPM 部署件装机之后的回归，其中一次用的是更新的 `93ccb02`，差别在那一节写明。
口径的定义只在 [20](20-performance-methodology.md)，本章不重复。

---

## 结论先行

### 成本模型

checkpoint 与 restore 的耗时都是"固定开销 + 与改动量成正比的一项"，与虚机内存有多大基本无关：

```
checkpoint ≈ a₁ + b₁ × 自上一个点以来改过的数据量（内存脏页 + 磁盘写入）
restore    ≈ a₂ + b₂ × 回滚集大小（当前状态与目标之间，沿树路径所有被改过的页）
```

为什么是这个形状：增量 checkpoint 只写出本代的脏页和本代封的磁盘层（[06](06-memory-diff-tree.md)、[07](07-disk-layering.md)），
restore 只回写回滚集里的页（[06](06-memory-diff-tree.md)、[09](09-in-place-rollback.md)），两者的工作量都与"改了多少"成正比；
固定开销是暂停与恢复虚机、Firecracker 的 API 往返、写位图侧车、换层、清连接跟踪表这些与改动量无关的步骤。

- 系数 a、b 随平台（脏页跟踪后端、存储介质）而不同。920B 上的拟合值见 §3.3：checkpoint ≈ 14.72 + 0.3228 × MB，restore ≈ 19.12 + 0.1876 × MB（客户端墙钟，ms）。
  直观地说，在 920B 上每多改 100 MB，checkpoint 多约 32 ms、restore 多约 19 ms；
- **每个沙箱的第一次 checkpoint 是全量**，耗时正比于 guest 内存大小，每个沙箱（每一代）只付一次，不参与判定（见下文"全量为什么单列"）；
- restore 另有一项随沙箱磁盘视图里被写过的块数增长的开销（读各层的块清单），块数涨到上界也在 1 ms 量级；
  长跑中 restore 随时间缓慢变慢，主要来自同期真实变大的回滚量，不是块数本身（[22 §4.9](22-long-run-and-concurrency.md#49-视图块数增长restore-随时间变慢)）；
- 与树的分支多少无关；"保留最新 N 个、删最旧的"用法下，树和磁盘都会走平，耗时不随运行时间持续上涨（[24 §4.2](24-semantics-and-limits.md#42-保留最新-n-个删最旧的会收敛)）。

对业务的直接含义：**改得少，回退就快**。在 Agent 的一步里只改了几十 MB 的沙箱，checkpoint 和 restore 都在几十毫秒量级；
一步改了几百 MB，耗时相应线性变长。

### 全量为什么单列

树根必须自包含，否则一条链没有可解析的底（[06 §8](06-memory-diff-tree.md#8-全量与增量怎么选)）。它的代价是写出整份 guest 内存：
920B 上 2 GB 模板服务端 433–445 ms、客户端墙钟 0.63–0.67 s，比 0 MB 档的增量（p50 14.9 ms）高一个数量级以上。

但它**每个沙箱只发生一次**：一个沙箱做 100 次 checkpoint，这约 0.65 s 摊下来每次约 6.5 ms，而且不在"回退到上一步"这个动作的路径上。
把它算进判定，得到的数字既不反映客户体验，也不反映实现质量。单列的另一个作用是当**分母**：没有这一行，"增量比全量快多少"这句话没法说。

### 判定口径（一句话）

客户参考指标是 **增量 checkpoint ≤ 200 ms、restore ≤ 100 ms**，按**客户端墙钟**判定，p50 判定、p99 并列，
按**每步改动量分档**（不按沙箱内存大小），全量根单列不判定；完整定义见 [20](20-performance-methodology.md)。

### 判定结论

**920B（kvm-wp 软件写保护）**。条件：`FC_TRACK_DIRTY_PAGES=true`（KVM 写保护跟踪脏页），模板 2 vCPU / 2 GB，单沙箱串行、页缓存全热：

| 档位 | checkpoint（≤ 200 ms） | restore（≤ 100 ms） |
|---|---|---|
| 混合档（内存 : 文件 = 3 : 1），每步改动 0–256 MB 各档 | p50、p99 全部达标 | p50、p99 全部达标 |
| 纯内存档、纯文件档、只读触碰档 | 全部达标 | 全部达标 |
| 512 MB 极限档（拟合锚点，不是常规用法） | 达标 | **不达标**（p50 108.2 ms、p99 113.0 ms） |

按拟合外推，随着每步改动量增大，**restore 先于 checkpoint 碰线**：restore 约在 431 MB、checkpoint 约在 574 MB。逐档数据见 §3。

多沙箱并发时，单次操作比单沙箱串行慢：16 个沙箱持续并发、每沙箱上限 60 个 checkpoint 的 30 min 长跑里，
服务端 restore 的 p50 88 ms 仍在 100 ms 线内，**p99 171 ms 越过 100 ms**；不设上限、链深涨到几百的极限负载下，p99 接近 1 s。
并发负载不是客户判定口径的一部分，数据与分析见 [22](22-long-run-and-concurrency.md)。

**950（HDBSS 硬件标脏）：待补。** 本期代码尚未在 950 上跑分档基准。920B 的数字不能直接当作 950 的指标：两者的脏页跟踪后端不同
（HDBSS 由 CPU 记录脏页，省掉软件写保护在每个干净页第一次写入时的陷出），存储介质也不同（§6）。

### 对使用方式的含义

| 使用形态 | 本章的单次数字能不能直接用 | 说明 |
|---|---|---|
| 短生命周期沙箱，几十次 checkpoint 就销毁 | **能** | §3 的判定表就是这个形态 |
| 长期存活、"保留最新 N 个、删最旧" | **能，另加少量长跑开销** | 树、磁盘走平；restore 随回滚量和视图块数缓慢增长，滚动保留 10 的 1 h 长跑里服务端 p50 从 71 升到 84 ms（[22 §5.3](22-long-run-and-concurrency.md#53-滚动保留-10一小时与两小时)） |
| 长期存活、只拍不删、也不设上限 | **不能** | 链深与磁盘无界增长，restore 物化要走的链越来越长；应设 `CHECKPOINT_MAX_PER_SANDBOX` 或 `CHECKPOINT_MAX_BYTES_PER_SANDBOX`，或按"删最旧"管理（[27](27-configuration-and-capacity.md#43-数量上限怎么选)） |
| 一个节点上多个沙箱同时高频操作 | 看并发数据 | 共享的锁、宿主连接跟踪表和内存回收会把代价互相传递（[22](22-long-run-and-concurrency.md)） |

### 已知的长尾

每条一句；数字见 §7 与 [22 §6](22-long-run-and-concurrency.md#6-结论与剩余长尾)，排障见 [29](29-troubleshooting.md)。

- **每个沙箱的第一次 checkpoint 是全量**，比增量慢一个数量级以上，正比于 guest 内存。这是树根的一次性代价。
- **delete 带同步合并，比不合并时慢**：删除的结尾会同步把可合并的隐藏节点合并（compact / fold）掉（每次最多 8 个），delete 的中位耗时因此从几毫秒升到十几到二十几毫秒；p99 基本不变。
- **空闲一段时间后、不先 checkpoint 直接 restore 偏慢**：时间主要花在 Firecracker 回滚前的设备静默上；在 920B 上仍在 100 ms 线内。空闲后的 checkpoint 也比不空闲时慢（§4.2）。
- **多沙箱同时做首个增量 checkpoint，耗时偶发成两簇**：同一个二进制前后两次测量就可能分属不同的簇，稳定触发条件未找到。
- **restore 随回滚量增长**：guest 在后台持续写内存时，回滚集会真实变大，restore 随之变慢；这是真实工作量，不是缺陷。
- **宿主上连接数很多时，restore 的连接跟踪清理贴近关键路径**：后台清扫在大多数 restore 里被冻结窗口遮住，负载很重时会露出来。
- **宿主不跟踪脏页时每次都是全量**：功能正确，但每次都按全量付时间与空间，`mem_mode` 会一直是 `full`（[23 §4](23-quickstart.md#4-mem_mode这次是全量还是增量)）。

---

## 1. 条件标签

| 项 | 值 |
|---|---|
| 机型 | 鲲鹏 920（Kunpeng 920 7280Z），2 路，160 物理核 / 320 线程，4 个 NUMA 节点 |
| 内存 | MemTotal 1055027476 kB（约 1 TiB） |
| OS / 内核 | openEuler 24.03 LTS-SP4，`6.6.0-159.4.3.154.oe2403sp4.aarch64` |
| 产物盘 | 根分区 `/dev/mapper/openeuler-root`，**ext4 真盘**，4.3T；产物目录 `/orchestrator/build/checkpoints` 在根分区上 |
| 脏页后端 | **`kvm-wp`**（KVM 软件写保护；920B 没有 HDBSS，orchestrator 显式设 `FC_TRACK_DIRTY_PAGES=true`）。每个干净页的第一次写都要陷出一次虚机，这笔开销含在下面每个数字里 |
| orchestrator | `deltabox-dev@8ea5322bf` 构建，与它拉起的 FC 同在一个上限 256 GiB 的 memory cgroup 里（cgroup v1；页缓存记在这个账上，guest 内存走 2 MiB 大页、不记账，[13 §8](13-state-concurrency-durability.md#8-节点级效应)） |
| Firecracker | 部署件自带，sha256 `18f3faa7f47c173a5f47bfc7f578f5073cfbde2ac9bdb50bf88c434341d6d4c9`（核对身份以它为准）。放在 `/fc-versions/v1.13.1/` 下，目录名取自模板记录的版本；二进制自报 `Firecracker v1.12.1`，即分叉所基于的上游版本（[26 §3](26-deployment-prerequisites.md#3-版本配对)） |
| guest 内核 | 6.1.158 |
| 模板 | `base`：2 vCPU / 2048 MB，rootfs 940 MiB（[19 §8](19-testing-and-functional-verification.md#8-测试环境与沙箱规格)） |

机型、内存、内核、产物盘用 `lscpu`、`/proc/meminfo`、`uname -r`、`df -hT /`、`findmnt -T /orchestrator/build/checkpoints` 核对。
**这些数字不能当作 950 的指标**：950 用 HDBSS，脏页后端、机型、盘都不同（[20 §2](20-performance-methodology.md#2-条件标签)）。

---

## 2. 功能结果

### 2.1 总表

| 项 | 结果 |
|---|---|
| `run.sh smoke` | 开发态部署上 4 / 5：`checkpoint_verify.py` **59 / 59**；宿主预检 FAIL 0；capabilities 行在；FC sha256 与上表一致。未过的一项是 SDK 覆盖层自检：当时的判据要求"恰好 8 个异常子类"，而本期新增了第 9 个（`CheckpointBytesLimitException`）。判据改为"原有 8 个都在、映射表全是子类"之后，在 RPM 部署件上两次重跑都是 **5 / 5**（§2.4） |
| `run.sh func` | **PASS 17 · FAIL 0 · SKIP 4**（SKIP 的 T18 / T21 / T37 / T38 需要重启或注入，单独跑，见 §2.3）；RPM 部署件上两次重跑结果相同（§2.4） |
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

### 2.4 部署后回归

前面几节跑在开发态部署上（只替换 orchestrator 二进制）。交付给客户的是 RPM 部署件，所以又用同一台 920B、按部署文档完整重装了两次
（orchestrator、Firecracker、SDK 覆盖层都换成部署件），各跑一遍回归：

- **第一次（2026-09-29 上午）**：部署件由 `8ea5322bf` 构建，即本章其余各节的代码；
- **第二次（2026-09-29 下午）**：部署件由 `93ccb02` 构建。它在 `8ea5322bf` 之上多四个提交：delete 按实际情况回答
  （不再把所有错误都答成 404：条目不存在仍是 404 `not_found`；删除已生效、只是之后清理文件失败时照常返回成功并打 WARN；其他错误回 500 `internal`，
  错误码见 [25](25-errors-timeouts-concurrency.md)）；orchestrator 启动时除了打能力行，还把同样的内容写进能力文件
  `/orchestrator/build/checkpoint-capabilities.json`（能力行在高负载下会很快被日志轮转冲掉，smoke 改读这个文件，[27 §3.1](27-configuration-and-capacity.md#31-能力文件)）；
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

第一次部署后还跑了一轮 16 沙箱 × 300 s、不设个数上限的混合并发，只用来确认极限负载下部署件的正确性不受影响；它的数据只在 [22 §5.6](22-long-run-and-concurrency.md#56-部署后无上限并发)。

---

## 3. 分档基准与判定

**条件**：§1 · 单沙箱串行 · 页缓存全热（1040 次 restore 的 `materialize_disk_read_mb` 与 `fc_rollback_disk_read_mb` 全为 0）·
`bench_tiers.py` 全表 3 遍、1040 次迭代全部 `mem_mode=incremental`、现场不一致 0、离群 0、三遍之间冻结窗口 p50 比值 0.95–1.05。
数字是**客户端墙钟**（ms），p99 取 `analyze.py` 的最近秩法，n ≤ 100 时即最大值（[20 §1.3](20-performance-methodology.md#13-分位数怎么算)）。

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

"内存差分"大于名义内存份，是因为文件那份经过 guest 页缓存，同时进了内存差分（[20 §3](20-performance-methodology.md#3-负载构造三种失真)）。

### 3.2 附表：纯内存 / 纯文件 / 只读

| 档 | n | checkpoint p50 / p99 | restore p50 / p99 |
|---|---|---|---|
| 纯内存 4 / 16 / 48 / 128 MB | 各 30 | 15.4 / 21.2、16.1 / 21.5、18.3 / 20.8、23.6 / 24.3 | 19.6 / 25.7、20.8 / 26.5、26.6 / 31.7、40.3 / 49.4 |
| 纯文件 4 / 16 / 64 MB（写后 `sync`） | 各 30 | 18.8 / 24.7、32.2 / 38.1、82.2 / 91.1 | 20.1 / 24.7、23.4 / 28.2、33.1 / 41.6 |
| 只读触碰 192 MB | 20 | 15.4 / 19.7 | 19.2 / 24.1 |

只读 192 MB 与 0 MB 档几乎相同：读不算脏（[05](05-dirty-page-tracking-and-hdbss.md)）。

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

同一沙箱串行，循环"空闲 T 秒 → checkpoint cpB → restore cpA → 删 cpB"（测法见 [20 §5](20-performance-methodology.md#5-空闲后-restore-与串口的专项测法)），客户端墙钟 ms：

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

16 个沙箱并发、服务端计时、连续两天两夜的长跑与并发实测单独成章，见 [22](22-long-run-and-concurrency.md)：负载怎么造、每一轮要回答什么、
长跑暴露并修掉的每个问题的证据链与前后对比，都在那里。一句话结论：两天 15 组长跑与串口段合计 **731877 次 restore 逐项一致、0 不一致**（三种负载加串口段累加的计数，各轮负载见 22 §3；另有部署件 `93ccb02` 的 2 h 滚动长测 long11 147557 次，同样 0 不一致，见 22 §5.1）；
每沙箱上限 60 的 30 min 常规负载下，服务端 restore p50 / p99 为 88 / 171 ms、checkpoint 为 30 / 71 ms，产物盘约 90 GiB 后走平（都是这一种负载下的数，不与别的负载比）。
这些是服务端、多沙箱并发的数字，与 §3 按客户端墙钟、单沙箱串行的判定表不能直接比较。

---

## 6. 950

本期代码在 950（HDBSS）上的**性能数据待补**。正确性结论可以从 920B 搬过去（两种脏页后端给上层的是同一张位图），性能数字不可以。

---

## 7. 已知项与缺口

| # | 项 | 现状 |
|---|---|---|
| 1 | delete 同步做合并 | 服务端 delete p50 从约 2 ms 升到约 10 ms（上限 60）/ 约 26 ms（滚动保留 10），客户端墙钟 p50 从约 4 ms 升到约 12 / 27 ms，p99 基本不变。需要时可改后台异步合并（[22 §4.10](22-long-run-and-concurrency.md#410-删除同步合并delete-变慢)） |
| 2 | 空闲后不先 checkpoint 直接 restore | p50 80.9 ms，大头在 FC 的 `fc_quiesce`（47.1 ms）；空闲 ≥ 2 s 后的 checkpoint 也升到 67–78 ms。根因未查，只作记录 |
| 3 | 16 路并发的首个增量 checkpoint 有两种形态 | p50 约 450 ms 一簇与约 100–130 ms 一簇都出现过，修复前就存在，同一二进制两次运行可分属两簇，未找到稳定触发条件（[22 §4.11](22-long-run-and-concurrency.md#411-16-路并发的首个增量-checkpoint-有两种形态)） |
| 4 | conntrack 后台清扫贴近关键路径 | `conntrack_bg` p50 在滚动负载下 52–55 ms、上限 60 负载下 74.9 ms（宿主表约 1.9 万条），已接近冻结窗口。清扫代价跟着整张宿主表走，表里约 95% 是与沙箱无关的条目；long11 第二个小时本机 DNS 条目堆积把它推到 69 ms。按 ct mark 过滤的方案 A 已设计，用户 09-30 决定暂不实施（[22 §4.6](22-long-run-and-concurrency.md#46-conntrack-清理占了冻结窗口)） |
| 5 | 滚动负载下 restore 随回滚量增长 | 60 min 内 `materialize_read_mb` p50 44 → 57 MB，写回滚内存约 0.65 ms/MB，restore p50 71 → 84 ms。guest 后台写让回滚量真实变大，是工作量不是缺陷（[22 §4.9](22-long-run-and-concurrency.md#49-视图块数增长restore-随时间变慢)） |
| 6 | 脏页跟踪关闭时不合并 | 每个 checkpoint 都是全量根，没有"隐藏且只有一个子节点"的条目，层照样累积。950 用 HDBSS、920B 显式开跟踪，都不走这条路 |
| 7 | 每重启一次 orchestrator 泄漏约 290 个 netns | 上游问题，靠运维清理（[29 §9](29-troubleshooting.md#9-网络槽位netns泄漏)） |
| 8 | 撕裂之后 SDK 的 `is_running()` | 两次运行分别得到 `TimeoutException` 和 `False`，用例注释预期为真；只记录，不判定 |
| 9 | 客户端偶发 `ReadError: [Errno 9] Bad file descriptor` | 每轮 0–2 次，服务端没收到这些请求，在客户端 httpx 连接层；只记录 |
| 10 | 非 restore 的流截断修复要两份代理都带上 | orchestrator 的沙箱代理与 client-proxy 共用同一段代理代码，部署件里两者都要是修复后的版本（[25 §5.3](25-errors-timeouts-concurrency.md#53-两类流式截断要分清)、[22 §4.1](22-long-run-and-concurrency.md#41-流式命令偶发截断)） |
| 11 | 字节配额默认关 | 是否在交付件里默认开、开多大待定；crtest 没有对应用例，端到端验证只在工作区脚本里做过 |
| 12 | SDK 覆盖层自检判据已改 | 已在两次 RPM 部署件上重跑 smoke，都是 5 / 5（§2.4） |
| 13 | 开发态工具 | `correctness.py`（已按合并语义更新）已在两次 RPM 部署件上跑过，都是 ALL PASS（§2.4）；`pause_verify.py`、`compat_matrix.py`、`loop.py`、`timing.py` 未在本期代码上跑 |
| 14 | 950 上的全部性能数据 | 分档基准、串行长尾、空闲后 restore、并发长跑都待补；长跑的重测清单见 [22 §6.3](22-long-run-and-concurrency.md#63-到-950-上要重测什么) |
| 15 | HDBSS 与软件写保护的收益对照 | 要在同一台 950 上各跑一遍，其余条件不动；跨机相减得不到它 |
| 16 | 950 上的 HDBSS L3 证据落盘 | `hdbss_evidence.py` 的冷/热比待在 950 上留档 |
| 17 | `FC_HDBSS_ORDER` 取值对比 | 写密集负载下 HDBSS buffer 溢出会吃掉多少收益，未测 |
| 18 | 跨树回滚 | 有单元测试（丢失 epoch 后跨树 restore 三种 sidecar 形态），无端到端用例 |
| 19 | HDBSS 在 vCPU 创建之后武装 | 只靠调用点位置保证，没有断言 |
| 20 | 条件标签留档 | 每轮必须记全机型、脏页后端、产物盘、模板、二进制 sha、脚本参数；漏记的数据事后补不上 |
| 21 | 部署脚本的 80 → 3002 转发劫持 hyperloop 请求 | `build.sh` 装的 `nat PREROUTING --dport 80 -j REDIRECT --to-port 3002` 不限入口网卡、排在各槽位的 hyperloop 规则（→ 5010）前面，guest 发往 hyperloop 的请求全被转给 client-proxy：hyperloop 这条链路实际不通，client-proxy 在 16 沙箱负载下每秒约 900 条 `invalid host`，long11 两小时写出约 9.1 GB 日志。不影响 checkpoint / restore 与本期结果。用户 09-30 决定暂不改，改法是给该规则加 `! -i veth+`（[26 §5.1](26-deployment-prerequisites.md#51-80-端口的转发规则劫持-hyperloop-请求)） |

---

## 本章要点

- checkpoint 与 restore 的耗时都是"固定开销 + 与改动量成正比"，与虚机内存大小基本无关；920B 上每多改 100 MB，checkpoint 多约 32 ms、restore 多约 19 ms。
- 每个沙箱的第一次 checkpoint 是全量（2 GB 模板约 0.65 s 墙钟），每个沙箱只付一次，单列不判定，同时作为"增量快多少"的分母。
- 920B（kvm-wp）单沙箱串行、热缓存：checkpoint 全部 18 档 p50 与 p99 达标；restore 除 512 MB 极限档外全部达标，按拟合约 430 MB 以内达标，restore 先于 checkpoint 碰线。
- 功能：smoke、func、T36 全规模、重启与注入类、FC 与 Go 单测全部通过；RPM 部署件上两次重装回归（`8ea5322bf`、`93ccb02`）smoke 5 / 5、func 17 / 0 / 4、`correctness.py` ALL PASS。
- 单沙箱串行 restore 5000 次 p50 9.58 ms、p99 18.2 ms、无漂移；空闲后不先 checkpoint 直接 restore 偏慢（p50 80.9 ms），根因未查。
- 并发与长跑不属于客户判定口径，数字与分析在 [22](22-long-run-and-concurrency.md)；950 上的全部性能数据待补，920B 的数字不能当作 950 的指标。
