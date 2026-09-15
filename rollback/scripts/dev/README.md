# 开发态工具箱 `rollback/scripts/dev/`：XFS+reflink 与 ext4 差分树两套 checkpoint/restore

（本目录 2026-09-15 由 `rollback/test-950/` 迁来，内容未变，只换了位置。）

整个目录拷到被测机（950 / 920B）任意位置即可，脚本之间只用相对路径。
凭据的配法见下面「完整流程」，以及上一层的 [`../README.md`](../README.md)。

```
bin/                两套的 orchestrator 与 firecracker 二进制 + SHA256SUMS
01-check-host.sh    宿主自检：HDBSS 能力、reflink 支持、二进制身份（不改任何东西）
cap_test.c          KVM cap 502 探针，01 会自动编译调用
02-prepare-loop-volume.sh 造数据卷（xfs 或 ext4，loop 或真盘），loop 已调优
03-switch.sh        在两套之间切换：换二进制、改 env、重启 template-manager
04-verify-runtime.sh 切换后冒烟：跑的是哪套、脏页后端是不是 HDBSS、数据落在哪个文件系统
lib.py              测试脚本共用的 SDK 封装（开头 load_dotenv()，凭据从 .env 或环境变量取）
correctness.py      正确性 e2e（线性 / 前滚 / 分叉 / 删除 / 失败语义，内存+磁盘+blob 三重校验）
hdbss_evidence.py   HDBSS 三级证据：能力 / FC 自报 / 数据面
timing.py           耗时与存储：逐代 create、逐级 restore、链深 5/20/50 对照
loop.py             稳定性：N 次回滚的成功率与分位数
pause_verify.py     checkpoint 之后 pause/resume，磁盘数据必须原样回来（O_DIRECT 读回）
compat_matrix.py    checkpoint/restore 与原生 create/connect/pause/kill 的组合矩阵
bench-ckpt.py       耗时基准：全量 vs 增量各档分布、虚机冻结窗口、restore、持续速率
probe-ramp.py       判别连打劣化是"链变深"还是"写得多"（把脏页量拉开做同样多次）
freeze_probe.py     被 bench-ckpt.py 推进 guest 里跑的时钟采样器，用来量虚机冻结窗口
run-all.sh          一条命令跑完一套（correctness → timing → loop → bench → pause → compat），产出一份报告
耗时基准结论.md      两套的耗时对照与三个发现（连打劣化 / cowextsize / 冻结窗口口径）
数据卷与loop.md      为什么用 loop、三项调优、nodiscard 的坑、两台机器为什么不对称
```

> SDK 侧怎么用 checkpoint/restore，看 e2b-infra 仓库的
> `e2b-deploy/dep/e2b-sdk-checkpoint/使用说明.md`（部署后在 `/opt/e2b-infra/dep/e2b-sdk-checkpoint/`）。

## 数据卷

| | 950 | 为什么 |
|---|---|---|
| **ext4 套** | **直接用根盘 ext4**，不设 `ORCHESTRATOR_BASE_PATH` | 950 根盘本来就是 ext4。这是要交付的那套，数字必须来自真盘 |
| **XFS 套** | loop 镜像（`02-prepare-loop-volume.sh --fs xfs`） | 950 上 `vg_free=0`、没有空闲裸盘，XFS 卷只能是 loop |

920B 上正好相反（根盘是 XFS）。所以 **XFS 套的绝对耗时偏悲观、ext4 套的是实数**。

loop 的三项调优（direct-io / 预分配 / 4K 扇区）、`nodiscard` 那个坑、
撤销办法、以及 XFS 还要设的 `cowextsize`，**全在 `数据卷与loop.md`**。

## 两套的区别（决定 env 怎么配）

| | XFS 套 | ext4 套 |
|---|---|---|
| orchestrator | `bin/orchestrator-xfs` | `bin/orchestrator-ext4` |
| firecracker | `bin/fc-xfs` | `bin/fc-ext4` |
| 数据路径 | **必须**落在开了 reflink 的 XFS 上 | 任意文件系统 |
| `ORCHESTRATOR_BASE_PATH` | 指向 XFS 挂载点，如 `/mnt/xfsdev/orchestrator` | 不设（默认 `/orchestrator`，950 根盘 ext4） |
| 内存产物 | 每代 `mem_full`（克隆+覆写，物理增量靠 reflink） | 每代 `mem_diff`（稀疏差分），树根为全量 |

**两套的 FC 与 orchestrator 必须配对**，混用会失败：ext4 套的 orchestrator 依赖
`PUT /snapshot/save-dirty-bitmap`，只有 `fc-ext4` 有这个端点。

两套共同必须：`FC_TRACK_DIRTY_PAGES=true`。漏配则每次 checkpoint 都退化成全量，
测试照样"通过"但测的不是增量，是最容易漏掉的坑。

## HDBSS

**不需要任何开关，能用就用。** FC 在装载快照、注册完 guest 内存之后自动探测：
KVM cap 502 可用就启用硬件标脏，不可用就退回 KVM 写保护，两者喂给上层的是同一个
`KVM_GET_DIRTY_LOG` 位图，语义完全一致，只差性能。920B 上没有 HDBSS，跑的是写保护，
所以 920B 验过的正确性结论在 950 上一样成立。

**怎么知道到底用没用上**，三级证据，越往下越硬：

| 级别 | 看什么 | 在哪跑 |
|---|---|---|
| L1 能力 | 宿主 KVM 认不认 cap 502（`cap_test.c` 直接 `KVM_ENABLE_CAP` 试一把） | `01-check-host.sh` |
| L2 自报 | Firecracker 为**这个沙箱**选了哪个后端：`GET /` 的 `dirty_tracking` = `hdbss` / `kvm-wp` / `off` | `04-verify-runtime.sh`；另外 `lib.py` 让**每个**测试脚本开头都打印它，报告里不会出现"不知道这组数字是哪个后端跑出来的" |
| L3 数据面 | 能力启用 ≠ 硬件真在记脏页。写密集负载下：① `kvm:kvm_exit` 次数 vs 被写页数（软件写保护 ≈1 次/页，HDBSS 应远小于 1）② checkpoint 之后第一遍写 vs 第二遍写的耗时比（写保护下第一遍要为每页陷出一次，明显更慢；HDBSS 下两遍接近） | `hdbss_evidence.py` |

L3 的判据在 920B 上验证过：920B 没有 HDBSS，量到冷/热 = 4.5~5.4，正是写保护的特征。
950 上如果 `dirty_tracking=hdbss` 而冷/热仍然接近这个值，说明能力启用了但数据面没生效，
要拿 dmesg 和 FC 日志一起看。`kvm:kvm_exit` 那一项需要 `perf`，没装就只出冷热比。

`FC_HDBSS_ORDER` 可调每 vCPU 的 buffer 大小（默认 1 = 8KiB，编码见下表），
写密集负载下 buffer 溢出会吃掉收益，值得在 950 上用 1/2/4 各跑一轮 `timing.py` 对比。

| `FC_HDBSS_ORDER` | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| buffer/vCPU | 8K | 16K | 32K | 64K | 128K | 256K | 512K | 1M | 2M |

## 完整流程

凭据（`E2B_API_URL` / `E2B_API_KEY`）两种给法，任选其一：

```bash
# ① .env（推荐）：每台机器做一次软链，指向凭据的唯一来源 benchmark/.env
ln -s ../../benchmark/.env ../.env       # 即 rollback/scripts/.env
# lib.py 开头的 load_dotenv() 会从脚本所在目录逐级向上找到它，
# 本目录所有 .py 都经由 lib.py 取配置，所以一次配好全都能跑。

# ② export（.sh 脚本只认这种，04-verify-runtime.sh 需要）：
set -a; . ../.env; set +a
# 或者直接
export E2B_API_URL=http://<950-ip>:3000
export E2B_API_KEY=<key>
```

> `.sh` 脚本不读 `.env`，只读环境变量。用 ① 的话，跑 `04-verify-runtime.sh` /
> `run-all.sh` 之前补一条 `set -a; . ../.env; set +a` 即可。

```bash
bash 01-check-host.sh                      # 先看清楚这台机器有什么

# --- ext4 套：950 根盘就是 ext4，直接用真盘，不造卷、不设 BASE_PATH ---
bash 03-switch.sh ext4 --yes
bash 04-verify-runtime.sh ext4 /
python3 hdbss_evidence.py                  # HDBSS 三级证据，只需跑一次
bash run-all.sh ext4 /                     # 六个脚本一条龙，含 pause 与兼容矩阵

# 也可以单独跑（两个都不需要参数，用上面那两个环境变量）：
#   python3 pause_verify.py                # checkpoint 之后 pause/resume 的数据一致性
#   python3 compat_matrix.py               # 与原生生命周期操作的兼容矩阵

# --- XFS 套：950 上只能 loop，先造卷 ---
bash 02-prepare-loop-volume.sh --fs xfs --mnt /mnt/xfsdev --size 300G
bash 03-switch.sh xfs --yes --base /mnt/xfsdev/orchestrator
bash 04-verify-runtime.sh xfs /mnt/xfsdev
bash run-all.sh xfs /mnt/xfsdev

# XFS 套跑完，loop 是临时的，撤掉即可，不动宿主任何东西：
#   umount /mnt/xfsdev && losetup -d /dev/loopN && rm -f /home/e2b-xfsvol.img

bash 03-switch.sh restore                  # 跑完把 950 还原成切换前的样子
```

`03-switch.sh` 每次改动前都会把被替换的文件和 nomad 配置备份到 `backup/<时间戳>/`，
`03-switch.sh restore` 用最近一次备份还原。

## 判据速查

| 步骤 | 必须看到 |
|---|---|
| 01 | `cap 502: supported`（否则 950 上也没有 HDBSS，先查内核）；`reflink: yes`（XFS 卷上） |
| 02 | `direct-io ✓`、`逻辑扇区 4096 ✓`、`预分配 ✓`、`FICLONE 实测可用 ✓` 四项齐 |
| 04 | `orchestrator: <期望的那套>`、`firecracker sha: <期望的>`、`dirty_tracking: hdbss`、`store fs: xfs/ext4` 与预期一致 |
| correctness | 末行 `ALL PASS` |
| timing | 末尾 `ALL CORRECT`，且 XFS 套的第 2..n 代 df 增量远小于脏页量（reflink 生效） |
| loop | `failures: 0` |
| bench | 末行 `BENCH OK`；各档 `mem_mode` 全是 incremental（出现 full 就是脏页跟踪没生效）；冻结窗口那张表里每档都标着「可区分 ✓」（标⚠说明冻结太短、被测量底噪淹没，数字不作数） |
| hdbss_evidence | L1 `supported` + L2 `hdbss` + L3 冷/热接近 1（写保护时是 4~5） |
| pause_verify | 末行 `✓ 全部通过`；尤其那条「**checkpoint 之前**写的 16MB 在 pause/resume 后完好」——它守的是一个**静默**的数据损坏（走 guest page cache 读会完全盖住，必须 O_DIRECT） |
| compat_matrix | 末行 `✓ 没有 BROKEN`。**`BROKEN` 一项都不能有** —— 那意味着某个 e2b 原生能力被 checkpoint/restore 弄坏了。`REFUSED` 是边界不是故障（当前已知两条：pause/resume 之后回不到 pause 之前的 checkpoint；kill 之后不能 connect，后者与我们无关） |

任何一步不过就停下来，把该步的输出和
`/data/nomad/alloc/*/alloc/logs/start.stdout.0` 的相关片段一起看。
