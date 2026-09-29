# 26 · 部署前提与检查清单

## 本章目标

读完本章，你应该能：

1. 分清增量 checkpoint 的两层前提（要不要记脏页、用什么方式记），知道 950、没有 HDBSS 的机器和 920B 开发机上 `FC_TRACK_DIRTY_PAGES` 各该怎么设；
2. 部署之前逐项核对平台前提，并用预检脚本一次查完；
3. 说清 orchestrator、Firecracker、SDK 覆盖层、guest 内核为什么必须成套，配错时各是什么表现；
4. 部署之后按顺序做完自检，确认 checkpoint 能力就位、而且真的是增量。

上一章（[25](25-errors-timeouts-concurrency.md)）讲完了调用方要处理的错误与超时；前三章都面向调用方，从本章起面向部署与运维。本章讲部署之前这台机器要满足什么，装完之后用哪几条命令确认 checkpoint / restore 真的就位、而且是增量。
部署步骤本身不在这里重复，照 [`single-node-offline-deploy.md`](../../single-node-offline-deploy.md) 与 [`deploy-docs/`](../../deploy-docs/) 做；
开关的完整语义见下一章（[27](27-configuration-and-capacity.md)），出了问题按症状查 [29](29-troubleshooting.md)。

---

## 1. 先分清两层：要不要记脏页、用什么方式记

增量 checkpoint 靠"本代写过哪些页"这张位图。这件事分两层，配置时最容易混：

| 层 | 管什么 | 谁决定 | 开关 |
|---|---|---|---|
| ① 要不要记 | 虚机启动时是否武装脏页跟踪 | orchestrator，启动时算一次，全节点一个值 | `FC_TRACK_DIRTY_PAGES` |
| ② 用什么记 | 硬件标脏（HDBSS）还是 KVM 写保护 | Firecracker 在构建每台虚机时自己探测 | 没有开关（`FC_HDBSS_REQUIRED` 只决定探测失败时是否拒绝启动） |

**"增量退化成全量"说的是第 ① 层关着**，与 HDBSS 在不在无关。第 ① 层关着时一切照常工作，只是每次 checkpoint 都拷整份 guest 内存。

`FC_TRACK_DIRTY_PAGES` 在三种机器上的用法：

| 机器 | `FC_TRACK_DIRTY_PAGES` | 结果 | Firecracker 自报的后端 |
|---|---|---|---|
| **鲲鹏 950**（有 HDBSS，交付目标） | **不设** | orchestrator 探到 KVM 能力 502，自动开；硬件标脏，checkpoint 为增量 | `hdbss` |
| 没有 HDBSS 的机器，不设 | 不设 | 关；每次 checkpoint 全量（`mem_mode="full"`），功能正确；启动日志有 `dirty page tracking is off` 的 WARN | `off` |
| 没有 HDBSS、要增量（例如 920B 开发机） | **显式 `true`** | 开，走 KVM 写保护；checkpoint 为增量 | `kvm-wp` |

交付件（`e2b-deploy/dep/template-manager.hcl`）**有意不传**这个变量：950 上不需要。
没有 HDBSS 的机器不设时默认关，是因为软件写保护要让每个干净页的第一次写入陷出一次，对从不做 checkpoint 的沙箱是净亏（原理见 [05](05-dirty-page-tracking-and-hdbss.md)）。
取值只认 Go `strconv.ParseBool` 的写法，`yes`、`on` 之类会被忽略并打 WARN，详见 [27 §2.1](27-configuration-and-capacity.md#21-脏页跟踪与-hdbss)。

---

## 2. 平台前提

逐项核对。"怎么查"一列的命令都是只读的。

| # | 前提 | 怎么查 | 不满足时 |
|---|---|---|---|
| 1 | aarch64；回滚路径（GIC 清线、vCPU 重置、HDBSS）只在 aarch64 上实现 | `uname -m` | 不能用 |
| 2 | 宿主内核：openEuler 24.03 LTS-SP4，`6.6.0-159.4.3.154.oe2403sp4.aarch64`（950 与 920B 开发机上实测的版本），带 KVM 与 userfaultfd | `uname -r`；`grep -E 'CONFIG_KVM=\|CONFIG_USERFAULTFD=' /boot/config-$(uname -r)` | 换内核后第 6 项的 `nbd.ko` 也要重编 |
| 3 | `/dev/kvm` 存在且 root 可读写；KVM 跑在 VHE 模式 | `ls -l /dev/kvm`；`dmesg \| grep -Ei 'kvm.*VHE mode initialized'` | 沙箱起不来；非 VHE 时 HDBSS 启用会被内核拒绝 |
| 4 | **HDBSS**（只对 950 要求）：内核编了 `CONFIG_ARM64_HDBSS=y`，**并且** CPU 支持（KVM 能力 502）。只有内核配置不够：920B 的内核也是 `=y`，但 CPU 没有 | `grep CONFIG_ARM64_HDBSS /boot/config-$(uname -r)`；能力以 orchestrator 能力行的 `track_dirty_pages_reason` 为准（§4.3），或跑 `preflight-customer.sh` 的第 3 节（本机有 gcc 时会编一个探针真开一次） | 950 上 Firecracker 静默退回 `kvm-wp`；想让它直接拒绝启动，见 [27](27-configuration-and-capacity.md) 的 `FC_HDBSS_REQUIRED` |
| 5 | GICv3 | `preflight-customer.sh` 第 2 节 | v2 未实测 |
| 6 | 自编译的 `nbd.ko`（`nbds_max=512`），vermagic 与运行内核一致，已固化为开机加载 | `lsmod \| grep -w nbd`；`cat /sys/module/nbd/parameters/nbds_max`（应为 512） | 沙箱根盘走 NBD，没有它沙箱起不来。固化步骤见部署指南 §0.2 |
| 7 | `tun` 模块与 `/dev/net/tun` | `ls -d /sys/module/tun /dev/net/tun` | 沙箱网卡起不来。`preflight-customer.sh` 还会查 `vhost_net`，未加载时只判 WARN（920B 开发机上它就没加载，沙箱照常） |
| 8 | `net.ipv4.ip_forward=1`，**且在 `build.sh -s` 之前生效**（槽位 netns 在创建时继承这个值） | `sysctl -n net.ipv4.ip_forward` | 建模板在等 envd 那一步 60 s 超时。已经装完才发现要重建整池槽位，见部署指南 §0.3 |
| 9 | 2 MiB 大页：guest 内存走 hugetlb，`init-client.sh` 启动时预留一部分，其余走 overcommit | `grep -i huge /proc/meminfo`；`cat /proc/sys/vm/nr_overcommit_hugepages` | 沙箱数被大页数卡住 |
| 10 | **产物盘**：store 根 `/orchestrator/build/checkpoints`（由 `ORCHESTRATOR_BASE_PATH`、`DEFAULT_CACHE_DIR` 决定，目录布局见 [04 §5](04-architecture.md#5-数据面目录布局)）所在文件系统为 **ext4**，支持稀疏文件 | `df -T /orchestrator/build` | 交付方案只在 ext4 上验证过；为什么是 ext4 见 [04 §8](04-architecture.md#8-为什么交付用-ext4) |
| 11 | 产物盘与沙箱缓存盘（`/orchestrator/sandbox`，活写层所在）是**同一个文件系统** | `findmnt -no SOURCE --target /orchestrator/build; findmnt -no SOURCE --target /orchestrator/sandbox` 两行相同 | 封层从 rename 退化为拷贝，checkpoint 变慢 |
| 12 | 产物盘余量：按 [27 §4](27-configuration-and-capacity.md#4-容量规划) 估；服务端另有水位闸，低于 max(4 GiB, 2 × guest 内存) 时拒绝 checkpoint 与 restore | `df -h /orchestrator/build` | 507 `disk_full` |

把 2–11 项一次查完的脚本是 `rollback/scripts/crtest/portability/preflight-customer.sh`（零依赖、只读，退出码 = FAIL 项数），在 clone 下来的 e2b-infra 仓库根目录执行：

```bash
bash rollback/scripts/crtest/portability/preflight-customer.sh
```

它也是 [30](30-acceptance-runbook.md) 里 `run.sh smoke` 的第一项。`WARN` 不算失败，但要逐条看一眼说明。

---

## 3. 版本配对

orchestrator 与 Firecracker **必须成对交付**：两者出自同一个代码仓库的同一个版本（`packages/` 与 `firecracker/`，代码基准见 [README](README.md)），
RPM 里分别对应 `0001-adapted-for-arm-architecture.patch` 与 `firecracker.arm`。SDK 覆盖层和 guest 内核也要对上。

| 组件 | 交付件 → 落地位置 | 核对 |
|---|---|---|
| orchestrator | RPM → `/opt/e2b-infra/bin/orchestrator`，再由部署拷到 `/usr/bin/template-manager`（nomad job `template-manager-system` 实际执行的就是它）与 `/usr/bin/orchestrator` | 三个文件 sha256 相同；运行中的进程 `readlink /proc/$(pidof -s template-manager)/exe` 指向 `/usr/bin/template-manager` |
| Firecracker | `firecracker.arm` → `/opt/e2b-infra/bin/firecracker` → `init-client.sh` 拷到 `/fc-versions/v1.13.1/firecracker` | 两处 sha256 都是 `18f3faa7f47c173a5f47bfc7f578f5073cfbde2ac9bdb50bf88c434341d6d4c9` |
| guest 内核 | `/fc-kernels/vmlinux-6.1.158/vmlinux.bin` | 文件存在；缺了建模板必失败 |
| Python SDK | `e2b==2.20.0` + 覆盖层 `dep/e2b-sdk-checkpoint/`（21 个文件，版本锁死） | `install.py --check`（§4.2） |

**Firecracker 的目录名只是查找键。** orchestrator 按 `$FIRECRACKER_VERSIONS_DIR/<模板记录的 FC 版本>/firecracker` 找二进制；
模板建时记的是 `v1.13.1`，而放在这个目录里的交付 FC 自报 `Firecracker v1.12.1`（`/fc-versions/v1.13.1/firecracker --version`）。
这不影响功能，但**换 FC 时必须换 `v1.13.1/firecracker` 这一个文件**，放错目录等于没换，而且不报错。

**核对 Firecracker 身份以 sha256 为准。** `--version` 报的 `v1.12.1` 是分叉所基于的上游版本号，目录名 `v1.13.1` 是模板记录的查找键，两者都区分不了交付的分叉与上游原版；orchestrator 能力文件里的 `commit` 目前也是空串（RPM 构建没有注入提交号，[27 §3.1](27-configuration-and-capacity.md#31-能力文件)）。所以核对二进制只看上表的 sha256。

配错的后果：

| 配法 | 表现 |
|---|---|
| 新 orchestrator + 未打补丁的 Firecracker | checkpoint 与 restore 都失败（500 `internal`）：未打补丁的 FC 不认识 `dirty_bitmap_path` 字段、没有 `save-dirty-bitmap` 与 `rollback` 端点；消息里可见 `save-dirty-bitmap`、`does not support in-place rollback` 等字样。是明确的失败，不会静默降级 |
| 旧 orchestrator + 新 Firecracker | 沙箱照常，但没有 checkpoint 能力（Firecracker 的扩展是加法） |
| SDK 没装覆盖层 | `sb.checkpoint` 不存在 |
| 覆盖层版本旧、没有 `mem_mode` 字段 | 功能可用，但验收脚本只能跑 57 项，"是不是增量"那两条判据跳过（[30](30-acceptance-runbook.md)） |
| 宿主内核换了而 `nbd.ko` 没重编 | 开机加载的是发行版原版 `nbd.ko`，设备数照样 512，从数量上看不出来；用 `sha256sum /lib/modules/$(uname -r)/kernel/drivers/block/nbd.ko` 对比 |

---

## 4. 部署后自检清单

装完、`build.sh -s` 打印 `e2b-infra 服务启动完成` 之后按顺序做。前五步不建沙箱。

### 4.1 二进制是不是这一套

```bash
sha256sum /opt/e2b-infra/bin/orchestrator /usr/bin/template-manager /usr/bin/orchestrator
sha256sum /opt/e2b-infra/bin/firecracker /fc-versions/v1.13.1/firecracker
readlink /proc/$(pidof -s template-manager)/exe
```

前三行 sha 相同、中间两行都是 `18f3faa7…`、最后一行是 `/usr/bin/template-manager`。
有沙箱在跑时，再确认运行中的 FC 就是这个文件：

```bash
for p in $(pidof firecracker); do readlink /proc/$p/exe; done | sort | uniq -c
```

### 4.2 SDK 覆盖层

用**装 SDK 的那个解释器**（`build.sh -i` 在哪个 Python 环境里跑，SDK 就装在哪，[30 §1](30-acceptance-runbook.md#1-跑之前)）：

```bash
python3 /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check
```

期望看到 `payload 的 21 个文件都在位` 和 `自检通过（干净子进程）：…`。异常族的判据是"原有 8 个子类都在，且 reason 映射表里每一项都是 `CheckpointException` 的子类"——
现在是基类加 9 个子类共 10 个（新增 `CheckpointBytesLimitException`，[25](25-errors-timeouts-concurrency.md)）。

### 4.3 orchestrator 自报的能力

orchestrator 启动时打一行 `checkpoint capabilities`，说清脏页跟踪开没开、为什么，以及各项限额的生效值和来源（字段逐个解释在 [27 §3](27-configuration-and-capacity.md#3-启动能力行)）。
从 deltabox-dev `93ccb02` 起，同样的内容还写进**能力文件** `checkpoint-capabilities.json`：在 `DEFAULT_CACHE_DIR` 下（默认 `/orchestrator/build/`，即 store 根的父目录），
另带 `pid`、`started_at`、`version`、`commit` 四个键（[27 §3.1](27-configuration-and-capacity.md#31-能力文件)）。
日志行在当前 alloc 的 `start.stdout.<n>` 里，**会轮转**（负载下几分钟就把启动行滚掉）；能力文件不会，下次启动时整份替换。

先看能力文件。文件里的 `pid` 必须是正在运行的进程，否则是以前那次启动留下的，不算：

```bash
p=$(pidof -s template-manager)
E=$(tr '\0' '\n' < /proc/$p/environ)
C=$(sed -n 's/^DEFAULT_CACHE_DIR=//p' <<<"$E"); B=$(sed -n 's/^ORCHESTRATOR_BASE_PATH=//p' <<<"$E")
F=${C:-${B:-/orchestrator}/build}/checkpoint-capabilities.json
grep -qE "^  \"pid\": $p,?\$" "$F" 2>/dev/null && cat "$F" || echo "没有 $F，或它不是当前进程写的：看下面的日志行"
```

没有能力文件（此前的版本不写）或 `pid` 对不上时，看日志行，部署后立刻抓一次留档。两条 WARN 只在日志里，能力文件里没有，所以第二条 grep 无论如何都要跑：

```bash
p=$(pidof -s template-manager)
A=$(tr '\0' '\n' < /proc/$p/environ | sed -n 's/^NOMAD_ALLOC_DIR=//p')
ls -1v "$A"/logs/start.stdout.* | xargs grep -ah 'checkpoint capabilities' | tail -1
ls -1v "$A"/logs/start.stdout.* | xargs grep -ahE 'dirty page tracking is off|is not a boolean and was ignored|fault injection is armed' | tail -3
```

看这几项：

- `track_dirty_pages` 为 `true`；`track_dirty_pages_reason` 在 950 上是 `hardware dirty state tracking present (KVM capability 502)`，在显式开启的机器上是 `FC_TRACK_DIRTY_PAGES="true"`；
- `fault_inject` 为空（`[]`）；
- 其余限额的值与 `_source`（`env` / `default`）与部署意图一致；
- 第二条 grep 没有输出。

启动行已经滚掉、又没有能力文件时，也可以直接看进程环境里设了哪些变量（只能说明"设了什么"，不能代替能力文件或能力行说明"读成了什么"）：

```bash
tr '\0' '\n' < /proc/$(pidof -s template-manager)/environ | grep -E '^(CHECKPOINT_|FC_|PROXY_TRACE|GODEBUG)'
```

### 4.4 宿主预检

§2 那条 `preflight-customer.sh`，`FAIL 0`。

### 4.5 网络槽位水位基线

```bash
ip netns list | grep -cE '^ns-[0-9]+$'
```

记下这个数。正常水位 = 300（预建池）+ 历史峰值并发；每重启一次 orchestrator 会多漏约一池（[29](29-troubleshooting.md#9-网络槽位netns泄漏)）。
刚启动时这个数还在涨（暖池填充），**做任何性能测试前等它连续几分钟不变**。

### 4.6 增量真的生效

这一步要建沙箱，用 [30](30-acceptance-runbook.md) 的 `run.sh smoke` 一次做完。smoke 里的 `checkpoint_verify.py` 开头打印
`脏页后端 : …`——这是 Firecracker 自己报的第 ② 层（950 应为 HDBSS，显式开启的无 HDBSS 机器为 KVM 写保护），能力行只能回答第 ① 层。

另外两个独立证据：

```bash
# 除每个沙箱的第一个 checkpoint 外，mem_mode 应全是 incremental
p=$(pidof -s template-manager)
A=$(tr '\0' '\n' < /proc/$p/environ | sed -n 's/^NOMAD_ALLOC_DIR=//p')
ls -1v "$A"/logs/start.stdout.* | xargs grep -ah 'created checkpoint' | grep -o '"mem_mode": "[a-z]*"' | sort | uniq -c

# 950：内核确认给 Firecracker 开了 HDBSS（方括号里是 FC 的 pid）
dmesg | grep 'Enable HDBSS success' | tail -3
```

---

## 5. 已知项

### 5.1 80 端口的转发规则劫持 hyperloop 请求

**现象**：guest 发往 hyperloop（`192.0.2.1:80`，orchestrator 在宿主上给每个沙箱提供的 HTTP 服务）的请求拿不到 hyperloop 的应答；
与此同时 client-proxy 的日志里是成片的 WARN `invalid host … "host": "192.0.2.1"`，每条带多行调用栈。
部署件的 2 h 滚动长测 long11（16 个沙箱）里这类行每秒约 900 条，两小时共 653 万条，client-proxy 日志写到约 9.1 GB。

**原因**：部署时 `build.sh` 在 `deploy.sh` 之后装了一条**不限入口网卡**的 REDIRECT
（`e2b-deploy/build.sh:662-663`；KASandbox_0904 的 `deploy/build.sh:1275` 是同一条）：

```bash
iptables -w -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 3002
```

它本意是把外部打到本机 80 端口的请求交给 client-proxy（3002）。orchestrator 给每个槽位装的 hyperloop 规则
（`-d 192.0.2.1 -i veth-<n> -p tcp --dport 80 -j REDIRECT --to-ports 5010`，`packages/orchestrator/internal/sandbox/network/network.go:226-234`）
是槽位建网时才追加的，所以永远排在上面这条后面。nat 表对一条新连接只取第一条匹配的 REDIRECT，guest 从 veth 进来、发往 80 端口的连接先匹配到
3002，被转给了 client-proxy；client-proxy 不认识 `192.0.2.1` 这个 Host，回 `invalid host`。用只读命令可以看到这个顺序：

```bash
iptables -t nat -S PREROUTING | head -5
# -A PREROUTING -p tcp -m tcp --dport 80 -j REDIRECT --to-ports 3002          ← 不限网卡，排在所有槽位规则前面
# -A PREROUTING -d 192.0.2.1/32 -i veth-2 -p tcp -m tcp --dport 80 -j REDIRECT --to-ports 5010
```

同一个顺序也挡在每个槽位的 `-i veth-<n> --dport 80 -j REDIRECT --to-ports 5016`（出网 HTTP 的 TCP 防火墙代理）前面，
guest 访问外部 80 端口的连接按规则顺序同样会被转到 client-proxy；这一点是从规则顺序推出来的，没有单独验证。

**影响**：checkpoint / restore 不经过这条链路，本手册的功能与性能结果不受影响（长跑全部 0 不一致）。受影响的是依赖 hyperloop 的 guest 功能，
以及 client-proxy 的日志量与宿主 conntrack 表（被劫持的连接也留下条目，见 [22 §4.6](22-long-run-and-concurrency.md#46-conntrack-清理占了冻结窗口)）。

**处置**：**用户 09-30 决定暂不改。** 将来要改时，给这条规则加 `! -i veth+`，只接住不是从槽位 veth 进来的流量，两个 `build.sh` 都要改：

```bash
iptables -w -t nat -C PREROUTING ! -i veth+ -p tcp --dport 80 -j REDIRECT --to-port 3002 2>/dev/null \
|| iptables -w -t nat -A PREROUTING ! -i veth+ -p tcp --dport 80 -j REDIRECT --to-port 3002
```

已经装好的节点还要删掉旧的那条（`iptables -w -t nat -D PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 3002`）。
`OUTPUT -o lo --dport 80` 那条只管本机发起的连接，不用动。排障条目见 [29 §15](29-troubleshooting.md#15-client-proxy-日志暴涨guest-访问-hyperloop-不通)。

---

## 本章要点

- 增量有两层前提：orchestrator 决定"记不记"（`FC_TRACK_DIRTY_PAGES`，950 不设即自动开，没有 HDBSS 的机器要增量就显式设 `true`），Firecracker 决定"用什么记"（HDBSS 或 KVM 写保护）。"增量退化成全量"说的是第一层关着，与 HDBSS 在不在无关。
- 交付件有意不传 `FC_TRACK_DIRTY_PAGES`：950 不需要；没有 HDBSS 的机器默认不开，是因为软件写保护对从不做 checkpoint 的沙箱是净亏。
- 平台前提共 12 项，`preflight-customer.sh` 一次查完；HDBSS 要内核配置与 CPU 能力同时具备。
- orchestrator、Firecracker、SDK 覆盖层、guest 内核成套交付；FC 只认 `/fc-versions/v1.13.1/firecracker` 这一个位置，放错目录等于没换，而且不报错。
- 新 orchestrator 配未打补丁的 FC 会明确失败，不会静默降级；SDK 覆盖层版本旧时功能可用，但验收只剩 57 项，"是不是增量"的判据失效。
- 已知项：`build.sh` 装的 80 → 3002 转发不限入口网卡，劫持了 guest 发往 hyperloop 的请求，client-proxy 日志随之暴涨；不影响 checkpoint / restore，用户 09-30 决定暂不改，改法是加 `! -i veth+`（§5.1）。
- 部署后六步自检：二进制 sha、SDK `--check`、能力文件或能力行（后者立刻留档）、宿主预检、netns 基线、`run.sh smoke`；最后确认除每个沙箱的第一个外，`mem_mode` 全是 `incremental`。
