# 单节点 Nomad 离线部署指南

面向「单台 ARM(aarch64) 服务器 + Nomad 模式 + 离线/内网」的完整部署流程。
按本文档从上到下走一遍即可，无需看其它文档。

> 约定：本机 IP 记作 `SERVER_IP`，示例用 `61.47.17.182`，请替换成你自己的。

---

## 0. 前置条件

- openEuler / aarch64。
- 构建 RPM 需要 `rpmbuild` 与 go 工具链（工具链随源码包提供）；也可在有网机器上把 RPM 构建好再拷到目标机。
- 需要一个可用的 **yum 源**和 **pip 源**（在线或本地镜像）——见文末「仍需联网的部分」。
- 会自动 `setenforce 0`（关闭 SELinux）。

### 0.1 Docker（需自备，脚本不装）

`build.sh` 里的 `install_docker` 已被注释掉——假定 **docker 由你自行安装维护**。请确保：

1. 版本达标（Harbor 的 `install.sh` 依赖 `docker-compose` 命令）：
   ```bash
   docker --version         # >= 25.0.3
   runc --version           # >= 1.0.2
   docker-compose version   # 必须存在 docker-compose 命令
   ```
2. 把本机 Harbor registry 加进 `insecure-registries`（HTTP，端口 2900）。**合并**进现有 `daemon.json`，不要整个覆盖：
   ```bash
   vi /etc/docker/daemon.json
   # 加入（<SERVER_IP> 换成本机 IP）：
   #   "insecure-registries": ["<SERVER_IP>:2900"]
   systemctl restart docker   # ⚠️ 会重启现有容器，挑好时机
   ```

### 0.2 自定义 nbd 内核模块（`nbd.ko`，需自备）

`start-client.sh` / `init-client.sh`（`build.sh -i` 从 `dep/` 拷进 `/opt/e2b-infra/`、
`build.sh -s` 启动 Nomad 客户端时执行）已不再用 `modprobe nbd`，改成先 `rmmod nbd` 再
`insmod` 你**自己编译的优化版 nbd 模块**（带 `nbds_max=512`）。模块路径默认
`/home/j30059180/tools/nbd-patch/nbd.ko`，可用环境变量 `NBD_KO` 覆盖。部署前须满足两个前提，
否则脚本会在加载 nbd 这步 `exit 1`：

1. **vermagic 必须与运行内核完全一致**。`insmod` 会校验模块 vermagic 是否等于本机 `uname -r`，
   不一致直接报 `Invalid module format`（这是从 `modprobe` 换成 `insmod` 新增的硬约束，`modprobe`
   不校验这么严）。检查：
   ```bash
   modinfo /home/j30059180/tools/nbd-patch/nbd.ko | grep vermagic
   # vermagic: 6.6.0_6.6.0_515-uffd_copy_open_tree SMP mod_unload modversions aarch64
   uname -r        # 必须与上面 vermagic 里的内核版本一致
   ```
   内核不同就要在目标机对应内核上重新编译 `nbd.ko`。

2. **`.ko` 必须存在于本节点**。脚本在本机读取 `$NBD_KO`；单节点部署里编译机和 client 通常是同一台，
   若 `.ko` 不在默认路径，就先拷过去或用 `NBD_KO` 指到实际路径。文件缺失时脚本 fail-fast 报
   「找不到自定义 nbd 模块」并退出：
   ```bash
   # 放到默认路径：
   mkdir -p /home/j30059180/tools/nbd-patch && cp nbd.ko /home/j30059180/tools/nbd-patch/nbd.ko
   # 或指到别处（例如和其它离线大件一起放 dep/）：
   export NBD_KO=/opt/e2b-infra/dep/nbd.ko
   ```

> ⚠️ 仓库里有**两套** client 脚本：RPM（`e2b-infra.spec`）打包的
> `.github/actions/host-init/init-client.sh`、`iac/provider-gcp/nomad-cluster/scripts/start-client.sh`
> **不加载 nbd**；真正加载 nbd（`rmmod`+`insmod`）的是 `e2b-deploy/dep/` 下的版本，由 `build.sh -i`
> 覆盖进 `/opt/e2b-infra/`、再由 `-s` 执行。所以**务必走 `build.sh -i` → `-s`**，别绕过它直接跑
> RPM 装的 `/opt/e2b-infra/*-client.sh`，否则不会加载你的自定义 nbd 模块。

---

## 1. 准备离线大件与镜像

这些体积大、**不在 RPM 里**，需在有网机器上下载后拷到目标机。

### 1.1 二进制 / 安装包 → 拷到 `/opt/e2b-infra/dep/`

文件名必须与下面完全一致（`build.sh` / `install-*.sh` / `init-client.sh` 按名字找）：

```bash
# 在有网机器上下载（aarch64）
wget https://releases.hashicorp.com/consul/1.21.4/consul_1.21.4_linux_arm64.zip
wget https://releases.hashicorp.com/nomad/1.10.4/nomad_1.10.4_linux_arm64.zip
wget https://github.com/firecracker-microvm/firecracker/releases/download/v1.12.1/firecracker-v1.12.1-aarch64.tgz
wget https://github.com/goharbor/harbor/releases/download/v2.13.0/harbor-offline-installer-aarch64-v2.13.0.tgz
wget -O minio https://dl.min.io/server/minio/release/linux-arm64/minio
```

拷到目标机后（RPM 装完会生成 `/opt/e2b-infra/dep/`，见第 2 步）：

```bash
cp consul_1.21.4_linux_arm64.zip nomad_1.10.4_linux_arm64.zip \
   firecracker-v1.12.1-aarch64.tgz harbor-offline-installer-aarch64-v2.13.0.tgz \
   minio  /opt/e2b-infra/dep/
```

### 1.2 docker 镜像 → 在目标机 `docker load`

这三个镜像是**最容易漏的**（`install_docker` 被注释，不会自动 `docker load`）：

| 镜像 | 谁用它 |
|------|--------|
| `postgres:latest` | `install_postgre` 启动 PostgreSQL 容器 |
| `redis:7.4.4-alpine` | `deploy.sh` 部署 redis 组件 |
| `debian:bookworm-slim` | `deploy.sh` 构建 api / client-proxy / db-migrator 镜像的**基础镜像** |

```bash
# 在有网机器上导出
docker pull postgres:latest        && docker save postgres:latest        | gzip > postgres.tar.gz
docker pull redis:7.4.4-alpine     && docker save redis:7.4.4-alpine     | gzip > redis-7.4.4-alpine.tar.gz
docker pull debian:bookworm-slim   && docker save debian:bookworm-slim   | gzip > debian-bookworm-slim.tar.gz

# 拷到目标机后加载
docker load -i postgres.tar.gz
docker load -i redis-7.4.4-alpine.tar.gz
docker load -i debian-bookworm-slim.tar.gz

# 校验标签存在（这三个 tag 是脚本里写死的）
for t in postgres:latest redis:7.4.4-alpine debian:bookworm-slim; do
  docker image inspect "$t" >/dev/null 2>&1 && echo "OK  $t" || echo "缺失 $t"
done
```

> 制作 VM 模板时沙箱内会联网装依赖，离线需另外准备 `ubuntu:22.04-custom` 基础镜像，
> 做法见根目录 `README.md` 的「无网环境准备」，属另一条线，与本文的集群部署无关。

---

## 2. 获取代码、构建并安装 RPM

```bash
# 1) 拉代码（大源码包走 git-lfs）
git clone <本仓库地址> e2b-infra
cd e2b-infra
git lfs pull                       # 拉取 e2b-infra-2026.09.tar.gz

# 2) 若改了 dep/.env（尤其 SERVER_IP、各端口），先重建 e2b-deploy.tar.gz
#    （e2b-deploy.tar.gz 是打进 RPM 的部署包，dep/.env 里的 SERVER_IP 必须是本机 IP）

# 3) 构建 RPM
rpmbuild -bb e2b-infra.spec --define "_sourcedir $PWD"

# 4) 安装到 /opt（会生成 /opt/e2b-infra/ 及 /opt/e2b-infra/dep/）
rpm -ivh ~/rpmbuild/RPMS/aarch64/e2b-infra-*.rpm      # 升级用 rpm -Uvh
```

> 关键：`dep/.env` 的 `SERVER_IP=` 必须改成本机 IP，否则 harbor/registry/nomad 地址全错。

---

## 3. 放置离线大件

RPM 装完后，执行第 1 步的拷贝与 `docker load`（把 5 个文件放进 `/opt/e2b-infra/dep/`，加载 3 个镜像）。

---

## 4. 安装组件并启动

```bash
cd /opt/e2b-infra
bash build.sh -i      # 装 postgres/minio/harbor/nginx/e2b，并把 nomad/consul/firecracker 缓存到位
bash build.sh -s      # 起 consul+nomad（首次会 bootstrap ACL）、登录 harbor、部署 nomad job
```

正常结束会打印 `✅ e2b-infra 服务启动完成`。验证：

```bash
source /opt/e2b-infra/.env
nomad job status -token "$NOMAD_ACL_TOKEN"     # redis/template-manager/edge/api 都在
curl -s http://$SERVER_IP:4646/v1/status/leader
```

---

## 5. 改了东西之后，怎么让它生效（日常工作流）

> 核心原则：**改源码 ≠ 重新 bootstrap 整个集群**。`build.sh -s` 是「全新搭集群」的重路径，
> 别把它当迭代循环，否则容易踩 ACL token 冲突（见第 6 节）；在共享大机器上 `-s` 还会因
> 重启 nomad 触发一次很慢的网卡指纹（见 6.5）。**能不 `-i -s` 就不 `-i -s`。**

### 速查：改动类型 → 最小操作

| 你改了什么 | 最小操作 | 用重启 nomad 吗 |
|-----------|---------|:--------------:|
| **Go 源码**（orchestrator / api / template-manager 等） | 重建 RPM → `cp bin/orchestrator /usr/bin/{orchestrator,template-manager}` → `nomad job run` 重跑受影响的 job（下方场景一） | 否 |
| **`.env` 部署变量** | `bash build.sh -f`（只 render + 提交 job，下方场景二） | 否 |
| **nomad / consul 配置**（如 `network_speed`、`network_interface`） | 改 `/etc/nomad.d/default.hcl` → `systemctl restart nomad`（下方场景四） | 是（仅 nomad，不动别的） |
| **harbor / minio / postgres 等组件** | 单独重启对应服务（`systemctl restart` / `docker restart`），不用整套重来 | 否 |
| **彻底重来 / 换 ACL / 清状态** | `build.sh -d` → `-i` → `-s`（下方场景三，最慢） | 是 |

> ⚠️ 直接改 `/opt/e2b-infra/` 下的脚本/配置都是**临时的**——下次 `rpm -Uvh` 会覆盖。要持久，
> 得改回仓库源码 → 重建 `e2b-deploy.tar.gz` → 重建 RPM。

### 场景一：只改了 Go 源码（orchestrator / api / template-manager 等，通过 patch）

```bash
# 1) 重建并安装 RPM（只更新 /opt/e2b-infra/bin 下的二进制）
rpmbuild -bb e2b-infra.spec --define "_sourcedir $PWD"
rpm -Uvh ~/rpmbuild/RPMS/aarch64/e2b-infra-*.rpm

# 2) 刷新节点二进制
cp -f /opt/e2b-infra/bin/orchestrator /usr/bin/orchestrator
cp -f /opt/e2b-infra/bin/orchestrator /usr/bin/template-manager

# 3) 用【已有】token 重跑受影响的 job（不要重新 bootstrap）
cd /opt/e2b-infra && source .env
nomad job run --token "$NOMAD_ACL_TOKEN" rendered/template-manager.hcl
nomad job run --token "$NOMAD_ACL_TOKEN" rendered/api.hcl      # 按需
```

> ⚠️ 这种场景不要跑 `build.sh -i`（它会把 `.env` 的 token 重置成占位符）。

### 场景二：只改了 `.env` / 部署配置

```bash
cd /opt/e2b-infra && source .env     # 确认 NOMAD_ACL_TOKEN 是真 token（非占位符）
bash build.sh -f                     # = deploy：重新 render + 提交 job
```

### 场景三：彻底重来（搭建/调试阶段推荐，会销毁运行中的 sandbox/job）

```bash
bash build.sh -d        # 停止并清空 nomad/consul 数据（含 ACL 状态）
rpm -Uvh ...rpm         # 若改了源码
bash build.sh -i
bash build.sh -s        # 全新 bootstrap，自动生成新 token 并写回 .env
```

### 场景四：只改了 nomad / consul 配置（不用 `-i`/`-s`）

`append_nomad_client_config` 只在完整 `-s` 时才重生成配置、且已有配置会被跳过，所以只编辑
`build.sh` 不会让运行中的配置生效。为一行配置跑完整 `-s` 不值——直接改配置 + 重启 nomad 即可：

```bash
# 例：给 client 加 network_speed（幂等，重复跑不会加两次）
grep -q 'network_speed' /etc/nomad.d/default.hcl || \
  sed -i '/network_interface = /a network_speed = 1000' /etc/nomad.d/default.hcl
sed -n '/^client {/,/^}/p' /etc/nomad.d/default.hcl        # 确认加对
systemctl restart nomad                                    # 只重启 nomad
time (until ss -tln | grep -q ':4646'; do sleep 2; done); echo "4646 up"
```

> 别忘了把同样的改动落回仓库的 `e2b-deploy/dep/run-nomad.sh` 或 `build.sh`，否则重装 RPM 会丢。

---

## 6. 故障排查

### 6.1 `deploy.sh` 报 `403 (Permission denied)`

**现象**：`build.sh -s` 走到 `==> submitting nomad job...` 时报
`Error submitting job: Unexpected response code: 403 (Permission denied)`。

**原因**：Nomad ACL 的 bootstrap token 只在「第一次」生成、存于 `/data/nomad`。
`build.sh -i` 会用占位符 token 覆盖 `/opt/e2b-infra/.env`；若旧集群还在跑，`-s`
检测到 leader 会跳过 bootstrap、不刷新 token，于是 `deploy.sh` 拿占位符提交 job → 403。

> 新版脚本已修复：`run-nomad.sh` 首次 bootstrap 时把真 token 存到 `/data/nomad/acl.token`，
> `run-consul.sh` 存到 `/opt/consul/acl.token`；重跑（join 已有集群）时自动读回并写进 `.env`。
> 升级到含该修复的版本、并**干净地跑过一次 `-d` + `-s`** 让 token 落盘后，`-i + -s` 不再 403。

**手动恢复（任选其一）**：

```bash
# A. 推倒重来（搭建阶段最简单）
bash build.sh -d && bash build.sh -s

# B. 保留集群，只重置 ACL（不销毁 job）
nomad acl bootstrap                                   # 报错并给出 "reset index: N"
echo N | sudo tee /data/nomad/server/acl-bootstrap-reset
nomad acl bootstrap                                   # 成功，打印新的 Secret ID
sed -i "s|^export NOMAD_ACL_TOKEN=.*|export NOMAD_ACL_TOKEN=<新SecretID>|" /opt/e2b-infra/.env
bash build.sh -f
```

### 6.2 `build.sh -d` 时出现 `Error querying jobs: 403` —— 正常

`uninstall-nomad.sh` 想用（此时已失效的）token 优雅停 job，403 是预期的；脚本没开 `set -e`，
会继续把 `/data/nomad` 等目录删干净。看到后面的 `Removing: /data/nomad` 就说明清理成功。

### 6.3 `-s` 时 `Failed to connect to <IP> port 8500` —— 正常

`run-consul.sh` 在探测「是否已有 Consul 集群」（`curl .../v1/status/leader`）。干净启动时
Consul 还没起来，连接被拒属预期，脚本随后会自行 bootstrap（日志里紧跟着 `Bootstrapping ACL`）。

### 6.4 日志显示「Downloading Nomad ... skipping download」—— 没在下载

`install-nomad.sh` 先打印 "Downloading" 再判断 `/tmp` 里是否已有包，有就跳过。只要 `dep/`
里放了对应 zip（`-i` 会拷到 `/tmp`），就是离线走缓存，没有真的联网。consul 同理。

### 6.5 卡在「等待 tcp 端口 4646 启动 …… 端口未启动，继续等待」

`start()` 最后 `wait_for_port 4646 tcp 1 0`（超时 0 = 永不超时）用 `ss` 等 Nomad 的 HTTP
端口。它**不是卡死，是很慢**：Nomad 把 server+client 跑在同一个 agent 里，**HTTP 4646 要等
client 那半边启动完才开**，而 client 启动要「枚举本机每一个网络接口并逐个探测链路速率」。

**根因：宿主机网络接口太多**。诊断：

```bash
systemctl status nomad --no-pager | head -12     # active(running)、已选出 leader，但…
ss -tlnp | grep -E ':4646|:4647'                 # …只有 4647(RPC)，没有 4646(HTTP)
ip -o link show | wc -l                          # 接口总数
ip -o link show | grep -c veth                   # veth 数量（每个容器一对）
```

在共享主机上，**别人**跑几百个容器（docker / nerdctl / containerd）会在**宿主 network
namespace** 里造出成千上万个 `veth` 接口（实测见过 1900+）。Nomad 逐个 `ethtool` 探测，就要
好几分钟甚至十几分钟——**你最初部署时机器干净、几秒就好，后来邻居容器多了就变慢**。这些 veth
在宿主 netns 里对所有 root 进程可见，所以邻居的容器会拖慢你这边的 Nomad，属「吵闹邻居」效应。

> `veth` 数 ≈ `ip -o link | grep -c veth`；邻居的 containerd 容器数可用
> `nerdctl -n <namespace> ps -a | wc -l` 看。

**缓解（新版 `build.sh` 的 `append_nomad_client_config` 已自动加，升级即可）**：给 client 加两行——
`network_interface = "<主网卡>"`（跳过 `ip route` 自动探测）+ `network_speed = 1000`
（写死链路速率，**跳过逐接口 ethtool 探测**，这一步最省时间）。手动加的话：

```bash
# 编辑 /etc/nomad.d/default.hcl 的 client { ... } 块，加：
#     network_interface = "eno4"     # 换成持有 SERVER_IP 的网卡
#     network_speed     = 1000
systemctl restart nomad             # 重启后 client 指纹会快很多
ss -tlnp | grep 4646
```

**根治**：别把 e2b 的 Nomad client 和「几百容器的邻居」放同一台机器——独占一台、或把 client
放进独立 network namespace，才能彻底不受邻居 veth 影响。日常迭代也尽量**别反复 `-i -s`**（那会
重启 nomad、每次都要重新指纹）；改代码用第 5 节「场景一」，不重启 nomad 就不会触发这段等待。

> 另外：另一个终端 `curl 127.0.0.1:4646` 卡住，多半是 shell 里设了 `http_proxy`/`https_proxy`
> 把本地请求也走代理了——和这个无关，`unset http_proxy https_proxy` 或 `export no_proxy=127.0.0.1,localhost`。

---

## 7. 清理多次重跑累积的脏数据

旧版脚本若干步骤非幂等（新版 `start-client.sh`/`init-client.sh` 已改幂等），历史残留清一次即可：

```bash
# 0. 先看累积量
grep -c '^nameserver 127.0.0.1' /etc/resolv.conf
grep -c 'server=/consul/127.0.0.1#8600' /etc/dnsmasq.d/consul.conf 2>/dev/null
mount | grep -Ec '/mnt/snapshot-cache|/mnt/hugepages'

# 1. /etc/resolv.conf：删掉所有重复的 127.0.0.1，只在最前留一条
sed -i '/^nameserver 127\.0\.0\.1$/d' /etc/resolv.conf
sed -i '1i nameserver 127.0.0.1' /etc/resolv.conf

# 2. /etc/dnsmasq.d/consul.conf：去重后重启 dnsmasq
sort -u /etc/dnsmasq.d/consul.conf -o /etc/dnsmasq.d/consul.conf
systemctl restart dnsmasq

# 3. /etc/sysctl.conf：去重重复追加的内核参数块（保留空行）
awk '!NF || !seen[$0]++' /etc/sysctl.conf > /tmp/sysctl.dedup && cat /tmp/sysctl.dedup > /etc/sysctl.conf
sysctl -p

# 4. /mnt/snapshot-cache、/mnt/hugepages 叠加的挂载（无 job 运行时执行）
while mountpoint -q /mnt/snapshot-cache; do umount /mnt/snapshot-cache || break; done
while mountpoint -q /mnt/hugepages;     do umount /mnt/hugepages     || break; done
```

---

## 8. 仍需联网（或本地源）的部分

以下步骤不是「拷文件」能解决的，离线环境需提前配好本地源：

- `yum install -y curl unzip jq tar rsync dnsmasq`（`yum_install`）、`yum install -y nginx`（`install_nginx`）
- `pip install e2b==2.20.0 e2b_code_interpreter==2.4.1 python-dotenv`（`install_e2b`）
