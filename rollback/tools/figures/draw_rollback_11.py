"""第 11 章配图：11-1 restore 的四档失败（提交点把失败切成两半）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, wrap, text_w


def card(d, x, y, w, h, code, ramp, style, rows):
    """失败档卡片：顶部是 reason，下面每项一个灰色小标签 + 正文。"""
    fill, stroke, tc, sc = RAMPS[ramp]
    d.rect(x, y, w, h, fill if style == "fill" else "#FFFFFF", stroke, sw=1.4)
    d.text(x + w / 2, y + 32, code, 17, tc, 600, "middle")
    d.o.append(f'<line x1="{x + 18:.1f}" y1="{y + 48:.1f}" x2="{x + w - 18:.1f}" y2="{y + 48:.1f}" '
               f'stroke="{stroke}" stroke-width="0.8" stroke-opacity="0.6"/>')
    yy = y + 72
    for lab, txt, strong in rows:
        d.text(x + 18, yy, lab, 12.5, MUTED, 600)
        yy += 22
        for s in wrap(txt, w - 36, 14):
            d.text(x + 18, yy, s, 14, tc if strong else INK, 600 if strong else 400)
            yy += 21
        yy += 12
    return yy - y


def card_h(w, rows):
    h = 72
    for _, txt, _ in rows:
        h += 22 + 21 * len(wrap(txt, w - 36, 14)) + 12
    return h


def fig1():
    d = Diagram(width=1600)
    M = d.M
    d.header("restore 的四档失败：落在提交点哪一侧，决定沙箱还能不能用",
             "提交点之前失败，guest 一个字节都没动，虚机恢复运行；之后失败，虚机是两个时刻的混合体，按撕裂处理；"
             "回滚完成后 guest 不应答，宿主侧已完成，不是撕裂。",
             legend=[("flow", "执行顺序"), ("back", "失败后恢复运行"), ("warn", "撕裂：虚机保持暂停")])

    # ------------------------------------------------------------ 第一段
    d.section(1, "一次 restore 走过的阶段，以及每段失败落在哪一档", highlight="本图重点",
              note="orchestrator 侧 RollbackInPlace，Firecracker 侧 faults_vm()")

    gap = 40
    commit_gap = 64
    n = 5
    bw = (d.W - 2 * M - (n - 1) * gap - (commit_gap - gap)) / n
    xs = []
    x = M
    for i in range(n):
        xs.append(x)
        x += bw + (commit_gap if i == 2 else gap)

    phases = [
        ("读磁盘视图、装配视图", "DiskViewForEntry、AssembleView，在暂停虚机之前完成", "orchestrator", "outline"),
        ("暂停，物化回滚集", "MaterializeRevert：查断链、算回滚路径、写物化文件", "orchestrator", "outline"),
        ("Firecracker 提交点之前", "校验、静默 I/O、取活跃脏图：不改 guest 状态", "Firecracker", "outline"),
        ("Firecracker 提交点之后", "写内存、vCPU、GIC、设备；再 ResetView 切磁盘、resume", "Firecracker / orchestrator", "fill"),
        ("等 guest 的 envd 应答", "宿主侧已完成，虚机在目标时刻", "orchestrator", "outline"),
    ]
    ph_h = max(d._need(t, s, bw, actor=True) for t, s, _, _ in phases)
    # 阶段带标签
    band_y = d.y + 8
    zones = [(0, 2, "提交点之前：guest 状态没动"), (3, 3, "提交点之后：guest 已改"), (4, 4, "回滚完成之后")]
    for a, b, lab in zones:
        x0, x1 = xs[a], xs[b] + bw
        d.text((x0 + x1) / 2, band_y + 14, lab, 14, MUTED, 600, "middle")
        d.o.append(f'<line x1="{x0:.1f}" y1="{band_y + 24:.1f}" x2="{x1:.1f}" y2="{band_y + 24:.1f}" '
                   f'stroke="#D3D1C7" stroke-width="1.2"/>')
    py = band_y + 40
    for i, (t, s, actor, style) in enumerate(phases):
        ramp = "coral" if i == 3 else "gray"
        d.block(xs[i], py, bw, ph_h, t, s, ramp, style, actor=actor)
        if i < n - 1:
            x1 = xs[i] + bw + 4
            x2 = xs[i + 1] - 6
            d.arrow(x1, py + ph_h / 2, x2, py + ph_h / 2, "flow")

    # 提交点竖线
    cx = xs[2] + bw + commit_gap / 2
    top, bot = band_y - 4, py + ph_h + 330
    # 卡片区
    cy = py + ph_h + 70
    pre_x0, pre_x1 = xs[0], xs[2] + bw
    cgap = 24
    cw_pre = (pre_x1 - pre_x0 - cgap) / 2
    cards = [
        (pre_x0, cw_pre, "chain_broken", "gray", "outline", [
            ("情形", "checkpoint 树解析不了这次 restore：目标没有磁盘视图、祖先缺失、当前基准不在库里、丢过纪元（断链）", False),
            ("沙箱", "原状态，继续可用", True),
            ("调用方", "不要重试；先打一个新 checkpoint 起新链", False)]),
        (pre_x0 + cw_pre + cgap, cw_pre, "internal", "gray", "outline", [
            ("情形", "提交点之前的其它失败：读 sidecar、装配视图、物化回滚集、Firecracker 在提交点前报错", False),
            ("沙箱", "虚机原样恢复；消息里明说「沙箱仍在当前状态运行」", True),
            ("调用方", "换目标重试，或先打一个新 checkpoint", False)]),
        (xs[3], bw, "torn", "coral", "fill", [
            ("情形", "应答带 \"fault\": true；rollback 调用超过 CHECKPOINT_FC_CALL_TIMEOUT 未应答；ResetView 或之后的 resume 失败", False),
            ("沙箱", "撕裂；这一代的 checkpoint 与 restore 一律被拒，list / delete 仍可用", True),
            ("调用方", "只能销毁重建沙箱", False)]),
        (xs[4], bw, "guest_unresponsive", "gray", "outline", [
            ("情形", "回滚成功，但 envd 在 45 s 内不应答", False),
            ("沙箱", "不是撕裂；虚机在目标时刻", True),
            ("调用方", "按业务决定：稍后重试命令，或销毁重建", False)]),
    ]
    ch = max(card_h(w, rows) for _, w, _, _, _, rows in cards)
    bot = cy + ch + 8
    d.o.append(f'<line x1="{cx:.1f}" y1="{top:.1f}" x2="{cx:.1f}" y2="{bot:.1f}" stroke="#D85A30" '
               f'stroke-width="2.4" stroke-dasharray="10 6"/>')
    lab = "提交点"
    lw = text_w(lab, 14) + 22
    d.rect(cx - lw / 2, py + ph_h + 20, lw, 28, "#FFFFFF", "#D85A30", sw=1.2, rx=14)
    d.text(cx, py + ph_h + 39, lab, 14, RAMPS["coral"][2], 600, "middle")

    for x, w, code, ramp, style, rows in cards:
        card(d, x, cy, w, ch, code, ramp, style, rows)

    # 阶段 → 卡片 的箭头
    ay0 = py + ph_h + 4
    ay1 = cy - 6
    # 提交点之前三段合成一个括号，再分到两档
    by = ay0 + 14
    d.o.append(f'<path d="M{pre_x0 + 20:.1f} {ay0:.1f} V{by:.1f} H{pre_x1 - 20:.1f} V{ay0:.1f}" fill="none" '
               f'stroke="#888780" stroke-width="1.4"/>')
    d.arrow(pre_x0 + cw_pre / 2, by + 2, pre_x0 + cw_pre / 2, ay1, "back")
    d.arrow(pre_x0 + cw_pre + cgap + cw_pre / 2, by + 2, pre_x0 + cw_pre + cgap + cw_pre / 2, ay1, "back")
    d.arrow(xs[3] + bw / 2, ay0, xs[3] + bw / 2, ay1, "warn")
    d.arrow(xs[4] + bw / 2, ay0, xs[4] + bw / 2, ay1, "flow")
    d.y = bot

    d.callout("rollback 调用超时也算撕裂：判断不了 Firecracker 停在提交点哪一侧，"
              "恢复运行一个可能半新半旧的 guest，会让它把说不清的状态写进磁盘", "coral")

    # ------------------------------------------------------------ 第二段
    d.section(2, "两半各怎么收尾", note="Firecracker 的 match 是穷尽的，新错误变体必须归入其中一半")
    c = d.compare([("提交点之前", "chain_broken、internal", "gray"),
                   ("提交点之后", "torn", "coral")])
    c.row("Firecracker 侧", [
        ("faults_vm() 返回 false",
         "NotPaused、Faulted、SnapshotFile、Validation、RevertBitmap、MemoryFile、MemoryFileCoverage、DirtyBitmap、BitmapWrite"),
        Box("faults_vm() 返回 true，虚机标记 Faulted", "Memory、Vcpu、Gic、Devices", "fill")])
    c.row("虚机", [
        ("恢复运行", "resumeOnError：先 conntrack.join() 等清表完成，再 ResumeVM；恢复也失败时两个错误一起返回"),
        Box("保持暂停", "它持有回滚写到一半的内容，是事后分析唯一的材料；恢复运行会让半新半旧的 guest 写它的磁盘", "fill")],
        links=["back", "warn"])
    c.row("账本", [
        Text("—", "muted"),
        Box("标记断链，磁盘账本 poisoned", None, "fill")],
        links=[None, "flow"])
    c.row("撕裂标记", [
        Text("—", "muted"),
        Box("按代记，不按沙箱 id", "记在 Sandbox.LifecycleID 下（refuseIfTorn、markTorn）；同一个 id 经原生 pause / resume 换了新进程后从干净状态开始", "fill")],
        links=[None, "flow"])
    c.end()

    d.footer("注：reason 是 restore 失败应答里的字段；调用方如何处理错误、SDK 的超时与重试见第 24 章。"
             "envd 的 45 s 要明显短于 SDK 通用请求超时 60 s，保证这一档由服务端报告，而不是变成客户端的读超时。")
    d.save(out("rollback", "11-1-restore的四档失败"))


if __name__ == "__main__":
    fig1()
