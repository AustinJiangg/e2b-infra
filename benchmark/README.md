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
| 准入排队 | 准入排队(等starting槽位) | `acquire wait cost` | **在 `total` 之外**：等 `startingSandboxes` 信号量，在 `server/utils.go` 埋点；高并发瓶颈定位详见 `高并发瓶颈定位方案.md` |
| 沙箱恢复准备 | 准备 rootfs（连接 nbd 设备） | `get rootfs path cost` | 与下面两个"等待"并行执行 |
| 沙箱恢复准备 | 获取网络槽位 | `wait network slot cost` | 从网络池取槽位 |
| 沙箱恢复准备 | 获取 template 元数据 | `get template metadata cost` | |
| 创建 firecracker 进程 | 创建 firecracker 进程 | `fc.NewProcess cost` | |
| 创建 firecracker 进程 | 等待firecracker启动 | `configured fc cost` | 启动 FC 进程并等待其 API socket（父=下面两段之和） |
| 创建 firecracker 进程 | └拉起FC进程 | `fc spawn cost` | `p.cmd.Start()` fork/exec 启动命令（受 `E2B_FC_LAUNCH_MODE` 3 档影响），在 `process.go` `configure` 埋点 |
| 创建 firecracker 进程 | └等FC API socket | `fc socket wait cost` | `socket.Wait()` 等 FC 的 API socket 就绪（含命名空间内脚本/exec + FC 启动），在 `process.go` `configure` 埋点 |
| 创建 firecracker 进程 | 等待uffd sock | `get uffd sock path cost` | |
| firecracker 恢复虚拟机 | 加载快照 | `load snapshot cost` | |
| firecracker 恢复虚拟机 | 调用恢复 | `post resume cost` | resumeVM API |
| firecracker 恢复虚拟机 | 设置mmds | `set mmds cost` | |
| firecracker 恢复虚拟机 | 恢复虚拟机 | `resume VM cost` | 上面 6 项所在函数的总耗时 |
| 启动 envd | 启动 envd | `start envd cost` | `WaitForEnvd` 整体（orchestrator 同步调 envd `/init`），在 `sandbox.go` 埋点 |
| 启动 envd | 请求init接口 | `envd init request cost` | `POST /init` 请求往返（含失败重试），在 `envd.go` `initEnvd` 埋点 |
| 启动 envd | 读取envd返回体 | `read envd response cost` | 读取 `/init` 响应体，在 `envd.go` `initEnvd` 埋点 |
| ResumeSandbox | ResumeSandbox总耗时 | `total cost` | `ResumeSandbox` 函数整体耗时（原「总耗时」，重命名以区分 total 外的准入排队） |

阶段之间的并行/包含关系（用于核对数据是否自洽）：

```
总耗时 ≈ 获取网络槽位 + 获取template元数据 + 创建fc进程 + 恢复虚拟机 + 少量其他开销
恢复虚拟机 ≈ max(等待firecracker启动, 等待uffd sock, 准备rootfs)   ← 三者并行
              + 加载快照 + 调用恢复 + 设置mmds
```

例如参考数据沙箱1：max(22.5, 10.7, 0.04) + 16.0 + 0.35 + 0.22 ≈ 39 ms（恢复虚拟机），
39 + 0.05 + 0.11 + 0.28 ≈ 42 ms（总耗时），与报告一致。

> 注意：`总耗时` 是 orchestrator `ResumeSandbox` 函数整体耗时，**含**恢复 VM 与
> orchestrator 同步调 envd `/init`（即「启动 envd」三行），但**不含** API 网关、
> 准入排队、proxy 侧 envd 就绪轮询等，所以客户端感受到的 `Sandbox.create()` 耗时会
> 明显大于它，两者口径不同（详见 `启动耗时阶段分析.md` 第 7 节）。

## 2. 前置条件

- 集群已按 `docs/zh/install.md` 部署完成（Nomad 模式），模板已构建（如 `base`）。
- **orchestrator 二进制必须是用本仓库 patch 构建的**，否则没有这些日志（见 3.1 预检）。
- 客户端环境（可以直接用 API server 节点）：
  ```bash
  pip install e2b==2.20.0 python-dotenv
  pip install matplotlib                     # 可选：仅第 3.6 步画启动甘特图时需要
  python /opt/e2b-infra/patch_e2b.py         # https -> http 补丁
  ```
  当前目录准备 `.env`（与 docs/zh/usage.md 一致）：
  ```env
  E2B_API_KEY="e2b_xxx"
  E2B_ACCESS_TOKEN="sk_e2b_xxx"
  E2B_DOMAIN="xxx"
  E2B_API_URL="http://{server_ip}:3000"
  E2B_HTTP_SSL="false"
  ```
  也可 `cp .env.example .env` 后用 `bash sync-env.sh` 从磁盘自动填入 token（见 `single-node-offline-deploy.md` §12）。
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
python run_benchmark.py --template base --count 1 --warmup 0 --kill-each
bash collect_logs.sh
grep -h "\[ResumeSandbox\]" runs/latest/orchestrator-logs/*.log | tail -15
```

能看到 `enter` / `... cost: x ms` / `total cost` 一组日志即可继续。
**看不到则说明部署的 orchestrator 不是 patch 版本，先解决这个再压测。**

### 3.2 正式压测（100 个沙箱）

```bash
python run_benchmark.py --template base --count 100 --concurrency 1 --warmup 3
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

脚本结束时会在 `runs/run_<时间戳>/`（如 `runs/run_20260629_142530/`）下保存本次所有
产物，记录 `runs/.latest` 指针，写入 `meta.json`（含压测时间窗口、期望数量等），并打印
客户端整体耗时统计与**简化后的两步后续命令**，直接复制执行即可。后续的采集与分析会
自动定位到这个运行目录，无需再传时间窗口/数量。

### 3.3 采集 orchestrator 日志

```bash
bash collect_logs.sh
```

- 默认自动写到最近一次运行目录的 `orchestrator-logs/`（按 `--run-dir` >
  `BENCH_RUN_DIR` > `runs/.latest` 定位）；采集某个历史运行用
  `bash collect_logs.sh --run-dir runs/run_<时间戳>`，自定义 job 名仍是第一个位置参数。
- 多个 client 节点时脚本会遍历全部 running allocation；**日志必须收齐**，
  否则落在其他节点的沙箱 trace 会缺失。
- orchestrator 自压测开始后不能重启过（重启会换 allocation、丢 stdout 日志）。

### 3.4 生成统计报告

用 3.2 结束时打印的命令，最简形式：

```bash
python parse_report.py
```

不带位置参数时，自动定位最近一次运行目录，读取其 `orchestrator-logs/*.log`，并从
`meta.json` 自动填入 `--expected` 与统计时间窗口（运行了多少沙箱就分析多少个）。

| 参数 | 说明 |
|---|---|
| `--run-dir` | 指定运行目录（默认 `runs/.latest`），重新分析历史某次运行时用它 |
| `--since/--until` | 统计时间窗口（默认取自 `meta.json`，把跨多次运行累积的日志隔离成本次） |
| `--expected N` | 有效沙箱数不等于 N 时告警（默认取自 `meta.json`） |
| `--sort` | 沙箱列排序：`traceid`（默认）或 `time` |
| `--tz` | 报告显示时区，默认 `+08:00` |

输出到本次运行目录的 `report/`：

| 文件 | 内容 |
|---|---|
| `report_wide.csv` | 行=阶段，列=沙箱1..N，右侧追加 平均/P50/P90/P95/最大 汇总列；UTF-8 BOM，Excel 直接打开 |
| `report_long.csv` | 每行一个沙箱，便于透视/画图 |
| `summary.csv` | 各阶段 min/avg/p50/p90/p95/p99/max |
| `intervals.csv` | 每个沙箱的开始/结束时间（两行），供 `visualize_intervals.py` 画图（见 3.6） |

控制台同时打印 summary。

### 3.5 判断是否复现

上游报告只给出 4 个完整沙箱样本（其 100 个的完整数据未提供），下面把这些参考量级
列出来供**人工对照**（不再随脚本生成对比表）。建议按阶段看：

- **总耗时 / 恢复虚拟机**：参考约 31~47 ms。本次 avg/p50 落在同一量级（几十 ms）
  即可认为复现；差 2~3 倍以上再按下面拆阶段定位。
- **等待firecracker启动**（参考 ~22 ms）：反映 CPU 单核性能与 FC 进程拉起速度。
- **等待uffd sock**（参考 ~10.5 ms）：数值非常稳定，适合做机器间基线对比。
- **加载快照**（参考 5~16 ms）：反映快照文件 IO/页缓存，受磁盘类型影响最大。
- **准备 rootfs**（参考多数 0.03 ms，偶发 38 ms）：长尾来自 NBD 设备连接，
  关注 p95/max 而不是 avg。
- **获取网络槽位**（参考 ~0.05 ms）：网络池命中时接近 0；如果本次普遍偏大，
  说明池子没预热（加大 `--warmup` 或等 `[Pool Status]` 日志显示池子充足后再压）。

### 3.6 可视化（可选）

`parse_report.py` 会在 `report/` 下写出 `timeline.csv`（每行一个「沙箱-阶段」区间，时间来自
各阶段埋点日志时间戳）。`visualize_intervals.py` 据此出 **3 张图**（y 轴都用从上到下 1..n 简单编号）：

```bash
python visualize_intervals.py           # 自动定位最近一次运行目录 → report/*.png
```

- `timeline.png` =「**真实时间轴 + 彩色分阶段 + 并行重叠**」（合并图）：每沙箱一条，按真实时刻摆放
  各阶段、按阶段上色；并行段（`configure`∥`uffd`∥`rootfs`）在同一条内用泳道分层显示重叠。一眼能看到：
  高并发下灰色「准入排队」排成阶梯（每 cap 一波）、红色 `fc socket wait` 多长/是否随波变长、蓝色 `uffd` 与红色并行。
- `total_gantt.png` = **单色 total 甘特**：每沙箱一条 total 区间（enter→total），按开始时间排序，底部附启动耗时统计。
- `stage_durations.png` = **分阶段堆叠**：每沙箱把各阶段 duration 首尾相接堆叠、按阶段上色，看耗时构成（不反映并行重叠）。

详见 `高并发瓶颈定位方案.md` 第 6 节。

- 需要 matplotlib（`pip install matplotlib`）；无图环境也会在控制台打印各阶段平均时长。
- 不带参数时按 `--run-dir` > `BENCH_RUN_DIR` > `runs/.latest` 定位运行目录；
  画历史某次用 `python visualize_intervals.py --run-dir runs/run_<时间戳>`，
  或直接 `python visualize_intervals.py path/to/timeline.csv`。

## 4. 注意事项 / FAQ

1. **有效沙箱数 < expected**：时间窗口不准（时钟偏差）、多节点日志没收齐、
   部分创建失败（看 run_benchmark 输出的 FAIL）、或 orchestrator 日志被轮转。
2. **时钟偏差**：`--since/--until` 用的是客户端时间，与 orchestrator 节点时钟比对。
   不在同一台机器时先 `chronyc tracking`/`ntpstat` 确认同步（`meta.json` 的时间窗口据此隔离本次运行）。
3. **首批数据异常大**：模板缓存冷启动。属正常现象，预热即可；如果想测冷启动，
   单独记录第一个沙箱的数据，不要混入统计。
4. **资源不够跑 100 个并存**：用 `--kill-each`。注意这会改变测试形态
   （NBD/网络槽位复用模式不同），对比时注明。
5. **多次压测**：每轮自动隔离在各自的 `runs/run_<时间戳>/` 里，互不覆盖，无需手动区分目录；
   重新分析某一轮用 `python parse_report.py --run-dir runs/run_<时间戳>`。
6. **envd 三行现已埋点**：patch 已在 `sandbox.go`/`envd.go` 对「启动 envd」阶段埋点
   （`start envd` / `envd init request` / `read envd response`），正常压测即可统计到。
   它量的是 orchestrator 同步调 envd `/init` 的耗时（在 `total` 计时内），从快照恢复时
   envd 已在运行，通常只有几 ms；这**不等于**客户端感受到的「envd 就绪」（后者还含
   proxy 轮询/排队/UFFD 缺页，在 `total` 之外，详见 `启动耗时阶段分析.md` 第 7 节）。
   若上游旧报告该三行为空，是因为旧 patch 未埋点。
7. **日志格式**：parse_report.py 同时支持 zap JSON 行和纯文本行，时间字段
   兼容 `timestamp/ts/time`（ISO 字符串或 epoch 秒/毫秒/纳秒）。

## 5. 文件清单与运行目录

| 文件 | 用途 |
|---|---|
| `run_benchmark.py` | 客户端压测：批量创建/清理沙箱，记录客户端耗时，建运行目录并输出后续命令 |
| `collect_logs.sh` | 从 Nomad 采集所有 orchestrator allocation 的 stdout/stderr 日志 |
| `parse_report.py` | 解析 `[ResumeSandbox]` 日志，生成报告 CSV、`timeline.csv`、`intervals.csv`（仅标准库） |
| `visualize_intervals.py` | 读 `timeline.csv`/`intervals.csv` 出 3 张图：合并甘特 + 单色 total 甘特 + 分阶段堆叠（需 matplotlib，见 3.6） |
| `.env.example` / `sync-env.sh` | `.env` 模板与凭据同步脚本（从磁盘填 E2B/Nomad token，见 `single-node-offline-deploy.md` §12） |
| `启动耗时阶段分析.md` | 各阶段在源码里的位置/嵌套关系/端到端口径分析 |
| `高并发瓶颈定位方案.md` | **高并发瓶颈定位的持续迭代方案**：准入排队、`等待fc启动` 拆分、甘特可视化 |

每次 `run_benchmark.py` 会在 `runs/` 下新建一个运行目录，一次压测的全部产物都归集在内：

```
runs/
├── .latest                       # 指针：最近一次运行目录名（采集/分析据此自动定位）
├── latest -> run_20260629_142530 # 便捷符号链接（尽力维护，可能在不支持的平台缺失）
└── run_20260629_142530/
    ├── meta.json                 # 压测窗口/期望数量/模板等，采集与分析据此自动填参
    ├── bench-<时间戳>.json        # 客户端耗时明细
    ├── bench-<时间戳>.client_times.csv
    ├── orchestrator-logs/        # collect_logs.sh 采集的日志
    └── report/                   # parse_report.py 生成的报告 CSV + timeline.csv + intervals.csv，
        ...                       # visualize_intervals.py 出的 timeline.png/total_gantt.png/stage_durations.png 也在这里
```

`collect_logs.sh` 与 `parse_report.py` 不带参数时，按 **`--run-dir` > 环境变量
`BENCH_RUN_DIR` > `runs/.latest`** 的顺序定位运行目录。`runs/` 已在 `.gitignore` 中忽略。
