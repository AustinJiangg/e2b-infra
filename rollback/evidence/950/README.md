# 950 阶段验证结果摘要

本页记录 950（141.61.11.24，Kunpeng 950 7592C，宿主内核 `6.6.0-159.4.3.154.oe2403sp4.aarch64`）上已回传测试的结论和验证边界，覆盖到 2026-11-18。原始日志与 JSON 只保留在被测机的 `test950/results/` 下，不提交、不推送，也不作为附件分发。

2026-10 曾在另一台 950（主机名 `slot6`，宿主内核 `6.6.0-159.4.12.166`）上测过 smoke、perf、func、HDBSS 各一轮。那台机器与本页的被测机不是同一台，其数据已由 2026-11-17 一轮取代，手册不再引用。

## 轮次摘要

| 轮次 | 汇总 | 主要结果与判读 |
|---|---|---|
| 20261106-native-tiers | PASS，108 样本，失败 0 | 原生 pause / resume 六档，每档 15 个 repeat、3 个 first；orchestrator sha256 `a5d03902…`，`hdbss`。数据见手册[第 19 章](../../docs/19-native-pause-resume.md)。原始 JSON 已回传并按样本重算，`create_snapshot()` 路径未测 |
| 20261117-161710-standard：smoke | 5 PASS / 0 FAIL / 0 SKIP | 宿主预检 19 PASS / 0 WARN，正确性 59/59，能力文件确认 HDBSS 自动启用；standard 结尾回切后的 smoke-after 同样 5 PASS |
| 20261117-161710-standard：func | 17 PASS / 0 FAIL / 5 SKIP | 16 个默认 crtest 用例全过，863 条硬断言；兼容矩阵 OK 13 / REFUSED 3 / BROKEN 0。T18、T21、T38、T37 的重启、注入、配额专项跳过；SDK 异常语义 pytest 因客户端未装 pytest 跳过 |
| 20261117-161710-standard：perf | 3 PASS / 1 FAIL / 0 SKIP | 十二档共 270 次增量迭代，混合 0～128 MiB 两项 p50、p99 全部达标，256 MiB 两项 p50 达标、restore p99 超线，512 MiB restore p50 超线；同点串行恢复 100 次 p50 10.6 ms。并发 A 段 N=16 有 1 次 restore 服务端已完成、客户端没收到应答，等满 300 s 超时，判 FAIL |
| 20261117-161710-standard：hdbss | 3 PASS / 0 FAIL / 0 SKIP | 默认 order 1 的启用和数据面证据；69 次增量迭代、MD5 不一致 0、非增量 0。未扫描其他 order、未做同机软件后端对照 |
| 20261118-111424-extended：沙箱级各步 | 9 步全部 PASS | 由 e2b-infra `1dd8525` 重新构建部署，orchestrator sha256 `e54489e4…`。11:22 宿主宕机中断，12:39 续跑。func PASS 18 / SKIP 4（860 条硬断言，SDK pytest 67 passed）；perf 与 perf-full 的并发 A/D 段不一致都是 0，串行 restore 100 次 p50 9.62 / 12.7 ms、p99 16.2 / 19.8 ms，按 p50 超线的只有 mix512（perf-full 另有 mix256 restore p50 100 ms）；hdbss order 1 与 hdbss-orders 的 order 1、9 各 63 次迭代，MD5 不一致 0、非增量 0；special PASS 14 / SKIP 1（T18、T21 四条、T37 两条通过，只剩 T38）；idle 七臂 restore p50 16.1～22.4 ms。数据见手册[第 17 章 §3.5](../../docs/17-functional-verification.md) |
| 20261118-111424-extended：进程级各步 | 3 步全部 PASS | 验收（含新加的 restore_incremental）、压测、扩展压测（第一次在 950 上跑，各组 1000 轮，两个容量组校验全部一致）。数据见进程级手册第 22 章 §10 |
| 20261118-142626-native | 两套各 PASS，各 324 样本 | 原生 pause / resume 在沙箱级与进程级两套代码上各跑一遍，含 950 第一次的 snapshot 模式。数据见手册[第 19 章 §3.5](../../docs/19-native-pause-resume.md) |
| 20261118-144513-compare | 两套各 PASS，各 460 次测量 | 两套用同一负载对比 checkpoint / restore，并记录服务端分段与 CRIU 统计；沙箱级脏页后端自报 `hdbss`。数据与分析见进程级手册第 24 章 §5.3、§5.4 |

同一个 standard 还跑了进程级的验收与压测，结果在进程级手册第 22 章。

## 身份与验证边界

具体测试条件与测量口径见手册[功能验证](../../docs/17-functional-verification.md)、[性能实测](../../docs/18-950-performance.md)、[原生 pause / resume 分档](../../docs/19-native-pause-resume.md)和[并发验证](../../docs/21-concurrency-and-longrun.md)。验证项的总览见手册[第 22 章 §9](../../docs/22-acceptance.md#9-结论边界已测跳过待验证)。

手册的讲解源码为 `KASandbox_0904/deltabox-dev@57a30632cb9cb8eb67a35dd226d9ef4b40458af4`。这是代码阅读基准，不是对所有被测二进制身份的认定。

- 2026-11-18 的三档（extended、native、compare）在重新部署之后跑，沙箱级 orchestrator sha256 是 `e54489e4…`，与 11-17 那一轮不是同一份二进制，两轮的耗时不直接相减。extended 的 perf 表、hdbss-orders 的 order 对比表、idle 的逐臂明细还没有回传。
- 2026-11-17 standard 的 orchestrator（`/usr/bin/template-manager` sha256 `a5d03902…`）与 FC（`18f3faa7…`）身份由 `test950.sh` 在同一轮记录，与 2026-11-06 原生分档是同一份 orchestrator。能力文件自报 commit `4853232`，与源码、部署包的对应仍需核实。
- perf、func、HDBSS 的原始 JSON / JSONL 没有回传，统计值来自日志文本，未独立重算；并发 A 段 N=16 的 96 个操作起止时间已回传。原生分档一轮 JSON 已回传。
- 950 小时级持续运行、滚动保留与资源收敛，及已跳过专项，需要对应验证。
- 短时并发不用于推断持续 QPS；小样本 p99 不用于承诺长期尾延迟。
- 默认 order 1 的实测不用于声称不同 order 的优劣或 HDBSS 相对软件后端的加速倍数。

本页是结果摘要，不包含原始日志、逐文件来源清单或日志下载链接，也不表示测试侧完整产物已经收齐。
