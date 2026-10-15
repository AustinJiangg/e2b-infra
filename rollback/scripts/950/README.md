# `rollback/scripts/950/` —— 950 上的一站式测试入口

这台机器：鲲鹏 950（有 HDBSS）+ openEuler，e2b-infra 由 RPM 部署到 `/opt/e2b-infra`。
本目录只有一个入口脚本 `run.sh`，把散在 `acceptance/`、`crtest/`、`crtest/bench/`
里的脚本按「冒烟 / 功能 / 性能 / HDBSS 专项 / 长测」五档串起来，每次跑完写一份 `SUMMARY.md`。

脚本本身不区分机器 —— 同一份 `run.sh` 在 920B 上也跑得动（`--env-file` 与 `--python`
指过去就行），950 与 920B 的差别只有一处，见 [§7](#7-950-与-920b-的差别)。

---

## 1. 部署完先跑这三条

### 1.0 从 clone 到能跑沙箱

950 上的实际流程是下面这一串；每一步的细节与排障在仓库根的 `single-node-offline-deploy.md`（§2 构建安装、§4 启动）。
这一串命令 2026-10-10 在一台已部署过的鲲鹏 920B 上照着走过一遍，注释里的耗时是那次实测的。

**先备齐不在仓库和 RPM 里的东西**（部署指南 §0、§1）。RPM 装到 `/opt/e2b-infra/dep/` 的只有部署脚本、配置和
`ubuntu-22.04-custom.tar.gz`；下面这些要另外拷进去或事先装好，`build.sh` 按这些名字找：

| 要有什么 | 谁检查、缺了会怎样 |
|---|---|
| Docker 本身（部署指南 §0.1） | `build.sh` 不装 Docker |
| docker 镜像 `postgres:latest`、`redis:7.4.4-alpine`、`debian:bookworm-slim` 已经 `docker load`（§1.2） | `-i` 检查 `postgres:latest`，没有就报 `docker 镜像 postgres:latest 不存在，请先 docker load 离线镜像` 并退出；另外两个在 `-s` 的 `deploy.sh` 里用到 |
| `/opt/e2b-infra/dep/minio`（§1.1） | `-i` 报 `MinIO 依赖文件缺失，请检查 /opt/e2b-infra/dep 目录` 并退出 |
| `/opt/e2b-infra/dep/harbor-offline-installer-aarch64-v2.13.0.tgz`（§1.1） | `-i` 报 `Harbor 安装包不存在：…` 并退出 |
| `/opt/e2b-infra/dep/consul_1.21.4_linux_arm64.zip`、`/opt/e2b-infra/dep/nomad_1.11.2_linux_arm64.zip`（§1.1） | `-i` 只是把它们拷到 `/tmp`，缺了不报错；到 `-s` 装 consul / nomad 时找不到本地包会转去下载，离线机器上在那里失败 |
| nbd 内核模块、`net.ipv4.ip_forward=1`（§0.2、§0.3） | 见部署指南 |

这些文件 `rpm -Uvh` 不会删，所以重新部署同一台机器时不用再拷；第一次装要在第 ③ 步装完 RPM（`dep/` 目录这时才有）之后、第 ④ 步之前放进去。

```bash
# ① 取代码与大源码包。源码包是 LFS 对象，放在 .lfsconfig 指的那台内部 LFS 服务器上，950 上能直接拉
git clone https://github.com/AustinJiangg/e2b-infra.git
cd e2b-infra
git lfs pull
ls -l e2b-infra-2026.09.tar.gz        # 应为 143753275 字节；134 字节说明还是 LFS 指针
echo "59f05d43eb5d6e44759ab0e51f35d99d359698f9aeb97c61030f7f3ea8b95ea9  e2b-infra-2026.09.tar.gz" | sha256sum -c   # 必须输出 OK

# ② 把本机 IP 写进部署配置，重建打进 RPM 的部署包
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
echo "本机 IP: ${IP:-（没取到，看下面的说明，先别往下走）}"
[ -n "$IP" ] && sed -i "s/^SERVER_IP=.*/SERVER_IP=$IP/" e2b-deploy/dep/.env
grep '^SERVER_IP' e2b-deploy/dep/.env       # 人工核对：必须是对外提供服务的那个地址
REPO=$PWD
rm -rf /tmp/e2b-deploy-repack && mkdir -p /tmp/e2b-deploy-repack && cd /tmp/e2b-deploy-repack
tar -xzf "$REPO/e2b-deploy.tar.gz"
rsync -a --exclude .git "$REPO/e2b-deploy/" e2b-deploy/
tar -czf "$REPO/e2b-deploy.tar.gz" e2b-deploy
cd "$REPO"

# ③ 出 RPM，装到 /opt/e2b-infra（920B 上 rpmbuild 135 秒，安装 7 秒）
rpmbuild -bb e2b-infra.spec --define "_sourcedir $PWD"
rpm -ivh ~/rpmbuild/RPMS/aarch64/e2b-infra-2026.09-3.aarch64.rpm     # 已装过同版本或旧版时换成 rpm -Uvh --force

# ④ 停 → 装 → 起（920B 上：-d 约 2 分钟，-i 约 20 秒，-s 约 2 分钟）
#    -i 要在准备用来跑 SDK 的那个 Python 环境里执行；已有部署的机器先读下面「重新部署会丢什么」
cd /opt/e2b-infra
bash build.sh -d
bash build.sh -i
bash build.sh -s

# ⑤ 确认模板能建、沙箱能跑（回到 clone 下来的仓库里；920B 上建模板有缓存时 8 秒，sandbox.py 3 秒）
cd "$REPO/benchmark"
bash sync-env.sh
python3 build_template.py
python3 sandbox.py
```

**① 拉不到源码包的机器**（访问不了那台 LFS 服务器）：从已有这个文件的地方把 `e2b-infra-2026.09.tar.gz` 拷到仓库根目录，
再执行上面那条 `sha256sum -c`，输出 `OK` 才能往下走（同部署指南 §2 第 1b 步）。不能拿上游的 GitHub 标签归档顶替，那个归档不带 `vendor/`。

**② 为什么不用 `hostname -I` 的第一个地址**：机器上有残留的沙箱网卡时，它排在最前面的可能是沙箱那一段的地址
（920B 上实测回的是 `10.12.17.54`，真正的地址排在第 28 个）；这种地址确实配在本机网卡上，`build.sh` 里
"`SERVER_IP` 是不是本机地址"的检查（`check_host_ip`）拦不住它。上面取的是默认路由的源地址，只查路由表、不发包，
`1.1.1.1` 不需要真能通，有默认路由就行。**没有默认路由时这条命令没有输出**（`ip` 报 `Network is unreachable`），
`IP` 为空，`sed` 那一行不会执行。这时改用下面两条：先看网卡，再取第一个不是沙箱 / 容器网卡的全局地址，然后同样用 `grep` 核对：

```bash
ip -4 -br addr | grep -vE '^(lo|veth|docker|br-|cni|flannel|virbr)'
IP=$(ip -4 -o addr show scope global | awk '$2 !~ /^(veth|docker|br-|cni|flannel|virbr)/ {sub("/.*","",$4); print $4; exit}')
[ -n "$IP" ] && sed -i "s/^SERVER_IP=.*/SERVER_IP=$IP/" e2b-deploy/dep/.env
grep '^SERVER_IP' e2b-deploy/dep/.env
```

机器有多块业务网卡时，第二条取到的不一定是你要的那个；以 `grep` 打出来的值为准，不对就用编辑器改 `e2b-deploy/dep/.env` 的 `SERVER_IP=` 那一行，再接着做重打包。

**④ 没有 HDBSS 的机器（例如 920B）**要在 `-i` 之后、`-s` 之前给 orchestrator 加上 `FC_TRACK_DIRTY_PAGES`，
否则脏页跟踪默认关、每次 checkpoint 都是全量；做法是部署指南 §4.1 的那条 `sed`（`-i` 每次都会把模板盖回不带这一行的版本，所以每次重新部署都要再加）。950 不需要。

**⑤ 全新机器建第一个模板之前**还要做部署指南 §1.3 的两件事（让 `harbor` 这个主机名能解析、把 `ubuntu:22.04-custom` 推进 harbor），`build.sh` 不做这两件事。

#### 重新部署会丢什么

在已经部署过的机器上再走一遍 ④（`-d` → `-i` → `-s`），下面这些**每次都会丢**：

- 全部沙箱与 checkpoint（`-d`）。
- **postgres 整库**：`-i` 把 `postgres` 容器删掉重建，数据卷不保留。团队、API key、access token、模板与构建记录都没了；`-s` 会重新 seed 出一个新团队并重写 `/root/.e2b/config.json`。
- nomad 与 consul 的全部状态和 ACL token（`-d` 删 `/data/nomad`、`/data/consul`、`/etc/nomad.d`、`/etc/consul.d`、`/opt/consul/acl.token`），连同 nomad 的 alloc 日志；`-s` 重新 bootstrap，新 token 写回 `/opt/e2b-infra/.env`。
- nginx / harbor 用的自签证书（`-i` 重新生成）。

不会丢：harbor 里的镜像（宿主机 `/data` 下）、minio 的数据（`/root/data/minio`）、`/orchestrator`、`/tmp/templates` 下的模板文件、大页池。

所以每次重新部署之后**必须**重做第 ⑤ 步：`sync-env.sh` 把新的 API key 同步进 `benchmark/.env`，`build_template.py` 重建模板 `base`。旧的 API key 和旧模板 id 都不能再用。

`build.sh` 三个开关各做什么（`/opt/e2b-infra/build.sh`，仓库里是 `e2b-deploy/build.sh`）：

| 开关 | 做什么 |
|---|---|
| `-d`（`--stop`） | 先让 `template-manager-system` 优雅退出，再停 consul / nomad，结束 redis、client-proxy、api、orchestrator 与残留的 firecracker 进程，清掉沙箱留下的 netns / veth / iptables 规则，检查端口是否放干净。全新机器上它只是空跑一遍 |
| `-i`（`--install`） | 装 postgres / minio / harbor / nginx；用**当前环境的 pip** 装 `e2b==2.20.0`、`e2b_code_interpreter==2.4.1`、`python-dotenv`，再铺 checkpoint 的 SDK 覆盖层（先 `install.py` 后 `patch_e2b.py`）；把 `dep/` 里的部署脚本、`.env`、`template-manager.hcl` 盖到 `/opt/e2b-infra`。其中 `postgres` 容器是删掉重建的，库里的数据不保留 |
| `-s`（`--start`） | 起 harbor、nomad 服务端与客户端并初始化（首次会 bootstrap ACL），登录 harbor 建项目，最后跑 `deploy.sh` 提交各个 nomad job。**会写内核参数与大页数**：`sysctl -w` 设 `net.core.somaxconn` 等几项并往 `/etc/sysctl.conf` 追加、把 `net.ipv4.ip_forward` 设成 1、按内存重算并写 `nr_hugepages` / `nr_overcommit_hugepages`（`start-client.sh`、`init-client.sh`）；还会把 RPM 里的 orchestrator 拷到 `/usr/bin/orchestrator` 与 `/usr/bin/template-manager` |

第 ⑤ 步：`sync-env.sh` 生成 `benchmark/.env`（客户端凭据）；`build_template.py` 建别名 `base` 的模板，规格 **2 vCPU / 2048 MB** 就写在这个文件里（`cpu_count=2`、`memory_mb=2048`），本目录的测试一律用它；`sandbox.py` 起一个沙箱跑几条命令再关掉。
**⑤ 这三条要在 `build.sh -i` 装 SDK 的那个环境里执行**（`-i` 在哪个 Python 环境里跑，SDK 与覆盖层就装在哪；装在虚拟环境或 conda 环境里的，先激活它，让 `python3` 指向它）。
这两条都过了，再进下面的 `run.sh`，解释器同样用这一个（见下）。

### 1.1 三条命令

**测试套件不在 RPM 里**（`e2b-infra.spec` 只把部署件装到 `/opt/e2b-infra`），它跟着仓库走：
出 RPM 时 clone 下来的那个 e2b-infra 目录里就有 `rollback/scripts/`，直接进去用，不需要再拷、不需要装任何东西。

然后三条命令：

```bash
cd rollback/scripts/950            # 在 clone 下来的 e2b-infra 目录里
bash run.sh smoke                          # ≤ 3 分钟：这台机器的 checkpoint 能不能用
bash run.sh func                           # ≤ 40 分钟：功能与健壮性
bash run.sh perf                           # ≤ 40 分钟：性能分档、长尾、并发
```

三条都不需要参数，默认值就是对的：

- **`.env`**：优先取仓库里的 `benchmark/.env`（由 `benchmark/sync-env.sh` 生成，带
  `E2B_API_KEY` / `E2B_ACCESS_TOKEN`），找不到才退到 `/opt/e2b-infra/.env`。部署根的
  `.env` 是**服务端**配置，里面通常**没有客户端凭据** —— 拿它跑会在开头看到
  `!! E2B_API_KEY 没有值`，随后建沙箱失败。所以正常流程是先生成一份：
  `cd ../../../benchmark && bash sync-env.sh`（它从 `/root/.e2b/config.json` 和
  `/data/nomad/acl.token` 取值）。要用别处的凭据就 `--env-file` 指过去。
- **解释器**：默认 `python3`，但**要用装了 SDK 覆盖层的那一个**。`build.sh -i` 在哪个
  环境里跑，`e2b==2.20.0` / `e2b_code_interpreter==2.4.1` / `python-dotenv` 和
  `dep/e2b-sdk-checkpoint/install.py` 的覆盖层就装在哪里。跑测试时在同一个环境里用
  `--python "$(command -v python3)"` 把那个解释器传给 `run.sh`；`build.sh -i` 若是在某个
  虚拟环境里跑的，就先进入那个环境再执行这条命令。别的解释器里不一定有覆盖层。
- 结果落在 `950/results/<时间戳>-<档>/`。

只要这三条的 `SUMMARY.md` 里 `FAIL 0`，这台机器的 checkpoint / restore 就是好的。
`run.sh` 的退出码 = FAIL 项数，所以 CI 里直接 `bash run.sh smoke && bash run.sh func` 即可。

### 跑之前要确认的五件事

以下命令都在本目录（`rollback/scripts/950/`）里跑。

| 事 | 怎么确认 | 不满足会怎样 |
|---|---|---|
| 客户端凭据拿得到 | `grep -c E2B_API_KEY ../../../benchmark/.env` 回 1（没有就 `cd ../../../benchmark && bash sync-env.sh` 生成） | `run.sh` 开头会打 `!! E2B_API_KEY 没有值`，随后建沙箱失败 |
| 要用的解释器装了带 checkpoint 覆盖层的 e2b | 在 `build.sh -i` 装 SDK 的那个环境里 `python3 /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check`，同一个解释器再用 `--python "$(command -v python3)"` 传给 `run.sh` | smoke 第 2 项就会 FAIL |
| 模板 `base` 已经建好 | `nomad job status` 里 template-manager 在跑，且建过一次模板 | 每个用例开头建沙箱就失败 |
| 模板 `base` 的规格是 **2 vCPU / 2048 MB**（磁盘约 940 MB） | 手册 19 篇 §8 那条核对命令：`GET /templates` 回的 `cpuCount` / `memoryMB` / `diskSizeMB` 应为 `2` / `2048` / `940` | 结果仍然有效，但**性能数字不能和手册第五部分（验证与实测）对比**（规格是条件标签的一部分，见手册 19 篇 §8 与 20 篇 §2） |
| 在**宿主机上**跑，且是 root | `id -u` 回 0 | 读不到服务端分段计时与产物目录，T32 会判失败、性能表会缺列 |

`benchmark/.env` 由 `benchmark/sync-env.sh` 生成：它从 `/root/.e2b/config.json`
（`build.sh -s` 的 seed 步骤写的团队凭据）和 `/data/nomad/acl.token` 取值，
`E2B_API_URL` 要指向本机 api 的 REST 端口：

```bash
cd ../../../benchmark
bash sync-env.sh
grep -E '^E2B_(API_KEY|ACCESS_TOKEN|API_URL)=' .env      # 三行都要有值
cd ../rollback/scripts/950
bash run.sh smoke --python "$(command -v python3)"   # 在 build.sh -i 装 SDK 的那个环境里执行
```

---

## 2. 五档各是什么

| 档 | 目标耗时 | 跑什么 | 判据 |
|---|---|---|---|
| `smoke` | ≤ 3 分钟 | 宿主预检、SDK 覆盖层自检、`checkpoint capabilities`（能力文件或日志行）、firecracker sha256、59 项功能正确性 | 5 项全 PASS |
| `func` | ≤ 40 分钟 | crtest 里 16 个不需要重启服务端的用例 + SDK 异常语义 pytest | 17 项里 PASS + SKIP = 17，FAIL = 0 |
| `perf` | ≤ 40 分钟 | 分档基准（短表）+ 达标判定、单沙箱串行 restore 长尾 n=100、并发 A/B 段 | 4 项无 FAIL，且达标表里各档 p50 在线内 |
| `hdbss` | 约 4 分钟（每个 order；扫描时每个 order 另加约 1 分钟重启） | HDBSS 证据（L1 / L2 / L3 + order 核实）、写密集基准、汇总表；`--orders` 扫 buffer 大小（见 [§2.4](#24-hdbss)） | 无 FAIL：checkpoint / restore 没失败、md5 一致、order 核实生效；性能数字不判。后端不是 `hdbss` 时整档 SKIP |
| `long` | 不自动跑 | 只打印清单与命令（见 [§6](#6-长测清单)） | — |

### 2.1 `smoke`

| 项 | 脚本 | 看什么 | PASS 判据 |
|---|---|---|---|
| 宿主预检 | `crtest/portability/preflight-customer.sh` | CPU / KVM / GIC / 大页 / 产物盘 / 二进制身份，共约 19 项。Firecracker 只校验**在用的**那个版本目录（见表下说明） | 退出码 = 0，即 `FAIL 0`（`WARN` 不算 FAIL） |
| SDK 覆盖层自检 | `/opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check` | 覆盖层 21 个文件在不在位、异常族（原 8 个子类都在且映射表全是子类）、端口 49984、四个 RPC 不重放 | 打出「自检通过（干净子进程）」，退出码 0 |
| capabilities 能力文件 / 日志行 | 先读 `DEFAULT_CACHE_DIR/checkpoint-capabilities.json`（默认 `/orchestrator/build/`；给了 `--store` 就是它的父目录），文件里的 `pid` 须是正在运行的 orchestrator / template-manager；没有这个文件（deltabox-dev `93ccb02` 之前的版本不写）或是以前的启动留下的，就退回从**正在运行**的那个进程所在 alloc 的 `start.stdout.*` 里抓最后一次启动行，再退到 journald | `track_dirty_pages` 是不是 `true`、为什么 | 读到有效的能力文件，或抓到带 `track_dirty_pages` 的日志行；都没有判 SKIP |
| firecracker sha256 | `/fc-versions/*/firecracker` 与 `/opt/e2b-infra/bin/firecracker` | 跑的到底是哪个二进制。`SUMMARY.md` 里显示在用的那一个（路径 + sha256），全部的在 `fc-sha256.log` | 至少找到一个 |
| 功能正确性 | `acceptance/checkpoint_verify.py` | 三代现场、内存/根文件系统/删除/权限位共 59 项，心跳进程 pid 证明是内存回来了 | 打出「✓ 59 项校验全部通过。」 |

看哪个文件：`SUMMARY.md` 一眼看完；要细节看 `preflight.log`、`checkpoint_verify.log`。

**`/fc-versions` 下有不止一个版本目录时**（例如以前的部署留下了 `v1.12.1`，本次装的是 `v1.13.1`）：预检只对在用的那个判 PASS / FAIL，
其余目录各列一行 `INFO … 未使用，不校验`，不计 FAIL 也不计 WARN。沙箱执行的是 `$FIRECRACKER_VERSIONS_DIR/<版本>/firecracker`，
"在用的版本"按这个顺序取第一个判得出来的，并在 `preflight.log` 的 `在用的 FC 版本` 那一行写明依据：
① 活着的 firecracker 进程的 `/proc/<pid>/exe`；② api 进程环境里的 `DEFAULT_FIRECRACKER_VERSION`；
③ 部署脚本 `/opt/e2b-infra/init-client.sh` 里的 `FIRECRACKER_VERSION`（本部署是 `1.13.1`，即目录 `v1.13.1`）；④ 目录下只有一个版本。
四条都判不出来时，退回"至少有一个目录带回滚接口就 PASS"，并用 INFO 说明无法判定在用版本。

**59 还是 57？** 打出 57 项说明这个 SDK 没有 `mem_mode` 字段，增量判据失效 ——
回去重跑 `install.py`（顺序：先 `install.py`，后 `patch_e2b.py`，不能反）。

**脏页后端是 hdbss 还是 kvm-wp？** 不在 capabilities 文件或那一行里。它们只说
「脏页跟踪开没开、为什么开」（orchestrator 只答这个）；`hdbss` / `kvm-wp` / `off`
这三个值是 **Firecracker 自己**报的（FC API `/` 的 `dirty_tracking` 字段），要起一个
沙箱才问得到，由 `checkpoint_verify.py` 开头那行「脏页后端 :」打印，在
`checkpoint_verify.log` 里。950 上应当是硬件标脏（hdbss），920B 上是软件写保护（kvm-wp）。

### 2.2 `func`

跑 crtest 里**所有不需要重启服务端、不需要改服务端 env** 的用例，按耗时从短到长排，
失败早暴露。括号里是 920B 上的历史中位耗时，950 应当同量级或更快：

`T12`(8 s) `T15`(8 s) `T22`(12 s) `T24`(12 s) `T23`(13 s) `T34`(14 s) `T32`(18 s)
`T41`(19 s) `T39`(21 s) `T26`(26 s) `T14`(50 s) `T40`(146 s) `T25`(153 s) `T36`(178 s)
`T11`(188 s) `T13`(197 s)

一句话说明见 `python3 -m crtest --list`，详细判据见 `crtest/README.md`。
再加一项 **SDK 异常语义 pytest**（`crtest/sdktests/test_checkpoint_errors.py`）：
起一个打桩 HTTP 服务端，驱动真实客户端，验十个 checkpoint 异常类（含字节配额的 CheckpointBytesLimitException）与 `.reason` 落点。
纯本地，不建沙箱、不碰栈。没装 pytest 就判 SKIP —— 950 能连外网，装上再重跑即可：

```bash
pip install "pytest>=7.4,<8" "pytest-asyncio>=0.23,<0.24"
```

版本按 py-sdk 自己的 `pyproject.toml` 钉；async 用例要的 `asyncio_mode=auto` 已经写在
`crtest/sdktests/pytest.ini` 里，不用另外配。920B 上实测 60 项全过、约 25 秒。

PASS 判据：每个用例自己三段式断言，退出码 0 = 通过、1 = 断言不过、**3 = 前置不满足（判 SKIP）**。
看哪个文件：`SUMMARY.md`；某项 FAIL 就看 `crtest-T??.log` 的最后三段（「断言失败 / 期望 /
实际 / 依据」）和同目录的 `T??.json`（`meta.failure` 是原文，`ops` 是每一步原始记录）。

### 2.3 `perf`

| 项 | 脚本 | 输出 |
|---|---|---|
| 分档基准 | `crtest/bench/bench_tiers.py --tier-set short` | 小档细分（0/4/8/16/32 MB 各 n=30）+ 中档（64/128/256 MB 各 n=20）+ 512 MB 极限档 n=10 + 纯内存 16 / 纯文件 16 / 只读 192 |
| 达标判定 | `crtest/bench/compliance.py` | 每档 p50 / p99 / 最大 + 与手册 20 篇 §1.1 达标线的对照表 |
| 串行长尾 | `crtest/bench/serial_restore.py -n 100` | 单沙箱、单调用方、同一个 checkpoint 连回 100 次的 p50 / p99 / 最大 |
| 并发 A/B | `acceptance/checkpoint_concurrent.py --stages A,B` | A = 跨沙箱扇出 N=1..16；B = 同沙箱 4 线程争用 |

**达标线**（手册 20 篇 §1.1，照抄客户那组粗略指标，未限定改动量）：
**checkpoint ≤ 200 ms、restore ≤ 100 ms**，量的都是**客户端墙钟**。
全量 checkpoint（每遍开头那一次）按同一口径**单列不判定**。

看哪个文件：`compliance.log` 是那张达标表，`analyze.log` 是完整分析（分段、拟合、
离群、缓存漂移），`serial-restore.log` 是长尾三个数，`concurrent-ab.log` 是并发表。
原始数据在 `raw-*.jsonl` / `bench-tiers.json` / `serial-restore.json` / `concurrent-ab.json`。

PASS 判据：脚本退出码 0（= 没有失败的 checkpoint/restore、没有现场不一致）。
**超达标线不判 FAIL** —— 超线是结论不是故障，看 `compliance.log` 末尾那句
「按 p50 超线的档」自己判断。

先试一下脚本跑不跑得通、不想等 40 分钟，加 `--quick`：
两个档各 n=5、长尾 n=10、并发只 A 段两个 N，10 分钟内跑完，`SUMMARY.md` 里会
标成「**--quick 短试跑**」，数字不能当结论。

### 2.4 `hdbss`

**只量 HDBSS 自己。**不测软件写保护，也不做 HDBSS 与写保护的对照；重点是每 vCPU 的 HDBSS buffer 大小
（`FC_HDBSS_ORDER`，`n` → 4 KiB × 2^n，默认 1 = 8 KiB）在写密集负载下的影响。

```bash
bash run.sh hdbss                                          # 当前 order，不改配置、不重启；约 4 分钟，基准有 14 分钟总时限
bash run.sh hdbss --orders 1,2,4,6,9 --allow-restart       # 扫 5 个 order，每个约 5 分钟，会重启 orchestrator 6 次
bash run.sh hdbss --quick                                  # 短试跑：只验证脚本跑得通，约 20 秒
```

| 项 | 脚本 | 看什么 | PASS 判据 |
|---|---|---|---|
| HDBSS 证据 | `crtest/bench/hdbss_bench.py --evidence` | L1：宿主 KVM 认不认 cap 502；L2：Firecracker 为这个沙箱报的 `dirty_tracking`；L3：顺序写 256 MB 的冷写 / 热写耗时与每页 KVM 退出数；order 核实：orchestrator 与 Firecracker 两个进程环境里的 `FC_HDBSS_ORDER`、`dmesg` 的 `Enable HDBSS success, HDBSS buffer size:`、FC 日志的 `HDBSS enabled (buffer order N)` | 后端是 `hdbss`；两个进程的环境变量一致；`dmesg` / FC 日志里抓到的 order 与期望相同；checkpoint / restore 没失败、md5 一致。后端不是 `hdbss` 时退出码 3，**整档 SKIP** |
| 写密集基准 | `crtest/bench/hdbss_bench.py` | 脏页量 16 / 64 / 256 / 512 / 1024 MB（guest 装不下的大档自动去掉并写明）× 写法 `seq`（连续）/ `rand`（随机散开）× 1 / 2 线程，每个组合 3 遍，另加一行不写任何页的基底 | checkpoint / restore 全部成功、每次 restore 后 md5 一致、order 核实生效 |
| 汇总表 | `hdbss_bench.py --summarize` | order × 脏页量 × 写法 × 线程一张表 | 生成成功 |

**每个组合量什么**（各列取 p50）：

| 列 | 含义 |
|---|---|
| 冷写 ms | 这批页自上次 checkpoint / restore 以来**第一次**被写的耗时，在 guest 里量。脏页跟踪的开销全在这里 |
| 热写 ms | 同一批页原样再写一遍。页已经是脏的，只剩写者自己的循环开销 |
| 跟踪 ns/页 | （冷写 − 热写）÷ 页数，即脏页跟踪摊到每页的代价 |
| kvm_exit/页 | 冷写期间这台虚机的 KVM 退出数 ÷ 页数。取自 debugfs `/sys/kernel/debug/kvm/<FC pid>-*/exits`，没有 debugfs 时退回 `perf stat -e kvm:kvm_exit`，都没有就是 `-` |
| checkpoint 墙钟 / 服务端 frozen ms | 增量 checkpoint 的客户端墙钟与服务端冻结窗口 |
| 差分 MB / 应有 MB | 这一代内存差分实占多少，对照「页数 × 4 KiB」。脏页量 0 那一行是基底 |
| restore 墙钟 ms | restore 回刚拍的那个 checkpoint |
| md5 一致 | 写完记 md5 → checkpoint → 把这批页改写掉 → restore → md5 必须回到原值 |

写者是 guest 里的常驻进程：每个 vCPU 一个绑核的工作进程，在基线 checkpoint 之前就把整块内存写过一遍，
所以之后每次写都没有 guest 自己的缺页，冷写与热写的差只剩脏页跟踪。两种写法每页都只写 1 个字节、
走同一个循环；1 线程只用 vCPU0，2 线程两个 vCPU 各写一半（HDBSS 的 buffer 是每个 vCPU 一个）。

**耗时**（2026-10-09 在 920B 上带 `--force-non-hdbss` 跑的流程耗时，后端是 `kvm-wp`，不是 HDBSS 的性能）：完整一轮 239 秒（证据 17 秒、基准 21 个组合 × 3 遍共 222 秒，1024 MB 档在 2 vCPU / 2048 MB 模板里放得下）；`--quick` 一轮 17 秒；`--quick --orders 1,2 --allow-restart` 共 217 秒，其中每次改 env 重启到就绪 45–53 秒。基准自带 14 分钟总时限，到点即停并标注未跑完。

**怎么读结果**：先看 `SUMMARY.md` 里「证据与 order 核实」那张表 —— 后端应为 `hdbss`，各 order 的环境变量与
`dmesg` / FC 日志对得上；HDBSS 下 L3 的 kvm_exit/页应远小于 1、冷/热接近 1。再看对比表：同一行脏页量与写法下，
比不同 order 的「跟踪 ns/页」「kvm_exit/页」「checkpoint 墙钟」。差分 MB 应约等于应有 MB 加基底。
**性能数字不判 PASS / FAIL**，PASS 只表示正确性与流程没问题。

**order 扫描的影响与恢复**。`FC_HDBSS_ORDER` 是 Firecracker 构建每台虚机时从环境变量读的，而它的环境继承自
orchestrator，所以换 order 必须改 nomad job 的 env 并重启：

- 每个 order 重启一次、扫完恢复再重启一次。**每次重启，这台机器上所有沙箱与 checkpoint 都作废**，
  所以 `--orders` 必须同时带 `--allow-restart`，不带直接报错退出。
- 机器上还有活着的 firecracker（= 还有沙箱在跑，可能是别人的）时，扫描**默认拒绝**：报出有几个、当前 orchestrator
  是哪个二进制，退出码 2，不建结果目录、不碰 job。确认这几个沙箱可以消失，才加 `--kill-sandboxes-ok`。
- 改法是取运行中的 job 定义（`nomad job inspect`，留底为结果目录下的 `job-before.json`），只在 task 的 Env 里
  加 `FC_HDBSS_ORDER`，用 `nomad job run -json` 提交；**不改 `/opt/e2b-infra` 下任何文件**。
- 每次重启后等新进程起来、`/health` 回 200、进程环境里的值对得上，再由基准在 300 秒内重试建沙箱（API 节点刚重启要一两分钟才接单）。
- 扫完、中途某一步失败、或者 Ctrl-C，都会把留底的定义原样提交回去，并在 `SUMMARY.md` 的「order 扫描与 job 恢复」
  一节写明：测完了哪些 order、启动命令与 Env 和扫描前是否完全相同、现在进程环境里的 `FC_HDBSS_ORDER`。
  恢复失败时那一节会给出手工恢复的命令。
- 每重启一次会多漏一池网络槽位（手册 29 篇 §9），扫完按那一节判断要不要清。
- 内核不接受的 order 会让 HDBSS 启用失败、Firecracker 静默退回写保护；这时基准看到后端不是 `hdbss`，那个 order 判 SKIP。

**在没有 HDBSS 的机器上**（例如 920B，后端是 `kvm-wp`）：默认整档 SKIP，不建沙箱、不重启。
要验证流程加 `--force-non-hdbss`，`SUMMARY.md` 顶部会标「非 HDBSS，仅流程验证，数字无意义」；
order 在那里不生效，核实降级为只核对两个进程的环境变量。

---

## 3. 跳过的用例及原因

`func` 默认跳过四类，SUMMARY 里会逐条写明 SKIP 与原因。都保留了显式打开的能力，
但**在交付态的 950 上都不建议跑**（前三类要改线上配置或重启，第四类要换 FC 二进制）：

| 用例 | 为什么默认跳 | 要跑怎么办 |
|---|---|---|
| `T18` orchestrator 重启后账本不过夜 | 会重启 orchestrator，**这台机器上所有沙箱都会没** | `bash run.sh func --allow-restart --restart-cmd "nomad job restart orchestrator"`（重启命令按本机部署方式填，脚本不替你猜） |
| `T21` 四条故障注入 | 要服务端带 `CHECKPOINT_FAULT_INJECT=envd_timeout`（四条注入一次只能装一条）启动，= 改线上配置并重启 | 改完 env 重启后 `bash run.sh func --fault-cases` |
| `T37` 两道配额闸 | 要服务端带 `CHECKPOINT_MIN_FREE_BYTES` / `CHECKPOINT_MAX_PER_SANDBOX` 启动 | 同上，`bash run.sh func --quota-cases` |
| `T38` FC 侧 faulted 路径 | 要 Firecracker 带 cargo feature `rollback-fault-inject` 并设 `FC_ROLLBACK_FAULT_INJECT=post_commit`；**交付的 FC 不带这个特性** | 换一份带特性的 FC 后 `--fault-cases` |

这四条的覆盖已经在 920B 上做过（见手册 21 篇 §2.3），950 上跳过不影响交付结论。

另外两类会自动判 SKIP（不是配置问题，是环境问题）：
SDK 异常语义 pytest 在没装 pytest 时 SKIP；smoke 的 SDK 自检在找不到
`install.py` 时 SKIP（`--sdk-install` 可以指到别处）。

---

## 4. 结果目录结构

```
rollback/scripts/950/results/20260922-102353-smoke/
├── SUMMARY.md              ← 要交回的就是这一份
├── run.log                 整轮的屏幕输出（tee 下来的）
├── preflight.log           每一项一份 stdout+stderr
├── sdk-check.log
├── capabilities.log
├── fc-sha256.log
└── checkpoint_verify.log

950/results/20260922-102634-func/
├── SUMMARY.md
├── run.log
├── crtest-T12.log …        每个用例一份日志
├── T12.json …              每个用例一份原始数据
└── sdk-errors.log

950/results/20260922-110000-perf/
├── SUMMARY.md
├── run.log
├── bench-tiers.log / .json / raw-*.jsonl
├── compliance.log          ← 达标表
├── analyze.log             完整分析
├── serial-restore.log / .json / serial-restore-*.jsonl
└── concurrent-ab.log / .json

950/results/20260922-120000-hdbss/
├── SUMMARY.md              ← 对比表、证据与 order 核实、job 恢复状态都在里面
├── run.log
├── hdbss-evidence.log / .json
├── hdbss-order-current.log / .json      不扫描时；扫描时是 hdbss-order1.log / .json …
├── hdbss-raw-*.jsonl                    每次迭代的原始记录
├── hdbss-table.md / hdbss-summary.json  ← 对比表（Markdown 与机器可读版）
├── hdbss-job.log / job-state.md         只在扫描时：每次提交与等待的记录、恢复后的状态
└── job-before.json / job-order*.json / job-restore.json / job-after.json
                                         只在扫描时：job 定义留底，含密钥，不要交回
```

`results/` 整个不入库（`.gitignore` 里只留 `results/.gitkeep`）：原始日志体积大，
还带着本机路径与沙箱 id。

`SUMMARY.md` 的表头固定是「项 / 结果 / 耗时 / 关键数字或原因 / 产物」，
表上方有一块环境信息（主机、内核、部署根、env 文件、解释器、模板、PASS/FAIL/SKIP 计数）。

---

## 5. 结果怎么交回

手动拷这些，不需要打包整个目录：

```bash
# 三档的汇总（体积很小，一定要带）
cp results/*/SUMMARY.md /tmp/交回/

# 有 FAIL 的项，把它那一份 .log 和 .json 一起带上
cp results/20260922-102634-func/crtest-T25.log results/20260922-102634-func/T25.json /tmp/交回/

# perf 档另外带这三份（数字都在里面）
cp results/*-perf/compliance.log results/*-perf/analyze.log results/*-perf/serial-restore.json /tmp/交回/

# hdbss 档另外带汇总的机器可读版（job-*.json 含密钥，不要带）
cp results/*-hdbss/hdbss-summary.json /tmp/交回/
```

交回时一并说明：跑的是哪个日期的 RPM、`smoke` 里 firecracker 的 sha256、
`checkpoint_verify.log` 开头那行「脏页后端」。这三样决定了这组数字能不能和别的机器对比。

---

## 6. 长测清单

`bash run.sh long` 打的就是下面这一段。这些都是小时级、要单独授权的，`run.sh` 不自动跑。

<!-- run.sh 的 long 档会 sed 出下面这一段，两行标记不要改 -->

## 长测清单

```bash
cd rollback/scripts/950            # 在 clone 下来的 e2b-infra 目录里
# 解释器：装了 SDK 覆盖层的那一个，与 run.sh 的 --python 用同一个（见 §1）。
# 这里取当前 shell 的 python3，所以要在 build.sh -i 装 SDK 的那个环境里执行本清单。
PY=$(command -v python3)
# 客户端凭据：仓库里的 benchmark/.env（同 run.sh 的默认值）。/opt/e2b-infra/.env 是服务端配置，
# 不带 E2B_API_KEY。checkpoint_concurrent.py 用不带路径的 load_dotenv()，所以还要 export 一遍。
ENVF=$(cd ../../../benchmark && pwd)/.env
set -a; . "$ENVF"; set +a
export PYTHONPATH=$PWD/../crtest   # `-m crtest` 要找得到包
OUT=/var/log/e2b-verify/long-$(date +%Y%m%d-%H%M%S); mkdir -p "$OUT"

# ① 并发 C / D 段（C 段可能把 orchestrator 打挂，机器上有别人的沙箱时不要开）
$PY ../acceptance/checkpoint_concurrent.py --stages D --soak-sandboxes 8 --soak-seconds 1800 \
    --out "$OUT/concurrent-D.json" 2>&1 | tee "$OUT/concurrent-D.log"
$PY ../acceptance/checkpoint_concurrent.py --stages C --lifecycle \
    --out "$OUT/concurrent-C.json" 2>&1 | tee "$OUT/concurrent-C.log"

# ② 空闲之后的 restore 长尾：仓库里暂时没有对应脚本，这里不列命令。
#    serial_restore.py 是背靠背连续 restore，没有"空闲 T 秒后再 restore"的参数；
#    在它外面 sleep 不等于空闲后 restore。

# ③ 数千次循环：混合稳态全规模（16 沙箱 × 1800 秒）+ 串行 restore 5000 次
$PY -m crtest T36 --env-file "$ENVF" --sandboxes 16 --seconds 1800 \
    --out "$OUT/T36-full.json" 2>&1 | tee "$OUT/T36-full.log"
$PY ../crtest/bench/serial_restore.py --env-file "$ENVF" -n 5000 --deadline-min 600 \
    --outdir "$OUT" --out "$OUT/serial-5000.json" 2>&1 | tee "$OUT/serial-5000.log"

# ④ 分档基准全表（十八档，约 10 分钟 × 想跑几遍）
$PY ../crtest/bench/bench_tiers.py --env-file "$ENVF" --tier-set full --passes 3 \
    --deadline-min 90 --outdir "$OUT" --out "$OUT/bench-full.json" 2>&1 | tee "$OUT/bench-full.log"
$PY ../crtest/bench/compliance.py "$OUT"/raw-*.jsonl 2>&1 | tee "$OUT/compliance-full.log"
```

## 长测清单结束

---

## 7. 950 与 920B 的差别

两台机器跑的是**同一份代码、同一种部署**（RPM 装到 `/opt/e2b-infra`，产物盘是根盘上的 ext4，
见仓库根的 `single-node-offline-deploy.md`），差别只有脏页跟踪的硬件后端一处：

| | 950（交付目标） | 920B |
|---|---|---|
| CPU | 鲲鹏 950 | 鲲鹏 920B |
| 脏页后端（FC 自报的 `dirty_tracking`） | `hdbss` —— CPU 自己标脏，开销接近零 | `kvm-wp` —— 内核写保护每一个干净页，第一次写陷出一次 |
| KVM cap 502 | 有 | 无 |
| `FC_TRACK_DIRTY_PAGES` | 不设也会自己打开（跟随硬件） | 必须显式设 `true`，否则默认关（陷出对不拍快照的沙箱是净亏）；设法见 `single-node-offline-deploy.md` §4.1 |

**对结果的影响**：功能与正确性结论两台机器等价（代码路径完全相同），
所以 `smoke` / `func` 在 920B 上验过即成立；**性能数字不等价** ——
920B 的 checkpoint 耗时里含 VM exit 开销，950 应当明显更快。
`perf` 的数字必须在 950 上取，920B 的只能当量级参考。
`hdbss` 档在 920B 上默认整档 SKIP（见 [§2.4](#24-hdbss)）。

在 920B 上跑本入口的命令与 950 完全相同（§1），不需要另外的参数。

---

## 8. 这些脚本从哪来

| 目录 | 来历 |
|---|---|
| `../acceptance/` | 交付态验收，单文件零共享依赖，一直在本仓库里 |
| `../crtest/` | 沙箱级补充测试套件（20 个用例），2026-09-22 从开发工作区迁入本仓库，迁入时去掉了全部 920B 硬编码 |
| `../crtest/bench/` | `bench_tiers.py` / `analyze.py` / `serial_restore.py` 由开发期的一次性采数脚本整理而来，迁入时参数化了；`compliance.py` 是 09-22 新写的达标判定；`hdbss_bench.py` 是 10-08 新写的 HDBSS 专项（三级证据的思路来自 `../dev/hdbss_evidence.py`，不依赖 `dev/lib.py`） |
| `../crtest/sdktests/` | `test_checkpoint_errors.py` 原样取自 `KASandbox_0904/py-sdk/tests/`。**随本套件带一份而不是让 950 去 clone 仓库** —— 它只依赖已安装的 `e2b` 加 pytest/httpx，没有 conftest 依赖，一个文件就能跑；950 上没有 py-sdk 源码树，clone 一个几十 MB 的仓库只为跑一个文件不划算。代价是它会随 SDK 演进而过时，改 SDK 异常语义时记得同步这一份 |
| `../crtest/portability/` `../crtest/probe950/` | 客户机器预检与「920B 结论能不能搬到 950」的探针，零外部依赖 |

手册里对应的篇目：19 篇（测试体系与功能验证）、20 篇（性能口径与方法）、
21 篇（分档基准与判定）、22 篇（长跑与并发实测）、30 篇（上机验收）。
