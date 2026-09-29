# 附录 A · 术语、代码地图与文件格式

> 全书的查询入口，不用从头读。术语、代码位置（以 `deltabox-dev@8ea5322bf` 为准）、产物文件格式、不变量速查、篇目表。
> 环境变量与开关见 [06](06-configuration-and-capacity.md)，错误码与异常见 [03](03-errors-timeouts-concurrency.md)，
> 计时键见 [07](07-observability-reference.md)。

---

## 1. 术语表

### 1.1 本方案的概念

| 术语 | 英文 / 标识 | 一句话 | 详见 |
|---|---|---|---|
| **checkpoint** | checkpoint | 沙箱某一时刻的状态记录，也指拍它的操作（与 restore 成对）。接口名里叫 `CreateCheckpoint` | [01](01-quickstart.md)、[14](14-memory-diff-tree.md) |
| **restore / 回滚** | restore / rollback | 把活着的沙箱原地退回某个 checkpoint，沙箱 ID 与 FC 进程不变 | [17](17-in-place-rollback.md) |
| **纪元** | epoch | 两次"脏页跟踪被重置"之间的时间段 | [14](14-memory-diff-tree.md) |
| **纪元位图 / 侧车** | `E_x`、`mem_bitmap`、sidecar | 记录一个纪元里写过哪些页 | [14](14-memory-diff-tree.md)、[15](15-firecracker-api-contract.md) |
| **差分树** | diff tree | 以 `ParentID` 组织的 checkpoint 树 | [14](14-memory-diff-tree.md) |
| **基准** | base | 下一个 checkpoint 的父节点；每沙箱一个 | [14](14-memory-diff-tree.md) |
| **回滚集** | revert set | restore 时需要写回的页集合：从基准到目标路径上各纪元位图的并集，去掉最近公共祖先以上的部分 | [14](14-memory-diff-tree.md) |
| **物化** | materialize | 把回滚集在目标时刻的内容聚成一个稀疏文件，连同位图交给 FC | [14](14-memory-diff-tree.md) |
| **全量根** | full root | 树根存完整内存，使整棵树自足；它的回滚因子恒为全 1 | [14](14-memory-diff-tree.md) |
| **隐藏条目** | hidden entry | 在树里、不在 API 里的条目：被删但仍有后代要经过它解析 | [14](14-memory-diff-tree.md)、[20](20-failure-semantics.md) |
| **合并** | fold / compact | 隐藏、非基准、恰有一个子节点的条目并入它的子节点：子节点接管它的父节点，回滚集取并集，内容以子节点为准，rootfs 层成对合并。在 delete 结尾同步执行，由 `CHECKPOINT_COMPACT` 控制 | [14](14-memory-diff-tree.md)、[16](16-disk-layering.md) |
| **条目数上界** | 2V + 1 | 合并追平后，一个沙箱的条目数不超过 2 × 可见数 + 1 | [14](14-memory-diff-tree.md) |
| **字节配额** | `CHECKPOINT_MAX_BYTES_PER_SANDBOX` | 每沙箱 checkpoint 占用磁盘实际块数的上限，默认关；超限拒绝 checkpoint（reason `checkpoint_bytes_limit`），restore 不受限 | [06](06-configuration-and-capacity.md)、[02](02-semantics-and-limits.md) |
| **层引用计数** | layer refs | 每个封存层被多少个已发布视图和活账本引用；最后一个引用消失时回收该层 | [16](16-disk-layering.md) |
| **断链** | broken chain | 纪元丢失，此后下一次 checkpoint 是新的全量根 | [20](20-failure-semantics.md) |
| **封存** | seal | 把活写层原地变成只读层，零数据搬运 | [16](16-disk-layering.md) |
| **封存层** | sealed layer，`layer-<uuid>` | 封存后的只读层文件 | [16](16-disk-layering.md) |
| **视图** | rootfs view | 某个 checkpoint 的磁盘由哪些层按什么顺序叠成 | [16](16-disk-layering.md) |
| **块归属索引** | `LayerStack` | 每块由哪一层提供的数组，读一块只查一次，与层数无关 | [16](16-disk-layering.md) |
| **合并 header** | merged header | "模板映射 + 各封存层恒等映射"的序列化结果 | [16](16-disk-layering.md) |
| **提交点** | commit point | 回滚中开始改 guest 状态的那一刻 | [17](17-in-place-rollback.md) |
| **撕裂** | torn / `Faulted` | 虚机介于两个时刻之间，只能重建沙箱 | [20](20-failure-semantics.md) |
| **账本污染** | poisoned | 账本与活的层栈不一致，此后拒绝服务直到下一次成功 restore | [16](16-disk-layering.md) |
| **冻结窗口** | freeze window | 虚机从暂停到恢复的时间；业务感受到的停顿，不用来判定达标 | [10](10-background.md)、[24](24-performance-methodology.md) |

### 1.2 平台与底层

| 术语 | 全称 | 一句话 | 详见 |
|---|---|---|---|
| **microVM** | —— | 保留硬件隔离、设备模型砍到最小的虚拟机 | [10](10-background.md) |
| **Firecracker** | —— | AWS 开源的 microVM 监控器 | [10](10-background.md) |
| **HDBSS** | Hardware Dirty state tracking Structure | ARMv9.5 的硬件脏页跟踪，950 上有，920B 上没有 | [13](13-dirty-page-tracking-and-hdbss.md) |
| **kvm-wp** | —— | KVM 软件写保护脏页跟踪，每个干净页第一次写陷出一次 | [13](13-dirty-page-tracking-and-hdbss.md) |
| **Stage-2** | —— | KVM 管理的第二层地址翻译（IPA → 宿主物理） | [13](13-dirty-page-tracking-and-hdbss.md) |
| **DBM** | Dirty Bit Modifier | 页表项上让硬件自己标脏的位 | [13](13-dirty-page-tracking-and-hdbss.md) |
| **VHE** | Virtualization Host Extensions | ARM 虚拟化宿主扩展，HDBSS 要求它 | [13](13-dirty-page-tracking-and-hdbss.md) |
| **UFFD** | userfaultfd | 用户态处理缺页的内核机制 | [10](10-background.md) |
| **NBD** | Network Block Device | 把用户态对象暴露成内核块设备 | [16](16-disk-layering.md) |
| **VMGenID** | VM Generation ID | ACPI 设备，代号变化告诉 guest"你被恢复了" | [18](18-rollback-pitfalls.md) |
| **conntrack** | connection tracking | 宿主内核的连接跟踪表，restore 时要清掉沙箱地址的条目 | [18](18-rollback-pitfalls.md) |
| **稀疏文件** | sparse file | 未写过的区间不占物理块 | [14](14-memory-diff-tree.md) |
| **MADV_POPULATE** | `MADV_POPULATE_READ` / `MADV_POPULATE_WRITE` | 在系统调用里预先把映射的页缺进来，Linux 5.14+ | [21](21-state-concurrency-durability.md) |
| **STW** | stop-the-world | Go GC 暂停所有 goroutine 的阶段 | [21](21-state-concurrency-durability.md) |

### 1.3 e2b 的概念

| 术语 | 一句话 | 详见 |
|---|---|---|
| **template** | 沙箱的出厂状态，构建产出一份可反复加载的快照 | [10](10-background.md) |
| **sandbox** | 一个运行中的沙箱实例 | [10](10-background.md) |
| **envd** | guest 里执行命令、读写文件的守护进程 | [10](10-background.md) |
| **orchestrator** | 宿主上管沙箱生命周期、网络、存储、代理的进程 | [12](12-architecture.md) |
| **client-proxy** | 集群入口代理，把客户端请求转给所在节点的 orchestrator | [12](12-architecture.md) |
| **SandboxID / ExecutionID / LifecycleID** | 三个不同层次的身份标识 | [11](11-baseline-goals-and-native.md) |

### 1.4 测试与测量

| 术语 | 一句话 | 详见 |
|---|---|---|
| **静默失效** | 断言全过、内容全对，坏的是成本、覆盖范围或判据本身 | [23](23-testing-and-functional-verification.md) |
| **`mem_mode` / `memMode`** | 服务端每次 checkpoint 回报本次是 `full` 还是 `incremental`；除了沙箱的第一个，出现 `full` 就是有问题 | [07](07-observability-reference.md)、[23](23-testing-and-functional-verification.md) |
| **条件标签** | 一个耗时或体积数字必须随身携带的机型、脏页后端、产物盘、模板规格、二进制、脚本参数 | [24](24-performance-methodology.md) |
| **名义改动量 / 档位** | 基准里每次迭代请求改多少（内存:文件 = 3:1），判定表按它分档 | [24](24-performance-methodology.md) |
| **时机成本** | "写完立刻拍"减"`sync` 后再拍"，回写争用的代价，不是快照机制的代价 | [24](24-performance-methodology.md) |
| **浅集 / 深集** | restore 回退一步与回到链根两种形态，把回滚集大小和链深拆开 | [24](24-performance-methodology.md) |
| **底噪** | 冻结窗口采样器自身的间隔抖动；冻结值不高于 2 倍底噪时不作数 | [24](24-performance-methodology.md) |
| **工作集**（对比脏页） | 被碰过的页集；脏页是被写过的页集。把读也算脏时增量退化成工作集 | [13](13-dirty-page-tracking-and-hdbss.md) |
| **`REFUSED` / `BROKEN`** | 兼容矩阵的两种非 OK 判定：前者是安全的拒绝（边界），后者是真问题 | [23](23-testing-and-functional-verification.md) |
| **真实链深** | 沿 `parent_id` 走到根的最大深度，含隐藏条目 | [24](24-performance-methodology.md) |
| **上限 60 / 滚动保留 10** | 长跑的两种负载：服务端数量上限 60；客户端只保留最新 10 个、删最旧 | [24](24-performance-methodology.md) |

---

## 2. 代码地图

代码在 openEuler 交付仓库 `KASandbox_0904`：开发分支 `deltabox-dev`，交付分支 `deltabox`（[附录 B](B-extending.md)）。三个组件同仓：

| 组件 | 目录 | 下表路径的前缀 | 单元测试 |
|---|---|---|---|
| orchestrator（Go） | `packages/orchestrator/` | §2.1 的 `internal/...` 前加 `packages/orchestrator/` | 同目录 `*_test.go` |
| Firecracker（Rust） | `firecracker/` | §2.2 的 `src/...` 前加 `firecracker/` | 源文件内 `#[cfg(test)]` |
| Python SDK | `py-sdk/` | —— | `py-sdk/tests/test_checkpoint_errors.py` |

### 2.1 orchestrator

| 关注点 | 文件 | 关键符号 |
|---|---|---|
| 服务入口、编排、失败分级、配额拒绝 | `internal/checkpoint/service.go` | `Port`（49984）、`Handles`、`ServeCheckpoint`、`create`、`restore`、`failCreate`、`fullRootEnabled`、`envdRestoreTimeout`、`refuseCheckpointBytesLimit`、`warnIfByteLimitTooSmall` |
| 树账本、回滚集、内容解析、删除、锁外回收 | `internal/checkpoint/store.go` | `Entry`、`Store`、`LockSandbox`、`Prepare`、`Commit`、`CommitHidden`、`InvalidateBase`、`revertPathLocked`、`entryBitmap`、`MaterializeRevert`、`writeRevertMem`、`Delete`、`pruneLocked`、`RemoveSandbox`、`reclaim`、`children`（子节点计数表）、`runIndexWriter`（`CHECKPOINT_DEBUG_INDEX`） |
| 合并（内存） | `internal/checkpoint/compact.go` | `Compaction`、`CompactMaxPerOp`、`queueCompactLocked`、`compactCandidateLocked`、`nextCompactLocked`、`runCompaction`、`fold`、`foldMemory`、`switchFoldLocked`、`layerHoldersUnchangedLocked`、`switchLayersLocked` |
| 合并（rootfs 层） | `internal/checkpoint/compact_layers.go` | `planLayerFold`、`foldLayers`、`rewriteHeader`、`copyBlocks` |
| 层引用计数 | `internal/checkpoint/layer_refs.go` | `refLayersLocked`、`unrefLayersLocked`、`checkLayerRefs` |
| 字节配额 | `internal/checkpoint/quota.go` | `MaxBytesPerSandbox`、`CheckpointBytesLimitError`、`atByteLimitLocked`、`floorBytesLocked`、`UsedBytes`、`chargeLocked`、`releaseLayerLocked`、`checkUsedBytes` |
| FCDB 位图 | `internal/checkpoint/bitmap.go` | `readDirtyBitmap`、`writeTo`、`merge`、`contains`、`allOnesBitmap` |
| 磁盘层账本 | `internal/checkpoint/rootfs.go` | `AppendLayer`、`SetRootfsToEntry`、`DiskViewForEntry`、`identityMappings`、`writeLayerMeta`、`readLayerMeta` |
| 启动能力行 | `internal/checkpoint/capabilities.go`；`main.go` | `Capabilities`、`DescribeCapabilities`；`reportCheckpointCapabilities` |
| metrics | `internal/checkpoint/metrics.go` | `callStats`、`record`、`recordCompaction` |
| 故障注入 | `internal/checkpoint/faults.go` | `CHECKPOINT_FAULT_INJECT`、`faultInjected` |
| GC 停顿取证 | `internal/runtimemetrics/runtimemetrics.go` | `Start`（`CHECKPOINT_RUNTIME_METRICS`） |
| 暂停窗口 | `internal/sandbox/checkpoint.go` | `CheckpointToFiles`、`RollbackInPlace`、`AssembleView`、`RollbackTornError` |
| conntrack 后台清理与汇合 | `internal/sandbox/conntrack_flush.go` | `startConntrackFlush`、`join`、`recordConntrackReport` |
| 进程读盘量 | `internal/sandbox/diskreads.go` | `diskReadBytes`、`diskReadSince` |
| 分阶段计时 | `internal/sandbox/phasetimings.go` | `PhaseTimings` |
| 写层与换层 | `internal/sandbox/block/overlay.go` | `Overlay`、`Seal`、`ResetView`、`EjectCache` |
| 封存层读路径 | `internal/sandbox/block/layerstack.go` | `LayerStack`、`AppendLayerWithBlocks`、`claimRun` |
| 写层实现 | `internal/sandbox/block/cache.go` | `Cache`、`NewCache`、`MarkCached`、`DirtyOffsets`、`MoveFile`、`CloseKeepFile`、`ExportToDiff` |
| 按块号的位图 | `internal/sandbox/block/blockset.go` | `blockSet`、`blockSetBatch`、`forEachRun` |
| 拷贝前预取页 | `internal/sandbox/block/populate.go` | `probePopulate`、`populateMapped` |
| 四步封存与导出 | `internal/sandbox/rootfs/nbd.go` | `SealLayer`、`ResetView`、`ExportDiff` |
| 脏页跟踪默认值 | `internal/sandbox/fc/dirtytracking.go` | `resolveTrackDirtyPages`、`hardwareDirtyTracking` |
| FC 两个端点的客户端 | `internal/sandbox/fc/rollback.go` | `rollbackSnapshot`、`saveDirtyBitmap`、`RollbackFaultedError` |
| 沙箱 netns 表清理 | `internal/sandbox/network/conntrack.go` | `FlushConntrack` |
| 宿主表原始扫描 | `internal/sandbox/network/conntrack_scan.go` | `deleteHostConntrackByRawScan`、`origTupleIPv4Raw` |
| 宿主表清扫批处理与选路 | `internal/sandbox/network/conntrack_sweeper.go` | `sweepHostConntrack`、`sweepUsesFilter`、`deleteHostConntrackBatch` |
| 过滤 dump 路径 | `internal/sandbox/network/conntrack_filter.go` | `dumpFilteredConntrack`、`deleteFilteredHostConntrack` |
| 代理拦截 | `internal/proxy/proxy.go` | `checkpoint.Handles` 那段包装 |
| 代理 full duplex | `packages/shared/pkg/proxy/handler.go` | `ServeHTTP` 前的 `EnableFullDuplex` |
| 原生路径（对照） | `internal/server/sandboxes.go`、`internal/sandbox/sandbox.go` | `Server.Pause`、`Sandbox.Pause` |
| 被注释掉的 uffd 写保护 | `internal/sandbox/uffd/userfaultfd/userfaultfd.go` | `// copyMode \|= UFFDIO_COPY_MODE_WP` |

### 2.2 Firecracker

| 关注点 | 文件 | 关键符号 |
|---|---|---|
| 原地回滚主体 | `src/vmm/src/rollback.rs` | `rollback_snapshot`、`validate_topology`、`quiesce_devices`、`apply_device_states`、`apply_net_rx_cache`、`save_dirty_bitmap`、`log_queue_diagnostics`、`RollbackError::faults_vm` |
| 脏页后端 | `src/vmm/src/vstate/vm.rs` | `setup_dirty_tracking`、`DirtyTrackingBackend`、`snapshot_memory_to_file`、`mincore_bitmap`、`test_full_snapshot_sidecar_is_all_ones` |
| HDBSS ioctl | `src/vmm/src/arch/aarch64/vm.rs` | `enable_hdbss` |
| 内存操作与 FCDB | `src/vmm/src/vstate/memory.rs` | `dump_dirty`、`restore_dirty`、`store_dirty_bitmap`、`reset_dirty`、`serialize_dirty_bitmap`、`deserialize_dirty_bitmap`、`DIRTY_BITMAP_VERSION` |
| 两个构建路径 | `src/vmm/src/builder.rs` | `build_microvm_for_boot`、`build_microvm_from_snapshot` |
| vCPU 原地恢复 | `src/vmm/src/lib.rs`、`src/vmm/src/arch/aarch64/vcpu.rs` | `restore_vcpu_states_in_place`、`RestoreState` |
| RX 缓存重建 | `src/vmm/src/devices/virtio/net/device.rs` | `rollback_rx_buffers`、`parse_rx_descriptors` |
| 端点参数 | `src/vmm/src/vmm_config/snapshot.rs` | `RollbackSnapshotParams`、`SaveDirtyBitmapParams`、`RollbackTimings` |
| 路由 | `src/firecracker/src/api_server/request/snapshot.rs` | `parse_put_snapshot` |
| 动作分发 | `src/vmm/src/rpc_interface.rs` | `VmmAction::{RollbackSnapshot, SaveDirtyBitmap}` |
| vmm 测试要引导的内核 | `src/vmm/src/test_utils/mock_resources/` | `test_pe.bin`、`test_elf.bin` |

### 2.3 Python SDK（`py-sdk/`）

| 关注点 | 文件 |
|---|---|
| 同步 / 异步接口 | `e2b/sandbox_sync/checkpoint.py`、`e2b/sandbox_async/checkpoint.py` |
| 异常族（基类 `CheckpointException` + 9 个子类，含 `CheckpointBytesLimitException`，它是 `CheckpointTooManyException` 的子类） | `e2b/exceptions.py` |
| reason → 异常类映射 | `e2b/sandbox/checkpoint/errors.py` |
| 返回类型 | `e2b/sandbox/checkpoint/types.py` |
| checkpoint 请求默认超时 `CHECKPOINT_REQUEST_TIMEOUT` | `e2b/connection_config.py` |
| 重新生成 RPC 桩 | `scripts/regen-checkpoint-pb.py`（proto 在仓库根的 `spec/checkpointd/checkpoint/checkpoint.proto`） |

### 2.4 测试脚本（`e2b-infra/rollback/scripts/`）

| 目录 | 文件 | 讲解 |
|---|---|---|
| `950/` | `run.sh`：smoke / func / perf / long 一站式入口 | [09](09-acceptance-runbook.md)、[23](23-testing-and-functional-verification.md) |
| `acceptance/`（单文件、互不依赖） | `checkpoint_verify.py`（59 项正确性）、`checkpoint_bench.py`（时机成本与产物实测）、`checkpoint_concurrent.py`（并发 A/B/C/D）、`checkpoint_bench_v2.py`（与进程级快照对照的口径）、`native_snapshot_bench.py`（原生 snapshot 同一张档位表） | [23](23-testing-and-functional-verification.md)、[24](24-performance-methodology.md) |
| `crtest/` | `crtest/cases/t11.py … t41.py`（20 个用例）、`crtest/common.py`、`tests/`（离线单测）、`sdktests/test_checkpoint_errors.py`、`portability/preflight-customer.sh`、`probe950/` | [23](23-testing-and-functional-verification.md) |
| `crtest/bench/` | `bench_tiers.py`、`compliance.py`、`analyze.py`、`serial_restore.py` | [24](24-performance-methodology.md) |
| `dev/`（共享 `lib.py`） | `correctness.py`、`pause_verify.py`、`compat_matrix.py`、`hdbss_evidence.py`（配 `cap_test.c`）、`loop.py`、`timing.py`、`bench-ckpt.py` + `freeze_probe.py`、`probe-ramp.py`、`01-check-host.sh` … `04-verify-runtime.sh`、`run-all.sh` | [23](23-testing-and-functional-verification.md)、[24](24-performance-methodology.md) |
| `probes/` | `pb2.py` … `pb5.py`、`probe_dirty.py`、`uffdwp_probe.c`：脏页判据的定向探针，不属于验收流程 | —— |

---

## 3. 文件格式索引

产物目录布局见 [12](12-architecture.md)。每个 checkpoint 一个目录 `ckpt_<UnixNano>/`，沙箱级的封存层在 `layers/`。

| 文件 | 格式 | 写者 | 读者 | 详见 |
|---|---|---|---|---|
| `snapfile` | FC vmstate（二进制） | FC | FC | —— |
| `mem_diff` | 稀疏文件，页写在各自的偏移上；全量根也用这个名字 | FC | orchestrator | [14](14-memory-diff-tree.md) |
| `mem_bitmap` | FCDB 位图（§3.1） | FC | orchestrator | [15](15-firecracker-api-contract.md) |
| `mem_diff.<id>`、`mem_bitmap.<id>` | 合并后子节点的新内存文件与并集侧车，`<id>` 是被并入条目的 ID；用新名字而不是改名，避免切换失败时两个条目共享 inode | orchestrator | orchestrator | [14](14-memory-diff-tree.md) |
| `rootfs.header` | 序列化的合并 header | orchestrator | orchestrator | [16](16-disk-layering.md) |
| `layers/layer-<uuid>` | 原写层文件改名而来，块在原偏移上 | guest（经 NBD） | 运行中的沙箱 / restore | [16](16-disk-layering.md) |
| `layers/layer-<uuid>.meta` | 8 字节小端一条的块偏移列表 | orchestrator | orchestrator | [16](16-disk-layering.md) |
| `layers/layer-<buildID>.<id>` | 层合并产生的合成层（及其 `.meta`） | orchestrator | 同上 | [16](16-disk-layering.md) |
| `manifest.json` | 单条目的完整 JSON | orchestrator | 无人（事后排查） | [12](12-architecture.md) |
| `index.json` | 沙箱的条目清单；**默认不写**，`CHECKPOINT_DEBUG_INDEX=true` 时后台最多每 5 s 写一次 | orchestrator | 无人（调试） | [12](12-architecture.md) |
| `timings.json` | `{计时键: 毫秒}`，每个 checkpoint 一份 | orchestrator | 基准脚本 | [07](07-observability-reference.md) |
| `last-restore-timings.json` | 沙箱目录下，每次 restore 覆写 | orchestrator | 基准脚本 | [07](07-observability-reference.md) |
| `revert_mem.tmp`、`revert_bitmap.tmp` | 物化结果：稀疏内存文件与 FCDB，写在目标 checkpoint 目录下，只存在于一次 restore 期间 | orchestrator | FC | [14](14-memory-diff-tree.md) |
| `<store>/.trash-<uuid>` | 被丢弃的整个沙箱目录，锁内改名、锁外删除；启动时清空 store 根会顺带删掉残留 | orchestrator | —— | [21](21-state-concurrency-durability.md) |

### 3.1 FCDB 逐字节

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 4 | magic `"FCDB"` |
| 4 | 4 | version，u32 小端，当前 `1`（`DIRTY_BITMAP_VERSION`） |
| 8 | 8 | `page_size`，u64 小端 |
| 16 | 8 | `num_pages`，u64 小端 |
| 24 | 8 × ⌈num_pages / 64⌉ | 位图字，u64 小端 |

第 `w` 个字的第 `i` 位对应页索引 `w × 64 + i`。页索引是内存文件偏移 ÷ 页大小，不是 guest 物理地址。
尾字超出 `num_pages` 的位必须为 0。FC 对全量快照写全 1 的侧车；orchestrator 对全量根不读侧车，直接取全 1。

---

## 4. 接口速查

SDK 与 RPC 的调用方式见 [01](01-quickstart.md)，超时、不重放与错误处置见 [03](03-errors-timeouts-concurrency.md)。

| SDK 方法 | RPC（宿主侧端口 49984） |
|---|---|
| `sandbox.checkpoint.create(name=None)` | `/checkpoint.Checkpoint/CreateCheckpoint` |
| `sandbox.checkpoint.restore(checkpoint_id)` | `/checkpoint.Checkpoint/RestoreCheckpoint` |
| `sandbox.checkpoint.delete(checkpoint_id)` | `/checkpoint.Checkpoint/DeleteCheckpoint` |
| `sandbox.checkpoint.list()` | `/checkpoint.Checkpoint/ListCheckpoints` |
| `sandbox.checkpoint.is_available()`（旧名 `is_running()`） | `/health`，由宿主应答，不进沙箱 |

Firecracker 的三处扩展：`CreateSnapshotParams.dirty_bitmap_path`（已有端点的可选字段）、`PUT /snapshot/rollback`、
`PUT /snapshot/save-dirty-bitmap`（均为新端点）。

---

## 5. 不变量速查

全书的不变量共 19 条，**编号、定义、破坏后果和是否静默只在 [20 §9](20-failure-semantics.md#9-不变量清单) 列出**，其他篇引用时都用那里的编号。
改代码前过一遍那张表；其中 #15–#19 是合并、full 根回滚因子、子节点计数表、层引用计数与字节计数这几项新机制带来的，
属性测试怎么守它们见 [23](23-testing-and-functional-verification.md)。

---

## 6. 篇目表

| # | 文件 | 一句话 |
|---|---|---|
| 导读 | [README](README.md) | 版本、署名与阅读入口 |
| 00 | [导读与总览](00-overview.md) | 定位、能力边界、按角色的阅读路径 |
| **一 使用指南** | | |
| 01 | [快速上手](01-quickstart.md) | 装 SDK 覆盖层，checkpoint / restore / list / delete 示例 |
| 02 | [语义与边界](02-semantics-and-limits.md) | 能回滚什么、回滚不了什么，删除与空间回收 |
| 03 | [错误、超时与并发调用](03-errors-timeouts-concurrency.md) | 错误总表、SDK 超时与不重放、流截断 |
| 04 | [性能预期](04-performance-expectations.md) | 成本模型与判定结论 |
| **二 部署与运维** | | |
| 05 | [部署前提与检查清单](05-deployment-prerequisites.md) | HDBSS、内核、产物盘、二进制配对、部署后自检 |
| 06 | [配置参考与容量规划](06-configuration-and-capacity.md) | 唯一的开关总表、能力行、容量规划 |
| 07 | [可观测性参考](07-observability-reference.md) | 能力行、`memMode`、计时键与 timings 全字段 |
| 08 | [排障](08-troubleshooting.md) | 按症状查 |
| 09 | [上机验收](09-acceptance-runbook.md) | `run.sh` 各档、自检、结果回传 |
| **三 原理与设计** | | |
| 10 | [背景](10-background.md) | 沙箱、microVM 与快照原理 |
| 11 | [基线、目标与原生对比](11-baseline-goals-and-native.md) | 原生 snapshot、设计目标、对比表 |
| 12 | [总体架构](12-architecture.md) | 部件、职责、目录布局 |
| 13 | [脏页跟踪与 HDBSS](13-dirty-page-tracking-and-hdbss.md) | 增量的前提与判据差异 |
| 14 | [内存差分树](14-memory-diff-tree.md) | 回滚集、物化、删除 / 隐藏 / 合并 |
| 15 | [FC 接口契约](15-firecracker-api-contract.md) | 两个进程之间的约定 |
| 16 | [磁盘分层](16-disk-layering.md) | 零拷贝封存、层引用计数、层合并、视图装配 |
| 17 | [进程内原地回滚](17-in-place-rollback.md) | 回滚的各阶段与提交点 |
| 18 | [原地回滚特有的问题](18-rollback-pitfalls.md) | RX 缓存、VMGenID、conntrack、时间 |
| 19 | [端到端走查](19-end-to-end.md) | 完整走查与顺序约束 |
| 20 | [失败语义与不变量](20-failure-semantics.md) | 宁可报错不可静默损坏 |
| 21 | [状态、并发与持久性](21-state-concurrency-durability.md) | 锁、锁外删除、节点级效应 |
| 22 | [生命周期边界的推导](22-lifecycle-reasoning.md) | 为什么脱离沙箱恢复不了 |
| **四 测试与证据** | | |
| 23 | [测试体系与功能验证](23-testing-and-functional-verification.md) | 静默失效、测试矩阵、各脚本判据、crtest |
| 24 | [性能口径与方法](24-performance-methodology.md) | 口径、负载构造、脚本分工、长跑测法 |
| 25 | [实测结果与判定](25-results-and-compliance.md) | 当前代码的全部数字与缺口清单 |
| **附录** | | |
| A | 本篇 | 查询入口 |
| B | [继续开发](B-extending.md) | 仓库、改动指引、自查清单、已知方向 |
