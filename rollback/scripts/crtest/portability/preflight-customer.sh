#!/usr/bin/env bash
# preflight-customer.sh —— 客户机器「能不能跑沙箱级快照回滚（checkpoint / restore）」的预检。
#
# 零外部依赖：只用 coreutils/bash。gcc、python3、nomad、strings 等一律「有就用，
# 没有就把该项判成 WARN 并说清为什么判不了」，绝不中断。
# 全程 `set -u`（没有 -e），每条外部命令带 timeout。只读，不写任何业务目录。
#
# 与 deploy/check-env.sh 的分工：check-env.sh 查的是「build.sh --install/--start 能不能跑通」
# （包管理器、docker、harbor、nomad 端口……），本脚本查的是「这台机器的 CPU/内核/内核模块/
# 产物盘/二进制身份 能不能支撑 checkpoint-restore」。两者不重叠，建议并列执行。
#
# 用法:
#   bash preflight-customer.sh
#   bash preflight-customer.sh --hugepages 8192      # 期望的 2MiB 大页数（默认 0=不判定）
#   bash preflight-customer.sh --min-free-gb 100     # 产物盘最小剩余（默认 50）
#   bash preflight-customer.sh --store /orchestrator/build/checkpoints
#
# 退出码 = FAIL 的项数。
set -u

EXPECT_HUGEPAGES=0
MIN_FREE_GB=50
STORE_OVERRIDE=""

while [ $# -gt 0 ]; do
	case "$1" in
	--hugepages) EXPECT_HUGEPAGES=$2; shift 2 ;;
	--min-free-gb) MIN_FREE_GB=$2; shift 2 ;;
	--store) STORE_OVERRIDE=$2; shift 2 ;;
	-h|--help) sed -n '2,22p' "$0"; exit 0 ;;
	*) echo "未知参数: $1（-h 看用法）" >&2; exit 2 ;;
	esac
done

RED='\033[31m'; GRN='\033[32m'; YLW='\033[33m'; CYA='\033[36m'; NC='\033[0m'
N_PASS=0; N_WARN=0; N_FAIL=0
pass() { printf "  ${GRN}PASS${NC}  %-34s %s\n" "$1" "$2"; N_PASS=$((N_PASS+1)); }
warn() { printf "  ${YLW}WARN${NC}  %-34s %s\n" "$1" "$2"; N_WARN=$((N_WARN+1)); }
fail() { printf "  ${RED}FAIL${NC}  %-34s %s\n" "$1" "$2"; N_FAIL=$((N_FAIL+1)); }
note() { printf "  ${CYA}INFO${NC}  %-34s %s\n" "$1" "$2"; }
sec()  { printf "\n${CYA}== %s ==${NC}\n" "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
T()    { timeout "$1" "${@:2}" 2>/dev/null; }

echo "沙箱快照回滚 预检  host=$(hostname 2>/dev/null)  $(date -Is 2>/dev/null)"

# ─────────────────────────────────────────── 1. CPU / 虚拟化
sec "1. CPU 与虚拟化"
ARCH=$(uname -m 2>/dev/null)
if [ "$ARCH" = "aarch64" ]; then
	pass "架构 aarch64" "$ARCH，$(uname -r 2>/dev/null)"
else
	fail "架构 aarch64" "当前 $ARCH；本方案的回滚路径（GIC 清线、HDBSS、vCPU reinit）只在 aarch64 上实现"
fi

if [ -e /dev/kvm ]; then
	if T 5 dd if=/dev/kvm of=/dev/null bs=1 count=0 >/dev/null 2>&1 || [ -r /dev/kvm -a -w /dev/kvm ]; then
		pass "/dev/kvm 可读写" "$(T 5 stat -c '%A %U:%G' /dev/kvm)"
	else
		fail "/dev/kvm 可读写" "存在但当前用户打不开；Firecracker 需要读写它"
	fi
else
	fail "/dev/kvm 存在" "没有 /dev/kvm，KVM 未启用或未加载 kvm 模块"
fi
note "kvm_arm.mode" "$(cat /sys/module/kvm_arm/parameters/mode 2>/dev/null || echo '<读不到>')"

# ─────────────────────────────────────────── 2. GIC 版本
sec "2. 中断控制器（GIC）"
klog() {
	local out
	out=$(T 20 dmesg 2>/dev/null | grep -i -E "$1" | head -5)
	if [ -z "$out" ] && have journalctl; then
		out=$(T 45 journalctl -k --no-pager -o cat 2>/dev/null | grep -i -E "$1" | head -5)
	fi
	printf '%s' "$out"
}
GICLOG=$(klog 'GICv[0-9]|GIC: |gic-v3|arm,gic')
GICDT=""
for c in /sys/firmware/devicetree/base/*intr*/compatible \
         /sys/firmware/devicetree/base/*interrupt-controller*/compatible \
         /sys/firmware/devicetree/base/*/interrupt-controller*/compatible; do
	[ -r "$c" ] || continue
	GICDT="$GICDT $(tr -d '\000' <"$c" 2>/dev/null)"
done
GICVER=""
case "$GICLOG$GICDT" in
	*GICv3*|*gic-v3*|*GICv4*|*gic-v4*) GICVER=v3 ;;
	*GICv2*|*gic-400*|*cortex-a15-gic*) GICVER=v2 ;;
esac
if [ "$GICVER" = v3 ]; then
	pass "GICv3" "$(printf '%s' "$GICLOG" | head -1)"
elif [ "$GICVER" = v2 ]; then
	warn "GIC 版本" "检测到 GICv2。回滚用的 GICD_ICACTIVER/ICPENDR 在 v2 下偏移与属性组相同、内核 uaccess 也同样注册，理论成立但我们未在 v2 上实测；且串口清线失败已是非致命（只记一条 warn）"
else
	warn "GIC 版本" "判不出来（dmesg 环形缓冲可能已滚掉，且无 devicetree 节点）。栈起来后看 orchestrator/FC 日志里有无 'cleared serial GIC line'"
fi

# ─────────────────────────────────────────── 3. HDBSS（KVM cap 502）
sec "3. 硬件脏页跟踪 HDBSS（KVM capability 502）"
CAP_VERDICT="unknown"
if [ ! -e /dev/kvm ]; then
	warn "KVM cap 502" "没有 /dev/kvm，判不了"
elif ! have gcc; then
	warn "KVM cap 502" "本机没有 gcc，无法编译探针。可在同款机器上编好 cap502 探针再拷过来；或看 orchestrator 启动日志的 track_dirty_pages_reason"
else
	CSRC=$(mktemp /tmp/cap502.XXXXXX.c 2>/dev/null) || CSRC=/tmp/cap502.$$.c
	cat >"$CSRC" <<'CEOF'
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
	CBIN=${CSRC%.c}
	if T 120 gcc -O0 -o "$CBIN" "$CSRC"; then
		COUT=$(timeout 20 "$CBIN" 2>/dev/null)
		CHK=$(printf '%s\n' "$COUT" | sed -n 's/^check_extension=//p' | head -1)
		ENA=$(printf '%s\n' "$COUT" | sed -n 's/^enable_cap=//p' | head -1)
		ERR=$(printf '%s\n' "$COUT" | sed -n 's/^enable_errno=//p' | head -1)
		if [ "${ENA:-x}" = "0" ]; then
			CAP_VERDICT="yes"
			pass "KVM cap 502 可用" "check_extension=$CHK, KVM_ENABLE_CAP 成功 → 增量快照（mem_mode=incremental）"
		elif [ "${CHK:-0}" != "0" ] && [ -n "$CHK" ]; then
			CAP_VERDICT="reported-but-broken"
			warn "KVM cap 502" "内核报告了 502（check_extension=$CHK）但 ENABLE_CAP 失败（${ERR:-?}）。Firecracker 会静默退到 kvm-wp（每个干净页一次 VM exit）。若不接受，部署时设 FC_HDBSS_REQUIRED=true 让它启动即失败"
		else
			CAP_VERDICT="no"
			warn "KVM cap 502" "本机无 HDBSS。功能仍可用，但脏页跟踪默认关闭，每次 checkpoint 全量拷贝 guest 内存（2GiB 沙箱 ≈ 40MiB→2GiB、几十毫秒→1.5 秒）。不是阻断项"
		fi
		rm -f "$CBIN"
	else
		warn "KVM cap 502" "探针编译失败，判不了"
	fi
	rm -f "$CSRC"
fi

# ─────────────────────────────────────────── 4. 内核特性
sec "4. 内核特性"
CFG=""; CATC=cat
for c in "/boot/config-$(uname -r 2>/dev/null)" /proc/config.gz /boot/config; do
	[ -e "$c" ] || continue
	CFG=$c; case "$c" in *.gz) CATC=zcat ;; esac; break
done
if [ -n "$CFG" ] && { [ "$CATC" = cat ] || have zcat; }; then
	CFGTXT=$($CATC "$CFG" 2>/dev/null)
	getcfg() { printf '%s\n' "$CFGTXT" | sed -n "s/^$1=//p" | head -1; }
	for opt in CONFIG_KVM CONFIG_USERFAULTFD; do
		v=$(getcfg "$opt")
		case "$v" in
		y|m) pass "$opt" "$v" ;;
		*)   fail "$opt" "未启用（=${v:-未编译）}；e2b 的内存按需加载依赖 userfaultfd，KVM 不用说" ;;
		esac
	done
	v=$(getcfg CONFIG_ARM64_HDBSS)
	[ -n "$v" ] && note "CONFIG_ARM64_HDBSS" "$v" || note "CONFIG_ARM64_HDBSS" "<内核配置里没有，与 cap 502 探针结论一致即可>"
	note "CONFIG_BLK_DEV_NBD" "$(getcfg CONFIG_BLK_DEV_NBD)"
else
	warn "内核配置" "读不到 /boot/config-* 或 /proc/config.gz，以下按运行时能力判定"
	if [ -e /dev/kvm ]; then pass "CONFIG_KVM" "由 /dev/kvm 存在反推"; fi
fi
if [ -e /dev/userfaultfd ] || grep -q userfaultfd /proc/kallsyms 2>/dev/null; then
	pass "userfaultfd 运行时可用" "$([ -e /dev/userfaultfd ] && echo /dev/userfaultfd || echo 'kallsyms 有符号')"
fi
UNPRIV=$(cat /proc/sys/vm/unprivileged_userfaultfd 2>/dev/null)
note "vm.unprivileged_userfaultfd" "${UNPRIV:-<读不到>}（orchestrator 以 root 跑，0 也没关系）"

if [ -d /sys/kernel/debug ] && T 5 ls /sys/kernel/debug >/dev/null 2>&1; then
	note "/sys/kernel/debug" "已挂载可读（可选：只用于排障看 vgic-state，功能不依赖）"
else
	note "/sys/kernel/debug" "不可用（可选项，不影响功能；排障时 mount -t debugfs none /sys/kernel/debug）"
fi

# ─────────────────────────────────────────── 5. 内核模块
sec "5. 内核模块"
modok() {
	local m=$1 desc=$2
	if grep -qE "^${m//-/_} " /proc/modules 2>/dev/null; then pass "$m" "已加载 — $desc"; return; fi
	if [ -d "/sys/module/${m//-/_}" ]; then pass "$m" "内建/已加载 — $desc"; return; fi
	if have modinfo && T 15 modinfo "$m" >/dev/null 2>&1; then
		warn "$m" "未加载但模块存在；启动前需 modprobe $m — $desc"
	else
		fail "$m" "既没加载也找不到模块 — $desc"
	fi
}
modok nbd   "沙箱根盘走 NBD，封层/回滚都基于它"
modok vhost_net "沙箱网卡"
modok tun   "沙箱网卡"
[ -e /dev/net/tun ] && pass "/dev/net/tun" "存在" || fail "/dev/net/tun" "缺失，沙箱起不来"
NBDMAX=$(cat /sys/module/nbd/parameters/nbds_max 2>/dev/null)
note "nbd.nbds_max" "${NBDMAX:-<未加载>}（单节点并发沙箱数的上限之一）"

# ─────────────────────────────────────────── 6. 大页与内存
sec "6. 大页与内存"
HP_SZ=$(sed -n 's/^Hugepagesize: *//p' /proc/meminfo 2>/dev/null)
HP_TOT=$(sed -n 's/^HugePages_Total: *//p' /proc/meminfo 2>/dev/null)
HP_FREE=$(sed -n 's/^HugePages_Free: *//p' /proc/meminfo 2>/dev/null)
note "Hugepagesize" "${HP_SZ:-<读不到>}"
if [ "$EXPECT_HUGEPAGES" -gt 0 ] 2>/dev/null; then
	if [ "${HP_TOT:-0}" -ge "$EXPECT_HUGEPAGES" ] 2>/dev/null; then
		pass "大页数量 ≥ $EXPECT_HUGEPAGES" "HugePages_Total=$HP_TOT free=$HP_FREE"
	else
		fail "大页数量 ≥ $EXPECT_HUGEPAGES" "HugePages_Total=${HP_TOT:-0}，不够跑预期沙箱数（沙箱 guest 内存走 hugetlb）"
	fi
else
	note "大页数量" "HugePages_Total=${HP_TOT:-0} free=${HP_FREE:-0}（没给 --hugepages，不判定）"
fi
note "MemAvailable" "$(sed -n 's/^MemAvailable: *//p' /proc/meminfo 2>/dev/null)"

# ─────────────────────────────────────────── 7. 产物盘
sec "7. checkpoint 产物盘"
ORCH_PID=""; ORCH_ENV=""
for pid in $(T 10 ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
	[ -r "/proc/$pid/environ" ] || continue
	grep -qa 'ORCHESTRATOR_SERVICES=' "/proc/$pid/environ" 2>/dev/null || continue
	ORCH_PID=$pid
	ORCH_ENV=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null)
	break
done
BASE=$(printf '%s\n' "$ORCH_ENV" | sed -n 's/^ORCHESTRATOR_BASE_PATH=//p' | head -1)
[ -z "$BASE" ] && BASE=/orchestrator
STORE="${STORE_OVERRIDE:-$BASE/build/checkpoints}"
note "ORCHESTRATOR_BASE_PATH" "$BASE$([ -n "$(printf '%s\n' "$ORCH_ENV" | sed -n 's/^ORCHESTRATOR_BASE_PATH=//p')" ] || echo '  (默认值，进程未起或未设)')"
note "checkpoint 产物目录" "$STORE"

PROBE="$STORE"
while [ -n "$PROBE" ] && [ ! -e "$PROBE" ]; do PROBE=$(dirname "$PROBE"); [ "$PROBE" = / ] && break; done
FSTYPE=$(T 15 findmnt -no FSTYPE --target "$PROBE" 2>/dev/null | head -1)
[ -z "$FSTYPE" ] && FSTYPE=$(T 15 df -PT "$PROBE" 2>/dev/null | awk 'NR==2{print $2}')
case "$FSTYPE" in
	ext4|xfs) pass "产物盘文件系统" "$FSTYPE（$PROBE）—— 无特殊要求，不依赖 reflink/FICLONE" ;;
	"")       warn "产物盘文件系统" "判不出来（$PROBE）" ;;
	*)        warn "产物盘文件系统" "$FSTYPE —— 未实测过；实现只用 rename/pwrite，跨文件系统 rename 失败也有拷贝回退，但请确认它支持稀疏文件" ;;
esac
AVAIL_GB=$(T 15 df -PBG "$PROBE" 2>/dev/null | awk 'NR==2{gsub("G","",$4); print $4}')
if [ -n "$AVAIL_GB" ]; then
	if [ "$AVAIL_GB" -ge "$MIN_FREE_GB" ] 2>/dev/null; then
		pass "产物盘剩余 ≥ ${MIN_FREE_GB}G" "${AVAIL_GB}G 可用（$PROBE）"
	else
		fail "产物盘剩余 ≥ ${MIN_FREE_GB}G" "只剩 ${AVAIL_GB}G。CHECKPOINT_FULL_ROOT 默认开 → 每沙箱首次 checkpoint 写一份完整 guest 内存"
	fi
else
	warn "产物盘剩余空间" "df 读不到"
fi
CACHE_FS=$(T 15 findmnt -no FSTYPE --target "$BASE/build" 2>/dev/null | head -1)
CACHE_SRC=$(T 15 findmnt -no SOURCE --target "$BASE/build" 2>/dev/null | head -1)
STORE_SRC=$(T 15 findmnt -no SOURCE --target "$PROBE" 2>/dev/null | head -1)
if [ -n "$CACHE_SRC" ] && [ -n "$STORE_SRC" ] && [ "$CACHE_SRC" != "$STORE_SRC" ]; then
	warn "缓存盘与产物盘同源" "不同设备（$CACHE_SRC vs $STORE_SRC）：封层文件跨文件系统只能拷贝而非 rename，代码有回退但 checkpoint 会变慢"
elif [ -n "$CACHE_SRC" ]; then
	pass "缓存盘与产物盘同源" "$CACHE_SRC（封层 rename 零拷贝）"
fi

# ─────────────────────────────────────────── 8. Firecracker 二进制身份
sec "8. Firecracker 二进制身份"
FCDIR=$(printf '%s\n' "$ORCH_ENV" | sed -n 's/^FIRECRACKER_VERSIONS_DIR=//p' | head -1)
FCDIR_SRC="orchestrator 进程的 FIRECRACKER_VERSIONS_DIR"
if [ -z "$FCDIR" ]; then FCDIR=/fc-versions; FCDIR_SRC="默认值（进程未起）"; fi
note "Firecracker 目录" "$FCDIR（来自 $FCDIR_SRC）"
FCFOUND=0
for fc in "$FCDIR"/*/firecracker; do
	[ -f "$fc" ] || continue
	FCFOUND=$((FCFOUND+1))
	if ! have strings; then
		warn "FC 二进制身份" "$fc 存在但没有 strings 命令，判不了"
		continue
	fi
	S=$(T 60 strings "$fc" 2>/dev/null)
	HAS_SDB=$(printf '%s' "$S" | grep -c 'SaveDirtyBitmap')
	HAS_RB=$(printf '%s' "$S" | grep -c 'RollbackSnapshot')
	if [ "${HAS_SDB:-0}" -gt 0 ] && [ "${HAS_RB:-0}" -gt 0 ]; then
		pass "FC 含回滚 API" "$fc (SaveDirtyBitmap×$HAS_SDB, RollbackSnapshot×$HAS_RB)"
	else
		fail "FC 含回滚 API" "$fc 不是本仓库构建的（SaveDirtyBitmap=$HAS_SDB RollbackSnapshot=$HAS_RB）；restore 会 404"
	fi
done
[ "$FCFOUND" = 0 ] && fail "Firecracker 二进制" "$FCDIR 下没找到 */firecracker"
# 其它位置上的 FC 只作提示：orchestrator 不从那里取，装错了才会用上
for fc in /fc-versions/*/firecracker /opt/e2b-infra/bin/firecracker; do
	[ -f "$fc" ] || continue
	case "$fc" in "$FCDIR"/*) continue ;; esac
	have strings || continue
	n=$(T 60 strings "$fc" 2>/dev/null | grep -c 'RollbackSnapshot')
	note "其它位置的 FC" "$fc RollbackSnapshot=$n（orchestrator 不从这里取，仅提示）"
done

# ─────────────────────────────────────────── 9. Python SDK
sec "9. Python SDK"
PY=""
for p in python3 python; do have "$p" && PY=$(command -v "$p") && break; done
if [ -z "$PY" ]; then
	warn "python3" "没有 python3，SDK 与验收脚本跑不了"
else
	PV=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
	PMAJ=${PV%%.*}; PMIN=${PV#*.}
	if [ "${PMAJ:-0}" -gt 3 ] 2>/dev/null || { [ "${PMAJ:-0}" = 3 ] && [ "${PMIN:-0}" -ge 10 ]; } 2>/dev/null; then
		pass "python ≥ 3.10" "$PV ($PY)"
	else
		fail "python ≥ 3.10" "$PV —— SDK 声明 python = ^3.10"
	fi
	EV=$(T 60 "$PY" -c 'import importlib.metadata as m;print(m.version("e2b"))' 2>/dev/null)
	EP=$(T 60 "$PY" -c 'import e2b,os;print(os.path.dirname(e2b.__file__))' 2>/dev/null)
	if [ -n "$EP" ]; then
		if [ -d "$EP/checkpointd" ] || [ -d "$EP/sandbox/checkpoint" ]; then
			pass "e2b SDK 带 checkpoint" "${EV:-?} @ $EP"
		else
			fail "e2b SDK 带 checkpoint" "$EP 里没有 checkpointd/ —— 装的是上游 PyPI 包，sb.checkpoint 不存在"
		fi
	else
		warn "e2b SDK" "当前 python 里 import e2b 失败（可能装在别的 venv）"
	fi
	HX=$(T 60 "$PY" -c 'import httpx;print(httpx.__version__)' 2>/dev/null)
	if [ -n "$HX" ]; then
		case "$HX" in
		0.1*|0.2[0-6]*) warn "httpx 版本" "$HX —— SDK 要求 >=0.27.0,<1.0.0" ;;
		1.*)            warn "httpx 版本" "$HX —— SDK 要求 <1.0.0" ;;
		*)              pass "httpx 版本" "$HX" ;;
		esac
	else
		warn "httpx" "未安装或 import 失败"
	fi
fi

# ─────────────────────────────────────────── 10. 运行中的栈
sec "10. 运行中的栈（可选，栈没起就跳过）"
if [ -z "$ORCH_PID" ]; then
	note "orchestrator 进程" "没找到（栈未启动）。装好后请复跑本脚本，或直接看启动日志里的 'checkpoint capabilities' 行"
else
	pass "orchestrator 进程" "pid=$ORCH_PID exe=$(readlink -f "/proc/$ORCH_PID/exe" 2>/dev/null)"
	for k in ENVIRONMENT FC_TRACK_DIRTY_PAGES FC_HDBSS_ORDER FC_HDBSS_REQUIRED \
	         CHECKPOINT_FULL_ROOT CHECKPOINT_LOCK_WAIT_TIMEOUT CHECKPOINT_FC_CALL_TIMEOUT \
	         CHECKPOINT_FAULT_INJECT ORCHESTRATOR_BASE_PATH FIRECRACKER_VERSIONS_DIR \
	         HOST_KERNELS_DIR ORCHESTRATOR_SERVICES; do
		v=$(printf '%s\n' "$ORCH_ENV" | sed -n "s/^$k=//p" | head -1)
		note "env $k" "${v:-<未设，用默认值>}"
	done
	FI=$(printf '%s\n' "$ORCH_ENV" | sed -n 's/^CHECKPOINT_FAULT_INJECT=//p' | head -1)
	[ -n "$FI" ] && fail "CHECKPOINT_FAULT_INJECT" "生产机器上绝不该设它（=$FI）：它会人为让 checkpoint 失败"
	CAPLINE=""
	if have journalctl; then
		CAPLINE=$(T 60 journalctl --no-pager -o cat 2>/dev/null | grep -F 'checkpoint capabilities' | tail -1)
	fi
	[ -z "$CAPLINE" ] && [ -r /var/log/e2b/orchestrator.log ] && \
		CAPLINE=$(T 30 grep -F 'checkpoint capabilities' /var/log/e2b/orchestrator.log 2>/dev/null | tail -1)
	if [ -n "$CAPLINE" ]; then
		note "capabilities 日志" "$(printf '%s' "$CAPLINE" | tail -c 400)"
		case "$CAPLINE" in
		*'"track_dirty_pages":true'*|*'track_dirty_pages=true'*) pass "脏页跟踪已开" "增量 checkpoint 生效" ;;
		*) warn "脏页跟踪" "日志显示未开启 → 每次 checkpoint 全量拷贝内存" ;;
		esac
	else
		note "capabilities 日志" "没抓到（日志不在 journald？）。启动日志里搜 'checkpoint capabilities'"
	fi
fi

# ─────────────────────────────────────────── 汇总
echo ""
printf "汇总: ${GRN}PASS %d${NC}  ${YLW}WARN %d${NC}  ${RED}FAIL %d${NC}\n" "$N_PASS" "$N_WARN" "$N_FAIL"
if [ "$N_FAIL" -eq 0 ]; then
	echo "结论: 这台机器可以跑沙箱级快照回滚。"
	[ "$CAP_VERDICT" = "no" ] && echo "      注意: 无 HDBSS —— 功能正确但每次 checkpoint 全量拷贝 guest 内存，慢一个量级。"
	[ "$CAP_VERDICT" = "reported-but-broken" ] && echo "      注意: 报告了 cap 502 但启用失败 —— 会静默退到 kvm-wp（更慢）。考虑 FC_HDBSS_REQUIRED=true。"
else
	echo "结论: 有 $N_FAIL 项阻断，修完再部署。"
fi
exit "$N_FAIL"
