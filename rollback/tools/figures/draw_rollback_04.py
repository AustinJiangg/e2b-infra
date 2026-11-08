"""第 04 章配图：架构全图（01 章用它的简版）、checkpoint 时序、restore 时序、三个时钟。

运行：python3 draw_rollback_04.py
本文件里的几个小工具（面板、折线箭头、色块图例）也被 draw_rollback_01.py、draw_rollback_02.py 引用。
"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, ARROWS, text_w, wrap

PANEL_FILL, PANEL_LINE = "#FBFBF9", "#D3D1C7"


# ------------------------------------------------------------------ 小工具
def panel(d, x, y, w, h, title=None, note=None, dash=None):
    """浅灰大圆角底板，左上角写面板名。"""
    d.rect(x, y, w, h, PANEL_FILL, PANEL_LINE, sw=1, rx=14, dash=dash)
    if title:
        d.text(x + 18, y + 27, title, 15.5, INK, 600)
        if note:
            d.text(x + 18 + text_w(title, 15.5) + 14, y + 27, note, 13, MUTED)


def need(d, title, sub, w, actor=None, tag=None, ts=None, ss=None, pad=28):
    return d._need(title, sub, w, bool(actor), tag, ts, ss, pad)


def elbow(d, pts, kind="flow", label=None, label_at=None, anchor="middle"):
    """正交折线箭头：pts=[(x,y),...]，末端带箭头。label_at=(x,y) 标签位置。"""
    col, dash = ARROWS[kind]
    d.kinds.add(kind)
    p = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(pts))
    ds = f' stroke-dasharray="{dash}"' if dash else ""
    d.o.append(f'<path d="{p}" fill="none" stroke="{col}" stroke-width="1.8"{ds} '
               f'stroke-linejoin="round" marker-end="url(#ar-{kind})"/>')
    if label and label_at:
        d.text(label_at[0], label_at[1], label, 12.5, col, 600, anchor)


def swatches(d, y, items, x=None):
    """色块图例：items=[(色阶, 说明)]。"""
    x = d.M if x is None else x
    for it in items:
        ramp, name = it[0], it[1]
        f, s, _, _ = RAMPS[ramp]
        if len(it) > 2:      # 第三项是框样式：outline / dashed
            d.rect(x, y - 13, 30, 18, "#FFFFFF", s, sw=1.2, rx=5, dash="5 4" if it[2] == "dashed" else None)
        else:
            d.rect(x, y - 13, 30, 18, f, s, sw=1.2, rx=5)
        d.text(x + 40, y + 1, name, 13.5, SUB)
        x += 40 + text_w(name, 13.5) + 36


def lines_at(d, x, y, items, size=13.5, lh=22, color=SUB, anchor="start", weight=400):
    for k, s in enumerate(items):
        d.text(x, y + k * lh, s, size, color, weight, anchor)


def pill(d, x, y, s, fill="#FCEBEB", stroke="#E24B4A", color="#A32D2D", size=13):
    """红色胶囊标签（提交点、纪元前进这类标记），(x, y) 是左端中点。"""
    w = text_w(s, size) + 22
    d.rect(x, y - 12, w, 24, fill, stroke, sw=0.9, rx=12)
    d.text(x + w / 2, y + 5, s, size, color, 600, "middle")
    return w


# ------------------------------------------------------------------ 架构图（04-1 详版，01-1 简版）
def draw_arch(brief, path):
    d = Diagram(width=1600)
    if brief:
        d.header("请求发往沙箱，由宿主完成：checkpoint / restore 经过的四个层次",
                 "SDK 发请求，orchestrator 编排并持有账本，分叉的 Firecracker 读写内存与设备状态，KVM 和硬件提供脏页信息。"
                 "Firecracker 进程全程不重建；①～④ 是贯穿全书的四处设计。")
    else:
        d.header("四个层次与它们之间的调用：语义只在账本，写回只在 Firecracker",
                 "代理在转发前截下 49984 端口的 checkpoint 请求，Checkpoint Service 经 Sandbox 对象驱动 Firecracker；"
                 "账本只在 orchestrator 进程内存里，产物落在宿主本地存储。")
    swatches(d, d.y + 34, [("teal", "本方案的部件：checkpoint 接口、拦截、编排、账本、Firecracker 扩展、产物目录"),
                           ("gray", "沙箱原有的部件")])
    d.kinds.add("flow")
    d.arrow(d.W - d.M - 190, d.y + 29, d.W - d.M - 150, d.y + 29)
    d.text(d.W - d.M - 140, d.y + 34, "调用 / 数据流向", 13.5, SUB)
    y = d.y + 70

    # 列：A、B、C 在左侧三层面板里，D 是右侧存储面板
    PL, PR = d.M, 1150                      # 左侧面板外框
    A0, A1 = 72, 352
    B0, B1 = 392, 712
    C0, C1 = 790, 1126
    D0, D1 = 1186, d.W - d.M
    Aw, Bw, Cw = A1 - A0, B1 - B0, C1 - C0
    Ac, Bc, Cc = (A0 + A1) / 2, (B0 + B1) / 2, (C0 + C1) / 2
    TB = 44                                  # 面板标题栏高度
    GAP = 104 if brief else 60               # 面板之间（简版要放 checkpoint / restore 路径两行字）
    tag = (lambda s: (s, "teal")) if brief else (lambda s: None)

    # ---- 调用方
    sdk_t, sdk_s = "Python SDK", "sandbox.checkpoint.create / restore / list / delete，目标地址：沙箱的 49984 端口"
    h = need(d, sdk_t, sdk_s, B1 - A0)
    panel(d, PL, y, PR - PL, TB + h + 18, "调用方")
    d.block(A0, y + TB, B1 - A0, h, sdk_t, sdk_s, "teal")
    if brief:
        d.text(C0, y + TB + h / 2 + 5, "沙箱内没有任何东西监听 49984", 13.5, MUTED)
    sdk_bottom = y + TB + h
    y += TB + h + 18 + GAP - (40 if brief else 0)
    orch_top = y

    # ---- orchestrator
    if brief:
        px = ("代理拦截", "49984 + checkpoint 路径不转发进沙箱，宿主直接应答并校验 token")
        sv = ("Checkpoint Service", "按沙箱串行 · 决定全量 / 增量 · 失败分级 · 分阶段计时")
        l1 = ("内存差分树账本", "父指针 · 每代脏页位图 · 删除 / 隐藏 / 合并")
        l2 = ("磁盘层账本", "合并 header · 层引用计数")
    else:
        px = ("Sandbox Proxy", "包装原 Handler：命中 49984 + checkpoint 路径就不转发")
        sv = ("Checkpoint Service", "鉴权 · 按沙箱串行 · 全量 / 增量决策 · 失败分级 · 分阶段计时")
        l1 = ("内存差分树", "条目 · 基准 · 子节点计数")
        l2 = ("磁盘层账本", "合并 header · 层清单 · 层引用计数")
    sbx = ("Sandbox 对象", "process · Rootfs Provider · Slot · Template")
    t1 = tag("① 宿主侧应答，沙箱内无代理")
    t3 = tag("③ 每代只存脏页，历史是树")
    r1 = max(need(d, *px, Aw, tag=t1), need(d, *sv, Bw), need(d, *l1, Cw, tag=t3))
    r2 = max(need(d, *l2, Cw), need(d, *sbx, Bw))
    RG = 44
    oy = orch_top + TB + 8
    orch_h = TB + 8 + r1 + RG + r2 + 24
    panel(d, PL, orch_top, PR - PL, orch_h, "宿主 orchestrator 进程")
    d.text(C0 - 6, orch_top + 27, "账本（只在进程内存）", 13.5, SUB, 600)
    d.rect(C0 - 12, oy - 10, Cw + 24, r1 + RG + r2 + 20, "none", "#B4B2A9", sw=1, rx=10, dash="5 4")
    ph = r1 if not brief else r1 + RG + r2
    d.block(A0, oy, Aw, ph, *px, "teal", tag=t1)
    d.block(B0, oy, Bw, ph, *sv, "teal")
    d.block(C0, oy, Cw, r1, *l1, "teal", tag=t3)
    d.block(C0, oy + r1 + RG, Cw, r2, *l2, "teal")
    d.arrow(A1 + 4, oy + r1 / 2, B0 - 6, oy + r1 / 2)
    d.arrow(B1 + 4, oy + r1 / 2, C0 - 18, oy + r1 / 2)
    d.arrow(Ac + 50, sdk_bottom + 2, Ac + 50, oy - 5, "flow", "HTTP")
    if not brief:
        d.block(B0, oy + r1 + RG, Bw, r2, *sbx, "gray")
        d.block(A0, oy + r1 + RG, Aw, r2, "restore 期间的其它请求", "代理立刻回答，不放进沙箱", "teal", "dashed")
        d.arrow(Ac + 50, oy + r1 + 2, Ac + 50, oy + r1 + RG - 5)
        d.arrow(Bc, oy + r1 + 2, Bc, oy + r1 + RG - 5)
    ledger_mid = oy + (r1 + RG + r2) / 2
    orch_bottom = orch_top + orch_h
    y = orch_bottom + GAP
    fc_top = y

    # ---- Firecracker
    vc = ("vCPU · GIC · virtio 设备", "逻辑状态写回活对象" if brief else "设备逻辑状态与寄存器")
    mm = ("Guest 内存", "差异页直接写回活映射" if brief else "guest 物理内存的映射")
    t2 = tag("② 原地写回，不重建进程")
    api = ("API socket", "标准端点 + 三处扩展：dirty_bitmap_path、/snapshot/save-dirty-bitmap、/snapshot/rollback")
    ah = 0 if brief else need(d, *api, B1 - A0)
    vh = max(need(d, *vc, Aw), need(d, *mm, Bw, tag=t2))
    fy = fc_top + TB + 8
    fc_h = TB + 8 + (ah + 40 if ah else 0) + vh + 20
    panel(d, PL, fc_top, B1 + 20 - PL, fc_h, "Firecracker 进程", "分叉版，全程不重建")
    if ah:
        d.block(A0, fy, B1 - A0, ah, *api, "teal")
        d.arrow(Ac, fy + ah + 2, Ac, fy + ah + 35)
        d.arrow(Bc, fy + ah + 2, Bc, fy + ah + 35)
        d.arrow(Bc, oy + r1 + RG + r2 + 2, Bc, fy - 5, "flow", "process：HTTP over unix socket")
        vy = fy + ah + 40
    else:
        vy = fy
        d.arrow(Bc, oy + ph + 2, Bc, vy - 5)
        d.text(Bc + 16, orch_bottom + 40, "checkpoint：暂停 → 写差分 ‖ 封存写层 → 恢复", 13.5, "#0F6E56", 600)
        d.text(Bc + 16, orch_bottom + 66, "restore：暂停 → 算回滚集 → 原地写回 → 切磁盘视图 → 恢复", 13.5, "#0F6E56", 600)
    d.block(A0, vy, Aw, vh, *vc, "gray")
    d.block(B0, vy, Bw, vh, *mm, "gray", tag=t2)
    if brief:
        nb = ("磁盘视图（live NBD 之下）", "模板底座 + 封存层 × k + 活写层")
    else:
        nb = ("NBD 设备 + Overlay", "模板 rootfs → 层栈 → 活写层；由 Sandbox 对象的 Rootfs Provider 持有")
    nh = max(vh, need(d, *nb, Cw))
    ny = vy + vh / 2 - nh / 2
    d.block(C0, ny, Cw, nh, *nb, "gray")
    d.arrow(B1 + 4, vy + vh / 2, C0 - 6, vy + vh / 2, "flow", "块 I/O")
    fc_bottom = fc_top + fc_h
    y = fc_bottom + 60

    # ---- KVM
    if brief:
        hd = ("脏页跟踪", "950：HDBSS 硬件标脏（启动时探测，自动启用）；无 HDBSS：KVM 软件写保护（需显式开启）")
        hw_title = "KVM · 鲲鹏"
    else:
        hd = ("脏页跟踪后端", "HDBSS 硬件标脏 / KVM 写保护 / 关闭")
        hw_title = "KVM · 宿主内核 · CPU"
    t4 = tag("④ 950 上由硬件标脏")
    hh = need(d, *hd, B1 - A0, tag=t4)
    panel(d, PL, y, B1 + 20 - PL, TB + hh + 18, hw_title)
    d.block(A0, y + TB, B1 - A0, hh, *hd, "gray", tag=t4)
    d.arrow(Bc, y + TB - 2, Bc, vy + vh + 5)          # 从 KVM 向上指到 Guest 内存
    d.text(Bc + 12, (y + TB + vy + vh) / 2 + 5, "脏页日志", 13, "#5F5E5A", 600)
    hw_bottom = y + TB + hh + 18

    # ---- 宿主本地存储
    panel(d, D0, orch_top, D1 - D0, hw_bottom - orch_top, "宿主本地存储")
    sx = D0 + 18
    sy = orch_top + 56
    for s in wrap("ORCHESTRATOR_BASE_PATH/build/checkpoints/<sandbox-id>/", D1 - D0 - 36, 13):
        d.text(sx, sy, s, 13, SUB)
        sy += 20
    sy += 10
    if brief:
        groups = [("ckpt_<UnixNano>/", "每个 checkpoint 一个目录",
                   [("snapfile", "vCPU 与设备状态"), ("mem_diff", "稀疏，仅本代脏页"),
                    ("mem_bitmap", "本代脏页位图"), ("rootfs.header", "这一刻的磁盘视图")]),
                  ("layers/", "封存写层，多代共享", [("layer-<uuid>", "一个封存层")])]
    else:
        groups = [("ckpt_<UnixNano>/", "一个 checkpoint 条目",
                   [("snapfile", "Firecracker 写"), ("mem_diff", "Firecracker 写"),
                    ("mem_bitmap", "Firecracker 写"), ("rootfs.header", "orchestrator 写"),
                    ("manifest.json", "orchestrator 写"), ("timings.json", "orchestrator 写")]),
                  ("layers/", "封存层，封存后改名移入", [("layer-<uuid>", "guest 经 NBD 写"),
                               ("layer-<uuid>.meta", "orchestrator 写")])]
    gw = D1 - D0 - 36
    for gt, gs, rows in groups:
        gh = 62 + 26 * len(rows) + 8
        d.rect(sx, sy, gw, gh, RAMPS["teal"][0], RAMPS["teal"][1], sw=1.2, rx=10)
        d.text(sx + 14, sy + 26, gt, 14.5, RAMPS["teal"][2], 600)
        d.text(sx + 14, sy + 47, gs, 13, RAMPS["teal"][3])
        for k, (fn, desc) in enumerate(rows):
            yy = sy + 74 + 26 * k
            d.text(sx + 14, yy, fn, 13.5, INK, 600)
            d.text(sx + 160, yy, desc, 13, RAMPS["teal"][3])
        sy += gh + 16
    if brief:
        lines_at(d, sx, sy + 8, ["绑定活沙箱的当前生命周期：", "不跨 orchestrator 重启、不跨节点"], 13, 20, MUTED)
    else:
        lines_at(d, sx, sy + 8, ["账本不读回这些文件：", "NewStore 启动时清空 store 根"], 13, 20, MUTED)
    # 进存储的三条箭头
    d.arrow(C1 + 14, ledger_mid, D0 - 6, ledger_mid)
    d.arrow(C1 + 4, vy + vh / 2, D0 - 6, vy + vh / 2, "flow")
    ey = fc_bottom + 28
    elbow(d, [(B1 - 40, vy + vh + 2), (B1 - 40, ey), (D0 - 6, ey)])
    d.text((B1 + D0) / 2, ey - 8, "snapfile · mem_diff · mem_bitmap", 12.5, SUB, 600, "middle")
    d.y = hw_bottom
    if brief:
        d.footer("注：控制面在宿主侧（第 4 章）；restore 原地写回的实现见第 9 章，脏页跟踪见第 5 章，差分树见第 6 章，磁盘分层见第 7 章。")
    else:
        d.footer("注：三处扩展的完整契约见第 8 章。Sandbox 对象经 Rootfs Provider 持有 NBD + Overlay，经 Slot 清连接跟踪表。"
                 "活写层在沙箱缓存目录，封存后才移进 layers/。")
    d.save(path)


# ------------------------------------------------------------------ 时序图（04-2 checkpoint、04-3 restore）
CORAL_BAND = ("#FDF3EF", "#E8A68C")


def _labels(label, width):
    return label.split("\n") if "\n" in label else wrap(label, width, 14)


def draw_seq(d, lanes, events, freeze, bw=560):
    """lanes: [(名字, 说明, x)]；events: 见下；freeze: (开始事件下标, 结束事件下标, 标签)。
    事件：("msg", 起, 止, 文字, 编号, 计时键, kind) / ("self", 泳道, 标题, 副标题, 编号, 计时键)
         / ("mark", 文字, 色阶)：横跨泳道的标记线。"""
    M, W = d.M, d.W
    LX0, LX1 = M + 64, W - M - 214          # 泳道区
    KX = W - M - 196                         # 计时键列
    lx = {n: x for n, _, x in lanes}
    top = d.y + 18
    # 泳道头
    hh = 58
    for n, sub, x in lanes:
        w = max(text_w(n, 16) + 40, 170)
        d.block(x - w / 2, top, w, hh, n, sub, "teal" if n == "orchestrator" else "gray", "outline", ts=16, ss=12.5)
    d.text(KX, top + 34, "计时键", 14, INK, 600)
    y = top + hh + 26
    # 先排版
    lay = []
    for i, ev in enumerate(events):
        if i == freeze[0]:
            y += 8
        if i == freeze[1] + 1:
            y += 12
        if ev[0] == "msg":
            _, a, b, label, num, key, kind = ev
            ls = _labels(label, abs(lx[a] - lx[b]) - 40)
            h = 22 + 20 * len(ls)
        elif ev[0] == "self":
            _, lane, t, sub, num, key = ev
            h = need(d, t, sub, bw, ts=15, ss=13, pad=22) + 16
        else:
            h = 40
        lay.append((y, h))
        y += h
    bottom = y + 10
    # 冻结窗口底色
    f0, f1, flab = freeze
    fy0 = lay[f0][0] - 6
    fy1 = lay[f1][0] + lay[f1][1] + 6
    d.rect(LX0 - 10, fy0, KX - LX0 - 6, fy1 - fy0, CORAL_BAND[0], CORAL_BAND[1], sw=1, rx=10, dash="6 4")
    d.text(LX0 + 4, fy0 + 22, flab, 14, RAMPS["coral"][2], 600)
    # 生命线
    for n, _, x in lanes:
        d.o.append(f'<line x1="{x:.1f}" y1="{top + hh:.1f}" x2="{x:.1f}" y2="{bottom:.1f}" stroke="#B4B2A9" '
                   f'stroke-width="1.2" stroke-dasharray="4 4"/>')
    # 事件
    for ev, (ey, h) in zip(events, lay):
        if ev[0] in ("msg", "self") and ev[4]:
            num = ev[4]
            nw = text_w(num, 12.5) + 16
            d.rect(M, ey + h / 2 - 11, nw, 22, "#F1EFE8", "#D3D1C7", sw=0.8, rx=11)
            d.text(M + nw / 2, ey + h / 2 + 4.5, num, 12.5, SUB, 600, "middle")
        if ev[0] in ("msg", "self") and ev[5]:
            for k, s in enumerate(wrap(ev[5], W - M - KX, 12.5)):
                d.text(KX, ey + h / 2 + 4.5 + 17 * k - 8.5 * (len(wrap(ev[5], W - M - KX, 12.5)) - 1), s, 12.5, MUTED)
        if ev[0] == "msg":
            _, a, b, label, num, key, kind = ev
            x1, x2 = lx[a], lx[b]
            ay = ey + h - 12
            dx = 4 if x2 > x1 else -4
            d.arrow(x1 + dx, ay, x2 - dx * 1.5, ay, kind)
            ls = _labels(label, abs(x2 - x1) - 40)
            for k, s in enumerate(ls):
                d.text((x1 + x2) / 2, ay - 9 - 20 * (len(ls) - 1 - k), s, 14, INK, 400, "middle")
        elif ev[0] == "self":
            _, lane, t, sub, num, key = ev
            x = lx[lane] - bw / 2
            d.block(x, ey + 8, bw, h - 16, t, sub, "teal", ts=15, ss=13)
        else:
            _, text, ramp = ev
            my = ey + h / 2
            col = "#E24B4A" if ramp == "red" else RAMPS[ramp][1]
            d.o.append(f'<line x1="{LX0 - 10:.1f}" y1="{my:.1f}" x2="{KX - 16:.1f}" y2="{my:.1f}" stroke="{col}" '
                       f'stroke-width="1.6" stroke-dasharray="7 4"/>')
            if ramp == "red":
                pill(d, LX0 + 4, my, text)
            else:
                f, st, tc, _ = RAMPS[ramp]
                pill(d, LX0 + 4, my, text, f, st, tc)
    d.y = bottom


def fig_ckpt_seq():
    d = Diagram(width=1600)
    d.header("一次 checkpoint 的时序：冻结窗口里只有暂停、写快照、封存写层、恢复",
             "编号对应 §7 的步骤表；右列是写进 timings.json 的计时键。Firecracker 进程全程不重建，封存层移入 store 和提交都在窗口外。",
             legend=[("flow", "调用"), ("back", "返回调用方")])
    lanes = [("Python SDK", "调用方", 160), ("orchestrator", "Checkpoint Service", 540),
             ("Firecracker", "同一个进程", 930), ("宿主本地存储", "store 根", 1250)]
    S, O, F, D = "Python SDK", "orchestrator", "Firecracker", "宿主本地存储"
    ev = [
        ("msg", S, O, "CreateCheckpoint（端口 49984，宿主拦截）", "1", "", "flow"),
        ("self", O, "进入服务、决定与准备（窗口外）",
         "鉴权 → 取沙箱操作锁 → 水位检查 → 决定全量 / 增量 → Prepare（个数与字节配额）", "2–12", ""),
        ("msg", O, F, "暂停虚机", "13", "pause", "flow"),
        ("msg", O, F, "（仅全量）导出活跃脏页位图", "14", "save_live_bitmap", "flow"),
        ("msg", O, F, "写快照（一次调用）\nvmstate + 稀疏内存差分 + 位图侧车", "15", "snapshot", "flow"),
        ("msg", F, D, "写出 snapfile、mem_diff、\nmem_bitmap（先写成 .tmp）", "", "", "flow"),
        ("mark", "纪元前进：此后失败不能简单删目录，走 failCreate", "coral"),
        ("self", O, "封存写层", "刷屏障 → 写层就地封存为只读层，挂上新的空写层；不搬运数据", "16", "seal"),
        ("msg", O, F, "恢复虚机（defer，无条件）", "17", "resume", "flow"),
        ("msg", O, D, "封存层文件移入 store 的 layers 目录", "18", "seal_move", "flow"),
        ("self", O, "AppendLayer", "锁内把新层折进合并映射；锁外写层 sidecar 与本条目的 rootfs.header", "19",
         "append_layer、append_lock_wait"),
        ("self", O, "Commit", "rename 到位（不 fsync）→ manifest 翻成 committed → 锁内发布，基准 ← 本条目", "20",
         "commit_files、commit_manifest、commit_lock_wait、commit_index"),
        ("mark", "提交：条目从 prepared 变为 committed，可见、可恢复", "red"),
        ("msg", O, S, "{checkpointId, memMode}", "21", "total", "back"),
    ]
    draw_seq(d, lanes, ev, (2, 8, "冻结窗口（frozen：pause 到 resume）"), bw=580)
    d.footer("注：第 14 步只在全量时做。seal_move 登记在 resume 之前、按 defer 的倒序在它之后执行，所以落在窗口外；"
             "跨文件系统时它是一次拷贝。第 15 步成功后的三层降级见 §7.6。")
    d.save(out("rollback", "04-2-checkpoint时序"))


def fig_restore_seq():
    d = Diagram(width=1600)
    d.header("一次 restore 的时序：提交点在 Firecracker 回滚调用内部",
             "编号对应 §8 的步骤表；右列是写进 last-restore-timings.json 的计时键。磁盘视图在窗口外装配好，"
             "提交点之后先更新账本、再等 envd。",
             legend=[("flow", "调用"), ("back", "返回调用方")])
    lanes = [("Python SDK", "调用方", 170), ("orchestrator", "Checkpoint Service", 560),
             ("Firecracker", "同一个进程，全程不重建", 1050)]
    S, O, F = "Python SDK", "orchestrator", "Firecracker"
    ev = [
        ("msg", S, O, "RestoreCheckpoint(checkpointId)", "1", "", "flow"),
        ("self", O, "准备（窗口外）",
         "鉴权 → 取锁 → 取条目 → 读层 sidecar → 水位检查 → 装配磁盘视图 → 丢弃代理连接池", "2–13",
         "disk_view_read、assemble_view_pre"),
        ("msg", O, F, "暂停虚机；后台启动 conntrack 清理", "14", "pause", "flow"),
        ("msg", O, F, "导出活跃脏页位图", "15", "save_live_bitmap", "flow"),
        ("self", O, "物化回滚集", "回滚集 = 树路径各代位图 ∪ 活跃脏页；沿目标祖先链物化内容", "16–17",
         "materialize、revert_mem_write、revert_bitmap_write"),
        ("msg", O, F, "PUT /snapshot/rollback\n内存 → vCPU → GIC → 设备 → VMGenID", "18", "fc_rollback、fc_*", "flow"),
        ("mark", "提交点（在第 18 步的调用内部，第 9 章）：之前失败虚机原样恢复，之后失败沙箱撕裂", "red"),
        ("self", O, "切换磁盘视图", "挂载不动，把装配好的视图与一个新空写层整体换上", "19", "reset_view"),
        ("self", O, "join conntrack 清理", "清理在第 14 步已并行启动，这里只等它", "20", "conntrack、conntrack_bg"),
        ("msg", O, F, "恢复虚机", "21", "resume", "flow"),
        ("self", O, "更新账本", "基准 ← 目标；磁盘账本 ← 目标 header", "22–23", ""),
        ("msg", O, F, "等 guest 内 envd 应答（上限 45 s）", "24", "wait_envd", "flow"),
        ("msg", O, S, "success", "25", "total", "back"),
    ]
    draw_seq(d, lanes, ev, (2, 9, "冻结窗口（frozen：pause 到 resume 之后，含第 20 步的等待）"), bw=620)
    d.footer("注：第 14 步必须在 15、16 之前，物化的内容才覆盖 Firecracker 要写回的全部页；conntrack 必须在恢复之前清完。"
             "账本跟着状态走，不跟着 RPC 走。")
    d.save(out("rollback", "04-3-restore时序"))


# ------------------------------------------------------------------ 三个时钟（04-4）
def _segs(d, x0, x1, labels, y, h, style):
    """按标签宽度把 [x0, x1] 分成若干段，返回每段的 (左, 右)。"""
    ws = [text_w(t, 13) + 26 for t in labels]
    k = (x1 - x0) / sum(ws)
    xs, x = [], x0
    for t, w in zip(labels, ws):
        w *= k
        xs.append((x, x + w))
        if style:
            ramp, st = style
            f, sc, tc, _ = RAMPS[ramp]
            d.rect(x + 1.5, y, w - 3, h, f if st == "fill" else "#FFFFFF", sc, sw=1.1, rx=6,
                   dash="5 4" if st == "dashed" else None)
            d.text(x + w / 2, y + h / 2 + 4.5, t, 13, tc if st != "dashed" else MUTED, 600 if st != "dashed" else 400,
                   "middle")
        x += w
    return xs


def _clock_block(d, pre, frozen, post, fc=None, pre_dashed=False):
    M, W = d.M, d.W
    LW = 230
    X0, X1 = M + LW, W - M
    NET = 92
    rows = [("客户端墙钟", "调用方等了多久"), ("冻结窗口 frozen", "业务停了多久"),
            ("宿主分阶段 timings", "停的这段时间花在哪")]
    if fc:
        rows.append(("fc_*", "Firecracker 自报，微秒"))
    RH, BH = 70, 36
    y0 = d.y + 14
    ys = [y0 + i * RH for i in range(len(rows))]
    if fc:
        ys[3] += 24
    for (t, sub), y in zip(rows, ys):
        d.text(M, y + BH / 2 - 2, t, 14.5, INK, 600)
        d.text(M, y + BH / 2 + 17, sub, 12.5, MUTED)
    # 第三行先排：窗口外前段、窗口内、窗口外后段
    labels = pre + frozen + post
    segs = _segs(d, X0 + NET, X1 - NET, labels, ys[2], BH, None)
    n0, n1 = len(pre), len(pre) + len(frozen)
    fx0, fx1 = segs[n0][0], segs[n1 - 1][1]
    # 墙钟
    f, sc, tc, _ = RAMPS["gray"]
    d.rect(X0, ys[0], X1 - X0, BH, f, sc, sw=1.2, rx=6)
    d.text((X0 + X1) / 2, ys[0] + BH / 2 + 5, "SDK 调用耗时", 14, INK, 600, "middle")
    for x in (X0 + NET / 2, X1 - NET / 2):
        d.text(x, ys[0] + BH / 2 + 4.5, "网络往返", 12.5, SUB, 400, "middle")
    # 冻结窗口
    f, sc, tc, _ = RAMPS["coral"]
    d.rect(fx0, ys[1], fx1 - fx0, BH, f, sc, sw=1.2, rx=6)
    d.text((fx0 + fx1) / 2, ys[1] + BH / 2 + 5, "frozen：业务实际感受到的停顿", 14, tc, 600, "middle")
    for x0, x1, t in ((X0 + NET, fx0, "窗口外"), (fx1, X1 - NET, "窗口外")):
        if x1 - x0 > 40:
            d.text((x0 + x1) / 2, ys[1] + BH / 2 + 4.5, t, 12.5, MUTED, 400, "middle")
    # 分阶段
    if pre:
        _segs(d, X0 + NET, segs[n0][0], pre, ys[2], BH, ("gray", "dashed") if pre_dashed else ("teal", "outline"))
    _segs(d, fx0, fx1, frozen, ys[2], BH, ("teal", "fill"))
    _segs(d, segs[n1][0] if post else fx1, X1 - NET, post, ys[2], BH, ("teal", "outline")) if post else None
    # 窗口边界的竖虚线
    for x in (fx0, fx1):
        d.o.append(f'<line x1="{x:.1f}" y1="{ys[1] - 6:.1f}" x2="{x:.1f}" y2="{ys[2] + BH + 6:.1f}" '
                   f'stroke="{RAMPS["coral"][1]}" stroke-width="1.2" stroke-dasharray="4 3"/>')
    bottom = ys[2] + BH
    if fc:
        name, parts = fc
        i = n0 + frozen.index(name)
        a, b = segs[i]
        zx0, zx1 = X0 + 180, X1 - 180
        _segs(d, zx0, zx1, parts, ys[3], BH, ("gray", "fill"))
        for p, q in ((a, zx0), (b, zx1)):
            d.o.append(f'<line x1="{p:.1f}" y1="{ys[2] + BH:.1f}" x2="{q:.1f}" y2="{ys[3]:.1f}" stroke="#B4B2A9" '
                       f'stroke-width="1" stroke-dasharray="4 3"/>')
        bottom = ys[3] + BH
    d.y = bottom


def fig_clocks():
    d = Diagram(width=1600)
    d.header("三个时钟：客户端墙钟 ⊃ 冻结窗口 ⊃ 宿主分阶段，fc_* 来自 Firecracker 自报",
             "三个时钟回答三个不同的问题。示意图，各段长度不按比例；格子里是 PhaseTimings 的键。")
    swatches(d, d.y + 34, [("teal", "orchestrator 自己计时：窗口内"), ("teal", "窗口外", "outline"),
                           ("coral", "派生：Mark(\"frozen\", pausedAt)"), ("gray", "Firecracker 回报（SetUs）"),
                           ("gray", "窗口外的步骤，不是计时键", "dashed")])
    d.y += 34
    d.section(1, "checkpoint", note="第 14 步 save_live_bitmap 只在全量时有")
    _clock_block(d, ["鉴权、取锁、Prepare"],
                 ["pause", "save_live_bitmap", "snapshot", "seal", "resume"],
                 ["seal_move", "append_layer", "commit_*"], pre_dashed=True)
    d.section(2, "restore", note="fc_rollback 里 Firecracker 再分段自报，下面列的是其中几个键", highlight="本图重点")
    _clock_block(d, ["disk_view_read", "assemble_view_pre"],
                 ["pause", "save_live_bitmap", "materialize", "fc_rollback", "reset_view", "conntrack", "resume"],
                 ["wait_envd"],
                 fc=("fc_rollback", ["fc_validate", "fc_memory", "fc_vcpus", "fc_gic", "fc_devices"]))
    d.footer("注：键默认单位是毫秒，以 _mb 结尾的是 MB，以 _count 结尾的是计数；全部键的定义见第 27 章。"
             "排查时先看哪个时钟异常，就知道该往哪一层找。")
    d.save(out("rollback", "04-4-三个时钟"))


if __name__ == "__main__":
    draw_arch(False, out("rollback", "04-1-四个层次"))
    fig_ckpt_seq()
    fig_restore_seq()
    fig_clocks()
