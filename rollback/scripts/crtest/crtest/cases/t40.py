# -*- coding: utf-8 -*-
"""
T40 「restore 紧跟请求」的定向取证（报告 §2.18 / §7.1）。

要抓的现象：envd 流式 `commands.run` 偶发 `RemoteProtocolError: incomplete chunked
read` / `unexpected EOF` / `ReadError [Errno 9] Bad file descriptor`。已知的四例
**都是紧跟一次成功 restore 之后 13–20 ms 建立的新连接**，服务端配着一条
`ReverseProxy read error … use of closed network connection`（orchestrator 先关了
到 envd 的上游连接）。

T36 那轮 30 分钟 74938 次操作 0 例，原因是它的 `exec` 是短 unary（几十毫秒就完），
**既不长流也不跨到下一次 restore**，正好避开触发条件。本用例就是把那个条件造出来：

  每个沙箱一条线程，循环：
    restore（回同一个 checkpoint） → **一步不歇**（不 sleep）发出
      · `--fanout` 条 **长流** `commands.run`（默认 2 s：`for i in $(seq 20);
        do echo $i; sleep 0.1; done`，`on_stdout` 数行）
      · 同时一条 unary 命令（`echo`）
    → 等它们回来 → 下一轮 restore

两个旋钮分别对准两条可能的机制：

  · `--gap-jitter-ms`：发请求之前随机等 0~N ms。默认 0（贴着 restore 返回就发，
    间隔 p50 不到 1 ms）；给 40 就能把间隔铺到 0–40 ms，**覆盖已知四例的 13–20 ms**。
  · `--overlap`：长流发出去之后不等它跑完，先睡 `--overlap-delay`（默认 0.5 s，
    落在流的中段）再**对同一个沙箱再 restore 一次**，然后才去等流。restore 的服务端
    第一件事就是 `beginRestore` → `dropConnections`（orchestrator
    `internal/checkpoint/service.go:1073` → `:865`），所以这是「池剔除抽走一条
    **在途**连接」的正面现场 —— T36 那轮抓不到，正是因为它的请求从不活到下一次
    restore。开了之后每轮两次 restore，第二次记成 `restore_mid`。

记录（进 JSON 的 `summary.T40`，收尾打表）：

  · 每次 restore 的墙钟、服务端 `timings` 的 `conntrack` / `total` 段；
  · 每条请求**发出时刻相对 restore 返回**的间隔 `gap_ms`（证明确实覆盖 0–50 ms 窗口），
    按 0–1 / 1–2 / 2–5 / 5–10 / 10–20 / 20–50 / 50–100 / >100 ms 分桶；
  · 每条失败：沙箱 id、毫秒精度的墙钟时刻（对 orchestrator 日志用）、异常类与
    message、`gap_ms`、**前一次 restore 的 conntrack / total 段**；
  · 截断类失败单独分一桶（`TRUNCATION_RE`：incomplete chunked / unexpected EOF /
    EBADF / Bad file descriptor / RemoteProtocolError / peer closed / connection reset），
    与别的失败（超时、409 sandbox_restored 等）分开数。

**判定**：硬判只有收尾那两条 ——「每个沙箱还活着」「客户端账本 = 服务端 list」——
外加「没有 worker 线程提前死」。**截断次数是记录项**（`note`），口径同 T36：
这一轮要的是把分布摆出来，不是给一条线。`--max-truncation` 可以自己加一条硬线。

规模：

    python -m crtest T40                                  # 4 沙箱 × 50 轮（默认，≤ 5 分钟）
    python -m crtest T40 --sandboxes 8 --rounds 300       # 定向取证的全规模
    python -m crtest T40 --sandboxes 8 --seconds 1200     # 按时间跑（与 --rounds 二选一）
    python -m crtest T40 --sandboxes 8 --rounds 300 --stream-seconds 5 --fanout 16   # 变体

`--rounds` 与 `--seconds` 二选一：给了 `--seconds` 就按时间跑（`--rounds` 当上限，
默认不限），都不给就按 `--rounds`。
"""

NAME = "T40"

import random
import re
import threading
import time

from .. import common
from ..common import expect, log, note

# 截断类错误的判据。四例现场的原文：
#   httpx.RemoteProtocolError: peer closed connection without sending complete
#     message body (incomplete chunked read)
#   httpcore.RemoteProtocolError: unexpected EOF
#   httpx.ReadError: [Errno 9] Bad file descriptor
TRUNCATION_RE = re.compile(
    r"incomplete chunked|unexpected EOF|Errno 9|Bad file descriptor|EBADF|"
    r"RemoteProtocolError|peer closed|connection reset|ConnectionResetError|"
    r"Server disconnected|broken pipe",
    re.I)

# restore 把连接掐了以后服务端给的那句（`checkpoint.RestoredAnswer`，409）。
# 它是**设计内**的答复，不是截断，单独一类。
RESTORED_RE = re.compile(r"sandbox_restored|rolled back to a checkpoint", re.I)

GAP_EDGES = [1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]

BASIS = ("报告 §2.18；orchestrator internal/checkpoint/service.go:1073 beginRestore → "
         ":865 dropConnections（restore 的第一件事就是丢掉这个沙箱的池条目），"
         "internal/sandbox/checkpoint.go:657 conntrack 段（在 resume 之前、200 之前）")

_OPS_LOCK = threading.Lock()


def add_args(ap):
    ap.add_argument("--sandboxes", type=int, default=4, help="沙箱数（默认 4；定向取证 8）")
    ap.add_argument("--rounds", type=int, default=50,
                    help="每沙箱多少轮 restore（默认 50；定向取证 300）")
    ap.add_argument("--seconds", type=float, default=None,
                    help="按时间跑，秒（给了就与 --rounds 二选一，--rounds 退化成上限）")
    ap.add_argument("--stream-seconds", type=float, default=2.0,
                    help="长流跑多久，秒（默认 2；变体 5）。命令是 seq N + sleep 0.1")
    ap.add_argument("--fanout", type=int, default=1,
                    help="每轮同时发几条长流（默认 1；变体 16）")
    ap.add_argument("--seed", type=int, default=20260918, help="随机种子（--gap-jitter-ms 用）")
    ap.add_argument("--gap-jitter-ms", type=float, default=0.0,
                    help="发请求前随机等 0~N 毫秒（默认 0 = 贴着 restore 返回就发；"
                         "给 40 可覆盖已知四例的 13–20 ms 间隔）")
    ap.add_argument("--overlap", action="store_true",
                    help="长流在途时再 restore 一次（正面压「池剔除抽走在途连接」）")
    ap.add_argument("--overlap-delay", type=float, default=0.5,
                    help="--overlap 时，发完流等多久再 restore，秒（默认 0.5，落在流中段）")
    ap.add_argument("--no-unary", action="store_true",
                    help="不发那条并行的 unary 命令（默认发）")
    ap.add_argument("--req-timeout", type=float, default=120.0,
                    help="单条 guest 命令的超时秒数（默认 120）")
    ap.add_argument("--max-truncation", type=int, default=-1,
                    help="硬线：截断次数超过它就判失败（默认 -1 = 不判，只记录）")
    ap.add_argument("--report-every", type=float, default=30.0,
                    help="每隔多少秒打一行进度（默认 30；0 = 不打）")
    ap.add_argument("--cleanup-timeout", type=float, default=120.0,
                    help="收尾等 firecracker 进程归零的上限秒数（默认 120）")
    return ap


# ---------------------------------------------------------------- 分类与分桶

def classify(rec):
    """一条失败记录归到哪一类：truncation / restored / other。成功的返回 None。"""
    if rec.get("ok"):
        return None
    text = rec.get("err") or ""
    if RESTORED_RE.search(text):
        return "restored"
    if TRUNCATION_RE.search(text):
        return "truncation"
    return "other"


def gap_bucket(ms):
    """把 gap_ms 归到一个区间标签上。"""
    if ms is None:
        return "?"
    lo = 0.0
    for hi in GAP_EDGES:
        if ms < hi:
            return "%g–%g ms" % (lo, hi)
        lo = hi
    return "≥%g ms" % GAP_EDGES[-1]


def gap_histogram(gaps):
    """返回 `[(标签, 次数), ...]`，按区间顺序（不是按次数）。"""
    labels = []
    lo = 0.0
    for hi in GAP_EDGES:
        labels.append("%g–%g ms" % (lo, hi))
        lo = hi
    labels.append("≥%g ms" % GAP_EDGES[-1])
    labels.append("?")
    counts = {k: 0 for k in labels}
    for g in gaps:
        counts[gap_bucket(g)] += 1
    return [(k, counts[k]) for k in labels if counts[k] or k != "?"]


def stream_cmd(seconds):
    """长流命令：每 0.1 s 一行，总共 seconds 秒。返回 `(命令, 该有几行)`。"""
    n = max(1, int(round(seconds / 0.1)))
    return "for i in $(seq %d); do echo $i; sleep 0.1; done" % n, n


# ---------------------------------------------------------------- 一次请求

def _one_request(box, kind, cmd, want_lines, ret_mono, ret_wall, prev, timeout,
                 jitter_ms=0.0, rnd=None):
    """发一条请求，返回一条操作记录。

    `ret_mono` / `ret_wall` 是**上一次 restore 返回的时刻**，`gap_ms` 就是本请求
    真正发出去的时刻减它 —— 这一列是「确实覆盖了 0–50 ms 窗口」的证据。
    `prev` 是上一次 restore 的记录（conntrack / total 段从它取）。
    """
    rec = {"op": kind, "scene_name": kind, "box": box.label, "sandbox": box.id,
           "want_lines": want_lines,
           "prev_restore_conntrack_ms": (prev or {}).get("conntrack_ms"),
           "prev_restore_total_ms": (prev or {}).get("total_ms"),
           "prev_restore_wall_s": (prev or {}).get("wall_s"),
           "round": (prev or {}).get("round")}
    lines = [0]

    def on_stdout(_line):
        lines[0] += 1

    if jitter_ms:
        time.sleep((rnd or random).uniform(0.0, jitter_ms) / 1000.0)
    t_send_mono = time.monotonic()
    rec["t"] = time.time()                        # 毫秒精度的墙钟，用来对日志
    rec["gap_ms"] = (t_send_mono - ret_mono) * 1000.0 if ret_mono is not None else None
    rec["restore_ret_wall"] = ret_wall
    try:
        box.sbx.commands.run(cmd, user="root", timeout=timeout,
                             on_stdout=on_stdout if want_lines else None)
        rec["wall_s"] = time.monotonic() - t_send_mono
        rec["lines"] = lines[0]
        # 流没抛异常但行数少了，也是一种截断（服务端把流截了、客户端没报错）
        short = bool(want_lines) and lines[0] < want_lines
        rec["short"] = short
        rec["ok"] = not short
        if short:
            rec["err"] = "StreamShort: 流只收到 %d 行，该有 %d 行" % (lines[0], want_lines)
            rec["error_info"] = common.error_info(rec["err"])
    except Exception as e:      # noqa: BLE001 —— 记下来就是目的
        rec["wall_s"] = time.monotonic() - t_send_mono
        rec["lines"] = lines[0]
        rec["ok"] = False
        common.record_error(rec, e)
    rec["klass"] = classify(rec)
    return rec


# ---------------------------------------------------------------- worker

def _worker(ctx, box, ck, st, deadline, max_rounds, stop):
    """一条线程一个沙箱：restore → 立刻发请求 → 等回来 → 下一轮。绝不许因异常退出。"""
    a = ctx.args
    cmd, want_lines = stream_cmd(a.stream_seconds)
    t0 = time.monotonic()
    rnd = 0
    while not stop.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            break
        if max_rounds is not None and rnd >= max_rounds:
            break
        rnd += 1
        try:
            # ---- restore（verify=False：验现场要先发一条命令，那会占掉我们要量的窗口）
            r = box.restore(ck, verify=False)
            ret_mono, ret_wall = time.monotonic(), time.time()
            r["scene_name"] = "restore"
            r["round"] = rnd
            ph = r.get("phases") or {}
            r["conntrack_ms"] = ph.get("conntrack")
            r["total_ms"] = ph.get("total")
            with _OPS_LOCK:
                ctx.op(r, stage="T40")
            if not r.get("ok"):
                st["restore_fail"] += 1
                time.sleep(0.2)
                continue
            st["restores"] += 1

            # ---- 一步不歇：fanout 条长流 + 一条 unary，全部并发发出去
            recs = []
            recs_lock = threading.Lock()
            threads = []

            def fire(kind, c, wl):
                def body():
                    rec = _one_request(box, kind, c, wl, ret_mono, ret_wall, r,
                                       a.req_timeout, a.gap_jitter_ms,
                                       random.Random(a.seed + rnd))
                    rec["overlapped"] = bool(a.overlap)
                    with recs_lock:
                        recs.append(rec)
                return body

            for _ in range(max(1, a.fanout)):
                threads.append(threading.Thread(target=fire("stream", cmd, want_lines),
                                                daemon=True))
            if not a.no_unary:
                threads.append(threading.Thread(target=fire("unary", "echo t40-ok", 0),
                                                daemon=True))
            for t in threads:
                t.start()

            # --overlap：不等流跑完，先对同一个沙箱再 restore 一次 —— 它的第一件事
            # 就是把这个沙箱的池条目连同上面的连接一起丢掉，而流正卡在中段。
            if a.overlap:
                time.sleep(a.overlap_delay)
                r2 = box.restore(ck, verify=False)
                r2["scene_name"] = "restore_mid"
                r2["round"] = rnd
                ph2 = r2.get("phases") or {}
                r2["conntrack_ms"] = ph2.get("conntrack")
                r2["total_ms"] = ph2.get("total")
                with _OPS_LOCK:
                    ctx.op(r2, stage="T40")
                if r2.get("ok"):
                    st["restores_mid"] += 1
                else:
                    st["restore_fail"] += 1

            for t in threads:
                t.join(timeout=a.req_timeout + 60)

            with _OPS_LOCK:
                for rec in recs:
                    ctx.op(rec, stage="T40")
            for rec in recs:
                k = rec.get("klass")
                if k:
                    st["by_class"][k] = st["by_class"].get(k, 0) + 1
                    st["fails"].append(rec)
        except BaseException as e:      # noqa: BLE001 —— 线程绝不能死
            st["loop_errors"].append("%s: %s" % (type(e).__name__, e))
            time.sleep(0.5)
        st["rounds"] = rnd
    st["alive_s"] = time.monotonic() - t0
    st["finished"] = True


# ---------------------------------------------------------------- 主流程

def run(ctx):
    a = ctx.args
    cmd, want_lines = stream_cmd(a.stream_seconds)
    by_time = a.seconds is not None
    log("  长流命令：%s（%d 行 / 约 %g s），fanout=%d，unary=%s"
        % (cmd, want_lines, a.stream_seconds, max(1, a.fanout), not a.no_unary))
    log("  跑法：%s" % ("按时间 %g s（轮数上限 %s）" % (a.seconds, a.rounds or "不限")
                        if by_time else "每沙箱 %d 轮" % a.rounds))

    boxes = common.spawn(ctx, a.sandboxes, "t40-")
    cks = {}
    for b in boxes:
        b.setup(warm_mem=16, warm_file=4)
        b.dirty("%s-g0" % b.label, mem_mb=4, file_mb=1)
        rec = ctx.op(b.create("%s-g0" % b.label), stage="T40", step="setup-create")
        expect(ctx, "%s 建出基线 checkpoint" % b.label, rec.get("ok"), "成功",
               rec.get("err") or rec.get("id"), "T40 前置：全程只回这一个 checkpoint")
        cks[b.label] = rec["id"]
    log("  沙箱：%s" % ", ".join(b.id for b in boxes))

    states = {}
    stop = threading.Event()
    deadline = (time.monotonic() + a.seconds) if by_time else None
    max_rounds = a.rounds if (not by_time or a.rounds) else None
    threads = []
    for b in boxes:
        st = {"rounds": 0, "restores": 0, "restores_mid": 0, "restore_fail": 0,
              "by_class": {},
              "fails": [], "loop_errors": [], "alive_s": None, "finished": False}
        states[b.label] = st
        t = threading.Thread(target=_worker,
                             args=(ctx, b, cks[b.label], st, deadline, max_rounds, stop),
                             name="t40-%s" % b.label, daemon=True)
        threads.append(t)
    ctx.stage("t40-start", sandboxes=len(boxes), rounds=a.rounds, seconds=a.seconds,
              fanout=max(1, a.fanout), stream_seconds=a.stream_seconds)
    log("\n  开跑")
    for t in threads:
        t.start()

    try:
        last = time.monotonic()
        while any(t.is_alive() for t in threads):
            time.sleep(1.0)
            now = time.monotonic()
            if a.report_every and now - last >= a.report_every:
                last = now
                done = sum(st["rounds"] for st in states.values())
                trunc = sum(st["by_class"].get("truncation", 0) for st in states.values())
                other = sum(st["by_class"].get("other", 0) for st in states.values())
                restored = sum(st["by_class"].get("restored", 0) for st in states.values())
                log("    已跑 %d 轮 restore；截断 %d、409 %d、其它失败 %d；线程活着 %d/%d"
                    % (done, trunc, restored, other,
                       sum(1 for t in threads if t.is_alive()), len(threads)))
    except KeyboardInterrupt:
        stop.set()
        raise
    for t in threads:
        t.join(timeout=600)

    # ---- 线程都跑满了吗（硬判）
    for b in boxes:
        st = states[b.label]
        expect(ctx, "%s 的线程跑满全程" % b.label, st["finished"] and not st["loop_errors"],
               "跑到收尾、循环体没抛过",
               "finished=%s，循环体异常 %d 次：%s；跑了 %d 轮 / %.0f s"
               % (st["finished"], len(st["loop_errors"]), st["loop_errors"][:3] or "无",
                  st["rounds"], st["alive_s"] or -1),
               "同 T36：worker 退了的话报表上看不出来，只会显得后半程没有失败")

    # ---- 统计
    with _OPS_LOCK:
        ops = [o for o in ctx.results["ops"] if o.get("stage") == "T40"
               and o.get("step") is None]
    buckets = common.bucket_ops(ops)
    reqs = [o for o in ops if o.get("op") in ("stream", "unary")]
    restores = [o for o in ops if o.get("scene_name") == "restore"]
    restores_mid = [o for o in ops if o.get("scene_name") == "restore_mid"]
    gaps = [o.get("gap_ms") for o in reqs]
    by_class = {}
    for o in reqs:
        k = o.get("klass")
        if k:
            by_class[k] = by_class.get(k, 0) + 1
    trunc = [o for o in reqs if o.get("klass") == "truncation"]

    log("")
    common.table(["场景", "次数", "失败", "p50 s", "p99 s", "max s", "失败分桶"],
                 [[k, str(v["n"]), str(v["fail"]), common.fmt_s(v["p50_s"]),
                   common.fmt_s(v["p99_s"]), common.fmt_s(v["max_s"]),
                   ", ".join("%s×%d" % kv for kv in sorted(v["errors"].items())) or "-"]
                  for k, v in sorted(buckets.items())])

    log("")
    log("  请求发出相对 restore 返回的间隔（%d 条请求）" % len(reqs))
    hist = gap_histogram(gaps)
    common.table(["区间", "次数", "占比"],
                 [[k, str(n), "%.2f%%" % (100.0 * n / len(reqs) if reqs else 0.0)]
                  for k, n in hist])
    good = [g for g in gaps if g is not None]
    if good:
        log("    p50=%s p99=%s max=%s（毫秒）"
            % (common.fmt_ms(common.p50(good)), common.fmt_ms(common.quantile(good, 0.99)),
               common.fmt_ms(common.pmax(good))))

    # ---- 截断例逐条摆出来（这是本用例的产物）
    if trunc:
        log("")
        log("  截断例（%d 条）" % len(trunc))
        common.table(["时刻", "沙箱", "类型", "gap ms", "收到行/该有",
                      "前次 restore conntrack ms", "前次 total ms", "message"],
                     [[time.strftime("%H:%M:%S", time.localtime(o["t"]))
                       + ".%03d" % int(o["t"] % 1 * 1000),
                       o["sandbox"], o["op"], common.fmt_ms(o.get("gap_ms")),
                       "%s/%s" % (o.get("lines"), o.get("want_lines")),
                       common.fmt_ms(o.get("prev_restore_conntrack_ms")),
                       common.fmt_ms(o.get("prev_restore_total_ms")),
                       (o.get("err") or "")[:90]]
                      for o in sorted(trunc, key=lambda x: x["t"])])

    ct = [r.get("conntrack_ms") for r in restores if r.get("conntrack_ms") is not None]
    tot = [r.get("total_ms") for r in restores if r.get("total_ms") is not None]
    if ct:
        log("")
        log("  restore 服务端分段：conntrack p50=%s p99=%s max=%s；total p50=%s p99=%s max=%s（毫秒）"
            % (common.fmt_ms(common.p50(ct)), common.fmt_ms(common.quantile(ct, 0.99)),
               common.fmt_ms(common.pmax(ct)), common.fmt_ms(common.p50(tot)),
               common.fmt_ms(common.quantile(tot, 0.99)), common.fmt_ms(common.pmax(tot))))

    note(ctx, "截断类失败次数", "只记录（口径同 T36 的错误分布）",
         "%d / %d 条请求（%.4f%%）"
         % (len(trunc), len(reqs), 100.0 * len(trunc) / len(reqs) if reqs else 0.0), BASIS)
    note(ctx, "409 sandbox_restored 次数", "只记录（这是设计内的答复，不是缺陷）",
         str(by_class.get("restored", 0)),
         "checkpoint.RestoredAnswer；请求撞进 restore 窗口时的正常答复")
    note(ctx, "其它失败次数", "只记录", str(by_class.get("other", 0)), BASIS)
    note(ctx, "restore 次数 / 失败数", "只记录",
         "%d / %d" % (len(restores), sum(1 for r in restores if not r.get("ok"))), BASIS)
    if a.overlap:
        note(ctx, "流在途时插进去的 restore 次数 / 失败数", "只记录",
             "%d / %d" % (len(restores_mid),
                          sum(1 for r in restores_mid if not r.get("ok"))),
             "--overlap：正面压「池剔除抽走在途连接」")
    if a.max_truncation >= 0:
        expect(ctx, "截断次数在硬线内", len(trunc) <= a.max_truncation,
               "≤ %d" % a.max_truncation, "%d" % len(trunc), "--max-truncation 给的硬线")

    # ---- 收尾硬判：沙箱存活 + 账本一致
    log("")
    log("  收尾对账")
    for b in boxes:
        rec = ctx.op(b.list_rec(), stage="T40", step="final-list")
        ok, want, got = common.judge_reconcile(b.created, rec.get("ids") or [])
        expect(ctx, "%s 收尾 list 与账本一致" % b.label, ok and rec.get("ok"), want,
               rec.get("err") or got, "T40 硬判之一：截断不许把账本弄乱")
        alive, d = b.alive()
        expect(ctx, "%s 跑完还活着" % b.label, alive,
               "命令能跑、能写盘、心跳在推进", d,
               "T40 硬判之二：活体判据同 checkpoint_verify.py")

    ids = [b.id for b in boxes]
    for b in boxes:
        b.kill()
        common.forget(b)
    _, waited, _ = common.wait_until(
        lambda: not any(common.fc_processes(i) for i in ids), a.cleanup_timeout, interval=2.0)
    leftovers = {i: common.fc_processes(i) for i in ids}
    leftovers = {i: v for i, v in leftovers.items() if v}
    expect(ctx, "跑完活 FC 归零", not leftovers, "本用例的沙箱一个 firecracker 都不剩",
           "还剩 %s（等了 %.0f s）" % (leftovers or "0 个", waited),
           "只看本用例这几个沙箱（机器上还有别人的）")

    ctx.results["summary"]["T40"] = {
        "sandboxes": a.sandboxes, "rounds": a.rounds, "seconds": a.seconds,
        "fanout": max(1, a.fanout), "stream_seconds": a.stream_seconds,
        "stream_cmd": cmd, "want_lines": want_lines, "unary": not a.no_unary,
        "requests": len(reqs), "restores": len(restores),
        "overlap": bool(a.overlap), "overlap_delay": a.overlap_delay,
        "gap_jitter_ms": a.gap_jitter_ms, "restores_mid": len(restores_mid),
        "by_class": by_class, "by_scene": buckets,
        "gap_ms_hist": dict(hist),
        "gap_ms": {"p50": common.p50(good), "p99": common.quantile(good, 0.99),
                   "max": common.pmax(good), "n": len(good)},
        "conntrack_ms": {"p50": common.p50(ct), "p99": common.quantile(ct, 0.99),
                         "max": common.pmax(ct), "n": len(ct)},
        "restore_total_ms": {"p50": common.p50(tot), "p99": common.quantile(tot, 0.99),
                             "max": common.pmax(tot), "n": len(tot)},
        "truncations": [{k: o.get(k) for k in
                         ("t", "sandbox", "box", "op", "round", "gap_ms", "wall_s",
                          "lines", "want_lines", "err", "error_info",
                          "prev_restore_conntrack_ms", "prev_restore_total_ms",
                          "prev_restore_wall_s", "restore_ret_wall")}
                        for o in sorted(trunc, key=lambda x: x["t"])],
        "threads": {k: {"rounds": v["rounds"], "restores": v["restores"],
                        "restores_mid": v["restores_mid"],
                        "restore_fail": v["restore_fail"], "by_class": v["by_class"],
                        "alive_s": v["alive_s"], "finished": v["finished"],
                        "loop_errors": v["loop_errors"]}
                    for k, v in states.items()},
        "fc_left": leftovers, "sandboxes_ids": ids,
    }
