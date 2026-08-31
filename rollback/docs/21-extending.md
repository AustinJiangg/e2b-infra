# 21 · 在这套方案上继续开发

> 前二十篇讲的是「它是什么」。本篇讲「你要动它的时候该知道什么」：
> 代码在哪、按任务该改哪里、哪些不变量碰不得、以及已知还差什么。
>
> **读者**：要接手或参与这个项目的工程师。
> **预备**：至少读完[第 13](13-end-to-end.md)、[14](14-failure-semantics.md)、
> [15 篇](15-state-and-concurrency.md)。
> **配套**：[第 22 篇 · 术语表与代码地图](22-glossary-and-code-map.md)。

---

## 0. 本篇要回答的问题

1. 代码在哪几个仓库、哪几个分支？交付形态是什么？
2. 要加一个设备 / 支持一种新文件系统 / 加一个 API，分别要动哪里？
3. 哪些不变量碰不得？改动时怎么自查？
4. 怎么在机器上验证一次改动？
5. 已知还差什么？

---

## 1. 代码在哪

### 1.1 两个仓库、两条轨

| 仓库 | 内容 | ext4 方案分支 | XFS 方案分支 |
|---|---|---|---|
| `infra-arm` | orchestrator（Go） | `jll` | `jll-xfs` |
| `KASandbox` | Firecracker（Rust） | `jll` | `jll-xfs` |

两个仓库的同名分支**必须配对使用**（[第 18 篇 §7](18-ext4-vs-xfs.md#7-版本配对)）。
两条轨的差异见[第 18 篇](18-ext4-vs-xfs.md)。

上游基线是 `infra-arm` 的 `2026.09`，加上 `fbee6fcd1 patch: all patch for arm64`
这一次 ARM 适配。**本方案的所有改动都在那之上。**

### 1.2 交付形态

产品交付的不是仓库，是 `e2b-infra` 里的两样东西：

| 交付物 | 内容 |
|---|---|
| `0001-adapted-for-arm-architecture.patch` | ARM 适配 + 本方案的全部 orchestrator 侧改动（约 12800 行，122 个文件） |
| `firecracker.arm` | 分叉 Firecracker 的编译产物 |

打包由 `e2b-infra.spec` 完成：

```spec
Source0: https://github.com/e2b-dev/infra/archive/refs/tags/%{tag}.tar.gz
Patch1:  0001-adapted-for-arm-architecture.patch
...
%build
# 只做 go build，不跑测试
```

**两个后果**：

1. **单元测试不进交付物** —— `%build` 只做 `go build`。测试只在 `infra-arm` 仓库里跑；
2. **改动必须能表达成 patch** —— 新增文件可以，但要注意 patch 的可维护性。

> 交付脚本 `e2b-deploy/dep/init-client.sh` 会把 `firecracker.arm` 拷进
> `/fc-versions/v<ver>/firecracker`。注意 950 上版本目录名与二进制真实版本脱钩，
> 换错目录**不会报错**（[第 19 篇 §7.1](19-kunpeng-platform.md#71-fc-versions-的版本号与二进制脱钩)）。

### 1.3 阅读源码的建议顺序

```
① internal/checkpoint/service.go        —— 编排，一眼看到全貌
② internal/sandbox/checkpoint.go        —— 两个暂停窗口里发生什么
③ internal/checkpoint/store.go          —— 树账本，本方案的核心
④ src/vmm/src/rollback.rs               —— Firecracker 侧主体
⑤ internal/sandbox/block/overlay.go     —— 磁盘换层
⑥ internal/checkpoint/{bitmap,rootfs}.go —— 两种账本的细节
```

前四个文件读完，整套机制就通了。

---

## 2. 按任务的改动指引

### 2.1 加一种 virtio 设备

| 步 | 做什么 |
|---|---|
| 1 | `validate_topology` 里加上它 —— 否则拓扑检查会因「设备总数不等」而失败 |
| 2 | `apply_device_states` 里写回它的状态 |
| 3 | **查[第 12 篇 §8](12-rollback-pitfalls.md#8-检查表还有哪些地方可能有同类问题) 的检查表**：它有没有从 guest 内存推导出来的缓存？有没有在途异步 I/O？运行期会不会自己写 guest 内存？ |
| 4 | 如果有在途 I/O，`quiesce_devices` 里加上排空逻辑 |
| 5 | 如果它运行期写 guest 内存，阶段 9 之后要重新标脏那些页 |
| 6 | 新增的系统调用检查 seccomp 白名单（VMM 线程 / vCPU 线程各一份） |

> 如果这个设备有跨 guest / host 的状态（像 vsock），先想清楚回滚语义。
> **想不清楚就在 `validate_topology` 里拒绝它** —— 现在 balloon 和 vsock 就是这么处理的。

### 2.2 支持一种新的文件系统能力

先问：**它改变的是「产物怎么存」还是「产物怎么读」？**

- 如果是前者（例如某种新的 COW 原语），参考 XFS 方案的形态：
  探针（启动时一次）+ 快路径 + 慢路径退回 + **退化上报**；
- 如果只是性能特性（例如更快的 `fallocate`），大概率不需要改代码。

**必须做退化上报。** 一个静默退化几十倍的路径比没有这个优化更糟。

### 2.3 加一个 API

| 步 | 做什么 |
|---|---|
| 1 | `spec/checkpointd/checkpoint/checkpoint.proto` 加 RPC |
| 2 | `service.go` 的 `switch operation` 加分支；`Handles` 不用改（前缀匹配） |
| 3 | 全程持有 `LockSandbox` —— 除非它确实不碰每沙箱状态 |
| 4 | 用 `context.WithoutCancel`，如果它会暂停虚机 |
| 5 | 失败要能分级：区分「沙箱还能用」和「沙箱撕裂了」 |
| 6 | 重新生成 SDK 桩（`packages/python-sdk/scripts/regen-checkpoint-pb.py`） |

### 2.4 改 Firecracker 侧

| 步 | 做什么 |
|---|---|
| 1 | 尽量做成**加法**：新端点、可选字段。不改既有语义 |
| 2 | 新错误变体要在 `RollbackError::faults_vm()` 里归类 —— **编译器会强迫你回答** |
| 3 | 新系统调用 → seccomp 白名单 |
| 4 | 改了线格式 → **两侧同时改**（Rust 与 Go），并升 `DIRTY_BITMAP_VERSION` |
| 5 | 改了 `setup_dirty_tracking` 的调用位置 → 检查它仍在 vCPU 创建之后 |

### 2.5 改账本

**最危险的区域。** 改之前请先把[第 14 篇 §9](14-failure-semantics.md#9-不变量清单)
那张表读一遍。特别注意：

- 改 `ParentID` 的设置时机 → 破坏「`E_x` 覆盖 `(parent(x), x]`」这条不变量；
- 改可见性判断 → 半写完的快照可能变成恢复目标；
- 改删除逻辑 → 可能打断后代的解析链；
- 加新状态 → 想清楚它在 `Get` / `List` / 内容解析 / 回滚集里各自怎么表现。

---

## 3. 改动自查清单

每次动这套代码，逐条问自己：

| # | 问题 | 相关篇 |
|---|---|---|
| 1 | 有没有引入「与虚机规格线性相关」的新开销？ | [05 §5.1](05-design-goals.md#51-成本只与改动量挂钩) |
| 2 | 有没有重建某个宿主资源？ | [05 §5.2](05-design-goals.md#52-不重建任何宿主资源) |
| 3 | 新的失败路径属于提交点之前还是之后？ | [14 §2](14-failure-semantics.md#2-提交点把失败切成两半) |
| 4 | 会不会静默降级？降级了怎么被发现？ | [17 §2](17-observability-and-verification.md#2-让静默退化现形) |
| 5 | 持锁期间有没有做 I/O？ | [15 §2.1](15-state-and-concurrency.md#21-storemu保护账本) |
| 6 | 依赖「虚机已暂停」吗？调用契约写清楚了吗？ | [15 §4](15-state-and-concurrency.md#4-暂停窗口作为静默机制) |
| 7 | 引入新系统调用了吗？ | [09 §7](09-firecracker-api-contract.md#7-seccomp) |
| 8 | 破坏了 14 条不变量里的哪一条？ | [14 §9](14-failure-semantics.md#9-不变量清单) |

**第 8 条最重要。** 14 条不变量里有 9 条被破坏后是**静默的** ——
测试会过，功能会「正常」，问题在几个月后以某种莫名其妙的方式出现。

---

## 4. 怎么验证一次改动

### 4.1 三层

| 层 | 跑什么 | 在哪 |
|---|---|---|
| 单元测试 | `go test ./internal/checkpoint/...` | 开发机 |
| 正确性验收 | `benchmark/checkpoint_verify.py` | 目标机（950 / 920B） |
| 性能基准 | `benchmark/checkpoint_bench.py` | 目标机 |

**深入排查**用开发态工具箱 `rollback/test-950/`（穷举语义、劣化排查、分位数），
它与交付态脚本**不要混用**。

### 4.2 换二进制的流程

```bash
# ① 宿主自检
./test-950/01-check-host.sh

# ② 换二进制（orchestrator + firecracker）
./test-950/03-switch.sh

# ③ 验证实际跑起来的是哪一版
./test-950/04-verify-runtime.sh    # 会打出 FC 的 sha
```

第 ③ 步别跳过 —— 950 上换错版本目录**不会报错**
（[第 19 篇 §7.1](19-kunpeng-platform.md#71-fc-versions-的版本号与二进制脱钩)）。

### 4.3 至少要看的三个信号

改完之后，第一次跑起来先确认：

| 信号 | 期望 |
|---|---|
| 启动日志的 `checkpoint capabilities` | `track_dirty_pages: true`，理由符合预期 |
| 第二个 checkpoint 的 `memMode` | `incremental`（不是 `full`） |
| `dmesg \| grep 'Enable HDBSS success'` | 950 上应该有，PID 是 Firecracker 的 |

三个里任何一个不对，后面的数字都不用看了。

---

## 5. 已知缺口

### 5.1 测试

| 缺口 | 说明 |
|---|---|
| **跨树回滚** | 断链后新根开新树，跨树回滚会退化成全量。逻辑上正确（[第 8 篇 §4.5](08-memory-diff-tree.md#45-最近公共祖先怎么求)），但**没有测试** |
| **并发操作同一沙箱** | 依赖 `opLocks`，没有针对性的竞态测试 |
| **HDBSS 武装时序** | 不变量 #13 只靠调用点位置保证，没有断言 |

### 5.2 实测

| 缺口 | 说明 |
|---|---|
| **950 上的分档基准** | `checkpoint_bench.py` 还没在 950 上跑过 |
| **HDBSS vs 软件写保护** | 收益尚未量化。`perf` 只有 950 有，`kvm_exit` 计数是最硬的证据 |
| **`FC_HDBSS_ORDER` 1/2/4** | 写密集负载下 buffer 溢出吃掉多少收益，不知道 |
| **完整 rpmbuild** | 尚未端到端跑通 |

### 5.3 可能的方向

| 方向 | 动机 | 难度 |
|---|---|---|
| **可导出的 checkpoint** | 层栈压平 + 模板底座物化，做成独立 export 接口（**不改 create 默认路径**） | 中 |
| **账本跨重启加载** | 数据已经够了，缺读取代码；但会引入陈旧条目回收与格式迁移（[第 16 篇 §3.3](16-lifecycle-and-portability.md#33-账本不从磁盘加载)） | 中 |
| **按沙箱按需武装脏页跟踪** | 现在是整个 orchestrator 一个值；要按沙箱得在创建时知道它会不会打 checkpoint | 中 |
| **结构化的 Faulted 标识** | 现在客户端靠字符串匹配识别（[第 9 篇 §3.3](09-firecracker-api-contract.md#33-客户端侧的三个细节)） | **低，建议顺手做** |
| **层文件的全零块回收** | 恒等映射不剔除全零块（[第 10 篇 §4.1](10-disk-layering.md#41-不紧凑化带来的简化)） | 中 |
| **跟上游合并** | 分叉小是有意的，但上游会动 | 持续 |

---

## 6. 几条经验

写在最后，都是这个项目里得到的。

**① 「显然更快」的路线要测。**
vCPU 只写寄存器、不做完整复位，直觉上更快。实测 p95 是 145.3 ms vs 48.3 ms ——
**慢三倍**。而且那次的处理方式值得学：不是保留两条路线加一个开关，
而是**连同测量一起删掉输的那条**。一个已经有答案的问题不该永久留在代码里。

**② 静默是最贵的故障模式。**
这个项目里花时间最多的两个问题（RX 缓存、conntrack）都表现为**挂死而不是报错**。
所以有了三个时钟、`memMode` 回报、能力上报、队列诊断日志。
**加一个可观测信号的成本，远低于一次静默故障的排查成本。**

**③ 划界比实现更重要。**
明确「不做崩溃恢复、不做跨节点、不做长期持久化」之后，
一整类问题（陈旧条目、跨节点一致性、离线加载、格式迁移）根本不存在。
**先想清楚不做什么。**

**④ 注释写「为什么」，不写「是什么」。**
这套代码里最有价值的注释都在回答「为什么是这样」：为什么不 msync、
为什么 45 秒、为什么手工解锁、为什么隐藏而不是删除。
本书的很多内容其实就是把这些注释展开。

---

## 7. 小结

1. 两个仓库、两条轨（`jll` / `jll-xfs`），**同名分支必须配对**。
   交付形态是 patch + 二进制，**单元测试不进交付物**。
2. 改动指引按任务组织；加设备时**必读**[第 12 篇 §8](12-rollback-pitfalls.md#8-检查表还有哪些地方可能有同类问题) 的检查表。
3. 自查清单 8 条，第 8 条（不变量）最重要 —— **9 条不变量被破坏后是静默的**。
4. 验证分三层；换完二进制**一定要验实际跑起来的是哪一版**。
5. 已知缺口：三项测试、四项实测、六个可能的方向。
   其中「结构化的 Faulted 标识」难度低，建议顺手做掉。

---

**下一篇**：[22 · 术语表与代码地图](22-glossary-and-code-map.md) —— 全书的查询入口。
