# 23 · 测试体系与功能验证

> 给评审和接手测试的人看。读完能知道：这套方案为什么要多问一句"是怎么做到的"、
> 三层测试各管什么、每个正确性脚本和 crtest 用例用什么判据、`run.sh` 各档跑的是哪些脚本。
> 数字不在本篇，实测结果见 [25](25-results-and-compliance.md)；性能口径见 [24](24-performance-methodology.md)；
> 上机操作步骤见 [09](09-acceptance-runbook.md)。

---

## 1. 为什么测试要多问一句

普通功能测试只问"结果对不对"。这套方案有四种失效，其中三种不改变结果：内容逐字节对得上，
断言全过，坏的是成本、覆盖范围或者判据本身。

| # | 失效 | 结果对吗 | 靠什么才看得见 |
|---|---|---|---|
| 1 | **增量退化成全量**：机器没有 HDBSS，又没显式设 `FC_TRACK_DIRTY_PAGES=true`，每次 checkpoint 都整份拷 guest 内存 | 对 | 服务端每次回报的 `mem_mode`：除了沙箱的第一个 checkpoint，出现 `full` 就是有问题 |
| 2 | **checkpoint 之后 pause 丢封层**：原生 pause 导出磁盘时只取了当前写层，最后一次 checkpoint 之前的写全丢；导出的 header 由导出内容推出，自己跟自己一致 | **错，且自洽** | checkpoint 两侧各写一份数据，pause/resume 后用 `O_DIRECT` 绕开 guest 页缓存读回 |
| 3 | **读过的页被算成脏页**：判据用"驻留"代替"写过"，脏页集塌缩成工作集（原生路径上的现象，本方案判据不同，见 [13](13-dirty-page-tracking-and-hdbss.md)） | 对 | 把名义改动量和实测产物大小并排放 |
| 4 | **判据本身消失**：SDK 不透出 `mem_mode` 时，脚本取到的是 `?`，于是跳过全量/增量两条断言，照样打印"全部通过" | 对 | 报告必须记断言项数与 SDK 版本 |

由此得到三条纪律，贯穿本部分：

- **先自证"这个检查会失败"**：比对之前先确认各代现场确实不同；兼容矩阵先测原生基线。
- **验磁盘内容一律 `O_DIRECT` 读回**：guest 页缓存会把磁盘层的错误完全盖住。
- **报告记项数**：同一个脚本少了两项仍然打印"全部通过"。

---

## 2. 三层测试

| 层 | 位置 | 证什么 | 怎么跑 |
|---|---|---|---|
| 单元测试与属性测试 | 代码仓库 `KASandbox_0904`（分支 `deltabox-dev`），与被测代码同目录；Go 在 `packages/orchestrator/internal/checkpoint/`、`internal/sandbox/{block,fc,network,rootfs}/`，FC 在各源文件的 `#[cfg(test)]`，SDK 在 `py-sdk/tests/test_checkpoint_errors.py` | 部件与不变量 | 见[附录 B](B-extending.md) |
| 交付态验收 | `e2b-infra/rollback/scripts/acceptance/`，五个单文件脚本，互不依赖 | 整机正确性、耗时、并发 | 单独拷到目标机即可跑 |
| 补充测试与开发态工具 | `rollback/scripts/crtest/`（20 个用例 + 基准）、`rollback/scripts/dev/`（共享 `lib.py` 的工具箱） | 盲区、边界、故障注入、长稳 | 一站式入口 `rollback/scripts/950/run.sh` |

几条约定：

- RPM 构建的 `%build` 只做 `go build`，**不跑单元测试**，单测要在代码仓库里跑。
- 交付态脚本**故意各带一份**宿主探针，不抽公共模块：拷走一半的公共模块不会报 ImportError，
  更可能被兜住，然后"脏页后端""产物文件系统"这些条件标签整列变成"未知"。crtest 的 `common.py`
  出于同一理由从 `checkpoint_concurrent.py` 复制而不 import。
- 正确性与性能**分开跑**：正确性现场不给性能垫噪声，性能档位也不拖慢正确性。
- 开发态脚本按位置参数选判据：`correctness.py <方案> [df挂载点]`、`loop.py <方案> [次数]`。
  交付的是 ext4 方案，第一个参数写 `ext4`；手敲时把挂载点当成方案名，会用错的规则判对的产物、报假 FAIL。
- **都在宿主机上以 root 跑。** 远程只拿得到客户端墙钟；服务端分段计时、产物实占、脏页后端、
  产物盘文件系统只在宿主机上读得到。正确性可以远程验，性能必须上宿主机。

---

## 3. 测试矩阵

全书唯一一张。只记状态，数字见 [25](25-results-and-compliance.md)。"本期"指 [README](README.md) 所列的代码基准。

| 要证的性质 | 脚本 / 用例 | 判据 | 本期 · 920B | 本期 · 950 |
|---|---|---|---|---|
| 快照/恢复后照常干活；内存、文件、删除、权限位、进程都回到那一刻 | `acceptance/checkpoint_verify.py` | 末行 `✓ 59 项校验全部通过。` | 已跑 | 待测 |
| 树语义：线性 / 前滚 / 分叉跨 LCA / 删除与合并（compact / fold） / 失败 | `dev/correctness.py ext4` | 末行 `ALL PASS` | 未跑（脚本已按合并语义更新） | 待测 |
| checkpoint 之后原生 pause，磁盘与内存完好 | `dev/pause_verify.py`（磁盘）；crtest T39（内存与盘上标记） | `O_DIRECT` 读回一致；内存 md5 一致 | T39 已跑，`pause_verify.py` 未跑 | 待测 |
| 不弄坏原生生命周期 | `dev/compat_matrix.py`；crtest T41 | 无 `BROKEN` | T41 已跑 | 待测 |
| HDBSS 三级证据 | `dev/hdbss_evidence.py` 等 | L1 / L2 / L3 | 920B 无 HDBSS，只作负样本 | 待测 |
| 稳定性：数百次交替回滚 | `dev/loop.py ext4` | `failures: 0` | 未跑；由长测覆盖（见下） | 待测 |
| 盲区、边界、故障注入 | crtest T11–T41 | 各用例三段式断言 | 已跑（重启类单独跑） | 待测 |
| 并发与长稳：多沙箱随机操作逐项校验 | `acceptance/checkpoint_concurrent.py` D 段；crtest T36 全规模 | restore 后五字段逐项一致 | 已跑 | 待测 |
| SDK 异常语义 | `crtest/sdktests/test_checkpoint_errors.py` | pytest 全过 | 已跑 | 待测 |
| 分档性能、串行长尾、空闲后 restore | 见 [24](24-performance-methodology.md) | —— | 已跑 | **待测** |

正确性结论可以从 920B 搬到 950：两种脏页后端喂给上层的是同一张位图，语义相同，只差性能。
**性能数字不能搬**（[24](24-performance-methodology.md#2-条件标签)）。

---

## 4. 正确性脚本的判据

### 4.1 三重证据加一条最硬的

"restore 返回成功、沙箱还能执行命令"不等于"内容回到了目标时刻"，一个只返回 `true` 的实现也能满足前两条。
所有正确性脚本用同一组证据：

| 证据 | 观测点 | 它证明什么 |
|---|---|---|
| 内存标记 | `/dev/shm` 下的标记（tmpfs 完全在 guest RAM 里，不落虚拟盘） | 变回去只可能是内存被搬回去了 |
| 内存大块 | `/dev/shm` 下上百 MB blob 的 md5 | 一大片逐字节对上，排除"碰巧那一页对了""共享零页" |
| 磁盘标记与大文件 | 根文件系统上的标记与几十 MB 文件的 md5 | 磁盘视图也换掉了 |
| **进程** | 快照前起的心跳进程，恢复后 PID 和启动时刻不变 | 是内存搬回去了，不是虚机重启了；先 `kill -9` 再回滚，它必须连 PID 一起活回来，重放式实现做不到 |

磁盘那一半的同类证据是**删除和权限位能回滚**：新建能回滚不稀奇，删掉的文件复活、权限位回到那一刻的值，
才说明是块级快照而不是补文件。

### 4.2 `checkpoint_verify.py`：一条直链上的 59 项

交付态脚本，默认参数直接跑。三代 A → B → C，每代换掉全部观测点，另外各动一样不好回滚的东西：
`victim` 文件 A 建、B 删、C 保持删掉；权限位 600 / 640 / 755；标记写在 `/`、`/etc/ckpt-root.conf`、`/ckpt-root/`
（不是 `/home/user`，用户目录另有挂载）。

- **区分度自证**：回滚前先把三代现场并排，11 项里 9 项必须三代各不相同，心跳的 `hb_pid`、`hb_start`
  两项必须三代相同。两条方向相反的断言合起来才把这张表钉住。
- **活体判据只用计数器，不用时间**：恢复会把 guest 的单调时钟拨回快照那一刻，时间倒流是对的。
  心跳 0.2 s 一拍，分两段各数 1.5 s，硬断言只要求 `g1 + g2 >= 1`；节奏打印给人看，不判定。
- **`mem_mode`**：`gA == full`，`gB / gC == incremental`。取值 `getattr(ck, "mem_mode", None) or "?"`，
  取到 `?` 时跳过这两条（第 4 种静默失效）。装好 SDK 覆盖层是这条判据有效的前提，
  `install.py --check` 会自检 `mem_mode` 字段。

59 项的构成（默认 `--rounds 3`）：

| 节 | `verify_live`（每次 5 项） | `verify_scene`（每次 2 项） | 其他 | 小计 |
|---|---|---|---|---|
| 1. 建三代现场 | 3 次 = 15 | —— | `mem_mode` 2 + 区分度 2 | 19 |
| 2. 逐级回退 C → B → A | 1 次 = 5 | 2 次 = 4 | —— | 9 |
| 3. 跨 2 代前滚 A → C | 1 次 = 5 | 1 次 = 2 | —— | 7 |
| 4. A / C 交替 3 轮 | 1 次 = 5 | 6 次 = 12 | —— | 17 |
| 5. `kill -9` 心跳后回滚到 A | 1 次 = 5 | 1 次 = 2 | —— | 7 |
| **合计** | 35 | 20 | 4 | **59** |

59 不是常数：`--rounds r` 时第 4 节是 `4r + 5` 项；模板里没有 `python3` 时每次活体检查少一项，总数 52；
有 `k` 项不一致时 `verify_scene` 记 `1 + k` 项。**57** 说明 SDK 没透出 `mem_mode`，增量判据失效。
末尾那张 API 耗时表样本少、每代现场上百 MB，不是性能基准。

### 4.3 `correctness.py`：树语义

交付态脚本只走直链。分叉之后回滚集怎么算、删一个被引用的节点会怎样、算不出回滚集时拒绝还是硬来，
在直链上构造不出来，由开发态的 `correctness.py ext4` 负责。每次 restore 都用三重证据判定。

| 节 | 做什么 | 断言 |
|---|---|---|
| 1. 线性链 | ck1..ck5，脏页 0 / 64 / 256 / 512 / 0 MB（0 MB 是回滚集为空的边界） | 树根 `mem_mode`（随 `CHECKPOINT_FULL_ROOT` 切换期望值）；ck2..ck5 全增量：2 项 |
| 2. 回退与前滚 | ck5→ck4→…→ck1，再前滚到 ck5、再回 ck1 | 7 次 restore：7 项 |
| 3. 分叉跨 LCA | 站在 ck1 建 ck6（ck2..ck5 成旁支），跨支来回；再建 ck7（父 ck5），覆盖 LCA = ck2 | 2 条父节点断言 + 6 次 restore：8 项 |
| 4. 删除 | 删 ck2（有后代，非基准，只有一个子节点 ck3） | 见下 |
| 5. 失败 | 移走 ck5 的 `mem_bitmap`，从 ck7 回 ck4 | 先站到 ck7、被拒绝、拒绝没动沙箱状态、沙箱照常运行、放回后能恢复：5 项 |

第 4 节按合并开关分两支（脚本自己判断走哪支）：

- **`CHECKPOINT_COMPACT` 开（默认）**：ck2 不是基准、只有一个子节点，删除结尾被合并进 ck3。
  断言 **ck3 的父节点变成 ck1**（ck2 的目录已消失），1 项。
- **合并关**：ck2 隐藏保留，断言 `hidden == true`、`mem_bitmap` 保留、`snapfile` 已丢、`mem_diff` 保留，4 项。

两支之后相同：`list` 里看不到 ck2；回 ck1（跨 ck2 纪元）、回 ck4；删叶子 ck6 必须物理删除；
回 ck3 其余分支不受影响。所以默认配置下共 2 + 7 + 8 + 6 + 5 = **28 项**，合并关时 **31 项**。
合并为什么不改变任何 restore 的结果，见 [14](14-memory-diff-tree.md)。

第 5 节报的错形如 `failed to materialize revert files: failed to read sidecar of checkpoint ...`，
是"宁可报错，不可静默损坏"的可执行版本（[20](20-failure-semantics.md)）。

### 4.4 `pause_verify.py`：一个自洽的静默损坏

结构：checkpoint 前写 `before.bin` → checkpoint（封层）→ 写 `after.bin` → 原生 pause / resume →
两份都用 `dd iflag=direct` 读回比 sha256。每份 16 MiB 随机数据（`--mb` 可改）。
抓缺陷的是 `before.bin`：它在被封存的层里，是唯一会丢的那份；`after.bin` 是对照。
不加 `O_DIRECT` 时，刚写过的数据还在 guest 页缓存里，读回来一定对，会把缺陷完全盖住。

脚本另钉住一条边界：pause/resume 之后回不到 pause 之前的 checkpoint。
这是断言而不是注释：哪天行为变了（支持了，或变成静默返回错数据），它会立刻报出来。语义见 [02](02-semantics-and-limits.md)。

### 4.5 `compat_matrix.py`：与原生生命周期的组合

本方案加了 checkpoint / restore，e2b 原有 create / connect / pause / kill（connect 对已 pause 的沙箱等于 resume），
两组操作动的是同一个沙箱的层栈和内存。每格三种判定：

| 判定 | 含义 |
|---|---|
| `OK` | 能做，做完沙箱和数据都对 |
| `REFUSED` | 服务端明确拒绝且没留下半吊子状态，**是边界不是缺陷** |
| `BROKEN` | 能调用但结果不对，或本该能做的原生操作被弄坏了，**唯一的真问题** |

六组 15 格：A 基线（没 checkpoint 的沙箱先 pause → connect，证明原生能力本来是好的）1 格；
B checkpoint 之后做原生操作 8 格；C restore 之后 3 格；D 连做三次 checkpoint（多层封存）之后 pause 1 格；
E 反向（pause/resume 之后走完整 checkpoint 流程）1 格；F kill 1 格。
数据一律 `O_DIRECT` 读回 8 MiB 比 sha256。预期的两个 `REFUSED`：pause/resume 之后回不到之前的 checkpoint
（账本不跨 pause）；kill 之后不能 connect（e2b 原有行为）。

### 4.6 `hdbss_evidence.py`：能力不等于数据面

| 层 | 问什么 | 怎么问 |
|---|---|---|
| L1 能力 | 宿主 KVM 认不认 cap 502 | `cap_test.c`：`KVM_CHECK_EXTENSION`，再在临时 VM fd 上真的 `KVM_ENABLE_CAP(502)` |
| L2 自报 | FC 给**这个沙箱**选了哪个后端 | FC API `GET /` 的 `dirty_tracking`：`hdbss` / `kvm-wp` / `off` |
| L3 数据面 | 硬件是否真在记 | 写密集负载：先把文件整份分配出来，再计时覆写两遍（`conv=notrunc`），比"冷/热"耗时；有 `perf` 时另数 `kvm_exit` |

软件写保护下，checkpoint 会把所有页重新写保护，之后每个干净页的第一次写都要陷出一次，所以冷一遍明显慢于热一遍；
HDBSS 下两遍接近，冷/热接近 1。920B 没有 HDBSS，是这条判据的负样本。
如果 950 上 `dirty_tracking=hdbss` 而冷/热仍明显大于 1，说明能力启用了但数据面没生效。原理见 [13](13-dirty-page-tracking-and-hdbss.md)。

### 4.7 `loop.py`：稳定性

默认在两个 checkpoint 之间**交替**回滚 200 次（交替才会换回滚集；`--same` 反复回同一个作对照）。
每次都做三重证据校验，任何一次内容错误立即停下并打印现场，判据是 `failures: 0`；分位数顺带打印，不判定。

---

## 5. crtest 用例

`rollback/scripts/crtest/` 覆盖上面几个脚本碰不到的盲区。每个用例失败即停、按"断言失败 / 期望 / 实际 / 依据"三段式打印；
退出码 0 全过、1 断言不过、**3 前置不满足**（`run.sh` 记为 SKIP）。原始数据写 JSON（`ops` 每次操作一条、`assertions` 每条断言一条，
`ok` 为 `null` 的是只记录不判定的观测项）。默认规模都按一次 ≤ 5 分钟定。

| 用例 | 测什么 | 硬断言要点 |
|---|---|---|
| T11 生命周期竞争 | checkpoint / restore 进行中 kill 或 `beta_pause` | 服务端不崩（旁观沙箱全程正常）；在途操作要么成功要么给明确错误，且不超过 200 s（锁排队 60 s + FC 调用 120 s）；kill 后目录、FC 进程、netns 都回收 |
| T12 同沙箱多客户端 | 按沙箱串行化 | 风暴后对账：无幽灵 checkpoint、删掉的不再出现、没删的不失踪、残留的每个都能回 |
| T13 冻结窗口干扰 | 别的沙箱 checkpoint 会不会拉长我的冻结 | 只硬判 restore 全成功、现场一致、心跳有数据；随并发数的曲线只记录 |
| T14 写密集并发正确性 | 大脏集 + 并发 | 默认先 `SIGSTOP` 写者：内存与文件 md5、seq 精确回到快照时刻，写者状态回到 `T`；`--no-stop` 只做页级/行级自校验。拍摄时刻正在写的那一页（至多 1 页）放过 |
| T15 树形分支并发 | 分支语义 + 隐藏条目回收 | 每次 restore 现场一致；`list` = 本地账本 |
| T18 orchestrator 重启 | 账本不跨重启 | 重启前 store 下的沙箱目录一个不剩；新沙箱 `list` 为空，第一次 checkpoint 是全量新根且能回；netns 不增长。要 `--allow-restart` 才执行 |
| T21 故障注入 | `CHECKPOINT_FAULT_INJECT` 的 `envd_timeout` / `torn_assemble` / `seal_move` / `commit_late`，每次一条 | 按常开与 `:once` 两套期望：失败后账本没坏（隐藏条目、链没断）或自愈；`envd_timeout` 下一次 checkpoint 的父节点是刚才 restore 的目标。要在宿主机上读 manifest |
| T22 深链随机回滚 | 深链 + 分支下的回滚集 | 随机顺序回每一个都一致；删中间层后后代仍能回；`list` = 账本 |
| T23 文件系统边界 | 写到一半拍、open fd、rename、fsync | 流式文件是快照那一刻的前缀；fd 上行号连续且 restore 后能接着写；rename 被回滚；fsync 过又删掉的文件回来 |
| T24 网络状态 | 活连接跨 restore | guest 内 loopback 长连接仍可用；新连接立刻可用；给了 `--external` 时跨出沙箱的旧连接干净失效 |
| T25 时间与 CPU | restore 后定时器健康 | uptime 必须倒退；`date` 不倒退且与宿主差 < 5 s；`sleep 1` = 1.0±0.1 s；10 s 窗口 CPU0 空闲 ≥ 90%；arch_timer < 200 次/s |
| T26 串口 | 串口自锁回归 | 每轮 restore 前后写 4 KB 到 `/dev/ttyS0`，退出码 0 且 < 3000 ms |
| T32 restore 随脏集 | O(脏页) | `fc_bitmap` 各档 p50 极差 < 2 倍；`fc_memory` 随脏集单调不降 |
| T34 运行期读链深 | 链深会不会拖慢 guest 冷块读 | 只硬判前置（层建得出、`drop_caches` 生效等），曲线只记录 |
| T36 混合稳态 | 长时间乱序负载下漏账、漏回收 | 每个 worker 线程跑满全程且循环体没抛过；restore 后 0 不一致；收尾 `list` = 账本；删干净后为空；活 FC 归零。全规模 `--sandboxes 16 --seconds 1800` |
| T37 两道配额闸 | `CHECKPOINT_MIN_FREE_BYTES` / `CHECKPOINT_MAX_PER_SANDBOX` | 507 `disk_full` / 429 `too_many_checkpoints`，异常类与 `.reason` 对，沙箱不受影响。字节配额没有对应用例 |
| T38 FC 侧 faulted | FC 过了提交点才失败（需带 `rollback-fault-inject` 特性的 FC 与 `FC_ROLLBACK_FAULT_INJECT=post_commit`） | 抛 `CheckpointTornException`、`.reason == "torn"`；之后 checkpoint / restore 都被拒；服务端与 FC 日志有对应行 |
| T39 checkpoint 之后原生 pause/resume | 做过 checkpoint 的沙箱原生 pause 时，导出的内存差分必须含全部该含的页（位图曾被 checkpoint/restore 清过） | 三个场景（checkpoint 后直接 pause；checkpoint → 改 → restore → pause；反复 3 轮再 pause）：内存 md5 与 pause 前一致、页级坏页 0、写者 pid 不变、内存与盘上标记符合场景 |
| T40 restore 紧跟请求 | restore 前后流式请求的截断 | 只硬判沙箱存活、账本 = `list`、线程没提前死；截断次数只记录 |
| T41 干净沙箱原生 pause/resume | 没做过 checkpoint 的沙箱 | pause/resume 后内存、盘、进程、连接都正常；连做三轮；resume 后能重新 checkpoint/restore；`list` 为空 |

三条口径容易问错，记在这里：

- **并发下"拍那一刻的现场"没有定义**，所以 T12 只在风暴后对账；T14 / T15 每个沙箱内部串行，现场才良定义。
- **串口只在 T26 里碰**，其余用例一律走 `commands.run`：restore 之后的串口发送不作为采集通道。
- **撕裂的沙箱不再碰 guest**：`torn_assemble` 之后不调用现场采集，否则测试自己会挂住。

`crtest/tests/` 下是离线单测（不需要沙箱），`python3 -m unittest discover -s tests` 即可跑。

---

## 6. `checkpoint_concurrent.py`：并发四段

| 段 | 做什么 | 断言 |
|---|---|---|
| **A** 跨沙箱扇出 | N 个沙箱用 barrier 对齐同时发同一操作，N 从 1 涨到 16（`--fanout`），每跳验回到了哪一代 | 看 p50 / 最坏随 N 怎么涨、涨在服务端哪一段；宿主侧同时记产物盘写入与 orchestrator CPU |
| **B** 同沙箱争用 | 一个沙箱 T 个线程（默认 4）各自循环 checkpoint → restore → list，默认各走各的连接 | 不断言现场（并发下没有定义）；断言操作全部收敛、没有错误、沙箱存活、`list` 不比成功数多（无幽灵）、每个 checkpoint 都回得去 |
| **D** 混合稳态 | S 个沙箱（默认 8）各自单线程随机循环 T 秒：50% 写脏（内存 8 MB + 文件 4 MB）后 checkpoint、35% restore 到随机一个已有 checkpoint、15% delete | 每次 restore 后逐项比 5 个字段：`mem_gen`、`file_gen`、内存 blob 前 4 MiB md5、文件 blob 前 4 MiB md5、心跳 pid |
| **C** 生命周期竞争 | checkpoint / restore 进行中 kill 或原生 pause | 默认不跑，要 `--lifecycle`；可能打挂 orchestrator，宿主上有别人的沙箱时不要开 |

D 段的循环体整体包在 `try` 里，任何异常只记一条失败、不带走线程；第一次 restore RPC 失败时该沙箱停手，
留现场并在 `--keep-on-failure` 时抄 FC 状态与日志。收到 429 不主动删，只记失败继续。
`D.json` 的 `verified` = 校验通过数 / 进入校验的 restore 数。长测怎么用它，见 [24](24-performance-methodology.md#6-并发与长跑的测量)。

---

## 7. `run.sh` 各档

入口 `rollback/scripts/950/run.sh`，退出码 = FAIL 项数，每档写一份 `SUMMARY.md`。
测试套件不在 RPM 里，跟着 e2b-infra 仓库走。

| 档 | 跑什么 | 判据 |
|---|---|---|
| `smoke` | `crtest/portability/preflight-customer.sh`（宿主预检）；SDK 覆盖层 `install.py --check`；从运行中 orchestrator 的日志抓 `checkpoint capabilities` 行；FC 二进制 sha256；`acceptance/checkpoint_verify.py` | 5 项全 PASS |
| `func` | crtest T12 T15 T22 T24 T23 T34 T32 T41 T39 T26 T14 T40 T25 T36 T11 T13（不需要重启、不改服务端 env 的 16 个）+ `crtest/sdktests/test_checkpoint_errors.py` | FAIL = 0 |
| `func --allow-restart --restart-cmd ...` | 加跑 T18 | 同上 |
| `func --fault-cases` | 加跑 T21 ×4 与 T38（需要服务端先带对应注入重启；没装的那条退 3 记 SKIP） | 同上 |
| `func --quota-cases` | 加跑 T37 `disk_full` / `too_many`（需要服务端带对应开关重启） | 同上 |
| `perf` | `crtest/bench/bench_tiers.py --tier-set short` → `compliance.py` + `analyze.py`；`serial_restore.py -n 100`；`checkpoint_concurrent.py --stages A,B` | 无 FAIL；**超达标线不判 FAIL**，看 `compliance.log` 末尾 |
| `long` | 不自动跑，只打印长测清单：D 段 16 沙箱长跑、C 段、T36 全规模、串行 5000 次、分档全表 | —— |

SDK 覆盖层自检的异常族判据是：原有 8 个子类都在、并且映射表里每一项都是 `CheckpointException` 的子类
（现在是基类加 9 个子类，新增 `CheckpointBytesLimitException`，见 [03](03-errors-timeouts-concurrency.md)）。

---

## 8. 测试环境与沙箱规格

全书的测试用同一个模板，规格变了数字要重采：

| 项 | 值 | 怎么核对 |
|---|---|---|
| 模板别名 | `base` | 各脚本与 `run.sh` 的默认模板（`--template` 可改） |
| 基础镜像 | `harbor:443/e2b-orchestration/ubuntu:22.04-custom` | `benchmark/build_template.py` 里的 `FROM` |
| vCPU | 2 | `GET /templates` 的 `cpuCount` |
| 内存 | 2048 MB | `GET /templates` 的 `memoryMB` |
| 磁盘 | 约 940 MB（guest 内 ext4，240640 块 × 4 KiB） | `GET /templates` 的 `diskSizeMB` |

在宿主机上核对（access token 取部署时写下的 SDK 凭据）：

```bash
AT=$(jq -r .accessToken /root/.e2b/config.json)
curl -s -H "Authorization: Bearer $AT" http://127.0.0.1:3000/templates \
  | jq -r '.[] | select(any(.aliases[]?; . == "base"))
           | "cpuCount=\(.cpuCount) memoryMB=\(.memoryMB) diskSizeMB=\(.diskSizeMB) buildStatus=\(.buildStatus)"'
```

预期 `cpuCount=2 memoryMB=2048 diskSizeMB=940 buildStatus=ready`。
全量 checkpoint 的代价约等于整份 guest 内存，增量只跟本代脏页量有关；换一个内存更大的模板，全量那一列等比变大、
增量各档基本不动，所以两组数字只能并列，不能相减。
