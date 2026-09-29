# 21 · 状态、并发与持久性

> 这篇给开发者看。这套系统要在一台**正在运行的虚拟机**上换零件 —— 换它的磁盘视图、改写它的内存、替换它的写层文件 ——
> 同时别的沙箱在并发地做同样的事。读完能说清状态怎么组织、锁怎么分层、「虚机暂停」在并发模型里扮演什么角色、
> 为什么全程不 fsync 也是对的，以及同一节点上的沙箱会通过哪些共享资源互相影响。
>
> 预备：[19 · 端到端走查](19-end-to-end.md)、[20 · 失败语义](20-failure-semantics.md)。

---

## 1. 三类状态

| 类别 | 具体是什么 | 谁保护 | 进程退出后 |
|---|---|---|---|
| **账本** | `Store` 里按沙箱分的表：条目 `bySandbox`、基准 `bases`、磁盘账本 `rootfs`、子节点计数 `children`、层引用 `layerRefs`、字节计数、合并（compact / fold）队列、代际归属 `owners` | `Store.mu`（全局互斥） | **全部消失**，从不从磁盘读回 |
| **活体对象** | `Overlay`、`Cache`、层栈、Firecracker 进程、NBD 设备 | 各自的锁 + **虚机暂停** | 随进程消失 |
| **文件产物** | 内存差分、位图侧车、snapfile、层文件与 `.meta`、header、manifest | 无锁；靠**按沙箱串行** + 原子提交 | 留在盘上，下次启动被清空 |

第三行是一个关键取舍：文件留着，但没有代码去加载它们（[22](22-lifecycle-reasoning.md)）。所以「崩溃后的一致性恢复」这个问题在这里不存在
—— orchestrator 重启会带走它上面的所有沙箱，checkpoint 跟着一起走。并发模型因此只需要保证进程活着的时候不出错。

---

## 2. 锁的层级

### 2.1 `Store.mu`：保护账本，持锁不做 I/O

一把普通互斥锁，保护上面那些表，持有时间**极短** —— 只在读写 map 期间。**规则：持有 `Store.mu` 时不做文件 I/O、不调 Firecracker。**
这条规则在代码里有几个典型的落实方式：

**① 手工解锁，锁内只算路径。** `MaterializeRevert` 在锁内取目标、查断链、算祖先链与回滚路径；之后的读位图、写物化文件全在锁外
（`store.go:1510-1548`）。它每个 `return` 前手工解锁而不用 `defer`：写成 `defer` 会让整个物化的 I/O 都持着全局锁，阻塞这台机器上所有沙箱的账本操作
—— 包括另一个正在冻结窗口里等这把锁的 restore（等待时间记在 `materialize_lock_wait`）。

**② 锁内摘账，锁外删文件。** 删除、剪枝、基准移动、沙箱移除这些操作在锁内只改 map，把要删的东西收进一个 `reclaim` 列表
（`store.go:133-232`），由调用方**放锁之后、返回之前**删除：

- 单个 checkpoint 的目录、被级联回收的祖先目录、隐藏条目的 snapfile 与 header、计数归零的层文件：放锁后直接删除；
- **整个沙箱的目录树**（沙箱被移除，或新一代接管同一个 id 时丢弃上一代的树）：锁内先 `rename` 成 store 根下的 `.trash-<uuid>`
  （`dropStateLocked`，`store.go:894`），放锁后再删这个 trash 目录。rename 是 O(1) 的，放锁那一刻原路径就空出来了；
  若放锁后原地删，会与同 id 的下一代在同一路径上 `MkdirAll` 竞争。rename 本身失败时才退回锁内原地删 —— 慢总比竞争好。

放锁后删为什么安全（`reclaim` 类型的注释逐条论证）：被收集的对象已经离开账本，任何查找都不会再把它交给别人；checkpoint 目录按准备时刻命名
（`ckpt_<UnixNano>`）、沙箱树先改成唯一名字，放锁后新建的东西不会被误删；读这些文件的只有同一沙箱的操作，而它们全程持有沙箱操作锁，
删除在调用方返回前完成，也就在操作锁释放前完成；层文件只在引用计数归零后才收集，而计数不会从零回升。唯一例外是 `OnRemove` / `OnInsert`
等操作锁超时的情形 —— 那时无论在锁内还是锁外删，都可能删掉进行中操作正在用的文件。

**③ 用计数表代替扫描。** 「这个条目有没有子节点」若靠扫描回答，就要扫一遍该沙箱的全部条目，而删除每次调一次、剪枝每一步调一次，全在全局锁下。
所以由子节点计数表 `children[sandbox][parentID]` 直接回答（`hasChildLocked`，`store.go:1907`），在条目进出账本处维护，O(1)。

**④ 发布是 O(1) 的。** `Commit` 在锁内只插入条目、移动基准、登记计数，不复制、不重写整份 index（index.json 默认不写，[12](12-architecture.md)）；
`commit_index` 计的就是这段锁内工作。合并的文件工作同样全在锁外，只在挑候选和最终切换时各短暂持锁一次（[20](20-failure-semantics.md)）。

### 2.2 `opLocks[sandboxID]`：按沙箱串行

每个沙箱一把锁（容量为 1 的 channel），checkpoint / restore / delete **全程持有**。`gate()` 在 `Store.mu` 下取出（或创建）这把锁，
**返回前已放掉 `Store.mu`**，然后才去等沙箱锁 —— 反过来（持着全局锁去等）会让一个进行中的 checkpoint 把全局账本锁一直占着。

排队有上限：持锁方可能卡在一个永不返回的调用上（NBD flush、不再应答的 Firecracker），无限排队只会让每个等待者各占一条连接、最后得到一个光秃秃的读超时。
所以应答客户端的路径都用限时版本 `LockSandboxWithin`，等满 `CHECKPOINT_LOCK_WAIT_TIMEOUT` 就回 busy（[06](06-configuration-and-capacity.md)、[03](03-errors-timeouts-concurrency.md)）。
沙箱插入 / 移除的回调也用限时版本，超时只告警、照常继续；不限时的 `LockSandbox` 留给不面向客户端的内部路径。
沙箱被移除后这把锁**不删**：同一个 id 经 pause/resume 会回来，两代共享同一个目录树，必须由同一把锁串行。

### 2.3 层级与理由

```
opLocks[sandbox]   ← 长时间持有（整个操作）
      ↓ 可以获取
Store.mu           ← 短时间持有（一次 map 读写）
```

**只有这一个方向**，所以不会死锁。

**为什么按沙箱**：要保护的是每沙箱的状态 —— 内存基准和磁盘层栈。两个操作同时推进同一个沙箱的基准，账本就会与实际的层栈、脏页跟踪状态错位；
不同沙箱之间在账本层面没有共享状态，可以完全并行（`TestCreatesOfDifferentSandboxesRunConcurrently`）。

**为什么全程持有**：一次 checkpoint 的中间态是 Firecracker 已写完快照（纪元前进了）而账本还没提交。此时另一个 checkpoint 进来会读到**旧**基准，
两代产物声称有同一个父亲、而它们的纪元位图覆盖了重叠的时间区间 —— [14](14-memory-diff-tree.md) 的那条不变量就破了。restore 同理：它最后把基准挪到目标，
中间并发的 checkpoint 会挂在错误的父亲上。合并也依赖这一点：它在 delete 里执行，持着沙箱锁，所以没有 restore 能与它交错。

---

## 3. Overlay：在 NBD dispatcher 底下换指针

`Overlay` 有自己的读写锁，保护 `device`（读路径的层栈）和 `cache`（写层）两个字段（`block/overlay.go:13-24`）。

- **读写路径持共享锁，而且持满整个读或写**（`ReadAt`，`overlay.go:39`；`WriteAt`，`overlay.go:201`）。原因是 `Seal` 把旧写层**原地追加**进同一个层栈，这把锁挡住读路径去读一个正在更新的索引，
  并在 `Seal` 退出时把新条目发布给之后的所有读者。读者之间持的是共享锁，互不排斥。数据本身的并发由各 `Cache` 自己负责（块集合是原子位图，[16](16-disk-layering.md)）。
- **`Seal` 与 `ResetView` 持独占锁**，在锁内完成替换：`Seal` 追加层、换写层；`ResetView` 一次换掉整个读路径和写层，旧写层连文件关闭，旧层栈只解除映射（文件归 store）。

NBD dispatcher 手里那个 `*Overlay` 指针**从头到尾没变过**，它不知道背后换了东西。

---

## 4. 暂停窗口作为静默机制

锁保证的是**数据结构的完整性**。换视图还需要另一样东西：**没有正在进行的 guest I/O**。锁做不到 —— 一个 guest 写请求可能已经进了内核块层、还没到 dispatcher。

| 操作 | 前置条件 | 谁保证 |
|---|---|---|
| `SealLayer` | 虚机暂停 + NBD `BLKFLSBUF` 刷屏障 | 调用方（`CheckpointToFiles`）+ 函数自己刷屏障 |
| `ResetView` | 同上 | 调用方（`RollbackInPlace`）+ 函数自己刷屏障 |
| `save-dirty-bitmap`、`rollback` | 虚机暂停 | Firecracker 端点自己检查 `VmState::Paused` |
| 清 conntrack | 虚机暂停（没有 guest 流量） | 调用方 |

分工：**锁**处理「另一个 goroutine 在改同一个字段」；**暂停**处理「guest 还在产生新 I/O」；**刷屏障**处理「内核手里还攥着已接受的写」。
三者缺一不可，而**只有第一个能靠代码自己保证**，后两个是调用契约，所以相关函数的文档注释都以「The VM must be paused」开头。

> 当外部世界（guest、内核）也在改状态时，锁只能保护你自己的数据结构，剩下的要靠**让外部世界停下来**。

---

## 5. 原子提交与持久性

### 5.1 原子发布，刻意不 fsync

一个条目必须**要么完整可见，要么完全不存在**。做法是 temp + rename，**刻意不 fsync**（`commitFiles`，`store.go:1032`）。POSIX 保证同目录内的 rename 是原子的；
manifest 同理（写临时文件再 rename）。这就是原子发布的全部 —— 它不依赖落盘，因为之后的每一个读者（orchestrator 解析页、Firecracker 读回文件、层栈读封存层）
走的都是写入方用过的同一份 page cache。

| | 内容 | 依据 |
|---|---|---|
| **承诺** | 发布是原子的：`list` 看不到半成品，restore 拿不到半成品 | `commitFiles`、`writeManifest` 的 rename；`prepared` 条目不进账本（§5.2） |
| **承诺** | checkpoint 返回成功后，同一宿主上立即可读、内容一致 | 读侧全部是普通缓冲读，无 `O_DIRECT` |
| **承诺** | e2b **原生 pause** 的 snapfile 仍然 fsync —— 那份文件要进持久化存储、活过本进程 | `firecracker/src/vmm/src/persist.rs` — `snapfile_must_be_durable`（判据：请求不带 `mem_file_path`） |
| **不承诺** | checkpoint 的任何文件落盘：snapfile、内存差分、侧车、回滚集、封存层、manifest 全程不 fsync | `commitFiles` 注释；`vstate/vm.rs` 写内存与位图只 `flush()`；`rootfs/nbd.go` — `SealLayer` 不做 msync |
| **不承诺** | checkpoint 活过 orchestrator 进程：账本只在进程内存里；`NewStore` 启动时先 `os.RemoveAll` 整个 store 根目录、再 `MkdirAll` 重建 | `store.go:535-542` |
| **不承诺** | checkpoint 活过宿主崩溃或掉电：目录里可能留下残缺文件，但没有任何东西会去读它，且下次启动即被清空 | 同上 |

**为什么不做 fsync 是对的**：fsync 能买到的只有「活过宿主崩溃」，而引用这些文件的账本本来就活不过进程；一致性不欠 flush 任何东西，因为读写走同一份 page cache。
而 fsync 的代价落在并发上：ext4 的日志提交是整个文件系统的串行点，多个沙箱同时 checkpoint 时每个 fsync 都要等别的沙箱的回写，暂停窗口内的那几次尤其贵。
将来若要做可持久化的 checkpoint，这些 fsync 需要按事务顺序（数据先于账本）整体加回来，并同时解决账本重建与沙箱重建（[22](22-lifecycle-reasoning.md)）。
面向使用方的结论（checkpoint 不是备份）在 [02](02-semantics-and-limits.md)。

### 5.2 两态可见性

条目有 `prepared` 与 `committed` 两态，但关键在于：**`prepared` 条目根本不在账本里**。`Prepare` 只建目录、写磁盘上的 manifest，不往 `bySandbox` 里放；
条目是在 `Commit` / `CommitHidden` 里通过 `publishLocked` 才进账本的。所以 `Get` 只需检查代际归属与是否隐藏（`store.go:1854`）——「不可见」不是靠过滤条件，而是**根本不存在**。

### 5.3 路径注入防护

条目 id 和沙箱 id 都会拼进文件路径，`validateID` 拒绝空串、路径分隔符和 `..`。在解析请求时（`checkpointIDFrom`）和使用时（`Prepare`、`LayersDir`、`RemoveSandbox`）**都做**；
`RemoveSandbox` 那一处尤其重要，它要删一整个目录树。

---

## 6. 请求生命周期 ≠ 操作生命周期

checkpoint 和 restore 的第一件事都是 `ctx := context.WithoutCancel(r.Context())`：HTTP 请求上下文在客户端断开时立刻取消，操作若跟着取消，
就会留下一个**暂停中、但所有人都以为在运行**的沙箱。

配套的是 `CheckpointToFiles` 里无条件的 `defer resume`，它对 `ResumeVM` 又套了一层 `WithoutCancel`：即使外层 ctx 因为别的原因被取消，恢复虚机也不能被跳过。
pause 本身超时的情形也照此处理：超时说明 pause 可能已经在另一侧生效，所以先补一次 resume 再报告失败（`pauseVM`，`internal/sandbox/checkpoint.go:78`）。

> 恢复虚机是全系统唯一一处「无论如何都要执行」的清理动作。例外只有撕裂：那时虚机刻意保持暂停（[20](20-failure-semantics.md)）。

---

## 7. 清理与幂等

restore 路径上有两个 `defer` 清理：删活跃位图临时文件，以及 `MaterializeRevert` 返回的 `cleanup`（删 `revert_mem.tmp` 与 `revert_bitmap.tmp`）。
`cleanup` 的注释写明它「无论回滚怎么结束都可以安全调用」—— 它就是两个 `os.Remove`，对不存在的文件返回的错误被忽略。
物化失败时 `MaterializeRevert` 自己会先调一次 `cleanup` 再返回错误，此时调用方拿不到 `cleanup`，不会重复调用；但这份幂等性依赖「`cleanup` 只做 `os.Remove`」，
往里加任何有副作用的动作之前要想清楚。

---

## 8. 节点级效应

同一节点上的沙箱在账本层面互不相干，但通过三样共享资源互相影响。它们不会破坏正确性，但会把一个沙箱的代价算到别的沙箱的冻结窗口里。

**① 全局账本锁。** §2.1 的规则就是为此：任何在 `Store.mu` 下做的慢事，都会出现在别的沙箱 restore 的 `materialize_lock_wait` 里。

**② 宿主 conntrack 表。** 全节点一张，restore 越频繁、表越大，宿主侧清扫越贵；清扫器攒批与选路见 [18](18-rollback-pitfalls.md)。

**③ memory cgroup 与 Go GC 的长暂停。** 这是最隐蔽的一条。

- **共享的 memory cgroup**：交付的部署里 orchestrator 由 Nomad job `template-manager-system` 拉起，落在这个 task 的 memory cgroup
  （cgroup v1 下是 `memory:/nomad/<alloc-id>.start`，v2 下是 `nomad.slice/…/<alloc-id>.start.scope`），上限取自 `template-manager.hcl`
  的 `resources.memory`（交付值 262144 MiB，即 256 GiB）。上游原本用 `CLONE_INTO_CGROUP` 把每个 Firecracker 放进 `/sys/fs/cgroup/e2b/sbx-<id>`，
  但 ARM 移植补丁把这几行注释掉了（`internal/sandbox/fc/process.go:185-191`），Firecracker 作为子进程继承 orchestrator 的 cgroup，
  `/sys/fs/cgroup/e2b/sbx-*` 目录存在但里面没有进程。所以 orchestrator 与全部 Firecracker 同在一个 memory cgroup，但**记账的只是其中一部分**：
  - **记账**：orchestrator 自己的堆；Firecracker 进程 guest 以外的内存；checkpoint / restore 读写的文件（内存差分、层文件、物化文件）
    以及模板构建写的文件，都经 page cache 记在这个 cgroup 上；
  - **不记账**：guest 内存。Firecracker ≥ 1.7 时沙箱以 2 MiB 大页启动（`packages/api/internal/sandbox/sandbox_features.go:45` `HasHugePages`；
    FC 侧 `machine_config.rs:65` 用 `MAP_HUGETLB | MAP_HUGE_2MB` 映射），hugetlb 页只记 hugetlb 控制器，memory 控制器不记
    （cgroup v2 以 `memory_hugetlb_accounting` 选项挂载时例外，该选项默认关）。

  所以把这个 cgroup 填满的主要是 page cache，而不是沙箱内存。负载一高它就被填满，之后内核在这个 cgroup 内持续回收。
  配额怎么取、改哪几个文件，见部署文档 [`12-orchestrator资源配额调优.md`](../../deploy-docs/12-orchestrator资源配额调优.md)。
- **缺页卡在回收里**：orchestrator 的块设备层用 `MAP_SHARED` 文件映射读写层文件。goroutine 在映射上拷贝时，缺页在**用户态**发生：分配页 → 记账到 memcg →
  超限触发回收 → 在回收里睡（`reclaim_throttle`）。这期间这个 goroutine 的 P 一直处于 `_Prunning`，而一个睡在内核缺页里的线程无法被异步抢占。
- **STW 被拉长**：Go 的 stop-the-world 要等所有 P 停下，只能等它醒来。于是一次 GC 暂停被拉长到数百毫秒，而且落在别的沙箱 restore 的冻结窗口里。
  判据是：长暂停全部花在「等所有 P 停下」这一半，而不是收集器本身的工作。
- **缓解：`MADV_POPULATE`**（`block/populate.go:105` `populateMapped`）。拷贝前对页对齐的范围调用 `madvise(MADV_POPULATE_READ / MADV_POPULATE_WRITE)`，
  每次最多 1 MiB，把缺页、记账、回收（写时还有块分配）挪进**系统调用**里做；处于系统调用中的 goroutine 其 P 是 `_Psyscall`，STW 不等它，
  随后的拷贝发现页已映射。调用点是 `block/cache.go` 的读（`:285`）和写（`:449`）、`chunk.go:141`、`streaming_chunk.go:205`。
  启动时在一个匿名页上探测一次，内核不支持（< 5.14，返回 `EINVAL`）则永久关闭、行为回到原样；`EFAULT` / `EHWPOISON`（直接访问会收到 `SIGBUS`，
  如产物盘写满）转成该次 I/O 的错误，其它错误一律回退为直接拷贝。它是优化不是保证：已填充的干净页仍可能在拷贝前被回收，缺页保护依然保留。
- **减少垃圾**同样有帮助：index.json 默认不写、发布 O(1)、锁外删文件，都减少了 GC 的次数与需要的暂停。
- **取证**：`CHECKPOINT_RUNTIME_METRICS=true` 时 orchestrator 每 10 s 记一行 runtime/metrics 的 STW 直方图（等 P 停下的 `stopping`、总暂停 `total`、GC 周期数），
  用来区分长暂停是卡在「停下」还是卡在「收集」（开关见 [06](06-configuration-and-capacity.md)，字段见 [07](07-observability-reference.md)）。
  缓解前后的 STW 分布与代价见 [25](25-results-and-compliance.md)。

---

## 9. 相关内容的位置

- **同沙箱多调用方、流式调用被 restore 截断**（既定限制），以及已修复的非 restore 流截断：[03](03-errors-timeouts-concurrency.md)。
- **限额与超时开关**（`CHECKPOINT_MIN_FREE_BYTES`、`CHECKPOINT_MAX_PER_SANDBOX`、`CHECKPOINT_MAX_BYTES_PER_SANDBOX`、`CHECKPOINT_LOCK_WAIT_TIMEOUT`、
  `CHECKPOINT_FC_CALL_TIMEOUT`）：[06](06-configuration-and-capacity.md)。

---

## 10. 单元测试守着哪些性质

`packages/orchestrator/internal/checkpoint/*_test.go` 里与本篇和 [20](20-failure-semantics.md) 对应的测试：

| 测试 | 守什么 |
|---|---|
| `TestPrepareIsInvisibleUntilCommit` | 提交前不可见 |
| `TestCommitMovesFilesAndWritesManifest`、`TestCommitPublishesThroughRenamesOnly` | 原子提交只靠 rename |
| `TestCommitAdvancesBaseAndParentsChain`、`TestSetBaseToEntryBranchesTheTree` | 基准推进；回滚后从目标分叉 |
| `TestCommitHiddenRescuesAdvancedEpoch`、`TestInvalidateBaseBreaksChainUntilFullRoot` | 纪元不能丢；断链后拒绝恢复 |
| `TestDeleteHidesWhileReferencedAndCascades`、`TestHiddenEntriesDropWhatCanNeverBeRead`、`TestBaseMoveReclaimsUnreferencedHiddenEntries` | 依赖感知的删除与级联回收 |
| `TestChildCountsFollowTheTree` | 子节点计数表与树一致 |
| `TestSlowRemovalHoldsUpNobodyElse`、`TestHidingDeleteRewritesTheManifestOutsideTheLock`、`TestRemovedIdIsReusableWhileItsTreeIsStillBeingDeleted` | 锁外删文件；沙箱树改名后删 |
| `TestCommitAllocationsDoNotGrowWithDepth`、`TestCommitDoesNotWriteIndexByDefault` | 发布 O(1)，默认不写 index |
| `TestFoldingChangesNoRestore`、`TestFoldingDoesNotHoldTheStoreLock`、`TestOneDeleteFoldsAtMostItsBudget`、`TestAFoldIsRetriedAFewTimesAndThenLeftAlone` | 合并不改变任何 restore；不持全局锁；每次预算；重试上限 |
| `TestLayerRefsHoldUnderRandomOperations` | 层引用计数在随机操作下与重算一致 |
| `TestCrossTreeRevertAfterLostEpoch` | 全量根回滚因子恒为全 1 |
| `TestLockSandboxWithinGivesUpInsteadOfQueueingForever`、`TestCreatesOfDifferentSandboxesRunConcurrently` | 排队上限；不同沙箱并行 |
| `TestGenerationsUnderConcurrency`、`TestALateRemovalDoesNotTakeTheNewGenerationDown` | 代际归属 |
| `TestValidateIDRejectsTraversal` | 路径注入 |
| `TestNewStoreStartsFromAnEmptyRoot` | 启动清空 store 根 |
| `header_agreement_test.go` | 合并 header 与层栈读的一致性 |

测试体系的全貌见 [23](23-testing-and-functional-verification.md)。

---

## 11. 小结

1. 三类状态：账本（进程内存）、活体对象、文件产物。**账本不跨重启存活**，这个取舍让并发模型少了崩溃一致性这一整类问题。
2. 两把锁，**单向层级**：沙箱操作锁（长持有）→ `Store.mu`（短持有），不会死锁；排队有上限，超时回 busy。
3. **持 `Store.mu` 时不做 I/O**：手工解锁、锁内摘账锁外删文件（沙箱树先改名为 `.trash-<uuid>`）、子节点计数表、O(1) 发布。
4. `Overlay.mu` 保护指针替换和原地追加的层栈索引；NBD dispatcher 手里的指针从头到尾没变过。
5. 并发安全靠三样东西分工：**锁**、**暂停**、**刷屏障**。只有第一样能靠代码自保。
6. 发布靠 rename 原子可见，**刻意不 fsync**：一致性来自共享的 page cache，持久性本来就不在承诺之内。
7. 节点级效应：全局锁、宿主 conntrack 表、共享 memory cgroup。最后一条会借 GC 的长暂停跨沙箱传播，`MADV_POPULATE` 把缺页挪进系统调用来缓解。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 两把锁 | `packages/orchestrator/internal/checkpoint/store.go` — `Store.mu`、`opLocks`、`gate`、`LockSandboxWithin`、`LockSandbox` |
| 锁外删除 | 同上 — `reclaim`、`dropStateLocked`、`trashPrefix` |
| 子节点计数 | 同上 — `hasChildLocked`、`addChildLocked`、`removeChildLocked` |
| 原子提交与启动清空 | 同上 — `commitFiles`、`writeManifest`、`publishLocked`、`NewStore`；`firecracker/src/vmm/src/persist.rs` — `snapfile_must_be_durable` |
| 两态可见性、路径校验 | 同上 — `Prepare`、`Get`、`validateID` |
| 指针替换 | `internal/sandbox/block/overlay.go` — `mu`、`ReadAt`、`Seal`、`ResetView` |
| 暂停契约 | `internal/sandbox/rootfs/nbd.go` — `SealLayer`、`ResetView` |
| 请求解耦、无条件恢复 | `internal/checkpoint/service.go` — `context.WithoutCancel`；`internal/sandbox/checkpoint.go` — `pauseVM`、`CheckpointToFiles` 的 `defer` |
| 缺页预填 | `internal/sandbox/block/populate.go` — `populateMapped`、`probePopulate` |
| STW 取证 | `internal/runtimemetrics/runtimemetrics.go` |
