#!/usr/bin/env python3
"""G-11 / G-17 泄漏专项：create → restore 基点 → delete 长循环里，根盘和内核对象是否随轮数增长。

    python3 probe_leak.py <模板> [--rounds 200] [--sample 10] [--mb 1] [--hold 20] [--hold-mb 4] [--out x.json]

阶段 loop（沙箱 1）：
    基点 c0；每轮：user 写 <mb> MiB 随机数据 → create x_i → 按路径读一遍（让 overlay 经 lower 层查到刚沉下去的文件）
    → restore c0 → delete x_i。gsd 随后回收不再被引用的层目录。每 <sample> 轮（先 sync）记录：
      df / 的 used、/overlay 与 /var/log 的 du（两者都在同一个 ext4 上；df − du 之和的增长 = 已删除但仍被钉住的 inode）、
      /overlay/layers 下的目录数、/proc/slabinfo 的 mnt_cache / dentry / ext4_inode_cache / ovl_inode / kmalloc-*。
    结束后 drop_caches 再记一次。判据：后半程（去掉前 1/4 的预热）df−du 和 mnt_cache 对轮数的斜率接近 0。

阶段 hold（沙箱 2，--hold 0 跳过）：
    root 进程（systemd-run，不在 criu 进程树里，restore 不杀它）每轮打开当轮写的 <hold-mb> MiB 文件并一直拿着 fd，
    另起一个 cwd 停在当轮新建目录里的子进程；循环同上。此时被删除层里的文件被 fd 钉住，残留应与持有的对象数成正比（有界）。
    然后让持有者全部放手，再跑 3 轮空循环（内核在下一次、再下一次切换时释放），残留应回到基线附近。

阶段 kept（沙箱 3，--no-kept 跳过）：回收逻辑最容易出 use-after-free 的形状，主要给 KASAN 内核看。
    k.txt 在 c1 之前建好（沉进 L1）；root 持有者只读打开它，另外 mmap 一份后关掉 fd（映射独自拿着 real file）；
    之后别人追加写（copy-up）、c2、再追加、c3（两次 promote 把 inode 的 lower 换掉两回）；restore c0，删掉 c3/c2/c1，
    层目录全被 gsd 回收；再空转几轮让内核把延迟释放的都放掉。此时持有者手里的 real file 指向已删除层里的 inode：
    读 fd、读映射都必须正常，放手（munmap/close）后 dmesg 无 KASAN/BUG。
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env  # noqa: E402

os.environ.update(load_e2b_env())
from e2b import Sandbox, CommandExitException  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("template")
ap.add_argument("--rounds", type=int, default=200)
ap.add_argument("--sample", type=int, default=10)
ap.add_argument("--mb", type=int, default=1)
ap.add_argument("--hold", type=int, default=20, help="hold 阶段的轮数，0 = 不跑")
ap.add_argument("--hold-mb", type=int, default=4)
ap.add_argument("--no-kept", action="store_true")
ap.add_argument("--no-loop", action="store_true")
ap.add_argument("--out", default="")
args = ap.parse_args()

T0 = time.time()
LAYERS = "/overlay/layers"
SLABS = ("mnt_cache", "dentry", "ext4_inode_cache", "ovl_inode", "inode_cache",
         "kmalloc-64", "kmalloc-96", "kmalloc-128", "kmalloc-192", "kmalloc-256", "kmalloc-512", "kmalloc-1k", "kmalloc-2k")
STAT = (
    "sync; "
    "df -k / | awk 'NR==2{print \"df_used_kb=\"$3}'; "
    "echo du_overlay_kb=$(du -skx /overlay 2>/dev/null | cut -f1); "
    "echo du_log_kb=$(du -skx /var/log 2>/dev/null | cut -f1); "
    f"echo n_layers=$(ls -A {LAYERS} | grep -v -x .trash | wc -l); "
    f"echo n_trash=$(ls -A {LAYERS}/.trash 2>/dev/null | wc -l); "
    "awk '{print \"slab_\"$1\"=\"$2}' /proc/slabinfo | grep -E '^slab_(" + "|".join(SLABS) + ")='; "
    "awk '/^(Slab|SUnreclaim|MemAvailable):/{sub(\":\",\"\",$1); print \"mem_\"$1\"_kb=\"$2}' /proc/meminfo"
)
HOLDER = r'''
import mmap, os, subprocess, sys, time
fds, kids, maps = [], [], []
open("/dev/shm/lk.ack", "w").write("ready")
while True:
    try:
        cmd = open("/dev/shm/lk.cmd").read().split()
    except FileNotFoundError:
        time.sleep(0.02); continue
    if not cmd:
        time.sleep(0.02); continue
    if cmd[0] == "open":
        fds.append(os.open(cmd[1], os.O_RDONLY))
    elif cmd[0] == "cwd":
        kids.append(subprocess.Popen(["sleep", "100000"], cwd=cmd[1]))
    elif cmd[0] == "mmap":
        fd = os.open(cmd[1], os.O_RDONLY)
        maps.append(mmap.mmap(fd, 0, mmap.MAP_SHARED, mmap.PROT_READ))
        os.close(fd)
    elif cmd[0] == "read":
        out = []
        for fd in fds:
            try: out.append("fd:" + os.pread(fd, 4096, 0).decode().replace("\n", "|"))
            except OSError as e: out.append("fd:errno=%d" % e.errno)
        for m in maps:
            out.append("map:" + m[:].decode().replace("\n", "|"))
        open("/dev/shm/lk.out", "w").write(" ".join(out))
    elif cmd[0] == "release":
        for fd in fds: os.close(fd)
        for k in kids: k.kill(); k.wait()
        for m in maps: m.close()
        fds, kids, maps = [], [], []
    os.unlink("/dev/shm/lk.cmd")
    open("/dev/shm/lk.ack", "w").write("done %d %d" % (len(fds), len(kids)))
'''


def log(msg):
    print(f"[{time.time() - T0:7.1f}s] {msg}", file=sys.stderr, flush=True)


class G:
    def __init__(self):
        self.sb = Sandbox.create(template=args.template, timeout=3600)
        self.id = self.sb.sandbox_id

    def sh(self, cmd, user="root", timeout=120):
        try:
            r = self.sb.commands.run(cmd, user=user, timeout=timeout)
            return r.stdout.strip()
        except CommandExitException as e:
            return f"rc={e.exit_code} {(e.stdout + e.stderr).strip()}"

    def stat(self):
        d = {k: int(v) for k, v in re.findall(r"([\w-]+)=(-?\d+)", self.sh(STAT))}
        # 同一个 ext4 上看得见的部分；其余（/mnt/lower 等）不变
        d["pinned_kb"] = d.get("df_used_kb", 0) - d.get("du_overlay_kb", 0) - d.get("du_log_kb", 0)
        return d

    def round(self, c0, i, mb, name="leak.bin", extra=None):
        """返回失败的步骤名或 None"""
        self.sh(f"dd if=/dev/urandom of=/home/user/{name} bs=1M count={mb} status=none", user="user")
        if extra:
            extra()
        try:
            c = self.sb.checkpoint.create(name=f"x{i}")
        except Exception as e:  # noqa: BLE001
            return f"create: {str(e)[:200]}"
        self.sh(f"cat /home/user/{name} > /dev/null; ls -lR /home/user > /dev/null", user="user")
        try:
            self.sb.checkpoint.restore(c0)
        except Exception as e:  # noqa: BLE001
            return f"restore: {str(e)[:200]}"
        try:
            self.sb.checkpoint.delete(c.checkpoint_id)
        except Exception as e:  # noqa: BLE001
            return f"delete: {str(e)[:200]}"
        return None

    def base(self):
        self.sb.commands.run("sleep 100000", background=True, user="user")
        time.sleep(0.5)
        return self.sb.checkpoint.create(name="base").checkpoint_id


def slope(samples, key, skip_frac=0.25):
    """最小二乘斜率（每轮），去掉前 skip_frac 的预热样本"""
    pts = [(s["round"], s[key]) for s in samples if key in s]
    pts = pts[int(len(pts) * skip_frac):]
    n = len(pts)
    if n < 2:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    return round(sum((p[0] - mx) * (p[1] - my) for p in pts) / den, 3) if den else None


def phase_loop(res):
    g = G()
    out = {"sandbox": g.id}
    res["loop"] = out
    try:
        out["kernel"] = g.sh("cat /proc/version")
        c0 = g.base()
        time.sleep(0.5)
        samples = [dict(g.stat(), round=0)]
        fails = []
        for i in range(1, args.rounds + 1):
            err = g.round(c0, i, args.mb)
            if err:
                fails.append((i, err))
                log(f"loop 第 {i} 轮失败: {err}")
                if len(fails) >= 5:
                    break
            if i % args.sample == 0:
                time.sleep(1.5)   # gsd 的层回收是异步的
                s = dict(g.stat(), round=i)
                samples.append(s)
                log(f"loop {i}: df={s.get('df_used_kb')} pinned={s['pinned_kb']} layers={s.get('n_layers')} "
                    f"mnt={s.get('slab_mnt_cache')} dentry={s.get('slab_dentry')} ext4i={s.get('slab_ext4_inode_cache')}")
        g.sh("sync; echo 3 > /proc/sys/vm/drop_caches")
        time.sleep(1)
        out["after_drop_caches"] = g.stat()
        out["samples"] = samples
        out["fails"] = fails
        first, last = samples[0], samples[-1]
        out["delta"] = {k: last[k] - first[k] for k in last if k in first and k != "round"}
        out["delta_after_drop_caches"] = {k: out["after_drop_caches"][k] - first[k] for k in first if k in out["after_drop_caches"]}
        out["slope_per_round"] = {k: slope(samples, k) for k in last if k != "round"}
        out["dmesg"] = g.sh("dmesg | grep -E 'BUG:|WARNING:|KASAN|Oops|unknown layer array|refcount_t' | tail -5")
    finally:
        g.sb.kill()


def phase_hold(res):
    g = G()
    out = {"sandbox": g.id}
    res["hold"] = out

    def tell(cmd):
        g.sh("rm -f /dev/shm/lk.ack")
        g.sb.files.write("/dev/shm/lk.cmd", cmd, user="root")
        for _ in range(100):
            a = g.sh("cat /dev/shm/lk.ack 2>/dev/null")
            if a.startswith("done"):
                return a
            time.sleep(0.05)
        return "timeout"

    try:
        c0 = g.base()
        g.sb.files.write("/dev/shm/holder.py", HOLDER, user="root")
        g.sh("systemd-run --unit=lkhold python3 /dev/shm/holder.py")
        for _ in range(100):
            if g.sh("cat /dev/shm/lk.ack 2>/dev/null") == "ready":
                break
            time.sleep(0.05)
        # 先空转几轮，让基线稳定
        for i in range(1, 4):
            g.round(c0, i, args.hold_mb, name="warm.bin")
        time.sleep(1.5)
        out["baseline"] = g.stat()
        fails = []
        for i in range(1, args.hold + 1):
            def grab(i=i):
                g.sh(f"mkdir -p /home/user/cw_{i} && cp /home/user/hold_{i}.bin /home/user/cw_{i}/f", user="user")
                out.setdefault("holder_acks", []).append(tell(f"open /home/user/hold_{i}.bin") + " / " + tell(f"cwd /home/user/cw_{i}"))
            err = g.round(c0, 100 + i, args.hold_mb, name=f"hold_{i}.bin", extra=grab)
            if err:
                fails.append((i, err))
        time.sleep(1.5)
        out["held"] = g.stat()
        out["holder_fd_reads"] = g.sh("ls -l /proc/$(systemctl show -p MainPID --value lkhold)/fd | grep -c hold_ || true")
        out["release_ack"] = tell("release")
        out["released_no_ioctl"] = g.stat()
        for i in range(1, 4):
            err = g.round(c0, 200 + i, args.hold_mb, name="warm.bin")
            if err:
                fails.append((200 + i, err))
        time.sleep(1.5)
        out["released_after_3_rounds"] = g.stat()
        out["fails"] = fails
        b = out["baseline"]
        out["delta_vs_baseline"] = {ph: {k: out[ph][k] - b[k] for k in ("df_used_kb", "pinned_kb", "n_layers", "slab_mnt_cache", "slab_ext4_inode_cache", "slab_ovl_inode") if k in b and k in out[ph]}
                                    for ph in ("held", "released_no_ioctl", "released_after_3_rounds")}
        out["dmesg"] = g.sh("dmesg | grep -E 'BUG:|WARNING:|KASAN|Oops|unknown layer array|refcount_t' | tail -5")
    finally:
        g.sb.kill()


def phase_kept(res):
    g = G()
    out = {"sandbox": g.id}
    res["kept"] = out

    def tell(cmd):
        g.sh("rm -f /dev/shm/lk.ack")
        g.sb.files.write("/dev/shm/lk.cmd", cmd, user="root")
        for _ in range(100):
            a = g.sh("cat /dev/shm/lk.ack 2>/dev/null")
            if a.startswith("done"):
                return a
            time.sleep(0.05)
        return "timeout"

    def view():
        tell("read")
        return g.sh("cat /dev/shm/lk.out")

    try:
        out["kernel"] = g.sh("cat /proc/version")
        c0 = g.base()
        g.sh("echo A1 > /home/user/k.txt", user="user")
        c1 = g.sb.checkpoint.create(name="c1").checkpoint_id
        g.sb.files.write("/dev/shm/holder.py", HOLDER, user="root")
        g.sh("systemd-run --unit=lkhold python3 /dev/shm/holder.py")
        for _ in range(100):
            if g.sh("cat /dev/shm/lk.ack 2>/dev/null") == "ready":
                break
            time.sleep(0.05)
        out["acks"] = [tell("open /home/user/k.txt"), tell("mmap /home/user/k.txt")]
        g.sh("echo A2 >> /home/user/k.txt", user="user")
        c2 = g.sb.checkpoint.create(name="c2").checkpoint_id
        g.sh("echo A3 >> /home/user/k.txt", user="user")
        c3 = g.sb.checkpoint.create(name="c3").checkpoint_id
        out["view_before_restore"] = view()
        out["layers_before"] = g.sh(f"ls {LAYERS}")
        g.sb.checkpoint.restore(c0)
        for c in (c3, c2, c1):
            g.sb.checkpoint.delete(c)
        fails = []
        for i in range(1, 4):
            err = g.round(c0, 300 + i, 1, name="warm.bin")
            if err:
                fails.append((i, err))
        time.sleep(1.5)
        out["layers_after_delete"] = g.sh(f"ls {LAYERS}")
        out["path_after_restore"] = g.sh("cat /home/user/k.txt 2>&1 | head -1; ls /home/user")
        out["view_after_layers_deleted"] = view()
        g.sh("sync; echo 3 > /proc/sys/vm/drop_caches")
        out["view_after_drop_caches"] = view()
        out["release_ack"] = tell("release")
        for i in range(4, 7):
            err = g.round(c0, 300 + i, 1, name="warm.bin")
            if err:
                fails.append((i, err))
        g.sh("sync; echo 3 > /proc/sys/vm/drop_caches")
        out["fails"] = fails
        out["dmesg"] = g.sh("dmesg | grep -E 'BUG:|WARNING:|KASAN|Oops|unknown layer array|refcount_t' | tail -5")
        want = "fd:A1|A2|A3| map:A1|"
        out["ok"] = (out["view_before_restore"] == want and out["view_after_layers_deleted"] == want
                     and out["view_after_drop_caches"] == want and not fails and not out["dmesg"])
    finally:
        g.sb.kill()


def main():
    res = {"template": args.template, "rounds": args.rounds, "mb": args.mb, "hold_rounds": args.hold, "hold_mb": args.hold_mb}
    try:
        if not args.no_loop:
            phase_loop(res)
        if args.hold:
            phase_hold(res)
        if not args.no_kept:
            phase_kept(res)
    finally:
        s = json.dumps(res, ensure_ascii=False, indent=1)
        if args.out:
            open(args.out, "w").write(s)
        print(s)


main()
