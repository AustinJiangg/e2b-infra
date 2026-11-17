#!/usr/bin/env bash
# 950 测试总入口（在 e2b-infra 仓库根目录执行），说明见 test950/README.md。
#   bash test950.sh quick | standard | extended | long [--stack sandbox|criu] [--hours N] [--new]
#   bash test950.sh native | compare [--stack sandbox|criu]   （性能对比，见 README 第 6 节）
#   bash test950.sh status
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
for p in "${TEST950_PYTHON:-}" python3 /usr/bin/python3; do
  if [ -n "$p" ] && command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; assert sys.version_info >= (3, 8)' 2>/dev/null; then
    exec "$(command -v "$p")" "$ROOT/test950/runner.py" "$@"
  fi
done
echo '找不到 3.8 以上的 python3；用 TEST950_PYTHON=解释器路径 指定' >&2
exit 1
