# 950 · checkpoint_verify.py 首轮（2026-08-29）

**本方案第一次在硬件标脏（HDBSS）路径上得到验证**，59 项校验全部通过。
原始终端输出见同目录 `checkpoint-verify.log`（150 行，含 59 条 `[PASS]`）。

| | |
|---|---|
| 机器 | 950（`slot6`），root |
| 时间 | 2026-08-29 15:02:42 ~ 15:02:51 起（三代 checkpoint 的 id 是纳秒时间戳，可回推） |
| 命令 | `python checkpoint_verify.py`（conda 环境 `jll-e2b`，工作目录 `benchmark/`），**全部用默认参数** |
| 参数 | 模板 `base`；每代脏内存 128 MB；每代根文件系统写入 32 MB；A/C 交替 3 轮 |
| 脏页后端 | **HDBSS（硬件标脏）**，Firecracker 1.12.1 |
| 产物落盘 | `/orchestrator/build/checkpoints`，文件系统 **ext4**（950 根盘就是 ext4，**真盘不是 loop**） |
| 沙箱 | `i8z0zcmkk2a1j1oxx9rnq`（跑完已删除） |
| checkpoint id | gA `ckpt_1787986962911970860`（full）、gB `...67164470970`、gC `...71238508730`（均 incremental） |

## 结果

| 场景 | 结果 |
|---|---|
| 三代现场互不相同 | 9 项确有区分度——先自证「这个检查会失败」 |
| 逐级回退 C → B → A | 11 项现场逐项一致 |
| 跨 2 代前滚 A → C | 11 项现场逐项一致 |
| A / C 交替 3 轮 | 每轮全量校验，无状态泄漏或累积误差 |
| **`kill -9` 心跳进程后回滚到 A** | **进程连同 PID(220) 与启动时刻(104) 一起复活** |
| 树根全量、后续增量 | `mem_mode` = `full` / `incremental` / `incremental`，没有静默退化成全量 |

客户端墙钟（含网络往返与服务端全部工作）：create 全量 0.283 s（1 次）、
create 增量 p50 0.113 s（2 次）、restore p50 0.100 s / min 0.089 / max 0.153（10 次）。

## 三条注意

1. **这不是性能基准。** `checkpoint_verify.py` 的定位是正确性验收，耗时只是顺带打印。
   分档基准要跑 `checkpoint_bench.py`，950 上还没跑过。
2. **不能和 920B 的数字相减。** 两边的模板大小、存储介质（真盘 vs 调优 loop 卷）、
   脏页后端（HDBSS vs 软件写保护）三项全都不同，差值不能归因于 HDBSS。
3. **这份记录是从终端手工抄回来的。** 两个验收脚本只打屏、自己不落盘，
   下次上机用 shell 接一份，别再靠 scrollback：
   `python checkpoint_verify.py 2>&1 | tee reports/<机器>-verify-<日期>.log`。
