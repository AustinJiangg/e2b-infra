#!/bin/bash
# 宿主机内存快照：把"沙箱占的内存"按来源拆开打印。需要 root（读 firecracker 的 smaps）。
#
#   bash mem_snap.sh          # 人读格式（含每个 firecracker 进程一行）
#   bash mem_snap.sh --kv     # key=value 格式，给 measure_idle_mem.sh 做差用
#
# 各项含义（详见 沙箱内存占用测量.md）：
#   hp_used_mib     大页已用量（所有大页尺寸求和，含 surplus/overcommit 部分）= 全部客户机内存
#   hp_rsvd_mib     大页 Rsvd：已被 mmap 预留、还没缺页落地的大页（free 里扣不出去给别人）。
#                   若 firecracker 映射客户机内存时没带 MAP_NORESERVE，每个沙箱会在这里先预留满 memory_mb
#   fc_hugetlb_mib  所有 firecracker 进程映射里已落地的大页 = 客户机实际被碰过的内存
#   fc_rss_mib      所有 firecracker 进程的普通页 RSS（不含大页）= VMM 自身开销
#   fc_pss_mib      同上的 PSS（共享的二进制/库按进程数均摊）
#   orch_rss_mib    orchestrator（template-manager 合体进程）RSS
#   orch_anon/file/shmem_mib  orchestrator RSS 拆分（/proc/<pid>/status 的 RssAnon/RssFile/RssShmem）：
#                   anon 是 Go 堆等私有内存；file 是 mmap 的文件页（属于 page cache，可被多方共享）
#   snapcache_mib   /mnt/snapshot-cache（tmpfs，SNAPSHOT_CACHE_DIR）用量：暂停/快照用的缓存。
#                   注意它**不是**模板缓存；模板 memfile/rootfs 的本地缓存在 ${ORCH_BASE}/build（磁盘，走 page cache），
#                   见 deploy-docs/10 §1
#   orch_build_mib  ${ORCH_BASE}/build 磁盘占用（模板 diff 缓存，同模板共享）
#   orch_sbx_mib    ${ORCH_BASE}/sandbox 磁盘占用（运行中沙箱的每沙箱文件）
#   cg_*_mib        orchestrator 所在 cgroup（Nomad task cgroup，firecracker 也在里面）的 memory.current /
#                   memory.max 及 memory.stat 的 anon/file/kernel 拆分。客户机大页不记在这里，
#                   但 orchestrator 堆、fc 进程、page cache、内核对象都记，超过 memory.max 会 memcg OOM（deploy-docs/12）
#   anon/cached/slab_*/pagetables/kstack_mib  /proc/meminfo 对应项，用来给 MemTotal-MemAvailable 的变化找归属
#   shmem_mib       /proc/meminfo Shmem（tmpfs 等），snapshot-cache 包含在内
#   nonhp_used_mib  MemTotal - MemAvailable：普通内存口径的"已用"（静态大页池开机就扣掉了，
#                   所以只有 overcommit 出来的大页会体现在这里）

set -u
ORCH_BASE=${ORCHESTRATOR_BASE_PATH:-/orchestrator}
KV=0
[ "${1:-}" = "--kv" ] && KV=1

mi() { awk -v k="$1:" '$1==k{print $2}' /proc/meminfo; }   # kB

# 大页：/sys 下每种尺寸单独记账；nr_hugepages 只含常驻池，surplus 是 overcommit 借出来的
hp_used_kb=0
hp_rsvd_kb=0
hp_detail=""
for d in /sys/kernel/mm/hugepages/hugepages-*kB; do
  [ -d "$d" ] || continue
  sz=${d##*hugepages-}; sz=${sz%kB}
  nr=$(cat "$d/nr_hugepages"); fr=$(cat "$d/free_hugepages"); su=$(cat "$d/surplus_hugepages")
  rs=$(cat "$d/resv_hugepages")
  used=$(( nr + su - fr ))
  hp_used_kb=$(( hp_used_kb + used * sz ))
  hp_rsvd_kb=$(( hp_rsvd_kb + rs * sz ))
  hp_detail="$hp_detail ${sz}kB:pool=$nr,surplus=$su,free=$fr,used=$used,rsvd=$rs"
done

# firecracker：smaps 里的 Rss/Pss 不含 hugetlb，大页单独记在 Private_Hugetlb/Shared_Hugetlb
fc_pids=$(pgrep -f '/fc-versions/.*/firecracker' || true)
fc_count=0; fc_rss_kb=0; fc_pss_kb=0; fc_huge_kb=0; fc_lines=""
for p in $fc_pids; do
  [ -r "/proc/$p/smaps" ] || continue
  read -r r s h < <(awk '/^Rss:/{r+=$2} /^Pss:/{s+=$2} /_Hugetlb:/{h+=$2} END{print r+0, s+0, h+0}' "/proc/$p/smaps" 2>/dev/null)
  [ -n "${r:-}" ] || continue
  fc_count=$((fc_count + 1))
  fc_rss_kb=$((fc_rss_kb + r)); fc_pss_kb=$((fc_pss_kb + s)); fc_huge_kb=$((fc_huge_kb + h))
  fc_lines="$fc_lines$(printf '  fc pid %-8s RSS=%7.1f MiB  PSS=%7.1f MiB  客户机大页=%8.1f MiB' "$p" \
    "$(awk -v x="$r" 'BEGIN{print x/1024}')" "$(awk -v x="$s" 'BEGIN{print x/1024}')" \
    "$(awk -v x="$h" 'BEGIN{print x/1024}')")"$'\n'
done

orch_pid=$(pgrep -f /usr/bin/template-manager | head -1)
orch_rss_kb=0; orch_anon_kb=0; orch_file_kb=0; orch_shmem_kb=0
if [ -n "$orch_pid" ] && [ -r "/proc/$orch_pid/status" ]; then
  read -r orch_rss_kb orch_anon_kb orch_file_kb orch_shmem_kb < <(awk '
    /^VmRSS:/{r=$2} /^RssAnon:/{a=$2} /^RssFile:/{f=$2} /^RssShmem:/{s=$2}
    END{print r+0, a+0, f+0, s+0}' "/proc/$orch_pid/status")
fi

# orchestrator 所在 cgroup（cgroup v2）
cg_cur_kb=0; cg_max_kb=0; cg_anon_kb=0; cg_file_kb=0; cg_kern_kb=0; cg_path=""
if [ -n "$orch_pid" ] && [ -r "/proc/$orch_pid/cgroup" ]; then
  cg_path="/sys/fs/cgroup$(awk -F: '$1=="0"{print $3}' "/proc/$orch_pid/cgroup")"
  if [ -r "$cg_path/memory.current" ]; then
    cg_cur_kb=$(( $(cat "$cg_path/memory.current") / 1024 ))
    mx=$(cat "$cg_path/memory.max"); [ "$mx" = "max" ] && cg_max_kb=0 || cg_max_kb=$(( mx / 1024 ))
    read -r cg_anon_kb cg_file_kb cg_kern_kb < <(awk '
      $1=="anon"{a=$2} $1=="file"{f=$2} $1=="kernel"{k=$2; hk=1}
      $1=="slab"||$1=="kernel_stack"||$1=="pagetables"||$1=="sock"||$1=="percpu"{ks+=$2}
      END{if(!hk)k=ks; printf "%d %d %d\n", a/1024, f/1024, k/1024}' "$cg_path/memory.stat")
  fi
fi

snapcache_kb=$(du -sk /mnt/snapshot-cache 2>/dev/null | cut -f1)
snapcache_kb=${snapcache_kb:-0}
dsk() { local v; v=$(du -sk "$1" 2>/dev/null | cut -f1); echo "${v:-0}"; }
orch_build_kb=$(dsk "$ORCH_BASE/build")
orch_sbx_kb=$(dsk "$ORCH_BASE/sandbox")

m() { awk -v x="$1" 'BEGIN{printf "%.1f", x/1024}'; }

if [ $KV -eq 1 ]; then
  echo "fc_count=$fc_count"
  echo "hp_used_mib=$(m $hp_used_kb)"
  echo "hp_rsvd_mib=$(m $hp_rsvd_kb)"
  echo "fc_hugetlb_mib=$(m $fc_huge_kb)"
  echo "fc_rss_mib=$(m $fc_rss_kb)"
  echo "fc_pss_mib=$(m $fc_pss_kb)"
  echo "orch_rss_mib=$(m $orch_rss_kb)"
  echo "orch_anon_mib=$(m $orch_anon_kb)"
  echo "orch_file_mib=$(m $orch_file_kb)"
  echo "orch_shmem_mib=$(m $orch_shmem_kb)"
  echo "snapcache_mib=$(m $snapcache_kb)"
  echo "cg_cur_mib=$(m $cg_cur_kb)"
  echo "cg_max_mib=$(m $cg_max_kb)"
  echo "cg_anon_mib=$(m $cg_anon_kb)"
  echo "cg_file_mib=$(m $cg_file_kb)"
  echo "cg_kernel_mib=$(m $cg_kern_kb)"
  echo "orch_build_mib=$(m $orch_build_kb)"
  echo "orch_sbx_mib=$(m $orch_sbx_kb)"
  echo "shmem_mib=$(m "$(mi Shmem)")"
  echo "anon_mib=$(m "$(mi AnonPages)")"
  echo "cached_mib=$(m "$(mi Cached)")"
  echo "slab_rec_mib=$(m "$(mi SReclaimable)")"
  echo "slab_unrec_mib=$(m "$(mi SUnreclaim)")"
  echo "pagetables_mib=$(m "$(mi PageTables)")"
  echo "kstack_mib=$(m "$(mi KernelStack)")"
  echo "nonhp_used_mib=$(m $(( $(mi MemTotal) - $(mi MemAvailable) )))"
else
  echo "== $(date '+%F %T')  PAGESIZE=$(getconf PAGESIZE)  默认Hugepagesize=$(mi Hugepagesize)kB  firecracker 进程数=$fc_count"
  echo "大页明细                    :$hp_detail"
  echo "大页已用 (MiB)              : $(m $hp_used_kb)"
  echo "大页已预留未落地 Rsvd (MiB) : $(m $hp_rsvd_kb)"
  echo "firecracker 客户机大页合计  : $(m $fc_huge_kb)"
  echo "firecracker RSS/PSS 合计    : $(m $fc_rss_kb) / $(m $fc_pss_kb)"
  echo "orchestrator RSS (MiB)      : $(m $orch_rss_kb)   (pid ${orch_pid:-未找到}; anon $(m $orch_anon_kb) / file $(m $orch_file_kb) / shmem $(m $orch_shmem_kb))"
  echo "orch cgroup 用量/上限 (MiB) : $(m $cg_cur_kb) / $(m $cg_max_kb)   (anon $(m $cg_anon_kb) / file $(m $cg_file_kb) / kernel $(m $cg_kern_kb)；${cg_path:-未找到})"
  echo "snapshot-cache tmpfs (MiB)  : $(m $snapcache_kb)"
  echo "build / sandbox 目录 (MiB)  : $(m $orch_build_kb) / $(m $orch_sbx_kb)   （$ORCH_BASE 下，磁盘）"
  echo "AnonPages / Cached (MiB)    : $(m "$(mi AnonPages)") / $(m "$(mi Cached)")"
  echo "Slab 可回收/不可回收 (MiB)  : $(m "$(mi SReclaimable)") / $(m "$(mi SUnreclaim)")"
  echo "PageTables / KernelStack    : $(m "$(mi PageTables)") / $(m "$(mi KernelStack)") MiB"
  echo "Shmem (MiB)                 : $(m "$(mi Shmem)")"
  echo "MemTotal-MemAvailable (MiB) : $(m $(( $(mi MemTotal) - $(mi MemAvailable) )))"
  printf '%s' "$fc_lines"
fi
