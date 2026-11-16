#!/usr/bin/env python3
"""层数组回收的卸载路径（ovl_put_super）与各种释放顺序，主要给 KASAN 内核看。

    python3 probe_umount.py <模板> [轮数=100]

guest 的根就是 overlay，卸不掉，所以回收逻辑的卸载分支在压测里走不到。这里在 guest 的 ext4（/overlay/t）上另挂一个
小 overlay，直接对它发 OVL_IOCTL_CHECKPOINT：
    ckpt（u0 沉下去）→ 打开 a.txt → ckpt（u1 沉下去）→ 打开 c.txt、起一个 cwd 在新目录里的进程
    → restore 回第一代（u0:l0）→ 检查内容与持有者视图 → 删掉被丢弃的层目录 u1/u2
然后按变体收尾，每个变体对应一种"旧层数组/共享 upper mount 的释放顺序"：
    0  放手 → 再 restore 一次 → umount            （旧数组在后续 ioctl 里释放，卸载时只剩当前数组）
    1  放手 → 直接 umount                          （旧数组还挂在 old_layer_sets 上，由 put_super 释放）
    2  放手 → 连做两次 ckpt → umount               （共享 upper mount 的两个数组先后释放）
    3  不放手再 restore 两次 → 放手 → umount       （多代旧数组同时被陈旧对象钉着）
    4  ckpt 后立刻 umount（没有任何持有者）         （前驱数组还在延迟链表上、共享 mount 的后继是当前数组）
每轮记录 mnt_cache，结束后查 dmesg。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env  # noqa: E402

os.environ.update(load_e2b_env())
from e2b import Sandbox, CommandExitException  # noqa: E402

TPL = sys.argv[1] if len(sys.argv) > 1 else "base-gsd"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 100

GUEST = r'''
import ctypes, fcntl, json, os, shutil, struct, subprocess, sys, time
IOC = 0x40104F01          # _IOW('O', 1, struct ovl_checkpoint_args {char *options; __u32 options_len;})
B = "/overlay/t"
N = int(sys.argv[1])

def ioctl(mnt, opts):
    fd = os.open(mnt, os.O_RDONLY | os.O_DIRECTORY)
    buf = ctypes.create_string_buffer(opts.encode())
    try:
        fcntl.ioctl(fd, IOC, struct.pack("QI4x", ctypes.addressof(buf), len(opts)))
    finally:
        os.close(fd)

def slab(name):
    for l in open("/proc/slabinfo"):
        if l.startswith(name + " "):
            return int(l.split()[1])

def w(p, s):
    open(p, "w").write(s)

def r(p):
    try:
        return open(p).read()
    except OSError as e:
        return "errno=%d" % e.errno

def switch(d, m, kind, lowers, up):
    os.makedirs(f"{d}/{up}"); os.makedirs(f"{d}/{up}w")
    ioctl(m, "lowerdir=%s,upperdir=%s/%s,workdir=%s/%sw,ovl_kind=%s" % (":".join(f"{d}/{x}" for x in lowers), d, up, d, up, kind))

def cycle(it, variant):
    d = f"{B}/{it}"; m = f"{d}/m"
    for x in ("l0", "u0", "u0w", "m"):
        os.makedirs(f"{d}/{x}")
    w(f"{d}/l0/base.txt", "base")
    subprocess.check_call(["mount", "-t", "overlay", "ovl-t", "-o", f"lowerdir={d}/l0,upperdir={d}/u0,workdir={d}/u0w", m])
    res = {"variant": variant}
    held, kids = [], []
    try:
        w(f"{m}/a.txt", "a0")
        switch(d, m, "ckpt", ["l0"], "u1")                 # 栈：u1 / u0 / l0
        if variant == 4:
            return res
        held.append(open(f"{m}/a.txt"))                    # lower（u0）上的 real file
        w(f"{m}/a.txt", "a1"); w(f"{m}/b.txt", "b1")       # a.txt copy-up 进 u1
        held.append(open(f"{m}/b.txt"))
        switch(d, m, "ckpt", ["u0", "l0"], "u2")           # 栈：u2 / u1 / u0 / l0
        os.mkdir(f"{m}/dir2"); w(f"{m}/dir2/c.txt", "c2"); w(f"{m}/a.txt", "a2")
        held.append(open(f"{m}/dir2/c.txt"))
        kids.append(subprocess.Popen(["sleep", "1000"], cwd=f"{m}/dir2"))
        res["before"] = [r(f"{m}/a.txt"), r(f"{m}/b.txt"), r(f"{m}/dir2/c.txt"), r(f"{m}/base.txt")]
        switch(d, m, "restore", ["u0", "l0"], "r1")        # 回到第一代：r1 / u0 / l0
        res["after"] = [r(f"{m}/a.txt"), r(f"{m}/b.txt"), r(f"{m}/dir2/c.txt"), r(f"{m}/base.txt")]
        for x in ("u1", "u1w", "u2", "u2w"):
            shutil.rmtree(f"{d}/{x}")
        res["held"] = [os.pread(f.fileno(), 64, 0).decode() for f in held]
        res["kid_cwd"] = os.readlink("/proc/%d/cwd" % kids[0].pid)
        if variant == 3:
            w(f"{m}/x.txt", "x"); held.append(open(f"{m}/x.txt"))
            switch(d, m, "restore", ["u0", "l0"], "r2")
            w(f"{m}/y.txt", "y"); held.append(open(f"{m}/y.txt"))
            switch(d, m, "restore", ["u0", "l0"], "r3")
            res["held3"] = [os.pread(f.fileno(), 64, 0).decode() for f in held]
        res["mnt_held"] = slab("mnt_cache")
    finally:
        for f in held: f.close()
        for k in kids: k.kill(); k.wait()
        if variant == 0:
            switch(d, m, "restore", ["u0", "l0"], "r9")
        elif variant == 2:
            switch(d, m, "ckpt", ["u0", "l0"], "n1")
            switch(d, m, "ckpt", ["r1", "u0", "l0"], "n2")
        res["end"] = [r(f"{m}/a.txt"), r(f"{m}/base.txt")]
        subprocess.check_call(["umount", m])
        shutil.rmtree(d)
    return res

os.makedirs(B, exist_ok=True)
out = {"mnt_start": slab("mnt_cache"), "cycles": []}
for i in range(N):
    try:
        c = cycle(i, i % 5)
    except Exception as e:
        c = {"variant": i % 5, "error": repr(e)}
    c["mnt_after"] = slab("mnt_cache")
    out["cycles"].append(c)
time.sleep(2)
out["mnt_end"] = slab("mnt_cache")
out["leftover"] = os.listdir(B)
print(json.dumps(out))
'''


def main():
    sb = Sandbox.create(template=TPL, timeout=900)
    res = {"template": TPL, "sandbox": sb.sandbox_id, "n": N}

    def sh(c, timeout=600):
        try:
            return sb.commands.run(c, user="root", timeout=timeout).stdout.strip()
        except CommandExitException as e:
            return f"rc={e.exit_code} {(e.stdout + e.stderr).strip()}"

    try:
        res["kernel"] = sh("cat /proc/version")
        sb.files.write("/dev/shm/um.py", GUEST, user="root")
        o = sh(f"python3 /dev/shm/um.py {N}")
        try:
            g = json.loads(o)
        except ValueError:
            g = {"raw": o[-3000:]}
        res["guest"] = g
        res["dmesg"] = sh("dmesg | grep -E 'BUG:|WARNING:|KASAN|Oops|unknown layer array|refcount_t|overlayfs' | grep -v 'overlay-init' | tail -12")
        res["debug_locks"] = sh("grep debug_locks /proc/lockdep_stats 2>/dev/null || true")
        cyc = g.get("cycles", [])
        want_after = ["a0", "errno=2", "errno=2", "base"]
        res["ok"] = bool(cyc) and all("error" not in c for c in cyc) \
            and all(c.get("after") == want_after for c in cyc if c["variant"] != 4) \
            and all(c.get("held") == ["a2", "b1", "c2"] for c in cyc if c["variant"] != 4) \
            and not [l for l in res["dmesg"].splitlines() if "overlayfs" not in l or "WARN" in l or "unknown" in l]
        # mnt_cache：每轮新建约 13 个私有 mount。slabinfo 的 active 数含 per-cpu slab 上已释放的对象，会在几十到两百之间摆动，
        # 所以只在轮数够多（>=50）时判"不随轮数线性增长"（净增 < 新建总数的 1/4）。KASAN 内核里释放的对象先进隔离区，不判。
        res["kasan"] = "kasan" in sh("dmesg | grep -i -m1 'kasan: KernelAddressSanitizer' || true").lower()
        res["mnt_cache_delta"] = g.get("mnt_end", 0) - g.get("mnt_start", 0)
        res["mnt_cache_max_delta"] = max([c.get("mnt_after", 0) for c in cyc] or [0]) - g.get("mnt_start", 0)
        res["mnt_ok"] = None if res["kasan"] or N < 50 else res["mnt_cache_delta"] < 13 * N / 4
    finally:
        sb.kill()
    print(json.dumps(res, ensure_ascii=False, indent=1))


main()
