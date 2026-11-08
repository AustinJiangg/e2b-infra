"""chart_kit：和 diagram_kit 同一风格的数据图（SVG）。在 Diagram 上加几种图表面板。"""
import math
from diagram_kit import Diagram, RAMPS, INK, SUB, MUTED, text_w, wrap

GRID, AXIS = "#E8E6DF", "#B4B2A9"
# 色阶别名：项目可以给系列起名，比如 chart_kit.ALIASES["baseline"] = "gray"，之后用系列名代替色阶名
ALIASES = {}


def col(ramp, k=1):
    return RAMPS[ALIASES.get(ramp, ramp)][k]


def fmt(v, digits=3):
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e9:
        return f"{v/1e9:.{digits-1}f}e9".replace(".00e9", "e9")
    if a >= 1e5:
        s = f"{v/1e4:.1f}".rstrip("0").rstrip(".")
        return s + " 万"
    if a >= 1e4:
        s = f"{v/1e4:.2f}".rstrip("0").rstrip(".")
        return s + " 万"
    if a >= 100:
        return f"{v:,.0f}"
    if a >= 10:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{v:.{digits-1}f}".rstrip("0").rstrip(".")


def nice_ticks(lo, hi, n=5):
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if span / step <= n + 0.5:
            break
    t0 = math.ceil(lo / step - 1e-9) * step
    ticks, t = [], t0
    while t <= hi + 1e-9 * step:
        ticks.append(round(t, 10))
        t += step
    return ticks


class Chart(Diagram):
    def __init__(self, width=1400, margin=48):
        super().__init__(width, margin)

    # ---------- 小工具
    def line(self, x1, y1, x2, y2, color=GRID, w=1, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.o.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{w}"{d}/>')

    def circle(self, x, y, r, fill, stroke="#FFFFFF", sw=1.2, op=1.0):
        self.o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" fill-opacity="{op}"/>')

    def poly(self, pts, color, w=2.4, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        self.o.append(f'<polyline points="{p}" fill="none" stroke="{color}" stroke-width="{w}" stroke-linejoin="round" stroke-linecap="round"{d}/>')

    def legend_row(self, items, x=None, y=None):
        """items: [(名字, 色阶, 形状 'dot'|'bar'|'line'|'dash')]"""
        x = self.M if x is None else x
        y = self.y + 22 if y is None else y
        for name, ramp, shape in items:
            c = col(ramp)
            if shape == "bar":
                self.rect(x, y - 11, 22, 14, RAMPS[ALIASES.get(ramp, ramp)][0], c, sw=1.2, rx=3)
                x += 30
            elif shape == "hollow":
                self.circle(x + 6, y - 4, 6, "#FFFFFF", c, 2)
                x += 18
            elif shape in ("line", "dash"):
                self.line(x, y - 4, x + 26, y - 4, c, 2.6, "6 4" if shape == "dash" else None)
                x += 34
            else:
                self.circle(x + 6, y - 4, 6, c)
                x += 18
            self.text(x, y + 1, name, 14, SUB)
            x += text_w(name, 14) + 30
        self.y = y + 12

    def subhead(self, title, note=None):
        self.text(self.M, self.y + 34, title, 18, INK, 600)
        if note:
            self.text(self.M + text_w(title, 18) + 16, self.y + 34, note, 14, MUTED)
        self.y += 46

    # ---------- 横向堆叠条
    def hstack(self, rows, segs, total_label=True, label_w=190, bar_h=40, gap=22, vmax=None, unit="",
               xticks=None, seg_labels=True):
        """rows: [(行名, [各段数值], 行副标题)]；segs: [(段名, 色阶)]"""
        x0 = self.M + label_w
        x1 = self.W - self.M - 110
        vmax = vmax or max(sum(v) for _, v, *_ in rows)
        sc = (x1 - x0) / vmax
        y = self.y + 26
        top = y
        ticks = xticks or nice_ticks(0, vmax, 5)
        n = len(rows)
        bottom = y + n * (bar_h + gap) - gap
        for t in ticks:
            xx = x0 + t * sc
            self.line(xx, top - 6, xx, bottom + 6, GRID)
            self.text(xx, bottom + 26, fmt(t) + unit, 13, MUTED, 400, "middle")
        for r in rows:
            name, vals = r[0], r[1]
            sub = r[2] if len(r) > 2 else None
            if sub:
                self.text(x0 - 14, y + bar_h / 2 - 2, name, 15, INK, 600, "end")
                self.text(x0 - 14, y + bar_h / 2 + 16, sub, 12.5, MUTED, 400, "end")
            else:
                self.text(x0 - 14, y + bar_h / 2 + 5, name, 15, INK, 600, "end")
            xx = x0
            for (sname, ramp), v in zip(segs, vals):
                w = v * sc
                if w <= 0:
                    continue
                f, s, tc, _ = RAMPS[ALIASES.get(ramp, ramp)]
                self.rect(xx, y, max(w, 1.5), bar_h, f, s, sw=1.1, rx=4)
                lab = fmt(v)
                if seg_labels and text_w(lab, 13.5) + 14 < w:
                    self.text(xx + w / 2, y + bar_h / 2 + 5, lab, 13.5, tc, 600, "middle")
                xx += w
            if total_label:
                self.text(xx + 10, y + bar_h / 2 + 5, fmt(sum(vals)) + unit, 14, INK, 600)
            y += bar_h + gap
        self.y = bottom + 40

    # ---------- 每次运行的散点（小多图）
    def strips(self, panels, per_row=2, h=290, gap_x=56, gap_y=40, label_bottom=True):
        """panels: [dict(title, unit, groups=[(名, 色阶, [值...])], ylim=None, fmt=None, note=None, ref=None)]"""
        W = self.W - 2 * self.M
        pw = (W - (per_row - 1) * gap_x) / per_row
        rows = [panels[i:i + per_row] for i in range(0, len(panels), per_row)]
        y = self.y + 10
        for prow in rows:
            for j, p in enumerate(prow):
                self._strip(self.M + j * (pw + gap_x), y, pw, h, p)
            y += h + gap_y
        self.y = y - gap_y + 4

    def _strip(self, x, y, w, h, p):
        f = p.get("fmt") or (lambda v: fmt(v))
        self.text(x, y + 18, p["title"], 16.5, INK, 600)
        if p.get("note"):
            self.text(x, y + 40, p["note"], 13, MUTED)
        top = y + (82 if p.get("note") else 56)
        ax_l = x + 78
        ax_r = x + w - 10
        bot = y + h - 40
        vals = []
        for g in p["groups"]:
            if isinstance(g[2], tuple):
                vals += [g[2][0] - g[2][1], g[2][0] + g[2][1]]
            else:
                vals += list(g[2])
        lo, hi = p.get("ylim") or (min(vals), max(vals))
        if not p.get("ylim"):
            pad = (hi - lo) * 0.18 or abs(hi) * 0.01
            lo, hi = lo - pad, hi + pad
        ticks = nice_ticks(lo, hi, 4)
        sy = lambda v: bot - (v - lo) / (hi - lo) * (bot - top)
        for t in ticks:
            if lo <= t <= hi:
                self.line(ax_l, sy(t), ax_r, sy(t), GRID)
                self.text(ax_l - 10, sy(t) + 4.5, f(t), 12.5, MUTED, 400, "end")
        if p.get("unit"):
            self.text(ax_l - 10, top - 16, p["unit"], 12.5, MUTED, 400, "end")
        n = len(p["groups"])
        cw = (ax_r - ax_l) / n
        for i, (gname, ramp, gv) in enumerate(p["groups"]):
            cx = ax_l + cw * (i + 0.5)
            c = col(ramp)
            if isinstance(gv, tuple):      # 只有均值 ± sd
                m, sd = gv
                self.line(cx, sy(m - sd), cx, sy(m + sd), c, 2.2)
                for v in (m - sd, m + sd):
                    self.line(cx - 8, sy(v), cx + 8, sy(v), c, 2.2)
                self.line(cx - cw * 0.3, sy(m), cx + cw * 0.3, sy(m), c, 3)
                self.circle(cx, sy(m), 6.5, c)
            else:
                m = sum(gv) / len(gv)
                self.line(cx - cw * 0.3, sy(m), cx + cw * 0.3, sy(m), c, 3)
                k = len(gv)
                for t, v in enumerate(gv):
                    dx = (t - (k - 1) / 2) * min(11, cw * 0.5 / max(k, 1))
                    self.circle(cx + dx, sy(v), 6, c, "#FFFFFF", 1.4, 0.9)
            self.text(cx, bot + 22, gname, 14, INK, 600, "middle")
        ref = p.get("ref")
        if ref:
            v, lab = ref
            self.line(ax_l, sy(v), ax_r, sy(v), MUTED, 1.2, "5 4")
            self.text(ax_r, sy(v) - 7, lab, 12.5, MUTED, 400, "end")

    # ---------- 置信区间（森林图）
    def forest(self, rows, xlim, label_w=300, row_h=46, unit="%", band=None, zero=0, ticks=None):
        """rows: [(行名, 均值, 下限, 上限, 色阶, 右侧注释)]；band: (lo, hi, 说明) 浅色竖带（如预期效应）"""
        x0 = self.M + label_w
        x1 = self.W - self.M - 200
        lo, hi = xlim
        sx = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)
        y = self.y + 30
        top = y
        bot = y + row_h * len(rows)
        if band:
            bl, bh, blab = band
            self.rect(sx(bl), top - 8, max(sx(bh) - sx(bl), 3), bot - top + 16, "#FAEEDA", "#BA7517", sw=0.8, rx=2)
            self.text(sx(bh) + 8, top - 14, blab, 12.5, "#854F0B", 600)
        for t in (ticks or nice_ticks(lo, hi, 6)):
            self.line(sx(t), top - 6, sx(t), bot + 4, GRID)
            lab = ((f"{t:+g}" if t else "0") + unit).replace("-", "−")
            self.text(sx(t), bot + 24, lab, 12.5, MUTED, 400, "middle")
        self.line(sx(zero), top - 10, sx(zero), bot + 6, INK, 1.4)
        for k, (name, m, l, h_, ramp, note) in enumerate(rows):
            yy = y + row_h * k + row_h / 2
            if k % 2 == 0:
                self.rect(self.M, yy - row_h / 2, self.W - 2 * self.M, row_h, "#F7F6F2", "none", sw=0, rx=4)
            c = col(ramp)
            self.text(x0 - 16, yy + 5, name, 14.5, INK, 600, "end")
            l2, h2 = max(l, lo), min(h_, hi)
            self.line(sx(l2), yy, sx(h2), yy, c, 3.2)
            for v, clipped, sgn in ((l2, l < lo, -1), (h2, h_ > hi, 1)):
                if clipped:   # 超出画面：画箭头
                    xx = sx(v)
                    self.o.append(f'<path d="M{xx:.1f},{yy:.1f} L{xx - sgn * 12:.1f},{yy - 7:.1f} L{xx - sgn * 12:.1f},{yy + 7:.1f} Z" fill="{c}"/>')
                else:
                    self.line(sx(v), yy - 7, sx(v), yy + 7, c, 2)
            self.circle(sx(m), yy, 7, c)
            if note:
                self.text(x1 + 18, yy + 5, note, 13.5, SUB)
        self.y = bot + 40

    # ---------- 对数刻度横条
    def logbars(self, items, lo_exp, hi_exp, label_w=330, bar_h=34, gap=16, unit="", tick_fmt=None, value_fmt=None):
        """items: [(名, 值, 色阶, 右侧注释)]"""
        x0 = self.M + label_w
        x1 = self.W - self.M - 260
        sx = lambda v: x0 + (math.log10(v) - lo_exp) / (hi_exp - lo_exp) * (x1 - x0)
        y = self.y + 40
        top = y
        bot = y + len(items) * (bar_h + gap) - gap
        for e in range(lo_exp, hi_exp + 1):
            xx = sx(10 ** e)
            self.line(xx, top - 6, xx, bot + 6, GRID)
            lab = tick_fmt(e) if tick_fmt else f"1e{e}"
            self.text(xx, bot + 26, lab, 12.5, MUTED, 400, "middle")
        for name, v, ramp, note in items:
            f, s, tc, sc = RAMPS[ALIASES.get(ramp, ramp)]
            self.text(x0 - 14, y + bar_h / 2 + 5, name, 14.5, INK, 600, "end")
            w = sx(v) - x0
            self.rect(x0, y, max(w, 2), bar_h, f, s, sw=1.1, rx=4)
            vs = value_fmt(v) if value_fmt else fmt(v)
            self.text(x0 + w + 10, y + bar_h / 2 + 5, vs, 14, tc, 600)
            if note:
                self.text(x1 + 70, y + bar_h / 2 + 5, note, 13, SUB)
            y += bar_h + gap
        self.y = bot + 42

    # ---------- 折线（类别型 x）
    def lines(self, xlabels, series, ylim=None, h=360, ytitle=None, yfmt=None, label_w=90, annotate=True,
              logy=False, right_pad=170):
        """series: [(名, 色阶, [值...], 'line'|'dash')]"""
        x0 = self.M + label_w
        x1 = self.W - self.M - right_pad
        top = self.y + 24
        bot = top + h
        vals = [v for s in series for v in s[2] if v is not None]
        if logy:
            lo, hi = ylim or (10 ** math.floor(math.log10(min(vals))), 10 ** math.ceil(math.log10(max(vals))))
            tr = lambda v: math.log10(v)
            ticks = [10 ** e for e in range(int(math.log10(lo)), int(math.log10(hi)) + 1)]
        else:
            lo, hi = ylim or (0, max(vals) * 1.12)
            tr = lambda v: v
            ticks = nice_ticks(lo, hi, 5)
        sy = lambda v: bot - (tr(v) - tr(lo)) / (tr(hi) - tr(lo)) * (bot - top)
        f = yfmt or fmt
        for t in ticks:
            self.line(x0, sy(t), x1, sy(t), GRID)
            self.text(x0 - 10, sy(t) + 4.5, f(t), 12.5, MUTED, 400, "end")
        if ytitle:
            self.text(x0 - 10, top - 12, ytitle, 12.5, MUTED, 400, "end")
        n = len(xlabels)
        sx = lambda i: x0 + (x1 - x0) * (i + 0.5) / n
        for i, lab in enumerate(xlabels):
            self.text(sx(i), bot + 26, lab, 14, INK, 600, "middle")
        for name, ramp, vs, style in series:
            c = col(ramp)
            pts = [(sx(i), sy(v)) for i, v in enumerate(vs) if v is not None]
            self.poly(pts, c, 2.8, "7 5" if style == "dash" else None)
            for (px, py), v in zip(pts, [v for v in vs if v is not None]):
                self.circle(px, py, 6, c, "#FFFFFF", 1.6)
            if annotate:
                px, py = pts[-1]
                self.text(px + 14, py + 5, name, 14, RAMPS[ALIASES.get(ramp, ramp)][2], 600)
        self.y = bot + 44

    # ---------- 按运行顺序的散点
    def seq(self, points, h=300, label_w=90, ylim=None, yfmt=None, ytitle=None, xlabel=None, bands=None, refs=None,
            right_pad=170):
        """points: [(x 标签, 值, 色阶, 是否空心)]，按顺序排开；bands: [(lo, hi, 说明)] 横向浅色带"""
        x0 = self.M + label_w
        x1 = self.W - self.M - right_pad
        top = self.y + 24
        bot = top + h
        vals = [p[1] for p in points]
        lo, hi = ylim or (min(vals), max(vals))
        if not ylim:
            pad = (hi - lo) * 0.15
            lo, hi = lo - pad, hi + pad
        sy = lambda v: bot - (v - lo) / (hi - lo) * (bot - top)
        f = yfmt or fmt
        for b0, b1, lab in (bands or []):
            self.rect(x0, sy(b1), x1 - x0, sy(b0) - sy(b1), "#F7F6F2", "#E5E3DB", sw=0.8, rx=4)
            self.text(x1 + 12, (sy(b1) + sy(b0)) / 2 + 5, lab, 13, SUB, 400)
        for t in nice_ticks(lo, hi, 5):
            if lo <= t <= hi:
                self.line(x0, sy(t), x1, sy(t), GRID)
                self.text(x0 - 10, sy(t) + 4.5, f(t), 12.5, MUTED, 400, "end")
        if ytitle:
            self.text(x0 - 10, top - 12, ytitle, 12.5, MUTED, 400, "end")
        for v, lab in (refs or []):
            self.line(x0, sy(v), x1, sy(v), MUTED, 1.1, "5 4")
            self.text(x0 + 8, sy(v) - 7, lab, 12.5, MUTED)
        n = len(points)
        sx = lambda i: x0 + (x1 - x0) * (i + 0.5) / n
        for i, (lab, v, ramp, hollow) in enumerate(points):
            c = col(ramp)
            if hollow:
                self.circle(sx(i), sy(v), 6.5, "#FFFFFF", c, 2)
            else:
                self.circle(sx(i), sy(v), 7, c, "#FFFFFF", 1.4)
            if lab:
                self.text(sx(i), bot + 22, lab, 12, MUTED, 400, "middle")
        if xlabel:
            self.text((x0 + x1) / 2, bot + 46, xlabel, 13, SUB, 400, "middle")
        self.y = bot + (58 if xlabel else 36)
