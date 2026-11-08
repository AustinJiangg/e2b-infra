"""20 章的数据图（920B）。

- 图 20-1：§2.2 混合档与 §2.3 纯内存 / 纯文件 / 只读的 checkpoint、restore p50 / p99（新 920B）。
  数据集中在下面的 MIX_TIERS、PURE_TIERS、JUDGE_LINES，以后重测只改这几个常量。
- 图 20-2：§6 体积修复前后（旧 920B），数字取自 §6.1 与 §6.3 的表格。
  颜色：修复前（驻留判据导出）coral，修复后（4 KiB 写跟踪差分）teal。

本文件还提供三个通用面板，供 draw_rollback_19.py、draw_rollback_21.py、draw_gsd_D.py、draw_gsd_E.py 复用：
- gbars：分组横条，每组 1～n 根条，值可以是区间 (lo, hi)，数值文字照原文写，支持对数 / 线性刻度；
- dotrows：每行若干个点（或区间短条）画在一条非零起点的线性轴上，右侧固定列写原文数值；
- tier_panel：横轴按档位顺序等距的折线面板，横轴下方用数值行写出每个点的原文数字。
"""
import math

from common import *  # noqa: F401,F403
from chart_kit import col, nice_ticks, GRID, AXIS
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w

# ---------------------------------------------------------------- 图 20-1 的数据
# 来源：rollback/docs/20-920b-performance.md §2.2 表（第 66～75 行）。客户端墙钟 ms。
# (名义改动, n, checkpoint p50, checkpoint p99, restore p50, restore p99)
MIX_TIERS = [
    ("0 MB", 100, 14.9, 19.5, 18.1, 23.8),
    ("4 MB", 100, 16.2, 23.2, 19.0, 24.8),
    ("8 MB", 100, 17.1, 22.7, 19.7, 25.2),
    ("16 MB", 100, 19.2, 23.5, 21.4, 27.2),
    ("32 MB", 100, 23.3, 29.1, 25.1, 31.1),
    ("64 MB", 100, 34.4, 40.4, 30.8, 36.3),
    ("128 MB", 60, 55.9, 63.3, 43.6, 49.5),
    ("192 MB", 60, 77.1, 83.0, 55.1, 63.0),
    ("256 MB", 60, 97.3, 118.7, 66.1, 71.8),
    ("512 MB", 30, 176.7, 183.9, 108.2, 113.0),   # 极限档
]
# 来源：同章 §2.3 表（第 84～86 行）。(类别, 档, n, checkpoint p50, p99, restore p50, p99)
PURE_TIERS = [
    ("纯内存", "4 MB", 30, 15.4, 21.2, 19.6, 25.7),
    ("纯内存", "16 MB", 30, 16.1, 21.5, 20.8, 26.5),
    ("纯内存", "48 MB", 30, 18.3, 20.8, 26.6, 31.7),
    ("纯内存", "128 MB", 30, 23.6, 24.3, 40.3, 49.4),
    ("纯文件", "4 MB", 30, 18.8, 24.7, 20.1, 24.7),
    ("纯文件", "16 MB", 30, 32.2, 38.1, 23.4, 28.2),
    ("纯文件", "64 MB", 30, 82.2, 91.1, 33.1, 41.6),
    ("只读触碰", "192 MB", 20, 15.4, 19.7, 19.2, 24.1),
]
# 来源：同章 §2.1 第 60 行，判定线 checkpoint ≤ 200 ms、restore ≤ 100 ms
JUDGE_LINES = {"checkpoint": 200, "restore": 100}


def _ramp(r):
    return RAMPS[chart_kit.ALIASES.get(r, r)]


def gbars(d, groups, scale, label_w=230, bar_h=24, inner=6, gap=22, note_w=300, ticks=None,
          tick_fmt=None, axis_title=None):
    """groups: [(行名, 行副标题或 None, [(值或 (lo, hi), 色阶, 数值文字, 右侧注释或 None), ...])]
    scale: ("log", lo_exp, hi_exp) 或 ("lin", vmax)。区间值画成实心到 lo、虚线框到 hi。"""
    x0 = d.M + label_w
    x1 = d.W - d.M - note_w
    if scale[0] == "log":
        lo_e, hi_e = scale[1], scale[2]
        sx = lambda v: x0 + (math.log10(v) - lo_e) / (hi_e - lo_e) * (x1 - x0)
        tks = ticks or [10 ** e for e in range(lo_e, hi_e + 1)]
    else:
        vmax = scale[1]
        sx = lambda v: x0 + v / vmax * (x1 - x0)
        tks = ticks or nice_ticks(0, vmax, 5)
    top = d.y + 30
    hs = [len(b) * bar_h + (len(b) - 1) * inner for _, _, b in groups]
    bottom = top + sum(hs) + gap * (len(groups) - 1)
    for t in tks:
        xx = sx(t)
        d.line(xx, top - 8, xx, bottom + 8, GRID)
        lab = tick_fmt(t) if tick_fmt else f"{t:g}"
        d.text(xx, bottom + 28, lab, 12.5, MUTED, 400, "middle")
    if axis_title:
        d.text(x1, bottom + 50, axis_title, 12.5, MUTED, 400, "end")
    d.line(x0, top - 8, x0, bottom + 8, AXIS, 1.2)
    y = top
    for (name, sub, bars), h in zip(groups, hs):
        cy = y + h / 2
        if sub:
            d.text(x0 - 14, cy - 3, name, 15, INK, 600, "end")
            d.text(x0 - 14, cy + 15, sub, 12.5, MUTED, 400, "end")
        else:
            d.text(x0 - 14, cy + 5, name, 15, INK, 600, "end")
        by = y
        for v, ramp, vtext, note in bars:
            f, s, tc, sc = _ramp(ramp)
            if isinstance(v, tuple):
                lo, hi = v
                d.rect(x0, by, max(sx(lo) - x0, 2), bar_h, f, s, sw=1.1, rx=4)
                d.rect(sx(lo), by, max(sx(hi) - sx(lo), 2), bar_h, "#FFFFFF", s, sw=1.1, rx=2, dash="4 3")
                end = sx(hi)
            else:
                end = sx(v)
                d.rect(x0, by, max(end - x0, 2), bar_h, f, s, sw=1.1, rx=4)
            d.text(end + 10, by + bar_h / 2 + 5, vtext, 14, tc, 600)
            if note:
                d.text(x1 + 96, by + bar_h / 2 + 5, note, 13, SUB)
            by += bar_h + inner
        y += h + gap
    d.y = bottom + (60 if axis_title else 44)


def dotrows(d, rows, lo, hi, label_w=330, row_h=34, val_w=260, ticks=None, refs=None, axis_title=None,
            sub_gap=13):
    """rows: [(行名, [(值或 (lo, hi), 色阶), ...], 右侧数值文字)]；同一行多个系列上下错开 sub_gap。
    refs: [(值, 说明)] 竖向虚线参考线。"""
    x0 = d.M + label_w
    x1 = d.W - d.M - val_w
    sx = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)
    top = d.y + (40 if refs else 24)
    bottom = top + len(rows) * row_h
    for t in ticks or nice_ticks(lo, hi, 6):
        if lo <= t <= hi:
            d.line(sx(t), top - 4, sx(t), bottom + 4, GRID)
            d.text(sx(t), bottom + 24, f"{t:g}", 12.5, MUTED, 400, "middle")
    if axis_title:
        d.text(x1, bottom + 46, axis_title, 12.5, MUTED, 400, "end")
    for v, lab in refs or []:
        d.line(sx(v), top - 14, sx(v), bottom + 4, MUTED, 1.1, "5 4")
        d.text(sx(v), top - 20, lab, 12.5, MUTED, 400, "middle")
    for i, (name, marks, vtext) in enumerate(rows):
        cy = top + i * row_h + row_h / 2
        if i % 2 == 0:
            d.rect(d.M, cy - row_h / 2, d.W - 2 * d.M, row_h, "#FAFAF7", "#FAFAF7", sw=0, rx=0)
        d.text(x0 - 16, cy + 5, name, 14.5, INK, 600, "end")
        n = len(marks)
        for k, (v, ramp) in enumerate(marks):
            yy = cy + (k - (n - 1) / 2) * sub_gap
            c = col(ramp)
            if isinstance(v, tuple):
                a, b = sx(v[0]), sx(v[1])
                d.rect(a - 5, yy - 5, b - a + 10, 10, _ramp(ramp)[0], c, sw=1.6, rx=5)
            else:
                d.circle(sx(v), yy, 6.5, c, "#FFFFFF", 1.4)
        d.text(x1 + 30, cy + 5, vtext, 14, INK, 600)
    d.y = bottom + (62 if axis_title else 40)


def tier_panel(d, x0, x1, top, h, xlabels, series, ylim, yticks, yfmt=None, ref=None, xsub=None, rows=None,
               title=None, title_x=None):
    """横轴按档位顺序等距的折线面板，画在 [x0, x1]，绘图区上沿 top、高 h。
    series: [(名, 色阶, [值或 None], 'line'|'dash', 空心?)]；None 的点不画。
    ref: (值, 说明) 灰色虚线参考线（判定线）。xsub: 每档横轴标签下的小字。
    rows: 横轴下方的数值行 [(行名, 色阶或 None, [文字...])]，写原文数字。返回面板最底部 y。"""
    lo, hi = ylim
    bot = top + h
    sy = lambda v: bot - (v - lo) / (hi - lo) * h
    n = len(xlabels)
    sx = lambda i: x0 + (x1 - x0) * (i + 0.5) / n
    yf = yfmt or (lambda t: f"{t:g}")
    if title:
        d.text(x0 if title_x is None else title_x, top - 22, title, 16, INK, 600)
    for t in yticks:
        d.line(x0, sy(t), x1, sy(t), GRID)
        d.text(x0 - 10, sy(t) + 4.5, yf(t), 12.5, MUTED, 400, "end")
    d.line(x0, bot, x1, bot, AXIS, 1.2)
    if ref:
        v, lab = ref
        d.line(x0, sy(v), x1, sy(v), MUTED, 1.3, "6 4")
        d.text(x0 + 8, sy(v) - 7, lab, 12.5, SUB)
    for name, ramp, vs, style, hollow in series:
        c = col(ramp)
        pts = [(sx(i), sy(v)) for i, v in enumerate(vs) if v is not None]
        d.poly(pts, c, 2.6, "7 5" if style == "dash" else None)
        for px, py in pts:
            if hollow:
                d.circle(px, py, 5.5, "#FFFFFF", c, 2)
            else:
                d.circle(px, py, 5.5, c, "#FFFFFF", 1.4)
    for i, lab in enumerate(xlabels):
        d.text(sx(i), bot + 24, lab, 13.5, INK, 600, "middle")
        if xsub:
            d.text(sx(i), bot + 42, xsub[i], 12, MUTED, 400, "middle")
    y = bot + (66 if xsub else 50)
    for k, (name, ramp, texts) in enumerate(rows or []):
        c = RAMPS[chart_kit.ALIASES.get(ramp, ramp)][2] if ramp else MUTED
        w = 600 if ramp else 400
        if k == 0:
            d.line(x0, y - 17, x1, y - 17, GRID)
        d.text(x0 - 10, y, name, 13, c, w, "end")
        for i, t in enumerate(texts):
            if t:
                d.text(sx(i), y, t, 13, c, w, "middle")
        y += 22
    return y - 22 + 8 if rows else bot + (48 if xsub else 32)


def pair_ranges(d, rows, vmax, ticks, ramp="amber", label_w=250, sub_h=16, inner=6, gap=16, group_gap=14,
                tick_fmt=None):
    """每行两根短条：上面实心（如 checkpoint），下面白底虚线框（如 restore）。
    条长到 p50，p50 之后的细线画到 p99（实线 / 虚线与条一致），右端写“p50 / p99”原文。
    rows: [(组名, 行名, 行副标题, (上 p50, 上 p99), (下 p50, 下 p99))]，组名变化时多留 group_gap。"""
    f, s, tc, sc = _ramp(ramp)
    x0 = d.M + label_w
    x1 = d.W - d.M - 150
    sx = lambda v: x0 + v / vmax * (x1 - x0)
    top = d.y + 24
    rh = 2 * sub_h + inner
    ys, y, prev = [], top, None
    for g, *_ in rows:
        if prev is not None:
            y += gap + (group_gap if g != prev else 0)
        ys.append(y)
        y += rh
        prev = g
    bottom = y
    for t in ticks:
        d.line(sx(t), top - 8, sx(t), bottom + 8, GRID)
        d.text(sx(t), bottom + 28, tick_fmt(t) if tick_fmt else f"{t:g}", 12.5, MUTED, 400, "middle")
    d.line(x0, top - 8, x0, bottom + 8, AXIS, 1.2)
    prev = None
    for (g, name, sub, a, b), yy in zip(rows, ys):
        cy = yy + rh / 2
        if g != prev:
            d.text(d.M, cy + 5, g, 15, INK, 600)
            prev = g
        d.text(x0 - 14, cy - 2, name, 14.5, INK, 600, "end")
        if sub:
            d.text(x0 - 14, cy + 15, sub, 12, MUTED, 400, "end")
        for k, ((p50, p99), dashed) in enumerate(((a, False), (b, True))):
            by = yy + k * (sub_h + inner)
            if dashed:
                d.rect(x0, by, max(sx(p50) - x0, 2), sub_h, "#FFFFFF", s, sw=1.4, rx=3, dash="4 3")
            else:
                d.rect(x0, by, max(sx(p50) - x0, 2), sub_h, f, s, sw=1.2, rx=3)
            mid = by + sub_h / 2
            d.line(sx(p50), mid, sx(p99), mid, s, 1.6, "4 3" if dashed else None)
            d.line(sx(p99), mid - 5, sx(p99), mid + 5, s, 1.6)
            d.text(sx(p99) + 10, mid + 4.5, f"{p50:.1f} / {p99:.1f}", 13, tc, 600)
    d.y = bottom + 44


def range_legend(d, items, ramp="amber"):
    """pair_ranges 的图例：items = [(说明, 是否虚线框)]。"""
    f, s, tc, sc = _ramp(ramp)
    x, y = d.M, d.y + 22
    for name, dashed in items:
        if dashed:
            d.rect(x, y - 11, 26, 13, "#FFFFFF", s, sw=1.4, rx=3, dash="4 3")
        else:
            d.rect(x, y - 11, 26, 13, f, s, sw=1.2, rx=3)
        d.line(x + 26, y - 4.5, x + 44, y - 4.5, s, 1.6, "4 3" if dashed else None)
        d.line(x + 44, y - 9, x + 44, y, s, 1.6)
        x += 54
        d.text(x, y + 1, name, 14, SUB)
        x += text_w(name, 14) + 30
    d.y = y + 12


# ---------------------------------------------------------------- 图 20-1
def fig_tiers():
    d = Chart()
    d.header("920B 分档：checkpoint 与 restore 随改动量的 p50 / p99",
             "新 920B，kvm-wp，deltabox-dev@8ea5322bf，2026-09-29；单沙箱串行、页缓存全热，bench_tiers.py 全表 3 遍共 1040 次迭代。"
             "客户端墙钟 ms。p99 取最近秩法，n < 100 的档取最大值。横轴按档位顺序等距排列，不按数值比例。")
    names = [t[0] for t in MIX_TIERS]
    subs = [f"极限档 · n {t[1]}" if t[0] == "512 MB" else f"n {t[1]}" for t in MIX_TIERS]
    x0, x1 = d.M + 120, d.W - d.M - 10
    ms = lambda t: f"{t:g} ms"
    for num, op, i50, i99, ylim, ticks in ((1, "checkpoint", 2, 3, (0, 250), [0, 50, 100, 150, 200, 250]),
                                           (2, "restore", 4, 5, (0, 125), [0, 25, 50, 75, 100, 125])):
        d.section(num, f"混合档（内存:文件 = 3:1）：{op}", note=f"判定线 {op} ≤ {JUDGE_LINES[op]} ms")
        d.legend_row([(f"{op} p50", "amber", "line"), (f"{op} p99", "amber", "dash"), ("判定线", "gray", "dash")])
        p50 = [t[i50] for t in MIX_TIERS]
        p99 = [t[i99] for t in MIX_TIERS]
        d.y = tier_panel(d, x0, x1, d.y + 36, 230, names,
                         [(f"{op} p99", "amber", p99, "dash", True), (f"{op} p50", "amber", p50, "line", False)],
                         ylim, ticks, yfmt=ms, ref=(JUDGE_LINES[op], f"判定线 {JUDGE_LINES[op]} ms"), xsub=subs,
                         rows=[("p50", "amber", [f"{v:.1f}" for v in p50]), ("p99", None, [f"{v:.1f}" for v in p99])])

    d.section(3, "纯内存、纯文件与只读触碰", note="条长到 p50，细线延到 p99；纯文件档写后 sync")
    range_legend(d, [("checkpoint p50 → p99", False), ("restore p50 → p99", True)])
    d.y += 6
    pair_ranges(d, [(g, name, f"n {n}", (c50, c99), (r50, r99)) for g, name, n, c50, c99, r50, r99 in PURE_TIERS],
                100, [0, 20, 40, 60, 80, 100], tick_fmt=ms)
    d.footer("注：全量 checkpoint（每遍首次，n = 3，服务端 total 433–445 ms、墙钟 630–670 ms）单列、不参与判定，图中未画。"
             "只读触碰 192 MB 档的内存差分 23.06 MB、盘层 0.71 MB。混合档“内存差分”大于名义内存份，因为文件那份经过 guest 页缓存，同时进了内存差分。")
    d.save(out("rollback", "20-1-920B分档checkpoint与restore"))


# ---------------------------------------------------------------- 图 20-2
def fig_volume():
    d = Chart()
    d.header("修复后导出等于写跟踪位图：只读 192 MiB 从 358 降到 12.7 MiB",
             "旧 920B，KVM 写保护，全部在显式 FC_TRACK_DIRTY_PAGES=true 下测。修复前是驻留判据（2026-09-10、09-11），"
             "修复后是 4 KiB 写跟踪差分（2026-09-11、09-14）。条长是原生 pause 实际导出的 memfile。")
    d.legend_row([("修复前：驻留判据导出", "coral", "bar"), ("修复后：4 KiB 写跟踪差分导出", "teal", "bar")])

    d.section(1, "pb2.py：每一步真做一次快照", note="模板 base，guest 内存 2 GiB，2 MiB 大页；单位 MiB，对数刻度")
    steps = [("A 刚建好", 154, "154", "4.1", 5.5, "5.5"),
             ("B 写 192 MiB", 370, "370", "219.8", 209.5, "209.5"),
             ("C 空转", 162, "162", "4.1", 4.5, "4.5"),
             ("D 只读 192 MiB", 358, "358", "14.6", 12.7, "12.7"),
             ("E 空转", 152, "152", "4.1", 4.1, "4.1"),
             ("F 只改 12 MiB", 176, "176", "20.5", 20.3, "20.3")]
    gbars(d, [(n, None, [(b, "coral", bt, f"同轮写跟踪位图 {bm}"), (a, "teal", at, None)])
              for n, b, bt, bm, a, at in steps],
          ("log", 0, 3), tick_fmt=lambda t: f"{t:g} MiB")

    d.section(2, "native_snapshot_bench.py：六档，pause 模式每档一代新进程",
              note="同一沙箱预热到最大档，cp0 含预热的 192 + 64 MB 本底；单位 MB")
    tiers = [("cp0", "0 / 0", 442, 281.7), ("cp1", "12 / 4", 432, 38.4), ("cp2", "24 / 8", 434, 54.2),
             ("cp3", "48 / 16", 440, 87.5), ("cp4", "96 / 32", 442, 153.1), ("cp5", "192 / 64", 450, 287.6)]
    gbars(d, [(n, f"名义内存 / 文件 {s}", [(b, "coral", f"{b:g}", None), (a, "teal", f"{a:g}", None)])
              for n, s, b, a in tiers],
          ("lin", 500), ticks=[0, 100, 200, 300, 400, 500], tick_fmt=lambda t: f"{t:g} MB", note_w=120)
    d.footer("注：修复前 memfile 恒在 440 MB 上下，是因为每档恢复后脚本把 192 MB 负载读一遍验证，这些页每代都被换入、被判脏；"
             "修复后 memfile 等于该档写入量加二三十 MB 本底。修复后的 4 KiB 写跟踪位图与导出逐行相等。950 只有修复前一轮，形状相同，数字见正文。")
    d.save(out("rollback", "20-2-体积修复前后"))


if __name__ == "__main__":
    fig_tiers()
    fig_volume()
