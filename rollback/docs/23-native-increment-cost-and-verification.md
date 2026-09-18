# 23 · 原生精确增量的代价、验证与边界

> 「4 KiB 存、2 MiB 拼」不是免费的：代价落在恢复的懒加载热路径上。本篇讲这笔代价怎么被压回噪声内、
> 哪些优化量过之后决定不做、两台机器上的平台策略差在哪，以及四类验证证据与这次修复的影响面。
>
> **读者**：工程师、系统工程师。
> **预备**：[第 22 篇 · 原生 snapshot 的精确增量](22-native-increment-fix.md)。
> **代码**：infra-arm `jll` 提交 `c9a92a5ab`；探针与基准 `e2b-infra/rollback/scripts/probes/` —
> `pb2.py`、`pb5.py`、`native_snapshot_bench.py`
>
> **与本书主线的关系**：这是对**原生路径**的一处独立修复，不属于本方案 checkpoint / restore；
> 两者只共用一个位图端点，不共用任何产物 —— 完整交代见[第 21 篇](21-native-increment-diagnosis.md)。

---

## 0. 本篇要回答的问题

1. 这次修复在恢复路径上付了多少？是怎么压回去的？
2. 哪些看起来该做的优化量过之后决定不做，为什么？
3. 为什么 950 上是默认精确、920B 上默认退回修复前口径？
4. 拿什么证明它对：四类证据分别证的是哪一层？
5. 对本方案与其他功能有没有影响？

---

## 1. 代价与边界

### 1.1 恢复开销：一度 +100~200 ms，压回噪声内

第一版实现每次 2 MiB 缺页都 `make` 一个 2 MiB 缓冲、经 `ReadAt` 多拷一次，200 次缺页就是 400 MiB
的分配和 GC，`touch1`（恢复后首次整读负载）六档均值从 243 ms 涨到 367 ms。
加上[第 22 篇 §3.3](22-native-increment-fix.md#33-③-填一次-2-mib-缺页三级取源) 的第二级零拷贝快路径回到 316，
再加缓冲池回到 241，与修复前持平
（逐档数字在[第 28 篇 表 3-J](28-results-and-compliance.md#表-3-j--native_snapshot_benchpy修复后920b-0914-native4k) 的读法段）。

教训只有一条：**懒加载路径上每一次缺页都是热路径**，多一次 2 MiB 分配就是多一次可测的延迟。

### 1.2 量过之后决定不做的

- **header 膨胀**。4 KiB 碎片化后映射条目增多，六代从 141 KiB 长到 232 KiB，每代约 +18 KiB。
  `NormalizeMappings` 会合并相邻区间；这个规模不值得加「同一 2 MiB 内脏页占比超阈值就整块导出」的折中。
- **零页**。导出里 0.8% ~ 8.4% 是全零页，可以走 `Empty` 映射省掉，代价是导出前多扫一遍全部脏页。不做。

### 1.3 写保护的运行期代价：平台差异

新判据不选后端，只读位图；供数机制由 Firecracker 启动时的 `setup_dirty_tracking` 定
（[第 7 篇 §2](07-dirty-page-tracking.md#2-怎么知道一页被写过)）：

| | 950（HDBSS） | 920B（无 HDBSS） |
|---|---|---|
| 位图怎么来 | CPU 硬件记脏，guest 写页不陷出 | KVM 写保护，每个干净页第一次被写陷出一次 |
| 谁付这笔账 | 几乎没有 | **所有沙箱**，不管做不做快照 |
| 默认是否开追踪 | 开（探到 KVM 能力 502） | 关 |
| 原生快照口径 | 精确增量 | 默认退回修复前口径；`FC_TRACK_DIRTY_PAGES=true` 才精确 |

这条策略与本方案 checkpoint 的完全一致（[第 7 篇 §4.6](07-dirty-page-tracking.md#46-默认值为什么跟着硬件走)）：
**950 上精确增量零副作用；无 HDBSS 的机器上「精确增量」与「不给所有沙箱开写保护」二选一，默认选后者。**
所以本书里 920B 的修复后数据全部是显式 `FC_TRACK_DIRTY_PAGES=true` 下测的，条件标签必须带这一项。

两台机器代码路径逐行相同，位图内容相同（950 只读 192 MiB 那档 15.0 MiB，920B 12.7 MiB，
差的是模板本底），差别只在这笔运行期代价。

### 1.4 兼容

| 场景 | 行为 |
|---|---|
| 新 orchestrator 起旧 2 MiB 快照 | 块等于页，走[第 22 篇 §3.3](22-native-increment-fix.md#33-③-填一次-2-mib-缺页三级取源) 第一级，与修复前相同 |
| 新 orchestrator 起新 4 KiB 快照 | 走第二、三级 |
| **旧** orchestrator 起新 4 KiB 快照 | `NewUserfaultfdFromFd` 报 `block size mismatch` **拒起**，不会静默错数据 |
| 无 `save-dirty-bitmap` 的 Firecracker | 退回修复前判据，记 warn |

集群内应先全量升级 orchestrator 再让沙箱产生 4 KiB 快照；旧快照不受影响。

---

## 2. 验证

四类证据，对应[第 21 篇 §2](21-native-increment-diagnosis.md#2-原生内存增量的三层) 的三层加一个「与本方案共存」：

1. **体积 = 位图真值**（① 生效）。`pb2.py` 修复后「实际导出」列与「写跟踪位图」列逐档相等：
   920B 只读 192 MiB 从 358 降到 12.7 MiB、只改 12 MiB 从 176 降到 20.3 MiB
   （[第 28 篇 表 3-K](28-results-and-compliance.md#表-3-k--pb2py-三列并排修复前后)）。
   `native_snapshot_bench.py` 六档 memfile 从恒 ~440 MiB 变成 38 / 54 / 88 / 153 / 288 MiB，
   随写入量线性（表 3-J）。
2. **跨代拼接正确**（②③ 生效）。`native_snapshot_bench.py --mode both` 两种模式全档位内存代号 /
   文件代号 / md5 全部回到目标代。专用例 `rollback/scripts/probes/pb5.py`：三代分别改**同一个 2 MiB 块里的不同 4 KiB 页**
   再各起新沙箱验 md5，外加一页清零 —— 这是「一个 2 MiB 页的 512 个子页来自多代 + nil build 空洞」的关键用例，
   ALL PASS。单测 `TestFileSliceAssemblesAcrossMappings` 断言零拷贝路径返回同一底层字节、跨映射块正确拼出；
   `TestToDiffHeaderFinerBlockSize` / `TestToDiffHeaderBlockSizeMustDivide` 覆盖
   [第 22 篇 §3.2](22-native-increment-fix.md#32-②-存header-块大小降到-4-kib父代映射原样保留) 的继承规则；
   `TestReadPage*` 覆盖[第 22 篇 §3.3](22-native-increment-fix.md#33-③-填一次-2-mib-缺页三级取源) 三级与缓冲复用不污染内容。
3. **退路完整**（[第 22 篇 §3.5](22-native-increment-fix.md#35-追踪没开时的门)）。920B `FC_TRACK_DIRTY_PAGES=false`：
   日志命中 `dirty tracking is off`，memfile 回到 172 / 178 MiB 的工作集口径，内存与文件校验仍全过。
4. **与本方案共存**（[§3](#3-影响面只在原生路径的内存侧)）。同一二进制上 `checkpoint_verify.py` 全过，
   `checkpoint_bench_v2.py` 六代全 `incremental`（[第 28 篇 §4](28-results-and-compliance.md#4-性能--920b软件写保护)）。

未做：950 上尚未复跑（代码路径相同，预期只差本底）；`jll-xfs` 未合入 —— 那条线的 Firecracker
没有 `save-dirty-bitmap` 端点，合过去只会走退路，没有收益。

---

## 3. 影响面：只在原生路径的内存侧

[第 21 篇 §4](21-native-increment-diagnosis.md#4-本方案为什么天生精确而这个做法学不过来) 说过本方案的差分
不经过 ②③ 那两把锁，所以它本来就没有要修的东西；这一节把「因此它也不会被这次修复碰到」按调用链坐实。

按调用链数一遍，改动只出现在两段：

- **打快照**：`Sandbox.Pause` → `Uffd.DiffMetadata`（① 换源）→ `ToDiffHeader`（② 块大小）→
  `ExportMemory`（步长 4 KiB）。`Checkpoint` RPC 复用同一段。
- **恢复**：`ResumeSandbox` 建新 Firecracker → uffd 缺页 / 预取 → `block.ReadPage`（③ 三级取源）。

不在链上的：模板构建（`NoopMemory.DiffMetadata` 未动，模板 header 仍 2 MiB）；rootfs 差分与 NBD；
SDK；上传与模板存储；**本方案 checkpoint / restore 的全部代码**。两边唯一的交点是
`save-dirty-bitmap` 这个端点 —— 它是非破坏性的合并读，本方案 checkpoint 也这么用，
同一沙箱先 checkpoint 再 pause 互不污染（本方案 restore 后有 reset，原生每代是新进程）。

这也是为什么本书[第 27 篇 §5.5](27-cross-implementation.md#55-原生快照口径修复后) 能宣布
「两套的内存产物量从此可相减」：两条路径读的是同一份位图，差别只剩差分文件的组织方式。

---

## 4. 小结

1. **代价在懒加载热路径上**，第一版 +100~200 ms，零拷贝快路径 + 缓冲池后回到噪声内；
   header 膨胀与零页量过后不做。
2. **策略与本方案一致**：有 HDBSS 默认开、零副作用；无 HDBSS 默认关，原生快照自动退回修复前口径。
3. **兼容方向是单向的**：新 orchestrator 读得了旧快照，旧 orchestrator 读新快照会**拒起**而不是错数据。
4. **影响面只在原生路径的内存侧**，本方案代码零改动、同一二进制上验证全过。

---

## 思考题

1. 如果把[§1.2](#12-量过之后决定不做的) 的「零页走 `Empty`」做了，[§2](#2-验证) 第 2 项的 `pb5.py`
   需要增加什么用例？
2. §1.4 那张表里，「旧 orchestrator 起新 4 KiB 快照」靠的是一条**拒起**的校验。
   如果那条校验当初写成了 warn 而不是 error，会在什么时候、以什么形态暴露出来？
   这和[第 24 篇](24-test-overview.md) 讲的「静默失效」是同一类问题吗？

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 三级取源与缓冲池 | `internal/sandbox/block/page.go` — `ReadPage`；`build/build.go` — `SliceContiguous`、`ReadAt`、`Slice` |
| uffd 校验与缺页 | `internal/sandbox/uffd/userfaultfd/userfaultfd.go` — `NewUserfaultfdFromFd`、`faultPage` |
| 追踪开关与平台默认值 | `internal/sandbox/fc/dirtytracking.go` — `TrackDirtyPagesEnabled`、`resolveTrackDirtyPages` |
| 判据换源与两道门 | `internal/sandbox/uffd/uffd.go` — `DiffMetadata` |
| header 块大小继承 | `packages/shared/pkg/storage/header/metadata.go` — `ToDiffHeader` |
| 探针与基准 | `e2b-infra/rollback/scripts/probes/` — `pb2.py`、`pb5.py`；`scripts/acceptance/native_snapshot_bench.py` |
| 设计稿与实施记录 | 工作区 `e2b-repo/原生快照精确增量-方案设计.md`、`原生快照增量判据-修复方案与验证计划.md` |

**相关篇目**：修复前的成本模型在[第 7 篇 §5](07-dirty-page-tracking.md#5-脏页判据的三方差异)
与[第 20 篇 §3](20-vs-native.md#3-成本模型)；修复后的对照口径在[第 27 篇 §5.5](27-cross-implementation.md#55-原生快照口径修复后)；
数据在[第 28 篇 §3.3](28-results-and-compliance.md)。

**下一篇**：[24 · 测试体系总览](24-test-overview.md) ——
这次修复的验证是四类证据；全书的测试体系是同一套思路的完整展开。
