# FC 启动优化 · 档位 4：`launch-c` 详解

> **版本基准：e2b-infra `2026.09` tag + `0001-adapted-for-arm-architecture.patch` + `0002-fc-launch-dedicated-helper.patch`。**
> 本档全部源码都在 `0002-fc-launch-dedicated-helper.patch` 里，文中文件路径均指补丁应用后的路径。
> 档位开关机制、配置方法见 `single-node-offline-deploy.md` 第 9 节；耗时数据口径见 `启动耗时阶段分析.md`。
> 姊妹篇：`FC启动优化-launch.md`（档位 3，本档的直接前身，含 spawn 两次踩坑的完整记录，本文不重复）。

---

## 0. 一句话结论

> **`launch-c` 档把档位 3 的 "unshare(1) 包装 + Go helper" 收敛成一个 ~200 行、单线程的
> C 二进制 `fc-launch-c`**：单线程进程可以在 execve 之后自己 `unshare(CLONE_NEWNS)`
> （Go 因 runtime 多线程共享 fs_struct 做不到——这正是档 3 需要外部 unshare(1) 包装的根因），
> 于是 orchestrator 直接 spawn 它：比档 3 再省一次 execve、~1-2ms 的 Go runtime 自启动，
> 以及对 util-linux `--propagation unchanged` 选项的依赖。每沙箱启动收敛为
> **1 次 clone + 2 次 execve（helper→firecracker）**，helper 二进制 ~17KB（Go 版 ~3MB）。

## 1. 相对档位 3 砍掉了什么

| 档 3 (`launch`) 残留成本 | 档 4 (`launch-c`) |
|---|---|
| `unshare(1)` 包装：一次小动态二进制的 execve + ld.so | 无（helper 自己 unshare） |
| 依赖 util-linux ≥ 2.26（`--propagation unchanged`） | 无外部依赖（helper 自己控制不做递归 remount） |
| fc-launch 是 Go 二进制：runtime 自启动（线程/调度器/GC 初始化）~1-2ms | C，main 前只有 libc init（<0.5ms） |
| plan 经 JSON 编解码 | plan 即 argv（见 §2），C 侧零解析 |

**没有变**的部分：mount ns 仍在**子进程**里创建（档 3 v2 用实测换来的教训——绝不能放回
orchestrator 的 clone(2)，见姊妹篇 §3.2 坑二）；传播保护仍是**定点非递归 `MS_PRIVATE`**
（O(路径深度)，不做全树遍历）；`socket.Wait` 的 inotify 快路径对本档同样生效（`socket.go`
按 env 字符串同时认 `launch` 与 `launch-c`）。

## 2. plan 的传递：有序 argv flags（不再是 JSON）

```
fc-launch-c --netns <ns名|绝对路径> --fc <firecracker> --sock <api-sock>
            [--tmpfs <dir> | --symlink <link> <target> | --mkdir <dir>]...
```

- orchestrator 侧由 `launchplan.Argv()`（`launchplan/argv.go`）从同一个 `launchplan.Plan`
  结构体编码——**plan 的生成路径（`BuildLaunchPlan`，与脚本模板逐步骤镜像）完全复用档 3 的**，
  两档只在"怎么把 plan 交给 helper"上分叉，`process.go` 里通过 `launch.HelperArgv()` 取得
  （定义在 `script_builder.go`，避免 process.go 新增 import）。
- step flags 严格按 `Plan.Steps` 顺序排列，helper 按 argv 顺序执行——V1/V2 布局的步骤次序
  天然保持。
- execve 的 argv 是数组原样传递：和 JSON 方案一样**没有 shell、没有引号/转义层**，
  但 C 侧连解析器都不需要了（JSON 的字符串转义在 C 里正是最容易写出洞的部分）。
- `argv_test.go` 断言编码输出；`fc_launch.c` 的 pass-1 先校验整个 argv 形态再动手，
  避免半途失败留下垃圾。

## 3. fc_launch.c 走读（`cmd/fc-launch-c/fc_launch.c`）

执行顺序（与 Go 版 `launchplan.Run` 逐步对应）：

1. **pass-1 校验 argv**：flags/值配对、`--netns/--fc/--sock` 齐全，否则 usage 退出（exit 2）。
2. **`unshare(CLONE_NEWNS)`**：单线程进程合法（Go 里 EINVAL）。注意这里**不会**像
   `unshare(1)` 默认行为或 Go 的 Unshareflags 路径那样递归 remount "/"——什么都不做，
   传播保护交给下一步定点处理。
3. **定点传播保护**：对每个 `--tmpfs` 目标执行 `make_containing_mount_private()`——
   从目标路径逐级向上 `mount("", dir, NULL, MS_PRIVATE, NULL)`：EINVAL=不是挂载点、
   ENOENT=组件还不存在（后面 tmpfs 步骤才创建），都继续向上；"/" 必是挂载点，循环必然终止。
   与 Go 版逐 syscall 等价（strace 对比过，序列一致）。
4. **按 argv 顺序执行 steps**：`--mkdir`→`mkdir -p`（0755，等价 os.MkdirAll）；
   `--tmpfs`→mkdir -p + `mount("tmpfs",dir,"tmpfs",0,NULL)`；`--symlink`→`symlink(target,link)`。
5. **进 netns + execve**：按 iproute2 的顺序在 `/var/run/netns` → `/run/netns` 下解析
   ns 名（绝对路径直接用），`setns(fd, CLONE_NEWNET)` 后原地
   `execve(fc, {fc, "--api-sock", sock}, environ)`。单线程进程 setns 天然作用于整个进程，
   **不需要**档 2/3 里 Go 的 `runtime.LockOSThread()` 技巧。

错误处理：任何失败都带 errno 文本打到 stderr（`fc-launch-c: <动作> <路径>: <strerror>`）
并 exit 1——这些会出现在 orchestrator 收集的 FC 进程输出里，排障时直接可读。

## 4. 构建与安装

- `packages/orchestrator/Makefile` 的 `make build` 里：
  `$(CC) -O2 -Wall -Wextra -o bin/fc-launch-c ./cmd/fc-launch-c/fc_launch.c`。
  spec 已有 `BuildRequires: gcc`，产物走既有 `packages/*/bin/*` 通配安装到
  `/opt/e2b-infra/bin/`，**spec 无需改动**。
- 动态链接（仅 libc，与 iproute2/unshare 同级依赖）。若构建环境装有 `glibc-static`，
  可自行加 `-static` 彻底去掉 ld.so（收益 ~0.2-0.4ms/次，非必需）。

## 5. 运行时开关与验证

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `E2B_FC_LAUNCH_MODE=launch-c` | 启用本档（同时激活 inotify 等 socket） | `disabled` |
| `E2B_FC_LAUNCH_C_HELPER` | helper 路径覆盖 | `/opt/e2b-infra/bin/fc-launch-c` |

```bash
# 1) strace 验证形态：单进程一条直线，无 wrapper、无递归 remount——
#    execve(fc-launch-c) → unshare(CLONE_NEWNS) → 定点 MS_PRIVATE（逐级向上，
#    EINVAL/ENOENT 后最终一次成功）→ mount tmpfs → setns(CLONE_NEWNET) → execve(firecracker)
strace -f -e trace=unshare,setns,mount,execve -p $(pgrep -f orchestrator) 2>&1 | grep -A8 fc-launch-c

# 2) 宿主机不应见到沙箱 tmpfs 传播（恒为 0）
grep -c '<沙箱目录前缀>' /proc/self/mountinfo

# 3) 压测对比档 3：fc spawn cost 应持平（都是纯 vfork+execve），
#    fc socket wait cost 应再降 ~2-4ms（少一次 execve + 无 Go runtime init）
```

## 6. 局限与下一步

- helper 收益是每沙箱固定几毫秒量级——100 并发下 `等待firecracker启动` 的大头
  （FC 自身加载/初始化 + CPU 争用 + 子进程侧 copy_mnt_ns 排队）不归本档管。
  下一步仍是姊妹篇 §6 的**按网络槽位预热池化 pre-boot FC**（Resume 命中只做
  LoadSnapshot → ResumeVM），那才是把这一段基本清零的方向。
- `Argv()` 与 `fc_launch.c` 的 flags 协议是手工镜像关系（如同 `BuildLaunchPlan` 与脚本
  模板），加新 step 类型时两处要一起改，`argv_test.go` 会兜住编码侧。

## 7. 文件清单

| 文件（补丁后路径） | 内容 |
|---|---|
| `packages/orchestrator/cmd/fc-launch-c/fc_launch.c` | C helper 本体（argv 校验、unshare、定点 MS_PRIVATE、steps、setns、execve） |
| `packages/orchestrator/internal/sandbox/fc/launchplan/argv.go` | `Argv()`：Plan → 有序 argv flags |
| `packages/orchestrator/internal/sandbox/fc/launchplan/argv_test.go` | 编码单测（顺序、未知 op 报错） |
| `packages/orchestrator/internal/sandbox/fc/script_builder.go` | `HelperArgv()`（复用 BuildLaunchPlan 产物） |
| `packages/orchestrator/internal/sandbox/fc/mode.go` | `launch-c` 档解析、helper 路径 |
| `packages/orchestrator/internal/sandbox/fc/process.go` | launch-c 分支：直接 spawn C helper |
| `packages/orchestrator/internal/sandbox/socket/socket.go` | inotify 门控同时认 `launch`/`launch-c` |
| `packages/orchestrator/Makefile` | `make build` 产出 `bin/fc-launch-c` |
