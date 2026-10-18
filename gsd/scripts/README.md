# `gsd/scripts/` —— 进程级 checkpoint / restore 的测试脚本

手册 [19 章](../docs/19-testing-and-debug-kernel.md)（每个脚本测什么、判据怎么设计的）与
[26 章](../docs/26-acceptance-runbook.md)（上机验收的顺序、确切命令、期望末行）讲的就是这里的脚本。
本页是速查：每个脚本一行，做什么、怎么跑、判据看哪里。

```
e2b_env.py          公共模块：读客户端连接参数与凭据，三个子目录的脚本共用
build_template.py   建模板（对 SDK 的 Template.build 的封装）
acceptance/         功能验证与性能：验收时按顺序跑的五个脚本
stress/             压测、看门狗、取串口输出、汇总判定
probes/             探针：为查清某一个具体问题写的小脚本，输出对着文件头的说明读
ops/                切栈脚本（新 920B：RPM 部署 + nomad）
kernel/             调试内核的 config 与构建说明
```

端到端脚本只在**新 920B**（arm64）验证，950 的 gsd 尚未实测；单元测试和结果汇总也可离线运行。完整日志与原始结果不入库，复核摘要见 [validation/](../validation/README.md)。

## 前置条件

在宿主机上跑。逐条的核对命令在 [26 章 §1](../docs/26-acceptance-runbook.md)，这里只列要点：

1. **gsd 栈在运行**：宿主上是带 gsd 注入的 orchestrator，补丁内核与 gsd 二进制在位（[23 章](../docs/23-deployment.md)）。
   同一个端口同时只能跑一套栈；机器与别人共用时，起沙箱、切栈之前先打招呼。
2. **模板 `base-gsd`**：在 gsd 栈下建的、2 vCPU / 2048 MB 的模板，所有脚本的默认模板名都是它。
   模板固化了建模板时的内核、gsd 与 overlay-init，三者换了任何一样都要重建，否则测的还是旧的。
3. **Python 环境与 SDK**：用装好 SDK 依赖的 Python 环境（3.10 或更新），并让 `PYTHONPATH` 指向代码仓的 `py-sdk` 目录：

   ```bash
   export PYTHONPATH=<代码仓>/py-sdk
   python -c 'import e2b; print(e2b.__file__)'      # 应落在 <代码仓>/py-sdk 下
   ```

   落在别处说明用到的是一份没有 checkpoint 接口的 SDK，表现为返回值里缺字段、若干断言失败。
4. **凭据**：脚本通过 `e2b_env.py` 读取，取值顺序是：已导出的环境变量 → `scripts/.env` → `~/.e2b/config.json`
   （`e2b auth login` 写出的）→ 内置默认值。`.env` 的格式：

   ```
   E2B_API_URL=http://localhost:3000
   E2B_DOMAIN=e2b.app
   E2B_HTTP_SSL=false
   E2B_ACCESS_TOKEN=<token>
   E2B_API_KEY=<key>
   ```

   `E2B_ACCESS_TOKEN` 与 `E2B_API_KEY` 没有默认值，三处都找不到时脚本直接退出并提示。**`.env` 含凭据，不入库**。
   确认读得到（在 `scripts/` 下）：

   ```bash
   python -c 'import sys; sys.path[:0]=["."]; from e2b_env import load_e2b_env; print(sorted(load_e2b_env()))'
   ```

下文的命令都在 `scripts/` 目录下执行。

## 脚本之间的依赖

- 每个 Python 脚本在**自身所在目录和上一级目录**里找 `e2b_env.py`，所以它放在 `scripts/` 下，三个子目录共用。
  要把某个脚本单独拷走，把 `e2b_env.py` 一起拷到它旁边即可。
- `acceptance/verify_multitree.py` 复用 `acceptance/verify_fixes.py` 里的沙箱封装（`import verify_fixes`），两个文件必须在同一个目录。
- `stress/run_stress_watch.sh` 调用同目录的压测脚本与 `fc_console.sh`。
- `stress/analyze_stress.py` 只用标准库，不起沙箱，可以在任何机器上对拷回来的结果目录跑。
- 除此之外各脚本互不依赖。

## 多数脚本的退出码不是判定

这些脚本是在排查过程中逐步长出来的，判定方式并不统一，**不要只看返回码**。下面各表的"判据"一栏写的是该看的那一行；
"退出码"一栏标"可信"的，才可以直接用返回码做自动化判断。

## `build_template.py`

```bash
python build_template.py --alias base-gsd --memory 2048 --image <镜像仓库>/e2b-orchestration/ubuntu:22.04-custom-criu421
```

镜像里要带 CRIU 4.2.1。不带 `--use-cache` 即不使用构建缓存。`--image` 的默认值取环境变量 `GSD_TEMPLATE_IMAGE`。
构建结束时 orchestrator 会自检（gsd 服务、`/health`、criu 版本），自检不过构建就失败。末行 `template 'base-gsd' built in …s`。

## `acceptance/` —— 功能验证与性能

验收时按表里的顺序跑，前一步不过就停下来查。

| 脚本 | 做什么 | 怎么跑 | 判据 | 退出码 |
|---|---|---|---|---|
| `verify_gsd.py` | 部署自检加一条最短的性能线：模板里是不是预期的 gsd 与内核；连续 restore 会不会吃根盘 | `python acceptance/verify_gsd.py base-gsd 8` | 末行 `判定: 根盘恒定 ✓`；开头四行环境信息人工核对（提交号、`rcu_expedited` 为 1、THP 为 `[never]`） | 不可信 |
| `verify_fixes.py` | 问题清单逐条的现场验证，28 项，每项独立起沙箱 | `python acceptance/verify_fixes.py --template base-gsd --out-json x.json --out-md x.md` | 末行 `合计 28 项，FAIL 0: []`；PASS 26 / FAIL 0 / SKIP 2（G-19、G-36 恒为 SKIP） | 可信 |
| `verify_multitree.py` | 多棵用户进程树、`/tmp` 回滚、根目录 755，共 13 项（V0～V12）；V12 是可选的性能对照 | `python acceptance/verify_multitree.py --template base-gsd --skip V12 --out-json x.json` | 末行 `合计 N 项，FAIL 0: []`（N 的期望值见 26 章），其后一行慢调用统计（不影响判定，次数不为 0 要留档） | 可信 |
| `verify_clear_refs_race.py` | 增量 dump 会不会漏页：限速写内存的进程，每轮比对 restore 后的每一页 | `python acceptance/verify_clear_refs_race.py --template base-gsd --rounds 30 --pages 4096 --rate 100000` | `有漏页的轮数: 0 / 30   漏页合计 0`；每轮 `restored_state=T`、`sha_eq=True` | 不可信 |
| `demo_checkpoint_perf.py` | 六档独立负载大小：每档新沙箱，只分配本档内存，改写后恢复本档基点；记录实际 RSS、墙钟与服务端分段；每档单样本，建议跑三次 | `TEMPLATE=base-gsd THP=never OUT_JSON=x.json python acceptance/demo_checkpoint_perf.py` | 6 行恢复全部 `OK`，完整负载 SHA256 与文件内容一致；SDK/journal 最大差 0.000 ms；末行 `判定: PASS` | 可信 |

性能脚本自 2026-10-16 起采用 `schema_version=2`、`measurement_mode=per-tier-size`。
六档额外负载内存分别为 **0 / 12 / 24 / 48 / 96 / 192 MiB**，文件为 **0 / 4 / 8 / 16 / 32 / 64 MiB**。
每档独立执行：启动本档大小的进程 → 初始化内存和文件 → 建全量基点 → 改写本档内存和文件 → 测增量 create → 测 restore 基点 → 校验 → 删除沙箱。
全量基点单列；负载构造、RSS 采集、SHA256 与文件校验均在计时窗口之外。每档记录的是新沙箱的第一次 restore，不混入同沙箱热恢复。
SDK 返回后 journal 可能尚未可读；采集按 checkpoint ID 和 create / restore 完成事件匹配，并在计时窗口之外有界重试，仍缺字段则判失败。
0 档不分配额外负载缓冲区，但仍有 Python 解释器、通信与标记等开销；输出的 RSS 包含这些实际内存。
这组测试同时改变进程负载大小和改写量，不能用于单独估计固定进程大小下的改动量效应。
[20 章 §3.3](../docs/20-results-and-gaps.md#33-demo_checkpoint_perfpy六档改动量10-08-历史口径) 保留的是 10-08 的历史结果：固定 192 MiB、连续建链后逐个恢复。
新旧数据不能混合统计，也不能用历史数值预测新版脚本的结果。失败时退出码非零，`OUT_JSON` 保留已完成的部分结果和错误。

三轮结果汇总（不连接沙箱，失败返回非零）：

```bash
python acceptance/summarize_checkpoint_perf.py \
  <结果目录>/perf-run1.json <结果目录>/perf-run2.json <结果目录>/perf-run3.json \
  --output <结果目录>/perf-summary.json
```

汇总校验测试口径、各档负载、基点与父节点、完整恢复内容及 SDK/journal，再生成逐轮样本和中位数。
`demo_checkpoint_perf.py` 负责采样，`test_demo_checkpoint_perf.py` 负责离线逻辑回归，`summarize_checkpoint_perf.py` 负责结果复核，三个职责分别记录。

本机离线回归（不需要 SDK、凭据或沙箱）：

```bash
python3 -m unittest discover -s acceptance -p 'test_demo_checkpoint_perf.py' -v
```

`verify_fixes.py` 与 `verify_multitree.py` 都支持 `--only <项,项>` 与 `--skip <项,项>`；`verify_multitree.py` 的 V12
要带对照时加 `--old-template <对照模板>`。`verify_clear_refs_race.py` 可用 `--gsd-binary <宿主上的 gsd 文件>` 换上另一版 gsd 做反向对照。

## `stress/` —— 压测

| 脚本 | 做什么 | 怎么跑 | 判据 | 退出码 |
|---|---|---|---|---|
| `run_stress_watch.sh` | 看门狗：后台起压测脚本，日志停滞超过 `STALL` 秒判定 guest 卡死，取串口输出、杀脚本和沙箱 | `bash stress/run_stress_watch.sh <输出目录> <压测脚本的参数…>`；换脚本用 `SCRIPT=<文件名>` | `<输出目录>/RESULT`：`DONE rc=0 …` 正常，`WEDGED …` 卡死 | 透传被看护脚本的；卡死为 2 |
| `stress_overlay_ckpt.py` | 层切换与并发文件操作：root worker 不停地做文件操作，主循环随机 create / restore / delete；另有 `enospc` 模式（根盘写满时的行为） | `bash stress/run_stress_watch.sh <输出目录> --template base-gsd --mode stress --rounds 300 --workers 6` | 日志末尾的 `判定:` 行与 `stress-summary.json`，按 [19 章 §5.4](../docs/19-testing-and-debug-kernel.md) 的规则判，或交给 `analyze_stress.py` | 不可信 |
| `stress_multitree.py` | 三棵用户进程树的自检负载：内存、文件、fd 三者必须回到同一时刻 | `SCRIPT=stress_multitree.py bash stress/run_stress_watch.sh <输出目录> --template base-gsd --mode multitree --rounds 200` | `结果 PASS` 行；`multitree-summary.json` 的 `verdict` | 可信 |
| `stress_delete_dense.py` | 删文件密集：反复删下层文件与目录再重建，每次 restore 后逐条核对文件清单 | `SCRIPT=stress_delete_dense.py bash stress/run_stress_watch.sh <输出目录> --template base-gsd --mode deldense --rounds 200` | `结果 PASS` 行；`deldense-summary.json` 的 `verdict` | 可信 |
| `analyze_stress.py` | 汇总一批压测输出目录并逐组判定（dmesg 新增报错、串口命中、worker errno、心跳、`debug_locks`） | `python stress/analyze_stress.py <各组输出目录的上一级> <目录名前缀> > summary.json` | 每组的 `"PASS": true` | 恒为 0 |
| `fc_console.sh` | 从 orchestrator 的 nomad 日志里取某个沙箱的 guest 串口输出（内核崩溃时 oops / KASAN 报告只能从这里看） | `bash stress/fc_console.sh <沙箱 id> [输出文件]` | 无；输出每行带宿主机时间，用来和 `<mode>-ops.log` 对时间 | — |
| `stress_gsd.py` | 最早的连续 restore 冒烟：负载同 `verify_gsd.py`，沙箱寿命更长，多打印可用内存与进程数 | `python stress/stress_gsd.py base-gsd 200` | 末行根盘判定 | 不可信 |

- `--mode` 的取值决定输出文件名的前缀，看门狗靠它找日志，要与上表一致。
- `run_stress_watch.sh` 的环境变量：`PY`（解释器，默认 `python3`）、`SCRIPT`（默认 `stress_overlay_ckpt.py`）、`STALL`（默认 90 秒；
  并行跑多组或在调试内核上跑时调大）；`PYTHONPATH` 沿用调用者的环境。
- `fc_console.sh` 依赖本机的部署方式，换机器用 `E2B_INFRA_ENV`（默认 `/opt/e2b-infra/.env`）与 `NOMAD_JOB`（默认 `template-manager-system`）调整。
- `analyze_stress.py` 按"`<前缀>-*`"找各组目录，所以各组的输出目录要起成 `<前缀>-<组名>` 的样子（例如 `rel-all`、`rel-multitree`）。
- `stress_overlay_ckpt.py` 完整的八组分法（各组的 `--ops` 写法）在 [19 章 §5.1](../docs/19-testing-and-debug-kernel.md) 的分组表里。
- `stress_gsd.py` 是最早的连续 restore 冒烟，入库后没有重新上机跑过；日常冒烟用 `verify_gsd.py` 并把次数调大即可。
- 发布版内核与调试内核上的压测要各跑一遍；调试内核见 [`kernel/README.md`](kernel/README.md)。

## `probes/` —— 探针

各自起沙箱、造出某个问题需要的最小形状、把现场打印出来。大多没有统一的 PASS / FAIL，输出要对着文件头的说明读；
每个探针针对的问题在哪一章展开，见 [19 章 §6](../docs/19-testing-and-debug-kernel.md) 的表。

| 脚本 | 针对的问题 | 怎么跑 |
|---|---|---|
| `probe_whiteout_bug.py` | restore 后删下层文件触发内核 `BUG`（`bug` 及对照 `ctl`、`nodel`、`masked`）；trap inode 悬空导致 `ELOOP` / `ENXIO` / `ENOTDIR`（`trap`）。**验收里唯一会跑的探针**：每个用例一行，应 `crashed=False`，`trap` 用例 `errs={}` | `python probes/probe_whiteout_bug.py base-gsd --cases bug,ctl,nodel,masked,trap,trap,trap > x.json 2> x.log` |
| `probe_fd_rollback.py` | checkpoint 前打开的 fd，之后经它写入的内容 restore 后还在不在（`fd`）；循环之后根盘占用是否回落（`df`） | `python probes/probe_fd_rollback.py fd [模板]`、`… df [模板]` |
| `probe_mmap_rollback.py` | 被映射着的文件在 checkpoint 之后的写入是否回滚、是否写穿进沉下去的层 | `python probes/probe_mmap_rollback.py [模板]`（`PHASE2=0` 关第二阶段） |
| `probe_move_cost.py` | checkpoint 时把以写方式打开的文件留在 upper 的代价（0 / 1 / 16 / 64 / 128 MiB） | `python probes/probe_move_cost.py [模板]` |
| `probe_estale.py` | restore 后新进程按绝对路径访问目录会不会得到 `ESTALE`，十种场景 | `python probes/probe_estale.py [模板]` |
| `probe_estale_file.py` | 同一问题的非目录分支 | `python probes/probe_estale_file.py [模板]` |
| `probe_rename_race.py` | `rename` 偶发 `ENOTEMPTY` 是不是 restore 夹在一次 rename 的两次路径查找之间 | `python probes/probe_rename_race.py [模板] [restore 次数]` |
| `probe_leak.py` | 长循环里根盘上已删除但仍被占着的空间与内核对象是否随轮数增长；另有持有者阶段与给 KASAN 看的释放顺序阶段 | `python probes/probe_leak.py <模板> [--rounds 200] [--out x.json]` |
| `probe_umount.py` | 回收逻辑的卸载分支：在 guest 里另挂一个小 overlay，轮流走五种释放顺序 | `python probes/probe_umount.py <模板> [轮数]` |
| `probe_user_roots.py` | 各种常见用法下 uid 1000 进程树的形状、有几个根。只读，不调 checkpoint 接口 | `python probes/probe_user_roots.py [模板]` |
| `probe_proc_cost.py` | guest 里扫一遍 `/proc` 的开销 | `python probes/probe_proc_cost.py [模板]` |
| `probe_tpl_env.py` | 并排打印两个模板的 guest 环境，定位模板之间的性能差异 | `python probes/probe_tpl_env.py <模板> <模板>…` |
| `probe_first_rpc.py` | 新沙箱上第一次 gsd 请求的墙钟耗时，经宿主代理与 guest 内直连两种方式 | `python probes/probe_first_rpc.py base-gsd 20 > out.json` |
| `exp_gsd_swap.py` | 在同一个模板上热换 guest 里的 gsd 二进制，逐轮交错对比 restore 各分段（定位性能差异） | `python probes/exp_gsd_swap.py --template base-gsd --rounds 2 --n 8 --variant a=<gsd 文件> --variant b=<gsd 文件> --out x.json` |

`[模板]` 省略时都是 `base-gsd`。

## `ops/` —— 切栈

`ops/switch-gsd-new920b.sh` 针对新 920B 的部署方式（RPM 部署，orchestrator 由 nomad 的 job `template-manager-system` 拉起、监听 5008）写的，
换机器要照它的思路改。逐步行为见 [23 章 §4](../docs/23-deployment.md)。

```bash
bash ops/switch-gsd-new920b.sh status   # 只看现状，不改任何东西
bash ops/switch-gsd-new920b.sh gsd      # 切到 gsd 栈；还有 firecracker 在跑就拒绝
```

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `GSD_STACK_DIR` | `$HOME/gsd-stack/stack` | 放基线 job 与生成的 job 定义的目录。生成的文件含 job 里的密钥，以 600 权限保存 |
| `GSD_JOB_BASE` | `$GSD_STACK_DIR/job-tm-new920b.base.json` | 基线 job 定义。事先在**非 gsd 栈**运行时用 `nomad job inspect template-manager-system` 存一份 |
| `E2B_INFRA_ENV` | `/opt/e2b-infra/.env` | 含 `NOMAD_ACL_TOKEN` 的环境文件 |
| `GSD_ORCH_BIN` / `GSD_BIN` / `GSD_KDIR` | `/fc-gsd/orchestrator`、`/fc-gsd/gsd`、`/fc-gsd/kernels` | 调试时临时换一套文件，例如 `GSD_KDIR=/fc-gsd/kernels-dbg` 换上调试内核 |

判据：末尾打印 `切换完成` 与"切换后"的现状，其中 `=> gsd 栈`、`health : 200`；超时未确认则以非零退出。

## `kernel/` —— 调试内核

`kernel-debug.config`（完整 config）、`kernel-debug.fragment`（相对发布版的差异）与 [`README.md`](kernel/README.md)（怎么编、为什么必须关 `VMGENID`、怎么用）。
