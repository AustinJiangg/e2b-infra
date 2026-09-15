#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨代 4KB 拼接正确性：三代快照改同一个 2MB 块里的不同 4KB 页，恢复每一代都要逐字节对。

    s1: /dev/shm/x = 4MB 随机内容（跨 2 个 2MB 块）
    s2: 只改块 0 的第 2 页（偏移 8KB），另把第 4 页清零
    s3: 只改块 0 的第 0 页（偏移 0）
恢复 s1/s2/s3 各起一个新沙箱，md5 必须等于建快照时记下的值。
块 0 在 s3 的视角由三代拼成：页 0 来自 s3，页 2/4 来自 s2，其余来自 s1。
"""
import hashlib, os, sys
from e2b import Sandbox
from dotenv import load_dotenv
load_dotenv()

F = "/dev/shm/x"
MB = 1048576


def run(sb, cmd):
    r = sb.commands.run(cmd, user="root", timeout=300)
    if r.exit_code != 0:
        raise SystemExit("cmd failed: %s\n%s" % (cmd, r.stderr))
    return r.stdout.strip()


def md5(sb):
    return run(sb, "md5sum %s | cut -d' ' -f1" % F)


sb = Sandbox.create(template="base", timeout=1800)
print("源沙箱", sb.sandbox_id)
snaps, want = [], []

run(sb, "dd if=/dev/urandom of=%s bs=1M count=4 2>/dev/null" % F)
want.append(md5(sb)); snaps.append(sb.create_snapshot().snapshot_id); print("s1", want[-1], snaps[-1])

run(sb, "dd if=/dev/urandom of=%s bs=4096 seek=2 count=1 conv=notrunc 2>/dev/null && "
        "dd if=/dev/zero of=%s bs=4096 seek=4 count=1 conv=notrunc 2>/dev/null" % (F, F))
want.append(md5(sb)); snaps.append(sb.create_snapshot().snapshot_id); print("s2", want[-1], snaps[-1])

run(sb, "dd if=/dev/urandom of=%s bs=4096 seek=0 count=1 conv=notrunc 2>/dev/null" % F)
want.append(md5(sb)); snaps.append(sb.create_snapshot().snapshot_id); print("s3", want[-1], snaps[-1])
assert len(set(want)) == 3, "三代 md5 应各不相同"
sb.kill()

ok = True
for i, (s, w) in enumerate(zip(snaps, want), 1):
    nb = Sandbox.create(template=s, timeout=600)
    got = md5(nb)
    zero = run(nb, "dd if=%s bs=4096 skip=4 count=1 2>/dev/null | tr -d '\\0' | wc -c" % F) if i >= 2 else "-"
    mark = "✓" if got == w and zero in ("-", "0") else "✗"
    ok &= mark == "✓"
    print("restore s%d: md5 %s (期望 %s)  第4页非零字节=%s  %s" % (i, got, w, zero, mark))
    nb.kill()

for s in snaps:
    try:
        Sandbox.delete_snapshot(s)
    except Exception:
        pass
print("ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
