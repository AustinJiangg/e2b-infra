# attic — 已下线的 FC 启动优化实验

这个目录只存在于 `archive/fc-launch-modes` 分支，**不会合并进 `main`**。

## 背景

FC 启动路径上一共做过 3 种优化，通过 `E2B_FC_LAUNCH_MODE` 环境变量在运行时四选一：

| 档位 | 说明 | 归属 |
|------|------|------|
| `disabled` | 上游原样：`unshare -m -- bash -c "... ip netns exec <ns> firecracker"` | upstream |
| `netns-exec` | 把末尾的 `ip netns exec` 换成 `fc-netns-exec` 助手（setns + execve） | openEuler 官方 |
| `launch` | 专用 Go 助手 `fc-launch`，一个进程里做完 mounts + setns + execve，配 inotify 等 socket | 本仓库实验 |
| `launch-c` | 同上，但助手是单线程 C 程序 `fc-launch-c`，省掉 unshare(1) 包装和 Go runtime 启动 | 本仓库实验 |

100 并发实测下来，`launch` / `launch-c` 相对 `netns-exec` 没有拿到有意义的收益：
子进程侧（`socket.Wait()` 段）确实从 138ms 降到 29ms，但父进程 `cmd.Start()` 段因为
mount namespace 复制被拖到 avg 143ms，两段合计反而比 `netns-exec` 差约 31ms。

因此 `main` 上只保留 `netns-exec` 一种，并且改写成与 openEuler 官方**字节级一致**的形式
（开关走 `cfg.BuilderConfig.FirecrackerNetnsExecHelper` / `E2B_FC_NETNS_EXEC_HELPER`，
注入点在 `script_builder.go` 的模板里），四档枚举 `fc/mode.go` 一并删除。

## 怎么把实验代码找回来

`0002-fc-launch-experiments.patch` 是**叠在 `main` 的 `0001` 之上**的增量补丁，
按顺序打完就能恢复四档齐全的状态：

```bash
# 在一棵干净的 upstream 2026.09 源码树里
patch -p1 < 0001-adapted-for-arm-architecture.patch      # main 上那份（只有 netns-exec）
patch -p1 < attic/0002-fc-launch-experiments.patch       # 加回 launch / launch-c
```

已验证：`upstream 2026.09 + 0001 + 0002` 与本分支 `7c0f30f` 的 patch 应用结果**字节级相同**。

`0002` 除了加回 `launch` / `launch-c`，也会把 `netns-exec` 从官方形式回退成原先基于
`strings.Replace` 的实现（因为两者是同一次改动的两面）。如果只想在官方形式上继续做
`launch` 实验，需要手工挑其中 `cmd/fc-launch*`、`internal/sandbox/fc/launchplan/`、
`fc/mode.go` 这几块。

## 本分支还包含

- 完整的四档源码（在 `0001-adapted-for-arm-architecture.patch` 里）
- `benchmark/FC启动优化-launch.md`、`benchmark/FC启动优化-launch-c.md` 两篇实验记录与实测数据
- `benchmark/` 下引用四档的分析文档原文
