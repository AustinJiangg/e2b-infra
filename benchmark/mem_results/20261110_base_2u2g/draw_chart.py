"""画 2026-11-10 空闲沙箱内存实测的展示图（SVG）。

依赖 explainer-chart skill 的 kit（hdbss-dirty-tracking 仓库 .claude/skills/explainer-chart/scripts），
路径用环境变量 EXPLAINER_CHART_KIT 指定：
    EXPLAINER_CHART_KIT=/path/to/explainer-chart/scripts python3 draw_chart.py
PNG 由 SVG 导出（本目录 idle_sandbox_mem.png，2 倍分辨率）。
数字全部来自本目录 report_n1.txt / report_n10.txt / host_context.txt，算法见 空闲沙箱内存实测分析.md §2。
"""
import os
import sys

sys.path.insert(0, os.environ.get(
    "EXPLAINER_CHART_KIT",
    "/home/user/hdbss-dirty-tracking/.claude/skills/explainer-chart/scripts"))
from chart_kit import Chart          # noqa: E402
from diagram_kit import Step, RAMPS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 原始数据（MiB，T2 = 空闲 300 s）
N1 = dict(hp=160.0, fc_pss=4.2, orch=102.8, nonhp=238.0)
N10 = dict(hp=1582.0, fc_pss=26.3, orch=833.5, nonhp=1862.3)
PER_FC_N10 = [158, 158, 156, 162, 156, 158, 160, 158, 160, 156]
HP_FREE_PAGES = 200026                     # 基线空闲 2 MiB 大页数


def split(d, n):
    """每沙箱：大页 / fc / orchestrator / 未归因（= ΔMemAvailable − fc − orch）"""
    un = d["nonhp"] - d["fc_pss"] - d["orch"]
    return [d["hp"] / n, d["fc_pss"] / n, d["orch"] / n, un / n]


r1 = split(N1, 1)
r10 = split(N10, 10)
tot1 = [N1[k] for k in ("hp", "fc_pss", "orch")] + [N1["nonhp"] - N1["fc_pss"] - N1["orch"]]
tot10 = [N10[k] for k in ("hp", "fc_pss", "orch")] + [N10["nonhp"] - N10["fc_pss"] - N10["orch"]]
marg = [(b - a) / 9 for a, b in zip(tot1, tot10)]          # 边际：(N=10 − N=1) / 9
hp_marg = marg[0]
cap_idle = int(HP_FREE_PAGES * 2 / hp_marg)
cap_full = int(HP_FREE_PAGES * 2 / 2048)

d = Chart(width=1600)
d.header(f"空闲 2U2G 沙箱只用 {hp_marg:.0f} MiB 大页：memory_mb = 2048 是上限，不是占用",
         "base 模板（ubuntu 22.04 + envd，cpu_count=2，memory_mb=2048）· 宿主机 2.4 TiB 内存，静态大页池 200064 × 2 MiB"
         " · 2026-11-10 实测 N=1、N=10，各空闲 300 s")

# ---------------- 1 测量方法
d.section(1, "怎么测", note="全部在宿主机侧读数，沙箱内不执行任何命令")
d.flow([
    Step("采基线", "drop_caches 后读大页计数、smaps、进程 RSS、meminfo", actor="宿主机"),
    Step("起 N 个沙箱", "N=1、N=10 各一次，创建后什么都不做", actor="E2B SDK"),
    Step("空闲采样", "T1 = 30 s、T2 = 300 s 各采一次", actor="宿主机"),
    Step("做差、均摊", "T2 − 基线，再除以 N", actor="脚本"),
    Step("求边际值", "(N=10 − N=1) ÷ 9，扣掉一次性开销", actor="分析"),
], min_h=118)

# ---------------- 2 结果
d.section(2, f"每多一个空闲沙箱，宿主机多付出约 {sum(marg):.0f} MiB", highlight="本图重点")
SEGS = [("客户机内存（大页池）", "teal"), ("firecracker 进程", "gray"),
        ("orchestrator 进程", "blue"), ("未归因的普通内存", "amber")]
d.legend_row([(n, r, "bar") for n, r in SEGS], y=d.y + 34)
d.hstack([
    ("N=1", r1, "含一次性开销"),
    ("N=10 均摊", r10, "总增量 ÷ 10"),
    ("边际：每多 1 个", marg, "(N=10 − N=1) ÷ 9"),
], SEGS, label_w=200, unit=" MiB", vmax=420, xticks=[0, 100, 200, 300, 400])
d.callout(f"大页 {hp_marg:.0f} MiB 是硬数据：直接读 firecracker 的 smaps，与“大页已用”增量逐 MiB 吻合（160 / 1582）；"
          f"firecracker 自身只有 {marg[1]:.1f} MiB", "teal")
d.callout(f"后三段是普通内存，合计 = MemTotal − MemAvailable 的下降（约 {sum(marg[1:]):.0f} MiB/个）；"
          f"其中 {marg[3]:.0f} MiB 还说不清归属（page cache、内核对象？），要补测", "amber")

# ---------------- 3 为什么不是 2048
d.section(3, "为什么不是 2048 MiB", note="从快照恢复后按需缺页：客户机碰到哪一页，宿主机才分配哪一个 2 MiB 大页")
d.legend_row([("已落地的大页", "teal", "bar"), ("映射了但没落地，不占物理内存", "gray", "bar")], y=d.y + 34)
d.hstack([("客户机地址空间", [hp_marg, 2048 - hp_marg], "memory_mb = 2048")],
         [("已落地的大页", "teal"), ("映射了但没落地，不占物理内存", "gray")],
         label_w=200, unit=" MiB", vmax=2048, xticks=[0, 512, 1024, 1536, 2048], bar_h=44)
d.strips([
    dict(title="每个沙箱的落地量：156–162 MiB", unit="MiB",
         note="N=10 里每个点是一个 firecracker 进程",
         groups=[("N=1", "teal", [N1["hp"]]), ("N=10", "teal", PER_FC_N10)],
         ylim=(140, 180), ref=(hp_marg, f"边际 {hp_marg:.0f}")),
    dict(title="空闲 270 s 只多 4 MiB（2 个大页）", unit="MiB",
         note="每沙箱均值；左点 N=1，右点 N=10",
         groups=[("T1 = 30 s", "gray", [156.0, 154.2]), ("T2 = 300 s", "teal", [160.0, 158.2])],
         ylim=(140, 180)),
], per_row=2, h=270)

# 三条旁证：并排卡片，没有先后关系，所以不画箭头
cards = [
    ("大页预留增量 = 0", "HugePages_Rsvd 始终是 129，没有按 2048 MiB 预先占坑"),
    ("关闭后大页全部归还", "两次测完 HugePages_Free 都回到 200026"),
    ("客户机里也只用了 130 MiB", "free：used 71 + buff/cache 59；宿主机多出的约 30 MiB 可由内核保留区和 2 MiB 取整解释"),
]
gap = 32
cw = (d.W - 2 * d.M - 2 * gap) / 3
y0 = d.y + 26
ch = max(d._need(t, s, cw) for t, s in cards)
for i, (t, s) in enumerate(cards):
    d.block(d.M + i * (cw + gap), y0, cw, ch, t, s, "teal", style="outline")
d.y = y0 + ch

# ---------------- 4 容量
d.section(4, "对并发容量意味着什么", note=f"按基线空闲大页 {HP_FREE_PAGES} 个（≈ 390.7 GiB）计算")
d.hstack([
    ("沙箱都空闲", [cap_idle, 0], f"每个 {hp_marg:.0f} MiB"),
    ("沙箱都用满", [0, cap_full], "每个 2048 MiB"),
], [("空闲时的上限", "teal"), ("最坏情况", "coral")], label_w=200, unit=" 个", vmax=2800,
    xticks=[0, 500, 1000, 1500, 2000, 2500])
d.callout(f"大页侧：真实上限落在 {cap_full}～{cap_idle} 之间，取决于沙箱里跑什么。没有 balloon，客户机碰过的页不会还给宿主机，"
          "所以长时间运行的沙箱会往 2048 MiB 靠", "gray")
d.callout("普通内存侧：orchestrator 和 firecracker 都在 memory.max = 32 GiB 的 Nomad cgroup 里。按每个 84 MiB 粗算约 390 个，"
          "若未归因的 97 MiB 也记在这本账上则约 180 个，可能比大页先到上限，待补测确认", "coral")

d.footer("注：firecracker 用 PSS（共享的二进制只算一次）；未归因 = MemAvailable 下降 − firecracker − orchestrator，"
         "假设 orchestrator 的 RSS 增量主要是匿名页。宿主机背景占用约 465 GiB，MemAvailable 口径精度约几十 MiB，"
         "大页各项不受影响。原始报告在 benchmark/mem_results/20261110_base_2u2g/。")
d.save(os.path.join(HERE, "idle_sandbox_mem.svg"))
