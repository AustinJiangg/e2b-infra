#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 native_snapshot_bench.py 里那个恒定 400MB 的 memfile 拆开量。

每一步只做一件事，然后存一次快照，看导出的 memfile 涨了多少。关键是 D/E 这一对：
中间只隔一次"只读"，如果 E 比 D 大出整整一个负载，就直接证明读也被算成脏。
"""
import os, sys, time, glob, tempfile
from e2b import Sandbox
from dotenv import load_dotenv
load_dotenv()

def orch_env():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            base = os.path.basename(os.path.realpath("/proc/%s/exe" % pid))
            raw = open("/proc/%s/environ" % pid, "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        if base.startswith("orchestrator") or "ORCHESTRATOR_SERVICES=" in raw:
            return dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
    return {}

CACHE = os.path.join(orch_env().get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build")

def alloc(p):
    try:
        return os.stat(p).st_blocks * 512
    except OSError:
        return None

def ls(p):
    try:
        return set(os.listdir(p))
    except OSError:
        return set()

def newest(before):
    g = {}
    for n in ls(CACHE) - before:
        for k in ("-memfile-", "-rootfs.ext4-"):
            if k in n:
                g.setdefault(n.split(k)[0], {})[k.strip("-")] = os.path.join(CACHE, n)
    c = [(b, d) for b, d in g.items() if "memfile" in d]
    if not c:
        return None, None, None
    c.sort(key=lambda bd: max(os.stat(p).st_mtime for p in bd[1].values()), reverse=True)
    b, d = c[0]
    return b, alloc(d["memfile"]), (alloc(d["rootfs.ext4"]) if "rootfs.ext4" in d else 0)

def mb(n):
    return "-" if n is None else "%7.1f" % (n / 1048576.0)

sb = Sandbox.create(template="base", timeout=1800)
print("sandbox", sb.sandbox_id, " 导出缓存", CACHE)
snaps = []
prev = [None]

def run(cmd):
    return sb.commands.run(cmd, user="root", timeout=900).stdout

def snap(label):
    before = ls(CACHE)
    t0 = time.time()
    s = sb.create_snapshot()
    e2e = (time.time() - t0) * 1000
    bid, m, d = newest(before)
    delta = "" if prev[0] is None or m is None else "   较上一次 %+8.1f MB" % ((m - prev[0]) / 1048576.0)
    print("%-46s memfile=%sMB  rootfs=%sMB  e2e=%6.1fms%s" % (label, mb(m), mb(d), e2e, delta))
    prev[0] = m
    snaps.append(s.snapshot_id)
    return m

try:
    print()
    snap("A 刚建好，什么都没做")
    snap("B 紧接着再存一次（中间只隔一次 resume）")
    run("dd if=/dev/urandom of=/dev/shm/sweep bs=1M count=192 2>/dev/null")
    snap("C 往 /dev/shm 写了 192MB")
    snap("D 什么都不做，再存一次")
    run("dd if=/dev/shm/sweep of=/dev/null bs=1M 2>/dev/null")
    snap("E 只**读**了那 192MB（一个字节没改）")
    snap("F 什么都不做，再存一次")
    run("mkdir -p /bench-root; dd if=/dev/urandom of=/bench-root/blob bs=1M count=64 2>/dev/null; sync")
    snap("G 往根文件系统写了 64MB")
    snap("H 什么都不做，再存一次")
    run("md5sum /bench-root/blob >/dev/null")
    snap("I 用 md5sum 读那 64MB（走 guest 页缓存）")
    run("dd if=/bench-root/blob of=/dev/null bs=1M iflag=direct 2>/dev/null")
    snap("J 用 O_DIRECT 读那 64MB（不进页缓存）")
    print()
    print("guest 侧内存实况：")
    print(run("free -m; echo; df -h /dev/shm | tail -1"))
finally:
    try:
        sb.kill()
    except Exception:
        pass
    for s in snaps:
        try:
            Sandbox.delete_snapshot(s)
        except Exception:
            pass
    print("清理了 %d 个快照模板" % len(snaps))
