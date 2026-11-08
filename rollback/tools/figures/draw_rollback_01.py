"""第 01 章配图：总体结构（04-1 架构全图的简版，画法共用 draw_rollback_04.draw_arch）；全书地图。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w
from draw_rollback_04 import draw_arch

# 篇章划分取自 rollback/contents.json，章名取自各章一级标题
PARTS = [
    ("第一篇", "导论与基础", [("01", "项目、交付范围与阅读方法"), ("02", "沙箱、microVM 与快照基础"),
                          ("03", "设计目标、能力边界与方案选择")]),
    ("第二篇", "核心设计与实现", [("04", "总体架构与端到端流程"), ("05", "脏页跟踪与 HDBSS"), ("06", "内存差分树"),
                            ("07", "磁盘分层与回收"), ("08", "Firecracker 接口契约"), ("09", "进程内原地回滚"),
                            ("10", "设备、网络与时间状态"), ("11", "失败语义与正确性不变量"),
                            ("12", "状态、并发、持久性与生命周期")]),
    ("第三篇", "原生快照与精确增量", [("13", "原生快照的机制与成本"), ("14", "精确增量的设计与实现"),
                              ("15", "两种快照的分工、配合与代价")]),
    ("第四篇", "验证、实测与验收", [("16", "测试体系、环境与测量口径"), ("17", "功能正确性与覆盖范围"),
                             ("18", "950 性能分档与 HDBSS 实测"), ("19", "原生 pause / resume 分档实测"),
                             ("20", "920B 性能与平台差异"), ("21", "并发与长跑"), ("22", "验收流程、结果判读与结论边界")]),
    ("第五篇", "使用、部署与运维", [("23", "SDK 快速上手"), ("24", "接口语义、错误、超时与并发调用"),
                             ("25", "构建、部署前提与能力自检"), ("26", "配置与容量规划"),
                             ("27", "日志、指标与分段计时"), ("28", "按症状排障")]),
    ("第六篇", "接手开发与后续方向", [("29", "代码入口、修改指引与开发验证"), ("30", "现状与方向")]),
]
APPX = [("A", "术语、协议与文件格式参考"), ("B", "920B 测试明细"), ("C", "工程修复案例"),
        ("D", "代码规模与统计口径"), ("E", "测试环境、轮次与工具索引")]

# 阅读路径取自第 01 章 §7 的表
PATHS = [
    ("完整学习", None, [("第一篇", "导论与基础"), ("第二篇", "核心设计与实现"), ("第三篇", "原生快照与精确增量"),
                     ("第四篇", "验证、实测与验收"), ("结合第五篇实践", "使用、部署与运维")]),
    ("评审与验收", None, [("第 01、03 章", "本章；设计目标与能力边界"), ("第 16～22 章", "验证、实测与验收"),
                       ("按证据回查", "机制章节和附录 B")]),
    ("理解原生精确增量", None, [("第 02 章 第 5、6 节", "e2b 沙箱模型；快照基本原理"), ("第 13 章", "原生快照的机制与成本"),
                          ("第 14 章", "精确增量的设计与实现"), ("第 15 章", "两种快照的分工、配合与代价"),
                          ("第 19 章", "原生 pause / resume 分档实测")]),
    ("使用接口", None, [("第 23 章", "SDK 快速上手"), ("第 24 章", "接口语义、错误、超时与并发调用"),
                     ("第 26 章", "相关配置"), ("第 28 章", "按症状排障")]),
    ("配置与诊断", None, [("第 25～28 章", "自检、配置、日志、排障"), ("第 18 章", "实测条件"),
                       ("附录 E", "测试环境、轮次与工具索引")]),
    ("接手开发", None, [("第 04 章", "总体架构与端到端流程"), ("对应机制章节", None),
                     ("第 29 章", "代码入口、修改指引与开发验证"), ("第 30 章", "现状与方向"),
                     ("附录 C", "工程修复案例")]),
]


def draw_map(path):
    d = Diagram(width=1600)
    M = d.M
    d.header("全书地图：六篇 30 章加 5 个附录，按目标选读其中一段",
             "篇章划分与首页、侧栏一致。上半部分是各篇包含哪些章，下半部分是第 01 章 §7 给出的六条阅读路径。",
             legend=[("flow", "阅读顺序")])

    # ---- 第 1 段：六篇与附录
    d.section(1, "六篇与附录", note="章名以各章标题为准")
    gap = 24
    cw = (d.W - 2 * M - 2 * gap) / 3
    LH, HEAD = 25, 62
    fill, stroke, tc, sc = RAMPS["gray"]
    y = d.y + 4
    for r in range(2):
        parts = PARTS[3 * r:3 * r + 3]
        h = HEAD + LH * max(len(p[2]) for p in parts) + 14
        for i, (pn, pt, chs) in enumerate(parts):
            x = M + i * (cw + gap)
            d.rect(x, y, cw, h, "#FFFFFF", stroke)
            d.rect(x, y, cw, 46, fill, stroke)
            d.text(x + 18, y + 29, f"{pn}  {pt}", 16.5, tc, 600)
            rng = f"第 {chs[0][0]}～{chs[-1][0]} 章"
            d.text(x + cw - 18, y + 29, rng, 13, sc, 400, "end")
            for k, (no, name) in enumerate(chs):
                yy = y + HEAD + 8 + LH * k
                d.text(x + 22, yy, no, 14, sc, 600)
                d.text(x + 56, yy, name, 14, INK)
        y += h + gap
    # 附录条
    h = 58
    d.rect(M, y, d.W - 2 * M, h, fill, stroke)
    d.text(M + 18, y + 35, "附录", 16.5, tc, 600)
    ax = M + 86
    aw = (d.W - 2 * M - 86 - 12) / len(APPX)
    for k, (no, name) in enumerate(APPX):
        d.text(ax + k * aw, y + 35, no, 14, sc, 600)
        d.text(ax + k * aw + 20, y + 35, name, 14, INK)
    d.y = y + h

    # ---- 第 2 段：按目标的阅读路径
    d.section(2, "按目标的阅读路径", highlight="本图重点", note="第 01 章 §7 的表")
    LW = 190
    cgap = 40
    x0 = M + LW
    n = 5
    bw = (d.W - M - x0 - (n - 1) * cgap) / n
    rgap = 18
    y = d.y + 4
    for k, (label, _, chips) in enumerate(PATHS):
        rh = max(64, max(d._need(t, s, bw, ts=15, ss=13, pad=18) for t, s in chips))
        if k % 2 == 0:
            d.rect(M, y - rgap / 2, d.W - 2 * M, rh + rgap, "#F7F6F2", "none", sw=0, rx=8)
        d.text(M + 18, y + rh / 2 + 5, label, 15.5, INK, 600)
        for j, (t, s) in enumerate(chips):
            x = x0 + j * (bw + cgap)
            d.block(x, y, bw, rh, t, s, "gray", "outline", ts=15, ss=13)
            if j + 1 < len(chips):
                d.arrow(x + bw + 4, y + rh / 2, x + bw + cgap - 6, y + rh / 2, "flow")
        y += rh + rgap
    d.y = y - rgap
    d.footer("注：正文从当前机制出发；附录 B 保存 920B 详细结果，附录 C 保存工程修复案例。")
    d.save(path)


if __name__ == "__main__":
    draw_arch(True, out("rollback", "01-1-总体结构"))
    draw_map(out("rollback", "01-2-全书地图"))
