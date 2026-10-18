# 20 · 实测结果与缺口

## 本章目标

读完本章，你应该能：

1. 说出当前代码在新 920B 上验证到了哪一步：哪些功能项通过、压测跑了哪几组、各自的判定是什么；
2. 查到一次 restore、一次 checkpoint 在三种口径下各是多少毫秒，并知道这三种口径为什么不能互相比较；
3. 把结果文件里出现的构建产物名对应到运行时布局里的路径；
4. 知道哪些东西**没有**测：950、注入开关的关闭态、若干失败分支、更大的规格与更长的时间；
5. 对本章任何一个数字，都能在出处表里找到它来自哪个结果文件。

上一章（[19](19-testing-and-debug-kernel.md)）讲了测试分几层、每个脚本测什么、判据是什么。本章给出按那套办法测出来的结果。
**全书的判定性数字只在这一章**：别的章为了讲清原理或过程也会举数，但都当场标了平台、日期与口径，并且不能与这里的数相减。

本章完整功能与压测数据以 2026-10-08 的确认验证为准：代码 `deltabox-gsd@259f1a25c`，新 920B（192.168.45.47），结果目录 `gsd-release-20261008`。2026-10-16 新版分档性能和与 950 沙箱级的对照单列在 §3.4，不替换历史验收结果。
少数专项数字（泄漏、可写打开文件的代价、八组压测、调试内核）取自同一天或 2026-09-30 较早的几轮，每处都单独标明是哪一轮、哪个代码版本。950 的 gsd 未部署、未测；§3.4 的 950 列仅引用沙箱级数据。

---

## 结论先行

**功能与稳定性。** 新 920B 上，`259f1a25c` 的全部确认性验证通过，没有发现问题：

- `verify_fixes.py`：PASS 26 / FAIL 0 / SKIP 2（恒为 SKIP 的两项见 [19](19-testing-and-debug-kernel.md)）；
- `verify_multitree.py`：V0～V12 共 13 项全部 PASS；
- `verify_clear_refs_race.py`：30 轮，有漏页的轮数 0；
- `probe_whiteout_bug.py`：五种用例（其中 `trap` 连跑三次）都不崩；
- 压测四组：并发文件操作全开 300 轮、无 worker 对照 300 轮、多棵树 200 轮、删文件密集 200 轮，全部通过，dmesg 与串口关键词零命中；
- `verify_gsd.py`：连续 8 次 restore 根盘占用恒定。

**性能。** 2 vCPU / 2048 MB 的 guest、THP 为 never、单棵 192 MiB 进程树：`verify_gsd.py` 连续 8 次 restore 的 `total_ms` p50 为 **83.3 ms**，
其中 CRIU 恢复 71.0 ms、杀进程 11.1 ms、进程扫描 0.7 ms、恢复前检查 0.1 ms。一次 restore 的时间几乎全在 CRIU 恢复与杀进程上，overlay 切层不到 1 ms。
多棵树时耗时随树数近似线性增长：每棵 64 MiB 时，一、二、三棵树的 restore `total_ms` p50 为 32.9 / 60.4 / 93.3 ms，不改内存的增量 checkpoint 为 11.2 / 21.8 / 33.5 ms（§3）。

**这些数字的边界。** 它们是一台机器、一种 guest 规格、几种人造负载下的结果。950 的 gsd 未部署、未测；§3.4 引用的 950 数字仅来自沙箱级。更大的 guest、三棵以上的树、小时级的连续运行都没有测过；
2026-10-08 验证时用的是开发期的文件路径；2026-10-09 按运行时布局落位后，在默认路径下重建模板并复核过一遍，结果单列在 §1.2，不改变本章的判定值。

---

## 1. 条件标签

本章所有 2026-10-08 确认验证的数字都在下面这组条件下测得。

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
| SDK | 代码仓 `py-sdk`（经 `PYTHONPATH` 指定），运行在新 920B 上装好 SDK 依赖的 conda 环境里（环境名见 [26](26-acceptance-runbook.md)） |

关于内核有一点要说清：这一轮**没有重新编译内核**，用的是当天稍早编好并验证过的那一个二进制。它与仓库里三个补丁的对应关系是用另一种办法确认的——
从上游 tag 建一个临时工作区，依次应用仓库里的 0001、0002、0003，得到的树哈希与编出这个内核的源码分支的树哈希相同，两者的差异为 0 字节。所以"仓库里的补丁就是验证过的内核的源码"成立，但"用仓库里的补丁重新编出同一个二进制"没有做。

### 1.1 验证时的产物名与运行时布局

确认验证是在开发期的目录结构下做的，结果文件里的路径和模板名都是当时的构建产物名。2026-10-09 定下了运行时布局（[23](23-deployment.md)），本书其余各章一律按运行时布局写。两者的对应如下：

| 结果文件里的名字 | 是什么 | 运行时布局里的对应 |
|---|---|---|
| `gsd-stack/bin/gsd-259f1a25c` | 注入 guest 的 gsd 二进制 | `/fc-gsd/gsd`（`HOST_GSD_PATH` 的默认值） |
| `gsd-stack/bin/orchestrator-gsd-259f1a25c` | 带 gsd 注入的 orchestrator | `/fc-gsd/orchestrator` |
| `gsd-stack/fc-kernels-v6/vmlinux-6.1.158/vmlinux.bin` | 打了三个补丁的 guest 内核（发布版 config） | `/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin` |
| `gsd-stack/fc-kernels-v6/config` | 上面这个内核的 config | 不在运行时布局里，属于构建材料 |
| `base-gsd9` | 用上面三样建出的模板 | `base-gsd` |
| `base-gsd8` | 上一轮的模板：同一个内核，gsd 与 orchestrator 是前一个提交号 `c0630349c`。本轮只在 V12 里作对照模板 | 无对应，对照用 |
| `gsd-stack/fc-kernels-dbg6/…`、`base-gsd-dbg8` | 同一份内核源码按调试 config 编出的内核，以及用它建的 4096 MB 模板（§4.2 的补充证据） | 无对应，调试用 |
| `HOST_GSD_PATH=…/gsd-259f1a25c`、`HOST_KERNELS_DIR=…/fc-kernels-v6` | 验证时 orchestrator 的两个环境变量：两个路径都是显式传入的 | 运行时布局下前者不用设（取默认值），后者在两套栈共用的机器上设为 `/fc-gsd/kernels` |

表里 `v6`、`dbg6`、`base-gsd8` 这类带序号的名字是开发期的构建代号，完整对照在附录 [A](A-glossary-and-code-map.md)。

由这张表可以直接读出一个限制：**2026-10-08 验证时 gsd 的路径是显式传给 orchestrator 的，没有走默认值。** 当时这台机器上还没有 `/fc-gsd` 目录。默认值这条路径在第二天落位后另做了现场确认，见 §1.2。
按运行时布局部署之后，三个文件的 sha256 应与上面条件标签里的值逐一相同，部署时要核对（[23](23-deployment.md)、[26](26-acceptance-runbook.md)）。

### 1.2 落位后的复核（2026-10-09）

2026-10-09 把三个文件原样放到运行时布局的位置（内容没有变，sha256 与条件标签逐一相同），切栈、在默认路径下重建模板并跑了一遍冒烟。它回答的是"换了位置、不再显式传 gsd 路径之后，建出来的东西是不是同一个"，
**不替换本章的判定值**：除另行标明日期的 §3.4 外，§2 以后的确认验证数字仍来自 2026-10-08。平台同为新 920B，代码同为 `259f1a25c`。

| 项 | 结果 |
|---|---|
| orchestrator 的启动环境 | 可执行文件 `/fc-gsd/orchestrator`，`HOST_KERNELS_DIR=/fc-gsd/kernels`；进程环境里**没有** `HOST_GSD_PATH` 这个键，走默认值 `/fc-gsd/gsd` |
| 建模板 | `base-gsd`，ID `ckn4sa18clgpy6leyufg`，2 vCPU / 2048 MB，不用缓存，15.2 s |
| guest 内核对 | `/usr/bin/gsd` 的 sha256 为 `a38b0db9…`，与 `/fc-gsd/gsd` 相同；`gsd -commit` 输出 `259f1a25c`；`gsd.service` 为 active；内核构建标识与条件标签相同；THP 为 `[never]` |
| `verify_fixes.py` | PASS 26 / FAIL 0 / SKIP 2，与 §2.1 相同 |
| `verify_multitree.py` | V0～V12 共 13 项全部 PASS |
| `verify_gsd.py`（8 次 restore） | `total_ms` p50 86.8 ms（最小 83.5，最大 96.1；`criu_restore_ms` 74.1，`kill_ms` 11.2，`find_pids_ms` 0.7，`precheck_ms` 0.1），根盘恒定在 272 MB |
| V12 逐轮交错（新模板对前一天的模板，8 轮） | restore `total_ms` p50 83.12 对 83.27，差 −0.15 ms；`criu_restore_ms` 70.25 对 70.27；create `total_ms` 15.83 对 16.00；八条对照全部在 ±3 ms 内 |
| V12 一、二、三棵树 | restore `total_ms` p50 33.8 / 61.9 / 94.4 |

两点说明。第一，`verify_gsd.py` 的 86.8 ms 比 §3.1 的 83.3 ms 高 3.5 ms。两者是不同模板上各测一次，这个差不能直接解读；同一轮里新模板与前一天模板的逐轮交错对照只差 0.15 ms，说明两个模板没有差别。没有再加样本去区分那 3.5 ms 是不是单次测量的波动。
第二，这一轮 `verify_multitree.py` 也记到一次"慢调用"（create 的客户端墙钟 1640 ms，gsd 自报 99.9 ms），与 §2.2 的那一次同类，不计入判定（[15](15-known-limits.md) 的 U-01）。

至此"不设 `HOST_GSD_PATH`、按默认路径注入"有了现场结果。`HOST_GSD_PATH=off` 的关闭态仍然没有真实建过模板，留在 §7 的缺口表里。

---

## 2. 功能结果

### 2.1 `verify_fixes.py`

28 项，PASS 26 / FAIL 0 / SKIP 2，逐项状态与上一轮（前一个提交号、同一个内核）相同，没有 `BAD` 行。SKIP 的是 G-19 与 G-36。几项带有值得记下的数：

| 项 | 实测 |
|---|---|
| G-06 零散写的增量 | 256 MiB 进程的全量镜像 272420864 字节；零散写 96 个字节后的增量镜像 1105920 字节（1.05 MiB） |
| G-08 层栈上限 | 连续成功 create 190 次，第 191 次被拒：`resource_exhausted`，`layer stack too deep: 192 lower layers, ioctl options 4105 bytes > kernel limit 4096`；最后一次成功的 ioctl 有 190 层、`lowerdir` 串长 3871 字节。之后 restore 最深与最浅的 checkpoint 都正确，回到浅层栈后再 create 成功 |
| G-11 循环后的根盘 | 40 轮"写 1 MiB → create → restore 基点 → delete"后，`df /` 增加 19376 KB，其中 `/var/log`（journal）19344 KB，扣除后 32 KB；层目录数不变，`.trash` 为空 |
| G-20 鉴权的开销 | guest 内各 40 次请求的中位数：secure 沙箱 `/health` 0.21 ms、带鉴权的 `ListCheckpoints` 0.83 ms，差 0.62 ms；非 secure 沙箱 0.19 / 0.76 ms，差 0.57 ms |
| G-21 容量预检 | 480 MiB 进程：前两次 create 成功（镜像 483 MiB、481 MiB），第三次被拒：估算 580.4 MiB（1.2 × 483.6 MiB），快照存储剩余 1082.5 MiB，`MemAvailable` 456.8 MiB——存储放得下，内存不够。进程照常运行，dmesg 无 OOM |
| G-29 并发 | 4 个并发 create 全部成功、ID 各不相同 |
| PERF 进程扫描 | `find_pids_ms` 中位数：create 0.65 ms，restore 0.61 ms；对照，guest 里调一次 `systemctl show` 查 MainPID 的中位数是 3.94 ms |

G-08 的"190 层"与 G-21 的拒绝条件是两条容量边界的实测，机制分别在 [13](13-state-concurrency-capacity.md)。

### 2.2 `verify_multitree.py`

V0～V12 共 13 项全部 PASS，没有 `BAD` 行。V5（多树下的增量大小）的实测：两棵 64 MiB 的树全量 133.2 MiB；不改内存再建一个，0.38 MiB；起第三棵树后再建一个，66.9 MiB。V12 的数字在 §3.2。

这一轮记到一次"慢调用"：V3 里的第一次 create，客户端墙钟 1861 ms，而 gsd 自报的 `total_ms` 是 34.9 ms。请求成功，判定不受影响，gsd 侧耗时正常，多出来的时间在请求到达 gsd 之前。
这与 [15](15-known-limits.md) 的 U-01（新沙箱上的首个请求偶发长等待）是同一类现象，原因未查。

### 2.3 `verify_clear_refs_race.py`

参数 `--rounds 30 --pages 4096 --rate 100000`（16 MiB 的缓冲区，每秒写 10 万页）。

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

**反向对照。** 这个脚本的判据可信，前提是它在有问题的版本上确实报得出漏页。2026-09-23（新 920B，结果目录 `gsd-new920b-20260923`，当时的内核与模板都早于本章的条件标签，只作对照）用同一个脚本、默认参数做过：
把 guest 里的 gsd 临时换成修改前的二进制（`criu dump` 用 `--leave-running`），先后三次运行、扣除写入进程卡死的轮次后共 50 轮，其中 **31 轮有漏页**（三次分别是 9 / 15、7 / 10、15 / 25，漏页合计 4283 页，单轮最多 717 页）；
同一天换成修改后的二进制跑 30 轮，有漏页的轮数为 0。别的章提到"修改前 31 轮漏页"时指的就是这一组。

"停住时长 0.53 ms"是这项修复的代价：为了关上漏页的窗口，进程在 dump 之后要多停这么久，等 overlay 切完层、soft-dirty 位清完（[09](09-checkpoint.md)）。

### 2.4 `probe_whiteout_bug.py`

用例 `bug`、`ctl`、`nodel`、`masked` 各一次，`trap` 三次，共七个沙箱。全部 `crashed=False`；四个 whiteout 用例的第二次删除都成功，dmesg 里没有 whiteout 相关的行；
三次 `trap` 各新建并访问 6000 个目录和文件，错误计数都为空。每个沙箱开机时既存的 whiteout 数都是 0（upper 与各层里都没有），也就是说模板里不存在曾经掩盖过这个问题的那个偶然条件（[07](07-overlay-concurrency-and-fixes.md) 的"属于 work 目录的缓存"一节）。

---

## 3. 性能

下面三张表是三种独立的口径。负载的形状、统计的方式、进程树的棵数都不一样，**同名指标不能跨表比较，更不能相减**。它们共同的条件是 §1 的那一组：2 vCPU / 2048 MB、THP never、同一个模板。
所有耗时都是 gsd 自报的服务端分段（[24](24-configuration-and-observability.md) 的计时键），不含客户端到 gsd 的网络往返，单位 ms。

### 3.1 `verify_gsd.py`：单棵 192 MiB、全脏，连续 restore 8 次

负载：一棵 192 MiB 的进程树，全量 checkpoint 之后把 192 MiB 全部改写，再建一个 checkpoint，然后对它连续 restore 8 次。统计这 8 次（包含第一次）。p50 取第 5 小的值。

| 分段 | p50 | 最小 | 最大 |
|---|---|---|---|
| `total_ms` | 83.3 | 80.5 | 93.3 |
| `criu_restore_ms` | 71.0 | 68.7 | 80.3 |
| `kill_ms` | 11.1 | 10.2 | 11.7 |
| `find_pids_ms` | 0.7 | 0.7 | 1.1 |
| `overlay_restore_ms` | 0.4 | 0.3 | 0.4 |
| `precheck_ms` | 0.1 | 0.1 | 0.1 |

8 次里最大的那一次（93.3 ms）是第一次 restore。根盘已用量起点 272 MB（`df -m` 的输出），8 次 restore 之后每次都是 +0，判定"根盘恒定"。

上一轮（前一个提交号、同一个内核、另一个模板）同一脚本的 `total_ms` p50 是 84.8 ms。两轮是不同模板上各测一次，这 1.5 ms 的差不说明任何事情；能说明"两个版本没有差别"的是下一节的逐轮交错对照。

### 3.2 `verify_multitree.py` 的 V12：逐轮交错，以及一、二、三棵树

**第一部分：与对照模板逐轮交错。** 负载是一棵 192 MiB 的树：全量 → 全部改写 → 再建一个 → 对它 restore 9 次并去掉第 1 次 → 连续 create 5 次（不改内存）。
两个模板各跑 8 轮、每轮一个新沙箱、奇偶轮交换先后，所以每个模板各有 64 次 restore、40 次 create 进入统计。本轮的对照是"同一个内核、同一份 Go 代码，只差提交号"（两个提交之间只有一个补丁文件的组织方式不同，`packages/` 与 `py-sdk/` 没有差异）。

| 分段（p50） | 本轮模板 | 对照模板 | 差 |
|---|---|---|---|
| restore `total_ms` | 81.28 | 82.55 | −1.27 |
| restore `criu_restore_ms` | 68.61 | 69.83 | −1.22 |
| restore `kill_ms` | 11.51 | 11.62 | −0.11 |
| restore `find_pids_ms` | 0.65 | 0.65 | 0.00 |
| restore `precheck_ms` | 0.12 | 0.12 | 0.00 |
| create `total_ms` | 15.55 | 16.10 | −0.55 |
| create `criu_dump_ms` | 13.60 | 14.18 | −0.58 |
| create `find_pids_ms` | 0.63 | 0.63 | 0.00 |
| create `precheck_ms` | 0.04 | 0.04 | 0.00 |

脚本判定的八条对照（上表除 `kill_ms` 一行外）全部在 ±3 ms 内。这张表同时给出了一个有用的参照：**两份本应相同的东西，在这套方法下测出来会差 1 ms 上下。** 小于这个量级的差别不要解读。
同一负载下，全量 checkpoint 的 `criu_dump_ms` p50 是 108.01 ms（`total_ms` 111.98），192 MiB 全部改写之后的那一次是 102.83 ms。

这张表里的 create 是"不改内存、连续再建一个"，所以只有十几毫秒；它量的是增量 checkpoint 的固定开销，不是有改动量时的耗时。有改动量的 checkpoint 见 §3.3。

**第二部分：一、二、三棵树。** 每棵树是一个 64 MiB 的进程，流程同上，只用本轮模板，同样 8 轮（每档 64 次 restore、40 次 create）。

| 分段（p50） | 1 棵 | 2 棵 | 3 棵 |
|---|---|---|---|
| restore `total_ms` | 32.94 | 60.38 | 93.31 |
| restore `criu_restore_ms` | 26.63 | 53.13 | 81.65 |
| restore `kill_ms` | 5.16 | 5.96 | 9.85 |
| 增量 create（不改内存）`total_ms` | 11.17 | 21.84 | 33.49 |
| 增量 create `criu_dump_ms` | 9.61 | 19.86 | 31.18 |
| 增量 create `stopped_ms` | 0.65 | 11.32 | 22.46 |
| 全量 create `criu_dump_ms` | 47.38 | 91.91 | 130.98 |
| 全量 create `stopped_ms` | 2.53 | 45.08 | 84.57 |
| 全部改写后的 create `criu_dump_ms` | 42.07 | 85.63 | 132.08 |
| 全部改写后的 create `stopped_ms` | 1.18 | 45.70 | 90.17 |

从这张表读得出多树的代价（机制在 [09](09-checkpoint.md)、[10](10-restore.md)，代价模型在 [17](17-performance-after-fixes.md)）：

- 每多一棵 64 MiB 的树，restore `total_ms` 多 27.44 ms（1→2 棵）与 32.93 ms（2→3 棵），不改内存的增量 checkpoint 的 `total_ms` 多 10.67 与 11.65 ms，全量 dump 的 `criu_dump_ms` 多 44.53 与 39.07 ms——各棵树是串行处理的，耗时随树数近似线性增长；
- `stopped_ms` 在单棵树时不到 3 ms，两棵以上时与"其余各棵的 dump 时间"同量级：先 dump 完的树要停着等后面的树。这就是多棵树时进程实际被冻结的时长。

### 3.3 `demo_checkpoint_perf.py`：六档改动量（10-08 历史口径）

本节保留旧脚本结果；当前脚本已采用 §3.4 的独立分档口径。旧负载：一个预分配 192 MiB 的常驻进程，按六档依次改写内存并写文件，每档建一个 checkpoint，最后逐个 restore 回去。脚本连跑三次，下表是三次的中位数；每次运行每档只有一个样本。

| 档 | 内存改写 / 文件写入 | create `total_ms` | restore `criu_restore_ms` | restore `total_ms` |
|---|---|---|---|---|
| cp0 | 全量（进程刚起来） | 117.1 | — | — |
| cp1 | 0 / 0 | 12.2 | 83.0 | 96.1 |
| cp2 | 12 MiB / 4 MiB | 23.3 | 73.0 | 85.8 |
| cp3 | 24 MiB / 8 MiB | 29.1 | 74.4 | 87.6 |
| cp4 | 48 MiB / 16 MiB | 38.9 | 78.5 | 91.8 |
| cp5 | 96 MiB / 32 MiB | 63.6 | 77.7 | 90.8 |
| cp6 | 192 MiB / 64 MiB | 107.7 | 84.1 | 96.6 |

- 三次运行共 18 次 restore，文件内容与内存标记的校验全部通过；
- SDK 返回的各分段与 gsd 日志里同一个 checkpoint 的分段逐项相同，最大差 0.000 ms；
- `overlay_sink_ms` 的中位数是 0.56 ms：checkpoint 时切层本身的开销与写了多少文件无关；
- checkpoint 的耗时随改动量增长（12.2 → 107.7 ms），因为增量 dump 要写出本代的脏页；
- **restore 的耗时基本不随改动量变化**（六档都在 85.8～96.6 ms 之间）。CRIU 恢复要把整个进程的内存重新建起来，工作量取决于进程有多大，不取决于某一代改了多少。这是进程级回滚与整机级回滚在成本模型上最大的不同（[18](18-process-vs-machine-level.md)）；
- cp1 的 restore 比 cp2 还慢。它在每次运行里是第一个被 restore 的；§3.1 里 8 次中最慢的同样是第一次。沙箱里的第一次 restore 偏慢这一点两个脚本都看得到，原因没有单独查过。

三次运行之间单档的差可以到十几毫秒（例如 cp6 的 create `total_ms` 三次分别是 104.2、107.7、122.6），所以这张表适合看趋势，不适合做版本之间的精细对比。


### 3.4 新版独立分档与 950 沙箱级对照（2026-10-16）

**本轮条件。** 进程级运行在新 920B，复用 §1 的 gsd、orchestrator 与发布内核二进制，三者哈希不变；SDK 为 `dcca61266`。
新建模板 `base-gsd`（ID `rdhpl4g7gl0ouexktqe9`），2 vCPU / 2048 MB，CRIU 4.2.1，THP=never、rcu_expedited=1。
脚本为 `schema_version=2`、`measurement_mode=per-tier-size`，每档新沙箱，只分配本档内存，改写后恢复本档全量基点；三轮共 18 次恢复，完整内存 SHA256、标记与文件内容全部正确，SDK/journal 分段一致。脚本 11 项离线回归在 920B 通过。本轮没有重跑完整功能验收或压测，不能把历史通过记录改写为本轮完成。

以下均为服务端 `total_ms` 中位数，单位 **ms**；快照列为增量快照，全量基点另存于摘要。
950 列引用 [950 沙箱级性能](../../rollback/docs/18-950-performance.md) 中 2026-10-13 的结果，**950 的 gsd 尚未测试**。

| 总改动量 MiB | 内存 / 文件 MiB | 920B 进程级快照 | 950 沙箱级快照 | 920B 进程级回滚 | 950 沙箱级回滚 |
|---|---|---:|---:|---:|---:|
| 0 | 0 / 0 | 10.6 | 16.0 | 9.1 | 20.9 |
| 16 | 12 / 4 | 18.1 | 22.4 | 14.8 | 28.8 |
| 32 | 24 / 8 | 25.9 | 29.0 | 19.8 | 34.8 |
| 64 | 48 / 16 | 37.6 | 40.0 | 31.1 | 44.1 |
| 128 | 96 / 32 | 59.8 | 71.5 | 52.6 | 65.9 |
| 256 | 192 / 64 | 106.2 | 123.8 | 93.9 | 103.5 |

原来进程级慢主要有两个原因，目前均已处理：

1. 实现中的性能缺陷：错误 copy-up、cgroup 迁移等待 RCU、杀进程后轮询，以及重复进程查询等，引入了额外复制和等待，相关问题已经修复或优化。
2. 测试分档不符合预期：原脚本所有档位都预分配最大档的 192 MiB 负载，每次回滚都重建这一规模的进程，导致小档耗时也接近最大档。现已改为各档独立分配对应大小的负载，并统一恢复到本档基点。

性能缺陷修复后，920B 历史同口径微基准中的进程级回滚，从修复前 196 ~ 206 ms，先降至 121 ms，再降至 85～89 ms，耗时降低约 55%～59%。

测试分档修正后，进程级回滚耗时随实际负载大小增长，从 **9.1 ms 增至 93.9 ms**，不再各档都接近 90 ms；本次 920B 与 950 的对照中，进程级各档快照和回滚耗时均更低。

**解释与适用范围。** 本轮各档 RSS 中位数分别为 11.63 / 24.76 / 36.75 / 60.76 / 108.74 / 204.76 MiB，包含 Python 运行时，确认负载不再全部等于最大档。
0 档只是额外负载为零，进程和系统活动仍需恢复。每档为新沙箱首次恢复；进程大小与改动量同时变化，不能用这组数隔离固定进程大小下的改动量效应。
两列来自不同宿主与负载构造，只作本次实测并列，不代表方案本身的净加速比；历史 196～206 → 121 → 85～89 ms 来自旧 920B 的另一项同口径微基准，不与本轮相减。

归档入口为 [验证摘要](../validation/README.md)：`summary.json` 保留三轮各档服务端与墙钟耗时、全量基点和 RSS；`provenance.json` 记录被测版本、脚本哈希及沙箱级对照来源；历史原文摘录记录两个修复前 p50 为 196 / 206 ms，而约 200 ms 是概括值。

---

## 4. 压测

### 4.1 本轮四组

判据见 [19](19-testing-and-debug-kernel.md)。四组都带看门狗，没有触发；`all` 与 `noworkers` 两组并行跑，多棵树与删文件密集两组并行跑。四组都没有留下"预期之外的 create 失败"的现场文件。

**并发文件操作与对照（`stress_overlay_ckpt.py`，各 300 轮）**

| 项 | all（6 个 worker，十种操作全开） | noworkers（无 worker） |
|---|---|---|
| 判定 | 通过 | 通过 |
| dmesg / 串口关键词新命中 | 0 / 0 | 0 / 0 |
| create / restore / delete 成功次数 | 77 / 153 / 15 | 75 / 155 / 13 |
| gsd 侧错误 | 只有 `create:resource_exhausted` 55 次 | 只有 `create:resource_exhausted` 57 次 |
| worker 操作总数 | 3355517 | 0 |
| worker 错误 | `ENOENT` 199748，`ESTALE` 118011，其他 0 | — |
| worker 心跳过期 | 无；6 个 worker 结束前都在运行 | — |
| 结束时 `/health` | ok | ok |
| `mnt_cache` 净增 | +42（282 → 324） | −34（332 → 298） |

`create:resource_exhausted` 是快照存储放不下时的预期保护（[13](13-state-concurrency-capacity.md)），不是故障；这两组的 300 轮里各有五十多次，说明压测确实一直压在容量边界上。
`mnt_cache` 的净增在几十个的量级且有正有负，不随轮数增长；修改前它每次切换都涨（§5.1）。

这两组的结果文件里还有脚本侧的墙钟中位数（all 组 create 84 ms、restore 66 ms）。那不是 gsd 的耗时，只反映当时有多少 worker 在抢 CPU，不作判据，也不要引用。
同理，all 组的 worker 操作总数比上一轮多约四成，是因为这一轮同时只有两组在跑、上一轮是四路并行，机器负载不同。

**多棵树（`stress_multitree.py`，200 轮，三棵树、每棵缓冲区 4 MiB）**

| 项 | 值 |
|---|---|
| 判定 | PASS |
| create / restore / delete 成功次数 | 92 / 100 / 9 |
| 宿主侧校验 | 192 次，0 失败 |
| 进程自检留下的 `.bad` 文件 | 0 个 |
| 预期之外的 gsd 错误 | 0 |
| dmesg 关键词命中 | 0 |
| create 的 p50 | `total_ms` 44.67，`criu_dump_ms` 41.96，`stopped_ms` 32.04 |
| restore 的 p50 | `total_ms` 27.13，`criu_restore_ms` 22.63，`kill_ms` 2.74 |
| 结束状态 | `/health` ok；`state.json` 版本 2，留有 88 个 checkpoint 条目、7 层 |
| `mnt_cache` 净增 | +42 |

这里的 create 耗时比 §3.2 的三棵树增量 create 大，是因为这个负载每 0.1 秒把每棵树的缓冲区整个写一遍，每次 checkpoint 都有实打实的脏页要写。它是第四种口径，同样不与前面的表比较。

**删文件密集（`stress_delete_dense.py`，200 轮，4 个 worker）**

| 项 | 值 |
|---|---|
| 判定 | PASS |
| create / restore / delete 成功次数 | 92 / 100 / 9 |
| 清单逐条校验 | 192 次，0 失败 |
| 可校验的那棵树上做的变更 | 删文件 4544，整目录删除 1093，重建目录 1132，建文件 6236；异常 0 次 |
| worker 操作总数 | 697777 |
| worker 错误 | `ENOENT` 316165，`ENOTEMPTY` 9，`ESTALE` 4，`EEXIST` 2；预期之外的 errno 0 |
| 被 dump 与 restore 的用户进程 | 无错误 |
| 结束时各层里的 whiteout / opaque 目录 | 3288 / 2821 |
| dmesg 关键词命中与 whiteout 相关提示 | 0 / 0 |

最后两行合起来才有意义：确实产生了几千个 whiteout 与 opaque 目录，而内核没有任何抱怨。

### 4.2 补充证据：前一个提交号上的八组、种子对照与调试内核

本轮的范围不含完整的八组压测、多种子对照、其余探针和调试内核。这些在当天稍早的一轮里做过，那一轮的代码是**前一个提交号 `c0630349c`**：Go 代码与 `259f1a25c` 相同，内核就是 §1 的同一个二进制，区别只是仓库里补丁文件的组织方式。
它们作为补充证据列在这里；严格地说，它们证明的是"这个内核加上这份 Go 代码"，而不是"这个提交号"。

**发布版内核，八组各 300 轮**（2026-10-08，结果目录 `gsd-fix6-20261008`；四路并行）：

| 组 | 判定 | dmesg / 串口 | create / restore / delete | worker 操作 | `ENOENT` | `ESTALE` | 其他 errno | `mnt_cache` 净增 |
|---|---|---|---|---|---|---|---|---|
| all | 通过 | 0 / 0 | 77 / 153 / 15 | 2370871 | 143279 | 88137 | 0 | +50 |
| readonly | 通过 | 0 / 0 | 77 / 151 / 18 | 3817809 | 319460 | 3451 | 0 | +11 |
| wr3 | 通过 | 0 / 0 | 77 / 153 / 15 | 1185589 | 522 | 51634 | 0 | +33 |
| rename | 通过 | 0 / 0 | 77 / 153 / 15 | 811240 | 719 | 30258 | 0 | +9 |
| append | 通过 | 0 / 0 | 77 / 153 / 15 | 5427939 | 0 | 4 | 0 | +46 |
| reltouch | 通过 | 0 / 0 | 77 / 153 / 15 | 4776714 | 0 | 53512 | 0 | +26 |
| noworkers | 通过 | 0 / 0 | 75 / 155 / 13 | 0 | 0 | 0 | 0 | +21 |
| enospc | 通过 | 0 / 0 | 磁盘写满的 7 档；删掉填充后 create 与 restore 恢复正常、内容正确 | — | — | — | — | — |

本轮 all 与 noworkers 两组的 create / restore / delete 次数与这张表里的逐个相同——脚本用固定种子，同样的代码走的是同一条操作序列。

**多种子对照。** all 与 noworkers 两种分组各用 10 个不同的种子跑 300 轮，共 20 组：20 组全部通过，worker 的错误只有 `ENOENT` 与 `ESTALE`，dmesg 与串口零命中，gsd 侧只有 `resource_exhausted`。
这组对照的意义在于参照：同样 20 组，在最后两个内核修复之前的那一版内核上只有 10 组干净（noworkers 9/10、all 1/10），其余各组出现 `ELOOP`、`ENXIO`、`ENOTDIR`。

**调试内核**（KASAN 加 lockdep，4096 MB 的调试模板；同一份内核源码）：

| 项 | 判定 | dmesg / 串口 | create / restore / delete | worker 操作 | 其他 errno | 结束时 `debug_locks` |
|---|---|---|---|---|---|---|
| `probe_whiteout_bug.py` 五种用例（`trap` 三次） | 都不崩；串口输出里 KASAN / BUG / WARNING 为 0 行 | — | — | — | — | — |
| all，300 轮 | 通过 | 0 / 0 | 77 / 154 / 14 | 882427 | 0 | 1 |
| wr3，300 轮 | 通过 | 0 / 0 | 77 / 154 / 14 | 637769 | 0 | 1 |
| rename，300 轮 | 通过 | 0 / 0 | 77 / 154 / 14 | 373076 | 0 | 1 |
| noworkers，300 轮 | 通过 | 0 / 0 | 79 / 153 / 14 | 0 | 0 | 1 |
| 多棵树，200 轮 | 通过 | 0 / 0 | 92 / 100 / 9；校验 192 次 0 失败 | — | — | 1 |
| 删文件密集，200 轮 | 通过 | 0 / 0 | 92 / 100 / 9；清单校验 192 次 0 失败 | 439863 | 0 | 1 |

调试内核上没有跑功能验证（`verify_fixes.py`、`verify_multitree.py`）。

**其余探针**（同一轮、发布版内核）：`probe_estale.py` 的 195 条命令结果、`probe_estale_file.py`、`probe_mmap_rollback.py` 的 98 个检查点、`probe_fd_rollback.py` 的输出，都与再前一轮逐项相同；
`probe_leak.py` 100 轮与 `probe_umount.py` 20 轮见 §5.1。

**一处没有查清的记录。** 这一轮的多种子对照里，在一个尚未定稿的候选内核上，有一组在第 174、177 轮各出现一次 create 返回 `internal`（`criu dump failed: exit status 1`），其余操作、worker、dmesg 全部正常，随后的 create 成功。
当时脚本只留了报错的前 200 个字符，看不到 CRIU 的出错行。之后同一个种子重放 5 次、正式内核上的各组压测、调试内核上的各组以及本轮的四组都没有再出现。没有证据把它归到内核改动，也不能排除；列在 [15](15-known-limits.md)，编号 U-02。

---

## 5. 专项数字

这一节的数字来自较早的几轮，各自的代码版本与内核都早于最终版本，每张表上方写明。它们回答的是"某个修复起没起作用"，与 §3 的性能口径无关。

### 5.1 被删层的回收（泄漏）

`probe_leak.py`：基点之上每轮"写 1 MiB → create → 按路径读回 → restore 基点 → delete"，每 10 轮采样；"被占着的磁盘"指同一个根盘上已删除但仍被内核引用着、因而不释放的部分。
2026-10-08，结果目录 `gsd-fix5-20261008`；对比的是加入回收逻辑之前与之后的两版内核，gsd 相同。

| 内核 | 轮数 | 被占着的磁盘（每轮） | 全程增量 | `mnt_cache`（每轮） | `mnt_cache` 起 → 止 |
|---|---|---|---|---|---|
| 加入回收之前 | 300 | +1040.0 KB | +312012 KB | +5.996 | 317 → 2100 |
| 加入回收之后 | 300 | 0.0 KB | +28 KB | −0.003 | 290 → 327 |

有进程拿着旧层里的文件不放时，残留与持有量相等、放手后回落：20 轮里一个范围外的 root 进程每轮打开当轮写的 4 MiB 文件并一直拿着，持有期间多占 82312 KB（即 20 个 4 MiB 文件），
放手后剩 160 KB，再过 3 轮为 0；加入回收之前的内核上是 164240 KB，放手后也不回落。

在最终内核上（前一个提交号那一轮，结果目录 `gsd-fix6-20261008`）复测：`probe_leak.py` 100 轮，被占着的磁盘恒定在 248880 KB，斜率 0.0 KB/轮，层目录数 3 → 3；
`probe_umount.py` 20 轮通过，没有残留，dmesg 为空。

G-11 的现场值（§2.1：40 轮扣除 journal 后净增 32 KB）是这条性质在本轮的确认。

### 5.2 可写打开文件的 checkpoint 代价

`probe_move_cost.py`：一个范围外的 root 进程以读写方式打开一个 N MiB 的文件并一直拿着，然后做一次 checkpoint，看 `overlay_sink_ms`。
2026-09-30，结果目录 `gsd-fix4-20260930`，当时的内核是引入这一行为的那一版。2026-10-08 较早的一轮在加入回收之后的内核上复测过同一个探针，16、64、128 MiB 三档是 12.67、47.52、98.71 ms，与下表同档；最终内核上没有再测。

| 被持有的文件 | `overlay_sink_ms` |
|---|---|
| 没有 | 1.70 |
| 1 MiB | 1.65 |
| 16 MiB | 12.90 |
| 64 MiB | 48.01 |
| 128 MiB | 101.54 |

由后三行得出约 0.8 ms/MiB：checkpoint 时内核在切换的写锁内把这个文件整份拷进沉下去的层（[07](07-overlay-concurrency-and-fixes.md) 的"打开的文件跨切换的语义"一节），期间别的 overlay 操作被挡住。
没有这类文件时，切层不到 2 ms；§3.3 里 `overlay_sink_ms` 的中位数 0.56 ms 是同一件事在本轮的读数。

### 5.3 层栈上限与容量预检

两者都由本轮的 `verify_fixes.py` 直接测到，数字在 §2.1：层栈上限是连续 190 次成功、第 191 次被拒（选项串 4105 字节，超过内核的 4096 字节上限）；
容量预检按"待 dump 进程的匿名内存与共享内存之和 × 1.2"估算，快照存储剩余与 `MemAvailable` 任何一个不够就拒绝，实测那一次是内存不够。

---

## 6. 修复历程对照

下表把历次验证里 `verify_gsd.py` 的 `total_ms` p50 并列出来，只为说明修复过程中性能的走向，对应的叙述在 [17](17-performance-after-fixes.md)。
**每一行是不同的模板、多数行是不同的内核，各测一次。不要把相邻两行相减当作某一项改动的代价**——能回答"某一项改动贵了多少"的只有逐轮交错的对照。负载与口径同 §3.1，平台都是新 920B。

| 日期 | gsd | 内核 | `total_ms` p50 | 结果目录 |
|---|---|---|---|---|
| 09-30 | 问题清单修复之前 | 问题清单修复之前 | 75.6、77.3（同一个模板先后两次） | `gsd-fixes-20260930` |
| 09-30 | 第一版修复 | 第一轮内核修复（当时仍有并发崩溃问题） | 93.8、94.5（同一个模板另有一次 104.1） | `gsd-fixes-20260930` |
| 09-30 | 收回进程扫描的回退之后 | 同上 | 84.7 | `gsd-fixes-20260930` |
| 09-30 | 同上 | 加入切换与文件操作的互斥、旧 fd 写入的回滚 | 87.0 | `gsd-fix3-20260930` |
| 09-30 | 同上 | 再加祖先链 `ESTALE`、可写打开文件的处理，并带代码布局填充 | 80.7 | `gsd-fix4-20260930` |
| 10-08 | 同上 | 去掉布局填充，加入层的回收 | 86.1 | `gsd-fix5-20261008` |
| 10-08 | 多棵树、`/tmp` 回滚、根目录 755（前一个提交号） | 同上 | 86.5 | `gsd-final-20261008` |
| 10-08 | 同上 | 再加 work 目录缓存与删除竞争的修复（最终内核） | 84.8 | `gsd-fix6-20261008` |
| 10-08 | `259f1a25c` | 最终内核 | **83.3** | `gsd-release-20261008` |

能从这张表里读出的只有三件事：第一版修复带来过一次明显的回退，随后收回了大半；带代码布局填充的那一行读数最低，这与 [17](17-performance-after-fixes.md) 里用逐轮交错对照得到的结论方向一致（为什么最终不保留填充也在那一章）；
最后几轮在 83～87 ms 之间，彼此的差在单次测量的波动之内。问题清单修复之前的 75～77 ms 与现在的 83 ms 之间的差距由哪几项构成，本表回答不了，逐项的归因与定量在 [17](17-performance-after-fixes.md)。

---

## 7. 缺口

下面是到本书定稿时**没有**证据的部分。评审和接手的人应当把这张表与前面的结果放在一起读。

| 缺口 | 现状 | 影响 |
|---|---|---|
| 950 | 未部署、未测 | 本章没有 950 的 gsd 数字；§3.4 的 950 列来自沙箱级。gsd 不依赖 950 独有的硬件特性（[18](18-process-vs-machine-level.md)），但这只是设计上的判断，没有实测 |
| `HOST_GSD_PATH=off` 的关闭态 | 没有真实建过模板。依据是三样：`internal/cfg` 与 rootfs 的单测通过；把 `internal/cfg` 的测试编成二进制后在真实进程环境变量下跑，六种取值的解析结果符合预期；读代码确认开关管的五处都在建模板路径上。没有验证"关掉之后建出的模板与上游逐字节一致"。（开关的另一半——不设变量、按默认路径注入——已于 2026-10-09 现场确认，见 §1.2） | 关闭态下是否真的与不带 gsd 的 orchestrator 行为相同，没有现场证据（[15](15-known-limits.md) 的 T-08）。另外只有小写的 `off` 才是关（[15](15-known-limits.md) 的 L-09） |
| 只有单测、没有现场走过的分支 | 两棵树持有同一个 socket 的预检；三棵树时中间一棵 restore 失败；多棵树的 dump 超时与总时限；某棵树 dump 失败后前面的树被放开；容量预检按多棵树内存之和估算；`state.json` 版本 1 的加载；criu 被杀或崩溃之后的防御性放行 | 这些路径的编排逻辑有单测，但 CRIU 与内核在这些情况下的实际行为没有观察过（[15](15-known-limits.md) 的 T-01 ～ T-07） |
| 更大的 guest 规格 | 只测了 2 vCPU / 2048 MB（调试模板为 4096 MB，不做性能测量） | 耗时与容量边界随规格怎么变没有数据。快照存储占 guest 内存，规格不同时容量预检的拒绝点会不同 |
| 更多的树 | 功能与性能都只到三棵 | 三棵以上的线性外推没有验证；[17](17-performance-after-fixes.md) 的代价模型只在一到三棵之间有依据 |
| 更大的进程 | §3.4 已覆盖额外负载 0～192 MiB 六档；容量测试最大 480 MiB | 超过 192 MiB 的性能趋势没有分档实测，容量验证不能代替性能结果 |
| 长时间运行 | 最长的单次压测是 300 轮，墙钟一分多钟；泄漏专项 300 轮 | 小时级连续运行下的行为（层与镜像的累积、journal 的增长、内核对象的缓慢变化）没有数据 |
| 调试内核上的功能验证 | 调试内核上只跑了压测与一个探针 | 功能路径上的越界与锁顺序问题，只在压测覆盖到的范围内被检查过 |
| 本轮没有重跑的压测组与探针 | readonly、wr3、rename、append、reltouch、enospc 六组，10 个种子的对照，除 `probe_whiteout_bug.py` 之外的探针，以及调试内核——都只在前一个提交号上跑过（§4.2） | Go 代码相同、内核是同一个二进制，推断结论不变；但严格说它们不是在 `259f1a25c` 的二进制上测的 |
| 可写打开文件的代价 | §5.2 的数字是 2026-09-30 与 2026-10-08 较早的两版内核上测的 | 约 0.8 ms/MiB 这个系数在最终内核上没有直接读数（限制本身见 [15](15-known-limits.md) 的 L-04） |
| 内核没有从仓库补丁重新编译 | 验证的内核二进制是当天稍早编出的；与仓库补丁的对应靠树哈希相等确认（§1） | "按仓库里的补丁与构建脚本重编，得到行为相同的内核"没有做过 |
| Python SDK 的单测 | 2026-10-08 的结果目录里没有 `py-sdk/tests/test_gsd_*.py` 的运行记录 | SDK 的行为由功能验证间接覆盖（所有脚本都经 SDK 调用），但单测本身在这一轮是否通过没有留档 |
| cgroup 的 `favordynmods` | 未测 | [16](16-restore-slowness-case.md) 里作为 `rcu_expedited` 之外的替代办法提到，效果未知 |
| CRIU 不支持的特性 | 没有兼容清单 | 已知 stdout 接 journald 的进程、树间用 unix socket 相连的进程无法 dump；除此之外哪些常见程序会失败没有系统地试过 |
| 沙箱 pause / resume 之后 | 没有专门的结果 | 设计上 gsd 连同内存一起被冻结再恢复、不重新加载状态（[13](13-state-concurrency-capacity.md)），但没有"pause → resume → restore"的现场验证（[15](15-known-limits.md) 的 T-09） |
| 三处未查清的现象 | 新沙箱首个请求偶发的长等待（本轮记到一次，§2.2；落位后的复核又记到一次，§1.2）；候选内核上未复现的两次 create `internal`（同一沙箱的第 174、177 轮，§4.2）；修复后仍有的单发 `ENOTEMPTY` | 都不影响判定，原因未知，见 [15](15-known-limits.md) 的 U-01、U-02、U-03 |

---

## 8. 数据出处

**本轮入库摘要（2026-10-16）**：见 [`validation/`](../validation/README.md)。`920b-20261016/summary.json` 保存三轮性能样本、各档中位数、恢复判定与原始文件哈希；`provenance.json` 保存部件、SDK 与脚本版本及 950 沙箱级参考数；`historical-fix-evidence.json` 保存旧 920B 原文摘录及来源哈希。完整日志和原始 JSON 留在测试工作区。


路径相对于各自的结果目录。结果目录不随手册入库，保存在测试工作区。文件名里的 `base-gsd9` 等是验证时的模板名，对照见 §1.1。

**`gsd-release-20261008`（2026-10-08，`259f1a25c`）**

| 数字 | 文件 |
|---|---|
| 条件标签：提交号、各 sha256、内核版本串、模板 ID、时间、go 版本 | `summary.txt` 第二、五节；`deliverables.sha256`；`build.log`；`envcheck-base-gsd9.txt`；`guest-gsd-check.txt`；`chain.log` |
| 补丁与内核源码树的对应（树哈希） | `applycheck.txt` |
| 两个提交之间只差一个补丁文件 | `summary.txt` 第一节第 6 条；`sync-check.txt` |
| 验证时 orchestrator 的环境变量、结束状态 | `final-status.txt`；`switch.log` |
| `HOST_GSD_PATH` 六种取值的解析结果与单测 | `hostgsdpath-env.log`；`hostgsdpath-unittests.log`；`summary.txt` 第四节 |
| §2.1 `verify_fixes.py` 的计数与各项实测 | `func/verify_fixes.log`；`func/verify_fixes.json` |
| §2.2 `verify_multitree.py` 的判定、V5 的大小、慢调用 | `func/verify_multitree.log`；`func/verify_multitree.json`（`slow_calls`） |
| §2.3 漏页测试 | `func/clear_refs_race.log`；`func/clear_refs_race.jsonl`；汇总值另见 `summary.json` 的 `this_round.clear_refs_race` |
| §2.4 `probe_whiteout_bug.py` | `func/probe-whiteout.log`；`func/probe-whiteout.json` |
| §3.1 `verify_gsd.py` 分段 | `func/verify_gsd-base-gsd9.log` |
| §3.2 V12 两部分 | `func/verify_multitree.json`（`perf.summary`）；`summary.json` 的 `this_round.verify_multitree.V12_summary` 与 `V12_checks` |
| §3.3 六档 | `func/perf-run1.json`、`perf-run2.json`、`perf-run3.json` 及同名 `.log`；三次的中位数在 `summary.json` 的 `this_round.demo_checkpoint_perf` |
| §4.1 all、noworkers | `stress/rel-all/stress-summary.json`、`stress/rel-noworkers/stress-summary.json`；各自的 `RESULT`、`console.txt`、`stress-dmesg-*.txt`；分组参数在 `stress/jobs-rel.txt` |
| §4.1 多棵树 | `stress/rel-multitree/multitree-summary.json`、`multitree-timings.json`、`RESULT` |
| §4.1 删文件密集 | `stress/rel-deldense/deldense-summary.json`、`RESULT` |
| 四组的汇总判定 | `summary.json` 的 `this_round.stress`；`summary.txt` 第三节 |
| 上一轮的同名指标（§3.1 的 84.8，§4.1 的"多约四成"） | `summary.json` 的 `previous_round`；`summary.txt` 第三节 |
| §1.2 落位后的复核：启动环境、模板 ID 与建模板耗时、guest 内核对 | `summary.txt` 第七节；`place-20261009/switch.log`、`build-base-gsd.log`、`guest-gsd-check.txt`、`envcheck-base-gsd.txt` |
| §1.2 落位后的复核：三个脚本的结果、慢调用 | `place-20261009/func/verify_gsd-base-gsd.log`、`verify_multitree.log`、`verify_multitree.json`、`verify_fixes.log`；`place-20261009/chain.log` |

**`gsd-fix6-20261008`（2026-10-08，前一个提交号 `c0630349c`，同一个内核二进制）**

| 数字 | 文件 |
|---|---|
| §4.2 发布版八组 | `summary.txt` 第七节第 2 条；`stress-v6-analysis.json`；`stress/jobs-v6.txt` |
| §4.2 多种子对照 | `summary.txt` 第七节第 3 条；`ab-stress-analysis.json`；`ab-stress/jobs.txt` |
| §4.2 调试内核各项 | `summary.txt` 第八节；`stress-dbg6-analysis.json`；`envcheck-base-gsd-dbg8.txt` |
| §4.2 其余探针、§5.1 最终内核上的泄漏复测与卸载路径 | `summary.txt` 第七节第 9 条 |
| §4.2 未查清的 create `internal` | `summary.txt` 第九节第 1 条 |
| §6 的 84.8 | `v6/verify_gsd-base-gsd8.log` |
| 内核与调试内核的 sha256 | `kernels.sha256` |

**更早的几轮**

| 数字 | 文件 |
|---|---|
| §2.3 的反向对照（修改前 31 / 50 轮漏页，修改后 0 / 30） | `gsd-new920b-20260923/race-prefix-0923-0826-1.log`、`-2.log`、`-3.log` 与 `race-prefix-0923-0826.jsonl`；`race-fixed-0923-0826.log` |
| §5.1 回收前后的对比、持有场景 | `gsd-fix5-20261008/summary.txt` 第四节第 2 条；`gsd-fix5-20261008/leak/` 下三个 `leak-*.json` |
| §5.2 可写打开文件的代价 | `gsd-fix4-20260930/probe-move-cost-base-gsd5.json`；`gsd-fix4-20260930/summary.txt` 第四节；复测值在 `gsd-fix5-20261008/probe-move-cost-base-gsd6.json` |
| §6 的 75.6、77.3 | `gsd-fixes-20260930/matrix/verify_gsd-base-gsd-kv1.log`；`gsd-fixes-20260930/verify_gsd-base-gsd-kv2.log` |
| §6 的 93.8、94.5、104.1 | `gsd-fixes-20260930/verify_gsd-base-gsd2-kv2.log`；`verify_gsd-base-gsd2.log`；`matrix/verify_gsd-base-gsd2-kv1.log` |
| §6 的 84.7 | `gsd-fixes-20260930/perf-fix/verify_gsd-base-gsd3.log` |
| §6 的 87.0 | `gsd-fix3-20260930/verify_gsd-base-gsd4.log` |
| §6 的 80.7 | `gsd-fix4-20260930/verify_gsd-base-gsd5.log` |
| §6 的 86.1 | `gsd-fix5-20261008/verify_gsd-base-gsd6.log` |
| §6 的 86.5 | `gsd-final-20261008/stageB/verify_gsd-base-gsd7.log` |
| §6 各行"内核"一栏的归属 | 各目录 `summary.txt` 的标题与第一节；`gsd-fixes-20260930/SUMMARY.md` 的"更正与后续" |

---

## 本章要点

- 新 920B 上，`259f1a25c` 的确认性验证全部通过：`verify_fixes.py` PASS 26 / FAIL 0 / SKIP 2，`verify_multitree.py` 13 项全过，漏页 0 / 30，四组压测通过且 dmesg 与串口零命中。
- 单棵 192 MiB 进程树的 restore，`total_ms` p50 为 83.3 ms，其中 CRIU 恢复 71.0、杀进程 11.1；overlay 切层不到 1 ms。restore 的耗时基本不随某一代的改动量变化，checkpoint 的耗时随改动量增长。
- 多棵树的耗时随树数近似线性增长；两棵以上时，先 dump 完的树要停着等后面的树，冻结时长与其余各棵的 dump 时间同量级。
- 各项性能表口径独立，10-16 的 v2 分档恢复随实际进程大小增长，结果见 §3.4；同名指标不能跨口径直接比较；两份本应相同的东西逐轮交错测出来会差 1 ms 上下，小于这个量级的差别不要解读。
- 结果文件里的路径与模板名是验证时的构建产物名，与运行时布局的对应在 §1.1；2026-10-08 验证时 gsd 的路径是显式传入的，默认路径在 2026-10-09 落位后另做了现场复核（§1.2），历史完整验收的判定值仍以 10-08 为准，新版分档数据见 §3.4。
- 八组压测、多种子对照、调试内核和其余探针是在前一个提交号上跑的：Go 代码相同、内核是同一个二进制，作为补充证据引用时要带上这句限定。
- 950 未部署、未测；注入开关关闭态的建模板、若干只有单测的失败分支、更大的规格、三棵以上的树、小时级运行、pause / resume 之后的行为都没有现场证据。
- 每个数字的来源文件在 §8；别的章需要判定值时链接到这里，不另写数字。
