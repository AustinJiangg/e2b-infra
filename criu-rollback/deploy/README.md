# 进程级快照回滚：部署、验收与切换

本目录让已按 [`single-node-offline-deploy.md`](../../single-node-offline-deploy.md) 部署了沙箱级快照回滚的机器，用一条命令切到进程级快照回滚（criu-rollback），跑完验收，再切回来。950 上 `git pull` 拿到最新仓库后即可使用，不需要另外拷贝文件。

- **改动范围。** 进程级产物放在 `/fc-gsd/`，通过 Nomad 切换同一个 orchestrator/template-manager job；切回时恢复**本机保存的原 job**。不重装 RPM，不执行 `build.sh -d/-i/-s`，不覆盖 `/usr/bin`、`/fc-kernels`、`/fc-versions`。
- **产物。** gsd、guest 内核、orchestrator 是 920B 上验收过的二进制，以 xz 压缩入库。orchestrator 和 gsd 用 `strip -g` 去掉了调试段，代码段与验收版逐字节相同，详见第 1 节。
- **镜像。** 前两层取仓库根目录已有的 `ubuntu-22.04-custom.tar.gz`，CRIU 那一层单独入库，现场拼回。拼出的镜像 ID 与验收过的相同。
- **SDK。** 以纯 Python wheel 入库。950 上离线克隆原 `jll-e2b` 为 `jll-e2b-gsd`，只在新环境里替换 SDK。
- **验证状态。** 2026-11-06 起已在 920B 上多次完整走过，记录见第 14 节。950 的实际兼容性、切换与验收尚未执行。950 首轮结果是它自己的基线，不能用 920B 的耗时当通过阈值。

下面的命令都在 **e2b-infra 仓库根目录**以 root 执行。

## 0. 最简用法

**首次：一条命令。**

```bash
sudo -i
cd /e2b-infra仓库所在目录
git pull
bash criu-rollback/deploy/950.sh all
```

它依次完成第 2～10 节的全部步骤，每一步打印进度，任何一步失败就停下并说明原因：

1. 校验部署目录；
2. 把三个运行文件和镜像展开到 `criu-rollback/deploy/.work/`，逐项核对 SHA256 和镜像 ID；
3. 没有 `jll-e2b-gsd` 就从 `jll-e2b` 离线克隆，再装 SDK；
4. 检查客户端凭据和 API；
5. 导入镜像并推送到 Harbor；
6. 预检，保存本机沙箱级基线；
7. 落位三个运行文件；
8. 切到进程级，切换前后各抓一次宿主网络配置做比对；
9. 建 `base-gsd`，自动等到服务能建沙箱；
10. 验收，然后压测；
11. 切回沙箱级，等原模板能建沙箱，跑冒烟；
12. 把结果打成 `criu-rollback/deploy/results/<时间>-all.tar.gz`。

920B 上全程约 11 分钟，不需要手动 `conda activate`，也不需要估等待时间。

跑之前要具备的条件：

- 950 上已有原沙箱级的 conda 环境 `jll-e2b`，conda 装在常见位置（`/root/miniforge3`、`/root/miniconda3` 等）。位置特殊时在 `criu-rollback/deploy/ops/site.env` 写 `CONDA_EXE=`。
- 客户端凭据：默认读 root 的 `~/.e2b/config.json`，没有时按第 5 节放 `criu-rollback/scripts/.env`。
- Harbor：没登录过的话，先执行一次 `docker login harbor:443`。950 没有 Docker、镜像在别的机器导入时，在 `ops/site.env` 写 `GSD_SKIP_IMAGE_IMPORT=1`。
- 部署不是默认形态（job 名、端口等）时，先按第 2 节准备 `ops/site.env`。
- 没有人在用这台机器建沙箱或模板。

**中途停下怎么办。** 按 Ctrl-C、断网或某一步失败，都只要处理完原因后再执行同一条 `bash criu-rollback/deploy/950.sh all`。它会从停下的那一步继续，已经做完的镜像导入、验收、压测、冒烟不会重做。

- 验收或压测失败时，脚本先删掉由 `base-gsd` 起的测试沙箱，再自动切回沙箱级，950 不会停在一半的状态。
- 只删 `base-gsd` 起的沙箱，不碰其他沙箱。
- 上一轮全部完成后再执行 `all`，会开始新的一轮；想放弃未完成的那一轮重来，加 `--new`。

**结果回传。** 只需要拷回最后打印的那个 `results/<时间>-all.tar.gz`。里面有验收、压测、冒烟的全部结果，以及网络比对 `netcheck.txt`。不含凭据。

**以后日常切换：**

```bash
bash criu-rollback/deploy/950.sh gsd
```
```bash
bash criu-rollback/deploy/950.sh sandbox
```

- 已经是目标栈时不重启服务。
- 切换后等到能建沙箱才返回。
- 切到进程级时，`base-gsd` 不在或建不出沙箱就自动重建。
- 每次都比对切换前后宿主上非 e2b 的网卡、路由、策略路由和 iptables 规则。

还有沙箱在运行时，这两条命令拒绝切换，不会删除任何沙箱。

客户端仍然要用对应的 conda 环境：进程级用 `jll-e2b-gsd`，沙箱级用 `jll-e2b`。

**同机有 AgentENV 时。** 看输出里的“宿主网络比对”一行。“无变化”表示 AgentENV 的网卡和规则没有被动过；有变化时逐条核对 `netcheck.txt`。比对排除了 e2b 自己的 `e2bv-` 网卡、`ns-` 命名空间和 `10.13`/`10.14` 地址池。

**以后 `git pull` 带来新的二进制时。** `all` 和 `gsd` 会重新展开并核对。`/fc-gsd/` 里已有内容不同的旧文件时，落位步骤拒绝覆盖，要先确认旧文件没有在用，再移走。

**每次重启 orchestrator 都会留下一批网络命名空间**（约 300 个），`all` 会重启两次。积累到上千个时，按 `single-node-offline-deploy.md` 用 `build.sh --recycle-netns` 清理。

第 1～13 节是逐步说明，`all` 出问题时对照着查。

## 1. 目录里有什么

```text
criu-rollback/deploy/
  950.sh                            唯一入口：all（首次全流程）、gsd/sandbox（日常切换）及各单步
  README.md                         本操作说明
  manifest.json                     来源、版本、镜像组成，三个运行文件解压后的 SHA256
  SHA256SUMS                        本目录入库文件的校验清单
  artifacts/
    orchestrator.xz  gsd.xz  vmlinux.bin.xz
    image/criu421-layer.tar.xz      镜像的 CRIU 层；配置、清单等小文件同目录
  sdk/
    e2b-2.19.0+gsd.dcca61266-py3-none-any.whl  可离线安装的进程级 SDK
    test_gsd_headers.py             SDK 自带的鉴权头测试，安装后离线执行
  ops/
    unpack.py                       展开运行文件、拼镜像并核对
    flow.py                         all 与日常切换的流程：续跑、就绪等待、模板检查、失败回切、网络比对
    stack.py                        保存基线、预检、落位、切换与恢复
    envs.py                         定位 conda 与两个客户端环境，用环境自己的解释器运行，不需要激活
    paths.py                        本目录与仓库内各位置的定义
    probe.py                        查询模板、试建沙箱、清理本流程的测试沙箱
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
| 镜像 | `ubuntu:22.04-custom-criu421`，arm64，CRIU 4.2.1，image ID `7caae4a62095a4371cbef9d4cf31a6734463f6cda976d34d95f49f9ae78a96ab` |
| Python | 使用 950 本机 `jll-e2b` 克隆出的 `jll-e2b-gsd`，保留原 Python 版本；不携带 Python 环境 |
| SDK wheel | `2.19.0+gsd.dcca61266`，源码版本 2.19.0 加本地 GSD 标识；通用 `py3-none-any` 包 |

SDK 与二进制的提交号不同是有意的：`dcca61266` 只是在发布版之后修复 SDK 给 gsd 转发流量 token，二进制不变。原手册全量实机验收严格覆盖 `259f1a25c`，不能把那份结果说成最后一个 SDK 提交的全量实测；到 950 后须跑验收。

`overlay-init`、`gsd.service` 已嵌入 orchestrator，无需单独安装。Firecracker、宿主内核、原沙箱级二进制和模板继续使用机器上已有的内容。发布内核的文件名是 `vmlinux.bin`，其父目录下面没有 `arm64/`。

## 2. 打开 root 终端，校验

下面所有现场命令都在 **950** 执行；需要向 Harbor 推镜像时，也可以在同一隔离网络中能访问该 Harbor 的 arm64 机器完成第 4 节。

```bash
sudo -i
cd /e2b-infra仓库所在目录
bash criu-rollback/deploy/950.sh verify
bash criu-rollback/deploy/950.sh unpack
```

必须看到 `部署目录 SHA256 全部通过` 和 `展开完成`（或 `已展开，核对通过`）。有任何文件缺失或校验不符，停止，不部署；先 `git status` 确认仓库没有被改动，不要在现场重新生成 `SHA256SUMS`。打开 root 终端后再激活 conda；`sudo -i` 会重置之前的 shell 环境。

若不是默认部署，先执行：

```bash
cp criu-rollback/deploy/ops/site.env.example criu-rollback/deploy/ops/site.env
# 编辑 site.env：job 名、端口、状态目录、服务端环境文件位置
```

默认值是 `template-manager-system`、5008、`/var/lib/gsd-950-offline`、`/opt/e2b-infra/.env`。状态目录名沿用最初的离线包名，已保存的基线继续有效。脚本会查找唯一对应的 raw_exec task，不要求它必须位于 TaskGroups[0].Tasks[0]。它只处理 `ORCHESTRATOR_SERVICES=orchestrator,template-manager` 的合并部署；其他部署形态会拒绝执行，不能强行继续。

## 3. 克隆 conda 环境，离线安装 GSD SDK，再预检

第 3～10 节都已包含在 `bash criu-rollback/deploy/950.sh all` 里，下面是逐步说明。单步执行时，`install-sdk`、`acceptance`、`stress`、`smoke` 会自动使用对应的 conda 环境，不需要先激活。

两套 SDK 分别安装在两个环境中：

| conda 环境 | 用途 |
|---|---|
| `jll-e2b` | 950 原沙箱级 SDK，保留原样 |
| `jll-e2b-gsd` | 克隆 `jll-e2b`，再安装 `sdk/` 下的进程级 SDK |

先确认当前 root shell 可以使用 conda。若未初始化，先 `source <conda安装目录>/etc/profile.d/conda.sh`，例如 `/root/miniforge3/etc/profile.d/conda.sh`。以下克隆命令只在第一次创建环境时执行：

```bash
conda deactivate
conda create --offline --copy --name jll-e2b-gsd --clone jll-e2b --yes
conda activate jll-e2b-gsd
unset PYTHONPATH PYTHONHOME

# 离线安装，只替换 jll-e2b-gsd 中的 e2b SDK
bash criu-rollback/deploy/950.sh install-sdk
python -c 'import sys, e2b; print(sys.executable); print(e2b.__file__)'
bash criu-rollback/deploy/950.sh preflight
```

`--offline` 禁止联网取包，`--copy` 使用文件副本。克隆继承原环境的 Python、pip 和第三方依赖；不单独搬运 920B 的 Python 环境。若 `jll-e2b-gsd` 已存在，不重复克隆，直接激活它后执行安装和检查。

安装脚本使用 wheel 执行 `python -m pip --isolated install --no-index --no-deps --no-cache-dir --force-reinstall ...`，不从公网下载、不调用构建后端、不更改依赖。它会先核对依赖版本；缺依赖时在替换 SDK 前停下并列出缺项，不能联网安装或从另一个 Python 版本复制二进制依赖。

脚本只允许在激活的 `jll-e2b-gsd` 中安装，拒绝在 `jll-e2b`、base、系统 Python 中运行。安装后会检查 `e2b`、`e2b_connect` 都从新环境的 site-packages 加载，并执行 7 项 SDK 鉴权头测试。输出应包括 `GSD SDK 与依赖检查 PASS`、`SDK header tests: PASS 7 / FAIL 0`。

**后续普通脚本、建模板、验收、压测都直接使用激活后的 `python`，不用 `PYTHONPATH`，也没有 `python/run` 包装器。** 原始手册里的 `PYTHONPATH=<代码仓>/py-sdk` 是源码运行方式，部署以本 README 的安装方式为准。

`preflight` 不修改部署。它检查包校验、aarch64、KVM、命令是否齐全、orchestrator 动态库、gsd 提交号、Nomad job 结构、沙箱地址池环境变量、运行进程和 health、活沙箱，以及当前 `jll-e2b-gsd` 的 SDK/依赖。

**orchestrator 是动态链接的，需要 glibc 2.38 对应符号。** 若出现动态库缺失、`GLIBC_2.38 not found` 或权限错误，先处理兼容性或挂载执行权限，不要替换系统 glibc。gsd 自身是静态编译。

若预检提示有 Firecracker，先让使用机器的人结束沙箱，并等待模板构建任务结束。预检通过后到切换完成期间，不要让其他使用者新建沙箱或模板。

## 4. 将镜像导入 950 使用的 Harbor

镜像由第 2 节的展开步骤拼出，全部层都来自仓库，不需要访问公网。920B 上检查过：CRIU 4.2.1、curl、python3，以及 `systemd systemd-sysv openssh-server sudo chrony linuxptp socat ca-certificates` 均在位。缺包会导致模板 provision 执行 apt；本目录镜像已具备所需包。

在有 Docker、能访问 950 Harbor 的 arm64 机器上执行；通常可以直接在 950 执行：

```bash
# 使用 950 已有的仓库地址和证书配置；默认引用如下。
export GSD_TEMPLATE_IMAGE=harbor:443/e2b-orchestration/ubuntu:22.04-custom-criu421

# 若当前用户还未登录该仓库，先 docker login harbor:443，交互输入 950 的凭据。
bash criu-rollback/deploy/ops/import-image.sh
```

脚本先校验包，再 `docker load`，按确定的 image ID 在不联网的临时容器中检查 CRIU、curl、python3 和预装包，最后 tag/push 到 `$GSD_TEMPLATE_IMAGE`。

如果仓库名称不同，改上面的引用，建模板时仍用完全相同的引用。确认 `harbor` 在 **950** 能解析，模板构建端能够访问仓库并通过认证。推送成功只证明导入端可访问；第 7 节模板构建成功才完成构建端拉取验证。

若 950 没有 Docker，在同一隔离网络里能向其 Harbor 推送的 arm64 机器完成导入即可，不要求因此在 950 安装 Docker。不要只 load 到导入机本地缓存而没有 push。

## 5. 核对客户端凭据

默认使用 950 当前 root 用户的 `~/.e2b/config.json`。它必须是 950 已有部署生成的配置。公共模块还支持环境变量或 `criu-rollback/scripts/.env`。

```bash
python - <<'PY'
import sys
sys.path.insert(0, 'criu-rollback/scripts')
from e2b_env import load_e2b_env
print(load_e2b_env())  # 只打印 API URL、域名、SSL 设置，不打印 key/token
print('客户端凭据加载成功')
PY
```

默认 API 是 `http://localhost:3000`。API 地址、域名或访问方式不同，使用 950 已验证的客户端环境配置。若使用 `.env`，新建 `criu-rollback/scripts/.env`，填下面五项，保存为 600 权限：

```dotenv
E2B_API_URL=http://localhost:3000
E2B_DOMAIN=e2b.app
E2B_HTTP_SSL=false
E2B_ACCESS_TOKEN=填写950自己的token
E2B_API_KEY=填写950自己的key
```

也可以复制 950 已有且有效的客户端 `.env` 到这个位置。`/opt/e2b-infra/.env` 是服务端配置，通常不包含完整客户端凭据，不能据此认定 key/token 就绪。不要复制 920B 的 job JSON、环境文件或登录配置；本离线包没有携带它们。

## 6. 保存 950 基线、落位、切换

必须先保存本机原沙箱级 job，再做部署：

```bash
bash criu-rollback/deploy/950.sh save-baseline
bash criu-rollback/deploy/950.sh install
bash criu-rollback/deploy/950.sh gsd
bash criu-rollback/deploy/950.sh status
```

`save-baseline` 要求当前原服务健康、无活沙箱，记录原 job、实际二进制路径与 SHA256、相关进程环境。默认保存在 `/var/lib/gsd-950-offline/`，目录 700、job 文件 600。**文件含 950 密钥，只保存在该机，不随结果回传。** 已有基线时拒绝覆盖；重复切换直接执行 `gsd` 或 `sandbox`。

`install` 再跑只读预检，要求基线存在，随后只安装：

```text
/fc-gsd/gsd
/fc-gsd/orchestrator
/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin
```

若这些位置已有内容不同的文件，会停下而不覆盖；先核对现场那一套的用途。相同内容可以重复安装。不要把进程级内核覆盖到 `/fc-kernels`。

`gsd` 从保存的基线生成新 job，只替换选中 task 的二进制路径，设置 `HOST_KERNELS_DIR=/fc-gsd/kernels`、`GSD_ORCH_SHA256`，去掉 `HOST_GSD_PATH` 以使用默认 `/fc-gsd/gsd`，保留该机原有其余配置。

切换会先停旧 job，等待退出后提交新 job，避免固定端口冲突；然后核对 **实际进程** 的路径、SHA256、环境和 health。只看到 `nomad job run` 成功不算完成。最长等待旧进程退出约 120 秒、新服务就绪约 360 秒，并额外等 20 秒让 API 重连。失败保留基线和生成的 job，按第 11 节恢复。

成功时 `status` 应显示：

```text
exe: /fc-gsd/orchestrator
HOST_GSD_PATH: null                 # 未设，默认 /fc-gsd/gsd
HOST_KERNELS_DIR: /fc-gsd/kernels
GSD_ORCH_SHA256: 与交付 orchestrator 一致
health: 200
```

切换返回后先等 1～2 分钟再建沙箱。920B 上切换返回后 1 秒建沙箱报过 `500: Failed to place sandbox`，约 1.5 分钟后同一命令正常。

进程级栈运行期间，**所有新建模板**都会注入 gsd 和 overlay-init。此时不要构建其他使用者的普通模板。

## 7. 在进程级栈下新建模板

确认已经切到 gsd，再执行：

```bash
python criu-rollback/scripts/build_template.py \
  --alias base-gsd --cpu 2 --memory 2048 \
  --image "$GSD_TEMPLATE_IMAGE"
```

`GSD_TEMPLATE_IMAGE` 必须与第 4 节一致；若换过终端，先重新 export。模板名 `base-gsd` 与原沙箱级 `base` 分开，不要覆盖原模板。

默认不使用缓存，不加 `--use-cache`。末行应为 `template 'base-gsd' built in ...s`。构建失败先查错误，不要直接进入验收。

模板固定了构建时的 gsd、guest 内核和 overlay-init；以后换任意一项都要重新构建模板。不要拿沙箱级模板直接测 gsd checkpoint。

## 8. 运行必跑验收

```bash
bash criu-rollback/deploy/950.sh acceptance
```

默认测试 `base-gsd`，输出目录自动创建在 `results/<时间>-acceptance/`。入口按下面顺序执行，遇到非零返回码或内容判据失败就停止，并写 `summary.json`：

| 顺序 | 内容 | 判据 |
|---|---|---|
| 1 | `ops/guest_check.py` | guest 内 gsd 提交号/SHA256、内核构建标识、THP never、RCU=1、服务 active、health ok、CRIU 4.2.1、根目录和 /tmp 权限、/tmp 位于 overlay、2 vCPU |
| 2 | `acceptance/verify_gsd.py base-gsd 8` | 8 次 restore 全部 ok，根盘增量 +0，末行根盘恒定 |
| 3 | `acceptance/verify_fixes.py` | 26 PASS / 0 FAIL / 2 SKIP；SKIP 只能是 G-19、G-36；无 BAD 和 dmesg 可疑行 |
| 4 | `acceptance/verify_multitree.py --skip V12` | 12 项全 PASS；无 BAD 和可疑行，记录慢调用 |
| 5 | `acceptance/verify_clear_refs_race.py` | 完整 30 轮、每轮漏页 0、state=T、sha 相同、writer 未卡死 |
| 6 | `acceptance/demo_checkpoint_perf.py` 三次 | 每次 6 行恢复内容全正确，SDK/journal 最大差 0.000 ms |

性能脚本采用 2026-10-16 的独立负载分档（JSON `schema_version=2`、`measurement_mode=per-tier-size`）：
每档新建沙箱，额外负载内存为 0 / 12 / 24 / 48 / 96 / 192 MiB，文件为 0 / 4 / 8 / 16 / 32 / 64 MiB。
建立本档全量基点后改写本档负载，测增量 create，再恢复本档基点并校验全部内存 SHA256 和文件内容。
全量基点单列，输出实际 RSS；0 档仍有 Python 运行时开销。每档都是新沙箱第一次 restore。
旧手册中固定 192 MiB、连续建链后的六档结果属于历史口径，不能与新结果混合统计。
新版脚本于 2026-10-16 在新 920B 上完成三轮六档实测，18 次恢复的完整内存和文件校验全部通过，SDK / journal 分段一致。
本轮还修复了 RPC 返回后 journal 尚不可读时的采集问题，重试在计时窗口之外进行。950 上尚未运行新版进程级脚本；920B 的结果不能直接作为 950 的通过阈值。

原始 `verify_gsd.py` 与竞态脚本可能在内容失败时仍返回 0；新版性能脚本失败返回非零。
入口会额外读日志和 JSON，核对六档实际负载、恢复目标与内容校验，避免误判通过。具体命令也会写进结果 `summary.json`。

正常末行应为 `判定: PASS`。这里只判功能正确性，不给性能达标线。V12 需要另一份仅版本不同的进程级模板作对照，首次部署跳过；原沙箱级模板不能充当这个对照。

若需自定义结果位置：

```bash
bash criu-rollback/deploy/950.sh acceptance --out /持久磁盘/950-gsd-acceptance-第一轮
```

输出目录必须尚不存在，以免竞态 JSONL 追加旧结果。

## 9. 首次部署再跑探针和四组压测

必跑验收通过后执行：

```bash
bash criu-rollback/deploy/950.sh stress
```

入口先跑 `probe_whiteout_bug.py` 的七个用例，再顺序跑四组：

| 组 | 脚本与参数 |
|---|---|
| all | `stress_overlay_ckpt.py --mode stress --rounds 300 --workers 6` |
| noworkers | `stress_overlay_ckpt.py --mode stress --rounds 300 --workers 0` |
| multitree | `stress_multitree.py --mode multitree --rounds 200` |
| deldense | `stress_delete_dense.py --mode deldense --rounds 200` |

四组都通过原始 `run_stress_watch.sh` 执行，停滞阈值为 150 秒。现场 job 名、服务端环境文件沿用 `ops/site.env`；自定义了这些值，压测同样会传给串口取证脚本。

通过要求包括：看门狗 `DONE rc=0`；串口证据存在且无内核错误；无 `*-create-fail-*`；dmesg 无新错误；worker 只允许 ENOENT/ESTALE；心跳未过期；gsd 只允许 `create:resource_exhausted`。另外两个专项组自身 verdict 也必须 PASS。任何一组失败就停止后续组。

若取不到串口日志，入口不会把证据缺失算通过。先核对现场 Nomad token、job 名、alloc 日志和串口日志格式，再重跑；缺证据不等同于已发现内核错误。

本次部署现有发布内核，不需要额外准备调试内核。以后修改补丁时，按 `criu-rollback/docs/18-testing-and-debug-kernel.md` 再跑调试内核验证。

## 10. 切回沙箱级

先等待验收/压测结束，确认测试沙箱和构建任务都退出，然后：

```bash
bash criu-rollback/deploy/950.sh sandbox
bash criu-rollback/deploy/950.sh status
```

`sandbox` 使用第 6 节保存的 **950 原始 job**，恢复原二进制路径及其余配置，不用 920B 的回切脚本。恢复前会检查原二进制仍是保存时的 SHA256。它也会等待新进程、核对 SHA256 和三个相关环境变量，并确认 health 200。

`HOST_KERNELS_DIR` 恢复原值，原来没有就恢复为未设置；`HOST_GSD_PATH`、`GSD_ORCH_SHA256` 同样恢复原来的状态。仅设置 `HOST_GSD_PATH=off` 不能替代恢复原沙箱级 orchestrator。

回切服务端后，退出进程级 conda 环境并激活原沙箱级环境，再做冒烟：

```bash
conda deactivate
conda activate jll-e2b
unset PYTHONPATH PYTHONHOME
python -c 'import sys, e2b; print(sys.executable); print(e2b.__file__)'
python criu-rollback/deploy/ops/sandbox_smoke.py --template base
```

模板不是 `base` 就换成原沙箱级别名。冒烟入口要求当前环境是 `jll-e2b`，直接使用它的解释器和 SDK。客户端凭据仍按第 5 节读取，只通过子进程环境传递，不输出、不另写文件。SDK 覆盖层自检使用原部署的 `/opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py`。

下次使用进程级时：

```bash
conda deactivate
conda activate jll-e2b-gsd
unset PYTHONPATH PYTHONHOME
bash criu-rollback/deploy/950.sh gsd
bash criu-rollback/deploy/950.sh status
```

激活 conda 选择的是客户端 SDK；`gsd`/`sandbox` 切换的是服务端，两步都要执行。首次安装完成后，正常切换环境不需要重新安装 SDK。

冒烟用仓库里的 `rollback/scripts/950/run.sh smoke`；需要 func/perf/hdbss/long 档时直接用该入口。完整冒烟应包括 SDK 自检、能力信息和 checkpoint/restore 正确性；有 FAIL 或未解释的 SKIP 先查原因。仅看 health 200 不足以确认 SDK 与模板也回到了原环境。

回切后 `/fc-gsd/` 和 `base-gsd` 可以保留；原普通模板同样保留。原沙箱级 checkpoint 与 gsd checkpoint 不能互相使用。以后重复切到进程级时，无需再次 save-baseline/install；执行 `gsd`，确认原 `base-gsd` 对应当前产物后再验收。

## 11. 失败时怎么处理

| 现象 | 下一步 |
|---|---|
| 包校验失败 | 重拷原包，不要改校验清单 |
| 动态库/GLIBC 不兼容 | 停在切栈前；回传预检错误，不改系统 glibc |
| SDK 安装提示环境不对 | 激活 `jll-e2b-gsd` 后用该环境的 `python` 安装 |
| 克隆或 SDK 安装提示缺离线依赖 | 保留完整报错；缺项补齐前不切栈，原 `jll-e2b` 不受影响 |
| 客户端 key/token 缺失 | 第 5 节使用 950 有效凭据，不重装服务 |
| save-baseline 提示已有基线 | 正常重复切换直接用 gsd/sandbox；不要删除或覆盖基线 |
| job 结构不同 | 保留现状，只读核对任务结构；不要手改脚本跳过保护 |
| 切换后端口/health 不对 | 查 `nomad job status` 和 alloc 日志；必要时执行 `bash criu-rollback/deploy/950.sh sandbox` |
| 模板构建失败 | 查镜像引用、Harbor 解析/认证、gsd health、CRIU 版本；不要重用失败模板作验收 |
| guest 自检发现旧 gsd/内核 | 确认当前栈和内核根目录后，无缓存重建模板 |
| 功能失败、漏页、内核错误 | 停止后续测试，完整保留本轮结果；先结束本轮沙箱，再按第 10 节恢复 |
| 预检提示缺沙箱地址池环境变量 | 950 部署早于 e2b-infra `870d087`；先升级部署或补齐 job 环境，不要跳过。缺这两项时 orchestrator 回落到 `10.11`/`10.12`，与同机 AgentENV 冲突 |
| 切换后立刻建沙箱报 `500: Failed to place sandbox` | 等 1～2 分钟重试；重试仍失败再查 `/tmp/templates` 下模板文件是否还在，以及 orchestrator 日志 |
| 回切提示原二进制变化 | 停止；核对是否有其他人更新部署，不自动覆盖现场文件 |

若失败后遗留了本轮测试沙箱，先从日志找到**该测试的沙箱 ID**，逐个清理，再回切；不要批量杀其他人的沙箱：

```bash
python - <本轮测试沙箱ID> <<'PY'
import os, sys
sys.path.insert(0, 'criu-rollback/scripts')
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox
print(Sandbox.kill(sys.argv[1]))
PY
```

若切换中断，950 基线保存在独立持久目录，重新登录后仍可在仓库根目录运行 `bash criu-rollback/deploy/950.sh sandbox`。脚本不提供强杀沙箱、强制覆盖或 purge Nomad 的选项。

## 12. 结果回传

用 `all` 时只需拷回最后打印的 `results/<时间>-all.tar.gz`，它包含下面说的全部内容、`all.log`（每步的进度与结论）、建模板日志、沙箱级冒烟结果、网络比对 `netcheck.txt`；单步执行时按本节拷目录。

每轮结果位于 `results/`，含 `summary.json`、三个运行文件的 `deliverables.sha256`、每步日志、功能 JSON/JSONL、性能三轮结果、压测子目录。`summary.json` 会明确标记 PASS/FAIL，失败时列出停止原因。

把验收和压测两个结果目录一起拷回；失败压测保留整个目录，包含 `console.txt`、dmesg、操作日志。另记下机器、起止时间、模板 ID、vCPU/内存、未跑项目、回切冒烟结果。成功后再据此更新原手册中的 950 状态。

回传结果时不要携带 `criu-rollback/scripts/.env`、`/var/lib/gsd-950-offline`、Nomad job JSON 或现场登录配置。

## 13. 校验清单与验证范围

`SHA256SUMS` 覆盖本目录入库的文件，不包含运行后新建的 `.work/`、`results/`、`ops/site.env`；新建这些文件不影响校验。展开出的三个运行文件和镜像按 `manifest.json` 另行核对。conda 环境安装在本机 conda 管理的目录，环境内部文件不在校验范围内。

打包方已在 920B 的临时环境实测：从 `jll-e2b` 离线克隆、安装 GSD wheel、7 项 SDK 鉴权头测试、激活切回 `jll-e2b`，并核对原 SDK 文件哈希未改变；临时环境已清理。950 本机的 Python 版本与依赖仍由现场克隆和检查确认。

打包方还已验证：三个二进制哈希；镜像归档和层结构；920B 原镜像的离线依赖；SDK wheel 完整性和源码对应关系；conda 克隆环境的离线安装检查；新增 job 处理单测、脚本语法和包完整性。具体记录见 `provenance/package-validation.json`。

这套运维入口已在 920B 实际切换过服务（第 14 节），没有在 950 切换服务或创建沙箱。只有按第 8、9、10 节完成现场测试之后，才能确认这台 950 的实际运行结果。

## 14. 更新记录与 920B 实测

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
