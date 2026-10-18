#!/usr/bin/env python3
"""多棵用户进程树 / /tmp 回滚 / 根目录 755 的现场验证（V0～V12）。

覆盖三个主题：多棵用户进程树各自保存与恢复、/tmp 放到 overlay 上随 restore 回滚、guest 根目录权限为 755。
每项独立起沙箱，输出 PASS / FAIL / INFO 和证据，最后汇总成表。

在宿主机上跑（gsd 栈在运行；解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python verify_multitree.py --template base-gsd \
        [--old-template <对照模板>] [--only V2,V6] [--skip V12] [--perf-rounds 4] [--out-json x.json]

V12 的 N=1 对照：--old-template 指向"旧 gsd + 同一内核"建的模板（gsd 是烤在模板快照里的），
两个模板逐轮交错跑同一个负载（192 MiB memhog，与 verify_gsd / exp_gsd_swap 相同的形状）。
公共部分（沙箱封装 G、Item、ecode）取自同目录的 verify_fixes.py（import verify_fixes），两个文件必须放在一起。
"""
import argparse
import json
import os
import sys
import time
import traceback

ap = argparse.ArgumentParser()
ap.add_argument("--template", default="base-gsd")
ap.add_argument("--old-template", default="", help="V12 N=1 的对照模板（另一版 gsd + 同内核）；默认空串 = 不对照")
ap.add_argument("--only", default="")
ap.add_argument("--skip", default="")
ap.add_argument("--perf-rounds", type=int, default=4)
ap.add_argument("--perf-restores", type=int, default=9, help="每个沙箱 restore 次数（第 1 次不计）")
ap.add_argument("--perf-creates", type=int, default=5)
ap.add_argument("--out-json", default="")
args = ap.parse_args()

_argv = sys.argv
sys.argv = [_argv[0], "--template", args.template]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_fixes as vf  # noqa: E402

sys.argv = _argv
Item, log, short, ecode = vf.Item, vf.log, vf.short, vf.ecode
SNAP = vf.SNAP
SLOW = []          # 墙钟比 gsd 自报 total_ms 多出 1.5 秒以上的 create/restore


class G(vf.G):
    """在 create/restore 外面量墙钟：SDK → client-proxy → gsd 整条路径上的额外等待都算在内。"""

    def _timed(self, what, fn, arg):
        t = time.time()
        ok, r = fn(arg)
        wall = (time.time() - t) * 1000
        tm = None
        if ok:
            tm = (r.timings if what == "create" else self.sb.checkpoint.last_timings) or {}
            tm = tm.get("total_ms")
        if wall - (tm or 0) > 1500:
            ev = {"sandbox": self.id, "op": what, "arg": str(arg), "ok": ok, "wall_ms": round(wall), "gsd_total_ms": tm,
                  "host_time": time.strftime("%H:%M:%S"),
                  "gsd_journal_tail": self.out("journalctl -u gsd --no-pager -o short-precise | tail -25 | cut -c1-330"),
                  "guest_now": self.out("date +%H:%M:%S.%N")}
            SLOW.append(ev)
            log(f"    !! 慢调用 {what}: 墙钟 {wall:.0f} ms，gsd 自报 total_ms={tm}（sandbox {self.id}）")
        return ok, r

    def create(self, name=None):
        return self._timed("create", super().create, name)

    def restore(self, cid):
        return self._timed("restore", super().restore, cid)


vf.G = G           # Item.sandbox() 用的也是它
RESULTS = []
ITEMS = []
PERF = {}


def item(iid, title):
    def deco(fn):
        ITEMS.append((iid, title, fn))
        return fn
    return deco


def selected(iid):
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    skip = [x.strip() for x in args.skip.split(",") if x.strip()]
    return (not only or iid in only) and iid not in skip


# ============================== 辅助 ==============================

def counter_cmd(name):
    return "sh -c 'i=0; while :; do i=$((i+1)); echo $i > /home/user/%s.cnt; sleep 0.2; done'" % name


def start_counters(g, names):
    for n in names:
        g.bg(counter_cmd(n))
    for n in names:
        g.wait_file(f"/home/user/{n}.cnt")


def cnts(g, names):
    o = g.out("for n in %s; do echo $n=$(cat /home/user/$n.cnt 2>/dev/null); done" % " ".join(names))
    res = {}
    for line in o.splitlines():
        k, _, v = line.partition("=")
        try:
            res[k] = int(v)
        except ValueError:
            res[k] = None
    return res


def trees(ps):
    """g.procs() → {根 pid: [该树的进程，按 pid 排序]}；根 = 父进程不是 uid 1000 进程。"""
    by = {p["pid"]: p for p in ps}
    res = {}
    for p in ps:
        r = p
        while r["ppid"] in by:
            r = by[r["ppid"]]
        res.setdefault(r["pid"], []).append(p)
    return {k: sorted(v, key=lambda x: x["pid"]) for k, v in sorted(res.items())}


def fmt_ps(ps):
    return [(p["pid"], p["ppid"], p["state"], p["comm"]) for p in ps]


def ckpt_state(g, cid):
    for c in g.state().get("checkpoints", []):
        if c.get("id") == cid:
            return c
    return {}


def mem_listing(g, cid):
    """<cid>_mem 下各 t* 目录：{t0: {inventory: bool, parent: 链接目标或 ''}}"""
    o = g.out(f"cd {SNAP}/{cid}_mem 2>/dev/null && for t in t*; do echo \"$t|$([ -s $t/inventory.img ] && echo 1 || echo 0)|$(readlink $t/parent 2>/dev/null)|$(readlink -f $t/parent 2>/dev/null)\"; done")
    res = {}
    for line in o.splitlines():
        p = line.split("|")
        if len(p) == 4:
            res[p[0]] = {"inventory": p[1] == "1", "parent": p[2], "parent_abs": p[3]}
    return res


def health_ok(g):
    return '"status":"ok"' in g.health().replace(" ", "")


PYCNT = r'''
import sys, time
name = sys.argv[1]; i = 0
while True:
    i += 1
    open("/home/user/%s.cnt" % name, "w").write("%d\n" % i)
    time.sleep(0.2)
'''


# ============================== 各项 ==============================

@item("V0", "开机日志：/ 是 755 root:root，/tmp 在 overlay 上，tmp.mount 被 mask")
def v0(it):
    g = it.sandbox()
    d = g.out("dmesg | grep overlay-init")
    it.e(d)
    it.check("overlay mounted at /mnt/newroot, / is 755 root:root" in d, "有 'overlay mounted at /mnt/newroot, / is 755 root:root'")
    it.check("/tmp on overlay: 1777 root:root, tmp.mount -> /dev/null" in d, "有 '/tmp on overlay: 1777 root:root, tmp.mount -> /dev/null'")
    it.e(f"gsd -commit: {g.out('/usr/bin/gsd -commit')}；内核: {g.out('cat /proc/version')}")


def v1_run(it, n):
    g = it.sandbox()
    names = [f"c{k}" for k in range(n)]
    start_counters(g, names)
    ps0 = g.procs()
    tr0 = trees(ps0)
    it.e(f"[N={n}] 起了 {n} 个计数器；根 {list(tr0)}；ps {fmt_ps(ps0)}")
    a = cnts(g, names)
    ok, cp = g.create(f"v1-{n}")
    if not it.check(ok, f"[N={n}] create: {cp.checkpoint_id if ok else short(cp)}"):
        return
    b = cnts(g, names)
    it.check(cp.process_trees == n and cp.has_process_image and cp.clear_refs_failed == 0,
             f"[N={n}] process_trees={cp.process_trees} has_process_image={cp.has_process_image} clear_refs_failed={cp.clear_refs_failed} "
             f"image_bytes={cp.image_bytes} timings={cp.timings}")
    ml = mem_listing(g, cp.checkpoint_id)
    it.check(sorted(ml) == [f"t{k}" for k in range(n)] and all(v["inventory"] for v in ml.values()),
             f"[N={n}] {cp.checkpoint_id}_mem 下 {sorted(ml)}，inventory.img 都在: {[v['inventory'] for v in ml.values()]}")
    time.sleep(3)
    c = cnts(g, names)
    ok, r = g.restore(cp.checkpoint_id)
    d = cnts(g, names)
    ps1 = g.procs()
    it.check(ok and health_ok(g), f"[N={n}] restore ok={ok} {'' if ok else short(r)}；health {g.health()}；last_timings={g.sb.checkpoint.last_timings}")
    it.e(f"[N={n}] .cnt create 前 {a} / create 后 {b} / 3 秒后 {c} / restore 后立刻 {d}")
    good = all(d[k] is not None and a[k] is not None and a[k] - 1 <= d[k] <= b[k] + 5 and d[k] < c[k] - 5 for k in names)
    it.check(good, f"[N={n}] restore 后各 .cnt 回到 create 时刻附近（create 前 -1 ～ create 后 +5，且比 3 秒后的值小 5 以上）")
    time.sleep(1.5)
    e = cnts(g, names)
    it.check(all(e[k] is not None and d[k] is not None and e[k] >= d[k] + 3 for k in names), f"[N={n}] 1.5 秒后继续增长: {e}")
    tr1 = trees(ps1)
    it.check(sorted(tr1) == sorted(tr0), f"[N={n}] restore 后根 pid {sorted(tr1)}（checkpoint 时 {sorted(tr0)}）；ps {fmt_ps(ps1)}")


@item("V1", "两个、三个后台命令的 create/restore")
def v1(it):
    v1_run(it, 2)
    v1_run(it, 3)


def v2_run(it, stop_tree):
    g = it.sandbox()
    g.write("/home/user/pycnt.py", PYCNT)
    names = ["a", "b", "c"]
    for k, n in enumerate(names):
        # 每棵树：bash（根）+ sleep + python 计数器（不 fork，pid 集合稳定）
        g.bg(f"bash -c 'sleep 4100{k} & python3 /home/user/pycnt.py {n} & wait'")
    for n in names:
        g.wait_file(f"/home/user/{n}.cnt")
    time.sleep(0.5)
    ps0 = g.procs()
    tr0 = trees(ps0)
    roots = sorted(tr0)
    if not it.check(len(roots) == 3 and all(len(v) == 3 for v in tr0.values()), f"[停 t{stop_tree}] 3 棵树各 3 个进程: {dict((k, fmt_ps(v)) for k, v in tr0.items())}"):
        return
    victim = [p for p in tr0[roots[stop_tree]] if p["comm"] == "sleep"][0]["pid"]
    g.sh(f"kill -STOP {victim}")
    time.sleep(0.3)
    pre = {p["pid"]: p["state"] for p in g.procs()}
    it.e(f"[停 t{stop_tree}] 预先 kill -STOP {victim}（树 t{stop_tree}，根 {roots[stop_tree]}）；create 前状态 {pre}")
    ok, cp = g.create("v2")
    if not it.check(ok and cp.process_trees == 3, f"[停 t{stop_tree}] create: {cp.checkpoint_id if ok else short(cp)} process_trees={getattr(cp, 'process_trees', None)}"):
        return
    ps1 = g.procs()
    it.e(f"[停 t{stop_tree}] create 后 ps -o pid,ppid,stat: " + g.out("ps -u 1000 -o pid=,ppid=,stat=,comm= | tr -s ' ' | tr '\\n' ';'"))
    tset = sorted(p["pid"] for p in ps1 if p["state"] == "T")
    it.check(tset == [victim], f"[停 t{stop_tree}] create 后只有预先停住的 {victim} 是 T: T={tset}；t0（根 {roots[0]}）各进程状态 {[(p['pid'], p['state']) for p in ps1 if p['pid'] in {q['pid'] for q in tr0[roots[0]]}]}")
    c1 = cnts(g, names)
    time.sleep(1.0)
    c2 = cnts(g, names)
    it.check(all(c2[k] > c1[k] for k in names), f"[停 t{stop_tree}] create 后三个计数器都在走: {c1} → {c2}")
    ok, r = g.restore(cp.checkpoint_id)
    if not it.check(ok, f"[停 t{stop_tree}] restore: {r if ok else short(r)}"):
        return
    ps2 = g.procs()
    it.e(f"[停 t{stop_tree}] restore 后 ps -o pid,ppid,stat: " + g.out("ps -u 1000 -o pid=,ppid=,stat=,comm= | tr -s ' ' | tr '\\n' ';'"))
    tr2 = trees(ps2)
    same = {k: sorted(p["pid"] for p in v) for k, v in tr2.items()} == {k: sorted(p["pid"] for p in v) for k, v in tr0.items()}
    it.check(same, f"[停 t{stop_tree}] restore 后每棵树的 pid 集合与 checkpoint 时相同: {dict((k, [p['pid'] for p in v]) for k, v in tr2.items())}")
    by = {p["pid"]: p for p in ps2}
    it.check(all(by.get(r_, {}).get("ppid") == 1 for r_ in roots), f"[停 t{stop_tree}] 各根的 ppid: {[(r_, by.get(r_, {}).get('ppid')) for r_ in roots]}")
    tset2 = sorted(p["pid"] for p in ps2 if p["state"] == "T")
    it.check(tset2 == [victim], f"[停 t{stop_tree}] restore 后只有预先停住的 {victim} 是 T（其余不能是 T）: T={tset2}，全部状态 {[(p['pid'], p['state']) for p in ps2]}")
    d1 = cnts(g, names)
    time.sleep(1.0)
    d2 = cnts(g, names)
    it.check(all(d2[k] > d1[k] for k in names), f"[停 t{stop_tree}] restore 后三个计数器都在走（进程确实在跑，不只是状态字母）: {d1} → {d2}")
    # 第二次 restore：确认从"恢复出来的树"再恢复也一样
    ok, r = g.restore(cp.checkpoint_id)
    ps3 = g.procs()
    tset3 = sorted(p["pid"] for p in ps3 if p["state"] == "T")
    it.check(ok and tset3 == [victim] and len(ps3) == 9, f"[停 t{stop_tree}] 第二次 restore: ok={ok} T={tset3} 进程数 {len(ps3)}")
    g.sh(f"kill -CONT {victim}")
    time.sleep(0.2)
    it.check(all(p["state"] != "T" for p in g.procs()), f"[停 t{stop_tree}] kill -CONT 后没有 T 进程")


@item("V2", "restore 后各树的 pid 和状态（--leave-stopped 逐棵 dump，恢复出来不能是 T）")
def v2(it):
    v2_run(it, 1)
    v2_run(it, 0)


def counter_roots(g):
    return sorted(k for k, v in trees(g.procs()).items() if any("while :" in p["args"] for p in v))


@item("V3", "一棵树退出后再 create；分别 restore 两个 checkpoint")
def v3(it):
    g = it.sandbox()
    names = ["c0", "c1", "c2"]
    start_counters(g, names)
    r0 = counter_roots(g)
    ok, c1 = g.create("C1")
    if not it.check(ok and c1.process_trees == 3, f"C1: {c1.checkpoint_id if ok else short(c1)} process_trees={getattr(c1, 'process_trees', None)}；根 {r0}"):
        return
    victim = [k for k, v in trees(g.procs()).items() if any("c1.cnt" in p["args"] for p in v)][0]
    g.sh(f"kill -9 {victim}")
    time.sleep(1.0)     # 它的 sleep 0.2 子进程被 PID 1 收养后自己退出
    r1 = counter_roots(g)
    it.e(f"杀掉根 {victim} 后的根: {r1}；ps {fmt_ps(g.procs())}")
    ok, c2 = g.create("C2")
    if not it.check(ok, f"C2: {c2.checkpoint_id if ok else short(c2)}"):
        return
    it.check(c2.process_trees == 2 and c2.parent_checkpoint_id == c1.checkpoint_id,
             f"C2.process_trees={c2.process_trees} parent_checkpoint_id={c2.parent_checkpoint_id}（C1={c1.checkpoint_id}）image_bytes C1={c1.image_bytes} C2={c2.image_bytes}")
    inc = [j for j in g.jlines("using incremental CRIU dump") if j.get("checkpoint_id") == c2.checkpoint_id]
    it.check(len(inc) == 2, f"C2 的 'using incremental CRIU dump' 日志 {len(inc)} 条: {[(j.get('tree'), j.get('prev_mem')) for j in inc]}")
    ok, r = g.restore(c1.checkpoint_id)
    time.sleep(0.3)
    ra = counter_roots(g)
    it.check(ok and ra == r0, f"restore C1: ok={ok} {'' if ok else short(r)} 根 {ra}（应为 {r0}）")
    x1 = cnts(g, names); time.sleep(1); x2 = cnts(g, names)
    it.check(all(x2[k] > x1[k] for k in names), f"restore C1 后三个计数器都在走: {x1} → {x2}")
    ok, r = g.restore(c2.checkpoint_id)
    time.sleep(0.3)
    rb = counter_roots(g)
    it.check(ok and rb == r1 and len(rb) == 2, f"restore C2: ok={ok} {'' if ok else short(r)} 根 {rb}（应为 {r1}）")
    alive = [n for k, n in enumerate(names) if k != 1]
    x1 = cnts(g, names); time.sleep(1); x2 = cnts(g, names)
    it.check(all(x2[k] > x1[k] for k in alive) and x2["c1"] == x1["c1"], f"restore C2 后剩下两个在走、被杀的那个不动: {x1} → {x2}")
    it.check(health_ok(g), f"health {g.health()}")


@item("V4", "新起一棵树后再 create：原有的树增量、新树全量")
def v4(it):
    g = it.sandbox()
    start_counters(g, ["c0", "c1"])
    ok, c1 = g.create("C1")
    if not it.check(ok and c1.process_trees == 2, f"C1: {c1.checkpoint_id if ok else short(c1)} trees={getattr(c1, 'process_trees', None)}"):
        return
    old_roots = counter_roots(g)
    start_counters(g, ["c2"])
    new_root = [r for r in counter_roots(g) if r not in old_roots]
    ok, c2 = g.create("C2")
    if not it.check(ok and c2.process_trees == 3, f"C2.process_trees={getattr(c2, 'process_trees', None)} {c2.checkpoint_id if ok else short(c2)}；原有根 {old_roots}，新根 {new_root}"):
        return
    st = ckpt_state(g, c2.checkpoint_id)
    st_trees = st.get("trees", [])
    it.e(f"state.json C2.trees: {st_trees}")
    ml = mem_listing(g, c2.checkpoint_id)
    it.e(f"C2_mem: {ml}")
    good = len(st_trees) == 3
    for t in st_trees:
        d = os.path.basename(t.get("dir", "").rstrip("/")) or t.get("dir")
        m = ml.get(d, {})
        if t.get("root_pid") in new_root:
            good &= it.check(not m.get("parent") and not t.get("parent_id"), f"新树 {d}（根 {t.get('root_pid')}）没有 parent 链接、state.json parent_id 为空: link={m.get('parent')!r} parent_id={t.get('parent_id')!r}")
        else:
            good &= it.check(m.get("parent_abs", "").startswith(f"{SNAP}/{c1.checkpoint_id}_mem/t") and t.get("parent_id") == c1.checkpoint_id,
                             f"原有树 {d}（根 {t.get('root_pid')}）parent → {m.get('parent')}（{m.get('parent_abs')}），parent_id={t.get('parent_id')}")
    it.check(good, "三棵树的 parent 关系都符合预期")
    ok, r = g.restore(c2.checkpoint_id)
    it.check(ok and len(counter_roots(g)) == 3, f"restore C2 ok={ok}，根 {counter_roots(g)}")
    ok, r = g.restore(c1.checkpoint_id)
    it.check(ok and counter_roots(g) == old_roots, f"restore C1 ok={ok}，根 {counter_roots(g)}（应为 {old_roots}）")


HOG64 = "python3 -c 'import time; b=bytearray(64<<20); [time.sleep(1) for _ in iter(int,1)]'"


def wait_rss(g, n, mb=60, tries=100):
    for _ in range(tries):
        o = g.out("for p in $(pgrep -u 1000 -x python3); do awk '/^RssAnon/{print $2}' /proc/$p/status; done")
        v = [int(x) for x in o.split()]
        if len(v) >= n and sum(1 for x in v if x >= mb * 1024) >= n:
            return v
        time.sleep(0.1)
    return v


@item("V5", "多树下的增量大小")
def v5(it):
    g = it.sandbox()
    g.bg(HOG64)
    g.bg(HOG64)
    it.e(f"两棵树 RssAnon(KB): {wait_rss(g, 2)}")
    ok, c1 = g.create("C1")
    if not it.check(ok, f"C1: {c1.checkpoint_id if ok else short(c1)}"):
        return
    time.sleep(1)
    ok, c2 = g.create("C2")
    if not it.check(ok, f"C2: {c2.checkpoint_id if ok else short(c2)}"):
        return
    g.bg(HOG64)
    it.e(f"三棵树 RssAnon(KB): {wait_rss(g, 3)}")
    ok, c3 = g.create("C3")
    if not it.check(ok, f"C3: {c3.checkpoint_id if ok else short(c3)}"):
        return
    mib = lambda x: x / (1 << 20)
    it.e(f"image_bytes: C1={mib(c1.image_bytes):.1f} MiB，C2={mib(c2.image_bytes):.2f} MiB，C3={mib(c3.image_bytes):.1f} MiB；"
         f"trees {c1.process_trees}/{c2.process_trees}/{c3.process_trees}；parent C2={c2.parent_checkpoint_id} C3={c3.parent_checkpoint_id}")
    it.check(128 <= mib(c1.image_bytes) <= 160, f"C1 约 2×64 MiB（128～160）: {mib(c1.image_bytes):.1f}")
    it.check(c2.image_bytes < 0.10 * c1.image_bytes and c2.clear_refs_failed == 0 and c1.clear_refs_failed == 0,
             f"C2/C1 = {c2.image_bytes / c1.image_bytes * 100:.2f}%（<10%），clear_refs_failed C1={c1.clear_refs_failed} C2={c2.clear_refs_failed}")
    it.check(64 <= mib(c3.image_bytes) <= 84 and c3.clear_refs_failed == 0, f"C3 约 64 MiB 加少量（64～84）: {mib(c3.image_bytes):.1f}，clear_refs_failed={c3.clear_refs_failed}")
    per = [(os.path.basename(t.get("dir", "")), t.get("root_pid"), round(mib(t.get("image_bytes", 0)), 2), t.get("parent_id", "")) for t in ckpt_state(g, c3.checkpoint_id).get("trees", [])]
    it.e(f"C3 各树（目录, 根 pid, MiB, parent_id）: {per}")
    ok, r = g.restore(c3.checkpoint_id)
    it.check(ok and len(trees(g.procs())) == 3, f"restore C3 ok={ok}，树 {len(trees(g.procs()))} 棵；last_timings={g.sb.checkpoint.last_timings}")


SOCK_SRV = r'''
import os, socket, time
p = "/home/user/x.sock"
try: os.unlink(p)
except OSError: pass
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.bind(p); s.listen(2)
open("/home/user/srv.ready", "w").write(str(os.getpid()))
c, _ = s.accept()
open("/home/user/srv.accepted", "w").write("1")
while True:
    d = c.recv(100)
    if d: c.send(d)
'''
SOCK_CLI = r'''
import os, socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect("/home/user/x.sock")
open("/home/user/cli.ready", "w").write(str(os.getpid()))
n = 0
while True:
    n += 1; s.send(b"ping"); s.recv(100)
    open("/home/user/cli.cnt", "w").write(str(n)); time.sleep(0.2)
'''
# 清单原文的命令在前；bash 在没有作业控制时会把后台命令的标准输入默认接到 /dev/null，
# 如果 cat 因此读不到管道（立刻退出、形不成第二棵树），改用显式 <&0 的写法。
PIPE_CMDS = ["bash -c '{ while :; do date; sleep 1; done; } | { ( cat > /home/user/out.txt & ); }; wait'",
             "bash -c '{ while :; do date; sleep 1; done; } | { ( cat <&0 > /home/user/out.txt & ); }; wait'"]


def pipe_fds(g, pids):
    return g.out("for p in %s; do echo \"pid $p: $(ls -l /proc/$p/fd 2>/dev/null | awk '/pipe:|socket:/{print $9\"->\"$11}' | tr '\\n' ' ')\"; done" % " ".join(map(str, pids)))


@item("V6", "树间共享管道的预检；对照组；unix socket 探索项；CRIU 对只有一端在树里的管道的行为")
def v6(it):
    # ---- 主测 ----
    g = it.sandbox()
    for k, cmd in enumerate(PIPE_CMDS):
        g.bg(cmd)
        time.sleep(2.5)
        ps = g.procs()
        tr = trees(ps)
        roots = sorted(tr)
        it.e(f"[主测] 命令 #{k + 1}: {cmd} → 根 {roots}，ps {[(p['pid'], p['ppid'], p['comm']) for p in ps]}")
        if len(roots) == 2 and any(p["comm"] == "cat" for p in ps):
            break
        it.e(f"[主测] 命令 #{k + 1} 没有形成'两棵树共用一条管道'的形状，杀掉换下一种写法")
        g.sh("for p in $(ps -u 1000 -o pid=); do kill -9 $p; done; rm -f /home/user/out.txt")
        time.sleep(1.5)
    it.e(f"[主测] 根 {roots}；ps {[(p['pid'], p['ppid'], p['state'], p['args'][:40]) for p in ps]}")
    it.e("[主测] 各进程的管道/socket fd: " + pipe_fds(g, [p["pid"] for p in ps]).replace("\n", " ; "))
    if it.check(len(roots) == 2, f"[主测] 是两个根（bash 一棵、被 PID 1 收养的 cat 一棵）: {roots}"):
        before = g.out(f"cd {SNAP} && ls -d */ 2>/dev/null | tr '\\n' ' '")
        ok, e = g.create("v6-pipe")
        if it.check(not ok, f"[主测] create 应被拒绝，实际 ok={ok} {e.checkpoint_id if ok else ''}"):
            t, code, msg = ecode(e)
            it.e(f"[主测] {t} / {code}: {msg}")
            it.check(t == "SandboxException" and "failed_precondition" in msg, f"[主测] 抛 SandboxException，信息含 failed_precondition（类型 {t}）")
            it.check("share pipe pipe:[" in msg, "[主测] 信息含 'share pipe pipe:['")
            it.check(all(str(r) in msg for r in roots), f"[主测] 信息含两个根 pid {roots}")
            it.check("nothing was changed" in msg, "[主测] 信息含 'nothing was changed'")
        after = g.out(f"cd {SNAP} && ls -d */ 2>/dev/null | tr '\\n' ' '")
        it.check(before == after, f"[主测] snapshot 目录没有新目录: 前 {before.split()} 后 {after.split()}；全部内容 {g.out(f'ls -A {SNAP}').split()}")
        ps2 = g.procs()
        it.check(len(ps2) >= 2 and all(p["state"] != "T" for p in ps2) and sorted(trees(ps2)) == roots, f"[主测] 进程都在跑: {fmt_ps(ps2)}")
        s1 = g.out("stat -c %s /home/user/out.txt"); time.sleep(2.2); s2 = g.out("stat -c %s /home/user/out.txt")
        it.check(int(s2) > int(s1), f"[主测] 管道仍通：out.txt {s1} → {s2} 字节")
        it.check(health_ok(g), f"[主测] health {g.health()}；list={len(g.sb.checkpoint.list())}")

        # ---- 前提确认：手工 criu dump，只有一端在树里的匿名管道 ----
        for k, r in enumerate(roots):
            d = f"/run/criu-manual/t{k}"
            rc, o, er = g.sh(f"mkdir -p {d} && /usr/sbin/criu dump --tree {r} -D {d} --shell-job --leave-running -v2 -o dump.log; rc=$?; "
                             f"echo rc=$rc; ls {d} | tr '\\n' ' '; echo; grep -n -i -E 'Error|Warn|pipe' {d}/dump.log | tail -12; echo ---tail; tail -4 {d}/dump.log")
            it.e(f"[前提] 手工 criu dump --tree {r} --leave-running（树 t{k}，它只有管道的一端）:\n{short(o, 1800)}")
            it.check("rc=0" in o.splitlines()[0] if o else False, f"[前提] 只有一端在树里的匿名管道：criu dump --tree {r} 成功（rc=0）")
        s1 = g.out("stat -c %s /home/user/out.txt"); time.sleep(2.2); s2 = g.out("stat -c %s /home/user/out.txt")
        it.e(f"[前提] 手工 dump（--leave-running）之后管道仍通：out.txt {s1} → {s2}")
        # 管道是不是真的静默断开（预检存在的理由）：先 dump 读端那棵（cat，不带 --leave-running，criu dump 完就杀掉它，
        # out.txt 从此不再变），马上再 dump 写端那棵（同样杀掉），然后两棵都 restore，看 out.txt 还长不长。
        rc, o, er = g.sh(f"cd /run/criu-manual && mkdir b0 b1 && /usr/sbin/criu dump --tree {roots[1]} -D b1 --shell-job -v2 -o dump.log; echo \"dump 读端树 rc=$?\"; "
                         f"/usr/sbin/criu dump --tree {roots[0]} -D b0 --shell-job -v2 -o dump.log; echo \"dump 写端树 rc=$?\"; "
                         "echo \"dump 后用户进程数 $(ps -u 1000 -o pid= | wc -l)，out.txt $(stat -c %s /home/user/out.txt)\"; "
                         "for k in 0 1; do /usr/sbin/criu restore -D b$k --restore-detached --shell-job -v2 -o restore.log; echo \"restore b$k rc=$?\"; "
                         "grep -E 'Error' b$k/restore.log | tail -3; done; "
                         "s1=$(stat -c %s /home/user/out.txt); sleep 3.2; s2=$(stat -c %s /home/user/out.txt); echo \"restore 后 out.txt $s1 -> $s2（3.2 秒）\"; "
                         "ps -u 1000 -o pid=,ppid=,stat=,args= | cut -c1-70; "
                         "for p in $(ps -u 1000 -o pid=); do echo \"pid $p: $(ls -l /proc/$p/fd 2>/dev/null | awk '/pipe:/{print $9\"->\"$11}' | tr '\\n' ' ')\"; done")
        it.e(f"[前提] 手工分别 dump 再分别 restore 两棵树之后（INFO：CRIU 各自都成功，但两棵树之间的管道不再是同一条）:\n{short(o, 2200)}")
        g.sh("for p in $(ps -u 1000 -o pid=); do kill -9 $p; done; rm -rf /run/criu-manual")

    # ---- 对照组 ----
    g2 = it.sandbox()
    g2.bg("bash -c '(sleep 1000 &); (sleep 1000 &); sleep 1000'")
    time.sleep(1.5)
    ps = g2.procs()
    tr = trees(ps)
    it.e(f"[对照] 根 {sorted(tr)}；ps {fmt_ps(ps)}；fd: " + pipe_fds(g2, [p["pid"] for p in ps]).replace("\n", " ; "))
    ok, cp = g2.create("v6-ctl")
    if it.check(ok and cp.process_trees == 3, f"[对照] create 不被拒绝，process_trees={getattr(cp, 'process_trees', None)} {'' if ok else short(cp)}"):
        ok, r = g2.restore(cp.checkpoint_id)
        ps2 = g2.procs()
        it.check(ok and sorted(trees(ps2)) == sorted(tr) and all(p["state"] != "T" for p in ps2), f"[对照] restore ok={ok} {'' if ok else short(r)}；ps {fmt_ps(ps2)}")

    # ---- 探索项：两棵树之间的 unix socket 连接 ----
    g3 = it.sandbox()
    g3.write("/home/user/srv.py", SOCK_SRV)
    g3.write("/home/user/cli.py", SOCK_CLI)
    g3.bg("python3 /home/user/srv.py")
    g3.wait_file("/home/user/srv.ready")
    g3.bg("python3 /home/user/cli.py")
    g3.wait_file("/home/user/cli.ready")
    g3.wait_file("/home/user/srv.accepted")
    ps = g3.procs()
    it.e(f"[探索] unix socket：根 {sorted(trees(ps))}；fd: " + pipe_fds(g3, [p["pid"] for p in ps]).replace("\n", " ; "))
    before = g3.out(f"cd {SNAP} && ls -d */ 2>/dev/null | tr '\\n' ' '")
    ok, e = g3.create("v6-sock")
    if ok:
        it.e(f"[探索] create 成功了（期望是 Internal）: process_trees={e.process_trees} id={e.checkpoint_id}")
        ok2, r = g3.restore(e.checkpoint_id)
        time.sleep(1)
        it.e(f"[探索] restore ok={ok2} {'' if ok2 else short(r, 1200)}；之后 ps {fmt_ps(g3.procs())}；cli.cnt {g3.out('cat /home/user/cli.cnt')}")
        it.check(False, "[探索] 期望 create 返回 Internal（external socket），实际成功")
    else:
        t, code, msg = ecode(e)
        it.e(f"[探索] {t} / {code}: {short(msg, 2500)}")
        it.check("internal" in msg.lower().split(":")[0] or code == "internal" or msg.startswith("internal"), f"[探索] 返回 Internal（{t}，开头 {msg[:40]!r}）")
        it.check("process tree t" in msg, "[探索] 信息含 'process tree t…'")
        it.check("criu-dump.log" in msg or "Error" in msg, "[探索] 信息带 criu 日志尾部")
        errs = [l for l in msg.splitlines() if "Error" in l or "error" in l.lower() and "sk" in l]
        it.e("[探索] CRIU 的实际报错行: " + " || ".join(errs[:6]))
        ps2 = g3.procs()
        c1 = g3.out("cat /home/user/cli.cnt"); time.sleep(1); c2 = g3.out("cat /home/user/cli.cnt")
        it.check(all(p["state"] != "T" for p in ps2) and int(c2) > int(c1), f"[探索] 失败后进程被放开且连接仍通: {fmt_ps(ps2)}，cli.cnt {c1} → {c2}")
        now = g3.out(f"cd {SNAP} && ls -d */ 2>/dev/null | tr '\\n' ' '")
        it.check(now == before and health_ok(g3), f"[探索] 镜像目录已清掉、health ok: 目录 {now.split()}，全部内容 {g3.out(f'ls -A {SNAP}').split()} {g3.health()}")
        ok, c = g3.create("v6-sock-again")
        it.e(f"[探索] 再 create 一次（应同样失败、可重复）: ok={ok} {short(c, 200) if not ok else c.checkpoint_id}")


@item("V7", "restore 中途失败：t1 损坏 → DataLoss、degraded；restore 另一个 checkpoint 后解除")
def v7(it):
    g = it.sandbox()
    start_counters(g, ["c0", "c1"])
    ok, c1 = g.create("C1")
    time.sleep(0.5)
    ok2, c2 = g.create("C2")
    if not it.check(ok and ok2 and c2.process_trees == 2, f"C1={getattr(c1, 'checkpoint_id', short(c1))} C2={getattr(c2, 'checkpoint_id', short(c2))}"):
        return
    r0 = counter_roots(g)
    d = f"{SNAP}/{c2.checkpoint_id}_mem/t1"
    it.e("破坏前: " + g.out(f"ls -la {d}/pstree.img {d}/inventory.img | tr '\\n' ';'"))
    g.sh(f"head -c 256 /dev/urandom > {d}/pstree.img")
    ok, e = g.restore(c2.checkpoint_id)
    if not it.check(not ok, f"restore C2 应失败，实际 ok={ok}"):
        return
    t, code, msg = ecode(e)
    it.e(f"{t} / {code}: {short(msg, 2500)}")
    it.check(t == "CheckpointRestoreException", f"抛 CheckpointRestoreException（实际 {t}）")
    it.check("already restored and left running: t0" in msg, "信息含 'already restored and left running: t0 …'")
    it.check("failed for process tree t1" in msg, "信息含 'failed for process tree t1 …'")
    h = g.health()
    it.check('"degraded"' in h, f"health degraded: {h}")
    ps = g.procs()
    it.e(f"失败后的用户进程（t0 应已恢复并在跑）: {fmt_ps(ps)}；根 {counter_roots(g)}")
    ok, e = g.create("refused")
    it.check(not ok and "degraded" in str(e), f"create 被拒绝: ok={ok} {short(e, 300)}")
    ok, r = g.restore(c1.checkpoint_id)
    time.sleep(0.3)
    it.check(ok and counter_roots(g) == r0, f"restore C1 ok={ok} {'' if ok else short(r)}；根 {counter_roots(g)}（应为 {r0}）")
    it.check(health_ok(g), f"degraded 解除: {g.health()}")
    ok, c3 = g.create("after")
    it.check(ok and c3.process_trees == 2, f"之后 create 正常: ok={ok} trees={getattr(c3, 'process_trees', short(c3))} parent={getattr(c3, 'parent_checkpoint_id', None)}")
    x1 = cnts(g, ["c0", "c1"]); time.sleep(1); x2 = cnts(g, ["c0", "c1"])
    it.check(all(x2[k] > x1[k] for k in x1), f"两个计数器都在走: {x1} → {x2}")


TMPW = r'''
import os, time
f = open("/tmp/f", "a"); n = 0
open("/tmp/f.pid", "w").write(str(os.getpid()))
while True:
    n += 1; f.write("line %d\n" % n); f.flush(); time.sleep(0.1)
'''


@item("V8", "/tmp 回滚")
def v8(it):
    g = it.sandbox()
    g.sh("echo v1 > /tmp/a", user="user")
    g.write("/home/user/tmpw.py", TMPW)
    g.bg("python3 /home/user/tmpw.py")
    g.wait_file("/tmp/f.pid")
    time.sleep(0.5)
    ok, cp = g.create("v8")
    if not it.check(ok, f"create: {cp.checkpoint_id if ok else short(cp)}"):
        return
    n0 = g.out("wc -l < /tmp/f")
    time.sleep(1)
    rc = g.sh("echo v2 > /tmp/a; touch /tmp/b; rm /tmp/f; ls -la /tmp/a /tmp/b /tmp/f 2>&1", user="user")
    it.e(f"create 时 /tmp/f 约 {n0} 行；改动后: {rc[1].strip()!r} {rc[2].strip()!r}")
    ok, r = g.restore(cp.checkpoint_id)
    if not it.check(ok, f"restore: {r if ok else short(r)}"):
        return
    it.check(g.out("cat /tmp/a") == "v1", f"/tmp/a = {g.out('cat /tmp/a')!r}（应为 v1）")
    it.check(g.sh("test -e /tmp/b")[0] != 0, "/tmp/b 不存在")
    n1 = g.out("wc -l < /tmp/f 2>&1")
    time.sleep(1)
    n2 = g.out("wc -l < /tmp/f 2>&1")
    it.check(n1.isdigit() and n2.isdigit() and int(n2) > int(n1) and int(n1) <= int(n0) + 8, f"/tmp/f 回来了并继续被写: create 时 ≈{n0} 行，restore 后 {n1} → 1 秒后 {n2}")
    pid = g.out("cat /tmp/f.pid")
    it.check(any(str(p["pid"]) == pid and p["state"] != "T" for p in g.procs()), f"写 /tmp/f 的进程 {pid} 在跑；它的 fd: {g.out(f'ls -l /proc/{pid}/fd | grep tmp')}")
    it.check(g.out("stat -c '%a %U:%G' /tmp") == "1777 root:root", "stat /tmp = " + g.out("stat -c '%a %U:%G' /tmp"))
    df = g.out("df /tmp | tail -1")
    it.check(df.split()[0] == "overlay", f"df /tmp: {df}")
    it.e("/tmp 内容: " + g.out("ls -A /tmp | tr '\\n' ' '"))


@item("V9", "tmp.mount 被 mask，/tmp 上没有别的挂载，新沙箱的 /tmp 没有构建残留")
def v9(it):
    g = it.sandbox()
    fm = g.out("findmnt -n /tmp")
    it.check(fm == "", f"findmnt -n /tmp 无输出: {fm!r}")
    en = g.out("systemctl is-enabled tmp.mount 2>&1")
    it.check(en == "masked", f"systemctl is-enabled tmp.mount = {en!r}；{g.out('ls -l /etc/systemd/system/tmp.mount')}")
    failed = g.out("systemctl --failed --no-legend --plain 2>&1")
    it.check("tmp" not in failed, f"systemctl --failed: {failed!r}")
    if failed:
        it.e(f"（注意）有失败的 unit，但与 tmp 无关: {failed}")
    ls = g.out("ls -A /tmp")
    bad = [x for x in ls.split() if x.endswith(".tar") or (len(x) >= 32 and all(c in "0123456789abcdef" for c in x))]
    it.check(not bad, f"ls -A /tmp 没有 *.tar 或哈希目录: {ls.split()}")


@item("V10", "/ 是 755 root:root，create/restore 之后不变；user 不能在 / 下建文件；服务与 SDK 基本功能正常")
def v10(it):
    g = it.sandbox()
    s0 = g.out("stat -c '%a %U:%G' /")
    it.check(s0 == "755 root:root", f"开机 stat / = {s0}")
    rc, o, er = g.sh("touch /x", user="user")
    it.check(rc != 0 and "Permission denied" in (o + er), f"user touch /x: rc={rc} {er.strip() or o.strip()}")
    g.bg("sleep 51001")
    time.sleep(0.5)
    ok, cp = g.create("v10")
    s1 = g.out("stat -c '%a %U:%G' /")
    it.check(ok and s1 == "755 root:root", f"create 后 stat / = {s1}（ok={ok}）")
    g.sh("echo x > /home/user/v10", user="user")
    ok, cp2 = g.create("v10b")
    s1b = g.out("stat -c '%a %U:%G' /")
    ok, r = g.restore(cp.checkpoint_id)
    s2 = g.out("stat -c '%a %U:%G' /")
    it.check(ok and s2 == "755 root:root" and s1b == "755 root:root", f"第二次 create 后 {s1b}；restore 后 stat / = {s2}（ok={ok}）")
    ok, r = g.restore(cp2.checkpoint_id)
    s3 = g.out("stat -c '%a %U:%G' /")
    it.check(ok and s3 == "755 root:root", f"restore 第二个 checkpoint 后 stat / = {s3}")
    rc, o, er = g.sh("touch /x", user="user")
    it.check(rc != 0 and "Permission denied" in (o + er), f"restore 后 user touch /x: rc={rc} {er.strip() or o.strip()}")
    act = g.out("systemctl is-active ssh envd gsd 2>&1 | tr '\\n' ' '")
    en = g.out("systemctl is-enabled ssh 2>&1; ls /usr/sbin/sshd 2>&1 | head -1")
    a = act.split()
    it.check(len(a) == 3 and a[1] == "active" and a[2] == "active", f"systemctl is-active ssh envd gsd = {act}")
    it.e(f"ssh 现状: is-active={a[0] if a else '?'}；is-enabled / sshd: {en!r}")
    u = g.sh("id -un; echo hi > /home/user/u.txt && cat /home/user/u.txt", user="user")
    rt = g.sh("id -un; touch /rootfile && ls -l /rootfile && rm /rootfile", user="root")
    it.check(u[0] == 0 and u[1].split()[:2] == ["user", "hi"], f"commands.run(user): rc={u[0]} {u[1].strip()!r}")
    it.check(rt[0] == 0 and rt[1].split()[0] == "root", f"commands.run(root): rc={rt[0]} {rt[1].strip()!r}")
    g.sb.files.write("/home/user/x", "filedata")
    it.check(g.sb.files.read("/home/user/x") == "filedata", f"files.write('/home/user/x') + read 正常；{g.out('stat -c %U:%a /home/user/x')}")
    it.e(f"模板构建成功：本沙箱就是从 {args.template} 起的（构建日志见结果目录 build-*.log）")
    it.e("权限相关目录: " + g.out("stat -c '%n %a %U:%G' / /tmp /home /home/user /var/tmp /run | tr '\\n' ';'"))


@item("V11", "gsd 重启后的状态（state.json 版本 2、list、增量、restore）")
def v11(it):
    g = it.sandbox()
    start_counters(g, ["c0", "c1"])
    ok, c1 = g.create("C1")
    if not it.check(ok and c1.process_trees == 2, f"C1: {c1.checkpoint_id if ok else short(c1)}"):
        return
    pid0 = g.out("systemctl show -p MainPID --value gsd")
    g.sh("systemctl restart gsd")
    h = g.wait_health("ok")
    pid1 = g.out("systemctl show -p MainPID --value gsd")
    it.check(pid0 != pid1 and '"ok"' in h, f"gsd 重启: MainPID {pid0} → {pid1}，health {h}")
    st = g.state()
    it.check(st.get("version") == 2, f"state.json version = {st.get('version')}；last_trees={st.get('last_trees')}")
    ls = g.sb.checkpoint.list()
    it.check(len(ls) == 1 and ls[0].process_trees == 2 and ls[0].has_process_image, f"list: {[(c.checkpoint_id, c.process_trees, c.has_process_image, c.image_bytes) for c in ls]}")
    ok, c2 = g.create("C2")
    if not it.check(ok, f"重启后 create: {c2.checkpoint_id if ok else short(c2)}"):
        return
    inc = [j for j in g.jlines("using incremental CRIU dump") if j.get("checkpoint_id") == c2.checkpoint_id]
    it.check(c2.parent_checkpoint_id == c1.checkpoint_id and len(inc) == 2 and c2.process_trees == 2,
             f"重启后的 create 是增量: parent={c2.parent_checkpoint_id}，incremental 日志 {len(inc)} 条，image_bytes C1={c1.image_bytes} C2={c2.image_bytes}")
    for name, c in (("C2", c2), ("C1", c1)):
        ok, r = g.restore(c.checkpoint_id)
        time.sleep(0.3)
        x1 = cnts(g, ["c0", "c1"]); time.sleep(1); x2 = cnts(g, ["c0", "c1"])
        it.check(ok and len(counter_roots(g)) == 2 and all(x2[k] > x1[k] for k in x1), f"restore {name}: ok={ok} {'' if ok else short(r)}；根 {counter_roots(g)}；计数 {x1} → {x2}")
    it.check(health_ok(g), f"health {g.health()}")


def p50(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    return round(xs[len(xs) // 2], 2) if xs else None


def perf_once(tpl, n, mb=192):
    """一个沙箱：n 棵 192 MiB memhog；cp0 → 全部改写 → cp1 → restore cp1 × R（去掉第 1 次）→ create × C。"""
    g = G(tpl=tpl)
    try:
        tags = [f"h{k}" for k in range(n)]
        for t in tags:
            vf.start_hog(g, mb, "full", t)
        ok, cp0 = g.create("cp0")
        if not ok:
            raise RuntimeError(f"cp0: {cp0}")
        for t in tags:
            vf.hog_cmd(g, t, 1)
        ok, cp1 = g.create("cp1")
        if not ok:
            raise RuntimeError(f"cp1: {cp1}")
        rs, cs = [], []
        for i in range(args.perf_restores):
            ok, r = g.restore(cp1.checkpoint_id)
            if not ok:
                raise RuntimeError(f"restore {i}: {r}")
            rs.append(dict(g.sb.checkpoint.last_timings or {}))
            time.sleep(0.3)
        for i in range(args.perf_creates):
            ok, c = g.create(f"pc{i}")
            if not ok:
                raise RuntimeError(f"create {i}: {c}")
            cs.append(dict(c.timings or {}))
            time.sleep(0.3)
        alive = len(trees(g.procs()))
        return {"template": tpl, "n": n, "mb_per_tree": mb, "sandbox": g.id, "gsd": g.out("/usr/bin/gsd -commit"), "cp0": dict(cp0.timings or {}), "cp1": dict(cp1.timings or {}),
                "cp0_image_bytes": cp0.image_bytes, "cp1_image_bytes": cp1.image_bytes, "restores": rs[1:], "creates": cs, "trees_alive": alive}
    finally:
        g.kill()


def perf_summary(runs):
    res = {}
    for kind, keys in (("restores", ("total_ms", "criu_restore_ms", "kill_ms", "overlay_restore_ms", "find_pids_ms", "precheck_ms")),
                       ("creates", ("total_ms", "criu_dump_ms", "stopped_ms", "overlay_sink_ms", "find_pids_ms", "precheck_ms"))):
        for k in keys:
            xs = [d.get(k) for r in runs for d in r[kind]]
            res[f"{kind[:-1]}.{k}"] = p50(xs)
    res["cp0_full.criu_dump_ms"] = p50([r["cp0"].get("criu_dump_ms") for r in runs])
    res["cp0_full.total_ms"] = p50([r["cp0"].get("total_ms") for r in runs])
    res["cp0_full.stopped_ms"] = p50([r["cp0"].get("stopped_ms") for r in runs])
    res["cp1_alldirty.criu_dump_ms"] = p50([r["cp1"].get("criu_dump_ms") for r in runs])
    res["cp1_alldirty.stopped_ms"] = p50([r["cp1"].get("stopped_ms") for r in runs])
    res["n_restores"] = sum(len(r["restores"]) for r in runs)
    res["n_creates"] = sum(len(r["creates"]) for r in runs)
    res["per_round_criu_restore_p50"] = [p50([d.get("criu_restore_ms") for d in r["restores"]]) for r in runs]
    return res


@item("V12", "性能：N=1 与旧 gsd（同内核）对比；N=2、3 的各分段")
def v12(it):
    # new1/old1：1 棵 192 MiB（与历次基线同形状）；m1/m2/m3：1/2/3 棵、每棵 64 MiB（2 GiB guest 放得下 3 棵的两套镜像）
    runs = {"new1": [], "old1": [], "m1": [], "m2": [], "m3": []}
    for rd in range(args.perf_rounds):
        order = [("new1", args.template, 1)] + ([("old1", args.old_template, 1)] if args.old_template else [])
        if rd % 2:
            order.reverse()
        order = [(k, t, n, 192) for k, t, n in order] + [("m1", args.template, 1, 64), ("m2", args.template, 2, 64), ("m3", args.template, 3, 64)]
        for key, tpl, n, mb in order:
            r = perf_once(tpl, n, mb)
            runs[key].append(r)
            log(f"    轮 {rd + 1} {key} [{tpl} gsd={r['gsd']} N={n}] criu_restore p50={p50([d.get('criu_restore_ms') for d in r['restores']])} "
                f"total p50={p50([d.get('total_ms') for d in r['restores']])} 存活树 {r['trees_alive']}")
    summ = {k: perf_summary(v) for k, v in runs.items() if v}
    PERF.update({"runs": runs, "summary": summ, "rounds": args.perf_rounds, "restores_per_sandbox": args.perf_restores - 1, "creates_per_sandbox": args.perf_creates,
                 "workload": "new1/old1：1 棵 192 MiB memhog；m1/m2/m3：1/2/3 棵、每棵 64 MiB；cp0 全量 → 全部改写 → cp1 → restore cp1（去掉第 1 次）→ 连续 create（不改内存）"})
    for k, s in summ.items():
        it.e(f"{k}: {json.dumps(s, ensure_ascii=False)}")
    it.check(all(r["trees_alive"] == r["n"] for v in runs.values() for r in v), "每个沙箱结束时树的数量都对")
    if "old1" in summ:
        new, old = summ["new1"], summ["old1"]
        keys = ("restore.total_ms", "restore.criu_restore_ms", "restore.find_pids_ms", "restore.precheck_ms",
                "create.total_ms", "create.criu_dump_ms", "create.find_pids_ms", "create.precheck_ms")
        for k in keys:
            a, b = new.get(k), old.get(k)
            if a is None or b is None:
                it.e(f"N=1 {k}: 新 {a} / 旧 {b}（有一边没有这个字段）")
                continue
            it.check(abs(a - b) <= 3.0, f"N=1 {k} p50: 新 {a} / 旧 {b}，差 {a - b:+.2f} ms（判据 ±3）")
    else:
        it.e("没有给对照模板，N=1 只记录数值")
    for n, k in ((1, "m1"), (2, "m2"), (3, "m3")):
        s = summ[k]
        it.e(f"N={n}（每棵 64 MiB）: criu_dump_ms(增量,不改内存)={s['create.criu_dump_ms']} stopped_ms={s['create.stopped_ms']} precheck_ms={s['create.precheck_ms']} "
             f"criu_restore_ms={s['restore.criu_restore_ms']} restore total={s['restore.total_ms']} | cp0 全量 dump={s['cp0_full.criu_dump_ms']} stopped={s['cp0_full.stopped_ms']} "
             f"| cp1 全脏 dump={s['cp1_alldirty.criu_dump_ms']} stopped={s['cp1_alldirty.stopped_ms']}")


# ============================== 主流程 ==============================

def main():
    log(f"template={args.template} old-template={args.old_template}")
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

    print("\n" + "=" * 100)
    print(f"{'项目':<6} {'结果':<6} {'秒':>5}  说明")
    print("-" * 100)
    for it in RESULTS:
        print(f"{it.iid:<6} {it.status:<6} {it.secs:5.0f}  {it.title}")
        for ev in it.ev:
            if ev.startswith("BAD "):
                print(f"{'':20}{short(ev, 300)}")
    bad = [it.iid for it in RESULTS if it.status == "FAIL"]
    print("-" * 100)
    print(f"合计 {len(RESULTS)} 项，FAIL {len(bad)}: {bad}")
    print(f"慢调用（墙钟比 gsd 自报多 1.5 秒以上）{len(SLOW)} 次: {[(e['op'], e['wall_ms'], e['gsd_total_ms'], e['sandbox']) for e in SLOW]}")
    if args.out_json:
        json.dump({"template": args.template, "old_template": args.old_template,
                   "items": [{"id": it.iid, "title": it.title, "status": it.status, "secs": round(it.secs, 1), "evidence": it.ev} for it in RESULTS],
                   "slow_calls": SLOW, "perf": PERF}, open(args.out_json, "w"), ensure_ascii=False, indent=1)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
