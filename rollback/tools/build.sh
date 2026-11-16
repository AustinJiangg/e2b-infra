#!/usr/bin/env bash
# 校验并生成快照手册；默认沙箱级，也可指定进程级。
set -euo pipefail

TOOLS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROLLBACK_DIR=$(cd -- "$TOOLS_DIR/.." && pwd)
BOOK_DIR=$ROLLBACK_DIR
DOCS_PYTHON=${DOCS_PYTHON:-python3}

usage() {
  printf '%s\n' '用法：bash rollback/tools/build.sh [--book rollback|criu-rollback] [--check | 输出.html]' \
    '不带参数时写入 rollback/dist/checkpoint-restore-docs.html。' \
    '--book criu-rollback 使用 criu-rollback/docs 和 criu-rollback/book.json，默认输出 criu-rollback/dist/criu-rollback-docs.html。' \
    '--check 校验 Markdown 链接并试生成一遍、检查产物，不留下 HTML。' \
    '可用 DOCS_PYTHON 指定安装了 requirements.txt 的 Python 解释器。'
}

if [ "${1:-}" = --book ]; then
  case "${2:-}" in
    rollback) ;;
    criu-rollback) BOOK_DIR=$(cd -- "$ROLLBACK_DIR/../criu-rollback" && pwd) ;;
    *) usage >&2; exit 2 ;;
  esac
  shift 2
fi
if [ "$#" -gt 1 ]; then usage >&2; exit 2; fi
case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  --check) CHECK_ONLY=1 ;;
  -*) usage >&2; exit 2 ;;
  *) CHECK_ONLY=0 ;;
esac

if ! "$DOCS_PYTHON" -c 'import markdown' 2>/dev/null; then
  printf '%s\n' '缺少 Python 依赖，请按 rollback/tools/README.md 安装 requirements.txt。' >&2
  exit 2
fi

"$DOCS_PYTHON" "$TOOLS_DIR/mdlinks.py" "$BOOK_DIR/docs"
if [ "$BOOK_DIR" = "$ROLLBACK_DIR" ]; then
  "$DOCS_PYTHON" "$TOOLS_DIR/code_stats.py" --check-docs
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  "$DOCS_PYTHON" "$TOOLS_DIR/build_book.py" "$BOOK_DIR" --check
  exit 0
fi

DEFAULT_OUTPUT=$ROLLBACK_DIR/dist/checkpoint-restore-docs.html
if [ "$BOOK_DIR" != "$ROLLBACK_DIR" ]; then
  DEFAULT_OUTPUT=$BOOK_DIR/dist/criu-rollback-docs.html
fi
OUTPUT=${1:-$DEFAULT_OUTPUT}
mkdir -p -- "$(dirname -- "$OUTPUT")"
"$DOCS_PYTHON" "$TOOLS_DIR/build_book.py" "$BOOK_DIR" "$OUTPUT"
