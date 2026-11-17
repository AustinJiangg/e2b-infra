# 进程级快照回滚：部署、验收与切换

本目录让已按 [`single-node-offline-deploy.md`](../../single-node-offline-deploy.md) 部署了沙箱级快照回滚的机器，用一条命令切到进程级快照回滚（criu-rollback），跑完验收，再切回来。950 上 `git pull` 拿到最新仓库后即可使用，不需要另外拷贝文件。

- **改动范围。** 只改 orchestrator 所在的那一个 Nomad job（默认 `template-manager-system`）：切到进程级时换二进制路径和三个环境变量，切回时恢复**本机保存的原 job**。不重装 RPM，不执行 `build.sh -d/-i/-s`，不覆盖 `/usr/bin`、`/fc-kernels`、`/fc-versions`，API、client-proxy 等其他 job 不动。
- **宿主上的位置。** 三个运行文件在 `/fc-criu/`，切栈基线在 `/var/lib/criu-rollback/`，客户端 conda 环境是命名环境 `jll-e2b-criu`。布局见第 1 节。
- **产物。** gsd、guest 内核、orchestrator 是 920B 上验收过的二进制，以 xz 压缩入库。orchestrator 和 gsd 用 `strip -g` 去掉了调试段，代码段与验收版逐字节相同。
- **镜像。** 前两层取仓库根目录已有的 `ubuntu-22.04-custom.tar.gz`，CRIU 那一层单独入库，现场拼回。拼出的镜像 ID 与验收过的相同。
- **SDK。** 以纯 Python wheel 入库。离线克隆原 `jll-e2b` 为 `jll-e2b-criu`，只在新环境里替换 SDK，原 `jll-e2b` 不改。
- **结果直接打印到终端。** `all` 结束时把逐步结论、身份、网络比对、验收与压测摘要打印出来；细节用 `950.sh report` 按部分打印。复制终端内容即可，不打包、不需要拷文件。
- **950 上的 e2b 沙箱都是我们的。** 切栈、清 netns 之前，自动删掉全部 e2b 沙箱；同机 AgentENV 的沙箱和网络不碰。
- **验证状态。** 已在 920B 上多次完整走过，记录见第 14 节。950 尚未执行；950 首轮结果是它自己的基线，不能用 920B 的耗时当通过阈值。

下面的命令都在 **e2b-infra 仓库根目录**以 root 执行。

## 0. 最简用法

**首次：一条命令。**

```bash
git pull
```
```bash
bash criu-rollback/deploy/950.sh all
```

它依次完成下面的步骤，每一步打印进度，任何一步失败就停下并说明原因：

1. 校验部署目录；
2. 把三个运行文件和镜像展开到 `criu-rollback/deploy/.work/`，逐项核对 SHA256 和镜像 ID；
3. 没有 `jll-e2b-criu` 就从 `jll-e2b` 离线克隆，再装 SDK；
4. 检查客户端凭据和 API；
5. 导入镜像并推送到 Harbor；
6. 删掉全部 e2b 沙箱，预检，保存本机沙箱级基线；
7. 落位三个运行文件；
8. 切到进程级，切换前后各抓一次宿主网络配置做比对；
9. 建模板 `base-criu`，自动等到服务能建沙箱；
10. 验收，然后压测；
11. 切回沙箱级，等原模板 `base` 能建沙箱，跑冒烟；
12. 把结果打印到终端（第 12 节）。

920B 上全程约 11 分钟，不需要手动 `conda activate`，也不需要估等待时间。

**之后的测试**：在仓库根目录用 `bash test950.sh quick|standard|extended|long`，它按档调用本目录的 `950.sh quick|acceptance|stress|stress-ext|soak` 和沙箱级的 `run.sh`，并负责切栈、记录身份、续跑、清 netns，说明见 [`test950/README.md`](../../test950/README.md)。

跑之前要具备的条件：

- 沙箱级已部署好，原模板 `base` 能建沙箱（`build.sh -s` 不建模板，要按单机部署文档在 `benchmark/` 下执行 `build_template.py`）。
- 原沙箱级的 conda 环境 `jll-e2b`，conda 装在常见位置（`/root/miniforge3`、`/root/miniconda3` 等）。位置特殊时在 `criu-rollback/deploy/ops/site.env` 写 `CONDA_EXE=`。
- 客户端凭据：默认读 root 的 `~/.e2b/config.json`（部署首次初始化数据库时写入），没有时按第 5 节放 `criu-rollback/scripts/.env`。
- Harbor：没登录过的话，先执行一次 `docker login harbor:443`。没有 Docker、镜像在别的机器导入时，在 `ops/site.env` 写 `CRIU_SKIP_IMAGE_IMPORT=1`。
- 部署不是默认形态（job 名、端口等）时，先按第 2 节准备 `ops/site.env`。

**中途停下怎么办。** 按 Ctrl-C、断网或某一步失败，都只要处理完原因后再执行同一条 `bash criu-rollback/deploy/950.sh all`。它会从停下的那一步继续，已经做完的镜像导入、验收、压测、冒烟不会重做。

- 验收或压测失败时，脚本先删掉 e2b 沙箱，再自动切回沙箱级，机器不会停在一半的状态。
- 上一轮全部完成后再执行 `all`，会开始新的一轮；想放弃未完成的那一轮重来，加 `--new`。

**以后日常切换：**

```bash
bash criu-rollback/deploy/950.sh criu
```
```bash
bash criu-rollback/deploy/950.sh sandbox
```

- 已经是目标栈时不重启服务。
- 切换前删掉全部 e2b 沙箱：先经 API 删，60 秒后仍在运行的 e2b Firecracker 直接结束。只认 `/fc-versions/` 下启动的 Firecracker，AgentENV 的（在 `/var/lib/aenv/` 下）不算、不碰。
- 切换后等到能建沙箱才返回。切到进程级时，`base-criu` 不在或建不出沙箱就自动重建。
- 每次都比对切换前后宿主上非 e2b 的网卡、路由、策略路由和 iptables 规则，有变化逐条打印。

客户端要用对应的 conda 环境：进程级用 `jll-e2b-criu`，沙箱级用 `jll-e2b`。`950.sh` 和 `test950.sh` 自己会选，不需要激活。

**同机有 AgentENV 时。** 看输出里的“宿主网络比对”。“无变化”表示 AgentENV 的网卡和规则没有被动过；有变化时把打印出的那几行复制回来核对。比对排除了 e2b 自己的 `e2bv-` 网卡、`ns-` 命名空间和 `10.13`/`10.14` 地址池。

**以后 `git pull` 带来新的二进制时。** `all` 和 `criu` 会重新展开并核对。`/fc-criu/` 里已有内容不同的旧文件时，落位步骤拒绝覆盖，要先确认旧文件没有在用，再移走。

**网络命名空间。** 每次重启 orchestrator 都会留下约 300 个 e2b 网络命名空间和对应的 iptables 规则，`all` 重启两次。积累到上千个时执行：

```bash
bash criu-rollback/deploy/950.sh recycle-netns
```

它在当前栈上做：删全部 e2b 沙箱 → 停 job → `/opt/e2b-infra/build.sh --recycle-netns`（服务停着时它只清理、不拉起；只认 `e2bv-` 网卡、`ns-<n>` 和本机地址池，不碰 AgentENV）→ 按当前栈重新拉起并等到能建沙箱。前后打印 e2b netns 数和 nat 规则数，再做一次宿主网络比对。`test950.sh` 在 e2b netns 超过 1500 个时自动调用它。

第 1～13 节是逐步说明，`all` 出问题时对照着查。

## 1. 目录里有什么

```text
criu-rollback/deploy/
  950.sh                            唯一入口：all（首次全流程）、criu/sandbox（日常切换）、recycle-netns、report 及各单步
  README.md                         本操作说明
  manifest.json                     来源、版本、镜像组成，三个运行文件解压后的 SHA256
  SHA256SUMS                        本目录入库文件的校验清单
  artifacts/
    orchestrator.xz  gsd.xz  vmlinux.bin.xz
    image/criu421-layer.tar.xz      镜像的 CRIU 层；配置、清单等小文件同目录
  sdk/
    e2b-2.19.0+gsd.dcca61266-py3-none-any.whl  可离线安装的进程级 SDK（版本号是发布时定的，不随改名变化）
    test_gsd_headers.py             SDK 自带的鉴权头测试，安装后离线执行
  ops/
    unpack.py                       展开运行文件、拼镜像并核对
    flow.py                         all 与日常切换的流程：续跑、就绪等待、模板检查、删 e2b 沙箱、失败回切、网络比对、终端汇报
    stack.py                        保存基线、预检、落位、切换与恢复、清 netns
    report.py                       把结果目录整理成终端文本（test950.sh 也用）
    envs.py                         定位 conda 与两个客户端环境，用环境自己的解释器运行，不需要激活
    paths.py                        本目录、仓库内各位置与宿主上各位置的定义
    probe.py                        查询模板、试建沙箱、删除全部 e2b 沙箱
    import-image.sh                 导入并推送镜像
    install_sdk.py / check_python.py / check_sdk_headers.py   SDK 安装与检查
    guest_check.py                  模板内实际版本检查
    run_tests.py                    串行执行测试并判读结果
    sandbox_smoke.py                切回后的沙箱级冒烟
    site.env.example                非默认部署时的现场参数模板
    test_*.py                       本地单测，不连接 Nomad、conda 或 API
  provenance/                       内核配置、来源检查、验证记录
运行时生成（不入库）：.work/  results/  ops/site.env
```

宿主上由本目录生成的东西：

| 位置 | 内容 |
|---|---|
| `/fc-criu/orchestrator`、`/fc-criu/gsd`、`/fc-criu/kernels/vmlinux-6.1.158/vmlinux.bin` | 进程级的三个运行文件 |
| `/var/lib/criu-rollback/` | 切栈基线（原 job、二进制路径与 SHA256）、生成的 job、`all` 的进度。**含 950 密钥**，目录 700、文件 600，只留在本机，`report` 不打印 |
| conda 的 envs 目录下的 `jll-e2b-criu`（如 `/root/miniforge3/envs/jll-e2b-criu`） | 进程级客户端 conda 环境 |
| 仓库内 `criu-rollback/deploy/results/` | 每轮结果与日志 |
| Harbor `e2b-orchestration/ubuntu:22.04-custom-criu421` | 进程级模板的镜像 |
| API 上的模板 `base-criu` | 进程级模板，2 vCPU / 2048 MiB。原沙箱级模板 `base` 不受影响 |

用到的仓库其他位置：

| 位置 | 用途 |
|---|---|
| `criu-rollback/scripts/` | 建模板、验收、压测、探针脚本 |
| `rollback/scripts/950/run.sh` | 切回后的沙箱级冒烟 |
| `ubuntu-22.04-custom.tar.gz` | 镜像前两层 |
| `criu-rollback/dist/criu-rollback-docs.html` | 进程级手册单文件版 |

版本身份：

| 内容 | 版本 |
|---|---|
| gsd / guest 内核 | 手册已验证的 `259f1a25c` 发布产物。gsd 经 `strip -g`，SHA256 `421597e5…`（去调试段前 `a38b0db9…`）；内核未改，`b81306e7…` |
| orchestrator | `259f1a25c-e2bv`：在 `259f1a25c` 上只把宿主机侧网卡名由 `veth-<n>` 改为 `e2bv-<n>`。经 `strip -g`，SHA256 `063e580e…`（去调试段前 `151308ba…`）。手册第 20 章的 `818fa667…` 是改网卡名之前的版本 |
| 去调试段 | GNU strip 2.41 `-g`：只删 DWARF，保留符号表；`.text`、`.rodata`、`.data`、`.noptrdata`、`.gopclntab`、`.go.buildinfo` 与加载段逐字节相同，BuildID 与 `gsd -commit` 不变。panic 栈、pprof、perf 照常显示函数名，只是不能再用 gdb/dlv 按源码行调试 |
| SDK | 与 `dcca61266` 的 SDK 逐文件一致；包含后续流量 token 修复 |
| 镜像 | `ubuntu:22.04-custom-criu421`，arm64，CRIU 4.2.1。镜像配置摘要 `7caae4a62095a4371cbef9d4cf31a6734463f6cda976d34d95f49f9ae78a96ab`（docker 传统存储显示的 image ID），OCI 清单摘要 `b7a2fdd21a5bd5a9013aca2c3062b28425038199daf19825ffe4fe84d537b165`（containerd 镜像存储显示的 image ID） |
| Python | 使用本机 `jll-e2b` 克隆出的 `jll-e2b-criu`，保留原 Python 版本；不携带 Python 环境 |
| SDK wheel | `2.19.0+gsd.dcca61266`，源码版本 2.19.0 加本地标识；通用 `py3-none-any` 包 |

SDK 与二进制的提交号不同是有意的：`dcca61266` 只是在发布版之后修复 SDK 给 gsd 转发流量 token，二进制不变。

`overlay-init`、`gsd.service` 已嵌入 orchestrator，无需单独安装。Firecracker、宿主内核、原沙箱级二进制和模板继续使用机器上已有的内容。

## 2. 校验与现场参数

```bash
bash criu-rollback/deploy/950.sh verify
```
```bash
bash criu-rollback/deploy/950.sh unpack
```

必须看到 `部署目录 SHA256 全部通过` 和 `展开完成`（或 `已展开，核对通过`）。有任何文件缺失或校验不符，停止，不部署；先 `git status` 确认仓库没有被改动，不要在现场重新生成 `SHA256SUMS`。

默认值是 job `template-manager-system`、端口 5008、服务端环境文件 `/opt/e2b-infra/.env`、运行文件 `/fc-criu`、基线目录 `/var/lib/criu-rollback`。不同时先复制模板再改：

```bash
cp criu-rollback/deploy/ops/site.env.example criu-rollback/deploy/ops/site.env
```

脚本会查找唯一对应的 raw_exec task，不要求它必须位于 TaskGroups[0].Tasks[0]。它只处理 `ORCHESTRATOR_SERVICES=orchestrator,template-manager` 的合并部署；其他部署形态会拒绝执行，不能强行继续。

## 3. conda 环境、SDK 与预检

第 3～10 节都已包含在 `all` 里，下面是逐步说明。所有命令自动使用对应的 conda 环境，不需要激活。

| conda 环境 | 位置 | 用途 |
|---|---|---|
| `jll-e2b` | 原位置（如 `/root/miniforge3/envs/jll-e2b`） | 原沙箱级 SDK，保留原样 |
| `jll-e2b-criu` | conda 的 envs 目录下（与 `jll-e2b` 同级） | 克隆 `jll-e2b`，再安装 `sdk/` 下的进程级 SDK |

```bash
bash criu-rollback/deploy/950.sh install-sdk
```

没有 `jll-e2b-criu` 时先执行 `conda create --offline --copy -n jll-e2b-criu --clone jll-e2b`：`--offline` 禁止联网取包，`--copy` 使用文件副本，克隆继承原环境的 Python、pip 和第三方依赖。然后用 wheel 执行 `pip --isolated install --no-index --no-deps --force-reinstall`，不从公网下载、不更改依赖；缺依赖时在替换 SDK 前停下并列出缺项。安装后检查 `e2b`、`e2b_connect` 都从新环境加载，并执行 7 项 SDK 鉴权头测试，输出应包括 `进程级 SDK 与依赖检查 PASS`、`SDK header tests: PASS 7 / FAIL 0`。

```bash
bash criu-rollback/deploy/950.sh preflight
```

`preflight` 不修改部署。它检查包校验、aarch64、KVM、命令是否齐全、orchestrator 动态库、gsd 提交号、Nomad job 结构、沙箱地址池环境变量、运行进程和 health、e2b 沙箱，以及 `jll-e2b-criu` 的 SDK/依赖。

**orchestrator 是动态链接的，需要 glibc 2.38 对应符号。** 若出现动态库缺失、`GLIBC_2.38 not found` 或权限错误，先处理兼容性，不要替换系统 glibc。gsd 自身是静态编译。

## 4. 将镜像导入 Harbor

镜像由第 2 节的展开步骤拼出，全部层都来自仓库，不需要访问公网。CRIU 4.2.1、curl、python3，以及 `systemd systemd-sysv openssh-server sudo chrony linuxptp socat ca-certificates` 均在位。

```bash
bash criu-rollback/deploy/ops/import-image.sh
```

脚本先校验包，再 `docker load`，按标签 `ubuntu:22.04-custom-criu421` 取导入后的镜像，核对它的 ID 是上表两个摘要之一、架构是 arm64，然后在不联网的临时容器中检查 CRIU、curl、python3 和预装包，最后 tag/push 到 `harbor:443/e2b-orchestration/ubuntu:22.04-custom-criu421`（引用不同时在 `ops/site.env` 写 `CRIU_TEMPLATE_IMAGE=`）。没登录过 Harbor 时先 `docker login harbor:443`。推送成功只证明导入端可访问；第 7 节模板构建成功才完成构建端拉取验证。

用哪种 docker 存储都行：`docker info --format '{{.Driver}} {{.DriverStatus}}'` 显示 `io.containerd.snapshotter.v1` 的是 containerd 镜像存储（950 是这种），显示 `overlay2` 的是传统存储（920B 是这种）。两种存储读归档里不同的清单文件，归档两种都带。

## 5. 核对客户端凭据

默认使用 root 的 `~/.e2b/config.json`，它是已有部署初始化数据库时写入的。公共模块还支持环境变量或 `criu-rollback/scripts/.env`。`all` 的“客户端凭据”一步会查一次 API 并列出现有模板，不打印 key/token。

默认 API 是 `http://localhost:3000`。API 地址、域名或访问方式不同时，新建 `criu-rollback/scripts/.env`，填 `E2B_API_URL`、`E2B_DOMAIN`、`E2B_HTTP_SSL`、`E2B_ACCESS_TOKEN`、`E2B_API_KEY` 五项，保存为 600 权限。`/opt/e2b-infra/.env` 是服务端配置，通常不包含完整客户端凭据。

## 6. 保存基线、落位、切换

```bash
bash criu-rollback/deploy/950.sh save-baseline
```
```bash
bash criu-rollback/deploy/950.sh install
```
```bash
bash criu-rollback/deploy/950.sh criu
```
```bash
bash criu-rollback/deploy/950.sh status
```

`save-baseline` 要求当前原服务健康、没有 e2b 沙箱（单独执行时先删沙箱；`all` 会自动删），记录原 job、实际二进制路径与 SHA256、相关进程环境，保存在 `/var/lib/criu-rollback/`。**文件含 950 密钥，只保存在本机，`report` 不打印。** 已有基线时拒绝覆盖；重复切换直接执行 `criu` 或 `sandbox`。

`install` 再跑只读预检，要求基线存在，随后只安装 `/fc-criu/` 下的三个文件。已有内容不同的文件时停下而不覆盖；相同内容可以重复安装。

`criu` 从保存的基线生成新 job，只替换选中 task 的二进制路径，并设置三个环境变量：`HOST_GSD_PATH=/fc-criu/gsd`（orchestrator 读这个名字，内置默认是旧位置 `/fc-gsd/gsd`，所以要显式设）、`HOST_KERNELS_DIR=/fc-criu/kernels`、`CRIU_ORCH_SHA256`；保留该机原有其余配置。

切换会先停旧 job，等待退出后提交新 job，避免固定端口冲突；然后核对**实际进程**的路径、SHA256、环境和 health。最长等待旧进程退出约 120 秒、新服务就绪约 360 秒，并额外等 20 秒。随后反复试建沙箱，直到成功才返回（切换返回后立即建沙箱可能报 `500: Failed to place sandbox`，约 1.5 分钟后正常）。

`status` 显示当前是哪一套栈、实际进程的路径与环境、health、e2b 沙箱数、e2b netns 数和 nat 规则数。

进程级栈运行期间，**所有新建模板**都会注入 gsd 和 overlay-init。

## 7. 在进程级栈下新建模板

`all` 和 `criu` 会自动建 `base-criu`（2 vCPU、2048 MiB，用第 4 节的镜像，不用缓存）。模板固定了构建时的 gsd、guest 内核和 overlay-init；以后换任意一项都要重新构建模板。不要拿沙箱级模板直接测 checkpoint，也不要在沙箱级栈上用 `base-criu`。

## 8. 必跑验收

```bash
bash criu-rollback/deploy/950.sh acceptance
```

默认测试 `base-criu`，结果目录自动创建在 `results/<时间>-acceptance/`。按下面顺序执行，遇到非零返回码或内容判据失败就停止，并写 `summary.json`：

| 顺序 | 内容 | 判据 |
|---|---|---|
| 1 | `ops/guest_check.py` | guest 内 gsd 提交号/SHA256、内核构建标识、THP never、RCU=1、服务 active、health ok、CRIU 4.2.1、根目录和 /tmp 权限、/tmp 位于 overlay、2 vCPU |
| 2 | `acceptance/verify_gsd.py base-criu 8` | 8 次 restore 全部 ok，根盘增量 +0，末行根盘恒定 |
| 3 | `acceptance/verify_fixes.py` | 26 PASS / 0 FAIL / 2 SKIP；SKIP 只能是 G-19、G-36；无 BAD 和 dmesg 可疑行 |
| 4 | `acceptance/verify_multitree.py --skip V12` | 12 项全 PASS；无 BAD 和可疑行，记录慢调用 |
| 5 | `acceptance/verify_clear_refs_race.py` | 完整 30 轮、每轮漏页 0、state=T、sha 相同、writer 未卡死 |
| 6 | `acceptance/demo_checkpoint_perf.py` 三次 | 每次 6 行恢复内容全正确，SDK/journal 最大差 0.000 ms |

性能脚本是独立负载分档（JSON `schema_version=2`、`measurement_mode=per-tier-size`）：每档新建沙箱，额外负载内存为 0 / 12 / 24 / 48 / 96 / 192 MiB，文件为 0 / 4 / 8 / 16 / 32 / 64 MiB；建立本档全量基点后改写负载，测增量 checkpoint，再恢复本档基点并校验全部内存 SHA256 和文件内容。这里只判功能正确性，不给性能达标线。V12 需要另一份仅版本不同的进程级模板作对照，首次部署跳过。

## 9. 探针和四组压测

```bash
bash criu-rollback/deploy/950.sh stress
```

先跑 `probe_whiteout_bug.py` 的七个用例，再顺序跑四组：

| 组 | 脚本与参数 |
|---|---|
| all | `stress_overlay_ckpt.py --mode stress --rounds 300 --workers 6` |
| noworkers | `stress_overlay_ckpt.py --mode stress --rounds 300 --workers 0` |
| multitree | `stress_multitree.py --mode multitree --rounds 200` |
| deldense | `stress_delete_dense.py --mode deldense --rounds 200` |

四组都通过 `run_stress_watch.sh` 执行，停滞阈值为 150 秒。通过要求包括：看门狗 `DONE rc=0`；串口证据存在且无内核错误；无 `*-create-fail-*`；dmesg 无新错误；worker 只允许 ENOENT/ESTALE；心跳未过期；gsd 只允许 `create:resource_exhausted`；两个专项组自身 verdict 也必须 PASS。任何一组失败就停止后续组。取不到串口日志不算通过。

## 10. 切回沙箱级

```bash
bash criu-rollback/deploy/950.sh sandbox
```

`sandbox` 使用第 6 节保存的**本机原始 job**，恢复原二进制路径及其余配置。恢复前检查原二进制仍是保存时的 SHA256；之后核对新进程的 SHA256 和三个相关环境变量（恢复成原来的状态，原来没有就是未设置），确认 health 200，再等原模板 `base` 能建沙箱。

冒烟（`all` 里自动跑）：

```bash
bash criu-rollback/deploy/950.sh smoke
```

它用 `jll-e2b` 的解释器和 SDK 跑 `rollback/scripts/950/run.sh smoke`：SDK 自检、能力信息和 checkpoint/restore 正确性，5 项全 PASS。

回切后 `/fc-criu/` 和 `base-criu` 都保留，下次 `950.sh criu` 直接复用。原沙箱级 checkpoint 与进程级 checkpoint 不能互相使用。

**升级沙箱级 RPM 之后。** 原二进制的 SHA256 变了，`sandbox` 会拒绝回切，防止盖掉别人的更新。此时先在沙箱级栈上核对新版本，再按需要重新保存基线（先移走 `/var/lib/criu-rollback/`），不要手改基线文件。

## 11. 失败时怎么处理

| 现象 | 下一步 |
|---|---|
| 包校验失败 | `git status` 确认仓库没被改动，重新 `git pull`；不要改校验清单 |
| 动态库/GLIBC 不兼容 | 停在切栈前；把预检输出复制回来，不改系统 glibc |
| 克隆或 SDK 安装提示缺离线依赖 | 保留完整报错；缺项补齐前不切栈，原 `jll-e2b` 不受影响 |
| 客户端 key/token 缺失 | 第 5 节使用本机有效凭据，不重装服务 |
| save-baseline 提示已有基线 | 正常重复切换直接用 criu/sandbox；不要删除或覆盖基线 |
| job 结构不同 | 保留现状，只读核对任务结构；不要手改脚本跳过保护 |
| 切换后端口/health 不对 | 查 `nomad job status` 和 alloc 日志；必要时执行 `bash criu-rollback/deploy/950.sh sandbox` |
| 模板构建失败 | 查镜像引用、Harbor 解析/认证、gsd health、CRIU 版本；不要重用失败模板作验收 |
| guest 自检发现旧 gsd/内核 | 确认当前栈和内核根目录后，无缓存重建模板 |
| 功能失败、漏页、内核错误 | 停止后续测试，完整保留本轮结果；`all` 会自动删沙箱并回切 |
| 预检提示缺沙箱地址池环境变量 | 部署早于 e2b-infra `870d087`；先升级部署或补齐 job 环境。缺这两项时 orchestrator 回落到 `10.11`/`10.12`，与同机 AgentENV 冲突 |
| 切换后立刻建沙箱报 `500: Failed to place sandbox` | 脚本会自动重试 5 分钟；仍失败再查 `/tmp/templates` 下模板文件是否还在，以及 orchestrator 日志 |
| 回切提示原二进制变化 | 见第 10 节“升级沙箱级 RPM 之后” |
| e2b netns 上千、建沙箱变慢 | `bash criu-rollback/deploy/950.sh recycle-netns` |

若切换中断，基线保存在独立目录 `/var/lib/criu-rollback/`，重新登录后仍可在仓库根目录运行 `bash criu-rollback/deploy/950.sh sandbox`。脚本不提供强制覆盖或 purge Nomad 的选项。

## 12. 看结果

`all` 结束（含失败、中断）时自动打印一段结果，从 `==== 950.sh all 结果` 一行到 `==== 结束 ====` 一行，内容是：逐步状态与用时、三个运行文件的 SHA256、当前 5008 上的进程、宿主网络比对、验收与压测每一项的结论（压测附轮数、create/restore 次数、p50、错误计数）、沙箱级冒烟每一项。复制这一段即可。

以后随时重新打印最近一轮：

```bash
bash criu-rollback/deploy/950.sh report
```

细节按部分打印：

| 命令 | 打印什么 |
|---|---|
| `bash criu-rollback/deploy/950.sh report acceptance` | 性能三轮的分档表（全量基点、增量 checkpoint、restore）和各项日志末尾 |
| `bash criu-rollback/deploy/950.sh report stress` | 四组压测摘要；失败组的错误样本、日志末尾和看门狗结果 |
| `bash criu-rollback/deploy/950.sh report smoke` | 冒烟各项日志末尾 |
| `bash criu-rollback/deploy/950.sh report netcheck` | 宿主网络比对全文 |
| `bash criu-rollback/deploy/950.sh report log` | 全流程日志末尾 |
| `bash criu-rollback/deploy/950.sh report files` | 结果目录里的全部文件 |

需要某个文件时，用 `report files` 找到它的相对路径，再用 `--file` 打印，`--lines N` 指定行数（0 为全部），例如：

```bash
bash criu-rollback/deploy/950.sh report --file stress/all/RESULT --lines 0
```

含密钥的文件（job 定义、基线、dotenv）不在结果目录里，`report` 也拒绝打印这类文件名。

## 13. 校验清单与验证范围

`SHA256SUMS` 覆盖本目录入库的文件，不包含运行后新建的 `.work/`、`results/`、`ops/site.env`。展开出的三个运行文件和镜像按 `manifest.json` 另行核对。conda 环境内部文件不在校验范围内。

打包方已验证：三个二进制哈希；镜像归档和层结构；镜像的离线依赖；SDK wheel 完整性和源码对应关系；conda 克隆环境的离线安装检查；job 处理单测、脚本语法和包完整性。具体记录见 `provenance/package-validation.json`。

这套入口已在 920B 实际切换过服务（第 14 节），950 上尚未执行。只有在 950 上完成第 8、9、10 节之后，才能确认这台 950 的实际运行结果。

## 14. 更新记录与 920B 实测

### 2026-11-17：补上镜像归档的 OCI 清单（950 上导入失败）

- **现象。** 950 上 `all` 停在“导入镜像到 Harbor”：`docker load` 打印了 `Loaded image: ubuntu:22.04-custom-criu421`，随后按 image ID `7caae4a6…` 查镜像报 `No such image`。`docker images` 里这个镜像是 0B，架构为空，`docker run` 报 `content digest sha256:b7a2fdd2…: not found`。
- **原因。** 950 的 docker 用 containerd 镜像存储（`docker info` 显示 `io.containerd.snapshotter.v1`），导入时读归档里的 `index.json`，它指向 OCI 清单 blob `b7a2fdd2…`。11-16 把离线包并入仓库时只留了 `manifest.json` 用到的文件，这个清单 blob 和一个旧格式层描述 `df1e2492…` 没有入库。920B 的 docker 用传统存储，只读 `manifest.json`，所以 920B 上没暴露。另外 containerd 存储把 OCI 清单摘要当 image ID，即使归档完整，按 `7caae4a6…` 也查不到。
- **修改。**
  - 两个 blob 从 920B 上同一镜像的 `docker save` 取回入库，SHA256 与 `index.json` 记录的一致；拼出的归档与 `docker save` 的条目名单完全相同。
  - `.work/image/image-id` 改为记录镜像 ID 加归档条目名单，入库文件增减时旧归档自动重拼。
  - `import-image.sh` 按标签取镜像，接受两种存储各自的 ID，并核对架构；导入前先去掉同名旧标签（上次留下的 0B 空镜像）。
  - 单测新增：`index.json` 指向的清单、清单里的配置和各层都在归档里。
- **920B 验证。** 用 docker 自带的 containerd 在临时 namespace 导入新归档：`complete (4/4)`、221.7 MiB、`linux/arm64`，验完删掉了这个 namespace。传统存储上 `import-image.sh` 完整通过，image ID 仍是 `7caae4a6…`，推送后仓库摘要仍是 `abc4b4b7…`。

### 2026-11-17：gsd 改名 criu，结果打印到终端，切栈前自动删 e2b 沙箱

- **改名与位置。** 运行文件 `/fc-gsd/` → `/fc-criu/`，基线 `/var/lib/gsd-950-offline/` → `/var/lib/criu-rollback/`，`950.sh gsd` → `950.sh criu`，模板 `base-gsd` → `base-criu`，conda 环境 `jll-e2b-gsd` → `jll-e2b-criu`，脚本变量 `GSD_*` → `CRIU_*`。job 里改为显式设 `HOST_GSD_PATH=/fc-criu/gsd`（orchestrator 内置默认是旧位置 `/fc-gsd/gsd`）。gsd 二进制、orchestrator 读的 `HOST_GSD_PATH`、SDK wheel 的版本号、`verify_gsd.py` 等以 gsd 守护进程命名的脚本不变。
- **结果。** 去掉打包步骤，`all` 结束时打印结果，新增 `950.sh report`、`test950.sh report`。
- **沙箱。** 切栈、清 netns 之前自动删掉全部 e2b 沙箱（只认 `/fc-versions/` 下的 Firecracker，AgentENV 的不碰）。
- **netns。** 新增 `950.sh recycle-netns`；`test950.sh` 在 e2b netns 超过 1500 个时自动调用。
- 二进制、镜像、SDK 没有变化。

新 920B 演练，模拟首次上机（旧的 `/fc-gsd/`、基线、`site.env` 先挪到备份目录）：

| 项目 | 结果 |
|---|---|
| `all --new` | 13 步全部 PASS，约 10 分钟：克隆环境并装 SDK 9 秒、切到进程级 61 秒、建 `base-criu` 11 秒、验收 204 秒、压测 192 秒、切回 57 秒、冒烟 42 秒（PASS 5 / FAIL 0）；网络比对无变化 |
| 有沙箱时 `950.sh criu` | 先起一个 `base` 沙箱，切换时经 API 删掉后切换，用时 1 分 15 秒；`950.sh sandbox` 1 分 41 秒 |
| `950.sh recycle-netns`（沙箱级栈上） | e2b netns 1740 → 175、nat 规则 12185 → 1237，共 1 分 42 秒 |
| `report` | 拒绝打印基线文件；`report stress` 打印各组摘要 |
| `test950.sh quick --new` | 3 步 PASS，3.5 分钟；`test950.sh report` 输出正常 |

### 2026-11-16：并入 e2b-infra，改名 criu-rollback，二进制去调试段

- **位置变化。** 原来的独立离线包 `gsd-950-offline/` 并入仓库，放在 `criu-rollback/deploy/`。其中 `gsd/` 是 `criu-rollback/` 的副本，`sandbox-smoke/` 是 `rollback/scripts/` 的子集，`py-sdk/` 是 SDK 源码，这三部分都不再重复存放。
- **二进制。** orchestrator 和 gsd 换成 `strip -g` 版：
  - orchestrator 的 SHA256 从 `151308ba…` 变为 `063e580e…`，大小 115.6 MB → 96.0 MB；
  - gsd 从 `a38b0db9…` 变为 `421597e5…`；
  - 代码段、数据段和加载段与原版逐字节相同。
  - 三个文件和镜像的 CRIU 层以 xz 压缩入库，合计 32 MB。
- **镜像。** 前两层取仓库的 `ubuntu-22.04-custom.tar.gz`，现场由 `ops/unpack.py` 拼回。拼出的镜像 ID 仍是 `7caae4a6…`，推送到 Harbor 后的仓库摘要也仍是 `abc4b4b7…`，与原离线包完全相同。
- **脚本路径。** 测试脚本改用 `criu-rollback/scripts/`，冒烟改用 `rollback/scripts/950/run.sh`；新增 `unpack` 步骤。

新 920B 演练，从仓库里执行，模拟首次上机：

- 准备：`/fc-gsd/` 和原基线都挪到备份目录，conda 环境重新从 `jll-e2b` 克隆。
- 结果：`bash criu-rollback/deploy/950.sh all` 共 14 步全部 PASS，用时 10 分 38 秒。

| 步骤 | 用时 |
|---|---|
| 展开 | 4 秒 |
| 克隆环境并装 SDK | 7 秒 |
| 切到进程级 | 61 秒 |
| 建模板 | 10 秒 |
| 验收 | 216 秒 |
| 压测 | 203 秒 |
| 切回沙箱级 | 92 秒 |
| 冒烟 | 43 秒（PASS 5 / FAIL 0） |

- guest 自检：gsd SHA256 为 `421597e5…`，`gsd -commit` 为 `259f1a25c`，内核标识不变。
- 宿主网络比对无变化。
- 随后执行 `950.sh gsd`：切换后服务约 10 秒就绪。

### 2026-11-16：一条命令 `all`，920B 演练

新增 `ops/flow.py`、`ops/envs.py`、`ops/probe.py`，`950.sh` 增加 `all`、`smoke`，`gsd`/`sandbox` 改为切换后等到能建沙箱。`stack.py` 有两处改动：

- 已是目标栈时不重启服务；
- 落位时目标文件已存在且内容相同就不重写。重写会换掉正在运行的 orchestrator 的文件，进程路径变成 `(deleted)`，后面的检查会误判。

二进制、镜像、SDK、`gsd/` 测试脚本都没有变化。

演练在新 920B 上进行，模拟首次上机：新目录、挪开原基线、从 `jll-e2b` 新克隆的 conda 环境。

| 项目 | 结果 |
|---|---|
| `950.sh sandbox`（从进程级切回） | 约 1 分钟，切换后 `base` 建沙箱 137 ms，网络比对无变化 |
| `all` 首轮 | 克隆环境、装 SDK、凭据、镜像、基线、落位、切栈、建模板约 2 分钟后进入验收 |
| 验收中途 Ctrl-C | 停在验收这一步，各步状态写入汇总 |
| 续跑 | 演练中暴露并修复了三个问题：落位重写文件；中断留下的测试沙箱（超时 1800 秒）挡住切换，改为先删 `base-gsd` 起的沙箱；刚删的沙箱几秒后才退出，改为切栈前最多等 60 秒。验收或压测失败后，自动删除测试沙箱并回切沙箱级，也在这一过程中实际走过 |
| `all --new`（最终版本，不中断） | 13 步全部 PASS，共 11 分钟：切到进程级 110 秒、建模板 10 秒、验收 217 秒、压测 193 秒、切回 83 秒、冒烟 42 秒（PASS 5 / FAIL 0）；网络比对无变化 |

### 2026-11-06 更新与 920B 全流程实测

### 为什么更新

e2b-infra `870d087` 为了和 AgentENV 同机，把沙箱级的宿主机侧网卡改名为 `e2bv-<n>`，地址池改为 `10.13.0.0/16` / `10.14.0.0/16`。上一版包里的 orchestrator 编译于这次改动之前，网卡名仍是 `veth-<n>`。在已有 AgentENV 占用 `veth-1…` 的机器上，建槽位会报 `File exists`。

本次改动：

| 内容 | 变化 |
|---|---|
| `fc-gsd/orchestrator` | 重编，`VethName()` 改为 `e2bv-%d`，与 `0001` 补丁相同。补丁在 `provenance/orchestrator-e2bv-veth-prefix.patch`。SHA256 由 `818fa667…` 变为 `151308ba…` |
| `ops/stack.py` | `preflight` 增加地址池环境变量检查 |
| gsd、guest 内核、镜像、SDK wheel | 不变 |

地址池不需要改代码：orchestrator 读 `SANDBOXES_HOST_NETWORK_CIDR` / `SANDBOXES_VRT_NETWORK_CIDR`，切栈时沿用 950 原 job 的环境。

950 若已装过上一版包，`/fc-gsd/orchestrator` 内容不同，`install` 会拒绝覆盖。上一版包在 950 未部署过，正常不会遇到；遇到时先确认没有在用，再把旧文件移走。

### 920B 实测结果

环境：鲲鹏 920B，e2b-infra `504f02c` 的 RPM 部署（`build.sh -d -i -s`），无 AgentENV。步骤和本 README 相同，`jll-e2b-gsd` 放在暂存目录，客户端凭据用第 5 节的 `.env` 方式。

| 步骤 | 结果 |
|---|---|
| `verify`、`install-sdk`、`preflight`、镜像导入、`save-baseline`、`install` | 全部通过，共约 7 秒 |
| `gsd` 切换 | 57 秒。`health` 200，`e2bv-` 747 张，`veth-` 0 张，`10.13` 路由 747 条 |
| 建 `base-gsd`（2 vCPU / 2048 MiB） | 9 秒 |
| `acceptance` | PASS：guest 自检；`verify_gsd`；`verify_fixes` 26 PASS / 0 FAIL / 2 SKIP；`verify_multitree --skip V12` 12 项；竞态 30 轮；性能三轮 18 次恢复校验全过；用时约 3.5 分钟 |
| `stress` | PASS：探针七例；`all`、`noworkers` 各 300 轮；`multitree`、`deldense` 各 200 轮；串口和 dmesg 无内核错误；用时约 3 分钟 |
| `sandbox` 回切 | 56 秒，`sandbox_smoke.py` PASS 5 / FAIL 0 / SKIP 0 |
| 再次 `gsd` 并复用 `base-gsd` | 切换 57 秒；`verify_gsd.py base-gsd 8` 通过，8 次 restore 根盘恒定。切换返回后 1 秒的第一次运行报 500，约 1.5 分钟后重跑通过 |

服务端中位耗时（毫秒），和 10-16 同口径。这组数字用于确认这次重编没有带来性能变化，不是 950 的通过阈值：

| 总量 | 全量基点 | 增量 checkpoint | restore |
|---|---:|---:|---:|
| 0 | 21.4 | 9.5 | 8.6 |
| 16 MiB | 32.8 | 25.2 | 15.5 |
| 32 MiB | 36.2 | 24.9 | 19.4 |
| 64 MiB | 48.8 | 40.0 | 30.3 |
| 128 MiB | 71.8 | 64.6 | 51.5 |
| 256 MiB | 120.4 | 117.8 | 94.3 |

没有覆盖的内容：
- 没有在 AgentENV 同机的环境实测。`e2bv-` 避开冲突这一点来自沙箱级部署的同机结论和相同的代码改动；若 950 上有 AgentENV，切换后请按 `single-node-offline-deploy.md` §0.6 核对对方网卡和规则没有变化。
- 950 本身。gsd 的脏页来自 guest 内核的 soft-dirty，orchestrator 不开宿主侧脏页跟踪，不读 `FC_TRACK_DIRTY_PAGES`，也没有 HDBSS 代码，所以 HDBSS 对 gsd 没有影响。
- V12 按第 8 节跳过，G-19、G-36 是预期 SKIP。
