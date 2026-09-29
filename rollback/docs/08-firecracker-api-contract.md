# 08 · Firecracker 接口契约

## 本章目标

读完本章，你应当能回答：

1. 上游 Firecracker 已经有 Diff 快照、脏页跟踪和快照加载，为什么还要分叉？分叉改了哪几处，为什么说它是"加法"？
2. `dirty_bitmap_path` 为什么必须在拍快照的**同一次调用**里写出，而不能事后另取？
3. `PUT /snapshot/rollback` 的错误怎么分级；orchestrator 靠什么判断虚机是"原样可恢复"还是"已经撕裂"？
4. `save-dirty-bitmap` 为什么是稀疏物化文件成立的前提？没有它会怎样？
5. FCDB 位图的页索引是什么空间；orchestrator 与 Firecracker 两侧版本配错时各会发生什么。

---

第 [04](04-architecture.md) 章把系统分成四个层次，并用一句话交代了"Firecracker 分叉了多少"；第 [05](05-dirty-page-tracking-and-hdbss.md) 章讲了脏页位图从哪来、
为什么 KVM 的取图是破坏性读、为什么有 KVM 与用户态两层位图；第 [06](06-memory-diff-tree.md) 章讲 orchestrator 怎样用每一代的位图算回滚集、
并把回滚集物化成两个文件交给 Firecracker；上一章（[07](07-disk-layering.md)）讲了磁盘那一半。

这些机制分布在两个进程里：**orchestrator 懂"树"，Firecracker 懂"虚机"**。本章把两者之间的边界单独拿出来讲 ——
它们之间的全部约定只有**一个可选字段、两个新端点、一种二进制格式**。每一处为什么存在、契约的边界在哪、错误怎么分级、配错版本会怎样，
都在这里说清。下一章（[09](09-in-place-rollback.md)）再走进 rollback 端点的内部。

---

## 1. 为什么要分叉

### 1.1 上游已经有什么

| 能力 | 说明 |
|---|---|
| Diff 快照 | `SnapshotType::Diff`，只写脏页，写在各自的文件偏移上 |
| 脏页跟踪 | `track_dirty_pages`，走 KVM 写保护（[05](05-dirty-page-tracking-and-hdbss.md)） |
| 全量恢复 | `PUT /snapshot/load`，在一个**新建的**进程里加载快照 |
| 暂停与恢复 | `PATCH /vm` |

### 1.2 上游缺什么

| 缺什么 | 为什么需要 |
|---|---|
| 告诉调用方"这次快照写了哪些页" | 差分文件本身不说明它含哪些页（§2.1），orchestrator 要用它算回滚集（[06](06-memory-diff-tree.md)） |
| 往活着的虚机写回状态 | 上游只有新建进程加载，成本与虚机规格挂钩（[09](09-in-place-rollback.md)） |
| 导出"当前还没进快照的脏页" | 回滚集必须包含它（[06](06-memory-diff-tree.md) §4.3） |
| aarch64 硬件标脏 | 950 上的性能前提（[05](05-dirty-page-tracking-and-hdbss.md)） |

### 1.3 最小化原则

分叉的形态是三处接口扩展，加上 HDBSS 的启用和 seccomp 放行，**没有改动任何既有语义**：

```
CreateSnapshotParams  +  dirty_bitmap_path: Option<PathBuf>      ← 可选字段
PUT /snapshot/rollback                                            ← 新端点
PUT /snapshot/save-dirty-bitmap                                   ← 新端点
```

一个不使用这三处的调用方，看到的行为与上游完全一致。这是有意的：

- 分叉越小，**跟上游合并**越容易；
- 出问题时**越容易判断**是不是我们引入的；
- 上游的既有测试仍然有效。

对应地，划分原则是**语义在 orchestrator、机械动作在 Firecracker**。Firecracker 不知道"树"是什么，它只做
"把这个位图指定的页从这个文件写进 guest 内存"这类无状态的动作；哪些页该回滚、内容从哪一代取，全由 orchestrator 算好再递过去。
这个划分让分叉停在一个容易审阅的规模，也让第 [06](06-memory-diff-tree.md) 章的全部正确性论证只需要在 orchestrator 一侧成立。

参数结构在 `firecracker/src/vmm/src/vmm_config/snapshot.rs`（`CreateSnapshotParams` :39、`RollbackSnapshotParams` :62、
`SaveDirtyBitmapParams` :82、`RollbackTimings` :89、`RollbackVcpuCounters` :126、`RollbackResponse` :146），路由在
`firecracker/src/firecracker/src/api_server/request/snapshot.rs:34-35`，三个扩展端点的请求、响应、错误体写在
`firecracker/src/firecracker/swagger/firecracker.yaml`（:621 起）。

---

## 2. 扩展一：`dirty_bitmap_path`

```rust
pub struct CreateSnapshotParams {
    pub snapshot_type: SnapshotType,
    pub snapshot_path: PathBuf,
    pub mem_file_path: Option<PathBuf>,
    /// Path to a sidecar file receiving the bitmap of pages this snapshot
    /// wrote to the memory file (requires `mem_file_path`).
    pub dirty_bitmap_path: Option<PathBuf>,
}
```

给了 `dirty_bitmap_path` 却没给 `mem_file_path` 会被拒绝（`persist.rs:173-175`，`DirtyBitmapWithoutMemFile`）：
侧车记录的是"内存文件里哪些页是这次写的"，没有内存文件，它就没有可描述的对象。

### 2.1 差分文件不自说明

一个 Diff 快照的内存文件是稀疏的：脏页在各自偏移上，其余是空洞。直觉上，用 `SEEK_DATA` / `SEEK_HOLE` 枚举出已分配的区间，
就知道这次写了哪些页。**但"空洞"和"内容恰好是零的页"在文件系统层面不可区分**：

- 一个被 guest 写成全零的页，Firecracker 照样会写进文件，它会被分配、出现在 data 区间里；
- 反过来，文件系统也可以把全零区间打洞回收；
- Firecracker 写 Diff 文件前会用 `set_len` 把它撑到与完整内存等长（`vm.rs` — `snapshot_memory_to_file`），所以文件长度也不携带信息。

所以**必须有一份显式的记录，说明这次快照写了哪些页**。这就是纪元位图 `E_x`（[06](06-memory-diff-tree.md) §2.2），
由 Firecracker 以侧车文件的形式交出。

### 2.2 为什么必须在同一次调用里

一个直觉的替代方案是：先调快照接口，再调一个"取脏页位图"的接口。**不行** —— 写快照的最后一步就是清位图：
`dump_dirty` 写完之后调 `reset_dirty()`（`vstate/memory.rs:246` 起）。等 orchestrator 再来问，已经没有了。

那就在拍快照**之前**先取？也不行：KVM 的取图是破坏性的（[05](05-dirty-page-tracking-and-hdbss.md) §3.1），
取走之后内核那一半就清零了，`dump_dirty` 再去取就拿不到这些页，它们不会被写进差分文件。

**唯一正确的位置是 `dump_dirty` 内部**：它遍历"KVM 位图 ∪ 用户态位图"、决定哪些页写进文件的同时，顺手把这些页攒成一个 merged 位图返回，
由 `snapshot_memory_to_file`（`vstate/vm.rs:463`）写成侧车：

```rust
let written_bitmap = match snapshot_type {
    SnapshotType::Diff => {
        let dirty_bitmap = self.get_dirty_bitmap()?;                 // 取 KVM 位图（破坏性）
        self.guest_memory().dump_dirty(&mut file, &dirty_bitmap)?    // 写页，返回 merged
    }
    SnapshotType::Full => { /* 写全部内存，清两层位图，返回全 1（§2.3） */ }
};
```

这样侧车描述的恰好是"文件里实际写了的页"，一页不多、一页不少。顺带省掉的：一轮 API 往返，以及"取图"接口本来要做的一次全内存扫描。

`dump_dirty` 还有一个细节值得注意：写文件失败时，它不清位图，而是把刚取出的 KVM 位图**折回**用户态位图（`store_dirty_bitmap`）。
KVM 那一半已经被破坏性读取走了，不折回就永远丢了；折回之后，下一次 Diff 快照或回滚仍然能看到完整集合。
"取出来的脏页信息在任何路径上都不能丢"是贯穿 Firecracker 这一侧的一条纪律，第 [09](09-in-place-rollback.md) 章的阶段 3 与本章 §4 的端点都遵守它。

### 2.3 全量快照也写位图

全量分支返回一个**全 1** 的位图，尾字按 `num_pages` 掩码（`vm.rs:519-530`，单测 `vm.rs:731` `test_full_snapshot_sidecar_is_all_ones`）。
这让下游统一：所有条目都有侧车，形态一致。

orchestrator 侧对全量条目则更进一步：**不读侧车、直接当全 1 用**（`store.go` — `entryBitmap` 对 `MemModeFull` 的分支）。
理由写在该函数的注释里：全量条目总是树根，只在跨树回滚时出现在回滚路径上，那时它的回滚因子必须覆盖"它的时刻与启动内存源之间所有不同的页"，
包括一个丢失的纪元里写过的页 —— 那些页不在任何侧车里，只有全 1 能覆盖（[06](06-memory-diff-tree.md)、[12](12-failure-semantics.md) §6）。
Firecracker 写的全 1 侧车因此不花额外代价，orchestrator 只是不再依赖它。

### 2.4 落地的持久性

内存文件与侧车都只 `flush()`、**不 `sync_all()`**（`vm.rs:534-560`）：

```rust
file.flush()?;                   // 内存文件：交给内核即可，不等磁盘
...
bitmap_file.write_all(&data)?;   // 侧车：同样不 sync
```

理由有三层：

1. 读这两个文件的只有**同一台宿主上**的 orchestrator（以及随后按回滚集读回的同一个 Firecracker），走同一份 page cache，写返回即可见；
2. checkpoint 本来就不承诺活过 orchestrator 进程（[13](13-state-concurrency-durability.md) §5），fsync 买到的"活过宿主崩溃"没有人用得上；
3. 而 fsync 的代价是实打实的：ext4 的日志提交是整个文件系统的串行点，暂停窗口里的每一次 fsync 都要等别的沙箱的回写。

侧车仍然写在它所描述的内存文件之后，但两者都不 sync，这个先后顺序已经不承载持久性含义，只是写代码的自然顺序。

snapfile 分两种情况，由 `persist.rs:232` `snapfile_must_be_durable` 判定：请求带 `mem_file_path`（checkpoint 路径）→ 不 sync；
不带（e2b 原生 pause，snapfile 要进持久化存储、活过本进程）→ 保留上游的 `sync_all()`（`persist.rs:258-262`）。
判据放在 Firecracker 这一侧而不是调用点，是为了让将来的第三个调用方必须经过这段注释。

---

## 3. 扩展二：`PUT /snapshot/rollback`

```rust
pub struct RollbackSnapshotParams {
    pub snapshot_path: PathBuf,               // 要回到哪个时刻的 vmstate
    pub mem_file_path: PathBuf,               // 内存内容从哪读
    pub revert_bitmap_path: Option<PathBuf>,  // 要回写哪些页（累积集合）
    pub resume_vm: bool,
}

pub struct RollbackResponse {
    pub restored_pages: u64,
    pub restored_bytes: u64,
    pub timings_us: RollbackTimings,            // validate / quiesce / bitmap / memory / vcpus / gic / devices / total
    pub vcpu_counters: RollbackVcpuCounters,    // mmio_drained / mmio_drain_failed / readback_mismatch / mmio_drained_pause_total
}
```

端点的主体是九个阶段加一个提交点（第十步"踢设备"在随后的 resume 里），见 [09](09-in-place-rollback.md)；
各计时与计数字段如何进入 orchestrator 的计时键见 [28](28-observability-reference.md)。这里只说接口层面的决定。

### 3.1 `resume_vm: false`

orchestrator 传的永远是 `false`（`internal/sandbox/fc/rollback.go:93-99`）。原因是**磁盘视图还没换**：Firecracker 返回时，
内存已经在目标时刻，磁盘还在当前时刻，这时恢复运行会让 guest 看到一个撕裂的世界 —— 它按目标时刻的文件系统元数据去读当前时刻的块。

恢复由 orchestrator 在换完磁盘视图、清完 conntrack 之后自己做（[11](11-end-to-end.md) §3.2）。参数保留 `true` 这个选项，
是为了让端点能独立使用（例如手工排查时只回滚内存）。

### 3.2 `revert_bitmap_path` 是可选的

参数的注释说明了省略时的语义：

> The live dirty bitmap is always unioned in; this carries the pages dirtied in *earlier* epochs since the target
> snapshot (the orchestrator's cumulative set). Without it only a rollback to the current epoch's base is correct.

也就是说：不给位图时，回滚集 = 仅活跃脏页。这只有在"回滚到当前基准"（上一次 checkpoint 或上一次 restore 的目标）时才正确，
因为只有那时，目标与当前之间的差异恰好就是活跃脏页。跨多代回滚必须给出更早各代的累积集合。orchestrator 永远给。

### 3.3 回滚前的校验

提交点之前（阶段 1–3，`firecracker/src/vmm/src/rollback.rs:271-356`），Firecracker 依次检查：

1. 虚机处于 `Paused`（处于 `Faulted` 时另报 `Faulted`）；
2. snapfile 能加载，拓扑与运行中的虚机一致（[09](09-in-place-rollback.md) §4）；
3. **内存文件长度等于 guest 内存大小**，guest 内存是整数个页；
4. 调用方给的位图几何（页大小、页数）与 guest 一致；
5. 取活跃脏图、与传入位图求并，得出回滚集；
6. **检查内存文件在回滚集的每个偏移上都有数据**（`validate_mem_file_coverage`，:858）。

最后一项值得展开。前几项检查分不出"完整视图"与"被 `set_len` 撑到等长的 Diff 层"（§2.1），也抓不住"orchestrator 漏物化了回滚集中的某一页"。
这两种错误如果放过去，`restore_dirty` 会从文件空洞读到零、写进 guest 内存 —— 而且发生在提交点之后，虚机已经无法挽回。

检查的做法是用 `SEEK_DATA` / `SEEK_HOLE` 遍历文件的 extent：每遇到一个空洞 `[offset, data)`，就看回滚集在这段页号里有没有置位的页，有就拒绝，
报出页号与空洞范围。代价是**每个空洞一对 `lseek`**，而不是每页一次。两个边界：

- 内容恰好全零**且**被文件系统存成空洞的页会被误拒 —— 当前的物化靠写入完成，不产生这种页，误拒只在理论上存在；
  而另一个选择（保持沉默）恰恰是这项检查要阻止的静默损坏；
- 文件系统不支持 `SEEK_DATA`（返回 `EINVAL`）时，检查无法进行而不是失败：跳过并告警一次。

### 3.4 错误分级：状态码加结构化字段

| 失败位置 | HTTP | 响应体 | 虚机状态 | orchestrator 翻译成 |
|---|---|---|---|---|
| 提交点之前（校验、取图、覆盖检查） | 400 | `fault_message`，**没有** `fault` 字段 | `Paused`，可恢复 | 普通错误 → resume 虚机，restore 失败 |
| 提交点之后（内存、vCPU、GIC、设备） | 500 | `fault_message` + `"fault": true` | 标为 `Faulted`，拒绝 resume 与快照，只能替换进程 | `RollbackFaultedError` → 撕裂 |
| 路由不存在（未打补丁的二进制） | 404，或 400 且文案为 `Invalid request method and/or path` | —— | 未动 | `RollbackUnsupportedError` |

Firecracker 侧：`RollbackError::faults_vm()`（`rollback.rs:85-101`）决定哪些变体在提交点之后；`api_server/parsed_request.rs:203-235`
对这些变体回 500，`api_server/mod.rs:205-206` 组出 `{"fault_message": ..., "fault": true}`；单测 `request/snapshot.rs:446`
`test_rollback_error_response` 钉住线上格式。

orchestrator 侧 `classifyRollbackFailure`（`internal/sandbox/fc/rollback.go:185`）**先看 `fault` 字段，不看文案**：

```go
if parsed.Fault != nil {
    // The field is the contract; the status only has to agree with it.
    if *parsed.Fault {
        return RollbackFaultedError{Message: message}
    }
    return fmt.Errorf("rollback failed with status %d: %s", status, message)
}
// 只有响应体里根本没有 fault 字段时，才退回文案匹配
if bytes.Contains(body, []byte("Faulted")) || bytes.Contains(body, []byte("faulted")) {
    return RollbackFaultedError{Message: message}
}
```

`Fault` 是指针，为的是区分"字段缺席"与"字段为 false"。文案匹配这条回退分支覆盖两种情形：不带 `fault` 字段的旧二进制，
以及 400 的 `RollbackError::Faulted`（"虚机已因之前的回滚失败而撕裂"，它发生在本次校验阶段，所以不带 `fault`，但文案里有 `Faulted`）。

为什么要这么较真？**判错的代价在两个方向上都很高**：把撕裂当成可恢复，是恢复运行一个半新半旧的 guest —— 静默损坏；
把可恢复当成撕裂，是白白杀掉一个本来完好的沙箱。而 Firecracker 是另一个仓库，它的错误文案随时可能被改写，不能当契约用。

### 3.5 客户端侧的两个细节

```go
httpClient := &http.Client{
    Transport: &http.Transport{
        DialContext:       /* 连 Firecracker 的 unix socket */,
        DisableKeepAlives: true,
    },
    Timeout: 0,   // 时限由调用方的 context 给（CHECKPOINT_FC_CALL_TIMEOUT）
}
defer httpClient.CloseIdleConnections()
```

| 细节 | 理由 |
|---|---|
| **禁用 keep-alive**（`fc/rollback.go:121`、:241） | Firecracker 的 API server 有并发连接上限。这个 client 每次调用新建，池化连接会一直开到 transport 被回收 —— 回滚够多次就用光上限，此后每次都是 503 |
| **client 自己不设超时**（`Timeout: 0`，:126、:244） | 时限由调用方的 context 统一给（`CHECKPOINT_FC_CALL_TIMEOUT`，见 [27](27-configuration-and-capacity.md)）；transport 里再藏一个固定值，两者不一致时它会悄悄胜出 |

rollback 调用超时按撕裂处理（`internal/sandbox/checkpoint.go:633`，`RollbackInPlace`）：主机放弃等待时，说不清 Firecracker 停在提交点的哪一侧 ——
它可能已经把半个回滚集写进了活的 guest 内存。恢复运行这样一个 guest，会让它把一个说不清的状态写进磁盘。

---

## 4. 扩展三：`PUT /snapshot/save-dirty-bitmap`

```rust
pub struct SaveDirtyBitmapParams { pub path: PathBuf }
```

把当前的活跃脏页位图（自上次快照或回滚以来写脏的页）以 FCDB 格式（§5）写到 `path`，要求虚机暂停。
实现（`rollback.rs:796` `save_dirty_bitmap`）与回滚的阶段 3 是同一套动作：

```rust
let kvm_bitmap = vmm.vm.get_dirty_bitmap()?;                         // 破坏性读
vmm.vm.guest_memory().store_dirty_bitmap(&kvm_bitmap, page_size);    // 先折回用户态位图
let words = userspace_bitmap_flat(vmm.vm.guest_memory(), page_size, total_pages);
std::fs::write(path, serialize_dirty_bitmap(&words, page_size, total_pages))?;
```

**它不清位图**：KVM 那一半被取走后立刻折回用户态位图，展平时读的是用户态位图的副本（`userspace_bitmap_flat` 先 `clone` 再取字），
所以之后的 Diff 快照或回滚仍然看到完整集合。

### 4.1 为什么非有不可

回顾第 [06](06-memory-diff-tree.md) 章的契约：**Firecracker 回滚时会把自己的活跃脏页并进写回集**（§3.3 第 5 步），
然后按页从 `mem_file` 读内容。而 orchestrator 递过去的 `mem_file` 是一个**只含回滚集的稀疏文件**。

如果某一页 Firecracker 要写、orchestrator 却没有物化它：

- 没有 §3.3 的覆盖检查时，`restore_dirty` 会从**文件空洞读到零**，把 guest 的那一页清零 —— 不报错、不留痕迹，guest 继续运行，某一页的内容变成了零；
- 有了覆盖检查，这变成提交点之前的一次明确拒绝，虚机原样恢复 —— 但 restore 仍然失败了。

要让 restore 成功，orchestrator 必须**事先知道** Firecracker 会并入哪些页 —— 也就是活跃脏页集，把它们也物化出来。
这就是这个端点存在的理由：它让稀疏物化文件这个设计成立。

orchestrator 还在两处用它（都服务于"自启动以来脏页集"，这个集合是 checkpoint 之后再做原生 pause 的正确性前提，
见 [16 §4](16-native-increment-fix.md#4-与-checkpoint-叠加累积位图)、[18 §7](18-native-and-checkpoint-together.md#7-checkpoint-之后做原生-pause-的正确性前提)）：

- **全量 checkpoint 拍之前**导出一次活跃脏图（`internal/sandbox/checkpoint.go` — `createEpoch.beforeSnapshot`）。
  全量快照的侧车是全 1，对"自启动以来写过哪些页"毫无信息量；而沙箱的**第一个** checkpoint 总是全量，所以必须在拍之前另要一份。
  代价是一次位图导出，每 GiB guest 内存 32 KiB；
- **原生 pause** 取写跟踪位图；运行中的 Firecracker 没有这个端点时，pause 退回驻留判据。

### 4.2 暂停前提

端点开头就检查 `VmState::Paused`。这不只是防御：虚机不暂停，活跃脏页集会在导出之后继续增长，物化出来的内容就覆盖不全
Firecracker 接下来要写的页 —— 又回到 §4.1 那个问题。
编排侧对应地保证次序：先 Pause，再 SaveDirtyBitmap，再物化，再 RollbackSnapshot（[11](11-end-to-end.md) §3.3）。

---

## 5. FCDB 位图格式

两个进程之间唯一的二进制格式（`firecracker/src/vmm/src/vstate/memory.rs:533-596`；Go 侧 `internal/checkpoint/bitmap.go`）。

### 5.1 逐字节

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 4 | magic `"FCDB"` |
| 4 | 4 | version，u32 小端，当前 `1` |
| 8 | 8 | `page_size`，u64 小端 |
| 16 | 8 | `num_pages`，u64 小端 |
| 24 | 8 × ⌈num_pages/64⌉ | 位图字，u64 小端；第 `w` 个字的第 `i` 位对应页 `w*64 + i` |

一个例子：2 GiB guest 内存、4 KiB 页，`num_pages` = 524288，位图 8192 个字，文件长 24 + 65536 字节。

两侧读时都要求文件长度**恰好**等于 24 + 8 × 字数，版本不符、magic 不符一律拒绝（`deserialize_dirty_bitmap`，`memory.rs:563`；
`readDirtyBitmap`，`bitmap.go:29`；测试 `bitmap_test.go:17` `TestBitmapWireFormat`、:40 `TestBitmapRejectsMalformed`）。

### 5.2 页索引是什么空间

**是内存快照文件里的偏移除以页大小，不是 guest 物理地址。**

> Page indices are file offsets in the memory snapshot divided by page size (guest regions tiled in order) — the same
> space `dump_dirty` writes in, so the orchestrator can union bitmaps and pread pages without knowing the guest's
> physical memory layout.

guest 内存可能由**多个 region** 组成（架构上有保留洞时）。快照文件把这些 region **按顺序首尾相接**地平铺，中间不留洞。所以：

- 文件偏移空间是**连续**的，而 guest 物理地址空间可能不是；
- orchestrator 因此**完全不需要知道** guest 的内存布局 —— 它只是在一个平坦的、`num_pages` 长的位空间上做并集，
  在一个平坦的文件上做 `pread` / `pwrite`；
- Firecracker 这一侧负责两个空间之间的换算：`dump_dirty` 写页、`userspace_bitmap_flat` 展平位图、`restore_dirty` 写回，
  都按"region 起始页号 + region 内页号"逐个 region 累加。

这是一个很干净的抽象边界：**内存布局的知识全部留在 Firecracker 里**。代价是位图与某个具体的内存布局绑定，
换了 region 划分，老位图就不能用了 —— 几何校验会挡住它（下一节）。

### 5.3 几何校验

每次读位图都要检查 `page_size` 与 `num_pages` 是否与当前虚机一致。三处都做：

| 位置 | 检查 |
|---|---|
| `entryBitmap`（orchestrator，`store.go:1439`） | 与当前 guest 的页数、页大小对比，不符则报错 |
| `merge`（orchestrator，`bitmap.go:62`） | 两个位图必须同几何才能求并 |
| 回滚端点（Firecracker，`rollback.rs:307-323`） | 位图几何 + 内存文件长度，都要对上运行中的虚机 |

不符时**报错，不猜测**：几何不符的位图属于另一个虚机或另一次运行，继续用下去只会产生一个随机的回滚集。

### 5.4 尾字必须掩码

`num_pages` 通常不是 64 的倍数，最后一个字里有些位不对应任何页。**它们必须为 0。** 两侧的全 1 位图都做了这个处理：

```rust
// Firecracker：Full 快照的侧车（vm.rs:524-529）
let mut all = vec![u64::MAX; total_pages.div_ceil(64)];
if total_pages % 64 != 0 {
    if let Some(last) = all.last_mut() { *last = (1u64 << (total_pages % 64)) - 1; }
}
```

```go
// orchestrator：allOnesBitmap（bitmap.go:110）
words := make([]uint64, (numPages+63)/64)
for i := range words { words[i] = ^uint64(0) }
if r := numPages % 64; r != 0 {
    words[len(words)-1] = (uint64(1) << r) - 1
}
```

不掩码会怎样？`contains(page)` 永远不会查询越界的位（页号不会越界），所以单看一个位图不会出错。问题出在**比较和并集**上：
一个来自 Firecracker 的全 1 位图和一个 orchestrator 构造的全 1 位图，如果尾字处理不同，它们就不是同一个位图；
求并之后再做字级比较或计数，结果会不一致。这类不一致极难排查，所以两侧的实现刻意写成镜像
（Go 侧注释："Bits past numPages stay clear so unions with real sidecars agree"）。按字处理的代码（如回滚集解析）另外在读时屏蔽尾部多余的位，作为第二道保险。

### 5.5 两侧实现的对称性

| | Firecracker（Rust） | orchestrator（Go） |
|---|---|---|
| 写 | `serialize_dirty_bitmap` | `dirtyBitmap.writeTo` |
| 读 | `deserialize_dirty_bitmap` | `readDirtyBitmap` |
| 全 1 | `SnapshotType::Full` 分支 | `allOnesBitmap` |

四个函数、两种语言，描述同一个格式。**改任何一个都必须同时改另一个** —— 这是分叉带来的一处固有维护成本，
靠格式版本号和上面的单元测试守着。

---

## 6. 版本配对

**orchestrator 与 Firecracker 必须成对交付。** 两者出自同一个代码仓库的同一个版本（`packages/` 与 `firecracker/`，代码基准见 [README](README.md)）；
按 rpm 部署时分别对应 `0001-adapted-for-arm-architecture.patch` 与 `firecracker.arm`（部署前核对见 [26](26-deployment-prerequisites.md)）。

配错的后果：

| 配法 | 结果 |
|---|---|
| 新 orchestrator + 未打补丁的 Firecracker | restore 时 `/snapshot/rollback` 路由不存在 → `RollbackUnsupportedError`，**明确失败** |
| 新 orchestrator + 没有 `save-dirty-bitmap` 的分叉 Firecracker | restore 失败（导出活跃脏图失败，虚机 resume，沙箱原样继续）；全量 checkpoint 前导不出活跃脏图，"自启动以来脏页集"改用全 1 侧车（多记不少记） |
| 旧 orchestrator + 新 Firecracker | 功能上兼容（扩展是加法），但没有 checkpoint 能力 |

"路由不存在"在两种前端下长得不一样：未打补丁的 Firecracker 对不认识的路径回 400 `Invalid request method and/or path`，
socket 前面若是别的东西则回 404。两个客户端（`rollbackSnapshot`、`saveDirtyBitmap`）都把这两种应答识别为"缺路由"，
而不是一次普通的失败（`fc/rollback.go:194-200`、:270-275）。

第一行是**好的失败**：立刻、明确、指名道姓。这是刻意的设计 —— 分叉的端点用新路径，而不是给既有端点加参数，
正是为了让"不支持"表现为路由不存在，而不是一个被旧二进制悄悄忽略的字段。

---

## 7. seccomp

Firecracker 给每个线程装独立的 seccomp 过滤器（`firecracker/resources/seccomp/aarch64-unknown-linux-musl.json`，分 `vmm` / `api` / `vcpu` 三组）。
回滚路径引入的系统调用必须逐一放行：

| 线程 | 需要什么 | 为什么 |
|---|---|---|
| vCPU 线程 | `KVM_SET_ONE_REG`、mp state、`KVM_ARM_VCPU_INIT` 及其后的 SVE finalize 等 vCPU ioctl（:1043-1082） | vCPU 状态恢复派发到这些线程执行（[09](09-in-place-rollback.md) §3.6） |
| VMM 线程 | `TUNSETOFFLOAD`（:448） | 回滚后按回退后的协商特性重设 tap 的 offload 标志，这一步在 VMM 线程上执行 |
| VMM 线程 | `pread64`（:29） | "取脏页位图"路径逐页读 `/proc/self/pagemap`（[05](05-dirty-page-tracking-and-hdbss.md)） |

漏了的表现是进程在**运行期**被 SIGSYS 杀掉 —— 明显，但不在编译期暴露。`TUNSETOFFLOAD` 那一条的白名单注释记下了漏掉时的样子：
带网卡的虚机第一次回滚就以 `BadSyscall(148)` 被杀。

反过来也有约束：**运行期能做的事比初始化期少**。例如回滚时不能重建需要 `memfd_create` 的对象，只能原地清空（[10](10-rollback-pitfalls.md) §2.5）。
改回滚路径时，新增任何系统调用都要同步检查两份白名单。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 三个参数结构与应答 | `firecracker/src/vmm/src/vmm_config/snapshot.rs` |
| 路由 | `firecracker/src/firecracker/src/api_server/request/snapshot.rs` — `parse_put_snapshot_rollback`、`parse_put_snapshot_save_dirty_bitmap` |
| 400 / 500 与 `fault` 字段 | `api_server/parsed_request.rs`；`api_server/mod.rs` — `json_vm_fault_message` |
| 快照写出与侧车 | `firecracker/src/vmm/src/vstate/vm.rs` — `snapshot_memory_to_file`；`vstate/memory.rs` — `dump_dirty` |
| snapfile 是否 fsync | `firecracker/src/vmm/src/persist.rs` — `snapfile_must_be_durable` |
| FCDB（Rust） | `vstate/memory.rs` — `serialize_dirty_bitmap`、`deserialize_dirty_bitmap` |
| 导出活跃脏图、覆盖检查 | `firecracker/src/vmm/src/rollback.rs` — `save_dirty_bitmap`、`validate_mem_file_coverage` |
| FCDB（Go） | `packages/orchestrator/internal/checkpoint/bitmap.go` — `readDirtyBitmap`、`writeTo`、`merge`、`allOnesBitmap` |
| 两个端点的客户端与错误分类 | `internal/sandbox/fc/rollback.go` — `rollbackSnapshot`、`saveDirtyBitmap`、`classifyRollbackFailure` |
| 线格式测试 | `internal/checkpoint/bitmap_test.go` — `TestBitmapWireFormat`、`TestBitmapRejectsMalformed` |
| OpenAPI 描述 | `firecracker/src/firecracker/swagger/firecracker.yaml` |
| seccomp 白名单 | `firecracker/resources/seccomp/aarch64-unknown-linux-musl.json` |

---

## 本章要点

1. 分叉是**加法**：一个可选字段、两个新端点、HDBSS 启用、seccomp 放行，既有语义一行没改；划分原则是**语义在 orchestrator、机械动作在 Firecracker**。
2. 差分文件不自说明（空洞与全零页不可区分），所以需要侧车；侧车必须在拍快照的**同一次调用**里由 `dump_dirty` 攒出 —— 写快照会清位图，取图又是破坏性的，没有第二个正确的时机。
3. 全量快照也写全 1 侧车，但 orchestrator 对全量条目不读侧车、直接当全 1，保证跨树回滚不依赖侧车内容。
4. checkpoint 路径上内存文件、侧车、snapfile 都只 `flush()` 不 fsync；只有原生 pause 的 snapfile 保留 `sync_all()`。
5. rollback 的错误按**状态码 + `fault` 字段**分级：提交点前 400（可恢复），提交点后 500 + `"fault": true`（撕裂）；文案匹配只作兜底；调用超时按撕裂处理。
6. Firecracker 在提交点前检查内存文件覆盖回滚集的每一页，把"漏物化"从静默清零变成明确失败；`save-dirty-bitmap` 让 orchestrator 事先知道活跃脏页集，是稀疏物化文件能让 restore 成功的前提，它不清位图。
7. FCDB 的页索引是**内存文件偏移空间**，内存布局的知识全部留在 Firecracker；几何校验三处都做，尾字必须掩码，两侧实现写成镜像。
8. 两侧必须成对交付；配错时表现为"路由不存在"→ 一个指名道姓的错误，这是刻意的设计。
