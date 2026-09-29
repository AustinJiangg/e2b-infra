# 10 · 原地回滚特有的问题

## 本章目标

读完本章，你应当能回答：

1. 为什么原地回滚会有"重建路线永远不会遇到"的问题？有没有一个判据能提前把它们找出来？
2. 一个网络 RX 描述符缓存，怎么会让 guest 的收包方向永久卡死？修法为什么只能原地清空、不能重建对象？
3. 宿主内核的连接跟踪表为什么必须清、怎么清才不误伤同节点的其他沙箱、为什么清理可以和回滚并行？
4. guest 恢复之后它的时钟是什么样的；这对验收判据和使用方意味着什么？
5. 给回滚路径加新设备或新代码时，用什么检查表审自己的改动？

---

上一章（[09](09-in-place-rollback.md)）按九个阶段讲了原地回滚的正常路径。把状态写回一个**活着**的进程，和从零**构造**一个新进程，
会遇到完全不同的问题。本章讲五类"重建路线永远不会遇到"的问题：网络 RX 描述符缓存、VMGenID 的次序、宿主连接跟踪（连同代理连接池）、时间、
队列页的脏标记。其中两类的表现是**挂死而不是报错**，这也是它们最难查的原因。最后给出一张检查表（§8），供继续开发的人逐条审自己的改动。
下一章（[11](11-end-to-end.md)）把 04–10 章串起来，按时间顺序完整走一遍一次 checkpoint 与一次 restore。

---

## 1. 为什么会有「特有」的问题

重建路线从快照**构造**一个新的设备对象，构造函数保证了对象内部自洽：每个字段要么来自快照，要么是新建时的初值。
原地路线不构造对象，它**修改**既有对象。于是出现两类新东西：

1. **从活对象里推导出来、又没有进快照的状态**。重建路线下它们根本不存在，原地路线下它们留了下来，
   而且是按**被丢弃的那条时间线**推导的。判据可以主动用：翻一遍设备对象的字段，凡是「缓存」「已解析」
   「上次的」这类语义、又不在 `save_state` 序列化范围内的，都是嫌疑。§2 就是这么一个字段。
2. **宿主上「记得 guest 的事」的状态**：内核的连接跟踪表、代理的连接池。它们不属于虚机，快照里没有，
   但 guest 一回滚它们就全错了（§4）。

---

## 2. 网络 RX 描述符缓存

### 2.1 现象

回滚成功，guest 一切正常，直到需要收包：RX 方向**永久卡死**，guest 内核日志里一行

```
virtio_net: id N is not a head!
```

不是丢包、不是慢，是彻底不动。而且不是每次回滚都出现。

### 2.2 背景

virtio 队列有两个环：guest 把可用缓冲区的描述符放进 **avail 环**，设备处理完把结果放进 **used 环**。RX 方向上：

1. guest 驱动准备一批空缓冲区，把描述符 id 写进 avail 环；
2. 设备从 avail 环取出描述符链，**解析**成可以直接写的 iovec，缓存在 `rx_buffer.parsed_descriptors`；
3. tap 上来一个帧，设备取一个 iovec 写进去，往 used 环放 `(描述符 id, 长度)`；
4. guest 驱动从 used 环取出，按 id 找到自己那块缓冲区。

第 2 步的缓存是性能优化：不必每来一帧就重走一遍环。

### 2.3 根因

回滚时发生了三件事：

- **avail 环的内容**（在 guest 内存里）被阶段 4 回退到目标时刻；
- **队列索引** `next_avail` 被 `apply_one_device` 按快照重置；
- 但设备对象里的 `rx_buffer.parsed_descriptors` **没人动它** —— 它还装着按**被丢弃的时间线**解析出来的描述符链。

于是第 3 步用一个已经不存在的描述符 id 写了一条 used 项。guest 驱动拿到这个 id 去自己的表里查，查不到 —— 在目标时刻它没有发出过这个描述符。
驱动认定环已损坏，拒绝继续处理 RX 队列：**永久卡死**。
只在「回滚时缓存里有已解析的链，且回滚后很快有帧到达」时出现，所以不是每次都有。

为什么重建路线不会遇到？新建的 net 设备 `rx_buffer` 是空的，它会从刚加载好的 avail 环重新解析一遍 —— 构造函数天然做对了这件事。

### 2.4 修法

`apply_net_rx_cache`（`rollback.rs`）在写回设备状态之后调设备侧的 `rollback_rx_buffers`
（`devices/virtio/net/device.rs:471`）：

```rust
self.rx_buffer.iovec.clear();
self.rx_buffer.parsed_descriptors.clear();
self.rx_buffer.used_descriptors = 0;
self.rx_buffer.used_bytes = 0;
self.rx_buffer.min_buffer_size = 0;

self.queues[RX_INDEX].next_avail -= parsed_descriptor_chains_nr;  // 把已解析的退回去
self.parse_rx_descriptors();                                      // 从已回滚的 avail 环重新解析
self.rx_buffer.used_descriptors = used_descriptors;
self.rx_buffer.used_bytes = used_bytes;
```

三个计数来自快照里 net 设备的 `rx_buffers_state` —— 上游快照格式本来就有，用于重建路线；原地路线只是把同样的信息用在活对象上。

### 2.5 连带约束：seccomp

上面是 `clear()` 而不是「换一个新的 `RxBuffers`」。代码注释说明了原因：

```rust
// Cleared in place rather than rebuilt: a fresh RxBuffers would mmap a
// new IovDeque, and the vmm thread's seccomp filter has no
// memfd_create rule — that syscall is only ever made before the
// filters are installed.
```

`IovDeque` 底层用 `memfd_create` + 双重映射实现环形缓冲；而 `memfd_create` 只在 seccomp 过滤器安装**之前**（初始化阶段）
被调用过，所以 VMM 线程的白名单里没有它。若换成新建对象，第一次回滚就会让进程被 SIGSYS 杀掉。

> **运行期能做的事比初始化期少。** 重建路线在一个还没装过滤器的新进程里什么都能做。
> 同类的还有：回滚用到的 vCPU ioctl 必须在 vCPU 线程执行（[09](09-in-place-rollback.md)），
> 「取脏页位图」路径需要放行 VMM 线程的 `pread64`。改动回滚路径时，**凡是新增系统调用，都要同步检查 seccomp 白名单**。

---

## 3. VMGenID 的次序

VMGenID 是一个 ACPI 设备，在 guest 物理内存里放一个 128 位代号；代号变化意味着「你被从快照恢复了」，
guest 据此重新播种随机数生成器等依赖时间单调的东西。**它就在 guest 内存里**，所以若在阶段 4（内存写回）之前刷新：

```
写入新代号  →  内存回滚  →  新代号被目标时刻的旧值覆盖
```

guest 醒来看到的是它熟悉的代号，不知道时间被回拨了。修法是放在阶段 8，内存写回之后，代码在那里写明了理由：

```rust
// ── Phase 8: VMGenID. The guest must learn that time was rewound; a new
// generation (never the snapshot's) is written after the memory revert so
// the revert cannot clobber it. ──
```

写的是**新**代号而不是快照里的那个：guest 需要知道的是「你被回滚了」，而不是「你回到了那个时刻」——
后者会让 guest 以为自己一直在那个时刻，从而不做任何重播种。

> **通用形式**：任何经由 guest 内存传递给 guest 的通知，都必须写在内存回滚之后。目前只有 VMGenID 一个。

---

## 4. 连接跟踪

### 4.1 现象

回滚成功，能发新连接，但**跨越回滚的那些连接全部挂死** —— 不是 RST、不是超时报错，发出去的包石沉大海。
挂死比失败难查得多，因为没有任何错误信息。

### 4.2 根因

宿主内核的连接跟踪表（conntrack）记录流经它的每条连接：五元组、TCP 序号窗口、连接阶段，NAT 依赖它。回滚之后，
guest 的内存回到了连接建立之前（或另一个阶段），**宿主内核却还记得**，包括序号走到了哪里。guest 发出的包序号在内核看来是
**倒退的**，落在跟踪窗口之外，被判为非法直接丢弃。guest 侧看到的就是「发出去了，没有回应」。

### 4.3 两张表，两种清法

| 表 | 内容 | 清法 | 为什么 |
|---|---|---|---|
| 沙箱网络命名空间内的表 | **只有**这个沙箱的流 | 整表 flush | 全都要清 |
| 宿主的表 | 本节点**所有**沙箱的流 | **只删原始元组任一端是本 slot 地址的表项** | 整表 flush 会打断别的沙箱 |

出向流量到达宿主时已被 SNAT 成 slot 地址（出现在原始源地址），入向流量的目的地是 slot 地址（出现在原始目的地址），
所以「原始元组任一端等于 slot 地址」恰好覆盖这个沙箱的全部流（`conntrack.go:134` `deleteHostConntrack` 的注释）。

两半都用常驻的 netfilter netlink socket：每个 slot 一个命名空间内的 socket，建网络时打开；宿主一个，归清扫器所有。
restore 路径上不开也不关 socket —— 关闭 netfilter netlink socket 要等 nf_tables 的工作队列排空，恰好在网络池给新命名空间装规则时很慢
（`conntrack_handles.go` 包注释）。socket 出错时丢弃，命名空间一侧退回「进入命名空间、临时开 socket」的旧路径。

### 4.4 宿主表怎么删：每批一次整表扫描，按代价模型选路

宿主表的删除由一个进程级的**清扫器**串行完成（`conntrack_sweeper.go`）。三件事决定了它的形状：

**① 攒批：一次遍历服务所有在等的 slot。** 内核的 conntrack dump 无论回多少条都要走完全部哈希桶，而且内核不会并行做两次 dump；
并发 restore 各扫各的只会互相排队。所以请求进一个队列，清扫器取出当时所有在等的请求组成一批，扫一次，逐个应答
（`loop`，`conntrack_sweeper.go:121`）。**扫描开始之后到达的请求排进下一批**：dump 按桶推进，可能已经走过新请求那条表项所在的桶，
所以一个请求只能由它发出**之后**才开始的遍历来服务。最坏情况下一个请求等「在途的那次 + 自己那次」两次清扫。

**② 默认路径：原始报文整表扫描。** `deleteHostConntrackByRawScan`（`conntrack_scan.go:32`）发一次不带过滤的 dump，
在回调里直接从线上格式的原始元组读出源、目的两个 IPv4 地址（`origTupleIPv4Raw`，`:103`），与本批全部地址比对；
不为不相关的表项分配任何对象，只把命中的表项原样拷下来。**删除放在 dump 结束之后**：dump 的应答流和删除请求走同一个 socket，
中途发删除会把 dump 的一部分当成自己的回复读走。dump 被内核中断（`ErrDumpInterrupted`）时，已经回来的照删。

**③ 选路：按实测代价模型。** 另一条路是让内核过滤（带 `CTA_FILTER` 的 dump，`conntrack_filter.go`）。一个过滤器内的条件是「与」，
多个过滤器之间才是「或」：若把「源是 slot 地址」和「目的是 slot 地址」写进同一个过滤器，语义就变成「源和目的**同时**是 slot 地址」，
一条也匹配不上。所以「地址在任一端」要两个过滤器，也就是**每个地址两次 dump**，而且每次 dump 内核仍要逐条比较全表。`sweepUsesFilter`
（`conntrack_sweeper.go:280`）比较两者：

```
过滤路径 = 地址数 × 2 × (walk + filtered_per_entry × 表项数)
整表路径 =               walk + scan_per_entry     × 表项数
```

代码里的常数：`walk` = 4.6 ms（一次 dump 不带任何数据的固定代价），`scan_per_entry` = 1.0 µs/条，
`filtered_per_entry` = 0.17 µs/条（鲲鹏 920、6.6 内核、262144 桶上实测）。解出来的交叉点：

| 本批地址数 | 过滤路径更便宜的条件 |
|---|---|
| 1 | 宿主表约 > 7000 条 |
| 2 | 宿主表约 > 43000 条 |
| ≥ 3 | 永不 |

表项数每批读一次 `/proc/sys/net/netfilter/nf_conntrack_count`（`hostConntrackCount`，`conntrack.go:171`）；
读不到就走整表路径，并只告警一次。

过滤路径还有两道保护（`deleteFilteredHostConntrack`，`conntrack_filter.go:250`）：内核回来的每一条在删除前**再核对一次地址**
—— 不认识 `CTA_FILTER` 的老内核会把同一个请求答成整张表，不核对就删会打断节点上所有沙箱；内核拒绝过滤请求时
（`EINVAL`、`EOPNOTSUPP` 等）进程级记住，此后一律走整表路径。

每批的开销分解进 restore 的计时：`conntrack_host_queue`（等在途清扫）、`conntrack_host_sweep`（覆盖本次的那次清扫）、
`conntrack_host_batch_count`、`conntrack_host_entries_count`。定义见 [28](28-observability-reference.md)；
并发与长跑下宿主表的增长、攒批与改算法前后的对比见 [22 §4.6](22-long-run-and-concurrency.md#46-conntrack-清理占了冻结窗口)。

注意这个代价与**整张宿主表**成正比，不是与本沙箱或全部沙箱的条目数成正比：宿主表里还有本机其他进程的连接，920B 长跑里约 95% 的条目与沙箱无关（lo:80 的 `TIME_WAIT`、本机 DNS）。宿主上其他流量一多，restore 的 `conntrack_bg` 就跟着变长，数据与根因见 [22 §4.6](22-long-run-and-concurrency.md#46-conntrack-清理占了冻结窗口)。

### 4.5 时机：pause 后启动、与回滚并行、resume 前 join

清表要满足一条约束：**表项必须在流量放回来之前清完**。反过来想：若放到 resume 之后再清，guest 已经在跑、可能已经发出了包，
我们正在删的表项里可能就有它刚刚建立的新连接；而在删完之前，那些跨越回滚的旧表项仍在丢 guest 的包。两种错误都表现为连接挂死，没有任何报错。

这条约束只要求「虚机已停」，不要求「回滚已完成」。所以：

- `RollbackInPlace` 在 pause 之后立刻在后台启动清理（`startConntrackFlush`，`internal/sandbox/checkpoint.go:497`），
  与导出位图、物化、Firecracker 回滚、`ResetView` 并行；
- 在 resume 之前 join（`checkpoint.go:694`）；提交点之前失败、需要原样恢复虚机的路径同样**先 join 再 resume**（`:520`）；
  另有一个 `defer` 兜底（`:510`）；
- 清理内部，命名空间表的 flush 与宿主侧的等待**也是并行的**（`Slot.FlushConntrack`，`conntrack.go:44`）：
  两张表互不依赖，flush 在宿主请求排队期间做完；
- 后台清理写自己的计时 map，join 时再拷进 restore 的 map —— restore 的 map 必须保持单写者，
  Go 的 map 并发写是直接让进程崩溃的 fatal error（`conntrack_flush.go:92` 注释）。

提前启动把「表项已删」到「guest 恢复运行」之间的空隙从不到 1 ms 拉长到整个回滚的长度。这段空隙里宿主侧到达的包可能
重建一条表项，代码注释（`conntrack_flush.go:37-64`）逐条论证了为什么可以接受：代理连接池在 pause 之前已丢弃、restore
窗口内到达的请求被拒而不是转发；一条表项只由宿主侧的包决定，虚机停着还是在跑不影响它长什么样，这条包晚几毫秒到达也会建出同一条表项；
真正变化的只是不重传的裸 ACK，它建出的松散表项会被恢复后的 guest 用 RST 关掉。另有一处行为变化：提交点之前失败、原样恢复的沙箱，
也付了一次本不需要的清表。

计时键 `conntrack` 量的是 resume 前 **join 的等待**（通常接近零），`conntrack_bg` 是清理自身的时长，`conntrack_ns` 是命名空间那一半。
`conntrack_bg` 是看宿主表增长的指标。

### 4.6 代理侧的连接池

orchestrator 自己的代理对每个沙箱维护一个连接池。回滚之后，池里那些还开着的连接，对端（guest 里的服务）已经不记得了。

```go
// service.go — beginRestore
if s.dropConnections != nil {
    if err := s.dropConnections(connectionKey); err != nil { ... }   // 只记 WARN
}
```

丢弃发生在 restore **一开始**：磁盘视图装配完、暂停虚机之前（`service.go` — `beginRestore`，`:1058`），而不是等回滚完成之后。跨越回滚的连接此刻已注定作废：
早断开，持有它们的调用方毫秒级得到应答（unary 调用被改判成 409 `sandbox_restored`）；晚断开，它们要挂到 restore 结束，
最坏是 45 s 的 envd 等待。代价是 restore 若在提交点之前失败，这些调用方白白重连一次 —— 两种错里便宜的那种。
丢弃失败只记警告：池里的坏连接会在下次使用时报错被剔除。

### 4.7 对使用方的结论

restore 会作废该沙箱上**全部跨越回滚的 TCP 连接**，同节点其他沙箱不受影响，restore 之后新建的连接不受影响。
面向使用方的完整说明在 [24](24-semantics-and-limits.md)，调用方看到的错误形态（unary 的 409、流式的传输层错误）在
[25](25-errors-timeouts-concurrency.md)。

---

## 5. 时间

回滚把 guest 的时间**拨回快照时刻**。这是快照语义的一部分，但后果不那么显然。

**单调时钟真的会倒退。** guest 的 `CLOCK_MONOTONIC` 来自 vCPU 的计时器寄存器，而它们在阶段 5 被恢复成快照时刻的值。
这违反 POSIX 对单调时钟的承诺，但在快照语义下是唯一自洽的做法：guest 的所有状态都与那个时刻一致。

**墙钟由 envd 校回。** 墙钟（`CLOCK_REALTIME`）不能留在过去 —— 那会让 guest 里的一切时间戳都错。恢复流程最后等 guest 里的 envd 应答，
envd 初始化会把墙钟校回当前时间（`service.go` — `finishRestore`）：

```go
// The guest woke up with the clock of checkpoint time; envd init brings
// the wall clock back to now, same as the pause/resume path. Monotonic
// time staying rolled back is part of restore semantics.
err := waitForEnvd(ctx)   // 即 sbx.WaitForEnvdAfterRestore(ctx, envdRestoreTimeout)
```

这与 e2b 原生 pause/resume 路径的行为一致，不是本方案特有的语义。

**对验收判据的影响。**「恢复后心跳进程还在跑」不能用「时间戳在推进」或「tick 速率正常」判：墙钟在恢复瞬间跳变，
单调时钟被拨回，基于它的速率会得到荒谬的值。验收脚本的活性判据因此只断言**心跳计数在增长**，不对时间做任何假设
（[19](19-testing-and-functional-verification.md)）。这一条是「先想清楚语义、再写测试」的典型：若按直觉用时间判活，
测试会在一个完全正确的 restore 上报失败。

反过来，时间倒退也是有用的证据：验收里心跳进程在多代 checkpoint / restore 之间保持**同一个 PID 和同一个启动时刻**，
这证明打快照没有重启虚机，而且是「回到那一刻」最硬的一条证据。

---

## 6. 队列页的脏标记

这一类属于「回滚破坏了别的机制的前提」。阶段 9 清空脏页跟踪，让下一代 Diff 相对刚恢复的状态；但 virtio 队列的环页
**在运行期被 Firecracker 自己写**（设备往 used 环放条目），这些写不经过 Stage-2 故障，KVM 不知道
（[05](05-dirty-page-tracking-and-hdbss.md)）。清空之后位图里没有这些页，下一代 Diff 会**漏掉**它们，
破坏 [06](06-memory-diff-tree.md) 里回滚集的正确性证明。所以阶段 9 清完立刻对已激活设备调 `mark_queue_memory_dirty`
（未激活的设备还没有队列内存可标）。

---

## 7. 为什么要有逐设备的诊断日志

RX 缓存和 conntrack 两类问题的共同点是：**表现为挂死，而沙箱一旦销毁就什么都不剩**。所以回滚写回设备状态之后，
Firecracker 按 info 级别逐设备记一行队列位置（`log_queue_diagnostics`）：

```
rollback: device {id} (type {ty}) q0 avail=… next_avail=… next_used=… pending=… | rx_buffer parsed=… used_desc=… used_bytes=…
```

avail / used 是按设备读环的方式从已回滚的 guest 内存里读出来的，所以这一行就是「设备和 guest 分别认为进行到哪里」的快照，
两者对不上就是问题所在。pause 路径上也输出同样的诊断，但降为 debug 级别 —— 每次 pause 都走这条路，默认不该刷屏；
需要对比「回滚前」与「回滚后」时调高日志级别即可。这类日志的价值在于**罕见故障只有一次现场**：一个几百次才复现一次的挂死，
如果现场只有「沙箱没反应」，基本无从下手；有了这一行，至少能分清是设备没往下走，还是 guest 没往下走。

按症状排查的步骤（连接挂死、RX 卡死、串口卡住等）见 [29](29-troubleshooting.md)。

---

## 8. 检查表：还有哪些地方可能有同类问题

给加新设备、改回滚路径的人，逐条过一遍：

| 问 | 如果是 |
|---|---|
| 设备对象里有没有「从 guest 内存推导出来的缓存」？ | 回滚后必须重建它（如 RX 描述符缓存） |
| 有没有「上次操作到哪」的游标，且不在 `save_state` 里？ | 同上 |
| 有没有经由 guest 内存传递给 guest 的通知？ | 必须写在内存回滚**之后**（如 VMGenID） |
| 设备运行期会不会自己写 guest 内存？ | 那些页要在阶段 9 之后重新标脏 |
| 宿主上有没有「记得这个 guest」的状态？ | 要在流量放回来之前清掉（如 conntrack、代理连接池） |
| 新代码有没有引入新的系统调用？ | 检查 seccomp 白名单（VMM 线程、vCPU 线程各一份） |
| 有没有在途的异步 I/O？ | 阶段 2 要排空它 |
| 有没有「KVM 内部挂起、不在快照里」的状态（如 arm64 延迟完成的 MMIO 退出）？ | pause 停车前与回滚写寄存器前都要排干（[09](09-in-place-rollback.md)） |
| 往活着的内核对象写「写 1 生效」的成对寄存器（GIC enable / pending / active）？ | 必须先清后设的绝对写回（[09](09-in-place-rollback.md)） |
| 设备状态与宿主侧后端（tap 的 offload 标志等）有没有必须一致的配对？ | 回滚设备状态后按回滚后的值重设后端 |
| 新设备支持热插拔或动态配置吗？ | `validate_topology` 要能表达，否则应当拒绝 |

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| RX 缓存重建 | `firecracker/src/vmm/src/rollback.rs` — `apply_net_rx_cache`；`devices/virtio/net/device.rs` — `rollback_rx_buffers`、`parse_rx_descriptors` |
| VMGenID、队列页重新标脏 | `rollback.rs` — 阶段 8、阶段 9 |
| 队列诊断日志 | `rollback.rs` — `log_queue_diagnostics` |
| 清表入口、两半并行 | `packages/orchestrator/internal/sandbox/network/conntrack.go` — `FlushConntrack`、`flushNamespaceConntrack`、`deleteHostConntrack`、`hostConntrackCount` |
| 攒批与选路 | 同目录 `conntrack_sweeper.go` — `loop`、`run`、`sweepUsesFilter`、`deleteHostConntrackBatch` |
| 整表原始扫描 | 同目录 `conntrack_scan.go` — `deleteHostConntrackByRawScan`、`origTupleIPv4Raw` |
| 内核过滤路径 | 同目录 `conntrack_filter.go` — `newFilteredDumpRequest`、`deleteFilteredHostConntrack` |
| 常驻 socket | 同目录 `conntrack_handles.go` |
| 调用时机与计时 | `internal/sandbox/checkpoint.go` — `RollbackInPlace`；`internal/sandbox/conntrack_flush.go` — `startConntrackFlush`、`join`、`recordConntrackReport` |
| 代理连接池丢弃 | `internal/checkpoint/service.go` — `beginRestore`、`SetConnectionDropper` |
| 等 envd 与墙钟校回 | 同上 — `finishRestore`、`envdRestoreTimeout` |

---

## 本章要点

1. 原地回滚特有的问题有统一形式：**从活对象推导出来、又没进快照的状态**，以及**宿主上记得 guest 的状态**。
2. **RX 描述符缓存**：环页回滚了、缓存没有，第一个完成的帧让 guest 驱动认定环损坏。修法是清空缓存、退回 `next_avail`、重新解析；
   修法本身受 seccomp 约束，只能原地清空。
3. **VMGenID 必须在内存回滚之后刷新**，且写新代号。
4. **连接跟踪**：命名空间表整表 flush，宿主表只删本 slot 地址的表项；宿主侧由清扫器攒批，每批一次原始报文整表扫描，
   单地址大表才按代价模型改走内核过滤；清理在 pause 后启动、与回滚并行、resume 前 join。代理连接池在 pause 前丢弃。
5. **时间**：单调时钟倒退，墙钟由 envd 校回，活性判据不能用时间。
6. **队列页要重新标脏**，否则下一代 Diff 漏页。
7. 这些问题的共同教训是一张检查表（§8）：给回滚路径加设备、加代码时逐条过一遍，比事后在挂死的沙箱上找原因便宜得多。
