# -*- coding: utf-8 -*-
"""
T15 树形分支并发（方案 §4.1）。

每个沙箱**内部串行**（所以"拍那一刻的现场"是良定义的），但若干沙箱同时在做：
回到自己的一个随机祖先 → 改一代 → 再 create（于是树长出分支）。

判定：
  · 每次 restore 之后现场逐项 = 拍它那一刻（内存代号、文件代号、两个 blob md5、
    心跳 pid）；
  · `list` 的条数 = 本地账本（成功 create 数 + cp0）—— 分支时被盖掉的 hidden 条目
    不许冒出来（评审 A3 / F5 的孤儿 snapfile 就会在这里露头）；
  · 跑完之后每个沙箱都还能用。

默认 4 个沙箱 × 6 轮（需求书写的是 16 个，那是长跑规模，用 `--sandboxes 16` 开）。
"""

NAME = "T15"

import random
import threading

from .. import common
from ..common import expect, log, note


def add_args(ap):
    ap.add_argument("--sandboxes", type=int, default=4, help="并发沙箱数（默认 4）")
    ap.add_argument("--rounds", type=int, default=6, help="每个沙箱的轮数（默认 6）")
    ap.add_argument("--seed", type=int, default=20260917, help="随机种子")
    return ap


def run(ctx):
    a = ctx.args
    boxes = common.spawn(ctx, a.sandboxes, "t15-")
    for b in boxes:
        log("  沙箱 %s = %s" % (b.label, b.id))
    errs = {}
    depth = {}

    def worker(b, seed):
        rnd = random.Random(seed)
        try:
            b.setup(warm_mem=32, warm_file=8)
            b.dirty("g0", mem_mb=16, file_mb=4)
            rec = ctx.op(b.create("g0"), stage="T15", box=b.label, round=-1)
            expect(ctx, "%s 建根 checkpoint" % b.label, rec.get("ok"), "成功",
                   rec.get("err") or "ok", "T15 前置")
            ledger = [rec["id"]]

            for r in range(a.rounds):
                # 回到随机祖先：这一步把树"分叉点"挪到历史里的某一处。
                target = rnd.choice(ledger)
                rr = ctx.op(b.restore(target, verify=True), stage="T15", box=b.label, round=r,
                            branch_from=target)
                expect(ctx, "%s 第 %d 轮 restore" % (b.label, r + 1), rr.get("ok"), "成功",
                       rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
                       "方案 §4.1 T15；失败现场 %s" % b.store_dir(target))
                expect(ctx, "%s 第 %d 轮现场 = 拍它那一刻" % (b.label, r + 1),
                       rr.get("verified") is not False, "五项全同",
                       rr.get("mismatch") or "全同",
                       "方案 §4.1 T15：每个 checkpoint 的内容 = 拍它那一刻")

                b.dirty("b%d-%d" % (r, rnd.randrange(1000)), mem_mb=16, file_mb=4)
                cr = ctx.op(b.create("branch-%d" % r, ), stage="T15", box=b.label, round=r)
                expect(ctx, "%s 第 %d 轮分支 create" % (b.label, r + 1), cr.get("ok"), "成功",
                       cr.get("err") or "%.3f s" % cr.get("wall_s", 0), "方案 §4.1 T15")
                ledger.append(cr["id"])

            lst = ctx.op(b.list_rec(), stage="T15", box=b.label, step="final")
            got = set(lst.get("ids") or [])
            expect(ctx, "%s list 与本地账本一致" % b.label, got == set(ledger),
                   "%d 条：%s" % (len(ledger), sorted(ledger)),
                   "%d 条：%s" % (len(got), sorted(got)),
                   "方案 §4.1 T15：hidden 条目要回收，链深/分支数上报正确（A3/F5）")
            depth[b.label] = len(ledger)

            ok, d = b.alive()
            expect(ctx, "%s 跑完可用" % b.label, ok, "命令能跑、能写盘、心跳在推进", d,
                   "活体判据同 checkpoint_verify.py")
        except Exception as e:          # noqa: BLE001
            errs[b.label] = e

    ts = [threading.Thread(target=worker, args=(b, a.seed + i)) for i, b in enumerate(boxes)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if errs:
        raise list(errs.values())[0]

    restores = [o for o in ctx.results["ops"] if o.get("op") == "restore" and o.get("ok")]
    note(ctx, "restore 客户端 p50/max", "只记录",
         "%s / %s s" % (common.fmt_s(common.p50([o["wall_s"] for o in restores])),
                        common.fmt_s(common.pmax([o["wall_s"] for o in restores]))),
         "对照并发报告 D 段（按链深分桶）")
    ctx.results["summary"]["T15"] = {
        "sandboxes": len(boxes), "rounds": a.rounds, "depth": depth,
        "restores": len(restores),
        "verified": "%d/%d" % (sum(1 for o in restores if o.get("verified")),
                               sum(1 for o in restores if "verified" in o)),
    }
