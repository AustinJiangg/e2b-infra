# FC 启动优化 · 档位 2：`netns-exec` 详解

> **版本基准：e2b-infra `2026.09` tag + `0001-adapted-for-arm-architecture.patch` + `0002-fc-launch-dedicated-helper.patch`。**
> 本档全部源码都在 `0002-fc-launch-dedicated-helper.patch` 里（仓库不直接跟踪打过补丁的 Go 源码树），
> 文中引用的文件路径均指补丁应用后的路径。三档开关机制、配置方法见 `single-node-offline-deploy.md` 第 9 节；
> 各阶段耗时数据口径见 `启动耗时阶段分析.md`。姊妹篇：`FC启动优化-launch.md`（档位 3）。

---

## 0. 一句话结论

> **`netns-exec` 档只换掉启动管线最末尾的 `ip netns exec <ns>` 一环**，用一个 ~70 行的静态 helper
> （`setns` + `execve` 两个 syscall）替代 iproute2 —— 因为 `ip netns exec` 除了进入网络命名空间之外，
> 还会**偷偷再开一个 mount namespace、递归遍历整棵挂载树、卸载并重挂 /sys**，这些动作 firecracker
> 完全不需要，却在高并发下全部压在全局 `namespace_sem` 锁上互相串行。

---

## 1. 背景：baseline（档位 1 `disabled`）到底在做什么

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

## 2. 为什么先拿 `ip netns exec` 开刀

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
socket）**刻意原封不动**——这样与 baseline 的 A/B 对比只有一个变量。

## 4. 如何实现

### 4.1 helper 本体的两个关键点

- **`runtime.LockOSThread()` 不能省**：`setns(CLONE_NEWNET)` 只改变**调用它的那个 OS 线程**的
  netns，而 Go runtime 会把 goroutine 在线程间迁移。锁住线程保证 setns 和随后的 `execve`
  发生在同一个线程上；execve 会把整个进程收拢到这个线程，于是 firecracker 完整继承目标 netns。
  （这也正是 `ip netns exec` 自己的做法，只不过它是单线程 C 程序天然满足。）
- **argv 约定**：`fc-netns-exec <netns名或绝对路径> <command> [args...]`，`commandArgs`
  从 `os.Args[2:]` 起切——即 argv[0] 就是 command 本身，与 execve 惯例一致。
  netns 名在 `/run/netns` 下解析，绝对路径则直接用（`namespacePath()`）。

### 4.2 接入方式：字符串替换，不动脚本生成器

`fc/mode.go` 的 `netnsExecScript()`：

```go
strings.Replace(script, "ip netns exec "+namespaceID, helper+" "+namespaceID, 1)
```

baseline 生成的脚本原样生成，仅把末尾 `ip netns exec <ns>` 换成 `<helper> <ns>`。
好处是**脚本其余每个字节与 baseline 相同**，出问题时对比面最小；坏处是它依赖脚本模板里
`ip netns exec ` 这个字面量——将来若上游改模板措辞，这个 Replace 会静默不生效
（回落成 baseline 行为，不会坏，但优化丢了），rebase 时要检查。

### 4.3 运行时开关（`fc/mode.go`）

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `E2B_FC_LAUNCH_MODE=netns-exec` | 启用本档 | `disabled`（不认识的值也回落 disabled） |
| `E2B_FC_NETNS_EXEC_HELPER` | helper 路径覆盖 | `/opt/e2b-infra/bin/fc-netns-exec`（RPM 安装路径） |

helper 由 `packages/orchestrator/Makefile` 的 `make build` 一并产出到 `bin/`，
被 spec 的 `packages/*/bin/*` 通配安装。**免重编切换**：改 nomad job env 重跑即可
（`single-node-offline-deploy.md` §9.1）。

### 4.4 诊断埋点

`E2B_FC_START_SCRIPT_DIAG=1` 时，helper 在 setns 之后、execve 之前往 stderr 打一行：

```
e2b_fc_start_script_marker stage=inside_netns_before_firecracker_exec ns=<纳秒时间戳> socket=<api-sock> ...
```

配合 orchestrator 侧 `fc spawn cost` 埋点，可以量出"shell 管线开始 → 即将 exec FC"这一段的真实耗时。

## 5. 本档没有解决什么（→ 档位 3）

| 遗留问题 | 说明 |
|---|---|
| 仍有 ~7 个 fork/exec | bash + mount×2 + ln×2 + mkdir 还在，每个都要过 ld.so + libc init |
| 仍有 1 次全树递归遍历 | 脚本开头的 `mount --make-rprivate /` 还在，仍持全局 `namespace_sem` |
| bash 解析开销 | `bash -c "<脚本>"` 的启动 + 词法解析 + 为 `&&` 链逐个 fork |
| socket 仍 10ms 轮询 | 平均白等 ~5ms + 长尾；本档**刻意不改**（`socket.go` 注释：保持 A/B 基线纯净） |
| helper 是 Go 二进制 | Go runtime 自启动（线程/调度器/GC 初始化）约 1~2ms，非零 |

这些全部由档位 3（`launch`）解决，见 `FC启动优化-launch.md`。

## 6. 验证方法

```bash
# 1) 确认档位生效（orchestrator 进程 env）
grep E2B_FC_LAUNCH_MODE /opt/e2b-infra/rendered/template-manager.hcl

# 2) strace 看一次真实启动：应看到 helper 只有 openat+setns+execve，
#    且再无 unshare/第二次 mount("/",...MS_SLAVE...)/umount2("/sys")
strace -f -e trace=clone,clone3,unshare,setns,mount,umount2,execve -p $(pgrep -f orchestrator) 2>&1 | grep -A3 fc-netns-exec

# 3) 压测对比（benchmark/run_benchmark.py），看这几个日志 key：
#    configured fc cost / fc spawn cost / fc socket wait cost
```

## 7. 文件清单

| 文件（补丁后路径） | 内容 |
|---|---|
| `packages/orchestrator/cmd/fc-netns-exec/main.go` | helper 本体（setns + execve + 诊断埋点） |
| `packages/orchestrator/internal/sandbox/fc/mode.go` | 档位解析、helper 路径、`netnsExecScript()` 替换 |
| `packages/orchestrator/internal/sandbox/fc/mode_test.go` | 档位解析 + 脚本替换的单测 |
| `packages/orchestrator/internal/sandbox/fc/process.go` | `NewProcess` 里按档位分支 |
| `packages/orchestrator/Makefile` | `make build` 产出 `bin/fc-netns-exec` |
