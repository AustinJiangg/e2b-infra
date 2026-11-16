"""进程级 20 章（920B 性能结果）的三张数据图。

图 20-1（§1.2）：分档主表，restore 与增量 checkpoint 的服务端 total_ms，两轮。
图 20-2（§3 第二部分）：一、二、三棵 64 MiB 进程树的 restore 与 checkpoint 分段。
图 20-3（§4）：早期六档，固定 192 MiB 进程的 checkpoint 与 restore。

数字全部写在下面的“数据”一节，每组旁边注明出处行号；画图代码只读这些常量。以后重测，只改这一节再重跑。
颜色：920B = amber。同一张图里区分两轮或两种操作时用实线 / 虚线；区分树的棵数时用 amber 的深浅。

本文件还提供 tier_chart（档位折线 + 下方数值表），draw_criu_24.py 复用。
"""
from common import *  # noqa: F401,F403
from chart_kit import col, ALIASES
from diagram_kit import RAMPS, MUTED, SUB, INK
from draw_rollback_20 import gbars

# ================================================================ 数据（以后只改这里）
DOC = "criu-rollback/docs/20-920b-performance.md"

# 档位：总改动量 MiB 与 内存 / 文件 MiB
# 来源：criu-rollback/docs/20-920b-performance.md §1.2 主表（第 52～57 行）第 1、2 列
TIERS = [("0", "0 / 0"), ("16", "12 / 4"), ("32", "24 / 8"), ("64", "48 / 16"),
         ("128", "96 / 32"), ("256", "192 / 64")]

# 服务端 total_ms 三轮中位数，单位 ms
# 来源：criu-rollback/docs/20-920b-performance.md §1.2 主表（第 52～57 行）
PERF_TIERS = {
    "restore": {"2026-10-16": [9.1, 14.8, 19.8, 31.1, 52.6, 93.9],      # 第 7 列
                "2026-11-06": [8.6, 15.5, 19.4, 30.3, 51.5, 94.3]},     # 第 8 列
    "增量 checkpoint": {"2026-10-16": [10.6, 18.1, 25.9, 37.6, 59.8, 106.2],   # 第 5 列
                       "2026-11-06": [9.5, 25.2, 24.9, 40.0, 64.6, 117.8]},  # 第 6 列
}

# 一、二、三棵 64 MiB 进程树，p50，单位 ms（2026-10-08，base-gsd9，8 轮，每档 64 次 restore、40 次 checkpoint）
# 来源：criu-rollback/docs/20-920b-performance.md §3 第二部分表（第 141～150 行）
TREES = [
    # (操作, 分段, [1 棵, 2 棵, 3 棵])，数值按原文两位小数写出
    ("restore", "total_ms", [32.94, 60.38, 93.31]),                        # 第 141 行
    ("restore", "criu_restore_ms", [26.63, 53.13, 81.65]),                 # 第 142 行
    ("restore", "kill_ms", [5.16, 5.96, 9.85]),                            # 第 143 行
    ("增量 checkpoint（不改内存）", "total_ms", [11.17, 21.84, 33.49]),     # 第 144 行
    ("增量 checkpoint（不改内存）", "criu_dump_ms", [9.61, 19.86, 31.18]),  # 第 145 行
    ("增量 checkpoint（不改内存）", "stopped_ms", [0.65, 11.32, 22.46]),    # 第 146 行
    ("全量 checkpoint", "criu_dump_ms", [47.38, 91.91, 130.98]),           # 第 147 行
    ("全量 checkpoint", "stopped_ms", [2.53, 45.08, 84.57]),               # 第 148 行
    ("全部改写后的 checkpoint", "criu_dump_ms", [42.07, 85.63, 132.08]),   # 第 149 行
    ("全部改写后的 checkpoint", "stopped_ms", [1.18, 45.70, 90.17]),       # 第 150 行
]

# 早期六档：固定 192 MiB 进程，三次运行的中位数，单位 ms（2026-10-08）
# 来源：criu-rollback/docs/20-920b-performance.md §4 表（第 166～171 行）；cp0 全量 117.1 见第 165 行
EARLY_TIERS = [("cp1", "0 / 0"), ("cp2", "12 / 4"), ("cp3", "24 / 8"), ("cp4", "48 / 16"),
               ("cp5", "96 / 32"), ("cp6", "192 / 64")]
EARLY = {
    "checkpoint total_ms": [12.2, 23.3, 29.1, 38.9, 63.6, 107.7],         # 第 3 列
    "restore total_ms": [96.1, 85.8, 87.6, 91.8, 90.8, 96.6],             # 第 5 列
    "restore criu_restore_ms": [83.0, 73.0, 74.4, 78.5, 77.7, 84.1],      # 第 4 列
}
EARLY_CP0 = 117.1   # 第 165 行，cp0 全量 checkpoint total_ms

# ================================================================ 画图
LABEL_W, RIGHT_PAD = 250, 40


def _rp(ramp):
    return RAMPS[ALIASES.get(ramp, ramp)]


def tier_chart(d, xlabels, xsubs, series, ylim, ytitle="ms", h=280, label_w=LABEL_W, right_pad=RIGHT_PAD,
               table_only=()):
    """档位折线：横轴按档位顺序等距排列，每个档位两行标签（档名 + 副标签）；下方逐系列列出原文数值。
    series: [(名, 色阶, [值或 None], 'line'|'dash')]；table_only: 只进数值表、不画线的系列，格式相同。
    None（待填）不画点、数值表里写“—”。"""
    top0 = d.y
    d.lines(xlabels, series, ylim=ylim, ytitle=ytitle, label_w=label_w, right_pad=right_pad,
            annotate=False, h=h)
    bot = top0 + 24 + h
    x0 = d.M + label_w
    x1 = d.W - d.M - right_pad
    n = len(xlabels)
    sx = lambda i: x0 + (x1 - x0) * (i + 0.5) / n
    if xsubs:
        for i, s in enumerate(xsubs):
            d.text(sx(i), bot + 46, s, 12.5, MUTED, 400, "middle")
        d.y = bot + 66
    y = d.y - 4
    d.line(d.M, y - 4, x1, y - 4)
    for name, ramp, vs, style in list(series) + list(table_only):
        y += 26
        c = col(ramp)
        tc = _rp(ramp)[2]
        if style in ("line", "dash"):
            d.line(d.M, y - 5, d.M + 22, y - 5, c, 2.6, "6 4" if style == "dash" else None)
        d.text(d.M + 30, y, name, 13.5, tc, 600)
        for i, v in enumerate(vs):
            d.text(sx(i), y, "—" if v is None else f"{v:.1f}", 13.5, tc, 400, "middle")
    d.y = y + 6


def fig_tiers():
    d = Chart()
    d.header("920B 分档：restore 与增量 checkpoint 的服务端 total_ms 随档位变化",
             "新 920B，2026-10-16 与 2026-11-06 两轮，每轮每档 3 个样本，取中位数；2 vCPU / 2048 MB，THP never，"
             "每轮 18 次恢复校验全部正确。每档新建沙箱、只分配本档负载，进程大小与改动量同时变化。")
    d.legend_row([("2026-10-16", "amber", "line"), ("2026-11-06", "amber", "dash")])
    xl = [f"{t} MiB" for t, _ in TIERS]
    xs = [f"内存 / 文件 {s}" for _, s in TIERS]
    style = {"2026-10-16": "line", "2026-11-06": "dash"}
    for k, (op, note) in enumerate([("restore", "回到本档全量基点"),
                                    ("增量 checkpoint", "改写本档负载之后")], 1):
        d.section(k, op, note=note + "；横轴是总改动量，按档位等距排列")
        S = [(f"{r}", "amber", vs, style[r]) for r, vs in PERF_TIERS[op].items()]
        tier_chart(d, xl, xs, S, (0, 125))
    d.footer("注：单位 ms，gsd 自报的服务端 total_ms，不含客户端到 gsd 的往返。全量基点和各档 RSS 单列在正文表中，未画。"
             "固定进程大小时的情形见 §4（图 20-3）。")
    d.save(out("criu", "20-1-920B分档"))


def fig_trees():
    # 树的棵数用 amber 的深浅：1 棵最浅，3 棵最深
    a = RAMPS["amber"]
    RAMPS["amber_t1"] = ("#FAEEDA", "#D9A55A", a[2], a[3])
    RAMPS["amber_t2"] = ("#EFC07A", a[1], a[2], a[3])
    RAMPS["amber_t3"] = ("#BA7517", "#854F0B", a[2], a[3])
    shades = ["amber_t1", "amber_t2", "amber_t3"]

    d = Chart()
    d.header("一、二、三棵 64 MiB 进程树：restore 与 checkpoint 各分段的 p50",
             "920B，2026-10-08 确认验证，verify_multitree.py 的 V12 第二部分，模板 base-gsd9；8 轮，每档 64 次 restore、"
             "40 次 checkpoint 进入统计。增量 checkpoint 是不改内存、连续再做一次。单位 ms。")
    d.legend_row([("1 棵", "amber_t1", "bar"), ("2 棵", "amber_t2", "bar"), ("3 棵", "amber_t3", "bar")])
    ops = []
    for op, seg, vs in TREES:
        if op not in ops:
            ops.append(op)
    notes = {"restore": None,
             "增量 checkpoint（不改内存）": "stopped_ms：进程被冻结的时长",
             "全量 checkpoint": None, "全部改写后的 checkpoint": None}
    for k, op in enumerate(ops, 1):
        d.section(k, op, note=notes[op])
        groups = [(seg, None, [(v, shades[i], f"{v:.2f}", None) for i, v in enumerate(vs)])
                  for o, seg, vs in TREES if o == op]
        gbars(d, groups, ("lin", 140), ticks=[0, 20, 40, 60, 80, 100, 120, 140],
              tick_fmt=lambda t: f"{t:g}", label_w=200, bar_h=18, inner=4, gap=18, note_w=70)
    d.footer("注：2026-10-09 落位复核的一、二、三棵树 restore total_ms p50 为 33.8 / 61.9 / 94.4，不在图中；"
             "2026-11-06 的全流程复测跳过了 V12。")
    d.save(out("criu", "20-2-一二三棵树"))


def fig_early():
    d = Chart()
    d.header("早期六档，固定 192 MiB 进程：checkpoint 与 restore 随改动量的变化",
             "920B，2026-10-08，2026-10-16 之前的测法：一个预分配 192 MiB 的常驻进程按六档依次改写内存并写文件，"
             "每档一次 checkpoint，最后逐个 restore 回去。脚本连跑三次，取三次的中位数，每次运行每档一个样本。")
    d.legend_row([("checkpoint total_ms", "amber", "line"), ("restore total_ms", "amber", "dash")])
    d.section(1, "服务端分段", note="横轴是各档的内存改写 / 文件写入，按档位等距排列")
    S = [("checkpoint total_ms", "amber", EARLY["checkpoint total_ms"], "line"),
         ("restore total_ms", "amber", EARLY["restore total_ms"], "dash")]
    T = [("restore criu_restore_ms（未画线）", "gray", EARLY["restore criu_restore_ms"], "none")]
    tier_chart(d, [n for n, _ in EARLY_TIERS], [f"{s} MiB" for _, s in EARLY_TIERS], S, (0, 125), table_only=T)
    d.footer(f"注：单位 ms。cp0 是进程刚起来时的全量 checkpoint，total_ms {EARLY_CP0:g}，不在图中。"
             "三次运行之间单档的差可以到十几毫秒，这组数适合看趋势，不适合做版本之间的精细对比。")
    d.save(out("criu", "20-3-早期六档固定192MiB"))


if __name__ == "__main__":
    fig_tiers()
    fig_trees()
    fig_early()
