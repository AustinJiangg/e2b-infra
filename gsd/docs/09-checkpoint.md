# 09 · checkpoint 的实现

## 本章目标

读完本章，你应当能够：

- 按顺序说出 `CreateCheckpoint` 的每一步，在哪一步之前失败"什么都没动"，每一步失败后 gsd 做哪些收尾；
- 解释多棵树逐棵 dump 为什么仍然得到一个自洽的状态，以及它在哪一点上不是原子的；
- 说清三个容易被改坏的设计决定：为什么用 `--leave-stopped`、为什么下沉之后要再写一次 `clear_refs`、为什么不能在 dump 之前自己发 `SIGSTOP`；
- 说明按树增量的父镜像怎么选、什么时候被清空、失败后为什么必须全量；
- 说出没有用户进程、或者 upper 为空时 create 做了什么。

前面几章准备好了零件：文件系统怎么换层（[06](06-overlay-hot-switch.md)）、用户进程怎么分成树（[08](08-process-trees.md)）、CRIU 的增量靠什么（[02](02-criu-and-incremental-memory.md)）。
本章把它们按 `CreateCheckpoint` 的执行顺序装起来。下一章（[10](10-restore.md)）是 restore。

主要代码：`ckpt/service.go` 的 `CreateCheckpoint`（:163）及其辅助函数；`ckpt/criu.go` 的 `buildCriuDumpCmd`、`runCriuCmd`。

---

## 1. 总览

```
持操作锁 → 检查健康状态 → 扫描 /proc、找根、记状态
        → [有用户进程时] 多树预检 → 容量预检                       ← 到这里为止什么都没动
        → 建镜像目录
        → [逐棵] criu dump --leave-stopped                         ← 第一棵 dump 完，冻结窗口开始
        → 列出全部被停住的进程
        → 下沉 upper（改名 + ioctl）
        → 对被停住的进程重写 clear_refs
        → SIGCONT                                                  ← 冻结窗口结束
        → 量镜像大小、更新增量父镜像、条目入池
        → 写 state.json → 放锁
```

create 没有不可逆的步骤：任何一步失败，收尾之后沙箱都回到 create 之前的样子（进程在跑，文件系统没变，没有多出条目）。这与 restore 形成对比（[10](10-restore.md)、[12](12-failure-semantics.md)）。

---

## 2. 逐步

### 2.1 进入与检查

| # | 步骤 | 代码 | 说明 |
|---|---|---|---|
| 1 | 取操作锁 `opMu`，全程持有 | `ckpt/service.go:164` | Create、Restore、Delete 互斥，后来的排队（[13](13-state-concurrency-capacity.md)）。`total_ms` 从拿到锁之后开始计 |
| 2 | `checkReady(false)` | :170，函数在 :677 | fatal、上一次 restore 半途失败、状态文件写不进去，三种情况都拒绝 create（[12](12-failure-semantics.md)） |
| 3 | 登记 `defer s.persist()` | :174 | 无论成功失败，退出前都把状态落盘：失败路径也可能改过状态（清空了增量父镜像、下沉回滚时改了记录） |
| 4 | 生成 id：`ckpt_<纳秒时间戳>` | :176 | |

### 2.2 扫描与预检

| # | 步骤 | 代码 | 说明 |
|---|---|---|---|
| 5 | `scanUsers`：一次 `/proc` 扫描 | :180 | 读不了 `/proc` 直接返回 `Internal`，不降级成只有文件系统的 checkpoint |
| 6 | 找根，得到树的棵数 | :185 | 定义见 [08](08-process-trees.md) |
| 7 | 记录所有树里每个进程此刻的状态 | :189-192 | 为了最后只放开"dump 前不是停止状态"的进程；必须在第一棵 dump 之前记全 |
| 8 | 多树预检 `crossTreeShared` | :200 | 两棵及以上才做。命中则 `FailedPrecondition` |
| 9 | 容量预检 `estimateDumpIn` | :209 | 全部树的匿名内存与共享内存之和乘 1.2，与 snapshot store 剩余空间、guest 可用内存比较，任一不够则 `ResourceExhausted`（[13](13-state-concurrency-capacity.md)） |

第 5～7 步的耗时记为 `find_pids_ms`，第 8～9 步记为 `precheck_ms`。**到第 9 步为止，沙箱没有被改动过任何东西**：没有进程被停过，没有目录被建过。
第 8、9 步的拒绝因此是最干净的一类失败——调用方按错误信息处理后重试即可。没有用户进程时第 7～9 步整个跳过。

### 2.3 逐棵 dump

| # | 步骤 | 代码 | 说明 |
|---|---|---|---|
| 10 | 建镜像目录 `<snapshot store>/<id>_mem` | :224-227 | 没有用户进程时也建，是个空目录 |
| 11 | 建一个带总时限的上下文 | :237 | `context.WithoutCancel`：与请求脱钩，客户端断开不会让 criu 半途被杀；总时限是单棵时限的 3 倍（§5） |
| 12 | 对每个根，按进程号升序 | :242 起 | |
| 12a | 　选增量父镜像 `incrementalParent` | :253 | 找到则加 `--prev-images-dir`，并记下父 checkpoint 的 id（§4） |
| 12b | 　建子目录 `t<i>`，在单棵时限内运行 criu | :260-264 | |
| 12c | 　失败 → 收尾并返回（§6） | :268-284 | |
| 12d | 　成功；第一棵成功时记下时刻 `tFirstStopped` | :285-287 | 这是冻结窗口的起点 |

每棵树的命令由 `buildCriuDumpCmd`（`ckpt/criu.go:51`）拼出：

```
/usr/sbin/criu dump --tree <根进程号> -D <镜像目录>/t<i> --shell-job --leave-stopped -v4 --track-mem [--prev-images-dir <父镜像目录>]
```

| 参数 | 作用 |
|---|---|
| `--tree` | 保存这个进程和它的全部子孙。一次只能一棵 |
| `-D` | 镜像写到哪 |
| `--shell-job` | 允许被保存的进程带着控制终端或属于某个会话；用户命令常常如此 |
| `--leave-stopped` | dump 完成后把这棵树留在停止状态而不是放它继续跑（§3.2） |
| `--track-mem` | dump 时把这棵树的 soft-dirty 位清零，为下一次增量建立基线 |
| `--prev-images-dir` | 只写相对父镜像改过的页；CRIU 会在本目录里留一个指向父目录的 `parent` 符号链接 |
| `-v4` | 最详细的日志 |

criu 的标准输出和标准错误被同时写到两处（`execCriuDump`，`criu.go:73`）：gsd 自己的标准错误（进入 journal），以及镜像目录里的 `criu-dump.log`。

所有树 dump 完之后，`criu_dump_ms` 记的是**各棵之和**，`process_trees` 记棵数。

### 2.4 冻结窗口内

此刻每棵树都停着。

| # | 步骤 | 代码 | 说明 |
|---|---|---|---|
| 13 | 再扫一次 `/proc`，列出各棵树现在的全部进程 | :297 `treePIDs` | 第 5 步的扫描与 CRIU 冻结之间可能有新派生的进程，以现在的为准；扫描失败则退而只处理各个根，并告警 |
| 14 | 算出要放开的名单 `resumable` | :302 | 去掉第 7 步记录里就是停止状态的 |
| 15 | 把"放开"包成只执行一次的函数并登记 `defer` | :303-313 | 之后任何错误路径都保证进程被放回运行状态 |
| 16 | `SinkUpper`：下沉 upper | :322 | 改名加 ioctl（[06](06-overlay-hot-switch.md)）。耗时记为 `overlay_sink_ms`。失败见 §6 |
| 17 | `clearSoftDirty`：对第 13 步列出的每个进程向 `/proc/<pid>/clear_refs` 写 `4` | :334，函数在 :418 | 见 §3.3。写失败的进程数进响应的 `clear_refs_failed` |
| 18 | 放开：对名单里的进程发 `SIGCONT` | :336 | **冻结窗口结束**。`stopped_ms` = 此刻减去 `tFirstStopped` |

### 2.5 收尾

| # | 步骤 | 代码 | 说明 |
|---|---|---|---|
| 19 | 量每棵树镜像目录的实际占用 | :348 `dirUsage` | 进程已经放开，不占冻结时间。量不出来只告警 |
| 20 | 用本次的树整体替换 `lastTrees` | :344-357 | 本次没有的树（已经退出）不再保留；根的启动时间为 0 的不记 |
| 21 | 建条目并入池 `pool.add` | :358-366 | 条目带镜像目录、**完整层栈**、各棵树；入池时算出父子依赖 |
| 22 | 组响应返回；`defer` 依次执行：放开（已执行过，空操作）、写 `state.json`、放锁 | :371-380 | |

---

## 3. 三个设计决定

### 3.1 多棵树的一致性

CRIU 一次只能 dump 一棵树，N 棵树就是 N 次先后进行的 dump。它们凭什么算"同一时刻"的状态？

论证只有三句（`CreateCheckpoint` 的头注释，`ckpt/service.go:147-155`）：

1. 每棵树的镜像是它**自己被冻住那一刻**的状态；
2. 因为 `--leave-stopped`，一棵树 dump 完之后不再运行——在等后面的树 dump 时，它不改内存，也不写文件；
3. 所以到最后一棵 dump 完时，全部镜像加上接下来下沉的文件系统，等价于"**最后一棵被冻住那一刻**"的状态。

文件系统的下沉发生在所有树都停住之后，所以层里的文件内容与每棵树的内存是对得上的：没有哪棵树在自己的镜像生成之后又写过文件。

**不是原子的地方**在树与树之间：排在后面的树，在前面的树已经冻住之后还多跑了一段——长度是前面各棵树的 dump 时间之和。
互不通信的后台命令对此无感。通过文件、网络相互协作的两棵树，restore 之后看到的对方状态可能比自己"旧"这么一小段。
需要严格一致的进程应当用同一条命令启动成一棵树（[22](22-semantics-and-limits.md)）。

### 3.2 为什么 `--leave-stopped`

交接前的实现用的是 `--leave-running`：dump 一结束进程就继续跑，gsd 随后下沉文件系统、重写 `clear_refs`。这在"dump 结束"与"`clear_refs` 写完"之间留下一个窗口，窗口里进程是活的：

- 进程在窗口里写了某一页 → 这一页被标上 soft-dirty；
- gsd 随后写 `clear_refs` → 这一页的 soft-dirty 被清掉；
- 下一次增量 dump 时，这一页看起来"没被改过" → 不写进镜像，restore 时从父镜像里取——取到的是窗口之前的旧内容。

结果是 restore 出来的内存里有个别页是旧的，而且毫无征兆。这类漏页在 2026-09-23 的现场对照里反复出现：把 guest 里的 gsd 换回修改前的二进制，有效的 50 轮比对里 31 轮有漏页（单轮最多 717 页），同一天换成修改后的二进制 30 轮为 0（新 920B，口径同下；出处见 [20](20-results-and-gaps.md)）；
改为 `--leave-stopped` 之后，2026-10-08 确认验证的 30 轮比对漏页为 0（新 920B、`259f1a25c`；判定值见 [20](20-results-and-gaps.md)，测试方法见 [19](19-testing-and-debug-kernel.md)）。

`--leave-stopped` 让进程从 dump 结束一直停到 `clear_refs` 写完，窗口里它们不可能写内存，也就没有可丢的修改。它同时保证了 §3.1 的第 2 句，以及文件系统与内存的对应。

代价是进程多停了一段。单树时这段只包含一次空 upper 检查或一次切换，再加一轮 `clear_refs`：同一次确认验证的这项比对测试里（单树、持续写内存的负载），`stopped_ms` 的中位数是 0.53 ms。多树时见 §7。

### 3.3 为什么下沉之后要重写 `clear_refs`

CRIU 带 `--track-mem` dump 时已经清过一次 soft-dirty，按理下一次增量的基线已经建立。但紧接着的 overlay 切换会在内核里收缩目录项缓存（[06](06-overlay-hot-switch.md)），
这个动作会让用户进程的内存区域被标上"区域级"的 soft-dirty 标志——不是某一页脏了，而是整个区域被视为脏（原理见 [02](02-criu-and-incremental-memory.md) 的"假全量"）。
如果不处理，下一次增量 dump 会把这些区域整个写出来，增量退化成接近全量。

所以 gsd 在下沉之后对被 dump 过的全部进程再写一次 `clear_refs`（`writeClearRefs`，`ckpt/service.go:434`，写入字符 `4` 表示清 soft-dirty）。
这时进程还停着，所以这一次清除与进程的写入之间没有竞争——这正是 §3.2 的窗口被关上的地方。

某个进程写失败时不让整个 create 失败：它的后果只是下一次增量比需要的大（最坏退化成全量），数据不会错。每个失败的进程打一条告警，个数放进响应的 `clear_refs_failed`。
进程已经不在了（`/proc/<pid>` 消失）不算失败。

### 3.4 为什么不在 dump 之前自己 `SIGSTOP`

要让多棵树"同时"冻住，最直接的想法是：先对所有树发 `SIGSTOP`，等它们都停了，再逐棵 dump。这样连 §3.1 里"树之间不是原子的"那一点也没有了。

不这样做，是因为 CRIU 的一条语义（`ckpt/service.go:157-160` 的注释）：**它开始接管时已经处于停止状态的任务，在镜像里被记成"停止"，restore 之后会被再发一次停止信号、留在停止状态。**
gsd 如果自己把所有进程停住再 dump，那么每一次 restore 恢复出来的进程就全是停着的。而 gsd 无法在 restore 之后分辨"这个进程是我为了 dump 停的"还是"用户自己按 Ctrl-Z 停的"——
后者正是靠这条语义才得以在 create 和 restore 之后保持停止（[08](08-process-trees.md)）。两者不能兼得，于是选择让 CRIU 自己去冻结每棵树，gsd 只在 dump 之后借 `--leave-stopped` 接手。

---

## 4. 按树增量

### 4.1 父镜像是谁

每棵树有自己的增量链。`incrementalParent`（`ckpt/service.go:396`）为一棵树选父镜像的规则在 [08](08-process-trees.md) 讲过：根的进程号和启动时间都与 `lastTrees` 里某一项相同，且那一项所属的 checkpoint 仍在条目池里。

`lastTrees` 里的各项总是指向**同一个** checkpoint——"上一个成功的 create，或者上一次 restore 到的那个 checkpoint"——因为每次成功的 create 或 restore 都把它整体替换。
所以按现在的流程，一个 checkpoint 的各棵树要么是全量，要么都以同一个 checkpoint 为父；`parent_checkpoint_id` 报告的就是它。
（条目池的依赖模型是按"可以有多个父"实现的，不依赖这一点，见 [13](13-state-concurrency-capacity.md)。）

混合的情况很常见：一个 checkpoint 里，老树做增量、新起的树做全量。确认验证里的一组数字直观地展示了这一点（新 920B、2026-10-08、每棵树 64 MiB 的内存负载；判定值见 [20](20-results-and-gaps.md)）：

| checkpoint | 当时的树 | 镜像大小 |
|---|---|---|
| 第一个 | 两棵，都是首次 | 133.2 MiB |
| 第二个 | 同样两棵，内存基本没动 | 0.38 MiB |
| 第三个 | 原来两棵 + 新起一棵 | 66.9 MiB，其中新树 66.6 MiB，两棵老树各 0.12 MiB |

### 4.2 什么时候清空

`lastTrees` 在以下时刻被清掉（全部或部分），之后相应的树下一次做全量：

| 时刻 | 清什么 | 为什么 |
|---|---|---|
| create 的任何一步失败（dump 或下沉） | 全部 | §6.3 |
| 它指向的 checkpoint 被 Delete | 指向它的各项 | 哪怕那个 checkpoint 因为还有子节点只是被隐藏、镜像还在，也清掉：继续以它为父会让新的 checkpoint 也挂在它下面，它就得一直留到这条新链也删光，而用户删它是希望它尽早消失（`ckpt/service.go:775-778`） |
| restore 杀掉进程之后 | 全部；restore 成功则改成目标 checkpoint 的各棵树 | 原来那些进程没了，它们的 soft-dirty 历史不再有意义（[10](10-restore.md)） |
| gsd 重启后加载的是旧格式的状态文件 | 全部 | 旧格式不带根的进程号与启动时间，认不出是哪棵树（[13](13-state-concurrency-capacity.md)） |

全量总是安全的：它不依赖任何 soft-dirty 历史。清空的唯一代价是下一次 dump 多写一些。

---

## 5. 时限

每棵树的 dump 有一个时限，默认 60 秒（`defaultCriuDumpTimeout`，`ckpt/service.go:25`；参数 `-criu-dump-timeout`）。一次 create 里全部树合计另有一个上限，是它的 3 倍
（`criuTotalTimeoutFactor`，`service.go:33`）。合计上限存在的理由是：先 dump 完的树在后面的树 dump 期间一直停着，树多的时候不能让"每一棵都差一点超时"把停顿累加到 N 倍。
只有一棵树时合计上限不起作用，单棵时限先到。

到时限时 `runCriuCmd`（`ckpt/criu.go:28`）先发 `SIGTERM`，让 criu 走它自己的清理路径——dump 出错时它会把被它接管的进程恢复运行；
2 秒之后（`criuTermGrace`，`criu.go:23`）还没退出就 `SIGKILL`。超时返回的错误里带着上下文的超时错误，`criuErrorCode`（`service.go:865`）据此把它映射成 `DeadlineExceeded`，
调用方可以和"criu 自己报错"（`Internal`）区分开。

这个时限是 gsd 自己的，与 SDK 的 `request_timeout` 无关：SDK 那一侧超时只是客户端不等了，gsd 里的 create 会继续做完（第 11 步的上下文与请求脱钩）。

---

## 6. 失败路径

### 6.1 某棵树 dump 失败

按顺序做四件事（`ckpt/service.go:268-284`）：

1. **保住诊断信息**：镜像目录马上要删，先把 `criu-dump.log` 的最后 2048 字节（从完整的一行开始）接到错误信息后面。完整的 `-v4` 输出仍在 gsd 的 journal 里。
2. **放开进程** `resumeAfterFailedDump`（:840）：重新扫描全部树，对"现在是停止状态、而 dump 前不是"的进程发 `SIGCONT`。这覆盖两类进程：
   失败的那棵之前已经 dump 完、被 `--leave-stopped` 留着的树；以及失败的这棵自己——criu 走正常错误路径时会自己放开它，但被超时杀掉、崩溃、被内存不足杀手杀掉时可能把它留在停止状态。
   dump 前就停着的不碰；还没轮到的树本来就在跑。
3. **丢弃镜像** `discardFailedCheckpoint`（:825）：删掉整个 `<id>_mem`，包括已经 dump 成功的那几棵；清空 `lastTrees`。
4. **返回错误**：`DeadlineExceeded` 或 `Internal`。多棵树时错误信息写明是哪棵树失败、它之前有几棵已经被放开并丢弃了镜像。

### 6.2 下沉失败

这时所有树都已经 dump 成功并停着。`SinkUpper` 自己负责文件系统一侧的回退（把改了名的 upper 改回原名、删掉新建的目录，见 [06](06-overlay-hot-switch.md)）；
`CreateCheckpoint` 丢弃全部镜像、清空 `lastTrees`，第 15 步登记的 `defer` 把进程放开。错误码由 `overlayErrorCode`（`service.go:874`）决定：
层栈太深（选项串超过内核上限）是 `ResourceExhausted`，其余是 `Internal`。

### 6.3 为什么失败后不能退回上一个父镜像

失败后把 `lastTrees` 恢复成 create 之前的样子，看起来能省掉下一次的全量。但这是错的（`discardFailedCheckpoint` 的注释，`ckpt/service.go:822-824`）：

- dump 失败时，CRIU 可能已经把一部分任务的 soft-dirty 清零了（它是先写页、再清零的）；
- 下沉失败时，这次 dump 已经把全部任务完整地清零过。

无论哪种，soft-dirty 现在记录的是"自这次失败的 dump 以来"的修改，而不是"自上一个成功的 checkpoint 以来"的。如果下一次仍以更早的那个镜像为父，
那么"上一个成功的 checkpoint"到"这次失败的 dump"之间被改过的页，既不在父镜像里（父镜像更早），也不会被当成脏页写出来——restore 出来的内存就是错的。
所以失败之后唯一正确的做法是清空，强制下一次全量。这是 [12](12-failure-semantics.md) 不变量清单里的一条。

### 6.4 失败之后的状态

| 失败点 | 进程 | 文件系统 | 条目池 | 下一次 dump |
|---|---|---|---|---|
| 扫描、多树预检、容量预检 | 没被碰过 | 没变 | 没变 | 仍可增量 |
| 某棵 dump | 全部在跑（dump 前停着的仍停着） | 没变 | 没变 | 全量 |
| 下沉 | 全部在跑 | 没变（改名已回退） | 没变 | 全量 |

---

## 7. 冻结窗口有多长

`stopped_ms` 从**第一棵树 dump 完**算起，到发出 `SIGCONT` 为止。里面有三样：其余各棵树的 dump、一次下沉、一轮 `clear_refs`。
所以单树时它很短，多树时它约等于"除第一棵外各棵的 dump 时间之和"，排在最前面的那棵树停得最久。

下表出自 2026-10-08 的一轮多树性能测量（新 920B；每棵树 64 MiB；1、2、3 棵树各 8 轮的中位数，单位 ms；这一轮早于最终提交，用的是当时的模板与内核；
只用来说明形状，**判定值见 [20](20-results-and-gaps.md)，不能与那里的数相减**）：

| 树数 | 增量且内存未改：`criu_dump` / `stopped` | 首次全量：`criu_dump` / `stopped` | 全部改写后的增量：`criu_dump` / `stopped` |
|---|---|---|---|
| 1 | 10.18 / 0.66 | 49.66 / 2.43 | 42.52 / 1.23 |
| 2 | 20.71 / 11.40 | 92.02 / 45.48 | 89.22 / 46.68 |
| 3 | 33.50 / 23.75 | 138.87 / 91.82 | 139.51 / 92.45 |

读法：`criu_dump` 随树数线性增长；`stopped` 在多树时大致等于"`criu_dump` 减去一棵的份额"。三棵树首次全量时，最先 dump 的那棵多停了约 92 ms，而单树时只有 2.4 ms。
还要注意 `stopped_ms` **不包含**每棵树自己 dump 期间被 CRIU 冻结的时间——那段时间进程同样不在运行，它记在 `criu_dump_ms` 里。
对一棵具体的树来说，它不在运行的总时长是"自己的 dump 时间 + 自己 dump 完之后到放开的时间"。代价模型的完整讨论在 [17](17-performance-after-fixes.md)。

---

## 8. 两条非主线

### 8.1 没有用户进程

空闲的沙箱没有用户进程树。这时 create 不是错误，而是做一个**只有文件系统的 checkpoint**：第 7～9 步、第 12～15 步、第 17～18 步都跳过，
镜像目录是空的，条目里没有树。响应里 `has_process_image=false`、`process_trees=0`、`image_bytes=0`，`timings` 里只有 `find_pids_ms`、`overlay_sink_ms`、`total_ms` 非零。

restore 到这样的 checkpoint 时，当前的用户进程照样全部被杀掉，文件系统切回去，不恢复出任何进程（[10](10-restore.md)）。

### 8.2 upper 为空

两次 checkpoint 之间如果没有写过任何文件，`SinkUpper` 发现当前 upper 是空目录，直接返回当前层栈：不改名，不发 ioctl，也不新增层（[06](06-overlay-hot-switch.md)）。
新条目与上一个条目记录的是同一组层，进程镜像则是各自的。这让"只改内存、不写文件"的负载反复 checkpoint 时层栈不会变深。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 编排 | `ckpt/service.go` — `CreateCheckpoint` |
| 健康检查 | `ckpt/service.go` — `checkReady` |
| 增量父镜像 | `ckpt/service.go` — `lastTree`、`incrementalParent` |
| 重写 soft-dirty | `ckpt/service.go` — `clearSoftDirty`、`writeClearRefs` |
| 失败收尾 | `ckpt/service.go` — `resumeAfterFailedDump`、`discardFailedCheckpoint`、`criuErrorCode`、`overlayErrorCode` |
| criu 的命令与执行 | `ckpt/criu.go` — `buildCriuDumpCmd`、`execCriuDump`、`runCriuCmd`、`logTail` |
| 进程的状态与放开 | `ckpt/pid.go` — `states`、`treePIDs`、`treeStates`、`resumable`、`resumeStoppedTree` |
| 容量预检与镜像大小 | `ckpt/capacity.go` — `estimateDumpIn`、`dirUsage` |
| 下沉 | `ckpt/overlay.go` — `SinkUpper` |
| 单测 | `ckpt/leave_stopped_test.go`、`ckpt/multitree_test.go`、`ckpt/service_failure_test.go`、`ckpt/timings_test.go` |

---

## 本章要点

1. create 的骨架是：扫描与预检（什么都没动）→ 逐棵 `criu dump --leave-stopped` → 全部停住后下沉 upper → 重写 `clear_refs` → 一起放开 → 入池、落盘。全程持操作锁。
2. create 没有不可逆步骤；任何一步失败，进程被放开、镜像被删、改名被回退，沙箱回到 create 之前的样子。
3. 多棵树逐棵 dump 得到的是"最后一棵被冻住那一刻"的自洽状态，因为先 dump 的树一直停着；树与树之间不是原子的，后 dump 的多跑了前面各棵的 dump 时间。
4. `--leave-stopped` 关掉了"dump 结束到 `clear_refs` 写完"之间进程还在写内存的窗口，那个窗口会让下一次增量静默漏页；下沉之后重写 `clear_refs` 是因为切换会让内存区域整体带脏。
5. gsd 不在 dump 之前自己发 `SIGSTOP`：CRIU 会把接管时已停止的任务恢复成停止状态，这条语义留给了用户自己停住的进程。
6. 增量按树做，父镜像是上一个成功的 create 或 restore 到的 checkpoint 里同一棵树的目录；任何一步失败、父 checkpoint 被删、restore 杀进程之后都清空，下一次全量。失败后不能退回更早的父镜像。
7. `stopped_ms` 从第一棵 dump 完算起，多树时约等于其余各棵的 dump 时间之和；没有用户进程时做只有文件系统的 checkpoint，upper 为空时不下沉。
