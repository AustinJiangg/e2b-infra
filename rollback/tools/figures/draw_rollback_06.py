"""第 06 章 内存差分树的配图。

06-1 稀疏差分文件与位图侧车（替换 §2.1 的文本示意）。
06-2 checkpoint 树、回滚集与逐页解析（替换 §3.2 的 Mermaid；吸收旧手画图 rollback/diagrams/02-diff-tree.svg
     的分支、回滚集、逐页解析三项信息，例子取自 §4.5）。
内容只取本章正文。全图只用 teal（本方案）和灰色。
"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w  # noqa: E402

TF, TS, TT, TSUB = RAMPS["teal"]
GF, GS, GT, GSUB = RAMPS["gray"]
MONO = "'DejaVu Sans Mono','Consolas',monospace"


def mono(d, x, y, s, size=13, color=INK, anchor="middle", weight=400):
    fw = f' font-weight="{weight}"' if weight != 400 else ""
    d.o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" font-family="{MONO}"{fw} '
               f'text-anchor="{anchor}">{s}</text>')


def path_arrow(d, pts, kind="back", dash=None, sw=1.8):
    """折线箭头：用 kit 的箭头标记，语义同 kit。"""
    from diagram_kit import ARROWS
    col, kdash = ARROWS[kind]
    dash = dash or kdash
    d.kinds.add(kind)
    dd = f' stroke-dasharray="{dash}"' if dash else ""
    p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    d.o.append(f'<polyline points="{p}" fill="none" stroke="{col}" stroke-width="{sw}"{dd} '
               f'marker-end="url(#ar-{kind})"/>')


# ------------------------------------------------------------------ 06-1
def f06_1():
    d = Diagram()
    d.header("稀疏差分文件：逻辑上是完整的 guest 内存，物理上只占改动量",
             "Firecracker 以 Diff 模式写快照时，把每一个脏页写在它自己的 guest 物理偏移上，中间的干净页不写；"
             "位图 E_x 与快照在同一次 Firecracker 调用内落地，参数是 CreateSnapshotParams.dirty_bitmap_path。")
    d.section(1, "一代产物里的 mem_diff 与 mem_bitmap", highlight="本图重点")
    M, W = d.M, d.W
    LBL = 230                       # 左侧行标签宽
    x0, x1 = M + LBL, W - M
    # 段：(类型, 页号文字, 宽度)
    segs = [("hole", "0–2", 110), ("page", "3", 92), ("hole", "4–16", 220), ("page", "17", 92),
            ("page", "18", 92), ("hole", "19–98", 330), ("page", "99", 92), ("hole", "100 起", None)]
    used = sum(w for _, _, w in segs if w)
    segs[-1] = (segs[-1][0], segs[-1][1], (x1 - x0) - used)
    y = d.y + 18
    # 坐标轴：逻辑偏移 = guest 物理地址
    d.text(x0, y + 14, "偏移 0", 13, MUTED)
    d.text(x1, y + 14, "guest 内存末尾", 13, MUTED, anchor="end")
    d.text((x0 + x1) / 2, y + 14, "逻辑偏移 = guest 物理地址（页号 × 页大小）", 14, SUB, 600, "middle")
    d.arrow(x0 + 60, y + 24, x1 - 4, y + 24, "flow")
    y += 44
    # 行 1：mem_diff
    rh = 70
    d.text(M, y + rh / 2 - 2, "mem_diff", 16, INK, 600)
    d.text(M, y + rh / 2 + 18, "稀疏差分文件", 13, MUTED)
    xx = x0
    for kind, lab, w in segs:
        if kind == "page":
            d.rect(xx, y, w, rh, TF, TS, sw=1.4, rx=4)
            d.text(xx + w / 2, y + 30, "page", 13, TSUB, anchor="middle")
            d.text(xx + w / 2, y + 52, lab, 16, TT, 600, "middle")
        else:
            d.rect(xx, y, w, rh, "#FFFFFF", GS, sw=1, rx=4, dash="5 4")
            d.text(xx + w / 2, y + 30, "空洞（未分配）", 13.5, MUTED, anchor="middle")
            d.text(xx + w / 2, y + 52, "页 " + lab, 13, MUTED, anchor="middle")
        xx += w
    y += rh + 14
    # 行 2：mem_bitmap
    bh = 46
    d.text(M, y + bh / 2 - 2, "mem_bitmap", 16, INK, 600)
    d.text(M, y + bh / 2 + 18, "位图 E_x，FCDB 格式", 13, MUTED)
    xx = x0
    for kind, lab, w in segs:
        if kind == "page":
            d.rect(xx, y, w, bh, TF, TS, sw=1.4, rx=4)
            mono(d, xx + w / 2, y + 29, "1", 16, TT, weight=600)
        else:
            d.rect(xx, y, w, bh, "#FFFFFF", GS, sw=1, rx=4)
            mono(d, xx + w / 2, y + 29, "0 … 0", 14, MUTED)
        xx += w
    y += bh + 26
    # 量度线：逻辑大小 / 物理占用
    d.o.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="{GS}" stroke-width="1.2"/>')
    for xv in (x0, x1):
        d.o.append(f'<line x1="{xv}" y1="{y - 7}" x2="{xv}" y2="{y + 7}" stroke="{GS}" stroke-width="1.2"/>')
    d.text((x0 + x1) / 2, y + 26, "文件逻辑大小：等于 guest 内存", 14.5, INK, 600, "middle")
    d.text((x0 + x1) / 2, y + 50, "物理占用：约等于脏页总量（只有 page 格分配了物理块）", 14.5, TT, 600, "middle")
    d.text(M, y + 26, "两个大小", 16, INK, 600)
    d.y = y + 64
    # 三条说明
    bw = (W - 2 * M - 2 * 30) / 3
    notes = [("读空洞返回零", "SEEK_DATA / SEEK_HOLE 可以枚举出真正有数据的区间"),
             ("长度校验天然满足", "Firecracker 的回滚端点校验内存文件长度等于 guest 内存大小，不需要额外元数据"),
             ("全量快照也写位图", "内容全 1；增量条目的 E_x = parent(x) 到 x 之间 guest 写过的页")]
    h = max(d._need(t, s, bw) for t, s in notes)
    for i, (t, s) in enumerate(notes):
        d.block(M + i * (bw + 30), d.y + 16, bw, h, t, s, "gray")
    d.y += 16 + h

    d.section(2, "一代产物的五个文件", note="目录布局见第 4 章 §5")
    d.y -= 0
    files = [("snapfile", "vCPU、GIC、设备状态", "gray"), ("mem_diff", "稀疏差分", "teal"),
             ("mem_bitmap", "E_x，FCDB 格式", "teal"), ("rootfs.header", "磁盘视图（第 7 章）", "gray"),
             ("manifest.json", "条目记录", "gray")]
    gap = 24
    bw = (W - 2 * M - 4 * gap) / 5
    h = 84
    for i, (t, s, r) in enumerate(files):
        d.block(M + i * (bw + gap), d.y, bw, h, t, s, r)
    d.y += h
    d.callout("checkpoint 成本 O(本代脏页)：与虚机规格无关，与历史长度无关，与文件系统无关", "teal")
    d.footer("注：图中页号 3、17、18、99 只是示意位置。位图与快照描述的是同一件事，省掉 orchestrator 再发一轮「取脏页位图」的往返。")
    d.save(out("rollback", "06-1-稀疏差分文件与位图侧车"))


# ------------------------------------------------------------------ 06-2
def node(d, x, y, w, h, name, role, bits, ramp="gray", style="fill"):
    f, s, tc, sc = RAMPS[ramp]
    if style == "fill":
        d.rect(x, y, w, h, f, s, sw=1.4)
    else:
        d.rect(x, y, w, h, "#FFFFFF", s, sw=2.2)
    d.text(x + w / 2, y + 32, name, 20, tc, 600, "middle")
    d.text(x + w / 2, y + 56, role, 13.5, sc, anchor="middle")
    d.text(x + w / 2, y + 80, bits, 13.5, sc, 600, "middle")


def f06_2():
    d = Diagram()
    d.header("checkpoint 树：restore 不剪枝，回滚集沿两条分支走到最近公共祖先",
             "打了 ck1 → ck2 → ck3，restore 到 ck1，再打 ck4 → ck5，于是长出两条分支。拍摄时间的先后不能代替内存增量的依赖关系；"
             "restore 本身不增加树节点。",
             legend=[("flow", "父 → 子（子条目的 ParentID 指向父）"), ("back", "restore 操作：① 虚线是之前那次，② 粗线是本次")])
    d.section(1, "一个沙箱的 checkpoint 树", note="节点里的 E 是 §4.5 例子中各代的纪元位图，设 guest 有 16 页")
    M, W = d.M, d.W
    nw, nh = 250, 100
    top = d.y + 70
    bot = top + 250
    mid = (top + bot) / 2
    X1, X2, X3 = M + 20, M + 350, M + 680
    node(d, X1, mid, nw, nh, "ck1", "全量捕获（树根）", "E = 全 1", "teal")
    node(d, X2, top, nw, nh, "ck2", "增量，ParentID = ck1", "E = {1, 5, 9}")
    node(d, X3, top, nw, nh, "ck3", "增量，ParentID = ck2", "E = {5, 7}")
    node(d, X2, bot, nw, nh, "ck4", "增量，ParentID = ck1", "E = {2, 5}")
    node(d, X3, bot, nw, nh, "ck5", "当前基准", "E = {9, 11}", "teal", "outline")
    # 父 → 子
    d.arrow(X1 + nw + 2, mid + 30, X2 - 6, top + nh / 2)
    d.arrow(X1 + nw + 2, mid + 70, X2 - 6, bot + nh / 2)
    d.arrow(X2 + nw + 2, top + nh / 2, X3 - 6, top + nh / 2)
    d.arrow(X2 + nw + 2, bot + nh / 2, X3 - 6, bot + nh / 2)
    # ① 曾 restore 到 ck1：ck3 顶 → 上方 → ck1 顶
    ya = top - 34
    path_arrow(d, [(X3 + nw / 2, top - 2), (X3 + nw / 2, ya), (X1 + nw / 2, ya), (X1 + nw / 2, mid - 6)], dash="6 4")
    d.text(X1 + nw / 2 + 14, ya - 10, "① 曾 restore 到 ck1：旁支 ck2 / ck3 保留，之后的 ck4 相对 ck1",
           13.5, RAMPS["teal"][3], 600)
    # ② 本次：ck5 → ck3
    cx = X3 + nw / 2
    path_arrow(d, [(cx, bot - 2), (cx, top + nh + 7)], sw=3)
    d.text(cx + 14, (top + nh + bot) / 2 - 4, "② 本次 restore：ck5 → ck3", 13.5, RAMPS["teal"][3], 600)
    d.text(cx + 14, (top + nh + bot) / 2 + 16, "跨分支，经最近公共祖先 ck1", 13.5, RAMPS["teal"][3], 600)
    # 右侧：树带来的三件事
    rx = X3 + nw + 230
    rw = W - M - rx
    d.text(rx, top - 6, "保留依赖关系后可以做", 15, INK, 600)
    ops = [("回滚后前滚", "回到 ck1 之后，仍可恢复到 ck2 / ck3"),
           ("跨分支跳转", "从 ck5 直接到 ck3，经最近公共祖先"),
           ("删除中间节点不打断后代", "被后代依赖的节点转为隐藏保留（§10）")]
    oy = top + 10
    oh = (bot + nh - oy - 2 * 16) / 3
    for t, s in ops:
        d.block(rx, oy, rw, oh, t, s, "gray")
        oy += oh + 16
    d.y = bot + nh

    d.section(2, "restore ck5 → ck3 在宿主侧怎么算", highlight="本图重点", note="当前基准 b = ck5，活跃脏页 L = {3}，目标 t = ck3")
    d.flow([
        Step("求路径", "目标链 [ck3, ck2, ck1]；从 ck5 上爬到 ck1 ∈ 目标链，A = ck1。ck1 本身不在路径里",
             "orchestrator"),
        Step("求回滚集", "两段路径上每一代的纪元位图求并，再并上活跃脏页 L", "orchestrator"),
        Step("逐页解析内容", "沿目标链 ck3 → ck2 → ck1 找第一个含该页的内容位图", "orchestrator"),
        Step("物化并交给 Firecracker", "写 revert_bitmap.tmp 与 revert_mem.tmp，调用 PUT /snapshot/rollback",
             "orchestrator"),
    ], min_h=130, gap=40)

    d.section(3, "16 页逐页看：为什么要回写，内容从哪来", note="两个问题用的是两组不同的位图")
    LBL = 300
    gx0 = M + LBL
    n = 16
    cw = (W - M - gx0) / n
    y = d.y + 8
    for p in range(n):
        d.text(gx0 + p * cw + cw / 2, y + 16, f"页 {p}", 13, MUTED, anchor="middle")
    y += 28
    rows = [("path(b→A)：ck5", {9, 11}), ("path(b→A)：ck4", {2, 5}),
            ("path(t→A)：ck3", {5, 7}), ("path(t→A)：ck2", {1, 5, 9}),
            ("活跃脏页 L", {3})]
    rh, rg = 36, 6
    for lab, bits in rows:
        d.text(M, y + rh / 2 + 5, lab, 14.5, INK, 600)
        for p in range(n):
            x = gx0 + p * cw
            if p in bits:
                d.rect(x + 2, y, cw - 4, rh, TF, TS, sw=1, rx=4)
                mono(d, x + cw / 2, y + rh / 2 + 5, "1", 14, TT, weight=600)
            else:
                d.rect(x + 2, y, cw - 4, rh, "#FFFFFF", "#E5E3DB", sw=1, rx=4)
        y += rh + rg
    rg = 12
    revert = {1, 2, 3, 5, 7, 9, 11}
    src = {1: "ck2", 2: "ck1", 3: "ck1", 5: "ck3", 7: "ck3", 9: "ck2", 11: "ck1"}
    y += 8
    d.o.append(f'<line x1="{M}" y1="{y - 7}" x2="{W - M}" y2="{y - 7}" stroke="#D3D1C7" stroke-width="1"/>')
    d.text(M, y + rh / 2 - 3, "回滚集（上面五行求并）", 14.5, INK, 600)
    d.text(M, y + rh / 2 + 15, "= {1, 2, 3, 5, 7, 9, 11}", 12.5, MUTED)
    for p in range(n):
        x = gx0 + p * cw
        if p in revert:
            d.rect(x + 2, y, cw - 4, rh, TS, TS, sw=1, rx=4)
            mono(d, x + cw / 2, y + rh / 2 + 5, "1", 14, "#FFFFFF", weight=600)
        else:
            d.rect(x + 2, y, cw - 4, rh, "#FFFFFF", "#E5E3DB", sw=1, rx=4)
    y += rh + rg
    d.text(M, y + rh / 2 - 3, "内容取自", 14.5, INK, 600)
    d.text(M, y + rh / 2 + 15, "沿 ck3 → ck2 → ck1 的第一个命中", 12.5, MUTED)
    for p in range(n):
        x = gx0 + p * cw
        if p in src:
            d.rect(x + 2, y, cw - 4, rh, "#FFFFFF", TS, sw=1.2, rx=4)
            d.text(x + cw / 2, y + rh / 2 + 5, src[p], 13.5, TT, 600, "middle")
        else:
            d.text(x + cw / 2, y + rh / 2 + 5, "不动", 12.5, MUTED, anchor="middle")
    y += rh + rg
    # extent 行
    d.text(M, y + rh / 2 - 3, "合并成 extent", 14.5, INK, 600)
    d.text(M, y + rh / 2 + 15, "连续且同源的页一次读写", 12.5, MUTED)
    ext = [(1, 1), (2, 3), (5, 5), (7, 7), (9, 9), (11, 11)]
    for k, (a, b) in enumerate(ext):
        x = gx0 + a * cw + 2
        w = (b - a + 1) * cw - 4
        d.rect(x, y, w, rh, GF, GS, sw=1, rx=4)
        d.text(x + w / 2, y + rh / 2 + 5, f"extent {k + 1}", 13, GT, 600, "middle")
    y += rh
    d.y = y
    d.callout("页 3 进回滚集，是因为现在脏（L）；它在 ck3 时刻的内容却要一路回溯到全量根 ck1 才找得到", "teal")
    d.footer("注：ck1 是最近公共祖先，它的全 1 位图不参与回滚集。页 4、6、8 等在分叉之后两侧都没动，两个时刻内容一致，不回写。"
             "恢复成本与链深无关：位图在解析前已加载进内存，文件 I/O 只发生在命中的那一代、按 extent 进行。")
    d.save(out("rollback", "06-2-checkpoint树与回滚集"))


if __name__ == "__main__":
    f06_1()
    f06_2()
