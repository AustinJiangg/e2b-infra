#!/usr/bin/env python3
"""稳定性：反复回滚，统计成功率与分位数。

默认在两个检查点之间交替（a/b），这是 920B 上唯一能触发"慢阶段"现象的模式；
--same 则反复回到同一个检查点作对照。任何一次内容校验失败都会立刻停下并打印
现场——静默的内容错误比崩溃危险得多。

用法: loop.py <xfs|ext4> [次数，默认 200] [--same]
"""
import statistics
import sys
import time

import lib

SCHEME = sys.argv[1] if len(sys.argv) > 1 else "ext4"
N = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 200
SAME = "--same" in sys.argv

check = lib.Checker()
b = lib.Box()
times, failures, slow = [], 0, []

try:
    b.checkpoint("a", 32)
    b.checkpoint("b", 32)

    t_start = time.monotonic()
    for i in range(1, N + 1):
        name = "a" if (SAME or i % 2) else "b"

        t0 = time.monotonic()
        ok = b.sbx.checkpoint.restore(b.cks[name])
        dt = time.monotonic() - t0

        mem, disk = b.markers()
        good = bool(ok) and mem == name and disk == name and b.blob_sum() == b.sums[name]

        times.append(dt)
        if dt > 1.0:
            slow.append((i, round(dt, 3)))
        if not good:
            failures += 1
            print("!! 第 %d 次回滚到 %s 失败：ok=%s mem=%s disk=%s" % (i, name, ok, mem, disk), flush=True)
            break
        if i % 25 == 0:
            print("  %d/%d  p50=%.3fs  max=%.3fs  慢于1s=%d 次"
                  % (i, N, statistics.median(times), max(times), len(slow)), flush=True)
    wall = time.monotonic() - t_start

    print("\n=== 结果 (%s, %s) ===" % (SCHEME, "同一检查点" if SAME else "两个检查点交替"), flush=True)
    print("  次数       : %d" % len(times), flush=True)
    print("  failures   : %d" % failures, flush=True)
    print("  p50 / p90  : %.3fs / %.3fs"
          % (statistics.median(times), sorted(times)[max(0, int(len(times) * 0.9) - 1)]), flush=True)
    print("  min / max  : %.3fs / %.3fs" % (min(times), max(times)), flush=True)
    print("  >1s 的次数 : %d %s" % (len(slow), slow[:10]), flush=True)
    print("  总墙钟     : %.1fs" % wall, flush=True)
    check(failures == 0, "%d 次回滚全部内容正确" % len(times))

    if slow:
        print("\n注：920B 上观察到过'慢阶段'——两个检查点交替回滚时约一半沙箱会进入", flush=True)
        print("每次回滚后 guest 内核忙 0.5~2s 的状态（khugepaged / timer IRQ 风暴），", flush=True)
        print("与本方案改动无关（旧 FC + 旧 orchestrator 同样出现），同一检查点反复回滚", flush=True)
        print("不出现。用 --same 跑一轮对照即可复现这个差异。950 上若也出现，值得用 perf", flush=True)
        print("抓一次 guest 侧现场——920B 上没装 perf，这条一直没定位。", flush=True)
finally:
    b.kill()

sys.exit(check.finish("STABLE"))
