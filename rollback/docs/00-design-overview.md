# 00 · 设计总览

> 一篇读完全貌。这套 checkpoint / restore 解决什么问题、由什么组成、凭什么快、边界在哪、现在做到哪一步。
>
> **读者**：所有人。想深入的按[第 12 节](#12-接下来读什么)继续。
> **预备**：无。涉及的背景知识都在文内交代。
> **范围**：**ext4 方案**（鲲鹏 950 上的主线）。另有一条 XFS 方案，差异见[第 9 节](#9-两套方案ext4-与-xfs)。

---

## 0. 本篇要回答的问题

1. 沙箱已经有快照能力了，为什么还要再做一套？
2. 它由哪些部件组成？一次快照、一次回滚分别发生了什么？
3. 它凭什么快 —— 成本模型和原有方案差在哪一步？
4. 它的边界在哪里，什么场景不该指望它？
5. 现在验证到哪一步，还差什么？

---

## 1. 场景：沙箱执行到一半，需要退回去

一个 AI Agent 在沙箱里执行多步任务：装依赖、改配置、跑构建、跑测试。第 7 步把环境搞坏了 ——
装错了版本、删错了文件、改崩了配置。它需要回到第 6 步结束时的状态，换个做法重试。

这个需求有三个特征，它们决定了后面所有的技术选择：

- **高频**。不是一天几次，是一个任务里十几次。单次成本必须低到可以随手用。
- **低延迟**。回退发生在 Agent 的决策循环里，几百毫秒和几秒是完全不同的产品体验。
  客户给的量化上限是 **checkpoint ≤ 200 ms、restore ≤ 100 ms**
  —— 它只约束高频路径上的增量 checkpoint 与原地 restore，完整口径见
  [第 23 篇 §1.2](23-performance-methodology.md#12-口径定义表)。
- **沙箱不能中断**。沙箱有 IP、有端口映射、有正在保持的连接、有外部持有的引用。
  中断一次，这些全要重建。

e2b 自带的 snapshot / resume 面向的是另一件事：**沙箱离场后再回来**。两者的定位差别：

| | e2b 原生 snapshot / resume | 本方案 checkpoint / restore |
|---|---|---|
| 场景 | 跨节点迁移、长期持久化、进程崩溃后恢复 | 活沙箱内部的快速回退 |
| 沙箱 | 打快照过程中被停掉再重新拉起 | 全程不中断 |
| 产物 | 进对象存储，跨节点可用 | 驻留宿主本地，随沙箱生命周期回收 |
| 成本 | 与虚机规格挂钩 | 与本次改动量挂钩 |
| 典型频次 | 一个沙箱一两次 | 一个任务里十几次 |

两条路径**并存**。本方案没有改动原生路径的任何一行代码 —— 沙箱进程死亡、虚机状态撕裂、
跨节点迁移这些场景不在本方案的承诺范围内，仍由原生 snapshot 负责。

### 1.1 为什么不直接复用原生 snapshot

三条理由，每一条都是硬的：

1. **它会中断沙箱**。导出磁盘增量的路径必须先摘出写层、停掉沙箱、等 NBD 设备释放。
   高频场景下不可接受。
2. **成本模型不对**。恢复要新建 Firecracker 进程，再靠缺页把整个工作集换回内存 ——
   代价取决于**虚机多大**，而不是**回退了多少**。回退一个只改了 40 MiB 的沙箱，
   和回退一个改了 2 GiB 的沙箱，花的时间几乎一样。
3. **ARM 适配版上增量已经退化**。上游 e2b 在 x86 上用 userfaultfd 的写保护位区分读缺页与写缺页，
   增量是精确的；移植到 aarch64 时那条写保护路径走不通，被注释掉了，判据退化成
   「凡是被换入过的页都算脏」。详见[第 6.2 节](#62-脏页判据的三方差异)。

---

## 2. 总体结构

```mermaid
flowchart TB
    SDK["Python SDK<br/>create / restore / list / delete&nbsp;&nbsp;→&nbsp;&nbsp;sandbox:49984"]

    subgraph HOST["宿主 orchestrator 进程"]
        direction LR
        PX["Proxy 拦截<br/>端口 49984 不转发进沙箱<br/>宿主直接应答 + 校验 token"]
        SVC["Checkpoint Service<br/>按沙箱串行 · 决定全量 / 增量<br/>失败分级 · 分阶段计时"]
        LED["内存差分树账本<br/>父指针 · 纪元位图 · 最近公共祖先"]
        RLE["磁盘层账本<br/>合并 header · 层清单"]
        PX --> SVC
        SVC --> LED
        SVC --> RLE
    end

    subgraph FCP["Firecracker 进程（沙箱）—— 全程不重建"]
        direction LR
        VC["vCPU · GIC · virtio 设备<br/>逻辑状态写回活对象"]
        MEM["Guest Memory<br/>差分页直接写回活映射"]
        DSK["磁盘视图 Overlay（live NBD 之下）<br/>模板底座 + 封存层 × k + 活写层"]
    end

    subgraph HW["KVM · 鲲鹏 950"]
        HD["HDBSS 硬件标脏（KVM cap 502）<br/>启动时探测，无硬件安全退回软件写保护"]
    end

    subgraph STO["宿主本地存储"]
        direction LR
        F1["snapfile<br/>vCPU 与设备状态"]
        F2["mem_diff<br/>稀疏，仅本代脏页"]
        F3["mem_bitmap<br/>本代纪元位图"]
        F4["rootfs.header + layers/<br/>封存写层，多代共享"]
    end

    SDK --> PX
    SVC -- "create：暂停 → 写差分 ‖ 封存层 → 恢复" --> FCP
    FCP -- "restore：暂停 → 算回滚集 → 原地写回 → 切视图 → 恢复" --> SVC
    HD -. "脏页日志" .-> MEM
    MEM --> F2
    MEM --> F3
    VC --> F1
    DSK --> F4

    classDef host fill:#f4f7fb,stroke:#b9c8db
    classDef fc fill:#fff8f0,stroke:#e0b98a
    classDef hw fill:#f2f0fb,stroke:#b8aede
    classDef sto fill:#f2faf3,stroke:#a8ceac
    class PX,SVC,LED,RLE host
    class VC,MEM,DSK fc
    class HD hw
    class F1,F2,F3,F4 sto
```

四个层次，职责边界清晰：SDK 发请求 → orchestrator 编排并持有账本 →
分叉 Firecracker 执行内存与设备状态的读写 → KVM / 硬件提供脏页信息。

### 2.1 控制面：请求发往沙箱，却由宿主应答

SDK 把 checkpoint 请求发给沙箱的 **49984** 端口，但 orchestrator 的反向代理在转发之前把它截下来，
由宿主侧的 Checkpoint Service 直接回答。

理由很直接：**打快照要暂停虚机，沙箱内的程序没法暂停自己**。

截获点位于代理自身的 token 校验之下，所以 Service 自己又做了一遍 `e2b-traffic-access-token` 校验 ——
这是安全上的必要重复，不是冗余。

这个设计的收益：沙箱内**不需要安装任何代理程序**。对比基于 CRIU 的 guest 侧实现，
省掉了一整个 guest agent，也省掉了它带来的版本管理、权限、以及「agent 自己被 checkpoint」的问题。

### 2.2 存储布局

单机部署下 `<store-root>` 是 `/orchestrator/build/checkpoints`：

```
<store-root>/<sandbox-id>/
├── <checkpoint-id>/
│   ├── snapfile          # Firecracker 的 vmstate：vCPU、GIC、设备状态
│   ├── mem_diff          # 稀疏文件，只含本代写脏的页，写在各自的 guest 物理偏移上
│   ├── mem_bitmap        # FCDB 侧车：本代纪元位图
│   ├── rootfs.header     # 该时刻磁盘视图的合并映射表
│   ├── manifest.json     # 该 checkpoint 的完整条目记录
│   └── timings.json      # 宿主侧分阶段耗时
├── layers/
│   ├── layer-<uuid>      # 封存写层，可被多代 checkpoint 共享引用
│   └── layer-<uuid>.meta # 该层持有哪些块偏移
├── index.json
└── last-restore-timings.json
```

产物**不进对象存储**，随沙箱销毁一并删除。磁盘上的索引是为了事后可查，不用于跨进程重启存活 ——
orchestrator 重启本来就会带走它上面的所有沙箱。这个取舍的完整推论见[第 10 节](#10-边界它不做什么)。

---

## 3. 内存产物：差分树

```mermaid
flowchart TD
    CK1["<b>ck1</b><br/>全量捕获（树根）<br/>整棵树自给自足，回滚不回头依赖模板"]
    CK2["<b>ck2</b><br/>增量：仅本代脏页"]
    CK3["<b>ck3</b><br/>增量：仅本代脏页"]
    CK4["<b>ck4</b><br/>增量：仅本代脏页"]
    CK5["<b>ck5</b><br/>增量：仅本代脏页<br/>当前基准"]

    CK1 --> CK2 --> CK3
    CK1 --> CK4 --> CK5

    CK3 -. "曾回滚到 ck1，旁支 ck2 / ck3 保留" .-> CK1
    CK5 == "本次：ck5 → ck3，跨分支回滚，经最近公共祖先 ck1" ==> CK3

    classDef root fill:#e8f0fe,stroke:#4285f4,stroke-width:2px
    classDef base fill:#fef7e0,stroke:#f9ab00,stroke-width:2px
    classDef inc  fill:#ffffff,stroke:#9fb4cc
    class CK1 root
    class CK5 base
    class CK2,CK3,CK4 inc
```

### 3.1 每代只存自己写脏的页

一次增量 checkpoint 的内存产物就是一个稀疏文件：Firecracker 按 Diff 模式把本代脏页写在各自的
guest 物理偏移上，中间的干净页是**文件空洞**（不占物理块）。成本 **O(本代脏页)**，
与虚机规格无关，与文件系统无关。

### 3.2 为什么不做整代克隆

一个自然的替代设计是让每代产物都是一份**完整**内存镜像：先克隆上一代镜像，
再让 Firecracker 把 Diff 写在克隆上。这样每代自足，恢复时直接加载即可。

在 XFS 上这可行 —— `FICLONE` 是元数据操作，物理上只落脏页。
但 **ext4 没有 reflink**，克隆退化成真实字节拷贝，于是代价与改动量彻底脱钩：
即使一代之内什么都没做，也要付一次全内存拷贝。

950 与客户环境全部是 ext4，所以整条数据路径不得依赖 reflink。克隆这一步被取消，
代之以「差分 + 按页解析」。这不是「比克隆更好」，是**在没有 reflink 的文件系统上，克隆不成立**。

### 3.3 树，而不是链

每个 checkpoint 记录一个 `ParentID`。回滚**不剪枝**：比目标更新的 checkpoint 保留成旁支，
仍可恢复；回滚之后新打的 checkpoint 以回滚目标为父，于是树长出新分支。

由此支持三种线性链做不到的操作：

- 回滚之后再**前滚**回去；
- **跨分支**跳转（经最近公共祖先）；
- 删除中间节点而**不打断后代**（被后代依赖的节点转为隐藏保留）。

### 3.4 回滚集：树路径并集 ∪ 活跃脏页

从当前基准 `b` 回滚到目标 `t`，需要写回的页集合是：

```
revert = ⋃ 纪元位图(x)   x ∈ path(b → LCA) ∪ path(t → LCA)   （不含 LCA）
       ∪ Firecracker 当前的活跃脏页
```

**正确性**：若页 p 不在上式中，说明 p 在分叉之后的两侧都没有被写过，
那么它在 `b` 时刻的内容 == 在 LCA 时刻的内容 == 在 `t` 时刻的内容，无需回写。

活跃脏页那一项是必需的：虚机自上次快照以来写过的页尚未进入任何一代产物，
不并进来就会漏回滚。它通过分叉 Firecracker 新增的 `PUT /snapshot/save-dirty-bitmap` 导出。

路径上任何一代缺少侧车文件，该次回滚**直接报错**，而不是静默降级。

> 完整推导、边界情形与代码对应见[第 8 篇](08-memory-diff-tree.md)。

### 3.5 内容解析：沿祖先链找最近的一代

对回滚集中的每一页 p，沿目标的祖先链 `t → parent(t) → …` 找**第一个**位图含 p 的那一代，
从它的 `mem_diff` 读取。树根是全量捕获，对解析而言等价于全 1 位图，因此解析必然在树内终止。

解析结果写成一个稀疏文件交给 Firecracker，连续同源的页合成单次读写（单次最多 1024 页）。
Firecracker 拿到后一次性写回活着的虚机内存。

### 3.6 树根为什么要全量

改造前树根是「相对沙箱启动内存源的差分」，于是凡是从没被任何一代写过的页，
都要回落到模板 memfile 读取 —— 而模板 memfile **本地并不存在**，
它是经 chunker 从对象存储按需拉取的。也就是说回滚路径上藏着一段**跨网络依赖**。

改为树根全量捕获后，整棵树自给自足，回滚只读本地文件。代价只落在每个沙箱的**第一次** checkpoint
（一次全内存写 + 一份全内存大小的存储），第 2..n 次不受影响。

开关 `CHECKPOINT_FULL_ROOT`，默认开。

### 3.7 恢复成本与链深无关

直觉上「沿祖先链解析」是这套方案最可能被长链拖垮的地方。实测（5 / 20 / 50 代）没有趋势。原因：

- 沿祖先链找「这一页最近写在哪一代」，只在**已加载到内存的位图**上做位测试。
  50 层查找相对一次 `pread` 可以忽略；真正读文件只发生在命中的那一次。
- 创建侧也与链深无关，因为不做克隆。

所以**不需要周期性压平历史或做全量重整**。

---

## 4. 磁盘产物：分层封存

### 4.1 原生做法与它的约束

沙箱的磁盘是一个 `Overlay`：底下是只读的模板 rootfs，上面是一层 COW 写层（一个稀疏文件 + mmap）。
guest 的写经 NBD dispatcher 落进写层，并把块偏移记入 dirty 集合。

原生导出增量的路径是 `ExportDiff`：

1. `EjectCache()` 把写层摘出来，Overlay 就此作废；
2. **停掉沙箱**，等 NBD 设备释放；
3. 逐块从 mmap 取出脏块，全零块只记 empty 位不写，非零块**紧凑追加**进输出流；
4. 关闭并删除写层文件。

产物是紧凑的 diff 文件，storage offset ≠ device offset，靠映射表定位。
它必须停沙箱 —— 这对高频 checkpoint 不可接受。

### 4.2 Seal：零拷贝换层

本方案在同一个 `Overlay` 上新增一条路径 `SealLayer`，它**不搬运任何数据**：

1. 对 NBD 块设备发一次 `BLKFLSBUF` ioctl —— 刷屏障，把内核块层已接受的写推进写层；
2. 新建一个空写层（open + truncate 出稀疏文件 + mmap，零数据写入）；
3. `Overlay.Seal()`：两次指针替换 —— 旧写层包装成 `SealedView` 折进读路径，新写层上位；
4. `MoveFile()` 把旧写层文件改名进 store（同一文件系统内是 rename，inode 不变，
   live mapping 继续有效）。

guest 写进去的那个文件自始至终是同一个文件，只是身份从「写层」变成「只读层」。
类比：**OverlayFS 把 upper 降级成 lower，再开一个新 upper，挂载全程不卸载。**

因为不做紧凑化，层文件里 storage offset == device offset，映射表是恒等映射，比原生少一步偏移换算。
代价是不剔除全零块，层文件按块粒度占盘，比紧凑 diff 略费空间。

### 4.3 封存不等待落盘

`SealLayer` **刻意不做 msync**。封存层的脏页留在宿主 page cache，由内核按自己的节奏回写。

理由：后续所有读者 —— 还在运行的沙箱通过 `SealedView`、restore 时重新打开该文件 ——
都读同一份 page cache，**一致性不欠这个 flush**；msync 买到的是宿主崩溃后的持久性，
而 store 的索引本来就活在 orchestrator 的进程内存里、随进程一起消失，
checkpoint 的语义达不到那么远。

实测这一步的代价是 **0.49 ms / 脏 MB**，是封存耗时随改动量线性增长的唯一来源。
去掉后封存基本恒定。

### 4.4 ResetView：挂载不动，整体换视图

回滚时磁盘侧要换掉的是整个视图（读路径 + 写层）。`ResetView` 在 live NBD 挂载之下完成：
NBD dispatcher 手里的 Device 指针不变，指针背后换成目标 checkpoint 的层栈加一个全新写层。

被丢弃时间线的写层连同文件一起关闭删除；旧的封存层只解除映射，文件归 store 所有 ——
可能还有别的 checkpoint 在引用它。

### 4.5 恢复读路径不走层链

运行中的沙箱通过 `SealedView` 链读盘，链有多深就有多少层。但**恢复时不走这条链**：
`DiskViewForEntry` 用每层的 `.meta` 侧车重新打开层文件并标记它持有哪些块，
按 checkpoint 自己的合并 header 组装视图。

所以**层数永远不影响恢复**。

---

## 5. 一次 checkpoint、一次 restore

```mermaid
sequenceDiagram
    autonumber
    participant S as Python SDK
    participant O as orchestrator<br/>Checkpoint Service
    participant F as Firecracker<br/>（同一个进程，全程不重建）
    participant K as KVM / HDBSS
    participant D as 宿主本地存储

    Note over S,D: ① 打一个 checkpoint
    S->>O: create（端口 49984，被宿主拦截，不进沙箱）
    O->>O: 按沙箱串行；决定本次是全量还是增量
    rect rgb(234, 243, 255)
        Note over F: 冻结窗口开始
        O->>F: 暂停虚机
        K-->>F: 硬件 / 内核脏页日志
        O->>F: 写快照（Diff + 脏页位图，一次调用）
        F->>D: snapfile · mem_diff（稀疏）· mem_bitmap
        O->>F: 写层就地封存为只读层，挂上新的空写层
        F->>D: layer 文件（改名入库，不做拷贝、不等待落盘）
        O->>F: 恢复虚机
        Note over F: 冻结窗口结束
    end
    O->>O: 合并 header、提交树账本（父指针 = 上一代）
    O-->>S: checkpointId + 本次是全量还是增量

    Note over S,D: ② 回到某个 checkpoint
    S->>O: restore(checkpointId)
    rect rgb(234, 243, 255)
        Note over F: 冻结窗口开始
        O->>F: 暂停虚机
        O->>F: 导出当前活跃脏页位图
        O->>O: 回滚集 = 树路径各代位图并集 ∪ 活跃脏页
        D-->>O: 沿目标祖先链逐页解析目标时刻内容
        O->>F: 原地写回：内存差异页 → vCPU → 中断控制器 → 设备状态
        O->>F: 挂载不动，整体切换磁盘视图
        O->>O: 清理连接跟踪表（此刻不可能有流量重建它）
        O->>F: 恢复虚机
        Note over F: 冻结窗口结束
    end
    O->>F: 等待 guest 内 envd 应答
    O->>O: 基准切到该 checkpoint，树从这里长出新分支
    O-->>S: success
```

### 5.1 冻结窗口是唯一对业务可见的代价

内存与磁盘在**同一次暂停**内完成。这不只是省一次暂停：两者因此是**同一瞬间的镜像** ——
guest 尚未刷盘的数据留在内存镜像里，位置完全正确，不会出现半新半旧。

restore 侧有个对称的次序约束：**暂停之后**才导出活跃脏页位图、才物化回滚数据。
因为虚机停住后活跃脏页集不再增长，物化出来的内容必然覆盖 Firecracker 接下来要写回的全部页。

### 5.2 Firecracker 内部的原地回滚

分叉 Firecracker 的 `PUT /snapshot/rollback` 在 VMM 线程上、虚机暂停时执行：

| 阶段 | 做什么 |
|---|---|
| 1 校验 | 快照拓扑必须与运行中的虚机一致（同 vCPU 数、同内存大小、同设备集、逐设备的激活状态）；内存文件长度、位图几何都要对上 |
| 2 静默 | 排空在途设备 I/O：块设备等异步引擎结束，网络读掉并丢弃 tap 里缓存的帧 —— 那些帧属于正在被丢弃的时间线 |
| 3 取活跃脏图 | KVM 的读是破坏性的，所以先把结果折回用户态位图，无论后面发生什么都不会丢失「这些页脏过」这一信息 |
| **—— 提交点 ——** | 从这里开始改动 guest 状态 |
| 4 内存 | 按回滚集把页写回活映射，连续页合批 |
| 5 vCPU | 固定走 reinit 路线（KVM 定义的复位 + 完整寄存器恢复）|
| 6 中断控制器 | 对既有设备 fd 恢复 GIC 状态 |
| 7 设备 | virtio 队列、协商特性、中断状态写回活对象；串口重新初始化 |
| 8 VMGenID | 刷新代号，让 guest 知道时间被回拨；放在内存写回**之后**，避免被回滚覆盖 |
| 9 重置基线 | 清空脏页跟踪，使下一代 Diff 相对刚恢复的状态；队列页重新标脏（运行期队列写不被跟踪）|

宿主资源 —— 进程、KVM fd、eventfd、irqfd / ioeventfd 注册、tap、网络槽位、NBD 设备、内存映射 ——
**全部保留**。代价正比于回滚集大小，而不是虚机规格。

> vCPU 那一步曾有一条只恢复寄存器、不做 `KVM_ARM_VCPU_INIT` 的替代路线。它在压力测试的
> G1 门槛上输了（p95 145.3 ms vs reinit 的 48.3 ms），连同测量一起被删除。详见[第 11 篇](11-in-place-rollback.md)。

### 5.3 原地回滚特有的三个坑

进程重建路线天然不会遇到、原地回滚必须自己处理的：

- **网络 RX 描述符缓存**：队列索引被回退后，设备对象里还留着按旧时间线解析出的描述符链，
  而那些环页刚被回滚。第一个完成的帧会写入携带失效描述符 id 的 used 项，
  guest 驱动拒绝（`id N is not a head!`）后 RX 永久卡死。必须在应用设备状态后重建该缓存。
- **VMGenID**：必须在内存写回之后刷新，否则新代号会被回滚数据覆盖。
- **连接跟踪**：恢复后的 guest 忘记了内核仍在跟踪的连接，且 TCP 序号已经倒退。
  残留表项会让内核判定 guest 的包非法而丢弃 —— 表现为**连接挂死而不是失败**，最难排查。
  因此在暂停窗口内清空宿主与沙箱命名空间两侧的连接跟踪表：此刻没有流量，
  不可能有人重建一条刚被作废的表项。同时丢弃代理侧到该沙箱的连接池。

### 5.4 失败语义分级

不做兜底重建路线之后，失败必须分级报告，**绝不允许「看起来成功、实际数据已坏」**：

| 失败点 | 结果 |
|---|---|
| 路径缺侧车 / 位图损坏 / 物化失败 | 回滚报错，**沙箱保持原状态继续可用** |
| Firecracker 在提交点之前失败 | 同上，虚机原样恢复运行 |
| Firecracker 在提交点之后失败 | 虚机介于两个时刻之间 = 已撕裂，标记为死并报错，只能重建沙箱 |
| 回滚成功但 guest 的 envd 不应答 | 报错并在服务端留下带耗时的日志（等待上限 45 s，刻意低于 SDK 的 60 s 超时，否则客户端先放弃，真正的原因永远到不了调用方） |

创建侧还有一条纪律：**纪元不能丢**。Firecracker 一旦写完快照就清空了脏页位图，
此后该 diff 文件是那一代脏页的唯一副本。若后续步骤失败，该条目以**隐藏条目**提交，
仍参与内容解析与回滚集计算，只是 API 不展示；连这都失败才把链标记为断裂，
此后**拒绝恢复**直到下一次全量 checkpoint —— 不完整的回滚是静默的数据损坏，比报错糟得多。

### 5.5 删除与回收

`delete(id)` 是显式 API：

- 该节点有后代、或它是当前基准 ⇒ 标记为**隐藏**，列表不再展示，文件保留（后代要靠它解析内容）；
- 无后代且非基准 ⇒ 物理删除，并沿父指针级联回收那些「隐藏、无子、非基准」的祖先。

隐藏时会丢掉永远读不到的部分（snapfile、rootfs header —— 隐藏条目不可能成为恢复目标），
只保留 `mem_diff` 与位图。磁盘层按引用计数回收。

---

## 6. 脏页跟踪

整套方案的成本模型建立在一个前提上：**知道从某时刻起 guest 写过哪些页**。

### 6.1 HDBSS 怎么被启用

鲲鹏 950 的 CPU 能自己记录脏页（HDBSS，Hardware Dirty State Tracking），
免去内核写保护每个干净页、并在首次写入时陷出的开销。启用要两侧各做一件事：

- **orchestrator** 启动时用 `KVM_CHECK_EXTENSION(502)` 探测一次（只查询，不创建 VM，
  在没有 `/dev/kvm` 的机器上也安全），据此决定这台机器上的虚机默认是否武装脏页跟踪；
- **Firecracker** 在 `setup_dirty_tracking()` 里用 `KVM_ENABLE_CAP(502, order)` 打开 HDBSS，
  失败则静默退回 KVM 软件写保护。

默认值跟着硬件走，因为**成本跟着硬件走**：有硬件标脏时武装几乎免费；
没有时内核要写保护每个干净页并在首次写入时陷出，对于从不打 checkpoint 的沙箱是纯亏损。

关键细节：**引导和快照加载两条路径都要武装**。e2b 的沙箱只从快照启动，
只改 machine-config 不改加载路径等于没开。

| 环境变量 | 作用 |
|---|---|
| `FC_TRACK_DIRTY_PAGES` | 强制开 / 关，覆盖硬件探测结果 |
| `FC_HDBSS_ORDER` | 每 vCPU 的 HDBSS 缓冲区阶数，默认 1（8 KiB）。写密集负载下缓冲区溢出会吃掉收益，值得实测 1 / 2 / 4 |
| `FC_HDBSS_REQUIRED` | 要求必须启用 HDBSS，否则启动即失败。回滚部署通常希望 fail fast，而不是默默退回软件路径 |

950 已确认 `CONFIG_ARM64_HDBSS=y`、`KVM_CHECK_EXTENSION(502) = 1`、`KVM_ENABLE_CAP` 实际调用成功，
**不需要配置任何环境变量**。

### 6.2 脏页判据的三方差异

这一节是理解本方案价值的前提，**不要跳过**。

| | 脏页怎么判定 | 结果 |
|---|---|---|
| **e2b x86 原生** | 读缺页填充时保留 uffd 写保护位，写缺页则清除；快照时取「已存在且未写保护」的页 | 增量**精确**，只有真正被写过的页算脏 |
| **ARM 适配版**（本方案的基线） | 该写保护路径在 arm64 上走不通，被注释掉；判据第二项恒真 | 增量**退化**：凡被换入过的页都算脏（读也算），量的下限 ≈ 整个常驻工作集 |
| **本方案** | 不经过 uffd，直接取 KVM / HDBSS 的脏页日志 | ARM 上**恢复精确增量**；950 上进一步由 CPU 硬件记录 |

必须明确：**这是修复 ARM 适配引入的退化，不是超越 x86 原生。** 在 x86 上 e2b 的增量本来就是精确的。

同样地，[第 7 节](#7-与-e2b-原生的路径对比)提到的「与改动量无关的下限」也只在 ARM 适配版上成立 ——
它是两件事叠加的结果：① 每次 snapshot 都会重建沙箱，工作集必须重新换入；② 换入即被判脏。
x86 上换入的是干净页，不计入增量。

> 补充一个实现细节：ARM 上「取脏页位图」这个接口实际是 mincore + pagemap 两级 ——
> 先用 mincore 一次筛出常驻页，再只对这些页读 pagemap。退化的是 pagemap 判据的第二项，
> 所以最终位图**等价于** mincore 的结果，但路径上仍在逐页 `pread`，那一级成了纯开销。

---

## 7. 与 e2b 原生的路径对比

| 维度 | e2b 原生 | 本方案 |
|---|---|---|
| 打快照时沙箱 | 停掉，再从新快照重新拉起一个新 Firecracker 进程 | 不停，同一个进程 |
| 快照后的身份 | SandboxID / ExecutionID 不变，但底层已是新进程（新 LifecycleID） | 无进程更替 |
| 内存搬运 | orchestrator 从 Firecracker 进程地址空间逐页拷出 | Firecracker 自己写稀疏差分，位图同一次调用落地 |
| 磁盘增量 | 停沙箱后把写层紧凑导出成 diff | 写层就地封存成只读层，零拷贝 |
| 历史结构 | 线性链，回滚后旧快照失去意义 | 树，可前滚、可跨分支 |
| 恢复方式 | 新建进程 + UFFD，靠缺页把工作集换回 | 原地写回差异页，宿主资源全保留 |
| 恢复成本 | 与虚机规格挂钩 | 与回退跨度挂钩 |
| 产物去向 | 对象存储，跨节点 | 宿主本地，随沙箱回收 |

完整对比与「两者如何配合使用」见[第 20 篇](20-vs-native.md)；配图见
[`../diagrams/03-vs-native.svg`](../diagrams/03-vs-native.svg)。

---

## 8. 优化点汇总

| 层次 | 优化点 | 带来什么 | 对照基准 |
|---|---|---|---|
| 脏页跟踪 | HDBSS 硬件标脏自动接管，启动探测能力，无硬件安全退回 | 由 CPU 记录脏页，950 上开箱即用 | 新增能力 |
| | 判据取自硬件 / 内核日志，绕开 uffd | 补回精确增量 | ARM 适配版 |
| 内存产物 | 差分树：每代只存本代脏页，不复制上一代 | 成本只与改动量有关，ext4 上不需要 reflink | 内部路线修正 |
| | 位图随快照同一次调用落地 | 省一轮接口往返和一次全内存扫描 | 原生 |
| | 树形历史而非线性链 | 回滚后可再前滚、可跨分支跳，历史不丢 | 原生 |
| | 树根存一次全量 | 整棵树自给自足，回滚路径不跨网络 | 内部路线修正 |
| 恢复路径 | 按页沿祖先链解析，不合并链、不重建完整镜像 | 恢复成本与历史链长度无关 | — |
| | 进程内原地回滚 | 宿主资源全部保留，恢复后无需重新换入工作集 | 原生 |
| | 回滚集精确到页 | 代价与回退跨度成正比 | 原生 |
| 磁盘 | 写层就地封存 + 视图活体切换 | 打快照与回滚都不停沙箱 | 原生 |
| | 封存不等待落盘 | 封存耗时不再随改动量增长 | 内部优化 |
| 一致性 | 内存与磁盘在同一次暂停窗口内完成 | 两者是同一瞬间的镜像 | 原生 |
| 架构 | 接口在宿主侧接管 | 沙箱内不需要任何代理程序 | 新增能力 |
| 工程保障 | 失败语义分级、断链拒绝恢复、删除不打断后代 | 杜绝「看起来成功、实际数据已坏」 | 新增能力 |
| | 三层计时 + 全量 / 增量模式回报 | 能定位到具体阶段，能发现静默退化 | 新增能力 |

### 8.1 可观测性：三个时钟

- **客户端墙钟** —— 一次 SDK 调用花了多久；
- **冻结窗口** —— 业务实际感受到的停顿；
- **宿主分阶段耗时** —— 时间去了哪里，落在 `timings.json`（Firecracker 还会回报自己内部的分段）。

前两个说「差了多少」，第三个说「差在哪」。此外每次调用都回报本次是全量还是增量 ——
专门用来抓「静默退化成全量拷贝但仍然成功」这类藏在延迟均值里的问题。

---

## 9. 两套方案：ext4 与 XFS

本项目有两套实现。**判据是产物落盘的文件系统，不是机型** —— 两者与机型正交：

| 轴 | 判据 | 影响 |
|---|---|---|
| 内存产物形态 | 文件系统有没有 reflink（`FICLONE`） | ext4 方案 / XFS 方案 |
| 脏页后端 | CPU 有没有 HDBSS | 硬件标脏 / 软件写保护 |

我们的部署恰好把它们配成了对（950 + ext4、920B + XFS），但这是部署事实，不是设计约束。

| | ext4 方案（主线） | XFS 方案 |
|---|---|---|
| 每代内存产物 | 稀疏差分，仅本代脏页 | 完整镜像（克隆上一代 + 覆盖脏页） |
| 依赖 | 无特殊文件系统要求 | XFS 且 `reflink=1` |
| 内容解析 | 按页沿祖先链解析 | 不需要，产物自足 |
| 树账本的用途 | 算回滚集 **+** 解析内容 | 只算回滚集 |
| 回滚时递给 Firecracker 的内存文件 | 只含回滚集的稀疏文件 | 该代的完整镜像 |
| 需要 `save-dirty-bitmap` 端点 | **是** | 否 |
| 存储占用 | 与改动量成正比 | 逻辑上每代全量（物理上靠 extent 共享） |

最后两行是连带关系，值得单独说明：Firecracker 在回滚时会把**自己的活跃脏页并进写回集**，
并按页从调用方给的文件读内容。

- XFS 方案给的是全量镜像，任何页都读得到，多读几页无害；
- ext4 方案给的是只含回滚集的稀疏文件 —— 如果 Firecracker 写回一个 orchestrator 没有物化的页，
  它会从**文件空洞读到零页**，静默损坏 guest 内存。

所以 ext4 方案必须让 orchestrator **事先知道**活跃脏页集，这正是
`PUT /snapshot/save-dirty-bitmap` 端点存在的唯一理由。XFS 方案的 Firecracker 里没有这个端点。

> 两套的 orchestrator 与 Firecracker **各自成对**，四个二进制不可交叉混用，混用必失败。
> 完整差异见[第 18 篇](18-ext4-vs-xfs.md)。

---

## 10. 边界：它不做什么

checkpoint 产物**随沙箱生命周期回收**，沙箱销毁即整目录删除。
但因果顺序不是「因为恢复不了所以删」—— **即使把目录完整保存下来，也恢复不了**。

缺的不是某一个文件，是四层东西：

| 缺什么 | 为什么 |
|---|---|
| 活体虚机 | 只有一条恢复路径 `RollbackInPlace`，往活着的 Firecracker 对象上写。回滚端点头两步就拒绝非活体 |
| 自足的磁盘产物 | 磁盘视图永远从模板 rootfs 起步再叠封存层；脱离模板，磁盘读不出完整内容 |
| 可加载的账本 | `NewStore` 只建目录，不扫描磁盘。磁盘上信息其实够用，**缺的是读取它的代码** |
| 运行时环境 | 进程、KVM fd、tap、NBD 设备、内存映射 —— 原地回滚的全部价值就在于一个都不重建 |

补齐这四层之后得到的东西，基本就是 e2b 原生 snapshot 本身。**这不是缺陷，是分工的必然结果。**

要长期持久化、跨节点迁移、崩溃恢复，用原生 snapshot。两者可以叠加：
先用 checkpoint 把状态回退到某个已知良好的点，再对该状态打一次原生 snapshot 落盘。

> 完整推导、思想实验与对使用方的建议见[第 16 篇](16-lifecycle-and-portability.md)。

---

## 11. 当前实测状态

**正确性**：950 上 `checkpoint_verify.py` **59 项全过**（2026-08-29，服务端自报脏页后端
`HDBSS（硬件标脏）`，产物落在根盘 ext4），其中最硬的一项是把 `kill -9` 掉的进程连同
**PID 与启动时刻**一起复活；920B 上全套跑完 —— 树语义、pause 之后数据完好、
与原生生命周期操作的兼容矩阵、200 次连续回滚 0 失败。
**性能**：920B（`kvm-wp` 软件写保护 · loop ext4 · 2 GB 模板）上增量六档的
客户端墙钟 **p50 全部达标**，但最高的 256 MB 档是**压线过** ——
p50 196 ms 只剩 3.8 ms 余量，并列的 p99
与深集 restore 已经越线；950 的分档基准尚未开跑。两台机器的模板大小、存储介质、
脏页后端**三项全不同**，两组数字不能相减。
**口径**：达标线只约束**增量 checkpoint（≤ 200 ms）与原地 restore（≤ 100 ms）**，
判定用客户端墙钟 p50、p99 并列；判定表按**脏页量档位**给，不按沙箱内存大小给；
树根全量是每个沙箱一次性的成本，单列不参与判定
（完整定义见[第 23 篇 §1.2](23-performance-methodology.md#12-口径定义表)）。

> **全书的实测数字、逐档达标判定与尚未覆盖的缺口只在[第 25 篇](25-results-and-compliance.md)
> 一处维护**，其他篇一律引用不复制。测试体系本身 —— 尤其是「测试通过 ≠ 测的是那个东西」
> 这件事 —— 见[第 21 篇](21-test-overview.md)。

---

## 12. 接下来读什么

| 你想要 | 读 |
|---|---|
| 搞懂机制，最短路径 | [06 架构](06-architecture.md) → [07 脏页跟踪](07-dirty-page-tracking.md) → [08 内存差分树](08-memory-diff-tree.md) → [10 磁盘分层](10-disk-layering.md) → [11 原地回滚](11-in-place-rollback.md) → [13 端到端](13-end-to-end.md) |
| 补背景（不熟悉 microVM / e2b / 快照） | [01](01-what-and-why.md) → [02](02-microvm-and-e2b.md) → [03](03-snapshot-fundamentals.md) → [04](04-e2b-native-snapshot.md) |
| 判断能不能用、边界在哪 | [16 生命周期与边界](16-lifecycle-and-portability.md) → [20 与原生的对比配合](20-vs-native.md) |
| 部署与验收 | [19 鲲鹏平台](19-kunpeng-platform.md) → [17 可观测与验证](17-observability-and-verification.md) |
| 看测试证据、对照客户指标 | [21 测试体系总览](21-test-overview.md) → [25 实测结果与达标判定](25-results-and-compliance.md)，方法细节按需进 [22](22-functional-tests.md) / [23](23-performance-methodology.md) / [24](24-cross-implementation.md) |
| 拿到交付件要上机验收 | [26 上机验收操作](26-acceptance-runbook.md) |
| 接手继续开发 | [14 失败语义](14-failure-semantics.md) → [15 状态与并发](15-state-and-concurrency.md) → [27 继续开发](27-extending.md) |

完整目录见 [README.md](README.md)。

---

## 附录 A：代码位置

**上游**：openEuler KASandbox 的 [`deltabox` 分支](https://gitcode.com/openeuler/KASandbox/tree/deltabox)（[MR !119](https://gitcode.com/openeuler/KASandbox/pull/119)，2026-09-09 合入），
orchestrator、Firecracker、Python SDK 同仓 —— 下表 orchestrator 的路径以 `packages/orchestrator/` 起，
Firecracker 的路径以 `firecracker/` 起。**开发分支**：`infra-arm@jll`（orchestrator）、`KASandbox@jll`（Firecracker）；
XFS 方案（`jll-xfs`）未合入上游。**交付形态**是 `e2b-infra` 仓库的 `0001-adapted-for-arm-architecture.patch` 与 `firecracker.arm`。
**单元测试只在 `infra-arm`**：不进 patch（rpmbuild 的 `%build` 只做 `go build`），也未随 MR 合入上游。
三处的关系见[第 27 篇 §1.1](27-extending.md#11-三个地方)。

| 关注点 | 位置 |
|---|---|
| 服务入口、create / restore 编排 | `packages/orchestrator/internal/checkpoint/service.go` |
| 树账本、回滚集、内容解析、物化 | `packages/orchestrator/internal/checkpoint/store.go` |
| FCDB 位图读写 | `packages/orchestrator/internal/checkpoint/bitmap.go` |
| 磁盘层账本、层侧车 | `packages/orchestrator/internal/checkpoint/rootfs.go` |
| 暂停窗口内的两个动作 | `packages/orchestrator/internal/sandbox/checkpoint.go` |
| 写层封存、视图活体切换 | `internal/sandbox/block/overlay.go`、`block/sealed_view.go`、`rootfs/nbd.go` |
| 脏页跟踪的默认值决策 | `packages/orchestrator/internal/sandbox/fc/dirtytracking.go` |
| 回滚端点的客户端 | `packages/orchestrator/internal/sandbox/fc/rollback.go` |
| 连接跟踪清理 | `packages/orchestrator/internal/sandbox/network/conntrack.go` |
| 分阶段计时 | `packages/orchestrator/internal/sandbox/phasetimings.go` |
| 原地回滚本体（Firecracker） | `src/vmm/src/rollback.rs` |
| HDBSS 启用与脏跟踪后端选择 | `src/vmm/src/arch/aarch64/vm.rs`、`src/vmm/src/vstate/vm.rs` |
| 快照写出与位图侧车 | `src/vmm/src/vstate/vm.rs`、`src/vmm/src/vstate/memory.rs` |

完整索引见[第 28 篇](28-glossary-and-code-map.md)。

## 附录 B：验收脚本

交付态是 `e2b-infra/benchmark/` 下两个**零共享依赖**的单文件脚本 ——
`checkpoint_verify.py` 只证正确性、`checkpoint_bench.py` 只测耗时，
拷到目标机上就能跑；开发态工具箱在 [`../test-950/`](../test-950/)，**两套不要混用**。
设计原则见[第 21 篇 §2](21-test-overview.md#2-三层测试)，上机怎么跑见[第 26 篇](26-acceptance-runbook.md)。
