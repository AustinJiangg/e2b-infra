# kernel —— 调试内核的 config 与构建说明

调试内核是在发布版 guest 内核的基础上打开 KASAN、lockdep 等检查编出来的另一个内核，只用于压测：
它把"释放后使用""加锁顺序可能死锁"这类在发布版上只是概率事件的问题变成当场报告。
为什么需要、开了什么、它不能代替什么，见手册 [19 章 §7](../../docs/19-testing-and-debug-kernel.md)；这里只讲怎么编、怎么用。

| 文件 | 是什么 |
|---|---|
| `kernel-debug.config` | **完整的调试 config**（arm64，6.1.158；sha256 `e29e1e61…5270c7`）。2026-10-08 最后一轮调试内核压测用的就是它，以它为准 |
| `kernel-debug.fragment` | 相对发布版 config 的差异，按"显式打开 / 必须关掉 / 自动带出"分组并注明理由。供阅读和移植到别的内核版本时参考 |

## 相对发布版 config 改了什么

发布版 config 是代码仓的 `fc-kernels/configs/arm64/6.1.158.config` 经 `make olddefconfig` 之后的结果。调试 config 在它上面：

- **打开**：`KASAN`（`KASAN_GENERIC`、`KASAN_INLINE`、`KASAN_STACK`、`KASAN_VMALLOC`）、`PROVE_LOCKING`
  （带出 `LOCKDEP`、`PROVE_RCU`、`DEBUG_LOCK_ALLOC`、`DEBUG_MUTEXES`、`DEBUG_RT_MUTEXES`、`DEBUG_RWSEMS`、`DEBUG_SPINLOCK`、
  `DEBUG_WW_MUTEX_SLOWPATH`、`TRACE_IRQFLAGS`、`PREEMPT_COUNT`）、`DEBUG_ATOMIC_SLEEP`、
  `DEBUG_OBJECTS`（`DEBUG_OBJECTS_RCU_HEAD`、`DEBUG_OBJECTS_ENABLE_DEFAULT=1`）、`KALLSYMS_ALL`、
  `CONSTRUCTORS`、`STACKDEPOT_ALWAYS_INIT`、`UNINLINE_SPIN_UNLOCK`；`LOG_BUF_SHIFT` 从 17 调到 21。
- **关掉**：`VMGENID`；另有一组 `INLINE_{READ,WRITE,SPIN}_*` 被锁调试自动关掉。
- `DEBUG_LIST` 在发布版 config 里本来就是开的，调试内核沿用，所以不在差异里。

直接拿两份完整 config 做 diff 还会看到编译器版本、汇编器版本以及一批与调试无关的选项不同——那是 `olddefconfig`
按本机工具链补出来的，不是有意的改动；有意的改动只有上面这些，也就是 `kernel-debug.fragment` 的内容。

## 为什么必须关 `VMGENID`

lockdep 一旦报告过一次问题就把自己关掉，之后不再检查任何东西（`/proc/lockdep_stats` 里 `debug_locks` 从 1 变成 0）。
沙箱不是冷启动的，是从模板快照恢复出来的；恢复时基线内核的 vmgenid 驱动会收到"虚机代数变了"的通知，
并在硬中断上下文里调用一个会睡眠的通知链。这个问题与 gsd 的补丁无关，但 lockdep 照样报告它，然后自关。

结果是：不关 `VMGENID` 的调试内核上，每个沙箱的 lockdep 在恢复后的第一秒就已经失效，后面几百轮压测的死锁检测覆盖是零，
而且没有任何报错提醒这一点——压测照样跑完、照样"干净"。所以：

- 调试 config 里 `VMGENID` 必须是 `# CONFIG_VMGENID is not set`；
- **每一组调试内核压测结束时都要确认 `debug_locks` 仍为 1**。压测脚本在开机时和结束时各读一次 `/proc/lockdep_stats` 写进汇总；
  `stress_multitree.py` 把 `debug_locks: 0` 直接算失败，`stress_overlay_ckpt.py` 只记录，由 `../stress/analyze_stress.py` 在汇总时判。

## 怎么编

**源码树**：amazonlinux/linux 的 tag `microvm-kernel-6.1.158-15.288.amzn2023`，依次打上代码仓
`fc-kernels/patches/6.1.158/` 下的三个补丁（`0001-virtio_balloon-…`、`0002-overlayfs-hot-switch-checkpoint.patch`、
`0003-overlayfs-hot-switch-fixes.patch`）。与发布版内核是同一棵树，只是 config 不同（发布版内核的构建见手册 [23 章](../../docs/23-deployment.md)）。

**工具链**（新 920B 上实际用的）：gcc 12.3.1、GNU ld 2.41、flex、bison；在 arm64 机器上原生编译。

用独立的输出目录（`O=`）编，不要动源码树里已有的构建结果：

```bash
K=<打好三个补丁的内核源码树>
B=<临时构建目录>
mkdir -p $B/bld-dbg && cp kernel-debug.config $B/bld-dbg/.config
cd $K
make -s ARCH=arm64 O=$B/bld-dbg olddefconfig
grep -E 'CONFIG_VMGENID|CONFIG_KASAN=|CONFIG_PROVE_LOCKING=' $B/bld-dbg/.config   # VMGENID 应为 is not set，另两项为 y
make ARCH=arm64 O=$B/bld-dbg Image -j"$(nproc)" > $B/build-dbg.log 2>&1
```

产物是 `$B/bld-dbg/arch/arm64/boot/Image`，约 66 MB（发布版约 22 MB）。把它放到一个**单独的**内核根目录下，
目录结构与正式位置相同，文件名固定为 `vmlinux.bin`：

```bash
mkdir -p /fc-gsd/kernels-dbg/vmlinux-6.1.158
cp $B/bld-dbg/arch/arm64/boot/Image /fc-gsd/kernels-dbg/vmlinux-6.1.158/vmlinux.bin
```

不要覆盖 `/fc-gsd/kernels/` 下的发布版内核。

> 实际编过的做法是上面这种——直接用完整的 `kernel-debug.config`。用 `kernel-debug.fragment` 经内核自带的
> `scripts/kconfig/merge_config.sh` 合到发布版 config 上应当得到同样的结果，但这条路径**没有实际跑过**；
> 这样做的话，合并后务必核对上面 grep 的三项，再核对 `kernel-debug.fragment` 里每一行都出现在生成的 `.config` 里。

## 怎么用

调试内核与发布版内核一样是固化在模板里的，所以流程是"换内核目录 → 用它另建一个模板 → 在这个模板上跑压测"。
以下命令在 `scripts/` 目录下执行：

```bash
# 1. 把 gsd 栈切到调试内核目录（先确认机器上没有沙箱在跑）
GSD_KDIR=/fc-gsd/kernels-dbg bash ops/switch-gsd-new920b.sh gsd

# 2. 用这个内核另建一个模板。内存给 4096 MB：KASAN 的影子内存与隔离区要额外占内存
python build_template.py --alias <调试模板名> --memory 4096 --image <镜像仓库>/e2b-orchestration/ubuntu:22.04-custom-criu421

# 3. 起一个沙箱确认环境（在 guest 里看）：
#      grep debug_locks /proc/lockdep_stats        应为 1
#      dmesg | grep -i kasan                       应有 "KernelAddressSanitizer initialized (generic)"
#      dmesg | grep -i -E 'vmgenid|sleeping function'   应为空

# 4. 带看门狗跑压测，--template 指向调试模板；调试内核慢，STALL 要调大
STALL=300 bash stress/run_stress_watch.sh <输出目录> --template <调试模板名> --mode stress --rounds 300 --workers 6

# 5. 每组结束后看汇总里的 lockdep_end，debug_locks 必须仍为 1；多组一起汇总用
python stress/analyze_stress.py <输出目录的上一级> <前缀>

# 6. 切回发布版内核目录（不带环境变量再跑一次）
bash ops/switch-gsd-new920b.sh gsd
```

用完之后调试模板可以删掉，`/fc-gsd/kernels-dbg` 也可以删。留着调试模板不影响别的模板，但**不要拿它做任何功能验收或性能测量**：
调试内核慢得多、时序也不同，前者没有额外价值，后者的数字没有意义。反过来，调试内核也不能代替发布版内核上的压测，两者要各跑一遍。

第 4 步里 `STALL=300` 只是示例值，按机器负载和并行组数调整；判断标准是正常运行时不被看门狗误判为卡死。
