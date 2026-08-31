# 11 · 进程内原地回滚

> 恢复一台虚机有两种形态：**重建**一个新进程去加载快照，或者往**活着的**进程里写回差异。
> 前者的代价取决于虚机多大，后者取决于回退了多少。本篇讲后者是怎么做到的 ——
> 分叉 Firecracker 的 `PUT /snapshot/rollback`，九个阶段，一个提交点。
>
> **读者**：工程师。本篇与[第 12 篇](12-rollback-pitfalls.md)是一对，
> 这一篇讲**正常路径**，那一篇讲**这条路径特有的坑**。
> **预备**：[第 8 篇 · 内存差分树](08-memory-diff-tree.md)（回滚集从哪来）、
> [第 10 篇 · 磁盘分层](10-disk-layering.md)（磁盘那一半）。
> **代码**：`src/vmm/src/rollback.rs`（618 行，本篇的主体）

---

## 0. 本篇要回答的问题

1. 「重建」和「原地回写」的成本差在哪些具体的项上？
2. 九个阶段各自做什么？为什么是这个顺序？
3. 「提交点」是什么，它把失败分成了哪两类？
4. 为什么回滚前要做拓扑校验？不校验会怎样？
5. vCPU 为什么要走「完整复位 + 全寄存器恢复」，而不是只写寄存器？

---

## 1. 两种恢复形态

### 1.1 重建：新进程加载快照

这是 Firecracker 的标准恢复路径，也是 e2b 原生 resume 用的那条：

```
新建进程 → 建 KVM VM → 建 vCPU → 建 GIC → 建 virtio 设备
        → 注册 eventfd / irqfd / ioeventfd → 建 tap → 挂 UFFD
        → 加载 vmstate → 恢复 vCPU 寄存器 → 恢复设备状态 → 跑起来
        → guest 开始运行，逐页缺页把工作集换回内存
```

每一项都要重新付一遍。最后那一步尤其贵：**恢复后的头几秒里，guest 的每一次内存访问
都可能是一次缺页**，要经用户态 handler 从文件或对象存储取回。

### 1.2 原地：往活着的对象上写

```
虚机暂停 → 校验 → 静默 I/O → 把差异页写回活映射
        → 把 vCPU / GIC / 设备状态写回既有对象 → 恢复运行
```

**一个宿主资源都不重建。** 具体是哪些：

| 资源 | 重建路线 | 原地路线 |
|---|---|---|
| 进程 | 新建 | 保留 |
| KVM VM fd / vCPU fd | 新建 | 保留 |
| GIC 设备 fd | 新建 | 保留 |
| eventfd / irqfd / ioeventfd 注册 | 全部重做 | 保留 |
| tap 设备、网络槽位 | 重建 | 保留 |
| guest 内存映射 | 重新 mmap | 保留 |
| **guest 工作集** | **靠缺页重新换入** | **保留在物理内存里** |
| NBD 设备、Overlay | 重建 | 保留（换视图，见[第 10 篇](10-disk-layering.md)） |

代价：**正比于回滚集大小**，与虚机规格无关。

> 这一段是整套方案「快」的全部来源。后面的复杂度 —— 拓扑校验、设备静默、
> RX 缓存重建（[第 12 篇](12-rollback-pitfalls.md)）—— 都是为这件事付的价。

---

## 2. 执行环境

回滚在 **VMM 线程**上执行，且**虚机必须处于暂停态**。

这意味着世界是冻结的：没有 vCPU 在执行指令，没有设备事件在触发，
没有任何人能观察到中间状态。整个九阶段序列是一个对 guest 而言的原子操作。

代码的第一件事就是确认这一点：

```rust
match vmm.instance_info.state {
    VmState::Paused => {}
    VmState::Faulted => return Err(RollbackError::Faulted),
    _ => return Err(RollbackError::NotPaused),
}
```

`Faulted` 是一次**之前**的回滚在提交点之后失败留下的状态。这样的虚机拒绝一切
resume 与 snapshot 操作 —— 它的状态介于两个时刻之间，任何进一步的操作都只会扩大损害。

---

## 3. 九个阶段

| # | 阶段 | 做什么 | 有副作用吗 |
|---|---|---|---|
| 1 | **校验** | 状态、拓扑、内存文件长度、位图几何 | 无 |
| 2 | **静默** | 排空在途设备 I/O | 有（丢弃 tap 缓存帧），但不改 guest 状态 |
| 3 | **取活跃脏图** | 取 KVM 脏页日志并折回用户态位图，与调用方给的位图求并 | 改的是 Firecracker 自己的账，不是 guest |
| | ══ **提交点** ══ | | |
| 4 | **内存** | 按回滚集把页写回活映射 | **改 guest 状态** |
| 5 | **vCPU** | 复位 + 全寄存器恢复 | 改 guest 状态 |
| 6 | **中断控制器** | 对既有 fd 恢复 GIC 状态 | 改 guest 状态 |
| 7 | **设备** | virtio 队列、协商特性、中断状态；串口重新初始化 | 改 guest 状态 |
| 8 | **VMGenID** | 刷新代号，通知 guest 时间被回拨 | 改 guest 状态 |
| 9 | **重置基线** | 清空脏页跟踪，重新标脏队列页 | 改 Firecracker 的账 |

第 10 步 —— 踢设备去重新处理（已被回退的）队列 —— 不在这里，
它发生在 `resume_vm` 里，那个函数**每次恢复都会无条件踢一遍**。

### 3.1 阶段 1：校验

四项检查，任何一项不过就直接返回，虚机原封不动：

```rust
let state = snapshot_state_from_file(&params.snapshot_path)?;   // 快照能读
validate_topology(vmm, &state, mem_size)?;                      // 拓扑一致（§4）
if mem_file_len != mem_size { ... }                             // 内存文件长度 == guest 内存
if bm_page_size != page_size || bm_pages != total_pages { ... }  // 位图几何对得上
```

「内存文件长度必须等于 guest 内存」这一条，正是[第 8 篇 §2.1](08-memory-diff-tree.md#21-稀疏文件)
里说的「稀疏文件天然满足」那条契约的另一端。

### 3.2 阶段 2：静默在途 I/O

设备可能有还没完成的 I/O。如果不管它，一个在回滚**之前**发起的操作可能在回滚**之后**完成，
把一个属于旧时间线的结果写进新时间线。

```rust
TYPE_BLOCK => { block.prepare_save(); }        // 等异步引擎收尾
TYPE_NET   => {
    // tap 里缓存的帧是发给正在被丢弃的那条时间线的，读出来扔掉。
    let fd = net.tap.as_raw_fd();
    let mut buf = [0u8; 65562];
    for _ in 0..4096 {
        let n = unsafe { libc::read(fd, buf.as_mut_ptr().cast(), buf.len()) };
        if n <= 0 { break; }                   // EAGAIN（tap 是非阻塞的）或读完
    }
}
```

网络那段有两个上限：`n <= 0` 处理正常情况（非阻塞 fd 读空返回 `EAGAIN`），
`4096` 次是一个**兜底的循环上限** —— 在一个持续高速灌包的环境里，
不能让静默阶段变成一个无限循环。

### 3.3 阶段 3：取活跃脏图

```rust
let kvm_bitmap = vmm.vm.get_dirty_bitmap()?;                    // 破坏性读
vmm.vm.guest_memory().store_dirty_bitmap(&kvm_bitmap, page_size); // 立刻折回

let mut revert = userspace_bitmap_flat(vmm.vm.guest_memory(), page_size, total_pages);
if let Some(words) = &file_bitmap {
    for (dst, src) in revert.iter_mut().zip(words.iter()) { *dst |= *src; }
}
```

三件事，顺序不能变：

1. **取**：`KVM_GET_DIRTY_LOG` 取走并清空内核位图（[第 7 篇 §3.1](07-dirty-page-tracking.md#31-破坏性读)）；
2. **折回**：立刻 OR 进用户态位图。此后无论发生什么，「这些页脏过」这个信息都不会丢；
3. **求并**：`revert = 活跃脏页 ∪ 调用方给的累积集合`。

第 3 步值得注意：Firecracker **自己**会把活跃脏页并进来。这就是
[第 8 篇 §5.1](08-memory-diff-tree.md#51-一条必须遵守的契约) 那条契约的来源 ——
orchestrator 必须事先知道这个集合，否则会有页落进文件空洞。

### 3.4 提交点

```rust
// ────────── commit point ──────────
// Guest memory changes from here on. Any failure now leaves a VM that is
// partly at the target snapshot and partly at the present: Faulted.
```

这条注释是整个文件里最重要的一行。它把错误分成了两类，
`RollbackError::faults_vm()` 把这个分类固化成代码：

```rust
pub fn faults_vm(&self) -> bool {
    match self {
        NotPaused | Faulted | SnapshotFile(_) | Validation(_)
        | RevertBitmap(_) | MemoryFile(_) | DirtyBitmap(_) | BitmapWrite(_) => false,
        Memory(_) | Vcpu(_) | Gic(_) | Devices(_) => true,
    }
}
```

**提交点之前**：虚机一个字节都没动，恢复运行即可，沙箱照常可用。
**提交点之后**：虚机是两个时刻的混合体，标记 `Faulted`，只能重建沙箱。

### 3.5 阶段 4：内存

```rust
let (restored_pages, restored_bytes) = vmm.vm.guest_memory()
    .restore_dirty(&mut mem_file, &revert, page_size)?;
```

`restore_dirty` 遍历回滚集，把**连续的页合成一批**，一次 `seek` + 一次
`read_exact_volatile` 直接读进 guest 内存的映射。

批处理很重要：回滚集通常是成片的（一个进程动过的内存是连续的），
逐页做就是几万次系统调用，合批之后是几百次。

### 3.6 阶段 5：vCPU

每个 vCPU 的状态恢复发生在**它自己的线程**上，通过事件通道派发：

```rust
handle.send_event(VcpuEvent::RestoreState(Arc::new(state)))?;
// … 等所有 vCPU 回 VcpuResponse::RestoredState
```

> **为什么必须在 vCPU 线程上做？** 因为 seccomp。每个线程有自己的过滤器，
> 允许 `KVM_SET_ONE_REG` 之类 ioctl 的是 vCPU 线程的那一份。
> VMM 线程直接调会被 seccomp 杀掉。

每个 vCPU 线程里执行的是与**加载快照完全相同**的序列，只是作用在既有的 fd 上：

```rust
self.kvi = state.kvi;
self.init_vcpu()?;            // KVM_ARM_VCPU_INIT —— 架构定义的复位
// SVE 的 VLS 寄存器必须在 finalize 之前写
self.finalize_vcpu()?;
for reg in state.regs { self.set_register(reg)?; }   // 全部寄存器
self.set_mpstate(state.mp_state)?;
```

处于 Running 状态的 vCPU 会拒绝这个事件（`NotAllowed("save/restore unavailable while running")`），
这是暂停前提的又一道保险。

### 3.7 阶段 6：中断控制器

```rust
#[cfg(target_arch = "aarch64")]
vmm.vm.restore_state(&mpidrs, &state.vm_state)?;
```

aarch64 上是 GIC。它需要 `mpidrs`（各 vCPU 的 MPIDR 值），由阶段 5 用到的那份
vCPU 状态构造出来 —— 所以这两步的顺序是被数据依赖固定的。

恢复作用在**既有的 GIC 设备 fd** 上，不新建设备。

### 3.8 阶段 7：设备状态

对每个 virtio 设备做三件事：

```rust
transport.apply_state(transport_state);      // ① MMIO transport 寄存器：纯字段写

let queues = virtio_state.build_queues_checked(
    vmm.vm.guest_memory(), ty, expected_queues, expected_max_size)?;   // ② 依快照重建队列
for (live, snapshot) in device.queues_mut().iter_mut().zip(queues) {
    *live = snapshot;
}

device.set_acked_features(virtio_state.acked_features);                // ③ 协商特性与中断状态
device.interrupt_status().store(virtio_state.interrupt_status, SeqCst);
```

第 ② 步依赖阶段 4 已经完成：队列对象要从 guest 内存里读环的地址与索引，
**内存必须已经是目标时刻的样子**，否则重建出来的队列描述的是一个不存在的时刻。

串口的内部状态不在快照里，所以按普通恢复路径的做法重新初始化一次：

```rust
vmm.emulate_serial_init()?;
```

网络设备还有一步 `apply_net_rx_cache` —— 那是原地回滚特有的，
放在[第 12 篇](12-rollback-pitfalls.md)讲。

### 3.9 阶段 8：VMGenID

```rust
if let Some(vmgenid) = vmm.acpi_device_manager.vmgenid.as_mut() {
    vmgenid.refresh_generation(vmm.vm.guest_memory())?;
} else {
    info!("rollback: no VMGenID device; guest is not notified of the rewind");
}
```

VMGenID 是一个 ACPI 设备，guest 内核会读它。**代号变了 = 时间被回拨了**，
guest 据此重新播种随机数生成器等对「时间单调」有依赖的东西。

写的是一个**新**代号，不是快照里那个 —— 因为 guest 要知道的是「你被回滚了」这件事本身，
而不是「你回到了那个曾经的时刻」。

**位置是关键**：必须在阶段 4 之后。放在之前，新代号会被内存回滚覆盖掉，
guest 什么都不知道。详见[第 12 篇](12-rollback-pitfalls.md)。

没有 VMGenID 设备时只打一条日志，不算失败 —— 这条路径要能在没有该设备的配置上工作。

### 3.10 阶段 9：重置脏页基线

```rust
vmm.vm.reset_dirty_bitmap();          // 清 KVM 侧
vmm.vm.guest_memory().reset_dirty();  // 清用户态侧

vmm.mmio_device_manager.for_each_virtio_device(|_, _, _, dev| {
    let d = dev.lock().unwrap();
    if d.is_activated() { d.mark_queue_memory_dirty(vmm.vm.guest_memory()) } else { Ok(()) }
})?;
```

两侧都清，因为下一代 Diff 必须相对**刚恢复的这个状态**。这条不变量就是
[第 8 篇 §2.2](08-memory-diff-tree.md#22-纪元epoch与它的位图) 里
「`E_x` 恰好覆盖 `(parent(x), x]`」的另一半 —— 账本那边把 `ParentID` 设成回滚目标，
这边把跟踪清零，两件事必须同时做才对得上。

**队列页要重新标脏**：设备在运行期写队列不经过 Stage-2 故障，KVM 不知道
（[第 7 篇 §3.2](07-dirty-page-tracking.md#32-两层位图)）。刚清空的位图里没有它们，
下一代 Diff 就会漏掉这些页。

---

## 4. `validate_topology`：为什么必须一致

原地回滚是把状态写到**既有的**对象上。对象不存在，就没有可写的目标 ——
这和「重建」路线的根本区别：重建可以按快照造出任何拓扑，原地不行。

检查项：

| 项 | 不一致时 |
|---|---|
| vCPU 数量 | 报错 |
| guest 内存大小 | 报错 |
| 快照里有 balloon / vsock | **一律拒绝** |
| 运行中有 balloon / vsock | **一律拒绝** |
| 每个设备（类型 + id）必须在运行中的虚机里存在 | 报错，指名道姓 |
| 每个设备的 **activated 状态**必须一致 | 报错 |
| vhost-user block | **一律拒绝** |
| 设备总数必须相等 | 报错 |

两点值得说：

**activated 状态也要对。** 一个尚未被 guest 驱动激活的设备，它的队列还没建立。
把一个「已激活」的快照状态写到一个「未激活」的活设备上，会产生一个内部不自洽的对象。

**balloon 与 vsock 直接拒绝**，不是「暂不支持」的占位。balloon 的语义是把 guest 内存还给宿主，
它与「按页回滚内存」的交互没有被论证过；vsock 有跨 guest / host 的连接状态，
回滚它需要一套额外的协议。与其做一个可能错的实现，不如明确拒绝。

---

## 5. vCPU 那一步的抉择

曾经有一条更「轻」的路线：**只写寄存器，不做 `KVM_ARM_VCPU_INIT`**。
直觉上它更快 —— 少一次架构复位。

实测把它否掉了：在压力测试的 G1 门槛上，

| 路线 | p95 |
|---|---|
| 只写寄存器 | **145.3 ms** |
| 复位 + 全恢复（reinit） | **48.3 ms** |

慢三倍。代码连同这条路线一起删掉了开关，回滚**固定**走 reinit。

> 直觉失效的原因值得琢磨（这也是[思考题 2](#思考题)）：跳过复位并不意味着少做事，
> 它意味着 vCPU 保留了一些不该保留的内部状态，而这些状态后续要靠别的机制去收敛。
>
> 更重要的是**方法论**：一个「显然更快」的路线，用一次真实负载的 p95 测量就否掉了。
> 保留两条路线加一个开关，看起来是稳妥，实际上是把一个已经有答案的问题永久留在代码里。

---

## 6. 失败模型总览

```
                     ┌── 阶段 1 校验失败 ────┐
                     ├── 阶段 2 静默失败 ────┤  虚机原封不动
                     ├── 阶段 3 取图失败 ────┤  orchestrator 恢复运行，沙箱继续可用
                     │                       │
        ════════════ 提交点 ════════════      │
                     │                       │
                     ├── 阶段 4 内存失败 ────┐
                     ├── 阶段 5 vCPU 失败 ───┤  Faulted
                     ├── 阶段 6 GIC 失败 ────┤  拒绝 resume 与 snapshot
                     └── 阶段 7 设备失败 ────┘  只能重建沙箱
```

orchestrator 侧对应地把 `RollbackFaultedError` 翻译成 `data_loss` 状态码，
其余翻译成 `internal` 并明确告诉调用方「沙箱仍在原状态运行」。
完整的失败分级见[第 14 篇](14-failure-semantics.md)。

**磁盘那一半也在提交点之后。** `ResetView` 失败时内存已经在目标时刻、磁盘还不是 ——
同样是撕裂，同样返回 `RollbackTornError`（[第 10 篇 §7.1](10-disk-layering.md#71-失败语义)）。

---

## 7. 计时与回报

每个阶段各自计时，结果随响应返回：

```rust
pub struct RollbackTimings {
    pub validate: u64, pub quiesce: u64, pub memory: u64,
    pub vcpus: u64, pub gic: u64, pub devices: u64, pub total: u64,
}
pub struct RollbackResponse {
    pub restored_pages: u64, pub restored_bytes: u64, pub timings_us: RollbackTimings,
}
```

单位是**微秒** —— 这里的阶段常常是毫秒以下，毫秒精度会把它们全变成 0。

orchestrator 收到后把它们摊平进自己的 `timings.json`（`fc_validate`、`fc_memory`、`fc_vcpus`…）。
理由：Firecracker 这一段是冻结窗口的主体，把它留成一个不透明的总数，
就等于放弃了定位问题的能力。见[第 17 篇](17-observability-and-verification.md)。

`restored_pages` 同样有用：它是「本次回退了多少」的直接度量，
可以和回滚集的大小对照 —— 两者不一致就说明契约出了问题。

---

## 8. 小结

1. 原地回滚的全部价值是**一个宿主资源都不重建**，尤其是 guest 工作集不用重新换页。
   代价正比于回滚集，与虚机规格无关。
2. 执行环境是 **VMM 线程 + 虚机暂停**，所以九个阶段对 guest 是一个原子操作。
3. **提交点**把失败切成两半：之前，虚机原封不动可继续运行；之后，虚机撕裂，
   标记 `Faulted` 并拒绝一切后续操作。这个分类在代码里由 `faults_vm()` 固化。
4. 阶段顺序由数据依赖决定：内存先于设备（队列要从回滚后的内存读环），
   VMGenID 后于内存（否则被覆盖），GIC 依赖 vCPU 状态里的 MPIDR。
5. vCPU 恢复派发到 **vCPU 自己的线程**，因为允许那些 ioctl 的是那个线程的 seccomp 过滤器。
6. **拓扑必须一致**，因为原地回滚是往既有对象上写。balloon / vsock / vhost-user block
   明确拒绝而不是勉强支持。
7. 「只写寄存器」这条看起来更快的路线，被一次 p95 测量否掉（145.3 vs 48.3 ms），
   连同开关一起删除。

---

## 思考题

1. 阶段 7 重建队列时要从 guest 内存读环的地址与索引。如果把阶段 7 挪到阶段 4 之前，
   具体会读到什么？guest 恢复运行后第一件出错的事是什么？
2. 「只写寄存器」比「复位 + 全恢复」慢三倍。给出至少两个可能的解释，
   并设计一个能区分它们的实验。
3. 阶段 2 的网络静默有一个 4096 次的循环上限。如果一个恶意 guest 的对端在持续高速灌包，
   达到上限之后会发生什么？这是安全问题吗？
4. `validate_topology` 检查设备的 activated 状态。构造一个场景：快照与运行中的虚机
   设备集合完全相同、但 activated 不同。这在正常使用中会出现吗？

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 九阶段主体 | `src/vmm/src/rollback.rs` — `rollback_snapshot` |
| 失败分类 | 同上 — `RollbackError::faults_vm` |
| 拓扑校验 | 同上 — `validate_topology` |
| 设备静默 | 同上 — `quiesce_devices` |
| 设备状态写回 | 同上 — `apply_device_states`、`apply_one_device` |
| 位图摊平 | 同上 — `userspace_bitmap_flat` |
| 内存写回 | `src/vmm/src/vstate/memory.rs` — `restore_dirty` |
| vCPU 事件派发 | `src/vmm/src/lib.rs` — `restore_vcpu_states_in_place` |
| vCPU 线程侧 | `src/vmm/src/vstate/vcpu.rs` — `VcpuEvent::RestoreState`；`arch/aarch64/vcpu.rs` — `restore_state` |
| 端点参数与响应 | `src/vmm/src/vmm_config/snapshot.rs` — `RollbackSnapshotParams`、`RollbackTimings` |
| orchestrator 侧客户端 | `internal/sandbox/fc/rollback.go` |
| 暂停窗口编排 | `internal/sandbox/checkpoint.go` — `RollbackInPlace` |

**下一篇**：[12 · 原地回滚特有的问题](12-rollback-pitfalls.md) —— 正常路径讲完了，
接下来是三个「重建路线永远不会遇到」的坑，每一个都花了不少时间才找到。
