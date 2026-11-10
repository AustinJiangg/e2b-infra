"""画空闲沙箱内存实测的展示图。数字全部从本目录下的 report_n*.txt 解析，不手抄。

    python3 draw_idle_mem.py

输出 idle_sandbox_mem.html（排版源文件，浏览器可直接打开）和 idle_sandbox_mem.png（2 倍图）。
PNG 用 node + playwright 的 Chromium 截图，没有就只出 HTML。只依赖 Python 标准库。
计算口径见 ../空闲沙箱内存实测分析.md §2。
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
R1 = os.path.join(HERE, "20261110_base_2u2g_r1")
R2 = os.path.join(HERE, "20261110_base_2u2g_r2")


def parse(path):
    """返回 ({行说明: (before, T1, T2)}, [T2 时刻每个 firecracker 的客户机大页])"""
    rows, per_fc, sect = {}, [], None
    for line in open(path, encoding="utf-8"):
        if line.startswith("----- "):
            sect = line
        if "|" in line and not line.lstrip().startswith("before"):
            parts = line.rstrip("\n").split("|")
            nums = parts[0].split()
            try:
                rows[parts[-1].strip()] = tuple(float(x) for x in nums[:3])
            except ValueError:
                pass
        m = re.search(r"客户机大页=\s*([\d.]+)", line)
        if m and sect and "T2" in sect:
            per_fc.append(float(m.group(1)))
    return rows, per_fc


def d2(rows, key):
    """T2 − 基线"""
    for k, v in rows.items():
        if k.startswith(key):
            return v[2] - v[0]
    raise KeyError(key)


def d1(rows, key):
    for k, v in rows.items():
        if k.startswith(key):
            return v[1] - v[0]
    raise KeyError(key)


def fit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    s = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    return s, my - s * mx


r1 = {n: parse(os.path.join(R1, f"report_n{n}.txt")) for n in (1, 10)}
r2 = {n: parse(os.path.join(R2, f"report_n{n}.txt")) for n in (1, 10, 50)}
NS = [1, 10, 50]


def comp(n):
    """第二轮某个 N 的 T2 增量拆分：大页 / anon / 沙箱目录文件页 / 其他 page cache / 其他"""
    rows = r2[n][0]
    hp = d2(rows, "① 客户机内存")
    cur, anon, file_ = d2(rows, "orch cgroup memory.current"), d2(rows, "其中 anon"), d2(rows, "其中 file")
    ofile = d2(rows, "其中 RssFile")
    return [hp, anon, ofile, file_ - ofile, cur - anon - file_]


tot = {n: comp(n) for n in NS}
per = {n: [v / n for v in tot[n]] for n in NS}
slope = [fit(NS, [tot[n][i] for n in NS])[0] for i in range(5)]
cg_slope, cg_icpt = fit(NS, [sum(tot[n][1:]) for n in NS])
hp_slope = slope[0]
cg_resid = max(abs(sum(tot[n][1:]) - (cg_slope * n + cg_icpt)) for n in NS)

# 容量：基线取第二轮 N=50 的 before（最接近大规模时的状态）
b50 = r2[50][0]
cg_base = b50["orch cgroup memory.current"][0]
cg_base_anon_other = b50["其中 anon"][0] + (cg_base - b50["其中 anon"][0] - b50["其中 file(page cache)"][0])
CG_MAX = 32768.0
HP_FREE_MIB = 200026 * 2
cap_reclaim = int((CG_MAX - cg_base) / cg_slope)
cap_own = int((CG_MAX - cg_base_anon_other) / cg_slope)
cap_anon = int((CG_MAX - cg_base_anon_other) / (slope[1] + slope[4]))
cap_hp_idle = int(HP_FREE_MIB / hp_slope)
cap_hp_full = int(HP_FREE_MIB / 2048)
total_slope = hp_slope + cg_slope

all_fc = r1[1][1] + r1[10][1] + r2[1][1] + r2[10][1] + r2[50][1]
idle_growth = [d2(r2[n][0], "① 客户机内存") / n - d1(r2[n][0], "① 客户机内存") / n for n in NS]

# ---------------------------------------------------------------- 画图
# 用 HTML + CSS 排版、手写 SVG 画图表，再用 Chromium 截成 2 倍 PNG。不依赖任何画图库。
import math  # noqa: E402
import random  # noqa: E402
from html import escape  # noqa: E402

CAP_GIB = CG_MAX / 1024
OLD_CACHE = cg_base - cg_base_anon_other          # N=50 基线里可回收的旧 page cache（MiB）

C = dict(  # 配色：绿 = 大页池这本账；靛蓝一族 = orchestrator 配额这本账；橙/红 = 风险
    ink="#1B1A17", sub="#55524C", muted="#8C877F", line="#E6E2DA", grid="#EFECE6",
    vm="#12A08A", vm_lt="#DDF3EE", vm_dk="#0B6B5C",
    q1="#3B46B8", q2="#6873E3", q3="#A9B0F2", q4="#CDD1F7", q_lt="#ECEEFD", q_dk="#28308A",
    warn="#E59A2B", warn_lt="#FCF1DE", bad="#D9472F", bad_lt="#FBE6E1",
)

# ---- 方块图：1 格 = 2 MiB
CELL, GAP = 10, 2.5


def waffle(cols, cells, title=None):
    """cells: 每格的颜色列表（None = 空格，只画描边）"""
    rows = (len(cells) + cols - 1) // cols
    w, h = cols * (CELL + GAP) - GAP, rows * (CELL + GAP) - GAP
    o = [f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">']
    for i, c in enumerate(cells):
        x, y = (i % cols) * (CELL + GAP), (i // cols) * (CELL + GAP)
        if c:
            o.append(f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="2" fill="{c}"/>')
        else:
            o.append(f'<rect x="{x + .5}" y="{y + .5}" width="{CELL - 1}" height="{CELL - 1}" rx="2" '
                     f'fill="#FFFFFF" stroke="#E3DFD7"/>')
    o.append("</svg>")
    return "".join(o)


hp_cells = round(hp_slope / 2)
idx = set(random.Random(20261110).sample(range(1024), hp_cells))
vm_svg = waffle(32, [C["vm"] if i in idx else None for i in range(1024)])

# 配额这本账：按最大余数法把各段分到 round(cg_slope/2) 格
Q = [("沙箱运行时文件", "orchestrator 为每个沙箱建的一组文件（快照内存映射等），映射进内存", slope[2], C["q1"]),
     ("磁盘读写缓存", "读写沙箱磁盘镜像时，内核顺手留下的文件缓存", slope[3], C["q2"]),
     ("进程自身数据", "orchestrator 管理这个沙箱的状态（约 20）、虚拟机进程本身（约 2）", slope[1], C["q3"]),
     ("内核对象", "其余零碎的内核数据结构", slope[4], C["q4"])]
n_q = round(cg_slope / 2)
raw = [v / 2 for _, _, v, _ in Q]
cnt = [int(r) for r in raw]
for i in sorted(range(len(raw)), key=lambda i: raw[i] - cnt[i], reverse=True)[:n_q - sum(cnt)]:
    cnt[i] += 1
q_cells = sum(([c] * k for (_, _, _, c), k in zip(Q, cnt)), [])
q_svg = waffle(16, q_cells)
q_list = "".join(
    f'<div class="qi"><span class="sw" style="background:{c}"></span><div><b>{escape(nm)}'
    f'<em>{v:.0f} MiB</em></b><p>{escape(ds)}</p></div></div>' for nm, ds, v, c in Q)


# ---- 折线图
def chart(w, h, xlim, ylim, xt, yt, body, xlab, ylab, pad=(56, 24, 40, 52)):
    """body(X, Y, box) -> svg 片段。pad = 左、右、上、下"""
    L, R, T, B = pad
    x0, x1, y0, y1 = L, w - R, h - B, T
    X = lambda v: x0 + (v - xlim[0]) / (xlim[1] - xlim[0]) * (x1 - x0)
    Y = lambda v: y0 - (v - ylim[0]) / (ylim[1] - ylim[0]) * (y0 - y1)
    o = [f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" class="ch">']
    for t in yt:
        o.append(f'<line x1="{x0}" x2="{x1}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="{C["grid"]}"/>'
                 f'<text x="{x0 - 12}" y="{Y(t) + 4.5:.1f}" class="tk" text-anchor="end">{t:g}</text>')
    for t in xt:
        o.append(f'<text x="{X(t):.1f}" y="{y0 + 24}" class="tk" text-anchor="middle">{t:g}</text>')
    o.append(body(X, Y, (x0, x1, y0, y1)))
    o.append(f'<line x1="{x0}" x2="{x1}" y1="{y0}" y2="{y0}" stroke="#CFCAC0" stroke-width="1.2"/>')
    o.append(f'<text x="{(x0 + x1) / 2:.0f}" y="{h - 8}" class="al" text-anchor="middle">{xlab}</text>')
    o.append(f'<text x="{x0 - 12}" y="{y1 - 18}" class="al" text-anchor="end">{ylab}</text>')
    o.append("</svg>")
    return "".join(o)


def dot(x, y, c, r=6.5):
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{c}" stroke="#FFFFFF" stroke-width="2.5"/>'


def lbl(x, y, s, c, cls="ll", anchor="start"):
    return f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" fill="{c}" text-anchor="{anchor}">{s}</text>'


_, hp_icpt = fit(NS, [tot[n][0] for n in NS])


def fit_body(X, Y, box):
    o = []
    for c, s_, b_, key, name in [(C["q1"], cg_slope, cg_icpt, "cg", "账本 B · 配额"),
                                 (C["vm"], hp_slope, hp_icpt, "hp", "账本 A · 大页池")]:
        o.append(f'<line x1="{X(0):.1f}" y1="{Y(b_ / 1024):.1f}" x2="{X(50):.1f}" y2="{Y((b_ + 50 * s_) / 1024):.1f}" '
                 f'stroke="{c}" stroke-width="2.6" stroke-linecap="round"/>')
        for n in NS:
            v = tot[n][0] if key == "hp" else sum(tot[n][1:])
            o.append(dot(X(n), Y(v / 1024), c))
        ye = Y((b_ + 50 * s_) / 1024)
        o.append(lbl(X(50) + 16, ye - 2, name, c))
        o.append(lbl(X(50) + 16, ye + 19, f"斜率 {s_:.1f} MiB / 个", C["sub"], "ls"))
    return "".join(o)


fit_svg = chart(700, 380, (0, 70), (0, 12), [0, 10, 20, 30, 40, 50], [0, 3, 6, 9, 12], fit_body,
                "同时空闲的沙箱数 N", "GiB")

XMAX, YMAX = 250, 48


def cap_body(X, Y, box):
    x0, x1, y0, y1 = box
    xa, xb = X(cap_reclaim), X(cap_own)
    o = [f'<rect x="{xa:.1f}" y="{y1}" width="{xb - xa:.1f}" height="{y0 - y1}" fill="{C["warn_lt"]}"/>',
         f'<rect x="{xb:.1f}" y="{y1}" width="{x1 - xb:.1f}" height="{y0 - y1}" fill="{C["bad_lt"]}"/>',
         lbl((x0 + xa) / 2, y1 + 24, "宽裕", C["vm_dk"], "zl", "middle"),
         lbl((xa + xb) / 2, y1 + 24, "挤掉旧缓存", "#9A5B06", "zl", "middle"),
         lbl((xb + x1) / 2, Y(13), "回收沙箱在用的页", "#A3301C", "zl", "middle"),
         lbl((xb + x1) / 2, Y(13) + 20, "变慢，可能 OOM → api 503", "#A3301C", "ls", "middle"),
         f'<line x1="{x0}" x2="{x1}" y1="{Y(CAP_GIB):.1f}" y2="{Y(CAP_GIB):.1f}" stroke="{C["bad"]}" stroke-width="2"/>',
         lbl(x0 + 10, Y(CAP_GIB) - 9, f"配额上限 {CAP_GIB:.0f} GiB", C["bad"], "ll")]
    for base, c, name, n_lab, dy in [(cg_base, C["q1"], "全部用量（含旧缓存）", 6, -10),
                                     (cg_base_anon_other, C["q2"], "必须常驻的部分", 6, -10)]:
        nx = (CG_MAX - base) / cg_slope
        nend = min(XMAX, (YMAX * 1024 - base) / cg_slope)
        o.append(f'<line x1="{X(0):.1f}" y1="{Y(base / 1024):.1f}" x2="{X(nx):.1f}" y2="{Y(CAP_GIB):.1f}" '
                 f'stroke="{c}" stroke-width="2.8" stroke-linecap="round"/>')
        o.append(f'<line x1="{X(nx):.1f}" y1="{Y(CAP_GIB):.1f}" x2="{X(nend):.1f}" y2="{Y((base + cg_slope * nend) / 1024):.1f}" '
                 f'stroke="{c}" stroke-width="2" stroke-dasharray="2 6" stroke-linecap="round"/>')
        o.append(f'<line x1="{X(nx):.1f}" y1="{Y(CAP_GIB) + 8:.1f}" x2="{X(nx):.1f}" y2="{y0}" stroke="{c}" '
                 f'stroke-width="1" stroke-dasharray="3 3"/>')
        o.append(dot(X(nx), Y(CAP_GIB), c, 7))
        o.append(f'<rect x="{X(nx) - 22:.1f}" y="{y0 - 30}" width="44" height="22" rx="11" fill="{c}"/>'
                 + lbl(X(nx), y0 - 14.5, f"{int(nx)}", "#FFFFFF", "pill", "middle"))
        ang = math.degrees(math.atan2(Y(cg_slope / 1024) - Y(0), X(1) - X(0)))
        lx, ly = X(n_lab), Y((base + cg_slope * n_lab) / 1024)
        o.append(f'<text x="{lx:.1f}" y="{ly + dy:.1f}" class="ll" fill="{c}" '
                 f'transform="rotate({ang:.2f} {lx:.1f} {ly:.1f})">{name}</text>')
    return "".join(o)


cap_svg = chart(700, 380, (0, XMAX), (0, YMAX), [0, 50, 100, 150, 200, 250], [0, 8, 16, 24, 32, 40, 48],
                cap_body, "同时空闲的沙箱数", "GiB")

# ---- 页面
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{width:1600px;background:#F3F1EC;font-family:'Noto Sans CJK SC','Noto Sans SC','PingFang SC','Microsoft YaHei',sans-serif;
 color:%(ink)s;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
.page{padding:56px 56px 40px}
.eyebrow{font-size:14px;letter-spacing:.12em;color:%(muted)s;font-weight:500}
h1{font-size:40px;line-height:1.25;font-weight:700;margin:10px 0 0;letter-spacing:-.01em}
h1 .hl{color:%(q1)s} h1 .hb{color:%(bad)s}
.lede{font-size:16px;color:%(sub)s;margin-top:12px;line-height:1.6}
.stats{display:grid;grid-template-columns:repeat(4,1fr);margin-top:32px;background:#FFFFFF;border-radius:18px;
 box-shadow:0 1px 2px rgba(40,30,10,.05),0 10px 30px rgba(40,30,10,.05)}
.st{padding:24px 28px;border-left:1px solid %(line)s}.st:first-child{border-left:0}
.st .n{font-size:38px;font-weight:700;letter-spacing:-.02em;line-height:1.1}
.st .n small{font-size:18px;font-weight:600;margin-left:4px;color:%(sub)s}
.st .t{font-size:15px;font-weight:600;margin-top:8px}
.st .d{font-size:13px;color:%(muted)s;margin-top:4px;line-height:1.55}
.sec{margin-top:44px}
.sh{display:flex;align-items:baseline;gap:14px}
.sh .no{font-size:13px;font-weight:700;color:#FFFFFF;background:%(ink)s;border-radius:999px;padding:3px 11px;
 position:relative;top:-3px}
.sh h2{font-size:24px;font-weight:700}
.sh p{font-size:15px;color:%(muted)s}
.ledgers{display:grid;grid-template-columns:1fr 64px 1fr;margin-top:20px;align-items:stretch}
.plus{display:flex;align-items:center;justify-content:center;font-size:40px;color:#B9B3A8;font-weight:300}
.card{background:#FFFFFF;border-radius:18px;padding:26px 28px 0;overflow:hidden;display:flex;flex-direction:column;
 box-shadow:0 1px 2px rgba(40,30,10,.05),0 10px 30px rgba(40,30,10,.05)}
.tag{align-self:flex-start;font-size:12.5px;font-weight:700;letter-spacing:.06em;border-radius:6px;padding:3px 9px}
.card h3{font-size:21px;font-weight:700;margin-top:12px;line-height:1.35}
.card h3 .big{font-size:30px}
.card .ex{font-size:14px;color:%(sub)s;line-height:1.65;margin-top:8px}
.body{display:flex;gap:26px;margin-top:22px;align-items:flex-start}
.key{font-size:13px;color:%(sub)s;line-height:1.8;white-space:nowrap}
.key .sw{margin-right:7px}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;flex:none}
.sw.o{background:#FFFFFF;border:1px solid #D9D4CA}
.qi{display:flex;gap:10px;margin-bottom:14px}.qi .sw{margin-top:5px}
.qi b{font-size:15px;font-weight:600;display:block}.qi b em{font-style:normal;color:%(q1)s;margin-left:8px}
.qi p{font-size:13px;color:%(muted)s;line-height:1.55;margin-top:2px}
.foot{margin:24px -28px 0;padding:16px 28px;display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:auto}
.foot div{font-size:12.5px;color:%(sub)s}.foot b{display:block;font-size:20px;font-weight:700;margin-top:2px}
.spacer{height:24px}
.qbar{margin-top:22px}.qbar .lab{font-size:13.5px;font-weight:600;margin-bottom:8px}
.qbar .bar{display:flex;height:30px;border-radius:7px;overflow:hidden;font-size:12.5px;font-weight:600}
.qbar .bar div{display:flex;align-items:center;justify-content:center;white-space:nowrap}
.qbar .cap{display:flex;justify-content:space-between;font-size:12.5px;color:%(muted)s;margin-top:7px}
.qbar .cap b{color:%(ink)s;font-weight:600}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:20px}
.charts .card{padding-bottom:18px}
.charts h3{font-size:19px;margin-top:0}
.ch .tk{font-size:13px;fill:%(muted)s}.ch .al{font-size:13px;fill:%(sub)s}
.ch .ll{font-size:14.5px;font-weight:700}.ch .ls{font-size:13px}.ch .zl{font-size:14px;font-weight:700}
.ch .pill{font-size:13px;font-weight:700}
.rec{margin-top:28px;background:#1E2140;color:#FFFFFF;border-radius:18px;padding:26px 32px;display:flex;gap:36px;align-items:center}
.rec .k{font-size:13px;letter-spacing:.12em;color:#A9B0F2;font-weight:700;white-space:nowrap}
.rec .f{font-size:28px;font-weight:700;white-space:nowrap}.rec .f span{color:#A9B0F2}
.rec .x{font-size:14px;color:#C9CCE6;line-height:1.65}
.note{font-size:12.5px;color:%(muted)s;line-height:1.7;margin-top:22px}
""" % C

hp_free_gib = HP_FREE_MIB / 1024
html = f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body><div class="page">
<div class="eyebrow">E2B 自托管 · 空闲沙箱内存实测 · 2026-11-10</div>
<h1>每个空闲沙箱让宿主机多用约 {total_slope:.0f} MiB，<br>能开多少个，先由 <span class="hl">orchestrator 的 {CAP_GIB:.0f} GiB 配额</span>决定</h1>
<div class="lede">base 模板（ubuntu 22.04 + envd，2 核，memory_mb = 2048）。宿主机 2.4 TiB 内存。分两轮测了 N = 1、10、50 个同时空闲的沙箱，每次空闲 300 s，全部在宿主机侧读数。</div>

<div class="stats">
 <div class="st"><div class="n">{total_slope:.0f}<small>MiB</small></div><div class="t">每多一个空闲沙箱</div>
  <div class="d">{hp_slope:.0f} 大页 + {cg_slope:.0f} 配额，三次测量偏差 ≤ {cg_resid:.0f} MiB</div></div>
 <div class="st"><div class="n" style="color:{C['vm']}">{hp_slope / 2048 * 100:.1f}<small>%</small></div><div class="t">2048 MiB 里真正用到的</div>
  <div class="d">memory_mb 是上限，不是占用</div></div>
 <div class="st"><div class="n" style="color:{C['warn']}">≈ {cap_reclaim}<small>个</small></div><div class="t">配额开始吃紧</div>
  <div class="d">超过约 {cap_own} 个可能 OOM</div></div>
 <div class="st"><div class="n" style="color:{C['vm']}">{cap_hp_idle:,}<small>个</small></div><div class="t">大页池能放下的空闲沙箱</div>
  <div class="d">每个都用满 2048 MiB 也能放 {cap_hp_full} 个</div></div>
</div>

<div class="sec"><div class="sh"><span class="no">1</span><h2>一个空闲沙箱的内存，记在宿主机的两本账上</h2>
 <p>图中 1 格 = 2 MiB，两边格子大小相同，可以直接比</p></div>
<div class="ledgers">
 <div class="card">
  <span class="tag" style="background:{C['vm_lt']};color:{C['vm_dk']}">账本 A · 大页池</span>
  <h3>虚拟机内存：<span class="big" style="color:{C['vm']}">{hp_slope:.0f}</span> MiB</h3>
  <div class="ex">宿主机开机时预留了一块专给虚拟机用的内存（2 MiB 一页的大页）。沙箱从快照恢复后，虚拟机看到 2048 MiB，
   但只有它真正读写过的页才会从这里分走一页。</div>
  <div class="body">{vm_svg}
   <div class="key"><div><span class="sw" style="background:{C['vm']}"></span>真正占用：{hp_cells} 页 ≈ {hp_slope:.0f} MiB</div>
    <div><span class="sw o"></span>没碰过：只是地址，不占内存</div>
    <div style="margin-top:14px;color:{C['muted']}">{len(all_fc)} 个沙箱逐个看，<br>都在 {min(all_fc):.0f}–{max(all_fc):.0f} MiB；<br>关闭后立即全部归还。</div></div>
  </div>
  <div class="foot" style="background:{C['vm_lt']}">
   <div>这本账的空闲额度<b style="color:{C['vm_dk']}">{hp_free_gib:.0f} GiB</b></div>
   <div>每个空闲沙箱<b style="color:{C['vm_dk']}">{hp_slope:.0f} MiB</b></div>
   <div>够放<b style="color:{C['vm_dk']}">≈ {cap_hp_idle:,} 个</b></div></div>
 </div>
 <div class="plus">+</div>
 <div class="card">
  <span class="tag" style="background:{C['q_lt']};color:{C['q_dk']}">账本 B · orchestrator 的内存配额</span>
  <h3>宿主机进程为它多花：<span class="big" style="color:{C['q1']}">{cg_slope:.0f}</span> MiB</h3>
  <div class="ex">orchestrator 是管理沙箱的后台服务。它和所有虚拟机进程被 Nomad 放在同一个 cgroup 里，
   普通内存合计不能超过 {CAP_GIB:.0f} GiB。大页不算在这里面，下面这些都算。</div>
  <div class="body">{q_svg}<div style="flex:1">{q_list}</div></div>
  <div class="qbar"><div class="lab">这 {CAP_GIB:.0f} GiB 测前的样子，以及还能放几个</div>
   <div class="bar">
    <div style="width:{max(cg_base_anon_other / CG_MAX * 100, .6):.2f}%;background:{C['q1']}"></div>
    <div style="width:{OLD_CACHE / CG_MAX * 100:.2f}%;background:repeating-linear-gradient(135deg,{C['q_lt']} 0 6px,#DCDFFA 6px 12px);color:{C['q_dk']}">旧文件缓存 {OLD_CACHE / 1024:.1f} GiB（可回收）</div>
    <div style="flex:1;background:#FFFFFF;border:1.5px dashed {C['q3']};border-left:0;border-radius:0 7px 7px 0;color:{C['sub']}">空闲 {(CG_MAX - cg_base) / 1024:.1f} GiB</div>
   </div>
   <div class="cap"><span><span class="sw" style="background:{C['q1']};margin-right:6px"></span>必须常驻 {cg_base_anon_other / 1024:.2f} GiB</span>
    <span>空闲 ÷ {cg_slope:.0f} MiB ≈ <b>{cap_reclaim} 个</b>；连旧缓存一起 ≈ <b>{cap_own} 个</b></span></div>
  </div>
  <div class="foot" style="background:{C['q_lt']}">
   <div>上限 {CAP_GIB:.0f} GiB，测前已用<b style="color:{C['q_dk']}">{cg_base / 1024:.1f} GiB</b></div>
   <div>每个空闲沙箱<b style="color:{C['q_dk']}">{cg_slope:.0f} MiB</b></div>
   <div>约多少个开始吃紧<b style="color:{C['bad']}">≈ {cap_reclaim} 个</b></div></div>
 </div>
</div></div>

<div class="sec"><div class="sh"><span class="no">2</span><h2>数字怎么来的，以及按这个速度多少个会出问题</h2></div>
<div class="charts">
 <div class="card"><h3>增量随沙箱数严格线性，所以斜率就是每个沙箱的成本</h3>
  <div class="ex">每个点是一次实测（空闲 300 s 时相对测前的增量），直线是最小二乘拟合。截距只有 {cg_icpt:.0f} MiB，说明几乎没有一次性开销。</div>
  {fit_svg}</div>
 <div class="card"><h3>沙箱越多，账本 B 越接近 {CAP_GIB:.0f} GiB 上限</h3>
  <div class="ex">测前账本 B 已用 {cg_base / 1024:.1f} GiB，其中 {OLD_CACHE / 1024:.1f} GiB 是可回收的旧文件缓存。
   蓝线到上限后内核开始挤旧缓存；紫线到上限后，只能回收沙箱自己在用的页。</div>
  {cap_svg}</div>
</div></div>

<div class="rec"><div class="k">建议</div>
 <div class="f">memory ≥ {CAP_GIB:.0f} GiB <span>+ X × {cg_slope / 1024:.2f} GiB</span></div>
 <div class="x">X 是想同时跑的空闲沙箱数，例如 500 个约 {CAP_GIB + 500 * cg_slope / 1024:.0f} GiB。改的是 template-manager.hcl 的 memory，
  {CAP_GIB:.0f} GiB 是构建模板本身需要的；有负载的沙箱还要更多。</div></div>

<div class="note">口径：账本 B 以 orchestrator 所在 cgroup 的 memory.current 增量为准，按 memory.stat 拆成匿名内存和文件缓存，余下记为内核对象；
“磁盘读写缓存”的来源是推断，未逐项确认。全机 MemTotal−MemAvailable 受同机其他负载干扰（1 个沙箱时 Cached 就涨了 6.4 GiB），不用于计算。
关闭沙箱后，orchestrator 堆几分钟内回落，未见泄漏。原始报告：benchmark/mem_results/20261110_base_2u2g_r1、_r2；推导见 空闲沙箱内存实测分析.md。</div>
</div></body></html>"""

out_html = os.path.join(HERE, "idle_sandbox_mem.html")
open(out_html, "w", encoding="utf-8").write(html)
print("HTML:", out_html)


# 计算结果打印出来，方便和文档核对
print(f"hp slope {hp_slope:.1f}; cg slope {cg_slope:.1f} icpt {cg_icpt:.1f} resid {cg_resid:.1f}")
print("slopes [hp, anon, orchfile, otherfile, other]:", [round(x, 1) for x in slope])
for n in NS:
    print(f"N={n} per:", [round(x, 1) for x in per[n]], "sum", round(sum(per[n]), 1))
print(f"cap: hp_idle {cap_hp_idle} hp_full {cap_hp_full} cg_reclaim {cap_reclaim} cg_own {cap_own} cg_anon {cap_anon}")
print(f"cg_base {cg_base} anon+other base {cg_base_anon_other:.1f}; fc n={len(all_fc)} range {min(all_fc)}-{max(all_fc)}")
print("idle growth hp per sandbox T1->T2:", [round(x, 1) for x in idle_growth])


# ---- 截图成 PNG
JS = """const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();
const p=await b.newPage({viewport:{width:1600,height:1000},deviceScaleFactor:2});
await p.goto('file://'+process.argv[1]);await p.waitForTimeout(300);
await p.screenshot({path:process.argv[2],fullPage:true});await b.close();})();"""
if shutil.which("node"):
    env = dict(os.environ)
    try:
        env.setdefault("NODE_PATH", subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip())
    except OSError:
        pass
    png = os.path.join(HERE, "idle_sandbox_mem.png")
    r = subprocess.run(["node", "-e", JS, out_html, png], env=env)
    print("PNG:", png) if r.returncode == 0 else print("PNG 导出失败（需要 node 的 playwright），只生成了 HTML")
