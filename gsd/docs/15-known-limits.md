# 15 · 已知限制与未闭合项

## 本章目标

读完本章，你应当能回答：

1. gsd 现在**做不到**什么，哪些是设计上的边界、不打算改？
2. 哪些现象见到过、但没有查清原因？
3. 代码里有、但没有在真机上走到过的分支有哪些？
4. 内核侧还有哪些记录在案、没有处理的地方？
5. 遇到其中某一条时，手头有什么规避办法？

---

上一章（[14](14-issue-resolution.md)）是"修掉了什么"，本章是它的另一半：**没修掉的、不打算修的、没来得及验的**。
本章是全书唯一的已知限制表，别的章提到限制时链接到这里，不另写一份。评审时请把本章和 [20](20-results-and-gaps.md) 的缺口表一起读：
本章按"行为"列，20 章按"测试覆盖"列，两者有交集但角度不同。面向使用者的边界说明（能回滚什么、怎么组织进程）在 [22](22-semantics-and-limits.md)，
遇到具体症状怎么处置在 [25](25-troubleshooting.md)，后续处理的建议和优先级在附录 [B](B-extending.md)。

---

## 结论先行

- **设计如此的 12 项**（§1）：其中对使用方影响最大的是三条——树与树之间不能共用管道或 socket；多棵树的耗时随树数线性增长且树间不是原子的；
  只支持 arm64 guest。
- **见过但没查清的 3 项**（§2）：新沙箱首个请求偶发约 5 秒、一组压测里 create 返回过 2 次 `internal`（同一个沙箱的第 174、177 轮）、极低概率的单发 `ENOTEMPTY`。
  三项都不损坏数据，都没有稳定的复现办法。
- **没有在真机上走到的分支 9 项**（§3）：集中在多棵树的异常路径和注入开关的关闭态。除 T-09 只有代码注释的论证外，其余都**只有单测、没有现场走过**。
- **内核里记录在案的 4 项**（§4）：`/proc/mounts` 的显示、不检查 lower 所在的文件系统、被丢弃时间线的有界残留，以及一条已在最后一轮处理、为对照旧结果而保留的记录。
- 950：未部署、未测。本章所有"现场"都指新 920B（192.168.45.47）。

表里"状态"一列的取值：**设计如此**（是取舍的结果，不计划改）、**未查**（见过现象，原因不明）、**未测**（有代码、有单测，没有现场结果）、**未处理**（知道问题在哪，没有改）。

---

## 1. 设计如此的限制

| 编号 | 现象 | 影响 | 原因 | 规避办法 | 状态 |
|---|---|---|---|---|---|
| L-01 | 两棵用户进程树之间共用一条管道（读端和写端分在两棵树里）或持有同一个 socket 时，create 返回 `failed_precondition`，什么都没动。连接两棵树的 unix socket（两端是不同的 inode）由 CRIU 在 dump 时报 external socket，create 返回 `internal` 并带日志尾部 | 这样的进程组合做不了 checkpoint | 每棵树单独 dump、单独 restore，跨树的连接接不回去。实测过"只有一端在树里的匿名管道"：dump 和 restore 都成功，但恢复后管道静默断开（读端读到 EOF），所以必须在 dump 前拒绝。gsd 不给 CRIU 加放宽外部资源的选项 | 需要互相通信的进程由同一个命令起成一棵树。只是共同继承、另一端在树外的管道（例如几个后台命令共用的通向 envd 的标准输出）不受影响 | 设计如此 |
| L-02 | 两棵树之间共享的匿名共享内存（只可能来自共同祖先）检查不出来；restore 之后各是各的一份 | 依赖这块共享内存通信的两棵树，恢复后互相看不到对方的写入 | 预检只看 `/proc/<pid>/fd`，匿名共享映射不在其中 | 同上：放进同一棵树 | 设计如此 |
| L-03 | 多棵树时 create 和 restore 的耗时随树的数量线性增长；树之间不是原子的 | 树多时冻结窗口变长：最先 dump 的那棵要一直停到最后一棵 dump 完。后 dump 的树比先 dump 的多跑了一小段（一棵树的 dump 时间），通过文件或网络协作的两棵树恢复后看到的对方状态可能略旧 | 每棵树各做一次 CRIU dump 和 restore，串行执行（[03](03-goals-and-tradeoffs.md) 的取舍二） | 需要严格一致的进程放进同一棵树；控制同时存在的后台命令数量。量级见 [17](17-performance-after-fixes.md)，判定值见 [20](20-results-and-gaps.md) | 设计如此 |
| L-04 | checkpoint 时，每个"以写方式打开着"的 upper 文件要被整份拷贝一次，约每 MiB 0.8 ms，拷贝期间整个 overlay 上的文件操作都被挡住 | 开着大文件（例如数据库文件）做 checkpoint 会拉长 create，并让沙箱里所有文件操作停顿相应的时间 | 这类文件在 checkpoint 之后的写不能落进 checkpoint 层，所以要把原文件留在 upper、层里放拷贝；拷贝在切换的排它锁内做（[07](07-overlay-concurrency-and-fixes.md) "打开的文件跨切换的语义"一节） | 内核模块参数 `overlay.ckpt_move_open`：设为 `1` 只搬被映射的文件，设为 `0` 不搬（此时被映射文件的写会进入 checkpoint，不随 restore 回滚）。checkpoint 前关闭不需要的大文件 | 设计如此 |
| L-05 | 硬链接数大于 1 的文件如果以写方式打开着，checkpoint 时不搬；之后经原 fd 或映射的写会进入 checkpoint，restore 回滚不掉。内核打一行限速告警 `checkpoint: could not move open file ...` | 该文件在这个 checkpoint 里的内容会被后续写入改掉 | 它的另一个名字仍然把 inode 留在沉下去的层里，搬走一个名字没有意义 | 避免对"多硬链接且长期开着写"的文件依赖回滚；dmesg 里有告警可查 | 设计如此 |
| L-06 | restore 之后，不被 gsd 杀掉的进程（root 服务、envd 代为执行的操作）如果 cwd 停在一个在目标 checkpoint 里不存在的目录，从这个 cwd 出发的写、创建、rename、删除返回 `ESTALE`。cwd 停在目标里存在的目录时，持有者看到的是目标时间线的内容；持有**文件**的进程继续看到旧时间线的内容 | 范围外进程在 restore 之后可能遇到 `ESTALE`，或者手里的 fd 与按路径重新打开看到的内容不同 | 被丢弃时间线上的对象只读保留，不允许把它们 copy-up 回当前 upper，否则被回滚的内容又被写回来（[07](07-overlay-concurrency-and-fixes.md) "修复之后，范围外的进程会看到什么"一节） | 长期运行的 root 服务不要把 cwd 留在会被回滚的目录里；遇到 `ESTALE` 重新 `chdir` 到绝对路径、重新打开文件 | 设计如此 |
| L-07 | `/proc/mounts` 和 `mount` 命令里 overlay 的 `lowerdir`、`upperdir`、`workdir` 一直显示开机时的三个目录，不反映当前层栈 | 只影响显示，容易误导排障的人 | 切换不更新挂载选项字符串；CRIU 要读 mountinfo，没有去动它的输入 | 当前层栈以 gsd 的 `state.json` 为准（[13](13-state-concurrency-capacity.md)、[24](24-configuration-and-observability.md)） | 设计如此 |
| L-08 | 只支持 arm64 guest | x86_64 的 guest 上 gsd 的进程回滚不可用 | x86_64 的内核配置没有打开 `CHECKPOINT_RESTORE`（`MEM_SOFT_DIRTY` 随之不可用）；补丁里的 soft-dirty 只为 arm64 实现 | 无。[03](03-goals-and-tradeoffs.md) 列为非目标 | 设计如此 |
| L-09 | `HOST_GSD_PATH` 只认小写的 `off` 表示关闭。`OFF`、`Off` 会被当成相对路径，gsd 仍然是开的，建模板时报 `error reading gsd file (HOST_GSD_PATH)` | 写错大小写时建模板失败（不会静默建出错误的模板） | 关闭值是与字符串 `off` 的精确比较（`makePathsAbsolute`，`packages/orchestrator/internal/cfg/model.go:47`）；空值不能用来关闭，因为环境变量库对空值会回落到默认值 | 写小写 `off` | 设计如此 |
| L-10 | `/run`、`/dev/shm`、snapshot store，以及从 ext4 bind 进来的 `/var/log`、`/var/lib/systemd`、`/var/lib/chrony` 不回滚：restore 之后这些目录里是 restore 那一刻的内容 | 被恢复的进程如果开着 `/dev/shm` 或 `/run` 里后来被删除、改过大小的文件，CRIU restore 可能失败（此时已过提交点，返回 `data_loss`，见 [12](12-failure-semantics.md)） | 这些目录不在 overlay 上（tmpfs 或直接来自 ext4），热切层管不到它们 | 需要随进程回滚的文件放在 overlay 上的目录里（`/tmp`、`/home` 等都可以） | 设计如此 |
| L-11 | 标准输出接到 journald 的进程无法 dump，create 返回 `internal`，日志尾部有 `External socket is used` | 用 `systemd-run` 之类方式起、输出进 journal 的用户进程做不了 checkpoint | 通向 journald 的 socket 另一端在树外，是 CRIU 的 external socket 限制；与 L-01 同理，gsd 不加放宽选项 | 把输出重定向到文件，或经 SDK 的后台命令起进程（它的输出走通向 envd 的管道，不受影响） | 设计如此 |
| L-12 | 同一条链上的层栈深度有上限，约 190 层；超过时 create 返回 `resource_exhausted`（`layer stack too deep`） | 沿同一条历史连续做约 190 次"有文件改动"的 checkpoint 之后不能再 create | ioctl 的选项串上限是一页（4 KiB），每层的路径都要写进去；gsd 用短层名把上限推到这个量级，并在 create 时预检，不会建出恢复不了的 checkpoint（[13](13-state-concurrency-capacity.md)） | restore 回一个较浅的 checkpoint 之后可以继续；长跑时定期删除旧 checkpoint 并从较浅的点重新开始。没有层压缩功能 | 设计如此 |

另有五条量级较小、但部署和长期运行时要知道的：

| 编号 | 现象 | 影响 | 原因 | 规避办法 | 状态 |
|---|---|---|---|---|---|
| L-13 | gsd 把 CRIU dump 和 restore 的详细输出打进 journal，`/var/log` 在根盘上持续增长，直到 journald 自己的上限 | 根盘占用随 checkpoint 次数上涨一段后稳定。实测量级（新 920B，2026-10-08，回收修复当轮，create→restore→delete 循环口径）每轮约 0.1 到 0.7 MB，8 到 10 MiB 一跳。判定值见 [20](20-results-and-gaps.md)。它**不是**层泄漏，但早期曾被误认成层泄漏 | CRIU 以最详细的日志级别运行，完整输出经 gsd 的标准错误进 journal；`/var/log` 从 ext4 bind 进来，不随 restore 回滚 | 调小 guest 里 journald 的容量上限；评估根盘占用时把 `/var/log` 单独扣除 | 设计如此 |
| L-14 | 没有自动清理：checkpoint 的镜像（占 guest 内存，上限 2 GiB 的 tmpfs）和层（占根盘）要调用方自己删。有增量子节点的 checkpoint 被删时只是隐藏，空间要等子节点全部删掉才释放 | 长期不删会先撞上容量预检（`resource_exhausted`） | 没有做保留策略；增量镜像依赖父镜像，父镜像必须活得比子久 | 调用方定期 delete；用 list 返回的镜像大小做预算 | 设计如此 |
| L-15 | 在"带 gsd"模式下建的模板与"不带 gsd"模式下建的模板之间互相派生（以一种模式建的模板为基础、在另一种模式下继续构建）没有处理 | 行为未定义 | 开关只保证两种模式的 base 层缓存不互相复用（缓存键里多一个 `"gsd"`）；跨模式派生涉及 init 和根文件系统布局的切换，没有做。本条没有对着模板派生的代码逐步核实，属未验证 | 同一台 orchestrator 上不要混用两种模式建有派生关系的模板 | 未处理、未验证 |
| L-16 | 建模板时的自检不验证 guest 内核是否带补丁：它只看 `gsd.service` 是否 active、`/health` 是否为 `ok`、`/usr/sbin/criu` 是否 4.x | `HOST_KERNELS_DIR` 指向没打补丁的内核时模板照样建成功，要到第一次 create 才失败 | 自检脚本（`gsdCheckScript`）不做 checkpoint，也不读内核版本串；gsd 的 `/health` 不探测切层 ioctl 是否存在 | 建完模板按 [23](23-deployment.md) 的"部署后自检"到 guest 里核对内核版本串，再跑 [26](26-acceptance-runbook.md) 的第一个验收脚本；症状与处置见 [25](25-troubleshooting.md) | 未处理 |
| L-17 | journald 默认按服务单元限流；CRIU 的详细输出把额度用完后，gsd 自己的 JSON 日志会被一起丢掉 | 连续做十来次 checkpoint 之后 gsd 日志可能残缺，排障和验收时缺行 | CRIU 的输出经 gsd 的标准错误进同一个单元的 journal，与 gsd 的日志共用限流额度。依据是验证脚本 `verify_clear_refs_race.py` 的处理（它在开头给 `gsd.service` 关掉限流，脚本注释记录默认额度下约第 10 轮起丢日志）；没有专门的结果文件 | 需要完整日志时先给 `gsd.service` 加关闭限流的 drop-in（[24](24-configuration-and-observability.md)） | 设计如此 |

---

## 2. 见过但没有查清的现象

| 编号 | 现象 | 见到的情况 | 已知的 | 影响与规避 | 状态 |
|---|---|---|---|---|---|
| U-01 | 新沙箱上的**第一个**请求偶发约 5 秒 | 新 920B，2026-10-08，`probe_first_rpc.py`：经 SDK 的首个 gsd 请求，新模板 20 次里 1 次、旧一代模板 20 次里 2 次是 5.01 秒；有一次连首个 envd 命令也是。更早的性能对照里也见过新沙箱首个请求偶发超时，是否同一现象没有确认。`verify_multitree.py` 的"慢调用"统计（客户端墙钟比 gsd 自报多 1.5 秒以上）在 2026-10-08 的确认验证和 2026-10-09 的复核里各记到 1 次，都是 create：墙钟 1861 ms 而 gsd 自报 34.9 ms，墙钟 1640 ms 而 gsd 自报 99.9 ms；多出的时间同样在请求到达 gsd 之前，归入这一类 | 在 guest 里直连 gsd 的 40 次和所有第二次请求都正常；gsd 自报的耗时只有几毫秒到几十毫秒。所以延迟在宿主侧（代理到 guest 的首次连接），不在 gsd；新旧 gsd 都有，不是修复引入的。原因没有查 | 只影响首个请求的延迟，不影响结果。对首个请求给足超时，或先发一个无关紧要的请求 | 未查 |
| U-02 | create 返回 `internal`（`criu dump failed: exit status 1`） | 新 920B，2026-10-08，一个候选内核（只含 whiteout 缓存与 work 目录 trap 两项修复、未入库的中间构建）上的全部操作压测，同一个沙箱在第 174、177 轮各出现一次；其余操作、worker、dmesg 全部正常，随后的 create 成功 | 当时压测脚本只留了报错的前 200 个字符，看不到 CRIU 的出错行，沙箱也已销毁。之后同一随机种子在下一个候选内核上重放 5 次、正式内核上 27 组、调试内核上 4 组同类压测（合计约四千次 create）以及多树、删文件密集各两次都没有再出现；最终提交的确认性验证里也没有。没有证据把它归到内核改动，也不能排除 | create 失败时什么都没动，重试即可。压测脚本已改为在遇到这类失败时保存完整报错和 gsd 日志，再出现就能定位 | 未查 |
| U-03 | rename 修复之后仍有单发的 `ENOTEMPTY` | 新 920B，2026-10-08，约 95 组、每组 300 轮的并发压测里共 2 次：一次记在 rename 上，一次记在追加写上 | 后一次在 rename 路径上没有任何诊断输出，推断来自目录 copy-up：真实的 upper 里已经有这个目录，而 overlay inode 认为还没有 upper。是否在更早的内核上就有，分不开（更早的单发都记在 rename 上，机制区分不了） | 操作返回错误、不改动数据，50 ms 后视图一致。范围外进程遇到时重试即可（[07](07-overlay-concurrency-and-fixes.md) "restore 与 rename、unlink、rmdir 的竞争"一节） | 未查 |

---

## 3. 没有在真机上走到的分支

下面这些路径代码里有，但没有现场结果：T-01 ～ T-08 **只有单测、没有现场走过**，T-09 连单测也没有。它们多数是异常路径，正常使用走不到。

| 编号 | 分支 | 现有依据 | 走到时的预期行为 |
|---|---|---|---|
| T-01 | 多树预检：两棵树持有同一个 socket（同一个 inode） | 单测；现场只验证了"共用管道被拒"和"共用通向 envd 的标准输出不被拒" | create 返回 `failed_precondition`，信息里写明哪两棵树、哪个 socket |
| T-02 | 三棵树 restore 时**中间**一棵失败 | 单测；现场破坏的是排在最后的那棵树的镜像，错误信息里"尚未尝试的树"为空 | 返回 `data_loss`，信息里写明哪几棵已恢复并留着运行、哪棵失败、哪几棵没尝试 |
| T-03 | 多树时的 dump 超时，以及全部树合计 3 倍于单棵时限的上限 | 单测；现场只测了单树的超时 | 返回 `deadline_exceeded`，已停住的树被放开，失败的镜像目录被删 |
| T-04 | 某棵树 dump 失败后，**排在它前面**、已经 dump 完停着的树被放开 | 单测；现场只在 unix socket 的探索里见到"失败的是第一棵"的情况 | 前面的树收到 SIGCONT 继续运行，整个 checkpoint 的镜像删掉，下一次全部全量 |
| T-05 | 容量预检按多棵树的内存之和估算 | 单测；现场的容量预检用的是单树负载 | 估算值是全部待 dump 进程的匿名内存与共享内存之和乘 1.2 |
| T-06 | 加载版本 1 的 `state.json` | 单测 | 按一棵树处理，加载后的第一次 dump 做全量 |
| T-07 | CRIU 在 dump 中途被 SIGKILL 或自己崩溃之后的防御性 SIGCONT | 单测（超时被杀的路径现场走过，走的是先 SIGTERM 的那一支） | 只对"现在停着、dump 前没停"的进程发 SIGCONT |
| T-08 | `HOST_GSD_PATH=off`（关闭注入）真实建模板 | 单测；另把配置解析的测试编成二进制、在真实的进程环境变量下跑过六种取值；读过五个受开关控制的代码点 | 建模板路径与上游相同，模板里没有 gsd 和 overlay-init |
| T-09 | 沙箱 pause 再 resume 之后的 checkpoint 与 restore | 只有代码注释的论证：gsd 进程连同内存被冻结再恢复，不重启、不重新加载状态（[13](13-state-concurrency-capacity.md)） | 与 pause 之前无差别 |

默认路径（不设 `HOST_GSD_PATH`、gsd 放在 `/fc-gsd/gsd`）下的真实建模板不在这张表里：2026-10-08 的确认性验证当时没有走到它（那时切栈脚本显式设置这个变量），
2026-10-09 按运行时布局落位后已在新 920B 上现场确认——orchestrator 的进程环境里没有这个变量，建出的模板里 gsd 的 sha256 与 `/fc-gsd/gsd` 相同，随后的功能验证通过（[20](20-results-and-gaps.md) 的"落位后的复核"一节）。
关闭态（T-08）仍然没有真实建过模板。

此外，调试内核（KASAN、lockdep）上只跑过压测和内核问题的复现，没有跑功能验证；更大规格的 guest、三棵以上的树、小时级的长时间运行都没有测。
这几条属于测试覆盖范围，列在 [20](20-results-and-gaps.md) 的缺口表里。

---

## 4. 内核里记录在案的

| 编号 | 内容 | 影响 | 状态 |
|---|---|---|---|
| K-01 | ioctl 不检查各个 lower 是否与 upper 在同一个文件系统上，新层一律按"同一个文件系统"登记 | lower 在别的文件系统上时，设备号与 inode 号的映射会不对。gsd 的所有层都在同一个 ext4 上，不会触发；只有绕过 gsd 直接发 ioctl 才会遇到。（upper 换文件系统已被 ioctl 拒绝，返回 `-EXDEV`。） | 未处理 |
| K-02 | 被 restore 丢弃的时间线里，仍被某个进程拿着的对象（陈旧 cwd、打开的文件）会让它所在的那一代层数组整组留在内核里；持有者放手后还要再过一到两次切换才释放。最后一次切换换下的对象留到下一次切换或卸载 | 内核内存和被删层的磁盘块有一段有界的滞留，量与持有量相当（实测 20 个各 4 MiB 的被持有文件对应约 82 MB，放手并再切换几次后归零；新 920B，2026-10-08，`probe_leak.py` 持有场景口径，判定值见 [20](20-results-and-gaps.md)） | 设计如此（有界） |
| K-03 | 一个一直被拿着、又被多次"copy-up 加切换"的普通文件，每次多保留一个旧 lower 的引用，直到它的 overlay inode 销毁 | 极少量内存；只对长期开着且反复被修改的文件有意义 | 设计如此（有界） |
| K-04 | ioctl 的失败路径上，新 work 目录的 dentry 比它的 trap 早放几行 | 窗口内没有机会让那个 inode 被回收再复用，实际不构成问题；与原有的另一对写法相同，记一笔 | 未处理 |

**一条为对照旧结果而保留的记录。** 较早一轮的结果文件里记过这样一个潜在问题：overlay inode 的哈希键是真实 inode 的内存地址；
纯 upper 的目录在 restore 后失去 upper，如果正被陈旧 cwd 拿着就仍挂在哈希里，键指向的真实 inode 随后被释放，地址被别的真实 inode 复用后可能误命中。
当时只做了记录。**这个情形在最后一轮内核修复里已经处理**：restore 时失去 upper 的目录，如果是按这个 upper 哈希的，一并从 inode 哈希里摘出
（`ovl_ckpt_apply_item`，见 [07](07-overlay-concurrency-and-fixes.md) "属于 work 目录的缓存"一节）；按 lower 哈希的目录照旧保留，目录复活要靠它。
"键是内存地址"这个上游的设计没有变，今后给内核补丁加任何新的缓存时都要按附录 [B](B-extending.md) 的不变量检查它的键由谁钉住。

---

## 5. 这些限制从哪来

给想核对原始记录的读者（结果目录在 `deltabox/e2b/results/` 下）：

| 条目 | 出处 |
|---|---|
| L-01、L-02、L-03、L-10、L-12、L-14 | `packages/gsd/README.md` 的"回滚范围与容量""多棵用户进程树""使用限制" |
| L-01 的管道静默断开实测、U-01、T-01 ～ T-06、T-08 | `gsd-final-20261008` 的 `summary.txt` 一、七、九 |
| L-04、L-05、L-06 | `gsd-fix4-20260930` 的 `summary.txt` 二 B、五 |
| L-07、K-01、K-04、U-02 | `gsd-fix6-20261008` 的 `summary.txt` 三、九 |
| L-09、T-08 | `gsd-release-20261008` 的 `summary.txt` 四 |
| 默认路径建模板的现场确认、U-01 的两次慢调用 | `gsd-release-20261008` 的 `summary.txt` 三、七，`func/verify_multitree.log`，`place-20261009/` |
| L-16 | `gsdCheckScript`（`packages/orchestrator/internal/template/build/layer/gsd.go:22`） |
| L-17 | 验证脚本 `verify_clear_refs_race.py` 开头的处理与注释 |
| L-11、T-07 | `gsd-fixes-20260930` 的 `SUMMARY.md` "逐项验证"与"未验证" |
| L-13、U-03、K-02、K-03 | `gsd-fix5-20261008` 的 `summary.txt` 一、四、六 |
| L-08 | `fc-kernels/configs/x86_64/6.1.158.config`；`0002-overlayfs-hot-switch-checkpoint.patch` 的 arm64 部分 |

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 多树预检（L-01、L-02、T-01） | `crossTreeShared`（`ckpt/shared.go:49`），文件头注释是这条限制的完整论证 |
| 容量预检（L-14、T-05） | `estimateDump`（`ckpt/capacity.go:60`） |
| 层栈深度预检（L-12） | `checkOverlayOptionsLen`（`ckpt/overlay.go:540`）、`maxOverlayOptionsLen`（`ckpt/overlay.go:31`） |
| 搬走打开的文件与调节参数（L-04、L-05） | `0003-overlayfs-hot-switch-fixes.patch` — `ovl_ckpt_want_move`、`ovl_ckpt_move_upper`、模块参数 `ckpt_move_open` |
| 陈旧对象的语义（L-06） | 同上 — `ovl_dentry_is_stale`、`ovl_ckpt_apply_item` |
| 注入开关（L-09、T-08） | `GSDEnabled`、`makePathsAbsolute`（`packages/orchestrator/internal/cfg/model.go:39`、`:47`）；base 层缓存键在 `packages/orchestrator/internal/template/build/phases/base/hash.go:55` |
| 不回滚的目录（L-10） | `packages/orchestrator/internal/template/build/core/rootfs/gsdfiles/overlay-init.sh.tpl` |
| 版本 1 状态文件（T-06） | `treesOf`（`ckpt/state.go:98`） |
| 防御性恢复（T-07） | `resumeAfterFailedDump`（`ckpt/service.go:840`） |
| 首个请求的探针（U-01） | 随手册附带的 `scripts/probes/probe_first_rpc.py` |

---

## 本章要点

1. 对使用方影响最大的三条边界：**跨树的管道和 socket 不支持**、**多棵树的代价是线性的且树间不是原子的**、**只支持 arm64 guest**。前两条的规避办法相同——需要一致的进程放进同一棵树。
2. **开着大文件写的时候做 checkpoint 会变慢**（约每 MiB 0.8 ms，期间文件操作停顿），可以用 `overlay.ckpt_move_open` 换成"更快但被映射文件的写不回滚"。
3. **范围外的进程在 restore 后可能遇到 `ESTALE`**，这是为了不把被回滚的内容写回去；重新 `chdir`、重新打开即可。
4. 三个没查清的现象（首个请求约 5 秒、同一沙箱上 2 次 create 返回 `internal`、单发 `ENOTEMPTY`）都不损坏数据，都没有稳定复现。
5. 没有现场结果的分支集中在**多棵树的异常路径**和**注入开关的关闭态**；它们只有单测、没有现场走过，评审时应当把它们算作未验证。默认路径建模板已于 2026-10-09 现场确认；建模板的自检不验证内核是否带补丁（L-16）。
6. 950 未部署、未测；本章的全部现场依据来自新 920B。
