"""第 23 章配图：23-1 SDK 视角的 checkpoint 树。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w


def fig1():
    d = Diagram(width=1600)
    M = d.M
    d.header("一个沙箱的 checkpoint 构成一棵树：restore 不剪枝，任意两点之间直接跳",
             "从 deps 分出两条分支：try-A 直接拍；try-B 是 restore 到 deps 之后拍的，挂在 deps 下面。"
             "接口只返回按时间排序的平表，父子关系由调用方自己记。",
             legend=[("flow", "父节点 → 子节点"), ("back", "restore：跳到另一个 checkpoint")])

    d.section(1, "树与 list() 看到的平表", highlight="本图重点")
    top = d.y + 4
    # 左：树
    tx, tw = M, 1040
    th = 430
    d.rect(tx, top, tw, th, "#FBFBF9", "#D3D1C7", sw=1, rx=16)
    d.text(tx + 24, top + 32, "checkpoint 之间的实际父子关系", 16.5, INK, 600)
    bw, bh = 280, 76
    deps = (tx + tw / 2 - bw / 2, top + 60)
    tra = (tx + 70, top + 196)
    trb = (tx + tw - 70 - bw, top + 196)
    trbf = (trb[0], top + 330)
    d.block(*deps, bw, bh, "deps", "第一次 checkpoint，full", "teal", "fill")
    d.block(*tra, bw, bh, "try-A", "从 deps 往下接着做，分支 A", "teal", "fill")
    d.block(*trb, bw, bh, "try-B", "restore 到 deps 之后拍的；incremental：相对 deps 的增量", "teal", "fill")
    d.block(*trbf, bw, bh, "try-B-fixed", None, "teal", "fill")
    # 父 → 子
    d.arrow(deps[0] + 60, deps[1] + bh + 2, tra[0] + bw / 2 + 40, tra[1] - 5, "flow")
    d.arrow(deps[0] + bw - 60, deps[1] + bh + 2, trb[0] + bw / 2 - 40, trb[1] - 5, "flow")
    d.arrow(trb[0] + bw / 2, trb[1] + bh + 2, trbf[0] + bw / 2, trbf[1] - 5, "flow")
    # 跨分支跳
    y_j = trbf[1] + bh / 2
    d.arrow(trbf[0] - 4, y_j, tra[0] + bw / 2, tra[1] + bh + 5, "back")
    lab = "restore(try-A)：跨分支直接跳"
    d.text((trbf[0] + tra[0] + bw / 2) / 2 + 10, y_j + 14, lab, 13.5, RAMPS["teal"][3], 600, "middle")

    # 右：list() 平表
    lx = tx + tw + 28
    lw = d.W - M - lx
    d.rect(lx, top, lw, th, "#FBFBF9", "#D3D1C7", sw=1, rx=16)
    d.text(lx + 24, top + 32, "sbx.checkpoint.list() 返回的", 16.5, INK, 600)
    d.text(lx + 24, top + 56, "按时间排序的平表，CheckpointInfo 里没有父节点字段", 13, MUTED)
    rows = [("deps", "mem_mode = full"), ("try-A", None), ("try-B", "mem_mode = incremental"), ("try-B-fixed", None)]
    ry = top + 82
    for k, (name, sub) in enumerate(rows):
        d.block(lx + 24, ry, lw - 48, 56, name, sub, "teal", "outline", ts=15.5, ss=13)
        ry += 56 + 18
    d.text(lx + 24, ry + 10, "调用方自己记：parent = {try-A: deps, try-B: deps}", 13.5, SUB, 600)
    d.y = top + th

    d.section(2, "四条规则", note="这几条决定树怎么长出来")
    cards = [("父节点", "拍下时沙箱所处的那个点：上一个 checkpoint，或上一次 restore 的目标"),
             ("restore 不剪枝", "比目标更新的 checkpoint 原样保留，仍然可以 restore 过去（前滚）"),
             ("restore 之后再 checkpoint", "新节点挂在 restore 目标下面，树长出一条新分支"),
             ("任意两点之间直接跳", "不管在不在同一条分支上；代价取决于两点之间被改过的页有多少")]
    gap = 24
    cw = (d.W - 2 * M - 3 * gap) / 4
    h = max(d._need(t, s, cw) for t, s in cards)
    for i, (t, s) in enumerate(cards):
        d.block(M + i * (cw + gap), d.y, cw, h, t, s, "gray", "outline")
    d.y += h
    d.footer("注：树只属于这一个沙箱的这一代：沙箱销毁、orchestrator 重启、经原生 pause / resume 换代，树都随之作废。")
    d.save(out("rollback", "23-1-SDK视角的checkpoint树"))


if __name__ == "__main__":
    fig1()
