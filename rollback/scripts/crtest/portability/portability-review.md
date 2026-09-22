# 沙箱级快照回滚 —— 可移植性审查（客户 950 vs 我们这台 950）

审查日期：2026-09-17 · 只读审查，未改任何仓库

## 审查范围

| 仓库 | 位置 | diff 区间 | 内容 |
|---|---|---|---|
| infra-arm | `$R/tmp/wt-jll` | `c9a92a5ab..jll`（19 提交） | orchestrator checkpoint 服务 + shared/proxy，2689 增 / 232 删 |
| KASandbox（Firecracker） | `$R/KASandbox` | `e797b30..jll`（12 提交，含 3863c76 之前的 HDBSS 硬化 / sidecar / rollback API / seccomp） | 2058 增 / 42 删；另有 1 处未提交改动（串口清线改非致命） |
| e2b-arm（py-sdk） | `$R/e2b-arm` | `66414855..jll`（6 提交） | 1113 增 / 64 删 |
| 部署侧对照 | WSL `KASandbox_0904/` | — | `deploy/check-env.sh`、`deploy/CHECKPOINT.md`、`deploy/dep/`、`firecracker/`、`packages/orchestrator/`、`py-sdk/` |

---

## 一、结论（先看这段）

**真正"偏僻"、客户 950 上可能不成立的只有一项：openEuler 私有的 KVM 能力号 502（HDBSS）。**
而它已经是**软依赖**：orchestrator 启动时 `KVM_CHECK_EXTENSION(502)` 探一次，探不到就把脏页跟踪默认关掉，
功能照常、只是每次 checkpoint 全量拷贝 guest 内存。没有任何一条代码路径会因为缺 502 而失败。
只要客户的 950 跑的是同一个 openEuler 内核（`HDBSS_KUNPENG950_KERNEL_6.6.0_515`），502 就在。

除此之外的全部依赖，逐条核对下来是三类：

1. **标准内核 API，任何 arm64 KVM 都有**：`KVM_GET_DIRTY_LOG`、`KVM_SET_DEVICE_ATTR`(vgic dist regs)、
   `KVM_ARM_VCPU_INIT` 复用作 vCPU reset、`userfaultfd`、`mincore`、`/proc/self/pagemap`。
   都不是私货，也没写死内核版本判断。**`KVM_ENABLE_CAP = 0x4068_AEA3` 虽然是硬编码，但经内核头核对
   （`struct kvm_enable_cap` = 4+4+32+64 = 104 = 0x68）它就是上游通用值，不是 openEuler 私有**——
   源码注释写成"openEuler defines"是误导，建议改掉，但代码本身可移植。
2. **部署配置 / 路径**：全部走环境变量且有合理默认（`ORCHESTRATOR_BASE_PATH=/orchestrator`、
   `FIRECRACKER_VERSIONS_DIR=/fc-versions`、`HOST_KERNELS_DIR=/fc-kernels`）。
   checkpoint 代码里**没有一条硬编码路径**，产物目录由 `DefaultCacheDir/checkpoints` 推导。
   nomad job 名在交付 monorepo 里统一是 `template-manager`（`-system` 那套只存在于 920B 我们的开发栈），
   且代码不读 job 名。
3. **无需担心**：没有 `O_DIRECT`、没有 `FICLONE`/reflink、没有 `debugfs`、没有 `perf`、没有 `uname`/内核版本判断、
   没有文件系统类型判断（`deploy/CHECKPOINT.md` 写的"文件系统无特殊要求"是真的）。
   跨文件系统的封层移动有 `EXDEV` 拷贝回退。SDK 只要 python ≥ 3.10 + httpx 0.27~1.0，与上游 e2b 同口径。

**需要客户方注意但不是代码问题的**：无 HDBSS 时的性能悬崖、产物盘容量、大页数量、vhost_net 未加载、
以及"装的 Firecracker 必须是本仓库构建的"——这几项正是 `preflight-customer.sh` 要替客户当场判掉的。

---

## 二、逐条清单

### A. 硬件能力 / 内核私有

| # | 出处 | 依赖的是什么 | 客户机器上不同时 | 运行时探测与退化 | 建议 |
|---|---|---|---|---|---|
| A1 | `firecracker/src/vmm/src/arch/aarch64/vm.rs:69` `KVM_CAP_ARM_HW_DIRTY_STATE_TRACK = 502`（提交 98f3f53） | **openEuler 私有 KVM cap 号**（上游 Linux 无 502）。硬件为鲲鹏 950 的 HDBSS | 缺 502 → `KVM_ENABLE_CAP` 返回 EINVAL | **有**。`setup_dirty_tracking()`（`vstate/vm.rs:206`）捕获失败，默认退到 `DirtyTrackingBackend::KvmWriteProtect` 并 `info!` 记一行；`FC_HDBSS_REQUIRED=true` 时才升级为致命 `VmError::HdbssRequired` | 无需改代码。**预检里判 WARN 不判 FAIL**（已做） |
| A2 | `packages/orchestrator/internal/sandbox/fc/dirtytracking.go:13-14,58` 用 `KVM_CHECK_EXTENSION(0xAE03)` 查 502 | 同上，但**只查不启用**，不创建 VM | 查不到 → `trackDirtyPagesEnabled=false` | **有，而且是主判据**。`/dev/kvm` 打不开也只返回 false，不 panic | 无需处理。这是本轮最稳的设计点 |
| A3 | `vm.rs:71` `const KVM_ENABLE_CAP: u64 = 0x4068_AEA3` 硬编码 ioctl 号 | **标准内核 API**（经 `include/uapi/linux/kvm.h:1736` + 结构体大小核对，与上游一致） | 不会不同 | 不需要 | **只建议改注释**："openEuler defines…" → "generic KVM ioctl"，免得日后有人以为它是私货 |
| A4 | `FC_HDBSS_ORDER`（`vstate/vm.rs:223`），默认 1（8 KiB/vCPU） | 部署配置 | 写密集负载下缓冲区溢出 → 增量收益退化 | 非法值（解析失败）静默回落到 1 | 已在 `CHECKPOINT.md` §2；**建议补一句"非法值回落到 1 且不报警"** |
| A5 | `FC_HDBSS_REQUIRED`（`vstate/vm.rs:227`），默认 false | 部署策略 | 设成 true 且机器报告 502 但启用失败 → FC 启动即失败 | 语义即"故意不退化" | 已在 `CHECKPOINT.md` §2；口径正确 |

### B. GIC / 串口清线（本轮新增，风险最高的一块）

| # | 出处 | 依赖的是什么 | 客户机器上不同时 | 运行时探测与退化 | 建议 |
|---|---|---|---|---|---|
| B1 | `arch/aarch64/gic/regs.rs:257-299` `clear_intid_active_pending()`：`KVM_SET_DEVICE_ATTR` 写 `KVM_DEV_ARM_VGIC_GRP_DIST_REGS` 的 `GICD_ICACTIVER`(0x0380) / `GICD_ICPENDR`(0x0280)（提交 f0c3390） | **标准 KVM vgic uaccess 语义**。内核侧落在 `vgic_mmio_uaccess_write_cactive` / `vgic_uaccess_write_cpending`（`arch/arm64/kvm/vgic/vgic-mmio.c:458`、`vgic-mmio-v3.c:743`）。这套 uaccess 访问器自 Linux 4.12 起就在（GICv3 dist regs 的 uaccess 路由），6.6 当然有 | 内核 < 4.12 才没有——现实里不会遇到 | **有（本轮刚加的未提交改动）**：`rollback.rs:326` `clear_serial_gic_line` 已改为**不返回错误**，失败只记一次 `warn!`（`SERIAL_GIC_CLEAR_FAILED` 闩锁，后续降 `debug!`），消息明确写"best-effort，串口可能仍卡住，其它设备不受影响" | **把这个未提交的改动提交掉**——它正是"客户机器上 uaccess 语义万一不同也不会断功能"的保险 |
| B2 | 同上，GICv2 下是否成立 | `GICD_ICACTIVER`/`ICPENDR` 在 GICv2 与 GICv3 的**分发器偏移完全相同**，且 `vgic-mmio-v2.c:437,445` 同样注册了 `vgic_uaccess_write_cpending` / `vgic_mmio_uaccess_write_cactive`，属性组也都是 `KVM_DEV_ARM_VGIC_GRP_DIST_REGS`。源码注释（`gic/mod.rs:236`）的说法成立 | **但 GICv2 的 attr 编码带 cpuid 字段**，SPI（intid ≥ 32）时该字段被忽略；`regs.rs:66` 的 `attr = (mpidr & mpidr_mask) | offset`，清线路径传 mpidr=0，v2 的 `mpidr_mask()` 也是 0 → 编码正确 | 未在 GICv2 上实测；失败时走 B1 的非致命路径 | 预检里 **GICv2 判 WARN 并注明"理论成立、未实测"**（已做）。950 是 GICv3，实际不会踩到 |
| B3 | `rollback.rs` 用 `gsi + VGIC_NR_PRIVATE_IRQS(32)` 换算 INTID | ARM GIC 架构常量 | 不会不同 | — | 无需处理 |
| B4 | seccomp：`KVM_SET_DEVICE_ATTR (0x4018aee1)` | vmm 线程过滤器**本来就放行**（上游为 GIC save/restore 加的），清线不需要新开口 | — | — | 无需处理 |

### C. 其它内核 / libc 调用

| # | 出处 | 依赖的是什么 | 客户机器上不同时 | 探测/退化 | 建议 |
|---|---|---|---|---|---|
| C1 | `vstate/vm.rs:373` `libc::mincore()` + `utils/pagemap.rs:70` `/proc/self/pagemap`（`GET /memory/dirty`，e797b30 之前就有） | 标准 Linux API。pagemap 的 present 位不需要 `CAP_SYS_ADMIN`（PFN 才需要，这里不读 PFN） | 不会不同。容器里若屏蔽 `/proc/self/pagemap` 才会失败 | `PagemapError::OpenPagemap` 返回错误，不 panic | 无需处理。注意**这是"读也算脏"的判据**（见 memory 里的已知结论），与可移植性无关 |
| C2 | seccomp `aarch64-unknown-linux-musl.json`：新放行 vmm 线程 `pread64`（3863c76）、`madvise`，vcpu 线程 `KVM_SET_ONE_REG` / `KVM_SET_MP_STATE` / `KVM_ARM_VCPU_INIT` / `KVM_ARM_VCPU_FINALIZE`（b8befad） | 标准 syscall/ioctl 号，arm64 固定 | 不会不同 | 缺了会 `BadSyscall` 直接关机——但这份 json 随二进制一起交付 | **只改了 aarch64 那份**；x86_64 的 seccomp 未同步。arm 交付无影响，但若日后要 x86 需补 |
| C3 | `arch/aarch64/vcpu.rs:363` `restore_state_in_place` = 对已运行的 vCPU 再发 `KVM_ARM_VCPU_INIT`（b8befad，"vCPU 路线固定 reinit"） | **ARM 架构定义的 reset 语义**，标准 KVM 行为（`kvm_arch_vcpu_ioctl_vcpu_init` 会走 reset 路径） | 不会不同 | `KvmVcpuError::Init` 会让回滚失败 | 无需处理，但值得在手册里点名——它是"回滚不重建进程"的关键 |
| C4 | `userfaultfd`（orchestrator `uffd/`，c9a92a5ab 及之前） | `CONFIG_USERFAULTFD=y`；只用 `UFFDIO_REGISTER_MODE_MISSING`（**不依赖 `-WP`**，arm64 6.6 本来也没有 uffd-wp） | 内核没编 userfaultfd → 沙箱根本起不来（不是 checkpoint 独有） | 无 | **预检里查 `CONFIG_USERFAULTFD` / `/dev/userfaultfd`**（已做） |
| C5 | 4 KB 写跟踪差分（c9a92a5ab，基线非本轮） | guest 页 2 MB（hugetlb）+ uffd 按 guest 页大小注册；差分块 2 MB 零拷贝或拼 | 大页配置不同会影响收益而非正确性；追踪关闭则回退旧路径 | **有回退** | 预检里**参数化判大页数量**（已做） |
| C6 | `KVM_GET_DIRTY_LOG`（`vstate/vm.rs:437,450`），**读即清**语义 | 标准 KVM 行为 | 不会不同 | — | **未启用也未依赖 `KVM_CAP_MANUAL_DIRTY_LOG_PROTECT2`**（全仓无引用）。即代码依赖的是"读一次就清"的经典语义，这是所有内核的默认；不会被客户机器上的内核选项改变 |

### D. 服务端环境变量（本轮新增 3 个）

| 变量 | 出处 | 默认 | 非法值处理 | 写进 `deploy/CHECKPOINT.md` 了吗 |
|---|---|---|---|---|
| `FC_TRACK_DIRTY_PAGES` | `fc/dirtytracking.go:36` | 跟随 cap 502 | `!= "true"` 一律当 false（含拼写错误），但会在 `track_dirty_pages_reason` 里原样回显 | ✅ 已写 |
| `FC_HDBSS_ORDER` | `vstate/vm.rs:223` | 1 | 解析失败静默回落 1 | ✅ 已写 |
| `FC_HDBSS_REQUIRED` | `vstate/vm.rs:227` | false | 只认 `"true"`/`"1"` | ✅ 已写 |
| `CHECKPOINT_FULL_ROOT` | `checkpoint/service.go:338` | 开（`""`/`true`/`1`） | 其它一律当关 —— **拼错就静默关掉**，会悄悄改变容量与回滚行为 | ✅ 已写 |
| **`CHECKPOINT_LOCK_WAIT_TIMEOUT`** | `checkpoint/service.go:60,77` | 60s | 解析失败 / ≤0 → `Warn` 日志 + 回落默认 | ❌ **本轮新增，未写进文档** |
| **`CHECKPOINT_FC_CALL_TIMEOUT`** | `sandbox/checkpoint.go:27,33` | 2min | 同上，有 Warn | ❌ **本轮新增，未写进文档** |
| **`CHECKPOINT_FAULT_INJECT`** | `checkpoint/faults.go:22,94` | 不设 | 未知名字收进 `warnings`，启动时报 | ❌ **未写进文档**。这是测试用故障注入，**生产机器上误设会让 checkpoint 人为失败** |

> 另：`envdRestoreTimeout = 45s` 写死在 `service.go:56`，不可配。SDK 的 300 s 超时就是按它 + 快照时间算出来的，两边要一起改，目前没必要外露。

### E. 路径与部署形态

| # | 项 | 结论 |
|---|---|---|
| E1 | `DefaultCacheDir/checkpoints` | `cfg/model.go:27` `DEFAULT_CACHE_DIR` 默认 `${ORCHESTRATOR_BASE_PATH}/build`；`store.NewStore(root)` 由 main.go 传入。**代码里零硬编码**。注意 `NewStore` 会 `RemoveAll(root)`——独占目录，客户别把它指到共享目录 |
| E2 | `/fc-versions` | 代码侧是 `FIRECRACKER_VERSIONS_DIR` 默认值；但**部署脚本里是写死的**：`deploy/build.sh:548`、`deploy/dep/init-client.sh:141`、`deploy/remote-worker-setup.sh:36`、`helm/templates/{orchestrator,template-manager}.yaml`。客户按标准流程装就一致 |
| E3 | `ORCHESTRATOR_BASE_PATH` | `cfg/model.go:22` 默认 `/orchestrator`，全链路 expand |
| E4 | nomad job 名 `template-manager` vs `template-manager-system` | 交付 monorepo 里统一 `template-manager`（`deploy/nomad/template-manager.hcl`、`deploy/dep/deploy.sh:428`）。`-system` 只是 920B 我们开发栈的命名。**代码不读 job 名**，仅 `deploy/checkpoint_verify.py:146` 用它做进程名前缀匹配，两种名字都覆盖到了 |
| E5 | 封层文件移动 | `rootfs/rootfs.go` `SealedLayer.MoveInto` + `block/cache.go` `renameFile`：**跨文件系统 rename 失败（EXDEV）有拷贝回退**，且移动被挪到 resume 之后，不进冻结窗口。缓存盘与产物盘分属不同设备只是变慢，不会断 |
| E6 | 产物盘文件系统 | 只用 `os.Rename` + `pwrite` + 稀疏文件，**不用 `O_DIRECT`、`FICLONE`、reflink、fallocate 特殊模式**。`store.go:409,1113` 还刻意去掉了 fsync（"不承诺跨进程持久性"）。ext4/xfs 皆可 |

### F. Python SDK

| # | 项 | 结论 |
|---|---|---|
| F1 | python 最低版本 | `pyproject.toml:13` `python = "^3.10"`，与上游 e2b 一致，本轮没引入 3.11+ 语法 |
| F2 | httpx | `>=0.27.0, <1.0.0`，未变 |
| F3 | `Retry-After` 解析 | `e2b_connect/client.py` `retry_after_seconds()`：**只认 delta-seconds**，HTTP-date 形式返回 `None` 而不是瞎猜；解析异常一律吞掉返回 `None`。服务端 `service.go` 的 `writeBusy` 只发 `"1"`，两边对得上。无可移植性风险 |
| F4 | 重试语义 | `_resolve_retries()` 让 `Client(retries=0)` 覆盖装饰器默认，checkpoint create/restore 不重放（非幂等）。纯客户端逻辑 |
| F5 | 300 s 超时 | `connection_config.py` `CHECKPOINT_REQUEST_TIMEOUT = 300.0`，只作用于 create/restore；list/delete 仍 60 s，恰好在服务端 `CHECKPOINT_LOCK_WAIT_TIMEOUT=60s` 之上——**这个耦合没写进文档，客户若调大锁等待会让 list/delete 客户端先超时** |
| F6 | `e2b-traffic-access-token` 头 | 纯协议层，无环境依赖 |

### G. 网络代理（本轮新增）

`shared/pkg/proxy/` 的 `OnTransportError` / `WithRequestStart` 与 `orchestrator/internal/proxy/proxy.go` 的
`RestoreInProgress` / `RestoredSince`：全部是进程内 map 查询与时间比较，**不依赖任何宿主机能力**。无风险。

---

## 三、建议动作（按优先级）

**P0 —— 交付前必须做**

1. **把串口清线改非致命的那笔改动提交掉**（`$R/KASandbox` 工作区里 `firecracker/src/vmm/src/rollback.rs` 仍是 ` M`）。
   这是"客户机器 GIC/内核语义万一不同"的唯一保险，不提交等于没有。
2. **`deploy/CHECKPOINT.md` 补三个变量**：`CHECKPOINT_LOCK_WAIT_TIMEOUT`、`CHECKPOINT_FC_CALL_TIMEOUT`、
   `CHECKPOINT_FAULT_INJECT`。尤其第三个要写明"**生产环境绝不要设**"。
3. **把 `preflight-customer.sh` 纳入交付**（见下节）。

**P1 —— 建议做**

4. `CHECKPOINT.md` §2 补一句：`FC_TRACK_DIRTY_PAGES` 与 `CHECKPOINT_FULL_ROOT` **拼错等于关闭且不报错**，
   排查时以启动日志的 `track_dirty_pages_reason` 为准（它会原样回显你设的值）。
5. `CHECKPOINT.md` §7 补一行排查项：`list`/`delete` 客户端 60 s 超时 ≈ 服务端锁等待 60 s，
   调大 `CHECKPOINT_LOCK_WAIT_TIMEOUT` 时要同步给 SDK 传 `request_timeout`。
6. 改掉 `arch/aarch64/vm.rs:70` 的误导注释（"openEuler defines KVM_ENABLE_CAP"→ 它是通用值）。

**P2 —— 可选**

7. `CHECKPOINT_FULL_ROOT` 与 `FC_TRACK_DIRTY_PAGES` 对非法值也打一条 Warn（对齐两个 duration 变量的做法）。
8. `FC_HDBSS_ORDER` 解析失败时记一条 Warn。
9. 若日后要交付 x86_64，需同步 `resources/seccomp/x86_64-*.json`。

---

## 四、`preflight-customer.sh` 与 `deploy/check-env.sh` 的关系

**建议并列，不要合并。** 两者的判据、运行时机、失败含义都不同：

| | `deploy/check-env.sh` | `preflight-customer.sh` |
|---|---|---|
| 回答的问题 | "`build.sh --install --start` 能不能跑通" | "这台机器能不能跑沙箱级快照回滚" |
| 查什么 | 包管理器、docker/nerdctl、dep/ 离线包、harbor、nomad/consul、端口占用、`.env` | CPU 架构、`/dev/kvm`、GIC 版本、KVM cap 502、`CONFIG_USERFAULTFD`、nbd/vhost/tun、大页、产物盘 fs 与容量、FC 二进制身份、SDK、运行时 env |
| 何时跑 | 部署**前** | 部署前跑一遍（判硬件/内核/模块），装完**再跑一遍**（判 FC 二进制、SDK、env、capabilities 日志） |
| 重叠项 | 磁盘空间（针对 harbor 镜像盘） | 磁盘空间（针对 checkpoint 产物盘，可能是另一块盘） |

现状 `check-env.sh` 里**一条 KVM / 内核 / 快照相关的检查都没有**，合并会把一个"部署流程检查"变成两件事的大杂烩，
而且本脚本要在装完之后再跑一次（`check-env.sh` 不适合重复跑）。

**落地方式**：把 `preflight-customer.sh` 放进 `deploy/`，在 `deploy/USAGE.md` 与 `deploy/CHECKPOINT.md` §1 各加一行指引：

```
部署前：  bash deploy/check-env.sh            # 部署流程前置
          bash deploy/preflight-customer.sh   # 快照回滚能力前置
部署后：  bash deploy/preflight-customer.sh   # 复查 FC 二进制身份 / SDK / 脏页跟踪是否真的开了
          python3 deploy/checkpoint_verify.py --server-ip <IP>
```

可选地在 `check-env.sh` 末尾加一句提示（不是调用）："若要启用 checkpoint/restore，另跑 `preflight-customer.sh`"。

---

## 五、`preflight-customer.sh` 在 920B 上的实跑结果

`bash preflight-customer.sh --hugepages 1024 --min-free-gb 20`

```
== 1. CPU 与虚拟化 ==
  PASS  架构 aarch64            aarch64，6.6.0-28.0.0.34.oe2403.aarch64
  PASS  /dev/kvm 可读写         crw-rw---- root:kvm
== 2. 中断控制器（GIC） ==
  PASS  GICv3                   GICv3: GIC: Using split EOI/Deactivate mode
== 3. 硬件脏页跟踪 HDBSS（KVM capability 502） ==
  WARN  KVM cap 502             本机无 HDBSS。功能仍可用，但每次 checkpoint 全量拷贝 guest 内存。不是阻断项
== 4. 内核特性 ==
  PASS  CONFIG_KVM  y     PASS  CONFIG_USERFAULTFD  y     PASS  userfaultfd 运行时可用  /dev/userfaultfd
  INFO  CONFIG_BLK_DEV_NBD  m  /  /sys/kernel/debug 已挂载可读（可选）
== 5. 内核模块 ==
  PASS  nbd（已加载）   WARN  vhost_net（未加载但模块存在）   PASS  tun   PASS  /dev/net/tun
  INFO  nbd.nbds_max 4096
== 6. 大页与内存 ==
  PASS  大页数量 ≥ 1024         HugePages_Total=201948 free=201833
== 7. checkpoint 产物盘 ==
  INFO  ORCHESTRATOR_BASE_PATH  /mnt/ext4dev/orchestrator
  PASS  产物盘文件系统          ext4 —— 无特殊要求，不依赖 reflink/FICLONE
  PASS  产物盘剩余 ≥ 20G        279G 可用
  PASS  缓存盘与产物盘同源      /dev/loop1（封层 rename 零拷贝）
== 8. Firecracker 二进制身份 ==
  INFO  Firecracker 目录        …/tmp/fc-versions（来自 orchestrator 进程的 FIRECRACKER_VERSIONS_DIR）
  PASS  FC 含回滚 API           …/v1.13.1/firecracker (SaveDirtyBitmap×2, RollbackSnapshot×2)
  INFO  其它位置的 FC           /fc-versions/v1.13.1/firecracker RollbackSnapshot=0（不从这里取，仅提示）
  INFO  其它位置的 FC           /opt/e2b-infra/bin/firecracker RollbackSnapshot=0（同上）
== 9. Python SDK ==
  PASS  python ≥ 3.10  3.11     PASS  e2b SDK 带 checkpoint     PASS  httpx 0.28.1
== 10. 运行中的栈 ==
  PASS  orchestrator 进程  pid=597808 exe=…/orchestrator-ext4-252d9532c
  INFO  env ENVIRONMENT=local  FC_TRACK_DIRTY_PAGES=true  ORCHESTRATOR_BASE_PATH=/mnt/ext4dev/orchestrator
        FIRECRACKER_VERSIONS_DIR=…/tmp/fc-versions  ORCHESTRATOR_SERVICES=orchestrator,template-manager
        FC_HDBSS_ORDER / FC_HDBSS_REQUIRED / CHECKPOINT_FULL_ROOT / CHECKPOINT_LOCK_WAIT_TIMEOUT /
        CHECKPOINT_FC_CALL_TIMEOUT / CHECKPOINT_FAULT_INJECT 均未设（用默认值）
  INFO  capabilities 日志       没抓到（920B 的 orchestrator 不走 journald）

汇总: PASS 18  WARN 2  FAIL 0
结论: 这台机器可以跑沙箱级快照回滚。
      注意: 无 HDBSS —— 功能正确但每次 checkpoint 全量拷贝 guest 内存，慢一个量级。
```

两条 WARN 都是 920B 的已知事实（无 HDBSS；`vhost_net` 没 modprobe），不是脚本误报。
`FC_TRACK_DIRTY_PAGES=true` 在这台无 HDBSS 的机器上意味着走 `kvm-wp` 软件写保护——
这正好是 `CHECKPOINT.md` §2 表里的第三行，**预检把它如实显示出来了**。
