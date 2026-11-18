# 附录 C · 问题处理与修复历程

## 本章目标

读完本附录，你应当能回答：

1. 代码审查得到的 36 个问题，每一个现在是什么状态：修掉了、按某个决定处理了，还是转成了已知限制？
2. 每一个问题的修复落在哪个提交、在本书哪一章展开、用什么验证过？
3. 清单之外还修了哪些在初始实现里就存在、到验证时才暴露的问题？
4. 有哪些方向性的决定中途改过，最后定在哪里、为什么？
5. 修复过程中 restore 的耗时怎样变化，这些读数能说明什么、不能说明什么？

---

正文第二篇（[05](05-architecture.md)～[16](16-known-limits.md)）讲的都是**现状**：代码现在怎么工作。
本附录以问题总表和修复结果为主线，供评审和继续开发时查阅：初始实现有哪些问题、每个问题的处理结果是什么。
它不重复机制，每组只写"修改前的后果 → 现在的行为 → 在哪章展开 → 怎么验证的"，细节一律链接过去。
只想用 gsd 的读者可以跳过本附录。[16](16-known-limits.md) 是它的另一半：没有修、或者不打算修的东西。

本附录是全书唯一的问题处理总表（§6），别的章提到问题编号时指的都是这里。

---

## 结论先行

- 对初始实现做过一次只读代码审查，得到 **36 个问题**（编号 G-01 到 G-36；按严重程度是 5 个会直接把沙箱弄坏的、17 个重要的、14 个一般的）。
- 处理结果：**29 个已修**；**5 个按决定处理**（修了，但走的不是清单建议的那条路，或者范围由一个明确的决定限定：G-08、G-12、G-19、G-26、G-33）；
  **2 个部分转为已知限制**（G-21 的回滚范围、G-34 的平台支持，可修的部分都修了）。没有一个是搁置未动的。
- 每项修复对应的提交见 §5，其中内核侧的全部修复合并成一个补丁 `0003-overlayfs-hot-switch-fixes.patch`。
- 验证中另外发现并修掉了 **9 个清单之外的内核问题**（§3），它们在初始实现里就存在，只读审查看不出来。
- 逐条验证脚本是 `verify_fixes.py`（28 个检查项覆盖清单中可在新 920B 上验证的条目）。新 920B、2026-10-08、`259f1a25c`、`base-gsd` 同代模板上的结果是
  通过 26、失败 0、跳过 2（跳过的两项见 §7）。判定值见 [19](19-920b-functional.md)。

---

## 1. 怎么读这张总表

**问题从哪来。** 清单来自对初始实现（提交 `e47d09d04`）的一次人工代码审查，当时没有编译也没有运行；CRIU 的行为对照了它 4.2.1 版本的源码。
清单里每一条都标了判定：读代码就能确定的事实、按语义推出来需要实际复现的后果、或者没有证据的猜测。
其中两条在核对后被降级（G-09 的机制被修正、G-23 大部分不成立），仍然保留编号并做了防御性处理。

**三种结果的含义。**

| 结果 | 含义 |
|---|---|
| 已修 | 问题描述的后果不再发生，有单测或新 920B 上的验证 |
| 按决定处理 | 修了，但做法或范围由一个方向性的决定确定，与清单建议的首选做法不同；决定和理由见 §4 |
| 转为限制 | 问题的一部分是设计边界而不是缺陷，写进 [16](16-known-limits.md)；其余可修的部分已修 |

**编号只在本附录和 [16](16-known-limits.md) 使用。** 其他章讲机制时不引用问题编号（代码注释里保留着编号，读代码时可以回到本附录查）。

---

## 2. 按主题归并

### 2.1 进程发现与杀进程（G-01、G-02、G-03、G-04、G-26、G-27）

**修改前的后果。** 这一组是初始实现里最容易把沙箱弄坏的地方，四个问题互相叠加：

- restore 只对缓存的"根进程"发 SIGKILL，子进程被 PID 1 收养后继续活着，既占着 PID（CRIU 按原 PID 重建进程树时报 `Can't fork ... File exists`），
  又带着 checkpoint 之后的状态继续写文件系统。只要用户进程有子进程，checkpoint 之后 restore 就失败，而失败时进程已经被杀、文件系统已经切走。
- dump 的"根"取的是 `/proc` 目录按字典序的第一个 uid 1000 进程（`"1203"` 排在 `"850"` 前面），可能是某棵树的子进程，也可能只是多棵树里的一棵。
- 缓存的根 PID 从不失效：那个进程退出后，之后每次 checkpoint 都对着一个死 PID 做 dump，一直失败到做一次 restore 为止。
- `/proc/<pid>/stat` 按空格切分，进程名里带空格的进程（`tmux: server` 之类）父 PID 取错，它和它的整棵子树在 dump 之后收不到 SIGCONT，永远停着。
- dump 之后对整棵树一律发 SIGCONT，把用户自己 Ctrl-Z 停住的进程也放开了。

**现在的行为。** 进程发现整个重写：一次 `/proc` 扫描建表，`stat` 按最后一个 `)` 切分；"根"有明确定义并按 PID 数值排序；不再缓存用户进程的 PID；
dump 前记录每个进程的状态，dump 后只放开原来不是停止状态的；restore 一律重新扫描并杀掉全部用户进程树——先冻住、反复扫描到没有新进程、再整体杀、限时等退出。
多棵树的问题最初的处理是拒绝，后来改为全部保存和恢复（§4）。G-26 只是一处注释与事实不符（被恢复的进程由 PID 1 收养，不是 gsd 的子进程），改正了注释。

**展开**：[10](10-process-trees.md)（发现、根的定义、多树预检、冻结与杀）、[12](12-restore.md)（restore 为什么必须杀整棵树）。
**验证**：单测（伪造的 `/proc` 与真实进程两类）；`verify_fixes.py` 的 G-01、G-02、G-03、G-04、G-26、G-27 六项；多树另有 `verify_multitree.py`。

### 2.2 dump 的失败与超时（G-09、G-23、G-28）

**修改前的后果。** "下一次增量的父镜像"在 dump 之前就记下了。dump 失败时失败的镜像目录留在 tmpfs 里（占 guest 内存，delete 也删不到）；
overlay 下沉失败时目录被删了、记录却还指着它，下一次 dump 必然再失败一次。CRIU 没有超时，卡住时 RPC 无限挂起，被 seize 的进程树一直冻着。

**现在的行为。** dump 或下沉任何一步失败都走同一条清理路径：删掉整个镜像目录、清空增量父记录、强制下一次全量。
不退回更早的父镜像，因为失败的 dump 可能已经重置了部分进程的 soft-dirty，再以更早的镜像为父会漏页。
CRIU 用独立于请求的带超时上下文执行，超时先 SIGTERM、宽限后 SIGKILL，返回 `DeadlineExceeded`；客户端断开不会让 CRIU 半途被杀。
dump 出错后做一次防御性恢复：只对"现在停着、dump 前没停"的进程发 SIGCONT（CRIU 正常出错会自己放开进程，这里兜的是 CRIU 被杀或崩溃的情况）。

**展开**：[11](11-checkpoint.md)（失败路径）、[14](14-failure-semantics.md)（错误码）。
**验证**：单测；`verify_fixes.py` 的 G-09 项用很短的 dump 时限触发超时，检查错误码、失败目录已清、进程没有停着、下一次是全量。
CRIU 被 SIGKILL 或自己崩溃的那一支没有在新 920B 上验证（[16](16-known-limits.md)）。

### 2.3 overlay 管理器的事务性与回收（G-07、G-08、G-11、G-13、G-25）

**修改前的后果。**

- 下沉 upper 时先把目录改名、再建新目录、再发 ioctl，后两步任何一步失败都不把名字改回去：gsd 记录的"当前 upper"从此指向一个不存在的路径，之后每次 checkpoint 都失败。
- ioctl 的选项串上限是一页（4 KiB），而层目录名很长（每层 47 字节），同一条链上 80 多层之后必然失败，随即落进上一条的永久失败状态。
- 层目录没有任何删除路径，根盘只增不减；回收临时目录的后台协程在锁外读目录、删目录，有窗口把下一次操作刚建的 upper 删掉。
- checkpoint 模式下内核会自动把旧 upper 作为最上面的 lower，gsd 又把同一个目录显式传了一次，层栈里重复一层。

**现在的行为。** 下沉改成"先准备、后提交"：先预检长度、建好新目录，再改名、发 ioctl，失败则把名字改回去。
层目录改用短名 `L<n>`；checkpoint 时预检这次 ioctl 和将来 restore 的选项串都放得下，放不下返回 `ResourceExhausted`，不会建出一个恢复不了的 checkpoint（上限见 §4）。
层目录按引用回收：既不在当前层栈、也没有任何 checkpoint 条目引用的层，连同临时目录一起在锁内改名进回收站、锁外删除。
gsd 传给 ioctl 的 lower 列表不再包含刚沉下去的那层。

**展开**：[07](07-overlay-hot-switch.md)（下沉、restore 的两段式、auto lower）、[15](15-state-concurrency-capacity.md)（回收、层栈深度预检）。
G-11 还有内核一半：层目录删掉之后数据块仍被内核钉着，见 §2.7 和 [09](09-overlay-cache-and-reclaim.md) "回收：被换下的层、mount 与缓存引用"一节。
**验证**：单测；`verify_fixes.py` 的 G-08、G-11、G-13、G-25 四项；ENOSPC 七档压测覆盖 G-07 的回滚。

### 2.4 生命周期与一致性（G-10、G-14、G-15、G-16、G-22）

**修改前的后果。**

- delete 不管 CRIU 增量镜像的父子依赖：删掉父镜像，所有以它为父的 checkpoint 再也恢复不了。
- restore 先杀进程，之后 overlay 切换或 CRIU 恢复失败时既不回滚也不说明，只返回一个普通的内部错误，调用方不知道沙箱已经处于"进程全灭"的状态。
- restore 到一个没有进程镜像的 checkpoint 时不杀当前进程，checkpoint 之后才起的进程留在"回滚后"的文件系统上继续跑。
- gsd 的状态全在内存里，而它被配置成总是自动重启、建模板时每层还要重启一次；重启之后它记得的层栈与内核里的实际层栈对不上，下一次 checkpoint 必然失败。
- 两处静默降级：查不到 envd 时降级成"只做文件系统的 checkpoint"；overlay 管理器初始化失败时"不切文件系统继续跑"。两种都返回成功，`/health` 恒为正常。

**现在的行为。** 有子节点的 checkpoint 被删时只是隐藏，最后一个子节点删掉时才连同它一起物理删除。
restore 分两段，杀进程之前把能检查的都检查完，这一段失败时什么都没动；杀进程之后的任何失败返回 `DataLoss`，写明文件系统处于旧状态还是目标状态，
并进入 degraded：`/health` 可见、拒绝 checkpoint、允许再 restore。restore 不管目标有没有进程镜像都先杀掉全部用户进程。
状态持久化到 snapshot store 上的 `state.json`，每次操作结束时原子写、启动时加载并校验。静默降级全部去掉：查不到 envd 不算错误、照常扫描；
初始化失败进入 degraded 并在每个请求里报出原因。

**展开**：[12](12-restore.md)（两段式）、[14](14-failure-semantics.md)（提交点、三类 degraded）、[15](15-state-concurrency-capacity.md)（状态文件、增量依赖与删除）。
**验证**：单测；`verify_fixes.py` 的 G-10、G-14、G-15、G-16、G-22 五项（破坏镜像文件、重启 gsd、写坏状态文件、停掉 envd）。

### 2.5 前提条件落地（G-06、G-18）

**修改前的后果。** "透明大页必须是 never"这条前提在代码里没有任何落点（guest 内核的默认值是 always），只有测试脚本手动设置；
不设的话 soft-dirty 按 2 MiB 记录，零散的小写入会让每次增量 dump 膨胀成接近全量。
另外，建模板的 provision 脚本把 `criu` 加进了发行版包列表，会用发行版的旧版本覆盖基础镜像里源码安装的 4.2.1，或者在源里没有这个包时让建模板失败。

**现在的行为。** overlay-init 在启动 PID 1 之前把透明大页写成 never 并读回确认，gsd 启动时再兜底检查一次。
provision 不再安装 criu；注入 gsd 之后的自检要求 `/usr/sbin/criu --version` 是 4.x，否则建模板失败。

**展开**：[03](03-criu-and-incremental-memory.md)（为什么必须 never）、[06](06-guest-boot-and-injection.md)（overlay-init 与注入后自检）。
**验证**：`verify_fixes.py` 的 G-06 项（开机即为 never、零散写负载的增量镜像在 MiB 量级）、G-18 项（版本与包状态）。

### 2.6 模板注入、鉴权与回滚范围（G-19、G-20、G-21）

**修改前的后果。**

- gsd 的注入和 overlay-init 对这台 orchestrator 建的**所有**模板生效；注入后重启 gsd 的错误被吞掉，gsd 起不来建模板也"成功"，问题到运行时才暴露。
- 鉴权形同虚设：SDK 固定发一个空密码的 Basic 头，gsd 只检查用户名存在。gsd 以 root 运行、能杀进程和切根文件系统，任何能访问沙箱 49984 端口的人都能调用。
- `/tmp` 是单独的 tmpfs，不参与回滚；CRIU 镜像放在占 guest 内存的 tmpfs 里，没有任何容量检查，内存不够时先触发 OOM，而被杀的是用户进程。

**现在的行为。** 注入由 orchestrator 的 `HOST_GSD_PATH` 控制（默认开，设为 `off` 关闭，演变见 §4），关闭后建模板路径与上游一致；
注入后的自检（服务状态、`/health`、CRIU 版本）任何一项失败都让建模板失败。每个 RPC 校验与 envd 相同的沙箱访问令牌。
`/tmp` 改为落在 overlay 上、随 checkpoint 回滚（演变见 §4）；checkpoint 之前做容量预检，放不下返回 `ResourceExhausted`，每个 checkpoint 记录镜像实际大小。

**转为限制的部分**（G-21）：`/run`、`/dev/shm`、`/var/log` 等仍不回滚；没有自动清理旧 checkpoint 的策略，由调用方删除。见 [16](16-known-limits.md)。

**展开**：[05](05-architecture.md)（鉴权）、[06](06-guest-boot-and-injection.md)（开关、自检、`/tmp`）、[15](15-state-concurrency-capacity.md)（容量预检）。
**验证**：`verify_fixes.py` 的 G-20、G-21 两项；G-19 的默认路径已于 2026-10-09 在新 920B 上真实建模板确认；关闭态（`HOST_GSD_PATH=off`）仍没有新 920B 上的结果，只有单测与在真实环境变量下运行的配置解析测试（[16](16-known-limits.md) 的 T-08、[23](23-gaps-and-boundaries.md)）。

### 2.7 内核（G-05、G-17、G-24）

**修改前的后果。** ioctl 在换上新层之后如果失败，清理路径会释放正在使用的层数组；被换下的层数组每次 ioctl 泄漏一组、从不释放，
而这份泄漏同时又在防止释放后使用，不能简单去掉；热路径上残留着调试打印。

**现在的行为。** 可能失败的步骤全部挪到提交点之前；被换下的层数组在确认无人引用后延迟一个切换周期释放；调试打印全部去掉。
这三条只是内核侧工作的起点——验证中又查出 9 个清单之外的问题（§3），它们与这三条一起构成补丁 `0003-overlayfs-hot-switch-fixes.patch`。

**展开**：[08](08-overlay-switch-consistency.md)、[09](09-overlay-cache-and-reclaim.md)（G-05 在 08 章的"失败处理与发布"一节，G-17 与 G-11 的内核一半在 09 章的"回收：被换下的层、mount 与缓存引用"一节，G-24 在 09 章的"mmap 与调试输出"一节）。
**验证**：ENOSPC 七档（G-05 的晚失败路径）、`probe_leak.py` 与 `probe_umount.py`（G-17）、`verify_fixes.py` 的 G-13 项顺带检查 dmesg 无调试输出（G-24）。

### 2.8 接口、可观测性与清理（G-12、G-29 ～ G-36）

这一组不改变核心行为，逐条简述：

- **G-12 并发**：服务没有互斥，并发的 checkpoint 和 restore 会交错。现在 checkpoint、restore、delete 全程持同一把锁，后来的请求排队（§4）；list 不持锁。见 [15](15-state-concurrency-capacity.md)。
- **G-29 返回信息**：各阶段耗时原来只进日志。现在随响应返回，另加创建时间、是否有进程镜像、父 checkpoint、镜像大小、`clear_refs` 失败数、进程树数。见 [05](05-architecture.md)、[29](29-configuration-and-observability.md)。
- **G-30 测试**：有一条单测断言因为拼错路径永远通过，编排逻辑完全没有测试。现在外部依赖（`/proc`、CRIU、信号、ioctl）都可注入，编排逻辑有成套单测。见 [18](18-testing-and-debug-kernel.md)。
- **G-31 文档与注释**、**G-32 死代码**：两份 README、overlay-init 头注释与代码对齐；死代码和整个旧的 overlay-setup 方案删除。
- **G-33 根目录权限**：guest 的 `/` 原来是 777。现在是 755，并在每次切换时保持（§4）。见 [06](06-guest-boot-and-injection.md)。
- **G-34 平台与基线**：内核构建脚本原来取"最新的上游 tag"，基线不可复现；现在固定在 `kernel_base_tags.txt`。只支持 arm64 guest 这一点是设计边界，转为限制（[16](16-known-limits.md)）。见 [28](28-deployment.md)。
- **G-35 可观测性**：restore 的 CRIU 输出落到镜像目录里的日志文件，失败时尾部进错误信息（dump 失败同样处理）；`clear_refs` 失败不再静默；每次 ioctl 的调试行改为 debug 级日志。见 [29](29-configuration-and-observability.md)。
- **G-36 SDK**：checkpoint 的请求地址不认 `sandbox_url` 覆盖、每个对象新建一个从不关闭的 HTTP 客户端。两处都改了，并删掉了不再需要的 Basic 头。见 [26](26-quickstart.md)。

---

## 3. 清单之外修掉的

下面这些问题在初始实现里就存在，只读审查没有发现，是压测和探针逐轮查出来的。全部在内核补丁里，这里只列名，机制、根因和验证都在 [08](08-overlay-switch-consistency.md)、[09](09-overlay-cache-and-reclaim.md)。

| 问题 | 怎么发现的 | 在哪一节 |
|---|---|---|
| 切换时有不被杀的进程在做文件操作：空指针解引用、`BUG`、KASAN 报告、软锁死 | 带 root worker 的并发压测 | 08 章"切换与所有 overlay 入口的互斥" |
| checkpoint 之前打开的 fd，之后的写入回滚不掉 | `probe_fd_rollback.py` | 08 章"打开的文件跨切换的语义" |
| 被映射的文件，之后的写入写穿到已经沉下去的 checkpoint 层里 | `probe_mmap_rollback.py` | 同上 |
| restore 之后，被某个进程钉住的目录的整条祖先链 `ESTALE` | `probe_estale.py` | 09 章"目录与 inode 缓存跨 restore" |
| restore 把一次 rename 劈成两半，留下一个挂在新名字上的旧时间线目录 | 压测里连续几百次 `ENOTEMPTY` | 09 章"restore 与 rename、unlink、rmdir 的竞争" |
| 被删层的磁盘块不释放、内核里的 mount 结构只增不减 | `verify_fixes.py` 的层回收项、`probe_leak.py` | 09 章"回收：被换下的层、mount 与缓存引用" |
| whiteout 缓存没有随切换丢弃：restore 后删下层文件必现内核 `BUG` | 最终版模板的整体验证 | 09 章"属于 work 目录的缓存" |
| work 目录的 trap inode 没有随切换更换：`ELOOP`、`ENXIO`、`ENOTDIR` | 同上（与上一条被同一个偶然条件掩盖） | 同上 |
| rmdir 一个"盖在 whiteout 上重建、又被 restore 丢弃"的目录：内核 `BUG` | 删文件密集压测 | 09 章"restore 与 rename、unlink、rmdir 的竞争" |

用户态也有三处是验证时才发现的，各有一个提交（§5）：第一版修复让 restore 变慢（每次扫描调两次 `systemctl show`，见 [附录 E](E-regression-case.md)）；
dump 失败的错误信息指向一个已被删除的日志文件；SDK 把 gsd 的 `resource_exhausted` 映射成了限流异常。

---

## 4. 方向性的决定及其演变

有七处不是"照清单修"就能定的，其中四处中途改过方向。每项只写最后的结论和一句理由。

| 事项 | 起初 | 最后 | 理由 |
|---|---|---|---|
| 沙箱里有多棵用户进程树（G-02） | checkpoint 直接拒绝，返回 `FailedPrecondition` 并列出各个根 | 全部保存、全部恢复；每棵树各做一次 CRIU dump 和 restore | 每个 SDK 后台命令就是一棵树，拒绝意味着稍复杂的用法都做不了 checkpoint。先拒绝是为了先堵住"只保存一棵、restore 却全杀"的数据丢失，支持多树需要按树维护增量链，是后做的一步（[10](10-process-trees.md)） |
| `/tmp` 是否回滚（G-21） | 不回滚，保持单独的 tmpfs，写进使用说明 | 回滚：`/tmp` 落在 overlay 上，开机时清空并保证权限为 1777 | 被恢复的进程如果开着 `/tmp` 里后来被删或改过大小的文件，CRIU 恢复可能失败；用户也普遍预期 `/tmp` 跟着回滚（[06](06-guest-boot-and-injection.md)） |
| `HOST_GSD_PATH` 的默认值（G-19） | 默认空，即默认不注入，要显式设置才建带 gsd 的模板 | 默认 `/fc-gsd/gsd`，即默认注入；设为小写 `off` 关闭 | 这条分支的用途就是 gsd，默认不注入时忘了设变量会静默建出没有 gsd 的模板；默认注入而二进制缺失时建模板会明确失败（[06](06-guest-boot-and-injection.md)） |
| guest 根目录的权限（G-33） | gsd 侧不再把挂载点改成 777，但 overlay-init 里的 `chmod 0777` 保留（当时没有发现依赖，按"设计如此"记录） | overlay-init 也不再改：`/` 从开机起就是 755，每次切换沿用最上层的权限和属主 | 777 是从更早的非根挂载方案带过来的，overlay 挂到 `/` 之后没有理由让根目录对所有人可写（[06](06-guest-boot-and-injection.md)） |
| 并发请求（G-12） | — | 排队，不拒绝 | 调用方不用自己处理"忙"的重试；操作本身是毫秒到百毫秒级的，排队的代价小（[15](15-state-concurrency-capacity.md)） |
| 被恢复进程的父进程（G-26） | — | 只改注释，不加 `--restore-sibling` | 让 gsd 成为被恢复进程的父进程，gsd 重启时这些进程会被重新托管，牵连状态持久化；由 PID 1 收养已经够用（[12](12-restore.md)） |
| 层栈深度上限（G-08） | — | gsd 侧预检加短层名，不放宽内核的 4 KiB 上限 | 不动已验证的 ioctl 接口；短层名把上限从 80 多层推到约 190 层，超过时明确拒绝而不是建出恢复不了的 checkpoint（[15](15-state-concurrency-capacity.md)） |

层栈上限的实测数字（新 920B，2026-09-30，第一版修复，`verify_fixes.py` 的 G-08 项口径）：连续 checkpoint 190 次成功，第 191 次被拒绝；判定值见 [19](19-920b-functional.md)。

---

## 5. 修复提交

下表是 `git log --reverse e47d09d04..259f1a25c` 的全部提交，按时间顺序。"对应"一列是它处理的清单条目；没有编号的是验证之后的补充或方向调整。
代码基准 `bc3cce913` 在这之后还有两个提交：`dcca61266`（SDK 向 gsd 发送入口流量令牌）和 `bc3cce913`（`slot.go` 的 `VethName()`），都不对应清单里的问题。

| 序 | 提交号 | 标题 | 对应 |
|---|---|---|---|
| 1 | `73db6f055` | gsd: make checkpoint orchestration testable with injectable system ops | G-30 |
| 2 | `326821e7e` | gsd: rewrite user process discovery (stat parsing, numeric roots, pre-dump states) | G-02、G-04、G-27 |
| 3 | `21e388b8a` | gsd: kill whole user process trees on restore and drop the PID cache | G-01、G-03、G-26 |
| 4 | `fdce8c8f2` | gsd: serialize checkpoint ops, bound criu runtime, clean up failed dumps | G-09、G-12、G-23、G-28 |
| 5 | `7fe4d3f51` | gsd: force transparent hugepages off and stop provisioning distro criu | G-06、G-18 |
| 6 | `f6af4a718` | gsd: make overlay sink transactional, reclaim via trash, short layer names | G-07、G-08、G-13，G-31 的一处注释 |
| 7 | `aedd4cf84` | gsd: stop passing the just-sunk layer twice in ckpt-mode overlay switches | G-25 |
| 8 | `8b8239f59` | gsd: keep incremental parents until children are gone, reclaim unreferenced layers | G-10、G-11 |
| 9 | `5cd4a980b` | gsd: check before killing on restore, report half-done restores, degrade instead of silently skipping | G-14、G-15、G-22，G-35 的一部分 |
| 10 | `8cdeadc1f` | gsd: persist checkpoint state to snapshot store and reload it on restart | G-16 |
| 11 | `328e4ad62` | gsd: authenticate RPCs with the sandbox access token from MMDS | G-20 |
| 12 | `b1e36e23e` | orchestrator: make gsd an opt-in via HOST_GSD_PATH and fail builds when gsd is unhealthy | G-19 |
| 13 | `7fd56c485` | gsd: refuse checkpoints that would not fit and record image sizes | G-21 |
| 14 | `d4b876813` | gsd: return stage timings and checkpoint metadata to the SDK | G-29，G-35 的一部分 |
| 15 | `91c42ab94` | py-sdk: honour URL overrides for gsd, stop per-Checkpoint HTTP clients, drop Basic auth | G-36，G-20 的 SDK 一侧 |
| 16 | `a9dc9f93f` | gsd: remove dead code and the overlay-setup scheme, stop making / world-writable | G-32、G-33 |
| 17 | `ebc135677` | fc-kernels: pin the upstream base tag the patches are made against | G-34 |
| 18 | `731611b66` | gsd: bring the READMEs and overlay-init comments in line with the code | G-31 |
| 19 | `9c8035627` | gsd: make process discovery cheap again (cache envd MainPID, one /proc scan per op, skip kernel threads) | 第一版修复引入的性能回退（[附录 E](E-regression-case.md)） |
| 20 | `31439d5d0` | gsd: carry the criu dump log tail in the error instead of pointing at a deleted file | 验证时发现，G-35 的补充 |
| 21 | `4742c4acb` | py-sdk: map gsd checkpoint errors on their own instead of through envd's defaults | 验证时发现（[14](14-failure-semantics.md)） |
| 22 | `93b90a454` | orchestrator: lift systemd start limit on gsd.service | 验证时发现：多步骤模板每层重启一次 gsd，撞上 systemd 的启动频率限制 |
| 23 | `823fe7b02` | orchestrator: inject gsd by default again, HOST_GSD_PATH=off to opt out | G-19 的方向调整（§4） |
| 24 | `92cfc2f29` | gsd: checkpoint and restore every user process tree, not just one | G-02 的方向调整（§4） |
| 25 | `fe453d270` | py-sdk: report the number of process trees in CheckpointInfo | 随上一项 |
| 26 | `4c1ba0ef8` | orchestrator: keep /tmp on the overlay root so restore rolls it back | G-21 的方向调整（§4） |
| 27 | `9bd06b942` | orchestrator: stop making the guest's / world-writable in overlay-init | G-33 的方向调整（§4） |
| 28 | `259f1a25c` | fc-kernels: fold all our overlayfs hot-switch fixes into patch 0003 | G-05、G-17、G-24，G-11 的内核一半，§3 的 9 项 |

第 19 项重新引入了一个缓存，但对象和 G-03 不同：缓存的是 envd 的主 PID（只用来把它排除在用户进程之外），并且每次用它之前都用同一次扫描里的启动时间校验；
用户进程的 PID 仍然不缓存。

---

## 6. 问题处理总表

全书唯一。"落点"是提交标题（提交号见 §5）；"所在章"是机制展开的地方。

| 编号 | 一句话 | 结果 | 落点 | 所在章 |
|---|---|---|---|---|
| G-01 | restore 只杀根进程，子进程活下来占着 PID、也没被回滚 | 已修 | gsd: kill whole user process trees on restore and drop the PID cache | [10](10-process-trees.md)、[12](12-restore.md) |
| G-02 | dump 的根按 `/proc` 字典序取；多棵树时只 dump 一棵、restore 却全杀 | 已修（先拒绝，后支持多树） | gsd: rewrite user process discovery (…)；gsd: checkpoint and restore every user process tree, not just one | [10](10-process-trees.md)、[11](11-checkpoint.md) |
| G-03 | 根 PID 缓存不失效，进程退出后每次 checkpoint 都失败 | 已修 | gsd: kill whole user process trees on restore and drop the PID cache | [10](10-process-trees.md) |
| G-04 | `stat` 按空格切分，进程名带空格的子进程 dump 后永远停着 | 已修 | gsd: rewrite user process discovery (…) | [10](10-process-trees.md) |
| G-05 | 内核 ioctl 换层之后失败，会释放正在使用的层数组 | 已修 | fc-kernels: fold all our overlayfs hot-switch fixes into patch 0003 | [08](08-overlay-switch-consistency.md) |
| G-06 | "透明大页必须 never"在代码里没有落点 | 已修 | gsd: force transparent hugepages off and stop provisioning distro criu | [03](03-criu-and-incremental-memory.md)、[06](06-guest-boot-and-injection.md) |
| G-07 | 下沉 upper 半途失败不回滚，之后 checkpoint 永久失败 | 已修 | gsd: make overlay sink transactional, reclaim via trash, short layer names | [07](07-overlay-hot-switch.md) |
| G-08 | ioctl 选项串上限 4 KiB，约 80 层后必然失败 | 按决定处理（gsd 预检加短层名，上限约 190 层） | 同上 | [15](15-state-concurrency-capacity.md)、[16](16-known-limits.md) |
| G-09 | 增量父镜像在 dump 前就记下，失败后下一次 dump 失败、失败目录泄漏 | 已修 | gsd: serialize checkpoint ops, bound criu runtime, clean up failed dumps | [11](11-checkpoint.md) |
| G-10 | delete 不管增量依赖，删父镜像后子 checkpoint 恢复不了 | 已修 | gsd: keep incremental parents until children are gone, reclaim unreferenced layers | [15](15-state-concurrency-capacity.md) |
| G-11 | 层目录永不回收，根盘只增不减 | 已修（gsd 侧回收目录，内核侧释放被钉住的块） | 同上；fc-kernels: fold all our overlayfs hot-switch fixes into patch 0003 | [15](15-state-concurrency-capacity.md)、[09](09-overlay-cache-and-reclaim.md) |
| G-12 | 服务没有互斥，并发 RPC 交错执行 | 按决定处理（排队，不拒绝） | gsd: serialize checkpoint ops, bound criu runtime, clean up failed dumps | [15](15-state-concurrency-capacity.md) |
| G-13 | 回收协程在锁外判定和删除，可能删掉刚建的 upper | 已修 | gsd: make overlay sink transactional, reclaim via trash, short layer names | [15](15-state-concurrency-capacity.md) |
| G-14 | restore 杀进程后失败，不回滚也不报告 | 已修 | gsd: check before killing on restore, report half-done restores, degrade instead of silently skipping | [12](12-restore.md)、[14](14-failure-semantics.md) |
| G-15 | restore 到没有进程镜像的 checkpoint 时不杀当前进程 | 已修 | 同上 | [12](12-restore.md) |
| G-16 | gsd 重启后内存状态全丢，与内核层栈不一致 | 已修 | gsd: persist checkpoint state to snapshot store and reload it on restart | [15](15-state-concurrency-capacity.md) |
| G-17 | 内核里被换下的层数组每次泄漏一组，而泄漏本身在防释放后使用 | 已修 | fc-kernels: fold all our overlayfs hot-switch fixes into patch 0003 | [09](09-overlay-cache-and-reclaim.md) |
| G-18 | provision 安装发行版 criu，覆盖源码装的 4.2.1 或让建模板失败 | 已修 | gsd: force transparent hugepages off and stop provisioning distro criu；orchestrator: make gsd an opt-in via HOST_GSD_PATH and fail builds when gsd is unhealthy | [06](06-guest-boot-and-injection.md) |
| G-19 | gsd 注入对所有模板生效；二进制缺失时全部建不成；重启错误被吞 | 按决定处理（orchestrator 级开关，默认开，`off` 关闭） | orchestrator: make gsd an opt-in via HOST_GSD_PATH and fail builds when gsd is unhealthy；orchestrator: lift systemd start limit on gsd.service；orchestrator: inject gsd by default again, HOST_GSD_PATH=off to opt out | [06](06-guest-boot-and-injection.md) |
| G-20 | 鉴权形同虚设 | 已修 | gsd: authenticate RPCs with the sandbox access token from MMDS；py-sdk: honour URL overrides for gsd, stop per-Checkpoint HTTP clients, drop Basic auth | [05](05-architecture.md) |
| G-21 | `/tmp` 等不回滚；镜像占 guest 内存、无容量检查、无自动清理 | 部分转为限制（`/tmp` 已回滚、容量预检已加；`/run` 等不回滚与无自动清理为限制） | gsd: refuse checkpoints that would not fit and record image sizes；orchestrator: keep /tmp on the overlay root so restore rolls it back | [06](06-guest-boot-and-injection.md)、[15](15-state-concurrency-capacity.md)、[16](16-known-limits.md) |
| G-22 | 两处静默降级都返回成功，`/health` 恒为正常 | 已修 | gsd: check before killing on restore, report half-done restores, degrade instead of silently skipping | [14](14-failure-semantics.md) |
| G-23 | dump 失败时 gsd 不发 SIGCONT（核对后大部分不成立） | 已修（防御性恢复；CRIU 被杀的一支未在新 920B 上验证） | gsd: serialize checkpoint ops, bound criu runtime, clean up failed dumps | [11](11-checkpoint.md)、[16](16-known-limits.md) |
| G-24 | 内核补丁残留调试打印 | 已修 | fc-kernels: fold all our overlayfs hot-switch fixes into patch 0003 | [09](09-overlay-cache-and-reclaim.md) |
| G-25 | checkpoint 模式下层栈里重复一层 | 已修 | gsd: stop passing the just-sunk layer twice in ckpt-mode overlay switches | [07](07-overlay-hot-switch.md) |
| G-26 | 注释说被恢复的进程是 gsd 的子进程，实际由 PID 1 收养 | 按决定处理（只改注释） | gsd: kill whole user process trees on restore and drop the PID cache | [12](12-restore.md) |
| G-27 | dump 后的 SIGCONT 把用户原本停着的进程也放开 | 已修 | gsd: rewrite user process discovery (…) | [10](10-process-trees.md) |
| G-28 | CRIU 没有超时，卡住时 RPC 无限挂起 | 已修 | gsd: serialize checkpoint ops, bound criu runtime, clean up failed dumps | [11](11-checkpoint.md)、[14](14-failure-semantics.md) |
| G-29 | 各阶段耗时不返回 SDK，checkpoint 不返回创建时间等信息 | 已修 | gsd: return stage timings and checkpoint metadata to the SDK；py-sdk: report the number of process trees in CheckpointInfo | [05](05-architecture.md)、[29](29-configuration-and-observability.md) |
| G-30 | 一条单测断言永远通过；编排逻辑没有测试 | 已修 | gsd: make checkpoint orchestration testable with injectable system ops | [18](18-testing-and-debug-kernel.md) |
| G-31 | 文档和注释与代码不符 | 已修 | gsd: bring the READMEs and overlay-init comments in line with the code | — |
| G-32 | 死代码与整个旧的 overlay-setup 方案 | 已修 | gsd: remove dead code and the overlay-setup scheme, stop making / world-writable | [06](06-guest-boot-and-injection.md) |
| G-33 | guest 根目录对所有人可写 | 按决定处理（最终为 755，分两步完成） | 同上；orchestrator: stop making the guest's / world-writable in overlay-init | [06](06-guest-boot-and-injection.md) |
| G-34 | 只支持 arm64；内核基线取"最新 tag"不可复现 | 部分转为限制（基线已固定；只支持 arm64 为限制） | fc-kernels: pin the upstream base tag the patches are made against | [04](04-goals-and-tradeoffs.md)、[28](28-deployment.md)、[16](16-known-limits.md) |
| G-35 | restore 的 CRIU 输出不落文件、`clear_refs` 失败被忽略、每次 ioctl 打调试行 | 已修 | gsd: check before killing on restore, (…)；gsd: return stage timings and checkpoint metadata to the SDK；gsd: carry the criu dump log tail in the error instead of pointing at a deleted file | [29](29-configuration-and-observability.md) |
| G-36 | SDK 的 checkpoint 地址不认 `sandbox_url` 覆盖；HTTP 客户端从不关闭 | 已修 | py-sdk: honour URL overrides for gsd, stop per-Checkpoint HTTP clients, drop Basic auth | [26](26-quickstart.md) |

表中"(…)"是为排版省略的标题后半段，完整标题见 §5。

---

## 7. 逐条验证的口径

`verify_fixes.py` 是对着这张清单写的验证脚本，共 28 个检查项（有的项一次覆盖几个编号，例如 G-09 项同时检查 G-23 和 G-28，G-29 项同时检查 G-12，G-13 项同时检查 G-24；
另有三项不带清单编号：一项检查 checkpoint 之前打开的 fd，一项检查带 unix socket 和已删除仍打开文件的进程，一项记录进程发现的开销）。脚本的用法和局限见 [18](18-testing-and-debug-kernel.md)。

**恒为跳过的两项**：G-19（关闭注入的开关要改 orchestrator 的启动环境，脚本做不了，而且会影响同机的其他使用者）和 G-36（SDK 的地址覆盖只有本机单测）。
这两项没有新 920B 上的结果，记在 [16](16-known-limits.md) 和 [23](23-gaps-and-boundaries.md) 的缺口表里。

**随行为变化调整过预期的项**。方向性的决定改了之后（§4），对应检查项的预期也跟着改，读旧的结果文件时要注意：

| 检查项 | 最初的预期 | 现在的预期 |
|---|---|---|
| G-02 | 两个后台命令时 checkpoint 返回 `FailedPrecondition` 并列出两个根 | checkpoint 成功、进程树数为 2；杀掉一棵再另起一个进程后 restore，两棵都回来，checkpoint 之后起的进程消失 |
| G-21 | `/tmp` 不回滚 | 删掉 `/tmp` 里的一个文件、新建另一个之后 restore，前者回来、后者消失；容量预检部分不变 |
| G-33 | 只判"`/` 的权限在切换前后一致"，实测为 777 | 另外断言 `/` 是 755、属主 root |
| G-11 | `df /` 的增量小于阈值 | `df /` 的增量扣掉 `/var/log` 的增量后小于阈值（journald 的增长与层回收无关，见 [16](16-known-limits.md)） |
| G-14、G-06 | 镜像在 `<id>_mem/` 下 | 镜像在 `<id>_mem/t0/` 下（多树之后的布局） |

**各轮的结果走势**（都在新 920B 上；各轮模板与内核不同，只说明趋势，判定值见 [19](19-920b-functional.md)）：
2026-09-30 第一版修复时通过 23 项、仅记录 1 项、跳过 2 项，失败的两项是层回收后磁盘不回落和旧 fd 写入不回滚，都是 §3 里清单之外的内核问题；
两者分别在其后的内核修复里转为通过；最终版模板第一次整体验证时又有一项因 whiteout 缓存的内核 `BUG` 失败；最后一轮内核修复后为通过 26、失败 0、跳过 2，最终提交的确认性验证与此相同。

---

## 8. 修复历程对照

下表把历次验证里 `verify_gsd.py` 的 `total_ms` p50 并列出来，只为说明修复过程中性能的走向，对应的叙述在 [附录 E](E-regression-case.md)。
**每一行是不同的模板、多数行是不同的内核，各测一次。不要把相邻两行相减当作某一项改动的代价**——能回答"某一项改动贵了多少"的只有逐轮交错的对照。负载与口径同 [20](20-920b-performance.md) 的"`verify_gsd.py`：单棵 192 MiB、全脏，连续 restore 8 次"一节，平台都是新 920B。

| 日期 | gsd | 内核 | `total_ms` p50 | 结果目录 |
|---|---|---|---|---|
| 09-30 | 问题清单修复之前 | 问题清单修复之前 | 75.6、77.3（同一个模板先后两次） | `gsd-fixes-20260930` |
| 09-30 | 第一版修复 | 第一轮内核修复（当时仍有并发崩溃问题） | 93.8、94.5（同一个模板另有一次 104.1） | `gsd-fixes-20260930` |
| 09-30 | 收回进程扫描的回退之后 | 同上 | 84.7 | `gsd-fixes-20260930` |
| 09-30 | 同上 | 加入切换与文件操作的互斥、旧 fd 写入的回滚 | 87.0 | `gsd-fix3-20260930` |
| 09-30 | 同上 | 再加祖先链 `ESTALE`、可写打开文件的处理，并带代码布局填充 | 80.7 | `gsd-fix4-20260930` |
| 10-08 | 同上 | 去掉布局填充，加入层的回收 | 86.1 | `gsd-fix5-20261008` |
| 10-08 | 多棵树、`/tmp` 回滚、根目录 755（前一个提交号 `c0630349c`） | 同上 | 86.5 | `gsd-final-20261008` |
| 10-08 | 同上 | 再加 work 目录缓存与删除竞争的修复（最终内核） | 84.8 | `gsd-fix6-20261008`（`v6/verify_gsd-base-gsd8.log`） |
| 10-08 | `259f1a25c` | 最终内核 | **83.3** | `gsd-release-20261008` |

能从这张表里读出的只有三件事：第一版修复带来过一次明显的回退，随后收回了大半；带代码布局填充的那一行读数最低，这与 [附录 E](E-regression-case.md) 里用逐轮交错对照得到的结论方向一致（为什么最终不保留填充也在那里）；
最后几轮在 83～87 ms 之间，彼此的差在单次测量的波动之内。问题清单修复之前的 75～77 ms 与现在的 83 ms 之间的差距由哪几项构成，本表回答不了，逐项的归因与定量在 [附录 E](E-regression-case.md)。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 问题清单原文（工作稿，含每条的判定依据与建议修法） | `gsd-code-issues.md`（保存在测试工作区，不在代码仓库里） |
| 修复提交 | `git log --reverse e47d09d04..259f1a25c` |
| 内核侧全部修复 | `fc-kernels/patches/6.1.158/0003-overlayfs-hot-switch-fixes.patch`，文件头的提交说明按主题列出 |
| 逐条验证 | 随手册附带的 `scripts/acceptance/verify_fixes.py` |
| 可注入的外部依赖（单测的基础） | `sysOps`（`ckpt/sysops.go:38` 的 `defaultSysOps`）、`ckpt/fake_test.go` |
| 代码里保留的问题编号 | `ckpt/` 各文件的注释，例如状态持久化的文件头（`ckpt/state.go:13`）、层名与选项串上限（`ckpt/overlay.go:27`、`layerName`（`ckpt/overlay.go:464`）） |

---

## 本章要点

1. 代码审查得到的 36 个问题全部有了结果：29 个已修，5 个按明确的决定处理，2 个的不可修部分转为已知限制。
2. 每项修复都对应到提交（§5）；内核侧的修复合并成一个补丁 `0003-overlayfs-hot-switch-fixes.patch`。
3. 验证中另外查出 9 个清单之外的内核问题，全部修掉，机制在 [08](08-overlay-switch-consistency.md)、[09](09-overlay-cache-and-reclaim.md)。只读审查找得到逻辑错误，找不到并发和生命周期问题，后者要靠压测和调试内核。
4. 四个方向性的决定中途改过：多棵树从拒绝到支持、`/tmp` 从不回滚到回滚、注入从默认关到默认开、根目录权限从 777 到 755。读旧的结果文件和旧文档时以本附录为准。
5. `verify_fixes.py` 是这张清单的可执行版本；它恒跳过的两项（注入开关的关闭态、SDK 地址覆盖）没有新 920B 上的结果。
6. 问题编号只在本附录和 [16](16-known-limits.md) 使用，其他章讲现状不讲编号。
7. §8 并列了历次验证的 `total_ms` p50，只说明走向；相邻两行不能相减，逐项归因在 [附录 E](E-regression-case.md)。
