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
6. 两个调用方同时操作同一个沙箱（一个在跑流式命令、另一个发 restore）会怎样？

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

每个沙箱一把互斥锁（容量为 1 的 channel），checkpoint / restore / delete **全程持有**：

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
    case <-timer.C:                // ← 等满 wait：放弃
        return nil, false
    }
}
```

**注意顺序**：先释放 `s.mu` 再去等沙箱锁。反过来（持着 `s.mu` 去等）
会让一个进行中的 checkpoint 把全局账本锁一直占着。

**排队有上限**：持锁方可能卡在一个永不返回的调用上，无限排队只会让每个等待者各占一条连接、
最后得到一个光秃秃的读超时。所以应答客户端的三条路径都用限时版本，等满
`CHECKPOINT_LOCK_WAIT_TIMEOUT`（默认 60 s）就回 503 `busy`（[§9](#9-限额与超时四个服务端开关)）。
不限时的 `LockSandbox` 只留给不面向客户端的内部路径。

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

## 5. 原子提交与持久性

一个条目必须**要么完整可见，要么完全不存在**（不变量 #7）。做法是 temp + rename，**刻意不 fsync**：

```go
for _, f := range files {
    // Deliberately no fsync before the rename. ...
    if err := os.Rename(f[0], f[1]); err != nil { ... }   // 原子改名
}
```

POSIX 保证同目录内的 rename 是原子的：要么旧名要么新名，不会有中间态。manifest 的替换同理
（`writeManifest`：写 `.tmp` 再 rename）。这就是「原子发布」的全部 ——
它不依赖落盘，因为之后的每一个读者（orchestrator 解析页、Firecracker 读回文件）
走的都是写入方用过的同一份 page cache。

**checkpoint 的持久性：承诺什么、不承诺什么**

| | 内容 | 依据 |
|---|---|---|
| **承诺** | 发布是原子的：条目要么完整可见、要么不存在；`list` 看不到半成品，restore 拿不到半成品 | `store.go` — `commitFiles`、`writeManifest` 的 rename；`prepared` 条目不进账本（§5.1） |
| **承诺** | checkpoint 返回成功后，同一宿主上立即可读、内容一致（page cache 对同一宿主上所有进程一致，写返回即可见） | 读侧全部是普通缓冲读，无 `O_DIRECT` |
| **承诺** | e2b **原生 pause** 的 snapfile 仍然 `fsync`（上游行为不变）—— 那份文件要进持久化存储、活过本进程 | `firecracker/src/vmm/src/persist.rs` — `snapfile_must_be_durable`（判据：请求不带 `mem_file_path`） |
| **不承诺** | checkpoint 的任何文件落盘。snapfile、内存差分、位图侧车、回滚集、封存的磁盘层、manifest 全程不 `fsync` | `store.go` — `commitFiles` 注释；`firecracker/src/vmm/src/vstate/vm.rs` — `dump_memory_and_bitmap` 只 `flush()`；[第 10 篇 §5](10-disk-layering.md#5-封存不等待落盘) |
| **不承诺** | checkpoint 活过 orchestrator 进程。账本只在进程内存里、从不从磁盘读回；orchestrator 重启时所有沙箱随之消失；`NewStore` 启动时把 store 根目录（`<产物根>/checkpoints`）**整个清空**再重建 | `store.go` — 包注释、`NewStore` 的 `os.RemoveAll(root)` |
| **不承诺** | checkpoint 活过宿主崩溃或掉电。崩溃后目录里可能留下残缺文件，但重启后没有任何东西会去读它，且下次启动即被清空 | 同上 |

**对使用方的含义**：checkpoint 是「这台宿主、这个 orchestrator 进程、这一代沙箱」之内的回滚点，
不是备份。orchestrator 升级或重启、宿主重启之后，之前的 checkpoint ID 全部失效（对它们 restore 得到 404）。
要跨重启、跨宿主保存沙箱状态，用 e2b 原生的 pause / snapshot（[第 16 篇 §7.1](16-lifecycle-and-portability.md#71-要长期保存怎么办)）；
各种失效条件的完整列表在[第 16 篇 §1.3](16-lifecycle-and-portability.md#13-什么情况下-checkpoint-会失效)。

不做 `fsync` 换来的是并发下的 checkpoint 延迟：ext4 的日志提交是整个文件系统的一个串行点，
多个沙箱同时 checkpoint 时每个 `fsync` 都要等其他沙箱的回写；暂停窗口内的那几次尤其贵。
自 `04ef67baa`（orchestrator 侧）与 `0a2592d45`、`02491e116`（Firecracker 侧）起按上表执行。
如果将来要做可持久化的 checkpoint，这些 `fsync` 需要按事务顺序（数据先于账本）整体加回来，
并同时解决账本重建与沙箱重建（[第 16 篇 §5](16-lifecycle-and-portability.md#5-补齐需要什么)）。

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

## 8. 同沙箱多调用方：流式调用会被 restore 截断

前面几节讲的并发都在服务端内部。还有一类并发在服务端**之外**：两个调用方同时操作同一个沙箱 ——
一个在跑流式命令（SDK `commands.run` 带 `on_stdout`，走 envd 的流式 RPC），另一个发起 restore。

### 8.1 行为

restore 的第一件事就是断开该沙箱上**所有在途连接** —— 沙箱马上要回到过去，
在途响应的后半截已经没有意义。此时两类调用的下场不同：

| 调用形态 | restore 到达时 | 调用方看到 |
|---|---|---|
| unary | 响应头**还没**写出去 | 服务端改口为 409，SDK 映射成 `CheckpointInterruptedException(reason=sandbox_restored)` |
| 流式 | 响应头（200）**已经**写出去 | 只能从中间切断：`RemoteProtocolError: incomplete chunked read` / `unexpected EOF` / `ReadError` |

差别不在实现取舍，而在 HTTP 本身：**状态码只能在响应头里说一次**。
头一旦发出，服务端就再没有办法把这次调用改判成 checkpoint 语义的异常，
只剩下切断连接这一种表达方式 —— 于是调用方拿到的是**传输层错误**，与真的网络故障长得一模一样。

这是**确定性行为，不是偶发**：定向用例 T40 的 `--overlap` 组（流跑到一半时对同一沙箱再发一次 restore）
1200 条流全中。反过来，**restore 返回之后才发出的新请求不受影响** ——
同一套用例用 21120 条请求覆盖 restore 返回后 0–140 ms 的窗口，0 例被截；
清 conntrack、`ResetView`、设备静默都在恢复虚机与应答之前做完（§4）。

### 8.2 影响范围与当前处理

只有「多个调用方同时操作同一个沙箱」才会遇到；单调用方串行使用（命令跑完再 restore）不会。

作为**既定限制**记录：调用方应当把「流式调用期间的传输层错误 + 随后确认确实发生过一次 restore」
当作被回滚打断，按需重发。

取证入口：orchestrator 的环境变量 `PROXY_TRACE=1` 打开代理的连接级日志，
被 restore 打掉的连接带 `drop_reason=restore` 与 `aborted_after_headers=true`
（[第 31 篇 §4](31-glossary-and-code-map.md#4-环境变量总表)）。

### 8.3 预留的改进方案（未实现）

让客户端自己分型：服务端为每个沙箱维护一个 **restore 世代号**，经 checkpoint 服务接口暴露；
SDK 在流式调用开始时记下世代号，流中途遇到传输层错误时回查一次 —— 世代号变了就改抛
`CheckpointInterruptedException(reason=sandbox_restored)`，没变就原样抛出。

代价是 orchestrator 与 SDK 各一处小改动，只有异常路径多一次查询，旧 SDK 不受影响。
**决定（2026-09-20）：暂不实现**，等使用方提出需求再做。

---

## 9. 限额与超时：四个服务端开关

四个环境变量，都由 orchestrator 进程读取，都可以不设。生效值与来源（`env` / `default`）
在启动时的能力行里打印（[第 17 篇 §2.2](17-observability-and-verification.md#22-启动时的能力上报)），
写错的值**不会**让服务不带保护地运行：解析不了就回落默认值并打一条 WARN。

| 变量 | 默认 | 取值 | 关闭方式 | 触发时的行为 |
|---|---|---|---|---|
| `CHECKPOINT_MIN_FREE_BYTES` | max(4 GiB, 2 × 该沙箱 guest 内存) | 非负整数，单位字节 | `0` | checkpoint 与 restore 在动手**之前**检查 store 所在文件系统的可用空间（`statfs` 的 `Bavail`），不足则 **507** `resource_exhausted` / `disk_full`。沙箱与已有 checkpoint 不受影响 |
| `CHECKPOINT_MAX_PER_SANDBOX` | `0`（不限） | 非负整数，单位个 | `0` | 单个沙箱**可见** checkpoint 数达到上限后，新的 checkpoint 被拒：**429** `resource_exhausted` / `too_many_checkpoints`。隐藏条目不计数（调用方看不见也删不掉）。`delete` 之后即可再打 |
| `CHECKPOINT_LOCK_WAIT_TIMEOUT` | `60s` | Go duration（`90s`、`2m`），须 > 0 | 不可关闭 | checkpoint / restore / delete 在同一沙箱的操作锁上排队超过此值：**503** `unavailable` / `busy`，带 `Retry-After: 1`。本次调用什么都没做，可重试 |
| `CHECKPOINT_FC_CALL_TIMEOUT` | `2m` | Go duration，须 > 0 | 不可关闭 | 单次 Firecracker API 调用（pause / snapshot / rollback / resume）的时限。pause 超时：补一次 resume 后按普通失败返回（500 `internal`）；**rollback 超时：无法判断虚机停在哪个时刻，按撕裂处理**（500 `data_loss` / `torn`） |

各拒绝对应的 SDK 异常与调用方动作见[第 14 篇 §10](14-failure-semantics.md#10-错误契约)。

**为什么磁盘余量默认是 2 × guest 内存**：起树根的 checkpoint 要写下整份 guest 内存，
restore 物化的回滚集也可能一样大，所以单次操作最多就是一份 guest 内存；
两份是给「紧接着的下一次」和沙箱自己的磁盘写留的 —— 它们落在同一个文件系统上。
guest 内存小于 2 GiB 时取 4 GiB 下限，因为磁盘层和沙箱自身的写入不随内存变小。

**产物盘写满的后果不是「这一次 checkpoint 失败」**。Firecracker 写快照遇到 `ENOSPC` 只是一次失败的调用；
但同一时刻，节点上**每一个**沙箱往稀疏映射里的写入都会因为内核无法分配后备块而出错，
影响的是整个节点。提前拒绝把它变成一个调用方能处理的应答。
所以容量规划上：产物盘除了模板与沙箱自身写入之外，应至少常留「最大 guest 内存 × 2」的余量；
多租户节点建议同时设 `CHECKPOINT_MAX_PER_SANDBOX`，
因为 checkpoint 不会自动过期（[第 16 篇 §1.2](16-lifecycle-and-portability.md#12-什么时候被删)），
一个循环打点的客户端可以无上限地占用产物盘。

**两个超时与 SDK 超时的配合**：SDK 对 checkpoint / restore / delete 的默认请求超时是 300 s
（[第 31 篇 §5.1](31-glossary-and-code-map.md#51-sdkpython)），
大于 `CHECKPOINT_LOCK_WAIT_TIMEOUT` 与 45 s 的 envd 等待之和，
目的是让调用方总能收到服务端给出的结论，而不是自己先超时。
把 `CHECKPOINT_LOCK_WAIT_TIMEOUT` 调大时，客户端超时要跟着调大。

---

## 10. 单元测试守着哪些不变量

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

> 单元测试与被测代码同目录（`packages/orchestrator/internal/checkpoint/*_test.go`），
> 限额与拒绝行为的用例在 `limits_test.go`、`service_test.go`；
> rpm 构建的 `%build` 只做 `go build`，不运行它们（[第 30 篇 §1.1](30-extending.md#11-代码仓库)）。

---

## 11. 小结

1. 三类状态：账本（进程内存）、活体对象、文件产物。**账本不跨重启存活**，
   这个取舍让并发模型少了一整类问题（崩溃一致性）。
2. 两把锁，**单向层级**：`opLocks[sandbox]`（长持有）→ `Store.mu`（短持有）。
   不可能死锁。先放全局锁再等沙箱锁，顺序不能反；排队有上限，超时回 `busy`（§9）。
3. **持 `Store.mu` 时不做 I/O**。`MaterializeRevert` 为此手工解锁而不是 `defer`。
4. `Overlay.mu` 只保护**指针替换的原子性**，数据竞争由 `Cache` 自己的锁负责。
   NBD dispatcher 手里的指针从头到尾没变过。
5. 并发安全靠三样东西分工：**锁**（自己的数据结构）、**暂停**（外部世界停下来）、
   **刷屏障**（内核手里的残留）。只有第一样能靠代码自保，后两样是调用契约。
6. 条目「要么完整可见、要么完全不存在」不是靠过滤，而是靠 `prepared` 条目
   **根本不在账本里**。
7. `context.WithoutCancel` + 无条件 `defer resume`：客户端放弃不能留下一台暂停的虚机。
8. 服务端之外还有一类并发：**同沙箱多调用方**。已经开始回数据的流式调用撞上 restore
   **必然**被截断成传输层错误 —— 响应头已发出，状态码改不了。unary 还来得及改判成 409。
   这是既定限制（§8）。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 两把锁 | `internal/checkpoint/store.go` — `Store.mu`、`opLocks`、`gate`、`LockSandboxWithin`、`LockSandbox` |
| 锁内不做 I/O 的例子 | 同上 — `MaterializeRevert` 的手工解锁 |
| 原子提交与持久性 | 同上 — `commitFiles`、`writeManifest`、`NewStore`；`firecracker/src/vmm/src/persist.rs` — `snapfile_must_be_durable` |
| 限额与超时 | `internal/checkpoint/service.go` — `minFreeBytes`、`refuseIfDiskFull`、`refuseTooManyCheckpoints`、`lockWaitTimeout`、`writeBusy`；`store.go` — `MaxPerSandbox`、`atCheckpointLimitLocked`；`internal/sandbox/checkpoint.go` — `fcCall`、`FCCallTimeout` |
| 两态可见性 | 同上 — `Prepare`、`publishLocked`、`Get` |
| 路径校验 | 同上 — `validateID` |
| 指针替换 | `internal/sandbox/block/overlay.go` — `mu`、`Seal`、`ResetView` |
| 暂停契约 | `internal/sandbox/rootfs/nbd.go` — `SealLayer`、`ResetView` 的文档注释 |
| 请求解耦 | `internal/checkpoint/service.go` — `context.WithoutCancel` |
| 无条件恢复 | `internal/sandbox/checkpoint.go` — `CheckpointToFiles` 的 `defer` |
| 断开在途连接 | `internal/checkpoint/service.go` — `beginRestore` 的 `dropConnections`；连接池的 `resetAllConnections` |
| 改判 409 的那半条路 | 同上 — `onTransportError` → `RestoredAnswer`（只在响应头写出前有效） |
| 测试 | `internal/checkpoint/{store,bitmap,rootfs,header_agreement}_test.go` |

**下一篇**：[16 · 生命周期与可移植性边界](16-lifecycle-and-portability.md) ——
账本不跨重启存活这个取舍，往下推会得到什么结论。
