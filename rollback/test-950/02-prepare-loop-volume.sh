#!/usr/bin/env bash
# 给某一套方案准备数据卷。两台机器、两种文件系统共用这一个脚本，
# loop 的参数完全一致 —— 这样两套方案背的是同一份 loop 开销，
# 方案之间的比较才成立。
#
# 用法: 02-prepare-loop-volume.sh --fs <xfs|ext4> [--mnt DIR] [--size 300G]
#                                 [--img PATH] [--device /dev/sdX] [--force]
#
#   --fs      必填。xfs 套用 xfs（要 reflink），ext4 套用 ext4
#   --mnt     默认 /mnt/<fs>dev
#   --size    默认 300G。**预分配**，不是稀疏
#   --img     镜像路径，默认自动挑剩余空间最多的目录
#   --device  在真盘/真分区上建，会抹掉该设备的数据（不走 loop）
#   --force   挂载点已挂载时先卸掉重建；镜像已存在时重建
#
# loop 的三项调优（默认全开，跟裸盘的差距主要来自这三项）：
#   1. --direct-io=on   loop 写宿主镜像时绕开宿主页缓存，消掉双份缓存与双回写
#   2. 预分配           fallocate 一次分配到位，消掉写路径上的 extent 分配与碎片
#   3. -b 4096          loop 逻辑扇区对齐到 4K，消掉内层 4K 块的读改写
#   另加 noatime。
#
# 两台机器都没有空闲裸盘可用（920B / 950 的 vgs 都是 VFree=0），所以默认走 loop。
# loop 相对裸盘仍有开销，但两套同等承担；要拿绝对性能数就用 --device。
set -uo pipefail

FS="" MNT="" SIZE=300G IMG="" DEV="" FORCE=0
while [ $# -gt 0 ]; do
	case "$1" in
		--fs)     FS=${2:-}; shift 2;;
		--mnt)    MNT=${2:-}; shift 2;;
		--size)   SIZE=${2:-}; shift 2;;
		--img)    IMG=${2:-}; shift 2;;
		--device) DEV=${2:-}; shift 2;;
		--force)  FORCE=1; shift;;
		*) echo "未知参数 $1" >&2; exit 2;;
	esac
done

die() { echo "$*" >&2; exit 1; }

case "$FS" in
	xfs|ext4) ;;
	*) die "用法: $0 --fs <xfs|ext4> [--mnt DIR] [--size 300G] [--img PATH] [--device /dev/sdX] [--force]";;
esac
MNT=${MNT:-/mnt/${FS}dev}

command -v "mkfs.$FS" >/dev/null || die "缺 mkfs.$FS —— xfs 装 xfsprogs，ext4 装 e2fsprogs"
[ "$FS" = xfs ] && { command -v xfs_info >/dev/null || die "缺 xfs_info —— dnf install -y xfsprogs"; }

# ---------- 已挂载的处理 ----------
if mountpoint -q "$MNT"; then
	cur=$(findmnt -no FSTYPE --target "$MNT")
	echo "$MNT 已挂载：$(findmnt -no SOURCE,FSTYPE,OPTIONS --target "$MNT")"
	if [ "$FORCE" = 1 ]; then
		echo "  --force：先卸载"
		src=$(findmnt -no SOURCE --target "$MNT")
		umount "$MNT" || die "  卸载失败（还有进程占着？先停 template-manager）"
		case "$src" in /dev/loop*) losetup -d "$src" 2>/dev/null;; esac
	elif [ "$cur" = "$FS" ]; then
		echo "  文件系统类型已对（$FS）。要按新参数重建请加 --force"
		exit 0
	else
		die "  类型是 $cur，要的是 $FS —— 加 --force 重建，或换 --mnt"
	fi
fi

mkdir -p "$MNT"

mkfs_opts() {
	# reflink 是 XFS 套的全部意义所在；crc 是 reflink 的前提。
	# nodiscard（xfs 是 -K）非常关键：mkfs 默认会对整个设备发一次 discard，
	# 而 loop 把 discard 翻译成对宿主镜像打洞 —— 那样刚做的预分配会被当场捅穿，
	# 文件又变回稀疏的，第 2 项调优白做。
	[ "$FS" = xfs ] && echo "-f -K -m reflink=1,crc=1 -b size=4096" \
	                || echo "-F -E nodiscard -b 4096 -L e2b-${FS}dev"
}

# ---------- 真盘 ----------
if [ -n "$DEV" ]; then
	[ -b "$DEV" ] || die "$DEV 不是块设备"
	echo "将在 $DEV 上创建 $FS。设备上的数据会被抹掉。"
	lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT "$DEV" | sed 's/^/  /'
	read -r -p "确认设备是 $DEV ？输入 yes 继续: " a
	[ "$a" = yes ] || die "已取消"
	# shellcheck disable=SC2046
	"mkfs.$FS" $(mkfs_opts) "$DEV" >/dev/null || die "mkfs.$FS 失败"
	mount -o noatime "$DEV" "$MNT" || die "挂载失败"
	SRC=$DEV
else
	# ---------- loop ----------
	if [ -z "$IMG" ]; then
		best="" bestfree=0
		for d in /home /data /var/lib /; do
			[ -d "$d" ] || continue
			free=$(df -BG --output=avail "$d" 2>/dev/null | tail -1 | tr -dc '0-9')
			[ -z "$free" ] && continue
			[ "$free" -gt "$bestfree" ] && { bestfree=$free; best=$d; }
		done
		[ -n "$best" ] || die "找不到可用目录放镜像"
		IMG="$best/e2b-${FS}vol.img"
		echo "自动选定镜像位置：$IMG（$best 剩余 ${bestfree}G，各候选中最多）"
		echo "  要换地方就 --img /path/to.img"
	fi

	imgdir=$(dirname "$IMG"); mkdir -p "$imgdir"
	want=$(echo "$SIZE" | tr -dc '0-9')
	case "$SIZE" in *T|*t) want=$((want*1024));; esac
	free=$(df -BG --output=avail "$imgdir" 2>/dev/null | tail -1 | tr -dc '0-9')
	echo "  $imgdir（$(findmnt -no FSTYPE --target "$imgdir")）剩余 ${free}G，镜像 $SIZE 预分配"
	[ -n "$free" ] && [ "$free" -lt "$want" ] && \
		die "  剩余空间不够预分配 $SIZE。预分配是要真占地方的，换小一点或换个目录。"

	if [ -f "$IMG" ] && [ "$FORCE" != 1 ]; then
		echo "复用已有镜像 $IMG（标称 $(stat -c %s "$IMG") B / 实占 $(du -sh "$IMG" | cut -f1)）"
		echo "  注意：复用的镜像不保证是按当前参数建的。要保证一致请加 --force"
	else
		[ -f "$IMG" ] && { echo "  --force：删除旧镜像"; rm -f "$IMG"; }
		echo "预分配镜像 $IMG（$SIZE）"
		# fallocate 一次把 extent 分配到位（XFS/ext4 上都是 unwritten extent，
		# 不写零、瞬间完成），写路径上就不再有分配开销。truncate 那种稀疏
		# 文件正相反：每碰一块新区域都要在宿主文件系统里分配一次。
		fallocate -l "$SIZE" "$IMG" || die "fallocate 失败（宿主文件系统不支持预分配？）"
	fi

	# -b 4096: loop 的逻辑扇区。默认 512，会让内层 4K 块的写变成读改写。
	# --direct-io=on: loop 写宿主镜像时绕开宿主页缓存 —— 双缓存是 loop 最大的一笔开销。
	SRC=$(losetup -b 4096 --direct-io=on -f --show "$IMG") || die "losetup 失败"
	echo "  loop 设备 $SRC"

	# shellcheck disable=SC2046
	"mkfs.$FS" $(mkfs_opts) "$SRC" >/dev/null || die "mkfs.$FS 失败"
	mount -o noatime "$SRC" "$MNT" || die "挂载失败"
fi

# ---------- 自检 ----------
echo
echo "=== 卷参数自检 ==="
findmnt -no SOURCE,FSTYPE,OPTIONS --target "$MNT" | sed 's/^/  挂载: /'
if [ -z "$DEV" ]; then
	losetup -l "$SRC" | tail -1 | sed 's/^/  loop: /'
	dio=$(losetup -l -O DIO --noheadings "$SRC" | tr -d ' ')
	sec=$(losetup -l -O LOG-SEC --noheadings "$SRC" | tr -d ' ')
	[ "$dio" = 1 ] && echo "  direct-io ✓" || echo "  ⚠ direct-io 没开（$dio）—— 双缓存开销还在"
	[ "$sec" = 4096 ] && echo "  逻辑扇区 4096 ✓" || echo "  ⚠ 逻辑扇区 $sec，不是 4096"
	blocks=$(du -B1 --apparent-size "$IMG" | cut -f1)
	used=$(du -B1 "$IMG" | cut -f1)
	pct=$(( used * 100 / (blocks > 0 ? blocks : 1) ))
	if [ "$pct" -ge 95 ]; then
		echo "  预分配 ✓（实占/标称 = ${pct}%）"
	else
		echo "  ⚠ 只占了标称的 ${pct}% —— 还是稀疏的，写路径上仍有分配开销。"
		echo "    最常见的原因是 mkfs 的 discard 把预分配打穿了（本脚本已加 nodiscard/-K）。"
	fi
fi

if [ "$FS" = xfs ]; then
	xfs_info "$MNT" | sed 's/^/  /'
	xfs_info "$MNT" | grep -q 'reflink=1' || die "reflink 没开，停"
	# 真正的判据不是 xfs_info 怎么说，是 FICLONE 到底能不能用。
	T=$(mktemp -d "$MNT/.clonetest.XXXXXX")
	dd if=/dev/zero of="$T/a" bs=1M count=1 2>/dev/null
	if cp --reflink=always "$T/a" "$T/b" 2>/dev/null; then
		echo "  FICLONE 实测可用 ✓"
	else
		rm -rf "$T"; die "  FICLONE 实测失败 —— XFS 套跑不了，先查这个"
	fi
	rm -rf "$T"

	# CoW extent 提示。默认 128 KB，往 reflink 克隆里写零散 4 KB 脏页时按 128 KB
	# 粒度分配，零散那部分被放大 32 倍 —— 实测每次 checkpoint 固定多花约 33 MB。
	# 设在挂载点根上，后面创建的目录和文件都继承，不用等 orchestrator 把
	# build/checkpoints 建出来再补设（那一步很容易忘，忘了也不报错）。
	if xfs_io -c "cowextsize 4096" "$MNT" >/dev/null 2>&1; then
		echo "  cowextsize 4096 ✓（默认 128K 会让每次 checkpoint 多花约 33MB）"
	else
		echo "  ⚠ cowextsize 设置失败 —— 物理占用会比 ext4 套每次多约 33MB，不影响正确性"
	fi
fi

mkdir -p "$MNT/orchestrator"
df -h "$MNT" | tail -1 | sed 's/^/  /'
echo
echo "就绪。跑这一套之前用："
echo "  bash 03-switch.sh $FS --yes --base $MNT/orchestrator"
echo
echo "跑完之后清理（loop 是临时的，撤掉即可，不动宿主任何东西）："
echo "  umount $MNT"
[ -z "$DEV" ] && { echo "  losetup -d $SRC"; echo "  rm -f $IMG"; }
