# 12 · 失败语义与 degraded

## 本章目标

读完本章，你应当能回答：

1. 一次 create、一次 restore 在每一步失败之后，沙箱分别处于什么状态；哪些失败"什么都没动"，哪些失败之后沙箱已经不在任何一个完整状态上？
2. gsd 返回的每一种 Connect 错误码是在哪一行代码产生的，SDK 把它变成哪个异常类，调用方拿到之后该做什么？
3. gsd 的三类 degraded 各由什么引起、各自还允许哪些操作、怎么解除？
4. 交接前的实现里有哪些"返回成功但其实没做"的静默降级，现在分别变成了什么？
5. 改这套代码时哪些不变量不能破坏，其中哪些被破坏之后不会报错？

---

第二部分讲完了正常路径：上一章（[11](11-end-to-end.md)）按时间顺序走完了一次 checkpoint 和一次 restore。
第三部分转向"出错时怎么办，以及怎么让调用方知道"。本章是第三部分的第一章，把 [09](09-checkpoint.md)、[10](10-restore.md) 两章沿途标出的失败出口收拢到同一个框架里：
先讲提交点，再给出全书唯一的一张错误表，然后是 degraded 状态、SDK 的异常映射和一份不变量清单。
状态文件怎么写、请求怎么排队、容量怎么预检是下一章（[13](13-state-concurrency-capacity.md)）的主题；面向排障的"看到某个报错该查哪里"在 [25](25-troubleshooting.md)。

---

## 1. 总纲：两条原则

gsd 操作的是一个正在运行的沙箱里的用户进程和根文件系统。它出错的方式里，最糟的不是报错，而是**返回成功但状态不对**：
调用方以为手里有一个 checkpoint，其实里面没有进程镜像；以为文件系统回到了某一刻，其实根本没切。这类错误不会立刻暴露，
等到几步之后进程行为异常时，已经没有任何线索能追回原因。所以整套错误处理围绕两条原则。

**第一条：做不到就拒绝，不降级成"部分成功"。** 一个请求要么完成它声称的全部事情，要么返回错误。
交接前的实现里有三处违反这一条的降级路径，现在都改成了明确的失败或明确的状态，见第 6 节。

**第二条：每一种失败之后，沙箱处于什么状态必须说得清，并且写进错误信息。** 调用方靠错误信息决定下一步，
所以"什么都没动"的失败在信息里写 `nothing was changed`，"已经动了、回不去"的失败单独用一个错误码（`data_loss`）并写明进程和文件系统各在什么状态。
能做到这一点，靠的是把每个操作里可能失败的步骤尽量排到不可逆步骤之前，这就是下一节的提交点。

---

## 2. 提交点：把失败切成两半

### 2.1 create 没有不可逆步骤

一次 create 依次做：扫描进程、预检、逐棵树 `criu dump`、沉下 upper、清 soft-dirty、放开进程、入池（[09](09-checkpoint.md)）。
这些步骤里没有一步是做了之后回不去的：dump 只读进程状态并把它留在停止状态，放开即可；沉下 upper 是一次改名加一次 ioctl，ioctl 失败时把名字改回去。
所以 create 的每一种失败都以"沙箱照常运行、checkpoint 列表里没有多出东西"收场。下表按失败位置列出 gsd 的收尾动作（行号见 `CreateCheckpoint`，`ckpt/service.go:163`）。

| 失败位置 | gsd 的收尾 | 失败后的状态 |
|---|---|---|
| degraded 检查（`checkReady`，`ckpt/service.go:677`） | 无 | 什么都没动 |
| 扫描 `/proc` 失败（`ckpt/service.go:183`） | 无 | 什么都没动 |
| 多树预检发现树间共享的管道或 socket（`ckpt/service.go:204`） | 无 | 什么都没动，进程没有被停过 |
| 容量预检不通过或读不到数字（`ckpt/service.go:212`、`:216`） | 无 | 什么都没动 |
| 建镜像目录失败（`ckpt/service.go:226`） | 无 | 什么都没动 |
| 某棵树 `criu dump` 报错或超时（`ckpt/service.go:268-284`） | `resumeAfterFailedDump` 放开所有"dump 前不是停止状态、现在停着"的进程；`discardFailedCheckpoint` 删掉整个镜像目录（含已经 dump 成功的树）并清空增量父镜像记录 | 进程照常运行，文件系统没动；**下一次 create 每棵树都是全量** |
| 沉下 upper 失败，包括层栈深度预检不通过（`ckpt/service.go:322-326`） | `SinkUpper` 自己删掉新建的目录、把 upper 改回原名；`discardFailedCheckpoint` 同上；延迟执行的 `resume` 放开进程 | 同上 |

表里有一处需要特别说明。层栈深度的预检写在 `SinkUpper` 里（`ckpt/overlay.go:152`、`:156`），而 `SinkUpper` 排在 dump 之后，
所以"层栈太深"这种失败发生时 dump 已经做完：进程被停过又被放开，soft-dirty 位已经被这次 dump 重置。
文件系统和 checkpoint 列表确实没有变化，但增量的起点丢了，下一次 create 是全量。SDK 给这类错误附加的提示语里写着 "Nothing was changed"，
对文件系统和进程是准确的，对"下一次 dump 的大小"不准确，读到时要知道这一点。

**为什么失败之后必须清空增量父镜像，而不是退回上一次的记录。** 增量 dump 只写"上一次 dump 以来改过的页"，判断依据是内核里的 soft-dirty 位，
而 `criu dump` 在写出页面之后会把这些位重置。dump 中途失败时，一部分任务的位可能已经被重置；沉下 upper 失败时，这次 dump 已经把所有位完整重置过一遍。
这时如果仍以更早的那份镜像为父，从更早那一刻到这次失败之间改过的页，在位图里已经看不出来，会被当成"父镜像里有、不必再写"，
之后 restore 出来的内存就是两个时刻的混合。这种错误不报错、无法察觉，所以代码选择无条件清空（`discardFailedCheckpoint`，`ckpt/service.go:825`），代价只是下一次多做一次全量。
全量 dump 不依赖任何历史，总是安全的。

### 2.2 restore 的提交点是"杀进程"

restore 有一个不可逆步骤：杀掉当前全部用户进程。进程一旦被杀，它们在内存里的状态就没有了，除非之后的 `criu restore` 成功，否则没有任何办法把它们找回来。
`RestoreCheckpoint`（`ckpt/service.go:463`）因此分成两段，中间以杀进程为界。

**第一段，杀进程之前。** 这一段做完所有能提前做的检查和准备，任何一步失败都直接返回，沙箱原样：

| 步骤 | 失败时 | 位置 |
|---|---|---|
| degraded 检查 | 返回错误 | `checkReady`，`ckpt/service.go:677` |
| 在池里找目标 checkpoint | `not_found` | `ckpt/service.go:477` |
| 每棵树的 `inventory.img` 可读，沿 `parent` 符号链接往上每一级父镜像都在 | `failed_precondition`，信息含 `nothing was changed` | `checkImages`，`ckpt/service.go:703` |
| 预检选项串长度、检查目标层目录存在、建好这次 restore 要用的 upper 和 work 目录 | 错误信息含 `prepare overlay restore, nothing was changed` | `PrepareRestore`，`ckpt/overlay.go:203` |
| 扫描当前用户进程 | 放弃准备好的目录（`AbortRestore`），返回 `scan user processes, nothing was changed` | `ckpt/service.go:506-512` |

把建目录这种"几乎不会失败"的事也挪到杀进程之前，是因为它在根盘写满时确实会失败（ENOSPC 压测的若干档正是停在这一步，见 [20](20-results-and-gaps.md)），
而那时候如果进程已经杀了，一次本可以原样返回的失败就变成了数据丢失。

**第二段，杀进程之后。** 从 `killTrees` 开始，后面只剩两步：切换层栈（`CommitRestore`）和逐棵 `criu restore`。
这两步任何一步失败，都由 `restoreFailed`（`ckpt/service.go:635`）统一处理：记下 degraded 原因、返回 `data_loss`，错误信息的开头固定是

```
restore of <id> failed after user processes were killed; filesystem at <old|target> state: <原因>
```

其中 `old` 表示层栈还没切过去，文件系统仍是 restore 之前的内容；`target` 表示层栈已经切到目标 checkpoint。两种情况下用户进程都已经不在了。

| 失败位置 | 文件系统 | 进程 | 位置 |
|---|---|---|---|
| 切换层栈的 ioctl 失败 | `old` | 全部被杀 | `ckpt/service.go:545` |
| 单树：`criu restore` 报错或超时 | `target` | 全部被杀，没有恢复出任何进程 | `ckpt/service.go:594` |
| 多树：第 i 棵 `criu restore` 报错或超时 | `target` | 前 i 棵已恢复并继续运行，第 i 棵失败，其余没有尝试；错误信息里逐棵列出 | `ckpt/service.go:603` |

第二段里有一个例外分支：切换层栈失败、而此前没有杀掉任何进程（沙箱里本来就没有用户进程）时，沙箱确实还是原样，
这时不算数据丢失，按普通错误返回并写明 `nothing was changed`（`ckpt/service.go:541-544`）。

有三点容易被忽略：

- **restore 阶段的 criu 超时也是 `data_loss`，不是 `deadline_exceeded`。** `deadline_exceeded` 只会从 create 出来。restore 时 criu 超时意味着进程已经杀了而恢复没有完成，
  性质上与 criu 报错没有区别，都走 `restoreFailed`。
- **`killTrees` 自己报的错只记日志，不中断 restore。** 例如给某个进程发信号失败，或者进程树在十轮冻结之后仍在长大（`ckpt/service.go:525-527`）。
  这时继续往下走是有意的：进程多半已经杀掉了一部分，停在这里只会更糟；如果确实有进程没杀干净占着 PID，后面的 `criu restore` 会失败并报出来。
- **多树 restore 不是原子的。** 中间一棵失败时，前面已经恢复的树留着继续跑（它们在目标文件系统上按 checkpoint 时的状态运行），沙箱不是任何一个 checkpoint 的完整状态。
  为什么不把已恢复的树杀掉凑成"全无"：那样既没有让沙箱更接近某个完整状态，又多毁掉一些可能对排查有用的现场；下一次 restore 开头反正会把当前用户进程全部杀掉。

### 2.3 客户端超时或断开不是一次失败

criu 运行时用的是脱离了请求的超时上下文（`context.WithoutCancel`，`ckpt/service.go:237`、`:566`）：客户端断开连接、SDK 侧请求超时，都不会让 gsd 半途杀掉 criu。
原因很直接，dump 到一半被杀的 criu 可能把进程留在停止状态，restore 到一半被杀则直接落进 `data_loss`，哪一种都比"让它做完"糟。

这带来一个调用方必须知道的后果：**SDK 抛出请求超时，并不表示操作没有发生。** gsd 那边会把这次 create 或 restore 做完并落盘。
调用方在客户端超时之后应当用 `list()` 核对结果，而不是假定失败后直接重试；重试一次 create 的代价是多出一个 checkpoint，重试一次 restore 则是把沙箱再回滚一遍。
单测 `TestCreateCheckpointIgnoresRequestCancellation`（`ckpt/service_failure_test.go:194`）固定了这个行为。

---

## 3. 错误表（全书唯一）

下表是 gsd 四个 RPC 可能返回的全部错误。别的章需要引用错误时链接到这里，不另列。

读表之前先说明三列的含义。"Connect 码"是线协议上的错误码，括号里是产生它的代码位置。"SDK 异常类"是 Python SDK 抛给调用方的类型，映射规则见第 5 节。
"失败后状态"说的是沙箱，不是这次请求。

| # | 场景 | Connect 码（产生处） | SDK 异常类 | 失败后状态 | 调用方该做什么 |
|---|---|---|---|---|---|
| 1 | restore 或 delete 的 checkpoint 不存在，或已经被删除（含"有子节点、只是被隐藏"的，见 [13](13-state-concurrency-capacity.md)） | `not_found`（`ckpt/service.go:477`、`:771`） | `NotFoundException` | 什么都没动 | 用 `list()` 取当前可见的 checkpoint |
| 2 | secure 沙箱的请求没带 `X-Access-Token`，或 token 不对 | `unauthenticated`（`permissions/authenticate.go:64`、`:68`） | `AuthenticationException` | 什么都没动 | 检查连接配置是否带上了沙箱的 access token（[21](21-quickstart.md)）；用别的沙箱的 token 也是这个错误 |
| 3 | gsd 读不到 MMDS，无法校验 token | `unavailable`（`permissions/authenticate.go:57`） | `TimeoutException`（沿用 envd 的映射，提示语见第 5 节的说明） | 什么都没动 | 稍后重试；持续出现时按 [25](25-troubleshooting.md) 查 guest 到 MMDS 的通路 |
| 4 | create：两棵用户进程树之间有一条管道（读写两端分在两棵树里），或持有同一个 socket | `failed_precondition`（`ckpt/service.go:204`） | `SandboxException` | 什么都没动，进程没有被停过 | 把相连的进程改成从同一条命令启动，使它们成为一棵树（[08](08-process-trees.md)、[22](22-semantics-and-limits.md)） |
| 5 | restore：目标 checkpoint 的进程镜像不可用（`inventory.img` 读不了，或父镜像链断了） | `failed_precondition`（`ckpt/service.go:488`） | `SandboxException` | 什么都没动 | 这个 checkpoint 已不可恢复，换一个目标；它本身可以 delete |
| 6 | create 或 restore：gsd 处于 degraded（三类原因见第 4 节） | `failed_precondition`（`ckpt/service.go:688`、`:691`、`:695`） | `SandboxException` | 什么都没动 | 读错误信息或 `/health` 里的原因：restore 半途失败的，先 restore 一个 checkpoint；其余见第 4 节 |
| 7 | create：估算的镜像放不进 snapshot store，或超过 guest 的 `MemAvailable` | `resource_exhausted`（`ckpt/service.go:216`） | `NotEnoughSpaceException` | 什么都没动 | 删掉一些 checkpoint，或释放沙箱里的内存后重试；错误信息里有估算值与两个剩余量 |
| 8 | create：层栈太深，切换 ioctl 的选项串会超过内核上限；或者这个 checkpoint 建出来之后将来无法 restore | `resource_exhausted`（`overlayErrorCode`，`ckpt/service.go:874`，经 `:326`） | `NotEnoughSpaceException` | 文件系统与 checkpoint 列表没动；dump 已做过并被丢弃，下一次 create 全量（见 2.1） | restore 到一个层数少的 checkpoint 之后再继续，或删掉不用的 checkpoint；上限见 [13](13-state-concurrency-capacity.md) |
| 9 | create：某棵树的 `criu dump` 超过时限（单棵默认 60 s，一次 create 合计上限为其 3 倍） | `deadline_exceeded`（`criuErrorCode`，`ckpt/service.go:865`，经 `:280`、`:282`） | `TimeoutException` | 进程已放开、照常运行；镜像已丢弃；下一次 create 全量 | 这不是 SDK 的 `request_timeout`，调大它没有用；查进程为什么 dump 不完（[25](25-troubleshooting.md)），必要时调 `-criu-dump-timeout`（[24](24-configuration-and-observability.md)） |
| 10 | create：`criu dump` 自己报错（例如进程用了 CRIU 不支持的特性、连着树外的 unix socket） | `internal`（同上，经 `:280`、`:282`） | `SandboxException` | 同 9 | 错误信息末尾带 `criu-dump.log` 的最后 2048 字节，按它判断；完整日志在 gsd 的 journal 里 |
| 11 | create：扫描 `/proc` 失败、读不到容量数字、建镜像目录失败 | `internal`（`ckpt/service.go:183`、`:212`、`:226`） | `SandboxException` | 什么都没动 | 属于环境异常，按 [25](25-troubleshooting.md) 排查 |
| 12 | create：沉下 upper 失败（建目录、改名或 ioctl 失败，层栈太深以外的原因） | `internal`（`ckpt/service.go:326`） | `SandboxException` | 文件系统没动（改名已撤回）；下一次 create 全量 | 最常见的是根盘写满，错误信息里有 `no space left on device`；腾出空间后重试 |
| 13 | restore：杀进程之前的准备失败（建目录失败、层目录缺失、扫描失败），或切换失败而此前没有杀任何进程 | `internal`，层栈太深时为 `resource_exhausted`（`ckpt/service.go:498`、`:511`、`:543`） | `SandboxException` / `NotEnoughSpaceException` | 什么都没动，错误信息含 `nothing was changed` | 可以直接重试，或换一个目标 |
| 14 | restore：杀进程之后失败（切换层栈失败，或 `criu restore` 报错、超时） | `data_loss`（`restoreFailed`，`ckpt/service.go:641`，经 `:545`、`:594`、`:603`） | `CheckpointRestoreException` | 用户进程已被杀；文件系统处于 `old` 或 `target`（信息里写明）；多树时可能有一部分树已恢复；gsd 进入 degraded，拒绝 create | **只能再 restore 一次**（同一个或另一个 checkpoint），成功后解除；`criu restore` 失败时信息末尾带 `criu-restore.log` 的尾部 |

对这张表的几点补充：

- `list` 不会返回上表里除 2、3 以外的任何错误：它只读内存里的池，不查 degraded、不排队。`delete` 只会返回 1、2、3。
- 第 8 行的第二种情形（"将来无法 restore"）是 create 替 restore 把关：restore 的选项串比 create 的长（它要把整个层栈显式写出来，再加上名字更长的 upper 和 work 目录），
  所以 create 时就用"最坏情况下的 restore 目录名"再算一遍，放不下就拒绝，避免建出一个恢复不了的 checkpoint。
  正因为有这道检查，第 13 行里 `resource_exhausted` 那一支按现在的流程不应出现。
- 表中多数路径在新 920B 上被逐条触发过（2026-10-08，`259f1a25c`，`verify_fixes` 与 `verify_multitree`），判定结果在 [20](20-results-and-gaps.md)。
  没有现场触发过的分支也在那里列出，其中与本表有关的是：两棵树持有同一个 socket 的预检（第 4 行的 socket 一支）、多树的 dump 超时（第 9 行的多树情形）、
  三棵树时中间一棵 restore 失败（第 14 行"其余没有尝试"非空的情形）。这几支只有单测覆盖。

---

## 4. 三类 degraded

degraded 是 gsd 的一个进程内状态，含义是"gsd 活着、能应答，但 create 或 restore 暂时不该做"。它有三个互相独立的原因，
各存在 `Service` 的一个字段里（`ckpt/service.go:78-90`），`DegradedReason`（`ckpt/service.go:661`）把非空的几个用分号连起来对外报告。

| 类别 | 何时进入 | create | restore | delete | 怎么解除 | 是否跨 gsd 重启 |
|---|---|---|---|---|---|---|
| fatal | overlay 管理器初始化失败；状态文件读不了、解析不了、版本不认识，或记录的目录在磁盘上不存在；没有状态文件但初始 upper 不可用 | 拒 | 拒 | 不拦 | 只有重启 gsd 才会重新尝试 | 重启后重新判断 |
| restore 半途失败 | 一次 restore 在杀进程之后失败（错误表第 14 行） | 拒 | 允许 | 允许 | 一次成功的 restore | 是，原因写在状态文件里 |
| 状态文件写失败 | 最近一次写 `state.json` 失败 | 开头先重试写入，仍失败则拒 | 同左 | 照常执行，结束时也会重试 | 任何一次写成功 | 不适用（重启后按磁盘上的旧文件加载） |

三类的处置各不相同，原因如下。

**fatal：状态不可信，全部拒绝。** 这一类的共同点是 gsd 内存里的状态与内核里实际的层栈对不上，或者根本不知道对不对得上。
此时做 create 会基于错误的层栈拼选项串，做 restore 则可能切到一个不存在的层。交接前的实现在 overlay 管理器初始化失败时选择"继续运行、只是不切文件系统"，
结果是 restore 返回成功而文件系统没有回滚。现在改为进入 fatal（`Handle`，`ckpt/service.go:130-134`）。
这里也没有让 gsd 直接退出：gsd 退出会被 systemd 立刻拉起（`Restart=always`），再次失败、再次拉起，结果相同，而且 `/health` 上看不到原因。
fatal 状态下不写状态文件（`persist` 返回 `errStateNotTrusted`，`ckpt/state.go:264`），加载失败的那份文件原样留着供排查；也不回收镜像目录，
因为这时池可能是空的，按它回收会把还有用的镜像当成垃圾删掉（`ckpt/snapshot_store.go:29-34`）。
`DeleteCheckpoint` 不调 `checkReady`，所以不被拦；但状态没加载成功时池里没有条目，delete 实际只会得到 `not_found`。

**restore 半途失败：只拒 create。** 沙箱此刻处于"进程没了、文件系统在旧状态或目标状态"的中间态。如果允许 create，
这个中间态就会被当成一个正常状态存成 checkpoint，之后有人 restore 到它，得到的是一个没有进程、文件可能对不上的沙箱，而且看不出任何异常。
restore 必须允许，因为它正是把沙箱带回一个完整状态的唯一手段：restore 开头会把残留的用户进程全部杀掉、把层栈切到目标、再恢复全部进程，不依赖当前处于什么中间态。
成功之后由 `clearRestoreFailure`（`ckpt/service.go:644`）解除。delete 也允许，因为腾出 snapshot store 的空间可能正是让下一次 restore 成功的前提。
这一类原因随状态文件保存（`restore_failure` 字段），gsd 重启不会让沙箱自己变回完整状态，所以重启后仍然是 degraded（单测 `TestRestoreFailurePersisted`，`ckpt/state_test.go:276`）。

**状态文件写失败：先重试，不行再拒。** 这时内存里的状态是对的，只是磁盘上的那份落后了；危险在于 gsd 此刻如果重启，会加载到旧状态。
继续做 create 或 restore 只会让内存和文件差得更远，所以 `checkReady` 在开头先重试一次写入（`ckpt/service.go:679-683`），成功就当什么都没发生过，失败才拒绝。
delete 不拦：snapshot store 是 tmpfs，写失败最可能的原因就是它满了，而 delete 是唯一能腾出空间的操作。
不过 delete 在状态文件没写成功时不会真的删镜像目录（磁盘上的状态文件还引用着它们），细节见 [13](13-state-concurrency-capacity.md)。

**`/health` 始终返回 HTTP 200。** 正常时 body 是 `{"status":"ok"}`，degraded 时是 `{"reason":"...","status":"degraded"}`（`GetHealth`，`api/store.go:23`；两个键按字母序输出，`reason` 在前，解析时不要依赖键序）。
不用非 200 状态码表达 degraded，是因为 gsd 进程本身是健康的，探活如果据此把它当成挂掉而重启，对 fatal 没有帮助，对另外两类则是有害的。
`/health` 不需要鉴权（`withOpenHealth`，`main.go:103`），所以排障时在 guest 里直接 `curl` 即可。
建模板时的自检要求 body 必须是 `ok`，degraded 会让模板构建失败（[05](05-guest-boot-and-injection.md)）。

---

## 5. SDK 侧的异常映射

gsd 的线协议是 Connect，SDK 用的是与 envd 同一套客户端。envd 的默认映射是为 envd 的语义写的，直接套在 gsd 上有两处是错的：
它把 `resource_exhausted` 变成 `RateLimitException` 并附一句 "Rate limit exceeded"，把 `deadline_exceeded` 解释成"超过了 SDK 的 `timeout` 选项"。
对 gsd 而言前者是"放不下"，后者是 gsd 自己给 criu 设的时限，两句提示都会把调用方引到错误的方向。

所以 checkpoint 的四个调用单独用一张映射表 `CHECKPOINT_RPC_ERROR_MAP`（`py-sdk/e2b/gsd/api.py:59`），只覆盖 gsd 有特定含义的四个码，其余仍落到 envd 的默认表（`py-sdk/e2b/envd/rpc.py:19`）：

| Connect 码 | 来自哪张表 | 异常类 | SDK 附加的说明 |
|---|---|---|---|
| `resource_exhausted` | gsd 专用 | `NotEnoughSpaceException` | 放不下，删掉一些 checkpoint 或释放内存后重试 |
| `failed_precondition` | gsd 专用 | `SandboxException` | 请求被 gsd 拒绝，后面跟 gsd 的原文 |
| `data_loss` | gsd 专用 | `CheckpointRestoreException` | 进程在失败之前已被杀掉，restore 一个 checkpoint 才能回到一致状态，在那之前 create 会被拒 |
| `deadline_exceeded` | gsd 专用 | `TimeoutException` | criu 没有在 gsd 的时限内完成，不是 SDK 的 `request_timeout`；checkpoint 没有建成，进程已放开 |
| `not_found` | envd 默认 | `NotFoundException` | 无 |
| `unauthenticated` | envd 默认 | `AuthenticationException` | 无 |
| `unavailable` | envd 默认 | `TimeoutException` | "likely due to sandbox timeout" |
| `internal` 及其余 | envd 默认的兜底 | `SandboxException` | 信息为 `Code.internal: <gsd 原文>`：兜底分支把错误码的枚举值原样拼在前面（`handle_rpc_exception`，`py-sdk/e2b/envd/rpc.py:36`） |

使用时要注意四件事：

- **`CheckpointRestoreException` 是唯一需要专门捕获的异常。** 其余失败之后沙箱都还在一个完整状态上，调用方可以按一般错误处理；
  只有它表示沙箱已经不完整，并且规定了唯一的出路。它的类文档（`py-sdk/e2b/exceptions.py:122`）把这一点写在了类型上。
- **`AuthenticationException` 不是 `SandboxException` 的子类**（`py-sdk/e2b/exceptions.py:86`）。只写 `except SandboxException` 的代码接不住鉴权失败。
- **`unavailable` 的提示语对 gsd 不准确。** 它沿用 envd 的映射，信息里说"可能是沙箱超时"，而 gsd 返回 `unavailable` 的唯一原因是读不到 MMDS。
  这一条没有像前四个码那样单独改写，遇到时以 gsd 原文（`cannot verify access token`）为准。
- **`deadline_exceeded` 的专用提示语是按 create 写的**（"进程已放开"）。这与代码一致：如 2.2 所述，restore 阶段的超时走 `data_loss`，不会以这个码出现。
- **调用方看到"超时"有三种来源，含义完全不同，要按信息原文区分。** 其一，`TimeoutException`，信息含 `CRIU did not finish within GSD's time limit`：gsd 自己给 `criu dump` 设的时限到了（第 3 节第 9 行），
  checkpoint 没有建成，进程已放开。其二，`TimeoutException`，信息以 `cannot verify access token` 开头：gsd 读不到 MMDS（第 3 行），请求在鉴权处就被拒，什么都没做。
  其三，SDK 自己的 `request_timeout` 到期：这时抛出的**不是** `TimeoutException`，而是底层 HTTP 库的超时异常——`handle_checkpoint_rpc_exception` 对不是 Connect 错误的异常原样放行（`py-sdk/e2b/gsd/api.py:80`，`py-sdk/tests/test_gsd_errors.py` 固定了这一点）；
  它只说明 SDK 不等了，gsd 那边的操作照常做完（2.3）。只按异常类型写 `except TimeoutException` 的代码分不清前两种，也接不住第三种。

`restore()` 成功时返回 `True`，失败一律抛异常，不存在"返回 `False`"的失败。`is_running()` 走的是 `/health` 而不是 RPC，
它只回答 gsd 是否在应答，degraded 时同样返回 `True`；要知道是否 degraded 需要自己读 `/health` 的 body。

**两边都不重试。** gsd 内部对 criu、ioctl 都只尝试一次（唯一的例外是鉴权时读 MMDS 最多三次，每次 2 秒时限，`permissions/mmds.go:21-22`）；
SDK 对这四个调用也不按错误码自动重试（Connect 客户端在连接层有一处例外：收到 `RemoteProtocolError`——连接在拿到响应之前被对端关掉——时会把请求重发，最多 3 次，`py-sdk/e2b_connect/client.py:288` 的 `_retry`；这是与 envd 共用的传输层行为，不是针对 gsd 的设计，本书没有它在 checkpoint 调用上被触发的现场结果）。不重试是有意的：create 重试成功会留下调用方不知道的 checkpoint，restore 重试则在调用方不知情的情况下把沙箱再回滚一遍；
而需要重试的失败（第 3、7、12 行）都要求调用方先做点什么，自动重试没有意义。

映射由 `py-sdk/tests/test_gsd_errors.py` 固定，同步与异步两个客户端各测一遍。

---

## 6. 不再有的静默降级

交接前的实现里有三条路径会在出问题时悄悄换一种做法并返回成功。它们都已去掉，这里列出来是因为读旧日志、旧文档或旧模板上的行为时仍会遇到。

| 修改前的行为 | 后果 | 现在 |
|---|---|---|
| create 时找不到或读不了用户进程，就只做文件系统部分，照常返回成功 | 调用方拿到一个没有进程镜像的 checkpoint，restore 之后进程全没了，两次调用都报成功 | 扫描失败返回 `internal`（`ckpt/service.go:183`）。"确实没有用户进程"是另一回事，仍然允许，但响应里明确给出 `has_process_image=false`、`process_trees=0`（[09](09-checkpoint.md)） |
| overlay 管理器初始化失败时继续提供服务，只是跳过所有层切换 | create 和 restore 都返回成功，文件系统既没有被保存也没有被回滚 | 进入 fatal degraded，create 和 restore 都拒绝，`/health` 给出原因（`ckpt/service.go:130-134`） |
| 找不到 envd 的主进程时报错，create 和 restore 都失败 | 这一条方向相反，是"不必要的失败"：envd 重启的窗口里 gsd 不可用 | 查不到 envd 不是错误，只是排除集合里少一项，照常按 uid 扫描（`envdPID`，`ckpt/pid.go:207`） |

第三条放在这里是为了完整：它与前两条属于同一次清理，共同的原则是"缺了某个前提时，行为必须是确定的、并且对调用方可见"，
前两条的毛病是该失败时不失败，第三条的毛病是不该失败时失败。

还有一处容易被误认为降级但其实不是：`clear_refs` 写失败。它不让 create 失败，只是打一条告警并把失败的进程数放进响应的 `clear_refs_failed`（`clearSoftDirty`，`ckpt/service.go:418`）。
这样处理的理由是它不影响这个 checkpoint 的正确性，只会让那几个进程下一次的增量比应有的大（最坏变成全量）；而结果通过响应字段告诉了调用方，所以不算静默。

---

## 7. 不变量清单

下面这些条件由代码结构保证，改代码时不能破坏。最后一列标出被破坏之后是否会有报错：标"是"的那几条，破坏后一切调用仍然返回成功，只是恢复出来的状态不对。

| # | 不变量 | 破坏后的后果 | 静默？ | 由什么保证 |
|---|---|---|---|---|
| 1 | 从每棵树 dump 完，到它的 `clear_refs` 写完，这棵树的进程必须一直停着 | 窗口里写的页既不在这次镜像里，soft-dirty 位又被清掉，下一次增量也不会带上；restore 出来缺页内容 | **是** | `--leave-stopped`；`resume` 只在 `clearSoftDirty` 之后调用（`ckpt/service.go:334-336`）。修改前用的是 `--leave-running`，漏页是实测到的（[09](09-checkpoint.md)） |
| 2 | dump 之前 gsd 不能自己给进程发 SIGSTOP | CRIU 把 seize 时已停住的任务记成 stopped，restore 之后它们一直停着，与用户自己暂停的进程分不开 | **是** | 一致性靠逐棵 `--leave-stopped`，见 `CreateCheckpoint` 头注释（`ckpt/service.go:157-160`） |
| 3 | dump 前就停着的进程，dump 后不替用户放开 | 用户用 Ctrl-Z 停住的进程在一次 checkpoint 之后自己跑了起来 | **是** | `resumable`（`ckpt/pid.go:414`）与 `resumeAfterFailedDump`（`ckpt/service.go:840`）都只放开"dump 前不是 T"的 |
| 4 | restore 切换层栈和恢复进程之前，当前用户进程必须杀干净，包括子孙和已被 PID 1 收养的 | 残留进程占着 PID 导致 `criu restore` 报 `Can't fork: File exists`；或带着 checkpoint 之后的状态在回滚后的文件系统上继续写 | 前者否，后者**是** | `killTrees` 先冻结、反复扫描到没有新进程、再整体杀（`ckpt/criu.go:215`） |
| 5 | 父镜像必须活得比所有以它为父的子镜像久 | 子镜像里标成"在父镜像里"的页读不到，`criu restore` 失败；发生在杀进程之后，是 `data_loss` | 否 | 池里的依赖计数与"隐藏而不删"（[13](13-state-concurrency-capacity.md)）；`checkImages` 在杀进程之前再兜一次 |
| 6 | 任何一步失败之后，下一次 dump 必须是全量 | 见 2.1：以过期的父镜像做增量，restore 出两个时刻混合的内存 | **是** | `discardFailedCheckpoint` 无条件清空；restore 杀进程后也清空（`ckpt/service.go:535`） |
| 7 | 增量的父镜像必须属于池里某个 checkpoint | 父目录没有属主，delete 不知道它被依赖，可能被回收 | 否（之后 restore 失败） | `incrementalParent` 检查 `pool.holds`（`ckpt/service.go:405`） |
| 8 | restore 里所有可能失败又不改状态的事，都在杀进程之前做 | 本可原样返回的失败变成 `data_loss` | 否 | `RestoreCheckpoint` 的两段结构；`PrepareRestore` 与 `CommitRestore` 分开 |
| 9 | 删除时先写状态文件，再删镜像目录 | 顺序反过来时，中途崩溃会留下指向已删目录的条目，重启后 restore 到它是 `failed_precondition`，以它为父的子镜像全部不可用 | 否 | `DeleteCheckpoint`（`ckpt/service.go:800-808`） |
| 10 | 状态可能改过的每一条路径（含失败路径）结束时都要落盘 | gsd 重启后加载到旧状态，与内核层栈对不上，进入 fatal | 否 | `defer s.persist()` 紧跟在 `checkReady` 之后（`ckpt/service.go:174`、`:472`） |
| 11 | criu 的运行不受请求取消的影响 | 见 2.3 | 否 | `context.WithoutCancel` |
| 12 | 半途失败的 restore 之后、成功的 restore 之前，不接受 create | 中间态被存成 checkpoint | **是** | `checkReady(false)`（`ckpt/service.go:690`） |

十二条里有六条被破坏后是静默的，而且都落在"内存内容对不对"上，现场测试很难碰巧发现。
这就是 gsd 的单测大量使用假的 `/proc` 和假的 criu 来逐条固定这些顺序的原因（[19](19-testing-and-debug-kernel.md)），也是第 1 条专门有一个现场竞态测试 `verify_clear_refs_race` 的原因。

内核一侧另有一组不变量（可能失败的步骤都在提交点之前、读后端状态必须持读锁等），它们保证的是切换 ioctl 本身的失败语义：ioctl 返回错误时内核里什么都没有改变。
本章第 2 节把"ioctl 失败"当成一个可以干净撤回的步骤，依赖的正是那一组，详见 [07](07-overlay-concurrency-and-fixes.md)。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| create 的失败出口与收尾 | `ckpt/service.go` — `CreateCheckpoint`（`:163`）、`discardFailedCheckpoint`（`:825`）、`resumeAfterFailedDump`（`:840`）、`clearSoftDirty`（`:418`） |
| restore 的两段与提交点 | `ckpt/service.go` — `RestoreCheckpoint`（`:463`）、`checkImages`（`:703`）、`restoreFailed`（`:635`）、`clearRestoreFailure`（`:644`） |
| 错误码的选择 | `ckpt/service.go` — `criuErrorCode`（`:865`）、`overlayErrorCode`（`:874`）；`ckpt/overlay.go` — `ErrLayerStackTooDeep`（`:35`）、`checkOverlayOptionsLen`（`:540`） |
| degraded | `ckpt/service.go` — `checkReady`（`:677`）、`setFatal`（`:653`）、`DegradedReason`（`:661`）；`ckpt/state.go` — `persist`（`:256`）、`loadState`（`:341`） |
| `/health` | `api/store.go` — `GetHealth`（`:23`）；`main.go` — `withOpenHealth`（`:103`） |
| 鉴权的两种错误 | `permissions/authenticate.go` — `Authenticate`（`:53`）；`permissions/mmds.go` — `AccessTokenHash`（`:49`） |
| criu 的超时与日志尾部 | `ckpt/criu.go` — `runCriuCmd`（`:28`）、`logTail`（`:144`）、`killTrees`（`:215`） |
| SDK 映射 | `py-sdk/e2b/gsd/api.py` — `CHECKPOINT_RPC_ERROR_MAP`（`:59`）、`handle_checkpoint_rpc_exception`（`:80`）；`py-sdk/e2b/envd/rpc.py` — `_DEFAULT_RPC_ERROR_MAP`（`:19`）；`py-sdk/e2b/exceptions.py` |
| 固定这些行为的单测 | `ckpt/service_failure_test.go`、`ckpt/restore_test.go`、`ckpt/state_test.go`；`py-sdk/tests/test_gsd_errors.py` |

以上 gsd 路径都相对 `packages/gsd/`，其中 `ckpt/` 是 `internal/services/checkpoint/` 的简写，`api/`、`permissions/` 在 `internal/` 下。

---

## 本章要点

1. 两条原则：做不到就拒绝，不降级成部分成功；每种失败之后沙箱的状态必须说得清，并写进错误信息。
2. create 没有不可逆步骤，任何失败都回到"进程照常运行、列表没有变化"；但只要 dump 已经开始，失败之后下一次 create 就是全量，这是为了不拿过期的 soft-dirty 历史做增量。
3. restore 的提交点是杀进程。之前的失败都带 `nothing was changed`；之后的失败一律是 `data_loss`（唯一的例外：沙箱里本来就没有用户进程可杀、切层又失败时，沙箱仍是原样，按普通错误返回并带 `nothing was changed`，不进 degraded），信息里写明文件系统在 `old` 还是 `target`，多树时逐棵写明已恢复、失败、未尝试。
4. 错误表在第 3 节，全书唯一。`deadline_exceeded` 只来自 create；restore 阶段的 criu 超时是 `data_loss`。
5. degraded 有三类：fatal（全拒，重启 gsd 才重试）、restore 半途失败（只拒 create，成功 restore 一次解除，跨 gsd 重启保留）、状态文件写失败（先重试再拒，delete 照常）。`/health` 始终 200。
6. SDK 对四个码单独映射，只有 `CheckpointRestoreException` 需要专门处理；`AuthenticationException` 不在 `SandboxException` 之下；`unavailable` 的提示语沿用 envd，对 gsd 不准确。gsd 和 SDK 都不重试。
7. 客户端超时不等于操作没发生：criu 不随请求取消，超时后应当用 `list()` 核对。
8. 十二条不变量里六条被破坏后是静默的，都关系到恢复出来的内存和进程状态是否正确。
