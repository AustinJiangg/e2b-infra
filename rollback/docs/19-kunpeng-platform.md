# 19 · 鲲鹏平台：HDBSS、920B 与 950

> 方案选择看**文件系统**（[第 18 篇](18-ext4-vs-xfs.md)），脏页后端看**机型**。
> 本篇讲后一条轴：HDBSS 在硬件与内核层面是怎么工作的、启用要满足什么、
> 两台机器实测到什么，以及部署时该检查什么。
>
> **读者**：系统工程师、要做部署与验收的人；也适合想了解硬件辅助虚拟化的工程师。
> **预备**：[第 7 篇 · 脏页跟踪](07-dirty-page-tracking.md)。
> **代码**：`src/vmm/src/arch/aarch64/vm.rs`、`src/vmm/src/vstate/vm.rs`、
> `internal/sandbox/fc/dirtytracking.go`

---

## 0. 本篇要回答的问题

1. HDBSS 在硬件和内核里到底做了什么？
2. 启用它要同时满足哪些条件？哪些条件不满足时会**静默失效**？
3. 950 和 920B 实测差在哪？920B 上能验证什么、不能验证什么？
4. buffer 该配多大？
5. 部署时该检查什么？

---

## 1. 先分清两层

[第 7 篇 §4.0](07-dirty-page-tracking.md#40-先分清两层) 讲过，这里重复一遍，
因为它是部署时最容易配错的地方：

| 层 | 管什么 | 谁决定 | 什么时候定 | 开关 |
|---|---|---|---|---|
| ① | **要不要记脏页** | orchestrator → Firecracker boot config 的 `track_dirty_pages` | **虚机启动那一刻** | `FC_TRACK_DIRTY_PAGES` |
| ② | **用什么方式记** | Firecracker 自己，运行时探测 | 虚机构建时 | **没有开关** |

> **「脏页跟踪关着 → 退化成全量拷贝」说的是第一层，跟 HDBSS 无关。**

第一层关着时不报错、测试照样通过，但测的不是增量。这就是 `memMode` 字段存在的理由
（[第 17 篇 §2.1](17-observability-and-verification.md#21-memmode每次调用都回报)）。

---

## 2. HDBSS 是什么

**HDBSS**（Hardware Dirty state tracking Structure）是 ARMv9.5 的能力。
在 KVM 场景下，guest 的地址翻译分两层：

```
Guest 虚拟地址
        │ Guest Stage-1（guest 内核自己的页表）
        ▼
Guest 物理地址 / IPA
        │ KVM Stage-2（宿主 KVM 的页表）
        ▼
宿主物理地址
```

HDBSS 工作在 **Stage-2**。完整数据流：

```
Guest 写入某个 GPA
    │
    ▼  Stage-2 PTE 上启用了 DBM（Dirty Bit Modifier）
CPU 直接把脏 GPA 写进该 vCPU 的 HDBSS buffer      ← 硬件，无陷出
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

### 2.1 它不是什么

三条同样重要：

- **不是独立的快照系统。** 它只负责高效记录「哪些 guest 页被改了」，
  不保存 vCPU / 设备状态，不生成快照文件。
- **没有独立的位图获取接口。** 最终仍通过标准 `KVM_GET_DIRTY_LOG` 出口。
- **对 VMM 几乎透明。** 软件写保护路径和 HDBSS 路径喂给上层的是**同一个位图**，
  语义完全一致，只差性能。

第三条是好事也是坏事：代码不用为两条路径分叉，**但降级完全静默** ——
只能从延迟和能力上报里看出来。

### 2.2 相关的内核实现位置

| 关注点 | 位置（OLK 6.6） |
|---|---|
| capability 检测、启用、释放、dirty-log 同步 | `arch/arm64/kvm/arm.c` |
| 对启用 dirty logging 的 Stage-2 映射设置 DBM | `arch/arm64/kvm/mmu.c` |
| VM-Exit 时刷新 HDBSS buffer | `arch/arm64/kvm/handle_exit.c` |
| 加载 HDBSS 的 EL2 寄存器 | `arch/arm64/include/asm/kvm_mmu.h` |
| 标准 dirty bitmap 与 `KVM_GET_DIRTY_LOG` | `virt/kvm/kvm_main.c` |
| buffer 大小编码定义 | `arch/arm64/tools/sysreg` |

---

## 3. 启用的六个前提

| # | 前提 | 不满足时 | 静默？ |
|---|---|---|---|
| 1 | 内核编译了 `CONFIG_ARM64_HDBSS` | `KVM_CHECK_EXTENSION(502)` 返回 0 | 否，能探到 |
| 2 | CPU 实现了 HDBSS | 同上 | 否 |
| 3 | KVM 运行在 **VHE** 模式 | OLK 实现**明确拒绝** non-VHE | 否，`ENABLE_CAP` 失败 |
| 4 | memslot 带 `KVM_MEM_LOG_DIRTY_PAGES` | 不记录脏页 | Firecracker 在 region 有 bitmap 时自动带上 |
| 5 | **没有**同时使用 KVM dirty ring | 两者不兼容 | Firecracker 不用 dirty ring |
| 6 | **`KVM_ENABLE_CAP` 在 vCPU 创建之后调用** | **一条脏页都记不到** | **是** |

第 6 条是最危险的。OLK 的 `kvm_cap_arm_enable_hdbss()` **只为调用时已经存在的 vCPU
分配 buffer**：

```
✗ 错误：  KVM_CREATE_VM → KVM_ENABLE_CAP(HDBSS) → KVM_CREATE_VCPU
✓ 正确：  KVM_CREATE_VM → KVM_CREATE_VCPU × N
                        → 注册带 KVM_MEM_LOG_DIRTY_PAGES 的 memory slot
                        → KVM_ENABLE_CAP(HDBSS)
                        → 启动 vCPU
```

**错误的顺序不会报错** —— ioctl 返回成功，只是没有任何 vCPU 拿到 buffer。

代码里靠调用点的位置保证：`setup_dirty_tracking()` 在 `create_vmm_and_vcpus()` 与
`register_memory_regions()` **之后**调用。两个构建路径都是这个顺序，
但**没有断言守着**（[第 14 篇 不变量 #13](14-failure-semantics.md#9-不变量清单)）。

### 3.1 引导和加载两条路径都要武装

HDBSS **不会被快照继承** —— 从快照恢复时会创建新的 KVM VM 和新的 vCPU。
所以 `setup_dirty_tracking()` 在 `builder.rs` 里有两个调用点：

| 路径 | 函数 |
|---|---|
| 冷启动 | `build_microvm_for_boot` |
| **从快照加载** | `build_microvm_from_snapshot` |

第二条是关键：**e2b 的沙箱从不冷启动，一律由快照加载而来**
（[第 2 篇](02-microvm-and-e2b.md)）。只管冷启动路径等于完全没开。

---

## 4. 两台机器的实测对照

### 4.1 硬件与系统

| | 950（slot6） | 920B |
|---|---|---|
| CPU | Kunpeng 950 7592C，384 线程（96 核 × 2 路 × 2 线程） | Kunpeng 920 7270Z，128 核 |
| MIDR part | `0xd06` | `0xd02` |
| 内存 | 1.1 TB | 2.0 TB |
| 2 MB 大页 | 55768 页 ≈ **109 GB** | 201948 页 ≈ 394 GB |
| **根盘文件系统** | **ext4** 988 G | **XFS** 3.0 T |
| 内核 | 6.6.0-159.4.3.154.oe2403sp4 | 6.6.0-28.0.0.34.oe2403 |

**最后两行解释了方案与机型为什么会配成对**：950 的根盘本来就是 ext4，
920B 的本来就是 XFS。这是部署事实，不是设计约束
（[第 18 篇 §6.1](18-ext4-vs-xfs.md#61-判据是文件系统不是机型)）。

### 4.2 HDBSS 的三层证据

| 证据层 | 950 | 920B |
|---|---|---|
| 内核配置 | `CONFIG_ARM64_HDBSS=y` | **没有这个选项** —— 不是关闭，是压根没编 |
| KVM 能力查询 | `KVM_CHECK_EXTENSION(502) = 1`（per-VM 也是 1） | `= 0` |
| **真开一把** | `KVM_ENABLE_CAP(502, order=1) = 0` ✅ | `= -1` |
| 内核日志 | `kvm [1580278]: Enable HDBSS success, HDBSS buffer size: 1` | 无 |
| VHE | `kvm [1]: VHE mode initialized successfully` | —— |

第三、四层是最硬的证据：**不是「声称支持」，是内核确认开成功了。**

但这张表证的仍然只是「能力打开了」，不是「硬件真的在记脏页」。要证到数据面还得加一层，
判据怎么设计、为什么必须先在 920B 上取到**负样本**才有资格拿到 950 上用，见
[第 25 篇 §6](25-functional-tests.md#6-hdbss_evidencepy能力不等于数据面)。

> 探测能力（`KVM_CHECK_EXTENSION`）可以在 KVM fd 上做，不建 VM；
> 但真正启用（`KVM_ENABLE_CAP`）必须在 VM fd 上，而且要先有 vCPU。
> 所以 orchestrator 启动时的探测只能做到第二层。

**结论**：950 上 Firecracker 会自动选 HDBSS，**不需要配任何环境变量**。

### 4.3 920B 上能验什么

920B 没有 HDBSS，跑的是 KVM 写保护路径。所以：

| 能验 | 不能验 |
|---|---|
| 全部**正确性** —— 内容回滚、树语义、失败语义 | HDBSS 本身 |
| 链深无关性（5 / 20 / 50 代） | 硬件标脏的性能收益 |
| 稳定性（200 次回滚 0 失败） | `FC_HDBSS_ORDER` 的调优 |
| 成本模型的**形状**（增量随改动量增长） | 950 的**绝对**数字 |

在 920B 上要跑 checkpoint，必须显式设 `FC_TRACK_DIRTY_PAGES=true`
（不设时按硬件判定为关）。

> 另有一项工具链差异值得记：**`perf` 只有 950 有**。
> 「HDBSS 真的省掉了 VM-Exit」这条证据要靠 `kvm_exit` 计数，只能在 950 上做。

---

## 5. buffer 大小

`cap.args[0]` 是**大小编码**，不是字节数（4 KiB 宿主页下它等于 `alloc_pages()` 的 order）。
**每个 vCPU 一份**：

| 编码 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| 每 vCPU buffer | 8 KiB | 16 KiB | 32 KiB | 64 KiB | 128 KiB | 256 KiB | 512 KiB | 1 MiB | 2 MiB |

默认是 **1（8 KiB）**，也就是内核默认值。

### 5.1 容量与溢出

一条记录 8 字节。8 KiB ≈ 1000 条 ≈ **4 MiB 的脏页覆盖量**（4 KiB 页）。

写密集负载下 buffer 会填满，触发额外的处理路径。极端情况下这部分开销可能把
「省掉写保护陷出」的收益吃掉相当一部分。

编码 9（2 MiB）能容纳约 26 万条记录，但要为**每个 vCPU** 分配 2 MiB
**连续物理内存** —— 在长时间运行、内存碎片化的宿主上这不是免费的。

### 5.2 尚未实测

`FC_HDBSS_ORDER` 取 1 / 2 / 4 在写密集负载下的对比，是
[第 28 篇 §8](28-results-and-compliance.md#8-尚未覆盖)明确列出的缺口之一。目前用默认值 1。

---

## 6. 部署检查清单

### 6.1 平台

```bash
# ① 内核编译了吗
grep CONFIG_ARM64_HDBSS /boot/config-$(uname -r)
# 或
zgrep CONFIG_ARM64_HDBSS /proc/config.gz
```

```bash
# ② VHE 模式吗
dmesg | grep -Ei 'kvm.*(VHE|nVHE).*initialized'
```

```bash
# ③ KVM 暴露 capability 502 吗（需要一个探针程序，见 test-950/cap_test.c）
gcc -O2 -Wall cap_test.c -o /tmp/cap_test && /tmp/cap_test
# 期望：KVM_CHECK_EXTENSION(cap=502) = 1
#      HDBSS capability is present and can be enabled.
```

```bash
# ④ 产物盘是什么文件系统（决定选哪套方案）
df -T /orchestrator/build
```

### 6.2 运行时

| 检查 | 怎么看 |
|---|---|
| orchestrator 判定了什么 | 启动日志的 `checkpoint capabilities`，看 `track_dirty_pages` 与 `track_dirty_pages_reason` |
| Firecracker 选了哪条路 | 沙箱的 instance info，`dirty_tracking` 字段：`hdbss` / `kvm-wp` / `off` |
| 内核确认开成功了吗 | `dmesg \| grep 'Enable HDBSS success'`，方括号里是 Firecracker 的 PID |
| 增量真的是增量吗 | 第二个 checkpoint 的 `memMode` 必须是 `incremental` |
| 二进制换对了吗 | `04-verify-runtime.sh` 会打出实际跑起来的 Firecracker 的 sha |

### 6.3 环境变量

| 变量 | 何时需要设 |
|---|---|
| `FC_TRACK_DIRTY_PAGES=true` | **无 HDBSS 的机器上要做 checkpoint 时**（如 920B）。有 HDBSS 时默认就是开的 |
| `FC_HDBSS_REQUIRED=true` | **生产回滚部署** —— 宁可启动失败，也不要静默走软件路径 |
| `FC_HDBSS_ORDER` | 写密集负载下调优，目前无实测依据，先别动 |
| `CHECKPOINT_FULL_ROOT=false` | 沙箱多、链短、存储紧、对象存储近的部署（[第 8 篇 §6.3](08-memory-diff-tree.md#63-全量根为什么是默认)） |
| `ORCHESTRATOR_BASE_PATH` | 产物盘不是默认位置时 |

---

## 7. 已知的部署陷阱

都是实际踩到过的，**其中两个不会报错**。

### 7.1 `/fc-versions` 的版本号与二进制脱钩

950 上 `/fc-versions/` 有 `v1.12.1` 和 `v1.13.1` 两个目录，
**orchestrator 实际读的是 `v1.13.1`，而放在里面的 Firecracker 其实是 v1.12 那一版**。

这是最早做 ARM 适配时留下的：目录名和二进制的真实版本脱了钩。不影响功能 ——
目录名只是个查找键。

> **但换二进制时必须记住**：要换的是 `v1.13.1/firecracker`。
> 换错了**等于没换，而且不会有任何报错**，表现只是「改动没生效」。
> `04-verify-runtime.sh` 会打出实际跑起来的 FC 的 sha，换完对一眼就知道。

### 7.2 nomad job 名不同

| | 950 | 920B |
|---|---|---|
| job 名 | `template-manager-system` | `template-manager` |

写死了 job 名的脚本在另一台上会找不到。

### 7.3 python 环境

950 上 `python3` 来自 conda 环境 `jll-e2b`（3.14.6），e2b SDK 装在那里面。
跑测试脚本前必须先 `conda activate jll-e2b`，否则会落到系统 python 上、
`import e2b` 直接失败。

### 7.4 要在 950 上跑 XFS 方案，只能用 loop 卷

950 的 LVM 已分完（`vg_free = 0g`），没有空闲裸盘或分区，
所以 XFS 卷只能是 loop 镜像。三项调优必须全开，否则数字没有参考价值：

| 调优 | 消掉什么 |
|---|---|
| `--direct-io=on` | 双份缓存与双回写 |
| `fallocate` 预分配 | 写路径上的 extent 分配与碎片 |
| `-b 4096` | 逻辑扇区不对齐导致的读改写 |
| `mkfs.xfs -K` | mkfs 默认的整盘 discard —— loop 会把它翻译成对宿主镜像**打洞**，预分配当场被捅穿 |

最后一条在 920B 上实际踩到过：第一次建完 300 G 镜像只实占 1 GB。

**即便调优过，loop 相对真盘仍有开销，所以这样测出来的 XFS 方案绝对耗时偏悲观。**
正确性和 reflink 省空间的效果不受影响（reflink 发生在镜像内部的 XFS 里）。

> ext4 方案在 950 上**不用造卷** —— 根盘本来就是 ext4，直接用，不设
> `ORCHESTRATOR_BASE_PATH`。这套是真正要交付的，它的数字必须来自真盘。

---

## 8. 小结

1. 脏页分两层：**要不要记**（orchestrator，`FC_TRACK_DIRTY_PAGES`）和
   **用什么方式记**（Firecracker 自动探测，无开关）。混淆这两层是最常见的配置错误。
2. HDBSS 工作在 **Stage-2**，CPU 把脏 GPA 写进每 vCPU 的 buffer，
   VM-Exit 时由内核汇总进标准 KVM 位图。**没有独立接口，对 VMM 透明** ——
   所以降级完全静默。
3. 六个前提，其中 **「必须在 vCPU 创建之后启用」不满足时完全静默** ——
   ioctl 返回成功，但一条脏页都记不到。
4. **HDBSS 不会被快照继承**，从快照加载的路径必须单独武装。e2b 沙箱只走这条路。
5. 950 上三层证据齐全（配置、能力、真开一把 + 内核日志），**不需要配任何环境变量**；
   920B 的内核**压根没编** HDBSS，只能验正确性。
6. 950 根盘是 ext4、920B 是 XFS —— 这解释了方案与机型为什么配成对，
   但**判据仍然是文件系统**。
7. 生产回滚部署应设 `FC_HDBSS_REQUIRED=true`，宁可启动失败也不要静默走软件路径。

---

## 代码位置

| 关注点 | 位置 |
|---|---|
| capability 常量与 ioctl | `src/vmm/src/arch/aarch64/vm.rs` — `enable_hdbss` |
| 后端选择与降级 | `src/vmm/src/vstate/vm.rs` — `setup_dirty_tracking`、`DirtyTrackingBackend` |
| 两个调用点 | `src/vmm/src/builder.rs` — `build_microvm_for_boot`、`build_microvm_from_snapshot` |
| 宿主探测与默认值 | `internal/sandbox/fc/dirtytracking.go` |
| 启动时能力上报 | `packages/orchestrator/main.go` — `reportCheckpointCapabilities` |
| 能力探针（C） | `e2b-infra/rollback/test-950/cap_test.c` |
| 宿主自检脚本 | `e2b-infra/rollback/test-950/{00-recon-950.sh,01-check-host.sh}` |
| loop 卷制备 | `e2b-infra/rollback/test-950/02-prepare-loop-volume.sh` |
| 换二进制与验证 | `e2b-infra/rollback/test-950/{03-switch.sh,04-verify-runtime.sh}` |
| 内核侧调研记录 | 工作区 `e2b-repo/HDBSS_KUNPENG950_KERNEL_6.6.0_515.md` |
| 950 摸底结论 | `e2b-infra/rollback/test-950/950-摸底结论.md` |

**下一篇**：[20 · 与原生 snapshot 的对比与配合](20-vs-native.md) —— 两条路径的完整对照，
以及怎么一起用。
