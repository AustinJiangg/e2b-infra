# 14 · 生命周期边界

## 本章目标

读完本章，你应当能回答：

1. checkpoint 产物什么时候会消失？有哪几条代码路径？原生 pause / resume 之后为什么旧 checkpoint 全部作废、新一代又能照常使用？
2. 把产物目录完整拷走，能恢复吗？会依次卡在哪几步？
3. 缺的到底是哪四层东西？哪一层其实已经不缺？补齐每一层要付什么代价？
4. 为什么说补齐之后得到的基本就是 e2b 原生 snapshot，以及这条边界为什么是取舍而不是缺陷？
5. 绑定沙箱生命周期之后，为什么空间仍然需要四层机制来管？

---

上一章（[13](13-state-concurrency-durability.md)）在讲并发模型时用到了一个取舍：**账本只在进程内存里，不跨重启存活**，这让并发模型少了崩溃一致性这一整类问题。
本章把这个取舍往下推，回答一个一定会被问到的问题：checkpoint 产物是真实落盘的文件，看起来像是"留着就能用"，为什么其实不能 ——
即使把整个目录完整备份下来，也恢复不了。缺的是什么、补齐要付什么代价、补齐之后得到的是什么，以及绑定沙箱生命周期之后，空间为什么仍然需要管理。

本章是第三部分的最后一章。使用方能直接用的结论（什么时候失效、要长期保存怎么办、怎么删才释放空间）已经写在 [24](24-semantics-and-limits.md)，
本章只做推导。正文区分**代码事实**（给出位置）与**推论**（显式标注）。下一章（[15](15-native-increment-diagnosis.md)）起是第四部分，转向 e2b 原生快照本身的增量问题。

---

## 1. 前提：产物在哪、什么时候消失

目录布局见 [04](04-architecture.md)：每个沙箱一个目录，下面是各 checkpoint 的子目录（snapfile、内存差分、位图侧车、`rootfs.header`、manifest、timings）
和多代共享的 `layers/`（封存层文件与各自的 `.meta`）。

产物消失有三条代码路径：

| 触发 | 代码事实 |
|---|---|
| 沙箱从 orchestrator 的沙箱表中移除 | `Service.OnRemove`（`service.go:355`）取该沙箱的操作锁后调 `Store.RemoveSandbox`：锁内把整个沙箱目录改名为 `.trash-<uuid>` 并从账本摘掉全部状态，放锁后删除（`store.go:2148`、`:894`） |
| 同一个 id 的新一代接管 | `admitLocked` / `AdoptSandbox` 丢弃上一代留下的全部状态，同样先改名再删（`store.go:820`、`:2197`） |
| orchestrator 启动 | `NewStore` 先 `os.RemoveAll` 整个 store 根目录，再 `MkdirAll` 重建（`store.go:535-542`） |

### 1.1 代际边界的规则

`RemoveSandbox` 与原生 pause/resume 之间有一条规则，是理解整篇的钥匙：

- **「代」是 `Sandbox.LifecycleID`**，每换一个 Firecracker 进程就换一次。原生 resume 以**同一个沙箱 id** 现建一个新的 Firecracker 进程，所以是新的一代。
- 账本记录的是「**哪一代**拥有挂在这个 id 下的状态」（`store.go` 包注释、`owners`）：
  - 属主那一代继续用；
  - 本节点当前正在跑的新一代**接管**这个 id，并丢掉上一代的差分 —— 已经没有进程可以回滚到它们上面；
  - 其余的请求（排队期间被新一代超越的迟到请求）明确拒绝，不留半吊子状态。
- 读路径（`List`、`Get`、`Delete`）按同一个归属判定：新一代看不到上一代的条目，对旧 id 的 restore 得到 `not_found`；新一代可以照常从零开始 checkpoint，第一次是一棵新的全量根。
- 移除也按代：`OnRemove` 是异步触发、再等操作锁的，可能在 resume 已经把新一代放回来**之后**才到达。`RemoveSandbox` 发现 id 已被当前正在运行的那一代持有时什么也不做，
  **迟到的移除不会带走活沙箱的状态**。
- 撕裂标记同样按代：撕裂是关于一个 Firecracker 进程的事实，不是关于一个比它活得久的 id 的事实。

**为什么必须如此（推论）**：本方案的 restore 是原地回滚，要求那个活着的进程、那份活着的内存映射、那套活着的宿主资源还在（§3.1）。
上一代的差分链没有可以写回去的对象；保留它们只会让使用方拿到一个必定失败、或者更糟、悄悄恢复到别的进程上的 id。所以账本按代记账，而不是按 id 记账。

`OnRemove` 先取操作锁、再删文件，这个顺序本身也有理由：它要删的正是一个进行中的 restore 正在读的文件（snapfile、各代差分、封存层）。
在一个已过提交点的回滚底下把它们抽走，会把一次 kill 变成一个撕裂的沙箱。等锁有上限，超时就告警后照删 —— 那时沙箱无论如何都要走了。

---

## 2. 一个思想实验：把目录完整拷走

假设在删除之前把沙箱目录整个打包保存，然后试图恢复。按实际代码路径逐步走。

### 2.1 第一步：orchestrator 不认识它

`NewStore` 启动时先清空 store 根目录再重建，账本的每张表都是空的，**不扫描磁盘、不读取任何 manifest**（`store.go:535`）：

```go
func NewStore(root string) (*Store, error) {
    if err := os.RemoveAll(root); err != nil { ... }       // ← 上一个进程留下的，没有任何东西再引用
    if err := os.MkdirAll(root, 0o755); err != nil { ... }
    // … 账本的各张表全部新建为空 …
}
```

拷回来的目录若在启动之前放好，启动时就被删掉；启动之后再放进去，`List` 返回空，`Get` 找不到条目 —— **在 API 层面这个 checkpoint 不存在**。
这是刻意的，包注释写明：manifest 与 index「exist so a run can be inspected after the fact」，store 从不读回，也都活不过重启。

### 2.2 第二步：即使账本能重建，也没有沙箱可回滚

假设补上加载逻辑（信息是够的，见 §3.3）。下一道门是 `ServeCheckpoint` 的第一行（`service.go:405`）：

```go
sbx, ok := s.sandboxes.Get(sandboxID)
if !ok {
    writeError(w, http.StatusNotFound, "not_found", reasonNotFound, fmt.Sprintf("sandbox %s not found", sandboxID))
    return
}
```

restore 的整个流程 —— 暂停虚机、导出活跃脏页位图、原地写回、切换磁盘视图 —— 每一步都作用在一个**活着的沙箱对象**上。没有它，流程无从启动。

### 2.3 第三步：换一个活沙箱顶上也不行

用同一个模板起一个新沙箱，把保存下来的 checkpoint 挂到它名下。至少三处失效：

| 失效点 | 说明 |
|---|---|
| **路径与归属** | 账本里记录的内存差分、snapfile、层文件都是**绝对路径**，指向原沙箱的目录；新沙箱是另一个 id、另一代，归属规则（§1.1）也不认 |
| **磁盘底座** | 磁盘视图永远从模板 rootfs 起步再叠封存层（§3.2），模板必须在位且是同一个 |
| **正确性前提** | 回滚集的推导依赖「当前基准与目标在**同一棵树**上」，或至少能用跨树规则界定差异。一个新起的沙箱的内存不是树上任何一个节点的状态 |

> **推论**：前两条是工程问题，改路径、对模板可以绕过；第三条是**语义问题**。即便凑巧算出一个覆盖足够的回滚集，那也是巧合而不是设计保证，
> 没有任何测试覆盖这个形态，不应依赖。

---

## 3. 四层依赖

### 3.1 恢复机制要求活体

**只有一条恢复路径**：`RollbackInPlace`，往活着的 Firecracker 对象上写（`store.go` 包注释："Restoring rolls the live Firecracker process back in place; there is no rebuild route"）。
"杀掉进程、从文件重建"那条路在设计上就没有做 —— 这是有意为之，不是遗漏：产品定位是活沙箱内的高频回退，进程已死、虚机撕裂的场景明确划给原生 snapshot
（设计目标见 [03](03-goals-and-design-choices.md)）。Firecracker 的 rollback 端点头两步就拒绝非活体：虚机必须处于 `Paused`，
`validate_topology` 要求快照拓扑与**运行中的虚机**一致（[09](09-in-place-rollback.md)）。回滚是把状态写到既有的 KVM fd、GIC 设备、virtio 对象上，对象不存在就没有可写的目标。

还有一处更本质。回滚集的定义是

```
revert = 树路径上各代的纪元位图并集  ∪  Firecracker 当前的活跃脏页
```

后一项描述的是「**当前**虚机相对上次快照改了什么」—— 这是**当前状态的属性，不是快照的属性**。没有当前状态，这一项无从谈起。

> **推论**：离线恢复本来也不需要它，直接全量加载即可。它不是障碍，而是「原地回滚」这个形态的必然产物 —— 恰恰说明这套机制是围绕活体设计的。

### 3.2 磁盘产物不自足

`AssembleView`（`internal/sandbox/checkpoint.go:339`）永远以模板 rootfs 为底，再把各封存层叠进层栈：

```go
base, err := s.Template.Rootfs()                   // ← 底座永远是模板
stack := block.NewLayerStack(base, size)
for _, layer := range layers {
    cache, _ := block.NewCache(size, blockSize, layer.Path, false)
    cache.MarkCached(layer.DirtyOffsets)
    stack.AppendLayerWithBlocks(cache, layer.DirtyOffsets)
}
```

封存层只含**被写过的块**，没被写过的块 —— 绝大多数 —— 要回模板取；`rootfs.header` 里的合并映射表，底座那些项也指向模板的 build。
所以**脱离模板，磁盘读不出完整内容**：产物不是一份完整磁盘镜像，而是「相对模板的增量」。

要做成可导出，需要把层栈压平、把模板底座物化进产物。合理的做法是一个独立的 export 接口，而不是改 checkpoint 的默认路径 ——
原地回滚场景下沙箱活着、模板必然在位，为一个不发生的场景让每次 checkpoint 付全量代价不划算。

### 3.3 账本不从磁盘加载

`NewStore` 不读盘（§2.1）。有意思的是**磁盘上的信息其实基本够**：

| 文件 | 记录了什么 |
|---|---|
| `manifest.json` | 整个条目：`ParentID`、`MemMode`、`Rootfs` 视图、各文件路径、状态、是否隐藏 |
| 层的 `.meta` | 该封存层持有哪些块偏移 |
| `index.json` | 该沙箱的条目清单 —— **默认不写**，只在 `CHECKPOINT_DEBUG_INDEX` 打开时由后台写者至多每 5 s 重写一次 |

父指针在、模式在、层清单在 —— 同一个进程存活期间，理论上足以重建条目树（推论）。**缺的是读取它的代码**，以及那些只在内存里的派生状态
（子节点计数、层引用计数、字节计数、合并（compact / fold）队列、代际归属）的重建。要跨进程、跨崩溃则还缺一样：这些文件都不 fsync，启动时又整目录清空
（[13](13-state-concurrency-durability.md)），落盘的完整性本身不在承诺之内。

这是取舍而非疏忽：让账本跨重启存活，就要处理一整类新问题 ——

- 陈旧条目的回收：一台机器上积累了多少个已消失沙箱的目录？
- 与已消失沙箱的对账：沙箱没了但目录还在，谁负责清？
- 格式版本迁移：`manifest.json` 的结构变了怎么办？

而这些问题**只在「checkpoint 能脱离沙箱」的前提下才有意义**。前提不成立，问题就不存在 —— 这正是 [13](13-state-concurrency-durability.md) §1 说的「并发模型少了一整类问题」。

### 3.4 缺运行时环境

Firecracker 进程、KVM fd、eventfd、irqfd / ioeventfd 注册、tap、网络槽位、NBD 设备 —— 全是活体资源。**原地回滚的全部价值就在于一个都不重建**
（[09](09-in-place-rollback.md)），这正是它快的原因，也正是它离不开沙箱的原因。**同一件事的两面。**

---

## 4. 内存那一半其实已经自足

这一节值得单独说明，因为它决定了「补齐」的难度。

`CHECKPOINT_FULL_ROOT` 默认开启时，每棵树的根是一次全量捕获。内容解析沿祖先链回溯，遇到全量条目即终止，不会回落到模板 memfile；
全量条目的回滚因子恒为全 1，不依赖侧车内容（[06](06-memory-diff-tree.md)）。

全量根这个默认值本来是为别的目的定的：`fullRootEnabled` 的注释写明，关掉它会让树根变成相对启动内存源的差分，restore 时早于所有 checkpoint 的页
要从模板 memfile 解析，在集群部署下这意味着**回滚过程中去访问对象存储**。全量根用第一次 checkpoint 多写一份内存的代价，
把这个跨网络依赖从回滚路径上拿掉了。它带来的副作用是：

> **内存差分树在数据上已经是自包含的。** 四层缺口里内存这一层已经不缺，真正的数据缺口只有磁盘那一半。

把 `CHECKPOINT_FULL_ROOT` 关掉，这条不再成立 —— 树根变成相对启动内存源的差分，内存半边也失去自足性。

---

## 5. 补齐需要什么

| 缺口 | 补法 | 代价 |
|---|---|---|
| 内存全量镜像 | **已具备**。回滚数据的解析器稍加改造即可物化出任意一代的完整内存 | 小 |
| 磁盘完整镜像 | 层栈压平，把模板底座物化进产物 | 中；产物体积回到全量 |
| 账本 | `NewStore` 不再清空根目录，增加从 manifest 扫描重建（含派生计数）的路径 | 小到中；引入陈旧条目回收与格式迁移问题 |
| 落盘 | 把产物与 manifest 的 fsync 按事务顺序（数据先于账本）加回来 | 并发 checkpoint 的延迟回升：ext4 日志提交是全文件系统的串行点 |
| 恢复路径 | 一条 load-from-files：新起 Firecracker 进程 + 加载 snapfile + 挂内存与磁盘 | 中 |

**补齐之后得到的是什么**：全量产物、新建进程、跨节点可用、不依赖原沙箱 —— **这就是 e2b 原生 snapshot 的定义**。
沿着「让 checkpoint 能脱离沙箱」这条路走到底，终点是一个已经存在的东西，而沿途会逐项丢掉这套方案的收益：

| 补齐动作 | 丢掉的收益 |
|---|---|
| 磁盘层栈压平 | 零拷贝封存 —— 封存重新变成一次全量导出（[07](07-disk-layering.md)） |
| 产物自足 | 「成本只与改动量有关」不再成立（[06](06-memory-diff-tree.md)） |
| 从文件重建 | 原地回滚的低延迟 —— 回到进程重建 + 工作集换页（[09](09-in-place-rollback.md)） |

---

## 6. 这是取舍，不是缺陷

> **用「绑定沙箱生命周期」换掉产物自足性与账本持久化，换来零拷贝封存、原地回滚、以及只随回退跨度增长的成本曲线。**

两条路径解决不同的问题，并且**并存** —— 本方案没有改动原生路径的语义：原生 snapshot 的产物自足、进持久存储、独立于沙箱存活，靠新建进程加载，适合迁移、持久化与崩溃恢复；checkpoint 的产物是驻留宿主本地的增量、随 Firecracker 进程（一代）回收，在活体上原地回滚，适合高频快速回退。逐项对比表只在 [18 §3](18-native-and-checkpoint-together.md#3-逐项对比)。

两者怎么配合（先 checkpoint 回到已知良好点，再对该状态做原生 snapshot；checkpoint 之后再做原生 pause 的正确性前提）见
[18](18-native-and-checkpoint-together.md)；机制层面的正确性前提在 [18 §7](18-native-and-checkpoint-together.md#7-checkpoint-之后做原生-pause-的正确性前提)与 [16 §4](16-native-increment-fix.md#4-与-checkpoint-叠加累积位图)。

---

## 7. 推论：绑定生命周期之后，空间仍然要管

随沙箱回收解决了一半的空间问题：**沙箱一走，它占的盘就全回来了**，不需要为「已消失沙箱的遗留文件」设清理任务或过期策略 ——
`RemoveSandbox` 整树回收、`NewStore` 启动清空，就是全部的清理。

但另一半问题在沙箱**活着的期间**：checkpoint 不会自动过期，一个循环打点的客户端可以一直占用产物盘；而产物盘是整个节点共享的，写满的后果不是一次 checkpoint 失败，
而是节点上每个沙箱往稀疏映射里的写都可能失败。更隐蔽的是，调用方删掉的 checkpoint 并不一定释放空间 —— 仍被后代依赖的条目只会被隐藏，
「保留最新 N 个、删最旧」这种常见用法恰好每次都删掉下一个的父节点，隐藏条目与它们的层会一直累积。

所以现行实现有四层机制，各管一段（各自的开关、默认值与取值建议只在 [27 §2](27-configuration-and-capacity.md#2-开关总表) 与 [27 §4](27-configuration-and-capacity.md#4-容量规划) 定义）：

- **节点级水位**：产物盘可用空间不足时，checkpoint 与 restore 在动手之前被拒，把「节点写满」变成调用方能处理的应答；
- **每沙箱个数上限**：只数可见的 checkpoint；
- **每沙箱字节上限**：数实际占用的块，restore 不受限；
- **合并**（compact / fold）：把「隐藏、只有一个子节点」的条目并入子节点，使每沙箱条目数 ≤ 2V + 1；层按视图引用计数，最后一个引用它的视图消失时回收。

几条推论：

- **个数上限不足以控制字节**：隐藏条目不计入个数，而一个 checkpoint 的大小从几页到一整份 guest 内存不等。合并把隐藏条目的数量压到有界，字节上限才管得住总量。
- **字节上限有一个下限**：删到只剩基准时，基准是一棵约一份 guest 内存大小的全量根，上限再小就删什么都回不来。下限的推导与告警阈值见 [27 §4.4](27-configuration-and-capacity.md#44-字节上限怎么选)。
- **怎么删才释放空间**取决于合并能释放什么：删基准几乎不释放；删较老的非基准条目会触发合并，释放两者重叠的那部分页。
  给使用方的操作建议在 [24 §4.4](24-semantics-and-limits.md#44-在字节上限下删哪个才能释放空间)。
- **脏页跟踪关闭时不合并**：每个 checkpoint 都是全量根，没有「只有一个子节点的隐藏条目」，层照样累积。这是设计边界，交付环境都开着跟踪。

开关的取值与容量规划见 [27](27-configuration-and-capacity.md)，删除与合并的机制见 [06](06-memory-diff-tree.md)、[12](12-failure-semantics.md)。

---

## 代码位置

| 本篇提到的行为 | 位置 |
|---|---|
| 沙箱移除、代际接管 | `packages/orchestrator/internal/checkpoint/service.go` — `OnRemove`、`OnInsert`；`store.go` — `RemoveSandbox`、`AdoptSandbox`、`admitLocked`、`dropStateLocked` |
| 启动清空、不从磁盘加载 | `store.go` — `NewStore` 及包注释 |
| manifest / debug index | 同上 — `writeManifest`、`DebugIndex`、`runIndexWriter` |
| restore 第一步取沙箱 | `service.go` — `ServeCheckpoint` |
| 唯一的恢复路径 | `internal/sandbox/checkpoint.go` — `RollbackInPlace` |
| 磁盘视图从模板底座起步 | 同上 — `AssembleView` |
| 回滚集与内容解析、全量根 | `store.go` — `MaterializeRevert`、`entryBitmap`；`service.go` — `fullRootEnabled` |
| 空间管理 | `service.go` — `minFreeBytes`、`refuseIfDiskFull`、`warnIfByteLimitTooSmall`；`store.go` — `atCheckpointLimitLocked`；`quota.go`；`compact.go`；`layer_refs.go` |

---

## 本章要点

1. checkpoint 产物随**一代沙箱**（一个 Firecracker 进程）回收：沙箱移除整树回收，新一代接管同一个 id 时丢弃上一代，orchestrator 启动清空整个 store。
2. 因果顺序不是「因为恢复不了所以删」—— **即使完整保存下来也恢复不了**。
3. 缺的是四层：**活体虚机、自足的磁盘产物、可加载的账本、运行时环境**。账本那层缺的主要是代码不是数据。
4. 内存那一半因为全量根，**在数据上已经自足**；真正的数据缺口只有磁盘。
5. 补齐这四层得到的**基本就是 e2b 原生 snapshot**，而沿途会逐项丢掉这套方案的收益。
6. 绑定生命周期免去了遗留文件的清理，但沙箱活着期间的空间仍要管：节点水位、个数上限、字节上限、合并与层引用计数回收。
