"""进程级附录 D 的两张图。

图 D-1（§3 归因链，讲解图）：节点和连线取自 D-restore-slowness-case.md §3 原 mermaid flowchart，框内的数字取自同附录 §1、§2、§3。
颜色：灰色 = 现象、方法、观察、假设；purple = 找到的原因（进程级手册的色阶）。
图 D-2（§5 修复前后，数据图）：数字全部取自 §5.1 表格与正文，不另加数据。
颜色：初始实现 coral，只修了 gsd 侧 gray，再修内核侧（最终）purple。
"""
from common import *  # noqa: F401,F403
from draw_rollback_20 import gbars

COLS, GAP_X, GAP_Y = 5, 40, 52

# 节点：键 -> (行, 列, 标题, 副标题, 执行者标签, 色阶, 样式, tag)
N = {
    "A": (0, 0, "现象一：restore 慢且抖", "最高一档端到端约 150～230 ms", "现象", "gray", "outline", None),
    "B": (0, 1, "隔离阶梯：差在调用上下文", "干净环境 35～39 ms，经 gsd 调用 113～170 ms", "方法", "gray", "fill", None),
    "C": (0, 2, "日志里程碑对齐", "gsd 调用比干净环境多出两段停顿", "方法", "gray", "fill", None),
    "E": (0, 3, "停顿二：打开映射文件之前", "约 15 ms 是 gsd 独有的", "观察", "gray", "fill", None),
    "G": (0, 4, "假设：切层后目录缓存是冷的", None, "假设", "gray", "dashed", None),
    "N": (1, 1, "原因三：杀进程后 10 ms 轮询", "kill_ms 总落在 10 或 20 附近", "原因", "purple", "fill", None),
    "D": (1, 2, "停顿一：紧跟 cgroup 迁移", "0～57 ms，随机出现", "观察", "gray", "fill", None),
    "F": (1, 3, "原因二：迁移等 RCU 宽限期", None, "原因", "purple", "fill", None),
    "H": (1, 4, "预热只省 3 ms", "假设不成立", "对照", "gray", "dashed", None),
    "K": (2, 0, "现象二：连续 restore 后失联", "连续 5～7 次后 envd 不应答", "现象", "gray", "outline", None),
    "L": (2, 1, "根盘每次增加，直到写满", "每次 restore 根盘净增约 31 MB，第 12 次时写满", "观察", "gray", "fill", None),
    "J": (2, 3, "原因一：ovl_mmap 误做 copy-up", None, "原因", "purple", "fill", ("真根因：同时解释两个现象", "purple")),
    "I": (2, 4, "查看新 upper 目录", "里面是整份的二进制", "观察", "gray", "fill", None),
    "M": (3, 1, "原因四：临时目录不回收", None, "原因", "purple", "fill", None),
    "O": (3, 3, "原因五：查找路径上的调试打印", None, "原因", "purple", "fill", None),
}
# 连线：(起点, 终点)，方向由两框的相对位置决定
E = [("A", "B"), ("B", "C"), ("C", "E"), ("E", "G"), ("B", "N"), ("C", "D"), ("D", "F"),
     ("G", "H"), ("H", "I"), ("I", "J"), ("K", "L"), ("L", "J"), ("L", "M"), ("J", "O")]


def fig_d1():
    d = Diagram(width=1600)
    d.header("restore 慢与连续 restore 后失联，大半出自同一处内核代码",
             "从两个现象出发，经隔离阶梯、日志里程碑对齐找到五个原因。停顿二先被归到目录缓存是冷的，"
             "预热对照推翻这个假设后，查看新换上的 upper 目录才找到根因 ovl_mmap 对私有可写映射做 copy-up。")
    M = d.M
    bw = (d.W - 2 * M - (COLS - 1) * GAP_X) / COLS
    nrows = 1 + max(v[0] for v in N.values())
    rh = [0] * nrows
    for r, c, t, s, a, ramp, st, tag in N.values():
        rh[r] = max(rh[r], 96, d._need(t, s, bw, a, tag))
    y0 = d.y + 40
    ry = [y0 + sum(rh[:k]) + GAP_Y * k for k in range(nrows)]
    pos = {}
    for k, (r, c, t, s, a, ramp, st, tag) in N.items():
        x, y = M + c * (bw + GAP_X), ry[r]
        d.block(x, y, bw, rh[r], t, s, ramp, st, actor=a, tag=tag)
        pos[k] = (x, y, rh[r])
    for a, b in E:
        xa, ya, ha = pos[a]
        xb, yb, hb = pos[b]
        if abs(ya - yb) < 1:          # 同一行：横向
            cy = ya + ha / 2
            if xb > xa:
                d.arrow(xa + bw + 4, cy, xb - 6, cy)
            else:
                d.arrow(xa - 4, cy, xb + bw + 6, cy)
        else:                         # 同一列：纵向
            cx = xa + bw / 2
            d.arrow(cx, ya + ha + 4, cx, yb - 6)
    d.y = ry[-1] + rh[-1]
    d.footer("注：数字来自旧 920B（已退役）上初始实现的微基准，口径见本附录开头，不能与正文的判定值比较。")
    d.save(out("criu", "D-1-restore慢的归因链"))


# ---------------------------------------------------------------- 图 D-2
def fig_d2():
    d = Chart()
    d.header("五个原因修完后，restore 的 total_ms p50 从 196～206 降到 85～89 ms",
             "旧 920B（已退役），2026-09-11，195 MB 单进程、guest 内直连 gsd 的微基准，THP 为 never，单位 ms。"
             "三档按修复先后排列。虚线框是原文给出的区间或两个样本。")
    S0, S1, S2 = "coral", "gray", "purple"
    d.legend_row([("初始实现", S0, "bar"), ("修掉原因二、三、四（gsd 侧）", S1, "bar"),
                  ("再修掉原因一、五（内核侧）", S2, "bar")])
    G = [
        ("cgroup 迁移等待", "原因二", [(38, S0, "38", None), (0.1, S1, "0.1", None), (0.1, S2, "0.1", None)]),
        ("文件校验前停顿", "原因一", [(18, S0, "18", None), (16, S1, "16", None), ((0.5, 0.8), S2, "0.5～0.8", None)]),
        ("kill_ms", "原因三", [(20.5, S0, "20.5", None), (11, S1, "11", None), ((11, 12), S2, "11～12", None)]),
        ("criu_restore_ms p50", None, [(181, S0, "181", None), (109, S1, "109", None),
                                       ((74, 76), S2, "74～76", None)]),
        ("total_ms p50", None, [((196, 206), S0, "196 / 206", "连续 14 次：第 12 次根盘写满"),
                                (121, S1, "121", "通过，根盘每次仍 +17 MB"),
                                ((85, 89), S2, "85～89", "通过，根盘恒定 289 MB")]),
    ]
    gbars(d, G, ("lin", 220), ticks=[0, 50, 100, 150, 200], tick_fmt=lambda t: f"{t:g} ms", note_w=330)
    d.footer("注：初始实现两个沙箱各恢复 4 次，total_ms p50 分别为 196、206 ms。这是旧 920B 同口径微基准，不能与正文 20 章的分档结果相减。")
    d.save(out("criu", "D-2-restore修复前后"))


if __name__ == "__main__":
    fig_d1()
    fig_d2()
