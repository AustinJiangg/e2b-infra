# 附录 A · 术语、代码地图与文件格式

> 全书的查询入口，不用从头读。术语、代码位置（以 `deltabox-dev@57a3063` 为准）、产物文件格式、不变量速查、章目表。
> 环境变量与开关见 [27](27-configuration-and-capacity.md)，错误码与异常见 [25](25-errors-timeouts-concurrency.md)，
> 计时键见 [28](28-observability-reference.md)。

---

## 1. 术语表

### 1.1 本方案的概念

| 术语 | 英文 / 标识 | 一句话 | 详见 |
|---|---|---|---|
| **checkpoint** | checkpoint | 沙箱某一时刻的状态记录，也指拍它的操作（与 restore 成对）。接口名里叫 `CreateCheckpoint` | [23](23-quickstart.md)、[06](06-memory-diff-tree.md) |
| **restore / 回滚** | restore / rollback | 把活着的沙箱原地退回某个 checkpoint，沙箱 ID 与 FC 进程不变 | [09](09-in-place-rollback.md) |
| **纪元** | epoch | 两次"脏页跟踪被重置"之间的时间段 | [06](06-memory-diff-tree.md) |
| **纪元位图 / 侧车** | `E_x`、`mem_bitmap`、sidecar | 记录一个纪元里写过哪些页 | [06](06-memory-diff-tree.md)、[08](08-firecracker-api-contract.md) |
| **差分树** | diff tree | 以 `ParentID` 组织的 checkpoint 树 | [06](06-memory-diff-tree.md) |
| **基准** | base | 下一个 checkpoint 的父节点；每沙箱一个 | [06](06-memory-diff-tree.md) |
| **回滚集** | revert set | restore 时需要写回的页集合：从基准到目标路径上各纪元位图的并集，去掉最近公共祖先以上的部分 | [06](06-memory-diff-tree.md) |
| **物化** | materialize | 把回滚集在目标时刻的内容聚成一个稀疏文件，连同位图交给 FC | [06](06-memory-diff-tree.md) |
| **全量根** | full root | 树根存完整内存，使整棵树自足；它的回滚因子恒为全 1 | [06](06-memory-diff-tree.md) |
| **隐藏条目** | hidden entry | 在树里、不在 API 里的条目：被删但仍有后代要经过它解析 | [06](06-memory-diff-tree.md)、[12](12-failure-semantics.md) |
| **合并** | fold / compact | 隐藏、非基准、恰有一个子节点的条目并入它的子节点：子节点接管它的父节点，回滚集取并集，内容以子节点为准，rootfs 层成对合并。在 delete 结尾同步执行，由 `CHECKPOINT_COMPACT` 控制 | [06](06-memory-diff-tree.md)、[07](07-disk-layering.md) |
| **条目数上界** | 2V + 1 | 合并追平后，一个沙箱的条目数不超过 2 × 可见数 + 1 | [06](06-memory-diff-tree.md) |
| **字节配额** | `CHECKPOINT_MAX_BYTES_PER_SANDBOX` | 每沙箱 checkpoint 占用磁盘实际块数的上限，默认关；超限拒绝 checkpoint（reason `checkpoint_bytes_limit`），restore 不受限 | [27](27-configuration-and-capacity.md)、[24](24-semantics-and-limits.md) |
| **层引用计数** | layer refs | 每个封存层被多少个已发布视图和活账本引用；最后一个引用消失时回收该层 | [07](07-disk-layering.md) |
| **断链** | broken chain | 纪元丢失，此后下一次 checkpoint 是新的全量根 | [12](12-failure-semantics.md) |
| **封存** | seal | 把活写层原地变成只读层，零数据搬运 | [07](07-disk-layering.md) |
| **封存层** | sealed layer，`layer-<uuid>` | 封存后的只读层文件 | [07](07-disk-layering.md) |
| **视图** | rootfs view | 某个 checkpoint 的磁盘由哪些层按什么顺序叠成 | [07](07-disk-layering.md) |
| **块归属索引** | `LayerStack` | 每块由哪一层提供的数组，读一块只查一次，与层数无关 | [07](07-disk-layering.md) |
| **合并 header** | merged header | "模板映射 + 各封存层恒等映射"的序列化结果 | [07](07-disk-layering.md) |
| **提交点** | commit point | 回滚中开始改 guest 状态的那一刻 | [09](09-in-place-rollback.md) |
| **撕裂** | torn / `Faulted` | 虚机介于两个时刻之间，只能重建沙箱 | [12](12-failure-semantics.md) |
| **账本污染** | poisoned | 账本与活的层栈不一致，此后拒绝服务直到下一次成功 restore | [07](07-disk-layering.md) |
| **冻结窗口** | freeze window | 虚机从暂停到恢复的时间；业务感受到的停顿，不用来判定达标 | [01](01-background.md)、[20](20-performance-methodology.md) |

### 1.2 平台与底层

| 术语 | 全称 | 一句话 | 详见 |
|---|---|---|---|
| **microVM** | —— | 保留硬件隔离、设备模型砍到最小的虚拟机 | [01](01-background.md) |
| **Firecracker** | —— | AWS 开源的 microVM 监控器 | [01](01-background.md) |
| **HDBSS** | Hardware Dirty state tracking Structure | ARMv9.5 的硬件脏页跟踪，950 上有，920B 上没有 | [05](05-dirty-page-tracking-and-hdbss.md) |
| **kvm-wp** | —— | KVM 软件写保护脏页跟踪，每个干净页第一次写陷出一次 | [05](05-dirty-page-tracking-and-hdbss.md) |
| **Stage-2** | —— | KVM 管理的第二层地址翻译（IPA → 宿主物理） | [05](05-dirty-page-tracking-and-hdbss.md) |
| **DBM** | Dirty Bit Modifier | 页表项上让硬件自己标脏的位 | [05](05-dirty-page-tracking-and-hdbss.md) |
| **VHE** | Virtualization Host Extensions | ARM 虚拟化宿主扩展，HDBSS 要求它 | [05](05-dirty-page-tracking-and-hdbss.md) |
| **UFFD** | userfaultfd | 用户态处理缺页的内核机制 | [01](01-background.md) |
| **NBD** | Network Block Device | 把用户态对象暴露成内核块设备 | [07](07-disk-layering.md) |
| **VMGenID** | VM Generation ID | ACPI 设备，代号变化告诉 guest"你被恢复了" | [10](10-rollback-pitfalls.md) |
| **conntrack** | connection tracking | 宿主内核的连接跟踪表，restore 时要清掉沙箱地址的条目 | [10](10-rollback-pitfalls.md) |
| **稀疏文件** | sparse file | 未写过的区间不占物理块 | [06](06-memory-diff-tree.md) |
| **MADV_POPULATE** | `MADV_POPULATE_READ` / `MADV_POPULATE_WRITE` | 在系统调用里预先把映射的页缺进来，Linux 5.14+ | [13](13-state-concurrency-durability.md) |
| **STW** | stop-the-world | Go GC 暂停所有 goroutine 的阶段 | [13](13-state-concurrency-durability.md) |

### 1.3 e2b 的概念

| 术语 | 一句话 | 详见 |
|---|---|---|
| **template** | 沙箱的出厂状态，构建产出一份可反复加载的快照 | [01](01-background.md) |
| **sandbox** | 一个运行中的沙箱实例 | [01](01-background.md) |
| **envd** | guest 里执行命令、读写文件的守护进程 | [01](01-background.md) |
| **orchestrator** | 宿主上管沙箱生命周期、网络、存储、代理的进程 | [04](04-architecture.md) |
| **client-proxy** | 集群入口代理，把客户端请求转给所在节点的 orchestrator | [04](04-architecture.md) |
| **hyperloop** | orchestrator 在宿主上给每个沙箱提供的 HTTP 服务，guest 经 `192.0.2.1:80` 访问（宿主侧端口 5010）；部署脚本的 80 → 3002 转发目前劫持了这条链路 | [26 §5.1](26-deployment-prerequisites.md#51-80-端口的转发规则劫持-hyperloop-请求) |
| **SandboxID / ExecutionID / LifecycleID** | 三个不同层次的身份标识 | [02](02-e2b-native-snapshot.md) |

### 1.4 测试与测量

| 术语 | 一句话 | 详见 |
|---|---|---|
| **静默失效** | 断言全过、内容全对，坏的是成本、覆盖范围或判据本身 | [19](19-testing-and-functional-verification.md) |
| **`mem_mode` / `memMode`** | 服务端每次 checkpoint 回报本次是 `full` 还是 `incremental`；除了沙箱的第一个，出现 `full` 就是有问题 | [28](28-observability-reference.md)、[19](19-testing-and-functional-verification.md) |
| **条件标签** | 一个耗时或体积数字必须随身携带的机型、脏页后端、产物盘、模板规格、二进制、脚本参数 | [20](20-performance-methodology.md) |
| **名义改动量 / 档位** | 基准里每次迭代请求改多少（内存:文件 = 3:1），判定表按它分档 | [20](20-performance-methodology.md) |
| **时机成本** | "写完立刻拍"减"`sync` 后再拍"，回写争用的代价，不是快照机制的代价 | [20](20-performance-methodology.md) |
| **浅集 / 深集** | restore 回退一步与回到链根两种形态，把回滚集大小和链深拆开 | [20](20-performance-methodology.md) |
| **底噪** | 冻结窗口采样器自身的间隔抖动；冻结值不高于 2 倍底噪时不作数 | [20](20-performance-methodology.md) |
| **工作集**（对比脏页） | 被碰过的页集；脏页是被写过的页集。把读也算脏时增量退化成工作集 | [05](05-dirty-page-tracking-and-hdbss.md)、[15](15-native-increment-diagnosis.md) |
| **`REFUSED` / `BROKEN`** | 兼容矩阵的两种非 OK 判定：前者是安全的拒绝（边界），后者是真问题 | [19](19-testing-and-functional-verification.md) |
| **真实链深** | 沿 `parent_id` 走到根的最大深度，含隐藏条目 | [20](20-performance-methodology.md) |
| **上限 60 / 滚动保留 10** | 长跑的两种常规负载：服务端数量上限 60；客户端只保留最新 10 个、删最旧。另有只用来找问题、耗时不进判定的无上限压测 | [20](20-performance-methodology.md)、[22](22-long-run-and-concurrency.md) |
| **走平** | 长跑中资源量（占盘、目录数、真实链深等）在最后 15 min 的最小二乘斜率接近 0；只看终值不算 | [20](20-performance-methodology.md) |

### 1.5 原生快照的精确增量

这一组术语属于 e2b 原生 pause 路径上的独立修复（第四部分），不是本方案 checkpoint / restore 的一部分；写法以 [15](15-native-increment-diagnosis.md)–[17](17-native-increment-cost-and-verification.md) 为准。

| 术语 | 英文 / 标识 | 一句话 | 详见 |
|---|---|---|---|
| **判 / 存 / 填** | ① ② ③ | 原生 pause 内存增量的三层：① 判（这一代导出哪些页）、② 存（按 header 的 `BlockSize` 把页存进紧凑差分，header 记映射链）、③ 填（恢复时 uffd 按 guest 页缺页，从映射链取内容填进去） | [15](15-native-increment-diagnosis.md) |
| **驻留即脏** | resident-page criterion | 原生 pause 修复前的脏页判据"页驻留（mincore）且 uffd 写保护位未置"在 arm64 上塌缩成"驻留过就算脏"：arm64 6.6 没有 uffd 写保护，ARM 适配补丁注释掉了填页时的写保护，于是读进来的页也算脏、增量退化成工作集。修复后它仍是所有退路的落脚点 | [15](15-native-increment-diagnosis.md)、[05](05-dirty-page-tracking-and-hdbss.md) |
| **2 MiB 归并地板** | —— | 只把判据换成 4 KiB 写跟踪位图、差分块仍是 2 MiB 时，每代零散的本底写被归并到 2 MiB 块后留下的、与改动量无关的导出量下限 | [15](15-native-increment-diagnosis.md) |
| **粒度锁** | `BlockSize` == guest 页 | 把三层钉在 2 MiB 上的链条：guest 内存由 2 MiB 大页背衬 → uffd 只能按 2 MiB 填 → 起沙箱时硬检查"页大小等于块大小" → header 块大小逐代继承 | [15](15-native-increment-diagnosis.md) |
| **写跟踪差分** | 4 KiB 写跟踪差分；"4 KiB 存、2 MiB 拼" | 修复后的原生增量：① 判据换成 Firecracker 的写跟踪位图（`save-dirty-bitmap`），② header 块大小降到 4 KiB，③ 缺页仍按 2 MiB，内容从 4 KiB 映射拼回；Firecracker 零改动 | [16](16-native-increment-fix.md) |
| **三级取源** | `block.ReadPage` | 一次 2 MiB 缺页取内容的三条路：块等于页走原来的 `Slice`；单个映射覆盖整页时直接返回那一代的切片（零拷贝）；跨代的块才从缓冲池取 2 MiB、按 4 KiB 拼 | [16](16-native-increment-fix.md) |
| **自启动以来脏页集** | `DirtySinceBoot`；累积位图 | 每虚机一份：在 checkpoint、restore 让 Firecracker 清零写跟踪位图之前，把即将被忘掉的位图并进来；原生 pause 时并回写跟踪位图，保证导出 guest 启动以来写过的每一页。有任何一份并不进去就整个标为不可信 | [16](16-native-increment-fix.md)、[06](06-memory-diff-tree.md) |
| **退路（四道门）** | `tracked` / `tracked+accumulated` / `resident` | 跟踪没开、脏页集不可信、Firecracker 没有 `save-dirty-bitmap` 端点、并入时几何不符，任一成立就退回驻留判据；pause 日志写明本次导出依据是三者中的哪一种 | [16](16-native-increment-fix.md) |
| **宁可多导，不可少导** | —— | 判据改动的安全方向：多导一页只费时间与空间，少导一页会让恢复出的 guest 内存由两个时刻拼成，而 pause 本身报成功 | [15](15-native-increment-diagnosis.md) |

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
| 服务入口、编排、失败分级、配额拒绝 | `internal/checkpoint/service.go` | `Port`（49984）、`Handles`、`ServeCheckpoint`、`create`、`restore`、`failCreate`、`fullRootEnabled`、`envdRestoreTimeout`、`refuseCheckpointBytesLimit`、`warnIfByteLimitTooSmall`、`deleteFailure`（delete 的 404 / 成功 + WARN / 500 分流） |
| 树账本、回滚集、内容解析、删除、锁外回收 | `internal/checkpoint/store.go` | `Entry`、`Store`、`LockSandbox`、`Prepare`、`Commit`、`CommitHidden`、`InvalidateBase`、`revertPathLocked`、`entryBitmap`、`MaterializeRevert`、`writeRevertMem`、`Delete`、`ErrCheckpointNotFound`、`DeleteCleanupError`、`pruneLocked`、`RemoveSandbox`、`reclaim`、`children`（子节点计数表）、`runIndexWriter`（`CHECKPOINT_DEBUG_INDEX`） |
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
| `950/` | `run.sh`：smoke / func / perf / long 一站式入口 | [30](30-acceptance-runbook.md)、[19](19-testing-and-functional-verification.md) |
| `acceptance/`（五个单文件脚本互不依赖；`rolling_keep10.py` 从同目录导入 `checkpoint_concurrent.py`） | `checkpoint_verify.py`（59 项正确性）、`checkpoint_bench.py`（时机成本与产物实测）、`checkpoint_concurrent.py`（并发 A/B/C/D）、`checkpoint_bench_v2.py`（与进程级快照对照的口径）、`native_snapshot_bench.py`（原生 snapshot 同一张档位表）、`rolling_keep10.py`（滚动保留长跑） | [19](19-testing-and-functional-verification.md)、[20](20-performance-methodology.md)、[22](22-long-run-and-concurrency.md) |
| `crtest/` | `crtest/cases/t11.py … t41.py`（20 个用例）、`crtest/common.py`、`tests/`（离线单测）、`sdktests/test_checkpoint_errors.py`、`portability/preflight-customer.sh`、`probe950/` | [19](19-testing-and-functional-verification.md) |
| `crtest/bench/` | `bench_tiers.py`、`compliance.py`、`analyze.py`、`serial_restore.py` | [20](20-performance-methodology.md) |
| `dev/`（共享 `lib.py`） | `correctness.py`、`pause_verify.py`、`compat_matrix.py`、`hdbss_evidence.py`（配 `cap_test.c`）、`loop.py`、`timing.py`、`bench-ckpt.py` + `freeze_probe.py`、`probe-ramp.py`、`01-check-host.sh` … `04-verify-runtime.sh`、`run-all.sh` | [19](19-testing-and-functional-verification.md)、[20](20-performance-methodology.md) |
| `probes/` | `pb2.py` … `pb5.py`、`probe_dirty.py`、`uffdwp_probe.c`：脏页判据的定向探针，不属于验收流程 | —— |

### 2.5 原生快照的精确增量（原生 pause 路径）

提交 `c42d23e`（4 KiB 写跟踪差分）与 `bc19b45`（自启动以来脏页集），均在 `deltabox-dev@93ccb02` 之内；Firecracker 零改动。
下表 `internal/...` 前加 `packages/orchestrator/`。讲解见 [15](15-native-increment-diagnosis.md)–[17](17-native-increment-cost-and-verification.md)。

| 关注点 | 文件 | 关键符号 |
|---|---|---|
| 原生 pause 的内存导出入口与汇总日志 | `internal/sandbox/sandbox.go` | `Sandbox.Pause`、`pauseProcessMemory`；日志 `exporting the memfile diff of a native pause` |
| 判据选择与四道门 | `internal/sandbox/uffd/uffd.go` | `DiffMetadata`、`diffMetadata`、`residentDiffMetadata`、`AccumulateDirtyBitmap`、`MarkDirtyAccumulationUntrusted`、`DirtySinceBootStats` |
| 自启动以来脏页集 | `internal/sandbox/uffd/sinceboot.go` | `DirtySinceBoot`、`MergeFile`、`MergeBitmap`、`MarkUntrusted`、`UnionInto`、`Stats` |
| 内存后端接口（无 uffd 时的空实现） | `internal/sandbox/uffd/memory_backend.go`、`internal/sandbox/uffd/noop.go` | `MemoryBackend` 上的三个累积方法；`NoopMemory` |
| 并入时机（checkpoint 拍前 / 拍后、restore 两处） | `internal/sandbox/checkpoint.go` | `accumulateDirtyBitmap`、`createEpoch.beforeSnapshot`、`createEpoch.afterSnapshot` |
| 写跟踪位图落盘与解析、按 4 KiB 导出 | `internal/sandbox/fc/memory.go` | `TrackedDirtyMemory`、`ReadDirtyBitmapFile`、`ExportMemory`、`ErrDirtyTrackingUnavailable`；修复前判据 `DirtyMemory` |
| 端点缺失的判定 | `internal/sandbox/fc/rollback.go` | `saveDirtyBitmap`、`isMissingRoute` |
| 跟踪开关 | `internal/sandbox/fc/dirtytracking.go` | `decideTrackDirtyPages`、`hostDefaultTrackDirtyPages`、`TrackDirtyPagesEnabled`、`TrackDirtyPagesReason` |
| header 块大小细化 | `packages/shared/pkg/storage/header/metadata.go` | `ToDiffHeader` |
| 三级取源与缓冲池 | `internal/sandbox/block/page.go` | `ReadPage`、`getPageBuffer`、`putPageBuffer` |
| 零拷贝与拼接 | `internal/sandbox/build/build.go`、`internal/sandbox/template/storage.go` | `File.ReadAt`、`File.SliceContiguous`、`File.Slice`；`Storage.SliceContiguous` |
| uffd 校验与缺页 | `internal/sandbox/uffd/userfaultfd/userfaultfd.go` | `NewUserfaultfdFromFd`（硬检查放宽为整除）、`faultPage` |
| 预取器 | `internal/sandbox/uffd/prefetch/prefetcher.go` | 按页大小计的预取跟踪 |
| 顺带修的两处 | `internal/sandbox/block/cache.go`、`internal/sandbox/block/chunk.go` | `Cache.coveringBlocks`；`FullFetchChunker.fetchToCache` |
| 修复前判据在 FC 里的实现（对照） | `firecracker/src/vmm/src/lib.rs`、`firecracker/src/vmm/src/utils/pagemap.rs` | `get_dirty_memory`、`is_page_dirty` |
| 单元测试 | `internal/sandbox/uffd/{diffmetadata,sinceboot}_test.go`、`internal/sandbox/checkpoint_epoch_test.go`、`internal/sandbox/block/page_test.go`、`internal/sandbox/build/file_slice_test.go`、`packages/shared/pkg/storage/header/finer_diff_test.go` | —— |

---

## 3. 文件格式索引

产物目录布局见 [04](04-architecture.md)。每个 checkpoint 一个目录 `ckpt_<UnixNano>/`，沙箱级的封存层在 `layers/`。

| 文件 | 格式 | 写者 | 读者 | 详见 |
|---|---|---|---|---|
| `snapfile` | FC vmstate（二进制） | FC | FC | —— |
| `mem_diff` | 稀疏文件，页写在各自的偏移上；全量根也用这个名字 | FC | orchestrator | [06](06-memory-diff-tree.md) |
| `mem_bitmap` | FCDB 位图（§3.1） | FC | orchestrator | [08](08-firecracker-api-contract.md) |
| `mem_diff.<id>`、`mem_bitmap.<id>` | 合并后子节点的新内存文件与并集侧车，`<id>` 是被并入条目的 ID；用新名字而不是改名，避免切换失败时两个条目共享 inode | orchestrator | orchestrator | [06](06-memory-diff-tree.md) |
| `rootfs.header` | 序列化的合并 header | orchestrator | orchestrator | [07](07-disk-layering.md) |
| `layers/layer-<uuid>` | 原写层文件改名而来，块在原偏移上 | guest（经 NBD） | 运行中的沙箱 / restore | [07](07-disk-layering.md) |
| `layers/layer-<uuid>.meta` | 8 字节小端一条的块偏移列表 | orchestrator | orchestrator | [07](07-disk-layering.md) |
| `layers/layer-<uuid>.<id>` | 层合并产生的合成层（及其 `.meta`），`<uuid>` 是接收方那一层的，`<id>` 是被并入条目的 ID | orchestrator | 同上 | [07](07-disk-layering.md) |
| `manifest.json` | 单条目的完整 JSON | orchestrator | 无人（事后排查） | [04](04-architecture.md) |
| `index.json` | 沙箱的条目清单；**默认不写**，`CHECKPOINT_DEBUG_INDEX=true` 时后台最多每 5 s 写一次 | orchestrator | 无人（调试） | [04](04-architecture.md) |
| `timings.json` | `{计时键: 毫秒}`，每个 checkpoint 一份 | orchestrator | 基准脚本 | [28](28-observability-reference.md) |
| `last-restore-timings.json` | 沙箱目录下，每次 restore 覆写 | orchestrator | 基准脚本 | [28](28-observability-reference.md) |
| `revert_mem.tmp`、`revert_bitmap.tmp` | 物化结果：稀疏内存文件与 FCDB，写在目标 checkpoint 目录下，只存在于一次 restore 期间 | orchestrator | FC | [06](06-memory-diff-tree.md) |
| `<store>/.trash-<uuid>` | 被丢弃的整个沙箱目录，锁内改名、锁外删除；启动时清空 store 根会顺带删掉残留 | orchestrator | —— | [13](13-state-concurrency-durability.md) |
| `<DEFAULT_CACHE_DIR>/checkpoint-capabilities.json` | 启动能力的 JSON（能力行的键 + `pid`、`started_at`、`version`、`commit`），在 store 根的父目录，每次启动整份替换 | orchestrator | 运维、`run.sh smoke` | [27](27-configuration-and-capacity.md#31-能力文件) |

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

SDK 与 RPC 的调用方式见 [23](23-quickstart.md)，超时、不重放与错误处置见 [25](25-errors-timeouts-concurrency.md)。

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

全书的不变量共 19 条，**编号、定义、破坏后果和是否静默只在 [12 §9](12-failure-semantics.md#9-不变量清单) 列出**，其他篇引用时都用那里的编号。
改代码前过一遍那张表；其中 #15–#19 是合并、full 根回滚因子、子节点计数表、层引用计数与字节计数这几项新机制带来的，
属性测试怎么守它们见 [19](19-testing-and-functional-verification.md)。

---

## 6. 章目表

| # | 文件 | 一句话 |
|---|---|---|
| 导读 | [README](README.md) | 版本、署名与阅读入口 |
| 00 | [导读](00-overview.md) | 定位、能力边界、全书结构与阅读路径 |
| **一 基础** |  |  |
| 01 | [沙箱、microVM 与快照原理](01-background.md) | 虚机状态由什么构成、一致性、重建与原地回写 |
| 02 | [e2b 原生 snapshot：机制与成本](02-e2b-native-snapshot.md) | 原生 pause / resume 怎么打快照、怎么恢复、成本在哪 |
| 03 | [设计目标、约束与方案选择](03-goals-and-design-choices.md) | 目标场景、硬约束、非目标、设计原则 |
| **二 核心设计与实现** |  |  |
| 04 | [总体架构](04-architecture.md) | 部件、职责、目录布局 |
| 05 | [脏页跟踪与 HDBSS](05-dirty-page-tracking-and-hdbss.md) | 增量的前提、三种跟踪机制、判据差异 |
| 06 | [内存差分树](06-memory-diff-tree.md) | 回滚集、物化、删除 / 隐藏 / 合并 |
| 07 | [磁盘分层](07-disk-layering.md) | 零拷贝封存、层引用计数、层合并、视图装配 |
| 08 | [Firecracker 接口契约](08-firecracker-api-contract.md) | 两个进程之间的约定、位图格式、版本配对 |
| 09 | [进程内原地回滚](09-in-place-rollback.md) | 回滚的各阶段与提交点 |
| 10 | [原地回滚特有的问题](10-rollback-pitfalls.md) | RX 缓存、VMGenID、conntrack、时间 |
| 11 | [端到端走查](11-end-to-end.md) | 一次 checkpoint 与一次 restore 的完整路径 |
| **三 工程保障** |  |  |
| 12 | [失败语义与不变量](12-failure-semantics.md) | 宁可报错不可静默损坏、不变量清单 |
| 13 | [状态、并发与持久性](13-state-concurrency-durability.md) | 锁、原子提交、锁外删除、节点级效应 |
| 14 | [生命周期边界](14-lifecycle-reasoning.md) | 为什么脱离活沙箱恢复不了 |
| **四 原生快照的精确增量** |  |  |
| 15 | [原生增量为什么不精确](15-native-increment-diagnosis.md) | 判 / 存 / 填三层、驻留即脏、2 MiB 归并地板 |
| 16 | [精确增量的改法](16-native-increment-fix.md) | 4 KiB 存、2 MiB 拼；自启动以来脏页集；退路 |
| 17 | [代价与验证](17-native-increment-cost-and-verification.md) | 恢复热路径的代价、平台差异、验证证据 |
| 18 | [两种快照的分工与配合](18-native-and-checkpoint-together.md) | 与原生的对比表、优化点表、怎么一起用 |
| **五 验证与实测** |  |  |
| 19 | [测试体系与功能验证](19-testing-and-functional-verification.md) | 四种静默失效、测试矩阵、各脚本判据、crtest |
| 20 | [性能口径与方法](20-performance-methodology.md) | 判定口径、负载构造、脚本分工、长跑与并发的测法 |
| 21 | [分档基准与判定](21-benchmarks-and-compliance.md) | 成本模型、分档数字与达标判定、已知项与缺口 |
| 22 | [长跑与并发实测](22-long-run-and-concurrency.md) | 各轮长跑、暴露与修掉的问题、最终数字 |
| **六 使用与运维** |  |  |
| 23 | [快速上手](23-quickstart.md) | 装 SDK 覆盖层，checkpoint / restore / list / delete 示例 |
| 24 | [语义与使用边界](24-semantics-and-limits.md) | 能回滚什么、回滚不了什么，删除与空间回收 |
| 25 | [错误、超时与并发调用](25-errors-timeouts-concurrency.md) | 错误总表、SDK 超时与不重放、流截断 |
| 26 | [部署前提与检查清单](26-deployment-prerequisites.md) | HDBSS、内核、产物盘、二进制配对、部署后自检 |
| 27 | [配置参考与容量规划](27-configuration-and-capacity.md) | 唯一的开关总表、能力行、容量规划 |
| 28 | [可观测性参考](28-observability-reference.md) | 能力行、`memMode`、计时键与 timings 全字段 |
| 29 | [排障](29-troubleshooting.md) | 按症状查 |
| 30 | [上机验收](30-acceptance-runbook.md) | `run.sh` 各档、自检、结果回传 |
| **附录** |  |  |
| A | 本附录 | 查询入口 |
| B | [继续开发](B-extending.md) | 仓库、改动指引、自查清单、已知方向 |
