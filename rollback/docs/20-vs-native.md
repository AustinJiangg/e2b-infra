# 20 · 与原生 snapshot 的对比与配合

> 两条路径**并存**，解决不同的问题。本篇做完整对照：原生那两个 RPC 到底做了什么、
> 成本模型差在哪一项、优化点各自的对照基准是什么，以及在一个真实工作流里怎么一起用。
>
> **读者**：所有读者。使用方可以只读 [§5](#5-两者如何配合)。
> **预备**：[第 4 篇 · e2b 原生 snapshot](04-e2b-native-snapshot.md)。
> 若已读完第二部分，本篇是一次收束；若只想做选型，可以直接从这里开始。
> **代码**：`internal/server/sandboxes.go`、`internal/sandbox/rootfs/nbd.go`

---

## 0. 本篇要回答的问题

1. 原生的 `Pause` 和 `Checkpoint` 两个 RPC 分别做了什么？
2. 「snapshot 之后沙箱 id 不变」，那底下还是同一个沙箱吗？
3. 两条路径的成本模型差在哪一项？什么时候该用哪个？
4. 所谓「优化点」，各自是相对谁而言的？
5. 一个真实的 Agent 工作流里，两者怎么配合？

---

## 1. 原生的两个 RPC

### 1.1 `Pause`：快照 + 停

```go
func (s *Server) Pause(ctx, in *orchestrator.SandboxPauseRequest) (*emptypb.Empty, error) {
    sbx, err := s.acquireSandboxForSnapshot(ctx, in.GetSandboxId())
    ...
    defer s.stopSandboxAsync(context.WithoutCancel(ctx), sbx)   // ← 停掉
    _, _, err = s.snapshotAndCacheSandbox(ctx, sbx, in.GetBuildId())
    ...
}
```

打完快照就把沙箱停了，产物进对象存储。**这是「沙箱离场」的语义。**

### 1.2 `Checkpoint`：快照 + 停 + 重新拉起

SDK 的 `create_snapshot()` 走这条。它比 `Pause` 多一步：

```go
defer s.stopSandboxAsync(context.WithoutCancel(ctx), sbx)         // ① 老沙箱照样要停
meta, waitForUpload, err := s.snapshotAndCacheSandbox(...)        // ② 打快照
template, err := s.templateCache.GetTemplate(ctx, in.GetBuildId(), true, false)
resumedSbx, err := s.sandboxFactory.ResumeSandbox(ctx, template, sbx.Config,
    sandbox.RuntimeMetadata{
        TemplateID:  sbx.Runtime.TemplateID,
        SandboxID:   sbx.Runtime.SandboxID,       // ← 不变
        ExecutionID: sbx.Runtime.ExecutionID,     // ← 不变
        TeamID:      sbx.Runtime.TeamID,
    }, ...)                                                       // ③ 从新快照重新拉起
```

**所以：sandbox id 不变，但底下已经是一个全新的 Firecracker 进程。**

### 1.3 三个 id

| id | 语义 | `Checkpoint` 之后 |
|---|---|---|
| `SandboxID` | 对外的沙箱标识 | **不变** |
| `ExecutionID` | 一次「执行」的标识，给 API、路由目录、分析用 | **不变** |
| `LifecycleID` | 一个 Firecracker 进程实例的标识 | **换新** |

`LifecycleID` 换新的理由写在注释里：

> so the old sandbox's cleanup goroutine (`RemoveByLifecycleID`) won't
> accidentally evict the resumed sandbox from the map.

老沙箱的清理协程会用它自己的 `LifecycleID` 去删表项。如果新沙箱沿用同一个 id，
老沙箱的清理就会把**新**沙箱从表里删掉。

> 这个细节回答了一个常被问的问题：**「snapshot 是 pause + resume 吗？沙箱 id 一致吗？」**
> —— id 一致，进程不一致。对调用方是透明的，但代价完全不同：
> 新进程要重新建 KVM VM、vCPU、GIC、设备、tap、UFFD，
> 然后靠缺页把整个工作集换回内存。

---

## 2. 逐项对比

| 维度 | e2b 原生 | 本方案 |
|---|---|---|
| **打快照时沙箱** | 停掉，再从新快照重新拉起一个新进程 | **不停**，同一个进程 |
| 快照后的身份 | SandboxID / ExecutionID 不变，LifecycleID 换新 | 无进程更替 |
| 内存搬运 | orchestrator 从 Firecracker 进程地址空间逐页拷出 | Firecracker 自己写稀疏差分，位图同一次调用落地 |
| 内存判据 | uffd 写保护（ARM 适配版上已退化） | KVM / HDBSS 脏页日志 |
| 磁盘增量 | 停沙箱后把写层**紧凑导出**成 diff | 写层**就地封存**成只读层，零拷贝 |
| 磁盘增量的落盘 | 同步导出 | 不等待落盘（[第 10 篇 §5](10-disk-layering.md#5-封存不等待落盘)） |
| 历史结构 | 线性链，回滚后旧快照失去意义 | **树**，可前滚、可跨分支 |
| 恢复方式 | 新建进程 + UFFD，靠缺页把工作集换回 | **原地写回**差异页，宿主资源全保留 |
| 恢复成本 | **与虚机规格挂钩** | **与回退跨度挂钩** |
| 产物 | 自足，进对象存储 | 相对模板的增量，宿主本地 |
| 生存期 | 独立于沙箱 | 随沙箱回收 |
| 跨节点 | 可以 | 不可以 |
| 崩溃恢复 | 可以 | 不可以 |
| 典型频次 | 一个沙箱一两次 | 一个任务里十几次 |

配图见 [`../diagrams/03-vs-native.svg`](../diagrams/03-vs-native.svg)。

---

## 3. 成本模型

设 guest 内存 M、本次改动量 D、回退跨度 R。

| 操作 | 原生 | 本方案 |
|---|---|---|
| 打快照 · guest 停顿 | **整个操作** —— 停掉、导出、重建、换页 | O(D) 冻结窗口 |
| 打快照 · 内存 | O(D)（x86）/ **O(工作集)**（ARM 适配版） | O(D) |
| 打快照 · 磁盘 | O(脏块) 紧凑导出，**必须停沙箱** | **O(1)** 改名 |
| 恢复 · 进程与设备 | 全部重建 | **零** |
| 恢复 · 内存 | **O(工作集)** 缺页换入 | O(R) 写回 |
| 恢复 · 磁盘 | 重建 Overlay | O(层数) 次 `NewCache`，无数据搬运 |

### 3.1 交叉点在哪

> **推论**（下面是基于成本模型的判断，不是实测结论）：

原生方案的代价基本是常数 —— 与虚机规格挂钩，与改动量关系不大。
本方案的代价随 D 和 R 线性增长。所以理论上存在一个交叉点：**当 R 接近整个工作集时，
两者的恢复代价趋同**。

但即使在那个点上，本方案仍有两处不可替代的优势：

1. **沙箱不中断** —— IP、端口映射、外部持有的引用全部保留；
2. **没有进程重建的固定开销** —— 建 VM、建 vCPU、建 GIC、建设备、建 tap、挂 UFFD。

所以交叉点不是「该切换到原生」的信号，而是「这个回退跨度太大，
也许该考虑重建沙箱」的信号 —— 那是另一个决策。

### 3.2 一个反直觉的现象

在 ARM 适配版上，**即使一次 snapshot 之间什么都没做，增量也不接近零**。

两件事叠加（[第 7 篇 §5.3](07-dirty-page-tracking.md#53-与改动量无关的下限)）：

1. `Checkpoint` RPC 会**重建沙箱**，新进程的工作集要重新缺页换入；
2. **换入即被判脏**（ARM 适配版的判据退化）。

所以下一次增量的下限 ≈ 整个常驻工作集。

**这个下限是 ARM 适配版特有的，x86 上不成立** —— 那里换入的是干净页，不计入增量。

---

## 4. 优化点汇总

每一条都标出**对照基准**，这是本表最重要的一列：

| 层次 | 优化点 | 带来什么 | 对照基准 |
|---|---|---|---|
| 脏页跟踪 | HDBSS 硬件标脏自动接管，启动探测能力，无硬件安全退回 | 由 CPU 记录脏页，950 上开箱即用 | **新增能力** |
| | 判据取自硬件 / 内核日志，绕开 uffd | 补回精确增量 | **ARM 适配版** |
| 内存产物 | 差分树：每代只存本代脏页，不复制上一代 | 成本只与改动量有关，ext4 上不需要 reflink | **内部路线修正** |
| | 位图随快照同一次调用落地 | 省一轮接口往返和一次全内存扫描 | 原生 |
| | 树形历史而非线性链 | 回滚后可再前滚、可跨分支跳，历史不丢 | 原生 |
| | 树根存一次全量 | 整棵树自给自足，回滚路径不跨网络 | **内部路线修正** |
| 恢复路径 | 按页沿祖先链解析，不合并链、不重建完整镜像 | 恢复成本与历史链长度无关 | 内部设计 |
| | 进程内原地回滚 | 宿主资源全部保留，恢复后无需重新换入工作集 | 原生 |
| | 回滚集精确到页 | 代价与回退跨度成正比 | 原生 |
| 磁盘 | 写层就地封存 + 视图活体切换 | 打快照与回滚都不停沙箱 | 原生 |
| | 封存不等待落盘 | 封存耗时不再随改动量增长 | **内部优化** |
| 一致性 | 内存与磁盘在同一次暂停窗口内完成 | 两者是同一瞬间的镜像 | 原生 |
| 架构 | 接口在宿主侧接管 | 沙箱内不需要任何代理程序 | **新增能力** |
| 工程保障 | 失败语义分级、断链拒绝恢复、删除不打断后代 | 杜绝「看起来成功、实际数据已坏」 | **新增能力** |
| | 三层计时 + 全量 / 增量模式回报 | 能定位到具体阶段，能发现静默退化 | **新增能力** |

**「对照基准」这一列不能省。** 少了它，整张表会被读成「我们比 e2b 强 14 项」，
而实际上其中：

- 4 项是**新增能力**（原生根本没有这个场景）；
- 2 项是**内部路线修正**（我们自己早期设计的问题）；
- 1 项是**补回 ARM 适配丢掉的东西**（x86 原生本来就有）；
- 其余才是相对原生路径的改进，而且是**在特定场景下**的改进。

---

## 5. 两者如何配合

### 5.1 分工

| 需求 | 用哪个 |
|---|---|
| 任务执行中的快速回退 | **checkpoint / restore** |
| 沙箱要离场，之后还想回来 | **原生 snapshot** |
| 跨节点迁移 | **原生 snapshot** |
| 进程崩溃后恢复 | **原生 snapshot** |
| 跨会话保存状态 | **原生 snapshot** |
| 长期持久化 | **原生 snapshot** |

一句话：**「沙箱活着时的回退」用 checkpoint，「沙箱离场」用 snapshot。**

### 5.2 叠加使用

两者不冲突，可以串起来用：

```
用 checkpoint 把状态回退到某个已知良好的点
        ↓
对这个状态打一次原生 snapshot
        ↓
产物进对象存储，之后可在任意节点恢复
```

这解决了「checkpoint 不能长期保存」这个限制
（[第 16 篇](16-lifecycle-and-portability.md)）：**先用 checkpoint 精确定位到想要的时刻，
再用 snapshot 把那个时刻固化下来。**

### 5.3 一个 Agent 工作流

```
① 沙箱起来（由原生快照加载）
② checkpoint「干净环境」                    ← 全量根，~0.28 s
③ 装依赖
④ checkpoint「依赖装好」                    ← 增量，~0.11 s
⑤ 改配置 → 跑构建 → 失败
⑥ restore 到「依赖装好」                    ← ~0.10 s，沙箱不中断
⑦ 换个改法 → 跑构建 → 成功
⑧ checkpoint「构建通过」                    ← 从④分叉出的新分支
⑨ 跑测试 → 失败 → restore 到「构建通过」
⑩ ……
⑪ 任务完成，对最终状态打一次原生 snapshot   ← 跨会话保留
⑫ 沙箱销毁，②④⑧ 的 checkpoint 随之消失，⑪ 的 snapshot 留下
```

几个要点：

- **⑥ 之后 ⑤ 那条分支还在**。如果后来发现原来那个改法其实是对的，可以再前滚回去
  （[第 8 篇 §3](08-memory-diff-tree.md#3-树而不是链)）。
- **⑨ 不需要重新装依赖**。回退到「构建通过」是几十毫秒的事。
- **⑫ 是设计使然，不是缺陷**。checkpoint 的定位就是任务执行期间的工作台，
  跨会话的东西交给 snapshot。

---

## 6. 三个常见误解

### 6.1 「本方案比 e2b 原生快」

**要看比什么。** 在「活沙箱内的快速回退」这个场景下，是的，而且差一个数量级 ——
因为原生根本不是为这个场景设计的。

在「沙箱离场后再回来」这个场景下，本方案**做不到**
（[第 16 篇](16-lifecycle-and-portability.md)）。

### 6.2 「原生 snapshot 是 pause + resume，沙箱没变」

**id 没变，进程变了。** `Checkpoint` RPC 会停掉旧 Firecracker、从新快照拉起一个新的，
`SandboxID` / `ExecutionID` 保持不变以维持对外身份，`LifecycleID` 换新
（[§1.3](#13-三个-id)）。

这解释了为什么原生方案的成本与虚机规格挂钩：**每次都在重建**。

### 6.3 「e2b 的增量快照有一个与改动量无关的下限」

**这只在 ARM 适配版上成立**（[§3.2](#32-一个反直觉的现象)）。
x86 上 e2b 的增量是精确的。把这条说成「e2b 的设计问题」是不准确的。

---

## 7. 小结

1. 原生 `Pause` = 快照 + 停；`Checkpoint` = 快照 + 停 + **从新快照重新拉起**。
   `SandboxID` / `ExecutionID` 不变，`LifecycleID` 换新 —— **底下是新进程**。
2. 成本模型的分野：原生**与虚机规格挂钩**，本方案**与改动量 / 回退跨度挂钩**。
3. 优化点表必须带**对照基准**列：14 条里 4 条是新增能力、2 条是内部路线修正、
   1 条是补回 ARM 适配的退化。
4. 分工一句话：**沙箱活着时的回退用 checkpoint，沙箱离场用 snapshot。**
5. 两者可**叠加**：先用 checkpoint 精确回退到想要的时刻，再用 snapshot 把它固化下来。
6. 「与改动量无关的下限」是 ARM 适配版特有的，不是 e2b 的设计问题。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| `Pause` RPC | `internal/server/sandboxes.go` — `Server.Pause` |
| `Checkpoint` RPC | 同上 — `Server.Checkpoint` |
| 三个 id 与生命周期 | 同上 — `ResumeSandbox`、`RemoveByLifecycleID`、`setupSandboxLifecycle` |
| 原生磁盘增量导出 | `internal/sandbox/rootfs/nbd.go` — `ExportDiff`；`block/cache.go` — `ExportToDiff` |
| 原生 diff 元数据 | `packages/shared/pkg/storage/header` — `DiffMetadataBuilder` |
| 本方案的对应实现 | 见[第 8](08-memory-diff-tree.md)、[10](10-disk-layering.md)、[11 篇](11-in-place-rollback.md) |

**下一部分**：[21 · 在这套方案上继续开发](21-extending.md)。
