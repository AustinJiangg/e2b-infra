"""18 章 950 性能分档与 HDBSS 实测的数据图。

数字全部抄自 rollback/docs/18-950-performance.md 的表格和正文，集中写在下面的常量里，
每组注明出处小节。以后重测或补数据时，只改这一块再重跑本脚本。
颜色：950 = blue（同一平台内用深浅 / 实线虚线区分 p50、p99 或档位）。
"""
from common import *  # noqa: F401,F403
from chart_kit import col, nice_ticks, GRID, AXIS
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w
from draw_rollback_20 import gbars

# ================================================================ 数据（只改这里）

# 图 18-1。来源：rollback/docs/18-950-performance.md §4 表（perf-full analyze.log），单位 ms
# 每项：(档名, n, ck 墙钟 p50, ck p99/max, rs 墙钟 p50, rs p99/max)
TIERS_MIXED = [
    ("0", 100, 15.5, 20.0, 19.0, 28.6),
    ("4", 100, 16.6, 21.5, 20.3, 29.7),
    ("8", 100, 17.7, 25.4, 21.4, 30.3),
    ("16", 100, 20.0, 27.2, 23.6, 33.3),
    ("32", 100, 25.2, 36.3, 28.4, 37.9),
    ("64", 100, 37.3, 49.7, 37.3, 50.3),
    ("128", 60, 66.2, 80.1, 54.5, 72.6),
    ("192", 60, 91.7, 113.8, 76.5, 109.0),
    ("256", 60, 119.0, 143.2, 99.8, 123.2),
    ("512", 30, 219.5, 253.1, 189.6, 229.0),
]
TIERS_OTHER = [
    ("内存 4", 30, 17.0, 20.7, 22.4, 32.0),
    ("内存 16", 30, 18.1, 25.0, 25.8, 41.5),
    ("内存 48", 30, 21.6, 28.5, 33.8, 42.4),
    ("内存 128", 30, 25.9, 36.5, 52.3, 70.9),
    ("文件 4", 30, 18.9, 25.7, 20.6, 31.3),
    ("文件 16", 30, 32.6, 41.5, 26.1, 33.1),
    ("文件 64", 30, 98.9, 119.7, 44.0, 52.3),
    ("只读 192", 20, 17.0, 22.3, 23.9, 29.0),
]
# 客户参考线。来源：§1
REF_CK, REF_RS = 200, 100
# 条件。来源：§2、§3
COND_18_1 = ("950（Kunpeng 950 7592C），2026-11-18 test950 extended 的 perf-full，后端自报 hdbss，宿主没有配置 UB；"
             "1040 次增量迭代，每档 n = 20～100，p99 为最近秩法")

# 图 18-2。来源：§5.1 分段表，混合 128 / 256 / 512 MiB 各分段 p50，单位 ms
SEG_TIERS = ["混合 128", "混合 256", "混合 512"]
SEG_CK = [  # (分段名, [128, 256, 512])
    ("snapshot", [56.6, 103.9, 197.8]),
    ("seal", [6.6, 11.2, 18.1]),
]
SEG_RS = [
    ("materialize", [29.5, 54.0, 106.0]),
    ("fc_memory", [13.7, 29.7, 63.7]),
    ("reset_view", [2.0, 3.0, 6.0]),
    ("wait_envd", [1.4, 1.4, 1.5]),
    ("fc_bitmap", [1.5, 1.5, 1.6]),
]
# 三档相同、日志只给约值的分段，画成一根条
SEG_RS_RANGE = [
    ("assemble_view_pre", (0.2, 0.2), "约 0.2"),
]
COND_18_2 = "950，2026-11-18 extended 的 perf-full，analyze.log 各分段中位数，每档 n = 60 / 60 / 30（§4 表）"

# 图 18-3。来源：§10.6 表（2026-11-17 hdbss-table.md，完整 21 组合），单写者，n = 3 中位数，单位 ms
# 0 档为基底，seq 与 rand 共用这个点
HD_X = ["0", "16", "64", "256", "512", "1024"]
HD_CK_SEQ = [14.7, 16.6, 21.5, 35.3, 57.2, 99.1]
HD_CK_RAND = [14.7, 29.9, 64.5, 170.9, 254.8, 100.2]
HD_RS_SEQ = [7.8, 17.8, 28.7, 72.8, 131.6, 253.1]
HD_RS_RAND = [7.8, 44.4, 116.2, 364.0, 507.4, 310.0]
# 条件。来源：§10.6
COND_18_3 = ("950（Kunpeng 950 7592C），2026-11-17 standard 的 HDBSS 步骤（20261117-165758-hdbss），默认 order 1；"
             "arena 1024 MiB，每页写 1 字节，每组合 n = 3 取中位数")

# ================================================================ 画图

# 同一平台内区分档位用的 blue 深浅（运行时加进色表，不改 kit）
RAMPS["blue_l"] = ("#E6F1FB", "#6FA6E0", "#185FA5", "#185FA5")
RAMPS["blue_m"] = ("#CFE3F7", "#378ADD", "#0C447C", "#185FA5")
RAMPS["blue_d"] = ("#7FB0E6", "#185FA5", "#0C447C", "#0C447C")


def tierpanel(d, xlabels, series, ylim, ticks, ref, groups=None, label_w=150, h=300, right_pad=40,
              ytitle="ms", xtitle=None):
    """按档位等距排开的折线面板，下方附逐档数值行。
    series: [(行名, 色阶, [值...], 'line'|'dash')]；groups: [(起, 止, 组名, 是否连线)]，组间画竖向分隔。
    ref: (值, 说明) 横向参考线。"""
    x0 = d.M + label_w
    x1 = d.W - d.M - right_pad
    top = d.y + 30
    bot = top + h
    lo, hi = ylim
    sy = lambda v: bot - (v - lo) / (hi - lo) * (bot - top)
    n = len(xlabels)
    groups = groups or [(0, n, None, True)]
    slots = n + len(groups) - 1          # 组与组之间空一格
    pos, k = [], 0
    for gi, (a, b, _, _) in enumerate(groups):
        for i in range(a, b):
            pos.append(k)
            k += 1
        k += 1
    sx = lambda i: x0 + (x1 - x0) * (pos[i] + 0.5) / slots
    for t in ticks:
        d.line(x0, sy(t), x1, sy(t), GRID)
        d.text(x0 - 10, sy(t) + 4.5, f"{t:g}", 12.5, MUTED, 400, "end")
    d.text(x0 - 10, top - 14, ytitle, 12.5, MUTED, 400, "end")
    d.line(x0, bot, x1, bot, AXIS, 1.2)
    for gi in range(1, len(groups)):
        a = groups[gi][0]
        xm = (sx(a - 1) + sx(a)) / 2
        d.line(xm, top - 6, xm, bot + 4, AXIS, 1, "3 4")
    if ref:
        rv, rlab = ref
        d.line(x0, sy(rv), x1, sy(rv), col("coral"), 1.4, "6 4")
        d.text(x0 + 8, sy(rv) - 8, rlab, 13, RAMPS["coral"][3], 600)
    for i, lab in enumerate(xlabels):
        d.text(sx(i), bot + 24, lab, 14, INK, 600, "middle")
    for a, b, gname, _ in groups:
        if gname:
            d.text((sx(a) + sx(b - 1)) / 2, top - 14, gname, 13, MUTED, 400, "middle")
    for name, ramp, vs, style in series:
        c = col(ramp)
        for a, b, _, connect in groups:
            pts = [(sx(i), sy(vs[i])) for i in range(a, b) if vs[i] is not None]
            if connect and len(pts) > 1:
                d.poly(pts, c, 2.6, "7 5" if style == "dash" else None)
        for i, v in enumerate(vs):
            if v is None:
                continue
            if style == "dash":
                d.circle(sx(i), sy(v), 5.5, "#FFFFFF", c, 2)
            else:
                d.circle(sx(i), sy(v), 6, c, "#FFFFFF", 1.6)
    y = bot + 34
    if xtitle:
        d.text(x1, y + 4, xtitle, 12.5, MUTED, 400, "end")
        y += 14
    # 逐档数值行
    for r, (name, ramp, vs, style) in enumerate(series):
        cy = y + 14 + r * 26
        if r % 2 == 0:
            d.rect(d.M, cy - 13, d.W - 2 * d.M, 26, "#FAFAF7", "#FAFAF7", sw=0, rx=0)
        c = col(ramp)
        lx = d.M + 4
        d.line(lx, cy, lx + 24, cy, c, 2.4, "6 4" if style == "dash" else None)
        d.text(lx + 32, cy + 5, name, 13.5, INK, 600)
        for i, v in enumerate(vs):
            if v is None:
                continue
            over = ref and v > ref[0]
            d.text(sx(i), cy + 5, f"{v:.1f}", 13.5, RAMPS["coral"][3] if over else SUB, 600 if over else 400, "middle")
    d.y = y + 14 + len(series) * 26


# ---------------------------------------------------------------- 图 18-1
def fig_tiers():
    d = Chart(width=1600)
    d.header("950 十八档：增量 checkpoint 与 restore 随改动量的墙钟 p50 / p99",
             COND_18_1 + "。右侧纯内存、纯文件、只读档单独成组、不连线。")
    d.legend_row([("p50（客户端墙钟中位数）", "p950", "line"), ("p99 / max", "p950", "dash"),
                  ("客户参考线", "coral", "dash")])
    rows = TIERS_MIXED + TIERS_OTHER
    xl = [r[0] for r in rows]
    groups = [(0, len(TIERS_MIXED), "混合负载：名义改动 MiB，档位等距排列，非线性刻度", True),
              (len(TIERS_MIXED), len(rows), "纯内存 / 纯文件 / 只读（MiB）", False)]
    ticks = [0, 50, 100, 150, 200, 250]

    d.section(1, "增量 checkpoint", note=f"参考线 ≤{REF_CK} ms；珊瑚色数字为超线值")
    tierpanel(d, xl, [("ck p50", "p950", [r[2] for r in rows], "line"),
                      ("ck p99 / max", "p950", [r[3] for r in rows], "dash")],
              (0, 275), ticks, (REF_CK, f"参考线 {REF_CK} ms"), groups)

    d.section(2, "restore", note=f"参考线 ≤{REF_RS} ms；珊瑚色数字为超线值")
    tierpanel(d, xl, [("rs p50", "p950", [r[4] for r in rows], "line"),
                      ("rs p99 / max", "p950", [r[5] for r in rows], "dash")],
              (0, 275), ticks, (REF_RS, f"参考线 {REF_RS} ms"), groups)
    nlist = " / ".join(str(r[1]) for r in rows)
    d.footer(f"注：各档 n 依次为 {nlist}。p50 抄 analyze.log 中位数，p99 为最近秩法，n ≤ 100 时 p99 接近或就是本轮最大值，"
             "不代表长期尾延迟。三遍首次全量单列在正文，不参与增量判定。")
    d.save(out("rollback", "18-1-950分档墙钟"))


# ---------------------------------------------------------------- 图 18-2
def fig_segments():
    d = Chart(width=1600)
    d.header("950 混合 128 / 256 / 512 MiB：checkpoint 与 restore 各分段的 p50",
             COND_18_2 + "。每个分段三根条分别是三档；单位 ms。")
    tone = ["blue_l", "blue_m", "blue_d"]
    d.legend_row([(t, r, "bar") for t, r in zip(SEG_TIERS, tone)] + [("三档相同、只给约值的分段", "gray", "bar")])
    fmt_ms = lambda t: f"{t:g} ms"

    d.section(1, "checkpoint 分段", note="snapshot 是内存快照段，seal 是封层段")
    gbars(d, [(name, None, [(v, r, f"{v:g}", None) for v, r in zip(vals, tone)]) for name, vals in SEG_CK],
          ("lin", 220), ticks=[0, 50, 100, 150, 200], tick_fmt=fmt_ms, note_w=60, label_w=210, bar_h=22, inner=5)

    d.section(2, "restore 分段", note="fc_memory 包含在 fc_rollback 中")
    groups = [(name, None, [(v, r, f"{v:g}", None) for v, r in zip(vals, tone)]) for name, vals in SEG_RS]
    groups += [(name, "三档相同", [(v, "gray", lab, None)]) for name, v, lab in SEG_RS_RANGE]
    gbars(d, groups, ("lin", 220), ticks=[0, 50, 100, 150, 200], tick_fmt=fmt_ms, note_w=60, label_w=210,
          bar_h=22, inner=5)
    d.footer("注：后台清扫可能与关键路径重叠，各列中位数也未必出自同一次操作，所以各分段不能相加成总耗时，"
             "这里按分段并排而不画成堆叠条。这是本轮的分段观察，不作平台间性能差异的因果证明。")
    d.save(out("rollback", "18-2-950分段耗时"))


# ---------------------------------------------------------------- 图 18-3
def fig_hdbss():
    d = Chart(width=1600)
    d.header("950 HDBSS 写密集基准（2026-11-17 完整组合）：连续页与随机散页的 checkpoint、restore 墙钟",
             COND_18_3 + "。只画单写者，双写者见正文表。")
    d.legend_row([("seq：写 arena 开头连续 N 页", "p950", "line"),
                  ("rand：在整个 arena 无放回随机挑 N 页", "p950", "dash"),
                  ("客户参考线", "coral", "dash")])
    groups = [(0, len(HD_X), "4 KiB 页的写脏覆盖量 MiB（档位等距排列，非线性刻度），0 为基底", True)]

    d.section(1, "checkpoint 墙钟", note=f"参考线 {REF_CK} ms；珊瑚色数字为超线值")
    tierpanel(d, HD_X, [("seq", "p950", HD_CK_SEQ, "line"), ("rand", "p950", HD_CK_RAND, "dash")],
              (0, 300), [0, 100, 200, 300], (REF_CK, f"参考线 {REF_CK} ms"), groups, h=200)

    d.section(2, "restore 墙钟（回到刚拍的 cpB）", note=f"参考线 {REF_RS} ms；珊瑚色数字为超线值")
    tierpanel(d, HD_X, [("seq", "p950", HD_RS_SEQ, "line"), ("rand", "p950", HD_RS_RAND, "dash")],
              (0, 560), [0, 100, 200, 300, 400, 500], (REF_RS, f"参考线 {REF_RS} ms"), groups, h=260)
    d.footer("注：这里的 restore 回到刚拍的 cpB，§4 混合分档是拍完后直接回基线；两者负载、页分布与回滚路径不同，"
             "不能拼成同一条曲线。脚本不据 200 / 100 ms 判 FAIL，参考线只作对照。21 个组合的 MD5 都是 3/3 一致。")
    d.save(out("rollback", "18-3-HDBSS写密集基准"))


if __name__ == "__main__":
    fig_tiers()
    fig_segments()
    fig_hdbss()
