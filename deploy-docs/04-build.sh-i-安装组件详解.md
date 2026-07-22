# 04 `build.sh -i` 安装组件详解

`bash /opt/e2b-infra/build.sh -i`（等价 `--install`）是装机第一条命令：把 E2B 运行所需的
**周边依赖**（PostgreSQL、MinIO、Harbor、Nginx、Python SDK）装好，并把 dep 里的增强版脚本
和离线安装包摆到位。**它不启动 consul/nomad、不部署任何 E2B 业务组件**——那是 `-s` 的事。

## 0. 执行前提（先核对再跑）

1. RPM 已装好（`/opt/e2b-infra/` 存在，`dep/` 在其下）；
2. **`dep/.env` 的 `SERVER_IP=` 已改成本机 IP**——`build.sh` 开头 `source $DEP_DIR/.env`，
   后续 harbor/registry/nomad 地址全部从它推导，写错则全盘皆错；
3. Docker **自备且在跑**（`install_docker` 已被注释，脚本不装 docker），并且
   `/etc/docker/daemon.json` 的 `insecure-registries` 已含 `SERVER_IP:2900`；
4. 离线大件已放进 `/opt/e2b-infra/dep/`：consul/nomad 的 zip、firecracker tgz、
   harbor 离线安装包、minio 二进制（文件名必须与脚本硬编码的一致，清单见 runbook §1.1）；
5. 三个 docker 镜像已 `docker load`：`postgres:latest`、`redis:7.4.4-alpine`、
   `debian:bookworm-slim`（runbook §1.2）；
6. yum 源、pip 源可用（离线环境配本地源，见 runbook §8）。

> ⚠️ **集群已在运行时慎用 `-i`**：`install_e2b` 会把 `/opt/e2b-infra/.env` 重置成
> dep 基线（占位符 token），之后 `deploy.sh` 提交 job 会 403（runbook §6.1）。
> `-i` 属于"全新搭建"路径。

## 1. 脚本入口与公共部分

```bash
WORK_DIR=$(cd $(dirname "$0") && pwd)   # 即 /opt/e2b-infra
DEP_DIR="$WORK_DIR/dep"
source $DEP_DIR/.env                    # 读部署基线（注意：不是顶层 .env）
HOST_IP=$SERVER_IP
```

要点：

- `build.sh` 的 IP、端口常量来自 **`dep/.env`**；而 `deploy.sh` 用的是**顶层 `.env`**。
  两份文件的分工见 01 篇 §5。
- 端口常量硬编码在脚本头：PG 5432、MinIO 9000/9001、Harbor HTTP **2900**、Nomad 4646、
  Consul 8500；Harbor 凭据 `admin/Harbor12345`。
- 参数解析用 getopt，`-i` 映射到 `install()` 函数。全部选项：
  `-i` 安装、`-u` 卸载、`-s` 启动、`-d` 停止、`-f` 部署、`-r <job>` 快速重跑、`-m <镜像>` 制作沙箱基础镜像。

## 2. `install()` 的执行顺序

```
install()
 ├─ yum_install            # 基础工具 + dnsmasq
 ├─ setenforce 0           # 关 SELinux（运行时，非永久）
 ├─ (install_docker)       # ← 已注释，docker 自备
 ├─ install_postgre        # PostgreSQL 容器
 ├─ install_minio          # MinIO systemd 服务
 ├─ install_harbor         # 只解压+生成配置，不安装不启动
 ├─ install_nginx          # https://harbor:443 反代 + 证书三处信任
 └─ install_e2b            # SDK + dep overlay + SDK http 补丁 + dnsmasq 域名
```

下面逐个函数拆。

## 3. 逐函数详解

### 3.1 `yum_install`

```bash
yum install -y curl unzip jq tar rsync
yum install -y dnsmasq
```

- `unzip/tar`：解 consul/nomad zip、firecracker tgz；`jq`：解析 nomad/consul 的 JSON 输出
  （uninstall 脚本、ACL 处理都用）；`rsync`：同步类操作。
- `dnsmasq` 是**部署的关键组件**：后面要用它接管 `*.e2b.app`（沙箱域名）和
  `*.consul`（服务发现域名）的解析。本部署用 dnsmasq 替代了上游的 systemd-resolved。

### 3.2 `setenforce 0`

临时关闭 SELinux（enforcing→permissive）。沙箱要做大量非常规动作（nbd 设备、netns、
raw_exec、hugetlbfs），SELinux 策略未适配，直接放开。注意这**不改** `/etc/selinux/config`，
重启后会恢复 enforcing——重启后的恢复动作之一（见 06 篇 §8.4）。

### 3.3 `install_postgre`（注意函数名没有 s）

1. 校验 `dep/postgres.tar` 存在（**只校验不加载**——镜像加载在被注释的 `install_docker`
   里，所以实际要求你提前 `docker load postgres:latest`，这正是 runbook §1.2 强调的坑）；
2. 已有同名容器则**先停删**（避免端口/名字冲突）；
3. 启动容器：

```bash
docker run -d --name postgres \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=local -e POSTGRES_DB=mydatabase \
    -p 5432:5432 --restart=always \
    --health-cmd="pg_isready -U postgres" --health-interval=5s ... \
    postgres:latest
```

   - 连接串即 `.env` 里的 `POSTGRES_CONNECTION_STRING`：
     `postgresql://postgres:local@localhost:5432/mydatabase?sslmode=disable`；
   - `--restart=always`：宿主机重启后容器自动拉起（数据在容器可写层里，**没挂卷**——
     `docker rm` 会连数据一起丢，这是当前设计的取舍，备份见 06 篇 §5.4）；
4. 轮询容器健康状态最多 10×2s，不健康则报错退出。

### 3.4 `install_minio`

1. 校验 `dep/minio`（二进制）、`dep/minio.yml`、`dep/minio.service` 存在；
2. `minio` 二进制 → `/usr/local/bin/minio`；
3. 数据目录 `mkdir -p /root/data/minio`；
4. 配置落位：`dep/minio.yml → /etc/default/minio.yml`（数据目录、控制台 :9001、
   凭据 `minioadmin/minioadmin`）；`dep/minio.service → /etc/systemd/system/minio.service`；
5. `systemctl daemon-reload && enable && start minio`；
6. 健康检查轮询 `http://SERVER_IP:9000/minio/health/ready` 最多 10×2s，等 HTTP 200。

MinIO 在整个系统里存**模板与构建缓存**（`.env`：`TEMPLATE_BUCKET_NAME=e2b-dev-fc-templates`、
`BUILD_CACHE_BUCKET_NAME=e2b-dev-fc-cache`，`STORAGE_PROVIDER=MinioBucket`）。

### 3.5 `install_harbor`——只"备菜"，不"下锅"

1. 校验 `dep/harbor-offline-installer-aarch64-v2.13.0.tgz`；
2. 解压到 `/opt/e2b-infra/harbor/`；
3. `cp harbor.yml.tmpl harbor.yml`，然后用 sed 改配置：
   - **注释掉整个 https 块**（port 443/certificate/private_key）——Harbor 走纯 HTTP；
   - `hostname:` 改成本机 IP；
   - HTTP 端口改成 **2900**（默认 80 会跟 nginx/沙箱入口撞）。

**注意：此函数不执行 `harbor/install.sh`**。真正安装（prepare + 起 docker-compose 容器组）
发生在 `build.sh -s` 的第一步。这样拆的效果是：`-i` 全程不依赖 harbor 在线，且 `-s`
可以在 harbor 配置就绪的前提下反复执行。

### 3.6 `install_nginx`——给 HTTP Harbor 套一层可信 HTTPS

背景：E2B 模板构建器拉基础镜像（`FROM harbor:443/...`）**只接受 https 且证书可验**，
而 Harbor 是 http。解决方案是 nginx 反代 + 自签证书 + 三处信任：

1. `yum install -y nginx`；
2. `dep/harbor.cnf → /etc/nginx/ssl/`、`dep/nginx.conf → /etc/nginx/nginx.conf`
   （443 vhost `server_name harbor` → `proxy_pass http://127.0.0.1:2900`，
   `client_max_body_size 2G` 保证大镜像层能推）；
3. 生成自签证书（CN=harbor、SAN DNS:harbor、带 CA:TRUE）：
   `/etc/nginx/ssl/harbor.{key,crt}`；
4. **信任点一：系统 CA 库**——`cp harbor.crt /etc/pki/ca-trust/source/anchors/harbor-ca.crt
   && update-ca-trust extract`。E2B 构建器（Go 程序）走系统信任库校验，这一步是模板构建
   能拉 harbor 镜像的关键；
5. **信任点二：docker 信任目录**——`/etc/docker/certs.d/harbor:443/ca.crt`，docker daemon
   对 `harbor:443` 的 pull/push 用；
6. **信任点三（进程级）**：`template-manager.hcl` 的 env 里还有
   `SSL_CERT_FILE=/etc/docker/certs.d/harbor:443/ca.crt`，给 orchestrator 进程兜底；
7. `systemctl start && enable nginx`，`nginx -t` 校验后 reload。

> `harbor` 这个主机名的**解析**脚本没有自动配置：单机部署需保证它解析到本机
> （通常 `/etc/hosts` 加 `127.0.0.1 harbor`）。证书 SAN 只签了 `DNS:harbor`，
> 所以模板 Dockerfile 里必须写 `harbor:443/...` 而不是 `IP:443`。

### 3.7 `install_e2b`——SDK、overlay、补丁、域名解析

这是 `-i` 里信息量最大的函数，做四类事：

**① 安装 Python SDK**

```bash
pip install e2b==2.20.0 e2b_code_interpreter==2.4.1 python-dotenv
```
版本是硬约束（补丁按这两个版本做的），别升级。

**② 重放 dep overlay（把增强版盖到 RPM 铺的上游版上）**

| dep 源 | 目标 | 备注 |
|---|---|---|
| `install-nomad.sh`、`install-consul.sh`、`uninstall-nomad.sh` | `/opt/e2b-infra/` | 离线缓存版安装/卸载脚本 |
| `consul_1.21.4_linux_arm64.zip` | `/tmp/consul.zip` | install-consul.sh 认这个缓存路径 |
| `nomad_1.10.4_linux_arm64.zip` | `/tmp/` | install-nomad.sh 认 `/tmp/nomad_<ver>_linux_<arch>.zip` |
| **`.env`** | `/opt/e2b-infra/.env` | ⚠️ **顶层 .env 被重置为基线**（占位 token）——集群在跑时的 403 之源 |
| `template-manager.hcl` | `nomad/template-manager.hcl` | 换成合体进程+双开关的增强版 |
| `start-client.sh`、`start-server.sh`、`init-client.sh`、`run-nomad.sh`、`run-consul.sh`、`deploy.sh` | `/opt/e2b-infra/` | 全套增强版部署脚本（含 `deploy.sh --only`） |

**③ 给 SDK 打补丁（http 化 + 同步构建适配）**

```bash
SITE=$(python3 -c "import e2b,os;print(os.path.dirname(os.path.dirname(e2b.__file__)))")
cp dep/code_interpreter_sync.py  $SITE/e2b_code_interpreter/
cp dep/connection_config.py      $SITE/e2b/
cp dep/dockerfile_parser.py      $SITE/e2b/template/
cp dep/build_api.py dep/main.py  $SITE/e2b/template_sync/
python /opt/e2b-infra/patch_e2b.py     # 再统一做 https→http 替换 + 清 __pycache__
```

`patch_e2b.py` 可重复执行（幂等，首次改动前留 `.backup`）。效果：SDK 所有对外连接
（API、run_code、volume、MCP）都走 http，适配无证书内网。

**④ dnsmasq 接管沙箱域名**

```bash
echo "address=/.e2b.app/127.0.0.1" >> /etc/dnsmasq.conf   # 幂等：已有则跳过
systemctl restart dnsmasq
```

`*.e2b.app`（沙箱访问域名，SDK 的 `E2B_DOMAIN`）全部解析到本机，配合 `-s` 末尾的
iptables 80→3002，沙箱 URL 在部署机上开箱即用。

## 4. `-i` 结束后系统处于什么状态

| 组件 | 状态 |
|---|---|
| postgres 容器 | **运行中**（healthy） |
| minio | **运行中**（systemd enabled） |
| nginx | **运行中**（443 反代就绪，证书已进系统信任库） |
| dnsmasq | **运行中**（*.e2b.app → 127.0.0.1） |
| harbor | **未运行**——已解压到 `/opt/e2b-infra/harbor/` 且 harbor.yml 已生成，等 `-s` 安装 |
| consul / nomad | **未安装未运行**——zip 已缓存到 `/tmp`，脚本已就位，等 `-s` |
| E2B 业务组件 | 无。二进制在 `bin/` 里躺着，`/usr/bin` 还没有 orchestrator |
| `/opt/e2b-infra/.env` | = dep 基线（**占位符 token**），等 `-s` bootstrap 后写入真值 |
| SDK | 已装 2.20.0/2.4.1 并打好 http 补丁 |

## 5. 幂等性与重复执行

- 各安装函数基本可重复执行：postgres 先删旧容器、minio/nginx 重复 cp+restart 无害、
  harbor 重复解压覆盖、dnsmasq 配置有存在性检查；
- 唯一"有破坏性"的是 **`.env` 被重置**。如果只是想补某一步（比如重放 overlay），
  照 runbook §5.0 手动 cp 对应文件，**别整跑 `-i`**；
- `uninstall()`（`-u`）是 `-i` + 部分 `-s` 的逆操作：卸 nomad/consul（含数据）、
  `rpm -e e2b-infra`、卸 nginx/harbor/postgres（minio/docker 保留）。

## 6. 常见问题

| 现象 | 原因/处置 |
|---|---|
| `PostgreSQL 镜像文件不存在：dep/postgres.tar` | 只是校验文件在不在；真正要保证的是 `docker images` 里有 `postgres:latest`（提前 load） |
| `启动 PostgreSQL 容器失败` + `Unable to find image` | 镜像没提前 `docker load`（install_docker 被注释，不会自动加载 dep 下的 tar） |
| MinIO 健康检查超时 | `journalctl -u minio -f` 看原因；常见是 9000 端口被占或数据目录权限 |
| `openssl` 生成证书失败 | `dep/harbor.cnf` 没被正确拷到 `/etc/nginx/ssl/`；或系统缺 openssl |
| pip 装不上 | 离线环境未配本地 pip 源（runbook §8） |
| 跑完 `-i` 后 nomad 命令 403 | 正常——`.env` 是占位 token。继续跑 `-s`（全新环境）或按 runbook §6.1 恢复真 token（已有集群） |
