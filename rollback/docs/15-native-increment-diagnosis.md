# 15 · 原生增量为什么不精确

## 本章目标

读完本章，你应该能够：

1. 说清 e2b 原生 pause 的内存增量由"判、存、填"三层组成，每一层的粒度由什么决定，三层又怎样被一根链条锁在一起；
2. 解释修复前 ARM 适配基线上原生增量为什么与改动量无关：一次只改 12 MiB，导出却有一百多 MiB；
3. 说清为什么"只把判据换成写跟踪位图"修不到底：2 MiB 差分块留下了一个约 130 MiB 的地板；
4. 说清本方案的 checkpoint 天生精确的原因，以及这个做法为什么不能照搬到原生路径上；
5. 说出修复的正确方向："4 KiB 存、2 MiB 拼"，以及任何判据改动都必须守住的安全方向："宁可多导，不可少导"。

---

## 第四部分导读

上一章（[14](14-lifecycle-reasoning.md)）推导完生命周期边界，第三部分到此结束。前三部分讲的都是本方案自己：checkpoint / restore 的设计、实现与工程保障。从本章起的四章（15–18）转向 e2b **原生快照**（pause / resume）。

这一部分讲的是一处独立的改进。在 ARM 适配基线上，原生 pause 做不到精确增量：每一代导出的内存量不随改动量变化。
我们让它做到了精确增量，并且保证它在与 checkpoint / restore 叠加使用时仍然正确。四章的分工如下：

| 章 | 回答什么 |
|---|---|
| 15（本章） | 修复前为什么不精确；只换判据为什么不够；本方案的做法为什么学不过来 |
| [16](16-native-increment-fix.md) | 怎么改：两条路线的取舍，判 / 存 / 填三层各改了什么，与 checkpoint 叠加时怎么保证导出完整，退路 |
| [17](17-native-increment-cost-and-verification.md) | 代价与验证：付了多少、怎么压回去、哪些优化量过之后不做、平台差异、证据 |
| [18](18-native-and-checkpoint-together.md) | 两种快照的分工与配合：各解决什么问题、为什么都必需、怎么叠加使用 |

为什么要在一本讲 checkpoint / restore 的书里花四章讲原生快照？因为两者是**互补**的：checkpoint 负责活沙箱里的高频快速回退，
原生快照负责沙箱离场后的长期保存、跨节点迁移和冷启动（[18](18-native-and-checkpoint-together.md) 详细论证）。
一个完整的产品两样都要用，而且经常串起来用：先用 checkpoint 找到想要的那一刻，再用原生 pause 把它固化下来。
原生这一半如果每次导出几百 MiB、而且与改动量无关，那么"固化"这一步的成本就与本方案的成本模型格格不入。

本章承接两处前文：

- [02](02-e2b-native-snapshot.md) 讲过原生 snapshot 的机制：`Pause` 的六步、内存差分靠链式 header 寻址、`ResumeSandbox` 新建进程后靠 uffd 缺页把工作集换回。本章在这个机制上按"谁决定粒度"再切一刀。
- [05](05-dirty-page-tracking-and-hdbss.md#5-判据差异读也算脏) 定义过判据差异"读也算脏"：ARM 适配基线把 uffd 写保护注释掉了，脏页判据塌缩成"驻留即脏"。本章用这个结论，不重复它的推导。

下一章（[16](16-native-increment-fix.md)）在本章诊断的基础上讲怎么改。

---

## 1. 先把原生 pause 的内存侧走一遍

诊断之前，先把原生 pause 导出内存的路径完整走一遍。后面所有分析都落在这条路径的某一步上。

### 1.1 拍：从暂停到差分文件

`Sandbox.Pause`（`packages/orchestrator/internal/sandbox/sandbox.go:912`）与内存有关的部分依次是：

1. `process.Pause` 暂停虚机（:942）；
2. `process.CreateSnapshot` 让 Firecracker 写出 snapfile，也就是 vCPU、中断控制器和设备状态（:950）。这一步**不写内存**：请求里没有 `mem_file_path`，
   Firecracker 只在带这个参数时才导出内存（`firecracker/src/vmm/src/persist.rs:177-178`）；
3. `memory.DiffMetadata` 取"这一代哪些页要导出"的集合（:966）；
4. `pauseProcessMemory`（:1056）先用这个集合和模板的 header 算出新一代 header（`ToDiffHeader`，:1067），
   再用 `ExportMemory`（:1074）按集合把页从 Firecracker 进程的地址空间 `process_vm_readv` 拷进一个**紧凑**的差分文件。

"紧凑"是指差分文件里只有被导出的块，一块紧挨一块，没有空洞。要知道某个 guest 偏移的内容在哪，得查 header。

### 1.2 header：一条映射链

header 是一张映射表。每一项 `BuildMap` 说："guest 内存的 `[Offset, Offset+Length)` 这段字节，存在 `BuildId` 这一代差分文件的
`BuildStorageOffset` 处"。新一代 header 由父代的映射和本代的映射合并而成（`MergeMappings`）：本代导出过的区间指向本代，
其余区间原样沿用父代的指向。于是任何一代的 header 都能直接回答"这一字节现在该去哪一代的哪个偏移读"，
不用沿着代际一层层往回找。

举个例子。模板是第 0 代，guest 写了块 5 之后 pause 得到第 1 代，再写块 2 之后 pause 得到第 2 代：

```
第 2 代 header：
  [块 0, 块 2)  → 第 0 代（模板）
  [块 2, 块 3)  → 第 2 代 差分文件偏移 0
  [块 3, 块 5)  → 第 0 代
  [块 5, 块 6)  → 第 1 代 差分文件偏移 0
  [块 6, 末尾)  → 第 0 代
```

header 里还有一个 `BlockSize`，它是整份 header 的"步长"：映射从哪个块开始、差分文件按多大的块拷。它写在 header 的元数据里，
每一代从父代继承（`Metadata.NextGeneration`）。模板的 `BlockSize` 由模板构建时的 `MemfilePageSize` 决定
（`packages/orchestrator/internal/template/build/config/config.go:69`）：开了大页就是 2 MiB（`header.HugepageSize`），否则 4 KiB。
e2b 的模板默认开大页，所以修复前每一代都是 2 MiB。

### 1.3 填：uffd 按 guest 页缺页

恢复时 `ResumeSandbox` 新建一个 Firecracker 进程，guest 内存交给 userfaultfd（uffd）托管（机制见 [01 §5.3](01-background.md#53-内存是按需拉取的)）。
guest 第一次碰某一页，内核发一个缺页事件给 orchestrator，orchestrator 按 header 找到内容，用 `UFFDIO_COPY` 把整页填进去。
这里的"页"是 **guest 页**：guest 内存用 hugetlbfs 的 2 MiB 大页背衬，一次缺页就是 2 MiB，`UFFDIO_COPY` 也必须给满 2 MiB。

---

## 2. 现象：改 12 MiB，导出 148 MiB

`rollback/scripts/probes/pb2.py` 在同一个沙箱上把三样东西并排量出来：

- **左列**：修复前原生 pause 用的判据，Firecracker 的 `GET /memory/dirty`；
- **中列**：Firecracker 的写跟踪位图，`PUT /snapshot/save-dirty-bitmap`（本方案 checkpoint 用的就是它，见 [08](08-firecracker-api-contract.md#4-扩展三put-snapshotsave-dirty-bitmap)）；
- **右列**：真做一次快照时实际导出的 memfile 大小。

每一步之间沙箱都经历一次 pause + resume，也就是每一步都是一个新的 Firecracker 进程。
下面是 950（HDBSS）上修复前的三行摘录（2026-09-10），完整表（含 920B 与修复后）见 [17](17-native-increment-cost-and-verification.md#2-体积修复前后)：

| 步骤 | `/memory/dirty` | 写跟踪位图 | 实际导出 |
|---|---|---|---|
| 刚建好 | 118.0 MiB | 4.6 MiB | 118.0 MiB |
| 只读 192 MiB，一字节没改 | 324.0 MiB | 15.0 MiB | 324.0 MiB |
| 只改 12 MiB | 148.0 MiB | 19.7 MiB | 148.0 MiB |

从这三行能读出三件事：

- **左列等于右列**，逐行相等。原生 pause 就是按左列那份位图导出的，中间没有别的放大环节。所以要找原因，只需要看判据。
- **只读那一行左列涨到 324 MiB。** 读了 192 MiB、一个字节没改，导出量却比上一步多了约 200 MiB。判据把"读"算成了"脏"。
- **中列是对的。** 刚建好 4.6、只读 15、只改 12 MiB 得 19.7，都是"写入量加上几 MiB 的系统本底"（guest 内核自己的记账、日志、页表更新）。

920B（KVM 写保护）上三行的形状完全一样，只是本底大几十 MiB。所以这不是某一台机器的问题，是 ARM 适配基线原生路径的问题。
而且**正确的位图就在旁边**：Firecracker 一直在算它，只是原生路径没有用。

问题于是变成两个：为什么原生路径不用那份位图？用了它就够了吗？回答这两个问题，需要把原生的内存增量拆开看。

---

## 3. 原生内存增量的三层

把 §1 的路径按"谁决定粒度"切成三层，每层都有一把决定粒度的锁：

| 层 | 做什么 | 粒度由谁决定 | 修复前 |
|---|---|---|---|
| ① **判** | 算出这一代哪些页要导出 | 判据接口返回的位图的粒度 | `GET /memory/dirty`：mincore 驻留 ∧ pagemap bit 57 未置位，按 header 的 `BlockSize` 取 |
| ② **存** | 按块把要导出的页拷进紧凑差分文件，header 记下每一块在哪一代的哪个偏移 | header 的 `BlockSize`，逐代继承 | 模板 header 是 2 MiB，之后每代都是 2 MiB |
| ③ **填** | 恢复时 uffd 缺页，按 guest 页从映射链取内容 `UFFDIO_COPY` 进去 | guest 页大小（hugetlbfs 2 MiB） | `NewUserfaultfdFromFd` 硬性要求 guest 页大小等于 `BlockSize`，否则拒绝启动 |

三层的粒度被一根链条锁在一起：

```mermaid
flowchart LR
    G["guest 页 = 2 MiB<br/>（hugetlbfs 大页）"] --> F["③ 填：一次缺页填一整页<br/>只能按 2 MiB 填"]
    F --> C["③ 的硬检查：<br/>guest 页大小 == BlockSize"]
    C --> S["② 存：BlockSize = 2 MiB<br/>写在 header 里逐代继承"]
    S --> J["① 判：位图再精细<br/>也要归并到 2 MiB 块导出"]
```

guest 页 2 MiB ⇒ ③ 只会按 2 MiB 填 ⇒ ③ 要求 ② 的块等于 2 MiB ⇒ ② 的块写在 header 里逐代继承 ⇒ ① 无论多精细，最终也要归并到 2 MiB 块导出。

这就是本章的骨架：**① 决定"读算不算脏"，② 和 ③ 决定"最小能存多细"。** 两件事要分开治。§4 先看 ①，§5 再看 ② 和 ③。

---

## 4. 修复前为什么不精确：判据与两个放大

### 4.1 判据在 ARM 上塌缩成"驻留即脏"

上游 x86 的判据是 uffd 写保护位：读缺页填进来的页保留写保护，写缺页填进来的页不保留，guest 之后写一个读进来的页会再触发一次写保护故障、
清掉保护位。于是"驻留且未被写保护"就等价于"写过"。Firecracker 的 `get_dirty_memory`（`firecracker/src/vmm/src/lib.rs:882`）
实现的正是这个两级判据：先用 `mincore` 筛出驻留页，再对驻留页查 pagemap，判据是 `is_present() && !is_write_protected()`
（`firecracker/src/vmm/src/utils/pagemap.rs:113`）。

ARM 适配基线上，arm64 的 6.6 内核没有 uffd 写保护，`faultPage` 里给 `UFFDIO_COPY` 加 `UFFDIO_COPY_MODE_WP` 的那几行被注释掉了
（`packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go:380-387`）。每次填页都清掉保护位，判据第二项恒真，
整个判据塌缩成 `mincore` 已经回答过的"驻留"。机制细节见 [05](05-dirty-page-tracking-and-hdbss.md#5-判据差异读也算脏)。本章只用它的结论：

> **修复前，原生 pause 的"脏页位图"就是驻留页集合。**

### 4.2 两个放大叠在一起：每代新进程，换入即脏

"驻留即脏"单独看还不至于让每一代都导出几百 MiB。它与原生路径的另一个特性叠在一起，才成了 §2 看到的数：

1. **每一代都是一个新的 Firecracker 进程。** 原生 `Checkpoint` RPC（SDK 的 `create_snapshot`）拍完快照会停掉旧进程、从新快照拉起一个新的；
   `Pause` 之后的 resume 同样是新进程（[02](02-e2b-native-snapshot.md) §1）。新进程的 guest 内存一开始是空的，guest 一跑起来，
   它用到的页（工作集）全部要重新经 uffd 缺页换入。
2. **换入即被判脏**（§4.1）。只要换入，就驻留；只要驻留，就算脏。

于是每一代增量的**下限约等于这一代 guest 碰过的全部页**，与改了多少无关。guest 读一遍 192 MiB 的文件缓存，这 192 MiB 就进了下一次导出；
guest 什么都不做，内核自己的活动也会碰到上百 MiB（§2 表里"刚建好"那一行）。

这件事对测量口径有直接影响。原生快照的基准脚本（`native_snapshot_bench.py`）在每一档恢复后都会把负载完整读一遍来验证内容。
这一读本身就让负载的全部页换入、被判脏，于是修复前六档导出的 memfile 恒在 440 MiB 左右，几乎不随档位变化
（数据见 [17](17-native-increment-cost-and-verification.md#2-体积修复前后)）。"验证内容"这个动作本身就在放大被测量。
[05](05-dirty-page-tracking-and-hdbss.md#53-与改动量无关的下限) 从成本模型的角度讲过同一个下限。

值得注意的是，这个判据虽然浪费，但**方向是安全的**：一个被写过的页一定已经驻留在 guest 内存里（写之前要先缺页填进来），
所以"驻留"是"写过"的超集。它只会多导，不会少导。后面会看到，这个性质让"驻留判据"成为所有退路的落脚点（[16](16-native-increment-fix.md#5-退路什么时候回到驻留判据)）。

### 4.3 x86 上为什么没有这个问题

x86 上换入的是干净页：读缺页填进来的页保留了写保护位，不算脏。所以同样是"每代新进程、工作集重新换入"，x86 的增量仍然只含写过的页。
**这个下限是 ARM 适配基线特有的退化，不是 e2b 的设计问题。** 本部分讲的修复，是把 ARM 这条线补回到 x86 本来就有的精确程度，
并没有超越 x86 原生。

---

## 5. 只换判据够不够：2 MiB 归并的地板

### 5.1 最直接的想法

正确的位图就在旁边，最直接的修法是：① 换成写跟踪位图，② 和 ③ 不动。

这能省多少？`rollback/scripts/probes/pb4.py` 算的就是这个：把 4 KiB 粒度的写跟踪位图按 2 MiB 归并（一个 2 MiB 块里只要有一个 4 KiB 页写过，
整块就算要导出），看导出量能降到多少。920B 上修复前的摘录如下（完整见 [17](17-native-increment-cost-and-verification.md#2-体积修复前后)）：

| 场景 | 修复前导出 | 只换判据（2 MiB 归并） | 4 KiB 真值 |
|---|---|---|---|
| 空转 | 162 MiB | 134 MiB | 4.1 MiB |
| 只读 192 MiB | 366 MiB | 148 MiB | 14.6 MiB |
| 只改 12 MiB | 184 MiB | 156 MiB | 20.5 MiB |
| 写 192 MiB | 382 MiB | 358 MiB | 219.8 MiB |

### 5.2 地板是怎么来的

看"空转"那一行：写跟踪只有 4.1 MiB，归并后却是 134 MiB，放大了三十多倍。原因是这 4 MiB 不是连成一片的，
它们散在 2 GiB 的 guest 物理地址空间里：内核的页表更新、slab 分配、定时器与调度器的记账、日志缓冲，各自落在不同的物理页上。
4.1 MiB 大约是 1050 个 4 KiB 页，落进了约 67 个不同的 2 MiB 块（134 ÷ 2），平均每块只有十几个页真被写过，剩下的四百多个页是"陪绑"导出的。

这就是**只换判据能到的地板**：只要 guest 还活着，每一代就有一百多 MiB 的"本底散写"，归并到 2 MiB 之后约 130 MiB，与改动量无关。

所以"只换判据"治好的是"读算脏"：对读多写少的负载收益很大（只读 192 MiB 那一行从 366 降到 148），
对写多的负载只省个位数百分比（写 192 MiB 那一行从 382 降到 358），而且**永远到不了写入量**。要精确，② 和 ③ 必须动。

### 5.3 粒度为什么被钉死

② 和 ③ 为什么不能直接把块改成 4 KiB？有三处顶着：

- **③ 的硬检查。** `NewUserfaultfdFromFd` 在起沙箱时逐个 region 比对 guest 页大小与 `BlockSize`，修复前要求二者相等，不等就报 `block size mismatch`。
  它守的是一个不变量："一次缺页恰好对应 header 里的一个块"：`faultPage` 调 `source.Slice(offset, pagesize)` 取**一个块**交给 `UFFDIO_COPY`。
- **③ 的填页单位是 guest 页。** guest 内存由 hugetlbfs 2 MiB 页背衬，内核一次缺页就是 2 MiB，`UFFDIO_COPY` 也必须给满 2 MiB。
  没办法只填其中 4 KiB、剩下的等下次再说。
- **② 的块大小逐代继承。** `ToDiffHeader` 用 `NextGeneration` 从父代 header 继承 `BlockSize`（`packages/shared/pkg/storage/header/metadata.go:78`），
  模板 header 是 2 MiB，之后每一代都是 2 MiB。

有一个办法能让三层天然都是 4 KiB：模板构建时关掉大页（`huge_pages=false`）。那样 guest 页是 4 KiB，`MemfilePageSize` 返回 4 KiB，
硬检查自然通过。代价是 guest 失去 2 MiB 大页的 TLB 收益，恢复时缺页次数乘 512：一个 400 MiB 的工作集，原来约 200 次缺页，
关大页后约 10 万次，每一次都要经 orchestrator 往返。这条路线可以验证"精确增量"的数字，但不适合交付，[16](16-native-increment-fix.md#2-两条路线) 会把它作为路线 A 与正式方案对比。

---

## 6. 本方案为什么天生精确，而这个做法学不过来

同一台机器上，本方案 checkpoint 的内存增量是精确的。它与原生差在哪里？

本方案的内存差分由 Firecracker 在**进程内**用 `dump_dirty` 按 4 KiB 页写进一个**稀疏文件**：写过的页写在它在 guest 内存里的原偏移上，
没写过的地方是文件空洞，`st_blocks` 只计真正写下去的页，粒度天然是 4 KiB。判据取自 KVM 或 HDBSS 的脏页日志，不经过 uffd。
恢复时 `restore_dirty` 在**同一个进程**里把回滚集的页直接写回活着的 guest 内存映射，也不经过 uffd
（[06](06-memory-diff-tree.md)、[09](09-in-place-rollback.md)）。所以本方案既没有 ① 的塌缩问题，也没有 ② ③ 那两把粒度锁。

原生路径不能照抄，因为它的用途不同。原生快照要支持**另起一个新沙箱、按需取页**，甚至在另一个节点上恢复。新节点上没有旧进程，
内存只能由 uffd 在缺页时从本地缓存或对象存储懒加载；差分也必须是"紧凑文件 + header 映射链"这种可以按偏移随机寻址、可以上传下载的形态。
把稀疏文件搬过去，等于放弃懒加载和跨节点，而那正是原生快照存在的理由（[18](18-native-and-checkpoint-together.md) 会展开两者的分工）。

所以正确的问法不是"怎么让原生像本方案一样存"，而是：

> **在保留映射链和 2 MiB 缺页的前提下，怎么让存储粒度降到 4 KiB？**

答案是：① 换成写跟踪位图；② 按 4 KiB 存；③ 缺页时把一个 2 MiB guest 页的 512 个 4 KiB 子页从映射链上逐个取来、拼成一整页再填。
三层各怎么改，是下一章的内容。

---

## 7. 改判据之前要先立的规矩：宁可多导，不可少导

在进入改法之前，先把一条贯穿后面三章的规矩立起来。

原生 pause 导出的内存差分是**相对模板（或上一代快照）**的。恢复时，没有被导出的页一律从父代读。所以：

- **多导一页**：这一页的内容本来就能从父代读到，重复存一份，只浪费空间和导出时间；
- **少导一页**：如果这一页其实被写过，恢复后它会回到父代的旧内容。guest 内存由两个时刻拼成，页表、链表、引用计数前后不一致，
  轻则数据错乱，重则 guest 内核在恢复后几十毫秒内崩溃。更糟的是，pause 本身会报成功，错误要到 resume 才暴露。

两个方向的代价完全不对称。所以任何判据改动都必须满足：**新判据导出的页集合是"真正写过的页"的超集。** 修复前的驻留判据满足这一点（§4.2），
只是超得太多。改成写跟踪位图之后，要论证它仍然是超集：写跟踪位图覆盖了哪些写、漏了哪些写，漏掉的那些由谁补上。
[16](16-native-increment-fix.md) 会逐项论证这件事；其中最不直观的一项，是沙箱做过 checkpoint 或 restore 之后，写跟踪位图会被清零，
必须另外补回（[16](16-native-increment-fix.md#4-与-checkpoint-叠加累积位图)）。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 原生 pause 的内存导出路径 | `packages/orchestrator/internal/sandbox/sandbox.go` — `Sandbox.Pause`（:912）、`pauseProcessMemory`（:1056） |
| 修复前的判据（驻留判据，至今仍是退路） | `packages/orchestrator/internal/sandbox/uffd/uffd.go` — `residentDiffMetadata`（:351）；`packages/orchestrator/internal/sandbox/fc/memory.go` — `DirtyMemory`（:23） |
| 两级判据的实现 | `firecracker/src/vmm/src/lib.rs` — `get_dirty_memory`（:882）；`firecracker/src/vmm/src/utils/pagemap.rs` — `is_page_dirty`（:86） |
| 被注释的写保护 | `packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go:380-387` |
| ③ 的校验与缺页 | 同上 — `NewUserfaultfdFromFd`（:56）、`faultPage`（:325） |
| ② 的块大小继承 | `packages/shared/pkg/storage/header/metadata.go` — `ToDiffHeader`（:52） |
| 模板块大小 | `packages/orchestrator/internal/template/build/config/config.go` — `MemfilePageSize`（:69） |
| 原生快照只在带 `mem_file_path` 时导出内存 | `firecracker/src/vmm/src/persist.rs:177-178` |
| 探针 | `rollback/scripts/probes/` — `pb2.py`、`pb4.py` |

---

## 本章要点

1. 原生 pause 的内存增量分三层：**① 判**（导出哪些页）、**② 存**（按 header 的 `BlockSize` 存进紧凑差分、映射链寻址）、
   **③ 填**（恢复时 uffd 按 guest 页缺页填）。三层的粒度被"guest 页 2 MiB → 硬检查 → header 继承"这根链条锁在 2 MiB 上。
2. 修复前不精确是三件事叠加：判据在 ARM 上塌缩成"驻留即脏"；每一代都是新进程，工作集全部重新换入；换入即算脏。
   结果是增量约等于工作集，与改动量无关。这是 ARM 适配基线的退化，x86 上没有。
3. 只换判据治好了"读算脏"，但 4 KiB 的本底散写被归并到 2 MiB 块后，每代约有 130 MiB 的地板；读多写少的负载省一半以上，写多的只省个位数百分比。
4. 粒度被三处钉死：uffd 的"页大小等于块大小"硬检查、hugetlbfs 一次缺页必须填满 2 MiB、header 的块大小逐代继承。关大页能绕开，但缺页次数乘 512，不适合交付。
5. 本方案天生精确，是因为它在进程内写稀疏文件、原地写回，不经过 uffd；原生要跨节点懒加载，映射链不能丢。正确的改法是 **4 KiB 存、2 MiB 拼**。
6. 判据改动的安全方向是**宁可多导，不可少导**：多导只费时间，少导会让恢复出来的 guest 内存由两个时刻拼成，而且 pause 自己报成功。
