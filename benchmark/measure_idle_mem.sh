#!/bin/bash
# 一键测"空闲沙箱占多少内存"：基线 → 起 N 个沙箱不做任何事 → T1、T2 两个时刻各采一次 → 做差/均摊。
# 需要 root；在 benchmark 目录下运行（idle_sandbox.py 要读这里的 .env）。
#
#   bash measure_idle_mem.sh                 # N=1，T1=30s，T2=300s
#   bash measure_idle_mem.sh 10              # 10 个沙箱
#   bash measure_idle_mem.sh 10 30 300       # N T1 T2（T1、T2 都是从"全部创建完成"开始算的秒数）
#   TEMPLATE=xxx PYTHON=python3 bash measure_idle_mem.sh 1
#
# 结果在 mem_runs/<时间戳>_n<N>/report.txt。建议 N=1 和 N=10 各跑一次，
# 用 (n10 增量 - n1 增量) / 9 得到"每多一个沙箱"的边际内存（扣掉模板缓存等一次性开销）。

set -u
cd "$(dirname "$0")"

N=${1:-1}; T1=${2:-30}; T2=${3:-300}
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

echo "[1/4] 采基线 ..."
sync; echo 1 > /proc/sys/vm/drop_caches 2>/dev/null || true   # 降低 page cache 对 MemAvailable 口径的干扰
bash mem_snap.sh      > "$OUT/before.txt"
bash mem_snap.sh --kv > "$OUT/before.kv"

echo "[2/4] 创建 $N 个空闲沙箱（模板 $TEMPLATE）..."
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

echo "[3/4] 已创建，空闲等待：${T1}s 采一次，${T2}s 再采一次 ..."
sleep "$T1"
bash mem_snap.sh      > "$OUT/t1.txt"
bash mem_snap.sh --kv > "$OUT/t1.kv"
sleep $((T2 - T1))
bash mem_snap.sh      > "$OUT/t2.txt"
bash mem_snap.sh --kv > "$OUT/t2.kv"

echo "[4/4] 关闭沙箱，生成报告 ..."
touch "$DONE"; wait $PY_PID 2>/dev/null; trap - EXIT

get() { awk -F= -v k="$2" '$1==k{print $2}' "$1"; }
row() {  # key 说明 是否按沙箱数均摊
  local k=$1 desc=$2 b t1 t2
  b=$(get "$OUT/before.kv" "$k"); t1=$(get "$OUT/t1.kv" "$k"); t2=$(get "$OUT/t2.kv" "$k")
  awk -v d="$desc" -v b="$b" -v t1="$t1" -v t2="$t2" -v n="$N" 'BEGIN{
    printf "%9.1f %9.1f %9.1f | %9.1f %9.1f | %9.1f %9.1f | %s\n", b, t1, t2, t1-b, t2-b, (t1-b)/n, (t2-b)/n, d }'
}

{
  echo "空闲沙箱内存测量  N=$N  模板=$TEMPLATE  T1=${T1}s  T2=${T2}s  $(date '+%F %T')"
  echo "PAGESIZE=$(getconf PAGESIZE)  $(grep Hugepagesize /proc/meminfo)"
  echo "沙箱: $(tr '\n' ' ' < "$READY")"
  echo
  printf "%9s %9s %9s | %9s %9s | %9s %9s | %s\n" "before" "T1" "T2" "T1-before" "T2-before" "T1/N" "T2/N" "(MiB)"
  row fc_hugetlb_mib  "① 客户机内存(fc 大页)"
  row fc_rss_mib      "② firecracker 进程 RSS"
  row fc_pss_mib      "   (同上 PSS)"
  row orch_rss_mib    "③ orchestrator RSS"
  row snapcache_mib   "④ snapshot-cache(共享)"
  row hp_used_mib     "   大页已用(核对≈①)"
  row hp_rsvd_mib     "   大页 Rsvd(预留未落地)"
  row nonhp_used_mib  "   MemTotal-MemAvailable"
  echo
  awk -F= -v n="$N" '
    FNR==NR {b[$1]=$2; next} {t[$1]=$2}
    END {
      per = (t["fc_hugetlb_mib"]-b["fc_hugetlb_mib"]) + (t["fc_pss_mib"]-b["fc_pss_mib"]) + (t["orch_rss_mib"]-b["orch_rss_mib"])
      printf "T2 时刻每个沙箱独占 ≈ (①+②PSS+③)/N = %.1f MiB\n", per/n
      printf "另有共享的 snapshot-cache 增量 %.1f MiB（同模板所有沙箱共用一份）\n", t["snapcache_mib"]-b["snapcache_mib"]
    }' "$OUT/before.kv" "$OUT/t2.kv"
  echo
  echo "----- T2 明细 -----"; cat "$OUT/t2.txt"
  echo
  echo "----- idle_sandbox.py 输出（含 --guest 客户机视角）-----"; cat "$OUT/sandbox.log"
} | tee "$OUT/report.txt"

echo
echo "报告已保存：$OUT/report.txt"
