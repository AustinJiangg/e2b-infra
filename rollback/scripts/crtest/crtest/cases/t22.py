# -*- coding: utf-8 -*-
"""
T22 深链随机回滚（方案 §4.2）。

建一条有分支的链（默认 40 层，每 8 层从一个随机祖先分出去一支），然后**随机顺序**
回其中若干个并逐项验现场；最后删掉中间的一层，再回它的一个后代。

判定：
  · 随机顺序回每一个 checkpoint，现场都必须 = 拍它那一刻（深链下 revertPath 把
    多层位图并起来，错一层就露馅）；
  · 删掉中间层之后，它的后代**仍然能回且内容不变**（评审 A3/F5、方案 §4.2 T22）；
  · 结束时 list 与本地账本一致。

需求书写的是 200 层：那是长跑规模（40 层已经要 1 分多钟），用 `--depth 200` 开。
"""

NAME = "T22"

import random

from .. import common
from ..common import expect, log, note


def add_args(ap):
    ap.add_argument("--depth", type=int, default=40, help="建多少个 checkpoint（默认 40）")
    ap.add_argument("--branch-every", type=int, default=8,
                    help="每隔几层从随机祖先分一支（默认 8；0 = 纯直链）")
    ap.add_argument("--sample", type=int, default=12, help="随机回几个（默认 12）")
    ap.add_argument("--seed", type=int, default=20260917, help="随机种子")
    return ap


def run(ctx):
    a = ctx.args
    rnd = random.Random(a.seed)
    box = common.spawn(ctx, 1, "t22-")[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)

    ledger = []          # 建出来的顺序
    parent = {}          # id -> 它是在哪个 checkpoint 的基础上拍的（None = 从当前现场直接拍）
    log("  建链：%d 层，每 %s 层分一支" % (a.depth, a.branch_every or "∞"))
    for i in range(a.depth):
        base = None
        if a.branch_every and ledger and (i % a.branch_every == 0):
            base = rnd.choice(ledger)
            rr = ctx.op(box.restore(base, verify=True), stage="T22", step="branch", idx=i)
            expect(ctx, "分支前回到 %s" % base, rr.get("ok"), "成功",
                   rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
                   "T22 前置；失败现场 %s" % box.store_dir(base))
            expect(ctx, "分支前现场一致（第 %d 层）" % i, rr.get("verified") is not False,
                   "五项全同", rr.get("mismatch") or "全同", "方案 §4.2 T22")
        box.dirty("g%d" % i, mem_mb=8, file_mb=2)
        rec = ctx.op(box.create("g%d" % i), stage="T22", step="build", idx=i, depth=len(ledger))
        expect(ctx, "建第 %d 层" % i, rec.get("ok"), "成功",
               rec.get("err") or "%.3f s" % rec.get("wall_s", 0), "T22 前置")
        ledger.append(rec["id"])
        parent[rec["id"]] = base

    walls = [o["wall_s"] for o in ctx.results["ops"]
             if o.get("step") == "build" and o.get("ok")]
    note(ctx, "建链 create 客户端 p50/max", "只记录",
         "%s / %s s" % (common.fmt_s(common.p50(walls)), common.fmt_s(common.pmax(walls))),
         "对照并发报告 D 段的链深分桶")

    picks = rnd.sample(ledger, min(a.sample, len(ledger)))
    log("\n  随机顺序回 %d 个" % len(picks))
    for ck in picks:
        rr = ctx.op(box.restore(ck, verify=True), stage="T22", step="random-restore")
        expect(ctx, "随机回 %s" % ck, rr.get("ok"), "成功",
               rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
               "方案 §4.2 T22；失败现场 %s" % box.store_dir(ck))
        expect(ctx, "随机回 %s 现场一致" % ck, rr.get("verified") is not False,
               "五项全同", rr.get("mismatch") or "全同",
               "方案 §4.2 T22：深链下每个 checkpoint 的内容 = 拍它那一刻")

    # 删中间层，再回它的后代（后代 = 在它之后建出来的、不属于别的分支的那些）。
    mid = ledger[len(ledger) // 2]
    later = [c for c in ledger[len(ledger) // 2 + 1:] if parent.get(c) is None]
    heir = later[-1] if later else ledger[-1]
    drec = ctx.op(box.delete(mid), stage="T22", step="delete-middle")
    expect(ctx, "删中间层 %s" % mid, drec.get("ok"), "成功", drec.get("err") or "ok",
           "方案 §4.2 T22：删中间层再 restore 后代")
    scene_of_heir = box.scenes.get(heir)
    rr = ctx.op(box.restore(heir, verify=True), stage="T22", step="restore-after-delete")
    expect(ctx, "删中间层之后后代 %s 还能回" % heir, rr.get("ok"), "成功",
           rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
           "方案 §4.2 T22；失败现场 %s" % box.store_dir(heir))
    expect(ctx, "删中间层之后后代现场不变", rr.get("verified") is not False,
           "五项全同（%s）" % (scene_of_heir or "无记录"), rr.get("mismatch") or "全同",
           "方案 §4.2 T22：删掉的那层的数据必须已经被并进后代（A3/F5）")

    lst = ctx.op(box.list_rec(), stage="T22", step="final")
    want = set(ledger) - {mid}
    got = set(lst.get("ids") or [])
    expect(ctx, "list 与本地账本一致", got == want,
           "%d 条" % len(want), "%d 条，差集 %s" % (len(got), sorted(want ^ got)),
           "方案 §4.2 T22：链深/分支数上报正确")

    ok, d = box.alive()
    expect(ctx, "跑完之后沙箱可用", ok, "命令能跑、能写盘、心跳在推进", d,
           "活体判据同 checkpoint_verify.py")
    rwalls = [o["wall_s"] for o in ctx.results["ops"]
              if o.get("op") == "restore" and o.get("ok")]
    ctx.results["summary"]["T22"] = {
        "depth": a.depth, "branch_every": a.branch_every, "sampled": len(picks),
        "create_p50_s": common.p50(walls), "restore_p50_s": common.p50(rwalls),
        "restore_max_s": common.pmax(rwalls), "deleted_middle": mid, "heir": heir,
    }
