# 沙箱启动耗时基准测试方案

复现上游提供的《监控统计报告》：批量创建 100 个沙箱，采集 orchestrator 端
`[ResumeSandbox]` 各阶段耗时日志，生成同格式统计报告，并与参考数据对比。

## 1. 原理

本仓库的 `0001-adapted-for-arm-architecture.patch` 在 orchestrator 的沙箱恢复路径
（`internal/sandbox/sandbox.go` 的 `ResumeSandbox` 和 `internal/sandbox/fc/process.go`
的 `Resume`）埋了一组 zap 日志，全部带 `[ResumeSandbox]` 前缀和 `traceID`。
**正常创建沙箱（从模板快照恢复）就会走这条路径**，无需额外开关。

参考报告中每一行与日志的对应关系：

| 报告阶段 | 报告描述 | 日志关键字 | 说明 |
|---|---|---|---|
| 沙箱恢复准备 | 准备 rootfs（连接 nbd 设备） | `get rootfs path cost` | 与下面两个"等待"并行执行 |
| 沙箱恢复准备 | 获取网络槽位 | `wait network slot cost` | 从网络池取槽位 |
| 沙箱恢复准备 | 获取 template 元数据 | `get template metadata cost` | |
| 创建 firecracker 进程 | 创建 firecracker 进程 | `fc.NewProcess cost` | |
| 创建 firecracker 进程 | 等待firecracker启动 | `configured fc cost` | 启动 FC 进程并等待其 API socket |
| 创建 firecracker 进程 | 等待uffd sock | `get uffd sock path cost` | |
| firecracker 恢复虚拟机 | 加载快照 | `load snapshot cost` | |
| firecracker 恢复虚拟机 | 调用恢复 | `post resume cost` | resumeVM API |
| firecracker 恢复虚拟机 | 设置mmds | `set mmds cost` | |
| firecracker 恢复虚拟机 | 恢复虚拟机 | `resume VM cost` | 上面 6 项所在函数的总耗时 |
| 启动 envd | （3 行） | *无埋点* | patch 未对 envd 阶段埋点，参考报告中同样为空 |
| 总耗时 | 总耗时 | `total cost` | `ResumeSandbox` 函数整体耗时 |

阶段之间的并行/包含关系（用于核对数据是否自洽）：

```
总耗时 ≈ 获取网络槽位 + 获取template元数据 + 创建fc进程 + 恢复虚拟机 + 少量其他开销
恢复虚拟机 ≈ max(等待firecracker启动, 等待uffd sock, 准备rootfs)   ← 三者并行
              + 加载快照 + 调用恢复 + 设置mmds
```

例如参考数据沙箱1：max(22.5, 10.7, 0.04) + 16.0 + 0.35 + 0.22 ≈ 39 ms（恢复虚拟机），
39 + 0.05 + 0.11 + 0.28 ≈ 42 ms（总耗时），与报告一致。

> 注意：`总耗时` 只是 orchestrator 内部恢复 VM 的耗时，**不含** API 网关、envd
> 就绪等待等，所以客户端感受到的 `Sandbox.create()` 耗时会明显大于它，两者口径不同。

## 2. 前置条件

- 集群已按 `docs/zh/install.md` 部署完成（Nomad 模式），模板已构建（如 `base`）。
- **orchestrator 二进制必须是用本仓库 patch 构建的**，否则没有这些日志（见 3.1 预检）。
- 客户端环境（可以直接用 API server 节点）：
  ```bash
  pip install e2b==2.20.0 python-dotenv
  python3 /opt/e2b-infra/patch_e2b.py        # https -> http 补丁
  ```
  当前目录准备 `.env`（与 docs/zh/usage.md 一致）：
  ```env
  E2B_API_KEY="e2b_xxx"
  E2B_ACCESS_TOKEN="sk_e2b_xxx"
  E2B_DOMAIN="xxx"
  E2B_API_URL="http://{server_ip}:3000"
  E2B_HTTP_SSL="false"
  ```
- 能访问 Nomad（`nomad` CLI + `NOMAD_ADDR`），用于采集 orchestrator 日志。
- 节点资源足够同时跑 100 个沙箱（参考测试是 100 个沙箱存活；1c1g 模板约需
  100G 内存预算，不够就用 `--kill-each` 改为"创建即销毁"，见 5.4）。

## 3. 测试步骤

### 3.0 记录环境信息（对比报告必备）

```bash
lscpu | grep -E "Model name|^CPU\(s\)|MHz"; free -h; uname -r
lsblk -d -o NAME,MODEL,ROTA,SIZE     # 磁盘类型，影响"加载快照/准备rootfs"
```

把结果记入最终报告，作为"机器型号差不多"的依据。

### 3.1 预检：确认日志埋点生效

创建 1 个沙箱，然后看 orchestrator 日志里有没有 `[ResumeSandbox]`：

```bash
cd benchmark
python3 run_benchmark.py --template base --count 1 --warmup 0 --kill-each
bash collect_logs.sh orchestrator ./precheck-logs
grep -h "\[ResumeSandbox\]" precheck-logs/*.log | tail -15
```

能看到 `enter` / `... cost: x ms` / `total cost` 一组日志即可继续。
**看不到则说明部署的 orchestrator 不是 patch 版本，先解决这个再压测。**

### 3.2 正式压测（100 个沙箱）

```bash
python3 run_benchmark.py --template base --count 100 --concurrency 1 --warmup 3
```

参数说明：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--count` | 100 | 正式沙箱数量 |
| `--concurrency` | 1 | 并发数。参考数据约 4 个/秒，串行即可对齐；想测并发能力可调大（节点上限 `MAX_STARTING_INSTANCES_PER_NODE=30`） |
| `--warmup` | 3 | 预热数量，不计入统计。首个沙箱要拉模板缓存，耗时会大几个量级，必须预热 |
| `--kill-each` | 关 | 每个创建完立即 kill。默认是全部创建完后统一 kill（与参考测试"100 个同时存活"形态一致） |
| `--sandbox-timeout` | 300 | 沙箱自动过期秒数，kill 失败时的兜底 |
| `--interval` | 0 | 两次创建之间的间隔秒数（限速） |

脚本结束时会打印：客户端整体耗时统计、结果文件路径，以及**已经填好时间窗口的
parse_report.py 命令**，直接复制执行即可。

### 3.3 采集 orchestrator 日志

```bash
bash collect_logs.sh orchestrator ./orchestrator-logs
```

- 多个 client 节点时脚本会遍历全部 running allocation；**日志必须收齐**，
  否则落在其他节点的沙箱 trace 会缺失。
- orchestrator 自压测开始后不能重启过（重启会换 allocation、丢 stdout 日志）。

### 3.4 生成统计报告

用 3.2 结束时打印的命令，形如：

```bash
python3 parse_report.py orchestrator-logs/*.log \
    --since '2026-06-10T15:00:00.000+08:00' \
    --until '2026-06-10T15:01:30.000+08:00' \
    --expected 100 --reference reference_sample.csv
```

| 参数 | 说明 |
|---|---|
| `--since/--until` | 统计时间窗口（run_benchmark.py 已打印）。**客户端与服务器时钟不同步时不要用**，改用 `--last 100` |
| `--last N` | 取最近 N 个有效沙箱，替代时间窗口 |
| `--expected N` | 有效沙箱数不等于 N 时告警 |
| `--reference` | 参考数据，生成对比表 |
| `--sort` | 沙箱列排序：`traceid`（默认，与参考报告一致）或 `time` |
| `--tz` | 报告显示时区，默认 `+08:00` |

输出到 `report-out/`：

| 文件 | 内容 |
|---|---|
| `report_wide.csv` | **与参考报告同布局**（行=阶段，列=沙箱1..N），右侧追加 平均/P50/P90/P95/最大 汇总列；UTF-8 BOM，Excel 直接打开 |
| `report_long.csv` | 每行一个沙箱，便于透视/画图 |
| `summary.csv` | 各阶段 min/avg/p50/p90/p95/p99/max |
| `compare.csv` | 与参考数据的均值对比（差值 ms 和 %） |

控制台同时打印 summary 和对比表。

### 3.5 判断是否复现

`reference_sample.csv` 是上游报告中可见的 4 个完整沙箱样本（其 100 个的完整数据
未提供）。对比时建议按阶段看：

- **总耗时 / 恢复虚拟机**：参考约 31~47 ms。本次 avg/p50 落在同一量级（几十 ms）
  即可认为复现；差 2~3 倍以上再按下面拆阶段定位。
- **等待firecracker启动**（参考 ~22 ms）：反映 CPU 单核性能与 FC 进程拉起速度。
- **等待uffd sock**（参考 ~10.5 ms）：数值非常稳定，适合做机器间基线对比。
- **加载快照**（参考 5~16 ms）：反映快照文件 IO/页缓存，受磁盘类型影响最大。
- **准备 rootfs**（参考多数 0.03 ms，偶发 38 ms）：长尾来自 NBD 设备连接，
  关注 p95/max 而不是 avg。
- **获取网络槽位**（参考 ~0.05 ms）：网络池命中时接近 0；如果本次普遍偏大，
  说明池子没预热（加大 `--warmup` 或等 `[Pool Status]` 日志显示池子充足后再压）。

## 4. 注意事项 / FAQ

1. **有效沙箱数 < expected**：时间窗口不准（时钟偏差）、多节点日志没收齐、
   部分创建失败（看 run_benchmark 输出的 FAIL）、或 orchestrator 日志被轮转。
2. **时钟偏差**：`--since/--until` 用的是客户端时间，与 orchestrator 节点时钟比对。
   不在同一台机器时先 `chronyc tracking`/`ntpstat` 确认同步，或直接用 `--last 100`。
3. **首批数据异常大**：模板缓存冷启动。属正常现象，预热即可；如果想测冷启动，
   单独记录第一个沙箱的数据，不要混入统计。
4. **资源不够跑 100 个并存**：用 `--kill-each`。注意这会改变测试形态
   （NBD/网络槽位复用模式不同），对比时注明。
5. **多次压测**：两轮之间间隔几秒即可，`--last`/时间窗口能区分轮次；
   报告目录用 `--outdir report-round2` 之类区分。
6. **envd 三行为空是正常的**：patch 没有对 envd 阶段埋点，上游报告同样为空。
   如需补测，可在客户端用 `sbx.commands.run("true")` 首次往返耗时近似。
7. **日志格式**：parse_report.py 同时支持 zap JSON 行和纯文本行，时间字段
   兼容 `timestamp/ts/time`（ISO 字符串或 epoch 秒/毫秒/纳秒）。

## 5. 文件清单

| 文件 | 用途 |
|---|---|
| `run_benchmark.py` | 客户端压测：批量创建/清理沙箱，记录客户端耗时，输出解析命令 |
| `collect_logs.sh` | 从 Nomad 采集所有 orchestrator allocation 的 stdout/stderr 日志 |
| `parse_report.py` | 解析 `[ResumeSandbox]` 日志，生成 4 个报告文件（仅标准库） |
| `reference_sample.csv` | 上游报告的 4 个沙箱参考数据，供 `--reference` 对比 |
