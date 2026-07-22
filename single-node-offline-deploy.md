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

### 0.2 自定义 nbd 内核模块（`nbd.ko`，需自备，**手动加载**）

`start-client.sh` / `init-client.sh` **不自动加载 nbd 模块**（脚本里原有的 `rmmod`+`insmod`
逻辑已删除——模块已加载时重复 `insmod` 会报
`insmod: ERROR: could not insert module nbd.ko: File exists`，而模块本来只需加载一次）。
你**自己编译的优化版 nbd 模块**（`nbds_max=512`）改为部署前手动加载一次即可，加载后长期有效；
**服务器每次重启后模块会丢，需重新手动执行一遍**：

```bash
# 先卸载内核自带（或旧的）nbd，未加载时忽略报错；再装自定义模块
sudo rmmod nbd 2>/dev/null || true
sudo insmod /home/j30059180/tools/nbd-patch/nbd.ko nbds_max=512

# 验证
lsmod | grep nbd
ls /dev/nbd* | wc -l    # 应为 512
```

两个注意点：

1. **vermagic 必须与运行内核完全一致**。`insmod` 会校验模块 vermagic 是否等于本机 `uname -r`，
   不一致直接报 `Invalid module format`（`modprobe` 不校验这么严）。检查：
   ```bash
   modinfo /home/j30059180/tools/nbd-patch/nbd.ko | grep vermagic
   # vermagic: 6.6.0_6.6.0_515-uffd_copy_open_tree SMP mod_unload modversions aarch64
   uname -r        # 必须与上面 vermagic 里的内核版本一致
   ```
   内核不同就要在目标机对应内核上重新编译 `nbd.ko`。

2. **重复 `insmod` 报 `File exists` = 模块已在**，无需处理。若要换新编译的 `.ko`，
   先确认没有 nbd 设备在用，`rmmod nbd` 后再 `insmod`。

---

## 1. 准备离线大件与镜像

这些体积大、**不在 RPM 里**，需在有网机器上下载后拷到目标机。

### 1.1 二进制 / 安装包 → 拷到 `/opt/e2b-infra/dep/`

文件名必须与下面完全一致（`build.sh` / `install-*.sh` 按名字找）：

```bash
# 在有网机器上下载（aarch64）
wget https://releases.hashicorp.com/consul/1.21.4/consul_1.21.4_linux_arm64.zip
wget https://releases.hashicorp.com/nomad/1.10.4/nomad_1.10.4_linux_arm64.zip
wget https://github.com/goharbor/harbor/releases/download/v2.13.0/harbor-offline-installer-aarch64-v2.13.0.tgz
wget -O minio https://dl.min.io/server/minio/release/linux-arm64/minio
```

拷到目标机后（RPM 装完会生成 `/opt/e2b-infra/dep/`，见第 2 步）：

```bash
cp consul_1.21.4_linux_arm64.zip harbor-offline-installer-aarch64-v2.13.0.tgz \
   nomad_1.10.4_linux_arm64.zip  minio  /opt/e2b-infra/dep/
```

> firecracker 不用下载：RPM 自带仓库定制版 v1.12.1（`/opt/e2b-infra/bin/firecracker`），
> `init-client.sh` 直接把它安装到 `/fc-versions/v1.12.1/firecracker`。

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

# 1b) git lfs pull 拉不动时（LFS 服务器 https://artlfs.openeuler.openatom.cn 不可达/被代理拦截），
#     从上游 GitHub 下载同一份源码后重打包。注意：GitHub 归档解包后的顶层目录叫 infra-2026.09，
#     而 spec 的 %autosetup 期望 e2b-infra-2026.09，必须解包改名再重新打包，只改文件名没用
wget https://github.com/e2b-dev/infra/archive/refs/tags/2026.09.tar.gz -O infra-2026.09.tar.gz
tar -xzf infra-2026.09.tar.gz && mv infra-2026.09 e2b-infra-2026.09
tar -czf e2b-infra-2026.09.tar.gz e2b-infra-2026.09
rm -rf e2b-infra-2026.09 infra-2026.09.tar.gz

# 2) 改 e2b-deploy/dep/.env：SERVER_IP= 必须改成本机 IP（否则 harbor/registry/nomad 地址全错），
#    改完重建打进 RPM 的部署包 e2b-deploy.tar.gz（改了其它端口/变量同理）
sed -i 's/^SERVER_IP=.*/SERVER_IP=<本机IP>/' e2b-deploy/dep/.env
tar -czf e2b-deploy.tar.gz e2b-deploy

# 3) 构建 RPM
rpmbuild -bb e2b-infra.spec --define "_sourcedir $PWD"

# 4) 安装到 /opt（会生成 /opt/e2b-infra/ 及 /opt/e2b-infra/dep/）
#    Release 要写明（当前 -4，与 e2b-infra.spec 的 Release 同步递增）；
#    别用 e2b-infra-*.rpm 通配——RPMS 目录里若还留着旧 -3 包会一起匹配进来报冲突
rpm -ivh ~/rpmbuild/RPMS/aarch64/e2b-infra-2026.09-4.aarch64.rpm      # 升级用 rpm -Uvh --force
```

> 关键：`dep/.env` 的 `SERVER_IP=` 必须改成本机 IP，否则 harbor/registry/nomad 地址全错。

---

## 3. 放置离线大件

RPM 装完后，执行第 1 步的拷贝与 `docker load`（把 4 个文件放进 `/opt/e2b-infra/dep/`，加载 3 个镜像）。

---

## 4. 安装组件并启动

```bash
cd /opt/e2b-infra
bash build.sh -i      # 装 postgres/minio/harbor/nginx/e2b，并把 nomad/consul 缓存到位
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

### 速查：改动类型 → 最小操作（顺序与下方场景一一对应）

| # | 你改了什么 | 最小操作 | 用重启 nomad 吗 |
|---|-----------|---------|:--------------:|
| 场景一 | **彻底重来 / 换 ACL / 清状态** | `build.sh -d` → `-i` → `-s`（最慢） | 是 |
| 场景二 | **Go 源码**（orchestrator / api / template-manager 等，通过 patch） | 重建 RPM → 重放 dep overlay（5.0）→ `cp` 二进制到 /usr/bin → `nomad job stop`+`run` 强制重启 | 否 |
| 场景三 | **单个 job 的 env**（如 `E2B_FC_LAUNCH_MODE`、`MAX_STARTING_INSTANCES_PER_NODE`） | 改 `nomad/<job>.hcl` → `bash build.sh -r <job>` → 四层验证（`-r` 细节见第 11 节） | 否 |
| 场景四 | **`.env` 部署变量** | `bash build.sh -f`（重新 render + 提交全部 job） | 否 |
| 场景五 | **nomad / consul 配置**（如 `network_speed`、`network_interface`） | 改 `/etc/nomad.d/default.hcl` → `systemctl restart nomad` | 是（仅 nomad，不动别的） |
| —— | **harbor / minio / postgres 等组件** | 单独 `systemctl restart` / `docker restart` 对应服务即可，无需专门场景 | 否 |

> ⚠️ 直接改 `/opt/e2b-infra/` 下的脚本/配置都是**临时的**——下次 `rpm -Uvh` 会覆盖。要持久，
> 得改回仓库源码 → 重建 `e2b-deploy.tar.gz` → 重建 RPM。

**`rpm -Uvh --force` 到底覆盖 `/opt/e2b-infra/` 里的什么**（以 spec 的 `%files` 清单为准：
在清单里的文件升级时一律被新包内容替换；不在清单里的一律不动）：

| 路径 | 升级时 | 说明 |
|---|:--:|---|
| `bin/*`（orchestrator、envd、firecracker、vmlinux.bin、goose、migrations…） | ✅ 覆盖 | 新二进制正是从这里来的 |
| `nomad/*.hcl`（render 模板） | ✅ 覆盖 | ⚠️ 被重置成「上游+patch 默认版」——`ORCHESTRATOR_SERVICES` 缺 `orchestrator`、`E2B_FC_LAUNCH_MODE` 整行消失（缺省=disabled）、`MAX_STARTING_INSTANCES_PER_NODE` 回 500。修复=重放 overlay（5.0） |
| `deploy.sh`、`start-*.sh`、`run-*.sh`、`install-*.sh`、`uninstall-*.sh`、`init-client.sh`、`env.template`、`nomad.service` | ✅ 覆盖成**上游 iac 版** | ⚠️ 同类坑：dep/ 侧增强丢失——`deploy.sh` 丢 `--only`（之后 `build.sh -r` 报 `Unknown parameter: --only`）。修复=重放 overlay（5.0）。（定制 nbd 模块已改为手动加载，见 0.2，不再受此影响） |
| `dep/*`（含 `dep/.env`、`dep/template-manager.hcl`） | ✅ 覆盖 | 重置为仓库里的部署基线——重放 overlay 的**来源**就是这里 |
| `build.sh`、`*.py`、`helm/*` | ✅ 覆盖（仓库版） | 与仓库同步，无坑——注意 `build.sh` 是新版而 `deploy.sh` 可能是旧版，两者错配正是上面坑的表现 |
| `.env`（顶层，含 bootstrap 出来的真实 `NOMAD_ACL_TOKEN`） | ❌ 不动 | 不在 `%files` 里——**这就是场景二不需要重新 bootstrap 的原因** |
| `rendered/*`（上次渲染并提交给 nomad 的产物） | ❌ 不动 | 幸存——场景二 stop+run 提交的就是它，自定义 env 因此还在 |
| `/usr/bin/orchestrator`、`/usr/bin/template-manager` | ❌ 不动 | 在 `/opt` 之外，rpm 从不管——所以场景二必须手动 `cp` 刷新 |

### 5.0 本质：完整部署 = RPM 铺底 + dep overlay + bootstrap 产物（rpm 之后必做重放）

部署机上的 `/opt/e2b-infra/` 是三层叠出来的：

1. **RPM 铺底**——`bin/*`、`nomad/*.hcl`、`deploy.sh` 等部署脚本，内容是**上游源树(+patch) 版**；每次 `rpm -Uvh` 整层重铺。
2. **dep overlay**——`build.sh -i` 把 `dep/` 里的**增强版**盖到对应位置（`deploy.sh` 的 `--only`、`template-manager.hcl` 的 `orchestrator,template-manager` 等）。**rpm 重铺第 1 层时，这层被打回上游版。**
3. **bootstrap / 运行产物**——顶层 `.env`（真 token）、`rendered/*`、`/etc` 下的组件配置、python site-packages 里的 SDK 补丁。rpm 一概不碰；但 `build.sh -i` 会重置 `.env`、`-s` 会重新 bootstrap——**所以不能用 `-i -s` 来补第 2 层**。

推论：**每次 `rpm -Uvh` 之后，手动重放第 2 层（= 执行 `-i` 中安全的那部分，唯独跳过 `.env`）**：

```bash
# ===== rpm -Uvh 之后的必做动作：重放 dep overlay =====
cd /opt/e2b-infra
for f in deploy.sh start-client.sh start-server.sh init-client.sh run-nomad.sh run-consul.sh \
         install-nomad.sh install-consul.sh uninstall-nomad.sh template-manager.hcl; do
  [ -f "dep/$f" ] || continue
  case "$f" in
    *.hcl) cp -f "dep/$f" "nomad/$f" ;;   # render 模板放 nomad/ 下
    *)     cp -f "dep/$f" "$f"      ;;   # 部署脚本放顶层
  esac
done
# ⚠️ 唯独不要 cp dep/.env —— 那会把真实 NOMAD_ACL_TOKEN 冲成占位符（见 6.1）
# 重放后 nomad/template-manager.hcl 回到 dep 基线（值以 dep/ 里的内容为准）——
# 如有自定义 env（例：MAX_STARTING_INSTANCES_PER_NODE=30），按场景三重新套上再 render。
```

> 覆盖面核查：其余被 rpm 覆盖的项无需重放——其他 `nomad/*.hcl`（api/edge/…）没有 dep 定制版；
> `env.template`、`nomad.service`、`helm/*` 不参与单机运行时。第 3 层（`.env`、`rendered/`、
> `/etc` 组件配置、SDK 补丁）rpm 都不碰，无需处理。

### 场景一：彻底重来（搭建/调试阶段推荐，会销毁运行中的 sandbox/job）

```bash
bash build.sh -d        # 停止并清空 nomad/consul 数据（含 ACL 状态）
rpm -Uvh --force ~/rpmbuild/RPMS/aarch64/e2b-infra-2026.09-4.aarch64.rpm   # 若改了源码
bash build.sh -i
bash build.sh -s        # 全新 bootstrap，自动生成新 token 并写回 .env
```

### 场景二：只改了 Go 源码（orchestrator / api / template-manager 等，通过 patch）

```bash
# 1) 重建并安装 RPM（只为更新 /opt/e2b-infra/bin 下的二进制）
#    --force：同一个 Release（当前 -4）反复改源码重建时也能覆盖安装，不然 rpm 会报 already installed；
#    版本号写明 -4，别用 e2b-infra-*.rpm 通配（会把旧 -3 包一起匹配进来报冲突）
rpmbuild -bb e2b-infra.spec --define "_sourcedir $PWD"
rpm -Uvh --force ~/rpmbuild/RPMS/aarch64/e2b-infra-2026.09-4.aarch64.rpm

# 2) 重放 dep overlay（见 5.0——rpm 之后的必做动作，不做则下次 render/-r/-f 必炸）

# 3) 刷新节点二进制（/usr/bin 在 /opt 之外，rpm 不管，必须手动）
cp -f /opt/e2b-infra/bin/orchestrator /usr/bin/orchestrator
cp -f /opt/e2b-infra/bin/orchestrator /usr/bin/template-manager

# 4) 用【已有】token 强制重启受影响的 job（不要重新 bootstrap）
#    ⚠️ 只 `nomad job run` 不行：换的是二进制、hcl 内容没变，nomad 会判定“无变更”，
#    不替换 allocation，旧进程继续跑旧二进制——现象就是“改动没生效”、alloc ID 不变。
#    必须先 stop 再 run 强制重建 alloc（stop 会杀掉该节点上正在跑的沙箱，注意时机）。
#    提交幸存的 rendered/*（= 正在运行的 env）：二进制换血、env 保持不变，正是本场景的语义；
#    想连 env 一起改，重启完再走场景三。
cd /opt/e2b-infra && source .env
nomad job stop --token "$NOMAD_ACL_TOKEN" template-manager-system
nomad job run  --token "$NOMAD_ACL_TOKEN" rendered/template-manager.hcl
# 改动波及 api 时才需要（job 名以 nomad job status 为准）：
# nomad job stop --token "$NOMAD_ACL_TOKEN" api && nomad job run --token "$NOMAD_ACL_TOKEN" rendered/api.hcl

# 5) 验证新二进制真的在跑（三个检查都过才算部署成功；特征串换成你本次改动新增的字符串）
strings /usr/bin/template-manager | grep -c "acquire wait cost"    # ≥1 = 磁盘上的二进制是新的
pid=$(pgrep -f /usr/bin/template-manager | head -1)
strings /proc/$pid/exe | grep -c "acquire wait cost"               # ≥1 = 运行中的进程也是新的（0=还在跑旧进程）
ps -o pid,lstart,cmd -p $pid                                       # lstart 应晚于本次 stop/run 的时间
```

> ⚠️ 这种场景不要跑 `build.sh -i`（它会把 `.env` 的 token 重置成占位符）。

### 场景三：改单个 job 的 env 变量（通用流程）

适用于任何写在 job hcl `env {}` 块里的变量（如 `E2B_FC_LAUNCH_MODE`、`MAX_STARTING_INSTANCES_PER_NODE`、
`ORCHESTRATOR_SERVICES`……）。**先分清变量在哪一层，别走错场景**：

- hcl 里值是**写死的字符串**（如 `= "launch"`）→ 本场景：改模板 → render → 重跑该 job
- hcl 里写的是 **`${XXX}` 占位**、真实值来自部署 `.env` → 场景四（改 `.env` → `build.sh -f`）
- 代码里会读（`env.GetEnv...`）但 hcl **没写这一行** → 程序在用内置默认值；想改就在 `env {}` 里**加上这一行**，仍走本场景
- 不是进程 env、是 nomad/consul 自身配置 → 场景五

**通用三步（每改一次值都重复 1→3；下面以 template-manager 为例，换别的 job 就换 hcl 名）**：

```bash
cd /opt/e2b-infra

# 0)【一次性；以及每次 rpm -Uvh 之后】确认 overlay 没被 rpm 打回上游版
#    快检两条，任一不过 ⇒ 先按 5.0 重放 dep overlay，再重套自定义值：
grep -E 'ORCHESTRATOR_SERVICES|E2B_FC_LAUNCH_MODE|MAX_STARTING' nomad/template-manager.hcl
#   ↑ ORCHESTRATOR_SERVICES 必须含 orchestrator、E2B_FC_LAUNCH_MODE 行必须存在
grep -c -- '--only' deploy.sh
#   ↑ 必须 ≥1；0 = deploy.sh 被重置（症状就是 build.sh -r 报 "Unknown parameter: --only"）

# 1) 改 render 模板 nomad/<job>.hcl 里的目标变量
#    ——不要改 rendered/（渲染产物，下次 render 就被覆盖）；手编或 sed 均可：
sed -i -E 's/^(\s*E2B_FC_LAUNCH_MODE\s*=\s*).*/\1"netns-exec"/' nomad/template-manager.hcl

# 2) render + 重跑该 job（deploy.sh 自己会 source .env，这步不用手动 source）
#    env 值变了 ⇒ job spec 有差异 ⇒ nomad 自动替换 allocation，本场景【不需要】stop。
#    （反之：一个值都没变时 run 是 no-op——那说明你要的其实是“重启”，走场景二第 4 步 stop+run）
bash build.sh -r template-manager

# 3) 四层验证，从源头到进程，哪层断了问题就在哪层：
source .env
grep 目标变量 nomad/template-manager.hcl                # ① 模板层：第 1 步改的
grep 目标变量 rendered/template-manager.hcl             # ② 渲染层：第 2 步生成的
nomad job inspect -token "$NOMAD_ACL_TOKEN" template-manager-system | grep 目标变量   # ③ nomad 收到的 spec
pid=$(pgrep -f /usr/bin/template-manager | head -1)
tr '\0' '\n' < /proc/$pid/environ | grep 目标变量        # ④ 运行进程的真实 env（最终裁决）
ps -o pid,lstart,cmd -p $pid                            # lstart 应晚于本次 -r（证明 alloc 换过了）
```

**多组值对照扫描**（例：3 种 `E2B_FC_LAUNCH_MODE` × 2 种 cap = 6 组）：每组 = 重复 1→3，然后跑一轮压测、
记下本组参数（`run_benchmark.py --fc-launch-mode <mode>` 会写进 meta.json，其余参数可自己往
`runs/$(cat runs/.latest)/` 里放个备注文件）。相邻两组只要有**至少一个值不同**，spec 就有差异、
alloc 就会被替换——按“蛇形”排列组合（每步只改一个变量）最稳。

> 持久化提醒：`nomad/<job>.hcl` 会被下次 `rpm -Uvh` 覆盖（见 5.0）。扫描完想把某组值定为
> 长期默认 → 改仓库 `e2b-deploy/dep/template-manager.hcl` → 重建 `e2b-deploy.tar.gz` → 重建 RPM。
> 相关细节：`E2B_FC_LAUNCH_MODE` 背景见第 9 节，`MAX_STARTING_INSTANCES_PER_NODE` 见第 10 节，
> `build.sh -r` 的实现与更多验证手段见第 11 节。

### 场景四：只改了 `.env` / 部署配置

```bash
cd /opt/e2b-infra && source .env     # 确认 NOMAD_ACL_TOKEN 是真 token（非占位符）
bash build.sh -f                     # = deploy：重新 render + 提交全部 job
```

> 前提：上次 `rpm -Uvh` 之后已按 5.0 重放过 overlay——`-f` 会从 `nomad/*.hcl` 重新 render
> 全部模板，模板若还是被 rpm 重置的上游版，渲染出来的 job 就是坏的（orchestrator 服务消失）。

### 场景五：只改了 nomad / consul 配置（不用 `-i`/`-s`）

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
重启 nomad、每次都要重新指纹）；改代码用第 5 节「场景二」，不重启 nomad 就不会触发这段等待。

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

---

## 9. Firecracker 启动优化档位（`E2B_FC_LAUNCH_MODE`）

高并发启动沙箱时，`[ResumeSandbox]` 的 `configured fc cost`（拉起 FC 进程 + 等 API socket）是主要瓶颈
（100 并发实测 ~240ms，详见 `benchmark/启动耗时阶段分析.md`）。patch `0002-fc-launch-dedicated-helper.patch`
给 orchestrator 加了一个**运行时开关** `E2B_FC_LAUNCH_MODE`，四档启动机制可**免重编**切换、A/B 对比
（两个优化档的动机/原理/实现详解见 `benchmark/FC启动优化-netns-exec.md` 与 `benchmark/FC启动优化-launch.md`）：

| 档 | `E2B_FC_LAUNCH_MODE` | 机制 |
|----|----------------------|------|
| 1（默认，不优化） | `disabled` | 原始 `unshare -m -- bash -c "… ip netns exec <ns> firecracker"` 全 shell 管道；轮询等 socket。装完 RPM 未改就是这档，行为与上游一致 |
| 2（中） | `netns-exec` | 同一条 shell 管道，但末尾 `ip netns exec` 换成 `fc-netns-exec` 助手（setns+execve），省掉 iproute2 的额外挂载/sysfs 开销 |
| 3（强） | `launch` | 专用无 shell 的 `fc-launch` 助手，一个小进程里做完 挂载+setns+execve；经 `unshare --mount --propagation unchanged` 包装启动（要求 util-linux ≥ 2.26）——mount ns 在子进程创建且跳过 unshare(1) 默认的递归 remount，orchestrator 的 `cmd.Start()` 保持纯 vfork+execve（旧版用 `Cloneflags` 在父进程 clone(2) 时创建 ns，100 并发实测挂载表复制持全局 namespace_sem 锁在 spawn 路径上排成车队，spawn avg 143ms vs 6.6ms，已废弃，详见 `benchmark/FC启动优化-launch.md` §3.2）；由 fc-launch 只对承载 per-sandbox tmpfs 的挂载点做非递归 `MS_PRIVATE`（O(路径深度)）；等 socket 用 inotify 而非轮询 |
| 4（最强） | `launch-c` | 档 3 的单线程 C 重写：`fc-launch-c`（~200 行，二进制 ~17KB）在 execve 后**自己** `unshare(CLONE_NEWNS)`（单线程合法；Go 因 runtime 多线程做不到，这正是档 3 需要 unshare(1) 包装的根因），orchestrator 直接 spawn——比档 3 再省一次 execve、Go runtime 自启动 ~1-2ms，且不依赖 util-linux 选项；plan 以有序 argv flags 传递（C 侧零解析）；传播保护/inotify 等 socket 与档 3 相同。详见 `benchmark/FC启动优化-launch-c.md` |

### 9.1 在哪配

真正跑沙箱、执行 `ResumeSandbox`/拉 FC 的 orchestrator 进程，是 **`template-manager.hcl`** 这个 nomad job
起来的——它设了 `ORCHESTRATOR_SERVICES = "orchestrator,template-manager"`，**一个进程同时跑
orchestrator + template-manager 两个 service**。所以开关加在它的 `env {}` 块里。

- 仓库源头（持久，扛得住 `build.sh -i` / `rpm -Uvh` 覆盖）：`e2b-deploy/dep/template-manager.hcl`
- 部署机上被 `deploy.sh` 渲染的模板：`/opt/e2b-infra/nomad/template-manager.hcl`

`env {}` 里加一行（**用字面量，别用 `${...}`**——`deploy.sh` 的 `envsubst` 有变量白名单，
`${E2B_FC_LAUNCH_MODE}` 不在名单里会被清空成空串，反而回落到 `disabled`）：

```hcl
      env {
        ...
        ORCHESTRATOR_SERVICES         = "orchestrator,template-manager"
        E2B_FC_LAUNCH_MODE            = "launch"     # disabled | netns-exec | launch
        ...
      }
```

> 本仓库已把默认值设为 `launch`。要换档/回退，改这一行的值即可（`disabled` = 关掉优化）。

### 9.2 前置：助手二进制必须在位（档 2/档 3）

`launch` / `netns-exec` 会去调 `/opt/e2b-infra/bin/{fc-launch,fc-netns-exec}`（路径可用
`E2B_FC_LAUNCH_HELPER` / `E2B_FC_NETNS_EXEC_HELPER` 覆盖）。这两个助手由 patch 0002 的 Makefile
`make build` 产出、spec 的 `packages/*/bin/*` glob 装到该路径，所以**含 patch 0002 的 RPM 里已经有**。
切档前先校验：

```bash
ls -l /opt/e2b-infra/bin/fc-launch /opt/e2b-infra/bin/fc-netns-exec
```

若**缺失** = 当前部署的二进制早于 patch 0002：先按「第 5 节·场景二」重建并刷新二进制
（`rpmbuild -bb e2b-infra.spec …` → `rpm -Uvh --force` → `cp -f /opt/e2b-infra/bin/orchestrator /usr/bin/orchestrator`），
再切档。`disabled` 不依赖助手，任何版本都能用。

### 9.3 怎么生效

**只改 `E2B_FC_LAUNCH_MODE` 是 nomad job env 改动，不是 Go 源码改动**，对应「第 5 节·场景三」，
**不用 `rpmbuild`，也不用 `-i` / `-s`**。它只影响 template-manager 这一个 job，所以用第 11 节的
快速重跑最省事——只 render + 重跑该 job，跳过整套 `build.sh -f` 的镜像构建：

```bash
# 让改动落到 /opt/e2b-infra/nomad/template-manager.hcl，二选一：
#   ① 从仓库同步这一个文件（推荐，不动 .env 的 token）：
cp -f e2b-deploy/dep/template-manager.hcl /opt/e2b-infra/nomad/template-manager.hcl
#   ② 或直接就地编辑 /opt/e2b-infra/nomad/template-manager.hcl 改那一行

cd /opt/e2b-infra && source .env         # 确认 NOMAD_ACL_TOKEN 是真 token（非占位符）
bash build.sh -r template-manager        # 只 render+重跑该 job（见第 11 节）；镜像/二进制没变时用它
#   （首次部署、或 patch 0002 的助手二进制刚更新时，改用全套 bash build.sh -f）
```

> ⚠️ 别为切个档去跑 `build.sh -i`——它会把 `.env` 的 `NOMAD_ACL_TOKEN` 重置成占位符（见 6.1）。
> 集群已在跑时，用上面 ① 手动 `cp` 那一个 hcl + `build.sh -r template-manager` 最稳
> （前提：`/opt/e2b-infra/deploy.sh` 已是带 `--only` 的新版，见第 11 节的「前提」）。
> 直接就地编辑 `/opt/e2b-infra/nomad/…` 是临时的，下次 `build.sh -i` 或 `rpm -Uvh` 会覆盖——
> 要持久必须改仓库 `e2b-deploy/dep/template-manager.hcl`（本仓库已改）。

### 9.4 验证 & 回退

> ⚠️ 别用 `pgrep -f orchestrator` 找进程——本部署里 orchestrator 逻辑跑在 `template-manager.hcl`
> 起的进程里，命令是 `/usr/bin/template-manager --port ...`，cmdline 里**没有 "orchestrator" 字样**，
> `pgrep -f orchestrator` 匹配不到会返回空，命令退化成 `/proc//environ` 报 `No such file`。
> deploy.sh 的 JOBS 里也没有单独的 orchestrator job。要按 `template-manager` 找。

```bash
# 1) 文件层：源模板 + 渲染结果都带上了
grep E2B_FC_LAUNCH_MODE /opt/e2b-infra/nomad/template-manager.hcl      # render 源头（持久）
grep E2B_FC_LAUNCH_MODE /opt/e2b-infra/rendered/template-manager.hcl   # 本次渲染产物

# 2) 进程层：真正在跑的进程 env 里生效了（父 bash 和子进程都继承 nomad 的 env）
for pid in $(pgrep -f /usr/bin/template-manager); do
  echo "== pid $pid: $(tr '\0' ' ' </proc/$pid/cmdline)"
  tr '\0' '\n' < /proc/$pid/environ | grep -E 'E2B_FC_LAUNCH_MODE|ORCHESTRATOR_SERVICES'
done

# 3) nomad 层（更权威）：提交的 job spec 带上了，且 alloc 是本次重跑之后重启的
cd /opt/e2b-infra && source .env
nomad job inspect -token "$NOMAD_ACL_TOKEN" template-manager-system | grep E2B_FC_LAUNCH_MODE
nomad job status  -token "$NOMAD_ACL_TOKEN" template-manager-system   # 看 Version 递增、Status=running
ps -o pid,lstart,cmd -p $(pgrep -f /usr/bin/template-manager | head -1) # lstart 应是你本次重跑之后的时间

# 4) 再跑 benchmark 对比 configured fc cost（benchmark/README.md）
```

判定：进程 env 出现 `E2B_FC_LAUNCH_MODE=launch`、`nomad job inspect` 里有该键、且进程 `lstart`
是本次重跑（`build.sh -r` 或 `-f`）之后的时间（说明 alloc 已按新 env 重启），即为生效。

> env 改动**只对新起的进程生效**。若 `job status` 的 Version 没变 / 进程 `lstart` 还是老时间，
> 说明 job 没重启：确认 `nomad/template-manager.hcl`（而非只改 `rendered/`）已带上该行，再 `build.sh -r template-manager`。

回退：把值改回 `disabled`（或删掉这行）→ `build.sh -r template-manager`。

---

## 10. 准入并发上限（`MAX_STARTING_INSTANCES_PER_NODE`）

orchestrator 用一个 `startingSandboxes` 信号量给「同时正在启动的沙箱数」封顶，超过上限的请求在
「准入排队」阶段等待（`0001-adapted-for-arm-architecture.patch` 的 `server/sandboxes.go`，常量
`maxStartingInstancesPerNode`；配套 `acquireTimeout` 已放宽到 300s）。上限越低、并发越高，排队越久
（详见 `benchmark/高并发瓶颈定位方案.md`）。

`main.go` 的 `server.New()` 用 `env.GetEnvAsInt("MAX_STARTING_INSTANCES_PER_NODE", 500)` 读环境变量，
**读不到就回落到编译默认 500**。这是个免重编的运行时开关，适合扫不同上限看耗时。

### 10.1 在哪配

和 `E2B_FC_LAUNCH_MODE`（第 9 节）一样：真正跑沙箱、执行 `ResumeSandbox` 的 orchestrator 逻辑在
**`template-manager.hcl`** 这个 job 里（`ORCHESTRATOR_SERVICES = "orchestrator,template-manager"`，
一个进程同时跑 orchestrator + template-manager 两个 service），所以开关加在它的 `env {}` 块。

- 仓库源头（持久，扛得住 `build.sh -i` / `rpm -Uvh` 覆盖）：`e2b-deploy/dep/template-manager.hcl`
- 部署机上被 `deploy.sh` 渲染的模板：`/opt/e2b-infra/nomad/template-manager.hcl`

`env {}` 里的这一行（**用字面数字，别用 `${...}`**——`deploy.sh` 的 `envsubst` 白名单里没有它，
`${MAX_STARTING_INSTANCES_PER_NODE}` 会被清空成空串，反而回落到 500）：

```hcl
      env {
        ...
        ORCHESTRATOR_SERVICES         = "orchestrator,template-manager"
        MAX_STARTING_INSTANCES_PER_NODE = "500"   # 500=当前默认；改这一个数字即可扫不同上限
        ...
      }
```

> 本仓库把默认值显式写成 `500`（= 原编译默认，行为不变），只是把旋钮摆到明面上、方便改。

### 10.2 怎么生效

这是 nomad job env 改动、不是 Go 源码改动（对应第 5 节·场景三）：改完让它落到
`/opt/e2b-infra/nomad/template-manager.hcl`，再重跑 template-manager 这一个 job 即可，
**不用 `rpmbuild`，也不用 `-i` / `-s`**。为省掉整套 `build.sh -f` 的镜像构建，用第 11 节的快速重跑：

```bash
# ① 从仓库同步这一个文件（推荐，不动 .env 的 token）：
cp -f e2b-deploy/dep/template-manager.hcl /opt/e2b-infra/nomad/template-manager.hcl
#    或就地编辑 /opt/e2b-infra/nomad/template-manager.hcl 改那一个数字

cd /opt/e2b-infra && source .env
bash build.sh -r template-manager     # 只 render + 重跑该 job（见第 11 节）；也可用 build.sh -f 走全套
```

### 10.3 验证 & benchmark 提醒

```bash
# 文件层 + nomad 层 + 进程层（同 9.4，键名换成 MAX_STARTING_INSTANCES_PER_NODE）
grep MAX_STARTING_INSTANCES_PER_NODE /opt/e2b-infra/nomad/template-manager.hcl
cd /opt/e2b-infra && source .env
nomad job inspect -token "$NOMAD_ACL_TOKEN" template-manager-system | grep MAX_STARTING_INSTANCES_PER_NODE
for pid in $(pgrep -f /usr/bin/template-manager); do
  tr '\0' '\n' < /proc/$pid/environ | grep MAX_STARTING_INSTANCES_PER_NODE
done
```

> **值的含义**：上限=500 时，并发 ≤100 基本不排队（「准入排队」≈0），扫 100/500 之间看不出差别。
> 要观察排队效应，要么把上限调小（如 15/30/100），要么把 benchmark 的 `--concurrency` 提到超过上限。
> 与 `benchmark/高并发瓶颈定位方案.md` 的「cap 扫描」一致：固定并发扫 15/30/100/500，
> 看「准入排队」与「等 FC API socket」此消彼长。

---

## 11. 快速重跑单个 job（`build.sh -r` / `deploy.sh --only`，免整套 `-f`）

只改了某个 job 的 env（如 `E2B_FC_LAUNCH_MODE`、`MAX_STARTING_INSTANCES_PER_NODE`）时，
`build.sh -f`（= `deploy.sh`）其实做了很多用不上的活：**重新 `docker build` 并 push `bin/*.Dockerfile`
的所有镜像、拉取/推 redis、重跑 redis/edge/api、跑 seed-db、改 tier 配额**。而 env 改动只需要：
重新 render 这一个 hcl + `nomad job run` 重跑它。扫参数要跑很多轮时，这个差别很明显。

为此给 `deploy.sh` 加了 `--only <job>`，并在 `build.sh` 暴露为 `-r / --redeploy <job>`：

```bash
cd /opt/e2b-infra && source .env
bash build.sh -r template-manager        # 等价于 bash deploy.sh --only template-manager
```

`--only <job>` 相比全量 `-f` 的区别：

| 步骤 | `build.sh -f` | `build.sh -r <job>` |
|------|:---:|:---:|
| render `nomad/*.hcl` | ✅ | ✅ |
| `docker build` + push 镜像 | ✅ | ❌ 跳过 |
| 拉取/推 redis 等 | ✅ | ❌ 跳过 |
| `nomad job run` | redis/template-manager/edge/api 全跑 | 只跑 `<job>` |
| seed-db / 改 tier 配额 | ✅ | ❌ 跳过 |

`<job>` 用 `nomad/` 下的 hcl 文件名（不带 `.hcl`）：`template-manager`、`api`、`edge`、`redis`。
重跑会给该 job 提交新版本、按新 env 重启 alloc（env 改动只对新起的进程生效，验证见 9.4 / 10.3）。

**适用 / 不适用**：
- ✅ 适用：只改了 job 的 **env / 部署配置**（本文档两个开关，或其它 env）。镜像与二进制都没变。
- ❌ 不适用：改了 **Go 源码 / 二进制**——那要走第 5 节·场景二（`rpmbuild` → `rpm -Uvh` →
  `cp bin/orchestrator /usr/bin/{orchestrator,template-manager}`）；`-r` 既不重建镜像也不刷二进制。
- ⚠️ 首次部署、或镜像/二进制有更新时，先跑一次全量 `build.sh -f`（或场景二）把镜像推上去，
  之后的 env 微调再用 `-r` 快速迭代。

> 前提：`/opt/e2b-infra/deploy.sh` 与 `build.sh` 得是带 `--only` / `-r` 的新版本。现有部署先同步一次：
> `cp -f e2b-deploy/dep/deploy.sh /opt/e2b-infra/deploy.sh`（`build.sh` 在 e2b-deploy 目录下直接用即可，
> 或重装 RPM）。直接改 `/opt/e2b-infra/…` 是临时的，要持久得改仓库 `e2b-deploy/dep/` 源文件（见第 5 节 ⚠️ 提示）。

---

## 12. 压测（benchmark）：凭据同步、运行流程与组合扫描

`benchmark/` 下跑压测要用两类凭据：建沙箱的 E2B 客户端配置，和采集 orchestrator 日志的
Nomad ACL token。它们都通过 `benchmark/.env` 提供，值用 `sync-env.sh` 从磁盘上的“真相源”**同步一次**，
之后整套流程（压测 → 采集 → 分析 → 出图）裸跑即可：

```bash
cd benchmark
bash sync-env.sh          # 首次会从 .env.example 生成 .env，并填入下面 3 个易变的值
# 首次必做：E2B_API_URL 默认是占位符，改成本机 API 地址（远程跑客户端则填 http://<SERVER_IP>:3000）
sed -i 's|^E2B_API_URL=.*|E2B_API_URL="http://127.0.0.1:3000"|' .env
python run_benchmark.py --template base --count 100 --concurrency 100 --warmup 3
bash collect_logs.sh      # token 只认 .env 里的 NOMAD_TOKEN，手动 export 的会被忽略
python parse_report.py    # 默认分析最近一次运行；指定某次用 --run-dir runs/run_<时间戳>
python visualize_intervals.py   # 可选：出 3 张图（timeline / total_gantt / stage_durations），需 matplotlib
```

- `.env` 已被 gitignore，**不入库**；入库的只是模板 `.env.example`，真实 token 只留在本机。
- `sync-env.sh` 只刷新下面 3 个易变值，其它行（`E2B_DOMAIN` / `E2B_API_URL` / `E2B_HTTP_SSL` 等）原样保留：

  | 变量 | 真相源（磁盘） | 用途 |
  |---|---|---|
  | `E2B_ACCESS_TOKEN` | `/root/.e2b/config.json` → `.accessToken` | E2B SDK 建沙箱 |
  | `E2B_API_KEY` | `/root/.e2b/config.json` → `.teamApiKey` | E2B SDK 建沙箱 |
  | `NOMAD_TOKEN` | `${NOMAD_DATA_DIR:-/data/nomad}/acl.token` | 采集 orchestrator 日志 |

- **首次生成的 `.env` 里 `E2B_API_URL` 是占位符 `http://<server_ip>:3000`，必须手工改成本机 IP**
  （本机跑压测用 `http://127.0.0.1:3000`；不改的话 SDK 第一步就报 `Name or service not known`）。
  `sync-env.sh` 永不动这行，改一次即可。`E2B_DOMAIN="e2b.app"` 保持默认——`build.sh -i` 装的
  dnsmasq 已把 `*.e2b.app` 解析到本机，这个域名是连沙箱用的。
- `collect_logs.sh` 的 token **只有一个来源：`.env` 里的 `NOMAD_TOKEN`**——shell 里已 export 的
  `NOMAD_TOKEN`/`NOMAD_ACL_TOKEN` 会被显式忽略（unset），`acl.token` 也不再直接读。
  好处是行为可预期：403 时只需检查 `.env` 是否过期。重新 bootstrap 过 ACL（如 `build.sh -u`
  卸载后再 `-s`）后先重跑 `bash sync-env.sh`，否则 `.env` 里的旧 token 会一直 403。
- 路径可用环境变量覆盖：`E2B_CONFIG_JSON=... NOMAD_DATA_DIR=... bash sync-env.sh`。
  `acl.token` 是 `chmod 600` 归 root，非 root 用户读它要 `sudo`。E2B token 取不到时先用 e2b CLI 登录。

### 12.1 组合扫描示例：3 种 `E2B_FC_LAUNCH_MODE` × 2 种 cap = 6 组

目标：对照测 `E2B_FC_LAUNCH_MODE`（disabled / netns-exec / launch，背景见第 9 节）×
`MAX_STARTING_INSTANCES_PER_NODE`（30 / 100，背景见第 10 节）在 100 并发下的启动耗时差异。
**每一组 = 「第 5 节·场景三」的一次 1→3 循环 + 一轮压测**，没有任何新机制；六组之间
不碰 rpm / 二进制（若中途重装过 rpm，先按 5.0 重放 overlay 再继续）。

**组合顺序**（蛇形：每步只改一个变量 ⇒ 相邻两组 spec 必有差异 ⇒ alloc 必被替换）：

| 轮次 | mode | cap |
|:--:|---|:--:|
| ① | launch | 30 |
| ② | launch | 100 |
| ③ | netns-exec | 100 |
| ④ | netns-exec | 30 |
| ⑤ | disabled | 30 |
| ⑥ | disabled | 100 |

**开跑前一次性确认**（都过了才开始）：

```bash
cd /opt/e2b-infra
# 场景三第 0 步快检：overlay 没被 rpm 打回上游版（任一不过 ⇒ 先按 5.0 重放）
grep -E 'ORCHESTRATOR_SERVICES|E2B_FC_LAUNCH_MODE|MAX_STARTING' nomad/template-manager.hcl
grep -c -- '--only' deploy.sh
ls -l bin/fc-launch bin/fc-netns-exec        # 档 2/3 的助手二进制在位（见 9.2）

BENCH_DIR=/home/j30059180/projects/mye2b/e2b-oe/benchmark   # ← 换成你的 benchmark 目录
cd "$BENCH_DIR" && bash sync-env.sh          # 凭据同步（见上文）
```

**每一组跑这一段**（只有 MODE/CAP 两个变量按上表换值，其余六组照抄）：

```bash
MODE=launch; CAP=30                          # ← 按组合表换值
BENCH_DIR=/home/j30059180/projects/mye2b/e2b-oe/benchmark

# 1) 改值 + 生效（场景三第 1、2 步；env 变了 nomad 自动替换 alloc，不需要 stop）
cd /opt/e2b-infra
sed -i -E "s/^(\s*E2B_FC_LAUNCH_MODE\s*=\s*).*/\1\"$MODE\"/"              nomad/template-manager.hcl
sed -i -E "s/^(\s*MAX_STARTING_INSTANCES_PER_NODE\s*=\s*).*/\1\"$CAP\"/" nomad/template-manager.hcl
bash build.sh -r template-manager

# 2) 验证（场景三第 3 步的第④层——进程真实 env 是最终裁决；两个值都要对上）
pid=$(pgrep -f /usr/bin/template-manager | head -1)
tr '\0' '\n' < /proc/$pid/environ | grep -E 'E2B_FC_LAUNCH_MODE|MAX_STARTING_INSTANCES_PER_NODE'

# 3) 压测 + 采集 + 报告（模板/count/并发六组必须一致，否则没有可比性）
cd "$BENCH_DIR"
python run_benchmark.py --template base --count 100 --concurrency 100 --warmup 3 --fc-launch-mode "$MODE"
echo "mode=$MODE cap=$CAP" > "runs/$(cat runs/.latest)/combo.txt"   # cap 标注（mode 已由 --fc-launch-mode 写进 meta.json）
bash collect_logs.sh
python parse_report.py
```

**看什么 / 怎么对比**：

- **排队 vs FC 启动的此消彼长**：cap=100 时「准入排队」应≈0——若「等待firecracker启动」「恢复虚拟机」
  反而变大，说明信号量原本在保护已饱和的 CPU，真瓶颈在 FC 启动侧（这正是 cap 扫描要回答的问题）。
- **各 mode 的差异**主要看「└拉起FC进程」（fc spawn）一段；档位含义见第 9 节。
- **事后对比某一轮**：`python parse_report.py --run-dir runs/run_<时间戳>`；每轮的 `combo.txt` +
  `meta.json` 里的 `fc_launch_mode` 能对上号。
- **扫完定档**：把胜出组合写回仓库 `e2b-deploy/dep/template-manager.hcl` 作为长期默认
  （场景三末尾的持久化提醒——`/opt` 下的改动会被下次 `rpm -Uvh` 覆盖）。
