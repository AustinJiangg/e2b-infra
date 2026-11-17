# 23 · 缺口与结论边界

## 本章目标

读完本章，你应该能：

1. 说出最大的缺口是什么：950 上的进程级只跑了首轮验收和四组压测；
2. 对书里的每一类结论，说出它适用的平台、规格和负载范围，以及不能推到哪里；
3. 查到哪些路径、规格和时长没有证据，这些缺口各有什么影响；
4. 对 [19](19-920b-functional.md)、[20](20-920b-performance.md)、[21](21-920b-stress.md) 的任何一个数字，在出处表里找到它来自哪个结果文件。

评审和接手的人应当把本章与前面几章的结果放在一起读。限制本身（代码现在不做什么）在 [16](16-known-limits.md)，本章讲的是证据的范围。

---

## 1. 最大的缺口：950

**950 的进程级只跑了首轮。** 2026-11-17 在 950 上首次部署，`950.sh all` 13 步全部 PASS：部署、必跑验收（guest 自检、`verify_gsd.py`、`verify_fixes.py`、`verify_multitree.py --skip V12`、漏页测试、性能三轮）、探针与四组压测、切回沙箱级后的冒烟（[22](22-950-results.md)）。用的是与新 920B 相同的三个运行文件和同一个镜像。

- 这一轮说明部件在 950 上能部署、功能验收和 200～300 轮压测能通过，脏页跟踪不依赖 950 独有的硬件特性这一判断得到了一次实测支持。
- [22](22-950-results.md) 里分档性能、漏页、G 项读数等单元格还没有填，书里引用的耗时仍都来自新 920B。
- 八组压测、多种子、兼容矩阵、长跑、调试内核在 950 上都没有跑。
- 宿主硬件和宿主内核不同，920B 的耗时不能当作 950 的预期值，也不能作为通过阈值。

读者看到书里的性能和长时间稳定性结论，仍应当按"新 920B 上成立"理解。

---

## 2. 各结论的适用范围

| 结论 | 依据 | 适用范围 | 不能推到 |
|---|---|---|---|
| 功能项通过 | `verify_fixes.py`、`verify_multitree.py`，2026-10-08、10-09、11-06 三轮（[19](19-920b-functional.md)） | 新 920B；guest 2 vCPU / 2048 MB；gsd 提交 `259f1a25c`；同一个 guest 内核二进制；CRIU 4.2.1；THP 为 never | 950；别的规格；别的内核或 gsd 版本；缺口表里只有单测的分支 |
| 增量不漏页 | `verify_clear_refs_race.py` 两轮各 30 轮，加修改前版本的反向对照（[19](19-920b-functional.md) §2.3、§2.5） | 16 MiB 缓冲区、每秒 10 万页的写入负载 | 证明的是这种负载下没有查出漏页，不是任意负载的形式化保证 |
| 切换与回收在并发下正确 | 压测和探针（[21](21-920b-stress.md)）、回收专项（[20](20-920b-performance.md) §5.1） | 每组 200～300 轮、十种文件操作、发布版内核；调试内核上的结果来自前一个提交号 | 小时级连续运行；更大规格；压测没有覆盖到的操作组合 |
| 分档耗时 | 2026-10-16、11-06 两轮，每档三个样本（[20](20-920b-performance.md) §1） | 新 920B；单棵树的 Python 常驻进程；额外负载 0～192 MiB；新沙箱第一次 restore；服务端 `total_ms` | 950；其他程序的内存形状；同一沙箱里的连续 restore；固定进程大小下的改动量效应 |
| restore 基本不随改动量变化 | 早期六档（[20](20-920b-performance.md) §4）与机制分析（[17](17-cost-model.md)） | 单棵固定 192 MiB 的进程 | 进程本身变大的情形：restore 随进程大小增长 |
| 多棵树线性增长 | V12 第二部分（[20](20-920b-performance.md) §3） | 一到三棵树，每棵 64 MiB | 三棵以上；大小差别很大的树 |
| 代价模型的系数 | [17](17-cost-model.md)，各系数当场标了日期和口径 | 用来估算量级；系数的适用范围是各自的测量条件 | 判定值；约 0.8 ms/MiB 在最终内核上没有直接读数 |
| 修复前后的改善幅度 | 旧 920B 的同口径微基准（[附录 D](D-restore-slowness-case.md)） | 只说明修复方向和相对大小 | 新 920B 的任何判定值，两者不能相减 |

几条通用的读法：

- 不同平台、不同日期、不同口径的数字不相减，不算倍数。能回答"某一项改动贵了多少"的只有逐轮交错的对照。
- 2026-10-08 的确认验证是当前 gsd 和内核最完整的一轮。2026-11-06 复测了验收和四组压测，覆盖 orchestrator 重编和 SDK `dcca61266`，但没有 V12 和调试内核。两轮的 gsd 与内核 sha256 相同。
- 2026-10-16 只测了分档性能和部署自检，不替代完整验收。
- 与沙箱级的对比只在 [24](24-vs-sandbox-level.md)，本章的适用范围都只针对进程级自身。

---

## 3. 缺口表

下面是到本书定稿时**没有**证据的部分。

| 缺口 | 现状 | 影响 |
|---|---|---|
| 950 | 只跑了首轮验收和四组压测（§1），读数部分待填 | 性能与长时间稳定性结论的平台范围只到新 920B；表在 [22](22-950-results.md) |
| `HOST_GSD_PATH=off` 的关闭态 | 没有真实建过模板。依据是三样：`internal/cfg` 与 rootfs 的单测通过；把 `internal/cfg` 的测试编成二进制后在真实进程环境变量下跑，六种取值的解析结果符合预期；读代码确认开关管的五处都在建模板路径上。没有验证"关掉之后建出的模板与上游逐字节一致"。（开关的另一半，即不设变量、按默认路径注入，已于 2026-10-09 确认，见 [19](19-920b-functional.md) §1.3） | 关闭态下是否真的与不带 gsd 的 orchestrator 行为相同，没有实测证据（[16](16-known-limits.md) 的 T-08）。另外只有小写的 `off` 才是关（[16](16-known-limits.md) 的 L-09） |
| 只有单测、没有在真机上走过的分支 | 两棵树持有同一个 socket 的预检；三棵树时中间一棵 restore 失败；多棵树的 dump 超时与总时限；某棵树 dump 失败后前面的树被放开；容量预检按多棵树内存之和估算；`state.json` 版本 1 的加载；criu 被杀或崩溃之后的防御性放行 | 这些路径的编排逻辑有单测，但 CRIU 与内核在这些情况下的实际行为没有观察过（[16](16-known-limits.md) 的 T-01 ～ T-07） |
| 更大的 guest 规格 | 只测了 2 vCPU / 2048 MB（调试模板为 4096 MB，不做性能测量） | 耗时与容量边界随规格怎么变没有数据。快照存储占 guest 内存，规格不同时容量预检的拒绝点会不同 |
| 更多的树 | 功能与性能都只到三棵 | 三棵以上的线性外推没有验证；[17](17-cost-model.md) 的代价模型只在一到三棵之间有依据 |
| 更大的进程 | [20](20-920b-performance.md) §1 已覆盖额外负载 0～192 MiB 六档；容量测试最大 480 MiB | 超过 192 MiB 的性能趋势没有分档实测，容量验证不能代替性能结果 |
| 长时间运行 | 最长的单次压测是 300 轮，墙钟一分多钟；泄漏专项 300 轮 | 小时级连续运行下的行为（层与镜像的累积、journal 的增长、内核对象的缓慢变化）没有数据 |
| 调试内核上的功能验证 | 调试内核上只跑了压测与一个探针 | 功能路径上的越界与锁顺序问题，只在压测覆盖到的范围内被检查过 |
| 只在前一个提交号上跑过的压测组与探针 | readonly、wr3、rename、append、reltouch、enospc 六组，10 个种子的对照，除 `probe_whiteout_bug.py` 之外的探针，以及调试内核，都只在 `c0630349c` 上跑过（[21](21-920b-stress.md) §3） | Go 代码相同、内核是同一个二进制，推断结论不变；但严格说它们不是在 `259f1a25c` 的二进制上测的 |
| V12 在 2026-11-06 复测中跳过 | 离线包的验收不含 V12，这一轮没有逐轮交错对照，也没有一、二、三棵树的分段；重编后的 orchestrator 与 2026-10-08 的版本之间没有逐轮交错对照 | 这一轮的分档结果只能并列阅读，不能证明重编前后没有差别（[20](20-920b-performance.md) §1.3） |
| 分档两轮的差别 | 增量 checkpoint 在 128、256 MiB 两档，2026-10-16 与 2026-11-06 的样本互不重叠，来源没有查 | gsd 和内核相同，差别可能来自机器状态，也可能有别的原因，现有数据分不开 |
| 分档的样本量 | 每档三个样本，每个样本是新沙箱的第一次 restore | 分位数只能读中位数，同一沙箱里连续 restore 的分档结果没有 |
| 同机并发 | 性能脚本单沙箱串行；压测最多四路并行 | 同一台宿主上多沙箱同时 checkpoint、restore 时的耗时没有数据 |
| 可写打开文件的代价 | [20](20-920b-performance.md) §5.2 的数字是 2026-09-30 与 2026-10-08 较早的两版内核上测的 | 约 0.8 ms/MiB 这个系数在最终内核上没有直接读数（限制本身见 [16](16-known-limits.md) 的 L-04） |
| 内核没有从仓库补丁重新编译 | 验证的内核二进制是当天稍早编出的；与仓库补丁的对应靠树哈希相等确认（[19](19-920b-functional.md) §1.1） | "按仓库里的补丁与构建脚本重编，得到行为相同的内核"没有做过 |
| Python SDK 的单测 | 2026-10-08 的结果目录里没有 `py-sdk/tests/test_gsd_*.py` 的运行记录 | SDK 的行为由功能验证间接覆盖（所有脚本都经 SDK 调用），但单测本身在这一轮是否通过没有留档 |
| cgroup 的 `favordynmods` | 未测 | [附录 D](D-restore-slowness-case.md) 里作为 `rcu_expedited` 之外的替代办法提到，效果未知 |
| CRIU 不支持的特性 | 没有兼容清单 | 已知 stdout 接 journald 的进程、树间用 unix socket 相连的进程无法 dump；除此之外哪些常见程序会失败没有系统地试过 |
| 沙箱 pause / resume 之后 | 2026-11-16 起有一项实测：兼容矩阵 B8（`rollback/scripts/acceptance/compat_matrix.py`，模板 `base-gsd`）在新 920B 上建 checkpoint、pause、resume，再用原 Sandbox 对象 restore，内容一致、账本跨 pause 保留（沙箱级则不保留）。只有这一种序列、单次 | 设计上 gsd 连同内存一起被冻结再恢复（[15](15-state-concurrency-capacity.md)）；更多组合与次数没有覆盖；`connect` 取回的对象受上一行的 SDK 缺陷影响（[16](16-known-limits.md) 的 T-09） |
| 容量耗尽时的安全失败 | 2026-11-17 新 920B 上 `950.sh stress-ext` 的两组 1000 轮（脚本删得少，checkpoint 不封顶）。`multitree-capacity`：snapshot store 涨到 2 GiB tmpfs 的 89%～93%，guest `MemAvailable` 降到 16～24 MiB，容量预检拒绝了一部分 create，但约第 306 轮 guest 发生 OOM，被杀的是用户进程（gsd 与 criu 的 `OOMScoreAdjust=-1000`，[15](15-state-concurrency-capacity.md) §7 说的正是这种情形）。`deldense-capacity`：层堆在根盘上，约第 685 轮根盘写满，此后多数 create / restore 以 `internal`（"nothing was changed"）安全失败，但另有 4 次 restore 返回 `data_loss`，即杀进程之后才失败。错误原文未留存 | 容量预检对"估算值与 `MemAvailable` 接近"的情形没有余量，挡不住 OOM；根盘写满时 restore 不能保证所有可能失败的步骤都在提交点之前。200 轮（容量以内）三个种子都通过 |
| SDK `Sandbox.connect` 拿到的对象 | 2026-11-16 新 920B 上查实：`_cls_connect_sandbox` 建连接配置时没有加 `E2b-Sandbox-Id` 头（`create` 路径有），client-proxy 按头定址，于是这个对象上的 checkpoint 调用全部被以 `missing header` 打回。兼容矩阵的 B3、B4、B6/B7、E1 因此判 BROKEN；用 pause 之前那个对象做 restore（B8）通过，即 [16](16-known-limits.md) 的 T-09 在真机上第一次走通 | 用户 pause / resume 之后若用 `Sandbox.connect` 取回沙箱，checkpoint 功能不可用，需要修 SDK |
| 三处未查清的现象 | 新沙箱首个请求偶发的长等待（2026-10-08 记到一次，[19](19-920b-functional.md) §2.2；落位后的复核又记到一次，[19](19-920b-functional.md) §1.3）；候选内核上未复现的两次 checkpoint `internal`（同一沙箱的第 174、177 轮，[21](21-920b-stress.md) §3.5）；修复后仍有的单发 `ENOTEMPTY` | 都不影响判定，原因未知，见 [16](16-known-limits.md) 的 U-01、U-02、U-03 |

---

## 4. 数据出处

### 4.1 入库摘要

入库的摘要在 [`validation/`](../validation/README.md)，完整日志和原始 JSON 留在测试工作区。

| 轮次 | 文件 | 内容 |
|---|---|---|
| 2026-10-16 分档，[20](20-920b-performance.md) §1 | `920b-20261016/summary.json` | 三轮性能样本、各档中位数、RSS、恢复判定与原始文件哈希 |
| 同上 | `920b-20261016/provenance.json` | 部件、SDK 与脚本版本 |
| 旧 920B 修复前后的微基准，[20](20-920b-performance.md) §1.4 | `920b-20261016/historical-fix-evidence.json` | 旧 920B 原文摘录及来源哈希 |
| 2026-11-06 分档，[20](20-920b-performance.md) §1 | `920b-20261106/summary.json` | 三轮性能样本、各档中位数、RSS、恢复判定与原始文件哈希 |
| 2026-11-06 全流程 | `920b-20261106/provenance.json` | 部署、部件 sha256、验收与压测结论、未覆盖的范围 |

### 4.2 2026-11-06 全流程的结果目录

结果目录是 `920b-gsd-20261106`，路径相对于它。

| 数字 | 文件 |
|---|---|
| [19](19-920b-functional.md) §1.4 条件：部件 sha256、guest 环境、模板 ID | `results/20261107-054143-acceptance/deliverables.sha256`、`guest-check.log`；`logs/10-status.log`、`logs/11-template.log` |
| [19](19-920b-functional.md) §2.5 各步判定与读数 | `results/20261107-054143-acceptance/` 下的 `summary.json`、`verify_fixes.json`、`verify_multitree.json`、`clear_refs_race.log`、`clear_refs_race.jsonl` |
| [19](19-920b-functional.md) §2.5 回切冒烟 | `results/sandbox-smoke/20261107-055059-smoke/SUMMARY.md` |
| [19](19-920b-functional.md) §2.5 切回进程级栈后的 500 | `logs/44-verify-gsd.log`；重跑通过的记录只在离线包说明的"920B 实测结果"一节 |
| [20](20-920b-performance.md) §2 `verify_gsd.py` 分段 | `results/20261107-054143-acceptance/verify_gsd.log` |
| [20](20-920b-performance.md) §5.3、§5.4 | `results/20261107-054143-acceptance/verify_fixes.json` 的 G-08、G-20、G-21、PERF |
| [21](21-920b-stress.md) §2 探针与四组 | `results/20261107-054517-stress/` 下的 `whiteout.json`，各子目录的 `*-summary.json`、`RESULT`、`console.txt`，`multitree/multitree.log` 的"性能 p50"行 |

### 4.3 2026-10-08 及更早的结果目录

路径相对于各自的结果目录。结果目录不随手册入库，保存在测试工作区。文件名里的 `base-gsd9` 等是验证时的模板名，对照见 [19](19-920b-functional.md) §1.2。

**`gsd-release-20261008`（2026-10-08，`259f1a25c`）**

| 数字 | 文件 |
|---|---|
| [19](19-920b-functional.md) §1.1 条件标签：提交号、各 sha256、内核版本串、模板 ID、时间、go 版本 | `summary.txt` 第二、五节；`deliverables.sha256`；`build.log`；`envcheck-base-gsd9.txt`；`guest-gsd-check.txt`；`chain.log` |
| 补丁与内核源码树的对应（树哈希） | `applycheck.txt` |
| 两个提交之间只差一个补丁文件 | `summary.txt` 第一节第 6 条；`sync-check.txt` |
| 验证时 orchestrator 的环境变量、结束状态 | `final-status.txt`；`switch.log` |
| `HOST_GSD_PATH` 六种取值的解析结果与单测 | `hostgsdpath-env.log`；`hostgsdpath-unittests.log`；`summary.txt` 第四节 |
| [19](19-920b-functional.md) §2.1、[20](20-920b-performance.md) §5.3、§5.4 `verify_fixes.py` 的计数与各项实测 | `func/verify_fixes.log`；`func/verify_fixes.json` |
| [19](19-920b-functional.md) §2.2 `verify_multitree.py` 的判定、V5 的大小、慢调用 | `func/verify_multitree.log`；`func/verify_multitree.json`（`slow_calls`） |
| [19](19-920b-functional.md) §2.3 漏页测试 | `func/clear_refs_race.log`；`func/clear_refs_race.jsonl`；汇总值另见 `summary.json` 的 `this_round.clear_refs_race` |
| [19](19-920b-functional.md) §2.4 `probe_whiteout_bug.py` | `func/probe-whiteout.log`；`func/probe-whiteout.json` |
| [20](20-920b-performance.md) §2 `verify_gsd.py` 分段 | `func/verify_gsd-base-gsd9.log` |
| [20](20-920b-performance.md) §3 V12 两部分 | `func/verify_multitree.json`（`perf.summary`）；`summary.json` 的 `this_round.verify_multitree.V12_summary` 与 `V12_checks` |
| [20](20-920b-performance.md) §4 早期六档 | `func/perf-run1.json`、`perf-run2.json`、`perf-run3.json` 及同名 `.log`；三次的中位数在 `summary.json` 的 `this_round.demo_checkpoint_perf` |
| [21](21-920b-stress.md) §1.1 all、noworkers | `stress/rel-all/stress-summary.json`、`stress/rel-noworkers/stress-summary.json`；各自的 `RESULT`、`console.txt`、`stress-dmesg-*.txt`；分组参数在 `stress/jobs-rel.txt` |
| [21](21-920b-stress.md) §1.2 多棵树 | `stress/rel-multitree/multitree-summary.json`、`multitree-timings.json`、`RESULT` |
| [21](21-920b-stress.md) §1.3 删文件密集 | `stress/rel-deldense/deldense-summary.json`、`RESULT` |
| 四组的汇总判定 | `summary.json` 的 `this_round.stress`；`summary.txt` 第三节 |
| 前一轮的同名指标（[20](20-920b-performance.md) §2 的 84.8，[21](21-920b-stress.md) §1.1 的"多约四成"） | `summary.json` 的 `previous_round`；`summary.txt` 第三节 |
| [19](19-920b-functional.md) §1.3 落位后的复核：启动环境、模板 ID 与建模板耗时、guest 内核对 | `summary.txt` 第七节；`place-20261009/switch.log`、`build-base-gsd.log`、`guest-gsd-check.txt`、`envcheck-base-gsd.txt` |
| [19](19-920b-functional.md) §1.3、[20](20-920b-performance.md) §2 落位后的复核：三个脚本的结果、慢调用 | `place-20261009/func/verify_gsd-base-gsd.log`、`verify_multitree.log`、`verify_multitree.json`、`verify_fixes.log`；`place-20261009/chain.log` |

**`gsd-fix6-20261008`（2026-10-08，前一个提交号 `c0630349c`，同一个内核二进制）**

| 数字 | 文件 |
|---|---|
| [21](21-920b-stress.md) §3.1 发布版八组 | `summary.txt` 第七节第 2 条；`stress-v6-analysis.json`；`stress/jobs-v6.txt` |
| [21](21-920b-stress.md) §3.2 多种子对照 | `summary.txt` 第七节第 3 条；`ab-stress-analysis.json`；`ab-stress/jobs.txt` |
| [21](21-920b-stress.md) §3.3 调试内核各项 | `summary.txt` 第八节；`stress-dbg6-analysis.json`；`envcheck-base-gsd-dbg8.txt` |
| [21](21-920b-stress.md) §3.4 其余探针、[20](20-920b-performance.md) §5.1 最终内核上的泄漏复测与卸载路径 | `summary.txt` 第七节第 9 条 |
| [21](21-920b-stress.md) §3.5 未查清的 checkpoint `internal` | `summary.txt` 第九节第 1 条 |
| [附录 C](C-issue-resolution.md) 修复历程对照的 84.8 | `v6/verify_gsd-base-gsd8.log` |
| 内核与调试内核的 sha256 | `kernels.sha256` |

**更早的几轮**

| 数字 | 文件 |
|---|---|
| [19](19-920b-functional.md) §2.3 的反向对照（修改前 31 / 50 轮漏页，修改后 0 / 30） | `gsd-new920b-20260923/race-prefix-0923-0826-1.log`、`-2.log`、`-3.log` 与 `race-prefix-0923-0826.jsonl`；`race-fixed-0923-0826.log` |
| [20](20-920b-performance.md) §5.1 回收前后的对比、持有场景 | `gsd-fix5-20261008/summary.txt` 第四节第 2 条；`gsd-fix5-20261008/leak/` 下三个 `leak-*.json` |
| [20](20-920b-performance.md) §5.2 可写打开文件的代价 | `gsd-fix4-20260930/probe-move-cost-base-gsd5.json`；`gsd-fix4-20260930/summary.txt` 第四节；复测值在 `gsd-fix5-20261008/probe-move-cost-base-gsd6.json` |
| [附录 C](C-issue-resolution.md) 修复历程对照的 75.6、77.3 | `gsd-fixes-20260930/matrix/verify_gsd-base-gsd-kv1.log`；`gsd-fixes-20260930/verify_gsd-base-gsd-kv2.log` |
| 同上，93.8、94.5、104.1 | `gsd-fixes-20260930/verify_gsd-base-gsd2-kv2.log`；`verify_gsd-base-gsd2.log`；`matrix/verify_gsd-base-gsd2-kv1.log` |
| 同上，84.7 | `gsd-fixes-20260930/perf-fix/verify_gsd-base-gsd3.log` |
| 同上，87.0 | `gsd-fix3-20260930/verify_gsd-base-gsd4.log` |
| 同上，80.7 | `gsd-fix4-20260930/verify_gsd-base-gsd5.log` |
| 同上，86.1 | `gsd-fix5-20261008/verify_gsd-base-gsd6.log` |
| 同上，86.5 | `gsd-final-20261008/stageB/verify_gsd-base-gsd7.log` |
| 同上，各行"内核"一栏的归属 | 各目录 `summary.txt` 的标题与第一节；`gsd-fixes-20260930/SUMMARY.md` 的"更正与后续" |

---

## 本章要点

- 最大的缺口是 950：只跑了首轮验收和四组压测（全部 PASS），[22](22-950-results.md) 的读数部分待填，扩展档、长跑和调试内核没有跑。
- 全书的进程级结论都按"新 920B、2 vCPU / 2048 MB、THP never、`259f1a25c` 的 gsd 与同一个内核二进制"理解。
- 分档结果每档三个样本，进程大小与改动量同时变化；多棵树只到三棵；压测最长 300 轮；这些都是结论的边界。
- 只有单测的失败分支、注入开关的关闭态、pause / resume 之后的行为、更大的规格、小时级运行、同机并发都没有实测证据。
- 八组压测、多种子对照、调试内核和其余探针只在前一个提交号上跑过，引用时要带上这句限定。
- 不同平台、日期、口径的数字不相减，不算倍数。
- 每个数字的来源文件在 §4，别的章需要判定值时链接到结果章，不另写数字。
