# 22 · 950 结果

> **状态：两轮已填（2026-11-17）。** 第一轮是 `criu-rollback/deploy/950.sh all`，13 步全部 PASS（§0～§7），各表由同一轮结果目录经 `bash criu-rollback/deploy/950.sh report handbook` 取数填写，“串口关键词命中”一行是事后用新版取数脚本补取的（正则与 `analyze_stress.py` 相同）。第二轮是同一天 `test950.sh standard` 里的进程级验收与压测，全部 PASS（§9）。原始文件留在 950 上。这是填写 950 结果的唯一位置。

## 本章目标

读完本章，你应该能：

1. 知道 950 的进程级验证由哪几步组成，每一步的结果落在哪个文件；
2. 按本章的表收集和填写 950 的部署条件、功能验收、探针、四组压测和分档性能；
3. 填数之前核对条件，判断一轮结果是否有效。

本章不给通过阈值。950 的第一轮结果是它自己的基线，920B 的耗时不能作为 950 的通过标准。四组压测和功能验收的判据在 [18](18-testing-and-debug-kernel.md)，表的结构与 [19](19-920b-functional.md)、[20](20-920b-performance.md) §1、[21](21-920b-stress.md) §2 相同，以便并排阅读。同一流程在新 920B 上跑出的结果就是那几节。

---

## 0. 第一轮概况

| 项 | 值 |
|---|---|
| 平台 | 950（141.61.11.24），宿主内核 `6.6.0-159.4.3.154.oe2403sp4.aarch64` |
| 结果目录 | `criu-rollback/deploy/results/20261117-142121-all/`（14:21:21 开始，导入镜像一步停过三次，15:21:53 续跑到完成） |
| 仓库提交 | e2b-infra `57b333e` |
| `all` 逐步用时 | 校验、展开、conda 与 SDK、凭据、导入镜像、预检与基线、落位合计 7 秒；切到进程级 59 秒；建模板 10 秒；必跑验收 189 秒；探针与四组压测 167 秒；切回沙箱级 59 秒；沙箱级冒烟 54 秒 |
| 判定 | 13 步全部 PASS；切换前后宿主上非 e2b 的网卡、路由、策略路由、iptables 规则无变化 |

导入镜像一步停下的三次都与 950 的 docker 配置有关，处理后不影响本轮结果：

1. 950 的 docker 使用 containerd 镜像存储，导入时读归档里的 `index.json` 及其指向的 OCI 清单。当时仓库里的归档缺这份清单，导入出 0B 的空镜像。补上后归档同时满足两种存储（`criu-rollback/deploy/README.md` 第 14 节）。
2. docker daemon 配了代理，`harbor` 不在 `NO_PROXY` 里，推送 `harbor:443` 返回 504。现在推送走 Harbor 的 http 端口 `SERVER_IP:2900`，模板仍引用 `harbor:443/...`（[27](27-deployment.md) §7）。
3. 重启 docker 后 Harbor 的 2900 端口没有恢复，重新执行沙箱级部署（`build.sh -d/-i/-s`）后正常。

---

## 1. 结果从哪里来

入口是仓库里的 `criu-rollback/deploy/950.sh`，在仓库根目录执行 `bash criu-rollback/deploy/950.sh all`，结束时把逐步结论和各项摘要打印到终端，细节用 `950.sh report acceptance|stress|smoke` 打印（不打包）；本章各表要的数用 `950.sh report handbook` 一次打印，每段标明来源文件。结果目录 `criu-rollback/deploy/results/<时间>-all/` 里与验证相关的三部分：

| `all` 里的步骤 | 内容 | 结果目录下 |
|---|---|---|
| 验收（也可单独 `950.sh acceptance`） | guest 自检、`verify_gsd.py`、`verify_fixes.py`、`verify_multitree.py --skip V12`、`verify_clear_refs_race.py`、`demo_checkpoint_perf.py` 三轮 | `acceptance/` |
| 压测（也可单独 `950.sh stress`） | `probe_whiteout_bug.py` 七个用例，然后 all、noworkers、multitree、deldense 四组 | `stress/` |
| 切回沙箱级并冒烟（也可单独 `950.sh sandbox`、`950.sh smoke`） | 切回后用原模板试建沙箱，再跑沙箱级冒烟 | `sandbox-smoke/` |

`all` 之后的分档测试由仓库根的 `test950.sh` 调度（`test950/README.md`），结果用 `bash test950.sh report` 打印，结果目录是 `test950/results/<时间>-<档>/`，进程级各步的目录名为 `criu-<命令>`，内部结构与下面相同。extended 档另有 `criu-stress-ext/`（八组压测、多种子与兼容矩阵），long 档另有 `criu-soak/`（按时长的长跑与宿主资源前后对比）。

包里另有 `all.log`（每步进度与结论）、`summary.json`（每步状态与用时）和 `netcheck.txt`（切换前后宿主上非 e2b 网络配置的比对）。验收和压测两个目录里都有 `summary.json`（每一步的命令和状态、判定 `verdict`、机器名、起止时间、Python 与 SDK 路径）和 `deliverables.sha256`（三个运行文件的 sha256）。`acceptance` 的 `summary.json` 还有 `slow_calls` 和 `skipped`。下面各表的"来源"列写的都是相对这两个目录的文件名。

分档性能的三轮结果用 `criu-rollback/scripts/acceptance/summarize_checkpoint_perf.py` 校验并汇总，命令见 [验证摘要](../validation/README.md)。950 的摘要另建带平台与日期的目录，放在 `criu-rollback/validation/` 下，不覆盖 920B 的目录。

结果里有几项不进入这些文件，要在运行时另外记下：机器名、起止时间、宿主内核版本、未跑的项目和回切冒烟结果。回传时不带 `criu-rollback/scripts/.env`、状态目录、Nomad job JSON 和登录配置。

---

## 2. 部署条件

| 项 | 值 | 来源 |
|---|---|---|
| 平台 | 950（141.61.11.24） | 运行时记录 |
| 日期与起止时间 | 2026-11-17；验收 15:23:09.68～15:26:17.79（`summary.json`），压测 15:26:17～15:29:04（`all.log`） | `summary.json` 的 `started`、`finished`；核对机器时钟 |
| 宿主内核 | `6.6.0-159.4.3.154.oe2403sp4.aarch64` | 宿主上 `uname -r` |
| 部署版本 | e2b-infra `57b333e` 的 `criu-rollback/deploy/`（gsd 与内核为 `259f1a25c`，orchestrator 为 `259f1a25c-e2bv`） | 运行时记录 |
| 提交：`gsd -commit` | `259f1a25c` | `guest-check.log` 的 `commit` |
| 提交：SDK | `2.19.0+gsd.dcca61266` | wheel 版本号 |
| gsd sha256（宿主 / guest） | 宿主 `421597e59cdc89f6538fa5b6ee8238903269f2de230f401e2ee72f82e5c7c0fe` / guest 相同 | `deliverables.sha256`；`guest-check.log` 的 `gsd_sha256` |
| orchestrator sha256 | `063e580e38becbbb3246e8822ccff5ac65c4f0bff266d1badefa7adff32dd870` | `deliverables.sha256` |
| guest 内核 sha256 | `b81306e76540a0e69a4cc1b9e943137deb317054e74809894754e8cd5e75d66c` | `deliverables.sha256` |
| guest 内核版本串 | `Linux version 6.1.158+ … #4 SMP Thu Oct  8 20:06:40 CST 2026` | `guest-check.log` 的 `kernel` |
| CRIU 版本 | 4.2.1 | `guest-check.log` 的 `criu` |
| THP | `always madvise [never]` | `guest-check.log` 的 `thp` |
| `rcu_expedited` | 1 | `guest-check.log` 的 `rcu` |
| guest 规格（vCPU / 内存） | 2 / `MemTotal: 2035188 kB` | `guest-check.log` 的 `vcpus`、`memory` |
| `/`、`/tmp` 的权限与 `/tmp` 所在文件系统 | `/` 0755 root:root，`/tmp` 1777 root:root，overlay | `guest-check.log` 的 `root`、`tmp`、`tmp_fs` |
| 模板别名与 ID | `base-criu` / `00gzphcgb2l55nmokvs8` | `summary.json` 的 `template`；`guest-check.log` 首行 |
| 建模板耗时 | 8.1 s（首个沙箱 94 ms） | `build_template.py` 末行 |
| Python 与 SDK 路径 | Python 3.14.10；`/root/miniconda3/envs/jll-e2b-criu/lib/python3.14/site-packages/e2b/__init__.py` | `summary.json` 的 `python`、`sdk` |

---

## 3. 功能验收

### 3.1 各步判定

| 步骤 | 判定 | 来源 |
|---|---|---|
| guest 自检 | PASS | `guest-check.log` |
| `verify_gsd.py`（8 次 restore） | PASS | `verify_gsd.log` |
| `verify_fixes.py` | PASS | `verify_fixes.log`、`verify_fixes.json` |
| `verify_multitree.py --skip V12` | PASS | `verify_multitree.log`、`verify_multitree.json` |
| `verify_clear_refs_race.py` | PASS | `clear_refs_race.log`、`clear_refs_race.jsonl` |
| 性能三轮 | PASS（三轮各自判定） | `perf-run1.json`、`perf-run2.json`、`perf-run3.json` |
| `acceptance` 总判定 | PASS（用时 189 秒） | `summary.json` 的 `verdict` |

### 3.2 计数与读数

| 项 | 值 | 来源 |
|---|---|---|
| `verify_fixes.py` PASS / FAIL / SKIP | 26 / 0 / 2 | `verify_fixes.json` 的 `status` 计数 |
| `verify_fixes.py` SKIP 的项 | G-19、G-36 | 同上 |
| `verify_multitree.py` PASS 项数 | 12 / 12；日志无 `BAD` 行 | `verify_multitree.json` 的 `items` |
| 慢调用次数 | 0 | `verify_multitree.json` 的 `slow_calls` |
| 漏页：有漏页的轮数 / 总轮数，漏页合计 | 0 / 30，0 | `clear_refs_race.log` 的汇总段 |
| 漏页：恢复后状态、哈希相等的轮数、写入进程卡死的轮数 | 全部 T，30，0 | `clear_refs_race.jsonl` 的 `restored_state`、`sha_eq`、`wedged` |
| 漏页：checkpoint A 的 `stopped_ms` p50 / 最小 / 最大 | 0.47 / 0.40 / 5.56 | `clear_refs_race.log` 的 `A_stopped_ms` |
| 漏页：checkpoint A 的 `criu_dump_ms` p50 / 最小 / 最大 | 14.99 / 10.93 / 30.70 | `clear_refs_race.log` 的 `A_dump_ms` |
| 漏页：checkpoint A 的 `overlay_sink_ms` p50 | 0.02 | `clear_refs_race.log` 的 `A_sink_ms` |
| 漏页：checkpoint B 的页镜像大小 p50 / 最小 / 最大 | 908 / 8 / 2552 KiB | `clear_refs_race.log` 的 `cpB 增量 pages` |
| G-06 全量镜像 / 增量镜像 | 272420864 字节 / 1097728 字节（1.05 MiB） | `verify_fixes.json` 的 G-06 |
| G-08 成功次数、被拒时的选项串长度 | 190 次；第 191 次 4105 字节 > 4096 被拒（最后一次成功时 3871） | `verify_fixes.json` 的 G-08 |
| G-11 `df /` 增量、journal 增量、扣除后净增 | 19296 KB、19264 KB、32 KB（40 轮） | `verify_fixes.json` 的 G-11 |
| G-20 secure 与非 secure 的鉴权差 | 带鉴权的 `ListCheckpoints` 比 `/health` 慢：secure 0.54 ms，非 secure 0.74 ms | `verify_fixes.json` 的 G-20 |
| G-21 被拒时的估算、快照存储剩余、`MemAvailable` | 580.4 MiB、1082.5 MiB、457.6 MiB | `verify_fixes.json` 的 G-21 |
| PERF `find_pids_ms` 中位数：checkpoint / restore | 0.50 / 0.47 | `verify_fixes.json` 的 PERF |
| V5 全量 / 不改内存 / 起第三棵树后的镜像大小 | 133.2 / 0.37 / 66.9 MiB | `verify_multitree.json` 的 V5 |

`verify_multitree.py --skip V12` 不产生 V12 的两部分结果。V12 需要一份仅版本不同的进程级模板作对照，首次部署没有这样的模板，所以第 20 章 §3 的逐轮交错和一、二、三棵树分段在本章没有对应的表。

---

## 4. 探针

`probe_whiteout_bug.py` 七个用例，来源是 `whiteout.json`。

| 用例 | `crashed` | 开机时的 whiteout 数 | dmesg |
|---|---|---|---|
| `bug` | false | upper=0 layers=0 | 空 |
| `ctl` | false | upper=0 layers=0 | 空 |
| `nodel` | false | upper=0 layers=0 | 空 |
| `masked` | false | upper=0 layers=0 | 空 |
| `trap` 第 1 次 | false | upper=0 layers=0 | 空 |
| `trap` 第 2 次 | false | upper=0 layers=0 | 空 |
| `trap` 第 3 次 | false | upper=0 layers=0 | 空 |

---

## 5. 四组压测

all 与 noworkers 的来源是各子目录的 `stress-summary.json`，多棵树是 `multitree/multitree-summary.json`，删文件密集是 `deldense/deldense-summary.json`；看门狗结果是各子目录的 `RESULT`，串口证据是 `console.txt`。

| 项 | all（6 个 worker） | noworkers | 多棵树 | 删文件密集 |
|---|---|---|---|---|
| 轮数 | 300 | 300 | 200 | 200 |
| 判定 | PASS | PASS | PASS | PASS |
| 看门狗 | `DONE rc=0` | `DONE rc=0` | `DONE rc=0` | `DONE rc=0` |
| dmesg 新命中 | 0 | 0 | 0 | 0 |
| 串口关键词命中 | 0（`console.txt` 56798 字节） | 0（55262 字节） | 0（140638 字节） | 0（205470 字节） |
| checkpoint / restore / delete 成功次数 | 77 / 153 / 15 | 75 / 155 / 13 | 92 / 100 / 9 | 92 / 100 / 9 |
| gsd 侧错误 | `create:resource_exhausted` 55 | `create:resource_exhausted` 57 | 无 | 无 |
| worker 操作总数 | 1794503 | 0 | — | 713015 |
| worker 错误（按 errno） | `ENOENT` 101890，`ESTALE` 71639 | — | — | `ENOENT` 322957，`ENOTEMPTY` 17，`ESTALE` 5，`EEXIST` 4；预期之外的 errno 0 |
| 校验次数与失败数 | 不校验 | 不校验 | 192 / 0 | 192 / 0 |
| `mnt_cache` 起 → 止 | 334 → 324（−10） | 334 → 335（+1） | 301 → 357（+56） | 301 → 340（+39） |
| 结束时 `/health` | ok | ok | ok | ok |

| 项 | 值 | 来源 |
|---|---|---|
| 多棵树结束时的 `state.json`（版本、checkpoint 条目数、层数） | 2、88、7 | `multitree-summary.json` 的 `state` |
| 多棵树 checkpoint 的 p50：`total_ms` / `criu_dump_ms` / `stopped_ms` | 38.36 / 35.76 / 26.52 | `multitree.log` 的"性能 p50"行 |
| 多棵树 restore 的 p50：`total_ms` / `criu_restore_ms` / `kill_ms` | 26.72 / 22.24 / 3.40 | 同上 |
| 删文件密集的宿主侧变更：删文件 / 整目录删除 / 重建目录 / 建文件 | 4544 / 1093 / 1132 / 6236 | `deldense-summary.json` 的 `stats.mutate` |
| 删文件密集结束时的 whiteout / opaque 目录 | 3181 / 2832 | `deldense-summary.json` 的 `marks_end` |
| `stress` 总判定 | PASS（含探针，用时 167 秒） | `summary.json` 的 `verdict` |

---

## 6. 分档性能

结构与 [20](20-920b-performance.md) §1 相同。服务端 `total_ms` 三轮中位数和墙钟中位数，单位 ms，来源是汇总出的 `summary.json`。

| 总改动量 MiB | 内存 / 文件 MiB | 全量基点 | 增量 checkpoint | restore | 全量基点墙钟 | 增量 checkpoint 墙钟 | restore 墙钟 |
|---|---|---:|---:|---:|---:|---:|---:|
| 0 | 0 / 0 | 18.7 | 8.2 | 8.5 | 22.2 | 10.0 | 11.1 |
| 16 | 12 / 4 | 23.9 | 14.6 | 14.7 | 27.6 | 16.6 | 16.8 |
| 32 | 24 / 8 | 28.1 | 20.8 | 21.0 | 31.5 | 22.7 | 22.8 |
| 64 | 48 / 16 | 38.6 | 30.9 | 26.2 | 43.3 | 33.2 | 28.4 |
| 128 | 96 / 32 | 67.4 | 52.0 | 51.1 | 71.8 | 54.4 | 53.2 |
| 256 | 192 / 64 | 96.1 | 88.7 | 83.6 | 99.9 | 90.8 | 86.3 |

128 档第 2 轮的全量基点和 restore（78.8、68.8 ms）比另两轮高出 10～25 ms，同一轮的增量 checkpoint 不高，原因没有查。这一轮没有记录机器上是否有别的负载（§8 第 11 条）。

三轮的服务端逐轮样本：

| 总改动量 MiB | 全量基点 轮 1 | 轮 2 | 轮 3 | 增量 checkpoint 轮 1 | 轮 2 | 轮 3 | restore 轮 1 | 轮 2 | 轮 3 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 18.7 | 17.4 | 19.7 | 7.3 | 8.2 | 8.4 | 8.5 | 8.2 | 8.7 |
| 16 | 22.8 | 31.4 | 23.9 | 14.6 | 14.1 | 17.9 | 12.7 | 18.4 | 14.7 |
| 32 | 28.1 | 27.6 | 33.9 | 20.8 | 20.7 | 24.9 | 21.0 | 19.1 | 21.6 |
| 64 | 38.5 | 45.6 | 38.6 | 30.8 | 33.9 | 30.9 | 25.7 | 27.9 | 26.2 |
| 128 | 57.4 | 78.8 | 67.4 | 46.6 | 52.0 | 62.2 | 43.7 | 68.8 | 51.1 |
| 256 | 85.7 | 97.3 | 96.1 | 84.4 | 88.7 | 92.0 | 79.1 | 84.4 | 83.6 |

各档实测 RSS：

| 总改动量 MiB | 轮 1 MiB | 轮 2 MiB | 轮 3 MiB |
|---|---:|---:|---:|
| 0 | 11.6 | 11.6 | 11.6 |
| 16 | 24.8 | 24.8 | 24.8 |
| 32 | 36.7 | 36.8 | 36.8 |
| 64 | 60.8 | 60.8 | 60.8 |
| 128 | 108.8 | 108.8 | 108.8 |
| 256 | 204.8 | 204.8 | 204.8 |

| 项 | 值 | 来源 |
|---|---|---|
| 恢复校验通过次数 | 18 / 18 | 汇总 `summary.json` 的 `restore_checks_passed` |
| SDK 与 journal 分段最大差 | 0.0 ms | 汇总 `summary.json` 的 `max_sdk_journal_diff_ms` |

`verify_gsd.py` 单棵 192 MiB、全脏、连续 restore 8 次的分段，结构与 [20](20-920b-performance.md) §2 相同，来源是 `verify_gsd.log`：

| 分段 | p50 | 最小 | 最大 |
|---|---:|---:|---:|
| `total_ms` | 68.9 | 67.0 | 76.7 |
| `criu_restore_ms` | 58.1 | 55.6 | 65.8 |
| `kill_ms` | 10.3 | 9.2 | 10.7 |
| `find_pids_ms` | 0.6 | 0.5 | 0.6 |
| `overlay_restore_ms` | 0.4 | 0.3 | 0.4 |
| `precheck_ms` | 0.1 | 0.1 | 0.1 |

| 项 | 值 |
|---|---|
| 根盘已用量起点与每次增量 | 266 MB，8 次都是 +0 |

---

## 7. 回切沙箱级

| 项 | 值 | 来源 |
|---|---|---|
| `sandbox` 切换 | 成功，59 秒；切回后用原模板 `base` 建沙箱 90 ms | 入口输出 |
| `status` 显示的二进制与 `/health` | `/usr/bin/template-manager`（`a5d03902…`），`/health` 200 | 入口输出 |
| 沙箱级冒烟 PASS / FAIL / SKIP | 5 / 0 / 0（宿主预检 19 项 PASS、SDK 覆盖层自检、能力文件、Firecracker sha256、59 项正确性） | `sandbox-smoke/` 下的 `SUMMARY.md` |

---

## 8. 填数时要核对的条件

下面每一条都要在填表之前确认。有一条不符，先查原因，不把这轮结果填进来。

**产物与模板。**

1. `deliverables.sha256` 里 gsd、orchestrator、guest 内核三个 sha256 与 `criu-rollback/deploy/manifest.json` 里的 `sha256` 一致（是去掉调试段后的版本），acceptance 和 stress 两个目录也互相一致。
2. `base-criu` 是在 950 的进程级栈下新建的，不是沙箱级模板，也不是从别的机器带来的；建模板时没有加 `--use-cache`。
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

## 9. 第二轮：test950 standard

同一天 17:01 起，`test950.sh standard` 在沙箱级各步之后切到进程级，又跑了一遍验收（`criu-acceptance`）和探针加四组压测（`criu-stress`），再切回沙箱级冒烟。结果目录 `test950/results/20261117-161710-standard/`，取数用与 `950.sh report handbook` 相同的脚本。这一轮前后机器由我们独占（§8 第 11 条满足），并有沙箱级的分档在 15 分钟前跑完，用来做 [24](24-vs-sandbox-level.md) §5 的同机对比。

### 9.1 条件与判定

| 项 | 值 |
|---|---|
| 仓库提交 | e2b-infra `c27b6f8` |
| 被测文件 | gsd `421597e5…`、orchestrator `063e580e…`、guest 内核 `b81306e7…`，与第一轮相同，acceptance 与 stress 两个目录一致 |
| guest 自检 | `gsd -commit` 为 `259f1a25c`，与第一轮相同 |
| 验收 | 17:03:01–17:06:10，3 分 09 秒，**PASS**：guest-check、verify_gsd、verify_fixes、verify_multitree、clear_refs_race、perf-run1..3 全部 PASS；`slow_calls` 为空 |
| 压测 | 3 分 52 秒，**PASS**：whiteout、all、noworkers、multitree、deldense 全部 PASS。开跑前 e2b netns 有 1539 个，超过 1500，`test950.sh` 先自动清理到 294（2 分 10 秒） |
| 回切冒烟 | `sandbox/smoke-after` PASS 5 / FAIL 0 / SKIP 0 |

### 9.2 与第一轮对照

| 项 | 第一轮（`950.sh all`） | 第二轮（standard） |
|---|---|---|
| `verify_fixes.py` | PASS 26 / SKIP 2（G-19、G-36） | 相同 |
| `verify_multitree.py --skip V12` | 12 / 12 PASS | 相同，`BAD` 行 0 |
| 漏页测试 | 30 轮，漏页 0 | 30 轮，漏页 0；`A_dump_ms` p50 16.28 ms；恢复后状态都是 T，哈希都相等，没有卡死 |
| `verify_gsd.py` restore `total_ms` p50（最小～最大） | 68.9（67.0～76.7） | 69.7（69.0～78.0）；`criuRestoreMs` 58.8、`killMs` 10.2；根盘 266 MB，8 次都是 +0 |
| 七个 whiteout 探针 | 全部未崩溃 | 全部未崩溃，起点 whiteout 0，dmesg 空 |
| all（6 worker，300 轮） | create 77 / restore 153 / delete 15，p50 65 / 60 ms，`resource_exhausted` 55 | create 77 / restore 153 / delete 15，p50 75.5 / 76.4 ms，`resource_exhausted` 55 |
| noworkers（300 轮） | create 75 / restore 155 / delete 13，p50 33 / 33 ms，`resource_exhausted` 57 | create 75 / restore 155 / delete 13，p50 30.4 / 32.6 ms，`resource_exhausted` 57 |
| multitree / deldense（各 200 轮） | 校验 192 / 0 失败 | 校验 192 / 0 失败；deldense 的文件操作 unlink 4544、rmtree 1093、mkdir 1132、create 6236，失败 0 |
| 四组串口关键词命中 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| 分档恢复校验 | 18 / 18，SDK 与 journal 最大差 0.0 ms | 相同 |

两轮的各组操作计数完全相同，这是压测脚本按固定种子决定操作序列的结果。all 组的 p50 第二轮高约 10～16 ms，noworkers 组没有升高；每组只有一次运行，不判断这是不是稳定差异。

### 9.3 分档性能

服务端 `total_ms` 三轮中位数和墙钟中位数，单位 ms：

| 总改动量 MiB | 内存 / 文件 MiB | 全量基点 | 增量 checkpoint | restore | 全量基点墙钟 | 增量 checkpoint 墙钟 | restore 墙钟 |
|---|---|---:|---:|---:|---:|---:|---:|
| 0 | 0 / 0 | 17.6 | 7.6 | 7.7 | 21.3 | 9.4 | 9.7 |
| 16 | 12 / 4 | 23.4 | 12.7 | 12.8 | 26.8 | 14.9 | 15.1 |
| 32 | 24 / 8 | 29.2 | 20.7 | 21.8 | 33.2 | 23.0 | 24.1 |
| 64 | 48 / 16 | 39.7 | 32.0 | 29.9 | 43.2 | 34.1 | 31.9 |
| 128 | 96 / 32 | 56.2 | 49.4 | 45.4 | 59.8 | 51.7 | 47.5 |
| 256 | 192 / 64 | 96.5 | 88.8 | 79.7 | 100.1 | 91.0 | 82.4 |

三轮的服务端逐轮样本：

| 总改动量 MiB | 全量基点 轮 1 | 轮 2 | 轮 3 | 增量 checkpoint 轮 1 | 轮 2 | 轮 3 | restore 轮 1 | 轮 2 | 轮 3 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 16.9 | 17.6 | 36.2 | 7.3 | 7.9 | 7.6 | 8.0 | 7.5 | 7.7 |
| 16 | 23.4 | 22.3 | 27.8 | 11.8 | 12.7 | 13.1 | 11.5 | 12.8 | 15.6 |
| 32 | 29.2 | 28.8 | 31.1 | 20.5 | 20.7 | 22.4 | 22.1 | 19.1 | 21.8 |
| 64 | 39.7 | 44.2 | 37.5 | 32.0 | 41.7 | 30.5 | 29.9 | 30.4 | 26.1 |
| 128 | 56.2 | 58.4 | 54.2 | 52.3 | 47.9 | 49.4 | 46.1 | 44.7 | 45.4 |
| 256 | 96.5 | 99.2 | 95.5 | 88.8 | 89.4 | 86.6 | 80.6 | 79.7 | 78.8 |

各档 RSS 与第一轮相同，最多差 0.1 MiB（11.6、24.8、36.8、60.8、108.8、204.8 MiB）。第一轮 128 档第 2 轮偏高的现象（§6）这一轮没有出现，三个样本在 54.2～58.4 ms（全量基点）和 44.7～46.1 ms（restore）之间。这一轮 0 档第 3 轮的全量基点 36.2 ms 比另两轮高约一倍，是每档每轮新沙箱的第一次 checkpoint，增量和 restore 不受影响，原因没有查。

两轮的增量 checkpoint 与 restore 中位数相差 0.1～5.7 ms，差最大的是 128 档的 restore（51.1 对 45.4 ms）；全量基点在 128 档相差 11.2 ms（67.4 对 56.2 ms）。这两处正是第一轮那个偏高样本所在的档。

---

## 本章要点

- 950 上第一轮 `950.sh all` 已跑完（2026-11-17），13 步全部 PASS；§8 的核对条件除第 11 条（运行期间机器上有没有别的负载，没有记录）外都满足。
- 同一天 `test950.sh standard` 的第二轮在独占机器上跑，验收与四组压测全部 PASS，功能计数、压测计数与第一轮相同；分档的增量 checkpoint 与 restore 中位数相差不超过 5.7 ms（§9）。
- 分档性能三轮的服务端中位数从 0 档的 8.2 / 8.5 ms（增量 checkpoint / restore）到 256 档的 88.7 / 83.6 ms；128 档第 2 轮明显偏高（全量基点 78.8、restore 68.8 ms），三轮中位数不受它影响。
- 结果由 `criu-rollback/deploy/950.sh all` 里的验收、压测和回切冒烟产生，每张表的来源文件都已写明。
- 表的结构与第 19、20、21 章相同；V12 首次部署跳过，没有对应的表。
- 950 的第一轮结果是它自己的基线，920B 的耗时不是通过阈值，两者不相减。
- 填数之前按 §8 逐条核对，这里是之后填写 950 结果的唯一位置。
