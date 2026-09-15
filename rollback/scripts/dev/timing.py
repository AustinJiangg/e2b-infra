#!/usr/bin/env python3
"""耗时与存储，两套通用。

A. 逐代 create：脏页量 0/64/256/512MB 各一代，看 create 是否 O(脏页)、
   df 增量是否只涨脏页那么多（XFS 套上这条直接检验 reflink 有没有生效）。
B. 链深对照：5/20/50 代各一条链，每代写脏**不同的** 4MB，把"回滚集大小"
   和"链深"解耦——
     深集 = tip↔root 往返，路径 N 个纪元；
     浅集 = tip↔tip-1 往返 ×5，路径恒为 1 个纪元，只有目标深度在变。
   浅集才是链深本身的成本。

用法: timing.py <xfs|ext4> [df挂载点] [深度列表，默认 5,20,50]
"""
import json
import statistics
import sys

import lib

SCHEME = sys.argv[1] if len(sys.argv) > 1 else "ext4"
MOUNT = sys.argv[2] if len(sys.argv) > 2 else "/"
DEPTHS = [int(x) for x in (sys.argv[3].split(",") if len(sys.argv) > 3 else ["5", "20", "50"])]
DIRTY_MB = 4

check = lib.Checker()
result = {"scheme": SCHEME, "store_fs": lib.store_fstype(), "full_root": lib.FULL_ROOT}

print("=== A. 逐代 create（脏页量扫描）===", flush=True)
b = lib.Box()
rows = []
try:
    for name, mb in [("g1", 0), ("g2", 64), ("g3", 256), ("g4", 512), ("g5", 0)]:
        dt, delta, mode = b.checkpoint(name, mb, MOUNT)
        rows.append({"name": name, "dirty_mb": mb, "time": round(dt, 3), "df_mb": delta, "mode": mode})
    print("\n逐级回退：", flush=True)
    for name in ["g5", "g4", "g3", "g2", "g1"]:
        dt, good = b.restore(name)
        check(good, "restore %s 内容正确" % name)
        rows.append({"name": name, "restore": round(dt, 3)})
finally:
    b.kill()
result["sweep"] = rows

print("\n=== B. 链深对照 ===", flush=True)
depth_results = {}
for depth in DEPTHS:
    print("\n--- 深度 %d ---" % depth, flush=True)
    b = lib.Box()
    try:
        creates = []
        u0 = lib.df_used_mb(MOUNT)
        for i in range(1, depth + 1):
            name = "g%d" % i
            # A different 4MB region per generation: the worst case for
            # content resolution, since no later generation covers it.
            b.sbx.commands.run(
                "dd if=/dev/urandom of=/dev/shm/blob bs=1M count=%d seek=%d conv=notrunc 2>/dev/null"
                % (DIRTY_MB, (i - 1) * DIRTY_MB), timeout=120)
            b.stamp(name)
            import time as _t
            t0 = _t.monotonic()
            ck = b.sbx.checkpoint.create(name=name)
            creates.append(_t.monotonic() - t0)
            b.cks[name] = ck.checkpoint_id
            if i % 10 == 0 or i == depth:
                print("    建到 %d/%d（create p50 %.3fs）" % (i, depth, statistics.median(creates)), flush=True)
        df_delta = lib.df_used_mb(MOUNT) - u0

        tip, root, prev = "g%d" % depth, "g1", "g%d" % (depth - 1)
        deep_down, ok1 = b.restore(root, "[深集 %d 个纪元]" % depth)
        check(ok1, "深度 %d：回退到链根内容正确" % depth)
        deep_up, ok2 = b.restore(tip, "[深集 %d 个纪元，前滚]" % depth)
        check(ok2, "深度 %d：前滚到链尾内容正确" % depth)

        shallow = []
        for _ in range(5):
            dt, ok = b.restore(prev, "[浅集 1 个纪元]")
            check(ok, "深度 %d：浅集回退内容正确" % depth)
            shallow.append(dt)
            dt, ok = b.restore(tip, "[浅集 1 个纪元]")
            check(ok, "深度 %d：浅集前滚内容正确" % depth)
            shallow.append(dt)

        depth_results[depth] = {
            "create_p50": round(statistics.median(creates), 3),
            "create_max": round(max(creates), 3),
            "df_delta_mb": df_delta,
            "deep_down": round(deep_down, 3),
            "deep_up": round(deep_up, 3),
            "shallow_p50": round(statistics.median(shallow), 3),
            "shallow_max": round(max(shallow), 3),
        }
        print("  => %s" % json.dumps(depth_results[depth]), flush=True)
    finally:
        b.kill()

result["depths"] = depth_results

print("\n=== 汇总 (%s, store on %s, full_root=%s) ===" % (SCHEME, result["store_fs"], lib.FULL_ROOT), flush=True)
print("%6s %11s %9s %8s %8s %12s" % ("深度", "create p50", "df ΔMB", "深集↓", "深集↑", "浅集 p50"), flush=True)
for d in DEPTHS:
    r = depth_results[d]
    print("%6d %11.3f %9d %8.3f %8.3f %12.3f"
          % (d, r["create_p50"], r["df_delta_mb"], r["deep_down"], r["deep_up"], r["shallow_p50"]), flush=True)
print("JSON " + json.dumps(result), flush=True)

sys.exit(check.finish("ALL CORRECT"))
