# 24 · 配置与容量规划

配置语义和容量模型在本章集中维护。950 的实测取值与来源另有环境记录；未设置、默认值和运行时探测结果分别说明，避免把一台测试机的设置当作所有环境的要求。

## 1. 开关怎么读、怎么生效

- 全部是**进程环境变量**。`CHECKPOINT_*`、`FC_TRACK_DIRTY_PAGES`、`PROXY_TRACE` 由 orchestrator 读；`FC_HDBSS_*`、`FC_ROLLBACK_FAULT_INJECT` 由 Firecracker 读，
  Firecracker 由 orchestrator 拉起、继承它的环境。客户应在自己的服务管理方式中向 orchestrator 传入环境，并确保子进程继承。**内部 950/RPM 部署**将它们写入 Nomad job `template-manager-system` 的 `env {}`（模板为 `/opt/e2b-infra/nomad/template-manager.hcl`）。
- 进程环境在启动时就定了，**改动后须按读取时机重启对应服务；内部 Nomad 部署重启该 job**（[§5](#5-改一个开关操作步骤)）。Firecracker 读的变量只对重启之后新起的虚机生效。
- 写错的值**不会**让节点不带保护地跑：数值和时长解析不了就回落默认值并打一条 WARN（`ignoring unusable … in the environment` 一类）；布尔值见各行的"解析"一列，
  **各开关的布尔写法并不统一**，照表写。
- 生效值与来源（`env` / `default`）大部分打在启动能力行里，也写在能力文件里（[§3](#3-启动能力行)）。能力行里没有的，表里注明了别的核对办法。

## 2. 开关总表

### 2.1 脏页跟踪与 HDBSS

| 变量 | 读取方 | 默认 | 取值与解析 | 作用 | 能力行 |
|---|---|---|---|---|---|
| `FC_TRACK_DIRTY_PAGES` | orchestrator，启动时一次 | 未设 = 跟随硬件：探到 KVM 能力 502（HDBSS）则开，否则关 | Go `strconv.ParseBool`：`1/t/T/TRUE/true/True` 开，`0/f/F/FALSE/false/False` 关。其他值（`yes`、`on`、空串、拼错）**忽略**，按未设处理，并打 WARN `FC_TRACK_DIRTY_PAGES="…" is not a boolean and was ignored` | 第 ① 层：虚机启动时是否武装脏页跟踪。关着时每次 checkpoint 都是全量。950 不设；无 HDBSS 而要增量的机器设 `true`（走 KVM 写保护，[第 23 章 §2](23-build-and-self-check.md#2-先分清两层要不要记脏页用什么方式记)） | `track_dirty_pages`、`track_dirty_pages_reason` |
| `FC_HDBSS_ORDER` | Firecracker，每台虚机构建时 | `1`（每 vCPU 8 KiB） | 按无符号整数解析，解析不了或未设都取 `1`；值原样交给内核，内核不接受则 HDBSS 启用失败 | 每 vCPU HDBSS 缓冲区大小的编码（`n` → 4 KiB × 2^n）。950已有默认order 1的写密集测量；不同order的收益比较未实测；测它的入口是 `run.sh hdbss --orders …`（[第 20 章 §5.1](20-acceptance.md#51-hdbss-档hdbss-专项)） | 无；FC 日志 `HDBSS enabled (buffer order …)`，950 上 `dmesg` 的 `Enable HDBSS success, HDBSS buffer size: …` |
| `FC_HDBSS_REQUIRED` | Firecracker，每台虚机构建且已武装脏页跟踪时 | 关 | **只认 `true` 和 `1`**，其余（含 `TRUE`）一律为关 | 已武装跟踪时，开：HDBSS启用失败则虚机构建失败；关：退回KVM写保护。未武装时直接为`off`，该变量不会强行开启跟踪；内部测试未设置 | FC后端、启用日志及沙箱创建结果；不在orchestrator能力行中 |

### 2.2 限额与超时

| 变量 | 默认 | 取值与解析 | 作用与触发 | 能力行 |
|---|---|---|---|---|
| `CHECKPOINT_MIN_FREE_BYTES` | max(4 GiB, 2 × 该沙箱 guest 内存) | 十进制非负整数，字节；`0` = 关闭检查；负数或解析不了 → WARN，用默认 | 产物盘水位闸。**checkpoint 与 restore** 动手前查 store 所在文件系统的 `statfs` `Bavail`，不足回 **507** `disk_full`；沙箱和已有 checkpoint 不受影响。statfs 本身失败时只记 WARN、放行 | `min_free_bytes`、`_source`（报的是没有沙箱时的下限 4 GiB） |
| `CHECKPOINT_MAX_PER_SANDBOX` | `0`（不限） | 十进制非负整数，个；负数或解析不了 → WARN，用 `0` | 每沙箱**可见** checkpoint 个数上限；到顶的 checkpoint 回 **429** `too_many_checkpoints`。隐藏条目不计 | `max_checkpoints_per_sandbox`、`_source` |
| `CHECKPOINT_MAX_BYTES_PER_SANDBOX` | `0`（关） | 十进制非负整数，字节；负数或解析不了 → WARN，用 `0` | 每沙箱全部 checkpoint 实际占盘（按分配块计，含隐藏条目与各层）的上限；**低于上限就放行，所以可能超出一次 checkpoint 的量**；到顶回 **429** `checkpoint_bytes_limit`。**restore 从不被它拒绝**。上限偏小时按沙箱打 warn / error（[§4.4](#44-字节上限怎么选)） | `max_checkpoint_bytes_per_sandbox`、`_source` |
| `CHECKPOINT_LOCK_WAIT_TIMEOUT` | `60s` | Go 时长（`90s`、`2m`），须 > 0；否则 WARN，用默认 | checkpoint / restore / delete 在同一沙箱的操作锁上排队的上限，超过回 **503** `busy`，带 `Retry-After: 1`，本次什么都没做 | `lock_wait_timeout`、`_source` |
| `CHECKPOINT_FC_CALL_TIMEOUT` | `2m` | Go 时长，须 > 0；否则 WARN，用默认 | 单次 Firecracker API 调用（pause / snapshot / rollback / resume）的时限。pause 超时：补一次 resume 后按普通失败返回；**rollback 超时：无法判断虚机停在哪个时刻，按撕裂处理**（500 `torn`） | `fc_call_timeout`、`_source` |

各拒绝在 SDK 里对应哪个异常、调用方该做什么，见 [第 22 章](22-api-semantics.md)。
SDK 对 checkpoint / restore / delete 的默认请求超时是 300 s，大于锁等待与服务端 45 s envd 等待之和；**把 `CHECKPOINT_LOCK_WAIT_TIMEOUT` 调大时，客户端超时要跟着调大**。

### 2.3 树与存储行为

| 变量 | 默认 | 取值与解析 | 作用 | 能力行 |
|---|---|---|---|---|
| `CHECKPOINT_FULL_ROOT` | 开 | **只有 `""`（未设或空）、`true`、`1` 算开，其余一律算关**——包括 `TRUE`、`True`、`yes`。不打 WARN | 开：每沙箱第一个 checkpoint 捕获整份内存，做树根，树自己能解析每一页。关：第一个 checkpoint 也是增量、根在模板内存源上，省一份 guest 内存的盘，但回滚时早于所有 checkpoint 的页要从模板内存文件读，字节配额的"下限"也只能按 guest 内存估 | **无**；看进程环境 |
| `CHECKPOINT_COMPACT` | 开 | `strconv.ParseBool`；未设为开；解析不了 → WARN，按开 | 开：删除操作结尾把"隐藏、非基准、恰有一个子节点"的条目**并入它的子节点**（内存与 rootfs 层成对合并），使条目数有上界 2V+1（机制与证明见 [第 6 章 §10](06-memory-diff-tree.md#10-删除隐藏与合并)）。关：隐藏条目无界积累，只供对照测试 | `compact`、`compact_source` |
| `CHECKPOINT_COMPACT_MAX_PER_OP` | `8` | 十进制整数，**须 ≥ 1**（关合并用上一行）；否则 WARN，用 `8` | 一次 delete 最多做几次合并。合并在 delete 调用里同步做，这个数越大单次 delete 越可能变长，越小则积压越慢消化（每个未处理候选让上界多 1） | `compact_max_per_op`、`compact_max_per_op_source` |
| `CHECKPOINT_DEBUG_INDEX` | 关 | `strconv.ParseBool`；未设为关；解析不了 → WARN，按关 | 开：后台写者每沙箱最多每 5 s 重写一次 `<store>/<sandbox>/index.json`，进程退出时再写一次。**只供事后查看，没有任何代码读它**；正确性不依赖它 | `debug_index`、`debug_index_source` |

### 2.4 调试与取证（用完即关）

| 变量 | 默认 | 取值与解析 | 作用 | 怎么确认生效 |
|---|---|---|---|---|
| `CHECKPOINT_RUNTIME_METRICS` | 关 | `strconv.ParseBool`；解析不了 → WARN，按关 | 每 10 s 打一行 `runtime GC pauses`：GC STW 的"等 P 停下"与"总时长"两个直方图（读法见 [第 25 章 §6](25-observability.md#6-checkpoint_runtime_metrics-输出)）。关时零开销 | 启动时一行 INFO `logging runtime GC pause histograms` |
| `PROXY_TRACE` | 关 | 不分大小写，`1`、`true`、`yes`、`on` 为开，其余为关；进程内第一次用到时读一次 | orchestrator 沙箱代理的逐请求日志（开始 / 结束两行）和连接池丢弃行，用于取证流截断（[第 25 章 §7](25-observability.md#7-proxy_trace)）。日志量大 | 能力行后紧跟一行 `proxy trace`，字段 `enabled` |
| `GODEBUG=gctrace=1` | —— | Go 运行时标准变量 | 每次 GC 一行写到 stderr（alloc 日志 `start.stderr.<n>`），配合上一项判断 GC 停顿 | 看 stderr |

client-proxy 用的是同一份代理代码，在它的 job 里设 `PROXY_TRACE` 同样生效。

### 2.5 故障注入：仅供测试，交付态不要设

| 变量 | 读取方 | 取值 | 作用 |
|---|---|---|---|
| `CHECKPOINT_FAULT_INJECT` | orchestrator，第一次用到时读一次 | 逗号分隔的注入点名，每个可带 `:once`（只在第一次触发）；认识的名字：`envd_timeout`、`torn_assemble`、`seal_move`、`commit_late`，以及合并路径的 `compact_mem_copy`、`compact_mem_link`、`compact_mem_bitmap`、`compact_switch`、`compact_layer_copy`、`compact_layer_link`、`compact_layer_meta`、`compact_layer_header`。未知的修饰符按"每次都触发"处理并告警 | 让对应失败路径**故意**失败，供 crtest T21 等用例验证恢复规则。设了之后启动日志有 `checkpoint fault injection is armed; these operations will fail on purpose`，能力行 `fault_inject` 非空。**生产节点上 `fault_inject` 必须为空** |
| `FC_ROLLBACK_FAULT_INJECT` | Firecracker | `post_commit` 或 `post_commit:once`，其他值不注入 | 让回滚在提交点之后失败（撕裂路径，crtest T38）。**只有带 cargo 特性 `rollback-fault-inject` 编出的 FC 才认**，交付的 FC 不带，设了也无效果 |

### 2.6 相关的通用变量

| 变量 | 默认 | 与 checkpoint 的关系 |
|---|---|---|
| `ORCHESTRATOR_BASE_PATH` | `/orchestrator` | 决定 store 根：`${DEFAULT_CACHE_DIR}/checkpoints` |
| `DEFAULT_CACHE_DIR` | `${ORCHESTRATOR_BASE_PATH}/build` | 同上；产物盘就是它所在的文件系统 |
| `FIRECRACKER_VERSIONS_DIR` | `/fc-versions` | FC 二进制按 `<它>/<模板记录的版本>/firecracker` 找（[第 23 章 §4](23-build-and-self-check.md#4-版本配对)）；T38 用它指向注入版 FC |

## 3. 启动能力行

orchestrator 启动时打一行 INFO `checkpoint capabilities`，同样的键和值还写进能力文件（[§3.1](#31-能力文件)）。每个值都是用运行时那组函数读出来的，不会出现"日志说一个值、实际按另一个值跑"。
怎么抓见 [第 23 章 §6](23-build-and-self-check.md#6-orchestrator-自报的能力)。

| 字段 | 含义 |
|---|---|
| `store` | checkpoint store 根目录 |
| `track_dirty_pages` | 脏页跟踪开没开（第 ① 层） |
| `track_dirty_pages_reason` | 为什么：`hardware dirty state tracking present (KVM capability 502)`（未设变量、探到 HDBSS）；`no hardware dirty state tracking; software tracking costs a VM exit per clean page`（未设、没探到，结论为关）；`FC_TRACK_DIRTY_PAGES="true"`（显式设置）；`FC_TRACK_DIRTY_PAGES="…" is not a boolean and was ignored; …`（值无效，后半句接前两种之一） |
| `lock_wait_timeout`、`lock_wait_timeout_source` | `CHECKPOINT_LOCK_WAIT_TIMEOUT` 的生效值与来源 |
| `fc_call_timeout`、`fc_call_timeout_source` | `CHECKPOINT_FC_CALL_TIMEOUT` |
| `min_free_bytes`、`min_free_bytes_source` | `CHECKPOINT_MIN_FREE_BYTES`；取默认时这里是 4 GiB 下限，guest 内存大于 2 GiB 的沙箱实际按 2 × guest 内存执行 |
| `max_checkpoints_per_sandbox`、`max_checkpoints_per_sandbox_source` | `CHECKPOINT_MAX_PER_SANDBOX`，`0` = 不限 |
| `max_checkpoint_bytes_per_sandbox`、`max_checkpoint_bytes_per_sandbox_source` | `CHECKPOINT_MAX_BYTES_PER_SANDBOX`，`0` = 关 |
| `debug_index`、`debug_index_source` | `CHECKPOINT_DEBUG_INDEX` |
| `compact`、`compact_source` | `CHECKPOINT_COMPACT` |
| `compact_max_per_op`、`compact_max_per_op_source` | `CHECKPOINT_COMPACT_MAX_PER_OP` |
| `fault_inject` | 已武装的注入点（带 `:once` 的原样写出）；生产节点应为 `[]` |

`_source` 为 `env` 表示环境变量被读到并采用；为 `default` 表示没设、或设了但无效（那时另有一条 WARN）。
能力行**不含** `CHECKPOINT_FULL_ROOT`、`CHECKPOINT_RUNTIME_METRICS`、`FC_HDBSS_*`；它们的核对办法见 [§2](#2-开关总表) 各表。
能力行之后可能紧跟两条 WARN：`FC_TRACK_DIRTY_PAGES` 值无效；以及结论为关时的 `dirty page tracking is off: every checkpoint will copy all of guest memory`。

### 3.1 能力文件

能力行只打一次，负载下 alloc 日志几分钟就把它轮转掉，那时运维和 `run.sh smoke` 都查不到节点按什么跑。所以从 deltabox-dev `93ccb02` 起，
orchestrator 启动时把同样的内容再写进一个文件（`internal/checkpoint/capabilities_file.go`，由 `main.go` 的 `reportCheckpointCapabilities` 调用）：

- **位置**：`${DEFAULT_CACHE_DIR}/checkpoint-capabilities.json`，默认 `/orchestrator/build/checkpoint-capabilities.json`。它在 store 根（`${DEFAULT_CACHE_DIR}/checkpoints`）的**父目录**，
  不在 store 根里，因为 store 根每次启动都被清空。RPM 部署不设 `DEFAULT_CACHE_DIR` 与 `ORCHESTRATOR_BASE_PATH`，就是默认位置。
- **内容**：一个 JSON 对象，两格缩进、键按字母序，权限 `0644`。上表的键与值全部在内，按能力行同一套编码写出，所以时长同样是 `"1m0s"` 这种写法、不是纳秒数。
  另多 4 个键，说明是谁写的：

  | 键 | 含义 |
  |---|---|
  | `pid` | 写这个文件的 orchestrator 进程号（RPM 部署里是 `template-manager` 进程） |
  | `started_at` | 该进程的启动时间，RFC3339 |
  | `version` | orchestrator 的版本号（`main.go` 的 `version`） |
  | `commit` | 构建时注入的提交（`main.go` 的 `commitSHA`；构建没注入时为空串）。早期 RPM 记录为空，950 的 2026-10-15 smoke 自报 `4853232`（[第 17 章 §4](17-functional-verification.md#4-950-smoke部署预检与正确性验收)）；标签有值也须核对源码 / RPM 对应，不能代替二进制 sha256（[第 23 章 §4](23-build-and-self-check.md#4-版本配对)） |

- **何时写**：每次启动写一次，先写同目录下的临时文件、再改名覆盖旧文件，读到的要么是上次启动的整份、要么是这次的整份。
  写成功打 INFO `capabilities file written`（字段 `path`）；写失败只打 WARN `failed to write the capabilities file; the log line above is the only copy`，启动照常。
  这两行的措辞刻意不含 `checkpoint capabilities`，按这个关键字 grep 能力行不会误中。
- **是不是当前的**：文件一直留到下次启动才被替换，进程停了它还在。**`pid` 等于正在运行的进程**才算数；对不上就是以前那次启动留下的，回到能力行。
  同一个 `DEFAULT_CACHE_DIR` 上起了多个 orchestrator 进程时，后启动的覆盖先启动的。
- 此前的版本不写这个文件，这时只有能力行。

## 4. 容量规划

### 4.1 一个沙箱的 checkpoint 占多少盘

记 M = 一次全量捕获（约等于 guest 内存），ε = 一个沙箱的 checkpoint 在内存镜像之外的开销（rootfs 层、层侧车、header、snapfile、脏页侧车；在 920B 开发机上 2 GiB 模板量得约 61 MiB）。

- **树根**：每沙箱第一个 checkpoint 是全量，约 M（`CHECKPOINT_FULL_ROOT=false` 时没有这一份）。
- **每个增量**：约等于本代写过的内存页（稀疏文件，只占写过的页）+ 本代写盘封成的一层。
- **隐藏条目**：删除中间节点时，被后代依赖的会被隐藏而不是删掉；开合并时隐藏条目数 ≤ 可见数 + 1（[第 6 章 §10](06-memory-diff-tree.md#10-删除隐藏与合并)），所以占盘随可见 checkpoint 数有界（不合并时隐藏条目怎样无界积累，见 [附录 C §8.8](C-evolution-and-cases.md#88-隐藏-checkpoint-无上界积累磁盘随时间线性增长)）。
- **restore 的临时文件**：物化的回滚内存最多一份 guest 内存，restore 结束即删。

### 4.2 节点产物盘怎么估

```
产物盘 ≥ Σ(每沙箱的 checkpoint 占用) + 水位闸余量 max(4 GiB, 2 × 最大 guest 内存) + 沙箱自身写层
```

设了字节上限 L 时，每沙箱占用不超过 L 加一次 checkpoint 的量，于是 `Σ ≤ 沙箱数 × (L + M + 一层)`；没设时只能按业务的保留习惯估。
**产物盘写满影响的是整个节点**：同一文件系统上每个沙箱往稀疏映射里的写都会失败。水位闸就是为此提前拒绝。920B 上各负载的实测占盘见 [附录 B §10.2](B-920b-results.md#102-上限-60-的最终长测30-min-时间序列)（上限 60）与 [附录 B §10.3](B-920b-results.md#103-滚动保留-10一小时与两小时)（滚动保留 10）。

### 4.3 数量上限怎么选

`CHECKPOINT_MAX_PER_SANDBOX` 数的是**可见** checkpoint，对磁盘意义有限（全量根约 M，增量从几页到几乎全部），它的价值是给"循环打点、从不删除"的客户端一个能处理的答复（429）。
按业务需要保留的恢复点数设；开合并时条目总数 ≤ 2 × 上限 + 1，目录数随之有界。

### 4.4 字节上限怎么选

`CHECKPOINT_MAX_BYTES_PER_SANDBOX` 是全节点一个值、对每个沙箱各自生效。下限的推导（L 为上限）：

- **L > M + ε：永远不会卡死。** 从最旧开始删非基准的 checkpoint，它们会被逐个合并进基准，最后基准成为约 M + ε 的全量根。
- **L > 2M + ε：最坏负载下也能保留一个早期恢复点。** 即使每次 checkpoint 都重写全部内存，也能在留住一个更早的点的同时继续 checkpoint。

服务端的告警阈值把 ε 向上取整到 256 MiB，并在某个沙箱第一次 checkpoint 时判定（启动时不知道各沙箱内存多大，所以每沙箱每代判一次）：

| 条件 | 级别 | 含义 |
|---|---|---|
| L < 2 × guest 内存 + 256 MiB | warn | 最坏负载下保留一个早期点可能卡在上限 |
| L < 1 × guest 内存 + 256 MiB | error | 连基准都放不下；到上限之后删什么都回不来 |

所以 **L ≥ 2 × 最大 guest 内存 + 256 MiB**。2048 MB 的模板对应 `4563402752`（= 2 × 2 GiB + 256 MiB）。

几件运维要知道的事：

- **删基准（沙箱当前所在的那个 checkpoint）几乎不释放空间**（只还 snapfile 与 header，几十 KiB），删分叉点同理；释放空间要删**最旧的**，或 restore 到更早的点再删掉之后那一支（用户视角的说明在 [第 22 章 §4.4](22-api-semantics.md#44-在字节上限下删哪个才能释放空间)）。
- 两个增量改的页互不相交时，合并释放为 0；最旧的是全量根、而子节点改了大部分内存时，合并释放接近一份 guest 内存。
- 拒绝时 WARN 行 `refused a checkpoint operation` 带 `checkpoint_bytes_held`、`max_checkpoint_bytes_per_sandbox`、`base_checkpoint_id`、`floor_bytes`、`floor_bytes_source`；
  删除行 `deleted checkpoint` 带 `sandbox_checkpoint_bytes` 与 `freed_bytes`（含合并效果）。注意 delete 的 `compact_mb` 是合并**写入**量，不是释放量。
- 交付件里默认关；是否默认开、开多大还没定。

### 4.5 合并让"保留最新 N 个"收敛

客户端"保留最新 N 个、删最旧"时，每次删的都是下一个的父节点：不合并的话它只能被隐藏，隐藏条目、目录数、链深和占盘随时间线性增长，最终写满节点；
开合并后每个被隐藏的节点并进它唯一的子节点，合并处理跟上时条目数稳定在2N+1以内，消除隐藏历史的无界积累；实际占盘仍由保留数据、视图覆盖和写入模式决定（920B 开与不开合并的对照见 [附录 C §8.8](C-evolution-and-cases.md#88-隐藏-checkpoint-无上界积累磁盘随时间线性增长)）。
代价是 delete 里同步做合并、delete 变慢（已知项，[第 20 章 §7](20-acceptance.md#7-已知项与缺口)）。**生产不要关 `CHECKPOINT_COMPACT`。**

**脏页跟踪关着时合并不起作用**：每个 checkpoint 都是全量根，没有"隐藏且只有一个子节点"的条目，层照样累积。无 HDBSS 又不开跟踪的节点，必须靠数量上限与字节上限兜住。

### 4.6 多租户节点的建议

1. 两个上限都设：数量上限挡"循环打点"，字节上限挡"少量但巨大"；字节上限按节点上**最大**的 guest 内存取 2M + 256 MiB 以上。
2. 水位闸保持默认或调大，别设 `0`。
3. 保持 `CHECKPOINT_COMPACT` 默认开。
4. checkpoint 不会自动过期，沙箱销毁时才随之删除；长期存活的沙箱要靠客户端删除或上限约束。
5. 巡检产物盘（`df -h /orchestrator/build`）与 refused 日志的数量。

## 5. 改一个开关：操作步骤

配置先按本章总表确认读取方、取值和生效时机，再使用客户自己的服务管理流程更新进程环境。

1. 记录当前运行身份、能力文件与有效配置；明确改动目标及默认行为。
2. 将变量写入实际服务配置，确保 Firecracker 所需变量会传给由 orchestrator 创建的子进程。
3. 按读取时机重启对应服务或创建新的沙箱。重启会销毁该节点上的活沙箱及其 checkpoint，需先安排业务。
4. 比较新进程的能力文件、`_source` 和启动 WARN；仅检查配置文件或进程环境不能证明解析后的结果。
5. 对 HDBSS 另检查新 FC 的后端、order 日志和内核证据，再运行最小正确性和增量检查。
6. 记录本轮变更后的实际取值、版本和自检结果；检查客户升级流程能否保留这些设置。

内部 950 测试采用 Nomad job 的 `env {}` 传入变量。完整的 job 修改、RPM 重建和部署操作继续在[内部部署资料](../../deploy-docs/08-源码开发与出包流程.md)中维护；本特性不要求客户采用同一种部署方式。
