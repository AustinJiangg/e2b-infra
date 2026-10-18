# 离线 HTML 与构建

本手册与沙箱级手册共用仓库内的 [`rollback/tools/`](../../rollback/tools/README.md)。
生成器、Markdown 链接校验器、Mermaid 校验器及依赖锁文件都复用已有版本，不需要工作区 `tmp/` 或 Slidev 工程。

## 安装依赖

按[共用工具说明](../../rollback/tools/README.md)安装 Python 和 Node.js 依赖。以下命令均在 `e2b-infra` 仓库根目录执行：

```bash
python3 -m venv rollback/tools/.venv
rollback/tools/.venv/bin/python -m pip install -r rollback/tools/requirements.txt
npm ci --prefix rollback/tools --ignore-scripts --no-audit --no-fund
```

## 校验与生成

```bash
# 只校验链接、标题锚点和 Mermaid 图
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh --book gsd --check

# 生成默认产物 gsd/dist/gsd-checkpoint-restore-docs.html
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh --book gsd

# 也可指定输出路径
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh --book gsd /tmp/gsd-docs.html
```

若当前 `python3` 已有依赖，可省略 `DOCS_PYTHON`。构建入口从自身位置解析仓库内输入，支持在其他当前目录调用。
校验失败时停止构建；gsd 不执行沙箱级手册的代码规模统计。

## 输入与交付

- 正文为 [`docs/`](../docs/README.md)，书名、版本、日期、署名及仓库链接配置在 [`book.json`](../book.json)。
- `build_docs_html.py` 支持按文件名排序；本手册没有独立 SVG，Mermaid 图保留在各章中。
- HTML 包含正文、样式、交互脚本与 Mermaid，可在离线浏览器打开，也可打印为 PDF。
- HTML 是构建产物，不入库；不直接编辑，不维护 ZIP / tar.gz 打包脚本。
- 同一轮修改同步 `book.json`、总 README 和正文目录中的版本与日期。相同输入、依赖和配置应生成相同内容。
- `git_ref` 是手册外链接的发布基准，与被测 gsd 二进制或 SDK 的提交号分别记录。本次尚未固定发布标签，因此外部相对链接在单文件 HTML 中显示为路径提示。

构建过程不提交、不推送，也不创建 Release。920B 验证摘要见 [`validation/`](../validation/README.md)。
