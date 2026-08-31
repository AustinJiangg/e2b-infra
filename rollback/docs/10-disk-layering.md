# 10 · 磁盘分层与零拷贝封存

> 内存那一半可以「只存脏页」，磁盘这一半怎么办？答案是：**一个字节都不搬。**
> guest 写进去的那个文件，直接改个名就成了只读层。本篇讲这是怎么做到的，以及它换来了什么、
> 让掉了什么。
>
> **读者**：工程师。
> **预备**：[第 6 篇 · 总体架构](06-architecture.md)。知道 mmap、稀疏文件、
> 写时复制（COW）大概是什么。
> **代码**：`internal/sandbox/block/{overlay,cache,sealed_view}.go`、
> `internal/sandbox/rootfs/nbd.go`、`internal/checkpoint/rootfs.go`

---

## 0. 本篇要回答的问题

1. 一个 e2b 沙箱的磁盘是怎么拼出来的？模板、写层、NBD 各是什么角色？
2. e2b 原生导出磁盘增量为什么**必须停沙箱**？
3. 「零拷贝封存」到底零在哪？四个步骤各自不可省的理由是什么？
4. 为什么封存之后**不等数据落盘**也是安全的？
5. 层叠了几十层之后，读会不会变慢？恢复会不会变慢？

---

## 1. 沙箱的磁盘长什么样

### 1.1 三个部件

```
        guest 看到的 /dev/vda
                 │
                 ▼
    ┌────────────────────────────┐
    │   NBD 设备  /dev/nbdX       │   内核块设备
    └────────────┬───────────────┘
                 │  dispatcher（用户态）
                 ▼
    ┌────────────────────────────┐
    │        Overlay             │   Go 对象：读合并、写落上层
    │  ┌──────────────────────┐  │
    │  │  Cache（活写层）      │  │   稀疏文件 + mmap + dirty 集合
    │  └──────────────────────┘  │
    │  ┌──────────────────────┐  │
    │  │  ReadonlyDevice      │  │   模板 rootfs（只读）
    │  └──────────────────────┘  │
    └────────────────────────────┘
```

| 部件 | 是什么 |
|---|---|
| **模板 rootfs** | 只读。**不在本地** —— 由 chunker 按块从对象存储按需拉取，本地只缓存拉过的块 |
| **Cache（写层）** | 一个与设备等大的**稀疏文件**，`mmap` 到进程地址空间，外加一张 `dirty` 表记录哪些块写过 |
| **Overlay** | 读：先查写层，未命中再问下面；写：全部落写层，并在 `dirty` 里登记 |
| **NBD** | 把这个 Go 对象暴露成一个内核块设备，guest 通过 virtio-blk 访问它 |

写层是**稀疏**的：创建时 `truncate` 到设备大小，物理上一个块都不占；
guest 写哪块，哪块才落盘。所以一个刚起来的沙箱，写层文件逻辑上几十 GiB、物理上接近 0。

### 1.2 `dirty` 表就是这一层的「位图」

`Cache.dirty` 是一张 `sync.Map`，键是块偏移。它的作用和内存那边的纪元位图完全对应：
**记录这一层持有哪些块**。`DirtyOffsets()` 把它排序后返回，是后面所有账本的原料。

---

## 2. 原生怎么导出磁盘增量

e2b 原生的路径是 `ExportDiff`：

```go
cache, err := o.overlay.EjectCache()     // ① 把写层摘出来，Overlay 就此作废
...                                      // ② 停沙箱，等 NBD 设备释放
builder := header.NewDiffMetadataBuilder(c.size, c.blockSize)
for _, offset := range c.dirtySortedKeys() {
    block := (*c.mmap)[offset : offset+c.blockSize]
    builder.Process(ctx, block, out, offset)   // ③ 全零块只记 empty 位；非零块紧凑追加
}
return builder.Build()                   // ④ 关闭并删除写层文件
```

产物是一个**紧凑**的 diff 文件：块在文件里按顺序排列，与它在设备上的位置无关。
所以要一张映射表把 device offset 翻译成 storage offset。

### 2.1 为什么必须停沙箱

两处：

- `EjectCache()` 用一个 `cacheEjected` 标志把 Overlay 置为作废状态。**之后任何写都没有去处** ——
  Overlay 的写路径就是往那个 cache 写。
- `mmap` 上的遍历必须在没有并发写的前提下进行，否则导出的是一个「一半新一半旧」的镜像。

也就是说，`ExportDiff` 的语义是**沙箱生命的终点**：导完了，这个 Overlay 就不能再用了。
这对「沙箱离场后再回来」的场景完全合适 —— 反正沙箱也要停。

对高频 checkpoint 则完全不能接受。

---

## 3. Seal：不搬运任何数据的换层

`SealLayer` 在同一个 `Overlay` 上另开一条路径。四步：

```go
// ① 刷屏障：把内核块层已接受的写推进写层
if err := o.sync(ctx); err != nil { ... }        // ioctl(BLKFLSBUF) + flush

// ② 新建一个空写层：open + truncate（稀疏）+ mmap，零数据写入
newCache, err := block.NewCache(size, o.blockSize, newCachePath, false)

// ③ 两次指针替换
sealed, err := o.overlay.Seal(newCache)

// ④ 改名入库
if err := sealed.MoveFile(sealedLayerPath); err != nil { ... }
```

**没有任何一步在搬运数据。**

### 3.1 第一步：刷屏障保证什么、不保证什么

```go
unix.IoctlSetInt(int(file.Fd()), unix.BLKFLSBUF, 0)
```

`BLKFLSBUF` 让内核把这个块设备上缓冲的写全部推下去，经 NBD dispatcher 落进写层的 mmap。

它**保证**：内核块层已经接受的每一个写，都到了写层里。
它**不保证**：guest 停止产生新的写。

所以调用契约里明确写着：**虚机必须已经暂停**。刷屏障处理的是「内核手里还攥着的」，
暂停处理的是「guest 还要写的」。两者缺一不可。

> 这也是为什么 `SealLayer` 和 `ResetView` 的文档注释都以「The VM must be paused」开头 ——
> 它是一个前置条件，不是这个函数能自己保证的事。

### 3.2 第三步：两次指针替换

```go
func (o *Overlay) Seal(newCache *Cache) (*Cache, error) {
    o.mu.Lock()
    defer o.mu.Unlock()

    sealed := o.cache
    o.device = NewSealedView(o.device, sealed)   // 旧写层折进读路径
    o.cache = newCache                           // 新写层上位
    return sealed, nil
}
```

旧写层没有被丢弃 —— 它被包装成一个 `SealedView`，叠在原来的读路径之上。
运行中的沙箱继续能读到自己封存前写的每一个块，只是现在是**从读路径**读到的。

这就是全部。`o.mu` 是一把读写锁，读写路径持共享锁，`Seal` 持独占锁 ——
换指针发生在 NBD dispatcher **底下**，它手里那个 `*Overlay` 指针从头到尾没变过。

> **类比**：OverlayFS 把 upper 降级成 lower，再开一个新 upper，**挂载全程不卸载**。
> 差别只是这里发生在宿主的块层，而不是 guest 的文件系统层。

### 3.3 第四步：改名，不是拷贝

```go
func (c *Cache) MoveFile(newPath string) error
```

同一个文件系统内，这是一次 `rename`：**inode 不变**，所以那个还活着的 `mmap` 继续有效 ——
它映射的是 inode，不是路径。

跨文件系统时 `rename` 会失败（`EXDEV`），代码退回**保洞的稀疏拷贝**，然后 unlink 原文件。
这看起来危险（我们刚说不搬数据），但它安全，理由很具体：

> 活着的 mmap 继续从那个**已被 unlink 的 inode** 提供读服务。这是正确的，
> 恰恰因为这只发生在**已封存**的层上 —— 封存内容再也不会改变，
> 所以映射看到的和 store 里那份拷贝永远一致。
> 等映射消失时，被 unlink 的 inode 的块才被真正释放。

也就是说：跨文件系统的路径在**正确性**上没问题，只是**代价**退化成一次拷贝。
把 store 和沙箱缓存放在同一个文件系统上，这条路径就不会走到。

### 3.4 对比

| | 原生 `ExportDiff` | `SealLayer` |
|---|---|---|
| 数据搬运 | 逐块从 mmap 读出、写进输出流 | **无** |
| 沙箱 | 必须停 | 暂停即可，之后继续跑 |
| Overlay | 作废 | 继续用，只是换了一层 |
| 产物布局 | 紧凑，storage offset ≠ device offset | 原样，storage offset == device offset |
| 全零块 | 剔除（只记 empty 位） | 保留，按块占盘 |
| 之后还能写吗 | 不能 | 能，写进新写层 |

---

## 4. 恒等映射

### 4.1 不紧凑化带来的简化

因为层文件就是原来那个写层文件、块还在原来的偏移上，映射关系是**恒等**的：

```
device offset 0x1000  →  layer 文件 offset 0x1000
```

`identityMappings` 把连续的脏块合成一个 run，每个 run 一条映射：

```go
mappings = append(mappings, &header.BuildMap{
    Offset:             uint64(runStart),
    Length:             uint64(runLength),
    BuildId:            layerID,
    BuildStorageOffset: uint64(runStart),   // ← 与 Offset 相同
})
```

比原生少一步偏移换算。代价是**不剔除全零块**：guest 写了一整块零，
紧凑 diff 会把它记成一个 empty 位（0 字节），层文件则实打实占一个块。

对高频 checkpoint 这个取舍是划算的：省下的是每次封存的一整趟 I/O，
多花的是磁盘空间，而且写层本来就是稀疏的、只有真写过的块才占盘。

### 4.2 合并 header

每打一个 checkpoint，`AppendLayer` 把新层的映射折进沙箱当前的合并 header：

```go
layerMaps := identityMappings(layerID, layer.DirtyOffsets, layer.BlockSize)
merged    := header.NormalizeMappings(header.MergeMappings(st.mapping, layerMaps))
if err := header.ValidateMappings(merged, st.meta.Size, st.meta.BlockSize); err != nil { ... }
```

`MergeMappings` 的语义是「上层覆盖下层」，`NormalizeMappings` 合并相邻同源区间，
`ValidateMappings` 检查结果**无缝无叠、恰好覆盖整个设备**。

结果序列化成 `rootfs.header`，随该 checkpoint 一起落盘。它描述的是
**「模板 rootfs 的映射 + 到此为止每一层的恒等映射」** —— 也就是那一时刻的完整磁盘视图。

### 4.3 层侧车

每个层文件旁边写一个 `.meta`：8 字节小端一条，记录这一层持有哪些块偏移。

它的作用只有一个：**恢复时重新打开这个层文件，要恢复「它持有哪些块」这份知识**。
文件里有数据，但文件系统不会告诉你哪些块是「这一层写的」、哪些是稀疏空洞
（空洞读出来是零，而零也可能是 guest 真写的内容）。

---

## 5. 封存不等待落盘

`SealLayer` 里有一段很长的注释，说的是一件被**刻意不做**的事：

```go
// Deliberately no synchronous flush of the sealed layer. Its dirty pages
// sit in the host page cache, and every later reader -- the running
// sandbox through its SealedView, a restore reopening the file -- reads
// through that same cache, so consistency owes nothing to a flush. What
// an msync would buy is durability across a host crash, and these
// checkpoints do not reach that far: the store's index lives in this
// process's memory and dies with it. The wait cost 0.49 ms per dirty MB
// (measured) and was the whole reason sealing scaled with the delta;
// the kernel writes the pages back on its own schedule anyway.
```

拆成两个论证：

### 5.1 一致性：不欠这个 flush

封存层的脏页留在宿主的 page cache 里。此后所有读者：

- 还在运行的沙箱，通过 `SealedView` 读同一个 mmap；
- 一次 restore，用 `NewCache` 重新打开同一个文件。

**都读同一份 page cache。** 页在内存里还是在盘上，对读者完全透明。
`msync` 买不到任何一致性 —— 它买的是别的东西。

### 5.2 持久性：本来就够不着

`msync` 买的是**宿主崩溃后的持久性**。但这套 checkpoint 的语义根本达不到那么远：
账本（`bySandbox`、`bases`、`rootfs`）全在 orchestrator 的进程内存里，
进程一死全部消失，剩下一堆没人认识的文件（[第 16 篇](16-lifecycle-and-portability.md)）。

为一个**不可能被兑现**的保证付每次封存的代价，不划算。

### 5.3 代价

实测 **0.49 ms / 脏 MB**。这是封存耗时随改动量线性增长的**唯一**来源 ——
去掉之后，封存基本是个常数（几次 ioctl + 几次指针写 + 一次 rename）。

对一个改了 256 MiB 的 checkpoint，这一步原本要 125 ms，比整个回滚还长。

> 换个角度看这条优化：它把磁盘那一半从「与改动量成正比」变成了「与改动量无关」。
> 内存那一半天然是 O(脏页)，磁盘这一半现在是 O(1)。

---

## 6. 读路径：SealedView 链

### 6.1 运行中的沙箱

封存 k 次，读路径上就有 k 个 `SealedView`：

```
Overlay.cache（活写层）
   ↓ 未命中
SealedView_k → SealedView_{k-1} → … → SealedView_1
   ↓ 都未命中
模板 rootfs
```

每一层的 `ReadAt` 先问自己的 cache，`BytesNotAvailableError` 就往下传。
**最坏情况一次读要走 k+2 层。** 这是链式结构的固有代价。

实践中影响有限：每一层的「问自己」只是一次 `sync.Map` 查找 + 可能的 mmap 读，
没有系统调用；而绝大多数块要么在最上面几层（刚写的），要么一路落到模板（从没写过）。

但**这确实是链深唯一会影响性能的地方**，值得知道。

### 6.2 恢复不走链

恢复时**不用**这条链。`assembleView` 按目标 checkpoint 自己的层清单，从模板开始重新叠：

```go
var device block.ReadonlyDevice = base
for _, layer := range layers {
    cache, err := block.NewCache(size, blockSize, layer.Path, false)
    cache.MarkCached(layer.DirtyOffsets)          // ← 侧车提供的知识
    device = block.NewSealedView(device, cache)
}
```

注意 `MarkCached`：文件提供数据，侧车提供「哪些块是这一层的」。两者合起来才是一个可用的层。

> 这里叠出来的仍然是一条链，所以严格说恢复后的**读**路径也是 k 层深。
> 但**恢复动作本身**（组装视图 + 换指针）与层数只是线性关系的 k 次 `NewCache`，
> 没有任何数据搬运。相比之下原生恢复要重建整个 Overlay 并靠缺页把工作集换回来。

---

## 7. ResetView：挂载不动，整体换视图

回滚时磁盘侧要换掉的是**整个视图** —— 读路径和写层一起换。

```go
func (o *Overlay) ResetView(device ReadonlyDevice, cache *Cache) error {
    o.mu.Lock()
    oldDevice, oldCache := o.device, o.cache
    o.device = device      // ← 目标 checkpoint 的层栈
    o.cache = cache        // ← 全新的空写层
    o.mu.Unlock()

    var errs []error
    errs = append(errs, oldCache.Close())              // 被丢弃时间线的写层：连文件一起删
    if view, ok := oldDevice.(*SealedView); ok {
        errs = append(errs, view.Close())              // 旧封存层：只解映射，文件留给 store
    }
    return errors.Join(errs...)
}
```

两类旧对象的处理**不同**，理由很明确：

| 旧对象 | 处理 | 为什么 |
|---|---|---|
| 旧写层 | `Close()` —— 解映射并**删除文件** | 它记录的是被丢弃时间线上的写，没有任何人会再需要 |
| 旧封存层 | `CloseKeepFile()` —— **只解映射** | 文件归 store 所有，别的 checkpoint 的视图可能还引用着它 |

### 7.1 失败语义

`ResetView` 里的「失败」只可能来自**清理**，不可能来自换视图本身 —— 换视图就是锁内的两次指针写。
所以代码里的注释说得很直白：

> The swap itself happened (it is two pointer writes); only cleanup of the old view failed.
> Report it — leaked mappings — but the view is correct, which is what rollback needs.

报错，但视图是对的。泄漏一些映射比让上层以为回滚失败要好 —— 因为**它没失败**。

---

## 8. 账本与污染

`AppendLayer` 里有一处设计值得单独讲。它的失败处理是这样的：

```go
defer func() {
    if err != nil {
        st.poisoned = true
    }
}()
```

一旦封存动作已经发生（层已经在活的栈里了），账本就**必须**跟上它。
如果跟不上（比如合并校验失败），后面每一个用这份账本构造的视图都会**少一层** ——
恢复时静默地读到旧数据。

所以处理方式不是「重试」也不是「回滚」，而是**把账本标记为不可信**，
此后一切 `AppendLayer` 直接报错：

```go
if st.poisoned {
    return nil, fmt.Errorf("rootfs bookkeeping for sandbox %s is poisoned; " +
        "refusing to record a view that may be wrong", sandboxID)
}
```

> **账本跟着栈走，不跟着 RPC 的成败走。** 栈已经变了，这是既成事实；
> RPC 失败只是调用方没拿到结果。把两者混为一谈，就会产生「账本说有 3 层、实际有 4 层」
> 这种没法自愈的状态。

污染由一次成功的 restore 清除 —— 因为 restore 会用 checkpoint 自己的 header
重新播种账本（`SetRootfsToEntry`），此时账本与栈重新对齐。

---

## 9. 小结

1. 沙箱的磁盘 = **只读模板（远程、按块拉取）+ COW 写层（稀疏文件 + mmap）**，经 NBD 暴露给 guest。
2. 原生 `ExportDiff` 必须停沙箱，因为它**摘掉**写层并把 Overlay 置为作废 ——
   那是「沙箱生命终点」的语义。
3. `SealLayer` **一个字节都不搬**：刷屏障 → 新建空写层 → 两次指针替换 → 改名入库。
   guest 写的那个文件自始至终是同一个文件，只是身份从写层变成只读层。
4. 刷屏障保证「内核已接受的写都到了」，**不保证** guest 停止写 —— 后者靠暂停虚机。
5. 不紧凑化 ⇒ **恒等映射**，比原生少一步换算；代价是不剔除全零块。
6. **刻意不 msync**：一致性不欠它（读者共享同一份 page cache），持久性够不着
   （账本本来就活在进程内存里）。省下 0.49 ms/脏 MB，把封存从 O(改动量) 变成 O(1)。
7. 层链只影响**运行中沙箱的读**；恢复按合并 header 重新组装，与链深无关。
8. 账本**跟着栈走**：封存一旦发生，账本跟不上就把自己标记为污染并拒绝再工作，
   而不是留下一个看起来正常的错账本。

---

## 思考题

1. `SealLayer` 若在 `Overlay.Seal()` 成功之后、`MoveFile()` 之前失败，系统处于什么状态？
   为什么代码在这里选择「只报告、不回退」？回退需要什么条件？
2. 恒等映射意味着层文件里可能有大量全零块。设计一个不引入数据搬运、
   又能回收这些块的方案。它需要什么前提？
3. 假设某个 checkpoint 的 `.meta` 侧车丢失，但层文件完好。
   恢复时会发生什么？这个错误是会被发现，还是会静默地读到错数据？
4. 层链的读放大是 O(k)。如果要把它压到 O(1)，需要引入什么结构？
   为什么当前实现认为不值得？（提示：考虑读放大发生在什么时候，与封存频率的关系。）

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 写层：稀疏文件 + mmap + dirty 表 | `internal/sandbox/block/cache.go` — `NewCache`、`DirtyOffsets`、`MarkCached` |
| 文件搬移与跨文件系统退化 | 同上 — `MoveFile`、`copyFileSparse`、`CloseKeepFile` |
| 原生紧凑导出 | 同上 — `ExportToDiff`；`internal/sandbox/rootfs/nbd.go` — `ExportDiff` |
| 换层的两次指针替换 | `internal/sandbox/block/overlay.go` — `Seal`、`ResetView` |
| 封存层的读路径 | `internal/sandbox/block/sealed_view.go` |
| 四步封存与刷屏障 | `internal/sandbox/rootfs/nbd.go` — `SealLayer`、`ResetView`、`sync` |
| 恒等映射与合并 header | `internal/checkpoint/rootfs.go` — `identityMappings`、`AppendLayer` |
| 层侧车 | 同上 — `writeLayerMeta`、`readLayerMeta`、`DiskViewForEntry` |
| 恢复时组装视图 | `internal/sandbox/checkpoint.go` — `assembleView` |
| 映射合并与校验 | `packages/shared/pkg/storage/header` — `MergeMappings`、`NormalizeMappings`、`ValidateMappings` |

**下一篇**：[11 · 进程内原地回滚](11-in-place-rollback.md) —— 磁盘视图换好了，
内存和设备状态怎么在活着的 Firecracker 进程里写回去。
