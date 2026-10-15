#!/usr/bin/env bash
# 校验并生成沙箱级快照手册；所有输入均位于本仓库。
set -euo pipefail

TOOLS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROLLBACK_DIR=$(cd -- "$TOOLS_DIR/.." && pwd)
DOCS_PYTHON=${DOCS_PYTHON:-python3}

usage() {
  printf '%s\n' '用法：bash rollback/tools/build.sh [--check | 输出.html]' \
    '不带参数时写入 rollback/dist/checkpoint-restore-docs.html。' \
    '--check 只校验 Markdown 链接和 Mermaid 图，不生成 HTML。' \
    '可用 DOCS_PYTHON 指定安装了 requirements.txt 的 Python 解释器。'
}

if [ "$#" -gt 1 ]; then usage >&2; exit 2; fi
case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  --check) CHECK_ONLY=1 ;;
  -*) usage >&2; exit 2 ;;
  *) CHECK_ONLY=0 ;;
esac

if ! "$DOCS_PYTHON" -c 'import markdown_it, pygments' 2>/dev/null; then
  printf '%s\n' '缺少 Python 依赖，请按 rollback/tools/README.md 安装 requirements.txt。' >&2
  exit 2
fi
if ! command -v node >/dev/null || [ ! -f "$TOOLS_DIR/node_modules/mermaid/dist/mermaid.min.js" ]; then
  printf '%s\n' '缺少 Node.js 或手册依赖，请按 rollback/tools/README.md 执行 npm ci --prefix rollback/tools。' >&2
  exit 2
fi

"$DOCS_PYTHON" "$TOOLS_DIR/mdlinks.py" "$ROLLBACK_DIR/docs"
"$DOCS_PYTHON" "$TOOLS_DIR/code_stats.py" --check-docs
node "$TOOLS_DIR/mdmermaid.mjs" --node-modules "$TOOLS_DIR/node_modules" "$ROLLBACK_DIR"/docs/*.md
[ "$CHECK_ONLY" -eq 0 ] || exit 0

OUTPUT=${1:-$ROLLBACK_DIR/dist/checkpoint-restore-docs.html}
mkdir -p -- "$(dirname -- "$OUTPUT")"
"$DOCS_PYTHON" "$TOOLS_DIR/build_docs_html.py" \
  "$ROLLBACK_DIR/docs" "$ROLLBACK_DIR/diagrams" \
  "$TOOLS_DIR/node_modules/mermaid/dist/mermaid.min.js" "$OUTPUT" \
  --config "$ROLLBACK_DIR/book.json"
