# E2B 单机部署流量处理架构详解

> 适用场景:e2b-infra 单机 all-in-one 离线部署(Nomad server+client 合体、Consul 服务发现、dnsmasq 本地 DNS、iptables 端口改道、firecracker microVM 沙箱)。
>
> 本文覆盖:DNS 解析原理(resolv.conf / glibc resolver / dnsmasq)、iptables 端口改道、Consul 服务发现、三类流量的完整链路、为什么必须这样设计(而不是别的方案)、验证与排障。

---

## 目录

1. [总体架构](#1-总体架构)
2. [地址与端口体系](#2-地址与端口体系)
3. [DNS 层详解](#3-dns-层详解)
4. [流量导向层(iptables)详解](#4-流量导向层iptables详解)
5. [三类流量的完整链路](#5-三类流量的完整链路)
6. [设计取舍:为什么必须这样做](#6-设计取舍为什么必须这样做)
7. [验证与排障](#7-验证与排障)
8. [已知坑与持久化问题](#8-已知坑与持久化问题)
9. [分层职责总结](#9-分层职责总结)

---

## 1. 总体架构

e2b 的流量处理由**三个层次**协作完成,每层只做自己层次的事,不可互相替代:

| 层次 | 组件 | 职责 | 回答的问题 |
|---|---|---|---|
| DNS 解析层 | dnsmasq + Consul DNS | 名字 → IP | "这个域名在哪台机器?" |
| 流量导向层 | iptables (nat 表) | 端口改道 | "到了这台机器后进哪个端口?" |
| 应用路由层 | e2b 代理 (client-proxy/edge, :3002) | 请求分发 | "这个请求给 api 还是给某个沙箱?" |

流量分**三大类**,走不同的路:

- **控制流量**:创建/销毁/管理沙箱,最终打到 `api` 服务;
- **沙箱流量**:连进某个具体沙箱执行代码/读写文件/访问沙箱内服务,最终打到 microVM 里的 `envd`;
- **内部服务互访**:e2b 各组件之间通信(api→redis、代理→api 等),靠 Consul 服务发现直连,**不经过** 80/3002 公共入口。

全景数据流(简化):

```
[SDK / 浏览器 / 外部客户端]
   │ ① DNS: xxx.e2b.app → dnsmasq → 127.0.0.1
   ▼
[127.0.0.1:80]  (http 默认端口,客户端自动补的)
   │ ② iptables: 80 → 3002
   │    (外部流量走 PREROUTING,本机自访问走 OUTPUT+lo)
   ▼
[e2b 代理 :3002]
   │ ③ 按请求类型路由
   ├── 控制请求 → api → Postgres/redis 校验 → orchestrator → firecracker 起沙箱
   └── 沙箱请求 → microVM 内网接口 → envd 执行 → 结果原路返回

[任意内部组件] ── 查 *.service.consul ──> dnsmasq ──> Consul:8600 ──> 真实 IP 直连
```

---

## 2. 地址与端口体系

### 2.1 两类域名

| 域名 | 解析方式 | 解析结果 | 用途 |
|---|---|---|---|
| `*.e2b.app`(如 `<sandbox-id>.e2b.app`) | dnsmasq 直接回答 | `127.0.0.1`(本机) | 沙箱访问入口、api 入口 |
| `*.service.consul`(如 `redis.service.consul`) | dnsmasq 转发给 Consul :8600 | 该服务**当前的真实动态 IP** | 内部服务发现 |

### 2.2 关键端口

| 端口 | 谁在监听 | 说明 |
|---|---|---|
| 53 | dnsmasq | 本机 DNS 总机,所有 DNS 查询的第一站 |
| 8600 | Consul | Consul 的 DNS 接口(**非标准端口**,resolv.conf 指不到它,必须靠 dnsmasq 中转) |
| 80 | (无人监听) | http 默认端口,流量到达后被 iptables 改道 |
| 3002 | e2b 代理 (client-proxy/edge) | 真正的流量入口,所有 80 流量被改道到这里 |
| 4646 | Nomad HTTP API | 调度器接口 |
| 8500 | Consul HTTP API | 服务发现接口 |
| 2900 | Harbor | 私有镜像仓库(HTTP) |
| 5432 / 9000 / 6379 | Postgres / MinIO / redis | 元数据库 / 对象存储 / 缓存 |

### 2.3 骨架路径

任何 `http://xxx.e2b.app` 的访问,公共入口路径固定为:

```
DNS 解析 xxx.e2b.app → 127.0.0.1
  → 客户端按 http 协议默认连 127.0.0.1:80
  → iptables 将 80 改道到 3002
  → e2b 代理接手,做应用层路由
```

"DNS 指本机 + iptables 改端口"这个组合是一切外部/本机入口流量的公共前半段。

---

## 3. DNS 层详解

### 3.1 resolv.conf 原理

`/etc/resolv.conf` 是 Linux 的 **DNS 客户端配置文件**。关键词是"客户端"——它不提供任何 DNS 服务,只是告诉本机程序"做域名解析时去问谁"。

```
nameserver 127.0.0.1      ← 第一个被问的(必须是 dnsmasq)
nameserver 8.8.8.8        ← 备胎,平时轮不到
```

`nameserver 127.0.0.1` 的含义是:"向本机(回环地址)的 **53 端口**发 DNS 查询"。注意:

- **resolv.conf 不保证 53 端口上真有服务。** 它只是"指路牌"。如果 127.0.0.1:53 没人监听(dnsmasq 没跑),查询就超时失败。
- **nameserver 行不能指定端口。** 永远默认 53。这是 Consul DNS(8600)无法被 resolv.conf 直接使用、必须由 dnsmasq 中转的直接原因之一。

#### 谁在读 resolv.conf:glibc resolver

"解析域名"这件事不是每个程序自己实现的,而是由 **glibc(GNU C 标准库,Linux 上几乎所有程序依赖的最底层库)** 提供的统一函数 `getaddrinfo()` 完成:

```
你的程序(curl / python / e2b 组件)
   │ "我要 github.com 的 IP"
   ▼
glibc 的 getaddrinfo()          ← 统一入口
   │ 内部 resolver 读 /etc/resolv.conf
   ▼
按 nameserver 列表向 127.0.0.1:53 发 DNS 查询包
   ▼
dnsmasq 回答 → glibc 把结果返回给程序
```

正因为解析是 glibc 统一做、统一读 resolv.conf,**改一次 resolv.conf,全机器所有程序的 DNS 指向都随之改变**。这就是脚本只改一个文件就能让整机 DNS 走 dnsmasq 的原因。

> 例外说明:少数程序不完全走 glibc(如 Go 的纯 Go resolver 会自己读 resolv.conf 直接发查询,行为类似;systemd-resolved 托管的系统另有机制)。在本部署中按"绝大多数程序经 glibc 读 resolv.conf"理解即可。

#### nameserver 的顺序规则(重要)

glibc resolver 对多个 nameserver 的默认行为:

1. **顺序尝试,非并行、非轮询**:永远先问第一个;
2. **只要第一个正常回答(哪怕答"域名不存在"),就采纳,不再问后面的**;
3. 只有第一个**完全无响应(超时)**时才 fallback 到第二个;
4. **最多只认前 3 个 nameserver**(glibc 写死的 `MAXNS = 3`),第 4 行起被忽略;
5. `options rotate` 可改为轮询均摊——**本部署绝不能加**,因为一旦查询轮到公网 DNS,`.consul`/`.e2b.app` 就解析失败。

由此得出两个关键结论:

- **127.0.0.1 必须放第一行**(脚本用 `sed '1i'` 强制插入首行),否则查询先被别的 DNS 答掉,永远轮不到 dnsmasq,内部域名全废;
- **127.0.0.1 后面的 nameserver 平时永远不生效**;只有 dnsmasq 挂掉时才 fallback 生效,且即使生效也只能兜底普通上网——公网 DNS 不认 `.consul`/`.e2b.app`,此时 e2b 内部解析全废,并且每次查询要先等 127.0.0.1 超时几秒,整机 DNS 又慢又半残。因此后面的备胎价值很低,真正要保证的是**第一行的 dnsmasq 别挂**。有一派做法干脆只留 127.0.0.1,让 dnsmasq 挂掉时彻底失败、快速暴露问题,而不是进入隐蔽的降级状态。

### 3.2 dnsmasq 原理与角色

dnsmasq 是一个轻量级本地 DNS 服务器,在本部署中扮演 **"DNS 总机 / 分流器"**:

1. **在 127.0.0.1:53 监听**,接收全机的 DNS 查询(resolv.conf 把大家引到这);
2. **按域名后缀分流**:自己直接答、转给特定上游、或转给默认上游;
3. **缓存**结果加速重复查询。

处理一个查询时的判断顺序:

```
收到查询
  ├─ 匹配某条 address=/后缀/ 规则?  → 是 → 自己直接返回规则里的 IP(不问任何人)
  ├─ 匹配某条 server=/后缀/ 规则?   → 是 → 转发给规则指定的上游 DNS 服务器
  └─ 都不匹配                       → 转发给默认上游(公网 DNS)
```

**resolv.conf 和 dnsmasq 是"指路牌"和"被指的那个人"的关系,缺一不可:**

| 组合 | 结果 |
|---|---|
| 有 127.0.0.1 指路 + dnsmasq 在跑 | ✅ 正常:全部查询正确分流 |
| 有 127.0.0.1 指路 + dnsmasq 没跑 | ❌ 最糟:查询发到空地址超时;若有备胎则慢速降级且内部域名全废;若无备胎则整机 DNS 瘫痪 |
| 没有 127.0.0.1 + dnsmasq 在跑 | ❌ 白跑:查询去了别的 DNS,规则形同虚设,内部域名解析失败 |
| 两者都没有 | ❌ e2b 域名和服务发现均不可用 |

### 3.3 两条核心配置:`address=` vs `server=` 深度对比

本部署给 dnsmasq 配了两条规则,**做的是完全不同的两件事**:

#### 规则一(`-i` 阶段写入主配置):

```
address=/.e2b.app/127.0.0.1
```

- 语义:**dnsmasq 自己直接回答**。任何 `*.e2b.app` 的查询,不转发给任何人,直接捏造一个 A 记录返回 `127.0.0.1`。
- 这里的 `127.0.0.1` 是**解析结果本身**(要塞进 A 记录返回给程序的答案)。
- **不能带端口**:因为 A 记录的数据结构里只有 IP 字段,没有端口字段(DNS 协议的根本限制,详见 §6.1)。`address=/.e2b.app/127.0.0.1#3002` 是非法语法。

#### 规则二(`start-client.sh` 阶段写入 /etc/dnsmasq.d/consul.conf):

```
server=/consul/127.0.0.1#8600
```

- 语义:**转发**。任何 `*.consul` 的查询,dnsmasq 自己不答,转身去问 `127.0.0.1:8600` 那个 DNS 服务器(即 Consul 的 DNS 接口),把 Consul 的回答带回给程序。
- 这里的 `127.0.0.1#8600` 是**另一个 DNS 服务器的监听地址**,`#8600` 是"目标 DNS 服务器跑在哪个端口"。
- **端口有意义且必须写**:因为 Consul DNS 不在标准 53,而在 8600。这个端口描述的是"DNS 服务器之间对话的通道",与"解析出来的结果"完全无关。

#### 并排对比

| | `server=/consul/127.0.0.1#8600` | `address=/.e2b.app/127.0.0.1` |
|---|---|---|
| dnsmasq 的动作 | **转发**给另一个 DNS 服务器 | **自己直接回答** |
| `127.0.0.1` 是什么 | 目标 **DNS 服务器**的地址 | 返回给程序的**解析结果 IP** |
| 端口的含义 | 目标 DNS 服务器的**监听端口** | 无此字段(A 记录装不下) |
| 背后有没有真的 DNS 服务 | 有(Consul 在 8600 说 DNS 协议) | 无(纯本地捏造) |
| 是否触碰实际业务流量 | 否,只解析 | 否,只解析(导流交给 iptables) |

**一句话区分**:`server=` 的端口是"我去问别人时,别人的 DNS 服务在哪个端口";`address=` 给的是"解析结果 IP",而解析结果里从来就不含端口。

### 3.4 Consul DNS 与服务发现

**为什么需要服务发现**:e2b 的内部服务(api、edge、redis、template-manager…)由 Nomad 调度,**IP/端口是动态的**——重新调度可能换端口、重启可能换地址,硬编码行不通。

解决方案:服务启动时注册到 Consul,其他组件通过查询 `*.service.consul` 域名获取其**当前**真实地址:

```
组件 A 查 redis.service.consul
  → glibc → 127.0.0.1:53 (dnsmasq)
  → 匹配 server=/consul/ → 转发到 127.0.0.1:8600 (Consul DNS)
  → Consul 查注册表,回答 redis 当前真实 IP(和端口,若走 SRV)
  → 组件 A 直连 redis
```

这条链**不经过** 80/3002 公共入口——内部互访是解析后直连,只有"从外面/SDK 进来"的流量才走代理。

### 3.5 默认上游(普通域名怎么上网)

两条规则都不匹配的域名(github.com、pypi.org…)由 dnsmasq 转发给**默认上游**。默认上游由 **dnsmasq 自己的配置**决定,与系统 resolv.conf 是两回事:

- 配置里不带域名前缀的 `server=8.8.8.8` → 默认上游是 8.8.8.8;
- `resolv-file=/etc/resolv.dnsmasq` → 从指定文件读上游列表;
- 都没配 → dnsmasq fallback 去读 `/etc/resolv.conf`——**此处有鸡生蛋陷阱**:该文件第一行现在是 127.0.0.1(dnsmasq 自己),可能造成自我转发循环或普通域名解析异常。

> ⚠️ 本部署脚本中未见明确的默认上游配置,建议实际验证(见 §7),必要时给 dnsmasq 显式补一条默认 `server=` 或配 `resolv-file`。dnsmasq 对"上游指向自己"有一定的环路检测,但显式配置总是更稳。

### 3.6 为什么必须用 dnsmasq(resolv.conf 直接配不行吗)

三个 resolv.conf 做不到、只有本地 DNS 服务能做的事:

1. **指定非标准端口**:resolv.conf 的 nameserver 只能写 IP,永远问 53;Consul DNS 在 8600,必须有人在 53 接一手再转 8600;
2. **按后缀分流**:`.consul` 走 Consul、`.e2b.app` 指本机、其余走公网——resolv.conf 只有"依次问这几个"一种笨逻辑,无条件分流能力;
3. **本地捏造解析结果**:把 `.e2b.app` 答成 127.0.0.1 是"伪造 A 记录",公网 DNS 不会配合,只能本地 DNS 干。

---

## 4. 流量导向层(iptables)详解

### 4.1 要解决的错位

dnsmasq 把 `*.e2b.app` 解析到 127.0.0.1 之后:

- 客户端按 URL 协议自动补端口:`http://` → **80**;
- 但 e2b 代理实际监听在 **3002**,80 上没人听。

iptables 的任务:在内核网络层把所有到 80 的 TCP 流量**无感知地改道**到 3002,让访问者照常用干净的域名(不用手写 `:3002`)。

### 4.2 前置知识:nat 表与两条链

iptables 的 **nat 表**负责地址/端口转换,包在生命周期的不同阶段经过不同的"链":

- **PREROUTING**:包**刚从网卡进入、路由决策之前**经过。处理**外部进入本机**的流量。
- **OUTPUT**:**本机进程主动发出**的包在离开前经过。处理**本机访问自己**的流量。
- **REDIRECT** 动作:把包的目标端口改成指定端口,目标始终是**本机**(区别于可指向其他 IP 的 DNAT)。

**关键事实:本机发给自己(走 loopback)的包不经过 PREROUTING。** 这决定了必须配两条规则。

### 4.3 两条规则逐条解析

```bash
# 规则一:外部进入的流量
iptables -w -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 3002 2>/dev/null \
  || iptables -w -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 3002

# 规则二:本机自访问(经 loopback)的流量
iptables -w -t nat -C OUTPUT -p tcp -o lo --dport 80 -j REDIRECT --to-port 3002 2>/dev/null \
  || iptables -w -t nat -A OUTPUT -p tcp -o lo --dport 80 -j REDIRECT --to-port 3002
```

逐个参数:

| 参数 | 含义 |
|---|---|
| `-w` | 等待 iptables 锁,避免并发执行时"资源忙"报错 |
| `-t nat` | 操作 nat 表 |
| `-C` | check:仅检查规则是否已存在(存在返回 0) |
| `-A` | append:追加规则 |
| `-p tcp --dport 80` | 匹配 TCP、目标端口 80 的包 |
| `-o lo` | 限定出接口为 loopback,即"本机访问本机"的流量 |
| `-j REDIRECT --to-port 3002` | 把目标端口改成 3002(仍是本机) |
| `-C ... \|\| -A ...` | **幂等**:不存在才添加,重跑脚本不叠加重复规则 |

**为什么第二条不可省略**:单机部署下,SDK、测试脚本、部分组件都跑在本机,访问 `xxx.e2b.app` 解析到 127.0.0.1 后走的是 loopback——这类流量 PREROUTING 完全管不到。没有 OUTPUT+lo 规则,会出现"外部访问正常,本机自己访问 e2b 域名却连不上"的诡异故障,且在单机场景这几乎是一半以上的流量。

### 4.4 覆盖矩阵

```
外部机器 → 本机:80 (经网卡)          → PREROUTING 规则 → :3002 → e2b 代理
本机进程 → 127.0.0.1:80 (经 lo)      → OUTPUT 规则     → :3002 → e2b 代理
```

两条合起来:**无论谁、从哪访问本机 80,都被导到 3002。**

---

## 5. 三类流量的完整链路

### 5.1 控制流量:创建一个沙箱

以本机运行 SDK 代码 `Sandbox.create(...)` 为例,逐跳:

1. **SDK 读凭证**:读 `/root/.e2b/config.json`(deploy 阶段 seed-db 写入的 accessToken / teamApiKey),请求带 key;
2. **DNS 解析**:api 域名(`*.e2b.app`)→ glibc → dnsmasq → 匹配 `address=` → 返回 **127.0.0.1**;
3. **连接 + 改道**:SDK 在本机,连 127.0.0.1:80 走 loopback → **OUTPUT+lo 规则**改到 **3002**;
4. **代理路由到 api**:代理识别为控制请求 → 查 `api.service.consul`(经 dnsmasq→Consul:8600)拿 api 真实地址 → 转发;
5. **api 业务处理**:查 **Postgres** 校验 team/tier 配额(base_v1 已放开到 10000)→ 读写 **redis** → 放行后调用 **orchestrator**(宿主机二进制,raw_exec 运行);
6. **orchestrator 创建 microVM**:
   - hypervisor:`/fc-versions/v1.13.1/firecracker`;
   - guest 内核:`/fc-kernels/vmlinux-6.1.102/vmlinux.bin`;
   - rootfs:从 **MinIO** 拉模板,或从 **/mnt/snapshot-cache(65G tmpfs)** 快照秒级恢复(e2b 启动快的关键);
   - 内存:从**大页(hugepages)池**分配;
   - 磁盘挂载:经 **NBD** 设备(受 nbds_max 限制);
   - 注入 `/fc-envd/envd` 作沙箱内 agent;
   - 配置沙箱网络(宿主 tap/veth + 内网 IP);
7. **回传**:沙箱信息(sandbox-id、访问地址)沿 orchestrator → api → 代理 → SDK 原路返回。

```
SDK → DNS(→127.0.0.1) → :80 → iptables(OUTPUT+lo) → :3002 代理
    → api → Postgres/redis 校验 → orchestrator
    → firecracker(内核 + MinIO/快照 rootfs + 大页 + NBD + envd)
    → 沙箱就绪 → 原路返回
```

### 5.2 沙箱流量:连进沙箱执行代码

调用 `sandbox.run_code(...)` 或访问 `<sandbox-id>.e2b.app`:

1~3. **同上**:DNS→127.0.0.1 → :80 → iptables → :3002 代理;
4. **代理按 sandbox-id 路由**:从域名提取 `<sandbox-id>`,定位该沙箱 microVM 的内网地址(单机下节点即本机);
5. **进入 microVM**:流量经宿主↔沙箱虚拟网络(tap/veth)送入沙箱;
6. **envd 执行**:执行代码(跑解释器,收集 stdout/stderr/结果)、文件读写、或转发到沙箱内自起的服务端口;
7. **结果回传**:envd → microVM 网络 → 代理 → SDK。

```
SDK → DNS → :80 → iptables → :3002 代理
    → 按 sandbox-id 路由 → microVM 内网接口
    → envd(代码/文件/沙箱内服务) → 原路返回
```

控制面与数据面分离:api 只管沙箱的"生老病死",**不参与每次代码执行的数据流**,因此沙箱执行路径短、延迟低。

### 5.3 内部服务互访

e2b 组件之间(api↔redis、代理↔api、api↔orchestrator…)**不走 80/3002 公共入口**:

```
组件 A → 查 xxx.service.consul → dnsmasq → Consul:8600 → 真实 IP:端口 → 直连组件 B
```

解析一次、直连通信,这是 Nomad 动态调度下组件互相寻址的唯一可靠方式。

### 5.4 全景图

```
[SDK / 外部客户端]
   │ ① 读 /root/.e2b/config.json 拿 key
   │ ② 解析 <sandbox-id>.e2b.app
   ▼
[dnsmasq :53] ── address=/.e2b.app/ → 答 127.0.0.1
   │
   ▼
[127.0.0.1:80]
   │ ③ iptables nat
   │    外部流量: PREROUTING 80→3002
   │    本机自访: OUTPUT+lo 80→3002
   ▼
[e2b 代理 client-proxy/edge :3002]
   │ ④ 应用层路由
   ├─ 控制请求 ─→ [api] ─校验→ [Postgres][redis] ─→ [orchestrator]
   │                                                  │ 起 microVM:
   │                                    firecracker + vmlinux 内核
   │                                    + rootfs(MinIO / snapshot-cache tmpfs)
   │                                    + 大页内存 + NBD + 注入 envd
   └─ 沙箱请求 ─→ [microVM 内网 tap/veth] ─→ [envd] ─执行→ 结果原路返回

[任意内部组件] ─ *.service.consul → [dnsmasq] ─ server=/consul/ → [Consul :8600] → 真实地址直连
```

---

## 6. 设计取舍:为什么必须这样做

### 6.1 为什么 DNS 不能直接指定端口(不能省掉 iptables)

**DNS 协议的根本限制**:DNS 的职责是"名字→IP"。最常用的 A/AAAA 记录数据结构里**只有 IP 字段,没有端口字段**。`address=` 生成的是标准 A 记录,物理上装不下端口。

**端口由客户端决定,DNS 管不着**:访问 `http://xxx.e2b.app` 时的流程:

1. 客户端做 DNS 解析,拿到 IP(127.0.0.1)——**DNS 的任务到此结束**;
2. 客户端**自己**按 URL 协议补默认端口:`http://` → 80,`https://` → 443;
3. 发起连接 `127.0.0.1:80`。

即使 DNS 能塞端口,客户端也不读——它只从 DNS 拿 IP。

**SRV 记录为什么救不了**:DNS 确实有能带端口的 SRV 记录,但 **HTTP 客户端(浏览器/curl/绝大多数 SDK)访问网址时根本不查 SRV**,只查 A/AAAA。SRV 只服务于少数专门支持它的协议(XMPP、SIP、Kerberos 等)。

结论:客户端**必然**把流量送到 80,"80→3002"的改道只能在网络层(iptables)完成。**DNS 管"去哪台机器",iptables 管"到了之后进哪个端口"——层次不同,不可互相替代。**

### 6.2 为什么 e2b 不能仿照 consul 用 `server=` 带端口

设想写成 `server=/.e2b.app/127.0.0.1#3002`——语义是"把 `.e2b.app` 的 **DNS 查询**转发给 127.0.0.1:3002 这个 **DNS 服务器**"。

但 **127.0.0.1:3002 上跑的是 e2b 的 HTTP 代理,不是 DNS 服务器**。`server=` 转发的前提是目标端口上有一个"会说 DNS 协议"的服务;把 DNS 查询包发给一个 HTTP 服务,对方听不懂、不会答,解析直接失败。

两个端口的本质差异:

| | Consul 的 8600 | e2b 的 3002 |
|---|---|---|
| 服务类型 | **DNS 服务器**(说 DNS 协议) | **HTTP 代理**(说 HTTP 协议) |
| 能否被 `server=` 转发 | ✅ 能 | ❌ 不能 |
| 场景本质 | 纯 DNS 查询转发,不碰业务流量 | 需要"解析 + 业务流量导向"两层 |

Consul 那条链只解析、不导流量,一条 `server=` 就够;e2b 那条链解析之后还有真实的 HTTP 流量要进 3002,必须 DNS(给 IP)+ iptables(改端口)两层接力。**这不是配置风格的选择,是 DNS 协议与服务类型共同决定的必然。**

### 6.3 备选方案对比(为什么最终是这套)

| 方案 | 做法 | 未采用/采用的原因 |
|---|---|---|
| A:代理直接监听 80 | 代理绑 80,免 iptables | 80 是特权端口(<1024 需 root);可能与 nginx(Harbor 用)争抢;改代理配置不如内核层改道灵活 |
| B:URL 手写端口 `:3002` | 免 iptables | 反人类:所有访问者/SDK/回调都要写端口,极易出错,且与 e2b 云端(标准 80/443)行为不一致 |
| **C:DNS 给 IP + iptables 改端口(采用)** | 用户照常用干净域名 | 对用户完全透明;代理留在非特权端口;不抢 80;规则幂等可重跑 |

---

## 7. 验证与排障

### 7.1 分层验证命令

```bash
# ============ DNS 层 ============
# 1. resolv.conf 第一行必须是 127.0.0.1
head -1 /etc/resolv.conf

# 2. dnsmasq 在运行且监听 53
systemctl status dnsmasq
ss -ulnp | grep :53

# 3. e2b 域名应被答成 127.0.0.1(验证 address= 规则 + 整条 dnsmasq 链)
dig xxx.e2b.app +short          # 期望输出: 127.0.0.1

# 4. consul 域名经 dnsmasq 应返回真实 IP(验证 server= 规则)
dig redis.service.consul +short

# 5. 绕过 dnsmasq 直问 Consul(切割故障:区分 dnsmasq 问题还是 Consul 问题)
dig @127.0.0.1 -p 8600 consul.service.consul +short

# 6. 普通域名正常解析(验证默认上游)
dig github.com +short

# 7. 查看 dnsmasq 生效的规则与默认上游来源
grep -E '^(server|address|resolv-file)' /etc/dnsmasq.conf /etc/dnsmasq.d/*.conf

# ============ iptables 层 ============
# 8. 两条改道规则是否在位
iptables -t nat -L PREROUTING -n --line-numbers | grep 3002
iptables -t nat -L OUTPUT -n --line-numbers | grep 3002

# 9. 端到端:本机访问 e2b 域名应实际连到 3002 上的代理
curl -v http://xxx.e2b.app 2>&1 | head -20

# 10. 代理确实在 3002 监听
ss -tlnp | grep :3002

# ============ 服务发现 / 调度层 ============
# 11. Consul 成员与服务
consul members
curl -s http://127.0.0.1:8500/v1/catalog/services | jq

# 12. Nomad job 状态
nomad job status -token $NOMAD_ACL_TOKEN
```

### 7.2 故障模式速查

| 症状 | 最可能的原因 | 定位手段 |
|---|---|---|
| 所有域名解析都失败/极慢 | dnsmasq 挂了(resolv.conf 指向空地址,先超时再 fallback) | 命令 2;`journalctl -u dnsmasq` |
| `.consul` 解析失败,普通域名正常 | server= 规则丢失,或 Consul 8600 没起 | 命令 4 vs 5 对比切割 |
| `.e2b.app` 解析不到 127.0.0.1 | address= 规则丢失;或 resolv.conf 首行 127.0.0.1 被冲掉,查询走了公网 DNS | 命令 1、3、7 |
| 普通域名解析失败,内部域名正常 | dnsmasq 默认上游没配对(可能循环指向自己) | 命令 6、7 |
| 外部访问正常,本机访问 e2b 域名连不上 | **OUTPUT+lo 规则缺失**(loopback 流量不过 PREROUTING) | 命令 8 |
| 本机外部都连不上 e2b 域名,但 3002 直连正常 | 两条 iptables 规则都丢了(常见于重启后) | 命令 8、9 |
| 解析正常、80→3002 正常,但请求 502/超时 | 代理后面的 api/orchestrator/沙箱问题,已出网络层范畴 | 命令 10、12;查 nomad alloc 日志 |
| 服务互相找不到(组件日志 connection refused 到旧 IP) | 组件没走服务发现或 Consul 注册异常 | 命令 4、11 |

排障心法:**沿链路自底向上切割**——先 DNS(3/4/5/6),再 iptables(8/9),再代理(10),再应用(12)。每一步都有"绕过上一层直接测本层"的手段(如 dig -p 8600 绕过 dnsmasq、curl 127.0.0.1:3002 绕过 iptables)。

---

## 8. 已知坑与持久化问题

### 8.1 iptables 规则重启即丢(最重要)

iptables 规则**不持久**,机器重启后 80→3002 改道消失 → e2b 域名访问全断(症状:解析正常但连不上)。解决:

```bash
# 方案一:保存 + 开机恢复(发行版对应 iptables-services / netfilter-persistent)
iptables-save > /etc/sysconfig/iptables      # CentOS/RHEL 系
systemctl enable iptables

# 方案二:做成开机脚本/systemd unit,重放那两条幂等命令(-C || -A 保证安全重放)
```

### 8.2 resolv.conf 可能被托管程序覆盖

NetworkManager / systemd-resolved / DHCP 客户端可能在网络事件后重写 `/etc/resolv.conf`,把首行 `nameserver 127.0.0.1` 冲掉 → 内部域名解析突然失灵。缓解:

- 在 NetworkManager 配置中设 `dns=none`,或让 dnsmasq 与托管机制集成;
- 简单粗暴:`chattr +i /etc/resolv.conf` 锁定文件(改配置前需先 `chattr -i`);
- 至少:把 `head -1 /etc/resolv.conf` 纳入巡检。

### 8.3 dnsmasq 默认上游的鸡生蛋问题

resolv.conf 首行指向 dnsmasq 自己,若 dnsmasq 又 fallback 读该文件当上游,存在自我转发风险。建议在 dnsmasq 配置里**显式**指定默认上游(`server=<公网DNS>`)或 `resolv-file=` 指向单独的上游文件,并用 `dig github.com` 验证。

### 8.4 nameserver 备胎的隐蔽降级

127.0.0.1 之后的公网 nameserver 平时不生效;dnsmasq 挂掉时会 fallback,进入"普通上网勉强可用 + 内部域名全废 + 每次查询先超时几秒"的**隐蔽半残状态**,故障不易察觉。权衡:要么删掉备胎让故障快速显性化,要么加对 dnsmasq 的监控告警。

### 8.5 其他关联事项

- **绝不要在 resolv.conf 加 `options rotate`**:轮询会把查询甩给公网 DNS,内部域名间歇性解析失败,极难排查;
- **80 端口与 nginx 的关系**:若 nginx(为 Harbor 服务)监听 80,与 REDIRECT 规则的交互需理清(REDIRECT 在 PREROUTING/OUTPUT 的 nat 阶段先于本地交付,流量会被改道而到不了 nginx 的 80);出现 Harbor 或 e2b 访问异常时优先检查 80 的归属;
- **iptables -F 会清掉改道规则**:脚本中 `iptable_clean` 等操作包含 `iptables -F`(清 filter 表)——虽然 `-F` 默认不清 nat 表,但任何涉及 `iptables -t nat -F` 的操作后都必须重放那两条规则;
- **glibc MAXNS=3**:resolv.conf 第 4 行起的 nameserver 是摆设,不要指望它。

---

## 9. 分层职责总结

五条网络配置各管一段,合起来构成完整入口:

| 配置 | 层次 | 负责的流量 | 一句话职责 |
|---|---|---|---|
| resolv.conf 首行 `nameserver 127.0.0.1` | DNS 客户端 | 全机所有 DNS 查询 | 把所有查询引到 dnsmasq 总机 |
| dnsmasq `address=/.e2b.app/127.0.0.1` | DNS 服务(本地应答) | e2b 域名解析 | 把入口/沙箱域名指到本机 |
| dnsmasq `server=/consul/127.0.0.1#8600` | DNS 服务(条件转发) | 内部服务发现 | 把 `.consul` 交给 Consul 回答动态地址 |
| iptables PREROUTING 80→3002 | 内核 nat | **外部**进入的 e2b 流量 | 外来 80 改道到代理 |
| iptables OUTPUT+lo 80→3002 | 内核 nat | **本机自访问**(loopback)的 e2b 流量 | 本机 80 改道到代理(loopback 不过 PREROUTING,必须单配) |

三层协作的最终图景:

> **DNS 负责"指对机器"**(dnsmasq 按后缀分流:e2b 域名→本机、consul 域名→服务发现、其余→公网),
> **iptables 负责"改对端口"**(80→3002,内外两条链全覆盖),
> **代理负责"路由对目标"**(控制请求→api→orchestrator→firecracker;沙箱请求→microVM→envd)。
>
> 端口 80 是客户端按 http 协议自补的、DNS 协议给不了端口(A 记录无此字段、HTTP 客户端不读 SRV),而 3002 是 HTTP 服务不是 DNS 服务、无法被 `server=` 转发——因此"`address=` 捏造 IP + iptables 改端口"的组合不是众多方案之一,而是协议约束下的必然设计。
