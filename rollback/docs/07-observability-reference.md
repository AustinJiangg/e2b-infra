# 07 · 可观测性参考

> 给运维和开发：orchestrator 为 checkpoint / restore / delete 留下的每一个数字在哪、叫什么、怎么读。
> 本篇是**计时键与 timings 字段的唯一定义处**，其他篇只引用。各键以 [README](README.md) 所列代码基准里实际打点的为准。

---

## 1. 数字从哪来

一次调用有三个钟：客户端墙钟（SDK 发出到收到回应）、冻结窗口（虚机暂停到恢复，业务真正感到的停顿）、宿主分阶段（时间花在哪）。
前两个说"差了多少"，第三个说"差在哪"。宿主分阶段落在三个地方，内容相同：

| 位置 | 内容 | 注意 |
|---|---|---|
| orchestrator 日志：`created checkpoint` / `restored checkpoint` / `deleted checkpoint` 行的 `timings_ms` | 每次成功操作一行 | 在当前 nomad alloc 的 `logs/start.stdout.<n>`，会轮转 |
| 失败时的日志：`checkpoint create failed` / `checkpoint restore failed`（WARN） | 失败的那次也有完整分段 | 同上 |
| `<store>/<sandbox>/<checkpoint>/timings.json` | 这个 checkpoint 的分段，写一次不再动 | store 默认 `/orchestrator/build/checkpoints` |
| `<store>/<sandbox>/last-restore-timings.json` | 该沙箱**最近一次** restore 的分段（成败都写） | 每次 restore 覆盖，要紧接着读 |

从日志里取数的写法（只读）：

```bash
p=$(pidof -s template-manager)
A=$(tr '\0' '\n' < /proc/$p/environ | sed -n 's/^NOMAD_ALLOC_DIR=//p')
ls -1v "$A"/logs/start.stdout.* | xargs grep -ah 'restored checkpoint' | tail -3
```

---

## 2. 能力行与 memMode

**能力行**：启动时一行 `checkpoint capabilities`，字段逐个解释在 [06 §3](06-configuration-and-capacity.md#3-启动能力行)。它回答"这台机器记不记脏页、为什么、各限额按什么值跑"。
同样的内容还写在**能力文件** `${DEFAULT_CACHE_DIR}/checkpoint-capabilities.json`（默认 `/orchestrator/build/` 下，[06 §3.1](06-configuration-and-capacity.md#31-能力文件)），不随日志轮转丢失；其中的 `pid` 须是正在运行的进程。
它不回答"用什么记"——`hdbss` / `kvm-wp` / `off` 是 Firecracker 自报的，由 `checkpoint_verify.py` 开头的"脏页后端"打印（[05 §4.6](05-deployment-prerequisites.md#46-增量真的生效)）。

**memMode**：每次 checkpoint 的返回值（SDK `CheckpointInfo.mem_mode`）、`created checkpoint` 日志行的 `mem_mode`、metrics `orchestrator.sandbox.checkpoint.calls` 的 `mem_mode` 属性，三处是同一个值：

| 值 | 什么时候出现 | 正常吗 |
|---|---|---|
| `full` | 每沙箱的第一个 checkpoint（树根，`CHECKPOINT_FULL_ROOT` 默认开）；断链之后的下一个（日志 `incremental chain is broken; capturing full memory as a new root`）；脏页跟踪关着时**每一个**（日志 `dirty page tracking is off; every checkpoint captures full memory`） | 前两种正常；第三种就是"增量退化成全量"（[08 §1](08-troubleshooting.md#1-增量退化成全量)） |
| `incremental` | 其余 | 正常 |

判据一句话：**除了沙箱的第一个 checkpoint 和断链后的那一个，出现 `full` 就是有问题**。

---

## 3. timings 全字段与计时键

### 3.1 通用约定

- 值默认是**毫秒**（orchestrator 自己计时，精度 1 µs）。以 `_mb` 结尾的是**兆字节**（按 1024 × 1024 字节计），以 `_count` 结尾的是**个数**。
- `fc_*` 时长是 Firecracker 回报的微秒换算成的毫秒。
- 失败的操作也记分段，只到失败那一步为止；某个键缺席表示那一步没走到（或该条件下不打，见各行）。
- **`total` 的起点**：checkpoint 与 restore 的 `total` 从进入处理函数开始，**包含在该沙箱操作锁上排队的时间**（没有单独的键；排队上限是 `CHECKPOINT_LOCK_WAIT_TIMEOUT`）；delete 的 `total` 从拿到操作锁之后开始。
- 所有 `*_lock_wait` 等的是 **store 的全局锁**，即"本沙箱在为别的沙箱的账本操作排队"，并发高时才会非零。

### 3.2 checkpoint

阶段关系（缩进表示包含，`≈` 表示另有零碎开销）：

```
total ≈ prepare + rootfs_header + checkpoint_to_files + append_layer + commit
  prepare               ⊃ prepare_lock_wait, prepare_manifest
  checkpoint_to_files   = frozen + seal_move
    frozen              ≈ pause + [save_live_bitmap] + snapshot + seal + resume
  append_layer          ≈ append_lock_wait + append_merge + append_meta_write + append_serialize + append_header_write
  commit                ≈ commit_files + commit_manifest + commit_lock_wait + commit_index
```

| 键 | 含义 | 在不在冻结窗口 | 口径注意 |
|---|---|---|---|
| `total` | 服务端整段 | —— | 含排队（§3.1） |
| `prepare` | 准入（个数上限、字节上限）+ 建条目目录 | 外 | |
| `prepare_lock_wait` | 准入检查等全局锁 | 外 | 并发时混入别人的账本操作 |
| `prepare_manifest` | 写条目的 `manifest.json` | 外 | |
| `rootfs_header` | 取沙箱当前的 rootfs header | 外 | |
| `checkpoint_to_files` | 暂停、写快照、封层、恢复、把封好的层移进 store | 大部分在内 | |
| `frozen` | **pause → resume，guest 停住的时间** | 就是窗口 | 同一值也记入 metrics `orchestrator.sandbox.checkpoint.paused.duration` |
| `pause` | 暂停虚机的 FC 调用 | 内 | |
| `save_live_bitmap` | 全量捕获前导出活跃脏图 | 内 | **只在 `mem_mode=full` 时出现** |
| `snapshot` | Firecracker 写快照（内存差分 + 位图侧车 + snapfile） | 内 | checkpoint 的大头，随本代脏页量线性增长 |
| `seal` | 封存 rootfs 写层（换层，不搬数据） | 内 | |
| `resume` | 恢复虚机的 FC 调用 | 内 | |
| `seal_move` | 把封好的层文件移进 store（同文件系统是 rename） | **外**（resume 之后） | 产物盘与缓存盘不同源时变成拷贝，这里变大 |
| `append_layer` | 把新层记进磁盘账本、写层侧车和 header | 外 | |
| `append_lock_wait` | 记账本等全局锁 | 外 | 同 `prepare_lock_wait` |
| `append_merge` | 锁内合并 header 映射 | 外 | |
| `append_meta_write` | 写层侧车 `.meta` | 外 | |
| `append_serialize` | 序列化 header | 外 | |
| `append_header_write` | 写 `rootfs.header`（不 fsync） | 外 | |
| `commit` | 提交 | 外 | |
| `commit_files` | 临时文件 rename 成最终名 | 外 | |
| `commit_manifest` | 重写 manifest | 外 | |
| `commit_lock_wait` | 发布条目等全局锁 | 外 | |
| `commit_index` | 拿到锁之后发布条目的工作 | 外 | 名字沿用；现在只计锁内这一段，与链深无关（`index.json` 不再在热路径上写） |

checkpoint 失败而 Firecracker 已写出快照时，条目会被"救成隐藏条目"，这时分段里也会出现 `commit_lock_wait` / `commit_index`（[20](20-failure-semantics.md)）。

### 3.3 restore

```
total ≈ disk_view_read + assemble_view_pre + rollback_in_place + wait_envd
  rollback_in_place ≈ frozen
    frozen ≈ pause + save_live_bitmap + materialize + fc_rollback + assemble_view(=0) + reset_view + conntrack + resume
      materialize ⊃ materialize_lock_wait, revert_mem_write, revert_bitmap_write
      fc_rollback ≈ fc_total + RPC 开销
        fc_total ≈ fc_validate + fc_quiesce + fc_bitmap + fc_memory + fc_vcpus + fc_gic + fc_devices
  conntrack_bg（与 frozen 里的其他步骤并行）= max(conntrack_ns, conntrack_host_queue + conntrack_host_sweep)
```

| 键 | 含义 | 单位 | 在不在冻结窗口 | 口径注意 |
|---|---|---|---|---|
| `total` | 服务端整段 | ms | —— | 含排队（§3.1） |
| `disk_view_read` | 读并解码目标视图各层的 `.meta`（每块 8 字节） | ms | 外（暂停前） | 随视图块数线性增长 |
| `assemble_view_pre` | 在暂停前把目标的磁盘视图装配好（按块号位图认领） | ms | 外 | 失败时沙箱原样继续跑，不是撕裂 |
| `rollback_in_place` | 从暂停到恢复的整段（含极少的准备） | ms | 包住窗口 | |
| `frozen` | **从回滚前 pause 到 resume 之后**，guest 停住的时间 | ms | 就是窗口 | **包含等 conntrack 的那段 `conntrack`** |
| `pause` / `resume` | 暂停 / 恢复的 FC 调用 | ms | 内 | |
| `save_live_bitmap` | 导出本代活跃脏图 | ms | 内 | |
| `materialize` | 算回滚集并写出回滚内存文件与回滚位图 | ms | 内 | restore 里我们自己代码的大头 |
| `materialize_lock_wait` | 物化前等全局锁 | ms | 内 | **别的沙箱的账本操作直接加进本沙箱的冻结窗口** |
| `revert_mem_write` | 沿祖先链解析每页内容并写回滚内存文件 | ms | 内 | 随回滚量线性增长 |
| `revert_bitmap_write` | 写回滚位图 | ms | 内 | |
| `materialize_read_mb` | 本次物化读的 checkpoint 文件字节：活跃脏图与各侧车（按盘上大小）+ 从各内存差分读到的字节 | MB | —— | **只算本次 restore**；含页缓存命中，表示"向输入要了多少"，不表示碰了盘 |
| `materialize_base_read_mb` | 本次物化从模板内存源读的字节 | MB | —— | 全量根默认开时应为 0；`CHECKPOINT_FULL_ROOT=false` 或老树才会非零 |
| `materialize_disk_read_mb` | orchestrator **进程** `/proc/self/io` 的 `read_bytes` 在物化前后的差 | MB | —— | **进程级口径**：并发 restore 时混入其他沙箱的读，只有单沙箱运行时才能读成"这次 restore 碰了盘"；页缓存命中不计 |
| `fc_rollback` | Firecracker 原地回滚调用（orchestrator 侧计时） | ms | 内 | |
| `fc_rollback_disk_read_mb` | 该 FC 进程在回滚期间的 `read_bytes` | MB | —— | 读的是刚写出、未 fsync 的回滚文件，非零说明回写抢在了回滚前面 |
| `fc_validate` … `fc_total` | FC 自报分段（§4） | ms | 内 | |
| `fc_vcpu_mmio_drained_count` | 本次回滚里排空在途 MMIO 访问的 vCPU 次数 | 个 | —— | FC 不报时整键缺席 |
| `assemble_view` | 恒为 0 | ms | —— | 占位键，便于与视图装配还在窗口内的老数据对齐；实际装配看 `assemble_view_pre` |
| `reset_view` | 把沙箱的磁盘整体切到目标视图 | ms | 内 | 在提交点之后，失败即撕裂 |
| `conntrack` | 冻结窗口里**等**后台连接跟踪清理结束的时间（join） | ms | 内 | 常态接近 0；清理比回滚慢时才非零 |
| `conntrack_bg` | 后台清理自身的时长，从 pause 开始与回滚并行 | ms | 并行 | 宿主 conntrack 表变大时先涨它 |
| `conntrack_ns` | 清沙箱自己 netns 里的 conntrack 表 | ms | 并行 | |
| `conntrack_host_queue` | 等宿主侧正在进行的那次清扫结束，才能开始覆盖本次的清扫 | ms | 并行 | **并发口径**：等的是别的沙箱发起的清扫 |
| `conntrack_host_sweep` | 覆盖本次地址的那次宿主表清扫 | ms | 并行 | **同批所有沙箱共享同一次清扫**，同一值出现在该批每个 restore 里 |
| `conntrack_host_batch_count` | 那次清扫服务了几个槽位 | 个 | —— | 本次放弃等待时，宿主侧三个键与下一个键都缺席 |
| `conntrack_host_entries_count` | 选路时读到的宿主 conntrack 表条目数 | 个 | —— | 读不到内核计数时缺席 |
| `wait_envd` | 回滚后等 guest 里 envd 应答 | ms | 外 | 服务端最多等 45 s，超时报 `guest_unresponsive` |

宿主表清扫怎么选路（整表扫描 vs 内核过滤）、为什么放在冻结窗口外并行，见 [18](18-rollback-pitfalls.md#44-宿主表怎么删每批一次整表扫描按代价模型选路)。

### 3.4 delete

| 键 | 含义 | 单位 | 口径注意 |
|---|---|---|---|
| `total` | 删除整段（从拿到该沙箱操作锁开始），**含结尾同步做的合并**（compact / fold） | ms | |
| `delete_lock_wait` | 改账本等全局锁 | ms | 并发口径 |
| `compact` | 本次 delete 结尾的合并总耗时 | ms | 只在至少尝试了一次合并时出现；上限由 `CHECKPOINT_COMPACT_MAX_PER_OP` 控制 |
| `compact_mb` | 合并**拷贝写入**的字节（内存页与盘块，失败的那次也算） | MB | 是写入量，**不是释放量**；释放量看日志字段 `freed_bytes` |
| `compact_count` | 成功合并了几个隐藏条目 | 个 | |
| `compact_layers_count` | 其中连 rootfs 层一起合并的次数 | 个 | 层的持有者不成对时只合内存 |

### 3.5 日志行上 timings 之外的字段

| 日志行 | 字段 | 含义 |
|---|---|---|
| `created checkpoint` | `checkpoint_id`、`mem_mode`、`parent_id`、`sandbox_checkpoint_bytes` | 新条目 id；全量 / 增量；树上的父节点（全量根为空）；本沙箱 checkpoint 此刻的总占盘（字节配额口径，关配额也照记） |
| `restored checkpoint` | `checkpoint_id`、`fc_vcpu_mmio_drained`、`fc_vcpu_mmio_drain_failed`、`fc_vcpu_readback_mismatch`、`fc_vcpu_mmio_drained_pause_total` | 前三个是本次回滚的增量计数，第四个是该 FC 进程累计的暂停期排空次数。**`drain_failed` 或 `readback_mismatch` 非零时整行降为 WARN**：guest 在一个没人核实过的 vCPU 状态上恢复了 |
| `deleted checkpoint` | `checkpoint_id`、`sandbox_checkpoint_bytes`、`freed_bytes` | 删后的总占盘；本次删除（含合并）实际释放的字节 |
| `refused a checkpoint operation`（WARN） | `reason`、`operation`，以及按原因不同：`free_bytes`/`min_free_bytes`/`min_free_bytes_source`；`checkpoints_held`/`max_checkpoints_per_sandbox`；`checkpoint_bytes_held`/`max_checkpoint_bytes_per_sandbox`/`base_checkpoint_id`/`floor_bytes`/`floor_bytes_source` | 哪道闸、判定用的数字。`floor_bytes_source` 为 `full_root`（基准链上全量根的大小）或 `guest_memory`（链上没有全量根，按 guest 内存估） |

格式示例（节选；数值只示意格式，性能数字见 [25](25-results-and-compliance.md)）：

```
INFO  restored checkpoint  {"sandbox.id": "…", "checkpoint_id": "ckpt_1790650329519085360", "timings_ms": {"assemble_view":0,
"assemble_view_pre":0.205,"conntrack":0.001,"conntrack_bg":11.434, … ,"frozen":24.04,"materialize":11.843,
"materialize_read_mb":35.87, … ,"total":35.473,"wait_envd":2.126}, "fc_vcpu_mmio_drained": 0, …}
```

---

## 4. Firecracker 自报的分段

restore 调用 Firecracker 的原地回滚接口，它在应答里带回自己的分段（微秒）和 vCPU 计数；orchestrator 换算成毫秒记为 `fc_*`：

| 键 | Firecracker 里这一步做什么 |
|---|---|
| `fc_validate` | 校验快照与运行中的虚机是否匹配 |
| `fc_quiesce` | 静默在途设备 I/O。**空闲一段时间后不先 checkpoint 直接 restore 时它会显著变大**（[08 §6](08-troubleshooting.md#6-空闲后第一次-restore-慢)） |
| `fc_bitmap` | 取活跃脏图并并入回滚集；与总内存成正比，是 `fc_memory` 的下限 |
| `fc_memory` | 把回滚集里的页写回 guest 内存；随回滚量线性增长 |
| `fc_vcpus` | 恢复 vCPU 状态 |
| `fc_gic` | 恢复中断控制器状态（含串口中断线清理） |
| `fc_devices` | 原地恢复设备状态 |
| `fc_total` | FC 侧端到端 |

`fc_rollback − fc_total` 就是 API 往返开销。**checkpoint 没有 `fc_*` 分段**，Firecracker 写快照只有 orchestrator 侧的 `snapshot` 一个数。

---

## 5. metrics

orchestrator 通过 OpenTelemetry 导出以下指标。**要部署里有 collector 接收才看得到**；单节点离线部署默认没有跑 otel-collector job（`nomad job status` 可见），这时以日志为准。

| 指标 | 类型 | 属性 | 含义 |
|---|---|---|---|
| `orchestrator.sandbox.checkpoint.calls` | 计数 | `operation`（create / restore / list / delete）、`success`、`mem_mode`（仅 create） | 调用次数；`mem_mode=full` 的比例就是"退化成全量"的看板 |
| `orchestrator.sandbox.checkpoint.duration` | 直方图，ms | 同上 | 服务端时长 |
| `orchestrator.sandbox.checkpoint.paused.duration` | 直方图，ms | —— | checkpoint 的冻结窗口 |
| `orchestrator.sandbox.checkpoint.vcpu.events` | 计数 | `operation`、`event`（mmio_drained / mmio_drain_failed / readback_mismatch） | 后两个应恒为 0 |
| `orchestrator.sandbox.checkpoint.vcpu.pause_drains` | gauge | `operation` | FC 进程累计的暂停期排空次数 |
| `orchestrator.sandbox.checkpoint.compact.folds` | 计数 | `success` | 合并次数 |
| `orchestrator.sandbox.checkpoint.compact.bytes` | 计数，字节 | `success` | 合并拷贝的字节 |

---

## 6. `CHECKPOINT_RUNTIME_METRICS` 输出

打开后（[06 §2.4](06-configuration-and-capacity.md#24-调试与取证用完即关)）每 10 s 一行 INFO `runtime GC pauses`，数据来自 Go runtime/metrics 的两个直方图：
`/sched/pauses/stopping/gc`（STW 里"等所有 P 停下"那部分）和 `/sched/pauses/total/gc`（整个 STW）。

```
INFO  runtime GC pauses  {"gc_cycles": 4, "gc_cycles_cumulative": 68, "stw_stopping_count": 8, "stw_stopping_p50": "262.144µs",
"stw_stopping_p99": "655.36µs", "stw_stopping_max": "655.36µs", "stw_stopping_count_cumulative": 136, … , "stw_total_max_cumulative": "1.572864ms"}
```

| 字段 | 含义 |
|---|---|
| `gc_cycles`、`gc_cycles_cumulative` | 本窗口 / 进程累计的 GC 轮数 |
| `stw_stopping_count`、`_p50`、`_p99`、`_max` | 本窗口"等 P 停下"的次数与分位 |
| `stw_stopping_count_cumulative`、`_p99_cumulative`、`_max_cumulative` | 进程累计 |
| `stw_total_*` | 同上，整个 STW |

读法：

- 一轮 GC 停两次世界，所以 `stw_*_count` 约为 `gc_cycles` 的两倍。
- 分位取的是直方图桶的**上界**，不会低估；桶宽最多约五分之一个 2 的幂。
- **长停顿里 `stopping` 占了 `total` 的几乎全部**，说明时间花在"等某个 goroutine 让出 P"上，而不是收集器本身——典型原因是 goroutine 在缺页里撞上 memory cgroup 回收（[08 §8](08-troubleshooting.md#8-gc-长停顿与-memory-cgroup-回收)）。
- 配合 `GODEBUG=gctrace=1`（stderr，每轮一行）看每轮细节。

---

## 7. `PROXY_TRACE`

打开后 orchestrator 的沙箱代理对每个请求写两行、对连接池事件写一行（INFO）：

| 消息 | 关键字段 |
|---|---|
| `proxy trace: request started` | `method`、`path`、`connection_key` |
| `proxy trace: request ended` | `status_code`、`wrote_header`、`streamed`、`response_bytes`、`duration_ms`、`aborted_after_headers`、`abort`、`upstream_local_addr`、`upstream_remote_addr`、`upstream_conn_reused`、`upstream_conn_was_idle` |
| `proxy trace: upstream connection reset` | 上游连接被重置 |
| `proxy trace: proxy client removed from pool` | `drop_reason`（`restore` / `sandbox_stopped` / `envd_restarted` / `build_done` / `unspecified`）、`active_connections` |

用它区分两类流截断（机制见 [03](03-errors-timeouts-concurrency.md#53-两类流式截断要分清)）：

- **restore 造成的**（既定限制）：同一沙箱先有 `removed from pool` 且 `drop_reason=restore`，随后那条流 `request ended` 带 `aborted_after_headers=true`。
- **与 restore 无关的**（代理未开 full duplex，已修）：`aborted_after_headers=true`、`abort="net/http: abort Handler"`、`upstream_conn_reused=false`，前后**没有** `drop_reason=restore`。修复后不应再出现；若出现，先确认 orchestrator 与 client-proxy 两个镜像都是修复后的版本。

日志量很大，取证完就关。

---

## 8. 日志关键字速查

都在 orchestrator 的 alloc 日志里（`start.stdout.<n>`；`GODEBUG=gctrace=1` 的输出在 `start.stderr.<n>`）。

| 关键字 | 级别 | 代表什么 | 去哪看 |
|---|---|---|---|
| `checkpoint capabilities` | INFO | 启动能力行 | [06 §3](06-configuration-and-capacity.md#3-启动能力行) |
| `capabilities file written` | INFO | 能力文件已写好，字段 `path` 是它的位置 | [06 §3.1](06-configuration-and-capacity.md#31-能力文件) |
| `failed to write the capabilities file` | WARN | 能力文件没写成，只剩能力行；启动不受影响 | [06 §3.1](06-configuration-and-capacity.md#31-能力文件) |
| `deleted checkpoint but could not remove all of its files` | WARN | delete 已生效、按成功应答，只是它的目录或 manifest 没处理完；残留随沙箱回收 | [03](03-errors-timeouts-concurrency.md#2-错误总表) |
| `dirty page tracking is off` | WARN | 脏页跟踪关，checkpoint 全是全量 | [08 §1](08-troubleshooting.md#1-增量退化成全量) |
| `is not a boolean and was ignored` | WARN | `FC_TRACK_DIRTY_PAGES` 值无效，按硬件决定 | [06 §2.1](06-configuration-and-capacity.md#21-脏页跟踪与-hdbss) |
| `ignoring unusable … in the environment`、`ignoring a … setting that is not a boolean` | WARN | 某个开关写错，用了默认值；字段 `env` 说是哪个 | [06 §2](06-configuration-and-capacity.md#2-开关总表) |
| `checkpoint fault injection is armed` | WARN | 故障注入开着；生产节点不应出现 | [06 §2.5](06-configuration-and-capacity.md#25-故障注入仅供测试交付态不要设) |
| `created checkpoint` / `restored checkpoint` / `deleted checkpoint` | INFO | 成功的操作与分段 | §3 |
| `checkpoint create failed` / `checkpoint restore failed` | WARN | 失败的操作与分段 | [08](08-troubleshooting.md) |
| `refused a checkpoint operation` | WARN | 被水位闸 / 个数上限 / 字节上限拒绝 | [08 §2](08-troubleshooting.md#2-checkpoint-被拒507--429) |
| `checkpoint byte limit is below …` | WARN / ERROR | 字节上限对这个沙箱的 guest 内存偏小 | [06 §4.4](06-configuration-and-capacity.md#44-字节上限怎么选) |
| `incremental chain is broken; capturing full memory as a new root` | WARN | 丢过一段脏页记录，这次以全量起新树 | [20](20-failure-semantics.md) |
| `failed to keep the advanced epoch; chain broken until a full checkpoint` | ERROR | 失败的 checkpoint 没能救成隐藏条目，链断开 | [20](20-failure-semantics.md) |
| `restore failed past the commit point; the sandbox is torn and must be recreated` | ERROR | 撕裂 | [08 §3](08-troubleshooting.md#3-撕裂torn与虚机-faulted) |
| `rollback succeeded but the guest's envd never answered` | ERROR | `guest_unresponsive`，带完整分段 | [08 §4](08-troubleshooting.md#4-guest-失联guest_unresponsive) |
| `sealing the write layer failed; checkpoints are refused until a restore reseeds …`、`failed to reset rootfs bookkeeping after restore` | ERROR | 磁盘账本污染（`rootfs_poisoned`），restore 一次即修复 | [20](20-failure-semantics.md) |
| `failed to fold a hidden checkpoint into its only child; the tree is unchanged` | WARN | 一次合并失败，树原样不动，下次 delete 重试（最多 3 次） | [14](14-memory-diff-tree.md#10-删除隐藏与合并) |
| `failed to delete dropped checkpoint files` | WARN | 锁外删文件失败，残留等下次启动清空 store 时处理 | [21](21-state-concurrency-durability.md) |
| `failed to flush conntrack during in-place rollback` | WARN | 连接跟踪清理失败；restore 不因此失败 | [18](18-rollback-pitfalls.md) |
| `runtime GC pauses` | INFO | `CHECKPOINT_RUNTIME_METRICS` 的输出 | §6 |
| `proxy trace: …` | INFO | `PROXY_TRACE` 的输出 | §7 |
| `httputil: ReverseProxy read error during body copy … use of closed network connection` | WARN | 代理在转发响应体时连接被关；与客户端的 `incomplete chunked read` 对应 | [08 §5](08-troubleshooting.md#5-restore-后连接挂死或流截断) |
