# 23 · 构建、部署前提与能力自检

客户已有 E2B 并使用自己的部署流程。本章简明说明本特性的源码与组件配对、平台条件和能力检查；950 的内部打包安装方式作为环境参考记录在附录 E。

## 1. 源码构建与组件配对范围

三个组件均从已选定的 KASandbox 提交取得，采用客户自己的构建、打包和部署方式。orchestrator 通过扩展接口调用 Firecracker，SDK 与服务端按同一协议配对。

| 组件 | 源码位置 | 本特性核对点 |
|---|---|---|
| orchestrator | `packages/orchestrator/` | checkpoint 服务、代理拦截、脏页跟踪和能力信息；构建入口为该目录的 `make build` |
| Firecracker | `firecracker/` | rollback、save-dirty-bitmap、位图格式和 ARM64 seccomp；现有构建入口为 `make build` / `scripts/build.sh` |
| Python SDK | `py-sdk/` | `Sandbox.checkpoint`、异步实现、异常和协议桩 |

构建工具链与运行依赖沿用仓库要求。记录实际源码 SHA 与构建产物 sha256；不能仅凭目录名或 `firecracker --version` 判断功能版本。内部 RPM、Nomad job 和 SDK 覆盖层只是一种测试部署方式，见[附录 E](E-environment-and-tools.md)。

## 2. 先分清两层：要不要记脏页、用什么方式记

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

内部部署模板（`e2b-deploy/dep/template-manager.hcl`）**有意不传**这个变量：950 上不需要。
没有 HDBSS 的机器不设时默认关，是因为软件写保护要让每个干净页的第一次写入陷出一次，对从不做 checkpoint 的沙箱是净亏（原理见 [第 5 章](05-dirty-tracking-and-hdbss.md)）。
取值只认 Go `strconv.ParseBool` 的写法，`yes`、`on` 之类会被忽略并打 WARN，详见 [第 24 章 §2.1](24-configuration-and-capacity.md#21-脏页跟踪与-hdbss)。

## 3. 平台前提

以下检查同时包含 ARM64 实现前提和内部测试环境设置。NBD 数量、大页容量及服务路径按客户负载与部署确定，不能把内部的 512 个 NBD 或固定目录当作所有环境的必需取值。逐项核对。"怎么查"一列的命令都是只读的。

| # | 前提 | 怎么查 | 不满足时 |
|---|---|---|---|
| 1 | aarch64；回滚路径（GIC 清线、vCPU 重置、HDBSS）只在 aarch64 上实现 | `uname -m` | 不能用 |
| 2 | 宿主内核：openEuler 24.03 LTS-SP4，带 KVM 与 userfaultfd；920B 历史实测为 `6.6.0-159.4.3.154.oe2403sp4.aarch64`，950 的 2026-10-13 实测为 `6.6.0-159.4.12.166.oe2403sp4.aarch64` | `uname -r`；`grep -E 'CONFIG_KVM=\|CONFIG_USERFAULTFD=' /boot/config-$(uname -r)` | 换内核后第 6 项的 `nbd.ko` 也要重编 |
| 3 | `/dev/kvm` 存在且 root 可读写；KVM 跑在 VHE 模式 | `ls -l /dev/kvm`；`dmesg \| grep -Ei 'kvm.*VHE mode initialized'` | 沙箱起不来；非 VHE 时 HDBSS 启用会被内核拒绝 |
| 4 | **HDBSS**：内核编了 `CONFIG_ARM64_HDBSS=y`，并且 CPU 支持 KVM 能力502；仅有内核配置不足 | 核对内核配置、当前能力文件的 `track_dirty_pages` 与原因，以及实际FC后端；辅助预检可真开探针 | 若未探到能力且未显式开跟踪，则为`off`，checkpoint全量；只有已武装跟踪而FC启用HDBSS失败时，才按`FC_HDBSS_REQUIRED`决定拒绝或退回`kvm-wp` |
| 5 | GICv3 | `preflight-customer.sh` 第 2 节 | v2 未实测 |
| 6 | NBD 模块可用，vermagic 与运行内核一致，设备数量满足并发需求；内部测试设 `nbds_max=512` | `lsmod \| grep -w nbd`；`cat /sys/module/nbd/parameters/nbds_max`（内部测试为512） | 沙箱根盘走 NBD，没有它沙箱起不来。按客户的内核模块管理流程配置 |
| 7 | `tun` 模块与 `/dev/net/tun` | `ls -d /sys/module/tun /dev/net/tun` | 沙箱网卡起不来。`preflight-customer.sh` 对 `vhost_net` 只给 INFO：当前 Firecracker 网卡在用户态实现、直接读写 tap，不依赖它 |
| 8 | `net.ipv4.ip_forward=1`，**且在创建槽位 netns 之前生效**（槽位 netns 在创建时继承这个值） | `sysctl -n net.ipv4.ip_forward` | 建模板在等 envd 那一步 60 s 超时。已经装完才发现要重建整池槽位，按实际槽位管理流程重建 |
| 9 | 2 MiB 大页：guest 内存走 hugetlb，`init-client.sh` 启动时预留一部分，其余走 overcommit | `grep -i huge /proc/meminfo`；`cat /proc/sys/vm/nr_overcommit_hugepages` | 沙箱数被大页数卡住 |
| 10 | **产物盘**：store 根 `/orchestrator/build/checkpoints`（由 `ORCHESTRATOR_BASE_PATH`、`DEFAULT_CACHE_DIR` 决定，目录布局见 [第 4 章 §5](04-architecture-and-flow.md#5-数据面目录布局)）所在文件系统为 **ext4**，支持稀疏文件 | `df -T /orchestrator/build` | 交付方案只在 ext4 上验证过；为什么是 ext4 见 [第 3 章 §9](03-goals-and-boundaries.md#9-为什么交付用-ext4) |
| 11 | 产物盘与沙箱缓存盘（`/orchestrator/sandbox`，活写层所在）是**同一个文件系统** | `findmnt -no SOURCE --target /orchestrator/build; findmnt -no SOURCE --target /orchestrator/sandbox` 两行相同 | 封层从 rename 退化为拷贝，checkpoint 变慢 |
| 12 | 产物盘余量：按 [第 24 章 §4](24-configuration-and-capacity.md#4-容量规划) 估；服务端另有水位闸，低于 max(4 GiB, 2 × guest 内存) 时拒绝 checkpoint 与 restore | `df -h /orchestrator/build` | 507 `disk_full` |

把 2–11 项一次查完的脚本是 `rollback/scripts/crtest/portability/preflight-customer.sh`（零依赖、只读，退出码 = FAIL 项数），在 clone 下来的 e2b-infra 仓库根目录执行：

```bash
bash rollback/scripts/crtest/portability/preflight-customer.sh
```

它也是 [第 20 章](20-acceptance.md) 里 `run.sh smoke` 的第一项。`WARN` 不算失败，但要逐条看一眼说明。

## 4. 版本配对

orchestrator、Firecracker 和 Python SDK 从选定的 KASandbox 版本配对。guest 内核与模板格式应满足实际平台要求；客户可以沿用自己的部署路径和服务管理方式。

| 组件 | 要核对的内容 |
|---|---|
| orchestrator | checkpoint服务、代理路由、能力字段与所选源码版本一致；记录运行产物sha256 |
| Firecracker | rollback、save-dirty-bitmap及位图格式与orchestrator配套，seccomp覆盖扩展路径；记录运行路径与sha256 |
| guest内核与模板 | 内核、设备拓扑和模板版本与当前运行方式匹配 |
| SDK | `Sandbox.checkpoint`、同步/异步接口、返回字段和异常与服务端协议配套 |

orchestrator按 `$FIRECRACKER_VERSIONS_DIR/<模板记录的FC版本>/firecracker` 选择文件。内部测试的模板查找键是 `v1.13.1`，二进制自报上游基线版本为 `1.12.1`；两种字符串均不能单独证明包含本特性。客户核对自己模板实际选择的路径和构建哈希，不固定替换一个与模板无关的目录。

950的2026-10-15 smoke已核对选定路径、回滚接口、部署产物哈希与功能；详见[第 17 章 §4](17-functional-verification.md#4-950-smoke部署预检与正确性验收)。能力文件自报的`commit=4853232`仍需与源码/内部RPM对应核实，不代替二进制哈希。历史RPM路径样例在[附录 E §6](E-environment-and-tools.md#6-内部-rpm-的路径示例)。

| 配法 | 表现 |
|---|---|
| 新orchestrator与未扩展的FC | `dirty_bitmap_path`、save-dirty-bitmap或rollback不受支持，明确报错；不会因此静默提供原地回滚 |
| 旧orchestrator与新FC | 原有沙箱路径可继续工作，但没有新的checkpoint服务入口 |
| SDK不含配套checkpoint实现 | `Sandbox.checkpoint`不存在 |
| SDK或外部验收工具过旧 | 可能缺少`mem_mode`及其检查，不能用较少的检查项证明增量生效 |
| 内核或NBD模块更换 | 核对模块vermagic与运行内核，以及实际并发设备能力 |

## 5. 二进制是不是这一套

记录当前运行的 orchestrator 和 Firecracker 的执行路径与 sha256，再与客户选定的构建产物核对。以下用内部进程名 `template-manager` 举例；采用其他进程名时替换为实际 PID：

```bash
ORCH_PID=$(pidof -s template-manager)
readlink /proc/"$ORCH_PID"/exe
sha256sum /proc/"$ORCH_PID"/exe
for fc_pid in $(pidof firecracker); do
    readlink /proc/"$fc_pid"/exe
    sha256sum /proc/"$fc_pid"/exe
done
```

运行路径和哈希应与实际构建记录一致。模板指定的 FC 版本决定选择哪个目录，不能只替换另一份同名文件；不同测试轮次的哈希以各轮结果为准。

## 6. orchestrator 自报的能力

以下涉及 `template-manager`、Nomad alloc 或辅助脚本的命令采用内部950环境约定；客户替换为实际进程、日志与工具路径。

orchestrator 启动时打一行 `checkpoint capabilities`，说清脏页跟踪开没开、为什么，以及各项限额的生效值和来源（字段逐个解释在 [第 24 章 §3](24-configuration-and-capacity.md#3-启动能力行)）。
从 deltabox-dev `93ccb02` 起，同样的内容还写进**能力文件** `checkpoint-capabilities.json`：在 `DEFAULT_CACHE_DIR` 下（默认 `/orchestrator/build/`，即 store 根的父目录），
另带 `pid`、`started_at`、`version`、`commit` 四个键（[第 24 章 §3.1](24-configuration-and-capacity.md#31-能力文件)）。
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

## 7. 宿主预检

以下涉及 `template-manager`、Nomad alloc 或辅助脚本的命令采用内部950环境约定；客户替换为实际进程、日志与工具路径。

[§3](#3-平台前提) 那条 `preflight-customer.sh`，`FAIL 0`。

## 8. 网络槽位水位基线

以下涉及 `template-manager`、Nomad alloc 或辅助脚本的命令采用内部950环境约定；客户替换为实际进程、日志与工具路径。

```bash
ip netns list | grep -cE '^ns-[0-9]+$'
```

记下这个数。正常水位 = 300（预建池）+ 历史峰值并发；每重启一次 orchestrator 会多漏约一池（[第 26 章 §9](26-troubleshooting.md#9-网络槽位netns泄漏)）。
刚启动时这个数还在涨（暖池填充），**做任何性能测试前等它连续几分钟不变**。

## 9. 增量真的生效

以下涉及 `template-manager`、Nomad alloc 或辅助脚本的命令采用内部950环境约定；客户替换为实际进程、日志与工具路径。

这一步要建沙箱，用 [第 20 章](20-acceptance.md) 的 `run.sh smoke` 一次做完。smoke 里的 `checkpoint_verify.py` 开头打印
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
