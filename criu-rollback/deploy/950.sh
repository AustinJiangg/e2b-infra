#!/usr/bin/env bash
# 进程级快照回滚的部署与切换入口，在 e2b-infra 仓库里执行（沙箱级已按 single-node-offline-deploy.md 部署好）。
#   首次：bash criu-rollback/deploy/950.sh all      日常：bash criu-rollback/deploy/950.sh criu | sandbox
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")" && pwd)
export CRIU_DEPLOY="$ROOT"
export PYTHONDONTWRITEBYTECODE=1

load_site() {
  if [ -f "$ROOT/ops/site.env" ]; then
    set -a; source "$ROOT/ops/site.env"; set +a
  fi
  export E2B_INFRA_ENV="${E2B_INFRA_ENV:-/opt/e2b-infra/.env}"
  # Nomad 令牌以服务端环境文件为准（重新部署 build.sh -s 会换令牌，shell 里留着的旧 NOMAD_TOKEN
  # 就失效了），文件里没有时才用 shell 里的。只取令牌，不把其余服务端变量带进客户端脚本。
  if [ -r "$E2B_INFRA_ENV" ]; then
    _tok=$(unset NOMAD_TOKEN NOMAD_ACL_TOKEN; set +eu; set -a; source "$E2B_INFRA_ENV" >/dev/null 2>&1; echo "${NOMAD_ACL_TOKEN:-${NOMAD_TOKEN:-}}") || true
    [ -n "$_tok" ] && NOMAD_TOKEN=$_tok
    unset _tok
  fi
  export NOMAD_TOKEN="${NOMAD_TOKEN:-}"
}

host_python() {
  # 运维脚本只用标准库，任何 3.8 以上的 python3 都可以，不需要激活 conda。
  local p
  for p in "${CRIU_HOST_PYTHON:-}" python3 /usr/bin/python3; do
    if [ -n "$p" ] && command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; assert sys.version_info >= (3, 8)' 2>/dev/null; then
      command -v "$p"; return
    fi
  done
  echo '找不到 3.8 以上的 python3；在 ops/site.env 写 CRIU_HOST_PYTHON=解释器路径' >&2
  exit 1
}

need_root() {
  [ "$(id -u)" = 0 ] || { echo '现场检查和切换请使用 root 账号' >&2; exit 1; }
}

case "${1:-help}" in
  verify)
    cd "$ROOT"
    sha256sum --quiet -c SHA256SUMS
    echo '部署目录 SHA256 全部通过'
    ;;
  status|preflight|save-baseline|install)
    need_root; load_site
    exec "$(host_python)" "$ROOT/ops/stack.py" "$1"
    ;;
  unpack)
    exec "$(host_python)" "$ROOT/ops/unpack.py"
    ;;
  all|criu|sandbox|recycle-netns|report|install-sdk|quick|acceptance|stress|stress-ext|soak|smoke)
    need_root; load_site
    exec "$(host_python)" "$ROOT/ops/flow.py" "$@"
    ;;
  *)
    cat <<'USAGE'
用法: bash criu-rollback/deploy/950.sh <命令>（在 e2b-infra 仓库根目录执行）
  all            首次全流程，一条命令做完，结束时把结果打印到终端；中途停下后再跑同一条命令会接着做
  criu           切到进程级（等到能建沙箱，base-criu 不可用时自动重建）
  sandbox        切回沙箱级（等到原模板能建沙箱）
  status         看当前是哪一套、e2b 沙箱数、e2b netns 数
  recycle-netns  清理重启留下的 e2b 网络命名空间与 iptables 规则，按当前栈重新拉起（约 1～2 分钟）
  report [acceptance|stress|smoke|netcheck|log|handbook|files] [--file 相对路径] [--lines N]
                 把最近一次 all 的结果打印到终端，便于复制
切栈和清理之前会先删掉全部 e2b 沙箱；同机 AgentENV 的沙箱不碰。
以下是 all 里的单步，一般不需要单独用：
  verify  unpack  install-sdk  preflight  save-baseline  install  acceptance  stress  smoke
以下是 all 之外的测试（要先在进程级栈上，即先执行 criu；平时用仓库根的 test950.sh 统一调度）：
  quick          部署自检 + verify_gsd，约 2 分钟
  stress-ext     八组压测各 1000 轮、多种子、兼容矩阵，约 40 分钟
  soak [--hours N]  一个沙箱里按时长连续压测，默认 4 小时
USAGE
    ;;
esac
