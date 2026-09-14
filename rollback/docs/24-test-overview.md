# 24 · 测试体系总览

> 这套系统最危险的故障不会让任何断言变红：功能全对、内容逐字复现、脚本打印
> 一屏 `PASS`，只是**测的根本不是那个东西**。本篇先讲四种这样的静默失效，
> 再讲为此搭起来的三层测试、一张测试矩阵，以及数字要带什么条件才算数。
>
> **读者**：评审、客户、接手者。
> **预备**：[第 00 篇 · 总览](00-design-overview.md)、
> [第 17 篇 · 可观测性与验证](17-observability-and-verification.md)。
> **代码**：`e2b-infra/benchmark/`（交付态两个脚本）、
> `e2b-infra/rollback/test-950/`（开发态工具箱）

---

## 0. 本篇要回答的问题

1. 为什么「测试全过」在这套方案里不足以说明问题？四种静默失效各是什么样子？
2. 三层测试各管什么、为什么交付态脚本要零依赖、为什么两套脚本不能混用？
3. 要证的每一条性质，由哪个脚本、用什么判据、在哪台机器上证到了哪一步？
4. 一个性能数字要带哪些条件标签才允许被引用？哪两类数字不允许相减？

---

## 1. 为什么这套方案的测试要格外小心

普通的功能测试问的是「结果对不对」。这套方案的麻烦在于：**下面四种失效里有三种
不改变结果**。回滚回来的内容一个字节都没错，断言全部通过，坏掉的是成本、是覆盖范围、
或者是判据本身。

### 1.1 增量静默退化成全量

`FC_TRACK_DIRTY_PAGES` 没配、或者机器上探不到硬件标脏又没有显式打开，
每一次 checkpoint 就整份拷走 guest RAM。

这条路径**功能是完全正确的**：全量捕获当然能精确地恢复到那一刻，内存标记、blob 的
md5、根文件系统上的文件，一项都不会错。变的只有两件事 —— 一次 checkpoint 从几十毫秒
变成一秒半，每一代产物从几十 MB 变成整份内存。

普通测试抓不到，是因为它只看得见「回来的内容对不对」，看不见「用什么方式回来的」。
`rollback/test-950/README.md` 把这句话写在两套方案的共同前提里：

> 两套共同必须：`FC_TRACK_DIRTY_PAGES=true`。漏配则每次 checkpoint 都退化成全量，
> 测试照样"通过"但测的不是增量，是最容易漏掉的坑。

要让它现形，只能加一条**报告「怎么做的」而不是「做对没有」**的通道 ——
这就是 `memMode`（[第 17 篇 §2.1](17-observability-and-verification.md#21-memmode每次调用都回报)）。
判据一句话：**除了沙箱的第一个 checkpoint，出现 `full` 就是有问题**。

### 1.2 checkpoint 之后 pause，封层丢了

第二种更狠：它**真的丢数据**，而且丢得自洽。

checkpoint 会把当前写层封存、另开一层（[第 10 篇 §3](10-disk-layering.md#3-seal不搬运任何数据的换层)）。
于是一个被 pause 的沙箱要想完整地回来，必须把整个层栈压平再导出。这一步做错的后果，
`test-950` 那次补测试的提交（`173a3d5`）说得很清楚：

> checkpoint seals the write layer and opens a fresh one, so a paused sandbox has to
> flatten the whole stack to come back whole. Getting that wrong lost every write made
> before the last checkpoint and said nothing (the diff header is derived from what was
> exported, so it agreed with itself).

**「the diff header is derived from what was exported, so it agreed with itself」**
是这一条的全部要害：导出多少，header 就描述多少，自洽性检查永远通过。
沙箱能起来、能干活、文件都在，只是最后一次 checkpoint 之前写的东西没了。

为什么当时所有测试都抓不到：

| 那一层测试 | 为什么漏掉 |
|---|---|
| Go 单测 | 修复之前那条代码路径根本没有单测；修复时补的 `export_layers_test.go` 是**跟着修复一起进来的**，它证明修好了，不能证明当初能发现 |
| 所有验收脚本 | 一个都不做原生 `pause` —— 它们测的是 checkpoint/restore 自己那条链，pause 是**别人的**生命周期操作 |
| 补测试的第一版 | 读回文件比对，**假通过**了 |

最后一行才是真正的教训。`pause_verify.py` 的设计说明把它写在了脚本注释与提交信息里：

> The script writes 16 MiB of random data on each side of a checkpoint, pauses, resumes,
> and reads both back with O_DIRECT. O_DIRECT is the point: a small file read through the
> guest page cache hides the fault completely, which is why the first attempt at verifying
> that fix passed.

**guest 的页缓存会把磁盘层的问题完全盖住** —— 刚写过的 16 MiB 还在缓存里，
读回来当然对。绕开缓存（`O_DIRECT`）才是在读那块盘。
这条判据的展开在[第 25 篇 §4](25-functional-tests.md#4-pause_verifypy一个自洽的静默损坏)。

### 1.3 把读过的页当成脏页

第三种发生在**对照的那一边**，但它教的东西一样重要。

950 上跑原生快照的分档基准时，最值得看的一列不是时间，是产物大小
（`三套快照方案的负载构造与测量口径.md` §8）：名义脏内存从 0 MB 一路加到 192 MB，
导出的 memfile 一直在 400 MB 上下不动。

链条是这样的：原生 pause 路径向 Firecracker 要脏页位图，判据是「页驻留（mincore）
且 `/proc/self/pagemap` 第 57 位（userfaultfd 写保护位）是清的」—— 上游代码注释写的是
*present and write-protected bit cleared, indicating it was written to*。这个判据成立
有个前提：页被**读**进来的时候要带着写保护，只有真被写过写保护才会解除。上游正是
这么做的（缺页时 `UFFDIO_COPY` 带 `UFFDIO_COPY_MODE_WP`），而 ARM 适配补丁把那三行
注释掉了（`fbee6fcd1`，2026-06-08，"patch: all patch for arm64"）。

后果：任何被填进来的页，读进来的也好写进来的也好，写保护位都是清的，于是全部报成脏。
**「脏页集」塌缩成了「工作集」**（[第 7 篇 §5.2](07-dirty-page-tracking.md#52-判据因此塌缩)）。

普通测试抓不到，因为快照本身没错 —— 它多带了一些没必要带的页，恢复出来的状态完全正确，
只是每一代都按工作集导出。**只有把「名义档位」和「实测产物大小」并排放，这一列才说话。**

两点必须说清楚：这是 **ARM 适配版**原生路径上的判据，x86 上游本来是精确的；
本方案不用这个判据，脏位图来自 KVM 或 HDBSS 的写跟踪，**读不算脏**
（[第 7 篇 §5.4](07-dirty-page-tracking.md#54-本方案怎么绕开)、
[第 20 篇 §6.3](20-vs-native.md#63-e2b-的增量快照有一个与改动量无关的下限)）。
所以同一台机器上，我们的内存差分随档位线性增长，原生的不增长。
这条链的完整推导、三套脏页判据的并排，以及它为什么不影响本方案，在
[第 27 篇 §5](27-cross-implementation.md#5-一个真实发现读也被算成脏)；
实测那一列产物大小在[第 28 篇 §3.3](28-results-and-compliance.md#33-跨实现对照在-950-上的状态)。

### 1.4 判据本身静默失效

第四种是前三种的元问题：**防静默退化的那道判据，自己也可能静默地不在了。**

`checkpoint_verify.py` 读 `mem_mode` 的方式是 `getattr(ck, "mem_mode", None) or "?"`。
不是每一版 SDK 都有这个字段：交付的 SDK 覆盖层有（`install.py --check` 会专门自检它），
但 2026-09-08 那轮 920B 基准所在的环境里装的那版 `CheckpointInfo`
（`e2b/sandbox/checkpoint/types.py`）只有 `checkpoint_id / name / created_at` 三个字段
（`benchmark/checkpoint-bench-对比.md` §5.1），取值必然落空。
脚本的处理是**跳过这两条断言**：

```python
if "?" in mem_modes.values():
    print("\n  （服务端没有报 mem_mode 字段，跳过全量/增量的判定）")
else:
    check(mem_modes["A"] == "full", ...)
    check(all(mem_modes[g] == "incremental" for g in ("B", "C")), ...)
```

于是同一个脚本、同一套二进制，只因为换了一版 SDK，就从 **59 项全过**变成
**57 项全过** —— 两轮都打印「✓ 全部通过」，谁也不会去数。这正是 920B 09-04
那一轮存下来的对照：用我们的 SDK 跑是 `59/59`，用旧的 gsd SDK 跑是 `57/57`
（`reports/920b-kas0904-20260904/00-context.md`）。
`benchmark/checkpoint-bench-对比.md` §5.1 把结论写死了：

> 也就是说，防"增量被静默降级成全量"的那道判据**现在是形同虚设的**。

09-08 那一轮有旁证顶上（对照组脚本的内存产物列：树根 2048 MB = 整份 guest RAM，
最后一代只有 276.5 MB，增量确实生效了），但**旁证不是判据**。修法两条：
服务端把 `mem_mode` 带进响应并让 SDK 透出，或者把脚本判据换成
「本代内存产物是否接近 guest RAM 总量」—— 后者不依赖服务端改动。

由此得到一条测试纪律：**报告里必须记项数**。`59/59` 与 `57/57` 的差不是噪声，
是两条判据的生死。

### 1.5 共同点

| # | 现象 | 结果对吗 | 靠什么才看得见 |
|---|---|---|---|
| 1 | 增量退化成全量 | 对 | 服务端自报的 `mem_mode` |
| 2 | pause 丢封层 | **错，且自洽** | `O_DIRECT` 读回、跨 checkpoint 两侧各写一份 |
| 3 | 读被算成脏 | 对 | 名义档位 vs 实测产物大小 |
| 4 | 判据消失 | 对 | 记项数、记 SDK 版本 |

一句话：**测试通过 ≠ 测的是那个东西**。所以这套测试体系的重心不在断言数量，
在于每个数字都带着「它是怎么来的」一起出现 —— 这也是
[第 17 篇 §2](17-observability-and-verification.md#2-让静默退化现形)
那三道防线存在的理由。

---

## 2. 三层测试

### 2.1 单元测试：只说位置与规模

新增的 Go 单测共 **8 个 `_test.go`、约 1600 行**，分布在
`packages/orchestrator/internal/checkpoint/`（账本、位图、层栈、合并 header 的一致性）与
`internal/sandbox/block/`（封层、层栈等价、导出压平）两处，只在 `infra-arm` 的
`jll` / `jll-xfs` 分支上 —— **未随 MR !119 合入上游**（[第 30 篇 §1.1](30-extending.md#11-三个地方)）。它们**不进交付 patch**：rpmbuild 的 `%build` 只做 `go build`，
从不 `go test`，进了源树也一次都不会被编译。

它们守着哪些不变量、哪些不变量没有被守住，见
[第 15 篇 §8](15-state-and-concurrency.md#8-单元测试守着哪些不变量)与
[第 30 篇 §5.1](30-extending.md#51-测试)。本部分不再展开 —— 单测证的是**部件**，
本部分讲的是**整机**。

### 2.2 交付态验收：两个单文件脚本

`e2b-infra/benchmark/` 下的
[`checkpoint_verify.py`](../../benchmark/checkpoint_verify.py)（只证正确性）与
[`checkpoint_bench.py`](../../benchmark/checkpoint_bench.py)（只测耗时），
**各自单文件、零共享依赖**，拷到目标机上就能跑。

两个脚本各自带着一份一模一样的宿主探针，互相不引用。这不是没来得及重构：

> 这两个函数和 `checkpoint_bench.py` 里的是同一份。宁可重复也不 import：本目录的脚本
> 是一个个单独拷到目标机上跑的，跨文件依赖会在版本对不齐时**静默降级成"未知"**，
> 而不是报错——已经踩过一次了。

值得把这句话展开，因为它就是 §1 的逻辑用在测试代码自己身上：探针失败时
`fc_get()` 返回 `None`，脏页后端那一列就打印「未知」。**一个提取成公共模块、
只拷走一半的脚本，不会报 ImportError**（同目录下没有那个文件才会），它更可能的下场是
被人补一个空壳、或者被 `try/except` 兜住，然后所有条件标签集体变成「未知」——
数字照样打印，报告照样归档，只是再也说不清它们是在什么后端上跑出来的。
**重复三十行代码换掉一个静默降级，这笔交易是划算的。**

正确性与性能**分开跑**也是同一个考虑，脚本 docstring 里写着：
「正确性现场不给性能垫噪声，性能档位也不拖慢正确性。」

已知缺口：**这两个脚本只打屏、自己不落盘**。要留记录得靠 shell 接一份（§5.3）。

### 2.3 开发态工具箱

`e2b-infra/rollback/test-950/`（[目录说明](../test-950/README.md)）是我们自己排查用的一套：
`correctness.py`、`timing.py`、`loop.py`、`bench-ckpt.py`、`probe-ramp.py`、
`freeze_probe.py`、`pause_verify.py`、`compat_matrix.py`、`hdbss_evidence.py`，
共享 `lib.py`，外加四个 shell（宿主自检 / 造数据卷 / 两套之间切换 / 切换后冒烟），
`run-all.sh` 一条命令跑完一套并把报告落到 `reports/<方案>-<时间戳>/`。

它比交付态跑得细得多 —— 分位数、链深、冻结窗口、连打劣化、兼容矩阵 —— 代价是
要配路径、要传对参数、要整个目录一起拷。

### 2.4 为什么两套不混用

|  | 交付态 `benchmark/` | 开发态 `rollback/test-950/` |
|---|---|---|
| 给谁 | 交付方在目标机上做验收 | 我们自己跑穷举、找劣化、判 HDBSS 真假 |
| 依赖 | 单文件、零共享依赖 | 共享 `lib.py`，要配 store 路径 |
| 参数 | 全默认即可 | 位置参数，**顺序敏感** |
| 产出 | 只打屏 | 落 `reports/<方案>-<时间戳>/` |

`MANIFEST.md` 里那条警告是踩出来的：

> 参数顺序要注意：`correctness.py <方案> <df挂载点>`、`timing.py <方案> <df挂载点>`、
> `loop.py <方案> <次数>`、`bench-ckpt.py <方案> [--选项]`。`run-all.sh` 传的是对的；
> 自己手敲容易把挂载点当成方案名，那样 XFS 的产物会被拿 ext4 的规则去判，报假 FAIL。

注意失效方向：**假 FAIL**。开发态脚本按方案名切换判据（ext4 方案看逐代 `mem_diff`，
XFS 方案没有按代 `mem_diff`），传错了就用错的规则去判对的产物。假 FAIL 比假 PASS 好，
但仍然会浪费一整轮上机时间 —— 所以开发态一律走 `run-all.sh`，别手敲。

---

## 3. 测试矩阵

全书唯一一张。**现状列只记三态**（✅ 跑过并留有报告 / 待测 / 部分），
数字一律不在这里，在[第 28 篇](28-results-and-compliance.md)。

| 要证的性质 | 脚本 | 判据 | 950 | 920B |
|---|---|---|---|---|
| 活体：快照 / 恢复之后照常干活 | `checkpoint_verify.py` | 每次 create / restore 后命令能跑、文件能读写、能起新进程、老进程仍在推进 | ✅ | ✅ |
| 内存真的回到那一刻 | `checkpoint_verify.py` | `/dev/shm` 标记 + 128 MB blob 的 md5 逐字一致 | ✅ | ✅ |
| 文件真的回到那一刻 | `checkpoint_verify.py` | 根文件系统标记 + 32 MB 文件的 md5 | ✅ | ✅ |
| 根目录 / 删除 / 权限位也能回滚 | `checkpoint_verify.py` | 被删的 victim 复活、`/etc` 下文件复原、权限位是那一刻的值 | ✅ | ✅ |
| 进程复活（是搬内存不是重启） | `checkpoint_verify.py` | PID 与启动时刻不变 | ✅ | ✅ |
| 树语义：线性 / 前滚 / 分叉跨 LCA / 删除 / 失败 | `correctness.py` | 末行 `ALL PASS` | 待测 | ✅ |
| pause 之后数据完好 | `pause_verify.py` | 末行 `✓ 全部通过`；**必须 `O_DIRECT` 读回** | 待测 | ✅ |
| 不弄坏原生生命周期操作 | `compat_matrix.py` | 末行 `✓ 没有 BROKEN`；`REFUSED` 是边界不是故障 | 待测 | ✅ |
| HDBSS 三级证据 | `01-check-host.sh` / `04-verify-runtime.sh` / `hdbss_evidence.py` | L1 `cap 502: supported`、L2 自报 `hdbss`、L3 冷/热写耗时比接近 1 | L1/L2 ✅、**L3 待落盘** | ✅（负样本） |
| 稳定性 | `loop.py` | 200 次回滚 `failures: 0`，任何内容错误立即停 | 待测 | ✅ |
| 成本随脏页量走（O(脏页)）与 df 增量 | `timing.py` | 末尾 `ALL CORRECT`，逐代 df 增量与脏页量相称 | 待测 | ✅ |
| 恢复不随链深变慢 | `timing.py` | 链深 5 / 20 / 50：create 不随深度增长，restore 只跟要跨的纪元数走 | 待测 | ✅ |
| 分档分布（p50 / p99） | `bench-ckpt.py` | 末行 `BENCH OK`；各档 `mem_mode` 全 `incremental` | 待测 | ✅ |
| 冻结窗口 | `freeze_probe.py`（由 `bench-ckpt.py` 推进 guest） | 每档标「可区分 ✓」；标 ⚠ 说明被测量底噪淹没，数字不作数 | 待测 | ✅ |
| 持续连打 60 秒 | `bench-ckpt.py` | 前 1/3 与后 1/3 对比，劣化倍数如实记录 | 待测 | ✅ |
| 连打劣化归因（链深还是写入量） | `probe-ramp.py` | 两种归一化哪种收拢；判定：跟代数走 | 待测 | ✅ |
| 分档扫描 + 时机成本 + 产物实占 | `checkpoint_bench.py` | 每一跳落到目标代；「写完立刻拍」− 「冲干净再拍」= 时机成本 | 待测 | ✅ |
| 三套方案横向对照 | `checkpoint_bench_v2.py`、`native_snapshot_bench.py` | 同一张档位表、同样的负载造法；只并列不作差 | `native_snapshot_bench.py` ✅、`checkpoint_bench_v2.py` 待测 | `checkpoint_bench_v2.py` ✅（与进程级并列）、原生待测 |

> **950 = 鲲鹏 950，HDBSS 硬件标脏，产物落根盘 ext4（真盘）；920B = 鲲鹏 920，
> 无 HDBSS 走 KVM 软件写保护，产物视轮次落调优过的 loop 卷或根盘 XFS（真盘）。**
> 两台机器的差异表见[第 28 篇 §1](28-results-and-compliance.md#1-条件标签)，
> 平台差异见[第 19 篇 §4](19-kunpeng-platform.md#4-两台机器的实测对照)。

矩阵里「待测」占了整整一列，这是当前状态的如实记录：**950 上已经证到的是正确性
（含硬件标脏路径）与原生对照，尚未证的是分档性能与稳定性**。
要点在于：正确性结论可以从 920B 搬到 950（两种脏页后端喂给上层的是
同一张位图，语义一致，只差性能，见[第 19 篇 §4.3](19-kunpeng-platform.md#43-920b-上能验什么)），
**性能数字不可以搬**。

---

## 4. 测量纪律

### 4.1 三个时钟对三种数字

[第 17 篇 §1](17-observability-and-verification.md#1-三个时钟) 定义了三个钟：
客户端墙钟、冻结窗口、宿主分阶段。落到测试上是三种不同用途的数字：

| 数字 | 谁产生 | 干什么用 |
|---|---|---|
| 客户端墙钟 | 脚本自己掐表 | **判定用它**：含网络往返与服务端全部工作，最保守，客户最容易复测 |
| 冻结窗口 | `freeze_probe.py` 在 guest 里 2.5 kHz 采样，看时间序列里的「洞」 | 解释业务感受到的停顿，**不作判定** |
| 宿主分阶段 | 服务端 `timings.json` | 回答「时间去哪了」，不回答「差多少」 |

判定口径与它的完整定义在[第 26 篇 §1](26-performance-methodology.md#1-指标口径)，
判定结果在[第 28 篇 §5](28-results-and-compliance.md#5-对照客户指标的判定表)。

### 4.2 一个数字要带的条件标签

任何一个耗时或体积数字，不带下面这些就不允许被引用：

| 标签 | 为什么 | 从哪读 |
|---|---|---|
| 机型 | 950 / 920B 的 CPU 与内核都不同 | 人填 |
| 脏页后端 | `hdbss` / `kvm-wp` / `off`，同样的代码能差好几倍 | 问沙箱的 Firecracker API 套接字 |
| 产物文件系统与介质 | 真盘 vs loop 卷，ext4 vs XFS | 读 orchestrator **进程的 environ**，不读配置文件 —— 配置只说「本该是什么」 |
| 模板与内存大小 | 全量代价按 guest RAM 走 | 脚本参数 |
| 二进制 sha | 换过二进制的机器上，这是唯一能对上号的东西 | `sha256sum` |
| 脚本与参数 | 默认值改过就不是同一组数 | 命令行原样抄 |

前三项脚本自己会打印，后三项要人填进 `00-context.md`（§5.3）。

### 4.3 先自证「这个检查会失败」

一个永远返回真的断言比没有断言更糟 —— 它让人以为测过了。所以
`checkpoint_verify.py` 的第一步不是验恢复，是验**三代现场彼此不同**：

三代 A / B / C 换掉全部标记，另外各自动一样「不好回滚」的东西 ——
A 建 victim、B 删掉它、C 保持删掉（回到 A 必须让它复活），权限位每代不同
（回滚必须连元数据一起回）。9 项现场先证有区分度，11 项现场再证恢复后逐项一致。

同样的道理用在别处：`hdbss_evidence.py` 的 L3 判据先在 920B 上取到**负样本**
（没有 HDBSS，冷/热 = 4.5~5.4，正是软件写保护的特征），这个判据才有资格拿到 950 上用
（[第 25 篇 §6](25-functional-tests.md#6-hdbss_evidencepy能力不等于数据面)）。

### 4.4 空着比错的数强

XFS 方案没有按代的 `mem_diff`，那一格就空着；宿主机之外跑，脏页后端那一列就是「未知」；
`timings.json` 读不到就打 `-`。**不猜、不折算、不用「大约」填格子。**
一个错的数会被引用很多年，一个空格只会被人问一句。

### 4.5 不同条件的数不相减

950 与 920B 的模板大小、存储介质、脏页后端**三项全不同**，
差值不能归因于其中任何一项 —— 尤其不能归因于 HDBSS。
同理，950 那一轮跑的是 `checkpoint_verify.py`，它的定位是正确性验收，
耗时只是顺带打印、样本少、每代现场又大（128 MB 内存 + 32 MB 文件），
**只当量级看，不构成性能基准**。

> 这类口径说明必须和数字放在一起。分开放，数字一定会被单独引用。

三套方案横向对照时这条纪律更严：快照范围不同，有些列只能并列、不能作差
（[第 27 篇 §4](27-cross-implementation.md#4-恢复语义不同所以有些列不能作差)）。

---

## 5. 环境与产物

### 5.1 跑起来需要什么

两个交付态脚本的依赖完全一样，写在各自 docstring 里：

> 依赖（与本目录其它脚本一致）:
>     `pip install e2b==2.20.0 python-dotenv`
>     `python /opt/e2b-infra/patch_e2b.py`
>     `python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py`   # checkpoint/restore 的 SDK 覆盖层
>
> 环境变量（可放在当前目录 .env 里，用 sync-env.sh 同步）:
>     `E2B_API_KEY` / `E2B_DOMAIN` / `E2B_API_URL` / `E2B_HTTP_SSL`

三点要注意：

1. **SDK 覆盖层是版本锁死的**，只对 `e2b==2.20.0` 有效，装之前核版本，
   对不上就停 —— 「不会悄悄装出一个半吊子」。装完用干净子进程自检
   （`Sandbox.checkpoint` 属性、`CheckpointInfo.mem_mode`、proto 里的 `mem_mode`、
   六个方法、端口 49984）。**`mem_mode` 在不在，决定了 §1.4 那两条判据在不在。**
2. **覆盖层要排在 `patch_e2b.py` 之前**，顺序反了会被 https→http 的全局替换盖回去。
3. 装在 conda 环境里是正常路径；装进已经被改过元数据的系统 python 才需要 `--force`。

具体命令与期望输出在[第 29 篇 §1](29-acceptance-runbook.md#1-前置条件)。

### 5.2 宿主机 vs 非宿主机

脚本能不能量到「跑在什么上」，取决于它跑在哪台机器上：

| 列 | 在宿主机上跑 | 从别的机器连过来 |
|---|---|---|
| 脏页后端（`hdbss` / `kvm-wp` / `off`） | 问沙箱的 Firecracker API 套接字 | **「未知」**（套接字是本地文件） |
| 产物路径与文件系统 | 读 orchestrator 进程的 environ + `/proc/mounts` | **空** |
| 每代内存 / 文件产物实占 | 量 `mem_diff` 与新封层的 `st_blocks` | **量不到** |
| 宿主分阶段 | 读 `timings.json` | 量不到 |
| 客户端墙钟、全部正确性断言 | ✅ | ✅ |

结论：**正确性可以远程验收，性能必须上宿主机**。远程跑出来的耗时数字缺条件标签，
按 §4.2 就不该被引用。

### 5.3 报告怎么存

开发态脚本自己落盘（`reports/<方案>-<时间戳>/`，`run-all.sh` 顺带写一份 `summary.txt`）。
交付态脚本不落盘，**用 shell 接**：

```bash
python checkpoint_verify.py 2>&1 | tee reports/<机器>-verify-<日期>.log
```

这条约定是 950 那一轮补出来的 —— 那份记录是从终端 scrollback 手工抄回来的，
`00-context.md` 里第三条注意就是「下次上机用 shell 接一份，别再靠 scrollback」。

每一轮实测在 `reports/<机器>-<内容>-<日期>/` 下留一个目录，
里面**必须**有一份 `00-context.md`：被测对象（二进制来源与 sha）、机器、时间、
命令与参数、脏页后端、产物落盘路径与文件系统、沙箱 id、结果表、以及「这份数不能拿来干什么」。
两个范本：`reports/950-verify-20260829/00-context.md`（交付态一轮）与
`reports/920b-kas0904-20260904/00-context.md`（开发态全套 + 两个遗留问题的定位过程）。
必填字段表在[第 29 篇 §5](29-acceptance-runbook.md#5-结果回传规范)。

---

## 6. 小结

1. 这套方案里**测试通过 ≠ 测的是那个东西**。四种静默失效：增量退化成全量、
   pause 丢封层（自洽所以无人报错）、把读过的页当脏页、以及**判据本身消失**。
2. 只有第二种会改变结果，其余三种**结果全对、代价或覆盖范围错**。
   要抓它们，测试必须多问一句「这是怎么做到的」，而不只问「做对了没有」。
3. `mem_mode` 是防第一种的主力判据；它依赖 SDK 透出该字段，
   **换一版 SDK 就会从 59 项静默变成 57 项**。报告里必须记项数与 SDK 版本。
4. 三层测试：单元测试证部件（不进交付 patch，见第 30 篇）、
   交付态两个单文件脚本证整机、开发态工具箱做穷举与归因。
5. 交付态**故意重复**宿主探针：拷走一半的公共模块不会报错，只会让条件标签集体变成「未知」。
6. 两套脚本不混用。开发态位置参数顺序敏感，传错会用错的规则判对的产物、报假 FAIL。
7. 矩阵现状：950 上正确性（含硬件标脏路径）与原生对照已证，**分档性能整列待测**；
   920B 全套跑过但没有 HDBSS。**正确性结论可以搬，性能数字不可以搬。**
8. 测量纪律五条：三个时钟各司其职、每个数字带条件标签、先自证会失败、
   空着比错的数强、不同条件的数不相减。

---

## 延伸阅读

| 想知道 | 去哪 |
|---|---|
| 三个时钟与退化上报的实现 | [第 17 篇](17-observability-and-verification.md) |
| 每条正确性判据怎么设计的 | [第 25 篇](25-functional-tests.md) |
| 性能指标口径与负载怎么造 | [第 26 篇](26-performance-methodology.md) |
| 三套方案怎么并排比 | [第 27 篇](27-cross-implementation.md) |
| 实测数字与达标判定 | [第 28 篇](28-results-and-compliance.md) |
| 上机怎么操作 | [第 29 篇](29-acceptance-runbook.md) |
| 开发态工具箱的完整说明 | [`../test-950/README.md`](../test-950/README.md)、[`MANIFEST.md`](../test-950/MANIFEST.md) |

**下一篇**：[25 · 功能正确性测试](25-functional-tests.md) —— 「回到那一刻」怎么被证明，
以及每个脚本各自守住哪一条。
