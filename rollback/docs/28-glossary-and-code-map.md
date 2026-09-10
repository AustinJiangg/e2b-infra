# 28 · 术语表与代码地图

> 全书的查询入口：术语中英对照、代码索引、文件格式索引、环境变量总表、API 总表。
> 不用从头读，需要时来查。
>
> **读者**：所有人。

---

## 1. 术语表

### 1.1 本方案的概念

| 术语 | 英文 / 标识 | 一句话 | 详见 |
|---|---|---|---|
| **checkpoint** | checkpoint | 沙箱某一时刻的状态记录，本方案的产物单位 | [08](08-memory-diff-tree.md) |
| **restore / 回滚** | restore / rollback | 把活着的沙箱退回某个 checkpoint | [11](11-in-place-rollback.md) |
| **纪元** | epoch | 两次「脏页跟踪被重置」之间的时间段 | [08 §2.2](08-memory-diff-tree.md#22-纪元epoch与它的位图) |
| **纪元位图** | `E_x`、`mem_bitmap` | 记录一个纪元里写过哪些页 | [08 §2.2](08-memory-diff-tree.md#22-纪元epoch与它的位图) |
| **差分树** | diff tree | 以 `ParentID` 组织的 checkpoint 树 | [08 §3](08-memory-diff-tree.md#3-树而不是链) |
| **基准** | base | 下一个 checkpoint 的父亲；每沙箱一个 | [08 §3.2](08-memory-diff-tree.md#32-树是怎么长出来的) |
| **回滚集** | revert set | 回滚时需要写回的页集合 | [08 §4](08-memory-diff-tree.md#4-回滚集) |
| **物化** | materialize | 把回滚集的目标时刻内容聚合成一个稀疏文件 | [08 §5](08-memory-diff-tree.md#5-物化交给-firecracker-的两个文件) |
| **全量树根** | full root | 树根存完整内存，使整棵树自足 | [08 §6.3](08-memory-diff-tree.md#63-全量根为什么是默认) |
| **隐藏条目** | hidden entry | 在树里、不在 API 里的条目 | [14 §5](14-failure-semantics.md#5-隐藏条目) |
| **断链** | broken chain / `invalid` | 纪元丢失，此后拒绝恢复直到下一次全量 | [14 §6](14-failure-semantics.md#6-断链) |
| **封存** | seal | 把活写层原地变成只读层，零数据搬运 | [10 §3](10-disk-layering.md#3-seal不搬运任何数据的换层) |
| **封存层** | sealed layer | 封存后的只读层文件 | [10](10-disk-layering.md) |
| **合并 header** | merged header | 「模板映射 + 各封存层恒等映射」的序列化结果 | [10 §4.2](10-disk-layering.md#42-合并-header) |
| **提交点** | commit point | 回滚中「开始改 guest 状态」的那一刻 | [11 §3.4](11-in-place-rollback.md#34-提交点) |
| **撕裂** | torn / `Faulted` | 虚机介于两个时刻之间，只能重建 | [14 §2](14-failure-semantics.md#2-提交点把失败切成两半) |
| **冻结窗口** | freeze window | 虚机暂停到恢复的这段时间 | [03 §2.1](03-snapshot-fundamentals.md#21-唯一的正确做法一次暂停内完成) |
| **账本污染** | poisoned | 账本与活的层栈不一致，此后拒绝服务 | [10 §8](10-disk-layering.md#8-账本与污染) |

### 1.2 平台与底层

| 术语 | 全称 | 一句话 | 详见 |
|---|---|---|---|
| **microVM** | —— | 保留硬件隔离、砍到最小设备模型的虚拟机 | [02 §1](02-microvm-and-e2b.md#1-三种隔离) |
| **Firecracker** | —— | AWS 开源的 microVM 监控器 | [02 §2](02-microvm-and-e2b.md#2-firecracker) |
| **HDBSS** | Hardware Dirty state tracking Structure | ARMv9.5 的硬件脏页跟踪 | [19 §2](19-kunpeng-platform.md#2-hdbss-是什么) |
| **Stage-2** | —— | KVM 管理的第二层地址翻译（IPA → 宿主物理） | [19 §2](19-kunpeng-platform.md#2-hdbss-是什么) |
| **DBM** | Dirty Bit Modifier | 页表项上的位，让硬件自己标脏 | [19 §2](19-kunpeng-platform.md#2-hdbss-是什么) |
| **VHE** | Virtualization Host Extensions | ARM 的虚拟化宿主扩展；HDBSS 要求它 | [19 §3](19-kunpeng-platform.md#3-启用的六个前提) |
| **UFFD** | userfaultfd | 用户态处理缺页的内核机制 | [02 §3.2](02-microvm-and-e2b.md#32-内存是按需拉取的) |
| **NBD** | Network Block Device | 把用户态对象暴露成内核块设备 | [10 §1](10-disk-layering.md#1-沙箱的磁盘长什么样) |
| **reflink** | `FICLONE` | 让两个文件共享磁盘 extent，写时才分裂 | [18 §1](18-ext4-vs-xfs.md#1-分歧的源头reflink) |
| **VMGenID** | VM Generation ID | ACPI 设备，代号变化告诉 guest「你被恢复了」 | [12 §3](12-rollback-pitfalls.md#3-vmgenid-的次序) |
| **conntrack** | connection tracking | 宿主内核的连接跟踪表 | [12 §4](12-rollback-pitfalls.md#4-连接跟踪) |
| **稀疏文件** | sparse file | 未写过的区间不占物理块 | [08 §2.1](08-memory-diff-tree.md#21-稀疏文件) |

### 1.3 e2b 的概念

| 术语 | 一句话 | 详见 |
|---|---|---|
| **template** | 沙箱的出厂状态，构建产出一份可反复加载的快照 | [02 §3](02-microvm-and-e2b.md#3-e2b-的对象模型) |
| **build** | 一次 template 构建的产物 | 同上 |
| **sandbox** | 一个运行中的沙箱实例 | 同上 |
| **envd** | guest 里执行命令、读写文件的守护进程 | 同上 |
| **orchestrator** | 宿主上管沙箱生命周期、网络、存储、代理的进程 | [06](06-architecture.md) |
| **SandboxID / ExecutionID / LifecycleID** | 三个不同层次的身份标识 | [20 §1.3](20-vs-native.md#13-三个-id) |

---

## 2. 代码地图

### 2.1 orchestrator（`infra-arm@jll`）

| 关注点 | 文件 | 关键符号 |
|---|---|---|
| 服务入口、编排、失败分级 | `internal/checkpoint/service.go` | `Port`、`Handles`、`ServeCheckpoint`、`create`、`restore`、`failCreate`、`fullRootEnabled`、`envdRestoreTimeout` |
| 树账本、回滚集、内容解析 | `internal/checkpoint/store.go` | `Entry`、`Store`、`LockSandbox`、`Prepare`、`Commit`、`CommitHidden`、`InvalidateBase`、`revertPathLocked`、`MaterializeRevert`、`writeRevertMem`、`Delete`、`pruneLocked`、`RemoveSandbox` |
| FCDB 位图 | `internal/checkpoint/bitmap.go` | `readDirtyBitmap`、`writeTo`、`merge`、`contains`、`allOnesBitmap` |
| 磁盘层账本 | `internal/checkpoint/rootfs.go` | `AppendLayer`、`SetRootfsToEntry`、`DiskViewForEntry`、`identityMappings`、`writeLayerMeta` |
| metrics 与退化属性 | `internal/checkpoint/metrics.go` | `callStats`、`record` |
| 暂停窗口 | `internal/sandbox/checkpoint.go` | `CheckpointToFiles`、`RollbackInPlace`、`assembleView`、`RollbackTornError` |
| 分阶段计时 | `internal/sandbox/phasetimings.go` | `PhaseTimings`、`Mark`、`SetUs`、`Timed` |
| 写层与换层 | `internal/sandbox/block/overlay.go` | `Overlay`、`Seal`、`ResetView`、`EjectCache` |
| 封存层读路径 | `internal/sandbox/block/sealed_view.go` | `SealedView` |
| 写层实现 | `internal/sandbox/block/cache.go` | `NewCache`、`MarkCached`、`DirtyOffsets`、`MoveFile`、`CloseKeepFile`、`ExportToDiff` |
| 四步封存 | `internal/sandbox/rootfs/nbd.go` | `SealLayer`、`ResetView`、`sync`、`ExportDiff` |
| 脏页跟踪默认值 | `internal/sandbox/fc/dirtytracking.go` | `resolveTrackDirtyPages`、`hardwareDirtyTracking` |
| 两个端点的客户端 | `internal/sandbox/fc/rollback.go` | `rollbackSnapshot`、`saveDirtyBitmap`、`RollbackFaultedError` |
| 连接跟踪清理 | `internal/sandbox/network/conntrack.go` | `FlushConntrack` |
| 代理拦截 | `internal/proxy/proxy.go` | `checkpoint.Handles` 那段包装 |
| 启动时能力上报 | `packages/orchestrator/main.go` | `reportCheckpointCapabilities` |
| 原生路径（对照） | `internal/server/sandboxes.go`、`internal/sandbox/sandbox.go` | `Server.Pause`、`Server.Checkpoint`、`Sandbox.Pause`、`Factory.ResumeSandbox` |
| **仅 XFS 方案** | `internal/checkpoint/clone.go` | `ProbeReflink`、`CloneOrCopy` |

### 2.2 Firecracker（`KASandbox@jll`）

| 关注点 | 文件 | 关键符号 |
|---|---|---|
| 原地回滚主体 | `src/vmm/src/rollback.rs` | `rollback_snapshot`、`validate_topology`、`quiesce_devices`、`apply_device_states`、`apply_net_rx_cache`、`save_dirty_bitmap`、`log_queue_diagnostics`、`RollbackError::faults_vm` |
| 脏页后端 | `src/vmm/src/vstate/vm.rs` | `setup_dirty_tracking`、`DirtyTrackingBackend`、`snapshot_memory_to_file`、`mincore_bitmap` |
| HDBSS ioctl | `src/vmm/src/arch/aarch64/vm.rs` | `enable_hdbss` |
| 内存操作与 FCDB | `src/vmm/src/vstate/memory.rs` | `dump_dirty`、`restore_dirty`、`store_dirty_bitmap`、`reset_dirty`、`serialize_dirty_bitmap`、`deserialize_dirty_bitmap` |
| 两个构建路径 | `src/vmm/src/builder.rs` | `build_microvm_for_boot`、`build_microvm_from_snapshot` |
| vCPU 恢复 | `src/vmm/src/lib.rs`、`vstate/vcpu.rs`、`arch/aarch64/vcpu.rs` | `restore_vcpu_states_in_place`、`VcpuEvent::RestoreState`、`restore_state` |
| RX 缓存重建 | `src/vmm/src/devices/virtio/net/device.rs` | `rollback_rx_buffers`、`parse_rx_descriptors` |
| 端点参数 | `src/vmm/src/vmm_config/snapshot.rs` | `CreateSnapshotParams`、`RollbackSnapshotParams`、`SaveDirtyBitmapParams`、`RollbackTimings` |
| 路由 | `src/firecracker/src/api_server/request/snapshot.rs` | `parse_put_snapshot` |
| 动作分发 | `src/vmm/src/rpc_interface.rs` | `VmmAction::{RollbackSnapshot, SaveDirtyBitmap}` |
| 退化的那一行（orchestrator 侧） | `internal/sandbox/uffd/userfaultfd/userfaultfd.go` | 被注释的 `UFFDIO_COPY_MODE_WP` |

### 2.3 测试与脚本

| 用途 | 位置 |
|---|---|
| 单元测试 | `internal/checkpoint/{store,bitmap,rootfs,header_agreement}_test.go` |
| 交付态正确性验收 | `e2b-infra/benchmark/checkpoint_verify.py` |
| 交付态性能基准 | `e2b-infra/benchmark/checkpoint_bench.py` |
| 开发态工具箱 | `e2b-infra/rollback/test-950/` |
| 汇报配图 | `e2b-infra/rollback/diagrams/` |
| 汇报稿 | `e2b-infra/rollback/slides/` |

---

## 3. 文件格式索引

| 文件 | 格式 | 写者 | 读者 | 详见 |
|---|---|---|---|---|
| `snapfile` | Firecracker vmstate（二进制） | Firecracker | Firecracker | —— |
| `mem_diff` | **稀疏文件**，页写在各自的 guest 物理偏移上 | Firecracker | orchestrator | [08 §2.1](08-memory-diff-tree.md#21-稀疏文件) |
| `mem_bitmap` | **FCDB**：magic + version + page_size + num_pages + 位图字 | Firecracker | orchestrator | [09 §5](09-firecracker-api-contract.md#5-fcdb-位图格式) |
| `rootfs.header` | 序列化的合并 header（元数据 + `BuildMap` 列表） | orchestrator | orchestrator | [10 §4.2](10-disk-layering.md#42-合并-header) |
| `layer-<uuid>` | **原写层文件**（改名而来），块在原偏移上 | guest（经 NBD） | 运行中的沙箱 / 恢复时 | [10 §3](10-disk-layering.md#3-seal不搬运任何数据的换层) |
| `layer-<uuid>.meta` | 8 字节小端一条的块偏移列表 | orchestrator | orchestrator | [10 §4.3](10-disk-layering.md#43-层侧车) |
| `manifest.json` | 单条目的完整 JSON | orchestrator | **无人**（事后排查） | [16 §3.3](16-lifecycle-and-portability.md#33-账本不从磁盘加载) |
| `index.json` | 该沙箱的条目清单 | orchestrator | 同上 | 同上 |
| `timings.json` | `{阶段名: 毫秒}` | orchestrator | 基准脚本 | [17 §1](17-observability-and-verification.md#1-三个时钟) |
| `revert_mem.tmp` | 稀疏文件，回滚集的目标时刻内容 | orchestrator | Firecracker | [08 §5](08-memory-diff-tree.md#5-物化交给-firecracker-的两个文件) |
| `revert_bitmap.tmp` | FCDB | orchestrator | Firecracker | 同上 |

### 3.1 FCDB 逐字节

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 4 | magic `"FCDB"` |
| 4 | 4 | version，u32 小端，当前 `1` |
| 8 | 8 | `page_size`，u64 小端 |
| 16 | 8 | `num_pages`，u64 小端 |
| 24 | 8 × ⌈num_pages/64⌉ | 位图字，u64 小端 |

第 `w` 个字的第 `i` 位对应页索引 `w*64 + i`。
**页索引是内存文件偏移 ÷ 页大小，不是 guest 物理地址**
（[第 9 篇 §5.2](09-firecracker-api-contract.md#52-页索引是什么空间)）。
尾字超出 `num_pages` 的位**必须为 0**。

---

## 4. 环境变量总表

| 变量 | 归属 | 取值 | 默认 | 作用 |
|---|---|---|---|---|
| `FC_TRACK_DIRTY_PAGES` | orchestrator | `true` / 其它 | 跟随硬件探测 | 强制开 / 关脏页跟踪（**第一层**） |
| `FC_HDBSS_ORDER` | Firecracker | `1`–`9` | `1`（8 KiB/vCPU） | HDBSS buffer 大小编码 |
| `FC_HDBSS_REQUIRED` | Firecracker | `true` / `1` | `false` | 必须启用 HDBSS，否则启动失败 |
| `CHECKPOINT_FULL_ROOT` | orchestrator | `""` / `true` / `1` = 开 | 开 | 树根是否全量捕获 |
| `ORCHESTRATOR_BASE_PATH` | orchestrator | 路径 | —— | 产物根目录 |

> 两层脏页的区别见[第 19 篇 §1](19-kunpeng-platform.md#1-先分清两层)。

---

## 5. API 总表

### 5.1 SDK（Python）

| 方法 | 说明 |
|---|---|
| `sandbox.checkpoint.create(name=None)` | 打一个 checkpoint，返回 `checkpointId` 与 `memMode` |
| `sandbox.checkpoint.restore(checkpoint_id)` | 回滚到该 checkpoint |
| `sandbox.checkpoint.list()` | 列出可见的 checkpoint |
| `sandbox.checkpoint.delete(checkpoint_id)` | 删除（可能转为隐藏保留） |
| `sandbox.checkpoint.is_available()` | 探活（等价的旧名是 `is_running()`） |

异步版在 `e2b/sandbox_async/checkpoint.py`。

### 5.2 宿主侧 RPC（端口 49984）

| 路径 | 说明 |
|---|---|
| `/checkpoint.Checkpoint/CreateCheckpoint` | |
| `/checkpoint.Checkpoint/RestoreCheckpoint` | |
| `/checkpoint.Checkpoint/ListCheckpoints` | |
| `/checkpoint.Checkpoint/DeleteCheckpoint` | |
| `/health` | 由宿主应答，不进沙箱 |

错误码：`not_found` / `invalid_argument` / `unauthenticated` / `internal` /
**`data_loss`**（撕裂，必须重建沙箱）/ `failed_precondition`。

### 5.3 Firecracker 的三处扩展

| 扩展 | 形态 | 仅 ext4 方案？ |
|---|---|---|
| `CreateSnapshotParams.dirty_bitmap_path` | 已有端点的可选字段 | 否 |
| `PUT /snapshot/rollback` | 新端点 | 否 |
| `PUT /snapshot/save-dirty-bitmap` | 新端点 | **是** |

---

## 6. 不变量速查

改代码前过一遍。完整说明见[第 14 篇 §9](14-failure-semantics.md#9-不变量清单)。

| # | 不变量 | 静默？ |
|---|---|---|
| 1 | 差分条目必须有侧车 | 否 |
| 2 | `E_x` 恰好覆盖 `(parent(x), x]` | **是** |
| 3 | 回滚集 ⊇ Firecracker 实际写回的页 | **是** |
| 4 | 恢复目标必须与运行中的虚机同拓扑 | 否 |
| 5 | 内存与磁盘在同一个暂停窗口内 | 部分 |
| 6 | 活跃脏图必须在暂停之后导出 | **是** |
| 7 | 提交前条目对 `Get` / `List` 不可见 | **是** |
| 8 | 纪元前进后产物必须保留（哪怕隐藏） | **是** |
| 9 | 账本与活的层栈必须一致，不一致就拒绝服务 | **是** |
| 10 | conntrack 在暂停窗口内清 | **是** |
| 11 | VMGenID 在内存回写之后 | **是** |
| 12 | 队列页在阶段 9 后重新标脏 | **是** |
| 13 | HDBSS 在 vCPU 创建之后武装 | **是** |
| 14 | 恢复虚机是无条件的 | 部分 |

---

## 7. 全书篇目

| # | 篇 | 一句话 |
|---|---|---|
| 00 | [设计总览](00-design-overview.md) | 一篇读完全貌 |
| 01 | [沙箱、快照与回滚](01-what-and-why.md) | 需求从哪来，「快照」在四个层次上各指什么 |
| 02 | [microVM 与 e2b](02-microvm-and-e2b.md) | 被快照的那个东西是什么 |
| 03 | [快照原理](03-snapshot-fundamentals.md) | 状态包括什么、一致性、重建 vs 原地 |
| 04 | [e2b 原生 snapshot](04-e2b-native-snapshot.md) | 基线与对照组 |
| 05 | [设计目标与边界](05-design-goals.md) | 做什么、不做什么、四条原则 |
| 06 | [总体架构](06-architecture.md) | 部件与职责边界 |
| 07 | [脏页跟踪](07-dirty-page-tracking.md) | 增量的前提 |
| 08 | [内存差分树](08-memory-diff-tree.md) | 核心数据结构与回滚集 |
| 09 | [Firecracker 接口契约](09-firecracker-api-contract.md) | 两个进程之间的约定 |
| 10 | [磁盘分层与零拷贝封存](10-disk-layering.md) | 一个字节都不搬 |
| 11 | [进程内原地回滚](11-in-place-rollback.md) | 九个阶段与提交点 |
| 12 | [原地回滚特有的问题](12-rollback-pitfalls.md) | 四个坑 |
| 13 | [端到端](13-end-to-end.md) | 完整走查与顺序约束 |
| 14 | [失败语义与不变量](14-failure-semantics.md) | 宁可报错不可静默损坏 |
| 15 | [状态管理与并发](15-state-and-concurrency.md) | 在运行中的机器上换零件 |
| 16 | [生命周期与可移植性边界](16-lifecycle-and-portability.md) | 为什么脱离沙箱恢复不了 |
| 17 | [可观测性与验证](17-observability-and-verification.md) | 让静默退化现形 |
| 18 | [ext4 与 XFS](18-ext4-vs-xfs.md) | 两套方案的差异与选择 |
| 19 | [鲲鹏平台](19-kunpeng-platform.md) | HDBSS、920B 与 950 |
| 20 | [与原生的对比与配合](20-vs-native.md) | 两条路径怎么一起用 |
| 21 | [继续开发](27-extending.md) | 改动指引与已知缺口 |
| 22 | 本篇 | 查询入口 |
