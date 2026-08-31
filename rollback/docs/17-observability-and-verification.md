# 17 · 可观测性与验证

> 这套系统最危险的故障不是崩溃，是**「看起来正常」** —— 功能全对、测试全过、
> 只是每次多拷两个 GiB，或者某几页内存的内容属于另一个时刻。
> 本篇讲怎么让这类问题现形：三个时钟、退化上报、以及两个验收脚本的设计原则。
>
> **读者**：工程师、系统工程师、要做验收的人。
> **预备**：[第 13 篇 · 端到端](13-end-to-end.md)、[第 14 篇 · 失败语义](14-failure-semantics.md)。
> **代码**：`internal/sandbox/phasetimings.go`、`internal/checkpoint/metrics.go`、
> `e2b-infra/benchmark/checkpoint_{verify,bench}.py`

---

## 0. 本篇要回答的问题

1. 「三个时钟」分别测什么？为什么需要三个？
2. 哪些退化是完全静默的？怎么把它们变成可观测的信号？
3. 正确性验收脚本要怎么设计，才能证明「回滚回来的确实是那个时刻」？
4. 为什么恢复之后**不能**用时间做活性判据？
5. 当前实测到哪一步，还差什么？

---

## 1. 三个时钟

一次 SDK 调用花了 300 ms。这个数字本身回答不了任何问题 —— 它是网络慢？服务端慢？
还是虚机被冻结了 300 ms？所以有三个不同的钟：

| 钟 | 测什么 | 谁能测 | 回答什么 |
|---|---|---|---|
| **客户端墙钟** | SDK 发出请求到拿到回应 | 调用方 | 「一次调用要等多久」 |
| **冻结窗口** | 虚机暂停到恢复 | 服务端（`timings["frozen"]`） | 「业务实际感受到的停顿」 |
| **宿主分阶段** | 每一步各花了多久 | 服务端（`timings.json`） | **「时间去哪了」** |

前两个说「差了多少」，第三个说「差在哪」。

```
├──────────────── 客户端墙钟 ────────────────┤
       ├────── 冻结窗口 ──────┤
   │pause│snapshot│ seal │resume│
              └── fc_validate / fc_memory / fc_vcpus / fc_gic / fc_devices
                  （Firecracker 自报，微秒精度）
```

### 1.1 `PhaseTimings`

```go
type PhaseTimings map[string]float64      // 毫秒

func (t PhaseTimings) Mark(name string, start time.Time)     // orchestrator 自己计时
func (t PhaseTimings) SetUs(name string, us uint64)          // Firecracker 回报的微秒
func (t PhaseTimings) Timed(name string, fn func() error) error  // 包住一次调用
```

两个设计细节：

**`nil` 安全。** 每个方法都以 `if t == nil { return }` 开头。不需要分解的调用方传 `nil`，
一分钱不花 —— 所以计时可以直接加在热路径上，不必条件编译。

**`Timed` 存在的理由**：几个被计时的调用位于错误分支里，拆成
「记开始时间 / 调用 / 记结束时间」会把错误处理搅乱：

```go
if err := timings.Timed("pause", func() error { return process.Pause(ctx) }); err != nil {
    return false, nil, fmt.Errorf("failed to pause VM: %w", err)
}
```

### 1.2 为什么写文件而不只是打日志

```go
// It goes in a file rather than only into the log because that is what a
// benchmark can read: the client wall clock and the guest freeze window are
// both measurable from outside, but where the time went inside the host is
// not, and scraping it back out of log lines is a worse contract than a small
// JSON file.
func writeTimings(path string, t sandbox.PhaseTimings)
```

`timings.json` 落在 checkpoint 目录里，`last-restore-timings.json` 落在沙箱目录里
（restore 会重复发生，只保留最近一次；checkpoint 的 timings 写一次就不动了）。

写失败**不算 checkpoint 失败** —— `_ = os.WriteFile(...)`。可观测性不应该成为
功能失败的来源。

---

## 2. 让静默退化现形

[第 14 篇 §9](14-failure-semantics.md#9-不变量清单) 列的 14 条不变量里有 9 条被破坏后是静默的。
除此之外还有几类「功能正确但代价错了」的退化。三道防线：

### 2.1 `memMode`：每次调用都回报

```protobuf
// A "full" on anything but a sandbox's first checkpoint means the host is
// not tracking dirty pages -- which costs time and space and is otherwise
// completely silent. This field is how a caller can tell.
string mem_mode = 2;
```

判据很简单：**除了沙箱的第一个 checkpoint，出现 `full` 就是有问题**。

它同时进 metrics 的属性：

```go
stats.set("mem_mode", memMode)
// …
attrs = append(attrs, attribute.String("operation", operation),
                      attribute.Bool("success", err == nil))
attrs = append(attrs, stats.attrs...)
checkpointCalls.Add(ctx, 1, set)
checkpointDuration.Record(ctx, time.Since(start).Milliseconds(), set)
```

`callStats` 的注释把这个动机说得很直白：

> It exists for the degradation paths. A checkpoint that had to capture full
> memory, or a clone that fell back to copying the base byte by byte, costs
> orders of magnitude more than the intended path while still succeeding —
> exactly the kind of regression that hides in a latency average until
> somebody reads the logs. Reporting it as an attribute makes it answerable
> from a dashboard.

**「藏在延迟均值里，直到有人去读日志」** —— 这就是要防的东西。

### 2.2 启动时的能力上报

一次性、开门见山地说清楚这台机器能做什么：

```go
fields := []zap.Field{
    zap.String("store", storeDir),
    zap.Bool("track_dirty_pages", fc.TrackDirtyPagesEnabled()),
    zap.String("track_dirty_pages_reason", fc.TrackDirtyPagesReason()),
}
logger.L().Info(ctx, "checkpoint capabilities", fields...)

if !fc.TrackDirtyPagesEnabled() {
    logger.L().Warn(ctx, "dirty page tracking is off: every checkpoint will copy all of guest memory. "+
        "Set FC_TRACK_DIRTY_PAGES=true to force it on.", fields...)
}
```

注意 `track_dirty_pages_reason` —— 不只报结论，还报**为什么**：

- `"hardware dirty state tracking present (KVM capability 502)"`
- `"no hardware dirty state tracking; software tracking costs a VM exit per clean page"`
- `"FC_TRACK_DIRTY_PAGES=\"true\""`

XFS 方案还多探一项 reflink，没有时打 **Error** 级日志并给出修复建议
（[第 18 篇](18-ext4-vs-xfs.md)）。

Firecracker 侧对称地在 instance info 里报脏页后端：`"hdbss"` / `"kvm-wp"` / `"off"`
（[第 7 篇 §4.5](07-dirty-page-tracking.md#45-能力上报)）。

### 2.3 Firecracker 自报的分段与计数

```rust
pub struct RollbackResponse {
    pub restored_pages: u64,
    pub restored_bytes: u64,
    pub timings_us: RollbackTimings,
}
```

`restored_pages` 是「本次实际回退了多少页」的直接度量。它可以和 orchestrator
算出的回滚集大小对照 —— **两者不一致就说明契约出了问题**
（[第 9 篇 §4.1](09-firecracker-api-contract.md#41-为什么非有不可)）。

### 2.4 罕见故障的现场

有些问题只有一次现场，沙箱一销毁就什么都不剩。两处专门为此加的日志：

| 日志 | 为什么 |
|---|---|
| 回滚后逐设备打印队列生产/消费位置与 RX 缓存状态 | 挂死类问题（[第 12 篇 §7](12-rollback-pitfalls.md#7-排查手段)） |
| envd 不应答时打印带完整耗时的 Error | 客户端可能已经走了，服务端得留下痕迹（[第 14 篇 §3.1](14-failure-semantics.md#31-envd-超时45-秒不是随便定的)） |

---

## 3. 验收脚本

`e2b-infra/benchmark/` 下两个**零共享依赖的单文件脚本**，供交付方在目标机上直接运行。

### 3.1 为什么故意重复代码

两个脚本各自带一份宿主探针，互相不引用。这是有意的：

> 防止只拷走一半，剩下的静默变成 `unknown`。

同样地，**交付态脚本与开发态工具箱（`rollback/test-950/`）不要混用** ——
后者脚本之间有共享模块、需要配路径，是给我们自己排查用的。

### 3.2 `checkpoint_verify.py`：只证正确性

要证的是一句话：**回滚之后，沙箱还是回滚那一刻的那个沙箱，而且还能接着干活。**

拆成四类断言：

| # | 断言 | 怎么做 |
|---|---|---|
| 一 | 快照 / 恢复之后沙箱照常干活 | 每次快照后、每次恢复后都跑活体检查：命令能跑、文件能读写、能起新进程、快照前的后台进程还在推进 |
| 二 | 内存和文件都真的回到目标时刻 | 内存看 `/dev/shm` 标记 + 上百 MB blob 的 md5；文件看根文件系统标记 + 几十 MB 文件的 md5 |
| 三 | 对**根目录**的操作也能回滚 | 在 `/` 下建目录建文件、改 `/etc` 下的文件、**删文件**、**改权限位** |
| 四 | API 调用计时 | 每次 create / restore 掐表，末尾给次数 / p50 / min / max |

三条设计原则值得单独说：

**① 先自证「这个检查会失败」。** 三代现场必须彼此不同 ——
如果三代的观测点本来就一样，那「恢复后一致」这个断言毫无意义。
脚本先验证有区分度，再验证恢复。

**② 删除和改属性能回滚，才说明是块级快照。**
新建文件能回滚不稀奇（补一个删除动作就行）；**把删掉的文件变回来**、
**把改过的权限位改回去**，只有整块磁盘视图被换掉才做得到。

**③ 最硬的证据：进程带着 PID 和启动时刻复活。**
快照前起的后台进程，恢复后 PID 和启动时刻都不变 ——
说明是**内存被搬回去了**，不是虚机重启了、更不是重放了什么操作。

### 3.3 为什么活性判据不能用时间

这是[第 12 篇 §5](12-rollback-pitfalls.md#5-时间)的直接后果，写进了脚本注释：

> 「还在往前跑」只断言在推进，不断言速率：恢复后头一两秒那个 0.2 秒一拍的循环偶尔会慢下来，
> 而恢复本身把 guest 的单调时钟也拨回了快照那一刻，所以**时间和节奏都不能拿来当活体判据**。

- **墙钟**：恢复瞬间被 envd 拨回，中间有跳变；
- **单调时钟**：被真的拨回了，任何基于它的速率计算都会得到荒谬的值。

所以只断言**计数器在增长**。

### 3.4 `checkpoint_bench.py`：分档扫描

设计上有三处值得学：

**① 负载按真实用法造，不做人为隔离。** 每档总改动量按 3:1 拆成内存和文件两份
（沙箱里跑代码的真实负载通常改内存远多于改文件）；文件那份就是**普通缓冲写 + sync**，
不用 `O_DIRECT`：

> 真实用户怎么写我们就怎么写：数据先进 guest 页缓存再刷到虚拟盘，
> 所以一份文件写会**同时**出现在内存增量（页缓存那些页）和文件增量（落盘的块）里。
> 这不是测量误差，是页缓存的本性，实测两列会把它如实摆出来。

**② 同一套档位拍两遍，差值本身是一个数。**

| 时机 | 含义 |
|---|---|
| ① 写完**立刻**拍 | 最坏情况上界：刚写的数据正被内核往盘上刷，会和写 `mem_diff` 抢磁盘；NBD 管道里还有在途数据 |
| ② 写完 `sync` + 歇 1 秒再拍 | 日常口径：「写完跑了一会儿才拍」 |

**① − ② 就是「时机成本」本身** —— 它不是快照机制的成本，把它分离出来才能公平比较。

**③ 直接量服务端产物，不靠外部推断。** 每档的实测内存 / 文件增量取的是
`mem_diff` 文件的**磁盘实占**（稀疏文件，实占 = 这一代的脏页）和新封存层文件的实占。
只有在宿主机上跑才量得到。

> XFS 方案没有按代的 `mem_diff`，那一格就**空着** ——
> 「空着比给一个错的数强」。

**restore 单独成表**：从 gN 回到 gN-1 的代价归到上一行，因为它衡量的是那一跳的跨度。

---

## 4. 当前实测状态

### 4.1 950 · HDBSS 硬件标脏（2026-08-29）

跑通 `checkpoint_verify.py`，**59 项校验全部通过**。这是本方案第一次在**硬件标脏**路径上
得到验证 —— 服务端自报的脏页后端为 `HDBSS（硬件标脏）`，Firecracker 1.12.1，
产物落在根盘 ext4 的 `/orchestrator/build/checkpoints`。

| 场景 | 结果 |
|---|---|
| 三代现场（各换掉内存标记、128 MB blob、根文件系统标记、32 MB 文件、`/etc` 下文件、权限位，并删掉一个文件） | 9 项现场在三代之间确实不同，**校验有区分度** |
| 逐级回退 C → B → A | 11 项现场逐项一致 |
| 跨 2 代前滚 A → C | 11 项现场逐项一致 |
| A / C 交替 3 轮 | 每轮全量校验，无状态泄漏或累积误差 |
| `kill -9` 掉快照前就在跑的心跳进程后回滚到 A | **进程连同 PID 与启动时刻一起复活** |
| 树根是全量、后续是增量 | `mem_mode` 分别为 `full` / `incremental` / `incremental` |

客户端墙钟（含网络往返与服务端全部工作；模板 `base`，每代脏内存 128 MB +
根文件系统写入 32 MB）：

| 操作 | 次数 | p50 | min | max |
|---|---|---|---|---|
| create 全量（第一代） | 1 | 0.283 s | 0.283 s | 0.283 s |
| create 增量 | 2 | 0.113 s | 0.111 s | 0.115 s |
| restore | 10 | 0.100 s | 0.089 s | 0.153 s |

### 4.2 920B · 软件写保护（对照组）

920B **没有 HDBSS 硬件能力**，以下数字来自 KVM 软件写保护路径，
数据盘是调优过的 loop-ext4 卷（`/mnt/ext4dev`）：

- 正确性：三代内容逐字校验全部通过；
- **链深 5 / 20 / 50 代：全部正确，且恢复耗时无随链深增长的趋势**
  （[第 8 篇 §7.1](08-memory-diff-tree.md#71-恢复为什么不随链深变慢)的实测印证）；
- 稳定性：200 次回滚 0 失败；
- 分档基准（创建 p50，2 GB 沙箱）：全量 **1.549 s**；增量 0 MB **0.022 s**、
  64 MB **0.062 s**、256 MB **0.183 s**；
- 恢复：常见档位在几十毫秒量级。

「增量 0 MB = 0.022 s」这一档最能说明成本模型：**什么都没改时，checkpoint 几乎免费。**

### 4.3 两组数字不能直接相减

950 与 920B 这两组的**模板大小、存储介质、脏页后端三项全都不同**
（真盘 vs loop 卷，硬件标脏 vs 软件写保护），因此**不能把差值归因于 HDBSS**。

而且 950 那一轮跑的是 `checkpoint_verify.py`，它的定位是**正确性验收**，
耗时只是顺带打印，样本少、每代现场又大，只当量级看，不构成性能基准。

> 这类口径说明必须和数字放在一起。分开放，数字一定会被单独引用。

### 4.4 尚未覆盖

| # | 缺口 | 影响 |
|---|---|---|
| 1 | **950 上的分档基准** —— `checkpoint_bench.py` 还没在 950 上跑过 | 内存 / 文件系统 / 混合三段、各脏页档位、服务端分段耗时都还没有数据 |
| 2 | **HDBSS 与软件写保护的对照** | 硬件标脏的收益尚未量化 |
| 3 | **`FC_HDBSS_ORDER` 取 1 / 2 / 4 的对比** | 写密集负载下 buffer 溢出会吃掉多少收益，不知道（[第 7 篇 §6.2](07-dirty-page-tracking.md#62-hdbss-的-buffer-与溢出)） |
| 4 | 完整 `rpmbuild` 流程尚未跑通 | 交付形态未端到端验证 |
| 5 | 跨树回滚没有测试覆盖 | [第 8 篇 §4.5](08-memory-diff-tree.md#45-最近公共祖先怎么求) |
| 6 | 并发操作同一沙箱没有针对性的竞态测试 | 依赖 `opLocks`（[第 15 篇 §8](15-state-and-concurrency.md#8-单元测试守着哪些不变量)） |

---

## 5. 小结

1. **三个时钟**：客户端墙钟（等多久）、冻结窗口（业务感受到的停顿）、
   宿主分阶段（时间去哪了）。前两个说差多少，第三个说差在哪。
2. `PhaseTimings` 是 `nil` 安全的 map，所以计时能直接加在热路径上；
   写进 JSON 文件而不只是日志，因为**基准脚本要读它**。
3. **`memMode` 是抓静默退化的主力**：除了第一个 checkpoint，出现 `full` 就是有问题。
   它同时进日志和 metrics 属性 —— 让这个问题能从仪表盘上被回答。
4. 能力上报**不只报结论，还报理由**。一个悄悄丢了增量的部署，应该能从日志里读出原因。
5. 正确性验收的三条原则：**先自证检查会失败**、**验删除和权限位**（才说明是块级）、
   **进程带 PID 复活**（才说明是内存搬回去了）。
6. **活性判据不能用时间** —— 单调时钟真的被拨回了。只断言计数器在推进。
7. 性能基准的三条原则：**按真实用法造负载**（不用 `O_DIRECT`）、
   **两个时机各拍一遍**（差值是时机成本）、**直接量服务端产物**（不靠外部推断）。
8. 口径说明必须和数字放在一起 —— 否则数字一定会被单独引用。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 三个时钟 | `internal/sandbox/phasetimings.go` — `PhaseTimings`、`Mark`、`SetUs`、`Timed` |
| 落文件 | `internal/checkpoint/service.go` — `writeTimings`、`lastRestoreTimingsName` |
| metrics 与退化属性 | `internal/checkpoint/metrics.go` — `callStats`、`record` |
| 启动时能力上报 | `packages/orchestrator/main.go` — `reportCheckpointCapabilities` |
| 脏页后端上报 | `src/vmm/src/vstate/vm.rs` — `DirtyTrackingBackend::as_str` |
| Firecracker 分段与计数 | `src/vmm/src/vmm_config/snapshot.rs` — `RollbackTimings`、`RollbackResponse` |
| 队列诊断 | `src/vmm/src/rollback.rs` — `log_queue_diagnostics` |
| 交付态验收脚本 | `e2b-infra/benchmark/checkpoint_verify.py`、`checkpoint_bench.py` |
| 开发态工具箱 | `e2b-infra/rollback/test-950/` |

**下一部分**：[18 · 两套方案：ext4 与 XFS](18-ext4-vs-xfs.md) ——
机制和保障都讲完了，接下来是「在什么样的机器上、用哪一套」。
