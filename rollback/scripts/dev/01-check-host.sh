#!/usr/bin/env bash
# Host self-check. Reads only — changes nothing, starts no sandbox.
set -uo pipefail
cd "$(dirname "$0")"

hr() { printf '\n=== %s ===\n' "$1"; }
ok() { printf '  [ok]   %s\n' "$1"; }
no() { printf '  [WARN] %s\n' "$1"; }

hr "机器"
printf '  kernel: %s\n' "$(uname -r)"
printf '  arch:   %s\n' "$(uname -m)"
printf '  cpu:    %s\n' "$(lscpu 2>/dev/null | sed -n 's/^Model name: *//p' | head -1)"

hr "HDBSS：内核编译开关"
CFG=""
for c in "/boot/config-$(uname -r)" /proc/config.gz; do
	[ -e "$c" ] && CFG="$c" && break
done
if [ -z "$CFG" ]; then
	no "找不到内核配置（/boot/config-$(uname -r) 或 /proc/config.gz），跳过；以 cap 探针为准"
elif [ "$CFG" = /proc/config.gz ]; then
	zgrep -q '^CONFIG_ARM64_HDBSS=y' "$CFG" && ok "CONFIG_ARM64_HDBSS=y" || no "CONFIG_ARM64_HDBSS 未开"
else
	grep -q '^CONFIG_ARM64_HDBSS=y' "$CFG" && ok "CONFIG_ARM64_HDBSS=y" || no "CONFIG_ARM64_HDBSS 未开（$CFG）"
fi

hr "HDBSS：KVM 模式与能力"
if [ -r /sys/module/kvm_arm/parameters/mode ]; then
	MODE=$(cat /sys/module/kvm_arm/parameters/mode)
	[ "$MODE" = vhe ] && ok "KVM 运行在 VHE 模式" || no "KVM mode = $MODE（HDBSS 需要 VHE）"
else
	no "读不到 /sys/module/kvm_arm/parameters/mode"
fi

if [ ! -e /dev/kvm ]; then
	no "/dev/kvm 不存在"
elif ! command -v gcc >/dev/null; then
	no "没有 gcc，无法编译 cap 探针；手工编译 cap_test.c 后运行"
else
	gcc -static -o /tmp/cap_test cap_test.c 2>/tmp/cap_test.err || gcc -o /tmp/cap_test cap_test.c 2>>/tmp/cap_test.err
	if [ -x /tmp/cap_test ]; then
		/tmp/cap_test | sed 's/^/  /'
	else
		no "cap 探针编译失败：$(tail -2 /tmp/cap_test.err)"
	fi
fi

hr "文件系统"
for m in / /mnt/xfsdev "${1:-}"; do
	[ -z "$m" ] && continue
	[ -d "$m" ] || continue
	FS=$(findmnt -no FSTYPE --target "$m" 2>/dev/null)
	SRC=$(findmnt -no SOURCE --target "$m" 2>/dev/null)
	AVAIL=$(df -h --output=avail "$m" 2>/dev/null | tail -1 | tr -d ' ')
	printf '  %-14s fstype=%-6s source=%-40s avail=%s\n' "$m" "$FS" "$SRC" "$AVAIL"
	if [ "$FS" = xfs ]; then
		if xfs_info "$m" 2>/dev/null | grep -q 'reflink=1'; then
			ok "$m 支持 reflink（XFS 套可用）"
		else
			no "$m 是 XFS 但 reflink=0——XFS 套会退化成整份拷贝，必须重造卷"
		fi
	fi
	# The real test: does FICLONE actually work here?
	TD=$(mktemp -d "$m/.clonetest.XXXXXX" 2>/dev/null) || continue
	dd if=/dev/zero of="$TD/a" bs=1M count=1 2>/dev/null
	if cp --reflink=always "$TD/a" "$TD/b" 2>/dev/null; then ok "$m FICLONE 实测可用"; else no "$m FICLONE 实测不可用"; fi
	rm -rf "$TD"
done

hr "二进制身份"
if [ -f bin/SHA256SUMS ]; then
	(cd bin && sha256sum -c SHA256SUMS 2>&1 | sed 's/^/  /')
else
	no "bin/SHA256SUMS 缺失"
fi
for b in bin/fc-xfs bin/fc-ext4; do
	[ -x "$b" ] || continue
	printf '  %-16s save-dirty-bitmap 端点: %s\n' "$(basename $b)" \
		"$(strings "$b" | grep -c SaveDirtyBitmap)"
done
printf '  （fc-ext4 必须是 2，fc-xfs 必须是 0；反了就是拿错了）\n'
for b in bin/orchestrator-xfs bin/orchestrator-ext4; do
	[ -x "$b" ] || continue
	if "$b" --help >/dev/null 2>&1 || [ $? -le 2 ]; then ok "$(basename $b) 能在本机运行"; else no "$(basename $b) 无法运行（glibc 版本？）"; fi
done

hr "现有部署"
printf '  FIRECRACKER_VERSIONS_DIR 候选:\n'
ls -d /fc-versions/* 2>/dev/null | sed 's/^/    /' || printf '    (无 /fc-versions)\n'
printf '  nomad 配置: '
[ -f /opt/e2b-infra/nomad/template-manager.hcl ] && echo "/opt/e2b-infra/nomad/template-manager.hcl" || echo "(未找到，03-switch.sh 需要 --hcl 指定)"
printf '\n检查完毕。上面任何一行 [WARN] 都先弄清楚再往下走。\n'
