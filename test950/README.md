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
bash test950.sh fcexit
```
```bash
bash test950.sh status
```
```bash
bash test950.sh report
```

默认两套都测。只测一套时加 `--stack sandbox` 或 `--stack criu`。`native`、`compare`、`fcexit` 是三项单独的性能测量，不在四档里，见第 6 节。

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

**性能测量前总要清一次 netns。** 每个 netns 在 `/run/netns` 下有一到两个挂载点；Firecracker 每次启动（新建沙箱、resume）都先新建一个 mount 命名空间，要复制整张挂载表，退出时再销毁它，两头的耗时都随挂载点个数增长（920B 实测：2915 个挂载点时一次 `unshare -m` 要 29 ms，减到 1486 个时 14 ms）。所以重启留下的 netns 越多，原生 resume、pause 就越慢，测出来的是"这台机器重启了多少次"。性能测量步骤（`compare`、`native`、`fcexit`，以及沙箱级的 `perf`、`perf-full`）切好栈之后、开测之前，不看上限，总要执行一次 `950.sh recycle-netns`，只留当前 orchestrator 自己的一池（与不频繁重启的生产节点一致），每次多约 2 分钟。`report` 的清理记录里有清理前后的 netns 与挂载点个数；性能脚本的宿主条件里另有开始、结束两次的挂载点个数。设 `TEST950_CLEAN_NETNS=0` 可关掉这一项（只剩超过上限才清）。切栈时如果已经在进程级栈上、但运行中的 gsd / orchestrator / 内核与仓库里的交付版本不同（例如 `git pull` 换了部署包），也会重新执行一次 `950.sh criu` 装上新版本。

**HDBSS**：在没有 HDBSS 的机器上（例如 920B），`hdbss` 两项整档判 SKIP。

## 4. 看结果

`report` 只把已有结果打印到终端，方便复制回传；它不跑测试、不改任何东西，什么时候执行、执行几次都一样。结果本身一直保存在 `test950/results/<时间>-<档>/` 下。

几档都跑完（或想看进度）时执行一次：

```bash
bash test950.sh report
```

不带参数时，**每一档各打印最近一轮**（一步都没开跑的轮次不算），开头列出是哪几轮，从 `==== test950 结果` 一行复制到 `==== 全部结束 ====` 一行即可。所以 extended、native、compare 连着跑完后只需要执行这一次。只看某一轮时加 `--run 目录名`。

每一轮打印的内容：逐步状态与用时、每段身份（orchestrator、Firecracker、进程级三个运行文件的 sha256，相同的只列一次）、宿主资源、自动清理 netns 的记录，以及每一步的逐项结论（沙箱级是 `run.sh` 的逐项表，带关键数字；进程级是每个子步骤，压测附轮数、create/restore 次数、p50、错误计数）。

某一步要看细节时，把步骤名跟在后面，它打印最近一轮里该步各项日志的末尾（失败项多打印一些）；进程级验收会打印性能三轮的分档表：

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

2026-11-16～17 在新 920B 上逐档演练、11-17 在 950 上跑 standard，发现下面五项。五项都已在 2026-11-17 修复；2026-11-18 在 920B 上用最终版本（2026-11-17 修复版 RPM `2026.09-3`、gsd `daae6968f`）完整跑了一遍 extended，五项都通过。第五项改的是沙箱级 orchestrator，随 2026-11-17 修复版 RPM `2026.09-3` 发布，950 要重新构建并部署 RPM 才生效。

| 步骤 | 项 | 原因 | 状态 |
|---|---|---|---|
| `criu/stress-ext` | `compat_matrix` 的 B3、B4、B6/B7、E1 | 进程级 SDK 的 `Sandbox.connect` 没带 `E2b-Sandbox-Id` 头，代理以 `missing header` 打回 checkpoint 请求 | 已修（SDK `ec1558c94`）；920B 复验 OK 15 / REFUSED 1 / BROKEN 0 |
| `criu/stress-ext` | `multitree-capacity` | 1000 轮把 snapshot store 涨到 guest 内存的九成，容量预检只要求放得下镜像，guest OOM 杀了用户进程 | 已修（gsd 预检要求 dump 后至少留 MemTotal 的 1/8）；920B 复验 PASS，无 OOM |
| `criu/stress-ext` | `deldense-capacity` | 1000 轮把沙箱根盘写满。restore 杀掉用户进程之后，层切换要在内核里新建 `work/work` 目录，ENOSPC，只能报 `data_loss`（3～4 次） | 已修（gsd 常驻预留空间、杀进程前按 `f_bavail` 检查、根盘写满报 `resource_exhausted`）；这一组改用 `--capacity` 判据：只允许安全失败。920B 复验见下 |
| `sandbox/perf` | 并发 `checkpoint_concurrent.py` | A 段 N=16 有 1 次 restore 客户端等满 300 s。根因在客户端：SDK 全部沙箱共用的 httpcore 连接池在连接总数超过 20 时，会关掉刚分给另一个线程的连接（沙箱级手册第 21 章 §2.3） | 已修（SDK keepalive 上限默认等于最大连接数）；920B 4800 次 restore 从 6 次失败降到 0 |
| `sandbox/special` | `crtest T18` | `nomad job restart` 时 orchestrator 关网络池超过 job 的 30 s kill_timeout，被 SIGKILL；Firecracker 在自己的进程组里，没被带走，由 init 收养。RPM 版 orchestrator 不含 deltabox 分支里启动时清理遗留 Firecracker 的代码（`fc/orphans.go`），所以一直留着 | 已修（2026-11-17 修复版 RPM `2026.09-3`：orchestrator 启动时结束父进程为 init 的遗留 Firecracker）；920B 复验 T18 通过，FC 在新 orchestrator 起来时即被清掉 |

`stress-ext` 里各组互不依赖，一组失败不影响其余组的结果。

2026-11-18 起 `stress-ext` 的 `append` 组按容量耗尽判：修好"restore 之后的第一次 checkpoint 是全量"（进程级手册第 16 章 L-18）之后，guest 内的镜像区不再先满，这一组 1000 轮会把沙箱根盘写满；6 个 worker 是 restore 管不到的 root 进程、一直追加，盘满后 checkpoint 和 restore 都只能安全拒绝（L-19）。判据允许 `resource_exhausted` 和 worker 的 ENOSPC，`data_loss`、`internal`、内核报错、校验失败仍判失败。

**920B 2026-11-18 extended 实测**（2026-11-17 修复版 RPM `2026.09-3`，gsd `daae6968f`）：沙箱级 smoke、func（PASS 18 · SKIP 4）、perf（含 N=16 并发）、special（T18、T21 四条、T37 两道全过；T38 要带故障注入的 FC，不跑）、perf --full、idle 全部 PASS，hdbss 两项整档 SKIP；进程级 acceptance（含新加的 `restore_incremental`）、stress PASS；stress-ext 27 组：26 组按原判据 PASS，`append` 按上面的判据离线重判 PASS；兼容矩阵 OK 15 / REFUSED 1 / BROKEN 0。各步用时合计约 85 分钟（另有切栈）。

沙箱级的两项修复都在 2026-11-17 修复版 RPM `2026.09-3` 里（orchestrator 与 SDK 覆盖层），按部署文档重新构建、部署 RPM 后生效；只想先拿到连接池修复而不重新部署时，可对客户端 conda 环境单独执行一次 `e2b-deploy/dep/e2b-sdk-checkpoint/install.py`（装完会自动重放 `patch_e2b.py`）；进程级的 gsd 与 SDK 由 `950.sh criu` 自动更新（`criu-rollback/deploy/README.md` 第 14 节）。

## 6. 三项单独的性能测量：`native`、`compare` 与 `fcexit`

四档测的是两套快照回滚本身。下面两项用来回答"和 e2b 原生 pause / resume 比快多少""两套为什么快慢不同"，与四档互不包含，各自单独执行，默认两套都测，跑完停在沙箱级（最后同样做一次沙箱级冒烟）。

| 命令 | 测什么 | 脚本 | 每套样本 |
|---|---|---|---|
| `bash test950.sh native` | e2b 原生 pause / resume，以及 `create_snapshot` → 从快照新建沙箱（snapshot 模式）。在沙箱级、进程级两套代码上各跑一遍 | `benchmark/native_snapshot_tiers.py --mode both --passes 3 --iters 8` | 六档（0～256 MiB，内存:文件 = 3:1），每档每种模式 24 个增量样本 + 3 个首次样本 |
| `bash test950.sh compare [--rounds N] [--series 组]` | 同一种负载下的 checkpoint / restore：两套用同一个常驻进程（P MiB 的内存，改写其中 d MiB）和同一个文件负载 | `benchmark/ckpt_same_workload.py` | 默认六组测点，每点每轮 20 个样本（见下） |
| `bash test950.sh dbm` | 进程级 soft-dirty 用不用硬件 DBM（2026-11-23 起的 guest 内核默认用）：只在进程级栈上，先跑一遍进程级验收（`950.sh acceptance`，这时 DBM 是开的），再在同一负载（`--series dirty,layout,mix`，不测原生）上把 guest 的 `soft_dirty_dbm` 设成 0、1 各跑一轮，最后 `criu/dbm-table` 并列成 A/B 表：常驻进程自己量的改写耗时（负载一侧，紧跟在一次 checkpoint 之后的那次改写）、连续 checkpoint、restore 的 p50 和差值。`--series` 可换测点组。结果打印：`bash test950.sh report criu/dbm-table`。约 25 分钟 |
| `bash test950.sh fcexit` | 原生 pause 里"停 Firecracker 到进程退出"那一段在内核里花在哪：只在进程级栈上（原生路径与上游相同），对这个 Firecracker 的线程开 ftrace | `benchmark/fc_exit_trace.py` | 进程 0、192 MiB 两个测点，每点 5 次 pause（另加第 0 次不计） |

**为什么原生 pause / resume 要在两套代码上各跑一遍。** 沙箱级为了 checkpoint / restore 改过原生路径（4 KiB 写跟踪的精确增量差分，以及脏页跟踪本身）；进程级的 orchestrator 不含这些改动，但也不是纯上游：它来自 KASandbox 一脉，pause 要等快照上传完才返回（上游不等），模板注入了 gsd、guest 内核打了补丁。两套各跑一遍，差值就是"我们的改动对原生路径的影响"，对外引用原生数字时要写明是哪一套上测的。与 checkpoint / restore 语义对应的是 snapshot 模式（可以对同一个点反复恢复）；pause 模式只能回到最近一次 pause。

**`compare` 的测点组。** 两套原来的分档负载不同（沙箱级改写 `/dev/shm`，进程级改写常驻进程，且进程大小随档位变），并列时分不开"改动量"和"进程大小"。这里两套用同一个负载：

| 组 | 进程大小 P | 改动 d | 文件 f | 回答什么 |
|---|---|---|---|---|
| mix | 等于内存改动 | 0、12、24、48、96、192、70 MiB | 0、4、8、16、32、64、0.001 MiB | 对外展示的分档表（内存 : 文件 = 3 : 1，另加 70 MiB + 0.001 MiB 一档）；每档的进程大小等于内存改动，即每一步改写整个进程；进程级栈上同一负载另起沙箱测 e2b 原生 pause / resume |
| big | 256 MiB | 0、12、48 MiB | 0、4、16 MiB | 大进程、小改动（总改动 0、16、64 MiB，内存 : 文件 = 3 : 1）：mix 里进程大小等于改动，这里改动只占进程的一小部分；与 mix 一样在进程级栈上另起沙箱测 e2b 原生 pause / resume，并列时另打一张对外表。不在默认测点里，要跑写 `--series big`（约 8 分钟一套） |
| dirty | 256 MiB | 0、16、32、64、128、256 MiB | 0 | 改动量变、进程大小不变时两套各自怎么变 |
| size | 32～384 MiB | 16 MiB | 0 | 改动量不变、进程变大时两套各自怎么变 |
| file | 16 MiB | 0 | 16、64 MiB | 只写文件 |
| conc | 64 MiB | 16 MiB | 0 | 1、4、8、16 个沙箱同时做 |
| layout | 256 MiB | 16、64、256 MiB，散页 | 0 | 改动分散在整个进程里（与 dirty 的连续改动对照） |
| trees | 合计 64 MiB | 合计 16 MiB | 0 | 平分到 1、4、16 棵进程树 |
| filewait | 16 MiB | 0 | 64 MiB | 落盘方式（fsync / 只 write）× 写完等 0、10、40 秒；每套约 22 分钟，不在默认组里，要 `--series` 显式写 |

默认跑前七组（不含 filewait）。`--series` 写逗号分隔的组名，`all` 是全部八组。另有 `scen` 组：用 `--scenarios 文件` 给出场景（每个场景写上面这些量的取值），格式见 `ckpt_same_workload.py --help`；场景的数值要按真实任务画像定。

**mix 组与对外分档表。** mix 每档用一个新沙箱，进程大小等于该档的内存改动量：这是故意构造的，让两套的 restore 都随档位增长——沙箱级 restore 随改动量增长，进程级 restore 随进程大小增长，本组里两者相等。实际进程往往大于每一步的改动，所以对外表下面附 size 组（改 16 MiB、进程 32～384 MiB）的 restore 小表。旧的“进程固定 256 MiB”那一组改名 mix256，不在默认测点里，要跑写 `--series mix256`。每个测点先照常测 checkpoint / restore；在进程级栈上再另起一个沙箱、同样的负载，循环“改写 → 读一遍内存 → `sb.pause()` → `Sandbox.connect()` → 第一条命令 → 读一遍内存 → 核对内存与文件”，测 e2b 原生 pause / resume。原生只在进程级栈上测：那套 orchestrator 的原生快照路径与上游相同。pause 与上游口径一致，只计第一步：上游拍完快照、放进本节点缓存就返回，这套代码还要等上传完成；orchestrator（`7e0a6a462` 起）每次 pause 打一行 `Pause timing` 日志，脚本从 orchestrator 的 Nomad 日志文件读出上传用时，pause = `sb.pause()` 墙钟减上传用时。resume 计 connect 返回加第一条命令完成；“resume 后首读内存”是 resume 之后第一次读完整个进程内存比 pause 之前多花的时间，即推迟到恢复之后的缺页成本。第一次 pause 导出的是自启动以来的全部写入，单列不进统计。orchestrator `6a60dec6f` 起，`Pause timing` 里另有各步完成的时刻，resume 另打一行 `Resume phases`；脚本据此每档再打一块“原生分段”（p50）：pause 拆成暂停 vCPU、写 snapfile、导出内存、停 Firecracker 到进程退出、断开 NBD、导出根盘差分、上传，resume 拆成起 Firecracker 到 API 可用、加载 snapfile、等 envd 等，客户端墙钟减 orchestrator 合计记为“API 与网络”。报告开头的宿主条件另有 netns 个数、iptables / nft 规则行数、运行中的 Firecracker 个数和 loadavg，两台机器的原生耗时对不上时先对照这一行。最后的 `compare-table` 除了按测点并列，还打印对外分档表（p50、p99 各一张，制表符分隔，可直接贴进表格）：总改动、内存、文件、进程大小、沙箱级 / 进程级 checkpoint、沙箱级 / 进程级 restore、沙箱级 / 进程级首次全量、E2B pause / resume、resume 后首读内存，表后附 restore 随进程大小的小表和表的说明。每档 checkpoint / restore 20 个样本、首次全量 2 个、pause / resume 20 个。

**多轮交替（`--rounds N`）。** 两套按 ABBA 顺序交替各跑 N 轮（第 1 轮沙箱级→进程级，第 2 轮进程级→沙箱级……），宿主状态随时间的变化平均分到两侧；最后一步 `compare-table` 把各轮合并，按测点并列两套的墙钟 p50 / p99。每测点样本数 = 20 × N，要 n ≥ 100 就用 `--rounds 5`。`report` 只打印并列表，各轮只列判定；单轮全文用 `bash test950.sh report criu/compare-r1` 这样的步骤名。

**基点前等 ext4 初始化。** 模板快照拍在 guest 的 ext4 把 inode 表后台清零完成之前，新沙箱启动后几秒内会额外往根盘写约 60 MiB。脚本在拍基点前等这个后台线程（`ext4lazyinit`）退出，等了几秒记在基点那一行；这样 restore 回基点不会重做清零，filewait 组测到的只是文件本身。2026-11-19 起 orchestrator 建模板时已把 inode 表清零，用新版建的模板没有这个线程，等待时间为 0；脚本仍保留这一步，兼容旧模板。每轮开头还记录宿主条件：NUMA 节点与大小、`kernel.numa_balancing`、宿主 THP、内核命令行。

一次测量是：改写 → checkpoint A → 再改写 → checkpoint B → restore 回基点 → 校验内存 SHA256 与文件 → 删掉 A、B。A 紧跟在上一次 restore 之后，B 紧跟在 A 之后，汇总表分两列给出：进程级 restore 之后的第一次 checkpoint 曾经总是全量（CRIU 的 PID 复用检测，进程级手册第 16 章 L-18，gsd `daae6968f` 起已修复为增量），分两列可以核对修复是否生效；沙箱级两者都是增量。每个沙箱的第一次不计入统计（首次 restore 偏慢），基点全量单列。每次都记客户端墙钟、服务端 `total`（进程级取 gsd 分段，沙箱级读宿主上的 `timings.json`）和产物字节。

汇总里另有三样用来分析"快在哪一段"的内容：

- **脏页后端**：表头下一行，Firecracker 自报的 `dirty_tracking`。`hdbss` 是硬件标脏（950 应是它），`kvm-wp` 是 KVM 写保护（软件，920B 是它），`off` 是没开。沙箱级的增量靠它；进程级用 guest 内核的 soft-dirty，不用它（进程级报 `off` 是正常的）。`native` 的汇总表头同样打印这一项。
- **服务端分段表**：总耗时表之后，选几个代表测点（dirty 的 d=0/16/64/256、size 的 P=32/384、file 的 f=64、layout 的 d=256、trees 的 16 棵、filewait 的三种、conc 的 N=16，没跑的组不列），列连续 checkpoint、restore 后 checkpoint、restore 三种操作各分段的 p50（每行按耗时从大到小）。分段之间有包含关系，不能相加。
- **CRIU 统计**（只有进程级）：每轮核对之后（不在计时区间内）读 guest 里镜像目录下的 `stats-dump` / `stats-restore`，把 `criu_dump` / `criu_restore` 再拆成冻结、扫页（memdump）、写页（memwrite）、恢复的耗时，以及扫描、跳过（父镜像已有）、写出、恢复的页数。
- **CRIU 日志**（只有进程级，compare 默认带 `--criu-logs`）：同样在计时区间外读 dump / restore 日志，把相邻两行日志的时间间隔记到前一行的消息上，列耗时最多的几项。dump 日志是 gsd 写死的 `-v4`；restore 默认级别几乎没有日志。

已有的结果可以不重跑、只重新打印汇总：例如重打最近一次 compare 的沙箱级结果：`python3 benchmark/ckpt_same_workload.py --print test950/results/$(ls test950/results | grep compare | tail -1)/sandbox-compare/result.json`（旧结果没有脏页后端和 CRIU 统计，这两项显示为 `?` 或不打印；`--print` 可以给同一套的多个结果文件，合并后打印）。两套并列：`python3 benchmark/ckpt_same_workload.py --compare 沙箱级结果.json 进程级结果.json …`，每套可以给多轮。

结果同样用 `bash test950.sh report` 打印，逐步打印的是脚本自己的汇总表；`bash test950.sh report sandbox/native` 等打印单步的汇总和日志末尾。

**`fcexit`：Firecracker 退出的内核拆分。** 原生 pause 要等 Firecracker 进程退出才能断开根盘、导出根盘差分，orchestrator 的分段里“停 FC 到进程退出”在 950 和 920B 上都是 170～200 ms，占 pause 的七成以上，用户态看不到里面。`fc_exit_trace.py` 在进程级栈上建沙箱、写满 P MiB 内存，每次 pause 之前把这个 Firecracker 的全部线程设进 `set_ftrace_pid`，用 `function_graph` 只跟踪退出路径上的一组函数（`do_exit`，释放地址空间的 `exit_mm` / `exit_mmap`，关文件与 KVM 的 `task_work_run` / `__fput` / `kvm_vm_release` / `kvm_destroy_vm`，销毁命名空间的 `exit_task_namespaces` / `put_mnt_ns`，等 RCU 的 `synchronize_rcu*` 等），另开 `signal_generate`（SIGTERM）与 `sched_process_exit` 两个事件。汇总按测点打 p50：orchestrator 记的这一段、SIGTERM 到主线程退出的内核时间线、退出最慢的线程的 `do_exit` 调用树。跟踪本身有开销，各段比不跟踪时略长，看的是比例和归属。ftrace 全机共用：开跑时 `current_tracer` 不是 `nop` 或已有函数过滤，就认为有人在用、不跑；结束时（包括出错）把 tracer、过滤、pid、事件、选项、时钟恢复成默认。打印：`bash test950.sh report criu/fcexit`。

**模板存储在 tmpfs 上时。** 每次原生 pause / `create_snapshot` 都会产出一个新 build，沙箱结束后不会被自动删除。模板存储放在 tmpfs（如新 920B 的 `/tmp/templates`）时，这些文件记在 orchestrator 所在 Nomad 任务的内存 cgroup 上、回收不掉，一轮几百个样本就会让 orchestrator 因 cgroup OOM 被杀。所以 `native_snapshot_tiers.py` 在 `STORAGE_PROVIDER=Local` 时，每个沙箱结束后删掉它自己产生的 build 目录（只删记下的 build id，`--keep-builds` 可关）。

