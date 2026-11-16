# 手册生成与校验工具

本目录用于把 `rollback/docs/`（以及 `criu-rollback/docs/`）生成为一个可离线打开的 HTML，并存放两本手册配图的画图脚本。工具、配置和依赖清单均位于本仓库，不再依赖工作区的 `tmp/`，也不需要安装整个 Slidev 工程。

这是 **WSL / 文档制作机器**上的工具，不是 RPM 或沙箱运行依赖。生成手册只需要 Python；Node.js 只有独立文章（`build_article.py`）用到。

## 文件

| 文件 | 用途 |
|---|---|
| `../contents.json` | 分篇、章序与文字附录清单；构建时检查遗漏和重复 |
| `../book.json` | 书名和仓库链接，键见 `build_book.py` 文件头 |
| `build.sh` | 统一入口：先校验链接，再生成 HTML 并检查产物 |
| `build_book.py` | 手册构建：按分篇清单排目录、把手册外链接改写成 GitHub 地址、拒绝 Mermaid 代码块，调用 `site/` 排版，最后检查产物（生成器警告、未解析链接、未内嵌图片、页内跳转目标都须为 0） |
| `site/` | 排版生成器，复制自 md-docs-site skill，不在这里修改，来源见 `site/README.md` |
| `figures/` | 两本手册配图的画图脚本，用法见 [`figures/README.md`](figures/README.md) |
| `build_article.py`、`build_article_chart.mjs` | 独立价值解读文章生成器；配图内嵌，D3 从正文表格生成实测曲线，入口见[文章说明](../articles/README.md) |
| `mdlinks.py` | 检查相对链接与标题锚点；缺失文件、错误锚点均失败 |
| `requirements.txt` | 固定 Python 依赖版本 |
| `package.json`、`package-lock.json` | 独立文章用的 Node 依赖锁文件；生成手册不需要 |
| `test_tools.py` | 链接、目录清单、导航顺序、图片内嵌、手册外链接改写、Mermaid 拒绝、转义与可重现的回归测试 |
| `code_stats.py`、`test_code_stats.py` | 固定提交的功能增量统计、Rust 测试拆分、有效行识别与回归检查 |
| `../stats/code_stats_scope.json` | 固定 SHA、逐文件分类及副本排除规则 |
| `../stats/code-volume.json` | 已复核的统计归档；导读与附录 D 表格共用的数据来源 |

不生成 ZIP / tar.gz：交付只有单文件 HTML。

## 安装依赖

在 `e2b-infra` 仓库根目录执行。需要 Bash、Python 3.10+（含 venv / pip）；已验证环境为 Python 3.12。只有构建独立文章时才需要满足 `package.json` 要求的 Node.js。

```bash
python3 -m venv rollback/tools/.venv
rollback/tools/.venv/bin/python -m pip install -r rollback/tools/requirements.txt
```

构建独立文章时再装 Node 依赖：`npm ci --prefix rollback/tools --ignore-scripts --no-audit --no-fund`。

安装依赖时需要访问 Python / npm 包源，或者由使用者提前准备离线依赖。工具入库不等于所有第三方依赖都内置；`node_modules/`、虚拟环境和生成的 HTML 仍不入库。

## 校验与构建

在仓库根目录执行：

```bash
# 检查 Markdown 链接，并试生成一遍、检查产物，不留下 HTML
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh --check

# 构建到新路径，适合核对后再决定是否替换本地产物
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh /tmp/checkpoint-restore-docs.html

# 不带参数则写入默认产物 rollback/dist/checkpoint-restore-docs.html
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh

# 工具自身的回归测试
rollback/tools/.venv/bin/python rollback/tools/test_tools.py
rollback/tools/.venv/bin/python rollback/tools/test_code_stats.py

```

若现有 `python3` 已安装 `requirements.txt` 中的依赖，可以省略 `DOCS_PYTHON`。脚本按自身位置解析输入，从其他当前目录调用也不依赖工作区的目录布局。

构建也可以单独调用：`python3 rollback/tools/build_book.py rollback /tmp/checkpoint-restore-docs.html`，参数和 `book.json` 的键见该文件头。

进程级手册复用本目录的生成器、校验器、画图脚本和依赖，不另维护工具副本：

```bash
bash rollback/tools/build.sh --book criu-rollback --check
bash rollback/tools/build.sh --book criu-rollback
```

输入为 `criu-rollback/docs/`、`criu-rollback/book.json`，默认输出 `criu-rollback/dist/criu-rollback-docs.html`。
gsd 不执行沙箱级代码规模统计；默认不带 `--book` 的行为保持不变。

## 代码规模统计

HTML 构建会运行 `code_stats.py --check-docs`，核对分类清单、归档和正文统计块一致；这一步不需要 KASandbox 源码仓库。
统计快照只引用固定 SHA，不把本轮文档制作代码或未提交的 `criu-rollback/` 算进回滚功能。

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

## 链接与产物

- 手册不设版本号和日期，也不署名；`book.json` 只有书名、分篇清单和仓库链接配置。
- `book.json` 的 `git_ref` 是手册外仓库链接（脚本、部署文档等）指向的分支，取 `main`，**不是被测二进制版本**；正文源码基准另在第 1 章说明。可以用 `DOC_GIT_REF` 临时覆盖。
- 生成的 HTML 随仓库提交：沙箱级是 `rollback/dist/checkpoint-restore-docs.html`，进程级是 `criu-rollback/dist/criu-rollback-docs.html`，[单机部署文档](../../single-node-offline-deploy.md)里有下载链接。改了正文就重新生成并一起提交。
- 工具不会提交或推送。

构建依赖由锁文件固定；相同正文、配置、依赖和环境变量应生成相同文件。原工作区 `tmp/` 中的旧工具继续保留，供尚未迁移的其他手册使用，但本手册入口不再调用它们。
