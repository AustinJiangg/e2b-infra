# 附录 B · 继续开发

> 给要接手或参与这个项目的工程师看。读完能知道：代码在哪、按什么顺序读、改不同的东西各要注意什么、
> 哪些不变量碰不得、改完怎么验证、已知的未闭合项建议按什么顺序处理。
> 建议先读 [11](11-end-to-end.md)、[12](12-failure-semantics.md)、[13](13-state-concurrency-capacity.md)，动内核之前读完 [07](07-overlay-concurrency-and-fixes.md)；
> 查符号用[附录 A](A-glossary-and-code-map.md)。

---

## 1. 代码在哪

### 1.1 仓库与组件

代码在 `KASandbox_0904` 仓库的 `deltabox-gsd` 分支，本书以 `259f1a25c` 为准。四个组件同仓：

| 组件 | 目录 | 单元测试 | 怎么跑 |
|---|---|---|---|
| gsd（Go，guest 内守护进程） | `packages/gsd/`：编排在 `internal/services/checkpoint/`（本书简写 `ckpt/`），鉴权在 `internal/permissions/` | 同目录 `*_test.go`；`ckpt/fake_test.go` 提供假的 `/proc` 和假的 CRIU | `cd packages/gsd && go vet ./... && go test -race -count=1 ./...` |
| orchestrator 的注入（Go） | `packages/orchestrator/internal/cfg/`、`internal/template/build/`（`core/rootfs/`、`layer/`、`phases/base/`） | 同目录 `*_test.go` | `cd packages/orchestrator && go test -count=1 ./internal/template/... ./internal/cfg/...` |
| Python SDK | `py-sdk/`：`e2b/sandbox_sync/checkpoint.py`、`e2b/sandbox_async/checkpoint.py`、`e2b/gsd/` | `py-sdk/tests/test_gsd_*.py` | `cd py-sdk && pytest tests/test_gsd_checkpoint.py tests/test_gsd_errors.py tests/test_gsd_headers.py` |
| guest 内核补丁 | `fc-kernels/patches/6.1.158/`（三个补丁）、`fc-kernels/build.sh`、`kernel_base_tags.txt` | 无单测，靠调试内核压测（§4.3） | [23](23-deployment.md) |

离线构建时 Go 的环境变量是 `GOTOOLCHAIN=local GOWORK=off GOPROXY=off GOFLAGS=-mod=readonly`；
gsd 以 `CGO_ENABLED=0` 静态编译，orchestrator 需要 `CGO_ENABLED=1`；两者都用 `-ldflags "-X main.commitSHA=<提交号>"` 把提交号编进去。完整命令在 [23](23-deployment.md)。

手册与测试脚本在 `e2b-infra` 仓库的 `gsd/` 下：`docs/` 是本书源文件，`scripts/` 是验收、压测与探针脚本，`scripts/kernel/` 是调试内核的配置与说明。

### 1.2 阅读源码的顺序

```
① ckpt/service.go            四个 RPC 的编排，一眼看到全貌（先读 CreateCheckpoint 的头注释）
② ckpt/pid.go、ckpt/shared.go  用户进程树怎么找、多树的预检（shared.go 的文件头是一段完整的论证）
③ ckpt/overlay.go            gsd 侧怎么用热切层 ioctl：下沉、restore 的两段式、回收
④ ckpt/criu.go               CRIU 命令怎么拼、怎么限时、restore 前怎么杀进程
⑤ ckpt/pool.go、ckpt/state.go  checkpoint 条目、增量依赖、持久化
⑥ ckpt/capacity.go           容量预检（文件头注释说明估算口径）
⑦ overlay-init.sh.tpl        guest 的根文件系统是怎么搭起来的
⑧ 补丁 0003 的提交说明        内核侧修了什么，按主题列；然后对着 07 章读 super.c 里的 ovl_do_ioctl_checkpoint
```

前五个读完，用户态的机制就通了。第 ⑧ 步只在要动内核时需要，而且**不要跳过 [07](07-overlay-concurrency-and-fixes.md) 直接读补丁**：补丁是对原始补丁的增量，单看 diff 很难看出每一处的理由。

---

## 2. 按任务的改动指引

### 2.1 改 gsd 的编排逻辑

- 所有对外部世界的访问都经 `sysOps`（`ckpt/sysops.go`）：`/proc` 的位置、发信号、等进程退出、执行 CRIU、查 systemd。新加的外部依赖也放进去，否则单测注入不了。
- 新的失败路径要回答三个问题：此刻进程是否已经被杀、文件系统处于旧状态还是目标状态、下一次 dump 是否必须全量。对照 [12](12-failure-semantics.md) 的错误表选错误码，不要新造"返回成功但少做了一步"的降级。
- 改了任何会影响持久化状态的字段，同步改 `state.go` 的导出与加载，并考虑旧版本状态文件的加载（现在版本 1 仍可加载）。
- 状态文件要在可能改过状态的每条路径上写，包括失败路径。

### 2.2 改线协议

`packages/gsd/spec/checkpoint/checkpoint.proto` 与仓库根的 `spec/gsd/checkpoint/checkpoint.proto` 必须逐字节相同，**只增字段、不改已有编号**。
Go 侧和 Python 侧的生成代码都要重新生成；SDK 的 `CheckpointInfo` 和计时字典跟着加。计时字典只放以 `_ms` 结尾的耗时，个数类的字段单独放（已有按键遍历耗时的调用方）。

### 2.3 改 guest 的启动或注入

- `overlay-init.sh.tpl` 在 PID 1 之前运行，出错的后果是 guest 起不来；改动后至少要重建模板并跑一遍 [26](26-acceptance-runbook.md) 的部署自检。
- **模板固化了建模板时的内核、gsd、overlay-init。** 改了这三样中的任何一样，已有模板不受影响，必须重建模板才能测到；做性能对照时这一点曾导致一次结论作废（[17](17-performance-after-fixes.md)）。
- 受 `GSDEnabled()` 控制的五个点（[附录 A](A-glossary-and-code-map.md) 的代码地图）要保持"关掉开关后与上游的建模板路径完全一致"，新增注入逻辑时放在开关之内。
- base 层缓存键不含 gsd 或内核的版本。换了 gsd 或内核后重建模板不要用缓存。

### 2.4 改内核补丁

- 在内核源码树的开发分支上按主题一个提交一个提交地做，验证通过后再压成一个补丁替换仓库里的 0003，并更新它文件头的提交说明。0002 保持不动。
- 替换后做可重放性核对：从 `kernel_base_tags.txt` 里的基线 tag 依次 `git apply` 三个补丁，`git write-tree` 的结果必须与开发分支末端的树相同（[23](23-deployment.md)）。
- 发布版和调试版两种配置都要编过，不引入新的告警。
- 必须满足 §3.2 的四条内核不变量，并通过 §4.3 的调试内核压测。

### 2.5 加一种新的缓存或加一个 overlay 入口（内核）

这是最容易重新引入问题的两类改动，单独列出来：

- **加一个 overlay 的操作入口**（新的 inode 操作、文件操作、ioctl）：它只要读后端状态，就要持 `switch_sem` 读锁，并遵守读锁的五条规则（不递归、锁序、不在 `mmap_lock` 下等、不跨无界等待、RCU 路径不拿锁）。
  做法照现有入口：函数体改成"调用方持读锁"的内层函数，外面包一层加锁解锁；内层如果要经 VFS 绕回 overlay，先放锁。
- **在 `struct ovl_fs`、overlay inode 或 dentry 上加一个缓存字段**：先回答它指向什么。指向 work 目录或某一层里的对象，就必须在 ioctl 的提交点上随之更新或丢弃；
  以某个真实 inode 的地址为键，就必须说清楚是谁钉住了那个 inode、切换之后这个"谁"还在不在。[07](07-overlay-concurrency-and-fixes.md) "属于 work 目录的缓存"一节的字段审查表是现成的模板，加字段时往表里添一行。

---

## 3. 不能破坏的不变量

### 3.1 gsd 侧

论证在 [12](12-failure-semantics.md)，这里是改动时的检查表。前七行对应 12 章"不变量清单"的第 1、4、5、9、6、8、2 条，最后一行是回收的不变量，论证在 [13](13-state-concurrency-capacity.md)。
12 章清单里另外五条——dump 前就停着的进程不替用户放开（第 3 条）、增量的父镜像必须属于池里的 checkpoint（第 7 条）、每条可能改过状态的路径结束时都落盘（第 10 条）、criu 不随请求取消（第 11 条）、半途失败的 restore 之后不接受 create（第 12 条）——同样不能破坏，改到相关代码时回 12 章逐条核对：

| 不变量 | 破坏的后果 | 现在由谁保证 |
|---|---|---|
| 一棵树从 dump 完到 `clear_refs` 写完必须一直停着 | 这段窗口里写的页会从下一次增量里漏掉，恢复出错误的内存 | `--leave-stopped`；全部树 dump 完才下沉、写 `clear_refs`、一起放开 |
| restore 换层之前，当前用户进程必须杀干净 | 残留进程占着 PID 使 CRIU 恢复失败，并带着"未来"的状态写文件系统 | `killTrees`：先冻住、反复扫描到没有新进程、整体杀、限时等退出 |
| 父镜像必须活得比子镜像久 | 子 checkpoint 恢复不了 | `pool` 的子节点计数；有子节点的只隐藏 |
| 删除时先写状态文件、再删目录 | 中途崩溃后状态文件指向不存在的目录 | `DeleteCheckpoint` 的顺序；无主目录由回收逻辑清理 |
| 任何一步失败之后下一次 dump 必须全量 | 以失败的或更早的镜像为父，漏页 | `discardFailedCheckpoint` 清空 `lastTrees` |
| 杀进程之前的失败"什么都没动"，之后的失败一律 `DataLoss` 并进入 degraded（例外：没有任何进程被杀而切层失败时仍是"什么都没动"） | 调用方无法判断沙箱状态 | restore 的两段式；`restoreFailed` |
| gsd 不在 dump 之前自己发 SIGSTOP | CRIU 会把它们记成"原本就停着"，恢复出来也是停着的，与用户自己停住的进程分不开 | 逐棵 `--leave-stopped`，dump 前只记录状态 |
| 判定和移走垃圾目录在同一把锁里 | 回收逻辑删掉下一次操作刚建的 upper | `moveOrphansToTrash` 在锁内改名进回收站，锁外删除 |

### 3.2 内核侧

论证在 [07](07-overlay-concurrency-and-fixes.md)：

| 不变量 | 破坏的后果 | 怎么检查 |
|---|---|---|
| ioctl 里可能失败的步骤都在提交点之前；提交点之后只做不会失败的操作 | 晚失败时释放正在使用的对象 | 读 `ovl_do_ioctl_checkpoint`：`ovl_ckpt_build_plan` 之后不应再有会返回错误的调用；提交点之后的小分配用 `__GFP_NOFAIL`。现场用 ENOSPC 七档压测 |
| 读后端状态必须持 `switch_sem` 读锁 | 读到改了一半的状态或已释放的指针 | 新入口逐个对照 §2.5；调试内核上跑带 root worker 的压测，KASAN 与 lockdep 会报 |
| 被换下的对象延迟一个切换周期释放（`ovl_defer_put`），涉及 RCU 路径的再等一个宽限期 | 借用指针的读者释放后使用 | 提交点之后不应出现对"别人可能借着的对象"的直接 `dput`、`iput`、`kfree` |
| 属于 work 目录或某一层的缓存随切换更新；以真实 inode 地址为键的缓存要有东西钉住那个 inode | 悬空键被新分配的 inode 撞上，表现为随机的 `ELOOP`、`ENXIO`、`ENOTDIR` 或内核 `BUG` | 加字段时过一遍字段审查表；`probe_whiteout_bug.py` 与删文件密集压测 |

---

## 4. 改动后怎么验证

### 4.1 三层

| 层 | 跑什么 | 什么时候必须跑 |
|---|---|---|
| 单测 | §1.1 表里的三条命令 | 每次改动 |
| 现场功能验证 | `verify_gsd`、`verify_fixes`、`verify_multitree`、`verify_clear_refs_race`（顺序与判据见 [26](26-acceptance-runbook.md)） | 改了 gsd、注入、overlay-init 或内核之后；要先重建模板 |
| 现场压测与探针 | 四个压测脚本加各个探针（[19](19-testing-and-debug-kernel.md)） | 改了内核，或改了 gsd 里与切换、回收、杀进程有关的逻辑 |

### 4.2 按改动选最小集合

| 改了什么 | 至少要过 |
|---|---|
| 进程发现、多树预检、杀进程 | 单测；`verify_fixes` 的进程类各项；`verify_multitree`；`stress_multitree.py` |
| dump 与增量链 | 单测；`verify_clear_refs_race`（漏页必须为 0）；`verify_multitree` 里增量相关的项 |
| overlay 管理器、回收 | 单测；`verify_fixes` 的层与回收各项；`stress_overlay_ckpt.py` 的无 worker 组和 ENOSPC 组；`probe_leak.py` |
| 状态持久化 | 单测；`verify_fixes` 里重启 gsd 与写坏状态文件的项 |
| 鉴权、线协议、SDK | 三组单测；`verify_fixes` 的鉴权与返回信息两项；`demo_checkpoint_perf.py`（它会核对 SDK 返回的计时与 gsd 日志逐项相同） |
| overlay-init、注入 | orchestrator 单测；重建模板；`verify_multitree` 里检查根文件系统形态与 `/tmp` 的几项 |
| 性能相关 | `verify_gsd`；`verify_multitree` 的逐轮交错对照；对照的做法见 §4.4 |

### 4.3 内核改动：必须过调试内核压测

内核侧的问题几乎都是并发和生命周期问题，功能验证测不出来。任何内核改动都要：

1. 用发布版和调试版两种配置各编一个内核，**各建一个模板**（调试内核的 guest 内存给 4096 MB）。调试版配置在发布版基础上打开 KASAN、lockdep、链表与对象调试，并且**必须关掉 `VMGENID`**，否则 lockdep 会在沙箱从快照恢复时自己关掉（[19](19-testing-and-debug-kernel.md)）。
2. 调试内核上跑：8 组并发压测（每组 300 轮、6 个 root worker）、多树压测、删文件密集压测、`probe_whiteout_bug.py`、`probe_leak.py`、`probe_umount.py`。
   判据：dmesg 与串口输出关键词零命中、worker 没有 `ENOENT` 与 `ESTALE` 以外的错误（删文件密集压测另允许 `EEXIST`、`ENOTEMPTY`）、每组跑完 `debug_locks` 仍为 1。
3. 发布版内核上把同一套再跑一遍。调试内核慢、时序不同，有的竞争只在发布版上撞得到（whiteout 缓存问题的并发表现就是这样），两者不能互相代替。
4. 所有探针的结果与上一轮逐项比对；功能验证全部重跑。

压测至少看三个信号：内核日志的关键词、worker 的错误分类、`mnt_cache` 的净增（回收是否还有效）。

### 4.4 性能对照的做法

- **每个被比较的内核或 gsd 各建自己的模板**，不要指望换宿主上的文件能改变已有模板。
- 变体**逐轮交错**、每轮用新沙箱；只看同一批里的差值，不同批次、不同日期的绝对值不比（整机负载会让绝对值漂移几毫秒）。
- 每个沙箱的第一次 restore 丢掉。
- 差值在正负 3 ms 以内按无差别处理；同一个内核的两个模板之间差 1.5 到 3 ms 在历次数据里是常见的。
- 新得到的数字要标明平台、日期、代码版本和口径；判定性数字只进 [20](20-results-and-gaps.md)。

---

## 5. 未闭合项的处理建议

[15](15-known-limits.md) 列了全部限制。这里给出建议的处理顺序和各自的切入点。

### 5.1 建议先做的

| 优先 | 事项 | 为什么先做 | 从哪下手 |
|---|---|---|---|
| 1 | 新沙箱首个请求偶发约 5 秒 | 用户最容易感知，而且每个新沙箱都有机会遇到 | 问题在宿主侧（guest 里直连全部正常、gsd 自报耗时正常）。用 `probe_first_rpc.py` 复现，在宿主代理与 orchestrator 上抓首个连接的建立过程。5.01 秒这个值很整，可以先查有没有固定 5 秒超时后重试的环节（这只是切入点，不是结论） |
| 2 | 压测里出现过 2 次的 create 返回 `internal`（同一沙箱的第 174、177 轮，[15](15-known-limits.md) 的 U-02） | 原因不明，不能排除与内核改动有关 | 压测脚本已经会在这种失败时保存完整报错和 gsd 日志（`<模式>-create-fail-r<轮次>.txt`）。持续跑全部操作组的压测等它再出现，拿到 CRIU 的出错行再定位 |
| 3 | 给没有现场结果的多树分支补测试 | 这些是异常路径，出问题时正是用户最需要明确错误信息的时候 | 在 `verify_multitree.py` 里加：两棵树共用一个 socket；三棵树破坏中间一棵的镜像；多树的 dump 超时；某棵 dump 失败后前面的树被放开；多棵树之和触发容量预检 |
| 4 | 注入开关关闭态（`HOST_GSD_PATH=off`）的真实建模板（[15](15-known-limits.md) 的 T-08；默认路径已于 2026-10-09 现场确认） | 与整机级共存时会用到，现在只有单测 | 需要一台可以改 orchestrator 启动环境的机器；验收标准是关闭态建出的模板里没有 gsd 和 overlay-init |
| 5 | 沙箱 pause 再 resume 之后的行为 | 是平台的常规操作，目前只有代码注释的论证 | 在 `verify_gsd.py` 之类的流程中间插一次 pause 和 resume，之后 create、restore、list 应当与之前无差别 |

### 5.2 值得单独立项的

- **残留的单发 `ENOTEMPTY`**：推断在目录 copy-up 路径上（真实的 upper 里已有目录，而 overlay inode 认为没有 upper）。要定位需要在 copy-up 的 rename 失败处加诊断输出，再用多组并行的压测等它出现。
- **inode 哈希键的系统性审查**：overlay inode 以真实 inode 的地址为键是上游的设计，已经处理的两处悬空键（work 目录的 trap、失去 upper 的纯 upper 目录）都是撞出来之后才修的。
  值得做一次主动审查：列出切换后仍留在哈希里的每一类 inode，逐类确认它的键由什么钉住。
- **层压缩**：现在层栈深度上限约 190 层，沿一条链不断 checkpoint 会撞上。把多个层合并成一个可以解除这个上限，但要与层的引用回收、checkpoint 条目记录的层栈一起设计。
- **`/proc/mounts` 的显示**：让挂载选项反映当前层栈能减少排障时的误导，但 CRIU 要读 mountinfo，改之前要确认对 dump 和 restore 没有影响。
- **cgroup 的 `favordynmods`**：作为 `rcu_expedited` 的替代，没有实测过（[16](16-restore-slowness-case.md)）。
- **x86_64 guest**：需要内核配置打开 `CHECKPOINT_RESTORE`，并确认 soft-dirty 在 x86_64 上可用（上游有实现）；overlayfs 的补丁与架构无关。当前列为非目标。
- **950 上的部署与验证**：未部署、未测。gsd 不依赖 950 独有的硬件特性，预期可以直接按 [23](23-deployment.md) 部署，但所有数字都要重新测。

### 5.3 融合进 e2b-infra

目前 gsd 是一条独立的分支，靠切换宿主上的 orchestrator 与整机级那套轮流使用（[23](23-deployment.md)）。要让一个部署包同时带两套，还有这些事没有做
（有一份 2026-09 的融合方案草案，写于修复之前，其中关于注入"无条件生效"的部分已经过时，只可作为清单参考）：

| 事项 | 现状 | 要做的 |
|---|---|---|
| 打包 | gsd、带注入的 orchestrator、补丁内核都是手工构建、手工放到 `/fc-gsd/` | 进 RPM：构建 gsd 静态二进制并安装到 `/fc-gsd/gsd`；内核作为一个产物随包分发；基础镜像换成带 CRIU 4.2.1 的那一个 |
| 两套共存的开关 | 进程级由 guest 内的 gsd 在 49984 上应答；按融合草案的描述，整机级由宿主侧处理同一个端口（这一点没有对着整机级的代码核实）。现在同一时刻只能跑一套 | 定一个开关决定某个沙箱的 checkpoint 请求由哪一侧应答；粒度（整机、按模板、按沙箱）是需要先拍板的设计问题 |
| 跨模式的模板派生 | 没有处理（[15](15-known-limits.md) 的 L-15） | 定义清楚：带 gsd 的模板能否作为不带 gsd 的模板的基础，反过来呢；不支持就在建模板时明确拒绝 |
| SDK | 进程级与整机级各有一份 SDK，接口形状相近但不是同一份代码 | 合并成一份，或者明确两份各自的安装方式；线协议的字段已经各自演进，合并前要逐字段对照 |
| 测试脚本 | 已随手册入库到 `scripts/` | 接进统一的验收流程 |

---

## 6. 若要追回代码布局的那 8.7 ms

[17](17-performance-after-fixes.md) 讲了来龙去脉：内核修复让 `criu_restore` 慢了约 8.7 ms，原因不是任何一行代码变慢，而是内核代码整体挪了位置，
落在这颗 CPU 上一个不利的布局相位；交接时的布局只是碰巧落在有利的相位上。**现在的决定是不处理，接受普通布局的性能。**
如果将来确实需要这几毫秒，下面是当时验证过的做法和复核判据。

**做法。** 在内核 `lib/` 的最前面加两个只含填充、不含任何代码的目标文件（只对 arm64），把 `lib/` 的代码钉回有利的相位：
一个用 64 KiB 对齐加固定偏移钉住 `lib/` 的相位，另一个放在它前面，把前面代码的末尾挪到一个 64 KiB 窗口的中间。
对应的补丁没有入库，在结果目录 `gsd-fix4-20260930/patches/` 下（附录 A 的对照表里标为"没有入库"的那一个）。代价是约 117 KiB 的内核代码段，功能上零风险。

**当时的证据**（新 920B，2026-09-30；guest 内微基准：从 tmpfs 文件 `preadv` 192 MiB 进尚未缺页的匿名内存，取中位数；判定值见 [20](20-results-and-gaps.md)）：

| 内核布局 | 微基准 |
|---|---|
| 有利布局（修复之前） | 约 77 ms |
| 修复之后的普通布局 | 约 84 到 85 ms |
| 在有利布局里插入一段不含代码的填充（语义零变化，只让其后的代码整体后移）：64 B、4 KiB、16 KiB、64 KiB | 86.2、85.7、83.8、84.2 ms（变慢） |
| 同上，填充 128 KiB | 77.9 ms（回到快的一档） |
| 修复之后的内核加上面说的两个填充文件 | 77.2 ms |

周期是 128 KiB，与代码内容无关。把整个代码段整体平移（用超过段对齐的对齐方式）反而会变慢，所以只能钉 `lib/` 这一段的相位。

**复核判据。** 这是针对"这颗 CPU 加当前这份代码"的调参，不是一劳永逸的：

- 每次改动内核之后看 `System.map`：`lockref_get` 的地址对 `0x20000` 取模应当等于 `0xc450`，`_stext` 不变。
- `fs/`（overlayfs 所在）的代码增减在正负 32 KiB 以内时不受影响；`fs/` 之前的代码（`mm/`、`kernel/`、`arch/arm64/`）或 `lib/` 之后的代码有改动，或者 `fs/` 的变化超过约 32 KiB，都要重新调填充量。
- 判据不满足的后果只是性能回到普通布局（慢约 8 到 9 ms），不会出错。
- 换一种 CPU（例如 950）之后有利的相位可能不同，要用同样的微基准重新找。

**为什么不默认采用。** 它把一个偶然的性能收益变成了一个要长期维护的约束：之后每一次内核改动都要复核；而被追回的那一档性能在交接时也只是碰巧得到的。
是否值得，取决于 restore 耗时对业务有多敏感；现状的耗时构成见 [17](17-performance-after-fixes.md)。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 可注入的外部依赖 | `defaultSysOps`（`ckpt/sysops.go:38`）；假实现在 `ckpt/fake_test.go` |
| 编排入口 | `CreateCheckpoint`（`ckpt/service.go:163`）、`RestoreCheckpoint`（`ckpt/service.go:463`） |
| 失败清理与 degraded | `discardFailedCheckpoint`（`ckpt/service.go:825`）、`restoreFailed`（`ckpt/service.go:635`）、`checkReady`（`ckpt/service.go:677`） |
| 状态的导出与加载 | `persist`（`ckpt/state.go:256`）、`loadState`（`ckpt/state.go:341`）、`treesOf`（`ckpt/state.go:98`） |
| 回收 | `moveOrphansToTrash`（`ckpt/overlay.go:383`）、`reclaimSnapshotStore`（`ckpt/snapshot_store.go:25`） |
| 注入开关 | `GSDEnabled`（`packages/orchestrator/internal/cfg/model.go:39`） |
| 线协议 | `packages/gsd/spec/checkpoint/checkpoint.proto`、`spec/gsd/checkpoint/checkpoint.proto` |
| 内核 ioctl 主体与读锁规则 | `0003-overlayfs-hot-switch-fixes.patch` — `ovl_do_ioctl_checkpoint`；`overlayfs.h` 里 `ovl_switch_lock_read` 上方的注释 |
| 基线 tag 与内核构建 | `fc-kernels/kernel_base_tags.txt`、`fc-kernels/build.sh` |
| 验收、压测、探针脚本 | `e2b-infra/gsd/scripts/{acceptance,stress,probes}/`；调试内核配置在 `scripts/kernel/` |
| 布局填充的补丁（未入库） | 结果目录 `gsd-fix4-20260930/patches/` |

---

## 本章要点

1. 用户态从 `ckpt/service.go` 读起；内核从 [07](07-overlay-concurrency-and-fixes.md) 读起，不要直接读补丁的 diff。
2. gsd 侧的改动守住 §3.1 检查表的八条和 12 章清单里其余的五条不变量，核心是三条：dump 到 `clear_refs` 之间进程必须停着、restore 前进程必须杀干净、失败之后必须全量。
3. 内核侧的改动守住四条：失败在提交点之前、读后端状态持读锁、被换下的对象延迟释放、缓存随它所属的目录或层一起更新。
4. **内核改动必须过调试内核压测，并且发布版内核上再跑一遍**；调试内核必须关掉 `VMGENID`，否则 lockdep 的覆盖是零。
5. 改了内核、gsd 或 overlay-init 之后**一定要重建模板**才测得到；性能对照要每个变体各建模板、逐轮交错。
6. 未闭合项里建议先查首个请求约 5 秒和那 2 次未复现的 create 返回 `internal`，再补多树异常路径的现场测试。
7. 代码布局的那几毫秒可以追回，但会把一个偶然收益变成长期约束，当前的决定是不处理。
