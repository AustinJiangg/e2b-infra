# `rollback/scripts/` —— 快照回滚相关脚本

原先散在 `e2b-infra/benchmark/`（交付态验收 + 探针）和 `e2b-infra/rollback/test-950/`
（开发态工具箱）两处，2026-09-15 集中到这里。`benchmark/` 只留启动耗时/并发那套压测，
**`benchmark/.env`、`benchmark/.env.example`、`benchmark/sync-env.sh` 不动，仍是凭据的唯一来源。**

```
acceptance/   交付态：单文件、零共享依赖，拷两个文件到目标机就能跑，验收方用
probes/       开发态探针：回答某个具体机理问题，以及三套实现的横向对照
dev/          开发态工具箱：宿主自检 / 造数据卷 / 换二进制 / 基准 / 出报告（原 test-950/）
```

## 凭据：每台机器做一次软链

```bash
ln -s ../../benchmark/.env rollback/scripts/.env
```

所有 Python 脚本开头都是 `load_dotenv()`（`dev/` 下的经由 `dev/lib.py`）。
python-dotenv 从**脚本所在目录逐级向上**查找 `.env`，所以这一个软链对
`acceptance/`、`probes/`、`dev/` 三个子目录全都生效，不必每处放一份。
已经 `export` 好的环境变量优先，`.env` 不会覆盖它们。

`.env` 本身不入库（见 `.gitignore`）。模板看 `benchmark/.env.example`。

`dev/` 下的 `.sh` 脚本不读 `.env`，只读环境变量；跑它们之前补一条
`set -a; . ../.env; set +a` 即可，详见 [`dev/README.md`](dev/README.md)。

依赖：`pip install e2b==2.20.0 python-dotenv`（2.21 也可）。

## 脚本一览

### `acceptance/` —— 交付态，验收方跑

| 脚本 | 一句话 |
|---|---|
| `checkpoint_verify.py` | 功能正确性：三代 × 多个观测点，内存/磁盘/删除/权限位全查，并用心跳进程的 pid + 启动时间证明是内存回来了而不是虚机重启 |
| `checkpoint_bench.py` | 耗时基准：一条链上全量 + 每档增量再逐级走回，每档按 `--split` 把改动拆成内存和文件两份，抓"刚写完"与"宿主平静后"两次 |

两个脚本互不依赖，也不依赖本目录其它任何文件 —— 拷这两个 `.py` 过去就能跑。

### `probes/` —— 开发态探针与跨实现对照

| 脚本 | 一句话 |
|---|---|
| `pb2.py` | 原生快照能不能做到精确增量：`/memory/dirty` 与 `save-dirty-bitmap` 两份位图**各按各的页粒度**换算后并排比，用实际导出量做锚点 |
| `pb3.py` | 原生 pause 里先做的那次 Full 快照会不会把脏位图擦掉（预期不会，这里实测） |
| `pb4.py` | 换判据之后能降到多少：把 4KB 位图归并到 memfile 的 2MB 差分块，给出每代导出量的预测值 |
| `pb5.py` | 跨代 4KB 拼接正确性：三代改同一个 2MB 块里的不同 4KB 页，恢复每一代都逐字节对 |
| `probe_dirty.py` | 把恒定 400MB 的 memfile 拆开量，D/E 一对专门证明"只读也被算成脏" |
| `uffdwp_probe.c` | 这台 arm64 内核到底支不支持 uffd 写保护、pagemap 第 57 位会不会置上（`gcc -o uffdwp_probe uffdwp_probe.c` 后直接跑） |
| `native_snapshot_bench.py` | 三套对照之三：e2b **原生** snapshot（pause / create_snapshot）的同一张档位表 |
| `checkpoint_bench_v2.py` | 三套对照之二：**我们这套**（宿主机 Firecracker 差分 + NBD 换盘）的同一张档位表 |

`pb3.py` / `pb4.py` `from pb2 import ...`，与 `pb2.py` 同目录即可，不必设 `PYTHONPATH`。

### `dev/` —— 开发态工具箱（原 `test-950/`）

宿主自检（`01-check-host.sh` + `cap_test.c`）、造数据卷（`02-prepare-loop-volume.sh`）、
两套之间切换二进制与 env（`03-switch.sh`、`hcl_env.py`）、切换后冒烟（`04-verify-runtime.sh`）、
正确性与稳定性（`correctness.py`、`loop.py`、`pause_verify.py`、`compat_matrix.py`）、
耗时与劣化（`timing.py`、`bench-ckpt.py`、`probe-ramp.py`、`freeze_probe.py`）、
HDBSS 三级证据（`hdbss_evidence.py`）、一条龙（`run-all.sh`）。
逐脚本说明、判据速查、两套 env 怎么配，全在 [`dev/README.md`](dev/README.md)；
逐脚本的验证状态见 [`dev/MANIFEST.md`](dev/MANIFEST.md)。

这些脚本共享 `dev/lib.py`，**不要和 `acceptance/` 混用**。

## 950 与 920B 验证矩阵

一套脚本不按机器分，按**结论**分。920B 是鲲鹏 920B（无 HDBSS，脏页跟踪退回 KVM 写保护），
950 是目标交付机（有 HDBSS）。两者跑的是同一份代码，差别只在脏页跟踪的硬件后端。

**920B 验过即成立**（代码路径与 950 完全相同，硬件后端不影响结论）：

| 结论 | 脚本 |
|---|---|
| 树语义：线性 / 前滚 / 分叉 / 删除 / 失败语义 | `dev/correctness.py`、`acceptance/checkpoint_verify.py` |
| checkpoint 之后 pause/resume 不丢封层数据 | `dev/pause_verify.py` |
| 与原生生命周期操作（create/connect/pause/kill）的兼容矩阵 | `dev/compat_matrix.py` |
| 200 次连续回滚的成功率 | `dev/loop.py` |
| 原生精确增量的**正确性**（跨代 4KB 拼接）与**体积** | `probes/pb5.py`、`probes/pb4.py`、`probes/pb2.py` |
| 退路：追踪关掉能退回全量 | `dev/timing.py`（`mem_mode` 一列）、`probes/probe_dirty.py` |

**必须 950 才算数**（结论依赖 HDBSS 或要给交付方最终数字）：

| 结论 | 脚本 |
|---|---|
| HDBSS 三级取证：能力 / FC 自报 / 数据面 | `dev/hdbss_evidence.py` |
| 性能分档的最终数字 | `acceptance/checkpoint_bench.py`、`dev/bench-ckpt.py` |
| 无写保护陷出时的开销（冷/热写耗时比应接近 1，写保护下是 4~5） | `dev/hdbss_evidence.py`、`dev/probe-ramp.py` |
| 原生精确增量在 HDBSS 下复跑 | `probes/native_snapshot_bench.py`、`probes/pb2.py` |

判定"跑在哪个后端"不靠推测：`dev/lib.py` 让每个脚本开头打印这一次的
`dirty_tracking`（`hdbss` / `kvm-wp` / `off`），报告里不会出现"不知道这组数字是哪个后端跑的"。

## 报告目录命名

已归档的实测报告在 `dev/reports/`，沿用现有命名 `<机器>-<内容>-<日期>`：

```
dev/reports/950-verify-20260829/          950   · checkpoint_verify
dev/reports/920b-kas0904-20260904/        920B  · KASandbox_0904 那一轮
dev/reports/bench-ext4-20260824-200601/   （早期只标内容+时间戳的，保留原样）
```

新报告一律用 `<机器>-<内容>-<日期>`，机器写 `950` / `920b`，日期 `YYYYMMDD`，
同一天多轮再加 `-HHMMSS`。目录里放一份 `00-context.md` 说明这一轮的二进制版本、
文件系统、脏页后端和每个日志对应哪条命令。
