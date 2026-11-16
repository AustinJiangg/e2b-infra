# 22 · 950 结果

> **状态：待填。** 950 的进程级尚未运行。本章的表由离线包跑完之后填写，现在所有单元格都是 `待填`，没有任何数字。这是填写 950 结果的唯一位置。

## 本章目标

读完本章，你应该能：

1. 知道 950 的进程级验证由哪几步组成，每一步的结果落在哪个文件；
2. 按本章的表收集和填写 950 的部署条件、功能验收、探针、四组压测和分档性能；
3. 填数之前核对条件，判断一轮结果是否有效。

本章不给通过阈值。950 的第一轮结果是它自己的基线，920B 的耗时不能作为 950 的通过标准。四组压测和功能验收的判据在 [18](18-testing-and-debug-kernel.md)，表的结构与 [19](19-920b-functional.md)、[20](20-920b-performance.md) §1、[21](21-920b-stress.md) §2 相同，以便并排阅读。同一流程在新 920B 上跑出的结果就是那几节。

---

## 1. 结果从哪里来

离线包的入口是 `ops/950.sh`。验证相关的三步：

| 入口 | 内容 | 结果目录 |
|---|---|---|
| `bash ops/950.sh acceptance` | guest 自检、`verify_gsd.py`、`verify_fixes.py`、`verify_multitree.py --skip V12`、`verify_clear_refs_race.py`、`demo_checkpoint_perf.py` 三轮 | `results/<时间>-acceptance/` |
| `bash ops/950.sh stress` | `probe_whiteout_bug.py` 七个用例，然后 all、noworkers、multitree、deldense 四组 | `results/<时间>-stress/` |
| `bash ops/950.sh sandbox`，再 `python ops/sandbox_smoke.py` | 切回沙箱级并冒烟 | `results/sandbox-smoke/` |

两个结果目录里都有 `summary.json`（每一步的命令和状态、判定 `verdict`、机器名、起止时间、Python 与 SDK 路径）和 `deliverables.sha256`（三个运行文件的 sha256）。`acceptance` 的 `summary.json` 还有 `slow_calls` 和 `skipped`。下面各表的"来源"列写的都是相对这两个目录的文件名。

分档性能的三轮结果用 `criu-rollback/scripts/acceptance/summarize_checkpoint_perf.py` 校验并汇总，命令见 [验证摘要](../validation/README.md)。950 的摘要另建带平台与日期的目录，放在 `criu-rollback/validation/` 下，不覆盖 920B 的目录。

结果里有几项不进入这些文件，要在运行时另外记下：机器名、起止时间、宿主内核版本、未跑的项目和回切冒烟结果。回传时不带 `criu-rollback/scripts/.env`、状态目录、Nomad job JSON 和登录配置。

---

## 2. 部署条件

| 项 | 值 | 来源 |
|---|---|---|
| 平台 | `待填` | 运行时记录 |
| 日期与起止时间 | `待填` | `summary.json` 的 `started`、`finished`；核对机器时钟 |
| 宿主内核 | `待填` | 宿主上 `uname -r` |
| 部署版本 | `待填` | 运行时记录 |
| 提交：`gsd -commit` | `待填` | `guest-check.log` 的 `commit` |
| 提交：SDK | `待填` | wheel 版本号 |
| gsd sha256（宿主 / guest） | `待填` | `deliverables.sha256`；`guest-check.log` 的 `gsd_sha256` |
| orchestrator sha256 | `待填` | `deliverables.sha256` |
| guest 内核 sha256 | `待填` | `deliverables.sha256` |
| guest 内核版本串 | `待填` | `guest-check.log` 的 `kernel` |
| CRIU 版本 | `待填` | `guest-check.log` 的 `criu` |
| THP | `待填` | `guest-check.log` 的 `thp` |
| `rcu_expedited` | `待填` | `guest-check.log` 的 `rcu` |
| guest 规格（vCPU / 内存） | `待填` | `guest-check.log` 的 `vcpus`、`memory` |
| `/`、`/tmp` 的权限与 `/tmp` 所在文件系统 | `待填` | `guest-check.log` 的 `root`、`tmp`、`tmp_fs` |
| 模板别名与 ID | `待填` | `summary.json` 的 `template`；`guest-check.log` 首行 |
| 建模板耗时 | `待填` | `build_template.py` 末行 |
| Python 与 SDK 路径 | `待填` | `summary.json` 的 `python`、`sdk` |

---

## 3. 功能验收

### 3.1 各步判定

| 步骤 | 判定 | 来源 |
|---|---|---|
| guest 自检 | `待填` | `guest-check.log` |
| `verify_gsd.py`（8 次 restore） | `待填` | `verify_gsd.log` |
| `verify_fixes.py` | `待填` | `verify_fixes.log`、`verify_fixes.json` |
| `verify_multitree.py --skip V12` | `待填` | `verify_multitree.log`、`verify_multitree.json` |
| `verify_clear_refs_race.py` | `待填` | `clear_refs_race.log`、`clear_refs_race.jsonl` |
| 性能三轮 | `待填` | `perf-run1.json`、`perf-run2.json`、`perf-run3.json` |
| `acceptance` 总判定 | `待填` | `summary.json` 的 `verdict` |

### 3.2 计数与读数

| 项 | 值 | 来源 |
|---|---|---|
| `verify_fixes.py` PASS / FAIL / SKIP | `待填` | `verify_fixes.json` 的 `status` 计数 |
| `verify_fixes.py` SKIP 的项 | `待填` | 同上 |
| `verify_multitree.py` PASS 项数 | `待填` | `verify_multitree.json` 的 `items` |
| 慢调用次数 | `待填` | `verify_multitree.json` 的 `slow_calls` |
| 漏页：有漏页的轮数 / 总轮数，漏页合计 | `待填` | `clear_refs_race.log` 的汇总段 |
| 漏页：恢复后状态、哈希相等的轮数、写入进程卡死的轮数 | `待填` | `clear_refs_race.jsonl` 的 `restored_state`、`sha_eq`、`wedged` |
| 漏页：checkpoint A 的 `stopped_ms` p50 / 最小 / 最大 | `待填` | `clear_refs_race.log` 的 `A_stopped_ms` |
| 漏页：checkpoint A 的 `criu_dump_ms` p50 / 最小 / 最大 | `待填` | `clear_refs_race.log` 的 `A_dump_ms` |
| 漏页：checkpoint A 的 `overlay_sink_ms` p50 | `待填` | `clear_refs_race.log` 的 `A_sink_ms` |
| 漏页：checkpoint B 的页镜像大小 p50 / 最小 / 最大 | `待填` | `clear_refs_race.log` 的 `cpB 增量 pages` |
| G-06 全量镜像 / 增量镜像 | `待填` | `verify_fixes.json` 的 G-06 |
| G-08 成功次数、被拒时的选项串长度 | `待填` | `verify_fixes.json` 的 G-08 |
| G-11 `df /` 增量、journal 增量、扣除后净增 | `待填` | `verify_fixes.json` 的 G-11 |
| G-20 secure 与非 secure 的鉴权差 | `待填` | `verify_fixes.json` 的 G-20 |
| G-21 被拒时的估算、快照存储剩余、`MemAvailable` | `待填` | `verify_fixes.json` 的 G-21 |
| PERF `find_pids_ms` 中位数：checkpoint / restore | `待填` | `verify_fixes.json` 的 PERF |
| V5 全量 / 不改内存 / 起第三棵树后的镜像大小 | `待填` | `verify_multitree.json` 的 V5 |

`verify_multitree.py --skip V12` 不产生 V12 的两部分结果。V12 需要一份仅版本不同的进程级模板作对照，首次部署没有这样的模板，所以第 20 章 §3 的逐轮交错和一、二、三棵树分段在本章没有对应的表。

---

## 4. 探针

`probe_whiteout_bug.py` 七个用例，来源是 `whiteout.json`。

| 用例 | `crashed` | 开机时的 whiteout 数 | dmesg |
|---|---|---|---|
| `bug` | `待填` | `待填` | `待填` |
| `ctl` | `待填` | `待填` | `待填` |
| `nodel` | `待填` | `待填` | `待填` |
| `masked` | `待填` | `待填` | `待填` |
| `trap` 第 1 次 | `待填` | `待填` | `待填` |
| `trap` 第 2 次 | `待填` | `待填` | `待填` |
| `trap` 第 3 次 | `待填` | `待填` | `待填` |

---

## 5. 四组压测

all 与 noworkers 的来源是各子目录的 `stress-summary.json`，多棵树是 `multitree/multitree-summary.json`，删文件密集是 `deldense/deldense-summary.json`；看门狗结果是各子目录的 `RESULT`，串口证据是 `console.txt`。

| 项 | all（6 个 worker） | noworkers | 多棵树 | 删文件密集 |
|---|---|---|---|---|
| 轮数 | `待填` | `待填` | `待填` | `待填` |
| 判定 | `待填` | `待填` | `待填` | `待填` |
| 看门狗 | `待填` | `待填` | `待填` | `待填` |
| dmesg 新命中 | `待填` | `待填` | `待填` | `待填` |
| 串口关键词命中 | `待填` | `待填` | `待填` | `待填` |
| checkpoint / restore / delete 成功次数 | `待填` | `待填` | `待填` | `待填` |
| gsd 侧错误 | `待填` | `待填` | `待填` | `待填` |
| worker 操作总数 | `待填` | `待填` | `待填` | `待填` |
| worker 错误（按 errno） | `待填` | `待填` | `待填` | `待填` |
| 校验次数与失败数 | `待填` | `待填` | `待填` | `待填` |
| `mnt_cache` 起 → 止 | `待填` | `待填` | `待填` | `待填` |
| 结束时 `/health` | `待填` | `待填` | `待填` | `待填` |

| 项 | 值 | 来源 |
|---|---|---|
| 多棵树结束时的 `state.json`（版本、checkpoint 条目数、层数） | `待填` | `multitree-summary.json` 的 `state` |
| 多棵树 checkpoint 的 p50：`total_ms` / `criu_dump_ms` / `stopped_ms` | `待填` | `multitree.log` 的"性能 p50"行 |
| 多棵树 restore 的 p50：`total_ms` / `criu_restore_ms` / `kill_ms` | `待填` | 同上 |
| 删文件密集的宿主侧变更：删文件 / 整目录删除 / 重建目录 / 建文件 | `待填` | `deldense-summary.json` 的 `stats.mutate` |
| 删文件密集结束时的 whiteout / opaque 目录 | `待填` | `deldense-summary.json` 的 `marks_end` |
| `stress` 总判定 | `待填` | `summary.json` 的 `verdict` |

---

## 6. 分档性能

结构与 [20](20-920b-performance.md) §1 相同。服务端 `total_ms` 三轮中位数和墙钟中位数，单位 ms，来源是汇总出的 `summary.json`。

| 总改动量 MiB | 内存 / 文件 MiB | 全量基点 | 增量 checkpoint | restore | 全量基点墙钟 | 增量 checkpoint 墙钟 | restore 墙钟 |
|---|---|---:|---:|---:|---:|---:|---:|
| 0 | 0 / 0 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 16 | 12 / 4 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 32 | 24 / 8 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 64 | 48 / 16 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 128 | 96 / 32 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 256 | 192 / 64 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |

三轮的服务端逐轮样本：

| 总改动量 MiB | 全量基点 轮 1 | 轮 2 | 轮 3 | 增量 checkpoint 轮 1 | 轮 2 | 轮 3 | restore 轮 1 | 轮 2 | 轮 3 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 16 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 32 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 64 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 128 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |
| 256 | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` | `待填` |

各档实测 RSS：

| 总改动量 MiB | 轮 1 MiB | 轮 2 MiB | 轮 3 MiB |
|---|---:|---:|---:|
| 0 | `待填` | `待填` | `待填` |
| 16 | `待填` | `待填` | `待填` |
| 32 | `待填` | `待填` | `待填` |
| 64 | `待填` | `待填` | `待填` |
| 128 | `待填` | `待填` | `待填` |
| 256 | `待填` | `待填` | `待填` |

| 项 | 值 | 来源 |
|---|---|---|
| 恢复校验通过次数 | `待填` | 汇总 `summary.json` 的 `restore_checks_passed` |
| SDK 与 journal 分段最大差 | `待填` | 汇总 `summary.json` 的 `max_sdk_journal_diff_ms` |

`verify_gsd.py` 单棵 192 MiB、全脏、连续 restore 8 次的分段，结构与 [20](20-920b-performance.md) §2 相同，来源是 `verify_gsd.log`：

| 分段 | p50 | 最小 | 最大 |
|---|---:|---:|---:|
| `total_ms` | `待填` | `待填` | `待填` |
| `criu_restore_ms` | `待填` | `待填` | `待填` |
| `kill_ms` | `待填` | `待填` | `待填` |
| `find_pids_ms` | `待填` | `待填` | `待填` |
| `overlay_restore_ms` | `待填` | `待填` | `待填` |
| `precheck_ms` | `待填` | `待填` | `待填` |

| 项 | 值 |
|---|---|
| 根盘已用量起点与每次增量 | `待填` |

---

## 7. 回切沙箱级

| 项 | 值 | 来源 |
|---|---|---|
| `sandbox` 切换 | `待填` | 入口输出 |
| `status` 显示的二进制与 `/health` | `待填` | 入口输出 |
| `sandbox_smoke.py` PASS / FAIL / SKIP | `待填` | `results/sandbox-smoke/` 下的 `SUMMARY.md` |

---

## 8. 填数时要核对的条件

下面每一条都要在填表之前确认。有一条不符，先查原因，不把这轮结果填进来。

**产物与模板。**

1. `deliverables.sha256` 里 gsd、orchestrator、guest 内核三个 sha256 与离线包 `manifest.json` 一致，acceptance 和 stress 两个目录也互相一致。
2. `base-gsd` 是在 950 的进程级栈下新建的，不是沙箱级模板，也不是从别的机器带来的；建模板时没有加 `--use-cache`。
3. guest 自检通过：`gsd -commit` 为 `259f1a25c`，内核构建标识为 `#4 SMP Thu Oct  8 20:06:40 CST 2026`，CRIU 为 4.2.1，THP 为 `[never]`，`rcu_expedited` 为 1，vCPU 为 2。
4. 客户端用的是带 checkpoint 接口的进程级 SDK，`summary.json` 的 `sdk` 落在进程级 conda 环境里。

**功能。**

5. `verify_fixes.py` 是 PASS 26 / FAIL 0 / SKIP 2，SKIP 只能是 G-19、G-36。
6. `verify_multitree.py --skip V12` 是 12 项全 PASS。日志里没有 `BAD` 和 dmesg 可疑行。
7. 漏页测试完整跑满 30 轮，每轮漏页 0，恢复后状态都是 T，哈希相等，写入进程没有卡死。

**压测。**

8. 四组的 `RESULT` 都以 `DONE rc=0` 开头，`console.txt` 存在且非空。取不到串口日志时结果无效，缺证据不算通过。
9. 没有 `*-create-fail-*` 文件；dmesg 新命中为 0；worker 的错误只有 `ENOENT`、`ESTALE`（删文件密集组按 [18](18-testing-and-debug-kernel.md) §5.4 的允许范围）；心跳未过期；gsd 侧只有 `create:resource_exhausted`。

**性能。**

10. 性能 JSON 是 `schema_version=2`、`measurement_mode=per-tier-size`，恢复路径是 `modified-child-to-full-baseline`，六档负载和恢复目标与脚本一致，三轮 18 次恢复全部校验通过。
11. 这轮运行期间机器上没有别的沙箱或模板构建。有就记下来，并在本章注明。
12. 记录的是服务端 `total_ms`。墙钟单列，不混用。
13. 三轮结果来自同一次部署、同一个模板。换过任何一样要重建模板，并作为新的一轮。

**填写。**

14. 每个数字都能追到本轮结果目录里的文件。摘要放进 `criu-rollback/validation/` 下带平台与日期的新目录，不覆盖 920B 的目录，也不和 920B 的结果混合统计。
15. 950 与 920B 的数字并列阅读时不相减、不算倍数。920B 的耗时不作通过阈值。与沙箱级的对比只写在 [24](24-vs-sandbox-level.md)。
16. 跳过的项写明原因，失败的项保留完整目录并写明状态。填完后把开头的状态说明、[23](23-gaps-and-boundaries.md) 的缺口表和各章里"950 尚未运行"的说法一起更新。

---

## 本章要点

- 950 的进程级尚未运行，本章所有单元格都是 `待填`。
- 结果由 `ops/950.sh acceptance`、`stress` 和回切冒烟产生，每张表的来源文件都已写明。
- 表的结构与第 19、20、21 章相同；V12 首次部署跳过，没有对应的表。
- 950 的第一轮结果是它自己的基线，920B 的耗时不是通过阈值，两者不相减。
- 填数之前按 §8 逐条核对，这里是之后填写 950 结果的唯一位置。
