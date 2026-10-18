# 27 · 代码入口、修改指引与开发验证

当前开发工作树是 KASandbox_0904/deltabox-dev，单元测试保留在开发分支，上机测试工具在 e2b-infra。本章给出源码阅读、按任务修改、不变量检查和验证入口。

## 1. 代码在哪

### 1.1 仓库与分支

| | 在哪 | 装着什么 |
|---|---|---|
| **代码** | 当前开发仓库 `KASandbox_0904`。开发分支 **`deltabox-dev`**，开发测试通过后合并进交付分支 **`deltabox`**。首次合入是 openEuler KASandbox 的 [MR !119](https://gitcode.com/openeuler/KASandbox/pull/119)「feat: host-side sandbox checkpoint/restore (ARM64, HDBSS)」 | orchestrator、Firecracker、Python SDK 三个组件同仓 |
| **手册与测试** | `e2b-infra` 仓库的 `rollback/` | 本手册源文件、验收与补充测试脚本（`rollback/scripts/`） |
| **内部部署工具** | `e2b-infra` 仓库的 `e2b-infra.spec` 与补丁 `0001-adapted-for-arm-architecture.patch` | 0001 补丁由 `deltabox-dev` 的 orchestrator 侧改动融合进上游源码后生成，做法与脚本见 `e2b-infra/deploy-docs/08-源码开发与出包流程.md` 第 11 节。补丁里剔除了新增的 `_test.go`，单测以 `deltabox-dev` 上的结果为准 |

| 组件 | 目录 | 单元测试 | 怎么跑 |
|---|---|---|---|
| orchestrator（Go） | `packages/orchestrator/`：checkpoint 服务在 `internal/checkpoint/`，暂停窗口在 `internal/sandbox/checkpoint.go`，磁盘层在 `internal/sandbox/{block,rootfs}/`，FC 客户端在 `internal/sandbox/fc/`，conntrack 在 `internal/sandbox/network/`；代理在 `packages/shared/pkg/proxy/` | 同目录 `*_test.go`，含属性测试（`compact_sim_test.go`、`layer_refs_test.go`、`blockset_test.go` 等） | `cd packages/orchestrator && go test -race ./internal/checkpoint/... ./internal/sandbox/...` |
| Firecracker（Rust） | `firecracker/`：回滚主体在 `src/vmm/src/rollback.rs` | 各源文件内 `#[cfg(test)]` | `cd firecracker && cargo test --all`；故障注入相关的再加 `-p vmm -p firecracker --features vmm/rollback-fault-inject,firecracker/rollback-fault-inject` |
| Python SDK | `py-sdk/`：`e2b/sandbox_sync/checkpoint.py`、`e2b/sandbox_async/checkpoint.py`、`e2b/sandbox/checkpoint/errors.py`、`e2b/exceptions.py` | `py-sdk/tests/test_checkpoint_errors.py` | `cd py-sdk && pytest tests/test_checkpoint_errors.py` |

FC 的 vmm 单测要引导仓库里的测试内核（`src/vmm/src/test_utils/mock_resources/test_pe.bin`、`test_elf.bin`），
并且要能访问 `/dev/kvm`；源码目录只读挂载时，部分测试会因为在当前目录写临时文件而失败，要在可写副本里跑。

交付的是 **ext4 方案**（为什么见[第 3 章 §9](03-goals-and-boundaries.md#9-为什么交付用-ext4)）。上游基线是 e2b infra 的 `2026.09` 加一次 ARM 适配，本方案的全部改动在那之上。

### 1.2 交付形态

| 交付物 | 是什么 |
|---|---|
| **代码** | 手册讲解 `KASandbox_0904` 的 `deltabox-dev`（版本见 [README](README.md)）；`deltabox` 已包含首次 MR 交付，后续开发改动合入交付分支待整理。两阶段关系见 [附录 C](C-evolution-and-cases.md)；`firecracker/`、`packages/`、`py-sdk/` 三个组件同仓 |
| **文档** | 本手册，导出为单个 HTML 文件 |

目标平台是 950（带 HDBSS）；920B 是开发环境。**orchestrator 与 Firecracker 必须取自同一版本、配对部署**：
orchestrator 依赖 FC 的 `PUT /snapshot/rollback` 与 `PUT /snapshot/save-dirty-bitmap` 两个新端点（[第 8 章](08-firecracker-contract.md)）。

### 1.3 阅读源码的顺序

```
① internal/checkpoint/service.go           编排，一眼看到全貌
② internal/sandbox/checkpoint.go           两个暂停窗口里发生什么
③ internal/checkpoint/store.go             树账本，本方案的核心
④ src/vmm/src/rollback.rs                  Firecracker 侧主体
⑤ internal/sandbox/block/overlay.go        磁盘换层（配 layerstack.go）
⑥ internal/checkpoint/{bitmap,rootfs}.go   两种账本的细节
⑦ internal/checkpoint/{compact,compact_layers,layer_refs,quota}.go
                                           删除后的合并、层回收、字节配额
```

前四个读完，整套机制就通了；第 ⑦ 组决定长期运行时的磁盘与链深。

## 2. 按任务的改动指引

### 2.1 加一种 virtio 设备

| 步 | 做什么 |
|---|---|
| 1 | `validate_topology` 里加上它，否则拓扑检查会因"设备总数不等"失败 |
| 2 | `apply_device_states` 里写回它的状态 |
| 3 | **查 [第 10 章](10-device-network-time.md) 的检查表**：它有没有从 guest 内存推导出来的缓存？有没有在途异步 I/O？运行期会不会自己写 guest 内存？ |
| 4 | 有在途 I/O 的，在 `quiesce_devices` 里加排空逻辑 |
| 5 | 运行期写 guest 内存的，回滚最后阶段之后要重新标脏那些页 |
| 6 | 新增的系统调用加进 seccomp 白名单（`firecracker/resources/seccomp/aarch64-unknown-linux-musl.json`，按线程分组） |

有跨 guest / host 状态的设备（像 vsock），先想清楚回滚语义。**想不清楚就在 `validate_topology` 里拒绝它**，
现在 balloon 和 vsock 就是这么处理的。

### 2.2 支持一种新的存储能力

先问：它改变的是"产物怎么存"还是"产物怎么读"？

- 改变存法的（例如一种新的写时复制原语）：做成启动时探测一次 + 快路径 + 退回慢路径 + **退化上报**；
- 只是性能特性的（例如更快的 `fallocate`）：大概率不需要改代码。

**必须做退化上报。** 静默退化几十倍的路径比没有这个优化更糟。

### 2.3 加一个 API

| 步 | 做什么 |
|---|---|
| 1 | `spec/checkpointd/checkpoint/checkpoint.proto` 加 RPC |
| 2 | `service.go` 的 `switch operation` 加分支；`Handles` 按前缀匹配，不用改 |
| 3 | 全程持有 `LockSandbox`，除非它确实不碰每沙箱状态 |
| 4 | 会暂停虚机的，用 `context.WithoutCancel`，客户端断开不能把虚机留在暂停里 |
| 5 | 失败要能分级：区分"沙箱还能用"和"沙箱撕裂了" |
| 6 | 新的拒绝原因要有自己的 `reason`，并在 SDK 的 `errors.py` 映射到异常类；新异常要挂在 `CheckpointException` 下（按需再挂到更具体的父类，像 `CheckpointBytesLimitException` 挂在 `CheckpointTooManyException` 下，让旧代码照样接得住） |
| 7 | 重新生成 SDK 桩：`py-sdk/scripts/regen-checkpoint-pb.py` |
| 8 | 同步 `e2b-infra` 里的 SDK 覆盖层与 `crtest/sdktests/test_checkpoint_errors.py`（它是 SDK 测试的一份拷贝） |

### 2.4 改 Firecracker 侧

| 步 | 做什么 |
|---|---|
| 1 | 尽量做成**加法**：新端点、可选字段，不改既有语义 |
| 2 | 新错误变体要在 `RollbackError::faults_vm()` 里归类，编译器会强迫你回答 |
| 3 | 新系统调用加进 seccomp 白名单 |
| 4 | 改了位图线格式要两侧同时改（Rust 与 Go），并升 `DIRTY_BITMAP_VERSION` |
| 5 | 改了 `setup_dirty_tracking` 的调用位置，检查它仍在 vCPU 创建之后 |
| 6 | 全量快照的侧车必须是全 1：orchestrator 对全量根直接取全 1，FC 侧由 `test_full_snapshot_sidecar_is_all_ones` 守着 |

### 2.5 改账本

**最危险的区域。** 改之前先把 [第 11 章](11-failures-and-invariants.md) 的不变量清单读一遍。特别注意：

- 改 `ParentID` 的设置时机 → 破坏"`E_x` 覆盖 `(parent(x), x]`"；
- 改可见性判断 → 半写完的快照可能变成恢复目标；
- 改删除、剪枝或合并 → 可能打断后代的解析链。合并的正确性依赖"被并入的条目隐藏、非基准、恰有一个子节点"
  这三个条件，任何一条放松都要重新证明回滚集不变（[第 6 章](06-memory-diff-tree.md)）；
- 层的回收**按视图引用计数，不按树**：跟踪关闭时每个 checkpoint 都是树根，而它的视图仍含之前的全部层（[第 7 章](07-disk-layering.md)）；
- 在全局锁里只改内存里的结构，删文件放到锁外（收进 `reclaim`，调用方放锁后删）；
- 加新状态 → 想清楚它在 `Get` / `List` / 内容解析 / 回滚集 / 合并候选 / 字节计数里各自怎么表现；
- 改了会影响文件增减的路径 → 同步维护层引用计数与字节计数，属性测试会从盘上重算并比对。

## 3. 改动自查清单

| # | 问题 | 相关篇 |
|---|---|---|
| 1 | 有没有引入与虚机规格线性相关的新开销？ | [第 3 章](03-goals-and-boundaries.md) |
| 2 | 有没有重建某个宿主资源？ | [第 3 章](03-goals-and-boundaries.md) |
| 3 | 新的失败路径在提交点之前还是之后？ | [第 11 章](11-failures-and-invariants.md) |
| 4 | 会不会静默降级？降级了怎么被发现？ | [第 25 章](25-observability.md) |
| 5 | 持全局锁期间有没有做文件 I/O 或 O(条目数) 的扫描？ | [第 12 章](12-state-and-lifecycle.md) |
| 6 | 依赖"虚机已暂停"吗？调用契约写清楚了吗？ | [第 12 章](12-state-and-lifecycle.md) |
| 7 | 会不会在冻结窗口里等别的沙箱、等 GC 或等内存回收？ | [第 12 章](12-state-and-lifecycle.md) |
| 8 | 引入新系统调用了吗？ | [第 8 章](08-firecracker-contract.md) |
| 9 | 破坏了哪一条不变量？ | [第 11 章](11-failures-and-invariants.md) |

**第 9 条最重要。** 大多数不变量被破坏后是静默的：测试会过，功能会"正常"，问题在很久以后以莫名其妙的方式出现。

## 4. 怎么验证一次改动

### 4.1 三层

| 层 | 跑什么 | 在哪 |
|---|---|---|
| 单元测试与属性测试 | [§1.1](#11-仓库与分支) 的三条命令；Go 一律带 `-race` | 开发机 |
| 正确性与功能 | `rollback/scripts/950/run.sh smoke`、`run.sh func`；动了树语义再跑 `dev/correctness.py ext4` | 目标机 |
| 性能 | `run.sh perf`；动了长期行为（删除、合并、回收、锁）要跑并发长测 | 目标机 |

判据讲解见 [第 16 章](16-test-methodology.md)，性能口径见 [第 16 章](16-test-methodology.md)，
上机步骤见 [第 20 章](20-acceptance.md)。换二进制的日常流程（编译、替换、确认跑的是新的）见
`e2b-infra/deploy-docs/08-源码开发与出包流程.md` 第 4 节。

### 4.2 至少要看的三个信号

改完第一次跑起来，先确认：

1. 启动日志的 `checkpoint capabilities` 行里 `track_dirty_pages` 为 `true` 且理由符合预期，各开关的值与来源是你想要的（[第 25 章](25-observability.md)）；
2. 第二个 checkpoint 的 `memMode` 是 `incremental`，不是 `full`；
3. 950 上 `dmesg | grep 'Enable HDBSS success'` 有输出，且 PID 是 Firecracker 的。

任何一个不对，后面的数字都不用看。第 2 条是防"增量静默退化成全量"的主力判据，而它本身也会失效
（SDK 没透出字段时脚本会跳过，[第 16 章](16-test-methodology.md)）。

### 4.3 故障注入钩子（只给开发者）

失败路径里最值得信任的恰恰是手工到不了的那几条，所以留了两个钩子：

- orchestrator 侧 `CHECKPOINT_FAULT_INJECT`（`internal/checkpoint/faults.go`），逗号分隔的故障名，可加 `:once`，启动时读一次；
  名字清单见 [第 24 章](24-configuration-and-capacity.md)。合并路径的 `compact_*` 注入点只在单测里用过。
- FC 侧 `FC_ROLLBACK_FAULT_INJECT`，只在用 cargo feature `rollback-fault-inject` 构建的二进制里存在，**交付二进制不含**。

生产部署不设这两个变量；是否有故障被武装，看启动能力行的 `fault_inject` 字段。

## 5. 代码地图

代码规模、MR 后增量及当前测试统计见 [附录 D](D-code-size.md)，本节负责代码位置与符号索引。

代码在 当前开发仓库 `KASandbox_0904`：开发分支 `deltabox-dev`，交付分支 `deltabox`（[第 27 章](27-development.md)）。三个组件同仓：

| 组件 | 目录 | 下表路径的前缀 | 单元测试 |
|---|---|---|---|
| orchestrator（Go） | `packages/orchestrator/` | [§5.1](#51-orchestrator) 的 `internal/...` 前加 `packages/orchestrator/` | 同目录 `*_test.go` |
| Firecracker（Rust） | `firecracker/` | [§5.2](#52-firecracker) 的 `src/...` 前加 `firecracker/` | 源文件内 `#[cfg(test)]` |
| Python SDK | `py-sdk/` | —— | `py-sdk/tests/test_checkpoint_errors.py` |

### 5.1 orchestrator

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

### 5.2 Firecracker

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

### 5.3 Python SDK（`py-sdk/`）

| 关注点 | 文件 |
|---|---|
| 同步 / 异步接口 | `e2b/sandbox_sync/checkpoint.py`、`e2b/sandbox_async/checkpoint.py` |
| 异常族（基类 `CheckpointException` + 9 个子类，含 `CheckpointBytesLimitException`，它是 `CheckpointTooManyException` 的子类） | `e2b/exceptions.py` |
| reason → 异常类映射 | `e2b/sandbox/checkpoint/errors.py` |
| 返回类型 | `e2b/sandbox/checkpoint/types.py` |
| checkpoint 请求默认超时 `CHECKPOINT_REQUEST_TIMEOUT` | `e2b/connection_config.py` |
| 重新生成 RPC 桩 | `scripts/regen-checkpoint-pb.py`（proto 在仓库根的 `spec/checkpointd/checkpoint/checkpoint.proto`） |

### 5.4 测试脚本（`e2b-infra/rollback/scripts/`）

| 目录 | 文件 | 讲解 |
|---|---|---|
| `950/` | `run.sh`：smoke / func / perf / long 一站式入口 | [第 20 章](20-acceptance.md)、[第 16 章](16-test-methodology.md) |
| `acceptance/`（五个单文件脚本互不依赖；`rolling_keep10.py` 从同目录导入 `checkpoint_concurrent.py`） | `checkpoint_verify.py`（59 项正确性）、`checkpoint_bench.py`（时机成本与产物实测）、`checkpoint_concurrent.py`（并发 A/B/C/D）、`checkpoint_bench_v2.py`（与进程级快照对照的口径）、`native_snapshot_bench.py`（原生 snapshot 同一张档位表）、`rolling_keep10.py`（滚动保留长跑） | [第 16 章](16-test-methodology.md)、[第 16 章](16-test-methodology.md)、[第 19 章](19-950-concurrency.md) |
| `crtest/` | `crtest/cases/t11.py … t41.py`（20 个用例）、`crtest/common.py`、`tests/`（离线单测）、`sdktests/test_checkpoint_errors.py`、`portability/preflight-customer.sh`、`probe950/` | [第 16 章](16-test-methodology.md) |
| `crtest/bench/` | `bench_tiers.py`、`compliance.py`、`analyze.py`、`serial_restore.py` | [第 16 章](16-test-methodology.md) |
| `dev/`（共享 `lib.py`） | `correctness.py`、`pause_verify.py`、`compat_matrix.py`、`hdbss_evidence.py`（配 `cap_test.c`）、`loop.py`、`timing.py`、`bench-ckpt.py` + `freeze_probe.py`、`probe-ramp.py`、`01-check-host.sh` … `04-verify-runtime.sh`、`run-all.sh` | [第 16 章](16-test-methodology.md)、[第 16 章](16-test-methodology.md) |
| `probes/` | `pb2.py` … `pb5.py`、`probe_dirty.py`、`uffdwp_probe.c`：脏页判据的定向探针，不属于验收流程 | —— |

### 5.5 原生快照的精确增量（原生 pause 路径）

提交 `c42d23e`（4 KiB 写跟踪差分）与 `bc19b45`（自启动以来脏页集），均在 `deltabox-dev@93ccb02` 之内；Firecracker 零改动。
下表 `internal/...` 前加 `packages/orchestrator/`。讲解见 [第 14 章](14-native-precise-increment.md)–[第 15 章](15-native-cooperation-and-cost.md)。

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
