#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 bench_tiers.py 的原始 JSONL 压成一张「每档 p50 / p99 / 最大 + 达标与否」的表。

达标线照抄手册 28 篇（客户那组粗略指标，未限定改动量）：
**checkpoint ≤ 200 ms、restore ≤ 100 ms**，量的都是**客户端墙钟**。
全量 checkpoint（每遍开头那一次）按 28 篇的口径**单列不判定**。

`bench/analyze.py` 是完整分析（分段、拟合、离群、漂移）；这里只出验收要看的那一张表，
给 950/run.sh 的 SUMMARY.md 用。

用法：python3 bench/compliance.py raw-*.jsonl
"""

import glob
import json
import sys

CK_LIMIT_MS = 200.0
RS_LIMIT_MS = 100.0


def pct(xs, q):
    """最近秩法，不插值；样本少时自然退化成最大值（与 crtest.common.quantile 同口径）。"""
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    return s[int(round(q * (len(s) - 1)))]


def f(x):
    return "-" if x is None else ("%.0f" % x if abs(x) >= 100 else "%.1f" % x)


def mark(v, limit):
    if v is None:
        return "-"
    return "达标" if v <= limit else "**超**"


def main(argv):
    paths = []
    for pat in argv or ["raw-*.jsonl"]:
        paths.extend(sorted(glob.glob(pat)) or [pat])
    tiers, order, fulls = {}, [], []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") == "full":
                    w = (r.get("create") or {}).get("wall_s")
                    fulls.append(None if w is None else w * 1000.0)
                    continue
                if r.get("kind") != "iter":
                    continue
                t = r.get("tier")
                if t not in tiers:
                    tiers[t] = {"ck": [], "rs": [], "mb": r.get("total_mb")}
                    order.append(t)
                cw = (r.get("create") or {}).get("wall_s")
                rw = (r.get("restore") or {}).get("wall_s")
                if cw is not None:
                    tiers[t]["ck"].append(cw * 1000.0)
                if rw is not None:
                    tiers[t]["rs"].append(rw * 1000.0)

    if not tiers:
        print("没有可用的迭代记录（找的文件：%s）" % ", ".join(paths))
        return 2

    print("达标线（手册 28 篇，客户粗略指标，未限定改动量）："
          "checkpoint ≤ %.0f ms、restore ≤ %.0f ms，量的是客户端墙钟。"
          % (CK_LIMIT_MS, RS_LIMIT_MS))
    print("")
    print("| 档 | 名义改动 MB | n | ck p50 | ck p99 | ck max | ck 达标(p50/p99) | "
          "rs p50 | rs p99 | rs max | rs 达标(p50/p99) |")
    print("|" + "---|" * 11)
    over = []
    for t in order:
        d = tiers[t]
        ck50, ck99, ckmx = pct(d["ck"], 0.5), pct(d["ck"], 0.99), max(d["ck"]) if d["ck"] else None
        rs50, rs99, rsmx = pct(d["rs"], 0.5), pct(d["rs"], 0.99), max(d["rs"]) if d["rs"] else None
        print("| %s | %s | %d | %s | %s | %s | %s / %s | %s | %s | %s | %s / %s |"
              % (t, d["mb"], len(d["ck"]), f(ck50), f(ck99), f(ckmx),
                 mark(ck50, CK_LIMIT_MS), mark(ck99, CK_LIMIT_MS),
                 f(rs50), f(rs99), f(rsmx),
                 mark(rs50, RS_LIMIT_MS), mark(rs99, RS_LIMIT_MS)))
        if ck50 is not None and ck50 > CK_LIMIT_MS:
            over.append("%s checkpoint p50 %.0f ms" % (t, ck50))
        if rs50 is not None and rs50 > RS_LIMIT_MS:
            over.append("%s restore p50 %.0f ms" % (t, rs50))

    ok = [x for x in fulls if x is not None]
    if ok:
        print("")
        print("全量 checkpoint（每遍开头那一次，**单列不判定**）：n = %d，"
              "p50 %s ms，最大 %s ms" % (len(ok), f(pct(ok, 0.5)), f(max(ok))))
    print("")
    if over:
        print("**按 p50 超线的档**：%s" % "；".join(over))
    else:
        print("**所有档的 p50 都在达标线内。**")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
