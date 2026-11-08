"""21 章的三张数据图（新 920B）。

图 21-1（§5）：两天 15 组长跑的 restore 逐项一致次数，数字取自 §4.5 轮次表与 §5.1。
图 21-2、21-3（§7）：产物盘走平，数字取自 §7.2、§7.4 原 mermaid xychart 与同节表格。不另加数据。
颜色按轮次区分：问题版本（修复前基线）coral，修复中 gray，修复后的版本 teal（21-2 两条修复后的线几乎重合，用实线 / 虚线区分）。
"""
from common import *  # noqa: F401,F403
from chart_kit import col, fmt
from diagram_kit import RAMPS, MUTED, SUB, text_w
from draw_rollback_20 import gbars

LABEL_W, RIGHT_PAD = 240, 50


def value_table(d, xlabels, series):
    """在折线图下方逐系列列出每个点的数值，列与 x 轴刻度对齐。series 与 Chart.lines 的参数相同。"""
    x0 = d.M + LABEL_W
    x1 = d.W - d.M - RIGHT_PAD
    n = len(xlabels)
    sx = lambda i: x0 + (x1 - x0) * (i + 0.5) / n
    y = d.y - 8
    d.line(x0, y - 4, x1, y - 4)
    for name, ramp, vs, style in series:
        y += 26
        c = col(ramp)
        tc = RAMPS[ramp][2]
        d.line(d.M, y - 5, d.M + 22, y - 5, c, 2.6, "6 4" if style == "dash" else None)
        d.text(d.M + 30, y, name, 13.5, tc, 600)
        for i, v in enumerate(vs):
            d.text(sx(i), y, f"{v:.1f}", 13.5, tc, 400, "middle")
    d.y = y + 6


def chart(d, xlabels, series, ylim, ytitle):
    d.lines(xlabels, series, ylim=ylim, ytitle=ytitle, label_w=LABEL_W, right_pad=RIGHT_PAD,
            annotate=False, h=340)
    value_table(d, xlabels, series)


# ---------------------------------------------------------------- 图 21-1
RUNS = [  # (轮, 二进制, 形态, 时长, restore 逐项一致, 阶段)
    ("long1", "4af2872c6", "无上限", "1800 s", 32547, "coral"),
    ("long2", "4af2872c6", "无上限", "1800 s", 31680, "coral"),
    ("long3", "cba26f7c1", "无上限", "1800 s", 32340, "gray"),
    ("long4", "baae30b56", "无上限", "1800 s", 30173, "gray"),
    ("long5", "5319de5dc", "无上限", "1800 s", 28331, "gray"),
    ("long6", "5319de5dc", "上限 60", "1800 s", 70702, "gray"),
    ("ct-opt", "1b112fbbd", "上限 60", "900 s", 41748, "gray"),
    ("d1", "df4b38336", "上限 60", "900 s", 43352, "gray"),
    ("long7", "0c4c2c5ae", "上限 60", "1800 s", 87973, "gray"),
    ("long8", "0c4c2c5ae", "滚动保留 10", "1800 s", 31443, "gray"),
    ("long8 对照", "0c4c2c5ae", "滚动保留 10，关合并", "600 s", 8128, "gray"),
    ("long9", "3539c45df", "滚动保留 10", "7200 s", 112402, "gray"),
    ("long10", "8ea5322bf", "滚动保留 10", "3600 s", 75817, "teal"),
    ("最终长测", "8ea5322bf", "上限 60", "1800 s", 97858, "teal"),
    ("串口段", "8ea5322bf", "单沙箱 × 10 段 × 180 s", "1888 s", 7383, "teal"),
]


def fig_runs():
    d = Chart()
    d.header("两天 15 组长跑：731877 次 restore 逐项一致，0 次不一致",
             "新 920B，2026-09-28～29，9 个二进制，每修一个问题就用同一套负载再跑一轮。除串口段外都是 16 沙箱、模板 base。"
             "每组取进入校验的 restore 次数（verified 分子），五项都回到目标时刻才算一致。")
    d.legend_row([("修复前基线（部署件 4af2872c6）", "coral", "bar"), ("修复中", "gray", "bar"),
                  ("最终代码 8ea5322bf", "teal", "bar")])
    gbars(d, [(n, f"{b} · {form} · {t}", [(v, r, str(v), None)]) for n, b, form, t, v, r in RUNS],
          ("lin", 120000), ticks=[0, 20000, 40000, 60000, 80000, 100000, 120000],
          tick_fmt=lambda t: f"{t:g}", label_w=330, bar_h=28, gap=24, note_w=110,
          axis_title="restore 逐项一致次数")
    d.footer("注：各组时长和形态不同。不计入的有 ct-opt 基线 17009、long8 预跑 165、串口段试跑 267、"
             "T36 的 16658。部署件 93ccb02 的 long11（147557 次）单独列出，加上后共 879434 次，全部一致。")
    d.save(out("rollback", "21-1-两天15组长跑restore一致"))


# ---------------------------------------------------------------- 图 21-2
def fig_cap60():
    d = Chart()
    d.header("上限 60：long6 的盘持续上涨，long7 与最终长测从第 5 分钟起走平",
             "920B，每沙箱可见 checkpoint 上限 60，同一负载各跑 30 min。纵轴是根分区 df 增量（GiB）。"
             "long6 在修复层回收和合并之前，long7 加了合并。")
    S = [("long6（5319de5dc）", "coral", [94.2, 175.7, 221.6, 255.1, 287.2, 318.8, 335.0], "line"),
         ("long7（0c4c2c5ae）", "teal", [76.9, 90.5, 87.1, 86.8, 84.8, 85.6, 86.2], "dash"),
         ("最终长测（8ea5322bf）", "teal", [78.1, 88.3, 88.4, 86.0, 87.1, 84.9, 85.2], "line")]
    d.legend_row([(n, r, s) for n, r, _, s in S])
    chart(d, ["1 min", "6 min", "11 min", "16 min", "21 min", "26 min", "30 min"], S, (0, 350), "GiB")
    d.footer("注：long6 峰值 340.6 GiB，后 15 min 斜率 6.12 GiB/min，仍在涨；long7 峰值 90.5 GiB、最终长测峰值 90.3 GiB，"
             "后 15 min 斜率 −0.03 与 −0.13 GiB/min。df 在 84–89 GiB 之间来回。")
    d.save(out("rollback", "21-2-上限60根分区增长"))


# ---------------------------------------------------------------- 图 21-3
def fig_keep10():
    d = Chart()
    d.header("滚动保留 10 两小时：120 min 时 restore p50 从 160.7 降到 94.3 ms，低约 41%",
             "920B，rolling_keep10.py，16 沙箱，保留 10，restore 概率 0.25，各跑 2 h。纵轴是每 10 min 一段的"
             "服务端 restore total p50（ms）。long9 是视图装配优化之前的版本，long11 是部署件。")
    S = [("long9（3539c45df）", "coral",
          [90.9, 109.2, 123.9, 133.6, 138.2, 141.9, 148.3, 154.9, 153.9, 158.2, 160.9, 160.7], "line"),
         ("long11（93ccb02 部署件）", "teal",
          [73.1, 78.8, 80.7, 82.5, 84.3, 85.2, 85.4, 85.6, 88.2, 90.1, 93.8, 94.3], "line")]
    d.legend_row([(n, r, s) for n, r, _, s in S])
    xl = ["0-10", "10-20", "20-30", "30-40", "40-50", "50-60", "60-70", "70-80", "80-90", "90-100",
          "100-110", "110-120"]
    chart(d, xl, S, (0, 180), "ms")
    d.footer("注：横轴单位 min。long11 全程斜率 0.296 ms / 千块，long9 为 1.190。"
             "long11 第二个小时 85.4 → 94.3 ms 的上涨主要来自冻结窗口里等 conntrack 的时间。")
    d.save(out("rollback", "21-3-滚动保留10两小时restore"))


if __name__ == "__main__":
    fig_runs()
    fig_cap60()
    fig_keep10()
