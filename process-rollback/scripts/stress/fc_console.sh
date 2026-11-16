#!/bin/bash
# 从 orchestrator 所在 nomad job 的日志里取出某个沙箱的 guest 串口输出。
# guest 内核崩了、envd 不通、读不了 dmesg 时，只能从这里看 oops/KASAN 报告。
#   fc_console.sh <sandbox_id> [输出文件]
# 每行形如 "<宿主机时间> <guest 控制台原文>"（宿主机时间用来和测试脚本的操作日志对时间）。
#
# 依赖本机的部署方式（RPM 部署、orchestrator 由 nomad 拉起），换机器用环境变量调整：
#   E2B_INFRA_ENV   含 NOMAD_ACL_TOKEN 等变量的环境文件，默认 /opt/e2b-infra/.env
#   NOMAD_JOB       orchestrator 所在的 nomad job 名，默认 template-manager-system
set -uo pipefail
SB=${1:?用法: fc_console.sh <sandbox_id> [输出文件]}
OUT=${2:-/dev/stdout}
set -a; . "${E2B_INFRA_ENV:-/opt/e2b-infra/.env}"; set +a
export NOMAD_TOKEN="$NOMAD_ACL_TOKEN"
for A in $(nomad job allocs -json "${NOMAD_JOB:-template-manager-system}" | python3 -c 'import json,sys; [print(x["ID"]) for x in json.load(sys.stdin)]'); do
  nomad alloc logs "$A" 2>/dev/null
done | grep -a "\"sandbox.id\": \"$SB\"" | grep -a -v '"logger": "hoststats' \
  | sed -E 's/^([0-9T:.+-]+) +\x1b\[[0-9;]*m[A-Z]+\x1b\[0m +/\1 /; s/  \{"service".*//' > "$OUT"
