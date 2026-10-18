# 23 · 部署

## 本章目标

读完本章，你应该能：

1. 列出 gsd 这套栈由哪几个部件组成、各自从代码仓的哪里来、装到机器的哪里；
2. 在不联网的机器上把 gsd 与 orchestrator 编出来，把 guest 内核按固定基线打上补丁编出来，并核对补丁能从基线重放出同一棵源码树；
3. 按运行时布局摆放文件，分清"与整机级回滚共用的机器"和"只跑 gsd 的机器"两种摆法；
4. 用切栈脚本把宿主切到 gsd 栈，知道它改了什么、在什么情况下拒绝执行；
5. 在 gsd 栈下建出模板 `base-gsd`，并说清为什么换内核、换 gsd 之后必须重建模板；
6. 部署后用一段脚本逐项确认 guest 里的内核、gsd、CRIU 与文件系统布局都是预期的。

前两章（[21](21-quickstart.md)、[22](22-semantics-and-limits.md)）面向调用方，从本章起面向部署与运维。本章讲"从代码到一个能用的模板"的全过程；
装好之后有哪些开关、怎么看日志在下一章（[24](24-configuration-and-observability.md)），出了问题按症状查 [25](25-troubleshooting.md)，成套的上机验收在 [26](26-acceptance-runbook.md)。

本章的命令以新 920B（192.168.45.47）上实际做过的为准，代码版本是 `deltabox-gsd@259f1a25c`。950：未部署、未测，见 §7。

---

## 1. 部件清单

gsd 这套栈不改 Firecracker，也不改宿主内核。要部署的是下面六样，其中前三样是宿主上的文件，第四样是镜像仓库里的镜像，第五样随 orchestrator 一起带进去，第六样在客户端。

| 部件 | 源码 | 装到哪里 | 说明 |
|---|---|---|---|
| gsd | `packages/gsd/` | 宿主 `/fc-gsd/gsd`；建模板时被拷进 guest 的 `/usr/bin/gsd` | 纯 Go、静态链接（`CGO_ENABLED=0`）的 arm64 二进制。宿主上的这个文件只在建模板时被读取，宿主自己不运行它 |
| gsd 版 orchestrator | `packages/orchestrator/` | 宿主 `/fc-gsd/orchestrator` | 与上游相比多了"建模板时注入 gsd"的逻辑（[05](05-guest-boot-and-injection.md)）。需要 CGO（`CGO_ENABLED=1`） |
| guest 内核 | `fc-kernels/`（构建脚本、config、补丁） | 宿主 `/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin` | 上游 6.1.158 的固定基线加三个补丁。arm64 的 `Image`，文件名按 orchestrator 的约定叫 `vmlinux.bin` |
| guest 基础镜像 | `packages/gsd/docker/Dockerfile.criu421` | 镜像仓库，例如 `…/e2b-orchestration/ubuntu:22.04-custom-criu421` | 在 `ubuntu:22.04-custom` 上加源码编译的 CRIU 4.2.1（`/usr/sbin/criu`） |
| 启动脚本与服务单元 | `packages/orchestrator/internal/template/build/core/rootfs/gsdfiles/` | 嵌在 orchestrator 二进制里，建模板时写进 guest 的 `/usr/bin/overlay-init` 与 `/etc/systemd/system/gsd.service` | 不需要单独部署 |
| Python SDK | `py-sdk/` | 客户端的解释器 | 见 [21](21-quickstart.md) |

发布时必须逐项记录这六样的源码版本与构建哈希。10-08 的完整验收以 `259f1a25c` 为基准；10-16 重用同一套 gsd、orchestrator 与内核，客户端 SDK 使用后续 `dcca61266`，它只补充入口流量令牌的传递，未修改 gsd 协议或内核接口。其他版本组合不能据此推断兼容。gsd 与内核补丁之间有接口约定（切换文件系统层的 ioctl），orchestrator 嵌着的启动脚本决定了 gsd 的启动参数，SDK 与 gsd 之间是同一份线协议；混用不同提交的部件，轻则功能缺失，重则在第一次 checkpoint 时才报错。

**只支持 arm64 的 guest。** x86_64 的 guest 内核 config 没有打开 CRIU 需要的选项，脏页跟踪的补丁也只做了 arm64（[03](03-goals-and-tradeoffs.md)）。

---

## 2. 构建

### 2.1 gsd 与 orchestrator（离线）

两个二进制都用 Go 编，下面是在不联网的机器上的做法。前提是机器上有一份匹配的 Go 工具链（新 920B 上用的是 go1.25.4）和一份预先备好的模块缓存。

```bash
W=<代码仓工作区>                 # 检出在 deltabox-gsd@259f1a25c，工作区干净
GO=<go 工具链>/bin/go
OUT=<产物目录>
V=259f1a25c                      # 嵌进二进制的提交号，与工作区的 HEAD 一致

export GOTOOLCHAIN=local GOWORK=off GOPROXY=off GOFLAGS=-mod=readonly
export GOMODCACHE=<模块缓存目录> GOCACHE=<构建缓存目录>
$GO version

# gsd：先 vet 和带竞态检测的单测，全过了再编
cd $W/packages/gsd
$GO vet ./... \
  && $GO test -race -count=1 ./... \
  && CGO_ENABLED=0 $GO build -ldflags "-X main.commitSHA=$V" -o $OUT/gsd .

# orchestrator：编完跑与 gsd 注入相关的两组单测
cd $W/packages/orchestrator
CGO_ENABLED=1 $GO build -ldflags "-X=main.commitSHA=$V" -o $OUT/orchestrator . \
  && $GO test -count=1 ./internal/template/... ./internal/cfg/...

sha256sum $OUT/gsd $OUT/orchestrator
$OUT/gsd -commit                 # 应输出 259f1a25c
```

四个环境变量各管一件事，合起来保证"这次构建只用到本机已有的东西"：

- `GOTOOLCHAIN=local`：只用本机这份工具链，不按 `go.mod` 的要求去下载别的版本；
- `GOWORK=off`：不使用仓库根的工作区文件，每个包按自己的 `go.mod` 解析依赖；
- `GOPROXY=off`：模块缓存里没有的依赖直接报错，而不是去联网；
- `GOFLAGS=-mod=readonly`：不允许构建过程改写 `go.mod` / `go.sum`。

`-ldflags "-X main.commitSHA=…"` 把提交号嵌进二进制。部署之后，`gsd -commit` 的输出是判断"guest 里跑的到底是哪一版"的唯一可靠依据（§6），所以这个值一定要和工作区的提交一致。
先跑 vet 和单测再编，是因为这两个二进制一旦进了模板，要到做 checkpoint 时才会暴露问题，而重建模板、重跑验证的代价远高于在这里多花一分钟。

新 920B 上的具体路径可以作为参照：Go 工具链取自 RPM 构建树里解出来的那一份（`<e2b-repo>/tmp/rpmbuild/BUILD/go-toolchain/go/bin/go`），`GOMODCACHE` 与 `GOCACHE` 放在构建工作区下的 `gomodcache/` 与 `gocache/`。
别的机器按自己的路径替换即可，没有任何一处依赖这些路径本身。2026-10-08 那次构建产物的 sha256 记在 [20](20-results-and-gaps.md) 的条件标签里。

编好之后放到运行时位置（布局见 §3）：

```bash
install -D -m 755 $OUT/gsd          /fc-gsd/gsd
install -D -m 755 $OUT/orchestrator /fc-gsd/orchestrator
```

### 2.2 guest 内核

内核的三样输入都在代码仓的 `fc-kernels/` 下：

- **基线**：`kernel_base_tags.txt` 把 6.1.158 固定在上游 amazonlinux/linux 的 tag `microvm-kernel-6.1.158-15.288.amzn2023`。补丁是对着这个 tag 做的；不固定的话，上游出一个新 tag 就会悄悄换掉补丁脚下的基线。
- **补丁**：`patches/6.1.158/` 下按文件名顺序应用的三个——`0001-virtio_balloon-Support-wait-on-ACK-for-hinting.patch`（上游带来的，与 gsd 无关）、`0002-overlayfs-hot-switch-checkpoint.patch`（文件系统热切层与 arm64 脏页跟踪）、`0003-overlayfs-hot-switch-fixes.patch`（对 0002 的全部修复）。
- **config**：`configs/arm64/6.1.158.config`，其中 `CONFIG_CHECKPOINT_RESTORE=y`、`CONFIG_MEM_SOFT_DIRTY=y`、`CONFIG_OVERLAY_FS=y`。

联网的 Debian / Ubuntu 机器上直接用仓库里的脚本：

```bash
cd $W/fc-kernels
./build.sh 6.1.158 arm64
# 产物：builds/vmlinux-6.1.158/arm64/vmlinux.bin
```

脚本会装依赖、克隆内核源码、检出固定的 tag、依次 `git apply` 三个补丁、`make olddefconfig`、编出 `Image`。

不联网的机器（新 920B 就是这种情况）手工做同样的几步。前提是本机已有一份带这个 tag 的内核源码克隆和 arm64 的编译环境：

```bash
cd <内核源码克隆>
git checkout -f microvm-kernel-6.1.158-15.288.amzn2023
for p in $W/fc-kernels/patches/6.1.158/*.patch; do
  git apply --check "$p" && git apply "$p" || { echo "补丁应用失败: $p"; break; }
done
cp $W/fc-kernels/configs/arm64/6.1.158.config .config
make ARCH=arm64 olddefconfig
make ARCH=arm64 Image -j"$(nproc)"
```

无论哪种做法，最后都要把产物放到 orchestrator 找内核的位置。orchestrator 读的是 `<内核根目录>/vmlinux-6.1.158/vmlinux.bin`，**中间没有架构子目录**，所以 `build.sh` 的产物不能整个目录原样拷过去：

```bash
install -D -m 644 arch/arm64/boot/Image /fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin
# 或者，用 build.sh 编的：
install -D -m 644 $W/fc-kernels/builds/vmlinux-6.1.158/arm64/vmlinux.bin /fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin
sha256sum /fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin
```

同时把这次用的 `.config` 留一份在旁边备查。这样编出的内核版本串是 `6.1.158+`。

### 2.3 核对补丁能从基线重放

仓库里的三个补丁文件是内核改动的唯一正本。要确认"手上这个内核的源码树"确实等于"基线加这三个补丁"，不靠比对补丁文本，而是从基线重放一遍、比较 git 的树哈希：

```bash
cd <内核源码克隆>
git worktree add --detach /tmp/applycheck microvm-kernel-6.1.158-15.288.amzn2023
cd /tmp/applycheck
for p in $W/fc-kernels/patches/6.1.158/000[123]-*.patch; do
  sha256sum "$p"; git apply --index "$p" && echo "  apply OK"
done
git write-tree
cd - && git worktree remove /tmp/applycheck
```

`git write-tree` 输出的是应用完三个补丁之后整棵源码树的哈希，它只取决于文件内容，与提交历史、提交说明无关。
2026-10-08 在新 920B 上对 `259f1a25c` 里的三个补丁做这一步，三个都 `apply OK`，得到的树哈希是 `744cbce67cef413e4838035c8a2788f97e5e9d3a`，与编出发布内核的那棵源码树相同。
以后只要补丁文件没变，任何人在任何机器上重放都应当得到同一个哈希；得到别的值，说明基线 tag 不对，或者补丁文件被改过。三个补丁文件各自的 sha256 记在 [20](20-results-and-gaps.md)。

调试用的内核（打开 KASAN、lockdep 等检查）用同一棵源码树、另一份 config 编，做法与注意事项在 [19](19-testing-and-debug-kernel.md)，config 片段随手册附带在 `scripts/kernel/`。

### 2.4 guest 基础镜像

模板从一个容器镜像建出来。gsd 要求镜像里有 4.x 的 CRIU，并且位置固定在 `/usr/sbin/criu`（gsd 写死了这个路径）。仓库里的 `Dockerfile.criu421` 在 `ubuntu:22.04-custom` 之上从源码编译 CRIU 4.2.1：

```bash
docker build -f $W/packages/gsd/docker/Dockerfile.criu421 \
  -t <镜像仓库>/e2b-orchestration/ubuntu:22.04-custom-criu421 <一个空目录>
docker push <镜像仓库>/e2b-orchestration/ubuntu:22.04-custom-criu421
```

这一步需要能访问 apt 源和 CRIU 源码包（下载地址是构建参数 `CRIU_TARBALL`，可以用 `--build-arg` 换成内网镜像；需要代理时同样只用 `--build-arg` 传，不会留在镜像里）。镜像建一次即可，换 gsd、换内核都不需要重建它。

有两点和"随便找个带 criu 的镜像"不同。第一，建模板时的 provision 步骤**不再安装**发行版自带的 criu 包，免得把源码编的 4.2.1 盖掉，所以镜像里必须自己带好。
第二，建模板的最后会在 guest 里检查 `/usr/sbin/criu --version` 是 4.x，不是则建模板失败（[25](25-troubleshooting.md)）。用自己的业务镜像建模板时，以这个 Dockerfile 为基础往上加东西。

---

## 3. 运行时布局

### 3.1 三个文件

gsd 栈在宿主上只需要三个文件，约定放在 `/fc-gsd/` 下：

```
/fc-gsd/
├── gsd                                   # 建模板时注入 guest 的 gsd
├── orchestrator                          # gsd 版 orchestrator
└── kernels/
    └── vmlinux-6.1.158/
        └── vmlinux.bin                   # 打了补丁的 guest 内核
```

orchestrator 用两个环境变量找另外两样东西：

- `HOST_GSD_PATH`：要注入的 gsd 二进制，**默认值就是 `/fc-gsd/gsd`**。所以按上面的布局摆好之后不需要设它，建模板时默认就会注入 gsd。不想注入（让这个 orchestrator 建出与上游相同的模板）时设成小写的 `off`。
- `HOST_KERNELS_DIR`：guest 内核的根目录，默认值是 `/fc-kernels`，orchestrator 在它下面按 `vmlinux-<版本>/vmlinux.bin` 找内核。

两个变量的完整取值规则（包括 `OFF` 大写不算关这类细节）在 [24](24-configuration-and-observability.md) 的开关总表里。

### 3.2 两种机器

内核该放哪里，取决于这台机器是否还要跑别的栈：

| 机器 | 内核放哪里 | `HOST_KERNELS_DIR` |
|---|---|---|
| **与整机级回滚共用**（新 920B 是这种）：`/fc-kernels/vmlinux-6.1.158/` 已经被另一套栈的内核占着，两套栈的 guest 内核版本号相同、内容不同 | `/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin` | 切到 gsd 栈时设为 `/fc-gsd/kernels`；切回另一套栈时去掉 |
| **只跑 gsd** | 直接放 `/fc-kernels/vmlinux-6.1.158/vmlinux.bin` | 不用设 |

共用的机器上绝对不要用补丁内核去覆盖 `/fc-kernels` 下原有的文件：两套栈对 guest 内核的要求不同，覆盖之后另一套栈新建的模板会带着错误的内核。

### 3.3 同一时刻只有一套

orchestrator 监听固定端口（新 920B 上是 5008），同一台机器同一时刻只能跑一个，所以 gsd 栈与别的栈是**切换**关系，不是并存关系。由此有一条必须记住的后果：

> **gsd 栈运行期间建的所有模板都会被注入 gsd、启动程序换成 `overlay-init`。** 这与谁发起的构建、模板叫什么名字无关。

共用机器上，切到 gsd 栈之后其他使用者不应再建他们自己的模板；反过来，在别的栈下建出来的模板里没有 gsd。两种模板之间互相派生（以一种模板为基础建另一种）没有处理，不要这样用（[15](15-known-limits.md)）。

---

## 4. 切到 gsd 栈

### 4.1 切栈脚本

新 920B 是 RPM 部署，orchestrator 由 nomad 的 job `template-manager-system` 拉起。把它换成 gsd 版，本质上是提交一份改过的 job 定义。这件事由随手册附带的 `scripts/ops/switch-gsd-new920b.sh` 完成：

```bash
bash scripts/ops/switch-gsd-new920b.sh status   # 只看现状，不改任何东西
bash scripts/ops/switch-gsd-new920b.sh gsd      # 切到 gsd 栈
```

`status` 打印当前占着端口的 orchestrator 是哪个二进制（及其 sha256 前缀）、它进程环境里的 `HOST_GSD_PATH` 与 `HOST_KERNELS_DIR`、据此判断出的"是不是 gsd 栈"、实际生效的 gsd 文件与它的提交号、补丁内核文件、`/health` 的状态码，以及当前活着的 firecracker 进程数。它读的是**运行中进程**的环境，而不是脚本自己的默认值，所以可以用来核对切换是否真的生效。

`gsd` 依次做这些事：

1. **检查前提**，任何一项不满足就退出、什么都不改：`/fc-gsd/orchestrator` 与 `/fc-gsd/gsd` 存在且可执行；补丁内核文件存在；基线 job 定义存在；**没有任何 firecracker 进程在跑**。
2. 如果当前已经是 gsd 栈（二进制、两个环境变量都对得上，`/health` 为 200），打印现状后直接结束。
3. **生成 job 定义**：读事先用 `nomad job inspect` 存下的基线 job，只改三处——启动命令里的二进制路径换成 `/fc-gsd/orchestrator`；去掉 `HOST_GSD_PATH`（用默认值）；加上 `HOST_KERNELS_DIR=/fc-gsd/kernels`——再去掉版本号、提交时间等由 nomad 维护的字段。生成的文件含有 job 里的密钥，以 600 权限保存。
4. **提交 job**。旧的 orchestrator 还占着端口时 nomad 会报端口冲突，脚本每 15 秒重试一次，最多 6 次。
5. **等切换真正完成**，最长 360 秒：占着端口的进程必须是新的二进制、`/health` 为 200、**并且这个进程的环境里 `HOST_GSD_PATH` 与 `HOST_KERNELS_DIR` 就是期望的值**。确认之后再等 20 秒让 API 一侧重新连上，打印切换后的现状。

第 1 步里"还有 firecracker 在跑就拒绝"是一道保护：切换会重启 orchestrator，正在运行的沙箱会受影响，而共用机器上那些沙箱可能是别人的。脚本不提供强制选项，要切就先让沙箱都结束。

第 5 步里要核对进程环境，是因为只换内核目录或只换 gsd 路径时，orchestrator 的二进制没变，旧进程的 `/health` 同样回 200；只看二进制和健康检查会把"还没切过去"误判成"切完了"。

每次都**从基线 job 重新生成**而不是在当前 job 上改，是为了让切换可以反复做而不积累残留：切回另一套栈的脚本同样从这份基线生成，gsd 加的环境变量不会留在里面。

调试时可以用三个环境变量临时换一套文件而不动 `/fc-gsd/` 下的正式文件，例如换上带检查的调试内核：

```bash
GSD_KDIR=<调试内核的根目录> bash scripts/ops/switch-gsd-new920b.sh gsd
# 同理还有 GSD_ORCH_BIN=<orchestrator 文件>、GSD_BIN=<gsd 文件>
```

用了 `GSD_BIN` 时脚本会在 job 里显式设 `HOST_GSD_PATH`；用正式位置时不设。

### 4.2 不是 RPM 加 nomad 的机器

脚本里与本机相关的东西有：基线 job 的存放位置、生成的 job 定义的存放位置、端口号、取 nomad 令牌的方式。换一台机器要改这几处。
部署方式完全不同的机器（不用 nomad）没有现成脚本，但要做的事是一样的：让 `/fc-gsd/orchestrator` 取代原来的 orchestrator 运行，并在它的进程环境里按 §3 设置 `HOST_KERNELS_DIR`（需要时）和 `HOST_GSD_PATH`（一般不设）。切完之后用 §6 的办法核对，而不是相信配置文件。

---

## 5. 建模板

宿主切到 gsd 栈之后，在这套栈下建一个模板，本书统一叫它 `base-gsd`。建模板就是普通的 e2b 模板构建，不需要任何 gsd 专有的参数——注入是 orchestrator 自动做的：

```python
from e2b import Template, default_build_logger

IMAGE = "<镜像仓库>/e2b-orchestration/ubuntu:22.04-custom-criu421"
Template.build(
    Template().from_dockerfile(f"FROM {IMAGE}"),
    alias="base-gsd",
    cpu_count=2,
    memory_mb=2048,
    on_build_logs=default_build_logger(),
    skip_cache=True,
)
```

随测试脚本附带的 `build_template.py` 是对这段调用的封装，等价的命令行是：

```bash
python build_template.py --alias base-gsd --memory 2048 --image <镜像仓库>/e2b-orchestration/ubuntu:22.04-custom-criu421
```

它不带 `--use-cache` 时就是 `skip_cache=True`。2 vCPU、2048 MB 是本书全部实测所用的规格（[20](20-results-and-gaps.md)）；进程镜像占 guest 内存，内存给多少要按 [22](22-semantics-and-limits.md) 的容量一节来定。

构建过程中 orchestrator 多做了这些事（细节见 [05](05-guest-boot-and-injection.md)）：往根文件系统里加 gsd、`gsd.service` 和 `overlay-init`；让 guest 以 `overlay-init` 而不是 systemd 作为启动程序；模板的每一层建好后把宿主的 gsd 拷进沙箱、重启 gsd，然后**自检**——`gsd.service` 是 active、`/health` 回 `ok`、`/usr/sbin/criu` 是 4.x，轮询最多约 10 秒。
自检不过，构建就失败，不会留下一个"建成功了但做不了 checkpoint"的模板。新 920B 上 2026-10-08 建这个模板用了 17.2 秒（仅作量级参考）。

不设 `HOST_GSD_PATH`、gsd 放在默认位置 `/fc-gsd/gsd` 这条路径，已于 2026-10-09 在新 920B 上实际建模板确认：orchestrator 的进程环境里没有 `HOST_GSD_PATH` 这个键，`/fc-gsd/gsd` 被注入，
guest 内 `/usr/bin/gsd` 的 sha256 与它相同、`gsd -commit` 输出 `259f1a25c`、`gsd.service` 为 active、THP 为 never；`base-gsd`（2 vCPU / 2048 MB）不用缓存，15.2 秒建成，随后的功能验证全部通过（结果见 [20](20-results-and-gaps.md) 的"落位后的复核"一节）。
`HOST_GSD_PATH=off` 的关闭态仍然没有真实建过模板（[15](15-known-limits.md) 的 T-08）。

要提醒的是，这一步自检**不验证内核是否带补丁**（[15](15-known-limits.md) 的 L-16）：`HOST_KERNELS_DIR` 指错、用没打补丁的内核建模板，自检照样通过，要到第一次 checkpoint 才失败。内核对不对靠 §6 到 guest 里核对版本串。

关于模板，有三条规则必须记住：

**模板固化了建模板时的内核、gsd 和启动脚本。** 沙箱不是"开机"出来的，而是从模板的快照恢复出来的：内核已经在内存里跑着，gsd 进程也已经在跑。所以宿主上后来换掉 `/fc-gsd/gsd` 或内核文件，**对已有的模板和从它起的沙箱没有任何影响**；
同理，换了三者中的任何一样，都必须重建模板才会生效。这一点曾经让一次内核性能对照失效（[17](17-performance-after-fixes.md)），部署时最容易犯的错也是"换了文件、没重建模板、以为已经升级"。
升级的完整顺序是：换文件 →（换的是 orchestrator 时）重新切栈 → 重建模板 → 用 §6 核对 guest 里的版本。

**重建模板不要用构建缓存。** 带 gsd 与不带 gsd 的基础层在缓存里是分开的，不会互相复用；但缓存的键里不含 gsd 二进制或内核的版本，换了 gsd 之后用缓存重建，可能拿到旧的那一层。上面的命令默认不用缓存。

**旧一代的模板不要再用。** 用修复之前的内核或 gsd 建的模板里固化着已知有严重缺陷的版本（[14](14-issue-resolution.md)）。升级之后把旧模板从使用方的配置里撤掉；判断一个模板是哪一代，看 §6 里 guest 的 `gsd -commit` 与内核版本串。

---

## 6. 部署后自检

自检的原则是**到 guest 里看**：宿主上的文件对不对只说明"下一次建模板会用什么"，沙箱里实际跑的是什么要问沙箱自己。下面这段起一个沙箱，把该看的一次列出来：

```python
from e2b import Sandbox

CHECK = r"""
echo "gsd commit  : $(/usr/bin/gsd -commit)"
echo "gsd sha256  : $(sha256sum /usr/bin/gsd | cut -d' ' -f1)"
echo "kernel      : $(cat /proc/version)"
echo "thp         : $(cat /sys/kernel/mm/transparent_hugepage/enabled)"
echo "rcu_exp     : $(cat /sys/kernel/rcu_expedited)"
echo "gsd.service : $(systemctl is-active gsd)"
echo "health      : $(curl -s http://127.0.0.1:49984/health)"
echo "criu        : $(/usr/sbin/criu --version | head -n 1)"
echo "/           : $(stat -c '%a %U:%G' /)"
echo "/tmp        : $(stat -c '%a %U:%G' /tmp), mount: $(findmnt -n /tmp || echo none)"
echo "store       : $(findmnt -n -o FSTYPE,SIZE /var/lib/gsd/snapshots)"
dmesg | grep 'overlay-init'
"""

sbx = Sandbox.create(template="base-gsd", timeout=300)
try:
    print("sandbox", sbx.sandbox_id)
    print(sbx.commands.run(CHECK, user="root", timeout=60).stdout)
    print("is_running:", sbx.checkpoint.is_running())
finally:
    sbx.kill()
```

逐项的期望值：

| 项 | 期望 | 不符时意味着 |
|---|---|---|
| `gsd commit` | 部署的提交号（本书是 `259f1a25c`） | 模板是用旧的 gsd 建的，或宿主上放的不是这一版 |
| `gsd sha256` | 与宿主 `sha256sum /fc-gsd/gsd` 相同 | 同上；提交号相同而 sha 不同说明是另一次构建 |
| `kernel` | 版本串以 `Linux version 6.1.158+` 开头，构建时间是你编这个内核的时间 | 模板固化的是别的内核：建模板时 `HOST_KERNELS_DIR` 指错了，或没重建模板 |
| `thp` | `always madvise [never]` | 启动脚本没能关掉透明大页；增量 checkpoint 会膨胀成接近全量（[02](02-criu-and-incremental-memory.md)） |
| `rcu_exp` | `1` | gsd 启动时没能打开加速的 RCU；restore 会慢且抖动（[16](16-restore-slowness-case.md)） |
| `gsd.service` | `active` | gsd 没起来，看 `journalctl -u gsd` |
| `health` | `{"status":"ok"}` | 是 `degraded` 时按 [25](25-troubleshooting.md) 处理 |
| `criu` | `Version: 4.2.1` | 基础镜像不对 |
| `/` | `755 root:root` | 模板是用旧的启动脚本建的 |
| `/tmp` | `1777 root:root`，`mount: none` | `/tmp` 上还挂着单独的内存盘，它不会跟着回滚 |
| `store` | `tmpfs 2G` | 镜像目录没有挂成内存盘 |
| `dmesg` 的 `overlay-init` 行 | 有 `overlay mounted at /mnt/newroot, / is 755 root:root`、`transparent_hugepage=never`、`/tmp on overlay: 1777 root:root, tmp.mount -> /dev/null`，没有 `WARNING` | 启动脚本的某一步没成功 |
| `is_running` | `True` | 宿主到 guest 49984 端口的路径不通（[25](25-troubleshooting.md)） |

内核版本串与各文件 sha256 的参考值（2026-10-08 那次部署）在 [20](20-results-and-gaps.md) 的条件标签表里。

这段自检只确认"部件就位"，不确认"功能正确"。接下来按 [26](26-acceptance-runbook.md) 跑验收脚本，第一个脚本 `verify_gsd` 会重复其中几项并实际做若干次 checkpoint / restore。

---

## 7. 950

950 上**未部署、未测**。gsd 的设计不依赖 950 独有的硬件特性（它的脏页跟踪在 guest 内核里用软件完成），按理同样的部件可以直接部署，但这只是推断，没有任何结果支持。
在 950 上部署的前提（宿主内核与 Firecracker 的版本要求、与整机级回滚共用时的布局、切栈方式）**待补**；在补上并做过 [26](26-acceptance-runbook.md) 的验收之前，不要把本章的步骤当作 950 上的部署说明。

---

## 代码位置

| 内容 | 位置 |
|---|---|
| gsd 的版本与提交号（`-version`、`-commit`） | `packages/gsd/main.go:38`、`:116` |
| `HOST_GSD_PATH`、`HOST_KERNELS_DIR` 的默认值与 `off` 的处理 | `packages/orchestrator/internal/cfg/model.go:22`、`:23`、`:39` |
| orchestrator 找内核文件的路径规则 | `HostKernelPath`（`packages/orchestrator/internal/sandbox/fc/config.go:33`） |
| 往根文件系统加 gsd 文件、读宿主 gsd 二进制 | `addGSDFiles`（`packages/orchestrator/internal/template/build/core/rootfs/gsd.go:33`） |
| 启动脚本与服务单元模板 | `packages/orchestrator/internal/template/build/core/rootfs/gsdfiles/overlay-init.sh.tpl`、`gsd.service.tpl` |
| 注入后的自检脚本与注入过程 | `gsdCheckScript`、`injectGsdInSandbox`（`packages/orchestrator/internal/template/build/layer/gsd.go:22`、`:50`） |
| 基础层缓存键里的 `"gsd"` | `packages/orchestrator/internal/template/build/phases/base/hash.go:55` |
| 内核构建脚本、固定基线、config、补丁 | `fc-kernels/build.sh`、`fc-kernels/kernel_base_tags.txt`、`fc-kernels/configs/arm64/6.1.158.config`、`fc-kernels/patches/6.1.158/` |
| 基础镜像 | `packages/gsd/docker/Dockerfile.criu421` |
| 切栈脚本 | 随手册附带的 `scripts/ops/switch-gsd-new920b.sh`（文件头注释即布局说明） |

---

## 本章要点

- 要部署的是 gsd、gsd 版 orchestrator、打了三个补丁的 guest 内核、带 CRIU 4.2.1 的基础镜像和 Python SDK，必须来自同一个提交；Firecracker 和宿主内核不动；只支持 arm64 的 guest。
- 离线构建靠 `GOTOOLCHAIN=local GOWORK=off GOPROXY=off GOFLAGS=-mod=readonly` 加预先备好的模块缓存；提交号用 `-ldflags` 嵌进二进制，先 vet 和单测再编。
- 内核从 `kernel_base_tags.txt` 固定的 tag 出发按顺序打三个补丁；用"从基线重放、比较 `git write-tree`"核对源码树，而不是比对补丁文本。
- 运行时只有三个文件：`/fc-gsd/gsd`、`/fc-gsd/orchestrator`、`/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin`。默认就注入 gsd，不用设 `HOST_GSD_PATH`；与别的栈共用的机器用 `HOST_KERNELS_DIR=/fc-gsd/kernels` 指向补丁内核，只跑 gsd 的机器把内核直接放 `/fc-kernels`。
- 切栈脚本从基线 job 重新生成定义、提交、并等到新进程的环境变量确实生效才算完成；还有 firecracker 在跑时拒绝切换。gsd 栈运行期间建的所有模板都会被注入。
- 模板固化建模板时的内核、gsd 和启动脚本：换任何一样都要不用缓存地重建模板，已有模板不受宿主文件更换的影响；旧一代模板不要再用。
- 部署后到 guest 里核对：`gsd -commit`、内核版本串、透明大页为 `[never]`、`rcu_expedited` 为 1、`/health` 为 ok、CRIU 4.2.1、`/` 为 755、`/tmp` 在 overlay 上；然后转 [26](26-acceptance-runbook.md) 做验收。
- 950 未部署、未测，部署前提待补。
