#!/usr/bin/env bash
# 950 vs 920B 差异探测 —— 静态部分（宿主机，不需要栈起着，不建沙箱）。
#
# 设计约束（950 没网、可能缺工具）：
#   · 零外部依赖：只用 coreutils/bash；python3、gcc、nvme、jq 等一律「有就用，没有就写 <absent>」
#   · 任何一项失败都不许中断：全程 `set -u`（没有 -e），每条外部命令带 timeout
#   · 每项输出一行 `KEY=VALUE`，缺失写 `KEY=<absent>`；同内容再出一份 JSON 便于 compare.py 逐键 diff
#   · 只写两个地方：results/ 下的报告，和产物盘上一个用完即删的 fsync 微基准临时目录
#
# 用法:
#   bash probe-host.sh                 # 结果写到脚本同级的 results/
#   bash probe-host.sh -o /some/dir    # 换输出目录
#   bash probe-host.sh --skip-fsync    # 不跑 fsync 微基准（唯一会写盘的探测）
#
# 输出: results/probe-host-<hostname>-<date>.txt / .json
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
OUTDIR="$HERE/results"
DO_FSYNC=1
FSYNC_N=200
FSYNC_THREADS=16

while [ $# -gt 0 ]; do
	case "$1" in
	-o | --out) OUTDIR=$2; shift 2 ;;
	--skip-fsync) DO_FSYNC=0; shift ;;
	--fsync-n) FSYNC_N=$2; shift 2 ;;
	-h | --help)
		sed -n '2,20p' "$0"
		exit 0
		;;
	*) echo "未知参数: $1（-h 看用法）" >&2; exit 2 ;;
	esac
done

mkdir -p "$OUTDIR" 2>/dev/null
HOST=$(hostname 2>/dev/null || echo unknown)
DATE=$(date +%Y%m%d-%H%M%S 2>/dev/null || echo nodate)
KV="$OUTDIR/probe-host-$HOST-$DATE.txt"
JSON="$OUTDIR/probe-host-$HOST-$DATE.json"
: >"$KV"

ABSENT='<absent>'
T() { timeout "${1}" "${@:2}" 2>/dev/null; }   # T <秒> <命令...>

# 值净化：去掉 CR 与其它控制字符（否则 JSON 里会留下非法字符，json.load 直接报
# "Invalid control character"——`pgrep -c` 那种「输出 0 又 echo 0」的两行值踩到过）、
# 把换行折成 " | "、压掉首尾空白。空值 → <absent>。
# 换行的替换用 bash 的 ${v//} 而不是 sed：sed 的 BRE 里写不了字面控制字符，
# `s/\001/ | /g` 在 GNU sed 下并不匹配那个字节。
san() {
	local v sep
	sep=$(printf '\036')
	v=$(printf '%s' "$1" | tr -d '\r' | tr -d '\000-\010\013\014\016-\037' | tr '\n' "$sep")
	v=${v//"$sep"/ | }
	v=$(printf '%s' "$v" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/[[:space:]]\{2,\}/ /g; s/ | $//')
	[ -z "$v" ] && v=$ABSENT
	printf '%s' "$v"
}

emit() { printf '%s=%s\n' "$1" "$(san "${2-}")" >>"$KV"; }

# 多行输出 → KEY.01 / KEY.02 …（最多 $4 行），一行都没有时 KEY.00=<absent>
emit_lines() {
	local key=$1 text=$2 max=${3:-30} i=0 line
	if [ -z "$text" ]; then emit "$key.00" "$ABSENT"; return; fi
	while IFS= read -r line; do
		[ -z "$line" ] && continue
		i=$((i + 1))
		[ "$i" -gt "$max" ] && break
		emit "$(printf '%s.%02d' "$key" "$i")" "$line"
	done <<EOF
$text
EOF
	[ "$i" = 0 ] && emit "$key.00" "$ABSENT"
	return 0
}

have() { command -v "$1" >/dev/null 2>&1; }
say() { printf '\n[%s]\n' "$1" >&2; }

echo "probe-host.sh  host=$HOST  date=$DATE" >&2
emit probe.script_version "1.0"
emit probe.generated_at "$(date -Is 2>/dev/null || date)"

# ════════════════════════════════════════════════════ 1. 身份
say "1/8 身份与 CPU"
emit host.hostname "$HOST"
emit host.kernel "$(uname -r 2>/dev/null)"
emit host.arch "$(uname -m 2>/dev/null)"
emit host.os "$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | tr -d '"')"
emit host.uptime_s "$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
emit host.boot_cmdline "$(head -c 1500 /proc/cmdline 2>/dev/null)"

CPUMODEL=$(sed -n 's/^[Mm]odel name[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo 2>/dev/null | head -1)
if [ -z "$CPUMODEL" ] || [ "$CPUMODEL" = "-" ]; then
	CPUMODEL=$(T 20 lscpu | sed -n 's/^BIOS Model name: *//p' | head -1)
fi
if [ -z "$CPUMODEL" ] || [ "$CPUMODEL" = "-" ]; then
	CPUMODEL=$(T 20 lscpu | sed -n 's/^Model name: *//p' | head -1)
fi
emit cpu.model "$CPUMODEL"
for f in implementer architecture variant part revision; do
	emit "cpu.midr_$f" "$(sed -n "s/^CPU $f[[:space:]]*:[[:space:]]*//p" /proc/cpuinfo 2>/dev/null | head -1)"
done
emit cpu.count "$(grep -c '^processor' /proc/cpuinfo 2>/dev/null)"
emit cpu.features "$(sed -n 's/^Features *: *//p' /proc/cpuinfo 2>/dev/null | head -1)"
if have lscpu; then
	LSCPU=$(T 20 lscpu)
	for pair in "threads_per_core:Thread(s) per core" "cores_per_socket:Core(s) per socket" \
		"sockets:Socket(s)" "numa_nodes:NUMA node(s)" "model_name:Model name" \
		"bios_model_name:BIOS Model name" "bogomips:BogoMIPS" "byte_order:Byte Order" "l3_cache:L3 cache"; do
		k=${pair%%:*}; label=${pair#*:}
		emit "cpu.$k" "$(printf '%s\n' "$LSCPU" | awk -F: -v L="$label" '
			{ h=$1; sub(/[ \t]+$/, "", h); if (h == L) { sub(/^[^:]*:[ \t]*/, "", $0); print; exit } }')"
	done
	emit_lines cpu.numa_cpus "$(printf '%s\n' "$LSCPU" | grep -E '^NUMA node[0-9]+ CPU')" 16
else
	emit cpu.lscpu "$ABSENT"
fi
# 每个 NUMA 节点的内存
NN=0
for d in /sys/devices/system/node/node*; do
	[ -d "$d" ] || continue
	NN=$((NN + 1))
	emit "numa.$(basename "$d").mem_total" "$(sed -n 's/.*MemTotal: *//p' "$d/meminfo" 2>/dev/null | head -1)"
	emit "numa.$(basename "$d").cpulist" "$(cat "$d/cpulist" 2>/dev/null)"
done
[ "$NN" = 0 ] && emit numa.nodes "$ABSENT" || emit numa.nodes "$NN"

say "2/8 内存与大页"
for k in MemTotal MemFree MemAvailable Buffers Cached SwapTotal Dirty Writeback \
	AnonHugePages ShmemHugePages HugePages_Total HugePages_Free HugePages_Rsvd \
	HugePages_Surp Hugepagesize Hugetlb CommitLimit Committed_AS; do
	emit "mem.$k" "$(sed -n "s/^$k: *//p" /proc/meminfo 2>/dev/null)"
done
for d in /sys/kernel/mm/hugepages/*/; do
	[ -d "$d" ] || continue
	b=$(basename "$d")
	emit "hugepages.$b.nr" "$(cat "$d/nr_hugepages" 2>/dev/null)"
	emit "hugepages.$b.free" "$(cat "$d/free_hugepages" 2>/dev/null)"
done
emit thp.enabled "$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)"
emit thp.defrag "$(cat /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null)"
emit thp.shmem_enabled "$(cat /sys/kernel/mm/transparent_hugepage/shmem_enabled 2>/dev/null)"
emit host.pagesize "$(getconf PAGESIZE 2>/dev/null)"

# ════════════════════════════════════════════════════ 3. 内核 config
say "3/8 内核 config"
CFG=""; CFGCAT=cat
for c in "/boot/config-$(uname -r)" /proc/config.gz /boot/config; do
	[ -e "$c" ] || continue
	CFG=$c
	case "$c" in *.gz) CFGCAT=zcat ;; *) CFGCAT=cat ;; esac
	break
done
emit kcfg.source "${CFG:-$ABSENT}"
if [ -n "$CFG" ] && { [ "$CFGCAT" = cat ] || have zcat; }; then
	CFGTXT=$($CFGCAT "$CFG" 2>/dev/null)
	# 精确项：没有就是 <absent>（「没编」与「=n」区别很大，950/920B 的 HDBSS 差异就在这）
	for opt in CONFIG_ARM64_HDBSS CONFIG_KVM CONFIG_KVM_ARM_PMU CONFIG_HAVE_KVM \
		CONFIG_USERFAULTFD CONFIG_HAVE_ARCH_USERFAULTFD_WP CONFIG_HAVE_ARCH_USERFAULTFD_MINOR \
		CONFIG_DEBUG_FS CONFIG_TRANSPARENT_HUGEPAGE CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS \
		CONFIG_TRANSPARENT_HUGEPAGE_MADVISE CONFIG_ARM64_HW_AFDBM CONFIG_ARM64_HAFT \
		CONFIG_ARM64_4K_PAGES CONFIG_ARM64_64K_PAGES CONFIG_ARM64_VA_BITS \
		CONFIG_HZ CONFIG_NO_HZ_FULL CONFIG_PREEMPT CONFIG_PREEMPT_VOLUNTARY; do
		v=$(printf '%s\n' "$CFGTXT" | sed -n "s/^$opt=//p" | head -1)
		if [ -z "$v" ]; then
			printf '%s\n' "$CFGTXT" | grep -q "^# $opt is not set" && v="n(not set)"
		fi
		emit "kcfg.$opt" "${v:-$ABSENT}"
	done
	# 通配组：把匹配到的行全列出来（每行一个 key，便于逐条 diff）
	for pat in '^CONFIG_KVM_ARM' '^CONFIG_ARM_GIC_V3' '^CONFIG_ARM_ARCH_TIMER' \
		'^CONFIG_SECCOMP' '^CONFIG_BLK_DEV_LOOP' '^CONFIG_ARM64_HDBSS'; do
		tag=$(printf '%s' "$pat" | sed 's/^\^CONFIG_//')
		n=0
		while IFS= read -r line; do
			[ -z "$line" ] && continue
			k=${line%%=*}; v=${line#*=}
			n=$((n + 1))
			emit "kcfggrp.$k" "$v"
		done <<EOF
$(printf '%s\n' "$CFGTXT" | grep -E "$pat" | grep -v '^#')
EOF
		[ "$n" = 0 ] && emit "kcfggrp.${tag}__none" "$ABSENT"
	done
else
	emit kcfg.note "读不到内核配置（950 上若缺，以 cap 502 探针为准）"
fi

# ════════════════════════════════════════════════════ 4. KVM
say "4/8 KVM 与 cap 502"
if [ -e /dev/kvm ]; then
	emit kvm.dev "$(T 5 stat -c '%A %U:%G' /dev/kvm)"
else
	emit kvm.dev "$ABSENT"
fi
# cap 502 探针：源码就地内嵌（950 无网，不能指望外部文件）；探针逻辑与
# rollback/scripts/dev/01-check-host.sh 的 cap_test.c 一致，不重写。
CAPSRC=$(mktemp /tmp/cap502.XXXXXX.c 2>/dev/null) || CAPSRC=/tmp/cap502.$$.c
cat >"$CAPSRC" <<'CEOF'
#include <errno.h>
#include <fcntl.h>
#include <linux/kvm.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#ifndef KVM_CAP_ARM_HW_DIRTY_STATE_TRACK
#define KVM_CAP_ARM_HW_DIRTY_STATE_TRACK 502
#endif
int main(void) {
	int kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
	if (kvm < 0) { printf("open_err=%s\n", strerror(errno)); return 2; }
	printf("check_extension=%d\n", ioctl(kvm, KVM_CHECK_EXTENSION, KVM_CAP_ARM_HW_DIRTY_STATE_TRACK));
	int vm = ioctl(kvm, KVM_CREATE_VM, 0);
	if (vm < 0) { printf("create_vm_err=%s\n", strerror(errno)); close(kvm); return 2; }
	printf("per_vm_check=%d\n", ioctl(vm, KVM_CHECK_EXTENSION, KVM_CAP_ARM_HW_DIRTY_STATE_TRACK));
	struct kvm_enable_cap cap;
	memset(&cap, 0, sizeof(cap));
	cap.cap = KVM_CAP_ARM_HW_DIRTY_STATE_TRACK;
	cap.args[0] = 1;   /* order 1 = 8 KiB/vCPU，与 Firecracker 默认一致 */
	int r = ioctl(vm, KVM_ENABLE_CAP, &cap);
	printf("enable_cap=%d\n", r);
	if (r < 0) printf("enable_errno=%s\n", strerror(errno));
	close(vm); close(kvm);
	return 0;
}
CEOF
if [ ! -e /dev/kvm ]; then
	emit kvm.cap502.status "skipped:/dev/kvm 不存在"
	emit kvm.cap502.check_extension "$ABSENT"
	emit kvm.cap502.per_vm_check "$ABSENT"
	emit kvm.cap502.enable_cap "$ABSENT"
elif ! have gcc; then
	emit kvm.cap502.status "skipped:没有 gcc，无法编译探针（把 cap 源码带到有 gcc 的机器编好再拷回来）"
	emit kvm.cap502.check_extension "$ABSENT"
	emit kvm.cap502.per_vm_check "$ABSENT"
	emit kvm.cap502.enable_cap "$ABSENT"
else
	CAPBIN=${CAPSRC%.c}
	if T 120 gcc -O0 -o "$CAPBIN" "$CAPSRC"; then
		CAPOUT=$(timeout 20 "$CAPBIN" 2>/dev/null)
		emit kvm.cap502.status "ran"
		for k in check_extension per_vm_check enable_cap enable_errno open_err create_vm_err; do
			v=$(printf '%s\n' "$CAPOUT" | sed -n "s/^$k=//p" | head -1)
			[ -n "$v" ] && emit "kvm.cap502.$k" "$v"
		done
		printf '%s\n' "$CAPOUT" | grep -q '^check_extension=' || emit kvm.cap502.check_extension "$ABSENT"
		printf '%s\n' "$CAPOUT" | grep -q '^enable_cap=' || emit kvm.cap502.enable_cap "$ABSENT"
		ENAB=$(printf '%s\n' "$CAPOUT" | sed -n 's/^enable_cap=//p' | head -1)
		emit kvm.hdbss_usable "$([ "${ENAB:-x}" = 0 ] && echo yes || echo no)"
		rm -f "$CAPBIN"
	else
		emit kvm.cap502.status "compile_failed"
		emit kvm.cap502.check_extension "$ABSENT"
		emit kvm.cap502.enable_cap "$ABSENT"
	fi
fi
rm -f "$CAPSRC"

emit kvm_arm.mode "$(cat /sys/module/kvm_arm/parameters/mode 2>/dev/null)"
for p in /sys/module/kvm/parameters/* /sys/module/kvm_arm/parameters/*; do
	[ -r "$p" ] || continue
	m=$(printf '%s' "$p" | sed 's#/sys/module/##; s#/parameters/#.#')
	emit "kvmparam.$m" "$(head -c 200 "$p" 2>/dev/null)"
done
if [ -d /sys/kernel/debug/kvm ]; then
	if ls /sys/kernel/debug/kvm >/dev/null 2>&1; then
		emit debugfs.kvm.readable yes
		emit debugfs.kvm.entries "$(ls /sys/kernel/debug/kvm 2>/dev/null | head -40 | tr '\n' ' ')"
		emit debugfs.kvm.entry_count "$(ls /sys/kernel/debug/kvm 2>/dev/null | wc -l)"
	else
		emit debugfs.kvm.readable "no(EACCES)"
	fi
else
	emit debugfs.kvm.readable "no(不存在；X1 的 vgic-state 取不到，需 mount -t debugfs)"
fi
emit debugfs.mounted "$(grep -c ' debugfs ' /proc/mounts 2>/dev/null)"
# 开机日志：dmesg 的环形缓冲会被滚掉（920B 跑了 24 天，里面一条 arch_timer 都不剩），
# 所以先 dmesg，空了退到 journalctl -k（本次开机的完整内核日志）。
klog() {
	local pat=$1 n=$2 out
	out=$(T 25 dmesg 2>/dev/null | grep -i -E "$pat" | head -"$n")
	if [ -z "$out" ] && have journalctl; then
		out=$(T 60 journalctl -k --no-pager -o cat 2>/dev/null | grep -i -E "$pat" | head -"$n")
	fi
	printf '%s' "$out"
}
emit klog.dmesg_available "$([ -n "$(T 10 dmesg 2>/dev/null | head -1)" ] && echo yes || echo no)"
emit klog.journalctl_available "$(have journalctl && echo yes || echo no)"
emit_lines dmesg.kvm "$(klog 'hdbss|vgic|gicv|gic:|kvm' 30)" 30

# ════════════════════════════════════════════════════ 5. 中断与时钟
say "5/8 中断与时钟"
while IFS= read -r line; do
	[ -z "$line" ] && continue
	set -- $line
	IRQLINE=${1%:}
	emit "irq.arch_timer.$IRQLINE.summary" "$(printf '%s\n' "$line" | awk -v n="$(nproc 2>/dev/null || echo 1)" '{
		s=0; for (i=2; i<=n+1 && i<=NF; i++) if ($i ~ /^[0-9]+$/) s+=$i;
		chip=""; intid=""; trig=""; name="";
		for (i=2; i<=NF; i++) if ($i !~ /^[0-9]+$/) { chip=$i; intid=$(i+1); trig=$(i+2); name=$(i+3); break }
		printf "intid=%s trigger=%s name=%s total=%d cpu0=%s cpu1=%s", intid, trig, name, s, $2, $3
	}')"
done <<EOF
$(grep -i 'arch_timer' /proc/interrupts 2>/dev/null)
EOF
emit irq.total_lines "$(wc -l </proc/interrupts 2>/dev/null)"
emit clocksource.current "$(cat /sys/devices/system/clocksource/clocksource0/current_clocksource 2>/dev/null)"
emit clocksource.available "$(cat /sys/devices/system/clocksource/clocksource0/available_clocksource 2>/dev/null)"
DMESG_TIMER=$(klog 'arch_timer' 8)
emit_lines timer.dmesg "$DMESG_TIMER" 8
emit timer.cntfrq "$(printf '%s\n' "$DMESG_TIMER" | sed -n 's/.*running at \([0-9.]*[MK]*Hz\).*/\1/p' | head -1)"
emit_lines gic.dmesg "$(klog 'GICv|GIC:|ITS' 12)" 12
if [ -d /sys/firmware/devicetree ]; then
	emit gic.devicetree "$(ls /sys/firmware/devicetree/base 2>/dev/null | grep -i -E 'intr|gic' | tr '\n' ' ')"
else
	emit gic.devicetree "$ABSENT"
fi
emit gic.acpi_madt "$([ -r /sys/firmware/acpi/tables/APIC ] && stat -c '%s bytes' /sys/firmware/acpi/tables/APIC 2>/dev/null || echo "$ABSENT")"

# ════════════════════════════════════════════════════ 6. 存储
say "6/8 存储"
if have lsblk; then
	emit_lines blk.lsblk "$(T 25 lsblk -e 7,43,1 -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,ROTA,DISC-GRAN)" 40
	emit blk.loop_count "$(T 20 lsblk -o NAME,TYPE | grep -c loop)"
else
	emit blk.lsblk.00 "$ABSENT"
fi

# orchestrator 的 env（下一节还要用），先取一次
ORCH_PID=""; ORCH_ENV=""
for pid in $(T 10 ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
	[ -r "/proc/$pid/environ" ] || continue
	b=$(basename "$(readlink -f "/proc/$pid/exe" 2>/dev/null)" 2>/dev/null)
	case "$b" in
	orchestrator*|template-manager*) : ;;
	*) grep -qa 'ORCHESTRATOR_SERVICES=' "/proc/$pid/environ" 2>/dev/null || continue ;;
	esac
	ORCH_PID=$pid
	ORCH_ENV=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null)
	break
done
BASE=$(printf '%s\n' "$ORCH_ENV" | sed -n 's/^ORCHESTRATOR_BASE_PATH=//p' | head -1)
[ -z "$BASE" ] && BASE=/orchestrator
STORE="$BASE/build/checkpoints"

# 额外要看的挂载点由 CRTEST_EXTRA_MOUNTS 给（空格分隔）：开发机上的数据卷
# （920B 的 /mnt/ext4dev、/mnt/xfsdev）这么传进来，不再写死在脚本里。
for m in / "$BASE" "$STORE" /orchestrator ${CRTEST_EXTRA_MOUNTS:-} /fc-versions /tmp /home; do
	[ -n "$m" ] || continue
	[ -e "$m" ] || { emit "mnt.$m" "$ABSENT"; continue; }
	emit "mnt.$m" "$(T 15 findmnt -no SOURCE,FSTYPE,OPTIONS --target "$m" | head -1)"
	emit "df.$m" "$(T 15 df -hP "$m" | tail -1)"
done
emit path.orchestrator_base "$BASE"
emit path.checkpoint_store "$STORE"

STORE_DEV=$(T 15 findmnt -no SOURCE --target "$STORE" 2>/dev/null | head -1)
emit store.device "${STORE_DEV:-$ABSENT}"
emit store.fstype "$(T 15 findmnt -no FSTYPE --target "$STORE" | head -1)"
if have tune2fs && [ -n "$STORE_DEV" ] && [ -b "$STORE_DEV" ]; then
	TT=$(T 30 tune2fs -l "$STORE_DEV")
	for f in "Filesystem features" "Default mount options" "Filesystem state" \
		"Block size" "Journal inode" "Journal backup" "Inode count" "Block count" \
		"Reserved block count" "Filesystem created"; do
		emit "tune2fs.$(printf '%s' "$f" | tr ' ' '_')" \
			"$(printf '%s\n' "$TT" | sed -n "s/^$f: *//p" | head -1)"
	done

else
	emit tune2fs.note "$ABSENT"
fi
# 挂载选项里的 journal 模式（data=ordered/writeback）比 tune2fs 的默认值更准
STORE_OPTS=$(T 15 findmnt -no OPTIONS --target "$STORE" | head -1)
emit store.mount_options "$STORE_OPTS"
SJM=$(printf '%s' "$STORE_OPTS" | tr ',' '\n' | sed -n 's/^data=//p' | head -1)
if [ -z "$SJM" ]; then
	case "$(T 15 findmnt -no FSTYPE --target "$STORE" | head -1)" in
	ext4 | ext3) SJM="ordered(默认，挂载选项未显式指定)" ;;
	esac
fi
emit store.journal_mode "$SJM"

NVME_N=0
for q in /sys/block/nvme*/queue/write_cache; do
	[ -r "$q" ] || continue
	NVME_N=$((NVME_N + 1))
	d=$(printf '%s' "$q" | sed 's#/sys/block/##; s#/queue/write_cache##')
	emit "nvme.$d.write_cache" "$(cat "$q" 2>/dev/null)"
	emit "nvme.$d.scheduler" "$(cat "/sys/block/$d/queue/scheduler" 2>/dev/null)"
	emit "nvme.$d.rotational" "$(cat "/sys/block/$d/queue/rotational" 2>/dev/null)"
	emit "nvme.$d.model" "$(cat "/sys/block/$d/device/model" 2>/dev/null)"
done
[ "$NVME_N" = 0 ] && emit nvme.devices "$ABSENT" || emit nvme.devices "$NVME_N"
if have nvme; then
	emit nvme.id_ctrl "$(T 20 nvme id-ctrl /dev/nvme0 2>/dev/null | grep -E '^(mn|fr|vwc) ' | tr '\n' ' ')"
else
	emit nvme.id_ctrl "$ABSENT"
fi
emit_lines loop.devices "$(T 20 losetup -l -O NAME,BACK-FILE,DIO,LOG-SEC 2>/dev/null)" 12

# ── fsync 微基准（本轮 create 随 N ×7.4 的直接对照，并发报告 A4/B6）
if [ "$DO_FSYNC" = 1 ]; then
	say "6b/8 fsync 微基准（唯一写盘的探测）"
	FSDIR="$STORE"
	[ -d "$FSDIR" ] || FSDIR="$BASE"
	[ -d "$FSDIR" ] || FSDIR=/tmp
	TD=$(mktemp -d "$FSDIR/.probe-fsync.XXXXXX" 2>/dev/null) || TD=$(mktemp -d)
	emit fsync.dir "$TD"
	emit fsync.dir_fstype "$(T 15 findmnt -no FSTYPE --target "$TD" | head -1)"
	PY=""
	for p in ${CRTEST_PY:-} python3 python; do
		command -v "$p" >/dev/null 2>&1 && PY=$p && break
		[ -x "$p" ] && PY=$p && break
	done
	if [ -n "$PY" ]; then
		emit fsync.engine "python:$PY"
		FSOUT=$(FS_DIR="$TD" FS_N="$FSYNC_N" FS_THREADS="$FSYNC_THREADS" timeout 600 "$PY" - <<'PYEOF' 2>/dev/null
# 4 KB 写 + fsync 的延迟分布：单线程 vs 16 线程并发。
# 并发那档是本轮的关键对照：ext4 日志会把并发 fsync 串成一串（并发报告 A4/B6），
# 920B 的产物盘是 loop 镜像，950 是真 NVMe，倍数差别就在这里看出来。
import os, sys, threading, time

d = os.environ["FS_DIR"]; n = int(os.environ["FS_N"]); nt = int(os.environ["FS_THREADS"])
buf = b"x" * 4096


def one(tag, res):
    path = os.path.join(d, "fs-%s.bin" % tag)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    lat = []
    try:
        for i in range(n):
            os.lseek(fd, (i % 64) * 4096, os.SEEK_SET)
            t0 = time.monotonic()
            os.write(fd, buf)
            os.fsync(fd)
            lat.append((time.monotonic() - t0) * 1000.0)
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
    res[tag] = lat


def q(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = min(len(s) - 1, int(round((len(s) - 1) * p)))
    return s[k]


res = {}
t0 = time.monotonic()
one("single", res)
single_wall = time.monotonic() - t0
s = res["single"]
print("single.count=%d" % len(s))
print("single.p50_ms=%.3f" % q(s, 0.50))
print("single.p99_ms=%.3f" % q(s, 0.99))
print("single.max_ms=%.3f" % max(s))
print("single.ops_per_s=%.1f" % (len(s) / single_wall))

res = {}
ths = [threading.Thread(target=one, args=("t%02d" % i, res)) for i in range(nt)]
t0 = time.monotonic()
for t in ths:
    t.start()
for t in ths:
    t.join()
wall = time.monotonic() - t0
allx = [x for v in res.values() for x in v]
print("threads=%d" % nt)
print("concurrent.count=%d" % len(allx))
print("concurrent.p50_ms=%.3f" % q(allx, 0.50))
print("concurrent.p99_ms=%.3f" % q(allx, 0.99))
print("concurrent.max_ms=%.3f" % max(allx))
print("concurrent.wall_s=%.3f" % wall)
print("concurrent.ops_per_s=%.1f" % (len(allx) / wall))
print("ratio_p50=%.2f" % (q(allx, 0.50) / q(s, 0.50)))
PYEOF
)
		if [ -n "$FSOUT" ]; then
			while IFS= read -r line; do
				[ -z "$line" ] && continue
				emit "fsync.${line%%=*}" "${line#*=}"
			done <<EOF
$FSOUT
EOF
		else
			emit fsync.result "$ABSENT"
		fi
	else
		# 没有 python：退化成 dd + fdatasync，只能给"每次 4 KB 写+同步的平均耗时"
		emit fsync.engine "dd(无 python3，只有均值，没有分位数)"
		T0=$(date +%s.%N 2>/dev/null)
		i=0
		while [ $i -lt "$FSYNC_N" ]; do
			dd if=/dev/zero of="$TD/dd.bin" bs=4096 count=1 conv=fdatasync,notrunc oflag=append 2>/dev/null
			i=$((i + 1))
		done
		T1=$(date +%s.%N 2>/dev/null)
		emit fsync.single.count "$FSYNC_N"
		emit fsync.single.mean_ms "$(awk -v a="$T0" -v b="$T1" -v n="$FSYNC_N" 'BEGIN{printf "%.3f",(b-a)*1000/n}' 2>/dev/null)"
		emit fsync.single.p50_ms "$ABSENT"
		emit fsync.single.p99_ms "$ABSENT"
		emit fsync.concurrent.p50_ms "$ABSENT"
		emit fsync.concurrent.p99_ms "$ABSENT"
	fi
	rm -rf "$TD" 2>/dev/null
else
	emit fsync.engine "skipped(--skip-fsync)"
	emit fsync.single.p50_ms "$ABSENT"
	emit fsync.concurrent.p50_ms "$ABSENT"
fi

# ════════════════════════════════════════════════════ 7. e2b 部署形态
say "7/8 e2b 部署形态"
emit e2b.orchestrator_pid "${ORCH_PID:-$ABSENT}"
if [ -n "$ORCH_PID" ]; then
	EXE=$(readlink -f "/proc/$ORCH_PID/exe" 2>/dev/null)
	emit e2b.orchestrator_exe "$EXE"
	emit e2b.orchestrator_sha256 "$(T 120 sha256sum "$EXE" 2>/dev/null | cut -d' ' -f1)"
	emit e2b.orchestrator_size "$(stat -c %s "$EXE" 2>/dev/null)"
	if have go; then
		emit_lines e2b.orchestrator_gover "$(T 60 go version -m "$EXE" 2>/dev/null | head -6)" 6
	else
		emit e2b.orchestrator_gover.00 "$ABSENT"
	fi
	for k in FC_TRACK_DIRTY_PAGES ORCHESTRATOR_BASE_PATH FIRECRACKER_VERSIONS_DIR ENVIRONMENT \
		HOST_KERNELS_DIR DEFAULT_KERNEL_VERSION LOCAL_TEMPLATE_STORAGE_BASE_PATH \
		ORCHESTRATOR_SERVICES CHECKPOINT_FULL_ROOT FC_HDBSS_ORDER CHECKPOINT_FAULT_INJECT \
		LOGS_COLLECTOR_ADDRESS LOGS_COLLECTOR_PUBLIC_IP OTEL_COLLECTOR_GRPC_ENDPOINT \
		NODE_ID ALLOW_SANDBOX_INTERNET; do
		emit "e2b.env.$k" "$(printf '%s\n' "$ORCH_ENV" | sed -n "s/^$k=//p" | head -1)"
	done
else
	emit e2b.orchestrator_exe "$ABSENT"
	emit e2b.env.note "找不到 orchestrator 进程（栈没起？950 上 :3000 曾经没起，见 950-摸底结论 §6）"
fi

# nomad：token 先从 /opt/e2b-infra/.env 拿，拿不到再看 acl.token
NOMAD_TOKEN_SRC="$ABSENT"
NT=""
if [ -r /opt/e2b-infra/.env ]; then
	NT=$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}NOMAD_ACL_TOKEN=//p' /opt/e2b-infra/.env 2>/dev/null | tr -d '"' | head -1)
	[ -n "$NT" ] && NOMAD_TOKEN_SRC=/opt/e2b-infra/.env
fi
if [ -z "$NT" ]; then
	for f in /opt/e2b-infra/dep/.env; do
		[ -r "$f" ] || continue
		NT=$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}NOMAD_ACL_TOKEN=//p' "$f" 2>/dev/null | tr -d '"' | head -1)
		[ -n "$NT" ] && NOMAD_TOKEN_SRC=$f && break
	done
fi
if [ -z "$NT" ]; then
	for f in /data/nomad/acl.token /var/lib/nomad/acl.token /data/nomad/server/acl.token; do
		[ -r "$f" ] || continue
		NT=$(head -1 "$f" 2>/dev/null); NOMAD_TOKEN_SRC=$f; break
	done
fi
emit nomad.token_source "$NOMAD_TOKEN_SRC"
if have nomad; then
	emit nomad.version "$(T 20 nomad version 2>/dev/null | head -1)"
	NJ=$(NOMAD_TOKEN="$NT" T 30 nomad job status 2>&1 | head -20)
	emit_lines nomad.jobs "$NJ" 20
	for j in template-manager template-manager-system orchestrator api client-proxy; do
		st=$(NOMAD_TOKEN="$NT" T 25 nomad job status -short "$j" 2>/dev/null | sed -n 's/^Status *= *//p' | head -1)
		emit "nomad.job.$j" "${st:-$ABSENT}"
	done
	TMENV=$(NOMAD_TOKEN="$NT" T 30 nomad job inspect template-manager 2>/dev/null)
	[ -z "$TMENV" ] && TMENV=$(NOMAD_TOKEN="$NT" T 30 nomad job inspect template-manager-system 2>/dev/null)
	if [ -n "$TMENV" ]; then
		emit_lines nomad.template_manager_env \
			"$(printf '%s\n' "$TMENV" | grep -E 'FC_TRACK_DIRTY_PAGES|ORCHESTRATOR_BASE_PATH|FIRECRACKER_VERSIONS_DIR|KERNEL|ENVIRONMENT|TEMPLATE_STORAGE|LOGS_COLLECTOR' | head -20)" 20
	else
		emit nomad.template_manager_env.00 "$ABSENT"
	fi
else
	emit nomad.version "$ABSENT"
fi

# Firecracker 制品
FCDIR=$(printf '%s\n' "$ORCH_ENV" | sed -n 's/^FIRECRACKER_VERSIONS_DIR=//p' | head -1)
emit e2b.fc_versions_dir "${FCDIR:-$ABSENT}"
FCN=0
for fc in /fc-versions/*/firecracker ${FCDIR:+$FCDIR/*/firecracker} /opt/e2b-infra/bin/firecracker; do
	[ -f "$fc" ] || continue
	FCN=$((FCN + 1))
	tag=$(printf '%s' "$fc" | sed 's#^/##; s#/#_#g')
	emit "fc.$tag.sha256" "$(T 60 sha256sum "$fc" 2>/dev/null | cut -d' ' -f1)"
	emit "fc.$tag.size" "$(stat -c %s "$fc" 2>/dev/null)"
	emit "fc.$tag.version" "$(T 20 "$fc" --version 2>/dev/null | head -1)"
	if have strings; then
		emit "fc.$tag.savedirtybitmap" "$(T 60 strings "$fc" 2>/dev/null | grep -c SaveDirtyBitmap)"
	else
		emit "fc.$tag.savedirtybitmap" "$ABSENT"
	fi
done
[ "$FCN" = 0 ] && emit fc.found "$ABSENT" || emit fc.found "$FCN"

KDIR=$(printf '%s\n' "$ORCH_ENV" | sed -n 's/^HOST_KERNELS_DIR=//p' | head -1)
[ -z "$KDIR" ] && KDIR=/fc-kernels
KN=0
for k in "$KDIR"/*/vmlinux.bin; do
	[ -f "$k" ] || continue
	KN=$((KN + 1))
	tag=$(basename "$(dirname "$k")")
	emit "guestkernel.$tag.sha256" "$(T 120 sha256sum "$k" 2>/dev/null | cut -d' ' -f1)"
	emit "guestkernel.$tag.size" "$(stat -c %s "$k" 2>/dev/null)"
done
[ "$KN" = 0 ] && emit guestkernel.found "$ABSENT" || emit guestkernel.found "$KN"

for p in 3000 5008 49984 4646 8500; do
	emit "listen.$p" "$(T 15 ss -lntp 2>/dev/null | grep -c ":$p ")"
done
emit rpm.e2b "$(T 60 rpm -qa 2>/dev/null | grep -i e2b | tr '\n' ' ')"
emit e2b.opt_bin "$(ls /opt/e2b-infra/bin 2>/dev/null | tr '\n' ' ')"
TPLDIR="$BASE/template"
[ -d "$TPLDIR" ] || TPLDIR=/orchestrator/template
emit templates.dir "$([ -d "$TPLDIR" ] && echo "$TPLDIR" || echo "$ABSENT")"
emit templates.count "$(ls "$TPLDIR" 2>/dev/null | wc -l)"
emit sandboxes.running_fc "$(pgrep -c -x firecracker 2>/dev/null || echo 0)"
emit checkpoints.sandbox_dirs "$(ls "$STORE" 2>/dev/null | wc -l)"

# ════════════════════════════════════════════════════ 8. SDK 与工具链
say "8/8 SDK 与工具链"
SDKPY=""
for p in ${CRTEST_PY:-} python3; do
	if [ -x "$p" ]; then SDKPY=$p; break; fi
	command -v "$p" >/dev/null 2>&1 && SDKPY=$(command -v "$p") && break
done
emit sdk.python "${SDKPY:-$ABSENT}"
if [ -n "$SDKPY" ]; then
	emit sdk.python_version "$("$SDKPY" -V 2>&1 | head -1)"
	SDKDIR=$(T 60 "$SDKPY" -c 'import e2b,os;print(os.path.dirname(e2b.__file__))' 2>/dev/null)
	emit sdk.e2b_path "${SDKDIR:-$ABSENT}"
	emit sdk.e2b_version "$(T 60 "$SDKPY" -m pip show e2b 2>/dev/null | sed -n 's/^Version: *//p' | head -1)"
	emit sdk.dotenv "$(T 60 "$SDKPY" -c 'import dotenv;print(getattr(dotenv,"__version__","installed"))' 2>/dev/null)"
	if [ -n "$SDKDIR" ] && [ -d "$SDKDIR" ]; then
		# 这四个计数就是"装的是哪一版 SDK"的指纹：
		#   E2b-Sandbox-Id       = 2 → 带 connect() 路由头修复（e2b-arm 66414855，并发报告 3.4）
		#   CHECKPOINT_REQUEST_TIMEOUT 有 → 带 K3（create/restore 独立 300 s 超时）
		#   _resolve_retries     有 → 带 K2（重试次数改实例属性，checkpoint 不重试）
		#   e2b-traffic-access-token 有 → 带 K1（私有沙箱五入口）
		emit sdk.count.E2b_Sandbox_Id "$(T 30 grep -rc 'E2b-Sandbox-Id' "$SDKDIR/sandbox_sync/main.py" 2>/dev/null | head -1)"
		SITE=$(dirname "$SDKDIR")
		SDKDIRS="$SDKDIR"
		for extra in "$SITE"/e2b_connect "$SITE"/e2b_sdk "$SITE"/checkpoint_connect; do
			[ -d "$extra" ] && SDKDIRS="$SDKDIRS $extra"
		done
		emit sdk.packages "$(printf '%s' "$SDKDIRS" | tr ' ' '\n' | xargs -r -n1 basename 2>/dev/null | tr '\n' ' ')"
		for pat in CHECKPOINT_REQUEST_TIMEOUT _resolve_retries e2b-traffic-access-token; do
			key=$(printf '%s' "$pat" | tr '-' '_')
			emit "sdk.count.$key" "$(T 60 grep -r --include='*.py' -c "$pat" $SDKDIRS 2>/dev/null | awk -F: '{s+=$2} END{print s+0}')"
		done
		emit sdk.checkpoint_module "$([ -f "$SDKDIR/checkpoint_connect.py" ] || ls "$SDKDIR" 2>/dev/null | grep -c checkpoint)"
	fi
else
	emit sdk.python_version "$ABSENT"
	emit sdk.e2b_version "$ABSENT"
fi

for t in perf gcc cargo rustc docker go jq nvme tune2fs xfs_info mkfs.xfs losetup nomad consul \
	curl python3 strings sha256sum findmnt lsblk ss dmesg; do
	if have "$t"; then
		v=$(T 15 "$t" --version 2>/dev/null | head -1)
		[ -z "$v" ] && v="present"
		emit "tool.$t" "$(command -v "$t") | $v"
	else
		emit "tool.$t" "$ABSENT"
	fi
done

# ════════════════════════════════════════════════════ JSON
# 纯 awk 生成，不依赖 python/jq（950 上两者都可能不在 root 的 PATH 里）
awk -F= '
function esc(s) {
  gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); gsub(/[\001-\037]/, " ", s);
  return s
}
BEGIN { print "{" ; first=1 }
{
  key=$1; sub(/^[^=]*=/, "", $0); val=$0;
  if (key == "") next;
  if (!first) printf ",\n"; first=0;
  printf "  \"%s\": \"%s\"", esc(key), esc(val);
}
END { printf "\n}\n" }
' "$KV" >"$JSON"

echo >&2
echo "写出:" >&2
echo "  $KV" >&2
echo "  $JSON" >&2
echo "共 $(wc -l <"$KV") 项。" >&2
