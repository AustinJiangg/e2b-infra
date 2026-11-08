"""diagram_kit：讲解型流程 / 对比图生成器（输出 SVG；需要 PNG 时另外调用 export_png）。纯标准库，无第三方依赖。"""
from xml.sax.saxutils import escape
import os, re, shutil, subprocess

FONT = ("'PingFang SC','Hiragino Sans GB','Microsoft YaHei','Noto Sans CJK SC',"
        "'Noto Sans SC','Source Han Sans SC',sans-serif")
INK, SUB, MUTED = "#2C2C2A", "#5F5E5A", "#888780"
# 色阶：fill, stroke, 标题字色, 副标题字色
RAMPS = {
    "gray":   ("#F7F6F2", "#B4B2A9", "#2C2C2A", "#5F5E5A"),
    "blue":   ("#E6F1FB", "#378ADD", "#0C447C", "#185FA5"),
    "coral":  ("#FAECE7", "#D85A30", "#712B13", "#993C1D"),
    "teal":   ("#E1F5EE", "#1D9E75", "#085041", "#0F6E56"),
    "purple": ("#EEEDFE", "#7F77DD", "#3C3489", "#534AB7"),
    "amber":  ("#FAEEDA", "#BA7517", "#633806", "#854F0B"),
    "green":  ("#EAF3DE", "#639922", "#27500A", "#3B6D11"),
    "pink":   ("#FBEAF0", "#D4537E", "#72243E", "#993556"),
}
# 箭头语义：flow 普通流程 / warn 代价、打断、异常 / back 返回、恢复 / async 稍后、异步
ARROWS = {
    "flow":  ("#5F5E5A", None),
    "warn":  ("#D85A30", None),
    "back":  ("#0F6E56", None),
    "async": ("#1D9E75", "6 4"),
}
TONES = {"bad": ("#A32D2D", 600), "good": ("#0F6E56", 600), "muted": (MUTED, 400), None: (INK, 400)}


# ------------------------------------------------------------------ 文字测量与换行
def _wide(ch):
    o = ord(ch)
    return o >= 0x2E80 or 0x2460 <= o <= 0x24FF or ch in "“”‘’…—→←↻×"


def _cw(ch, fs):
    if _wide(ch):
        return fs
    if ch == " ":
        return fs * 0.30
    if ch.isupper():
        return fs * 0.66
    if ch.isdigit():
        return fs * 0.57
    if ch.islower():
        return fs * 0.55
    if ch in "−_=+<>#%&@~^":
        return fs * 0.62
    return fs * 0.38


def text_w(s, fs):
    return sum(_cw(c, fs) if _wide(c) else _cw(c, fs) * 1.05 for c in s)


def _tokens(s):
    toks, cur = [], ""
    for ch in s:
        if ch == " " or _wide(ch):
            if cur:
                toks.append(cur)
                cur = ""
            toks.append(ch)
        else:
            cur += ch
    if cur:
        toks.append(cur)
    return toks


_CLOSE = set("，。、：；！？）」』”】》,.;:!?)")


def wrap(s, maxw, fs):
    """按宽度换行：中文逐字断行，英文按词断行；标点不出现在行首；避免最后一行只剩一两个字。"""
    if not s:
        return []
    lines, cur = [], []

    def w(tl):
        return text_w("".join(tl), fs)

    for t in _tokens(s):
        if not cur or w(cur + [t]) <= maxw:
            if not cur and w([t]) > maxw:
                piece = ""
                for ch in t:
                    if text_w(piece + ch, fs) > maxw and piece:
                        lines.append([piece])
                        piece = ""
                    piece += ch
                cur = [piece]
            else:
                cur.append(t)
            continue
        if t == " ":
            lines.append(cur)
            cur = []
        elif t in _CLOSE and len(cur) == 1:
            cur.append(t)
        elif t in _CLOSE:
            moved = cur.pop()
            lines.append(cur)
            cur = [moved, t]
        else:
            lines.append(cur)
            cur = [t]
    if cur:
        lines.append(cur)
    if len(lines) >= 2 and w(lines[-1]) < 2.2 * fs and len(lines[-2]) >= 3:
        lines[-1].insert(0, lines[-2].pop())
    return ["".join(tl).strip() for tl in lines]


# ------------------------------------------------------------------ 单元格构造
def Box(title, sub=None, style="fill", span=1, ramp=None):
    """方框。style: fill 实心浅色 / outline 白底彩边 / dashed 白底虚线（表示稍后、可选）。"""
    return dict(kind="box", title=title, sub=sub, style=style, span=span, ramp=ramp)


def Note(*lines, span=1, ramp=None):
    """虚线区域，里面居中放一两行说明文字（例如“没有额外开销”）。"""
    return dict(kind="note", lines=lines, span=span, ramp=ramp)


def Text(s, tone=None):
    """不画框，只在格子中间放一行文字（例如“—”“已经是最新的”）。"""
    return dict(kind="text", text=s, tone=tone)


def Step(title, sub=None, actor=None, tag=None, ramp="gray"):
    """流程行中的一步。actor：执行者小标签；tag：(文字, 色阶) 底部彩色标注，用来标出某方案在这一步的差异。"""
    return dict(title=title, sub=sub, actor=actor, tag=tag, ramp=ramp)


def _cell(c):
    if c is None or isinstance(c, dict):
        return c
    if isinstance(c, str):
        return Box(c)
    return Box(*c)


# ------------------------------------------------------------------ 主画布
class Diagram:
    TS, SS = 16.5, 14  # 框标题 / 副标题字号

    def __init__(self, width=1600, margin=48):
        self.W, self.M = width, margin
        self.y = 0
        self.o = []
        self.kinds = set()
        self._flow = None

    # ---------- 基础图元
    def text(self, x, y, s, size=14, color=INK, weight=400, anchor="start"):
        fw = f' font-weight="{weight}"' if weight != 400 else ""
        self.o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}"{fw} '
                      f'text-anchor="{anchor}">{escape(s)}</text>')

    def rect(self, x, y, w, h, fill, stroke, sw=1.2, rx=10, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.o.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
                      f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def arrow(self, x1, y1, x2, y2, kind="flow", label=None):
        col, dash = ARROWS[kind]
        self.kinds.add(kind)
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.o.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" '
                      f'stroke-width="1.8"{d} marker-end="url(#ar-{kind})"/>')
        if label:
            if abs(x1 - x2) < 1:   # 竖箭头：标签放右侧
                self.text(x1 + 12, (y1 + y2) / 2 + 5, label, 13, col, 600)
            else:                  # 横箭头：标签放上方
                self.text((x1 + x2) / 2, y1 - 8, label, 12.5, col, 600, "middle")

    def _lines(self, title, sub, w, ts=None, ss=None):
        ts, ss = ts or self.TS, ss or self.SS
        ent = [(s, ts, 0, 600, ts + 8) for s in wrap(title, w - 32, ts)]
        sl = wrap(sub, w - 32, ss) if sub else []
        if sl:
            ent.append((None, 0, 0, 0, 6))
            ent += [(s, ss, 1, 400, ss + 7) for s in sl]
        return ent

    def _need(self, title, sub, w, actor=False, tag=None, ts=None, ss=None, pad=28):
        h = sum(e[4] for e in self._lines(title, sub, w, ts, ss)) + pad
        if actor:
            h += 30
        if tag:
            h += 18 + 18 * len(wrap(tag[0], w - 48, 13))
        return h

    def block(self, x, y, w, h, title, sub=None, ramp="gray", style="fill", actor=None, tag=None,
              ts=None, ss=None):
        fill, stroke, tc, sc = RAMPS[ramp]
        if style == "fill":
            self.rect(x, y, w, h, fill, stroke)
        elif style == "outline":
            self.rect(x, y, w, h, "#FFFFFF", stroke)
        else:
            self.rect(x, y, w, h, "#FFFFFF", stroke, dash="6 4")
        top = y
        if actor:
            aw = text_w(actor, 12) + 18
            self.rect(x + 12, y + 11, aw, 22, "#FFFFFF", stroke, sw=0.8, rx=11)
            self.text(x + 12 + aw / 2, y + 26.5, actor, 12, sc, 500, "middle")
            top = y + 30
        th = 0
        if tag:
            tl = wrap(tag[0], w - 48, 13)
            th = 18 + 18 * len(tl)
            tf, tst, ttc, _ = RAMPS[tag[1]]
            ty = y + h - th + 2
            self.rect(x + 12, ty, w - 24, th - 10, tf, tst, sw=0.8, rx=6)
            for k, s in enumerate(tl):
                self.text(x + w / 2, ty + 17 + 18 * k, s, 13, ttc, 500, "middle")
        ent = self._lines(title, sub, w, ts, ss)
        y0 = top + (y + h - th - top - sum(e[4] for e in ent)) / 2
        for s, size, is_sub, wgt, lh in ent:
            if s is not None:
                self.text(x + w / 2, y0 + (lh + size * 0.72) / 2, s, size, sc if is_sub else tc, wgt, "middle")
            y0 += lh

    # ---------- 页面结构
    def header(self, title, subtitle=None, legend=None):
        """legend: [(箭头种类, 说明), ...]，只列图中真正用到的箭头。"""
        M = self.M
        self.text(M, 62, title, 30, INK, 600)
        y = 62
        if subtitle:
            for k, s in enumerate(wrap(subtitle, self.W - 2 * M, 15.5)):
                self.text(M, 96 + 24 * k, s, 15.5, SUB)
                y = 96 + 24 * k
        if legend:
            y += 34
            lx = M
            for kind, name in legend:
                self.arrow(lx, y - 5, lx + 40, y - 5, kind)
                self.text(lx + 50, y, name, 13.5, SUB)
                lx += 50 + text_w(name, 13.5) + 40
        self.y = y + 2

    def section(self, num, title, note=None, highlight=None):
        """分段标题：黑色序号圆 + 标题 + 灰色备注 + 可选红色高亮胶囊（如“本图重点”）。"""
        M = self.M
        cy = self.y + 46
        self.o.append(f'<circle cx="{M + 15}" cy="{cy}" r="15" fill="{INK}"/>')
        self.text(M + 15, cy + 5.5, str(num), 16, "#FFFFFF", 600, "middle")
        self.text(M + 42, cy + 7, title, 20, INK, 600)
        x = M + 42 + text_w(title, 20) + 20
        if highlight:
            pw = text_w(highlight, 13) + 22
            self.rect(x, cy - 10, pw, 24, "#FCEBEB", "#E24B4A", sw=0.8, rx=12)
            self.text(x + pw / 2, cy + 6, highlight, 13, "#A32D2D", 600, "middle")
            x += pw + 16
        if note:
            self.text(x, cy + 6, note, 14.5, MUTED)
        self.y = cy + 28

    def flow(self, steps, min_h=100, gap=48):
        """横向一行流程（2–5 步），步与步之间自动加箭头。steps: [Step(...)]"""
        n = len(steps)
        bw = (self.W - 2 * self.M - (n - 1) * gap) / n
        h = max([min_h] + [self._need(s["title"], s["sub"], bw, s["actor"], s["tag"]) for s in steps])
        xs = [self.M + i * (bw + gap) for i in range(n)]
        for i, s in enumerate(steps):
            self.block(xs[i], self.y, bw, h, s["title"], s["sub"], s["ramp"], actor=s["actor"], tag=s["tag"])
            if i < n - 1:
                self.arrow(xs[i] + bw + 4, self.y + h / 2, xs[i + 1] - 6, self.y + h / 2)
        self._flow = (xs, bw)
        self.y += h

    def callout(self, text, ramp="blue", span=None):
        """在上一行流程下方加一条彩色说明条；span=(i, j) 表示横跨第 i 到第 j 步（从 0 开始）。"""
        if span and self._flow:
            xs, bw = self._flow
            x0, x1 = xs[span[0]], xs[span[1]] + bw
        else:
            x0, x1 = self.M, self.W - self.M
        fill, stroke, tc, _ = RAMPS[ramp]
        lines = wrap(text, x1 - x0 - 32, 14.5)
        h = 18 + 22 * len(lines)
        y = self.y + 16
        self.rect(x0, y, x1 - x0, h, fill, stroke, sw=0.8, rx=8)
        for k, s in enumerate(lines):
            self.text((x0 + x1) / 2, y + 26 + 22 * k, s, 14.5, tc, 600, "middle")
        self.y = y + h

    def compare(self, columns, label_width=152):
        """多方案并排对比面板。columns: [(方案名, 一句话特点, 色阶), ...]，2–4 列。用完调用 .end()。"""
        return Compare(self, columns, label_width)

    def footer(self, text):
        M = self.M
        y = self.y + 36
        self.o.append(f'<line x1="{M}" y1="{y}" x2="{self.W - M}" y2="{y}" stroke="#E5E3DB" stroke-width="1"/>')
        for k, s in enumerate(wrap(text, self.W - 2 * M, 13)):
            self.text(M, y + 26 + 20 * k, s, 13, MUTED)
            self.y = y + 26 + 20 * k

    # ---------- 输出
    def save(self, svg_path):
        H = int(self.y + 32)
        defs = ["<defs>"]
        for k in sorted(self.kinds):
            c = ARROWS[k][0]
            defs.append(f'<marker id="ar-{k}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" '
                        f'markerHeight="7" orient="auto-start-reverse"><path d="M1.5 1L8.5 5L1.5 9" fill="none" '
                        f'stroke="{c}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></marker>')
        defs.append("</defs>")
        svg = "\n".join([f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.W}" height="{H}" '
                         f'viewBox="0 0 {self.W} {H}" font-family="{FONT}">', *defs,
                         f'<rect width="{self.W}" height="{H}" fill="#FFFFFF"/>', *self.o, "</svg>"])
        os.makedirs(os.path.dirname(os.path.abspath(svg_path)), exist_ok=True)
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg)
        print("SVG:", svg_path, f"{self.W}x{H}")
        return svg_path


class Compare:
    GAP, CG, PAD = 30, 22, 20

    def __init__(self, d, columns, label_width):
        self.d, self.cols_def, self.rows = d, columns, []
        n = len(columns)
        self.LX = d.M + 20
        x0 = d.M + 20 + label_width
        right = d.W - d.M - 20
        self.CW = (right - x0 - (n - 1) * self.CG) / n
        self.cx = [x0 + i * (self.CW + self.CG) for i in range(n)]

    # 各种行（label：左侧行标签；sub：标签下的灰色小字）
    def states(self, label, cells, sub=None):
        """各方案的初始状态：白底彩边小卡片，(标题, 副标题)。"""
        self.rows.append(dict(t="states", label=label, sub=sub, cells=[_cell(c) for c in cells]))
        return self

    def span(self, label, text, sub=None):
        """横跨所有列的事件条（各方案相同的触发点），下方自动分出箭头到每一列。"""
        self.rows.append(dict(t="span", label=label, sub=sub, text=text))
        return self

    def row(self, label, cells, sub=None, links=None):
        """每列一个格子：(标题, 副标题) / Box / Note / Text / None。
        links：与上一行之间每列的箭头，元素为 'flow' / ('warn', '标签') / None；默认上下都有框就画 flow。"""
        self.rows.append(dict(t="row", label=label, sub=sub, cells=[[_cell(c)] if c is not None else [] for c in cells],
                              links=links, slots=1))
        return self

    def stack(self, label, columns, sub=None, links=None, inner=None):
        """每列一串竖向小步骤（同一行内对齐）。columns：每列一个列表；inner：每列内部相邻步骤之间的箭头列表。"""
        cells = [[_cell(c) for c in col] for col in columns]
        slots = max(sum(c.get("span", 1) for c in col if c["kind"] != "text") or 1 for col in cells)
        self.rows.append(dict(t="row", label=label, sub=sub, cells=cells, links=links, slots=slots, inner=inner))
        return self

    def summary(self, title, rows):
        """对比小结表：rows = [(行名, [(文字, tone), ...]), ...]；tone: 'bad' 红 / 'good' 绿 / 'muted' 灰 / None。"""
        self.summary_def = (title, rows)
        return self

    # 布局与绘制
    def end(self):
        d, n, CW = self.d, len(self.cols_def), self.CW
        P0 = d.y - 2
        y = P0 + self.PAD
        head_y = y
        y += 62 + 16
        lay = []
        for r in self.rows:
            if r["t"] == "states":
                h = max([60] + [d._need(c["title"], c.get("sub"), CW - 80, ts=16, ss=13, pad=16) for c in r["cells"] if c])
            elif r["t"] == "span":
                h = 52
            else:
                sh = 56
                for col in r["cells"]:
                    for c in col:
                        if c["kind"] == "box" and c.get("span", 1) == 1:
                            sh = max(sh, d._need(c["title"], c.get("sub"), CW, ts=15.5, ss=13, pad=16))
                r["slot_h"] = sh
                h = sh * r["slots"] + 34 * (r["slots"] - 1)
            lay.append((y, h))
            y += h + self.GAP
        y -= self.GAP
        sm = getattr(self, "summary_def", None)
        tab_y = y + 52
        P1 = (tab_y + 32 + 44 * len(sm[1]) + 22) if sm else y + self.PAD
        d.rect(d.M, P0, d.W - 2 * d.M, P1 - P0, "#FBFBF9", "#D3D1C7", sw=1, rx=16)
        for i, (name, tagline, ramp) in enumerate(self.cols_def):
            f, s, tc, sc = RAMPS[ramp]
            d.rect(self.cx[i], head_y, CW, 62, f, s, sw=1.6, rx=10)
            d.text(self.cx[i] + CW / 2, head_y + 28, name, 19, tc, 600, "middle")
            if tagline:
                d.text(self.cx[i] + CW / 2, head_y + 50, tagline, 13.5, sc, 400, "middle")
        prev_bottom = [None] * n   # 每列上一行最后一个框的底边 y
        prev_span = None
        for r, (ry, rh) in zip(self.rows, lay):
            if r["sub"]:
                d.text(self.LX, ry + rh / 2 - 3, r["label"], 14.5, INK, 600)
                d.text(self.LX, ry + rh / 2 + 16, r["sub"], 12.5, MUTED)
            else:
                d.text(self.LX, ry + rh / 2 + 5, r["label"], 14.5, INK, 600)
            if r["t"] == "states":
                for i, c in enumerate(r["cells"]):
                    if c:
                        d.block(self.cx[i] + 40, ry, CW - 80, rh, c["title"], c.get("sub"),
                                c.get("ramp") or self.cols_def[i][2], "outline", ts=16, ss=13)
                prev_bottom, prev_span = [None] * n, None
                continue
            if r["t"] == "span":
                x0, x1 = self.cx[0], self.cx[-1] + CW
                d.rect(x0, ry, x1 - x0, rh, "#FFFFFF", MUTED, sw=1.2)
                d.text((x0 + x1) / 2, ry + 32, r["text"], 16.5, INK, 600, "middle")
                prev_bottom, prev_span = [ry + rh] * n, True
                continue
            sh = r["slot_h"]
            for i, col in enumerate(r["cells"]):
                x, ramp = self.cx[i], None
                yy, first_top, last_bottom = ry, None, None
                inner = (r.get("inner") or [None] * n)[i] or []
                for c in col:
                    if c["kind"] == "text":
                        tone = c.get("tone")
                        tcol, tw = TONES[tone] if tone in TONES else (tone, 400)
                        d.text(x + CW / 2, ry + rh / 2 + 5, c["text"], 15, tcol, tw, "middle")
                items = [c for c in col if c["kind"] != "text"]
                for k, c in enumerate(items):
                    hh = sh * c.get("span", 1) + 34 * (c.get("span", 1) - 1)
                    ramp = c.get("ramp") or self.cols_def[i][2]
                    if c["kind"] == "box":
                        d.block(x, yy, CW, hh, c["title"], c.get("sub"), ramp, c["style"], ts=15.5, ss=13)
                        if first_top is None:
                            first_top = yy
                        last_bottom = yy + hh
                    else:
                        _, stroke, _, sc = RAMPS[ramp]
                        d.rect(x, yy, CW, hh, "none", stroke, sw=1, dash="5 4")
                        m = yy + hh / 2 - 11 * (len(c["lines"]) - 1)
                        for j, s in enumerate(c["lines"]):
                            d.text(x + CW / 2, m + 5 + 22 * j, s, 15, sc, 600, "middle")
                    if k + 1 < len(items) and c["kind"] == "box" and items[k + 1]["kind"] == "box":
                        lk = inner[k] if k < len(inner) else "flow"
                        kind, lab = (lk, None) if isinstance(lk, str) or lk is None else lk
                        if kind:
                            d.arrow(x + CW / 2, yy + hh + 2, x + CW / 2, yy + hh + 34 - 5, kind, lab)
                    yy += hh + 34
                # 与上一行之间的箭头
                lk = (r.get("links") or ["flow"] * n)[i]
                kind, lab = (lk, None) if isinstance(lk, str) or lk is None else lk
                if kind and first_top is not None and col and col[0]["kind"] == "box" and prev_bottom[i] is not None:
                    d.arrow(x + CW / 2, prev_bottom[i] + 2, x + CW / 2, first_top - 5, kind, lab)
                prev_bottom[i] = last_bottom
        if sm:
            title, rows = sm
            ty = tab_y
            d.o.append(f'<line x1="{d.M + 20}" y1="{ty - 20}" x2="{d.W - d.M - 20}" y2="{ty - 20}" '
                       f'stroke="#D3D1C7" stroke-width="1"/>')
            d.text(self.LX, ty + 14, title, 16.5, INK, 600)
            ty += 32
            for k, (lab, cells) in enumerate(rows):
                yy = ty + k * 44
                if k % 2 == 0:
                    d.rect(d.M + 14, yy, d.W - 2 * d.M - 28, 44, "#F1EFE8", "none", sw=0, rx=6)
                d.text(self.LX, yy + 28, lab, 14.5, INK, 600)
                for i, cell in enumerate(cells):
                    txt, tone = (cell, None) if isinstance(cell, str) else cell
                    col, wgt = TONES[tone]
                    d.text(self.cx[i] + CW / 2, yy + 28, txt, 15, col, wgt, "middle")
        d.y = P1


# ------------------------------------------------------------------ PNG 导出
def export_png(svg_path, png_path, scale=2):
    """依次尝试 playwright(chromium) → cairosvg → rsvg-convert → inkscape。需要系统装有中文字体。"""
    try:
        import asyncio
        from playwright.async_api import async_playwright
        src = open(svg_path, encoding="utf-8").read()
        w = int(re.search(r'width="(\d+)"', src).group(1))
        h = int(re.search(r'height="(\d+)"', src).group(1))

        async def run():
            async with async_playwright() as p:
                b = await p.chromium.launch()
                pg = await b.new_page(viewport={"width": w, "height": h}, device_scale_factor=scale)
                await pg.set_content(f'<html><body style="margin:0">{src}</body></html>')
                await pg.wait_for_timeout(300)
                await pg.screenshot(path=png_path, timeout=120000)
                await b.close()
        asyncio.run(run())
        return png_path
    except Exception as e:
        err = e
    try:
        import cairosvg
        cairosvg.svg2png(url=svg_path, write_to=png_path, scale=scale)
        return png_path
    except Exception:
        pass
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-z", str(scale), "-o", png_path, svg_path], check=True)
        return png_path
    if shutil.which("inkscape"):
        subprocess.run(["inkscape", svg_path, "--export-type=png", f"--export-filename={png_path}",
                        f"--export-dpi={96 * scale}"], check=True)
        return png_path
    return f"未导出（没有可用的渲染器：{err}）。可 pip install playwright && playwright install chromium，或 pip install cairosvg"
