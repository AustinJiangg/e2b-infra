# FC 启动优化 · `netns-exec`（openEuler 官方实现）

> **版本基准：e2b-infra `2026.09` tag + `0001-adapted-for-arm-architecture.patch`。**
> 本优化全部源码都在 `0001-adapted-for-arm-architecture.patch` 里（仓库不直接跟踪打过补丁的 Go 源码树），
> 文中引用的文件路径均指补丁应用后的路径。开关配置见 `single-node-offline-deploy.md` 第 9 节；
> 各阶段耗时数据口径见 `启动耗时阶段分析.md`。
>
> **实现归属**：这是 openEuler 官方在 e2b-infra ARM 移植中做的优化，本仓库按**与官方等价、且逐字节一致**
> 的形式收录（`cmd/fc-netns-exec/main.go`、`internal/cfg/model.go`、`internal/sandbox/fc/script_builder.go`
> 及其单测四个文件与官方源码 `cmp` 无差异）。唯一一处有意偏离官方的是 `socket/socket.go`
> 的一个 ctx bug 修复，见 §4.5。本仓库自研的 `launch` / `launch-c` 两档实测收益不足，
> 已从 main 移除，存档见分支 `archive/fc-launch-modes`（`attic/README.md` 有回放方法）。

---

## 0. 一句话结论

> **`netns-exec` 只换掉启动管线最末尾的 `ip netns exec <ns>` 一环**，用一个 ~70 行的静态 helper
> （`setns` + `execve` 两个 syscall）替代 iproute2 —— 因为 `ip netns exec` 除了进入网络命名空间之外，
> 还会**偷偷再开一个 mount namespace、递归遍历整棵挂载树、卸载并重挂 /sys**，这些动作 firecracker
> 完全不需要，却在高并发下全部压在全局 `namespace_sem` 锁上互相串行。

---

## 1. 背景：上游 baseline 到底在做什么

orchestrator 每启动一个 firecracker，走的是这条 shell 管线（`fc/process.go` `NewProcess`）：

```
unshare -m -- bash -c "mount --make-rprivate / &&
    mount -t tmpfs tmpfs <sandbox目录> -o X-mount.mkdir &&
    ln -s <本沙箱真实rootfs> <快照记录的固定rootfs路径> &&
    mkdir -p <内核目录> && ln -s <宿主机内核> <快照记录的固定内核路径> &&
    ip netns exec <ns> <firecracker> --api-sock <sock>"
```

目的只有两个：

1. **私有 mount ns + 符号链接，实现"同一路径、各指各的文件"**：快照里录死了固定路径
   （如 `/fc-vm/rootfs.ext4`），每个沙箱恢复时都得让这**同一个**固定路径解析到**自己**的
   宿主机文件。做法：给每个 FC 开一个私有 mount namespace，在固定路径处挂一层 tmpfs
   遮住原目录，再在里面放符号链接指向本沙箱的真实文件——各 FC 的 mount ns 互不可见，
   同名路径互不冲突。
2. **进入网络命名空间**：每个沙箱有独立 netns（tap 设备在里面），FC 必须在该 netns 内运行。

100 并发压测下，这条管线所在的 `configured fc cost`（拉起 FC 进程 + 等 API socket）
**avg 241.6ms / p99 329ms，占 `total` 294.7ms 的 82%**，是唯一的主战场（见 `启动耗时阶段分析.md` §0/§6）。

## 2. 为什么拿 `ip netns exec` 开刀

直觉上 `ip netns exec` 只是"进个 netns 再 exec"，实际上 iproute2 的 `netns_switch()` 每次调用都做完整一套：

| # | iproute2 实际动作 | 成本/问题 | firecracker 需要吗 |
|---|---|---|---|
| 1 | `open /var/run/netns/<ns>` + `setns(CLONE_NEWNET)` | 便宜，这是唯一真正需要的 | ✅ 需要 |
| 2 | `unshare(CLONE_NEWNS)` —— 再开**第二个** mount ns | 内核 `copy_tree` 复制整棵挂载树，持全局 `namespace_sem` | ❌ 管线开头 `unshare -m` 已经开过了 |
| 3 | `mount("", "/", MS_SLAVE\|MS_REC)` —— 递归 make-rslave | **第二次**全挂载树遍历（第一次是脚本里的 `--make-rprivate`），同样持全局锁 | ❌ 不需要 |
| 4 | `umount2("/sys", MNT_DETACH)` + 重挂 sysfs | sysfs 重建 kobject 树本身就不便宜，且是为了让 `/sys` 反映目标 netns 的网络设备 | ❌ FC 不读 `/sys` 的网络视图 |
| 5 | bind-mount `/etc/netns/<ns>/*` 覆盖 `/etc` | 为了 per-netns 的 resolv.conf 等 | ❌ 沙箱内是 guest 自己的 /etc |
| 6 | `ip` 二进制本身的加载 | 动态链接 libmnl/libbpf/libelf 等，每沙箱一次 ld.so 解析 | ❌ |

也就是说 **6 个动作里 5 个是白做的**，而且 #2/#3/#4 都在全局 `namespace_sem` 上排队 ——
100 个沙箱同时 resume 时，这正是把 `configured fc cost` 从单沙箱几十 ms 放大到 240ms 的串行化来源之一。
p99(329) ≫ avg(241) 的长尾形态也是锁争用的典型特征。

## 3. 优化是什么：只保留第 1 行

`fc-netns-exec`（`packages/orchestrator/cmd/fc-netns-exec/main.go`，CGO_ENABLED=0 静态编译）把上表压缩成：

```go
runtime.LockOSThread()                                  // 见 §4.1
fd, _ := unix.Open("/run/netns/<ns>", O_RDONLY|O_CLOEXEC, 0)
unix.Setns(fd, unix.CLONE_NEWNET)                       // 进入目标 netns
unix.Exec(command, commandArgs, os.Environ())           // 原地 execve 成 firecracker
```

没有第二个 mount ns、没有挂载树遍历、没有 /sys 重挂、没有 /etc bind、没有动态库
（静态二进制不过 ld.so）。管线其余部分（bash、mount、ln、mkdir、`--make-rprivate`、10ms 轮询等
socket）**原封不动**——与上游 baseline 的对比只有一个变量。

## 4. 如何实现

### 4.1 helper 本体的两个关键点

- **`runtime.LockOSThread()` 不能省**：`setns(CLONE_NEWNET)` 只改变**调用它的那个 OS 线程**的
  netns，而 Go runtime 会把 goroutine 在线程间迁移。锁住线程保证 setns 和随后的 `execve`
  发生在同一个线程上；execve 会把整个进程收拢到这个线程，于是 firecracker 完整继承目标 netns。
  （这也正是 `ip netns exec` 自己的做法，只不过它是单线程 C 程序天然满足。）
- **argv 约定**：`fc-netns-exec <netns名或绝对路径> <command> [args...]`，`commandArgs`
  从 `os.Args[2:]` 起切——即 argv[0] 就是 command 本身，与 execve 惯例一致。
  netns 名在 `/run/netns` 下解析，绝对路径则直接用（`namespacePath()`）。

### 4.2 接入方式：在脚本模板里生成，不做字符串替换

`script_builder.go` 把两个模板末尾原来写死的 `ip netns exec {{ .NamespaceID }} ...` 换成一个
占位符 `{{ .FirecrackerCommand }}`，由 `firecrackerCommand()` 决定填什么：

```go
func (sb *StartScriptBuilder) firecrackerCommand(args startScriptArgs, rootfsPaths RootfsPaths) string {
	firecrackerArgs := fmt.Sprintf("%s --api-sock %s", args.FirecrackerPath, args.FirecrackerSocket)

	helper := strings.TrimSpace(sb.builderConfig.FirecrackerNetnsExecHelper)
	if helper == "" || helper == "disabled" || helper == "ip-netns-exec" {
		return fmt.Sprintf("ip netns exec %s %s", args.NamespaceID, firecrackerArgs)
	}

	// Template-build VMs use ConstantRootfsPaths. Keep their original ip-netns
	// path even when the helper is enabled for restored sandboxes.
	if rootfsPaths.TemplateID == "" || rootfsPaths.BuildID == "" {
		return fmt.Sprintf("ip netns exec %s %s", args.NamespaceID, firecrackerArgs)
	}

	return fmt.Sprintf("%s %s %s", helper, args.NamespaceID, firecrackerArgs)
}
```

两个要点：

- **从模板层生成，不是对生成好的脚本做 `strings.Replace`**。后者依赖脚本里
  `ip netns exec ` 这个字面量，上游一旦改模板措辞就会**静默不生效**（回落 baseline 行为，
  不报错、不打日志，优化悄悄丢掉）。模板层生成不存在这个问题。
- **模板构建（template build）的 VM 被显式排除**：它们走 `ConstantRootfsPaths`，
  `TemplateID` / `BuildID` 为空，即使 helper 开着也仍用 `ip netns exec`。
  优化只作用在快照恢复（resume）路径上。

### 4.3 运行时开关（`internal/cfg/model.go`）

只有**一个**环境变量，同时兼任「开关」和「helper 路径」：

```go
FirecrackerNetnsExecHelper string `env:"E2B_FC_NETNS_EXEC_HELPER" envDefault:"/opt/e2b-infra/bin/fc-netns-exec"`
```

| `E2B_FC_NETNS_EXEC_HELPER` 取值 | 行为 |
|---|---|
| 不设置（默认） | 用 `/opt/e2b-infra/bin/fc-netns-exec`，即**默认开启** |
| `disabled` / `ip-netns-exec` / 空串 | 回落上游 `ip netns exec` |
| 任意其它路径 | 用该路径下的 helper |

该字段进了 `makePathsAbsolute()`，相对路径会自动转绝对。helper 由
`packages/orchestrator/Makefile` 的 `make build` 一并产出到 `bin/`，被 spec 的
`packages/*/bin/*` 通配安装。**免重编切换**：改 nomad job env 重跑即可
（`single-node-offline-deploy.md` §9.1）。

> ⚠️ **默认开启 + 不做存在性检查**：`firecrackerCommand()` 不校验 helper 文件在不在，
> 直接拼进 shell。二进制缺失时 bash 报 command not found、FC 根本没起，`p.cmd.Wait()` 拿到
> exit status 127，`configure` 随即以 `error waiting for fc process: exit status 127` 失败。
> 装完 RPM 请确认 `/opt/e2b-infra/bin/fc-netns-exec` 存在（运维检查清单见
> `deploy-docs/06-日常运维手册.md`）。
>
> 这条错误路径能**快速**报出来，依赖 `socket.Wait()` 尊重调用方 ctx —— 见下方 §4.5。

### 4.4 诊断埋点

`E2B_FC_START_SCRIPT_DIAG=1` 时，helper 在 setns 之后、execve 之前往 stderr 打一行：

```
e2b_fc_start_script_marker stage=inside_netns_before_firecracker_exec ns=<纳秒时间戳> socket=<api-sock> ...
```

配合 orchestrator 侧 `configured fc cost` 埋点，可以量出"shell 管线开始 → 即将 exec FC"这一段的真实耗时。

### 4.5 唯一一处偏离官方：`socket.Wait()` 的 ctx

`internal/sandbox/socket/socket.go` 是本优化链路上唯一**没有**与官方逐字节一致的文件。
官方那版（以及本仓库此前照抄的版本）是：

```go
ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutSeconds)*time.Second)
```

用的是 `context.Background()` 而不是传进来的 `ctx`，等于把调用方的取消信号整个丢掉。
而调用方 `fc/process.go` 的 `configure()` 恰恰依赖这条链路：

```go
startCtx, cancelStart := context.WithCancelCause(ctx)
go func() {
    waitErr := p.cmd.Wait()          // FC 进程/shell 管线挂了
    ...
    cancelStart(errMsg)              // ← 通知 socket.Wait 别等了
}()
err = socket.Wait(startCtx, p.firecrackerSocketPath)
```

FC 起不来（比如 §4.3 说的 helper 二进制缺失，bash 退 127）时，`cancelStart` 已经触发，
但 `socket.Wait` 收不到，会一路空等到 `SOCKET_WAIT_TIMEOUT_SECONDS`（默认 **300 秒**）才返回，
且返回的还是超时错误而不是真正的死因。本仓库改成：

```go
ctx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSeconds)*time.Second)
```

超时上限不变，只是挂到调用方 ctx 上。附带好处：`Wait` 错误信息里的 `context.Cause(ctx)`
现在能带出真正的原因（`error waiting for fc process: exit status 127`），以前恒为超时。

⚠️ **rebase 注意**：这是一行改动，将来跟上游（2027.xx）合版时 `socket.go` 一旦有冲突，
很容易被顺手选成上游/官方那一侧，bug 就悄悄回来了——而它的症状（沙箱起不来时卡 300 秒、
且报错指向超时而非真实死因）在正常压测里根本不显眼。合版时请专门确认这一行。

| 遗留问题 | 说明 |
|---|---|
| 仍有 ~7 个 fork/exec | bash + mount×2 + ln×2 + mkdir 还在，每个都要过 ld.so + libc init |
| 仍有 1 次全树递归遍历 | 脚本开头的 `mount --make-rprivate /` 还在，仍持全局 `namespace_sem` |
| bash 解析开销 | `bash -c "<脚本>"` 的启动 + 词法解析 + 为 `&&` 链逐个 fork |
| socket 仍 10ms 轮询 | 平均白等 ~5ms + 长尾 |
| helper 是 Go 二进制 | Go runtime 自启动（线程/调度器/GC 初始化）约 1~2ms，非零 |

这些正是本仓库 `launch` / `launch-c` 两档想解决的问题。实测结论：子进程侧
（`socket.Wait()` 段）确实从 138ms 压到 29ms，但父进程 `cmd.Start()` 段因为 mount namespace
复制被拖到 avg 143ms，两段合计比 `netns-exec` 反而**倒赔约 31ms**，故未采纳。
完整实验记录见分支 `archive/fc-launch-modes` 的 `benchmark/FC启动优化-launch.md` /
`FC启动优化-launch-c.md`。

## 6. 验证方法

```bash
# 1) 确认开关生效（orchestrator 进程 env）
grep E2B_FC_NETNS_EXEC_HELPER /opt/e2b-infra/rendered/template-manager.hcl

# 2) 确认 helper 二进制在位（默认开启，缺了 FC 起不来）
ls -l /opt/e2b-infra/bin/fc-netns-exec

# 3) strace 看一次真实启动：应看到 helper 只有 openat+setns+execve，
#    且再无 unshare/第二次 mount("/",...MS_SLAVE...)/umount2("/sys")
strace -f -e trace=clone,clone3,unshare,setns,mount,umount2,execve -p $(pgrep -f orchestrator) 2>&1 | grep -A3 fc-netns-exec

# 4) 压测对比（benchmark/run_benchmark.py），看这个日志 key：
#    configured fc cost
```

## 7. 文件清单

| 文件（补丁后路径） | 内容 | 与官方源码 |
|---|---|---|
| `packages/orchestrator/cmd/fc-netns-exec/main.go` | helper 本体（setns + execve + 诊断埋点） | 逐字节一致 |
| `packages/orchestrator/internal/cfg/model.go` | `FirecrackerNetnsExecHelper` 配置项 + 路径绝对化 | 逐字节一致 |
| `packages/orchestrator/internal/sandbox/fc/script_builder.go` | 模板占位符 + `firecrackerCommand()` 档位判断 | 逐字节一致 |
| `packages/orchestrator/internal/sandbox/fc/script_builder_test.go` | 三种取值（默认/`disabled`/模板构建）的单测 | 逐字节一致 |
| `packages/orchestrator/internal/sandbox/socket/socket.go` | `socket.Wait()`：超时上限 + 尊重调用方 ctx | 多一处 ctx bug 修复（§4.5） |
| `packages/orchestrator/Makefile` | `make build` 产出 `bin/fc-netns-exec` | 本仓库改造（ARM 本地编译） |
