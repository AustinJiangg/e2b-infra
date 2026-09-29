# 09 · 进程内原地回滚

## 本章目标

读完本章，你应当能回答：

1. "重建进程加载快照"和"往活着的进程里写回差异"，成本差在哪些具体的项上？
2. `PUT /snapshot/rollback` 的九个阶段各做什么，为什么是这个顺序；哪几处顺序是被数据依赖锁死的？
3. "提交点"是什么，它把失败切成了哪两类，Firecracker 与 orchestrator 分别怎样处理这两类？
4. 为什么回滚前要做拓扑校验；为什么 vCPU 固定走"复位 + 全寄存器恢复"而不是"只写寄存器"？
5. 把状态写回**活对象**时，比重建多出了哪几件必须处理的事（MMIO 排干、GIC 绝对写回、串口、tap offload）？

---

前面几章从 orchestrator 这一侧准备好了回滚的全部输入：第 [06](06-memory-diff-tree.md) 章算出回滚集，并把"回滚集里每一页在目标时刻的内容"
物化成一个与 guest 内存等长的稀疏文件；第 [07](07-disk-layering.md) 章在窗口外装配好目标时刻的磁盘视图；上一章（[08](08-firecracker-api-contract.md)）
定义了 orchestrator 怎样把这些交给 Firecracker、怎样读懂它的应答。

本章走进 Firecracker 这一侧：`PUT /snapshot/rollback` 收到这两个文件之后，怎样把一台**正在运行的虚机**原地拨回过去 ——
九个阶段，一个提交点。本章只讲正常路径；这条路径特有的、重建路线永远不会遇到的问题，放在下一章（[10](10-rollback-pitfalls.md)）。
主体代码是 `firecracker/src/vmm/src/rollback.rs`。

---

## 1. 两种恢复形态

恢复一台虚机有两种做法：**重建**一个新进程去加载快照，或者往**活着的**进程里写回差异。
前者的代价取决于虚机多大，后者取决于回退了多少。

### 1.1 重建：新进程加载快照

这是 Firecracker 的标准恢复路径，也是 e2b 原生 resume 用的那条（[02](02-e2b-native-snapshot.md)）：

```
新建进程 → 建 KVM VM → 建 vCPU → 建 GIC → 建 virtio 设备
        → 注册 eventfd / irqfd / ioeventfd → 建 tap → 挂 UFFD
        → 加载 vmstate → 恢复 vCPU 与设备 → 运行
        → guest 逐页缺页，把工作集换回内存
```

每一项都要重新付一遍。最后那一步尤其贵：新进程的 guest 内存映射是空的，**恢复后的头几秒里，guest 的每一次内存访问都可能是一次缺页**，
要经用户态 handler 从快照文件（或对象存储）取回。guest 的工作集越大，这段"热身"越长，而且它发生在 resume 之后，
不在任何冻结窗口的计时里，却实实在在地落在业务的第一批请求上。

### 1.2 原地：往活着的对象上写

```
虚机暂停 → 校验 → 静默 I/O → 把差异页写回活映射
        → 把 vCPU / GIC / 设备状态写回既有对象 → 恢复运行
```

**一个宿主资源都不重建。** 具体是哪些：

| 资源 | 重建 | 原地 |
|---|---|---|
| 进程、KVM VM fd、vCPU fd | 新建 | 保留 |
| GIC 设备 fd | 新建 | 保留 |
| eventfd / irqfd / ioeventfd 注册 | 全部重做 | 保留 |
| tap 设备、网络槽位 | 重建 | 保留 |
| guest 内存映射 | 重新 mmap | 保留 |
| **guest 工作集** | **靠缺页重新换入** | **留在物理内存里** |
| NBD 设备、Overlay | 重建 | 保留，只换视图（[07](07-disk-layering.md)） |

原地路线的代价**正比于回滚集大小**，与虚机规格无关：没被回退的页原封不动地留在物理内存里，guest 恢复后访问它们不需要任何缺页。
这一点是整套方案"快"的来源。后面所有的复杂度 —— 拓扑校验、设备静默、RX 缓存重建 —— 都是为它付的价。

---

## 2. 执行环境

回滚在 **VMM 线程**上执行，且**虚机必须处于暂停态**。这意味着世界是冻结的：没有 vCPU 在执行指令，没有设备事件在触发，
没有任何人能观察到中间状态，所以整个九阶段序列对 guest 是一个原子操作。代码的第一件事就是确认这一点（`rollback.rs:271` 起）：

```rust
match vmm.instance_info.state {
    VmState::Paused => {}
    VmState::Faulted => return Err(RollbackError::Faulted),
    _ => return Err(RollbackError::NotPaused),
}
```

`Faulted` 是**之前**某次回滚在提交点之后失败留下的状态。这样的虚机拒绝一切 resume 与 snapshot 操作 —— 它的状态介于两个时刻之间，
任何进一步的操作都只会扩大损害。

打 `Faulted` 标记发生在 `rollback_snapshot` 自己里面（`brand_vm_on_faulting_failure`，`rollback.rs:180`），不依赖 HTTP 层。
代码注释说明了为什么放在这里：`Faulted` 是虚机的属性，不是传输层的属性。若交给 HTTP 层去标，任何别的调用方（一个测试、将来的进程内调用）
拿到提交点之后的错误时，虚机仍然自称 `Paused`，也就是"可以恢复运行" —— 恰好是最不该发生的事。

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

第 10 步 —— 踢设备去重新处理（已被回退的）队列 —— 不在这里，它发生在 `resume_vm` 里，那个函数**每次恢复都会无条件踢一遍**
（`rollback.rs:466` 的注释）。所以回滚本身不必为此多做什么。

### 3.1 阶段 1：校验

任何一项不过就直接返回，虚机原封不动（`rollback.rs:271-325`）：

```rust
let state = snapshot_state_from_file(&params.snapshot_path)?;        // 快照能读
validate_topology(vmm, &state, mem_size)?;                           // 拓扑一致（§4）
if mem_file_len != mem_size { ... }                                  // 内存文件长度 == guest 内存
if u64_to_usize(mem_size) != total_pages * page_size { ... }         // guest 内存是整数个页
if bm_page_size != page_size || bm_pages != total_pages { ... }      // 位图几何对得上
```

"内存文件长度等于 guest 内存"是第 [06](06-memory-diff-tree.md) 章物化文件那条契约的另一端：orchestrator 物化的是一个与 guest 内存**等长**的稀疏文件，
页 `p` 的内容放在偏移 `p × 页大小` 上，所以两边按同一个平坦的页号空间对话（[08](08-firecracker-api-contract.md) §5.2）。
"guest 内存是整数个页"则是这个平坦页号空间存在的前提。

### 3.2 阶段 2：静默在途 I/O

设备可能有还没完成的 I/O。如果不管它，一个在回滚**之前**发起的操作可能在回滚**之后**完成，把一个属于旧时间线的结果写进新时间线
（`quiesce_devices`，`rollback.rs:747`）：

```rust
TYPE_BLOCK => { block.prepare_save(); }        // 等异步引擎收尾
TYPE_NET   => {
    // tap 里缓存的帧是发给正在被丢弃的那条时间线的，读出来扔掉。
    let fd = net.tap.as_raw_fd();
    let mut buf = [0u8; 65562];
    for _ in 0..4096 {
        let n = unsafe { libc::read(fd, buf.as_mut_ptr().cast(), buf.len()) };
        if n <= 0 { break; }                   // EAGAIN（tap 是非阻塞的）或读空
    }
}
```

网络那段有两个出口：`n <= 0` 处理正常情况（非阻塞 fd 读空返回 `EAGAIN`）；4096 次是一个**兜底的循环上限** ——
在一个持续高速灌包的环境里，不能让静默阶段变成一个无限循环，把冻结窗口拖到无限长。

达到上限之后会怎样？剩下的帧留在 tap 里，guest 恢复后照常收到。它们是发给被丢弃那条时间线的包：guest 的 TCP 状态已经回到目标时刻，
这些段落在序号窗口之外会被丢弃或以 RST 回应，UDP 应用则可能收到旧时间线的数据报。这与真实网络上的重复包、迟到包是同一类情况，
协议栈本来就要容忍 —— 所以这是一个"多收到几个旧包"的问题，不是内存正确性问题（推论）。

### 3.3 阶段 3：取活跃脏图与覆盖校验

```rust
let kvm_bitmap = vmm.vm.get_dirty_bitmap()?;                      // 破坏性读
vmm.vm.guest_memory().store_dirty_bitmap(&kvm_bitmap, page_size);  // 立刻折回
let mut revert = userspace_bitmap_flat(vmm.vm.guest_memory(), page_size, total_pages);
if let Some(words) = &file_bitmap {
    for (dst, src) in revert.iter_mut().zip(words.iter()) { *dst |= *src; }
}
validate_mem_file_coverage(&mem_file, &revert, page_size, total_pages)?;
```

顺序不能变：

1. **取**：`KVM_GET_DIRTY_LOG` 取走并清空内核位图（[05](05-dirty-page-tracking-and-hdbss.md) §3.1）；
2. **折回**：立刻 OR 进用户态位图。此后无论发生什么（包括后面的校验失败），"这些页脏过"的信息都不会丢；
3. **求并**：`revert = 活跃脏页 ∪ 调用方给的累积集合`；
4. **覆盖校验**（`validate_mem_file_coverage`，`rollback.rs:858`）：用 `SEEK_DATA` / `SEEK_HOLE` 逐段走内存文件的 extent，
   回滚集里任何一页落在文件空洞上就拒绝（做法与边界见 [08](08-firecracker-api-contract.md) §3.3）。

第 3 步说明 Firecracker **自己**会把活跃脏页并进来，所以 orchestrator 必须事先知道这个集合并物化它 ——
这就是 [08](08-firecracker-api-contract.md) §4 的 `save-dirty-bitmap` 端点存在的原因。第 4 步是这条契约的执行手段：
长度检查分不出"完整视图"和"被 `set_len` 撑长的 Diff 层"，也抓不住"调用方漏物化了某页"；不拦下来，`restore_dirty` 会从空洞读到零写进 guest，
而且发生在提交点之后。这一步放在提交点之前，拒绝时虚机仍可恢复运行。

`userspace_bitmap_flat` 按 64 位字整体搬运，代价是 `总页数 / 64` 次字操作。它与虚机内存线性相关，但常数极小：
2 GiB 内存是 8192 个字。这是回滚里**唯一**与虚机规格线性相关的一步（[11](11-end-to-end.md) §4），也是 `fc_bitmap` 降不下去的底。

### 3.4 提交点

```rust
// ────────── commit point ──────────
// Guest memory changes from here on. Any failure now leaves a VM that is
// partly at the target snapshot and partly at the present: Faulted.
```

这条注释是整个文件里最重要的一行。它把错误分成了两类，`RollbackError::faults_vm()`（`rollback.rs:85`）把这个分类固化成代码：

```rust
pub fn faults_vm(&self) -> bool {
    match self {
        // 提交点之前：guest 状态一个字节都没动
        NotPaused | Faulted | SnapshotFile(_) | Validation(_) | RevertBitmap(_)
        | MemoryFile(_) | MemoryFileCoverage(_) | DirtyBitmap(_) | BitmapWrite(_) => false,
        // 提交点之后：guest 是两个时刻的混合体
        Memory(_) | Vcpu(_) | Gic(_) | Devices(_) => true,
    }
}
```

- **提交点之前**：虚机一个字节都没动，恢复运行即可，沙箱照常可用。
- **提交点之后**：虚机是两个时刻的混合体，标记 `Faulted`，只能重建沙箱。

`match` 是穷尽的：加一个新错误变体时，编译器强迫你回答它属于哪一半。

### 3.5 阶段 4：内存

```rust
let (restored_pages, restored_bytes) = vmm.vm.guest_memory()
    .restore_dirty(&mem_file, &revert, page_size)?;
```

`restore_dirty`（`vstate/memory.rs:321`）按 64 位字遍历回滚集，只处理置位的页，把**连续的页合成一批**，每批一次 `pread`
直接读进 guest 内存映射。批的文件偏移事先已知（区间起始页号 × 页大小），所以不需要单独 `seek` —— 在回滚集很稀疏、多数批只有一页时，
省掉的 `seek` 就是一半的系统调用。回滚集通常是成片的（一个进程动过的内存往往连续），合批之后系统调用数从"每页一次"降到"每段一次"。

写回走的是 VMM 地址空间里的映射，KVM 不会记录这些写；函数仍然在用户态位图上把写过的页标脏（注释说明这是为了不给将来的调用方留陷阱），
反正阶段 9 会把用户态位图清掉。

### 3.6 阶段 5：vCPU

vCPU 状态恢复派发到**各 vCPU 自己的线程**上执行（`restore_vcpu_states_in_place`，`lib.rs:582`）：

```rust
for (handle, state) in self.vcpus_handles.iter().zip(states.into_iter()) {
    handle.send_event(VcpuEvent::RestoreState(Arc::new(state)))?;
}
// … 等所有 vCPU 回 VcpuResponse::RestoredState
```

为什么必须在 vCPU 线程上做？因为 **seccomp**：每个线程有自己的过滤器，允许 `KVM_SET_ONE_REG` 这类 ioctl 的是 vCPU 线程的那一份，
VMM 线程直接调会被杀掉（[08](08-firecracker-api-contract.md) §7）。处于 Running 的 vCPU 会拒绝这个事件（`NotAllowed`），这是暂停前提的又一道保险。

每个 vCPU 线程执行 `restore_state_in_place`（`arch/aarch64/vcpu.rs:298`），与"从文件加载"用的是同一套复位与写寄存器序列，
只是作用在既有的 fd 上，并且前后各多一步：

1. **排干挂起的 MMIO 退出**（`drain_pending_exit`，`vcpu.rs:379`）。arm64 KVM 对 MMIO 的完成是延迟的：用户态模拟完一次访问后，
   "把读到的值写进目的寄存器、PC + 4"要到**下一次 `KVM_RUN` 入口**才做；这份挂起状态在 KVM 内部、不在快照里，`KVM_ARM_VCPU_INIT` 也不清它。
   不排干，resume 后第一次 `KVM_RUN` 会把旧时间线的回填套到刚写好的新寄存器上 —— 某个寄存器被悄悄改掉，PC 多走一条指令。
   排干的做法是一次 `immediate_exit = 1` 的 `KVM_RUN`：KVM 先完成挂起的 MMIO，再检查 `immediate_exit`，不进 guest 就返回 `EINTR`。
   **保存侧同样要做**：pause 停车前先排干，checkpoint 拍到的才是自洽的 vCPU 状态。
2. **复位 + 全寄存器恢复**：`KVM_ARM_VCPU_INIT`（架构定义的复位）→ SVE 的 VLS 寄存器在 finalize 之前写 → finalize → 写全部寄存器 → `set_mpstate`。
3. **读回核心寄存器比对**（`verify_restored_core_regs`，`vcpu.rs:406`）：不一致只计数与告警，不让回滚失败 ——
   此时已过提交点，失败只会把一台可疑的虚机变成一台死的。

三个计数（本次排干次数、排干失败、读回不一致）随回滚应答带回 orchestrator（§6）。

#### 为什么固定走"复位 + 全恢复"

曾经有一条更"轻"的路线：**只写寄存器，不做 `KVM_ARM_VCPU_INIT`**。直觉上它更快 —— 少一次架构复位。
实测把它否掉了：在压力测试的同一道门槛上，只写寄存器的 p95 反而是复位路线的约三倍。这组数出自代码注释而不是本书的测量 ——`rollback.rs` 阶段 5 处的注释记下了那次门槛的 p95：复位路线 48.3 ms、只写寄存器 145.3 ms（注释没有写平台与负载）；它是这个设计决定的依据，所以作为例外留在原理篇里。代码连同那条路线的开关一起删掉了，回滚**固定**走复位路线。

直觉为什么失效？没有做过区分性实验，下面是推论：跳过复位并不意味着少做事，它意味着 vCPU 保留了一些快照之外的 KVM 内部状态，
而这些状态之后要靠别的机制去收敛；复位把 vCPU 放回架构定义的起点，再一次性写满，得到的是一个与快照逐项一致、不需要再收敛的状态。
§3.6 第 1 步的"挂起 MMIO 连 `KVM_ARM_VCPU_INIT` 都不清"说明，这类快照之外的内部状态确实存在。

这里还有一条方法论：一个"显然更快"的路线，用一次真实负载的尾延迟测量就否掉了；保留两条路线加一个开关看起来稳妥，
实际上是把一个已经有答案的问题永久留在代码里，让每个后来者都要重新论证一遍。

### 3.7 阶段 6：中断控制器

GIC 恢复需要各 vCPU 的 MPIDR，由阶段 5 用到的那份 vCPU 状态构造（`construct_kvm_mpidrs`）—— 两步的顺序由数据依赖固定。

恢复作用在**既有的 GIC 设备 fd** 上，不新建设备。这带来一个重建路线没有的问题：GIC 的 enable / pending / active 是"写 1 生效"的成对寄存器
（`IS*` 置位、`IC*` 清位），KVM 的写接口只处理值里为 1 的位。新建的 GIC 全 0，写快照值就等于赋值；而活着的 GIC 上，
**快照里为 0、当前为 1 的位会原样留下**。active 位残留尤其致命 —— 那条中断此后不再投递，对应的设备看起来就"卡住了"。

所以回滚调用 `restore_state_absolute`（`arch/aarch64/gic/gicv3/regs/mod.rs:61`）：先做普通恢复，再对分发器与各重分发器的
active / pending / enable 三对寄存器**先清后设**（`set_dist_regs_absolute`，`dist_regs.rs:165`），结果与快照逐位相等。

之后单独清一次串口那根 SPI 的 active / pending（`clear_serial_gic_line`，`rollback.rs:522`）。理由：阶段 7 对串口是**复位**而不是恢复
（串口的内部状态不在快照里），若快照里这根线恰好是 active，它就与"刚复位、以为自己从头开始的串口"矛盾；active 又是终态 ——
分发器不再投递这个 INTID，guest 写 IER 清不掉它，阶段 7 触发的那次中断也到不了 guest。再回滚一次也没用，因为下一次回滚又会从快照恢复同一个 active 位。
代码注释记下了在 920B 上观察到的样子：卡死时串口 INTID 读出 `P=1 A=1`（pending 被一个卡住的 active 挡在后面），同一虚机的 virtio INTID 只有 `E=1`。

有了绝对写回之后，快照里 active 为 0 的情形已经被阶段 6 本身清掉；这一步保留为双保险：它只花两次 ioctl，并且在绝对写回被宿主内核拒绝、
或快照里这根线恰好是 active 时仍然有效。INTID 按设备注册顺序分配，从设备管理器取而不是写死。

这一步是 **best-effort**：它走分发器寄存器的 uaccess 写路径，宿主内核有权拒绝；失败只在第一次打 warn、之后降为 debug，不让整次回滚失败。
代价的权衡是：阶段 6、7 恢复的是 guest 离不开的状态，这一步只复位一根本来就会卡住的线；为它让整次回滚失败，
等于把"串口可能卡住"升级成"这台宿主上的虚机根本不能回滚"。

### 3.8 阶段 7：设备状态

对每个 virtio 设备（块、网卡、熵源）做三件事（`apply_one_device`）：

```rust
transport.apply_state(transport_state);                              // ① MMIO transport 寄存器：纯字段写

let queues = virtio_state.build_queues_checked(
    vmm.vm.guest_memory(), ty, expected_queues, expected_max_size)?;  // ② 依快照、对当前 guest 内存重建队列
for (live, snapshot) in device.queues_mut().iter_mut().zip(queues) {
    *live = snapshot;                                                 //    覆盖活队列对象
}

device.set_acked_features(virtio_state.acked_features);             // ③ 协商特性与中断状态
device.interrupt_status().store(virtio_state.interrupt_status, SeqCst);
```

第 ② 步依赖阶段 4 已经完成：代码注释写明，内存已先回退，所以环的内容与索引属于同一时刻。反过来想更清楚：若把阶段 7 挪到阶段 4 之前，
队列对象按快照的地址与索引建好，随后内存被回退到目标时刻 —— 单看队列对象似乎无害，但紧跟着的 RX 缓存重建（下面的 `apply_net_rx_cache`）
要**读 avail 环的内容**重新解析描述符，它读到的会是被丢弃那条时间线的环，解析出 guest 在目标时刻从未发出过的描述符，
guest 恢复后第一次收包就会报 `id N is not a head!`、RX 永久卡死（[10](10-rollback-pitfalls.md) §2）。逐设备诊断日志读到的环位置也会全是错的。

**串口**：内部状态不在快照里，回滚后要补成与 guest 驱动认知一致的硬件状态（`reset_serial_for_rollback`，`lib.rs:531`）。
只像普通恢复路径那样写 `IER = 0x01` 不够：guest 内存可能回到"正在发送、THRI 已打开"的时刻，8250 驱动只在影子值的 THRI 位由 0 变 1 时才回写 IER，
设备侧的 THRE 使能被抹掉后发送完成中断永不再来，guest 往串口写会永久阻塞。所以复位时清 IIR，同时打开 RDA 与 THRE，再触发一次（边沿）中断线。

**网卡**多两步：

- `apply_net_rx_cache`（`rollback.rs:1111`）：重建 RX 描述符缓存，原因与做法见 [10](10-rollback-pitfalls.md) §2；
- `apply_net_tap_offload`（`rollback.rs:1169`）：从活设备读回刚写进去的 `acked_features`，据此重设 tap 的 offload 标志。
  重建路线在加载时就做这一步；原地路线原本只回退了 `acked_features`，guest 在 checkpoint 之后重协商过特性（驱动重载、`ip link down/up`、`ethtool -K`）时，
  tap 仍按新的特性集工作，vnet header 长度对不上，帧静默出错。读回而不是作为参数传入，是为了不可能与 `apply_one_device` 写进去的值不一致。

**有意不回滚**的两样，保留活对象，并在日志里明说：

- **限速器**：令牌桶延续当前值，至多一个补充周期内，guest 的额度反映的是被丢弃时间线的流量；重建它要换一个 timerfd，
  在运行中的事件循环底下换 fd 不是一个可以盲目做的改动。后果有界且自愈；
- **MMDS 网络栈**：它的状态是一条到 MMDS 端点的半开 TCP 连接，跨着一次在途 MMDS 请求回滚，guest 手里那条连接会超时。
  MMDS 的数据存储不在 guest 内存里，也不回退，这正是 orchestrator 依赖的行为。

### 3.9 阶段 8：VMGenID

```rust
if let Some(vmgenid) = vmm.acpi_device_manager.vmgenid.as_mut() {
    vmgenid.refresh_generation(vmm.vm.guest_memory())?;
} else {
    info!("rollback: no VMGenID device; guest is not notified of the rewind");
}
```

VMGenID 是一个 ACPI 设备，代号放在 guest 物理内存里，guest 内核会读它。**代号变了 = 时间被回拨了**，guest 据此重新播种随机数生成器等
依赖"时间单调"的东西。写的是一个**新**代号，不是快照里那个 —— guest 要知道的是"你被回滚了"这件事本身，而不是"你回到了那个曾经的时刻"。

**位置是关键**：必须在阶段 4 之后。放在之前，新代号会被内存回滚覆盖掉，guest 什么都不知道（[10](10-rollback-pitfalls.md) §3）。
没有 VMGenID 设备时只打一条日志，不算失败 —— 这条路径要能在没有该设备的配置上工作。

### 3.10 阶段 9：重置脏页基线

```rust
vmm.vm.guest_memory().reset_dirty();          // 只清用户态位图
vmm.mmio_device_manager.for_each_virtio_device(|_, _, _, dev| {
    let d = dev.lock().unwrap();
    if d.is_activated() { d.mark_queue_memory_dirty(vmm.vm.guest_memory()) } else { Ok(()) }
})?;
```

下一代 Diff 必须相对**刚恢复的这个状态**。这是第 [06](06-memory-diff-tree.md) 章"纪元位图 `E_x` 恰好覆盖 `(parent(x), x]`"的另一半 ——
账本那边把 `ParentID` 设成回滚目标（[11](11-end-to-end.md) §3.5），这边把跟踪清零，两件事必须同时做才对得上。

为什么只清用户态位图就够？它依赖三个前提，缺一不可：

1. **KVM 那侧已经空了**：阶段 3 的破坏性读刚把它取走；
2. **阶段 3–9 之间没有 vCPU 跑过**：虚机全程暂停，KVM 位图里不会出现新页；
3. **阶段 4 的写回 KVM 不记录**：它走的是 VMM 地址空间里的映射，不经过 Stage-2 写保护。

哪一条被打破，后果方向不同：若前两条不成立（例如某处让 vCPU 在中间跑了一下），KVM 位图里会带着一些"基线之前"的页进入下一代 ——
下一代多记几页，回滚集仍是超集，只是多做功；若第三条不成立而又没有被清，情况相同。真正危险的是反方向：**Firecracker 自己在基线之后写了 guest 内存、
却没有记账**，下一代就会漏页。队列页就是这种情况，所以要补下面那一步。

**队列页要重新标脏**：设备运行期写队列（往 used 环放条目）不经过 Stage-2 故障，KVM 不知道（[05](05-dirty-page-tracking-and-hdbss.md) §3.2）；
刚清空的位图里没有它们，下一代 Diff 就会漏掉这些页。只对已激活设备做 —— 未激活的设备还没有队列内存可标。

---

## 4. `validate_topology`：为什么必须一致

原地回滚是把状态写到**既有的**对象上。对象不存在或不一样，就没有可写的目标 —— 这是它与重建路线的根本区别：重建可以按快照造出任何拓扑，原地不行。
检查项（`rollback.rs:636`）：

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

- **activated 也要对**：未被 guest 驱动激活的设备还没建立队列，把"已激活"的快照状态写到"未激活"的活设备上，会得到内部不自洽的对象。
  正常使用中很难碰到：checkpoint 都在 guest 启动完成（envd 已应答）之后发出，那时设备早已激活；这项检查主要挡住误用（推论）。
- **`avail_features` 要对**（`check_net_avail_features`，`rollback.rs:616`）：`acked_features` 会变、回滚能修（§3.8）；
  `avail_features` 在设备建出时由 tap 能力与 Firecracker 构建决定、运行期不变。不一致说明快照是对另一台设备拍的，回滚写上去的队列、
  RX 缓冲布局、offload 标志都会按错误的契约解释。
- **balloon 与 vsock 直接拒绝**，不是"暂不支持"的占位：balloon 的语义是把 guest 内存还给宿主，它与"按页回滚内存"的交互没有被论证过；
  vsock 有跨 guest / host 的连接状态，回滚它需要一套额外的协议。与其做一个可能错的实现，不如明确拒绝。

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

- `RollbackFaultedError`（按响应体的 `fault` 字段判定）→ `RollbackTornError`（`checkpoint.go:624`）；
- rollback 调用在 `CHECKPOINT_FC_CALL_TIMEOUT` 内没应答 → 同样按撕裂处理（`checkpoint.go:633`）：无法判断 Firecracker 停在提交点哪一侧，
  恢复运行一个可能半新半旧的 guest 会让它把说不清的状态写进磁盘；
- 其余错误 → join conntrack 清理后恢复虚机，按普通失败返回，沙箱在原状态继续运行。

**磁盘那一半也在提交点之后**：`ResetView` 失败时内存已经在目标时刻、磁盘还不是，同样返回 `RollbackTornError`（`checkpoint.go:687`）。
磁盘视图的**装配**（`AssembleView`）则在暂停之前完成，装配失败只是一次普通的失败 restore（[11](11-end-to-end.md) §3.1）。

提交点之后的失败在真实环境里极难碰到，但它恰恰是整套 `Faulted` 机制存在的理由，所以两侧都留了只用于测试的触发点：
Firecracker 以 `rollback-fault-inject` 特性构建时，`FC_ROLLBACK_FAULT_INJECT=post_commit` 在阶段 5 与阶段 6 之间注入一个 `RollbackError::Gic`
（与真实的阶段 6 失败是同一个变体，下游看到的是逐字节相同的提交点后失败；生产构建里这段代码与字符串都不存在，`rollback.rs:195-258`）；
orchestrator 的 `RollbackInPlace` 接受一个 `afterCommit` 回调，生产路径从不传它。完整的失败分级见 [12](12-failure-semantics.md)，
错误码与 SDK 异常见 [25](25-errors-timeouts-concurrency.md)。

---

## 6. 计时与回报

每个阶段各自计时，**单位微秒**（这些阶段常在毫秒以下，毫秒精度会把它们全变成 0），随应答返回（`vmm_config/snapshot.rs`）：

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

- `bitmap` 是 `O(guest 内存)` 的（§3.3），是 `memory` 阶段降不下去的底；
- `vcpu_counters`：`mmio_drained`、`mmio_drain_failed`、`readback_mismatch` 是**本次调用的增量**，`mmio_drained_pause_total` 是 pause 路径排干的进程累计值。
  Firecracker 在回滚开始前和结束后各取一次整组计数、相减得出增量 —— 这只因为虚机全程暂停才成立：没有 vCPU 在跑，两次读数之间只有本次调用能动这些计数。
  orchestrator 不读 Firecracker 的 metrics 文件，所以这些计数只能搭应答回来；
- orchestrator 把它们摊平进自己的 timings：`fc_validate`、`fc_quiesce`、`fc_bitmap`、`fc_memory`、`fc_vcpus`、`fc_gic`、`fc_devices`、`fc_total`，
  以及 `fc_vcpu_mmio_drained_count`（Firecracker 不回报时这个键整个缺席，以区分"没报"和"为 0"）。理由是 Firecracker 这一段是冻结窗口的主体，
  把它留成一个不透明的总数，就等于放弃了定位问题的能力。各键定义见 [28](28-observability-reference.md)，实测分布见 [21](21-benchmarks-and-compliance.md)；
- `restored_pages` 是"本次回退了多少"的直接度量，可与回滚集大小对照，对不上说明契约出了问题。

回滚后 Firecracker 还会按 info 级别逐设备记一行队列位置（`log_queue_diagnostics`），用途见 [10](10-rollback-pitfalls.md) §7。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 九阶段主体 | `firecracker/src/vmm/src/rollback.rs` — `rollback_snapshot_inner` |
| 失败分类、打 `Faulted` | 同上 — `RollbackError::faults_vm`、`brand_vm_on_faulting_failure` |
| 测试用故障注入 | 同上 — `injected_post_commit_fault`（仅 `rollback-fault-inject` 构建） |
| 拓扑校验 | 同上 — `validate_topology`、`check_net_avail_features` |
| 覆盖校验、位图展平 | 同上 — `validate_mem_file_coverage`、`userspace_bitmap_flat` |
| 设备静默、设备写回 | 同上 — `quiesce_devices`、`apply_device_states`、`apply_one_device`、`apply_net_rx_cache`、`apply_net_tap_offload` |
| 串口 SPI | 同上 — `clear_serial_gic_line` |
| 内存写回 | `firecracker/src/vmm/src/vstate/memory.rs` — `restore_dirty` |
| vCPU | `firecracker/src/vmm/src/lib.rs` — `restore_vcpu_states_in_place`、`reset_serial_for_rollback`；`arch/aarch64/vcpu.rs` — `restore_state_in_place`、`drain_pending_exit`、`verify_restored_core_regs` |
| GIC 绝对写回 | `arch/aarch64/gic/gicv3/regs/mod.rs` — `restore_state_absolute`；`dist_regs.rs` — `set_dist_regs_absolute` |
| 端点参数与应答 | `firecracker/src/vmm/src/vmm_config/snapshot.rs` — `RollbackTimings`、`RollbackResponse`、`RollbackVcpuCounters` |
| orchestrator 侧 | `packages/orchestrator/internal/sandbox/fc/rollback.go`；`internal/sandbox/checkpoint.go` — `RollbackInPlace` |

---

## 本章要点

1. 原地回滚的价值是**一个宿主资源都不重建**，尤其是 guest 工作集不用重新缺页换入；代价正比于回滚集，与虚机规格无关。
2. 执行环境是 **VMM 线程 + 虚机暂停**，九个阶段对 guest 是一个原子操作；`Faulted` 标记在 Firecracker 内部打，不依赖传输层。
3. **提交点**把失败切成两半：之前原封不动可继续运行（HTTP 400），之后撕裂、标记 `Faulted`（HTTP 500 + `fault`）。`faults_vm()` 以穷尽 `match` 固化这个分类。
4. 阶段顺序由数据依赖决定：内存先于设备（RX 缓存要从回滚后的环重新解析），VMGenID 后于内存（否则被覆盖），GIC 依赖 vCPU 状态里的 MPIDR；
   第十步"踢设备"在 resume 里无条件发生。
5. 回滚集必须被物化文件完整覆盖，阶段 3 在提交点前用 extent 遍历强制检查；取出的 KVM 脏页立刻折回用户态位图，任何路径上都不丢。
6. 写回**活对象**有几件重建路线不用管的事：挂起 MMIO 要排干、GIC 成对寄存器要绝对写回、串口要复位成自洽状态、tap offload 要跟着 `acked_features` 重设；
   限速器与 MMDS 网络栈有意不回滚。
7. vCPU 固定走"复位 + 全寄存器恢复"：更"轻"的只写寄存器路线被一次尾延迟测量否掉，连同开关一起删除。
8. **拓扑必须一致**；balloon / vsock / vhost-user block 明确拒绝；阶段 9 只清用户态位图成立的前提是 KVM 位图已空、中间没有 vCPU 运行、写回不被 KVM 记录，队列页要补标。
