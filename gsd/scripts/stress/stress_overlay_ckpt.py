#!/usr/bin/env python3
"""overlay 热切换 + gsd checkpoint/restore 压测。发布版内核与调试内核（KASAN/PROVE_LOCKING/DEBUG_LIST/DEBUG_OBJECTS）上各跑一遍。

在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python stress_overlay_ckpt.py --template base-gsd --mode stress --rounds 300 --workers 6 --outdir <dir>
    python stress_overlay_ckpt.py --template base-gsd --mode enospc --outdir <dir>
通常经同目录的 run_stress_watch.sh 带看门狗跑（它负责传 --outdir）。--template 默认 base-gsd。
在调试内核上跑：先把 gsd 栈切到调试内核（GSD_KDIR=<调试内核目录> bash ../ops/switch-gsd-new920b.sh gsd，
内核编法见 ../kernel/README.md），用该内核另建一个模板（内存 4096 MB），--template 指向它。
判据：<outdir>/<mode>-summary.json；汇总与判定用同目录的 analyze_stress.py。lockdep 的 debug_locks 本脚本只记录不判，
调试内核上每组结束都要核对它仍为 1。

stress：
  - 沙箱里用 root 起 N 个 worker（systemd-run，不在 envd 的 cgroup 里，也不是 uid 1000，gsd 不 dump 也不杀）：
    对多层文件 stat/open/read；listdir 根目录和深层目录；在固定 cwd（深目录）里相对路径 cat/ls/touch；
    对早期层文件追加写（copy-up）；目录 rename。按 op:errno 计数，每秒把统计写到 /dev/shm（不随 overlay 回滚）。
  - 一个 uid 1000 的用户负载（envd 后台命令，一棵树），每轮都被 criu dump / restore。
  - 主循环 --rounds 轮：随机 create，或 restore 到随机的更早 checkpoint（40% 概率选最早的 3 个，
    让层栈交替变深变浅），偶尔 delete 一个旧 checkpoint（走回收路径）。
  - 每 50 轮抓一次完整 dmesg；结束时按关键字搜 dmesg、汇总 worker 错误分类和心跳、对比 slabinfo。
enospc：
  - 直接在 /overlay（upper 所在的 ext4）上写满，再逐步放出 0..N 个块，每一档做 create 和 restore，
    记录 gsd 的错误、沙箱是否可用、dmesg；删掉填充后 create/restore 恢复正常且内容正确。
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
ap.add_argument("--mode", choices=["stress", "enospc"], default="stress")
ap.add_argument("--rounds", type=int, default=300)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--seed", type=int, default=20260930)
ap.add_argument("--ops", default="stat,read,lsroot,lsdeep,relcat,rells,reltouch,append,rename,scandir",
                help="worker 的操作集合（逗号分隔），用来缩小触发内核问题的操作范围")
ap.add_argument("--outdir", default=".")
ap.add_argument("--premask-whiteout", action="store_true", help="先删一个 base 镜像里的文件（/etc/legal），让内核缓存的共享 whiteout 落在开机时的 workdir 里（永不删除），掩盖修复前的内核上的一个 BUG（restore 后删下层文件 → may_delete BUG），以便看其余路径；默认不开")
ap.add_argument("--pre-cmd", default=os.environ.get("STRESS_PRE_CMD", ""), help="沙箱起来后先用 root 跑一条命令（判别实验用，例如用一个 systemd-run 进程的 cwd 钉住开机时的 workdir）；默认取环境变量 STRESS_PRE_CMD，没有就不跑")
args = ap.parse_args()
random.seed(args.seed)
os.makedirs(args.outdir, exist_ok=True)

BAD_RE = (r"KASAN|BUG:|Oops|WARNING:|list_add corruption|list_del corruption|possible circular locking|"
          r"possible recursive locking|sleeping function called from invalid context|ODEBUG|"
          r"blocked for more than|hung_task|soft lockup|rcu.*stall|detected stalls|list corruption")
T0 = time.time()
LOGF = open(os.path.join(args.outdir, f"{args.mode}.log"), "a")


def log(msg):
    line = f"[{time.time() - T0:7.1f}s {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


OPF = open(os.path.join(args.outdir, f"{args.mode}-ops.log"), "a")


def oplog(msg):
    """每个 gsd 操作一行（宿主机墙钟，毫秒），用来和 orchestrator 日志里的 guest 控制台输出对时间。"""
    t = time.time()
    OPF.write(f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d} {msg}\n")
    OPF.flush()


def save(name, text):
    with open(os.path.join(args.outdir, name), "w") as f:
        f.write(text)


sb = Sandbox.create(template=args.template, timeout=3600)
log(f"sandbox {sb.sandbox_id} template={args.template} mode={args.mode}")
if args.premask_whiteout:
    try:
        log("premask-whiteout: " + sb.commands.run("rm -f /etc/legal; find /overlay/workspace -xdev -type c | head -3 | tr '\\n' ' '", user="root").stdout)
    except Exception as e:  # noqa: BLE001
        log(f"premask-whiteout 失败: {e}")
if args.pre_cmd:
    try:
        log("pre-cmd: " + sb.commands.run(args.pre_cmd, user="root").stdout.strip())
    except Exception as e:  # noqa: BLE001
        log(f"pre-cmd 失败: {e}")


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


def dmesg_hits(text):
    return [l for l in text.splitlines() if re.search(BAD_RE, l)]


def slab():
    res = {}
    for line in out("grep -E '^(mnt_cache|dentry|kmalloc-[0-9k]+) ' /proc/slabinfo").splitlines():
        p = line.split()
        res[p[0]] = int(p[1])  # active_objs
    return res


# ---- 开机信息 ----
boot = out("uname -a; zcat /proc/config.gz 2>/dev/null | grep -E '^CONFIG_(KASAN|KASAN_GENERIC|KASAN_INLINE|PROVE_LOCKING|DEBUG_LIST|DEBUG_OBJECTS|DEBUG_ATOMIC_SLEEP)=' ; "
           "dmesg | grep -i -E 'kasan|lockdep|Lock dependency' | head -5; "
           "cat /sys/kernel/mm/transparent_hugepage/enabled; free -m | sed -n 2p; df -m / /overlay /var/lib/gsd/snapshots | tail -3; "
           "/usr/bin/gsd -commit")
log("boot:\n" + boot)
def lockdep():
    """/proc/lockdep_stats 里的 debug_locks（1 = lockdep 仍在工作；文件不存在 = 内核没开 PROVE_LOCKING）。"""
    return out("grep -E 'debug_locks|dependency chains:' /proc/lockdep_stats 2>/dev/null || echo 'no /proc/lockdep_stats'")


LOCKDEP0 = lockdep()
log("lockdep(开机): " + LOCKDEP0.replace("\n", " | "))
dm0 = out("dmesg")
save(f"{args.mode}-dmesg-boot.txt", dm0)
pre_hits = dmesg_hits(dm0)
log(f"开机 dmesg 命中 {len(pre_hits)} 行" + ("：\n" + "\n".join(pre_hits[:20]) if pre_hits else ""))

WORKER = r'''
import os, sys, time, json, errno, random
k = int(sys.argv[1]); OPS = sys.argv[2].split(","); base = "/srv/st"; deep = base + "/d1/d2/d3/d4/d5/d6/d7/d8"
outp = "/dev/shm/st/w%d.json" % k
random.seed(k)
counts = {}; ops = 0; rechdir = 0; consec = 0
files = [base + "/early/e%d.txt" % i for i in range(20)] + [deep + "/f_deep.txt"] + \
        [base + "/mid%d/m%d.txt" % (j, i) for j in range(4) for i in range(5)]
def rec(op, e):
    key = "%s:%s" % (op, errno.errorcode.get(e.errno, str(e.errno)))
    counts[key] = counts.get(key, 0) + 1
ndiag = 0; rdir = ""
def diag(a, b):
    # rename 得到 ENOTEMPTY（a、b 同时存在且目标非空）时记下现场：overlay 视图、各层里的真实目录和 overlay xattr、最近的层栈
    import glob, subprocess
    L = ["t=%.3f k=%d rename %s" % (time.time(), k, rdir)]
    def view(tag):
        for p in (a, b):
            try: st = os.stat(p); L.append("%s %s ino=%d dev=%d ls=%s" % (tag, p, st.st_ino, st.st_dev, sorted(os.listdir(p))))
            except OSError as e: L.append("%s %s errno=%s" % (tag, p, errno.errorcode.get(e.errno, e.errno)))
    view("ovl")
    for layer in sorted(glob.glob("/overlay/layers/*")) + ["/mnt/lower"]:
        for p in (a, b):
            rp = layer + p
            try: st = os.lstat(rp)
            except OSError: continue
            xa = {}
            try:
                for x in os.listxattr(rp): xa[x] = os.getxattr(rp, x)[:40]
            except OSError as e: xa = {"err": e.errno}
            try: ls = sorted(os.listdir(rp))
            except OSError: ls = None
            L.append("real %s mode=%o ino=%d rdev=%d ls=%s xattr=%s" % (rp, st.st_mode, st.st_ino, st.st_rdev, ls, xa))
    try: L.append(subprocess.run("journalctl -b --no-pager -o cat | grep -E 'lowerdir|starting restore|checkpoint created|checkpoint deleted' | tail -6 | cut -c1-400", shell=True, capture_output=True, text=True, timeout=20).stdout)
    except Exception as e: L.append("journal: %r" % e)
    time.sleep(0.05); view("ovl+50ms")
    with open("/dev/shm/st/diag-w%d-%d.txt" % (k, ndiag), "w") as f: f.write("\n".join(L) + "\n")
try: os.chdir(deep)
except OSError as e: rec("chdir", e)
last = 0.0; per = {}
while True:
    op = random.choice(OPS)
    try:
        if op == "stat": os.stat(random.choice(files))
        elif op == "read":
            with open(random.choice(files), "rb") as f: f.read(8192)
        elif op == "lsroot": os.listdir("/")
        elif op == "lsdeep": os.listdir(deep); os.listdir(base + "/d1/d2/d3")
        elif op == "scandir":
            for d in os.scandir(base): d.stat(follow_symlinks=False)
        elif op == "relcat":
            with open("f_deep.txt", "rb") as f: f.read()
        elif op == "rells": os.listdir(".")
        elif op == "reltouch":
            with open("t_w%d" % k, "a") as f: f.write("t")
        elif op == "append":
            with open(base + "/early/e%d.txt" % random.randrange(20), "a") as f: f.write("x" * 64)
        elif op == "rename":
            a, b = base + "/rn/w%d_a" % k, base + "/rn/w%d_b" % k
            if os.path.exists(a): rdir = "a>b"; os.rename(a, b)
            else: rdir = "b>a"; os.rename(b, a)
        consec = 0 if op.startswith("rel") else consec
    except OSError as e:
        rec(op, e)
        if e.errno == errno.ENOTEMPTY and ndiag < 3:
            ndiag += 1
            if op == "rename":
                try: diag(a, b)
                except Exception as e3: open("/dev/shm/st/diag-w%d-%d.txt" % (k, ndiag), "w").write("diag failed: %r\n" % (e3,))
            else:   # 其他操作（例如 append 的 copy-up）拿到 ENOTEMPTY：记下时间和路径，便于和 gsd 操作时间线对照
                open("/dev/shm/st/diag-w%d-%d.txt" % (k, ndiag), "w").write("t=%.3f k=%d op=%s file=%r file2=%r cwd_ok=%s\n" % (time.time(), k, op, e.filename, e.filename2, os.path.exists(".")))
        if op.startswith("rel"):
            consec += 1
            if consec >= 50:          # cwd 已经失效（restore 把它所在的目录换掉了）：重新进入，计数
                try: os.chdir(deep); rechdir += 1
                except OSError as e2: rec("chdir", e2)
                consec = 0
    ops += 1; per[op] = per.get(op, 0) + 1
    now = time.time()
    if now - last >= 1:
        tmp = outp + ".tmp"
        with open(tmp, "w") as f: json.dump({"ts": now, "ops": ops, "errs": counts, "rechdir": rechdir, "per_op": per}, f)
        os.rename(tmp, outp); last = now
'''

SETUP = r'''
set -e
mkdir -p /srv/st/early /srv/st/rn /srv/st/d1/d2/d3/d4/d5/d6/d7/d8 /dev/shm/st
for i in $(seq 0 19); do echo "early $i" > /srv/st/early/e$i.txt; done
echo deep > /srv/st/d1/d2/d3/d4/d5/d6/d7/d8/f_deep.txt
for k in $(seq 1 16); do mkdir -p /srv/st/rn/w${k}_a; echo $k > /srv/st/rn/w${k}_a/x; done
cat > /dev/shm/st/worker.py <<'EOF'
%s
EOF
'''

USERLOAD = r'''
import os, time
buf = bytearray(48 << 20)
for i in range(0, len(buf), 4096): buf[i] = 1
n = 0
while True:
    n += 1
    for i in range(0, len(buf), 64 << 10): buf[i] = n & 255
    with open("/home/user/uload.txt", "a") as f: f.write("%d\n" % n)
    open("/home/user/uload.hb", "w").write(str(time.time()))
    time.sleep(0.2)
'''


def worker_stats():
    res = {}
    raw = out("for f in /dev/shm/st/w*.json; do echo \"$f $(cat $f)\"; done")
    for line in raw.splitlines():
        name, _, js = line.partition(" ")
        try:
            res[os.path.basename(name)] = json.loads(js)
        except ValueError:
            pass
    return res


def stress():
    rc, o, e = sh(SETUP % WORKER)
    if rc:
        raise SystemExit(f"setup 失败: {o} {e}")
    # 建几层"中间层"，让 worker 读的文件分布在不同的层里
    sb.files.write("/home/user/uload.py", USERLOAD)
    sb.commands.run("python3 /home/user/uload.py", background=True)
    time.sleep(1)
    cps = []
    for j in range(4):
        sh(f"mkdir -p /srv/st/mid{j}; for i in 0 1 2 3 4; do echo mid{j}-$i > /srv/st/mid{j}/m$i.txt; done")
        ok, c = call(sb.checkpoint.create, f"mid{j}")
        if not ok:
            raise SystemExit(f"建中间层失败: {c}")
        cps.append(c.checkpoint_id)
    log(f"中间层 checkpoint: {cps}")
    oplog("workers start")
    for k in range(1, args.workers + 1):
        sh(f"systemd-run --unit=stw{k} --property=Nice=5 python3 /dev/shm/st/worker.py {k} {args.ops}")
    time.sleep(2)
    log(f"workers({args.workers}, ops={args.ops}): " + out("systemctl is-active " + " ".join(f"stw{k}" for k in range(1, args.workers + 1)) + " | tr '\\n' ' '"))
    slab0 = slab()
    log(f"slab 起点: {slab0}")
    stats = {"create_ok": 0, "restore_ok": 0, "delete_ok": 0, "errors": {}}
    err_samples = []
    lat = {"create": [], "restore": []}
    for r in range(1, args.rounds + 1):
        x = random.random()
        if x < 0.45 or len(cps) < 2:
            sh(f"echo r{r} > /srv/st/round_{r % 7}.txt")
            t = time.time()
            oplog(f"r{r} create start")
            ok, c = call(sb.checkpoint.create, f"r{r}")
            oplog(f"r{r} create end ok={ok} {c.checkpoint_id if ok else str(c)[:200]}")
            if ok:
                cps.append(c.checkpoint_id)
                stats["create_ok"] += 1
                lat["create"].append((time.time() - t) * 1000)
            else:
                k = f"create:{ecode(c)}"
                stats["errors"][k] = stats["errors"].get(k, 0) + 1
                err_samples.append(f"r{r} {k} {str(c)[:300]}")
                if ecode(c) != "resource_exhausted" and stats["errors"][k] <= 3:
                    # 预期之外的 create 失败：留下完整的报错和 gsd 日志（criu 的日志尾部在里面），沙箱没了就查不到了
                    save(f"{args.mode}-create-fail-r{r}.txt", str(c) + "\n\n==== journalctl -u gsd (tail) ====\n"
                         + out("journalctl -u gsd --no-pager -o short-precise | tail -150") + "\n\n==== df ====\n"
                         + out("df -k / /overlay /var/lib/gsd/snapshots; dmesg | tail -5"))
                if ecode(c) == "resource_exhausted" and cps:
                    call(sb.checkpoint.restore, cps[0])
        elif x < 0.92:
            target = random.choice(cps[:3]) if random.random() < 0.4 else random.choice(cps)
            t = time.time()
            oplog(f"r{r} restore start {target} (第 {cps.index(target) + 1}/{len(cps)} 个)")
            ok, c = call(sb.checkpoint.restore, target)
            oplog(f"r{r} restore end ok={ok} {'' if ok else str(c)[:200]}")
            if ok:
                stats["restore_ok"] += 1
                lat["restore"].append((time.time() - t) * 1000)
            else:
                k = f"restore:{ecode(c)}"
                stats["errors"][k] = stats["errors"].get(k, 0) + 1
                err_samples.append(f"r{r} {k} {str(c)[:300]}")
        else:
            if len(cps) > 6:
                victim = random.choice(cps[3:-2])
                oplog(f"r{r} delete start {victim}")
                ok, c = call(sb.checkpoint.delete, victim)
                oplog(f"r{r} delete end ok={ok}")
                if ok:
                    cps.remove(victim)
                    stats["delete_ok"] += 1
                else:
                    k = f"delete:{ecode(c)}"
                    stats["errors"][k] = stats["errors"].get(k, 0) + 1
                    err_samples.append(f"r{r} {k} {str(c)[:300]}")
        if r % 50 == 0 or r == args.rounds:
            dm = out("dmesg")
            save(f"stress-dmesg-r{r:03d}.txt", dm)
            hits = [h for h in dmesg_hits(dm) if h not in pre_hits]
            ws = worker_stats()
            now = time.time()
            stale = {k: round(now - v["ts"], 1) for k, v in ws.items() if now - v["ts"] > 10}
            st = json.loads(out("cat /var/lib/gsd/snapshots/state.json") or "{}")
            depth = len((st.get("overlay") or {}).get("layers") or [])
            log(f"r{r}: {stats} ckpts={len(cps)} 当前层栈={depth} dmesg新命中={len(hits)} worker 心跳过期={stale} "
                f"health={out('curl -s -m 5 http://127.0.0.1:49984/health')} uload hb={out('cat /home/user/uload.hb 2>/dev/null || echo none')}")
            if hits:
                log("dmesg 命中:\n" + "\n".join(hits[:40]))
    time.sleep(2)
    ws = worker_stats()
    slab1 = slab()
    alive = out("systemctl is-active " + " ".join(f"stw{k}" for k in range(1, args.workers + 1)) + " | tr '\\n' ' '")
    sh("systemctl stop " + " ".join(f"stw{k}" for k in range(1, args.workers + 1)))
    dm = out("dmesg")
    save("stress-dmesg-final.txt", dm)
    hits = [h for h in dmesg_hits(dm) if h not in pre_hits]
    errs = {}
    tot_ops = 0
    for v in ws.values():
        tot_ops += v["ops"]
        for k2, n in v["errs"].items():
            errs[k2] = errs.get(k2, 0) + n
    by_errno = {}
    for k2, n in errs.items():
        en = k2.split(":")[1]
        by_errno[en] = by_errno.get(en, 0) + n
    now = time.time()
    dg = out("for f in /dev/shm/st/diag-*.txt; do [ -f $f ] && { echo \"== $f\"; cat $f; }; done 2>/dev/null")
    if dg:
        save(f"{args.mode}-enotempty-diag.txt", dg)
    summary = {
        "rounds": args.rounds, "workers": args.workers, "gsd_ops": stats, "ckpts_left": len(cps),
        "lat_ms_p50": {k2: (sorted(v)[len(v) // 2] if v else None) for k2, v in lat.items()},
        "error_samples": err_samples[:30],
        "worker_ops_total": tot_ops, "worker_errs_by_op": errs, "worker_errs_by_errno": by_errno,
        "worker_rechdir": {k2: v.get("rechdir") for k2, v in ws.items()},
        "worker_heartbeat_age_s": {k2: round(now - v["ts"], 1) for k2, v in ws.items()},
        "workers_active_before_stop": alive,
        "slab_start": slab0, "slab_end": slab1,
        "slab_delta": {k2: slab1.get(k2, 0) - slab0.get(k2, 0) for k2 in sorted(set(slab0) | set(slab1))},
        "dmesg_new_hits": hits, "dmesg_boot_hits": pre_hits,
        "user_load_hb_age_s": None,
        "health": out("curl -s -m 5 http://127.0.0.1:49984/health"),
        "lockdep_boot": LOCKDEP0, "lockdep_end": lockdep(),
    }
    try:
        summary["user_load_hb_age_s"] = round(now - float(out("cat /home/user/uload.hb")), 1)
    except ValueError:
        pass
    summary["user_procs"] = out("ps -u 1000 -o pid=,stat=,args=")
    save("stress-summary.json", json.dumps(summary, ensure_ascii=False, indent=1))
    log("汇总:\n" + json.dumps(summary, ensure_ascii=False, indent=1))
    bad_errno = {k2: n for k2, n in by_errno.items() if k2 not in ("ENOENT", "ESTALE")}
    stale = {k2: v for k2, v in summary["worker_heartbeat_age_s"].items() if v > 10}
    log(f"判定: dmesg 新命中 {len(hits)}；非 ENOENT/ESTALE 的 worker 错误 {bad_errno}；心跳过期 {stale}；gsd 错误 {stats['errors']}")


def enospc():
    res = []
    start = sb.checkpoint.create("base")
    sh("echo v0 > /home/user/en.txt", user="user")
    c0 = sb.checkpoint.create("c0")
    log(f"base={start.checkpoint_id} c0={c0.checkpoint_id}")
    log(out("df -k /overlay / | tail -2; mount | grep -E ' /overlay | / '"))

    def fill():
        # 先用大文件吃掉大部分，再用 4 KiB 小文件把剩下的块一个个吃光
        avail = int(out("df -k --output=avail /overlay | tail -1"))
        if avail > 8192:
            sh(f"fallocate -l {(avail - 4096) * 1024} /overlay/fill.big.$(date +%s%N)")
        n = 0
        while n < 5000:
            rc, _, _ = sh(f"dd if=/dev/zero of=/overlay/fill.s.$(date +%s%N) bs=4k count=1 status=none conv=fsync 2>/dev/null")
            if rc:
                break
            n += 1
        return n

    for gap in range(0, 7):
        fill()
        sh(f"ls -1 /overlay/fill.s.* 2>/dev/null | head -{gap} | xargs -r rm -f; sync") if gap else sh("sync")
        free = out("df -k --output=avail,iavail /overlay | tail -1")
        w = sh("echo v1 > /home/user/en.txt", user="user")
        t = time.time()
        okc, c = call(sb.checkpoint.create, f"gap{gap}")
        cmsg = c.checkpoint_id if okc else f"{ecode(c)}: {str(c)[:400]}"
        okr, r = call(sb.checkpoint.restore, c0.checkpoint_id)
        rmsg = "ok" if okr else f"{ecode(r)}: {str(r)[:400]}"
        usable = out("echo alive; cat /home/user/en.txt; curl -s -m 5 http://127.0.0.1:49984/health")
        ls = out("ls -A /overlay/layers | tr '\\n' ' '")
        row = {"gap_blocks": gap, "free_kb_inodes": free, "write_rc": w[0], "create": cmsg, "restore_c0": rmsg,
               "usable": usable, "layers_dir": ls, "secs": round(time.time() - t, 2)}
        res.append(row)
        log(json.dumps(row, ensure_ascii=False))
        if okc:
            call(sb.checkpoint.delete, c.checkpoint_id)
    log("gsd 错误日志:\n" + out("journalctl -u gsd --no-pager -o cat | grep -E '\"level\":\"(error|warn)\"' | tail -20"))
    sh("rm -f /overlay/fill.*; sync")
    log("删掉填充后: " + out("df -k /overlay | tail -1"))
    sh("echo v2 > /home/user/en.txt", user="user")
    ok1, c2 = call(sb.checkpoint.create, "after")
    ok2, _ = call(sb.checkpoint.restore, c0.checkpoint_id)
    v_c0 = out("cat /home/user/en.txt")
    ok3, _ = call(sb.checkpoint.restore, c2.checkpoint_id) if ok1 else (False, None)
    v_c2 = out("cat /home/user/en.txt")
    rec = {"create_after": ok1 and c2.checkpoint_id, "restore_c0": ok2, "c0_content": v_c0, "restore_after": ok3, "after_content": v_c2,
           "health": out("curl -s -m 5 http://127.0.0.1:49984/health"),
           "layers_dir": out("ls -A /overlay/layers | tr '\\n' ' '")}
    log("恢复: " + json.dumps(rec, ensure_ascii=False))
    dm = out("dmesg")
    save("enospc-dmesg-final.txt", dm)
    hits = [h for h in dmesg_hits(dm) if h not in pre_hits]
    ext4 = [l for l in dm.splitlines() if re.search(r"EXT4-fs (error|warning)|overlayfs:", l)]
    save("enospc-summary.json", json.dumps({"gaps": res, "recovery": rec, "dmesg_new_hits": hits, "dmesg_ext4_overlay": ext4[-30:],
                                            "lockdep_boot": LOCKDEP0, "lockdep_end": lockdep()},
                                           ensure_ascii=False, indent=1))
    log(f"判定: dmesg 新命中 {len(hits)}；ext4/overlayfs 行 {len(ext4)}；恢复后 c0 内容 {v_c0!r}（应为 v0）、after 内容 {v_c2!r}（应为 v2）")
    if hits:
        log("\n".join(hits[:40]))


try:
    stress() if args.mode == "stress" else enospc()
except BaseException as e:  # noqa: BLE001
    import traceback
    log(f"异常退出: {type(e).__name__}: {str(e)[:500]}\n{traceback.format_exc()[-2000:]}")
    raise
finally:
    try:
        sb.kill()
    except Exception:  # noqa: BLE001
        pass
    log(f"sandbox {sb.sandbox_id} killed")
