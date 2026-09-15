#!/usr/bin/env bash
# Switch the node between the two checkpoint schemes, or put it back.
#
#   03-switch.sh xfs  --yes --base /mnt/xfsdev/orchestrator
#   03-switch.sh ext4 --yes
#   03-switch.sh restore
#   03-switch.sh show                 # what is installed right now, changes nothing
#
# Options:
#   --yes            apply (without it the script only prints what it would do)
#   --base PATH      ORCHESTRATOR_BASE_PATH for this scheme (xfs needs one)
#   --hcl PATH       nomad job file (default: auto-detected from the job name)
#   --job NAME       nomad job to restart (default: auto-detected;
#                    920B calls it template-manager, 950 template-manager-system)
#   --full-root off  ext4 only: root the tree on a diff instead of a full capture
#   --hdbss-order N  per-vCPU HDBSS buffer order (default: leave unset = 1)
#
# Every replaced file is copied to backup/<timestamp>/ first, and `restore`
# puts the most recent backup back. This touches a running deployment: all
# sandboxes on the node die when template-manager restarts.
set -euo pipefail
cd "$(dirname "$0")"

SCHEME=${1:-show}; shift || true
APPLY=0; BASE=""; HCL=""; JOB=""
FULL_ROOT=""; HDBSS_ORDER=""
while [ $# -gt 0 ]; do
	case "$1" in
		--yes) APPLY=1 ;;
		--base) BASE=$2; shift ;;
		--hcl) HCL=$2; shift ;;
		--job) JOB=$2; shift ;;
		--full-root) FULL_ROOT=$2; shift ;;
		--hdbss-order) HDBSS_ORDER=$2; shift ;;
		*) echo "unknown option $1" >&2; exit 2 ;;
	esac
	shift
done

# The job that runs the orchestrator role is not named the same everywhere:
# 920B calls it "template-manager", 950 "template-manager-system". Guessing
# wrong means the restart silently does nothing, so detect it.
detect_job() {
	[ -n "$JOB" ] && return
	local names
	if command -v nomad >/dev/null; then
		[ -f /opt/e2b-infra/.env ] && . /opt/e2b-infra/.env 2>/dev/null
		export NOMAD_TOKEN="${NOMAD_ACL_TOKEN:-${NOMAD_TOKEN:-}}"
		names=$(nomad job status 2>/dev/null | awk 'NR>1 {print $1}' | grep '^template-manager' | head -1)
	fi
	JOB=${names:-template-manager}
}
detect_job

# The hcl usually shares the job's name; fall back to whichever file in
# /opt/e2b-infra/nomad actually carries ORCHESTRATOR_SERVICES.
detect_hcl() {
	[ -n "$HCL" ] && return
	local c
	for c in "/opt/e2b-infra/nomad/$JOB.hcl" /opt/e2b-infra/nomad/template-manager.hcl; do
		[ -f "$c" ] && { HCL=$c; return; }
	done
	HCL=$(grep -l 'ORCHESTRATOR_SERVICES' /opt/e2b-infra/nomad/*.hcl 2>/dev/null | head -1)
	[ -n "$HCL" ] || HCL=/opt/e2b-infra/nomad/template-manager.hcl
}
detect_hcl

# build.sh 的 --nomad-job deploy 跑的不是 /opt/e2b-infra/nomad/ 里那份，而是
# deploy.sh 用 envsubst 渲染出来的 /opt/e2b-infra/rendered/ 副本。只改模板的话
# 文件看着是对的、nomad 拿到的还是旧的 —— 二进制换了、env 没换，checkpoint 会
# 静默退化成全量。所以两份都要改：模板保证以后重渲染不丢，渲染产物保证这次生效。
RENDERED=""
[ -f "/opt/e2b-infra/rendered/$(basename "$HCL")" ] && RENDERED="/opt/e2b-infra/rendered/$(basename "$HCL")"

FCDIRS=$(ls -d /fc-versions/*/ 2>/dev/null || true)
say() { printf '%s\n' "$*"; }
sha() { [ -f "$1" ] && sha256sum "$1" | cut -c1-16 || echo "-"; }

running_fcdir() {
	# Which /fc-versions/<ver> the running orchestrator actually reads. 950 has
	# two of them, so replacing the wrong one is a silent no-op.
	local p e
	for p in /proc/[0-9]*; do
		e=$(readlink "$p/exe" 2>/dev/null) || continue
		case "$e" in *orchestrator*|*template-manager*) ;; *) continue ;; esac
		tr '\0' '\n' < "$p/environ" 2>/dev/null | sed -n 's/^FIRECRACKER_VERSIONS_DIR=//p' | head -1
		return
	done
}

show() {
	say "检测到：nomad job = $JOB   hcl = $HCL"
	local used; used=$(running_fcdir)
	[ -n "$used" ] && say "  运行中的 orchestrator 读的是 FIRECRACKER_VERSIONS_DIR=$used"
	say "当前安装："
	printf '  %-42s %s\n' "/usr/bin/orchestrator" "$(sha /usr/bin/orchestrator)"
	printf '  %-42s %s\n' "/usr/bin/template-manager" "$(sha /usr/bin/template-manager)"
	for d in $FCDIRS; do printf '  %-42s %s\n' "$d/firecracker" "$(sha "$d/firecracker")"; done
	say "本工具箱的二进制："
	for b in bin/orchestrator-xfs bin/orchestrator-ext4 bin/fc-xfs bin/fc-ext4; do
		printf '  %-42s %s\n' "$b" "$(sha "$b")"
	done
	if [ -f "$HCL" ]; then
		say "$HCL 里本工具箱管的 env："
		sed -n '/>>> checkpoint-test >>>/,/<<< checkpoint-test <<</p' "$HCL" | sed 's/^/  /' || true
		grep -nE '^\s*(FC_TRACK_DIRTY_PAGES|ORCHESTRATOR_BASE_PATH|FC_HDBSS_ORDER|CHECKPOINT_FULL_ROOT)\s*=' "$HCL" | sed 's/^/  /' || true
	fi
	if [ -n "$RENDERED" ]; then
		say "$RENDERED（nomad 实际跑的那份）里的同一批 env："
		grep -nE '^\s*(FC_TRACK_DIRTY_PAGES|ORCHESTRATOR_BASE_PATH|FC_HDBSS_ORDER|CHECKPOINT_FULL_ROOT)\s*=' "$RENDERED" | sed 's/^/  /' || say "  （没有）"
	else
		say "⚠ 找不到 /opt/e2b-infra/rendered/$(basename "$HCL")，改 env 可能不会生效"
	fi
}

if [ "$SCHEME" = show ]; then show; exit 0; fi

if [ "$SCHEME" = restore ]; then
	LAST=$(ls -1d backup/*/ 2>/dev/null | tail -1 || true)
	[ -n "$LAST" ] || { echo "没有备份可还原" >&2; exit 1; }
	say "从 $LAST 还原"
	[ "$APPLY" = 1 ] || { say "（预演，加 --yes 才真的改）"; ls -la "$LAST"; exit 0; }
	[ -f "$LAST/orchestrator" ] && { rm -f /usr/bin/orchestrator; cp "$LAST/orchestrator" /usr/bin/orchestrator; chmod +x /usr/bin/orchestrator; }
	[ -f "$LAST/template-manager" ] && { rm -f /usr/bin/template-manager; cp "$LAST/template-manager" /usr/bin/template-manager; chmod +x /usr/bin/template-manager; }
	for f in "$LAST"/fc-*.bin; do
		[ -e "$f" ] || continue
		d=$(basename "$f" .bin); d=${d#fc-}; d=$(echo "$d" | tr '_' '/')
		rm -f "/$d/firecracker"; cp "$f" "/$d/firecracker"; chmod +x "/$d/firecracker"
		say "  恢复 /$d/firecracker"
	done
	[ -f "$LAST/$(basename "$HCL")" ] && cp "$LAST/$(basename "$HCL")" "$HCL" && say "  恢复 $HCL"
	[ -n "$RENDERED" ] && [ -f "$LAST/rendered-$(basename "$HCL")" ] && cp "$LAST/rendered-$(basename "$HCL")" "$RENDERED" && say "  恢复 $RENDERED"
	say "重启 template-manager..."
	(cd /opt/e2b-infra && bash build.sh --nomad-job deploy "$JOB")
	exit 0
fi

case "$SCHEME" in
	xfs)  ORCH=bin/orchestrator-xfs;  FC=bin/fc-xfs ;;
	ext4) ORCH=bin/orchestrator-ext4; FC=bin/fc-ext4 ;;
	*) echo "第一个参数必须是 xfs / ext4 / restore / show" >&2; exit 2 ;;
esac
[ -x "$ORCH" ] || { echo "缺 $ORCH" >&2; exit 1; }
[ -x "$FC" ] || { echo "缺 $FC" >&2; exit 1; }
[ -f "$HCL" ] || { echo "找不到 $HCL，用 --hcl 指定" >&2; exit 1; }

if [ "$SCHEME" = xfs ]; then
	[ -n "$BASE" ] || { echo "XFS 套必须给 --base（指向 XFS 挂载点下的目录）" >&2; exit 1; }
	parent=$(dirname "$BASE")
	[ -d "$parent" ] || { echo "--base $BASE 的上层目录 $parent 不存在，先跑 02-prepare-loop-volume.sh --fs xfs" >&2; exit 1; }
	FS=$(findmnt -no FSTYPE --target "$parent" 2>/dev/null || true)
	[ "$FS" = xfs ] || { echo "--base $BASE 落在 ${FS:-未知文件系统} 上，不是 XFS" >&2; exit 1; }
	# Ask the filesystem, not xfs_info: this openEuler build of xfs_info
	# refuses a plain directory ("Is a directory"), and a successful FICLONE
	# is the only thing that actually matters here.
	mkdir -p "$BASE"
	probe=$(mktemp -d "$BASE/.reflink.XXXXXX") || { echo "$BASE 不可写" >&2; exit 1; }
	dd if=/dev/zero of="$probe/a" bs=4k count=4 2>/dev/null
	if cp --reflink=always "$probe/a" "$probe/b" 2>/dev/null; then
		rm -rf "$probe"
	else
		rm -rf "$probe"
		echo "$BASE 上 FICLONE 不可用（XFS 没开 reflink？）先跑 02-prepare-loop-volume.sh --fs xfs" >&2
		exit 1
	fi
fi

ENVS=(FC_TRACK_DIRTY_PAGES=true)
[ -n "$BASE" ] && ENVS+=("ORCHESTRATOR_BASE_PATH=$BASE")
[ -n "$HDBSS_ORDER" ] && ENVS+=("FC_HDBSS_ORDER=$HDBSS_ORDER")
[ "$FULL_ROOT" = off ] && ENVS+=("CHECKPOINT_FULL_ROOT=false")

say "切换到 [$SCHEME]"
say "  orchestrator: $ORCH ($(sha "$ORCH"))  ->  /usr/bin/{orchestrator,template-manager}"
for d in $FCDIRS; do say "  firecracker:  $FC ($(sha "$FC"))  ->  ${d}firecracker"; done
say "  env:          ${ENVS[*]}"
say "  重启:         cd /opt/e2b-infra && bash build.sh --nomad-job deploy $JOB  （会杀掉本节点所有沙箱）"

if [ "$APPLY" != 1 ]; then say ""; say "预演结束。确认无误后加 --yes 真的执行。"; exit 0; fi

TS=$(date +%Y%m%d-%H%M%S); BK="backup/$TS"; mkdir -p "$BK"
say ""; say "备份到 $BK"
[ -f /usr/bin/orchestrator ] && cp -a /usr/bin/orchestrator "$BK/orchestrator"
[ -f /usr/bin/template-manager ] && cp -a /usr/bin/template-manager "$BK/template-manager"
for d in $FCDIRS; do
	[ -f "${d}firecracker" ] || continue
	tag=$(echo "${d%/}" | sed 's|^/||; s|/|_|g')
	cp -a "${d}firecracker" "$BK/fc-$tag.bin"
done
cp -a "$HCL" "$BK/$(basename "$HCL")"
[ -n "$RENDERED" ] && cp -a "$RENDERED" "$BK/rendered-$(basename "$HCL")"

say "安装二进制"
rm -f /usr/bin/orchestrator /usr/bin/template-manager
cp "$ORCH" /usr/bin/orchestrator && cp "$ORCH" /usr/bin/template-manager
chmod +x /usr/bin/orchestrator /usr/bin/template-manager
for d in $FCDIRS; do
	rm -f "${d}firecracker"; cp "$FC" "${d}firecracker"; chmod +x "${d}firecracker"
done

say "写 env"
python3 hcl_env.py "$HCL" "${ENVS[@]}"
if [ -n "$RENDERED" ]; then
	python3 hcl_env.py "$RENDERED" "${ENVS[@]}"
else
	echo "⚠ 没找到 rendered/ 副本，nomad 可能拿不到新 env —— 切换后务必跑 04-verify-runtime.sh" >&2
fi

# 只调 `nomad job run` 是不够的：换二进制不改 job spec，nomad 看不出区别，
# 于是什么也不做 —— 老进程继续跑在那个已经被删掉的 inode 上，脚本却报"切换完成"。
# 换方案时因为 env 变了才碰巧有效；同一套里换个新构建就是彻底的空操作。
# 所以先停干净、确认进程真的退了（它还占着端口，不退新 alloc 会因端口冲突起不来），
# 再部署，最后核对跑起来的到底是不是刚装的那个。
say "停 template-manager"
(cd /opt/e2b-infra && bash build.sh --nomad-job stop "$JOB" >/dev/null 2>&1) || true

running_pids() {
	local q e
	for q in /proc/[0-9]*; do
		e=$(readlink -f "$q/exe" 2>/dev/null) || continue
		case "$e" in */template-manager*|*/orchestrator*) basename "$q" ;; esac
	done
}
for _ in $(seq 1 30); do
	[ -z "$(running_pids)" ] && break
	sleep 2
done
if [ -n "$(running_pids)" ]; then
	say "  旧进程没退（还占着端口），强制结束：$(running_pids | tr '\n' ' ')"
	# shellcheck disable=SC2046
	kill -9 $(running_pids) 2>/dev/null || true
	sleep 3
fi

say "部署 template-manager"
(cd /opt/e2b-infra && bash build.sh --nomad-job deploy "$JOB")

say "核对跑起来的是不是刚装的"
want=$(sha256sum /usr/bin/template-manager | cut -c1-16)
got=""
for _ in $(seq 1 40); do
	for q in /proc/[0-9]*; do
		e=$(readlink -f "$q/exe" 2>/dev/null) || continue
		case "$e" in */template-manager) got=$(sha256sum "$e" 2>/dev/null | cut -c1-16); break ;; esac
	done
	[ -n "$got" ] && break
	sleep 3
done
if [ "$got" = "$want" ]; then
	say "  ✓ 跑的是 $want"
else
	say "  ✗ 期望 $want，实际 ${got:-<没有进程>} —— 切换没生效，别往下测"
	exit 1
fi

say ""
say "切换完成。下一步：bash 04-verify-runtime.sh $SCHEME <df挂载点>"
