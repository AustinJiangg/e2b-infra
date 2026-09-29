# 附录 B · 继续开发

> 给要接手或参与这个项目的工程师看。读完能知道：代码在哪、交付物是什么形态、按任务该改哪里、
> 哪些不变量碰不得、改完怎么验证、还有哪些已知的方向。
> 建议先读 [19](19-end-to-end.md)、[20](20-failure-semantics.md)、[21](21-state-concurrency-durability.md)；
> 查符号用[附录 A](A-glossary-and-code-map.md)。

---

## 1. 代码在哪

### 1.1 仓库与分支

| | 在哪 | 装着什么 |
|---|---|---|
| **代码** | openEuler 交付仓库 `KASandbox_0904`。开发分支 **`deltabox-dev`**，开发测试通过后合并进交付分支 **`deltabox`**。首次合入是 openEuler KASandbox 的 [MR !119](https://gitcode.com/openeuler/KASandbox/pull/119)「feat: host-side sandbox checkpoint/restore (ARM64, HDBSS)」 | orchestrator、Firecracker、Python SDK 三个组件同仓 |
| **手册与测试** | `e2b-infra` 仓库的 `rollback/` | 本手册源文件、验收与补充测试脚本（`rollback/scripts/`） |
| **部署件** | `e2b-infra` 仓库的 `e2b-infra.spec` 与补丁 `0001-adapted-for-arm-architecture.patch` | 0001 补丁由 `deltabox-dev` 的 orchestrator 侧改动融合进上游源码后生成，做法与脚本见 `e2b-infra/deploy-docs/08-源码开发与出包流程.md` 第 11 节。补丁里剔除了新增的 `_test.go`，单测以 `deltabox-dev` 上的结果为准 |

| 组件 | 目录 | 单元测试 | 怎么跑 |
|---|---|---|---|
| orchestrator（Go） | `packages/orchestrator/`：checkpoint 服务在 `internal/checkpoint/`，暂停窗口在 `internal/sandbox/checkpoint.go`，磁盘层在 `internal/sandbox/{block,rootfs}/`，FC 客户端在 `internal/sandbox/fc/`，conntrack 在 `internal/sandbox/network/`；代理在 `packages/shared/pkg/proxy/` | 同目录 `*_test.go`，含属性测试（`compact_sim_test.go`、`layer_refs_test.go`、`blockset_test.go` 等） | `cd packages/orchestrator && go test -race ./internal/checkpoint/... ./internal/sandbox/...` |
| Firecracker（Rust） | `firecracker/`：回滚主体在 `src/vmm/src/rollback.rs` | 各源文件内 `#[cfg(test)]` | `cd firecracker && cargo test --all`；故障注入相关的再加 `-p vmm -p firecracker --features vmm/rollback-fault-inject,firecracker/rollback-fault-inject` |
| Python SDK | `py-sdk/`：`e2b/sandbox_sync/checkpoint.py`、`e2b/sandbox_async/checkpoint.py`、`e2b/sandbox/checkpoint/errors.py`、`e2b/exceptions.py` | `py-sdk/tests/test_checkpoint_errors.py` | `cd py-sdk && pytest tests/test_checkpoint_errors.py` |

FC 的 vmm 单测要引导仓库里的测试内核（`src/vmm/src/test_utils/mock_resources/test_pe.bin`、`test_elf.bin`），
并且要能访问 `/dev/kvm`；源码目录只读挂载时，部分测试会因为在当前目录写临时文件而失败，要在可写副本里跑。

交付的是 **ext4 方案**（为什么见 [12](12-architecture.md)）。上游基线是 e2b infra 的 `2026.09` 加一次 ARM 适配，本方案的全部改动在那之上。

### 1.2 交付形态

| 交付物 | 是什么 |
|---|---|
| **代码** | 当前是 `KASandbox_0904` 的 `deltabox-dev`（版本见 [README](README.md)），`deltabox` 交付分支待整理；`firecracker/`、`packages/`、`py-sdk/` 三个组件同仓 |
| **文档** | 本手册，导出为单个 HTML 文件 |

目标平台是 950（带 HDBSS）；920B 是开发环境。**orchestrator 与 Firecracker 必须取自同一版本、配对部署**：
orchestrator 依赖 FC 的 `PUT /snapshot/rollback` 与 `PUT /snapshot/save-dirty-bitmap` 两个新端点（[15](15-firecracker-api-contract.md)）。

### 1.3 阅读源码的顺序

```
① internal/checkpoint/service.go           编排，一眼看到全貌
② internal/sandbox/checkpoint.go           两个暂停窗口里发生什么
③ internal/checkpoint/store.go             树账本，本方案的核心
④ src/vmm/src/rollback.rs                  Firecracker 侧主体
⑤ internal/sandbox/block/overlay.go        磁盘换层（配 layerstack.go）
⑥ internal/checkpoint/{bitmap,rootfs}.go   两种账本的细节
⑦ internal/checkpoint/{compact,compact_layers,layer_refs,quota}.go
                                           删除后的合并、层回收、字节配额
```

前四个读完，整套机制就通了；第 ⑦ 组决定长期运行时的磁盘与链深。

---

## 2. 按任务的改动指引

### 2.1 加一种 virtio 设备

| 步 | 做什么 |
|---|---|
| 1 | `validate_topology` 里加上它，否则拓扑检查会因"设备总数不等"失败 |
| 2 | `apply_device_states` 里写回它的状态 |
| 3 | **查 [18](18-rollback-pitfalls.md) 的检查表**：它有没有从 guest 内存推导出来的缓存？有没有在途异步 I/O？运行期会不会自己写 guest 内存？ |
| 4 | 有在途 I/O 的，在 `quiesce_devices` 里加排空逻辑 |
| 5 | 运行期写 guest 内存的，回滚最后阶段之后要重新标脏那些页 |
| 6 | 新增的系统调用加进 seccomp 白名单（`firecracker/resources/seccomp/aarch64-unknown-linux-musl.json`，按线程分组） |

有跨 guest / host 状态的设备（像 vsock），先想清楚回滚语义。**想不清楚就在 `validate_topology` 里拒绝它**，
现在 balloon 和 vsock 就是这么处理的。

### 2.2 支持一种新的存储能力

先问：它改变的是"产物怎么存"还是"产物怎么读"？

- 改变存法的（例如一种新的写时复制原语）：做成启动时探测一次 + 快路径 + 退回慢路径 + **退化上报**；
- 只是性能特性的（例如更快的 `fallocate`）：大概率不需要改代码。

**必须做退化上报。** 静默退化几十倍的路径比没有这个优化更糟。

### 2.3 加一个 API

| 步 | 做什么 |
|---|---|
| 1 | `spec/checkpointd/checkpoint/checkpoint.proto` 加 RPC |
| 2 | `service.go` 的 `switch operation` 加分支；`Handles` 按前缀匹配，不用改 |
| 3 | 全程持有 `LockSandbox`，除非它确实不碰每沙箱状态 |
| 4 | 会暂停虚机的，用 `context.WithoutCancel`，客户端断开不能把虚机留在暂停里 |
| 5 | 失败要能分级：区分"沙箱还能用"和"沙箱撕裂了" |
| 6 | 新的拒绝原因要有自己的 `reason`，并在 SDK 的 `errors.py` 映射到异常类；新异常要挂在 `CheckpointException` 下（按需再挂到更具体的父类，像 `CheckpointBytesLimitException` 挂在 `CheckpointTooManyException` 下，让旧代码照样接得住） |
| 7 | 重新生成 SDK 桩：`py-sdk/scripts/regen-checkpoint-pb.py` |
| 8 | 同步 `e2b-infra` 里的 SDK 覆盖层与 `crtest/sdktests/test_checkpoint_errors.py`（它是 SDK 测试的一份拷贝） |

### 2.4 改 Firecracker 侧

| 步 | 做什么 |
|---|---|
| 1 | 尽量做成**加法**：新端点、可选字段，不改既有语义 |
| 2 | 新错误变体要在 `RollbackError::faults_vm()` 里归类，编译器会强迫你回答 |
| 3 | 新系统调用加进 seccomp 白名单 |
| 4 | 改了位图线格式要两侧同时改（Rust 与 Go），并升 `DIRTY_BITMAP_VERSION` |
| 5 | 改了 `setup_dirty_tracking` 的调用位置，检查它仍在 vCPU 创建之后 |
| 6 | 全量快照的侧车必须是全 1：orchestrator 对全量根直接取全 1，FC 侧由 `test_full_snapshot_sidecar_is_all_ones` 守着 |

### 2.5 改账本

**最危险的区域。** 改之前先把 [20](20-failure-semantics.md) 的不变量清单读一遍。特别注意：

- 改 `ParentID` 的设置时机 → 破坏"`E_x` 覆盖 `(parent(x), x]`"；
- 改可见性判断 → 半写完的快照可能变成恢复目标；
- 改删除、剪枝或合并 → 可能打断后代的解析链。合并的正确性依赖"被并入的条目隐藏、非基准、恰有一个子节点"
  这三个条件，任何一条放松都要重新证明回滚集不变（[14](14-memory-diff-tree.md)）；
- 层的回收**按视图引用计数，不按树**：跟踪关闭时每个 checkpoint 都是树根，而它的视图仍含之前的全部层（[16](16-disk-layering.md)）；
- 在全局锁里只改内存里的结构，删文件放到锁外（收进 `reclaim`，调用方放锁后删）；
- 加新状态 → 想清楚它在 `Get` / `List` / 内容解析 / 回滚集 / 合并候选 / 字节计数里各自怎么表现；
- 改了会影响文件增减的路径 → 同步维护层引用计数与字节计数，属性测试会从盘上重算并比对。

---

## 3. 改动自查清单

| # | 问题 | 相关篇 |
|---|---|---|
| 1 | 有没有引入与虚机规格线性相关的新开销？ | [11](11-baseline-goals-and-native.md) |
| 2 | 有没有重建某个宿主资源？ | [11](11-baseline-goals-and-native.md) |
| 3 | 新的失败路径在提交点之前还是之后？ | [20](20-failure-semantics.md) |
| 4 | 会不会静默降级？降级了怎么被发现？ | [07](07-observability-reference.md) |
| 5 | 持全局锁期间有没有做文件 I/O 或 O(条目数) 的扫描？ | [21](21-state-concurrency-durability.md) |
| 6 | 依赖"虚机已暂停"吗？调用契约写清楚了吗？ | [21](21-state-concurrency-durability.md) |
| 7 | 会不会在冻结窗口里等别的沙箱、等 GC 或等内存回收？ | [21](21-state-concurrency-durability.md) |
| 8 | 引入新系统调用了吗？ | [15](15-firecracker-api-contract.md) |
| 9 | 破坏了哪一条不变量？ | [20](20-failure-semantics.md) |

**第 9 条最重要。** 大多数不变量被破坏后是静默的：测试会过，功能会"正常"，问题在很久以后以莫名其妙的方式出现。

---

## 4. 怎么验证一次改动

### 4.1 三层

| 层 | 跑什么 | 在哪 |
|---|---|---|
| 单元测试与属性测试 | §1.1 的三条命令；Go 一律带 `-race` | 开发机 |
| 正确性与功能 | `rollback/scripts/950/run.sh smoke`、`run.sh func`；动了树语义再跑 `dev/correctness.py ext4` | 目标机 |
| 性能 | `run.sh perf`；动了长期行为（删除、合并、回收、锁）要跑并发长测 | 目标机 |

判据讲解见 [23](23-testing-and-functional-verification.md)，性能口径见 [24](24-performance-methodology.md)，
上机步骤见 [09](09-acceptance-runbook.md)。换二进制的日常流程（编译、替换、确认跑的是新的）见
`e2b-infra/deploy-docs/08-源码开发与出包流程.md` 第 4 节。

### 4.2 至少要看的三个信号

改完第一次跑起来，先确认：

1. 启动日志的 `checkpoint capabilities` 行里 `track_dirty_pages` 为 `true` 且理由符合预期，各开关的值与来源是你想要的（[07](07-observability-reference.md)）；
2. 第二个 checkpoint 的 `memMode` 是 `incremental`，不是 `full`；
3. 950 上 `dmesg | grep 'Enable HDBSS success'` 有输出，且 PID 是 Firecracker 的。

任何一个不对，后面的数字都不用看。第 2 条是防"增量静默退化成全量"的主力判据，而它本身也会失效
（SDK 没透出字段时脚本会跳过，[23](23-testing-and-functional-verification.md)）。

### 4.3 故障注入钩子（只给开发者）

失败路径里最值得信任的恰恰是手工到不了的那几条，所以留了两个钩子：

- orchestrator 侧 `CHECKPOINT_FAULT_INJECT`（`internal/checkpoint/faults.go`），逗号分隔的故障名，可加 `:once`，启动时读一次；
  名字清单见 [06](06-configuration-and-capacity.md)。合并路径的 `compact_*` 注入点只在单测里用过。
- FC 侧 `FC_ROLLBACK_FAULT_INJECT`，只在用 cargo feature `rollback-fault-inject` 构建的二进制里存在，**交付二进制不含**。

生产部署不设这两个变量；是否有故障被武装，看启动能力行的 `fault_inject` 字段。

---

## 5. 已知缺口与可能的方向

测试与实测上的缺口只在 [25](25-results-and-compliance.md) 的"已知项与缺口"维护一份，这里不重复。下面是代码上可以做的方向：

| 方向 | 动机 | 难度 |
|---|---|---|
| **后台异步合并** | 合并现在在 delete 结尾同步做，delete 的 p50 因此变长 | 中 |
| **conntrack 后台清扫再提速** | 它已贴近冻结窗口的关键路径；可先按生产数据调单地址走过滤的阈值 | 中 |
| **层侧车改为区间编码** | `.meta` 每块 8 字节，restore 读解码所有层的侧车随块数线性增长；要改盘上格式 | 中 |
| **可导出的 checkpoint** | 层栈压平 + 模板底座物化，做成独立的导出接口，不改 checkpoint 默认路径 | 中 |
| **账本跨重启加载** | 现在启动时清空 store 根；要加回按事务顺序的 `fsync`、读取代码、陈旧条目回收与格式迁移（[22](22-lifecycle-reasoning.md)） | 中 |
| **按沙箱按需武装脏页跟踪** | 现在整个 orchestrator 一个值；要在创建沙箱时就知道它会不会做 checkpoint | 中 |
| **层文件的全零块回收** | 恒等映射不剔除全零块 | 中 |
| **视图层列表改为引用父代** | 每个条目的视图把截止到自己的全部层完整记一份；合并之后层数有界，但仍是每条目 O(层数) 的记账 | 中 |
| **跨树回滚与 HDBSS 武装时序的端到端 / 断言** | 前者只有单元测试，后者只靠调用点位置保证 | 低 |
| **跟上游合并** | 分叉小是有意的，但上游会动；每次同步后按 `deploy-docs/08` 第 11 节重新生成 0001 补丁 | 持续 |
