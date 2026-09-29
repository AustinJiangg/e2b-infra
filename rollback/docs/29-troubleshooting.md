# 29 · 排障

## 本章目标

读完本章，你应该能：

1. 从看到的症状找到对应条目，按"看哪里 → 怎么处置"一步步做；
2. 用统一的只读命令从当前 nomad alloc 的日志里取出相关的行；
3. 分清哪些症状要运维动手、哪些要调用方处理、哪些是已知项不必处理；
4. 知道每条症状背后的原理在哪一章，需要时回去读。

上一章（[28](28-observability-reference.md)）讲了计时键与日志，再往前一章（[27](27-configuration-and-capacity.md)）讲了开关；本章把它们用在出问题的时候：按看到的症状找到对应的一条，照"看哪里 → 怎么处置"做，每条最后给出讲原理的章。
计时键的定义在 [28](28-observability-reference.md)，开关在 [27](27-configuration-and-capacity.md)，错误码与 SDK 异常在 [25](25-errors-timeouts-concurrency.md)。下一章（[30](30-acceptance-runbook.md)）是上机验收。

下文命令里的日志取法统一为（只读）：

```bash
p=$(pidof -s template-manager)
A=$(tr '\0' '\n' < /proc/$p/environ | sed -n 's/^NOMAD_ALLOC_DIR=//p')
ls -1v "$A"/logs/start.stdout.* | xargs grep -ah 'restored checkpoint' | tail -5
```

把最后一行的关键字换成各条里给的即可。日志会轮转，出事后尽快取。

---

## 1. 增量退化成全量

**症状**：checkpoint 的 `mem_mode` 除第一个外也是 `full`；checkpoint 明显变慢、每次占盘约一份 guest 内存；验收脚本报 `有增量档被服务端报成 full`。

**看哪里**
- 能力文件或能力行的 `track_dirty_pages` 与 `track_dirty_pages_reason`（取法见 [26 §4.3](26-deployment-prerequisites.md#43-orchestrator-自报的能力)）。
- 关键字 `dirty page tracking is off`、`is not a boolean and was ignored`、`incremental chain is broken`。
- 进程环境：`tr '\0' '\n' < /proc/$(pidof -s template-manager)/environ | grep FC_TRACK_DIRTY_PAGES`。

**怎么处置**

| 看到的 | 原因 | 处置 |
|---|---|---|
| 950 上 reason 是 `no hardware dirty state tracking…` | HDBSS 没探到 | 按 [26 §2](26-deployment-prerequisites.md#2-平台前提) 第 3、4 项查 VHE、`CONFIG_ARM64_HDBSS`、CPU 能力；**不要**设 `FC_TRACK_DIRTY_PAGES=false` |
| 没有 HDBSS 的机器，reason 同上 | 默认关 | 需要增量就设 `FC_TRACK_DIRTY_PAGES=true`（[27 §5](27-configuration-and-capacity.md#5-改一个开关操作步骤)） |
| reason 以 `FC_TRACK_DIRTY_PAGES="yes" is not a boolean…` 开头 | 值写成了 `yes` / `on` 等 | 改成 `true` 或 `1` |
| 只有个别 `full`，前面有 `incremental chain is broken` | 某次失败的 checkpoint 丢了一段脏页记录，下一次以全量起新树 | 属设计行为；查它之前的 `checkpoint create failed` 找失败原因 |

另一种相近的症状：950 上 `mem_mode` 是增量，但 checkpoint 比预期慢、`checkpoint_verify.py` 打印的脏页后端是 KVM 写保护——这是 HDBSS 启用失败、Firecracker 退回了 `kvm-wp`。
看 `dmesg | grep 'Enable HDBSS success'` 有没有输出；想让这种情况直接报错而不是静默变慢，设 `FC_HDBSS_REQUIRED=true`（[27 §2.1](27-configuration-and-capacity.md#21-脏页跟踪与-hdbss)）。

原理：[05](05-dirty-page-tracking-and-hdbss.md)。

---

## 2. checkpoint 被拒（507 / 429）

**看哪里**：WARN `refused a checkpoint operation`，字段 `reason` 说是哪道闸，其余字段是判定用的数字（[28 §3.5](28-observability-reference.md#35-日志行上-timings-之外的字段)）。

| `reason` | 状态码 | 谁能解决 | 处置 |
|---|---|---|---|
| `disk_full` | 507 | **运维**。checkpoint 和 restore 都会被拒 | 先 `df -h /orchestrator/build`，再找大户：`du -sh /orchestrator/build/checkpoints/* \| sort -h \| tail`；让占用大的用户删旧 checkpoint 或销毁不用的沙箱（checkpoint 随沙箱销毁删除）。确认能力行 `compact` 为 `true`、`track_dirty_pages` 为 `true`（否则隐藏条目或全量层会一直累积，[27 §4.5](27-configuration-and-capacity.md#45-合并让保留最新-n-个收敛)）。不要把 `CHECKPOINT_MIN_FREE_BYTES` 设成 `0` 来"解决"：盘真写满时受影响的是全节点 |
| `too_many_checkpoints` | 429 | 调用方 | `list` → `delete` 不需要的。确实需要更多就调大 `CHECKPOINT_MAX_PER_SANDBOX` |
| `checkpoint_bytes_limit` | 429 | 调用方，前提是上限合理 | 删**最旧的**，或 restore 到较早的点再删掉之后那一支；删基准（日志的 `base_checkpoint_id`）几乎不释放。若 `max_checkpoint_bytes_per_sandbox` 小于 `floor_bytes` 加开销，或日志里有 ERROR `the checkpoint byte limit is below this sandbox's guest memory plus overhead`，删什么都回不来，只能调大上限（下限推导见 [27 §4.4](27-configuration-and-capacity.md#44-字节上限怎么选)） |

原理：[27 §4](27-configuration-and-capacity.md#4-容量规划)、[24 §4](24-semantics-and-limits.md#4-删除与空间回收用户视角)。错误本身的定义（状态码、SDK 异常类、调用方该做什么）只在 [25 §2](25-errors-timeouts-concurrency.md#2-错误总表)。

---

## 3. 撕裂（torn）与虚机 faulted

**症状**：restore 返回 500 `data_loss` / `torn`（SDK `CheckpointTornException`）；此后该沙箱的每次 checkpoint 与 restore 都回 `torn`，`list` / `delete` 仍可用。

**看哪里**
- ERROR `restore failed past the commit point; the sandbox is torn and must be recreated`，以及同一沙箱的 WARN `checkpoint restore failed` 里的 `timings_ms`：分段停在哪一步。
- 分段里有 `fc_rollback` 且约等于 `CHECKPOINT_FC_CALL_TIMEOUT`：回滚调用超时，按撕裂处理；有 `fc_rollback` 而没有 `fc_total`：Firecracker 在提交点之后报错（虚机已标 faulted）；有 `fc_total` 而没有 `resume`：多半是 `reset_view` 失败（磁盘一侧，先查产物盘）。
- 能力文件或能力行的 `fault_inject` 是否为空（`torn_assemble` 注入会故意造成撕裂）。

**怎么处置**：**销毁并重建这个沙箱**，没有修复手段；它的 checkpoint 随之删除。持续出现时留下上面的日志、`last-restore-timings.json`，并确认 FC 与 orchestrator 成对（[26 §3](26-deployment-prerequisites.md#3-版本配对)）、`CHECKPOINT_FC_CALL_TIMEOUT` 没被调小。

原理：[12](12-failure-semantics.md)、[09](09-in-place-rollback.md)。

---

## 4. guest 失联（guest_unresponsive）

**症状**：restore 约 45 s 后返回 500 `internal` / `guest_unresponsive`。宿主一侧的回滚**已经完成**，不是撕裂。

**看哪里**
- ERROR `rollback succeeded but the guest's envd never answered`，带完整 `timings_ms`。
- 同一行附近的 `restored checkpoint` 是否为 WARN（`fc_vcpu_mmio_drain_failed` 或 `fc_vcpu_readback_mismatch` 非零）。
- 能力文件或能力行的 `fault_inject` 是否含 `envd_timeout`。
- 宿主是否过载：netns 数（§9）、`uptime`。

**怎么处置**：由业务决定稍后重试命令，或销毁重建。反复出现时核对 Firecracker sha（回滚时清理串口中断线的修复在交付 FC 里），并收集上述日志。

原理：[12](12-failure-semantics.md)、[10](10-rollback-pitfalls.md)。

---

## 5. restore 后连接挂死或流截断

先分清是哪一种：

| 现象 | 原因 | 处置 |
|---|---|---|
| restore **之前**建立的 TCP 连接在 restore 之后挂住或被 RST | **预期行为**：restore 作废该沙箱全部跨越回滚的连接（guest 的 TCP 状态回到了过去，宿主也清了 conntrack 与代理连接池） | 客户端重连。restore 之后新建的连接不受影响 |
| restore 发生时，同一沙箱上**正在进行的**流式调用报 `incomplete chunked read` / `RemoteProtocolError`；unary 调用收到 409 `sandbox_restored` | **既定限制**：响应头已发出的流只能被切断 | 调用方把"流式调用中途的传输层错误 + 同时发生过 restore"当作被回滚打断，按需重发 |
| **没有任何 restore**，流式命令偶发截断；orchestrator 或 client-proxy 日志有 WARN `httputil: ReverseProxy read error during body copy … use of closed network connection` | 代理未开 HTTP/1 full duplex 的老问题，已修 | 确认 orchestrator 与 client-proxy **两个**都是修复后的版本：client-proxy 镜像（`docker images \| grep client-proxy` 的创建时间）应与 RPM 同一次构建（`rpm -qi e2b-infra \| grep 'Build Date'`） |
| restore 之后 guest 里的命令卡几秒 | 宿主侧 netns 泄漏导致网络路径变慢（§9），或 guest 侧回滚后的中断状态 | 先数 netns，再核对 FC sha |

**取证**：设 `PROXY_TRACE=1` 重启后复现，按 [28 §7](28-observability-reference.md#7-proxy_trace) 区分两类截断，取证完就关。

原理：[25 §5](25-errors-timeouts-concurrency.md#5-多个调用方与并发)、[10](10-rollback-pitfalls.md)。

---

## 6. 空闲后第一次 restore 慢

**症状**：沙箱空闲几秒之后，**不先 checkpoint 直接 restore**，比平时慢得多；空闲之后的第一次 checkpoint 也偏慢。数据正确性不受影响。

**看哪里**：那次 restore 的 `fc_quiesce`（平时约为 0）与 `frozen`；对照先 checkpoint 再 restore 的同样场景。

**怎么处置**：已知项，根因未查，当前不处理。只要 `fc_quiesce` 是大头，就是这一条，不必再往别处找。数字见 [21 §4.2](21-benchmarks-and-compliance.md#42-空闲后-restore)。

---

## 7. restore 随运行时间变慢

**症状**：长期存活、反复 checkpoint / restore 的沙箱，restore 在几小时里缓慢变慢。

**看哪里**：按时间比较这些键：

| 键 | 如果它在涨 | 说明 |
|---|---|---|
| `disk_view_read`、`assemble_view_pre` | 当前代码下应一直在毫秒以内 | guest 里 ext4 每次重写小文件都分配新块，视图里"guest 写过的块"只增不减，上限约为 guest 能写到的块数（设备的六成多）；装配视图已改为按块号位图，每块代价很小。若 `assemble_view_pre` 随块数明显增长，或日志里根本没有 `disk_view_read` 这个键，说明跑的是老版本 |
| `materialize_read_mb`、`revert_mem_write`、`fc_memory` | 回滚量变大 | guest 后台写让每次要回滚的页真实变多，是工作量，不是缺陷 |
| `conntrack_bg`、`conntrack_host_entries_count` | 宿主连接表变大 | 见 §10 |

**怎么处置**：确认版本；必要时让业务定期重建长寿沙箱。原理：[07 §6](07-disk-layering.md#6-读路径与视图装配)。

---

## 8. GC 长停顿与 memory cgroup 回收

**症状**：多个沙箱的 `frozen` 在同一时刻一起变长，而各分段都解释不了多出来的部分。

**看哪里**
- 带 `CHECKPOINT_RUNTIME_METRICS=1` 与 `GODEBUG=gctrace=1` 重启后复现（会带走全部沙箱，[27 §5](27-configuration-and-capacity.md#5-改一个开关操作步骤)），读法见 [28 §6](28-observability-reference.md#6-checkpoint_runtime_metrics-输出)：长停顿里 `stw_stopping` 占满 `stw_total` 就是这一条。
- orchestrator 所在 memory cgroup 的回收压力（cgroup v1）：

```bash
p=$(pidof -s template-manager)
d=$(awk -F: '$2=="memory"{print $3}' /proc/$p/cgroup)
cat /sys/fs/cgroup/memory$d/memory.limit_in_bytes /sys/fs/cgroup/memory$d/memory.usage_in_bytes /sys/fs/cgroup/memory$d/memory.failcnt
ps -L -o stat=,wchan:32= -p $p | awk '$1 ~ /D/' | sort | uniq -c
```

`failcnt` 持续上涨、有线程停在 `reclaim_throttle`，就是 goroutine 在缺页里撞上了 cgroup 回收，STW 只能等它。

**怎么处置**：当前代码在拷贝前用 `MADV_POPULATE_READ/WRITE` 把缺页挪进系统调用（要求宿主内核 ≥ 5.14，交付内核满足），常规负载下不应再出现。仍出现时按 [`deploy-docs/12-orchestrator资源配额调优.md`](../../deploy-docs/12-orchestrator资源配额调优.md) 核对 job 的内存上限与页缓存；取证开关用完即关。

原理：[13 §8](13-state-concurrency-durability.md#8-节点级效应)。

---

## 9. 网络槽位（netns）泄漏

**症状**：netns 数远超"300 + 历史峰值并发"；新沙箱起得慢；重启后很久才就绪；性能测试长尾变多。

**看哪里**

```bash
ip netns list | grep -cE '^ns-[0-9]+$'
ip -o link | wc -l
iptables -t nat -S | wc -l
```

**原因**：orchestrator **每重启一次就漏掉约一整池（约 300 个）槽位**，连同 veth 与 iptables 规则；新进程既不回收也不复用它们。这是 ARM 适配所基于的上游 `2026.09` 的问题，上游在 `2026.27` 补上了开机回收，本项目不单独移植，随基线升级解决。

**怎么处置**（RPM 交付态）：

```bash
bash /opt/e2b-infra/build.sh --recycle-netns
```

它优雅停 `template-manager-system` → 清 netns / veth / iptables（只匹配 `ns-<n>`、`veth-<n>` 和 `10.11.x.y/32` 的 MASQUERADE，跳过仍有进程的槽位）→ 再拉起。**会带走全部沙箱与 checkpoint**。
跑的是自建二进制时不要直接用它（它按 RPM 的样子把 job 拉回来），只取清理那一步，见 [`deploy-docs/06-日常运维手册.md`](../../deploy-docs/06-日常运维手册.md) §8.1。
清完之后等 netns 数连续几分钟不变再做性能测试。

---

## 10. conntrack 占了冻结窗口

**症状**：restore 的 `frozen` 里 `conntrack`（冻结窗口里等清理结束的时间）明显非零。

**看哪里**：`conntrack_bg` 是清理自身时长，拆成 `conntrack_ns`（沙箱 netns 表）与 `conntrack_host_queue` + `conntrack_host_sweep`（宿主表），`conntrack_host_entries_count` 是宿主表大小；
宿主表当前条目数 `cat /proc/sys/net/netfilter/nf_conntrack_count`。

| 哪部分大 | 原因 | 处置 |
|---|---|---|
| `conntrack_host_sweep` 随 `conntrack_host_entries_count` 涨 | 宿主连接表大（同机其他负载的连接也在里面），整表扫描约每条 1 µs | 减少宿主上的无关连接；这是已知的下一个优化点，没有开关 |
| `conntrack_host_queue` 大 | restore 频率高，排在别人的清扫后面 | 同上 |
| `conntrack_ns` 大 | guest 自己的连接多 | 业务侧 |

原理：[10 §4](10-rollback-pitfalls.md#4-连接跟踪)。

---

## 11. Firecracker 版本目录错配

**症状**：换了 Firecracker，行为没变；或 checkpoint / restore 报 500，消息含 `save-dirty-bitmap`、`does not support in-place rollback`。

**看哪里**

```bash
ls /fc-versions
sha256sum /opt/e2b-infra/bin/firecracker /fc-versions/*/firecracker
for p in $(pidof firecracker); do readlink /proc/$p/exe; done | sort | uniq -c
tr '\0' '\n' < /proc/$(pidof -s template-manager)/environ | grep FIRECRACKER_VERSIONS_DIR
```

**怎么处置**：沙箱用的是 `$FIRECRACKER_VERSIONS_DIR/<模板记录的版本>/firecracker`，交付模板记的是 `v1.13.1`，所以要换的就是 `/fc-versions/v1.13.1/firecracker`（里面的 FC 自报 `v1.12.1`，这是正常的）。
`FIRECRACKER_VERSIONS_DIR` 不应出现在交付态的环境里。换完只对之后新起的沙箱生效。原理：[26 §3](26-deployment-prerequisites.md#3-版本配对)、[08](08-firecracker-api-contract.md)。

---

## 12. SDK 自检失败

**症状**：`install.py --check` 失败（`run.sh smoke` 第 2 项 FAIL）；`sb.checkpoint` 不存在；验收只跑 57 项；调用报 404 `unimplemented`。

**看哪里**

```bash
python3 -c 'import sys, e2b; print(sys.executable, e2b.__file__)'
python3 -m pip show e2b | head -2
python3 /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check
```

| 原因 | 处置 |
|---|---|
| 用的不是 `build.sh -i` 装 SDK 的那个解释器 | 换到那个环境再查（[30 §1](30-acceptance-runbook.md#1-跑之前)） |
| `e2b` 不是 `2.20.0` | 覆盖层版本锁死，会拒绝安装；装 `e2b==2.20.0` |
| 覆盖层与 `patch_e2b.py` 的先后顺序反了，或 `e2b` 包被重装过、覆盖层随之丢失 | 重装：`python3 /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py` 之后 `python3 /opt/e2b-infra/patch_e2b.py`，**顺序不能反** |
| `install.py` 是旧版，要求"恰好 8 个异常子类" | 现在的 SDK 有 9 个子类（新增 `CheckpointBytesLimitException`）；换用当前部署件里的 `install.py`，它的判据是"原有 8 个都在、映射表里全是子类" |

原理：[23](23-quickstart.md)、[25 §3](25-errors-timeouts-concurrency.md#3-异常类层次)。

---

## 13. 其他错误的运维视角

下表只写运维一侧的动作；各错误的定义见 [25 §2](25-errors-timeouts-concurrency.md#2-错误总表)。

| `reason` | 运维要做的 |
|---|---|
| `busy`（503） | 同一沙箱上一个操作还没完。若频繁出现，看该沙箱前一个操作的 `total` 是否异常长（例如全量 checkpoint、`wait_envd` 超时） |
| `chain_broken`（412） | 让调用方先做一次 checkpoint 再 restore；查之前的 `checkpoint create failed` |
| `rootfs_poisoned`（500） | 让调用方 restore 一次即恢复；查 ERROR `sealing the write layer failed` 或 `failed to reset rootfs bookkeeping after restore` 的原因（多为磁盘） |
| `internal`（500） | 看同一时刻的 `checkpoint create failed` / `checkpoint restore failed` 行里的错误文本与分段 |

---

## 本章要点

- 先取证再处置：日志会轮转，出事后尽快用统一的只读命令取相关行；`last-restore-timings.json` 每次 restore 都覆盖，要紧接着读。
- 除第一个外 `mem_mode` 仍是 `full`：先看能力文件或能力行的 `track_dirty_pages` 与理由；没有 HDBSS 的机器要增量就显式开，950 上不要设 `FC_TRACK_DIRTY_PAGES=false`。
- 507 / 429 看 `refused a checkpoint operation` 行的 `reason`：产物盘满是运维的事，不要把水位闸设成 0 来"解决"；字节上限下让调用方删最旧的。
- 撕裂没有修复手段，只能销毁重建；guest 失联时宿主一侧的回滚已经完成，由业务决定稍后重试还是重建。
- 空闲后第一次 restore 慢是已知项；restore 随运行时间缓慢变慢先确认版本，必要时定期重建长寿沙箱；GC 长停顿在当前代码的常规负载下不应再出现。
- 每重启一次 orchestrator 会漏约一池网络槽位，按条目判断后清理；换 Firecracker 只换 `/fc-versions/v1.13.1/firecracker` 这一个文件。
- SDK 自检失败、验收只剩 57 项，说明覆盖层没装进这个解释器或版本旧。
