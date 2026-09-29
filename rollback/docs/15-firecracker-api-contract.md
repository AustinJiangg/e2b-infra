# 15 · Firecracker 接口契约

> 这篇给要改 Firecracker 那一侧、或要排查两个进程之间问题的开发者看。orchestrator 与 Firecracker 之间的全部约定是
> **一个可选字段、两个新端点、一种二进制格式**。读完你能说清每一处为什么存在、契约的边界在哪、错误怎么分级，
> 以及两侧版本配错会发生什么。

---

## 1. 为什么要分叉

上游 Firecracker 已有 Diff 快照（`SnapshotType::Diff`）、`track_dirty_pages` 脏页跟踪、`PUT /snapshot/load`、`PATCH /vm` 暂停与恢复。
上游没有的：

| 缺什么 | 为什么需要 |
|---|---|
| 告诉调用方"这次快照写了哪些页" | 差分文件本身不说明它含哪些页，orchestrator 要用它算回滚集（[14](14-memory-diff-tree.md)） |
| 往活着的虚机写回状态 | 上游只有新建进程加载，成本与虚机规格挂钩（[17](17-in-place-rollback.md)） |
| 导出"当前还没进快照的脏页" | 回滚集必须包含它（[14](14-memory-diff-tree.md)） |
| aarch64 硬件标脏 | 950 上的性能前提（[13](13-dirty-page-tracking-and-hdbss.md)） |

分叉的形态是三处扩展加 HDBSS，**没有改动任何既有语义**：

```
CreateSnapshotParams  +  dirty_bitmap_path: Option<PathBuf>      ← 可选字段
PUT /snapshot/rollback                                            ← 新端点
PUT /snapshot/save-dirty-bitmap                                   ← 新端点
```

不使用这三处的调用方，看到的行为与上游完全一致：分叉越小越容易跟上游合并，出问题时越容易判断是不是我们引入的，上游既有测试仍然有效。
划分原则是**语义在 orchestrator、机械动作在 Firecracker**：Firecracker 不知道"树"，只做"把这个位图指定的页从这个文件写进 guest 内存"这类无状态动作。

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

给了 `dirty_bitmap_path` 却没给 `mem_file_path` 会被拒绝（`persist.rs:173-175`，`DirtyBitmapWithoutMemFile`）。

### 2.1 差分文件不自说明

Diff 快照的内存文件是稀疏的，但"空洞"和"内容恰好是零的页"在文件系统层面不可区分：被 guest 写成全零的页也会分配、也出现在 data 区间里；
反过来文件系统也可以把全零区间打洞回收。所以必须有一份显式记录说明这次快照写了哪些页，这就是纪元位图 `E_x`。

### 2.2 为什么必须在同一次调用里

先 `create` 再调一个"取位图"接口？不行：写快照的最后一步就是清位图（`reset_dirty`）。在 `create` 之前先取？也不行：取图是破坏性的
（[13](13-dirty-page-tracking-and-hdbss.md)），取完 `dump_dirty` 就拿不到 KVM 那一半。**唯一正确的位置是 `dump_dirty` 内部**：它遍历
"KVM 位图 ∪ 用户态位图"时顺手攒出 merged 位图返回，由 `snapshot_memory_to_file`（`vstate/vm.rs:463`）写成侧车。
顺带省掉一轮 API 往返，以及"取图"接口本来要做的一次全内存扫描。

**全量快照也写位图**，内容全 1、尾字按 `num_pages` 掩码（`vm.rs:519-530`，单测 `vm.rs:731` `test_full_snapshot_sidecar_is_all_ones`）。
orchestrator 侧对全量条目不再读侧车、直接当全 1 用（[14](14-memory-diff-tree.md)）。

### 2.3 落地的持久性

内存文件与侧车都只 `flush()`、**不 `sync_all()`**（`vm.rs:534-560`）：读这两个文件的只有同一台宿主上的 orchestrator，
走同一份 page cache，写返回即可见；checkpoint 本来就不承诺活过 orchestrator 进程（[21](21-state-concurrency-durability.md)），
fsync 买不到东西，却要在暂停窗口里付 ext4 日志提交的串行等待。

snapfile 分两种情况，由 `persist.rs:232` `snapfile_must_be_durable` 判定：请求带 `mem_file_path`（checkpoint 路径）→ 不 sync；
不带（e2b 原生 pause，snapfile 要进持久化存储）→ 保留上游的 `sync_all()`（`persist.rs:258-262`）。

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

主体（十个阶段）见 [17](17-in-place-rollback.md)；各计时与计数字段如何进入 orchestrator 的计时键见 [07](07-observability-reference.md)。
这里只说接口层面的决定。

### 3.1 `resume_vm: false`

orchestrator 传的永远是 `false`（`internal/sandbox/fc/rollback.go:93-99`）：磁盘视图还没换，内存已经在目标时刻、磁盘还在当前时刻，
这时恢复运行会让 guest 看到撕裂的世界。恢复由 orchestrator 在换完磁盘视图、清完 conntrack 之后自己做。保留 `true` 是为了让端点能独立使用。

### 3.2 `revert_bitmap_path` 是可选的

> The live dirty bitmap is always unioned in; this carries the pages dirtied in *earlier* epochs since the target
> snapshot (the orchestrator's cumulative set). Without it only a rollback to the current epoch's base is correct.

不给位图时回滚集 = 仅活跃脏页，只有"回滚到当前基准"时才正确。跨多代回滚必须给，orchestrator 永远给。

### 3.3 回滚前的校验

提交点之前（阶段 1–3，`firecracker/src/vmm/src/rollback.rs:271-352`），Firecracker 依次检查：虚机处于 `Paused`（`Faulted` 另报）；
snapfile 能加载、拓扑与运行中的虚机一致；**内存文件长度等于 guest 内存大小**；位图几何（页大小、页数）一致；
然后取活跃脏图、与传入位图求并得出回滚集，**再检查内存文件在回滚集的每个偏移上都有数据**（`validate_mem_file_coverage`，:858）。

最后一项用 `SEEK_DATA` / `SEEK_HOLE` 遍历文件的 extent，每个空洞一对 `lseek`。它挡住的是"把一个 Diff 文件当完整视图递过来"
和"orchestrator 漏物化了回滚集中的某一页"两种错误 —— 否则 `restore_dirty` 会从空洞读到零、写进 guest 内存，而且发生在提交点之后。
文件系统不支持 `SEEK_DATA`（`EINVAL`）时这项检查跳过并告警一次。

### 3.4 错误分级：状态码加结构化字段

| 失败位置 | HTTP | 响应体 | 虚机状态 | orchestrator 翻译成 |
|---|---|---|---|---|
| 提交点之前（校验、取图、覆盖检查） | 400 | `fault_message`，**没有** `fault` 字段 | `Paused`，可恢复 | 普通错误 → resume 虚机，restore 失败 |
| 提交点之后（内存、vCPU、GIC、设备） | 500 | `fault_message` + `"fault": true` | 标为 `Faulted`，拒绝 resume 与快照，只能替换进程 | `RollbackFaultedError` → 撕裂 |
| 路由不存在（未打补丁的二进制） | 404，或 400 且文案为 `Invalid request method and/or path` | —— | 未动 | `RollbackUnsupportedError` |

Firecracker 侧：`RollbackError::faults_vm()`（`rollback.rs:85-101`）决定哪些变体在提交点之后；`api_server/parsed_request.rs:205`
对这些变体回 500，`api_server/mod.rs:206` 组出 `{"fault_message": ..., "fault": true}`；单测 `request/snapshot.rs:446`
`test_rollback_error_response` 钉住线上格式。orchestrator 侧：`classifyRollbackFailure`（`internal/sandbox/fc/rollback.go:185`）
**先看 `fault` 字段，不看文案**。只有响应体里根本没有 `fault` 字段时，才退回对 `Faulted` / `faulted` 字样的字符串匹配 ——
这既覆盖不带该字段的旧二进制，也覆盖 400 的 `RollbackError::Faulted`（"虚机已因之前的回滚失败而撕裂"）。

判错的代价在两个方向上都很高：把撕裂当成可恢复是静默损坏，把可恢复当成撕裂是白杀一个沙箱。所以不能依赖另一个仓库随时可能改写的文案。

### 3.5 客户端侧的两个细节

| 细节 | 理由 |
|---|---|
| **禁用 keep-alive**（`fc/rollback.go:121`、:241） | Firecracker 的 API server 有并发连接上限。client 每次调用新建，池化连接会一直开到 transport 被回收，回滚够多次就用光上限，此后每次 503 |
| **client 自己不设超时**（`Timeout: 0`，:126、:244） | 时限由调用方的 context 统一给（`CHECKPOINT_FC_CALL_TIMEOUT`，见 [06](06-configuration-and-capacity.md)）；transport 里再藏一个固定值，两者不一致时它会悄悄胜出 |

rollback 调用超时按撕裂处理：主机放弃等待时说不清 Firecracker 在提交点哪一侧（`internal/sandbox/checkpoint.go` `RollbackInPlace`）。

---

## 4. 扩展三：`PUT /snapshot/save-dirty-bitmap`

```rust
pub struct SaveDirtyBitmapParams { pub path: PathBuf }
```

把当前的活跃脏页位图（自上次快照或回滚以来写脏的页）以 FCDB 格式写到 `path`，要求虚机暂停（`rollback.rs:796`）。
实现与回滚阶段 3 是同一套动作：取 KVM 位图 → 先折回用户态位图 → 展平 → 序列化写文件。**它不清位图**，所以之后的 Diff 快照或回滚仍看到完整集合。

### 4.1 为什么非有不可

契约（[14](14-memory-diff-tree.md)）：Firecracker 回滚时会把自己的活跃脏页并进写回集，再按页从 `mem_file` 读。
ext4 方案递过去的是**只含回滚集的稀疏文件**，所以 orchestrator 必须**事先**知道 Firecracker 会并入哪些页，把它们也物化出来。
没有这个端点，那些页就落在文件空洞上：现在会被 §3.3 的覆盖检查挡下、restore 失败；没有那道检查时则是静默清零。

orchestrator 还在两处用它：全量 checkpoint 拍之前导出一次活跃脏图，并进"自启动以来脏页集"（[14](14-memory-diff-tree.md#9-checkpoint-之后再做原生-pause-的正确性前提)）；
原生 pause 取写跟踪位图（没有这个端点时 pause 退回驻留判据）。

### 4.2 暂停前提

端点开头就检查 `VmState::Paused`。这不只是防御：虚机不暂停，活跃脏页集会在导出之后继续增长，物化出来的内容就覆盖不全。
编排侧对应地保证次序：先 Pause，再 SaveDirtyBitmap，再物化，再 RollbackSnapshot（[19](19-end-to-end.md)）。

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

两侧读时都要求文件长度**恰好**等于 24 + 8 × 字数，版本不符、magic 不符一律拒绝（`deserialize_dirty_bitmap`，`memory.rs:563`；
`readDirtyBitmap`，`bitmap.go:29`；测试 `bitmap_test.go:17` `TestBitmapWireFormat`、:40 `TestBitmapRejectsMalformed`）。

### 5.2 页索引是什么空间

**是内存快照文件里的偏移除以页大小，不是 guest 物理地址。**

> Page indices are file offsets in the memory snapshot divided by page size (guest regions tiled in order) — the same
> space `dump_dirty` writes in, so the orchestrator can union bitmaps and pread pages without knowing the guest's
> physical memory layout.

guest 内存可能由多个 region 组成，快照文件把它们按顺序首尾相接平铺。orchestrator 因此只在一个平坦的、`num_pages` 长的位空间上做并集，
在一个平坦的文件上 `pread` / `pwrite`，**内存布局的知识全部留在 Firecracker 里**。代价是位图与具体布局绑定，换了 region 划分老位图就不能用 ——
几何校验会挡住它。

### 5.3 几何校验

| 位置 | 检查 |
|---|---|
| `entryBitmap`（orchestrator，`store.go:1434`） | 与当前 guest 的页数、页大小对比，不符则报错 |
| `merge`（orchestrator，`bitmap.go:62`） | 两个位图必须同几何才能求并 |
| 回滚端点（Firecracker，`rollback.rs:307-323`） | 位图几何 + 内存文件长度，都要对上运行中的虚机 |

不符时**报错，不猜测**：几何不符的位图属于另一个虚机或另一次运行。

### 5.4 尾字必须掩码

`num_pages` 通常不是 64 的倍数，最后一个字里不对应任何页的位**必须为 0**。两侧的全 1 位图刻意写成镜像：Firecracker 的 Full 分支
（`vm.rs:524-529`）与 orchestrator 的 `allOnesBitmap`（`bitmap.go:110`）。按字处理的代码（如回滚集解析）另外在读时屏蔽尾部多余的位。

| | Firecracker（Rust） | orchestrator（Go） |
|---|---|---|
| 写 | `serialize_dirty_bitmap` | `dirtyBitmap.writeTo` |
| 读 | `deserialize_dirty_bitmap` | `readDirtyBitmap` |
| 全 1 | `SnapshotType::Full` 分支 | `allOnesBitmap` |

四个函数、两种语言，描述同一个格式，**改任何一个都必须同时改另一个**，靠版本号和上面的单元测试守着。

---

## 6. 版本配对

**orchestrator 与 Firecracker 必须成对交付。** 两者出自同一个代码仓库的同一个版本（`packages/` 与 `firecracker/`，代码基准见 [README](README.md)）；按 rpm 部署时分别对应
`0001-adapted-for-arm-architecture.patch` 与 `firecracker.arm`（部署前核对见 [05](05-deployment-prerequisites.md)）。

| 配法 | 结果 |
|---|---|
| 新 orchestrator + 未打补丁的 Firecracker | restore 时 `/snapshot/rollback` 路由不存在 → `RollbackUnsupportedError`，**明确失败** |
| 新 orchestrator + 没有 `save-dirty-bitmap` 的分叉 Firecracker | restore 失败（导出活跃脏图失败，虚机 resume）；全量 checkpoint 前的活跃脏图导不出，"自启动以来脏页集"改用全 1 侧车（多记不少记） |
| 旧 orchestrator + 新 Firecracker | 功能上兼容（扩展是加法），但没有 checkpoint 能力 |

第一行是**好的失败**：立刻、明确、指名道姓。分叉的端点用新路径而不是给既有端点加参数，正是为了让"不支持"表现为路由不存在，
而不是一个被忽略的字段。

---

## 7. seccomp

Firecracker 给每个线程装独立的 seccomp 过滤器（`firecracker/resources/seccomp/aarch64-unknown-linux-musl.json`，分 `vmm` / `api` / `vcpu` 三组）。
回滚路径引入的系统调用必须逐一放行：

| 线程 | 需要什么 | 为什么 |
|---|---|---|
| vCPU 线程 | `KVM_SET_ONE_REG`、mp state、`KVM_ARM_VCPU_INIT` 及其后的 SVE finalize 等 vCPU ioctl（:1043-1082） | vCPU 状态恢复派发到这些线程执行（[17](17-in-place-rollback.md)） |
| VMM 线程 | `TUNSETOFFLOAD`（:448） | 回滚后按回退后的协商特性重设 tap 的 offload 标志，在 VMM 线程上执行 |
| VMM 线程 | `pread64`（:29） | "取脏页位图"路径逐页读 `/proc/self/pagemap`（[13](13-dirty-page-tracking-and-hdbss.md)） |

反过来也有约束：**运行期能做的事比初始化期少**，例如回滚时不能重建需要 `memfd_create` 的对象（[18](18-rollback-pitfalls.md)）。
改回滚路径时，新增任何系统调用都要同步检查白名单；漏了的表现是进程在运行期被 SIGSYS 杀掉。

---

## 8. 小结

1. 分叉是加法：一个可选字段、两个新端点、HDBSS 启用、seccomp 放行；划分原则是语义在 orchestrator、机械动作在 Firecracker。
2. `dirty_bitmap_path` 必须在快照的同一次调用里：写快照的最后一步会清位图，取图又是破坏性的。
3. rollback 的错误按**状态码 + `fault` 字段**分级：提交点前 400（可恢复），提交点后 500 + `"fault": true`（撕裂）；文案匹配只作兜底。
4. Firecracker 在提交点前检查内存文件覆盖回滚集的每一页，漏物化变成明确的失败。
5. `save-dirty-bitmap` 让 orchestrator 事先知道活跃脏页集，是稀疏物化文件成立的前提；它不清位图。
6. FCDB 的页索引是**内存文件偏移空间**；几何校验三处都做，尾字必须掩码，两侧实现写成镜像。
7. 版本必须成对，配错表现为路由不存在 → 指名道姓的错误。
