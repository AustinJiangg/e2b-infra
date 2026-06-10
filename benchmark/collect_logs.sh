#!/usr/bin/env bash
# 采集 orchestrator 的 stdout/stderr 日志（Nomad 部署）。
#
# 用法: bash collect_logs.sh [job名,默认 orchestrator] [输出目录,默认 ./orchestrator-logs]
#
# orchestrator 是 system 类型 job，会在每个 client 节点各跑一个 allocation，
# 本脚本会遍历所有 running 状态的 allocation（多节点时日志必须收齐，
# 否则部分沙箱的 trace 会缺失）。
#
# 如果 nomad CLI 不可用，也可以直接到各 client 节点的 Nomad 数据目录拿文件:
#   <nomad data_dir>/alloc/<alloc_id>/alloc/logs/start.stdout.0
#   <nomad data_dir>/alloc/<alloc_id>/alloc/logs/start.stderr.0
set -euo pipefail

JOB="${1:-orchestrator}"
OUT="${2:-./orchestrator-logs}"
TASK="start"   # 与 orchestrator.hcl 中的 task 名一致

command -v nomad >/dev/null 2>&1 || {
  echo "错误: 未找到 nomad CLI。请在 Nomad server 节点执行，或设置 NOMAD_ADDR 指向 server (如 export NOMAD_ADDR=http://<server_ip>:4646)。" >&2
  exit 1
}

ALLOCS="$(nomad job allocs -json "$JOB" | python3 -c '
import json, sys
for a in json.load(sys.stdin):
    if a.get("ClientStatus") == "running":
        print(a["ID"])
')"

if [[ -z "$ALLOCS" ]]; then
  echo "错误: job \"$JOB\" 没有 running 状态的 allocation。请用 nomad job status $JOB 检查。" >&2
  exit 1
fi

mkdir -p "$OUT"
for a in $ALLOCS; do
  echo "采集 allocation $a ..."
  # zap 日志可能写在 stdout 或 stderr，两个都收，parse_report.py 会自动去重
  nomad alloc logs "$a" "$TASK" > "$OUT/${a}.stdout.log" 2>/dev/null || true
  nomad alloc logs -stderr "$a" "$TASK" > "$OUT/${a}.stderr.log" 2>/dev/null || true
done

echo "完成。日志保存在 $OUT/"
grep -l "\[ResumeSandbox\]" "$OUT"/*.log 2>/dev/null || {
  echo "警告: 收集到的日志中没有 [ResumeSandbox] 行 —— 请确认 orchestrator 二进制是用本仓库 patch 构建的。" >&2
}
echo "下一步: python3 parse_report.py $OUT/*.log --expected <数量> --reference reference_sample.csv"
