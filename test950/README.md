# `test950.sh` —— 950 测试总入口

在 950 上测两套快照回滚（沙箱级、进程级）的唯一入口。它不重写测试，只做编排：

- 按档调用现有脚本：
  - 沙箱级：`rollback/scripts/950/run.sh` 的各档；
  - 进程级：`criu-rollback/deploy/950.sh` 的测试命令。
- 自动切栈，跑完停在沙箱级。切栈前删掉全部 e2b 沙箱（950 上的 e2b 只有我们在用）；同机 AgentENV 的沙箱不碰。
- 每段开头记录身份：orchestrator、Firecracker、进程级运行文件的 sha256，宿主内核，仓库提交。
- 默认转到后台运行，断开 ssh 不影响。中途停下，再执行同一条命令会从停下的那一步继续。
- e2b 网络命名空间超过 1500 个时，在下一步之前自动清理（`950.sh recycle-netns`）。
- 结果用 `bash test950.sh report` 打印到终端，复制终端内容即可，不打包、不需要拷文件。

测什么、不测什么，以及分档的理由，见工作区的《950 测试方案》。下面只讲怎么用。

## 1. 前提

- 沙箱级已按 `single-node-offline-deploy.md` 部署好。
- 测进程级之前，执行过一次 `bash criu-rollback/deploy/950.sh all`，即完成首次部署，并留下了切回沙箱级要用的基线。
- 用 root 在仓库根目录执行。
- 两个 conda 环境与 `950.sh` 用的相同：沙箱级 `jll-e2b`，进程级 `jll-e2b-criu`（`950.sh all` 克隆出来的）。如果不在默认位置，在 `criu-rollback/deploy/ops/site.env` 里写明。
- 进度文件和后台日志在 `/var/lib/test950/`，结果在仓库里的 `test950/results/`。
- 凭据和 `950.sh` 一样，从 `~/.e2b/config.json` 与 `/opt/e2b-infra/.env` 读取，不需要另外准备。

## 2. 命令

```bash
git pull
```
```bash
bash test950.sh quick
```
```bash
bash test950.sh standard
```
```bash
bash test950.sh extended
```
```bash
bash test950.sh long
```
```bash
bash test950.sh status
```
```bash
bash test950.sh report
```

默认两套都测。只测一套时加 `--stack sandbox` 或 `--stack criu`。

| 参数 | 作用 |
|---|---|
| `--hours N` | long 档每套的长跑时长（小时），默认 4；两套都测时总共约 9 小时 |
| `--new` | 不续跑上一轮，重新开始 |
| `--fg` | 在前台跑 |

上一轮有没通过的步骤时，再执行同一条命令只重跑那些步骤，已通过的保留（加 `--new` 则从头来）。一次只能跑一轮。上一轮还在跑时，新命令会拒绝执行，并提示先看 `status`。

## 3. 四档

低档是高档的子集：quick ⊂ standard ⊂ extended。long 单独成档，只包含长跑；应当先跑过 extended，再跑 long。两套都测时，最后会切回沙箱级，再跑一次沙箱级冒烟。

| 档 | 沙箱级 | 进程级 | 实测耗时 |
|---|---|---|---|
| quick | `run.sh smoke`：预检、SDK 自检、能力、FC 身份、59 项正确性 | `950.sh quick`：部署自检 + `verify_gsd.py` | 5 分钟（沙箱级 smoke 45 秒、进程级 5 秒，其余是两次切栈各约 70 秒） |
| standard | `smoke` + `func`（16 个用例、兼容矩阵、SDK 异常语义）+ `perf` + `hdbss`（当前 order） | `acceptance` + `stress`，即 `950.sh all` 里的验收与四组压测 | 920B 32 分钟（func 18 分钟、perf 2 分钟、进程级验收 3.6 分钟、压测 3 分钟）。950 约 49 分钟（smoke 50 秒、func 18.5 分、perf 9 分、hdbss 4 分、进程级验收 3 分、压测 4 分，另有切栈和 netns 清理；perf 里的并发 N=16 因一次 300 s 超时多用了约 5 分钟） |
| extended | 在 standard 之上加：<br>`special`：T18 重启、T21 四条注入、T37 两道配额，逐条改 env 并重启，共 8 次；<br>`hdbss --orders 1,9`；<br>`perf --full`：分档全表 3 遍；<br>`idle`：空闲后 restore，测法同沙箱级手册第 16 章 §7 | 在 standard 之上加 `stress-ext`：<br>八组压测各 1000 轮（含 enospc）；<br>多棵树、删文件密集各 3 个种子 200 轮，再各跑一次 1000 轮的容量耗尽（`*-capacity`）；<br>all、noworkers 各 5 个种子；<br>兼容矩阵（checkpoint → pause → resume → restore） | 1 小时 50 分（另加两次切栈）：沙箱级 func 18 分、special 9.5 分（重启 8 次）、perf --full 6 分、idle 9.5 分；进程级 stress-ext 51 分。950 上 hdbss 两项不再 SKIP，约多 20 分钟 |
| long | `run.sh soak`：16 沙箱混合稳态与 16 沙箱滚动保留各占时长的 40%，再加 T36 全规模、串行 restore 5000 次，前后对比宿主资源 | `950.sh soak`：一个沙箱里按时长连续 checkpoint / restore / delete，前后对比宿主资源 | `--hours 0.25` 试跑 37 分钟（沙箱级 20 分、进程级 15 分）；默认 4 小时时两套共约 9 小时 |

**extended 和 long 期间机器要独占**：

- extended 会重启服务约 12 次，每次重启都会让机器上所有 e2b 沙箱消失（AgentENV 的沙箱不受影响）；
- long 会同时开 16 个沙箱。

**网络命名空间**：每次重启 orchestrator 会留下约 300 个 e2b netns，920B 上 extended 一轮从 1608 涨到 7494。现在每一步开始前和整轮结束时检查，超过 1500 个（`TEST950_NETNS_LIMIT` 可改）就调用 `950.sh recycle-netns` 清理，约 1～2 分钟，清理记录列在 `report` 里；清理失败只记录，不影响测试步骤。

**HDBSS**：在没有 HDBSS 的机器上（例如 920B），`hdbss` 两项整档判 SKIP。

## 4. 看结果

跑完（或跑到一半）执行：

```bash
bash test950.sh report
```

它打印最近一轮的结果，从 `==== test950` 一行到 `==== 结束 ====` 一行：逐步状态与用时、每段身份（orchestrator、Firecracker、进程级三个运行文件的 sha256，相同的只列一次）、宿主资源、自动清理 netns 的记录，以及每一步的逐项结论（沙箱级是 `run.sh` 的逐项表，带关键数字；进程级是每个子步骤，压测附轮数、create/restore 次数、p50、错误计数）。复制这一段即可。

某一步要看细节时，把步骤名跟在后面，它打印该步各项日志的末尾（失败项多打印一些）；进程级验收会打印性能三轮的分档表：

```bash
bash test950.sh report sandbox/perf
```
```bash
bash test950.sh report criu/stress-ext
```

还要看某个文件时，先列出文件，再用 `--file` 打印（`--lines N` 指定行数，0 为全部；`--run 目录名` 看更早的一轮）：

```bash
bash test950.sh report files
```
```bash
bash test950.sh report --file criu-stress-ext/deldense-capacity/RESULT --lines 0
```

含密钥的文件（nomad job 定义、dotenv）不打印。

- 结果目录：`test950/results/<时间>-<档>/`，内有 `SUMMARY.md`、`run.json`、`identity-*.json` 和每一步的子目录（该脚本自己的 `SUMMARY.md` 或 `summary.json`，以及原始数据）。
- 每一步的判定：
  - 沙箱级看 `run.sh` 的退出码，等于 FAIL 项数；
  - 进程级看 `summary.json` 的 `verdict`；
  - 性能数字只记录，不判定。
- 用途：数字填进两本手册的 950 章节，即沙箱级第 18、21、22 章，进程级第 22 章。

## 5. 已知会 FAIL 的项

2026-11-16～17 在新 920B 上逐档演练、11-17 在 950 上跑 standard，下面几项在修复前会判 FAIL，结果里看到它们不必另查：

| 步骤 | 项 | 原因 | 状态 |
|---|---|---|---|
| `criu/stress-ext` | `compat_matrix` 的 B3、B4、B6/B7、E1 | 进程级 SDK 的 `Sandbox.connect` 没带 `E2b-Sandbox-Id` 头，代理以 `missing header` 打回 checkpoint 请求。用原 Sandbox 对象做 pause → resume → restore（B8）通过 | 产品缺陷，待修 SDK |
| `criu/stress-ext` | `multitree-capacity` | 1000 轮把 snapshot store 涨到 guest 内存的九成，容量预检没挡住，guest OOM 杀了用户进程 | 产品缺陷，见进程级手册第 23 章 |
| `criu/stress-ext` | `deldense-capacity` | 1000 轮把根盘写满，多数失败是安全的（`internal`，"nothing was changed"），但有 4 次 restore 返回 `data_loss` | 产品缺陷，见进程级手册第 23 章 |
| `sandbox/perf` | 并发 `checkpoint_concurrent.py` | 950 上 A 段 N=16 有 1 次 restore 服务端约 140 ms 已完成并写回应答，客户端没收到，等满 300 s 超时（2026-11-17）；数据未受影响。丢在 orchestrator 代理、client-proxy、SDK 之间，见沙箱级手册第 21 章 §2.3 | 待查，计划在 920B 循环复现 |
| `sandbox/special` | `crtest T18` | `nomad job restart` 原地重启后，旧沙箱的 Firecracker 进程 90 秒后仍在。手工复现两次都在 10 秒内退出，原因未查 | 待查 |

`stress-ext` 里各组互不依赖，一组失败不影响其余组的结果。

偶发、不稳定出现的：滚动保留里个别 delete 报客户端 `ReadError: Bad file descriptor`，是沙箱级手册第 30 章 §1 的客户端连接异常，restore 的逐项校验不受影响。
