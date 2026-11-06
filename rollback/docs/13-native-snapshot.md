# 13 · 原生快照的机制与成本

原生 pause/resume 服务于保存和重建沙箱，与活沙箱原地回滚承担不同任务。本章先完整解释原生路径，为精确增量改造和两类快照的配合建立基础。

## 1. 两个 RPC

| RPC | 做什么 | SDK 里对应 |
|---|---|---|
| `Pause` | 打快照 + **停掉沙箱** | `sb.pause()` |
| `Checkpoint` | 打快照 + 停掉沙箱 + **从新快照重新拉起一个** | `create_snapshot()` |

两者共用 `snapshotAndCacheSandbox`（`internal/server/sandboxes.go:581`；`Server.Pause` 在 :380，`Server.Checkpoint` 在 :444），
区别只在之后要不要再拉起一个。两者都在函数开头 `defer s.stopSandboxAsync(...)`（:400、:469）：旧沙箱一定会被停掉。
`Pause` 打完快照就结束，产物上传进存储，这是"沙箱离场"的语义。`Pause` 会等 `waitForUpload` 完成才返回（:411）：快照元数据不能先于数据标记为就绪。所以客户端看到的 `pause` 耗时包含停沙箱、导出和上传，不只是冻结虚机。

`Checkpoint` 拉起新沙箱时，三个 id 的变化是：

| id | 语义 | `Checkpoint` 之后 |
|---|---|---|
| `SandboxID` | 对外的沙箱标识 | **不变** |
| `ExecutionID` | 一次"执行"的标识，给 API、路由目录、分析用 | **不变** |
| `LifecycleID` | 一个 Firecracker 进程实例的标识 | **换新** |

`LifecycleID` 换新的理由写在代码注释里（`sandboxes.go:488-491`）：老沙箱的清理协程用自己的 `LifecycleID`
调 `RemoveByLifecycleID` 删表项，新沙箱若沿用同一个 id 就会被误删。

> 所以"snapshot 之后沙箱 id 不变"成立，但**底下已经是一个全新的 Firecracker 进程**：
> 新进程要重建 KVM VM、vCPU、GIC、设备、tap、UFFD，然后靠缺页把工作集换回内存。
> 这也是原生成本与虚机规格挂钩的根源 —— 它每次都在"重建"（[第 2 章 §6.4](02-background.md#64-恢复的两种形态重建与原地回写) 的第一种形态）。

## 2. 打快照：`Sandbox.Pause` 的步骤

`Sandbox.Pause`（`internal/sandbox/sandbox.go:912`）的主干，按执行顺序：

```go
s.Checks.Stop()                                                     // ① 停掉健康检查
s.process.Pause(ctx)                                                // ② 暂停虚机
s.process.CreateSnapshot(ctx, snapfile.Path())                      // ③ 让 Firecracker 写出 vmstate
memfileDiffMetadata, _ := s.Resources.memory.DiffMetadata(ctx, s.process) // ④ 取内存脏页集合
memfileDiff, memfileDiffHeader, _ := pauseProcessMemory(...)        // ⑤ 导出内存增量
rootfsDiff, rootfsDiffHeader, _ := pauseProcessRootfs(...)          // ⑥ 导出磁盘增量
m.ToFile(metadataFileLink.Path())                                   //   写元数据
```

| 步 | 说明 |
|---|---|
| ① | 快照期间健康检查会失败，先停掉 |
| ② | 暂停虚机，此后 guest 不再产生新状态 |
| ③ | Firecracker 写出 `snapfile`：vCPU、GIC、设备状态。**不含内存** |
| ④ | 问内存后端（UFFD）"哪些页算脏"（[§3.1](#31-判据uffd-的写保护位)、[§3.2](#32-arm-适配基线上的退化)） |
| ⑤ | 按脏页集合把内存从 Firecracker 进程地址空间**拷出来**（[§3.3](#33-搬运从进程地址空间拷出)） |
| ⑥ | 把磁盘写层导出成紧凑 diff —— **这一步会关掉沙箱**（[§4](#4-磁盘增量)） |

**注意 ② 之后一直没有 resume。** 原生路径的语义就是"沙箱到此为止"；`Checkpoint` 里的"继续跑"是另起一个新沙箱实现的。

## 3. 内存增量

### 3.1 判据：UFFD 的写保护位

内存由 UFFD 托管（[第 2 章 §5.3](02-background.md#53-内存是按需拉取的)）。上游 x86 的做法是在填页时区分读和写：

- 因**读**缺页而填的页 → 带 `UFFDIO_COPY_MODE_WP`，页仍是写保护的；
- 因**写**缺页而填的页 → 不带，页可写；
- 之后 guest 写一个"因读而填"的页 → 再触发一次写保护故障 → handler 清掉 WP 位。

于是 `/proc/self/pagemap` 里那一页的 **bit 57（uffd 写保护）** 就是一个准确的脏页信号：

```
脏  ⟺  present（bit 63）= 1  且  uffd-wp（bit 57）= 0
```

Firecracker 的 `GET /memory/dirty`（`firecracker/src/vmm/src/lib.rs:882` `get_dirty_memory`）就是按这条判据出位图的：
先用 `mincore` 筛出常驻页，再只对常驻页读 pagemap。代价是读缺页多走一次写保护故障（如果后来确实被写了），收益是判据**精确** ——
只读不写的页不算脏。

### 3.2 ARM 适配基线上的退化

ARM 适配版把填页时带写保护位的那几行注释掉了（`internal/sandbox/uffd/userfaultfd/userfaultfd.go:383-385`），于是每一次填页都清掉 WP 位，
无论触发它的是读还是写。bit 57 恒为 0，判据塌缩成"常驻即脏"——**读也算脏**。

叠加 [§1](#1-两个-rpc) 的"每次 `Checkpoint` 都重建沙箱、新进程的工作集要重新缺页换入"，得到一个反直觉的结果：
**即使两次 snapshot 之间什么都没做，增量的下限也约等于常驻工作集。**

必须说清：这是 ARM 适配引入的退化，不是 e2b 的设计问题 —— x86 上 e2b 的增量是精确的。
这个判据差异（"读也算脏"）的完整定义与推导在 [第 5 章](05-dirty-tracking-and-hdbss.md)，全书只在那里定义。

**本书交付的代码里，原生 pause 的这条判据已经修复**：跟踪开着时，`Uffd.DiffMetadata`（`internal/sandbox/uffd/uffd.go`）
改用 Firecracker 的写跟踪位图按 4 KiB 页判脏（做过 checkpoint 的沙箱还要并入"自启动以来脏页集"），跟踪关着、集合不可信或 Firecracker 没有导出端点时，才退回按常驻页判（[第 14 章 §10](14-native-precise-increment.md#10-退路什么时候回到驻留判据)）。为什么"只换判据"还不够、
存储和缺页填充两层还要怎么改、代价与验证，**第三篇（[第 14 章](14-native-precise-increment.md)–[第 15 章](15-native-cooperation-and-cost.md)）专门讲**。
本章讲的是修复前的机制，它仍是理解原生成本模型的基础。

### 3.3 搬运：从进程地址空间拷出

拿到脏页集合后，`pauseProcessMemory`（`sandbox.go:1056`）经 `fc.Process.ExportMemory`（`internal/sandbox/fc/memory.go:116`）
**直接读 Firecracker 进程的内存**：`block.NewCacheFromProcessMemory`（`internal/sandbox/block/cache.go:756`）把脏区间组织成
`unix.RemoteIovec` 数组，批量调 `process_vm_readv`（:848），按 `IOV_MAX` 和单次读写上限分批，读进本地的 cache 文件。

这是一次真实的数据搬运：脏了多少就拷多少，从一个进程的地址空间到另一个进程的文件。

### 3.4 产物：diff + 链式 header

内存增量的产物是**紧凑**的：只含脏块，按顺序排列。要定位某个块，靠一张映射表。

`DiffMetadata.ToDiffHeader`（`packages/shared/pkg/storage/header/metadata.go:52`）把"原 header + 本次脏块集合"合成新 header：

```
新 build 的 header
  ├── 本次写过的块  →  指向本次的 diff 文件
  └── 没写过的块    →  指向上一代 build（可能再往上指）
```

所以 header 是**链式**的：一个 build 的 header 里，不同区间可能指向不同代的 build。恢复时按 header 逐块寻址。

## 4. 磁盘增量

### 4.1 `ExportDiff` 的步骤

原生导出磁盘增量走 `NBDProvider.ExportDiff`（`internal/sandbox/rootfs/nbd.go:72`）：

1. **摘出写层**：`Overlay.EjectLayers`，Overlay 就此作废；
2. **停沙箱**：另起一个协程调传进来的 `closeSandbox`，然后等 NBD 设备释放；
3. **逐块导出**：对写层里每个写过的块调 `DiffMetadataBuilder.Process`（`header/metadata.go:135`）——
   全零块只在 `empty` 位图里记一位、不写数据；非零块紧凑追加进输出流；
4. **关闭写层**。

所以产物很小，但"存储偏移 ≠ 设备偏移"，必须靠映射表换算。

（沙箱如果打过本方案的 checkpoint，写层已经被封存过若干次，当前写层只含最近一次之后的写，这时 `ExportDiff` 会把全部层压平后导出；
这是本方案为兼容原生 pause 做的处理，见 [第 7 章](07-disk-layering.md)。）

### 4.2 "必须停沙箱"的硬证据

这不是设计取向，是代码结构决定的。`Sandbox.Pause` 传给 `pauseProcessRootfs` 的 `DiffCreator` 是
（`sandbox.go:1015-1018`、`internal/sandbox/diffcreator.go:15-22`）：

```go
&RootfsDiffCreator{
    rootfs:    s.Rootfs(),
    closeHook: s.Close,        // ← 沙箱自己的 Close
}

func (r *RootfsDiffCreator) process(ctx context.Context, out io.Writer) (*header.DiffMetadata, error) {
    return r.rootfs.ExportDiff(ctx, out, r.closeHook)
}
```

**导出磁盘增量这个动作，参数之一就是"关掉沙箱"。** 为什么必须这样：

- 摘出写层之后 Overlay 作废，**之后任何写都没有去处**；
- 在写层的 mmap 上遍历必须没有并发写，否则导出的是一个"一半新一半旧"的镜像；
- NBD 设备要释放，写层文件才能关闭。

对"沙箱离场"这个场景，这完全合适 —— 反正沙箱也要停。**对高频回退则完全不能接受**，这是本方案要另辟蹊径的直接原因
（本方案的做法：写层原地封存，不搬数据、不停沙箱，见 [第 7 章](07-disk-layering.md)）。

## 5. 恢复：`ResumeSandbox`

客户端对已暂停的沙箱调用 `Sandbox.connect()` 即触发 resume，服务端走 `Factory.ResumeSandbox`（`sandbox.go:424`）：

```
新建 Firecracker 进程
   ├── 建 KVM VM、vCPU、GIC、virtio 设备
   ├── 注册 eventfd / irqfd / ioeventfd
   ├── 建 tap、分配网络槽位
   ├── 起一个 UFFD handler，挂上 guest 内存
   ├── 建 NBD 设备 + Overlay（模板 rootfs + 新写层）
   └── 加载 snapfile：恢复 vCPU、GIC、设备状态
跑起来
   └── guest 每访问一个还没有内容的页 → 缺页 → UFFD handler
        → 查本地缓存 → 没有就按 header 从对象存储拉那一块
```

成本落在两处：

| 成本 | 说明 |
|---|---|
| 建一台虚机的固定开销 | 进程、KVM、设备、网络、NBD —— 与虚机规格弱相关 |
| **把工作集换回内存** | 与虚机规格**强相关**，与"回退了多少"**完全无关** |

第二项是大头。一个刚恢复的沙箱在头几秒里，几乎每次访问新页都可能是一次缺页，要经用户态 handler 从文件或对象存储取回。

e2b 为此做了 **prefetch**：记录上一次运行时的缺页顺序，下次恢复时按这个顺序提前拉（`ResumeSandbox` 里读 `meta.Prefetch`）。
这能缓解，但改变不了成本模型 —— 要换回的量仍然取决于工作集有多大。

## 6. 成本模型

| 阶段 | 成本正比于 |
|---|---|
| 打快照 · vmstate | 常数（几十 KB） |
| 打快照 · 内存 | 脏页量（x86）/ **常驻工作集**（ARM 适配基线，修复前） |
| 打快照 · 磁盘 | 脏块量，**且必须停沙箱** |
| 恢复 · 构建 | 虚机规格（弱） |
| 恢复 · 换页 | **虚机工作集**（强） |
| 上传 / 下载 | 增量大小 + 网络 |

一句话：**原生方案的主要代价来自销毁和重建虚机，改动量只决定其上的增量部分。** 即使内存判据修成精确的（第三篇），
"每次打快照都停掉沙箱、每次恢复都重建进程并换回工作集"这两项不变 —— 它们来自"沙箱离场"这个语义本身。

修复后的分档实测（950 见 [第 18 章 §9](18-950-performance.md#9-原生-pause--resume-分档实测修复后)，920B 见 [附录 B §13.4](B-920b-results.md#134-native_snapshot_tierspy每档独立沙箱)）量化了这句话：

| 950，客户端墙钟 p50 | 0 MiB 档 | 256 MiB 档 | 规律 |
|---|---|---|---|
| pause | 251.9 ms | 429.9 ms | 约 246 ms 固定开销 + 每 MiB 导出量约 0.52 ms（按六档 p50 拟合） |
| resume | 109.3 ms | 120.7 ms | 与档位基本无关 |

下面两节按阶段说明这些时间花在哪里。拆分来自 920B（`kvm-wp`，产物在 tmpfs）的单样本跟踪，用来说明结构；950 没有做同样的逐段测量，各段数值不能直接套到 950。

### 6.1 pause：改动量为 0 也要约 250 ms

0 MiB 档总耗时 234.9 ms 的一次 pause，时间点从 `Sandbox.Pause` 开始计：

| 时间点 ms | 发生的事 | 依据 |
|---|---|---|
| 0～约 10 | Firecracker 暂停约 0.08 ms；写 `snapfile` 约 3.2 ms；取脏页位图 0.67 ms；约 9.6 ms 建好内存差分文件 | Firecracker 日志、文件事件 |
| 约 12 | 建根盘差分文件，进入 `ExportDiff`，开始停沙箱 | 文件事件 |
| 约 12～169 | Firecracker 收到 SIGTERM 后退出，168.8 ms 时进程结束 | 进程退出时间；内部分段见下 |
| 169～232 | `doStop` 收尾（删 cgroup、停 uffd）；`NBDProvider.Close` 依次刷盘、断开 NBD 设备；`ExportDiff` 导出根盘差分 | 代码顺序、文件事件。这段没有单独计时 |
| 232～235 | 上传到本地存储并返回 | 文件事件 |

**真正保存内存状态的只有前约 10 ms，其余约 225 ms 都在销毁虚机环境并等它结束。** 根盘差分要等 NBD 设备释放后才能导出（[§4.1](#41-exportdiff-的步骤)），`Pause` 又要等上传完成，所以这段等待全部计入 `pause`。

对 Firecracker 进程做内核栈采样的另一次运行（总耗时 289 ms，进程在 220.9 ms 消失；采样本身拖慢了退出，时间点只能近似参考）显示退出过程的内容：

| 时间点 ms | 内核栈 | 含义 |
|---|---|---|
| 约 9～123 | 主线程在运行，栈不可读 | **未归因** |
| 123.4 | `synchronize_rcu_expedited` ← `namespace_unlock` ← `drop_collected_mounts` ← `put_mnt_ns` ← `free_nsproxy` | 进程退出时释放 mount 命名空间，等 RCU 宽限期 |
| 137 | `cleanup_mnt` | 卸载挂载 |
| 140.5 | `__flush_workqueue` ← `kvm_irqfd_release` ← `kvm_vm_release` | 关闭 KVM 虚机 fd，等 irqfd 清理完成 |
| 149.4 | `__wait_rcu_gp` ← `synchronize_rcu` ← `kvm_destroy_vm` | 销毁 KVM 虚机，等 RCU 宽限期 |

这次采样里约 114 ms 的未归因段是退出过程中最长的一段，要继续优化 pause 应从这里查起。

**改动量增大时的变化。** 同样的跟踪在 256 MiB 档（总耗时 443 ms）得到：约 61 ms 建好内存差分文件，88.8 ms 建根盘差分文件，280 ms 进程退出，368.9～440.1 ms 写根盘差分。与 0 MiB 档相比：

| 阶段 | 0 MiB 档 | 256 MiB 档 | 增长 |
|---|---|---|---|
| 开始停沙箱之前（含内存差分导出） | 12.4 ms | 88.8 ms | 约 76 ms |
| 开始停沙箱到 Firecracker 退出 | 约 156 ms | 约 191 ms | 约 35 ms |
| Firecracker 退出到根盘差分导出完成 | 约 63 ms | 约 160 ms | 约 97 ms，其中写根盘差分约 71 ms（64.5 MiB） |

所以增长不全是拷数据，进程退出本身也慢约 35 ms。这是单样本的比较，只说明方向。

### 6.2 resume：各档都在 100 ms 上下

一次 `Sandbox.connect()` 耗时 92.6 ms、第一条命令 97.9 ms 完成的 resume，时间点从客户端发起调用开始计：

| 时间点 ms | 发生的事 | 依据 |
|---|---|---|
| 0～35 | 请求经 API 到节点（约 +3 ms 开始建 cgroup），建网络槽位、NBD、uffd，启动 Firecracker 进程（约 +27 ms 进程起来，+28 ms API 就绪） | cgroup 与 Firecracker 日志、代码顺序 |
| 35～57 | 加载 `snapfile`（`load snapshot` 21.3 ms） | Firecracker 日志 |
| 57～58 | 恢复 vCPU（`resume vm` 0.9 ms） | Firecracker 日志 |
| 58～93 | 等 envd 就绪和 API 返回 | 这段未细拆 |
| 93～98 | 第一条命令执行 | 客户端计时 |

这些步骤都不搬运内存，也不拷根盘：内存由 uffd 按缺页加载，根盘由 Overlay 按需读差分，所以 resume 耗时**不随档位变化**。

成本只是推迟了。950 上恢复后第一次读 192 MiB 内存负载需要 702.6 ms，快照前同一读法 590.9 ms，多出约 112 ms 不计入 resume 耗时，而由 guest 里第一个访问这些页的程序承担。

### 6.3 与 checkpoint / restore 的差别

本方案 checkpoint 不停虚机，也不销毁任何资源；restore 把内存写回活着的 Firecracker 进程（[第 9 章](09-in-place-rollback.md)）。§6.1、§6.2 里进程退出、KVM 销毁、NBD 断开、进程启动、加载 `snapfile` 这些固定步骤都不会发生，所以 950 上 0 MiB 档 checkpoint / restore 墙钟 p50 是 17.2 / 22.4 ms。

这不是"原生实现得慢"：原生要产出能离开这个进程、这台宿主的自足快照，停掉并重建进程是这个目标的一部分（[§7](#7-它擅长什么)）。

## 7. 它擅长什么

这套设计对它的目标场景是**合适**的：

| 场景 | 为什么合适 |
|---|---|
| **跨节点迁移** | 产物自足、进对象存储，任何节点都能加载 |
| **长期持久化** | 同上，且不依赖任何活体资源 |
| **进程崩溃后恢复** | 恢复本来就是"新建进程加载"，进程死不死无所谓 |
| **沙箱离场后再回来** | 停掉沙箱本来就是这个场景的一部分 |
| 低频使用 | 一个沙箱一两次，固定开销摊得开 |

它**不擅长**的恰好是本方案的目标场景：高频、低延迟、沙箱不能中断。这不是实现缺陷，是**成本模型和场景不匹配**。
所以本书从头到尾不说"原生方案有缺陷"，只说"它的成本模型不适合这个场景"。

## 8. 代码位置

| 关注点 | 位置（`packages/orchestrator/` 下，另注明者除外） |
|---|---|
| 两个 RPC | `internal/server/sandboxes.go` — `Server.Pause`、`Server.Checkpoint`、`snapshotAndCacheSandbox` |
| 打快照主体 | `internal/sandbox/sandbox.go` — `Sandbox.Pause` |
| 内存脏页判据 | `internal/sandbox/uffd/uffd.go` — `DiffMetadata`；`internal/sandbox/uffd/userfaultfd/userfaultfd.go` — 被注释的 `UFFDIO_COPY_MODE_WP` |
| 内存导出与进程内存拷贝 | `internal/sandbox/sandbox.go` — `pauseProcessMemory`；`internal/sandbox/fc/memory.go` — `ExportMemory`；`internal/sandbox/block/cache.go` — `NewCacheFromProcessMemory`、`copyProcessMemory` |
| 磁盘增量 | `internal/sandbox/diffcreator.go` — `RootfsDiffCreator`；`internal/sandbox/rootfs/nbd.go` — `ExportDiff` |
| 空块剔除、header 与映射 | `packages/shared/pkg/storage/header/metadata.go` — `DiffMetadataBuilder.Process`、`ToDiffHeader` |
| 恢复 | `internal/sandbox/sandbox.go` — `Factory.ResumeSandbox` |
| Firecracker 的驻留判据 | `firecracker/src/vmm/src/lib.rs` — `get_dirty_memory` |
