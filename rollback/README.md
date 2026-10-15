# rollback —— checkpoint / restore 的文档、配图与验证工具箱

沙箱活着时的高频快速回退。本目录是这套功能的**文档与验证材料**；
实现代码交付在 openEuler [KASandbox `deltabox` 分支](https://gitcode.com/openeuler/KASandbox/tree/deltabox)（[MR !119](https://gitcode.com/openeuler/KASandbox/pull/119)，2026-09-09 合入）；
本仓库根的 `0001-adapted-for-arm-architecture.patch` 与 `firecracker.arm` 只用于我们在 950 测试环境上的 rpm 部署。

| 路径 | 内容 | 给谁 |
|---|---|---|
| [`docs/`](docs/) | **技术手册**（教材版），导读 + 33 篇（含附录 A、B），按教学顺序分六部分：基础 → 设计与实现 → 工程保障 → 原生快照的精确增量 → 验证与实测 → 使用与运维 | 首要入口 |
| [`diagrams/`](diagrams/) | 三张汇报用 SVG + Mermaid 图源 | 需要单独看图或改图的人 |
| [`slides/`](slides/) | Slidev 汇报稿与构建脚本 | 需要浏览器演示或导出的人 |
| [`scripts/`](scripts/) | **快照回滚的全部脚本**：`acceptance/` 交付态验收与基准（五个单文件脚本，外加复用 `checkpoint_concurrent.py` 的滚动保留长跑驱动 `rolling_keep10.py`）、`probes/` 开发态探针、`dev/` 开发态工具箱（原 `test-950/`）。实测报告不入库，归档在工作区 `e2b-repo/rollback-reports/` | 验收方（`acceptance/`）；我们自己（另两个） |
| **[Releases](https://github.com/AustinJiangg/e2b-infra/releases)** | 文档打包件与汇报稿 PDF，下载下来直接发给别人 | 要把材料发出去的人 |

> 构建产物一律不进仓库：`slides/` 只跟踪源文件（`node_modules`、`dist/`、
> 同步过来的 `public/diagrams/`、导出的 PDF 都不跟踪），`dist/`（文档打包件）整个不跟踪。
> 它们的成品在 [Releases](https://github.com/AustinJiangg/e2b-infra/releases) 里。

## 从哪读起

| 你是 | 建议路径 |
|---|---|
| 想先看个全貌 | [`docs/00-overview.md`](docs/00-overview.md) |
| 当教材从头学 | 按顺序读：[`00`](docs/00-overview.md) → 第一部分 [`01`](docs/01-background.md)–[`03`](docs/03-goals-and-design-choices.md) → 第二部分 [`04`](docs/04-architecture.md)–[`11`](docs/11-end-to-end.md) → 第三部分 [`12`](docs/12-failure-semantics.md)–[`14`](docs/14-lifecycle-reasoning.md) → 第四部分 [`15`](docs/15-native-increment-diagnosis.md)–[`18`](docs/18-native-and-checkpoint-together.md) → 第五部分 [`19`](docs/19-testing-and-functional-verification.md)–[`22`](docs/22-long-run-and-concurrency.md) → 第六部分 [`23`](docs/23-quickstart.md)–[`30`](docs/30-acceptance-runbook.md) |
| 不写这类代码，但想搞懂它在干什么 | [`01 背景`](docs/01-background.md) → [`24 语义与使用边界`](docs/24-semantics-and-limits.md) |
| 工程师，要接手或参与 | 第一到第三部分；机制最短路径 [`04`](docs/04-architecture.md) → [`05`](docs/05-dirty-page-tracking-and-hdbss.md) → [`06`](docs/06-memory-diff-tree.md) → [`07`](docs/07-disk-layering.md) → [`09`](docs/09-in-place-rollback.md) → [`11`](docs/11-end-to-end.md)；动原生 pause 路径前读第四部分 [`15`](docs/15-native-increment-diagnosis.md)–[`18`](docs/18-native-and-checkpoint-together.md) |
| 系统工程师 / 运维 | [`26 部署前提`](docs/26-deployment-prerequisites.md) → [`27 配置与容量`](docs/27-configuration-and-capacity.md) → [`28 可观测性`](docs/28-observability-reference.md) → [`29 排障`](docs/29-troubleshooting.md) → [`30 上机验收`](docs/30-acceptance-runbook.md) |
| 使用方，判断什么时候用它 | [`00`](docs/00-overview.md) → [`24 语义与使用边界`](docs/24-semantics-and-limits.md) → [`18 两种快照的分工与配合`](docs/18-native-and-checkpoint-together.md) |
| 要看测试证据 / 验收 | [`19 测试体系与功能验证`](docs/19-testing-and-functional-verification.md) → [`21 分档基准与判定`](docs/21-benchmarks-and-compliance.md) → [`22 长跑与并发实测`](docs/22-long-run-and-concurrency.md) → [`30 上机验收`](docs/30-acceptance-runbook.md) |

完整目录见 [`docs/README.md`](docs/README.md)。

## 要在机器上验收

| 想做什么 | 用什么 |
|---|---|
| 正确性验收 | [`scripts/acceptance/checkpoint_verify.py`](scripts/acceptance/checkpoint_verify.py) |
| 性能基准 | [`scripts/acceptance/checkpoint_bench.py`](scripts/acceptance/checkpoint_bench.py) |
| 深入排查 | [`scripts/dev/`](scripts/dev/) |
| 机理探针、跨实现对照 | [`scripts/probes/`](scripts/probes/) |

前两个是**交付态**脚本：各自单文件、零共享依赖，两条命令跑完。
`scripts/dev/`（原 `test-950/`）是**开发态**工具箱，脚本之间有共享模块、需要配路径。
凭据怎么配、950 与 920B 各自要验哪些结论，见 [`scripts/README.md`](scripts/README.md)。

> 两套测试脚本**不要混用**。宿主探针在两个交付脚本里故意重复了一份，
> 就是为了防止只拷走一半、剩下的静默变成 unknown。

**讲解见 [`docs/19`](docs/19-testing-and-functional-verification.md)（每条正确性判据怎么设计的）与
[`docs/20`](docs/20-performance-methodology.md)（判定口径、负载构造、长跑与并发的测法），
操作见 [`docs/30`](docs/30-acceptance-runbook.md)（前置条件、三条命令、期望末行、结果回传规范）。**

## 当前状态

- 950：已归档 **2026-10-13 的完整 perf 档文本结果**：非并发分档、同一快照连续恢复、跨沙箱 A 段与同沙箱 B 段争用；日志确认 FC 自报 `hdbss`、产物文件系统为 ext4。
  分档判定与串行结果见 [`docs/21 §6`](docs/21-benchmarks-and-compliance.md#6-950)，并发延迟与命令异常见 [`docs/22 §7`](docs/22-long-run-and-concurrency.md#7-950-短时并发2026-10-13-ab)。原始 JSON / JSONL、被测二进制身份与部分环境标签尚未回传，不能将本轮文本归档视为确定提交的完整验收。
  同日 16:52 的 **HDBSS 默认 order 1 专项**也已归档：能力、自报、启用日志、冷 / 热写、KVM 退出与 MD5，详见 [`docs/21 §6.7`](docs/21-benchmarks-and-compliance.md#67-hdbss-专项默认-order-1-的启用证据与写密集实测)。该轮已记录二进制 sha256；源码 / RPM 对应与较早 perf 轮身份仍待补。
  同日 16:30 的 **func 默认功能回归**已归档：PASS 17 / FAIL 0 / SKIP 4，16 个 crtest 用例共 863 条硬断言通过、SDK 67 项通过；四类重启 / 注入 / 配额专项未跑，详见 [`docs/21 §2.5`](docs/21-benchmarks-and-compliance.md#25-950-默认功能回归2026-10-13)。混合短稳态与恢复后立即请求见 [`docs/22 §8`](docs/22-long-run-and-concurrency.md#8-950-func-中的短稳态与恢复后请求)。
  **2026-10-15 12:13 的 smoke 五项全部通过**：PASS 5 / FAIL 0 / SKIP 0，宿主预检 19 / 0 / 0、正确性 **59 / 59**；SDK 2.20.0 自检、能力文件自报 `commit=4853232`、预检选定的 `v1.13.1` FC 接口与交付件 hash 核对通过（[`docs/21 §2.6`](docs/21-benchmarks-and-compliance.md#26-950-smoke部署预检与正确性验收)）。本轮身份不直接追认 10 月 13 日 perf / func。
  四档均有文本归档，默认部署与功能验收已有通过证据；func 的专项跳过、perf 超线档与并发命令边界仍单列。其他 order 比较、小时级长跑、滚动保留与原生精确增量专项仍缺本期归档证据。2026-08-29 的 HDBSS 正确性 59 项全过保留为历史开发版记录。
- 920B：2026-09-28/29 在当前代码基准上做了除 HDBSS 外的全面测试：功能回归、按改动量分档的基准、两天两夜的长跑与并发（15 组长跑与串口段合计 731877 次 restore 逐项一致、0 不一致；另有部署件 2 h 滚动长测 147557 次，同样 0 不一致），并用 RPM 重新部署回归通过。
  这里的数字来自软件写保护路径，与 950 口径不同，不能相减。

**对照客户指标的逐档判定在 [`docs/21-benchmarks-and-compliance.md`](docs/21-benchmarks-and-compliance.md)，长跑与并发的实测在 [`docs/22-long-run-and-concurrency.md`](docs/22-long-run-and-concurrency.md)，原生精确增量自己的代价数据在 [`docs/17-native-increment-cost-and-verification.md`](docs/17-native-increment-cost-and-verification.md)**；判定性的实测数字只在这三处维护，尚未覆盖的缺口见 21 §7 与 22 §6。

## 把文档发给别人

去 [Releases](https://github.com/AustinJiangg/e2b-infra/releases) 下载附件，然后把文件发过去就行 —— 对方不需要仓库权限。

当前文档版本 **v0.4.7**（2026-10-15，江路路 j30059180；归档 950 smoke 五项全部通过及宿主、SDK、能力、FC 与正确性证据，汇总四档实测；代码讲解基准仍为 deltabox-dev@57a3063，部署与源码对应仍待补）；本轮本地 HTML 在 `rollback/dist/checkpoint-restore-docs.html`，尚未发布 Release。
每份新产物的封面与侧栏都印着版本号，版本号以 [`docs/README.md`](docs/README.md) 开头的信息块为准；Releases 中的历史附件不会随本地重建自动更新。

| 附件 | 大小 | 给谁 |
|---|---|---|
| `checkpoint-restore-docs.html` | 本地新版约 4.7 MiB | **推荐**。单文件，双击用浏览器打开，不联网、不装任何东西 |
| `checkpoint-restore-docs.zip` | 历史附件 | Markdown 原文包，本轮尚未重建 |
| `checkpoint-restore-docs.tar.gz` | 历史附件 | 同上，Linux 习惯 |
| `checkpoint-restore-slides.pdf` | 历史附件 | 汇报稿，本轮尚未重建 |

HTML 版把全部跨篇链接改写成了页内锚点，mermaid 图内联渲染、
三张 SVG 内嵌在附录，所以怎么传都不会断；浏览器 Ctrl+P 可直接存 PDF。

改完 `docs/` 之后重新生成（在 WSL 工作区 `e2b-repo/` 下跑）：

```bash
python3 tmp/build_docs_html.py e2b-infra/rollback/docs e2b-infra/rollback/diagrams e2b-infra/rollback/slides/node_modules/mermaid/dist/mermaid.min.js e2b-infra/rollback/dist/checkpoint-restore-docs.html --config tmp/books/rollback.json
```

打包与两个校验脚本（`mdlinks.py` 查链接与中文锚点、`mdmermaid.mjs` 查 mermaid 语法）
也在 `e2b-repo/tmp/`，尚未随本仓库同步。生成完新建一个 Release 传上去即可。

## 关于 `scripts/dev/bin/`

四个被测二进制（两个 orchestrator 各 108 MB、两个 Firecracker）**不进本仓库** ——
它们是构建产物，可从 `infra-arm` / `KASandbox` 的对应 commit 重建，且超过 GitHub 的单文件上限。
目录里只保留 `SHA256SUMS`，用来对上「当时验的是哪一版」。二进制本身在 WSL 工作区。

## 不在这里的东西

方案设计稿、原理调研、过程日志与一次性探针仍在 WSL 工作区 `e2b-repo/`，尚未同步到本仓库。
源码在 openEuler 交付仓库 `KASandbox_0904`（交付分支 `deltabox`），不在这里重复存放。
