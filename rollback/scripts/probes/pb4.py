#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换判据之后原生快照能降到多少：把 KVM+宿主写位图归并到 memfile 的 2MB 差分块。

差分是按 2MB 块存的，所以真实收益不是 4KB 位图的置位量，而是它覆盖了多少个
2MB 块。这一列就是"改完之后每代导出多少"的预测值，与当前实际导出并排。
"""
import os, struct, sys
from e2b import Sandbox
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pb2 import fc, fc_sock, CACHE, export_size, MB

BLOCK = 2 * MB
SWEEP = "/dev/shm/sweep"


def fcdb_blocks(path, block=BLOCK):
    """返回 (4KB 置位页数, 页大小, 覆盖到的 block 数)。"""
    d = open(path, "rb").read()
    if len(d) < 24 or d[:4] != b"FCDB":
        return None, None, None
    ps = struct.unpack_from("<Q", d, 8)[0]
    npg = struct.unpack_from("<Q", d, 16)[0]
    words = struct.unpack_from("<%dQ" % ((npg + 63) // 64), d, 24)
    per = block // ps                      # 一个差分块里有多少页
    pages, blocks = 0, set()
    for wi, w in enumerate(words):
        if not w:
            continue
        for bi in range(64):
            if w >> bi & 1:
                p = wi * 64 + bi
                if p >= npg:
                    break
                pages += 1
                blocks.add(p // per)
    return pages, ps, len(blocks)


sb = Sandbox.create(template="base", timeout=1800)
sid = sb.sandbox_id
print("沙箱 %s   差分块 %d MB\n" % (sid, BLOCK // MB))
hdr = "%-28s %14s %16s %16s" % ("步骤", "写跟踪(4KB)", "归并到2MB块", "当前实际导出")
print(hdr); print("-" * len(hdr))
snaps = []


def step(label, cmd=None):
    if cmd:
        sb.commands.run(cmd, user="root", timeout=900)
    sock = fc_sock(sid)
    pages = blocks = None
    c, _ = fc(sock, "PATCH", "/vm", {"state": "Paused"})
    if c < 300:
        p = "/tmp/pb4-%s.fcdb" % sid
        c2, _ = fc(sock, "PUT", "/snapshot/save-dirty-bitmap", {"path": p})
        if c2 < 300:
            pages, ps, blocks = fcdb_blocks(p)
            os.unlink(p)
        fc(sock, "PATCH", "/vm", {"state": "Resumed"})
    before = set(os.listdir(CACHE)) if CACHE else set()
    s = sb.create_snapshot()
    snaps.append(s.snapshot_id)
    ex = export_size(before)
    print("%-28s %14s %16s %16s"
          % (label,
             "-" if pages is None else "%9.1f MB" % (pages * 4096 / MB),
             "-" if blocks is None else "%9.1f MB" % (blocks * BLOCK / MB),
             "-" if ex is None else "%9.1f MB" % (ex / MB)))


step("A 刚建好，什么都没做")
step("B 写 192MB", "dd if=/dev/urandom of=%s bs=1M count=192 2>/dev/null" % SWEEP)
step("C 什么都不做")
step("D 只读 192MB，一字节没改", "dd if=%s of=/dev/null bs=1M 2>/dev/null" % SWEEP)
step("E 什么都不做")
step("F 只改 12MB", "dd if=/dev/urandom of=%s bs=1M count=12 conv=notrunc 2>/dev/null" % SWEEP)
step("G 只改 48MB", "dd if=/dev/urandom of=%s bs=1M count=48 conv=notrunc 2>/dev/null" % SWEEP)

print("-" * len(hdr))
print("中间一列 = 换用写跟踪位图之后每代的导出量（按 2MB 差分块算），")
print("右边一列 = 现在的导出量。两列之比就是这次改动的收益。")
for s in snaps:
    try:
        Sandbox.delete_snapshot(s)
    except Exception:
        pass
sb.kill()
