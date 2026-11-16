#!/usr/bin/env python3
"""多棵用户进程树的 checkpoint/restore 压测。

沙箱里常驻 3 棵 uid 1000 的进程树（各是一个 envd 后台命令），每棵都在写内存、写文件：
  a：python（/home/user/mt）
  b：bash + sleep + python（/home/user/mt），树里有多个进程
  c：python（/tmp/mt）—— /tmp 在 overlay 上，跟着回滚
每个 python 每 0.1 秒一拍：给缓冲区每一页写上拍数、往一直开着的 fd 追加一行日志、用 tmp+rename 更新计数文件。
每拍开头自检"内存里的拍数 == 计数文件 == 日志行数（按路径和按 fd 各看一次）== 缓冲区每一页的值"，不一致就往
/dev/shm/mt/<名字>.bad 追加一行（/dev/shm 不回滚，证据留得住）。进程是从 checkpoint 那一刻继续跑的，所以只要
内存镜像（含增量链）和文件系统回到的是同一时刻，自检就永远成立；漏页、文件没回滚、fd 接错都会被它抓到。

主循环 --rounds 轮随机 create / restore（40% 概率回到最早的 3 个）/ 偶尔 delete。宿主侧每次操作后校验：
  - 三棵树都活着，pid 集合不变，没有 T 状态；
  - restore 后各计数文件的值落在"该 checkpoint create 前后读到的值"之间（加上 restore 之后流逝的拍数），随后继续增长；
  - 没有 .bad 文件。
结束时按关键字搜 dmesg、看 lockdep、对比 slab。输出 <outdir>/multitree.log、multitree-ops.log、multitree-summary.json。

在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python stress_multitree.py --template base-gsd --rounds 200 --outdir <dir>
也可以经 run_stress_watch.sh（SCRIPT=stress_multitree.py，带 --mode multitree）带看门狗跑。
判据：multitree-summary.json 的 verdict 为 PASS（lockdep 的 debug_locks 变成 0 直接算失败）。
"""
import argparse
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env  # noqa: E402

os.environ.update(load_e2b_env())
from e2b import Sandbox, CommandExitException  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--template", default="base-gsd")
ap.add_argument("--mode", default="multitree", help="只用于文件名前缀（与 run_stress_watch.sh 配合）")
ap.add_argument("--rounds", type=int, default=200)
ap.add_argument("--mb", type=int, default=4, help="每棵树的缓冲区大小（MiB），每拍全部写一遍")
ap.add_argument("--seed", type=int, default=20261008)
ap.add_argument("--outdir", default=".")
ap.add_argument("--premask-whiteout", action="store_true", help="先删一个 base 镜像里的文件（/etc/legal），让内核缓存的共享 whiteout 落在开机时的 workdir 里（永不删除），掩盖修复前的内核上的一个 BUG（restore 后删下层文件 → may_delete BUG），以便看其余路径；默认不开")
args = ap.parse_args()
random.seed(args.seed)
os.makedirs(args.outdir, exist_ok=True)
P = args.mode

BAD_RE = (r"KASAN|BUG:|Oops|WARNING:|list_add corruption|list_del corruption|possible circular locking|"
          r"possible recursive locking|sleeping function called from invalid context|ODEBUG|"
          r"blocked for more than|hung_task|soft lockup|rcu.*stall|detected stalls|list corruption")
TICK = 0.1
T0 = time.time()
LOGF = open(os.path.join(args.outdir, f"{P}.log"), "a")
OPF = open(os.path.join(args.outdir, f"{P}-ops.log"), "a")


def log(msg):
    line = f"[{time.time() - T0:7.1f}s {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


def oplog(msg):
    t = time.time()
    OPF.write(f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d} {msg}\n")
    OPF.flush()


def save(name, text):
    with open(os.path.join(args.outdir, name), "w") as f:
        f.write(text)


TREE_PY = r'''
import os, sys, time
name, d, mb = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.makedirs(d, exist_ok=True)
size = mb << 20; npg = size // 4096; buf = bytearray(size)
buf[::4096] = bytes(npg)
cntp = "%s/%s.cnt" % (d, name); logp = "%s/%s.log" % (d, name)
bad = "/dev/shm/mt/%s.bad" % name; hb = "/dev/shm/mt/%s.hb" % name
n = 0; nbad = 0
def wr_cnt(v):
    with open(cntp + ".tmp", "w") as f: f.write(str(v))
    os.replace(cntp + ".tmp", cntp)
def complain(msg):
    global nbad
    nbad += 1
    if nbad <= 50:
        with open(bad, "a") as f: f.write("%.3f pid=%d n=%d %s\n" % (time.time(), os.getpid(), n, msg))
logf = open(logp, "ab", buffering=0)
wr_cnt(0)
while True:
    try: fv = int(open(cntp).read())
    except Exception as e: fv = "ERR %r" % (e,)
    if fv != n: complain("cnt file=%r mem=%d" % (fv, n))
    try: sz = os.stat(logp).st_size
    except Exception as e: sz = "ERR %r" % (e,)
    fsz = os.fstat(logf.fileno()).st_size
    if sz != 8 * n or fsz != 8 * n: complain("log size path=%r fd=%r want=%d" % (sz, fsz, 8 * n))
    got = bytes(buf[::4096])
    if got != bytes([n & 255]) * npg:
        wrong = sum(1 for x in got if x != (n & 255))
        complain("mem pages wrong=%d/%d want=%d sample=%r" % (wrong, npg, n & 255, sorted(set(got))[:6]))
    m = n + 1
    buf[::4096] = bytes([m & 255]) * npg
    logf.write(b"%07d\n" % m)
    wr_cnt(m)
    n = m
    with open(hb, "w") as f: f.write("%d %.3f" % (n, time.time()))
    time.sleep(0.1)
'''
NAMES = ["a", "b", "c"]
DIRS = {"a": "/home/user/mt", "b": "/home/user/mt", "c": "/tmp/mt"}

sb = Sandbox.create(template=args.template, timeout=3600)
log(f"sandbox {sb.sandbox_id} template={args.template} mode={P} rounds={args.rounds} mb={args.mb}")
if args.premask_whiteout:
    try:
        log("premask-whiteout: " + sb.commands.run("rm -f /etc/legal; find /overlay/workspace -xdev -type c | head -3 | tr '\\n' ' '", user="root").stdout)
    except Exception as e:  # noqa: BLE001
        log(f"premask-whiteout 失败: {e}")


def sh(cmd, user="root", timeout=180):
    try:
        r = sb.commands.run(cmd, user=user, timeout=timeout)
        return 0, r.stdout, r.stderr
    except CommandExitException as e:
        return e.exit_code, e.stdout, e.stderr


def out(cmd, user="root", timeout=180):
    return sh(cmd, user, timeout)[1].strip()


def call(fn, *a):
    try:
        return True, fn(*a)
    except Exception as e:  # noqa: BLE001
        return False, e


def ecode(e):
    m = re.search(r"Code\.(\w+)", str(e)) or re.search(r"\((failed_precondition)\)", str(e))
    if m:
        return m.group(1)
    return {"RateLimitException": "resource_exhausted", "NotEnoughSpaceException": "resource_exhausted",
            "CheckpointRestoreException": "data_loss"}.get(type(e).__name__, type(e).__name__)


def slab():
    res = {}
    for line in out("grep -E '^(mnt_cache|dentry|kmalloc-[0-9k]+) ' /proc/slabinfo").splitlines():
        p = line.split()
        res[p[0]] = int(p[1])
    return res


def lockdep():
    return out("grep -E 'debug_locks|dependency chains:' /proc/lockdep_stats 2>/dev/null || echo 'no /proc/lockdep_stats'")


SNAPSHOT = ("for n in a b; do echo \"cnt $n $(cat /home/user/mt/$n.cnt 2>/dev/null)\"; done; echo \"cnt c $(cat /tmp/mt/c.cnt 2>/dev/null)\"; "
            "ps -u 1000 -o pid=,ppid=,stat=,comm= | sed 's/^/ps /'; for f in /dev/shm/mt/*.bad; do [ -e $f ] && echo \"bad $f\"; done; true")


def snapshot():
    """→ ({名字: 计数}, [(pid, ppid, stat, comm)], [bad 文件])"""
    c, ps, bad = {}, [], []
    for line in out(SNAPSHOT).splitlines():
        p = line.split()
        if p[:1] == ["cnt"]:
            try:
                c[p[1]] = int(p[2])
            except (IndexError, ValueError):
                c[p[1]] = None
        elif p[:1] == ["ps"] and len(p) >= 5:
            ps.append((int(p[1]), int(p[2]), p[3], p[4]))
        elif p[:1] == ["bad"]:
            bad.append(p[1])
    return c, ps, bad


def main():
    boot = out("uname -a; zcat /proc/config.gz 2>/dev/null | grep -E '^CONFIG_(KASAN|KASAN_GENERIC|PROVE_LOCKING|DEBUG_LIST|DEBUG_OBJECTS)=' ; "
               "cat /sys/kernel/mm/transparent_hugepage/enabled; free -m | sed -n 2p; df -m / /tmp /var/lib/gsd/snapshots | tail -3; /usr/bin/gsd -commit")
    log("boot:\n" + boot)
    lock0 = lockdep()
    log("lockdep(开机): " + lock0.replace("\n", " | "))
    save(f"{P}-dmesg-boot.txt", out("dmesg"))

    sh("mkdir -p /dev/shm/mt && chmod 777 /dev/shm/mt")
    sb.files.write("/home/user/mt_tree.py", TREE_PY)
    sb.commands.run(f"python3 /home/user/mt_tree.py a {DIRS['a']} {args.mb}", background=True)
    sb.commands.run(f"bash -c 'sleep 99999 & python3 /home/user/mt_tree.py b {DIRS['b']} {args.mb}; wait'", background=True)
    sb.commands.run(f"python3 /home/user/mt_tree.py c {DIRS['c']} {args.mb}", background=True)
    for _ in range(100):
        c, ps, bad = snapshot()
        if all((c.get(n) or 0) >= 3 for n in NAMES):
            break
        time.sleep(0.1)
    pids0 = sorted(p[0] for p in ps)
    log(f"三棵树起来了: cnt={c} ps={ps}")
    if len(ps) != 5:
        raise SystemExit(f"进程数不是 5（a: python；b: bash+sleep+python；c: python）: {ps}")
    slab0 = slab()
    log(f"slab 起点: {slab0}")

    stats = {"create_ok": 0, "restore_ok": 0, "delete_ok": 0, "errors": {}, "verify_fail": 0, "verify_ok": 0}
    fails, err_samples = [], []
    tim = {"create": [], "restore": []}
    cps = []        # [{"id", "lo", "hi", "round"}]

    def vfail(r, what, detail):
        stats["verify_fail"] += 1
        msg = f"r{r} {what}: {detail}"
        fails.append(msg)
        log("  !! 校验失败 " + msg[:600])
        oplog("VERIFY-FAIL " + msg[:300])

    def check_alive(r, what, ps, bad):
        ok = True
        if sorted(p[0] for p in ps) != pids0:
            vfail(r, what, f"pid 集合变了: 现在 {sorted(p[0] for p in ps)}，应为 {pids0}；ps={ps}")
            ok = False
        if any(p[2].startswith("T") for p in ps):
            vfail(r, what, f"有 T 状态的进程: {ps}")
            ok = False
        if bad:
            vfail(r, what, f"进程自检报错文件: {bad}")
            ok = False
        return ok

    def do_create(r):
        lo, _, _ = snapshot()
        t = time.time()
        oplog(f"r{r} create start")
        ok, c = call(sb.checkpoint.create, f"r{r}")
        oplog(f"r{r} create end ok={ok} {c.checkpoint_id if ok else str(c)[:200]}")
        if not ok:
            k = f"create:{ecode(c)}"
            stats["errors"][k] = stats["errors"].get(k, 0) + 1
            err_samples.append(f"r{r} {k} {str(c)[:400]}")
            if ecode(c) == "resource_exhausted":
                # 快照存储满：删掉几个旧 checkpoint 腾地方（保留最早的 3 个）
                for v in [x for x in cps[3:-2]][:6]:
                    okd, _ = call(sb.checkpoint.delete, v["id"])
                    if okd:
                        cps.remove(v)
                        stats["delete_ok"] += 1
            _, ps, bad = snapshot()
            check_alive(r, "create 失败后", ps, bad)
            return
        hi, ps, bad = snapshot()
        stats["create_ok"] += 1
        tim["create"].append(dict(c.timings or {}, wall_ms=(time.time() - t) * 1000, image_bytes=c.image_bytes))
        cps.append({"id": c.checkpoint_id, "lo": lo, "hi": hi, "round": r})
        good = check_alive(r, "create 后", ps, bad)
        if getattr(c, "process_trees", None) != 3 or c.clear_refs_failed != 0:
            vfail(r, "create", f"process_trees={getattr(c, 'process_trees', None)} clear_refs_failed={c.clear_refs_failed}")
            good = False
        if any(hi.get(n) is None or lo.get(n) is None or hi[n] < lo[n] for n in NAMES):
            vfail(r, "create", f"计数没有前进或读不到: 前 {lo} 后 {hi}")
            good = False
        stats["verify_ok"] += good

    def do_restore(r):
        target = random.choice(cps[:3]) if random.random() < 0.4 else random.choice(cps)
        t = time.time()
        oplog(f"r{r} restore start {target['id']} (第 {cps.index(target) + 1}/{len(cps)} 个，r{target['round']})")
        ok, c = call(sb.checkpoint.restore, target["id"])
        t1 = time.time()
        oplog(f"r{r} restore end ok={ok} {'' if ok else str(c)[:200]}")
        if not ok:
            k = f"restore:{ecode(c)}"
            stats["errors"][k] = stats["errors"].get(k, 0) + 1
            err_samples.append(f"r{r} {k} {str(c)[:600]}")
            _, ps, bad = snapshot()
            log(f"  restore 失败后 ps={ps} bad={bad}")
            return
        v, ps, bad = snapshot()
        slack = int((time.time() - t1) / TICK) + 3
        stats["restore_ok"] += 1
        tim["restore"].append(dict(sb.checkpoint.last_timings or {}, wall_ms=(t1 - t) * 1000))
        good = check_alive(r, "restore 后", ps, bad)
        for n in NAMES:
            lo, hi = target["lo"].get(n), target["hi"].get(n)
            if v.get(n) is None or lo is None or hi is None or not (lo <= v[n] <= hi + slack):
                vfail(r, "restore", f"树 {n} 的计数 {v.get(n)} 不在 checkpoint（r{target['round']}）的范围 [{lo}, {hi}+{slack}] 内；全部 {v}，目标 lo={target['lo']} hi={target['hi']}")
                good = False
        time.sleep(0.35)
        v2, ps2, bad2 = snapshot()
        if not all(v2.get(n) is not None and v.get(n) is not None and v2[n] > v[n] for n in NAMES):
            vfail(r, "restore", f"restore 后计数没有继续增长: {v} → {v2}；ps={ps2}")
            good = False
        good &= check_alive(r, "restore 后 0.35 秒", ps2, bad2)
        stats["verify_ok"] += good

    do_create(0)
    if not cps:
        raise SystemExit(f"第一个 checkpoint 就失败: {err_samples}")
    for r in range(1, args.rounds + 1):
        x = random.random()
        if x < 0.45 or len(cps) < 2:
            do_create(r)
        elif x < 0.92:
            do_restore(r)
        else:
            if len(cps) > 6:
                victim = random.choice(cps[3:-2])
                oplog(f"r{r} delete start {victim['id']}")
                ok, c = call(sb.checkpoint.delete, victim["id"])
                oplog(f"r{r} delete end ok={ok}")
                if ok:
                    cps.remove(victim)
                    stats["delete_ok"] += 1
                else:
                    k = f"delete:{ecode(c)}"
                    stats["errors"][k] = stats["errors"].get(k, 0) + 1
                    err_samples.append(f"r{r} {k} {str(c)[:300]}")
            else:
                do_restore(r)
        if r % 25 == 0:
            c, ps, bad = snapshot()
            log(f"轮 {r}: {stats}；cnt={c}；checkpoint {len(cps)} 个；{out('df -m /var/lib/gsd/snapshots | tail -1')}")
        if r % 50 == 0:
            save(f"{P}-dmesg-r{r}.txt", out("dmesg"))

    # ---- 收尾 ----
    c, ps, bad = snapshot()
    badtxt = out("for f in /dev/shm/mt/*.bad; do [ -e $f ] && { echo \"== $f\"; head -20 $f; }; done; true")
    hb = out("for n in a b c; do echo \"$n $(cat /dev/shm/mt/$n.hb)\"; done; date +%s.%N")
    dm = out("dmesg")
    save(f"{P}-dmesg-final.txt", dm)
    hits = [l for l in dm.splitlines() if re.search(BAD_RE, l)]
    lock1 = lockdep()
    slab1 = slab()
    health = out("curl -s -m 5 http://127.0.0.1:49984/health")
    state = out("python3 - <<'EOF'\nimport json\nd=json.load(open('/var/lib/gsd/snapshots/state.json'))\nprint(json.dumps({'version': d.get('version'), 'checkpoints': len(d.get('checkpoints', [])), 'layers': len(d['overlay']['layers']), 'last_trees': len(d.get('last_trees', []))}))\nEOF")
    lsl = out("ls /overlay/layers | wc -l; ls -d /var/lib/gsd/snapshots/*_mem | wc -l; df -m / /var/lib/gsd/snapshots | tail -2")

    def p50(xs):
        xs = sorted(x for x in xs if isinstance(x, (int, float)))
        return round(xs[len(xs) // 2], 2) if xs else None

    perf = {"create": {k: p50([d.get(k) for d in tim["create"]]) for k in ("wall_ms", "total_ms", "criu_dump_ms", "stopped_ms", "overlay_sink_ms", "precheck_ms", "find_pids_ms", "image_bytes")},
            "restore": {k: p50([d.get(k) for d in tim["restore"]]) for k in ("wall_ms", "total_ms", "criu_restore_ms", "kill_ms", "overlay_restore_ms", "precheck_ms", "find_pids_ms")},
            "create_max_total_ms": max([d.get("total_ms", 0) for d in tim["create"]] or [0]),
            "restore_max_total_ms": max([d.get("total_ms", 0) for d in tim["restore"]] or [0])}
    unexpected = {k: v for k, v in stats["errors"].items() if k != "create:resource_exhausted"}
    verdict = "PASS" if (stats["verify_fail"] == 0 and not hits and not unexpected and not bad and "debug_locks: 0" not in lock1.replace("  ", " ")
                         and '"ok"' in health and len(ps) == 5) else "FAIL"
    summ = {"verdict": verdict, "sandbox": sb.sandbox_id, "template": args.template, "rounds": args.rounds, "mb_per_tree": args.mb, "seed": args.seed,
            "stats": stats, "unexpected_errors": unexpected, "error_samples": err_samples[:20], "verify_fail_samples": fails[:20],
            "bad_files": bad, "bad_text": badtxt, "final_cnt": c, "final_ps": ps, "heartbeats": hb, "dmesg_hits": hits[:40], "dmesg_lines": len(dm.splitlines()),
            "lockdep_boot": lock0, "lockdep_end": lock1, "slab_boot": slab0, "slab_end": slab1,
            "slab_delta": {k: slab1.get(k, 0) - slab0.get(k, 0) for k in slab0}, "health": health, "state": state, "dirs": lsl,
            "checkpoints_left": len(cps), "perf_p50": perf, "boot": boot, "elapsed_s": round(time.time() - T0, 1)}
    json.dump(summ, open(os.path.join(args.outdir, f"{P}-summary.json"), "w"), ensure_ascii=False, indent=1)
    json.dump(tim, open(os.path.join(args.outdir, f"{P}-timings.json"), "w"))
    log(f"结果 {verdict}: {stats}")
    log(f"  校验: ok {stats['verify_ok']} / fail {stats['verify_fail']}；意外错误 {unexpected}；bad 文件 {bad}")
    log(f"  dmesg 关键字命中 {len(hits)} 行；lockdep(结束): {lock1.splitlines()[0] if lock1 else ''}；health {health}")
    log(f"  mnt_cache {slab0.get('mnt_cache')} → {slab1.get('mnt_cache')}；state {state}")
    log(f"  性能 p50: {json.dumps(perf)}")
    for s in fails[:10]:
        log("  FAIL样本: " + s[:500])
    for s in err_samples[:5]:
        log("  错误样本: " + s[:300])
    return 0 if verdict == "PASS" else 1


rc = 1
try:
    rc = main()
finally:
    try:
        sb.kill()
    except Exception as e:  # noqa: BLE001
        log(f"kill failed: {e}")
sys.exit(rc)
