# 25 · 排障

## 本章目标

读完本章，你应该能：

1. 从看到的症状（SDK 抛出的异常、错误信息里的一句话、沙箱的表现）找到对应条目，按"看哪里 → 怎么处置"做；
2. 用几条固定的命令在 guest 里取到 gsd 的健康状态、日志和状态文件，在宿主上取到 orchestrator 的日志和 guest 的串口输出；
3. 分清哪些症状调用方自己就能处理、哪些要运维动手、哪些是已知项不必处理；
4. 知道每条症状背后的原理在哪一章。

上一章（[24](24-configuration-and-observability.md)）讲了有哪些开关、计时键、日志和状态文件；本章把它们用在出问题的时候。每条按"症状 / 看哪里 / 怎么处置 / 原理"写，报错文本都是代码里的原文，可以直接搜索。
错误码与异常类的完整定义在 [12](12-failure-semantics.md)，已知限制的总表在 [15](15-known-limits.md)，本章不重复定义，只讲怎么查、怎么办。下一章（[26](26-acceptance-runbook.md)）是上机验收。

---

## 0. 通用取证办法

**在 guest 里执行命令。** gsd 的日志、状态文件和内核输出都在沙箱里，要以 root 身份看。从宿主上通过 SDK 连到出问题的沙箱：

```python
from e2b import Sandbox

sbx = Sandbox.connect("<沙箱 ID>")

def g(cmd):
    """以 root 在 guest 里执行；末尾的 || true 让没有匹配行的 grep 不抛异常。"""
    return sbx.commands.run(cmd + " || true", user="root", timeout=60).stdout

print(g("curl -s http://127.0.0.1:49984/health"))
print(g("journalctl -u gsd --no-pager -o cat | grep '^{' | tail -n 40"))
print(g("python3 -m json.tool < /var/lib/gsd/snapshots/state.json"))
```

以 root 起的命令不是"用户进程"，不会被 checkpoint 保存，也不会被 restore 杀掉，所以用这种方式取证不会干扰现场，restore 进行中也能用。下文各条"看哪里"里的命令，除特别说明外都是在 guest 里以 root 执行的。

**三样第一手信息**，绝大多数问题看这三样就够：

```bash
curl -s http://127.0.0.1:49984/health                                             # 是否降级、为什么
journalctl -u gsd --no-pager -o cat | grep '^{' | grep -E '"level":"(warn|error)"' | tail -n 30   # gsd 的告警与错误
python3 -m json.tool < /var/lib/gsd/snapshots/state.json                          # 层栈、checkpoint、增量关系
```

日志里每条消息的含义在 [24](24-configuration-and-observability.md) 的"日志"一节，状态文件的字段在同一章的"state.json"一节。连续做过很多次 checkpoint 之后如果发现 gsd 的日志残缺，见 §14。

**宿主上 orchestrator 的日志**（建模板失败、guest 失联时用）。新 920B 上 orchestrator 由 nomad 的 job `template-manager-system` 运行，日志这样取：

```bash
set -a; . /opt/e2b-infra/.env; set +a; export NOMAD_TOKEN="$NOMAD_ACL_TOKEN"
for A in $(nomad job allocs -json template-manager-system \
           | python3 -c 'import json,sys; [print(x["ID"]) for x in json.load(sys.stdin)]'); do
  nomad alloc logs "$A" 2>/dev/null
done | grep -a -i 'gsd' | tail -n 40
```

把最后一行的关键字换成各条里给的即可。日志会轮转，出事后尽快取。

---

## 1. create 被拒：`failed_precondition`

**症状**：`create()` 抛 `SandboxException`，信息以 `Checkpoint request refused by GSD (failed_precondition):` 开头。沙箱没有被改动。后半句有两种。

**（一）`user process trees t… and t… share pipe …` 或 `… share socket …`**

两棵用户进程树之间有一条匿名管道（读端、写端分在两棵树里），或者持有同一个 socket。信息里写明了是哪两棵树（根进程号）、哪个对象、哪两个进程的哪个文件描述符。

看哪里：

```bash
journalctl -u gsd --no-pager -o cat | grep -F 'refusing checkpoint' | tail -n 3      # 字段 shared 即具体对象
ps -eo pid,ppid,uid,stat,cmd --forest | awk '$3==1000 || NR==1'                     # 现在有哪几棵用户进程树
ls -l /proc/<信息里的进程号>/fd                                                       # 找到 pipe:[…] 或 socket:[…] 的那一项
```

怎么处置：让相连的进程待在同一棵树里——由同一条命令起，并且不要让其中某个进程脱离出去被 PID 1 收养。改写的例子见 [21](21-quickstart.md) 的"第一次用会遇到的三件事"。如果多出来的那棵树是早先某条命令遗留的孤儿进程，杀掉它即可。

原理：[08](08-process-trees.md)。

**（二）`checkpoint service degraded: …`**

gsd 处于降级状态。转 §6。

---

## 2. create 被拒：`resource_exhausted`

**症状**：`create()` 抛 `NotEnoughSpaceException`，信息末尾有 `Nothing was changed.`。沙箱没有被改动，进程照常运行。按信息分两种。

**（一）`not enough room for checkpoint images: estimated images … (1.2 x … RssAnon+RssShmem of N processes); snapshot store free …, MemAvailable …`**

进程镜像放在 guest 内存里的 2 GiB 内存盘上。gsd 在保存前估算镜像大小，内存盘剩余空间或 guest 可用内存有一个不够就拒绝。信息里的三个数字就是判定依据：估算值、内存盘剩余、`MemAvailable`。

看哪里：

```bash
df -h /var/lib/gsd/snapshots                       # 内存盘用了多少
du -sh /var/lib/gsd/snapshots/*_mem | sort -h      # 每个 checkpoint 的镜像各占多少
grep -E 'MemAvailable|MemTotal' /proc/meminfo
journalctl -u gsd --no-pager -o cat | grep -F 'not enough room' | tail -n 3   # store_short / mem_short 指明是哪一项
```

怎么处置：删掉不需要的 checkpoint。注意删除的顺序——有增量子节点的 checkpoint 被删时只是隐藏、镜像不释放，要从新往旧删或整条链一起删（[22](22-semantics-and-limits.md)）；删完再看一次 `df` 确认真的腾出来了。
是 `MemAvailable` 不够时，除了删 checkpoint（镜像占的就是内存），还可以让沙箱里的进程少占内存，或者给模板配更大的内存。估算按全量乘 1.2，偏保守是有意的：写到一半才发现放不下，后果是 guest 内存耗尽。

**（二）`… layer stack too deep: N lower layers, ioctl options M bytes > kernel limit 4096`**

当前这条链上的文件系统层数到了上限。完整信息以 `overlay sink failed:` 开头，中间可能还有 `checkpoint would not be restorable:`（表示这次 checkpoint 本身能做，但将来恢复它时放不下，所以提前拒绝）。

与（一）不同，这项检查排在 CRIU 保存之后：文件系统和 checkpoint 列表没有变，但进程已经被保存过一次、镜像随即丢弃，所以**下一次 create 对所有进程树存全量**（[12](12-failure-semantics.md)）。

看哪里：`state.json` 里 `overlay.layers` 的长度就是当前深度。

怎么处置：restore 到一个较早的 checkpoint，深度就回到那个点，可以继续 create；不再需要的 checkpoint 删掉，它们独占的层会被回收。单纯删除当前链上较旧的 checkpoint **不会**让当前层栈变浅——层栈是当前文件系统的实际组成，只有 restore 能换掉它。

原理：[13](13-state-concurrency-capacity.md)。

---

## 3. create 失败：`internal`

**症状**：`create()` 抛 `SandboxException`，信息以 `Code.internal:` 开头（SDK 把没有专门映射的错误码原样拼在信息前面）。沙箱没有被改动：已经被冻住的进程被放开，这次写出的镜像全部丢弃。但有一个后果——**下一次 create 对所有进程树存全量**，这是有意的（[09](09-checkpoint.md)）。

最常见的是 CRIU 保存失败：

```
Code.internal: criu dump failed: exit status 1; tail of criu-dump.log (removed with the failed images; full log in the gsd journal):
…CRIU 日志的最后 2048 字节…
```

多棵树时开头是 `criu dump failed for process tree t1 (root pid …, 2 of 3); the 1 tree(s) dumped before it were resumed and their images discarded:`。

看哪里：先读错误信息里的那段日志尾部，通常最后几行 `Error` 就是原因。不够时看 journal 里完整的 CRIU 输出（失败的镜像目录已经被删，`criu-dump.log` 文件不在了）：

```bash
journalctl -u gsd --no-pager -o cat | grep -v '^{' | grep -n -E 'Error|Warn' | tail -n 30
journalctl -u gsd --no-pager -o cat | grep '^{' | grep -F 'criu dump failed' | tail -n 3
```

| 日志里的线索 | 原因 | 处置 |
|---|---|---|
| `External socket is used` 之类提到 external socket | 进程持有一个另一端在这棵树之外的 unix socket：标准输出接在 journald 上（例如用 `systemd-run` 起的进程）、连着某个系统服务、或连着另一棵用户进程树 | 让进程不持有这样的连接：经 SDK 的 `commands.run()` 起、输出写文件；相连的进程放进同一棵树 |
| 其他 `Error (…)` | 进程用了 CRIU 在 gsd 这组固定选项下不支持的东西 | 没有完整的兼容清单（[20](20-results-and-gaps.md) 的缺口表）。按日志指出的对象调整负载；gsd 不提供放宽 CRIU 选项的开关 |

`internal` 的其他形式都少见，按错误码之后那段信息的开头判断：

| 信息开头 | 含义 | 看哪里 |
|---|---|---|
| `scan user processes:` | 读 `/proc` 失败 | `dmesg \| tail`；guest 是否内存或进程数耗尽 |
| `estimate checkpoint size:` | 读内存盘或 `/proc/meminfo` 失败 | `findmnt /var/lib/gsd/snapshots` |
| `create snapshot dir:` | 镜像目录建不出来 | `df -h /var/lib/gsd/snapshots` |
| `overlay sink failed: mkdir next upper/work:`、`… rename upper to sunk layer:` | 根盘上建目录或改名失败，多半是根盘满 | §13 |
| `overlay sink failed: overlay ioctl sink: overlay ioctl failed: errno=… (…)` | 内核拒绝了切层 | `dmesg \| tail -n 30`；errno 是 `ENOTTY` 或 `EINVAL` 而 `dmesg` 没有别的线索时，先怀疑内核不是打过补丁的那个（§11） |

原理：[09](09-checkpoint.md)、[12](12-failure-semantics.md)。

---

## 4. `deadline_exceeded`

**症状**：`create()` 抛 `TimeoutException`，信息形如 `criu dump failed: context deadline exceeded: signal: terminated: CRIU did not finish within GSD's time limit in the sandbox (not the SDK's request_timeout). …`。

含义：某棵树的 CRIU 保存超过了 gsd 自己的时限（默认每棵 60 秒，全部树合计 180 秒）。gsd 已经终止 CRIU、放开进程、丢弃镜像，沙箱没有被改动；下一次 create 存全量。这不是 SDK 的 `request_timeout`，调大后者没有用。

看哪里：

```bash
journalctl -u gsd --no-pager -o cat | grep '^{' | grep -E 'criu dump failed|resumed processes left stopped' | tail -n 5
ps -eo pid,stat,rss,cmd --sort=-rss | head        # 进程是否都回到了运行状态（STAT 不是 T）、哪个进程最大
```

怎么处置：正常负载下保存远用不到 60 秒（量级见 [20](20-results-and-gaps.md)），超时多半说明 guest 当时极度缺内存或缺 CPU，先查这个。确实需要更长时限时调 gsd 的 `-criu-dump-timeout`，并相应调大客户端的 `request_timeout`（[24](24-configuration-and-observability.md)）。

**restore 一侧的超时不是这个错误码。** CRIU 恢复超时发生在进程已经被杀之后，按 restore 半途失败处理，抛的是 `CheckpointRestoreException`，信息里同样带 `context deadline exceeded`。转 §5。

原理：[12](12-failure-semantics.md)。

---

## 5. restore 返回 `data_loss`

**症状**：`restore()` 抛 `CheckpointRestoreException`，信息形如：

```
restore of ckpt_… failed after user processes were killed; filesystem at target state: criu restore failed: …; tail of /var/lib/gsd/snapshots/ckpt_…_mem/t0/criu-restore.log:
…
```

之后 `create()` 一律被拒（`checkpoint service degraded: restore of … ; restore a checkpoint first`），`/health` 报降级；`restore()`、`list()`、`delete()` 仍然可用。

含义：restore 在杀掉用户进程**之后**失败。原来的用户进程已经没有了；文件系统要么还在旧状态（`filesystem at old state`，层没切过去），要么已经在目标状态（`filesystem at target state`）。
多棵树时信息里还有 `trees already restored and left running: …`（这些已经在跑）和 `trees not attempted: …`。沙箱此刻不是任何一个 checkpoint 的完整状态，所以 gsd 不允许把它存成新的 checkpoint。

看哪里：

```bash
curl -s http://127.0.0.1:49984/health
journalctl -u gsd --no-pager -o cat | grep '^{' | grep -E 'restore failed half way|criu restore failed|overlay restore failed' | tail -n 5
tail -n 40 /var/lib/gsd/snapshots/<checkpoint ID>_mem/t<序号>/criu-restore.log     # 路径在 criu restore failed 那行的 log 字段里
ps -eo pid,ppid,uid,stat,cmd --forest | awk '$3==1000 || NR==1'                   # 现在还剩哪些用户进程
```

怎么处置：**再 restore 一次。** 这是唯一的出路，也是设计好的出路：下一次 restore 会先把残留的用户进程（包括上一次已经恢复出来的那几棵树）全部杀掉，再从头做。

- 先重试同一个 checkpoint。失败如果是偶发的（例如超时），这一次多半就成功了。
- 同一个 checkpoint 反复失败，读 `criu-restore.log` 的尾部找原因。一类已知原因是被恢复的进程开着 `/dev/shm` 或 `/run` 里的文件，而这些目录不回滚、文件已经被删或改了大小（[22](22-semantics-and-limits.md)）。这时这个 checkpoint 的进程镜像实际上恢复不了，换一个 checkpoint restore；任何一个成功的 restore 都能解除降级。
- 实在没有能恢复进程的 checkpoint 时，restore 一个只有文件系统的 checkpoint（`has_process_image` 为 `False`）也能解除降级，然后重新起进程。
- 都不行就销毁沙箱重建。

持续出现时，留下上面四条命令的输出再处理。

原理：[10](10-restore.md)、[12](12-failure-semantics.md)。

---

## 6. `/health` 报 degraded

**症状**：`create()` 或 `restore()` 抛 `SandboxException`，信息含 `checkpoint service degraded:`；或者巡检发现 `/health` 的响应体里 `status` 是 `degraded`。注意 `/health` 的 HTTP 状态码始终是 200，SDK 的 `is_running()` 此时仍然返回 `True`，必须读响应体。

看哪里：`curl -s http://127.0.0.1:49984/health` 的 `reason`，按开头分三类。

| `reason` 的开头 | 影响 | 处置 |
|---|---|---|
| `restore of <ID> failed after user processes were killed; filesystem at old/target state` | 只拒绝 create | 按 §5 再 restore 一次。gsd 重启不会清除这个状态 |
| `cannot persist state to …:` | create、restore 开头先重试写状态文件，仍失败才拒绝；delete 照常 | 状态文件在内存盘上，写不进去通常是内存盘满了：`df -h /var/lib/gsd/snapshots`。delete 掉一些 checkpoint 腾出空间，下一次操作重试成功后自动解除（日志 `state persisted again, leaving degraded state`） |
| `overlay manager init failed:`、`no state file … but initial upper is unusable:`、`cannot read state file:`、`cannot parse state file …:`、`state file … has version …`、`invalid checkpoints in state file …:`、`state file … does not match the disk:` | create 与 restore 都拒绝，直到 gsd 重启 | 见下 |

第三类是致命降级：gsd 启动时发现自己掌握的状态不可信，于是拒绝做任何会改变状态的事，也**不再覆盖状态文件**，把现场留给排查。先看原因：

```bash
journalctl -u gsd --no-pager -o cat | grep '^{' | grep -F 'checkpoint service degraded' | tail -n 3
ls -la /var/lib/gsd/snapshots/ /overlay/layers/ /overlay/workspace/
python3 -m json.tool < /var/lib/gsd/snapshots/state.json | head -n 20
```

- `overlay manager init failed: open overlay mount …`：gsd 的启动参数不对（挂载点打不开）。模板里的 `gsd.service` 被改过，或有人在 guest 里不带参数手工启动了 gsd。恢复正确的启动方式后 `systemctl restart gsd`。
- `state file … does not match the disk: recorded overlay dir: …`：状态文件里记录的层目录或当前 upper 不在磁盘上了——有人手工删过 `/overlay/layers/` 下的目录，或磁盘出过问题。没有修复手段：缺了层，依赖它的 checkpoint 和当前的文件系统都不完整。保留现场后销毁沙箱重建。
- 其余几种（读不了、解析不了、版本不认识）指向状态文件本身损坏。同样保留现场后重建沙箱。

`systemctl restart gsd` 会让 gsd 重新加载一次，原因已经消除时降级随之解除；原因没消除，重启后还是同样的降级，不必反复重启。

原理：[12](12-failure-semantics.md)、[13](13-state-concurrency-capacity.md)。

---

## 7. 鉴权失败与 `unavailable`

**症状与原因**

| 现象 | gsd 返回 | 原因 |
|---|---|---|
| SDK 抛 `AuthenticationException`，信息 `missing X-Access-Token header` | `unauthenticated` | 请求没带访问令牌。用的不是代码仓里这份 SDK（旧版只发 `Authorization: Basic`，现在的 gsd 不认）；或者在 guest 里手工 `curl` secure 沙箱的 gsd 而没带头 |
| SDK 抛 `AuthenticationException`，信息 `invalid access token` | `unauthenticated` | 带的令牌不是这个沙箱的。gsd 日志有 `request with invalid access token` |
| SDK 抛 `TimeoutException`，信息以 `cannot verify access token: read MMDS after 3 attempts:` 开头 | `unavailable` | gsd 读不到沙箱元数据，无法校验，于是拒绝。SDK 沿用了 envd 的映射，把 `unavailable` 报成超时并提示"可能是沙箱超时"，这句提示在这里不适用。它与 §4 的保存超时、SDK 的 `request_timeout` 到期是三回事，区分办法见 [12](12-failure-semantics.md) |

看哪里：

```bash
journalctl -u gsd --no-pager -o cat | grep '^{' | grep -E 'invalid access token|cannot read access token hash' | tail -n 5

# gsd 读元数据的同样两步（先取会话令牌，再取内容），正常时输出里有 accessTokenHash
T=$(curl -s -m 2 -X PUT -H 'X-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
curl -s -m 2 -H "X-metadata-token: $T" -H 'Accept: application/json' http://169.254.169.254/ | head -c 300
```

怎么处置：前两种改客户端——用 [21](21-quickstart.md) 的 SDK，经 `Sandbox.create()` 或 `Sandbox.connect()` 拿到的沙箱对象会自动带对令牌。在 guest 里手工调 gsd 的 RPC 时，加 `-H 'X-Access-Token: <这个沙箱的访问令牌>'`；非 secure 沙箱不需要；`/health` 任何时候都不需要。
第三种是宿主一侧的问题：上面两条 `curl` 也拿不到内容时，是沙箱的元数据服务不通，收集 orchestrator 的日志上报；偶发一次可以直接重试，gsd 每个请求都重新读元数据。

原理：[04](04-architecture.md)。

---

## 8. SDK 连不上 gsd

**症状**：`is_running()` 返回 `False`，或 checkpoint 方法报连接类错误、502。

按"guest 里通不通"把问题一分为二：

```bash
systemctl is-active gsd
ss -ltn | grep -w 49984
curl -s -m 3 http://127.0.0.1:49984/health
/usr/bin/gsd -commit
```

| 结果 | 原因 | 处置 |
|---|---|---|
| `/usr/bin/gsd` 不存在，或没有 `gsd.service` | 这个模板不是在 gsd 栈下建的（或建模板时 `HOST_GSD_PATH=off`） | 换用 `base-gsd` 这类在 gsd 栈下建的模板（[23](23-deployment.md)） |
| 服务不是 `active`，或在反复重启 | gsd 起不来 | `systemctl status gsd --no-pager -l`、`journalctl -u gsd --no-pager \| tail -n 40` 看退出原因 |
| guest 里三条都正常，SDK 仍然连不上 | 问题在宿主到 guest 的路径或客户端配置 | 见下 |

guest 里正常时查客户端：SDK 实际连的地址是 `sbx.checkpointd_api_url`，打印出来看。它应当是 `http://49984-<沙箱 ID>.<域名>`，除非设了 `E2B_CHECKPOINTD_URL` 或 `E2B_SANDBOX_URL`——这两个变量如果是为别的环境留下的，会把请求带到错误的地方（[24](24-configuration-and-observability.md) 的 SDK 开关表）。宿主代理是按 `Host` 里的 `49984-<沙箱 ID>` 解析目标的，`E2b-Sandbox-Port` 请求头只在本地开发环境下才被采用，所以地址写错时请求头救不回来。
如果报错是 403，消息里有 `Sandbox is secured with traffic access token`（`is_running()` 此时是抛 `SandboxException` 而不是返回 `False`），说明沙箱是用 `allow_public_traffic=False` 创建的，而 SDK 没有给 gsd 的请求带入口流量令牌：升级到含提交 `dcca61266` 的 SDK；自己拼请求时加上请求头 `e2b-traffic-access-token`，值取 `sbx.traffic_access_token`。

地址没问题时，确认同一个沙箱的普通命令（`sbx.commands.run("true")`）能通：它走的是同一个代理、只是端口不同；普通命令也不通就不是 gsd 的问题。

原理：[04](04-architecture.md)。

---

## 9. 增量忽然变成全量

**症状**：`image_bytes` 突然接近进程的全部内存，`criu_dump_ms` 明显变长；或者 `clear_refs_failed` 非 0。

先分清是哪一种，判断依据是这次 `create()` 返回的 `parent_checkpoint_id`：

**（一）`parent_checkpoint_id` 为空——gsd 这一次就没有做增量**

gsd 只在"认得出这是同一棵树、并且手里有它上一次的镜像"时做增量。下面每一种情况都会让下一次存全量，全部是有意的行为：

| 之前发生了什么 | 日志里的线索 |
|---|---|
| 这棵树是新起的（新的后台命令；或原进程退出后重起，即使进程号碰巧相同） | 没有这棵树的 `using incremental CRIU dump` |
| 上一次 create 失败过（CRIU 失败、超时、切层失败） | `criu dump failed`、`overlay sink failed` |
| 删除了作为增量父节点的那个 checkpoint（哪怕只是被隐藏） | `checkpoint deleted`、`checkpoint hidden; …` |
| 上一次操作是 restore 到一个没有进程镜像的 checkpoint，或 restore 半途失败 | `no CRIU images, overlay-only restore`、`restore failed half way…` |
| 沙箱开机后的第一次 checkpoint | — |
| 父镜像不属于任何现存 checkpoint（不应出现） | `last images dir is not owned by any checkpoint, doing a full dump` |

看哪里：

```bash
journalctl -u gsd --no-pager -o cat | grep '^{' \
  | grep -E 'using incremental CRIU dump|criu dump failed|overlay sink failed|checkpoint deleted|checkpoint hidden|overlay-only restore|doing a full dump' | tail -n 20
python3 -c "import json; print(json.load(open('/var/lib/gsd/snapshots/state.json')).get('last_trees'))"
```

`last_trees` 为空就是"下一次全部存全量"。这种全量只发生一次，之后恢复增量，不需要处置。

**（二）`parent_checkpoint_id` 有值，但镜像仍然很大——做了增量，只是"脏页"很多**

| 检查 | 命令 | 含义与处置 |
|---|---|---|
| 透明大页 | `cat /sys/kernel/mm/transparent_hugepage/enabled` 应为 `always madvise [never]`；`journalctl -u gsd --no-pager -o cat \| grep -F transparent_hugepage` | 不是 `[never]` 时，脏页按 2 MiB 记，零散的小写入会让增量膨胀成接近全量。看 `dmesg \| grep overlay-init` 里有没有 `WARNING: failed to set transparent_hugepage=never`。模板有问题，重建（[23](23-deployment.md)）；不要为了别的目的把它改回 `always` |
| 脏页标记没重置 | 上一次 create 的 `clear_refs_failed`；日志 `clear_refs failed; next incremental dump of this process will be larger` | 上一次 checkpoint 之后有进程没能重置标记，这一次它的增量偏大。偶发、且那个进程此后正常则不必处理 |
| 负载自己 | 两次 checkpoint 之间进程是否新分配了大块内存 | 新分配的内存区域整个算脏，这是内核记账的方式，与实际写了多少无关。高频 checkpoint 的负载应预先分配、原地改写（[02](02-criu-and-incremental-memory.md) 的"假全量"） |
| 确实写了那么多 | — | 增量的大小就是两次之间改过的内存 |

原理：[02](02-criu-and-incremental-memory.md)、[09](09-checkpoint.md)。

---

## 10. 范围外的进程报 `ESTALE`

**症状**：restore 之后，沙箱里某个以 root 运行的服务、或一个跨过了 restore 的 root shell，在写文件或 `ls` 时报 `Stale file handle`（errno 116，`ESTALE`）。被恢复的用户进程和 restore 之后新起的命令没有这个问题。

看哪里：

```bash
readlink /proc/<进程号>/cwd                # 它的当前目录
ls -l /proc/<进程号>/fd | head -n 30       # 它一直开着的文件
dmesg | grep -c 'failed to get inode'      # 应为 0，见下
```

怎么处置：这是既定语义，不是故障。不在回滚范围内的进程没有被重启，但它脚下的文件系统被换掉了：它的当前目录如果只属于被丢弃的那段历史，从那里出发的写操作就返回 `ESTALE`。让这个进程重新 `cd` 到一个绝对路径、重新打开文件，或者在 restore 之后重启这个服务。需要长期运行又会碰文件的辅助进程，要么以默认用户运行（让它跟着回滚），要么只用绝对路径、不长期持有 overlay 上的句柄（[22](22-semantics-and-limits.md)）。

需要区分的另一种情况：如果 restore 之后**按绝对路径**访问一些本该存在的目录也整条路径 `ESTALE`，并且 `dmesg` 里有 `failed to get inode (-116)`，那是修复之前的内核的缺陷，说明模板固化的是旧内核，转 §11。

原理：[07](07-overlay-concurrency-and-fixes.md)、[15](15-known-limits.md) 的 L-06。

---

## 11. 模板里不是新内核或新 gsd

**症状**：宿主上明明换了 `/fc-gsd/gsd` 或内核文件，沙箱的行为却没有变；或者出现了本书说已经修掉的现象（连续 restore 之后根盘被写满、`dmesg` 里成批的 `ovl_ioctl_checkpoint:` 调试输出、SDK 拿不到 `timings`、`/` 的权限是 777、`/tmp` 是单独的内存盘）。

看哪里（guest 里与宿主上各一半）：

```bash
# guest 里
/usr/bin/gsd -commit; sha256sum /usr/bin/gsd
cat /proc/version
stat -c '%a %U:%G' / ; findmnt -n /tmp || echo "/tmp on overlay"
dmesg | grep -c -E 'ovl_ioctl_checkpoint|ovl_lookup:'

# 宿主上
/fc-gsd/gsd -commit; sha256sum /fc-gsd/gsd /fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin
bash scripts/ops/switch-gsd-new920b.sh status
```

怎么处置：guest 里的提交号、sha256、内核版本串与宿主上的对不上，原因只有一个——**模板是换文件之前建的**。模板固化了建模板那一刻的内核、gsd 和启动脚本，沙箱从它的快照恢复，不会去读宿主上后来的文件。不用缓存地重建模板（[23](23-deployment.md)），再核对一次。
宿主上 `status` 显示的 `HOST_KERNELS_DIR` 不是你放补丁内核的目录时，是切栈没有生效，先重新切栈再重建模板。

还有一种更隐蔽的情况：模板是在 gsd 栈下建的，但建模板时 `HOST_KERNELS_DIR` 指向了没打补丁的内核。建模板时的自检只验证 gsd 服务、`/health` 和 CRIU 的版本，**不验证内核是否带补丁**（[15](15-known-limits.md) 的 L-16），所以这样的模板能建成功，要到第一次 create 才失败（切层的 ioctl 被内核拒绝，或 CRIU 因内核没有脏页跟踪而报错；这两种失败的具体报错文本没有实测过）。判断办法仍然是上面的 `cat /proc/version`，与补丁内核的版本串对照（参考值在 [20](20-results-and-gaps.md)）。

原理：[05](05-guest-boot-and-injection.md)、[17](17-performance-after-fixes.md)。

---

## 12. 建模板失败

**症状**：在 gsd 栈下建模板，构建报错。按报错文本对号入座（在构建日志或 §0 的 orchestrator 日志里搜）：

| 报错 | 原因 | 处置 |
|---|---|---|
| `error reading gsd file (HOST_GSD_PATH): open … no such file or directory` | orchestrator 要注入 gsd，但读不到二进制。路径是默认的 `/fc-gsd/gsd` 时：文件没放。路径以 orchestrator 的工作目录开头、末尾是 `OFF` 或 `Off` 时：有人想关掉注入但写了大写——只有小写 `off` 才是关，其他值被当成相对路径 | 把 gsd 放到位（[23](23-deployment.md)）；或把环境变量改成小写 `off` 后重启 orchestrator |
| `gsd is not healthy after injection (…): gsd check failed: gsd not healthy after 10s: systemctl is-active=<…>, /health=<…>` | 注入并重启 gsd 之后，约 10 秒内它没有同时满足"服务 active"和"`/health` 回 ok"。报错后面附着 `systemctl status gsd` 的最后 20 行 | 看附带的 `is-active` 与 `/health`：服务没起来多半是二进制不对（架构不是 arm64、文件损坏）；`/health` 是 `degraded` 时原因就在其中（按 §6 的表判断） |
| `… gsd check failed: /usr/sbin/criu is not 4.x: …` 或 `… /usr/sbin/criu --version failed: …` | 基础镜像里没有 4.x 的 CRIU，或不在 `/usr/sbin/criu`。建模板不会替你安装 | 用 `Dockerfile.criu421` 建的镜像做基础镜像（[23](23-deployment.md)） |
| `failed to copy gsd binary to sandbox`、`failed to inject gsd binary`、`systemctl restart gsd: …` | 往构建中的沙箱里拷文件或执行命令失败 | 不是 gsd 特有的问题，按 orchestrator 日志里紧邻的错误查沙箱本身 |

取 orchestrator 日志时把 §0 那条命令的关键字换成 `gsd check failed`、`error reading gsd file` 或 `inject-gsd`。

另有两点提醒。第一，建模板成功只说明自检通过，不说明内核对（§11）。第二，换了 gsd 或内核之后重建模板要保证不用构建缓存，否则可能复用旧的基础层（[23](23-deployment.md)）。

原理：[05](05-guest-boot-and-injection.md)。

---

## 13. 根盘或 `/var/log` 持续增长

**症状**：沙箱跑久了根盘越来越满；严重时 checkpoint、restore 或普通写文件报空间不足。

看哪里：

```bash
df -h /                                             # 根盘（overlay 的各层、/tmp、/var/log 都在它上面）
du -shx /var/log /tmp 2>/dev/null
du -sh /overlay/layers/* 2>/dev/null | sort -h | tail -n 15
ls -a /overlay/layers/.trash 2>/dev/null | wc -l
journalctl --disk-usage
python3 -c "
import json; s=json.load(open('/var/lib/gsd/snapshots/state.json'))
print('当前层栈', len(s['overlay']['layers']), '层；checkpoint', len(s['checkpoints']), '个，其中隐藏', sum(1 for c in s['checkpoints'] if c.get('deleted')), '个')"
```

| 谁在涨 | 原因 | 处置 |
|---|---|---|
| `/var/log`（journal） | gsd 把 CRIU 的详细输出写进 journal，每次 checkpoint 都增长，一直涨到 journald 的上限。`/var/log` 不在 overlay 上，restore 不会把它退回去 | 已知项（[15](15-known-limits.md) 的 L-13）。`journalctl --vacuum-size=64M` 立即清理；长跑的沙箱在模板里给 journald 设 `SystemMaxUse` |
| `/overlay/layers/L<n>` | 每个 checkpoint 之间写过的文件构成一层，层要留到引用它的 checkpoint（包括隐藏的）都删掉 | 删不需要的 checkpoint。对照 `state.json`：`L<n>` 只要还出现在 `overlay.layers` 或任一 checkpoint 的 `layers` 里就不会被回收 |
| `/overlay/layers/restore_*_upper`、`upper_*_next` | 当前的可写层，以及刚被切走、等待后台回收的旧可写层 | 正常只有当前那一对加少量待回收的；成批堆积且不减少时取 gsd 日志里 `cannot move orphan dir to trash`、`failed to remove reclaimed dir` 上报 |
| `/tmp` | `/tmp` 是根文件系统的一部分，占根盘而不是内存 | 清理 `/tmp`；注意它会随 restore 回到 checkpoint 时的内容 |
| `df` 的已用量远大于各目录 `du` 之和，并且每次 restore 都涨 | 被删除的层仍被内核钉着不释放——这是修复之前的内核的行为 | 模板固化的是旧内核，转 §11 |

原理：[13](13-state-concurrency-capacity.md)、[07](07-overlay-concurrency-and-fixes.md) 的"回收：被换下的层、mount 与缓存引用"一节。

---

## 14. gsd 的日志残缺

**症状**：连续做了很多次 checkpoint 之后，`journalctl -u gsd` 里某个时刻之后缺了大量行，对不上实际做过的操作。

看哪里：`journalctl -u systemd-journald --no-pager | grep -c Suppressed`，非 0 即是。

怎么处置：journald 对每个服务限流，CRIU 的详细输出把额度用完后，gsd 自己的日志也被一起丢掉。需要完整日志的场合，开始之前给 `gsd.service` 加一个关闭限流的 drop-in 并重启 gsd，做法在 [24](24-configuration-and-observability.md) 的"取日志时要知道的三件事"。已经丢掉的日志找不回来；这时 `state.json` 和各镜像目录里的 `criu-restore.log` 仍然可用。

---

## 15. 新沙箱的第一个请求约 5 秒

**症状**：沙箱刚建好，经 SDK 发出的第一个 gsd 请求偶尔要 5 秒左右才返回，之后的请求都正常。

看哪里：比较这次调用的墙钟时间与 gsd 自报的 `total_ms`（`CheckpointInfo.timings` 或 `last_timings`）。属于这一条时，`total_ms` 只有几毫秒到几十毫秒，时间花在请求到达 gsd 之前。

怎么处置：已知项，根因未查，当前不处理。新 920B 上 2026-10-08 的专项探测里，经 SDK 的首个请求在 20 个沙箱中有 1 到 2 个是 5.01 秒；同一批沙箱在 guest 里直连 gsd 的请求、以及所有的第二个请求都正常；交接前的版本上现象相同。由此判断延迟在宿主一侧的转发路径上，与 gsd 无关。
对延迟敏感的调用方不要把第一个请求的 `request_timeout` 设得低于 6 秒；也可以在沙箱建好后先发一个无关紧要的请求（例如 `is_running()`）——这是按现象推出的做法，没有专门验证过它一定能把这次延迟吃掉。要复现或进一步定位，用随手册附带的探针 `scripts/probes/probe_first_rpc.py`。

原理：无；列在 [15](15-known-limits.md)，编号 U-01。

---

## 16. guest 失联：取串口输出

**症状**：沙箱不再响应任何命令，SDK 的调用全部超时。在 checkpoint 或 restore 之后立刻出现时，要怀疑 guest 内核出了故障。

看哪里：guest 已经跑不了 `dmesg`，唯一的信息来源是它的串口输出。orchestrator 把每个沙箱的串口输出记在自己的日志里，随手册附带的 `scripts/stress/fc_console.sh` 把属于某个沙箱的行筛出来（在宿主上执行）：

```bash
bash scripts/stress/fc_console.sh <沙箱 ID> console.txt
grep -n -E 'BUG|Oops|KASAN|panic|lockup|Call trace|overlayfs:|Unable to handle' console.txt | head -n 20
```

输出的每一行是"宿主时间 + guest 控制台原文"，宿主时间用来和调用方的操作记录对时间。orchestrator 的日志会轮转，沙箱销毁后也不再有新内容，所以**发现失联后先取串口输出，再销毁沙箱**。

怎么处置：串口输出里有内核故障报告（`BUG`、`Oops`、`Call trace` 等）时，这是内核缺陷，应当连同完整的 `console.txt`、出事前的操作序列、模板的 `gsd -commit` 与内核版本串一起上报；先按 §11 确认模板不是旧内核建的——修复之前的内核在并发文件操作下有多个已知的崩溃路径（[14](14-issue-resolution.md)）。
串口输出里没有故障报告、只是停在某处时，多半是 guest 内存耗尽或宿主过载，查沙箱的内存配置与宿主负载。失联的沙箱没有修复手段，取证之后销毁重建。

压测用的看门狗 `run_stress_watch.sh` 自动做的就是这件事：操作日志超过设定时间没有新行即判定卡死，取串口输出后再杀沙箱（[19](19-testing-and-debug-kernel.md)）。

原理：[07](07-overlay-concurrency-and-fixes.md)、[19](19-testing-and-debug-kernel.md)。

---

## 代码位置

| 内容 | 位置 |
|---|---|
| create 各种拒绝与失败的报错文本 | `CreateCheckpoint`（`ckpt/service.go:163`）；树间共享的描述 `describe`（`ckpt/shared.go:149`）；容量估算的文字 `String`（`ckpt/capacity.go:49`） |
| 层栈太深的报错 | `checkOverlayOptionsLen`（`ckpt/overlay.go:540`）、`SinkUpper`（`ckpt/overlay.go:125`） |
| restore 两段式与 `data_loss` 的报错文本 | `RestoreCheckpoint`（`ckpt/service.go:463`）、`restoreFailed`（`ckpt/service.go:635`） |
| 降级的检查与原因文本 | `checkReady`（`ckpt/service.go:677`）、`setFatal`（`ckpt/service.go:653`）、`loadState`（`ckpt/state.go:341`）、`persist`（`ckpt/state.go:256`） |
| 超时与错误码的对应 | `criuErrorCode`、`overlayErrorCode`（`ckpt/service.go:865`、`:874`）；`runCriuCmd`（`ckpt/criu.go:28`） |
| 鉴权的报错文本与读元数据 | `Authenticate`（`packages/gsd/internal/permissions/authenticate.go:53`）、`AccessTokenHash`（`packages/gsd/internal/permissions/mmds.go:49`） |
| SDK 的错误映射 | `CHECKPOINT_RPC_ERROR_MAP`（`py-sdk/e2b/gsd/api.py:59`）；其余错误码沿用 `py-sdk/e2b/envd/rpc.py` 的默认映射 |
| 建模板的报错文本 | `addGSDFiles`（`packages/orchestrator/internal/template/build/core/rootfs/gsd.go:33`）；`gsdCheckScript`、`injectGsdInSandbox`（`packages/orchestrator/internal/template/build/layer/gsd.go:22`、`:50`） |
| 层与镜像目录的回收 | `reclaimOrphans`（`ckpt/overlay.go:372`）、`reclaimSnapshotStore`（`ckpt/snapshot_store.go:25`） |
| 取串口输出、首个请求探针 | 随手册附带的 `scripts/stress/fc_console.sh`、`scripts/probes/probe_first_rpc.py` |

---

## 本章要点

- 先取三样第一手信息：`/health` 的响应体、`journalctl -u gsd` 里的告警与错误、`state.json`。以 root 在 guest 里取证不会干扰用户进程，也不会被 restore 杀掉。
- create 的拒绝（`failed_precondition`、`resource_exhausted`）和失败（`internal`、`deadline_exceeded`）都保证沙箱没被改动；失败之后的下一次 create 存全量。树间管道就合并成一棵树，放不下就删 checkpoint（从新往旧），层栈太深只能靠 restore 到较早的点来变浅。
- CRIU 保存失败先读错误信息里的日志尾部；失败的镜像目录已被删，完整输出在 journal 里。
- `data_loss` 之后唯一的出路是再 restore 一次；同一个 checkpoint 反复失败就换一个，任何一次成功的 restore 都解除降级。
- 降级分三类：restore 半途失败（再 restore）、状态文件写不进（腾内存盘空间）、致命（状态与磁盘对不上，保留现场后重建沙箱）。`/health` 永远是 200，要读响应体。
- "换了文件却没生效"和各种"本应修掉的现象"，根因几乎总是模板没有重建；建模板的自检不验证内核是否带补丁，要到 guest 里核对版本串。
- 增量变全量先看 `parent_checkpoint_id`：为空是 gsd 有意存全量（新树、上次失败、父节点被删、restore 之后），有值而镜像大则查透明大页、`clear_refs_failed` 和负载的内存分配方式。
- 范围外进程的 `ESTALE`、journal 增长、首个请求约 5 秒是已知项；guest 失联时先取串口输出再销毁沙箱。
