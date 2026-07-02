#!/usr/bin/env bash
# 采集 orchestrator 的 stdout/stderr 日志（Nomad 部署）到本次压测的运行目录。
#
# 用法: bash collect_logs.sh [job名,默认自动探测] [--run-dir 运行目录]
#
# 默认把日志写到最近一次 run_benchmark.py 创建的运行目录下的 orchestrator-logs/，
# 运行目录依次按 --run-dir > 环境变量 BENCH_RUN_DIR > runs/.latest 解析。
#
# 注意：本单机部署里没有单独的 orchestrator job —— orchestrator 与 template-manager
# 是同一个二进制、并在 template-manager-system 这个 system job 里一起跑
# （template-manager.hcl: ORCHESTRATOR_SERVICES=orchestrator,template-manager），
# [ResumeSandbox] 等 trace 就来自它。脚本默认在候选 job 里自动挑存在的那个，
# 也可把 job 名作为第一个参数显式指定。system job 会在每个 client 节点各跑一个
# allocation，脚本会遍历所有 running 的 allocation（多节点时日志必须收齐，
# 否则部分沙箱的 trace 会缺失）。
#
# 本部署启用了 Nomad ACL，nomad 调用需要 token；请先 export NOMAD_TOKEN=<token>
# （或提供部署 .env 里的 NOMAD_ACL_TOKEN），否则会报 403 Permission denied。
#
# 如果 nomad CLI 不可用，也可以直接到各 client 节点的 Nomad 数据目录拿文件:
#   <nomad data_dir>/alloc/<alloc_id>/alloc/logs/start.stdout.0
#   <nomad data_dir>/alloc/<alloc_id>/alloc/logs/start.stderr.0
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNS_ROOT="$SCRIPT_DIR/runs"
TASK="start"   # 与 template-manager.hcl 中的 task 名一致

# ---- 解析参数：可选 --run-dir，其余位置参数当作 JOB ----
# 默认不写死单一 job 名：显式传参则只采集指定的那个（JOB_EXPLICIT=1）；
# 不传则在候选里自动挑第一个存在的（见下方“预检 + 解析 job”）。
JOB=""
JOB_EXPLICIT=0
JOB_CANDIDATES=(template-manager-system orchestrator orchestrator-system)
RUN_DIR_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir) RUN_DIR_FLAG="${2:?--run-dir 需要一个目录参数}"; shift 2 ;;
    --run-dir=*) RUN_DIR_FLAG="${1#*=}"; shift ;;
    *) JOB="$1"; JOB_EXPLICIT=1; shift ;;
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

# ---- Nomad ACL token ----
# 本部署默认启用了 Nomad ACL（e2b-deploy/dep/default.hcl: acl.enabled=true），
# 所有 nomad 调用都必须携带有效 token，否则返回 403 Permission denied。
# 分层解析，命中即止（越靠前优先级越高），命中后 export 给后续所有 nomad 子命令复用：
#   1) 已 export 的 NOMAD_TOKEN（显式覆盖）
#   2) 环境变量 NOMAD_ACL_TOKEN（部署脚本惯用名）
#   3) benchmark/.env 里的 NOMAD_TOKEN / NOMAD_ACL_TOKEN（用 sync-env.sh 刷新它）
#   4) ${NOMAD_DATA_DIR:-/data/nomad}/acl.token（bootstrap 持久化的权威 token，永不过期）
# 只按需读取指定键、不整体 source .env：既避免 .env 里的空值把已有 token 冲掉，
# 也不执行 .env 里的任意内容。同步过一次后就能裸跑，不必每次手动 export。
ENV_FILE="$SCRIPT_DIR/.env"
env_file_get() {  # 从 .env 取某键的值（去掉可选 export 前缀与首尾引号，多条时取最后一条）
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?$1=//p" "$ENV_FILE" \
    | tail -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
nomad_token=""
for cand in \
  "${NOMAD_TOKEN:-}" \
  "${NOMAD_ACL_TOKEN:-}" \
  "$(env_file_get NOMAD_TOKEN)" \
  "$(env_file_get NOMAD_ACL_TOKEN)"; do
  if [[ -n "$cand" ]]; then nomad_token="$cand"; break; fi
done
if [[ -z "$nomad_token" ]]; then
  acl_file="${NOMAD_DATA_DIR:-/data/nomad}/acl.token"
  [[ -r "$acl_file" ]] && nomad_token="$(tr -d '[:space:]' < "$acl_file")"
fi
[[ -n "$nomad_token" ]] && export NOMAD_TOKEN="$nomad_token"

# ---- 预检 + 解析 job ----
# 用 `nomad job status`（纯文本表格）列出集群里真实存在的 job：
#   * 纯文本第一列固定就是 job ID，跨 Nomad 版本稳定，不依赖 `-json` 的结构
#     （本部署 `nomad job status -json` 的结构里 job 名在 .Allocations[].JobID，
#      不是顶层 .ID，早先按顶层 .ID 解析会得到空列表 → “当前 job: <无>”）；
#   * 同时验证连通性 / ACL token（失败时区分「没 token 的 403」和其它错误）。
# 再按候选名 / 关键词自动挑出跑 orchestrator 的 job（本部署是 template-manager-system）。
jobs_txt="$(mktemp)"
alloc_json="$(mktemp)"
tmp_err="$(mktemp)"
trap 'rm -f "$jobs_txt" "$alloc_json" "$tmp_err"' EXIT

if ! nomad job status >"$jobs_txt" 2>"$tmp_err"; then
  echo "错误: 连接 Nomad 失败:" >&2
  sed 's/^/  /' "$tmp_err" >&2
  if grep -qiE '403|permission denied|\bACL\b' "$tmp_err"; then
    echo >&2
    echo "  原因: 本部署启用了 Nomad ACL，nomad 调用必须携带有效 token（脚本没在 env / .env /" >&2
    echo "        \${NOMAD_DATA_DIR:-/data/nomad}/acl.token 里找到）。" >&2
    echo "  修复: 同步一次凭据（推荐，之后裸跑即可）:  bash sync-env.sh" >&2
    echo "        或临时手动导出:  export NOMAD_TOKEN=<你的 nomad ACL token>" >&2
  fi
  exit 1
fi

# 集群里所有 job ID（`nomad job status` 表格第一列，跳过表头/空行）
mapfile -t ALL_JOBS < <(awk 'NR>1 && $1 != "" && $1 != "No" { print $1 }' "$jobs_txt")

in_list() {
  local x
  [[ ${#ALL_JOBS[@]} -gt 0 ]] || return 1
  for x in "${ALL_JOBS[@]}"; do [[ "$x" == "$1" ]] && return 0; done
  return 1
}

if [[ "$JOB_EXPLICIT" -eq 1 ]]; then
  if ! in_list "$JOB"; then
    echo "错误: 指定的 job \"$JOB\" 不在当前 Nomad 集群里。" >&2
    printf '  当前 job: %s\n' "${ALL_JOBS[*]:-<无>}" >&2
    exit 1
  fi
else
  # 1) 先按候选精确名匹配
  for c in "${JOB_CANDIDATES[@]}"; do
    if in_list "$c"; then JOB="$c"; break; fi
  done
  # 2) 匹配不到再按关键词模糊匹配（名字里带 orchestrat / template-manager 的）
  if [[ -z "$JOB" && ${#ALL_JOBS[@]} -gt 0 ]]; then
    for x in "${ALL_JOBS[@]}"; do
      case "$x" in *orchestrat*|*template-manager*) JOB="$x"; break ;; esac
    done
  fi
  if [[ -z "$JOB" ]]; then
    echo "错误: 没在当前 Nomad 集群里找到 orchestrator/template-manager 对应的 job。" >&2
    echo "  试过的候选名: ${JOB_CANDIDATES[*]}" >&2
    printf '  当前集群里的 job: %s\n' "${ALL_JOBS[*]:-<无>}" >&2
    if [[ ${#ALL_JOBS[@]} -eq 0 ]]; then
      echo "  集群里一个 job 都没列出来：确认 token 权限足够、且 orchestrator 确实以" >&2
      echo "  Nomad job 形式部署（本仓库单机部署用的是 template-manager-system）。" >&2
      echo "  也可以绕过 nomad，直接从各 client 节点的数据目录取日志：" >&2
      echo "    <nomad data_dir>/alloc/<alloc_id>/alloc/logs/start.stdout.0（stderr 同理）" >&2
    else
      echo "  请从上面挑出跑 orchestrator/template-manager 的 job，作为第一个参数传入：" >&2
      echo "    bash collect_logs.sh <job名>" >&2
    fi
    exit 1
  fi
  echo "使用 job: $JOB（自动选中，可用第一个参数覆盖）"
fi

# ---- 取该 job 所有 running 状态的 allocation ----
if ! nomad job allocs -json "$JOB" >"$alloc_json" 2>"$tmp_err"; then
  echo "错误: 获取 job \"$JOB\" 的 allocation 列表失败:" >&2
  sed 's/^/  /' "$tmp_err" >&2
  exit 1
fi

ALLOCS="$(python -c '
import json, sys
for a in json.load(sys.stdin):
    if a.get("ClientStatus") == "running":
        print(a["ID"])
' < "$alloc_json")"

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
echo "下一步: python parse_report.py   # 自动读取本次运行目录并按时间窗口隔离本次沙箱"
