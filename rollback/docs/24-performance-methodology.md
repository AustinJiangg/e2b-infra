# 24 · 性能口径与方法

> 给要做验收、要复测、要看懂 [25](25-results-and-compliance.md) 判定表的人看。
> 读完能知道：客户的两个数字（checkpoint ≤ 200 ms、restore ≤ 100 ms）被翻译成了什么口径，
> 负载怎么造才不失真，各个基准脚本分别回答什么问题，并发和长跑是怎么测的。
> 本篇不放任何实测数字，数字全在 [25](25-results-and-compliance.md)。

---

## 1. 口径定义

### 1.1 客户给的是上限，不是口径

"checkpoint ≤ 200 ms"缺六件事：谁的钟、哪一次、哪个统计量、什么负载、什么恢复形态、什么机器。
任何一项换一种取法，同一批数据都能得出相反的结论，所以先把六项钉死，而且钉在对我们不利的一侧。
[25](25-results-and-compliance.md) 的判定表都按下表读。

| 维度 | 本书口径 | 为什么 |
|---|---|---|
| **量什么钟** | **客户端墙钟**：`time.monotonic()` 夹住 SDK 的 checkpoint / restore 调用，含网络往返、代理和服务端全部工作 | 三个钟里最大、最保守；也是客户在自己机器上唯一能复测的 |
| **哪类 checkpoint** | **增量 checkpoint**：服务端回报 `mem_mode=incremental` | 客户指标约束的是高频回退里的每一次；树根全量每个沙箱只发生一次 |
| **哪类 restore** | **原地 restore**（沙箱 ID 与 FC 进程不变）。分档基准里是"撤销本档改动"的单跳：从 cpB 回到 cpA | 一跳的代价取决于起点与终点之间差了多少，不是目标那一代多大 |
| **统计量** | **p50 判定，p99 并列**；同时给 p90 / max / n | p50 回答"通常多久"，p99 回答"最坏能不能接受"；单次会抖，不作判定 |
| **分档变量** | **名义改动量**：总改动 0 / 4 / 8 / 16 / 32 / 64 / 128 / 192 / 256 MB，按内存:文件 = **3:1** 拆；512 MB 是极限档，只作拟合锚点 | 增量的成本随"改了多少"走，不随沙箱内存大小走 |
| **样本构造** | 每遍一个新沙箱，默认 3 遍；每档样本数由档位表给出（小档密、大档稀），均摊到各遍 | 遍与遍之间可以查漂移 |
| **全量 checkpoint** | 单列，不参与判定（每遍开头那一次） | 树根要写整份 guest 内存，是每个沙箱一次性的成本，不在高频回退的路径上；但必须列出来，否则"增量快多少"没有分母 |
| **冻结窗口** | 解释项，列在墙钟旁边，不用来判定 | 它比墙钟小，拿它判定等于放水；但它才是业务感受到的停顿 |
| **判据前提** | 该轮所有增量迭代 `mem_mode` 必须是 `incremental`，出现 `full` 整轮作废 | 防"增量静默退化成全量"（[23](23-testing-and-functional-verification.md#1-为什么测试要多问一句)） |

达标线照抄客户，**未限定改动量**：

| 操作 | 达标线 | 判定对象 |
|---|---|---|
| checkpoint | ≤ 200 ms | 增量 checkpoint 的客户端墙钟 p50（p99 并列） |
| restore | ≤ 100 ms | 原地 restore 的客户端墙钟 p50（p99 并列） |

### 1.2 三个钟

| 钟 | 谁产生 | 用途 |
|---|---|---|
| 客户端墙钟 | 脚本自己掐表 | **判定** |
| 服务端 `total` | orchestrator 的 `timings_ms` | 长测的主口径（客户端数千并发线程的墙钟里混着客户端调度） |
| 冻结窗口 `frozen` | orchestrator 从 pause 到 resume 的计时 | 解释业务感受到的停顿 |

包含关系：**客户端墙钟 ⊇ 服务端 `total` ⊇ 冻结窗口**。墙钟达标，冻结窗口必然达标，反过来不成立。
但"各分段之和"不受这个约束：`timings_ms` 里既有冻结窗口内的段，也有窗口外的段
（例如 restore 的 `assemble_view_pre`、`conntrack_bg`、`wait_envd`），全部相加会超过冻结窗口；
而且 `seal` 只计到换层完成、不等数据落盘。所以分段只用来看时间分布，不做加总核对。
全部计时键与含义见 [07](07-observability-reference.md)。

### 1.3 分位数怎么算

三类脚本用了三种取法，引用时要知道是哪一种：

| 脚本 | 取法 | 注意 |
|---|---|---|
| `crtest/bench/analyze.py` | 最近秩 `ceil(q·n)`，不插值 | n ≤ 100 时 p99 就是最大值 |
| `crtest/bench/compliance.py` | `s[round(q·(n−1))]`，不插值 | n = 60 时 p99 是第二大的样本，可能比 `analyze.py` 的小 |
| 长测分析脚本 | 线性插值：`k = (n−1)q`，取 `xs[f] + (xs[c]−xs[f])(k−f)` | 只统计起止标记之间的日志行 |

[25](25-results-and-compliance.md) 的分档表 p99 取 `analyze.py` 的主表。

### 1.4 为什么按改动量分档

增量 checkpoint 写出的是本代脏页，成本是 O(本代脏页)，与虚机规格无关（[14](14-memory-diff-tree.md)）。
一台 8 GB 沙箱只改了 4 MB，和一台 2 GB 沙箱改 4 MB 是一样的代价。所以"不同内存快照大小下的性能"
在本方案里翻译成"改动量档位"，这也是唯一能让客户按自己的负载对号入座的分法。
沙箱内存大小只影响两处，都不在判定范围内：全量根的耗时，以及产物的逻辑体积（稀疏文件表观大小恒等于整份内存）。

---

## 2. 条件标签

任何一个耗时或体积数字，不带下面这些不允许被引用：

| 标签 | 为什么 | 从哪读 |
|---|---|---|
| 机型 | 950 与 920B 的 CPU、内核都不同 | `lscpu`、`uname -r` |
| 脏页后端 | `hdbss` / `kvm-wp` / `off`，同样的代码能差几倍 | 问沙箱的 FC API（`dirty_tracking`），脚本开头会打印 |
| 产物盘文件系统与介质 | 真盘还是 loop，ext4 还是别的 | 读 orchestrator **进程**的 environ 与 `/proc/mounts`，不读配置文件 |
| 模板规格 | 全量代价按 guest 内存走 | [23 §8](23-testing-and-functional-verification.md#8-测试环境与沙箱规格) |
| 二进制 | 换过二进制的机器上唯一能对上号的东西 | orchestrator 与 FC 的 sha256 |
| 脚本与参数 | 默认值改过就不是同一组数 | 命令行原样抄 |
| 负载形态 | 单沙箱串行、并发几路、页缓存冷热 | 人填 |

两条纪律：

- **不同条件的数不相减。** 950 与 920B 的脏页后端、机型、盘至少三项同时不同，差值不能归因于其中任何一项，尤其不能归因于 HDBSS。
- **空着比错的数强。** 读不到的格子写"-"或"待测"，不猜、不折算。

---

## 3. 负载构造：三种失真

判定表第一列是"改动 N MB"，这一列成立的前提是**标签上的 N 等于服务端实际搬运的量**。
这是整套性能测试里最容易做错的一步。

| 失真 | 是什么 | 解法 |
|---|---|---|
| **分配成本混进档位** | 第一次申请并写一块新内存，内核要清零、建页表，这笔开销与"改"无关 | **预热**：先把内存文件和磁盘文件一次撑到最大档位，这一代的成本不计入任何档位 |
| **写放大** | 用 shell 字符串拼接、命令替换之类造负载，缓冲、复制、`realloc` 中间态都会弄脏额外的页，实测产物能是标签的数倍 | **原地覆写**：`dd ... conv=notrunc` 只覆写前 N MB、不改文件大小；数据用随机源（`/dev/urandom` 或预生成的随机块），不用全零页（全零页会被稀疏、去重特殊对待） |
| **负载放错层** | 快照只带回它覆盖范围内的东西，负载放在范围外，测出来的既是零成本也是假通过 | 内存那份写 `/dev/shm`（tmpfs 的页就是 guest 内存的页，不落块设备）；文件那份写根文件系统（`/bench-root`），进磁盘写层 |

两条读表时的固定预期：

- **文件那份是普通缓冲写 + `sync`，故意不用 `O_DIRECT`**：真实用户就是这么写的。于是一份文件写会同时出现在内存差分
  （guest 页缓存那些页）和盘层里。所以**实测内存 ≈ 名义内存 + 名义文件，实测盘层 ≈ 名义文件**；偏离这个关系先查负载，再读时间。
  验证读回时反过来，必须 `iflag=direct`（[23](23-testing-and-functional-verification.md#44-pause_verifypy一个自洽的静默损坏)）。
- **3:1 是固定比例**：沙箱里跑代码的真实负载通常改内存远多于改文件。换了比例的数据属于另一条曲线，不和判定表混排。
  纯内存、纯文件、只读触碰三组附表用来求各自的斜率。

---

## 4. 基准脚本分工

| 脚本 | 回答什么 | 产出 |
|---|---|---|
| `crtest/bench/bench_tiers.py` | **判定表**：各档客户端墙钟分布 | 一次迭代 = 在基线 cpA 上造本档改动 → 增量 checkpoint 出 cpB（记分段、墙钟、产物字节）→ restore 回 cpA（撤销这份改动）→ 校验现场 → 删 cpB。`--tier-set full` 18 档，`short` 是小档细分 + 512 MB 极限档 |
| `crtest/bench/compliance.py` | 每档 p50 / p99 / 最大与达标线对照 | `compliance.log` |
| `crtest/bench/analyze.py` | 完整分析 | 异常记录、首个全量、主表与附表、checkpoint / restore 分段、线性拟合、反推碰线改动量、实际产物对名义改动、缓存与漂移、离群（单次 `frozen` > 该档中位 2 倍） |
| `crtest/bench/serial_restore.py` | 没有任何并发时 restore 的长尾 | 一个沙箱、一个调用方、同一个 checkpoint 连回 N 次，定期抽验现场 |
| `acceptance/checkpoint_bench.py` | **时机成本**与**产物实测**（每档只跑一次，不给分位数） | 同一套档位拍两遍：写完立刻拍（最坏上界）与 `sync` 后歇 1 s 再拍（日常口径），两者之差是"写完立刻拍"这个时机的成本；产物按 `st_blocks` 直接量 `mem_diff` 与新封层 |
| `dev/bench-ckpt.py` + `dev/freeze_probe.py` | 冻结窗口在 guest 内看起来多长；60 s 持续连打会不会变慢 | guest 内约 2.5 kHz 单调时钟采样器，时间序列里的"洞"就是冻结时长；每档先测底噪，`min(冻结) > 2 × 底噪` 才标"可区分" |
| `dev/timing.py` | O(脏页) 与链深解耦 | 逐代 checkpoint 量 `df` 增量；链深 5 / 20 / 50 各一条链，浅集（tip ↔ tip−1，路径恒为 1 个纪元）只让深度变，深集（tip ↔ root）让回滚集随深度涨 |
| `dev/probe-ramp.py` | 连打变慢是"链变深"还是"写得多" | 同样多次 checkpoint、把每次脏页量拉开，按"第几代"和"累计 GB"两种归一化算斜率，哪种把曲线收拢就是哪个；判据写在脚本里 |
| `acceptance/checkpoint_concurrent.py` A / B 段 | 跨沙箱扇出与同沙箱争用下耗时怎么涨 | 见 [23 §6](23-testing-and-functional-verification.md#6-checkpoint_concurrentpy并发四段) |

`bench_tiers.py` 的判据前提与异常：`mem_mode` 全是 `incremental`、每次 restore 后现场一致，否则判失败；
**超达标线不判失败**，那是结论不是故障。`analyze.log` 开头的"异常记录：0 条"和"`mem_mode` 取值"两行必须先看，
不过关就不用读时间。"缓存与漂移"一节给出 `materialize_disk_read_mb` 与 `fc_rollback_disk_read_mb` 是否全为 0
（页缓存全热）以及遍与遍之间的冻结窗口比值，用来判断这批数字是冷还是热、有没有随时间漂。

---

## 5. 空闲后 restore 与串口的专项测法

这两项不在仓库脚本里，数据来自工作区的定向复现工具，这里只记测法，便于复测时对齐口径：

- **空闲后 restore**：同一沙箱串行。先拍 cpA 作目标，然后循环"sleep T 秒 → checkpoint cpB → restore cpA → 删 cpB"，
  T 取 0 / 0.5 / 2 / 5 / 10 s；另两臂：a 臂 sleep 2 s 后**不拍** cpB 直接 restore，b 臂 guest 里常驻一个 100 ms 一次的忙循环。
  `serial_restore.py` 是背靠背连续 restore，在它外面加 sleep 不等于"空闲后 restore"。
- **串口段**：每段一个新沙箱、单沙箱随机负载跑 180 s，旁路抓 tap 与 eth 报文、采样 vCPU，判据是串口是否卡死、
  串口行间隔是否有 > 2 s 的缺口。

---

## 6. 并发与长跑的测量

### 6.1 负载

驱动是 `checkpoint_concurrent.py --stages D`：每个沙箱一个线程，随机循环 50% 写脏后 checkpoint /
35% restore 到随机一个已有 checkpoint / 15% delete，每次 restore 后逐项校验 5 个字段
（[23 §6](23-testing-and-functional-verification.md#6-checkpoint_concurrentpy并发四段)）。在它之上用两种负载：

| 负载 | 怎么造 | 代表什么 |
|---|---|---|
| **上限 60** | 服务端设 `CHECKPOINT_MAX_PER_SANDBOX=60`，16 沙箱 30 min；驱动收到 429 不主动删，只记失败继续 | 客户"把数量卡住"的常规用法。可见 checkpoint 很快顶到 60，之后大部分 checkpoint 被 429 拒，restore 占比升高，所以 restore 频率远高于不设上限时 |
| **滚动保留 10** | 驱动每个沙箱只保留最新 10 个，每次新拍之后删最旧的一个；restore 概率 0.25 | 客户"保留最新 N 个、删最旧"的用法。每次删的都是下一个的父节点，正是隐藏节点积累、合并（compact / fold）起作用的场景 |

不设上限、也不删的压测只用来找问题（链深会长到上千），不代表常规负载，数字不进判定。
超过 1 h 的长跑要显式把建沙箱的 timeout 设大（驱动默认 3600 s，到点沙箱被回收，客户端会对着死沙箱空转）。

### 6.2 逐项校验

- `D.json` 的 `verified` = 场景校验通过数 / 进入校验的 restore 数。restore RPC 成功但紧接着的 guest 命令被截断的，
  不进入校验，也不算不一致。
- 滚动保留负载复用同一套校验；出现任何不一致时全体停手、保留现场。
- 多轮的"restore 总数"只累加各轮 `D.json` 的 `verified` 分子，不把 T36、分档基准里的 restore 混进去。

### 6.3 统计

- **主口径是服务端 `timings_ms`**：从 orchestrator 日志里每次操作的完成行取，只统计起止标记之间的行，
  按 300 s 或 600 s 分段看是否随时间漂；分位数用线性插值（§1.3）。客户端墙钟只在注明时使用。
- **磁盘**：旁路每 10 s 采一次 `df`，另有一个只读 manifest 的采样器每 60 s 统计目录数、可见 / 隐藏条目数、层文件数和
  **真实链深**（沿 `parent_id` 走到根的最大深度），看它们是走平还是线性增长。
- **GC 停顿**：服务端开 `GODEBUG=gctrace=1`，STW = 清扫终止 + 标记终止两段之和；需要区分"等所有 P 停下"和"收集器本身"时，
  再开 `CHECKPOINT_RUNTIME_METRICS=1`，每 10 s 一行 runtime/metrics 直方图（分位取桶上界，不会低估）。开关见 [06](06-configuration-and-capacity.md)。
- **内存压力**：orchestrator 与它拉起的 FC 在同一个 memory cgroup 里（记账的是页缓存与进程堆，guest 内存走大页不记账，[21 §8](21-state-concurrency-durability.md#8-节点级效应)），采样 cgroup 的用量与 `failcnt`，
  用来解释 GC 停顿和缺页路径上的回收。
- **流式截断**：客户端按异常类（`incomplete chunked read` 一类）计数，同时在 orchestrator 与 client-proxy 日志里数
  `ReverseProxy read error`，两侧对照判断截断发生在哪一跳。

### 6.4 读盘量：用哪个键

restore 的读盘量有两套口径，并发时读数完全不同（各键的定义只在 [07 §3.3](07-observability-reference.md#33-restore)）：

- `materialize_read_mb`、`materialize_base_read_mb` 由物化过程自己边读边计，含页缓存命中，只计本沙箱，并发时也可直接比较；
- `materialize_disk_read_mb` 是 **orchestrator 进程级**的真实读盘差值，并发时会混入同时段其他沙箱的读；
- `fc_rollback_disk_read_mb` 是该沙箱 FC 进程的真实读盘差值，FC 每沙箱一个进程，不混入别的沙箱。

单沙箱基准里，后两个键非零表示真的读了盘、页缓存不热；并发和长测里看 `materialize_read_mb`。
