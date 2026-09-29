# 12 · 失败语义与不变量

## 本章目标

读完本章，你应当能回答：

1. 为什么"没有兜底路线"会把失败分级从一个好习惯变成一条硬要求？
2. 一次 restore 在不同位置失败后，沙箱分别处于什么状态、调用方该做什么；一次 checkpoint 失败时，"纪元不能丢"是什么意思？
3. "隐藏条目""断链""账本污染"分别解决什么问题？
4. 删除为什么是依赖感知的；删除结尾的合并（compact / fold）为什么不改变任何 restore；删除失败时服务端怎样区分"没删成"和"删了但文件没清干净"？
5. 改代码时有哪些不变量绝对不能破坏，其中哪些被破坏后是静默的？

---

第二部分讲完了正常路径：上一章（[11](11-end-to-end.md)）按时间顺序走完了一次 checkpoint 和一次 restore，并在沿途标出了几个失败出口。
第三部分转向"怎么保证它不出错，以及出错时怎么让人知道"。本章是第三部分的第一章，也是要动这套代码的人**必读**的一章：
它把第 [09](09-in-place-rollback.md) 章的提交点、第 [06](06-memory-diff-tree.md) 章的纪元与回滚集、第 [07](07-disk-layering.md) 章的磁盘账本，
放进同一个失败语义的框架里，最后给出一份不变量清单。并发与持久性（锁、暂停、不 fsync）是下一章 [13](13-state-concurrency-durability.md) 的主题。

错误码、`reason` 与 SDK 异常类的对照表在 [25](25-errors-timeouts-concurrency.md)，本章不重复。

---

## 1. 总纲：静默损坏比报错糟得多

这套系统操作的是**正在运行的虚拟机的内存**。一次错误的回滚不会让虚机崩溃 —— 它会让某几页的内容属于另一个时刻，然后虚机继续跑。
后果可能是几分钟后一个进程段错误（幸运，至少有信号）、一个文件写出了错误内容（可能永远没人发现）、一个计算结果错了（最糟，因为它看起来是对的）。
所以整套错误处理的第一原则是：

> **宁可拒绝服务，不可返回一个可能已损坏的沙箱。**

第二个原因是**没有兜底**：设计上不提供「回滚失败就杀掉进程、从快照重建」那条路，进程已死、虚机撕裂这些场景划给 e2b 原生 snapshot
（[02](02-e2b-native-snapshot.md)）。一条路走到黑，就必须把每一种走不通的情况说清楚。

---

## 2. 提交点：把失败切成两半

Firecracker 侧，`RollbackError::faults_vm()`（`rollback.rs:85`）是全部失败语义的浓缩：

```rust
pub fn faults_vm(&self) -> bool {
    match self {
        // 提交点之前：guest 状态一个字节都没动
        NotPaused | Faulted | SnapshotFile(_) | Validation(_) | RevertBitmap(_)
        | MemoryFile(_) | MemoryFileCoverage(_) | DirtyBitmap(_) | BitmapWrite(_) => false,
        // 提交点之后：guest 是两个时刻的混合体
        Memory(_) | Vcpu(_) | Gic(_) | Devices(_) => true,
    }
}
```

提交点之前的错误（含内存文件覆盖校验）不动 guest 状态，之后的错误让虚机成为两个时刻的混合体、被标记 `Faulted`（[09](09-in-place-rollback.md) §3.4）。
`match` 是穷尽的，加新错误变体时编译器强迫你回答它属于哪一半。

编排侧（`internal/sandbox/checkpoint.go` — `RollbackInPlace`）有三处对称的划分：

| 情形 | 归类 | 理由 |
|---|---|---|
| Firecracker 应答带 `"fault": true` | 撕裂（`RollbackTornError`） | 提交点之后失败 |
| rollback 调用超过 `CHECKPOINT_FC_CALL_TIMEOUT` 未应答 | 撕裂 | 无法判断停在提交点哪一侧；恢复运行一个可能半新半旧的 guest，会让它把说不清的状态写进磁盘 |
| `ResetView` 失败、或之后的 resume 失败 | 撕裂 | 内存已在目标时刻、磁盘或运行态还不是 |

磁盘视图的**装配**（`AssembleView`）在暂停之前完成（`service.go:1261`），装配失败是一次普通的失败 restore，沙箱原样继续运行，
装配出来的视图随即释放（`ReleaseView`）。

---

## 3. restore 的四档失败

| 失败点 | 沙箱状态 | `reason` | 调用方该做什么 |
|---|---|---|---|
| checkpoint 树解析不了这次 restore：目标没有磁盘视图、祖先缺失、当前基准不在库里、丢过纪元（断链） | **原状态，继续可用** | `chain_broken` | **不要重试**；先打一个新 checkpoint 起新链 |
| 提交点**之前**的其它失败：读 sidecar、装配视图、物化回滚集、Firecracker 在提交点前报错 | 同上，虚机原样恢复；消息里明说「沙箱仍在当前状态运行」 | `internal` | 换目标重试，或先打一个新 checkpoint |
| 提交点**之后**失败，或 rollback 调用超时 | **撕裂**；此后这一代沙箱的 checkpoint 与 restore 一律被拒，`list` / `delete` 仍可用 | `torn` | 只能销毁重建沙箱 |
| 回滚成功但 guest 的 envd 在 45 s 内不应答 | 宿主侧已完成，虚机在目标时刻；**不是撕裂** | `guest_unresponsive` | 按业务决定：稍后重试命令，或销毁重建 |

- **撕裂按代记**：标记记在 `Sandbox.LifecycleID` 下（每个 Firecracker 进程一个），不按沙箱 id。同一个 id 经原生 pause/resume 换了新进程之后从干净状态开始
  （`service.go` — `refuseIfTorn`、`markTorn`）。撕裂时服务端同时标记断链与磁盘账本 poisoned，并**让虚机保持暂停**：它持有回滚写到一半的内容，
  是事后分析唯一的材料；恢复运行会让一个半新半旧的 guest 写它的磁盘。
- **前两档的关键是恢复虚机**（`internal/sandbox/checkpoint.go:514`）：

  ```go
  resumeOnError := func(err error) error {
      conntrack.join()   // 先等清表完成：放流量回来之前表项必须已删（10 §4.5）
      if resumeErr := fcCall(context.WithoutCancel(ctx), process.ResumeVM); resumeErr != nil {
          err = errors.Join(err, fmt.Errorf("failed to resume VM: %w", resumeErr))
      }
      return err
  }
  ```

  失败**并且**恢复失败时两个错误一起返回，不掩盖任何一个。
- **envd 的 45 s 不是随便定的**（`service.go:46-58`）：

  ```go
  // It has to stay well under the SDK's default request timeout, which is
  // also 60s. At equal budgets the client always gives up first, so the
  // caller sees a bare "ReadTimeout: timed out" and the one line that says
  // what actually happened -- the rollback succeeded, the guest did not come
  // back -- never reaches them.
  envdRestoreTimeout = 45 * time.Second
  ```

  注释里的 60 s 是 SDK 的**通用**请求超时。两边预算相等时，客户端总是先放弃 —— 于是调用方看到一个光秃秃的读超时，
  「回滚成功了，是 guest 没醒过来」这条真正的信息永远到不了他手上。SDK 的 checkpoint 调用另有更长的默认超时（`CHECKPOINT_REQUEST_TIMEOUT`），
  细节与重试策略见 [25](25-errors-timeouts-concurrency.md)。健康的 restore 远在这个上限之内应答，所以 45 s 对 guest 不构成约束，
  **它唯一决定的是由谁来报告这次失败**。

  这一档还会在服务端留一条带完整耗时的 error 日志（`finishRestore`）—— 因为这类问题罕见，且沙箱一销毁现场就没了：

  ```go
  logger.L().Error(ctx, "rollback succeeded but the guest's envd never answered",
      logger.WithSandboxID(sandboxID),
      zap.String("checkpoint_id", entry.ID),
      zap.Duration("waited", envdRestoreTimeout),
      zap.Any("timings_ms", map[string]float64(timings)),
      zap.Error(err))
  ```

---

## 4. checkpoint：纪元不能丢

### 4.1 什么是「纪元前进」

Firecracker 一旦写完差分快照就**清空了脏页位图**（`dump_dirty` 末尾的 `reset_dirty`，[08](08-firecracker-api-contract.md) §2.2）。从那一刻起：

> 那个差分文件是**那一代脏页的唯一副本**，那个侧车是**「哪些页属于那一代」的唯一记录**。

如果这时候丢掉它们，就在时间线上开了一个洞：从上一代到这一代之间被写过的页，再也没有任何记录能说明它们是哪些。
后果不是「少一个 checkpoint」，而是**此后任何跨越这个洞的回滚都会漏页** —— [06](06-memory-diff-tree.md) §4.2 的正确性证明依赖
「每一代的纪元位图恰好覆盖那一代写过的页」，这一步不再成立。而漏页是静默的。

`CheckpointToFiles` 的返回值 `epochAdvanced` 就是为此存在的，函数的文档注释写明了它的含义：

```go
// epochAdvanced reports whether Firecracker wrote the snapshot: doing so
// clears its dirty-page bitmap, so from that point on the memory file is the
// only valid base for the next Diff and must be preserved even if a later
// step fails.
```

两个容易写错的分支都偏向「保留」：

- **封存失败**：内存那一半已经落地，磁盘那一半没成 —— 仍然返回 `true`（代码注释："The snapshot exists, so the epoch has advanced regardless."）；
- **快照调用超时**：主机放弃等待时，Firecracker 可能已经写完、清了位图 —— 也返回 `true`。宁可保留一个可能多余的文件，也不丢掉可能是唯一副本的页。

### 4.2 三层降级

`failCreate`（`service.go:724`）：

| 纪元状态 | 处理 | 结果 |
|---|---|---|
| 没前进（快照本身失败） | `Discard`：删掉目录 | 干净，什么都没发生 |
| 前进了，能抢救 | **`CommitHidden`**：作为隐藏条目提交 | 纪元保住，RPC 仍然失败 |
| 前进了，连抢救都失败 | **`InvalidateBase`**：标记断链，再 `Discard` | 此后拒绝恢复，直到下一次全量 checkpoint |

代码（`failCreate`，完整片段见 [11](11-end-to-end.md) §2.6）只有十几行，但每一支都有理由：纪元没动时删干净是安全的，因为 Firecracker 的位图还在，
下一次 checkpoint 会把这些页照常写进下一代；纪元动了，就只能保、不能删；保不住（`CommitHidden` 自己也失败，例如产物盘写满），就明确地把链标为断开，
让之后的 restore 拒绝而不是算出一个漏页的回滚集。

如果 `InvalidateBase` 之后 orchestrator 进程崩溃呢？重启会带走这个节点上所有沙箱，`NewStore` 启动时清空整个 store（[13](13-state-concurrency-durability.md) §5），
不会有任何人对着一个半更新的账本做 restore。这是「账本不跨重启存活」这个取舍在失败语义上的一个好处。

另有一支与磁盘有关：纪元已前进、但失败发生在封存层被 `AppendLayer` 登记之前时，封存可能已经把写层换进活的层栈却没人登记它。
此时把磁盘账本标为 poisoned，并把这个层路径记为「未登记」，留给下一次替换层栈的 restore 回收（§8）。

---

## 5. 隐藏条目

「隐藏」贯穿全系统：**在树里，不在 API 里。**

| 视角 | 隐藏条目 |
|---|---|
| `List` / `Get` | 看不见 |
| 恢复目标、删除目标 | 不能是它 |
| 个数上限 | 不计（调用方看不见也删不掉） |
| 内容解析、回滚集计算 | **完全参与**，和普通条目一样 |

隐藏条目永远不会成为恢复目标，所以只有恢复才用得着的东西可以扔：snapfile（vCPU / GIC / 设备状态）、`rootfs.header` 与它的视图（连同视图持有的层引用）。
留下的是内存差分与位图侧车 —— 后代要靠它们解析内容、算回滚集。`commitFiles` 里有对应的分支：隐藏提交时不把临时 snapfile 改名到位，直接删掉。

隐藏有两个来源：**删除一个仍被依赖的条目**（§7）和**失败 checkpoint 的纪元抢救**（§4.2）。两者处理相同，因为要保住的是同一样东西：那一代的脏页记录。

---

## 6. 断链

`InvalidateBase` 把基准标记成 `invalid`。此后：

- **恢复被拒绝**，因为回滚集不可能算对（`MaterializeRevert`，`store.go:1527`）：

  ```go
  base := s.bases[sandboxID]
  if base.invalid {
      s.mu.Unlock()
      return "", "", nil, fmt.Errorf("%w: an epoch was lost; take a new checkpoint before restoring", ErrChainBroken)
  }
  ```

- **下一次 checkpoint 强制走全量**，成为一棵**新树的根**。

新根之后为什么安全：全量条目持有每一页，到它为止的内容解析不需要更老的信息；它的回滚因子恒为全 1，
跨树回滚时能把「它的时刻与启动内存源之间所有不同的页」都带进回滚集，包括丢失的那个纪元里写过的页（[06](06-memory-diff-tree.md)）。
老树还在，它的条目仍能互相解析，只是新老两棵树之间要按跨树规则回滚。

**「拒绝服务」在这里是正确答案。** 一次不完整的回滚会让 guest 内存混入另一个时刻的页，而调用方会以为一切正常。

---

## 7. 删除：依赖感知的回收与合并

`delete(id)` 不能简单删文件 —— 后代要靠它解析内容。`Store.Delete`（`store.go:2009`）分三段：锁内改账本、锁外删文件、最后做合并。
机制细节（合并的证明、层合并的条件）在 [06](06-memory-diff-tree.md) 与 [07](07-disk-layering.md)；给用户看的「怎么删才释放空间」在 [24](24-semantics-and-limits.md)。

### 7.1 锁内：隐藏还是移除

`deleteLocked`（`store.go:2043`）只改账本，要删的文件收进一个 `reclaim` 列表：

```go
if !s.ownsLocked(sandboxID, lifecycleID) { return "", nil, notFound(...) }   // 上一代的树不归本代删
e, ok := entries[id]
if !ok || e.Hidden { return "", nil, notFound(...) }                          // 不存在或已隐藏 → 404

if s.hasChildLocked(sandboxID, id) || base.entryID == id {
    // 有子节点，或它是下一代的父亲 → 隐藏
    rc.file(e.Snapfile); rc.file(e.RootfsHeaderPath())  // 只有恢复才用的文件（合并改过名的 header 一并收走）
    s.unrefLayersLocked(sandboxID, e.Rootfs.Layers, rc)  // 视图走了，层引用随之归还
    e.Hidden, e.Rootfs, e.Snapfile = true, nil, ""
    s.chargeLocked(sandboxID, -(snap + header))           // 字节计数
    s.queueCompactLocked(sandboxID, id)                   // 可能成为合并候选
    return "", &manifest, nil                             // manifest 放锁后再写
}
// 叶子且非基准 → 从账本移除
delete(entries, id); s.removeChildLocked(sandboxID, parentID)
s.chargeLocked(sandboxID, -e.bytes.total())
s.unrefLayersLocked(sandboxID, e.Rootfs.Layers, rc)       // 计数归零的层进 reclaim
s.pruneLocked(sandboxID, entries[parentID], rc)          // 沿父指针级联
return e.Dir, nil, nil                                    // 目录放锁后再删
```

- `hasChildLocked` 查的是**子节点计数表** `Store.children[sandbox][parentID]`（`store.go:1907`），O(1)，在条目进出账本处维护，
  不再扫描全部条目。
- 删除路径**不写 index.json**：它默认不存在，只在 `CHECKPOINT_DEBUG_INDEX` 打开时由后台写者至多每 5 s 重写一次（[04](04-architecture.md)）。

### 7.2 级联回收

`pruneLocked`（`store.go:1293`）的条件是三个「且」：**隐藏 且 非基准 且 无子**。三者同时成立，说明没有任何祖先链或回滚路径经过它，可以从账本移除；
然后对它的父亲重复同样的判断，一路向上。爬升停下的那个条目若恰好「隐藏、只剩一个子节点」，就被排进合并队列。

基准移动时也做同样的检查（`setBaseLocked` → `pruneLocked`）：一个隐藏条目如果只是因为「身为基准」才被保留，基准一挪走它就该走。

> 这里的取舍是：宁可留下垃圾文件，也不留下一条坏链。直接删掉仍被依赖的文件，所有经过它的链会**静默地**读到错误内容。

能不能给 `delete` 加一个「强制删除仍被依赖的条目」的选项？要让它安全，就必须先把这个条目持有、而后代还要用的页搬进每一个后代 ——
这正是 §7.4 合并在「只有一个子节点」时做的事；有多个子节点时，同一批页要复制多份。所以现行实现只在合并条件成立时才真正回收，其余情况隐藏（推论）。

### 7.3 锁外：删文件

放锁之后、返回之前，`Delete` 执行 `reclaim`：删被移除条目的目录、被级联回收的祖先目录、隐藏条目的 snapfile 与 header、计数归零的层文件及其 `.meta`；
隐藏时的 manifest 也在锁外重写（`store.go:2016-2029`）。为什么放锁后删是安全的，见 [13](13-state-concurrency-durability.md)。
删除的失败于是分成三种结局，服务端 `deleteFailure`（`service.go:1482`）逐一对应：

| `Store.Delete` 返回 | 含义 | 服务端应答 |
|---|---|---|
| `ErrCheckpointNotFound`（`store.go:693`） | ID 不指向任何本代可删的条目：不存在、已隐藏（它已不是 API 对象）、或属于上一代的树。锁内就判定，什么都没动 | 404 `not_found` |
| `*DeleteCleanupError`（`store.go:718`） | 删除**已经生效**（条目已离开账本、不再是恢复目标、字节不再计费），只是被删条目自身的文件没清干净：移除时删不掉它的目录，或隐藏时写不了它的 manifest | 按成功应答，打一条 WARN；残留在沙箱目录下，随沙箱移除、新一代接管或下次启动清空一并回收 |
| 其余错误 | 账本还没改就失败了，什么都没删 | 500 `internal`，调用方可以重试 |

中间那一行为什么按成功应答？因为从调用方的角度删除已经完成：条目从 `list` 里消失了，此时若回错误，调用方重试只会得到 404，
反而让人以为「第一次没删成、第二次发现已经不在」。反过来，最后一行不能回 404 —— 那会告诉调用方「已经不在了」，而实际上什么都没删。
祖先与层的删除失败只记日志，不影响应答。对应的单元测试是 `service_test.go` 的 `TestDeleteOfNothingIsNotFound`、
`TestDeleteThatTookEffectIsASuccessEvenIfItsFilesStay`、`TestDeleteFailureSortsTheThreeOutcomes`。错误总表见 [25](25-errors-timeouts-concurrency.md#2-错误总表)。

### 7.4 结尾：合并

最后调用 `runCompaction`（`store.go:2034`，`compact.go:423`）。候选是**隐藏、非基准、已提交、恰有一个子节点**的条目 H；
做法是把 H 并入它唯一的子节点 C：C 接管 H 的父节点，回滚位图取并集，内容以 C 为准；两者的 rootfs 层在「永远成对出现」时一起合并。

开关与默认值（`CHECKPOINT_COMPACT` 默认开，`CHECKPOINT_COMPACT_MAX_PER_OP` 默认 8）见 [27 §2.3](27-configuration-and-capacity.md#23-树与存储行为)，合并的机制与证明见 [06 §10](06-memory-diff-tree.md#10-删除隐藏与合并)。放在失败语义里要说的是它怎么失败：

- 一次合并失败时树原样不动，**本轮合并就此结束**，候选排回队尾，等下一次 delete；
- 同一候选累计失败 3 次（`compactMaxAttempts`）后放弃，行为等同不合并；它若之后重新成为候选会再入队；
- 合并**从不**让触发它的 delete 失败。

文件工作全在全局锁外、沙箱操作锁内：先把页写到没人读的位置、以**新文件名**写并集侧车与合并层，最后短暂持锁检查树没变并一次性切换。
切换之前任何失败都只删掉新文件名，树原样不动。合并在 delete 里同步执行，所以 delete 的延迟随之上升，这是已知项（[22 §4.10](22-long-run-and-concurrency.md#410-删除同步合并delete-变慢)）。

合并追平时，非基准的隐藏条目至少有两个子节点，于是每沙箱条目数有上界 **≤ 2V + 1**（V 是可见条目数），证明见 [06](06-memory-diff-tree.md) §10.4。

为什么候选必须「恰有一个子节点」？合并不改变任何 restore 的论证（[06](06-memory-diff-tree.md) §10.3）依赖一个事实：**H 在任一回滚路径上当且仅当 C 在**。
只有一个子节点时这成立 —— 任何经过 H 的祖先链都必然继续经过 C。若 H 有两个子节点 C1、C2，把 H 并进 C1 之后，
通往 C2 的祖先链仍然要经过 H 的内容，而 H 已经不存在了：C2 那一支的内容解析会漏掉 H 持有的页。这就是条件不能放宽的原因。

---

## 8. 账本污染

[07](07-disk-layering.md) 讲过磁盘账本的 `poisoned` 标志。放在失败语义的框架里，它是一条规则：

> **既成事实与记账失败要分开处理。** 封存已经发生（层在活的栈里）是既成事实，账本跟不上是记账失败。
> 不能因为记账失败就假装封存没发生 —— 那会让后面每一个视图都少一层，restore 静默读到旧数据。

处理方式是把账本标记为不可信、拒绝再 checkpoint（`reason` `rootfs_poisoned`），而不是留下一个看起来正常的错账本。
污染由一次成功的 restore 清除：`SetRootfsToEntry` 用目标 checkpoint 自己的 header 重新播种，并回收记下的「未登记」层。
restore 在污染期间仍然可用，因为它不依赖当前账本：它用的是目标条目自己记下的视图。

同一条规则在内存那边的体现就是 `CommitHidden`：快照已经写了，这是既成事实；后续步骤失败不能让它消失。

---

## 9. 不变量清单

**改代码时不能破坏的。** 注意有多少条被破坏后是**静默**的。

| # | 不变量 | 破坏后果 | 静默？ |
|---|---|---|---|
| 1 | 差分条目**必须**有侧车 | 该条目不可用 | 否（`commitFiles` 报错） |
| 2 | 纪元位图 `E_x` 恰好覆盖 `(parent(x), x]` | 回滚集不再是超集 → **漏页** | **是** |
| 3 | 物化文件覆盖 Firecracker 实际写回的全部页 | 从空洞读到零 → 清零若干页 | 否（Firecracker 覆盖校验在提交点前拒绝） |
| 4 | 恢复目标与运行中的虚机**同拓扑** | 往不存在的对象写 | 否（`validate_topology`） |
| 5 | 内存与磁盘在**同一个**暂停窗口内 | guest 看到两个时刻的世界 | 部分 |
| 6 | 活跃脏图在**暂停之后**导出 | 同 #3 | 否（同上兜底） |
| 7 | 提交前条目对 `Get` / `List` **不可见**（`prepared` 条目根本不进账本；磁盘上的 `prepared` manifest 只供事后查看，从不被读回） | 半写完的快照成为恢复目标 | **是** |
| 8 | 纪元前进后产物必须保留（哪怕隐藏） | 时间线开洞 → 后续回滚漏页 | **是** |
| 9 | 账本与活的层栈一致，不一致就拒绝服务 | 视图少一层 → 读到旧数据 | **是** |
| 10 | conntrack 在恢复之前清完 | 残留表项让连接挂死 | **是**（挂死无报错） |
| 11 | VMGenID 在内存回写**之后** | guest 不知道时间被回拨 | **是** |
| 12 | 队列页在阶段 9 后重新标脏 | 下一代 Diff 漏页 → 同 #2 | **是** |
| 13 | HDBSS 在 vCPU 创建**之后**武装（[05](05-dirty-page-tracking-and-hdbss.md) §4.3） | 一条脏页都记不到 → 每次都退化成全量 | **是**（只表现为慢；目前只靠调用点位置保证） |
| 14 | 恢复虚机是无条件的 | 虚机停在暂停态，所有人都以为它在跑 | 部分 |
| 15 | **合并前后回滚集不变**：H 在任一回滚路径上当且仅当 C 在，合并后 `rev(C') = rev(H) ∪ rev(C)` | restore 少回滚或多回滚若干页 | **是** |
| 16 | **合并前后内容解析不变**：C 持有的页以 C 为准，其余取 H 的 | restore 读到错误时刻的页 | **是** |
| 17 | 合并只在沙箱操作锁内做，切换前检查树没变 | 与并发 restore 交错，读到半切换的树 | **是** |
| 18 | full 条目的回滚因子恒为全 1，不依赖侧车内容 | 跨树回滚漏掉丢失纪元里写过的页 | **是** |
| 19 | 子节点计数表、层引用计数、字节计数与账本一致；层只在最后一个引用它的视图消失时回收 | 删早了：restore 读不到层；删晚了：盘泄漏 | 删早了**是** |

19 条里多数被破坏后是静默的。这就是为什么代码里到处是「宁可报错」的分支，以及单元测试为什么以属性测试逐步重算这些计数、
比对合并与不合并两份账本的 restore 结果（[19](19-testing-and-functional-verification.md)）。

另有三处**保护性检查**，把原本静默或致命的情形变成一次明确的失败。它们是上表 #3 与节点级隔离的执行手段，改相关代码时同样不能拿掉：

| 检查 | 没有它的后果 | 位置 |
|---|---|---|
| 物化回滚集时，差分文件**短读即报错**（指出页号、文件、读到多少），不补零 | 被截断的差分让若干页以全零写回 guest，无报错 | `internal/checkpoint/store.go` — `writeRevertMem` 的 `copyExtent` |
| 稀疏缓存文件的 mmap 读写由 `guardMmapFault` 包住，缺页失败（如产物盘写满时的 `SIGBUS`）转成该次 I/O 的错误；拷贝前的 `MADV_POPULATE` 遇到同类错误同样返回错误 | `SIGBUS` 对 Go 进程是致命信号：orchestrator 退出，节点上全部沙箱一起消失 | `internal/sandbox/block/cache.go`、`populate.go` |
| 沙箱被移除时，`OnRemove` 先取该沙箱的操作锁再删文件；checkpoint / restore 取到锁后重新确认沙箱还在、还是同一代（`refuseIfGone`） | 删除与进行中的 restore 抢同一批文件：提交点之后是撕裂，之前是凭空的 `ENOENT`；反方向则给已不存在的沙箱重建目录、永不回收 | `internal/checkpoint/service.go` — `OnRemove`、`refuseIfGone` |

---

## 10. 错误契约

四个 RPC 失败时响应体是 JSON：`code`（Connect 错误码）、`reason`、`message`。**`code` 只说这次调用怎么结束，`reason` 才说调用方下一步该做什么** ——
同一个 `internal` 下面就有三种要求不同动作的失败：普通的 `internal`（沙箱在原状态，可换目标重试）、`rootfs_poisoned`（restore 一次即可恢复 checkpoint）、
`guest_unresponsive`（回滚已完成，由调用方按业务决定）。SDK 先按 `reason` 选异常类，服务端没给 `reason` 时才退回按 `code` 选。
全部 `reason`、HTTP 状态、SDK 异常类与调用方动作的对照表，以及错误在 Firecracker、FC 客户端、沙箱层、服务层、SDK 各层的形态，见
[25](25-errors-timeouts-concurrency.md)。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 失败分类 | `firecracker/src/vmm/src/rollback.rs` — `RollbackError::faults_vm` |
| 撕裂、恢复虚机 | `packages/orchestrator/internal/sandbox/checkpoint.go` — `RollbackTornError`、`resumeOnError`；`internal/checkpoint/service.go` — `markTorn`、`refuseIfTorn` |
| 三层降级 | `internal/checkpoint/service.go` — `failCreate` |
| 纪元标志 | `internal/sandbox/checkpoint.go` — `CheckpointToFiles` 的 `epochAdvanced` |
| 提交与隐藏提交 | `internal/checkpoint/store.go` — `Commit`、`CommitHidden`、`commitFiles`、`publishLocked` |
| 断链 | 同上 — `InvalidateBase`、`MaterializeRevert` 里的 `base.invalid` |
| 删除与级联回收 | 同上 — `Delete`、`deleteLocked`、`pruneLocked`、`setBaseLocked`、`reclaim`、`hasChildLocked`；`ErrCheckpointNotFound`、`DeleteCleanupError` |
| 删除失败的三种应答 | `internal/checkpoint/service.go` — `delete`、`deleteFailure` |
| 合并 | `internal/checkpoint/compact.go` — `queueCompactLocked`、`runCompaction`、`fold`、`retryCompactLocked`；`compact_layers.go` |
| 层引用 | `internal/checkpoint/layer_refs.go` |
| 账本污染 | `internal/checkpoint/rootfs.go` — `PoisonRootfs`、`SetRootfsToEntry` |
| envd 超时 | `internal/checkpoint/service.go` — `envdRestoreTimeout` |

---

## 本章要点

1. 第一原则：**宁可拒绝服务，不可返回一个可能已损坏的沙箱**。没有兜底路线之后，这从习惯变成了硬要求。
2. **提交点**把失败切成两半，`faults_vm()` 把分类固化在代码里；编排侧再加上三种撕裂：`fault: true`、rollback 调用超时、`ResetView` 或其后的 resume 失败。
3. restore 四档失败：`chain_broken`（原状态，别重试）、`internal`（原状态，可换目标）、`torn`（只能重建，虚机刻意保持暂停）、
   `guest_unresponsive`（回滚已完成、不是撕裂）；45 s 的 envd 上限只决定由谁来报告失败。
4. **纪元不能丢**：快照一写完，那份差分就是那一代脏页的唯一副本；`epochAdvanced` 在封存失败和快照超时时都偏向保留；三层降级是正常提交 → 隐藏提交 → 标记断链。
5. **隐藏条目**在树里、不在 API 里，丢掉只有恢复才用的部分；**断链**后拒绝恢复、下一次强制全量起新根；**账本污染**把「既成事实」与「记账失败」分开处理。
6. **删除是依赖感知的**：被依赖就隐藏，叶子才移除并级联回收，文件放锁后删；删除结尾同步做合并，合并从不让 delete 失败、不改变任何 restore。
   删除失败分三种结局：`ErrCheckpointNotFound` → 404，`DeleteCleanupError` → 按成功应答并告警，其余 → 500。
7. 不变量 19 条，多数被破坏后是静默的；另有三处保护性检查把静默或致命的情形变成明确失败。
