# 28 · 当前待处理事项与可选增强方向

使用契约中的设计边界见第 3、22 章。本章维护已知问题、待验证项和可选增强的状态、前提与证据；候选方向不表示已安排排期。

## 1. 已知问题与验证缺口

本表汇总影响使用与后续判断的主要事项。完整现象、数字及历史轮次见 [第 20 章 §7](20-acceptance.md#7-已知项与缺口)、[附录B历史长测](B-920b-results.md#10-最终版的结果)与[附录C修复案例](C-evolution-and-cases.md#8-问题逐个讲)。其中的 920B 历史结果不自动适用于 950。

| 项目 | 类别与当前状态 | 影响、处理及证据入口 |
|---|---|---|
| delete 同步合并 | 待修复问题：性能优化暂缓 | 合并延长 delete 耗时；需要时可改后台异步合并，见 [§2](#2-候选开发方向) 及 [附录 B §11](B-920b-results.md#11-已知未解决) |
| conntrack 清扫长尾 | 待修复问题：已有方案，暂不实施 | 清扫成本受整张宿主表影响，可能进入冻结关键路径；按 ct mark 过滤的方案已设计，2026-09-30 决定暂不实施，见 [附录 C §8.6](C-evolution-and-cases.md#86-conntrack-清理占了冻结窗口) |
| 视图块数与装配成本增长 | 待验证项；相关优化为可选增强 | 长跑末尾块数尚未饱和，需要更长实测确认走势；区间编码候选见 [§2](#2-候选开发方向)。随真实回滚量增加的写回成本另行判断，见 [附录 B §11](B-920b-results.md#11-已知未解决) |
| 空闲后操作变慢、首个增量的两种耗时形态 | 待修复问题：触发条件或根因待定位 | 保留现象记录，需要针对负载复现；一次短测不能关闭概率项，见 [第 20 章 §7](20-acceptance.md#7-已知项与缺口)、[第 19 章 §3](19-950-concurrency.md#3-到-950-上要重测什么) |
| netns 泄漏、客户端连接异常、偶发 GC 长暂停 | 待修复问题：分别为上游运维处置、客户端记录、未归因 | 各项影响范围与处理不同，不能归为同一个回滚缺陷，见 [附录 B §11](B-920b-results.md#11-已知未解决)、[第 26 章 §9](26-troubleshooting.md#9-网络槽位netns泄漏) |
| 950 长期运行与专项覆盖 | 待验证项 | 已有默认功能、短时并发与混合短稳态证据；小时级长跑、滚动保留、资源收敛以及重启 / 注入 / 配额等专项仍需按目标结论补测，见 [第 19 章 §3](19-950-concurrency.md#3-到-950-上要重测什么)、[第 17 章 §3](17-functional-verification.md#3-950-默认功能回归2026-10-13) |
| 950 部署身份、原始数据与 HDBSS 覆盖 | 待验证项 | 各轮身份与回传材料分别核对；默认 order 1 已有实测，其他 order 比较待补。HDBSS 与软件写保护对照已决定不做，见 [第 20 章 §7](20-acceptance.md#7-已知项与缺口) |
| 跨树回滚与 HDBSS 武装时序 | 待验证项 | 前者已有单元测试，缺端到端用例；后者由调用点位置保证，缺断言，见 [第 20 章 §7](20-acceptance.md#7-已知项与缺口) |
| 封层刷屏障与重复刷盘 | 待验证项：V1–V3 均未验证 | 先确认运行内核行为并测量耗时与读请求，再决定是否改代码；现状、源码依据和测法见 [第 28 章 §3.1](#31-待验证项) |

## 2. 候选开发方向

本表承接原[第 28 章 §3](28-status-and-directions.md#3-已知缺口与可能的方向) 的方向清单，统一维护候选项与状态。难度为原清单的粗估；实施前仍要验证收益、调用契约与失败语义。

| 方向 | 预期收益与前置条件 | 当前状态 / 难度 | 机制与证据入口 |
|---|---|---|---|
| 后台异步合并 | 减少 delete 同步等待；需要设计与 checkpoint / restore / delete 的并发及回收协调 | 可选增强；需要时再做 / 中 | [附录 C §8.10](C-evolution-and-cases.md#810-删除同步合并delete-变慢)、[第 6 章](06-memory-diff-tree.md) |
| conntrack 后台清扫再提速 | 降低宿主表规模对冻结窗口的影响；可先按生产数据评估单地址过滤阈值，进一步方案采用 ct mark 过滤 | 可选增强；ct mark 方案已设计、暂不实施 / 中 | [附录 C §8.6](C-evolution-and-cases.md#86-conntrack-清理占了冻结窗口) |
| 层侧车改为区间编码 | 减少逐块读取、解码成本；涉及 `.meta` 盘上格式变化，需处理格式兼容 | 可选增强；待评估 / 中 | [附录 B §11](B-920b-results.md#11-已知未解决)、[第 7 章](07-disk-layering.md) |
| 可导出的 checkpoint | 层栈压平并物化模板底座，提供独立导出接口；保留当前 checkpoint 默认路径 | 可选增强；待评估 / 中 | [第 12 章](12-state-and-lifecycle.md)、[第 15 章](15-native-cooperation-and-cost.md) |
| 账本跨重启加载 | 补齐按事务顺序的 `fsync`、读取、陈旧条目回收及格式迁移；恢复账本之外还需处理虚机与设备生命周期 | 可选增强；待评估 / 中 | [第 12 章](12-state-and-lifecycle.md)、[第 12 章](12-state-and-lifecycle.md) |
| 按沙箱按需武装脏页跟踪 | 将 orchestrator 统一配置细化到沙箱；需在创建时获知该沙箱是否使用 checkpoint | 可选增强；待评估 / 中 | [第 5 章](05-dirty-tracking-and-hdbss.md)、[第 23 章](23-build-and-self-check.md) |
| 层文件的全零块回收 | 当前恒等映射不剔除全零块；需区分“显式写零”与“沿用父层”，保持读取语义 | 可选增强；待评估 / 中 | [第 7 章](07-disk-layering.md) |
| 视图层列表改为引用父代 | 减少每条目完整记录全部层的记账成本；需保持历史合并与回收语义 | 可选增强；待评估 / 中 | [第 7 章](07-disk-layering.md)、[第 12 章](12-state-and-lifecycle.md) |
| 跨树回滚端到端验证、HDBSS 武装时序断言 | 补齐 [§1](#1-已知问题与验证缺口) 对应的验证缺口 | 待验证项；待补 / 低 | [第 16 章](16-test-methodology.md)、[第 5 章](05-dirty-tracking-and-hdbss.md) |
| 跟上游合并 | 持续维护分叉；同步后重新生成部署补丁并验证组件配对 | 持续维护 / 持续 | [第 27 章 §1](27-development.md#1-代码在哪)、[源码开发与出包流程 §11](../../deploy-docs/08-源码开发与出包流程.md#11-checkpoint--restore-代码的来源是-deltabox-dev再生成走脚本) |

V1–V3 的具体测量任务保留在 [第 28 章 §3.1](#31-待验证项)。实施任何候选改动前，按 [第 27 章 §3](27-development.md#3-改动自查清单) 检查不变量，按 [第 27 章 §4](27-development.md#4-怎么验证一次改动) 验证。

## 3. 已知缺口与可能的方向

候选方向及状态见[本章§2](#2-候选开发方向)。
已知问题与验证缺口的概览见 [第 28 章 §1](#1-已知问题与验证缺口)，实测细节仍在 [第 20 章 §7](20-acceptance.md#7-已知项与缺口) 与[附录 B](B-920b-results.md)、[附录 C](C-evolution-and-cases.md)。
本节保留开发者需要的待验证细节；这些测量任务尚未形成确定的优化结论。

### 3.1 待验证项

读代码时发现、还没有量过也没有动过的点。每一项写明现状、依据、要先量什么；量过之后要么改代码，要么把结论写回对应章节并从这里删掉。

| # | 项 | 现状与依据 | 先量什么 | 状态 |
|---|---|---|---|---|
| V1 | **checkpoint 封层的刷屏障去掉 `BLKFLSBUF`，只留 `fsync`** | `SealLayer` 第一步调 `sync`，先 `ioctl(BLKFLSBUF)` 再 `fsync`（`rootfs/nbd.go` 的 `sync`）。`BLKFLSBUF` 除了把缓冲的写推下去，还会丢弃宿主为这个块设备缓存的已读内容。封层前后设备内容不变，这份缓存本来仍然有效；丢掉之后客户机再读这些块，要重新经 NBD 向 orchestrator 取。restore 的视图切换内容确实变了，那里必须丢，不在本项范围 | ① 在运行内核上确认 `BLKFLSBUF` 是否丢弃读缓存；② 同一沙箱 checkpoint 前后重复读同一批文件，对比读耗时与 NBD 读请求数；③ 改成只 `fsync` 后重跑，并跑一遍正确性用例 | 未验证 |
| V2 | **`flush` 里对同一个描述符连做两次 `fsync`** | `rootfs/rootfs.go` 的 `flush` 先 `syscall.Fsync(fd)` 再 `file.Sync()`，后者也是 `fsync`，第二次面对的是已经刷空的缓存。上游原有代码，原生 pause 也走这里 | `seal` 段里这一次调用的耗时；预期是固定的系统调用开销，量级很小，不值得单独改，随 V1 一起处理 | 未验证 |
| V3 | **一次 checkpoint 对 NBD 设备刷两遍** | 先是 Firecracker 保存块设备状态前的 `prepare_save`（同步引擎下是对设备文件 `sync_all`，`devices/virtio/block/virtio/io/mod.rs` 的 `drain_and_flush`），后是 orchestrator 的 `SealLayer`。两次之间 vCPU 停着、同步引擎没有在途请求、设备只有 Firecracker 一个写入方，所以第二遍没有数据可刷，正确性上一遍就够 | 第二遍的耗时。**结论倾向保留**：Firecracker 那一遍失败只打一行日志（`device.rs` 的 `drain_and_flush`：`Failed to drain ops and flush block data`），快照照常继续；orchestrator 这一遍失败会让 checkpoint 报错返回，是唯一带错误检查的一遍，也不依赖 Firecracker 用哪种读写引擎 | 未验证 |

与这三项有关的两个事实，查代码时一并确认过，记在这里备查：

- **沙箱的根盘用同步读写引擎。** 模板构建时显式指定 `Sync`（`template/build/layer/create_sandbox.go` 的 `DefaultIoEngine`，以及默认值为 `Sync` 的开关 `build-io-engine`）；引擎类型存在快照里（`devices/virtio/block/virtio/persist.rs` 的 `file_engine_type`），从模板恢复出来的沙箱沿用它。同步引擎一次只处理一个请求，写调用返回后才取下一个，所以 Firecracker 自己不积压在途的写。
- **NBD 设备没有声明支持刷盘命令。** 接入内核时只带 `FlagHasFlags | FlagCanMulticonn`（`sandbox/nbd/path_direct.go`），dispatcher 收到 `NBD_CMD_FLUSH` 会报错断开（`sandbox/nbd/dispatch.go`）。所以对设备的 `fsync` 只表现为"把缓冲的写作为 NBD 写请求全部发下去并等应答"；dispatcher 是 `WriteAt` 成功之后才应答（`dispatch.go` 的 `cmdWrite`），内核认为写完成即数据已在写层里。"未声明该能力的设备内核不下发刷盘命令"这一点没有对着运行内核的源码核对，旁证是每次 checkpoint 都对设备 `fsync`、连接从未因此断开。
