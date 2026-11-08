"""第 07 章 磁盘分层与回收的配图。

07-1 一次 guest 磁盘读写经过的各层（替换 §1.2 的文本示意）。
07-2 层栈在 checkpoint、restore、删除时怎么变（§2～§4，新增）：封存与接替、改指向、引用计数回收。
     例子取自 §2.1、§3.1、§4.2 正文（块 100 的 A / B / C，层 W1、W2、W3，引用数 3 与 2）。
内容只取本章 §1 正文与被替换的代码块。LayerStack（封层后才有）用 teal，其余用灰色；被删除的旧写层用 coral。
"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w  # noqa: E402

GF, GS, GT, GSUB = RAMPS["gray"]


def f07_1():
    d = Diagram()
    d.header("一次 guest 磁盘读写经过的各层：从 ext4 到 Overlay 的活写层",
             "Firecracker 负责虚拟磁盘的设备行为，orchestrator 经 NBD 提供磁盘的数据内容。"
             "读请求沿同一路径返回，写请求由 Overlay 写入当前活写层。",
             legend=[("flow", "请求方向")])
    M, W = d.M, d.W
    cw = 600
    cx0 = (W - cw) / 2 + 40            # 主链框的左边
    cxm = cx0 + cw / 2                  # 主链中心线
    bh, gap = 92, 58
    chain = [
        ("ext4 文件系统驱动", "把文件读写转换为磁盘读写", "guest 内核", None),
        ("virtio-blk 驱动", "设备名 /dev/vda", "guest 内核", "通过 virtio 队列提交请求"),
        ("Firecracker", "模拟 virtio-blk 磁盘设备", "host 进程", "对配置的后端设备执行读写"),
        ("/dev/nbdX", "host 内核中的 NBD 块设备", "host 内核", "NBD 驱动通过连接转发请求"),
        ("NBD dispatcher", "orchestrator 进程中", "orchestrator", "调用 Overlay 的读写方法"),
        ("Overlay", "读合并、写落上层", "orchestrator", None),
    ]
    y = d.y + 40
    tops = []
    for i, (t, s, a, lab) in enumerate(chain):
        if i:
            y += gap
        tops.append(y)
        y += bh
    # 区域：guest 内部 / host 上
    gtop, gbot = tops[0] - 22, tops[1] + bh + 18
    htop, hbot = tops[2] - 18, None
    # 主链框与箭头
    for i, (t, s, a, lab) in enumerate(chain):
        d.block(cx0, tops[i], cw, bh, t, s, "gray", actor=a)
        if i:
            d.arrow(cxm, tops[i - 1] + bh + 2, cxm, tops[i] - 5, "flow", chain[i - 1][3])
    # Overlay 下面的数据来源：LayerStack 居中在 Overlay 正下方，Cache 在左，模板在右
    oy = tops[-1] + bh
    yA = oy + 40
    sy = oy + 120
    sh = 100
    sw_ = 400
    sx = [cxm - sw_ / 2 - 80 - sw_, cxm - sw_ / 2, cxm + sw_ / 2 + 60]
    srcs = [("Cache（活写层）", "稀疏文件 + mmap + 块位图；所有写入都进这里", "gray", "fill", "写；读 ①"),
            ("LayerStack", "封存层 + 块归属索引（封层后才有）", "teal", "dashed", "读 ②：活写层没有该块"),
            ("模板 rootfs", "经 chunker 按需拉取并在本地缓存", "gray", "fill", "读 ③：封存层也没有")]
    rx0, rx1 = sx[1] - 24, sx[2] + sw_ + 24
    d.rect(rx0, sy - 44, rx1 - rx0, sh + 82, "#FFFFFF", "#D3D1C7", sw=1, rx=14, dash="5 4")
    d.text(rx1 - 18, sy + sh + 27, "只读数据来源（o.device）", 13.5, SUB, 600, "end")
    starts = [cx0 + 60, cxm, cx0 + cw - 60]
    for i, (t, s_, r, st, lab) in enumerate(srcs):
        d.block(sx[i], sy, sw_, sh, t, s_, r, st)
        tx = sx[i] + sw_ / 2
        if i == 1:
            d.arrow(cxm, oy + 2, cxm, sy - 6, "flow")
        else:
            d.kinds.add("flow")
            pts = [(starts[i], oy + 2), (starts[i], yA), (tx, yA), (tx, sy - 6)]
            d.o.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) +
                       '" fill="none" stroke="#5F5E5A" stroke-width="1.8" marker-end="url(#ar-flow)"/>')
        d.text(tx + 12, (yA + sy) / 2 + 12, lab, 13.5, SUB, 600)
    hbot = sy + sh + 56
    # 区域底框（画在最底层：插到最前面）
    band = []
    for (t0, t1, name, note) in [(gtop, gbot, "guest 内部", "/dev/vda 是 guest 内核为这块虚拟磁盘命名的设备"),
                                 (htop, hbot, "host 上", "/dev/nbdX 是 host 上的块设备，作为这块虚拟磁盘的数据后端；"
                                                         "guest 不直接访问它")]:
        band.append(f'<rect x="{M}" y="{t0:.1f}" width="{W - 2 * M}" height="{t1 - t0:.1f}" rx="16" '
                    f'fill="#FBFBF9" stroke="#D3D1C7" stroke-width="1"/>')
    d.o[0:0] = band
    d.text(M + 24, gtop + 36, "guest 内部", 20, INK, 600)
    d.text(M + 24, htop + 36, "host 上", 20, INK, 600)
    # 右侧说明（与主链同高）
    nx = cx0 + cw + 40
    nw = W - M - 24 - nx
    for (yy, title, sub) in [
        (tops[1], "/dev/vda", "guest 内核为 Firecracker 提供的 virtio-blk 虚拟磁盘命名的设备"),
        (tops[3], "/dev/nbdX", "host 上的块设备，作为这块虚拟磁盘的数据后端；guest 不直接访问它"),
        (tops[5], "换视图不换设备", "后面的封层和换视图发生在 Overlay 内部，不需要换 /dev/vda 或 /dev/nbdX"),
    ]:
        d.block(nx, yy, nw, bh, title, sub, "gray", "dashed")
    d.y = hbot
    d.footer("注：读块时，活写层位图为 1 就用本层内容（即使内容全零），为 0 才继续往下查。"
             "没有保存过 checkpoint 时还没有 LayerStack，读不到就直接读模板。")
    d.save(out("rollback", "07-1-一次磁盘读写经过的各层"))


def _panel(d, x, y, w, h, title, sub):
    d.rect(x, y, w, h, "#FBFBF9", "#D3D1C7", sw=1, rx=14)
    d.text(x + 20, y + 32, title, 16.5, INK, 600)
    d.text(x + 20, y + 54, sub, 13, MUTED)


def _ptr(d, x, y, w, name):
    """指针标签：o.cache / o.device。"""
    d.text(x, y, name, 13.5, SUB, 600)


def f07_2():
    d = Diagram()
    d.header("层栈怎么变：checkpoint 封存并接替，restore 改指向，删除看引用计数",
             "Overlay 只有两个指针：o.cache 指向当前活写层，o.device 指向活写层没有该块时使用的只读来源。"
             "checkpoint 与 restore 都只改这两个指针，不逐块复制数据；层文件何时删除由引用计数决定，不按 checkpoint 树决定。",
             legend=[("flow", "操作的先后")])
    M, W = d.M, d.W
    TF, TS, TT, TSUB = RAMPS["teal"]
    CF, CS, CT, CSUB = RAMPS["coral"]

    d.section(1, "checkpoint 与 restore：Overlay 的两个指针怎么变", note="以块 100 为例：模板中是 A", highlight="本图重点")
    G = 100
    PWD = (W - 2 * M - 2 * G) / 3
    xs = [M + i * (PWD + G) for i in range(3)]
    T = d.y + 14
    PH = 560
    panels = [
        ("① 运行中，还没保存过", "块 100 改成 B，写进 W1"),
        ("② checkpoint c1 之后，又改成 C", "W1 封存进层栈，W2 接替为活写层"),
        ("③ restore c1 之后", "改用 c1 的层清单，新建空写层 W3"),
    ]
    for i, (t, sb) in enumerate(panels):
        _panel(d, xs[i], T, PWD, PH, t, sb)
    for i, lab in enumerate(["checkpoint c1", "restore c1"]):
        x1 = xs[i] + PWD + 4
        x2 = xs[i + 1] - 6
        d.arrow(x1, T + 150, x2, T + 150, "flow")
        d.text((x1 + x2) / 2, T + 136, lab.split()[0], 12.5, SUB, 600, "middle")
        d.text((x1 + x2) / 2, T + 176, lab.split()[1], 12.5, SUB, 600, "middle")
    ix = 20
    bw = PWD - 2 * ix
    lh = 64
    for i in range(3):
        x = xs[i] + ix
        y = T + 84
        _ptr(d, x, y, bw, "o.cache → 当前活写层")
        if i == 0:
            d.block(x, y + 12, bw, lh, "W1", "块 100 = B；所有写入都进这里", "gray")
        elif i == 1:
            d.block(x, y + 12, bw, lh, "W2（接替时是新建的空文件）", "之后写入：块 100 = C", "gray")
        else:
            d.block(x, y + 12, bw, lh, "W3（新建的空文件）", "后续新写入的位置", "gray", "dashed")
        y2 = y + 12 + lh + 40
        if i == 0:
            _ptr(d, x, y2, bw, "o.device → 只读来源")
            d.block(x, y2 + 12, bw, lh, "模板 rootfs", "块 100 = A", "gray")
            ry = y2 + 12 + lh + 30
            d.text(x, ry, "读块 100：W1 位图为 1 → B", 14, INK, 600)
            d.block(x, ry + 24, bw, 120, "还没有 LayerStack",
                    "活写层没有该块就直接读模板", "gray", "dashed")
        else:
            _ptr(d, x, y2, bw, "o.device → LayerStack（c1 的层清单）")
            d.rect(x, y2 + 12, bw, 2 * lh + 34, "#FFFFFF", TS, sw=1.4, rx=10)
            d.block(x + 12, y2 + 24, bw - 24, lh - 6, "W1（封存层）", "块 100 = B；不再接收正常写入", "teal")
            d.block(x + 12, y2 + 24 + lh + 4, bw - 24, lh - 6, "模板 rootfs", "块 100 = A", "gray")
            ry = y2 + 12 + 2 * lh + 34 + 30
            if i == 1:
                d.text(x, ry, "读块 100：W2 有 → C（改成 C 之前从 W1 读 B）", 14, INK, 600)
                d.block(x, ry + 24, bw, 120, "封存文件移进 layers/",
                        "resume 之后 rename；同一文件系统内 inode 不变，活着的 mmap 继续有效", "teal", "dashed")
            else:
                d.text(x, ry, "读块 100：W3 没有 → 从 W1 读 B", 14, INK, 600)
                hw = (bw - 12) / 2
                d.block(x, ry + 24, hw, 120, "旧写层 W2", "Close()：解映射，连文件一起删",
                        "coral", "dashed")
                d.block(x + hw + 12, ry + 24, hw, 120, "旧层栈", "Close()：只解映射，文件留给 store",
                        "gray", "dashed")
    d.y = T + PH
    d.callout("两个操作都在虚机暂停期间先刷屏障（BLKFLSBUF）再换指针；NBD dispatcher 手里的 *Overlay 指针、/dev/vda、/dev/nbdX 都不变",
              "gray")

    d.section(2, "删除 checkpoint：先看哪些层仍被引用", note="§4.2 的例子，尚未发生层合并")
    T2 = d.y + 14
    refs = [("c1 的视图", "Entry.Rootfs", [1, 0]), ("c2 的视图", "Entry.Rootfs", [1, 1]),
            ("活账本", "rootfsState.layers", [1, 1])]
    TW = 600
    tx = [M, W - M - TW]
    cols = ["W1", "W2"]
    LW = 290
    CWID = (TW - LW - 20) / 2
    RH = 46
    for k, (title, dropped) in enumerate([("删除前", False), ("删除 c1 之后", True)]):
        x = tx[k]
        th = 40 + RH * (len(refs) + 1) + 26
        d.rect(x, T2, TW, th, "#FBFBF9", "#D3D1C7", sw=1, rx=14)
        d.text(x + 20, T2 + 28, title, 16, INK, 600)
        for j, cn in enumerate(cols):
            d.text(x + LW + CWID * j + CWID / 2, T2 + 28, cn, 15, TT, 600, "middle")
        for r, (name, sub, bits) in enumerate(refs):
            ry = T2 + 40 + r * RH
            gone = dropped and r == 0
            d.text(x + 20, ry + 22, name, 14.5, MUTED if gone else INK, 600)
            d.text(x + 20 + text_w(name, 14.5) + 10, ry + 22, sub, 12.5, MUTED)
            for j, b in enumerate(bits):
                cx = x + LW + CWID * j + 10
                if b and not gone:
                    d.rect(cx, ry + 4, CWID - 20, RH - 12, TF, TS, sw=1, rx=6)
                    d.text(cx + (CWID - 20) / 2, ry + 27, "列出", 13.5, TT, 600, "middle")
                elif b and gone:
                    d.rect(cx, ry + 4, CWID - 20, RH - 12, "#FFFFFF", CS, sw=1, rx=6, dash="4 4")
                    d.text(cx + (CWID - 20) / 2, ry + 27, "−1", 13.5, CT, 600, "middle")
                else:
                    d.text(cx + (CWID - 20) / 2, ry + 27, "—", 13.5, MUTED, 400, "middle")
        ry = T2 + 40 + len(refs) * RH
        d.o.append(f'<line x1="{x + 16}" y1="{ry + 2}" x2="{x + TW - 16}" y2="{ry + 2}" stroke="#D3D1C7" stroke-width="1"/>')
        d.text(x + 20, ry + 30, "引用数", 14.5, INK, 600)
        cnt = [3, 2] if not dropped else [2, 2]
        for j, c in enumerate(cnt):
            d.text(x + LW + CWID * j + CWID / 2, ry + 31, str(c), 18, INK, 600, "middle")
        tb = T2 + th
    mid1, mid2 = tx[0] + TW + 8, tx[1] - 10
    d.arrow(mid1, T2 + (tb - T2) / 2, mid2, T2 + (tb - T2) / 2, "flow")
    d.text((mid1 + mid2) / 2, T2 + (tb - T2) / 2 - 30, "deleteLocked", 13, SUB, 600, "middle")
    d.text((mid1 + mid2) / 2, T2 + (tb - T2) / 2 - 12, "条目视图每层 −1", 13, SUB, 400, "middle")
    d.text((mid1 + mid2) / 2, T2 + (tb - T2) / 2 + 26, "W1 从 3 减到 2", 13, SUB, 600, "middle")
    d.text((mid1 + mid2) / 2, T2 + (tb - T2) / 2 + 44, "c2 仍要从 W1 读 B", 13, SUB, 400, "middle")
    d.y = tb + 24
    d.flow([
        Step("引用数归零", "没有任何视图和活账本再列出这一层", "Store.layerRefs"),
        Step("进 reclaim，锁外删除", "层文件连同 .meta 一起", "store"),
        Step("数据块真正释放", "运行中的旧 LayerStack 若仍有 mmap，要等映射解除", "host 文件系统"),
    ], min_h=110, gap=48)
    d.footer("注：层栈不是 checkpoint 树，所以不能“删一个叶子就删它封存的那一层”：appendLocked 不看 ParentID，全量根的视图也叠在此前每一层上。"
             "跟踪关闭时层文件随 checkpoint 次数累积，直到 restore 替换活栈或沙箱销毁；层合并见 §5。")
    d.save(out("rollback", "07-2-层栈怎么变"))


if __name__ == "__main__":
    f07_1()
    f07_2()
