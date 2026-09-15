# 29 · 上机验收操作

> 拿到交付件之后，在目标机上怎么把这套 checkpoint / restore 验一遍：装什么、跑哪几条命令、
> 每条该看到什么、结果怎么留档回传。**本篇只讲操作**，判据背后的道理在第 24–28 篇。
>
> **读者**：做验收的人。**预备**：[第 24 篇 §5](24-test-overview.md#5-环境与产物)。
> **代码**：`e2b-infra/rollback/scripts/acceptance/`、`e2b-infra/rollback/scripts/dev/`

---

## 0. 本篇要回答的问题

1. 跑验收之前，机器上要装好哪几样东西？
2. 交付态三条命令分别该看到什么字样才算过？
3. 一定要在宿主机上跑吗？远程跑会少掉什么？
4. 结果怎么留档，回传时必须写清楚哪些字段？

---

## 1. 前置条件

**在哪跑。** 强烈建议**直接在跑 orchestrator 的宿主机上**跑。远程也能验正确性，
但脏页后端、产物文件系统、每代产物实占、宿主分阶段这四类信息全都拿不到
（套接字与 `/proc` 都是本地的），报告里会是「未知」和空格
（[第 24 篇 §5.2](24-test-overview.md#52-宿主机-vs-非宿主机)）。

**Python 环境。** 建议用独立 conda 环境，别动系统 python：

```bash
conda create -n e2b-verify python=3.12 && conda activate e2b-verify
pip install e2b==2.20.0 python-dotenv
```

版本**必须**是 `e2b==2.20.0` —— SDK 覆盖层是整文件覆盖、版本锁死的，对不上会拒绝安装。

**SDK 覆盖层。** checkpoint / restore 的四个 RPC 不在上游 SDK 里，靠覆盖层装进去：

```bash
python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py    # 装覆盖层
python /opt/e2b-infra/patch_e2b.py                         # 再改端点
```

**顺序不能反** —— 覆盖层要排在 `patch_e2b.py` 之前，反了会被后者 https→http 的
全局替换盖回去（`MANIFEST.md` §七）。正常部署时 `build.sh` 的 `install_e2b`
已经按这个顺序调过一次，这里只是手工场景的说明。

覆盖层带不带 `mem_mode` 字段，直接决定验收会跑 59 项还是 57 项（§2）。

**`.env`。** 在 `benchmark/` 目录下放一份 `.env`（`cp .env.example .env`，或直接
`bash sync-env.sh` 自动生成并填凭据），再做一条软链
`ln -s ../../benchmark/.env rollback/scripts/.env` 让脚本读得到，四个变量：

| 变量 | 填什么 |
|---|---|
| `E2B_API_KEY` | 团队 API Key（`/root/.e2b/config.json` 的 `.teamApiKey`） |
| `E2B_DOMAIN` | 默认 `e2b.app` |
| `E2B_API_URL` | api job 的 REST 端口，如 `http://<server_ip>:3000`。**占位符没替换掉会报 `Name or service not known`** |
| `E2B_HTTP_SSL` | `false` |

---

## 2. 交付态：三条命令

三条命令都在 `rollback/scripts/acceptance/` 目录下跑，**全部用默认参数**。

### 2.1 覆盖层自检

```bash
python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py --check
```

期望结尾（`payload` 文件齐 + 干净子进程自检通过）：

```
payload 的 16 个文件都在位
  自检通过（干净子进程）：Sandbox.checkpoint / CheckpointInfo.mem_mode / proto mem_mode / 六个方法 / 端口 49984 / 无死 Authorization 头
```

看到 `CheckpointInfo.mem_mode` 才说明「增量有没有被静默降级成全量」这道判据是活的。

### 2.2 正确性验收

```bash
python checkpoint_verify.py
```

期望结尾三段：汇总头一行说明本次跑在什么上、判定行、以及最后的清理行：

```
跑在：脏页后端 = HDBSS（硬件标脏）   |   产物文件系统 = ext4   |   落盘路径 = /orchestrator/build/checkpoints

✓ 59 项校验全部通过。
  …（内存 / 根文件系统 / 进程 三行说明）
沙箱已删除
```

退出码 `0`。判定看**判定行**：`✓ N 项校验全部通过。`，一项都不能少。
**N 必须是 59**；出现 `57` 说明 SDK 没有 `mem_mode` 字段，脚本会先打印一行
「（服务端没有报 mem_mode 字段，跳过全量/增量的判定）」并跳过那两条断言 ——
这轮结果仍然是有效的正确性验收，但**不能拿来证明用的是增量**
（[第 24 篇 §1.4](24-test-overview.md#14-判据本身静默失效)）。

任何一项 `FAIL` 都会让脚本打印 `✗ n / N 项校验未通过：` 并逐条列出，退出码 `1`。

### 2.3 性能基准

```bash
python checkpoint_bench.py
```

期望结尾：

```
跑在：脏页后端 = HDBSS（硬件标脏）   |   产物文件系统 = ext4   |   落盘路径 = …
全量（第一代）… s。
最大档 256 MB（内192+文64）：立刻拍 … s，冲干净再拍 … s ——
差值 … s 是「写完立刻拍」的时机成本，不是快照机制的成本。
每一跳都落到了目标代（代号标记校验通过）。
沙箱已删除
```

判定行是 `每一跳都落到了目标代（代号标记校验通过）。`，退出码 `0`。两条会让它变成 `1`：

- `⚠ 有增量档被服务端报成 full` —— 脏页跟踪没生效，这组数字不是增量，查
  template-manager 的 `FC_TRACK_DIRTY_PAGES`；
- `✗ n 跳没落到目标代` —— 恢复没回到该回的那一代，属于正确性问题，先停下来。

另外三件事要顺手确认：档位表里 `mem_mode` 一列全是 `incremental`；
「跑在」那一行的脏页后端是期望的那个（`kvm-wp` 说明走的是软件写保护，
数字含 VM exit 开销）；「实测内存 / 实测文件」两列不是空的 —— 空了说明不在宿主机上跑。

---

## 3. 留档：一律 `tee`

**两个交付态脚本只打屏、自己不落盘。** 上机第一件事就是把输出接住，
别靠终端 scrollback：

```bash
mkdir -p reports/<机器>-verify-<日期>
python checkpoint_verify.py 2>&1 | tee reports/<机器>-verify-<日期>/checkpoint-verify.log
python checkpoint_bench.py   2>&1 | tee reports/<机器>-verify-<日期>/checkpoint-bench.log
```

---

## 4. 开发态流程摘要

要更细的数据（分位数、链深、冻结窗口、兼容矩阵、HDBSS 三级证据），
把 `rollback/scripts/dev/` 整个目录拷到目标机，按下面四步走。
每一步的细节、两套方案的差别、数据卷怎么造，见
[`../scripts/dev/README.md`](../scripts/dev/README.md) —— **别手敲单个脚本**，位置参数顺序敏感。

| 步 | 命令 | 必须看到 |
|---|---|---|
| 1 | `bash 01-check-host.sh` | `cap 502: supported`（有 HDBSS）、`bin/SHA256SUMS` 校验通过 |
| 2 | `bash 03-switch.sh ext4 --yes` | 换二进制 + 改 env + 重启 template-manager；改动前自动备份到 `backup/<时间戳>/` |
| 3 | `bash 04-verify-runtime.sh ext4 /` | `orchestrator` / `firecracker sha` 是期望的那套、`dirty_tracking: hdbss`、`store fs` 与预期一致 |
| 4 | `bash run-all.sh ext4 /` | 六个脚本一条龙，报告落 `reports/<方案>-<时间戳>/` |

第 4 步逐个脚本的判据速查：

| 脚本 | 必须看到 |
|---|---|
| `correctness.py` | 末行 `ALL PASS` |
| `timing.py` | 末尾 `ALL CORRECT` |
| `loop.py` | `failures: 0` |
| `pause_verify.py` | 末行 `✓ 全部通过` |
| `compat_matrix.py` | 末行 `✓ 没有 BROKEN`（`REFUSED` 是边界不是故障） |
| `bench-ckpt.py` | 末行 `BENCH OK`；各档 `mem_mode` 全 `incremental`；冻结窗口每档标「可区分 ✓」 |
| `hdbss_evidence.py` | L1 `supported` + L2 `hdbss` + L3 冷/热接近 1（软件写保护时约 4.5~5.4，见[第 25 篇 §6](25-functional-tests.md#6-hdbss_evidencepy能力不等于数据面)） |

验收结束后 `bash 03-switch.sh restore` 把机器还原成切换前的样子。
任何一步不过就停下来，把该步输出和 `/data/nomad/alloc/*/alloc/logs/start.stdout.0`
的相关片段一起看。

---

## 5. 结果回传规范

每一轮实测在 `reports/<机器>-<内容>-<日期>/` 下留一个目录：原始日志（`tee` 出来的）
+ 一份 `00-context.md`。**没有 `00-context.md` 的日志不能被引用** ——
不带条件的数字一定会被人单独拿去比较
（[第 24 篇 §4.2](24-test-overview.md#42-一个数字要带的条件标签)）。

`00-context.md` 必填字段：

| 字段 | 写什么 | 从哪来 |
|---|---|---|
| 机器 | 机型、CPU、OS、内核；是不是宿主机 | 人填 |
| 时间 | 起止时间（checkpoint id 是纳秒时间戳，可回推） | 脚本输出 |
| 命令与参数 | 命令行原样抄，**默认参数也要写明「全部用默认参数」** | 人填 |
| 被测二进制 | orchestrator / firecracker 的来源分支与 `sha256`（前 16 位即可） | `sha256sum` |
| SDK | e2b 版本 + 覆盖层装没装 + `CheckpointInfo` 有没有 `mem_mode` | `install.py --check` |
| 脏页后端 | `hdbss` / `kvm-wp` / `off` | 脚本「跑在」那一行 |
| 产物文件系统与介质 | 路径 + 文件系统 + **真盘还是 loop 卷** | 脚本「跑在」那一行 + 人补介质 |
| 沙箱 id | 本轮用的沙箱 id（跑完已删除也要记） | 脚本输出 |
| 结果 | 场景 → 结果的表；**项数照抄**（`59/59` 还是 `57/57`） | 脚本输出 |
| 这份数不能拿来干什么 | 至少一条：不是性能基准 / 不能与另一台机器相减 / 判据当时是否有效 | 人填 |

两份可以照抄结构的范本：
`reports/950-verify-20260829/00-context.md`（报告归档在工作区 `e2b-repo/rollback-reports/`，不入库）
（交付态一轮，末尾三条注意写明了「这不是性能基准」「不能和另一台机器相减」）与
`reports/920b-kas0904-20260904/00-context.md`（开发态全套，外加两个遗留问题的定位过程）。

---

## 6. 小结

1. 前置四样：`e2b==2.20.0`、SDK 覆盖层（**排在 `patch_e2b.py` 之前**）、`.env` 四个变量、
   尽量在宿主机上跑。
2. 交付态三条命令的判定行：覆盖层 `自检通过（干净子进程）…`、
   正确性 `✓ 59 项校验全部通过。`、基准 `每一跳都落到了目标代（代号标记校验通过）。`
3. `59` 变 `57` 不是小事：说明 SDK 没有 `mem_mode`，「是不是增量」那道判据没跑。
4. 两个交付态脚本不落盘，**一律 `tee`**。
5. 要细数据走开发态 `01 → 03 → 04 → run-all`，别手敲单个脚本；跑完 `03-switch.sh restore`。
6. 回传的是**目录**不是日志：原始日志 + `00-context.md`，后者必须写清机器、时间、
   命令参数、二进制 sha、SDK、脏页后端、产物文件系统与介质、沙箱 id、项数，
   以及这份数不能拿来干什么。

---

## 延伸阅读

| 想知道 | 去哪 |
|---|---|
| 这些判据为什么是这几条 | [第 24 篇](24-test-overview.md)、[第 25 篇](25-functional-tests.md) |
| 数字怎么读、口径是什么 | [第 26 篇](26-performance-methodology.md) |
| 实测结果与达标判定 | [第 28 篇](28-results-and-compliance.md) |
| 开发态工具箱完整说明 | [`../scripts/dev/README.md`](../scripts/dev/README.md) |
| 平台前提与部署陷阱 | [第 19 篇 §6](19-kunpeng-platform.md#6-部署检查清单) |

**下一篇**：[30 · 在这套方案上继续开发](30-extending.md) —— 验收之后，
要在这套方案上改代码、加能力，需要先知道哪些不变量不能碰。
