# 16 · 生命周期与可移植性边界

> checkpoint 产物是**真实落盘的文件**，看起来像是「留着就能用」。其实不能 ——
> 即使把整个目录完整备份下来，也恢复不了。本篇讲清楚缺的是什么、补齐要付什么代价、
> 以及这条边界对使用方意味着什么。
>
> **读者**：工程师、使用方、评审。
> **预备**：[第 8 篇 · 内存差分树](08-memory-diff-tree.md)、
> [第 10 篇 · 磁盘分层](10-disk-layering.md)。知道差分树和分层封存的大意即可。
> **代码**：`internal/checkpoint/{service,store}.go`、`internal/sandbox/checkpoint.go`
>
> 正文区分两类陈述：**代码事实**有明确出处（篇末列了位置），**推论**是基于这些事实的判断，
> 会显式标注。

---

## 0. 本篇要回答的问题

1. checkpoint 产物什么时候会消失？有哪几种触发方式？
2. 把产物目录完整拷走，能恢复吗？会卡在哪一步？
3. 缺的到底是哪些东西？补齐要付什么代价？
4. 补齐之后得到的是什么？
5. 使用方应该怎么用它、不该怎么用它？

---

## 1. 产物的生命周期

### 1.1 产物在哪

单机部署下 checkpoint 落在 `/orchestrator/build/checkpoints/<sandbox-id>/`：

```
<sandbox-id>/
├── <checkpoint-id>/
│   ├── snapfile        # Firecracker vmstate：vCPU、GIC、设备状态
│   ├── mem_diff        # 稀疏文件，本代脏页
│   ├── mem_bitmap      # 本代纪元位图
│   ├── rootfs.header   # 该时刻磁盘视图的合并映射表
│   ├── manifest.json   # 该 checkpoint 的完整条目记录
│   └── timings.json
├── layers/             # 封存写层，多代共享
├── index.json
└── last-restore-timings.json
```

它们是**真实落盘的文件**，不是内存里的临时对象。这也正是这个问题会被反复问到的原因 ——
东西就在盘上，看起来像是「拷一份就能留住」。

### 1.2 什么时候被删

沙箱从 orchestrator 的沙箱表中移除时，触发订阅回调：

```go
// OnRemove implements sandbox.MapSubscriber. Checkpoints are host-local state
// with the sandbox's lifetime, so they go with it.
func (s *Service) OnRemove(sandboxID string) {
    if err := s.store.RemoveSandbox(sandboxID); err != nil { ... }
}
```

`RemoveSandbox` 做两件事：

```go
delete(s.bySandbox, sandboxID)
delete(s.bases, sandboxID)
delete(s.rootfs, sandboxID)
delete(s.opLocks, sandboxID)

os.RemoveAll(filepath.Join(s.root, sandboxID))   // ← 整个目录，连同 layers/
```

### 1.3 什么情况下 checkpoint 会失效

| 事件 | 结果 |
|---|---|
| 沙箱正常结束 / 被删除 | 该沙箱的全部 checkpoint 立即删除 |
| orchestrator 重启 | 同上 —— 重启本来就会带走这台机器上的所有沙箱；上一个进程留在盘上的 checkpoint 文件由 `NewStore` 在启动时整目录清空（[第 15 篇 §5](15-state-and-concurrency.md#5-原子提交与持久性)） |
| 宿主重启或宕机 | 同上 |
| 沙箱迁移到别的节点 | checkpoint **不跟随**，留在原节点直至清理 |
| 沙箱被原生 `pause` 再 `connect`（等同 resume） | pause 之前的 checkpoint **全部作废**：`list` 返回空，对旧 checkpoint id 的 restore 回 `not_found`。resume 之后的这一代**可以照常重新 checkpoint / restore**。详见 [§1.4](#14-原生-pause--resume-与-checkpoint-的代际边界) |
| 单个 checkpoint 被显式删除 | 仍被后代依赖则转为隐藏保留，否则物理删除并级联回收祖先（[第 14 篇 §7](14-failure-semantics.md#7-删除依赖感知的回收)） |

pause 那一行是这张表里唯一**不是「沙箱没了」**的失效原因，值得多说一句：
pause 把当时的层栈整体压进沙箱快照，账本又不从磁盘加载（[§3.3](#33-账本不从磁盘加载)），
两件事叠加就得到「跨 pause 的 checkpoint id 无效」。这条边界有实测印证 ——
兼容矩阵把它判成 `REFUSED`（明确拒绝、不留半吊子状态），不是 `BROKEN`，
同一轮里 `checkpoint.list` 返回 0 个、pause 之后新建的 checkpoint 能正常 restore
（[第 25 篇 §5](25-functional-tests.md#5-compat_matrixpy与原生生命周期的组合矩阵)）。
**checkpoint 与原生生命周期操作并存但不交叉**，是边界，不是缺陷。

### 1.4 原生 pause / resume 与 checkpoint 的代际边界

这条边界是使用方最容易撞上的一条，值得把规则完整写一遍。

**规则（一句话）**：**原生 pause 会让该沙箱的全部 checkpoint 作废；resume 之后，同一个沙箱 id
底下是"新的一代"，它可以从零开始重新 checkpoint / restore。**

| 时刻 | `checkpoint.list` | 对 pause 之前某个 checkpoint id 做 restore | 新建 checkpoint |
|---|---|---|---|
| pause 之前 | 列出这一代的全部条目 | 正常 | 正常 |
| resume 之后 | **空** | **`not_found`** | **正常**，并且从这一代的第一次起是一棵新的全量树根 |

**为什么必须如此。** 原生 pause 把沙箱从这个节点上取下来，resume 是**以同一个沙箱 id 现建一个新的
Firecracker 进程**（[第 4 篇 §5](04-e2b-native-snapshot.md)）。而本方案的 restore 是**原地回滚**：
它要求那个活着的进程、那份活着的内存映射、那套活着的宿主资源还在（[§3.1](#31-恢复机制要求活体)）。
旧那一代的差分链没有可以写回去的对象了 —— 保留它们只会让使用方拿到一个必定失败、
或者更糟、悄悄恢复到别的进程上的 id。所以账本按**代**记账，而不是按 id 记账。

**"代"是什么**：`Sandbox.LifecycleID`，每换一个 Firecracker 进程就换一次。
账本记的是"**哪一代**拥有挂在这个 sandbox id 下的状态"：

- 属主那一代继续用；
- 本节点当前正在跑的新一代**接管**这个 id，并丢掉上一代留下的差分（已经没有进程可以回滚到它们上面）；
- 其余的（被后来者超越的迟到请求）明确拒绝，不留半吊子状态。

读路径（`list` / 取条目 / 删除）按同一个归属判定，所以上面那张表的三格是同一条规则的三个侧面。
沙箱移除也是分代的 —— 它是异步触发、再等操作锁，可能在 resume 已经把新进程放回来**之后**才到达，
所以它会先确认自己是不是还适用，**迟到的移除不会带走活沙箱的状态**；
撕裂标记（[第 14 篇 §3](14-failure-semantics.md)）同样点名它判死的是哪一代 ——
撕裂是关于一个 Firecracker 进程的事实，不是关于一个比它活得久的 id 的事实。

> **2026-09-21 修掉的一处缺陷**：在此之前，账本把"沙箱被移除"当成对这个 id 的**终局**
> （写一条墓碑），于是**第一次原生 pause 之后，同一个 id 的新一代连 checkpoint 都建不起来**，
> 永久返回 `sandbox has been removed from this node: refusing to recreate its checkpoint directory`。
> 上表的第三列当时是坏的，与交付文档 `deploy/CHECKPOINT.md` §6 写明的边界相反。
> 修法就是上面那套按代记账（提交 `4af2872c6`）。回归用例 **T41 场景 c**（resume 之后再
> checkpoint → 改数据 → restore 回去，14 条断言）与 **场景 d**（checkpoint → pause → resume 后
> `list` 为空、旧 id restore 被拒且 `code=not_found`、新建的 checkpoint 完整可用，18 条断言）
> 各自锁住一半；场景 c 正是它把缺陷抓出来的。

---

## 2. 一个思想实验：把目录完整拷走

假设在删除之前把 `<sandbox-id>/` 整个打包保存下来，然后试图恢复。按实际代码路径逐步走。

### 2.1 第一步：orchestrator 不认识它

`NewStore` 启动时**先把 store 根目录整个清空**、再 `MkdirAll` 建出来。三张表都是空的，
**不扫描磁盘、不读取任何 manifest**：

```go
func NewStore(root string) (*Store, error) {
    if err := os.RemoveAll(root); err != nil { ... }      // ← 上一个进程留下的，没有任何东西再引用
    if err := os.MkdirAll(root, 0o755); err != nil { ... }
    return &Store{
        root:      root,
        bySandbox: make(map[string]map[string]*Entry),   // ← 空
        bases:     make(map[string]baseRef),             // ← 空
        rootfs:    make(map[string]*rootfsState),        // ← 空
        ...
    }, nil
}
```

所以拷过去的目录如果在 orchestrator 启动之前放好，启动时就被删掉了；启动之后再放进去，
`ListCheckpoints` 返回空，`Get` 找不到条目。**在 API 层面，这个 checkpoint 不存在。**
根目录是 store 独占的（`<产物根>/checkpoints`），没有别的东西往里写。

这是刻意的。包注释写明：

> The on-disk index exists so a run can be inspected after the fact, not to survive a restart.

### 2.2 第二步：即使账本能重建，也没有沙箱可回滚

假设补上加载逻辑（信息是够的，见[§3.3](#33-账本不从磁盘加载)）。下一道门是
`ServeCheckpoint` 的第一行：

```go
sbx, ok := s.sandboxes.Get(sandboxID)
if !ok {
    writeError(w, http.StatusNotFound, "not_found", ...)
    return
}
```

restore 的整个流程 —— 暂停虚机、导出活跃脏页位图、原地写回、切换磁盘视图 ——
每一步都作用在一个**活着的沙箱对象**上。没有它，流程无从启动。

### 2.3 第三步：换一个活沙箱顶上也不行

假设用同一个模板起一个新沙箱，把保存下来的 checkpoint 挂到它名下。至少三处会失效：

| 失效点 | 说明 |
|---|---|
| **路径** | 账本里记录的 `mem_diff`、`snapfile`、封存层文件都是**绝对路径**，指向原沙箱的 store 目录。新沙箱的目录按新的 sandbox id 建立，路径全部对不上 |
| **磁盘底座** | 磁盘视图永远从模板 rootfs 起步再叠封存层（[§3.2](#32-磁盘产物不自足)）。模板必须在位且是同一个 |
| **正确性前提** | 回滚集的推导依赖「当前基准与目标在**同一棵树**上」这个不变量。一个新起的沙箱不在这棵树上 —— 它的内存状态不是树上任何一个节点 |

> **推论**：前两条是工程问题，改路径、对模板可以绕过；第三条是**语义问题**。
> 即便凑巧能算出一个覆盖足够的回滚集，那也是巧合，不是设计保证 ——
> 没有任何测试覆盖这个形态，不应依赖。

---

## 3. 四层依赖

### 3.1 恢复机制要求活体

**只有一条恢复路径**：`RollbackInPlace`，往活着的 Firecracker 对象上写。
kill-and-rebuild 那条 `RestoreFromFiles` 在设计阶段连同骨架一起删除了 —— 这是有意为之，
不是遗漏：产品定位是活沙箱内的高频回退，进程已死的场景明确划给原生 snapshot
（[第 5 篇](05-design-goals.md)）。

Firecracker 侧的 rollback 端点头两步就拒绝非活体：

| 检查 | 失败时 |
|---|---|
| 虚机必须处于 `Paused` 状态 | 返回 `NotPaused` |
| `validate_topology`：快照描述的拓扑必须与**运行中的虚机**一致 | 返回校验错误 |

拓扑检查不是形式主义：回滚是把状态写到**既有的** KVM fd、**既有的** GIC 设备、
**既有的** virtio 对象上（[第 11 篇 §4](11-in-place-rollback.md#4-validate_topology为什么必须一致)）。
对象不存在，就没有可写的目标。

还有一处更本质。回滚集的定义是：

```
revert = 树路径上各代的纪元位图并集  ∪  Firecracker 当前的活跃脏页
```

后一项描述的是「**当前**虚机相对上次快照改了什么」—— 这是**当前状态的属性，不是快照的属性**。
没有当前状态，这一项无从谈起。

> 当然，离线恢复本来也不需要它：直接全量加载即可。所以它不是障碍，
> 而是「原地回滚」这个形态的**必然产物** —— 恰恰说明这套机制是围绕活体设计的。

### 3.2 磁盘产物不自足

`assembleView` 永远从模板 rootfs 起步，再往上叠封存层：

```go
base, err := s.Template.Rootfs()          // ← 起点永远是模板
var device block.ReadonlyDevice = base
for _, layer := range layers {
    cache, _ := block.NewCache(size, blockSize, layer.Path, false)
    cache.MarkCached(layer.DirtyOffsets)
    device = block.NewSealedView(device, cache)
}
```

`rootfs.header` 里的合并映射表是「模板 rootfs 的映射 + 各封存层的恒等映射」，
底座那些映射项指向**模板的 build**。

封存层只含**被写过的块**。没被写过的块 —— 也就是绝大多数 —— 要回模板取。

所以：**脱离模板，磁盘读不出完整内容。** 产物不是一份完整磁盘镜像，
而是「相对模板的增量」。

要做成可导出，需要把层栈压平、把模板底座物化进产物。这件事讨论过，
结论是做成**独立的 export 接口**，而不改 create 的默认路径 ——
就地回滚场景下沙箱活着、模板必然在位，为一个不发生的场景付全量代价不划算。

### 3.3 账本不从磁盘加载

如[§2.1](#21-第一步orchestrator-不认识它)所述，`NewStore` 不读盘。

有意思的是**磁盘上的信息其实是够的**：

| 文件 | 记录了什么 |
|---|---|
| `manifest.json` | 整个条目的 JSON：`ParentID`、`MemMode`、`Rootfs` 视图、各文件路径、状态 |
| `<layer>.meta` | 该封存层持有哪些块偏移 |
| `index.json` | 该沙箱的条目清单 |

父指针在、模式在、层清单在 —— 同一个进程存活期间，理论上足以重建整棵树。**缺的是读取它的代码。**
要跨进程、跨崩溃则还缺一样：这些文件都不 `fsync`，启动时又整目录清空
（[第 15 篇 §5](15-state-and-concurrency.md#5-原子提交与持久性)），落盘的完整性本身不在承诺之内。

这是取舍而非疏忽：让账本能跨重启存活，就要处理一整类新问题 ——

- 陈旧条目的回收（一台机器上积累了多少个已消失沙箱的目录？）；
- 与已消失沙箱的对账（沙箱没了但目录还在，谁负责清？）；
- 格式版本迁移（`manifest.json` 的结构变了怎么办？）。

而这些问题**只在「checkpoint 能脱离沙箱」的前提下才有意义**。前提不成立，
问题就不存在 —— 这正是[第 15 篇 §1](15-state-and-concurrency.md#1-三类状态)里说的
「并发模型少了一整类问题」。

### 3.4 缺运行时环境

Firecracker 进程、KVM fd、eventfd、irqfd / ioeventfd 注册、tap、网络槽位、NBD 设备 ——
全是活体资源。

**原地回滚的全部价值就在于一个都不重建**（[第 11 篇 §1](11-in-place-rollback.md#1-两种恢复形态)）。
这正是它比「新建进程加载快照」快一个量级的原因，也正是它离不开沙箱的原因。
**同一件事的两面。**

---

## 4. 内存那一半其实已经自足

值得单独说明，因为它决定了「补齐」的难度。

`CHECKPOINT_FULL_ROOT` 默认开启时，每个沙箱的**第一个 checkpoint 是全量捕获**。
内容解析沿祖先链回溯，遇到全量条目即终止 —— 不会回落到模板 memfile
（[第 8 篇 §6.2](08-memory-diff-tree.md#62-为什么必然终止)）。

这个改造的初衷是消除回滚路径上的跨网络依赖（模板 memfile 本地不存在，
要经 chunker 从对象存储拉取），副作用是：

> **内存差分树在数据上已经是自包含的。**

所以四层缺口里，内存这一层已经不缺了。**真正的数据缺口只有磁盘那一半。**

> 若把 `CHECKPOINT_FULL_ROOT` 关掉，这条不再成立 —— 树根变成相对模板 memfile 的差分，
> 内存半边也会失去自足性。

---

## 5. 补齐需要什么

| 缺口 | 补法 | 代价 |
|---|---|---|
| 内存全量镜像 | **已具备**。回滚数据的解析器稍加改造，即可物化出任意一代的完整内存 | 小 |
| 磁盘完整镜像 | 层栈压平，把模板底座物化进产物 | 中；产物体积回到全量 |
| 账本 | `NewStore` 不再清空根目录，增加一条从 `manifest.json` / `index.json` 扫描重建的路径 | 小；但引入陈旧条目回收与格式迁移问题 |
| 落盘 | 把产物与 manifest 的 `fsync` 按事务顺序（数据先于账本）加回来（[第 15 篇 §5](15-state-and-concurrency.md#5-原子提交与持久性)） | 并发 checkpoint 的延迟回升：ext4 日志提交是全文件系统的串行点 |
| 恢复路径 | 一条 load-from-files：新起 Firecracker 进程 + 加载 snapfile + 挂载内存与磁盘 | 中 |

### 5.1 补齐之后得到的是什么

全量产物、新建进程、跨节点可用、不依赖原沙箱 —— **这就是 e2b 原生 snapshot 的定义**。

也就是说，沿着「让 checkpoint 能脱离沙箱」这条路走到底，终点是一个**已经存在**的东西。
而沿途会逐项丢掉这套方案的收益：

| 补齐动作 | 丢掉的收益 |
|---|---|
| 磁盘层栈压平 | 零拷贝封存 —— 封存重新变成一次全量导出（[第 10 篇](10-disk-layering.md)） |
| 产物自足 | 「成本只与改动量有关」不再成立，每代都要付全量（[第 8 篇](08-memory-diff-tree.md)） |
| 从文件重建 | 原地回滚的低延迟 —— 重新回到进程重建 + 工作集换页（[第 11 篇](11-in-place-rollback.md)） |

---

## 6. 这是取舍，不是缺陷

用一句话概括这笔交易：

> **用「绑定沙箱生命周期」换掉产物自足性与账本持久化，换来零拷贝封存、原地回滚、
> 以及只随回退跨度增长的成本曲线。**

两条路径解决的是不同的问题，并且**并存** —— 本方案没有改动原生路径：

| | 原生 snapshot / resume | checkpoint / restore |
|---|---|---|
| 产物 | 自足，进对象存储 | 相对模板的增量，驻留宿主本地 |
| 生存期 | 独立于沙箱 | 随沙箱回收 |
| 恢复 | 新建进程加载 | 活体原地回滚 |
| 适用 | 迁移、持久化、崩溃恢复 | 高频快速回退 |

完整对比与配合方式见[第 20 篇](20-vs-native.md)。

---

## 7. 对使用方意味着什么

### 7.1 要长期保存怎么办

用原生 snapshot。典型配合方式：

- **进行中的任务**用 checkpoint —— 每完成一个阶段打一个点，出错就退回去重试，沙箱不中断；
- **要跨会话保留的状态**用原生 snapshot —— 产物进对象存储，之后可在任意节点恢复。

两者可以叠加：先用 checkpoint 把当前状态回退到某个已知良好的点，
再对该状态打一次原生 snapshot 落盘。

### 7.2 不要依赖的用法

| 别做 | 为什么 |
|---|---|
| 通过备份 `/orchestrator/build/checkpoints` 来「保存」checkpoint | 见[§2](#2-一个思想实验把目录完整拷走) |
| 手工删除 store 里的目录 | 会打断后代条目的解析链；删除走 API（[第 14 篇 §7](14-failure-semantics.md#7-删除依赖感知的回收)） |
| 假设 checkpoint id 跨沙箱有意义 | 它只在所属沙箱的生命周期内有效 |
| 指望 checkpoint 能扛住 orchestrator 重启 | 重启会带走所有沙箱 |

### 7.3 一个正面的推论

因为产物随沙箱回收，**不需要任何配额、清理任务或过期策略**。
沙箱一走，它占的盘就全回来了。运维上少一整类问题。

---

## 8. 小结

1. checkpoint 产物随沙箱生命周期回收，`OnRemove` → `RemoveSandbox` 直接
   `os.RemoveAll` 整个目录。
2. 因果顺序不是「因为恢复不了所以删」—— **即使完整保存下来也恢复不了**。
3. 缺的是四层：**活体虚机、自足的磁盘产物、可加载的账本、运行时环境**。
   其中账本那层**缺的是代码不是数据**。
4. 内存那一半因为全量树根，**在数据上已经自足**。真正的数据缺口只有磁盘。
5. 补齐这四层之后得到的东西，**基本就是 e2b 原生 snapshot 本身** ——
   而且沿途会逐项丢掉这套方案的全部收益。
6. 这是取舍：用「绑定沙箱生命周期」换零拷贝封存、原地回滚、与改动量挂钩的成本曲线。
7. 一个正面副产品：**不需要配额、清理任务或过期策略**。
8. **原生 pause 让该沙箱的全部 checkpoint 作废，resume 之后是新的一代、可以重新 checkpoint / restore**
   （[§1.4](#14-原生-pause--resume-与-checkpoint-的代际边界)）。账本按**代**（`LifecycleID`）记账而不是按
   沙箱 id 记账 —— 原地回滚要求活体，而 resume 起来的是另一个 Firecracker 进程。

---

## 代码位置

| 本篇提到的行为 | 位置 |
|---|---|
| 沙箱移除时删除整个 store 目录 | `internal/checkpoint/service.go` — `OnRemove`；`store.go` — `RemoveSandbox` |
| `NewStore` 不从磁盘加载账本 | `internal/checkpoint/store.go` — `NewStore` 及包注释 |
| manifest / index 的写入内容 | 同上 — `writeManifest`、`writeIndexLocked` |
| restore 第一步取沙箱 | `internal/checkpoint/service.go` — `ServeCheckpoint` |
| 唯一的恢复路径 | `internal/sandbox/checkpoint.go` — `RollbackInPlace` |
| 磁盘视图从模板底座起步 | 同上 — `assembleView` |
| 回滚集与内容解析 | `internal/checkpoint/store.go` — `MaterializeRevert` |
| 暂停态检查与拓扑校验 | `src/vmm/src/rollback.rs` — `rollback_snapshot`、`validate_topology` |
| 全量树根开关 | `internal/checkpoint/service.go` — `fullRootEnabled` |

**下一篇**：[17 · 可观测性与验证](17-observability-and-verification.md) ——
这套系统最危险的故障模式是「看起来正常」，那一篇讲怎么让它们现形。
