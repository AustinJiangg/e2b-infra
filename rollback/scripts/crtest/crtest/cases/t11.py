# -*- coding: utf-8 -*-
"""
T11 生命周期竞争（方案 §4.1，并发套件 C 段从来没真跑过）。

一个线程对某个沙箱做 create（或 restore），主线程在它**进行中**（随机 0–50 ms 后）
把这个沙箱 `kill()` 掉或 `beta_pause()` 掉。第 3 轮之前这么干没有确定的期望值，
现在有了：

  · R2（e4c0e9f11）给两处等待封了顶：`CHECKPOINT_LOCK_WAIT_TIMEOUT`（默认 60 s，
    超了回 503 + Retry-After，reason busy）与 `CHECKPOINT_FC_CALL_TIMEOUT`（默认
    120 s）。所以在途操作最迟 60+120 s 必须有个说法，本用例给 200 s 的表；
  · R3（b1df1fb5f）让 kill 和在途操作互斥：`OnRemove` 先拿沙箱锁再删文件，
    删完留墓碑，`Prepare`/`LayersDir` 不再给已经没了的沙箱重建目录（F7）。

判定四条：

  1. **服务端不崩**：旁观沙箱每轮照常 create+restore，最后再做一次
     `checkpoint_verify` 式的冒烟（建→改→回→现场逐项一致）；
  2. **在途操作要么成功要么给出明确错误**，且不许挂过 `--op-timeout`（默认 200 s）——
     客户端读超时也算没给说法（`common.judge_t11_op`）；
  3. **kill 之后宿主机侧收干净**：该沙箱的 checkpoint 目录没了、没有它的
     firecracker 进程、netns 数量不增长（只比前后，920B 上还有别人的沙箱）；
  4. **kill 之后同 id `connect()` 应当用不了**，而新建沙箱正常。

`beta_pause` 那一支需要 SDK 有这个方法（2.20.0 有）；没有就退回只做 kill，并把
那几轮记成 skipped。被 kill 的沙箱不能复用，所以每轮现开一个"祭品"沙箱；
旁观沙箱从头活到尾。
"""

NAME = "T11"

import os
import random
import threading
import time

from .. import common
from ..common import expect, log, note

BASIS = ("方案 §4.1 T11；服务端上限见 infra-arm jll e4c0e9f11（锁 60 s + FC 调用 120 s）、"
         "kill 与在途操作互斥见 b1df1fb5f")


def add_args(ap):
    ap.add_argument("--sandboxes", type=int, default=3,
                    help="沙箱数：1 个祭品 + 其余旁观（默认 3）")
    ap.add_argument("--rounds", type=int, default=5, help="轮数（默认 5）")
    ap.add_argument("--delay-max-ms", type=float, default=50.0,
                    help="在途操作开始后等多久再下手，0–这个值之间随机（默认 50 ms）")
    ap.add_argument("--op-timeout", type=float, default=200.0,
                    help="在途操作的判失败线，秒（默认 200 = 60 锁 + 120 FC + 余量）")
    ap.add_argument("--reclaim-wait", type=float, default=60.0,
                    help="kill 之后等资源回收的上限秒数（默认 60）")
    ap.add_argument("--seed", type=int, default=20260917, help="随机种子")
    return ap


# ---------------------------------------------------------------- 小工具

def _prep(box, gen="g0"):
    box.setup(warm_mem=32, warm_file=8)
    box.dirty(gen, mem_mb=16, file_mb=4)


def _seed_checkpoint(ctx, box, step):
    rec = ctx.op(box.create("cp0"), stage="T11", step=step)
    expect(ctx, "%s 的 cp0 建出来" % box.label, rec.get("ok"), "成功",
           rec.get("err") or "ok", "T11 前置")
    return rec["id"]


def _reclaimed(ctx, box, deadline):
    """等到（或等不到）宿主机侧收干净。返回 (目录没了, 残留 fc pid)。"""
    store_gone, procs = False, common.fc_processes(box.id)
    while time.monotonic() < deadline:
        store_gone = not (ctx.store and os.path.isdir(os.path.join(ctx.store, box.id)))
        procs = common.fc_processes(box.id)
        if store_gone and not procs:
            break
        time.sleep(1.0)
    return store_gone, procs


def _connect_dead(ctx, box):
    """同 id 再 connect() 应当用不了。connect 本身就抛，或者连上之后跑命令抛，
    两种都算"用不了"；真跑通了才是问题。"""
    try:
        dead = common.connect_box(ctx, box.id, box.label + "-dead")
    except Exception as e:      # noqa: BLE001
        return True, "connect() 就抛了：%s: %s" % (type(e).__name__, e)
    try:
        dead.run("echo hi", timeout=20)
    except Exception as e:      # noqa: BLE001
        return True, "connect() 过了但命令抛了：%s: %s" % (type(e).__name__, e)
    return False, "居然还能在里面跑命令"


def _bystander_round(ctx, box, r):
    """旁观沙箱照常做一次 create + restore —— 服务端还活着的证据。"""
    box.dirty("b%d" % r, mem_mb=16, file_mb=4)
    rec = ctx.op(box.create("b%d" % r), stage="T11", step="bystander-create", round=r)
    expect(ctx, "第 %d 轮旁观沙箱 create 正常" % (r + 1), rec.get("ok"), "成功",
           rec.get("err") or "%.2f s" % rec.get("wall_s", 0),
           "方案 §4.1 T11：其它沙箱不受影响")
    rr = ctx.op(box.restore(rec["id"]), stage="T11", step="bystander-restore", round=r)
    expect(ctx, "第 %d 轮旁观沙箱 restore 正常且现场一致" % (r + 1),
           rr.get("ok") and rr.get("verified") is not False, "成功且五项全同",
           rr.get("err") or rr.get("mismatch") or "ok", "同上")


# ---------------------------------------------------------------- 主体

def run(ctx):
    a = ctx.args
    rnd = random.Random(a.seed)
    boxes = common.spawn(ctx, a.sandboxes, "t11-")
    victim, bystanders = boxes[0], boxes[1:]
    for b in boxes:
        log("  沙箱 %s（%s）" % (b.id, b.label))
        _prep(b)
    for b in bystanders:
        _seed_checkpoint(ctx, b, "bystander-cp0")
    cp0 = _seed_checkpoint(ctx, victim, "victim-cp0")

    has_pause = hasattr(victim.sbx, "beta_pause")
    if not has_pause:
        note(ctx, "SDK 有没有 beta_pause", "有就交替 kill / beta_pause",
             "没有，pause 那几轮记为 skipped，只做 kill",
             "方案 §4.1 T11：pause 支需要 SDK 支持")
    rounds = []

    for r in range(a.rounds):
        op = "create" if r % 2 == 0 else "restore"
        action = "kill" if (r % 4) < 2 or not has_pause else "beta_pause"
        skipped = (r % 4) >= 2 and not has_pause
        log("\n  --- 第 %d/%d 轮：在途 %s，撞它的是 %s%s ---"
            % (r + 1, a.rounds, op, action, "（本想 beta_pause，SDK 没有）" if skipped else ""))

        netns_before = common.netns_count_steady()
        result = {}

        def inflight():
            if op == "create":
                result["rec"] = victim.create("race-%d" % r, record_scene=False)
            else:
                result["rec"] = victim.restore(cp0, verify=False)

        th = threading.Thread(target=inflight, daemon=True)
        t0 = time.monotonic()
        th.start()
        delay = rnd.uniform(0, a.delay_max_ms) / 1000.0
        time.sleep(delay)

        act = {"op": action, "box": victim.label, "sandbox": victim.id, "stage": "T11",
               "round": r, "inflight": op, "delay_ms": delay * 1000.0, "skipped": skipped}
        ta = time.monotonic()
        try:
            if action == "kill":
                victim.sbx.kill()
            else:
                victim.sbx.beta_pause()
            act["ok"] = True
        except Exception as e:      # noqa: BLE001
            act["ok"] = False
            common.record_error(act, e)
        act["wall_s"] = time.monotonic() - ta
        ctx.op(act)
        log("  %s 用了 %.2f s（%s）" % (action, act["wall_s"], act.get("err") or "ok"))

        th.join(timeout=a.op_timeout)
        rec = result.get("rec")
        wall = None if th.is_alive() else (rec or {}).get("wall_s")
        if rec is not None:
            ctx.op(rec, stage="T11", step="inflight", round=r, raced_by=action,
                   delay_ms=delay * 1000.0)
        info = common.rec_error_info(rec) if rec and not rec.get("ok") else None

        expect(ctx, "第 %d 轮 %s 本身不许挂住" % (r + 1, action),
               act["wall_s"] <= a.op_timeout, "≤ %g s" % a.op_timeout,
               "%.1f s" % act["wall_s"], BASIS)
        ok, want, got = common.judge_t11_op(
            bool(rec and rec.get("ok")), (rec or {}).get("err"), wall, a.op_timeout, info)
        expect(ctx, "第 %d 轮在途 %s 有说法" % (r + 1, op), ok, want, got, BASIS)
        note(ctx, "第 %d 轮在途 %s 的结局" % (r + 1, op), "只记录（成功/失败都算过）",
             "ok=%s code=%s reason=%s（出处 %s）"
             % (bool(rec and rec.get("ok")), (info or {}).get("code"),
                (info or {}).get("reason"), (info or {}).get("reason_src")),
             "错误分型见 common.error_info；SDK 异常层次还在做，本用例不依赖它")

        row = {"round": r, "inflight": op, "action": action, "delay_ms": delay * 1000.0,
               "inflight_ok": bool(rec and rec.get("ok")), "inflight_wall_s": wall,
               "action_wall_s": act["wall_s"], "code": (info or {}).get("code"),
               "reason": (info or {}).get("reason"), "skipped": skipped}

        if action == "kill":
            deadline = time.monotonic() + a.reclaim_wait
            store_gone, procs = _reclaimed(ctx, victim, deadline)
            netns_after = common.netns_count_steady()
            for name, ok2, want2, got2 in common.judge_reclaim(
                    store_gone, procs, netns_before, netns_after):
                if ok2 is None:
                    note(ctx, "第 %d 轮 %s" % (r + 1, name), want2, got2, BASIS)
                else:
                    expect(ctx, "第 %d 轮 %s" % (r + 1, name), ok2, want2, got2,
                           "方案 §4.1 T11：被 kill 的沙箱资源回收（netns、FC、层文件）")
            ok2, detail = _connect_dead(ctx, victim)
            expect(ctx, "第 %d 轮 kill 之后同 id connect 用不了" % (r + 1), ok2,
                   "connect 或其后的命令失败", detail, "方案 §4.1 T11")
            row.update({"store_gone": store_gone, "fc_left": procs,
                        "netns_before": netns_before, "netns_after": netns_after})
            common.forget(victim)
        else:
            # pause 之后沙箱还在：连回去（SDK 的 connect 会把它唤醒），
            # 要么用得了，要么给个明确错误 —— 两种都记，不判。
            try:
                back = common.connect_box(ctx, victim.id, victim.label + "-back")
                alive, d = back.alive(timeout=120)
                row["after_pause"] = "alive" if alive else "connect 上了但不活：%s" % d
            except Exception as e:      # noqa: BLE001
                row["after_pause"] = "%s: %s" % (type(e).__name__, e)
            note(ctx, "第 %d 轮 beta_pause 之后沙箱的样子" % (r + 1),
                 "只记录（唤醒可用 / 明确错误都算）", row["after_pause"],
                 "方案 §4.1 T11：pause 与在途操作交叉的既定口径还没定，先记症状")
            victim.kill()
            common.forget(victim)

        rounds.append(row)
        if bystanders:
            _bystander_round(ctx, bystanders[0], r)

        if r + 1 < a.rounds:
            victim = common.spawn(ctx, 1, "t11-v%d-" % (r + 1))[0]
            log("  新祭品沙箱 %s" % victim.id)
            _prep(victim)
            cp0 = _seed_checkpoint(ctx, victim, "victim-cp0")

    # 收尾冒烟：新沙箱、建→改→回→现场逐项一致。
    log("\n  --- 收尾冒烟 ---")
    fresh = common.spawn(ctx, 1, "t11-smoke-")[0]
    _prep(fresh)
    rec = ctx.op(fresh.create("smoke"), stage="T11", step="smoke-create")
    expect(ctx, "收尾：新沙箱能 create", rec.get("ok"), "成功", rec.get("err") or "ok",
           "方案 §4.1 T11：orchestrator 不崩")
    fresh.dirty("g9", mem_mb=16, file_mb=4)
    rr = ctx.op(fresh.restore(rec["id"]), stage="T11", step="smoke-restore")
    expect(ctx, "收尾：restore 回来且现场逐项一致",
           rr.get("ok") and rr.get("verified") is True, "成功且五项全同",
           rr.get("err") or rr.get("mismatch") or "ok",
           "现场口径同 checkpoint_verify.py")
    ok, d = fresh.alive()
    expect(ctx, "收尾：沙箱可用", ok, "命令能跑、能写盘、心跳在推进", d,
           "活体判据同 checkpoint_verify.py")

    for b in bystanders:
        ok, d = b.alive()
        expect(ctx, "%s 全程活着" % b.label, ok, "命令能跑、能写盘、心跳在推进", d,
               "方案 §4.1 T11：其它沙箱不受影响")

    ctx.results["summary"]["T11"] = {
        "rounds": a.rounds, "sandboxes": a.sandboxes,
        "kills": len([r for r in rounds if r["action"] == "kill"]),
        "pauses": len([r for r in rounds if r["action"] == "beta_pause"]),
        "skipped_pause": len([r for r in rounds if r["skipped"]]),
        "inflight_ok": len([r for r in rounds if r["inflight_ok"]]),
        "inflight_wall_max_s": common.pmax([r["inflight_wall_s"] for r in rounds]),
        "reasons": sorted({r["reason"] for r in rounds if r["reason"]}),
        "per_round": rounds,
    }
