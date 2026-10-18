# 22 · 语义与使用边界

## 本章目标

读完本章，你应该能：

1. 列出一次 restore 会带回什么、不会带回什么，并据此判断自己的负载该把状态放在哪里；
2. 说清什么算一棵用户进程树，怎样让需要严格一致的进程成为同一棵，以及 restore 之后进程在哪些方面和原来不一样；
3. 预期打开着的文件在 checkpoint 与 restore 前后的行为，知道不在回滚范围内的进程会看到什么；
4. 把 checkpoint 历史当成一棵树来用：分支、删除、空间什么时候真正释放；
5. 估算一个沙箱能放多少 checkpoint，知道哪些事件会让 checkpoint 全部失效。

上一章（[21](21-quickstart.md)）跑通了接口。本章讲这些接口**承诺什么、不承诺什么**，全部从调用方的角度写；每一条背后的机制都在前面的章里，这里只给结论和指向。
出错时返回什么、该怎么处理，统一在 [12](12-failure-semantics.md)；全部已知限制的总表在 [15](15-known-limits.md)，本章只展开调用方每天会碰到的那几条。

---

## 1. 回滚什么，不回滚什么

gsd 做的是**进程级**回滚：它不动虚机，只在 guest 里把"用户的进程"和"根文件系统"这两样东西退回去。范围之外的东西在 restore 之后保持 restore 那一刻的样子。

| 对象 | restore 之后 | 说明 |
|---|---|---|
| uid 1000 的全部进程（及它们的子孙） | **回到 checkpoint 时刻** | 内存、寄存器、打开的文件、进程号都恢复；checkpoint 之后才起的用户进程被杀掉 |
| 根文件系统：`/home`、`/etc`、`/usr`、`/opt`、`/var`（下面列出的三个目录除外）等 | **回到 checkpoint 时刻** | 改过的内容复原，新建的消失，删掉的回来 |
| `/tmp` | **回到 checkpoint 时刻** | `/tmp` 是根文件系统的一部分，不是单独的内存盘；它占的是根盘空间 |
| `/dev/shm`、`/run` | 不回滚 | 都是内存文件系统，不在 overlay 上 |
| `/var/log`、`/var/lib/systemd`、`/var/lib/chrony` | 不回滚 | 启动时从底层磁盘单独挂出来，留给系统服务写 |
| PID 1（systemd）、envd、gsd 自己、其他以 root 运行的服务 | 不回滚，也不重启 | 它们在 restore 前后一直在跑 |
| 内核状态：网络连接表、时钟、挂载表、sysctl、cgroup 计数等 | 不回滚 | |
| checkpoint 本身（镜像、层、gsd 的记录） | 不回滚 | restore 不会让"之后创建的 checkpoint"消失 |
| 沙箱之外的一切（宿主、对端服务、已经发出去的网络请求） | 不回滚 | |

由此得到几条使用上的推论：

- **要跟着回滚的状态，放在用户进程的内存里或 overlay 上的文件里。** 放在 `/dev/shm`、`/run` 里的文件不会回滚；更糟的是，被恢复的进程如果在 checkpoint 时开着这些目录里的某个文件，而这个文件后来被删了或改了大小，CRIU 可能无法重新打开它，restore 会失败（§7）。
- **以 root 身份起的进程不在范围内。** `commands.run(..., user="root")` 起的进程既不会被保存，也不会被 restore 杀掉；它看到的文件系统会在 restore 时"从脚下换掉"（§3.3）。需要回滚的工作负载一律用默认用户起。
  例外是用户进程自己派生出来的子孙：它们不论 uid 都算在那棵树里，一起保存、一起恢复、一起被杀（例如用户进程经 `sudo` 起的子进程）。
- **日志不回滚。** `/var/log` 下的系统日志（包括 gsd 自己的日志）在 restore 之后仍然完整，排障时可以放心看（[24](24-configuration-and-observability.md)）。
- **时间不回滚。** 被恢复的进程醒来时墙钟已经前进了；依赖"上次时间戳"的逻辑（超时、租约、缓存过期）会看到一次时间跳变。
- **网络连接不保证。** 被恢复的进程在 checkpoint 时持有的 TCP 连接，gsd 没有为它启用 CRIU 的连接保存选项，这类进程的保存结果没有专门验证过（§7）。需要回滚的负载应当能在恢复之后重新建立连接。

哪些目录在 overlay 上、哪些不在，由模板的启动脚本决定，细节见 [05](05-guest-boot-and-injection.md)。

---

## 2. 进程树

### 2.1 什么算一棵树

gsd 把"uid 1000、且父进程不是用户进程"的进程当作一棵树的根，根和它的全部子孙是一棵树。落到日常使用上：

- 每个 `commands.run(..., background=True)` 是一棵树（根的父进程是 envd）；
- `create()` 那一刻还在执行的前台命令也是一棵树——它同样是 uid 1000 的进程；
- 用户进程里脱离出去、被 PID 1 收养的进程，各自成为一棵新的树（例如 `( cmd & )` 之后父 shell 退出、`setsid`、守护进程的双重 fork）；
- 没有用户进程在跑时是 0 棵树，这时的 checkpoint 只有文件系统（`has_process_image` 为 `False`）。

`CheckpointInfo.process_trees` 就是这个数。

### 2.2 多棵树时的两条限制

**树之间不是同一瞬间。** gsd 一棵一棵地保存：保存完的那棵被留在停止状态，等最后一棵保存完、文件系统也切完，才一起放行。所以每棵树的镜像是它自己被冻住那一刻的状态，先后相差一棵树的保存时间（几十毫秒量级，判定值见 [20](20-results-and-gaps.md)）。
互不通信的后台命令感觉不到这个差别；通过文件或网络协作的两棵树，恢复之后可能看到对方比自己"旧一点"。**需要严格一致的进程，用同一条命令起成一棵树**：

```python
# 两个进程是同一个 shell 的子进程 → 一棵树，同一瞬间冻住
sbx.commands.run("sh -c 'server & worker; wait'", background=True, timeout=0)
```

**树之间不能共享管道或 socket。** 每棵树单独恢复，树间的连接接不回去。一条匿名管道的读端和写端分在两棵树里，或者两棵树持有同一个 socket，`create()` 会在动手之前被拒绝（`SandboxException`，`failed_precondition`，什么都没改）；两棵树之间的 unix socket 连接由 CRIU 在保存时报错。改写办法见 [21](21-quickstart.md) 的"第一次用会遇到的三件事"。
gsd 检查不出来的一种情况是树之间共享的匿名共享内存（只可能来自共同的祖先）：保存和恢复都会成功，但恢复之后两棵树各有一份，不再共享。

多棵树的保存与恢复都是串行的，耗时随树的数量大致线性增长。原因与取舍见 [08](08-process-trees.md)。

### 2.3 restore 之后进程哪里不一样

被恢复的进程在这些方面**和 checkpoint 时相同**：进程号、内存内容、打开的普通文件及其读写位置、当前目录、环境变量、信号处理方式、子进程关系。

在这些方面**不同**：

- **父进程。** 树根原来是 envd 的子进程，恢复之后被 PID 1 收养。
- **与 envd 的连接。** 后台命令的标准输出原来经一条管道通向 envd，再流回 SDK。恢复之后这条管道接不回 envd，所以 restore 之前拿到的命令句柄不会再收到这个进程的输出。
  需要在 restore 之后还能读到的输出，让进程写文件。SDK 一侧的命令句柄在 restore 之后的其他行为（等待退出、发送输入、按句柄终止）没有专门验证过，不要依赖；要操作恢复出来的进程，按进程号用一条新命令去做。
- **停止状态。** checkpoint 时处于停止状态的进程（被 Ctrl-Z 或 `SIGSTOP` 停住的），保存之后不会被 gsd 放开，恢复出来也仍然停着。gsd 只放开它自己为了保存而冻住的那些。

### 2.4 restore 会杀掉什么

restore 的语义是"回到 checkpoint 那一刻"，所以它先把**当前全部**用户进程树杀干净，再恢复目标里的树，不管目标 checkpoint 里有没有进程：

- checkpoint 之后才起的用户进程被杀掉，不会回来；
- restore 一个只有文件系统的 checkpoint（`has_process_image` 为 `False`），结果是用户进程全部被杀、一个也不恢复；
- 正在执行的前台命令如果是默认用户起的，同样会被杀，那条命令就此中断。不要在同一个沙箱上一边跑命令一边 restore，除非你就是想中断它。

---

## 3. 打开的文件

### 3.1 被恢复的进程

被恢复的进程重新打开它在 checkpoint 时开着的文件，路径相同、读写位置相同，而文件系统此时已经回到 checkpoint 时刻，所以它看到的文件内容、映射进内存的文件内容，都是 checkpoint 时的。这是最符合直觉的一种情况，不需要调用方做什么。

### 3.2 checkpoint 之后继续写的文件

一个进程在 checkpoint 之前打开了文件，checkpoint 之后接着往里写（典型的是日志、数据库文件）。这些写入**不会**改到已经保存下来的 checkpoint：写入落在 checkpoint 之后的新一层上，checkpoint 里留的是保存那一刻的内容。

做到这一点是有代价的：checkpoint 时，凡是被以写方式打开着（或被共享可写映射着）的 overlay 上的文件，内核要给 checkpoint 留一份拷贝，拷贝期间对文件系统的其他操作会被短暂挡住。
代价与文件大小成正比——新 920B 上 2026-09-30 的专项测量是 16 / 64 / 128 MiB 的文件分别让 checkpoint 多花 12.9 / 48.0 / 101.5 ms，约 0.8 ms/MiB（判定值与条件见 [20](20-results-and-gaps.md)）。
所以**沙箱里有大文件长期以写方式打开着（例如一个几百 MiB 的数据库文件）时，每次 checkpoint 都会付这笔钱**。这个行为可以用内核参数调低，见 [24](24-configuration-and-observability.md)。
有两种文件不在此列：有多个硬链接的文件不做拷贝，checkpoint 之后经旧文件描述符的写入会改到 checkpoint 里（内核日志有告警）；`/var/log` 等不在 overlay 上的目录里的文件不涉及。

### 3.3 不在回滚范围内的进程

root 起的服务、envd 的文件接口这类进程不会被杀也不会被恢复，但它们脚下的文件系统在 restore 时被换掉了。内核保证这种切换是安全的（不会崩、不会损坏数据），但不保证这些进程看到一致的视图。具体是：

| 该进程在 restore 之前持有的东西 | restore 之后 |
|---|---|
| 一个打开着的**文件**（文件描述符） | 继续读到的是**被丢弃的那条时间线**上的内容，而不是 restore 之后的内容；经它继续写入的结果不要依赖 |
| 一个打开着的**目录**或以某个目录为**当前目录**，且内核能把它接回目标 checkpoint 里的同一个目录 | 看到目标 checkpoint 里这个目录的内容 |
| 一个已经**陈旧**的当前目录（它只属于被丢弃的时间线，接不回目标 checkpoint） | 从这个目录出发的相对路径写操作返回 `ESTALE`（"Stale file handle"）；查找可能返回 `ENOENT` |
| 重新按绝对路径打开任何东西 | 看到 restore 之后的文件系统，一切正常 |

哪些目录能接回、哪些会变陈旧，判定条件在 [07](07-overlay-concurrency-and-fixes.md) 的"目录与 inode 缓存跨 restore"一节；调用方不需要记住条件，只需要知道这两种结果都可能出现。

给调用方的建议只有两条。第一，不要让范围外的长寿进程长期持有 overlay 上的文件描述符或把当前目录放在会被回滚掉的目录里；需要时在 restore 之后重新打开、重新 `cd`。
第二，SDK 的 `files.*` 和每一条新的 `commands.run()` 都是按绝对路径重新进入文件系统的，不受影响——受影响的只是那些跨过 restore 一直开着的句柄。
`ESTALE` 的由来与这套语义的取舍见 [07](07-overlay-concurrency-and-fixes.md)。

另外一个只影响排障的现象：`/proc/mounts` 里根文件系统那一行始终显示开机时的层，不随 checkpoint / restore 变化；当前真实的层栈要看 gsd 的状态文件（[24](24-configuration-and-observability.md)）。

---

## 4. 历史是一棵树

### 4.1 分支

restore 不删除任何 checkpoint。回到较早的 `A` 之后，比它新的 `B` 仍然在 `list()` 里，可以再 restore 到 `B`；也可以从 `A` 出发做别的事，再 `create()` 出 `C`，这时 `B` 和 `C` 是从 `A` 分出去的两支。任意两个 checkpoint 之间都可以直接 restore，不需要经过中间节点。

`parent_checkpoint_id` 记录的是**进程镜像的增量关系**：这个 checkpoint 的进程镜像是在哪个 checkpoint 的基础上只存了差异。它多数时候和"历史上的前一个点"一致——增量的父镜像总是取自上一次成功的 `create()`，或上一次 `restore()` 到的那个 checkpoint——但不完全等同：
没有用户进程的 checkpoint 没有父；进程树是新起的（或上一次操作失败过）时这一次存全量，也没有父。所以如果你的应用需要一棵完整的"历史树"，自己在调用方记父子关系，不要依赖这个字段。

### 4.2 删除与空间释放

`delete()` 之后这个 checkpoint 立刻从 `list()` 里消失，再 restore 它会得到 `NotFoundException`。但它占的空间不一定立刻释放，要分两部分看：

- **进程镜像（占 guest 内存）。** 如果还有别的 checkpoint 的镜像是以它为父做的增量，它的镜像必须留着（否则子节点恢复不出来），此时它只是"隐藏"；等依赖它的子节点全部删掉，它才被真正删除。一个 checkpoint 没有子节点时，删除立即释放。
  实用规则：**想腾内存，从最新的往旧的删，或者把一整条增量链一起删。** 只删链条中间或最老的那个，释放不了什么。
- **文件系统层（占根盘）。** 层按引用回收：一层只要还在当前层栈里，或者还被某个 checkpoint（包括隐藏的）引用，就留着；没人引用了才删。被 restore 丢弃掉的那段写入（restore 之前、最后一次 checkpoint 之后写的东西）在 restore 完成后由后台清掉，不需要调用方管。

删掉一个 checkpoint 还有一个副作用：如果它正是下一次增量要用的父镜像，下一次 `create()` 会对相关的进程树存全量（多花一次全量的时间和空间），之后恢复增量。这是有意的——用户删它是希望它尽早消失，不应该再让新的 checkpoint 挂在它下面。

机制见 [13](13-state-concurrency-capacity.md)。

---

## 5. 容量

一个沙箱能放多少 checkpoint，受三样东西限制，三样都在 guest **里面**，宿主上看不到也不占宿主的盘。

**进程镜像占 guest 内存。** 镜像放在一个上限 2 GiB 的内存文件系统上，写进去多少就占掉多少 guest 内存。`create()` 在保存之前先估算：把要保存的全部进程的匿名内存与共享内存加起来乘以 1.2，如果这个数超过内存文件系统的剩余空间，或者超过 guest 当前的可用内存（`MemAvailable`），就拒绝（`NotEnoughSpaceException`），不会等到写一半才失败。
估算偏保守：增量保存也按全量估，父子进程共享的页会被重复计。也就是说，**一个占 500 MiB 匿名内存的进程，要求 guest 至少还有约 600 MiB 可用内存才允许 checkpoint，哪怕这一次实际只会写出几 MiB 的增量**。给需要 checkpoint 的沙箱配内存时，按"工作负载的内存 × 2 以上"留余量。
每个 checkpoint 实际占了多少看 `image_bytes`。

**文件系统层占根盘。** 两次 checkpoint 之间写过的文件构成一层，层留在 guest 的根盘上，直到引用它的 checkpoint 都被删掉。`/tmp` 也在根盘上。根盘写满时后续的 checkpoint、restore 以及普通的文件写入都会失败，所以长跑的沙箱要留意根盘余量（查看办法见 [25](25-troubleshooting.md)）。

**层栈深度有上限。** 文件系统每做一次（有文件改动的）checkpoint 就多一层，而内核切换层栈的接口有长度限制，按现在的命名大约能放 190 层（判定值见 [20](20-results-and-gaps.md)）。到顶时 `create()` 返回 `NotEnoughSpaceException`，信息里有 `layer stack too deep`；gsd 保证不会建出一个将来恢复不了的 checkpoint。被这样拒绝时文件系统和 checkpoint 列表都没有变，但进程已经被保存过一次（镜像随即丢弃），所以下一次成功的 `create()` 存的是全量（[12](12-failure-semantics.md)）。
这个上限针对的是**当前所在的这条链**的深度，不是 checkpoint 的总个数：restore 到一个较早的 checkpoint 之后，深度回到那个点，可以继续 create。两次 checkpoint 之间没有任何文件改动时不产生新层。

长跑时的建议：

- 定期删不再需要的 checkpoint，从新往旧删或整链删（§4.2）；
- 不要在一条链上无限叠加。需要成百上千步的"每步一个 checkpoint"时，隔一段就回到一个较早的基准点重新开始，或者只保留最近若干个；
- 把 `NotEnoughSpaceException` 当成正常的背压信号处理：删旧的，然后重试。它保证什么都没改。

容量预检与层栈上限的实现在 [13](13-state-concurrency-capacity.md)。

---

## 6. checkpoint 能活多久

checkpoint 的全部数据（镜像、层、gsd 的记录）都在沙箱里。它的生命周期如下：

| 事件 | checkpoint |
|---|---|
| gsd 进程重启（崩溃后被 systemd 拉起，或手工 `systemctl restart gsd`） | **保留**。gsd 每次操作结束都把状态写进状态文件，重启后加载，层栈、镜像、增量关系都接得上 |
| restore 半途失败后 gsd 重启 | 保留，"只能先 restore"的限制也保留 |
| 沙箱销毁（`kill()`、超时回收） | **全部消失**。没有任何东西留在宿主上 |
| guest 操作系统重启 | 全部消失：镜像所在的内存文件系统和内核里的层栈一起重置。正常使用中沙箱不会重启 |
| 沙箱被暂停再恢复（pause / resume） | 按设计应当保留：暂停时 gsd 连同它的内存和内存文件系统一起被冻结，恢复后原样继续，不需要重新加载。**这一条只有代码层面的依据，没有做过专门的现场验证**，在得到验证之前不要把它当作保证 |
| 从这个沙箱做模板快照、再起新沙箱 | 未验证，不要依赖 |

checkpoint 不能导出到沙箱之外，也不能拿到另一个沙箱里 restore。需要跨沙箱或持久保存的场景，用整机级回滚或平台自己的快照能力，两者的分工见 [18](18-process-vs-machine-level.md)。

---

## 7. CRIU 保存不了的进程

进程的保存与恢复由 CRIU 完成，gsd 用的是固定的一组选项，不为特殊进程放宽。进程用了 CRIU 在这组选项下不支持的东西时，`create()` 会失败——抛 `SandboxException`，信息形如 `Code.internal: criu dump failed: …`，后面带着 CRIU 日志的最后一段，从中能看到是哪个进程的哪样东西出了问题。失败时什么都没改：已经冻住的进程被放开，这次写出的镜像全部丢弃。

已经确认的情况：

- **标准输出接在 journald 上的进程**（例如用 `systemd-run` 起的）保存不了，CRIU 报 `External socket is used`。经 SDK 的 `commands.run()` 起的进程不是这种情况。
- **一端在进程树之外的 unix socket 连接**同样报 external socket。连接到沙箱里某个 root 服务的 unix socket 的用户进程属于这一类。
- 被恢复的进程开着 `/dev/shm`、`/run` 里的文件，而文件在 checkpoint 之后被删或改了大小，**restore** 时可能失败（§1）。这种失败发生在进程已经被杀之后，表现为 `CheckpointRestoreException`。

没有一份"哪些特性能保存、哪些不能"的完整清单，也没有系统测过带活动 TCP 连接、GPU、容器运行时等负载的进程（[20](20-results-and-gaps.md) 的缺口表）。上新类型的负载之前，先在一个沙箱里实际 create、restore 一次，看它能不能过。

---

## 8. 并发与超时

- 同一个沙箱上的 `create`、`restore`、`delete` 由 gsd 串行执行，后到的请求排队等待，不会被拒绝；`list` 不排队。从多个线程或多个客户端同时调用是安全的，但顺序由到达先后决定，调用方如果在意顺序要自己协调。
- gsd 给每棵树的 CRIU 保存和恢复各设 60 秒的时限，一次操作里全部树合计 180 秒。保存超时：`create()` 抛 `TimeoutException`，进程已被放开，什么都没留下。恢复超时：此时进程已经被杀，按 restore 半途失败处理（`CheckpointRestoreException`）。
- SDK 一侧的 `request_timeout` 到期只是 SDK 不等了，gsd 仍然会把正在做的操作做完；这时抛出的是底层 HTTP 库的超时异常，不是上一条的 `TimeoutException`（三种"超时"的区分见 [12](12-failure-semantics.md)）。这时调用方不知道结果，用 `list()` 看 checkpoint 是否已经出现，或者直接再 restore 一次。
- gsd 不重试，SDK 也不按错误自动重试（连接层的一处例外见 [12](12-failure-semantics.md)）。除了 `CheckpointRestoreException` 之外，所有失败都保证"什么都没改"，可以安全地由调用方决定重试。

错误码、异常类与处理办法的完整对照在 [12](12-failure-semantics.md)。

---

## 代码位置

| 内容 | 位置 |
|---|---|
| 哪些目录在 overlay 上、哪些单独挂出 | `packages/orchestrator/internal/template/build/core/rootfs/gsdfiles/overlay-init.sh.tpl`（文件头注释与 Step 4、Step 7） |
| 用户进程与树根的认定 | `userRootsIn`（`ckpt/pid.go:259`），`userUID`（`ckpt/pid.go:17`） |
| 树间管道与 socket 的预检 | `crossTreeShared`（`ckpt/shared.go:49`），文件头注释 |
| create 的保存顺序、停止状态的处理 | `CreateCheckpoint`（`ckpt/service.go:163`） |
| restore 先杀后恢复、两段式 | `RestoreCheckpoint`（`ckpt/service.go:463`），`killTrees`（`ckpt/criu.go:215`） |
| CRIU 的固定选项 | `buildCriuDumpCmd`（`ckpt/criu.go:51`），`buildCriuRestoreCmd`（`ckpt/criu.go:100`） |
| 隐藏式删除与连带删除 | `remove`（`ckpt/pool.go:151`），`DeleteCheckpoint`（`ckpt/service.go:764`） |
| 容量预检的估算 | `estimateDumpIn`（`ckpt/capacity.go:72`），文件头注释 |
| 层栈深度预检 | `checkOverlayOptionsLen`（`ckpt/overlay.go:540`），`SinkUpper`（`ckpt/overlay.go:125`） |
| 状态持久化与生命周期论证 | `ckpt/state.go` 文件头注释，`loadState`（`ckpt/state.go:341`） |
| 可写打开文件在 checkpoint 时的处理 | 补丁 `0003-overlayfs-hot-switch-fixes.patch` 的 `ovl_ckpt_want_move`、`ovl_ckpt_move_upper` |

---

## 本章要点

- 回滚的是 uid 1000 的进程树和 overlay 上的根文件系统（含 `/tmp`）；`/dev/shm`、`/run`、`/var/log`、root 起的进程、内核状态、时间和沙箱之外的一切都不回滚。要跟着回滚的状态放在用户进程内存或 overlay 上的文件里。
- 每个后台命令是一棵树，树与树之间依次冻住、不能共享管道或 socket；需要严格一致的进程用同一条命令起成一棵树。
- restore 先杀掉当前全部用户进程再恢复目标里的树；恢复出来的进程进程号不变，但父进程变成 PID 1，通向 envd 的输出管道接不回去，输出要写文件。
- checkpoint 之后经旧文件描述符的写入不会改到 checkpoint，代价是 checkpoint 时为可写打开的文件留拷贝，与文件大小成正比；范围外进程跨 restore 持有的文件描述符看到被丢弃的时间线，陈旧的当前目录上的写返回 `ESTALE`。
- 历史是一棵树，restore 不剪枝；删除有增量子节点的 checkpoint 只是隐藏，内存要等子节点删完才释放，所以从新往旧删或整链删。
- 镜像占 guest 内存（2 GiB 上限，按全量乘 1.2 预检），层占根盘，一条链最多约 190 层；`NotEnoughSpaceException` 是正常的背压信号，保证什么都没改。
- gsd 重启后 checkpoint 仍在；沙箱销毁或 guest 重启后全部消失；暂停再恢复之后的行为只有代码依据、未经现场验证。
- CRIU 保存不了的进程（标准输出接 journald、树外的 unix socket 等）会让 `create()` 失败并带回日志尾部；没有完整的兼容清单，新负载先实测一次。
