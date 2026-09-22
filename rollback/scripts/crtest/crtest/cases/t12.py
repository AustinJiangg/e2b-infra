# -*- coding: utf-8 -*-
"""
T12 同沙箱多客户端交错（方案 §4.1）。

4 个客户端各自 `Sandbox.connect()` 连同一个沙箱（这才是"多个独立调用方"；SDK 少
路由头的老环境会在这里当场炸，见并发报告 §6.3），随机交错 create / restore /
delete / list，并且**故意去 delete 别人正在 restore 的那个 checkpoint**。

服务端按沙箱串行（`Store.LockSandbox`），所以预期是排队而不是出错。判定只放在
"风暴停下来之后"，因为并发进行中的 `list` 本来就没有确定答案：

  · 无幽灵：`list` 里的每一个 id 都必须是某次**成功**的 create 返回过的（或是 cp0）；
  · 删干净：每一个**成功** delete 掉的 id 都不许再出现在 `list` 里；
  · 不失踪：成功 create 且没被成功 delete 的 id 必须还在 `list` 里；
  · restore 到已经删掉的 checkpoint 必须回 not_found，不许"成功"也不许别的错；
  · 风暴过后沙箱还能用，残留的每个 checkpoint 都还能回。

**不**断言现场逐项一致：并发下"拍那一刻的现场"没有定义（并发报告 §4 已论证）。
"""

NAME = "T12"

import random
import threading
import time

from .. import common
from ..common import expect, log, note


def add_args(ap):
    ap.add_argument("--clients", type=int, default=4, help="客户端数（默认 4）")
    ap.add_argument("--rounds", type=int, default=8, help="每个客户端的轮数（默认 8）")
    ap.add_argument("--seed", type=int, default=20260917, help="随机种子")
    return ap


def run(ctx):
    a = ctx.args
    owner = common.spawn(ctx, 1, "t12-")[0]
    log("  沙箱 %s" % owner.id)
    owner.setup(warm_mem=32, warm_file=8)
    owner.dirty("g0", mem_mb=16, file_mb=4)
    crec = ctx.op(owner.create("cp0"), stage="T12", step="cp0")
    expect(ctx, "建 cp0", crec.get("ok"), "成功", crec.get("err") or "ok", "T12 前置")
    cp0 = crec["id"]

    clients = []
    for i in range(a.clients):
        c = common.connect_box(ctx, owner.id, "c%d" % i)
        try:
            c.list_ids()
        except Exception as e:      # noqa: BLE001
            raise common.Failed("Sandbox.connect() 能打 checkpoint 接口",
                                "list 成功", "%s: %s" % (type(e).__name__, e),
                                "并发报告 §6.3：SDK 缺沙箱路由头（e2b-arm 66414855 修）")
        clients.append(c)

    lock = threading.Lock()
    created_ok = {cp0}
    deleted_ok = set()
    in_flight = []              # 正在被 restore 的 id，给 delete 去撞
    rnd = random.Random(a.seed)
    errs = []

    def alive_ids():
        with lock:
            return sorted(created_ok - deleted_ok)

    def worker(c, ci):
        try:
            for r in range(a.rounds):
                choice = rnd.choice(["create", "restore", "restore", "delete", "list"])
                if choice == "create":
                    rec = ctx.op(c.create("c%d-r%d" % (ci, r), record_scene=False),
                                 stage="T12", client=ci, round=r)
                    if rec.get("ok"):
                        with lock:
                            created_ok.add(rec["id"])
                elif choice == "restore":
                    ids = alive_ids()
                    if not ids:
                        continue
                    target = rnd.choice(ids)
                    with lock:
                        in_flight.append(target)
                    rec = ctx.op(c.restore(target, verify=False),
                                 stage="T12", client=ci, round=r)
                    with lock:
                        if target in in_flight:
                            in_flight.remove(target)
                elif choice == "delete":
                    with lock:
                        target = rnd.choice(in_flight) if in_flight and rnd.random() < 0.5 else None
                    if target is None:
                        ids = [i for i in alive_ids() if i != cp0]
                        if not ids:
                            continue
                        target = rnd.choice(ids)
                    rec = ctx.op(c.delete(target), stage="T12", client=ci, round=r,
                                 raced_restore=target in in_flight)
                    if rec.get("ok"):
                        with lock:
                            deleted_ok.add(target)
                else:
                    ctx.op(c.list_rec(), stage="T12", client=ci, round=r)
        except Exception as e:      # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(c, i)) for i, c in enumerate(clients)]
    t0 = time.monotonic()
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=1800)
    log("  风暴结束，%.1f s，%d 条操作" % (time.monotonic() - t0, len(ctx.results["ops"])))
    if errs:
        raise errs[0]

    time.sleep(2)               # 让服务端把排队的活干完再对账
    final = ctx.op(owner.list_rec(), stage="T12", step="final")
    expect(ctx, "风暴后 list 成功", final.get("ok"), "成功", final.get("err") or "ok", "T12")
    got = set(final.get("ids") or [])

    ghosts = got - created_ok
    expect(ctx, "无幽灵 checkpoint", not ghosts, "list ⊆ 成功 create 过的 id",
           "多出来 %s" % sorted(ghosts),
           "方案 §4.1 T12：服务端串行化正确，list 与实际树一致")
    zombies = got & deleted_ok
    expect(ctx, "成功删掉的不再出现", not zombies, "list ∩ 已删 = 空",
           "还在 %s" % sorted(zombies), "方案 §4.1 T12")
    missing = (created_ok - deleted_ok) - got
    expect(ctx, "没被删的不许失踪", not missing, "成功 create 且没成功 delete 的都还在",
           "少了 %s" % sorted(missing),
           "方案 §4.1 T12；若服务端有「删父级联删子」的语义，这条会亮，属于要确认的口径")

    if deleted_ok:
        gone = sorted(deleted_ok)[0]
        rec = ctx.op(owner.restore(gone, verify=False), stage="T12", step="restore-deleted")
        err = rec.get("err") or ""
        expect(ctx, "restore 到已删 checkpoint 回 not_found",
               (not rec.get("ok")) and bool(common.NOTFOUND_RE.search(err)),
               "失败且错误里带 not_found/404",
               ("居然成功了" if rec.get("ok") else err) or "空错误",
               "方案 §4.1 T12")
    else:
        note(ctx, "restore 到已删 checkpoint", "本轮没有成功的 delete", "跳过", "方案 §4.1 T12")

    for ck in sorted(got):
        rec = ctx.op(owner.restore(ck, verify=False), stage="T12", step="restore-survivor")
        expect(ctx, "残留 checkpoint %s 还能回" % ck, rec.get("ok"), "成功",
               rec.get("err") or "%.3f s" % rec.get("wall_s", 0),
               "方案 §4.1 T12：风暴过后每个 checkpoint 都能回")

    ok, d = owner.alive()
    expect(ctx, "风暴过后沙箱可用", ok, "命令能跑、能写盘、心跳在推进", d,
           "活体判据同 checkpoint_verify.py")
    ctx.results["summary"]["T12"] = {
        "clients": a.clients, "rounds": a.rounds,
        "created_ok": len(created_ok), "deleted_ok": len(deleted_ok),
        "final_list": sorted(got),
        "ops": len([o for o in ctx.results["ops"] if o.get("stage") == "T12"]),
        "failed_ops": len([o for o in ctx.results["ops"]
                           if o.get("stage") == "T12" and o.get("ok") is False]),
    }
