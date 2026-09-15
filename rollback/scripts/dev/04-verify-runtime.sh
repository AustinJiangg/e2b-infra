#!/usr/bin/env bash
# Post-switch smoke check: is the node actually running the scheme you think,
# with hardware dirty tracking, writing to the filesystem you meant?
#
# Usage: 04-verify-runtime.sh <xfs|ext4> [df-mount]
#
# Starts one throwaway sandbox and kills it. Takes no checkpoint — that is
# what correctness.py is for.
set -uo pipefail
cd "$(dirname "$0")"

SCHEME=${1:?用法: 04-verify-runtime.sh <xfs|ext4> [df挂载点]}
MOUNT=${2:-/}
API=${E2B_API_URL:-http://127.0.0.1:3000}
KEY=${E2B_API_KEY:?请先 export E2B_API_KEY}
LOGS=${LOGS:-/data/nomad/alloc/*/alloc/logs/start.stdout.0}

FAIL=0
ok() { printf '  [ok]   %s\n' "$1"; }
no() { printf '  [FAIL] %s\n' "$1"; FAIL=1; }

echo "=== 1. 跑的是哪个 orchestrator ==="
PID=$(python3 - <<'PY'
import os
# Identified by name in a normal deployment, by its own environment when it
# is a hash-suffixed build artifact.
for p in os.listdir("/proc"):
    if not p.isdigit(): continue
    try:
        base = os.path.basename(os.path.realpath('/proc/%s/exe' % p))
        env = open('/proc/%s/environ' % p, 'rb').read().decode('utf8', 'replace')
    except Exception: continue
    if base.startswith('orchestrator') or base.startswith('template-manager') or 'ORCHESTRATOR_SERVICES=' in env:
        print(p); break
PY
)
if [ -z "$PID" ]; then
	no "找不到运行中的 orchestrator/template-manager 进程"
else
	EXE=$(readlink -f "/proc/$PID/exe")
	printf '  pid=%s exe=%s\n  sha=%s\n' "$PID" "$EXE" "$(sha256sum "$EXE" | cut -c1-16)"
	WANT=$(sha256sum "bin/orchestrator-$SCHEME" | cut -c1-16)
	[ "$(sha256sum "$EXE" | cut -c1-16)" = "$WANT" ] && ok "就是 bin/orchestrator-$SCHEME" \
		|| no "跑的不是 bin/orchestrator-$SCHEME（期望 sha $WANT）"

	echo "=== 2. 进程实际拿到的 env ==="
	ENVV=$(tr '\0' '\n' < "/proc/$PID/environ")
	TDP=$(echo "$ENVV" | sed -n 's/^FC_TRACK_DIRTY_PAGES=//p')
	OBP=$(echo "$ENVV" | sed -n 's/^ORCHESTRATOR_BASE_PATH=//p')
	CFR=$(echo "$ENVV" | sed -n 's/^CHECKPOINT_FULL_ROOT=//p')
	HBO=$(echo "$ENVV" | sed -n 's/^FC_HDBSS_ORDER=//p')
	printf '  FC_TRACK_DIRTY_PAGES=%s  ORCHESTRATOR_BASE_PATH=%s  CHECKPOINT_FULL_ROOT=%s  FC_HDBSS_ORDER=%s\n' \
		"${TDP:-<未设>}" "${OBP:-<未设,默认/orchestrator>}" "${CFR:-<未设,默认true>}" "${HBO:-<未设,默认1>}"
	[ "$TDP" = true ] && ok "脏页跟踪已开" || no "FC_TRACK_DIRTY_PAGES 不是 true —— checkpoint 会全部退化成全量"

	STORE="${OBP:-/orchestrator}/build/checkpoints"
	mkdir -p "$STORE" 2>/dev/null
	FS=$(findmnt -no FSTYPE --target "$STORE" 2>/dev/null)
	printf '  checkpoint store: %s (fstype=%s)\n' "$STORE" "$FS"
	if [ "$SCHEME" = xfs ]; then
		[ "$FS" = xfs ] && ok "store 落在 XFS 上" || no "XFS 套的 store 落在 $FS 上"
		# 别用 xfs_info $STORE —— 它只认挂载点或设备，对普通子目录直接报
		# "Is a directory"，于是在完全正常的机器上也会判成"没开 reflink"。
		# 真正的判据本来也不是 xfs_info 怎么说，是 FICLONE 到底能不能用。
		CT=$(mktemp -d "$STORE/.clonetest.XXXXXX" 2>/dev/null || true)
		if [ -n "$CT" ] && dd if=/dev/zero of="$CT/a" bs=1M count=1 2>/dev/null \
			&& cp --reflink=always "$CT/a" "$CT/b" 2>/dev/null; then
			ok "该 XFS 的 reflink 可用（FICLONE 实测通过）"
		else
			no "该 XFS 上 FICLONE 不可用 —— XFS 套跑不了，先查这个"
		fi
		[ -n "$CT" ] && rm -rf "$CT"
	else
		ok "ext4 套对文件系统无要求（当前 $FS）"
	fi
fi

echo "=== 3. 起一个沙箱，看 FC 选了哪个脏页后端 ==="
# 刚重启过的节点会被 API 判成不健康、拒绝调度一两分钟。不等的话这里必然报
# "Failed to place sandbox"，而原因跟被测代码毫无关系 —— 03-switch.sh 之后
# 直接跑本脚本一定会撞上。
for i in $(seq 1 30); do
	RESP=$(curl -s -X POST "$API/sandboxes" -H 'Content-Type: application/json' \
		-H "X-API-KEY: $KEY" -d '{"templateID":"base","timeout":120}')
	echo "$RESP" | grep -q 'Failed to place sandbox' || break
	[ "$i" = 1 ] && printf '  等 API 接受调度'
	printf '.'
	sleep 6
done
[ "${i:-1}" != 1 ] && echo
SBX=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sandboxID",""))' 2>/dev/null)
if [ -z "$SBX" ]; then
	no "沙箱创建失败: $(echo "$RESP" | head -c 300)"
else
	ok "沙箱 $SBX"
	sleep 2
	SOCK=$(ls /tmp/fc-"$SBX"-*.sock 2>/dev/null | head -1)
	if [ -n "$SOCK" ]; then
		INFO=$(curl -s --unix-socket "$SOCK" http://localhost/ || true)
		printf '  GET / -> %s\n' "$(echo "$INFO" | head -c 300)"
		BACKEND=$(echo "$INFO" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("dirty_tracking","?"))' 2>/dev/null)
		case "$BACKEND" in
			hdbss) ok "脏页后端 = hdbss（硬件标脏生效）" ;;
			kvm-wp) no "脏页后端 = kvm-wp（降级到软件写保护；950 上不该是这个，查 01-check-host.sh 的 cap 502）" ;;
			off) no "脏页后端 = off（没有开脏页跟踪）" ;;
			*) no "读不到 dirty_tracking 字段（FC 二进制可能不是本工具箱的）" ;;
		esac
		FCPID=$(python3 - "$SBX" <<'PY'
import os, sys
sbx = sys.argv[1]
for p in os.listdir("/proc"):
    if not p.isdigit(): continue
    try: cmd = open('/proc/%s/cmdline' % p, 'rb').read().decode('utf8', 'replace')
    except Exception: continue
    if 'firecracker' in cmd and sbx in cmd:
        print(p); break
PY
)
		if [ -n "$FCPID" ]; then
			FCEXE=$(readlink -f "/proc/$FCPID/exe")
			printf '  firecracker: pid=%s exe=%s sha=%s\n' "$FCPID" "$FCEXE" "$(sha256sum "$FCEXE" | cut -c1-16)"
			WANTFC=$(sha256sum "bin/fc-$SCHEME" | cut -c1-16)
			[ "$(sha256sum "$FCEXE" | cut -c1-16)" = "$WANTFC" ] && ok "就是 bin/fc-$SCHEME" \
				|| no "FC 跑的不是 bin/fc-$SCHEME（期望 $WANTFC）—— 检查 FIRECRACKER_VERSIONS_DIR 指向哪个目录"
		fi
	else
		no "找不到 /tmp/fc-$SBX-*.sock，无法查脏页后端"
	fi
	echo "  日志里的 HDBSS 结论："
	grep -h "HDBSS" $LOGS 2>/dev/null | tail -2 | sed 's/^/    /' || echo "    (日志里没有 HDBSS 行)"
	curl -s -X DELETE "$API/sandboxes/$SBX" -H "X-API-KEY: $KEY" >/dev/null 2>&1
	ok "沙箱已删除"
fi

echo
[ "$FAIL" = 0 ] && echo "冒烟全部通过，可以跑 run-all.sh。" || echo "有 [FAIL]，先解决再往下。"
exit $FAIL
