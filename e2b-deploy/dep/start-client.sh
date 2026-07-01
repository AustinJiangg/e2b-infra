#!/usr/bin/env bash

# This script is meant to be run in the User Data of each EC2 Instance while it's booting. The script uses the
# run-nomad and run-consul scripts to configure and start Nomad and Consul in client mode. Note that this script
# assumes it's running in an AMI built from the Packer template in examples/nomad-consul-ami/nomad-consul.json.

set -a
[[ -f "$(dirname "$0")/.env" ]] && source "$(dirname "$0")/.env"
set +a

set -euo pipefail

NODE_POOL_NAME="${1:-default}"
INSTANCE_IP_ADDRESS="$2"

cp -f "$(dirname "$0")/bin/orchestrator"      /usr/bin/orchestrator
cp -f "$(dirname "$0")/bin/orchestrator"    /usr/bin/template-manager
chmod +x /usr/bin/orchestrator /usr/bin/template-manager

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
    echo "$SWAPFILE none swap sw 0 0" | $SUDO tee -a /etc/fstab >/dev/null
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

# Increase the maximum number of socket connections
sysctl -w net.core.somaxconn=65535

# Increase the maximum number of backlogged connections
sysctl -w net.core.netdev_max_backlog=65535

# Increase maximum number of TCP sockets
sysctl -w net.ipv4.tcp_max_syn_backlog=65535

# Increase the maximum number of memory map areas
sysctl -w vm.max_map_count=1048576


echo "Disabling inotify for NBD devices"
# https://lore.kernel.org/lkml/20220422054224.19527-1-matthew.ruffell@canonical.com/
cat <<EOH >/etc/udev/rules.d/97-nbd-device.rules
# Disable inotify watching of change events for NBD devices
ACTION=="add|change", KERNEL=="nbd*", OPTIONS:="nowatch"
EOH

sudo udevadm control --reload-rules
sudo udevadm trigger

# 加载自定义优化后的 nbd 模块（512 个设备），替换内核自带的 nbd
# Load our custom-patched (optimized) nbd module with 512 devices instead of the in-tree one.
# 模块路径可通过环境变量 NBD_KO 覆盖
NBD_KO="${NBD_KO:-/home/j30059180/tools/nbd-patch/nbd.ko}"
if [ ! -f "$NBD_KO" ]; then
    echo "错误：找不到自定义 nbd 模块：$NBD_KO" >&2
    exit 1
fi
# 先卸载当前已加载的 nbd（内核自带或上一次加载的），未加载时忽略报错
sudo rmmod nbd 2>/dev/null || true
sudo insmod "$NBD_KO" nbds_max=512

# Create the directory for the fc mounts
mkdir -p /fc-vm

# Create the config file for gcsfuse
fuse_cache="/fuse/cache"
mkdir -p $fuse_cache

fuse_config="/fuse/config.yaml"

cat >$fuse_config <<EOF
file-cache:
  max-size-mb: -1
  cache-file-for-range-read: false

metadata-cache:
  ttl-secs: -1

cache-dir: $fuse_cache
EOF

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

#./install-consul.sh --version ${CONSUL_VERSION}
#./install-nomad.sh --version ${NOMAD_VERSION}

# Idempotent: drop any existing 'nameserver 127.0.0.1' lines first, then add exactly one
# at the top. Re-running this script used to stack a new duplicate line on every run.
sed -i '/^nameserver 127\.0\.0\.1$/d' /etc/resolv.conf
sed -i '1i nameserver 127.0.0.1' /etc/resolv.conf
mkdir -p /etc/dnsmasq.d
# Idempotent: only append the consul forwarder once
grep -qxF 'server=/consul/127.0.0.1#8600' /etc/dnsmasq.d/consul.conf 2>/dev/null \
  || echo 'server=/consul/127.0.0.1#8600' >> /etc/dnsmasq.d/consul.conf
systemctl restart dnsmasq

# These variables are passed in via Terraform template interpolation
#./run-consul.sh --client --server-ips "${SERVER_IPS}" --dns-request-token "${CONSUL_ACL_TOKEN}" --instance-ip-address "${INSTANCE_IP_ADDRESS}"

#./run-nomad.sh --client --consul-token "${CONSUL_ACL_TOKEN}" ${NODE_POOL_NAME:+--node-pool-name "$NODE_POOL_NAME" --instance-ip-address "${INSTANCE_IP_ADDRESS}"}

# Add alias for ssh-ing to sbx
#echo '_sbx_ssh() {
#  local address=$(dig @127.0.0.4 $1. A +short 2>/dev/null)
#  ssh -o StrictHostKeyChecking=accept-new "root@$address"
#}

#alias sbx-ssh=_sbx_ssh' >>/etc/profile
