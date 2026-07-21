#!/usr/bin/env bash

set -euo pipefail

# Set timestamp format
PS4='[\D{%Y-%m-%d %H:%M:%S}] '
# Enable command tracing
set -x

# Add cache disk for orchestrator and swapfile
MOUNT_POINT="/orchestrator"

# Step 2: Create the mount point
sudo mkdir -p $MOUNT_POINT

sudo mkdir -p /orchestrator/sandbox
sudo mkdir -p /orchestrator/template
sudo mkdir -p /orchestrator/build

# 定义swap文件路径（仅定义一次，避免冗余）
SWAPFILE="/swapfile"
SWAP_SIZE="1G"

# 1. 检查swap文件是否已存在
if [ ! -e "$SWAPFILE" ]; then
    echo "开始创建 $SWAP_SIZE 大小的swap文件：$SWAPFILE"
    
    # 创建swap文件（fallocate失败时，降级使用dd命令，兼容更多系统）
    if ! fallocate -l "$SWAP_SIZE" "$SWAPFILE"; then
        echo "fallocate命令失败"
    fi

    # 设置swap文件权限（必须600，否则mkswap会警告）
    chmod 600 "$SWAPFILE"
    
    # 格式化swap文件
    if !  mkswap "$SWAPFILE"; then
        echo "错误：格式化swap文件失败！"
        rm -f "$SWAPFILE"  # 清理失败的文件
        exit 1
    fi

    # 启用swap文件
    if ! swapon "$SWAPFILE"; then
        echo "错误：启用swap文件失败！"
        rm -f "$SWAPFILE"  # 清理失败的文件
        exit 1
    fi

    echo "✅ swap文件创建并启用成功！"
else
    echo "ℹ️ Swapfile $SWAPFILE 已存在，跳过创建步骤。"
fi

# 2. 设置swap永久生效（避免重复写入fstab）
echo "检查swap配置是否已写入/etc/fstab..."
if ! grep -q "^$SWAPFILE\s\+none\s\+swap\s\+sw\s\+0\s\+0$" /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
    echo "✅ swap配置已写入/etc/fstab，重启后自动生效。"
else
    echo "ℹ️ swap配置已存在于/etc/fstab，无需重复写入。"
fi

# 3. 验证swap状态（可选，输出当前swap信息）
echo -e "\n当前swap状态："
swapon --show
echo -e "\n内存+swap总览："
free -h

# Set swap settings
sudo sysctl vm.swappiness=10
sudo sysctl vm.vfs_cache_pressure=50

# Add tmpfs for snapshotting
# TODO: Parametrize this
sudo mkdir -p /mnt/snapshot-cache
# Idempotent: don't stack another tmpfs on every re-run
mountpoint -q /mnt/snapshot-cache || sudo mount -t tmpfs -o size=65G tmpfs /mnt/snapshot-cache

ulimit -n 1048576
export GOMAXPROCS='nproc'

# Idempotent: only append this sysctl block once (re-runs used to append it every time)
if ! grep -q '^net.core.somaxconn = 65535' /etc/sysctl.conf 2>/dev/null; then
sudo tee -a /etc/sysctl.conf <<EOF
# Increase the maximum number of socket connections
net.core.somaxconn = 65535

# Increase the maximum number of backlogged connections
net.core.netdev_max_backlog = 65535

# Increase maximum number of TCP sockets
net.ipv4.tcp_max_syn_backlog = 65535

# Increase the maximum number of memory map areas
vm.max_map_count=1048576

EOF
fi
sudo sysctl -p

echo "Disabling inotify for NBD devices"
# https://lore.kernel.org/lkml/20220422054224.19527-1-matthew.ruffell@canonical.com/
cat <<EOH >/etc/udev/rules.d/97-nbd-device.rules
# Disable inotify watching of change events for NBD devices
ACTION=="add|change", KERNEL=="nbd*", OPTIONS:="nowatch"
EOH

sudo udevadm control --reload-rules
sudo udevadm trigger

# 自定义 nbd 模块（nbds_max=512）不在此处加载：服务器重启后手动执行一次
# rmmod nbd && insmod nbd.ko nbds_max=512 即可，见 single-node-offline-deploy.md 0.2

# Create the directory for the fc mounts
mkdir -p /fc-vm

# Download envd buckets
envd_dir="/fc-envd"
mkdir -p $envd_dir

cp ./bin/envd "${envd_dir}/."

chmod -R 755 $envd_dir
ls -lh $envd_dir
du -h "${envd_dir}/envd"



FIRECRACKER_VERSION=1.12.1

# Download kernels
kernels_dir="/fc-kernels"
mkdir -p ${kernels_dir}/vmlinux-6.1.102/
cp ./bin/vmlinux.bin "${kernels_dir}/vmlinux-6.1.102/"
chmod -R 755 $kernels_dir
ls -lh $kernels_dir

# Install FC versions
fc_versions_dir="/fc-versions"
mkdir -p $fc_versions_dir
mkdir -p $fc_versions_dir/v${FIRECRACKER_VERSION}
cd /opt/e2b-infra
# RPM 自带定制版 firecracker（bin/firecracker，仅 aarch64 打包），直接安装，不再下载官方 tgz
if [ ! -f ./bin/firecracker ]; then
    echo "错误：/opt/e2b-infra/bin/firecracker 不存在（x86_64 RPM 不打包定制 firecracker）！"
    exit 1
fi
cp ./bin/firecracker "${fc_versions_dir}/v${FIRECRACKER_VERSION}/firecracker"
chmod +x ${fc_versions_dir}/v${FIRECRACKER_VERSION}/firecracker
chmod -R 755 $fc_versions_dir/v${FIRECRACKER_VERSION}
ls -lh $fc_versions_dir

# Set up huge pages
# We are not enabling Transparent Huge Pages for now, as they are not swappable and may result in slowdowns + we are not using swap right now.
# The THP are by default set to madvise
# We are allocating the hugepages at the start when the memory is not fragmented yet
echo "[Setting up huge pages]"
sudo mkdir -p /mnt/hugepages
# Idempotent: don't stack another hugetlbfs mount on every re-run
mountpoint -q /mnt/hugepages || mount -t hugetlbfs none /mnt/hugepages
# Increase proactive compaction to reduce memory fragmentation for using overcomitted huge pages

available_ram=$(grep MemTotal /proc/meminfo | awk '{print $2}') # in KiB
available_ram=$(($available_ram / 1024))                        # in MiB
echo "- Total memory: $available_ram MiB"

min_normal_ram=$((4 * 1024))                             # 4 GiB
min_normal_percentage_ram=$(($available_ram * 16 / 100)) # 16% of the total memory
max_normal_ram=$((42 * 1024))                            # 42 GiB

max() {
    if (($1 > $2)); then
        echo "$1"
    else
        echo "$2"
    fi
}

min() {
    if (($1 < $2)); then
        echo "$1"
    else
        echo "$2"
    fi
}

ensure_even() {
    if (($1 % 2 == 0)); then
        echo "$1"
    else
        echo $(($1 - 1))
    fi
}

remove_decimal() {
    echo "$(echo $1 | sed 's/\..*//')"
}

reserved_normal_ram=$(max $min_normal_ram $min_normal_percentage_ram)
reserved_normal_ram=$(min $reserved_normal_ram $max_normal_ram)
echo "- Reserved RAM: $reserved_normal_ram MiB"

# The huge pages RAM should still be usable for normal pages in most cases.
hugepages_ram=$(($available_ram - $reserved_normal_ram))
hugepages_ram=$(remove_decimal $hugepages_ram)
hugepages_ram=$(ensure_even $hugepages_ram)
echo "- RAM for hugepages: $hugepages_ram MiB"

hugepage_size_in_mib=$(grep -i "Hugepagesize" /proc/meminfo | awk '{print $2}')
if [ -z "$hugepage_size_in_mib" ]; then
    echo "无法从/proc/meminfo获取大页,使用默认大小2M"
    hugepage_size_in_mib=2
else
    hugepage_size_in_mib=$((hugepage_size_in_mib/1024))
fi
echo "- Huge page size: $hugepage_size_in_mib MiB"
hugepages=$(($hugepages_ram / $hugepage_size_in_mib))

# This percentage will be permanently allocated for huge pages and in monitoring it will be shown as used.
base_hugepages_percentage=20
base_hugepages=$(($hugepages * $base_hugepages_percentage / 100))
base_hugepages=$(remove_decimal $base_hugepages)
echo "- Allocating $base_hugepages huge pages ($base_hugepages_percentage%) for base usage"
echo $base_hugepages >/proc/sys/vm/nr_hugepages

overcommitment_hugepages_percentage=$((100 - $base_hugepages_percentage))
overcommitment_hugepages=$(($hugepages * $overcommitment_hugepages_percentage / 100))
overcommitment_hugepages=$(remove_decimal $overcommitment_hugepages)
echo "- Allocating $overcommitment_hugepages huge pages ($overcommitment_hugepages_percentage%) for overcommitment"
echo $overcommitment_hugepages >/proc/sys/vm/nr_overcommit_hugepages
