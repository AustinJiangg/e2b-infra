# 09 · 分叉 Firecracker 的接口契约

> orchestrator 与 Firecracker 是两个进程。它们之间的全部约定是：**一个可选字段、两个新端点、
> 一种二进制格式**。本篇讲每一处为什么存在、契约的边界在哪、以及配错版本会怎样。
>
> **读者**：工程师。要改 Firecracker 那一侧的人必读。
> **预备**：[第 7 篇 · 脏页跟踪](07-dirty-page-tracking.md)、
> [第 8 篇 · 内存差分树](08-memory-diff-tree.md)。
> **代码**：`src/vmm/src/vmm_config/snapshot.rs`、`src/firecracker/src/api_server/request/snapshot.rs`、
> `src/vmm/src/vstate/memory.rs`、`internal/checkpoint/bitmap.go`、`internal/sandbox/fc/rollback.go`

---

## 0. 本篇要回答的问题

1. 上游 Firecracker 已经有 Diff 快照了，为什么还要分叉？
2. 三处扩展分别解决什么问题？哪一处是**非有不可**的？
3. FCDB 位图里的「页索引」是 guest 物理地址还是别的什么？
4. 把打过补丁的 orchestrator 配上未打补丁的 Firecracker，会发生什么？

---

## 1. 为什么要分叉

上游 Firecracker **已经有**的：

| 能力 | 说明 |
|---|---|
| Diff 快照 | `SnapshotType::Diff`，只写脏页，写在各自的文件偏移上 |
| 脏页跟踪 | `track_dirty_pages`，走 KVM 写保护 |
| 全量恢复 | `PUT /snapshot/load`，新建进程加载 |
| Pause / Resume | `PATCH /vm` |

上游**没有**的：

| 缺什么 | 为什么需要 |
|---|---|
| 把「这次快照写了哪些页」告诉调用方 | 差分文件本身不说明它含哪些页；orchestrator 要用它算回滚集（[第 8 篇](08-memory-diff-tree.md)） |
| 往活着的虚机写回状态 | 上游只有「新建进程加载」，成本与虚机规格挂钩（[第 11 篇](11-in-place-rollback.md)） |
| 导出「当前还没进快照的脏页」 | 回滚集必须包含它（[第 8 篇 §4.4](08-memory-diff-tree.md#44-为什么必须并入活跃脏页)） |
| aarch64 硬件标脏 | 950 上的性能前提（[第 7 篇](07-dirty-page-tracking.md)） |

### 1.1 最小化原则

分叉的形态是三处扩展加 HDBSS，**没有改动任何既有语义**：

```
CreateSnapshotParams  +  dirty_bitmap_path: Option<PathBuf>      ← 可选字段
PUT /snapshot/rollback                                            ← 新端点
PUT /snapshot/save-dirty-bitmap                                   ← 新端点
```

一个不使用这三处的调用方，看到的行为与上游完全一致。这是有意的：

- 分叉越小，**跟上游合并**越容易；
- 出问题时**越容易判断**是不是我们引入的；
- 上游的既有测试仍然有效。

对应地，**语义放在 orchestrator，机械动作放在 Firecracker**：
Firecracker 不知道「树」是什么，它只做「把这个位图指定的页从这个文件写进 guest 内存」
这类无状态的动作。这个划分让分叉停在几百行的量级。

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

### 2.1 差分文件不自说明

一个 Diff 快照的内存文件是稀疏的：脏页在各自偏移上，其余是空洞。
**但「空洞」和「内容恰好是零的页」在文件系统层面不可区分** ——
`SEEK_DATA` / `SEEK_HOLE` 能枚举出已分配的区间，但一个被 guest 写成全零的页
也会被分配，也会出现在「data」里；反过来，文件系统也可以把全零区间打洞回收。

所以：**必须有一份显式的记录，说明这次快照写了哪些页。** 这就是纪元位图 `E_x`
（[第 8 篇 §2.2](08-memory-diff-tree.md#22-纪元epoch与它的位图)）。

### 2.2 为什么必须在同一次调用里

一个直觉的替代方案是：先调 `create`，再调一个「取脏页位图」的接口。**不行** ——
写快照的最后一步就是 `reset_dirty()`，把位图清了。等 orchestrator 再来问，已经没有了。

那就在 `create` 之前先取？也不行：取图是破坏性的
（[第 7 篇 §3.1](07-dirty-page-tracking.md#31-破坏性读)），
取完之后 `dump_dirty` 就拿不到 KVM 那一半了。

**唯一正确的位置是 `dump_dirty` 内部** —— 它遍历「KVM 位图 ∪ 用户态位图」的过程中
顺手把 merged 位图攒出来，返回给调用者写文件：

```rust
let written_bitmap = match snapshot_type {
    SnapshotType::Diff => {
        let dirty_bitmap = self.get_dirty_bitmap()?;
        self.guest_memory().dump_dirty(&mut file, &dirty_bitmap)?   // ← 返回 merged
    }
    SnapshotType::Full => { … 全 1 … }
};
```

顺带省掉的：一轮 API 往返，以及那个「取图」接口本来要做的一次全内存扫描。

### 2.3 全量快照也写位图

全量分支返回一个**全 1** 的位图（尾字按 `num_pages` 掩码）。

这让下游统一：所有条目都有侧车，`entryBitmap` 不需要为全量条目做特殊处理。
（内容解析那边仍有一处区分，见[第 8 篇 §6.1](08-memory-diff-tree.md#61-规则)。）

### 2.4 落地的持久性

位图侧车写在它所描述的内存文件**之后**，两者都只 `flush()`、**不 `sync_all()`**：

```rust
file.flush()?;                     // 内存文件：交给内核即可，不等磁盘
...
bitmap_file.write_all(&data)?;     // 侧车：同样不 sync
```

读这两个文件的只有同一台宿主上的 orchestrator，走的是同一份 page cache，写返回即可见；
而 checkpoint 本来就不承诺活过 orchestrator 进程
（[第 15 篇 §5](15-state-and-concurrency.md#5-原子提交与持久性)），`fsync` 买不到东西，
却要在暂停窗口里付 ext4 日志提交的串行等待。

snapfile 分两种情况，由 `persist.rs` 的 `snapfile_must_be_durable` 判定：
请求带 `mem_file_path`（checkpoint 路径）→ 不 sync；不带（e2b 原生 pause，snapfile 要进持久化存储）→
保留上游的 `sync_all()`。

---

## 3. 扩展二：`PUT /snapshot/rollback`

```rust
pub struct RollbackSnapshotParams {
    pub snapshot_path: PathBuf,          // 要回到哪个时刻的 vmstate
    pub mem_file_path: PathBuf,          // 内存内容从哪读
    pub revert_bitmap_path: Option<PathBuf>,  // 要回写哪些页（累积集合）
    pub resume_vm: bool,
}

pub struct RollbackResponse {
    pub restored_pages: u64,
    pub restored_bytes: u64,
    pub timings_us: RollbackTimings,     // 七个阶段各自的微秒数
}
```

主体在[第 11 篇](11-in-place-rollback.md)。这里只说接口层面的三个决定。

### 3.1 `resume_vm: false`

orchestrator 传的永远是 `false`。因为**磁盘视图还没换** ——
内存已经在目标时刻，磁盘还是当前时刻，这时候恢复运行会让 guest 看到一个撕裂的世界。

恢复由 orchestrator 在换完磁盘视图、清完 conntrack 之后自己做。
参数保留 `true` 这个选项是为了让端点能独立使用（比如手工排查）。

### 3.2 `revert_bitmap_path` 是可选的

注释说明了它省略时的语义：

> The live dirty bitmap is always unioned in; this carries the pages dirtied in
> *earlier* epochs since the target snapshot (the orchestrator's cumulative set).
> Without it only a rollback to the current epoch's base is correct.

也就是说：不给位图时，回滚集 = 仅活跃脏页，这**只有在「回滚到当前基准」时才正确**。
跨多代回滚必须给。orchestrator 永远给。

### 3.3 客户端侧的三个细节

```go
httpClient := &http.Client{
    Transport: &http.Transport{
        DialContext: /* unix socket */,
        DisableKeepAlives: true,
    },
    Timeout: 0,   // 时限由调用方的 context 给（CHECKPOINT_FC_CALL_TIMEOUT）
}
defer httpClient.CloseIdleConnections()
```

| 细节 | 理由 |
|---|---|
| **禁用 keep-alive** | Firecracker 的 API server 有并发连接上限。这个 client 是每次调用新建的，池化连接会一直开到 transport 被 GC —— 回滚够多次就会用光上限，此后每次都 503 |
| **client 自己不设超时** | 时限由调用方的 context 统一给：`CHECKPOINT_FC_CALL_TIMEOUT`，默认 2 分钟（[第 15 篇 §9](15-state-and-concurrency.md#9-限额与超时四个服务端开关)）。transport 里再藏一个固定值，两者不一致时它会悄悄胜出。rollback 调用超时按撕裂处理 |
| **404 → `RollbackUnsupportedError`** | 未打补丁的 Firecracker 把未知的 `/snapshot/*` 路由成 404。翻译成一个明确的错误，而不是让上层看到一个含糊的 HTTP 状态码 |

「提交点之后失败」按**状态码加一个结构化字段**识别，不看错误文案：
Firecracker 对提交点之后的失败应答 HTTP 500 且响应体带 `"fault": true`（虚机已标记 `Faulted`、拒绝 resume、只能替换），
对提交点之前的失败应答 400（虚机可恢复）。

```go
if parsed.Fault != nil {
    if *parsed.Fault {
        return RollbackFaultedError{Message: message}
    }
    return fmt.Errorf("rollback failed with status %d: %s", status, message)
}
```

判错的代价在两个方向上都很高 —— 把撕裂当成可恢复是静默损坏，把可恢复当成撕裂是白杀一个沙箱 ——
所以不能依赖另一个组件随时可能改写的文案。对响应体里 `Faulted` 字样的字符串匹配仍在，
但只作为对接**没有 `fault` 字段的旧 Firecracker 二进制**时的回退（`fc/rollback.go` — `classifyRollbackFailure`）。
三个扩展端点的请求、响应与错误体已写进 `firecracker/src/firecracker/swagger/firecracker.yaml`。

---

## 4. 扩展三：`PUT /snapshot/save-dirty-bitmap`

```rust
pub struct SaveDirtyBitmapParams { pub path: PathBuf }
```

把当前的活跃脏页位图（自上次快照或回滚以来写脏的页）以 FCDB 格式写到 `path`。要求虚机暂停。

实现与回滚的阶段 3 是同一套动作：

```rust
let kvm_bitmap = vmm.vm.get_dirty_bitmap()?;
vmm.vm.guest_memory().store_dirty_bitmap(&kvm_bitmap, page_size);   // 先折回，再做别的
let words = userspace_bitmap_flat(vmm.vm.guest_memory(), page_size, total_pages);
std::fs::write(path, serialize_dirty_bitmap(&words, page_size, total_pages))?;
```

### 4.1 为什么非有不可

这是三处扩展里唯一一个**「不加就会静默损坏数据」**的。

回顾契约（[第 8 篇 §5.1](08-memory-diff-tree.md#51-一条必须遵守的契约)）：
**Firecracker 在回滚时会把自己的活跃脏页并进写回集**，然后按页从 `mem_file` 读内容。

ext4 方案递过去的 `mem_file` 是一个**只含回滚集的稀疏文件**。如果某一页 Firecracker 要写、
而 orchestrator 没有物化它，`restore_dirty` 会从**文件空洞读到零**，把 guest 的那一页清零。

- 不报错；
- 不留痕迹；
- guest 继续运行，某一页的内容变成了零。

要让契约成立，orchestrator 必须**事先知道** Firecracker 会并入哪些页 —— 也就是活跃脏页集。
这就是这个端点存在的唯一理由。

### 4.2 为什么 XFS 方案不需要它

XFS 方案递过去的 `mem_file` 是那一代的**完整镜像**（克隆上一代 + Diff 覆盖）。
任何页都读得到，Firecracker 多写几页只是多做无用功，不会读到空洞。

所以 XFS 方案的 Firecracker **没有这个端点**。这是两套方案在接口层面唯一的差异。
见[第 18 篇](18-ext4-vs-xfs.md)。

### 4.3 暂停前提

和回滚一样，端点头几行就检查 `VmState::Paused`。

这不只是防御性检查 —— 它是**正确性前提**：虚机不暂停，活跃脏页集会在导出之后继续增长，
orchestrator 物化出来的内容就覆盖不全 Firecracker 接下来要写的页。
回到 §4.1 那个静默损坏。

编排侧对应地保证了这个次序：先 `Pause`，再 `SaveDirtyBitmap`，再物化，再 `RollbackSnapshot`
（[第 13 篇](13-end-to-end.md)）。

---

## 5. FCDB 位图格式

两个进程之间唯一的二进制格式。

### 5.1 逐字节

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 4 | magic `"FCDB"` |
| 4 | 4 | version，u32 小端，当前 `1` |
| 8 | 8 | `page_size`，u64 小端 |
| 16 | 8 | `num_pages`，u64 小端 |
| 24 | 8 × ⌈num_pages/64⌉ | 位图字，u64 小端 |

第 `w` 个字的第 `i` 位对应页索引 `w*64 + i`。

### 5.2 页索引是什么空间

**是内存快照文件里的偏移除以页大小，不是 guest 物理地址。**

Firecracker 侧的注释说得很明确：

> Page indices are file offsets in the memory snapshot divided by page size
> (guest regions tiled in order) — the same space `dump_dirty` writes in, so
> the orchestrator can union bitmaps and pread pages without knowing the
> guest's physical memory layout.

guest 内存可能由**多个 region** 组成（架构上有保留洞时）。快照文件把这些 region
**按顺序首尾相接**地平铺，中间不留洞。所以：

- 文件偏移空间是**连续**的，而 guest 物理地址空间可能不是；
- orchestrator 因此**完全不需要知道** guest 的内存布局 ——
  它只是在一个平坦的、`num_pages` 长的位空间上做并集，
  以及在一个平坦的文件上做 `pread` / `pwrite`。

这是一个很干净的抽象边界：**内存布局的知识全部留在 Firecracker 里。**

> 代价是位图与某个具体的内存布局绑定。换了 region 划分（比如加了内存热插拔），
> 老的位图就不能用了 —— 几何校验会挡住它（下一节）。

### 5.3 几何校验

每次读位图都要检查 `page_size` 与 `num_pages` 是否与当前虚机一致。三处都做：

| 位置 | 检查 |
|---|---|
| `entryBitmap`（orchestrator） | 与当前 guest 的页数、页大小对比，不符则条目不可用 |
| `merge`（orchestrator） | 两个位图必须同几何才能求并 |
| 回滚端点（Firecracker） | 位图几何 + 内存文件长度，都要对上运行中的虚机 |

不符时**报错，不猜测**。一个几何不符的位图意味着它属于另一个虚机或另一次运行，
继续用下去只会产生一个随机的回滚集。

### 5.4 尾字必须掩码

`num_pages` 通常不是 64 的倍数，最后一个字里有些位不对应任何页。**它们必须为 0。**

两侧的全 1 位图都做了这个处理：

```rust
// Firecracker: Full 快照
let mut all = vec![u64::MAX; total_pages.div_ceil(64)];
if total_pages % 64 != 0 {
    if let Some(last) = all.last_mut() { *last = (1u64 << (total_pages % 64)) - 1; }
}
```

```go
// orchestrator: allOnesBitmap
words := make([]uint64, (numPages+63)/64)
for i := range words { words[i] = ^uint64(0) }
if r := numPages % 64; r != 0 {
    words[len(words)-1] = (uint64(1) << r) - 1
}
```

不掩码的后果是：并集里会出现越界的位，`contains(page)` 对这些位不会被查询（页号不会越界），
但**两个来源不同的位图求并之后再比较，结果会不一致** —— 一个来自 Firecracker 的全 1 位图
和一个 orchestrator 构造的全 1 位图，如果尾字处理不同，就不是同一个位图。
这类不一致极难排查，所以两侧的实现刻意写成镜像。

### 5.5 两侧实现的对称性

| | Firecracker（Rust） | orchestrator（Go） |
|---|---|---|
| 写 | `serialize_dirty_bitmap` | `dirtyBitmap.writeTo` |
| 读 | `deserialize_dirty_bitmap` | `readDirtyBitmap` |
| 全 1 | `SnapshotType::Full` 分支 | `allOnesBitmap` |

四个函数、两种语言，描述同一个格式。**改任何一个都必须同时改另一个** ——
这是分叉带来的一处固有维护成本，目前靠格式版本号 + 单元测试
（`bitmap_test.go` 的 `TestBitmapWireFormat`）守着。

---

## 6. 版本配对

**orchestrator 与 Firecracker 必须成对交付。** 交付形态是 `e2b-infra` 仓库里的
`0001-adapted-for-arm-architecture.patch`（orchestrator 侧）与 `firecracker.arm`（二进制）。

配错的后果：

| 配法 | 结果 |
|---|---|
| 新 orchestrator + 旧 Firecracker | restore 时 `/snapshot/rollback` 返回 404 → `RollbackUnsupportedError`，**明确失败** |
| 新 orchestrator + XFS 方案的 Firecracker | `save-dirty-bitmap` 返回 404 → restore 失败 |
| 旧 orchestrator + 新 Firecracker | 功能上兼容（扩展是加法），但没有 checkpoint 能力 |
| **ext4 方案的 orchestrator + XFS 方案的 orchestrator 混用** | 账本语义完全不同，见[第 18 篇](18-ext4-vs-xfs.md) |

第一、二行是**好的失败**：立刻、明确、指名道姓。这是刻意设计的 —— 分叉的端点用新路径
而不是给既有端点加参数，正是为了让「不支持」表现为 404 而不是一个被忽略的字段。

---

## 7. seccomp

Firecracker 给每个线程装独立的 seccomp 过滤器。回滚路径引入的新系统调用必须逐一放行：

| 线程 | 需要什么 | 为什么 |
|---|---|---|
| vCPU 线程 | 回滚用到的 vCPU ioctl（`KVM_ARM_VCPU_INIT`、`KVM_SET_ONE_REG`…） | vCPU 状态恢复派发到这些线程执行（[第 11 篇 §3.6](11-in-place-rollback.md#36-阶段-5vcpu)） |
| VMM 线程 | `pread64` | 「取脏页位图」路径要逐页读 `/proc/self/pagemap`（[第 7 篇 §5.2](07-dirty-page-tracking.md#52-判据因此塌缩)） |

反过来也有约束：**运行期能做的事比初始化期少**。
[第 12 篇 §2.6](12-rollback-pitfalls.md#26-一个连带的约束seccomp) 里那个
「不能重建 `RxBuffers`，因为要 `memfd_create`」就是这条约束的直接后果。

> **改回滚路径的检查项**：新增任何系统调用，同步检查两份白名单。
> 漏了的表现是进程被 SIGSYS 杀掉 —— 明显，但发生在运行期而不是编译期。

---

## 8. 小结

1. 分叉是**加法**：一个可选字段、两个新端点、HDBSS 启用、seccomp 放行。既有语义一行没改。
2. 划分原则是**语义在 orchestrator、机械动作在 Firecracker**。Firecracker 不知道树是什么，
   这让分叉停在几百行。
3. `dirty_bitmap_path` 必须在 `create` 的**同一次调用**里 ——
   写快照的最后一步会清位图，取图又是破坏性的，没有第二个正确的时机。
4. `save-dirty-bitmap` 是唯一**不加就会静默损坏数据**的扩展：
   ext4 方案递过去的是稀疏文件，Firecracker 多写一页就会从空洞读到零。
   XFS 方案递的是全量镜像，所以不需要它。
5. FCDB 的页索引是**内存文件偏移空间**，不是 guest 物理地址。
   这让 orchestrator 完全不需要知道 guest 的内存布局。
6. 几何校验三处都做，不符**报错不猜测**；尾字必须掩码，两侧实现刻意写成镜像。
7. 版本必须成对。配错时表现为 404 → 一个指名道姓的错误，这是刻意的设计。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 三个参数结构 | `src/vmm/src/vmm_config/snapshot.rs` |
| 路由 | `src/firecracker/src/api_server/request/snapshot.rs` — `parse_put_snapshot` |
| 动作分发 | `src/vmm/src/rpc_interface.rs` — `VmmAction::{RollbackSnapshot, SaveDirtyBitmap}` |
| 快照写出与侧车 | `src/vmm/src/vstate/vm.rs` — `snapshot_memory_to_file` |
| FCDB（Rust） | `src/vmm/src/vstate/memory.rs` — `serialize_dirty_bitmap`、`deserialize_dirty_bitmap` |
| 导出活跃脏图 | `src/vmm/src/rollback.rs` — `save_dirty_bitmap` |
| FCDB（Go） | `internal/checkpoint/bitmap.go` — `readDirtyBitmap`、`writeTo`、`allOnesBitmap` |
| 两个端点的客户端 | `internal/sandbox/fc/rollback.go` — `rollbackSnapshot`、`saveDirtyBitmap` |
| 线格式测试 | `internal/checkpoint/bitmap_test.go` — `TestBitmapWireFormat`、`TestBitmapRejectsMalformed` |
| OpenAPI 描述 | `src/firecracker/swagger/firecracker.yaml` |

**下一篇**：[10 · 磁盘分层与零拷贝封存](10-disk-layering.md) —— 内存那一半讲完了，
接下来是磁盘那一半：怎么做到一个字节都不搬就换掉整个磁盘视图。
