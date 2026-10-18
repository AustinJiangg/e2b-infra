# 19 · 测试体系与调试内核

## 本章目标

读完本章，你应该能：

1. 说出四层测试（单测、功能验证、性能、压测与探针）各自回答什么问题，哪一类缺陷只有哪一层抓得到；
2. 对每个功能验证脚本说清它测什么、判据是什么、怎么读它的输出，以及它**不**证明什么；
3. 读懂压测的判据：dmesg 与串口关键词、worker 允许的 errno、心跳、看门狗，以及调试内核上多出来的 `debug_locks`；
4. 按一张表找到针对某个具体问题的探针脚本；
5. 知道调试内核开了什么、为什么必须关掉 `VMGENID`、它能代替什么和不能代替什么。

上一章（[18](18-process-vs-machine-level.md)）把进程级与整机级两套回滚的分工讲完，第四部分到此结束。前四部分讲的是 gsd 怎么工作、出了问题怎么收场、时间花在哪里；
第五部分回答另一个问题：凭什么相信它是对的。本章讲"用什么测、怎么判"，下一章 [20](20-results-and-gaps.md) 给出按这套办法在新 920B 上测到的结果和还没有测到的部分，
上机逐步执行的操作在 [26](26-acceptance-runbook.md)。

本章不放实测数字。脚本里写死的阈值（例如"根盘增量不超过 2 MB""差值在 ±3 ms 内"）属于判据，会在这里出现；测出来是多少，一律见 [20](20-results-and-gaps.md)。

---

## 1. 四层测试各回答什么

gsd 的正确性分散在四个地方：gsd 自己的编排逻辑（Go）、CRIU、guest 内核里的 overlayfs 热切层补丁、以及建模板时把这些东西装进 guest 的注入逻辑。
没有一种测试能同时覆盖这四处，所以测试分成四层，每一层只对自己能看见的那部分负责。

| 层 | 是什么 | 在哪跑 | 能抓到什么 | 抓不到什么 |
|---|---|---|---|---|
| 单测 | Go 与 Python 的单元测试 | 任何有 Go 工具链的机器，不需要 guest | 编排顺序、失败路径的返回码与清理、状态文件的读写、增量父子关系、SDK 的错误映射与 URL 选择 | 真实 CRIU 的行为、内核补丁、模板注入的实际效果 |
| 功能验证 | 四个 `verify_*` 脚本 | gsd 栈运行中的宿主机，起真实沙箱 | 端到端行为：进程是否真的回来、文件是否真的回滚、错误码与 SDK 异常、增量是否漏页 | 并发文件活动下的内核竞态（每项只跑几秒到几十秒） |
| 性能 | `demo_checkpoint_perf`、`exp_gsd_swap` | 同上 | 各分段耗时、SDK 返回的分段与 gsd 日志是否一致、两个版本之间的差 | 未覆盖的程序或内核行为；v2 性能脚本虽校验完整负载，也不能替代功能验收 |
| 压测与探针 | 四个 `stress_*` 脚本和十几个 `probe_*` 脚本 | 同上；压测另在调试内核上跑一遍 | 切换与并发文件操作的竞态、释放后使用、死锁、泄漏；探针各自复现一个具体问题 | 功能语义的全面覆盖（压测只盯几条不变量） |

为什么不能只靠单测：gsd 的单测用假的 `/proc` 和假的 criu（§2），它证明的是"gsd 以为自己做了什么"。而本项目里最严重的几个问题都不在 gsd 的 Go 代码里——
切换时并发文件操作导致的内核崩溃、被删层不释放、restore 后删下层文件触发的 `BUG`，全部在内核补丁里（[07](07-overlay-concurrency-and-fixes.md)），单测一条都看不见。

为什么不能只靠功能验证：功能验证每一项都是"做一件事、看结果"，沙箱里除了被测负载之外是安静的。内核侧的竞态需要切换发生在别的进程正做文件操作的那几微秒里，
这要靠压测里那几个不归 gsd 管的 root worker 连续几百轮地去撞（§5）。

### 1.1 先记住一件事：多数脚本的退出码不是判定

这些脚本是在排查过程中逐步长出来的，判定方式并不统一。读脚本逻辑核对的结果如下，**上机时不要只看返回码**：

| 脚本 | 退出码的含义 | 判定看哪里 |
|---|---|---|
| `verify_gsd.py` | 只有脚本抛异常才非零 | 末行 `判定: 根盘恒定 ✓`，以及开头打印的四行环境信息（人工核对） |
| `verify_fixes.py`、`verify_multitree.py` | 有任何一项 FAIL 则为 1 | 末尾汇总表与 `合计 N 项，FAIL n` |
| `verify_clear_refs_race.py` | 只有 restore 失败等硬错误才非零 | `有漏页的轮数: 0 / N`，以及每轮的 `restored_state=T`、`sha_eq=True` |
| `demo_checkpoint_perf.py` | v2 校验失败或异常返回非零，通过返回 0 | 6 行 restore 全部 `OK`，SDK/journal 一致，末行 `判定: PASS` |
| `stress_overlay_ckpt.py` | 只有脚本抛异常才非零 | 日志末尾的 `判定:` 行与 `stress-summary.json`，按 §5.4 的规则判 |
| `stress_multitree.py`、`stress_delete_dense.py` | 判定为 PASS 时 0，否则 1 | `结果 PASS` 行与 `*-summary.json` 的 `verdict` |
| `run_stress_watch.sh` | 透传被看护脚本的退出码；判定卡死时为 2 | 输出目录里的 `RESULT` 文件：`DONE rc=0 …` 或 `WEDGED …` |

---

## 2. 单测

### 2.1 gsd：假的 `/proc` 与假的 criu

gsd 的编排逻辑要读 `/proc`、给进程发信号、跑 criu、查文件系统剩余空间。这些动作如果直接写在编排代码里，单测就必须有真实进程和 root 权限，失败路径（criu 超时、某棵树 dump 失败、状态文件写不进去）更是造不出来。
所以 gsd 把对外部世界的全部依赖收拢到一个结构体 `sysOps`（`ckpt/sysops.go:17`）里：`/proc` 的位置、查 systemd unit 的 MainPID、发信号、等进程退出、执行 criu dump 与 restore、查剩余空间，各是一个字段。
生产代码用 `defaultSysOps`（`ckpt/sysops.go:38`）填上真实实现，单测换成 `fakeSys`（`ckpt/fake_test.go:38`）。

`fakeSys` 在内存里维护一份进程表，同时把它按内核的真实格式写成临时目录下的 `<pid>/stat` 与 `<pid>/status`，被测代码照常去解析这些文件；
信号只改这份表，不碰真实进程；假的 criu dump 默认模拟"把整棵树停住并写出 `inventory.img`"，也可以换成一个返回错误或一直不返回的函数。
热切层的 ioctl 另有一个注入点：`OverlayManager` 的 `switchFn` 字段（`ckpt/overlay.go:62`），单测用 `newTestOverlay`（`ckpt/fake_test.go:391`）在临时目录里搭一个 overlay 管理器并换上假的切换函数。
`newTestService`（`ckpt/fake_test.go:364`）把假系统和这个 overlay 管理器拼成一个可调用的服务。

这样单测就能直接断言编排层面的性质，例如：

- 两棵树时 create 逐棵 dump、restore 逐棵恢复，增量链按树各算各的（`TestCreateRestoreTwoTrees`、`TestIncrementalChainIsPerTree`，`ckpt/multitree_test.go:32`、`:122`）；
- 第二棵树 dump 失败时前面的树被放开、镜像目录被清掉、下一次强制全量（`TestCreateSecondTreeDumpFailure`，`ckpt/multitree_test.go:192`；`TestCreateCheckpointDumpFailureForcesFullNext`，`ckpt/service_failure_test.go:60`）；
- restore 的检查在杀进程之前失败时什么都没动，杀进程之后失败则是 `DataLoss`（`TestRestorePrecheckFailsBeforeKill`、`TestRestoreOverlayFailureAfterKillIsDataLoss`，`ckpt/restore_test.go:126`、`:195`）；
- 客户端断开不会半途取消 criu，并发的 create 与 restore 被串行化（`TestCreateCheckpointIgnoresRequestCancellation`、`TestConcurrentCreateRestoreSerialized`，`ckpt/service_failure_test.go:194`、`:215`）；
- 状态文件的往返、损坏文件导致 degraded、写失败后恢复、版本 1 文件的加载（`ckpt/state_test.go`；`TestLoadLegacyStateFile`，`ckpt/multitree_test.go:666`）。

gsd 一共四个包带单测：主包、`internal/api`、`internal/permissions`、`internal/services/checkpoint`。统一用竞态检测跑：

```bash
cd packages/gsd
go vet ./... && go test -race -count=1 ./...
```

这一层的边界要记清楚：假的 criu 永远"成功地"写出一个 `inventory.img`，它不知道真实 CRIU 会因为 external socket 拒绝 dump，
也不知道只有一端在树里的匿名管道会被静默断开（[08](08-process-trees.md)）；overlay 管理器在单测里照常建目录、改名，但层切换走的是假函数，不发真实的 ioctl。
凡是结论依赖 CRIU 或内核实际行为的，单测通过不构成证据。[15](15-known-limits.md) 里"未测的分支"有几条正是这种情况：单测有，现场没有走过。

### 2.2 orchestrator 与 SDK

orchestrator 一侧只有建模板路径与 gsd 有关，对应两处单测：

- 开关的解析：不设 `HOST_GSD_PATH` 时默认注入、设成 `off` 时关闭（`TestParse` 的两个子用例，`packages/orchestrator/internal/cfg/model_test.go:10`）；
- 往 rootfs 里加文件：开时带上 gsd 二进制、`gsd.service`、`overlay-init`，关时与上游一致，二进制读不到时报错
  （`TestAdditionalOCILayersWithGSD`、`TestAdditionalOCILayersWithoutGSD`、`TestAdditionalOCILayersGSDBinaryMissing`，`packages/orchestrator/internal/template/build/core/rootfs/gsd_test.go:88`、`:70`、`:128`）。

```bash
cd packages/orchestrator
go test -count=1 ./internal/template/... ./internal/cfg/...
```

Python SDK 的三个测试文件都打桩服务端、不建沙箱：`py-sdk/tests/test_gsd_checkpoint.py`（返回值各字段、三种 URL 选择、`is_running`）、
`test_gsd_errors.py`（Connect 错误码到异常类的映射，同步与异步各一遍）、`test_gsd_headers.py`（secure 沙箱自动带 `X-Access-Token`、不再发 Basic 认证头）。

---

## 3. 功能验证

四个脚本都在宿主机上跑、起真实沙箱。前置条件见 §8。脚本路径按入库后的位置写（`e2b-infra/gsd/scripts/acceptance/`）。

### 3.1 `verify_gsd.py`：部署自检加一条最短的性能线

**测什么。** 两件事。第一，这个模板里装的是不是预期的那套东西：脚本开头打印 guest 内 `gsd -commit`、内核版本、`/sys/kernel/rcu_expedited`、THP 设置四行。
第二，连续 restore 会不会吃根盘：这是交接时最显眼的故障——每次 restore 都把被 exec 的二进制拷进 upper，几十次之后根盘写满、沙箱失联（[16](16-restore-slowness-case.md)）。

**怎么做。** 起一个沙箱，后台跑一个预分配 192 MiB 并逐页碰实的常驻进程；建第一个 checkpoint，让进程把 192 MiB 全部改写一遍，再建第二个 checkpoint；
然后对第二个 checkpoint 连续 restore N 次（默认 8），每次记下 `df -m /` 的已用量和响应里的各分段耗时。gsd 是在 guest 里用 curl 直连 `127.0.0.1:49984` 调用的，带沙箱自己的 access token，不经过宿主代理。

**判据。** 脚本自己只判一条：结束时根盘已用量比起点多出不超过 2 MB，末行打印 `判定: 根盘恒定 ✓`，否则打印 `根盘仍在增长 ✗（内核未换或模板未重建）`。
开头的四行环境信息脚本不判，要人看：提交号应是部署的那个版本，`rcu_expedited` 应为 `1`，THP 应为 `[never]`。

**用法与输出。**

```bash
python scripts/acceptance/verify_gsd.py base-gsd 8
```

每次 restore 一行（是否成功、`total_ms`、根盘已用量与相对起点的差、各分段），之后是 `total_ms` 与各分段的 p50 / min / max，最后是判定行。
这里的 p50 取排序后下标为 `N // 2` 的那个值，N 为偶数时是偏大的那个中位数；N=8 时即第 5 小的值。

**局限。** 它只有一种负载（单棵树、192 MiB 全脏），不校验 restore 回来的内容对不对；`ok` 一栏只是看响应里有没有 `true`。
它适合回答"这台机器这个模板能不能用、大概多快"，不能回答"功能是否正确"。它的 p50 与其他脚本的数字口径不同，不能互相比较（[20](20-results-and-gaps.md)）。

### 3.2 `verify_fixes.py`：逐条覆盖问题清单

**测什么。** 交接后审读出的问题清单（[14](14-issue-resolution.md)）里，凡是需要在真实 guest 上才能确认的修复，各有一项。一共 28 项，每项独立起沙箱、用完即销毁，互不影响。

| 分组 | 项 | 一句话判据 |
|---|---|---|
| 进程发现与杀进程 | G-01、G-02、G-03、G-04、G-27、G-26 | 带子进程的树 restore 两次后进程集合与 checkpoint 时相同、之后起的进程都不在；两个后台命令保存两棵树并都恢复；首个进程结束另起一个后仍能 create；comm 带空格和括号的进程 create 后不停在 T；事先被 `kill -STOP` 的进程 create 后仍是 T、其余照常；恢复出的根进程父进程是 1 |
| 前提条件 | G-06、G-18 | 开机即 THP `[never]` 且 dmesg 有 overlay-init 的记录，256 MiB 进程零散写 96 个字节后增量镜像小于 16 MiB；guest 里的 criu 是 4.2.1 且没有发行版的 criu 包 |
| 历史与回收 | G-10、G-11、G-13、G-08 | 删掉有子节点的 checkpoint 后它只是隐藏、restore 它返回 `not_found`，子节点删掉后两者的镜像目录都消失；40 轮"create → restore 基点 → delete"后层目录不堆积、根盘增量扣除 `/var/log` 后小于 4096 KB；20 次 restore 后没有临时目录堆积、`.trash` 为空、dmesg 无调试输出；连续 create 直到返回 `resource_exhausted`（层栈太深），之后 restore 最深与最浅的 checkpoint 都正确、还能再 create |
| 文件内容 | G-25、FD | checkpoint 的 ioctl 选项里不含刚沉下去的那层，且"写 → checkpoint → 追加 → checkpoint → 各自 restore"内容逐次正确；checkpoint 前打开的 fd 在 checkpoint 后写入的内容 restore 后消失（uid 1000 进程、root 进程、按路径重新打开三种各一个文件） |
| 失败语义与生命周期 | G-14、G-15、G-16、G-22、G-09 | 缺 `inventory.img` 时 restore 返回 `failed_precondition` 且不杀进程；删掉页镜像后 restore 返回 `data_loss`、SDK 抛 `CheckpointRestoreException`、错误带日志尾部、`/health` 为 degraded、create 被拒，成功 restore 一次后解除；没有用户进程时的 checkpoint restore 后把后来起的进程杀光；重启 gsd 后 checkpoint 仍在且可用，写坏 `state.json` 再重启则 degraded；envd 停着时 create 照样 dump 用户进程；把 dump 时限设成 40 ms 触发超时，返回 `deadline_exceeded`、失败目录被清掉、进程不留在 T、下一次 create 是全量 |
| 鉴权与容量 | G-20、G-21 | secure 沙箱缺 token 或错 token 得 401、拿别的沙箱的 token 也是 401、`/health` 免鉴权、非 secure 沙箱放行；`/tmp` 下的文件随 restore 回滚，480 MiB 进程连续 create 到返回 `resource_exhausted`（镜像放不下）而不是被 OOM 杀掉，`image_bytes` 与 `du` 相差不超过 5% |
| 接口与布局 | G-29、G-33、UMASK、G-32、PERF | create 的返回值带齐各字段、4 个并发 create 都成功且互不相同；`/` 是 755 root:root 且 create、restore 之后不变，层目录与 work 目录的权限符合预期；带 unix socket 和已删除打开文件的进程 restore 后功能正常；模板里没有旧的 overlay-setup，gsd 正常；create 与 restore 的 `find_pids_ms` 中位数小于 1 ms |
| 恒为 SKIP | G-19、G-36 | 见下 |

**两项恒为 SKIP 的原因。** G-19 是"`HOST_GSD_PATH` 关闭时建出普通模板"。这个开关在 orchestrator 启动时读一次，要现场看到关闭态，必须改 orchestrator 的启动环境并重启它，会影响机器上的其他使用者，脚本里直接记为 SKIP；
它的依据是 §2.2 的单测和读代码（[20](20-results-and-gaps.md) 的缺口表有这一条）。G-36 是"SDK 带 `sandbox_url` 覆盖时 checkpoint 请求发往哪里"，由 SDK 的三个 URL 选择单测覆盖，现场没有另做。
所以这个脚本"全部通过"的样子是 **PASS 26 / FAIL 0 / SKIP 2**；SKIP 不是 2 或者出现 INFO（G-08 在上限次数内没有碰到层栈上限时会记 INFO）都要看原因。

**用法与输出。**

```bash
python scripts/acceptance/verify_fixes.py --template base-gsd --out-json verify_fixes.json
python scripts/acceptance/verify_fixes.py --template base-gsd --only G-14,G-16    # 只跑几项
```

每项的每条断言一行，`ok` 或 `BAD` 开头；末尾是汇总表和 `合计 28 项，FAIL n: [...]`。`--out-json` 里每项带全部证据行，`--out-md` 生成同内容的 Markdown。

**局限。** 三点。第一，每项结束时脚本会按关键词扫一遍该沙箱的 dmesg，命中的行只作为证据记下来，**不改变该项的 PASS/FAIL**；看结果时要在日志里搜一下 `可疑行`。
第二，G-11 的根盘判据扣掉了 `/var/log` 的增长：gsd 把 criu 的日志打进 journal，journal 与层目录在同一个 ext4 上，不扣的话它会盖过真正要看的层回收（[15](15-known-limits.md)）。
第三，G-22 靠定时器停掉又拉起 envd，单这一项就要一分钟左右，期间沙箱对 SDK 不可达是预期的。

### 3.3 `verify_multitree.py`：多棵树、`/tmp` 与根目录权限

**测什么。** 多棵用户进程树的支持（[08](08-process-trees.md)）以及同期两项行为变化：`/tmp` 留在 overlay 上随 restore 回滚、`/` 从开机起就是 755（[05](05-guest-boot-and-injection.md)）。共 13 项。

| 项 | 测什么 | 判据要点 |
|---|---|---|
| V0 | 开机日志 | dmesg 里 overlay-init 的两行记录在：`/` 是 755 root:root，`/tmp` 在 overlay 上且 `tmp.mount` 被 mask |
| V1 | 两个、三个后台命令 | `process_trees` 等于命令数，`<id>_mem/` 下每棵树一个 `t<i>` 目录且都有 `inventory.img`；restore 后各计数文件回到 create 时刻附近、随后继续增长；根 pid 不变 |
| V2 | 进程状态 | 三棵树各三个进程，事先 `kill -STOP` 其中一个；create 后、restore 后、第二次 restore 后都只有它是 T，三个计数器都在走（确认进程真的在跑，而不只是状态字母对） |
| V3 | 一棵树退出后再 create | 第二个 checkpoint 只有两棵树、父节点是第一个、两棵树都走增量；分别 restore 两个 checkpoint，树数各自正确 |
| V4 | 新起一棵树后再 create | 原有两棵树的镜像目录有指向上一个 checkpoint 的 `parent` 链接，新树没有；`state.json` 里的 `parent_id` 与之一致 |
| V5 | 多树下的增量大小 | 两棵 64 MiB 的树全量在 128～160 MiB；不改内存再建一个，大小不到前者的 10%；再起第三棵后的那一个在 64～84 MiB（两棵增量加一棵全量） |
| V6 | 树间共享管道的预检 | 两棵树共用一条管道时 create 抛 `SandboxException`，信息含 `failed_precondition`、`share pipe pipe:[`、两个根 pid、`nothing was changed`，没有新目录、进程照常跑、管道仍通；对照组（三棵互不相干的树）不被拒；两棵树用 unix socket 相连时 create 返回 `internal` 并带 criu 日志尾部，失败后进程被放开、连接仍通 |
| V7 | restore 中途失败 | 写坏第二棵树的 `pstree.img`：抛 `CheckpointRestoreException`，信息写明第一棵已恢复并在跑、第二棵失败；`/health` 为 degraded、create 被拒；restore 另一个 checkpoint 后解除 |
| V8 | `/tmp` 回滚 | 改写、新建、删除 `/tmp` 下的文件后 restore：内容回来、新建的消失、被删的回来并继续被写；`/tmp` 是 1777 root:root 且在 overlay 上 |
| V9 | `/tmp` 的挂载 | `/tmp` 上没有单独的挂载，`tmp.mount` 为 masked，没有与 tmp 有关的失败 unit，新沙箱的 `/tmp` 里没有建模板留下的残留 |
| V10 | 根目录权限 | `/` 为 755 root:root，两次 create、两次 restore 之后不变；uid 1000 不能在 `/` 下建文件；envd、gsd 在运行，SDK 的命令与文件接口正常 |
| V11 | gsd 重启 | 重启后 `state.json` 版本为 2，list 结果不变，重启后的第一次 create 仍是增量，两个 checkpoint 都能 restore |
| V12 | 性能 | 见下 |

V6 里还夹着一段不判成败、只留证据的实验：手工对"只有管道一端在树里"的两棵树分别做 criu dump 再分别 restore，记录两边都成功、但恢复后管道不再是同一条。这是预检存在的理由（[08](08-process-trees.md)），每次跑都重新确认一遍。

**V12 怎么比。** V12 有两部分。第一部分是与另一个模板的逐轮交错对照：`--old-template` 指向用另一版 gsd 或另一版内核建的模板，
每轮给两个模板各起一个新沙箱，跑同一个负载（一棵 192 MiB 的树，全量 → 全部改写 → 再建一个 → 对它 restore 若干次并去掉第一次 → 连续 create 若干次），奇偶轮交换先后顺序；
最后比较两边八个分段的中位数，每条差值的绝对值不超过 3 ms 才算过。交错与"每轮新沙箱"是有来历的：模板里固化了建模板时的内核与 gsd，早先一次不换模板、只换宿主文件的对照因此整个作废（[17](17-performance-after-fixes.md)）。
不给 `--old-template` 时这一部分只记录数值、不判定。第二部分固定跑一、二、三棵树（每棵 64 MiB）各自的分段，只记录，用来看耗时随树数怎么涨；唯一的判据是每个沙箱结束时树的数量是对的。

**用法与输出。**

```bash
python scripts/acceptance/verify_multitree.py --template base-gsd --skip V12 --out-json verify_multitree.json
python scripts/acceptance/verify_multitree.py --template base-gsd --old-template <对照模板> --perf-rounds 8 --only V12
```

输出格式与 `verify_fixes.py` 相同（它直接复用了后者的沙箱封装，两个文件要放在同一个目录下），汇总表里会把 `BAD` 行重印一遍。
末尾多一行"慢调用"：凡是客户端墙钟比 gsd 自报的 `total_ms` 多出 1.5 秒以上的 create 或 restore 都会记下，并附当时 gsd 日志的尾部。
这一行**不影响判定**，但值得看——它对应 [15](15-known-limits.md) 里"新沙箱上偶发的长等待"那一类未查清的现象。

**局限。** V12 的 ±3 ms 是针对"两边应当相同"的对照定的，它不是性能达标线；对照模板选错了（例如两个模板内核不同却以为相同），这个判据就没有意义。
另外和 `verify_fixes.py` 一样，dmesg 的可疑行只记录、不判定。

### 3.4 `verify_clear_refs_race.py`：增量会不会漏页

**测什么。** 一个只在增量 dump 下才会出现、而且普通测试完全看不见的问题。修改前 gsd 让 CRIU dump 完立刻放行进程，自己再去切 overlay、写 `clear_refs` 清 soft-dirty 位；
这段窗口里进程写过的页被清掉了脏标记，下一次增量 dump 不会带上它们，于是 restore 之后这些页是上一代的旧内容。进程没有崩溃、文件都对，只是内存里有几页是错的。
现在的做法是 dump 完让整棵树保持停住，切完层、清完位再放行（[09](09-checkpoint.md)）；这个脚本验证的就是这条窗口确实关上了。

**怎么做。** guest 里跑一个写入进程：一块私有匿名映射，共 N 页（默认 4096），按全局递增的序号 s 把 s 写进第 `s mod N` 页的开头，以固定速率（默认每秒 10 万页）绕着这块缓冲区一圈圈写。每一轮：

1. 写入进程在跑的时候建一个 checkpoint（记作 A）——要检验的窗口就在这次 dump 结束之后；
2. 同一条 shell 命令里紧接着 `kill -STOP` 写入进程，间隔只有几毫秒；
3. 从 `/proc/<pid>/mem` 把整块缓冲区读出来，记下每一页的值，这是**期望内容**，存到不随 overlay 回滚的 `/dev/shm`；
4. 再建一个 checkpoint（记作 B）。它是以 A 为父的增量，而写入进程此刻停着，所以 B 应当精确等于第 3 步读到的内容；
5. restore 到 B。CRIU 把 dump 时处于停止状态的任务恢复成停止状态，所以恢复出来的写入进程一条指令都没执行；
6. 再读一遍 `/proc/<pid>/mem`，逐页与期望内容比对。不相等的页就是漏页。

之后 `kill -CONT`，进入下一轮。

三处设计各有原因：

- **期望值取自 `/proc/<pid>/mem`，而不是让进程自己报告。** 进程自己的状态（计数器、时间基准）也在被测的内存里，漏页可能恰好漏在那里；从外面读出来的才是独立的参照。
  脚本另外按"环形写"的规律独立再算一遍（已知最大序号，每页应有的值可以直接推出来），两种算法互为校验。
- **必须限速。** 如果全速写，窗口里写过的页在下一次 dump 之前会被环形写入再覆盖很多遍、重新变脏，漏掉的页就被掩盖了。限速保证窗口里写的页到下一次 dump 之前不会被再写一遍；
  这要求缓冲区的页数大于"速率 ×（窗口 + 两次 checkpoint 的间隔）"。
- **恢复后必须是停止状态。** 脚本核对恢复后进程的状态字母为 T；否则进程已经继续写了，比对就失去意义。

**判据。** `有漏页的轮数: 0 / N`，且每一轮 `restored_state=T`、`sha_eq=True`（整块缓冲区的哈希在停住时与恢复后相同）。

**用法与输出。**

```bash
python scripts/acceptance/verify_clear_refs_race.py --template base-gsd --rounds 30 --pages 4096 --rate 100000 --jsonl race.jsonl
```

每轮一行（漏页数、按环形规律算的漏页数、最大序号、恢复后的状态、哈希是否相等、checkpoint A 的 dump / sink / 停住时长、A 与 B 的页镜像大小），末尾是汇总。
`--gsd-binary` 可以把 guest 里的 gsd 临时换成另一个二进制，用修改前的版本做反向对照——确认这个脚本在有问题的版本上确实会报出漏页，是它的判据可信的前提。2026-09-23 做过一次这样的反向对照（修改前的二进制 50 轮里 31 轮有漏页，修改后 30 轮为 0），数字与出处在 [20](20-results-and-gaps.md) 的"`verify_clear_refs_race.py`"一节。

**局限。** 它只测单棵树。它不删旧 checkpoint（`--keep-rounds` 默认 0），轮数很多时要留意 guest 里快照存储的空间，脚本结尾会打印 `df` 与 `free`。
脚本开头会把 guest 的 journald 改成易失存储并取消限流、重启一次 gsd（§8 说明原因），所以它验证的沙箱与刚从模板恢复的沙箱在日志配置上略有不同。

---

## 4. 性能脚本

### 4.1 `demo_checkpoint_perf.py`

自 2026-10-16 起采用独立分档的 v2 脚本。每档新建一个沙箱，只分配本档负载；六档内存分别为 0 / 12 / 24 / 48 / 96 / 192 MiB，文件为 0 / 4 / 8 / 16 / 32 / 64 MiB。

每档按固定顺序执行：初始化本档内存和文件 → 建全量基点 → 改写全部本档负载 → 测增量 checkpoint → 测 restore 本档基点 → 校验完整内存 SHA256、标记和文件内容 → 删除沙箱。
全量基点单列；负载构造、RSS 采集、内容校验与 journal 重试均在计时窗口之外。持续复用小块写入缓冲，避免新 VMA 让增量退化为全量（[02](02-criu-and-incremental-memory.md)）。

```bash
TEMPLATE=base-gsd THP=never OUT_JSON=perf-run1.json python scripts/acceptance/demo_checkpoint_perf.py
```

JSON 为 `schema_version=2`、`measurement_mode=per-tier-size`，恢复路径为 `modified-child-to-full-baseline`。
每次每档一个样本，建议连跑三次；用 `summarize_checkpoint_perf.py` 校验三轮并取中位数，命令见 [26](26-acceptance-runbook.md)。
脚本失败返回非零并保留已完成的部分 JSON；成功末行为 `判定: PASS`，6 次恢复完整内容正确，SDK 与 journal 对应事件的分段一致。
journal 按 checkpoint ID 和 create / restore 完成事件匹配，在计时之外有界重试，字段缺失也判失败。

0 档只有额外负载为零，仍包含解释器、通信和系统活动，不能理解为无需恢复。
本测法同时改变进程大小与改动量，用于负载分档，不能隔离固定进程大小下的改动量效应。
旧脚本所有档位预分配 192 MiB，并在同一个沙箱连续建链、逐个恢复；历史数值保留在 [20](20-results-and-gaps.md) §3.3，新数据见 §3.4，不能混合统计。

### 4.2 `exp_gsd_swap.py`

定位性能差异用的实验工具，不属于验收。它在同一个模板上把 guest 里的 gsd 换成指定的二进制（可附带环境变量），每个"变体"每轮各起一个新沙箱、跑与 `verify_gsd.py` 同款的负载，变体之间按轮交错以抵消漂移。
它能回答"两版 gsd 在同一个内核上差多少"；因为内核固化在模板里，它**不能**用来比较内核，比较内核要给每个内核各建一个模板。来龙去脉见 [17](17-performance-after-fixes.md)。入库后在 `scripts/probes/` 下。

---

## 5. 压测

压测的目标与功能验证不同：它不关心每个功能点，只在几百轮随机的 create / restore / delete 里盯住少数几条必须始终成立的不变量，同时让切换反复发生在"别人正在做文件操作"的时刻。
四个脚本入库后在 `scripts/stress/`。

读结果时要知道一点：最终提交的确认验证只重跑了下面的"全部操作"与"无 worker"两组、多棵树、删文件密集这四组压测，加上 `probe_whiteout_bug.py` 和功能回归；其余压测组、其余探针和调试内核上的结果来自它的前一轮（同一个内核、同一份 Go 代码，只差提交号）。逐项归属以 [20](20-results-and-gaps.md) 为准，[07](07-overlay-concurrency-and-fixes.md) 的"验证汇总与出处"一节是同一个说法。

### 5.1 `stress_overlay_ckpt.py`：切换与并发文件操作

这是发现并验证内核侧并发问题的主力（[07](07-overlay-concurrency-and-fixes.md) 的"切换与所有 overlay 入口的互斥"一节）。它有两种模式。

**stress 模式。** 沙箱里有两类负载：

- 若干个 **root worker**（默认 6 个）。它们用 `systemd-run` 起，既不在 envd 的 cgroup 里，也不是 uid 1000，所以 gsd 不 dump 它们、restore 时也不杀它们——它们代表的就是真实沙箱里那些不在回滚范围内的进程。
  每个 worker 把当前目录固定在一个八层深的目录里，然后不停地从操作集合里随机挑一个做，按"操作:errno"计数，每秒把统计写到 `/dev/shm`（不随 overlay 回滚）；
- 一个 **uid 1000 的用户负载**，每轮都被 criu dump 与 restore，持续改内存、追加写文件、更新心跳。

worker 的操作集合由 `--ops` 指定，一共十种：

| 操作 | 做什么 | 属于 |
|---|---|---|
| `stat`、`read` | 对分布在不同层里的文件 stat、打开并读 | 读 |
| `lsroot`、`lsdeep`、`scandir` | 列根目录、列深层目录、扫描目录并逐项 stat | 读 |
| `relcat`、`rells` | 从固定的当前目录出发按相对路径读文件、列目录 | 读，经由可能已陈旧的 cwd |
| `reltouch` | 从当前目录出发按相对路径追加写 | 写，经由可能已陈旧的 cwd |
| `append` | 对早期层里的文件追加写，触发 copy-up | 写 |
| `rename` | 把一个非空目录在两个名字之间来回改名 | 写 |

排查时把集合缩小，可以看出是哪类操作在触发问题。历次验证固定用下面七种分组，再加上 enospc 模式，合称"八组"：

| 组名 | `--workers` 与 `--ops` |
|---|---|
| all | 6 个 worker，十种操作全开（即 `--ops` 的默认值） |
| readonly | 6 个 worker，`stat,read,lsroot,lsdeep,scandir,relcat,rells` |
| wr3 | 6 个 worker，`append,rename,reltouch` |
| rename | 6 个 worker，只 `rename` |
| append | 6 个 worker，只 `append` |
| reltouch | 6 个 worker，只 `reltouch` |
| noworkers | `--workers 0`，没有并发文件操作，作对照 |

这些组名不是脚本的参数，只是约定的叫法；脚本只认 `--workers` 和 `--ops`。

主循环先建四个"中间层"checkpoint，让 worker 读的文件分布在不同的层里，然后跑 `--rounds` 轮（默认 300）。每轮按固定种子的随机数决定做什么：
约 45% 的轮次写一个文件后 create；约 47% restore 到一个更早的 checkpoint，其中四成概率从最早的三个里选，好让层栈交替变深变浅；其余轮次在 checkpoint 多于 6 个时删掉一个中间的，走回收路径。
create 因快照存储放不下而返回 `resource_exhausted` 是预期的保护，脚本随即 restore 到最早的 checkpoint 腾出空间继续。每 50 轮存一份完整的 dmesg。

**enospc 模式。** 把 upper 所在的 ext4 写满，再逐档放出 0 到 6 个 4 KiB 的块，每一档做一次 create 和一次 restore，记录 gsd 返回了什么、沙箱是否还可用、dmesg 有无异常；
最后删掉填充，确认 create 与 restore 恢复正常且内容正确。它检验的是"可能失败的步骤全部在提交点之前"这条性质（[07](07-overlay-concurrency-and-fixes.md) 的"失败处理与发布"一节）：磁盘满造成的失败无论落在哪一步，都不能留下副作用。

### 5.2 `stress_multitree.py`：多棵树的自检负载

沙箱里常驻三棵 uid 1000 的进程树：一棵是单个 python 进程，一棵是 bash 带着 sleep 和 python，一棵把工作目录放在 `/tmp` 下。每个 python 每 0.1 秒"走一拍"：
给缓冲区的每一页写上当前拍数，往一直开着的文件描述符追加一行日志，再用"写临时文件后改名"的办法更新计数文件。

每拍开头进程先自检：**内存里的拍数、计数文件里的数、日志的行数（按路径读一次、按一直开着的 fd 读一次）、缓冲区每一页的值**，必须全部相等；不相等就往 `/dev/shm` 下的 `.bad` 文件追加一行。
这个自检之所以有力，是因为进程是从 checkpoint 那一刻原样继续跑的：只要内存镜像（含增量链）与文件系统回到的是同一时刻，这几个数就永远相等；
增量漏页、文件没有回滚、恢复后 fd 接到了别的文件、三棵树回到了不同的时刻，任何一种都会让它们对不上。`.bad` 文件放在不回滚的 `/dev/shm`，证据不会被下一次 restore 抹掉。

宿主侧在每次操作之后再核对：三棵树都活着、pid 集合不变、没有进程停在 T；restore 后各计数文件的值落在"该 checkpoint 创建前后读到的值"之间，随后继续增长。
脚本自己给出判定：校验失败次数为 0、dmesg 关键词零命中、除 `create:resource_exhausted` 外没有别的 gsd 错误、没有 `.bad` 文件、`/health` 为 ok、结束时用户进程数正确，全部满足才是 PASS。

### 5.3 `stress_delete_dense.py`：删文件密集

针对的是"属于 work 目录的缓存"那一组问题（[07](07-overlay-concurrency-and-fixes.md)）：切换层栈之后第一次需要 whiteout 时的内核 `BUG`，以及随之查出的另外两个缺陷。原有压测从不删除下层文件、从不 rmdir，所以这类路径一直没有被走到；这个脚本专门补这块。
脚本一开始先建一棵目录树并做一次 checkpoint 把它沉进下层，之后的删除才是"删下层文件"，才会产生 whiteout 与 opaque 目录。然后三类负载同时进行：

- **可精确校验的一棵树**（一半在 `/home/user` 下，一半在 `/tmp` 下），由宿主驱动：每次 create 之前、restore 之后各做一批变更——删文件、整目录删除、在被删的名字上重建目录和文件。
  这些变更都在静止状态下做，任何异常都算失败；create 之前记下清单（类型、路径、大小、内容的 md5），restore 之后的清单必须与目标 checkpoint 的**逐条相同**；
- **root worker**（默认 4 个），不归 gsd 管，各自在自己的子树里不停地删、重建、列目录，切换就发生在这些操作中间；
- **一个被 dump 与 restore 的用户进程**，循环"删掉一个文件、过一会儿再建回来"。它内存里的进度随 restore 一起回滚，所以它不应当遇到任何错误。

结束时脚本统计各层里 whiteout（主次设备号为 0:0 的字符设备）与 opaque 目录的个数，确认这一轮确实产生过它们——否则"没出问题"可能只是因为没走到。

### 5.4 判据

四个压测脚本共用同一套关键词，扫 guest 的 dmesg；看门狗另外取回串口输出，汇总时用同一套词再扫一遍：

```
KASAN  BUG:  Oops  WARNING:  list_add corruption  list_del corruption  list corruption
possible circular locking  possible recursive locking  sleeping function called from invalid context
ODEBUG  blocked for more than  hung_task  soft lockup  rcu.*stall  detected stalls
```

只算"新命中"：开机阶段就有的行（存在 `*-dmesg-boot.txt` 里）不算。`WARNING:` 这个词会抓到内核补丁里几处一次性的告警，那正是想要的——它们出现就说明走到了不该走到的分支（[24](24-configuration-and-observability.md)）。

一组压测判为通过，要同时满足：

| 条件 | 说明 |
|---|---|
| 看门狗的 `RESULT` 以 `DONE rc=0` 开头 | 脚本自己跑完，没有被判定卡死 |
| dmesg 与串口输出的关键词新命中都是 0 | 两处都要看：guest 一旦崩了就读不了 dmesg，只剩串口 |
| worker 的错误只在允许范围内 | `stress_overlay_ckpt.py` 只允许 `ENOENT` 与 `ESTALE`；`stress_delete_dense.py` 另外允许 `EEXIST` 与 `ENOTEMPTY` |
| 没有心跳过期 | 每个 worker 每秒写一次统计，超过 10 秒没更新即视为卡住 |
| gsd 侧的错误只有 `create:resource_exhausted` | 快照存储放不下时的预期保护；其他任何错误都要查 |
| 调试内核上另加：结束时 `debug_locks` 为 1 | 见 §7 |

worker 为什么允许出错：restore 会把 worker 脚下的目录树整个换成另一个时刻的样子，它刚要访问的文件可能在目标时刻并不存在（`ENOENT`），
它的当前目录可能已经属于被丢弃的时间线（`ESTALE`，[22](22-semantics-and-limits.md)）。这两种是切换的语义后果，不是缺陷。删文件密集的场景里，worker 自己删了又建的名字与 restore 换进来的内容相撞，还会得到 `EEXIST` 与 `ENOTEMPTY`。
除此之外的任何 errno——修复过程中出现过 `ELOOP`、`ENXIO`、`ENOTDIR`——都要当作内核里的真实问题去查。

`stress_overlay_ckpt.py` 自己只在日志末尾打印一行 `判定:`，把上面几项的原始计数列出来，不给出 PASS 或 FAIL，退出码也不反映判定；按上表逐条对照的工作在 2026-10-08 的验证里是由结果目录 `tools/` 下的一个汇总脚本做的。
它在遇到预期之外的 create 失败时，会把完整报错和当时的 gsd 日志存成 `stress-create-fail-r<轮次>.txt`，这类文件出现就必须查。

压测脚本还记录了内核 slab 里 `mnt_cache` 等对象在起止之间的增量。它不是判据，但值得顺手看一眼：修改前每次切换都会漏掉一组私有 mount，这个数会随轮数稳定上涨（[07](07-overlay-concurrency-and-fixes.md) 的"回收：被换下的层、mount 与缓存引用"一节）。泄漏的专门检验是 `probe_leak.py`（§6）。

### 5.5 看门狗与串口输出

内核一旦死锁或者在持锁时崩溃，guest 不会干脆地退出，而是卡住：测试脚本的下一条命令永远不返回，dmesg 也读不出来。所以压测要套一层看门狗。

```bash
bash scripts/stress/run_stress_watch.sh <输出目录> --template base-gsd --mode stress --rounds 300 --workers 6
SCRIPT=stress_multitree.py    bash scripts/stress/run_stress_watch.sh <输出目录> --template base-gsd --mode multitree --rounds 200
SCRIPT=stress_delete_dense.py bash scripts/stress/run_stress_watch.sh <输出目录> --template base-gsd --mode deldense --rounds 200
```

`run_stress_watch.sh` 在后台起压测脚本（默认是 `stress_overlay_ckpt.py`，用环境变量 `SCRIPT` 换别的），每 5 秒数一次输出目录里两个日志文件的总行数；连续 `STALL` 秒（默认 90）没有新行，就判定 guest 卡死：
先取回这个沙箱的串口输出存成 `console.txt`，再杀掉脚本和沙箱，往 `RESULT` 里写 `WEDGED sandbox=<id>` 并以 2 退出。正常结束时同样取一份串口输出，`RESULT` 里写 `DONE rc=<退出码> sandbox=<id>`。
`--mode` 对后两个脚本只决定输出文件名的前缀，看门狗靠它找到要数行数的日志，所以两边必须一致。几组并行跑时机器负载高、单次操作变慢，`STALL` 要相应调大。

`fc_console.sh <沙箱 id> [输出文件]` 负责取串口输出：guest 的控制台输出被 orchestrator 收进了自己的日志，这个脚本从 orchestrator 的日志里按沙箱 id 把它们筛出来，每行前面保留宿主机时间，
用来和压测脚本的操作日志（`<mode>-ops.log`，每个 gsd 操作的起止各一行，毫秒精度）对时间。guest 内核崩溃时的 oops 与 KASAN 报告只能从这里看到。它依赖本机 orchestrator 的部署方式，换机器要相应调整（[25](25-troubleshooting.md)）。

### 5.6 `stress_gsd.py`

最早的一个连续 restore 脚本，负载与 `verify_gsd.py` 相同，区别只是沙箱寿命更长、每次 restore 多打印 guest 的可用内存与进程数、不打印分段。它保留下来只作最小冒烟——想让一个沙箱连续 restore 几百次、看根盘与内存有没有缓慢变化时用它。
用法与 `verify_gsd.py` 一样是两个位置参数（模板名、次数），判据也只有末行的根盘判定。

---

## 6. 探针

探针是为查清某一个具体问题写的小脚本：各自起沙箱、造出那个问题需要的最小形状、把现场打印出来。它们大多没有统一的 PASS/FAIL，输出要对着脚本开头的说明读。
修一个内核问题之前先写出能稳定复现它的探针，修完之后同一个探针转为回归用例——[07](07-overlay-concurrency-and-fixes.md) 每个主题的"验证"小节基本都落在下表的某一行上。入库后在 `scripts/probes/`。

| 脚本 | 针对的问题 | 在哪一章展开 |
|---|---|---|
| `probe_fd_rollback.py` | checkpoint 前打开的 fd，之后经它写入的内容 restore 后还在不在（`fd` 模式）；循环之后根盘占用是否回落（`df` 模式） | 07 章"打开的文件跨切换的语义"一节 |
| `probe_mmap_rollback.py` | 被映射着的文件（共享可写、共享只读、私有三种映射，经映射写或经 fd 写）在 checkpoint 之后的写入是否回滚、是否写穿进了沉下去的层 | 07 章"打开的文件跨切换的语义"一节 |
| `probe_move_cost.py` | checkpoint 时把以写方式打开的文件留在新 upper 的代价：0、1、16、64、128 MiB 各一次，并核对沉下去的层里是拷贝、新 upper 里是原 inode | 07 章同上一节；代价模型在 [17](17-performance-after-fixes.md)，限制在 [15](15-known-limits.md) |
| `probe_estale.py` | restore 之后，新进程按绝对路径访问目录会不会得到 `ESTALE`：按"有没有别的进程把当前目录或目录 fd 停在这条路径或其祖先上"分成十种场景 | 07 章"目录与 inode 缓存跨 restore"一节 |
| `probe_estale_file.py` | 同一问题的非目录分支：一个被别的进程只读打开着、checkpoint 之后又改过的文件，restore 后按路径访问 | 07 章同上一节 |
| `probe_rename_race.py` | 压测里偶发的 `rename` 返回 `ENOTEMPTY`，是不是 restore 夹在一次 rename 的两次路径查找之间 | 07 章"restore 与 rename、unlink、rmdir 的竞争"一节；修复后仍有的单发见 [15](15-known-limits.md) |
| `probe_leak.py` | 长循环里根盘上"已删除但仍被占着"的空间与内核对象是否随轮数增长；另有"有进程拿着旧层里的文件不放"与专门给 KASAN 看的释放顺序两个阶段 | 07 章"回收：被换下的层、mount 与缓存引用"一节 |
| `probe_umount.py` | 回收逻辑的卸载分支。guest 的根卸不掉，所以在 guest 里另挂一个小 overlay，直接对它发热切层 ioctl，轮流走五种释放顺序 | 07 章"回收：被换下的层、mount 与缓存引用"一节 |
| `probe_whiteout_bug.py` | restore 之后删除下层文件触发内核 `BUG`（`bug` 用例及三个对照 `ctl`、`nodel`、`masked`）；work 目录的 trap inode 悬空导致 `ELOOP` / `ENXIO` / `ENOTDIR`（`trap` 用例） | 07 章"属于 work 目录的缓存"一节 |
| `probe_user_roots.py` | 各种常见用法下 guest 里 uid 1000 的进程树是什么形状、有几个根。只读探测，不调 checkpoint 接口 | [08](08-process-trees.md) |
| `probe_proc_cost.py` | guest 里扫一遍 `/proc` 的开销：进程数、内核线程数、读 `stat` 与 `status` 的耗时 | [17](17-performance-after-fixes.md) 的"收回"部分 |
| `probe_tpl_env.py` | 并排打印两个模板的 guest 环境（vCPU、内存、THP、内核、挂载、服务），定位模板之间的性能差异 | [17](17-performance-after-fixes.md) 的"一次失效的对照" |
| `probe_first_rpc.py` | 新沙箱上第一次 gsd 请求的墙钟耗时，分"经宿主代理"与"guest 内直连"两种方式各起若干沙箱 | [15](15-known-limits.md) 的"未查的"部分 |

其中 `probe_whiteout_bug.py` 是验收里唯一会跑的探针，因为它针对的问题修复得最晚、后果最重（guest 内核直接 `BUG`，带着目录锁死掉）。它的五种用例各起一个新沙箱：

- `bug`：两个文件进下层 → 删第一个（内核建一个 whiteout 并缓存在当前 work 目录里）→ restore（这对 upper 与 work 被丢弃并删除）→ 删第二个。修改前第二次删除在 3 秒内必然触发 `BUG`；
- `ctl`、`nodel`、`masked`：三个对照，分别是"缓存的 whiteout 所在的层被保留""restore 之前没删过下层文件""第一个 whiteout 落在开机时的 upper 里"，修改前也不崩，用来确认 `bug` 用例抓到的确实是那一个条件；
- `trap`：两次 create 之后丢弃页缓存，再新建并逐个访问几千个目录和文件，看有没有上述三种 errno。修改前这个用例的命中是概率性的，所以验收里连跑三次。

判读看输出：每个用例一行，`crashed=False`，`trap` 用例的错误计数为空（`errs={}`），`dmesg` 一栏为空。

---

## 7. 调试内核

### 7.1 为什么需要

发布版内核上，释放后使用不一定立刻崩溃：被释放的内存可能还没被别人拿走，读到的还是旧值，程序看上去一切正常，直到某一次碰巧不正常。锁顺序的错误更是如此，只有两条路径恰好交错时才会死锁。
压测在发布版上跑几百轮干净，只能说明"这几百轮没撞上"。调试内核把这两类问题从概率事件变成确定事件：每一次越界或释放后访问都当场报告，每一种可能死锁的加锁顺序只要出现过一次就报告，不需要真的死锁。

### 7.2 开了什么

在发布版 guest 内核的 config 基础上打开四类检查：

| 选项 | 检查什么 |
|---|---|
| KASAN（generic 模式） | 越界访问、释放后使用。报告以 `BUG: KASAN:` 开头 |
| lockdep（`PROVE_LOCKING`） | 加锁顺序可能成环、同一把锁递归、在不可睡眠的上下文里调用会睡眠的函数 |
| `DEBUG_LIST`（发布版 config 里本来就开着，不在差异片段里） | 链表增删时指针被破坏（`list_add corruption` / `list_del corruption`） |
| `DEBUG_OBJECTS` | 对象在仍被使用时被释放或重新初始化（`ODEBUG`） |

这四类报告的开头都在 §5.4 的关键词里。调试内核的 config 与构建说明随手册附带，在 `scripts/kernel/` 下；本章只列出上面这几项，完整的选项清单以那份 config 为准。

### 7.3 必须关掉 `VMGENID`

这是一个只有在本项目的使用方式下才会踩到的坑，而且踩到之后没有任何报错会提醒你"检查已经失效"。

lockdep 有一个特性：它一旦报告过一次问题，就把自己关掉，之后不再检查任何东西；`/proc/lockdep_stats` 里的 `debug_locks` 从 1 变成 0。
而沙箱不是冷启动的，是从模板快照恢复出来的（[05](05-guest-boot-and-injection.md)）。恢复时，基线内核的 vmgenid 驱动会收到"虚机代数变了"的通知，它在硬中断上下文里调用了一个会睡眠的通知链。
这个问题与 gsd 的补丁无关，但 lockdep 一视同仁地报告它，然后自关。结果是：每个从快照恢复的沙箱，lockdep 在第一秒就已经关掉了，后面几百轮压测的死锁检测覆盖是零。
第一次用调试内核压测时就是这样——跑完"干净"，事后才从 `debug_locks: 0` 发现死锁类检查根本没有工作。

所以调试内核的 config 里必须关掉 `VMGENID`，并且**每一组调试内核压测结束时都要确认 `debug_locks` 仍为 1**。压测脚本在开机时和结束时各读一次 `/proc/lockdep_stats` 写进汇总；
`stress_multitree.py` 把 `debug_locks: 0` 直接算作失败，`stress_overlay_ckpt.py` 只记录，要在汇总时核对（§5.4）。发布版内核上没有这个文件，汇总里会记成 `no /proc/lockdep_stats`，那是正常的。

### 7.4 怎么用

调试内核与发布版内核一样是固化在模板里的，所以流程是"换内核目录 → 用它另建一个模板 → 在这个模板上跑压测"：

1. 把 gsd 栈切到调试内核所在的目录。随手册附带的 `scripts/ops/switch-gsd-new920b.sh` 支持用环境变量 `GSD_KDIR` 临时指定内核目录（[23](23-deployment.md)）；
2. 用这个内核另建一个模板，**guest 内存给 4096 MB**——KASAN 的影子内存和隔离区要额外占用内存，历次调试模板都按这个规格建；建模板不使用缓存；
3. 起一个沙箱确认环境：`/proc/lockdep_stats` 里 `debug_locks` 为 1，dmesg 里有 `kasan: KernelAddressSanitizer initialized (generic)`，没有 vmgenid 或 `sleeping function` 相关的行；
4. 用看门狗跑压测，`--template` 指向调试模板。调试内核慢，`STALL` 要调大；
5. 每组结束后看汇总里的 `lockdep_end`，`debug_locks` 必须仍为 1；
6. 切回发布版内核目录。调试模板留着不影响别的模板，但不要拿它做任何性能测量。

### 7.5 它不能代替什么

调试内核不能代替发布版内核上的压测，两者要各跑一遍。原因是调试内核慢得多、时序也不同：同样 300 轮，worker 完成的操作数只有发布版的几分之一，切换与文件操作交错的方式不一样，
某些竞态窗口在调试内核上反而更难撞上。发布版负责"在真实时序下撞得上"，调试内核负责"撞上之后一定看得见"。

调试内核上也不做功能验证和性能测量：前者没有额外价值，后者的数字没有意义。这一点在 [20](20-results-and-gaps.md) 的缺口表里记了一笔。

---

## 8. 现场条件

跑功能验证、性能与压测之前要具备的条件，逐条的核对命令在 [26](26-acceptance-runbook.md)：

- **gsd 栈在运行。** 宿主上是带 gsd 注入的那一版 orchestrator，补丁内核与 gsd 二进制在位（[23](23-deployment.md)）。同一个端口同时只能跑一套栈。
- **模板 `base-gsd`。** 在 gsd 栈下建的、2 vCPU / 2048 MB 的模板。所有脚本的默认模板名都是它。模板固化了建模板时的内核、gsd 与 overlay-init，三者换了任何一样都要重建模板，否则测的还是旧的。
- **Python 环境与 SDK。** 用装有本分支 SDK 依赖的 conda 环境（新 920B 上的例子是 `jll-e2b-gsd`，其他机器用自己的环境名），并把 `PYTHONPATH` 指向代码仓的 `py-sdk` 目录，让脚本用到的是带 checkpoint 接口的那份 SDK，而不是环境里可能装着的别的版本。
  判断用对了没有：`python -c 'import e2b; print(e2b.__file__)'` 应当落在代码仓的 `py-sdk` 下。
- **凭据。** 所有脚本通过同目录或上一级目录的 `e2b_env.py` 读取客户端凭据（API 地址、key、token）；建模板用 `build_template.py`。
- **guest 内直连 gsd 要带 token。** 几个脚本为了绕开宿主代理，在 guest 里用 curl 直接调 `127.0.0.1:49984`。secure 沙箱里除 `/health` 外每个请求都要带 `X-Access-Token`（[04](04-architecture.md)），
  脚本从 SDK 的沙箱对象上取到这个 token 并加进请求头。自己手工在 guest 里 curl 时同样要带，否则得到 401。
- **长测时的 journal。** gsd 把 criu 的详细输出打进 journal，量不小。它带来两个要处理的地方：一是 journald 默认按单元限流，短时间内条数太多会连 gsd 自己的 JSON 日志一起丢掉，而好几个脚本是从日志里取分段耗时和 dump 的根 pid 的——
  `verify_clear_refs_race.py` 因此在开头给 gsd 取消限流、把 journald 改成易失存储并限定上限；二是 journal 所在的 `/var/log` 与层目录在同一个根盘上，会让"根盘有没有增长"这类判据失真——
  `verify_fixes.py` 的 G-11 与 `probe_leak.py` 都在计算时把 `/var/log` 的增长单独扣掉。写新的长时间测试时两点都要考虑。
- **机器是共用的。** 压测一组要起沙箱跑几百轮，并行跑几组时会明显抬高机器负载；压测脚本记录的客户端墙钟耗时因此只反映当时的并发程度，不作判据，也不能当作 gsd 的耗时引用。

950 上未部署、未测，本章所有脚本都没有在 950 上跑过。

---

## 代码位置

| 内容 | 位置 |
|---|---|
| 外部依赖的注入点 | `sysOps`、`defaultSysOps`（`ckpt/sysops.go:17`、`:38`） |
| 假的 `/proc`、假信号、假 criu | `fakeSys`、`newFakeSys`、`newTestService`、`newTestOverlay`（`ckpt/fake_test.go:38`、`:65`、`:364`、`:391`） |
| 层切换的注入点 | `OverlayManager.switchFn`（`ckpt/overlay.go:62`） |
| gsd 单测 | `ckpt/` 下各 `*_test.go`；`packages/gsd/internal/permissions/authenticate_test.go`、`packages/gsd/internal/api/store_test.go`、`packages/gsd/main_test.go` |
| orchestrator 单测 | `packages/orchestrator/internal/cfg/model_test.go`、`packages/orchestrator/internal/template/build/core/rootfs/gsd_test.go` |
| SDK 单测 | `py-sdk/tests/test_gsd_checkpoint.py`、`test_gsd_errors.py`、`test_gsd_headers.py` |
| 功能验证与性能 | `e2b-infra/gsd/scripts/acceptance/`：`verify_gsd.py`、`verify_fixes.py`、`verify_multitree.py`、`verify_clear_refs_race.py`、`demo_checkpoint_perf.py` |
| 压测与看门狗 | `e2b-infra/gsd/scripts/stress/`：`stress_overlay_ckpt.py`、`stress_multitree.py`、`stress_delete_dense.py`、`stress_gsd.py`、`run_stress_watch.sh`、`fc_console.sh` |
| 探针 | `e2b-infra/gsd/scripts/probes/`：各 `probe_*.py` 与 `exp_gsd_swap.py` |
| 调试内核 config 与构建说明 | `e2b-infra/gsd/scripts/kernel/` |

---

## 本章要点

- 测试分四层：单测管 gsd 的编排逻辑，功能验证管端到端行为，性能脚本管分段耗时，压测与探针管内核侧的并发与回收。最严重的几个问题都在内核补丁里，单测看不见，只有压测撞得出来。
- gsd 的单测靠 `sysOps` 这个注入点换上假的 `/proc` 与假的 criu；凡是依赖 CRIU 或内核实际行为的结论，单测通过不构成证据。
- 多数脚本的退出码不是判定。`verify_gsd.py` 看末行的根盘判定，`verify_clear_refs_race.py` 看漏页轮数，新版 `demo_checkpoint_perf.py` 看退出码、`OK` 列和 `判定: PASS`，`stress_overlay_ckpt.py` 要按判据表逐条对照；`verify_fixes.py`、`verify_multitree.py`、新版性能脚本和后两个压测脚本的退出码可信。
- `verify_fixes.py` 全部通过的样子是 PASS 26 / FAIL 0 / SKIP 2；恒为 SKIP 的两项分别要重启 orchestrator 才能测、已由 SDK 单测覆盖。`verify_fixes.py` 与 `verify_multitree.py` 把 dmesg 的可疑行只记为证据、不计入判定，要自己搜一下。
- 漏页测试的三个要点：期望值从 `/proc/<pid>/mem` 读而不是让进程自报，写入必须限速否则漏页会被重新写脏掩盖，恢复出来的进程必须仍是停止状态。
- 压测的判据是 dmesg 与串口关键词零新命中、worker 的错误只有切换语义允许的那几种 errno、没有心跳过期、gsd 侧只有 `resource_exhausted`；guest 会卡死而不是退出，所以压测一律套看门狗。
- 调试内核在发布版基础上开 KASAN、lockdep、`DEBUG_OBJECTS`（`DEBUG_LIST` 发布版本来就开着），必须关 `VMGENID`，并在每组结束时确认 `debug_locks` 仍为 1——否则 lockdep 在沙箱从快照恢复的第一秒就已自关，死锁检测的覆盖是零而没有任何提示。
- 调试内核慢、时序不同，不能代替发布版内核上的压测；两者各跑一遍，一个负责撞得上，一个负责撞上之后看得见。
