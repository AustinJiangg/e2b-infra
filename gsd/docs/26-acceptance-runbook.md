# 26 · 上机验收

## 本章目标

读完本章，你应该能：

1. 在宿主机上把验收环境准备好：gsd 栈、模板、Python 环境、SDK 路径、凭据；
2. 按固定顺序跑完部署自检与五个验收脚本，对每一步说出期望的末行和判据；
3. 知道哪些脚本的退出码不能当判定用，该看输出里的哪一行；
4. 决定要不要加跑压测，以及怎么带看门狗跑、怎么判一组压测是否通过；
5. 把结果按固定的目录结构留档并回传，写清这份结果的条件。

上一章（[25](25-troubleshooting.md)）按症状讲了排障。本章是第六部分的最后一章，也是交付的最后一步：按 [23](23-deployment.md) 部署完之后，把 checkpoint 与 restore 在这台机器上验一遍。
每个脚本测什么、判据为什么长成那样，在 [19](19-testing-and-debug-kernel.md)；每一步该看到什么数，在 [20](20-results-and-gaps.md)——**本章不另写数字**，需要对数的地方都指向 20 章的具体小节。
跑的过程中某一步不过，按症状去查 [25](25-troubleshooting.md)。

---

## 1. 跑之前

**在哪跑。** 在运行 gsd 栈的那台宿主机上跑。脚本通过 SDK 起沙箱，其中几个还要在 guest 里直连 gsd、读 guest 的 dmesg 与 journal；压测的看门狗要读宿主上 orchestrator 的日志取串口输出。

**这台机器是不是只有你在用。** 验收要建模板、起几十个沙箱，压测还会明显抬高机器负载。开始之前确认机器上没有别人的沙箱在跑；gsd 栈运行期间建的**所有**模板都会被注入 gsd（[23](23-deployment.md)），所以验收期间别人也不要在这台机器上建模板。

**gsd 栈在运行，三个文件是要验的那一套。** 宿主上带 gsd 注入的 orchestrator 已经在服务，gsd 二进制与补丁内核在运行时布局的位置上：

```bash
/fc-gsd/gsd -commit                                   # 应输出要验收的提交号
sha256sum /fc-gsd/gsd /fc-gsd/orchestrator /fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin
```

三个 sha256 与交付清单逐一相同；验收的若是 [20](20-results-and-gaps.md) 所述的那个版本，值在 20 章"条件标签"一节。再确认正在运行的 orchestrator 就是这一个二进制、环境变量符合 [23](23-deployment.md) 的两种机器形态之一；
新 920B 上用随手册附带的 `scripts/ops/switch-gsd-new920b.sh status` 看（它只读，不改任何东西），其他机器按本机的部署方式核对。

**模板。** 在 gsd 栈下建好别名为 `base-gsd` 的模板：2 vCPU / 2048 MB，不使用缓存（[23](23-deployment.md)）。所有脚本的默认模板名都是它。
模板固化了建模板时的内核、gsd 与 overlay-init——**换过三者中的任何一样而没有重建模板，接下来测的就是旧的那一套**，这是最常见的白跑原因，§3 的部署自检专门查这一点。
规格与 20 章不同（更多 vCPU、更大内存）时功能判定仍然有效，但性能数字不能与 20 章对照。

**Python 环境与 SDK。** 用装好 SDK 依赖的 conda 环境（新 920B 上的例子是 `jll-e2b-gsd`，其他机器用自己的环境名），并让 `PYTHONPATH` 指向代码仓的 `py-sdk` 目录：

```bash
conda activate jll-e2b-gsd                           # 新 920B 上的环境名；其他机器换成自己的
export PYTHONPATH=<代码仓>/py-sdk
python -c 'import e2b; print(e2b.__file__)'           # 应落在 <代码仓>/py-sdk 下
```

最后一条如果打印的是 conda 环境自己 `site-packages` 里的路径，说明 `PYTHONPATH` 没生效，脚本会用到一份没有 checkpoint 接口或接口较旧的 SDK，表现为返回值里缺字段、若干断言失败。

**脚本与凭据。** 验收脚本在 `e2b-infra/gsd/scripts/` 下，分 `acceptance/`、`stress/`、`probes/` 三个目录；本章的命令都在 `e2b-infra/gsd/scripts/` 目录里执行。
脚本通过 `e2b_env.py` 读取客户端凭据（API 地址、key、token），它会在脚本自身所在目录和上一级目录里找这个文件，所以把它放在 `scripts/` 下就能被三个子目录里的脚本共用；先确认它在位并且读得到凭据：

```bash
python -c 'import sys; sys.path[:0]=["."]; from e2b_env import load_e2b_env; print(sorted(load_e2b_env()))'
```

`verify_multitree.py` 复用 `verify_fixes.py` 里的沙箱封装，两个文件必须在同一个目录下。`stress/run_stress_watch.sh` 用环境变量 `PY` 指定解释器（默认 `python3`），SDK 路径沿用调用者的 `PYTHONPATH`；`fc_console.sh` 用 `E2B_INFRA_ENV`、`NOMAD_JOB` 找本机 orchestrator 的日志，换机器先核对这几个变量。

**准备一个结果目录。** 后面每一步都把输出重定向进去：

```bash
OUT=$PWD/results/$(date +%Y%m%d-%H%M)-acceptance; mkdir -p $OUT/func $OUT/stress
```

---

## 2. 顺序一览

按下表从上到下跑。前六步是必跑的，跳过 V12 时全部跑完大约六分钟，带上 8 轮 V12 大约十一分钟；压测可选，四组加起来再要几分钟。"耗时"一栏是 2026-10-08 在新 920B 上各步的墙钟，只用来安排时间，不是性能指标。

| 步 | 脚本 | 耗时 | 期望的末行 | 判据 | 退出码可信 |
|---|---|---|---|---|---|
| 1 | 部署自检（§3） | 不到 1 分钟 | — | 十项逐一符合 | — |
| 2 | `verify_gsd.py` | 约 10 秒 | `判定: 根盘恒定 ✓` | 末行；开头四行环境信息人工核对 | 否 |
| 3 | `verify_fixes.py` | 约 2 分钟 | `合计 28 项，FAIL 0: []` | PASS 26 / FAIL 0 / SKIP 2 | 是 |
| 4 | `verify_multitree.py` | 跳过 V12 约 1 分钟；带 8 轮 V12 约 6 分钟 | `合计 13 项，FAIL 0: []`，其后一行慢调用统计 | 全部 PASS | 是 |
| 5 | `verify_clear_refs_race.py` | 约 30 秒 | `有漏页的轮数: 0 / 30   漏页合计 0` | 漏页轮数为 0，每轮恢复后为 T、哈希相等 | 否 |
| 6 | `demo_checkpoint_perf.py` 连跑三次 | 新版每次约 20～30 秒 | `判定: PASS` | 每次 6 行 restore 全部 `OK`，完整内容与计时校验通过 | 是 |
| 7（可选） | `probe_whiteout_bug.py` 与四组压测（§5） | 探针约半分钟；压测两两并行，每对一分半钟左右 | `RESULT` 为 `DONE rc=0 …` | §5 的判据表 | 部分 |

"退出码可信"为"否"的两步，脚本即使判定不过也返回 0，自动化编排时必须另外检查输出（原因见 [19](19-testing-and-debug-kernel.md)）。

任何一步不过都先停下来查，不要接着往下跑：后面的步骤建立在前面的结论上，例如部署自检没过就意味着模板不是要验的那一个，之后所有结果都没有意义。

---

## 3. 第 1 步：部署自检

目的是确认"模板里装的就是要验的那一套"。起一个沙箱，在 guest 里把十项一次看完：

```bash
python - <<'EOF' | tee $OUT/guest-check.txt
import os, sys
sys.path[:0] = ["."]
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox
sb = Sandbox.create(template="base-gsd", timeout=300)
try:
    print("template_id", sb.get_info().template_id)
    print(sb.commands.run(
        "echo commit: $(/usr/bin/gsd -commit); sha256sum /usr/bin/gsd; cat /proc/version; "
        "echo thp: $(cat /sys/kernel/mm/transparent_hugepage/enabled); "
        "echo rcu_expedited: $(cat /sys/kernel/rcu_expedited); "
        "echo gsd.service: $(systemctl is-active gsd); "
        "echo health: $(curl -s -m 5 http://127.0.0.1:49984/health); "
        "/usr/sbin/criu --version | head -1; "
        "stat -c '%n %a %U:%G' / /tmp; df /tmp | tail -1; nproc; grep MemTotal /proc/meminfo",
        user="root", timeout=60).stdout)
finally:
    sb.kill()
EOF
```

| 项 | 期望 | 不符合说明什么 |
|---|---|---|
| guest 内 `gsd -commit` | 等于宿主上 `/fc-gsd/gsd -commit` 的输出 | 模板是换 gsd 之前建的，要重建模板 |
| guest 内 `/usr/bin/gsd` 的 sha256 | 等于宿主上 `/fc-gsd/gsd` 的 sha256 | 同上 |
| `/proc/version` 的内核版本串与构建时间 | 等于要验收的那个内核的版本串（[20](20-results-and-gaps.md) 的条件标签） | 模板是换内核之前建的，或 orchestrator 取内核的目录不对（[23](23-deployment.md)） |
| THP | `always madvise [never]` | overlay-init 不是当前版本；增量会退化成接近全量（[02](02-criu-and-incremental-memory.md)） |
| `rcu_expedited` | `1` | gsd 不是当前版本或没有正常启动（[10](10-restore.md)） |
| `gsd.service` | `active` | 见 [25](25-troubleshooting.md) |
| `/health` | `{"status":"ok"}` | 返回 degraded 时 body 里带原因（[12](12-failure-semantics.md)） |
| criu 版本 | `Version: 4.2.1` | 基础镜像不对（[23](23-deployment.md)） |
| `/` 与 `/tmp` 的权限 | `/ 755 root:root`，`/tmp 1777 root:root` | overlay-init 不是当前版本（[05](05-guest-boot-and-injection.md)） |
| `df /tmp` 的第一列 | `overlay` | `/tmp` 被单独挂载了，不会随 restore 回滚 |

最后两行（vCPU 数与 `MemTotal`）不是判据，记下来作为这轮结果的条件。`/health` 是免鉴权的，所以上面的 curl 不用带 token；在 guest 里手工调其他接口要带 `X-Access-Token`（[04](04-architecture.md)）。

---

## 4. 第 2～6 步：功能验证与性能

### 4.1 `verify_gsd.py`

```bash
python acceptance/verify_gsd.py base-gsd 8 2>&1 | tee $OUT/func/verify_gsd.log
```

**期望。** 开头四行环境信息与 §3 一致（提交号、内核、`rcu_expedited` 为 1、THP 为 never）；8 行 restore 每行 `ok=True`、根盘已用量相对起点都是 `(+0)`；末行 `判定: 根盘恒定 ✓`。

**对数。** `total_ms` 与各分段的 p50 对照 [20](20-results-and-gaps.md)"`verify_gsd.py`：单棵 192 MiB"一节的表。同一台机器、同一规格、同一版本下，重复跑的读数应当落在那张表的最小值与最大值之间或其附近；怎么算"附近"见 §6。

**不过时。** 末行是 `根盘仍在增长 ✗`：内核不是当前版本或模板没有重建（[16](16-restore-slowness-case.md) 讲了这个现象的来历）。某次 `ok=False`：看 [25](25-troubleshooting.md) 里 restore 失败的各条。

### 4.2 `verify_fixes.py`

```bash
python acceptance/verify_fixes.py --template base-gsd --out-json $OUT/func/verify_fixes.json 2>&1 | tee $OUT/func/verify_fixes.log
```

**期望。** 汇总表 28 行，其中 G-19 与 G-36 为 SKIP，其余 26 行为 PASS；末行 `合计 28 项，FAIL 0: []`。再做两个检查：

```bash
grep -c 'BAD ' $OUT/func/verify_fixes.log        # 应为 0
grep -n '可疑行' $OUT/func/verify_fixes.log       # 应无输出
```

第二条不能省：脚本把各沙箱 dmesg 里按关键词命中的行只记作证据，不计入 PASS/FAIL。

**对数。** 这个脚本以通过与否为准。日志里几处带数的证据行（层栈上限的次数、容量预检的拒绝信息、鉴权开销、循环后的根盘增量）可以对照 [20](20-results-and-gaps.md)"`verify_fixes.py`"一节的表，其中层栈上限的次数只取决于代码，应当完全相同。

**不过时。** 用 `--only <项>` 单独重跑失败的那一项，日志里该项的 `BAD` 行写明了期望与实测。G-08 出现 INFO、SKIP 多于两项，同样要看原因。G-22 那一项要一分钟左右，期间沙箱对 SDK 暂时不可达是预期的。

### 4.3 `verify_multitree.py`

没有对照模板时跳过 V12：

```bash
python acceptance/verify_multitree.py --template base-gsd --skip V12 --out-json $OUT/func/verify_multitree.json 2>&1 | tee $OUT/func/verify_multitree.log
```

**期望。** 汇总表 12 行全部 PASS，`合计 12 项，FAIL 0: []`；汇总表里没有重印出来的 `BAD` 行。带上 V12 时是 13 行、`合计 13 项`。同样搜一下 `可疑行`。

最后一行是慢调用统计。它不影响判定；次数不为 0 时把那一行连同 JSON 里的 `slow_calls` 一起留档——这是一个未查清的现象（[15](15-known-limits.md)、[20](20-results-and-gaps.md) 的"`verify_multitree.py`"一节），多一份现场就多一分线索。

**什么时候跑 V12。** V12 是性能项，有两种用途，各有前提：

- 想知道这台机器上一、二、三棵树各要多久：不给对照模板直接跑 `--only V12`，结果对照 [20](20-results-and-gaps.md)"V12"一节的第二张表。这种用法只记录、不判定；
- 想确认"新旧两个版本性能没有差别"：需要机器上另有一个用旧版本建的模板，用 `--old-template <旧模板> --perf-rounds 8` 跑，脚本会对八个分段的中位数逐条判 ±3 ms。
  两个模板必须只差你想比较的那一样东西；内核固化在模板里，想比较内核就得两个内核各建一个模板（[17](17-performance-after-fixes.md)）。

首次部署的机器上没有旧模板，第二种用法无从谈起，跳过即可。

### 4.4 `verify_clear_refs_race.py`

```bash
python acceptance/verify_clear_refs_race.py --template base-gsd --rounds 30 --pages 4096 --rate 100000 \
    --jsonl $OUT/func/clear_refs_race.jsonl 2>&1 | tee $OUT/func/clear_refs_race.log
```

注意 `--jsonl` 是追加写，重跑前先删掉旧文件。

**期望。** 30 行轮次记录，每行 `漏页 diff=    0`、`restored_state=T`、`sha_eq=True`；汇总里 `有漏页的轮数: 0 / 30   漏页合计 0`。汇总标题行的轮数应当是 30：如果少于 30 并且出现"排除 writer 卡死后的 N 轮"，说明写入进程中途停止了写入，这一轮验收无效，要查原因后重跑。

**对数。** 停住时长与增量镜像大小对照 [20](20-results-and-gaps.md)"`verify_clear_refs_race.py`"一节。判定只看漏页。

**不过时。** 任何一轮漏页不为 0 都是严重问题：它意味着增量 checkpoint 会静默地恢复出错误的内存内容。先确认 guest 里的 gsd 是当前版本（§3），再按 [09](09-checkpoint.md) 的"为什么 `--leave-stopped`"一节核对。

### 4.5 `demo_checkpoint_perf.py`

```bash
for i in 1 2 3; do
  TEMPLATE=base-gsd THP=never OUT_JSON="$OUT/func/perf-run$i.json" \
    python acceptance/demo_checkpoint_perf.py > "$OUT/func/perf-run$i.log" 2>&1 || exit 1
  sleep 8
done
python acceptance/summarize_checkpoint_perf.py \
  "$OUT/func/perf-run1.json" "$OUT/func/perf-run2.json" "$OUT/func/perf-run3.json" \
  --output "$OUT/func/perf-summary.json"
```

**期望。** 每轮末行为 `判定: PASS`，6 次恢复全部 `OK`；完整内存 SHA256、标记和文件内容均恢复到各自基点，SDK 与 journal 分段一致。v2 脚本及汇总脚本失败都返回非零；汇总会拒绝旧口径、重复文件、不完整档位、错误父节点或恢复校验失败的记录。

**口径。** 每档独立沙箱，只分配该档内存；初始化后建全量基点，改写本档负载，测增量 create，再恢复本档基点。每档均为新沙箱首次恢复，0 档仍包含解释器和通信开销。首次全量基点与客户端墙钟单列，正文表使用服务端 `total_ms`。

**对数。** 三轮中位数对照 [20](20-results-and-gaps.md) §3.4；该表的进程大小与改动量同时变化。§3.3 是旧的固定 192 MiB、连续建链结果，不能与 v2 混合或要求新版各档仍接近最大档。换到 950 后另存一组结果、记录版本与机器条件，再并列比较。

---

## 5. 可选：探针与压测

功能验证全部通过之后，下面这些回答的是另一个问题：内核侧在并发文件操作下是否稳定。以下情况应当跑：第一次在一台机器上部署、换了 guest 内核、改了内核补丁。只换了 gsd 二进制而内核没动时可以不跑。
压测脚本各自做什么见 [19](19-testing-and-debug-kernel.md)。

### 5.1 `probe_whiteout_bug.py`

```bash
python probes/probe_whiteout_bug.py base-gsd --cases bug,ctl,nodel,masked,trap,trap,trap \
    > $OUT/func/probe-whiteout.json 2> $OUT/func/probe-whiteout.log
cat $OUT/func/probe-whiteout.log
```

**期望。** 七行，每行 `crashed=False`；三行 `trap` 都是 `errs={}`；各行末尾 `dmesg=` 之后没有内核报错的内容（前四个用例显示为 `('ok', '')`，`trap` 用例为空）。结果对照 [20](20-results-and-gaps.md)"`probe_whiteout_bug.py`"一节。

出现 `crashed=True` 或者某个用例一直不返回：guest 内核是修复之前的版本。这种情况下不要接着跑压测，回去查模板里的内核（§3）。

### 5.2 四组压测

四组都带看门狗跑，各有自己的输出目录。并行跑的组数多了单次操作会变慢，所以把看门狗的停滞阈值放宽：

```bash
export STALL=150
A="--template base-gsd --mode stress --rounds 300"
bash stress/run_stress_watch.sh $OUT/stress/all       $A --workers 6 &
bash stress/run_stress_watch.sh $OUT/stress/noworkers $A --workers 0 &
wait; cat $OUT/stress/all/RESULT $OUT/stress/noworkers/RESULT

SCRIPT=stress_multitree.py    bash stress/run_stress_watch.sh $OUT/stress/multitree --template base-gsd --mode multitree --rounds 200 &
SCRIPT=stress_delete_dense.py bash stress/run_stress_watch.sh $OUT/stress/deldense  --template base-gsd --mode deldense  --rounds 200 &
wait; cat $OUT/stress/multitree/RESULT $OUT/stress/deldense/RESULT
```

`--mode` 的取值要与上面一致：它决定输出文件名的前缀，看门狗靠它找日志。想跑完整的八组（再加 readonly、wr3、rename、append、reltouch 与 enospc），各组的 `--ops` 写法在 [19](19-testing-and-debug-kernel.md)"`stress_overlay_ckpt.py`"一节的分组表里。

**判据。** 每组都要同时满足下表；前两行四组通用。

| 条件 | 怎么看 |
|---|---|
| 看门狗没有判定卡死 | `RESULT` 以 `DONE rc=0` 开头。`WEDGED` 表示 guest 卡死，同目录的 `console.txt` 是当时的串口输出 |
| 串口输出关键词零命中 | 见下面的 grep |
| all、noworkers：dmesg 无新命中，worker 错误只有 `ENOENT` 与 `ESTALE`，没有心跳过期，gsd 侧只有 `create:resource_exhausted` | 见下面的 Python 片段；脚本日志末尾的 `判定:` 行列出了同样的四个计数 |
| 多棵树、删文件密集：脚本自己的判定为 PASS | `multitree.log`、`deldense.log` 里的 `结果 PASS: …` 行，或 `*-summary.json` 的 `verdict` |
| 没有预期之外的 create 失败 | 各输出目录里没有 `*-create-fail-*` 文件 |

```bash
K='KASAN|BUG:|Oops|WARNING:|list_add corruption|list_del corruption|list corruption|possible circular locking|possible recursive locking|sleeping function called from invalid context|ODEBUG|blocked for more than|hung_task|soft lockup|rcu.*stall|detected stalls'
grep -a -c -E "$K" $OUT/stress/*/console.txt        # 每个文件应为 0
ls $OUT/stress/*/*-create-fail-* 2>/dev/null        # 应无输出

python - $OUT/stress/all $OUT/stress/noworkers <<'EOF'
import json, sys
for d in sys.argv[1:]:
    j = json.load(open(d + "/stress-summary.json"))
    bad = {
        "dmesg 新命中": j["dmesg_new_hits"],
        "worker 的其他 errno": {k: v for k, v in j["worker_errs_by_errno"].items() if k not in ("ENOENT", "ESTALE")},
        "心跳过期": {k: v for k, v in j["worker_heartbeat_age_s"].items() if v > 10},
        "gsd 的其他错误": {k: v for k, v in j["gsd_ops"]["errors"].items() if k != "create:resource_exhausted"},
    }
    print(d, "通过" if not any(bad.values()) else "不通过 %s" % {k: v for k, v in bad.items() if v}, j["gsd_ops"])
EOF
```

`stress_overlay_ckpt.py`（all 与 noworkers 两组用的脚本）自己不给出 PASS 或 FAIL，退出码也不反映判定，所以要有上面这段。

**对数。** 压测以通过与否为准。create / restore / delete 的成功次数、worker 的错误分类可以对照 [20](20-results-and-gaps.md)"本轮四组"一节：脚本用固定的种子，同样的代码在 all 与 noworkers 两组上走的是同一条操作序列，同一规格下这三个次数应当与那一节的表相同或非常接近（create 因容量不足被拒的次数会随镜像大小略有出入，并带动后面的序列）；
worker 的操作总数和 `ENOENT`、`ESTALE` 的次数随机器负载变化，不必相同。结果文件里脚本侧的墙钟耗时不是 gsd 的耗时，不要拿来对数。

**不过时。** 把整个输出目录原样留好：`console.txt`、各个 `*-dmesg-*.txt`、`*-ops.log` 是定位内核问题的全部现场，沙箱一销毁就再也取不到。然后按 [25](25-troubleshooting.md) 与 [07](07-overlay-concurrency-and-fixes.md) 对应的主题查。

### 5.3 调试内核

改过内核补丁时，上面的压测要在调试内核上再跑一遍：切到调试内核目录、另建一个 4096 MB 的调试模板、跑同样的命令（把 `--template` 换成调试模板，`STALL` 再调大），
并在每组结束后确认汇总里的 `debug_locks` 仍为 1。步骤与原因在 [19](19-testing-and-debug-kernel.md)"调试内核"一节。只是部署既有版本、没有改内核时不需要这一步。

---

## 6. 性能数字怎么对

验收的判定是**功能性**的：§2 那张表里每一步的判据都是"对或不对"。性能数字没有达标线，对数的目的是发现"这台机器上的 gsd 与验证过的那一套是不是同一个东西"，而不是打分。

对的时候记住三条：

1. **条件相同才对得上。** [20](20-results-and-gaps.md) 的数是在它"条件标签"一节的条件下测的：新 920B、2 vCPU / 2048 MB、THP never。规格或机器不同，数字没有可比性，只留档、不比较。950 上跑出的数就属于这种情况——20 章没有 950 的数可对。
2. **口径相同才对得上。** `verify_gsd.py` 的数只和 20 章 `verify_gsd.py` 的表比，V12 的数只和 V12 的表比。几个脚本里同名的 `total_ms` 负载不同，互相之间不能比。
3. **波动范围按 V12 的判据理解。** V12 对"两份本应相同的东西"用的判据是各分段中位数相差不超过 ±3 ms，20 章的实测是这样的两份东西差 1 ms 上下。所以条件与口径都相同时，各分段的中位数与 20 章相差在 3 ms 以内视为一致；
   超出这个范围不等于有故障，但值得找原因——最常见的是模板没有重建（§3 能查出来）、机器上同时有别的负载、THP 被改过。`demo_checkpoint_perf.py` 每档只有单个样本，起伏更大，按 20 章那一节给出的幅度看。

---

## 7. 留档与回传

把这一轮整理成与 [20](20-results-and-gaps.md) 所引的结果目录相同的结构，之后才能被引用、被对比：

```
<日期>-acceptance/
  summary.txt            条件、各步判定、与 20 章的对照、发现的问题（人写）
  deliverables.sha256    宿主上三个文件的 sha256
  guest-check.txt        §3 的 guest 内自检输出
  func/                  verify_gsd.log、verify_fixes.{log,json}、verify_multitree.{log,json}、
                         clear_refs_race.{log,jsonl}、perf-run{1,2,3}.{log,json}、probe-whiteout.{log,json}
  stress/<组名>/         RESULT、console.txt、stdout.log、<前缀>.log、<前缀>-ops.log、
                         <前缀>-summary.json、<前缀>-dmesg-*.txt
```

`deliverables.sha256` 用 §1 那条 `sha256sum` 的输出。`summary.txt` 至少写清下面几项——没有条件的数字迟早会被人单独拿去和别的数字比较：

| 项 | 写什么 |
|---|---|
| 机器与时间 | 机器名或地址、起止时间 |
| 版本 | 代码提交号；gsd、orchestrator、内核三个 sha256；内核版本串；模板别名与 ID |
| guest 规格 | vCPU 数与内存（§3 输出的最后两行）、THP 设置 |
| 命令与参数 | 原样抄下来；用默认值的也写明 |
| 各步判定 | 每步通过与否；`verify_fixes.py` 的 PASS / FAIL / SKIP 计数**照抄**（应为 26 / 0 / 2）；慢调用次数 |
| 与 20 章的对照 | 条件是否相同；相同的话各口径的 p50 与 20 章的差 |
| 没跑的部分 | 例如"V12 没有对照模板，跳过""压测未跑""调试内核未跑" |
| 这份结果不能拿来做什么 | 至少一条，例如"规格与 20 章不同，性能数字不可比""压测时机器上另有负载，worker 操作数不可比" |

**回传什么。** `summary.txt`、`deliverables.sha256`、`guest-check.txt` 一定带；功能验证带 `func/` 下的 JSON 与日志（都不大）；压测通过的组带 `RESULT` 与 `*-summary.json` 即可，没通过的组带整个目录。结果目录里有沙箱 ID 与本机路径，不入库。

---

## 8. 950

950 上未部署、未测，本章的步骤没有在 950 上走过。950 的验收要补哪些内容、与本章有哪些不同，**待补**。可以预先确定的只有两点：gsd 只支持 arm64 的 guest（[03](03-goals-and-tradeoffs.md)），950 满足这一条；
[20](20-results-and-gaps.md) 里没有 950 的 gsd 数字，已有 950 对照仅为沙箱级，在 950 上第一次跑出的这一轮结果就是它的基线，§6 的"对数"在那之前无从谈起。

---

## 本章要点

- 在运行 gsd 栈的宿主机上、用装好 SDK 依赖的 conda 环境（新 920B 上是 `jll-e2b-gsd`，其他机器用自己的环境名）加代码仓的 `py-sdk`、对模板 `base-gsd` 跑；开始前确认三个文件的 sha256、确认机器上没有别人的沙箱。
- 顺序固定：部署自检 → `verify_gsd.py` → `verify_fixes.py` → `verify_multitree.py` → `verify_clear_refs_race.py` → `demo_checkpoint_perf.py` 三次 → 可选的探针与压测。前一步不过就停下来查。
- 部署自检查的是"模板里装的是不是要验的那一套"：guest 内 gsd 的提交号与 sha256、内核版本串、THP、`rcu_expedited`、`/health`、criu 版本、`/` 与 `/tmp`。换过内核、gsd、overlay-init 而没有重建模板，是最常见的白跑原因。
- `verify_gsd.py`、`verify_clear_refs_race.py`、`demo_checkpoint_perf.py` 与 `stress_overlay_ckpt.py` 的退出码不是判定，分别看末行的根盘判定、漏页轮数、`OK` 行数、以及按判据表核对汇总 JSON。
- `verify_fixes.py` 通过的样子是 PASS 26 / FAIL 0 / SKIP 2；它和 `verify_multitree.py` 都把 dmesg 的可疑行只记作证据，要另外搜一遍。
- 压测在首次部署、换内核、改补丁时跑，一律带看门狗；判据是 `RESULT` 为 `DONE rc=0`、dmesg 与串口关键词零命中、worker 只有允许的 errno、没有心跳过期、gsd 侧只有 `resource_exhausted`。改过内核补丁还要在调试内核上再跑一遍并确认 `debug_locks` 为 1。
- 验收的判定是功能性的，性能数字没有达标线。条件与口径都与 20 章相同时，各分段中位数相差 3 ms 以内视为一致；条件不同就只留档、不比较。
- 结果按 `summary.txt`、环境核对文件、`func/`、`stress/` 的结构留档，`summary.txt` 写清条件与"这份结果不能拿来做什么"。950 的验收待补。
