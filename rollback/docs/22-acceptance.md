# 22 · 验收流程、结果判读与结论边界

## 本章目标

- 说明如何在已有环境上选择验收项目、取得测试工具、判读结果并留存条件。
- 逐档给出 smoke、func、perf、hdbss、long 的判据和看哪个文件。
- 列清已测、跳过、待验证的全部内容，给出每项结论能说到哪里。

已知问题与候选开发方向的状态见 [第 30 章](30-status-and-directions.md)。本章 §7 记录缺口的现象和数字，§9 按已测、跳过、待验证汇总。

## 1. 获取工具与准备验证环境

正式交付是功能代码和本手册。完整上机工具 `rollback/scripts/` 属于 950 测试部署方式，不随交付分支自动附带。以下 `run.sh` 命令以已经取得该工具目录为前提，并从工具目录所在的仓库根目录执行。

客户采用自己的 E2B 部署方式。使用已安装配套 SDK 的解释器，设置实际客户端连接凭据与模板，确认[平台与能力前提](25-build-and-self-check.md)。宿主侧性能采集需要读取服务端计时和产物，远程仅调用接口不能替代这些观测。

```bash
PY=$(command -v python3)
"$PY" -c 'import sys, e2b; print(sys.executable, e2b.__file__)'
bash rollback/scripts/950/run.sh --help
```

`run.sh` 可用参数以[工具说明](../scripts/950/README.md)为准；内部默认的 `benchmark/.env`、Nomad 名称与产物路径应按实际环境提供或调整。复测所需的工具访问与凭据由双方另行安排，阅读本书和审阅既有结果不依赖取得这些工具。

## 2. 各档一览

```bash
bash rollback/scripts/950/run.sh smoke --python "$PY"
bash rollback/scripts/950/run.sh func  --python "$PY"
bash rollback/scripts/950/run.sh perf  --python "$PY"
bash rollback/scripts/950/run.sh hdbss --python "$PY"
bash rollback/scripts/950/run.sh long
```

| 档 | 目标耗时 | 回答什么 | 判过 |
|---|---|---|---|
| `smoke` | ≤ 3 分钟 | 这台机器的 checkpoint 能不能用、是不是增量 | 5 项全 PASS |
| `func` | ≤ 40 分钟 | 功能与健壮性 | FAIL = 0 |
| `perf` | ≤ 40 分钟（`--quick` ≤ 10 分钟，只验证脚本跑得通） | 分档耗时、达标判定、长尾、并发 | FAIL = 0；超达标线**不**判 FAIL |
| `hdbss` | 约 4 分钟（每个 order；扫描时每个 order 另加约 1 分钟重启；`--quick` 约 20 秒） | 这个沙箱是否真在用 HDBSS、`FC_HDBSS_ORDER` 是否生效、写密集负载下的代价（[§5.1](#51-hdbss-档hdbss-专项)） | FAIL = 0；性能数字不判；后端不是 `hdbss` 时整档 SKIP |
| `long` | 小时级，不自动跑 | 长跑与大样本 | 只打印命令清单 |

每档在 `rollback/scripts/950/results/<时间戳>-<档>/` 下写一份 `SUMMARY.md`（表头"项 / 结果 / 耗时 / 关键数字或原因 / 产物"，上方是主机、内核、env 文件、解释器、模板与 PASS/FAIL/SKIP 计数）、
整轮屏幕输出 `run.log`、每项一份 `.log` 和脚本自己的 `.json`。`run.sh` 的**退出码 = FAIL 项数**。
`results/` 作为运行目录不入库。950 六轮测试结论整理为[阶段结果摘要](../evidence/950/README.md)，如实记录失败、跳过与超线情况；原始日志仅留本地，不复制入库或分发。

## 3. smoke

以下表格描述内部950验证工具当前的检查流程。覆盖层安装路径和文件哈希属于该测试部署；客户自建环境使用实际配套SDK、模板选中的FC路径、对应构建哈希及等效能力检查，不要求匹配内部二进制。

| 项 | 做什么 | PASS 判据 | 看哪个文件 |
|---|---|---|---|
| 宿主预检 | `crtest/portability/preflight-customer.sh`：CPU、KVM、GIC、HDBSS 能力、内核特性、模块、大页、产物盘、FC 二进制身份、SDK；FC 只判定选定的在用版本，其余版本目录记 INFO | 退出码 0（`WARN` 不算 FAIL） | `preflight.log` |
| SDK 覆盖层自检 | `install.py --check` | 打出 `自检通过（干净子进程）` | `sdk-check.log` |
| 能力文件 / 能力行 | 先读能力文件 `checkpoint-capabilities.json`（目录取正在运行的进程环境里的 `DEFAULT_CACHE_DIR`，没有就 `$ORCHESTRATOR_BASE_PATH/build`，再没有就 `/orchestrator/build`；给了 `--store` 就取它的父目录），其中 `pid` 须是该进程；没有有效的文件（旧版本不写）就从**正在运行**的 orchestrator 所在 alloc 的日志里找最后一次 `checkpoint capabilities`，再退到 journald | 读到有效的能力文件，或抓到带 `track_dirty_pages` 的能力行；两样都没有时判 SKIP，按 [§8](#8-记录条件与判定结果) 手工留档 | `capabilities.log` |
| FC sha256 | `/fc-versions/*/firecracker` 与 `/opt/e2b-infra/bin/firecracker`；摘要优先显示预检选定的在用版本 | 脚本 PASS 表示采集到非空身份文本；另须核对选定FC与该次实际构建产物的预期hash；内部950历史哈希见[第17章](17-functional-verification.md#42-宿主与版本配套950)；未判定在用版本而退回第一行时，摘要会注明 | `fc-sha256.log` |
| 功能正确性 | `acceptance/checkpoint_verify.py`：三代现场，内存 / 根文件系统 / 删除 / 权限位，心跳进程 pid 证明内存真的回来了 | 打出 `✓ 59 项校验全部通过。` | `checkpoint_verify.log` |

**在用版本怎么判。** 预检按以下优先级取可判定的依据，并在 `在用的 FC 版本` 行注明：运行中 FC 的 `/proc/<pid>/exe`（可有多个版本）→ api 显式设置的 `DEFAULT_FIRECRACKER_VERSION` → 部署脚本 `init-client.sh` 的 `FIRECRACKER_VERSION` → 版本目录中仅有一个 FC。
其余版本仅列 INFO。若全部无法判定，脚本会说明这一点，并在至少有一个目录带回滚接口时 PASS；这种情况仍需人工核对模板和实际执行路径，不能把“存在兼容 FC”当作“已证明实际使用该 FC”。`vhost_net` 只记 INFO，当前 Firecracker 网卡直接使用 tap，不依赖该模块。

另外确认两件事：

- **59 还是 57。** 57 说明 SDK 没有 `mem_mode` 字段，"是不是增量"那两条断言被跳过——这轮仍是有效的正确性验收，但**不能证明用的是增量**。重装覆盖层（先 `install.py`，后 `patch_e2b.py`）。
- **脏页后端。** `checkpoint_verify.log` 开头 `脏页后端 : …` 是 Firecracker 自己报的：950 应为 HDBSS（硬件标脏），显式开跟踪的无 HDBSS 机器为 KVM 写保护。能力行只回答"开没开"，回答不了这一项。

**950 smoke 已有五项全部通过的实测归档。** 2026-11-17 `test950.sh standard` 开头的 smoke 为 PASS 5 / FAIL 0 / SKIP 0，宿主预检 19 / 0 / 0、正确性 59 / 59；结尾回切后的 smoke-after 同样 PASS 5。
这一轮根据部署脚本选定 `v1.13.1`，接口检查、SDK、能力信息与 FC hash 核对通过。920B 的一轮为 PASS 5 / FAIL 0 / SKIP 0，预检 18 / 1 / 0，WARN 是本机无 HDBSS。完整条件与证据见 [第 17 章 §4](17-functional-verification.md#4-部署预检与正确性验收)。

## 4. func

默认跑 crtest 里**不需要重启服务端、不改服务端 env** 的 16 个用例，短的在前：
`T12 T15 T22 T24 T23 T34 T32 T41 T39 T26 T14 T40 T25 T36 T11 T13`，
再加一项 SDK 异常语义 pytest（`crtest/sdktests/test_checkpoint_errors.py`：打桩服务端、驱动真实客户端，验 10 个 checkpoint 异常类与 `.reason`；不建沙箱）。
每个用例的一句话说明：`PYTHONPATH=rollback/scripts/crtest python3 -m crtest --list`；逐条判据见 [第 17 章 §2](17-functional-verification.md#2-crtest-用例)。

另有 4 行 SKIP，SUMMARY 里写明原因：

| 用例 | 为什么默认跳 | 要跑的开关 |
|---|---|---|
| T18（重启后账本不跨重启） | 要重启 orchestrator，全机沙箱都会没 | `--allow-restart --restart-cmd "…"`（重启命令按本机部署方式给，脚本不替你猜） |
| T21（四条故障注入） | 要服务端带 `CHECKPOINT_FAULT_INJECT` 重启 | 按 [第 26 章 §5](26-configuration-and-capacity.md#5-改一个开关操作步骤) 改 env 重启后加 `--fault-cases` |
| T38（FC 侧 faulted 路径） | 要带 `rollback-fault-inject` 特性的 FC 并设 `FC_ROLLBACK_FAULT_INJECT=post_commit`；交付的 FC 不带 | 同上 |
| T37（水位闸与个数上限） | 要服务端带 `CHECKPOINT_MIN_FREE_BYTES` / `CHECKPOINT_MAX_PER_SANDBOX` 重启 | `--quota-cases` |

**在线业务机器上不建议跑这四条**（要改线上配置、重启或换 FC）。它们在 920B 上有 `3539c45df` 的专项结果（[附录 B §4](B-920b-results.md#4-重启与注入类)），在 950 上没有结果，结论里要写明未验证。
解释器里没装 pytest 时那一项也判 SKIP，装上后重跑：`python3 -m pip install "pytest>=7.4,<8" "pytest-asyncio>=0.23,<0.24"`。

**默认 func 在两个平台都有实测**：950 的 2026-11-17 一轮为 PASS 17 · FAIL 0 · SKIP 5，16 个 crtest 用例全过，四类重启 / 注入 / 配额项按计划跳过，SDK 异常语义 pytest 因客户端未装 pytest 跳过；920B 的 2026-11-06 一轮为 PASS 17 · FAIL 0 · SKIP 4。
逐项与 warning 的解释见 [第 17 章 §3](17-functional-verification.md#3-功能回归结果)，950 的混合短稳态与恢复后立即命令见 [第 21 章 §2](21-concurrency-and-longrun.md#3-950-func-中的短稳态与恢复后请求)。
判读时既看 PASS，也核对 T36 / T40 的失败记录项；不能将默认短测的通过写成小时级长稳，或写成所有负载的 restore 均满足 100 ms。

**PASS 判据**：每个用例退出码 0 = 通过，1 = 断言不过，3 = 前置不满足（记 SKIP）。整档 FAIL = 0。
某项 FAIL 时看 `crtest-T??.log` 的最后几段（"断言失败 / 期望 / 实际 / 依据"）和同目录 `T??.json`（`meta.failure` 是原文，`ops` 是每一步的原始记录）。

## 5. perf

| 项 | 脚本 | 产物 |
|---|---|---|
| 分档基准 | `crtest/bench/bench_tiers.py --tier-set short --passes 2` | `bench-tiers.log/.json`、`raw-*.jsonl` |
| 达标判定 | `crtest/bench/compliance.py` + `analyze.py` | **`compliance.log`**（达标表）、`analyze.log`（分段、拟合、离群、缓存漂移） |
| 串行长尾 | `crtest/bench/serial_restore.py -n 100`（单沙箱、单调用方、同一 checkpoint 连回 100 次；`-n` 可改） | `serial-restore.log/.json` |
| 并发 | `acceptance/checkpoint_concurrent.py --stages A,B`（A：跨沙箱扇出 1–16；B：同沙箱 4 线程争用） | `concurrent-ab.log/.json` |

**达标线**是客户给的参考指标：checkpoint ≤ 200 ms、restore ≤ 100 ms，量的是**客户端墙钟**；每遍开头那次全量 checkpoint **单列、不判定**（口径见 [第 16 章](16-test-methodology.md)）。
**超线不判 FAIL**——超线是结论不是故障，看 `compliance.log` 末尾"按 p50 超线的档"。PASS 只表示没有失败的操作、没有现场不一致。

判读时还要看：`analyze.log` 里各档 `mem_mode` 应全为 `incremental`；数字所在平台（920B 为 `kvm-wp`、950 为 `hdbss`）写进留档。
950的结果与判定见[第 18 章](18-950-performance.md)，920B历史分档见[第 20 章 §2](20-920b-performance.md#2-分档基准与判定)。
950 的 2026-11-17 perf 中，分档、达标判定、串行三项 PASS 与性能超线同时成立；并发项 FAIL，原因是 A 段 N=16 的 1 次 restore 应答丢失（[第 21 章 §2.3](21-concurrency-and-longrun.md#23-n16-的一次-restore-应答丢失)），根因在客户端 SDK 的共享连接池，已修复，950 待复验。并发 B 还记录了跨 restore 的 guest 命令异常，需读其明细，不能只据退出码写“整轮无异常”。

### 5.1 hdbss 档：HDBSS 专项

```bash
bash rollback/scripts/950/run.sh hdbss --python "$PY"
bash rollback/scripts/950/run.sh hdbss --python "$PY" --orders 1,2,4,6,9 --allow-restart
```

这一档只量 HDBSS 自己的性能，不测软件写保护，也不做两者的对照；重点是每 vCPU 的 HDBSS buffer 大小（`FC_HDBSS_ORDER`，[第 5 章 §4.5](05-dirty-tracking-and-hdbss.md#45-buffer-大小)）在写密集负载下的影响。脚本是 `crtest/bench/hdbss_bench.py`。

| 项 | 做什么 | PASS 判据 | 看哪个文件 |
|---|---|---|---|
| HDBSS 证据 | L1：宿主 KVM 认不认能力 502；L2：Firecracker 为这个沙箱报的 `dirty_tracking`；L3：顺序写 256 MB 的冷写 / 热写耗时与每页 KVM 退出数；order 核实：orchestrator 与 Firecracker 两个进程环境里的 `FC_HDBSS_ORDER`、`dmesg` 的 `Enable HDBSS success, HDBSS buffer size:`、FC 日志的 `HDBSS enabled (buffer order N)` | 后端为 `hdbss`，两个进程的环境变量一致，抓到的 order 与期望相同，checkpoint / restore 成功且 md5 一致；后端不是 `hdbss` 时退出码 3，整档 SKIP | `hdbss-evidence.log` |
| 写密集基准 | 脏页量 16 / 64 / 256 / 512 / 1024 MB（guest 装不下的大档自动去掉）× 连续写 / 随机散开写 × 1 / 2 线程，每个组合 3 遍，另有一行不写任何页的基底 | checkpoint / restore 全部成功、每次 restore 后 md5 一致、order 核实生效 | `hdbss-order-current.log`（扫描时每个 order 一份，如 `hdbss-order1.log`） |
| 汇总表 | order × 脏页量 × 写法 × 线程 | 生成成功 | `SUMMARY.md`、`hdbss-summary.json` |

每个组合记录（p50）：**冷写**耗时（这批页自上次 checkpoint / restore 以来第一次被写，在 guest 里量）、**热写**耗时（原样再写一遍）、
两者之差摊到每页的纳秒数、冷写期间每页的 KVM 退出数（取自 debugfs 里这台虚机的 `exits` 计数器，没有时退回 `perf`）、
增量 checkpoint 的客户端墙钟与服务端冻结窗口、内存差分的实占字节（对照"页数 × 4 KiB"）、restore 墙钟、restore 之后 md5 是否回到原值。
**性能数字只记录，不判 PASS / FAIL。**

**order 扫描要重启。** `FC_HDBSS_ORDER` 由 Firecracker 在构建每台虚机时读，环境继承自 orchestrator，换一个值就要改 nomad job 的 env 并重启 `template-manager-system`：
这台机器上的全部沙箱与 checkpoint 随之作废（[第 26 章 §5](26-configuration-and-capacity.md#5-改一个开关操作步骤)）。所以 `--orders` 必须同时带 `--allow-restart`，不带则报错退出；机器上还有活着的 firecracker 时默认也拒绝扫描（退出码 2，不建结果目录、不碰 job），确认这些沙箱可以消失才加 `--kill-sandboxes-ok`。
`run.sh` 取运行中的 job 定义留底（结果目录下的 `job-before.json`），只在 task 的 Env 里加这一个变量再提交，不改 `/opt/e2b-infra` 下的文件；
每个 order 重启后等 `/health` 回 200、进程环境里的值对得上，基准再在 300 秒内重试建沙箱。扫完、中途失败或被中断，都把留底的定义提交回去，
并在 `SUMMARY.md` 的"order 扫描与 job 恢复"一节写明测完了哪些 order、启动命令与 Env 和扫描前是否完全相同、现在进程环境里的 `FC_HDBSS_ORDER`；恢复失败时给出手工恢复的命令。
`job-*.json` 含 job 的全部环境变量（包括密钥），不回传。

**没有 HDBSS 的机器**（后端为 `kvm-wp`）上默认整档 SKIP，不建沙箱、不重启。`--force-non-hdbss` 可以照跑以验证流程，
`SUMMARY.md` 顶部标"非 HDBSS，仅流程验证，数字无意义"，order 核实降级为只核对进程环境变量。

基准有 14 分钟的总时限，到点即停并在结果里标注未跑完。流程已在无 HDBSS 的 920B 上带 `--force-non-hdbss` 跑通（后端 `kvm-wp`）：完整一轮 239 秒，1024 MB 档在 2 vCPU / 2048 MB 模板里放得下；`--orders 1,2` 的短试跑 217 秒，每次改 env 重启到就绪 45–53 秒，扫描后 job 的启动命令与 Env 和扫描前完全相同。这些是流程耗时，不是 HDBSS 的性能。

**950 默认 order 1 的完整专项已有文本归档**，来源为 2026-11-17 standard 的 `sandbox/hdbss` 步骤（`20261117-165758-hdbss`）：启用证据、写密集实测及统计边界见
[第 18 章 §8](18-950-performance.md#8-hdbss-专项默认-order-1-的启用证据与写密集实测)。这一轮无 `--quick`、无 order 扫描、无重启；
其他 order 的性能和扫描恢复流程不能由它推定。L3 的冷/热比需与退出数及启用日志一起读，不能只因比值大于 1 就判定 HDBSS 失效。

### 5.2 原生 pause / resume 分档

这一项不在 `run.sh` 的五档里，脚本是单文件的 `benchmark/native_snapshot_tiers.py`，在部署宿主上运行：

```bash
"$PY" benchmark/native_snapshot_tiers.py --mode pause --tiers 0,16,32,64,128,256 --passes 3 --iters 5 --template base --out result-native-tiers.json
```

| 判据 | 说明 |
|---|---|
| 末行 `判定: PASS` 且失败 0 | 每个样本的内容校验和常驻进程校验都通过 |
| 客户参考线 | 不适用。200 / 100 ms 约束的是 checkpoint / restore，原生路径的耗时只记录 |

结果里要核对 `dirty_tracking`、orchestrator sha256 和 `STORAGE_PROVIDER`。`--mode both` 同时跑 `create_snapshot()` 路径，950 目前只跑过 pause。结果与边界见 [第 19 章](19-native-pause-resume.md)。

## 6. long

```bash
bash rollback/scripts/950/run.sh long
```

只**打印**长测清单，不自动跑：这些都是小时级的，要单独安排、单独授权。清单包括：

- 并发 D 段长跑（`checkpoint_concurrent.py --stages D`，清单里是 8 沙箱 × 1800 s）与 C 段（沙箱生命周期；可能把 orchestrator 打挂，机器上有别人的沙箱时不要开）；
- 混合稳态全规模 `crtest T36`（16 沙箱 × 1800 s）；
- 串行 restore 5000 次；
- 分档基准全表（18 档 × 3 遍）与达标判定。

打印出的清单第一段用 `PY=$(command -v python3)` 取解释器，所以要在包含配套SDK的那个环境里照抄（与 [§1](#1-获取工具与准备验证环境) 的 `$PY` 是同一个）；其余命令原样可用，每条都已 `tee` 到 `$OUT` 下。
长跑前确认 netns 数已稳定。`checkpoint_concurrent.py` 建沙箱时给的寿命是 3600 s，清单里的长跑都在这之内；把时长加到 1 小时以上时，沙箱会先被回收，结果作废。

## 7. 已知项与缺口

能力边界、已知问题与候选开发方向的统一入口见 [第 30 章](30-status-and-directions.md)。本节记录现象、数字和证据入口。

| # | 项 | 现状 |
|---|---|---|
| 1 | delete 同步做合并 | 服务端 delete p50 从约 2 ms 升到约 10 ms（上限 60）/ 约 26 ms（滚动保留 10），客户端墙钟 p50 从约 4 ms 升到约 12 / 27 ms，p99 基本不变。需要时可改后台异步合并（[附录 C §3.10](C-evolution-and-cases.md#310-删除同步合并delete-变慢)） |
| 2 | 空闲后不先 checkpoint 直接 restore | p50 80.9 ms，大头在 FC 的 `fc_quiesce`（47.1 ms）；空闲 ≥ 2 s 后的 checkpoint 也升到 67–78 ms。根因未查，只作记录 |
| 3 | 16 路并发的首个增量 checkpoint 有两种形态 | p50 约 450 ms 一簇与约 100–130 ms 一簇都出现过，修复前就存在，同一二进制两次运行可分属两簇，未找到稳定触发条件（[附录 C §3.11](C-evolution-and-cases.md#311-16-路并发的首个增量-checkpoint-有两种形态)） |
| 4 | conntrack 后台清扫贴近关键路径 | `conntrack_bg` p50 在滚动负载下 52–55 ms、上限 60 负载下 74.9 ms（宿主表约 1.9 万条），已接近冻结窗口。清扫代价跟着整张宿主表走，表里约 95% 是与沙箱无关的条目；long11 第二个小时本机 DNS 条目堆积把它推到 69 ms。按 ct mark 过滤的方案 A 已设计，2026-09-30 决定暂不实施（[附录 C §3.6](C-evolution-and-cases.md#36-conntrack-清理占了冻结窗口)） |
| 5 | 滚动负载下 restore 随回滚量增长 | 60 min 内 `materialize_read_mb` p50 44 → 57 MB，写回滚内存约 0.65 ms/MB，restore p50 71 → 84 ms。guest 后台写让回滚量真实变大，是工作量不是缺陷（[附录 C §3.9](C-evolution-and-cases.md#39-视图块数增长restore-随时间变慢)） |
| 6 | 脏页跟踪关闭时不合并 | 每个 checkpoint 都是全量根，没有"隐藏且只有一个子节点"的条目，层照样累积。950 用 HDBSS、920B 显式开跟踪，都不走这条路 |
| 7 | 每重启一次 orchestrator 泄漏约 290 个 netns | 上游问题，靠运维清理（[第 28 章 §9](28-troubleshooting.md#9-网络槽位netns泄漏)） |
| 8 | 撕裂之后 SDK 的 `is_running()` | 两次运行分别得到 `TimeoutException` 和 `False`，用例注释预期为真；只记录，不判定 |
| 9 | 客户端偶发 `ReadError: [Errno 9] Bad file descriptor` | 每轮 0–2 次，服务端没收到这些请求，在客户端 httpx 连接层；只记录 |
| 10 | 非 restore 的流截断修复要两份代理都带上 | orchestrator 的沙箱代理与 client-proxy 共用同一段代理代码，部署件里两者都要是修复后的版本（[第 24 章 §11.3](24-api-semantics.md#113-两类流式截断要分清)、[附录 C §3.1](C-evolution-and-cases.md#31-流式命令偶发截断)） |
| 11 | 字节配额默认关 | 是否在交付件里默认开、开多大待定；crtest 没有对应用例，端到端验证只在工作区脚本里做过 |
| 12 | SDK 覆盖层自检 | 判据是“原有异常子类都在、映射表全是子类”，在两次部署件上重跑 smoke 都是 5 / 5（[附录 B §5](B-920b-results.md#5-部署后回归)） |
| 13 | 开发态工具 | `correctness.py` 已在两次部署件上跑过，都是 ALL PASS（[附录 B §5](B-920b-results.md#5-部署后回归)）。`correctness.py`、`pause_verify.py`、`loop.py` 不在 950 上跑，覆盖关系见[第 16 章](16-test-methodology.md)的验证项总表；`compat_matrix.py` 已移到 `acceptance/`，由 `run.sh func` 运行；`timing.py` 未在代码基准上跑 |
| 14 | 950 已归档范围与身份条件 | func 见 [第 17 章 §3](17-functional-verification.md#3-功能回归结果)，smoke 见 [第 17 章 §4](17-functional-verification.md#4-部署预检与正确性验收)，原生分档见 [第 19 章](19-native-pause-resume.md)，2026-11-18 perf-full 的 18 档与串行 100 次见 [第 18 章 §2](18-950-performance.md#2-条件与来源)～[第 18 章 §7](18-950-performance.md#7-同-checkpoint-串行-restore-100-次)，2026-11-17 的 12 档见 [第 18 章 §10](18-950-performance.md#10-第一轮2026-11-17-standard配置了-ub)，HDBSS 专项见 [第 18 章 §8](18-950-performance.md#8-hdbss-专项默认-order-1-的启用证据与写密集实测)，并发 A/B 与 func 短稳态在 [第 21 章](21-concurrency-and-longrun.md)。2026-11-17 standard 一轮的 orchestrator 与 FC sha256、宿主内核和机器型号由 `test950.sh` 在同一轮记录；原始 JSON / JSONL 未回传，源码 commit 与部署包的对应仍待核实。950 重启 / 注入 / 配额专项已跳过，空闲后专项与小时级并发长跑仍缺结果；见 [第 21 章 §3](21-concurrency-and-longrun.md#10-到-950-上要重测什么) |
| 15 | HDBSS 与软件写保护的收益对照 | 已决定不做：950 上只量 HDBSS 自身的性能，不测写保护 |
| 16 | 950 上的 HDBSS L3 证据落盘 | 已回传 2026-11-17 16:58 与 2026-11-18 13:19 两轮默认 order 1 的能力、自报、dmesg / FC 日志、冷 / 热写与 debugfs 退出计数，2026-11-18 另有 order 9 的对比，见 [第 18 章 §8](18-950-performance.md#8-hdbss-专项默认-order-1-的启用证据与写密集实测)；JSON / JSONL 尚未回传，不从文本推定长期表现 |
| 17 | `FC_HDBSS_ORDER` 取值对比 | 已确认默认 order 1 生效；已跑的只有 `run.sh hdbss`，没有 `--orders`，其他 order 比较仍待补。入口为 `run.sh hdbss --orders 1,2,4,6,9 --allow-restart`（[§5.1](#51-hdbss-档hdbss-专项)） |
| 18 | 跨树回滚 | 有单元测试（丢失 epoch 后跨树 restore 三种 sidecar 形态），无端到端用例 |
| 19 | HDBSS 在 vCPU 创建之后武装 | 只靠调用点位置保证，没有断言 |
| 20 | 条件标签留档 | 每轮必须记全机型、脏页后端、产物盘、模板、二进制 sha、脚本参数；漏记的数据事后补不上 |
| 21 | 原生 pause / resume | 950 与 920B 分档已测（[第 19 章](19-native-pause-resume.md)）。950 的 `create_snapshot()` 路径、逐段拆分、不开跟踪的对照，以及 `pb2.py` 待补；T39、T41 已在同一二进制上通过（2026-11-17 func）。pause 约 110 ms 的 Firecracker 退出前段和 resume 末尾约 35 ms 未拆开；920B 上 resume 比对照慢 25.1～36.0 ms，原因未查 |
| 22 | 封层刷屏障与重复刷盘 | V1–V3 均未验证，现状、依据和测法见 [第 30 章 §2.2](30-status-and-directions.md#22-读代码发现的待验证项) |
| 23 | 部署脚本的 80 → 3002 转发劫持 hyperloop 请求 | `build.sh` 装的 `nat PREROUTING --dport 80 -j REDIRECT --to-port 3002` 不限入口网卡、排在各槽位的 hyperloop 规则（→ 5010）前面，guest 发往 hyperloop 的请求全被转给 client-proxy：hyperloop 这条链路实际不通，client-proxy 在 16 沙箱负载下每秒约 900 条 `invalid host`，long11 两小时写出约 9.1 GB 日志。不影响 checkpoint / restore 的结果。2026-09-30 决定暂不改，改法是给该规则加 `! -i veth+`（[附录 C §6](C-evolution-and-cases.md#6-80-端口的转发规则劫持-hyperloop-请求)） |

## 8. 记录条件与判定结果

记录主机与内核、模板规格、存储、后端、特性配置、源码或二进制身份、命令参数、样本与计时口径，再记录各项 PASS/FAIL/SKIP 和性能达标范围。

`smoke` 证明本轮基本配置与正确性；`func` 覆盖默认用例；`perf` 输出测量和判定；`hdbss` 提供硬件后端专项；`long` 只打印长测命令。流程成功、断言通过与性能达标是三个独立判断。

内部环境的留档命令示例见[附录 E](E-environment-and-tools.md)。已有 950 测试中未回传的身份或原始数据应继续标明，不能用相邻轮次补填。

## 9. 结论边界：已测、跳过、待验证

本节把全部验证项按状态归到一处。“已测”只代表表中写明的平台、轮次和规模。状态变化时，本节与 [第 30 章](30-status-and-directions.md) 同步修改。

### 9.1 已测

| 内容 | 950 | 920B | 证据 |
|---|---|---|---|
| 部署预检与直链正确性（smoke） | 2026-11-17：PASS 5，59 / 59；结尾 smoke-after 同样 PASS 5 | 2026-11-06 部署：PASS 5，59 项通过，预检 WARN 1（无 HDBSS） | [第 17 章 §4](17-functional-verification.md#4-部署预检与正确性验收) |
| 默认功能回归（func） | 2026-11-17：PASS 17 / FAIL 0 / SKIP 5，16 个用例 863 条硬断言；SDK pytest 未跑 | 2026-11-06：PASS 17 / FAIL 0 / SKIP 4，SDK 67 passed | [第 17 章 §3](17-functional-verification.md#3-功能回归结果) |
| 分档性能 | 2026-11-18：十八档 1040 次迭代（2026-11-17：十二档 270 次），判定见第 18 章 | 全表 3 遍 1040 次迭代 | [第 18 章](18-950-performance.md)、[第 20 章 §2](20-920b-performance.md#2-分档基准与判定) |
| 同点串行 restore | 100 次（11-17、11-18 各轮） | 5000 次 | [第 18 章 §7](18-950-performance.md#7-同-checkpoint-串行-restore-100-次)、[第 20 章 §3](20-920b-performance.md#3-单沙箱串行与空闲后-restore) |
| HDBSS | 默认 order 1：69 次增量迭代，MD5 不一致 0；2026-11-18 order 1 与 9 各 63 次，MD5 不一致 0 | 无 HDBSS，只作负样本与流程验证 | [第 18 章 §8](18-950-performance.md#8-hdbss-专项默认-order-1-的启用证据与写密集实测) |
| 原生 pause / resume 分档 | 2026-11-06：pause 模式 108 个样本，失败 0 | 2026-11-06：精确增量与对照构建各 216 个样本，失败 0，含 snapshot 模式 | [第 19 章](19-native-pause-resume.md) |
| 短时并发与短稳态 | A/B 段（N=16 有 1 次 restore 应答丢失）、T36 4 沙箱 × 120 s、T40 | A/B 段、T36、T40 | [第 21 章](21-concurrency-and-longrun.md) |
| 长跑、滚动保留与资源走平 | 无结果 | 历史长测，含 T36 全规模 | [附录 B §10](B-920b-results.md#10-最终版的结果) |
| 重启、配额、故障注入 | 无结果 | `3539c45df` 上 10 项全过 | [附录 B §4](B-920b-results.md#4-重启与注入类) |
| 单元测试与属性测试 | 不在 950 上跑 | 在代码仓库里跑 | [附录 B §2](B-920b-results.md#2-总表)、[第 29 章](29-development.md) |

### 9.2 跳过

| 项 | 范围 | 不能下的结论 |
|---|---|---|
| T18 账本不跨重启 | 950 默认 func、920B 2026-11-06 func 都跳过 | 950 上重启语义未验证 |
| T21 四条服务端故障注入 | 同上 | 950 上这四条失败路径未验证 |
| T37 水位闸与个数上限 | 同上 | 950 上配额拒绝路径未验证 |
| T38 FC 侧 faulted 路径 | 同上 | 950 上 FC 过提交点后的路径未验证 |
| T41 外部出网连接 | 950 未指定 `--external` | 只验证了 guest 内回环和本次生命周期场景 |
| 其他 `FC_HDBSS_ORDER` | 950 只跑默认 order 1 | 不能比较 order 优劣 |
| HDBSS 与软件写保护的同机对照 | 已决定不做 | 不能给出 HDBSS 的加速倍数或净收益 |
| 950 上的 `create_snapshot()` 路径 | 分档只跑 `--mode pause` | 950 上这条路径的耗时未知 |

### 9.3 待验证

下表与 [第 30 章 §1](30-status-and-directions.md#1-已知问题) 的待验证项和待定位项一一对应，详情见 [§7](#7-已知项与缺口)。

| 项 | 第 30 章类别 | 现状 |
|---|---|---|
| 950 长期运行与专项覆盖 | 待验证项 | 已有默认功能、短时并发与混合短稳态证据；小时级长跑、滚动保留、资源收敛以及重启 / 注入 / 配额专项待补 |
| 950 部署身份、原始数据与 HDBSS 覆盖 | 待验证项 | 2026-11-17 一轮身份已记录；JSON / JSONL 未回传；其他 order 待补 |
| 跨树回滚与 HDBSS 武装时序 | 待验证项 | 前者有单元测试，缺端到端用例；后者由调用点位置保证，缺断言 |
| 视图块数与装配成本增长 | 待验证项 | 长跑末尾块数尚未饱和，需要更长实测确认走势 |
| 原生 pause / resume 固定开销 | 已拆分（950，2026-11-20），余项待查 | pause 的固定开销是发 SIGTERM 前的 `ps`、内核销毁 KVM 虚机和断开 NBD，resume 的大头是起新 Firecracker（cgroup v2 建 cgroup、新建 mount 命名空间）和等 envd，见 [第 13 章 §6](13-native-snapshot.md#6-成本模型)；进程级部署包 `f66dbe2c1` 已把 `ps` 改为读 `/proc`（920B 停 FC 一段 192 → 85 ms，950 待重测），那套的原生 pause 不再等于上游；等 envd 一段未再拆，920B 上 resume 比对照慢的原因未查 |
| 封层刷屏障与重复刷盘 | 待验证项 | V1–V3 均未验证 |
| 空闲后操作变慢、首个增量的两种耗时形态 | 待修复问题：根因或触发条件待定位 | 保留现象记录，需要针对负载复现 |
| restore 应答丢失 | 已修复，950 待复验 | 950 并发 N=16 出现 1 次，服务端已完成、客户端等满 300 s。2026-11-17 在 920B 复现并查实是客户端 SDK 共享连接池在多线程下误关正在使用的连接，SDK 已修，920B 复验未再出现（[第 21 章 §2.3](21-concurrency-and-longrun.md#23-n16-的一次-restore-应答丢失)） |
| 950 空闲后 restore | 属于 950 专项覆盖 | 950 待测，由 `run.sh idle` 运行；`compat_matrix.py` 已在 2026-11-17 func 中跑过（OK 13 / REFUSED 3 / BROKEN 0）；不在 950 上跑的开发态工具见 §7 第 13 项 |
| 当前 950 二进制上的 `pb2.py` | 属于 950 专项覆盖 | T39、T41 已在 `a5d03902…` 上通过（2026-11-17 func）；`pb2.py` 待跑 |

待修复但不属于验证缺口的项，例如 delete 同步合并、conntrack 清扫长尾、netns 泄漏和偶发 GC 长暂停，状态见 [第 30 章 §1](30-status-and-directions.md#1-已知问题)。

## 本章要点

- 流程成功、断言通过与性能达标是三个独立判断。超线不判 FAIL，SKIP 不算通过。
- smoke、func 在 950 和 920B 上都有结果；perf、HDBSS 默认 order 1、原生 pause 分档在 950 上有结果。
- 950 上没有重启、注入、配额专项，也没有小时级长跑、滚动保留和资源收敛结果。
- 920B 的历史结果不自动适用于 950，相邻轮次的身份不能互相追认。
- HDBSS 只有默认 order 1 的证据，没有其他 order 比较，没有同机软件写保护对照。
- 每轮要记全机型、脏页后端、产物盘、模板、二进制 sha、脚本参数。漏记的条件事后补不上。
