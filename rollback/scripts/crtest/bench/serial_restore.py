#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单沙箱串行 restore 的长尾：一个沙箱、一个调用方、同一个 checkpoint 连回 N 次。

回答的问题只有一个：**在没有任何并发的情况下，restore 的墙钟长尾长什么样。**
并发那一面在 `acceptance/checkpoint_concurrent.py`，档位那一面在 `bench/bench_tiers.py`；
这里刻意把两者都去掉，让 p99 和最大值只归因于服务端自己的长尾（09-20 查过的
conntrack 删表、09-21 查过的空闲后定时器）而不是排队。

来历：920B 上 tmp/conntrack-20260920/ctloop.py（一次性调查脚本），2026-09-22 参数化
后迁进本仓库，加了分位数汇总、超时保护与 JSON 产物。

用法：
    python3 bench/serial_restore.py -n 100 --outdir /tmp/perf
    python3 bench/serial_restore.py -n 100 --mem-mb 16 --file-mb 4 --env-file /opt/e2b-infra/.env
"""

import argparse
import json
import os
import sys
import time

# crtest 包在本文件的上一级（rollback/scripts/crtest/crtest），按位置定位。
CRTEST_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CRTEST_PARENT not in sys.path:
    sys.path.insert(0, CRTEST_PARENT)

from crtest import common                                    # noqa: E402
from crtest.common import log                                # noqa: E402

# 手册 28 篇抄客户的那组粗略指标（未限定改动量）。
RS_LIMIT_MS = 100.0


def run(ctx):
    a = ctx.args
    outdir = a.outdir or os.getcwd()
    os.makedirs(outdir, exist_ok=True)
    jsonl_path = os.path.join(outdir, "serial-restore-%s.jsonl" % common.now_tag())
    fh = open(jsonl_path, "a", buffering=1)
    ctx.results["meta"]["jsonl"] = jsonl_path
    log("原始数据：%s" % jsonl_path)

    sbx = common.sandbox_create(a.template, private=not a.public, timeout=7200)
    box = common.Box(sbx, ctx.store, "sr")
    common._ALL_BOXES.append(box)
    log("沙箱 %s" % box.id)
    box.setup(warm_mem=a.mem_mb * 2, warm_file=a.file_mb * 2)
    box.dirty("g0", mem_mb=a.mem_mb, file_mb=a.file_mb)

    crec = box.create("g0")
    if not crec.get("ok"):
        raise common.Failed("基准 checkpoint", "成功", str(crec.get("error")), "开跑前")
    cp0 = crec["id"]
    log("基准 checkpoint %s（mem_mode=%s）" % (cp0, crec.get("mem_mode")))
    ctx.results["meta"]["checkpoint"] = cp0
    ctx.stage("SERIAL-RESTORE", n=a.n)

    deadline = time.monotonic() + a.deadline_min * 60.0
    walls, frozens, totals = [], [], []
    bad = 0
    for i in range(1, a.n + 1):
        if time.monotonic() > deadline:
            log("!! 到总时限，停在第 %d 次（计划 %d 次）" % (i, a.n))
            ctx.results["meta"]["stopped_early_at"] = i
            break
        ct_before = common.conntrack_count()
        rr = box.restore(cp0, verify=(i % a.verify_every == 0))
        ph = rr.get("phases") or {}
        rec = {"i": i, "t": time.time(), "ok": rr.get("ok"), "wall_s": rr.get("wall_s"),
               "verified": rr.get("verified"), "mismatch": rr.get("mismatch"),
               "host_ct_before": ct_before, "host_ct_after": common.conntrack_count(),
               "netns": common.netns_count(), "phases": ph}
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        ctx.results["ops"].append(rec)
        if not rr.get("ok"):
            bad += 1
            log("  !! 第 %d 次 restore 失败：%s" % (i, rr.get("error")))
        if rr.get("verified") is False:
            bad += 1
            log("  !! 第 %d 次 restore 后现场不一致：%s" % (i, rr.get("mismatch")))
        if rr.get("wall_s") is not None:
            walls.append(rr["wall_s"] * 1000.0)
        frozens.append(ph.get("frozen"))
        totals.append(ph.get("total"))
        if i % 20 == 0 or i == a.n:
            log("  %3d/%d  墙钟 p50 %s ms  p99 %s ms  最大 %s ms"
                % (i, a.n, common.fmt_ms(common.p50(walls)),
                   common.fmt_ms(common.quantile(walls, 0.99)),
                   common.fmt_ms(common.pmax(walls))))

    fh.close()
    box.delete(cp0)
    box.kill()
    common.forget(box)

    s = {"n": len(walls),
         "wall_p50_ms": common.p50(walls), "wall_p99_ms": common.quantile(walls, 0.99),
         "wall_max_ms": common.pmax(walls), "wall_min_ms": min(walls) if walls else None,
         "frozen_p50_ms": common.p50(frozens), "frozen_p99_ms": common.quantile(frozens, 0.99),
         "server_total_p50_ms": common.p50(totals),
         "server_total_p99_ms": common.quantile(totals, 0.99),
         "bad": bad, "limit_ms": RS_LIMIT_MS}
    s["p50_within_limit"] = (s["wall_p50_ms"] is not None and s["wall_p50_ms"] <= RS_LIMIT_MS)
    s["p99_within_limit"] = (s["wall_p99_ms"] is not None and s["wall_p99_ms"] <= RS_LIMIT_MS)
    ctx.results["summary"]["SERIAL-RESTORE"] = s

    log("")
    log("== 单沙箱串行 restore ×%d ==" % s["n"])
    log("  客户端墙钟  p50 %s ms   p99 %s ms   最大 %s ms   最小 %s ms"
        % (common.fmt_ms(s["wall_p50_ms"]), common.fmt_ms(s["wall_p99_ms"]),
           common.fmt_ms(s["wall_max_ms"]), common.fmt_ms(s["wall_min_ms"])))
    log("  服务端分段  frozen p50 %s ms  p99 %s ms | total p50 %s ms  p99 %s ms"
        % (common.fmt_ms(s["frozen_p50_ms"]), common.fmt_ms(s["frozen_p99_ms"]),
           common.fmt_ms(s["server_total_p50_ms"]), common.fmt_ms(s["server_total_p99_ms"])))
    log("  对照 %s ms 达标线：p50 %s，p99 %s"
        % (common.fmt_ms(RS_LIMIT_MS),
           "达标" if s["p50_within_limit"] else "未达标",
           "达标" if s["p99_within_limit"] else "未达标（长尾，见手册 28 篇 §4.8）"))
    # 失败与现场不一致要让退出码非 0；长尾超线只记录不判失败（口径同手册 28 篇）。
    common.expect(ctx, "restore 全部成功且抽验一致", bad == 0, "0 次异常", "%d 次异常" % bad,
                  "单沙箱串行 ×%d" % s["n"])


def main():
    ap = argparse.ArgumentParser(description="单沙箱串行 restore 长尾")
    common.add_common_args(ap)
    ap.add_argument("-n", type=int, default=100, help="restore 次数（默认 100）")
    ap.add_argument("--mem-mb", type=int, default=16, help="基准现场的内存脏量 MB（默认 16）")
    ap.add_argument("--file-mb", type=int, default=4, help="基准现场的文件量 MB（默认 4）")
    ap.add_argument("--verify-every", type=int, default=10,
                    help="每几次做一次现场逐项校验（默认 10；1 = 每次都验，会拖慢）")
    ap.add_argument("--deadline-min", type=float, default=20.0, help="总时限（分钟）")
    ap.add_argument("--outdir", default=None, help="产物落点（默认当前目录）")
    args = ap.parse_args()
    if not args.out:
        args.out = os.path.join(args.outdir or os.getcwd(),
                                "serial-restore-%s.json" % common.now_tag())
    shim = type("M", (), {"run": staticmethod(run)})
    return common.run_case("SERIAL-RESTORE", shim, args)


if __name__ == "__main__":
    sys.exit(main())
