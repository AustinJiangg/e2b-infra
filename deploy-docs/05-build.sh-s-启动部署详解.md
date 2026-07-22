# 05 `build.sh -s` 启动部署详解

`bash /opt/e2b-infra/build.sh -s`（等价 `--start`）是装机第二条命令：在 `-i` 备好的环境上，
把 **Harbor 装起来 → Consul/Nomad 控制面拉起来（含 ACL bootstrap）→ 宿主机调成沙箱运行形态 →
构建并推送业务镜像 → 提交 4 个 Nomad job → 注入初始用户**，一路跑到 E2B 可用。

它是"全新搭集群"的重路径：会重启 nomad、首次会生成新 ACL token。**日常改动迭代不要用它**
（用 runbook §5 的场景表），否则容易踩 token 冲突（runbook §6.1）和慢网卡指纹（§6.5）。

## 0. 前提

- `-i` 已成功执行（postgres/minio/nginx/dnsmasq 在跑、harbor 已解压、overlay 已重放、
  consul/nomad zip 已缓存到 `/tmp`）；
- 自编译 `nbd.ko` 已加载（`ls /dev/nbd* | wc -l` = 512；按 runbook §0.2 一次性固化后开机自动加载——脚本**不**负责加载）；
- docker 在跑且 daemon.json 已含 `SERVER_IP:2900`。

## 1. `start()` 总流程

```
start()
 ①  校验 /opt/e2b-infra 与 dep/.env 存在
 ②  Harbor 正式安装（harbor/install.sh + 权限修复 + 重启 registry/proxy）
 ③  start-server.sh $HOST_IP
      ├─ install-consul.sh --version 1.21.4     （离线缓存安装）
      ├─ install-nomad.sh  --version 1.10.4
      ├─ run-consul.sh --server ...             （配置+systemd+启动+ACL bootstrap→写回 .env）
      └─ run-nomad.sh  --server ...             （配置+systemd+启动+ACL bootstrap+node pools→写回 .env）
 ④  append_nomad_client_config                  （给 default.hcl 追加 client 块）
 ⑤  systemctl restart nomad                     （同一 agent 变成 server+client 合体）
 ⑥  start-client.sh api $HOST_IP                （业务二进制→/usr/bin + 宿主机调优）
 ⑦  init-client.sh                              （envd/内核/firecracker 铺到 /fc-*）
 ⑧  wait_for_port 4646                          （等 Nomad HTTP 就绪，可能很慢，见 §8）
 ⑨  docker login Harbor + 创建 e2b-orchestration 项目
 ⑩  rm bin/orchestrator.Dockerfile              （orchestrator 不容器化）
 ⑪  deploy.sh                                   （构建/推镜像 → 渲染 hcl → 提交 4 个 job → seed 用户 → 放开配额）
 ⑫  iptables 80 → 3002                          （沙箱域名入口）
```

下面按步拆解。

## 2. Harbor 正式安装（步骤②）

```bash
cd /opt/e2b-infra/harbor
bash install.sh                       # Harbor 官方离线安装：load 镜像 + prepare + docker-compose up
chown -R 10000:10000 common/config/registry common/config/nginx
docker-compose restart registry proxy
```

- `install.sh` 会把离线包里的 Harbor 镜像 `docker load`，用 `-i` 阶段生成的 `harbor.yml`
  渲染配置，然后 `docker-compose up -d` 拉起 nginx/core/registry/db/redis/portal 等一组容器；
- **chown 修复**：prepare 生成的部分配置属主是 root，而 Harbor 容器内进程跑 UID 10000，
  读不了配置会起不来——把 registry 和 nginx 的配置目录 chown 给 10000 再重启这两个容器。
  这是重复踩过的坑固化下来的步骤；
- 完成后 `http://SERVER_IP:2900` 可访问（admin/Harbor12345）。

## 3. `start-server.sh`：拉起控制面（步骤③）

```bash
bash start-server.sh "$HOST_IP"
```

脚本很短，但每一步都重：

```bash
set -a; source .env; set +a          # 读顶层 .env（此时 token 还是占位符，没关系）
ulimit -n 65536
./install-consul.sh --version 1.21.4
./install-nomad.sh  --version 1.10.4
./run-consul.sh --server --server-ips "$SERVER_IPS" --instance-ip-address "$IP"
set -a; source .env; set +a          # ★ 重新 source：拿到 run-consul 刚写回的真 CONSUL_ACL_TOKEN
./run-nomad.sh --server --num-servers 1 --consul-token "$CONSUL_ACL_TOKEN" --instance-ip-address "$IP"
```

### 3.1 `install-consul.sh` / `install-nomad.sh`：离线优先的安装器

- 先打印 "Downloading ..."，**随后检查 `/tmp` 里是否已有对应 zip**（`-i` 已拷好
  `/tmp/consul.zip`、`/tmp/nomad_1.10.4_linux_arm64.zip`），有则 `skipping download`
  ——所以日志里的 "Downloading" 并不代表联网（runbook §6.4）；
- 创建 `consul`/`nomad` 系统用户与目录（`/opt/consul`、`/opt/nomad` 的 bin/config/data/log）；
- 解压二进制装入 `/opt/{consul,nomad}/bin/`，并 symlink 到 `/usr/local/bin/`；
- `install-nomad.sh` 还会 `systemctl enable --now docker`（Nomad 的 docker driver 需要），
  并安装 `nomad.service` systemd 单元。

### 3.2 `run-consul.sh`：配置、启动、ACL bootstrap（含 token 持久化）

核心动作：

1. 生成 Consul 配置（server 模式、`bootstrap_expect` 按节点数、开 ACL）、写 systemd
   `consul.service`，`systemctl enable && restart consul`；
2. **探测是否已有集群**：能连上其它 server 的 leader 接口则走 join 路径、跳过 bootstrap；
3. 首次启动（无集群）：`consul acl bootstrap` 生成 root token，然后——
   - 持久化到 **`/opt/consul/acl.token`**（chmod 600）；
   - 写回 **`/opt/e2b-infra/.env` 的 `CONSUL_ACL_TOKEN=`**；
4. 重跑（join 已 bootstrap 的集群）：从 `/opt/consul/acl.token` **恢复**真 token 写回 `.env`
   ——这是修复 "`-i` 重置 .env 后 `-s` 403" 的机制（runbook §6.1）。

### 3.3 `run-nomad.sh`：Nomad server 的配置与 bootstrap

1. **生成 `/etc/nomad.d/default.hcl`**（此时只有 server 半边），要点逐块：

   | 配置块 | 内容 | 作用 |
   |---|---|---|
   | `advertise` | http/rpc/serf 全部广播 `$HOST_IP` | 单网卡多 IP 时锁定对外地址 |
   | `server` | `enabled=true`、`bootstrap_expect=1` | 单节点即成集群 |
   | `plugin "docker"` | `volumes.enabled=true`、`auth.config=/root/docker/config.json` | 允许 job 挂卷；从 Harbor 拉镜像用 docker 凭据 |
   | `log_level=DEBUG`、`log_json=true` | zap JSON 日志 | benchmark 采集/解析的前提 |
   | `telemetry` | prometheus 指标、发布 alloc/node 指标 | 可观测性 |
   | `acl { enabled=true }` | 开 ACL | 所有 API 调用都要 token |
   | `consul` | `127.0.0.1:8500` + `$CONSUL_ACL_TOKEN` | 服务注册进 Consul（`*.service.consul` 域名的来源） |
   | `limits` | http/rpc 每客户端 80 连接 | 防单客户端耗尽 |

2. **生成 `/etc/systemd/system/nomad.service`**：
   `ExecStart=nomad agent -config /etc/nomad.d -data-dir /data/nomad`，
   `LimitNOFILE/NPROC=infinity`、`Restart=on-failure`；
3. `systemctl enable && start nomad`；
4. **bootstrap 或恢复**（与 consul 同构）：
   - 探测 `$SERVER_IPS` 各机的 `/v1/status/leader`，已有 leader → 跳过 bootstrap，
     从 **`/data/nomad/acl.token`** 恢复 token（没有该文件则警告：后面 deploy 可能 403，
     干净重来的口令是 `build.sh -d` + `-s`）；
   - 全新集群 → 等本机 `/v1/agent/health` 就绪 → `nomad acl bootstrap` 拿 Secret ID →
     **创建 node pool `api` 与 `build`** → token 持久化到 `/data/nomad/acl.token`（600）；
5. 无论哪条路，拿到的 token 都会 **sed 写回 `/opt/e2b-infra/.env` 的 `NOMAD_ACL_TOKEN=`**
   ——`deploy.sh` 后面就靠它提交 job。

> **token 的"家"只有两个**：集群数据目录（`/data/nomad/acl.token`、`/opt/consul/acl.token`，
> 与集群共生死，`build.sh -d` 一起清掉）和顶层 `.env`（易被 `-i` 重置，但可从前者恢复）。
> 理解了这一点，runbook §6.1 的 403 处置就是显然的。

## 4. `append_nomad_client_config` + 重启（步骤④⑤）：server 变身 server+client

单机部署不再单独跑 client agent，而是**给同一个 agent 的配置追加 client 块**：

```hcl
client {
  enabled = true
  node_pool = "api"                 # 单机所有 job 都调度到 api 池（.env: API_NODE_POOL=BUILD_NODE_POOL=api）
  network_interface = "<持有 HOST_IP 的网卡>"   # 跳过默认路由自动探测
  network_speed = 1000              # 写死链路速率 → 跳过逐接口 ethtool 探测
  meta { node_pool = "api" }
}
plugin "raw_exec" { config { enabled = true } }   # template-manager-system 要直接跑宿主机二进制
```

- 幂等：靠标记注释判断，已存在则跳过；
- `network_interface`/`network_speed` 两行是针对"共享大机器上几千个邻居 veth 把 Nomad client
  网络指纹拖慢到十几分钟"的缓解（原理与诊断见 runbook §6.5）；
- 追加后 `systemctl restart nomad`，agent 以 server+client 双角色回来。**4646 端口要等
  client 半边完成指纹后才监听**——这就是步骤⑧要等待的原因。

## 5. `start-client.sh api $HOST_IP`（步骤⑥）：业务二进制 + 宿主机调优

参数含义：`$1`=node_pool 名（api），`$2`=本机 IP。做的事按块拆：

1. **业务二进制上岗**：
   ```bash
   cp bin/orchestrator /usr/bin/orchestrator
   cp bin/orchestrator /usr/bin/template-manager    # 同一个二进制，两个名字
   ```
   实际由 nomad job 启动的是 `/usr/bin/template-manager`；跑哪些服务由 job env 的
   `ORCHESTRATOR_SERVICES` 决定。**rpm 升级不会刷新 /usr/bin 下这两个文件**（场景二要手动 cp）。
2. **orchestrator 工作目录**：`/orchestrator/{sandbox,template,build}`。
3. **swap**：建 1G `/swapfile`（幂等），写 fstab，`vm.swappiness=10`、`vm.vfs_cache_pressure=50`
   ——留一点 swap 兜底但尽量不用。
4. **快照缓存**：`mount -t tmpfs -o size=65G tmpfs /mnt/snapshot-cache`（幂等，防叠加挂载）。
5. **网络/内存 sysctl（运行时 `-w`）**：somaxconn/netdev_max_backlog/tcp_max_syn_backlog=65535、
   `vm.max_map_count=1048576`（大量 mmap/uffd 需要）；`ulimit -n 1048576`。
6. **NBD udev 规则**：`/etc/udev/rules.d/97-nbd-device.rules` 给 nbd 设备加 `nowatch`
   ——禁止 inotify 监听 nbd 变更事件（内核邮件列表已知的性能问题）。
   注意：**nbd 模块本身不在这里加载**（自编译 nbds_max=512 版已按 runbook §0.2 固化，开机自动加载）。
7. **目录与 fuse 配置**：`/fc-vm`、`/fuse/config.yaml`（gcsfuse 风格缓存配置，本地部署基本闲置）。
8. **大页（hugepages）预留**——算法值得记住：
   ```
   保留普通内存 = clamp( max(4G, 总内存×16%), ≤42G )
   大页可用内存 = 总内存 - 保留值（取偶）
   页数 = 大页内存 / Hugepagesize（一般 2M）
   其中 20% 写入 nr_hugepages（常驻预留）、80% 写入 nr_overcommit_hugepages（超售上限）
   ```
   沙箱 VM 内存走大页（性能+避免碎片），启动早期趁内存未碎片化先占住。
   同时挂载 `hugetlbfs` 到 `/mnt/hugepages`（幂等）。
9. **DNS 链路**：
   - `/etc/resolv.conf` 顶部保证**恰好一条** `nameserver 127.0.0.1`（先删重复再插入——
     旧版每跑一次叠一行的坑已修）；
   - `/etc/dnsmasq.d/consul.conf` 写 `server=/consul/127.0.0.1#8600`（幂等）：
     `*.consul` 域名转发给 Consul DNS。配合 `-i` 写的 `address=/.e2b.app/127.0.0.1`，
     宿主机 DNS 变成：consul 域名→Consul，沙箱域名→本机，其余→上游；
   - `systemctl restart dnsmasq`。
10. 尾部原上游的 `run-consul.sh --client`、`run-nomad.sh --client` 调用**已注释**
    ——单机由 §4 的合体 agent 代替。

## 6. `init-client.sh`（步骤⑦）：沙箱运行三件套

与 start-client.sh 重复的宿主机调优（目录/swap/tmpfs/大页）再幂等跑一遍，另外：

- sysctl 这次写进 **`/etc/sysctl.conf`**（持久化，有存在性检查防重复追加）+ `sysctl -p`；
- **envd**：`cp bin/envd /fc-envd/envd`（模板构建时注入沙箱 rootfs 的代理）；
- **客户机内核**：`cp bin/vmlinux.bin /fc-kernels/vmlinux-6.1.102/`（RPM 自带 ARM 内核；
  目录名 6.1.102 是 orchestrator 按版本寻址的约定路径）；
- **firecracker**：`cp bin/firecracker /fc-versions/v1.13.1/firecracker` 并加执行权限
  （RPM 自带的仓库定制版，基于 v1.12.1，仅 aarch64 打包；不再下载/解压官方 tgz。
  目录名 v1.13.1 是代码运行时查找的版本标签）。

至此宿主机具备跑沙箱的全部静态条件：`/usr/bin` 双二进制、`/fc-envd`、`/fc-kernels`、
`/fc-versions`、大页、nbd（已固化自动加载）、netns 工具链。

## 7. `wait_for_port 4646 tcp 1 0`（步骤⑧）

通用等端口函数（`ss -tln` 轮询，间隔 1s，超时 0=**永不超时**）。等的是 Nomad HTTP/UI 端口：
重启后 client 半边要枚举全部网卡做指纹，**宿主机 veth 多时这里会"很慢但不是卡死"**
——诊断与缓解见 runbook §6.5（`network_speed` 已在 §4 自动加上，通常几秒~几十秒过）。

## 8. Harbor 凭据与项目（步骤⑨）

```bash
docker login -u admin -p Harbor12345 $HOST_IP:2900        # 失败仅告警不中断
curl -X POST http://localhost:2900/api/v2.0/projects -u admin:Harbor12345 \
     -d '{"project_name": "e2b-orchestration", "public": true}'
```

- login 把凭据写进 `/root/.docker/config.json`，供后续 `deploy.sh` 的 docker push 使用。
  留意一个细节：`run-nomad.sh` 生成的 docker plugin 配置里 `auth.config` 指向的是
  `/root/docker/config.json`（**没有点**，与 login 写的路径不同）——但因为下一步把项目
  设为 public，nomad 拉镜像不需要凭据，这个差异实际不影响运行；
- 项目 `e2b-orchestration` 是所有业务镜像的 namespace（`REGISTRY_URL=IP:2900/e2b-orchestration`），
  public 让拉取免鉴权。重复创建返回 409，无害。

## 9. `rm bin/orchestrator.Dockerfile`（步骤⑩）

`deploy.sh` 会对 `bin/` 下**每个** `*.Dockerfile` 做 docker build+push。orchestrator 在本部署
走 **raw_exec 直跑宿主机二进制**（要操作 nbd/netns/uffd/大页，容器化反而碍事），
所以先把它的 Dockerfile 删掉，跳过无意义的镜像构建。

## 10. `deploy.sh`（步骤⑪）：镜像、渲染、提交、seed

`build.sh -f` 单独执行的就是这一步；`-r <job>` 是它的 `--only` 快捷方式。全量逻辑：

### 10.1 构建并推送业务镜像

```bash
export DOCKER_BUILDKIT=0            # 本机 buildx 插件与 daemon 版本不兼容，退回传统构建
cd bin/
for dockerfile in *.Dockerfile:     # 典型：api / client-proxy / db-migrator
    docker build -t $REGISTRY_URL/<name> -f <name>.Dockerfile .
    docker push  $REGISTRY_URL/<name>
```

构建上下文就是 `bin/`——Dockerfile 的套路是把**已编好的二进制** COPY 进
`debian:bookworm-slim` 基础镜像（离线前提：该基础镜像已 `docker load`）。
所以这里的 docker build 很快，没有编译发生。

随后把 redis 转推到私仓（离线时本地必须已有该镜像）：

```bash
docker pull/tag/push redis:7.4.4-alpine → $REGISTRY_URL/redis:7.4.4-alpine
```

### 10.2 渲染 job 模板：envsubst 白名单机制

```bash
for hcl in nomad/*.hcl:
    envsubst '<白名单变量列表>' < $hcl > rendered/$(basename $hcl)
```

- 白名单 = deploy.sh 里那一长串 `$VAR` 列表（几十个：端口、镜像名、凭据、桶名……），
  值来自顶层 `.env`；
- **只有白名单里的 `${VAR}` 会被替换**。不在名单里的 `${XXX}` 原样留给 Nomad 的 HCL
  解析层，Nomad 把它当自家插值处理，未知变量最终成空串——这就是 runbook §9.1 强调
  "自定义开关要写字面量、别写 `${...}`" 的原因；
- `$${node.unique.name}` 这类**双 dollar** 是刻意留给 Nomad 的运行时插值（envsubst 不动它，
  Nomad 解析 `$${}` 转义成 `${node.unique.name}` 再在节点上求值）。

### 10.3 提交 Nomad job

```bash
JOBS=(redis template-manager edge api)          # --only 时只剩指定的一个
for j in JOBS: nomad job run --token $NOMAD_ACL_TOKEN rendered/$j.hcl
```

提交顺序即依赖顺序：redis（沙箱状态存储）→ template-manager-system（orchestrator 合体，
raw_exec）→ client-proxy（edge.hcl）→ api（其 prestart 任务 db-migrator 先对 postgres
跑 goose 迁移，然后 api 主任务才起）。

### 10.4 seed 初始用户 + 写 SDK 凭据

```bash
psql: SELECT EXISTS (SELECT 1 FROM teams WHERE name='E2B')
```

- 查不到（首次部署）→ 跑 `bin/seed-db`（喂入 `SEED_EMAIL`，默认 admin@e2b.dev）：
  创建 E2B 团队/用户并打印 **Team ID / Access Token / Team API Key**；
- 脚本解析这三个值写入 **`/root/.e2b/config.json`**——SDK 与 benchmark 的凭据源头；
- 已 seed 过则整段跳过（不会重复造用户、不会覆盖 config.json）。

> 首次部署时 db-migrator 可能还没跑完迁移，seed-db 若因表不存在失败：等 api job 就绪后
> 重跑 `bash build.sh -f` 即可（seed 段幂等）。

### 10.5 放开配额

```bash
UPDATE tiers SET concurrent_instances=10000, max_length_hours=10000 WHERE id='base_v1';
```

默认 tier 的并发沙箱数/最长存活时间放开到 1 万，压测不受限。

## 11. iptables 沙箱入口（步骤⑫）

```bash
iptables -w -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 3002 || -A ...
iptables -w -t nat -C OUTPUT -p tcp -o lo --dport 80 -j REDIRECT --to-port 3002 || -A ...
```

外部流量（PREROUTING）和本机回环流量（OUTPUT -o lo）的 80 端口都重定向到 client-proxy
的 3002。`-C ... || -A ...` 保证幂等。副作用：nginx 的 80 vhost 从外部不可达（预期，
nginx 只为 443 反代存在）。**iptables -F 会把这两条清掉**——flush 过后要重跑本段或 `-s`。

## 12. 成功判据与验证

脚本最后打印 `✅ e2b-infra 服务启动完成`。人工验证：

```bash
source /opt/e2b-infra/.env
nomad job status -token "$NOMAD_ACL_TOKEN"
#  api / client-proxy / redis / template-manager-system 都 running
curl -s http://$SERVER_IP:4646/v1/status/leader        # 有 leader
curl -s http://$SERVER_IP:3000/health                  # api 健康（若有该路由）
docker ps | grep -E "postgres|harbor"                  # 依赖在跑
ls /root/.e2b/config.json                              # SDK 凭据已生成（首次）
```

然后构建首个模板（`python /opt/e2b-infra/build_prod.py base`）→ `Sandbox.create("base")`。

## 13. `-s` / `-f` / `-r` / `-d` 的关系（收束）

| 命令 | 范围 |
|---|---|
| `build.sh -s` | 上面全部 ①~⑫（重路径，全新搭建/彻底重来用） |
| `build.sh -f` | 只有步骤⑪ `deploy.sh` 全量（镜像构建+渲染+全部 job+seed+配额）——改 `.env` 后用 |
| `build.sh -r <job>` | `deploy.sh --only <job>`：只渲染+重跑一个 job，跳过镜像/seed——改单个 job env 后用（runbook §11） |
| `build.sh -d` | 逆操作：purge 全部 job、卸 consul/nomad（**连数据目录带 ACL 状态一起删**）、kill 残留业务进程。`-d` 之后再 `-s` = 全新 bootstrap |
