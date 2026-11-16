# 离线 HTML 与构建

本手册与沙箱级手册共用仓库内的 [`rollback/tools/`](../../rollback/tools/README.md)。
生成器、Markdown 链接校验器和依赖清单都复用沙箱级手册的，不需要工作区 `tmp/` 或 Slidev 工程。

## 安装依赖

按[共用工具说明](../../rollback/tools/README.md)安装 Python 依赖；生成手册不需要 Node.js。以下命令均在 `e2b-infra` 仓库根目录执行：

```bash
python3 -m venv rollback/tools/.venv
rollback/tools/.venv/bin/python -m pip install -r rollback/tools/requirements.txt
```

## 校验与生成

```bash
# 校验链接与标题锚点，并试生成一遍检查产物
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh --book criu-rollback --check

# 生成默认产物 criu-rollback/dist/criu-rollback-docs.html
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh --book criu-rollback

# 也可指定输出路径
DOCS_PYTHON="$PWD/rollback/tools/.venv/bin/python" bash rollback/tools/build.sh --book criu-rollback /tmp/criu-rollback-docs.html
```

若当前 `python3` 已有依赖，可省略 `DOCS_PYTHON`。构建入口从自身位置解析仓库内输入，支持在其他当前目录调用。
校验失败时停止构建；本书不执行沙箱级手册的代码规模统计。

## 输入与交付

- 正文为 [`docs/`](../docs/README.md)，书名和仓库链接配置在 [`book.json`](../book.json)。手册不设版本号和日期，也不署名。
- 配图是 `docs/img/` 下的 SVG，由 `rollback/tools/figures/draw_criu_*.py` 生成；正文里不再有 Mermaid 代码块，出现了构建会报错。
- HTML 包含正文、样式、主题切换脚本和内嵌的 SVG，可在离线浏览器打开，也可打印为 PDF。
- HTML 随仓库提交，[单机部署文档](../../single-node-offline-deploy.md)里有下载链接；改了正文就重新生成并一起提交。不直接编辑 HTML，不生成 ZIP / tar.gz。
- 相同输入、依赖和配置应生成相同内容。
- `git_ref` 是手册外链接（脚本、验证摘要等）指向的 GitHub 分支，取 `main`，与被测 gsd 二进制或 SDK 的提交号无关。

构建过程不提交、不推送。920B 验证摘要见 [`validation/`](../validation/README.md)。
