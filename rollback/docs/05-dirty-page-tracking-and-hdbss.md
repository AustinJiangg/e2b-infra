# 05 · 脏页跟踪与 HDBSS

## 本章目标

"只存改动量"的前提是**知道改了哪些页**。读完本章，你应当能够：

- 说出在虚拟化下判断"一页被写过"的三种机制（KVM 写保护、uffd 写保护、HDBSS），以及它们的代价为什么差两个数量级；
- 解释为什么判据的方向是"宁可多记、不可漏记"，漏记一页会造成什么后果；
- 说清 KVM 脏页日志为什么是**破坏性读**，本方案在哪三处因此"先落地、再做可能失败的事"；
- 说清 HDBSS 的启用分哪两层、要满足哪六个前提，哪些条件不满足时会**静默**失效；
- 精确说出"读也算脏"这个判据差异指什么、是怎么产生的，以及本方案为什么不受它影响（全书只在本章定义它）。

上一章（[04](04-architecture.md)）给了全局分工：脏页信息由最底层的 KVM / CPU 产生，经 Firecracker 交给 orchestrator 的账本。
本章讲这一层。下一章（[06](06-memory-diff-tree.md)）用本章产出的脏页位图组织多代内存状态。

主要代码：orchestrator 侧 `packages/orchestrator/internal/sandbox/fc/dirtytracking.go`；Firecracker 侧
`firecracker/src/vmm/src/vstate/{vm,memory}.rs`、`arch/aarch64/vm.rs`、`builder.rs`、`rollback.rs`。

---

## 1. 为什么这是地基

两件事压在脏页跟踪上：

**成本模型。** [06](06-memory-diff-tree.md) 里每一代产物是 O(本代脏页)。如果"脏页"的判据不准，把没写过的页也算进去，
这个 O 就退化了 —— 极端情况下退化到 O(内存)，整套方案的价值归零，而且是**静默**归零：功能全对，只是慢。

**正确性。** 回滚集的正确性证明（[06](06-memory-diff-tree.md) §4.2）依赖一条不变量：

> 纪元位图 `E_x` 恰好覆盖 `(parent(x), x]` 之间被写过的页。

位图**漏记**一页，回滚就会漏一页 —— guest 内存里混进一页来自另一个时刻的数据，而虚机照常运行，这是静默损坏。
位图**多记**只是浪费，多写回几页而已，不影响正确性。

所以判据的方向性很重要：**宁可多记，不可漏记**。后面会看到，ARM 适配基线恰恰是"多记到极致"——
它不出正确性问题，它只是把成本模型毁了。

---

## 2. 怎么知道一页被写过

Guest 的地址翻译在虚拟化下是两层：

```
Guest 虚拟地址
   │  Stage-1（guest 内核自己管的页表）
   ▼
Guest 物理地址 / IPA
   │  Stage-2（宿主 KVM 管的页表）
   ▼
宿主物理地址
```

我们要跟踪的是**中间那一层**：哪些 guest 物理页被写过。guest 写内存时不经过任何宿主代码，宿主软件看不见这次写；
能看见的只有管 Stage-2 页表的 KVM，或者 CPU 自己。于是有三条路。

### 2.1 KVM 写保护 + 脏页日志

最经典的做法，也是 KVM 的标准能力：

1. VMM 给内存槽（memslot）打上 `KVM_MEM_LOG_DIRTY_PAGES`；
2. KVM 把 Stage-2 页表里所有页设成**只读**；
3. guest 第一次写某页 → 权限故障 → **VM-Exit**（虚机退出到宿主内核）→ KVM 在脏页位图里置位、把该页改回可写、返回 guest；
4. VMM 用 `KVM_GET_DIRTY_LOG` 取走位图。

代价模型很清楚：**每个干净页的第一次写，付一次 VM-Exit。** 之后该页就一直可写，不再有开销，直到下一次取位图重新写保护。
对一个会打快照的虚机，这个代价摊到快照收益里通常划算；对一个**从不打快照**的虚机，这是纯亏损。

### 2.2 userfaultfd 写保护

e2b 在 x86 上用的做法（原生路径，[02](02-e2b-native-snapshot.md) §3.1）。内存不是普通匿名内存，而是注册给 userfaultfd 的区域，
缺页由用户态 handler 填充（这样内存可以按需从对象存储拉取）。在 `UFFDIO_COPY` 填页时可以**选择保留写保护位**：

- 因**读**缺页而填的页 → 带 `UFFDIO_COPY_MODE_WP`，页仍是写保护的；
- 因**写**缺页而填的页 → 不带，页可写。

之后 guest 若去写一个"因读而填"的页，会再触发一次写保护故障，handler 清掉 WP 位。于是 `/proc/self/pagemap` 里那一页的
**bit 57（uffd 写保护）** 成了一个准确的信号：

```
这一页是脏的  ⟺  present（bit 63）= 1  且  uffd-wp（bit 57）= 0
```

代价是读缺页之后若确实被写，要多走一次用户态故障往返。收益是判据**精确** —— 只读不写的页不算脏。

### 2.3 HDBSS：硬件标脏

**HDBSS**（Hardware Dirty state tracking Structure）是 ARMv9.5 的能力，鲲鹏 950 实现了它。它工作在 **Stage-2** 这一层：

```
Guest 写入某个 GPA
    │
    ▼  Stage-2 PTE 上启用了 DBM（Dirty Bit Modifier）
CPU 直接把脏 GPA 写进该 vCPU 的 HDBSS buffer         ← 硬件完成，无陷出
    │
    ▼  vCPU 发生 VM-Exit（任何原因）
KVM 遍历 HDBSS buffer，逐条 kvm_vcpu_mark_page_dirty()
    │
    ▼
标准 KVM memory-slot dirty bitmap
    │
    ▼
VMM 调用 KVM_GET_DIRTY_LOG
```

关键在于**没有写保护，也就没有为标脏而生的 VM-Exit**。CPU 顺手记一笔，等到 vCPU 因为别的原因退出时，内核再把 buffer 里的记录
汇总进标准位图（下表：VM-Exit 时刷新 buffer，dirty-log 同步时也会处理）。打快照、回滚前都先暂停虚机，暂停本身就让每个 vCPU 退出 `KVM_RUN`，
所以即使一个几乎不产生 VM-Exit 的纯计算 guest，在"暂停之后取图"那一刻 buffer 里的记录也已经汇总进位图。

它**不是**什么，同样重要：

- 不是独立的快照系统：只记录"哪些页被改了"，不保存 vCPU / 设备状态，不生成快照文件；
- **没有独立的位图获取接口**：最终仍走 `KVM_GET_DIRTY_LOG`；
- 对 VMM 几乎透明：软件写保护和 HDBSS 喂给上层的是**同一个位图**，语义一致，只差性能。

第三条是好事也是坏事：代码不用为两条路径分叉，**但降级完全静默**，只能从延迟和能力上报里看出来（§4.6 专门处理这一点）。

还有一个推论：每个 vCPU 一个 buffer，有容量上限。buffer 写满会触发额外的处理，写密集负载下这部分开销可能把收益吃掉一部分（§4.5）。

内核侧实现位置（OLK 6.6）：

| 关注点 | 位置 |
|---|---|
| capability 检测、启用、释放、dirty-log 同步 | `arch/arm64/kvm/arm.c` |
| 对启用 dirty logging 的 Stage-2 映射设置 DBM | `arch/arm64/kvm/mmu.c` |
| VM-Exit 时刷新 HDBSS buffer | `arch/arm64/kvm/handle_exit.c` |
| 加载 HDBSS 的 EL2 寄存器 | `arch/arm64/include/asm/kvm_mmu.h` |
| 标准 dirty bitmap 与 `KVM_GET_DIRTY_LOG` | `virt/kvm/kvm_main.c` |
| buffer 大小编码定义 | `arch/arm64/tools/sysreg` |

### 2.4 三者对比

| | 判据来源 | 每页首次写的代价 | 精度 | 可用性 |
|---|---|---|---|---|
| KVM 写保护 | Stage-2 权限故障 | 一次 VM-Exit | 精确（只有写才触发） | 任何 KVM |
| userfaultfd 写保护 | pagemap bit 57 | 一次用户态故障往返 | 精确 | 内存由 uffd 托管，且内核支持 uffd 写保护 |
| **HDBSS** | CPU 写 buffer + VM-Exit 时汇总 | **≈ 0** | 精确 | ARMv9.5 + 内核支持 + VHE |

本方案用的是第一和第三种：有 HDBSS 用 HDBSS，没有就退回 KVM 写保护。两者对上层是同一个位图，所以下文说"KVM 脏页日志"时两者都包括。

---

## 3. KVM 脏页日志的语义

### 3.1 破坏性读

`KVM_GET_DIRTY_LOG` **取走并清空**位图。这是标准语义 —— 否则调用方无法区分"上次之后新脏的"和"历史上脏过的" ——
但它意味着：

> **每一次读，都是一次不可撤销的信息转移。** 读完之后如果后续步骤失败，"这些页脏过"的信息就永远消失了。

在本方案里，丢掉这份信息意味着下一次增量会漏页（§1 的正确性问题）。这条约束出现在三处，处理模式一样 ——
**先把易失的信息落到不易失的地方，再做可能失败的事**：

| 场合 | 处理 | 代码 |
|---|---|---|
| 写差分快照 | `dump_dirty` 写文件失败时把 KVM 位图折回用户态位图；成功才 `reset_dirty` | `firecracker/src/vmm/src/vstate/memory.rs:308-312` |
| 原地回滚阶段 3 | 取图后立刻 `store_dirty_bitmap` 折回，再往下走 | `firecracker/src/vmm/src/rollback.rs:332-342` |
| `save-dirty-bitmap` | 取图 → 折回 → 再序列化写文件 | `rollback.rs:796-825` |

### 3.2 两层位图

Firecracker 侧其实有两份脏页记录：

| 位图 | 在哪 | 谁写 |
|---|---|---|
| KVM 位图 | 内核，每 memslot 一份 | KVM（写保护故障 / HDBSS 汇总） |
| 用户态位图 | Firecracker 进程，每 region 一个 | `store_dirty_bitmap` 折回；Firecracker 自己写 guest 内存时也标 |

`dump_dirty`（`memory.rs:246`）写快照时取的是**两者的并集**：一页只要在任一份里是脏的，就写进差分文件，并在返回的 `merged` 位图里置位。

用户态那一份不只是备份。**Firecracker 自己写 guest 内存时（设备 DMA、virtio 队列操作）不经过 Stage-2 故障**，KVM 不知道，
要由 Firecracker 自己标。如果只取 KVM 位图，这类页就会漏掉 —— 例如 virtio 设备刚往 guest 的接收缓冲写了一个网络包，
这一页的新内容不在任何纪元位图里，回滚时就不会被写回。原地回滚阶段 9 的"队列页重新标脏"也是这一类（[09](09-in-place-rollback.md)）。

### 3.3 三个函数的关系

| 函数 | 做什么 | 什么时候 |
|---|---|---|
| `store_dirty_bitmap(kvm_bitmap)`（`memory.rs:408`） | 把 KVM 位图 OR 进用户态位图 | 取图之后立刻 |
| `dump_dirty(writer, kvm_bitmap)`（:246） | 按并集把脏页写进文件，返回 merged；成功则清用户态位图 | 写差分快照 |
| `reset_dirty()`（:399） | 清空用户态位图 | 快照成功后、回滚阶段 9 |

`dump_dirty` 返回的 `merged`，就是随快照落地的那个**纪元位图** `E_x`（格式 FCDB 见 [08](08-firecracker-api-contract.md)，
它在差分树里的作用见 [06](06-memory-diff-tree.md)）。全量快照不走 `dump_dirty`，直接写全 1 位图（`firecracker/src/vmm/src/vstate/vm.rs:519-530`）。

---

## 4. HDBSS 怎么被启用

### 4.1 先分清两层

这里最容易混的一点：**脏页这件事分两层，HDBSS 只是第二层。** 混淆这两层是部署时最常见的配置错误。

| 层 | 管什么 | 谁决定 | 什么时候定 | 开关 |
|---|---|---|---|---|
| ① | **要不要记脏页** | orchestrator，写进 Firecracker boot config 的 `track_dirty_pages` | 虚机启动那一刻 | `FC_TRACK_DIRTY_PAGES` |
| ② | **用什么方式记** | Firecracker 自己运行时探测 | 虚机构建时 | 无开关：能开 HDBSS 就开，不能就退回写保护（`FC_HDBSS_REQUIRED` 可改成"不能就失败"） |

两层的失效表现完全不同：

- **第一层关着** → Firecracker 根本不产生脏位图 → checkpoint 算不出增量 → 每次抓走整份 guest 内存（`memMode=full`）。
  **不报错，测试照样通过，测的却不是增量。**
- **第二层退回软件路径** → 结果一模一样（都是 `KVM_GET_DIRTY_LOG` 位图），只是每个干净页首次写多一次 VM-Exit。**只有延迟能看出来。**

开关的取值与默认值见 [27 §2.1](27-configuration-and-capacity.md#21-脏页跟踪与-hdbss)，三种机器上各该怎么设见 [26 §1](26-deployment-prerequisites.md#1-先分清两层要不要记脏页用什么方式记)。

### 4.2 两侧各做一件事

**orchestrator** 启动时探测一次，决定这台机器上的虚机默认要不要武装脏页跟踪（`internal/sandbox/fc/dirtytracking.go:109` `hardwareDirtyTracking`）：

```go
func hardwareDirtyTracking() bool {
    f, err := os.OpenFile("/dev/kvm", os.O_RDWR, 0)
    if err != nil { return false }
    defer f.Close()
    ret, _, errno := unix.Syscall(unix.SYS_IOCTL, f.Fd(),
        kvmCheckExtension, kvmCapArmHWDirtyStateTrack)   // 0xAE03, 502
    return errno == 0 && ret > 0
}
```

`KVM_CHECK_EXTENSION` **只查询**：不建 VM、不启用任何东西；没有 `/dev/kvm` 的机器上安全地返回 false。
判定逻辑在 `decideTrackDirtyPages`（:68）：变量未设置时跟硬件走；能被 `strconv.ParseBool` 解析就照办；解析不了则忽略并跟硬件走。
这个决策是**整个 orchestrator 一个值**（包级变量，启动时算一次），不是每沙箱一个。做成"按沙箱按需武装"需要在创建沙箱时就知道它会不会打
checkpoint，而目前的 API 里没有这个信息；这是一个明确的可扩展点（[B](B-extending.md)）。

**Firecracker** 在 `Vm::setup_dirty_tracking()`（`vstate/vm.rs:206`）里真正武装：region 没有 bitmap 就是 `off`；
aarch64 上调 `enable_hdbss(order)`（`arch/aarch64/vm.rs:66`，对 VM fd 做 `KVM_ENABLE_CAP`，cap 502）。成功记为 `hdbss`；
失败时若要求必须 HDBSS 则构建失败，否则退回 `kvm-wp`。

```rust
const KVM_CAP_ARM_HW_DIRTY_STATE_TRACK: u32 = 502;
const KVM_ENABLE_CAP: u64 = 0x4068_AEA3;      // openEuler 的 _IOW(KVMIO, 0xa3, struct kvm_enable_cap)

let mut cap = kvm_bindings::kvm_enable_cap::default();
cap.cap = KVM_CAP_ARM_HW_DIRTY_STATE_TRACK;
cap.args[0] = order;                           // buffer 大小编码
```

ioctl 号在代码里写死（:68-71），因为 502 是 openEuler 的厂商扩展，Firecracker 用的上游 `kvm-bindings` 里没有这个常量。

为什么探测能在不建 VM 的前提下完成、启用却不能？因为 `KVM_CHECK_EXTENSION` 是在 `/dev/kvm` 这个 KVM fd 上问"内核支不支持"，
而 `KVM_ENABLE_CAP` 是在某台具体虚机的 VM fd 上改它的状态，而且要为它已有的 vCPU 分配 buffer（§4.3 第 6 条）。
所以 orchestrator 启动时的探测只能证明"能力存在"，证明不了"开成功了"——后者要看 Firecracker 的自报（§4.6）。

### 4.3 六个前提

| # | 前提 | 不满足时 | 静默？ |
|---|---|---|---|
| 1 | 内核编译了 `CONFIG_ARM64_HDBSS` | `KVM_CHECK_EXTENSION(502)` 返回 0 | 否，能探到 |
| 2 | CPU 实现了 HDBSS | 同上 | 否 |
| 3 | KVM 运行在 **VHE** 模式 | OLK 实现明确拒绝 non-VHE，`ENABLE_CAP` 失败 | 否 |
| 4 | memslot 带 `KVM_MEM_LOG_DIRTY_PAGES` | 不记录脏页 | Firecracker 在 region 有 bitmap 时自动带上 |
| 5 | **没有**同时使用 KVM dirty ring | 两者不兼容 | Firecracker 不用 dirty ring |
| 6 | **`KVM_ENABLE_CAP` 在 vCPU 创建之后调用** | **一条脏页都记不到** | **是** |

第 6 条最危险。OLK 的 `kvm_cap_arm_enable_hdbss()` **只为调用时已经存在的 vCPU 分配 buffer**：

```
✗ 错误：  KVM_CREATE_VM → KVM_ENABLE_CAP(HDBSS) → KVM_CREATE_VCPU
✓ 正确：  KVM_CREATE_VM → KVM_CREATE_VCPU × N
                        → 注册带 KVM_MEM_LOG_DIRTY_PAGES 的 memory slot
                        → KVM_ENABLE_CAP(HDBSS)
                        → 启动 vCPU
```

错误的顺序**不会报错**：ioctl 返回成功，只是没有任何 vCPU 拿到 buffer，脏页一条也记不到。代码靠调用点位置保证：
`setup_dirty_tracking()` 在 `create_vmm_and_vcpus()` 与 `register_memory_regions()` **之后**调用
（`firecracker/src/vmm/src/builder.rs:229-239`、:425-438）。**这个约束没有断言守着**，挪动调用点是静默的破坏性改动
（不变量清单见 [12](12-failure-semantics.md)）。

### 4.4 引导和加载两条路径都要武装

HDBSS **不会被快照继承**：从快照恢复会创建新的 KVM VM 和新的 vCPU。所以 `setup_dirty_tracking()` 有两个调用点：

| 路径 | 函数 | 位置 |
|---|---|---|
| 冷启动 | `build_microvm_for_boot` | `builder.rs:199`，调用在 :239 |
| **从快照加载** | `build_microvm_from_snapshot` | `builder.rs:414`，调用在 :438 |

第二条是关键：e2b 的用户沙箱一律由快照加载而来（[01](01-background.md) §5.2），只改 machine-config、只管冷启动路径，等于完全没开。

### 4.5 buffer 大小

`cap.args[0]` 是**大小编码**，不是字节数（4 KiB 宿主页下它等于 `alloc_pages()` 的 order），**每个 vCPU 一份**：

| 编码 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| 每 vCPU buffer | 8 KiB | 16 KiB | 32 KiB | 64 KiB | 128 KiB | 256 KiB | 512 KiB | 1 MiB | 2 MiB |

默认 1（8 KiB，也是内核默认，`vstate/vm.rs:220-225`，由 `FC_HDBSS_ORDER` 覆盖）。一条记录 8 字节，8 KiB 约 1000 条，
也就是约 4 MiB 的脏页覆盖量（4 KiB 页）。写密集负载下 buffer 会填满、触发额外处理，极端情况下可能吃掉"省掉写保护陷出"的一部分收益。
编码 9 能容纳约 26 万条，但要为**每个 vCPU** 分配 2 MiB **连续物理内存**，在长期运行、内存碎片化的宿主上不是免费的。

### 4.6 能力上报

因为降级完全静默（§2.3），必须主动把"这台机器实际在用哪条路"说出来。两侧都报：

- **Firecracker** 在 instance info 里报 `dirty_tracking`：`DirtyTrackingBackend { Off, KvmWriteProtect, Hdbss }`，
  字符串为 `off` / `kvm-wp` / `hdbss`（`vstate/vm.rs:48-66`）。HDBSS 开成功时内核日志有 `Enable HDBSS success`。
- **orchestrator** 启动时打 `checkpoint capabilities` 一行，带判定结果**和理由**（`packages/orchestrator/main.go` 的 `reportCheckpointCapabilities`）。
  理由的三种写法：`hardware dirty state tracking present (KVM capability 502)`、
  `no hardware dirty state tracking; software tracking costs a VM exit per clean page`、`FC_TRACK_DIRTY_PAGES="true"`
  （`dirtytracking.go:92-104`、:73-78）。跟踪关着时另打一条 WARN。

"一个部署悄悄丢了增量 checkpoint，应该能从日志里读出原因，而不是只表现为慢"—— 这是这段代码的全部动机。
字段与日志的完整列表见 [28](28-observability-reference.md)，部署后怎么逐项核对见 [26](26-deployment-prerequisites.md)。

### 4.7 默认值为什么跟着硬件走

因为**代价跟着硬件走**：

| 硬件 | 武装的代价 | 对从不 checkpoint 的沙箱 |
|---|---|---|
| 有 HDBSS | 几乎为零 | 无所谓，默认开 |
| 无 HDBSS | 每个干净页首次写一次 VM-Exit | 纯亏损，默认关 |

一台 950 上，能做增量 checkpoint 的沙箱开箱就在做；一台无 HDBSS 的机器上，不打 checkpoint 的沙箱不用为别人的功能买单。
在没有 HDBSS 的机器上不设变量，checkpoint 照样能做，只是每次都是全量；要得到增量，显式设 `FC_TRACK_DIRTY_PAGES=true`
（退化为 KVM 写保护，仍是增量）。

---

## 5. 判据差异：读也算脏

**这一节是理解本方案价值的前提，全书只在这里定义。** 三个东西必须分清，否则很容易得出一个错误的结论：

| | 脏页怎么判定 | 结果 |
|---|---|---|
| **e2b x86 原生** | 读缺页填充时保留 uffd 写保护位，写缺页则清除；快照时取"已存在且未写保护"的页 | 增量**精确** |
| **ARM 适配基线** | 该写保护路径在 arm64 上走不通，被注释掉；判据第二项恒真 | 增量**退化**：凡被换入过的页都算脏，**读也算脏** |
| **本方案** | 不经过 uffd，直接取 KVM / HDBSS 的脏页日志 | ARM 上**恢复精确增量** |

> **必须明确：这是修复 ARM 适配引入的退化，不是超越 x86 原生。** 在 x86 上 e2b 的增量本来就是精确的。

### 5.1 退化是怎么发生的

ARM 适配版里，`UFFDIO_COPY` 的写保护模式被注释掉了（`packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go:380-387`）：

```go
// Performing copy() on UFFD clears the WP bit unless we explicitly tell
// it not to. We do that for faults caused by a read access. Write accesses
// would anyways cause clear the write-protection bit.
//if accessType != block.Write {
//	copyMode |= UFFDIO_COPY_MODE_WP
//}

copyErr := u.fd.copy(addr, pagesize, b, copyMode)   // copyMode 不带 WP
```

于是**每一次填页都会清掉 WP 位**，无论触发它的是读还是写。

### 5.2 判据因此塌缩

取脏页位图的 `GET /memory/dirty` 是两级的（`firecracker/src/vmm/src/lib.rs:882` `get_dirty_memory`）：

```rust
// 第一级：mincore 一次筛出整个 region 的常驻页
let resident_bitmap = mincore_bitmap(base_addr, len, page_size)?;

// 第二级：只对常驻页读 pagemap
for page_idx in 0..nr_pages {
    if is_resident(page_idx) {
        if pagemap.is_page_dirty(virt_addr)? {   // present && !write_protected
            slot_bitmap[..] |= ...;
        }
    }
}
```

第二级的判据是 `is_present() && !is_write_protected()`。§5.1 的结果是 **bit 57 恒为 0**，所以 `!is_write_protected()` 恒真，
判据塌缩成 `is_present()` —— 也就是 mincore 已经回答过的那个问题。两个后果：

1. **语义上**：位图 = 常驻页集合，"被读进来过"和"被写过"不再有区别；
2. **性能上**：第二级成了纯开销 —— 对每一个常驻页做一次 `pread`，算出一个已经知道的答案（这条路径还要求放行 VMM 线程的 `pread64` seccomp 白名单）。

### 5.3 "与改动量无关的下限"

现在可以解释一个反直觉的现象：在 ARM 适配基线上，即使两次原生 snapshot 之间什么都没做，增量也不会接近零。两件事叠加：

1. **原生 `Checkpoint` RPC 会重建沙箱** —— 打完快照停掉旧进程，从新快照拉起一个新 Firecracker（[02](02-e2b-native-snapshot.md) §1）。
   新进程的内存是空的，guest 一跑起来就要把工作集重新缺页换入；
2. **换入即被判脏**（§5.2）。

于是下一次原生增量的下限 ≈ **整个常驻工作集**，与"这段时间改了多少"无关。x86 上第 2 条不成立：换入的是干净页（保留了 WP 位），
不计入增量。所以这个下限是 ARM 适配基线特有的，不是 e2b 的设计问题。

**原生路径上的这个问题在本书交付的代码里已经修复**：原生 pause 在跟踪开着时改用 Firecracker 的写跟踪位图判脏。
但"只换判据"并不能把原生增量降到写入量，还要改存储粒度和缺页填充 —— 为什么，以及怎么改，是第四部分的内容：
诊断见 [15](15-native-increment-diagnosis.md)，改法见 [16](16-native-increment-fix.md)。本章只负责定义"读也算脏"这个判据差异本身。

### 5.4 本方案怎么绕开

本方案根本不走 uffd 这条判据。它取的是 **KVM / HDBSS 的脏页日志** —— 一个由硬件或内核在 Stage-2 层面维护的、真正意义上的"被写过"的记录。
读一页不会让它在 Stage-2 上变脏，所以"读也算脏"对本方案不成立。

副产品是：这条路径也**不需要**沙箱的内存由 uffd 托管。差分快照、位图侧车、原地回滚全都只依赖 KVM 的能力。

---

## 6. 精确性的代价

### 6.1 软件写保护的代价模型

设工作集 W 页，其中一次快照周期内被写的有 D 页。写保护路径的额外开销 ≈ **D 次 VM-Exit**（每个干净页第一次写各一次）：

- **D 小**（改动少）：开销小，收益大（差分小）。划算。
- **D ≈ W**（写密集）：付了 W 次 VM-Exit，差分也接近全量。**不划算** —— 这种负载下增量快照本来就没什么可省的。
- **从不快照**：付了 D 次 VM-Exit，收益为零。**纯亏损**，所以无 HDBSS 时默认关。

### 6.2 HDBSS 的 buffer 与溢出

HDBSS 把"每页一次 VM-Exit"换成了"CPU 顺手记一笔 + 定期汇总"，代价转移到 buffer 溢出处理（§4.5）。
`FC_HDBSS_ORDER` 就是为这件事准备的。各档在写密集负载下的对比**尚未实测**，是明确列出的缺口之一（[21](21-benchmarks-and-compliance.md)）；目前用默认值 1。

### 6.3 什么时候不该开

| 场景 | 建议 |
|---|---|
| 沙箱从不打 checkpoint，宿主无 HDBSS | 关（默认行为） |
| 沙箱从不打 checkpoint，宿主有 HDBSS | 开着无妨，代价接近零 |
| 写密集且很少 checkpoint | 关；增量在这种负载下本来就省不下什么 |
| 要做回滚的生产部署 | 开，并设 `FC_HDBSS_REQUIRED=true`：**宁可启动失败，也不要静默走软件路径** |

最后一条值得强调：静默降级是这套系统里**最难发现的故障模式**。功能全对、测试全过、只是慢 —— 而且慢的幅度取决于负载，不容易触发告警。

---

## 本章要点

1. 判据的精确性同时决定成本（多记 = 慢）和正确性（漏记 = 静默损坏），方向是**宁可多记，不可漏记**。
2. 三种机制：KVM 写保护（每页首写一次 VM-Exit）、uffd 写保护（用户态故障）、**HDBSS（≈ 零成本）**。HDBSS 最终也走 `KVM_GET_DIRTY_LOG`，
   **接口一样、只有代价不同**，所以降级完全静默，必须靠能力上报说出来。
3. KVM 脏页日志是**破坏性读**：取图之后先折回用户态位图，再做可能失败的事；用户态位图还记着 Firecracker 自己写 guest 内存的页，所以快照取两者的并集。
4. 脏页分两层：**要不要记**（orchestrator 决定，`FC_TRACK_DIRTY_PAGES`）和**用什么方式记**（Firecracker 自动探测）；第一层关着时每次都是全量且不报错。
5. HDBSS 六个前提，其中"必须在 vCPU 创建之后启用"不满足时完全静默；它不被快照继承，引导与加载两条路径都要武装。
6. **读也算脏**是 ARM 适配基线的退化：写保护位被注释掉，判据塌缩成"常驻即脏"，叠加原生每代新进程，增量下限约等于工作集。
   本方案直取 KVM / HDBSS 日志，恢复精确增量，不是超越 x86 原生；原生路径的修复在 15–16。
7. 默认值跟着硬件走，因为代价跟着硬件走；生产回滚部署应设 `FC_HDBSS_REQUIRED=true`，宁可启动失败。
