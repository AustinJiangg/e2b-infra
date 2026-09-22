# -*- coding: utf-8 -*-
"""
T13 跨沙箱冻结窗口干扰（方案 §4.1，对应 K4 / 第 2 轮验收口径 T33）。

N−1 个沙箱一直做"写 128 MB 脏页 → create"，第 N 个沙箱每 5 s 回一次同一个
checkpoint，并且 guest 里养一个 10 ms 的心跳。要的是三条曲线随 N 怎么涨：

  · 第 N 个沙箱每次 restore 的**客户端耗时**；
  · 服务端 `last-restore-timings.json` 的 **frozen**（guest 真被冻住多久）；
  · guest 心跳的**最大间隔**。

心跳这一条要小心读：一次 restore 把 guest 整个搬回快照那一刻，心跳文件本身也
回滚，monotonic 倒退、realtime 被 envd 拨回当前 —— 跨过 restore 的那一"跳"等于
create 到 restore 的间隔，**不是停顿**。所以心跳文件在每次 restore 之前先抄走，
统计时按 monotonic 倒退切段、段内一律用 **monotonic** 相邻差、并且丢掉每段的
第一跳（`common.heartbeat_gaps`）。**段内也不能用 realtime**：envd 拨钟发生在
monotonic 倒退之后，那一跳落在新段**段内**，会让"最大间隔"变成距 checkpoint 的
时长（第 1 轮数据就是 13 段线性递增 5.2 s，是口径错，不是停顿）。
除了 max 还输出 `hb_gap_p99_ms` 与 `hb_gaps_gt_100ms`，第 2 轮好看分布。
它量的是"这个沙箱正常跑着的时候，被别人的 create 卡了多久"—— K4 说的就是
别人 create 的 fsync 会变成这个沙箱的停顿。

这一段**只记录不判定**（需求书写的是"记录曲线"）：硬断言只有"restore 全都成功、
现场每次都对、心跳一直有数据"。第 2 轮去 fsync 之后拿同样的命令再跑一遍作对比。
"""

NAME = "T13"

import threading
import time

from .. import common
from ..common import expect, log, note

HB_FILE = "/dev/shm/t13_hb"
HB_SCRIPT = "/dev/shm/t13_hb.py"
HB_PIDF = "/dev/shm/t13_hb.pid"
NOISE_FILE = "/dev/shm/t13_noise"


def add_args(ap):
    ap.add_argument("--fanout", default="1,2,4", help="N 序列（默认 1,2,4）")
    ap.add_argument("--seconds", type=float, default=60.0, help="每个 N 跑多久（默认 60 s）")
    ap.add_argument("--every", type=float, default=5.0, help="第 N 个沙箱多久 restore 一次（默认 5 s）")
    ap.add_argument("--dirty-mb", type=int, default=128, help="干扰沙箱每次 create 前写多少脏页（默认 128 MB）")
    ap.add_argument("--hb-ms", type=float, default=10.0, help="心跳间隔毫秒（默认 10）")
    return ap


def run_one(ctx, n):
    """一个 N：1 个受害沙箱 + (n-1) 个干扰沙箱。返回摘要行。"""
    a = ctx.args
    log("\n  --- N = %d（1 个 restore + %d 个大脏集 create）---" % (n, n - 1))
    boxes = common.spawn(ctx, n, "t13n%d-" % n)
    victim, noisy = boxes[-1], boxes[:-1]
    log("  受害沙箱 %s；干扰沙箱 %s" % (victim.id, ", ".join(b.id for b in noisy) or "无"))

    victim.setup(warm_mem=32, warm_file=8)
    victim.put(HB_SCRIPT, common.GUEST_HEARTBEAT)
    victim.run("rm -f %s" % HB_FILE)
    hb_pid = victim.bg("python3 %s %s %.4f" % (HB_SCRIPT, HB_FILE, a.hb_ms / 1000.0), HB_PIDF)
    victim.dirty("g0", mem_mb=32, file_mb=8)
    crec = ctx.op(victim.create("g0"), stage="T13", n=n, role="victim")
    expect(ctx, "N=%d 受害沙箱建 checkpoint" % n, crec.get("ok"), "成功",
           crec.get("err") or "ok", "T13 前置")
    cp0 = crec["id"]

    for b in noisy:
        b.run("mkdir -p %s" % common.BENCH_DIR)

    stop_flag = threading.Event()
    errs = []

    def noise(b):
        """写 128 MB 脏页 → create，一直循环。"""
        i = 0
        while not stop_flag.is_set():
            try:
                b.run("dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null"
                      % (NOISE_FILE, a.dirty_mb), timeout=600)
                rec = ctx.op(b.create("noise-%d" % i, record_scene=False),
                             stage="T13", n=n, role="noise")
                if not rec.get("ok"):
                    log("    干扰沙箱 %s create 失败：%s" % (b.label, rec.get("err")))
                i += 1
            except Exception as e:          # noqa: BLE001
                errs.append(e)
                return

    ts = [threading.Thread(target=noise, args=(b,)) for b in noisy]
    for t in ts:
        t.start()

    samples = []
    t_end = time.monotonic() + a.seconds
    rounds = 0
    try:
        while time.monotonic() < t_end:
            time.sleep(a.every)
            # restore 之前把心跳抄走：restore 会把这个文件也回滚掉。
            txt = victim.run("cat %s; : > %s" % (HB_FILE, HB_FILE), timeout=120)
            samples.extend(common.parse_heartbeat(txt))
            rr = ctx.op(victim.restore(cp0, verify=True), stage="T13", n=n,
                        role="victim", round=rounds)
            expect(ctx, "N=%d 第 %d 次 restore" % (n, rounds + 1), rr.get("ok"), "成功",
                   rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
                   "方案 §4.1 T13；失败现场 %s" % victim.store_dir(cp0))
            expect(ctx, "N=%d 第 %d 次 restore 现场一致" % (n, rounds + 1),
                   rr.get("verified") is not False, "五项全同",
                   rr.get("mismatch") or "全同", "现场定义同 checkpoint_concurrent.py")
            rounds += 1
    finally:
        stop_flag.set()
        for t in ts:
            t.join(timeout=900)

    txt = victim.run("cat %s" % HB_FILE, timeout=120)
    samples.extend(common.parse_heartbeat(txt))
    gaps = common.heartbeat_gaps(samples)
    expect(ctx, "N=%d 心跳有数据" % n, gaps["samples"] > 10 and gaps["max_gap_s"] is not None,
           "> 10 条样本且至少一段可统计",
           "%d 条样本 / %d 段" % (gaps["samples"], gaps["segments"]),
           "T13 前置：心跳没数据就什么也量不到")

    vic = [o for o in ctx.results["ops"]
           if o.get("stage") == "T13" and o.get("n") == n and o.get("role") == "victim"
           and o.get("op") == "restore" and o.get("ok")]
    noise_creates = [o for o in ctx.results["ops"]
                     if o.get("stage") == "T13" and o.get("n") == n and o.get("role") == "noise"
                     and o.get("ok")]
    walls = [o["wall_s"] for o in vic]
    frozen = [(o.get("phases") or {}).get("frozen") for o in vic]
    row = {"n": n, "restores": len(vic), "noise_creates": len(noise_creates),
           "wall_p50_s": common.p50(walls), "wall_max_s": common.pmax(walls),
           "frozen_p50_ms": common.p50(frozen), "frozen_max_ms": common.pmax(frozen),
           "hb_max_gap_s": gaps["max_gap_s"], "hb_gap_p99_ms": gaps["p99_ms"],
           "hb_gaps_gt_100ms": gaps["gt_count"], "hb_segments": gaps["segments"],
           "hb_samples": gaps["samples"], "hb_intervals": gaps["intervals"],
           "hb_gaps": gaps["gaps"]}
    ctx.op({"op": "summary", "stage": "T13", **row})
    note(ctx, "N=%d 曲线" % n, "只记录，不判定",
         "restore %d 次，客户端 p50 %s s，frozen p50 %s ms，"
         "心跳段内间隔 max %s s / p99 %s ms / > 100 ms %d 条（共 %d 个间隔）"
         % (len(vic), common.fmt_s(row["wall_p50_s"]), common.fmt_ms(row["frozen_p50_ms"]),
            common.fmt_s(row["hb_max_gap_s"]), common.fmt_ms(row["hb_gap_p99_ms"]),
            row["hb_gaps_gt_100ms"], row["hb_intervals"]),
         "方案 §4.1 T13 / §4.3 T33")

    for b in boxes:
        b.kill()
        if b in common._ALL_BOXES:
            common._ALL_BOXES.remove(b)
    if errs:
        raise errs[0]
    return row


def run(ctx):
    rows = []
    for n in [int(x) for x in ctx.args.fanout.split(",") if x.strip()]:
        rows.append(run_one(ctx, n))

    log("")
    common.table(["N", "restore 次数", "干扰 create", "客户端 p50/max s", "frozen p50/max ms",
                  "心跳 max s / p99 ms", "心跳 > 100 ms"],
                 [[str(r["n"]), str(r["restores"]), str(r["noise_creates"]),
                   "%s / %s" % (common.fmt_s(r["wall_p50_s"]), common.fmt_s(r["wall_max_s"])),
                   "%s / %s" % (common.fmt_ms(r["frozen_p50_ms"]), common.fmt_ms(r["frozen_max_ms"])),
                   "%s / %s" % (common.fmt_s(r["hb_max_gap_s"]), common.fmt_ms(r["hb_gap_p99_ms"])),
                   "%d / %d" % (r["hb_gaps_gt_100ms"], r["hb_intervals"])] for r in rows])
    ctx.results["summary"]["T13"] = rows
