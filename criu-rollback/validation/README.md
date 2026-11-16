# 进程级验证摘要

本目录保存已复核的结果摘要、版本和数据依据，完整日志、原始 JSON、二进制及含凭据的运行配置保留在测试工作区。
当前阶段为 **920B 已验证，950 待验证**。950 沙箱级参考数据单独标明，不计为进程级验收结果。

## 920B：2026-10-16

| 文件 | 内容 |
|---|---|
| [summary.json](920b-20261016/summary.json) | 三轮六档服务端 `total_ms` 与墙钟的逐轮样本及中位数、全量基点、RSS、18 次恢复校验状态、原始 JSON 的 SHA256 |
| [provenance.json](920b-20261016/provenance.json) | 运行部件 `259f1a25c`、SDK `dcca61266`、脚本和二进制哈希、宿主与 guest 条件、950 沙箱级参考值 |
| [historical-fix-evidence.json](920b-20261016/historical-fix-evidence.json) | 旧 920B 修复前 196 / 206 ms 及后续 121、85～89 ms 的原文摘录、原文件路径和哈希 |

正文表与两项慢因说明集中维护在 [20 章](../docs/20-920b-performance.md)。
这一次完成部署自检、三轮分档性能和 11 项脚本回归；完整功能验收与压测引用 2026-10-08 / 10-09 的记录。

分档脚本六档独立建立基点并恢复，三轮共 18 次完整内存和文件校验通过，SDK/journal 一致。
0 档无额外负载，仍有解释器与通信开销；每档均为新沙箱首次恢复。

## 920B：2026-11-06

| 文件 | 内容 |
|---|---|
| [summary.json](920b-20261106/summary.json) | 三轮六档样本及中位数，由 `summarize_checkpoint_perf.py` 生成 |
| [provenance.json](920b-20261106/provenance.json) | 运行部件版本与哈希、部署版本 `504f02c`、验收与压测结论 |

在新部署上用 950 离线包的入口跑完 `acceptance`、`stress` 和回切，结论见 [20 章](../docs/20-920b-performance.md)。

## 复核与后续归档

从测试工作区取回三轮原始 JSON 后，在仓库根目录运行：

```bash
python3 criu-rollback/scripts/acceptance/summarize_checkpoint_perf.py \
  <结果目录>/perf-run1.json <结果目录>/perf-run2.json <结果目录>/perf-run3.json \
  --output /tmp/gsd-summary.json
cmp /tmp/gsd-summary.json criu-rollback/validation/920b-20261016/summary.json
```

此命令只读原始结果，不连接宿主、不起沙箱。需要使用归档中同名三份文件，并保持运行顺序，以重现摘要中的样本顺序和来源哈希。
汇总拒绝失败结果、旧口径、重复输入文件、错误负载、错误父节点、恢复内容不一致或计时缺失的记录。全量基点单列，不混入增量快照表。
原始目录位置记录在 `provenance.json`；这些位置是历史出处，不是运行脚本的依赖。无原始文件时，可检查摘要中的样本和中位数，但不能据此重新验证完整恢复内容。

950 上的进程级跑完后另建带平台与日期的摘要目录，记录源码版本、实际脚本哈希、机器条件及验收范围，不覆盖本目录，也不混合不同的测试口径。正文结果填进 [22 章](../docs/22-950-results.md)。
