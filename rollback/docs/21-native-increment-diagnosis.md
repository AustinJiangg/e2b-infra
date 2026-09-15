# 21 · 原生 snapshot 的增量为什么不精确

> ARM 适配版上，e2b 原生 `pause` / `create_snapshot` 每一代导出的内存量不随改动量变化：
> 改 12 MiB 也导出一百多 MiB。本篇从现象倒推到三层机制，解释为什么修复前不精确、
> 为什么「只换判据」修不到底，以及本方案天生精确的那个做法为什么学不过来。
>
> **读者**：工程师。想弄清「同一台机器上本方案精确、原生不精确」到底差在哪的人也适合读。
> **预备**：[第 4 篇 · e2b 原生 snapshot](04-e2b-native-snapshot.md)、
> [第 7 篇 · 脏页跟踪](07-dirty-page-tracking.md)、[第 8 篇 · 内存差分树](08-memory-diff-tree.md)。
> **代码**：`internal/sandbox/uffd/uffd.go`、`internal/sandbox/uffd/userfaultfd/userfaultfd.go`、
> `packages/shared/pkg/storage/header/metadata.go`；探针 `e2b-infra/rollback/scripts/probes/pb2.py`、`pb4.py`
>
> **与本书主线的关系**：本篇与随后的[第 22](22-native-increment-fix.md)、[23 篇](23-native-increment-cost-and-verification.md)
> 讲的是对**原生路径**的一处独立修复，不是本方案 checkpoint / restore 的一部分。
> 两者共用一个位图端点，不共用任何产物；本方案的代码在这次修复里一行没动
> （[第 23 篇 §3](23-native-increment-cost-and-verification.md#3-影响面只在原生路径的内存侧)）。

---

## 0. 本篇要回答的问题

1. 修复前的原生增量为什么与改动量无关？那个「下限」是怎么来的？
2. 「读也算脏」修掉之后，为什么导出量还是降不到写入量？
3. 是什么把差分块的粒度钉死在 2 MiB 上？
4. 本方案的差分天生是 4 KiB 精确的，为什么这个做法不能直接搬到原生路径上？

---

## 1. 现象：改 12 MiB，导出 148 MiB

先看数据。`rollback/scripts/probes/pb2.py` 把同一沙箱上三样东西并排：修复前原生 `pause` 用的判据
`GET /memory/dirty`、Firecracker 写跟踪位图 `PUT /snapshot/save-dirty-bitmap`、以及
`create_snapshot` 真正导出的 memfile 大小。950（HDBSS）上摘录三行
（完整表见[第 28 篇 表 3-K](28-results-and-compliance.md#表-3-k--pb2py-三列并排修复前后)）：

| 步骤 | `/memory/dirty` | 写跟踪位图 | 实际导出 |
|---|---|---|---|
| 刚建好 | 118.0 MiB | 4.6 MiB | 118.0 MiB |
| 只读 192 MiB，一字节没改 | 324.0 MiB | 15.0 MiB | 324.0 MiB |
| 只改 12 MiB | 148.0 MiB | 19.7 MiB | 148.0 MiB |

三个事实：

- **左列 = 右列**，逐行相等。原生 `pause` 就是按左列那份位图导出的，没有别的放大环节。
- **只读那行左列涨到 324 MiB**。读了 192 MiB、一个字节没改，导出量却涨了 192 MiB。
  判据把「读」算成了「脏」。
- **中间那列是对的**。刚建好 4.6、只读 15、只改 12 出 19.7，都是「写入量 + 几 MiB 系统本底」。
  这份位图正是本方案 checkpoint 用的那份（[第 7 篇 §5.4](07-dirty-page-tracking.md#54-本方案怎么绕开)）。

920B（KVM 写保护）上三行的形状完全一样，只是本底大几十 MiB。所以这不是某台机器的问题，
是 ARM 适配版原生路径的问题；而且**正确的位图就在旁边**，Firecracker 早就在算。

问题于是变成：为什么原生路径不看那份位图？看了就够了吗？

---

## 2. 原生内存增量的三层

[第 4 篇 §3](04-e2b-native-snapshot.md#3-内存增量) 讲过原生 `Pause` 的内存侧。
把它按「谁决定粒度」切成三层，每层都有一个决定粒度的锁：

| 层 | 做什么 | 粒度由谁决定 | 修复前 |
|---|---|---|---|
| ① **判** | 算出这一代哪些页要导出 | 判据接口返回的位图 | `GET /memory/dirty`：mincore 驻留 ∧ pagemap bit 57 未置 |
| ② **存** | 按块把脏页从 Firecracker 进程拷进紧凑差分文件，header 记「哪块在哪代的哪个偏移」 | header 的 `BlockSize`，逐代继承 | 模板 header 写死 2 MiB（`MemfilePageSize`，跟 guest 大页走） |
| ③ **填** | 恢复时 uffd 缺页，按 guest 页从映射链取内容 `UFFDIO_COPY` 进去 | guest 页大小，hugetlbfs 2 MiB | `NewUserfaultfdFromFd` 硬检查 `region.PageSize == BlockSize`，否则拒起 |

三层的粒度被一根链条锁在一起：guest 页 2 MiB ⇒ ③ 只会按 2 MiB 填 ⇒ ③ 要求 ② 的块等于 2 MiB
⇒ ② 的块写在 header 里逐代继承 ⇒ ① 无论多精细，最终也要归并到 2 MiB 块导出。

这就是后面所有分析的骨架：**① 决定「读算不算脏」，②③ 决定「最小能存多细」**。两件事分开治。

---

## 3. 修复前为什么不精确

### 3.1 判据：ARM 上塌缩成「驻留即脏」

上游 x86 的判据是 uffd 写保护位：读缺页填进来的页保留写保护，写缺页填进来的不保留，
之后 guest 写一个读进来的页会再陷一次故障、清掉保护位。于是「常驻且未保护」就是「写过」。

ARM 适配版上，arm64 6.6 内核没有 uffd 写保护，`UFFDIO_COPY_MODE_WP` 那两行被注释掉，
每次填页都清掉保护位。判据第二项恒真，塌缩成 `mincore` 已经回答过的「常驻」。
机制细节在[第 7 篇 §5.1–5.2](07-dirty-page-tracking.md#51-退化是怎么发生的)，这里不重复；
本篇只用它的结论：**修复前的位图 = 常驻页集合。**

### 3.2 两个放大：每代都是新进程，读也算脏

「常驻即脏」单独看还不至于每档 400 MiB。它和原生路径的另一个特性叠在一起才成了那个数：

1. **每一代都是一个新的 Firecracker 进程**。`create_snapshot` 打完快照停掉旧进程，从新快照拉起一个新的
   （[第 4 篇 §1](04-e2b-native-snapshot.md#1-两个-rpc)）。新进程内存是空的，guest 一跑起来，
   工作集全部要重新缺页换入。
2. **换入即被判脏**（§3.1）。

于是每代增量的下限 ≈ 这一代 guest **碰过**的全部页，与改了多少无关。
基准脚本每档恢复后都把 192 MiB 负载读一遍验证内容（`touch0`），那 192 MiB 每代都被换入、
每代都被算脏，所以 `native_snapshot_bench.py` 修复前六档的 memfile 恒在 440 MiB 左右
（[第 28 篇 表 3-J](28-results-and-compliance.md#表-3-j--native_snapshot_benchpy修复后920b-0914-native4k)「修复前」列）。
[第 7 篇 §5.3](07-dirty-page-tracking.md#53-与改动量无关的下限) 从成本模型的角度讲过同一件事。

### 3.3 只换判据够不够：2 MiB 块归并的地板

既然正确的位图就在旁边，最直接的想法是 ① 换成写跟踪位图、②③ 不动。
`rollback/scripts/probes/pb4.py` 算的就是这个：把 4 KiB 写跟踪位图归并到 2 MiB 差分块，看能降到多少。
920B 摘录（完整见[第 28 篇 表 3-K](28-results-and-compliance.md#表-3-k--pb2py-三列并排修复前后)）：

| 场景 | 修复前导出 | 只换判据（2 MiB 归并） | 4 KiB 真值 |
|---|---|---|---|
| 空转 | 162 MiB | 134 MiB | 4.1 MiB |
| 只读 192 MiB | 366 MiB | 148 MiB | 14.6 MiB |
| 只改 12 MiB | 184 MiB | 156 MiB | 20.5 MiB |
| 写 192 MiB | 382 MiB | 358 MiB | 219.8 MiB |

空转时写跟踪只有 4 MiB，但那 4 MiB 散在 2 GiB 地址空间里，落进约 66 个不同的 2 MiB 块，
归并后就是 134 MiB。这是**只换判据能到的地板**：每代 130 MiB 上下，与改动量无关。

所以「只换判据」治好的是「读算脏」，对读多写少的负载收益很大（366 → 148），
对写多的负载只有个位数百分比，而且**永远到不了写入量**。要精确，②③ 必须动。

### 3.4 粒度为什么被钉死

②③ 为什么不能直接把块改成 4 KiB？三个地方顶着：

- **③ 的硬检查**。`NewUserfaultfdFromFd` 起沙箱时逐个 region 比对 `region.PageSize != blockSize`，
  不等就报 `block size mismatch`。它保证的是「一次缺页填一个块」这个不变量：
  `faultPage` 调 `source.Slice(offset, pagesize)` 取**一个块**交给 `UFFDIO_COPY`。
- **③ 的填页单位是 guest 页**。guest RAM 由 hugetlbfs 2 MiB 背衬，内核一次缺页就是 2 MiB，
  `UFFDIO_COPY` 也必须给满 2 MiB。没法只填其中 4 KiB。
- **② 的块大小逐代继承**。`ToDiffHeader` 用 `NextGeneration` 从父代 header 继承 `BlockSize`，
  模板 header 是 2 MiB，之后每一代都是 2 MiB。

关大页（`huge_pages=false`）能让三层天然全是 4 KiB，但 guest 失去 TLB 收益，
恢复时缺页次数乘 512（400 MiB 工作集从约 200 次缺页变成约 10 万次，每次经 orchestrator 往返），
`touch1` 从几百毫秒量级升到秒级。这是[第 22 篇 §2](22-native-increment-fix.md#2-两条路线) 会讲的路线 A，
可验、不可交付。

---

## 4. 本方案为什么天生精确，而这个做法学不过来

本方案 checkpoint 的内存差分是 Firecracker 进程内 `dump_dirty` 按 4 KiB 页写进一个**稀疏文件**，
`st_blocks` 只算真写下去的页，粒度天然 4 KiB；恢复是 `restore_dirty` 在同一进程内把页搬回去，
根本不经过 uffd（[第 8 篇](08-memory-diff-tree.md)、[第 11 篇](11-in-place-rollback.md)）。
所以本方案没有 ②③ 那两把锁。

原生路径不能照抄，因为它的用途不同：原生快照要**跨节点另起新沙箱、按需取页**
（[第 20 篇 §2](20-vs-native.md#2-逐项对比)）。新节点上没有旧进程，内存必须由 uffd 按缺页从对象存储懒加载，
差分必须是「紧凑文件 + header 映射链」这种能被随机寻址的形态。
把稀疏文件搬过去，等于放弃懒加载和跨节点 —— 那是原生快照存在的理由。

所以正确的问法不是「怎么让原生像本方案一样存」，而是「**在保留映射链和 2 MiB 缺页的前提下，
怎么让存储粒度降到 4 KiB**」。答案是：② 按 4 KiB 存，③ 缺页时把 512 个 4 KiB 子页拼成一个 2 MiB 页再填。

判据那一层（①）本来就该换成写跟踪位图，而 ②③ 既然必须动，改法就是下一篇的内容。

---

## 5. 小结

1. **修复前不精确是三层叠加**：判据在 ARM 上塌缩成「驻留即脏」；每代新进程让工作集全部重新换入；
   换入即算脏。结果是增量 ≈ 工作集，与改动量无关。
2. **只换判据修不到底**：差分块 2 MiB 被 uffd 硬检查钉在 guest 页大小上，4 MiB 散写归并成 130 MiB。
   读多写少的负载省一半以上，写多的只省一成。
3. **本方案天生精确是因为它没有那两把锁**（进程内稀疏文件、不经 uffd），但原生要跨节点懒加载，
   映射链不能丢。正确的改法是 **4 KiB 存、2 MiB 拼**。

---

## 思考题

1. §1 的三列里，「只读 192 MiB」那行左列比上一行涨了 192 MiB，中间列只涨了约 10 MiB。
   如果把负载换成「读 192 MiB 然后立刻 `madvise(MADV_DONTNEED)` 掉一半」，三列各会怎么变？
2. §3.2 说下限 ≈「这一代 guest 碰过的全部页」。基准脚本的 `touch0` 是为了验证内容正确性而读一遍负载。
   如果去掉 `touch0`，修复前的 memfile 会降到多少？这说明测量口径本身对结论有什么影响？
3. §3.3 的地板是「散写落进多少个 2 MiB 块」决定的。同样是写 4 MiB，什么样的地址分布会让归并后的
   导出量最小、什么样最大？两者相差几倍？

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 修复前的判据 | `internal/sandbox/uffd/uffd.go` — `DiffMetadata`、`DirtyMemory` |
| 两级判据的实现 | `src/vmm/src/lib.rs` — `get_dirty_memory`；`src/vmm/src/utils/pagemap.rs` — `is_page_dirty` |
| 退化的那一行 | `internal/sandbox/uffd/userfaultfd/userfaultfd.go` — 被注释的 `UFFDIO_COPY_MODE_WP` |
| ③ 的硬检查与缺页 | `internal/sandbox/uffd/userfaultfd/userfaultfd.go` — `NewUserfaultfdFromFd`、`faultPage` |
| ② 的块大小继承 | `packages/shared/pkg/storage/header/metadata.go` — `ToDiffHeader` |
| 探针 | `e2b-infra/rollback/scripts/probes/` — `pb2.py`、`pb4.py` |
| 设计稿与实施记录 | 工作区 `e2b-repo/原生快照精确增量-方案设计.md`、`原生快照增量判据-修复方案与验证计划.md` |

**下一篇**：[22 · 原生 snapshot 的精确增量：4 KiB 存、2 MiB 拼](22-native-increment-fix.md) ——
诊断说清楚了 ②③ 必须动，接下来是三层各改了什么。
