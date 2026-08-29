#!/usr/bin/env python3
"""在 guest 里跑：高频采样单调时钟，找出最大的几个间隔。

虚机被 checkpoint 暂停时，guest 里的一切都停住，包括这个采样器 —— 于是在
它的时间序列里留下一个洞。洞的宽度就是**虚机真正被冻结的时长**，也就是
业务负载实际感受到的停顿。这个量和客户端墙钟不是一回事：墙钟还包含
RPC、代理、以及暂停之外的宿主侧工作。

自己只写一个预分配好的数组（几十 KB），不产生额外脏页，也不刷盘。
"""
import array
import sys
import time

dur = float(sys.argv[1])
top = int(sys.argv[2]) if len(sys.argv) > 2 else 20
out = sys.argv[3] if len(sys.argv) > 3 else "/tmp/freeze.out"

# ~2.5kHz。再高就变成忙等，会跟被测负载抢 CPU；再低就分辨不出几毫秒的抖动。
n = int(dur * 2600) + 2000
buf = array.array("q", bytes(8 * n))

i = 0
t_end = time.monotonic() + dur
while i < n:
    buf[i] = time.monotonic_ns()
    i += 1
    if time.monotonic() > t_end:
        break
    time.sleep(0.0004)

gaps = sorted(buf[k + 1] - buf[k] for k in range(i - 1))
mid = gaps[len(gaps) // 2]
with open(out, "w") as f:
    f.write("SAMPLES %d\n" % i)
    f.write("SPAN_S %.3f\n" % ((buf[i - 1] - buf[0]) / 1e9))
    f.write("MEDIAN_MS %.4f\n" % (mid / 1e6))
    f.write("TOP_MS %s\n" % " ".join("%.3f" % (g / 1e6) for g in gaps[-top:][::-1]))
    f.write("DONE\n")
print("DONE")
