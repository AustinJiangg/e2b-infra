# crtest：沙箱级 checkpoint / restore 补充测试

对应 `e2b-repo/checkpoint-restore-fix-plan-2026-09-17.md` §4（T11–T36）。
现有三个脚本（`checkpoint_verify.py` 单沙箱正确性、`checkpoint_bench_v2.py` 单沙箱耗时、
`checkpoint_concurrent.py` A/B/C/D 并发）覆盖不到的盲区放在这里。

2026-09-22 从工作区 `e2b-repo/rollback-tests/` 迁入本仓库（`rollback/scripts/crtest/`），
迁入时去掉了全部 920B 硬编码：env 文件、T18 的重启命令、probe950 的解释器与数据卷挂载点，
都改成了参数或环境变量（见下表）。原工作区目录未动。

公共函数是从 `acceptance/checkpoint_concurrent.py` **复制**过来的
（`crtest/common.py` 顶部注明了来源），不 import 那边的文件 —— 那边的脚本是一个个
单独拷到目标机上跑的，跨文件依赖会在版本对不齐时静默降级成「未知」而不是报错。

```
rollback/scripts/crtest/
├── README.md                 本文件
├── run-r1.sh                 第 1 轮验收组合（T25 T26 T14 T13，小规模）
├── crtest/                   用例包（20 个用例）
│   ├── __main__.py           子命令分发
│   ├── common.py             沙箱包装 / 宿主机指标 / 断言 / JSON / guest 小程序
│   └── cases/
│       ├── t11.py t12.py t13.py t14.py t15.py t18.py t21.py t22.py t23.py
│       ├── t24.py t25.py t26.py t32.py t34.py t36.py t37.py
│       ├── t38.py t39.py t40.py t41.py
│       └── pending.py        留位机制（当前没有留位的用例）
├── bench/                    性能基准（都 import 上面的 crtest 包）
│   ├── bench_tiers.py        按改动量分档：--tier-set full（18 档）| short（小档细分 + 512 MB 极限档）
│   ├── serial_restore.py     单沙箱串行 restore 长尾：一个调用方、同一个 checkpoint 连回 N 次
│   ├── compliance.py         每档 p50 / p99 / 最大 + 与手册 28 篇达标线的对照表
│   └── analyze.py            完整分析：分段、线性拟合、反推达标改动量、离群、缓存漂移
├── sdktests/
│   └── test_checkpoint_errors.py   打桩服务端 + 真实客户端，验九个 checkpoint 异常类与 .reason
├── portability/
│   └── preflight-customer.sh 客户机器预检，零外部依赖、只读，退出码 = FAIL 数
├── probe950/                 「920B 的结论能不能搬到 950」：probe-host.sh / probe-dynamic.py / compare.py
└── tests/                    171 项离线单测（不需要沙箱、不需要栈，WSL 也能跑）
    ├── test_common.py        纯函数单测（不需要 e2b）
    ├── test_t39.py           T39 判定的离线单测
    ├── test_t41.py           T41 判定的离线单测
    ├── stub.py               打桩沙箱 + 打桩 checkpoint 服务端（错误文案/manifest 抄服务端原文）
    └── test_stub_smoke.py    T11 / T21 / T18 / T34 / T36 整段跑一遍

python3 -m unittest discover -s tests      # 171 项，约 30 秒
```

## 环境变量（迁入时用来替掉硬编码的那几个）

| 变量 | 作用 | 不设时 |
|---|---|---|
| `CRTEST_ENV_FILE` | dotenv 文件路径（`--env-file` 的默认值） | `$E2B_DEPLOY_DIR/.env` |
| `E2B_DEPLOY_DIR` | 部署根 | `/opt/e2b-infra` |
| `CRTEST_RESTART_CMD` | T18 重启 orchestrator 的命令（`--restart-cmd` 的默认值） | 空 —— T18 判前置不满足（退 3），不猜 |
| `CRTEST_REPO` / `R` | 重启命令里 `$R` 展开成什么 | 空 |
| `CRTEST_PY` | `probe950/probe-host.sh` 优先用哪个解释器探 SDK | `python3` |
| `CRTEST_EXTRA_MOUNTS` | `probe950/probe-host.sh` 额外要看的挂载点（空格分隔） | 只看 `/`、store、`/fc-versions`、`/tmp`、`/home` |
| `CRTEST_OUTDIR` | `bench/bench_tiers.py` 的产物落点（`--outdir` 的默认值） | 当前目录 |

`rollback/scripts/950/run.sh` 会替你把前三个设好。

## 怎么跑

一站式入口在 `rollback/scripts/950/run.sh`（smoke / func / perf 三档，自动写 SUMMARY.md）；
下面是直接手跑单个用例的办法。必须**在宿主机（920B / 950）上**跑，否则读不到服务端分段计时
（`<store>/<sandbox>/last-restore-timings.json`、`<ckpt>/timings.json`），T32 会直接
判失败。跑之前先读 `e2b-repo/stack-components-checklist.md`（两套栈别混）。

```bash
cd rollback/scripts/crtest          # 本套件根目录（内含 crtest/ 包）
PY=python3                          # 装了 e2b（含 checkpoint 覆盖层）的那个解释器
export CRTEST_ENV_FILE=/opt/e2b-infra/.env   # RPM 部署；源码树部署指到 e2b-infra/benchmark/.env

$PY -m crtest --list                              # 列出全部用例
$PY -m crtest T25 --out t25.json                  # 跑一个
$PY -m crtest T14 --sandboxes 4 --rounds 2        # 调规模
bash run-r1.sh                                    # 第 1 轮验收组合
```

依赖：`e2b`（带 checkpoint 覆盖层）、`httpx`、`python-dotenv`（没装也能跑，会退回
自己解析 .env）。除此之外不用第三方库。

### 公共参数（每个子命令都有）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--env-file` | `$CRTEST_ENV_FILE`，没设就是 `$E2B_DEPLOY_DIR/.env`（默认 `/opt/e2b-infra/.env`） | dotenv **显式路径**（不靠 CWD 找） |
| `--template` | `base` | 模板 id |
| `--out` | `<用例>-<时间>.json` | 原始数据 |
| `--keep-on-failure` | 关 | 失败时不 kill 沙箱，打印沙箱 id 与它在 orchestrator 里的 checkpoint 目录 |
| `--public` | 关 | 默认建**私有**沙箱（`allow_public_traffic=False`，顺带一直回归 K1），加这个才建公网的 |
| `--no-probe` | 关 | 跳过开跑前那次"脏页后端"探测（省一个沙箱的建/删） |

规模参数每个用例自己有（`--sandboxes / --rounds / --seconds / --depth …`），
默认值都按**一次 ≤ 5 分钟**定。

### 输出与退出码

JSON 结构与并发报告 §9.2 兼容，可以和那边的数据放一起算：

- `meta`：用例、模板、store 路径、文件系统、脏页后端、FC 版本、主机名、命令行参数、总耗时；
  失败时还有 `failure`（三段式原文）和 `kept`（留下来的现场）。
- `stages`：分段标记。
- `ops`：**每次操作一条**原始记录 —— `op`（create/restore/delete/list/sample/serial/net/fs/verify/scan/summary）、
  `box`、`sandbox`、`id`、`wall_s`、`ok`、`err`、`verified`、`mismatch`、`phases`（服务端分段，ms）、
  `mem_mode`，外加各用例自己的字段（`round` / `n` / `dirty_mb` / …）。
- `spawn`：建沙箱耗时。
- `summary`：每个用例的摘要（见下表最后一列）。
- `assertions`：每一条断言的 `name / ok / want / got / basis`。`ok` 为 `null` 的是
  "只记录不判定"的观测项。

退出码：0 = 全过；1 = 有断言没过或脚本异常；2 = 参数/留位用例；**3 = 前置不满足**
（T21 检测不到服务端装了指定的 `CHECKPOINT_FAULT_INJECT`，会打印怎么设 env）；130 = Ctrl-C。
**失败即停**：第一条不过的断言就结束，按三段式打印

```
断言失败：<名字>
    期望：…
    实际：…
    依据：<方案编号 / 报告章节>
```

正常结束会 kill 掉所有沙箱（`atexit` + `finally` 兜底）；`--keep-on-failure` 时
留下现场并打印 `沙箱 <id>` 与 `产物目录 <store>/<sandbox>/<checkpoint>`。

## 用例一览

| 用例 | 目的 | 判定（硬断言） | 主要参数（默认） | summary 字段 |
|---|---|---|---|---|
| **T25** 时间与 CPU 健康 | restore 之后 guest 的时间/定时器健不健康（S2 定时器风暴的捉手） | uptime **必须倒退**（既定语义，09-17 E1）；`date` 不倒退且与宿主机差 < 5 s；`sleep 1` = 1.0±0.1 s（restore 后和 10 s 后各一次）；10 s 窗口 CPU0 空闲 ≥ 90%；arch_timer < 200 次/s；现场回到 cp0 | `--rounds 10 --settle 10 --gap 3` | `restore_wall_p50_s`、`frozen_p50_ms` |
| **T26** 串口 | F1（S1 串口自锁）回归 | 每轮 restore **前后**各写一次 `/dev/ttyS0` 4 KB（`timeout 5` 包着）：退出码 0 且 < 3000 ms；跑完沙箱可用 | `--rounds 20 --limit-ms 3000 --gap 1` | `before_p50_ms`、`after_p50_ms`、`after_max_ms` |
| **T14** 写密集并发正确性 | 大脏集 + 并发下的 uffd / B4 / B5 / B7 路径 | **默认（stop）**：内存整块 md5、文件整体 md5、mem.md5 末行 seq、写者 seq 标记全部 = 快照那一刻；写者进程状态回到 `T`；页级坏页 ⊆ {快照时刻的进行中页}（至多 1 页，见口径 6）、行级坏行 0（末尾半条另计，至多 1 条）。**`--no-stop`**：只判页级/行级自校验（同一条容忍规则）+ 写者 pid 还在且 R/S/D + seq 回滚且落在快照区间 | `--sandboxes 4 --rounds 2 --seconds 8 --mem-mb 256`（`--skip-page-scan` 可省扫描，`--no-stop` 下忽略） | `mode`、`creates`、`restores`、`mismatch` |
| **T13** 冻结窗口干扰 | K4：别人 create 的 fsync 会不会变成我的冻结 | 只有"restore 全成功 + 现场一致 + 心跳有数据"是硬断言；曲线（客户端耗时 / `frozen` / 心跳段内间隔 随 N）**只记录** | `--fanout 1,2,4 --seconds 60 --every 5 --dirty-mb 128 --hb-ms 10` | 每个 N 一行：`wall_p50_s`、`frozen_p50_ms`、`hb_max_gap_s`、`hb_gap_p99_ms`、`hb_gaps_gt_100ms` |
| **T11** 生命周期竞争 | create/restore 进行到一半时 kill / `beta_pause`（方案 §4.1，C 段第一次真跑） | 服务端不崩（旁观沙箱每轮 C/R 正常 + 收尾冒烟现场一致）；在途操作**要么成功要么给明确错误**且不挂过 `--op-timeout`（客户端读超时算没说法）；kill 后 checkpoint 目录消失、无对应 `firecracker` 进程、netns 不增长；kill 后同 id `connect()` 用不了、新建沙箱正常 | `--sandboxes 3 --rounds 5 --delay-max-ms 50 --op-timeout 200 --reclaim-wait 60` | `kills`、`pauses`、`inflight_ok`、`inflight_wall_max_s`、`reasons`、`per_round` |
| **T12** 同沙箱多客户端交错 | 服务端按沙箱串行化对不对 | 风暴**之后**：无幽灵（list ⊆ 成功 create 过的）、成功删的不再出现、没删的不许失踪、restore 已删的回 not_found、残留的每个都能回、沙箱可用 | `--clients 4 --rounds 8 --seed …` | `created_ok`、`deleted_ok`、`final_list`、`failed_ops` |
| **T15** 树形分支并发 | 分支语义 + hidden 条目回收（A3/F5） | 每次 restore 现场逐项一致；`list` = 本地账本；沙箱可用 | `--sandboxes 4 --rounds 6` | `depth`、`verified` |
| **T24** 网络状态 | 活连接跨 restore 的语义 | guest 内 loopback 长连接 restore 后**仍可用**；新连接立刻可用；给了 `--external` 时跨出沙箱的旧连接必须**干净失效**（err/eof，不挂死）；conntrack 计数只记录 | `--rounds 3 --port 45123 --external ""` | `conntrack` 前后对 |
| **T23** 文件系统边界 | 写到一半拍、open fd、rename、fsync | (a) 流式文件是确定性流的前缀且块数退回快照那一刻；(b) open fd 上行号 1..N 连续、restore 后还能接着写；(c) 目录 rename 被回滚；(d) fsync 过又被删的文件回来且内容一致 | `--rounds 2 --run-seconds 5 --tol-blocks 100` | `rounds` |
| **T22** 深链随机回滚 | 深链 + 分支下 revertPath 对不对 | 随机顺序回每一个都现场一致；删中间层后后代仍能回且内容不变；`list` = 账本 | `--depth 40 --branch-every 8 --sample 12` | `create_p50_s`、`restore_p50_s`、`deleted_middle`、`heir` |
| **T21** 故障注入 | `CHECKPOINT_FAULT_INJECT` 的四条失败路径（A1/F2/A2/A3），一次一条 | `envd_timeout`：restore 回 `guest_unresponsive`、回滚其实已落地、**下一次 create 的 `parent_id` = 刚才 restore 的目标**（A1）；`torn_assemble`：restore 回 `data_loss/torn`，之后 create/restore 都被拒、list/delete 仍可用、重建的新沙箱照样能 create；`seal_move` / `commit_late`：create 回 500、推进过的 epoch 救成隐藏条目、隐藏条目不进 list、链没断（manifest 里仍是 `incremental`+挂在前一个条目下）。**两种注入模式各判各的**（见下面第 7 条）：常开验"失败之后账本没坏"，`:once` 验自愈 —— `commit_late` 第二次 create 成功、`seal_move` 第二次 create 回 `rootfs_poisoned` | `--fault envd_timeout\|torn_assemble\|seal_move\|commit_late` | `fault`、`inject_mode`、`reason_src`、`sdk_has_reason_field`、`entries` |
| **T32** restore 随脏集 | O(脏页) 与位图常数（C1/F3/O7） | `fc_bitmap` 三档 p50 极差 < 2 倍；`fc_memory` 随脏集单调不降且最大档 > 0 档；每次 restore 成功 | `--dirty 0,64,256 --repeat 5 --bitmap-ratio 2` | 每档的 `fc_memory/fc_bitmap/materialize/frozen/total` p50 |

| **T37** 两道配额闸 | `CHECKPOINT_MIN_FREE_BYTES` / `CHECKPOINT_MAX_PER_SANDBOX` | 507 `disk_full` / 429 `too_many_checkpoints`，异常类与 `.reason` 是 field 级，沙箱不受影响 | `--quota disk_full\|too_many` | `capabilities_line`、`quota` |
| **T18** orchestrator 重启 | F8：重启之后账本不过夜 | 重启前 create ×2 = 全量 + 增量；orchestrator 换了进程；**重启前 store 根下的沙箱目录一个不剩**；沙箱 A 没了；新沙箱 `list` 为空；它的第一次 create 是**全量新根**（manifest `mem_mode=full` + `parent_id` 空）且能回；netns 不增长。记录：重启耗时 / health 200 耗时 / API 能建沙箱的耗时 | `--allow-restart`（**不给就退 3**）`--restart-cmd "$R/tmp/switch-stack.sh jll" --pre-restart auto\|keep\|kill --health-url http://127.0.0.1:5008/health --restart-timeout 600 --health-timeout 300 --ready-timeout 300` | `restart_s`、`health_s`、`api_ready_s`、`pre_killed`、`b_first_manifest` |
| **T34** 运行期读链深（B8） | 链深会不会线性拖慢 guest 的冷块读 | **只记录不硬判**（曲线）；硬判的只有前置：每层建得出来、restore 成功、`drop_caches` 真生效、沙箱可用。软阈值 `--max-slowdown` 超了标 `warn`，仍不判失败 | `--depths 0,20,50 --file-mb 256 --layer-mb 4 --rand-reads 64 --rand-kb 4 --max-slowdown 3.0`（全规模 `--depths 0,50,200 --layer-mb 16 --rand-reads 256`） | `rows`（每档 `mbps/p50_ms/p99_ms/slowdown/warn`）、`warned` |
| **T36** 混合稳态 | 长时间乱序负载下漏不漏账、漏不漏回收 | 每条 worker 线程**跑满全程且循环体没抛过**；restore 后现场 0 不一致；收尾 `list` = 客户端账本；每沙箱删干净后 `list` 为空；沙箱还活着；活 FC 归零；netns 不增长。过程中的失败只统计不判（`--max-fail-ratio` 可加硬线） | `--sandboxes 4 --seconds 120 --weights "create=3,restore=3,delete=1,list=2,exec=3,write=2" --max-checkpoints 24 --report-every 30 --max-fail-ratio 0`（全规模 `--sandboxes 16 --seconds 1800`） | `by_scene`（次数/失败/p50/p99/失败分桶）、`threads`、`mismatch`、`fc_left` |
| **T38** FC 侧 faulted 路径（D2） | FC 过了 commit point 才炸的那一类（cargo feature `rollback-fault-inject` 读 `FC_ROLLBACK_FAULT_INJECT=post_commit`，rollback Phase 5 之后人为失败并置 `Faulted`），验服务端对它的既有处理 | SDK 异常类 = `CheckpointTornException`、`.reason == "torn"`、`reason_src == "field"`；三层文案一路带上来（`faulted` / `torn between two moments` / `must be recreated`）；之后再 create / 再 restore 都被 `refuseIfTorn` 打回同一个异常；服务端日志有 `markTorn` 的 Error 行与 restore defer 的 Warn 行，FC 日志有 `injected on purpose by FC_ROLLBACK_FAULT_INJECT`。后置状态按**仍在**判（`markTorn` 只做 `InvalidateBase`/`PoisonRootfs`/记 `s.torn`，不 kill FC、不释放 netns、不删 `store/<沙箱>/`，`is_running` 仍为真）。记录项：`fc_vcpu_*` 四个字段预期**不**出现、torn 之后 guest 命令的形态。**用例只检测不装注入**，前置不满足退 3 | `--fault fc_post_commit --cmd-timeout 30`（要 FC 带 `rollback-fault-inject` 特性 + task env `FC_ROLLBACK_FAULT_INJECT=post_commit`） | `sandbox`、`checkpoint`、`fc_before`/`fc_after`、`netns_before`/`netns_after`、`store_dir_exists`、`is_running`、`cmd_after_torn` |
| **T39** checkpoint 之后的原生 pause/resume | 09-18 的 P0：做过 checkpoint 的沙箱一原生 pause，导出的 memfile 差分只剩最后一段 epoch 的页（位图被 create/rollback 清过），resume 出来的 guest 内存是两个时刻拼的 | 三场景同一套秤：内存整块 md5 与 pause 前一致且页级自校验坏页 0；常驻写者 pid 不变、状态仍是 `T`（pid 变了说明是虚机重启）；内存里（`/dev/shm`）与盘上（`BENCH_DIR`）两份标记按场景该有的样子；`Box.alive()`。a：create 之后直接 pause/resume；b：create → 写 M1 → restore 回 create 那一刻 → pause/resume，resume 后必须看到 M0、**不该**看到 M1；c：create/restore 反复 3 轮再 pause/resume。服务端两条导出日志（页数、来源 `tracked`/`tracked+accumulated`/`resident`、累计集合大小）**只记录不判定** | `--scene all --mem-mb 64 --rounds 3 --connect-attempts 10 --connect-gap 5 --connect-timeout 180` | 每个场景一份：`sandbox`、`checkpoint`（c 是 `checkpoints`）、`before`、`export_log` |
| **T40** restore 紧跟请求的定向取证 | 报告 §2.18 / §7.1 的流截断（`incomplete chunked read` / `unexpected EOF` / EBADF）：贴着 restore 返回就发长流，或让在途流横跨下一次 restore（`beginRestore` → `dropConnections`） | 硬判只有三条：每个沙箱还活着、客户端账本 = 服务端 `list`、没有 worker 线程提前死。**截断次数是记录项**（口径同 T36，`--max-truncation` 可自己加硬线）。记录：每次 restore 的墙钟与服务端 `conntrack`/`total` 段；每条请求发出时刻相对 restore 返回的 `gap_ms`（0–1/1–2/2–5/5–10/10–20/20–50/50–100/>100 ms 八档分桶）；每条失败的沙箱 id、毫秒时刻、异常类与 message、前一次 restore 的段耗时 | `--sandboxes 4 --rounds 50 --stream-seconds 2 --fanout 1 --gap-jitter-ms 0 --req-timeout 120`（`--seconds` 与 `--rounds` 二选一）；`--overlap`（配 `--overlap-delay 0.5`）让在途流横跨下一次 restore；定向取证全规模 `--sandboxes 8 --rounds 300` | `requests`、`restores`、`restores_mid`、`by_class`、`by_scene`、`gap_ms_hist`、`gap_ms`/`conntrack_ms`/`restore_total_ms` 的 p50/p99/max、`truncations`、`threads`、`fc_left` |

| **T41** 干净沙箱的原生 pause/resume | 没做过 checkpoint 的普通沙箱原生 pause→resume 还正不正常（T39 只覆盖了做过 checkpoint 的那一支，而这条路**所有**沙箱都走） | a：pause/resume 之后自校验内存整块 md5 + 页级自校验、盘上文件 md5、写者与心跳 pid 不变且心跳继续涨（不涨/倒退分开报）、命令能跑、回环旧连接仍可用且新连接立刻可用（`--external` 给了才判出网）、`list` 为空；b：连续 3 轮，每轮改数据后逐轮同判，跑完两份标记攒齐 `M0,R0,…`；c：resume 之后 create → 改数据 → restore，现场回到 create 那一刻且 C1 不在。每次 pause/resume 的墙钟**只记录不判定**（`beta_pause()` 返回 `None`，拿不到快照大小） | `--scene all --mem-mb 64 --file-mb 4 --rounds 3 --port 45141 --external ""` | 每个场景一份：`sandbox`、`pause_wall_s`、`resume_wall_s`、`per_round` |

当前**没有留位未实现的用例**（T18/T34/T36 于 09-18 落地）。`crtest/cases/pending.py`
与 `__main__.PENDING` 留着机制：以后再有"先占位、后实现"的用例，加回去即可，跑它返回 2。

三条落地时的口径变化，记在这里免得下次又问：

- **T18 不自己重启。** 重启是要放行的事（920B 上还有别人的沙箱），所以要显式
  `--allow-restart` 才会执行 `--restart-cmd`；不给就打印那条命令并以 **3** 退出。
  另外 `tmp/switch-stack.sh` 在**有活 FC 时会拒绝换栈**，而需求书要的是"沙箱存活时
  重启" —— `--pre-restart auto`（默认）先带着沙箱 A 试一发，被拒了才 kill 掉 A 重来，
  并把 `pre_killed=true` 记进 JSON，此时"重启带走了沙箱 A"降为记录项。要验真正的
  "带着活沙箱重启"，用 `--pre-restart keep` 配一个不要求空载的 `--restart-cmd`。
- **T34 / T36 的默认规模都按"一次 ≤ 5 分钟"缩过**（T34 `0,20,50` 档、T36 120 s），
  需求书的全规模（T34 `0,50,200`、T36 30 分钟）用上表的参数开，属于长测试，按纪律
  先问过用户再跑。
- **T36 的 worker 线程绝不因异常退出。** 参考的 `checkpoint_concurrent.py` D 段一旦
  某个 scene 抛出去，那条线程就此退出而报表上看不出来；这里每个 scene 都在 `try` 里，
  循环体外还有一层，另报每条线程的 `iterations` / `alive_s` / `finished`，并把
  "线程跑满全程"判成硬断言。

## 几个必须知道的口径

1. **restore 之后 guest 里的后台进程也回到 checkpoint 那一刻。** T14 默认模式把这件事
   当工具用：create 前先 `SIGSTOP` 住写者，于是快照拍的是"写者停着"的一刻，restore
   之后内存不再变化，md5 可以精确比对；判据是 pid 不变 + 进程状态回到 `T` + 文件里的
   seq 回到那一刻。**`--no-stop` 是另一个问题**：快照拍在写者活动中（真实负载），
   "期望 md5"没人能预知，所以只做**自校验**（每页每行自带 页号/版本/crc32，撕裂和
   seq 断裂不用期望值也看得出来）+ 写者跨 restore 连续（pid 还在、R/S/D）+ seq 回滚
   且落在 `[create 前采样, restore 前采样)` 区间。一句话：默认模式测**精确回滚**，
   `--no-stop` 测**活动中拍快照的自洽性**。
2. **心跳间隔既不能跨 restore 读，段内也不能用 realtime 读。** restore 把心跳文件也
   回滚了，跨过去的那一"跳"等于 create 到 restore 的间隔，不是停顿；而 envd 把
   realtime 拨回当前发生在 monotonic 倒退**之后**，那一跳落在新段**段内** —— 用
   realtime 量段内间隔，"最大间隔"就变成了距 checkpoint 的时长（第 1 轮 T13 数据 13 段
   线性递增 5.2 s，那是口径错，不是停顿，09-17 已修）。`common.heartbeat_gaps` 按
   monotonic 倒退切段、**段内用 monotonic 相邻差**、并丢掉每段第一跳；除 max 外还给
   p99（`hb_gap_p99_ms`）与 > 100 ms 的条数（`hb_gaps_gt_100ms`），看分布别只看 max。
   （T13 还会在每次 restore 前把心跳文件抄走再清空。）
3. **并发下"拍那一刻的现场"没有定义**（并发报告 §4 已论证），所以 T12 不断言现场，
   只在风暴结束后对账；T14/T15 每个沙箱内部串行，现场才是良定义的。
4. **串口只在 T26 里碰**，其它用例一律走 `commands.run` —— restore 之后 guest 串口
   发送会卡死（并发报告 §6.7），拿它采集会把测试本身挂住。
6. **T14 的撕裂判据放过「拍摄时刻正在写的那一页」。** SIGSTOP / VM pause 可能落在写者写
   某一页的中途（那一次 memcpy 里），快照拍下的就是半新半旧的一页，restore 忠实还原 ——
   这不是"回滚混了两个时刻"（第 2 轮验收 `--sandboxes 4` 的页 42451 就是它：整块 256 MB
   的 md5 与 create 时记录的期望完全一致）。所以写者每写一页前把页号写进控制页
   `/dev/shm/t14-inprogress`（写完清 -1；控制页也在 tmpfs 里，随内存一起进快照），判定改成
   **坏页集合 ⊆ {进行中页号}**：至多 1 页且正是那一页 → 通过、在 note 里记一笔；**坏页 ≥ 2、
   或坏页 ≠ 进行中页号、或压根没有进行中页 → 仍判失败**，那才是回滚混了两个时刻。
   `--no-stop` 同一条规则（那边扫描前也先 SIGSTOP 住写者再读控制页；而且写者 restore 之后
   会从那条 memcpy 的中途接着跑完，所以进行中页多半自己就愈合了）。文件那份同理：一行
   20 来字节、`write()` 一次写完且短写会补齐，半条只可能落在**文件末尾**，校验器把它单独
   报成 `tail_partial` 并容忍 1 条，中间的坏行 / seq 断裂仍判失败。判定是纯函数
   `common.judge_page_scan` / `common.judge_line_scan`，有单测。

5. **不测宿主机侧的 FC stdout。** FC 的输出被 orchestrator 混进 nomad 轮转日志，
   按沙箱切出来只能 grep 全量日志，既不可靠又会翻出别人的沙箱。

7. **T21 的注入有两种模式，两套期望表。** `CHECKPOINT_FAULT_INJECT` 是 orchestrator
   启动时读一次的，写法 `<名字>` = 常开、`<名字>:once` = 只炸一次。模式是**用例自己测出来的**
   （`t21._inject_mode()` 读错误文案里的 `(CHECKPOINT_FAULT_INJECT=<名字>[:once])`，
   记进 summary 的 `inject_mode`），文案里没写模式就按常开办 —— 那是保守的一侧，真是 once
   的话第二次操作会成功、用例当场报失败，不会悄悄放过。两套期望表：

   | | 常开 `=<名字>` | 只炸一次 `=<名字>:once` |
   |---|---|---|
   | 验什么 | 失败之后账本没坏 | 自愈路径（注入用光之后那一步） |
   | `commit_late` 第二次 create | **也在同一处失败**（客户端只看得到 500） | **成功**，且 `mem_mode=incremental`、`parent_id` = 刚才那个隐藏条目，客户端拿到的 id = 盘上第二条 manifest，list 里只有它 |
   | `seal_move` 第二次 create | 仍被拒，reason 只能是 `internal`（注入点在 `AppendLayer` **之前**，`rootfs_poisoned` 轮不到，记成 note） | 仍被拒，但 **reason 必须是 `rootfs_poisoned`** —— 挡住它的不再是注入而是 poison 本身 |
   | 两种模式都判 | 隐藏条目（`state=committed`+`hidden=true`）、隐藏条目不进 list、不能当 restore 目标、链没断 | 同左 |

   还有一条**两种模式都测不了**：`seal_move` 的 poison 要一次成功的 restore 重新播种，而
   armed 过的沙箱一个可见 checkpoint 都没有（唯一那次 create 失败了），所以"之后 create
   恢复可用"只能记成 note，要测得靠运行期可切的开关。另外 `seal_move` 与 `commit_late`
   都让 create 回同一个 500，所以检测注入时**必须比对文案里的注入名**，模式也只认属于自己
   那一段（`=seal_move:once` 不能让 `commit_late` 按 once 判）。

8. **T21 的第二把秤是盘上的 manifest。** armed 时客户端只看得到 500，后置状态
   （`parent_id` / `hidden` / `state` / `mem_mode`）全在
   `<store>/<沙箱>/ckpt_*/manifest.json` 里 —— A1 的"账本跟着虚拟机走"、A3 的"链没断"
   都是这么判的。所以 T21 必须在宿主机上跑，不在就以退出码 3 退出。

9. **撕了的沙箱不许再碰 guest。** `markTorn` 故意把虚拟机停在 paused（那是事后分析的
   唯一材料），所以 T21 的 `torn_assemble` 从撕裂那一刻起一律 `record_scene=False`、
   不调 `scene()` / `alive()`，否则测试自己会挂在那条 guest 命令上。

10. **T11 的 200 s 是从服务端两道上限来的**：`CHECKPOINT_LOCK_WAIT_TIMEOUT`（60 s，
   超了回 503 + `Retry-After`，reason busy）+ `CHECKPOINT_FC_CALL_TIMEOUT`（120 s）
   （infra-arm jll e4c0e9f11）。被 kill 的沙箱不能复用，所以每轮现开一个"祭品"沙箱，
   旁观沙箱从头活到尾用来证明服务端没崩。`beta_pause` 那一支需要 SDK 有这个方法，
   没有就退回只做 kill 并记 skipped。

## 自检（不需要沙箱，WSL 上就能跑）

```bash
python3 -m py_compile crtest/*.py crtest/cases/*.py tests/*.py
python3 -m crtest --help && python3 -m crtest T25 --help
python3 -m unittest discover -s tests -v          # 164 个用例（含 T11/T21 的打桩冒烟）
```

`crtest/common.py` 与各用例模块**都不在模块层 import e2b / dotenv**，所以上面三条在
没装 SDK 的机器上也能过。`tests/test_stub_smoke.py` 还会把 T11 与 T21 的四条注入整段
跑一遍（打桩的沙箱与打桩的服务端，见 `tests/stub.py`），连退出码一起验 —— 抓的是
字段名拼错、越界、判定分支写反这类低级错误，真行为验证仍然要在 920B 上对真服务端跑。
打桩的服务端认 `commit_late:once` / `seal_move:once` 这种写法，所以 T21 两套期望表
（上面第 7 条）在打桩里各跑一遍；日志判定的轮询（`common.wait_for_log`，nomad logmon
批量刷盘滞后 0.05–1.3 s）有自己的单测：假时钟 + 第 3 次才刷出那一行。
