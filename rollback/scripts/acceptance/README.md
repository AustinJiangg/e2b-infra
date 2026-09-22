# `rollback/scripts/acceptance/` —— 交付态验收脚本

五个脚本，**互不依赖，也不依赖本目录以外的任何文件**：拷哪个 `.py` 到目标机就能跑哪个。
`dev/` 与 `crtest/` 那两套共享库、有开发态假设，和这里不要混用。

想要「部署完几行命令跑完一轮」，别一个个手跑，用 [`../950/run.sh`](../950/README.md)。

## 依赖与凭据

```bash
pip install e2b==2.20.0 python-dotenv
python3 /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py   # checkpoint/restore 的 SDK 覆盖层
python3 /opt/e2b-infra/patch_e2b.py                        # 顺序不能反
```

五个脚本开头都是 `load_dotenv()`（**不带路径**，从脚本所在目录逐级向上找 `.env`），
所以要么在本目录放一个 `.env` 软链（见 [`../README.md`](../README.md)），
要么先 `set -a; . /opt/e2b-infra/.env; set +a` 把变量 export 出来 —— 已 export 的变量优先，
`.env` 不会覆盖它们。需要的四个变量：`E2B_API_KEY` / `E2B_DOMAIN` / `E2B_API_URL` / `E2B_HTTP_SSL`
（外加 `E2B_ACCESS_TOKEN`）。

**都要在宿主机上跑。** 远程只拿得到客户端墙钟；服务端分段计时、产物实占、脏页后端、
产物盘文件系统这四类信息只有在宿主机上才读得到（手册 24 篇 §5.2）。

## 五个脚本

| 脚本 | 一句话 | 常用参数 | 判定行 | 920B 实测耗时 |
|---|---|---|---|---|
| `checkpoint_verify.py` | 功能正确性 59 项：三代现场，内存 / 根文件系统 / 删除 / 权限位全查，并用心跳进程的 pid + 启动时间证明是**内存**回来了而不是虚机重启 | `--mem-mb 128 --disk-mb 32 --rounds 3` | `✓ 59 项校验全部通过。` | 约 31 s |
| `checkpoint_bench.py` | 耗时基准：一条链上全量 + 每档增量再逐级走回；`--split` 把改动拆成内存和文件两份；抓「刚写完」与「宿主平静后」两遍 | `--tiers 0,16,64,256 --split 3:1` | `每一跳都落到了目标代…` | 随档位数 |
| `checkpoint_bench_v2.py` | 耗时基准的**对照组口径**：照搬进程级那套 `demo_checkpoint_perf.py` 的档位表与两张汇总表，服务端分段从 `timings.json` 读 | 同上 | 同上 | 约 12 s |
| `native_snapshot_bench.py` | e2b **原生** snapshot（`--mode pause` / `--mode snapshot`）的同一张档位表，外加 `touch` 懒加载列；三套横向对照里代表原生那套 | `--mode pause` | — | 约 44 s |
| `checkpoint_concurrent.py` | 并发四段：**A** 跨沙箱扇出（N=1…16，barrier 对齐）、**B** 同沙箱多调用方争用、**D** 混合稳态、**C** 生命周期竞争 | `--stages A,B --fanout 1,2,4,8,16 --soak-seconds 180 --out x.json` | 各段表尾的对账行 | 随 `--fanout` / `--soak-seconds` |

### `checkpoint_concurrent.py` 的两个坑

- **C 段默认关，要 `--lifecycle` 才跑，而且可能把 orchestrator 打挂** —— 它是去看
  「checkpoint / restore 进行到一半把沙箱 kill 掉」的症状，服务端对这两条路目前没有互斥。
  宿主机上有别人的沙箱时**不要开**。
- **B 段不断言现场逐项一致** —— 并发下「拍的那一刻现场是什么」本身没有定义。
  它断言的是：操作全部收敛、没有错误、沙箱还活着、每个 checkpoint 都回得去且回去之后自洽。

## 判定行速查

| 看到 | 说明 |
|---|---|
| `✓ 59 项校验全部通过。` | 正确性过了 |
| `✓ 57 项校验…` | SDK 缺 `mem_mode` 字段，**增量判据失效** —— 回去重跑 `install.py`（先它、后 `patch_e2b.py`） |
| 开头 `脏页后端 : 硬件标脏` | `hdbss`，950 应当是这个 |
| 开头 `脏页后端 : 软件写保护` | `kvm-wp`，920B 是这个；checkpoint 耗时里含 VM exit 开销 |
| 开头 `产物落盘 : 未知（本脚本没跑在宿主机上？）` | 跑错机器了，服务端那几列全会缺 |

达标线（手册 28 篇，照抄客户那组粗略指标，未限定改动量）：
**checkpoint ≤ 200 ms、restore ≤ 100 ms**，量的都是客户端墙钟；
全量 checkpoint 单列不判定。逐档对照表由 `../crtest/bench/compliance.py` 出。

## 输出

`checkpoint_verify.py` / `checkpoint_bench*.py` / `native_snapshot_bench.py` 只打屏，
**自己 `tee`**；`checkpoint_concurrent.py` 另有 `--out` 出一份 JSON（原始数据在里面，
表格只是摘要，分位数要自己算的话读 JSON）。

报告目录命名与归档位置见 [`../README.md`](../README.md) 末尾。
