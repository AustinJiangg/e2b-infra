"""第 14 章配图：14-1 原生内存增量的三层；14-2 修复前为什么不精确（判据与每代换入）；
14-3 2 MiB 归并的地板；14-4 一次 2 MiB 缺页的三级取源。

14-2、14-3 的画法参考 hdbss-dirty-tracking 的 31-1、32-1，内容按本章正文。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, TONES, wrap, text_w


def fig1():
    d = Diagram(width=1600)
    d.header("原生内存增量的三层：判据再精细，也被 2 MiB 的 guest 页锁在 2 MiB 块上",
             "修复前原生 pause 的内存路径按“谁决定粒度”切成判、存、填三层。"
             "③ 的硬检查要求 guest 页大小等于 header 的 BlockSize，把三层的粒度连在一起。",
             legend=[("flow", "执行顺序"), ("warn", "粒度约束的传递方向")])

    d.section(1, "三层按执行顺序", note="pause 时做 ① ②，恢复时做 ③")
    d.flow([
        Step("① 判：哪些页要导出", "粒度由判据接口返回的位图决定",
             actor="Firecracker", tag=("修复前：GET /memory/dirty，mincore 驻留 ∧ pagemap bit 57 未置位，按 BlockSize 取", "coral")),
        Step("② 存：按块拷进紧凑差分文件", "header 记下每一块在哪一代的哪个偏移；粒度由 header 的 BlockSize 决定，逐代继承",
             actor="orchestrator", tag=("修复前：模板 header 是 2 MiB，之后每代都是 2 MiB", "coral")),
        Step("③ 填：恢复时 uffd 缺页", "按 guest 页从映射链取内容，UFFDIO_COPY 进去；粒度是 guest 页大小（hugetlbfs 2 MiB）",
             actor="orchestrator", tag=("NewUserfaultfdFromFd 硬性要求 guest 页大小 == BlockSize，否则拒绝启动", "coral")),
    ], min_h=150)

    d.section(2, "粒度怎样被锁在一起", highlight="本图重点", note="从 guest 页出发，约束一路传到判据")
    steps = [
        ("guest 页 = 2 MiB", "hugetlbfs 大页背衬"),
        ("③ 一次缺页填一整页", "只能按 2 MiB 填，UFFDIO_COPY 必须给满"),
        ("③ 的硬检查", "guest 页大小 == BlockSize"),
        ("② BlockSize = 2 MiB", "写在 header 里，逐代继承"),
        ("① 位图再精细", "也要归并到 2 MiB 块导出"),
    ]
    n, gap, M = len(steps), 44, d.M
    bw = (d.W - 2 * M - (n - 1) * gap) / n
    h = max(d._need(t, s, bw) for t, s in steps)
    y = d.y
    for i, (t, s) in enumerate(steps):
        x = M + i * (bw + gap)
        d.block(x, y, bw, h, t, s, "coral" if i == 2 else "gray", "fill" if i == 2 else "outline")
        if i < n - 1:
            d.arrow(x + bw + 4, y + h / 2, x + bw + gap - 6, y + h / 2, "warn")
    d.y = y + h
    d.callout("① 决定“读算不算脏”，② 和 ③ 决定“最小能存多细”：两件事要分开治", "gray")
    d.footer("注：修复后 ① 换成写跟踪位图（4 KiB），② 的 BlockSize 降到 4 KiB，③ 的硬检查从“相等”放宽为“整除”，"
             "缺页仍按 2 MiB，内容从 4 KiB 映射拼（§8）。")
    d.save(out("rollback", "14-1-原生内存增量的三层"))


# ------------------------------------------------------------------ 14-2、14-3 共用的小图元
PANEL_F, PANEL_S = "#FBFBF9", "#D3D1C7"
LINE = "#D3D1C7"
OLD_F, OLD_S = "#E8E6DF", "#B4B2A9"       # 模板（第 0 代）
READ_F, READ_S = "#F7F6F2", "#888780"     # guest 只读过（经 uffd 换入）
WRITE_F = "#5F5E5A"                        # guest 写过


def panel(d, x, y, w, h):
    d.rect(x, y, w, h, PANEL_F, PANEL_S, sw=1, rx=14)


def hline(d, x0, x1, y):
    d.o.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x1:.1f}" y2="{y:.1f}" stroke="#E5E3DB" stroke-width="1"/>')


def cell(d, x, y, w, h, fill="#FFFFFF", stroke=LINE, label=None, tc=INK, dash=None, size=12.5, rx=4):
    d.rect(x, y, w, h, fill, stroke, sw=1, rx=rx, dash=dash)
    if label:
        d.text(x + w / 2, y + h / 2 + size * 0.36, label, size, tc, 600, "middle")


def row_label(d, x, y, name, sub, color, width=270):
    d.text(x, y, name, 15.5, color, 600)
    for k, s in enumerate(wrap(sub, width, 12.5)):
        d.text(x, y + 21 + 18 * k, s, 12.5, MUTED)


# 一代里 24 个 2 MiB 块的示意：0 没碰过，1 只读过（经 uffd 换入），2 写过
GEN = [0, 1, 2, 0, 1, 1, 0, 2, 1, 0, 1, 0, 1, 0, 2, 1, 0, 0, 1, 1, 2, 0, 1, 0]
N, PW, PG, X0 = 24, 38, 7, 372


def cx(i):
    return X0 + i * (PW + PG)


def fig2():
    d = Diagram(width=1600)
    M, W = d.M, d.W
    d.header("修复前为什么不精确：ARM 上判据塌缩成“驻留即脏”，而每一代都要把工作集重新换入",
             "导出集合只需满足一条：写过的页一个都不能漏，多导只是浪费。"
             "三种判据的差别在于怎样对待经 uffd 读缺页换入的页；原生每代是新进程，换入的页在驻留判据下全部算脏。")

    # ======== 1
    d.section(1, "同一代里 guest 碰过的块，三种判据各导出哪些", note="以 24 个 2 MiB 块为例，示意")
    T = d.y + 4
    PH = 520
    panel(d, M, T, W - 2 * M, PH)
    d.text(70, T + 52, "这一代 guest", 15, INK, 600)
    d.text(70, T + 72, "碰了哪些块", 15, INK, 600)
    look = {0: ("#FFFFFF", LINE, None, INK), 1: (READ_F, READ_S, "读", INK), 2: (WRITE_F, WRITE_F, "写", "#FFFFFF")}
    for i, v in enumerate(GEN):
        f, s, lab, tc = look[v]
        cell(d, cx(i), T + 34, PW, 44, f, s, lab, tc, size=13)
    lx = X0
    for v, name in [(0, "没碰过：恢复后不缺页，内容留在父代"), (1, "只读过：读缺页经 uffd 换入"), (2, "写过")]:
        f, s, _, _ = look[v]
        d.rect(lx, T + 96, 18, 18, f, s, sw=1, rx=3)
        d.text(lx + 26, T + 110, name, 13, SUB)
        lx += 60 + text_w(name, 13)
    hline(d, 70, W - 70, T + 134)
    rows = [
        ("x86 上游：uffd 写保护", "驻留 ∧ 未被写保护（pagemap）", "gray", lambda v: v == 2,
         ("只导出写过的块", "good"), "读缺页填进来的页保留写保护，不算脏"),
        ("ARM 适配基线：驻留判据", "faultPage 里 UFFDIO_COPY_MODE_WP 被注释掉，第二项恒真", "coral", lambda v: v in (1, 2),
         ("驻留即脏：只读过的块也导出", "bad"), "驻留是写过的超集：只会多导，不会少导"),
        ("写跟踪位图", "KVM / HDBSS 脏页日志 ∪ Firecracker 用户态位图", "teal", lambda v: v == 2,
         ("只导出写过的块", "good"), "经 uffd 填进来的页不在位图里；修复后 ARM 用它（§8.1）"),
    ]
    for k, (name, sub, ramp, pick, prec, note) in enumerate(rows):
        ry = T + 156 + k * 120
        f, s, tc, sc = RAMPS[ramp]
        solid = "#888780" if ramp == "gray" else s
        row_label(d, 70, ry + 26, name, sub, tc)
        for i, v in enumerate(GEN):
            if pick(v):
                cell(d, cx(i), ry + 8, PW, 36, solid, solid)
            else:
                cell(d, cx(i), ry + 8, PW, 36, "#FFFFFF", LINE, dash="3 3")
        cnt = sum(1 for v in GEN if pick(v))
        d.text(cx(N) + 8, ry + 32, f"{cnt} 块", 15, tc, 600)
        col, wgt = TONES[prec[1]]
        d.text(X0, ry + 72, prec[0], 13.5, col, wgt)
        d.text(X0 + 290, ry + 72, note, 13.5, SUB)
    d.text(70, T + PH - 22, "实心格 = 导出到差分文件；虚线格 = 不导出，恢复时从父代读", 12.5, MUTED)
    d.y = T + PH

    # ======== 2
    d.section(2, "每一代都是新进程：工作集每代重新换入，驻留判据下每代再导出一遍", highlight="本图重点",
              note="以驻留判据为例")
    T2 = d.y + 4
    PH2 = 424
    panel(d, M, T2, W - 2 * M, PH2)
    gen1 = {i for i, v in enumerate(GEN) if v}             # 第 1 代碰过的块
    gen2 = (gen1 - {5, 18}) | {3, 16}                       # 第 2 代碰过的块：工作集大体相同
    w2 = {9}                                                # 第 2 代只写了这一块
    gen2 |= w2
    G1F, G1S = RAMPS["coral"][0], RAMPS["coral"][1]
    G2 = RAMPS["coral"][1]
    bars = [
        ("第 0 代：模板", "全量", lambda i: (OLD_F, OLD_S, None, INK)),
        ("第 1 代差分", "新进程，碰过的块全部换入、全部导出",
         lambda i: (G1F, G1S, None, INK) if i in gen1 else None),
        ("第 2 代差分", "又是新进程：工作集再换入一遍；只写了 1 块",
         lambda i: (G2, G2, "写" if i in w2 else None, "#FFFFFF") if i in gen2 else None),
        ("第 2 代 header", "每块指向最近导出它的那一代",
         lambda i: (G2, G2, "2", "#FFFFFF") if i in gen2 else ((G1F, G1S, "1", RAMPS["coral"][2]) if i in gen1
                                                             else (OLD_F, OLD_S, "0", SUB))),
    ]
    for k, (name, sub, fn) in enumerate(bars):
        ry = T2 + 28 + k * 86
        if k == 3:
            ry += 16
            hline(d, 70, W - 70, ry - 14)
        row_label(d, 70, ry + 22, name, sub, INK)
        for i in range(N):
            r = fn(i)
            if r:
                cell(d, cx(i), ry + 4, PW, 40, r[0], r[1], r[2], r[3], size=13)
            else:
                cell(d, cx(i), ry + 4, PW, 40, "#FFFFFF", LINE, dash="3 3")
        if k == 1:
            d.text(cx(N) + 8, ry + 30, f"{len(gen1)} 块", 15, RAMPS["coral"][2], 600)
        if k == 2:
            d.text(cx(N) + 8, ry + 30, f"{len(gen2)} 块", 15, RAMPS["coral"][2], 600)
    yb = T2 + 28 + 3 * 86 + 16 + 64
    for k, s in enumerate(wrap("差分文件是紧凑的：只放导出的块，一块紧挨一块。新一代 header 由父代映射与本代映射合并（MergeMappings）："
                               "本代导出过的区间指向本代，其余沿用父代。任何一代的 header 都能直接回答“这一块去哪一代读”，不用逐代往回找。",
                               1100, 13)):
        d.text(X0, yb + 20 * k, s, 13, SUB)
    d.y = T2 + PH2
    d.callout("每一代增量的下限约等于这一代 guest 碰过的全部页，与改了多少无关；写跟踪位图下，第 2 代只导出写过的那 1 块", "coral")

    # ======== 3
    d.section(3, "三种判据")
    c = d.compare([("x86 上游：uffd 写保护", "驻留 ∧ 未被写保护", "gray"),
                   ("ARM 适配基线：驻留判据", "修复前", "coral"),
                   ("写跟踪位图", "KVM / HDBSS 日志", "teal")], label_width=150)
    c.summary("对比小结", [
        ("读缺页换入的页", [("保留写保护，不算脏", "good"), ("驻留即算脏", "bad"), ("不在位图里", "good")]),
        ("导出集合", ["写过的块", ("驻留的块：写过的超集", "bad"), "写过的块"]),
        ("会不会少导", [("不会", "good"), ("不会：方向安全", "good"), ("不会，叠加 checkpoint 时靠 §9 补", "good")]),
        ("在 arm64 6.6 上", [("不可用：没有 uffd 写保护", "bad"), "修复前一直在用", ("修复后开了跟踪时用", "good")]),
    ])
    c.end()
    d.footer("注：两个放大叠在一起才是附录 C“改 12 MiB，导出 148 MiB”的来源。修复前块是 2 MiB（header 的 BlockSize），"
             "所以图里按块画；块为什么是 2 MiB、只换判据能省多少，见图 14-3。")
    d.save(out("rollback", "14-2-修复前为什么不精确"))


def fig3():
    d = Diagram(width=1600)
    M, W = d.M, d.W
    d.header("只换判据的地板：写跟踪是 4 KiB，导出却按 2 MiB 块，本底散写归并后约 130 MiB",
             "把写跟踪位图按 2 MiB 归并：一个块里只要有一个 4 KiB 页写过，整块导出。"
             "块之所以是 2 MiB，是因为差分块必须等于 guest 页大小，而 guest 页是 hugetlbfs 2 MiB 大页。",
             legend=[("warn", "按 2 MiB 归并；粒度约束的传递方向")])
    CO = RAMPS["coral"]

    # ======== 1
    d.section(1, "写了十几个 4 KiB 页，整个 2 MiB 块都要导出", highlight="本图重点", note="示意")
    T = d.y + 4
    PH = 470
    panel(d, M, T, W - 2 * M, PH)
    # 上：guest 物理内存里散落的写
    d.text(70, T + 40, "guest 物理内存，按 2 MiB 块切开", 15, INK, 600)
    d.text(70, T + 60, "空转时的本底散写：内核的页表更新、slab 分配、定时器与调度器的记账、日志缓冲，各自落在不同的物理页上",
           13, SUB)
    nb, bw, bg, sx = 30, 42, 6, 70
    hit = {1: [3, 17], 4: [8], 6: [2, 25, 30], 9: [12], 13: [5, 20], 14: [27], 18: [9, 14], 21: [31], 24: [6, 22], 27: [18]}
    sy = T + 80
    for b in range(nb):
        x = sx + b * (bw + bg)
        on = b in hit
        d.rect(x, sy, bw, 34, "#FFFFFF", CO[1] if on else LINE, sw=1.4 if on else 1, rx=3)
        for p in hit.get(b, []):
            d.rect(x + 2 + p * (bw - 4) / 33, sy + 4, 2.6, 26, CO[1], CO[1], sw=0, rx=0)
    d.text(sx + nb * (bw + bg) + 6, sy + 23, "……", 14, MUTED)
    d.text(W - 70, sy + 56, "红边块：这一代里至少写过一个 4 KiB 页；块里的竖线：写过的页", 12.5, MUTED, anchor="end")
    # 放大其中一块
    zb = 6
    zx = sx + zb * (bw + bg)
    gx, gy = 120, T + 200
    d.o.append(f'<polyline points="{zx:.1f},{sy + 34:.1f} {gx:.1f},{gy - 34:.1f}" fill="none" stroke="#B4B2A9" '
               f'stroke-width="1" stroke-dasharray="4 3"/>')
    d.o.append(f'<polyline points="{zx + bw:.1f},{sy + 34:.1f} {gx + 414:.1f},{gy - 34:.1f}" fill="none" stroke="#B4B2A9" '
               f'stroke-width="1" stroke-dasharray="4 3"/>')
    d.text(gx, gy - 14, "放大一块：2 MiB = 512 个 4 KiB 页", 15, INK, 600)
    wr = {(0, 2), (0, 25), (1, 30), (3, 7), (4, 18), (5, 11), (7, 2), (8, 26), (10, 14), (11, 5), (12, 29), (14, 9), (15, 21)}
    for r in range(16):
        for c in range(32):
            on = (r, c) in wr
            d.rect(gx + c * 13, gy + r * 13, 11, 11, CO[1] if on else "#FFFFFF", CO[1] if on else "#B4B2A9", sw=0.6, rx=1.5)
    d.text(gx, gy + 234, "写跟踪位图（4 KiB）：这一块里写过十几个页（红格）", 13, CO[2], 600)
    d.arrow(gx + 440, gy + 104, gx + 640, gy + 104, "warn")
    d.text(gx + 540, gy + 92, "按 2 MiB 归并", 14, CO[2], 600, "middle")
    d.text(gx + 540, gy + 128, "一页写过，整块算脏", 12.5, CO[3], 400, "middle")
    bx = gx + 680
    d.rect(bx, gy, 414, 206, CO[1], CO[1], sw=1, rx=4)
    d.text(bx + 207, gy + 98, "整块 2 MiB 导出", 17, "#FFFFFF", 600, "middle")
    d.text(bx + 207, gy + 124, "header 的 BlockSize = 2 MiB", 13, "#FFFFFF", 400, "middle")
    d.text(bx, gy + 234, "剩下的四百多个页只因同在一块，被一起导出", 13, CO[2], 600)
    # 右侧：920B 实测
    a = RAMPS["amber"]
    ax, aw = bx + 444, W - M - 22 - (bx + 444)
    d.rect(ax, gy, aw, 206, "#FFFFFF", a[1], sw=1.2, rx=8)
    d.text(ax + 16, gy + 28, "920B 空转，修复前", 14.5, a[2], 600)
    d.text(ax + 16, gy + 48, "KVM 写保护，2026-09-10 前后", 12.5, a[3])
    for k, (lab, val) in enumerate([("4 KiB 真值", "4.1 MiB"), ("约 1050 个页落进", "约 67 块"), ("2 MiB 归并后", "134 MiB")]):
        yy = gy + 86 + 38 * k
        d.text(ax + 16, yy, lab, 13.5, SUB)
        d.text(ax + aw - 16, yy, val, 16, a[2], 600, "end")
    d.text(ax + 16, gy + 234, "放大三十多倍，与改动量无关", 13, a[2], 600)
    d.y = T + PH

    # ======== 2
    d.section(2, "差分块 = guest 页大小：块要降到 4 KiB，guest 页也得降到 4 KiB", note="§4.3 三处顶着；§7 路线 A")
    c = d.compare([("保留 2 MiB 大页", "修复前的状态", "coral"),
                   ("模板关大页 huge_pages=false", "三层天然都是 4 KiB", "gray")], label_width=170)
    c.row("guest 页", [("hugetlbfs 2 MiB", None), ("4 KiB", None)])
    c.row("③ 填一次缺页", [("2 MiB，UFFDIO_COPY 必须给满", "没办法只填其中 4 KiB"), ("4 KiB", None)],
          links=["warn", None])
    c.row("③ 硬检查", [Box("guest 页大小 == BlockSize", "NewUserfaultfdFromFd 不等就报 block size mismatch", ramp="coral"),
                     ("自然通过", "MemfilePageSize 返回 4 KiB")], sub="一次缺页恰好一个块",
          links=["warn", None])
    c.row("② header 的块", [("BlockSize = 2 MiB", "ToDiffHeader 经 NextGeneration 逐代继承"), ("BlockSize = 4 KiB", None)],
          links=["warn", None])
    c.summary("对比小结", [
        ("导出量", [("有地板，空转约 130 MiB", "bad"), ("4 KiB 真值", "good")]),
        ("400 MiB 工作集恢复", ["约 200 次缺页", ("约 10 万次，每次经 orchestrator 往返", "bad")]),
        ("guest TLB", [("保留 2 MiB 大页收益", "good"), ("失去大页收益", "bad")]),
        ("影响面", ["—", ("所有用这个模板的沙箱，不只是做快照的", "bad")]),
    ])
    c.end()
    d.callout("本章的做法是第三条路：保留 2 MiB 缺页，header 按 4 KiB 存，缺页时把 512 个 4 KiB 子页从映射链上拼成一整页再填（§5、§7、§8）",
              "teal")
    d.footer("注：数字取自 rollback/scripts/probes/pb4.py 在 920B 上的结果（§4.1 表“空转”一行，完整见第 20 章）。"
             "约 67 块 = 134 ÷ 2，是正文的估算。")
    d.save(out("rollback", "14-3-2MiB归并的地板"))


def fig4():
    d = Diagram(width=1600)
    d.header("一次 2 MiB 缺页的三级取源：能零拷贝就零拷贝，跨代的块才按 4 KiB 拼",
             "faultPage 与预取器共用 block.ReadPage(ctx, src, off, pageSize)，它返回一整页内容和一个 release 函数。"
             "缺页单位仍是 2 MiB，变的只是这 2 MiB 从哪里取。",
             legend=[("flow", "执行顺序"), ("warn", "要分配缓冲并拷贝")])

    d.section(1, "按源的形态分三级", highlight="本图重点", note="block/page.go:63")
    c = d.compare([("第一级", "块等于页：与修复前同一条路", "gray"),
                   ("第二级", "一条映射覆盖整页", "gray"),
                   ("第三级", "跨代的块", "coral")])
    c.span("触发", "uffd 缺页：offset，pageSize = 2 MiB")
    c.row("判断", [
        ("源的 BlockSize == pageSize", "是：旧的 2 MiB 快照"),
        ("SliceContiguous(off, 2 MiB) 为真", "BlockSize ≠ pageSize；整块同代，或整块是空洞"),
        ("SliceContiguous(off, 2 MiB) 为假", "BlockSize ≠ pageSize；这 2 MiB 里混着几代"),
    ])
    c.row("怎么取", [
        ("src.Slice(off, 2 MiB)", "page.go:66-71；行为与修复前逐字节相同"),
        ("返回那一代 mmap 缓存里的切片", "page.go:77-89；空洞返回共享的全零大页 header.EmptyHugePage"),
        ("从 sync.Pool 取 2 MiB 缓冲，src.ReadAt 沿映射链逐段拼满", "page.go:91-109；空洞段由 ReadAt 自己清零；字节数不等于页大小直接报错"),
    ], links=["flow", "flow", "warn"])
    c.row("填页", [("UFFDIO_COPY 2 MiB", None), ("UFFDIO_COPY 2 MiB", None),
                   ("UFFDIO_COPY 2 MiB", "完成后 release() 把缓冲还回池")])
    c.summary("对比小结", [
        ("拷贝", [("零拷贝", "good"), ("零拷贝", "good"), ("一次 2 MiB 拷贝", "bad")]),
        ("映射查表", ["不多付", "查一次映射", ("最多 512 次", "bad")]),
        ("多少缺页走这里", ["从旧的 2 MiB 快照起沙箱时", "大多数缺页", "只有跨代的块"]),
    ])
    c.end()
    d.footer("注：template.Storage 也补了 SliceContiguous（sandbox/template/storage.go:155），"
             "让 ReadPage 能透过模板存储这一层问到“是否整页同源”。")
    d.save(out("rollback", "14-4-缺页的三级取源"))


if __name__ == "__main__":
    fig1()
    fig2()
    fig3()
    fig4()
