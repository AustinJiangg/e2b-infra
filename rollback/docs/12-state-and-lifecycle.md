# 12 · 状态、并发、持久性与生命周期

本章先说明状态归属、锁和请求生命周期，再推导 checkpoint 对活虚机、模板与宿主资源的依赖。生命周期边界与持久性取舍在同一处解释。

## 1. 三类状态

| 类别 | 具体是什么 | 谁保护 | 进程退出后 |
|---|---|---|---|
| **账本** | `Store` 里按沙箱分的表：条目 `bySandbox`、基准 `bases`、磁盘账本 `rootfs`、子节点计数 `children`、层引用 `layerRefs`、字节计数、合并（compact / fold）队列、代际归属 `owners` | `Store.mu`（全局互斥） | **全部消失**，从不从磁盘读回 |
| **活体对象** | `Overlay`、`Cache`、层栈、Firecracker 进程、NBD 设备 | 各自的锁 + **虚机暂停** | 随进程消失 |
| **文件产物** | 内存差分、位图侧车、snapfile、层文件与 `.meta`、header、manifest | 无锁；靠**按沙箱串行** + 原子提交 | 留在盘上，下次启动被清空 |

第三行是一个关键取舍：文件留着，但没有代码去加载它们（[第 12 章](12-state-and-lifecycle.md)）。所以「崩溃后的一致性恢复」这个问题在这里不存在
—— orchestrator 重启会带走它上面的所有沙箱，checkpoint 跟着一起走。

这让并发模型简单了很多：**不需要考虑崩溃后的一致性恢复**（没有日志、没有重放、没有启动时的对账），只需要保证进程活着的时候不出错。
后面几节的每一个设计，都可以用这句话来检验。

## 2. 锁的层级

### 2.1 `Store.mu`：保护账本，持锁不做 I/O

一把普通互斥锁，保护上面那些表，持有时间**极短** —— 只在读写 map 期间。**规则：持有 `Store.mu` 时不做文件 I/O、不调 Firecracker。**
这条规则在代码里有几个典型的落实方式：

**① 手工解锁，锁内只算路径。** `MaterializeRevert` 在锁内取目标、查断链、算祖先链与回滚路径；之后的读位图、写物化文件全在锁外
（`store.go:1515-1553`）：

```go
lockStart := time.Now()
s.mu.Lock()
timings.Mark("materialize_lock_wait", lockStart)

target, ok := entries[targetID]
if !ok || target.State != StateCommitted {
    s.mu.Unlock()                          // ← 每个 return 之前手工解锁
    return "", "", nil, fmt.Errorf(...)
}
// … 查断链、算 chain 与 pathEntries …
s.mu.Unlock()                              // ← 之后全是 I/O，不再持锁

live, err := readDirtyBitmap(liveBitmapPath)
// … 读位图、沿祖先链解析内容、写物化文件 …
```

写成 `defer s.mu.Unlock()` 会让整个物化过程的 I/O 都持着全局锁，阻塞这台机器上**所有**沙箱的账本操作 —— 包括另一个正在冻结窗口里等这把锁的 restore
（等待时间记在 `materialize_lock_wait`）。手工解锁更啰嗦、更容易漏，但这里的性质决定了值得：这是一个有意识的取舍，不是疏忽。

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
所以由子节点计数表 `children[sandbox][parentID]` 直接回答（`hasChildLocked`，`store.go:1912`），在条目进出账本处维护，O(1)。

**④ 发布是 O(1) 的。** `Commit` 在锁内只插入条目、移动基准、登记计数，不复制、不重写整份 index（index.json 默认不写，[第 4 章](04-architecture-and-flow.md)）；
`commit_index` 计的就是这段锁内工作。合并的文件工作同样全在锁外，只在挑候选和最终切换时各短暂持锁一次（[第 11 章](11-failures-and-invariants.md)）。

### 2.2 `opLocks[sandboxID]`：按沙箱串行

每个沙箱一把锁（容量为 1 的 channel，"持锁"就是 channel 里有一个值），checkpoint / restore / delete **全程持有**：

```go
func (s *Store) LockSandboxWithin(sandboxID string, wait time.Duration) (func(), bool) {
    gate := s.gate(sandboxID)      // ← gate() 内部短暂持 s.mu 取出（或创建）这把锁，返回前已放掉

    select {
    case gate <- struct{}{}:       // ← 空闲：立即拿到
        return func() { <-gate }, true
    default:
    }

    timer := time.NewTimer(wait)
    defer timer.Stop()

    select {
    case gate <- struct{}{}:       // ← 排队等到了
        return func() { <-gate }, true
    case <-timer.C:                // ← 等满 wait：放弃，回 busy
        return nil, false
    }
}
```

**注意顺序**：`gate()` 返回前已放掉 `Store.mu`，然后才去等沙箱锁。反过来（持着全局锁去等）会让一个进行中的 checkpoint 把全局账本锁一直占着。

排队有上限：持锁方可能卡在一个永不返回的调用上（NBD flush、不再应答的 Firecracker），无限排队只会让每个等待者各占一条连接、最后得到一个光秃秃的读超时。
所以应答客户端的路径都用限时版本 `LockSandboxWithin`，等满 `CHECKPOINT_LOCK_WAIT_TIMEOUT` 就回 busy（[第 24 章](24-configuration-and-capacity.md)、[第 22 章](22-api-semantics.md)）。
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
两代产物声称有同一个父亲、而它们的纪元位图覆盖了重叠的时间区间 —— [第 6 章](06-memory-diff-tree.md) 的那条不变量就破了。restore 同理：它最后把基准挪到目标，
中间并发的 checkpoint 会挂在错误的父亲上。合并也依赖这一点：它在 delete 里执行，持着沙箱锁，所以没有 restore 能与它交错。

## 3. Overlay：在 NBD dispatcher 底下换指针

`Overlay` 有自己的读写锁，保护 `device`（读路径的层栈）和 `cache`（写层）两个字段（`block/overlay.go:13-24`）。

```go
type Overlay struct {
    // mu guards device and cache, which Seal replaces underneath the NBD
    // dispatchers while the VM is paused. Reads and writes hold it shared;
    // their real synchronization is the caches' own locking.
    mu     sync.RWMutex
    device ReadonlyDevice   // 读路径：模板底座 + 层栈（LayerStack）
    cache  *Cache           // 写层
    ...
}
```

- **读写路径持共享锁，而且持满整个读或写**（`ReadAt`，`overlay.go:39`；`WriteAt`，`overlay.go:201`）。原因是 `Seal` 把旧写层**原地追加**进同一个层栈，这把锁挡住读路径去读一个正在更新的索引，
  并在 `Seal` 退出时把新条目发布给之后的所有读者。读者之间持的是共享锁，互不排斥。数据本身的并发由各 `Cache` 自己负责（块集合是原子位图，[第 7 章](07-disk-layering.md)）。
  也就是说 `Overlay.mu` 不负责数据竞争，它负责的是**替换与追加的原子性**：不能有人读到一半发现 `device` 换了而 `cache` 没换，或者读到一个追加了一半的层栈索引。
- **`Seal` 与 `ResetView` 持独占锁**，在锁内完成替换：

  ```go
  // Seal（overlay.go:128）
  o.mu.Lock()
  sealed := o.cache
  stack.AppendLayer(sealed)   // 旧写层原地追加进层栈，只登记它持有的块
  o.device = stack
  o.cache = newCache          // 换上新的空写层
  o.mu.Unlock()
  ```

  `ResetView`（`overlay.go:168`）一次换掉整个读路径和写层，旧写层连文件关闭，旧层栈只解除映射（文件归 store）。

NBD dispatcher 手里那个 `*Overlay` 指针**从头到尾没变过**，它不知道背后换了东西。

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

## 5. 原子提交与持久性

### 5.1 原子发布，刻意不 fsync

一个条目必须**要么完整可见，要么完全不存在**（[第 11 章](11-failures-and-invariants.md) 不变量 #7）。做法是 temp + rename，**刻意不 fsync**（`commitFiles`，`store.go:1037`）：

```go
for _, f := range files {
    // Deliberately no fsync before the rename. ...
    if err := os.Rename(f[0], f[1]); err != nil { ... }   // 原子改名
}
```

POSIX 保证同目录内的 rename 是原子的：要么旧名要么新名，不会有中间态。manifest 同理（`writeManifest`：写临时文件再 rename）。这就是原子发布的全部 —— 它不依赖落盘，因为之后的每一个读者（orchestrator 解析页、Firecracker 读回文件、层栈读封存层）
走的都是写入方用过的同一份 page cache。

| | 内容 | 依据 |
|---|---|---|
| **承诺** | 发布是原子的：`list` 看不到半成品，restore 拿不到半成品 | `commitFiles`、`writeManifest` 的 rename；`prepared` 条目不进账本（[§5.2](#52-两态可见性)） |
| **承诺** | checkpoint 返回成功后，同一宿主上立即可读、内容一致 | 读侧全部是普通缓冲读，无 `O_DIRECT` |
| **承诺** | e2b **原生 pause** 的 snapfile 仍然 fsync —— 那份文件要进持久化存储、活过本进程 | `firecracker/src/vmm/src/persist.rs` — `snapfile_must_be_durable`（判据：请求不带 `mem_file_path`） |
| **不承诺** | checkpoint 的任何文件落盘：snapfile、内存差分、侧车、回滚集、封存层、manifest 全程不 fsync | `commitFiles` 注释；`vstate/vm.rs` 写内存与位图只 `flush()`；`rootfs/nbd.go` — `SealLayer` 不做 msync |
| **不承诺** | checkpoint 活过 orchestrator 进程：账本只在进程内存里；`NewStore` 启动时先 `os.RemoveAll` 整个 store 根目录、再 `MkdirAll` 重建 | `store.go:535-542` |
| **不承诺** | checkpoint 活过宿主崩溃或掉电：目录里可能留下残缺文件，但没有任何东西会去读它，且下次启动即被清空 | 同上 |

**为什么不做 fsync 是对的**：fsync 能买到的只有「活过宿主崩溃」，而引用这些文件的账本本来就活不过进程；一致性不欠 flush 任何东西，因为读写走同一份 page cache。
而 fsync 的代价落在并发上：ext4 的日志提交是整个文件系统的串行点，多个沙箱同时 checkpoint 时每个 fsync 都要等别的沙箱的回写，暂停窗口内的那几次尤其贵。
将来若要做可持久化的 checkpoint，这些 fsync 需要按事务顺序（数据先于账本）整体加回来，并同时解决账本重建与沙箱重建（[第 12 章 §15](12-state-and-lifecycle.md#15-补齐需要什么)）。

对使用方的含义：checkpoint 是「这台宿主、这个 orchestrator 进程、这一代沙箱」之内的回滚点，**不是备份**。orchestrator 升级或重启、宿主重启之后，
之前的 checkpoint ID 全部失效（对它们 restore 得到 404）；要跨重启、跨宿主保存沙箱状态，用 e2b 原生的 pause / snapshot。
面向使用方的完整说明在 [第 22 章](22-api-semantics.md)。

### 5.2 两态可见性

条目有 `prepared` 与 `committed` 两态，但关键在于：**`prepared` 条目根本不在账本里**。`Prepare` 只建目录、写磁盘上的 manifest，不往 `bySandbox` 里放；
条目是在 `Commit` / `CommitHidden` 里通过 `publishLocked` 才进账本的。所以 `Get` 只需检查代际归属与是否隐藏（`store.go:1859`）——「不可见」不是靠过滤条件，而是**根本不存在**。

### 5.3 路径注入防护

条目 id 和沙箱 id 都会拼进文件路径，所以有一道校验：

```go
func validateID(id string) error {
    if id == "" { return fmt.Errorf("empty id") }
    if strings.ContainsAny(id, "/\\") || strings.Contains(id, "..") {
        return fmt.Errorf("id %q contains path separators", id)
    }
    return nil
}
```

在解析请求时（`checkpointIDFrom`）和使用时（`Prepare`、`LayersDir`、`RemoveSandbox`）**都做**；`RemoveSandbox` 那一处尤其重要，它要删一整个目录树。

## 6. 请求生命周期 ≠ 操作生命周期

checkpoint 和 restore 的第一件事都是：

```go
// The VM is paused partway through. A client that gives up on the request
// must not leave it that way, so the work is not tied to the request.
ctx := context.WithoutCancel(r.Context())
```

HTTP 请求上下文在客户端断开时立刻取消，操作若跟着取消，就会留下一个**暂停中、但所有人都以为在运行**的沙箱。
反过来也意味着：客户端超时并不会取消服务端正在做的 checkpoint 或 restore，调用方把超时调小只会丢掉结果、不会省下工作。

配套的是 `CheckpointToFiles` 里无条件的 `defer resume`，它对 `ResumeVM` 又套了一层 `WithoutCancel`：即使外层 ctx 因为别的原因被取消，恢复虚机也不能被跳过。
pause 本身超时的情形也照此处理：超时说明 pause 可能已经在另一侧生效，所以先补一次 resume 再报告失败（`pauseVM`，`internal/sandbox/checkpoint.go:78`）。

> 恢复虚机是全系统唯一一处「无论如何都要执行」的清理动作。例外只有撕裂：那时虚机刻意保持暂停（[第 11 章](11-failures-and-invariants.md)）。

## 7. 清理与幂等

restore 路径上有两个 `defer` 清理（`RollbackInPlace`）：

```go
liveBitmapPath := filepath.Join(filepath.Dir(snapfilePath), "live_bitmap.tmp")
// … 导出活跃位图 …
defer os.Remove(liveBitmapPath)

memfilePath, revertBitmapPath, cleanup, err := materialize(liveBitmapPath)
// …
defer cleanup()      // 删掉 revert_mem.tmp 与 revert_bitmap.tmp
```

`cleanup` 的注释写明它「无论回滚怎么结束都可以安全调用」—— 它就是两个 `os.Remove`，对不存在的文件返回的错误被忽略。
物化失败时 `MaterializeRevert` 自己会先调一次 `cleanup` 再返回错误，此时调用方拿不到 `cleanup`，不会重复调用；但这份幂等性依赖「`cleanup` 只做 `os.Remove`」，
往里加任何有副作用的动作之前要想清楚。

## 8. 节点级效应

同一节点上的沙箱在账本层面互不相干，但通过三样共享资源互相影响。它们不会破坏正确性，但会把一个沙箱的代价算到别的沙箱的冻结窗口里。
这三样的实测表现（并发 restore 下的锁等待、宿主表增长、GC 暂停）都在 [第 19 章](19-950-concurrency.md)。

**① 全局账本锁。** [§2.1](#21-storemu保护账本持锁不做-io) 的规则就是为此：任何在 `Store.mu` 下做的慢事，都会出现在别的沙箱 restore 的 `materialize_lock_wait` 里。

**② 宿主 conntrack 表。** 全节点一张，宿主侧清扫每批遍历整张表，表越大越贵；表有多大主要由宿主上与沙箱无关的流量决定（920B 长跑里约 95% 是非沙箱条目，[附录 C §8.6](C-evolution-and-cases.md#86-conntrack-清理占了冻结窗口)），restore 越频繁，排队越长。清扫器攒批与选路见 [第 10 章](10-device-network-time.md)。

**③ memory cgroup 与 Go GC 的长暂停。** 这是最隐蔽的一条：一个沙箱的 I/O 可以把另一个沙箱 restore 的冻结窗口拉长，而两者在账本上毫无关系。
它由三个事实串起来，缺一个都不会发生。

*事实一：orchestrator 与全部 Firecracker 同在一个 memory cgroup 里，而填满它的主要是 page cache。*
交付的部署里 orchestrator 由 Nomad job `template-manager-system` 拉起，落在这个 task 的 memory cgroup
（cgroup v1 下是 `memory:/nomad/<alloc-id>.start`，v2 下是 `nomad.slice/…/<alloc-id>.start.scope`），上限取自 `template-manager.hcl`
的 `resources.memory`（交付值 262144 MiB，即 256 GiB）。上游原本用 `CLONE_INTO_CGROUP` 把每个 Firecracker 放进 `/sys/fs/cgroup/e2b/sbx-<id>`，
但 ARM 移植补丁把这几行注释掉了（`internal/sandbox/fc/process.go:185-191`），Firecracker 作为子进程继承 orchestrator 的 cgroup，
`/sys/fs/cgroup/e2b/sbx-*` 目录存在但里面没有进程。所以这个 cgroup 里什么记账、什么不记账要分清：

- **记账**：orchestrator 自己的堆；Firecracker 进程 guest 以外的内存；checkpoint / restore 读写的文件（内存差分、层文件、物化文件）
  以及模板构建写的文件，都经 page cache 记在这个 cgroup 上；
- **不记账**：guest 内存。Firecracker ≥ 1.7 时沙箱以 2 MiB 大页启动（`packages/api/internal/sandbox/sandbox_features.go:45` `HasHugePages`；
  FC 侧 `machine_config.rs:65` 用 `MAP_HUGETLB | MAP_HUGE_2MB` 映射），hugetlb 页只记 hugetlb 控制器，memory 控制器不记
  （cgroup v2 以 `memory_hugetlb_accounting` 选项挂载时例外，该选项默认关）。

checkpoint / restore 越频繁，page cache 越多；负载一高这个 cgroup 就被填满，之后内核在 cgroup 内**持续回收**。
配额怎么取、改哪几个文件，见部署文档 [`12-orchestrator资源配额调优.md`](../../deploy-docs/12-orchestrator资源配额调优.md)。

*事实二：orchestrator 在用户态拷贝文件映射，缺页会睡在回收里，而这时它的 P 无法被抢占。*
orchestrator 的块设备层用 `MAP_SHARED` 文件映射读写层文件（[第 7 章](07-disk-layering.md)）。goroutine 在映射上做 `copy` 时，缺页发生在**用户态指令**上：
内核分配页 → 记账到 memcg → 超限触发回收 → 在回收里睡（`reclaim_throttle`，线程处于 D 态）。在 Go 运行时看来，这个 goroutine 正在执行普通的 Go 代码，
它的 P 处于 `_Prunning`；而 Go 的异步抢占靠给线程发信号，信号只有在线程回到用户态时才处理 —— 一个睡在内核缺页里的线程收不到它。

*事实三：Go 的 stop-the-world 必须等每个 P 都停下。*
GC 的 STW 阶段要等所有处于 `_Prunning` 的 P 响应抢占。只要有一个 goroutine 睡在回收里，STW 就只能等它醒来；
这期间**所有** goroutine 都停着，其中包括别的沙箱 restore 路径上正在冻结窗口里干活的那些。于是一次本该很短的 GC 暂停被拉长，
而且落在与这次缺页毫不相干的沙箱的冻结窗口里。

**判据**：用运行时指标把 STW 拆成两半 —— "等所有 P 停下"（`/sched/pauses/stopping/gc`）与"总暂停"（`/sched/pauses/total/gc`）。
若长暂停几乎全部花在前一半，而同期 orchestrator 有线程睡在 `reclaim_throttle`，就是这个机制；若花在后一半，才是收集器本身的工作量问题。
还有一个容易误判的地方：它不是"回收越重暂停越长"的线性关系，而是"只要有某个 goroutine 恰好在回收里睡着，STW 就会卡住"，
所以用回收强度去解释暂停长度是解释不通的。

**缓解：把缺页挪进系统调用**（`block/populate.go:105` `populateMapped`）。拷贝前，对页对齐的范围调用 `madvise(MADV_POPULATE_READ / MADV_POPULATE_WRITE)`，
每次最多 1 MiB（`populateChunk`，免得一个大拷贝长时间占着一次系统调用和 mmap_lock）。它在内核里做完同样的事 —— 分配页、memcg 记账与所需的回收、
写时的 `page_mkwrite` 与块分配 —— 但此时 goroutine 处于**系统调用中**，P 是 `_Psyscall`，STW 不等它（运行时可以直接把这个 P 收走）。
随后的拷贝发现页已映射，不再缺页。调用点是 `block/cache.go` 的读（`:285`）和写（`:449`）、`chunk.go:141`、`streaming_chunk.go:205`。

几个边界：

- 启动时在一个匿名页上探测一次，内核不支持（< 5.14，返回 `EINVAL`）则永久关闭、行为回到原样；
- `EFAULT` / `EHWPOISON`（直接访问会收到 `SIGBUS`，如产物盘写满）转成该次 I/O 的错误，与缺页保护 `guardMmapFault` 的语义一致（[第 11 章 §9](11-failures-and-invariants.md#9-不变量清单)）；
  其它错误一律回退为直接拷贝；
- 它是**优化不是保证**：已填充的干净页仍可能在拷贝前被回收，那时拷贝照旧缺页，缺页保护依然保留。

**减少垃圾**同样有帮助：GC 次数越少，撞上"某个 goroutine 恰好睡在回收里"的机会越少。index.json 默认不写、发布 O(1)、锁外删文件，都减少了分配与 GC 次数。

**取证**：`CHECKPOINT_RUNTIME_METRICS=true` 时 orchestrator 每 10 s 记一行 runtime/metrics 的 STW 直方图（等 P 停下的 `stopping`、总暂停 `total`、GC 周期数）
（`internal/runtimemetrics/runtimemetrics.go`），正是上面那条判据所需的两半。开关见 [第 24 章](24-configuration-and-capacity.md)，字段见 [第 25 章](25-observability.md)。

缓解前后长跑下的 STW 分布、长暂停次数与最大值，以及 cgroup 回收的取证数据，见 [附录 C §8.4](C-evolution-and-cases.md#84-gc-长暂停落进别的沙箱的冻结窗口)。

## 9. 单元测试守着哪些性质

`packages/orchestrator/internal/checkpoint/*_test.go` 里与本篇和 [第 11 章](11-failures-and-invariants.md) 对应的测试：

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

测试体系的全貌见 [第 16 章](16-test-methodology.md)。

## 10. 代码位置

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
| 共享 cgroup 的成因 | `internal/sandbox/fc/process.go`（`CLONE_INTO_CGROUP` 被注释掉处） |

## 11. 前提：产物在哪、什么时候消失

目录布局见 [第 4 章](04-architecture-and-flow.md)：每个沙箱一个目录，下面是各 checkpoint 的子目录（snapfile、内存差分、位图侧车、`rootfs.header`、manifest、timings）
和多代共享的 `layers/`（封存层文件与各自的 `.meta`）。

产物消失有三条代码路径：

| 触发 | 代码事实 |
|---|---|
| 沙箱从 orchestrator 的沙箱表中移除 | `Service.OnRemove`（`service.go:355`）取该沙箱的操作锁后调 `Store.RemoveSandbox`：锁内把整个沙箱目录改名为 `.trash-<uuid>` 并从账本摘掉全部状态，放锁后删除（`store.go:2153`、`:894`） |
| 同一个 id 的新一代接管 | `admitLocked` / `AdoptSandbox` 丢弃上一代留下的全部状态，同样先改名再删（`store.go:820`、`:2202`） |
| orchestrator 启动 | `NewStore` 先 `os.RemoveAll` 整个 store 根目录，再 `MkdirAll` 重建（`store.go:535-542`） |

### 11.1 代际边界的规则

`RemoveSandbox` 与原生 pause/resume 之间有一条规则，是理解整篇的钥匙：

- **「代」是 `Sandbox.LifecycleID`**，每换一个 Firecracker 进程就换一次。原生 resume 以**同一个沙箱 id** 现建一个新的 Firecracker 进程，所以是新的一代。
- 账本记录的是「**哪一代**拥有挂在这个 id 下的状态」（`store.go` 包注释、`owners`）：
  - 属主那一代继续用；
  - 本节点当前正在跑的新一代**接管**这个 id，并丢掉上一代的差分 —— 已经没有进程可以回滚到它们上面；
  - 其余的请求（排队期间被新一代超越的迟到请求）明确拒绝，不留半吊子状态。
- 读路径（`List`、`Get`、`Delete`）按同一个归属判定：新一代看不到上一代的条目，对旧 id 的 restore 得到 `not_found`；新一代可以照常从零开始 checkpoint，第一次是一棵新的全量根。
- 移除也按代：`OnRemove` 是异步触发、再等操作锁的，可能在 resume 已经把新一代放回来**之后**才到达。`RemoveSandbox` 发现 id 已被当前正在运行的那一代持有时什么也不做，
  **迟到的移除不会带走活沙箱的状态**。
- 撕裂标记同样按代：撕裂是关于一个 Firecracker 进程的事实，不是关于一个比它活得久的 id 的事实。

**为什么必须如此（推论）**：本方案的 restore 是原地回滚，要求那个活着的进程、那份活着的内存映射、那套活着的宿主资源还在（[§13.1](#131-恢复机制要求活体)）。
上一代的差分链没有可以写回去的对象；保留它们只会让使用方拿到一个必定失败、或者更糟、悄悄恢复到别的进程上的 id。所以账本按代记账，而不是按 id 记账。

`OnRemove` 先取操作锁、再删文件，这个顺序本身也有理由：它要删的正是一个进行中的 restore 正在读的文件（snapfile、各代差分、封存层）。
在一个已过提交点的回滚底下把它们抽走，会把一次 kill 变成一个撕裂的沙箱。等锁有上限，超时就告警后照删 —— 那时沙箱无论如何都要走了。

## 12. 一个思想实验：把目录完整拷走

假设在删除之前把沙箱目录整个打包保存，然后试图恢复。按实际代码路径逐步走。

### 12.1 第一步：orchestrator 不认识它

`NewStore` 启动时先清空 store 根目录再重建，账本的每张表都是空的，**不扫描磁盘、不读取任何 manifest**（`store.go:535`）：

```go
func NewStore(root string) (*Store, error) {
    if err := os.RemoveAll(root); err != nil { ... }       // ← 上一个进程留下的，没有任何东西再引用
    if err := os.MkdirAll(root, 0o755); err != nil { ... }
    // … 账本的各张表全部新建为空 …
}
```

拷回来的目录若在启动之前放好，启动时就被删掉；启动之后再放进去，`List` 返回空，`Get` 找不到条目 —— **在 API 层面这个 checkpoint 不存在**。
这是刻意的，包注释写明：manifest 与 index「exist so a run can be inspected after the fact」，store 从不读回，也都活不过重启。

### 12.2 第二步：即使账本能重建，也没有沙箱可回滚

假设补上加载逻辑（信息是够的，见 [§13.3](#133-账本不从磁盘加载)）。下一道门是 `ServeCheckpoint` 的第一行（`service.go:405`）：

```go
sbx, ok := s.sandboxes.Get(sandboxID)
if !ok {
    writeError(w, http.StatusNotFound, "not_found", reasonNotFound, fmt.Sprintf("sandbox %s not found", sandboxID))
    return
}
```

restore 的整个流程 —— 暂停虚机、导出活跃脏页位图、原地写回、切换磁盘视图 —— 每一步都作用在一个**活着的沙箱对象**上。没有它，流程无从启动。

### 12.3 第三步：换一个活沙箱顶上也不行

用同一个模板起一个新沙箱，把保存下来的 checkpoint 挂到它名下。至少三处失效：

| 失效点 | 说明 |
|---|---|
| **路径与归属** | 账本里记录的内存差分、snapfile、层文件都是**绝对路径**，指向原沙箱的目录；新沙箱是另一个 id、另一代，归属规则（[§11.1](#111-代际边界的规则)）也不认 |
| **磁盘底座** | 磁盘视图永远从模板 rootfs 起步再叠封存层（[§13.2](#132-磁盘产物不自足)），模板必须在位且是同一个 |
| **正确性前提** | 回滚集的推导依赖「当前基准与目标在**同一棵树**上」，或至少能用跨树规则界定差异。一个新起的沙箱的内存不是树上任何一个节点的状态 |

> **推论**：前两条是工程问题，改路径、对模板可以绕过；第三条是**语义问题**。即便凑巧算出一个覆盖足够的回滚集，那也是巧合而不是设计保证，
> 没有任何测试覆盖这个形态，不应依赖。

## 13. 四层依赖

### 13.1 恢复机制要求活体

**只有一条恢复路径**：`RollbackInPlace`，往活着的 Firecracker 对象上写（`store.go` 包注释："Restoring rolls the live Firecracker process back in place; there is no rebuild route"）。
"杀掉进程、从文件重建"那条路在设计上就没有做 —— 这是有意为之，不是遗漏：产品定位是活沙箱内的高频回退，进程已死、虚机撕裂的场景明确划给原生 snapshot
（设计目标见 [第 3 章](03-goals-and-boundaries.md)）。Firecracker 的 rollback 端点头两步就拒绝非活体：虚机必须处于 `Paused`，
`validate_topology` 要求快照拓扑与**运行中的虚机**一致（[第 9 章](09-in-place-rollback.md)）。回滚是把状态写到既有的 KVM fd、GIC 设备、virtio 对象上，对象不存在就没有可写的目标。

还有一处更本质。回滚集的定义是

```
revert = 树路径上各代的纪元位图并集  ∪  Firecracker 当前的活跃脏页
```

后一项描述的是「**当前**虚机相对上次快照改了什么」—— 这是**当前状态的属性，不是快照的属性**。没有当前状态，这一项无从谈起。

> **推论**：离线恢复本来也不需要它，直接全量加载即可。它不是障碍，而是「原地回滚」这个形态的必然产物 —— 恰恰说明这套机制是围绕活体设计的。

### 13.2 磁盘产物不自足

`AssembleView`（`internal/sandbox/checkpoint.go:341`）永远以模板 rootfs 为底，再把各封存层叠进层栈：

```go
base, err := s.Template.Rootfs()                   // ← 底座永远是模板
stack := block.NewLayerStack(base, size)
for _, layer := range layers {
    cache, _ := block.NewCache(size, blockSize, layer.Path, false)
    cache.MarkCached(layer.DirtyOffsets)
    stack.AppendLayerWithBlocks(cache, layer.DirtyOffsets)
}
```

封存层只含**被写过的块**，没被写过的块 —— 绝大多数 —— 要回模板取；`rootfs.header` 里的合并映射表，底座那些项也指向模板的 build。
所以**脱离模板，磁盘读不出完整内容**：产物不是一份完整磁盘镜像，而是「相对模板的增量」。

要做成可导出，需要把层栈压平、把模板底座物化进产物。合理的做法是一个独立的 export 接口，而不是改 checkpoint 的默认路径 ——
原地回滚场景下沙箱活着、模板必然在位，为一个不发生的场景让每次 checkpoint 付全量代价不划算。

### 13.3 账本不从磁盘加载

`NewStore` 不读盘（[§12.1](#121-第一步orchestrator-不认识它)）。有意思的是**磁盘上的信息其实基本够**：

| 文件 | 记录了什么 |
|---|---|
| `manifest.json` | 整个条目：`ParentID`、`MemMode`、`Rootfs` 视图、各文件路径、状态、是否隐藏 |
| 层的 `.meta` | 该封存层持有哪些块偏移 |
| `index.json` | 该沙箱的条目清单 —— **默认不写**，只在 `CHECKPOINT_DEBUG_INDEX` 打开时由后台写者至多每 5 s 重写一次 |

父指针在、模式在、层清单在 —— 同一个进程存活期间，理论上足以重建条目树（推论）。**缺的是读取它的代码**，以及那些只在内存里的派生状态
（子节点计数、层引用计数、字节计数、合并（compact / fold）队列、代际归属）的重建。要跨进程、跨崩溃则还缺一样：这些文件都不 fsync，启动时又整目录清空
（[第 12 章](12-state-and-lifecycle.md)），落盘的完整性本身不在承诺之内。

这是取舍而非疏忽：让账本跨重启存活，就要处理一整类新问题 ——

- 陈旧条目的回收：一台机器上积累了多少个已消失沙箱的目录？
- 与已消失沙箱的对账：沙箱没了但目录还在，谁负责清？
- 格式版本迁移：`manifest.json` 的结构变了怎么办？

而这些问题**只在「checkpoint 能脱离沙箱」的前提下才有意义**。前提不成立，问题就不存在 —— 这正是 [第 12 章 §1](12-state-and-lifecycle.md#1-三类状态) 说的「并发模型少了一整类问题」。

### 13.4 缺运行时环境

Firecracker 进程、KVM fd、eventfd、irqfd / ioeventfd 注册、tap、网络槽位、NBD 设备 —— 全是活体资源。**原地回滚的全部价值就在于一个都不重建**
（[第 9 章](09-in-place-rollback.md)），这正是它快的原因，也正是它离不开沙箱的原因。**同一件事的两面。**

## 14. 内存那一半其实已经自足

这一节值得单独说明，因为它决定了「补齐」的难度。

`CHECKPOINT_FULL_ROOT` 默认开启时，每棵树的根是一次全量捕获。内容解析沿祖先链回溯，遇到全量条目即终止，不会回落到模板 memfile；
全量条目的回滚因子恒为全 1，不依赖侧车内容（[第 6 章](06-memory-diff-tree.md)）。

全量根这个默认值本来是为别的目的定的：`fullRootEnabled` 的注释写明，关掉它会让树根变成相对启动内存源的差分，restore 时早于所有 checkpoint 的页
要从模板 memfile 解析，在集群部署下这意味着**回滚过程中去访问对象存储**。全量根用第一次 checkpoint 多写一份内存的代价，
把这个跨网络依赖从回滚路径上拿掉了。它带来的副作用是：

> **内存差分树在数据上已经是自包含的。** 四层缺口里内存这一层已经不缺，真正的数据缺口只有磁盘那一半。

把 `CHECKPOINT_FULL_ROOT` 关掉，这条不再成立 —— 树根变成相对启动内存源的差分，内存半边也失去自足性。

## 15. 补齐需要什么

| 缺口 | 补法 | 代价 |
|---|---|---|
| 内存全量镜像 | **已具备**。回滚数据的解析器稍加改造即可物化出任意一代的完整内存 | 小 |
| 磁盘完整镜像 | 层栈压平，把模板底座物化进产物 | 中；产物体积回到全量 |
| 账本 | `NewStore` 不再清空根目录，增加从 manifest 扫描重建（含派生计数）的路径 | 小到中；引入陈旧条目回收与格式迁移问题 |
| 落盘 | 把产物与 manifest 的 fsync 按事务顺序（数据先于账本）加回来 | 并发 checkpoint 的延迟回升：ext4 日志提交是全文件系统的串行点 |
| 恢复路径 | 一条 load-from-files：新起 Firecracker 进程 + 加载 snapfile + 挂内存与磁盘 | 中 |

**补齐之后得到的是什么**：全量产物、新建进程、跨节点可用、不依赖原沙箱 —— **这就是 e2b 原生 snapshot 的定义**。
沿着「让 checkpoint 能脱离沙箱」这条路走到底，终点是一个已经存在的东西，而沿途会逐项丢掉这套方案的收益：

| 补齐动作 | 丢掉的收益 |
|---|---|
| 磁盘层栈压平 | 零拷贝封存 —— 封存重新变成一次全量导出（[第 7 章](07-disk-layering.md)） |
| 产物自足 | 「成本只与改动量有关」不再成立（[第 6 章](06-memory-diff-tree.md)） |
| 从文件重建 | 原地回滚的低延迟 —— 回到进程重建 + 工作集换页（[第 9 章](09-in-place-rollback.md)） |

## 16. 这是取舍，不是缺陷

> **用「绑定沙箱生命周期」换掉产物自足性与账本持久化，换来零拷贝封存、原地回滚、以及只随回退跨度增长的成本曲线。**

两条路径解决不同的问题，并且**并存** —— 本方案没有改动原生路径的语义：原生 snapshot 的产物自足、进持久存储、独立于沙箱存活，靠新建进程加载，适合迁移、持久化与崩溃恢复；checkpoint 的产物是驻留宿主本地的增量、随 Firecracker 进程（一代）回收，在活体上原地回滚，适合高频快速回退。逐项对比表只在 [第 15 章 §3](15-native-cooperation-and-cost.md#3-逐项对比)。

两者怎么配合（先 checkpoint 回到已知良好点，再对该状态做原生 snapshot；checkpoint 之后再做原生 pause 的正确性前提）见
[第 15 章](15-native-cooperation-and-cost.md)；机制层面的正确性前提在 [第 15 章 §7](15-native-cooperation-and-cost.md#7-checkpoint-之后做原生-pause-的正确性前提)与 [第 14 章 §9](14-native-precise-increment.md#9-与-checkpoint-叠加累积位图)。

## 17. 推论：绑定生命周期之后，空间仍然要管

随沙箱回收解决了一半的空间问题：**沙箱一走，它占的盘就全回来了**，不需要为「已消失沙箱的遗留文件」设清理任务或过期策略 ——
`RemoveSandbox` 整树回收、`NewStore` 启动清空，就是全部的清理。

但另一半问题在沙箱**活着的期间**：checkpoint 不会自动过期，一个循环打点的客户端可以一直占用产物盘；而产物盘是整个节点共享的，写满的后果不是一次 checkpoint 失败，
而是节点上每个沙箱往稀疏映射里的写都可能失败。更隐蔽的是，调用方删掉的 checkpoint 并不一定释放空间 —— 仍被后代依赖的条目只会被隐藏，
「保留最新 N 个、删最旧」这种常见用法恰好每次都删掉下一个的父节点，隐藏条目与它们的层会一直累积。

所以现行实现有四层机制，各管一段（各自的开关、默认值与取值建议只在 [第 24 章 §2](24-configuration-and-capacity.md#2-开关总表) 与 [第 24 章 §4](24-configuration-and-capacity.md#4-容量规划) 定义）：

- **节点级水位**：产物盘可用空间不足时，checkpoint 与 restore 在动手之前被拒，把「节点写满」变成调用方能处理的应答；
- **每沙箱个数上限**：只数可见的 checkpoint；
- **每沙箱字节上限**：数实际占用的块，restore 不受限；
- **合并**（compact / fold）：把「隐藏、只有一个子节点」的条目并入子节点，使每沙箱条目数 ≤ 2V + 1；层按视图引用计数，最后一个引用它的视图消失时回收。

几条推论：

- **个数上限不足以控制字节**：隐藏条目不计入个数，而一个 checkpoint 的大小从几页到一整份 guest 内存不等。合并把隐藏条目的数量压到有界，字节上限才管得住总量。
- **字节上限有一个下限**：删到只剩基准时，基准是一棵约一份 guest 内存大小的全量根，上限再小就删什么都回不来。下限的推导与告警阈值见 [第 24 章 §4.4](24-configuration-and-capacity.md#44-字节上限怎么选)。
- **怎么删才释放空间**取决于合并能释放什么：删基准几乎不释放；删较老的非基准条目会触发合并，释放两者重叠的那部分页。
  给使用方的操作建议在 [第 22 章 §4.4](22-api-semantics.md#44-在字节上限下删哪个才能释放空间)。
- **脏页跟踪关闭时不合并**：每个 checkpoint 都是全量根，没有「只有一个子节点的隐藏条目」，层照样累积。这是设计边界，交付环境都开着跟踪。

开关的取值与容量规划见 [第 24 章](24-configuration-and-capacity.md)，删除与合并的机制见 [第 6 章](06-memory-diff-tree.md)、[第 11 章](11-failures-and-invariants.md)。

## 18. 代码位置

| 本篇提到的行为 | 位置 |
|---|---|
| 沙箱移除、代际接管 | `packages/orchestrator/internal/checkpoint/service.go` — `OnRemove`、`OnInsert`；`store.go` — `RemoveSandbox`、`AdoptSandbox`、`admitLocked`、`dropStateLocked` |
| 启动清空、不从磁盘加载 | `store.go` — `NewStore` 及包注释 |
| manifest / debug index | 同上 — `writeManifest`、`DebugIndex`、`runIndexWriter` |
| restore 第一步取沙箱 | `service.go` — `ServeCheckpoint` |
| 唯一的恢复路径 | `internal/sandbox/checkpoint.go` — `RollbackInPlace` |
| 磁盘视图从模板底座起步 | 同上 — `AssembleView` |
| 回滚集与内容解析、全量根 | `store.go` — `MaterializeRevert`、`entryBitmap`；`service.go` — `fullRootEnabled` |
| 空间管理 | `service.go` — `minFreeBytes`、`refuseIfDiskFull`、`warnIfByteLimitTooSmall`；`store.go` — `atCheckpointLimitLocked`；`quota.go`；`compact.go`；`layer_refs.go` |
