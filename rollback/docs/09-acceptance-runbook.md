# 09 · 上机验收

> 给做验收的人：拿到交付件、按 [05](05-deployment-prerequisites.md) 部署完之后，用一站式入口 `rollback/scripts/950/run.sh` 把 checkpoint / restore 验一遍——
> 每一档跑什么、要多久、怎么判过、结果怎么留档回传。判据背后的道理见 [23](23-testing-and-functional-verification.md)，性能口径见 [24](24-performance-methodology.md)。

---

## 1. 跑之前

**在哪跑。** 在跑 orchestrator 的**宿主机**上、以 root 跑。远程也能验正确性，但读不到服务端分段计时、产物目录和脏页后端，部分用例会判失败、性能表会缺列。

**测试套件在哪。** 不在 RPM 里，跟着 e2b-infra 仓库走：出 RPM 时 clone 下来的那个仓库里就有 `rollback/scripts/`，不需要再装任何东西。
**本篇所有命令都在这个仓库的根目录执行。**

**用哪个 Python。** 用 **`build.sh -i` 时装 SDK 的那个解释器**：`build.sh -i` 在哪个 Python 环境里跑，`e2b==2.20.0`、`python-dotenv` 和 checkpoint 覆盖层就装在哪里。
系统 Python 里不一定有 SDK；如果那个解释器装在虚拟环境或 conda 环境里，先在当前 shell 里激活它，让 `python3` 指向它。然后记下它的路径并确认：

```bash
PY=$(command -v python3)
"$PY" -c 'import sys, e2b; print(sys.executable, e2b.__file__)'
"$PY" /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check
```

第二条要打出 `payload 的 21 个文件都在位` 和 `自检通过（干净子进程）：…`。之后用 `run.sh` 的 `--python "$PY"` 把这个解释器传进去
（不传时 `run.sh` 依次用环境变量 `CRTEST_PY`、`python3`）。

**客户端凭据。** `run.sh` 默认读仓库里的 `benchmark/.env`（`/opt/e2b-infra/.env` 是服务端配置，里面没有客户端凭据）。生成并检查：

```bash
(cd benchmark && bash sync-env.sh)
grep -E '^E2B_(API_KEY|ACCESS_TOKEN|API_URL)=' benchmark/.env
```

三行都要有值。`sync-env.sh` 从 `/root/.e2b/config.json`（部署 seed 步骤写的团队凭据）取 key 与 token，从部署件的 `SERVER_IP` 填 `E2B_API_URL`。

**模板。** 所有脚本默认用别名 `base` 的模板，规格 2 vCPU / 2048 MB / 磁盘约 940 MB；规格不同结果仍有效，但性能数字不能与 [25](25-results-and-compliance.md) 对比。核对命令见 [23 §8](23-testing-and-functional-verification.md#8-测试环境与沙箱规格)。

**服务端状态。**
- 按 [05 §4.3](05-deployment-prerequisites.md#43-orchestrator-自报的能力) 留档能力文件（没有就抓能力行）：`track_dirty_pages=true`，`fault_inject=[]`，各限额与部署意图一致。
- orchestrator 刚重启过的话，等 `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5008/health` 回 `200`，并且 `ip netns list | grep -cE '^ns-[0-9]+$'` 连续几分钟不变再开始（槽位暖池在填充时性能数字带扰动）。
- 产物盘余量高于 `min_free_bytes`，否则第一次 checkpoint 就是 507。

---

## 2. 四档一览

```bash
bash rollback/scripts/950/run.sh smoke --python "$PY"
bash rollback/scripts/950/run.sh func  --python "$PY"
bash rollback/scripts/950/run.sh perf  --python "$PY"
bash rollback/scripts/950/run.sh long
```

| 档 | 目标耗时 | 回答什么 | 判过 |
|---|---|---|---|
| `smoke` | ≤ 3 分钟 | 这台机器的 checkpoint 能不能用、是不是增量 | 5 项全 PASS |
| `func` | ≤ 40 分钟 | 功能与健壮性 | FAIL = 0 |
| `perf` | ≤ 40 分钟（`--quick` ≤ 10 分钟，只验证脚本跑得通） | 分档耗时、达标判定、长尾、并发 | FAIL = 0；超达标线**不**判 FAIL |
| `long` | 小时级，不自动跑 | 长跑与大样本 | 只打印命令清单 |

每档在 `rollback/scripts/950/results/<时间戳>-<档>/` 下写一份 `SUMMARY.md`（表头"项 / 结果 / 耗时 / 关键数字或原因 / 产物"，上方是主机、内核、env 文件、解释器、模板与 PASS/FAIL/SKIP 计数）、
整轮屏幕输出 `run.log`、每项一份 `.log` 和脚本自己的 `.json`。`run.sh` 的**退出码 = FAIL 项数**。
`results/` 不入库（体积大，含本机路径与沙箱 id）。

---

## 3. smoke

| 项 | 做什么 | PASS 判据 | 看哪个文件 |
|---|---|---|---|
| 宿主预检 | `crtest/portability/preflight-customer.sh`：CPU、KVM、GIC、HDBSS 能力、内核特性、模块、大页、产物盘、FC 二进制身份、SDK | 退出码 0（`WARN` 不算 FAIL） | `preflight.log` |
| SDK 覆盖层自检 | `install.py --check` | 打出 `自检通过（干净子进程）` | `sdk-check.log` |
| 能力文件 / 能力行 | 先读能力文件 `checkpoint-capabilities.json`（目录取正在运行的进程环境里的 `DEFAULT_CACHE_DIR`，没有就 `$ORCHESTRATOR_BASE_PATH/build`，再没有就 `/orchestrator/build`；给了 `--store` 就取它的父目录），其中 `pid` 须是该进程；没有有效的文件（此前的版本不写）就从**正在运行**的 orchestrator 所在 alloc 的日志里找最后一次 `checkpoint capabilities`，再退到 journald | 读到有效的能力文件，或抓到带 `track_dirty_pages` 的能力行；两样都没有时判 SKIP，按 §1 手工留档 | `capabilities.log` |
| FC sha256 | `/fc-versions/*/firecracker` 与 `/opt/e2b-infra/bin/firecracker` | 至少找到一个；值应是 `18f3faa7…`（[05 §3](05-deployment-prerequisites.md#3-版本配对)） | `fc-sha256.log` |
| 功能正确性 | `acceptance/checkpoint_verify.py`：三代现场，内存 / 根文件系统 / 删除 / 权限位，心跳进程 pid 证明内存真的回来了 | 打出 `✓ 59 项校验全部通过。` | `checkpoint_verify.log` |

两件要顺手确认的事：

- **59 还是 57。** 57 说明 SDK 没有 `mem_mode` 字段，"是不是增量"那两条断言被跳过——这轮仍是有效的正确性验收，但**不能证明用的是增量**。重装覆盖层（先 `install.py`，后 `patch_e2b.py`）。
- **脏页后端。** `checkpoint_verify.log` 开头 `脏页后端 : …` 是 Firecracker 自己报的：950 应为 HDBSS（硬件标脏），显式开跟踪的无 HDBSS 机器为 KVM 写保护。能力行只回答"开没开"，回答不了这一项。

---

## 4. func

默认跑 crtest 里**不需要重启服务端、不改服务端 env** 的 16 个用例，短的在前：
`T12 T15 T22 T24 T23 T34 T32 T41 T39 T26 T14 T40 T25 T36 T11 T13`，
再加一项 SDK 异常语义 pytest（`crtest/sdktests/test_checkpoint_errors.py`：打桩服务端、驱动真实客户端，验 10 个 checkpoint 异常类与 `.reason`；不建沙箱）。
每个用例的一句话说明：`PYTHONPATH=rollback/scripts/crtest python3 -m crtest --list`；逐条判据见 [23 §5](23-testing-and-functional-verification.md#5-crtest-用例)。

另有 4 行 SKIP，SUMMARY 里写明原因：

| 用例 | 为什么默认跳 | 要跑的开关 |
|---|---|---|
| T18（重启后账本不跨重启） | 要重启 orchestrator，全机沙箱都会没 | `--allow-restart --restart-cmd "…"`（重启命令按本机部署方式给，脚本不替你猜） |
| T21（四条故障注入） | 要服务端带 `CHECKPOINT_FAULT_INJECT` 重启 | 按 [06 §5](06-configuration-and-capacity.md#5-改一个开关操作步骤) 改 env 重启后加 `--fault-cases` |
| T38（FC 侧 faulted 路径） | 要带 `rollback-fault-inject` 特性的 FC 并设 `FC_ROLLBACK_FAULT_INJECT=post_commit`；交付的 FC 不带 | 同上 |
| T37（水位闸与个数上限） | 要服务端带 `CHECKPOINT_MIN_FREE_BYTES` / `CHECKPOINT_MAX_PER_SANDBOX` 重启 | `--quota-cases` |

**交付态的机器上不建议跑这四条**（要改线上配置、重启或换 FC）；它们在 920B 上已覆盖（[25 §2.3](25-results-and-compliance.md#23-重启与注入类)），不影响交付结论。
解释器里没装 pytest 时那一项也判 SKIP，装上后重跑：`python3 -m pip install "pytest>=7.4,<8" "pytest-asyncio>=0.23,<0.24"`。

**PASS 判据**：每个用例退出码 0 = 通过，1 = 断言不过，3 = 前置不满足（记 SKIP）。整档 FAIL = 0。
某项 FAIL 时看 `crtest-T??.log` 的最后几段（"断言失败 / 期望 / 实际 / 依据"）和同目录 `T??.json`（`meta.failure` 是原文，`ops` 是每一步的原始记录）。

---

## 5. perf

| 项 | 脚本 | 产物 |
|---|---|---|
| 分档基准 | `crtest/bench/bench_tiers.py --tier-set short --passes 2` | `bench-tiers.log/.json`、`raw-*.jsonl` |
| 达标判定 | `crtest/bench/compliance.py` + `analyze.py` | **`compliance.log`**（达标表）、`analyze.log`（分段、拟合、离群、缓存漂移） |
| 串行长尾 | `crtest/bench/serial_restore.py -n 100`（单沙箱、单调用方、同一 checkpoint 连回 100 次；`-n` 可改） | `serial-restore.log/.json` |
| 并发 | `acceptance/checkpoint_concurrent.py --stages A,B`（A：跨沙箱扇出 1–16；B：同沙箱 4 线程争用） | `concurrent-ab.log/.json` |

**达标线**是客户给的参考指标：checkpoint ≤ 200 ms、restore ≤ 100 ms，量的是**客户端墙钟**；每遍开头那次全量 checkpoint **单列、不判定**（口径见 [24](24-performance-methodology.md)）。
**超线不判 FAIL**——超线是结论不是故障，看 `compliance.log` 末尾"按 p50 超线的档"。PASS 只表示没有失败的操作、没有现场不一致。

判读时还要看：`analyze.log` 里各档 `mem_mode` 应全为 `incremental`；数字所在平台（920B 为 `kvm-wp`、950 为 `hdbss`）写进留档。
920B 上的结果与判定在 [25](25-results-and-compliance.md)；**950 的性能数字待补**，在 950 上跑出的这一档就是它的来源。

---

## 6. long

```bash
bash rollback/scripts/950/run.sh long
```

只**打印**长测清单，不自动跑：这些都是小时级的，要单独安排、单独授权。清单包括：

- 并发 D 段长跑（`checkpoint_concurrent.py --stages D`，清单里是 8 沙箱 × 1800 s）与 C 段（沙箱生命周期；可能把 orchestrator 打挂，机器上有别人的沙箱时不要开）；
- 混合稳态全规模 `crtest T36`（16 沙箱 × 1800 s）；
- 串行 restore 5000 次；
- 分档基准全表（18 档 × 3 遍）与达标判定。

打印出的清单第一段用 `PY=$(command -v python3)` 取解释器，所以要在装了覆盖层的那个环境里照抄（与 §1 的 `$PY` 是同一个）；其余命令原样可用，每条都已 `tee` 到 `$OUT` 下。
长跑前确认 netns 数已稳定。`checkpoint_concurrent.py` 建沙箱时给的寿命是 3600 s，清单里的长跑都在这之内；把时长加到 1 小时以上时，沙箱会先被回收，结果作废。

---

## 7. 留档与回传

**一律留原始输出。** `run.sh` 自己把每项输出和 `run.log` 写进结果目录；手工跑的命令（`long` 清单、单个脚本）一律 `2>&1 | tee` 到文件，别靠终端回滚。例如单独再跑一次正确性验收：

```bash
OUT=rollback/scripts/950/results/$(date +%Y%m%d-%H%M%S)-manual; mkdir -p "$OUT"
"$PY" rollback/scripts/acceptance/checkpoint_verify.py 2>&1 | tee "$OUT/checkpoint_verify.log"   # $PY 见 §1
```

**每个结果目录配一份 `00-context.md`，没有它的日志不能被引用**——不带条件的数字一定会被单独拿去比较。先用下面这段把能自动取的填上（在最新的结果目录里生成）：

```bash
OUT=$(ls -1dt rollback/scripts/950/results/*/ | head -1)
p=$(pidof -s template-manager)
A=$(tr '\0' '\n' < /proc/$p/environ | sed -n 's/^NOMAD_ALLOC_DIR=//p')
{
  echo "# 00-context"
  echo "- 机器：$(hostname) · $(lscpu | sed -n 's/^Model name: *//p' | head -1) · $(uname -r)"
  echo "- orchestrator sha256：$(sha256sum /usr/bin/template-manager | cut -c1-16)"
  echo "- firecracker sha256：$(sha256sum /fc-versions/v1.13.1/firecracker | cut -c1-16)"
  echo "- RPM：$(rpm -q e2b-infra)"
  echo "- e2b：$("$PY" -m pip show e2b 2>/dev/null | sed -n 's/^Version: //p')，解释器 $PY"
  echo "- 产物盘：$(df -T /orchestrator/build | awk 'NR==2{print $1, $2}')"
  F=/orchestrator/build/checkpoint-capabilities.json   # 设了 DEFAULT_CACHE_DIR 就换成它下面的同名文件
  if grep -qE "^  \"pid\": $p,?\$" "$F" 2>/dev/null; then
    echo "- 能力文件（$F）："; tr -d '\n' < "$F" | tr -s ' '; echo
  else
    echo "- 能力行："
    ls -1v "$A"/logs/start.stdout.* | xargs grep -ah 'checkpoint capabilities' | tail -1
  fi
} > "$OUT/00-context.md"
cat "$OUT/00-context.md"
```

再手工补上：

| 字段 | 写什么 |
|---|---|
| 时间 | 起止时间（`SUMMARY.md` 里有开跑 / 收尾） |
| 命令与参数 | 原样抄；默认参数也写明"全部默认" |
| 脏页后端 | `checkpoint_verify.log` 开头那行（`hdbss` / `kvm-wp` / `off`） |
| 产物介质 | 真盘还是 loop 卷 |
| 模板规格 | `cpuCount` / `memoryMB` / `diskSizeMB` |
| 结果 | 各档 PASS/FAIL/SKIP；**项数照抄**（59 还是 57） |
| 这份数不能拿来干什么 | 至少一条：例如"不是性能基准""不能与另一台机器相减""--quick 短试跑" |

**回传什么**：各档的 `SUMMARY.md` 与 `00-context.md` 一定带；有 FAIL 的项带它的 `.log` 与 `.json`；perf 档另带 `compliance.log`、`analyze.log`、`serial-restore.json`。不需要打包整个目录。

---

## 8. 小结

1. 在宿主机上、用装了覆盖层的解释器、从仓库根目录跑；凭据用 `benchmark/sync-env.sh` 生成。
2. `smoke`（≤ 3 分钟，5 项全过、59 项）→ `func`（≤ 40 分钟，FAIL 0）→ `perf`（≤ 40 分钟，超线不算失败）；`long` 只打印清单。
3. 交付态不跑要重启、改 env 或换 FC 的四条用例。
4. 每个结果目录配 `00-context.md`，写全二进制 sha、脏页后端、产物盘、模板规格与"这份数不能拿来干什么"。
