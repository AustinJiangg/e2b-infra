# 07 · 磁盘分层

## 本章目标

内存那一半可以"只存脏页"，磁盘这一半怎么办？答案是：**一个字节都不搬** —— guest 写进去的那个文件，改个名就成了只读层。
读完本章，你应当能够：

- 画出一个 e2b 沙箱的磁盘是怎么拼出来的：模板、写层、层栈、Overlay、NBD 各是什么角色；
- 说清原生导出磁盘增量为什么必须停沙箱，而封存（`SealLayer`）为什么不用；
- 说出封存的三步各自不可省的理由，解释为什么封存之后**不等数据落盘**也是安全的；
- 说清运行中沙箱的读路径和 restore 装配视图的代价与什么有关、与什么无关，以及视图块数为什么随时间增长；
- 说清磁盘账本什么时候被标为"污染"、层文件按什么规则回收、什么时候两层会被合成一层，以及为什么这些都不改变任何视图读到的块。

上一章（[06](06-memory-diff-tree.md)）讲了内存那一半怎么存、怎么回。本章讲磁盘那一半。两者在同一次暂停里拍下、在同一次暂停里换回
（[01](01-background.md) §6.2 的一致性要求），内存的合并与磁盘的层合并也成对发生。下一章（[08](08-firecracker-api-contract.md)）讲 Firecracker 这一侧的接口契约。

代码在 `packages/orchestrator/internal/sandbox/block/`、`internal/sandbox/rootfs/nbd.go`、`internal/checkpoint/{rootfs,layer_refs,compact_layers}.go`。

---

## 1. 沙箱的磁盘长什么样

### 1.1 部件

```
        guest 看到的 /dev/vda
                 │
                 ▼
    ┌────────────────────────────┐
    │   NBD 设备  /dev/nbdX       │   内核块设备
    └────────────┬───────────────┘
                 │  dispatcher（用户态）
                 ▼
    ┌──────────── Overlay ────────────┐   读合并、写落上层
    │  Cache（活写层）                  │   稀疏文件 + mmap + 块位图
    │  LayerStack（封存过 checkpoint 后）│   封存层 + 块归属索引
    │  模板 rootfs（只读）              │   chunker 从对象存储按块拉取
    └──────────────────────────────────┘
```

| 部件 | 是什么 |
|---|---|
| 模板 rootfs | 只读。**不在本地** —— 由 chunker 按块从对象存储按需拉取，本地只缓存拉过的块 |
| Cache（写层） | 一个与设备等大的**稀疏文件**，`mmap` 进进程地址空间，外加一张记录"这一层持有哪些块"的集合 |
| LayerStack | 打过 checkpoint 之后才有：若干封存层叠在模板之上，外加按块的归属索引（§6.1） |
| Overlay | 读：先查写层，未命中再问下面；写：全部落写层（`block/overlay.go:39`、:201） |
| NBD | 把这个 Go 对象暴露成一个内核块设备，guest 通过 virtio-blk 访问它 |

写层是**稀疏**的：创建时 `truncate` 到设备大小，物理上一个块都不占；guest 写哪块，哪块才落盘。
所以一个刚起来的沙箱，写层文件逻辑上与磁盘等大、物理上接近 0。

### 1.2 块集合就是这一层的"位图"

`Cache.dirty`（`block/cache.go:54-56`）是按块号索引的位图 `blockSet`（`block/blockset.go:30`），`NewCache` 时按设备大小一次分配（每块 1 位）。
位只置不清，读写用原子 OR / Load，不需要锁；一个位被置上之前写进映射的字节，对看见这个位的读者可见。

它的作用和内存那边的纪元位图完全对应：**记录这一层持有哪些块**。`DirtyOffsets()`（`cache.go:534`）把它排序后返回，是后面所有账本的原料。

---

## 2. 原生怎么导出磁盘增量

原生 pause 走 `NBDProvider.ExportDiff`（`rootfs/nbd.go:72`，逐步拆解见 [02](02-e2b-native-snapshot.md) §4）：摘出写层（`Overlay.EjectLayers`，
Overlay 就此作废）→ 停沙箱、等 NBD 设备释放 → 全零块只记 empty 位、非零块紧凑追加进 diff → 关闭写层。

产物是一个**紧凑**的 diff 文件：块在文件里按顺序排列，与它在设备上的位置无关，所以要一张映射把设备偏移翻译成存储偏移。
它的语义是**沙箱生命的终点**：摘出之后任何写都没有去处，遍历 mmap 也必须没有并发写。对"沙箱离场"合适，对高频 checkpoint 不能接受。

沙箱打过 checkpoint 之后，写层已经被封存过多次，当前写层只含最近一次之后的写。所以 `EjectLayers`（`block/overlay.go:96`）交出**全部**层
（封存层自底向上，再加当前写层），`ExportLayersToDiff`（`block/cache.go:181`）按"最上层有就取最上层"把整个栈压平导出，
与沙箱自己读到的内容一致。内存那一半在同一场景下的正确性前提见 [16 §4](16-native-increment-fix.md#4-与-checkpoint-叠加累积位图)，两半合起来见 [18 §7](18-native-and-checkpoint-together.md#7-checkpoint-之后做原生-pause-的正确性前提)。

---

## 3. 封存：不搬运任何数据的换层

`NBDProvider.SealLayer`（`rootfs/nbd.go:141`）在同一个 Overlay 上另开一条路径，在虚机暂停期间做三步：

```go
o.sync(ctx)                                                             // ① 刷屏障：ioctl(BLKFLSBUF) + flush
newCache, _ := block.NewCache(size, o.blockSize, newCachePath, false)   // ② 新空写层：open + truncate（稀疏）+ mmap
sealed, _ := o.overlay.Seal(newCache)                                   // ③ 旧写层进层栈，新写层上位
```

**没有任何一步在搬运数据。** 第四步"移入 store"放在恢复运行之后（§3.3）。

### 3.1 第一步：刷屏障保证什么、不保证什么

`BLKFLSBUF` 让内核把这个块设备上缓冲的写全部推下去，经 NBD dispatcher 落进写层的 mmap。

- 它**保证**：内核块层已经接受的每一个写，都到了写层里；
- 它**不保证**：guest 停止产生新的写。

所以调用契约里明确写着**虚机必须已经暂停**：刷屏障处理的是"内核手里还攥着的"，暂停处理的是"guest 还要写的"，两者缺一不可。
`SealLayer` 与 `ResetView` 的注释都以这个前置条件开头 —— 它是调用方的责任，不是这个函数能自己保证的事。

### 3.2 第三步：换层

`Overlay.Seal`（`block/overlay.go:128`）持 Overlay 的独占锁：

```go
sealed := o.cache
stack, ok := o.device.(*LayerStack)
if !ok {
    stack = NewLayerStack(o.device, size)   // 第一次封存：把模板包进一个层栈
}
stack.AppendLayer(sealed)                   // 旧写层叠上去，它持有的块在归属索引里记成"归这一层"
o.device = stack
o.cache = newCache                          // 新写层上位
```

旧写层没有被丢弃 —— 它成了层栈的最上一层，运行中的沙箱继续能读到自己封存前写的每一个块，只是现在是**从读路径**读到的。
代价与**本层写过的块数**成正比（在归属索引里登记），与磁盘大小和已有层数无关。

`o.mu` 是一把读写锁，读写路径持共享锁，`Seal` 持独占锁 —— 换指针发生在 NBD dispatcher **底下**，它手里那个 `*Overlay` 指针从头到尾没变过，
所以换层对它们是原子的。

> **类比**：OverlayFS 把 upper 降级成 lower，再开一个新 upper，**挂载全程不卸载**。差别只是这里发生在宿主的块层，而不是 guest 的文件系统层。

`Seal` 成功之后，代码注释写明"从这里起失败只报告、不回退"（`rootfs/nbd.go` 中 `Seal` 之后）：栈已经换了，是既成事实；
回退要把一个已经接收过新写的新写层再拆下来，反而会让层栈与账本对不上。账本怎么跟上这个既成事实见 §8。

### 3.3 移入 store：改名，不是拷贝

封存出的层文件先留在沙箱缓存目录，checkpoint 在 resume **之后**才调 `SealedLayer.MoveInto`（`rootfs/rootfs.go:62`；
调用在 `internal/sandbox/checkpoint.go:238`，计时键 `seal_move`）把它移进 `layers/`。

同一文件系统内这是一次 `rename`：**inode 不变**，所以那个还活着的 mmap 继续有效 —— 它映射的是 inode，不是路径。

跨文件系统时 `rename` 返回 `EXDEV`，`Cache.MoveFile`（`cache.go:570`）退回**保洞的稀疏拷贝**，然后 unlink 原文件。
这看起来危险（刚说不搬数据），但它安全：活着的 mmap 继续从那个已被 unlink 的 inode 提供读服务，而这只发生在**已封存**的层上 ——
封存内容再也不会改变，映射看到的和 store 里那份拷贝永远一致；等映射消失时，被 unlink 的 inode 的块才被真正释放。
也就是说跨文件系统的路径在**正确性**上没问题，只是**代价**退化成一次拷贝；放在暂停窗口之外，是为了不让冻结时长随改动量增长。
把 store 和沙箱缓存放在同一个文件系统上（默认都在 `ORCHESTRATOR_BASE_PATH` 下），这条路径就不会走到。

### 3.4 对比

| | 原生 `ExportDiff` | `SealLayer` |
|---|---|---|
| 数据搬运 | 逐块从 mmap 读出、写进输出流 | **无** |
| 沙箱 | 必须停 | 暂停即可，之后继续跑 |
| Overlay | 作废 | 继续用，只是换了一层 |
| 产物布局 | 紧凑，存储偏移 ≠ 设备偏移 | 原样，存储偏移 == 设备偏移 |
| 全零块 | 剔除（只记 empty 位） | 保留，按块占盘 |
| 之后还能写吗 | 不能 | 能，写进新写层 |

---

## 4. 恒等映射、合并 header 与层侧车

### 4.1 不紧凑化带来的简化

因为层文件就是原来那个写层文件、块还在原来的偏移上，映射关系是**恒等**的：

```
设备偏移 0x1000  →  层文件偏移 0x1000
```

`identityMappings`（`internal/checkpoint/rootfs.go:393`）把连续的块合成一个 run，每个 run 一条 `BuildStorageOffset == Offset` 的映射：

```go
mappings = append(mappings, &header.BuildMap{
    Offset:             uint64(runStart),
    Length:             uint64(runLength),
    BuildId:            layerID,
    BuildStorageOffset: uint64(runStart),   // ← 与 Offset 相同
})
```

比原生少一步偏移换算。代价是**不剔除全零块**：guest 写了一整块零，紧凑 diff 会把它记成一个 empty 位（0 字节），层文件则实打实占一个块。
对高频 checkpoint 这个取舍是划算的：省下的是每次封存的一整趟 I/O，多花的是磁盘空间，而写层本来就是稀疏的、只有真写过的块才占盘。

### 4.2 合并 header

每打一个 checkpoint，`AppendLayer`（`rootfs.go:64`）在全局锁下把新层的映射折进沙箱当前的合并 header：

- `MergeMappings`：上层覆盖下层；
- `NormalizeMappings`：合并相邻同源区间；
- `ValidateMappings`：检查结果**无缝无叠、恰好覆盖整个设备**。

结果序列化成该条目的 `rootfs.header`，它描述的是"模板 rootfs 的映射 + 到此为止每一层的恒等映射"—— 也就是那一时刻的完整磁盘视图。
序列化和两次写文件在锁外。

### 4.3 层侧车

每个层文件旁边写一个 `.meta` 侧车（`writeLayerMeta`，`rootfs.go:356`）：每个块偏移 8 字节小端一条，记录这一层持有哪些块。

它的作用只有一个：**restore 重新打开这个层文件时，要恢复"它持有哪些块"这份知识**。文件里有数据，但文件系统不会告诉你哪些块是"这一层写的"、
哪些是稀疏空洞 —— 空洞读出来是零，而零也可能是 guest 真写的内容。所以数据由文件提供，归属由侧车提供，两者合起来才是一个可用的层。

侧车丢了会怎样？restore 在暂停之前就要读侧车（§6.2），`DiskViewForEntry`（`rootfs.go:331`）读不出来就返回错误，restore 在动虚机之前失败，
沙箱照常运行在当前状态。这类错误会被发现，不会静默读到错数据。

---

## 5. 封存不等待落盘

`SealLayer` 里**刻意不做** msync（`rootfs/nbd.go` 中 `Seal` 之后那段注释）。拆成两个论证：

**一致性不欠这个 flush。** 封存层的脏页留在宿主的 page cache 里。此后所有读者 —— 运行中的沙箱经层栈读同一个 mmap，
restore 用 `NewCache` 重开同一个文件 —— 都读同一份 page cache。页在内存里还是在盘上，对读者完全透明；msync 买不到任何一致性。

**持久性本来就够不着。** msync 买的是**宿主崩溃后的持久性**。但这套 checkpoint 的语义根本达不到那么远：账本全在 orchestrator 的进程内存里，
进程一死全部消失，剩下一堆没人认识的文件（[14](14-lifecycle-reasoning.md)）。为一个不可能兑现的保证付每次封存的代价，不划算。

**代价在哪。** 等待 msync 的时间与脏数据量成正比，是封存耗时随改动量增长的**唯一**来源；去掉之后封存基本是常数（几次 ioctl、几次指针写）。
代码注释记下了去掉之前量到的代价：**每个脏 MB 约 0.49 ms**（`rootfs/nbd.go` 中 `Seal` 之后那段注释；注释没有写平台与负载，这里只当量级看）。按这个量级，一个改了 256 MiB 的 checkpoint 光等落盘就要约 125 ms，比一次 restore 还长。
换个角度看这条优化：它把磁盘那一半从"与改动量成正比"变成了"与改动量无关"。内存那一半天然是 O(脏页)，磁盘这一半因此是 O(1)（当前代码 checkpoint 耗时随改动量的斜率见 [21 §3.3](21-benchmarks-and-compliance.md#33-拟合与碰线点)）。内核照样会按自己的节奏把这些页写回盘。

---

## 6. 读路径与视图装配

### 6.1 运行中的沙箱：块归属索引

封存 k 次之后，写层之下有 k 个封存层。最朴素的读法是一层层往下问："最上层有没有这个块？没有就问下一层……"——
最坏一次读要走 k + 2 层，链越长读越慢。

`LayerStack`（`block/layerstack.go:40`）不这样做。它维护一个**按块的归属索引** `owner []uint16`（0 表示模板底座持有，k 表示第 k 层持有）。
后封存的层覆盖它持有的块的索引项，正是"最上层胜出"。读一个块时从数组里取出归属、直接去那一层读（`ReadAt`，:156），**代价与层数无关**。

代价是每块 2 字节：每 GiB rootfs 每个视图 512 KiB。索引最多容纳 65535 层（`maxStackLayers`，:14）。LayerStack 自己不加锁，
靠 `Overlay.mu`：读在共享锁下、`Seal` 在独占锁下原地追加，读永远看不到半更新的索引。

### 6.2 restore：装配视图

restore 不沿用运行中的栈，而是按目标条目自己的层清单重新叠一个（`Sandbox.AssembleView`，`internal/sandbox/checkpoint.go:339`）：

```go
stack := block.NewLayerStack(base, size)
for _, layer := range layers {                                 // 自底向上
    cache, _ := block.NewCache(size, blockSize, layer.Path, false)
    cache.MarkCached(layer.DirtyOffsets)                       // 侧车给出"哪些块是这一层的"
    stack.AppendLayerWithBlocks(cache, layer.DirtyOffsets)     // 按区间填归属索引
}
```

装配在**暂停之前**做：读的全是不可变的封存层和侧车，沙箱的 checkpoint 操作又是串行的，没人能在底下再封一层。装配不出来时
沙箱照常运行在当前状态。冻结窗口里只剩一次 `ResetView` 换指针（§7）。相比之下，原生恢复要重建整个 Overlay 并靠缺页把工作集换回来。

侧车先由 `DiskViewForEntry`（`internal/checkpoint/rootfs.go:331`）读出解码（计时键 `disk_view_read`），再逐层重开。两处都**按区间批量**处理侧车列表：
`MarkCached`（`block/cache.go:506`）把连续块合成 run、再按 64 块一个字做一次原子 OR；`AppendLayerWithBlocks`（`layerstack.go:88`）对每个 run
做一次索引区间填充（`claimRun`，:130；run 由 `forEachRun` 切出，`blockset.go:161`）。

块集合为什么用位图而不用以块偏移为键的哈希表？后者每块要一次哈希插入和一次分配、每块约占上百字节堆，装配视图的时间和存活堆都随块数线性增长；
位图 + 区间批量之后，restore 随块数增长的斜率很小（前后对比见 [22 §4.9](22-long-run-and-concurrency.md#49-视图块数增长restore-随时间变慢)）。

### 6.3 视图块数为什么会增长

一个视图的块数是它各层侧车列出的块数，最底层（经层合并后常常是一层大层）约等于"guest 自启动以来写过的块的并集"。
guest 里的 ext4 在截断重写小文件时往往**分配新块**而不是复用旧块，于是这个并集只增不减，长期运行的沙箱视图块数随时间增长。

它有上界：块集合不会超过 guest 能写到的块 —— 大致是文件系统起始的空闲块加上日志区（循环写）。对交付模板的 rootfs，这个上界约 16 万块
（长跑中的块数曲线与 restore 随块数的斜率都见 [22 §4.9](22-long-run-and-concurrency.md#49-视图块数增长restore-随时间变慢)）。restore 剩下的随块数增长的部分主要来自读侧车和内存回滚量，是真实工作量。

---

## 7. ResetView：挂载不动，整体换视图

回滚时磁盘侧要换掉的是**整个视图** —— 读路径和写层一起换（`Overlay.ResetView`，`block/overlay.go:168`；`NBDProvider.ResetView`，`rootfs/nbd.go:194`
先刷屏障、再建新空写层）：

```go
o.mu.Lock()
oldDevice, oldCache := o.device, o.cache
o.device = device      // ← 目标 checkpoint 的层栈（§6.2 装配好的）
o.cache = cache        // ← 全新的空写层
o.mu.Unlock()

errs = append(errs, oldCache.Close())              // 被丢弃时间线的写层：连文件一起删
if stack, ok := oldDevice.(*LayerStack); ok {
    errs = append(errs, stack.Close())             // 旧层栈：只解映射，文件留给 store
}
```

两类旧对象的处理**不同**，理由很明确：

| 旧对象 | 处理 | 为什么 |
|---|---|---|
| 旧写层 | `Close()`：解映射并**删除文件** | 它记录的是被丢弃时间线上的写，没有任何人会再需要 |
| 旧层栈 | `LayerStack.Close()`：各层 `CloseKeepFile()`，**只解映射** | 文件归 store 所有，别的 checkpoint 的视图可能还引用着它 |

**失败语义。** `ResetView` 里的"失败"只可能来自**清理**，不可能来自换视图本身 —— 换视图就是锁内的两次指针写。
所以它报错（泄漏了一些映射），但视图是对的，而回滚需要的正是视图对。泄漏一些映射比让上层以为回滚失败要好，因为它没失败。

---

## 8. 账本与污染

`appendLocked`（`internal/checkpoint/rootfs.go:130`）一旦越过"层已经进了活栈"这一点，任何失败都把沙箱的磁盘账本标为**污染**。
为什么？封存已经发生，层已经在活的栈里了，账本就**必须**跟上它。如果跟不上（例如合并校验失败），后面每一个用这份账本构造的视图都会**少一层** ——
restore 时静默读到旧数据。

所以处理方式不是"重试"，也不是"回滚"，而是**把账本标记为不可信**，此后一切 `AppendLayer` 直接报错（`ErrRootfsPoisoned`）。
header 写不出来、封存层没能进 store 等发生在 `AppendLayer` 之外的同类失败走 `PoisonRootfs`（:245）。

> **账本跟着栈走，不跟着 RPC 的成败走。** 栈已经变了，这是既成事实；RPC 失败只是调用方没拿到结果。
> 把两者混为一谈，就会产生"账本说有 3 层、实际有 4 层"这种没法自愈的状态。

污染由一次成功的 restore 清除：`SetRootfsToEntry`（:276）用该条目自己的 header 重新播种账本，此时账本与栈重新对齐。
那些进了活栈却没进账本的层记在 `rootfsState.unrecorded`（:47），它们不在任何视图里、不被计数，等下一次 restore 替换活栈时回收；没有 restore 就随沙箱走。
污染对调用方意味着什么（先 restore 一个已有的 checkpoint）见 [25](25-errors-timeouts-concurrency.md)。

---

## 9. 层回收：按视图引用计数

每个 checkpoint 封存一层。一层什么时候可以删？答案是：**没有谁再读它的时候**。谁读一层，就是谁列着它（`internal/checkpoint/layer_refs.go:13-59`）：

- 每个已发布条目的视图（`Entry.Rootfs`），restore 从它重组磁盘；
- 活账本（`rootfsState.layers`），下一个视图从它复制，它也描述着沙箱正在读的栈。

两者都计数，每列一次一个引用（`Store.layerRefs`，`store.go:480`），**计数归零**的层连同 `.meta` 进 reclaim，锁外删除：

| 位置 | 引用变化 |
|---|---|
| `appendLocked` | 新层进活账本，+1 |
| `publishLocked`（Commit） | 视图列出的每层 +1；`CommitHidden` 没有视图，不加 |
| `SetRootfsToEntry`（restore） | 恢复的视图每层 +1，然后被替换的活账本每层 −1（先加后减，两边都列的层不会碰到零） |
| `deleteLocked` | 条目的视图每层 −1，无论物理删除还是隐藏（隐藏条目不再有视图） |
| `pruneLocked` | 被级联删除的条目若还有视图，每层 −1 |
| 层合并切换 | 一对 [A, B] 的计数移给合并层（§10） |
| `dropStateLocked` | 随整棵沙箱树一起丢弃 |

计数不会从零回升：引用只来自发布从已计数状态复制的视图、restore 到仍被计数的视图、或新封存的层。`PoisonRootfs` 与 `InvalidateBase`
不还引用，因为活栈仍在读 `st.layers`。代码在 `refLayersLocked` / `unrefLayersLocked`（`layer_refs.go:63`、:86）；调试断言 `checkLayerRefs`（:115）从视图重算计数。

**为什么不能按 checkpoint 树回收。** 直觉上"删一个叶子条目，就删它封存的那一层"。但层栈不是 checkpoint 树：`appendLocked` 完全不看 `ParentID`，
只往活账本上叠；而全量捕获是树根（`ParentID == ""`），它的视图却叠在此前封存的每一层上。于是树的叶子可能拥有一层、而它子树之外的条目还在读这一层。
两个反例：跟踪关着时每个 checkpoint 都是全量根，但层一路叠加，删一个叶子若按树回收它的层，后面的条目就永远恢复不了；断链后的新全量根同理。

**跟踪关闭时仍会累积。** 那时每个视图列着此前全部层，删掉一个条目只放掉它自己那份引用，其它视图和活账本仍列着这些层；
也没有"隐藏且只有一个子"的条目，层合并不会发生。层文件因此随 checkpoint 次数累积，直到 restore 替换活栈或沙箱销毁。

---

## 10. 层合并

内存合并（[06](06-memory-diff-tree.md#10-删除隐藏与合并)）把隐藏的 H 并入它唯一的子 C 时，磁盘这一半顺带尝试把
**C 封存的层 B 和紧挨在它下面的层 A** 合成一层（`internal/checkpoint/compact_layers.go`）。不合的话，内存那边条目收敛了，磁盘这边层文件还在一层层叠。

**条件看持有者，不看树**（`planLayerFold`，:155）：所有列 A 的持有者（各条目视图与活账本）都在 A 正上方列 B，所有列 B 的持有者都在 B 正下方列 A，
即 A、B 永远以相邻的一对 `[.., A, B, ..]` 出现。不满足（例如 restore 到 H 之后又做了一次全量捕获，A 被列在别的层下面）就只合内存。

**为什么合并不改变任何视图读到的块。** 视图里每个块由列着它的最上层提供。把相邻的 [A, B] 换成一层"B 的块叠在 A 上"：

- B 持有的块，之前之后都由 B（或更上层）提供；
- A 有而 B 没有的块，之前由 A 提供，之后由合并层在**同一偏移**提供（层文件原样封存，存储偏移 = 设备偏移）；
- 两者都没有的块照旧落到下面。

header 里原来指向 A 的映射改指合并层，偏移不变。由于"永远成对出现"对每个持有者成立，这个论证对每个视图都成立。

**做法**：字节往少的一边拷（`foldLayers`，:291）—— A 有 B 无的块填进 B 的空洞，或者 B 的块覆盖到 A 的文件上（每个持有者都只在 B 没有的地方读 A）；
接收方以新名字 `layer-<B 的 uuid>.<H-id>` 硬链接进来，写新的 `.meta`，每个列着这对层的视图改写一份新名字的 header
（`rootfs.header.<条目-id>.<H-id>`）；活账本换成新层清单与改名后的映射。切换时在锁内再用引用计数核对持有者没变（`layerHoldersUnchangedLocked`，
`compact.go:951`），然后 A、B 计数归零进 reclaim，合并层接过它们的计数（`switchLayersLocked`，:989）。

**活栈不重建。** 正在运行的沙箱继续经已建的映射读 A、B：unlink 不影响已有映射，合并写进去的字节恰好在活栈从不从该文件读的块上。
空间在下一次 restore 替换活栈时才真正释放。

---

## 本章要点

1. 沙箱磁盘 = 只读模板（远程按块拉取）+ COW 写层（稀疏文件 + mmap + 块位图），经 NBD 暴露；打过 checkpoint 后写层之下是层栈。
2. 原生 `ExportDiff` 摘掉写层、必须停沙箱，是"沙箱生命终点"的语义；打过 checkpoint 的沙箱导出时压平全部层。
3. `SealLayer` 一个字节都不搬：刷屏障（保证内核已接受的写到位，不保证 guest 停写 —— 那靠暂停）→ 新空写层 → 旧写层进层栈；
   移入 store 在 resume 之后，同文件系统是 rename，跨文件系统退化为拷贝但仍正确。
4. 恒等映射省一步换算，代价是不剔除全零块；层侧车记录"这一层持有哪些块"，丢了会在 restore 暂停前报错。
5. 刻意不 msync：一致性不欠它（读者共享同一份 page cache），持久性够不着（账本活在进程内存里）；封存因此从 O(改动量) 变成 O(1)。
6. 读路径是块归属索引，代价与层数无关；restore 在暂停前按侧车装配视图，块集合是位图、按区间批量处理；视图块数随 guest ext4 分配新块而增长，有上界。
7. `ResetView` 在锁内两次指针写换掉整个视图，失败只可能来自清理；账本**跟着栈走**，跟不上就标记污染并拒绝再工作，由一次成功的 restore 清除。
8. 层按**视图引用计数**回收，不按 checkpoint 树；跟踪关闭时仍会累积。层合并看持有者：A、B 永远成对出现时合成一层，任何视图读到的块都不变。
