#!/bin/bash
# 一键测"空闲沙箱占多少内存"：基线 → 起 N 个沙箱不做任何事 → T1、T2 各采一次 → 关闭沙箱 → 等 T3 秒再采一次（看还回去多少）。
# 需要 root；在哪个目录运行都行（脚本会切到自己所在目录）。凭据由 idle_sandbox.py 从上一级 benchmark/.env 读取。
#
#   bash measure_idle_mem.sh                 # N=1，T1=30s，T2=300s，T3=60s
#   bash measure_idle_mem.sh 10              # 10 个沙箱
#   bash measure_idle_mem.sh 10 30 300 60    # N T1 T2 T3（T1、T2 从"全部创建完成"算起；T3 从"全部关闭"算起）
#   TEMPLATE=xxx PYTHON=python3 bash measure_idle_mem.sh 1
#
# 结果在本目录的 mem_runs/<时间戳>_n<N>/report.txt（不入库）。建议 N=1 和 N=10 各跑一次，
# 用 (n10 增量 - n1 增量) / 9 得到"每多一个沙箱"的边际内存（扣掉一次性开销）。
# 结果解读见 空闲沙箱内存实测分析.md。每个沙箱的普通内存以 orch cgroup memory.current 的增量为准。

set -u
cd "$(dirname "$0")"

N=${1:-1}; T1=${2:-30}; T2=${3:-300}; T3=${4:-60}
TEMPLATE=${TEMPLATE:-base}
PYTHON=${PYTHON:-python}
[ "$T2" -ge "$T1" ] || { echo "T2 必须 >= T1"; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行（需要读 firecracker 的 /proc/<pid>/smaps）"; exit 1; }

OUT="mem_runs/$(date +%Y%m%d_%H%M%S)_n${N}"
mkdir -p "$OUT"
READY="$OUT/ready"; DONE="$OUT/done"

existing=$(pgrep -cf '/fc-versions/.*/firecracker' || true)
if [ "${existing:-0}" -gt 0 ]; then
  echo "警告：基线时已有 $existing 个 firecracker 在跑（其他沙箱或模板构建），基线会被抬高、增量仍然有效但请尽量在空机上测。"
fi

snap() {  # 采一次快照：$1 = 文件名前缀
  bash mem_snap.sh      > "$OUT/$1.txt"
  bash mem_snap.sh --kv > "$OUT/$1.kv"
}

echo "[1/5] 采基线 ..."
sync; echo 1 > /proc/sys/vm/drop_caches 2>/dev/null || true   # 降低 page cache 对 MemAvailable 口径的干扰
snap before

echo "[2/5] 创建 $N 个空闲沙箱（模板 $TEMPLATE）..."
$PYTHON idle_sandbox.py --count "$N" --hold $((T2 + 600)) --template "$TEMPLATE" \
  --ready-file "$READY" --done-file "$DONE" --guest > "$OUT/sandbox.log" 2>&1 &
PY_PID=$!
trap 'touch "$DONE"; wait $PY_PID 2>/dev/null' EXIT

for _ in $(seq 1 $((N * 30 + 60))); do
  [ -f "$READY" ] && break
  kill -0 $PY_PID 2>/dev/null || { echo "idle_sandbox.py 异常退出："; cat "$OUT/sandbox.log"; exit 1; }
  sleep 1
done
[ -f "$READY" ] || { echo "等待沙箱创建超时，见 $OUT/sandbox.log"; exit 1; }

echo "[3/5] 已创建，空闲等待：${T1}s 采一次，${T2}s 再采一次 ..."
sleep "$T1"; snap t1
sleep $((T2 - T1)); snap t2

echo "[4/5] 关闭沙箱，${T3}s 后再采一次（看内存还回去多少）..."
touch "$DONE"; wait $PY_PID 2>/dev/null; trap - EXIT
sleep "$T3"; snap t3

echo "[5/5] 生成报告 ..."
get() { awk -F= -v k="$2" '$1==k{print $2}' "$1"; }
row() {  # key 说明
  local k=$1 desc=$2 b t1 t2 t3
  b=$(get "$OUT/before.kv" "$k"); t1=$(get "$OUT/t1.kv" "$k"); t2=$(get "$OUT/t2.kv" "$k"); t3=$(get "$OUT/t3.kv" "$k")
  awk -v d="$desc" -v b="${b:-0}" -v t1="${t1:-0}" -v t2="${t2:-0}" -v t3="${t3:-0}" -v n="$N" 'BEGIN{
    printf "%9.1f %9.1f %9.1f %9.1f | %9.1f %9.1f | %9.1f | %9.1f | %s\n", b, t1, t2, t3, t1-b, t2-b, (t2-b)/n, t3-b, d }'
}

{
  echo "空闲沙箱内存测量  N=$N  模板=$TEMPLATE  T1=${T1}s  T2=${T2}s  T3=关闭后${T3}s  $(date '+%F %T')"
  echo "PAGESIZE=$(getconf PAGESIZE)  $(grep Hugepagesize /proc/meminfo)"
  echo "沙箱: $(tr '\n' ' ' < "$READY")"
  echo
  printf "%9s %9s %9s %9s | %9s %9s | %9s | %9s | %s\n" "before" "T1" "T2" "T3" "T1-before" "T2-before" "T2/N" "T3-before" "(MiB；T3=关闭后残留)"
  row fc_hugetlb_mib  "① 客户机内存(fc 大页)"
  row hp_used_mib     "   大页已用(核对≈①)"
  row hp_rsvd_mib     "   大页 Rsvd(预留未落地)"
  row fc_rss_mib      "② firecracker 进程 RSS"
  row fc_pss_mib      "   (同上 PSS)"
  row orch_rss_mib    "③ orchestrator RSS"
  row orch_anon_mib   "   其中 RssAnon(私有)"
  row orch_file_mib   "   其中 RssFile(文件页)"
  row cg_cur_mib      "   orch cgroup memory.current"
  row cg_anon_mib     "     其中 anon"
  row cg_file_mib     "     其中 file(page cache)"
  row cg_other_mib    "     其中 其他(current-anon-file)"
  row nonhp_used_mib  "④ MemTotal-MemAvailable(普通内存)"
  row anon_mib        "   AnonPages"
  row cached_mib      "   Cached(page cache)"
  row slab_rec_mib    "   Slab 可回收"
  row slab_unrec_mib  "   Slab 不可回收"
  row pagetables_mib  "   PageTables"
  row kstack_mib      "   KernelStack"
  row shmem_mib       "   Shmem"
  row snapcache_mib   "   snapshot-cache tmpfs"
  row orch_build_mib  "   orchestrator build 目录(磁盘)"
  row orch_sbx_mib    "   orchestrator sandbox 目录(磁盘)"
  echo
  awk -F= -v n="$N" '
    FNR==NR {b[$1]=$2; next} {t[$1]=$2}
    END {
      hp = t["fc_hugetlb_mib"]-b["fc_hugetlb_mib"]
      cg = t["cg_cur_mib"]-b["cg_cur_mib"]
      printf "T2 时刻每个沙箱：大页 %.1f MiB + orch cgroup 普通内存 %.1f MiB = %.1f MiB\n", hp/n, cg/n, (hp+cg)/n
      printf "（④ MemTotal-MemAvailable 是全机口径，宿主机有别的负载时噪声很大，只作参考）\n"
    }' "$OUT/before.kv" "$OUT/t2.kv"
  awk -F= '$1=="cg_max_mib"{if($2>0) printf "orch cgroup memory.max = %s MiB（客户机大页不计入；其余普通内存计入，超了会 memcg OOM）\n", $2}' "$OUT/t2.kv"
  echo "（同一 N 下的绝对值含一次性开销；边际值请用 N=1 与 N=10 两次做差，见 空闲沙箱内存实测分析.md）"
  echo
  echo "----- T2 明细 -----"; cat "$OUT/t2.txt"
  echo
  echo "----- T3（关闭后）明细 -----"; cat "$OUT/t3.txt"
  echo
  echo "----- idle_sandbox.py 输出（含 --guest 客户机视角）-----"; cat "$OUT/sandbox.log"
} | tee "$OUT/report.txt"

echo
echo "报告已保存：$OUT/report.txt"
