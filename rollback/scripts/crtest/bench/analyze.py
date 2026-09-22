#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 bench_tiers.py（2026-09-21 版）的 JSONL 汇成报告里的表与拟合式。

用法：python analyze.py raw-*.jsonl
"""
import glob
import json
import statistics
import sys

MB = 1048576.0

MIX = ["mix0", "mix4", "mix8", "mix16", "mix32", "mix64", "mix128", "mix192", "mix256", "mix512"]
PURE_MEM = ["mem4", "mem16", "mem48", "mem128"]
PURE_FILE = ["file4", "file16", "file64"]
READ = ["read192"]
ORDER = MIX + PURE_MEM + PURE_FILE + READ
LABEL = {"mix0": "混合 0", "mix4": "混合 4", "mix8": "混合 8", "mix16": "混合 16",
         "mix32": "混合 32", "mix64": "混合 64", "mix128": "混合 128",
         "mix192": "混合 192", "mix256": "混合 256", "mix512": "混合 512（极限）",
         "mem4": "纯内存 4", "mem16": "纯内存 16", "mem48": "纯内存 48", "mem128": "纯内存 128",
         "file4": "纯文件 4", "file16": "纯文件 16", "file64": "纯文件 64",
         "read192": "只读 192"}

CK_LIMIT_MS = 200.0
RS_LIMIT_MS = 100.0


def pct(xs, q):
    """分位数：排序后取 ceil(q*n)-1 号（保守，不插值）。"""
    if not xs:
        return None
    s = sorted(xs)
    # 最近秩法：rank = ceil(q*n)，不插值；n 不够时自然退化成最大值
    rank = max(1, int(-(-q * len(s) // 1)))
    return s[min(rank, len(s)) - 1]


def p50(xs):
    return statistics.median(xs) if xs else None


def f(x, n=1):
    return "-" if x is None else ("%.*f" % (n, x))


def load(paths):
    rows, fulls = [], []
    for p in paths:
        for line in open(p):
            r = json.loads(line)
            (fulls if r.get("kind") == "full" else rows).append(r)
    return [r for r in rows if r.get("kind") == "iter"], fulls


def val(r, side, key):
    return ((r[side].get("phases") or {}).get(key))


def wall_ms(r, side):
    return r[side].get("wall_s", 0.0) * 1000.0


def fit(pairs):
    n = len(pairs)
    if n < 3:
        return None
    sx = sum(x for x, _ in pairs)
    sy = sum(y for _, y in pairs)
    sxx = sum(x * x for x, _ in pairs)
    sxy = sum(x * y for x, y in pairs)
    d = n * sxx - sx * sx
    if d == 0:
        return None
    b = (n * sxy - sx * sy) / d
    a = (sy - b * sx) / n
    ybar = sy / n
    sst = sum((y - ybar) ** 2 for _, y in pairs)
    sse = sum((y - (a + b * x)) ** 2 for x, y in pairs)
    return a, b, (1 - sse / sst if sst else None)


def main():
    paths = sys.argv[1:] or sorted(glob.glob("raw-*.jsonl"))
    rows, fulls = load(paths)
    by = {t: [r for r in rows if r["tier"] == t] for t in ORDER}
    passes = sorted({r["pass"] for r in rows})
    print("# 数据来源 %s   遍数 %s   迭代记录 %d 条\n" % (paths, passes, len(rows)))

    # ---------------- 异常
    bad = [r for r in rows if r.get("verify_bad") or not r["create"].get("ok")
           or not r["restore"].get("ok") or not (r.get("delete") or {}).get("ok")]
    print("## 异常记录：%d 条" % len(bad))
    for r in bad:
        print("  遍%s %s #%s : verify_bad=%s ck_ok=%s rs_ok=%s del_ok=%s"
              % (r["pass"], r["tier"], r["idx"], r.get("verify_bad"),
                 r["create"].get("ok"), r["restore"].get("ok"),
                 (r.get("delete") or {}).get("ok")))
    modes = {r["create"].get("mem_mode") for r in rows}
    print("  mem_mode 取值：%s（%d 次迭代）" % (modes, len(rows)))

    # ---------------- 全量
    print("\n## 首个全量 checkpoint（每遍一次）")
    print("| 遍 | mem_mode | frozen ms | snapshot ms | seal ms | total ms | 墙钟 s | 产物 MB |")
    print("|---|---|---|---|---|---|---|---|")
    for r in fulls:
        ph = r["create"].get("phases") or {}
        print("| %s | %s | %s | %s | %s | %s | %s | %s |"
              % (r["pass"], r["create"].get("mem_mode"), f(ph.get("frozen")),
                 f(ph.get("snapshot")), f(ph.get("seal")), f(ph.get("total")),
                 f(r["create"].get("wall_s"), 2),
                 f((r["artifact"]["mem_bytes"] or 0) / MB, 2)))

    # ---------------- 主表
    def tab(tiers, title):
        print("\n## %s" % title)
        print("| 档 | n | ck 墙钟 p50 | p90 | p99 | max | ck frozen p50 | p99 | "
              "rs 墙钟 p50 | p90 | p99 | max | rs frozen p50 | p99 | "
              "内存差分 MB | 盘层 MB | 达标(p50) | 达标(p99) |")
        print("|" + "---|" * 18)
        for t in tiers:
            rs = by.get(t) or []
            if not rs:
                continue
            cw = [wall_ms(r, "create") for r in rs]
            cf = [val(r, "create", "frozen") for r in rs if val(r, "create", "frozen") is not None]
            rw = [wall_ms(r, "restore") for r in rs]
            rf = [val(r, "restore", "frozen") for r in rs if val(r, "restore", "frozen") is not None]
            mem = [(r["artifact"]["mem_bytes"] or 0) / MB for r in rs]
            dsk = [(r["artifact"]["disk_bytes"] or 0) / MB for r in rs]
            n = len(rs)
            note99 = "" if n >= 100 else ""
            ok50 = "✓/✓" if (p50(cw) <= CK_LIMIT_MS and p50(rw) <= RS_LIMIT_MS) else \
                   ("%s/%s" % ("✓" if p50(cw) <= CK_LIMIT_MS else "✗",
                               "✓" if p50(rw) <= RS_LIMIT_MS else "✗"))
            ok99 = "%s/%s" % ("✓" if pct(cw, 0.99) <= CK_LIMIT_MS else "✗",
                              "✓" if pct(rw, 0.99) <= RS_LIMIT_MS else "✗")
            print("| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                  % (LABEL[t], n, f(p50(cw)), f(pct(cw, 0.90)), f(pct(cw, 0.99)), f(max(cw)),
                     f(p50(cf)), f(pct(cf, 0.99)),
                     f(p50(rw)), f(pct(rw, 0.90)), f(pct(rw, 0.99)), f(max(rw)),
                     f(p50(rf)), f(pct(rf, 0.99)),
                     f(p50(mem), 2), f(p50(dsk), 2), ok50, ok99))

    tab(MIX, "主表：混合档（内存:文件 = 3:1；单位 ms，产物为中位数）")
    tab(PURE_MEM + PURE_FILE + READ, "附表：纯内存 / 纯文件 / 只读档")

    # ---------------- 分段
    print("\n## checkpoint 分段（p50 ms）")
    print("| 档 | n | frozen | snapshot | seal | 其余(total-snapshot-seal) | total | 墙钟-total |")
    print("|" + "---|" * 8)
    for t in ORDER:
        rs = by.get(t) or []
        if not rs:
            continue
        g = lambda k: p50([val(r, "create", k) for r in rs if val(r, "create", k) is not None])
        sn, se, to = g("snapshot"), g("seal"), g("total")
        rest = None if None in (sn, se, to) else to - sn - se
        wm = p50([wall_ms(r, "create") - (val(r, "create", "total") or 0) for r in rs])
        print("| %s | %d | %s | %s | %s | %s | %s | %s |"
              % (LABEL[t], len(rs), f(g("frozen")), f(sn), f(se), f(rest), f(to), f(wm)))

    print("\n## restore 分段（p50 ms；assemble_view_pre / wait_envd 在冻结窗口之外）")
    keys = ["frozen", "materialize", "fc_memory", "fc_bitmap", "fc_rollback", "reset_view",
            "conntrack", "conntrack_bg", "assemble_view_pre", "wait_envd", "total"]
    print("| 档 | n | " + " | ".join(keys) + " | 墙钟 |")
    print("|" + "---|" * (len(keys) + 3))
    for t in ORDER:
        rs = by.get(t) or []
        if not rs:
            continue
        cells = []
        for k in keys:
            vs = [val(r, "restore", k) for r in rs if val(r, "restore", k) is not None]
            cells.append(f(p50(vs)))
        print("| %s | %d | %s | %s |" % (LABEL[t], len(rs), " | ".join(cells),
                                         f(p50([wall_ms(r, "restore") for r in rs]))))

    print("\n## fc_memory 专项（ms）")
    print("| 档 | n | p50 | p90 | p99 | max | min |")
    print("|" + "---|" * 7)
    for t in ORDER:
        rs = by.get(t) or []
        vs = [val(r, "restore", "fc_memory") for r in rs
              if val(r, "restore", "fc_memory") is not None]
        if not vs:
            continue
        print("| %s | %d | %s | %s | %s | %s | %s |"
              % (LABEL[t], len(vs), f(p50(vs)), f(pct(vs, 0.9)), f(pct(vs, 0.99)),
                 f(max(vs)), f(min(vs))))

    # ---------------- 拟合
    print("\n## 线性拟合（最小二乘，单次迭代为样本）")
    mix_le256 = [t for t in MIX if t != "mix512"]

    def pairs_of(tiers, xf, yf):
        out = []
        for t in tiers:
            for r in by.get(t) or []:
                y = yf(r)
                if y is not None:
                    out.append((xf(r), y))
        return out

    nom = lambda r: r["mem_mb"] + r["file_mb"]
    act = lambda r: ((r["artifact"]["mem_bytes"] or 0) + (r["artifact"]["disk_bytes"] or 0)) / MB

    fits = {}
    defs = [
        ("混合 ck 墙钟 ~ 名义总改动(≤256)", mix_le256, nom, lambda r: wall_ms(r, "create")),
        ("混合 ck frozen ~ 名义总改动(≤256)", mix_le256, nom, lambda r: val(r, "create", "frozen")),
        ("混合 rs 墙钟 ~ 名义总改动(≤256)", mix_le256, nom, lambda r: wall_ms(r, "restore")),
        ("混合 rs frozen ~ 名义总改动(≤256)", mix_le256, nom, lambda r: val(r, "restore", "frozen")),
        ("混合 ck 墙钟 ~ 实际产物 MB(≤256)", mix_le256, act, lambda r: wall_ms(r, "create")),
        ("混合 rs 墙钟 ~ 实际产物 MB(≤256)", mix_le256, act, lambda r: wall_ms(r, "restore")),
        ("混合 ck 墙钟 ~ 名义总改动(含512)", MIX, nom, lambda r: wall_ms(r, "create")),
        ("混合 rs 墙钟 ~ 名义总改动(含512)", MIX, nom, lambda r: wall_ms(r, "restore")),
        ("纯内存 ck frozen ~ 内存 MB", ["mix0"] + PURE_MEM, lambda r: r["mem_mb"],
         lambda r: val(r, "create", "frozen")),
        ("纯文件 ck frozen ~ 文件 MB", ["mix0"] + PURE_FILE, lambda r: r["file_mb"],
         lambda r: val(r, "create", "frozen")),
        ("纯内存 rs frozen ~ 内存 MB", ["mix0"] + PURE_MEM, lambda r: r["mem_mb"],
         lambda r: val(r, "restore", "frozen")),
        ("纯文件 rs frozen ~ 文件 MB", ["mix0"] + PURE_FILE, lambda r: r["file_mb"],
         lambda r: val(r, "restore", "frozen")),
        ("混合 内存差分 MB ~ 名义总改动(≤256)", mix_le256, nom,
         lambda r: (r["artifact"]["mem_bytes"] or 0) / MB),
        ("混合 盘层 MB ~ 名义总改动(≤256)", mix_le256, nom,
         lambda r: (r["artifact"]["disk_bytes"] or 0) / MB),
    ]
    for name, ts, xf, yf in defs:
        res = fit(pairs_of(ts, xf, yf))
        if res:
            a, b, r2 = res
            fits[name] = res
            print("- %s：y = %.2f + %.4f·x   （R² = %s，n = %d）"
                  % (name, a, b, f(r2, 4), len(pairs_of(ts, xf, yf))))

    # 512 外推误差
    print("\n### 512 档相对 ≤256 拟合的外推")
    for key, side in (("混合 ck 墙钟 ~ 名义总改动(≤256)", "create"),
                      ("混合 rs 墙钟 ~ 名义总改动(≤256)", "restore")):
        if key not in fits:
            continue
        a, b, _ = fits[key]
        pred = a + b * 512
        obs = p50([wall_ms(r, side) for r in by.get("mix512") or []])
        if obs:
            print("- %s：预测 %.1f ms，实测 p50 %.1f ms，误差 %+.1f%%"
                  % (key, pred, obs, (obs - pred) / pred * 100))

    print("\n### 反推达标改动量")
    for key, lim in (("混合 ck 墙钟 ~ 名义总改动(≤256)", CK_LIMIT_MS),
                     ("混合 rs 墙钟 ~ 名义总改动(≤256)", RS_LIMIT_MS)):
        if key in fits:
            a, b, _ = fits[key]
            print("- %s 到 %.0f ms：x = %.1f MB" % (key, lim, (lim - a) / b))

    # ---------------- 实际 vs 名义
    print("\n## 实际产物 vs 名义改动")
    print("| 档 | 名义内存 | 名义文件 | 内存差分 MB | 减 0 档基底 | 倍率 | 盘层 MB |")
    print("|" + "---|" * 7)
    base = p50([(r["artifact"]["mem_bytes"] or 0) / MB for r in by.get("mix0") or []]) or 0
    for t in ORDER:
        rs = by.get(t) or []
        if not rs:
            continue
        m = p50([(r["artifact"]["mem_bytes"] or 0) / MB for r in rs])
        d = p50([(r["artifact"]["disk_bytes"] or 0) / MB for r in rs])
        nm = rs[0]["mem_mb"] + rs[0]["file_mb"]
        print("| %s | %d | %d | %s | %s | %s | %s |"
              % (LABEL[t], rs[0]["mem_mb"], rs[0]["file_mb"], f(m, 2), f(m - base, 2),
                 ("%.2f×" % ((m - base) / nm)) if nm else "-", f(d, 3)))
    print("\n0 档基底内存差分 = %s MB" % f(base, 2))

    # ---------------- 缓存 / 漂移 / 离群
    print("\n## 缓存与漂移")
    dr = [val(r, "restore", "materialize_disk_read_mb") for r in rows]
    fr = [val(r, "restore", "fc_rollback_disk_read_mb") for r in rows]
    print("- materialize_disk_read_mb 非零 %d / %d；fc_rollback_disk_read_mb 非零 %d / %d"
          % (sum(1 for x in dr if x), len(dr), sum(1 for x in fr if x), len(fr)))
    print("\n| 档 | " + " | ".join("遍%d ck frozen p50" % p for p in passes) + " | 末/首 |")
    print("|" + "---|" * (len(passes) + 2))
    for t in ORDER:
        vs = []
        for p in passes:
            x = [val(r, "create", "frozen") for r in by.get(t) or []
                 if r["pass"] == p and val(r, "create", "frozen") is not None]
            vs.append(p50(x))
        ratio = "-" if not (vs[0] and vs[-1]) else "%.2f×" % (vs[-1] / vs[0])
        print("| %s | %s | %s |" % (LABEL[t], " | ".join(f(v) for v in vs), ratio))

    print("\n## 离群（单次 frozen > 该档中位数 2 倍）")
    hit = 0
    for t in ORDER:
        for side in ("create", "restore"):
            vs = [(r, val(r, side, "frozen")) for r in by.get(t) or []]
            vs = [(r, v) for r, v in vs if v is not None]
            if len(vs) < 5:
                continue
            med = p50([v for _, v in vs])
            for r, v in vs:
                if med and v > 2.0 * med:
                    hit += 1
                    print("- 遍%s %s #%s %s frozen = %.1f ms（中位 %.1f，%.1f×）"
                          % (r["pass"], t, r["idx"], side, v, med, v / med))
    if not hit:
        print("- 零命中")


if __name__ == "__main__":
    main()
