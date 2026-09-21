# 18 · 两套方案：ext4 与 XFS

> 同一套 checkpoint / restore 有两个实现，区别只在**内存产物怎么存**。
> 分歧的源头是一个文件系统特性：**reflink**。本篇讲这个特性怎么一路影响到
> API 设计、冻结窗口长度和存储占用，以及怎么选。
>
> **读者**：工程师、部署与运维。
> **预备**：[第 8 篇 · 内存差分树](08-memory-diff-tree.md)、
> [第 9 篇 · Firecracker 接口契约](09-firecracker-api-contract.md)。
> **代码**：交付的是 ext4 方案（`KASandbox_0904`，交付分支 `deltabox`）；XFS 方案已归档，代码不在该仓库、不在交付范围，本篇保留作设计对照。
> 两者的差异集中在 `internal/checkpoint/{store,service,clone}.go`

---

## 0. 本篇要回答的问题

1. reflink 是什么？为什么一个文件系统特性能改变整套设计？
2. 两套方案具体差在哪些地方？
3. 为什么只有 ext4 方案需要 `save-dirty-bitmap` 端点？
4. 该怎么选？判据是机型还是文件系统？
5. 配错了会怎样？

---

## 1. 分歧的源头：reflink

`FICLONE` ioctl 让两个文件**共享同一批磁盘 extent**，任一方写入时才真正分裂
（写时复制）。XFS、Btrfs、ZFS 支持；**ext4 不支持**。

对本方案，这个特性决定了一件事能不能做：**「克隆上一代的完整内存镜像，
再把本代脏页覆盖上去」**。

| 文件系统 | `FICLONE` | 克隆 2 GiB 内存镜像的代价 |
|---|---|---|
| XFS（`reflink=1`） | 支持 | **元数据操作**，毫秒级，物理上不占新空间 |
| ext4 | 不支持（`EOPNOTSUPP`） | 退化成**真实字节拷贝**，2 GiB 的读 + 写 |

代码里的退化路径是显式的：

```go
if err := unix.IoctlFileClone(int(out.Fd()), int(in.Fd())); err == nil {
    return true, nil
}
// Not supported (ext4, cross-device, …) — fall through to a real copy.
if err := copySparse(in, out); err != nil { ... }
return false, nil
```

返回值 `cloned` 一路传到日志和 metrics 属性 —— 因为这个退化**完全静默**：
结果正确，只是慢几十倍。

---

## 2. 两套方案

### 2.1 XFS 方案：每代自足

每个 checkpoint 的内存产物是一份**完整镜像**：

```
① 克隆上一代的 mem_full            ← 元数据操作（在暂停窗口之外）
② Firecracker 以 Diff 模式把本代脏页写进克隆   ← 在暂停窗口内
③ 得到本代时刻的完整内存视图
```

关键细节：**第 ① 步在虚机还在运行时就完成了**。

```go
// The base is immutable, so the copy runs while the VM still runs —
// the pause window only pays for the dirty pages. With reflink this
// is a metadata operation either way.
cloned, err := CloneOrCopy(basePath, entry.TempMemFull())
```

上一代的镜像是**不可变**的，所以克隆不需要暂停虚机。
冻结窗口里只有「写脏页」这一步。

**树账本仍然存在**，但它只用来**算回滚集**，不用来解析内容 —— 内容直接从目标那份完整镜像读。

### 2.2 ext4 方案：差分树

取消克隆，每代只存本代脏页的稀疏差分；内容按页沿祖先链解析
（[第 8 篇](08-memory-diff-tree.md)）。

树账本承担**两个**职责：算回滚集 **和** 解析内容。

---

## 3. 逐项对比

| 维度 | ext4 方案（主线） | XFS 方案 |
|---|---|---|
| 每代内存产物 | 稀疏差分，仅本代脏页 | 完整镜像 |
| 对文件系统的要求 | **无** | XFS 且 `reflink=1` |
| 树账本的用途 | 算回滚集 **+ 解析内容** | 只算回滚集 |
| 内容解析 | 沿祖先链逐页 | 不需要 |
| 递给回滚端点的内存文件 | 只含回滚集的**稀疏**文件 | 该代的**完整镜像** |
| 需要 `save-dirty-bitmap` 端点 | **是** | 否 |
| 回滚集何时计算 | **暂停之后**（要活跃脏页） | **暂停之前**（只要账本） |
| 冻结窗口（restore） | 多含「导出活跃脏图 + 物化」 | 不含这两步 |
| 全量树根 | 需要（`CHECKPOINT_FULL_ROOT`），否则回滚路径跨网络 | 天然自足 |
| 存储占用 | O(Σ 各代脏页) + 一份全量 | 逻辑上每代全量，物理上靠 extent 共享 |
| 隐藏条目保留什么 | `mem_diff` + 位图（后代要解析） | 位图；`mem_full` 只在它还是基准时保留 |
| 静默退化的形态 | 脏页跟踪没开 → `memMode=full` | 无 reflink → 每代真拷贝 |
| 退化的探测手段 | `memMode` 回报 | 启动时 reflink 探针 + `reflink` 属性 |

### 3.1 磁盘那一半完全相同

值得强调：**分层封存、`SealLayer`、`SealedView`、`ResetView`、合并 header ——
两套方案完全一样**（[第 10 篇](10-disk-layering.md)）。

磁盘增量本来就不依赖 reflink：封存是改名，不是拷贝。所以分歧只发生在内存那一半。

### 3.2 Firecracker 侧的差异只有一个端点

| | ext4 方案的 Firecracker（交付） | XFS 方案的（已归档） |
|---|---|---|
| `dirty_bitmap_path` | ✓ | ✓ |
| `PUT /snapshot/rollback` | ✓ | ✓ |
| `PUT /snapshot/save-dirty-bitmap` | **✓** | ✗ |
| HDBSS 启用与上报 | ✓ | ✓ |
| vCPU 路线 | 固定 reinit | 固定 reinit |

---

## 4. 为什么只有 ext4 方案需要 `save-dirty-bitmap`

这是两套方案最有意思的一处连带差异。回顾那条契约
（[第 9 篇 §4.1](09-firecracker-api-contract.md#41-为什么非有不可)）：

> **Firecracker 在回滚时会把自己的活跃脏页并进写回集**，然后按页从调用方给的文件读内容。

| | 递过去的文件 | Firecracker 多写一页会怎样 |
|---|---|---|
| **XFS 方案** | 该代的**完整镜像** | 读到那一页的真实内容。**无害** —— 只是多做一次无用功 |
| **ext4 方案** | 只含回滚集的**稀疏文件** | 从**文件空洞读到零** → **静默把 guest 的那一页清零** |

所以 ext4 方案必须让 orchestrator **事先知道**活跃脏页集，两边的集合才能相等。
这就是那个端点存在的唯一理由。

XFS 方案则可以把回滚集算得**更早**（暂停之前），因为它不需要活跃脏页那一项：

```go
// The revert set: the epoch bitmaps between the memory base and the
// target, unioned (the live dirty set Firecracker folds in itself). It is
// computed before the VM is paused, so a refusal costs the guest nothing.
hasFile, err := s.store.RevertBitmapForTarget(sandboxID, entry.ID, revertPath)
```

注释里那句「so a refusal costs the guest nothing」点出了一个附带好处：
**算不出回滚集时，虚机根本没被暂停过。**

> **一个诚实的结论**：在 restore 的冻结窗口这一项上，**XFS 方案更短** ——
> 它不含「导出活跃脏图 + 物化」这两步。ext4 方案为「不依赖 reflink」付的价，
> 一部分就落在这里。

`RevertBitmapForTarget` 还有一个小优化：当基准就是目标时，路径为空，
返回 `wroteFile=false`，连文件都不写 —— 此时活跃脏页集**本身**就是完整的回滚集。

---

## 5. 成本模型对比

设 guest 内存 M、本代脏页 D、回滚集 R。

| 操作 | ext4 方案 | XFS 方案（有 reflink） | XFS 方案（**无** reflink） |
|---|---|---|---|
| create · 窗口外 | 常数 | O(1) 克隆 | **O(M) 真拷贝** |
| create · 窗口内 | O(D) 写 | O(D) 写 | O(D) 写 |
| create · 存储增量 | O(D) | O(D)（extent 共享） | **O(M)** |
| restore · 窗口外 | 常数 | O(M/64) 位运算 | 同左 |
| restore · 窗口内 | O(M/64) 扫描 + **O(R) 物化** + O(R) 写回 | O(R) 写回 | 同左 |

两处值得注意：

**① 无 reflink 时，XFS 方案的退化不影响冻结窗口。** 那次真拷贝在暂停窗口之外，
guest 感受到的停顿仍是 O(D)。退化的是**调用总耗时**、**磁盘 I/O** 和**存储占用**。
这仍然是一个数十倍的退化，只是不表现为业务停顿 —— 所以更隐蔽。

**② ext4 方案的物化在窗口内。** 这是它的固有代价：内容分散在多代文件里，
必须先聚合成一个文件才能递给 Firecracker，而聚合又必须在暂停之后
（要活跃脏页）。

---

## 6. 怎么选

### 6.1 判据是文件系统，不是机型

**方案选择与机型正交。**

| 轴 | 判据 | 结果 |
|---|---|---|
| 内存产物形态 | 产物盘有没有 reflink | ext4 方案 / XFS 方案 |
| 脏页后端 | CPU 有没有 HDBSS | 硬件标脏 / 软件写保护 |

我们的部署恰好把它们配成了对（950 + ext4、920B + XFS），但那是**部署事实，不是设计约束**。
一台 950 上如果把产物盘做成 XFS，XFS 方案照样能跑，而且还能用 HDBSS。

### 6.2 决策表

| 情况 | 选 |
|---|---|
| 产物盘是 ext4（或不确定 / 不可控） | **ext4 方案** |
| 产物盘是 XFS 且能保证 `reflink=1` | 两者皆可；XFS 方案的 restore 冻结窗口略短 |
| 客户环境的存储由对方决定 | **ext4 方案** —— 它没有文件系统前提 |
| 沙箱内存很大、每代改动很小、存储紧张 | ext4 方案（存储 O(Σ脏页)） |

**主线是 ext4 方案**，理由不是它更快，而是它**没有前提**。一个需要「请把产物盘做成
带 reflink 的 XFS」的方案，在交付时会变成一条需要对方配合的约束，而这条约束一旦不满足
就静默退化。

### 6.3 部署时的探测

XFS 方案在启动时做一次真实的 reflink 探针（建两个临时文件试一次 `FICLONE`），
不通过就打 **Error** 级日志并给出修复建议：

```
checkpoint store is on a filesystem without reflink: every checkpoint will copy
all of guest memory into the store. Point ORCHESTRATOR_BASE_PATH at an XFS volume
created with reflink=1.
```

探针的注释说明了为什么值得在启动时做一次：

> That is a silent tens-of-times regression, so it is worth one probe at startup
> rather than one log line per checkpoint that nobody reads.

---

## 7. 版本配对

**四个二进制，两两成对，不可交叉。**

| 组合 | 结果 |
|---|---|
| ext4 orchestrator + ext4 Firecracker | ✓ |
| XFS orchestrator + XFS Firecracker | ✓ |
| ext4 orchestrator + XFS Firecracker | restore 时 `save-dirty-bitmap` 返回 **404** → 明确失败 |
| XFS orchestrator + ext4 Firecracker | 功能上能跑（多一个端点无害），但**未经验证，不要这么用** |
| 任一 orchestrator + 未打补丁的 Firecracker | `/snapshot/rollback` 返回 404 → `RollbackUnsupportedError` |

**产物不可跨方案共享**：两者的账本语义、条目结构（`MemDiff` vs `MemFull`）、
文件布局都不同。一个方案写的 store 目录，另一个方案读不了 ——
好在账本本来就不从磁盘加载（[第 16 篇 §3.3](16-lifecycle-and-portability.md#33-账本不从磁盘加载)），
所以这个错误不会真的发生：换二进制重启 orchestrator，旧产物直接作废。

---

## 8. 演进关系

两套方案不是并行开发的两个产品，而是**一次路线修正**：

XFS 方案先出现，因为「每代自足」的设计更简单 —— 产物自足、恢复路径短、
不需要内容解析、不需要额外的 API 端点。它在 XFS 上完全成立。

后来确认 950 与客户环境的产物盘**全部是 ext4**，克隆退化成真实拷贝，
成本与改动量彻底脱钩。于是有了 ext4 方案：**去掉克隆，代之以差分树 + 按页解析**。

所以[第 8 篇 §1.1](08-memory-diff-tree.md#11-reflink-是什么为什么-ext4-没有) 里那句话值得重复：

> 这不是「差分比克隆更好」。在 XFS 上克隆是更简单的设计。
> 这是**在没有 reflink 的文件系统上，克隆这个方案不成立**。

XFS 方案保留下来，一是 920B 上的验证环境用它，二是它在 XFS 部署下确实是更优解。

---

## 9. 小结

1. 分歧的源头是 **reflink**：XFS 有，ext4 没有。有它时「克隆整代镜像」是元数据操作，
   没有时退化成真实拷贝。
2. **XFS 方案**每代自足（克隆 + 覆盖），树账本只算回滚集；
   **ext4 方案**每代只存差分，树账本还要负责解析内容。
3. **磁盘那一半两套完全相同** —— 封存是改名不是拷贝，本来就不依赖 reflink。
4. 只有 ext4 方案需要 `save-dirty-bitmap`：它递给回滚端点的是稀疏文件，
   Firecracker 多写一页就会从空洞读到零。XFS 方案递的是完整镜像，无害。
5. 连带地，XFS 方案的回滚集可以在**暂停之前**算出来，
   所以它的 restore 冻结窗口**更短**。这是 ext4 方案为「不依赖 reflink」付的价之一。
6. 无 reflink 时 XFS 方案的退化**不影响冻结窗口**（克隆在窗口外），
   退化的是总耗时、I/O 和存储 —— 更隐蔽，所以要在启动时探测。
7. **判据是文件系统，不是机型**。主线选 ext4 方案的理由是它**没有前提**。
8. 四个二进制两两成对，不可交叉；产物不可跨方案共享。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| reflink 探针与克隆 | `internal/checkpoint/clone.go`（**仅 XFS 方案，已归档**）— `ProbeReflink`、`CloneOrCopy`、`copySparse` |
| XFS 方案的 create | `internal/checkpoint/service.go`（XFS 方案）— `create` 里的 `CloneOrCopy` 分支 |
| XFS 方案的回滚集 | `internal/checkpoint/store.go`（XFS 方案）— `RevertBitmapForTarget`、`MemSource` |
| ext4 方案的差分与解析 | `internal/checkpoint/store.go`（ext4 方案）— `MaterializeRevert`、`writeRevertMem` |
| 启动时的能力上报 | `packages/orchestrator/main.go` — `reportCheckpointCapabilities`（两方案内容不同） |
| `save-dirty-bitmap`（仅 ext4 方案） | `firecracker/src/vmm/src/rollback.rs`— `save_dirty_bitmap` |

**下一篇**：[19 · 鲲鹏平台](19-kunpeng-platform.md) —— 另一条轴：机型决定脏页后端。
