# 02 · CRIU 与增量内存原理

## 本章目标

读完本章，你应当能够：

- 说清 CRIU 的 dump 和 restore 各做了什么，镜像目录里有什么，gsd 传给它的每个选项是什么意思；
- 解释增量 dump 怎么只写"上一次以来改过的页"，以及为什么父镜像必须活得比子镜像久；
- 向一个没学过的人讲明白 soft-dirty：内核用页表里的一个位加一次写保护缺页，记下哪些页被写过；
- 解释为什么透明大页必须关（否则增量几乎每次都退化成全量），以及关掉它付出的代价；
- 认出"假全量"：明明只改了一点，增量却和全量一样大——并知道它的两个来源和各自的应对办法；
- 说出 CRIU 的一条语义"保存时停着的任务，恢复后仍然停着"，以及 gsd 有哪两处依赖它。

上一章（[01](01-problem-and-scope.md)）划定了回滚范围：用户进程树加 overlay 根文件系统。本章只讲进程那一半的原理，
不涉及 gsd 怎样把多棵树、文件系统切换和失败处理编排在一起，那是 [09](09-checkpoint.md) 和 [10](10-restore.md) 的内容。
文件那一半的原理在 [06](06-overlay-hot-switch.md)。下一章（[03](03-goals-and-tradeoffs.md)）把本章得出的几条约束
（内核必须带 soft-dirty、透明大页必须关）与其他约束放在一起，讲整体的取舍。

---

## 1. CRIU 做什么

CRIU（Checkpoint/Restore In Userspace）是一个用户态工具。给它一个进程，它能把这个进程和它的全部子孙"冻"成一组文件；
之后再给它这组文件，它能把这些进程原样建回来，从被冻住的那条指令继续执行。它不需要被保存的程序做任何配合，
但需要内核提供一些接口（编译选项 `CONFIG_CHECKPOINT_RESTORE`）。

gsd 自己不实现保存和恢复进程的逻辑，它做的是在合适的时机、用合适的参数调用 `/usr/sbin/criu`（路径写死，`criuBin`，`ckpt/criu.go:20`），
并处理调用前后的所有事情。

### 1.1 dump：把一棵进程树变成文件

一次 `criu dump` 大致经过这几步：

1. **接管**。CRIU 用 `ptrace` 接管目标进程树里的每一个任务，让它们停在当前位置。接管用的是不向进程发信号的方式，
   进程自己察觉不到。
2. **从外面收集**。读 `/proc/<pid>/` 下的各种文件，得到内存映射的布局、打开的文件、信号处理方式、进程间的父子关系等。
3. **从里面收集**。有些信息只有在进程自己的上下文里才拿得到。CRIU 往进程里临时注入一小段代码，让它替自己把内存页的内容送出来，
   用完再把这段代码清除干净。
4. **写镜像**。把收集到的东西写成一组镜像文件，放进指定的目录。
5. **放手**。CRIU 松开对任务的接管。这时任务的去向由选项决定：默认是杀掉（因为通常紧接着要在别处恢复）；
   `--leave-running` 是让它们继续跑；`--leave-stopped` 是让它们停在原地，等别人发 `SIGCONT`。

gsd 用的是 `--leave-stopped`，原因见 §6。

### 1.2 镜像目录

一次 dump 的产物是一个目录。gsd 给每个 checkpoint 的每棵树各准备一个：
`/var/lib/gsd/snapshots/<checkpoint ID>_mem/t<序号>/`（目录布局的总表在 [04](04-architecture.md)）。里面主要有：

| 文件 | 内容 |
|---|---|
| `inventory.img` | 这次 dump 的总目录：镜像格式版本、根任务是谁等。gsd 在 restore 之前用"它存在且读得出来"作为镜像可用的最低判据 |
| `core-<pid>.img` | 每个任务的寄存器、信号状态、调度参数，以及它被保存时是在运行还是停着 |
| `mm-<pid>.img` | 每个进程的内存映射布局（有哪些区域、各自的权限和来源） |
| `pagemap-<pid>.img` | 页面索引：哪些地址范围的页有内容，内容在本目录的 `pages-*.img` 里，还是"在父镜像里"（增量时，见 §2） |
| `pages-<n>.img` | 页面内容本身。镜像的体积几乎全在这里 |
| `fdinfo-*.img` 等 | 打开的文件、管道、socket 之类 |
| `parent` | 一个符号链接，只在增量 dump 时出现，指向父镜像目录（§2.2） |
| `criu-dump.log`、`criu-restore.log` | gsd 让 CRIU 把日志写在这里；`restored.pid` 是恢复出来的根进程号 |

镜像写的是**匿名内存**和共享内存的内容：堆、栈、进程自己改过的数据。文件映射里没被改过的部分（可执行文件、共享库的代码）
不写进镜像，恢复时从文件系统里重新映射。这有两个推论：镜像大小接近进程"自己的内存"的大小，而不是它的全部常驻内存；
恢复时文件系统里必须有内容一致的那些文件——这正是文件系统必须和进程一起回滚的原因之一。

### 1.3 restore：按原样重新建一遍

`criu restore` 做的是反过来的事，但它不是"把内存写回去"，而是**从零创建**：

1. 读镜像，弄清楚要建哪些进程、它们的父子关系；
2. 按原来的 PID 一个一个 fork 出来；
3. 在每个进程里重建内存映射，把页面内容从镜像读进去；
4. 重新打开文件、重建管道和 socket；
5. 恢复寄存器，让每个任务从被保存时的那条指令继续执行。

这里有两点对 gsd 影响很大。

**恢复出来的进程用的是原来的 PID。** 很多程序把 PID 记在内存里或文件里，换了 PID 会出错，所以 CRIU 坚持用原值。
这要求恢复时这些 PID 必须空着。如果 checkpoint 时的进程（或它的子进程）还活着、占着 PID，CRIU 会报 `Can't fork: File exists`。
所以 gsd 在 restore 之前必须把当前的用户进程**连同子孙**全部杀干净（[08](08-process-trees.md)、[10](10-restore.md)）。

**恢复要把进程的全部内存读回来。** 不管 checkpoint 之后进程只改了多少，恢复时都是整个地址空间重建一遍。
因此 restore 的耗时主要随进程的内存大小增长，而不随"这一步的改动量"增长——这与整机级回滚的成本形状正好不同（[18](18-process-vs-machine-level.md)）。

### 1.4 gsd 传给 CRIU 的选项

dump 的命令行由 `buildCriuDumpCmd`（`ckpt/criu.go:51`）拼出：

```
/usr/sbin/criu dump --tree <根 pid> -D <镜像目录> --shell-job --leave-stopped -v4 --track-mem [--prev-images-dir <父镜像目录>]
```

restore 的命令行由 `buildCriuRestoreCmd`（`ckpt/criu.go:100`）拼出：

```
/usr/sbin/criu restore -D <镜像目录> --restore-detached --shell-job --pidfile <镜像目录>/restored.pid
```

| 选项 | 含义 | gsd 为什么这样用 |
|---|---|---|
| `--tree <pid>` | 保存以这个进程为根的整棵树 | CRIU 一次只能保存一棵树。沙箱里有几棵用户进程树，gsd 就调用几次，每棵一个镜像目录（[08](08-process-trees.md)） |
| `-D <目录>` | 镜像目录 | 见 §1.2 |
| `--shell-job` | 允许保存"会话和终端属于树外某个进程"的任务 | 用户命令是 envd 启动的，它们所在的会话不是由树里的进程建立的。按 CRIU 的规则，这类任务不加这个选项不能保存 |
| `--leave-stopped` | dump 完让任务停在原地 | 见 §6：从 dump 结束到 soft-dirty 清零之间，进程不能再写内存 |
| `-v4` | 最详细的日志 | 失败时把日志尾部带进错误信息（[12](12-failure-semantics.md)）。只在 dump 时加 |
| `--track-mem` | 打开内存改动跟踪 | 让这次 dump 结束后内核开始记"之后哪些页被写过"，为下一次增量做准备。每次 dump 都加 |
| `--prev-images-dir` | 指定父镜像目录，本次只写相对它改过的页 | 只在能做增量时加（§2） |
| `--restore-detached` | 恢复完成后 CRIU 自己退出，不等被恢复的进程 | 被恢复的根进程原本是 CRIU 的子进程，CRIU 一退出它就被 PID 1 收养，从此与 gsd 没有父子关系 |
| `--pidfile` | 把恢复出来的根进程号写进文件 | gsd 读它来确认恢复出了谁，并据此记录下一次增量的起点 |

---

## 2. 增量：只写改过的页

### 2.1 为什么需要，以及怎么做

一个占 192 MiB 内存的进程，全量 dump 要写出约 192 MiB 的镜像。如果每一步都这样做，checkpoint 的耗时和占用就只取决于进程多大，
而与这一步做了多少事无关——Agent 的一步往往只改动很少的内存。

CRIU 的增量 dump 解决的就是这个问题。它需要两个选项配合：

- 上一次 dump 带了 `--track-mem`，于是从那次 dump 结束起，内核记下了每个页有没有被写过（怎么记的见 §3）；
- 这一次 dump 带 `--prev-images-dir <上一次的镜像目录>`。CRIU 逐页询问内核"这一页自上次以来被写过吗"：
  写过的，内容写进本次的 `pages-*.img`；没写过的，只在 `pagemap` 里记一条"这一段在父镜像里"，不写内容。

于是这一次的镜像只有改过的页那么大。

gsd 按**树**做增量：下一次 checkpoint 时，如果某棵树的根进程还是同一个（PID 相同，并且进程的启动时间也相同，
后者用来排除 PID 被复用的情况），就以上一个 checkpoint 里这棵树的镜像目录为父；新出现的树做全量。
restore 之后也可以接着做增量：恢复出来的进程从被恢复的那个镜像开始重新计脏，下一次 checkpoint 以它为父。
认定规则和各种边界情况见 `incrementalParent`（`ckpt/service.go:396`）与 [09](09-checkpoint.md)。

### 2.2 parent 链接，以及"父目录必须活得比子久"

增量镜像自己是不完整的：它只有改过的页，其余的页要到父镜像里去取；父镜像如果也是增量，就再往上找。
CRIU 用镜像目录里的一个符号链接 `parent` 把这条链串起来。restore 时不需要再指定父目录，
CRIU 沿着 `parent` 一级一级往上读（所以 `buildCriuRestoreCmd` 里没有 `--prev-images-dir`）。

这带来一条贯穿全书的不变量：

> **一个镜像目录只要还有子镜像依赖它，就不能删。**

删掉了，所有依赖它的 checkpoint 都恢复不出来，而且是在杀掉进程之后才发现。gsd 围绕这条不变量做了三件事：

- 删除一个还被依赖的 checkpoint 时只是把它**隐藏**（列表里看不到、不能再 restore），镜像留到最后一个依赖它的 checkpoint 也被删除为止
  （[13](13-state-concurrency-capacity.md)）；
- restore 在杀进程**之前**先检查目标镜像的 `inventory.img` 可读，并沿 `parent` 一路往上，确认每一级父目录都在
  （`checkImages`，`ckpt/service.go:703`）。查出问题就拒绝，这时什么都还没动；
- 任何一步失败之后，下一次 dump 一律做全量，不再接到一条可能已经不可靠的链上（[09](09-checkpoint.md)、[12](12-failure-semantics.md)）。

---

## 3. soft-dirty：内核怎么知道哪些页被写过

增量的前提是能回答"这一页自某个时刻以来被写过没有"。Linux 回答这个问题的机制叫 soft-dirty。

### 3.1 思路：一个标记位，加一次"故意的"缺页

进程的每一页虚拟内存在页表里有一个页表项（PTE），记录它对应哪一页物理内存、能不能写。
页表项里有几个处理器不使用、留给操作系统自由支配的位。soft-dirty 占用其中一个，含义是"这一页从上次清零以来被写过"。

难点在于：进程写内存是一条普通的机器指令，不经过内核，内核怎么知道某一页刚被写了？办法是**先把页设成只读**：

1. **清零并上锁。** 往 `/proc/<pid>/clear_refs` 写入字符 `4`。内核遍历这个进程的全部页表项，对每一页做两件事：
   清掉 soft-dirty 位，并去掉写权限（即使这一页本来是可写的）。
2. **第一次写触发缺页。** 之后进程一旦写某一页，处理器发现页表项不允许写，产生一次缺页异常，控制权进入内核。
3. **内核记账并放行。** 内核检查后发现这一页所在的内存区域其实允许写，于是恢复写权限，同时把 soft-dirty 位置上，然后让进程重新执行那条写指令。
4. **之后再写不再打扰。** 这一页已经可写，同一页上的后续写入不再经过内核。

所以每一页从"干净"变"脏"恰好付一次缺页的代价，之后不再有开销；没被写的页一直保持只读、标记位一直是零。

```
            写 clear_refs = 4                    进程第一次写这一页
  ┌──────────────┐        ┌──────────────────┐         ┌──────────────┐
  │ 可写, 脏=?    │ ─────▶ │ 只读, soft-dirty=0 │ ──缺页─▶ │ 可写, soft-dirty=1 │
  └──────────────┘        └──────────────────┘         └──────────────┘
                                   │ 没有写                      │ 再写：不经过内核
                                   ▼                             ▼
                            一直保持 0（干净）              一直保持 1（脏）
```

查询的入口是 `/proc/<pid>/pagemap`：这个文件对每一页虚拟内存给出一个 64 位的描述，其中第 55 位就是"soft-dirty"。
CRIU 做增量 dump 时读的就是它。

把上面的机制对应到 CRIU 的两个选项：`--track-mem` 就是让 CRIU 在 dump 的过程中替我们做第 1 步；
`--prev-images-dir` 就是让 CRIU 在下一次 dump 时去读 pagemap，只保存 soft-dirty 为 1 的页。

### 3.2 两级标记：页表项上一级，内存区域上一级

除了每一页自己的标记位，内核还在每个**内存区域**（VMA，进程地址空间里连续的一段，例如堆、某次 `mmap` 得到的一块）上放了一个标志 `VM_SOFTDIRTY`。
它的作用是处理"新出现的内存"：

- 一块新映射出来的内存，页表项可能还不存在（页还没被碰过），没有地方放页级的标记；
- 但对增量来说，新映射的区域相对上一次 dump 显然是"变了的"，必须整块保存。

所以内核在创建或扩大一个内存区域时给它打上 `VM_SOFTDIRTY`；pagemap 在报告时，只要一页所在的区域带着这个标志，
就把这一页报成脏，不管页表项上的位是多少。写 `clear_refs` 时，这个区域级的标志也一起被清掉。

**记住这个区域级的标志**，§5 的"假全量"全是它引起的。

### 3.3 arm64 上本来没有，靠补丁加

soft-dirty 需要每个处理器架构各自实现：在页表项里选哪一位、在哪些路径上置位和清位。本书用的 guest 内核是 6.1，
这个版本的上游内核在 arm64 上没有实现它。没有它，`--track-mem` 无从谈起，每次 dump 都只能是全量。

热切层补丁 `0002-overlayfs-hot-switch-checkpoint.patch` 顺带补上了这一块（改动集中在 `arch/arm64/` 下）：

| 改动 | 作用 |
|---|---|
| `arch/arm64/Kconfig` 增加 `select HAVE_ARCH_SOFT_DIRTY` | 告诉内核配置系统：arm64 支持 soft-dirty 了，`CONFIG_MEM_SOFT_DIRTY` 才选得上 |
| `pgtable-prot.h` 定义 `PTE_SOFT_DIRTY` 为第 63 位、`PTE_SWP_SOFT_DIRTY` 为第 61 位 | 在页表项里给标记找位置。第 63 位只在页表项有效时有意义；页被换出时页表项是另一种格式，标记放在第 61 位 |
| `pgtable.h` 增加 `pte_soft_dirty` / `pte_mksoft_dirty` / `pte_clear_soft_dirty` 及换出页、大页（pmd）的对应函数 | 内核通用代码通过这些函数读写标记 |
| `pte_mkdirty` 改成同时置 soft-dirty；原来的逻辑保留为 `__pte_mkdirty` | 页因为被写而变脏时顺便置上标记。那些只是"搬运"已有脏状态的地方（`pte_modify`、大页相关的几处）改用 `__pte_mkdirty`，避免把没被写的页误标成脏 |
| `arch/arm64/mm/fault.c` 的 `ptep_set_access_flags` 在保留位掩码里加上 `PTE_SOFT_DIRTY` | 这个函数在缺页处理的最后把新的权限写回页表项，原来只保留访问、写权限和脏这几位；不加的话刚置上的标记在写回时会被丢掉 |

配置上需要同时打开 `CONFIG_CHECKPOINT_RESTORE=y`（`fc-kernels/configs/arm64/6.1.158.config:196`）和
`CONFIG_MEM_SOFT_DIRTY=y`（同文件 `:3359`）。x86_64 的那份配置没有打开 `CONFIG_CHECKPOINT_RESTORE`，
这是 gsd 目前只支持 arm64 guest 的原因之一（[03](03-goals-and-tradeoffs.md)）。

---

## 4. 透明大页的两难

### 4.1 透明大页是什么

通常一页内存是 4 KiB。透明大页（Transparent Huge Pages，THP）是内核的一项优化：在条件允许时，
自动用一个 2 MiB 的大页来承载一段连续的匿名内存，程序不需要做任何修改。好处是页表更小、缺页次数更少：
填满 192 MiB 内存，用 4 KiB 的页要缺页约四万九千次，用 2 MiB 的页只要 96 次。

开关在 `/sys/kernel/mm/transparent_hugepage/enabled`，有三个取值：`always`（尽量都用大页）、`madvise`（程序主动要求时才用）、`never`（不用）。
本书的 arm64 guest 内核配置默认是 `always`（`CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS=y`，`fc-kernels/configs/arm64/6.1.158.config:930`）。

### 4.2 它为什么毁掉增量

回到 §3.1：soft-dirty 记在**页表项**上，一个页表项一个位。一个 2 MiB 的大页在页表里只有一个表项（比普通页表项高一级，叫 pmd），
所以它也只有**一个** soft-dirty 位。于是：

> 在一个 2 MiB 的大页里改一个字节，整个 2 MiB 都算脏。

真实程序的写入是零散的：这里改一个对象，那里改一个计数器。只要每个大页里都被碰到一点，增量就和全量一样大。
下面这组实测把这一点展示得很清楚（一个 192 MB 堆的进程，用同一种写法各做一次增量 dump）：

| 写法 | 真正被写的量 | 透明大页 `always` 时的增量镜像 | `never` 时的增量镜像 |
|---|---|---|---|
| 零散地碰 96 个 4 KB 的页（每 2 MB 碰一个） | 0.375 MB | 190.5 MB | 0.8 MB |
| 零散地碰 3072 个 4 KB 的页（每 64 KB 碰一个） | 12 MB | 190.5 MB | 12.4 MB |
| 连续写满 192 MB | 192 MB | 195.3 MB | 195.3 MB |

> 条件：旧 920B（已退役），2026-09-11，交接时的代码，在沙箱里直接运行 CRIU（不经过 gsd），单位照原记录写作 MB。
> 只用来说明机制；当前代码的判定值见 [20](20-results-and-gaps.md)。

第一行是最极端的情形：真正改动的只有 0.375 MB，大页开着时增量却是 190.5 MB，放大了约五百倍；关掉之后是 0.8 MB。
只有第三行那种"整块连续写满"的负载，开不开没有区别——但那不是真实程序的样子。

### 4.3 关掉的代价

把透明大页设成 `never` 不是没有代价的。恢复一个进程要把它的全部内存重新填一遍（§1.3），
用小页填意味着多得多的缺页和页表操作。同一份镜像、同一个沙箱、只切换这一个开关的对照里，
restore 中 CRIU 那一段的中位数从 145 ms（`always`）变成 173 ms（`never`）。

> 条件：旧 920B（已退役），2026-09 上旬，交接时的代码，gsd 路径的 `criu_restore_ms`。这两个数处在后来一系列修复之前，
> 绝对值早已不同，这里只看方向。修复之后 restore 的耗时构成见 [17](17-performance-after-fixes.md)，判定值见 [20](20-results-and-gaps.md)。

### 4.4 结论：`never` 是设计约束

这是一个真正的两难：开着，增量失去意义，每次 checkpoint 都按进程大小付费；关着，restore 慢一些。
soft-dirty 的粒度是由"一个页表项一个位"决定的，在这个前提不变的情况下，gsd 只能选择关。
全书的所有测量都是在 `never` 下做的；与整机级比较时也必须带上这个条件——整机级的脏页跟踪在宿主侧按 4 KiB 记账，
guest 里开不开透明大页不影响它的增量精度（[18](18-process-vs-machine-level.md)）。

因为它是正确使用的前提，不能指望使用者记得去设，所以有两处代码保证它：

- **`overlay-init` 在开机时设置。** 沙箱的开机脚本在切换到真正的 `systemd` 之前把开关写成 `never`，并检查确实生效。
  选这个时机是为了让任何用户进程都没有机会拿到大页（[05](05-guest-boot-and-injection.md)）。
- **gsd 启动时兜底。** `ensureTHPNever`（`packages/gsd/main.go:196`）读一次开关，不是 `never` 就改成 `never` 并打一条警告。
  这只是兜底：在这个时刻才改的话，此前已经拿到大页的进程不会立刻变回小页，它们的增量仍然会偏大，直到这些大页被拆掉或进程重启。

现场确认过这两处落实后的效果：模板开机后开关是 `[never]`，内核日志里有 `overlay-init` 的设置记录；
一个 256 MiB 的进程做零散写入，全量 checkpoint 的镜像是 260 MiB，紧接着的增量是 1.05 MiB，进程没有占用任何大页。

> 条件：新 920B，2026-09-30，提交 `a35ce1993`，功能验证脚本的对应用例，gsd 的 `image_bytes`。判定值见 [20](20-results-and-gaps.md)。

---

## 5. "假全量"：没改多少，增量却很大

关掉透明大页之后，还会遇到另一种现象：某一次增量突然和全量一样大，下一次又恢复正常。
它与透明大页无关，原因是 §3.2 里那个**区域级**的标志 `VM_SOFTDIRTY`。

### 5.1 来源一：两次 dump 之间分配过新内存

程序在两次 dump 之间如果向系统要过新的内存（一次大的 `malloc` 在底层是一次 `mmap`，用完再 `munmap`），
内核会新建一个内存区域并给它打上 `VM_SOFTDIRTY`。新区域如果紧挨着一个已有的区域、属性又相同，内核会把两者**合并**成一个区域，
合并后的区域带着这个标志。结果是：原来那个很大的、其实没怎么改的区域，现在整个被报成脏。

下面是一组对照（透明大页 `never`，64 MB 的堆，每次的目标都是改写其中 12 MB）：

| 写法 | 增量镜像 |
|---|---|
| 每次先造一个 1 MB 的临时对象，再用它覆盖目标位置 | 68 MB（等于全量） |
| 原地逐字节改写，不分配任何新对象 | 13 MB |
| 再用第一种写法做一次 | 16 MB（区域的布局稳定下来之后恢复正常） |

> 条件：旧 920B（已退役），2026-09 上旬，交接时的代码，单位照原记录写作 MB。只用来说明机制。

第一行的"1 MB 临时对象"就是触发点：Python 为它向系统申请了一块新内存，这块内存与堆合并，整个堆被报成脏。

**怎么避免。** 这不是 gsd 能替应用解决的，但写负载和测试脚本时可以注意：

- 两次 checkpoint 之间尽量不要有大块内存的申请和释放。需要缓冲区就在启动时分配好，之后复用；
- 尤其是性能测试：测试进程应当把用来写入的数据块预先分配一次，否则测到的是"假全量"，不是增量。
  随手册附带的性能脚本都是这样写的；
- 第一次增量偏大、之后恢复正常，多半是这个原因，不是故障。

**怎么判断是不是它。** `CreateCheckpoint` 的响应里有 `image_bytes`（本次镜像的实际大小）和 `parent_checkpoint_id`（是否做了增量）。
如果 `parent_checkpoint_id` 不为空而 `image_bytes` 接近进程大小，就是假全量；排查步骤在 [25](25-troubleshooting.md)。

### 5.2 来源二：overlay 切换本身

同一类问题还有一个出在 gsd 自己身上的来源。checkpoint 的后半段要把 overlay 的可写层沉下去（[06](06-overlay-hot-switch.md)），
内核在切换时要清理这个文件系统的目录项缓存。按代码里的记录，这个清理会让用户进程的内存区域重新带上 `VM_SOFTDIRTY`
（`CreateCheckpoint` 中调用 `clearSoftDirty` 之前的注释，`ckpt/service.go:331`）。

后果是：CRIU 在 dump 过程中已经清过一次 soft-dirty，但紧接着的层切换又把区域级的标志打了回去。
如果不处理，下一次增量 dump 时这些区域整个被报成脏，**每一次**增量都变成全量。

所以 gsd 在层切换之后，对刚才保存过的全部进程再写一遍 `clear_refs`（`clearSoftDirty`，`ckpt/service.go:418`；
实际写入的是 `writeClearRefs`，`ckpt/service.go:434`）。某个进程写不成功不会让 checkpoint 失败，
但会计入响应里的 `clear_refs_failed`，表示这些进程的下一次增量会比应有的大。

这次重写带来了一个时序上的要求，下一节讲。

---

## 6. 清零的时机：为什么保存完要让进程停着

把一次 checkpoint 里与 soft-dirty 有关的动作按时间排开：

```
  CRIU dump（过程中清一次 soft-dirty）──▶ overlay 层切换 ──▶ gsd 重写 clear_refs ──▶ 进程继续运行
                                  └────────── 这一段里进程必须停着 ──────────┘
```

设想 dump 结束后进程立刻恢复运行（`--leave-running`），在 gsd 重写 `clear_refs` 之前的这一小段时间里写了某一页：

1. 这一页的内容**没有**进这次的镜像（dump 已经结束了）；
2. 写入让这一页的 soft-dirty 置上了，这是对的，下一次增量应当带上它；
3. 但随后 gsd 重写 `clear_refs`，把这个标记**清掉了**；
4. 下一次增量 dump 认为这一页没变，不保存它；
5. 以后恢复到那个 checkpoint 时，这一页的内容来自更早的父镜像——是旧值。

这是一种**静默的内存错误**：checkpoint 和 restore 都报成功，进程也能跑，只是有几页内存的内容属于另一个时刻。
交接前的实现用的正是 `--leave-running`，存在这个窗口。

修复后改用 `--leave-stopped`：CRIU 保存完一棵树就让它停在原地，gsd 做完层切换、重写完 `clear_refs` 之后，
才统一发 `SIGCONT` 放行。在这段时间里进程一条指令也不执行，自然不会写内存。多棵树时这个安排还带来了文件系统的一致性：
先保存完的树在等待期间不会写文件（论证见 [09](09-checkpoint.md)）。

针对这个窗口有一个专门的验证：让一个进程以固定速率不停地写内存，反复做"checkpoint、停住、读出内存作为期望值、
再 checkpoint、restore、逐页比对"。新 920B 上 2026-10-08 对提交 `259f1a25c` 跑了 30 轮，没有一轮出现内容不符的页
（判定值与脚本说明见 [20](20-results-and-gaps.md)、[19](19-testing-and-debug-kernel.md)）。

进程停着的时间会计入响应里的 `stopped_ms`，它的定义和量级见 [11](11-end-to-end.md)、[24](24-configuration-and-observability.md)。

---

## 7. 一条被依赖的语义：保存时停着的，恢复后仍停着

CRIU 在接管一个任务时会看它当时的状态。如果任务在被接管之前**已经处于停止状态**（收到过 `SIGSTOP`，例如用户按了 Ctrl-Z，
或者被调试器停住），CRIU 会在镜像里把它记成"停着的"；恢复时，CRIU 在把它建出来之后再让它回到停止状态。
换句话说，CRIU 忠实地保存并还原"这个任务是在跑还是停着"。

注意这与 `--leave-stopped` 是两回事：`--leave-stopped` 说的是 dump **结束之后**把任务留在什么状态，
不改变镜像里记录的状态。一个 dump 时正在运行的任务，即使因为 `--leave-stopped` 而在 dump 后停着，镜像里它仍是"运行中"，
恢复出来会直接开始跑。

gsd 有两处依赖这条语义。

**依赖一：保留用户自己停住的进程。** 用户可能有意停住了某个进程。checkpoint 不应该把它放开，restore 之后它也应该还停着。
后一半由 CRIU 的这条语义自动保证；前一半由 gsd 保证：在第一棵树 dump 之前，gsd 记下每个进程当时是否停着，
dump 结束后只给"dump 之前不是停着的"进程发 `SIGCONT`（`resumable`，`ckpt/pid.go:414`；[08](08-process-trees.md)）。

**依赖二：gsd 不能在 dump 之前自己发 `SIGSTOP`。** 多棵树的情况下，一个很自然的想法是先把所有树都停住，
再一棵一棵慢慢保存，这样各棵树的状态严格是同一时刻的。但按上面的语义，被 gsd 停住的任务会在镜像里被记成"停着的"，
恢复出来全都不动；而 gsd 没有办法区分"我停的"和"用户停的"。所以 gsd 不在 dump 之前发信号，
而是靠 `--leave-stopped` 让每棵树保存完之后停下。这个选择的代价是树与树之间有很短的先后差，
论证写在 `CreateCheckpoint` 的函数头注释里（`ckpt/service.go:147`），展开见 [09](09-checkpoint.md)。

---

## 代码位置

| 内容 | 位置 |
|---|---|
| CRIU 的路径（写死） | `criuBin`（`ckpt/criu.go:20`） |
| dump 命令行 | `buildCriuDumpCmd`（`ckpt/criu.go:51`） |
| restore 命令行 | `buildCriuRestoreCmd`（`ckpt/criu.go:100`） |
| 带超时地运行 CRIU | `runCriuCmd`（`ckpt/criu.go:28`） |
| 选父镜像（按树增量） | `incrementalParent`（`ckpt/service.go:396`） |
| 层切换后重写 `clear_refs` | `clearSoftDirty`（`ckpt/service.go:418`）、`writeClearRefs`（`ckpt/service.go:434`） |
| restore 前检查镜像与 parent 链 | `checkImages`（`ckpt/service.go:703`） |
| 只放开 dump 前没停着的进程 | `resumable`（`ckpt/pid.go:414`） |
| 不在 dump 前发 `SIGSTOP` 的论证 | `CreateCheckpoint` 的函数头注释（`ckpt/service.go:147`） |
| 透明大页的兜底检查 | `ensureTHPNever`（`packages/gsd/main.go:196`） |
| 开机时关闭透明大页 | `overlay-init.sh.tpl` 的 Step 6（`packages/orchestrator/internal/template/build/core/rootfs/gsdfiles/`） |
| arm64 的 soft-dirty | `0002-overlayfs-hot-switch-checkpoint.patch` 中 `arch/arm64/` 下的改动：`PTE_SOFT_DIRTY`、`pte_mksoft_dirty`、`pte_mkdirty`、`ptep_set_access_flags` |
| 内核配置 | `fc-kernels/configs/arm64/6.1.158.config`（`:196`、`:930`、`:3359`） |

---

## 本章要点

1. CRIU 的 dump 接管一棵进程树、把它写成一个镜像目录；restore 是**按原 PID 从零重建**，要把进程的全部内存读回来。
   前者决定了 checkpoint 可以做增量，后者决定了 restore 之前必须杀干净旧进程、restore 的耗时随进程大小增长。
2. 增量靠 `--track-mem` 加 `--prev-images-dir`：只写上次以来改过的页，其余的记为"在父镜像里"，由 `parent` 符号链接串成链。
   因此**父镜像必须活得比子镜像久**，删除、检查和失败处理都围绕这一条。
3. soft-dirty 的原理是"清标记并去掉写权限 → 第一次写触发缺页 → 内核置标记并放行"。6.1 的上游内核在 arm64 上没有它，
   由热切层补丁在页表项第 63 位（换出页第 61 位）上实现。
4. 透明大页开着时，一个 2 MiB 的大页只有一个标记位，零散写入会让增量放大到接近全量；所以 **`never` 是设计约束**，
   由 `overlay-init` 在开机时设置、gsd 启动时兜底。代价是 restore 变慢。
5. "假全量"来自内存区域级的 `VM_SOFTDIRTY`：两次 dump 之间分配新内存会触发（负载要预分配、原地写）；
   overlay 层切换也会触发，所以 gsd 在切换之后重写一次 `clear_refs`。
6. 从 dump 结束到 `clear_refs` 重写完成，进程必须停着，否则这段时间写的页会从下一次增量里静默漏掉。这是用 `--leave-stopped` 的原因。
7. CRIU 把"保存时已经停着的任务"恢复成停着的。gsd 靠它保留用户自己停住的进程，也因此不能在 dump 之前自己发 `SIGSTOP`。
