# 附录 E · 测试环境、轮次与工具索引

本附录记录实测环境和工具来源。/opt/e2b-infra、Nomad job、RPM 和覆盖层属于内部测试部署方式；客户可以用自己的方式提供同样的功能前提。

## 1. 950 的实际环境与特性配置

| 条件或配置 | 已有证据 | 对客户的含义 |
|---|---|---|
| 系统 | 950，`slot6`，aarch64；2026-10-13 宿主内核 `6.6.0-159.4.12.166.oe2403sp4.aarch64` | 平台前提见第 23 章，具体型号和拓扑只采用实际回传值 |
| 内存产物 | perf 两遍首次全量均为 2048 MiB；实际模板其他字段按各轮记录 | 不从 920B 模板推定 950 未留档字段 |
| 存储 | `/orchestrator/build/checkpoints`、ext4；下层介质在 perf 轮未核实 | 客户路径可不同，需满足文件系统与同盘条件 |
| `FC_TRACK_DIRTY_PAGES` | 默认未设置时，orchestrator 探测 KVM 能力 502；smoke 能力文件实际记录见第 17 章 | 以启动能力与结果核对是否启用，不仅查看环境变量 |
| HDBSS 后端 | perf/func 等日志自报 `hdbss`，专项另有内核与 FC 启用证据 | 能力探测、FC 自报与数据面证据分别核对 |
| `FC_HDBSS_ORDER` | HDBSS 专项默认 order 1；本轮未扫描其他 order | 默认和可选值查第 24 章，不把未测 order 写成已验证 |
| 2026-10-15 smoke有效配置 | `track_dirty_pages=true`（硬件能力502）；`compact=true`、最多合并8个；`debug_index=false`；数量/字节上限均0；最低余量4294967296字节；`fault_inject=[]` | 这是该次能力文件记录，不追认更早perf/func；对应配置语义查第24章 |
| 内部部署 | `/opt/e2b-infra`、RPM、Nomad、SDK 覆盖层 | 只说明测试环境，不要求客户采用此安装方法 |

配置总表在[第 24 章](24-configuration-and-capacity.md)。perf、HDBSS、func 和 smoke 的身份分别在各自结果中记录，不能互相追认。

## 2. 测试环境与沙箱规格

以下是920B历史轮次的模板和内部构建约定。950以各轮实际留档字段为准；未记录的字段不能从此表补填，规格变化时性能需重采：

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

本节仅说明内部测试采用的SDK覆盖层。使用实际安装该SDK的解释器；完整安装流程仍在辅助仓库维护，客户可采用自己的配套SDK构建。

```bash
python3 /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check
```

期望看到 `payload 的 21 个文件都在位` 和 `自检通过（干净子进程）：…`。异常族的判据是"原有 8 个子类都在，且 reason 映射表里每一项都是 `CheckpointException` 的子类"——
现在是基类加 9 个子类共 10 个（新增 `CheckpointBytesLimitException`，[第 22 章](22-api-semantics.md)）。

## 5. 轮次与结果来源索引

| 轮次 | 时间 | 手册位置 | 当前归档范围 |
|---|---|---|---|
| 950 perf | 2026-10-13 16:11 | 第 18、19 章 | 分档、串行 restore、A/B 短时并发文本；JSON/JSONL 与部分身份待补 |
| 950 func | 2026-10-13 16:30 | 第 17、19 章 | 默认功能用例、SDK 异常、混合短稳态与恢复后立即请求文本 |
| 950 HDBSS | 2026-10-13 16:52 | 第 18 章 | 默认 order 1、三级证据、写密集负载与该轮二进制哈希 |
| 950 smoke | 2026-10-15 12:13 | 第 17 章 | 5 项通过、能力文件、SDK 自检、FC 路径哈希和 59 项正确性 |
| 920B 历史与开发验证 | 对应各轮原始日期 | 附录 B | 功能、分档、原生增量、并发、小时级长测和部署后回归 |

脚本与环境材料保留在辅助仓库；五轮 950 测试的结论见[结果摘要](../evidence/950/README.md)，原始日志仅保留在本地 `rollback/scripts/950/results/`，其余未入库报告保留在测试工作区。主要条件、数据与判据已经进入本 HTML，辅助仓库地址不替代证据。

## 6. 内部 RPM 的路径示例

以下是920B历史RPM部署的路径映射和哈希核对示例，不能作为950或客户新构建产物的预期身份。950各轮实际哈希见第17、18章。

| 组件 | 内部产物 → 历史安装位置 | 核对 |
|---|---|---|
| orchestrator | RPM → `/opt/e2b-infra/bin/orchestrator`，再由部署拷到 `/usr/bin/template-manager`（nomad job `template-manager-system` 实际执行的就是它）与 `/usr/bin/orchestrator` | 三个文件 sha256 相同；运行中的进程 `readlink /proc/$(pidof -s template-manager)/exe` 指向 `/usr/bin/template-manager` |
| Firecracker | `firecracker.arm` → `/opt/e2b-infra/bin/firecracker` → `init-client.sh` 拷到 `/fc-versions/v1.13.1/firecracker` | 两处 sha256 都是 `18f3faa7f47c173a5f47bfc7f578f5073cfbde2ac9bdb50bf88c434341d6d4c9` |
| guest 内核 | `/fc-kernels/vmlinux-6.1.158/vmlinux.bin` | 文件存在；缺了建模板必失败 |
| Python SDK | KASandbox `py-sdk/` 中的配套 SDK；内部测试采用 `e2b==2.20.0` 加覆盖层 | 确认 checkpoint 接口与异常类型；内部覆盖层另用 `install.py --check`（[附录 E §4](E-environment-and-tools.md#4-sdk-覆盖层)） |
