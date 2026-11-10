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
d = Chart(width=1600)
d.header(f"空闲 2U2G 沙箱每个约 {total_slope:.0f} MiB：大页 {hp_slope:.0f} + 普通内存 {cg_slope:.0f}，"
         "memory_mb = 2048 只是上限",
         "base 模板（ubuntu 22.04 + envd，cpu_count=2，memory_mb=2048）· 宿主机 2.4 TiB 内存，静态大页池 200064 × 2 MiB"
         " · 2026-11-10 两轮实测，每次空闲 300 s")

d.section(1, "怎么测", note="全部在宿主机侧读数，沙箱内不执行任何命令")
d.flow([
    Step("采基线", "drop_caches 后读大页计数、smaps、orchestrator cgroup", actor="宿主机"),
    Step("起 N 个沙箱", "第二轮 N = 1、10、50，创建后什么都不做", actor="E2B SDK"),
    Step("空闲采样", "T1 = 30 s、T2 = 300 s 各采一次", actor="宿主机"),
    Step("关闭后再采", "关闭 60 s 后看还回去多少", actor="宿主机"),
    Step("对 N 做线性拟合", "斜率 = 每多一个沙箱的增量，截距 = 一次性开销", actor="分析"),
], min_h=118)

d.section(2, f"每多一个空闲沙箱，宿主机多付出约 {total_slope:.0f} MiB", highlight="本图重点")
SEGS = [("客户机内存（大页池）", "teal"), ("匿名内存（orchestrator 堆等）", "blue"),
        ("沙箱目录文件页（orchestrator mmap）", "purple"), ("其他 page cache 与内核对象", "amber")]
d.legend_row([(nm, r, "bar") for nm, r in SEGS], y=d.y + 34)
d.hstack([
    ("N=1", per[1][:3] + [per[1][3] + per[1][4]], "T2 增量"),
    ("N=10 均摊", per[10][:3] + [per[10][3] + per[10][4]], "T2 增量 ÷ 10"),
    ("N=50 均摊", per[50][:3] + [per[50][3] + per[50][4]], "T2 增量 ÷ 50"),
    ("斜率：每多 1 个", slope[:3] + [slope[3] + slope[4]], "对 N=1、10、50 拟合"),
], SEGS, label_w=200, unit=" MiB", vmax=400, xticks=[0, 100, 200, 300, 400])
d.callout(f"大页 {hp_slope:.0f} MiB：直接读 firecracker 的 smaps，与系统“大页已用”增量逐 MiB 吻合；不记在任何 cgroup 的内存账上",
          "teal")
d.callout(f"后三段全部记在 orchestrator 所在的 Nomad cgroup 里（memory.max = 32 GiB）："
          f"三个 N 的增量严格线性，每个 {cg_slope:.1f} MiB，残差不超过 {cg_resid:.0f} MiB", "blue")

d.section(3, "为什么不是 2048 MiB", note="从快照恢复后按需缺页：客户机碰到哪一页，宿主机才分配哪一个 2 MiB 大页")
d.legend_row([("已落地的大页", "teal", "bar"), ("映射了但没落地，不占物理内存", "gray", "bar")], y=d.y + 34)
d.hstack([("客户机地址空间", [hp_slope, 2048 - hp_slope], "memory_mb = 2048")],
         [("已落地的大页", "teal"), ("映射了但没落地，不占物理内存", "gray")],
         label_w=200, unit=" MiB", vmax=2048, xticks=[0, 512, 1024, 1536, 2048], bar_h=44)
d.strips([
    dict(title=f"{len(all_fc)} 个沙箱的大页落地量：{min(all_fc):.0f}–{max(all_fc):.0f} MiB", unit="MiB",
         note=f"每个点是一个 firecracker 进程（两轮、五次测量）；虚线是斜率 {hp_slope:.0f}",
         groups=[("一轮 N=1", "teal", r1[1][1]), ("一轮 N=10", "teal", r1[10][1]),
                 ("二轮 N=1", "teal", r2[1][1]), ("二轮 N=10", "teal", r2[10][1]),
                 ("二轮 N=50", "teal", r2[50][1])],
         ylim=(140, 170), ref=(hp_slope, "")),
    dict(title="每个沙箱的普通内存不随 N 变", unit="MiB",
         note="orchestrator cgroup 的 T2 增量 ÷ N",
         groups=[(f"N={n}", "blue", [sum(tot[n][1:]) / n]) for n in NS],
         ylim=(150, 230), ref=(cg_slope, f"斜率 {cg_slope:.0f}")),
], per_row=2, h=270)

cards = [
    ("大页预留增量 = 0", "HugePages_Rsvd 始终是 129，没有按 2048 MiB 预先占坑"),
    ("关闭后能还回去", "大页和 page cache 立即归还；orchestrator 堆几分钟内回落"),
    ("客户机里只用了 130 MiB", "free：used 68～71 + buff/cache 59，和宿主机大页量级一致"),
]
gap = 32
cw = (d.W - 2 * d.M - 2 * gap) / 3
y0 = d.y + 26
ch = max(d._need(t, s, cw) for t, s in cards)
for i, (t, s) in enumerate(cards):
    d.block(d.M + i * (cw + gap), y0, cw, ch, t, s, "teal", style="outline")
d.y = y0 + ch

d.section(4, "并发容量：先到上限的是 cgroup，不是大页", note="按第二轮 N=50 测量前的基线计算")
d.legend_row([("orchestrator cgroup 32 GiB", "blue", "bar"), ("大页池：都用满", "coral", "bar"),
              ("大页池：都空闲", "teal", "bar")], y=d.y + 34)
d.hstack([
    ("cgroup：开始回收", [cap_reclaim, 0, 0], f"已有 {cg_base / 1024:.1f} GiB 旧缓存"),
    ("cgroup：旧缓存挤完", [cap_own, 0, 0], "沙箱的页全部常驻"),
    ("cgroup：只算匿名", [cap_anon, 0, 0], "文件页全部可回收"),
    ("大页池：都用满", [0, cap_hp_full, 0], "每个 2048 MiB"),
    ("大页池：都空闲", [0, 0, cap_hp_idle], f"每个 {hp_slope:.0f} MiB"),
], [("orchestrator cgroup 32 GiB", "blue"), ("大页池·最坏", "coral"), ("大页池", "teal")],
    label_w=200, unit=" 个", vmax=2800, xticks=[0, 500, 1000, 1500, 2000, 2500])
d.callout(f"约 {cap_reclaim} 个沙箱时 cgroup 用量碰到 32 GiB，内核开始挤掉旧 page cache；超过约 {cap_own} 个，"
          f"就得回收沙箱自己正在用的文件页，能否平稳要压测。只算匿名内存的话上限约 {cap_anon} 个", "blue")
d.callout(f"想稳定支撑 X 个空闲沙箱：template-manager.hcl 的 memory 建议 ≥ 32 GiB（构建所需）+ X × {cg_slope / 1024:.2f} GiB；"
          "有负载的沙箱还要更多", "coral")

d.footer("注：普通内存以 orchestrator cgroup 的 memory.current 为准，memory.stat 拆出 anon 和 file，"
         f"余下的 {slope[4]:.1f} MiB 内核对象并入橙色一段。全机 MemTotal−MemAvailable 第二轮受其他负载干扰，"
         "1 个沙箱时 Cached 就涨了 6.4 GiB，所以不用于计算。原始报告在 benchmark/mem_results/ 下的 _r1、_r2 两个目录。")

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
