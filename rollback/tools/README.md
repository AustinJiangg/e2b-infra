# 手册生成与校验工具

本目录用于把 `rollback/docs/` 与 `rollback/diagrams/` 生成为一个可离线打开的 HTML。工具、配置和依赖清单均位于本仓库，不再依赖工作区的 `tmp/`，也不需要安装整个 Slidev 工程。

这是 **WSL / 文档制作机器**上的工具，不是 RPM 或沙箱运行依赖，无需在 950 上安装 Node.js 来运行快照测试。

## 文件

| 文件 | 用途 |
|---|---|
| `../contents.json` | 分篇、章序与文字附录清单；构建时检查遗漏和重复 |
| `../book.json` | 书名、版本、日期、署名、配图和仓库链接基准 |
| `build.sh` | 统一入口：先校验链接与图表，再生成 HTML |
| `build_docs_html.py` | 单文件HTML生成器，按分篇清单生成分组目录与当前章小节导航 |
| `build_article.py`、`build_article_chart.mjs` | 独立价值解读文章生成器；配图内嵌，D3 从正文表格生成实测曲线，入口见[文章说明](../articles/README.md) |
| `diagram_viewer.js`、`diagram_viewer.css` | 图表全屏预览、过渡、滚轮/双指缩放与拖动；构建时内嵌到HTML，无需随交付件单独发送 |
| `mdlinks.py` | 检查相对链接与标题锚点；缺失文件、错误锚点均失败 |
| `mdmermaid.mjs`、`mermaid_env.mjs` | 用 Mermaid 解析所有图表；依赖未装好时返回失败 |
| `requirements.txt` | 固定 Python 依赖版本 |
| `package.json`、`package-lock.json` | 独立的 Mermaid / jsdom 依赖及完整锁文件 |
| `test_tools.py` | 链接、目录清单、导航结构、转义与生成器回归测试 |
| `test_reader.mjs` | 在离线DOM中执行产物脚本，检查明暗切换与选择保存、页内目标、目录展开和手机端收起及图表放大、缩放与拖动交互 |
| `code_stats.py`、`test_code_stats.py` | 固定提交的功能增量统计、Rust 测试拆分、有效行识别与回归检查 |
| `../stats/code_stats_scope.json` | 固定 SHA、逐文件分类及副本排除规则 |
| `../stats/code-volume.json` | 已复核的统计归档；导读与附录 D 表格共用的数据来源 |

旧 `pack_docs.py` 不迁入：新 Release 只附一个 HTML，不再生成或发布 ZIP / tar.gz。

## 安装依赖

在 `e2b-infra` 仓库根目录执行。需要 Bash、Python 3.10+（含 venv / pip），以及满足 `package.json` 要求的 Node.js；已验证环境为 Python 3.12、Node.js 24.20。

```bash
python3 -m venv rollback/tools/.venv
rollback/tools/.venv/bin/python -m pip install -r rollback/tools/requirements.txt
npm ci --prefix rollback/tools --ignore-scripts --no-audit --no-fund
```

安装依赖时需要访问 Python / npm 包源，或者由使用者提前准备离线依赖。工具入库不等于所有第三方依赖都内置；`node_modules/`、虚拟环境和生成的 HTML 仍不入库。

## 校验与构建

在仓库根目录执行：

```bash
# 只检查 Markdown 链接和 全部Mermaid图，不改动 HTML
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh --check

# 构建到新路径，适合核对后再决定是否替换本地产物
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh /tmp/checkpoint-restore-docs.html

# 不带参数则写入默认产物 rollback/dist/checkpoint-restore-docs.html
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh

# 工具自身的回归测试
rollback/tools/.venv/bin/python rollback/tools/test_tools.py
rollback/tools/.venv/bin/python rollback/tools/test_code_stats.py

# 对实际生成的HTML检查分组目录、当前章高亮和全部页内跳转
node rollback/tools/test_reader.mjs rollback/dist/checkpoint-restore-docs.html
```

若现有 `python3` 已安装 `requirements.txt` 中的依赖，可以省略 `DOCS_PYTHON`。脚本按自身位置解析输入，从其他当前目录调用也不依赖工作区的目录布局。

生成器仍支持单独调用，可用 `python3 rollback/tools/build_docs_html.py --help` 查看参数。

## 代码规模统计

HTML 构建会运行 `code_stats.py --check-docs`，核对分类清单、归档和正文统计块一致；这一步不需要 KASandbox 源码仓库。
统计快照只引用固定 SHA，不把本轮文档制作代码或未提交的 `gsd/` 算进回滚功能。

重新统计时需要两个仓库均包含清单里的 Git 对象。以下命令在 `e2b-infra` 根目录运行；仓库可以位于任意路径：

```bash
# 固定提交重算，核对归档，既不切分支也不修改统计结果
python3 rollback/tools/code_stats.py --kasandbox ../KASandbox_0904 --infra . --verify

# 审查并更新 stats/code_stats_scope.json 后，重算和同步导读、附录表格
python3 rollback/tools/code_stats.py --kasandbox ../KASandbox_0904 --infra . --update-docs

# 只检查正文与归档是否一致
python3 rollback/tools/code_stats.py --check-docs
```

未知变更文件和未知脚本范围会导致统计失败，需要人工审查分类；不会自动将其计入生产。
Python docstring 用 AST 识别，其他代码行用固定版本 Pygments 词法识别；字符串按宿主语言计数。
导读与附录中 `code-stats` 标记之间的内容由脚本维护，避免手改数字；完整口径见 [附录 D](../docs/D-code-size.md)。

## 版本与发布

- 每轮手册修改完成后，自动把末位版本号加1（例如`v0.5.0`→`v0.5.1`），小幅文字、配图或阅读功能变更也适用。同一轮校验和重复构建沿用本轮版本，不因单纯重新构建而加号。同步`rollback/book.json`、`rollback/docs/README.md`及当前版本索引，日期使用本轮修改日期。
- `book.json` 的 `git_ref` 是手册外仓库链接的固定基准，**不是被测二进制版本**。本阶段使用不移动的发布标签 `docs-v0.5.6`，使结果摘要与工具链接指向同一发布源码；正文源码基准另在第1章说明。可以用 `DOC_GIT_REF` 显式覆盖，覆盖后生成的文件不再与原发布件逐字节相同。
- 工具不会提交、推送、建标签或上传 Release；构建和发布是两个独立动作。
- 本阶段 Release 上传手册 `checkpoint-restore-docs.html`、独立文章 `sandbox-rollback-article.html` 与两者的 `SHA256SUMS`。GitHub 自动显示的源码 ZIP / tar.gz 入口由平台提供，不是手动上传的附件。
- 迁移工具不修改已发布的 `docs-v0.4.7` 标签或 HTML，也不删除历史 Release 的附件。

构建依赖由锁文件固定；相同正文、配置、依赖和环境变量应生成相同文件。原工作区 `tmp/` 中的旧工具继续保留，供尚未迁移的其他手册使用，但本手册入口不再调用它们。
