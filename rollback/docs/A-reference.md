# 附录 A · 术语、协议与文件格式参考

本附录用于查阅术语、协议、文件格式和不变量速查；实现推导与代码阅读顺序分别见第二、三篇和第 27 章。

## 1. 术语表

### 1.1 本方案的概念

| 术语 | 英文 / 标识 | 一句话 | 详见 |
|---|---|---|---|
| **checkpoint** | checkpoint | 沙箱某一时刻的状态记录，也指拍它的操作（与 restore 成对）。接口名里叫 `CreateCheckpoint` | [第 21 章](21-sdk-quickstart.md)、[第 6 章](06-memory-diff-tree.md) |
| **restore / 回滚** | restore / rollback | 把活着的沙箱原地退回某个 checkpoint，沙箱 ID 与 FC 进程不变 | [第 9 章](09-in-place-rollback.md) |
| **纪元** | epoch | 两次"脏页跟踪被重置"之间的时间段 | [第 6 章](06-memory-diff-tree.md) |
| **纪元位图 / 侧车** | `E_x`、`mem_bitmap`、sidecar | 记录一个纪元里写过哪些页 | [第 6 章](06-memory-diff-tree.md)、[第 8 章](08-firecracker-contract.md) |
| **差分树** | diff tree | 以 `ParentID` 组织的 checkpoint 树 | [第 6 章](06-memory-diff-tree.md) |
| **基准** | base | 下一个 checkpoint 的父节点；每沙箱一个 | [第 6 章](06-memory-diff-tree.md) |
| **回滚集** | revert set | restore 时需要写回的页集合：从基准到目标路径上各纪元位图的并集，去掉最近公共祖先以上的部分 | [第 6 章](06-memory-diff-tree.md) |
| **物化** | materialize | 把回滚集在目标时刻的内容聚成一个稀疏文件，连同位图交给 FC | [第 6 章](06-memory-diff-tree.md) |
| **全量根** | full root | 树根存完整内存，使整棵树自足；它的回滚因子恒为全 1 | [第 6 章](06-memory-diff-tree.md) |
| **隐藏条目** | hidden entry | 在树里、不在 API 里的条目：被删但仍有后代要经过它解析 | [第 6 章](06-memory-diff-tree.md)、[第 11 章](11-failures-and-invariants.md) |
| **合并** | fold / compact | 隐藏、非基准、恰有一个子节点的条目并入它的子节点：子节点接管它的父节点，回滚集取并集，内容以子节点为准，rootfs 层成对合并。在 delete 结尾同步执行，由 `CHECKPOINT_COMPACT` 控制 | [第 6 章](06-memory-diff-tree.md)、[第 7 章](07-disk-layering.md) |
| **条目数上界** | 2V + 1 | 合并追平后，一个沙箱的条目数不超过 2 × 可见数 + 1 | [第 6 章](06-memory-diff-tree.md) |
| **字节配额** | `CHECKPOINT_MAX_BYTES_PER_SANDBOX` | 每沙箱 checkpoint 占用磁盘实际块数的上限，默认关；超限拒绝 checkpoint（reason `checkpoint_bytes_limit`），restore 不受限 | [第 24 章](24-configuration-and-capacity.md)、[第 22 章](22-api-semantics.md) |
| **层引用计数** | layer refs | 每个封存层被多少个已发布视图和活账本引用；最后一个引用消失时回收该层 | [第 7 章](07-disk-layering.md) |
| **断链** | broken chain | 纪元丢失，此后下一次 checkpoint 是新的全量根 | [第 11 章](11-failures-and-invariants.md) |
| **封存** | seal | 把活写层原地变成只读层，零数据搬运 | [第 7 章](07-disk-layering.md) |
| **封存层** | sealed layer，`layer-<uuid>` | 封存后的只读层文件 | [第 7 章](07-disk-layering.md) |
| **视图** | rootfs view | 某个 checkpoint 的磁盘由哪些层按什么顺序叠成 | [第 7 章](07-disk-layering.md) |
| **块归属索引** | `LayerStack` | 每块由哪一层提供的数组，读一块只查一次，与层数无关 | [第 7 章](07-disk-layering.md) |
| **合并 header** | merged header | "模板映射 + 各封存层恒等映射"的序列化结果 | [第 7 章](07-disk-layering.md) |
| **提交点** | commit point | 回滚中开始改 guest 状态的那一刻 | [第 9 章](09-in-place-rollback.md) |
| **撕裂** | torn / `Faulted` | 虚机介于两个时刻之间，只能重建沙箱 | [第 11 章](11-failures-and-invariants.md) |
| **账本污染** | poisoned | 账本与活的层栈不一致，此后拒绝服务直到下一次成功 restore | [第 7 章](07-disk-layering.md) |
| **冻结窗口** | freeze window | 虚机从暂停到恢复的时间；业务感受到的停顿，不用来判定达标 | [第 2 章](02-background.md)、[第 16 章](16-test-methodology.md) |

### 1.2 平台与底层

| 术语 | 全称 | 一句话 | 详见 |
|---|---|---|---|
| **microVM** | —— | 保留硬件隔离、设备模型砍到最小的虚拟机 | [第 2 章](02-background.md) |
| **Firecracker** | —— | AWS 开源的 microVM 监控器 | [第 2 章](02-background.md) |
| **HDBSS** | Hardware Dirty state tracking Structure | ARMv9.5 的硬件脏页跟踪，950 上有，920B 上没有 | [第 5 章](05-dirty-tracking-and-hdbss.md) |
| **kvm-wp** | —— | KVM 软件写保护脏页跟踪，每个干净页第一次写陷出一次 | [第 5 章](05-dirty-tracking-and-hdbss.md) |
| **Stage-2** | —— | KVM 管理的第二层地址翻译（IPA → 宿主物理） | [第 5 章](05-dirty-tracking-and-hdbss.md) |
| **DBM** | Dirty Bit Modifier | 页表项上让硬件自己标脏的位 | [第 5 章](05-dirty-tracking-and-hdbss.md) |
| **VHE** | Virtualization Host Extensions | ARM 虚拟化宿主扩展，HDBSS 要求它 | [第 5 章](05-dirty-tracking-and-hdbss.md) |
| **UFFD** | userfaultfd | 用户态处理缺页的内核机制 | [第 2 章](02-background.md) |
| **NBD** | Network Block Device | 把用户态对象暴露成内核块设备 | [第 7 章](07-disk-layering.md) |
| **VMGenID** | VM Generation ID | ACPI 设备，代号变化告诉 guest"你被恢复了" | [第 10 章](10-device-network-time.md) |
| **conntrack** | connection tracking | 宿主内核的连接跟踪表，restore 时要清掉沙箱地址的条目 | [第 10 章](10-device-network-time.md) |
| **稀疏文件** | sparse file | 未写过的区间不占物理块 | [第 6 章](06-memory-diff-tree.md) |
| **MADV_POPULATE** | `MADV_POPULATE_READ` / `MADV_POPULATE_WRITE` | 在系统调用里预先把映射的页缺进来，Linux 5.14+ | [第 12 章](12-state-and-lifecycle.md) |
| **STW** | stop-the-world | Go GC 暂停所有 goroutine 的阶段 | [第 12 章](12-state-and-lifecycle.md) |

### 1.3 e2b 的概念

| 术语 | 一句话 | 详见 |
|---|---|---|
| **template** | 沙箱的出厂状态，构建产出一份可反复加载的快照 | [第 2 章](02-background.md) |
| **sandbox** | 一个运行中的沙箱实例 | [第 2 章](02-background.md) |
| **envd** | guest 里执行命令、读写文件的守护进程 | [第 2 章](02-background.md) |
| **orchestrator** | 宿主上管沙箱生命周期、网络、存储、代理的进程 | [第 4 章](04-architecture-and-flow.md) |
| **client-proxy** | 集群入口代理，把客户端请求转给所在节点的 orchestrator | [第 4 章](04-architecture-and-flow.md) |
| **hyperloop** | orchestrator 在宿主上给每个沙箱提供的 HTTP 服务，guest 经 `192.0.2.1:80` 访问（宿主侧端口 5010）；部署脚本的 80 → 3002 转发目前劫持了这条链路 | [附录 C §13.1](C-evolution-and-cases.md#131-80-端口的转发规则劫持-hyperloop-请求) |
| **SandboxID / ExecutionID / LifecycleID** | 三个不同层次的身份标识 | [第 13 章](13-native-snapshot.md) |

### 1.4 测试与测量

| 术语 | 一句话 | 详见 |
|---|---|---|
| **静默失效** | 断言全过、内容全对，坏的是成本、覆盖范围或判据本身 | [第 16 章](16-test-methodology.md) |
| **`mem_mode` / `memMode`** | 服务端每次 checkpoint 回报本次是 `full` 还是 `incremental`；除了沙箱的第一个，出现 `full` 就是有问题 | [第 25 章](25-observability.md)、[第 16 章](16-test-methodology.md) |
| **条件标签** | 一个耗时或体积数字必须随身携带的机型、脏页后端、产物盘、模板规格、二进制、脚本参数 | [第 16 章](16-test-methodology.md) |
| **名义改动量 / 档位** | 基准里每次迭代请求改多少（内存:文件 = 3:1），判定表按它分档 | [第 16 章](16-test-methodology.md) |
| **时机成本** | "写完立刻拍"减"`sync` 后再拍"，回写争用的代价，不是快照机制的代价 | [第 16 章](16-test-methodology.md) |
| **浅集 / 深集** | restore 回退一步与回到链根两种形态，把回滚集大小和链深拆开 | [第 16 章](16-test-methodology.md) |
| **底噪** | 冻结窗口采样器自身的间隔抖动；冻结值不高于 2 倍底噪时不作数 | [第 16 章](16-test-methodology.md) |
| **工作集**（对比脏页） | 被碰过的页集；脏页是被写过的页集。把读也算脏时增量退化成工作集 | [第 5 章](05-dirty-tracking-and-hdbss.md)、[第 14 章](14-native-precise-increment.md) |
| **`REFUSED` / `BROKEN`** | 兼容矩阵的两种非 OK 判定：前者是安全的拒绝（边界），后者是真问题 | [第 16 章](16-test-methodology.md) |
| **真实链深** | 沿 `parent_id` 走到根的最大深度，含隐藏条目 | [第 16 章](16-test-methodology.md) |
| **上限 60 / 滚动保留 10** | 长跑的两种常规负载：服务端数量上限 60；客户端只保留最新 10 个、删最旧。另有只用来找问题、耗时不进判定的无上限压测 | [第 16 章](16-test-methodology.md)、[第 19 章](19-950-concurrency.md) |
| **走平** | 长跑中资源量（占盘、目录数、真实链深等）在最后 15 min 的最小二乘斜率接近 0；只看终值不算 | [第 16 章](16-test-methodology.md) |

### 1.5 原生快照的精确增量

这一组术语属于 e2b 原生 pause 路径上的独立修复（第三篇），不是本方案 checkpoint / restore 的一部分；写法以 [第 14 章](14-native-precise-increment.md)–[第 15 章](15-native-cooperation-and-cost.md) 为准。

| 术语 | 英文 / 标识 | 一句话 | 详见 |
|---|---|---|---|
| **判 / 存 / 填** | ① ② ③ | 原生 pause 内存增量的三层：① 判（这一代导出哪些页）、② 存（按 header 的 `BlockSize` 把页存进紧凑差分，header 记映射链）、③ 填（恢复时 uffd 按 guest 页缺页，从映射链取内容填进去） | [第 14 章](14-native-precise-increment.md) |
| **驻留即脏** | resident-page criterion | 原生 pause 修复前的脏页判据"页驻留（mincore）且 uffd 写保护位未置"在 arm64 上塌缩成"驻留过就算脏"：arm64 6.6 没有 uffd 写保护，ARM 适配补丁注释掉了填页时的写保护，于是读进来的页也算脏、增量退化成工作集。修复后它仍是所有退路的落脚点 | [第 14 章](14-native-precise-increment.md)、[第 5 章](05-dirty-tracking-and-hdbss.md) |
| **2 MiB 归并地板** | —— | 只把判据换成 4 KiB 写跟踪位图、差分块仍是 2 MiB 时，每代零散的本底写被归并到 2 MiB 块后留下的、与改动量无关的导出量下限 | [第 14 章](14-native-precise-increment.md) |
| **粒度锁** | `BlockSize` == guest 页 | 把三层钉在 2 MiB 上的链条：guest 内存由 2 MiB 大页背衬 → uffd 只能按 2 MiB 填 → 起沙箱时硬检查"页大小等于块大小" → header 块大小逐代继承 | [第 14 章](14-native-precise-increment.md) |
| **写跟踪差分** | 4 KiB 写跟踪差分；"4 KiB 存、2 MiB 拼" | 修复后的原生增量：① 判据换成 Firecracker 的写跟踪位图（`save-dirty-bitmap`），② header 块大小降到 4 KiB，③ 缺页仍按 2 MiB，内容从 4 KiB 映射拼回；Firecracker 零改动 | [第 14 章](14-native-precise-increment.md) |
| **三级取源** | `block.ReadPage` | 一次 2 MiB 缺页取内容的三条路：块等于页走原来的 `Slice`；单个映射覆盖整页时直接返回那一代的切片（零拷贝）；跨代的块才从缓冲池取 2 MiB、按 4 KiB 拼 | [第 14 章](14-native-precise-increment.md) |
| **自启动以来脏页集** | `DirtySinceBoot`；累积位图 | 每虚机一份：在 checkpoint、restore 让 Firecracker 清零写跟踪位图之前，把即将被忘掉的位图并进来；原生 pause 时并回写跟踪位图，保证导出 guest 启动以来写过的每一页。有任何一份并不进去就整个标为不可信 | [第 14 章](14-native-precise-increment.md)、[第 6 章](06-memory-diff-tree.md) |
| **退路（四道门）** | `tracked` / `tracked+accumulated` / `resident` | 跟踪没开、脏页集不可信、Firecracker 没有 `save-dirty-bitmap` 端点、并入时几何不符，任一成立就退回驻留判据；pause 日志写明本次导出依据是三者中的哪一种 | [第 14 章](14-native-precise-increment.md) |
| **宁可多导，不可少导** | —— | 判据改动的安全方向：多导一页只费时间与空间，少导一页会让恢复出的 guest 内存由两个时刻拼成，而 pause 本身报成功 | [第 14 章](14-native-precise-increment.md) |

## 2. 文件格式索引

产物目录布局见 [第 4 章](04-architecture-and-flow.md)。每个 checkpoint 一个目录 `ckpt_<UnixNano>/`，沙箱级的封存层在 `layers/`。

| 文件 | 格式 | 写者 | 读者 | 详见 |
|---|---|---|---|---|
| `snapfile` | FC vmstate（二进制） | FC | FC | —— |
| `mem_diff` | 稀疏文件，页写在各自的偏移上；全量根也用这个名字 | FC | orchestrator | [第 6 章](06-memory-diff-tree.md) |
| `mem_bitmap` | FCDB 位图（[§2.1](#21-fcdb-逐字节)） | FC | orchestrator | [第 8 章](08-firecracker-contract.md) |
| `mem_diff.<id>`、`mem_bitmap.<id>` | 合并后子节点的新内存文件与并集侧车，`<id>` 是被并入条目的 ID；用新名字而不是改名，避免切换失败时两个条目共享 inode | orchestrator | orchestrator | [第 6 章](06-memory-diff-tree.md) |
| `rootfs.header` | 序列化的合并 header | orchestrator | orchestrator | [第 7 章](07-disk-layering.md) |
| `layers/layer-<uuid>` | 原写层文件改名而来，块在原偏移上 | guest（经 NBD） | 运行中的沙箱 / restore | [第 7 章](07-disk-layering.md) |
| `layers/layer-<uuid>.meta` | 8 字节小端一条的块偏移列表 | orchestrator | orchestrator | [第 7 章](07-disk-layering.md) |
| `layers/layer-<uuid>.<id>` | 层合并产生的合成层（及其 `.meta`），`<uuid>` 是接收方那一层的，`<id>` 是被并入条目的 ID | orchestrator | 同上 | [第 7 章](07-disk-layering.md) |
| `manifest.json` | 单条目的完整 JSON | orchestrator | 无人（事后排查） | [第 4 章](04-architecture-and-flow.md) |
| `index.json` | 沙箱的条目清单；**默认不写**，`CHECKPOINT_DEBUG_INDEX=true` 时后台最多每 5 s 写一次 | orchestrator | 无人（调试） | [第 4 章](04-architecture-and-flow.md) |
| `timings.json` | `{计时键: 毫秒}`，每个 checkpoint 一份 | orchestrator | 基准脚本 | [第 25 章](25-observability.md) |
| `last-restore-timings.json` | 沙箱目录下，每次 restore 覆写 | orchestrator | 基准脚本 | [第 25 章](25-observability.md) |
| `revert_mem.tmp`、`revert_bitmap.tmp` | 物化结果：稀疏内存文件与 FCDB，写在目标 checkpoint 目录下，只存在于一次 restore 期间 | orchestrator | FC | [第 6 章](06-memory-diff-tree.md) |
| `<store>/.trash-<uuid>` | 被丢弃的整个沙箱目录，锁内改名、锁外删除；启动时清空 store 根会顺带删掉残留 | orchestrator | —— | [第 12 章](12-state-and-lifecycle.md) |
| `<DEFAULT_CACHE_DIR>/checkpoint-capabilities.json` | 启动能力的 JSON（能力行的键 + `pid`、`started_at`、`version`、`commit`），在 store 根的父目录，每次启动整份替换 | orchestrator | 运维、`run.sh smoke` | [第 24 章 §3.1](24-configuration-and-capacity.md#31-能力文件) |

### 2.1 FCDB 逐字节

| 偏移 | 长度 | 内容 |
|---|---|---|
| 0 | 4 | magic `"FCDB"` |
| 4 | 4 | version，u32 小端，当前 `1`（`DIRTY_BITMAP_VERSION`） |
| 8 | 8 | `page_size`，u64 小端 |
| 16 | 8 | `num_pages`，u64 小端 |
| 24 | 8 × ⌈num_pages / 64⌉ | 位图字，u64 小端 |

第 `w` 个字的第 `i` 位对应页索引 `w × 64 + i`。页索引是内存文件偏移 ÷ 页大小，不是 guest 物理地址。
尾字超出 `num_pages` 的位必须为 0。FC 对全量快照写全 1 的侧车；orchestrator 对全量根不读侧车，直接取全 1。

## 3. 接口速查

SDK 与 RPC 的调用方式见 [第 21 章](21-sdk-quickstart.md)，超时、不重放与错误处置见 [第 22 章](22-api-semantics.md)。

| SDK 方法 | RPC（宿主侧端口 49984） |
|---|---|
| `sandbox.checkpoint.create(name=None)` | `/checkpoint.Checkpoint/CreateCheckpoint` |
| `sandbox.checkpoint.restore(checkpoint_id)` | `/checkpoint.Checkpoint/RestoreCheckpoint` |
| `sandbox.checkpoint.delete(checkpoint_id)` | `/checkpoint.Checkpoint/DeleteCheckpoint` |
| `sandbox.checkpoint.list()` | `/checkpoint.Checkpoint/ListCheckpoints` |
| `sandbox.checkpoint.is_available()`（旧名 `is_running()`） | `/health`，由宿主应答，不进沙箱 |

Firecracker 的三处扩展：`CreateSnapshotParams.dirty_bitmap_path`（已有端点的可选字段）、`PUT /snapshot/rollback`、
`PUT /snapshot/save-dirty-bitmap`（均为新端点）。

## 4. 不变量速查

全书的不变量共 19 条，**编号、定义、破坏后果和是否静默只在 [第 11 章 §9](11-failures-and-invariants.md#9-不变量清单) 列出**，其他篇引用时都用那里的编号。
改代码前过一遍那张表；其中 #15–#19 是合并、full 根回滚因子、子节点计数表、层引用计数与字节计数这几项新机制带来的，
属性测试怎么守它们见 [第 16 章](16-test-methodology.md)。
