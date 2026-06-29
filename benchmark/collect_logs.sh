#!/usr/bin/env bash
# 采集 orchestrator 的 stdout/stderr 日志（Nomad 部署）到本次压测的运行目录。
#
# 用法: bash collect_logs.sh [job名,默认 orchestrator] [--run-dir 运行目录]
#
# 默认把日志写到最近一次 run_benchmark.py 创建的运行目录下的 orchestrator-logs/，
# 运行目录依次按 --run-dir > 环境变量 BENCH_RUN_DIR > runs/.latest 解析。
#
# orchestrator 是 system 类型 job，会在每个 client 节点各跑一个 allocation，
# 本脚本会遍历所有 running 状态的 allocation（多节点时日志必须收齐，
# 否则部分沙箱的 trace 会缺失）。
#
# 如果 nomad CLI 不可用，也可以直接到各 client 节点的 Nomad 数据目录拿文件:
#   <nomad data_dir>/alloc/<alloc_id>/alloc/logs/start.stdout.0
#   <nomad data_dir>/alloc/<alloc_id>/alloc/logs/start.stderr.0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNS_ROOT="$SCRIPT_DIR/runs"
TASK="start"   # 与 orchestrator.hcl 中的 task 名一致

# ---- 解析参数：可选 --run-dir，其余位置参数当作 JOB ----
JOB="orchestrator"
RUN_DIR_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) RUN_DIR_FLAG="${2:?--run-dir 需要一个目录参数}"; shift 2 ;;
    --run-dir=*) RUN_DIR_FLAG="${1#*=}"; shift ;;
    *) JOB="$1"; shift ;;
  esac
done

# ---- 定位运行目录：--run-dir > BENCH_RUN_DIR > runs/.latest ----
RUN_DIR=""
if [[ -n "$RUN_DIR_FLAG" ]]; then
  RUN_DIR="$RUN_DIR_FLAG"
elif [[ -n "${BENCH_RUN_DIR:-}" ]]; then
  RUN_DIR="$BENCH_RUN_DIR"
else
  ptr="$RUNS_ROOT/.latest"
  if [[ -f "$ptr" ]]; then
    name="$(tr -d '[:space:]' < "$ptr")"
    [[ -n "$name" && -d "$RUNS_ROOT/$name" ]] && RUN_DIR="$RUNS_ROOT/$name"
  fi
fi
if [[ -z "$RUN_DIR" || ! -d "$RUN_DIR" ]]; then
  echo "错误: 未找到基准测试运行目录。" >&2
  echo "  请先运行 run_benchmark.py（会创建 runs/run_<时间戳>/ 并记录 runs/.latest），" >&2
  echo "  或用 --run-dir <目录> 显式指定，或设置环境变量 BENCH_RUN_DIR=<目录>。" >&2
  exit 1
fi
OUT="$RUN_DIR/orchestrator-logs"

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
echo "下一步: python3 parse_report.py --reference reference_sample.csv   # 自动读取本次运行目录（时钟不同步时加 --last <数量>）"
