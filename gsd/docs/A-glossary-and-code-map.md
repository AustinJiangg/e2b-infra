# 附录 A · 术语、代码地图与文件格式

> 全书的查询入口，不用从头读。术语、代码位置（以 `deltabox-gsd@259f1a25c` 为准）、文件与目录格式、接口速查、
> 开发期各种编号与本书说法的对照、章目表。
> 开关与计时键见 [24](24-configuration-and-observability.md)，错误码与异常见 [12](12-failure-semantics.md)，
> 已知限制见 [15](15-known-limits.md)，问题编号见 [14](14-issue-resolution.md)。

**路径简写**（全书通用，在这里声明一次）：`ckpt/` = `packages/gsd/internal/services/checkpoint/`。
内核补丁只引"补丁文件名 + 函数名"，不引补丁文件的行号（合并后的补丁行号没有意义）。

---

## 1. 术语表

### 1.1 本方案的概念

| 术语 | 英文 / 标识 | 一句话 | 详见 |
|---|---|---|---|
| **gsd** | Guest Sandbox Daemon | guest 内以 root 运行的守护进程，监听 49984，提供四个 RPC 和 `/health` | [01](01-problem-and-scope.md)、[04](04-architecture.md) |
| **checkpoint** | checkpoint | 沙箱某一时刻"用户进程 + 根文件系统"的记录，也指拍它的操作；接口名 `CreateCheckpoint`，书里也写 create | [09](09-checkpoint.md) |
| **restore / 回滚** | restore | 在沙箱不停机的前提下，把用户进程和根文件系统退回某个 checkpoint | [10](10-restore.md) |
| **进程级 / 整机级** | — | 本书讲的是进程级（guest 内 CRIU 加 overlayfs）；整机级指另一本手册（`rollback/`）讲的 Firecracker 层回滚 | [18](18-process-vs-machine-level.md) |
| **用户进程树** | process tree | uid 1000 的一个进程连同它的全部子孙；CRIU 一次 dump 或 restore 的单位 | [08](08-process-trees.md) |
| **根** | root（pid） | 一棵用户进程树的最上面那个进程：uid 1000、父进程不是用户进程、祖先里没有别的根 | [08](08-process-trees.md) |
| **范围外进程** | — | 不被 gsd 冻结或杀掉的进程：PID 1、envd、gsd 自己、root 起的服务 | [01](01-problem-and-scope.md)、[07](07-overlay-concurrency-and-fixes.md) |
| **层** | layer | overlay 的一个只读目录（lower）；gsd 管理的层在 `/overlay/layers/L<n>` | [04](04-architecture.md)、[06](06-overlay-hot-switch.md) |
| **upper / work** | upperdir / workdir | overlay 当前可写的目录，以及与它配对、供 copy-up 等操作周转用的目录 | [06](06-overlay-hot-switch.md)、[07](07-overlay-concurrency-and-fixes.md) |
| **层栈** | layer stack | 当前（或某个 checkpoint 记录的）自上而下的一组层 | [06](06-overlay-hot-switch.md) |
| **切换** | hot switch | 一次热切层 ioctl（`OVL_IOCTL_CHECKPOINT`） | [06](06-overlay-hot-switch.md) |
| **下沉** | sink | checkpoint 时把当前 upper 原地变成最上面一个层、换上空的新 upper | [06](06-overlay-hot-switch.md) |
| **自动加入的最上层** | auto lower | checkpoint 模式下内核自动把旧 upper 作为第一个 lower，gsd 不必（也不能）再传一次 | [06](06-overlay-hot-switch.md) |
| **时间线 / 被丢弃的时间线** | timeline | 一段连续的历史；restore 丢弃的是"目标 checkpoint 之后到 restore 那一刻"的那一段 | [07](07-overlay-concurrency-and-fixes.md) |
| **提交点** | commit point | 一个操作里开始做不可逆修改的那一刻。gsd 的 restore 是"杀进程"；内核 ioctl 是"发布新层栈" | [12](12-failure-semantics.md)、[07](07-overlay-concurrency-and-fixes.md) |
| **degraded** | degraded | gsd 的三类受限状态，`/health` 的 body 里给出原因 | [12](12-failure-semantics.md) |
| **隐藏的 checkpoint** | deleted（标记） | 已被删除、但还有增量子节点依赖其镜像的 checkpoint：list 不返回，镜像保留到子节点删完 | [13](13-state-concurrency-capacity.md) |
| **增量 / 全量** | incremental / full dump | CRIU 只写上次 dump 之后脏的页 / 写全部页 | [02](02-criu-and-incremental-memory.md) |
| **父镜像** | parent image | 一棵树的增量 dump 所依赖的上一份镜像（上一个 checkpoint 里同一棵树的目录） | [09](09-checkpoint.md)、[13](13-state-concurrency-capacity.md) |
| **假全量** | — | 名义上是增量、实际写出接近全量的镜像（THP 或 VMA 合并造成） | [02](02-criu-and-incremental-memory.md) |
| **snapshot store** | `/var/lib/gsd/snapshots` | 放 CRIU 镜像和 `state.json` 的 2 GiB tmpfs，占 guest 内存 | [04](04-architecture.md)、[13](13-state-concurrency-capacity.md) |
| **注入** | injection | 建模板时 orchestrator 把 gsd、gsd.service、overlay-init 放进模板并自检 | [05](05-guest-boot-and-injection.md) |
| **模板固化** | — | 模板快照里带着建模板时的内核、gsd、overlay-init；换其中任何一样都要重建模板 | [05](05-guest-boot-and-injection.md)、[17](17-performance-after-fixes.md) |
| **冻结窗口** | `stopped_ms` | 用户进程从被停住到被放开的时间；多树时从第一棵 dump 完算起 | [11](11-end-to-end.md)、[24](24-configuration-and-observability.md) |

### 1.2 内核侧（overlayfs 热切层）

| 术语 | 标识 | 一句话 | 详见 |
|---|---|---|---|
| **copy-up** | — | 修改只在 lower 里的文件前，先把它拷到 upper | [07](07-overlay-concurrency-and-fixes.md) |
| **whiteout** | 0/0 字符设备 | upper 里表示"这个名字被删了"的占位项；内核会在 work 目录里缓存一个共享的 | [07](07-overlay-concurrency-and-fixes.md) |
| **opaque 目录** | — | 盖在 whiteout 上重建的目录，不与下层同名目录合并 | [07](07-overlay-concurrency-and-fixes.md) |
| **后端状态** | `__upperdentry`、`lowerpath`、`ovl_entry`、`realfile` | overlay 的 inode、dentry、打开文件所记录的"背后是哪个真实文件" | [07](07-overlay-concurrency-and-fixes.md) |
| **层数组** | `ofs->layers` | 文件系统级的各层描述，每层一个私有 mount | [07](07-overlay-concurrency-and-fixes.md) |
| **trap inode** | `ovl_get_trap_inode` | 陷阱 inode。overlay 在自己的 inode 缓存里给层根目录、work 目录放的占位项，用来发现层嵌套（命中返回 `ELOOP`） | [07](07-overlay-concurrency-and-fixes.md) |
| **inode 哈希键** | — | overlay inode 在缓存里的键，是它背后某个真实 inode 的内存地址 | [07](07-overlay-concurrency-and-fixes.md) |
| **代数** | `checkpoint_gen` | 层栈每切换一次加一；inode 上记着它最后一次被确认时的代数 | [06](06-overlay-hot-switch.md) |
| **计划** | `struct ovl_ckpt_item` | ioctl 在提交点之前为每个受影响的缓存 inode 备好的改写项 | [07](07-overlay-concurrency-and-fixes.md) |
| **promote** | — | 提升。checkpoint 时把一个有 upper 的 inode 改成"以原 upper 为 lower"的 inode | [06](06-overlay-hot-switch.md)、[07](07-overlay-concurrency-and-fixes.md) |
| **detach** | `OVL_DETACHED` | restore 时把一个有 upper 的非目录 inode 从哈希里摘出，保留数据，只对还拿着它的进程可见 | [07](07-overlay-concurrency-and-fixes.md) |
| **陈旧** | `OVL_E_STALE` | 被 restore 丢弃的 dentry（及经它查出来的一切）；只读可见，修改返回 `ESTALE` | [07](07-overlay-concurrency-and-fixes.md) |
| **复活** | `ovl_splice_alias` | restore 后一次新的 lookup 把被钉住的旧目录 dentry 接回当前时间线 | [07](07-overlay-concurrency-and-fixes.md) |
| **延迟释放** | `ovl_defer_put` | 被换下的引用不当场释放，等下一次 ioctl 拿到写锁后再放 | [07](07-overlay-concurrency-and-fixes.md) |

### 1.3 CRIU 与内存

| 术语 | 标识 | 一句话 | 详见 |
|---|---|---|---|
| **CRIU** | `/usr/sbin/criu` | 用户态的进程 checkpoint/restore 工具；本方案固定用基础镜像里源码编译的 4.2.1 | [02](02-criu-and-incremental-memory.md) |
| **soft-dirty** | `PTE_SOFT_DIRTY` | 页表项上的"上次清零之后被写过"标志，增量 dump 靠它；arm64 上由补丁 0002 提供 | [02](02-criu-and-incremental-memory.md) |
| **clear_refs** | `/proc/<pid>/clear_refs` 写 `4` | 清掉一个进程全部页的 soft-dirty 标志 | [02](02-criu-and-incremental-memory.md)、[09](09-checkpoint.md) |
| **THP** | 透明大页 | 必须是 never，否则 soft-dirty 按 2 MiB 记 | [02](02-criu-and-incremental-memory.md) |
| **MMDS** | — | Firecracker 提供给 guest 的元数据服务；gsd 每个请求从这里读访问令牌的哈希 | [04](04-architecture.md) |
| **调试内核** | — | 在发布版配置上打开 KASAN、lockdep 等检查的 guest 内核，只用于压测 | [19](19-testing-and-debug-kernel.md) |

---

## 2. 代码地图

代码在 `KASandbox_0904` 仓库的 `deltabox-gsd` 分支。gsd、orchestrator 的注入逻辑、Python SDK、guest 内核补丁同仓。

### 2.1 gsd（`packages/gsd/`）

| 文件 | 职责 | 主要入口 |
|---|---|---|
| `main.go` | 进程入口：命令行参数、HTTP 服务、`/health` 免鉴权、启动时的两项内核设置 | `parseFlags`（`:55`）、`withOpenHealth`（`:103`）、`main`（`:113`）、`enableExpeditedRCU`（`:178`）、`ensureTHPNever`（`:196`） |
| `internal/api/store.go` | `/health`：正常或 degraded 加原因 | `GetHealth`（`:23`） |
| `internal/permissions/authenticate.go` | 校验 `X-Access-Token` | `HashAccessToken`（`:20`）、`Authenticate`（`:53`） |
| `internal/permissions/mmds.go` | 从 MMDS 读访问令牌的哈希，不缓存 | `AccessTokenHash`（`:49`） |
| `ckpt/service.go` | 四个 RPC 的编排，degraded 状态 | `Handle`（`:115`）、`CreateCheckpoint`（`:163`）、`RestoreCheckpoint`（`:463`）、`ListCheckpoints`（`:745`）、`DeleteCheckpoint`（`:764`）；`incrementalParent`（`:396`）、`clearSoftDirty`（`:418`）、`restoreFailed`（`:635`）、`DegradedReason`（`:661`）、`checkReady`（`:677`）、`checkImages`（`:703`）、`discardFailedCheckpoint`（`:825`）、`resumeAfterFailedDump`（`:840`）、`criuErrorCode`（`:865`）、`overlayErrorCode`（`:874`） |
| `ckpt/overlay.go` | overlay 管理器：发 ioctl、下沉、restore 的两段式、目录回收 | `NewOverlayManager`（`:75`）、`SinkUpper`（`:125`）、`PrepareRestore`（`:203`）、`CommitRestore`（`:240`）、`AbortRestore`（`:260`）、`makeUpperWork`（`:285`）、`restoreDirs`（`:323`）、`reclaimOrphans`（`:372`）、`moveOrphansToTrash`（`:383`）、`isTransientLayerDir`（`:451`）、`layerName`（`:464`）、`overlayOptions`（`:533`）、`checkOverlayOptionsLen`（`:540`）、`applyOverlaySwitch`（`:549`） |
| `ckpt/criu.go` | CRIU 命令的组装与执行（带时限），restore 前杀进程 | `runCriuCmd`（`:28`）、`buildCriuDumpCmd`（`:51`）、`buildCriuRestoreCmd`（`:100`）、`logTail`（`:144`）、`killTrees`（`:215`） |
| `ckpt/pid.go` | 一次 `/proc` 扫描、找根、排除集合、dump 前的状态 | `parseStat`（`:52`）、`scanProcs`（`:83`）、`kernelThreads`（`:131`）、`envdPID`（`:207`）、`userRootsIn`（`:259`）、`scanUsers`（`:305`）、`resumable`（`:414`） |
| `ckpt/shared.go` | 多棵树时的预检：树间共用的管道与 socket | `crossTreeShared`（`:49`）；文件头注释是这条规则的论证 |
| `ckpt/pool.go` | checkpoint 条目、增量父子关系、延迟删除、层引用 | `linkParents`（`:114`）、`remove`（`:151`）、`layerRefs`（`:197`） |
| `ckpt/state.go` | `state.json` 的保存、加载与校验 | `treesOf`（`:98`）、`importState`（`:137`）、`persist`（`:256`）、`writeStateFile`（`:296`）、`loadState`（`:341`） |
| `ckpt/snapshot_store.go` | 回收无主的镜像目录 | `reclaimSnapshotStore`（`:25`） |
| `ckpt/capacity.go` | create 前的容量预检 | `estimateDump`（`:60`）；文件头注释说明估算口径 |
| `ckpt/sysops.go` | 对外部世界的依赖（`/proc`、信号、CRIU、systemctl）集中成可注入的字段 | `defaultSysOps`（`:38`） |
| `ckpt/*_test.go` | 单测；`fake_test.go` 提供假的 `/proc` 与假的 CRIU | — |
| `spec/checkpoint/checkpoint.proto` | 线协议（与仓库根 `spec/gsd/checkpoint/checkpoint.proto` 必须逐字节相同） | `service Checkpoint`（`:105`）、`message Timings`（`:17`） |
| `docker/Dockerfile.criu421` | guest 基础镜像：ubuntu 22.04 加源码编译的 CRIU 4.2.1 | — |
| `README.md` | 现状描述，已与代码对齐 | — |

### 2.2 orchestrator 的注入（`packages/orchestrator/internal/`）

注入只发生在建模板时，由 `GSDEnabled()` 控制（`cfg/model.go:39`；`HOST_GSD_PATH` 默认 `/fc-gsd/gsd`，设为小写 `off` 关闭，`cfg/model.go:22`、`:47`）。它管五处：

| 序 | 位置 | 做什么 |
|---|---|---|
| 1 | `template/build/core/rootfs/rootfs.go:233` | 往模板的根文件系统里加 gsd 相关文件 |
| 2 | `template/build/core/rootfs/gsd.go` — `addGSDFiles`（`:33`） | 读宿主上的 gsd 二进制（读不到则建模板报错），加入 gsd、`gsd.service`、`overlay-init` |
| 3 | `template/build/layer/create_sandbox.go:102` | init 用 `/usr/bin/overlay-init`（`GSDInitPath`，`core/rootfs/gsd.go:24`）而不是 systemd |
| 4 | `template/build/layer/layer_executor.go:122` → `layer/gsd.go` — `injectGsdInSandbox`（`:50`）、`gsdCheckScript`（`:22`） | 每建一层后把 gsd 注入沙箱、重启并自检 |
| 5 | `template/build/phases/base/hash.go:55` | base 层缓存键多一个 `"gsd"`，两种模式的 base 层不互相复用 |

模板文件在 `template/build/core/rootfs/gsdfiles/`：`overlay-init.sh.tpl`（guest 的 init，头注释是布局说明）和 `gsd.service.tpl`。
gsd 的启动参数取值在 `template/build/core/rootfs/templates.go:57-61`。

### 2.3 Python SDK（`py-sdk/`）

| 文件 | 内容 |
|---|---|
| `e2b/sandbox_sync/checkpoint.py`、`e2b/sandbox_async/checkpoint.py` | `sbx.checkpoint` 的同步与异步实现：`create`、`restore`、`list`、`delete`、`is_running` |
| `e2b/sandbox/checkpoint/types.py` | `CheckpointInfo` 与计时字典的转换 |
| `e2b/gsd/api.py` | 发给 gsd 的请求头（`checkpointd_headers`，`:26`）、错误码映射（`CHECKPOINT_RPC_ERROR_MAP`，`:59`） |
| `e2b/gsd/checkpoint/` | 由 proto 生成的三个文件 |
| `e2b/connection_config.py` | gsd 地址的选择（`get_checkpointd_url`，`:168`） |
| `e2b/exceptions.py` | `NotEnoughSpaceException`（`:50`）、`CheckpointRestoreException`（`:122`） |
| `tests/test_gsd_checkpoint.py`、`test_gsd_errors.py`、`test_gsd_headers.py` | 单测 |

### 2.4 guest 内核（`fc-kernels/`）

| 文件 | 内容 |
|---|---|
| `kernel_base_tags.txt` | 每个内核版本对应的上游基线 tag。6.1.158 固定为 `microvm-kernel-6.1.158-15.288.amzn2023` |
| `build.sh` | 取基线、依次打 `patches/<版本>/` 下的补丁、编译 |
| `configs/arm64/6.1.158.config` | 发布版内核配置（已开 `CHECKPOINT_RESTORE`、`MEM_SOFT_DIRTY`） |
| `patches/6.1.158/0001-virtio_balloon-Support-wait-on-ACK-for-hinting.patch` | 上游的 virtio_balloon 补丁，与 gsd 无关 |
| `patches/6.1.158/0002-overlayfs-hot-switch-checkpoint.patch` | 交接时的原始补丁：热切层 ioctl、arm64 soft-dirty。未改动 |
| `patches/6.1.158/0003-overlayfs-hot-switch-fixes.patch` | 全部修复，合并成一个；文件头的提交说明按主题列出 |

补丁 0003 按主题到函数（主题名与提交说明的六组一一对应，机制都在 [07](07-overlay-concurrency-and-fixes.md)）：

| 主题 | 函数或字段 |
|---|---|
| mmap 与调试输出 | `ovl_mmap`；`fs/proc/task_mmu.c` 去掉调试计数器 |
| 失败处理与发布 | `ovl_ioctl_checkpoint`、`ovl_do_ioctl_checkpoint`、`ovl_ckpt_create_workdir`、`ovl_layers_snapshot`、`layers_seq`、`ckpt_mutex`、`ovl_path_next_oe` |
| 切换与 overlay 操作的互斥 | `switch_sem`、`ovl_switch_lock_read` / `ovl_switch_trylock_read` / `ovl_switch_unlock_read`；`ovl_ckpt_build_plan`、`ovl_ckpt_plan_inode`、`ovl_ckpt_move_entry`、`ovl_ckpt_apply_item`；`ovl_defer_put`、`ovl_release_deferred`；各入口的 `__ovl_xxx` 内层函数 |
| 打开的文件与目录跨切换的语义 | `ovl_write_switch`、`struct ovl_file`、`ovl_inode_lower_is_private`；`ovl_ckpt_want_move`、`ovl_ckpt_move_upper`、`ovl_ckpt_copy_file`、模块参数 `ckpt_move_open`；`ovl_get_inode`、`ovl_refresh_reused_inode`、`ovl_splice_alias`、`ovl_dentry_is_stale`；`ovl_rename`、`__ovl_rename`、`FS_RENAME_DOES_D_MOVE` |
| 回收被换下的对象 | `ovl_inode_keep_real`、`ovl_dentry_release`、`struct ovl_layer_set`、`ovl_ckpt_retarget`、`ovl_ckpt_retarget_layers`、`ovl_layer_set_release`、`ovl_layer_sets_reap`、`ovl_layer::mnt_peer` |
| 属于 work 目录的状态、跨 restore 的删除 | `ovl_whiteout`、`ovl_whiteout_cache_ok`、`ofs->workdir_trap`；`ovl_do_remove`、`ovl_do_remove_locked`、`ovl_dir_read_merged` |

### 2.5 测试脚本

随手册附带，在 `e2b-infra/gsd/scripts/` 下分三类（各脚本测什么见 [19](19-testing-and-debug-kernel.md)）：

| 目录 | 脚本 |
|---|---|
| `acceptance/` | `verify_gsd.py`、`verify_fixes.py`、`verify_multitree.py`、`verify_clear_refs_race.py`、`demo_checkpoint_perf.py` |
| `stress/` | `stress_overlay_ckpt.py`、`stress_multitree.py`、`stress_delete_dense.py`、`stress_gsd.py`、`run_stress_watch.sh`、`fc_console.sh` |
| `probes/` | `probe_fd_rollback.py`、`probe_mmap_rollback.py`、`probe_move_cost.py`、`probe_estale.py`、`probe_estale_file.py`、`probe_rename_race.py`、`probe_leak.py`、`probe_umount.py`、`probe_whiteout_bug.py`、`probe_first_rpc.py`、`probe_user_roots.py`、`probe_proc_cost.py`、`probe_tpl_env.py`、`exp_gsd_swap.py` |

---

## 3. 文件与目录格式

### 3.1 运行时布局

| 位置 | 路径 | 内容 |
|---|---|---|
| 宿主 | `/fc-gsd/gsd` | gsd 二进制（`HOST_GSD_PATH` 的默认值） |
| 宿主 | `/fc-gsd/orchestrator` | 带注入逻辑的 orchestrator |
| 宿主 | `/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin` | 打过三个补丁的 guest 内核（与整机级共用的机器用 `HOST_KERNELS_DIR=/fc-gsd/kernels` 指过来） |
| guest | `/usr/bin/gsd`、`/usr/bin/overlay-init`、`/usr/sbin/criu` | 注入的 gsd 与 init；基础镜像自带的 CRIU |
| guest | `/mnt/lower` | 基础层（ext4 根的 bind） |
| guest | `/overlay/workspace/{upper,work}` | 开机时的初始 upper 与 work |
| guest | `/overlay/layers/` | gsd 管理的层与临时目录，见 §3.2 |
| guest | `/var/lib/gsd/snapshots/` | snapshot store，见 §3.3 |

完整的目录与层布局表在 [04](04-architecture.md)（全书唯一），这里只列查询用的要点。

### 3.2 `/overlay/layers/` 下的名字

| 名字 | 是什么 | 生命周期 |
|---|---|---|
| `L<n>` | 一个层（下沉后的 upper）。`n` 在 guest 的一次开机内单调递增（`layerName`，`ckpt/overlay.go:464`） | 被当前层栈或任何 checkpoint 条目引用时保留，否则回收 |
| `upper_<ckpt>_next`、`work_<ckpt>_next` | 一次 create 换上的新 upper 与 work | 是当前 upper/work 时保留；被切走后即为垃圾 |
| `restore_<ckpt>_<seq>_upper`、`restore_<ckpt>_<seq>_work` | 一次 restore 换上的新 upper 与 work；`seq` 每次 restore 加一（`restoreDirs`，`ckpt/overlay.go:323`） | 同上 |
| `.trash/` | 回收站：待删目录先在锁内改名进来，再在锁外删除 | — |
| `layer_*` | 修复之前的旧层名，仍被认作层 | 只在从旧状态升级时出现 |

`<ckpt>` 是 checkpoint 的 ID，形如 `ckpt_<纳秒时间戳>`（`generateCheckpointID`，`ckpt/service.go:895`）。

### 3.3 snapshot store 下的名字

| 名字 | 是什么 |
|---|---|
| `<ckpt>_mem/` | 一个 checkpoint 的镜像目录。没有用户进程时是空目录 |
| `<ckpt>_mem/t<i>/` | 第 `i` 棵用户进程树的 CRIU 镜像（按根 pid 升序编号，一棵树时是 `t0`） |
| `<ckpt>_mem/t<i>/inventory.img`、`pstree.img`、`pages-*.img`、`pagemap-*.img` 等 | CRIU 的镜像文件。restore 前检查 `inventory.img` 可读 |
| `<ckpt>_mem/t<i>/parent` | 增量镜像里 CRIU 留下的符号链接，指向父镜像目录（上一个 checkpoint 里同一棵树的目录）。restore 沿它读父镜像 |
| `<ckpt>_mem/t<i>/criu-dump.log` | dump 的详细日志。dump 失败时随镜像目录删除，尾部进错误信息 |
| `<ckpt>_mem/t<i>/criu-restore.log` | 最近一次 restore 的日志（每次覆盖），失败时尾部进错误信息 |
| `state.json` | gsd 的持久化状态，见 §3.4 |
| `.trash/` | 回收站 |

### 3.4 `state.json`（版本 2）

每次 create、restore、delete 结束时原子写入（临时文件、fsync、rename、fsync 目录），gsd 启动时加载。字段（`ckpt/state.go:39-96`）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `version` | 整数 | 2。版本 1（单树、镜像直接在 `<ckpt>_mem/` 下）仍可加载 |
| `overlay.current_upper`、`overlay.current_work` | 字符串 | 当前的 upper 与 work 目录 |
| `overlay.layers` | 字符串数组 | 当前层栈，自上而下 |
| `overlay.layer_seq`、`overlay.restore_seq` | 整数 | 层序号与 restore 序号的当前值 |
| `last_trees[]` | 数组 | 每棵树下一次增量 dump 的父镜像：`root_pid`、`root_start`（根进程的启动时间，用来认"同一棵树"）、`checkpoint_id`、`dir`。为空表示下一次全部全量 |
| `restore_failure` | 字符串 | 上一次半途失败的 restore 留下的 degraded 原因；空表示没有 |
| `checkpoints[]` | 数组 | 全部条目，含已删除但因子节点而保留的 |
| `checkpoints[].id`、`name`、`mem_path`、`layers`、`created_at` | — | ID、名字、镜像目录、这个 checkpoint 的层栈、创建时间（Unix 纳秒） |
| `checkpoints[].trees[]` | 数组 | 每棵树：`root_pid`、`root_start`、`dir`（子目录名）、`parent_id`（父镜像所在的 checkpoint，全量为空）、`image_bytes` |
| `checkpoints[].parent_id`、`had_processes`、`image_bytes` | — | 整个 checkpoint 的汇总值，由 `trees` 推出，写出来方便人读 |
| `checkpoints[].deleted` | 布尔 | 已被删除、只是隐藏 |

排障时这是第一手状态：当前层栈以它为准（`/proc/mounts` 不反映切换，见 [15](15-known-limits.md)）。

---

## 4. 接口速查

gsd 监听 guest 的 `0.0.0.0:49984`，Connect 协议，服务名 `checkpoint.Checkpoint`。除 `GET`/`HEAD /health` 外每个请求校验 `X-Access-Token`。

| RPC | 作用 | 关键返回 |
|---|---|---|
| `CreateCheckpoint` | 保存全部用户进程树并下沉 upper | `checkpoint_id`、`timings`、`created_at`、`has_process_image`、`process_trees`、`parent_checkpoint_id`、`image_bytes`、`clear_refs_failed` |
| `RestoreCheckpoint` | 杀掉当前全部用户进程，切回目标层栈，恢复目标里的全部树 | `success`、`timings` |
| `ListCheckpoints` | 列出未被隐藏的 checkpoint | 每项的 ID、名字、`created_at`、`image_bytes`、`has_process_image`、`process_trees`、`parent_checkpoint_id` |
| `DeleteCheckpoint` | 删除一个 checkpoint（有子节点时只隐藏） | `success` |
| `GET /health` | 恒为 HTTP 200；body 是 `{"status":"ok"}` 或 `{"reason":"...","status":"degraded"}`（键按字母序输出，不要依赖键序） | — |

错误码与每种错误之后的状态见 [12](12-failure-semantics.md)；计时键的定义见 [24](24-configuration-and-observability.md)。

内核接口只有一个：`OVL_IOCTL_CHECKPOINT`（`_IOW('O', 1, struct ovl_checkpoint_args)`），对 overlay 挂载点的目录 fd 调用，需要 `CAP_SYS_ADMIN`；
参数是一个长度不超过 4096 字节的选项串（[06](06-overlay-hot-switch.md)）。以及一个模块参数 `overlay.ckpt_move_open`（0、1、2，默认 2）。

---

## 5. 开发期编号与本书说法的对照

本书正文按主题讲、不用开发期的编号。但结果文件、构建记录和旧文档里到处是这些编号，核对原始材料时需要这张表。**这些名字只在本节出现。**

### 5.1 内核补丁的开发期编号（2026-09-30 到 10-08）

修复是在一条开发分支上一个提交一个提交做的，当时每个提交导出成一个编号补丁；最后全部合并成入库的 `0003-overlayfs-hot-switch-fixes.patch`。

| 开发期编号或提交 | 当时的标题（文件名里的部分） | 主题（0003 提交说明里的组） | 最终归属 |
|---|---|---|---|
| 0003（开发期） | mmap-no-copyup-drop-debug-printk | mmap 与调试输出 | 进 0003 |
| 0004 | fix-late-failure-uaf-drop-debug-printk | 失败处理与发布；调试输出 | 进 0003 |
| 0005 | publish-layers-numlayer-consistently | 失败处理与发布 | 进 0003 |
| 0006 | retire-root-oe-pin-readdir-serialize-ioctl | 失败处理与发布（串行化、readdir 用一个 entry）；其中"保留旧的根 entry"后来被回收方案取代 | 进 0003 |
| 0007 | exclude-overlay-operations-from-checkpoint-ioctl | 切换与 overlay 操作的互斥（读写锁、计划、延迟释放） | 进 0003 |
| 0008 | move-writes-of-pre-checkpoint-fds-to-new-upper | 打开的文件跨切换的语义（第一次写时切到新 upper） | 进 0003 |
| 0009 | revive-directories-with-replaced-lower-after-restore | 目录跨 restore 的语义（祖先链 `ESTALE`） | 进 0003 |
| 0010 | keep-open-for-write-files-in-upper-on-checkpoint | 打开的文件跨切换的语义（checkpoint 时搬走） | 进 0003 |
| 0011 | lib-pin-lib-text-address-phase-for-arm64-guest | 与 overlayfs 无关：用填充固定内核代码布局的相位，追回约 8.7 ms（[17](17-performance-after-fixes.md)） | **没有入库**，接受普通布局 |
| `dc44545bf` | keep the replaced lower of a regular file with its inode | 回收 | 进 0003 |
| `4ab286374` | drop the lower stack references of a released dentry | 回收 | 进 0003 |
| `31a7069f7` | release replaced layer arrays and root entries | 回收 | 进 0003 |
| `3d270b5ad` | do not let a restore split a rename | 目录跨 restore 的语义（rename 竞争） | 进 0003 |
| `a2fb16545` | drop the whiteout cache and work dir trap on a switch | 属于 work 目录的状态 | 进 0003 |
| `123915cfb` | do not remove through a dentry that a restore dropped | 跨 restore 的删除 | 进 0003 |

最后六行没有开发期编号，是内核开发分支上的提交号；入库的 0003 等于"开发期 0003 到 0010 加这六个提交"压成一个补丁。
从基线 tag 依次打 0001、0002、0003 得到的源码树与开发分支末端逐字节相同（结果目录 `gsd-release-20261008` 的 `applycheck.txt`）。

### 5.2 更早的一次重排（2026-09-11 与 09-28）

旧文档里也出现"0003""0004""0005"，指的是**另一套编号**，不要与 §5.1 混淆：

| 时间 | 编号 | 内容 |
|---|---|---|
| 2026-09-11 | 0001 | 上游 virtio_balloon |
| | 0002 | 交接时的热切层与 soft-dirty 实现（当时已导出成补丁的部分） |
| | 0003 | 交接时实现中只存在于现场内核、从未导出成补丁的部分（`fs/proc/task_mmu.c` 的改动），当时补导出来 |
| | 0004 | `ovl_mmap` 只让共享可写映射走写路径（restore 变慢的真根因，[16](16-restore-slowness-case.md)） |
| | 0005 | 去掉每次 lookup 的调试打印 |
| 2026-09-28 重排 | 0001 | 同上 |
| | 0002 | 旧 0002 加旧 0003：交接时的实现归并成一个补丁 |
| | 0003 | 旧 0004 加旧 0005：交接之后的修复归并成一个补丁 |

重排前后的源码树逐字节相同。所以旧文档（`gsd-restore-fix-plan.md`、`gsd-restore-report.md`、`RUNBOOK-950-gsd.md`、`gsd-merge-into-e2b-infra-plan.md` 等）里的
"0004/0005"是 mmap 修复与去打印，即 §5.1 的"0003（开发期）"、现在 0003 的第一组；而 2026-09-30 之后结果文件里的"0004/0005"是失败路径修复与成组发布。

### 5.3 内核构建代号

| 代号 | 包含的补丁 | 构建时间（版本串） | 出现在哪些结果目录 | 备注 |
|---|---|---|---|---|
| v1 | 0001 至开发期 0003 | 2026-09-23 07:36 | `gsd-new920b-20260923`、`gsd-fixes-20260930`、`gsd-fix3-20260930` | 并发相关代码与交接时相同 |
| v2 | 0001 至 0006 | 2026-09-30 11:48 | `gsd-fixes-20260930`、`gsd-fix3-20260930` | |
| dbg | v2 加 KASAN、lockdep、`DEBUG_LIST`、`DEBUG_OBJECTS` | 无记录 | `gsd-fixes-20260930` | lockdep 实际没有生效（未关 `VMGENID`，[19](19-testing-and-debug-kernel.md)） |
| bisect-0003 / 0004 / 0005 | 0001 至对应编号 | 2026-09-30 16:50 / 15:55 / 15:56 | `gsd-fix3-20260930`、`gsd-fix4-20260930` | 定位那 8.7 ms 用的二分构建 |
| v3 / dbg3 | 0001 至 0008 | 2026-09-30 15:44 | `gsd-fix3-20260930` | dbg3 起调试内核关掉 `VMGENID`，lockdep 有效 |
| v4 / dbg4 | 0001 至 0011 | 2026-09-30 19:20 | `gsd-fix4-20260930` | 唯一带布局填充的一代 |
| v5 / dbg5 | 0001 至 0010 加 §5.1 的前四个提交 | 2026-10-08 16:00 | `gsd-fix5-20261008`、`gsd-final-20261008` | 不含 0011 |
| v6 / dbg6 | v5 加 §5.1 的后两个提交 | 2026-10-08 20:06 | `gsd-fix6-20261008`、`gsd-release-20261008` | **v6 即入库补丁对应的发布版内核**，最终验证用的就是它（未重编） |

没有名为 dbg2 的构建记录。结果目录里以 `c-` 开头的内核目录名（例如各轮的候选与诊断构建）是未入库的中间产物，不对应任何交付。

### 5.4 gsd 的开发期提交号

分支历史在定稿前整理过，下面的提交号**不在** `deltabox-gsd` 的最终历史里，只出现在结果文件和二进制文件名中：

| 提交号 | 是什么 | 对应最终历史 |
|---|---|---|
| `5f6b66b42` | 修复之前的 gsd（旧仓库里的构建），各轮对照里的"旧 gsd" | 交接点 `e47d09d04` 的 gsd 包与它同源 |
| `a35ce1993` | 第一版修复（当时 21 个提交） | 约相当于 [14](14-issue-resolution.md) 提交表的第 1 至 18 项 |
| `5882e84d7` | 加上性能回退的修复、dump 失败带日志尾部、SDK 错误映射、systemd 启动限制 | 约相当于第 19 至 22 项 |
| `c0630349c` | 加上默认注入、多棵树、`/tmp` 回滚、根目录 755；Go 代码与最终提交相同 | 第 23 至 27 项 |
| `259f1a25c` | 最终提交：只把仓库里的 0003 换成最终版 | 第 28 项 |

对应关系标"约"的两行是按各提交的内容推断的，结果文件里没有逐一对照的记录。

### 5.5 模板代号

本书统一把模板叫 `base-gsd`。验证时每换一次内核或 gsd 都要重建模板（模板固化），所以结果文件里有一串带序号的名字：

| 代号 | 内核 | gsd | 规格 | 模板 ID | 出现在 |
|---|---|---|---|---|---|
| base-gsd（旧） | v1 | `5f6b66b42` | 无记录 | `py4zonipnqq5s4gd36en` | `gsd-fixes-20260930` |
| base-gsd2 | v2 | `a35ce1993` | 2 vCPU / 2048 MB | `8fsnsgjhszc0u2tf2yjq` | `gsd-fixes-20260930` |
| base-gsd-dbg | dbg | `a35ce1993` | 2 vCPU / 4096 MB | `92rsazy24poj23o0z7ju` | `gsd-fixes-20260930` |
| base-gsd3 | v2 | `5882e84d7` | 无记录 | `j70pk1gv55353n26dk0j` | `gsd-fixes-20260930`（`perf-fix/`）、`gsd-fix3-20260930` |
| base-gsd3-kv1 | v1 | 与 base-gsd3 同一版 gsd（提交号无单独记录） | 无记录 | `fwhlnxm9rcd72d9bnayp` | 同上；只用于内核归因 |
| base-gsd4 / base-gsd-dbg3 | v3 / dbg3 | `5882e84d7` | 2048 MB / 4096 MB | `s7ia9wgg7s7m36tq7d1e` / `h0wxz2pl9m5likrkqkn7` | `gsd-fix3-20260930` |
| base-gsd5 / base-gsd-dbg4 | v4 / dbg4 | `5882e84d7` | 2048 MB / 4096 MB | `mw5jk6w5c5tlcyhvbjr1` / `y6oom1x646mjie49327q` | `gsd-fix4-20260930` |
| base-gsd6 / base-gsd-dbg5 | v5 / dbg5 | `5882e84d7` | 2048 MB / 4096 MB | 无记录 | `gsd-fix5-20261008` |
| base-gsd7 / base-gsd-dbg7 | v5 / dbg5 | `c0630349c` | 2048 MB / 4096 MB | `1476srajq7fyorycvcvf` / `51wgpyhzqzzdn4ai3k9e` | `gsd-final-20261008`；这一代模板有 whiteout 缓存等三个内核问题，不要再用 |
| base-gsd8 / base-gsd-dbg8 | v6 / dbg6 | `c0630349c` | 2048 MB / 4096 MB | `6hyb7dt8339ohdashug1` / `begv1gydjcbg8okcvmxq` | `gsd-fix6-20261008` |
| base-gsd9 | v6 | `259f1a25c` | 2 vCPU / 2048 MB | `afw242v9c4zvx0m3w6r1` | `gsd-release-20261008`；**本书判定性数字所用的模板** |

没有名为 base-gsd-dbg2、base-gsd-dbg6 的模板记录。以 `kab-` 开头的模板是内核 A/B 对照专用的（每个内核各建一到两个），不在此表。
验证时宿主上的产物名（`gsd-stack/bin/gsd-<提交号>`、`gsd-stack/bin/orchestrator-gsd-<提交号>`、`gsd-stack/fc-kernels-<代号>/`）与本书运行时布局的对照在 [20](20-results-and-gaps.md) 的条件标签表里。

### 5.6 结果目录与旧文档

结果目录在 `deltabox/e2b/results/` 下，旧文档在 `deltabox/e2b/docs/` 下，都不在代码仓库里。

| 名字 | 日期 | 是什么 |
|---|---|---|
| `gsd-920b-20260911` | 2026-09-11 起 | 旧 920B（已退役）上修复之前的微基准与压测 |
| `gsd-new920b-20260923` | 2026-09-23 | 新 920B 上修复之前的性能与 `clear_refs` 竞态对照数据 |
| `gsd-fixes-20260930` | 2026-09-30 | 第一版修复的现场验证（`SUMMARY.md`，注意文末"更正与后续"） |
| `gsd-fix3-20260930` | 2026-09-30 | 并发互斥与写入切换两项内核修复的验证；那 8.7 ms 的定位 |
| `gsd-fix4-20260930` | 2026-09-30 | 祖先链 `ESTALE`、搬走打开的文件、布局填充三项的验证 |
| `gsd-fix5-20261008` | 2026-10-08 | 去掉布局填充、回收、rename 竞争的验证 |
| `gsd-final-20261008` | 2026-10-08 | 最终版 gsd（多棵树等）的整体验证；发现 whiteout 缓存问题 |
| `gsd-fix6-20261008` | 2026-10-08 | whiteout 缓存、work 目录 trap、rmdir 竞争三项的修复与验证 |
| `gsd-release-20261008` | 2026-10-08 | 最终提交 `259f1a25c` 的重编、重建模板与确认性验证；本书判定性数字的来源 |
| `gsd-code-issues.md` | 2026-09-30 | 交接时的问题清单（36 条）原文 |
| `checkpoint-bench-comparison.md`、`gsd-restore-fix-plan.md`、`gsd-restore-report.md` | 2026-09 | restore 变慢的归因过程（[16](16-restore-slowness-case.md) 的材料） |
| `gsd-new920b-walkthrough.md`、`gsd-new920b-survey.md`、`RUNBOOK-950-gsd.md`、`gsd-merge-into-e2b-infra-plan.md` | 2026-09 | 修复之前的部署记录与融合方案草案。**只作历史材料，与代码冲突时以代码为准** |

三处被后一轮推翻的说法，读这些结果文件时以后一轮为准：`gsd-fix3-20260930` 对祖先链 `ESTALE` 触发条件的描述（由 `gsd-fix4-20260930` 更正）；
`gsd-fix4-20260930` 对 rename `ENOTEMPTY`"不是内核缺陷"的判断（由 `gsd-fix5-20261008` 更正）；
`gsd-final-20261008` 把四种错误归为"同一个条件"（由 `gsd-fix6-20261008` 更正为两个独立的缺陷）。
另外 `gsd-fixes-20260930` 正文里"内核影响不超过约 3 ms"的对照已由它自己文末的更正作废。

---

## 6. 28 个提交

`git log --reverse e47d09d04..259f1a25c`。每个提交处理了哪些问题见 [14](14-issue-resolution.md) 的提交表。

| 提交号 | 标题 |
|---|---|
| `73db6f055` | gsd: make checkpoint orchestration testable with injectable system ops |
| `326821e7e` | gsd: rewrite user process discovery (stat parsing, numeric roots, pre-dump states) |
| `21e388b8a` | gsd: kill whole user process trees on restore and drop the PID cache |
| `fdce8c8f2` | gsd: serialize checkpoint ops, bound criu runtime, clean up failed dumps |
| `7fe4d3f51` | gsd: force transparent hugepages off and stop provisioning distro criu |
| `f6af4a718` | gsd: make overlay sink transactional, reclaim via trash, short layer names |
| `aedd4cf84` | gsd: stop passing the just-sunk layer twice in ckpt-mode overlay switches |
| `8b8239f59` | gsd: keep incremental parents until children are gone, reclaim unreferenced layers |
| `5cd4a980b` | gsd: check before killing on restore, report half-done restores, degrade instead of silently skipping |
| `8cdeadc1f` | gsd: persist checkpoint state to snapshot store and reload it on restart |
| `328e4ad62` | gsd: authenticate RPCs with the sandbox access token from MMDS |
| `b1e36e23e` | orchestrator: make gsd an opt-in via HOST_GSD_PATH and fail builds when gsd is unhealthy |
| `7fd56c485` | gsd: refuse checkpoints that would not fit and record image sizes |
| `d4b876813` | gsd: return stage timings and checkpoint metadata to the SDK |
| `91c42ab94` | py-sdk: honour URL overrides for gsd, stop per-Checkpoint HTTP clients, drop Basic auth |
| `a9dc9f93f` | gsd: remove dead code and the overlay-setup scheme, stop making / world-writable |
| `ebc135677` | fc-kernels: pin the upstream base tag the patches are made against |
| `731611b66` | gsd: bring the READMEs and overlay-init comments in line with the code |
| `9c8035627` | gsd: make process discovery cheap again (cache envd MainPID, one /proc scan per op, skip kernel threads) |
| `31439d5d0` | gsd: carry the criu dump log tail in the error instead of pointing at a deleted file |
| `4742c4acb` | py-sdk: map gsd checkpoint errors on their own instead of through envd's defaults |
| `93b90a454` | orchestrator: lift systemd start limit on gsd.service |
| `823fe7b02` | orchestrator: inject gsd by default again, HOST_GSD_PATH=off to opt out |
| `92cfc2f29` | gsd: checkpoint and restore every user process tree, not just one |
| `fe453d270` | py-sdk: report the number of process trees in CheckpointInfo |
| `4c1ba0ef8` | orchestrator: keep /tmp on the overlay root so restore rolls it back |
| `9bd06b942` | orchestrator: stop making the guest's / world-writable in overlay-init |
| `259f1a25c` | fc-kernels: fold all our overlayfs hot-switch fixes into patch 0003 |

---

## 7. 不变量速查

改代码前过一遍；每条的论证在所链接的章，改动后的检查办法在附录 [B](B-extending.md)。

**gsd 侧**（[12](12-failure-semantics.md)）：

1. 从一棵树 dump 完到 `clear_refs` 写完，这棵树的进程必须一直停着。
2. restore 换层之前，当前的用户进程必须已经杀干净。
3. 父镜像必须活得比所有以它为父的镜像久。
4. 删除时先把状态文件写好，再删目录。
5. 任何一步失败之后，下一次 dump 必须是全量。
6. 杀进程之前的失败"什么都没动"；杀进程之后的失败一律是 `DataLoss`（唯一的例外：沙箱里本来就没有用户进程可杀、切层又失败时，沙箱仍是原样，按普通错误返回并带 `nothing was changed`，不进 degraded）。

这六条是速查；完整的十二条及各自"破坏后是否静默"在 [12](12-failure-semantics.md) 的"不变量清单"一节，附录 [B](B-extending.md) 的检查表与之逐条对应。

**内核侧**（[07](07-overlay-concurrency-and-fixes.md)）：

1. ioctl 里可能失败的步骤都在提交点之前。
2. 读任何后端状态都必须持 `switch_sem` 读锁；读锁不递归、不在 `mmap_lock` 下等待。
3. 被换下的对象延迟一个切换周期释放，涉及 RCU 路径的再等一个宽限期。
4. 属于 work 目录或某一层的缓存，必须随它一起更新；以真实 inode 地址为键的缓存，必须有东西钉住那个 inode。

---

## 8. 章目表

| 部分 | 章 |
|---|---|
| 导读 | [00](00-overview.md) 导读 |
| 一　基础 | [01](01-problem-and-scope.md) 进程级回滚：问题与定位 · [02](02-criu-and-incremental-memory.md) CRIU 与增量内存原理 · [03](03-goals-and-tradeoffs.md) 设计目标、约束与取舍 |
| 二　设计与实现 | [04](04-architecture.md) 总体架构、请求路径与鉴权 · [05](05-guest-boot-and-injection.md) guest 启动、根文件系统与模板注入 · [06](06-overlay-hot-switch.md) overlayfs 热切层：机制 · [07](07-overlay-concurrency-and-fixes.md) overlayfs 热切层：并发、生命周期与修复 · [08](08-process-trees.md) 用户进程树与多树 · [09](09-checkpoint.md) checkpoint 的实现 · [10](10-restore.md) restore 的实现 · [11](11-end-to-end.md) 端到端走查 |
| 三　工程保障 | [12](12-failure-semantics.md) 失败语义与 degraded · [13](13-state-concurrency-capacity.md) 状态、并发与容量 · [14](14-issue-resolution.md) 问题清单的处理结果 · [15](15-known-limits.md) 已知限制与未闭合项 |
| 四　性能 | [16](16-restore-slowness-case.md) restore 为什么慢：一次归因 · [17](17-performance-after-fixes.md) 修复之后的性能：回退、收回与代价模型 · [18](18-process-vs-machine-level.md) 进程级与整机级：分工与对照 |
| 五　验证与实测 | [19](19-testing-and-debug-kernel.md) 测试体系与调试内核 · [20](20-results-and-gaps.md) 实测结果与缺口 |
| 六　使用与运维 | [21](21-quickstart.md) 快速上手 · [22](22-semantics-and-limits.md) 语义与使用边界 · [23](23-deployment.md) 部署 · [24](24-configuration-and-observability.md) 配置与可观测性 · [25](25-troubleshooting.md) 排障 · [26](26-acceptance-runbook.md) 上机验收 |
| 附录 | A 术语、代码地图与文件格式（本篇）· [B](B-extending.md) 继续开发 |
