# 15 · 状态管理与并发

> 这套系统要在一台**正在运行的虚拟机**上换零件：换掉它的磁盘视图、改写它的内存、
> 替换它的写层文件 —— 同时还有别的沙箱在并发地做同样的事。
> 本篇讲状态怎么组织、锁怎么分层、以及「虚机暂停」这件事在并发模型里扮演什么角色。
>
> **读者**：工程师。
> **预备**：[第 13 篇 · 端到端](13-end-to-end.md)、[第 14 篇 · 失败语义](14-failure-semantics.md)。
> **代码**：`internal/checkpoint/store.go`、`internal/sandbox/block/overlay.go`、
> `internal/checkpoint/service.go`

---

## 0. 本篇要回答的问题

1. 系统里有哪几类状态？各自由什么保护？进程崩溃后各自会怎样？
2. 有几把锁？它们的层级关系是什么？为什么不会死锁？
3. 「虚机暂停」在并发模型里是什么角色？它替代了什么？
4. 一个条目怎么做到「要么完整可见，要么完全不存在」？
5. 客户端断开连接时，一个进行到一半的 checkpoint 会怎样？

---

## 1. 三类状态

| 类别 | 具体是什么 | 谁保护 | 进程崩溃后 |
|---|---|---|---|
| **账本** | `bySandbox`、`bases`、`rootfs` | `Store.mu`（全局互斥） | **全部消失**（磁盘上有镜像但不加载） |
| **活体对象** | `Overlay`、`Cache`、Firecracker 进程、NBD 设备 | 各自的锁 + **虚机暂停** | 随进程消失 |
| **文件产物** | `mem_diff`、`snapfile`、层文件、header | 无锁；靠**按沙箱串行** + 原子提交 | 留在盘上，但无人认识 |

第三行是这套设计的一个关键取舍：文件留着，但没有代码去加载它们
（[第 16 篇](16-lifecycle-and-portability.md)）。所以「崩溃恢复」这个问题在这里不存在 ——
orchestrator 重启会带走它上面的所有沙箱，checkpoint 跟着一起走。

这让并发模型简单了很多：**不需要考虑崩溃后的一致性恢复**，
只需要保证进程活着的时候不出错。

---

## 2. 两把锁，两个层级

### 2.1 `Store.mu`：保护账本

一把普通互斥锁，保护三张 map。持有时间**极短** —— 只在读写 map 期间：

```go
func (s *Store) BaseState(sandboxID string) (parentID string, broken bool) {
    s.mu.Lock()
    defer s.mu.Unlock()
    b := s.bases[sandboxID]
    return b.entryID, b.invalid
}
```

**规则：持有 `s.mu` 时不做 I/O、不调 Firecracker。** `MaterializeRevert` 是这条规则最明显的例子 ——
它需要在锁内读账本，然后在锁外做大量文件 I/O，所以是手工解锁而不是 `defer`：

```go
s.mu.Lock()
entries := s.bySandbox[sandboxID]
target, ok := entries[targetID]
if !ok || target.State != StateCommitted {
    s.mu.Unlock()                          // ← 每个 return 之前手工解锁
    return "", "", nil, fmt.Errorf(...)
}
// … 算出 chain 和 pathEntries …
s.mu.Unlock()                              // ← 之后全是 I/O，不再持锁

live, err := readDirtyBitmap(liveBitmapPath)
// … 读位图、写物化文件 …
```

写成 `defer s.mu.Unlock()` 会让整个物化过程（可能几十毫秒的 I/O）都持着全局锁，
阻塞这台机器上**所有**沙箱的账本操作。手工解锁更啰嗦、更容易漏，
但这里的性质决定了值得 —— 这是一个有意识的取舍，不是疏忽。

### 2.2 `opLocks[sandboxID]`：按沙箱串行

每个沙箱一把互斥锁，create / restore / delete **全程持有**：

```go
func (s *Store) LockSandbox(sandboxID string) func() {
    s.mu.Lock()
    l, ok := s.opLocks[sandboxID]
    if !ok {
        l = &sync.Mutex{}
        s.opLocks[sandboxID] = l
    }
    s.mu.Unlock()          // ← 先放掉全局锁

    l.Lock()               // ← 再获取沙箱锁（可能长时间等待）
    return l.Unlock
}
```

**注意解锁顺序**：先释放 `s.mu` 再获取 `l`。反过来（持着 `s.mu` 去抢 `l`）
会让一个进行中的 checkpoint 把全局账本锁一直占着。

锁的层级因此是明确的：

```
opLocks[sandbox]   ← 长时间持有（整个操作）
      ↓ 可以获取
Store.mu           ← 短时间持有（一次 map 读写）
```

**只有这一个方向**，所以不可能死锁。

### 2.3 为什么按沙箱而不是全局

因为要保护的是**每沙箱的状态**：内存基准（`bases[id]`）和磁盘层栈（`rootfs[id]`）。
两个操作同时推进同一个沙箱的基准，账本就会与实际的层栈 / 脏页跟踪状态错位。

不同沙箱之间没有共享状态，可以完全并行。

### 2.4 为什么全程持有

一次 create 的中间态是这样的：Firecracker 已经写完快照（纪元前进了），
但账本还没提交。此时如果另一个 create 进来，它读到的基准是**旧的**，
于是两代产物会声称有同一个父亲，而它们的纪元位图实际上覆盖了重叠的时间区间 ——
[第 8 篇 §2.2](08-memory-diff-tree.md#22-纪元epoch与它的位图) 那条不变量就破了。

restore 同理：它会在最后把基准挪到目标条目，中间任何并发的 create 都会挂在错误的父亲上。

---

## 3. 在 NBD dispatcher 底下换指针

`Overlay` 有自己的读写锁，保护 `device` 和 `cache` 两个字段：

```go
type Overlay struct {
    mu     sync.RWMutex   // guards device and cache, which Seal replaces
    device ReadonlyDevice //   underneath the NBD dispatchers while the VM is paused
    cache  *Cache
    ...
}
```

读写路径持**共享锁**，而且只持很短一瞬 —— 只为了把指针复制出来：

```go
func (o *Overlay) ReadAt(ctx context.Context, p []byte, off int64) (int, error) {
    o.mu.RLock()
    device, cache := o.device, o.cache   // ← 只是取指针
    o.mu.RUnlock()
    // … 之后用本地副本做实际的读，不再持锁
}
```

注释里点明了这一点：

> Reads and writes hold it shared; their real synchronization is the caches' own locking.

也就是说 `Overlay.mu` 不负责数据竞争 —— 那是 `Cache` 自己的事。
它只负责**指针替换的原子性**：不能有人读到一半发现 `device` 换了而 `cache` 没换。

`Seal` 和 `ResetView` 持独占锁，在锁内完成全部替换：

```go
o.mu.Lock()
sealed := o.cache
o.device = NewSealedView(o.device, sealed)
o.cache = newCache
o.mu.Unlock()
```

NBD dispatcher 手里那个 `*Overlay` 指针**从头到尾没变过**。它不知道背后换了东西。

---

## 4. 暂停窗口作为静默机制

上面的锁保证的是**数据结构的完整性**。但换视图还需要另一样东西：
**没有正在进行的 guest I/O**。

锁做不到这件事 —— 一个 guest 的写请求可能已经进了内核块层、还没到达 dispatcher。
所以有一条锁之外的前置条件：**虚机必须暂停，并且刷过屏障**。

| 操作 | 前置条件 | 谁保证 |
|---|---|---|
| `SealLayer` | 虚机暂停 + `BLKFLSBUF` | 调用方（`CheckpointToFiles`）+ 函数自己刷屏障 |
| `ResetView` | 同上 | 调用方（`RollbackInPlace`）+ 函数自己刷屏障 |
| `save-dirty-bitmap` | 虚机暂停 | Firecracker 端点自己检查 `VmState::Paused` |
| `rollback` | 虚机暂停 | 同上 |
| 清 conntrack | 虚机暂停（没有流量） | 调用方 |

**分工是清楚的**：

- **锁**处理「另一个 goroutine 在改同一个字段」；
- **暂停**处理「guest 还在产生新的 I/O」；
- **刷屏障**处理「内核手里还攥着已接受的写」。

三者缺一不可，而且**只有第一个能靠代码自己保证**。后两个是调用契约，
所以每个相关函数的文档注释都以「The VM must be paused」开头。

> 这是一个值得记住的模式：当外部世界（guest、内核）也在改状态时，
> 锁只能保护你自己的数据结构，剩下的要靠**让外部世界停下来**。

---

## 5. 原子提交

一个条目必须**要么完整可见，要么完全不存在**（不变量 #7）。做法是经典的
temp + fsync + rename：

```go
for _, f := range files {
    if err := syncFile(f[0]); err != nil { ... }      // ① 临时文件落盘
    if err := os.Rename(f[0], f[1]); err != nil { ... } // ② 原子改名
}
if err := syncDir(e.Dir); err != nil { ... }          // ③ 目录项落盘
```

三步各自的作用：

| 步 | 保证 |
|---|---|
| ① `fsync(临时文件)` | 内容真的在盘上，而不只是在 page cache |
| ② `rename` | POSIX 保证同目录内的 rename 是原子的：要么旧名要么新名，不会有中间态 |
| ③ `fsync(目录)` | 目录项本身也落盘，否则崩溃后可能看到旧的目录内容 |

不过第 ①③ 步在这套系统里其实**没有兑现价值** ——
账本不跨重启存活，所以「崩溃后磁盘上是什么」没人会去读
（对比[第 10 篇 §5](10-disk-layering.md#5-封存不等待落盘) 里刻意**不**做 msync 的封存层，
那里是把同样的逻辑贯彻到底了）。

保留它们的理由是：代价小（每次 checkpoint 几次 fsync，相对写快照可以忽略），
而且**万一将来要做账本持久化，这一半已经是对的**。这是一个可以接受的不对称。

### 5.1 两态可见性

```go
const (
    StatePrepared  = "prepared"
    StateCommitted = "committed"
)
```

`Get` 只返回 `committed` 且非隐藏的条目：

```go
func (s *Store) Get(sandboxID string, id string) (*Entry, bool) {
    ...
    e, ok := entries[id]
    if !ok || e.State != StateCommitted || e.Hidden { return nil, false }
    return e, true
}
```

关键在于 **`prepared` 条目根本不在 `bySandbox` 里** —— `Prepare` 只建目录、写磁盘 manifest，
不往账本里放。条目是在 `Commit` / `CommitHidden` 里通过 `publishLocked` 才进账本的。

所以「不可见」不是靠一个过滤条件，而是**根本不存在**。过滤条件是第二道防线。

### 5.2 路径注入防护

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

在解析请求时（`checkpointIDFrom`）和使用时（`LayersDir`、`RemoveSandbox`）**都做**。
`RemoveSandbox` 那一处尤其重要 —— 它要 `os.RemoveAll` 一整个目录。

---

## 6. 请求生命周期 ≠ 操作生命周期

create 和 restore 的第一件事都是：

```go
ctx := context.WithoutCancel(r.Context())
```

理由写在注释里：

> The VM is paused partway through. A client that gives up on the request
> must not leave it that way, so the work is not tied to the request.

HTTP 请求上下文在客户端断开时立刻取消。如果操作跟着取消，
就会留下一个**暂停中、但所有人都以为在运行**的沙箱。

配套的是那个无条件的 `defer`：

```go
defer func() {
    if err := timings.Timed("resume", func() error {
        return process.ResumeVM(context.WithoutCancel(ctx))   // ← 这里也要 WithoutCancel
    }); err != nil {
        e = errors.Join(e, fmt.Errorf("failed to resume VM after snapshot: %w", err))
    }
    timings.Mark("frozen", pausedAt)
}()
```

注意 `ResumeVM` 里又套了一层 `WithoutCancel`：即使外层 ctx 因为别的原因被取消，
恢复虚机这个动作也不能被跳过。

> **不变量 #14**：恢复虚机是无条件的。这是全系统唯一一处「无论如何都要执行」的清理动作。

---

## 7. 清理与幂等

restore 路径上有两个 `defer` 清理：

```go
liveBitmapPath := filepath.Join(filepath.Dir(snapfilePath), "live_bitmap.tmp")
// … 导出位图 …
defer os.Remove(liveBitmapPath)

memfilePath, revertBitmapPath, cleanup, err := materialize(liveBitmapPath)
// …
defer cleanup()      // ← 删掉 revert_mem.tmp 和 revert_bitmap.tmp
```

`cleanup` 由 `MaterializeRevert` 返回，注释明确说它「无论回滚怎么结束都可以安全调用」——
它就是两个 `os.Remove`，对不存在的文件返回错误但被忽略。

物化失败的路径上 `cleanup()` 会被调用**两次**（错误分支里一次，`defer` 一次）。
这没问题，因为它是幂等的 —— 但这依赖于「`cleanup` 只做 `os.Remove`」这个事实。
往里面加任何有副作用的动作之前要想清楚。

---

## 8. 单元测试守着哪些不变量

`internal/checkpoint` 下的测试大致对应本篇和[第 14 篇](14-failure-semantics.md)的不变量：

| 测试 | 守什么 |
|---|---|
| `TestPrepareIsInvisibleUntilCommit` | 不变量 #7：提交前不可见 |
| `TestCommitMovesFilesAndWritesManifest` | 原子提交的文件搬移 |
| `TestCommitAdvancesBaseAndParentsChain` | 基准推进与父指针 |
| `TestSetBaseToEntryBranchesTheTree` | 回滚后从目标分叉 |
| `TestCommitHiddenRescuesAdvancedEpoch` | 不变量 #8：纪元不能丢 |
| `TestInvalidateBaseBreaksChainUntilFullRoot` | 断链后拒绝恢复 |
| `TestDeleteHidesWhileReferencedAndCascades` | 依赖感知的删除 |
| `TestHiddenEntriesDropWhatCanNeverBeRead` | 隐藏时丢对东西 |
| `TestBaseMoveReclaimsUnreferencedHiddenEntries` | 基准移动触发回收 |
| `TestValidateIDRejectsTraversal` | 路径注入 |
| `TestMaterializeRevertTreePath` 等 | 回滚集与内容解析 |
| `header_agreement_test.go` | 合并 header 与层栈读的一致性（不变量 #9 的一半） |

**没有被测试覆盖的**（值得补）：

- 跨树回滚（基准与目标不在同一棵树上，[第 8 篇 §4.5](08-memory-diff-tree.md#45-最近公共祖先怎么求)）；
- 并发操作同一沙箱（依赖 `opLocks`，但没有针对性的竞态测试）；
- HDBSS 武装时序（不变量 #13，只靠调用点位置保证）。

> 单元测试只在 `infra-arm` 仓库里，**不进交付 patch**（rpmbuild 的 `%build` 只做 `go build`），
> 也未随上游 MR 合入（[第 27 篇 §1.1](27-extending.md#11-三个地方)）。

---

## 9. 小结

1. 三类状态：账本（进程内存）、活体对象、文件产物。**账本不跨重启存活**，
   这个取舍让并发模型少了一整类问题（崩溃一致性）。
2. 两把锁，**单向层级**：`opLocks[sandbox]`（长持有）→ `Store.mu`（短持有）。
   不可能死锁。`LockSandbox` 里先放全局锁再抢沙箱锁，顺序不能反。
3. **持 `Store.mu` 时不做 I/O**。`MaterializeRevert` 为此手工解锁而不是 `defer`。
4. `Overlay.mu` 只保护**指针替换的原子性**，数据竞争由 `Cache` 自己的锁负责。
   NBD dispatcher 手里的指针从头到尾没变过。
5. 并发安全靠三样东西分工：**锁**（自己的数据结构）、**暂停**（外部世界停下来）、
   **刷屏障**（内核手里的残留）。只有第一样能靠代码自保，后两样是调用契约。
6. 条目「要么完整可见、要么完全不存在」不是靠过滤，而是靠 `prepared` 条目
   **根本不在账本里**。
7. `context.WithoutCancel` + 无条件 `defer resume`：客户端放弃不能留下一台暂停的虚机。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 两把锁 | `internal/checkpoint/store.go` — `Store.mu`、`opLocks`、`LockSandbox` |
| 锁内不做 I/O 的例子 | 同上 — `MaterializeRevert` 的手工解锁 |
| 原子提交 | 同上 — `commitFiles`、`syncFile`、`syncDir` |
| 两态可见性 | 同上 — `Prepare`、`publishLocked`、`Get` |
| 路径校验 | 同上 — `validateID` |
| 指针替换 | `internal/sandbox/block/overlay.go` — `mu`、`Seal`、`ResetView` |
| 暂停契约 | `internal/sandbox/rootfs/nbd.go` — `SealLayer`、`ResetView` 的文档注释 |
| 请求解耦 | `internal/checkpoint/service.go` — `context.WithoutCancel` |
| 无条件恢复 | `internal/sandbox/checkpoint.go` — `CheckpointToFiles` 的 `defer` |
| 测试 | `internal/checkpoint/{store,bitmap,rootfs,header_agreement}_test.go` |

**下一篇**：[16 · 生命周期与可移植性边界](16-lifecycle-and-portability.md) ——
账本不跨重启存活这个取舍，往下推会得到什么结论。
