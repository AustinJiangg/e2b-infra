# 25 · 错误、超时与并发调用

## 本章目标

读完本章，你应该能：

1. 拿到任何一个 checkpoint 相关的错误，按 `reason` 查到对应的异常类、沙箱此刻的状态和下一步该做什么；
2. 写出按异常类分别处置的调用代码：哪些可以等一下重试、哪些要删了再来、哪些只能销毁重建；
3. 说清 SDK 的默认超时，以及为什么三个写操作不继承连接级超时、为什么四个 RPC 都不自动重发，超时或断线后该先做什么；
4. 知道多个调用方同时操作一个沙箱时在途调用的下场，分清"被 restore 打断"和"与 restore 无关的偶发截断"。

上一章（[24](24-semantics-and-limits.md)）讲了成功时 restore 做了什么。本章讲失败时怎么办：错误怎么到达调用方、每种错误意味着沙箱处于什么状态、超时与重发的约定，
以及多个调用方同时操作一个沙箱时会发生什么。本章面向要把 checkpoint / restore 接进生产代码的开发者，排障时要对照错误含义的运维也用它。
**本章是全书错误表的唯一定义处**，其他章只链接过来。下一章（[26](26-deployment-prerequisites.md)）起面向部署与运维。

---

## 1. 错误怎么到达调用方

checkpoint 的四个 RPC（checkpoint / restore / list / delete，SDK 方法名是 `create` / `restore` / `list` / `delete`）失败时，
服务端回一个 HTTP 错误状态，响应体是 JSON，三个字段：

- `code`：Connect 错误码，说这次调用**怎么结束**的；
- `reason`：说调用方**下一步该做什么**；
- `message`：给人读的说明，SDK 把它作为异常文本原样带出。

同一个 `code` 下面可能是要求完全不同动作的几种失败（例如 `internal` 下有"guest 没回来"、"磁盘账本不可信"和一般失败），
所以 **SDK 先按 `reason` 选异常类**；服务端版本太老、没有 `reason` 时才退回按 `code` 选，那时只能分到更粗的类（§2.2）。

每个 checkpoint 异常都带三个属性：`reason`（服务端给的原因；老服务端没给时取该类的默认值）、
`checkpoint_id`（这次调用点名的 checkpoint，没有则为 `None`）、`sandbox_id`。

---

## 2. 错误总表

### 2.1 按 `reason`

"出现在"一列的 checkpoint 指 SDK 的 `create`。

| `reason` | HTTP / Connect `code` | SDK 异常类 | 出现在 | 沙箱此时的状态 | 调用方该做什么 |
|---|---|---|---|---|---|
| `torn` | 500 / `data_loss` | `CheckpointTornException` | restore 在提交点之后失败（包括回滚调用在服务端时限内没有应答）；之后对这个沙箱的每次 checkpoint 与 restore 也都回这个 | **撕裂**：停在两个时刻之间，一部分在 checkpoint 那一刻、一部分在原地，无法判断落了多少。`list` 与 `delete` 仍可用 | **不可重试、不可恢复**。销毁这个沙箱，重新建一个 |
| `chain_broken` | 412 / `failed_precondition` | `CheckpointChainBrokenException` | restore | 沙箱在当前状态照常运行；是 checkpoint 树解析不了这次 restore（目标没有可用的磁盘视图、祖先缺失，或服务端丢过一段脏页记录） | **不要原样重试**。先做一次新的 checkpoint（它会以全量起一棵新树根），之后再 restore |
| `rootfs_poisoned` | 500 / `internal` | `CheckpointRootfsPoisonedException` | checkpoint | 沙箱照常运行；磁盘层的账本不再可信，之后的 checkpoint 一律被拒 | **restore 到任意一个已有的 checkpoint**，账本随之重建，checkpoint 恢复可用 |
| `guest_unresponsive` | 500 / `internal` | `CheckpointGuestUnresponsiveException` | restore | 宿主一侧的回滚**已经完成**，虚机在目标时刻；是 guest 里的 envd 在服务端等待时间（45 s）内没有应答。**不是撕裂** | 不要当成"没回滚成"处理。此时 guest 暂时执行不了命令，由调用方按业务决定：稍后再试命令，或销毁重建 |
| `busy` | 503 / `unavailable`，带 `Retry-After: 1` | `CheckpointBusyException` | checkpoint / restore / delete | 无影响：同一沙箱上另一个 checkpoint 操作还没结束，本次排队超过了服务端的锁等待时限（默认 60 s），什么都没做 | **可重试**。等 `e.retry_after` 秒（服务端没给或给的是日期时为 `None`）再发 |
| `sandbox_restored` | 409 / `aborted` | `CheckpointInterruptedException` | **不是** checkpoint 接口自己的错误：是 restore 发生时，对同一沙箱的**其他**经代理进沙箱的调用（命令、文件读写等）收到的（§5） | 沙箱已回到 checkpoint 那一刻，沙箱里的服务没坏 | 重新连接、重试这次调用。它在被打断前是否已在 guest 里生效无从得知 —— 但回滚反正已经把 checkpoint 之后的一切撤销了 |
| `disk_full` | 507 / `resource_exhausted` | `CheckpointDiskFullException` | checkpoint / restore | 无影响：服务端在动手之前查产物盘余量，不足就拒绝；沙箱照常运行，已有 checkpoint 完好 | 这是**宿主**的问题：删本沙箱的 checkpoint 通常腾不出足够空间，要请运维清理产物盘（[29](29-troubleshooting.md)）。清理之后可重试；紧循环重试只会重复同一个拒绝 |
| `too_many_checkpoints` | 429 / `resource_exhausted` | `CheckpointTooManyException` | checkpoint | 无影响：本沙箱可见的 checkpoint 个数到了每沙箱上限，检查在写任何东西之前 | 调用方自己能解决：`list()` → `delete()` 掉任意一个不再需要的 → 再 checkpoint |
| `checkpoint_bytes_limit` | 429 / `resource_exhausted` | `CheckpointBytesLimitException`（`CheckpointTooManyException` 的子类） | checkpoint | 无影响：本沙箱全部 checkpoint 的占盘到了每沙箱字节上限，检查在写任何东西之前。**restore 从不被这个上限拒绝** | 删**最旧**的 checkpoint（或 restore 到较早的点再删掉之后那一支）；删最新的那个几乎不释放空间（[24](24-semantics-and-limits.md) §4.4）。异常文本里写明了当前占用、上限、删了没用的那个 checkpoint ID 和下限 |
| `internal` | 500 / `internal` | `CheckpointException`（基类） | checkpoint / restore / delete | 服务端没有更具体的说法；checkpoint 失败时虚机已恢复运行，restore 在提交点之前失败时沙箱保持原状态（错误文本会写明），delete 回它时什么都没删（下面的补充） | 可以稍后重试或换一个目标；持续出现时记录 `str(e)` 与 `e.reason`，请运维查 orchestrator 日志 |
| `not_found` | 404 / `not_found` | `NotFoundException`（SDK 既有异常） | 全部四个 | 沙箱或 checkpoint 不存在。也包括：排队期间沙箱被删除、沙箱经原生 pause / resume 换了一代（旧 checkpoint 作废，[24](24-semantics-and-limits.md) §3.1）、拿别的沙箱的 checkpoint ID 来用 | 核对 ID 与沙箱；换代之后要重新 checkpoint |
| `unauthenticated` | 401 / `unauthenticated` | `AuthenticationException`（SDK 既有异常） | 全部四个 | —— | checkpoint 接口用沙箱自己的 traffic access token 鉴权；用 `Sandbox.create` / `Sandbox.connect` 拿到的对象会自动带上。确认覆盖层是当前版本（[23](23-quickstart.md) §1.3） |
| `invalid_argument` | 400 / `invalid_argument` | `InvalidArgumentException`（SDK 既有异常） | checkpoint / restore / delete | —— | 修正请求（例如请求体解析不了，或 `checkpoint_id` 为空、格式不对） |

补充：

- 未知的接口路径回 404，`code` 是 `unimplemented`，SDK 把它报成通用的 `SandboxException`；只有覆盖层与服务端版本不配时才会遇到。
- `delete` 按实际发生了什么作答（`service.go:1451` 调 `deleteFailure`，`:1482`）：
  - **checkpoint 不存在** → 404 `not_found`，SDK 抛 `NotFoundException`。包括没有这个 ID、已经删过（重发的 delete 就落在这里）、已被隐藏、
    是上一代沙箱的 ID；错误文本仍是 `checkpoint <id> not found`。服务端按 `ErrCheckpointNotFound` 判定，不靠匹配字符串。
  - **删除已生效、只是清理文件失败** → 按**成功**返回（`delete()` 返回 `True`）。账本已经移除或隐藏了这个条目，它已从 `list()` 里消失、不能再作 restore 目标、
    也不再计入配额，只是之后删它的目录或重写它的 manifest 失败了（`store.go:2022-2028`，`*DeleteCleanupError`）。服务端打一条 WARN
    `deleted checkpoint but could not remove all of its files; the leftovers are reclaimed when the sandbox is removed`；
    残留在该沙箱目录下，随沙箱删除时整棵目录一起回收；同一沙箱 ID 的新一代接管、orchestrator 下次启动清空 store 根时也会回收。
  - **其他错误** → 500 `internal`（`reason` 为 `internal`），SDK 抛基类 `CheckpointException`。这类失败发生在账本改动之前，什么都没删，可以重试。
- `AuthenticationException` 不是 `SandboxException` 的子类（SDK 原有设计），`except SandboxException` 接不住它。
- `disk_full`、`too_many_checkpoints`、`checkpoint_bytes_limit` 三种拒绝会在服务端各打一条 WARN
  `refused a checkpoint operation`，带 `reason` 和判定用的数字，运维据此区分是哪一个限额。
  限额怎么配见 [27](27-configuration-and-capacity.md)，日志字段见 [28](28-observability-reference.md)。

### 2.2 没有 `reason` 时（老服务端）

| Connect `code` | SDK 异常类 |
|---|---|
| `data_loss` | `CheckpointTornException` |
| `failed_precondition` | `CheckpointChainBrokenException` |
| `aborted` | `CheckpointInterruptedException` |
| `internal` | `CheckpointException`（基类；分不出 guest 没回来、账本污染与一般失败） |
| `resource_exhausted` | `CheckpointException`（基类；分不出产物盘满和本沙箱超限，两者要找不同的人处理） |
| `unavailable` | `TimeoutException`（SDK 对这个 code 的通用解释） |
| `not_found` / `unauthenticated` / `invalid_argument` | `NotFoundException` / `AuthenticationException` / `InvalidArgumentException` |

---

## 3. 异常类层次

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

---

## 4. 超时与不重放

### 4.1 默认超时

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

### 4.2 服务端不取消

客户端超时或断开，**只是放弃了一件服务端还在继续做的事**：checkpoint 与 restore 在服务端不会因为客户端走了而中止。

- checkpoint 超时：checkpoint 多半已经做成，只是 ID（由服务端生成）没送到你手里；
- restore 超时：沙箱可能已经回滚完了，也可能还在回滚中。

所以超时或断线之后，**先 `list()` 核对结果，再决定要不要重发**。

### 4.3 四个 RPC 不自动重发

SDK 对 envd 的普通调用（命令、文件）在"对端中途断开连接"时会自动重发，那是给幂等调用准备的。
checkpoint 的四个 RPC 一律**不重发**（传输层重试次数为 0），因为它们都不幂等：

- 重发的 checkpoint 会多做一个调用方永远不知道的 checkpoint；
- 重发的 restore 会把 guest 再回滚一次；
- 重发的 delete 会得到 `not_found`。

连接中途断开时，SDK 把错误直接交给调用方，由调用方先 `list()` 核对、再决定是否重发。

---

## 5. 多个调用方与并发

### 5.1 同一个沙箱：操作串行

同一个沙箱上的 checkpoint、restore、delete 在宿主上**串行**执行（`list` 不排队）。
第二个调用会等第一个做完；等待超过服务端的锁等待时限（默认 60 s）就回 `busy`，调用方按 `retry_after` 重试即可。
不同沙箱之间不互相排队。

### 5.2 restore 对在途调用的影响

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

### 5.3 两类流式截断，要分清

| | restore 造成的截断 | 与 restore 无关的偶发截断 |
|---|---|---|
| 什么时候出现 | 流式调用进行中，同一沙箱被 restore（§5.2） | 没有任何 restore，流式命令偶尔在开头几毫秒被切断 |
| 性质 | **既定限制**，行为确定：只要撞上就会被截 | **缺陷，已修复** |
| 原因 | 响应头已经写出，只能切断连接 | 代理没有为 HTTP/1 请求开启 full duplex：handler 一写响应头，Go 标准库就把请求体关掉，正在转发这个请求的上游连接随之被判为已死、被关闭 |
| 现状 | 保留；按 §5.2 处理 | 代理 handler 已开启 full duplex（提交 `baae30b`）。orchestrator 的沙箱代理与 client-proxy 共用这份代理代码，两者都已修复 |

如果在**没有 restore** 的情况下仍然看到偶发的流式截断，先请运维确认部署的 orchestrator 和 client-proxy 都已包含上述修复，
排查方法见 [29](29-troubleshooting.md)；orchestrator 的代理连接日志开关见 [28](28-observability-reference.md)。

---

## 本章要点

- 错误响应带 `code`、`reason`、`message` 三个字段；SDK 先按 `reason` 选异常类，因为 `reason` 回答的是"下一步该做什么"，同一个 `code` 下可能要做完全不同的事。
- 异常族是基类 `CheckpointException` 加 9 个子类；`CheckpointBytesLimitException` 是 `CheckpointTooManyException` 的子类，按个数上限写的处理代码不改也接得住。
- 不要自动重试的几类：撕裂（销毁重建）、账本污染（先 restore 一个已有的 checkpoint）、产物盘满（找运维）、断链（先 checkpoint 一次再 restore）。
- `create`、`restore`、`delete` 默认超时 300 s，不要调到 60 s 以下：服务端排队 60 s 后回可重试的 `busy`，客户端先超时就丢掉了这个信息。
- 客户端超时或断开不会让服务端中止；四个 RPC 都不幂等，所以一律不自动重发，超时或断线后先 `list()` 核对再决定。
- 同一沙箱的写操作串行、不同沙箱互不排队；restore 会切断在途调用：响应头还没写出的得到 409 `CheckpointInterruptedException`，已经在流式回数据的只能被切断。
- restore 造成的流截断是既定限制；没有 restore 时的偶发截断是已修复的缺陷，仍出现时先确认 orchestrator 与 client-proxy 都已更新。
