# 06 · overlayfs 热切层：机制

## 本章目标

读完本章，你应当能够：

- 说清"热切层"要解决的问题，以及 checkpoint 与 restore 两种切换在内核里的唯一区别；
- 写出 ioctl 的参数格式，并解释 gsd 传给内核的 `lowerdir` 为什么比它自己记录的层栈少一层；
- 按顺序说出一次切换在内核里经过的阶段，指出哪一步是提交点、提交点之前失败意味着什么；
- 说明切换之后，已经在内存里的 inode、目录项和打开的文件各自变成什么；
- 读懂 gsd 侧的 `SinkUpper` 与 `PrepareRestore` / `CommitRestore`，知道每次 restore 为什么要用一对全新命名的目录。

上一章（[05](05-guest-boot-and-injection.md)）讲了 overlay 根是怎么在开机时搭起来的。本章讲 gsd 在这个根上做的核心动作。
本章**只讲现在的机制**；这套机制在并发、对象生命周期上曾经有过哪些缺陷、怎么发现、怎么修、怎么验证，全部在下一章（[07](07-overlay-concurrency-and-fixes.md)）。

主要代码：`ckpt/overlay.go`；内核侧是两个补丁——`0002-overlayfs-hot-switch-checkpoint.patch`（交接时的原始补丁，定义了接口）
和 `0003-overlayfs-hot-switch-fixes.patch`（在其上的全部修复，合并成一个）。按全书约定，内核代码只引"补丁文件名 + 函数名"。

---

## 1. 问题：不卸载根，换掉它下面的层

overlayfs 把若干只读的 lower 层和一个可写的 upper 层叠成一个目录树。标准的 overlayfs 里，这组层在挂载时定死，要换只能卸载重挂。
但 gsd 管的那个 overlay 是 guest 的根文件系统，PID 1、所有服务、所有用户进程的根和当前目录都在它上面，**不可能卸载**。

进程级回滚需要两种"换层"：

- **checkpoint**：把当前 upper 冻结成一个只读层，换上一个空的新 upper。之后的写入落在新 upper 里，被冻结的那一层连同它下面的层就是这个 checkpoint 的文件系统内容。
- **restore**：把整个层栈换回某个 checkpoint 记录的那一组只读层，再配一个**全新的空 upper**。原来 upper 里的内容——也就是 checkpoint 之后的全部写入——从视图里消失。

两者都要在文件系统挂着、有进程正在使用的情况下完成。这就是内核补丁加的 ioctl 做的事，本书把一次这样的 ioctl 称为一次**切换**。

---

## 2. ioctl 接口

### 2.1 命令号与参数

定义在 `0002-overlayfs-hot-switch-checkpoint.patch` 对 `fs/overlayfs/overlayfs.h` 的修改里：

```c
#define OVL_IOCTL_MAGIC       'O'
#define OVL_IOCTL_CHECKPOINT  _IOW(OVL_IOCTL_MAGIC, 1, struct ovl_checkpoint_args)

struct ovl_checkpoint_args {
    char __user *options;
    __u32        options_len;
};
```

ioctl 可以发在 **overlay 上任意一个打开的文件或目录**的描述符上（补丁把 `ovl_dir_ioctl` 同时挂到了普通文件和目录的文件操作表里），作用对象是它所在的整个 overlay 实例。gsd 在启动时打开 overlay 的挂载点 `/` 并一直持有这个描述符
（`NewOverlayManager`，`ckpt/overlay.go:75`），之后每次切换都用它。调用者必须有 `CAP_SYS_ADMIN`，否则返回 `-EPERM`。

gsd 一侧没有引用内核头文件，而是自己算命令号、自己拼参数：`ovlCheckpointArgs`（`overlay.go:21`）是 8 字节指针 + 4 字节长度 + 4 字节填充，共 16 字节；
命令号在 `init`（`overlay.go:579`）里按"写方向、参数大小、魔数 `'O'`、序号 1"拼出。两边任何一方改了结构体布局，另一方都要跟着改。

### 2.2 选项串

`options` 指向一个字符串，格式刻意做得像挂载选项（`overlayOptions`，`overlay.go:533`）：

```
lowerdir=<层1>:<层2>:…:/mnt/lower,upperdir=<新 upper>,workdir=<新 work>,ovl_kind=<ckpt|restore>
```

规则如下（内核侧的解析在 `ovl_do_ioctl_checkpoint` 的开头）：

- **选项之间用逗号**分隔，`lowerdir` 内部各层之间用**冒号**分隔，最上面的层写在最前。因此层目录的路径里不能出现逗号或冒号。
- 只认四个键：`lowerdir`、`upperdir`、`workdir`、`ovl_kind`。出现别的键返回 `-EINVAL`。
- `lowerdir` 必填。给了 `upperdir` 就必须同时给 `workdir`。
- 每个路径在内核里用 `kern_path` 解析，起点是**调用进程的根**，并要求是目录。这就是 [05](05-guest-boot-and-injection.md) 里 `/mnt/lower` 与 `/overlay` 必须在新根里再 bind 一次的原因。
- `options_len` 为 0 或大于一页返回 `-EINVAL`。guest 内核是 4 KiB 页，所以整个选项串**不能超过 4096 字节**；长度不含结尾的空字符，内核自己补。

4096 字节这个上限直接决定了层栈能有多深——每多一层，`lowerdir` 就长一段。gsd 为此做了两件事：层目录用很短的名字 `L<n>`（`layerName`，`overlay.go:464`），
并且在调用 ioctl 之前自己先算长度（`checkOverlayOptionsLen`，`overlay.go:540`），超限时返回一个说得清原因的错误而不是让内核回一个看不出原因的 `-EINVAL`。
容量上的后果见 [13](13-state-concurrency-capacity.md)。

### 2.3 `ovl_kind`：两种切换的唯一区别

内核里两种切换走的是同一个函数，区别只在一个布尔量：`ovl_kind=restore` 时为 restore 模式，**其他任何值**（gsd 传的是 `ckpt`）都是 checkpoint 模式。
这个布尔量只决定一件事——**要不要把旧 upper 自动留在新层栈里**：

| | checkpoint 模式 | restore 模式 |
|---|---|---|
| 新层栈的第 0 层 | `upperdir` 给的新 upper | `upperdir` 给的新 upper |
| 新层栈的第 1 层 | **旧 upper，由内核自动加入**（下文称"自动 lower"） | `lowerdir` 的第一个 |
| 之后各层 | `lowerdir` 给的各层 | `lowerdir` 的其余各层 |
| 对内存里已有对象的处理 | promote（提升）：原来有 upper 的 inode 变成只有 lower、由自动 lower 那一层提供内容 | 丢弃、标记陈旧、摘出（§4） |

自动 lower 的条件写在补丁里是三个同时成立：旧的 overlay 有 upper、这次也给了 upper、不是 restore 模式。gsd 的 checkpoint 切换总是满足。

这带来一个读代码时必须留意的不对称，`SinkUpper`（`overlay.go:125`）里用了一整段注释说明它：

- checkpoint 时，gsd 先把当前 upper 目录改名成 `L<n>`，然后发 ioctl。内核自动加入的"旧 upper"就是这个刚改了名的目录（内核按目录项而不是按路径名引用它，改名不影响）。
  所以 gsd 传的 `lowerdir` **不包含** `L<n>`，只包含它下面的层——再显式传一次，同一个目录就会在层栈里出现两次。
- 但是 gsd 记录在 checkpoint 条目里的层栈**包含** `L<n>`。因为将来 restore 到这个 checkpoint 时走的是 restore 模式，没有自动 lower，层栈必须由 gsd 显式给全。

一句话：**同一个层栈，checkpoint 时传"除最上一层以外的"，restore 时传"完整的"**。

### 2.4 ioctl 拒绝什么

提交点（§3）之前的任何失败都不改变文件系统。gsd 实际可能遇到的返回值：

| 返回 | 条件 |
|---|---|
| `-EPERM` | 调用者没有 `CAP_SYS_ADMIN` |
| `-EINVAL` | 选项串为空、超过一页、有未知键、缺 `lowerdir`、给了 `upperdir` 却没给 `workdir` |
| `-EOPNOTSUPP` | 这个 overlay 挂载带有 index 目录（开了 `index=on` 或 `nfs_export=on`）。index 目录描述的是挂载时那个 upper，切换不会带上它 |
| `-EXDEV` | 新 upper 与当前 upper 不在同一个文件系统上。挂载时对 upper 所在文件系统探测到的能力（临时文件、扩展属性、带 whiteout 的改名等）会跨切换沿用，所以 upper 必须留在原来的文件系统上 |
| `-EBUSY` | 新 upper 或新 work 目录正被某个 overlay 实例占用（`ovl_inuse_trylock` 失败，§6.3） |
| `-ENOMEM` | 分配失败，或者在新 upper 下建内部的工作目录失败（磁盘满时会走到这里） |
| `-EINTR` | 等待串行化锁或读写锁时调用者收到了致命信号 |
| 路径解析的错误 | 某个层目录不存在等 |

`-EXDEV` 是"所有层必须在同一个 ext4 上"这条硬约束（[03](03-goals-and-tradeoffs.md)）在内核里的落点之一；另一个落点是 gsd 用 `rename` 做层下沉，`rename` 本身就不能跨文件系统。
按 `overlay-init` 搭出来的布局（[05](05-guest-boot-and-injection.md)），gsd 不会触发 `-EOPNOTSUPP` 和 `-EXDEV`。

---

## 3. 一次切换在内核里的过程

下面是 `0003-overlayfs-hot-switch-fixes.patch` 里 `ovl_do_ioctl_checkpoint` 的现状。把它分成三段来看：准备、提交、善后。
外层的 `ovl_ioctl_checkpoint` 先取一把互斥锁，保证同一个 overlay 上的两次切换不会交错——切换在开头读当前层栈、在提交点替换它，中间不能插进另一次切换。
这一节按顺序叙述流程；同一过程的阶段图和三把锁各管什么的表在 [07](07-overlay-concurrency-and-fixes.md) 的"总览：一次 ioctl 的阶段、提交点与锁"一节。

### 3.1 准备：所有可能失败的事

这一段里，正在使用的那个 overlay 的任何状态都**还没有被修改**，所以任何一步失败都只需要释放本次调用自己申请的东西：

1. 复制并解析选项串，做 §2.4 里的各项检查。
2. 对新 upper：解析路径，克隆一个私有挂载，检查与当前 upper 同文件系统，取得对它的写权限；对新 work 目录：解析路径，加"使用中"标记；给两者各建一个 trap inode（陷阱 inode）。
   trap inode 是 overlayfs 用来防止"某一层的目录又出现在 overlay 自己的视图里造成环"的机制，热切层沿用了它。
3. 对 `lowerdir` 里的每一层：解析路径，克隆一个只读的私有挂载，建 trap inode。checkpoint 模式下把旧 upper 的挂载填进自动 lower 的位置（多取一个引用）。
4. 分配新的根目录项的层栈描述，分配一个用于"退役"当前层数组的小结构。
5. 在新 upper 所在的文件系统上建内部的工作目录及其 trap inode。
6. **取读写锁的写端**（`switch_sem`）。所有读取 inode 或目录项后端状态的 overlay 操作都持这把锁的读端，所以从这里起整个 overlay 的操作被挡在门外，正在途中的操作被等完。
7. 收缩目录项缓存（`shrink_dcache_sb`），把没人引用的目录项清掉，这样下一步的"计划"只需要覆盖真正被钉住的对象——当前目录、打开的文件、挂载点以及它们的祖先。
8. **建计划**（`ovl_ckpt_build_plan`）：遍历这个超级块上所有内存中的 inode，为每个受切换影响的 inode 准备一个计划项，里面预先取好了它和它每个目录项的引用，预先分配好了要换上去的新结构。
   这是**最后一个可能失败的步骤**。

### 3.2 提交点

建计划成功之后就是提交点。补丁在这里有一句注释：从这里起不允许任何事情失败。原因很直接——接下来要改的是成千上万个彼此引用的内存对象，
没有办法在改到一半时回滚。所以设计上把一切要分配内存、要做磁盘操作、可能出错的事情都推到提交点之前，提交点之后只做"指针赋值"性质的动作：

1. 把所有目录 inode 的版本号加一，使它们缓存的目录内容失效。
2. 摘下属于旧 work 目录的状态：丢掉 whiteout 缓存，释放旧 upper 和旧 work 目录上的"使用中"标记。
3. 在一个顺序锁的写区间内，**成对地**发布新的层数组、新的层数和加一之后的代数 `checkpoint_gen`。读的一方通过 `ovl_layers_snapshot` 取这三样，保证不会拿到"新数组配旧长度"。
4. 按计划逐项改写 inode 和目录项（`ovl_ckpt_apply_item`，§4）。
5. 换上新的 work 目录、新的内部工作目录及两个 trap inode。
6. 替换根目录的层栈描述和根 inode 的 upper。
7. checkpoint 模式下，处理"以写方式打开着的文件"（§5.2）。

### 3.3 善后

仍在写锁内：

- 被换下来的对象（旧的层栈描述、旧的工作目录、被替换掉的目录项引用）不立刻释放，而是挂到一张"延迟释放"表上，等**下一次**切换拿到写锁时再放。
  这是因为有些使用者不持读写锁（路径查找的免锁快速路径、异步 I/O 的完成回调），它们可能还看得见这些对象；等到下一次切换拿到写锁，这些使用者必然都已经离开。
- restore 模式下再收缩一次目录项缓存，把刚被丢弃、已经没人用的目录项清掉。
- 把旧的层数组"退役"：能改指到新数组的层指针都改过去，没人再指向的旧数组在下一次切换时释放，其中的挂载交给一个工作队列项去卸载。

放开写锁之后，释放计划持有的引用，释放**上一次**切换留下的延迟释放表。

这一节里"延迟到下一次切换再释放""层数组退役与改指"都是为了在不引入释放后使用的前提下不泄漏，它们各自的论证见 [07](07-overlay-concurrency-and-fixes.md) 的"切换与所有 overlay 入口的互斥"和"回收：被换下的层、mount 与缓存引用"两节。

### 3.4 冻结的范围

从取到写锁到放开写锁之间，这个 overlay 上所有需要读后端状态的操作都在等待——这是切换对 guest 里**所有**进程（不只是用户进程）造成的停顿。
它的长度取决于内存里被钉住的 inode 数量，以及 §5.2 里要拷贝的文件大小。gsd 把一次 checkpoint 切换的耗时记在 `overlay_sink_ms`、一次 restore 切换记在 `overlay_restore_ms`
（位置见 [11](11-end-to-end.md)）；在 2026-10-08 的确认验证里两者都不到 1 ms（新 920B、`259f1a25c`、没有可写打开的大文件；判定值见 [20](20-results-and-gaps.md)）。

---

## 4. 代数与内存里已有的对象

切换改的是磁盘上"哪些目录算层"，但内核内存里早已缓存了大量 inode 和目录项，每一个都记着"我的内容来自哪一层的哪个真实文件"。切换之后这些记录全都可能是错的。
补丁用一个单调递增的代数 `checkpoint_gen` 标记"第几次切换之后"，并在提交点按计划改写所有缓存对象。计划的语义写在 `0003` 补丁里 `ovl_ckpt_item` 结构定义前的注释中，分两种模式。

### 4.1 checkpoint：promote

旧 upper 变成了新层栈里最上面的那个 lower。一个原来"有 upper"的 inode，它的内容没有变，只是所在的那一层从可写变成了只读。所以：

- inode 的 upper 引用移到 lower 的位置上，标记所在层为自动 lower 那一层，清掉"数据在 upper"的标志；
- 它的每个目录项换上一个新的层栈描述——对目录来说是"自动 lower 那一层的目录，后面接着它原来的各层"，这样目录合并的结果不变；
- inode 的代数更新为当前代数。

这正是"在新层栈里重新查找一次这个路径"会得到的结果。也就是说，promote 之后缓存与磁盘完全一致，进程感觉不到发生过切换。
之后第一次写这个文件，会像写任何一个只在 lower 里的文件一样，先 copy-up 到新 upper。

### 4.2 restore：丢弃、标记陈旧、摘出

restore 不保留旧 upper，而且 lower 也可能整个换了一组。缓存里的对象描述的是"被丢弃的时间线"，不能再作为新查找的结果：

- **所有目录项**被标记为陈旧并从目录项哈希里摘掉（`d_drop`）。之后的路径查找会重新走一遍 lookup，在新层栈里重建。
- **有 upper 的非目录 inode** 被"摘出"：它保留自己的数据，变成一个只有 lower 的 inode，由那个已被丢弃的旧 upper 提供内容，同时被从 inode 哈希里移除，这样新的查找永远找不到它。
  仍然拿着它的进程（打开着的文件）继续看到旧时间线的内容，但只读——对陈旧对象做 copy-up 会返回 `-ESTALE`。
- **目录**只是失去它的 upper，仍留在 inode 哈希里。之后的查找如果在新层栈里找到了同一个 lower 目录，就会把这个 inode 连同它的目录项"复活"。
  这样做是为了挂在它上面的东西：例如 `/var/lib` 是 overlay 上的目录，而 `/var/lib/gsd/snapshots` 是挂在它下面的 tmpfs，如果 `/var/lib` 的目录项不能复活，下面的挂载点就全部失联。
- **只存在于被丢弃 upper 里的目录**没有任何后端了：它被从 inode 哈希里移除，对它的操作返回 `-ESTALE`。

对进程的含义是：restore 之后，从根开始的新路径查找看到的是目标 checkpoint 的文件系统；而在 restore 之前就拿着的当前目录、打开的文件，可能看到旧时间线，或者得到 `-ESTALE`。
用户进程不受影响——它们在切换之前已经被 gsd 全部杀掉，恢复出来的进程由 CRIU 在新层栈上重新打开文件（[10](10-restore.md)）。
受影响的是不在回滚范围内的进程：root 起的服务、envd 的文件接口。这是 [03](03-goals-and-tradeoffs.md) 的取舍三，细节和它引出的几个修复在 [07](07-overlay-concurrency-and-fixes.md)，对使用者的说明在 [22](22-semantics-and-limits.md)。

### 4.3 代数还用在哪

提交点之后才第一次被查找到的 inode 不在计划里。如果一次查找命中了一个代数比当前小的缓存 inode，说明它是更早的时间线留下的：没有 upper 的可以接纳这次查找找到的 upper；
没有 upper 的目录还可以接纳这次找到的 lower（即上面说的"复活"）；对不上的非目录则被移出哈希、另建一个新 inode。这段逻辑在 `0003` 补丁对 `ovl_get_inode` 与 `ovl_refresh_reused_inode` 的修改里。

---

## 5. 打开的文件与内存映射

### 5.1 checkpoint 之前打开的文件描述符

一个以写方式打开的文件在打开时就已经被 copy-up，它背后的真实文件在**当时的** upper 里。checkpoint 把那一层变成了属于 checkpoint 的只读层；
如果之后的 `write()` 继续写到那个真实文件上，就等于在修改 checkpoint 的内容，并且这些修改在 restore 到该 checkpoint 之后依然存在——回滚就不成立了。

现在的做法（`0003` 补丁的 `ovl_write_switch`）是：切换之后第一次通过这样的描述符写入时，先把文件 copy-up 到当前的 upper（连数据一起），再把这个描述符改接到新 upper 里的真实文件上。
之后的写入都落在新 upper，checkpoint 层不再被触碰。

### 5.2 checkpoint 时仍以写方式打开的文件

§5.1 的"首次写时再搬"对内存映射不够：一个共享可写映射会绕过 `write()` 直接改页缓存，而页缓存属于旧的真实文件。所以对 checkpoint 那一刻**仍被以写方式打开**
（包括被共享可写映射着）的普通文件，内核在切换时主动处理（`ovl_ckpt_want_move` 判定，`ovl_ckpt_move_upper` 执行）：

- 把这个文件**原样搬到新 upper**——同一个 inode，打开的描述符和映射都跟着它走，从此写的是新 upper；
- 在下沉的那一层里放一份**拷贝**顶替它的位置，作为 checkpoint 的内容。两者用"交换"式的改名对调，下沉的层里任何时刻都不缺这个名字。

代价是在写锁内做一次整文件拷贝，期间 overlay 上的操作都被挡住。2026-09-30 的修复验证里测过三档：可写打开的文件为 16、64、128 MiB 时，切换耗时分别是 12.9、48.0、101.5 ms，
约 0.8 ms/MiB（新 920B，口径是 `overlay_sink_ms`；判定值见 [20](20-results-and-gaps.md)）。

这个行为由模块参数 `overlay.ckpt_move_open` 控制：`2`（默认）全部搬，`1` 只搬当时还被内存映射着的，`0` 不搬。有多个硬链接的文件不搬（它的其他名字会把 inode 留在下沉的层里），
内核为此打一条限速的告警，之后对它的写入会改到 checkpoint 的内容。这些都是已知限制，见 [15](15-known-limits.md)；为什么这样设计、怎么验证的，见 [07](07-overlay-concurrency-and-fixes.md) 的"打开的文件跨切换的语义"一节。

### 5.3 内存映射本身不触发 copy-up

`ovl_mmap` 现在的行为与上游 overlayfs 一致：把映射直接建在文件当前的真实文件上，**不做 copy-up**。只读映射和私有映射本来就不会写穿到文件；
共享可写映射要求文件以读写方式打开，而读写打开在打开时就已经 copy-up 过了。

这一条值得单独写出来，是因为交接前的补丁在这里对所有可写映射都做 copy-up，后果是每次 restore 都把被执行的二进制文件拷进 upper，根盘逐渐写满。
那是 restore 变慢的真根因，完整的归因过程在 [16](16-restore-slowness-case.md)；本章只需要记住现状：映射不拷文件。

---

## 6. gsd 一侧怎么用

`OverlayManager`（`ckpt/overlay.go:41`）记着四样东西：当前 upper、当前 work、当前层栈 `layers`（最上面的在前，最后一个是基础层 `/mnt/lower`）、两个序号。
它自己有一把锁 `mu`，与 `Service` 的操作锁是两回事——后台回收（[13](13-state-concurrency-capacity.md)）也要进这把锁。

### 6.1 `SinkUpper`：checkpoint 的切换

`SinkUpper`（`overlay.go:125`）按"先准备、后提交"的顺序做：

1. **upper 为空就什么都不做**（`isDirDirty`，`overlay.go:104`）：两次 checkpoint 之间没写过文件时，直接返回当前层栈，不下沉、不发 ioctl。
   这个 checkpoint 与上一个共用同一组层。这既省掉一次切换，也避免层栈被空层撑长。
2. 分配下一个层名 `L<n>`（`allocLayerPath`，`overlay.go:329`）：序号递增，并跳过磁盘上已经存在的名字。
3. 算出新层栈 `newLayers = [L<n>] + 当前层栈`，以及这次 ioctl 实际要传的 `ioctlLayers = 当前层栈`（§2.3 的不对称）。
4. **两次长度预检**（`checkOverlayOptionsLen`）：这一次 checkpoint 模式的选项串要放得下；**将来 restore 到这个 checkpoint 时**的选项串也要放得下——
   后者用完整的 `newLayers` 加上序号取最大整数时的 restore 目录名来算。第二项不满足时返回"checkpoint would not be restorable"，因为建出一个永远恢复不了的 checkpoint 比拒绝更糟。
   到这里什么都还没动。
5. 建新 upper 与新 work：`upper_<ckpt>_next`、`work_<ckpt>_next`（`makeUpperWork`，§6.4）。目录已存在算失败。
6. 把当前 upper 目录 `rename` 成 `L<n>`。
7. 发 ioctl（`switchLayers`，`overlay.go:522`）。
8. 成功后更新记录：当前 upper、当前 work、层栈；启动一次后台回收；返回新层栈给 `Service`，存进 checkpoint 条目。

失败时的回退：第 5 步失败删掉已建的目录；第 6 步失败删掉新目录；第 7 步失败删掉新目录并把 `L<n>` **改回原名**——因为内核按目录项引用这个目录，来回改名对正在挂载着的 overlay 没有影响。
万一改回也失败（不应发生），打一条错误日志并把"当前 upper"记成 `L<n>` 这个名字，保证记录与磁盘一致、下一次下沉仍然找得到它（`overlay.go:173-177`）。

### 6.2 `PrepareRestore` / `CommitRestore` / `AbortRestore`：restore 的切换

restore 的切换被拆成两半，是因为 `Service` 要在它们中间**杀进程**（[10](10-restore.md)）：杀进程不可逆，所以一切可能失败又不改变状态的事都要在杀之前做完。

`PrepareRestore`（`overlay.go:203`）只做准备，失败时什么都没动：

1. 取下一个 restore 序号，算出本次的目录名 `restore_<ckpt>_<seq>_upper` / `_work`。
2. 用目标层栈和这对目录名预检选项串长度。
3. 检查目标层栈里由 gsd 管理的每一层（`/overlay/layers/` 下的）仍然存在且是目录。基础层由 `overlay-init` 挂好，不归 gsd 管，不查。
4. 建好这对全新的目录，并把它们登记到 `pending` 集合里——在提交或放弃之前，后台回收不会把它们当成垃圾收走。

`CommitRestore`（`overlay.go:240`）发 restore 模式的 ioctl，`lowerdir` 是完整的目标层栈。成功后更新当前 upper、当前 work、层栈，并启动后台回收：
被切走的那个旧 upper（里面是这次 restore 丢弃的全部写入，可能很大）此刻成了垃圾。ioctl 失败时管理器的记录一点没动，只删掉准备好的两个目录。

`AbortRestore`（`overlay.go:260`）用于准备之后、杀进程之前就决定不做了的情况（例如扫描 `/proc` 失败），删掉准备好的目录。

### 6.3 为什么每次 restore 都用新名字的目录

overlayfs 对每个实例的 upper 和 work 目录加"使用中"标记，防止同一个目录被两个实例同时当 upper 用；切换的 ioctl 对新 upper 和新 work 也做这个检查，占用中就返回 `-EBUSY`。

设想目录名只按 checkpoint 命名：第一次 restore 到某个 checkpoint 用了 `restore_<ckpt>_upper`；紧接着第二次 restore 到同一个 checkpoint，新 upper 又是这个名字，
而它此刻正是当前实例的 upper——标记还在，ioctl 返回 `-EBUSY`。即使绕过标记，把"正在用的 upper"当作"全新的空 upper"也是错的，它里面有上一次 restore 之后的写入。

所以每次 restore 都用一个递增的序号造一对全新的目录（`nextRestoreSeq`，`overlay.go:510`）。序号随 `state.json` 持久化，gsd 重启后接着往上数，不会撞上磁盘上残留的名字。

### 6.4 新 upper 的根目录属性

`makeUpperWork`（`overlay.go:285`）建一对目录，并让新 upper 根目录的属主与权限**照抄一个参照目录**：下沉时参照当前 upper，restore 时参照目标层栈的最上层。
因为 overlay 根目录（guest 的 `/`）的属性来自 upper 的根目录，这样做的效果是 `/` 的属主与权限不因切换而改变。顺序是先改属主再改权限（改属主会清掉 setuid 与 setgid 位），权限带上粘滞位等高位。
work 目录只有内核使用，固定 0755。

### 6.5 一个会让人困惑的现象

切换不更新 `/proc/mounts` 里这个 overlay 的挂载选项：无论做过多少次 checkpoint 和 restore，那里显示的始终是开机时的
`lowerdir=/mnt/lower,upperdir=/overlay/workspace/upper,…`。要知道当前真正的层栈，看 gsd 的 `state.json` 或日志，不要看 `/proc/mounts`（[15](15-known-limits.md)、[25](25-troubleshooting.md)）。

---

## 7. arm64 的 soft-dirty

`0002` 补丁里与 overlayfs 无关的另一半，是给 arm64 加上 soft-dirty 支持——CRIU 的增量 dump 靠它知道哪些页被写过（原理在 [02](02-criu-and-incremental-memory.md)），而上游内核在 arm64 上没有实现它。要点只有几处：

- 架构配置里声明 `HAVE_ARCH_SOFT_DIRTY`；页表项里占用两个软件位：有效页表项的第 63 位（`PTE_SOFT_DIRTY`）和换出页表项的第 61 位（`PTE_SWP_SOFT_DIRTY`）；
- 把页表项标脏的函数同时置上 soft-dirty 位；更新访问标志时保留这一位；
- 页中间目录一级的同名操作直接复用页表项一级的实现，所以大页存在时 soft-dirty 的粒度是 2 MiB——这就是透明大页必须关掉的原因。

`0003` 补丁在这一半上只做了一件事：删掉 `fs/proc/task_mmu.c` 里遗留的调试计数与调试输出。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 接口定义、选项解析、`ovl_kind`、自动 lower | `0002-overlayfs-hot-switch-checkpoint.patch` — `OVL_IOCTL_CHECKPOINT`、`struct ovl_checkpoint_args`、`ovl_dir_ioctl`、`ovl_ioctl_checkpoint` |
| 切换的现状实现 | `0003-overlayfs-hot-switch-fixes.patch` — `ovl_ioctl_checkpoint`（串行化）、`ovl_do_ioctl_checkpoint`（主体） |
| 计划的建立与应用 | 同上 — `ovl_ckpt_build_plan`、`ovl_ckpt_plan_inode`、`ovl_ckpt_apply_item` |
| 成对发布层数组 | 同上 — `ovl_layers_snapshot` |
| 打开的文件 | 同上 — `ovl_write_switch`、`ovl_ckpt_want_move`、`ovl_ckpt_move_upper`、`ovl_ckpt_copy_file`、模块参数 `ckpt_move_open` |
| 内存映射 | 同上 — `ovl_mmap` |
| 缓存 inode 的接纳与复活 | 同上 — `ovl_get_inode`、`ovl_refresh_reused_inode` |
| arm64 soft-dirty | `0002-overlayfs-hot-switch-checkpoint.patch` 对 `arch/arm64/` 的修改 |
| gsd 的切换封装 | `ckpt/overlay.go` — `SinkUpper`、`PrepareRestore`、`CommitRestore`、`AbortRestore`、`switchLayers`、`applyOverlaySwitch` |
| 选项串与长度预检 | `ckpt/overlay.go` — `overlayOptions`、`checkOverlayOptionsLen`、`maxOverlayOptionsLen` |
| 目录的建立与命名 | `ckpt/overlay.go` — `makeUpperWork`、`allocLayerPath`、`layerName`、`restoreDirs`、`nextRestoreSeq` |

---

## 本章要点

1. 一次切换就是一次 ioctl：传一个像挂载选项的字符串，整串不超过 4096 字节；checkpoint 与 restore 走同一个函数，唯一区别是内核要不要把旧 upper 自动留作最上面的 lower。
2. 因此同一个层栈，gsd 在 checkpoint 时传"除刚下沉那层以外的"，在 restore 时传"完整的"；checkpoint 条目里记录的是完整的。
3. 内核把一切可能失败的事放在提交点之前，提交点之后只做不会失败的改写；提交点之前失败，文件系统没有任何变化。
4. 切换期间持读写锁的写端，overlay 上所有读后端状态的操作都在等；耗时取决于被钉住的 inode 数和要拷贝的可写打开文件的大小。
5. checkpoint 对缓存对象做 promote，进程无感；restore 对它们做"丢弃、标记陈旧、摘出"，目录可被之后的查找复活，范围外进程手里的旧对象看到旧时间线或得到 `-ESTALE`。
6. 以写方式打开的文件在 checkpoint 时被搬到新 upper、层里留拷贝；其余可写描述符在切换后首次写时改接到新 upper；内存映射不触发 copy-up。
7. gsd 侧一律"先准备、后提交"：长度预检和建目录在前，改名和 ioctl 在后，失败改回原名；每次 restore 用新序号的目录以避开 `-EBUSY`；upper 为空时不下沉。
