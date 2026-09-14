# 12 · 原地回滚特有的问题

> 把状态写回一个**活着**的进程，和从零**构造**一个新进程，会遇到完全不同的问题。
> 本篇讲四个「重建路线永远不会遇到」的坑：网络 RX 描述符缓存、VMGenID 的次序、
> 宿主的连接跟踪、以及时间。每一个都花了不少时间才找到，其中两个的表现是**挂死而不是报错**。
>
> **读者**：工程师。要在这套方案上继续开发的人应当读完本篇。
> **预备**：[第 11 篇 · 进程内原地回滚](11-in-place-rollback.md)。
> **代码**：`src/vmm/src/rollback.rs`、`src/vmm/src/devices/virtio/net/device.rs`、
> `internal/sandbox/network/conntrack.go`

---

## 0. 本篇要回答的问题

1. 为什么原地回滚会有「特有」的问题？有没有一个判据能提前找出它们？
2. 一个网络描述符缓存怎么会让 RX 永久卡死？
3. 为什么宿主内核的连接跟踪表必须在暂停窗口内清？
4. guest 恢复之后，它的时钟是什么样的？这对使用方意味着什么？

---

## 1. 为什么会有「特有的」问题

重建路线从快照**构造**一个新的设备对象。构造函数保证了对象内部自洽 ——
它的每一个字段要么来自快照，要么是新建时的初值。

原地路线不构造对象，它**修改**既有对象。于是出现了一类新东西：

> **那些从活对象里推导出来、又没有进快照的状态。**

它们在重建路线下不存在（新对象里根本没有这些推导结果），在原地路线下却留了下来 ——
而且是按**被丢弃的那条时间线**推导出来的。

这个判据可以主动用：翻一遍设备对象的字段，凡是「缓存」「已解析」「上次的」这类语义，
又不在 `save_state` 序列化范围内的，都是嫌疑。下面第一个坑正是这么一个字段。

同类的还有第二种：**宿主上那些"记得 guest 的事"的状态** —— 内核的连接跟踪表、
代理的连接池。它们不属于虚机，快照里当然没有，但 guest 一回滚它们就全错了。

---

## 2. 网络 RX 描述符缓存

### 2.1 现象

回滚成功，虚机跑起来了，guest 里一切正常 —— 直到需要收网络包。
然后 RX 方向**永久卡死**，guest 内核日志里一行：

```
virtio_net: id N is not a head!
```

不是丢包、不是慢，是彻底不动了。而且不是每次回滚都出现。

### 2.2 背景：virtio-net 的 RX 是怎么工作的

virtio 队列有两个环：guest 把「可用的缓冲区」放进 **avail 环**，
设备处理完之后把结果放进 **used 环**。RX 方向上：

1. guest 驱动准备一批空缓冲区，把它们的描述符 id 写进 avail 环；
2. 设备（Firecracker 的 net 设备）从 avail 环取出这些描述符链，**解析**成可以直接写的
   iovec，缓存在自己的 `rx_buffer.parsed_descriptors` 里；
3. tap 上来了一个帧，设备从缓存里取一个 iovec 写进去，然后往 used 环里放一条
   `(描述符 id, 长度)`；
4. guest 驱动从 used 环取出，按 id 找到自己那块缓冲区。

第 2 步的**缓存**是性能优化：不必每来一个帧就重新走一遍环。

### 2.3 根因

回滚时：

- **avail 环的内容**（在 guest 内存里）被回滚到目标时刻；
- **队列索引** `next_avail` 被 `apply_one_device` 按快照重置；
- 但设备对象里的 `rx_buffer.parsed_descriptors` **没人动它** ——
  它还装着按**被丢弃的时间线**解析出来的描述符链。

于是第 3 步用一个已经不存在的描述符 id 写了一条 used 项。
guest 驱动拿到这个 id，去自己的表里查，查不到 —— 它没有发出过这个描述符。
驱动认为环已经损坏，拒绝继续处理 RX 队列。**永久卡死。**

不是每次都出现，因为要恰好满足：回滚时缓存里有已解析的链，且回滚后很快就有帧到达。

### 2.4 为什么重建路线不会遇到

新建的 net 设备，`rx_buffer` 是空的。它会从（刚加载好的）avail 环重新解析一遍。
构造函数天然做对了这件事。

### 2.5 修法

`apply_net_rx_cache` 在应用完设备状态之后，把这一步补上：

```rust
self.rx_buffer.iovec.clear();
self.rx_buffer.parsed_descriptors.clear();
self.rx_buffer.used_descriptors = 0;
self.rx_buffer.used_bytes = 0;
self.rx_buffer.min_buffer_size = 0;

self.queues[RX_INDEX].next_avail -= parsed_descriptor_chains_nr;  // 把已解析的那些退回去
self.parse_rx_descriptors();                                      // 从（已回滚的）avail 环重新解析
self.rx_buffer.used_descriptors = used_descriptors;
self.rx_buffer.used_bytes = used_bytes;
```

`parsed_descriptor_chains_nr`、`used_descriptors`、`used_bytes` 三个计数来自快照里
net 设备的 `rx_buffers_state` —— 上游 Firecracker 的快照格式里本来就有它们，
用于重建路线。原地路线只是把同样的信息用在了活对象上。

### 2.6 一个连带的约束：seccomp

注意上面是 `clear()` 而不是「换一个新的 `RxBuffers`」。代码注释解释了为什么：

```rust
// Cleared in place rather than rebuilt: a fresh RxBuffers would mmap a
// new IovDeque, and the vmm thread's seccomp filter has no
// memfd_create rule — that syscall is only ever made before the
// filters are installed.
```

`IovDeque` 底层用 `memfd_create` + 双重映射实现环形缓冲。而 `memfd_create` 只在
**seccomp 过滤器安装之前**（初始化阶段）被调用过，所以 VMM 线程的白名单里没有它。

> 这是「原地」这个约束的一个很具体的体现：**运行期能做的事，比初始化期少。**
> 重建路线在一个还没装过滤器的新进程里，什么都能做。
>
> 同类的还有回滚路径用到的那些 vCPU ioctl —— 它们必须在 vCPU 线程执行
> （[第 11 篇 §3.6](11-in-place-rollback.md#36-阶段-5vcpu)），以及
> 「取脏页位图」路径需要放行 vmm 线程的 `pread64`。改动回滚路径时，
> **凡是新增系统调用，都要同步检查 seccomp 白名单**。

---

## 3. VMGenID 的次序

VMGenID 是一个 ACPI 设备，在 guest 物理内存里放着一个 128 位的代号。
guest 内核会读它，代号变化意味着「你被从一个快照恢复了」，
于是重新播种随机数生成器等对时间单调性有依赖的东西。

**它就在 guest 内存里。**

所以如果在阶段 4（内存回写）**之前**刷新代号：

```
写入新代号  →  内存回滚  →  新代号被目标时刻的旧值覆盖
```

guest 醒来看到的是它熟悉的那个代号，完全不知道时间被回拨了。

修法就是把它放在阶段 8 —— 内存回写之后：

```rust
// ── Phase 8: VMGenID. The guest must learn that time was rewound; a new
// generation (never the snapshot's) is written after the memory revert so
// the revert cannot clobber it. ──
```

括号里那句「never the snapshot's」也重要：写的是一个**新**代号，不是快照里那个。
guest 需要知道的是「你被回滚了」这个事实，而不是「你回到了那个曾经的时刻」——
后者会让 guest 以为自己一直在那个时刻，从而不做任何重播种。

> **这一类问题的通用形式**：任何「要通知 guest 的信息」，如果通过 guest 内存传递，
> 就必须写在内存回滚之后。目前只有 VMGenID 一个，但加新的通知机制时要记得这条。

---

## 4. 连接跟踪

### 4.1 现象

回滚成功，网络看起来是通的（能发新连接），但**跨越回滚的那些连接全部挂死** ——
不是 RST、不是超时报错，是发出去的包石沉大海。

这是四个坑里最难排查的：**挂死比失败难查得多**，因为没有任何错误信息。

### 4.2 根因

宿主的 Linux 内核有一张**连接跟踪表**（conntrack），记录着流经它的每一条连接的状态：
五元组、TCP 序号窗口、连接阶段。NAT 依赖它。

回滚之后：

- **guest 忘记了**这些连接 —— 它的内存回到了连接建立之前（或另一个阶段）；
- **宿主内核还记得**，包括 TCP 序号已经走到哪里了。

于是 guest 发出的包，序号在宿主内核看来是**倒退的**，落在跟踪窗口之外。
内核判定为非法包，**直接丢弃**。guest 侧看到的就是「发出去了，没有回应」。

### 4.3 两张表，两种清法

沙箱的流量经过两张表，处理方式不同：

| 表 | 内容 | 清法 | 为什么 |
|---|---|---|---|
| 沙箱网络命名空间内的表 | **只有**这个沙箱的流 | 整表 flush | 全都是要清的 |
| 宿主的表 | 本节点**所有**沙箱的流 | **按本 slot 的 IP 过滤删除** | 整表 flush 会打断别的沙箱 |

宿主侧的过滤有个细节值得说：

```go
// One filter per direction rather than one filter with both: conditions
// within a filter must all match, while ConntrackDeleteFilters deletes a
// flow matching any of the filters it is given.
for _, direction := range []netlink.ConntrackFilterType{
    netlink.ConntrackOrigSrcIP, netlink.ConntrackOrigDstIP,
} {
    filter := &netlink.ConntrackFilter{}
    filter.AddIP(direction, s.HostIP)
    filters = append(filters, filter)
}
```

一个过滤器内部的条件是**与**关系，多个过滤器之间是**或**关系。
出向流量到达宿主时已经 SNAT 成 slot 地址（出现在 OrigSrc），
入向流量的目的地是 slot 地址（出现在 OrigDst）。所以两个方向各一个过滤器，
两者取或，恰好覆盖这个沙箱的全部流。

写成一个带两个条件的过滤器，语义会变成「源和目的**同时**是 slot 地址」，一条也匹配不上。

### 4.4 时机：必须在暂停窗口内

清表要在虚机暂停、还没恢复的那个窗口里做：

> 此刻**没有流量**，所以不可能有人重建一条刚刚被作废的表项。

如果放在恢复之后，就有一个竞态窗口：guest 已经在跑、可能已经发出了包，
而我们正在删的表项可能是它刚建立的。

### 4.5 代理侧的连接池

同样的道理适用于 orchestrator 自己的代理：它对每个沙箱维护一个连接池。
回滚之后，池子里那些还开着的连接，对端（guest 里的服务）已经不记得它们了。

```go
if s.dropConnections != nil {
    if err := s.dropConnections(sbx.LifecycleID); err != nil { ... }
}
```

这一步在**恢复之后**做（它是 orchestrator 自己的状态，不涉及内核竞态），
失败只记警告 —— 连接池里的坏连接会在下次使用时报错并被剔除，不是致命问题。

---

## 5. 时间

回滚会把 guest 的时间**拨回快照时刻**。这不是 bug，是快照语义的一部分，
但它有几个不那么显然的后果。

### 5.1 单调时钟真的会倒退

guest 的单调时钟（`CLOCK_MONOTONIC`）来自 vCPU 的计时器寄存器，
而那些寄存器在阶段 5 被恢复成了快照时刻的值。

所以对 guest 内的程序而言，**单调时钟倒退了**。这违反了 POSIX 对
`CLOCK_MONOTONIC` 的承诺，但在快照恢复的语义下这是唯一自洽的做法 ——
guest 相信自己一直在那个时刻，它的所有状态都与那个时刻一致。

### 5.2 墙钟由 envd 校回

墙钟（`CLOCK_REALTIME`）不能留在过去 —— 那会让 guest 里的一切时间戳都错。
恢复流程的最后一步是等 guest 里的 envd 应答，envd 的初始化会把墙钟校回当前时间：

```go
// The guest woke up with the clock of checkpoint time; envd init brings
// the wall clock back to now, same as the pause/resume path. Monotonic
// time staying rolled back is part of restore semantics.
if err := sbx.WaitForEnvd(ctx, envdRestoreTimeout); err != nil { ... }
```

注意这与 e2b 原生 pause/resume 路径的行为是**一致**的 —— 不是本方案特有的语义。

### 5.3 对验收脚本的影响

这条直接决定了怎么写正确性测试。一个自然的想法是「恢复后检查心跳进程还在跑」，
判据用「时间戳在推进」或者「tick 速率正常」。**两个都不能用**：

- **墙钟**：恢复瞬间会被 envd 拨回来，中间有一段跳变；
- **tick 速率**：单调时钟被拨回，任何基于它的速率计算都会得到荒谬的值。

`checkpoint_verify.py` 的活性判据因此只断言**心跳在推进**（计数器在增长），
不对时间做任何假设。判据怎么落到脚本里、为什么这一条是**先想清楚再写测试**的典型，
见[第 25 篇 §2.3](25-functional-tests.md#23-活体判据为什么不能用时间)。

> 反过来，时间倒退也是一个**有用的证据**：验收里心跳进程在三代之间保持
> **同一个 PID 和同一个启动时刻** —— 这证明了打快照没有重启虚机，
> 而且它是「回到那一刻」最硬的一条证据
> （[第 25 篇 §1](25-functional-tests.md#1-什么叫回到那一刻)；
> 实测结果见[第 28 篇 §2](28-results-and-compliance.md#2-功能正确性结果)）。

---

## 6. 队列页的脏标记

这是一个更隐蔽的问题，属于「回滚破坏了别的机制的前提」。

阶段 9 清空脏页跟踪，让下一代 Diff 相对刚恢复的状态。但 virtio 队列的环页
**在运行期被 Firecracker 自己写**（设备往 used 环里放条目），
这些写不经过 Stage-2 故障，KVM 不知道（[第 7 篇 §3.2](07-dirty-page-tracking.md#32-两层位图)）。

清空之后，位图里没有这些页。下一代 Diff 就会**漏掉**它们 ——
而漏记是会破坏[第 8 篇 §4.3](08-memory-diff-tree.md#43-正确性) 那个正确性证明的。

所以阶段 9 清完之后立刻补一步：

```rust
vmm.mmio_device_manager.for_each_virtio_device(|_, _, _, dev| {
    let d = dev.lock().unwrap();
    if d.is_activated() { d.mark_queue_memory_dirty(vmm.vm.guest_memory()) } else { Ok(()) }
})?;
```

只对**已激活**的设备做 —— 未激活的设备还没有队列内存可标。

---

## 7. 排查手段

前两个坑（RX 缓存、conntrack）的共同点是：**表现为挂死，且沙箱一旦销毁就什么都不剩**。
所以回滚路径里加了一步逐设备的队列诊断日志：

```rust
info!("rollback: device {id} (type {ty}) q0 avail=… next_avail=… next_used=… pending=… \
      | rx_buffer parsed=… used_desc=… used_bytes=…");
```

avail / used 是**按设备读取环的方式**从（已回滚的）guest 内存里读出来的，
所以这一行就是「设备和 guest 分别认为进行到哪里了」的快照。两者对不上就是问题所在。

pause 路径上也输出同样的诊断，这样可以对比「回滚前」与「回滚后」。

> 这类日志的价值在于**罕见故障只有一次现场**。一个几百次才复现一次的挂死，
> 如果现场只有「沙箱没反应」，基本无从下手。

---

## 8. 检查表：还有哪些地方可能有同类问题

给要继续开发的人。加新设备、改回滚路径时，逐条过一遍：

| 问 | 如果是 |
|---|---|
| 这个设备对象里有没有「从 guest 内存推导出来的缓存」？ | 回滚后必须重建它（如 RX 描述符缓存） |
| 有没有「上次操作到哪」的游标，且不在 `save_state` 里？ | 同上 |
| 有没有要通知 guest 的信息经由 guest 内存传递？ | 必须写在内存回滚**之后**（如 VMGenID） |
| 这个设备在运行期会不会自己写 guest 内存？ | 那些页要在阶段 9 之后重新标脏 |
| 宿主上有没有「记得这个 guest 的状态」的东西？ | 要在暂停窗口内清掉（如 conntrack） |
| 新代码有没有引入新的系统调用？ | 检查 seccomp 白名单（VMM 线程 / vCPU 线程各一份） |
| 有没有在途的异步 I/O？ | 阶段 2 要排空它 |
| 新设备支持热插拔或动态配置吗？ | `validate_topology` 要能表达这件事，否则应当拒绝 |

---

## 9. 小结

1. 原地回滚特有的问题有一个统一形式：**从活对象推导出来、又没进快照的状态**，
   以及**宿主上记得 guest 的状态**。重建路线因为从零构造，天然没有这两类。
2. **RX 描述符缓存**：环页被回滚了，缓存里的描述符 id 没有。第一个完成的帧就会让
   guest 驱动认定环损坏，RX 永久卡死。修法是清空缓存、退回 `next_avail`、从回滚后的环重新解析。
3. 修法本身受**运行期约束**：不能重建 `RxBuffers`，因为那要 `memfd_create`，
   而 VMM 线程的 seccomp 白名单里没有它。**运行期能做的事比初始化期少。**
4. **VMGenID 必须在内存回滚之后刷新**，否则新代号被回滚数据覆盖，guest 不知道时间被拨回。
5. **连接跟踪**必须在暂停窗口内清：命名空间内整表 flush，宿主表按 slot IP 双向过滤删除
   （不能整表 flush，那会打断别的沙箱）。放在恢复之后清会有竞态。
6. **时间**：单调时钟真的会倒退，墙钟由 envd 校回。验收脚本的活性判据因此不能用时间。
7. **队列页要重新标脏**，否则下一代 Diff 会漏页 —— 而漏页会破坏回滚集的正确性证明。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| RX 缓存重建（调用侧） | `src/vmm/src/rollback.rs` — `apply_net_rx_cache` |
| RX 缓存重建（设备侧） | `src/vmm/src/devices/virtio/net/device.rs` — `rollback_rx_buffers`、`parse_rx_descriptors` |
| VMGenID 刷新与位置 | `src/vmm/src/rollback.rs` — 阶段 8 |
| 队列页重新标脏 | 同上 — 阶段 9 |
| 队列诊断日志 | 同上 — `log_queue_diagnostics` |
| 连接跟踪清理 | `internal/sandbox/network/conntrack.go` — `FlushConntrack`、`flushNamespaceConntrack`、`deleteHostConntrack` |
| 清表的调用时机 | `internal/sandbox/checkpoint.go` — `RollbackInPlace` 里 resume 之前那一步 |
| 代理连接池丢弃 | `internal/checkpoint/service.go` — `dropConnections` |
| 等 envd 与墙钟校回 | 同上 — `WaitForEnvd`、`envdRestoreTimeout` |

**下一篇**：[13 · 端到端](13-end-to-end.md) —— 把第 6 到 12 篇串起来，
按时间顺序完整走一遍 create 与 restore。
