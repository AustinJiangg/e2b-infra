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
bash test950.sh native
```
```bash
bash test950.sh compare
```
```bash
bash test950.sh status
```
```bash
bash test950.sh report
```

默认两套都测。只测一套时加 `--stack sandbox` 或 `--stack criu`。`native`、`compare` 是两项单独的性能测量，不在四档里，见第 6 节。

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

2026-11-16～17 在新 920B 上逐档演练、11-17 在 950 上跑 standard，发现下面五项。五项都已在 2026-11-17 修复；2026-11-18 在 920B 上用最终版本（RPM `2026.09-4`、gsd `daae6968f`）完整跑了一遍 extended，五项都通过。第五项改的是沙箱级 orchestrator，随 RPM `2026.09-4` 发布，950 要重新构建并部署 RPM 才生效。

| 步骤 | 项 | 原因 | 状态 |
|---|---|---|---|
| `criu/stress-ext` | `compat_matrix` 的 B3、B4、B6/B7、E1 | 进程级 SDK 的 `Sandbox.connect` 没带 `E2b-Sandbox-Id` 头，代理以 `missing header` 打回 checkpoint 请求 | 已修（SDK `ec1558c94`）；920B 复验 OK 15 / REFUSED 1 / BROKEN 0 |
| `criu/stress-ext` | `multitree-capacity` | 1000 轮把 snapshot store 涨到 guest 内存的九成，容量预检只要求放得下镜像，guest OOM 杀了用户进程 | 已修（gsd 预检要求 dump 后至少留 MemTotal 的 1/8）；920B 复验 PASS，无 OOM |
| `criu/stress-ext` | `deldense-capacity` | 1000 轮把沙箱根盘写满。restore 杀掉用户进程之后，层切换要在内核里新建 `work/work` 目录，ENOSPC，只能报 `data_loss`（3～4 次） | 已修（gsd 常驻预留空间、杀进程前按 `f_bavail` 检查、根盘写满报 `resource_exhausted`）；这一组改用 `--capacity` 判据：只允许安全失败。920B 复验见下 |
| `sandbox/perf` | 并发 `checkpoint_concurrent.py` | A 段 N=16 有 1 次 restore 客户端等满 300 s。根因在客户端：SDK 全部沙箱共用的 httpcore 连接池在连接总数超过 20 时，会关掉刚分给另一个线程的连接（沙箱级手册第 21 章 §2.3） | 已修（SDK keepalive 上限默认等于最大连接数）；920B 4800 次 restore 从 6 次失败降到 0 |
| `sandbox/special` | `crtest T18` | `nomad job restart` 时 orchestrator 关网络池超过 job 的 30 s kill_timeout，被 SIGKILL；Firecracker 在自己的进程组里，没被带走，由 init 收养。RPM 版 orchestrator 不含 deltabox 分支里启动时清理遗留 Firecracker 的代码（`fc/orphans.go`），所以一直留着 | 已修（RPM `2026.09-4`：orchestrator 启动时结束父进程为 init 的遗留 Firecracker）；920B 复验 T18 通过，FC 在新 orchestrator 起来时即被清掉 |

`stress-ext` 里各组互不依赖，一组失败不影响其余组的结果。

2026-11-18 起 `stress-ext` 的 `append` 组按容量耗尽判：修好"restore 之后的第一次 checkpoint 是全量"（进程级手册第 16 章 L-18）之后，guest 内的镜像区不再先满，这一组 1000 轮会把沙箱根盘写满；6 个 worker 是 restore 管不到的 root 进程、一直追加，盘满后 checkpoint 和 restore 都只能安全拒绝（L-19）。判据允许 `resource_exhausted` 和 worker 的 ENOSPC，`data_loss`、`internal`、内核报错、校验失败仍判失败。

**920B 2026-11-18 extended 实测**（RPM `2026.09-4`，gsd `daae6968f`）：沙箱级 smoke、func（PASS 18 · SKIP 4）、perf（含 N=16 并发）、special（T18、T21 四条、T37 两道全过；T38 要带故障注入的 FC，不跑）、perf --full、idle 全部 PASS，hdbss 两项整档 SKIP；进程级 acceptance（含新加的 `restore_incremental`）、stress PASS；stress-ext 27 组：26 组按原判据 PASS，`append` 按上面的判据离线重判 PASS；兼容矩阵 OK 15 / REFUSED 1 / BROKEN 0。各步用时合计约 85 分钟（另有切栈）。

沙箱级的两项修复都在 RPM `2026.09-4` 里（orchestrator 与 SDK 覆盖层），按部署文档重新构建、部署 RPM 后生效；只想先拿到连接池修复而不重新部署时，可对客户端 conda 环境单独执行一次 `e2b-deploy/dep/e2b-sdk-checkpoint/install.py`（装完会自动重放 `patch_e2b.py`）；进程级的 gsd 与 SDK 由 `950.sh criu` 自动更新（`criu-rollback/deploy/README.md` 第 14 节）。

## 6. 两项单独的性能测量：`native` 与 `compare`

四档测的是两套快照回滚本身。下面两项用来回答"和 e2b 原生 pause / resume 比快多少""两套为什么快慢不同"，与四档互不包含，各自单独执行，默认两套都测，跑完停在沙箱级（最后同样做一次沙箱级冒烟）。

| 命令 | 测什么 | 脚本 | 每套样本 |
|---|---|---|---|
| `bash test950.sh native` | e2b 原生 pause / resume，以及 `create_snapshot` → 从快照新建沙箱（snapshot 模式）。在沙箱级、进程级两套代码上各跑一遍 | `benchmark/native_snapshot_tiers.py --mode both --passes 3 --iters 8` | 六档（0～256 MiB，内存:文件 = 3:1），每档每种模式 24 个增量样本 + 3 个首次样本 |
| `bash test950.sh compare` | 同一种负载下的 checkpoint / restore：两套用同一个常驻进程（P MiB 的内存，改写其中 d MiB）和同一个文件负载 | `benchmark/ckpt_same_workload.py` | 四组测点，每点 20 个样本（见下） |

**为什么原生 pause / resume 要在两套代码上各跑一遍。** 沙箱级为了 checkpoint / restore 改过原生路径（4 KiB 写跟踪的精确增量差分，以及脏页跟踪本身）；进程级的 orchestrator 不含这些改动，但也不是纯上游：它来自 KASandbox 一脉，pause 要等快照上传完才返回（上游不等），模板注入了 gsd、guest 内核打了补丁。两套各跑一遍，差值就是"我们的改动对原生路径的影响"，对外引用原生数字时要写明是哪一套上测的。与 checkpoint / restore 语义对应的是 snapshot 模式（可以对同一个点反复恢复）；pause 模式只能回到最近一次 pause。

**`compare` 的四组测点。** 两套原来的分档负载不同（沙箱级改写 `/dev/shm`，进程级改写常驻进程，且进程大小随档位变），并列时分不开"改动量"和"进程大小"。这里两套用同一个负载：

| 组 | 进程大小 P | 改动 d | 文件 f | 回答什么 |
|---|---|---|---|---|
| dirty | 256 MiB | 0、16、32、64、128、256 MiB | 0 | 改动量变、进程大小不变时两套各自怎么变 |
| size | 32～384 MiB | 16 MiB | 0 | 改动量不变、进程变大时两套各自怎么变 |
| file | 16 MiB | 0 | 16、64 MiB | 只写文件 |
| conc | 64 MiB | 16 MiB | 0 | 1、4、8、16 个沙箱同时做 |

一次测量是：改写 → checkpoint A → 再改写 → checkpoint B → restore 回基点 → 校验内存 SHA256 与文件 → 删掉 A、B。A 紧跟在上一次 restore 之后，B 紧跟在 A 之后，汇总表分两列给出：进程级 restore 之后的第一次 checkpoint 曾经总是全量（CRIU 的 PID 复用检测，进程级手册第 16 章 L-18，gsd `daae6968f` 起已修复为增量），分两列可以核对修复是否生效；沙箱级两者都是增量。每个沙箱的第一次不计入统计（首次 restore 偏慢），基点全量单列。每次都记客户端墙钟、服务端 `total`（进程级取 gsd 分段，沙箱级读宿主上的 `timings.json`）和产物字节。

汇总里另有三样用来分析"快在哪一段"的内容：

- **脏页后端**：表头下一行，Firecracker 自报的 `dirty_tracking`。`hdbss` 是硬件标脏（950 应是它），`kvm-wp` 是 KVM 写保护（软件，920B 是它），`off` 是没开。沙箱级的增量靠它；进程级用 guest 内核的 soft-dirty，不用它（进程级报 `off` 是正常的）。`native` 的汇总表头同样打印这一项。
- **服务端分段表**：总耗时表之后，选 8 个代表测点（dirty 的 d=0/16/64/256、size 的 P=32/384、file 的 f=64、conc 的 N=16），列连续 checkpoint、restore 后 checkpoint、restore 三种操作各分段的 p50（每行按耗时从大到小）。分段之间有包含关系，不能相加。
- **CRIU 统计**（只有进程级）：每轮核对之后（不在计时区间内）读 guest 里镜像目录下的 `stats-dump` / `stats-restore`，把 `criu_dump` / `criu_restore` 再拆成冻结、扫页（memdump）、写页（memwrite）、恢复的耗时，以及扫描、跳过（父镜像已有）、写出、恢复的页数。

已有的结果可以不重跑、只重新打印汇总：例如重打最近一次 compare 的沙箱级结果：`python3 benchmark/ckpt_same_workload.py --print test950/results/$(ls test950/results | grep compare | tail -1)/sandbox-compare/result.json`（旧结果没有脏页后端和 CRIU 统计，这两项显示为 `?` 或不打印）。

结果同样用 `bash test950.sh report` 打印，逐步打印的是脚本自己的汇总表；`bash test950.sh report sandbox/native` 等打印单步的汇总和日志末尾。

**模板存储在 tmpfs 上时。** 每次原生 pause / `create_snapshot` 都会产出一个新 build，沙箱结束后不会被自动删除。模板存储放在 tmpfs（如新 920B 的 `/tmp/templates`）时，这些文件记在 orchestrator 所在 Nomad 任务的内存 cgroup 上、回收不掉，一轮几百个样本就会让 orchestrator 因 cgroup OOM 被杀。所以 `native_snapshot_tiers.py` 在 `STORAGE_PROVIDER=Local` 时，每个沙箱结束后删掉它自己产生的 build 目录（只删记下的 build id，`--keep-builds` 可关）。

