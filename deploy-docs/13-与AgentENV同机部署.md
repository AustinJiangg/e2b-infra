# 13 与 AgentENV 同机部署

> 起因：以前在同一台服务器上同时部署过 e2b 和 [AgentENV](https://github.com/kvcache-ai/AgentENV)
> （kvcache-ai 的 Firecracker 沙箱平台），现象是先起来的那个能用、另一个就不行。
> 2026-11 在一台已经跑着 AgentENV 0.2.0 的鲲鹏服务器（openEuler 24.03 SP4，384 核、2.2 TB 内存）
> 上重新部署 e2b 之前，对照两边源码和这台机器的实际状态把原因查清楚，并在 e2b 一侧改掉。
>
> 本篇讲：两边的沙箱网络为什么会撞、撞在哪里、e2b 这边改了什么、以后要再调网段或前缀时动哪几处。
> 照着做的步骤在部署指南 [`single-node-offline-deploy.md`](../single-node-offline-deploy.md) §0.6。
>
> 配套：[`07-single-node-traffic-architecture.md`](07-single-node-traffic-architecture.md)（80→3002 规则的原理）、
> [`05-build.sh-s-启动部署详解.md`](05-build.sh-s-启动部署详解.md)（`-d` 清理 netns/veth/iptables 的来龙去脉）、
> [`08-源码开发与出包流程.md`](08-源码开发与出包流程.md)（0001 补丁怎么重新生成）。

---

## 0. 结论速查

1. 两边的沙箱网络几乎是同一套设计：宿主机侧网卡都叫 `veth-<n>`、n 都从 1 起，地址池都是
   `10.11.0.0/16` + `10.12.0.0/16`，算法也一样。同机时网卡名和 /32 路由直接撞，后建的一方报 `File exists`。
2. 两边还各装了一条会误伤对方的规则：AgentENV 的 `INPUT -i veth-+ -s 10.11.0.0/16 -j REJECT`
   拒掉 e2b 沙箱访问宿主机（e2b 的出网 TCP 代理、hyperloop 全靠它）；e2b 的
   `PREROUTING --dport 80 -j REDIRECT --to-port 3002` 不限目的地址，截走 AgentENV 沙箱的出网 http。
   e2b 的 `build.sh -d` 还会把所有 `veth-<n>` 删掉。
3. **全部在 e2b 一侧改，AgentENV 不用动**：网卡改名 `e2bv-<n>`（0001 补丁），地址池改成
   `10.13.0.0/16` / `10.14.0.0/16`（`.env`），清理逻辑只认自己的名字和地址池，80 端口规则只接管目的地址是本机的流量。
4. 改完后在这台机器上实测：e2b 建出 288 个 `e2bv-` 槽位，AgentENV 的网卡、路由、iptables 规则与部署前逐行一致，
   两边的沙箱都能正常创建。
5. 地址分开之后，两边仍共用同一个内核里的邻居表、conntrack 表、网卡总数，以及大页和内存（§7）。

---

## 1. 两边的沙箱网络长什么样

两边每个沙箱占一个"槽位"：一个 netns，里面一张 `vpeer`/`eth0`（veth 的沙箱端）和一张 `tap0` 接 Firecracker；
veth 的另一端留在宿主机。宿主机通过一个"槽位地址"访问沙箱，netns 里用 DNAT/SNAT 把它映射到 guest 的 169.254.0.21。

```
宿主机                                   槽位 netns                          guest
  10.11.0.n/32 ──route──► veth-n ═══════ vpeer/eth0 10.12.x.(2n+1)/31
                         10.12.x.2n/31       │ DNAT 10.11.0.n → 169.254.0.21
                                            tap0 169.254.0.22/30 ─────────── eth0 169.254.0.21
```

逐项对比（"上游 e2b"指 2026.09 的 `packages/orchestrator/internal/sandbox/network/`，
AgentENV 指 main `23e46a3` 的 `src/sandbox/network/` 与 `src/cfg/network.rs`）：

| 资源 | 上游 e2b | AgentENV 默认 | 撞不撞 |
|---|---|---|---|
| 宿主机侧 veth | `veth-<n>`（`slot.go` 的 `VethName()`） | `veth-<n>`（`HOST_VETH_PREFIX = "veth-"`） | **撞** |
| 槽位地址池 | `10.11.0.0/16`（`SANDBOXES_HOST_NETWORK_CIDR`） | `10.11.0.0/16`（`network.internal.host_interaction_cidr`） | **撞** |
| veth 地址池 | `10.12.0.0/16`，每槽 /31（`SANDBOXES_VRT_NETWORK_CIDR`） | `10.12.0.0/16`，每槽 /31（`veth_cidr`） | **撞** |
| 宿主机路由 | `10.11.x.y/32 via 10.12.…` | 同左 | **撞** |
| netns | `/var/run/netns/ns-<n>` | `/run/aenv/netns/agentenv-ns-<uuid>` | 不撞 |
| netns 内 | `tap0` 169.254.0.22/30，guest 169.254.0.21 | 同左 | 不撞（各在各的 netns） |
| guest 里的 envd | 49983 | 49983 | 不撞（在 guest 里） |
| firecracker 二进制 | `/fc-versions/…` | `/var/lib/aenv/deps/firecracker/…` | 不撞 |
| 块设备 | nbd | ublk | 不撞 |
| 宿主机端口 | 3000–3003、5007–5018、4646–4648、8300–8302/8500/8600、2900、9000、5432、6379、53、80、443 | 8000（API）、9103（指标）；podman 里的 `aenv-registry` 占 5000 | 不撞（e2b 里用 5000 的 docker-reverse-proxy 不在 `deploy.sh` 部署的 job 里） |

AgentENV 自己也意识到可能有别的沙箱系统：它启动时扫描已有的 `veth-<n>` 并跳过这些编号，日志里还会告警
`another sandbox runtime may already be active`。但这只能避开"它启动那一刻已经存在"的编号，
避不开之后 e2b 新建的槽位，也挡不住下面两条规则。

## 2. 撞在哪里

### 2.1 网卡名与路由

内核不允许同名网卡，也不允许同一条 /32 路由出现两次。e2b 的暖池（0001 补丁里 `NewSlotsPoolSize = 300`）
在 orchestrator 启动时一次预建约 300 个槽位，从 `veth-1`、`10.11.0.1/32` 开始；
只要 AgentENV 占着其中任何一个，e2b 的槽位池就建不起来。

这台机器部署前的状态（AgentENV 的暖池）：

```
veth-1 … veth-11              10.12.0.2/31 … 10.12.0.22/31
10.11.0.1 via 10.12.0.3 dev veth-1
…
10.11.0.11 via 10.12.0.23 dev veth-11
```

### 2.2 AgentENV 的 INPUT 规则拒掉 e2b 沙箱

AgentENV 启动时在宿主机装的全局规则（`manager.rs` 的 `global_host_iptables_commands`）：

```
-A INPUT   -s 10.11.0.0/16 -i veth-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A INPUT   -s 10.11.0.0/16 -i veth-+ -j REJECT --reject-with icmp-port-unreachable
-A FORWARD -s 10.11.0.0/16 -i veth-+ -j ACCEPT
-A FORWARD -d 10.11.0.0/16 -o veth-+ -m state --state RELATED,ESTABLISHED -j ACCEPT
-t nat -A POSTROUTING -s 10.11.0.0/16 -j MASQUERADE
```

`veth-+` 匹配所有 `veth-` 开头的网卡，不分是谁的。e2b 沙箱的出网 TCP 会先被 REDIRECT 到宿主机上
orchestrator 的 TCP 代理（80→5016、443→5017、其余→5018），hyperloop 是 5010，NFS 是 5011——
被 REDIRECT 的包目的地是本机，要走 INPUT，源地址又正好在 10.11.0.0/16，于是被第二条拒掉。
结果是 AgentENV 在跑时，e2b 沙箱即使建起来，出网 TCP 和 hyperloop 也都不通。

（这台机器上这组规则每条出现了两次，是 AgentENV 某次退出没走清理、下次启动又装了一遍，对功能无影响。）

### 2.3 e2b 的 80 端口规则截走别人的 http

`build.sh -s` 收尾时装的规则原本是：

```
-t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 3002
```

本意是让别的机器访问本机 80 端口时交给 client-proxy（`*.e2b.app` 的入口）。它没有限制入口网卡也没有限制目的地址，
所以**经本机转发**的所有 80 端口 TCP 都被它截走：AgentENV 沙箱、docker/podman 容器访问外网 http 都会被送到
e2b 的 client-proxy。

它对 e2b 自己也有害，rollback 文档附录 C §13.1 记录过：orchestrator 给每个槽位装的 hyperloop 规则
（`-d 192.0.2.1 -i <veth> --dport 80 -j REDIRECT --to-ports 5010`）是建槽位时才追加的，永远排在这条后面，
guest 访问 hyperloop 的连接全被转给 client-proxy，16 个沙箱两小时打出约 9 GB 的 `invalid host` 日志。

### 2.4 e2b 的 `-d` 删掉 AgentENV 的网卡

`build.sh -d` 和 `--recycle-netns` 的 `purge_sandbox_netns` 原本删除所有匹配 `^veth-[0-9]+$` 的网卡，
只跳过 e2b 自己里面还有进程的 `ns-<n>`。AgentENV 的网卡名一样、又不在跳过名单里，会被全部删掉，
它正在跑的沙箱当场断网。`purge_sandbox_iptables` 同理会删 `-i veth-<n>` 的规则和 `10.11.x.y/32` 的 MASQUERADE。

### 2.5 两种部署顺序

- **先起 AgentENV，再起 e2b**：e2b 预建槽位撞名失败（2.1）；建得起来的槽位也被 REJECT（2.2）。e2b 不能用。
- **先起 e2b，再起 AgentENV**：AgentENV 跳过已有编号，自己能起来；但它的 REJECT 让 e2b 沙箱随即断网（2.2）；
  之后 e2b 补充暖池时撞上 AgentENV 的编号（2.1）；e2b 一跑 `-d`，AgentENV 的网卡被删（2.4）。

这就是"先起来的那个能用、另一个就不行"。

## 3. e2b 这边改了什么

| 改动 | 位置 | 为什么 |
|---|---|---|
| 宿主机侧 veth 改名 `e2bv-<n>` | 0001 补丁：`packages/orchestrator/internal/sandbox/network/slot.go` 的 `VethName()` | 网卡名不再撞（2.1）；AgentENV 的 `veth-+` 规则匹配不到 e2b 的网卡（2.2）。orchestrator 里所有用到网卡名的地方（建网卡、各条 iptables 规则、TCP 代理）都经过这个函数，改一行即可 |
| 地址池改成 `10.13.0.0/16` / `10.14.0.0/16` | `e2b-deploy/dep/.env` 的 `SANDBOXES_HOST_NETWORK_CIDR` / `SANDBOXES_VRT_NETWORK_CIDR`，加进 `deploy.sh` 的 envsubst 白名单，由 `template-manager.hcl` 的 `env {}` 传给 orchestrator | 槽位地址、veth 地址、/32 路由都不再重叠（2.1）。上游本来就读这两个环境变量，代码不用改 |
| 清理逻辑只认 e2b 自己的 | `build.sh` 的 `purge_sandbox_netns` / `purge_sandbox_iptables`：网卡按 `SANDBOX_VETH_PREFIX`（`e2bv-`）匹配；MASQUERADE 按 `.env` 的地址池做网段成员判断（`host_cidr_masquerade_rules`），不再写死 `10\.11\.` | `-d` / `--recycle-netns` 不再碰 AgentENV 的网卡和它那条 `-s 10.11.0.0/16 -j MASQUERADE`（2.4） |
| 80 端口规则只接管目的地址是本机的流量 | `build.sh` 的 `start()`：`-m addrtype --dst-type LOCAL`，并先删掉老规则 | 转发流量（AgentENV 沙箱、容器、e2b 自己的沙箱）不再被截（2.3）。hyperloop 的 `192.0.2.1` 没有配在任何网卡上，不算本机地址，所以 hyperloop 也恢复了。附录 C 当初设想的 `! -i veth+` 只管得了 e2b 自己的沙箱，而且同机 AgentENV 的网卡也叫 `veth-` |

同一次部署里顺带暴露、一起改掉的几处（都不是网络冲突本身，但新机器上会挡路）：

| 改动 | 位置 | 为什么 |
|---|---|---|
| `/etc/resolv.conf` 不存在时直接写一行 `nameserver 127.0.0.1` | `start-client.sh` | 这台机器没有这个文件（上外网走 `http_proxy`），原来的 `sed -i` 在 `set -e` 下直接让 `build.sh -s` 退出。只写回环地址、不补上游：AgentENV 读这个文件给沙箱选 DNS，它跳过回环地址，只有这一行时行为与文件不存在时一致 |
| 静态大页池上限 | `start-client.sh` / `init-client.sh`（两份相同的大页代码），`.env` 的 `HUGEPAGES_BASE_MAX_MIB=65536` | 静态池原来是"大页可用内存的 20%"，这台机器上约 444 GB，开机就占住、AgentENV 用不了。现在取 min(20%, 64 GB)；按需部分（overcommit 80%）不变 |
| `harbor` 主机名与基础镜像 | `build.sh` 的 `ensure_harbor_hostname` / `push_base_image` | 原来都是部署指南 §1.3 的手工步骤，新机器上漏掉就建不出模板。推送走 `<SERVER_IP>:2900`：docker daemon 的代理配置里 `NO_PROXY` 没有 `harbor`，走 `harbor:443` 会被代理回 504 |

## 4. 以后要换网段或前缀，动哪几处

**换网段**只改 `.env` 的两个变量，重新出包（或直接改 `/opt/e2b-infra/dep/.env`）后 `-d` → `-i` → `-s`：

- 两段不能互相重叠，不能与本机、内网、docker（172.17）、podman（10.88）、AgentENV（默认 10.11/10.12，
  看它的 `/var/lib/aenv/config/config.toml` 的 `[network.internal]`）在用的网段重叠。
- VRT 段决定单机槽位上限：可用槽位 = 地址数 / 2 − 2，`/16` 约 3.2 万个。
- `build.sh` 的清理按 `SANDBOXES_HOST_NETWORK_CIDR` 计算，任何前缀长度都可以，不用跟着改。
- 文档里举例的 10.13/10.14（部署指南 §0.3、§0.6，rollback 第 26 章的 conntrack 统计命令）要顺手改。

**换前缀**要动两处，必须一致：

1. 0001 补丁里 `slot.go` 的 `VethName()`（网卡名最长 15 个字符，`<前缀><槽位号>` 要放得下）；
2. `build.sh` 的 `SANDBOX_VETH_PREFIX`。`benchmark/run_benchmark.py` 的 `sandbox_veth` 计数也跟着改。

> ⚠️ **补丁改动要同步回源码开发仓库**。这次的 `slot.go` 改动是直接改在 `0001-adapted-for-arm-architecture.patch`
> 上的（在上游 2026.09 + 原补丁的源码树上改完，只重新生成了 `slot.go` 这一段）。08 篇的出包流程是在开发仓库里
> `git diff upstream HEAD` 整体重新生成补丁——开发仓库里不做同样的改动，下次重新生成时 `e2bv-` 就丢了，
> e2b 回到 `veth-<n>`，本篇的冲突全部回来。

## 5. 部署前查什么、部署后核对什么

命令在部署指南 §0.6。要点：

- **部署前**：新地址池在本机没人用（路由、网卡地址、iptables、docker/podman 网络里都没有 10.13/10.14）；
  记下 AgentENV 的现状——`veth-<n>` 个数、`10.11.` 路由条数、`iptables-save` 里 `veth-`/`10.11.` 相关的规则、
  `systemctl is-active aenv`，部署后逐行对比。
- **部署后**：`e2bv-<n>` 有几百个、`10.13.` 路由条数与之相当；`PREROUTING` 里 3002 只剩带 `--dst-type LOCAL`
  的一条；AgentENV 那份记录与部署前一致；两边各建一个沙箱。

这台机器 2026-11-02 的实测：`e2bv-` 288 个、`10.13.` 路由 288 条、`HugePages_Total` 32768（64 GB）、
AgentENV 记录逐行一致，`benchmark/build_template.py` 与 `sandbox.py` 跑通。

**装过老版本 e2b 的机器**：新的清理逻辑不再碰 `veth-<n>` 和 10.11，老残留要在确认同机没有 AgentENV 之后
手工清一次，命令同样在部署指南 §0.6。有 AgentENV 时这些名字和网段是它在用的，不能清。

## 6. 为什么全在 e2b 一侧改

- AgentENV 由别人负责；它的网卡前缀是代码里的常量，地址池虽然能在配置里改，但改了要重启它的服务、
  影响它已有的沙箱。e2b 这边的部署件和补丁都在本仓库里。
- e2b 一侧的改动面很小：一行 Go、两个环境变量、`build.sh` 里两个清理函数和一条规则。
- 0001 补丁本来就在改这个目录（`pool.go` 的暖池大小、`slot.go` 的 conntrack 句柄），多一处改动不增加维护负担，只需记得同步回开发仓库（§4）。

## 7. 地址分开之后仍然共用的东西

| 共享资源 | 风险 | 这台机器的值 | 怎么看 |
|---|---|---|---|
| 邻居（ARP）表 | 每张宿主机侧 veth 一有流量就占一条表项，两边加起来超过 `gc_thresh3` 后内核报 `neighbour table overflow` 并丢包 | 4096 / 8192 / 16384（已调过） | `sysctl net.ipv4.neigh.default.gc_thresh{1,2,3}`、`ip -4 neigh \| wc -l` |
| conntrack 表 | 全机一张，满了两边的新连接都被丢 | `nf_conntrack_max` 1048576 | `sysctl net.netfilter.nf_conntrack_{max,count}` |
| 宿主机网卡总数 | 几万张网卡时 `ip link`、netlink 遍历变慢，nomad 的 client fingerprint 要枚举全部网卡，4646 迟迟不开（05 篇） | 部署后约 310（e2b 288 + AgentENV 10 + 其他） | `ip -o link \| wc -l` |
| 宿主机 iptables 规则数 | e2b 每个槽位在宿主机上约 9 条规则，几万个槽位时每次调用都要整表读写；AgentENV 的单槽位规则在各自 netns 里 | — | `iptables -S \| wc -l; iptables -t nat -S \| wc -l` |
| 大页 / 内存 | 静态池开机就占住；overcommit 部分按需分配，负载高时 e2b 最多能拿到"大页可用内存"的 80% + 静态池 | 静态 64 GB，overcommit 上限约 1775 GiB | `grep -E '^HugePages_(Total\|Free\|Surp)' /proc/meminfo` |
| nbd 设备（e2b 独用） | 每个运行中的 e2b 沙箱占一个，`nbds_max` 就是 e2b 单机同时运行沙箱数的上限 | 512（内核自带模块） | `cat /sys/module/nbd/parameters/nbds_max` |

几百个沙箱用不着调这些；往几万个走时先查这几项。

**静态大页池怎么定**：静态池回答的是"e2b 有多少内存需要保证有"，不是 e2b 的用量上限。先用 64 GB，
跑几天正常负载、记录 `HugePages_Total − HugePages_Free`（实际用量）和 `HugePages_Surp`（超出静态池、由按需部分补上的量）。
从来没出现过因为分不到大页导致的创建失败（firecracker 报 `Cannot allocate memory`，`dmesg` 里有 hugetlb 报错）就不用加；
出现过，就把静态池调到平时用量的 p50～p90，改 `.env` 的 `HUGEPAGES_BASE_MAX_MIB`。

## 8. 变更记录

- 2026-11-02：首次在已有 AgentENV 0.2.0 的机器上部署。veth 改名 `e2bv-`、地址池改 10.13/10.14、清理逻辑与 80 端口规则收窄、
  resolv.conf 缺失处理、静态大页池上限 64 GB、harbor 主机名与基础镜像自动化。
