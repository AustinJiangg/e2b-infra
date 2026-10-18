# 24 · 配置与可观测性

## 本章目标

读完本章，你应该能：

1. 说清 gsd 这套栈的开关分布在哪四个地方、各自什么时候被读取、改了之后要重启什么或重建什么；
2. 在开关总表里查到任一开关的默认值、模板里的实际取值、取值规则和核对办法；
3. 读懂 `timings` 里的每个计时键：从哪一步计到哪一步、什么时候不出现、多棵树时怎么算；
4. 在 guest 里取到 gsd 的日志、CRIU 的日志和内核的相关输出，知道每条关键消息意味着什么；
5. 读懂 `/health` 的两种响应和 `state.json`，把它们当作排障时的第一手状态。

上一章（[23](23-deployment.md)）把部件装好、模板建好。本章讲装好之后**能调什么、能看什么**。本书**唯一的开关总表**和**唯一的计时键定义表**都在这里，别的章用到时只链接、不复述；
每个默认值都对着 `deltabox-gsd@259f1a25c` 的代码核对过。出了问题怎么用这些信息定位，在下一章（[25](25-troubleshooting.md)）。

---

## 1. 开关在哪里、怎么生效

gsd 这套栈的可调项分布在四个地方，生效方式各不相同。动手之前先分清要改的是哪一类：

| 位置 | 有哪些 | 什么时候读 | 改了之后 |
|---|---|---|---|
| **guest 里的 gsd** | 命令行参数与 `GSD_*` 环境变量（§2.1） | gsd 进程启动时 | 参数写在模板里的 `gsd.service` 中。要对所有沙箱生效：改 orchestrator 里的服务单元模板，重编 orchestrator，重建模板。只改一个正在运行的沙箱：在沙箱里加 systemd drop-in 后重启 gsd |
| **宿主的 orchestrator** | `HOST_GSD_PATH`、`HOST_KERNELS_DIR`（§2.2） | orchestrator 进程启动时读一次 | 改 job 的环境变量并重启 orchestrator（切栈脚本做的就是这件事，[23](23-deployment.md)）；只影响**之后建的模板** |
| **guest 内核** | 模块参数 `overlay.ckpt_move_open`（§2.3） | 每次 checkpoint 时读当前值 | 在沙箱里写 sysfs 立即生效，只影响这一个沙箱 |
| **客户端 SDK** | 连接参数与 `E2B_*` 环境变量（§2.4） | 创建或连接沙箱对象时 | 重新创建沙箱对象 |

贯穿全书的一条规则在这里尤其要紧：**模板固化了建模板时的 gsd、内核和服务单元**。沙箱是从模板快照恢复出来的，gsd 进程在快照里已经带着它的参数在跑。
所以前三类里凡是"建模板时定下来"的东西，换了之后不重建模板就不会生效；反过来，已有模板也不会因为宿主上的文件或环境变量变了而变。

gsd 在沙箱里重启是安全的：它每次操作结束都把状态写进状态文件，重启后加载（[13](13-state-concurrency-capacity.md)），已有的 checkpoint 不会丢。

---

## 2. 开关总表

### 2.1 gsd 的命令行参数与环境变量

gsd 的参数在 `parseFlags`（`packages/gsd/main.go:55`）里定义。除 `-port`、`-version`、`-commit` 外，每个参数都有一个同义的环境变量；**命令行优先于环境变量，环境变量优先于编译进去的默认值**。

| 参数 | 环境变量 | 编译进去的默认值 | 模板里的值 | 作用 |
|---|---|---|---|---|
| `-port` | 无 | `49984` | 不传，用默认 | 监听端口，地址固定是 `0.0.0.0` |
| `-overlay-mount` | `GSD_OVERLAY_MOUNT` | `/home/user-sandbox` | `/` | 要做 checkpoint 的 overlay 挂载点；切层的 ioctl 打在它上面 |
| `-base-layer` | `GSD_BASE_LAYER` | `/home/user-sandbox-data` | `/mnt/lower` | 最底下的只读层，即基础镜像的根文件系统 |
| `-layers-root` | `GSD_LAYERS_ROOT` | `/overlay/layers` | `/overlay/layers` | 放层目录、临时 upper/work 目录和层回收站的目录 |
| `-work-root` | `GSD_WORK_ROOT` | `/overlay/workspace` | `/overlay/workspace` | 开机时初始的 upper 与 work 所在目录（其下的 `upper`、`work`） |
| `-snapshot-store` | `GSD_SNAPSHOT_STORE` | `/var/lib/gsd/snapshots` | `/var/lib/gsd/snapshots` | 进程镜像和状态文件 `state.json` 的目录；模板里它是一个 2 GiB 的内存盘 |
| `-criu-dump-timeout` | `GSD_CRIU_DUMP_TIMEOUT` | `60s` | 不传，用默认 | **一棵**进程树的 CRIU 保存时限。到期先发 SIGTERM，2 秒后 SIGKILL。一次 create 里全部树合计另有 3 倍于它的上限 |
| `-criu-restore-timeout` | `GSD_CRIU_RESTORE_TIMEOUT` | `60s` | 不传，用默认 | 一棵进程树的 CRIU 恢复时限，规则同上。恢复超时发生在进程已被杀之后，返回的是 `data_loss` 而不是 `deadline_exceeded`（[12](12-failure-semantics.md)） |
| `-version` | 无 | — | — | 打印版本号（当前 `0.1.0`）后退出 |
| `-commit` | 无 | — | — | 打印构建时嵌入的提交号后退出 |

几点说明：

- **前两行的编译默认值不可用。** `/home/user-sandbox` 与 `/home/user-sandbox-data` 是更早一套方案留下的值，现在的 guest 里没有这两个目录。模板里的 `gsd.service` 总是显式传入 `/` 和 `/mnt/lower`，所以平时不会碰到；手工在 guest 里直接运行 `gsd` 而不带参数时，它会因为打不开挂载点而进入降级状态（`overlay manager init failed`）。
- **五个路径参数互相关联，并且要和启动脚本一致。** 启动脚本 `overlay-init` 用同一组值建目录、挂内存盘；两边的值都出自 orchestrator 里的同一处（`newTemplateModel`，`packages/orchestrator/internal/template/build/core/rootfs/templates.go:49`）。不要单独改其中一个。
- **时长的写法**是 Go 的格式，如 `90s`、`2m`。环境变量的值解析不了或不大于 0 时被忽略，gsd 在启动输出里打一行 `ignoring invalid GSD_CRIU_DUMP_TIMEOUT="…", using 1m0s` 并使用默认值；命令行传入 0 或负值同样回落到 60 秒。字符串类的环境变量设成空串等同于没设。
- **两个时限是 gsd 自己的**，与 SDK 的 `request_timeout` 无关（[21](21-quickstart.md)）。把它们调大时，客户端的 `request_timeout` 要跟着调大，否则 SDK 会先放弃等待。
- `gsd.service` 里还有几项固定设置，不是 gsd 的参数但影响它的行为：`Restart=always`（崩溃后自动拉起）、`StartLimitIntervalSec=0`（重启次数不设上限，因为建多层模板时每层都要重启一次 gsd）、`OOMScoreAdjust=-1000`（内存不足时内核不杀 gsd；这个值会被它拉起的 CRIU 继承，所以才需要容量预检，见 [13](13-state-concurrency-capacity.md)）。

在一个正在运行的沙箱里临时改时限，用 drop-in 设环境变量即可（因为 `ExecStart` 没有显式传这两个参数，环境变量会生效）。这个做法依据的是参数解析的代码，没有单独做过现场验证：

```bash
mkdir -p /etc/systemd/system/gsd.service.d
printf '[Service]\nEnvironment=GSD_CRIU_DUMP_TIMEOUT=120s\n' > /etc/systemd/system/gsd.service.d/timeout.conf
systemctl daemon-reload && systemctl restart gsd
```

### 2.2 orchestrator 的两个环境变量

定义在 `packages/orchestrator/internal/cfg/model.go:22`、`:23`，在 orchestrator 进程启动时读一次。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `HOST_GSD_PATH` | `/fc-gsd/gsd` | 建模板时注入 guest 的 gsd 二进制在宿主上的路径，同时也是"建模板时是否注入 gsd"的总开关 |
| `HOST_KERNELS_DIR` | `/fc-kernels` | guest 内核的根目录；内核文件是其下的 `vmlinux-<版本>/vmlinux.bin` |

`HOST_GSD_PATH` 的取值规则值得逐项列出，因为它既是路径又是开关。下表是 2026-10-08 在新 920B 上把配置解析的单测编成二进制、在真实进程环境变量下跑出来的结果：

| 环境 | 解析出的路径 | 是否注入 gsd |
|---|---|---|
| 不设 | `/fc-gsd/gsd` | 注入 |
| 设为空串 | `/fc-gsd/gsd`（空串等同于不设） | 注入 |
| `off` | 空 | **不注入**，建模板的行为与上游相同 |
| `/x/y/gsd` | `/x/y/gsd` | 注入 |
| `OFF`、`Off` | `<orchestrator 的当前目录>/OFF`（被当成相对路径） | 注入——并因为读不到这个文件而建模板失败 |
| `rel/gsd` | `<当前目录>/rel/gsd` | 注入 |

要点是三条：默认就是开的；**只有小写的 `off` 才是关**；开着而文件读不到时建模板直接失败（报 `error reading gsd file (HOST_GSD_PATH)`），不会悄悄建出一个不带 gsd 的模板。
这个开关一共控制建模板路径上的五处行为（往根文件系统加文件、换启动程序、逐层注入并自检、基础层缓存键、读宿主二进制），关掉后五处都走与上游相同的分支，详见 [05](05-guest-boot-and-injection.md)。
"关掉之后真实建出的模板与上游一致"这一点只有单测与读代码的依据，没有现场结果（[20](20-results-and-gaps.md) 的缺口表）。

核对一个运行中的 orchestrator 实际用的值，看它的进程环境，不要看配置文件（切栈脚本的 `status` 做的就是这件事）：

```bash
p=<orchestrator 的进程号>
tr '\0' '\n' < /proc/$p/environ | grep -E '^HOST_(GSD_PATH|KERNELS_DIR)=' || echo "两个都没设，用默认值"
```

### 2.3 内核模块参数 `overlay.ckpt_move_open`

由 `0003-overlayfs-hot-switch-fixes.patch` 加入，决定 checkpoint 时怎样对待"正被以写方式打开着"的 overlay 文件。背景是：这样的文件在 checkpoint 之后还会被原来的文件描述符继续写，如果不处理，这些写入会改到已经保存下来的那一层里（[07](07-overlay-concurrency-and-fixes.md)）。
处理办法是把原文件留在新的可写层、在保存下来的层里放一份拷贝，代价是拷贝的时间。

| 值 | 行为 |
|---|---|
| `0` | 从不搬。checkpoint 最快，但 checkpoint 之后经旧文件描述符的写入会改到 checkpoint 里 |
| `1` | 只搬同时被内存映射着的文件 |
| `2`（默认） | 凡是以写方式打开着的都搬 |

读写位置是 `/sys/module/overlay/parameters/ckpt_move_open`（权限 0644，overlay 编进了内核，所以也可以在内核命令行上写 `overlay.ckpt_move_open=<值>`）。在沙箱里写 sysfs 立即生效，只影响这一个沙箱、之后的 checkpoint：

```bash
cat /sys/module/overlay/parameters/ckpt_move_open
echo 1 > /sys/module/overlay/parameters/ckpt_move_open
```

默认值 2 是正确性优先的选择。只有在确认沙箱里长期以写方式打开的大文件不需要被 checkpoint 精确保存时，才考虑调低；代价的量级见 [22](22-semantics-and-limits.md) 与 [20](20-results-and-gaps.md)。无论取哪个值，有多个硬链接的文件都不搬，内核会打一条限速告警（§4.3）。

### 2.4 SDK 的连接参数与环境变量

SDK 一侧与 gsd 有关的配置都在 `py-sdk/e2b/connection_config.py`。每一项既可以作为连接参数传给 `Sandbox.create()` / `Sandbox.connect()`，也可以用环境变量给默认值；参数优先。

| 连接参数 | 环境变量 | 默认 | 作用 |
|---|---|---|---|
| `checkpointd_url` | `E2B_CHECKPOINTD_URL` | 无 | gsd 的地址。给了就原样使用，优先级最高 |
| `sandbox_url` | `E2B_SANDBOX_URL` | 无 | 沙箱（envd）的地址。没有 `checkpointd_url` 时，gsd 的请求也**原样**发到这里，靠请求头 `E2b-Sandbox-Port: 49984` 让代理转到 gsd。只对按请求头路由的代理有效：本仓库自带的宿主代理仅在本地开发环境下采用 `E2b-Sandbox-Id` / `E2b-Sandbox-Port`，否则按 `Host` 解析（[04](04-architecture.md)） |
| `domain` | `E2B_DOMAIN` | `e2b.app` | 前两项都没有时，gsd 的地址是 `http://49984-<沙箱 ID>.<域名>` |
| `debug` | `E2B_DEBUG` | `false` | 为 `true` 时沙箱地址变成 `localhost:<端口>`，gsd 即 `http://localhost:49984` |
| 无 | `E2B_HTTP_SSL` | `true` | 设为 `false` 时关闭证书校验。它不改变协议：这个分支生成的沙箱地址固定是 `http://` |
| `request_timeout` | 无 | 60 秒 | SDK 等一次响应的时间；`0` 表示不限。每个 checkpoint 方法也可以单独传 |

访问令牌不是开关：secure 沙箱（`Sandbox.create()` 的默认）的 `X-Access-Token` 由 SDK 自动带上，gsd 每次请求都对着沙箱元数据里的哈希校验（[04](04-architecture.md)）。

### 2.5 写死的值

下面这些值在代码里是常量，没有开关。列在这里是为了排障时知道它们存在；要改只能改代码。

| 项 | 值 | 位置 |
|---|---|---|
| CRIU 的路径 | `/usr/sbin/criu` | `ckpt/criu.go:20` |
| CRIU 保存的日志级别 | `-v4`（恢复用默认级别） | `buildCriuDumpCmd`（`ckpt/criu.go:51`） |
| "用户进程"的 uid | 1000 | `ckpt/pid.go:17` |
| 一次操作里全部树的 CRIU 合计时限 | 单棵时限的 3 倍 | `ckpt/service.go:33` |
| CRIU 超时后从 SIGTERM 到 SIGKILL 的宽限 | 2 秒 | `ckpt/criu.go:23` |
| restore 杀进程后等它们退出的总时限 | 3 秒 | `ckpt/criu.go:184` |
| 杀进程前"扫描并冻结"的最多轮数 | 10 | `ckpt/criu.go:188` |
| 容量预检的系数 | 1.2 | `ckpt/capacity.go:34` |
| 切层 ioctl 选项串的长度上限 | 4096 字节（一页） | `ckpt/overlay.go:31` |
| 进程镜像内存盘的大小 | 2 GiB | `overlay-init.sh.tpl` 的 Step 7 |
| 失败时带回的 CRIU 日志尾部长度 | 最多 2048 字节 | `ckpt/criu.go:141` |
| 读沙箱元数据（校验令牌）的时限与次数 | 每次 2 秒，最多 3 次，间隔 50 ms | `packages/gsd/internal/permissions/mmds.go:21` |
| 被排除在回滚之外的服务 | PID 1、gsd 自己、`envd.service` 的主进程 | `ckpt/pid.go:220` |
| gsd 在 guest 里的路径 | `/usr/bin/gsd` | `packages/shared/pkg/storage/template.go:9` |
| 状态文件 | `<snapshot-store>/state.json`，格式版本 2 | `ckpt/state.go:32` |

---

## 3. 计时键

### 3.1 定义

gsd 对每次 create 和 restore 分段计时，结果放在响应的 `Timings` 消息里（`packages/gsd/spec/checkpoint/checkpoint.proto` 第 17 行起），单位都是毫秒、带小数，由 gsd 在 guest 里用自己的时钟量出。
SDK 把 create 的结果放进 `CheckpointInfo.timings`，把最近一次成功的 create 或 restore 的结果放进 `sandbox.checkpoint.last_timings`（[21](21-quickstart.md)）。

| 键 | create | restore | 从哪里计到哪里 |
|---|---|---|---|
| `total_ms` | 有 | 有 | 从 gsd 拿到操作锁，到组装响应之前。**不含**在锁上排队的时间，不含网络往返和鉴权，也不含响应组好之后写 `state.json` 的时间 |
| `find_pids_ms` | 有 | 有 | 扫描一遍 `/proc` 找出用户进程。create 里还包括找出各棵树的根、记下每个进程保存前的运行状态 |
| `precheck_ms` | 有进程时 | 有 | 改动任何东西之前的检查。create：树间共享管道与 socket 的检查，加上镜像大小的估算与比较。restore：检查目标的每棵树镜像可读、父镜像链完整，并建好切层要用的新目录 |
| `criu_dump_ms` | 有进程时 | — | CRIU 保存的耗时，**各棵树之和** |
| `overlay_sink_ms` | 有 | — | 把当前可写层变成只读层：一次改名加一次 ioctl。两次 checkpoint 之间没写过文件时不切层，这一项很小 |
| `stopped_ms` | 有进程时 | — | 用户进程被留在停止状态的时长，**从第一棵树保存完算起**，到全部进程被放行为止。这段窗口里发生的是：其余各棵树的保存、切层、重置脏页标记 |
| `kill_ms` | — | 有进程可杀时 | 杀掉当前全部用户进程：冻结、发 SIGKILL、等它们退出 |
| `overlay_restore_ms` | — | 有 | 把文件系统切到目标 checkpoint 的层栈：一次 ioctl |
| `criu_restore_ms` | — | 目标有进程时 | CRIU 恢复的耗时，**各棵树之和** |

读这张表时注意几点：

- **没发生的阶段不出现。** 值为 0 的字段不会被发送，SDK 的字典里就没有这个键。例如没有用户进程时的 create 没有 `criu_dump_ms`、`precheck_ms`、`stopped_ms`；目标只有文件系统的 restore 没有 `criu_restore_ms`。读的时候用 `.get()`。
- **`Timings` 里还有一个 `process_trees`**，是这次保存或恢复的树的数量，不是耗时。SDK 不把它放进 `timings` 字典（字典里只有以 `_ms` 结尾的键），而是放在 `CheckpointInfo.process_trees`。
- **各项加起来不等于 `total_ms`。** 总时间里还有建目录、量镜像大小、写状态文件之前的收尾等没有单独计的部分；`stopped_ms` 与 `overlay_sink_ms` 在时间上是重叠的（切层发生在停止窗口里）。
- **`stopped_ms` 是用户进程感受到的停顿的下界**，不是全部：每棵树在自己被保存期间也是冻住的，那一段算在 `criu_dump_ms` 里。一棵树时，进程实际不运行的时间大致是 `criu_dump_ms + stopped_ms`；多棵树时第一棵停得最久，`stopped_ms` 约等于其余各棵的保存时间之和再加切层的时间。
- **多棵树的耗时线性增长。** 保存与恢复都是逐棵串行的。

每个键在一次请求的时间线上落在哪里，见 [11](11-end-to-end.md)；各项的实测值与量级见 [20](20-results-and-gaps.md)。

### 3.2 日志里多出来的细分

同样的数字也打在 gsd 的日志里，字段名与计时键相同（`precheck_ms` 除外，它只在响应里）。日志里另有响应中没有的细分：

**每棵树各自的耗时。** 响应里的 `criu_dump_ms`、`criu_restore_ms` 是合计；日志里每棵树一行：`criu dump succeeded` 和 `user process tree restored`，各带字段 `tree`（形如 `t1 (root pid 1203, 2 of 3)`）和这棵树自己的 `criu_dump_ms` 或 `criu_restore_ms`。
哪棵树是增量也只在日志里：增量的树在保存前有一行 `using incremental CRIU dump`，带 `prev_mem`（父镜像目录）和 `parent_id`。每棵树镜像的大小和父节点则在 `state.json` 里（§6）。

**杀进程的细分。** restore 的 `user processes killed` 一行，除了 `kill_ms` 还有：

| 字段 | 含义 |
|---|---|
| `freeze_rounds` | "扫描并冻结新出现的进程"做了几轮，含最后那轮确认没有新进程的扫描。正常是 2 |
| `proc_scans` | 其中实际重新扫描 `/proc` 的次数。第一轮直接用 restore 开头那次扫描的结果，所以通常比 `freeze_rounds` 少 1 |
| `scan_ms` | 这些扫描的合计耗时 |
| `signal_ms` | 发 SIGSTOP 与 SIGKILL 的合计耗时 |
| `wait_exit_ms` | 等被杀的进程退出并让出进程号的耗时。`kill_ms` 的大头通常在这里 |
| `killed_pids` | 实际被发了信号的进程号 |

**整次请求的耗时。** 每个 RPC 结束时有一行 `rpc completed`，带 `procedure`（如 `/checkpoint.Checkpoint/CreateCheckpoint`）和 `duration`。它从 gsd 的处理函数被调用算起，**包含在操作锁上排队的时间**，所以 `duration` 明显大于 `total_ms` 时，说明这个请求排了队。失败的请求这一行是 error 级别，带 `error` 字段。

---

## 4. 日志

### 4.1 gsd 的日志

gsd 把结构化日志以每行一个 JSON 对象的形式写到标准输出，由 systemd 收进 journal。每行有 `timestamp`（RFC 3339，纳秒精度）、`level`、`logger`（固定为 `gsd`）、`message`，以及随消息而定的字段；时长类字段的单位是毫秒。在 guest 里以 root 身份取：

```bash
journalctl -u gsd --no-pager -o cat | grep '^{' | tail -n 50
journalctl -u gsd --no-pager -o cat | grep -F '"checkpoint_id":"ckpt_1759928000123456789"'
journalctl -u gsd --no-pager -o cat | grep -E '"level":"(warn|error)"'
```

`grep '^{'` 是为了滤掉混在同一个 journal 里的 CRIU 输出（§4.2）。从宿主上取，就通过 SDK 以 root 身份在沙箱里跑这些命令（[25](25-troubleshooting.md) 开头有现成的写法）。

下面是值得认识的消息。左列是 `message` 的原文，可以直接拿去 grep。

**启动**

| 消息 | 级别 | 含义 |
|---|---|---|
| `expedited RCU enabled` | info | 已打开加速的 RCU 宽限期，restore 的抖动因此消除（[16](16-restore-slowness-case.md)） |
| `cannot enable expedited RCU; cgroup migration in criu restore will keep its 20-50ms jitter` | warn | 上一项没成功；功能不受影响，restore 会慢且不稳 |
| `transparent_hugepage is never` | info | 透明大页是关的，正常 |
| `transparent_hugepage was not never (overlay-init did not set it), switched to never; processes started before this may still hold huge pages` | warn | 启动脚本没关掉透明大页，gsd 兜底关了；此前起的进程的增量 checkpoint 可能仍然偏大 |
| `transparent_hugepage is not never and cannot be changed; incremental checkpoints will bloat` | error | 关不掉；增量会膨胀成接近全量（[02](02-criu-and-incremental-memory.md)） |
| `no persisted state, starting fresh` | info | 没有状态文件：这是开机后的第一次启动 |
| `persisted state loaded` | info | gsd 重启后加载了状态；字段 `checkpoints`、`layers`、`current_upper`、`restore_failure` |
| `checkpoint service degraded; create and restore are disabled` | error | 进入致命降级，字段 `reason` 是原因（§5） |
| `gsd starting` | info | 开始监听；字段 `port`、`overlay_mount`、`snapshot_store` |

**create**（按出现顺序）

| 消息 | 级别 | 含义与字段 |
|---|---|---|
| `checkpoint size precheck passed` | info | 容量预检通过；`estimate` 是估算与两个余量的文字描述 |
| `starting checkpoint` | info | `dump_root_pids` 是各棵树的根，`find_pids_ms` |
| `using incremental CRIU dump` | info | 这棵树做增量；`tree`、`prev_mem`、`parent_id` |
| `criu dump succeeded` | info | 一棵树保存完；`tree`、`criu_dump_ms` |
| `overlay sink succeeded` | info | 切层完成；`layers`（层栈深度）、`overlay_sink_ms` |
| `user process trees resumed` | info | 进程已放行；`stopped_pids`、`resumed_pids`、`stopped_ms`。两个列表不同，说明有进程在保存前就是停着的，没有被放开 |
| `checkpoint created` | info | `process_trees`、`image_bytes`、`total_ms` |
| `no user processes, overlay-only checkpoint` | info | 没有用户进程，只保存文件系统 |
| `user process trees share a pipe or socket, refusing checkpoint` | warn | 被树间共享检查拒绝；`shared` 是具体哪条管道或哪个 socket |
| `not enough room for checkpoint images, refusing` | warn | 被容量预检拒绝；`estimate`、`store_short`、`mem_short` 说明是哪一项不够 |
| `criu dump failed` | error | 某棵树保存失败；`tree`、`mem_dir` |
| `resumed processes left stopped by failed dump` | warn | 保存失败后，gsd 把被留在停止状态的进程放开了；`resumed_pids` |
| `overlay sink failed` | error | 切层失败；这次的镜像已丢弃 |
| `clear_refs failed; next incremental dump of this process will be larger` | warn | 某个进程的脏页标记没重置成功；`pid` |

**restore**（按出现顺序）

| 消息 | 级别 | 含义与字段 |
|---|---|---|
| `starting restore` | info | `checkpoint_id`、`process_trees` |
| `found user PIDs for kill` | info | `user_pids`、`find_pids_ms` |
| `user processes killed` | info | 见 §3.2 |
| `overlay layers restored` | info | `overlay_restore_ms` |
| `user process tree restored` | info | 一棵树恢复完；`tree`、`restored_pid`、`criu_restore_ms` |
| `no CRIU images, overlay-only restore` | info | 目标没有进程镜像 |
| `restore completed` | info | `total_ms` |
| `checkpoint images unusable` | error | 杀进程之前发现镜像不可用；什么都没动 |
| `overlay restore preparation failed` | error | 杀进程之前准备切层失败；什么都没动 |
| `failed to kill some user processes` | warn | 有进程没杀成功，restore 继续往下走 |
| `criu restore failed` | error | 某棵树恢复失败；`tree`、`log`（这棵树的恢复日志路径） |
| `restore failed half way, service degraded until a restore succeeds` | error | 杀进程之后失败；`fs_state` 是 `old` 或 `target` |
| `restore succeeded, leaving degraded state` | info | 一次成功的 restore 解除了上一条造成的降级 |

**删除、状态与鉴权**

| 消息 | 级别 | 含义 |
|---|---|---|
| `checkpoint deleted` | info | `removed` 是这次真正删掉的 checkpoint（可能带上此前被隐藏的祖先） |
| `checkpoint hidden; images kept until its incremental children are deleted` | info | 还有增量子节点，只是隐藏 |
| `cannot persist state; create and restore are disabled until it can be written` | error | 状态文件写失败，进入降级；`path` |
| `state persisted again, leaving degraded state` | info | 重试写入成功，解除 |
| `unreferenced checkpoint images moved to trash` | info | 回收了没人引用的镜像目录；`dirs` |
| `cannot read access token hash from MMDS, rejecting request` | error | 读不到沙箱元数据，请求被拒（`unavailable`） |
| `request with invalid access token` | warn | 令牌不对；`procedure`、`client` |
| `rpc completed` | info / error | 每个 RPC 一行，见 §3.2 |

### 4.2 CRIU 的日志

CRIU 的输出有两个去处，内容相同。

**镜像目录里的日志文件。** 每棵树的镜像目录是 `<snapshot-store>/<checkpoint ID>_mem/t<序号>/`，其中：

- `criu-dump.log`：这棵树保存时的输出，`-v4` 级别，很详细。保存成功时它留在镜像目录里，占内存盘的空间，随 checkpoint 一起删除。**保存失败时整个镜像目录连同它立即被删**，所以失败之后到这里找不到它——它的最后 2048 字节被带进了返回给调用方的错误信息里，完整内容在 journal 里。
- `criu-restore.log`：这棵树最近一次恢复时的输出，每次恢复覆盖。恢复失败时文件留着，尾部同样带进错误信息；路径在 `criu restore failed` 那行日志的 `log` 字段里。

**gsd 的 journal。** CRIU 的标准输出与标准错误同时接在 gsd 的标准错误上，所以全部内容也进了 `journalctl -u gsd`，是不带 JSON 外壳的原始文本行。看一次失败的完整 CRIU 输出：

```bash
journalctl -u gsd --no-pager -o cat | grep -v '^{' | grep -n -i -E 'error|warn|fail' | tail -n 40
```

这带来两个副作用，都在 §4.4 里说。

### 4.3 内核的输出

guest 内核的输出用 `dmesg` 看。与 gsd 有关的有三类。

**启动脚本的输出。** `overlay-init` 把自己的输出写进内核日志，每行以 `[overlay-init]` 开头，记录了开机时每一步挂载的结果；沙箱是从快照恢复的，这些行是建模板那次开机留下的。正常时能看到 `overlay mounted at /mnt/newroot, / is 755 root:root`、`transparent_hugepage=never`、`/tmp on overlay: 1777 root:root, tmp.mount -> /dev/null`，不应该有以 `WARNING:` 开头的行。

**热切层补丁的告警。** 修复之后，补丁在正常运行中只可能打出两条限速的告警：

| 输出（节选） | 含义 |
|---|---|
| `checkpoint: could not move open file … to the new upper layer (…); writes to it change the checkpoint` | checkpoint 时有一个以写方式打开的文件没能留在新的可写层（最常见的原因是它有多个硬链接）。这次 checkpoint 仍然成功，但之后经旧文件描述符对它的写入会改到 checkpoint 里（§2.3） |
| `checkpoint: not supported with an index dir (index=on or nfs_export=on)` | overlay 是带 index 目录挂载的，切层被内核拒绝。按本书的启动脚本挂出来的 overlay 不会遇到 |

此外有几处只打一次的内部一致性检查，例如 `overlayfs: layer … of an unknown layer array`。它们出现意味着内核里的状态与预期不符，属于缺陷，应当连同完整的 `dmesg` 上报（压测的关键词检查会抓这一类，见 [19](19-testing-and-debug-kernel.md)）。

交接前的补丁里有大量调试输出（每次 ioctl、每次查找都打一行），已全部删除。原始补丁文件 `0002-…` 本身没有改动，其输出是在 `0003-…` 里去掉的；如果在某个 guest 的 `dmesg` 里看到成批的 `ovl_ioctl_checkpoint:`、`ovl_lookup:` 之类的行，说明这个模板固化的是修复之前的内核，应当重建模板。本书没有逐行清点过修复后内核在所有路径上的输出，是否还有个别残留以现场的 `dmesg` 为准。

**内核自己的故障报告。** `BUG`、`Oops`、`KASAN`、`soft lockup` 之类。出现任何一条都是严重问题；guest 已经失去响应、跑不了 `dmesg` 时，从宿主取串口输出（[25](25-troubleshooting.md)）。

### 4.4 取日志时要知道的三件事

**journal 不随 restore 回滚。** journal 文件在 `/var/log` 下，而启动脚本把 `/var/log` 从底层磁盘单独挂出来，不在 overlay 上（`overlay-init.sh.tpl` 的 Step 4）。所以 restore 之后 journal 是完整的，能看到 restore 之前、包括被丢弃的那段时间里的全部日志。
2026-10-09 在新 920B 上做过现场确认：guest 里 `/var/log` 是来自 `/dev/root` 的独立 ext4 挂载，journald 的存储在 `/var/log/journal`；checkpoint 之后写入的 journal 条目和在 `/var/log` 下新建的文件，restore 之后都还在，而同时在 `/home/user` 下新建的对照文件被回滚掉了。那次用的是没有用户进程的 checkpoint，带进程镜像的 restore 没有单独测（`/var/log` 是独立挂载，按理不受影响）。随手册附带的 `verify_clear_refs_race` 脚本在开头把 journald 改成了只写内存（`Storage=volatile`），目的是不让长时间的测试把根盘写满，不要据此认为 journal 会回滚。

**journal 会持续增长。** 每次 create 都把 `-v4` 级别的 CRIU 输出写进 journal。新 920B 上 2026-10-08 的循环测试里，journal 每轮增长约 0.1 到 0.7 MB，直到 journald 自己的容量上限为止；`/var/log` 与文件系统的各层在同一块根盘上，所以这部分增长会体现在根盘占用里，容易被误认为是层没有回收。
长时间高频做 checkpoint 的沙箱要么给 journald 设一个明确的上限，要么定期清理（[25](25-troubleshooting.md)）。

**journald 的限流会丢 gsd 的日志。** journald 默认对每个服务按时间窗口限流。CRIU 的 `-v4` 输出量很大，连续做十来次 checkpoint 就可能触发限流，这时被丢掉的不只是 CRIU 的行，gsd 自己的 JSON 日志也会一起丢。
症状是 gsd 的日志在某个时刻之后变得残缺，同时 `journalctl -u systemd-journald` 里有 `Suppressed` 字样的行。需要完整日志时（验收、排障、压测），先给 gsd 关掉按单元的限流再开始：

```bash
mkdir -p /etc/systemd/system/gsd.service.d
printf '[Service]\nLogRateLimitIntervalSec=0\n' > /etc/systemd/system/gsd.service.d/norl.conf
systemctl daemon-reload && systemctl restart gsd
```

---

## 5. `/health`

`GET /health` 不需要访问令牌（建模板时的自检和探活都没有令牌），**始终返回 HTTP 200**，状态在响应体里：

```json
{"status":"ok"}
```

```json
{"reason":"restore of ckpt_1759928000123456789 failed after user processes were killed; filesystem at target state","status":"degraded"}
```

（降级时两个键按字母序输出，`reason` 在前；判断状态时匹配 `"status":"ok"` 或解析 JSON，不要依赖键的顺序。）

始终回 200 是有意的：降级只表示 create / restore 暂时不可用，gsd 进程本身活着、能应答，不应该让探活把它当成挂了去重启——重启解决不了其中两类降级，反而会丢掉现场。
因此 SDK 的 `is_running()` 在降级时仍然返回 `True`；要知道是否降级，必须读响应体：

```bash
curl -s http://127.0.0.1:49984/health        # 在 guest 里
```

`reason` 的开头指明是三类降级中的哪一类；同时存在多类时用分号加空格连接。

| `reason` 的开头 | 类别 | 被拒绝的操作 | 怎样解除 |
|---|---|---|---|
| `overlay manager init failed:`、`no state file … but initial upper is unusable:`、`cannot read state file:`、`cannot parse state file …`、`state file … has version …`、`invalid checkpoints in state file …`、`state file … does not match the disk:` | 致命：gsd 启动时发现自己的状态不可信 | create 与 restore 都拒绝 | 只有重启 gsd 才会重新尝试加载；原因不消除则重启后仍然降级 |
| `restore of <ID> failed after user processes were killed; filesystem at old state` 或 `… at target state` | restore 半途失败 | 只拒绝 create | 一次成功的 restore。这个原因写在状态文件里，gsd 重启后仍然保留 |
| `cannot persist state to <路径>:` | 状态文件写失败 | create 与 restore 开头先重试写入，仍失败则拒绝；delete 照常 | 下一次写入成功即自动解除 |

各类降级的语义与设计理由在 [12](12-failure-semantics.md)，处置步骤在 [25](25-troubleshooting.md)。

---

## 6. `state.json`

gsd 的全部状态都在 `<snapshot-store>/state.json`（模板里是 `/var/lib/gsd/snapshots/state.json`），每次 create、restore、delete 结束时原子地重写，gsd 重启时加载。它是带缩进的 JSON，可以直接读，是排障时最可靠的第一手状态——比 `list()` 的结果多出层栈、每棵树的明细和被隐藏的 checkpoint。

```bash
python3 -m json.tool < /var/lib/gsd/snapshots/state.json | head -n 60
```

文件不存在是正常的：说明这个沙箱开机以来 gsd 还没有做过任何改变状态的操作。下面是一个做过两次 checkpoint 的沙箱的示意（数值是编的，用来说明结构）：

```json
{
  "version": 2,
  "overlay": {
    "current_upper": "/overlay/layers/upper_ckpt_1759928000500000000_next",
    "current_work": "/overlay/layers/work_ckpt_1759928000500000000_next",
    "layers": ["/overlay/layers/L2", "/overlay/layers/L1", "/mnt/lower"],
    "layer_seq": 2,
    "restore_seq": 0
  },
  "last_trees": [
    {"root_pid": 812, "root_start": 5321, "checkpoint_id": "ckpt_1759928000500000000",
     "dir": "/var/lib/gsd/snapshots/ckpt_1759928000500000000_mem/t0"}
  ],
  "checkpoints": [
    {"id": "ckpt_1759928000100000000", "name": "v1",
     "mem_path": "/var/lib/gsd/snapshots/ckpt_1759928000100000000_mem",
     "layers": ["/overlay/layers/L1", "/mnt/lower"],
     "created_at": 1759928000100000000,
     "trees": [{"root_pid": 812, "root_start": 5321, "dir": "t0", "image_bytes": 201654272}],
     "had_processes": true, "image_bytes": 201654272},
    {"id": "ckpt_1759928000500000000", "name": "v2",
     "mem_path": "/var/lib/gsd/snapshots/ckpt_1759928000500000000_mem",
     "layers": ["/overlay/layers/L2", "/overlay/layers/L1", "/mnt/lower"],
     "created_at": 1759928000500000000,
     "trees": [{"root_pid": 812, "root_start": 5321, "dir": "t0",
                "parent_id": "ckpt_1759928000100000000", "image_bytes": 1105920}],
     "parent_id": "ckpt_1759928000100000000", "had_processes": true, "image_bytes": 1105920}
  ]
}
```

| 字段 | 含义 | 排障时怎么用 |
|---|---|---|
| `version` | 文件格式版本，当前是 2 | — |
| `overlay.current_upper` / `current_work` | 当前的可写层和它的工作目录 | 这两个目录必须存在；`/proc/mounts` 里显示的始终是开机时的那一对，不能用来判断当前状态，要看这里 |
| `overlay.layers` | 当前的只读层栈，**最上层在前**，最后一项是基础层 | 长度就是当前这条链的深度（[22](22-semantics-and-limits.md) 的层栈上限） |
| `overlay.layer_seq` / `restore_seq` | 层名 `L<n>` 和 restore 临时目录用到的序号，只增不减 | `restore_seq` 随每一次进入准备阶段的 restore 加一，可以当作开机以来 restore 的次数来看 |
| `last_trees` | 下一次 create 时，哪些树可以做增量、各自以哪个镜像目录为父。按根进程的进程号与启动时间认定"同一棵树" | 为空表示下一次全部存全量；"增量忽然变成全量"时先看这里（[25](25-troubleshooting.md)） |
| `restore_failure` | 上一次半途失败的 restore 留下的原因；正常时没有这个字段 | 有值即处于"只能先 restore"的降级 |
| `checkpoints[]` | 全部 checkpoint，按创建时间从旧到新，**包括已被隐藏的** | `list()` 看不到的隐藏项在这里能看到 |
| `checkpoints[].layers` | 这个 checkpoint 对应的层栈，restore 时原样切过去 | 这里引用的层目录都必须存在 |
| `checkpoints[].trees[]` | 每棵树：保存时根进程的进程号与启动时间、镜像子目录、增量的父 checkpoint、镜像大小 | 没有 `parent_id` 的树是全量 |
| `checkpoints[].deleted` | 为 `true` 表示已被 delete 但因为还有增量子节点而保留着镜像 | 解释"删了但内存没释放" |
| `checkpoints[].parent_id` / `had_processes` / `image_bytes` | 由各棵树汇总出来的值，写出来方便人看 | — |

与状态文件相关的目录都在两个根下面，直接 `ls` 就能和文件对照：`/var/lib/gsd/snapshots/` 下是各个 `<ID>_mem/` 镜像目录、`state.json` 和回收站 `.trash/`；`/overlay/layers/` 下是层目录 `L<n>`、当前和临时的 upper/work 目录、回收站 `.trash/`。目录布局的完整说明在 [04](04-architecture.md)。

不要手工编辑这个文件，也不要手工删这两个根下面的目录：gsd 启动时会校验文件里记录的目录都存在，对不上就进入致命降级并且不再覆盖这个文件（留作排查）。状态文件的写入时机、原子性与加载校验见 [13](13-state-concurrency-capacity.md)。

---

## 代码位置

| 内容 | 位置 |
|---|---|
| gsd 的参数、环境变量、默认值 | `parseFlags`、`envOr`、`envDurationOr`（`packages/gsd/main.go:55`、`:69`、`:76`），默认值常量（`:25` 起） |
| 模板里 gsd 的启动参数与服务设置 | `packages/orchestrator/internal/template/build/core/rootfs/gsdfiles/gsd.service.tpl`；取值来源 `newTemplateModel`（`…/rootfs/templates.go:49`） |
| `HOST_GSD_PATH`、`HOST_KERNELS_DIR` | `packages/orchestrator/internal/cfg/model.go:22`、`:23`；`GSDEnabled`（`:39`） |
| `overlay.ckpt_move_open` | 补丁 `0003-overlayfs-hot-switch-fixes.patch` 的 `module_param_named(ckpt_move_open, …)` 与 `ovl_ckpt_want_move` |
| SDK 的连接配置 | `py-sdk/e2b/connection_config.py`（`get_checkpointd_url` 在 `:168`） |
| 计时键的定义 | `packages/gsd/spec/checkpoint/checkpoint.proto` 的 `Timings`；SDK 侧 `timings_dict`（`py-sdk/e2b/sandbox/checkpoint/types.py:62`） |
| 各计时键的赋值点与日志字段 | `CreateCheckpoint`（`ckpt/service.go:163`）、`RestoreCheckpoint`（`ckpt/service.go:463`）；杀进程的细分 `killStats`（`ckpt/criu.go:197`） |
| 每个 RPC 的 `rpc completed` | `packages/gsd/internal/logs/interceptor.go:24` |
| 日志格式 | `NewLogger`（`packages/gsd/internal/logs/logger.go`） |
| CRIU 日志文件名与尾部截取 | `criuDumpLogName`、`logTail`（`ckpt/criu.go:71`、`:144`） |
| `/health` | `GetHealth`（`packages/gsd/internal/api/store.go:23`）；原因的拼接 `DegradedReason`（`ckpt/service.go:661`）；免鉴权 `withOpenHealth`（`packages/gsd/main.go:103`） |
| 状态文件的结构、写入与加载 | `persistedState` 等（`ckpt/state.go:39` 起）、`persist`（`:256`）、`loadState`（`:341`） |
| 启动时的 RCU 与透明大页检查 | `enableExpeditedRCU`、`ensureTHPNever`（`packages/gsd/main.go:178`、`:196`） |

---

## 本章要点

- 开关分四处：guest 里 gsd 的参数、宿主 orchestrator 的两个环境变量、guest 内核的一个模块参数、客户端 SDK 的连接配置。前两处的值在建模板时固化进模板，改了之后要重建模板才对新沙箱生效。
- gsd 的五个路径参数在模板里总是显式传入并与启动脚本一致，编译进去的前两个默认值不可用；两个 CRIU 时限默认各 60 秒、针对单棵树，全部树合计 3 倍。
- `HOST_GSD_PATH` 默认 `/fc-gsd/gsd`、默认注入；只有小写 `off` 才关闭注入；开着而文件读不到时建模板失败。
- `overlay.ckpt_move_open` 默认 2：checkpoint 时为以写方式打开的文件留拷贝，保证之后的写入不改到 checkpoint；可在沙箱里写 sysfs 调低，以正确性换时间。
- 计时键都是 gsd 自己计的毫秒数：`total_ms` 不含排队与网络；`criu_dump_ms`、`criu_restore_ms` 是各棵树之和；`stopped_ms` 从第一棵树保存完算起；没发生的阶段不出现。每棵树的明细和杀进程的细分只在日志里。
- gsd 的 JSON 日志与 CRIU 的原始输出都在 `journalctl -u gsd` 里；保存失败时镜像目录里的 `criu-dump.log` 已被删除，看错误信息里的尾部或 journal。
- journal 在单独挂出的 `/var/log` 上，不随 restore 回滚（2026-10-09 现场确认）；它会因 CRIU 的详细输出持续增长，journald 的限流还可能丢掉 gsd 的日志，需要完整日志时先关限流。
- `/health` 始终是 HTTP 200，是否降级、哪一类降级看响应体的 `reason`；`state.json` 是排障的第一手状态，能看到层栈、每棵树的增量关系和被隐藏的 checkpoint，不要手工改它。
