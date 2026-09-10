# 17 · 可观测性与验证

> 这套系统最危险的故障不是崩溃，是**「看起来正常」** —— 功能全对、测试全过、
> 只是每次多拷两个 GiB，或者某几页内存的内容属于另一个时刻。
> 本篇讲**系统自己**怎么让这类问题现形：三个时钟、退化上报，以及两个交付态验收脚本
> 各自回答什么。测试体系与实测数据分别在[第 21 篇](21-test-overview.md)与
> [第 25 篇](25-results-and-compliance.md)。
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
5. 交付态两个脚本各回答什么？为什么它们故意重复代码、又故意跟开发态工具箱分开？

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

### 3.1 交付态：两个零依赖的单文件脚本

`e2b-infra/benchmark/` 下两个脚本，供交付方在目标机上直接运行：
[`checkpoint_verify.py`](../../benchmark/checkpoint_verify.py) 只回答「回滚回来的是不是那一刻」，
[`checkpoint_bench.py`](../../benchmark/checkpoint_bench.py) 只回答「一次要多久、产物有多大」。
两件事分开跑，正确性现场不给性能垫噪声，性能档位也不拖慢正确性。

**各自单文件、零共享依赖**，而且两个脚本各带一份一模一样的宿主探针，互相不引用 ——
这不是没来得及重构。它们是一个个单独拷到目标机上跑的，跨文件依赖在版本对不齐时
**静默降级成「未知」**而不是报错：脚本照跑、数字照打印，只是再也说不清那组数是在什么
脏页后端、什么文件系统上跑出来的。这就是[§2](#2-让静默退化现形)那条逻辑用在测试代码自己身上
（展开见[第 21 篇 §2.2](21-test-overview.md#22-交付态验收两个单文件脚本)）。

同样地，交付态脚本与开发态工具箱（[`../test-950/`](../test-950/)）**不要混用** ——
后者脚本之间有共享模块、位置参数顺序敏感，是给我们自己排查用的。

三层测试各管什么见[第 21 篇 §2](21-test-overview.md#2-三层测试)；
两个脚本的判据怎么设计的，正确性在[第 22 篇 §2](22-functional-tests.md#2-checkpoint_verifypy一条直链上的-59-项)、
性能在[第 23 篇 §3](23-performance-methodology.md#3-checkpoint_benchpy时机成本与直接量产物)。

### 3.2 `checkpoint_verify.py`：只证正确性

要证的是一句话：**回滚之后，沙箱还是回滚那一刻的那个沙箱，而且还能接着干活。**
拆成四类断言：活体（命令能跑、文件能读写、能起新进程、快照前的后台进程还在推进）、
内容（内存看 `/dev/shm` 标记 + 上百 MB blob 的 md5，文件看根文件系统标记 + 几十 MB 文件的 md5）、
根目录操作（建、改 `/etc`、**删文件**、**改权限位**）、以及每次调用的墙钟。

三条设计原则值得单独记住：

- **先自证「这个检查会失败」。** 三代现场必须彼此不同 —— 如果三代的观测点本来就一样，
  「恢复后一致」这个断言毫无意义。脚本先验证有区分度，再验证恢复。
- **删除和改属性能回滚，才说明是块级快照。** 新建文件能回滚不稀奇（补一个删除动作就行）；
  把删掉的文件变回来、把改过的权限位改回去，只有整块磁盘视图被换掉才做得到。
- **最硬的证据：进程带着 PID 和启动时刻复活。** 说明是内存被搬回去了，
  不是虚机重启了、更不是重放了什么操作。

59 项断言逐项怎么构成、`mem_mode` 那两条依赖哪一版 SDK、怎么读输出，见
[第 22 篇 §2](22-functional-tests.md#2-checkpoint_verifypy一条直链上的-59-项)。

### 3.3 为什么活性判据不能用时间

这是[第 12 篇 §5](12-rollback-pitfalls.md#5-时间)的直接后果，写进了脚本注释：

> 「还在往前跑」只断言在推进，不断言速率：恢复后头一两秒那个 0.2 秒一拍的循环偶尔会慢下来，
> 而恢复本身把 guest 的单调时钟也拨回了快照那一刻，所以**时间和节奏都不能拿来当活体判据**。

**墙钟**在恢复瞬间被 envd 拨回，中间有跳变；**单调时钟**被真的拨回了，
任何基于它的速率计算都会得到荒谬的值。所以只断言**计数器在增长**
（[第 22 篇 §2.3](22-functional-tests.md#23-活体判据为什么不能用时间)）。

---

## 4. 当前实测状态

**实测数据不在本篇。** 全书的实测数字、条件标签、对照客户指标的逐档判定、
以及尚未覆盖的缺口，只在[第 25 篇 · 实测结果与达标判定](25-results-and-compliance.md)
一处维护 —— 分散在多篇里的数字一定会走样，也一定会被脱离条件单独引用。

| 你想查 | 去哪 |
|---|---|
| 950 / 920B 的功能正确性结果（含每轮的 SDK 与判据是否有效） | [第 25 篇 §2](25-results-and-compliance.md#2-功能正确性结果) |
| 分档性能与对照 200 / 100 ms 的达标判定 | [第 25 篇 §5](25-results-and-compliance.md#5-对照客户指标的判定表) |
| 两台机器的条件标签差异（为什么不能相减） | [第 25 篇 §1](25-results-and-compliance.md#1-条件标签) |
| 还有哪些性质没测到 | [第 25 篇 §8](25-results-and-compliance.md#8-尚未覆盖) |
| 每条性质由哪个脚本、用什么判据、证到了哪一步 | [第 21 篇 §3](21-test-overview.md#3-测试矩阵) |

---

## 5. 小结

1. **三个时钟**：客户端墙钟（等多久）、冻结窗口（业务感受到的停顿）、
   宿主分阶段（时间去哪了）。前两个说差多少，第三个说差在哪。
2. `PhaseTimings` 是 `nil` 安全的 map，所以计时能直接加在热路径上；
   写进 JSON 文件而不只是日志，因为**基准脚本要读它**。
3. **`memMode` 是抓静默退化的主力**：除了第一个 checkpoint，出现 `full` 就是有问题。
   它同时进日志和 metrics 属性 —— 让这个问题能从仪表盘上被回答。
4. 能力上报**不只报结论，还报理由**。一个悄悄丢了增量的部署，应该能从日志里读出原因。
5. 交付态两个脚本**一个只证正确性、一个只测耗时**，各自单文件、零共享依赖，
   连宿主探针都故意重复一份 —— 拷走一半的公共模块不会报错，只会让条件标签集体变成「未知」。
6. 正确性验收的三条原则：**先自证检查会失败**、**验删除和权限位**（才说明是块级）、
   **进程带 PID 复活**（才说明是内存搬回去了）。
7. **活性判据不能用时间** —— 单调时钟真的被拨回了。只断言计数器在推进。
8. 口径说明必须和数字放在一起 —— 否则数字一定会被单独引用。
   所以**全书的实测数字只在[第 25 篇](25-results-and-compliance.md)一处维护**，本篇不留数字。

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

---

## 延伸阅读

| 想知道 | 去哪 |
|---|---|
| 四种静默失效、三层测试、测试矩阵、测量纪律 | [第 21 篇](21-test-overview.md) |
| 每条正确性判据怎么设计的、59 项怎么构成 | [第 22 篇](22-functional-tests.md) |
| 性能指标口径与负载构造 | [第 23 篇](23-performance-methodology.md) |
| 实测数字与达标判定 | [第 25 篇](25-results-and-compliance.md) |
| 上机怎么跑 | [第 26 篇](26-acceptance-runbook.md) |

**下一部分**：[18 · 两套方案：ext4 与 XFS](18-ext4-vs-xfs.md) ——
机制和保障都讲完了，接下来是「在什么样的机器上、用哪一套」。
