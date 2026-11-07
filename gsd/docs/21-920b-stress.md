# 21 · 920B 压测结果

## 本章目标

读完本章，你应该能：

1. 说出新 920B 上做过哪几轮压测，每一轮跑了哪几组、在哪份代码和内核上；
2. 读懂压测结果里的每一项：判定、操作次数、`resource_exhausted`、worker 的 errno、`mnt_cache` 的净增；
3. 区分确认验证、全流程复测和补充证据三类结果各自能证明什么；
4. 知道还有哪一条压测记录没有查清。

判据、看门狗和各组的含义在 [18](18-testing-and-debug-kernel.md) §5。测试条件在 [19](19-920b-functional.md) §1。回收专项（`probe_leak.py`、`probe_umount.py`）在 [20](20-920b-performance.md) §5.1。

---

## 结论先行

- 当前内核上的压测全部通过，dmesg 与串口关键词零命中，gsd 侧只有预期的 `resource_exhausted`，worker 的错误只有切换语义允许的 errno。
- 2026-10-08 确认验证（`259f1a25c`）跑了四组；2026-11-06 全流程复测（gsd 与内核不变，orchestrator 重编）重跑了同样四组和探针七例。
- 其余四组加 enospc、10 个种子的对照、调试内核和其余探针只在前一个提交号 `c0630349c` 上跑过。Go 代码相同、内核是同一个二进制，它们是补充证据。
- 最长的单组是 300 轮，没有小时级的连续运行（[23](23-gaps-and-boundaries.md)）。950 尚未运行压测（[22](22-950-results.md)）。

---

## 1. 2026-10-08 确认验证的四组

四组都带看门狗，没有触发；`all` 与 `noworkers` 两组并行跑，多棵树与删文件密集两组并行跑。四组都没有留下"预期之外的 checkpoint 失败"的取证文件。

### 1.1 并发文件操作与对照

`stress_overlay_ckpt.py`，各 300 轮。

| 项 | all（6 个 worker，十种操作全开） | noworkers（无 worker） |
|---|---|---|
| 判定 | 通过 | 通过 |
| dmesg / 串口关键词新命中 | 0 / 0 | 0 / 0 |
| checkpoint / restore / delete 成功次数 | 77 / 153 / 15 | 75 / 155 / 13 |
| gsd 侧错误 | 只有 `create:resource_exhausted` 55 次 | 只有 `create:resource_exhausted` 57 次 |
| worker 操作总数 | 3355517 | 0 |
| worker 错误 | `ENOENT` 199748，`ESTALE` 118011，其他 0 | — |
| worker 心跳过期 | 无；6 个 worker 结束前都在运行 | — |
| 结束时 `/health` | ok | ok |
| `mnt_cache` 净增 | +42（282 → 324） | −34（332 → 298） |

`create:resource_exhausted` 是快照存储放不下时的预期保护（[15](15-state-concurrency-capacity.md)），不是故障；这两组的 300 轮里各有五十多次，说明压测确实一直压在容量边界上。
`mnt_cache` 的净增在几十个的量级且有正有负，不随轮数增长；修改前它每次切换都涨（[20](20-920b-performance.md) §5.1）。

这两组的结果文件里还有脚本侧的墙钟中位数（all 组 checkpoint 84 ms、restore 66 ms）。那不是 gsd 的耗时，只反映当时有多少 worker 在抢 CPU，不作判据，也不要引用。
同理，all 组的 worker 操作总数比 §3.1 那一轮多约四成，是因为这一轮同时只有两组在跑、那一轮是四路并行，机器负载不同。

### 1.2 多棵树

`stress_multitree.py`，200 轮，三棵树、每棵缓冲区 4 MiB。

| 项 | 值 |
|---|---|
| 判定 | PASS |
| checkpoint / restore / delete 成功次数 | 92 / 100 / 9 |
| 宿主侧校验 | 192 次，0 失败 |
| 进程自检留下的 `.bad` 文件 | 0 个 |
| 预期之外的 gsd 错误 | 0 |
| dmesg 关键词命中 | 0 |
| checkpoint 的 p50 | `total_ms` 44.67，`criu_dump_ms` 41.96，`stopped_ms` 32.04 |
| restore 的 p50 | `total_ms` 27.13，`criu_restore_ms` 22.63，`kill_ms` 2.74 |
| 结束状态 | `/health` ok；`state.json` 版本 2，留有 88 个 checkpoint 条目、7 层 |
| `mnt_cache` 净增 | +42 |

这里的 checkpoint 耗时比 [20](20-920b-performance.md) §3 的三棵树增量 checkpoint 大，是因为这个负载每 0.1 秒把每棵树的缓冲区整个写一遍，每次 checkpoint 都有实打实的脏页要写。它是另一种口径，同样不与第 20 章的表比较。

### 1.3 删文件密集

`stress_delete_dense.py`，200 轮，4 个 worker。

| 项 | 值 |
|---|---|
| 判定 | PASS |
| checkpoint / restore / delete 成功次数 | 92 / 100 / 9 |
| 清单逐条校验 | 192 次，0 失败 |
| 可校验的那棵树上做的变更 | 删文件 4544，整目录删除 1093，重建目录 1132，建文件 6236；异常 0 次 |
| worker 操作总数 | 697777 |
| worker 错误 | `ENOENT` 316165，`ENOTEMPTY` 9，`ESTALE` 4，`EEXIST` 2；预期之外的 errno 0 |
| 被 dump 与 restore 的用户进程 | 无错误 |
| 结束时各层里的 whiteout / opaque 目录 | 3288 / 2821 |
| dmesg 关键词命中与 whiteout 相关提示 | 0 / 0 |

最后两行合起来才有意义：确实产生了几千个 whiteout 与 opaque 目录，而内核没有任何抱怨。

---

## 2. 2026-11-06 全流程复测的四组

离线包入口 `ops/950.sh stress` 先跑 `probe_whiteout_bug.py` 的七个用例，再顺序跑四组，每组都经看门狗（停滞阈值 150 秒），判定 PASS。条件见 [19](19-920b-functional.md) §1.4。

探针七例（`bug`、`ctl`、`nodel`、`masked` 各一次，`trap` 三次）全部 `crashed=False`，dmesg 为空，开机时既存的 whiteout 数都是 0。

| 项 | all（6 个 worker） | noworkers | 多棵树 | 删文件密集 |
|---|---|---|---|---|
| 轮数 | 300 | 300 | 200 | 200 |
| 判定 | 通过 | 通过 | PASS | PASS |
| 看门狗 | `DONE rc=0` | `DONE rc=0` | `DONE rc=0` | `DONE rc=0` |
| dmesg 新命中 | 0 | 0 | 0 | 0 |
| checkpoint / restore / delete 成功次数 | 77 / 153 / 15 | 77 / 153 / 15 | 92 / 100 / 9 | 92 / 100 / 9 |
| gsd 侧错误 | 只有 `create:resource_exhausted` 55 次 | 只有 `create:resource_exhausted` 55 次 | 0 | 0 |
| worker 操作总数 | 2012471 | 0 | — | 643443 |
| worker 错误 | `ENOENT` 119232，`ESTALE` 74659 | — | — | `ENOENT` 290890，`ESTALE` 17，`ENOTEMPTY` 11，`EEXIST` 1；预期之外的 errno 0 |
| 校验 | — | — | 宿主侧 192 次，0 失败 | 清单 192 次，0 失败 |
| `mnt_cache` 起 → 止 | 298 → 324（+26） | 298 → 329（+31） | 298 → 336（+38） | 298 → 289（−9） |
| 结束时 `/health` | ok | ok | ok | ok |

其余读数：

- 多棵树结束时 `state.json` 版本 2，留有 88 个 checkpoint 条目、7 层，与 §1.2 相同。性能 p50：checkpoint `total_ms` 51.72，`criu_dump_ms` 48.28，`stopped_ms` 36.19；restore `total_ms` 32.08，`criu_restore_ms` 27.01，`kill_ms` 3.22。这组 p50 与 §1.2 不同。两轮的运行方式不同（这一轮四组顺序跑，§1 两两并行），也没有逐轮交错，不作版本比较。
- 删文件密集的宿主侧变更次数与 §1.3 相同（删文件 4544，整目录删除 1093，重建目录 1132，建文件 6236），结束时 whiteout / opaque 目录 3238 / 2865。
- noworkers 组的操作次数（77 / 153 / 15，`resource_exhausted` 55）与 2026-10-08 的 75 / 155 / 13、57 不同，四组里只有这一组的 gsd 操作次数在两轮之间不同。这个差别没有单独查，它不影响判定。

---

## 3. 补充证据：前一个提交号上的八组、种子对照与调试内核

2026-10-08 确认验证的范围不含完整的八组压测、多种子对照、其余探针和调试内核。这些在当天稍早的一轮里做过，那一轮的代码是**前一个提交号 `c0630349c`**：Go 代码与 `259f1a25c` 相同，内核就是 [19](19-920b-functional.md) §1.1 的同一个二进制，区别只是仓库里补丁文件的组织方式。
它们作为补充证据列在这里；严格地说，它们证明的是"这个内核加上这份 Go 代码"，而不是"这个提交号"。

### 3.1 发布版内核，八组各 300 轮

2026-10-08，结果目录 `gsd-fix6-20261008`；四路并行。

| 组 | 判定 | dmesg / 串口 | checkpoint / restore / delete | worker 操作 | `ENOENT` | `ESTALE` | 其他 errno | `mnt_cache` 净增 |
|---|---|---|---|---|---|---|---|---|
| all | 通过 | 0 / 0 | 77 / 153 / 15 | 2370871 | 143279 | 88137 | 0 | +50 |
| readonly | 通过 | 0 / 0 | 77 / 151 / 18 | 3817809 | 319460 | 3451 | 0 | +11 |
| wr3 | 通过 | 0 / 0 | 77 / 153 / 15 | 1185589 | 522 | 51634 | 0 | +33 |
| rename | 通过 | 0 / 0 | 77 / 153 / 15 | 811240 | 719 | 30258 | 0 | +9 |
| append | 通过 | 0 / 0 | 77 / 153 / 15 | 5427939 | 0 | 4 | 0 | +46 |
| reltouch | 通过 | 0 / 0 | 77 / 153 / 15 | 4776714 | 0 | 53512 | 0 | +26 |
| noworkers | 通过 | 0 / 0 | 75 / 155 / 13 | 0 | 0 | 0 | 0 | +21 |
| enospc | 通过 | 0 / 0 | 磁盘写满的 7 档；删掉填充后 checkpoint 与 restore 恢复正常、内容正确 | — | — | — | — | — |

§1.1 的 all 与 noworkers 两组的 checkpoint / restore / delete 次数与这张表里的逐个相同：脚本用固定种子，同样的代码走的是同一条操作序列。

### 3.2 多种子对照

all 与 noworkers 两种分组各用 10 个不同的种子跑 300 轮，共 20 组：20 组全部通过，worker 的错误只有 `ENOENT` 与 `ESTALE`，dmesg 与串口零命中，gsd 侧只有 `resource_exhausted`。
这组对照的意义在于参照：同样 20 组，在最后两个内核修复之前的那一版内核上只有 10 组干净（noworkers 9/10、all 1/10），其余各组出现 `ELOOP`、`ENXIO`、`ENOTDIR`。

### 3.3 调试内核

KASAN 加 lockdep，4096 MB 的调试模板；同一份内核源码。

| 项 | 判定 | dmesg / 串口 | checkpoint / restore / delete | worker 操作 | 其他 errno | 结束时 `debug_locks` |
|---|---|---|---|---|---|---|
| `probe_whiteout_bug.py` 五种用例（`trap` 三次） | 都不崩；串口输出里 KASAN / BUG / WARNING 为 0 行 | — | — | — | — | — |
| all，300 轮 | 通过 | 0 / 0 | 77 / 154 / 14 | 882427 | 0 | 1 |
| wr3，300 轮 | 通过 | 0 / 0 | 77 / 154 / 14 | 637769 | 0 | 1 |
| rename，300 轮 | 通过 | 0 / 0 | 77 / 154 / 14 | 373076 | 0 | 1 |
| noworkers，300 轮 | 通过 | 0 / 0 | 79 / 153 / 14 | 0 | 0 | 1 |
| 多棵树，200 轮 | 通过 | 0 / 0 | 92 / 100 / 9；校验 192 次 0 失败 | — | — | 1 |
| 删文件密集，200 轮 | 通过 | 0 / 0 | 92 / 100 / 9；清单校验 192 次 0 失败 | 439863 | 0 | 1 |

调试内核上没有跑功能验证（`verify_fixes.py`、`verify_multitree.py`）。

### 3.4 其余探针

同一轮、发布版内核：`probe_estale.py` 的 195 条命令结果、`probe_estale_file.py`、`probe_mmap_rollback.py` 的 98 个检查点、`probe_fd_rollback.py` 的输出，都与再前一轮逐项相同；
`probe_leak.py` 100 轮与 `probe_umount.py` 20 轮见 [20](20-920b-performance.md) §5.1。

### 3.5 一处没有查清的记录

这一轮的多种子对照里，在一个尚未定稿的候选内核上，有一组在第 174、177 轮各出现一次 checkpoint 返回 `internal`（`criu dump failed: exit status 1`），其余操作、worker、dmesg 全部正常，随后的 checkpoint 成功。
当时脚本只留了报错的前 200 个字符，看不到 CRIU 的出错行。之后同一个种子重放 5 次、正式内核上的各组压测、调试内核上的各组以及 §1、§2 的四组都没有再出现。没有证据把它归到内核改动，也不能排除；列在 [16](16-known-limits.md)，编号 U-02。

---

## 本章要点

- 2026-10-08 确认验证和 2026-11-06 全流程复测各跑了四组压测，全部通过，dmesg 与串口关键词零命中。
- `create:resource_exhausted` 是快照存储放不下时的预期保护，四组里出现它说明压测确实压在容量边界上，不是故障。
- worker 的错误只有切换语义允许的 errno；删文件密集组确实产生了几千个 whiteout 与 opaque 目录，内核没有报错。
- `mnt_cache` 的净增在几十个的量级且有正有负，不随轮数增长。
- 八组压测、多种子对照、调试内核和其余探针是在前一个提交号上跑的：Go 代码相同、内核是同一个二进制，作为补充证据引用时要带上这句限定。
- 压测脚本侧的墙钟中位数不是 gsd 的耗时，不作判据。
- 未查清的记录有一处：候选内核上两次 checkpoint 返回 `internal`（U-02），之后没有再出现。
