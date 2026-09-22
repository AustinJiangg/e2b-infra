# `rollback/scripts/950/` —— 950 上的一站式测试入口

这台机器：鲲鹏 950（有 HDBSS）+ openEuler，e2b-infra 由 RPM 部署到 `/opt/e2b-infra`。
本目录只有一个入口脚本 `run.sh`，把散在 `acceptance/`、`crtest/`、`crtest/bench/`
里的脚本按「冒烟 / 功能 / 性能 / 长测」四档串起来，每次跑完写一份 `SUMMARY.md`。

脚本本身不区分机器 —— 同一份 `run.sh` 在 920B 上也跑得动（`--env-file` 与 `--python`
指过去就行），950 与 920B 的差别只有一处，见 [§7](#7-950-与-920b-的差别)。

---

## 1. 部署完先跑这三条

**测试套件不在 RPM 里**（`e2b-infra.spec` 只把部署件装到 `/opt/e2b-infra`），它跟着仓库走：
出 RPM 时 clone 下来的那个 e2b-infra 目录里就有 `rollback/scripts/`，直接进去用，不需要再拷、不需要装任何东西。

然后三条命令：

```bash
cd rollback/scripts/950            # 在 clone 下来的 e2b-infra 目录里
bash run.sh smoke                          # ≤ 3 分钟：这台机器的 checkpoint 能不能用
bash run.sh func                           # ≤ 40 分钟：功能与健壮性
bash run.sh perf                           # ≤ 40 分钟：性能分档、长尾、并发
```

三条都不需要参数，默认值就是对的：

- **`.env`**：优先取仓库里的 `benchmark/.env`（由 `benchmark/sync-env.sh` 生成，带
  `E2B_API_KEY` / `E2B_ACCESS_TOKEN`），找不到才退到 `/opt/e2b-infra/.env`。部署根的
  `.env` 是**服务端**配置，里面通常**没有客户端凭据** —— 拿它跑会在开头看到
  `!! E2B_API_KEY 没有值`，随后建沙箱失败。所以正常流程是先生成一份：
  `cd ../../../benchmark && bash sync-env.sh`（它从 `/root/.e2b/config.json` 和
  `/data/nomad/acl.token` 取值）。要用别处的凭据就 `--env-file` 指过去。
- **解释器**：默认 `python3`，但**要用装了 SDK 覆盖层的那一个**。`build.sh -i` 在哪个
  环境里跑，`e2b==2.20.0` / `e2b_code_interpreter==2.4.1` / `python-dotenv` 和
  `dep/e2b-sdk-checkpoint/install.py` 的覆盖层就装在哪里。**venv 或 conda 都行**：
  仓库内 venv 是 `e2b-infra/.venv/bin/python`（推荐，`build.sh -i` 在激活它的 shell
  里跑，SDK 就装进去了），conda 环境就是那个环境的 python，用 `--python` 指过去。
  系统 `python3` 里不一定有。
- 结果落在 `950/results/<时间戳>-<档>/`。

只要这三条的 `SUMMARY.md` 里 `FAIL 0`，这台机器的 checkpoint / restore 就是好的。
`run.sh` 的退出码 = FAIL 项数，所以 CI 里直接 `bash run.sh smoke && bash run.sh func` 即可。

### 跑之前要确认的五件事

以下命令都在本目录（`rollback/scripts/950/`）里跑。

| 事 | 怎么确认 | 不满足会怎样 |
|---|---|---|
| 客户端凭据拿得到 | `grep -c E2B_API_KEY ../../../benchmark/.env` 回 1（没有就 `cd ../../../benchmark && bash sync-env.sh` 生成） | `run.sh` 开头会打 `!! E2B_API_KEY 没有值`，随后建沙箱失败 |
| 要用的解释器装了带 checkpoint 覆盖层的 e2b | `../../../.venv/bin/python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check`（用 conda 就把路径换成那个环境的 python），同一个解释器再用 `--python` 传给 `run.sh` | smoke 第 2 项就会 FAIL |
| 模板 `base` 已经建好 | `nomad job status` 里 template-manager 在跑，且建过一次模板 | 每个用例开头建沙箱就失败 |
| 模板 `base` 的规格是 **2 vCPU / 2048 MB**（磁盘约 940 MB） | 手册 24 篇 §5.0 那条核对命令：`GET /templates` 回的 `cpuCount` / `memoryMB` / `diskSizeMB` 应为 `2` / `2048` / `940` | 结果仍然有效，但**性能数字不能和手册第五部分对比**（规格是条件标签的一部分，见手册 24 篇 §5.0 与 §4.2） |
| 在**宿主机上**跑，且是 root | `id -u` 回 0 | 读不到服务端分段计时与产物目录，T32 会判失败、性能表会缺列 |

`benchmark/.env` 由 `benchmark/sync-env.sh` 生成：它从 `/root/.e2b/config.json`
（`build.sh -s` 的 seed 步骤写的团队凭据）和 `/data/nomad/acl.token` 取值，
`E2B_API_URL` 要指向本机 api 的 REST 端口：

```bash
cd ../../../benchmark
bash sync-env.sh
grep -E '^E2B_(API_KEY|ACCESS_TOKEN|API_URL)=' .env      # 三行都要有值
cd ../rollback/scripts/950
bash run.sh smoke --python ../../../.venv/bin/python      # conda 就换成那个环境的 python
```

---

## 2. 四档各是什么

| 档 | 目标耗时 | 跑什么 | 判据 |
|---|---|---|---|
| `smoke` | ≤ 3 分钟 | 宿主预检、SDK 覆盖层自检、`checkpoint capabilities` 日志行、firecracker sha256、59 项功能正确性 | 5 项全 PASS |
| `func` | ≤ 40 分钟 | crtest 里 16 个不需要重启服务端的用例 + SDK 异常语义 pytest | 17 项里 PASS + SKIP = 17，FAIL = 0 |
| `perf` | ≤ 40 分钟 | 分档基准（短表）+ 达标判定、单沙箱串行 restore 长尾 n=100、并发 A/B 段 | 4 项无 FAIL，且达标表里各档 p50 在线内 |
| `long` | 不自动跑 | 只打印清单与命令（见 [§6](#6-长测清单)） | — |

### 2.1 `smoke`

| 项 | 脚本 | 看什么 | PASS 判据 |
|---|---|---|---|
| 宿主预检 | `crtest/portability/preflight-customer.sh` | CPU / KVM / GIC / 大页 / 产物盘 / 二进制身份，共约 19 项 | 退出码 = 0，即 `FAIL 0`（`WARN` 不算 FAIL） |
| SDK 覆盖层自检 | `/opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check` | 覆盖层 21 个文件在不在位、九个异常类、端口 49984、四个 RPC 不重放 | 打出「自检通过（干净子进程）」，退出码 0 |
| capabilities 日志行 | 从 `/data/nomad/alloc/*/alloc/logs/start.stdout.*` 里抓 | `track_dirty_pages` 是不是 `true`、为什么 | 抓到且带 `track_dirty_pages` |
| firecracker sha256 | `/fc-versions/*/firecracker` 与 `/opt/e2b-infra/bin/firecracker` | 跑的到底是哪个二进制 | 至少找到一个 |
| 功能正确性 | `acceptance/checkpoint_verify.py` | 三代现场、内存/根文件系统/删除/权限位共 59 项，心跳进程 pid 证明是内存回来了 | 打出「✓ 59 项校验全部通过。」 |

看哪个文件：`SUMMARY.md` 一眼看完；要细节看 `preflight.log`、`checkpoint_verify.log`。

**59 还是 57？** 打出 57 项说明这个 SDK 没有 `mem_mode` 字段，增量判据失效 ——
回去重跑 `install.py`（顺序：先 `install.py`，后 `patch_e2b.py`，不能反）。

**脏页后端是 hdbss 还是 kvm-wp？** 不在 capabilities 那一行里。那一行只说
「脏页跟踪开没开、为什么开」（orchestrator 只答这个）；`hdbss` / `kvm-wp` / `off`
这三个值是 **Firecracker 自己**报的（FC API `/` 的 `dirty_tracking` 字段），要起一个
沙箱才问得到，由 `checkpoint_verify.py` 开头那行「脏页后端 :」打印，在
`checkpoint_verify.log` 里。950 上应当是硬件标脏（hdbss），920B 上是软件写保护（kvm-wp）。

### 2.2 `func`

跑 crtest 里**所有不需要重启服务端、不需要改服务端 env** 的用例，按耗时从短到长排，
失败早暴露。括号里是 920B 上的历史中位耗时，950 应当同量级或更快：

`T12`(8 s) `T15`(8 s) `T22`(12 s) `T24`(12 s) `T23`(13 s) `T34`(14 s) `T32`(18 s)
`T41`(19 s) `T39`(21 s) `T26`(26 s) `T14`(50 s) `T40`(146 s) `T25`(153 s) `T36`(178 s)
`T11`(188 s) `T13`(197 s)

一句话说明见 `python3 -m crtest --list`，详细判据见 `crtest/README.md`。
再加一项 **SDK 异常语义 pytest**（`crtest/sdktests/test_checkpoint_errors.py`）：
起一个打桩 HTTP 服务端，驱动真实客户端，验九个 checkpoint 异常类与 `.reason` 落点。
纯本地，不建沙箱、不碰栈。没装 pytest 就判 SKIP —— 950 能连外网，装上再重跑即可：

```bash
pip install "pytest>=7.4,<8" "pytest-asyncio>=0.23,<0.24"
```

版本按 py-sdk 自己的 `pyproject.toml` 钉；async 用例要的 `asyncio_mode=auto` 已经写在
`crtest/sdktests/pytest.ini` 里，不用另外配。920B 上实测 60 项全过、约 25 秒。

PASS 判据：每个用例自己三段式断言，退出码 0 = 通过、1 = 断言不过、**3 = 前置不满足（判 SKIP）**。
看哪个文件：`SUMMARY.md`；某项 FAIL 就看 `crtest-T??.log` 的最后三段（「断言失败 / 期望 /
实际 / 依据」）和同目录的 `T??.json`（`meta.failure` 是原文，`ops` 是每一步原始记录）。

### 2.3 `perf`

| 项 | 脚本 | 输出 |
|---|---|---|
| 分档基准 | `crtest/bench/bench_tiers.py --tier-set short` | 小档细分（0/4/8/16/32 MB 各 n=30）+ 中档（64/128/256 MB 各 n=20）+ 512 MB 极限档 n=10 + 纯内存 16 / 纯文件 16 / 只读 192 |
| 达标判定 | `crtest/bench/compliance.py` | 每档 p50 / p99 / 最大 + 与手册 28 篇达标线的对照表 |
| 串行长尾 | `crtest/bench/serial_restore.py -n 100` | 单沙箱、单调用方、同一个 checkpoint 连回 100 次的 p50 / p99 / 最大 |
| 并发 A/B | `acceptance/checkpoint_concurrent.py --stages A,B` | A = 跨沙箱扇出 N=1..16；B = 同沙箱 4 线程争用 |

**达标线**（手册 28 篇，照抄客户那组粗略指标，未限定改动量）：
**checkpoint ≤ 200 ms、restore ≤ 100 ms**，量的都是**客户端墙钟**。
全量 checkpoint（每遍开头那一次）按同一口径**单列不判定**。

看哪个文件：`compliance.log` 是那张达标表，`analyze.log` 是完整分析（分段、拟合、
离群、缓存漂移），`serial-restore.log` 是长尾三个数，`concurrent-ab.log` 是并发表。
原始数据在 `raw-*.jsonl` / `bench-tiers.json` / `serial-restore.json` / `concurrent-ab.json`。

PASS 判据：脚本退出码 0（= 没有失败的 checkpoint/restore、没有现场不一致）。
**超达标线不判 FAIL** —— 超线是结论不是故障，看 `compliance.log` 末尾那句
「按 p50 超线的档」自己判断。

先试一下脚本跑不跑得通、不想等 40 分钟，加 `--quick`：
两个档各 n=5、长尾 n=10、并发只 A 段两个 N，10 分钟内跑完，`SUMMARY.md` 里会
标成「**--quick 短试跑**」，数字不能当结论。

---

## 3. 跳过的用例及原因

`func` 默认跳过四类，SUMMARY 里会逐条写明 SKIP 与原因。都保留了显式打开的能力，
但**在交付态的 950 上都不建议跑**（前三类要改线上配置或重启，第四类要换 FC 二进制）：

| 用例 | 为什么默认跳 | 要跑怎么办 |
|---|---|---|
| `T18` orchestrator 重启后账本不过夜 | 会重启 orchestrator，**这台机器上所有沙箱都会没** | `bash run.sh func --allow-restart --restart-cmd "nomad job restart orchestrator"`（重启命令按本机部署方式填，脚本不替你猜） |
| `T21` 四条故障注入 | 要服务端带 `CHECKPOINT_FAULT_INJECT=envd_timeout`（四条注入一次只能装一条）启动，= 改线上配置并重启 | 改完 env 重启后 `bash run.sh func --fault-cases` |
| `T37` 两道配额闸 | 要服务端带 `CHECKPOINT_MIN_FREE_BYTES` / `CHECKPOINT_MAX_PER_SANDBOX` 启动 | 同上，`bash run.sh func --quota-cases` |
| `T38` FC 侧 faulted 路径 | 要 Firecracker 带 cargo feature `rollback-fault-inject` 并设 `FC_ROLLBACK_FAULT_INJECT=post_commit`；**交付的 FC 不带这个特性** | 换一份带特性的 FC 后 `--fault-cases` |

这四条的覆盖已经在 920B 上做过（见手册 25 篇），950 上跳过不影响交付结论。

另外两类会自动判 SKIP（不是配置问题，是环境问题）：
SDK 异常语义 pytest 在没装 pytest 时 SKIP；smoke 的 SDK 自检在找不到
`install.py` 时 SKIP（`--sdk-install` 可以指到别处）。

---

## 4. 结果目录结构

```
rollback/scripts/950/results/20260922-102353-smoke/
├── SUMMARY.md              ← 要交回的就是这一份
├── run.log                 整轮的屏幕输出（tee 下来的）
├── preflight.log           每一项一份 stdout+stderr
├── sdk-check.log
├── capabilities.log
├── fc-sha256.log
└── checkpoint_verify.log

950/results/20260922-102634-func/
├── SUMMARY.md
├── run.log
├── crtest-T12.log …        每个用例一份日志
├── T12.json …              每个用例一份原始数据
└── sdk-errors.log

950/results/20260922-110000-perf/
├── SUMMARY.md
├── run.log
├── bench-tiers.log / .json / raw-*.jsonl
├── compliance.log          ← 达标表
├── analyze.log             完整分析
├── serial-restore.log / .json / serial-restore-*.jsonl
└── concurrent-ab.log / .json
```

`results/` 整个不入库（`.gitignore` 里只留 `results/.gitkeep`）：原始日志体积大，
还带着本机路径与沙箱 id。

`SUMMARY.md` 的表头固定是「项 / 结果 / 耗时 / 关键数字或原因 / 产物」，
表上方有一块环境信息（主机、内核、部署根、env 文件、解释器、模板、PASS/FAIL/SKIP 计数）。

---

## 5. 结果怎么交回

手动拷这些，不需要打包整个目录：

```bash
# 三档的汇总（体积很小，一定要带）
cp results/*/SUMMARY.md /tmp/交回/

# 有 FAIL 的项，把它那一份 .log 和 .json 一起带上
cp results/20260922-102634-func/crtest-T25.log results/20260922-102634-func/T25.json /tmp/交回/

# perf 档另外带这三份（数字都在里面）
cp results/*-perf/compliance.log results/*-perf/analyze.log results/*-perf/serial-restore.json /tmp/交回/
```

交回时一并说明：跑的是哪个日期的 RPM、`smoke` 里 firecracker 的 sha256、
`checkpoint_verify.log` 开头那行「脏页后端」。这三样决定了这组数字能不能和别的机器对比。

---

## 6. 长测清单

`bash run.sh long` 打的就是下面这一段。这些都是小时级、要单独授权的，`run.sh` 不自动跑。

<!-- run.sh 的 long 档会 sed 出下面这一段，两行标记不要改 -->

## 长测清单

```bash
cd rollback/scripts/950            # 在 clone 下来的 e2b-infra 目录里
PY=python3
ENVF=/opt/e2b-infra/.env
OUT=/var/log/e2b-verify/long-$(date +%Y%m%d-%H%M%S); mkdir -p "$OUT"

# ① 并发 C / D 段（C 段可能把 orchestrator 打挂，机器上有别人的沙箱时不要开）
$PY ../acceptance/checkpoint_concurrent.py --stages D --soak-sandboxes 8 --soak-seconds 1800 \
    --out "$OUT/concurrent-D.json" 2>&1 | tee "$OUT/concurrent-D.log"
$PY ../acceptance/checkpoint_concurrent.py --stages C --lifecycle \
    --out "$OUT/concurrent-C.json" 2>&1 | tee "$OUT/concurrent-C.log"

# ② 空闲之后的 restore 长尾：空闲 0 / 5 / 10 秒各 n=30
for T in 0 5 10; do
  $PY ../crtest/bench/serial_restore.py --env-file "$ENVF" -n 30 --outdir "$OUT" \
      --out "$OUT/idle-$T.json" 2>&1 | tee "$OUT/idle-$T.log"
  sleep "$T"
done

# ③ 数千次循环：混合稳态全规模（1800 秒）+ 串行 restore 数千次
$PY -m crtest T36 --env-file "$ENVF" --seconds 1800 --out "$OUT/T36-full.json" 2>&1 | tee "$OUT/T36-full.log"
$PY ../crtest/bench/serial_restore.py --env-file "$ENVF" -n 5000 --deadline-min 600 \
    --outdir "$OUT" --out "$OUT/serial-5000.json" 2>&1 | tee "$OUT/serial-5000.log"

# ④ 分档基准全表（十八档，约 10 分钟 × 想跑几遍）
$PY ../crtest/bench/bench_tiers.py --env-file "$ENVF" --tier-set full --passes 3 \
    --deadline-min 90 --outdir "$OUT" --out "$OUT/bench-full.json" 2>&1 | tee "$OUT/bench-full.log"
$PY ../crtest/bench/compliance.py "$OUT"/raw-*.jsonl 2>&1 | tee "$OUT/compliance-full.log"
```

跑长测前把 `PYTHONPATH` 指到 crtest 套件根（`export PYTHONPATH=$PWD/../crtest`），
`-m crtest` 才找得到包。

## 长测清单结束

---

## 7. 950 与 920B 的差别

两台机器跑的是**同一份代码**，差别只有脏页跟踪的硬件后端一处：

| | 950（交付目标） | 920B（开发机） |
|---|---|---|
| CPU | 鲲鹏 950 | 鲲鹏 920B |
| 脏页后端（FC 自报的 `dirty_tracking`） | `hdbss` —— CPU 自己标脏，开销接近零 | `kvm-wp` —— 内核写保护每一个干净页，第一次写陷出一次 |
| KVM cap 502 | 有 | 无 |
| `FC_TRACK_DIRTY_PAGES` | 不设也会自己打开（跟随硬件） | 必须显式设 `true`，否则默认关（陷出对不拍快照的沙箱是净亏） |
| 产物盘 | 根盘 ext4 | `/mnt/ext4dev`（loop 卷） |
| 部署方式 | RPM → `/opt/e2b-infra` | 源码树 + nomad job |

**对结果的影响**：功能与正确性结论两台机器等价（代码路径完全相同），
所以 `smoke` / `func` 在 920B 上验过即成立；**性能数字不等价** ——
920B 的 checkpoint 耗时里含 VM exit 开销，950 应当明显更快。
`perf` 的数字必须在 950 上取，920B 的只能当量级参考。

在 920B 上跑本入口的命令（给开发自己用）：

```bash
cd /home/j30059180/projects/e2b-repo/e2b-infra/rollback/scripts/950
bash run.sh smoke --python /home/j30059180/projects/e2b-repo/e2b-infra/.venv/bin/python
```

（`--env-file` 不用给：默认就取仓库里的 `benchmark/.env`。解释器换成 conda 环境的
python 也一样，只要它装过 SDK 覆盖层。）

---

## 8. 这些脚本从哪来

| 目录 | 来历 |
|---|---|
| `../acceptance/` | 交付态验收，单文件零共享依赖，一直在本仓库里 |
| `../crtest/` | 沙箱级补充测试套件（20 个用例），2026-09-22 从工作区 `e2b-repo/rollback-tests/` 迁入本仓库，迁入时去掉了全部 920B 硬编码 |
| `../crtest/bench/` | `bench_tiers.py` / `analyze.py` 来自 `tmp/bench-tiers-20260921/` 的一次性采数脚本，`serial_restore.py` 来自 `tmp/conntrack-20260920/ctloop.py`，都在迁入时参数化了；`compliance.py` 是 09-22 新写的达标判定 |
| `../crtest/sdktests/` | `test_checkpoint_errors.py` 原样取自 `KASandbox_0904/py-sdk/tests/`。**随本套件带一份而不是让 950 去 clone 仓库** —— 它只依赖已安装的 `e2b` 加 pytest/httpx，没有 conftest 依赖，一个文件就能跑；950 上没有 py-sdk 源码树，clone 一个几十 MB 的仓库只为跑一个文件不划算。代价是它会随 SDK 演进而过时，改 SDK 异常语义时记得同步这一份 |
| `../crtest/portability/` `../crtest/probe950/` | 客户机器预检与「920B 结论能不能搬到 950」的探针，零外部依赖 |

手册里对应的篇目：24 篇（测试总览）、25 篇（功能测试）、26 篇（性能方法）、
28 篇（实测结果与达标判定）、29 篇（验收操作手册）。
