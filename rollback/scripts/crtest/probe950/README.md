# probe950 —— 950 与 920B 的差异探测

本轮所有修复都在 920B 上开发验证，最终要交付在 950。这套脚本回答一个问题：
**920B 上跑出来的结论，哪些能原样搬到 950，哪些不能。**

用法是「两台机器各跑一遍同样的探测，把两份结果逐键 diff」。950 没有网，
所以脚本设计成零依赖、可整目录拷贝、缺什么工具就记 `<absent>` 而不是崩。

问题编号引用 `e2b-repo/checkpoint-restore-fix-plan-2026-09-17.md`（本轮总方案）
与 `e2b-repo/950-vs-920b-differences-and-risks.md`（差异清单）。

---

## 1. 文件

| 文件 | 说明 |
|---|---|
| `probe-host.sh` | 静态探测。root 跑，不需要 e2b 栈起着，不建沙箱。输出 `probe-host-<hostname>-<时间>.txt`（每行 `KEY=VALUE`）和同名 `.json` |
| `probe-dynamic.py` | 动态探测。需要栈起着，建 **1 个**沙箱、跑完必删。输出 `probe-dynamic-<hostname>-<时间>.json` + 同名 `.txt`（人读摘要） |
| `compare.py` | 把两台机器的 `probe-host-*.json` 逐键 diff 成表（不同 / 一边缺失 / 相同），已知重要键标 ★ |
| `results/920b/` | 920B 的对照结果（已跑好，随目录一起带到 950） |

---

## 2. 拷到 950 怎么跑

在 920B（或 WSL）上打包：

```bash
cd "$(dirname "$(python3 -c "import crtest,os;print(os.path.dirname(crtest.__file__))" 2>/dev/null || echo .)")"  # 或直接 cd 到本套件根目录
tar czf probe950.tgz probe950/
# 拷到 950（无网：走跳板机 / U 盘 / 已有的传输通道都行）
```

在 950 上：

```bash
tar xzf probe950.tgz && cd probe950

# ① 静态（先跑这个，不碰栈，随时可跑）
bash probe-host.sh -o results/950
#    唯一会写盘的是 fsync 微基准：在 checkpoint 产物目录下建一个临时目录，跑完即删。
#    不想写盘就加 --skip-fsync（但那样就没有本轮最关心的 fsync 对照数据了）。

# ② 动态（要求栈起着、:3000 在监听）
conda activate jll-e2b          # 950 的 python3/e2b SDK 在这个环境里
python probe-dynamic.py --env-file <950 上 dotenv 的路径> --out results/950/probe-dynamic-950.json
#    950 上 dotenv 的路径与 920B 不同，**必须显式传 --env-file**；
#    路径不存在且环境变量里也没有 E2B_API_KEY 时脚本直接报错退出，不会瞎跑。
#    只跑其中几步：--only guest,checkpoint,restore,vgic,writeheavy 里挑
#    写密集档想小一点：--sizes 16,128

# ③ 与 920B 对照
python compare.py results/920b/probe-host-*.json results/950/probe-host-*.json --only-star
python compare.py results/920b/probe-host-*.json results/950/probe-host-*.json > diff-full.txt
```

**跑完把这几个文件拷回来**（只有这几个，其它都是中间产物）：

```
results/950/probe-host-<hostname>-<时间>.txt
results/950/probe-host-<hostname>-<时间>.json     ← compare.py 要的就是它
results/950/probe-dynamic-950.json
results/950/probe-dynamic-950.txt
```

### 950 上已知的几处坑（来自 `950-摸底结论.md` §5/§6）

- **nomad job 名叫 `template-manager-system`**，不是 `template-manager`。`probe-host.sh`
  两个名字都查，不用改。
- **python 要用 conda 的 `jll-e2b`**（950 上是 3.14.6，920B 是 3.12）；系统 python3 里没有 e2b SDK。
- **`:3000` 曾经没起**。没起就只能跑静态那半；`probe-host.sh` 的 `listen.3000` 会记下来。
- `/fc-versions/` 下有 `v1.12.1` 和 `v1.13.1` 两个目录，**真正生效的是 `v1.13.1`**，
  里面装的却是 v1.12 的二进制。`probe-host.sh` 把每个目录的 sha256/`--version` 都记下来，
  换二进制后对一眼就知道换没换对。
- 950 有 `perf`、没有 `docker`；920B 反过来（09-17 实测 920B 也有 perf 了）。脚本对缺失一律记 `<absent>`。

---

## 3. 每项探测对应哪个问题

### `probe-host.sh`

| 探测项（键前缀） | 对应问题 | 为什么要看 |
|---|---|---|
| `host.*` `cpu.*` `numa.*` `mem.*` `hugepages.*` `thp.*` | 差异清单 §1 | 950 是 96 核 ×2 路 ×2 线程（SMT）、1.1 TB、大页 109 GB；920B 是 128 核无 SMT、2.0 TB、大页 394 GB。并发档位与定时器类表现的上限都在这 |
| `kcfg.CONFIG_ARM64_HDBSS` | 方案 §0「脏页后端」 | 950 有、920B **压根没编**。这是两台机器最根本的差别 |
| `kcfg.CONFIG_HAVE_ARCH_USERFAULTFD_WP` | 原生 snapshot 脏页判据 | arm64 6.6 没有 uffd-wp，是 2 MB 差分块锁死的前提 |
| `kcfg.CONFIG_ARM_GIC_V3*` `CONFIG_ARM_ARCH_TIMER*` | S1 串口 / S2 定时器 | 串口楔死与定时器风暴都落在 vgic/vtimer 上，内核选项不同则结论不能搬 |
| `kcfg.CONFIG_SECCOMP*` | FC 改动自查清单 | F1 第二版就是栽在 vmm 线程的 seccomp 白名单上 |
| `kvm.cap502.*` `kvm.hdbss_usable` | 方案 §0、`两个环境变量与已知坑.md` | `KVM_ENABLE_CAP(502)` 真开成功才叫有 HDBSS。920B 是 `-1/EINVAL`，950 应是 `0` |
| `debugfs.kvm.readable` | X1 | `vgic-state` 在 `/sys/kernel/debug/kvm/<pid>-<vmfd>/` 下；读不到，X1 在 950 上就做不了 |
| `irq.arch_timer.*` `clocksource.*` `timer.cntfrq` | S2 / T25 | 定时器风暴的坐标系（宿主机 CNTFRQ、时钟源） |
| `gic.dmesg.*` | S1 | GIC 版本、SPI 数、有没有 ITS |
| `store.*` `tune2fs.*` `nvme.*` `loop.*` | 差异清单 §1「产物盘」 | 950 是真 NVMe ext4，920B 是 XFS 上的 loop 镜像。正确性无差，**性能倍数不可外推** |
| **`fsync.single.*` / `fsync.concurrent.*` / `fsync.ratio_p50`** | 并发报告 A4/B6、方案第 2 轮 | 「增量 create 随 N ×7.4」的直接对照：单线程 vs 16 线程 4 KB 写+fsync 的 p50/p99。第 2 轮去 fsync 前后、两台机器之间，都拿这组数说话 |
| `e2b.env.FC_TRACK_DIRTY_PAGES` | 差异清单 §3.4 | 950 靠自动探测默认开，920B 是显式强开——路径不同 |
| `e2b.env.LOCAL_TEMPLATE_STORAGE_BASE_PATH` | `950-摸底结论.md` §6.3 | 不设时默认落 `/tmp`，而 950 的 `/tmp` 是 tmpfs，一重启模板全丢 |
| `fc.*.sha256` `.version` `.savedirtybitmap` | 换栈核对 | `SaveDirtyBitmap` 计数 = 2 才是我们这套带 checkpoint 的 FC；官方构建是 0 |
| `e2b.orchestrator_sha256` `guestkernel.*` | 换栈核对 | 跑起来的到底是不是这一轮编的那个二进制 |
| `sdk.count.*` | K1/K2/K3、并发报告 3.4 | `E2b-Sandbox-Id`=2 → 带 `connect()` 路由头修复；`_resolve_retries` → K2；`e2b-traffic-access-token` → K1；`CHECKPOINT_REQUEST_TIMEOUT` → K3。**注意 K2 的代码在 `e2b_connect/` 包里，脚本已把 site-packages 下所有 e2b* 包一起数** |
| `tool.*` | 差异清单 §1 | perf 只有 950 有（HDBSS 的 kvm_exit 证据只能在那边取）；docker 只有 920B 有 |

### `probe-dynamic.py`

| 步骤 | 对应问题 | 判据 / 看什么 |
|---|---|---|
| 1 `guest` | X1、S2 的坐标系 | guest 内核、vCPU 数、页大小，以及 `/proc/interrupts` 里每个中断的 **INTID**（ttyS0/virtio0-2/arch_timer）。第 4 步就是拿这些 INTID 去 vgic-state 里定位的 |
| 2 `checkpoint` | 方案 §0、差异清单 §3.4 | FC `GET /` 的 `dirty_tracking`（950 应为 `hdbss`，920B 是 `kvm-wp`）；两次 create 的 `mem_mode`（第二次应为增量）与产物实际大小 |
| 3 `restore` | **E1** + **T25**（方案 §3 进度、§4.2） | E1：`/proc/uptime` 必须倒退（既定语义，920B 上 10/10 次倒退 −6.0～−6.4 s）。T25：`sleep 1` = 1.0±0.1 s、10 s 窗口 CPU0 空闲 ≥ 90%、arch_timer < 200 次/s（捕捉定时器风暴） |
| 4 `vgic` | **X1**（S1 串口楔死） | restore 之后读 `vgic-state`，打印 ttyS0 SPI、三个 virtio SPI、PPI 27（vtimer）、PPI 30（ptimer）的 `PLAEHCGN` 位。920B 上楔死时 ttyS0 那条是 `P=1 A=1 E=1`。再让 guest 往 `/dev/ttyS0` 写 4 KB（`timeout 5`）：**rc=124 就是楔死**（T26） |
| 5 `writeheavy` | 差异清单 §3.1/3.2（950 独有风险） | 16/128/512 MB 三档写密集：记每档的 `mem_mode`、产物目录大小与最大文件（= 内存差分）的实际字节数，restore 后做 md5 校验（口径同 `m0-verification/g2_diff_correctness.py`）。**HDBSS buffer 溢出**会表现为 md5 不一致；**4 KB 差分在 2 MB 大页上失效**会表现为差分大小不随脏集缩小 |
| 6 收尾 | 920B 现场纪律 | kill 沙箱，并确认**自己这个沙箱**的 firecracker 已退出（最多等 30 s）。总数 `pgrep -c -x firecracker` 也记，但只作参考——920B 上别人也在建沙箱，总数随时会变 |

---

## 4. 920B 的对照结果（`results/920b/`，2026-09-17）

跑这套脚本时 920B 的栈是第 2 轮的 orchestrator（`ed03fe57c`）+ 第 1 轮的 FC
（`tmp/fc-versions/v1.13.1/firecracker`，`SaveDirtyBitmap` 计数 = 2）。
完整数据在 `results/920b/` 下的四个文件里，下面是 950 上最该逐条对的几行。

**静态（`probe-host-*.txt`，339 项）**

| 键 | 920B 的值 | 到 950 上期待看到什么 |
|---|---|---|
| `host.kernel` | `6.6.0-28.0.0.34.oe2403.aarch64` | `6.6.0-159.4.3.154.oe2403sp4`，差 131 个小版本 |
| `cpu.model` / `cpu.count` / `cpu.threads_per_core` / `cpu.sockets` / `cpu.numa_nodes` | Kunpeng 920 7270Z / 128 / 1 / 2 / 4 | Kunpeng 950 7592C，96×2×2（**SMT=2**） |
| `mem.MemTotal` / `mem.HugePages_Total` | 2 111 990 472 kB / 201 948 | 约 1.1 TB / 约 55 768（大页只有 109 GB） |
| `kcfg.CONFIG_ARM64_HDBSS` | `<absent>`（**压根没编**） | `y` |
| `kvm.cap502.check_extension` / `enable_cap` / `kvm.hdbss_usable` | `0` / `-1`（EINVAL）/ `no` | `1` / `0` / `yes` |
| `debugfs.kvm.readable` | `yes` | 必须也是 yes，否则 X1 做不了 |
| `clocksource.current` / `timer.cntfrq` | `arch_sys_counter` / `100.00MHz` | 对一下频率 |
| `store.device` / `store.fstype` / `store.journal_mode` | `/dev/loop1` / ext4 / ordered（默认） | 真 NVMe 上的 ext4 |
| **`fsync.single.p50_ms` / `p99_ms`** | **0.033 / 0.282** | 真盘的绝对值会不同，重点看下一行 |
| **`fsync.concurrent.p50_ms` / `p99_ms`（16 线程）** | **0.255 / 0.669** | —— |
| **`fsync.ratio_p50`** | **7.82**（16 线程相对单线程的 p50 放大） | 与并发报告里「增量 create 随 N ×7.4」同量级，两者互为佐证。950 上这个倍数若明显更小，说明那条结论不能原样搬 |
| `e2b.env.FC_TRACK_DIRTY_PAGES` | `true`（显式强开） | 950 靠自动探测，预期不设也为开 |
| `sdk.e2b_version` / 四个指纹计数 | 2.20.0；`E2b-Sandbox-Id`=2、`CHECKPOINT_REQUEST_TIMEOUT`=3、`_resolve_retries`=3、`e2b-traffic-access-token`=1 | 950 上装的是 08-29 那版，四个数大概率都更小——装 SDK 前后各跑一次 |

**动态（`probe-dynamic-920b.txt`）**

- `dirty_tracking=kvm-wp`、`vmm_version=1.12.1`（950 应为 `hdbss`）
- 两次 create：`full` 1.94 s / 产物实占 2.0 GB；`incremental` 0.017 s / 产物实占 3.4 MB
- **E1**：uptime `9.180 → 2.650`（Δ −6.53 s），倒退 = True（既定语义）
- **T25**：`sleep 1` = 1.001 s、10 s 窗口 CPU0 空闲 99.4%、arch_timer 35 次/s（阈值 200）、date 偏差 0.02 s —— 本次无定时器风暴
- 服务端 restore 分段：`frozen` 30.8 ms、`materialize` 8.3 ms、`fc_total` 6.5 ms、`fc_bitmap` 1.53 ms、`fc_memory` 3.67 ms、`fc_rollback` 6.88 ms、`conntrack` 9.97 ms
- **X1 / vgic-state**（`/sys/kernel/debug/kvm/<fcpid>-<vmfd>/vgic-state`，本次可读）：
  - ttyS0 SPI 67 = `00010010`（E=1、G=1；**P=0 A=0**，没楔死）
  - virtio0/1/2 SPI 64/65/66 = `00010010`（写串口后 virtio1 一次采到 `00110010`，即 L=1 线电平拉起，属正常在途中断）
  - vtimer PPI 27（vcpu0/1）= `00011110`、ptimer PPI 30 = `00001110`
  - 对照：920B 上楔死时 ttyS0 那条是 `P=1 A=1 E=1`
- **T26**：guest 写 `/dev/ttyS0` 4 KB，rc=0、0.03 s，未楔死（楔死时 rc=124）
- **写密集三档**（脏集 → 差分文件实占）：16 MB → 31.3 MB、128 MB → 147.9 MB、512 MB → 562.9 MB；
  三档 md5 全对。差分表观大小恒为 2 GB（稀疏文件），所以**只看实占**。
  这条曲线就是 950 上判断「HDBSS + 2 MB 大页会不会让 4 KB 差分失效」（差异清单 §3.2）的基准：
  950 上若三档实占都逼近整份内存，就是失效了。
- 收尾：自己的 firecracker 已退出 = True（总数因别人的沙箱可能不变/变化，以这一行为准）

## 5. 脚本的约束（改之前先看这几条）

- **不动栈、不重启服务**：两个脚本都只读，唯二的写是 fsync 微基准的临时目录和自己的 results。
- **动态探测只建 1 个沙箱，且一定 kill**（`--keep` 才留）。跑之前脚本会记下别人的
  firecracker 进程数，收尾时打印是否回到基线。
- **任何一步失败不中断后面的步骤**：`probe-dynamic.py` 把异常记进 JSON 的 `errors` 继续跑；
  `probe-host.sh` 全程 `set -u`（没有 `-e`），每条外部命令带 `timeout`。
- **不要 import crtest**：这个目录要能整个拷到别的机器上单独跑，所以 `fc_get`、
  `/proc/interrupts` 与 `/proc/stat` 的解析、T25 判据都是从 `rollback-tests/crtest/common.py`
  **复制**过来的（那边也是同样理由从 `checkpoint_concurrent.py` 复制的）。改判据时两边都要改。
