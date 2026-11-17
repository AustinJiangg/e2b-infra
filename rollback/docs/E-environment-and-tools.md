# 附录 E · 测试环境、轮次与工具索引

## 本章目标

记录实测环境、结果留档方法和各轮次的来源，让读者能判断一个数字来自什么条件。`/opt/e2b-infra`、Nomad job、RPM 和 SDK 覆盖层是 950 测试部署方式，其他部署可以用自己的方式提供同样的功能前提。

## 1. 950 的实际环境与特性配置

| 条件或配置 | 已有证据 | 含义 |
|---|---|---|
| 机器 | Kunpeng 950 7592C，2.3 GHz（最低 1.2 GHz）；2 路 × 96 核 × 2 线程 = 384 CPU；4 个 NUMA 节点；内存 2261 GB | 2026-11-17 `lscpu`、`free -g` 回传；地址 141.61.11.24 |
| 系统与内核 | openEuler 24.03 (LTS-SP4)；宿主内核 `6.6.0-159.4.3.154.oe2403sp4.aarch64`；GICv3；`CONFIG_ARM64_HDBSS=y` | 平台前提见 [第 25 章](25-build-and-self-check.md) |
| 编排与容器 | Nomad v1.11.2、Consul v1.21.4、docker 29.7.2 | 部署方式的版本，不是功能前提 |
| 大页 | 2048 kB × 32768 页（64 GiB），开跑时空闲 32730 页 | 预检只记录，不据此保证某个并发规模的容量 |
| 内存产物 | 模板 `base`：guest 2 vCPU、可见内存 1990 MiB；全量内存产物 2048 MiB | 不从 920B 模板推定 950 未留档字段 |
| 存储 | `/orchestrator/build/checkpoints` 在根盘 `/dev/mapper/openeuler-root`（LVM，ext4，1.1 TB，开跑时 798 GB 可用）；缓存盘与产物盘同源 | 路径可不同，需满足文件系统与同盘条件 |
| 被测二进制 | orchestrator `/usr/bin/template-manager` sha256 `a5d03902afd56e84c4cd956bf73b3bb9d40c4049d736a51985d4d5c7927289a8`（能力文件自报 commit `4853232`）；FC `/fc-versions/v1.13.1/firecracker` sha256 `18f3faa7f47c173a5f47bfc7f578f5073cfbde2ac9bdb50bf88c434341d6d4c9`，自报 1.12.1 | 2026-11-17 standard 与 2026-11-06 原生分档用的是同一份 orchestrator |
| 客户端 | conda 环境 `jll-e2b`：Python 3.14、e2b SDK 2.20.0、httpx 0.28.1 | 在被测宿主上运行 |
| `FC_TRACK_DIRTY_PAGES` | 未设置；orchestrator 探测 KVM 能力 502 后自动开启，能力文件 `track_dirty_pages_reason=hardware dirty state tracking present (KVM capability 502)` | 以启动能力与结果核对是否启用，不仅查看环境变量 |
| HDBSS 后端 | 各用例与基准日志自报 `hdbss`，HDBSS 专项另有内核与 FC 启用证据 | 能力探测、FC 自报与数据面证据分别核对 |
| `FC_HDBSS_ORDER` | 未设置，默认 order 1；未扫描其他 order | 默认和可选值查第 26 章，不把未测 order 写成已验证 |
| smoke 有效配置 | `track_dirty_pages=true`（硬件能力 502）；`compact=true`、最多合并 8 个；`debug_index=false`；数量 / 字节上限均 0；最低余量 4294967296 字节；`fault_inject=[]`；各项来源均为 `default` | 对应配置语义查第 26 章 |
| 950 测试部署方式 | `/opt/e2b-infra`、RPM、Nomad、SDK 覆盖层 | 只说明测试环境，不要求采用此安装方法 |

2026-10 曾在另一台 950（主机名 `slot6`，宿主内核 `6.6.0-159.4.12.166`）上测过一轮。那台机器与上表不是同一台，其数据已由 2026-11-17 一轮取代，不再引用。

配置总表在[第 26 章](26-configuration-and-capacity.md)。smoke、func、perf、HDBSS 出自同一轮 `test950.sh standard`，期间 orchestrator 没有重启（能力文件启动时间 15:52:20，早于本轮开始）。

## 2. 测试环境与沙箱规格

以下是 920B 历史轮次的模板约定。950 以各轮实际留档字段为准；未记录的字段不能从此表补填，规格变化时性能需重采：

| 项 | 值 | 怎么核对 |
|---|---|---|
| 模板别名 | `base` | 各脚本与 `run.sh` 的默认模板（`--template` 可改） |
| 基础镜像 | `harbor:443/e2b-orchestration/ubuntu:22.04-custom` | `benchmark/build_template.py` 里的 `FROM` |
| vCPU | 2 | `GET /templates` 的 `cpuCount` |
| 内存 | 2048 MB | `GET /templates` 的 `memoryMB` |
| 磁盘 | 约 940 MB（guest 内 ext4，240640 块 × 4 KiB） | `GET /templates` 的 `diskSizeMB` |

在宿主机上核对（access token 取部署时写下的 SDK 凭据）：

```bash
AT=$(jq -r .accessToken /root/.e2b/config.json)
curl -s -H "Authorization: Bearer $AT" http://127.0.0.1:3000/templates \
  | jq -r '.[] | select(any(.aliases[]?; . == "base"))
           | "cpuCount=\(.cpuCount) memoryMB=\(.memoryMB) diskSizeMB=\(.diskSizeMB) buildStatus=\(.buildStatus)"'
```

预期 `cpuCount=2 memoryMB=2048 diskSizeMB=940 buildStatus=ready`。
为什么内存规格要写进条件标签：全量 checkpoint 的代价约等于整份 guest 内存，增量只跟本代脏页量有关。
换一个内存更大的模板，全量那一列等比变大、增量各档基本不动，所以两组数字只能并列，不能相减、也不能并表。

## 3. 留档与回传

**一律留原始输出。** `run.sh` 自己把每项输出和 `run.log` 写进结果目录；手工跑的命令（`long` 清单、单个脚本）一律 `2>&1 | tee` 到文件，别靠终端回滚。例如单独再跑一次正确性验收：

```bash
OUT=rollback/scripts/950/results/$(date +%Y%m%d-%H%M%S)-manual; mkdir -p "$OUT"
PY=$(command -v python3)  # 选用已经包含配套SDK的解释器
"$PY" rollback/scripts/acceptance/checkpoint_verify.py 2>&1 | tee "$OUT/checkpoint_verify.log"   # $PY 为上面选定的配套SDK解释器
```

**每个结果目录应配`00-context.md`或等效条件记录；现有材料缺项时，结论须保留相应限制**——不带条件的数字一定会被单独拿去比较。先用下面这段把能自动取的填上（在最新的结果目录里生成）：

```bash
OUT=$(ls -1dt rollback/scripts/950/results/*/ | head -1)
p=$(pidof -s template-manager)
A=$(tr '\0' '\n' < /proc/$p/environ | sed -n 's/^NOMAD_ALLOC_DIR=//p')
{
  echo "# 00-context"
  echo "- 机器：$(hostname) · $(lscpu | sed -n 's/^Model name: *//p' | head -1) · $(uname -r)"
  echo "- orchestrator sha256：$(sha256sum /proc/$p/exe | cut -c1-16)"
  for fc_pid in $(pidof firecracker); do
    echo "- Firecracker PID $fc_pid：$(readlink /proc/$fc_pid/exe) sha256=$(sha256sum /proc/$fc_pid/exe | cut -d' ' -f1)"
  done
  echo "- RPM：$(rpm -q e2b-infra)"
  echo "- e2b：$("$PY" -m pip show e2b 2>/dev/null | sed -n 's/^Version: //p')，解释器 $PY"
  echo "- 产物盘：$(df -T /orchestrator/build | awk 'NR==2{print $1, $2}')"
  F=/orchestrator/build/checkpoint-capabilities.json   # 设了 DEFAULT_CACHE_DIR 就换成它下面的同名文件
  if grep -qE "^  \"pid\": $p,?\$" "$F" 2>/dev/null; then
    echo "- 能力文件（$F）："; tr -d '\n' < "$F" | tr -s ' '; echo
  else
    echo "- 能力行："
    ls -1v "$A"/logs/start.stdout.* | xargs grep -ah 'checkpoint capabilities' | tail -1
  fi
} > "$OUT/00-context.md"
cat "$OUT/00-context.md"
```

再手工补上：

| 字段 | 写什么 |
|---|---|
| 时间 | 起止时间（`SUMMARY.md` 里有开跑 / 收尾） |
| 命令与参数 | 原样抄；默认参数也写明"全部默认" |
| 脏页后端 | `checkpoint_verify.log` 开头那行（`hdbss` / `kvm-wp` / `off`） |
| 产物介质 | 真盘还是 loop 卷 |
| 模板规格 | `cpuCount` / `memoryMB` / `diskSizeMB` |
| 结果 | 各档 PASS/FAIL/SKIP；**项数照抄**（59 还是 57） |
| 这份数不能拿来干什么 | 至少一条：例如"不是性能基准""不能与另一台机器相减""--quick 短试跑" |

**回传什么**：各档的 `SUMMARY.md` 与 `00-context.md` 一定带；有 FAIL 的项带它的 `.log` 与 `.json`；perf 档另带 `compliance.log`、`analyze.log`、`serial-restore.json`。不需要打包整个目录。

## 4. SDK 覆盖层

本节说明 950 测试部署采用的 SDK 覆盖层。使用实际安装该 SDK 的解释器；也可以直接构建配套 SDK，不需要这层工具。

```bash
python3 /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check
```

期望看到 `payload 的 21 个文件都在位` 和 `自检通过（干净子进程）：…`。异常族的判据是"原有 8 个子类都在，且 reason 映射表里每一项都是 `CheckpointException` 的子类"——
现在是基类加 9 个子类共 10 个（[第 24 章 §9](24-api-semantics.md#9-异常类层次)）。

## 5. 轮次与结果来源索引

| 轮次 | 时间 | 手册位置 | 当前归档范围 |
|---|---|---|---|
| 950 standard：smoke | 2026-11-17 16:17 | 第 17 章 | 5 项通过、预检全文、能力文件、FC 路径哈希和 59 项正确性；结尾 smoke-after 同样 5 项通过 |
| 950 standard：func | 2026-11-17 16:30 | 第 17、21 章 | 16 个默认用例全过；T13 / T36 / T40 全文与各用例只记录项；SDK pytest 未跑 |
| 950 standard：perf | 2026-11-17 16:48 | 第 18、21 章 | 分档、达标判定、串行 restore、A/B 并发；A 段 N=16 的 96 个操作起止时间；JSON / JSONL 未回传 |
| 950 standard：HDBSS | 2026-11-17 16:58 | 第 18 章 | 默认 order 1、三级证据、写密集负载 |
| 950 原生分档 | 2026-11-06 | 第 19 章 | 原生 pause / resume 六档，每档 15 个 repeat、3 个 first，共 108 样本；身份与原始 JSON 已回传 |
| 920B 历史与开发验证 | 对应各轮原始日期 | 附录 B | 功能、分档、原生增量、并发、小时级长测和部署后回归 |

脚本与环境材料保留在工具仓库；950 各轮测试的结论见 [结果摘要](../evidence/950/README.md)，原始日志与 JSON 只保留在被测机的 `test950/results/` 下。主要条件、数据与判据已经进入本手册。

## 6. RPM 部署的路径示例

以下是 920B 上 RPM 部署的路径映射和哈希核对示例，不是 950 或新构建产物的预期身份。950 各轮实际哈希见第 17、18 章。

| 组件 | 产物 → 安装位置 | 核对 |
|---|---|---|
| orchestrator | RPM → `/opt/e2b-infra/bin/orchestrator`，再由部署拷到 `/usr/bin/template-manager`（nomad job `template-manager-system` 实际执行的就是它）与 `/usr/bin/orchestrator` | 三个文件 sha256 相同；运行中的进程 `readlink /proc/$(pidof -s template-manager)/exe` 指向 `/usr/bin/template-manager` |
| Firecracker | `firecracker.arm` → `/opt/e2b-infra/bin/firecracker` → `init-client.sh` 拷到 `/fc-versions/v1.13.1/firecracker` | 两处 sha256 都是 `18f3faa7f47c173a5f47bfc7f578f5073cfbde2ac9bdb50bf88c434341d6d4c9` |
| guest 内核 | `/fc-kernels/vmlinux-6.1.158/vmlinux.bin` | 文件存在；缺了建模板必失败 |
| Python SDK | KASandbox `py-sdk/` 中的配套 SDK；950 测试部署采用 `e2b==2.20.0` 加覆盖层 | 确认 checkpoint 接口与异常类型；覆盖层另用 `install.py --check`（[§4](#4-sdk-覆盖层)） |

## 本章要点

- 950 的条件以各轮实际回传的字段为准，perf、HDBSS、func、smoke 的身份互不追认；920B 模板的规格不能补填到 950。
- 全量 checkpoint 的代价随内存规格变化，增量只跟本代脏页量有关，所以模板规格是条件标签的一部分。
- 每个结果目录配 `00-context.md`，回传时带 `SUMMARY.md`、条件记录和 FAIL 项的日志。
- 项数要照抄：59 还是 57 差的是增量判据是否生效。
