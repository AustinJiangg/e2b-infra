# 02 · 语义与边界

> **这篇给谁看**：用 checkpoint / restore 写业务的客户开发者。
> **读完能做什么**：说清一次 restore 把什么带回过去、什么带不回；知道 checkpoint 在什么情况下失效；
> 在有配额的环境里知道该删哪个 checkpoint 才能腾出空间；知道它和原生 snapshot（pause / resume）怎么一起用。
> 接口与示例见 [01](01-quickstart.md)，错误与并发见 [03](03-errors-timeouts-concurrency.md)。

---

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

checkpoint 期间业务能感到的只有一次短暂停顿（冻结窗口）；restore 同样只停一次。量级见 [04](04-performance-expectations.md)。

---

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
  两种情形的区别见 [03](03-errors-timeouts-concurrency.md)；
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
  如果 envd 始终不应答（restore 以 guest 失联报错，见 [03](03-errors-timeouts-concurrency.md)），墙钟就没有被校正。

对业务代码的影响：

- 不要用单调时钟测量跨越 restore 的间隔，得到的值没有意义；
- 依赖"时间一直向前"的租约、缓存过期、超时判断，restore 之后可能要重新校准；
- 验收或业务里的"活性判据"用**计数器在增长**，不要用"时间戳在推进"或"tick 速率正常"。

### 2.4 随机数与"被回滚"这件事本身

如果 guest 带有 VMGenID 设备，restore 会在写回内存之后刷新它的代号，guest 内核据此知道自己被从快照恢复过、重新播种随机数。
下面这点是内存回滚的直接推论：用户态程序自己缓存的随机状态（例如进程里已经初始化好的 PRNG）就在 guest 内存里，
restore 把它原样带回 checkpoint 时刻，VMGenID 也不会替它重新播种；所以从同一个 checkpoint 出发的两次"不同"执行，
可能产生相同的随机序列。对随机性敏感的程序应当在 restore 之后自行重新初始化。

---

## 3. checkpoint 什么时候失效

checkpoint 存在**宿主本地**，只在所属沙箱的当前这一代里有效。它不进对象存储，不跟随沙箱迁移，
也没有加载回来的机制 —— 即使把目录完整拷走也恢复不了（原因见 [22](22-lifecycle-reasoning.md)）。

| 事件 | 结果 |
|---|---|
| 沙箱被删除、超时被回收 | 该沙箱的全部 checkpoint 随之删除 |
| orchestrator 重启 | 同上。重启本来就会带走这台机器上的所有沙箱；上一个进程留在盘上的 checkpoint 文件在启动时被清空 |
| 宿主重启或宕机 | 同上 |
| 沙箱迁移到别的节点 | checkpoint **不跟随** |
| 沙箱经原生 `pause` 再 resume（`connect`） | pause 之前的 checkpoint **全部作废**；resume 之后是新的一代，可以照常重新 checkpoint / restore（§3.1） |
| 单个 checkpoint 被 `delete` | 从接口上消失；数据由服务端按需保留、合并（compact / fold）或回收（§4） |

### 3.1 原生 pause / resume 与 checkpoint 的代际边界

**规则**：原生 pause 会让该沙箱的全部 checkpoint 作废；resume 之后，同一个沙箱 ID 底下是"新的一代"，从零开始。

| 时刻 | `checkpoint.list()` | restore 一个 pause 之前的 checkpoint | 新建 checkpoint |
|---|---|---|---|
| pause 之前 | 这一代的全部条目 | 正常 | 正常 |
| resume 之后 | **空** | **`NotFoundException`**（`not_found`） | **正常**；这一代的第一次是新的全量根 |

原因：restore 是往**活着的那个 Firecracker 进程**里原地写回，而原生 resume 是用同一个沙箱 ID 新起一个 Firecracker 进程。
旧那一代的差分已经没有可以写回的对象了，保留它们只会给调用方一个必定失败的 ID。
所以服务端按"代"（每个 Firecracker 进程一代）而不是按沙箱 ID 管理 checkpoint。

---

## 4. 删除与空间回收：用户视角

这一节只讲调用方需要知道的规则和该怎么做；删除、隐藏、合并的机制与正确性证明见 [14](14-memory-diff-tree.md)，
相关开关与容量规划见 [06](06-configuration-and-capacity.md)。

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
- 占用的空间与恢复耗时都不会随运行时间持续增长。

如果运维关掉了合并，这种用法会让隐藏的 checkpoint 越积越多，占盘和 restore 耗时随时间上涨。
同样的积累也发生在宿主不跟踪脏页的时候（每个 checkpoint 都是全量根，没有可合并的节点）。

### 4.3 两种每沙箱上限

运维可以给每个沙箱设两种上限（默认都不设），被拒时的异常与状态码见 [03](03-errors-timeouts-concurrency.md)：

| 上限 | 数什么 | 被拒时怎么办 |
|---|---|---|
| **个数上限** | 可见的 checkpoint 个数（隐藏的不算，调用方看不见也删不掉） | 删掉任意一个不再需要的，再 checkpoint |
| **字节上限** | 这个沙箱的全部 checkpoint 实际占用的盘（包括隐藏的、共享的磁盘层） | **删哪个很重要**，见 §4.4 |

两种上限都只拦 checkpoint，**从不拦 restore**；拒绝发生在写任何东西之前，沙箱照常运行、已有的 checkpoint 完好。
字节上限只要当前还低于上限就放行，所以实际占用可能超出上限一个 checkpoint 的量。

### 4.4 在字节上限下，删哪个才能释放空间

| 做法 | 能腾出多少 |
|---|---|
| 删**最新**的那个（也就是沙箱当前所处的点） | **几乎为零**：它是沙箱正在运行的基准，只能隐藏，只释放它的快照描述文件 |
| 删**最旧**的（或较旧的中间节点） | 它会被合并进下一个 checkpoint，释放两者**重叠**的那部分页：两次改的是同一批页，就能腾出一份；两次改的页完全不相交，合并后几乎腾不出空间 |
| restore 到一个较早的 checkpoint，再删掉它之后的那一整支 | 被删的分支没有后代、也不是当前点，直接删除，**释放最彻底** |

被拒时服务端的错误信息会写明：当前占用、上限、沙箱当前所处的 checkpoint ID（删它没用），
以及这个沙箱无论怎么删都至少要占的量（约一份 guest 内存的全量镜像）。

一个沙箱在运行期间至少要保留一份全量内存镜像（它的树根，或合并后成为树根的那个 checkpoint）。
因此上限设得比"一份 guest 内存加少量开销"还小，沙箱一旦顶到上限就再也删不回来；
要在最坏负载下还能保留一个更早的恢复点，上限应不低于"两份 guest 内存加少量开销"。
服务端在沙箱第一次 checkpoint 时发现上限过小会打日志告警，推荐值与推导见 [06](06-configuration-and-capacity.md)。

---

## 5. 与原生 snapshot（pause / resume）怎么配合

两者是分工关系，**不互相替代**。与原生 snapshot 的逐项对比见 [11](11-baseline-goals-and-native.md)。

| 需求 | 用哪个 |
|---|---|
| 任务执行中的快速回退、试错、在几个方案之间来回跳 | **checkpoint / restore** |
| 沙箱要离场，之后还想回来 | 原生 snapshot |
| 跨节点迁移、进程崩溃后恢复 | 原生 snapshot |
| 跨会话、长期保存状态 | 原生 snapshot |

一句话：**沙箱活着时的回退用 checkpoint，沙箱离场用原生 snapshot。**

两者可以串起来用：先用 checkpoint / restore 把沙箱精确定位到想要的那一刻，再对这个状态做一次原生 pause 固化下来。
一个 Agent 工作流的样子：

```
① 沙箱起来
② checkpoint「干净环境」                ← 第一次，全量根
③ 装依赖 → checkpoint「依赖装好」        ← 增量
④ 改配置 → 跑构建 → 失败
⑤ restore 到「依赖装好」                 ← 沙箱不中断，不用重装依赖
⑥ 换个改法 → 跑构建 → 成功 → checkpoint「构建通过」   ← 从③分出的新分支
⑦ 跑测试 → 失败 → restore 到「构建通过」
⑧ 任务完成，对最终状态做一次原生 pause   ← 跨会话保留
⑨ 之后 resume：②③⑥ 的 checkpoint 已作废，⑧ 的状态还在
```

注意方向：checkpoint → 原生 pause 可以，pause / resume 之后原来的 checkpoint 就作废了（§3.1）。

原生 snapshot 的产物存在哪里取决于部署。单机部署把它和模板写在本机，宿主重启后是否还在，见部署文档
[`../../deploy-docs/10-模板与快照存储位置梳理.md`](../../deploy-docs/10-模板与快照存储位置梳理.md)。

### 5.1 checkpoint / restore 之后再原生 pause：正确性前提

原生 pause 导出的内存差分是相对模板内存文件的，必须包含 guest **自启动以来**写过的每一页。
而 checkpoint 和 restore 都会让 Firecracker 清零它的写跟踪记录，restore 还会绕开写跟踪直接把回滚集写进 guest 内存。
如果 pause 只看 Firecracker 当下的记录，打过 checkpoint 的沙箱就会漏导出较早写过的页，resume 之后 guest 内存由两个时刻拼成、
内核崩溃，而 pause 本身却报告成功。

服务端的做法是：把每次 checkpoint、restore 时 Firecracker 即将清掉的那几份脏页记录并进一个每虚机的累积集合，
pause 时一并导出。任何一份并不进去时，整个累积集合标为不可信，pause 退回按"内存里驻留过的页"导出 ——
导多了只多花时间，不会导少。从不做 checkpoint 的沙箱不受影响。

对调用方意味着：**checkpoint / restore 之后可以放心做原生 pause**，resume 回来的内存是完整的；
只有在退回驻留判据的那种少见情形下，pause 才会比平常多导出一些页、多花一些时间。机制细节见 [14](14-memory-diff-tree.md)。

---

## 6. 其他需要知道的边界

- **操作按沙箱串行**。同一个沙箱上的 checkpoint、restore、delete 在宿主上排队执行，不同沙箱之间互不排队。
  排队太久会被告知稍后重试。多个调用方共用一个沙箱时的行为见 [03](03-errors-timeouts-concurrency.md)。
- **沙箱要活着**。checkpoint 和 restore 都要求沙箱处于运行状态。
- **不要直接动宿主上的 checkpoint 目录**。手工删除会打断后代的恢复链；删除一律走 `delete`。
  备份这个目录也保不住 checkpoint（§3）。
- **checkpoint ID 只在所属沙箱的这一代里有意义**，拿到别的沙箱上用是 `not_found`。
- **restore 失败的绝大多数情形下沙箱保持原状继续可用**；唯一的例外是"撕裂"，那时只能销毁重建。
  各种失败的含义与处理见 [03](03-errors-timeouts-concurrency.md)。
