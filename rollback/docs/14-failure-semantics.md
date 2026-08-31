# 14 · 失败语义与不变量

> 这套系统只有一条恢复路径，没有兜底。所以「失败」必须被精确分级并如实报告 ——
> **一个看起来成功、实际数据已坏的沙箱，比一个明确报错的沙箱糟得多。**
> 本篇讲失败怎么分级、纪元为什么不能丢、以及改代码时不能破坏的那些不变量。
>
> **读者**：工程师。要动这套代码的人**必读**。
> **预备**：[第 13 篇 · 端到端](13-end-to-end.md)。
> **代码**：`internal/checkpoint/service.go`、`internal/checkpoint/store.go`、`src/vmm/src/rollback.rs`

---

## 0. 本篇要回答的问题

1. 为什么「没有兜底路线」会把失败分级从一个好习惯变成一条硬要求？
2. 一次 create 在不同位置失败，沙箱分别处于什么状态？
3. 「纪元不能丢」是什么意思？丢了会怎样？
4. 「隐藏条目」和「断链」分别解决什么问题？
5. 有哪些不变量是改代码时绝对不能破坏的？

---

## 1. 总纲：静默损坏比报错糟得多

这套系统操作的是**一台正在运行的虚拟机的内存**。一次错误的回滚不会让虚机崩溃 ——
它会让某几页内存的内容属于另一个时刻，然后虚机继续跑。

后果可能是：

- 几分钟后一个进程段错误（还算幸运，至少有信号）；
- 一个文件写出了错误的内容（可能永远没人发现）；
- 一个计算结果错了（最糟，因为它看起来是对的）。

所以整套错误处理的第一原则是：

> **宁可拒绝服务，不可返回一个可能已损坏的沙箱。**

第二个原因是**没有兜底**。设计上明确删掉了「回滚失败就杀掉进程、从快照重建」那条路
（[第 5 篇](05-design-goals.md)）—— 进程已死、虚机撕裂这些场景划给原生 snapshot。
一条路走到黑，就必须把每一种走不通的情况说清楚。

---

## 2. 提交点：把失败切成两半

`RollbackError::faults_vm()` 是这个分类在代码里的形态：

```rust
pub fn faults_vm(&self) -> bool {
    match self {
        // 提交点之前：guest 状态一个字节都没动
        NotPaused | Faulted | SnapshotFile(_) | Validation(_)
        | RevertBitmap(_) | MemoryFile(_) | DirtyBitmap(_) | BitmapWrite(_) => false,
        // 提交点之后：guest 是两个时刻的混合体
        Memory(_) | Vcpu(_) | Gic(_) | Devices(_) => true,
    }
}
```

**这个函数是 Firecracker 侧全部失败语义的浓缩。** 加一个新的错误变体时，
必须回答「它发生在提交点之前还是之后」，编译器会强迫你回答（`match` 穷尽）。

编排侧还有一处对称的划分：磁盘视图切换（`ResetView`）也在提交点之后 ——
内存已经在目标时刻、磁盘还不是，同样是撕裂：

```go
device, err := s.assembleView(ctx, layers)
if err != nil {
    return RollbackTornError{Err: fmt.Errorf("failed to assemble checkpoint disk view: %w", err)}
}
```

---

## 3. restore 的四档失败

| 失败点 | 沙箱状态 | 返回 | 调用方该做什么 |
|---|---|---|---|
| 路径缺侧车 / 位图损坏 / 物化失败 | **原状态，继续可用** | `internal`，消息里明说「沙箱仍在当前状态运行」 | 换一个目标重试，或先打一个新 checkpoint |
| Firecracker 提交点**之前**失败 | 同上，虚机原样恢复 | 同上 | 同上 |
| Firecracker 提交点**之后**失败 | **撕裂**，标记 `Faulted`，拒绝一切后续操作 | `data_loss` | 只能重建沙箱 |
| 回滚成功但 guest 的 envd 不应答 | 虚机在目标时刻，但 guest 内部卡住 | `internal` | 视情况；服务端留了带耗时的日志 |

前两档的关键在于**恢复虚机**：

```go
resumeOnError := func(err error) error {
    if resumeErr := process.ResumeVM(context.WithoutCancel(ctx)); resumeErr != nil {
        err = errors.Join(err, fmt.Errorf("failed to resume VM: %w", resumeErr))
    }
    return err
}
```

失败**并且**恢复失败时，两个错误一起返回。不掩盖任何一个。

### 3.1 envd 超时：45 秒不是随便定的

```go
// It has to stay well under the SDK's default request timeout, which is
// also 60s. At equal budgets the client always gives up first, so the
// caller sees a bare "ReadTimeout: timed out" and the one line that says
// what actually happened -- the rollback succeeded, the guest did not come
// back -- never reaches them.
const envdRestoreTimeout = 45 * time.Second
```

SDK 的默认请求超时也是 60 秒。**两边预算相等时，客户端总是先放弃** ——
于是调用方看到的是一个干巴巴的 `ReadTimeout`，而真正的信息
（「回滚成功了，是 guest 没醒过来」）永远到不了他手上。

健康的 restore 在 ~2 ms 内应答，所以 45 秒对 guest 完全不构成约束。
**这个数字唯一决定的是：谁来报告这次失败。**

这一档还会在服务端留一条带完整耗时的错误日志 —— 因为这类问题罕见、
且沙箱一销毁现场就没了：

```go
logger.L().Error(ctx, "rollback succeeded but the guest's envd never answered",
    logger.WithSandboxID(sandboxID),
    zap.String("checkpoint_id", id),
    zap.Duration("waited", envdRestoreTimeout),
    zap.Any("timings_ms", timings),
    zap.Error(err))
```

---

## 4. create：纪元不能丢

### 4.1 什么是「纪元前进」

Firecracker 一旦写完差分快照，就**清空了脏页位图**（`reset_dirty`）。
从那一刻起：

> 那个差分文件是**那一代脏页的唯一副本**，那个侧车是**「哪些页属于那一代」的唯一记录**。

如果这时候丢掉它们，就在时间线上开了一个洞：从上一代到这一代之间被写过的页，
再也没有任何记录能说明它们是哪些。

后果不是「少一个 checkpoint」，而是**此后任何跨越这个洞的回滚都会漏页** ——
[第 8 篇 §4.3](08-memory-diff-tree.md#43-正确性) 那个证明的第 2 步不成立了。
而漏页是静默的。

### 4.2 三层降级

```go
func (s *Service) failCreate(ctx context.Context, entry *Entry, memMode string, epochAdvanced bool) {
    if epochAdvanced {
        if err := s.store.CommitHidden(entry, memMode); err != nil {
            s.store.InvalidateBase(entry.SandboxID)
            logger.L().Error(ctx, "failed to keep the advanced epoch; chain broken until a full checkpoint", ...)
            s.store.Discard(entry)
        }
        return
    }
    s.store.Discard(entry)
}
```

| 纪元状态 | 处理 | 结果 |
|---|---|---|
| 没前进（快照本身就失败了） | `Discard` —— 删掉目录 | 干净，什么都没发生 |
| 前进了，能抢救 | **`CommitHidden`** —— 作为隐藏条目提交 | 纪元保住，RPC 仍然失败 |
| 前进了，连抢救都失败 | **`InvalidateBase`** —— 标记断链 | 此后拒绝恢复，直到下一次全量 checkpoint |

`epochAdvanced` 这个返回值就是为此存在的：

```go
// epochAdvanced reports whether Firecracker wrote the snapshot: doing so
// clears its dirty-page bitmap, so from that point on the memory file is the
// only valid base for the next Diff and must be preserved even if a later
// step fails.
func (s *Sandbox) CheckpointToFiles(...) (epochAdvanced bool, layer *rootfs.SealedLayer, e error)
```

注意封存失败那一支：

```go
layer, err := sealer.SealLayer(ctx, newCachePath, sealedLayerPath)
if err != nil {
    // The snapshot exists, so the epoch has advanced regardless.
    return true, nil, fmt.Errorf("failed to seal rootfs write layer: %w", err)
}
```

磁盘那一半失败了，但内存那一半已经落地 —— **仍然返回 `true`**。

---

## 5. 隐藏条目

「隐藏」是一个贯穿全系统的概念：**在树里，不在 API 里。**

| 视角 | 隐藏条目 |
|---|---|
| `List` / `Get` | 看不见 |
| 恢复目标 | 不能是它 |
| 删除目标 | 不能是它 |
| 内容解析 | **完全参与**，和普通条目一样 |
| 回滚集计算 | **完全参与** |

### 5.1 隐藏时丢掉什么

隐藏条目**永远不可能成为恢复目标**，所以只有恢复才用得着的东西可以扔：

```go
e.Hidden = true
e.Rootfs = nil
os.Remove(e.Snapfile)               // vCPU / GIC / 设备状态 —— 只有恢复才用
os.Remove(e.RootfsHeaderPath())     // 磁盘视图 —— 同上
e.Snapfile = ""
```

留下的是 `mem_diff` 和 `mem_bitmap` —— 后代要靠它们解析内容、算回滚集。

`commitFiles` 里有个对应的分支：隐藏提交时直接把临时 snapfile 删掉，不做 rename。

### 5.2 隐藏的两个来源

1. **删除一个仍被依赖的条目**（[§7](#7-删除依赖感知的回收)）；
2. **失败创建的纪元抢救**（[§4.2](#42-三层降级)）。

两者的处理完全一样，因为它们要保住的是同一样东西：那一代的脏页记录。

---

## 6. 断链

`InvalidateBase` 把基准标记成 `invalid`。此后：

```go
base := s.bases[sandboxID]
if base.invalid {
    return "", "", nil, fmt.Errorf(
        "the incremental chain is broken (an epoch was lost); take a new checkpoint before restoring")
}
```

- **恢复被拒绝** —— 因为回滚集不可能算对；
- **下一次 create 强制走全量**，成为一棵**新树的根**。

新根之后为什么就安全了？因为全量条目的内容位图是全 1，
到它为止的解析不需要任何更老的信息。老树还在（它的条目还能互相解析），
只是新老两棵树之间无法用纪元位图界定差异 ——
所以跨树回滚会退化成全量回滚（[第 8 篇 §4.5](08-memory-diff-tree.md#45-最近公共祖先怎么求)）。

**「拒绝服务」在这里是正确答案。** 一次不完整的回滚会让 guest 内存混入另一个时刻的页，
而调用方会以为一切正常。

---

## 7. 删除：依赖感知的回收

`delete(id)` 不能简单删文件 —— 后代要靠它解析内容。

```go
if hasChildLocked(entries, id) || base.entryID == id {
    // 有后代，或它是下一代的父亲 → 隐藏
    e.Hidden = true
    ...
    return s.writeIndexLocked(sandboxID)
}

// 叶子且非基准 → 物理删除，然后沿父指针级联回收
parentID := e.ParentID
delete(entries, id)
os.RemoveAll(e.Dir)
s.pruneLocked(sandboxID, entries[parentID])
```

`pruneLocked` 的条件是三个「且」：

```go
for cur := e; cur != nil && cur.Hidden && cur.ID != base.entryID && !hasChildLocked(entries, cur.ID); {
    parentID := cur.ParentID
    delete(entries, cur.ID)
    os.RemoveAll(cur.Dir)
    cur = entries[parentID]
}
```

**隐藏 且 非基准 且 无子** —— 三者同时成立，说明没有任何祖先链或回滚路径经过它，可以真删。
然后对它的父亲重复同样的判断，一路向上。

基准移动时也会触发一次同样的检查（`setBaseLocked` → `pruneLocked`）：
一个隐藏条目如果只是因为「身为基准」才被保留，基准一挪走它就该走。

> 代码注释里点名了一个反例：某些实现会直接删掉文件，
> 于是所有经过它的链**静默地**读到错误内容。这里的做法是宁可留下垃圾文件，
> 也不留下一条坏链。

---

## 8. 账本污染

[第 10 篇 §8](10-disk-layering.md#8-账本与污染) 讲过磁盘账本的 `poisoned` 标志。
放在失败语义的框架里看，它是这样一条规则：

> **既成事实与记账失败要分开处理。**
> 封存已经发生了（层在活的栈里），这是既成事实；账本跟不上是记账失败。
> 不能因为记账失败就假装封存没发生 —— 那会让后面每一个视图都少一层。

处理方式是把账本标记为不可信并拒绝再工作，而不是留下一个看起来正常的错账本。
污染由一次成功的 restore 清除（`SetRootfsToEntry` 用 checkpoint 自己的 header 重新播种）。

同一条规则在内存那边的体现就是 `CommitHidden`：快照已经写了，这是既成事实；
后续步骤失败不能让它消失。

---

## 9. 不变量清单

**改代码时不能破坏的。** 每条附上破坏后的表现 —— 注意有多少条是**静默**的。

| # | 不变量 | 破坏后果 | 静默？ |
|---|---|---|---|
| 1 | 差分条目**必须**有侧车 | 该条目不可用；解析或回滚集经过它就出错 | 否（`commitFiles` 报错） |
| 2 | `E_x` 恰好覆盖 `(parent(x), x]` | 回滚集不再是超集 → **漏页** | **是** |
| 3 | 回滚集 ⊇ Firecracker 实际写回的页 | Firecracker 从文件空洞读到零 → **清零若干页** | **是** |
| 4 | 恢复目标必须与运行中的虚机**同拓扑** | 往不存在的对象写 | 否（`validate_topology`） |
| 5 | 内存与磁盘在**同一个**暂停窗口内 | guest 看到两个时刻的世界 | 部分（可能表现为文件系统错误） |
| 6 | 活跃脏图必须在**暂停之后**导出 | 同 #3 | **是** |
| 7 | 提交前条目对 `Get` / `List` **不可见** | 半写完的快照成为恢复目标 | **是** |
| 8 | 纪元前进后产物必须保留（哪怕隐藏） | 时间线上开洞 → 后续回滚漏页 | **是** |
| 9 | 账本与活的层栈必须一致，不一致就拒绝服务 | 视图少一层 → 读到旧数据 | **是** |
| 10 | conntrack 在暂停窗口内清 | 竞态删掉新表项，或残留表项让连接挂死 | **是**（挂死无报错） |
| 11 | VMGenID 在内存回写**之后** | guest 不知道时间被回拨 | **是** |
| 12 | 队列页在阶段 9 后重新标脏 | 下一代 Diff 漏页 → 同 #2 | **是** |
| 13 | HDBSS 在 vCPU 创建**之后**武装 | 一条脏页都记不到 → 每次都退化成全量 | **是**（只表现为慢） |
| 14 | 恢复虚机是无条件的（`defer`） | 虚机停在暂停态，所有人都以为它在跑 | 部分 |

**14 条里有 9 条被破坏后是静默的。** 这就是为什么这套代码里到处是「宁可报错」的分支，
以及为什么[第 17 篇](17-observability-and-verification.md)要花那么大篇幅讲怎么让退化可见。

---

## 10. 错误如何到达调用方

| 层 | 形态 |
|---|---|
| Firecracker | `RollbackError` 变体 + `faults_vm()` 分类 |
| FC 客户端 | `RollbackUnsupportedError`（404）、`RollbackFaultedError`（响应体含 "Faulted"） |
| 沙箱层 | `RollbackTornError` —— 撕裂，只能重建 |
| Service | Connect 错误码：`data_loss`（撕裂）/ `internal`（其余）/ `not_found` / `invalid_argument` / `unauthenticated` |
| SDK | 抛异常 |

有一处**已知的不够严谨**：客户端靠字符串匹配识别 `Faulted`
（[第 9 篇 §3.3](09-firecracker-api-contract.md#33-客户端侧的三个细节)）。
改 Firecracker 那一侧时值得顺手在响应里加一个结构化字段。

---

## 11. 小结

1. 第一原则：**宁可拒绝服务，不可返回一个可能已损坏的沙箱。**
   删掉兜底路线之后，这从习惯变成了硬要求。
2. **提交点**把失败切成两半，`faults_vm()` 把这个分类固化在代码里 ——
   加新错误变体时编译器强迫你回答它属于哪一半。
3. **纪元不能丢**：快照一写完，那份差分就是那一代脏页的唯一副本。
   三层降级：正常提交 → 隐藏提交 → 标记断链。
4. **隐藏条目**在树里不在 API 里，丢掉只有恢复才用的部分，保住解析和回滚集要用的部分。
5. **断链后拒绝恢复**是正确答案，因为回滚集不可能算对。
6. **删除是依赖感知的**：被依赖就隐藏，是叶子才真删，然后沿父指针级联回收。
7. 不变量 14 条，**其中 9 条被破坏后是静默的**。

---

## 思考题

1. `CommitHidden` 自己也可能失败（比如磁盘满）。此时代码调用 `InvalidateBase` 并 `Discard`。
   如果 `InvalidateBase` 之后进程崩溃，重启后的状态是什么？这个状态安全吗？
2. 不变量 #7 说「提交前不可见」。`Prepare` 已经把条目写进了磁盘的 manifest（状态 `prepared`）。
   既然账本不从磁盘加载，这个磁盘上的 `prepared` 条目有什么用？会不会有害？
3. 设想给 `delete` 加一个 `force` 参数，允许删掉仍被依赖的条目。
   要让它安全，必须先做什么？代价是什么？
4. 不变量 #13（HDBSS 武装时序）目前只靠调用点的位置保证，没有断言。
   设计一个能在运行期发现这个错误的检查，要求代价足够低、可以常开。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 失败分类 | `src/vmm/src/rollback.rs` — `RollbackError::faults_vm` |
| 撕裂错误 | `internal/sandbox/checkpoint.go` — `RollbackTornError`、`resumeOnError` |
| 三层降级 | `internal/checkpoint/service.go` — `failCreate` |
| 纪元标志 | `internal/sandbox/checkpoint.go` — `CheckpointToFiles` 的 `epochAdvanced` |
| 提交与隐藏提交 | `internal/checkpoint/store.go` — `Commit`、`CommitHidden`、`commitFiles` |
| 断链 | 同上 — `InvalidateBase`、`MaterializeRevert` 里的 `base.invalid` 检查 |
| 删除与级联回收 | 同上 — `Delete`、`pruneLocked`、`setBaseLocked` |
| 账本污染 | `internal/checkpoint/rootfs.go` — `AppendLayer` 的 `poisoned` |
| envd 超时 | `internal/checkpoint/service.go` — `envdRestoreTimeout` |
| 相关单元测试 | `internal/checkpoint/store_test.go` — `TestCommitHiddenRescuesAdvancedEpoch`、`TestInvalidateBaseBreaksChainUntilFullRoot`、`TestDeleteHidesWhileReferencedAndCascades`、`TestHiddenEntriesDropWhatCanNeverBeRead` |

**下一篇**：[15 · 状态管理与并发](15-state-and-concurrency.md) —— 失败语义讲的是「出错怎么办」，
下一篇讲「怎么在一台正在运行的机器上安全地换零件」。
