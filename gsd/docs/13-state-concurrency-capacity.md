# 13 · 状态、并发与容量

## 本章目标

读完本章，你应当能回答：

1. gsd 的状态有哪几块、存在哪里；`state.json` 里记了什么，什么时候写、怎么保证写到一半崩溃也不坏？
2. gsd 重启、guest 重启、沙箱 pause/resume 这三种情况下，状态各自会怎样；为什么"状态文件不存在"可以当成"刚开机"？
3. 删除一个被别的 checkpoint 依赖的 checkpoint 时发生了什么，空间什么时候才真正释放？
4. 层目录、临时目录、镜像目录各按什么规则回收，为什么都是"锁内改名、锁外删除"？
5. 并发的请求怎么处理；容量预检和层栈深度预检各自估的是什么、偏向哪一边？

---

上一章（[12](12-failure-semantics.md)）讲的是一次操作失败之后沙箱处于什么状态。本章讲的是 gsd 自己：它记着哪些东西，
这些东西怎么在崩溃和重启之间保持与内核一致，多个请求同时到来时怎么办，以及它怎么避免把 guest 的内存和根盘用光。
这些机制大部分是修复过程中补上的：修改前状态全在内存里、没有锁、不回收层目录、不估算镜像大小，对应的后果在各节的开头用一两句话交代，完整的问题清单在 [14](14-issue-resolution.md)。

---

## 1. gsd 记着什么

gsd 的状态分四块，全部在 guest 里，宿主看不见（这是 [03](03-goals-and-tradeoffs.md) 的取舍一）：

| 状态 | 内容 | 在内存里的位置 |
|---|---|---|
| checkpoint 池 | 每个 checkpoint 的 id、名字、创建时间、它的层栈（一串层目录）、每棵进程树的镜像（根的 pid 与 starttime、子目录名、增量父镜像所在的 checkpoint、占用字节数）、是否已被标记删除 | `pool`（`ckpt/pool.go:92`） |
| 增量父镜像记录 | 每棵活着的用户进程树下一次增量 dump 该以哪个镜像目录为父 | `Service.lastTrees`（`ckpt/service.go:66`） |
| overlay 管理器 | 当前的 upper 与 work 目录、当前层栈、最近分配的层序号、restore 序号 | `OverlayManager`（`ckpt/overlay.go:41`） |
| degraded 原因 | 三类，见 [12](12-failure-semantics.md) | `Service` 的三个字段（`ckpt/service.go:78-90`） |

磁盘上与之对应的是两处目录：根盘 ext4 上的 `/overlay/layers/`（层目录和临时的 upper、work 目录）和 tmpfs 上的 `/var/lib/gsd/snapshots/`（CRIU 镜像和状态文件）。
目录布局的总表在 [04](04-architecture.md)，这里只在用到时提及。

这四块状态里最要紧的是 overlay 管理器的那一块，因为它必须与**内核里实际挂着的层栈**一致：gsd 每次切换都要把完整的层栈拼成选项串传给内核，
拼错一层，轻则 ioctl 失败，重则切到一个内容不对的文件系统上。而内核里的层栈没有办法反过来查询（`/proc/mounts` 一直显示开机时的那几层，见 [15](15-known-limits.md)），
所以 gsd 的记录是唯一的依据，丢了就无从恢复。

---

## 2. `state.json`

### 2.1 为什么需要它

修改前状态全在内存里。gsd 的服务单元配置了 `Restart=always`，进程崩溃后会被立刻拉起，新进程的池是空的，
overlay 管理器回到初始值：当前 upper 指向开机时的 `/overlay/workspace/upper`，层栈只有基础层。
而只要此前做过一次 checkpoint，那个路径就已经被改名成层目录、不存在了，内核里的层栈也不止一层。
于是重启后的第一次 create 必然失败，此前所有的镜像和层目录都成了没有记录的孤儿，占着内存和磁盘再也收不回来。

现在的做法是把第 1 节的四块状态（degraded 里只需要"restore 半途失败"那一类）写进 `<snapshot store>/state.json`，启动时加载。

### 2.2 内容

文件是带缩进的 JSON，当前格式版本为 2（`ckpt/state.go:31-37`）。下面是一个示意（数值是编的，字段名与结构对应 `persistedState`，`ckpt/state.go:39`）：

```json
{
  "version": 2,
  "overlay": {
    "current_upper": "/overlay/layers/upper_ckpt_1760000000000000002_next",
    "current_work":  "/overlay/layers/work_ckpt_1760000000000000002_next",
    "layers": ["/overlay/layers/L2", "/overlay/layers/L1", "/mnt/lower"],
    "layer_seq": 2,
    "restore_seq": 0
  },
  "last_trees": [
    {"root_pid": 512, "root_start": 48211, "checkpoint_id": "ckpt_1760000000000000002",
     "dir": "/var/lib/gsd/snapshots/ckpt_1760000000000000002_mem/t0"}
  ],
  "checkpoints": [
    {"id": "ckpt_1760000000000000001", "mem_path": "/var/lib/gsd/snapshots/ckpt_1760000000000000001_mem",
     "layers": ["/overlay/layers/L1", "/mnt/lower"], "created_at": 1760000000000000001,
     "trees": [{"root_pid": 512, "root_start": 48211, "dir": "t0", "image_bytes": 201326592}],
     "had_processes": true, "image_bytes": 201326592},
    {"id": "ckpt_1760000000000000002", "mem_path": "/var/lib/gsd/snapshots/ckpt_1760000000000000002_mem",
     "layers": ["/overlay/layers/L2", "/overlay/layers/L1", "/mnt/lower"], "created_at": 1760000000000000002,
     "trees": [{"root_pid": 512, "root_start": 48211, "dir": "t0",
                "parent_id": "ckpt_1760000000000000001", "image_bytes": 1048576}],
     "parent_id": "ckpt_1760000000000000001", "had_processes": true, "image_bytes": 1048576}
  ]
}
```

几个字段的含义值得说明：

- `overlay.layers` 是当前层栈，最上面的层在前。它与每个 checkpoint 条目里的 `layers` 是两回事：前者是"现在挂着什么"，后者是"restore 到这个 checkpoint 时要挂什么"。
- `last_trees` 为空表示下一次 create 每棵树都做全量。它可能与"最新的 checkpoint"不是同一个：restore 成功之后它指向被恢复的那个 checkpoint（[10](10-restore.md)）。
- `checkpoints` 包含已被标记删除、但因为还有子节点而保留的条目，带 `"deleted": true`。这些条目对外不可见，但它们的镜像目录和层目录仍然受保护（第 4 节）。
- checkpoint 条目上的 `parent_id`、`had_processes`、`image_bytes` 是从 `trees` 汇总出来的冗余值，写出来只为了人读方便，加载时以 `trees` 为准。
- `restore_failure` 只在上一次 restore 半途失败时出现，内容就是 degraded 的原因字符串。

排障时这个文件是第一手材料：它是 gsd 眼里的全部事实。怎么读、怎么与磁盘对照在 [24](24-configuration-and-observability.md) 和 [25](25-troubleshooting.md)。

### 2.3 原子写

`writeStateFile`（`ckpt/state.go:296`）的写法是标准的四步：在同一目录下建临时文件并写入全部内容，对临时文件 `fsync`，把它 `rename` 成 `state.json`，再对目录 `fsync`。
`rename` 在同一文件系统内是原子的，所以任何时刻读 `state.json`，看到的要么是完整的旧版本，要么是完整的新版本，不会是写了一半的文件。
写到一半崩溃留下的临时文件（名字形如 `.state.json.tmp-*`）在下次启动时清掉（`ckpt/state.go:345-350`）。

snapshot store 是 tmpfs，`fsync` 在上面没有落盘的含义。保留这两次调用，是让这段代码不依赖"底下一定是 tmpfs"这个部署细节：snapshot store 的位置可以通过参数改（[24](24-configuration-and-observability.md)）。

### 2.4 什么时候写

每次 create、restore、delete 结束时都写，**包括失败的路径**：

- create 和 restore 在通过 degraded 检查之后立即登记一个延迟执行的 `persist`（`ckpt/service.go:174`、`:472`），所以无论从哪个出口返回都会执行。
  失败路径也要写，是因为失败路径同样可能改了状态：增量父镜像记录被清空；沉下 upper 失败而名字又改不回去时，当前 upper 的记录被改成了层名；restore 序号已经递增。
- delete 显式调用（`ckpt/service.go:803`），并且根据写成功与否决定要不要删镜像目录（第 4 节）。
- 上一次写失败时，下一次 create 或 restore 开头会先重试（`checkReady`，`ckpt/service.go:679-683`）。

"只要可能改过状态就写"比"只在改了状态时写"多出一些无谓的写入，例如被预检拒绝的 create 也会把一份内容没变的状态文件重写一遍。
这是有意换来的简单：不需要在每个失败出口判断状态有没有变，也就不会漏掉某一个。状态文件很小，又写在 tmpfs 上，这点代价可以接受。

写失败时 gsd 进入"状态文件写失败"这一类 degraded（`persist`，`ckpt/state.go:281-287`），处置见 [12](12-failure-semantics.md)。

### 2.5 启动时加载与校验

`loadState`（`ckpt/state.go:341`）在 overlay 管理器初始化成功之后调用，分三种情况：

1. **文件不存在**：认为这是本次开机后 gsd 第一次启动（理由见第 3 节），按初始值运行。但要先确认开机时的 upper 目录确实还在，不在就进入 fatal。
   这道检查防的是"状态文件被外力删掉、而层栈其实已经切换过"的情形。
2. **文件读不了、解析不了、版本号不认识**：进入 fatal。
3. **文件正常**：先把 checkpoint 条目解析进一个临时的池，再校验 overlay 状态，两者都通过才一起生效；任何一步不通过都进入 fatal，**不做部分应用**。

校验 overlay 状态的规则在 `importState`（`ckpt/state.go:137`）：记录的当前 upper、当前 work，以及当前层栈里所有归 gsd 管的层目录（位于 `/overlay/layers/` 下的），
都必须在磁盘上存在并且是目录。基础层不查，它由开机脚本挂好，不归 gsd 管。层序号取"记录值"与"磁盘上已有层目录的最大序号"两者中较大的，保证之后不会分配到一个已经存在的名字。

**fatal 时不覆盖状态文件。** 加载失败的那份文件是排查"到底哪里对不上"的唯一材料，而此时内存里的状态是初始值，写出去等于用一份错的覆盖一份可能只是部分损坏的。
`persist` 在 fatal 时直接返回（`ckpt/state.go:264`）。

加载成功之后还要做两件收尾的事：增量父镜像记录里，所属 checkpoint 已经不在池里的项丢掉（那棵树下一次全量）；然后触发一次层目录和镜像目录的回收，
收掉重启之前没来得及删的东西。

现场验证过的两条路径（新 920B，2026-10-08，`259f1a25c`，`verify_fixes` 与 `verify_multitree`）：重启 gsd 之后日志出现 `persisted state loaded`，
list、create、restore 正常，并且重启后的第一次 create 仍是增量；把状态文件写坏后重启，`/health` 变为 degraded，create 被拒。判定值见 [20](20-results-and-gaps.md)。

### 2.6 版本 1

版本 1 是只支持一棵进程树时的格式：镜像直接放在 `<id>_mem/` 下而不是 `<id>_mem/t0/`，条目上没有 `trees`，全局只有一个"上一次的镜像目录"而没有按树的记录。
版本 1 的文件仍然能加载（`treesOf`，`ckpt/state.go:98`）：有进程镜像的条目按"一棵树、镜像直接在镜像目录下、根进程未知"处理，restore 照常可用。
旧格式的那条全局记录不带根进程的 pid 和 starttime，认不出是哪棵树，加载时丢掉，所以升级后的第一次 create 是全量。之后写出的文件就是版本 2。

这条兼容路径只有单测（`TestLoadLegacyStateFile`，`ckpt/multitree_test.go:666`）、没有现场走过：现场的模板都是用新 gsd 重建的，没有遇到过版本 1 的文件。它在 [15](15-known-limits.md) 里编号 T-06。

---

## 3. 生命周期：状态和谁同生共死

状态文件放在 snapshot store 里而不是根盘上，是一个刻意的选择。它依据的是下面这条对应关系：

> snapshot store 是 tmpfs，内核里的 overlay 层栈是内核内存里的结构。两者都只在 guest 这一次开机期间存在，guest 重启时一起归零。

由此可以逐一推出各种"重启"之后的行为。

**gsd 进程重启**（崩溃后被 systemd 拉起，或建模板时的主动重启）。内核没有重启，层栈还是原来的；tmpfs 没有清，状态文件和镜像都在。
gsd 加载状态文件，回到重启前的状态，已有的 checkpoint 全部可用。这是 `state.json` 要解决的主要场景。
需要注意的只有一点：如果重启前刚发生过"状态文件写失败"，加载到的会是上一次写成功时的旧状态，它可能与内核层栈对不上，
这时校验多半会因为目录不存在而进入 fatal。这正是"状态文件写失败"要被当成 degraded 并阻止后续操作的原因。

**guest 重启。** 内核层栈回到开机脚本挂的初始状态，tmpfs 被清空，状态文件和镜像一起消失。gsd 启动时看到文件不存在，按初始值运行，两边恰好一致。
此前的 checkpoint 全部丢失；根盘上 `/overlay/layers/` 里留下的层目录不再有人引用。假如状态文件放在根盘上，guest 重启后 gsd 会加载到一份指向"上一次开机时的层栈"的记录，
而内核里已经是初始层栈，两边必然对不上。把它放在 tmpfs 上，这种不一致就不可能出现，"文件不存在"也就可以无歧义地解释成"刚开机"。
不过正常使用中沙箱不会经历 guest 重启：沙箱是从模板快照恢复出来的，不重新开机（[05](05-guest-boot-and-injection.md)）。

**从模板快照创建沙箱。** 模板快照是在 gsd 已经启动之后拍的，gsd 进程连同它的内存都在快照里。每个新沙箱里的 gsd 都从同一份内存状态继续运行，
这份状态就是建模板结束时的状态：没有 checkpoint，层栈是初始值。gsd 在这个过程中没有"启动"这个动作，不加载任何东西。
正因为一份快照会恢复出多个沙箱，gsd 才不能在内存里缓存任何与具体沙箱有关的东西，例如 access token 的哈希（[04](04-architecture.md)）。

**沙箱 pause/resume。** 暂停时整个虚拟机被冻结并保存，恢复时原样还原。按 `ckpt/state.go` 文件头的说明，这种情况下 gsd 进程连同内存一起被冻结再恢复，不会重启；
内存里的状态、tmpfs 里的文件、内核里的层栈都原样还在，不需要重新加载。从机制上看这个推断是成立的，三样东西都是 guest 内存的一部分，被同一份快照保存。
**但这一条目前只有代码注释和上述推理为据，没有专门的现场结果**：没有任何一次验证做过"建 checkpoint、pause、resume、再 restore"。
它列在 [20](20-results-and-gaps.md) 的缺口表里，使用时请按"未验证"对待。

---

## 4. 增量依赖与删除

### 4.1 依赖关系是什么样的

增量 dump 出来的镜像不是自足的：其中标记为"在父镜像里"的页，restore 时要到父镜像目录（以及父镜像的父镜像，一路往上）去读。
CRIU 在子镜像目录里留一个名为 `parent` 的符号链接指向父目录。所以**父镜像必须活得比所有子镜像久**，这是 [12](12-failure-semantics.md) 不变量清单里的第 5 条。

多棵进程树时，每棵树各有自己的一条增量链：这次 dump 的第 i 棵树以"上一次同一棵树的镜像"为父，新出现的树没有父。
于是一个 checkpoint 依赖的父 checkpoint 是各棵树父镜像所在 checkpoint 的**并集**（`treeParents`，`ckpt/pool.go:79`）。
按现在的流程，增量的父镜像总是来自"上一个成功的 create，或 restore 到的那个 checkpoint"，所以这个并集实际上至多只有一个成员；
但池的依赖模型是按一般情况（多个父节点）实现的，不依赖这一点（`ckpt/pool.go:37-43`）。对外的 `parent_checkpoint_id` 是单值字段，有多个时报告 id 最小的那个。

restore 之后再 create，新 checkpoint 的父是被恢复的那个，而不是时间上最近的那个。所以 checkpoint 之间的依赖关系是一棵树，不是一条链。

### 4.2 删除一个有子节点的 checkpoint

修改前 delete 直接删掉镜像目录。如果它是别人的增量父镜像，子 checkpoint 就在不知不觉中坏掉了，要到 restore 时、杀了进程之后才报错。

现在池给每个条目维护一个子节点计数（`children`，`ckpt/pool.go:47-49`）。`remove`（`ckpt/pool.go:151`）的规则是：

- 条目没有子节点：从池里摘掉，交给调用方物理删除。然后沿依赖关系向上，给它的每个父节点的计数减一；
  父节点如果此前已经被标记删除、且这是它最后一个子节点，就一并摘掉，如此递归。
- 条目还有子节点：只打上"已删除"标记，返回空。镜像目录和它引用的层目录都原样保留。

被标记删除的条目对外等同于不存在：`list` 不返回它，`restore` 和再次 `delete` 都得到 `not_found`（`get` 只返回未标记的条目，`ckpt/pool.go:129`）。
但它的镜像仍占着 snapshot store 的空间，直到最后一个依赖它的子 checkpoint 被删掉。**所以删除一个 checkpoint 并不保证立刻腾出空间。**
这一点对使用者的影响（"历史是一棵树，空间要等子节点删完"）在 [22](22-semantics-and-limits.md)。

现场验证的序列（新 920B，`verify_fixes`）：建 A，再建 B（B 以 A 为父）；删 A 之后 list 只剩 B、A 的镜像目录还在，restore A 得到 `not_found`，restore B 成功；再删 B，A 和 B 的目录一起消失。

### 4.3 删除对增量父镜像记录的影响

如果被删的 checkpoint 正好是下一次增量 dump 的父（即 `lastTrees` 指向它），`DeleteCheckpoint` 会把这些记录清掉，那几棵树下一次做全量（`ckpt/service.go:775-789`）。
即使它因为还有子节点只是被隐藏、镜像还在，也清掉。理由是：继续以它为父，新的 checkpoint 又会挂到它下面，它就得一直留到这条新链也删光；
而用户删它，本意是希望它尽早消失。代价是下一次 dump 多做一次全量。

### 4.4 先写状态，再删目录

物理删除分两步，顺序是固定的（`ckpt/service.go:800-808`）：先把"条目已经不在"写进状态文件，写成功之后才回收镜像目录。

- 两步之间崩溃：磁盘上多出几个没有条目引用的目录，重启后由回收逻辑收掉。只是泄漏，不是损坏。
- 反过来（先删后写）两步之间崩溃：状态文件里留着指向已删目录的条目，重启后它们被加载回来，看起来是可用的 checkpoint，一 restore 就失败。
- 状态文件写失败：不删。磁盘上的状态文件仍然引用着这些目录，gsd 此时若重启会把条目加载回来，目录必须还在。留给下一次 delete 或 gsd 重启时的回收处理。

---

## 5. 回收

gsd 管理的目录有三类会变成垃圾，回收规则各不相同，但动作的形态是统一的。

### 5.1 三类垃圾

**临时的 upper 与 work 目录。** 每次 create 新建一对 `upper_<id>_next`、`work_<id>_next`；每次 restore 新建一对 `restore_<id>_<序号>_upper`、`_work`
（序号每次递增，为的是避开内核对"正在使用的目录"的检查，见 [06](06-overlay-hot-switch.md)）。checkpoint 记录引用的只是层目录，从不引用这些临时目录，
所以它们当中除了当前正在用的那一对，其余全是垃圾：work 目录一旦切走就没有用处；被 restore 切走的 upper 里装的正是"被丢弃的时间线"上的写入，可能很大。
此外还要保留已经由 `PrepareRestore` 建好、尚未提交也未放弃的那一对（`pending`）。

**层目录。** 名为 `L<序号>` 的目录是 checkpoint 的正式组成部分，按引用回收：既不在当前层栈里，也不被池里任何条目引用的，才是垃圾。
"池里任何条目"包括已标记删除但保留着的条目（`layerRefs`，`ckpt/pool.go:197`），因为它们的子 checkpoint 的层栈里包含同样的层。
修改前从不回收层目录，删掉 checkpoint 之后层一直留在根盘上。

**无主的镜像目录。** snapshot store 里名字形如 `ckpt_*_mem`、又不被池里任何条目引用的目录（`reclaimSnapshotStore`，`ckpt/snapshot_store.go:25`）。
来源有三种：delete 的两步之间崩溃；delete 时状态文件写失败而暂时留下；create 在第一次落盘之前崩溃。这个函数只碰这种名字的目录，状态文件、临时文件和别的东西一概不动。

层目录的回收有一处与内核有关的细节。目录被删时，内核可能还持有对它的引用（被换下的层要等到下一次切换才释放，仍被打开的文件也会钉住 inode）。
删除只是去掉目录项，已经挂上的层照样可用，磁盘块要等最后一个引用释放后才归还。修复前内核从不释放这些引用，表现为"层目录删了但根盘不回落"；
修复后的行为和残留的上界在 [07](07-overlay-concurrency-and-fixes.md) 的"回收：被换下的层、mount 与缓存引用"一节。

### 5.2 锁内改名，锁外删除

三类回收用同一种两段式做法（以层目录为例，`reclaimOrphans`，`ckpt/overlay.go:372`）：

1. **持锁**：列出目录、算出哪些是垃圾、把它们 `rename` 进同一文件系统下的 `.trash` 目录。改名是原子的，也很快。
2. **放锁之后**：在后台 goroutine 里把 `.trash` 清空。删除一个装着大文件的目录耗时不可控，这段时间不占锁，也不计入任何请求的耗时。

为什么不直接在锁外"先算出垃圾名单，再逐个删"：那样在算名单和删除之间有一个窗口，后来的 create 或 restore 可能刚好新建了一个目录，
而它不在"要保留"的集合里（集合是之前算的），就会被当成垃圾删掉。修改前的回收实现就有这个竞态。
把"判定"和"移走"放进同一把锁之后，新建目录的操作要么在判定之前完成（于是被看到、被保留），要么在移走之后才开始（于是不受影响），不存在中间情况。
单测 `TestReclaimDoesNotRemoveDirCreatedAfterSnapshot`（`ckpt/overlay_reclaim_test.go:66`）专门固定这一点。

为什么不干脆在锁内删完：根盘不大（代码注释里按 767 MB 计），回收必须及时；但删除的耗时不可控，放在锁内会直接加到下一次切换的等待时间上。

`.trash` 以点开头，既不匹配层目录的命名也不匹配临时目录的命名，所以不会被回收逻辑自己再处理一遍。上一次没删完就崩溃留下的内容，下一次清空时一并删掉。

### 5.3 什么时候触发

- 层目录与临时目录：每次成功的沉下 upper、每次成功的 restore 切换、每次 delete 之后、以及启动加载完状态之后，各触发一次后台回收。
- 镜像目录：delete 成功写完状态文件之后，以及启动时。fatal 状态下不回收。

---

## 6. 并发

### 6.1 一把大锁，排队不拒绝

修改前没有任何并发控制。Connect 的服务端为每个请求开一个 goroutine，两个同时到达的请求会并发读写增量父镜像记录；
一个 restore 可能在另一个 create 的 dump 中途把进程杀掉；一个 delete 可能在 restore 读镜像的时候把它删掉。

现在 `Service` 上有一把互斥锁 `opMu`（`ckpt/service.go:46-50`），create、restore、delete 三个 RPC 从进入到返回全程持有。同一时刻只有一个在执行，其余的在锁上等待。

选择"排队"而不是"忙时拒绝"，是从调用方的角度考虑的。这三个操作在语义上本来就必须串行，并发调用它们的客户端只有两种：一种是无意的（例如两个协程各自觉得该打一个 checkpoint），
对它来说排队之后两次都成功是最不意外的结果；另一种是有意压测的。无论哪种，返回"忙"都只是把重试的负担推给调用方，而它重试的结果与排队完全一样。
排队的代价是后到的请求耗时里包含等待时间，这一点从响应的分段计时里看不出来（计时从拿到锁之后开始），需要时看 SDK 侧的端到端时间。

现场验证过四个并发的 create 全部成功、id 各不相同（新 920B，`verify_fixes`）；单测 `TestConcurrentCreateRestoreSerialized`（`ckpt/service_failure_test.go:215`）验证 create 与 restore 交错时互不穿插。

### 6.2 不持这把锁的路径

- **list** 不持 `opMu`。它只读池，而池有自己的锁（`pool.mu`）。所以 list 不会被一个正在进行的长 dump 挡住，看到的是那次操作开始之前的列表。
- **`/health`** 读 degraded 原因，由另一把小锁 `healthMu` 保护，同样不等 `opMu`。
- **后台回收**在 goroutine 里运行，与 RPC 之间靠 overlay 管理器自己的锁 `om.mu` 互斥（第 5.2 节）。清空两个 `.trash` 目录各有一把专用的锁，避免两次回收同时删同一批文件。

锁的获取顺序固定为 `opMu` → `om.mu` → `pool.mu`。回收时 overlay 管理器在持有自己的锁的情况下回调池取层引用（`ckpt/overlay.go:68-72`），所以池的代码不能反过来调用 overlay 管理器。

### 6.3 criu 不随请求取消

criu 的超时上下文是从请求上下文"脱钩"之后另建的（`ckpt/service.go:237`、`:566`），客户端断开不会终止 criu。
每棵树有自己的时限（默认 60 s），一次操作里全部树合计另有上限（单棵时限的 3 倍，`criuTotalTimeoutFactor`，`ckpt/service.go:33`）。
合计上限防的是树很多、每棵都差一点超时的情形：先 dump 完的树在后面的树 dump 期间一直停着，不能让停顿累加到"树数 × 单棵时限"。
时限到了之后先发 SIGTERM 让 criu 走自己的清理路径，2 秒后还没退出才 SIGKILL（`runCriuCmd`，`ckpt/criu.go:28`）。
这对失败语义的影响（客户端超时不等于操作没发生）在 [12](12-failure-semantics.md)。

---

## 7. 容量预检

### 7.1 要防的是什么

CRIU 镜像写在 snapshot store 里，而它是 tmpfs，占的是 guest 的内存。镜像写不下时有两种结局：

- tmpfs 先满（它有 2 GiB 的大小上限）：dump 中途失败。这还算干净，只是白停了一次进程。
- guest 的可用内存先耗尽：触发 OOM。麻烦在于 gsd 的服务单元设了 `OOMScoreAdjust=-1000` 以保证 gsd 自己不被杀，而这个值会被它派生的 criu 继承。
  于是 OOM 发生时，gsd 和 criu 都不会被选中，被杀的是**用户进程**，也就是这次 checkpoint 想要保存的对象。

修改前不做任何估算。现在 create 在 dump 之前先估镜像大小，放不下就拒绝（`resource_exhausted`），此时什么都还没动。

### 7.2 怎么估

估算值 = 待 dump 的全部进程（多棵树时合在一起）的 `RssAnon + RssShmem` 之和 × 1.2（`estimateDumpIn`，`ckpt/capacity.go:72`）。
然后与两个数比较：snapshot store 的剩余空间（`statfs`），以及 `/proc/meminfo` 的 `MemAvailable`。任一个小于估算值就拒绝，错误信息里把这几个数都列出来。

用 `RssAnon + RssShmem` 而不是整个 RSS，是因为 CRIU 写进镜像的是匿名私有页和共享内存页；文件映射里没改过的页（可执行文件、共享库）不写。
按整个 RSS 估，对 python、node 这类映射了大量库的进程会多估几十到上百 MiB，造成不必要的拒绝。这两个字段取自 create 开头那一次带 `status` 的 `/proc` 扫描，不为预检另读一遍。

这个估算有意偏保守，偏的方向有三处（`ckpt/capacity.go` 文件头）：

- **增量 dump 也按全量估。** 增量只写脏页，实际可能只有全量的百分之几，但事先无法知道脏了多少。
  后果是：一个本来放得下的增量 checkpoint，在剩余空间小于"进程的全部匿名内存 × 1.2"时会被拒绝。这是当前实现最主要的保守之处，使用上的影响见 [22](22-semantics-and-limits.md)。
- **共享的页被重复计算。** fork 出来的父子进程以写时复制方式共享的匿名页、多个进程映射的同一段共享内存，在每个进程的 RSS 里各算一次。
- **系数 1.2** 覆盖 CRIU 的其他镜像文件（页表映射、文件描述符等的描述）和详细日志。

预检只在有用户进程时做；没有用户进程的 checkpoint 只切文件系统，不写镜像。

### 7.3 记录实际占用

dump 成功后，gsd 量出每棵树镜像目录的实际占用（按分配的块数算，不跟随 `parent` 符号链接，所以父镜像的空间记在父 checkpoint 名下），
存进条目和状态文件，并通过 create 的响应和 list 返回为 `image_bytes`（`dirUsage`，`ckpt/capacity.go:154`）。量不出来只打告警，不影响这次 checkpoint。
调用方可以据此知道每个 checkpoint 实际占了多少 guest 内存，决定删哪些。

现场的两个例子（均为新 920B，口径各自独立，判定值见 [20](20-results-and-gaps.md)）：

- 一个持有约 480 MiB 匿名内存的进程，在已有两个 checkpoint 之后第三次 create 被拒，错误信息为
  `estimated images 580.4 MiB ...; MemAvailable 453.4 MiB`，没有发生 OOM；`image_bytes` 与 `du` 的结果相差不到 0.01%（2026-09-30，第一版修复，`verify_fixes`）。
- 300 轮的压测里每组有五十多次 create 以 `resource_exhausted` 结束，原因都是 snapshot store 将满。压测判据把它算作预期内的保护，而不是失败（2026-10-08，`259f1a25c`）。

---

## 8. 层栈深度预检

切换层栈的 ioctl 把整个层栈作为一个选项串传给内核，形如 `lowerdir=<层>:<层>:…,upperdir=…,workdir=…,ovl_kind=…`。内核对这个串的长度有上限：一页，即 4096 字节。
超过时内核返回 `EINVAL`，从这个错误码看不出原因。

gsd 在调用 ioctl 之前自己算一遍长度（`checkOverlayOptionsLen`，`ckpt/overlay.go:540`），超过 4096 就返回 `ErrLayerStackTooDeep`，
最终成为 `resource_exhausted` 和一句说得清的错误信息。上限写死为 4096 而不是取运行时的页大小：guest 内核是 4 KiB 页，页更大的内核只会更宽松（`ckpt/overlay.go:27-31`）。

预检在 create 时做两次（`SinkUpper`，`ckpt/overlay.go:150-158`）：

1. 这一次沉下 upper 的选项串放得下；
2. 这个 checkpoint 将来被 restore 时的选项串也放得下。restore 的串更长：create 时内核会自动把刚沉下去的那层接在最上面，gsd 不必把它写进串里；
   restore 时没有这个自动行为，要把完整层栈写全，upper 和 work 的目录名也更长。所以 gsd 用"最坏情况的 restore 目录名"（序号取最大的整数）再算一遍。
   第二次不通过时错误信息以 `checkpoint would not be restorable` 开头。

第二次检查的意义在于：没有它，就可能建出一个能 create、不能 restore 的 checkpoint，而这要等到用户真的需要回滚时才会发现。

要注意这两次预检在一次 create 里的位置：`SinkUpper` 排在 `criu dump` 之后（`ckpt/service.go:322`）。所以因层栈太深被拒时，文件系统和 checkpoint 列表都没有变，
但 dump 已经做过并被丢弃，增量的起点也随之丢了，下一次 create 对每棵树都是全量（[12](12-failure-semantics.md)）。

上限具体是多少层，取决于每层的路径长度。修改前层目录名里带着 19 位的纳秒时间戳，每层在选项串里占 47 字节，同一条链上叠到八十多层就放不下。
现在层名缩短为 `L<序号>`（`layerName`，`ckpt/overlay.go:464`），在默认路径下每层约 20 字节。
现场实测（新 920B，2026-09-30，第一版修复，`verify_fixes`）：在同一条链上连续 create，前 190 次成功，第 191 次被拒，错误信息为
`layer stack too deep: 192 lower layers, ioctl options 4105 bytes > kernel limit 4096`；之后 restore 到最后一个和第一个 checkpoint 都正确，回到浅的层栈之后 create 恢复正常。

注意这个上限说的是**一条链的深度**，不是 checkpoint 的总数。restore 到较早的 checkpoint 再继续，新的层接在那个较浅的层栈上；
upper 为空时 create 也不会新增一层（[06](06-overlay-hot-switch.md)）。没有把内核的一页上限改大，是因为一条链上叠近两百层已经远超合理用法，而每多一层，overlay 的路径查找就多一层开销。
这条限制同时列在 [15](15-known-limits.md)。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 状态文件的格式 | `ckpt/state.go` — `persistedState`（`:39`）、`overlayState`（`:54`）、`persistedCheckpoint`（`:70`）、`persistedTree`（`:88`） |
| 写与加载 | `ckpt/state.go` — `persist`（`:256`）、`writeStateFile`（`:296`）、`loadState`（`:341`）、`importState`（`:137`）、`treesOf`（`:98`）；文件头注释是第 2、3 节的出处 |
| 池与依赖 | `ckpt/pool.go` — `checkpointEntry`（`:26`）、`linkParents`（`:114`）、`remove`（`:151`）、`get`（`:129`）、`holds`（`:140`）、`layerRefs`（`:197`）、`memPaths`（`:210`） |
| 删除 | `ckpt/service.go` — `DeleteCheckpoint`（`:764`） |
| 层与临时目录的回收 | `ckpt/overlay.go` — `reclaimOrphans`（`:372`）、`moveOrphansToTrash`（`:383`）、`isTransientLayerDir`（`:451`）、`isLayerDir`（`:487`） |
| 镜像目录的回收 | `ckpt/snapshot_store.go` — `reclaimSnapshotStore`（`:25`）、`emptyTrashDir`（`:85`） |
| 并发 | `ckpt/service.go` — `opMu`（`:50`）、`healthMu`（`:79`）、`criuTotalTimeoutFactor`（`:33`） |
| 容量预检 | `ckpt/capacity.go` — 文件头注释、`estimateDumpIn`（`:72`）、`dirUsage`（`:154`）；调用处 `ckpt/service.go:207-219` |
| 层栈深度预检 | `ckpt/overlay.go` — `maxOverlayOptionsLen`（`:31`）、`checkOverlayOptionsLen`（`:540`）、`overlayOptions`（`:533`）、`layerName`（`:464`） |
| 单测 | `ckpt/state_test.go`、`ckpt/pool_test.go`、`ckpt/capacity_test.go`、`ckpt/overlay_reclaim_test.go`、`ckpt/snapshot_store_test.go` |
| 服务单元与 snapshot store 的挂载 | `packages/orchestrator/internal/template/build/core/rootfs/gsdfiles/gsd.service.tpl`（`Restart=always`、`OOMScoreAdjust=-1000`）、`overlay-init.sh.tpl`（2 GiB tmpfs） |

---

## 本章要点

1. gsd 的状态是四块：checkpoint 池、每棵树的增量父镜像记录、overlay 管理器的层栈记录、degraded 原因。其中层栈记录必须与内核一致，而且没有别处可以查回来。
2. `state.json`（版本 2）在每次 create、restore、delete 结束时原子写入，失败路径也写；启动时加载并校验记录的目录是否存在，任何不一致都进入 fatal、不做部分应用、不覆盖原文件。
3. 状态文件放在 tmpfs 上，与内核层栈同生共死：gsd 重启后状态完整恢复；guest 重启后两者一起归零，"文件不存在"即"刚开机"。pause/resume 之后的行为只有代码注释为据，没有专门的现场结果。
4. checkpoint 之间的增量依赖是一棵树。删除有子节点的 checkpoint 只是隐藏，最后一个子节点删掉时才递归物理删除；删除的顺序是先写状态、再删目录。
5. 临时目录切走即垃圾，层目录按引用回收，镜像目录按是否有条目引用回收；三者都是"锁内改名进 `.trash`、锁外删除"，以消除判定与新建之间的竞态。
6. create、restore、delete 由一把锁串行化，后到的排队而不是被拒；list 和 `/health` 不等这把锁；criu 的运行与请求的取消脱钩。
7. 容量预检按 `RssAnon + RssShmem` × 1.2 估镜像大小，与 snapshot store 剩余和 `MemAvailable` 比较，目的是避免 OOM 杀掉用户进程；它对增量 dump 也按全量估，偏保守。
8. 层栈深度受内核一页选项串的限制，gsd 在 create 时就同时保证"这次放得下"和"将来 restore 也放得下"；短层名下一条链的实测上限约 190 层。
