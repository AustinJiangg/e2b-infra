# -*- coding: utf-8 -*-
"""
T32 restore 随脏集（方案 §4.3）。

一个沙箱，一个 checkpoint，反复回同一个目标，只改一件事：**回之前弄脏多少内存**
（默认 0 / 64 / 256 MB 三档，每档 5 次）。要看的是服务端分段随脏集怎么动：

  · `fc_memory`（搬脏页）应当随脏集**单调不降** —— 它就是 O(脏页) 的那一项；
  · `fc_bitmap`（扫位图，FC 第 1 轮 F3 新加的 `timings_us.bitmap`，orchestrator
    O7 落成 `fc_bitmap`）应当与脏集**无关** —— 三档 p50 相差 < 2 倍；
  · 顺带记 `materialize`（宿主机侧拼出目标内存）、`frozen`（guest 冻结窗口）
    和客户端墙钟。

fc_bitmap 一列全是空的话，多半是服务端没带 `timings_us.bitmap`（O7/F3 没上），
判定会直接失败并把这句写进"依据"。
"""

NAME = "T32"

import time

from .. import common
from ..common import expect, log, note

DIRTY_FILE = "/dev/shm/t32_dirty"
KEYS = ("fc_memory", "fc_bitmap", "materialize", "frozen", "fc_total", "total")


def add_args(ap):
    ap.add_argument("--dirty", default="0,64,256", help="脏集档位 MB（默认 0,64,256）")
    ap.add_argument("--repeat", type=int, default=5, help="每档 restore 次数（默认 5）")
    ap.add_argument("--bitmap-ratio", type=float, default=2.0,
                    help="fc_bitmap 三档极差上限倍数（默认 2）")
    return ap


def run(ctx):
    a = ctx.args
    dirty = [int(x) for x in a.dirty.split(",") if x.strip()]
    box = common.spawn(ctx, 1, "t32-")[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)
    box.dirty("g0", mem_mb=16, file_mb=4)
    crec = ctx.op(box.create("g0"), stage="T32", step="cp0")
    expect(ctx, "建 checkpoint", crec.get("ok"), "成功", crec.get("err") or "ok", "T32 前置")
    cp0 = crec["id"]
    if not ctx.store:
        raise common.Failed("能读到服务端分段", "在宿主机上跑（读 last-restore-timings.json）",
                            "读不到 orchestrator 的产物目录",
                            "并发报告 §4：分段一个字段都没进 HTTP 响应（评审 C3）")

    rows = []
    for d in dirty:
        vals = {k: [] for k in KEYS}
        walls = []
        for i in range(a.repeat):
            if d > 0:
                box.run("dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null" % (DIRTY_FILE, d),
                        timeout=600)
            else:
                box.run("rm -f %s" % DIRTY_FILE)
            time.sleep(0.5)
            rr = ctx.op(box.restore(cp0, verify=False), stage="T32", dirty_mb=d, round=i)
            expect(ctx, "脏集 %d MB 第 %d 次 restore" % (d, i + 1), rr.get("ok"), "成功",
                   rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
                   "方案 §4.3 T32；失败现场 %s" % box.store_dir(cp0))
            ph = rr.get("phases") or {}
            for k in KEYS:
                vals[k].append(ph.get(k))
            walls.append(rr.get("wall_s"))
        rows.append({"dirty_mb": d, "wall_s": walls, **vals})
        log("  脏集 %d MB：fc_memory p50 %s ms，fc_bitmap p50 %s ms，materialize p50 %s ms，"
            "frozen p50 %s ms，客户端 p50 %s s"
            % (d, common.fmt_ms(common.p50(vals["fc_memory"])),
               common.fmt_ms(common.p50(vals["fc_bitmap"])),
               common.fmt_ms(common.p50(vals["materialize"])),
               common.fmt_ms(common.p50(vals["frozen"])), common.fmt_s(common.p50(walls))))

    common.table(["脏集 MB", "次数", "fc_memory p50", "fc_bitmap p50", "materialize p50",
                  "frozen p50", "客户端 p50 s"],
                 [[str(r["dirty_mb"]), str(len(r["wall_s"])),
                   common.fmt_ms(common.p50(r["fc_memory"])),
                   common.fmt_ms(common.p50(r["fc_bitmap"])),
                   common.fmt_ms(common.p50(r["materialize"])),
                   common.fmt_ms(common.p50(r["frozen"])),
                   common.fmt_s(common.p50(r["wall_s"]))] for r in rows])

    for name, ok, want, got in common.judge_t32(rows, a.bitmap_ratio):
        expect(ctx, name, ok, want, got,
               "方案 §4.3 T32；FC 第 1 轮 F3（C1 分段）+ orchestrator O7（fc_bitmap）")

    note(ctx, "frozen 随脏集", "只记录",
         ", ".join("%d MB: %s ms" % (r["dirty_mb"], common.fmt_ms(common.p50(r["frozen"])))
                   for r in rows), "第 4 轮冻结窗口的基线")
    ctx.results["summary"]["T32"] = [
        {"dirty_mb": r["dirty_mb"],
         **{k: common.p50(r[k]) for k in KEYS},
         "wall_p50_s": common.p50(r["wall_s"])} for r in rows]
