"""画空闲沙箱内存实测的展示图（SVG + 2 倍 PNG）。数字全部从本目录下的 report_n*.txt 解析，不手抄。

依赖 explainer-chart skill 的 kit（hdbss-dirty-tracking 仓库 .claude/skills/explainer-chart/scripts）：
    EXPLAINER_CHART_KIT=/path/to/explainer-chart/scripts python3 draw_idle_mem.py
导出 PNG 需要 node + playwright（chromium），没有就只出 SVG。
计算口径见 ../空闲沙箱内存实测分析.md §2。
"""
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.environ.get(
    "EXPLAINER_CHART_KIT",
    "/home/user/hdbss-dirty-tracking/.claude/skills/explainer-chart/scripts"))
from chart_kit import Chart   # noqa: E402
from diagram_kit import Step  # noqa: E402

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
# 版式：结论卡片 → ① 一个沙箱的 350 MiB 拆在哪两本账上 → ② 左：为什么能用斜率（线性拟合）；右：沙箱数增加时 cgroup 何时撞上限
from chart_kit import GRID, AXIS, col, nice_ticks  # noqa: E402
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w, wrap  # noqa: E402

d = Chart(width=1600)
W, M = d.W, d.M
CW = W - 2 * M
cap_gib = CG_MAX / 1024
d.header(f"空闲 2U2G 沙箱：每个让宿主机多用约 {total_slope:.0f} MiB，并发先撞上 orchestrator 的 {cap_gib:.0f} GiB 内存上限",
         "base 模板（ubuntu 22.04 + envd，cpu_count=2，memory_mb=2048）· 宿主机 2.4 TiB 内存，静态大页池 200064 × 2 MiB"
         " · 2026-11-10 两轮实测，N = 1、10、50，每次空闲 300 s，全部在宿主机侧读数")


def paras(x, y, w, s, size, color, weight=400, lh=None, anchor="start"):
    lh = lh or size + 7
    ls = wrap(s, w, size)
    for k, t in enumerate(ls):
        d.text(x, y + k * lh, t, size, color, weight, anchor)
    return len(ls) * lh


# ---- 结论卡片
kpis = [
    (f"{total_slope:.0f} MiB", "每个空闲沙箱的边际内存",
     f"大页 {hp_slope:.0f} + cgroup 普通内存 {cg_slope:.0f}；三个 N 拟合，偏差 ≤ {cg_resid:.0f} MiB", "teal"),
    (f"{hp_slope:.0f} / 2048", "客户机内存真正落地的 MiB",
     "memory_mb 只是上限；按需缺页，不预留", "teal"),
    (f"≈ {cap_reclaim} 个", f"orchestrator cgroup 到 {cap_gib:.0f} GiB",
     f"开始回收旧缓存；超过约 {cap_own} 个就要回收沙箱自己在用的页", "coral"),
    (f"{cap_gib:.0f} + {cg_slope / 1024:.2f}·X GiB", "template-manager 的 memory 建议",
     "X = 目标空闲沙箱数；有负载的沙箱还要更多", "coral"),
]
gap = 20
kw = (CW - 3 * gap) / 4
ky = d.y + 30
kh = 138
for i, (big, lab, sub, ramp) in enumerate(kpis):
    f, s, tc, sc = RAMPS[ramp]
    x = M + i * (kw + gap)
    d.rect(x, ky, kw, kh, f, s, sw=1, rx=10)
    d.text(x + 20, ky + 46, big, 30, tc, 700)
    d.text(x + 20, ky + 76, lab, 15, INK, 600)
    paras(x + 20, ky + 100, kw - 40, sub, 13, sc, lh=19)
d.y = ky + kh

# ---- ① 拆解条
d.section(1, f"一个空闲沙箱的 {total_slope:.0f} MiB 记在两本账上", note="每段都是对 N = 1、10、50 拟合出的斜率")
SEG = [  # (值, 色阶, 名称, 说明)
    (slope[0], "teal", "客户机内存（大页）",
     f"firecracker smaps 直接读到；{len(all_fc)} 个沙箱都在 {min(all_fc):.0f}–{max(all_fc):.0f}"),
    (slope[2], "purple", "沙箱目录文件页",
     "orchestrator mmap 的 /orchestrator/sandbox 下每个沙箱的文件，与目录大小一致"),
    (slope[3], "amber", "其他 page cache",
     "非 mmap 的文件缓存，推测来自 rootfs 读写，未逐项确认"),
    (slope[1], "blue", "匿名内存",
     "orchestrator 堆约 20、firecracker 约 2；关闭后几分钟回落"),
    (slope[4], "gray", "其他",
     "内核对象等，= current − anon − file"),
]
sc_ = CW / sum(v for v, *_ in SEG)
bx, by, bh = M, d.y + 78, 58
# 上方括号：两本账
def bracket(x0, x1, y, title, note, ramp):
    c = col(ramp)
    d.line(x0 + 1, y, x1 - 1, y, c, 2)
    d.line(x0 + 1, y, x0 + 1, y + 10, c, 2)
    d.line(x1 - 1, y, x1 - 1, y + 10, c, 2)
    t = title
    d.text(x0 + 2, y - 12, t, 15.5, RAMPS[ramp][2], 600)
    d.text(x0 + 2 + text_w(t, 15.5) + 14, y - 12, note, 13.5, MUTED)
hp_w = slope[0] * sc_
bracket(bx, bx + hp_w, by - 14, f"大页池 · {slope[0]:.0f} MiB", "不计入任何 cgroup", "teal")
bracket(bx + hp_w + 6, bx + CW, by - 14, f"orchestrator 所在的 Nomad cgroup · {cg_slope:.0f} MiB",
        f"计入 memory.max = {cap_gib:.0f} GiB，超了会 OOM", "blue")
x = bx
for v, ramp, name, _ in SEG:
    f, s, tc, _ = RAMPS[ramp]
    w = v * sc_
    d.rect(x, by, w, bh, f, s, sw=1.2, rx=4)
    lab = f"{v:.0f}"
    if text_w(lab, 20) + 12 < w:
        d.text(x + w / 2, by + bh / 2 + 7, lab, 20, tc, 700, "middle")
    x += w
# 下方图例卡（颜色与上面对应，顺序相同）
cy = by + bh + 22
cw5 = (CW - 4 * 14) / 5
hmax = 0
for i, (v, ramp, name, note) in enumerate(SEG):
    f, s, tc, sc2 = RAMPS[ramp]
    x = M + i * (cw5 + 14)
    d.rect(x, cy, 6, 70, s, s, sw=0, rx=3)
    d.text(x + 18, cy + 17, f"{name}  {v:.1f} MiB", 14.5, tc, 600)
    hmax = max(hmax, 26 + paras(x + 18, cy + 41, cw5 - 24, note, 13, SUB, lh=19))
d.y = cy + max(hmax, 70)

# 小条：2048 地址空间里落地了多少
ay = d.y + 30
lab_w = 210
d.text(M, ay + 13, "客户机看到的 2048 MiB", 14.5, INK, 600)
d.text(M, ay + 32, "memory_mb = 2048", 12.5, MUTED)
aw = CW - lab_w - 10
ax = M + lab_w
hpw = aw * slope[0] / 2048
d.rect(ax, ay, hpw, 30, *RAMPS["teal"][:2], sw=1.2, rx=4)
d.text(ax + hpw / 2, ay + 20, f"{slope[0]:.0f}", 14, RAMPS["teal"][2], 700, "middle")
d.rect(ax + hpw, ay, aw - hpw, 30, "#FFFFFF", AXIS, sw=1, rx=4, dash="5 4")
d.text(ax + hpw + (aw - hpw) / 2, ay + 20,
       f"{2048 - slope[0]:.0f} MiB 映射了但客户机没碰过，不占物理内存（HugePages_Rsvd 增量 = 0）",
       13.5, MUTED, 400, "middle")
d.y = ay + 30


# ---- ② 两张折线图
def axes(x, y, w, h, xlim, ylim, xticks, yticks, xfmt, yfmt, xtitle, ytitle):
    L, B = 54, 46          # 左侧刻度区、下方刻度区
    px0, px1, py0, py1 = x + L, x + w - 8, y + h - B, y + 10
    X = lambda v: px0 + (v - xlim[0]) / (xlim[1] - xlim[0]) * (px1 - px0)
    Y = lambda v: py0 - (v - ylim[0]) / (ylim[1] - ylim[0]) * (py0 - py1)
    for t in yticks:
        d.line(px0, Y(t), px1, Y(t), GRID)
        d.text(px0 - 10, Y(t) + 4.5, yfmt(t), 13, MUTED, 400, "end")
    for t in xticks:
        d.line(X(t), py0, X(t), py0 + 5, AXIS)
        d.text(X(t), py0 + 22, xfmt(t), 13, MUTED, 400, "middle")
    d.line(px0, py0, px1, py0, AXIS, 1.2)
    d.text(px0 - 10, py1 - 18, ytitle, 12.5, MUTED, 400, "end")
    d.text((px0 + px1) / 2, py0 + 42, xtitle, 13.5, SUB, 400, "middle")
    return X, Y, (px0, px1, py0, py1)


def panel_title(x, y, w, title, note):
    d.text(x, y, title, 17, INK, 600)
    return 10 + paras(x, y + 24, w, note, 13.5, MUTED, lh=20)


d.section(2, "为什么能按“每个沙箱”算，以及按这个速度多少个会出问题")
pw = (CW - 56) / 2
py = d.y + 34
lx, rx_ = M, M + pw + 56
th_l = panel_title(lx, py, pw, "增量随 N 严格线性：斜率 = 每多一个沙箱的成本",
                   f"每个点是一次实测（T2 时刻相对基线的增量）；直线是最小二乘拟合，截距 {cg_icpt:.0f} MiB ≈ 没有一次性开销")
th_r = panel_title(rx_, py, pw, f"沙箱越多，orchestrator cgroup 越接近 {cap_gib:.0f} GiB",
                   f"按 N=50 那次的基线：cgroup 里已有 {(cg_base - cg_base_anon_other) / 1024:.1f} GiB 可回收的旧缓存，"
                   f"{cg_base_anon_other / 1024:.2f} GiB 匿名内存")
gy = py + max(th_l, th_r) + 34
GH = 360

# 左：拟合
cg_tot = {n: sum(tot[n][1:]) / 1024 for n in NS}
hp_tot = {n: tot[n][0] / 1024 for n in NS}
X, Y, (px0, px1, py0, py1) = axes(lx, gy, pw, GH, (0, 72), (0, 12), [0, 10, 20, 30, 40, 50], [0, 3, 6, 9, 12],
                                  lambda v: f"{v:.0f}", lambda v: f"{v:.0f}", "空闲沙箱数 N", "GiB")
_, hp_icpt = fit(NS, [tot[n][0] for n in NS])
for ramp, pts, s_, b_, lab in [
        ("blue", cg_tot, cg_slope, cg_icpt, f"cgroup 普通内存：每个 {cg_slope:.1f} MiB"),
        ("teal", hp_tot, hp_slope, hp_icpt, f"大页：每个 {hp_slope:.1f} MiB")]:
    c = col(ramp)
    d.poly([(X(0), Y(b_ / 1024)), (X(50), Y((b_ + s_ * 50) / 1024))], c, 2.2)
    for n in NS:
        d.circle(X(n), Y(pts[n]), 6.5, c, "#FFFFFF", 2)
    yl = Y((b_ + s_ * 50) / 1024)
    d.text(X(50) + 14, yl + 5, lab.split("：")[0], 14, RAMPS[ramp][2], 600)
    d.text(X(50) + 14, yl + 24, lab.split("：")[1], 13, RAMPS[ramp][3])

# 右：容量
xmax, ymax = 250, 48
X, Y, (px0, px1, py0, py1) = axes(rx_, gy, pw, GH, (0, xmax), (0, ymax), [0, 50, 100, 150, 200, 250],
                                  [0, 8, 16, 24, 32, 40, 48], lambda v: f"{v:.0f}", lambda v: f"{v:.0f}",
                                  "空闲沙箱数", "GiB")
# 区域底色
d.rect(X(cap_reclaim), py1, X(cap_own) - X(cap_reclaim), py0 - py1, RAMPS["amber"][0], RAMPS["amber"][0], sw=0, rx=0)
d.rect(X(cap_own), py1, px1 - X(cap_own), py0 - py1, RAMPS["coral"][0], RAMPS["coral"][0], sw=0, rx=0)
d.text((px0 + X(cap_reclaim)) / 2, py1 + 22, "宽裕", 13.5, RAMPS["teal"][2], 600, "middle")
d.text((X(cap_reclaim) + X(cap_own)) / 2, py1 + 22, "挤掉旧缓存", 13.5, RAMPS["amber"][2], 600, "middle")
d.text((X(cap_own) + px1) / 2, Y(14), "回收沙箱在用的页", 13.5, RAMPS["coral"][2], 600, "middle")
d.text((X(cap_own) + px1) / 2, Y(14) + 19, "变慢，可能 OOM → api 503", 12.5, RAMPS["coral"][3], 400, "middle")
# 上限
d.line(px0, Y(cap_gib), px1, Y(cap_gib), "#A32D2D", 2)
d.text(px0 + 8, Y(cap_gib) - 8, f"memory.max = {cap_gib:.0f} GiB", 13, "#A32D2D", 600)


def demand(base, ramp, label, lab_n, dy):
    c = col(ramp)
    nx = (CG_MAX - base) / cg_slope
    d.poly([(X(0), Y(base / 1024)), (X(nx), Y(cap_gib))], c, 2.6)
    nend = min(xmax, (ymax * 1024 - base) / cg_slope)
    d.poly([(X(nx), Y(cap_gib)), (X(nend), Y((base + cg_slope * nend) / 1024))], c, 2, "6 5")
    d.circle(X(nx), Y(cap_gib), 6.5, c, "#FFFFFF", 2)
    d.line(X(nx), Y(cap_gib) + 8, X(nx), py0, c, 1, "3 3")
    d.text(X(nx), py0 - 8, f"{int(nx)}", 14, RAMPS[ramp][2], 700, "middle")
    yl = Y((base + cg_slope * lab_n) / 1024)
    d.text(X(lab_n), yl + dy, label, 13.5, RAMPS[ramp][2], 600)


demand(cg_base, "blue", "全部用量（含旧缓存）", 8, 30)
demand(cg_base_anon_other, "purple", "必须常驻的部分", 48, 30)
d.y = gy + GH

note_hp = (f"大页池不是先到的瓶颈：空闲池 {HP_FREE_MIB / 1024:.0f} GiB，每个空闲沙箱 {hp_slope:.0f} MiB 可放约 {cap_hp_idle} 个；"
           f"即使每个都把 2048 MiB 用满也有 {cap_hp_full} 个，仍在 {cap_own} 之后")
d.callout(note_hp, "teal")
d.callout(f"要稳定跑 X 个空闲沙箱：把 template-manager.hcl 的 memory 调到 ≥ {cap_gib:.0f} GiB（构建模板所需）"
          f"+ X × {cg_slope / 1024:.2f} GiB，例如 500 个 ≈ {cap_gib + 500 * cg_slope / 1024:.0f} GiB", "coral")

d.footer("口径：普通内存以 orchestrator cgroup 的 memory.current 增量为准，memory.stat 拆出 anon、file；"
         "全机 MemTotal−MemAvailable 受同机其他负载干扰（1 个沙箱时 Cached 就涨了 6.4 GiB），不用于计算。"
         "关闭沙箱 60 s 后大页与 page cache 全部归还，orchestrator 堆几分钟内回落，未见泄漏。"
         "原始报告：benchmark/mem_results/20261110_base_2u2g_r1、_r2；算法见 空闲沙箱内存实测分析.md。")


svg = os.path.join(HERE, "idle_sandbox_mem.svg")
d.save(svg)

# 计算结果打印出来，方便和文档核对
print(f"hp slope {hp_slope:.1f}; cg slope {cg_slope:.1f} icpt {cg_icpt:.1f} resid {cg_resid:.1f}")
print("slopes [hp, anon, orchfile, otherfile, other]:", [round(x, 1) for x in slope])
for n in NS:
    print(f"N={n} per:", [round(x, 1) for x in per[n]], "sum", round(sum(per[n]), 1))
print(f"cap: hp_idle {cap_hp_idle} hp_full {cap_hp_full} cg_reclaim {cap_reclaim} cg_own {cap_own} cg_anon {cap_anon}")
print(f"cg_base {cg_base} anon+other base {cg_base_anon_other:.1f}; fc n={len(all_fc)} range {min(all_fc)}-{max(all_fc)}")
print("idle growth hp per sandbox T1->T2:", [round(x, 1) for x in idle_growth])

js = os.environ.get("SVG2PNG_JS")
if js and shutil.which("node"):
    subprocess.run(["node", js, svg, os.path.join(HERE, "idle_sandbox_mem.png"), "2"], check=True)
    print("PNG:", os.path.join(HERE, "idle_sandbox_mem.png"))
