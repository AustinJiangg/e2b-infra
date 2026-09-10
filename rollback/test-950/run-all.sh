#!/usr/bin/env bash
# Run one scheme's full suite and leave a report behind.
# Usage: run-all.sh <xfs|ext4> [df-mount] [loop-count]
set -uo pipefail
cd "$(dirname "$0")"

SCHEME=${1:?用法: run-all.sh <xfs|ext4> [df挂载点] [回滚次数]}
MOUNT=${2:-/}
LOOPS=${3:-200}
TS=$(date +%Y%m%d-%H%M%S)
OUT="reports/$SCHEME-$TS"
mkdir -p "$OUT"

echo "方案=$SCHEME  df挂载点=$MOUNT  回滚次数=$LOOPS"
echo "报告目录: $OUT"
echo

python3 - <<'PY' | tee "$OUT/00-context.txt"
import lib
print("store        :", lib.STORE, "(fstype %s)" % lib.store_fstype())
print("base path    :", lib.BASE_PATH)
print("full root    :", lib.FULL_ROOT)
print("track dirty  :", lib.ENV.get("FC_TRACK_DIRTY_PAGES", "<未设>"))
print("hdbss order  :", lib.ENV.get("FC_HDBSS_ORDER", "<未设,默认1>"))
PY

# A node that just restarted is refused placement by the API for a minute or
# two while it is still marked unhealthy. Without this the first script fails
# with "Failed to place sandbox" for a reason that has nothing to do with the
# code under test. Always hit this right after 03-switch.sh.
echo
echo "########## 等 API 接受调度 ##########"
python3 - <<'PY_WAIT' | tee "$OUT/00-wait.txt"
import lib, sys
sys.exit(0 if lib.wait_for_api() else 1)
PY_WAIT
if [ "${PIPESTATUS[0]}" != 0 ]; then
	echo "API 一直没就绪，停。先查 nomad/api 状态再重跑。"
	exit 1
fi

RC=0
run() {
	local name=$1; shift
	echo; echo "########## $name ##########"
	if "$@" 2>&1 | tee "$OUT/$name.log"; then
		echo "[$name] OK"
	else
		echo "[$name] FAILED"
		RC=1
	fi
}

run 01-correctness python3 correctness.py "$SCHEME" "$MOUNT"
run 02-timing      python3 timing.py "$SCHEME" "$MOUNT"
run 03-loop        python3 loop.py "$SCHEME" "$LOOPS"
# checkpoint 之后 pause/resume，磁盘数据要原样回来。O_DIRECT 读回，绕开 guest
# page cache —— 走缓存会把磁盘层的问题完全盖住，这个 bug 当初就是这么假通过的。
run 05-pause       python3 pause_verify.py
# checkpoint/restore 与原生 create/connect/pause/kill 的组合矩阵：
# 哪些能做、哪些是边界、有没有把原生能力弄坏。
run 06-compat      python3 compat_matrix.py
# 耗时基准：全量 vs 增量各档的分布、虚机冻结窗口、restore、持续速率。
# 前三个脚本回答"对不对"，这个回答"多久" —— 是要拿去对性能指标、给客户看的那份数据。
# 报告另落在 reports/bench-<方案>-<时间戳>/，路径记进本轮汇总。
run 04-bench       python3 bench-ckpt.py "$SCHEME" --out "$OUT/bench"

echo
echo "########## 汇总 ##########" | tee "$OUT/summary.txt"
{
	grep -h "ALL PASS\|ALL CORRECT\|STABLE\|FAILED" "$OUT"/0*.log 2>/dev/null | sort -u
	echo
	echo "--- 链深汇总 ---"
	sed -n '/=== 汇总/,/^JSON/p' "$OUT/02-timing.log" 2>/dev/null
	echo
	echo "--- 稳定性 ---"
	sed -n '/=== 结果/,/总墙钟/p' "$OUT/03-loop.log" 2>/dev/null
	echo
	echo "--- 耗时基准（全量 vs 增量）---"
	sed -n '/^| | 创建 p50/,/^$/p' "$OUT/bench/报告.md" 2>/dev/null
	echo "完整基准报告: $OUT/bench/报告.md"
	echo
	echo "--- 与原生操作的兼容矩阵 ---"
	sed -n '/OK .* 项 \/ REFUSED/,$p' "$OUT/06-compat.log" 2>/dev/null
} | tee -a "$OUT/summary.txt"

echo
[ $RC = 0 ] && echo "全部通过。报告在 $OUT" || echo "有失败项，看 $OUT 里对应的 .log"
exit $RC
