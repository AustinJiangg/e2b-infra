# 04 · e2b 原生 snapshot / resume

> 原生方案是本书的**基线与对照组**。不把它讲清楚，后面「我们改了什么、为什么改」就无从谈起。
> 本篇按代码路径走一遍：打一次快照发生了什么、内存和磁盘增量各自怎么做、
> 恢复时又付出了什么。
>
> **读者**：工程师。想了解一个真实快照系统怎么实现的人也适合读。
> **预备**：[第 3 篇 · 快照原理](03-snapshot-fundamentals.md)、
> [第 2 篇 · microVM 与 e2b](02-microvm-and-e2b.md)。
> **代码**：`internal/server/sandboxes.go`、`internal/sandbox/sandbox.go`、
> `internal/sandbox/{diffcreator,rootfs/nbd}.go`、`packages/shared/pkg/storage/header/`

---

## 0. 本篇要回答的问题

1. 原生打一次快照，从 RPC 到产物，具体经过哪些步骤？
2. 内存增量的判据是什么？数据是怎么搬出来的？
3. 磁盘增量为什么**必须停沙箱**？
4. 恢复时的成本落在哪里？
5. 它擅长什么、不擅长什么？

---

## 1. 两个 RPC

| RPC | 做什么 | SDK 里对应 |
|---|---|---|
| `Pause` | 打快照 + **停掉沙箱** | 暂停沙箱 |
| `Checkpoint` | 打快照 + 停掉沙箱 + **从新快照重新拉起一个** | `create_snapshot()` |

两者共用同一个 `snapshotAndCacheSandbox`，区别只在之后要不要再拉起一个。

`Checkpoint` 拉起新沙箱时，`SandboxID` / `ExecutionID` **保持不变**（对外身份稳定），
`LifecycleID` **换新**（否则老沙箱的清理协程会把新沙箱从表里删掉）。
细节见[第 20 篇 §1](20-vs-native.md#1-原生的两个-rpc)。

> 所以：**沙箱 id 不变，但底下已经是一个全新的 Firecracker 进程。**

---

## 2. 打快照：`Sandbox.Pause` 的六步

```go
s.Checks.Stop()                                          // ① 停掉健康检查
s.process.Pause(ctx)                                     // ② 暂停虚机
s.process.CreateSnapshot(ctx, snapfile.Path())           // ③ 让 FC 写出 vmstate
memfileDiffMetadata, _ := s.Resources.memory.DiffMetadata(ctx, s.process)   // ④ 取内存脏块集合
memfileDiff, memfileDiffHeader, _ := pauseProcessMemory(...)                // ⑤ 导出内存增量
rootfsDiff, rootfsDiffHeader, _ := pauseProcessRootfs(...)                  // ⑥ 导出磁盘增量
m.ToFile(metadataFileLink.Path())                                          //   写元数据
```

| 步 | 说明 |
|---|---|
| ① | 快照期间健康检查会失败，先停掉 |
| ② | 暂停虚机，此后 guest 不再产生新状态 |
| ③ | Firecracker 写出 `snapfile`：vCPU、GIC、设备状态。**不含内存** |
| ④ | 从内存后端（UFFD）问「哪些块脏了」 |
| ⑤ | 按脏块集合把内存从 Firecracker 进程地址空间**拷出来** |
| ⑥ | 把磁盘写层导出成紧凑 diff —— **这一步会关掉沙箱** |

**注意 ② 之后一直没有 resume。** 原生路径的语义就是「沙箱到此为止」。

---

## 3. 内存增量

### 3.1 判据：UFFD 的写保护位

内存由 UFFD 托管（[第 2 篇 §3.2](02-microvm-and-e2b.md#32-内存是按需拉取的)）。
上游 x86 的做法是：

- 因**读**缺页而填的页 → 带 `UFFDIO_COPY_MODE_WP`，保留写保护；
- 因**写**缺页而填的页 → 不带，页可写；
- 之后 guest 写一个「因读而填」的页 → 再触发一次写保护故障 → handler 清掉 WP 位。

于是 `/proc/self/pagemap` 里的 **bit 57（uffd 写保护）** 就是一个准确的脏页信号：

```
脏  ⟺  present（bit 63）= 1  且  uffd-wp（bit 57）= 0
```

> **在 ARM 适配版上这条路径被注释掉了**，判据因此塌缩成「常驻即脏」。
> 这是本方案要修复的东西 —— 见[第 7 篇 §5](07-dirty-page-tracking.md#5-脏页判据的三方差异)。

### 3.2 搬运：从进程地址空间拷出

拿到脏块集合后，orchestrator **直接读 Firecracker 进程的内存**：

```go
remote = append(remote, unix.RemoteIovec{ Base: uintptr(r.Start), Len: int(r.Size) })
// … 批量 process_vm_readv 进本地 cache 文件
```

用的是 `process_vm_readv`，按 `IOV_MAX` 和 `MAX_RW_COUNT` 分批。

**这是一次真实的数据搬运**：脏了多少就拷多少，从一个进程的地址空间到另一个进程的文件。

### 3.3 产物：diff + header

内存增量的产物是**紧凑**的：只含脏块，按顺序排列。要定位某个块，靠一张**映射表**。

`ToDiffHeader` 把「原 header + 本次脏块集合」合成新 header：

```
新 build 的 header
  ├── 本次写过的块  →  指向本次的 diff 文件
  └── 没写过的块    →  指向上一代 build（可能再往上指）
```

所以 header 是**链式**的：一个 build 的 header 里，不同区间可能指向不同代的 build。
恢复时按 header 逐块寻址。

---

## 4. 磁盘增量

### 4.1 `ExportDiff` 四步

```go
cache, err := o.overlay.EjectCache()      // ① 摘出写层，Overlay 就此作废
// ② 停沙箱，等 NBD 设备释放
for _, offset := range c.dirtySortedKeys() {
    block := (*c.mmap)[offset : offset+c.blockSize]
    builder.Process(ctx, block, out, offset)   // ③ 逐块处理
}
// ④ 关闭并删除写层文件
```

第 ③ 步的 `DiffMetadataBuilder.Process` 做两件事：

```go
isEmpty, _ := IsEmptyBlock(block, b.blockSize)
if isEmpty {
    b.empty.Set(uint(blockIdx))     // 全零块：只记一个 empty 位，不写数据
    return nil
}
b.dirty.Set(uint(blockIdx))
out.Write(block)                     // 非零块：紧凑追加进输出流
```

**全零块被剔除**（只占一个 bit），非零块紧凑排列。所以产物很小，
但 `storage offset ≠ device offset`，必须靠映射表换算。

### 4.2 「必须停沙箱」的硬证据

不是设计取向，是代码结构决定的。`pauseProcessRootfs` 传进去的 `DiffCreator` 是：

```go
&RootfsDiffCreator{
    rootfs:    s.Rootfs(),
    closeHook: s.Close,        // ← 沙箱自己的 Close
}

func (r *RootfsDiffCreator) process(ctx context.Context, out io.Writer) (*header.DiffMetadata, error) {
    return r.rootfs.ExportDiff(ctx, out, r.closeHook)
}
```

**导出磁盘增量这个动作，其参数之一就是「关掉沙箱」。**

为什么必须这样：

- `EjectCache()` 把 Overlay 标记为作废，之后**任何写都没有去处**；
- `mmap` 上的遍历必须没有并发写，否则导出的是「一半新一半旧」的镜像；
- NBD 设备要释放，否则文件不能删。

对「沙箱离场」这个场景，这完全合适 —— 反正沙箱也要停。
**对高频回退则完全不能接受**，这是本方案要另辟蹊径的直接原因
（[第 10 篇](10-disk-layering.md)）。

---

## 5. 恢复：`ResumeSandbox`

```
新建 Firecracker 进程
   ├── 建 KVM VM、vCPU、GIC、virtio 设备
   ├── 注册 eventfd / irqfd / ioeventfd
   ├── 建 tap、分配网络槽位
   ├── 起一个 UFFD handler 进程，挂上 guest 内存
   ├── 建 NBD 设备 + Overlay（模板 rootfs + 新写层）
   └── 加载 snapfile：恢复 vCPU、GIC、设备状态
跑起来
   └── guest 每访问一个还没有内容的页 → 缺页 → UFFD handler
        → 查本地缓存 → 没有就按 header 从对象存储拉那一块
```

**成本落在两处**：

| 成本 | 说明 |
|---|---|
| 建一台虚机的固定开销 | 进程、KVM、设备、网络、NBD —— 与虚机规格弱相关 |
| **把工作集换回内存** | 与虚机规格**强相关**，与「回退了多少」**完全无关** |

第二项是大头。一个刚恢复的沙箱在头几秒里，**几乎每次内存访问都可能是一次缺页**，
要经用户态 handler 从文件或对象存储取回。

> e2b 为此做了 **prefetch**：记录上一次运行时的缺页顺序，
> 下次恢复时按这个顺序提前拉。这能缓解，但改变不了成本模型 ——
> 要换回的量仍然取决于工作集有多大。

---

## 6. 成本模型总结

| 阶段 | 成本正比于 |
|---|---|
| 打快照 · vmstate | 常数（几十 KB） |
| 打快照 · 内存 | 脏块量（x86）/ **常驻工作集**（ARM 适配版） |
| 打快照 · 磁盘 | 脏块量，**且必须停沙箱** |
| 恢复 · 构建 | 虚机规格（弱） |
| 恢复 · 换页 | **虚机工作集**（强） |
| 上传 / 下载 | 增量大小 + 网络 |

一句话：**原生方案的代价与虚机规格挂钩，与改动量关系不大。**

---

## 7. 它擅长什么

这套设计对它的目标场景是**合适**的：

| 场景 | 为什么合适 |
|---|---|
| **跨节点迁移** | 产物自足、进对象存储，任何节点都能加载 |
| **长期持久化** | 同上，且不依赖任何活体资源 |
| **进程崩溃后恢复** | 恢复本来就是「新建进程加载」，进程死不死无所谓 |
| **沙箱离场后再回来** | 停掉沙箱本来就是这个场景的一部分 |
| 低频使用 | 一个沙箱一两次，固定开销摊得开 |

它**不擅长**的恰好是本方案的目标场景：高频、低延迟、沙箱不能中断。
这不是实现问题，是**成本模型和场景不匹配**。

> 所以本书从头到尾不说「原生方案有缺陷」，只说「它的成本模型不适合这个场景」。
> 两条路径并存，各管各的（[第 20 篇 §5](20-vs-native.md#5-两者如何配合)）。

---

## 8. 小结

1. `Pause` = 打快照 + 停；`Checkpoint` = 打快照 + 停 + **从新快照重新拉起**。
   后者沙箱 id 不变，但**底下是新进程**。
2. 打快照六步，其中 **② 暂停之后一直没有 resume** —— 原生路径的语义就是「沙箱到此为止」。
3. 内存增量：判据来自 **UFFD 写保护位**，搬运用 **`process_vm_readv`**
   从 Firecracker 进程地址空间拷出。产物紧凑，靠**链式 header** 寻址。
4. 磁盘增量**必须停沙箱**，这不是取向而是结构决定的 ——
   `ExportDiff` 的参数之一就是「关掉沙箱」这个回调。
5. 恢复是**新建进程 + UFFD 按需拉取**，大头成本是把工作集重新换回内存 ——
   **与回退跨度完全无关**。
6. 成本模型一句话：**与虚机规格挂钩**。这对「跨节点迁移、长期持久化、崩溃恢复」
   是合适的，对「高频快速回退」不合适。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 两个 RPC | `internal/server/sandboxes.go` — `Server.Pause`、`Server.Checkpoint` |
| 打快照主体 | `internal/sandbox/sandbox.go` — `Sandbox.Pause` |
| 内存增量 | 同上 — `pauseProcessMemory`；`internal/sandbox/fc/memory.go` — `ExportMemory` |
| 进程内存拷贝 | `internal/sandbox/block/cache.go` — `NewCacheFromProcessMemory`、`copyProcessMemory` |
| 磁盘增量 | `internal/sandbox/diffcreator.go` — `RootfsDiffCreator`；`rootfs/nbd.go` — `ExportDiff` |
| 空块剔除与紧凑追加 | `packages/shared/pkg/storage/header/metadata.go` — `DiffMetadataBuilder.Process` |
| header 与映射 | 同上 — `ToDiffHeader`、`BuildMap`、`MergeMappings` |
| 恢复 | `internal/sandbox/sandbox.go` — `Factory.ResumeSandbox` |
| UFFD 与判据 | `internal/sandbox/uffd/userfaultfd/userfaultfd.go` |

**下一篇**：[05 · 设计目标、约束与边界](05-design-goals.md) ——
基线讲清楚了，接下来说明本方案要解决什么、明确不解决什么。
