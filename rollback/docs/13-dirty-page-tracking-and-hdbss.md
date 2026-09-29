# 13 · 脏页跟踪与 HDBSS

> 这篇给开发者和系统工程师看。"只存改动量"的前提是**知道改了哪些页**。读完你能说清：ARM 上有哪几种判据、代价差多少；
> KVM 脏页日志为什么是破坏性读；HDBSS 在硬件和内核里做了什么、启用要满足哪些条件、哪些条件不满足时会静默失效；
> 以及"读也算脏"这个判据差异（全书只在本篇定义）。

---

## 1. 为什么这是地基

两件事压在脏页跟踪上：

- **成本模型。** 差分树里每一代产物是 O(本代脏页)（[14](14-memory-diff-tree.md)）。判据把没写过的页也算进去，
  这个 O 就退化，极端情况下退化到 O(内存)，而且是**静默**的：功能全对，只是慢。
- **正确性。** 回滚集的正确性证明依赖一条不变量：纪元位图 `E_x` 恰好覆盖 `(parent(x), x]` 之间被写过的页。
  位图**漏记**一页，回滚就漏一页，guest 内存里混进一页来自另一个时刻的数据；**多记**只是浪费。

所以判据的方向是**宁可多记，不可漏记**。后面会看到，ARM 适配基线恰恰是"多记到极致"：它不出正确性问题，只是毁了成本模型。

---

## 2. 怎么知道一页被写过

虚拟化下地址翻译分两层：guest 虚拟地址 →（guest 自己的 Stage-1 页表）→ guest 物理地址 / IPA →（宿主 KVM 的 Stage-2 页表）→ 宿主物理地址。
要跟踪的是中间那一层：哪些 guest 物理页被写过。三条路。

### 2.1 KVM 写保护 + 脏页日志

1. VMM 给内存槽（memslot）打上 `KVM_MEM_LOG_DIRTY_PAGES`；
2. KVM 把 Stage-2 页表里的页设成只读；
3. guest 第一次写某页 → 权限故障 → **VM-Exit** → KVM 在脏页位图里置位、把页改回可写、返回 guest；
4. VMM 用 `KVM_GET_DIRTY_LOG` 取走位图。

代价：**每个干净页的第一次写，付一次 VM-Exit**。对打快照的虚机通常划算，对从不打快照的虚机是纯亏损。

### 2.2 userfaultfd 写保护

e2b 在 x86 上的做法。内存注册给 userfaultfd，缺页由用户态 handler 填充。`UFFDIO_COPY` 填页时，因**读**缺页而填的页带
`UFFDIO_COPY_MODE_WP` 保留写保护，因**写**缺页而填的页不带；之后 guest 写一个"因读而填"的页会再触发一次写保护故障，
handler 清掉 WP 位。于是 `/proc/self/pagemap` 里的 bit 57（uffd 写保护）成为准确的信号：

```
脏  ⟺  present（bit 63）= 1  且  uffd-wp（bit 57）= 0
```

### 2.3 HDBSS：硬件标脏

**HDBSS**（Hardware Dirty state tracking Structure）是 ARMv9.5 的能力，鲲鹏 950 实现了它，工作在 **Stage-2**：

```
Guest 写入某个 GPA
    ▼  Stage-2 PTE 上启用了 DBM（Dirty Bit Modifier）
CPU 直接把脏 GPA 写进该 vCPU 的 HDBSS buffer         ← 硬件，无陷出
    ▼  vCPU 发生 VM-Exit（任何原因）
KVM 遍历 HDBSS buffer，逐条 kvm_vcpu_mark_page_dirty()
    ▼
标准 KVM memory-slot dirty bitmap
    ▼
VMM 调用 KVM_GET_DIRTY_LOG
```

关键在于**没有写保护，也就没有为标脏而生的 VM-Exit**：CPU 顺手记一笔，等 vCPU 因别的原因退出时内核再汇总。

它**不是**什么，同样重要：

- 不是独立的快照系统：只记录"哪些页被改了"，不保存 vCPU / 设备状态，不生成快照文件；
- **没有独立的位图获取接口**：最终仍走 `KVM_GET_DIRTY_LOG`；
- 对 VMM 几乎透明：软件写保护和 HDBSS 喂给上层的是**同一个位图**，语义一致，只差性能。

第三条是好事也是坏事：代码不用为两条路径分叉，**但降级完全静默**，只能从延迟和能力上报里看出来（§4.6）。

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
| KVM 写保护 | Stage-2 权限故障 | 一次 VM-Exit | 精确 | 任何 KVM |
| userfaultfd 写保护 | pagemap bit 57 | 一次用户态故障往返 | 精确 | 内存由 uffd 托管，且内核支持 |
| **HDBSS** | CPU 写 buffer + VM-Exit 时汇总 | ≈ 0 | 精确 | ARMv9.5 + 内核支持 + VHE |

---

## 3. KVM 脏页日志的语义

### 3.1 破坏性读

`KVM_GET_DIRTY_LOG` **取走并清空**位图。这是标准语义，但意味着每一次读都是不可撤销的信息转移：读完之后后续步骤失败，
"这些页脏过"的信息就永远消失了。本方案里这条约束出现在三处，处理模式一样 —— **先把易失的信息落到不易失的地方，再做可能失败的事**：

| 场合 | 处理 | 代码 |
|---|---|---|
| 写差分快照 | `dump_dirty` 写文件失败时把 KVM 位图折回用户态位图；成功才 `reset_dirty` | `firecracker/src/vmm/src/vstate/memory.rs:308-312` |
| 原地回滚阶段 3 | 取图后立刻 `store_dirty_bitmap` 折回，再往下走 | `firecracker/src/vmm/src/rollback.rs:332-342` |
| `save-dirty-bitmap` | 取图 → 折回 → 再序列化写文件 | `rollback.rs:796-825` |

### 3.2 两层位图

| 位图 | 在哪 | 谁写 |
|---|---|---|
| KVM 位图 | 内核，每 memslot 一份 | KVM（写保护故障 / HDBSS 汇总） |
| 用户态位图 | Firecracker 进程，每 region 一个 | `store_dirty_bitmap` 折回；Firecracker 自己写 guest 内存时也标 |

`dump_dirty`（`memory.rs:246`）写快照时取**两者的并集**，并按快照文件的平铺页号攒出 `merged` 位图返回 ——
它就是随快照落地的纪元位图 `E_x`（格式见 [15](15-firecracker-api-contract.md)）。用户态那份不只是备份：
**Firecracker 自己写 guest 内存时（设备 DMA、队列操作）不经过 Stage-2 故障**，KVM 不知道，要由 Firecracker 自己标。

| 函数 | 做什么 | 什么时候 |
|---|---|---|
| `store_dirty_bitmap(kvm_bitmap)`（`memory.rs:408`） | 把 KVM 位图 OR 进用户态位图 | 取图之后立刻 |
| `dump_dirty(writer, kvm_bitmap)`（:246） | 按并集把脏页写进文件，返回 merged；成功则清用户态位图 | 写差分快照 |
| `reset_dirty()`（:399） | 清空用户态位图 | 快照成功后、回滚阶段 9 |

全量快照不走 `dump_dirty`，直接写全 1 位图（`firecracker/src/vmm/src/vstate/vm.rs:519-530`）。

---

## 4. HDBSS 怎么被启用

### 4.1 先分清两层

**脏页这件事分两层，HDBSS 只是第二层。** 混淆这两层是部署时最常见的配置错误：

| 层 | 管什么 | 谁决定 | 什么时候定 | 开关 |
|---|---|---|---|---|
| ① | **要不要记脏页** | orchestrator，写进 Firecracker boot config 的 `track_dirty_pages` | 虚机启动那一刻 | `FC_TRACK_DIRTY_PAGES` |
| ② | **用什么方式记** | Firecracker 自己运行时探测 | 虚机构建时 | 无开关：能开 HDBSS 就开，不能就退回写保护（`FC_HDBSS_REQUIRED` 可改成"不能就失败"） |

两层的失效表现完全不同：

- **第一层关着** → Firecracker 根本不产生脏位图 → 每次 checkpoint 抓整份 guest 内存（`memMode=full`）。**不报错，测试照样通过，测的却不是增量。**
- **第二层退回软件路径** → 结果一模一样，只是每个干净页首次写多一次 VM-Exit。**只有延迟能看出来。**

开关的取值、默认值与三种部署情形见 [06](06-configuration-and-capacity.md)。

### 4.2 两侧各做一件事

**orchestrator** 启动时探测一次（`internal/sandbox/fc/dirtytracking.go:109`）：打开 `/dev/kvm`，
`KVM_CHECK_EXTENSION(502)`（`kvmCheckExtension = 0xAE03`，`kvmCapArmHWDirtyStateTrack = 502`，:14-15）。
这个 ioctl **只查询**，不建 VM、不启用任何东西；没有 `/dev/kvm` 的机器上安全地返回 false。
判定逻辑在 `decideTrackDirtyPages`（:68）：变量未设置跟硬件走，能被 `strconv.ParseBool` 解析就照办，解析不了则忽略并跟硬件走。
这个决策是**整个 orchestrator 一个值**，不是每沙箱一个（按沙箱按需武装是可扩展点，见 [B](B-extending.md)）。

**Firecracker** 在 `Vm::setup_dirty_tracking()`（`vstate/vm.rs:206`）里真正武装：region 没有 bitmap 就是 `off`；
aarch64 上调 `enable_hdbss(order)`（`arch/aarch64/vm.rs:66`，对 VM fd 做 `KVM_ENABLE_CAP`，cap 502）。成功记为 `hdbss`，
失败时若要求必须 HDBSS 则构建失败，否则退回 `kvm-wp`。`KVM_ENABLE_CAP` 的 ioctl 号在代码里写死为 `0x4068_AEA3`
（:71），因为 502 是 openEuler 的厂商扩展，上游 `kvm-bindings` 里没有这个常量。

探测能在 KVM fd 上做、不建 VM；启用必须在 VM fd 上、而且要先有 vCPU。所以 orchestrator 启动时的探测只能证明"能力存在"，
证明不了"开成功了"。

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

错误的顺序**不会报错**：ioctl 返回成功，只是没有任何 vCPU 拿到 buffer。代码靠调用点位置保证：`setup_dirty_tracking()`
在 `create_vmm_and_vcpus()` 与 `register_memory_regions()` **之后**调用（`firecracker/src/vmm/src/builder.rs:229-239`、:425-438）。
**这个约束没有断言守着**，挪动调用点是静默的破坏性改动（不变量清单见 [20](20-failure-semantics.md)）。

### 4.4 引导和加载两条路径都要武装

HDBSS **不会被快照继承**：从快照恢复会创建新的 KVM VM 和新的 vCPU。所以 `setup_dirty_tracking()` 有两个调用点：

| 路径 | 函数 | 位置 |
|---|---|---|
| 冷启动 | `build_microvm_for_boot` | `builder.rs:199`，调用在 :239 |
| **从快照加载** | `build_microvm_from_snapshot` | `builder.rs:414`，调用在 :438 |

第二条是关键：e2b 的用户沙箱一律由快照加载而来（[10](10-background.md)），只管冷启动路径等于完全没开。

### 4.5 buffer 大小

`cap.args[0]` 是**大小编码**，不是字节数（4 KiB 宿主页下等于 `alloc_pages()` 的 order），**每个 vCPU 一份**：

| 编码 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| 每 vCPU buffer | 8 KiB | 16 KiB | 32 KiB | 64 KiB | 128 KiB | 256 KiB | 512 KiB | 1 MiB | 2 MiB |

默认 1（8 KiB，也是内核默认，`vstate/vm.rs:222-225`，由 `FC_HDBSS_ORDER` 覆盖）。一条记录 8 字节，8 KiB ≈ 1000 条 ≈ 4 MiB 脏页覆盖量。
写密集负载下 buffer 会填满、触发额外处理，极端情况下可能吃掉"省掉写保护陷出"的一部分收益。编码 9 能容纳约 26 万条，
但要为每个 vCPU 分配 2 MiB **连续物理内存**，在长期运行、内存碎片化的宿主上不是免费的。

### 4.6 能力上报

降级完全静默，所以两侧都要把"实际在用哪条路"说出来：

- **Firecracker** 在 instance info 里报 `dirty_tracking`：`DirtyTrackingBackend { Off, KvmWriteProtect, Hdbss }`，
  字符串为 `off` / `kvm-wp` / `hdbss`（`vstate/vm.rs:48-66`）。HDBSS 开成功时内核日志有 `Enable HDBSS success`。
- **orchestrator** 启动时打 `checkpoint capabilities` 一行，带判定结果**和理由**（`main.go` 的 `reportCheckpointCapabilities`）。
  理由的三种写法：`hardware dirty state tracking present (KVM capability 502)`、
  `no hardware dirty state tracking; software tracking costs a VM exit per clean page`、`FC_TRACK_DIRTY_PAGES="true"`
  （`dirtytracking.go:92-104`、:73-78）。跟踪关着时另打一条 WARN。

字段与日志的完整列表见 [07](07-observability-reference.md)，部署后怎么逐项核对见 [05](05-deployment-prerequisites.md)。

### 4.7 默认值为什么跟着硬件走

因为**代价跟着硬件走**：

| 硬件 | 武装的代价 | 对从不 checkpoint 的沙箱 |
|---|---|---|
| 有 HDBSS | 几乎为零 | 无所谓，默认开 |
| 无 HDBSS | 每个干净页首次写一次 VM-Exit | 纯亏损，默认关 |

一台 950 上能做增量 checkpoint 的沙箱开箱就在做；一台无 HDBSS 的机器上，不打 checkpoint 的沙箱不用为别人的功能买单，
要增量就显式打开第一层（退化为 KVM 写保护）。

---

## 5. 判据差异：读也算脏

**这一节是理解本方案价值的前提，全书只在这里定义。** 三个东西必须分清：

| | 脏页怎么判定 | 结果 |
|---|---|---|
| **e2b x86 原生** | 读缺页填充时保留 uffd 写保护位，写缺页清除；快照时取"已存在且未写保护"的页 | 增量**精确** |
| **ARM 适配基线** | 该写保护路径在 arm64 上走不通，被注释掉；判据第二项恒真 | 增量**退化**：凡被换入过的页都算脏，**读也算脏** |
| **本方案** | 不经过 uffd，直接取 KVM / HDBSS 的脏页日志 | ARM 上**恢复精确增量** |

> **必须明确：这是修复 ARM 适配引入的退化，不是超越 x86 原生。** 在 x86 上 e2b 的增量本来就是精确的。

### 5.1 退化是怎么发生的

ARM 适配版里 `UFFDIO_COPY` 的写保护模式被注释掉了（`internal/sandbox/uffd/userfaultfd/userfaultfd.go:380-387`）：

```go
// Performing copy() on UFFD clears the WP bit unless we explicitly tell
// it not to. We do that for faults caused by a read access. ...
//if accessType != block.Write {
//	copyMode |= UFFDIO_COPY_MODE_WP
//}

copyErr := u.fd.copy(addr, pagesize, b, copyMode)   // copyMode 不带 WP
```

于是每一次填页都清掉 WP 位，无论触发它的是读还是写。

### 5.2 判据因此塌缩

取脏页位图的 `GET /memory/dirty` 是两级的（`firecracker/src/vmm/src/lib.rs:882` `get_dirty_memory`）：
第一级用 `mincore` 一次筛出整个 region 的常驻页；第二级只对常驻页读 pagemap，判据是 `is_present() && !is_write_protected()`。
§5.1 的结果是 bit 57 恒为 0，`!is_write_protected()` 恒真，判据塌缩成 `is_present()`，也就是 mincore 已经回答过的问题：

1. **语义上**：位图 = 常驻页集合，"被读进来过"和"被写过"不再有区别；
2. **性能上**：第二级成了纯开销，对每个常驻页做一次 `pread` 算出已知的答案（这条路径还要求放行 VMM 线程的 `pread64` seccomp 白名单）。

### 5.3 "与改动量无关的下限"

两件事叠加：原生 `Checkpoint` RPC 会**重建沙箱**，新进程的内存是空的，guest 一跑就要把工作集重新缺页换入；
而**换入即被判脏**（§5.2）。于是下一次原生增量的下限 ≈ 整个常驻工作集，与"这段时间改了多少"无关。
x86 上换入的是干净页（保留了 WP 位），不计入增量，所以这个下限是 ARM 适配基线特有的。

原生路径这条判据的修复是另一个专题，不属于本方案交付（[11](11-baseline-goals-and-native.md#15-arm-适配基线上的增量判据)）。

### 5.4 本方案怎么绕开

本方案根本不走 uffd 这条判据，取的是 **KVM / HDBSS 的脏页日志**：由硬件或内核在 Stage-2 层面维护的、真正意义上的"被写过"。
副产品是差分快照、位图侧车、原地回滚全都只依赖 KVM 的能力，**不需要**沙箱内存由 uffd 托管。

---

## 6. 精确性的代价

### 6.1 软件写保护

设一次快照周期内被写的页有 D 个，写保护路径的额外开销 ≈ D 次 VM-Exit：

- D 小：开销小，收益大（差分小），划算；
- D ≈ 工作集（写密集）：付了几乎全部页的 VM-Exit，差分也接近全量，不划算 —— 这种负载下增量本来就省不下什么；
- 从不快照：纯亏损，所以无 HDBSS 时默认关。

### 6.2 HDBSS

把"每页一次 VM-Exit"换成"CPU 顺手记一笔 + 定期汇总"，代价转移到 buffer 溢出处理（§4.5）。
`FC_HDBSS_ORDER` 各档在写密集负载下的对比**尚未实测**，缺口清单见 [25](25-results-and-compliance.md)；目前用默认值 1。

### 6.3 什么时候不该开

| 场景 | 建议 |
|---|---|
| 从不打 checkpoint，宿主无 HDBSS | 关（默认） |
| 从不打 checkpoint，宿主有 HDBSS | 开着无妨，代价接近零 |
| 写密集且很少 checkpoint | 关；增量在这种负载下本来就省不下什么 |
| 要做回滚的生产部署 | 开，并要求必须 HDBSS：**宁可启动失败，也不要静默走软件路径** |

静默降级是这套系统里最难发现的故障模式：功能全对、测试全过、只是慢，而且慢的幅度取决于负载，不容易触发告警。

---

## 7. 小结

1. 判据的精确性同时决定成本（多记 = 慢）和正确性（漏记 = 静默损坏），方向是**宁可多记，不可漏记**。
2. 三种机制：KVM 写保护（每页首写一次 VM-Exit）、uffd 写保护、**HDBSS（≈ 零成本）**。HDBSS 最终也走 `KVM_GET_DIRTY_LOG`，
   **接口一样、只有代价不同**，所以降级完全静默。
3. KVM 脏页日志是破坏性读：取图之后先折回用户态位图，再做可能失败的事。
4. 脏页分两层：**要不要记**（orchestrator 决定）和**用什么方式记**（Firecracker 自动探测）。
5. HDBSS 六个前提，其中"必须在 vCPU 创建之后启用"不满足时完全静默；它不被快照继承，引导与加载两条路径都要武装。
6. **读也算脏**是 ARM 适配基线的退化：写保护位被注释掉，判据塌缩成"常驻即脏"。本方案直取 KVM / HDBSS 日志，恢复精确增量，不是超越 x86 原生。
