# 17 · 进程内原地回滚

> 这篇给要读或改 Firecracker 回滚端点的开发者看。读完能说清 `PUT /snapshot/rollback`
> 的九个阶段各做什么、为什么是这个顺序、提交点把失败切成哪两类，以及原地写回活对象时
> 必须额外处理的几件事（MMIO 排干、GIC 绝对写回、串口、tap offload、拓扑校验）。
>
> 本篇讲正常路径；这条路径特有的问题见 [18](18-rollback-pitfalls.md)。
> 预备：[14 · 内存差分树](14-memory-diff-tree.md)（回滚集从哪来）、
> [16 · 磁盘分层](16-disk-layering.md)（磁盘那一半）、
> [15 · FC 接口契约](15-firecracker-api-contract.md)（端点参数）。
> 主体代码：`firecracker/src/vmm/src/rollback.rs`。

---

## 1. 两种恢复形态

恢复一台虚机有两种做法：**重建**一个新进程去加载快照，或者往**活着的**进程里写回差异。

重建是 Firecracker 的标准恢复路径，也是 e2b 原生 resume 用的那条：

```
新建进程 → 建 KVM VM → 建 vCPU → 建 GIC → 建 virtio 设备
        → 注册 eventfd / irqfd / ioeventfd → 建 tap → 挂 UFFD
        → 加载 vmstate → 恢复 vCPU 与设备 → 运行
        → guest 逐页缺页，把工作集换回内存
```

原地路线是：

```
虚机暂停 → 校验 → 静默 I/O → 把差异页写回活映射
        → 把 vCPU / GIC / 设备状态写回既有对象 → 恢复运行
```

| 资源 | 重建 | 原地 |
|---|---|---|
| 进程、KVM VM fd、vCPU fd | 新建 | 保留 |
| GIC 设备 fd | 新建 | 保留 |
| eventfd / irqfd / ioeventfd 注册 | 全部重做 | 保留 |
| tap 设备、网络槽位 | 重建 | 保留 |
| guest 内存映射 | 重新 mmap | 保留 |
| **guest 工作集** | **靠缺页重新换入** | **留在物理内存里** |
| NBD 设备、Overlay | 重建 | 保留，只换视图（[16](16-disk-layering.md)） |

原地路线的代价**正比于回滚集大小**，与虚机规格无关。后面所有的复杂度 —— 拓扑校验、
设备静默、RX 缓存重建 —— 都是为这一点付的价。

---

## 2. 执行环境

回滚在 **VMM 线程**上执行，且**虚机必须处于暂停态**。没有 vCPU 在执行，没有设备事件在触发，
没有任何人能观察到中间状态，所以整个序列对 guest 是一个原子操作。代码第一步就确认这一点
（`rollback.rs:271` 起）：

```rust
match vmm.instance_info.state {
    VmState::Paused => {}
    VmState::Faulted => return Err(RollbackError::Faulted),
    _ => return Err(RollbackError::NotPaused),
}
```

`Faulted` 是**之前**某次回滚在提交点之后失败留下的状态。这样的虚机拒绝一切 resume 与 snapshot
操作 —— 它的状态介于两个时刻之间，任何进一步的操作都只会扩大损害。打 `Faulted` 标记发生在
`rollback_snapshot` 自己里面（`brand_vm_on_faulting_failure`，`rollback.rs:180`），不依赖 HTTP 层：
任何调用方拿到提交点之后的错误时，虚机都已经不再自称 `Paused`。

---

## 3. 九个阶段

| # | 阶段 | 做什么 | 副作用 | 计时键 |
|---|---|---|---|---|
| 1 | **校验** | 状态、快照可读、拓扑、内存文件长度、位图几何 | 无 | `fc_validate` |
| 2 | **静默** | 排空在途设备 I/O | 丢弃 tap 缓存帧，不改 guest 状态 | `fc_quiesce` |
| 3 | **取活跃脏图** | 取 KVM 脏页日志折回用户态位图，与调用方给的位图求并；校验内存文件覆盖回滚集 | 只改 Firecracker 自己的账 | `fc_bitmap` |
| | ══ **提交点** ══ | | | |
| 4 | **内存** | 按回滚集把页写回活映射 | **改 guest 状态** | `fc_memory` |
| 5 | **vCPU** | 排干挂起 MMIO → 复位 → 全寄存器恢复 → 读回比对 | 改 guest 状态 | `fc_vcpus` |
| 6 | **中断控制器** | 对既有 GIC fd 绝对写回；清串口那根 SPI | 改 guest 状态 | `fc_gic` |
| 7 | **设备** | virtio 队列、协商特性、中断状态；串口复位；RX 缓存；tap offload | 改 guest 状态 | `fc_devices` |
| 8 | **VMGenID** | 写入新代号，通知 guest 时间被回拨 | 改 guest 状态 | 计入 `fc_devices` |
| 9 | **重置基线** | 清用户态脏页位图，重新标脏队列页 | 改 Firecracker 的账 | 计入 `fc_total` |

第 10 步 —— 踢设备重新处理（已被回退的）队列 —— 不在这里，它发生在 `resume_vm` 里，
那个函数每次恢复都无条件踢一遍（`rollback.rs:466` 的注释）。

### 3.1 阶段 1：校验

任何一项不过就直接返回，虚机原封不动（`rollback.rs:271-325`）：

- 状态必须是 `Paused`（§2）；
- 快照文件能读出 `MicrovmState`；
- `validate_topology`：快照描述的虚机与运行中的一致（§4）；
- 内存文件长度必须**等于** guest 内存大小，guest 内存必须是整数个页；
- 调用方给的回滚位图（FCDB 格式）页大小、页数与 guest 一致。

「内存文件长度等于 guest 内存」是 [14](14-memory-diff-tree.md) 里物化文件那条契约的另一端：
orchestrator 物化的是一个与 guest 内存等长的稀疏文件。

### 3.2 阶段 2：静默在途 I/O

设备可能有还没完成的 I/O。不管它，一个回滚**之前**发起的操作可能在回滚**之后**完成，
把旧时间线的结果写进新时间线（`quiesce_devices`，`rollback.rs:747`）：

- **块设备**：`prepare_save()`，等异步引擎收尾；
- **网卡**：把 tap 里已缓存的帧读出来扔掉 —— 它们是发给正在被丢弃的那条时间线的。
  tap 是非阻塞的，`read` 返回 `≤ 0`（`EAGAIN` 或读空）即停；另有 4096 次的循环上限，
  防止持续灌包时静默阶段变成死循环。

### 3.3 阶段 3：取活跃脏图与覆盖校验

```rust
let kvm_bitmap = vmm.vm.get_dirty_bitmap()?;                      // 破坏性读
vmm.vm.guest_memory().store_dirty_bitmap(&kvm_bitmap, page_size);  // 立刻折回
let mut revert = userspace_bitmap_flat(vmm.vm.guest_memory(), page_size, total_pages);
if let Some(words) = &file_bitmap { /* revert |= words */ }
validate_mem_file_coverage(&mem_file, &revert, page_size, total_pages)?;
```

顺序不能变：

1. **取**：`KVM_GET_DIRTY_LOG` 取走并清空内核位图；
2. **折回**：立刻 OR 进用户态位图。此后无论发生什么，「这些页脏过」的信息都不会丢；
3. **求并**：`revert = 活跃脏页 ∪ 调用方给的累积集合`；
4. **覆盖校验**（`validate_mem_file_coverage`，`rollback.rs:858`）：用 `SEEK_DATA`/`SEEK_HOLE`
   逐段走内存文件的 extent，回滚集里任何一页落在文件空洞上就拒绝。

第 3 步说明 Firecracker **自己**会把活跃脏页并进来，所以 orchestrator 必须事先知道这个集合并物化它
（[15](15-firecracker-api-contract.md) 的 save-dirty-bitmap 端点）。第 4 步是这条契约的执行手段：
长度检查分不出「完整视图」和「被 `set_len` 撑长的 Diff 层」，也抓不住「调用方漏物化了某页」；
不拦下来，`restore_dirty` 会从空洞读到零写进 guest，而且发生在提交点之后。
这一步在提交点之前，拒绝时虚机仍可恢复运行。两个边界：内容恰好全零**且**被文件系统存成空洞的页
会被误拒（当前两种方案都靠写入来物化，不产生这种页）；文件系统不支持 `SEEK_DATA` 时（`EINVAL`）
跳过校验并打一条告警。

`userspace_bitmap_flat` 按 64 位字整体搬运，代价是 `总页数 / 64` 次字操作，与虚机内存线性相关但常数极小。

### 3.4 提交点

```rust
// ────────── commit point ──────────
// Guest memory changes from here on. Any failure now leaves a VM that is
// partly at the target snapshot and partly at the present: Faulted.
```

`RollbackError::faults_vm()`（`rollback.rs:85`）把这个划分固化成代码：

```rust
NotPaused | Faulted | SnapshotFile(_) | Validation(_) | RevertBitmap(_)
| MemoryFile(_) | MemoryFileCoverage(_) | DirtyBitmap(_) | BitmapWrite(_) => false,
Memory(_) | Vcpu(_) | Gic(_) | Devices(_) => true,
```

- **提交点之前**：虚机一个字节都没动，恢复运行即可，沙箱照常可用。
- **提交点之后**：虚机是两个时刻的混合体，标记 `Faulted`，只能重建沙箱。

`match` 是穷尽的：加一个新错误变体时，编译器强迫你回答它属于哪一半。

### 3.5 阶段 4：内存

`restore_dirty`（`vstate/memory.rs:321`）遍历回滚集，把**连续的页合成一批**，每批一次 `pread`
直接读进 guest 内存映射。批的文件偏移事先已知，所以不需要单独 `seek`。
回滚集通常是成片的，合批后系统调用数从「每页一次」降到「每段一次」。

### 3.6 阶段 5：vCPU

vCPU 状态恢复派发到**各 vCPU 自己的线程**上执行（`restore_vcpu_states_in_place`，`lib.rs:582`）：
允许 `KVM_SET_ONE_REG` 这类 ioctl 的是 vCPU 线程的 seccomp 过滤器，VMM 线程直接调会被杀掉。
处于 Running 的 vCPU 会拒绝这个事件，这是暂停前提的又一道保险。

每个 vCPU 线程执行 `restore_state_in_place`（`arch/aarch64/vcpu.rs:298`），比「从文件加载」多出前后两步：

1. **排干挂起的 MMIO 退出**（`drain_pending_exit`，`vcpu.rs:379`）。arm64 KVM 对 MMIO 的完成是延迟的：
   用户态模拟完一次访问后，「把读到的值写进目的寄存器、PC + 4」要到**下一次 `KVM_RUN` 入口**才做；
   这份挂起状态在 KVM 内部、不在快照里，`KVM_ARM_VCPU_INIT` 也不清它。不排干，resume 后第一次
   `KVM_RUN` 会把旧时间线的回填套到刚写好的新寄存器上。排干的做法是一次 `immediate_exit = 1` 的
   `KVM_RUN`：KVM 先完成挂起的 MMIO 再检查 `immediate_exit`，不进 guest 就返回 `EINTR`。
   **保存侧同样要做**：pause 停车前先排干，checkpoint 拍到的才是自洽的 vCPU 状态。
2. **复位 + 全寄存器恢复**：`KVM_ARM_VCPU_INIT`（架构定义的复位）→ SVE 的 VLS 寄存器在 finalize 之前写
   → finalize → 写全部寄存器 → `set_mpstate`。回滚固定走这条路线，没有「只写寄存器」的开关。
3. **读回核心寄存器比对**（`verify_restored_core_regs`，`vcpu.rs:406`）：不一致只计数与告警，不让回滚失败
   —— 此时已过提交点，失败只会把一台可疑的虚机变成一台死的。

三个计数（本次排干次数、排干失败、读回不一致）随回滚应答带回 orchestrator（§6）。

### 3.7 阶段 6：中断控制器

GIC 恢复需要各 vCPU 的 MPIDR，由阶段 5 用到的那份 vCPU 状态构造 —— 两步的顺序由数据依赖固定。

恢复作用在**既有的 GIC 设备 fd** 上，这带来重建路线没有的问题：GIC 的 enable / pending / active
是「写 1 生效」的成对寄存器（`IS*` / `IC*`），KVM 的写接口只处理值里为 1 的位。新建的 GIC 全 0，
写快照值就等于赋值；活着的 GIC 上，**快照里为 0、当前为 1 的位会原样留下**。active 位残留尤其致命
—— 那条中断此后不再投递。所以回滚调用 `restore_state_absolute`（`arch/aarch64/gic/gicv3/regs/mod.rs:61`）：
先做普通恢复，再对分发器与各重分发器的 active / pending / enable 三对寄存器**先清后设**
（`set_dist_regs_absolute`，`dist_regs.rs:165`），结果与快照逐位相等。

之后单独清一次串口那根 SPI 的 active / pending（`clear_serial_gic_line`，`rollback.rs:522`）。
理由：阶段 7 对串口是**复位**而不是恢复（串口状态不在快照里），若快照里这根线恰好是 active，
它就与「刚复位的串口」矛盾。INTID 按设备注册顺序分配，从设备管理器取而不是写死。
这一步是 **best-effort**：它走的分发器寄存器写路径取决于宿主内核是否放行，失败只在第一次打 warn、
之后降为 debug，不让整次回滚失败 —— 后果仅是串口可能在本次回滚后卡住，其它设备不受影响。

### 3.8 阶段 7：设备状态

对每个 virtio 设备（块、网卡、熵源）做三件事（`apply_one_device`）：

1. MMIO transport 寄存器：纯字段写；
2. 依快照**对当前 guest 内存**重建队列（`build_queues_checked`，校验队列数与最大长度），覆盖活队列对象；
3. 写回协商特性 `acked_features` 与中断状态。

第 2 步依赖阶段 4 已完成：队列要从 guest 内存读环的地址与索引，内存必须已经是目标时刻的样子。

**串口**：内部状态不在快照里，回滚后补成与 guest 驱动认知一致的硬件状态（`reset_serial_for_rollback`）。
只像普通恢复路径那样写 `IER = 0x01` 不够：guest 内存可能回到「正在发送、THRI 已打开」的时刻，
8250 驱动只在影子值的 THRI 位由 0 变 1 时才回写 IER，设备侧的 THRE 使能被抹掉后发送完成中断永不再来，
guest 往串口写会永久阻塞。所以复位时清 IIR，同时打开 RDA 与 THRE，再触发一次（边沿）中断线。

**网卡**多两步：

- `apply_net_rx_cache`：重建 RX 描述符缓存，原因与做法见 [18](18-rollback-pitfalls.md)；
- `apply_net_tap_offload`（`rollback.rs:1169`）：从活设备读回刚写进去的 `acked_features`，据此重设 tap 的
  offload 标志。guest 在 checkpoint 之后重协商过特性（驱动重载、`ip link down/up`、`ethtool -K`）时，
  两者不一致会让 vnet header 长度对不上，帧静默出错。

**有意不回滚**的两样，保留活对象：限速器（令牌桶延续当前值，至多一个补充周期内额度反映被丢弃时间线的流量）、
MMDS 网络栈（跨着一次在途 MMDS 请求回滚，guest 手里那条连接会超时）。

### 3.9 阶段 8：VMGenID

VMGenID 是一个 ACPI 设备，代号放在 guest 物理内存里。**代号变了 = 时间被回拨了**，guest 据此重新播种
随机数生成器等依赖时间单调的东西。写的是一个**新**代号，不是快照里那个 —— guest 要知道的是
「你被回滚了」这件事。必须在阶段 4 之后，否则新代号被内存回滚覆盖（[18](18-rollback-pitfalls.md)）。
没有 VMGenID 设备时只打一条日志，不算失败。

### 3.10 阶段 9：重置脏页基线

```rust
vmm.vm.guest_memory().reset_dirty();          // 只清用户态位图
for_each_virtio_device(|..| if activated { mark_queue_memory_dirty(..) })?;
```

下一代 Diff 必须相对**刚恢复的这个状态**。这一步只需要清用户态位图：KVM 那侧在阶段 3 已被破坏性读清空，
阶段 3–9 之间虚机一直暂停、没有 vCPU 跑过，而阶段 4 的写回走的是 VMM 地址空间里的映射，KVM 不记录。
这是 [14](14-memory-diff-tree.md) 里「纪元位图恰好覆盖 `(parent(x), x]`」的另一半 —— 账本那边把
`ParentID` 设成回滚目标，这边把跟踪清零，两件事必须同时做。

**队列页要重新标脏**：设备运行期写队列不经过 Stage-2 故障，KVM 不知道；刚清空的位图里没有它们，
下一代 Diff 就会漏掉（[13](13-dirty-page-tracking-and-hdbss.md)）。只对已激活设备做。

---

## 4. `validate_topology`：为什么必须一致

原地回滚是把状态写到**既有的**对象上。对象不存在或不一样，就没有可写的目标 ——
重建可以按快照造出任何拓扑，原地不行。检查项（`rollback.rs:636`）：

| 项 | 不一致时 |
|---|---|
| vCPU 数量 | 拒绝 |
| guest 内存大小 | 拒绝 |
| 快照里或运行中有 balloon / vsock | **一律拒绝** |
| 快照里的每个设备（类型 + id）必须在运行中的虚机里存在 | 拒绝，指名道姓 |
| 每个设备的 **activated 状态**必须一致 | 拒绝 |
| 网卡的 `avail_features`（设备提供的特性集）必须一致 | 拒绝 |
| vhost-user block | **一律拒绝** |
| virtio 设备总数必须相等 | 拒绝 |

- **activated 也要对**：未被 guest 驱动激活的设备还没建立队列，把「已激活」的快照状态写到「未激活」的活设备上，
  会得到内部不自洽的对象。
- **`avail_features` 要对**：`acked_features` 会变、回滚能修（§3.8）；`avail_features` 在设备建出时由 tap 能力与
  Firecracker 构建决定、运行期不变。不一致说明快照是对另一台设备拍的，回滚写上去的队列、RX 缓冲布局、
  offload 标志都会按错误的契约解释。
- **balloon 与 vsock 直接拒绝**：balloon 把 guest 内存还给宿主，与「按页回滚内存」的交互没有论证过；
  vsock 有跨 guest / host 的连接状态，回滚它需要额外协议。明确拒绝好过一个可能错的实现。

以上全部在提交点之前，拒绝时虚机原样可恢复。

---

## 5. 失败模型

```
         ┌── 阶段 1 校验失败 ──────┐
         ├── 阶段 2 静默失败 ──────┤  虚机原封不动
         ├── 阶段 3 取图 / 覆盖失败 ┤  HTTP 400；orchestrator 恢复运行，沙箱继续可用
         │                         │
 ═══════════════ 提交点 ═══════════════
         │                         │
         ├── 阶段 4 内存失败 ──────┐
         ├── 阶段 5 vCPU 失败 ─────┤  Faulted；HTTP 500 且响应体 "fault": true
         ├── 阶段 6 GIC 失败 ──────┤  拒绝 resume 与 snapshot
         └── 阶段 7–9 设备失败 ────┘  只能重建沙箱
```

orchestrator 侧（`internal/sandbox/checkpoint.go` — `RollbackInPlace`）对应地：

- `RollbackFaultedError`（按响应体的 `fault` 字段判定）→ `RollbackTornError`（`checkpoint.go:620`）；
- rollback 调用在 `CHECKPOINT_FC_CALL_TIMEOUT` 内没应答 → 同样按撕裂处理（`checkpoint.go:630`）：
  无法判断 Firecracker 停在提交点哪一侧，恢复运行一个可能半新半旧的 guest 会让它把说不清的状态写进磁盘；
- 其余错误 → join conntrack 清理后恢复虚机，按普通失败返回，沙箱在原状态继续运行。

**磁盘那一半也在提交点之后**：`ResetView` 失败时内存已经在目标时刻、磁盘还不是，同样返回 `RollbackTornError`。
磁盘视图的**装配**（`AssembleView`）则在暂停之前完成，装配失败只是一次普通的失败 restore
（[19](19-end-to-end.md)）。完整的失败分级见 [20](20-failure-semantics.md)，错误码与 SDK 异常见
[03](03-errors-timeouts-concurrency.md)。

---

## 6. 计时与回报

每个阶段各自计时，**单位微秒**（这些阶段常在毫秒以下），随应答返回（`vmm_config/snapshot.rs`）：

```rust
pub struct RollbackTimings {
    pub validate: u64, pub quiesce: u64, pub bitmap: u64, pub memory: u64,
    pub vcpus: u64, pub gic: u64, pub devices: u64, pub total: u64,
}
pub struct RollbackResponse {
    pub restored_pages: u64, pub restored_bytes: u64,
    pub timings_us: RollbackTimings, pub vcpu_counters: RollbackVcpuCounters,
}
```

- `bitmap` 是 `O(guest 内存)` 的，是 `memory` 阶段降不下去的底；
- `vcpu_counters`：`mmio_drained`、`mmio_drain_failed`、`readback_mismatch` 是**本次调用的增量**，
  `mmio_drained_pause_total` 是 pause 路径排干的进程累计值。orchestrator 不读 Firecracker 的 metrics 文件，
  所以这些计数只能搭应答回来；
- orchestrator 把它们摊平进自己的 timings：`fc_validate`、`fc_quiesce`、`fc_bitmap`、`fc_memory`、`fc_vcpus`、
  `fc_gic`、`fc_devices`、`fc_total`，以及 `fc_vcpu_mmio_drained_count`（Firecracker 不回报时这个键整个缺席，
  以区分「没报」和「为 0」）。各键定义见 [07](07-observability-reference.md)，实测分布见
  [25](25-results-and-compliance.md)；
- `restored_pages` 是「本次回退了多少」的直接度量，可与回滚集大小对照，对不上说明契约出了问题。

回滚后 Firecracker 还会按 info 级别逐设备记一行队列位置（`log_queue_diagnostics`），用途见 [18](18-rollback-pitfalls.md)。

---

## 7. 小结

1. 原地回滚的价值是**一个宿主资源都不重建**，尤其是 guest 工作集不用重新换页；代价正比于回滚集。
2. 执行环境是 **VMM 线程 + 虚机暂停**，九个阶段对 guest 是一个原子操作。
3. **提交点**把失败切成两半：之前原封不动可继续运行，之后撕裂、标记 `Faulted`。`faults_vm()` 固化这个分类。
4. 阶段顺序由数据依赖决定：内存先于设备（队列从回滚后的内存读环），VMGenID 后于内存，GIC 依赖 vCPU 状态里的 MPIDR。
5. 回滚集必须被物化文件完整覆盖，阶段 3 在提交点前用 extent 遍历强制检查。
6. 写回**活对象**有几件重建路线不用管的事：挂起 MMIO 要排干、GIC 成对寄存器要绝对写回、串口要复位成自洽状态、
   tap offload 要跟着 `acked_features` 重设。
7. **拓扑必须一致**；balloon / vsock / vhost-user block 明确拒绝。

---

## 思考题

1. 阶段 7 重建队列时要从 guest 内存读环的地址与索引。把阶段 7 挪到阶段 4 之前，具体会读到什么？
   guest 恢复运行后第一件出错的事是什么？
2. 阶段 9 只清用户态位图、不清 KVM 位图。列出这个做法成立所依赖的全部前提；哪一条被打破会导致下一代 Diff 多记、哪一条会导致漏记？
3. 阶段 2 的网络静默有 4096 次上限。对端持续高速灌包、达到上限之后会发生什么？这是正确性问题吗？
4. 构造一个场景：快照与运行中虚机的设备集合完全相同、但 activated 不同。正常使用中会出现吗？

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 九阶段主体 | `firecracker/src/vmm/src/rollback.rs` — `rollback_snapshot_inner` |
| 失败分类、打 `Faulted` | 同上 — `RollbackError::faults_vm`、`brand_vm_on_faulting_failure` |
| 拓扑校验 | 同上 — `validate_topology`、`check_net_avail_features` |
| 覆盖校验 | 同上 — `validate_mem_file_coverage` |
| 设备静默、设备写回 | 同上 — `quiesce_devices`、`apply_device_states`、`apply_one_device`、`apply_net_tap_offload` |
| 串口 SPI | 同上 — `clear_serial_gic_line` |
| 内存写回 | `firecracker/src/vmm/src/vstate/memory.rs` — `restore_dirty` |
| vCPU | `firecracker/src/vmm/src/lib.rs` — `restore_vcpu_states_in_place`；`arch/aarch64/vcpu.rs` — `restore_state_in_place`、`drain_pending_exit`、`verify_restored_core_regs` |
| GIC 绝对写回 | `arch/aarch64/gic/gicv3/regs/mod.rs` — `restore_state_absolute`；`dist_regs.rs` — `set_dist_regs_absolute` |
| 端点参数与应答 | `firecracker/src/vmm/src/vmm_config/snapshot.rs` — `RollbackTimings`、`RollbackResponse`、`RollbackVcpuCounters` |
| orchestrator 侧 | `packages/orchestrator/internal/sandbox/fc/rollback.go`；`internal/sandbox/checkpoint.go` — `RollbackInPlace` |
