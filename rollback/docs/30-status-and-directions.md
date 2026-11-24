# 30 · 现状与方向

## 本章目标

汇总当前的已知问题、验证缺口和候选开发方向，让接手的工程师知道什么已经确定有问题、什么还没有测过、下一步可以做什么。使用契约中的设计边界见 [第 3 章](03-goals-and-boundaries.md)、[第 24 章](24-api-semantics.md)。候选方向不表示已安排排期。其中的 920B 历史结果不自动适用于 950。

## 1. 已知问题

完整现象和数字见 [第 22 章 §7](22-acceptance.md#7-已知项与缺口)、[附录 B §10](B-920b-results.md#10-最终版的结果)、[附录 B §11](B-920b-results.md#11-已知未解决) 与 [附录 C §3](C-evolution-and-cases.md#3-并发与长跑暴露的问题)。

| 项目 | 当前状态 | 影响、处理及证据入口 |
|---|---|---|
| delete 同步合并 | 待修复：性能优化暂缓 | 合并延长 delete 耗时；需要时可改后台异步合并，见 [§3](#3-候选开发方向) 及 [附录 C §3.10](C-evolution-and-cases.md#310-删除同步合并delete-变慢) |
| conntrack 清扫长尾 | 待修复：已有方案，暂不实施 | 清扫成本受整张宿主表影响，可能进入冻结关键路径；按 ct mark 过滤的方案已设计，见 [附录 C §3.6](C-evolution-and-cases.md#36-conntrack-清理占了冻结窗口) |
| 空闲后操作变慢、首个增量的两种耗时形态 | 待修复：触发条件或根因待定位 | 保留现象记录，需要针对负载复现；一次短测不能关闭概率项，见 [第 22 章 §7](22-acceptance.md#7-已知项与缺口)、[第 21 章](21-concurrency-and-longrun.md) |
| restore 应答丢失、客户端偶发 `Bad file descriptor` | 已修复（SDK），950 待复验 | 950 并发 N=16 出现 1 次：服务端约 140 ms 完成 restore 并写回应答，客户端等满 300 s 超时；920B 长跑里每轮 0–2 次的 `Bad file descriptor` 是同一个原因。根因是同一进程内全部沙箱共用的 httpcore 连接池在连接总数超过 `max_keepalive_connections`（默认 20）时，会关掉刚分给另一个线程的连接。SDK 把该上限默认改为与最大连接数相同，920B 复验未再出现，见 [第 21 章 §2.3](21-concurrency-and-longrun.md#23-n16-的一次-restore-应答丢失) |
| 重启 orchestrator 后旧沙箱的 Firecracker 残留 | 已修复（2026-11-17 修复版 RPM `2026.09-3`），950 待复验 | `nomad job restart` 时 orchestrator 关网络池超过 job 的 30 s kill_timeout，被 SIGKILL；Firecracker 在自己的进程组里，不随之退出，由 init 收养，一直占着内存和网络槽位（920B 2026-11-17 复现）。deltabox 分支 2026-07 已有启动时清理遗留 Firecracker 的代码，构建 RPM 的 `0001` 补丁没有带上。`2026.09-3` 的补丁加了同样的清理（只结束父进程为 init、路径在 `/fc-versions/` 下、API socket 名为 `fc-<沙箱>-<随机>.sock` 的进程），920B 复验 T18 通过；被结束的 Firecracker 占过的 NBD 设备暂不回收 |
| netns 泄漏、偶发 GC 长暂停 | 待修复：分别为上游运维处置、未归因 | 各项影响范围与处理不同，不能归为同一个回滚缺陷，积累的 netns 还会拖慢原生 resume（每起一个 Firecracker 都要复制宿主挂载表，[第 13 章 §6.3](13-native-snapshot.md#63-测试环境对这两组数字的影响)），测性能前要先回收。见 [附录 B §11](B-920b-results.md#11-已知未解决)、[第 28 章 §9](28-troubleshooting.md#9-网络槽位netns泄漏) |

## 2. 验证缺口

### 2.1 待补的测量与覆盖

| 项目 | 当前状态 | 缺什么、证据入口 |
|---|---|---|
| 视图块数与装配成本增长 | 待验证 | 长跑末尾块数尚未饱和，需要更长实测确认走势；区间编码候选见 [§3](#3-候选开发方向)。随真实回滚量增加的写回成本另行判断，见 [附录 B §11](B-920b-results.md#11-已知未解决) |
| 950 长期运行与专项覆盖 | 待验证 | 已有默认功能、短时并发与混合短稳态证据；小时级长跑、滚动保留、资源收敛以及重启 / 注入 / 配额等专项仍需按目标结论补测，见 [第 21 章](21-concurrency-and-longrun.md)、[第 17 章](17-functional-verification.md) |
| 950 部署身份、原始数据与 HDBSS 覆盖 | 待验证 | 各轮身份与回传材料分别核对；默认 order 1 已有实测，其他 order 比较待补。HDBSS 与软件写保护对照已决定不做，见 [第 22 章 §7](22-acceptance.md#7-已知项与缺口) |
| 跨树回滚与 HDBSS 武装时序 | 待验证 | 前者已有单元测试，缺端到端用例；后者由调用点位置保证，缺断言，见 [第 22 章 §7](22-acceptance.md#7-已知项与缺口) |
| 原生 pause / resume 固定开销 | 已拆分（950，2026-11-20）；`ps` 已改 | 发 SIGTERM 前那次 `ps`（约 106 ms，只为日志）在进程级部署包 `f66dbe2c1` 改为读 `/proc`，同一负载下 pause 每档少 89～129 ms，沙箱级 RPM 未改。改后 pause 0 档 orchestrator 计约 108 ms：内核销毁 KVM 虚机约 57 ms、断开 NBD 约 20 ms，保存状态约 28 ms；resume 约 57 ms：起新 Firecracker 约 34 ms（cgroup v2 建 cgroup 约 13 ms、新建 mount 命名空间随挂载点数增长）、等 envd 约 16 ms。仍未拆：256 档起 Firecracker 多出的约 10 ms、等 envd 一段；920B 上 resume 比不含修复的对照慢 25～36 ms，原因未查。见 [第 13 章 §6](13-native-snapshot.md#6-成本模型)、[第 19 章 §6](19-native-pause-resume.md#6-固定开销从哪里来) |

### 2.2 读代码发现的待验证项

下面三项是读代码时发现的点，2026-11-24 已有结论：V1 改了代码，V2、V3 量过后不改。

| # | 项 | 现状与依据 | 先量什么 | 状态 |
|---|---|---|---|---|
| V1 | **checkpoint 封层的刷屏障去掉 `BLKFLSBUF`，只留 `fsync`** | 改前 `SealLayer` 第一步调 `sync`，先 `ioctl(BLKFLSBUF)` 再 `fsync`（`rootfs/nbd.go` 的 `sync`）。`BLKFLSBUF` 除了把缓冲的写推下去，还会丢弃宿主为这个块设备缓存的已读内容。封层前后设备内容不变，这份缓存本来仍然有效；丢掉之后客户机再读这些块，要重新经 NBD 向 orchestrator 取。restore 的视图切换内容确实变了，那里必须丢，不在本项范围 | ① 在运行内核上确认 `BLKFLSBUF` 是否丢弃读缓存；② 同一沙箱 checkpoint 前后重复读同一批文件，对比读耗时与 NBD 读请求数；③ 改成只 `fsync` 后重跑，并跑一遍正确性用例 | **已做（2026-11-24）**：`SealLayer` 只 `fsync`、保留宿主页缓存（KASandbox deltabox-dev `0ce35c8`，已进部署包；`CHECKPOINT_SEAL_DROP_CACHE=true` 恢复旧行为）。920B 上 `seal` 10～13 → 0.3 ms，针对性读回校验 0 不一致，沙箱级 standard 通过；见 [第 7 章 §2.3](07-disk-layering.md#23-为什么先暂停再做同步)、[第 18 章 §5.4](18-950-performance.md#54-写文件的代价由什么决定) |
| V2 | **`flush` 里对同一个描述符连做两次 `fsync`** | `rootfs/rootfs.go` 的 `flush` 先 `syscall.Fsync(fd)` 再 `file.Sync()`，后者也是 `fsync`，第二次面对的是已经刷空的缓存。上游原有代码，原生 pause 也走这里 | `seal` 段里这一次调用的耗时；预期是固定的系统调用开销，量级很小，不值得单独改，随 V1 一起处理 | **已量，不改**：封层拆开计时后，这次调用（`seal_fsync`）在 920B 上 0.02～0.06 ms，两次 `fsync` 的差别在这个量级以内 |
| V3 | **一次 checkpoint 对 NBD 设备刷两遍** | 先是 Firecracker 保存块设备状态前的 `prepare_save`（同步引擎下是对设备文件 `sync_all`，`devices/virtio/block/virtio/io/mod.rs` 的 `drain_and_flush`），后是 orchestrator 的 `SealLayer`。两次之间 vCPU 停着、同步引擎没有在途请求、设备只有 Firecracker 一个写入方，所以第二遍没有数据可刷，正确性上一遍就够 | 第二遍的耗时。**结论倾向保留**：Firecracker 那一遍失败只打一行日志（`device.rs` 的 `drain_and_flush`：`Failed to drain ops and flush block data`），快照照常继续；orchestrator 这一遍失败会让 checkpoint 报错返回，是唯一带错误检查的一遍，也不依赖 Firecracker 用哪种读写引擎 | **已量，保留**：第二遍即 `seal_fsync`，920B 上 0.02～0.06 ms；保留的理由同左 |

与这三项有关的两个事实：

- **沙箱的根盘用同步读写引擎。** 模板构建时显式指定 `Sync`（`template/build/layer/create_sandbox.go` 的 `DefaultIoEngine`，以及默认值为 `Sync` 的开关 `build-io-engine`）；引擎类型存在快照里（`devices/virtio/block/virtio/persist.rs` 的 `file_engine_type`），从模板恢复出来的沙箱沿用它。同步引擎一次只处理一个请求，写调用返回后才取下一个，所以 Firecracker 自己不积压在途的写。
- **NBD 设备没有声明支持刷盘命令。** 接入内核时只带 `FlagHasFlags | FlagCanMulticonn`（`sandbox/nbd/path_direct.go`），dispatcher 收到 `NBD_CMD_FLUSH` 会报错断开（`sandbox/nbd/dispatch.go`）。所以对设备的 `fsync` 只表现为"把缓冲的写作为 NBD 写请求全部发下去并等应答"；dispatcher 是 `WriteAt` 成功之后才应答（`dispatch.go` 的 `cmdWrite`），内核认为写完成即数据已在写层里。"未声明该能力的设备内核不下发刷盘命令"这一点没有对着运行内核的源码核对，旁证是每次 checkpoint 都对设备 `fsync`、连接从未因此断开。

## 3. 候选开发方向

难度是粗估；实施前仍要验证收益、调用契约与失败语义。

| 方向 | 预期收益与前置条件 | 当前状态 / 难度 | 机制与证据入口 |
|---|---|---|---|
| 后台异步合并 | 减少 delete 同步等待；需要设计与 checkpoint / restore / delete 的并发及回收协调 | 可选增强；需要时再做 / 中 | [附录 C §3.10](C-evolution-and-cases.md#310-删除同步合并delete-变慢)、[第 6 章](06-memory-diff-tree.md) |
| conntrack 后台清扫再提速 | 降低宿主表规模对冻结窗口的影响；可先按生产数据评估单地址过滤阈值，进一步方案采用 ct mark 过滤 | 可选增强；ct mark 方案已设计、暂不实施 / 中 | [附录 C §3.6](C-evolution-and-cases.md#36-conntrack-清理占了冻结窗口) |
| 层侧车改为区间编码 | 减少逐块读取、解码成本；涉及 `.meta` 盘上格式变化，需处理格式兼容 | 可选增强；待评估 / 中 | [附录 B §11](B-920b-results.md#11-已知未解决)、[第 7 章](07-disk-layering.md) |
| 可导出的 checkpoint | 层栈压平并物化模板底座，提供独立导出接口；保留当前 checkpoint 默认路径 | 可选增强；待评估 / 中 | [第 12 章](12-state-and-lifecycle.md)、[第 15 章](15-native-cooperation-and-cost.md) |
| 账本跨重启加载 | 补齐按事务顺序的 `fsync`、读取、陈旧条目回收及格式迁移；恢复账本之外还需处理虚机与设备生命周期 | 可选增强；待评估 / 中 | [第 12 章](12-state-and-lifecycle.md) |
| 按沙箱按需武装脏页跟踪 | 将 orchestrator 统一配置细化到沙箱；需在创建时获知该沙箱是否使用 checkpoint | 可选增强；待评估 / 中 | [第 5 章](05-dirty-tracking-and-hdbss.md)、[第 25 章](25-build-and-self-check.md) |
| 层文件的全零块回收 | 当前恒等映射不剔除全零块；需区分“显式写零”与“沿用父层”，保持读取语义 | 可选增强；待评估 / 中 | [第 7 章](07-disk-layering.md) |
| 视图层列表改为引用父代 | 减少每条目完整记录全部层的记账成本；需保持历史合并与回收语义 | 可选增强；待评估 / 中 | [第 7 章](07-disk-layering.md)、[第 12 章](12-state-and-lifecycle.md) |
| 跨树回滚端到端验证、HDBSS 武装时序断言 | 补齐 [§2.1](#21-待补的测量与覆盖) 对应的验证缺口 | 待验证项；待补 / 低 | [第 16 章](16-test-methodology.md)、[第 5 章](05-dirty-tracking-and-hdbss.md) |
| 跟上游合并 | 持续维护分叉；同步后重新验证组件配对 | 持续维护 / 持续 | [第 29 章 §1](29-development.md#1-代码在哪) |

实施任何候选改动前，按 [第 29 章 §3](29-development.md#3-改动自查清单) 检查不变量，按 [第 29 章 §4](29-development.md#4-怎么验证一次改动) 验证。

## 本章要点

- delete 同步合并、conntrack 清扫长尾是成本问题；空闲后变慢、首个增量的两种形态待定位；950 上的 restore 应答丢失已查实是客户端 SDK 连接池问题并修复。
- 950 的小时级长跑、滚动保留、资源收敛和重启 / 注入 / 配额专项尚未覆盖，920B 的结果不能替代。
- V1 已做（封层保留页缓存），V2、V3 已量，代价在 0.1 ms 以内，不改。
- 候选方向按需要启动，实施前验证收益、调用契约与失败语义。
