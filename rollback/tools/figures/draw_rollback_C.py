"""附录 C §3.9 视图块数与 restore 的数据图（920B）。

数字全部取自 C-evolution-and-cases.md §3.9 原 mermaid xychart 与同节表格，不另加数据。
颜色按轮次区分：问题版本 coral，修复后的两个版本 teal（几乎重合，用实线 / 虚线区分）。
"""
from common import *  # noqa: F401,F403
from draw_rollback_21 import chart


def fig_c1():
    d = Chart()
    d.header("视图装配改位图后：第 50–60 min 的 restore p50 从 141.9 降到 84.1 ms",
             "920B，滚动保留 10，同一负载的前 60 min。纵轴是每 10 min 一段的服务端 restore total p50（ms）。"
             "long9 是视图装配优化之前的版本；long10 改了位图；long11 是热路径相同的 RPM 部署版本。")
    S = [("long9（3539c45df）", "coral", [90.9, 109.2, 123.9, 133.6, 138.2, 141.9], "line"),
         ("long10（8ea5322bf）", "teal", [71.0, 78.6, 81.7, 82.1, 83.2, 84.1], "line"),
         ("long11（93ccb02）", "teal", [73.1, 78.8, 80.7, 82.5, 84.3, 85.2], "dash")]
    d.legend_row([(n, r, s) for n, r, _, s in S])
    chart(d, ["0-10", "10-20", "20-30", "30-40", "40-50", "50-60"], S, (0, 160), "ms")
    d.footer("注：横轴单位 min。long10 的块数比 long9 同窗还略多，restore 却一直更快。"
             "对块数回归，restore total 的斜率从 1.138（long9 前 60 min）降到 0.268 ms / 千块；"
             "long11 与 long10 逐段只差 0.2–2.1 ms。")
    d.save(out("rollback", "C-1-滚动保留10前60分钟restore"))


if __name__ == "__main__":
    fig_c1()
