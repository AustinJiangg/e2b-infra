# -*- coding: utf-8 -*-
"""
T36 混合稳态（方案 §4.3）。

N 个沙箱各自一条线程，**独立**按权重随机做 create / restore / delete / list /
命令执行 / 写文件校验，跑满 `--seconds`。和 `checkpoint_concurrent.py` 的 D 段是
同一个意思，两处不同：

  1. **每个场景都在 try 里**，worker 线程永远不会因为一次异常死掉 —— D 段那版
     一旦某个场景抛出去，那条线程就此退出，剩下的时间它什么都没做，而报表上
     看不出来（"失败 1 次"和"从第 30 秒起就没人干活了"长得一样）。这里每条线程
     还额外报 `alive_s`（它实际跑到了第几秒）与 `iterations`，对不上就是有线程
     提前死了；
  2. 结束时**对账 + 收干净**：客户端账本 vs 服务端 list、每沙箱把剩下的
     checkpoint 删光后 list 必须为空、netns 计数不比开跑前多、所有沙箱的
     firecracker 进程归零。

一个沙箱只有一条线程 → 同沙箱不并发（那是 T12/T14 的题目），这里压的是
**跨沙箱的混合稳态**：服务端在长时间乱序负载下会不会漏账、漏回收、越跑越慢。

统计（进 JSON 的 summary，也在收尾打表）：每类操作的次数 / 失败数 /
p50 / p99 / max、失败按 `"<异常类>/<reason>"` 分桶、restore 后现场不一致数、
沙箱失联数、各线程的存活时长。

规模：

    python -m crtest T36                        # 4 沙箱 × 120 s（默认，≤ 5 分钟）
    python -m crtest T36 --sandboxes 16 --seconds 1800     # 需求书的全规模 30 min

判据里**硬判**的只有收尾那几条（对账、删干净、不泄漏）与"不许有线程提前死"；
过程中的失败只统计不判 —— 稳态跑的意义是把错误分布摆出来，判定口径由用户定
（同 §4.3 的"报错误分布"）。`--max-fail-ratio`（默认 0，即关掉）可以给一条硬线。
"""

NAME = "T36"

import random
import threading
import time

from .. import common
from ..common import expect, log, note

SCENES = ["create", "restore", "delete", "list", "exec", "write"]
DEFAULT_WEIGHTS = "create=3,restore=3,delete=1,list=2,exec=3,write=2"

BASIS = ("方案 §4.3 T36；参考 e2b-infra/rollback/scripts/acceptance/checkpoint_concurrent.py "
         "的 D 段（本用例把所有 scene 挪进 try，并补了收尾对账）")

_OPS_LOCK = threading.Lock()


def add_args(ap):
    ap.add_argument("--sandboxes", type=int, default=4, help="沙箱数（默认 4；全规模 16）")
    ap.add_argument("--seconds", type=float, default=120.0,
                    help="跑多久，秒（默认 120；全规模 1800）")
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS,
                    help="场景权重，`名字=数字` 逗号分隔（默认 %s）" % DEFAULT_WEIGHTS)
    ap.add_argument("--max-checkpoints", type=int, default=24,
                    help="每沙箱同时留几个 checkpoint，到顶了就先删最老的（默认 24）")
    ap.add_argument("--mem-mb", type=int, default=8, help="每次写脏的内存 MB（默认 8）")
    ap.add_argument("--file-mb", type=int, default=2, help="每次写脏的文件 MB（默认 2）")
    ap.add_argument("--report-every", type=float, default=30.0,
                    help="每隔多少秒打一行进度（默认 30；0 = 不打）")
    ap.add_argument("--max-fail-ratio", type=float, default=0.0,
                    help="硬线：失败率超过它就判失败（默认 0 = 不判，只统计）")
    ap.add_argument("--cleanup-timeout", type=float, default=120.0,
                    help="收尾等 firecracker 进程归零的上限秒数（默认 120）")
    ap.add_argument("--seed", type=int, default=20260918, help="随机种子")
    return ap


# ---------------------------------------------------------------- 场景
#
# 每个都返回一条操作记录（`Box.*` 自己就是这个形状）。**它们不许抛**——
# 抛了也由 `_step()` 兜住，但那样就少了 scene_name 之外的上下文。

def _sc_create(ctx, box, rnd, st):
    a = ctx.args
    if len(box.created) >= a.max_checkpoints:
        # 到顶了：这一拍改成删最老的，免得链无限长（这不是失败，如实记成 delete）
        rec = _sc_delete(ctx, box, rnd, st, oldest=True)
        rec["scene_name"] = "delete"
        rec["why"] = "create 时已到 --max-checkpoints，先腾一个"
        return rec
    st["gen"] += 1
    gen = "%s-g%d" % (box.label, st["gen"])
    box.dirty(gen, mem_mb=a.mem_mb, file_mb=a.file_mb)
    return box.create(gen)


def _sc_restore(ctx, box, rnd, st):
    if not box.created:
        return {"op": "restore", "box": box.label, "sandbox": box.id, "ok": True,
                "noop": "还没有 checkpoint 可回"}
    return box.restore(rnd.choice(list(box.created)), verify=True)


def _sc_delete(ctx, box, rnd, st, oldest=False):
    if not box.created:
        return {"op": "delete", "box": box.label, "sandbox": box.id, "ok": True,
                "noop": "没有 checkpoint 可删"}
    ck = box.created[0] if oldest else rnd.choice(list(box.created))
    rec = box.delete(ck)
    if rec.get("ok"):
        try:
            box.created.remove(ck)
        except ValueError:
            pass
    return rec


def _sc_list(ctx, box, rnd, st):
    rec = box.list_rec()
    if rec.get("ok"):
        want, got = set(box.created), set(rec.get("ids") or [])
        if want != got:
            rec["ledger_diff"] = {"missing": sorted(want - got), "ghost": sorted(got - want)}
            st["list_diff"] += 1
    return rec


def _sc_exec(ctx, box, rnd, st):
    rec = {"op": "exec", "box": box.label, "sandbox": box.id, "t": time.time()}
    t0 = time.monotonic()
    try:
        ok, d = box.alive(timeout=120)
        rec["wall_s"] = time.monotonic() - t0
        rec["ok"] = bool(ok)
        rec["detail"] = d
        if not ok:
            rec["err"] = "活体判据没过：%s" % d
            st["exec_bad"] += 1
    except Exception as e:      # noqa: BLE001
        rec["wall_s"] = time.monotonic() - t0
        rec["ok"] = False
        common.record_error(rec, e)
        st["exec_bad"] += 1
    return rec


def _sc_write(ctx, box, rnd, st):
    """写一轮标记再读回来核对 —— 不经过 checkpoint 的那条基本正确性。"""
    a = ctx.args
    st["gen"] += 1
    gen = "%s-w%d" % (box.label, st["gen"])
    rec = {"op": "write", "box": box.label, "sandbox": box.id, "gen": gen, "t": time.time()}
    t0 = time.monotonic()
    try:
        box.dirty(gen, mem_mb=a.mem_mb, file_mb=a.file_mb)
        sc = box.scene()
        rec["wall_s"] = time.monotonic() - t0
        bad = {k: sc.get(k) for k in ("mem_gen", "file_gen") if sc.get(k) != gen}
        rec["ok"] = not bad
        rec["verified"] = not bad
        if bad:
            rec["mismatch"] = bad
            rec["err"] = "写完读回来的代号不是刚写的：%s" % bad
    except Exception as e:      # noqa: BLE001
        rec["wall_s"] = time.monotonic() - t0
        rec["ok"] = False
        common.record_error(rec, e)
    return rec


SCENE_FN = {"create": _sc_create, "restore": _sc_restore, "delete": _sc_delete,
            "list": _sc_list, "exec": _sc_exec, "write": _sc_write}


# ---------------------------------------------------------------- worker

def _step(ctx, box, rnd, st, weights):
    """跑一拍。**任何异常都吞掉并记成一条失败记录** —— worker 不许因此退出。"""
    name = common.pick_weighted(weights, rnd.random())
    try:
        rec = SCENE_FN[name](ctx, box, rnd, st)
    except Exception as e:      # noqa: BLE001 —— 记下来就是目的
        rec = {"op": name, "box": box.label, "sandbox": box.id, "ok": False,
               "t": time.time(), "raised": True}
        common.record_error(rec, e)
    rec.setdefault("scene_name", name)
    with _OPS_LOCK:
        ctx.op(rec, stage="T36")
    return rec


def _worker(ctx, box, deadline, weights, seed, st, stop):
    """一条线程一个沙箱。整个循环体也在 try 里：连 `_step` 自己都炸了也接着跑。"""
    rnd = random.Random(seed)
    t0 = time.monotonic()
    while time.monotonic() < deadline and not stop.is_set():
        try:
            _step(ctx, box, rnd, st, weights)
        except BaseException as e:      # noqa: BLE001 —— 含 MemoryError 之类；线程绝不能死
            st["loop_errors"].append("%s: %s" % (type(e).__name__, e))
            time.sleep(0.5)
        st["iterations"] += 1
    st["alive_s"] = time.monotonic() - t0
    st["finished"] = True


# ---------------------------------------------------------------- 主流程

def run(ctx):
    a = ctx.args
    try:
        weights = common.parse_weights(a.weights, SCENES)
    except ValueError as e:
        raise common.Unmet("--weights 解析不了：%s" % e,
                           "写法：--weights '%s'" % DEFAULT_WEIGHTS)
    log("  场景权重：%s" % ", ".join("%s=%g" % kv for kv in sorted(weights.items()) if kv[1]))

    netns_before = common.netns_count_steady()
    # 暖池状态必须取在 **before 这一刻**：跑完时 orchestrator 早已跑够 35 分钟，
    # 但 before 是在暖池填充中采的，后面的 after 必然更大 —— 那是暖池在长不是泄漏。
    netns_warm, netns_warm_why = common.netns_pool_warm()
    boxes = common.spawn(ctx, a.sandboxes, "t36-")
    for b in boxes:
        b.setup(warm_mem=32, warm_file=8)
        b.dirty("%s-g0" % b.label, mem_mb=a.mem_mb, file_mb=a.file_mb)
    log("  沙箱：%s" % ", ".join(b.id for b in boxes))

    states = {}
    stop = threading.Event()
    deadline = time.monotonic() + a.seconds
    threads = []
    for i, b in enumerate(boxes):
        st = {"gen": 0, "iterations": 0, "list_diff": 0, "exec_bad": 0,
              "loop_errors": [], "alive_s": None, "finished": False}
        states[b.label] = st
        t = threading.Thread(target=_worker,
                             args=(ctx, b, deadline, weights, a.seed + i, st, stop),
                             name="t36-%s" % b.label, daemon=True)
        threads.append(t)
    ctx.stage("steady-start", sandboxes=len(boxes), seconds=a.seconds, netns=netns_before)
    log("\n  开跑：%d 沙箱 × %g s" % (len(boxes), a.seconds))
    for t in threads:
        t.start()

    try:
        last_report = time.monotonic()
        while any(t.is_alive() for t in threads):
            time.sleep(1.0)
            now = time.monotonic()
            if a.report_every and now - last_report >= a.report_every:
                last_report = now
                with _OPS_LOCK:
                    ops_now = list(ctx.results["ops"])
                bad = sum(1 for o in ops_now if o.get("ok") is False)
                log("    还剩 %.0f s：%d 次操作，%d 次失败，线程活着 %d/%d"
                    % (max(0.0, deadline - now), len(ops_now), bad,
                       sum(1 for t in threads if t.is_alive()), len(threads)))
    except KeyboardInterrupt:
        stop.set()
        raise
    for t in threads:
        t.join(timeout=300)

    # ---- 线程都跑满了吗
    for b in boxes:
        st = states[b.label]
        expect(ctx, "%s 的线程跑满全程" % b.label, st["finished"] and not st["loop_errors"],
               "跑到收尾、循环体没抛过",
               "finished=%s，循环体异常 %d 次：%s；实际跑了 %.0f s / %g s，%d 拍"
               % (st["finished"], len(st["loop_errors"]), st["loop_errors"][:3] or "无",
                  st["alive_s"] or -1, a.seconds, st["iterations"]),
               "worker 线程绝不能因为一次异常退出（D 段那版会）——"
               "退了的话报表上看不出来，只会显得后半程「没有失败」")

    # ---- 统计
    with _OPS_LOCK:
        ops = [o for o in ctx.results["ops"] if o.get("stage") == "T36"]
    buckets = common.bucket_ops(ops)
    total = sum(v["n"] for v in buckets.values())
    fails = sum(v["fail"] for v in buckets.values())
    mismatch = sum(v["mismatch"] for v in buckets.values())
    log("")
    common.table(["场景", "次数", "失败", "p50 s", "p99 s", "max s", "现场不一致", "失败分桶"],
                 [[k, str(v["n"]), str(v["fail"]), common.fmt_s(v["p50_s"]),
                   common.fmt_s(v["p99_s"]), common.fmt_s(v["max_s"]), str(v["mismatch"]),
                   ", ".join("%s×%d" % (kk, vv) for kk, vv in sorted(v["errors"].items())) or "-"]
                  for k, v in sorted(buckets.items())])

    expect(ctx, "restore 后现场逐项一致", mismatch == 0, "0 次不一致",
           "%d 次" % mismatch,
           "现场口径同 checkpoint_verify.py；这是稳态跑里唯一的正确性硬判")
    note(ctx, "操作总数 / 失败数", "只记录（错误分布由用户定口径）",
         "%d / %d（%.2f%%）" % (total, fails, 100.0 * fails / total if total else 0.0), BASIS)
    if a.max_fail_ratio:
        ratio = (fails / total) if total else 0.0
        expect(ctx, "失败率在硬线内", ratio <= a.max_fail_ratio,
               "≤ %.2f%%" % (100 * a.max_fail_ratio), "%.2f%%" % (100 * ratio),
               "--max-fail-ratio 给的硬线")
    list_diff = sum(states[b.label]["list_diff"] for b in boxes)
    note(ctx, "跑动中 list 与账本对不上的次数", "0", str(list_diff),
         "同沙箱只有一条线程，跑动中的 list 也应当和账本一致；这里只记录，收尾那次是硬判")
    lost = [b.id for b in boxes if states[b.label]["exec_bad"]]
    note(ctx, "命令执行没过活体判据的沙箱", "0 个", "%d 个：%s" % (len(lost), lost or "无"),
         "09-15 并发跑里 restore 后沙箱报废 3/16748，是未结的 P0")

    # ---- 收尾：对账 + 删干净
    log("")
    log("  收尾对账")
    for b in boxes:
        rec = ctx.op(b.list_rec(), stage="T36", step="final-list")
        ok, want, got = common.judge_reconcile(b.created, rec.get("ids") or [])
        expect(ctx, "%s 收尾 list 与账本一致" % b.label, ok and rec.get("ok"), want,
               rec.get("err") or got, "方案 §4.3 T36：跑完不许有幽灵，也不许漏账")
        for ck in list(b.created):
            d = ctx.op(b.delete(ck), stage="T36", step="final-delete")
            expect(ctx, "%s 删掉 %s" % (b.label, ck), d.get("ok"), "成功",
                   d.get("err") or "ok", "方案 §4.3 T36：每沙箱 delete 干净")
            try:
                b.created.remove(ck)
            except ValueError:
                pass
        rec = ctx.op(b.list_rec(), stage="T36", step="final-list-empty")
        expect(ctx, "%s 删干净之后 list 为空" % b.label,
               rec.get("ok") and not (rec.get("ids") or []), "0 条",
               rec.get("err") or "%d 条：%s" % (len(rec.get("ids") or []), rec.get("ids")),
               "方案 §4.3 T36")
        alive, d = b.alive()
        expect(ctx, "%s 跑完还活着" % b.label, alive,
               "命令能跑、能写盘、心跳在推进", d, "活体判据同 checkpoint_verify.py")

    # ---- 收尾：资源归零
    ids = [b.id for b in boxes]
    for b in boxes:
        b.kill()
        common.forget(b)
    left, waited, _ = common.wait_until(
        lambda: not any(common.fc_processes(i) for i in ids), a.cleanup_timeout, interval=2.0)
    leftovers = {i: common.fc_processes(i) for i in ids}
    leftovers = {i: v for i, v in leftovers.items() if v}
    expect(ctx, "跑完活 FC 归零", not leftovers, "本用例的沙箱一个 firecracker 都不剩",
           "还剩 %s（等了 %.0f s）" % (leftovers or "0 个", waited),
           "方案 §4.3 T36：宿主机侧不许泄漏；只看本用例这几个沙箱（机器上还有别人的）")
    netns_after = common.netns_count_steady()
    ok, want, got = common.judge_netns_steady(netns_before, netns_after, slack=len(boxes))
    warm, warm_why = netns_warm, netns_warm_why
    if ok is None:
        note(ctx, "netns 槽位不泄漏", want, got, "09-16 清过 8729 个泄漏槽位")
    elif warm is not True:
        # 暖池没填满（或读不到启动时间）时 before 取在填充中，after 必然更大 —— 那是
        # 暖池在长，不是泄漏。降级成记录项，不判失败。
        log("  注意：netns 判定降级为记录项 —— %s" % warm_why)
        note(ctx, "netns 槽位不泄漏（降级为记录项）", want,
             "%s；不判失败，原因：%s" % (got, warm_why),
             "09-16 清过 8729 个泄漏槽位；orchestrator 启动不足 %.0f 分钟时这条判定不成立"
             % (common.NETNS_POOL_WARMUP_S / 60.0))
    else:
        expect(ctx, "netns 槽位不泄漏", ok, want, got,
               "09-16 清过 8729 个泄漏槽位；容差 = 沙箱数（别人的沙箱也在起落）")

    ctx.results["summary"]["T36"] = {
        "sandboxes": a.sandboxes, "seconds": a.seconds, "weights": weights,
        "total_ops": total, "fails": fails, "mismatch": mismatch,
        "list_diff_inflight": list_diff, "exec_bad_boxes": lost,
        "by_scene": buckets,
        "threads": {k: {"iterations": v["iterations"], "alive_s": v["alive_s"],
                        "finished": v["finished"], "loop_errors": v["loop_errors"],
                        "exec_bad": v["exec_bad"], "list_diff": v["list_diff"]}
                    for k, v in states.items()},
        "netns_before": netns_before, "netns_after": netns_after,
        "netns_pool_warm": warm, "netns_pool_warm_why": warm_why,
        "orchestrator_uptime_s": common.orchestrator_uptime_s(),
        "fc_left": leftovers, "cleanup_wait_s": waited, "sandboxes_ids": ids,
    }
