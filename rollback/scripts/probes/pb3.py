#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最后一个未知数：原生 pause 里先做的那次 Full 快照，会不会把脏位图擦掉。

原生 pause 的顺序是 CreateSnapshot(Full, 只存 snapfile) 然后才取脏页元数据。
FC 的 Full 分支里有 reset_dirty_bitmap + reset_dirty，但只在 mem_file_path
给了的时候才走到。原生这次没给 memfile 路径，所以**预期不会重置**。这里实测。
"""
import glob, json, os, socket, struct, sys, tempfile
from e2b import Sandbox
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pb2 import fc, fc_sock, fcdb, MB   # 复用

sb = Sandbox.create(template="base", timeout=900)
sid = sb.sandbox_id
print("沙箱 %s" % sid)
sb.commands.run("dd if=/dev/urandom of=/dev/shm/sweep bs=1M count=192 2>/dev/null",
                user="root", timeout=600)
sock = fc_sock(sid)
c, _ = fc(sock, "PATCH", "/vm", {"state": "Paused"})
print("暂停 HTTP %s" % c)

def bm(tag):
    p = "/tmp/pb3-%s.fcdb" % sid
    c, r = fc(sock, "PUT", "/snapshot/save-dirty-bitmap", {"path": p})
    if c >= 300:
        print("  %s: 取位图失败 HTTP %s %s" % (tag, c, str(r)[:120]))
        return None
    n, ps = fcdb(p)
    os.unlink(p)
    print("  %-28s 置位 %6d 页 × %dKB = %8.1f MB" % (tag, n, ps // 1024, n * ps / MB))
    return n

a = bm("Full 快照之前")
snapf = "/tmp/pb3-%s.snap" % sid
c, r = fc(sock, "PUT", "/snapshot/create",
          {"snapshot_type": "Full", "snapshot_path": snapf})
print("  PUT /snapshot/create (Full, 只给 snapshot_path) → HTTP %s %s" % (c, str(r)[:140] if c >= 300 else ""))
b = bm("Full 快照之后")
fc(sock, "PATCH", "/vm", {"state": "Resumed"})
for f in (snapf,):
    try:
        os.unlink(f)
    except OSError:
        pass
sb.kill()

print()
if a and b:
    if b >= a * 0.9:
        print("结论：位图**没有被擦掉**（%d → %d）。原生 pause 的现有顺序可以直接沿用，" % (a, b))
        print("      在取脏页元数据那一步换成这份位图即可，不需要调整调用顺序。")
    else:
        print("结论：位图**被擦掉了**（%d → %d）。若要换判据，必须把取位图挪到" % (a, b))
        print("      CreateSnapshot 之前，或让那次 Full 快照不重置。")
