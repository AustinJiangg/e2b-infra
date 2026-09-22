#!/usr/bin/env bash
# 第 1 轮验收组合（小规模）：T25 时间与 CPU 健康 → T26 串口（F1 回归）
# → T14 写密集并发正确性 → T13 冻结窗口干扰曲线。
#
# 在宿主机（920B / 950）上跑，跑之前先按 stack-components-checklist.md 确认栈。
# 全部串行，总时长大约 12–15 分钟。任何一个用例失败就停（set -e），
# 失败的那个会留现场（--keep-on-failure），沙箱 id 与 checkpoint 目录打在日志里。
#
#   bash run-r1.sh                 # 默认
#   PY=/usr/bin/python3 bash run-r1.sh
#   OUT=/tmp/r1 bash run-r1.sh

set -euo pipefail

PY="${PY:-python3}"
OUT="${OUT:-r1-$(date +%Y%m%d-%H%M%S)}"
ENV_FILE="${ENV_FILE:-${CRTEST_ENV_FILE:-${E2B_DEPLOY_DIR:-/opt/e2b-infra}/.env}}"
COMMON=(--env-file "$ENV_FILE" --keep-on-failure)

mkdir -p "$OUT"
cd "$(dirname "$0")"

echo "== 第 1 轮验收组合 =="
echo "python   : $PY"
echo "输出目录 : $OUT"
echo

run() {          # run <用例> <参数...>
  local case="$1"; shift
  echo "----- $case -----"
  "$PY" -m crtest "$case" "${COMMON[@]}" --out "$OUT/$case.json" "$@" \
      2>&1 | tee "$OUT/$case.log"
}

run T25 --rounds 5 --settle 10 --gap 3
run T26 --rounds 10
run T14 --sandboxes 2 --rounds 1 --seconds 6 --mem-mb 256
run T13 --fanout 1,2 --seconds 45 --every 5

echo
echo "全部通过。原始数据在 $OUT/（*.json 与 *.log）"
