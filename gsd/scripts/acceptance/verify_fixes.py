#!/usr/bin/env python3
"""gsd 问题清单的逐条现场验证（代码基准 KASandbox_0904 deltabox-gsd@259f1a25c）。

问题清单（G-xx 编号）与各项的处理见手册 14 章；凡是要在真实 guest 上才能确认的修复，这里各有一项，共 28 项。
每项独立起沙箱（用完即 kill），输出 PASS / FAIL / SKIP / INFO 和证据，最后汇总成表（stdout + --out-json + --out-md）。

在宿主机上跑（gsd 栈在运行；解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python verify_fixes.py [--template base-gsd]
        [--only G-01,G-02] [--skip G-08] [--out-json x.json] [--out-md x.md]

判据：汇总表 28 行，全部通过的样子是 PASS 26 / FAIL 0 / SKIP 2，末行"合计 28 项，FAIL 0: []"。
恒为 SKIP 的两项：G-19（HOST_GSD_PATH 关闭态——这个开关在 orchestrator 启动时读一次，要现场看到关闭态必须改
orchestrator 的启动环境并重启它，会影响机器上的其他使用者；依据是 orchestrator 单测与读代码）、G-36（SDK 的 URL 选择，由 SDK 单测覆盖）。
SKIP 不是 2，或者出现 INFO（G-08 在 --g08-max 次内没碰到层栈上限时记 INFO），都要看原因。有 FAIL 时退出码非 0。

依赖：带 checkpoint 接口的 SDK（CheckpointInfo 带 timings/has_process_image/...；Sandbox._envd_access_token）。
guest 里直接 curl 127.0.0.1:49984 的调用都带 X-Access-Token（secure 沙箱必须带）。
acceptance/verify_multitree.py 复用本文件里的沙箱封装，两个文件要放在同一个目录下。
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env  # noqa: E402

os.environ.update(load_e2b_env())
from e2b import Sandbox, CommandExitException  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--template", default="base-gsd")
ap.add_argument("--only", default="", help="逗号分隔的项目 ID，只跑这些")
ap.add_argument("--skip", default="", help="逗号分隔的项目 ID，跳过这些")
ap.add_argument("--g08-max", type=int, default=260, help="G-08 连续 create 的上限次数")
ap.add_argument("--g11-loops", type=int, default=40)
ap.add_argument("--out-json", default="")
ap.add_argument("--out-md", default="")
ap.add_argument("--premask-whiteout", action="store_true", help="先删一个 base 镜像里的文件（/etc/legal），让内核缓存的共享 whiteout 落在开机时的 workdir 里（永不删除），掩盖修复前的内核上的一个 BUG（restore 后删下层文件 → may_delete BUG），以便看其余路径；默认不开")
args = ap.parse_args()

SNAP = "/var/lib/gsd/snapshots"
LAYERS = "/overlay/layers"
RESULTS = []
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


def short(x, n=600):
    s = str(x)
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n})"


class G:
    """一个沙箱加上常用的 guest 操作。"""

    def __init__(self, secure=True, tpl=None):
        self.sb = Sandbox.create(template=tpl or args.template, timeout=1800, secure=secure)
        self.id = self.sb.sandbox_id
        self.tok = getattr(self.sb, "_envd_access_token", None)
        log(f"  sandbox {self.id} (secure={secure})")
        if args.premask_whiteout:
            log("  premask-whiteout: " + self.out("rm -f /etc/legal; find /overlay/workspace -xdev -type c | head -3 | tr '\\n' ' '"))

    # ---- 命令 ----
    def sh(self, cmd, user="root", timeout=120):
        try:
            r = self.sb.commands.run(cmd, user=user, timeout=timeout)
            return 0, r.stdout, r.stderr
        except CommandExitException as e:
            return e.exit_code, e.stdout, e.stderr

    def out(self, cmd, user="root", timeout=120):
        return self.sh(cmd, user, timeout)[1].strip()

    def bg(self, cmd, user="user"):
        return self.sb.commands.run(cmd, background=True, user=user)

    def write(self, path, data, user="user"):
        self.sb.files.write(path, data, user=user)

    # ---- gsd ----
    def curl(self, path, body=None, token="__own__", extra=""):
        """guest 里 curl gsd，返回 (http_code, body)。token=None 不带头，"__own__" 用本沙箱的。"""
        tok = self.tok if token == "__own__" else token
        h = f"-H 'X-Access-Token: {tok}'" if tok else ""
        d = f"-H 'Content-Type: application/json' -d '{body}'" if body is not None else ""
        o = self.out(f"curl -s -m 30 {h} {d} {extra} -w '\\n%{{http_code}}' http://127.0.0.1:49984{path}")
        lines = o.rsplit("\n", 1)
        return (lines[-1], lines[0] if len(lines) > 1 else "")

    def rpc(self, method, body="{}", token="__own__"):
        return self.curl(f"/checkpoint.Checkpoint/{method}", body, token)

    def health(self):
        return self.out("curl -s -m 5 http://127.0.0.1:49984/health")

    def wait_health(self, want="ok", tries=100):
        h = ""
        for _ in range(tries):
            h = self.out("curl -s -m 2 http://127.0.0.1:49984/health || true")
            if f'"status":"{want}"' in h.replace(" ", ""):
                return h
            time.sleep(0.2)
        return h

    def create(self, name=None):
        try:
            return True, self.sb.checkpoint.create(name=name)
        except Exception as e:  # noqa: BLE001
            return False, e

    def restore(self, cid):
        try:
            return True, self.sb.checkpoint.restore(cid)
        except Exception as e:  # noqa: BLE001
            return False, e

    def delete(self, cid):
        try:
            return True, self.sb.checkpoint.delete(cid)
        except Exception as e:  # noqa: BLE001
            return False, e

    def journal(self, grep, n=50):
        return self.out(f"journalctl -u gsd --no-pager -o cat | grep -F -- '{grep}' | tail -{n} || true")

    def jlines(self, grep, n=200):
        res = []
        for line in self.journal(grep, n).splitlines():
            try:
                res.append(json.loads(line))
            except ValueError:
                pass
        return res

    def state(self):
        try:
            return json.loads(self.out(f"cat {SNAP}/state.json"))
        except ValueError:
            return {}

    # ---- 进程 ----
    PROCS = r'''for d in /proc/[0-9]*; do u=$(awk '/^Uid:/{print $2}' $d/status 2>/dev/null); [ "$u" = 1000 ] || continue; s=$(cat $d/stat 2>/dev/null) || continue; r=${s##*) }; set -- $r; echo "${d#/proc/}|$2|$1|$(tr '\0\n' '  ' < $d/cmdline)|$(cat $d/comm)"; done'''

    def procs(self):
        """uid 1000 进程：[{pid, ppid, state, args, comm}]"""
        res = []
        for line in self.out(self.PROCS).splitlines():
            p = line.split("|", 4)
            if len(p) == 5:
                res.append({"pid": int(p[0]), "ppid": int(p[1]), "state": p[2], "args": p[3].strip(), "comm": p[4]})
        return sorted(res, key=lambda x: x["pid"])

    def wait_file(self, path, want=None, tries=150):
        v = ""
        for _ in range(tries):
            v = self.out(f"cat {path} 2>/dev/null || true")
            if v and (want is None or want in v):
                return v
            time.sleep(0.1)
        return v

    def dmesg_bad(self):
        return self.out("dmesg | grep -E 'KASAN|BUG:|Oops|WARNING:|list_add corruption|list_del corruption|"
                        "possible circular locking|possible recursive locking|sleeping function called|ODEBUG|"
                        "Out of memory|oom-kill' || true")

    def kill(self):
        try:
            self.sb.kill()
        except Exception as e:  # noqa: BLE001
            log(f"  kill {self.id} failed: {e}")


def ecode(e):
    """SDK 异常 → (类型名, gsd/connect code 的猜测, 信息)"""
    s = str(e)
    # 旧 SDK：没单独映射的码是 SandboxException("Code.<code>: ...")，resource_exhausted 是 RateLimitException。
    # 新 SDK（checkpoint 专用映射）：failed_precondition 信息里带 "(failed_precondition)"，
    # resource_exhausted → NotEnoughSpaceException，data_loss → CheckpointRestoreException。
    m = re.search(r"Code\.(\w+)", s) or re.search(r"\((failed_precondition)\)", s)
    code = m.group(1) if m else {"RateLimitException": "resource_exhausted", "NotEnoughSpaceException": "resource_exhausted",
                                 "CheckpointRestoreException": "data_loss", "TimeoutException": "deadline_exceeded",
                                 "NotFoundException": "not_found", "AuthenticationException": "unauthenticated"
                                 }.get(type(e).__name__, "?")
    return type(e).__name__, code, s


class Item:
    def __init__(self, iid, title):
        self.iid, self.title = iid, title
        self.status, self.ev = "PASS", []
        self.sbs = []

    def sandbox(self, **kw):
        g = G(**kw)
        self.sbs.append(g)
        return g

    def e(self, msg):
        self.ev.append(str(msg))
        log(f"    {short(msg, 400)}")

    def check(self, cond, msg):
        self.e(("ok  " if cond else "BAD ") + str(msg))
        if not cond:
            self.status = "FAIL"
        return cond


ITEMS = []


def item(iid, title):
    def deco(fn):
        ITEMS.append((iid, title, fn))
        return fn
    return deco


def selected(iid):
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    skip = [x.strip() for x in args.skip.split(",") if x.strip()]
    return (not only or iid in only) and iid not in skip


# ============================== guest 负载 ==============================

TREE_SH = r'''#!/bin/bash
sleep 10001 &
python3 -c '
import os, time
if os.fork() == 0:
    time.sleep(10002)
else:
    time.sleep(10003)
' &
wait
'''

# 常驻进程：按 cmd 文件里的序号执行一次动作（避免每轮 mmap，脏页只来自 buf 本身）
HOG_PY = r'''
import os, sys, time
mb = int(sys.argv[1]); mode = sys.argv[2]; tag = sys.argv[3]
size = mb << 20; buf = bytearray(size)
for i in range(0, size, 4096): buf[i] = 1
ack = "/home/user/%s.ack" % tag; cmdf = "/home/user/%s.cmd" % tag
open(ack, "w").write("ready %d" % os.getpid()); done = 0
chunk = bytearray(1 << 20)
while True:
    try: n = int(open(cmdf).read().strip() or 0)
    except Exception: n = 0
    if n > done:
        if mode == "scatter":          # 96 处零散的单字节写
            for k in range(96): buf[(k * 2731 * 1024) % size] = (n + k) & 255
        else:                          # 整块改写
            for j in range(0, len(chunk), 4096): chunk[j] = (n * 7 + j) & 255
            for i in range(0, size, 1 << 20): buf[i:i + (1 << 20)] = chunk
        done = n
        open(ack, "w").write("done %d" % n)
    time.sleep(0.05)
'''

APPENDER_PY = r'''
import os, time
f = open("/home/user/g25fd.txt", "a")
f.write("p1\n"); f.flush()
open("/home/user/g25fd.ack", "w").write("ready %d" % os.getpid()); done = 0
while True:
    try: c = open("/home/user/g25fd.cmd").read().strip()
    except Exception: c = ""
    if c and c != "%d" % done:
        n = int(c.split()[0]); f.write(c.split()[1] + "\n"); f.flush(); done = n
        open("/home/user/g25fd.ack", "w").write("done %d" % n)
    time.sleep(0.05)
'''

UMASK_PY = r'''
import os, socket, select
d = "/home/user/um"; os.makedirs(d, exist_ok=True)
a, b = socket.socketpair()
p = d + "/srv.sock"
try: os.unlink(p)
except OSError: pass
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(p); srv.listen(4)
f = open(d + "/deleted.txt", "w+"); f.write("ghost-content"); f.flush(); os.unlink(d + "/deleted.txt")
open(d + "/ready", "w").write(str(os.getpid()))
while True:
    r, _, _ = select.select([srv], [], [], 0.2)
    if r:
        c, _ = srv.accept(); msg = c.recv(100)
        a.send(msg); got = b.recv(100)
        f.seek(0); ghost = f.read()
        c.send(got + b"|" + ghost.encode()); c.close()
'''

UMASK_CLIENT = ("python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(5); "
                "s.connect('/home/user/um/srv.sock'); s.send(b'ping'); print(s.recv(200).decode())\"")


def start_hog(g, mb, mode, tag):
    g.write(f"/home/user/{tag}.py", HOG_PY)
    g.bg(f"python3 /home/user/{tag}.py {mb} {mode} {tag}")
    v = g.wait_file(f"/home/user/{tag}.ack", "ready", tries=300)
    return int(v.split()[1]) if v.startswith("ready") else None


def hog_cmd(g, tag, n):
    g.sh(f"echo {n} > /home/user/{tag}.cmd", user="user")
    return g.wait_file(f"/home/user/{tag}.ack", f"done {n}", tries=300)


def dump_roots(g, cid):
    """某次 create 的 dump 根：新 gsd 的日志字段是 dump_root_pids（数组），旧的是 dump_root_pid。"""
    res = []
    for j in g.jlines(cid):
        if j.get("message") != "starting checkpoint":
            continue
        if "dump_root_pids" in j:
            res += list(j["dump_root_pids"])
        elif "dump_root_pid" in j:
            res.append(j["dump_root_pid"])
    return res


def start_sleeper(g, secs=100000):
    g.bg(f"sleep {secs}")
    for _ in range(50):
        ps = [p for p in g.procs() if p["args"] == f"sleep {secs}"]
        if ps:
            return ps[0]["pid"]
        time.sleep(0.1)
    return None


# ============================== 各项 ==============================

@item("G-01", "带子进程的负载 create → restore → restore；restore 后没有 checkpoint 之后的进程")
def t_g01(it):
    g = it.sandbox()
    g.write("/home/user/tree.sh", TREE_SH)
    g.bg("bash /home/user/tree.sh")
    for _ in range(50):
        p0 = g.procs()
        if len(p0) >= 4:
            break
        time.sleep(0.2)
    it.e(f"checkpoint 前 uid1000 进程: {[(p['pid'], p['ppid'], p['state'], p['args']) for p in p0]}")
    it.check(len(p0) >= 4, f"进程树有 {len(p0)} 个进程（期望 ≥4：bash + sleep + python 父子）")
    ok, cp = g.create("g01")
    if not it.check(ok and cp.has_process_image, f"create: {short(cp) if not ok else cp.checkpoint_id} has_process_image={getattr(cp, 'has_process_image', None)}"):
        return
    g.bg("sleep 20001")
    g.sh("nohup sleep 20002 >/dev/null 2>&1 &", user="user")
    time.sleep(1)
    it.e(f"checkpoint 之后新起: {[(p['pid'], p['args']) for p in g.procs() if p['args'].startswith('sleep 2000')]}")
    base = sorted((p["pid"], p["args"]) for p in p0)
    for k in (1, 2):
        ok, r = g.restore(cp.checkpoint_id)
        it.check(ok and r is True, f"restore #{k}: {r if ok else short(r)}  timings={g.sb.checkpoint.last_timings}")
        time.sleep(0.5)
        pk = g.procs()
        now = sorted((p["pid"], p["args"]) for p in pk)
        it.check(now == base, f"restore #{k} 后进程集合与 checkpoint 时相同: {now}")
        it.check(not [p for p in pk if p["args"].startswith("sleep 2000")], f"restore #{k} 后没有 sleep 2000x")
    # G-26 顺带：恢复出来的根进程 PPid
    roots = [p for p in g.procs() if p["args"].startswith("bash /home/user/tree.sh")]
    if roots:
        pp = g.out(f"grep PPid /proc/{roots[0]['pid']}/status")
        it.e(f"(G-26) 恢复后根 bash pid={roots[0]['pid']} {pp}")


@item("G-02", "两个后台命令时 create 保存两棵树、restore 两棵都回来（4fb04408b 起不再返回 FailedPrecondition）")
def t_g02(it):
    g = it.sandbox()
    a = start_sleeper(g, 30001)
    b = start_sleeper(g, 30002)
    it.e(f"两个根: {a}, {b}; 进程 {[(p['pid'], p['ppid'], p['args']) for p in g.procs()]}")
    ok, c = g.create("g02")
    if not it.check(ok, f"create 应成功（旧预期是 FailedPrecondition），实际 ok={ok} {short(c) if not ok else c.checkpoint_id}"):
        return
    it.check(getattr(c, "process_trees", None) == 2 and c.has_process_image, f"process_trees={getattr(c, 'process_trees', None)} has_process_image={c.has_process_image} image_bytes={c.image_bytes}")
    roots = [j.get("dump_root_pids") for j in g.jlines(c.checkpoint_id) if "dump_root_pids" in j]
    it.check(roots and sorted(roots[0]) == sorted([a, b]), f"gsd 日志 dump_root_pids={roots}，应为 {sorted([a, b])}")
    ps = g.procs()
    it.check(all(p["state"] != "T" for p in ps) and len(ps) == 2, f"create 后两个进程仍在且未停: {[(p['pid'], p['state']) for p in ps]}")
    g.sh(f"kill -9 {a}")
    g.bg("sleep 30003")
    time.sleep(0.5)
    it.e(f"restore 前（杀掉 {a}、另起 sleep 30003）: {[(p['pid'], p['args']) for p in g.procs()]}")
    ok, r = g.restore(c.checkpoint_id)
    time.sleep(0.3)
    ps = g.procs()
    it.check(ok and sorted(p["pid"] for p in ps) == sorted([a, b]) and all(p["state"] != "T" and p["ppid"] == 1 for p in ps),
             f"restore 后两棵树都回来、PPid 都是 1、没有 checkpoint 之后的进程: {[(p['pid'], p['ppid'], p['state'], p['args']) for p in ps]}")
    ls = g.sb.checkpoint.list()
    it.check(len(ls) == 1 and getattr(ls[0], "process_trees", None) == 2, f"list: {[(x.checkpoint_id, getattr(x, 'process_trees', None)) for x in ls]}")


@item("G-03", "create 后结束第一个用户进程、另起一个，再 create 成功")
def t_g03(it):
    g = it.sandbox()
    a = start_sleeper(g, 40001)
    ok, c1 = g.create("g03a")
    it.check(ok, f"create #1: {c1.checkpoint_id if ok else short(c1)}")
    g.sh(f"kill -9 {a}")
    time.sleep(0.3)
    b = start_sleeper(g, 40002)
    it.e(f"第一个 pid={a} 已杀，新进程 pid={b}")
    ok, c2 = g.create("g03b")
    if not it.check(ok and c2.has_process_image, f"create #2: {c2.checkpoint_id if ok else short(c2)}"):
        return
    root = dump_roots(g, c2.checkpoint_id)
    it.check(root == [b], f"gsd 日志 dump_root_pids={root}，应只有新进程 {b}")
    ok, r = g.restore(c2.checkpoint_id)
    it.check(ok and any(p["pid"] == b for p in g.procs()), f"restore #2 后新进程 {b} 在: {[(p['pid'], p['args']) for p in g.procs()]}")


@item("G-04", "comm 带空格/括号的子进程，create 后状态不是 T")
def t_g04(it):
    g = it.sandbox()
    g.write("/home/user/g04.py", r'''
import ctypes, os, time
libc = ctypes.CDLL(None)
def name(n): libc.prctl(15, ctypes.c_char_p(n), 0, 0, 0)
name(b"a) b c")
if os.fork() == 0:
    name(b"kid (x) y")
    while True: time.sleep(0.2)
while True: time.sleep(0.2)
''')
    g.bg("python3 /home/user/g04.py")
    time.sleep(1.5)
    p0 = g.procs()
    it.e(f"进程: {[(p['pid'], p['ppid'], p['state'], p['comm']) for p in p0]}")
    it.check(any(" " in p["comm"] for p in p0), "有 comm 带空格的进程")
    ok, c = g.create("g04")
    it.check(ok and c.has_process_image, f"create: {c.checkpoint_id if ok else short(c)}")
    time.sleep(0.5)
    p1 = g.procs()
    it.check(len(p1) == len(p0) and all(p["state"] != "T" for p in p1), f"create 后状态: {[(p['pid'], p['state'], p['comm']) for p in p1]}")


@item("G-27", "被 kill -STOP 的进程 create 后仍是 T，其余照常运行")
def t_g27(it):
    g = it.sandbox()
    g.bg("bash -c 'sleep 50001 & sleep 50002 & wait'")
    time.sleep(1)
    ps = g.procs()
    bash = [p["pid"] for p in ps if p["args"].startswith("bash -c")]
    s1 = [p["pid"] for p in ps if p["args"] == "sleep 50001"]
    s2 = [p["pid"] for p in ps if p["args"] == "sleep 50002"]
    if not it.check(bash and s1 and s2, f"进程: {[(p['pid'], p['args']) for p in ps]}"):
        return
    g.sh(f"kill -STOP {s1[0]} {bash[0]}")
    time.sleep(0.3)
    it.e(f"STOP 后: {[(p['pid'], p['state'], p['args']) for p in g.procs()]}")
    ok, c = g.create("g27")
    it.check(ok, f"create: {c.checkpoint_id if ok else short(c)}")
    time.sleep(0.5)
    st = {p["pid"]: p["state"] for p in g.procs()}
    it.check(st.get(s1[0]) == "T" and st.get(bash[0]) == "T", f"原本停着的 bash {bash[0]}={st.get(bash[0])}、sleep {s1[0]}={st.get(s1[0])} 仍是 T")
    it.check(st.get(s2[0]) in ("S", "R"), f"原本在跑的 sleep {s2[0]}={st.get(s2[0])} 不是 T")
    rs = [j for j in g.jlines(c.checkpoint_id) if "resumed_pids" in j]
    it.e(f"gsd 日志: stopped={rs[-1].get('stopped_pids') if rs else None} resumed={rs[-1].get('resumed_pids') if rs else None}")


@item("G-26", "restore 后根进程 PPid 是 1")
def t_g26(it):
    g = it.sandbox()
    pid = start_sleeper(g, 60001)
    it.e(f"restore 前 sleep pid={pid} {g.out(f'grep PPid /proc/{pid}/status')}")
    ok, c = g.create("g26")
    ok2, r = g.restore(c.checkpoint_id) if ok else (False, c)
    it.check(ok and ok2, f"create/restore: {r if ok2 else short(r)}")
    pp = g.out(f"grep PPid /proc/{pid}/status || echo gone")
    it.check(pp.split()[-1] == "1", f"restore 后 pid={pid} {pp}")


@item("G-06", "guest 开机 THP=[never]，dmesg 有 overlay-init 记录，零散写的增量镜像在 MiB 量级")
def t_g06(it):
    g = it.sandbox()
    thp = g.out("cat /sys/kernel/mm/transparent_hugepage/enabled")
    it.check("[never]" in thp, f"THP: {thp}")
    oi = g.out("dmesg | grep overlay-init")
    it.check("transparent_hugepage=never" in oi, "dmesg overlay-init:\n" + oi)
    it.e("gsd 日志: " + g.journal("transparent_hugepage", 5))
    # 不做 echo never，直接零散写（fix-plan §1 的负载）
    pid = start_hog(g, 256, "scatter", "g06")
    ok, a = g.create("g06a")
    it.check(ok, f"cpA: {a.checkpoint_id if ok else short(a)} image_bytes={getattr(a, 'image_bytes', None)}")
    hog_cmd(g, "g06", 1)
    ok, b = g.create("g06b")
    if not it.check(ok, f"cpB: {b.checkpoint_id if ok else short(b)}"):
        return
    it.check(b.parent_checkpoint_id == a.checkpoint_id, f"cpB parent={b.parent_checkpoint_id}（增量）")
    ab = int(g.out(f"du -sb {SNAP}/{a.checkpoint_id}_mem | cut -f1"))
    bb = int(g.out(f"du -sb {SNAP}/{b.checkpoint_id}_mem | cut -f1"))
    pages = g.out(f"ls -la {SNAP}/{b.checkpoint_id}_mem/ {SNAP}/{b.checkpoint_id}_mem/t0/ 2>/dev/null | grep pages")
    it.check(b.image_bytes < 16 << 20, f"cpB image_bytes={b.image_bytes} ({b.image_bytes / 2**20:.2f} MiB, du -sb={bb})；cpA={a.image_bytes} (du -sb={ab})；hog pid={pid}\n{pages}")
    ahp = g.out(f"grep AnonHugePages /proc/{pid}/smaps_rollup")
    it.e(f"hog {ahp}")


@item("G-18", "guest 里 criu 是源码装的 4.2.1，没有发行版 criu 包")
def t_g18(it):
    g = it.sandbox()
    v = g.out("criu --version; ls -la /usr/sbin/criu; which -a criu")
    it.check("Version: 4.2.1" in v, v)
    rc, o, e = g.sh("dpkg -s criu")
    it.check(rc != 0, f"dpkg -s criu rc={rc}: {short((o + e).strip(), 200)}")


@item("G-10", "create A → create B → delete A → restore B；delete B 后 A、B 镜像目录都消失")
def t_g10(it):
    g = it.sandbox()
    pid = start_sleeper(g, 70001)
    ok, a = g.create("A")
    g.sh("echo b > /home/user/g10b", user="user")
    ok2, b = g.create("B")
    if not it.check(ok and ok2, f"A={getattr(a, 'checkpoint_id', a)} B={getattr(b, 'checkpoint_id', b)}"):
        return
    it.check(b.parent_checkpoint_id == a.checkpoint_id, f"B parent={b.parent_checkpoint_id}")
    ok, r = g.delete(a.checkpoint_id)
    it.check(ok and r, f"delete A: {r if ok else short(r)}")
    ids = [c.checkpoint_id for c in g.sb.checkpoint.list()]
    it.check(ids == [b.checkpoint_id], f"list 后只剩 B: {ids}")
    it.check(g.sh(f"test -d {SNAP}/{a.checkpoint_id}_mem")[0] == 0, "A 的镜像目录保留（B 依赖它）")
    ok, e = g.restore(a.checkpoint_id)
    it.check(not ok and ecode(e)[1] == "not_found", f"restore A（已隐藏）: {ecode(e)[:2] if not ok else 'ok?'}")
    ok, r = g.restore(b.checkpoint_id)
    it.check(ok and r and pid in [p["pid"] for p in g.procs()], f"restore B 成功，进程 {pid} 在；timings={g.sb.checkpoint.last_timings}")
    ok, r = g.delete(b.checkpoint_id)
    it.check(ok and r, f"delete B: {r if ok else short(r)}")
    time.sleep(1)
    ls = g.out(f"ls -a {SNAP}; ls -a {SNAP}/.trash 2>/dev/null || true")
    it.check(a.checkpoint_id not in ls and b.checkpoint_id not in ls, f"snapshots 下: {ls}")
    it.e("gsd 日志: " + g.journal("checkpoint deleted", 3) + " | " + g.journal("checkpoint hidden", 3))


LAYERS_STAT = (f"cd {LAYERS}; echo n_L=$(ls -d L* 2>/dev/null | wc -l) n_restore=$(ls -d restore_* 2>/dev/null | wc -l) "
               f"n_next=$(ls -d *_next* 2>/dev/null | wc -l) n_trash=$(ls -A .trash 2>/dev/null | wc -l) "
               f"n_other=$(ls -A | grep -v -E '^(L[0-9]+|restore_.*|\\.trash)$' | wc -l); "
               f"df -k / | awk 'NR==2{{print \"root_used_kb=\"$3}}'; df -k {SNAP} | awk 'NR==2{{print \"snap_used_kb=\"$3}}'; "
               f"echo n_mem=$(ls -d {SNAP}/*_mem 2>/dev/null | wc -l); "
               # /var/log 与层目录在同一个 ext4 上：journald 的增长也算进 df /，G-11 要把它扣掉
               f"echo log_kb=$(du -skx /var/log 2>/dev/null | cut -f1)")


def kv(s):
    return {k: int(v) for k, v in re.findall(r"(\w+)=(-?\d+)", s)}


@item("G-11", "create → restore 基点 → delete 循环：层目录数、df / 稳定（另测不 restore 的 create/delete 循环）")
def t_g11(it):
    g = it.sandbox()
    start_sleeper(g, 80001)
    ok, c0 = g.create("base")
    it.check(ok, f"基点 c0 {getattr(c0, 'checkpoint_id', c0)}")
    time.sleep(0.5)
    g.sh("sync")
    s0 = kv(g.out(LAYERS_STAT))
    it.e(f"起点: {s0}")
    fails = 0
    for i in range(args.g11_loops):
        g.sh("dd if=/dev/urandom of=/home/user/g11.bin bs=1M count=1 status=none", user="user")
        ok, c = g.create(f"x{i}")
        ok2, _ = g.restore(c0.checkpoint_id) if ok else (False, None)
        ok3, _ = g.delete(c.checkpoint_id) if ok else (False, None)
        fails += not (ok and ok2 and ok3)
        if i in (0, 9, 19, 29):
            it.e(f"  第 {i + 1} 轮后: {kv(g.out(LAYERS_STAT))}")
    time.sleep(2)
    g.sh("sync")
    s1 = kv(g.out(LAYERS_STAT))
    it.e(f"{args.g11_loops} 轮后: {s1}")
    it.check(fails == 0, f"循环中失败 {fails} 次")
    # 当前 upper/work 是 restore_*（restore 之后）或 upper_*_next/work_*_next（create 之后），合计应恰好 2 个
    it.check(s1["n_L"] - s0["n_L"] <= 1 and s1["n_restore"] + s1["n_next"] <= 2 and s1["n_trash"] == 0,
             f"层目录 L {s0['n_L']}→{s1['n_L']}，restore_* {s1['n_restore']}，*_next {s1['n_next']}，.trash {s1['n_trash']}")
    d = s1["root_used_kb"] - s0["root_used_kb"]
    dlog = s1["log_kb"] - s0["log_kb"]
    g.sh("sync; echo 3 > /proc/sys/vm/drop_caches")
    time.sleep(1)
    s2 = kv(g.out(LAYERS_STAT))
    d2 = s2["root_used_kb"] - s0["root_used_kb"]
    dlog2 = s2["log_kb"] - s0["log_kb"]
    # 判据（2026-10-08 调整）：df / 的增量里扣掉 /var/log 的增量。gsd 把 criu 的 dump/restore 日志打进 journal，
    # 每轮约 0.7 MB，journald 以 8 MiB 为单位扩文件；这部分与层回收无关，原判据（不扣）在内核修好后仍会 FAIL。
    it.check(d - dlog < 4096, f"df / used 增量 {d} KB，其中 /var/log（journal）{dlog} KB，扣除后 {d - dlog} KB"
                              f"（{args.g11_loops} 轮 × 1 MiB 写入）；drop_caches 后 {d2} KB / 扣除后 {d2 - dlog2} KB")
    it.check(s1["n_mem"] == s0["n_mem"], f"镜像目录数 {s0['n_mem']}→{s1['n_mem']}，snap used {s0['snap_used_kb']}→{s1['snap_used_kb']} KB")
    # G-24 顺带
    n = g.out("dmesg | grep -c -E 'ovl_|pagemap_read|clear_refs' || true")
    it.e(f"(G-24) {args.g11_loops} 次 restore 后 dmesg 里 ovl_/pagemap_read/clear_refs 行数: {n}")
    # 不 restore 的 create/delete：层留在当前层栈里，本来就回收不了（记录，不判定）
    g2 = it.sandbox()
    t0 = kv(g2.out(LAYERS_STAT))
    for i in range(20):
        g2.sh(f"dd if=/dev/urandom of=/home/user/g11_{i}.bin bs=1M count=1 status=none", user="user")
        ok, c = g2.create(f"y{i}")
        if ok:
            g2.delete(c.checkpoint_id)
    time.sleep(1)
    t1 = kv(g2.out(LAYERS_STAT))
    it.e(f"INFO 不 restore 的 create/delete 20 轮: {t0} → {t1}（新层都在当前层栈里，属预期）")


@item("G-13", "多次 restore 后没有 restore_*/*_next 堆积，.trash 为空；dmesg 无 ovl_ 调试输出（G-24）")
def t_g13(it):
    g = it.sandbox()
    start_sleeper(g, 90001)
    g.sh("echo 1 > /home/user/g13", user="user")
    ok, c1 = g.create("c1")
    g.sh("echo 2 > /home/user/g13", user="user")
    ok2, c2 = g.create("c2")
    if not it.check(ok and ok2, "c1/c2 建好"):
        return
    bad = 0
    for i in range(20):
        ok, _ = g.restore((c1 if i % 2 else c2).checkpoint_id)
        bad += not ok
    time.sleep(1.5)
    s = g.out(LAYERS_STAT)
    ls = g.out(f"ls -A {LAYERS}; echo ---; ls -A {LAYERS}/.trash 2>/dev/null")
    it.check(bad == 0, f"20 次 restore 失败 {bad} 次")
    k = kv(s)
    it.check(k["n_restore"] + k["n_next"] <= 2 and k["n_trash"] == 0, f"{s}\n{ls}")
    n = g.out("dmesg | grep -c -E 'ovl_|pagemap_read|clear_refs' || true")
    it.check(n == "0", f"(G-24) dmesg 里 ovl_/pagemap_read/clear_refs 行数 {n}")


@item("G-08", "层名 L<n>；连续 写文件+create 直到 ResourceExhausted，之后 restore 仍正常")
def t_g08(it):
    g = it.sandbox()
    ids, err, t0 = [], None, time.time()
    for i in range(1, args.g08_max + 1):
        g.sh(f"echo {i} > /home/user/g08_{i}", user="user")
        ok, c = g.create(f"n{i}")
        if not ok:
            err = c
            break
        ids.append(c.checkpoint_id)
    it.e(f"成功 create {len(ids)} 次，用时 {time.time() - t0:.0f}s")
    names = g.out(f"ls {LAYERS} | grep -v -E '^L[0-9]+$' || true")
    it.check(g.out(f"ls {LAYERS} | grep -c -E '^L[0-9]+$'") != "0", f"层目录名是 L<n>；非 L<n> 的条目: {names.split()}")
    if err is None:
        it.status = "INFO"
        it.e(f"{args.g08_max} 次内没有碰到上限")
        return
    t, code, msg = ecode(err)
    it.check(code == "resource_exhausted" and "layer stack too deep" in msg, f"第 {len(ids) + 1} 次: {code} ({t}) {short(msg, 300)}")
    ioc = g.jlines('"overlay ioctl"', 3)
    if ioc:
        it.e(f"最后一次 ioctl: layers={ioc[-1].get('layers')} lowerdir 长度={len(ioc[-1].get('lowerdir', ''))}")
    ok, r = g.restore(ids[-1])
    it.check(ok and g.out(f"cat /home/user/g08_{len(ids)}") == str(len(ids)), f"restore 最后一个 checkpoint: {r if ok else short(r)}")
    ok, r = g.restore(ids[0])
    it.check(ok and g.sh("test -e /home/user/g08_2")[0] != 0 and g.out("cat /home/user/g08_1") == "1",
             f"restore 第一个 checkpoint: {r if ok else short(r)}；g08_1 在、g08_2 不在")
    g.sh("echo again > /home/user/g08_again", user="user")
    ok, c = g.create("after")
    it.check(ok, f"restore 到浅层栈后再 create: {c.checkpoint_id if ok else short(c)}")
    it.check(g.health().replace(" ", "") == '{"status":"ok"}', f"/health {g.health()}")


@item("G-25", "ckpt ioctl 的 lowerdir 不含刚沉下去的层；checkpoint→写→checkpoint→restore 内容正确（按路径写、copy-up）")
def t_g25(it):
    g = it.sandbox()
    start_sleeper(g, 100001)
    g.sh("printf 'v1\\n' > /home/user/g25.txt", user="user")
    ok, c1 = g.create("c1")
    it.check(ok, f"c1 {getattr(c1, 'checkpoint_id', c1)}")
    lay1 = [c for c in g.state().get("checkpoints", []) if c.get("id") == c1.checkpoint_id]
    g.sh("printf 'v2\\n' >> /home/user/g25.txt", user="user")                   # copy-up from c1 的层
    ok, c2 = g.create("c2")
    it.check(ok, f"c2 {getattr(c2, 'checkpoint_id', c2)}")
    lay2 = [c for c in g.state().get("checkpoints", []) if c.get("id") == c2.checkpoint_id]
    ioc = [j for j in g.jlines('"overlay ioctl"', 50) if j.get("kind") == "ckpt"]
    it.e(f"c1 层栈={lay1[0]['layers'] if lay1 else '?'}；c2 层栈={lay2[0]['layers'] if lay2 else '?'}")
    for j in ioc[-2:]:
        it.e(f"ioctl kind=ckpt lowerdir={j.get('lowerdir')} upper={j.get('upperdir')}")
    if lay1 and lay2 and len(ioc) >= 2:
        sunk1, sunk2 = lay1[0]["layers"][0], lay2[0]["layers"][0]
        it.check(sunk1 not in ioc[-2]["lowerdir"].split(":"), f"c1 的 ioctl lowerdir 不含刚沉的 {sunk1}")
        it.check(sunk2 not in ioc[-1]["lowerdir"].split(":") and sunk1 in ioc[-1]["lowerdir"].split(":"),
                 f"c2 的 ioctl lowerdir 不含 {sunk2}、含 {sunk1}")
    else:
        it.check(False, f"拿不到层栈或 ioctl 日志: lay1={lay1} lay2={lay2} ioc={len(ioc)}")
    rio = [j for j in g.jlines('"overlay ioctl"', 50) if j.get("kind") == "restore"]
    g.sh("printf 'v3\\n' >> /home/user/g25.txt", user="user")
    for name, c, want in (("c1", c1, "v1"), ("c2", c2, "v1\nv2"), ("c1", c1, "v1")):
        ok, _ = g.restore(c.checkpoint_id)
        got = g.out("cat /home/user/g25.txt")
        it.check(ok and got == want, f"restore {name}: g25.txt={got!r}（期望 {want!r}）")
    rio = [j for j in g.jlines('"overlay ioctl"', 50) if j.get("kind") == "restore"]
    if rio:
        it.e(f"最后一次 restore ioctl lowerdir={rio[-1].get('lowerdir')}")
    ok, _ = g.restore(c2.checkpoint_id)
    g.sh("printf 'v4\\n' >> /home/user/g25.txt", user="user")
    got = g.out("cat /home/user/g25.txt")
    it.check(got == "v1\nv2\nv4", f"restore c2 后再追加（从 L 层 copy-up）: {got!r}")


HOLDER_PY = r'''
import os, sys, time
path, tag = sys.argv[1], sys.argv[2]
f = open(path, "a"); f.write(tag + "1\n"); f.flush()
open(path + ".ack", "w").write("ready"); done = False
while True:
    if not done and os.path.exists(path + ".go"):
        f.write(tag + "2\n"); f.flush(); done = True
        open(path + ".ack", "w").write("done")
    time.sleep(0.05)
'''


@item("FD", "checkpoint 前打开、checkpoint 后经同一个 fd 写入的内容，restore 后应当消失（uid1000 进程 / root 进程）")
def t_fd(it):
    g = it.sandbox()
    g.write("/home/user/holder.py", HOLDER_PY)
    g.sh("cp /home/user/holder.py /root/holder.py")
    g.bg("python3 /home/user/holder.py /home/user/F_U U")
    g.sh("systemd-run --unit=holder-r -p StandardOutput=null -p StandardError=null python3 /root/holder.py /home/user/F_R R")
    g.sh("echo P1 > /home/user/F_P", user="user")
    g.wait_file("/home/user/F_U.ack", "ready")
    g.wait_file("/home/user/F_R.ack", "ready")
    ok, c1 = g.create("c1")
    lay = [c for c in g.state().get("checkpoints", []) if c.get("id") == c1.checkpoint_id]
    sunk = lay[0]["layers"][0] if lay else "?"
    g.sh("touch /home/user/F_U.go /home/user/F_R.go")
    g.wait_file("/home/user/F_U.ack", "done")
    g.wait_file("/home/user/F_R.ack", "done")
    g.sh("echo P2 >> /home/user/F_P", user="user")
    show = ("for f in F_U F_R F_P; do printf '%s=' $f; tr '\\n' ' ' < /home/user/$f; "
            f"printf '| {sunk}: '; tr '\\n' ' ' < {sunk}/home/user/$f 2>/dev/null; echo; done")
    it.e("checkpoint 后写完:\n" + g.out(show))
    ok, _ = g.restore(c1.checkpoint_id)
    time.sleep(0.3)
    after = g.out(show)
    it.e("restore c1 后:\n" + after)
    for f, first in (("F_U", "U1"), ("F_R", "R1"), ("F_P", "P1")):
        got = g.out(f"cat /home/user/{f}")
        it.check(got == first, f"{f} restore 后 {got!r}（期望 {first!r}）")


@item("G-14", "镜像损坏：FailedPrecondition 不杀进程；criu restore 失败：DataLoss+日志尾部+degraded、create 被拒；成功 restore 后恢复 ok")
def t_g14(it):
    g = it.sandbox()
    pid = start_sleeper(g, 110001)
    ok, c1 = g.create("c1")
    g.sh("echo x > /home/user/g14", user="user")
    ok2, c2 = g.create("c2")
    if not it.check(ok and ok2, "c1/c2 建好"):
        return
    d2 = f"{SNAP}/{c2.checkpoint_id}_mem"
    if g.sh(f"test -d {d2}/t0")[0] == 0:      # 4fb04408b 起每棵树的镜像在 <id>_mem/t<序号>/ 下
        d2 += "/t0"
    it.e(f"镜像目录: {d2}")
    g.sh(f"mv {d2}/inventory.img {d2}/inventory.bak")
    ok, e = g.restore(c2.checkpoint_id)
    it.check(not ok and ecode(e)[1] == "failed_precondition", f"缺 inventory.img: {short(ecode(e)) if not ok else 'ok?'}")
    ps = g.procs()
    it.check(any(p["pid"] == pid and p["state"] != "T" for p in ps), f"进程 {pid} 还在: {[(p['pid'], p['state']) for p in ps]}")
    it.check('"ok"' in g.health(), f"/health {g.health()}")
    g.sh(f"mv {d2}/inventory.bak {d2}/inventory.img; rm -f {d2}/pages-*.img; ls {d2}")
    ok, e = g.restore(c2.checkpoint_id)
    t, code, msg = ecode(e) if not ok else ("", "", "")
    it.check(not ok and code == "data_loss", f"删 pages 后 restore: {code} ({t})")
    it.check(not ok and t == "CheckpointRestoreException", f"SDK 映射: {t}")
    it.check("tail of" in msg and "criu-restore.log" in msg, f"错误信息带日志尾部: {short(msg, 900)}")
    h = g.health()
    it.check('"degraded"' in h, f"/health {h}")
    it.e(f"进程: {[(p['pid'], p['args']) for p in g.procs()]}")
    ok, e = g.create("rejected")
    it.check(not ok and ecode(e)[1] == "failed_precondition", f"degraded 时 create: {short(ecode(e)) if not ok else 'ok?'}")
    ok, r = g.restore(c1.checkpoint_id)
    it.check(ok and r, f"restore c1: {r if ok else short(r)}")
    it.check('"ok"' in g.health(), f"/health {g.health()}")
    it.check(any(p["pid"] == pid for p in g.procs()), f"进程 {pid} 已恢复")
    ok, c3 = g.create("c3")
    it.check(ok, f"恢复后 create: {c3.checkpoint_id if ok else short(c3)}")


@item("G-15", "没有用户进程时 create，再起进程，restore 后进程被杀光")
def t_g15(it):
    g = it.sandbox()
    it.check(g.procs() == [], f"起始 uid1000 进程: {g.procs()}")
    ok, c0 = g.create("empty")
    it.check(ok and not c0.has_process_image, f"create: has_process_image={getattr(c0, 'has_process_image', c0)} image_bytes={getattr(c0, 'image_bytes', None)}")
    start_sleeper(g, 120001)
    g.sh("nohup sleep 120002 >/dev/null 2>&1 &", user="user")
    time.sleep(0.5)
    it.e(f"restore 前: {[(p['pid'], p['args']) for p in g.procs()]}")
    ok, r = g.restore(c0.checkpoint_id)
    it.check(ok and r, f"restore: {r if ok else short(r)} timings={g.sb.checkpoint.last_timings}")
    time.sleep(0.3)
    it.check(g.procs() == [], f"restore 后 uid1000 进程: {g.procs()}")


@item("G-16", "systemctl restart gsd 后 create/restore 正常、/health ok；损坏 state.json 重启后 degraded")
def t_g16(it):
    g = it.sandbox()
    pid = start_sleeper(g, 130001)
    ok, c1 = g.create("c1")
    it.check(ok, f"c1 {getattr(c1, 'checkpoint_id', c1)}")
    g.sh("echo after > /home/user/g16", user="user")
    old = g.out("systemctl show -p MainPID --value gsd")
    g.sh("systemctl restart gsd")
    h = g.wait_health()
    new = g.out("systemctl show -p MainPID --value gsd")
    it.check(old != new and '"ok"' in h, f"gsd {old} → {new}, /health {h}")
    it.e("加载日志: " + g.journal("persisted state loaded", 1))
    it.check(any(p["pid"] == pid for p in g.procs()), f"重启 gsd 后用户进程 {pid} 仍在: {[(p['pid'], p['args']) for p in g.procs()]}")
    ids = [c.checkpoint_id for c in g.sb.checkpoint.list()]
    it.check(ids == [c1.checkpoint_id], f"list: {ids}")
    ok, c2 = g.create("c2")
    it.check(ok, f"重启后 create: {c2.checkpoint_id if ok else short(c2)} parent={getattr(c2, 'parent_checkpoint_id', None)}")
    ok, r = g.restore(c1.checkpoint_id)
    it.check(ok and r and g.sh("test -e /home/user/g16")[0] != 0, f"重启后 restore c1: {r if ok else short(r)}")
    ok, r = g.restore(c2.checkpoint_id)
    it.check(ok and g.out("cat /home/user/g16") == "after", f"restore c2: {r if ok else short(r)}")
    it.check('"ok"' in g.health(), f"/health {g.health()}")
    g.sh(f"cp {SNAP}/state.json /tmp/state.bak; echo '{{garbage' > {SNAP}/state.json; systemctl restart gsd")
    h = g.wait_health("degraded")
    it.check('"degraded"' in h, f"state.json 损坏后 /health {h}")
    ok, e = g.create("x")
    it.check(not ok and ecode(e)[1] == "failed_precondition", f"degraded 时 create: {short(ecode(e)) if not ok else 'ok?'}")
    it.e("恢复手段：kill 这个沙箱（本项结束时）")


@item("G-22", "systemctl stop envd 期间 create 仍 dump 用户进程")
def t_g22(it):
    g = it.sandbox()
    # 用户进程放在 envd 的 cgroup 之外，否则 stop envd 会连它一起杀掉
    # stdout/stderr 不接 journald：接着的话进程持有 /run/systemd/journal/stdout 的外部 unix socket，criu 拒绝 dump
    g.sh("systemd-run --uid=1000 --gid=1000 --unit=g22work -p StandardOutput=null -p StandardError=null /bin/sleep 140001")
    time.sleep(0.5)
    ps = g.procs()
    it.e(f"用户进程: {[(p['pid'], p['ppid'], p['args']) for p in ps]}")
    pid = ps[0]["pid"] if ps else None
    g.sh("systemd-run --on-active=1 --unit=g22stop /bin/systemctl stop envd; "
         "systemd-run --on-active=25 --unit=g22start /bin/systemctl start envd")
    time.sleep(4)
    ok, c = g.create("noenvd")
    it.check(ok and c.has_process_image, f"envd 停着时 create: {c.checkpoint_id if ok else short(c)} has_process_image={getattr(c, 'has_process_image', None)}")
    back = False
    for _ in range(60):
        try:
            if g.sb.commands.run("true", user="root", timeout=5).exit_code == 0:
                back = True
                break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    it.check(back, "envd 回来了（systemd-run 定时 start）")
    if back and ok:
        it.e("envd 状态: " + g.out("systemctl is-active envd; journalctl -u envd --no-pager -o cat | tail -2"))
        root = dump_roots(g, c.checkpoint_id)
        it.check(pid in root, f"dump_root_pids={root}，应含 {pid}")
        it.e("envd MainPID 在 create 时: " + g.out(f"journalctl --no-pager -o cat -u envd | grep -E 'Stopp|Start' | tail -3"))
        # 5882e84d7 起 envd 的 MainPID 缓存 + starttime 校验：envd 换了进程之后照样排除、照样便宜
        ok2, c2 = g.create("envdback")
        fp = getattr(c2, "timings", {}).get("find_pids_ms") if ok2 else None
        it.check(ok2 and c2.has_process_image, f"envd 回来后 create: {c2.checkpoint_id if ok2 else short(c2)} find_pids_ms={fp}")
        ok3, r3 = g.restore(c2.checkpoint_id) if ok2 else (False, None)
        lt = g.sb.checkpoint.last_timings or {}
        it.check(ok3 and r3 and any(p["pid"] == pid for p in g.procs()),
                 f"envd 回来后 restore: find_pids_ms={lt.get('find_pids_ms')} kill_ms={lt.get('kill_ms')}")
        it.e("envd 现在的 MainPID: " + g.out("systemctl show envd -p MainPID"))


@item("G-20", "X-Access-Token 鉴权：secure 沙箱缺/错 token 401，对的 200，/health 免鉴权；跨沙箱 token 被拒；非 secure 放行；MMDS 开销")
def t_g20(it):
    a = it.sandbox()
    b = it.sandbox()
    c = it.sandbox(secure=False)
    it.e(f"A token={'有' if a.tok else '无'} B token={'有' if b.tok else '无'} C(secure=False) token={c.tok!r}")
    code, body = a.rpc("ListCheckpoints", token=None)
    it.check(code == "401", f"A 不带 token: {code} {short(body, 200)}")
    code, body = a.rpc("ListCheckpoints", token="wrong-token")
    it.check(code == "401", f"A 错 token: {code} {short(body, 200)}")
    code, body = a.rpc("ListCheckpoints")
    it.check(code == "200", f"A 对的 token: {code} {short(body, 200)}")
    code, body = a.curl("/health", token=None)
    it.check(code == "200", f"A /health 不带 token: {code} {body}")
    code, body = a.rpc("ListCheckpoints", token=b.tok)
    it.check(code == "401", f"A 用 B 的 token: {code}")
    code, body = b.rpc("ListCheckpoints", token=a.tok)
    it.check(code == "401", f"B 用 A 的 token: {code}")
    code, body = b.rpc("ListCheckpoints")
    it.check(code == "200", f"B 自己的 token: {code}")
    code, body = c.rpc("ListCheckpoints", token=None)
    it.check(code == "200", f"C(非 secure) 不带 token: {code} {short(body, 200)}")
    code, body = c.rpc("CreateCheckpoint", '{"name":"nosec"}', token=None)
    it.check(code == "200", f"C 不带 token create: {code} {short(body, 200)}")
    it.check(isinstance(a.sb.checkpoint.list(), list), "SDK（A）list 正常")
    # MMDS 开销：ListCheckpoints（每次读 MMDS）对 /health（不鉴权）
    for g, tag in ((a, "secure"), (c, "non-secure")):
        tk = f"-H 'X-Access-Token: {g.tok}'" if g.tok else ""
        o = g.out("for i in $(seq 40); do curl -s -o /dev/null -w '%{time_total}\\n' http://127.0.0.1:49984/health; done; echo ---; "
                  f"for i in $(seq 40); do curl -s -o /dev/null -w '%{{time_total}}\\n' {tk} -H 'Content-Type: application/json' -d '{{}}' "
                  "http://127.0.0.1:49984/checkpoint.Checkpoint/ListCheckpoints; done")
        h, l = o.split("---")
        hs = sorted(float(x) * 1000 for x in h.split())
        ls = sorted(float(x) * 1000 for x in l.split())
        it.e(f"{tag}: /health p50={hs[len(hs) // 2]:.2f} ms，ListCheckpoints(带鉴权) p50={ls[len(ls) // 2]:.2f} ms，差 {ls[len(ls) // 2] - hs[len(hs) // 2]:.2f} ms")


@item("G-21", "/tmp 回滚（b19d7d710 起在 overlay 上）；大内存负载连续 create 到 ResourceExhausted（非 OOM）；image_bytes 与 du 相符")
def t_g21(it):
    g = it.sandbox()
    g.sh("echo tmpdata > /tmp/g21")
    ok, c1 = g.create("tmp")
    g.sh("rm -f /tmp/g21; echo later > /tmp/g21b")
    ok2, _ = g.restore(c1.checkpoint_id) if ok else (False, None)
    it.check(ok and ok2 and g.out("cat /tmp/g21 2>&1") == "tmpdata" and g.sh("test -e /tmp/g21b")[0] != 0,
             f"/tmp/g21 删掉、另建 /tmp/g21b 后 restore：g21 回来、g21b 消失（/tmp 回滚；旧预期是不回滚）: g21={g.out('cat /tmp/g21 2>&1')!r} "
             f"g21b 存在={g.sh('test -e /tmp/g21b')[0] == 0}；df /tmp: {g.out('df /tmp | tail -1')}")
    pid = start_hog(g, 480, "full", "g21")
    it.e(f"hog pid={pid}; {g.out('free -m | sed -n 2p')}; {g.out(f'df -m {SNAP} | tail -1')}")
    made, err = [], None
    for n in range(1, 9):
        hog_cmd(g, "g21", n)
        ok, c = g.create(f"m{n}")
        if not ok:
            err = c
            break
        made.append(c)
        it.e(f"create #{n}: image_bytes={c.image_bytes >> 20} MiB parent={c.parent_checkpoint_id}; "
             f"{g.out('grep MemAvailable /proc/meminfo')}; snap {g.out(f'df -m {SNAP} | tail -1')}")
    if not it.check(err is not None, f"{len(made)} 次 create 后得到错误"):
        return
    t, code, msg = ecode(err)
    it.check(code == "resource_exhausted" and "not enough room" in msg, f"{code} ({t}): {short(msg, 500)}")
    it.check(t == "NotEnoughSpaceException" and "Rate limit" not in msg, f"SDK 映射: {t}（以前是 RateLimitException）")
    it.check(any(p["pid"] == pid and p["state"] != "T" for p in g.procs()), f"hog {pid} 还活着且没停")
    it.check(g.out("dmesg | grep -c -i -E 'out of memory|oom-kill' || true") == "0", "dmesg 没有 OOM")
    it.e("gsd 日志: " + g.journal("not enough room", 1))
    st = g.state()
    rows = []
    for c in st.get("checkpoints", []):
        mp = c.get("mem_path")
        du = g.out(f"du -sb {mp} 2>/dev/null | cut -f1") if mp else ""
        rows.append((c.get("id"), c.get("image_bytes"), int(du or 0)))
    it.e(f"state.json image_bytes vs du -sb: {rows}")
    it.check(rows and all(abs(ib - du) <= max(64 << 10, du * 0.05) for _, ib, du in rows if ib), "image_bytes 与 du -sb 相差 ≤5%")
    lst = {c.checkpoint_id: c.image_bytes for c in g.sb.checkpoint.list()}
    it.check(all(lst.get(i) == (ib or 0) for i, ib, _ in rows), f"List 的 image_bytes 与 state.json 一致（state 里 0 省略）: {lst}")


@item("G-29", "create 返回 has_process_image/parent_checkpoint_id/image_bytes/timings，clear_refs_failed=0；并发 create 排队（G-12）")
def t_g29(it):
    g = it.sandbox()
    e0 = g.create("fsonly")[1]
    it.check(not e0.has_process_image and e0.image_bytes == 0 and e0.parent_checkpoint_id is None,
             f"无进程: has_process_image={e0.has_process_image} image_bytes={e0.image_bytes} parent={e0.parent_checkpoint_id} timings={e0.timings}")
    start_sleeper(g, 150001)
    ok, c1 = g.create("c1")
    it.check(ok and c1.has_process_image and c1.parent_checkpoint_id is None and c1.image_bytes > 0 and c1.clear_refs_failed == 0,
             f"c1: has_process_image={c1.has_process_image} parent={c1.parent_checkpoint_id} image_bytes={c1.image_bytes} clear_refs_failed={c1.clear_refs_failed} created_at={c1.created_at}")
    it.check(set(c1.timings or {}) >= {"total_ms", "find_pids_ms", "criu_dump_ms", "overlay_sink_ms"}, f"c1 timings={c1.timings}")
    ok, c2 = g.create("c2")
    it.check(ok and c2.parent_checkpoint_id == c1.checkpoint_id and c2.clear_refs_failed == 0, f"c2 parent={c2.parent_checkpoint_id} image_bytes={c2.image_bytes}")
    ls = {c.checkpoint_id: (c.has_process_image, c.parent_checkpoint_id, c.image_bytes) for c in g.sb.checkpoint.list()}
    it.check(ls.get(c2.checkpoint_id) == (True, c1.checkpoint_id, c2.image_bytes), f"list: {ls}")
    ok, r = g.restore(c1.checkpoint_id)
    lt = g.sb.checkpoint.last_timings or {}
    it.check(ok and set(lt) >= {"total_ms", "kill_ms", "overlay_restore_ms", "criu_restore_ms"}, f"restore last_timings={lt}")
    # G-12：4 个并发 create 都成功、各不相同，gsd 串行处理
    res = [None] * 4

    def w(k):
        res[k] = g.create(f"par{k}")
    th = [threading.Thread(target=w, args=(k,)) for k in range(4)]
    [t.start() for t in th]
    [t.join() for t in th]
    ids = [r[1].checkpoint_id for r in res if r and r[0]]
    it.check(len(set(ids)) == 4, f"(G-12) 4 个并发 create 成功 {len(ids)} 个: {[(r[0], getattr(r[1], 'checkpoint_id', short(r[1], 100))) for r in res]}")
    tot = [r[1].timings.get("total_ms") for r in res if r and r[0]]
    it.e(f"(G-12) 并发 create 的 total_ms（从拿到锁开始算）: {tot}")


def stat_perm(g):
    return g.out(f"stat -c '/=%a:%U' /; for d in {LAYERS}/*; do stat -c '%n=%a:%U' $d; done | sed 's#{LAYERS}/##' | tr '\\n' ' '")


@item("G-33", "create/restore 前后 / 与层目录的权限（预期 / 755 root，层目录 755；e77d29983 之前 / 是 777）")
def t_g33(it):
    g = it.sandbox()
    s = stat_perm(g)
    it.e(f"开机: {s}")
    root0 = g.out("stat -c %a /")
    st = {}
    ok, c0 = g.create("c0")
    st0 = [c for c in g.state().get("checkpoints", []) if c.get("id") == c0.checkpoint_id]
    it.e(f"c0 后: {stat_perm(g)}；c0 层栈={st0[0]['layers'] if st0 else '?'}")
    g.sh("echo 1 > /home/user/g33", user="user")
    ok, c1 = g.create("c1")
    it.e(f"c1 后: {stat_perm(g)}")
    for name, c in (("c0", c0), ("c1", c1), ("c0", c0)):
        g.restore(c.checkpoint_id)
        st[name] = g.out("stat -c %a /")
        it.e(f"restore {name} 后: {stat_perm(g)}")
    # 专门造一个"只有基础层"的 checkpoint：restore 回一个层栈只有 /mnt/lower 的 checkpoint（若 c0 不是，则说明）
    base_only = st0 and st0[0]["layers"] == ["/mnt/lower"]
    it.e(f"c0 是否只有基础层: {bool(base_only)}")
    roots = {root0} | set(st.values())
    it.check(len(roots) == 1, f"/ 的权限始终一致: {roots}")
    # 层目录和 upper 的根就是 / 的属性来源，按设计跟 / 一致；work 目录 0755
    lay = re.findall(r"(\S+?)=(\d+):(\w+)", stat_perm(g).split(" ", 1)[1])
    it.check(all(m == root0 for n, m, _ in lay if n.startswith("L") or n.endswith("_upper") or n.startswith("upper_")),
             f"层目录/upper 与 / 同为 {root0}: {[(n, m) for n, m, _ in lay if not n.endswith('work') and not n.startswith('work_')]}")
    it.check(all(m == "755" for n, m, _ in lay if n.endswith("_work") or n.startswith("work_")), f"work 目录 755: {[(n, m) for n, m, _ in lay if 'work' in n]}")
    it.check(root0 == "755" and g.out("stat -c %U:%G /") == "root:root", f"/ 是 755 root:root: 实测 {root0} {g.out('stat -c %U:%G /')}")
    it.e("gsd umask: " + g.out("grep Umask /proc/$(systemctl show -p MainPID --value gsd)/status"))


@item("UMASK", "umask 022 下 criu restore 带 unix socket、带打开的已删除文件的进程，功能正常")
def t_umask(it):
    g = it.sandbox()
    it.e("gsd umask: " + g.out("grep Umask /proc/$(systemctl show -p MainPID --value gsd)/status"))
    g.write("/home/user/um.py", UMASK_PY)
    g.bg("python3 /home/user/um.py")
    g.wait_file("/home/user/um/ready")
    r0 = g.out(UMASK_CLIENT, user="user")
    it.check(r0 == "ping|ghost-content", f"起始: {r0!r}；sock {g.out('stat -c %a:%U /home/user/um/srv.sock')}")
    ok, c = g.create("um")
    it.check(ok, f"create {getattr(c, 'checkpoint_id', c)}")
    r1 = g.out(UMASK_CLIENT, user="user")
    it.check(r1 == "ping|ghost-content", f"create 后: {r1!r}")
    for k in (1, 2):
        ok, r = g.restore(c.checkpoint_id)
        rr = g.out(UMASK_CLIENT, user="user")
        it.check(ok and rr == "ping|ghost-content", f"restore #{k}: {r if ok else short(r)} → {rr!r}；sock {g.out('stat -c %a:%U /home/user/um/srv.sock')}")
    it.e("fd: " + g.out("ls -l /proc/$(cat /home/user/um/ready)/fd | grep -E 'deleted|socket' | head"))


@item("G-32", "模板里没有 overlay-setup，gsd 正常起来")
def t_g32(it):
    g = it.sandbox()
    o = g.out("ls /etc/systemd/system/ /etc/systemd/system/multi-user.target.wants/ | grep -i overlay || true; "
              "ls /usr/bin/overlay* /usr/local/bin/overlay* 2>/dev/null || true")
    it.check("overlay-setup" not in o, f"overlay 相关文件: {o.split()}")
    it.check(g.out("systemctl is-active gsd") == "active", "gsd active")
    it.check('"ok"' in g.health(), f"/health {g.health()}")
    it.e("gsd.service After=: " + g.out("systemctl show -p After gsd"))


@item("G-09", "criu dump 超时（GSD_CRIU_DUMP_TIMEOUT）：DeadlineExceeded、失败目录清掉、进程不留 T（G-23/G-28）、下一次是全量（G-09）")
def t_g09(it):
    g = it.sandbox()
    pid = start_hog(g, 300, "full", "g09")
    ok, c1 = g.create("c1")
    it.check(ok, f"c1（正常超时）{getattr(c1, 'checkpoint_id', c1)} criu_dump_ms={getattr(c1, 'timings', {}).get('criu_dump_ms') if ok else None}")
    g.sh("mkdir -p /etc/systemd/system/gsd.service.d && printf '[Service]\\nEnvironment=GSD_CRIU_DUMP_TIMEOUT=40ms\\n' "
         "> /etc/systemd/system/gsd.service.d/timeout.conf && systemctl daemon-reload && systemctl restart gsd")
    it.e("gsd 重启: " + g.wait_health())
    hog_cmd(g, "g09", 1)
    before = g.out(f"ls -d {SNAP}/*_mem")
    ok, e = g.create("timeout")
    t, code, msg = ecode(e) if not ok else ("", "", "")
    it.check(not ok and code == "deadline_exceeded", f"create: {code} ({t}) {short(msg, 300)}")
    # 5882e84d7 起：dump 失败的错误带 criu-dump.log 尾部，不再指向已删的日志；SDK 提示 GSD 自己的时限
    it.check(not ok and "tail of criu-dump.log" in msg and "(log: " not in msg,
             f"dump 失败的错误带日志尾部、不指向已删文件: {short(msg, 900)}")
    it.check(not ok and t == "TimeoutException" and "GSD's time limit" in msg and "Rate limit" not in msg,
             f"SDK 映射: {t}")
    time.sleep(1)
    after = g.out(f"ls -d {SNAP}/*_mem")
    it.check(before == after, f"失败的镜像目录已清掉: before={before.split()} after={after.split()}")
    st = [(p["pid"], p["state"]) for p in g.procs()]
    it.check(all(s != "T" for _, s in st) and any(p == pid for p, _ in st), f"hog 状态: {st}")
    it.e("gsd 日志: " + g.journal("criu dump failed", 1)[:600])
    it.e(g.journal("resumed processes left stopped", 1)[:300])
    g.sh("rm -f /etc/systemd/system/gsd.service.d/timeout.conf && systemctl daemon-reload && systemctl restart gsd")
    g.wait_health()
    ok, c3 = g.create("c3")
    it.check(ok and c3.has_process_image and c3.parent_checkpoint_id is None,
             f"恢复超时后 create: {getattr(c3, 'checkpoint_id', c3)} parent={getattr(c3, 'parent_checkpoint_id', None)}（应为全量）")
    ok, r = g.restore(c3.checkpoint_id)
    it.check(ok and r, f"restore c3: {r if ok else short(r)}")


@item("PERF", "find_pids 开销：systemctl show 的耗时（INFO）；5882e84d7 起 create/restore 的 find_pids_ms p50 < 1 ms")
def t_perf(it):
    g = it.sandbox()
    o = g.out("for i in $(seq 20); do s=$(date +%s%N); systemctl show envd.service --property=MainPID >/dev/null; "
              "e=$(date +%s%N); echo $(( (e-s)/1000 )); done")
    xs = sorted(int(x) for x in o.split())
    it.e(f"guest 里 systemctl show envd.service --property=MainPID：p50={xs[len(xs) // 2] / 1000:.2f} ms min={xs[0] / 1000:.2f} max={xs[-1] / 1000:.2f}")
    it.e(f"guest 进程数 {g.out('ls -d /proc/[0-9]* | wc -l')}，其中内核线程 {g.out('cat /proc/2/task/2/children | wc -w')}")
    pid = start_sleeper(g, 160001)
    cf, rf, ck = [], [], []
    for i in range(5):
        ok, c = g.create(f"p{i}")
        if not ok:
            it.check(False, f"create p{i}: {short(c)}")
            return
        cf.append(c.timings.get("find_pids_ms"))
        ok, r = g.restore(c.checkpoint_id)
        lt = g.sb.checkpoint.last_timings or {}
        rf.append(lt.get("find_pids_ms"))
        ck.append(lt.get("kill_ms"))
    it.e(f"create find_pids_ms={cf}")
    it.e(f"restore find_pids_ms={rf} kill_ms={ck}")
    med = lambda v: sorted(v)[len(v) // 2]
    it.check(med(cf) < 1.0 and med(rf) < 1.0, f"find_pids_ms p50: create {med(cf):.2f}，restore {med(rf):.2f}（a35ce1993 约 9～10）")
    it.check(any(p["pid"] == pid for p in g.procs()), "sleeper 仍在（最后一次 restore 恢复出来的）")
    it.e("kill 细分（最近 2 次）: " + g.journal("user processes killed", 2)[:800])


@item("G-19", "HOST_GSD_PATH 开关（关闭时建普通模板）")
def t_g19(it):
    it.status = "SKIP"
    it.e("未在现场测关闭态：HOST_GSD_PATH 在 orchestrator 启动时读一次，要看到关闭态必须改 orchestrator 的启动环境并重启它，会影响机器上的其他使用者；依据是 orchestrator 单测与读代码。")
    it.e("开启态已由建模板时的日志证实（最早在 09-30 的实验模板上）：template-manager 日志 3 次 'gsd healthy; criu Version: 4.2.1'（base / user / finalize 各一次），"
         "对应 layer/gsd.go 注入后 systemctl restart gsd + gsdCheckScript。")


@item("G-36", "SDK 带 sandbox_url 覆盖时 checkpoint 请求的 URL")
def t_g36(it):
    it.status = "SKIP"
    it.e("本机单测已覆盖（py-sdk 的三种 URL 选择，sync/async）；现场可选，未做。")


# ============================== 主流程 ==============================

def main():
    log(f"template={args.template}")
    for iid, title, fn in ITEMS:
        if not selected(iid):
            continue
        it = Item(iid, title)
        log(f"=== {iid} {title}")
        t0 = time.time()
        try:
            fn(it)
        except Exception as e:  # noqa: BLE001
            it.status = "FAIL"
            it.e(f"异常: {type(e).__name__}: {short(e, 800)}")
            it.e(traceback.format_exc()[-1500:])
        finally:
            for g in it.sbs:
                try:
                    bad = g.dmesg_bad()
                    if bad:
                        it.e(f"dmesg({g.id}) 可疑行:\n{short(bad, 1500)}")
                except Exception:  # noqa: BLE001
                    pass
                g.kill()
        it.secs = time.time() - t0
        log(f"--> {iid}: {it.status} ({it.secs:.0f}s)")
        RESULTS.append(it)

    print("\n" + "=" * 90)
    print(f"{'项目':<8} {'结果':<6} {'秒':>5}  说明")
    print("-" * 90)
    for it in RESULTS:
        print(f"{it.iid:<8} {it.status:<6} {it.secs:5.0f}  {it.title}")
    bad = [it.iid for it in RESULTS if it.status == "FAIL"]
    print("-" * 90)
    print(f"合计 {len(RESULTS)} 项，FAIL {len(bad)}: {bad}")
    if args.out_json:
        json.dump([{"id": it.iid, "title": it.title, "status": it.status, "secs": round(it.secs, 1), "evidence": it.ev}
                   for it in RESULTS], open(args.out_json, "w"), ensure_ascii=False, indent=1)
    if args.out_md:
        with open(args.out_md, "w") as f:
            f.write(f"# verify_fixes 汇总（模板 {args.template}）\n\n| 项目 | 结果 | 说明 |\n|---|---|---|\n")
            for it in RESULTS:
                f.write(f"| {it.iid} | {it.status} | {it.title} |\n")
            f.write("\n## 证据\n")
            for it in RESULTS:
                f.write(f"\n### {it.iid} {it.status}\n\n```\n" + "\n".join(it.ev) + "\n```\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
