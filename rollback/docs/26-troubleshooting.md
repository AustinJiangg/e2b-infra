# 26 · 按症状排障

每个条目按症状、观察位置和处置方式组织。涉及内部 Nomad/RPM 路径的命令说明其环境归属；客户应换用实际服务管理方式和路径。

## 1. 增量退化成全量

**症状**：checkpoint 的 `mem_mode` 除第一个外也是 `full`；checkpoint 明显变慢、每次占盘约一份 guest 内存；验收脚本报 `有增量档被服务端报成 full`。

**看哪里**
- 能力文件或能力行的 `track_dirty_pages` 与 `track_dirty_pages_reason`（取法见 [第 23 章 §6](23-build-and-self-check.md#6-orchestrator-自报的能力)）。
- 关键字 `dirty page tracking is off`、`is not a boolean and was ignored`、`incremental chain is broken`。
- 进程环境：`tr '\0' '\n' < /proc/$(pidof -s template-manager)/environ | grep FC_TRACK_DIRTY_PAGES`。

**怎么处置**

| 看到的 | 原因 | 处置 |
|---|---|---|
| 950 上 reason 是 `no hardware dirty state tracking…` | HDBSS 没探到 | 按 [第 23 章 §3](23-build-and-self-check.md#3-平台前提) 第 3、4 项查 VHE、`CONFIG_ARM64_HDBSS`、CPU 能力；**不要**设 `FC_TRACK_DIRTY_PAGES=false` |
| 没有 HDBSS 的机器，reason 同上 | 默认关 | 需要增量就设 `FC_TRACK_DIRTY_PAGES=true`（[第 24 章 §5](24-configuration-and-capacity.md#5-改一个开关操作步骤)） |
| reason 以 `FC_TRACK_DIRTY_PAGES="yes" is not a boolean…` 开头 | 值写成了 `yes` / `on` 等 | 改成 `true` 或 `1` |
| 只有个别 `full`，前面有 `incremental chain is broken` | 某次失败的 checkpoint 丢了一段脏页记录，下一次以全量起新树 | 属设计行为；查它之前的 `checkpoint create failed` 找失败原因 |

另一种相近的症状：950 上 `mem_mode` 是增量，但 checkpoint 比预期慢、`checkpoint_verify.py` 打印的脏页后端是 KVM 写保护——这是 HDBSS 启用失败、Firecracker 退回了 `kvm-wp`。
看 `dmesg | grep 'Enable HDBSS success'` 有没有输出；想让这种情况直接报错而不是静默变慢，设 `FC_HDBSS_REQUIRED=true`（[第 24 章 §2.1](24-configuration-and-capacity.md#21-脏页跟踪与-hdbss)）。

原理：[第 5 章](05-dirty-tracking-and-hdbss.md)。

## 2. checkpoint 被拒（507 / 429）

**看哪里**：WARN `refused a checkpoint operation`，字段 `reason` 说是哪道闸，其余字段是判定用的数字（[第 25 章 §3.5](25-observability.md#35-日志行上-timings-之外的字段)）。

| `reason` | 状态码 | 谁能解决 | 处置 |
|---|---|---|---|
| `disk_full` | 507 | **运维**。checkpoint 和 restore 都会被拒 | 先 `df -h /orchestrator/build`，再找大户：`du -sh /orchestrator/build/checkpoints/* \| sort -h \| tail`；让占用大的用户删旧 checkpoint 或销毁不用的沙箱（checkpoint 随沙箱销毁删除）。确认能力行 `compact` 为 `true`、`track_dirty_pages` 为 `true`（否则隐藏条目或全量层会一直累积，[第 24 章 §4.5](24-configuration-and-capacity.md#45-合并让保留最新-n-个收敛)）。不要把 `CHECKPOINT_MIN_FREE_BYTES` 设成 `0` 来"解决"：盘真写满时受影响的是全节点 |
| `too_many_checkpoints` | 429 | 调用方 | `list` → `delete` 不需要的。确实需要更多就调大 `CHECKPOINT_MAX_PER_SANDBOX` |
| `checkpoint_bytes_limit` | 429 | 调用方，前提是上限合理 | 删**最旧的**，或 restore 到较早的点再删掉之后那一支；删基准（日志的 `base_checkpoint_id`）几乎不释放。若 `max_checkpoint_bytes_per_sandbox` 小于 `floor_bytes` 加开销，或日志里有 ERROR `the checkpoint byte limit is below this sandbox's guest memory plus overhead`，删什么都回不来，只能调大上限（下限推导见 [第 24 章 §4.4](24-configuration-and-capacity.md#44-字节上限怎么选)） |

原理：[第 24 章 §4](24-configuration-and-capacity.md#4-容量规划)、[第 22 章 §4](22-api-semantics.md#4-删除与空间回收用户视角)。错误本身的定义（状态码、SDK 异常类、调用方该做什么）只在 [第 22 章 §8](22-api-semantics.md#8-错误总表)。

## 3. 撕裂（torn）与虚机 faulted

**症状**：restore 返回 500 `data_loss` / `torn`（SDK `CheckpointTornException`）；此后该沙箱的每次 checkpoint 与 restore 都回 `torn`，`list` / `delete` 仍可用。

**看哪里**
- ERROR `restore failed past the commit point; the sandbox is torn and must be recreated`，以及同一沙箱的 WARN `checkpoint restore failed` 里的 `timings_ms`：分段停在哪一步。
- 分段里有 `fc_rollback` 且约等于 `CHECKPOINT_FC_CALL_TIMEOUT`：回滚调用超时，按撕裂处理；有 `fc_rollback` 而没有 `fc_total`：Firecracker 在提交点之后报错（虚机已标 faulted）；有 `fc_total` 而没有 `resume`：多半是 `reset_view` 失败（磁盘一侧，先查产物盘）。
- 能力文件或能力行的 `fault_inject` 是否为空（`torn_assemble` 注入会故意造成撕裂）。

**怎么处置**：**销毁并重建这个沙箱**，没有修复手段；它的 checkpoint 随之删除。持续出现时留下上面的日志、`last-restore-timings.json`，并确认 FC 与 orchestrator 成对（[第 23 章 §4](23-build-and-self-check.md#4-版本配对)）、`CHECKPOINT_FC_CALL_TIMEOUT` 没被调小。

原理：[第 11 章](11-failures-and-invariants.md)、[第 9 章](09-in-place-rollback.md)。

## 4. guest 失联（guest_unresponsive）

**症状**：restore 约 45 s 后返回 500 `internal` / `guest_unresponsive`。宿主一侧的回滚**已经完成**，不是撕裂。

**看哪里**
- ERROR `rollback succeeded but the guest's envd never answered`，带完整 `timings_ms`。
- 同一行附近的 `restored checkpoint` 是否为 WARN（`fc_vcpu_mmio_drain_failed` 或 `fc_vcpu_readback_mismatch` 非零）。
- 能力文件或能力行的 `fault_inject` 是否含 `envd_timeout`。
- 宿主是否过载：netns 数（[§9](#9-网络槽位netns泄漏)）、`uptime`。

**怎么处置**：由业务决定稍后重试命令，或销毁重建。反复出现时核对 Firecracker sha（回滚时清理串口中断线的修复在交付 FC 里），并收集上述日志。

原理：[第 11 章](11-failures-and-invariants.md)、[第 10 章](10-device-network-time.md)。

## 5. restore 后连接挂死或流截断

先分清是哪一种：

| 现象 | 原因 | 处置 |
|---|---|---|
| restore **之前**建立的 TCP 连接在 restore 之后挂住或被 RST | **预期行为**：restore 作废该沙箱全部跨越回滚的连接（guest 的 TCP 状态回到了过去，宿主也清了 conntrack 与代理连接池） | 客户端重连。restore 之后新建的连接不受影响 |
| restore 发生时，同一沙箱上**正在进行的**流式调用报 `incomplete chunked read` / `RemoteProtocolError`；unary 调用收到 409 `sandbox_restored` | **既定限制**：响应头已发出的流只能被切断 | 调用方把"流式调用中途的传输层错误 + 同时发生过 restore"当作被回滚打断，按需重发 |
| **没有任何 restore**，流式命令偶发截断；orchestrator 或 client-proxy 日志有 WARN `httputil: ReverseProxy read error during body copy … use of closed network connection` | 代理未开 HTTP/1 full duplex 的老问题，已修 | 确认 orchestrator 与 client-proxy **两个**都是修复后的版本：client-proxy 镜像（`docker images \| grep client-proxy` 的创建时间）应与 RPM 同一次构建（`rpm -qi e2b-infra \| grep 'Build Date'`） |
| restore 之后 guest 里的命令卡几秒 | 宿主侧 netns 泄漏导致网络路径变慢（[§9](#9-网络槽位netns泄漏)），或 guest 侧回滚后的中断状态 | 先数 netns，再核对 FC sha |

**取证**：设 `PROXY_TRACE=1` 重启后复现，按 [第 25 章 §7](25-observability.md#7-proxy_trace) 区分两类截断，取证完就关。

原理：[第 22 章 §11](22-api-semantics.md#11-多个调用方与并发)、[第 10 章](10-device-network-time.md)。

## 6. 空闲后第一次 restore 慢

**症状**：沙箱空闲几秒之后，**不先 checkpoint 直接 restore**，比平时慢得多；空闲之后的第一次 checkpoint 也偏慢。数据正确性不受影响。

**看哪里**：那次 restore 的 `fc_quiesce`（平时约为 0）与 `frozen`；对照先 checkpoint 再 restore 的同样场景。

**怎么处置**：已知项，根因未查，当前不处理。只要 `fc_quiesce` 是大头，就是这一条，不必再往别处找。数字见 [附录 B §7.2](B-920b-results.md#72-空闲后-restore)。

## 7. restore 随运行时间变慢

**症状**：长期存活、反复 checkpoint / restore 的沙箱，restore 在几小时里缓慢变慢。

**看哪里**：按时间比较这些键：

| 键 | 如果它在涨 | 说明 |
|---|---|---|
| `disk_view_read`、`assemble_view_pre` | 视图块数、侧车读取与装配成本增加 | 位图装配降低每块处理成本，但耗时仍受视图覆盖、缓存和资源竞争影响。历史测量中的毫秒量级不构成所有负载的上界；比较时间序列和实际块数。版本用运行路径、哈希与源码记录核对，不凭耗时或缺少单个计时键直接判定 |
| `materialize_read_mb`、`revert_mem_write`、`fc_memory` | 回滚量变大 | guest 后台写让每次要回滚的页真实变多，是工作量，不是缺陷 |
| `conntrack_bg`、`conntrack_host_entries_count` | 宿主连接表变大 | 见 [§10](#10-conntrack-占了冻结窗口)、[§14](#14-restore-随宿主上其他流量变慢) |

**怎么处置**：确认版本；必要时让业务定期重建长寿沙箱。原理：[第 7 章 §7](07-disk-layering.md#7-实现细节读取索引装配成本与落盘)。

## 8. GC 长停顿与 memory cgroup 回收

**症状**：多个沙箱的 `frozen` 在同一时刻一起变长，而各分段都解释不了多出来的部分。

**看哪里**
- 带 `CHECKPOINT_RUNTIME_METRICS=1` 与 `GODEBUG=gctrace=1` 重启后复现（会带走全部沙箱，[第 24 章 §5](24-configuration-and-capacity.md#5-改一个开关操作步骤)），读法见 [第 25 章 §6](25-observability.md#6-checkpoint_runtime_metrics-输出)：长停顿里 `stw_stopping` 占满 `stw_total` 就是这一条。
- orchestrator 所在 memory cgroup 的回收压力（cgroup v1）：

```bash
p=$(pidof -s template-manager)
d=$(awk -F: '$2=="memory"{print $3}' /proc/$p/cgroup)
cat /sys/fs/cgroup/memory$d/memory.limit_in_bytes /sys/fs/cgroup/memory$d/memory.usage_in_bytes /sys/fs/cgroup/memory$d/memory.failcnt
ps -L -o stat=,wchan:32= -p $p | awk '$1 ~ /D/' | sort | uniq -c
```

`failcnt` 持续上涨、有线程停在 `reclaim_throttle`，就是 goroutine 在缺页里撞上了 cgroup 回收，STW 只能等它。

**怎么处置**：当前代码在拷贝前用 `MADV_POPULATE_READ/WRITE` 把缺页挪进系统调用（要求宿主内核 ≥ 5.14，交付内核满足），常规负载下不应再出现。仍出现时按 [`deploy-docs/12-orchestrator资源配额调优.md`](../../deploy-docs/12-orchestrator资源配额调优.md) 核对 job 的内存上限与页缓存；取证开关用完即关。

原理：[第 12 章 §8](12-state-and-lifecycle.md#8-节点级效应)。

## 9. 网络槽位（netns）泄漏

**症状**：netns 数远超"300 + 历史峰值并发"；新沙箱起得慢；重启后很久才就绪；性能测试长尾变多。

**看哪里**

```bash
ip netns list | grep -cE '^ns-[0-9]+$'
ip -o link | wc -l
iptables -t nat -S | wc -l
```

**原因**：orchestrator **每重启一次就漏掉约一整池（约 300 个）槽位**，连同 veth 与 iptables 规则；新进程既不回收也不复用它们。这是 ARM 适配所基于的上游 `2026.09` 的问题，上游在 `2026.27` 补上了开机回收，本项目不单独移植，随基线升级解决。

**怎么处置**（内部RPM部署）：

```bash
bash /opt/e2b-infra/build.sh --recycle-netns
```

它优雅停 `template-manager-system` → 清 netns / veth / iptables（只匹配 `ns-<n>`、`veth-<n>` 和 `10.11.x.y/32` 的 MASQUERADE，跳过仍有进程的槽位）→ 再拉起。**会带走全部沙箱与 checkpoint**。
跑的是自建二进制时不要直接用它（它按 RPM 的样子把 job 拉回来），只取清理那一步，见 [`deploy-docs/06-日常运维手册.md`](../../deploy-docs/06-日常运维手册.md) §8.1。
清完之后等 netns 数连续几分钟不变再做性能测试。

## 10. conntrack 占了冻结窗口

**症状**：restore 的 `frozen` 里 `conntrack`（冻结窗口里等清理结束的时间）明显非零。

**看哪里**：`conntrack_bg` 是清理自身时长，拆成 `conntrack_ns`（沙箱 netns 表）与 `conntrack_host_queue` + `conntrack_host_sweep`（宿主表），`conntrack_host_entries_count` 是宿主表大小；
宿主表当前条目数 `cat /proc/sys/net/netfilter/nf_conntrack_count`。

| 哪部分大 | 原因 | 处置 |
|---|---|---|
| `conntrack_host_sweep` 随 `conntrack_host_entries_count` 涨 | 宿主连接表大（同机其他负载的连接也在里面），整表扫描约每条 1 µs | 减少宿主上的无关连接；这是已知的下一个优化点，没有开关。先看表里是什么，见 [§14](#14-restore-随宿主上其他流量变慢) |
| `conntrack_host_queue` 大 | restore 频率高，排在别人的清扫后面 | 同上 |
| `conntrack_ns` 大 | guest 自己的连接多 | 业务侧 |

原理：[第 10 章 §4](10-device-network-time.md#4-连接跟踪)。

## 11. Firecracker 版本目录错配

**症状**：换了 Firecracker，行为没变；或 checkpoint / restore 报 500，消息含 `save-dirty-bitmap`、`does not support in-place rollback`。

**看哪里**

```bash
ls /fc-versions
sha256sum /opt/e2b-infra/bin/firecracker /fc-versions/*/firecracker
for p in $(pidof firecracker); do readlink /proc/$p/exe; done | sort | uniq -c
tr '\0' '\n' < /proc/$(pidof -s template-manager)/environ | grep FIRECRACKER_VERSIONS_DIR
```

**怎么处置**：核对客户当前模板记录的FC版本、`FIRECRACKER_VERSIONS_DIR`和运行中`/proc/<pid>/exe`，替换模板实际选择的构建产物。该变量可按部署需要配置；修改后仅对新创建的FC进程生效。

内部测试中模板查找键是`v1.13.1`，对应FC自报上游基线`v1.12.1`，这只是该环境的例子。不能据此要求所有客户使用同一目录，也不能只看版本字符串判断功能身份。原理见[第 23 章 §4](23-build-and-self-check.md#4-版本配对)。

## 12. SDK 自检失败

**症状**：业务解释器没有`sb.checkpoint`，协议桩冲突、接口报`unimplemented`，或者旧验收工具缺少增量判据。

```bash
python3 -c 'import sys, e2b; print(sys.executable, e2b.__file__)'
python3 -c 'from e2b import Sandbox, CheckpointInfo; print(Sandbox.checkpoint)'
```

核对业务实际使用的解释器、KASandbox中配套SDK实现、返回字段和服务端版本。不要用系统解释器的安装状态代替业务环境。

只有采用内部覆盖层部署时，才额外检查`e2b==2.20.0`版本锁、覆盖层与`patch_e2b.py`的顺序及`install.py --check`；相关记录见[附录 E §4](E-environment-and-tools.md#4-sdk-覆盖层)。客户直接构建配套SDK时不以是否安装这层内部工具判断功能是否存在。

异常与协议语义见[第 22 章](22-api-semantics.md)。

## 13. 其他错误的运维视角

下表只写运维一侧的动作；各错误的定义见 [第 22 章 §8](22-api-semantics.md#8-错误总表)。

| `reason` | 运维要做的 |
|---|---|
| `busy`（503） | 同一沙箱上一个操作还没完。若频繁出现，看该沙箱前一个操作的 `total` 是否异常长（例如全量 checkpoint、`wait_envd` 超时） |
| `chain_broken`（412） | 让调用方先做一次 checkpoint 再 restore；查之前的 `checkpoint create failed` |
| `rootfs_poisoned`（500） | 让调用方 restore 一次即恢复；查 ERROR `sealing the write layer failed` 或 `failed to reset rootfs bookkeeping after restore` 的原因（多为磁盘） |
| `internal`（500） | 看同一时刻的 `checkpoint create failed` / `checkpoint restore failed` 行里的错误文本与分段 |

## 14. restore 随宿主上其他流量变慢

**症状**：沙箱这边的负载没变，restore 的 `total` 却跟着宿主上别的业务一起涨；同时 `conntrack_bg` 与 `conntrack_host_entries_count` 上涨，
`conntrack_host_sweep` 占了 `conntrack_bg` 的大头，冻结窗口里等清理的 `conntrack` 从接近 0 变成几毫秒。

**看哪里**：宿主表多大、里面都是什么（只读；表很大时这条 awk 本身要读一遍整表，别高频跑）：

```bash
cat /proc/sys/net/netfilter/nf_conntrack_count
awk '{p=$3; st=(p=="tcp")?$6:"-"; s=""; d=""; dp=""
  for(k=4;k<=NF;k++){ if(s==""&&$k~/^src=/)s=substr($k,5); else if(d==""&&$k~/^dst=/)d=substr($k,5); else if(dp==""&&$k~/^dport=/)dp=substr($k,7) }
  sb=(s~/^10\.1[12]\./||d~/^10\.1[12]\./)?"沙箱":"非沙箱"; a=($0~/ASSURED/)?"ASSURED":"-"
  c[sb" "p" "st" dport="dp" "a]++} END{for(x in c) print c[x], x}' /proc/net/nf_conntrack | sort -rn | head -10
```

`10.11.x.y` 是槽位地址、`10.12.x.y` 是 veth 地址（默认网段）。

**原因**：宿主侧清扫每批做一次整表 dump，代价约 4.6 ms + 1 µs / 条（实测更高），与**整张宿主表**成正比，不管表里是不是沙箱的连接。
920B 长跑里表的约 95% 与沙箱无关：宿主本机访问 lo:80 的短连接留下的 `TIME_WAIT`，和本机 DNS 查询；滚动负载跑到第二个小时，
本机 DNS 的 `ASSURED` 条目会堆积，把表从约 1.9 万推到约 2.4 万条，restore p50 随之多约 9 ms（堆积的机制是推断）。

**怎么处置**：不影响正确性。减少宿主上与沙箱无关的短连接和 DNS 查询量；当前没有开关。按 ct mark 让内核只交回沙箱条目的改法（方案 A）已设计，
用户 09-30 决定暂不实施。原理：[第 10 章 §4.4](10-device-network-time.md#44-宿主表怎么删每批一次整表扫描按代价模型选路)；数据与根因：[附录 C §8.6](C-evolution-and-cases.md#86-conntrack-清理占了冻结窗口)。

## 15. client-proxy 日志暴涨、guest 访问 hyperloop 不通

**症状**：有沙箱在跑时 client-proxy 日志暴涨，满是 WARN `invalid host` 且 `"host": "192.0.2.1"`（每条带多行调用栈）；
16 个沙箱的长跑里约每秒 900 条，两小时约 9.1 GB。guest 里访问 `http://192.0.2.1/`（hyperloop）拿不到 hyperloop 的应答。

**看哪里**（只读）：

```bash
iptables -t nat -S PREROUTING | head -5
c=$(docker ps -q --filter ancestor="$(docker ps --format '{{.Image}}' | grep -m1 '/client-proxy')")
docker logs --since 60s "$c" 2>&1 | grep -c 'invalid host'
```

第一条命令里如果不带 `-i` 的 `--dport 80 -j REDIRECT --to-ports 3002` 排在各槽位的 `-d 192.0.2.1/32 -i veth-<n> … --to-ports 5010` 前面，就是这一条。

**原因**：部署脚本装的 80 → 3002 转发不限入口网卡，排在 hyperloop 规则前面，guest 发往 hyperloop 的连接被转给了 client-proxy（[附录 C §13.1](C-evolution-and-cases.md#131-80-端口的转发规则劫持-hyperloop-请求)）。

**怎么处置**：checkpoint / restore 不受影响。用户 09-30 决定暂不改；要改时给该规则加 `! -i veth+` 并删掉旧规则，命令见 [附录 C §13.1](C-evolution-and-cases.md#131-80-端口的转发规则劫持-hyperloop-请求)。
日志量大时注意 client-proxy 所在盘的余量。
