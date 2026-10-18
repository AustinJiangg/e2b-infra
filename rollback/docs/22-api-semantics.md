# 22 · 接口语义、错误、超时与并发调用

这里是使用方的完整契约：恢复范围、外部副作用、失效条件、删除和回收、异常、超时及多调用方行为。机制原因通过链接回查第二、三篇。

## 1. restore 回滚回什么

restore 把**这台正在运行的虚机**原地放回 checkpoint 那一刻。沙箱全程不重建：同一个 Firecracker 进程、同一个沙箱 ID、
同一个 IP 与端口映射。回到过去的是 guest 看得见的全部状态：

| 部分 | 回到 checkpoint 时刻 |
|---|---|
| guest 内存 | 是。进程、页缓存、tmpfs（如 `/dev/shm`）里的内容都在内存里，一起回去 |
| vCPU 寄存器、中断控制器 | 是 |
| virtio 设备的逻辑状态（队列位置、协商特性、中断状态） | 是 |
| 磁盘（整个根文件系统，不只是家目录） | 是。新建、修改、删除、改权限都回去 |
| 进程 | 是。checkpoint 时在跑的进程以**同一个 PID** 继续跑，之后启动的进程消失，之后被杀的进程回来 |

内存和磁盘是在**同一次暂停**里拍下、在同一次暂停里换回的，所以两者是同一瞬间的镜像：
guest 还没刷到盘上的数据留在内存镜像里，restore 之后不会出现"内存新、磁盘旧"的半新半旧状态。

checkpoint 期间业务能感到的只有一次短暂停顿（冻结窗口）；restore 同样只停一次。量级见 [第 18 章](18-950-performance.md)。

## 2. restore 回滚不了什么

### 2.1 外部世界

虚机退回去了，外部世界没有。checkpoint 之后沙箱对外做过的事 —— 调过的 API、写进外部数据库的数据、发出去的消息、
上传到别处的文件 —— **restore 撤销不了**，restore 之后沙箱里的程序也不记得它们发生过。
需要"恰好一次"的外部操作，要由业务自己做幂等或去重。这是任何虚机级快照的硬边界。

### 2.2 TCP 连接：跨越 restore 的一律作废，要重连

restore 让 guest 的 TCP 状态也回到了 checkpoint 时刻，跨越 restore 保持着的连接，guest 一侧已经不认识了。
服务端因此在 restore 时主动清掉这个沙箱的连接状态：

- 宿主连接跟踪表里属于这个沙箱的表项、沙箱网络命名空间内的连接跟踪表，都在暂停窗口里清掉；
- orchestrator 代理到这个沙箱的连接池在 restore 一开始就丢弃。

对调用方意味着：

- **所有跨越 restore 的连接都要重建**：进沙箱的连接（SDK 的命令、文件读写、你自己暴露的服务端口），
  以及沙箱里的程序连到外面的长连接。SDK 自己的调用会重新建连；你在沙箱里起的服务、沙箱里连外部的客户端，要自己处理重连；
- restore 那一刻还在途的 SDK 调用会被打断，调用方会收到 `CheckpointInterruptedException` 或传输层错误，
  两种情形的区别见 [第 22 章](22-api-semantics.md)；
- **restore 返回之后新建的连接不受影响**，guest 往外新建的连接也不受影响；
- **同一节点上的其他沙箱不受影响**：宿主侧只删属于这个沙箱的表项，从不整表清空。

### 2.3 时钟

- **单调时钟（`CLOCK_MONOTONIC`）会倒退**。它来自 vCPU 的计时器寄存器，而这些寄存器被恢复成了 checkpoint 时刻的值。
  对 guest 而言它一直处在那一刻，这是快照语义下唯一自洽的做法。
- **墙钟（`CLOCK_REALTIME`）在 restore 返回之前被校回当前时间**。restore 的最后一步是 orchestrator 向 guest 里的 envd 发
  `POST /init`，请求体带宿主的当前时间（`service.go:1315` → `sandbox.go:1316` `waitForEnvd` → `envd.go:104` `initEnvd`，
  时间戳在 `envd.go:48` 填入）。envd 收到后比较自己的墙钟：比宿主早超过 50 ms 或晚超过 5 s，就用 `clock_settime(CLOCK_REALTIME)`
  设成宿主时间（`packages/envd/internal/api/init.go:172` `SetData`、`:312` `shouldSetSystemTime`，阈值在 `:30-31`）。
  这与 e2b 原生 pause / resume 走的是同一条路径。
  由此可知：guest 从回滚后恢复运行，到这次 `/init` 被处理，这段时间里墙钟仍停在 checkpoint 时刻附近；
  这段时间在 restore 调用返回之前，调用方在 restore 返回后发起的命令看到的已是校正后的时间（误差不超过上面的阈值）。
  如果 envd 始终不应答（restore 以 guest 失联报错，见 [第 22 章](22-api-semantics.md)），墙钟就没有被校正。

对业务代码的影响：

- 不要用单调时钟测量跨越 restore 的间隔，得到的值没有意义；
- 依赖"时间一直向前"的租约、缓存过期、超时判断，restore 之后可能要重新校准；
- 验收或业务里的"活性判据"用**计数器在增长**，不要用"时间戳在推进"或"tick 速率正常"。

### 2.4 随机数与"被回滚"这件事本身

如果 guest 带有 VMGenID 设备，restore 会在写回内存之后刷新它的代号，guest 内核据此知道自己被从快照恢复过、重新播种随机数。
下面这点是内存回滚的直接推论：用户态程序自己缓存的随机状态（例如进程里已经初始化好的 PRNG）就在 guest 内存里，
restore 把它原样带回 checkpoint 时刻，VMGenID 也不会替它重新播种；所以从同一个 checkpoint 出发的两次"不同"执行，
可能产生相同的随机序列。对随机性敏感的程序应当在 restore 之后自行重新初始化。

## 3. checkpoint 什么时候失效

checkpoint 存在**宿主本地**，只在所属沙箱的当前这一代里有效。它不进对象存储，不跟随沙箱迁移，
也没有加载回来的机制 —— 即使把目录完整拷走也恢复不了（原因见 [第 12 章](12-state-and-lifecycle.md)）。

| 事件 | 结果 |
|---|---|
| 沙箱被删除、超时被回收 | 该沙箱的全部 checkpoint 随之删除 |
| orchestrator 重启 | 同上。重启本来就会带走这台机器上的所有沙箱；上一个进程留在盘上的 checkpoint 文件在启动时被清空 |
| 宿主重启或宕机 | 同上 |
| 沙箱迁移到别的节点 | checkpoint **不跟随** |
| 沙箱经原生 `pause` 再 resume（`connect`） | pause 之前的 checkpoint **全部作废**；resume 之后是新的一代，可以照常重新 checkpoint / restore（[§3.1](#31-原生-pause--resume-与-checkpoint-的代际边界)） |
| 单个 checkpoint 被 `delete` | 从接口上消失；数据由服务端按需保留、合并（compact / fold）或回收（[§4](#4-删除与空间回收用户视角)） |

### 3.1 原生 pause / resume 与 checkpoint 的代际边界

**规则**：原生 pause 会让该沙箱的全部 checkpoint 作废；resume 之后，同一个沙箱 ID 底下是"新的一代"，从零开始。

| 时刻 | `checkpoint.list()` | restore 一个 pause 之前的 checkpoint | 新建 checkpoint |
|---|---|---|---|
| pause 之前 | 这一代的全部条目 | 正常 | 正常 |
| resume 之后 | **空** | **`NotFoundException`**（`not_found`） | **正常**；这一代的第一次是新的全量根 |

原因：restore 是往**活着的那个 Firecracker 进程**里原地写回，而原生 resume 是用同一个沙箱 ID 新起一个 Firecracker 进程。
旧那一代的差分已经没有可以写回的对象了，保留它们只会给调用方一个必定失败的 ID。
所以服务端按"代"（每个 Firecracker 进程一代）而不是按沙箱 ID 管理 checkpoint。

## 4. 删除与空间回收：用户视角

这一节只讲调用方需要知道的规则和该怎么做；删除、隐藏、合并的机制与正确性证明见 [第 6 章](06-memory-diff-tree.md)，
相关开关与容量规划见 [第 24 章](24-configuration-and-capacity.md)。

### 4.1 `delete` 做了什么

- 被删的 checkpoint **立即从 `list()` 消失**，之后不能再 restore 到它；
- 如果它**还有后代**（有别的 checkpoint 是在它之后拍的），或者它是**沙箱当前所处的点**
  （最近一次 checkpoint，或最近一次 restore 的目标），它的数据不能删 —— 后代恢复时还要读它。
  这时它只是**隐藏**：接口上看不到，只丢掉它自己专用的一小部分文件；
- 被隐藏、又只剩**一个**子节点的 checkpoint，会在后续的 `delete` 结尾被**合并进那个子节点**（默认开启）。
  合并不改变任何一次 restore 的结果，只是把两份数据变成一份；
- 没有后代、也不是当前点的 checkpoint 直接删除，并顺带回收因此不再被需要的隐藏祖先。

所以"删了但盘没降"通常不是泄漏，而是那份数据还有人要读。

### 4.2 "保留最新 N 个、删最旧的"会收敛

最常见的滚动用法是：每一步 checkpoint 一次，超过 N 个就删最旧的。每次被删的都是下一个的父节点，
于是它变成"隐藏且只有一个子节点"，随即被合并进子节点。结果是：

- 可见的 checkpoint 稳定在 N 个，隐藏的不会无限积累 —— 合并追上之后，一个沙箱的条目总数（可见加隐藏）不超过 2N+1；
- 这消除了隐藏条目和历史依赖链的无界积累；总占盘和恢复耗时仍受各代实际数据、视图覆盖块、当前回滚量及宿主负载影响，不能仅由保留数量推出固定上界。920B的实际走势见[附录 B §10.3](B-920b-results.md#103-滚动保留-10一小时与两小时)，950小时级收敛仍待对应证据。

如果运维关掉了合并，这种用法会让隐藏的 checkpoint 越积越多，占盘和 restore 耗时随时间上涨。
同样的积累也发生在宿主不跟踪脏页的时候（每个 checkpoint 都是全量根，没有可合并的节点）。

### 4.3 两种每沙箱上限

运维可以给每个沙箱设两种上限（默认都不设），被拒时的异常与状态码见 [第 22 章](22-api-semantics.md)：

| 上限 | 数什么 | 被拒时怎么办 |
|---|---|---|
| **个数上限** | 可见的 checkpoint 个数（隐藏的不算，调用方看不见也删不掉） | 删掉任意一个不再需要的，再 checkpoint |
| **字节上限** | 这个沙箱的全部 checkpoint 实际占用的盘（包括隐藏的、共享的磁盘层） | **删哪个很重要**，见 [§4.4](#44-在字节上限下删哪个才能释放空间) |

两种上限都只拦 checkpoint，**从不拦 restore**；拒绝发生在写任何东西之前，沙箱照常运行、已有的 checkpoint 完好。
字节上限只要当前还低于上限就放行，所以实际占用可能超出上限一个 checkpoint 的量。

### 4.4 在字节上限下，删哪个才能释放空间

| 做法 | 能腾出多少 |
|---|---|
| 删**当前基准**（最近一次checkpoint或restore的目标，未必是最新条目） | **几乎为零**：它是沙箱当前运行的基准，只能隐藏，只释放它的快照描述文件 |
| 删**最旧**的（或较旧的中间节点） | 它会被合并进下一个 checkpoint，释放两者**重叠**的那部分页：两次改的是同一批页，就能腾出一份；两次改的页完全不相交，合并后几乎腾不出空间 |
| restore 到一个较早的 checkpoint，再删掉它之后的那一整支 | 被删的分支没有后代、也不是当前点，直接删除，**释放最彻底** |

被拒时服务端的错误信息会写明：当前占用、上限、沙箱当前所处的 checkpoint ID（删它没用），
以及这个沙箱无论怎么删都至少要占的量（约一份 guest 内存的全量镜像）。

一个沙箱在运行期间至少要保留一份全量内存镜像（它的树根，或合并后成为树根的那个 checkpoint）。
因此上限设得比"一份 guest 内存加少量开销"还小，沙箱一旦顶到上限就再也删不回来；
要在最坏负载下还能保留一个更早的恢复点，上限应不低于"两份 guest 内存加少量开销"。
服务端在沙箱第一次 checkpoint 时发现上限过小会打日志告警，推荐值与推导见 [第 24 章](24-configuration-and-capacity.md)。

## 5. 与原生 snapshot（pause / resume）怎么配合

一句话分工：**沙箱活着时的回退用 checkpoint，沙箱离场用原生 snapshot。** checkpoint 是宿主本地、原地、只存本代改动的；
原生 snapshot 是可以离开这台宿主、另起沙箱的。按需求选哪一个的对照表（[第 15 章 §1.3](15-native-cooperation-and-cost.md#13-分工)）、两者为什么这样分工、逐项对比与优化点，都在 [第 15 章](15-native-cooperation-and-cost.md)。

两者可以串起来用：先用 checkpoint / restore 把沙箱精确定位到想要的那一刻，再对这个状态做一次原生 pause 固化下来。一个完整的 Agent 工作流、以及每一步底下发生了什么，见 [第 15 章 §6.2](15-native-cooperation-and-cost.md#62-一个-agent-工作流以及底下发生了什么)。

使用时记住三条：

- **方向只能是先 checkpoint、后原生 pause。** pause / resume 之后，原来的 checkpoint 全部作废（[§3.1](#31-原生-pause--resume-与-checkpoint-的代际边界)）；resume 之后可以重新 checkpoint，那一代的第一次是新的全量根。
- **checkpoint / restore 之后可以放心做原生 pause**，resume 回来的内存是完整的。服务端会把 checkpoint、restore 过程中被清掉的脏页记录补进 pause 的导出里；
  记录不完整的少见情形下，它退回按"驻留过的页"多导出一些，只会多花时间，不会少导（机制见 [第 15 章 §7](15-native-cooperation-and-cost.md#7-checkpoint-之后做原生-pause-的正确性前提)）。
- **原生 snapshot 的产物存在哪里取决于部署。** 单机部署把它和模板写在本机，宿主重启后是否还在，见部署文档
  [`../../deploy-docs/10-模板与快照存储位置梳理.md`](../../deploy-docs/10-模板与快照存储位置梳理.md)。

## 6. 其他需要知道的边界

- **操作按沙箱串行**。同一个沙箱上的 checkpoint、restore、delete 在宿主上排队执行，不同沙箱之间互不排队。
  排队太久会被告知稍后重试。多个调用方共用一个沙箱时的行为见 [第 22 章](22-api-semantics.md)。
- **沙箱要活着**。checkpoint 和 restore 都要求沙箱处于运行状态。
- **不要直接动宿主上的 checkpoint 目录**。手工删除会打断后代的恢复链；删除一律走 `delete`。
  备份这个目录也保不住 checkpoint（[§3](#3-checkpoint-什么时候失效)）。
- **checkpoint ID 只在所属沙箱的这一代里有意义**，拿到别的沙箱上用是 `not_found`。
- **restore 失败的绝大多数情形下沙箱保持原状继续可用**；唯一的例外是"撕裂"，那时只能销毁重建。
  各种失败的含义与处理见 [第 22 章](22-api-semantics.md)。

## 7. 错误怎么到达调用方

checkpoint 的四个 RPC（checkpoint / restore / list / delete，SDK 方法名是 `create` / `restore` / `list` / `delete`）失败时，
服务端回一个 HTTP 错误状态，响应体是 JSON，三个字段：

- `code`：Connect 错误码，说这次调用**怎么结束**的；
- `reason`：说调用方**下一步该做什么**；
- `message`：给人读的说明，SDK 把它作为异常文本原样带出。

同一个 `code` 下面可能是要求完全不同动作的几种失败（例如 `internal` 下有"guest 没回来"、"磁盘账本不可信"和一般失败），
所以 **SDK 先按 `reason` 选异常类**；服务端版本太老、没有 `reason` 时才退回按 `code` 选，那时只能分到更粗的类（[§8.2](#82-没有-reason-时老服务端)）。

每个 checkpoint 异常都带三个属性：`reason`（服务端给的原因；老服务端没给时取该类的默认值）、
`checkpoint_id`（这次调用点名的 checkpoint，没有则为 `None`）、`sandbox_id`。

## 8. 错误总表

### 8.1 按 `reason`

"出现在"一列的 checkpoint 指 SDK 的 `create`。

| `reason` | HTTP / Connect `code` | SDK 异常类 | 出现在 | 沙箱此时的状态 | 调用方该做什么 |
|---|---|---|---|---|---|
| `torn` | 500 / `data_loss` | `CheckpointTornException` | restore 在提交点之后失败（包括回滚调用在服务端时限内没有应答）；之后对这个沙箱的每次 checkpoint 与 restore 也都回这个 | **撕裂**：停在两个时刻之间，一部分在 checkpoint 那一刻、一部分在原地，无法判断落了多少。`list` 与 `delete` 仍可用 | **不可重试、不可恢复**。销毁这个沙箱，重新建一个 |
| `chain_broken` | 412 / `failed_precondition` | `CheckpointChainBrokenException` | restore | 沙箱在当前状态照常运行；是 checkpoint 树解析不了这次 restore（目标没有可用的磁盘视图、祖先缺失，或服务端丢过一段脏页记录） | **不要原样重试**。先做一次新的 checkpoint（它会以全量起一棵新树根），之后再 restore |
| `rootfs_poisoned` | 500 / `internal` | `CheckpointRootfsPoisonedException` | checkpoint | 沙箱照常运行；磁盘层的账本不再可信，之后的 checkpoint 一律被拒 | **restore 到任意一个已有的 checkpoint**，账本随之重建，checkpoint 恢复可用 |
| `guest_unresponsive` | 500 / `internal` | `CheckpointGuestUnresponsiveException` | restore | 宿主一侧的回滚**已经完成**，虚机在目标时刻；是 guest 里的 envd 在服务端等待时间（45 s）内没有应答。**不是撕裂** | 不要当成"没回滚成"处理。此时 guest 暂时执行不了命令，由调用方按业务决定：稍后再试命令，或销毁重建 |
| `busy` | 503 / `unavailable`，带 `Retry-After: 1` | `CheckpointBusyException` | checkpoint / restore / delete | 无影响：同一沙箱上另一个 checkpoint 操作还没结束，本次排队超过了服务端的锁等待时限（默认 60 s），什么都没做 | **可重试**。等 `e.retry_after` 秒（服务端没给或给的是日期时为 `None`）再发 |
| `sandbox_restored` | 409 / `aborted` | `CheckpointInterruptedException` | **不是** checkpoint 接口自己的错误：是 restore 发生时，对同一沙箱的**其他**经代理进沙箱的调用（命令、文件读写等）收到的（[§11](#11-多个调用方与并发)） | 沙箱已回到 checkpoint 那一刻，沙箱里的服务没坏 | 重新连接、重试这次调用。它在被打断前是否已在 guest 里生效无从得知 —— 但回滚反正已经把 checkpoint 之后的一切撤销了 |
| `disk_full` | 507 / `resource_exhausted` | `CheckpointDiskFullException` | checkpoint / restore | 无影响：服务端在动手之前查产物盘余量，不足就拒绝；沙箱照常运行，已有 checkpoint 完好 | 这是**宿主**的问题：删本沙箱的 checkpoint 通常腾不出足够空间，要请运维清理产物盘（[第 26 章](26-troubleshooting.md)）。清理之后可重试；紧循环重试只会重复同一个拒绝 |
| `too_many_checkpoints` | 429 / `resource_exhausted` | `CheckpointTooManyException` | checkpoint | 无影响：本沙箱可见的 checkpoint 个数到了每沙箱上限，检查在写任何东西之前 | 调用方自己能解决：`list()` → `delete()` 掉任意一个不再需要的 → 再 checkpoint |
| `checkpoint_bytes_limit` | 429 / `resource_exhausted` | `CheckpointBytesLimitException`（`CheckpointTooManyException` 的子类） | checkpoint | 无影响：本沙箱全部 checkpoint 的占盘到了每沙箱字节上限，检查在写任何东西之前。**restore 从不被这个上限拒绝** | 删**最旧**的 checkpoint（或 restore 到较早的点再删掉之后那一支）；删最新的那个几乎不释放空间（[第 22 章 §4.4](22-api-semantics.md#44-在字节上限下删哪个才能释放空间)）。异常文本里写明了当前占用、上限、删了没用的那个 checkpoint ID 和下限 |
| `internal` | 500 / `internal` | `CheckpointException`（基类） | checkpoint / restore / delete | 服务端没有更具体的说法；checkpoint 失败时虚机已恢复运行，restore 在提交点之前失败时沙箱保持原状态（错误文本会写明），delete 回它时什么都没删（下面的补充） | 可以稍后重试或换一个目标；持续出现时记录 `str(e)` 与 `e.reason`，请运维查 orchestrator 日志 |
| `not_found` | 404 / `not_found` | `NotFoundException`（SDK 既有异常） | 全部四个 | 沙箱或 checkpoint 不存在。也包括：排队期间沙箱被删除、沙箱经原生 pause / resume 换了一代（旧 checkpoint 作废，[第 22 章 §3.1](22-api-semantics.md#31-原生-pause--resume-与-checkpoint-的代际边界)）、拿别的沙箱的 checkpoint ID 来用 | 核对 ID 与沙箱；换代之后要重新 checkpoint |
| `unauthenticated` | 401 / `unauthenticated` | `AuthenticationException`（SDK 既有异常） | 全部四个 | —— | checkpoint 接口用沙箱自己的 traffic access token 鉴权；用 `Sandbox.create` / `Sandbox.connect` 拿到的对象会自动带上。确认覆盖层是当前版本（[第 21 章](21-sdk-quickstart.md)） |
| `invalid_argument` | 400 / `invalid_argument` | `InvalidArgumentException`（SDK 既有异常） | checkpoint / restore / delete | —— | 修正请求（例如请求体解析不了，或 `checkpoint_id` 为空、格式不对） |

补充：

- 未知的接口路径回 404，`code` 是 `unimplemented`，SDK 把它报成通用的 `SandboxException`；只有覆盖层与服务端版本不配时才会遇到。
- `delete` 按实际发生了什么作答（`service.go:1451` 调 `deleteFailure`，`:1482`）：
  - **checkpoint 不存在** → 404 `not_found`，SDK 抛 `NotFoundException`。包括没有这个 ID、已经删过（重发的 delete 就落在这里）、已被隐藏、
    是上一代沙箱的 ID；错误文本仍是 `checkpoint <id> not found`。服务端按 `ErrCheckpointNotFound` 判定，不靠匹配字符串。
  - **删除已生效、只是清理文件失败** → 按**成功**返回（`delete()` 返回 `True`）。账本已经移除或隐藏了这个条目，它已从 `list()` 里消失、不能再作 restore 目标、
    也不再计入配额，只是之后删它的目录或重写它的 manifest 失败了（`store.go:2027-2033`，`*DeleteCleanupError`）。服务端打一条 WARN
    `deleted checkpoint but could not remove all of its files; the leftovers are reclaimed when the sandbox is removed`；
    残留在该沙箱目录下，随沙箱删除时整棵目录一起回收；同一沙箱 ID 的新一代接管、orchestrator 下次启动清空 store 根时也会回收。
  - **其他错误** → 500 `internal`（`reason` 为 `internal`），SDK 抛基类 `CheckpointException`。这类失败发生在账本改动之前，什么都没删，可以重试。
- `AuthenticationException` 不是 `SandboxException` 的子类（SDK 原有设计），`except SandboxException` 接不住它。
- `disk_full`、`too_many_checkpoints`、`checkpoint_bytes_limit` 三种拒绝会在服务端各打一条 WARN
  `refused a checkpoint operation`，带 `reason` 和判定用的数字，运维据此区分是哪一个限额。
  限额怎么配见 [第 24 章](24-configuration-and-capacity.md)，日志字段见 [第 25 章](25-observability.md)。

### 8.2 没有 `reason` 时（老服务端）

| Connect `code` | SDK 异常类 |
|---|---|
| `data_loss` | `CheckpointTornException` |
| `failed_precondition` | `CheckpointChainBrokenException` |
| `aborted` | `CheckpointInterruptedException` |
| `internal` | `CheckpointException`（基类；分不出 guest 没回来、账本污染与一般失败） |
| `resource_exhausted` | `CheckpointException`（基类；分不出产物盘满和本沙箱超限，两者要找不同的人处理） |
| `unavailable` | `TimeoutException`（SDK 对这个 code 的通用解释） |
| `not_found` / `unauthenticated` / `invalid_argument` | `NotFoundException` / `AuthenticationException` / `InvalidArgumentException` |

## 9. 异常类层次

基类加 9 个子类，共 10 个，都能从 `e2b` 顶层导入：

```
SandboxException
└── CheckpointException                         基类：没有更具体说法的失败
    ├── CheckpointTornException                 torn
    ├── CheckpointChainBrokenException          chain_broken
    ├── CheckpointRootfsPoisonedException       rootfs_poisoned
    ├── CheckpointGuestUnresponsiveException    guest_unresponsive
    ├── CheckpointBusyException                 busy（多一个属性 retry_after）
    ├── CheckpointInterruptedException          sandbox_restored
    ├── CheckpointDiskFullException             disk_full
    └── CheckpointTooManyException              too_many_checkpoints
        └── CheckpointBytesLimitException       checkpoint_bytes_limit
```

- `CheckpointBytesLimitException` 是 `CheckpointTooManyException` 的**子类**：两个上限的解法都是"删了再来"，
  所以按个数上限写的"删掉旧的再重试"代码不改也能接住字节上限；它单独成类，是为了让调用方和运维分清是哪个上限拒绝的，
  也因为字节上限下**删哪个**很重要（删最旧的，不要删最新的）。
- `except CheckpointException` 接得住全部 checkpoint 专属失败；`except SandboxException` 的老代码也接得住，只是分不出该做什么。
- `NotFoundException`、`AuthenticationException`、`InvalidArgumentException` 沿用 SDK 原有的类，不在这棵树下。

推荐的处理骨架：

```python
import time
from e2b import (
    CheckpointBusyException,
    CheckpointBytesLimitException,
    CheckpointTooManyException,
)


def checkpoint_with_retry(sbx, name=None, attempts=5):
    for _ in range(attempts):
        try:
            return sbx.checkpoint.create(name=name)
        except CheckpointBusyException as e:            # 503：别人正在操作这个沙箱，等一下再来
            time.sleep(e.retry_after or 1)
        except CheckpointBytesLimitException:           # 429：占盘到上限，删最旧的
            oldest = sbx.checkpoint.list()[0]
            sbx.checkpoint.delete(oldest.checkpoint_id)
        except CheckpointTooManyException:              # 429：个数到上限，删任意一个不要的
            oldest = sbx.checkpoint.list()[0]
            sbx.checkpoint.delete(oldest.checkpoint_id)
    raise RuntimeError("checkpoint 重试多次仍未成功")

# 下面几种不要自动重试：
#   CheckpointTornException            → 销毁沙箱重建
#   CheckpointRootfsPoisonedException  → 先 restore 一个已有的 checkpoint
#   CheckpointDiskFullException        → 请运维清理产物盘
#   CheckpointChainBrokenException     → 先 checkpoint 一次，再 restore
#   其余 CheckpointException           → 记录 str(e) 与 e.reason 后上报
```

`except` 的顺序要**子类在前**：`CheckpointBytesLimitException` 必须写在 `CheckpointTooManyException` 之前，否则永远走不到。

## 10. 超时与不重放

### 10.1 默认超时

| 方法 | 默认客户端超时 | 服务端一侧可能花掉的时间 |
|---|---|---|
| `create` | **300 s** | 在同沙箱的操作锁上排队（默认最多 60 s），加上暂停、写快照、恢复运行；单次 Firecracker 调用的服务端时限默认 2 min |
| `restore` | **300 s** | 排队（最多 60 s），加上回滚本身，再加上回滚后**最多 45 s** 等 guest 里的 envd 应答 |
| `delete` | **300 s** | 不碰虚机，但与 checkpoint / restore 抢同一把锁，可能排在它们后面（最多 60 s） |
| `list` | 60 s | 不取锁 |
| `is_available` / `is_running` | 60 s | —— |

- 每次调用显式传的 `request_timeout=`（单位秒）优先；
- 连接级的 `request_timeout`（`Sandbox.create(request_timeout=...)` 那个）**故意不继承**到 `create` / `restore` / `delete` 这三个方法上 ——
  它是为 envd 的普通请求调的，默认 60 s，比服务端光是报出一个失败所需的时间还短；
- **不要把这三个操作的超时调到 60 s 以下**。服务端排队 60 s 后会回可重试的 `busy`，客户端若也是 60 s，就会先超时，
  把一个本可以重试的应答变成一个什么都说明不了的超时。运维调大服务端的锁等待时限时，客户端超时要跟着调大。

### 10.2 服务端不取消

客户端超时或断线只说明调用方没有收到完整结果。create与restore使用独立于请求取消的服务端上下文，可能继续执行，也可能尚未完成或已返回错误；客户端不能仅凭超时判断成功或失败。

| 操作 | 结果不确定时的核对方法与限制 |
|---|---|
| create | 对照调用前后的`list()`、应用记录及服务端完成日志寻找新条目。`list`不排队，暂时没有新条目不能证明操作已结束；当前协议没有客户端幂等键，直接重发可能再生成一个checkpoint |
| restore | `list()`只列保存的checkpoint，不报告当前恢复目标，也不等待restore结束。需结合服务端恢复完成/失败记录及业务状态判据核实；仅看到目标仍在列表中不能证明已恢复 |
| delete | 对照条目可见性和服务端记录；条目不可见不等于物理数据已全部回收，重发还可能返回`not_found` |

结果尚不明确时避免叠加新的变更请求。取得服务端完成状态后，再根据业务语义决定是否继续或重发；本接口没有把一次超时请求自动转换成可安全重试的事务。

### 10.3 四个 RPC 不自动重发

SDK 对 envd 的普通调用（命令、文件）在"对端中途断开连接"时会自动重发，那是给幂等调用准备的。
checkpoint 的四个 RPC 一律**不重发**（传输层重试次数为 0），以避免在结果不确定时重复提交变更：

- 重发的 checkpoint 会多做一个调用方永远不知道的 checkpoint；
- 重发的 restore 会把 guest 再回滚一次；
- 重发的 delete 会得到 `not_found`。

连接中途断开时，SDK把错误交给调用方。`list`可以用于读取已保存条目；create、restore、delete的结果核对分别遵循上节约束，不能统一用一次`list`推断操作已经结束。

## 11. 多个调用方与并发

### 11.1 同一个沙箱：操作串行

同一个沙箱上的 checkpoint、restore、delete 在宿主上**串行**执行（`list` 不排队）。
第二个调用会等第一个做完；等待超过服务端的锁等待时限（默认 60 s）就回 `busy`，调用方按 `retry_after` 重试即可。
不同沙箱之间不互相排队。

### 11.2 restore 对在途调用的影响

restore 一开始就会丢掉进这个沙箱的所有在途连接（沙箱马上要回到过去，在途响应的后半截已经没有意义），并在回滚期间拒绝新请求。
一个调用方在跑命令、另一个调用方对同一沙箱发起 restore 时，命令调用的下场取决于它走到了哪一步：

| 调用所处的阶段 | 调用方看到 |
|---|---|
| restore 进行中才到达的请求 | 立即收到 409，SDK 抛 `CheckpointInterruptedException` |
| restore 开始前发出、响应头**还没**写出的请求（普通的一问一答调用基本都是这种） | 409，SDK 抛 `CheckpointInterruptedException` |
| 响应头（200）**已经**写出、正在流式回数据的调用（例如 `commands.run` 带 `on_stdout`、长时间运行的命令） | 只能从中间被切断：传输层错误，如 `RemoteProtocolError: incomplete chunked read`、`unexpected EOF`、`ReadError` |
| restore **返回之后**才发出的请求 | 不受影响 |

最后一类的差别不是实现上的取舍，而是 HTTP 本身的限制：状态码只能在响应头里说一次，头一旦发出，
服务端就再没有办法把这次调用改判成 409，只剩切断连接这一种表达方式。

对调用方意味着：

- **只有"多个调用方同时操作同一个沙箱"才会遇到**。单调用方串行使用（命令跑完再 restore）不会；
- 收到 `CheckpointInterruptedException`：重连重试即可；
- 流式调用中途收到传输层错误，并且能确认这期间确实有人对这个沙箱做了 restore：按"被回滚打断"处理，按需重发。
  SDK 目前不会替你把这种情形改判成 `CheckpointInterruptedException`；
- 最稳妥的做法是在业务层让同一个沙箱的 restore 与其他调用互斥。

### 11.3 两类流式截断，要分清

| | restore 造成的截断 | 与 restore 无关的偶发截断 |
|---|---|---|
| 什么时候出现 | 流式调用进行中，同一沙箱被 restore（[§11.2](#112-restore-对在途调用的影响)） | 没有任何 restore，流式命令偶尔在开头几毫秒被切断 |
| 性质 | **既定限制**，行为确定：只要撞上就会被截 | **缺陷，已修复** |
| 原因 | 响应头已经写出，只能切断连接 | 代理没有为 HTTP/1 请求开启 full duplex：handler 一写响应头，Go 标准库就把请求体关掉，正在转发这个请求的上游连接随之被判为已死、被关闭 |
| 现状 | 保留；按 [§11.2](#112-restore-对在途调用的影响) 处理 | 代理 handler 已开启 full duplex（提交 `baae30b`）。orchestrator 的沙箱代理与 client-proxy 共用这份代理代码，两者都已修复 |

如果在**没有 restore** 的情况下仍然看到偶发的流式截断，先请运维确认部署的 orchestrator 和 client-proxy 都已包含上述修复，
排查方法见 [第 26 章](26-troubleshooting.md)；orchestrator 的代理连接日志开关见 [第 25 章](25-observability.md)。
