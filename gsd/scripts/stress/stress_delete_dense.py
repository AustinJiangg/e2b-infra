#!/usr/bin/env python3
"""删文件密集的 overlay 热切换压测：反复删除下层文件和目录、再重建（产生 whiteout 和 opaque 目录），
同时连续做 create / restore / delete；每次 restore 之后校验文件集合与该 checkpoint 创建时完全一致。

针对的是"属于 work 目录的状态没有随切换丢弃"这一类缺陷：切换层栈之后第一次要 whiteout 时内核 BUG，以及 trap inode 悬空造成的 ELOOP/ENXIO/ENOTDIR。

三类负载：
  det   ：宿主驱动、可精确校验的一棵树（一半在 /home/user/dd/det，一半在 /tmp/dd/det）。每轮在 create 之前、restore 之后各做一批
          变更（删文件、rm -rf 目录、在被删的名字上重建目录和文件），都在静止状态下做，任何异常都算失败；
          create 之前记下清单（类型、路径、大小、内容 md5），restore 之后的清单必须与目标 checkpoint 的逐条相同。
  live  ：--workers 个 root worker（systemd-run，不归 gsd 管，不被 dump 也不被杀），各自在自己的子树里不停地删、重建、列目录，
          切换就发生在这些操作中间。树会被 restore 整个换掉，所以 ENOENT / ESTALE / EEXIST / ENOTEMPTY 是预期的，其他 errno 算失败。
  user  ：一个 uid 1000 的后台进程（一棵用户树，每轮被 criu dump / restore），循环"删掉一个文件、过一会儿再建回来"，
          内存里的进度随 restore 一起回滚，所以它不应该遇到任何错误（有就写 /dev/shm，算失败）。
另外：一开始先建一个 checkpoint（树沉进下层），之后的删除才是"删下层文件"；结束时统计各层里的 whiteout（0:0 字符设备）
和 opaque 目录（trusted.overlay.opaque=y）个数，确认确实产生过。

在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    SCRIPT=stress_delete_dense.py bash run_stress_watch.sh <outdir> --template base-gsd --mode deldense --rounds 200
判据：<outdir>/deldense-summary.json 的 verdict 为 PASS。
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
ap.add_argument("--mode", default="deldense", help="只用于文件名前缀（与 run_stress_watch.sh 配合）")
ap.add_argument("--rounds", type=int, default=200)
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--seed", type=int, default=20261008)
ap.add_argument("--outdir", default=".")
args = ap.parse_args()
random.seed(args.seed)
os.makedirs(args.outdir, exist_ok=True)
P = args.mode

BAD_RE = (r"KASAN|BUG:|Oops|WARNING:|list_add corruption|list_del corruption|possible circular locking|"
          r"possible recursive locking|sleeping function called from invalid context|ODEBUG|"
          r"blocked for more than|hung_task|soft lockup|rcu.*stall|detected stalls|list corruption")
# 不在关键词判据里、但与本场景直接相关的内核提示，单独统计
NOTE_RE = r"whiteout|failed to lock workdir|overlapping|newly created inode found in cache"
EXPECTED_ERRNO = {"ENOENT", "ESTALE", "EEXIST", "ENOTEMPTY"}
ROOTS = ["/home/user/dd", "/tmp/dd"]
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


# det 树：init 建满；mutate 做一批删除/重建；manifest 输出清单
DET_PY = r'''
import hashlib, json, os, random, shutil, sys
cmd, seed = sys.argv[1], int(sys.argv[2])
ROOTS = ["/home/user/dd/det", "/tmp/dd/det"]
ND, NF, NS, NSF = 6, 12, 3, 5
def all_dirs():
    for r in ROOTS:
        for d in range(ND):
            yield "%s/d%02d" % (r, d)
            for s in range(NS):
                yield "%s/d%02d/s%d" % (r, d, s)
def files_of(p):
    n = NSF if os.path.basename(p).startswith("s") else NF
    return ["%s/f%02d" % (p, i) for i in range(n)]
def put(path, tag):
    with open(path, "w") as f:
        f.write("%s %s\n" % (path, tag) * (1 + hash(path) % 7))
def fill(p, tag):
    os.makedirs(p, exist_ok=True)
    for f in files_of(p):
        put(f, tag)
if cmd == "init":
    for p in all_dirs():
        fill(p, "init")
    print("init ok")
elif cmd == "mutate":
    rnd = random.Random(seed); st = {"unlink": 0, "rmtree": 0, "mkdir": 0, "create": 0}
    dirs = list(all_dirs())
    # 删文件（下层的文件 → whiteout；本层的 → 直接删）
    cand = [f for p in dirs if os.path.isdir(p) for f in files_of(p) if os.path.lexists(f)]
    for f in rnd.sample(cand, min(16, len(cand))):
        os.unlink(f); st["unlink"] += 1
    # rm -rf 整个目录（下层目录 → 逐个 whiteout 再 whiteout 目录本身）
    cand = [p for p in dirs if os.path.isdir(p)]
    for p in rnd.sample(cand, min(4, len(cand))):
        if os.path.isdir(p):
            shutil.rmtree(p); st["rmtree"] += 1
    # 在被删掉的名字上重建目录（盖在 whiteout 上 → opaque 目录）和文件
    cand = [p for p in dirs if not os.path.lexists(p) and os.path.isdir(os.path.dirname(p))]
    for p in rnd.sample(cand, min(4, len(cand))):
        if os.path.isdir(os.path.dirname(p)):
            os.mkdir(p); st["mkdir"] += 1
            for f in rnd.sample(files_of(p), 3):
                put(f, "m%d" % seed); st["create"] += 1
    cand = [f for p in dirs if os.path.isdir(p) for f in files_of(p) if not os.path.lexists(f)]
    for f in rnd.sample(cand, min(10, len(cand))):
        put(f, "m%d" % seed); st["create"] += 1
    print(json.dumps(st))
elif cmd == "manifest":
    out = []
    for r in ROOTS:
        for dp, dn, fn in os.walk(r):
            dn.sort()
            out.append("d %s" % dp)
            for f in sorted(fn):
                p = os.path.join(dp, f); s = os.lstat(p)
                out.append("f %s %d %s" % (p, s.st_size, hashlib.md5(open(p, "rb").read()).hexdigest()))
    print("\n".join(out))
'''

# live worker：在自己的子树里不停地删、重建、列目录
LIVE_PY = r'''
import errno, json, os, random, shutil, sys, time
wid, root = int(sys.argv[1]), sys.argv[2]
rnd = random.Random(wid)
ops = {}; errs = {}; last = 0.0
def note(op, e):
    k = op + ":" + errno.errorcode.get(e.errno, str(e.errno)); errs[k] = errs.get(k, 0) + 1
def run(op, fn):
    ops[op] = ops.get(op, 0) + 1
    try:
        fn()
    except OSError as e:
        note(op, e)
def fill(p):
    os.makedirs(p, exist_ok=True)
    for i in range(6):
        with open("%s/f%d" % (p, i), "w") as f:
            f.write("x" * 64)
def rmtree(p):
    # 不用 shutil.rmtree：它会吞掉部分错误
    for n in os.listdir(p):
        q = p + "/" + n
        if os.path.isdir(q) and not os.path.islink(q):
            rmtree(q)
        else:
            os.unlink(q)
    os.rmdir(p)
dirs = ["%s/w%d/d%d" % (root, wid, i) for i in range(8)] + ["%s/w%d/d%d/s%d" % (root, wid, i, j) for i in range(8) for j in range(2)]
for p in dirs:
    run("fill", lambda: fill(p))
while True:
    p = rnd.choice(dirs); x = rnd.random()
    if x < 0.40:
        run("unlink", lambda: os.unlink("%s/f%d" % (p, rnd.randrange(6))))
    elif x < 0.55:
        run("rmtree", lambda: rmtree(p))
    elif x < 0.75:
        run("fill", lambda: fill(p))
    elif x < 0.85:
        run("listdir", lambda: os.listdir(p))
    elif x < 0.95:
        run("stat", lambda: os.stat("%s/f%d" % (p, rnd.randrange(6))))
    else:
        run("read", lambda: open("%s/f%d" % (p, rnd.randrange(6))).read())
    now = time.time()
    if now - last > 1.0:
        last = now
        with open("/dev/shm/dd/w%d.json.tmp" % wid, "w") as f:
            json.dump({"ops": ops, "errs": errs, "t": now}, f)
        os.replace("/dev/shm/dd/w%d.json.tmp" % wid, "/dev/shm/dd/w%d.json" % wid)
    if sum(ops.values()) % 64 == 0:
        time.sleep(0.002)
'''

# user 进程：被 criu dump/restore；删一个、过一会儿建回来。内存进度与文件系统一起回滚，不应有任何错误
USER_PY = r'''
import os, time
d = "/home/user/dd/user"; os.makedirs(d, exist_ok=True)
N = 40
for i in range(N):
    open("%s/u%02d" % (d, i), "w").write("u")
n = 0; gone = []
while True:
    i = n % N; p = "%s/u%02d" % (d, i)
    try:
        os.unlink(p); gone.append(p)
        if len(gone) > N // 2:
            q = gone.pop(0)
            fd = os.open(q, os.O_WRONLY | os.O_CREAT | os.O_EXCL); os.write(fd, b"u"); os.close(fd)
        want = N - len(gone); have = len(os.listdir(d))
        if have != want:
            raise OSError(0, "listdir %d != %d" % (have, want))
    except OSError as e:
        with open("/dev/shm/dd/user.bad", "a") as f:
            f.write("%.3f n=%d %s %r\n" % (time.time(), n, p, e))
        # 重新对齐，避免一条错误刷屏
        gone = [("%s/u%02d" % (d, k)) for k in range(N) if not os.path.exists("%s/u%02d" % (d, k))]
    n += 1
    with open("/dev/shm/dd/user.hb", "w") as f:
        f.write("%d %.3f" % (n, time.time()))
    time.sleep(0.02)
'''

sb = Sandbox.create(template=args.template, timeout=3600)
log(f"sandbox {sb.sandbox_id} template={args.template} mode={P} rounds={args.rounds} workers={args.workers}")


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


def lockdep():
    return out("grep -E 'debug_locks|dependency chains:' /proc/lockdep_stats 2>/dev/null || echo 'no /proc/lockdep_stats'")


def slab():
    res = {}
    for line in out("grep -E '^(mnt_cache|dentry|ovl_inode|ext4_inode_cache) ' /proc/slabinfo").splitlines():
        p = line.split()
        res[p[0]] = int(p[1])
    return res


def ovl_marks():
    """各层里的 whiteout（0:0 字符设备）和 opaque 目录个数。"""
    return out("python3 - <<'EOF'\nimport os, stat\nw = o = 0\nfor top in ('/overlay/workspace', '/overlay/layers'):\n    for dp, dn, fn in os.walk(top):\n"
               "        for n in fn:\n            try:\n                s = os.lstat(os.path.join(dp, n))\n                w += stat.S_ISCHR(s.st_mode) and s.st_rdev == 0\n            except OSError: pass\n"
               "        for n in dn:\n            try:\n                o += os.getxattr(os.path.join(dp, n), 'trusted.overlay.opaque') == b'y'\n            except OSError: pass\n"
               "print('whiteouts=%d opaque_dirs=%d' % (w, o))\nEOF")


def workers_state():
    txt = out("for f in /dev/shm/dd/w*.json; do cat $f; echo; done; date +%s.%N")
    lines = [l for l in txt.splitlines() if l.strip()]
    now = float(lines[-1])
    ops, errs, age = {}, {}, {}
    for i, l in enumerate(lines[:-1]):
        try:
            d = json.loads(l)
        except ValueError:
            continue
        for k, v in d["ops"].items():
            ops[k] = ops.get(k, 0) + v
        for k, v in d["errs"].items():
            errs[k] = errs.get(k, 0) + v
        age[i] = round(now - d["t"], 1)
    return ops, errs, age


def user_state():
    txt = out("cat /dev/shm/dd/user.hb 2>/dev/null; echo; date +%s.%N; ps -o pid=,stat=,args= -u user | grep user_dd.py | grep -v grep; "
              "echo ==BAD; head -20 /dev/shm/dd/user.bad 2>/dev/null")
    head, _, bad = txt.partition("==BAD")
    lines = head.strip().splitlines()
    hb = lines[0].split() if lines and lines[0].strip() else ["-1", "0"]
    now = float(lines[1]) if len(lines) > 1 else 0.0
    procs = [l.split(None, 2) for l in lines[2:]]
    return {"n": int(hb[0]), "hb_age": round(now - float(hb[1]), 2), "procs": procs, "bad": bad.strip()}


def main():
    boot = out("uname -a; cat /sys/kernel/mm/transparent_hugepage/enabled; free -m | sed -n 2p; df -m / /overlay /tmp /var/lib/gsd/snapshots | tail -4; "
               "/usr/bin/gsd -commit; findmnt -n -o SOURCE,FSTYPE,OPTIONS /")
    log("boot:\n" + boot)
    lock0 = lockdep()
    log("lockdep(boot): " + lock0.replace("\n", "; "))
    save(f"{P}-dmesg-boot.txt", out("dmesg"))
    sb.files.write("/dev/shm/det_dd.py", DET_PY)
    sb.files.write("/dev/shm/live_dd.py", LIVE_PY)
    sb.files.write("/home/user/user_dd.py", USER_PY, user="user")
    out("mkdir -p /dev/shm/dd && chmod 1777 /dev/shm/dd; mkdir -p /home/user/dd /tmp/dd && chown user:user /home/user/dd")
    log("marks(boot): " + ovl_marks())

    stats = {"create_ok": 0, "restore_ok": 0, "delete_ok": 0, "errors": {}, "verify_ok": 0, "verify_fail": 0,
             "mutate": {"unlink": 0, "rmtree": 0, "mkdir": 0, "create": 0}, "mutate_fail": 0}
    fails, err_samples = [], []
    cps = []
    mseq = [0]

    def vfail(r, what, detail):
        stats["verify_fail"] += 1
        msg = f"r{r} {what}: {detail}"
        fails.append(msg)
        log("  !! 校验失败 " + msg[:700])
        oplog("VERIFY-FAIL " + msg[:300])

    def det(cmd, r, seed=0):
        rc, o, e = sh(f"python3 /dev/shm/det_dd.py {cmd} {seed}")
        if rc != 0:
            stats["mutate_fail"] += 1
            vfail(r, f"det {cmd}", f"rc={rc} stderr={e.strip()[-500:]}")
            return None
        return o

    def mutate(r):
        mseq[0] += 1
        o = det("mutate", r, args.seed * 1000 + mseq[0])
        if o:
            for k, v in json.loads(o.strip().splitlines()[-1]).items():
                stats["mutate"][k] += v

    def manifest(r):
        o = det("manifest", r)
        return None if o is None else o.strip().splitlines()

    def do_create(r):
        mutate(r)
        man = manifest(r)
        if man is None:
            return
        oplog(f"r{r} create start")
        ok, c = call(sb.checkpoint.create, f"r{r}")
        oplog(f"r{r} create end ok={ok} {c.checkpoint_id if ok else str(c)[:200]}")
        if not ok:
            k = f"create:{ecode(c)}"
            stats["errors"][k] = stats["errors"].get(k, 0) + 1
            err_samples.append(f"r{r} {k} {str(c)[:400]}")
            if ecode(c) == "resource_exhausted":
                for v in [x for x in cps[3:-2]][:6]:
                    okd, _ = call(sb.checkpoint.delete, v["id"])
                    if okd:
                        cps.remove(v)
                        stats["delete_ok"] += 1
            return
        stats["create_ok"] += 1
        cps.append({"id": c.checkpoint_id, "man": man, "round": r})
        # create 不应改变文件集合
        man2 = manifest(r)
        if man2 is not None and man2 != man:
            vfail(r, "create", f"create 前后清单不同: {diff(man, man2)}")
        else:
            stats["verify_ok"] += 1
        # create 之后马上再删一批（切换后的第一批 whiteout）
        mutate(r)

    def diff(a, b):
        sa, sb_ = set(a), set(b)
        return f"少了 {len(sa - sb_)} 条 {sorted(sa - sb_)[:4]}；多了 {len(sb_ - sa)} 条 {sorted(sb_ - sa)[:4]}"

    def do_restore(r):
        target = random.choice(cps[:3]) if random.random() < 0.4 else random.choice(cps)
        oplog(f"r{r} restore start {target['id']} (第 {cps.index(target) + 1}/{len(cps)} 个，r{target['round']})")
        ok, c = call(sb.checkpoint.restore, target["id"])
        oplog(f"r{r} restore end ok={ok} {'' if ok else str(c)[:200]}")
        if not ok:
            k = f"restore:{ecode(c)}"
            stats["errors"][k] = stats["errors"].get(k, 0) + 1
            err_samples.append(f"r{r} {k} {str(c)[:600]}")
            return
        stats["restore_ok"] += 1
        man = manifest(r)
        if man is None:
            return
        if man != target["man"]:
            vfail(r, "restore", f"清单与 checkpoint r{target['round']} 创建时不同（{len(target['man'])} → {len(man)} 条）: {diff(target['man'], man)}")
        else:
            stats["verify_ok"] += 1
        # restore 之后马上删下层文件（F-1 的触发点）
        mutate(r)

    # ---- 准备：建树，沉进下层 ----
    if det("init", 0) is None:
        raise SystemExit("det init 失败")
    for i in range(args.workers):
        root = ROOTS[i % 2] + "/live"
        rc, o, e = sh(f"systemd-run --quiet --unit ddw{i} --collect -p KillMode=process python3 /dev/shm/live_dd.py {i} {root}")
        if rc != 0:
            raise SystemExit(f"起 worker {i} 失败: {e}")
    sb.commands.run("python3 /home/user/user_dd.py", background=True, user="user")
    time.sleep(1.5)
    us = user_state()
    log(f"user 进程: {us}")
    if len(us["procs"]) != 1:
        raise SystemExit(f"user 进程数不是 1: {us}")
    upid = us["procs"][0][0]
    slab0 = slab()
    do_create(0)
    if not cps:
        raise SystemExit(f"第一个 checkpoint 就失败: {err_samples}")
    log(f"marks(第一个 checkpoint 和第一批删除之后): {ovl_marks()}")

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
            ops, errs, age = workers_state()
            us = user_state()
            log(f"轮 {r}: {stats}；worker ops {sum(ops.values())} errs {errs} 心跳 {age}；user n={us['n']} hb_age={us['hb_age']} procs={us['procs']} bad={us['bad'][:200]!r}；"
                f"checkpoint {len(cps)} 个；{ovl_marks()}；{out('df -m /overlay /var/lib/gsd/snapshots | tail -2 | tr -s \" \" | cut -d\" \" -f3-5 | tr \"\\n\" \" \"')}")
        if r % 50 == 0:
            save(f"{P}-dmesg-r{r}.txt", out("dmesg"))

    # ---- 收尾 ----
    time.sleep(1.2)
    ops, errs, age = workers_state()
    us = user_state()
    marks = ovl_marks()
    dm = out("dmesg")
    save(f"{P}-dmesg-final.txt", dm)
    hits = [l for l in dm.splitlines() if re.search(BAD_RE, l)]
    notes = [l for l in dm.splitlines() if re.search(NOTE_RE, l)]
    lock1 = lockdep()
    slab1 = slab()
    health = out("curl -s -m 5 http://127.0.0.1:49984/health")
    state = out("python3 - <<'EOF'\nimport json\nd=json.load(open('/var/lib/gsd/snapshots/state.json'))\nprint(json.dumps({'version': d.get('version'), 'checkpoints': len(d.get('checkpoints', [])), 'layers': len(d['overlay']['layers']), 'last_trees': len(d.get('last_trees', []))}))\nEOF")
    unexpected_errno = {k: v for k, v in errs.items() if k.split(":")[1] not in EXPECTED_ERRNO}
    stale_hb = {k: v for k, v in age.items() if v > 10}
    unexpected = {k: v for k, v in stats["errors"].items() if k != "create:resource_exhausted"}
    user_ok = (not us["bad"]) and len(us["procs"]) == 1 and us["procs"][0][0] == upid and not us["procs"][0][1].startswith("T") and us["hb_age"] < 10
    verdict = "PASS" if (stats["verify_fail"] == 0 and stats["mutate_fail"] == 0 and not hits and not notes and not unexpected and not unexpected_errno
                         and not stale_hb and len(age) == args.workers and user_ok
                         and "debug_locks: 0" not in lock1.replace("  ", " ") and '"ok"' in health) else "FAIL"
    summ = {"verdict": verdict, "sandbox": sb.sandbox_id, "template": args.template, "rounds": args.rounds, "workers": args.workers, "seed": args.seed,
            "stats": stats, "unexpected_errors": unexpected, "error_samples": err_samples[:20], "verify_fail_samples": fails[:20],
            "worker_ops": ops, "worker_ops_total": sum(ops.values()), "worker_errs": errs, "worker_unexpected_errno": unexpected_errno,
            "worker_heartbeat_age_s": age, "user": us, "user_ok": user_ok, "marks_end": marks,
            "dmesg_hits": hits[:40], "dmesg_notes": notes[:40], "dmesg_lines": len(dm.splitlines()),
            "lockdep_boot": lock0, "lockdep_end": lock1, "slab_boot": slab0, "slab_end": slab1,
            "slab_delta": {k: slab1.get(k, 0) - slab0.get(k, 0) for k in slab0}, "health": health, "state": state,
            "checkpoints_left": len(cps), "boot": boot, "elapsed_s": round(time.time() - T0, 1)}
    json.dump(summ, open(os.path.join(args.outdir, f"{P}-summary.json"), "w"), ensure_ascii=False, indent=1)
    log(f"结果 {verdict}: {stats}")
    log(f"  校验: ok {stats['verify_ok']} / fail {stats['verify_fail']}；det 变更失败 {stats['mutate_fail']}；gsd 意外错误 {unexpected}")
    log(f"  worker: 操作 {sum(ops.values())}，错误 {errs}，预期之外 {unexpected_errno}，心跳 {age}")
    log(f"  user: {us}")
    log(f"  {marks}；dmesg 关键字命中 {len(hits)} 行，whiteout 等提示 {len(notes)} 行；lockdep(结束): {lock1.splitlines()[0] if lock1 else ''}；health {health}")
    log(f"  slab {slab0} → {slab1}；state {state}")
    for s in fails[:10]:
        log("  FAIL样本: " + s[:600])
    for s in err_samples[:5]:
        log("  错误样本: " + s[:300])
    for s in (hits + notes)[:10]:
        log("  dmesg: " + s[:200])
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
