# 03 RPM 包构建深度解析

本篇深入讲解 `e2b-infra.spec`：这个 RPM 是怎么从"上游源码 + 两个补丁 + 一堆离线二进制"
构建出来的，构建产物如何映射到 `/opt/e2b-infra/`，以及日常改代码后怎么正确地重建、升级。
读完本篇你应该能独立完成一次"改 patch → 重建 RPM → 升级部署机"的完整循环。

## 1. 为什么用 RPM（设计动机）

上游 e2b-infra 的标准玩法是 Terraform + Packer 在 GCP 上构建集群镜像，联网拉依赖。
本仓库的目标环境是**离线/内网的单台 ARM 服务器（openEuler）**，于是选择 RPM 作为交付物：

1. **封闭构建**：Go 依赖全部 vendor 进源码包、Go 工具链自带（`tools-arm64.tar.gz`），
   构建机不需要访问 proxy.golang.org；
2. **一次交付**：业务二进制、客户机内核、firecracker、部署脚本、nomad job 模板、
   数据库迁移文件……全部打进一个包，拷一个 rpm 就能到处装；
3. **版本化升级**：`rpm -Uvh` 有明确的文件属主语义（`%files` 清单内的被替换、清单外的不动），
   这正是 runbook §5.0 "三层叠加模型"的基础；
4. **多架构**：spec 里 `%ifarch aarch64 / x86_64` 分支让同一个 spec 也能出 x86 包。

## 2. 构建输入全景

构建时 `rpmbuild -bb e2b-infra.spec --define "_sourcedir $PWD"` 把**仓库根目录**当作
SOURCES 目录，所以下面所有文件直接放在仓库根即可。

### 2.1 Source 清单

| 编号 | 文件 | 用途 |
|---|---|---|
| `Source0` | `e2b-infra-2026.09.tar.gz` | 上游源码（tag 2026.09），**含 vendor 目录**。git-lfs 管理，构建前必须 `git lfs pull` |
| `Source1` | `busybox_1.35_arm64` | glibc 版 busybox，`%prep` 拷进 `packages/orchestrator/internal/template/build/core/systeminit/`，编进模板构建器作为沙箱 rootfs 的 init（配合 patch 里 `busybox.go`、`inittab.tpl`、`envd.service.tpl` 的改动；musl 版 busybox 无 bash，`configure.sh` 跑不起来，是当年的实际踩坑） |
| `Source2/3` | `goose_3.24.2_linux_{arm64,x86}` | DB 迁移工具，按目标架构选一个装到 `bin/goose` |
| `Source4/5/6` | `vmlinux.bin.{arm,x86,arm.openeuler}` | 沙箱客户机内核。aarch64 包装 arm 版为 `bin/vmlinux.bin`、openEuler 变体为 `bin/vmlinux.bin.openeuler`；x86 包装 x86 版 |
| `Source7` | `patch_e2b.py` | SDK https→http 补丁脚本，装到 `/opt/e2b-infra/patch_e2b.py` |
| `Source8` | `firecracker.arm` | ARM 版 Firecracker VMM，装到 `bin/firecracker`（仅 aarch64 包） |
| `Source9` | `e2b-deploy.tar.gz` | 部署包（`build.sh` + `dep/`），整体铺进 `/opt/e2b-infra/` |
| （未声明） | `tools-{arm64,amd64}.tar.gz` | **离线 Go 工具链**。注意它没有 SourceN 编号，`%prep` 里直接用 `%{_sourcedir}` 路径按架构解压——所以它必须和其它 Source 放在同一目录（仓库根） |

### 2.2 补丁体系

```
Patch1: 0001-adapted-for-arm-architecture.patch   （~300KB，主改造）
Patch2: 0002-fc-launch-dedicated-helper.patch     （~32KB，FC 启动优化）
```

`%autosetup -p1` 会在解包后按序应用两个补丁（`-p1` 去掉路径前缀 `a/`、`b/`）。

**Patch1（ARM 适配 + 去 GCP 化 + 可观测性）** 触达约 100 个文件，按改动域拆解：

| 改动域 | 代表文件 | 干了什么 |
|---|---|---|
| 服务发现 | `packages/api/internal/clusters/discovery/local.go`、`nomad_discovery.go`、`k8s_discovery.go`、`packages/shared/pkg/clusters/discovery/*` | 去掉 GCP 依赖，新增 local/nomad/k8s 三种 orchestrator 发现方式（单机用 `LOCAL_CLUSTER_ENDPOINT`） |
| 存储后端 | `packages/shared/pkg/storage/storage_minio.go`、`storage.go` | 新增 MinIO/Local 存储 provider（模板/构建缓存桶），替代 GCS |
| 沙箱恢复埋点 | `packages/orchestrator/internal/sandbox/sandbox.go`、`fc/process.go`、`envd.go`、`server/utils.go` | `[ResumeSandbox]` 全链路耗时日志（benchmark 的数据源）；准入排队 `acquire wait cost` 埋点 |
| 准入并发上限 | `packages/orchestrator/internal/server/sandboxes.go`、`server/main.go` | `startingSandboxes` 信号量上限改为读环境变量 `MAX_STARTING_INSTANCES_PER_NODE`（默认 500），`acquireTimeout` 放宽 300s |
| 模板构建 | `template/build/core/systeminit/busybox.go`、`phases/base/provision.sh`、`finalize/configure.sh`、`core/oci/oci.go`、`rootfs/files/*.tpl` | busybox init 适配 glibc 版、离线 provision、Harbor（https 自签）拉镜像适配 |
| ARM 特有 | `packages/orchestrator/internal/sandbox/uffd/*`、`cgroup/manager.go`、`envd/internal/host/mmds.go` | userfaultfd/页大小、cgroup、mmds 等 aarch64 适配 |
| 构建体系 | 各 `packages/*/Makefile`、`Dockerfile`、`go.mod/go.sum` | vendor 构建可用、Dockerfile 改为离线基础镜像（debian:bookworm-slim） |
| 部署物料 | `iac/provider-gcp/nomad/jobs/*.hcl`、`iac/provider-gcp/nomad-cluster/scripts/*`、`.github/actions/host-init/init-client.sh`、`helm/*` | nomad job 模板去 GCP 化（上游版基线，dep overlay 在其上再增强）、部署脚本、k8s helm chart |

**Patch2（FC 启动三档优化）** 全部集中在 orchestrator：

| 文件 | 干了什么 |
|---|---|
| `cmd/fc-launch/main.go` | 新增专用启动器：单进程内完成 mount ns（经 `Cloneflags` 在 clone(2) 时创建）+ setns 进 netns + execve firecracker，等 socket 用 inotify。对应 `E2B_FC_LAUNCH_MODE=launch` |
| `cmd/fc-netns-exec/main.go` | 轻量助手：替换 shell 管道末端的 `ip netns exec`（setns+execve），省 iproute2 开销。对应 `netns-exec` 档 |
| `internal/sandbox/fc/mode.go`、`launchplan/*`、`process.go`、`script_builder.go`、`socket/socket.go` | 三档开关解析、启动计划、socket inotify 等待 |
| `Makefile` | `make build` 额外产出 `bin/fc-launch`、`bin/fc-netns-exec`（随 `packages/*/bin/*` glob 一起装进 RPM） |

两档优化的动机与原理详见 `benchmark/FC启动优化-netns-exec.md`、`benchmark/FC启动优化-launch.md`。

## 3. spec 逐段精读

### 3.1 头部

```spec
%define debug_package %{nil}      # 不产 debuginfo 子包（Go 二进制 strip 意义不大，还会因缺 build-id 报错）
%global tag 2026.09               # 上游 tag，一处定义多处引用
Name: e2b-infra    Version: %{tag}    Release: 4
BuildRequires: make gcc           # Go 工具链自带，不写进 BuildRequires
```

`Release` 是**部署迭代号**：每次改 patch/dep 重发包都应 +1（当前 4），并同步更新 `%changelog`。
同一 Release 反复重建时，安装要用 `rpm -Uvh --force`（见 §7）。

### 3.2 `%prep`：解包 + 打补丁 + 摆好离线料

```spec
%autosetup -p1 -n e2b-infra-%{tag}
```
等价于：解开 Source0 到 `BUILD/e2b-infra-2026.09/`，然后按序 `patch -p1` 应用 Patch1、Patch2。
**补丁应用失败会直接停在这一步**——改 patch 后构建报 `FAILED ... hunk` 就是这儿。

```spec
cp %{SOURCE1} packages/orchestrator/internal/template/build/core/systeminit/
```
把 glibc busybox 放进模板构建器的嵌入目录（Go `embed` 或构建时打包进二进制，
模板构建时写入沙箱 rootfs 当 init 用）。

```spec
mkdir -p %{_builddir}/go-toolchain
%ifarch aarch64
    tar -xf %{_sourcedir}/tools-arm64.tar.gz -C %{_builddir}/go-toolchain
%endif
```
解出自带 Go 工具链到 `~/rpmbuild/BUILD/go-toolchain/go`。这就是**构建机不需要装 Go** 的原因。

```spec
tar -xf %{SOURCE9} -C %{_builddir}
```
把部署包解到 `BUILD/e2b-deploy/`，留给 `%install` 阶段整体拷走。

### 3.3 `%build`：离线编译五个模块

```spec
export GOROOT=%{_builddir}/go-toolchain/go
export PATH=$GOROOT/bin:$PATH
export GOFLAGS=-mod=vendor          # 只用源码包里的 vendor 目录，绝不联网
rm -f go.work go.work.sum           # workspace 模式与 -mod=vendor 冲突，必须删
```

先单独编 seed-db（初始化用户的小工具，静态编译方便进任何环境）：

```spec
pushd packages/db/scripts/seed/postgres
CGO_ENABLED=0 go build -o seed-db seed-db.go
popd
```

再循环各模块 `make build`：

```spec
for d in packages/api packages/client-proxy packages/envd packages/db packages/orchestrator; do
    pushd $d && make build && popd
done
```

每个模块的 Makefile（patch 改造过）把产物放进各自的 `bin/`。产物一览：

| 模块 | `make build` 产物（→ 汇入 `/opt/e2b-infra/bin/`） |
|---|---|
| `packages/api` | `api` |
| `packages/client-proxy` | `client-proxy` |
| `packages/envd` | `envd`（沙箱内代理） |
| `packages/db` | 迁移相关（migrations 由 `%install` 直接 cp 目录） |
| `packages/orchestrator` | `orchestrator`（同一二进制按 `ORCHESTRATOR_SERVICES` 环境变量决定跑哪些服务）、**`fc-launch`、`fc-netns-exec`**（patch 0002 加的） |

> 构建耗时主要在 orchestrator（依赖多）。vendor 不全时报
> `cannot find module providing package ...`——说明改了 go.mod 却没同步 vendor，
> 需要在有网机器上 `go mod vendor` 后重打源码包。

### 3.4 `%install`：产物 → buildroot 映射

spec 里分了 4 个小节（注释是中文的，很好认），全部装进 `/opt/e2b-infra/`：

**① 主目录 + nomad job 模板**
```spec
install -d %{buildroot}/opt/e2b-infra/bin
for hcl in iac/provider-gcp/nomad/jobs/*.hcl; do
    install -D -m 644 "$hcl" %{buildroot}/opt/e2b-infra/nomad/$(basename "$hcl")
done
```
→ `nomad/`：api.hcl、edge.hcl、redis.hcl、template-manager.hcl（这 4 个会被 `deploy.sh`
提交），以及 orchestrator.hcl、clickhouse.hcl、loki.hcl、logs-collector.hcl、
otel-collector.hcl（这 5 个单机部署不提交，仅备用）。

**② 二进制**
```spec
install -D -m 755 packages/db/scripts/seed/postgres/seed-db  → bin/seed-db
for exe in packages/*/bin/*; do → bin/$(basename)            # 所有模块产物一网打尽
```

**③ Dockerfile**（供 `deploy.sh` 在部署机上 `docker build`）
```spec
packages/db/Dockerfile        → bin/db-migrator.Dockerfile    # 特例改名
packages/api/Dockerfile       → bin/api.Dockerfile
packages/client-proxy/...     → bin/client-proxy.Dockerfile
packages/orchestrator/...     → bin/orchestrator.Dockerfile   # 部署时会被 build.sh -s 删掉（orchestrator 走 raw_exec 不容器化）
```
（循环带 `[ -f ... ] || continue`，所以只装实际存在的。）

**④ 运维脚本 + 配置（扁平铺到顶层）**
```spec
iac/provider-gcp/nomad-cluster-disk-image/setup/install-{consul,nomad}.sh、nomad.service
iac/provider-gcp/nomad-cluster/scripts/{uninstall-consul,uninstall-nomad,start-api,start-client,start-server,run-consul,run-nomad}.sh
iac/provider-gcp/nomad/jobs/deploy.sh、env.template
.github/actions/host-init/init-client.sh
packages/db/migrations           → bin/migrations
packages/clickhouse/migrations   → bin/migrations-clickhouse
cp -rp %{_builddir}/e2b-deploy/* → /opt/e2b-infra/            # build.sh、build_prod.py、dep/ 整体铺入
```

> **关键理解**：顶层的 `deploy.sh`/`start-*.sh`/`run-*.sh` 此时是**上游源树(+patch)版**；
> `dep/` 里躺着**增强版**。要等 `build.sh -i` 执行时才把 dep 版覆盖上去（dep overlay）。
> 这就是为什么 **rpm 升级后必须重放 overlay**（runbook §5.0）——rpm 又把顶层铺回了上游版。

**⑤ 按架构装二进制**
```spec
aarch64: goose(arm) / vmlinux.bin + vmlinux.bin.openeuler / firecracker
x86_64 : goose(x86) / vmlinux.bin(x86)（无 firecracker——x86 用官方发布件）
另：patch_e2b.py → /opt/e2b-infra/patch_e2b.py；helm/ → /opt/e2b-infra/helm/
```

### 3.5 `%files`：包的"属地"声明（升级行为的根源）

`%files` 列出的路径归 RPM 管：**升级时一律用新包内容替换；没列的 RPM 永远不碰**。
对照 runbook §5 的覆盖矩阵：

| `%files` 里有 | 升级被覆盖 | `%files` 里没有 | 升级不动 |
|---|---|---|---|
| `/opt/e2b-infra/bin/*` | ✅（新二进制正是这么来的） | `/opt/e2b-infra/.env`（顶层） | ✅ 真 token 幸存 |
| `/opt/e2b-infra/nomad/*.hcl` | ✅（被打回上游+patch 默认版） | `/opt/e2b-infra/rendered/*` | ✅ 正在运行的渲染产物幸存 |
| 顶层 `deploy.sh`、`start-*.sh` 等 | ✅（被打回上游版，丢 `--only` 等增强） | `/opt/e2b-infra/harbor/` | ✅ Harbor 安装目录幸存 |
| `/opt/e2b-infra/dep/*`（含 `dep/.env`） | ✅（重置为仓库基线） | `/usr/bin/orchestrator`、`/usr/bin/template-manager` | ✅ 所以场景二要手动 cp 刷新 |
| `build.sh`、`*.py`、`helm/*` | ✅ | `/etc`、`/data/nomad`、`/opt/consul` 等系统位置 | ✅ RPM 不碰运行状态 |

`%license LICENSE`、`%doc README.md` 把许可证/说明装进标准 doc 路径。

### 3.6 `%changelog`

记录每个 Release 改了什么（当前最新 2026.09-4）。**每次发新包在顶部追加一条**，
格式 `* 星期 月 日 年 作者 <邮箱> - 版本-Release`，正文一行一个要点。

## 4. `e2b-deploy.tar.gz` 的地位与安全重建

它是 RPM 的 Source9，也是"dep overlay"的唯一来源。仓库里同时存在：

- `e2b-deploy.tar.gz` —— 构建**真正用**的压缩包（含 `dep/ubuntu-22.04-custom.tar.gz` 约 70MB）；
- `e2b-deploy/` —— 可读镜像目录（**不含**那个 70MB 镜像和 `.git`），供 review。

**改了 `e2b-deploy/` 下任何文件后，必须重建压缩包再构建 RPM**，且不能直接 tar 目录
（会把 70MB 镜像弄丢）。安全流程：

```bash
REPO=$PWD                                   # 仓库根
cd /tmp && rm -rf e2b-deploy
tar -xzf "$REPO/e2b-deploy.tar.gz"          # 1) 解开旧包（拿到 ubuntu-22.04-custom.tar.gz）
rsync -av --exclude .git "$REPO/e2b-deploy/" e2b-deploy/   # 2) 用仓库目录覆盖文本内容
tar -czf "$REPO/e2b-deploy.tar.gz" e2b-deploy              # 3) 重打包放回仓库根
```

反向地，如果直接改了压缩包，也要同步更新可读目录（解包后 rsync 回 `e2b-deploy/`，
排除 `ubuntu-22.04-custom.tar.gz` 与 `.git`），保证 git diff 可读——这是仓库的约定（根 README）。

## 5. 构建产物验收清单

装完包后快速核对（任何一项缺失都说明包/构建有问题）：

```bash
rpm -ql e2b-infra | head -50                       # 包内文件清单
ls /opt/e2b-infra/bin/ | sort
# 应至少包含：api client-proxy envd orchestrator seed-db goose vmlinux.bin firecracker
#             fc-launch fc-netns-exec              ← 没有这两个 = 构建时没带上 patch 0002
#             api.Dockerfile client-proxy.Dockerfile db-migrator.Dockerfile orchestrator.Dockerfile
#             migrations/ migrations-clickhouse/
ls /opt/e2b-infra/nomad/*.hcl                      # job 模板齐全
ls /opt/e2b-infra/dep/ | head                      # dep overlay 源就位
strings /opt/e2b-infra/bin/orchestrator | grep -c "acquire wait cost"   # ≥1 = 埋点 patch 在
```

## 6. 完整重建操作手册（从改代码到部署机生效）

```bash
# 0) 首次准备：拉源码大件
git lfs pull                                        # e2b-infra-2026.09.tar.gz 变成真文件

# 1) 改动落到正确源头：
#    - Go 源码改动 → 改 0001/0002 patch（见 §6.1）
#    - 部署脚本/配置 → 改 e2b-deploy/dep/* → 按 §4 重建 e2b-deploy.tar.gz
#    - 打包逻辑     → 改 e2b-infra.spec（记得 Release+1 与 %changelog）

# 2) 构建（在 aarch64 机器上）
rpmbuild -bb e2b-infra.spec --define "_sourcedir $PWD"
# 产物：~/rpmbuild/RPMS/aarch64/e2b-infra-2026.09-<Release>.aarch64.rpm

# 3) 安装/升级到部署机
rpm -Uvh --force ~/rpmbuild/RPMS/aarch64/e2b-infra-2026.09-4.aarch64.rpm
#   --force：同 Release 反复迭代时也能覆盖安装
#   ⚠️ 写明 Release，别用 e2b-infra-*.rpm 通配（RPMS 里留着旧包会一起匹配报冲突）

# 4) rpm 之后的必做动作：重放 dep overlay（runbook §5.0 有完整脚本）
#    然后按改动类型走 runbook §5 对应场景（如场景二：cp 新二进制到 /usr/bin + stop/run job）
```

### 6.1 怎么改 patch（Go 源码改动的标准姿势）

> **推荐流程已升级**：现在以 `e2b-infra-arm` 源码仓库的分层 git 历史为真相源，
> 用 `scripts/gen-patches.sh` 机械化导出 patch，见 **08-补丁规范流程.md**。
> 下面的"临时工作树"方法仅作应急备用。

patch 文件不能手改（行号/上下文一错就 FAILED）。标准流程是**维护一棵打好补丁的工作树**：

```bash
# 1) 展开上游源码并应用现有补丁（与 %prep 完全一致的状态），每层打个 tag 作基线
tar -xzf e2b-infra-2026.09.tar.gz && cd e2b-infra-2026.09
git init -q && git add -A && git commit -qm base && git tag upstream   # 基线：纯上游
patch -p1 < ../0001-adapted-for-arm-architecture.patch
git add -A && git commit -qm p1 && git tag patch1                      # 基线：patch1 之后
patch -p1 < ../0002-fc-launch-dedicated-helper.patch
git add -A && git commit -qm p2 && git tag patch2

# 2) 直接改代码、本地验证（可用仓库的 tools-arm64.tar.gz 里的 go 编译）

# 3) 重新生成受影响的 patch（改动属于哪层就重生成哪个）：
git add -A && git commit -qm "my change"
git diff patch1 HEAD > ../0002-fc-launch-dedicated-helper.patch    # 改动属于 patch2 范围
# 改动属于 patch1 范围：git diff upstream <patch1新状态> > ../0001-...patch，
# 然后务必确认 patch2 仍能在其上干净叠加

# 4) 验证补丁能干净应用（模拟 %prep）：
cd /tmp && rm -rf t && mkdir t && tar -xzf $REPO/e2b-infra-2026.09.tar.gz -C t && cd t/e2b-infra-2026.09
patch -p1 --dry-run < $REPO/0001-*.patch && patch -p1 < $REPO/0001-*.patch
patch -p1 --dry-run < $REPO/0002-*.patch
```

### 6.2 常见构建报错速查

| 报错 | 原因 / 处置 |
|---|---|
| `Bad source: .../e2b-infra-2026.09.tar.gz`（或 tar 报不是 gzip） | 忘了 `git lfs pull`，源码包还是 LFS 指针 |
| `%prep` 阶段 `Hunk #N FAILED` | patch 与源码不匹配：patch 是手改的、或两个 patch 顺序/基线错了，按 §6.1 重生成 |
| `cannot find module providing package ...` | vendor 不全：go.mod 加了依赖但源码包里 vendor 没更新 |
| `go.work` 相关报错 | spec 已 `rm -f go.work`，若复现说明源码包结构变了 |
| 安装时 `file ... conflicts with file from package ...` | RPMS 目录里旧 Release 的包被通配符一起装了；写明确的文件名 |
| 安装时 `package ... is already installed` | 同 Release 重装，用 `rpm -Uvh --force` |

## 7. 与部署的衔接：三层叠加模型（收束）

RPM 只是三层中的第一层。部署机上 `/opt/e2b-infra/` 的最终形态 =

1. **RPM 铺底**（本篇）：`%files` 清单内的一切，升级即重铺；
2. **dep overlay**（04 篇 `install_e2b`）：增强版脚本/配置盖到顶层，**每次 rpm 升级后要手动重放**；
3. **bootstrap/运行产物**（05 篇）：顶层 `.env` 真 token、`rendered/`、`/etc` 配置、SDK 补丁——RPM 永不触碰，但 `build.sh -i` 会重置 `.env`、`-s` 会重新 bootstrap。

所以"改了源码怎么上线"的正确答案永远是 runbook §5 的场景表，而不是无脑 `-i -s`。
