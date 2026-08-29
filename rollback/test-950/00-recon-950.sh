#!/usr/bin/env bash
# 950 摸底：只读，不改任何东西，不需要网络。
#
# 目的：把我在 920B 上猜不到的东西问清楚——磁盘有多大、XFS 卷该切多少、
# 内核认不认 HDBSS、缺哪些工具——然后据此把 01~04 和测试脚本调准。
#
# 用法:  bash 00-recon-950.sh 2>&1 | tee recon-950.txt
# 然后把 recon-950.txt 整个发回来即可。
#
# 唯一会写盘的地方：$TMPDIR 下的几个探针临时文件，退出时删掉。\n#\n# 每个可能扫全盘的命令都加了 timeout，卡住会打印"(超时跳过)"继续，\n# 不会把整份报告拖死。

set -u
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
say() { printf '\n===== %s =====\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "950 摸底报告  生成于 $(date -Is 2>/dev/null || date)"

say "1. 基本信息"
echo "hostname : $(hostname 2>/dev/null)"
echo "kernel   : $(uname -r)"
echo "arch     : $(uname -m)"
grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release 2>/dev/null
echo "uptime   : $(uptime -p 2>/dev/null || uptime)"

say "2. CPU"
if have lscpu; then
	lscpu 2>/dev/null | grep -iE 'architecture|model name|^cpu\(s\)|thread|core|socket|numa node\(s\)|bogomips|flags' | head -12
fi
echo "--- MIDR / 实现者 ---"
grep -m4 -E 'CPU implementer|CPU architecture|CPU variant|CPU part' /proc/cpuinfo 2>/dev/null
echo "--- 硬件特性 ---"
grep -m1 '^Features' /proc/cpuinfo 2>/dev/null | tr ' ' '\n' | grep -iE 'hafdbs|hdbss|dbm|hpds' | tr '\n' ' '; echo
echo "(注：HDBSS 是华为扩展，通常不出现在 Features 里，以下面的内核/KVM 探测为准)"

say "3. 内存与大页"
grep -E '^(MemTotal|MemAvailable|HugePages_Total|HugePages_Free|Hugepagesize)' /proc/meminfo 2>/dev/null
for d in /sys/kernel/mm/hugepages/*/; do
	[ -d "$d" ] || continue
	echo "  $(basename "$d"): nr=$(cat "$d/nr_hugepages" 2>/dev/null) free=$(cat "$d/free_hugepages" 2>/dev/null)"
done

say "4. 内核对 HDBSS 的支持"
CFG=""
for c in /boot/config-$(uname -r) /proc/config.gz /boot/config; do
	[ -e "$c" ] && CFG=$c && break
done
if [ -n "$CFG" ]; then
	echo "内核配置文件: $CFG"
	if [ "${CFG##*.}" = "gz" ]; then RD="zcat"; else RD="cat"; fi
	$RD "$CFG" 2>/dev/null | grep -iE 'HDBSS|ARM64_HAFT|HW_AFDBM|CONFIG_KVM=' | sed 's/^/  /'
	echo "  (若一行都没有 → 这个内核根本没编 HDBSS，950 上也用不了硬件标脏)"
else
	echo "！找不到内核配置（/boot/config-$(uname -r) 与 /proc/config.gz 都没有）"
	echo "  可以试：rpm -qf /boot/config-\$(uname -r)  或  ls /boot/"
	ls /boot/ 2>/dev/null | head -10 | sed 's/^/    /'
fi
echo "--- dmesg 里的 HDBSS 痕迹 ---"
timeout 15 dmesg 2>/dev/null | grep -iE 'hdbss|kvm.*dirty|arm-smmu.*hdbss' | tail -10 | sed 's/^/  /' || echo "  (读不到 dmesg 或没有相关行)"
echo "--- 内核模块参数 ---"
ls /sys/module/kvm/parameters/ 2>/dev/null | tr '\n' ' '; echo
for p in /sys/module/kvm/parameters/*hdbss* /sys/module/kvm_arm/parameters/*hdbss*; do
	[ -e "$p" ] && echo "  $p = $(cat "$p" 2>/dev/null)"
done

say "5. KVM 与 cap 502 探针"
ls -l /dev/kvm 2>/dev/null || echo "！/dev/kvm 不存在"
if have gcc; then
	cat > "$TMP/cap.c" <<'CEOF'
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <linux/kvm.h>
#ifndef KVM_CAP_ARM_HW_DIRTY_STATE_TRACK
#define KVM_CAP_ARM_HW_DIRTY_STATE_TRACK 502
#endif
int main(void) {
	int kvm = open("/dev/kvm", O_RDWR | O_CLOEXEC);
	if (kvm < 0) { perror("open /dev/kvm"); return 2; }
	int r = ioctl(kvm, KVM_CHECK_EXTENSION, KVM_CAP_ARM_HW_DIRTY_STATE_TRACK);
	printf("KVM_CHECK_EXTENSION(502) = %d  %s\n", r,
	       r > 0 ? "-> supported" : "-> NOT supported");
	int vm = ioctl(kvm, KVM_CREATE_VM, 0);
	if (vm < 0) { perror("KVM_CREATE_VM"); close(kvm); return 0; }
	r = ioctl(vm, KVM_CHECK_EXTENSION, KVM_CAP_ARM_HW_DIRTY_STATE_TRACK);
	printf("per-VM  CHECK_EXTENSION(502) = %d\n", r);
	struct kvm_enable_cap cap;
	memset(&cap, 0, sizeof(cap));
	cap.cap = KVM_CAP_ARM_HW_DIRTY_STATE_TRACK;
	cap.args[0] = 1;                 /* order 1 = 8 KiB per vCPU */
	r = ioctl(vm, KVM_ENABLE_CAP, &cap);
	printf("KVM_ENABLE_CAP(502, order=1) = %d  %s\n", r,
	       r == 0 ? "-> 真的能开" : strerror(0));
	if (r != 0) perror("  KVM_ENABLE_CAP");
	close(vm); close(kvm);
	return 0;
}
CEOF
	if gcc -o "$TMP/cap" "$TMP/cap.c" 2>"$TMP/cc.err"; then
		"$TMP/cap" 2>&1 | sed 's/^/  /'
	else
		echo "  编译失败:"; head -5 "$TMP/cc.err" | sed 's/^/    /'
	fi
else
	echo "！没有 gcc，跳过 cap 502 探针（这是判断 HDBSS 能不能用的关键，建议装上 gcc 再跑一次）"
fi

say "6. 磁盘与文件系统（决定 XFS 卷切多大）"
# 排除 loop(7) / nbd(43) / ram(1)：跑着 e2b 的机器上这三类有成百上千个，
# 全列出来会把报告淹掉，而且跟"哪里能切 XFS 卷"无关。
echo "--- lsblk（已排除 loop/nbd/ram）---"
timeout 20 lsblk -e 7,43,1 -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL 2>/dev/null | head -40 | sed 's/^/  /'
echo "--- df（只看真实文件系统）---"
timeout 20 df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs 2>/dev/null | head -25 | sed 's/^/  /'
echo "--- 根盘与关键目录落在哪 ---"
for d in / /opt /orchestrator /fc-versions /data /var/lib/nomad /home; do
	[ -e "$d" ] && printf '  %-20s %s\n' "$d" "$(findmnt -no FSTYPE,SOURCE,TARGET --target "$d" 2>/dev/null | head -1)"
done
echo "--- LVM 有没有空闲空间（做 XFS 卷最省事的来源）---"
# lvm 工具会重扫所有块设备；机器上 loop/dm/nbd 一多就可能卡几分钟，
# 所以每条都加超时，卡住就跳过而不是把整个报告拖死。
if have vgs; then
	timeout 20 vgs -o vg_name,vg_size,vg_free --noheadings --units g 2>/dev/null | sed 's/^/  VG /' || echo "  VG (超时跳过)"
	timeout 20 lvs -o lv_name,vg_name,lv_size --noheadings --units g 2>/dev/null | head -20 | sed 's/^/  LV /' || echo "  LV (超时跳过)"
	timeout 20 pvs --noheadings --units g 2>/dev/null | head -10 | sed 's/^/  PV /' || echo "  PV (超时跳过)"
else
	echo "  (没有 lvm 工具)"
fi
echo "--- 整块没被用起来的裸盘 ---"
timeout 20 lsblk -dn -e 7,43,1 -o NAME,SIZE,TYPE 2>/dev/null | while read -r n s t; do
	[ "$t" = disk ] || continue
	children=$(( $(timeout 10 lsblk -n -o NAME "/dev/$n" 2>/dev/null | wc -l) - 1 ))
	mounted=$(timeout 10 lsblk -n -o MOUNTPOINT "/dev/$n" 2>/dev/null | grep -c '[^[:space:]]')
	fstype=$(timeout 10 lsblk -dn -o FSTYPE "/dev/$n" 2>/dev/null)
	printf '  /dev/%-8s %-8s 子设备=%-3s 已挂载=%-3s 自身fs=%s\n' "$n" "$s" "$children" "$mounted" "${fstype:-无}"
done
echo "--- 根文件系统剩余空间（若只能用 loop 文件造 XFS 卷，这决定上限）---"
df -h --output=source,fstype,size,used,avail,pcent / 2>/dev/null | sed 's/^/  /'

say "7. reflink 支持"
echo "--- 现有各挂载点实测 FICLONE ---"
for m in / /opt /home /data; do
	[ -d "$m" ] || continue
	t=$(findmnt -no FSTYPE --target "$m" 2>/dev/null | head -1)
	d="$m/.reflink-probe.$$"
	if mkdir -p "$d" 2>/dev/null; then
		dd if=/dev/zero of="$d/a" bs=4k count=4 2>/dev/null
		if cp --reflink=always "$d/a" "$d/b" 2>/dev/null; then r=yes; else r=no; fi
		rm -rf "$d"
	else
		r="(不可写)"
	fi
	printf '  %-10s fs=%-6s reflink=%s\n' "$m" "$t" "$r"
done
echo "--- mkfs.xfs 版本与 reflink 默认值 ---"
if have mkfs.xfs; then
	mkfs.xfs -V 2>&1 | sed 's/^/  /'
	echo "  (xfsprogs >= 5.1 默认 reflink=1；我们的脚本会显式带 -m reflink=1,crc=1)"
else
	echo "！没有 mkfs.xfs —— XFS 那套跑不了，需要先装 xfsprogs"
fi
have xfs_info && echo "  xfs_info: 有" || echo "  xfs_info: 无"

say "8. e2b 部署现状"
timeout 30 rpm -qa 2>/dev/null | grep -i e2b | sed 's/^/  rpm: /' || echo "  (查不到 rpm)"
echo "  /opt/e2b-infra 存在: $([ -d /opt/e2b-infra ] && echo yes || echo no)"
[ -d /opt/e2b-infra/bin ] && { echo "  /opt/e2b-infra/bin:"; ls /opt/e2b-infra/bin 2>/dev/null | tr '\n' ' ' | fold -w 100 -s | sed 's/^/    /'; }
echo "  --- 关键二进制 ---"
for f in /usr/bin/orchestrator /usr/bin/template-manager /opt/e2b-infra/bin/orchestrator /opt/e2b-infra/bin/firecracker; do
	[ -e "$f" ] && printf '    %-40s %10s B  sha=%s\n' "$f" "$(stat -c %s "$f")" "$(sha256sum "$f" 2>/dev/null | cut -c1-16)"
done
echo "  --- /fc-versions ---"
ls -la /fc-versions/*/ 2>/dev/null | sed 's/^/    /' || echo "    (不存在)"
echo "  --- 服务 ---"
for s in nomad consul docker; do
	printf '    %-8s %s\n' "$s" "$(systemctl is-active $s 2>/dev/null || echo '-')"
done
have nomad && nomad version 2>/dev/null | head -1 | sed 's/^/    /'
echo "  --- nomad job（需要 token，读不到就算了）---"
( [ -f /opt/e2b-infra/.env ] && . /opt/e2b-infra/.env 2>/dev/null; \
  export NOMAD_TOKEN="${NOMAD_ACL_TOKEN:-}"; nomad job status 2>/dev/null | head -8 | sed 's/^/    /' ) || true
echo "  --- e2b API 端口 ---"
for p in 3000 5007 5008 4646; do
	printf '    :%-5s %s\n' "$p" "$(ss -lntp 2>/dev/null | grep -c ":$p ")"
done

say "9. 工具链齐不齐（测试脚本要用）"
for t in gcc make python3 pip3 perf curl jq git rsync findmnt losetup truncate dd md5sum sha256sum; do
	printf '  %-10s %s\n' "$t" "$(command -v $t 2>/dev/null || echo '缺')"
done
echo "  python3 版本: $(python3 -V 2>&1)"
echo "  e2b SDK    : $(python3 -c 'import e2b,sys;print(getattr(e2b,"__version__","已装(无版本号)"))' 2>&1 | tail -1)"

say "10. 现有沙箱/模板占用"
for d in /orchestrator /orchestrator/template /orchestrator/sandbox /orchestrator/build; do
	[ -d "$d" ] && printf '  %-28s 条目=%-6s 实占=%s\n' "$d" "$(ls "$d" 2>/dev/null | wc -l)" "$(du -sh "$d" 2>/dev/null | cut -f1)"
done

say "报告结束"
echo "把这份输出整个发回来即可。特别关注："
echo "  · 第 4/5 节 —— 决定 950 上到底能不能用 HDBSS"
echo "  · 第 6 节   —— 决定 XFS 卷从哪切、切多大"
echo "  · 第 9 节   —— 缺哪些工具需要先装"
