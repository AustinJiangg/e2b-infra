# 19 · 920B 功能结果

## 本章目标

读完本章，你应该能：

1. 说出当前代码在新 920B 上做过哪几轮验证，每一轮的代码、模板和范围是什么；
2. 查到功能验证各脚本的结果：哪些项通过、哪些项跳过、跳过的原因是什么；
3. 把结果文件里出现的构建产物名对应到运行时布局里的路径；
4. 说出 2026-10-08、10-09、11-06 三轮之间哪些部件变了、哪些没变，以及哪一轮的结果能说明什么。

测试分几层、每个脚本测什么、判据是什么，在 [18](18-testing-and-debug-kernel.md)。本章只给新 920B 上的功能结果。性能在 [20](20-920b-performance.md)，压测在 [21](21-920b-stress.md)，950 在 [22](22-950-results.md)，没有测到的部分在 [23](23-gaps-and-boundaries.md)。
本章和这几章合起来是全书判定性数字的唯一位置：别的章为了讲清原理或过程也会举数，但都当场标了平台、日期与口径，不能与这里的数相减。每个数字的来源文件在 [23](23-gaps-and-boundaries.md) 的数据出处。

---

## 结论先行

新 920B 上做过三轮功能验证，全部通过，没有发现问题：

| 轮次 | 代码与模板 | `verify_fixes.py` | `verify_multitree.py` | `verify_clear_refs_race.py` | `probe_whiteout_bug.py` |
|---|---|---|---|---|---|
| 2026-10-08 确认验证 | `259f1a25c`，模板 `base-gsd9` | PASS 26 / FAIL 0 / SKIP 2 | V0～V12 共 13 项全部 PASS | 30 轮，有漏页的轮数 0 | 七个沙箱都不崩 |
| 2026-10-09 落位后复核 | 同一批文件，按运行时布局落位，模板 `base-gsd` | PASS 26 / FAIL 0 / SKIP 2 | V0～V12 共 13 项全部 PASS | 未跑 | 未跑 |
| 2026-11-06 全流程复测 | gsd 与内核不变，orchestrator 重编，SDK `dcca61266`，模板 `base-gsd` | PASS 26 / FAIL 0 / SKIP 2 | V0～V11 共 12 项全部 PASS，V12 跳过 | 30 轮，有漏页的轮数 0 | 七个沙箱都不崩 |

三轮里 `verify_gsd.py` 都判定根盘恒定。

**这些结果的边界。** 它们是一台机器、一种 guest 规格（2 vCPU / 2048 MB）、几种人造负载下的结果。恒为 SKIP 的两项是 G-19、G-36（[18](18-testing-and-debug-kernel.md) §3.2）。950 的进程级尚未运行（[22](22-950-results.md)）。
2026-10-08 验证时用的是开发期的文件路径，默认路径由 2026-10-09 的落位复核确认（§1.3）。

---

## 1. 条件标签

### 1.1 2026-10-08 确认验证

本章和 [20](20-920b-performance.md)、[21](21-920b-stress.md) 里标为"2026-10-08 确认验证"的数字都在下面这组条件下测得。结果目录是 `gsd-release-20261008`。

| 项 | 值 |
|---|---|
| 机器 | 新 920B（192.168.45.47） |
| 时间 | 2026-10-08 21:03～21:27（机器本地时钟）；功能与压测部分 21:13:12～21:26:24，各步返回码均为 0，没有看门狗触发 |
| 代码 | `deltabox-gsd@259f1a25c`（完整提交号 `259f1a25c531ca75b82955087121a15713cc6b5b`），工作区干净 |
| gsd | 静态编译（`CGO_ENABLED=0`），sha256 `a38b0db90c309abe4a7e1b4ad6b9180c2f257cbe204c4a982190b2b4b04894bb`；guest 内 `/usr/bin/gsd` 的 sha256 与之相同，`gsd -commit` 输出 `259f1a25c` |
| orchestrator | `CGO_ENABLED=1`，sha256 `818fa667640e0cbf9cbd064532d285f874def101ed9e4661b0d2c6136c4a837e` |
| 构建 | go1.25.4，离线、`-mod=readonly`；gsd 的 `go vet` 无输出，带竞态检测的单测四个包全部通过；orchestrator 的 `internal/template/...`、`internal/cfg/...` 单测全部通过 |
| guest 内核 | 6.1.158，`uname -r` 为 `6.1.158+`，`/proc/version` 里的构建标识为 `#4 SMP Thu Oct  8 20:06:40 CST 2026`，sha256 `b81306e76540a0e69a4cc1b9e943137deb317054e74809894754e8cd5e75d66c`；config 的 sha256 `49d2f45f3a83d6ccb4529dbb2d4920018f604c15817bae9420d040fe86c0c52b` |
| 内核源码 | 上游 tag `microvm-kernel-6.1.158-15.288.amzn2023` 依次打三个补丁；树的哈希 `744cbce67cef413e4838035c8a2788f97e5e9d3a` |
| 补丁 | `0001-virtio_balloon-Support-wait-on-ACK-for-hinting.patch` sha256 `a817adea1cfbcb4984a73ddacaf947cf7323edf66130384d092b4b49eda01b51`；`0002-overlayfs-hot-switch-checkpoint.patch` sha256 `5d364ce12b50a81ae126e4e6f0d1b473276cde845bddbc2fa6c513af42d0c669`；`0003-overlayfs-hot-switch-fixes.patch` sha256 `4dfc12fd12471fc458e97b21f2715c1d3c3978fb7e658fa5b51fde4267138e50` |
| guest 规格 | 2 vCPU / 2048 MB（guest 内 `MemTotal` 2035188 kB） |
| guest 镜像 | ubuntu 22.04 加源码编译的 CRIU 4.2.1（`ubuntu:22.04-custom-criu421`） |
| THP | `always madvise [never]`，开机即如此 |
| 模板 | 别名 `base-gsd9`，ID `afw242v9c4zvx0m3w6r1`；不使用缓存；建模板耗时 17.2 s |
| SDK | 代码仓 `py-sdk`（经 `PYTHONPATH` 指定），运行在新 920B 上装好 SDK 依赖的 conda 环境里（环境名见 [30](30-acceptance-runbook.md)） |

关于内核有一点要说清：这一轮**没有重新编译内核**，用的是当天稍早编好并验证过的那一个二进制。它与仓库里三个补丁的对应关系是用另一种办法确认的——
从上游 tag 建一个临时工作区，依次应用仓库里的 0001、0002、0003，得到的树哈希与编出这个内核的源码分支的树哈希相同，两者的差异为 0 字节。所以"仓库里的补丁就是验证过的内核的源码"成立，但"用仓库里的补丁重新编出同一个二进制"没有做。

### 1.2 验证时的产物名与运行时布局

确认验证是在开发期的目录结构下做的，结果文件里的路径和模板名都是当时的构建产物名。2026-10-09 定下了运行时布局（[27](27-deployment.md)），本书其余各章一律按运行时布局写。两者的对应如下：

| 结果文件里的名字 | 是什么 | 运行时布局里的对应 |
|---|---|---|
| `gsd-stack/bin/gsd-259f1a25c` | 注入 guest 的 gsd 二进制 | `/fc-gsd/gsd`（`HOST_GSD_PATH` 的默认值） |
| `gsd-stack/bin/orchestrator-gsd-259f1a25c` | 带 gsd 注入的 orchestrator | `/fc-gsd/orchestrator` |
| `gsd-stack/fc-kernels-v6/vmlinux-6.1.158/vmlinux.bin` | 打了三个补丁的 guest 内核（发布版 config） | `/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin` |
| `gsd-stack/fc-kernels-v6/config` | 上面这个内核的 config | 不在运行时布局里，属于构建材料 |
| `base-gsd9` | 用上面三样建出的模板 | `base-gsd` |
| `base-gsd8` | 前一轮的模板：同一个内核，gsd 与 orchestrator 是前一个提交号 `c0630349c`。确认验证只在 V12 里把它作对照模板 | 无对应，对照用 |
| `gsd-stack/fc-kernels-dbg6/…`、`base-gsd-dbg8` | 同一份内核源码按调试 config 编出的内核，以及用它建的 4096 MB 模板（[21](21-920b-stress.md) §3 的补充证据） | 无对应，调试用 |
| `HOST_GSD_PATH=…/gsd-259f1a25c`、`HOST_KERNELS_DIR=…/fc-kernels-v6` | 验证时 orchestrator 的两个环境变量：两个路径都是显式传入的 | 运行时布局下前者不用设（取默认值），后者在两套栈共用的机器上设为 `/fc-gsd/kernels` |

表里 `v6`、`dbg6`、`base-gsd8` 这类带序号的名字是开发期的构建代号，完整对照在附录 [附录 A](A-glossary-and-code-map.md)。

由这张表可以直接读出一个限制：**2026-10-08 验证时 gsd 的路径是显式传给 orchestrator 的，没有走默认值。** 当时这台机器上还没有 `/fc-gsd` 目录。默认值这条路径在第二天落位后另做了确认，见 §1.3。
按运行时布局部署 `259f1a25c` 时，三个文件的 sha256 应与 §1.1 里的值逐一相同，部署时要核对（[27](27-deployment.md)、[30](30-acceptance-runbook.md)）。

### 1.3 落位后的复核（2026-10-09）

2026-10-09 把三个文件原样放到运行时布局的位置（内容没有变，sha256 与 §1.1 逐一相同），切栈、在默认路径下重建模板并跑了一遍冒烟。它回答的是"换了位置、不再显式传 gsd 路径之后，建出来的东西是不是同一个"，
不替换 2026-10-08 的确认验证结果。平台同为新 920B，代码同为 `259f1a25c`。性能部分的读数见 [20](20-920b-performance.md) §2。

| 项 | 结果 |
|---|---|
| orchestrator 的启动环境 | 可执行文件 `/fc-gsd/orchestrator`，`HOST_KERNELS_DIR=/fc-gsd/kernels`；进程环境里**没有** `HOST_GSD_PATH` 这个键，走默认值 `/fc-gsd/gsd` |
| 建模板 | `base-gsd`，ID `ckn4sa18clgpy6leyufg`，2 vCPU / 2048 MB，不用缓存，15.2 s |
| guest 内核对 | `/usr/bin/gsd` 的 sha256 为 `a38b0db9…`，与 `/fc-gsd/gsd` 相同；`gsd -commit` 输出 `259f1a25c`；`gsd.service` 为 active；内核构建标识与 §1.1 相同；THP 为 `[never]` |
| `verify_fixes.py` | PASS 26 / FAIL 0 / SKIP 2，与 §2.1 的逐项状态相同 |
| `verify_multitree.py` | V0～V12 共 13 项全部 PASS |
| `verify_gsd.py`（8 次 restore） | 全部 ok，根盘恒定在 272 MB |
| V12 逐轮交错（新模板对前一天的模板，8 轮） | 八条对照全部在 ±3 ms 内 |

这一轮 `verify_multitree.py` 也记到一次"慢调用"（checkpoint 的客户端墙钟 1640 ms，gsd 自报 99.9 ms），与 §2.2 的那一次同类，不计入判定（[16](16-known-limits.md) 的 U-01）。

至此"不设 `HOST_GSD_PATH`、按默认路径注入"有了实测结果。`HOST_GSD_PATH=off` 的关闭态仍然没有真实建过模板，留在 [23](23-gaps-and-boundaries.md) 的缺口表里。

### 1.4 2026-11-06 全流程复测

为适配新部署重编了 orchestrator，只改网卡命名。代码等于 `bc3cce913` 的 orchestrator（`259f1a25c` 之后 `packages/` 下只有 `slot.go` 的 `VethName()` 这一处改动）。gsd、guest 内核、容器镜像不变，SDK 用 `dcca61266`。全程用 950 离线包的入口 `ops/950.sh` 完成切栈、建模板、验收、压测和回切。

| 项 | 值 |
|---|---|
| 机器 | 新 920B（192.168.45.47），宿主内核 `6.6.0-159.4.3.154.oe2403sp4.aarch64` |
| 时间 | 2026-11-06。结果目录按机器本地时钟命名为 `20261107-054143-acceptance`、`20261107-054517-stress`，guest 日志时间戳为 `2026-11-06T21:40Z` 起 |
| 部署 | e2b-infra `504f02c` 的 RPM 部署 |
| gsd | sha256 `a38b0db9…`，与 §1.1 相同；`gsd -commit` 输出 `259f1a25c` |
| orchestrator | sha256 `151308ba59fa5785686b51b0940919889b641e5a5fbbd1beb2b9da4272715832`；启动环境没有 `HOST_GSD_PATH`，`HOST_KERNELS_DIR=/fc-gsd/kernels` |
| guest 内核 | sha256 `b81306e7…`，构建标识 `#4 SMP Thu Oct  8 20:06:40 CST 2026`，与 §1.1 相同 |
| guest 规格 | 2 vCPU / 2048 MB（`MemTotal` 2035188 kB），CRIU 4.2.1，THP `always madvise [never]`，`rcu_expedited` 为 1，`/` 为 755 root:root，`/tmp` 为 1777 且位于 overlay |
| 模板 | 别名 `base-gsd`，ID `9x7mcy64fvfm1xjw6114`，2 vCPU / 2048 MB，不用缓存，9.1 s 建成 |
| SDK | `dcca61266`，装在独立 conda 环境里，Python 3.12.14 |

---

## 2. 功能结果

### 2.1 `verify_fixes.py`

2026-10-08 确认验证：28 项，PASS 26 / FAIL 0 / SKIP 2，逐项状态与前一个提交号 `c0630349c`（同一个内核）那一轮相同，没有 `BAD` 行。SKIP 的是 G-19 与 G-36。几项带有值得记下的数：

| 项 | 实测 |
|---|---|
| G-06 零散写的增量 | 256 MiB 进程的全量镜像 272420864 字节；零散写 96 个字节后的增量镜像 1105920 字节（1.05 MiB） |
| G-11 循环后的根盘 | 40 轮"写 1 MiB → checkpoint → restore 基点 → delete"后，`df /` 增加 19376 KB，其中 `/var/log`（journal）19344 KB，扣除后 32 KB；层目录数不变，`.trash` 为空 |
| G-29 并发 | 4 个并发 checkpoint 全部成功、ID 各不相同 |

G-08（层栈上限）、G-21（容量预检）、G-20（鉴权开销）和 PERF（进程扫描）的读数在 [20](20-920b-performance.md) §5。

### 2.2 `verify_multitree.py`

2026-10-08 确认验证：V0～V12 共 13 项全部 PASS，没有 `BAD` 行。V5（多树下的增量大小）的实测：两棵 64 MiB 的树全量 133.2 MiB；不改内存再做一次 checkpoint，0.38 MiB；起第三棵树后再做一次，66.9 MiB。V12 的数字在 [20](20-920b-performance.md) §3。

这一轮记到一次"慢调用"：V3 里的第一次 checkpoint，客户端墙钟 1861 ms，而 gsd 自报的 `total_ms` 是 34.9 ms。请求成功，判定不受影响，gsd 侧耗时正常，多出来的时间在请求到达 gsd 之前。
这与 [16](16-known-limits.md) 的 U-01（新沙箱上的首个请求偶发长等待）是同一类现象，原因未查。

### 2.3 `verify_clear_refs_race.py`

2026-10-08 确认验证，参数 `--rounds 30 --pages 4096 --rate 100000`（16 MiB 的缓冲区，每秒写 10 万页）。

| 指标 | 值 |
|---|---|
| 有漏页的轮数 | 0 / 30，漏页合计 0 |
| 恢复后的进程状态 | 30 轮全部为 T |
| 停住时与恢复后的缓冲区哈希 | 30 轮全部相等 |
| 写入进程被判定卡死的轮数 | 0 |
| checkpoint A 从 dump 完到放行的停住时长（`stopped_ms`） | p50 0.53 ms，最小 0.49，最大 4.26 |
| checkpoint A 的 `criu_dump_ms` | p50 20.37 ms，最小 14.75，最大 26.50 |
| checkpoint A 的 `overlay_sink_ms` | p50 0.02 ms |
| checkpoint B（增量）的页镜像大小 | p50 800 KiB，最小 8，最大 2148（脚本输出；汇总文件按另一种中位数算法记为 804 KiB） |
| checkpoint A 的页镜像大小 | p50 19300 KiB |

**反向对照。** 这个脚本的判据可信，前提是它在有问题的版本上确实报得出漏页。2026-09-23（新 920B，结果目录 `gsd-new920b-20260923`，当时的内核与模板都早于 §1.1，只作对照）用同一个脚本、默认参数做过：
把 guest 里的 gsd 临时换成修改前的二进制（`criu dump` 用 `--leave-running`），先后三次运行、扣除写入进程卡死的轮次后共 50 轮，其中 **31 轮有漏页**（三次分别是 9 / 15、7 / 10、15 / 25，漏页合计 4283 页，单轮最多 717 页）；
同一天换成修改后的二进制跑 30 轮，有漏页的轮数为 0。别的章提到"修改前 31 轮漏页"时指的就是这一组。

"停住时长 0.53 ms"是这项修复的代价：为了关上漏页的窗口，进程在 dump 之后要多停这么久，等 overlay 切完层、soft-dirty 位清完（[11](11-checkpoint.md)）。

### 2.4 `probe_whiteout_bug.py`

2026-10-08 确认验证：用例 `bug`、`ctl`、`nodel`、`masked` 各一次，`trap` 三次，共七个沙箱。全部 `crashed=False`；四个 whiteout 用例的第二次删除都成功，dmesg 里没有 whiteout 相关的行；
三次 `trap` 各新建并访问 6000 个目录和文件，错误计数都为空。每个沙箱开机时既存的 whiteout 数都是 0（upper 与各层里都没有），也就是说模板里不存在曾经掩盖过这个问题的那个偶然条件（[09](09-overlay-cache-and-reclaim.md) 的"属于 work 目录的缓存"一节）。

### 2.5 2026-11-06 全流程复测

离线包入口 `ops/950.sh acceptance` 按 guest 自检、`verify_gsd.py`、`verify_fixes.py`、`verify_multitree.py --skip V12`、`verify_clear_refs_race.py`、性能脚本三轮的顺序串行执行，判定 PASS；`stress` 的结果在 [21](21-920b-stress.md) §2。

| 项 | 结果 |
|---|---|
| guest 自检 | PASS：提交号、gsd sha256、内核构建标识、THP、`rcu_expedited`、服务状态、`/health`、CRIU 版本、根目录与 `/tmp` 权限、vCPU 全部符合预期 |
| `verify_gsd.py`（8 次 restore） | 全部 ok，根盘恒定在 272 MB；分段见 [20](20-920b-performance.md) §2 |
| `verify_fixes.py` | PASS 26 / FAIL 0 / SKIP 2，SKIP 仍是 G-19、G-36 |
| `verify_multitree.py --skip V12` | V0～V11 共 12 项全部 PASS，慢调用 0 次 |
| `verify_clear_refs_race.py` | 30 轮，漏页合计 0；恢复后 30 轮全部为 T，哈希全部相等，写入进程卡死 0 轮 |
| G-06 零散写的增量 | 256 MiB 进程的全量镜像 272416768 字节，增量镜像 1097728 字节（1.05 MiB） |
| G-11 循环后的根盘 | 40 轮后 `df /` 增加 19376 KB，其中 `/var/log`（journal）19344 KB，扣除后 32 KB |
| G-29 并发 | 4 个并发 checkpoint 全部成功 |
| V5 多树下的增量大小 | 全量 133.3 MiB；不改内存 0.37 MiB；起第三棵树后 66.9 MiB |
| 竞态：checkpoint A 的 `stopped_ms` | p50 0.52 ms，最小 0.47，最大 7.14 |
| 竞态：checkpoint A 的 `criu_dump_ms` | p50 19.63 ms，最小 13.26，最大 43.86 |
| 竞态：checkpoint B（增量）的页镜像大小 | p50 816 KiB，最小 8，最大 3416 |
| 回切沙箱级 | `sandbox_smoke.py` PASS 5 / FAIL 0 / SKIP 0 |

V12 需要一份仅版本不同的进程级模板作对照，这一轮按离线包的设计跳过。

回切之后又切回 gsd 复用 `base-gsd`：切换返回后 1 秒的第一次 `verify_gsd.py` 报 `500: Failed to place sandbox`，约 1.5 分钟后重跑通过，8 次 restore 根盘恒定。这一条记录在离线包说明里，重跑的日志没有归档。

---

## 本章要点

- 新 920B 上 2026-10-08、10-09、11-06 三轮功能验证全部通过：`verify_fixes.py` 三轮都是 PASS 26 / FAIL 0 / SKIP 2，`verify_multitree.py` 没有失败项，漏页测试 0 / 30。
- 漏页测试有反向对照：修改前的二进制 50 轮里 31 轮有漏页，修改后 30 轮为 0，说明这个脚本在有问题的版本上确实报得出来。
- gsd 与 guest 内核的 sha256 三轮不变。2026-11-06 只换了 orchestrator（只改网卡命名）和 SDK，这一轮跳过了 V12。
- 结果文件里的路径与模板名是验证时的构建产物名，与运行时布局的对应在 §1.2。2026-10-08 验证时 gsd 的路径是显式传入的，默认路径在 2026-10-09 确认（§1.3）。
- 内核没有从仓库补丁重新编译，"仓库里的补丁就是验证过的内核的源码"靠树哈希相等确认（§1.1）。
- 恒为 SKIP 的 G-19、G-36 和其余没有测到的部分列在 [23](23-gaps-and-boundaries.md)。
