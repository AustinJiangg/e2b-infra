"""进程级附录 E 的两张数据图（新 920B）。

图 E-1（§1）：第一版修复回退约 18 ms 的分段与三块拆分，数字取自 E-regression-case.md §1 表格与列表。
图 E-2（§4）：那约 8.7 ms 的定位，数字取自 §4.1 二分表、§4.4 微基准表、§4.5 惰性填充表。不另加数据。
颜色：E-1 按分段着色（criu_restore purple、kill gray、find_pids coral）；
E-2 按内核布局着色（快的内核 purple、含第一个修复提交的内核 coral、在快的内核上加填充 gray）。
"""
from common import *  # noqa: F401,F403
from chart_kit import fmt
from diagram_kit import INK, SUB
from draw_rollback_20 import gbars, dotrows


# ---------------------------------------------------------------- 图 E-1
def fig_e1():
    d = Chart()
    d.header("第一版修复让 restore 慢了约 18 ms，分成三块",
             "新 920B，2026-09-30 第一版修复（21 个提交）首次上机。verify_gsd：192 MiB 匿名内存的进程，同一沙箱连续 8 次 restore，"
             "各分段取 p50，单位 ms。")
    segs = [("criu_restore_ms", "purple"), ("kill_ms", "gray"), ("find_pids_ms", "coral")]
    d.legend_row([(n, r, "bar") for n, r in segs])

    d.section(1, "verify_gsd 的分段 p50", note="条内是各分段 p50，右端写 total_ms p50")
    rows = [("修复前", [64.0, 11.1, 0], "修复前的 gsd，修复前建的模板", "total_ms 75.6（find_pids 不单独计时）"),
            ("第一版修复", [70.4, 13.6, 9.0], "新建的模板，两次", "total_ms 93.8 / 94.5")]
    label_w, bar_h, gap, vmax = 260, 40, 22, 100
    y0 = d.y + 26
    x0, x1 = d.M + label_w, d.W - d.M - 110
    d.hstack([(n, v, s) for n, v, s, _ in rows], segs, total_label=False, label_w=label_w, bar_h=bar_h, gap=gap,
             vmax=vmax, xticks=[0, 20, 40, 60, 80, 100], unit=" ms")
    for i, (_, v, _, t) in enumerate(rows):
        end = x0 + sum(v) * (x1 - x0) / vmax
        d.text(end + 12, y0 + i * (bar_h + gap) + bar_h / 2 + 5, t, 14, INK, 600)

    d.section(2, "多出来的约 18 ms 分三块", note="区间是正文给的范围；原因见 §2 与 §3、§4")
    gbars(d, [("find_pids", "每次操作多调两次 systemctl show", [(9, "coral", "约 9", "gsd 引入，读代码可确定")]),
              ("kill", "直接杀改成先冻结再杀", [((1.5, 2.5), "gray", "约 +1.5～2.5", "gsd 引入，读代码可确定")]),
              ("criu_restore", "gsd 调用 criu 的方式几乎没变", [((6, 10), "purple", "约 +6～10", "当时原因不明")])],
          ("lin", 12), ticks=[0, 2, 4, 6, 8, 10, 12], tick_fmt=lambda t: f"{t:g} ms", label_w=260, bar_h=28,
          note_w=330)
    d.footer("注：criu_restore 的约 6～10 ms 在 verify_gsd 里是 64.0 对 70.4，性能脚本 demo_checkpoint_perf 各档是 8～10。"
             "18 次 restore 的文件与内存校验全部正确，这是纯粹的性能问题。")
    d.save(out("criu", "E-1-回退18ms的三块"))


# ---------------------------------------------------------------- 图 E-2
FAST, SLOW, PAD = "purple", "coral", "gray"


def fig_e2():
    d = Chart()
    d.header("约 8.7 ms 只取决于内核代码的位置：挪 64 字节变慢，挪 128 KiB 回到快",
             "新 920B，2026-09-30，guest 2 vCPU、2048 MB，THP 为 never，192 MiB。三段各用不同的内核和模板，段与段之间不能相减。单位 ms。")
    d.legend_row([("快的内核（修复前的布局）", FAST, "dot"), ("含第一个修复提交的内核", SLOW, "dot"),
                  ("快的内核 + 不执行的填充", PAD, "dot")])

    d.section(1, "二分：约 8.5～9 ms 全部在第一个修复提交处出现",
              note="criu_restore_ms，各轮 p50 的中位数；一行两个点是两个模板")
    dotrows(d, [("修复前的内核（09-23 构建）", [(64.0, FAST), (63.9, FAST)], "64.0 / 63.9"),
                ("同一份源码，用后来的编译配置重编", [(63.6, FAST), (63.6, FAST)], "63.6 / 63.6"),
                ("加上第一个修复提交", [(73.2, SLOW), (71.5, SLOW)], "73.2 / 71.5"),
                ("再加上第二个修复提交", [(71.8, SLOW)], "71.8"),
                ("加到当时的第一批修复为止", [(74.2, SLOW), (73.1, SLOW)], "74.2 / 73.1"),
                ("再加上切换与并发操作互斥的修复", [(72.9, SLOW), (72.5, SLOW)], "72.9 / 72.5")],
            60, 80, ticks=[60, 65, 70, 75, 80], sub_gap=10)

    d.section(2, "微基准：只有“拷贝当中缺页”这一种操作变慢", note="guest 内各做 192 MiB；上为快的内核，下为慢的内核")
    dotrows(d, [("只触发匿名内存的缺页", [((67, 69), FAST), ((67, 69), SLOW)], "67～69 · 67～69"),
                ("只读文件", [((15, 16), FAST), ((15, 16), SLOW)], "15～16 · 15～16"),
                ("把文件读进已经缺过页的内存", [(15, FAST), (15, SLOW)], "约 15 · 约 15"),
                ("preadv 进尚未缺页的匿名内存", [((77, 78), FAST), ((86, 88), SLOW)], "77～78 · 86～88")],
            0, 100, ticks=[0, 20, 40, 60, 80, 100], row_h=44, sub_gap=14)

    d.section(3, "惰性填充：只挪位置、不改逻辑", note="同一个微基准的中位数；填充插在 fs/proc 的代码段里，永远不被执行")
    dotrows(d, [("快的内核（基准）", [(77.0, FAST)], "77.0"),
                ("加上第一个修复提交", [(84.1, SLOW)], "84.1"),
                ("快的内核 + 64 字节填充", [(86.2, PAD)], "86.2"),
                ("快的内核 + 4 KiB 填充", [(85.7, PAD)], "85.7"),
                ("快的内核 + 16 KiB 填充", [(83.8, PAD)], "83.8"),
                ("快的内核 + 64 KiB 填充", [(84.2, PAD)], "84.2"),
                ("快的内核 + 128 KiB 填充", [(77.9, PAD)], "77.9"),
                ("快的内核 + 2 MiB 填充", [(80.6, PAD)], "80.6"),
                ("快的内核，所有函数按 64 字节对齐", [(84.6, PAD)], "84.6"),
                ("慢的内核，所有函数按 64 字节对齐", [(89.1, SLOW)], "89.1")],
            74, 92, ticks=[74, 77, 80, 83, 86, 89, 92], refs=[(77.0, "快的内核 77.0")])
    d.footer("注：§4.1 中同一内核的两个模板相差 0.1～1.1 ms，不同内核之间相差约 9 ms。§4.4 的最后一种操作正是 criu 恢复内存的做法。"
             "效应以 128 KiB 为周期；为什么相位会影响这条路径的速度，没有继续往下查。")
    d.save(out("criu", "E-2-8.7ms与代码布局"))


if __name__ == "__main__":
    fig_e1()
    fig_e2()
