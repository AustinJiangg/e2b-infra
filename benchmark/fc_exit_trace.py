"""原生 pause 里“停 Firecracker 到进程退出”拆开看：内核 ftrace 只跟踪这个 Firecracker 的线程。

原生 pause 要等 Firecracker 进程退出才能断开根盘、导出根盘差分（ExportDiff），orchestrator 的
pause.phases 里 stop_begin → fc_exited 这一段在 950 / 920B 上都要 170～200 ms，占 pause 的七成以上。
这一段从 orchestrator 发 SIGTERM 开始，到内核把进程收完、Go 的 Wait 返回为止，用户态看不到里面。

做法（只在进程级栈上，原生路径与上游相同）：
  1. 新建沙箱，起常驻进程写满 P MiB 内存（与 ckpt_same_workload 的 mix 同一负载，不写文件），等 ext4 初始化；
  2. 找到这个沙箱的 Firecracker 进程（命令行里带沙箱 ID），把它的全部线程设进 set_ftrace_pid；
  3. function_graph 只跟踪退出路径上的一组函数（FUNCS：do_exit、释放地址空间、关文件与 KVM、销毁
     命名空间、等 RCU 等），另开三个事件：signal_generate（SIGTERM 发给 Firecracker）、
     sched_process_exit（各线程退出）、sched_process_exec（看 orchestrator 起的 ps）；时钟用 mono；
  4. sb.pause()，关跟踪，读出 trace，按线程重建调用树；然后 connect 回来，下一次 pause 换新的 Firecracker。
每个测点一个沙箱，第 0 次 pause 导出的是自启动以来的全部写入，单列不进统计（与 ckpt_same_workload 一致）。

输出（每个测点 p50）：
  orchestrator 记的“停 FC 到进程退出”（stop_begin → fc_exited）；
  内核时间线：发 SIGTERM 前 orchestrator 先跑一次 ps 查进程状态（sched_process_exec → exit；f66dbe2c1 起
  改读 /proc、不再起 ps，这一项显示为“-”）、
  SIGTERM → 第一个线程进入 do_exit、SIGTERM → 主线程退出完（它最后一条记录 exit_notify；do_exit 不返回，
  function_graph 记不到结束行，还开着的帧都按线程的最后一条记录收尾）；
  退出最慢的那个线程的 do_exit 调用树（各函数含子函数的耗时，<0.3 ms 的不列），其他线程 do_exit 的最大值。

跟踪本身有开销（只在列出的函数进出时记一条），各段会比不跟踪时略长，看的是比例和归属。
ftrace 是全机共用的：开跑前 current_tracer 不是 nop、或 set_ftrace_filter 不空，就当作有人在用，不跑。
结束（包括出错、Ctrl-C）时把 tracer、过滤、pid、事件、选项、时钟都恢复成默认。

在部署宿主上用 root、进程级栈的 SDK 环境运行（test950.sh fcexit 会切栈、清 netns 后调用它）：
    python3 fc_exit_trace.py --out result.json [--points 0,192] [--reps 5]
"""
import argparse
import json
import os
import re
import socket
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ckpt_same_workload as w  # noqa: E402

T = "/sys/kernel/tracing"
# 退出路径上的函数。不存在的跳过（不同内核版本名字不同）；只跟踪这些函数，调用树也只由它们组成。
FUNCS = [
    "do_group_exit", "do_exit", "exit_signals", "exit_mm", "mmput", "exit_mmap", "unmap_vmas", "free_pgtables",
    "__mmu_notifier_release", "kvm_mmu_notifier_release", "exit_files", "exit_fs", "exit_task_namespaces",
    "free_nsproxy", "put_mnt_ns", "namespace_unlock", "synchronize_rcu", "synchronize_rcu_expedited",
    "task_work_run", "__fput", "kvm_vm_release", "kvm_vcpu_release", "kvm_put_kvm", "kvm_destroy_vm",
    "kvm_arch_destroy_vm", "kvm_destroy_vcpus", "kvm_uninit_stage2_mmu", "kvm_free_stage2_pgd",
    "kvm_irqfd_release", "userfaultfd_release", "exit_notify", "release_task", "cgroup_exit",
    "perf_event_exit_task", "exit_sem", "exit_thread", "__unmap_hugepage_range", "remove_vma",
]
OPTIONS = {"funcgraph-abstime": "1", "funcgraph-proc": "1", "funcgraph-tail": "1", "funcgraph-irqs": "0"}
EVENTS = {"signal/signal_generate": "sig == 15", "sched/sched_process_exit": None, "sched/sched_process_exec": None}
MIN_SHOW_MS = 0.3


def rd(name):
    with open(os.path.join(T, name)) as f:
        return f.read()


def wr(name, value, append=False):
    with open(os.path.join(T, name), "a" if append else "w") as f:
        f.write(value)


class Ftrace:
    """全机唯一的 ftrace：只在 with 块里改，退出时全部恢复默认。"""

    def __enter__(self):
        if not os.path.isdir(T):
            raise SystemExit("没有 %s（tracefs 没挂载）" % T)
        cur = rd("current_tracer").strip()
        filt = [l for l in rd("set_ftrace_filter").splitlines() if l.strip() and not l.startswith("#")]
        if cur != "nop" or filt:
            raise SystemExit("ftrace 正在被使用（current_tracer=%s，set_ftrace_filter %d 项），不跑" % (cur, len(filt)))
        self.funcs = []
        avail = set()
        with open(os.path.join(T, "available_filter_functions")) as f:
            for line in f:
                avail.add(line.split()[0])
        for fn in FUNCS:
            if fn in avail:
                try:
                    wr("set_ftrace_filter", fn + "\n", append=True)
                    self.funcs.append(fn)
                except OSError:
                    pass
        for opt, v in OPTIONS.items():
            wr("options/" + opt, v)
        wr("trace_clock", "mono")
        for ev, flt in EVENTS.items():
            if flt:
                wr("events/%s/filter" % ev, flt)
            wr("events/%s/enable" % ev, "1")
        wr("tracing_on", "0")
        return self

    def arm(self, tids):
        wr("set_ftrace_pid", " ".join(str(t) for t in tids))
        wr("trace", "")
        wr("current_tracer", "function_graph")
        wr("tracing_on", "1")

    def disarm(self):
        wr("tracing_on", "0")
        text = rd("trace")
        wr("current_tracer", "nop")
        return text

    def __exit__(self, *exc):
        for step in (lambda: wr("tracing_on", "0"), lambda: wr("current_tracer", "nop"),
                     lambda: wr("set_ftrace_filter", ""), lambda: wr("set_ftrace_pid", ""),
                     lambda: wr("trace_clock", "local"), lambda: wr("trace", ""), lambda: wr("tracing_on", "1")):
            try:
                step()
            except OSError:
                pass
        for ev, flt in EVENTS.items():
            for name, v in (("enable", "0"), ("filter", "0")):
                if name == "filter" and not flt:
                    continue
                try:
                    wr("events/%s/%s" % (ev, name), v)
                except OSError:
                    pass
        for opt, v in (("funcgraph-abstime", "0"), ("funcgraph-proc", "0"), ("funcgraph-tail", "0"),
                       ("funcgraph-irqs", "1")):
            try:
                wr("options/" + opt, v)
            except OSError:
                pass
        return False


def fc_pid(sid):
    """这个沙箱的 Firecracker 进程（命令行里带沙箱 ID）。"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            comm = open("/proc/%s/comm" % pid).read().strip()
            cmd = open("/proc/%s/cmdline" % pid, "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        if comm.startswith("firecracker") and sid in cmd:
            return int(pid)
    return None


def tids_of(pid):
    try:
        return sorted(int(t) for t in os.listdir("/proc/%d/task" % pid))
    except OSError:
        return [pid]


LINE = re.compile(r"^\s*(\d+\.\d+)\s*\|\s*\d+\)\s*(.*?)-(\d+)\s*\|\s*(?:[+!#*@$]\s*)?([\d.]+)?\s*(?:us)?\s*\|(.*)$")
EVENT = re.compile(r"/\*\s*(\w+):\s*(.*?)\s*\*/")


def parse(text):
    """function_graph 输出 → (调用记录, 事件)。调用记录 (tid, comm, 深度, 函数, 开始 s, 耗时 ms, 路径)。"""
    calls, events, stacks, last, comms = [], [], {}, {}, {}
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        ts, comm, tid, dur, body = float(m.group(1)), m.group(2).strip(), int(m.group(3)), m.group(4), m.group(5)
        last[tid], comms[tid] = ts, comm
        ev = EVENT.search(body)
        if ev and "(" not in body.split("/*")[0]:
            fields = dict(kv.split("=", 1) for kv in ev.group(2).split() if "=" in kv)
            events.append({"ts": ts, "tid": tid, "name": ev.group(1), **fields})
            continue
        depth = (len(body) - len(body.lstrip(" ")) - 1) // 2
        name = body.strip()
        st = stacks.setdefault(tid, [])
        if name.endswith("{"):
            st.append((name.split("(")[0], ts))
        elif name.startswith("}"):
            # 结束行带函数名（funcgraph-tail）：按名字出栈。线程中途换 CPU 时，各 CPU 缓冲区合并后偶尔会
            # 出现入口没记上的结束行，按位置出栈会把外层（如 do_exit）提前弹掉
            tail = re.search(r"/\*\s*(\S+)\s*\*/", name)
            want = tail.group(1) if tail else (st[-1][0] if st else None)
            if want not in [f[0] for f in st]:
                continue
            while st:
                fn, t0 = st.pop()
                if fn == want:
                    break
            ms = float(dur) / 1000 if dur else (ts - t0) * 1000
            calls.append((tid, comm, len(st), fn, t0, ms, "/".join([s[0] for s in st] + [fn])))
        elif name.endswith(";"):
            fn = name.split("(")[0]
            ms = float(dur) / 1000 if dur else 0.0
            calls.append((tid, comm, len(st), fn, ts - ms / 1000, ms, "/".join([s[0] for s in st] + [fn])))
    # do_exit 不返回（线程在里面结束），function_graph 记不到它的结束行：还开着的帧按这个线程的最后一条记录收尾
    for tid, st in stacks.items():
        while st:
            fn, t0 = st.pop()
            calls.append((tid, comms[tid], len(st), fn, t0, (last[tid] - t0) * 1000,
                          "/".join([s[0] for s in st] + [fn])))
    return calls, events


def analyze(calls, events, tgid):
    """一次 pause 的退出拆分：{项: ms}。"""
    sig = [e for e in events if e["name"] == "signal_generate" and int(e.get("pid", -1)) == tgid]
    if not sig:
        return None
    t_sig = sig[0]["ts"]
    out = {}
    # 发 SIGTERM 之前 orchestrator 用 ps 查一次进程状态：取 SIGTERM 之前最近一次 ps 的 exec → exit
    execs = [e for e in events if e["name"] == "sched_process_exec" and e.get("filename", "").endswith("/ps")
             and e["ts"] < t_sig]
    if execs:
        ps = execs[-1]
        ps_exit = [e["ts"] for e in events if e["name"] == "sched_process_exit" and e.get("pid") == ps.get("pid")]
        if ps_exit:
            out["ps 查进程状态"] = (ps_exit[0] - ps["ts"]) * 1000
            out["ps 结束 → 发 SIGTERM"] = (t_sig - ps_exit[0]) * 1000
    mine = [c for c in calls if c[0] == tgid and c[4] >= t_sig]
    if mine:   # 主线程最后一条记录是 exit_notify：这之后父进程（orchestrator）就能收走它
        out["SIGTERM → 主线程退出完"] = (max(c[4] + c[5] / 1000 for c in mine) - t_sig) * 1000
    dx = [c for c in calls if c[3] == "do_exit" and c[4] >= t_sig]
    if dx:
        out["SIGTERM → 第一个线程进入 do_exit"] = (min(c[4] for c in dx) - t_sig) * 1000
        slow = max(dx, key=lambda c: c[5])
        out["线程数"] = len(dx)
        out["其他线程 do_exit 最长"] = max([c[5] for c in dx if c is not slow] or [0.0])
        tree, first = {}, {}
        for c in calls:   # 最慢线程 do_exit 里面的调用树，路径相同的累加；按开始时刻排，父在子前
            if c[0] == slow[0] and slow[4] <= c[4] <= slow[4] + slow[5] / 1000 and "do_exit" in c[6].split("/"):
                p = c[6][c[6].index("do_exit"):]
                tree[p] = tree.get(p, 0.0) + c[5]
                first[p] = min(first.get(p, c[4]), c[4])
        out["tree"] = {p: tree[p] for p in sorted(tree, key=lambda p: (first[p], p.count("/")))}
        out["最慢线程"] = slow[1]
    return out


def run_point(Sandbox, ctx, ft, point, reps, rows):
    point = w.norm(point)
    sb = Sandbox.create(template=ctx.args.template, timeout=w.SBX_TIMEOUT)
    sid = sb.sandbox_id
    builds = []
    try:
        hog, _ = w.prepare_sandbox(sb, ctx, point, threading.Lock())
        for rep in range(reps + 1):
            hog.dirty(point["d"], point["layout"])
            pid = fc_pid(sid)
            if not pid:
                raise RuntimeError("找不到沙箱 %s 的 Firecracker 进程" % sid)
            before = w.list_cache(ctx.cache)
            mark = ctx.pause_log.mark()
            ft.arm(tids_of(pid))
            t0 = time.perf_counter()
            try:
                sb.pause()
            finally:
                wall = (time.perf_counter() - t0) * 1000
                time.sleep(0.2)
                text = ft.disarm()
            timing = ctx.pause_log.take(sid, mark)
            _, _, build = w.export_sizes(ctx.cache, before)
            builds.append(build)
            if ctx.trace_dir:     # 原始 trace 留在结果目录里，便于复查
                with open(os.path.join(ctx.trace_dir, "trace-P%d-rep%d.txt" % (point["P"], rep)), "w") as f:
                    f.write(text)
            calls, events = parse(text)
            res = analyze(calls, events, pid)
            ph = (timing or {}).get("phases") or {}
            row = {"P": point["P"], "d": point["d"], "rep": rep, "kind": "first" if rep == 0 else "sample",
                   "fc_pid": pid, "pause_wall_ms": wall, "trace_lines": text.count("\n"), "split": res,
                   "orch_stop_ms": (ph["fc_exited"] - ph["stop_begin"]) if "fc_exited" in ph and "stop_begin" in ph
                   else None, "pause_phases": ph}
            rows.append(row)
            w.LOG.info("P=%d rep %d：pause %.0f ms，orchestrator 停 FC→退出 %s ms，trace %d 行", point["P"], rep, wall,
                       "%.1f" % row["orch_stop_ms"] if row["orch_stop_ms"] is not None else "-", row["trace_lines"])
            sb = Sandbox.connect(sid, timeout=w.SBX_TIMEOUT)
            sb.commands.run("true", user="root", timeout=60)
            hog.sb = sb
    finally:
        try:
            sb.kill()
        except Exception as exc:
            w.LOG.warning("kill %s：%s", sid, exc)
        if ctx.storage:
            w.remove_builds(ctx.storage, builds)


def p50(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def f1(v):
    return "-" if v is None else "%.1f" % v


def summarize(rows):
    out = []
    for P in dict.fromkeys(r["P"] for r in rows):
        rs = [r for r in rows if r["P"] == P and r["kind"] == "sample" and r["split"]]
        if not rs:
            continue
        e = {"P": P, "n": len(rs), "orch_stop_ms": p50([r["orch_stop_ms"] for r in rs]),
             "pause_wall_ms": p50([r["pause_wall_ms"] for r in rs])}
        for k in ("ps 查进程状态", "ps 结束 → 发 SIGTERM", "SIGTERM → 第一个线程进入 do_exit", "SIGTERM → 主线程退出完",
                  "其他线程 do_exit 最长", "线程数"):
            e[k] = p50([r["split"].get(k) for r in rs])
        paths = []
        for r in rs:
            for p in r["split"].get("tree", {}):
                if p not in paths:
                    paths.append(p)
        e["tree"] = {p: p50([r["split"]["tree"].get(p, 0.0) for r in rs]) for p in paths}
        e["slowest"] = statistics.mode([r["split"].get("最慢线程") for r in rs])
        ph = [r["pause_phases"] for r in rs if r["pause_phases"]]
        if ph:
            # orchestrator 的 stop 段减去内核里看得到的 ps 与 SIGTERM→退出完，剩下的是其余等待
            # f66dbe2c1 起 orchestrator 改读 /proc、不再起 ps，没有 ps 就按 0 计
            k = [e["orch_stop_ms"], e["ps 查进程状态"] or 0.0, e["SIGTERM → 主线程退出完"]]
            e["orch_minus_kernel_ms"] = k[0] - k[1] - k[2] if None not in k else None
        out.append(e)
    return out


def print_summary(summary):
    print("FC 退出拆分（p50，ms；内核 ftrace，只跟踪这个 Firecracker 的线程；跟踪有开销，看比例和归属）")
    for e in summary:
        print("\nP=%d d=%d（%d 次）· pause 墙钟 %s" % (e["P"], e["P"], e["n"], f1(e["pause_wall_ms"])))
        print("  orchestrator：开始停沙箱 → FC 退出                %7s" % f1(e["orch_stop_ms"]))
        print("    发 SIGTERM 前用 ps 查进程状态（exec → exit）   %7s" % f1(e["ps 查进程状态"]))
        print("    SIGTERM → 主线程退出完（exit_notify）         %7s" % f1(e["SIGTERM → 主线程退出完"]))
        print("    其余（差值：起 ps 之前、Go 收到退出之后）      %7s" % f1(e.get("orch_minus_kernel_ms")))
        print("  内核：SIGTERM → 第一个线程进入 do_exit           %7s" % f1(e["SIGTERM → 第一个线程进入 do_exit"]))
        print("  退出最慢的线程（%s）的 do_exit，含子函数：" % e["slowest"])
        for p, ms in e["tree"].items():
            if ms is None or ms < MIN_SHOW_MS:
                continue
            depth = p.count("/")
            print("    %s%-*s %7s" % ("  " * depth, 44 - 2 * depth, p.rsplit("/", 1)[-1], f1(ms)))
        print("  其他 %s 个线程的 do_exit 最长 %s" % (
            "%d" % (e["线程数"] - 1) if e["线程数"] else "-", f1(e["其他线程 do_exit 最长"])))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--points", default="0,192", help="进程大小 MiB（= 每次改写量），逗号分隔")
    ap.add_argument("--reps", type=int, default=5, help="每个测点几次 pause（另加第 0 次，不进统计）")
    ap.add_argument("--template", default="base-criu")
    ap.add_argument("--out", default="fc-exit-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args()
    args.criu_verbosity, args.criu_logs = None, False
    if os.geteuid() != 0:
        raise SystemExit("要用 root（写 tracefs）")
    exe, env = w.orchestrator_proc()
    stack = w.detect_stack(exe)
    if stack != "criu":
        raise SystemExit("当前不是进程级栈（原生路径与上游相同的是进程级那套），先 950.sh criu")
    cache = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build") if env else None
    storage = (env.get("LOCAL_TEMPLATE_STORAGE_BASE_PATH") or "/tmp/templates") \
        if env and env.get("STORAGE_PROVIDER", "Local") == "Local" else None
    ctx = w.Ctx(args, stack, None, cache, storage)
    result = {"schema_version": 1, "stack": stack, "rows": [], "summary": [], "verdict": "FAIL",
              "provenance": {"hostname": socket.gethostname(), "kernel": os.uname().release,
                             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             "orchestrator_exe": exe, "orchestrator_sha256": w.sha256_file(exe) if exe else None,
                             "args": vars(args), "host_conditions": w.host_conditions()}}
    ctx.prov = result["provenance"]
    ctx.trace_dir = os.path.join(os.path.dirname(os.path.abspath(args.out)), "traces")
    os.makedirs(ctx.trace_dir, exist_ok=True)
    ctx.pause_log = w.PauseLog()
    if not ctx.pause_log.dir:
        raise SystemExit("找不到 orchestrator 的 Nomad 日志目录")
    from e2b import Sandbox
    err = None
    try:
        with Ftrace() as ft:
            result["provenance"]["traced_functions"] = ft.funcs
            for P in [int(x) for x in args.points.split(",") if x]:
                run_point(Sandbox, ctx, ft, {"P": P, "d": P}, args.reps, result["rows"])
        result["summary"] = summarize(result["rows"])
        result["verdict"] = "PASS" if result["summary"] else "FAIL"
    except Exception as exc:
        err = exc
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    result["provenance"]["host_conditions_end"] = w.host_conditions()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("\n---- 汇总 ----")
    w.print_host(result["provenance"])
    if result["summary"]:
        print()
        print_summary(result["summary"])
    print("\n判定: %s%s" % (result["verdict"], "（%s）" % result["error"] if err else ""))
    print("结果文件: %s" % os.path.abspath(args.out))
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
