# 07 · 脏页跟踪

> 「只存改动量」的前提是**知道改了哪些页**。这件事在 ARM 上有三种做法，代价差两个数量级，
> 而且其中一种在我们的代码基线上已经退化了。本篇把这三种讲清楚，并说明本方案怎么选、怎么降级。
>
> **读者**：工程师、系统工程师。
> **预备**：[第 6 篇 · 总体架构](06-architecture.md)。知道 KVM、页表、VM-Exit 是什么会读得更顺，
> 但不知道也能读 —— [§2](#2-怎么知道一页被写过) 会铺垫。
> **代码**：`internal/sandbox/fc/dirtytracking.go`、`src/vmm/src/vstate/vm.rs`、
> `src/vmm/src/arch/aarch64/vm.rs`、`src/vmm/src/vstate/memory.rs`

---

## 0. 本篇要回答的问题

1. 「这一页被写过」这件事，硬件和内核是怎么知道的？有几种做法？
2. KVM 的脏页日志为什么是**破坏性读**？这带来了什么约束？
3. HDBSS 是什么，怎么启用，为什么启用时序很讲究？
4. 为什么说本方案「恢复了精确增量」，而不是「比 e2b 原生更精确」？
5. 什么时候**不该**开脏页跟踪？

---

## 1. 为什么这是全书的地基

两件事都压在脏页跟踪上：

**成本模型。** [第 8 篇](08-memory-diff-tree.md)里每一代产物是 O(本代脏页)。
如果「脏页」的判据不准，把没写过的页也算进去，这个 O 就退化了 —— 极端情况下退化到 O(内存)，
整套方案的价值归零，而且是**静默**归零：功能全对，只是慢。

**正确性。** [第 8 篇 §4.3](08-memory-diff-tree.md#43-正确性) 那个证明的第 2、3 步依赖一条不变量：

> `E_x` 恰好覆盖 `(parent(x), x]` 之间被写过的页。

位图**漏记**一页，回滚就会漏一页 —— guest 内存里混进一页来自另一个时刻的数据，静默损坏。
位图**多记**只是浪费，不影响正确性。

所以判据的方向性很重要：**宁可多记，不可漏记**。后面会看到，ARM 适配版恰恰是「多记到极致」——
它不正确性出错，它只是把成本模型毁了。

---

## 2. 怎么知道一页被写过

Guest 的地址翻译在虚拟化下是两层：

```
Guest 虚拟地址
   │  Guest Stage-1（guest 内核自己管的页表）
   ▼
Guest 物理地址 / IPA
   │  KVM Stage-2（宿主 KVM 管的页表）
   ▼
宿主物理地址
```

我们要跟踪的是**中间那一层**：哪些 guest 物理页被写过。有三条路。

### 2.1 KVM 写保护 + 脏页日志

最经典的做法，也是 KVM 的标准能力：

1. VMM 给内存槽（memslot）打上 `KVM_MEM_LOG_DIRTY_PAGES`；
2. KVM 把 Stage-2 页表里所有页设成**只读**；
3. guest 第一次写某页 → 权限故障 → **VM-Exit** 陷入内核 → KVM 在脏页位图里置位、把该页改回可写、返回 guest；
4. VMM 用 `KVM_GET_DIRTY_LOG` 取走位图。

代价模型很清楚：**每个干净页的第一次写，付一次 VM-Exit。** 之后该页就一直可写，不再有开销。
对一个会打快照的虚机，这个代价摊到快照收益里通常划算；
但对一个**从不打快照**的虚机，这是纯亏损。

### 2.2 userfaultfd 写保护

e2b 在 x86 上用的做法。内存不是普通匿名内存，而是注册给 userfaultfd 的区域，
缺页由用户态的 handler 负责填充（这样内存可以按需从对象存储拉取）。

在 `UFFDIO_COPY` 填页时，可以**选择保留写保护位**：

- 因**读**缺页而填的页 → 带 `UFFDIO_COPY_MODE_WP`，页仍是写保护的；
- 因**写**缺页而填的页 → 不带，页可写。

之后 guest 若去写一个「因读而填」的页，会再触发一次写保护故障，handler 清掉 WP 位。
于是 `/proc/self/pagemap` 里那一页的 **bit 57（uffd 写保护）** 就成了一个准确的信号：

```
这一页是脏的  ⟺  present（bit 63）= 1  且  uffd-wp（bit 57）= 0
```

代价是读缺页要多走一次写保护故障（如果后来确实被写了）。收益是判据**精确** ——
只读不写的页不算脏。

### 2.3 HDBSS：硬件标脏

**HDBSS**（Hardware Dirty state tracking Structure）是 ARMv9.5 的能力，鲲鹏 950 实现了它。
它工作在 **KVM Stage-2** 这一层：

```
Guest 写入某个 GPA
    │
    ▼  Stage-2 PTE 上启用了 DBM（Dirty Bit Modifier）
CPU 直接把脏 GPA 写进该 vCPU 的 HDBSS buffer
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

关键在于**没有写保护，也就没有为标脏而生的 VM-Exit**。CPU 顺手记一笔，
等到下次因为别的原因退出时，内核再把 buffer 里的记录汇总进标准位图。

两个推论：

- **对 VMM 完全透明。** HDBSS **没有**独立的位图获取接口，最终还是走 `KVM_GET_DIRTY_LOG`。
  也就是说，2.1 和 2.3 在**结果和接口上完全一样**，只有代价不同。
  这是好事（代码不用分叉），也是坏事（**降级是完全静默的**，只能从延迟上看出来）——
  后面 [§4.5](#45-能力上报) 专门处理这一点。
- **每 vCPU 一个 buffer，有容量上限。** buffer 写满会触发额外的处理，
  写密集负载下这部分开销可能把收益吃掉一部分。见 [§6.2](#62-hdbss-的-buffer-与溢出)。

### 2.4 三者对比

| | 判据来源 | 每页首次写的代价 | 精度 | 可用性 |
|---|---|---|---|---|
| KVM 写保护 | Stage-2 权限故障 | 一次 VM-Exit | 精确（只有写才触发） | 任何 KVM |
| userfaultfd 写保护 | pagemap bit 57 | 一次用户态故障往返 | 精确 | 需要内存由 uffd 托管 |
| **HDBSS** | CPU 写 buffer + VM-Exit 时汇总 | **≈ 0** | 精确 | ARMv9.5 + 内核支持 + VHE |

---

## 3. KVM 脏页日志的语义

### 3.1 破坏性读

`KVM_GET_DIRTY_LOG` **取走并清空**位图。这是标准语义（否则调用方无法区分「上次之后新脏的」
和「历史上脏过的」），但它意味着：

> **每一次读，都是一次不可撤销的信息转移。** 读完之后如果后续步骤失败，
> 那些「这些页脏过」的信息就永远消失了。

这条约束在本方案里出现了三次：

| 场合 | 处理 |
|---|---|
| 写快照 | `dump_dirty` 写文件失败时，把 KVM 位图**折回**用户态位图；成功才 `reset_dirty` |
| 原地回滚阶段 3 | 取图后**立刻** `store_dirty_bitmap` 折回，再往下走 |
| `save-dirty-bitmap` | 同上：取图 → 折回 → 再序列化写文件 |

三处的模式是一样的：**先把易失的信息落到不易失的地方，再做可能失败的事。**

### 3.2 两层位图

Firecracker 侧其实有两份脏页记录：

| 位图 | 在哪 | 谁写 |
|---|---|---|
| KVM 位图 | 内核，每 memslot 一份 | KVM（写保护故障 / HDBSS 汇总） |
| 用户态位图 | Firecracker 进程，每 region 一个 `AtomicBitmap` | `store_dirty_bitmap` 折回；设备直写内存时也标 |

`dump_dirty` 写快照时取的是**两者的并集**：

```rust
if is_kvm_page_dirty || is_firecracker_page_dirty {
    // 这一页写进差分文件，并在返回的 merged 位图里置位
}
```

用户态那一份不只是备份。**Firecracker 自己写 guest 内存时（设备 DMA、队列操作）不会经过
Stage-2 故障**，KVM 不知道，所以要由 Firecracker 自己标记。回滚的阶段 9
「队列页重新标脏」就是这一类（[第 11 篇](11-in-place-rollback.md)）。

### 3.3 三个函数的关系

| 函数 | 做什么 | 什么时候 |
|---|---|---|
| `store_dirty_bitmap(kvm_bitmap)` | 把 KVM 位图 OR 进用户态位图 | 取图之后立刻 |
| `dump_dirty(writer, kvm_bitmap)` | 按并集把脏页写进文件，返回 merged 位图；成功则重置两侧 | 写差分快照 |
| `reset_dirty()` | 清空用户态位图 | 快照成功后、回滚阶段 9 |

`dump_dirty` 返回的 `merged` 就是随快照落地的那个**纪元位图** `E_x`
（[第 8 篇 §2.2](08-memory-diff-tree.md#22-纪元epoch与它的位图)）。
全量快照不走这条路，它直接写全 1 位图。

---

## 4. HDBSS 怎么被启用

### 4.0 先分清两层

这里最容易混的一点：**脏页这件事分两层，HDBSS 只是第二层。**

| 层 | 管什么 | 谁决定 | 什么时候定 | 开关 |
|---|---|---|---|---|
| ① | **要不要记脏页** | orchestrator，写进 Firecracker 的 boot config（`track_dirty_pages`） | **虚机启动那一刻** | `FC_TRACK_DIRTY_PAGES` |
| ② | **用什么方式记** | Firecracker 自己，运行时探测 | 虚机构建时 | **没有开关** —— cap 502 能开就 HDBSS，不能就退回写保护 |

两层的失效表现完全不同：

- **第一层关着** → Firecracker 根本不产生脏位图 → checkpoint 算不出增量 →
  每次抓走整份 guest 内存。**不报错，测试照样通过，测的却不是增量。**
- **第二层退回软件路径** → 结果一模一样（都是 `KVM_GET_DIRTY_LOG` 位图），
  只是每个干净页首次写多一次 VM-Exit。**只有延迟能看出来。**

下面 §4.1 讲的是这两层各自怎么落地。

### 4.1 两侧各做一件事

**orchestrator** 在启动时探测一次，决定这台机器上的虚机默认要不要武装脏页跟踪：

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

`KVM_CHECK_EXTENSION` **只查询**：不创建 VM、不启用任何东西。
在没有 `/dev/kvm` 的机器上（比如开发机、CI）它安全地返回 false。

**Firecracker** 在 `setup_dirty_tracking()` 里真正武装：

```rust
let mut cap = kvm_bindings::kvm_enable_cap::default();
cap.cap = KVM_CAP_ARM_HW_DIRTY_STATE_TRACK;   // 502
cap.args[0] = order;                          // buffer 大小编码
let ret = unsafe { libc::ioctl(vm_fd, KVM_ENABLE_CAP, &cap) };
```

> `KVM_ENABLE_CAP` 的 ioctl 号在代码里是写死的 `0x4068_AEA3`，
> 因为 capability 502 是 openEuler 的厂商扩展，Firecracker 用的上游 `kvm-bindings`
> 里没有这个常量。

### 4.2 启用时序：必须在 vCPU 之后

这是最容易踩错的一点。**OLK 的 `kvm_cap_arm_enable_hdbss()` 只为调用时已经存在的 vCPU 分配 buffer。**
所以：

```
✗ 错误：  KVM_CREATE_VM → KVM_ENABLE_CAP(HDBSS) → KVM_CREATE_VCPU
✓ 正确：  KVM_CREATE_VM → KVM_CREATE_VCPU → KVM_ENABLE_CAP(HDBSS)
```

错误的顺序**不会报错** —— ioctl 返回成功，只是没有任何 vCPU 拿到 buffer，脏页一条也记不到。

代码里靠调用点的位置保证：`setup_dirty_tracking()` 在 `create_vmm_and_vcpus()` 与
`register_memory_regions()` **之后**调用。两个构建路径都是这个顺序。

> **改代码时注意**：这个约束没有断言守着，只靠调用点的位置。
> 挪动 `setup_dirty_tracking()` 的位置是一个静默的破坏性改动。

### 4.3 引导和加载两条路径都要武装

`setup_dirty_tracking()` 在 `builder.rs` 里有**两个**调用点：

| 路径 | 函数 |
|---|---|
| 冷启动 | `build_microvm_for_boot` |
| **从快照加载** | `build_microvm_from_snapshot` |

第二条是关键：**e2b 的沙箱从不冷启动，一律由快照加载而来**
（[第 2 篇](02-microvm-and-e2b.md)）。只改 machine-config、只管冷启动路径，等于完全没开。

### 4.4 运行时前提与环境变量

HDBSS 能真正生效，要同时满足：

| 前提 | 检查方式 |
|---|---|
| 内核编译了 `CONFIG_ARM64_HDBSS` | `grep CONFIG_ARM64_HDBSS /boot/config-$(uname -r)` |
| CPU 实现了 HDBSS | `KVM_CHECK_EXTENSION(502) > 0` |
| KVM 运行在 **VHE** 模式 | `dmesg \| grep -i 'VHE mode initialized'` —— OLK 明确拒绝 non-VHE |
| memslot 带 `KVM_MEM_LOG_DIRTY_PAGES` | Firecracker 在 region 有 bitmap 时自动带上 |
| **没有**同时用 KVM dirty ring | 两者不兼容 |
| `KVM_ENABLE_CAP` 在 vCPU 创建之后调用 | [§4.2](#42-启用时序必须在-vcpu-之后) |

三个环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `FC_TRACK_DIRTY_PAGES` | 强制开 / 关，覆盖硬件探测 | 跟随探测 |
| `FC_HDBSS_ORDER` | 每 vCPU 的 buffer 大小编码 | `1`（8 KiB） |
| `FC_HDBSS_REQUIRED` | 要求必须启用 HDBSS，否则**启动即失败** | `false` |

`args[0]` 是**大小编码**，不是字节数（4 KiB 宿主页下它等于 `alloc_pages()` 的 order）：

| 编码 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| 每 vCPU buffer | 8 KiB | 16 KiB | 32 KiB | 64 KiB | 128 KiB | 256 KiB | 512 KiB | 1 MiB | 2 MiB |

编码 9 能容纳约 26 万条记录，但要为**每个 vCPU** 分配 2 MiB **连续物理内存** ——
在长时间运行、内存碎片化的宿主上这不是免费的。

### 4.5 能力上报

因为降级完全静默（[§2.3](#23-hdbss硬件标脏)），必须主动把「这台机器实际在用哪条路」说出来。
两侧都报：

**Firecracker** 在 instance info 里报一个字符串：

```rust
pub enum DirtyTrackingBackend { Off, KvmWriteProtect, Hdbss }
// as_str() → "off" | "kvm-wp" | "hdbss"
```

**orchestrator** 启动时打一条日志，带上判定结果**和判定理由**：

```go
"hardware dirty state tracking present (KVM capability 502)"
"no hardware dirty state tracking; software tracking costs a VM exit per clean page"
"FC_TRACK_DIRTY_PAGES=\"true\""
```

> 「一个部署悄悄丢了增量 checkpoint，应该能从日志里读出原因，而不是只表现为慢。」
> 这句话是这段代码的全部动机。

### 4.6 默认值为什么跟着硬件走

因为**代价跟着硬件走**：

| 硬件 | 武装的代价 | 对从不 checkpoint 的沙箱 |
|---|---|---|
| 有 HDBSS | 几乎为零 | 无所谓，默认开 |
| 无 HDBSS | 每个干净页首次写一次 VM-Exit | **纯亏损**，默认关 |

所以一台 950 上，能做增量 checkpoint 的沙箱**开箱就在做**，没人需要知道这件事；
一台 920B 上，不打 checkpoint 的沙箱不用为别人的功能买单。

要在 920B 上做 checkpoint，显式设 `FC_TRACK_DIRTY_PAGES=true`。

> **当前限制**：这个决策是**整个 orchestrator 一个值**（包级变量，启动时算一次），
> 不是每沙箱一个。做成「按沙箱按需武装」需要在创建沙箱时就知道它会不会打 checkpoint，
> 目前的 API 里没有这个信息。这是一个明确的可扩展点，见[第 30 篇](30-extending.md)。

---

## 5. 脏页判据的三方差异

**这一节是理解本方案价值的前提。**

三个东西必须区分清楚，否则很容易得出一个错误的结论：

| | 脏页怎么判定 | 结果 |
|---|---|---|
| **e2b x86 原生** | 读缺页填充时保留 uffd 写保护位，写缺页则清除；快照时取「已存在 且 未写保护」的页 | 增量**精确** |
| **ARM 适配版**（本方案的基线） | 该写保护路径在 arm64 上走不通，被注释掉；判据第二项恒真 | 增量**退化**：凡被换入过的页都算脏 |
| **本方案** | 不经过 uffd，直接取 KVM / HDBSS 的脏页日志 | ARM 上**恢复精确增量** |

> **必须明确：这是修复 ARM 适配引入的退化，不是超越 x86 原生。**
> 在 x86 上 e2b 的增量本来就是精确的。

再加上进程级快照（gsd / CRIU）用的内核 soft-dirty，一共四条判据；
把它们并排成一张表、以及这个差异在同一台机器同一份负载下测出来是什么形状，
见[第 27 篇 §5](27-cross-implementation.md#5-一个真实发现读也被算成脏)。

### 5.1 退化是怎么发生的

ARM 适配版里，`UFFDIO_COPY` 的写保护模式被注释掉了：

```go
// Performing copy() on UFFD clears the WP bit unless we explicitly tell
// it not to. We do that for faults caused by a read access. Write accesses
// would anyways cause clear the write-protection bit.
//if accessType != block.Write {
//	copyMode |= UFFDIO_COPY_MODE_WP
//}

copyErr := u.fd.copy(addr, pagesize, b, copyMode)   // copyMode 恒为 0
```

于是**每一次填页都会清掉 WP 位**，无论触发它的是读还是写。

### 5.2 判据因此塌缩

取脏页位图的接口 `GET /memory/dirty` 是两级的：

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

第二级的判据是 `is_present() && !is_write_protected()`。而 [§5.1](#51-退化是怎么发生的) 的结果是
**bit 57 恒为 0**，所以 `!is_write_protected()` 恒真，判据塌缩成 `is_present()` ——
也就是 mincore 已经回答过的那个问题。

两个后果：

1. **语义上**：位图 = 常驻页集合。「被读进来过」和「被写过」不再有区别。
2. **性能上**：第二级成了纯开销 —— 对每一个常驻页做一次 `pread`，
   算出一个已经知道的答案。（顺带一提，这条路径还要求放行 vmm 线程的 `pread64` seccomp 白名单。）

### 5.3 「与改动量无关的下限」

现在可以解释一个反直觉的现象了：在 ARM 适配版上，即使一次 snapshot 之间什么都没做，
增量也不会接近零。

两件事叠加：

1. **原生 `Checkpoint` RPC 会重建沙箱** —— 打完快照停掉旧进程，从新快照拉起一个新 Firecracker
   （[第 4 篇](04-e2b-native-snapshot.md)）。新进程的内存是空的，
   guest 一跑起来就要把工作集重新缺页换入。
2. **换入即被判脏**（[§5.2](#52-判据因此塌缩)）。

于是下一次增量的下限 ≈ **整个常驻工作集**，与「这段时间改了多少」无关。

x86 上第 2 条不成立：换入的是干净页（保留了 WP 位），不计入增量。所以这个下限是
**ARM 适配版特有的**，不是 e2b 的设计问题。

> **2026-09-11 起已修复**（infra-arm `jll` `de25fe4d0`）：原生 pause 的判据换成 Firecracker 的写跟踪位图，差分按 4 KiB 存、按 2 MiB 页拼回。本段描述的是修复前的机制，仍是理解成本模型的依据；机理见[第 21 篇](21-native-increment-diagnosis.md)、改法见[第 22 篇](22-native-increment-fix.md)，修复后的口径与数据见[第 27 篇 §5.5](27-cross-implementation.md#55-原生快照口径修复后)、[第 28 篇 §3.3 表 3-J](28-results-and-compliance.md#表-3-j--native_snapshot_benchpy修复后920b-0914-native4k)。


### 5.4 本方案怎么绕开

本方案根本不走 uffd 这条判据。它取的是 **KVM / HDBSS 的脏页日志** ——
一个由硬件或内核在 Stage-2 层面维护的、真正意义上的「被写过」的记录。

副产品是：这条路径也**不需要**沙箱的内存由 uffd 托管。差分快照、位图侧车、原地回滚
全都只依赖 KVM 的能力。

---

## 6. 精确性的代价

### 6.1 软件写保护的代价模型

设工作集 W 页，其中一次快照周期内被写的有 D 页。写保护路径的额外开销 ≈ **D 次 VM-Exit**
（每个干净页第一次写各一次）。

- **D 小**（改动少）：开销小，收益大（差分小）。划算。
- **D ≈ W**（写密集）：付了 W 次 VM-Exit，差分也接近全量。**不划算** ——
  这种负载下增量快照本来就没什么可省的。
- **从不快照**：付了 D 次 VM-Exit，收益为零。**纯亏损**，所以默认关。

### 6.2 HDBSS 的 buffer 与溢出

HDBSS 把「每页一次 VM-Exit」换成了「CPU 顺手记一笔 + 定期汇总」。但 buffer 有限：
默认 8 KiB / vCPU，每条记录 8 字节，约 1000 条 —— 也就是约 **4 MiB 的脏页覆盖量**
（4 KiB 页）。

写密集负载下 buffer 会填满，触发额外的处理。极端情况下，
这部分开销可能把「省掉写保护陷出」的收益吃掉相当一部分。

`FC_HDBSS_ORDER` 就是为这件事准备的。**这一项还没有实测数据** ——
1 / 2 / 4 三档在写密集负载下的对比是[第 28 篇 §8](28-results-and-compliance.md#8-尚未覆盖)
里明确列出的缺口之一。

### 6.3 什么时候不该开

| 场景 | 建议 |
|---|---|
| 沙箱从不打 checkpoint，宿主无 HDBSS | 关（默认行为） |
| 沙箱从不打 checkpoint，宿主有 HDBSS | 开着无妨，代价接近零 |
| 写密集且很少 checkpoint | 关；增量在这种负载下本来就省不下什么 |
| 要做回滚的生产部署 | 开，并设 `FC_HDBSS_REQUIRED=true`，**宁可启动失败也不要静默走软件路径** |

最后一条值得强调：静默降级在这套系统里是**最难发现的故障模式**。
功能全对、测试全过、只是慢 —— 而且慢的幅度取决于负载，不容易触发告警。

---

## 7. 小结

1. 脏页判据的**精确性**同时决定成本模型（多记 = 慢）和正确性（漏记 = 静默损坏）。
   方向性是**宁可多记，不可漏记**。
2. 三种机制：KVM 写保护（每页首写一次 VM-Exit）、uffd 写保护（用户态故障）、
   **HDBSS 硬件标脏（≈ 零成本）**。三者最终都通过 `KVM_GET_DIRTY_LOG` 出口，
   **接口完全一样，只有代价不同** —— 所以降级是完全静默的。
3. KVM 脏页日志是**破坏性读**。取图之后必须立刻折回用户态位图，再做可能失败的事。
4. HDBSS 的启用有三个容易踩的坑：**必须在 vCPU 创建之后**、**引导和加载两条路径都要武装**、
   **必须 VHE 模式**。前两个都不会报错，只会静默失效。
5. ARM 适配版把 `UFFDIO_COPY_MODE_WP` 注释掉了，判据塌缩成「常驻即脏」。
   本方案绕开 uffd 直取 KVM 日志，**恢复了 ARM 上的精确增量** —— 不是超越 x86 原生。
6. 默认值跟着硬件走，因为**代价跟着硬件走**。生产回滚部署应当 `FC_HDBSS_REQUIRED=true` 来 fail fast。

---

## 思考题

1. `dump_dirty` 取的是「KVM 位图 ∪ 用户态位图」。如果只取 KVM 位图，会漏掉哪一类页？
   构造一个能观察到后果的场景。
2. HDBSS 在 VM-Exit 时才汇总 buffer。一个几乎不产生 VM-Exit 的 guest（纯计算、无 I/O）
   长时间运行后，KVM 位图里会有什么？这对「暂停后立刻取图」的正确性有影响吗？
3. `KVM_CHECK_EXTENSION` 是在 KVM fd 上查询、`KVM_ENABLE_CAP` 是在 VM fd 上启用。
   为什么探测能在「不创建 VM」的前提下完成，而启用不能？
4. 假设要把脏页跟踪做成「按沙箱按需武装」。需要在哪些地方引入新信息？
   有没有办法在沙箱运行到一半时才武装？代价是什么？

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| 探测与默认值决策 | `internal/sandbox/fc/dirtytracking.go` — `resolveTrackDirtyPages`、`hardwareDirtyTracking` |
| 启动时的能力上报 | `packages/orchestrator/main.go` — `reportCheckpointCapabilities` |
| 后端选择与武装 | `src/vmm/src/vstate/vm.rs` — `setup_dirty_tracking`、`DirtyTrackingBackend` |
| HDBSS 的 ioctl | `src/vmm/src/arch/aarch64/vm.rs` — `enable_hdbss` |
| 两个调用点 | `src/vmm/src/builder.rs` — `build_microvm_for_boot`、`build_microvm_from_snapshot` |
| 两层位图与三个函数 | `src/vmm/src/vstate/memory.rs` — `dump_dirty`、`store_dirty_bitmap`、`reset_dirty` |
| 退化的那一行 | `internal/sandbox/uffd/userfaultfd/userfaultfd.go` — 被注释的 `UFFDIO_COPY_MODE_WP` |
| 两级判据 | `src/vmm/src/lib.rs` — `get_dirty_memory`；`src/vmm/src/utils/pagemap.rs` — `is_page_dirty` |
| 内核侧调研记录 | 工作区 `e2b-repo/HDBSS_KUNPENG950_KERNEL_6.6.0_515.md` |

**下一篇**：[08 · 内存差分树](08-memory-diff-tree.md) —— 有了精确的脏页位图，
接下来是怎么用它组织 n 代内存状态。
