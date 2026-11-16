#!/bin/bash
# 跑 stress_overlay_ckpt.py，带看门狗：操作日志（<outdir>/<mode>-ops.log 与 <mode>.log）超过 STALL 秒没有新行就认为 guest 卡死，
# 从 orchestrator 日志取出 guest 控制台（fc_console.sh），杀掉脚本和沙箱。
#   run_stress_watch.sh <outdir> <stress_overlay_ckpt.py 的其余参数...>
# 例：run_stress_watch.sh <outdir> --template base-gsd --mode stress --rounds 300 --workers 6 --seed 3
# 换脚本：SCRIPT=stress_multitree.py run_stress_watch.sh <outdir> --template base-gsd --mode multitree --rounds 200
#
# 环境变量：
#   PY          解释器，默认 PATH 里的 python3（先激活装好 SDK 依赖的环境，或直接给解释器的完整路径）
#   PYTHONPATH  沿用调用者的环境，本脚本不改；要指向代码仓的 py-sdk 目录（带 checkpoint 接口的 SDK）
#   SCRIPT      被看护的压测脚本（本目录下的文件名），默认 stress_overlay_ckpt.py
#   STALL       判定卡死的停滞秒数，默认 90；并行跑多组或在调试内核上跑时调大
# --mode 的取值决定日志文件名前缀，看门狗靠它找日志，要与被看护脚本实际写的前缀一致。
# 判据：<outdir>/RESULT —— "DONE rc=0 …" 为正常结束，"WEDGED …" 为判定卡死（退出码 2）；否则透传被看护脚本的退出码。
set -uo pipefail
OUT=$1; shift
STALL=${STALL:-90}
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-python3}
mkdir -p "$OUT"
mode=stress; prev=""
for a in "$@"; do [ "$prev" = "--mode" ] 2>/dev/null && mode=$a; prev=$a; done
$PY "$HERE/${SCRIPT:-stress_overlay_ckpt.py}" --outdir "$OUT" "$@" > "$OUT/stdout.log" 2>&1 < /dev/null &
PID=$!
last=$(date +%s); lines=0
while kill -0 $PID 2>/dev/null; do
  sleep 5
  n=$(cat "$OUT/$mode-ops.log" "$OUT/$mode.log" 2>/dev/null | wc -l)
  if [ "$n" != "$lines" ]; then lines=$n; last=$(date +%s); continue; fi
  if [ $(( $(date +%s) - last )) -ge "$STALL" ]; then
    SB=$(grep -o "sandbox [a-z0-9]*" "$OUT/$mode.log" | head -1 | cut -d' ' -f2)
    echo "$(date +%T) 看门狗：${STALL}s 没有进展，判定卡死（sandbox $SB）" | tee -a "$OUT/$mode.log"
    [ -n "$SB" ] && bash "$HERE/fc_console.sh" "$SB" "$OUT/console.txt"
    kill $PID 2>/dev/null
    sleep 2
    [ -n "$SB" ] && $PY - "$SB" "$HERE" <<'EOF'
import os, sys
sys.path[:0] = [sys.argv[2], os.path.dirname(sys.argv[2])]   # e2b_env.py 在本目录或上一级目录
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox
print("kill", sys.argv[1], Sandbox.kill(sys.argv[1]))
EOF
    echo "WEDGED sandbox=$SB" > "$OUT/RESULT"
    exit 2
  fi
done
wait $PID; rc=$?
SB=$(grep -o "sandbox [a-z0-9]*" "$OUT/$mode.log" | head -1 | cut -d' ' -f2)
[ -n "$SB" ] && bash "$HERE/fc_console.sh" "$SB" "$OUT/console.txt"
echo "DONE rc=$rc sandbox=$SB" > "$OUT/RESULT"
exit $rc
