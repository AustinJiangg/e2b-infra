"""第 09 章配图：09-1 冷重建与原地回滚；09-2 九个阶段与失败模型。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, wrap, text_w


def fig1():
    d = Diagram(width=1600)
    d.header("回到一个快照：原生 resume 重建整台虚机，原地回滚只写回差异",
             "原生 resume 新建 Firecracker 进程，宿主资源全部重做，工作集靠缺页换回；"
             "原地回滚往同一个进程的活对象上写，代价正比于回滚集大小，与虚机规格无关。",
             legend=[("flow", "执行顺序"), ("warn", "恢复之后仍要付的代价"), ("back", "直接继续运行")])

    d.section(1, "两条恢复路径", highlight="本图重点")
    c = d.compare([("冷重建：原生 resume", "Factory.ResumeSandbox 新建进程加载快照", "coral"),
                   ("原地回滚：本方案 restore", "往活着的 Firecracker 进程里写回差异", "teal")])
    c.states("恢复前", [("沙箱已离场", "旧 Firecracker 进程已停，快照在本地缓存或对象存储"),
                        ("沙箱活着，虚机已暂停", "同一个 Firecracker 进程，宿主资源都在")])
    c.span("触发", "回到一个快照：原生 Sandbox.connect() 触发 resume / 本方案调用 restore")
    c.stack("恢复做什么", [
        [("新建 Firecracker 进程", "对外 SandboxID 不变，底下是全新进程"),
         ("建 KVM VM、vCPU、GIC、virtio 设备", None),
         ("注册 eventfd / irqfd / ioeventfd", None),
         ("建 tap、分配网络槽位", None),
         ("起 UFFD handler，挂上 guest 内存", "新进程的 guest 内存映射是空的"),
         ("建 NBD 设备 + Overlay", "模板 rootfs + 新写层"),
         ("加载 snapfile", "恢复 vCPU、GIC、设备状态"),
         ("运行", None)],
        [("虚机暂停", "回滚在 VMM 线程上执行"),
         ("校验、静默 I/O", "拓扑一致、内存文件覆盖回滚集；排空在途设备 I/O"),
         ("算回滚集", "活跃脏页 ∪ 调用方给的累积集合"),
         ("把差异页写回活映射", "只处理回滚集里的页"),
         ("vCPU / GIC / 设备状态写回既有对象", "作用在既有的 fd 上"),
         ("切磁盘视图", "NBD 设备、Overlay 保留，只换视图"),
         ("恢复运行", "一个宿主资源都不重建")],
    ], links=["flow", "flow"])
    c.row("运行之后", [
        ("guest 逐页缺页，把工作集换回内存",
         "缺页 → UFFD handler → 查本地缓存 → 没有就按 header 从对象存储拉那一块；prefetch 按上次缺页顺序提前拉，只能缓解"),
        ("工作集留在物理内存里", "没被回退的页原封不动，guest 访问它们不缺页")],
        links=[("warn", "落在业务的第一批请求上"), ("back", "直接继续")])
    c.summary("哪些对象保留、哪些重建", [
        ("进程、KVM VM fd、vCPU fd", [("新建", "bad"), ("保留", "good")]),
        ("GIC 设备 fd", [("新建", "bad"), ("保留", "good")]),
        ("eventfd / irqfd / ioeventfd 注册", [("全部重做", "bad"), ("保留", "good")]),
        ("tap 设备、网络槽位", [("重建", "bad"), ("保留", "good")]),
        ("guest 内存映射", [("重新 mmap", "bad"), ("保留", "good")]),
        ("guest 工作集", [("靠缺页重新换入", "bad"), ("留在物理内存里", "good")]),
        ("NBD 设备、Overlay", [("重建", "bad"), ("保留，只换视图", "good")]),
        ("代价正比于", ["虚机规格与工作集", "回滚集大小"]),
    ])
    c.end()
    d.footer("注：原生 resume 适合跨节点迁移、长期持久化、进程崩溃后恢复、沙箱离场后再回来；"
             "原地回滚服务活沙箱内的高频回退。两者并存，本方案没有改动原生的恢复路径。")
    d.save(out("rollback", "09-1-冷重建与原地回滚"))


# ---------------------------------------------------------------- 09-2
STAGES = [
    # (#, 阶段, 做什么, 副作用, 是否改 guest, 计时键)
    (1, "校验", "状态、快照可读、拓扑、内存文件长度、位图几何", "无", False, "fc_validate"),
    (2, "静默", "排空在途设备 I/O", "丢弃 tap 缓存帧，不改 guest 状态", False, "fc_quiesce"),
    (3, "取活跃脏图", "取 KVM 脏页日志折回用户态位图，与调用方给的位图求并；校验内存文件覆盖回滚集",
     "只改 Firecracker 自己的账", False, "fc_bitmap"),
    (4, "内存", "按回滚集把页写回活映射", "改 guest 状态", True, "fc_memory"),
    (5, "vCPU", "排干挂起 MMIO → 复位 → 全寄存器恢复 → 读回比对", "改 guest 状态", True, "fc_vcpus"),
    (6, "中断控制器", "对既有 GIC fd 绝对写回；清串口那根 SPI", "改 guest 状态", True, "fc_gic"),
    (7, "设备", "virtio 队列、协商特性、中断状态；串口复位；RX 缓存；tap offload", "改 guest 状态", True, "fc_devices"),
    (8, "VMGenID", "写入新代号，通知 guest 时间被回拨", "改 guest 状态", True, "计入 fc_devices"),
    (9, "重置基线", "清用户态脏页位图，重新标脏队列页", "改 Firecracker 的账", False, "计入 fc_total"),
]


def fig2():
    d = Diagram(width=1600)
    M = d.M
    d.header("原地回滚的九个阶段：提交点把失败分成两半",
             "阶段 1–3 失败时 guest 一个字节都没动，恢复运行即可；阶段 4 起改 guest 状态，"
             "此后失败虚机是两个时刻的混合体，标记 Faulted，只能重建沙箱。",
             legend=[("flow", "执行顺序"), ("warn", "提交点之后失败"), ("back", "提交点之前失败：恢复运行")])

    d.section(1, "九个阶段与各段失败时虚机的状态", highlight="本图重点",
              note="虚机全程暂停，在 VMM 线程上执行（rollback_snapshot_inner）")

    # 列布局
    xs_num = M + 4
    x_box, w_box = M + 52, 600              # 阶段框
    x_eff, w_eff = x_box + w_box + 28, 250  # 副作用
    x_key = x_eff + w_eff + 16              # 计时键
    x_out = 1220                            # 失败结果
    w_out = d.W - M - x_out
    RH, RG = 66, 26                         # 行高、行距
    COMMIT_GAP = 64

    y = d.y + 10
    # 列标题
    for x, s in ((x_box, "阶段 · 做什么"), (x_eff, "副作用"), (x_key, "计时键"), (x_out, "这一段失败时")):
        d.text(x, y + 14, s, 14, MUTED, 600)
    y += 30

    ys = []
    for i, (n, name, what, eff, guest, key) in enumerate(STAGES):
        if i == 3:
            y += COMMIT_GAP
        ys.append(y)
        ramp = "teal"
        d.block(x_box, y, w_box, RH, f"阶段 {n} · {name}", what, ramp, "fill" if n >= 4 and n <= 8 else "outline",
                ts=15.5, ss=13)
        col = RAMPS["coral"][3] if guest else SUB
        d.text(x_eff, y + RH / 2 + 5, eff, 14, col, 600 if guest else 400)
        d.text(x_key, y + RH / 2 + 5, key, 13.5, INK)
        y += RH + RG
    # 阶段之间的竖箭头
    for i in range(len(ys) - 1):
        top = ys[i] + RH
        d.arrow(x_box + 40, top + 2, x_box + 40, ys[i + 1] - 5, "flow")

    # 提交点
    cy = ys[2] + RH + RG + (COMMIT_GAP - RG) / 2 + 2
    d.o.append(f'<line x1="{M}" y1="{cy:.1f}" x2="{d.W - M}" y2="{cy:.1f}" stroke="#D85A30" '
               f'stroke-width="2.4" stroke-dasharray="10 6"/>')
    lab = "提交点：此后 guest 内存开始改变（faults_vm 的穷尽匹配把错误分成两半）"
    lw = text_w(lab, 15) + 28
    lx = x_box + 90
    d.rect(lx, cy - 15, lw, 30, "#FFFFFF", "#D85A30", sw=1.2, rx=15)
    d.text(lx + lw / 2, cy + 5.5, lab, 15, RAMPS["coral"][2], 600, "middle")

    # 失败结果：两个括号框
    pre_top, pre_bot = ys[0], ys[2] + RH
    d.block(x_out, pre_top, w_out, pre_bot - pre_top, "虚机原封不动",
            "HTTP 400；orchestrator 恢复运行，沙箱继续可用。拓扑校验、覆盖校验的拒绝都在这一段",
            "gray", "outline")
    post_top, post_bot = ys[3], ys[8] + RH
    d.block(x_out, post_top, w_out, post_bot - post_top, "标记 Faulted",
            "HTTP 500 且响应体 \"fault\": true；拒绝 resume 与 snapshot；只能重建沙箱",
            "coral", "fill")
    # 从阶段区指向结果区的箭头
    gx0 = x_key + 108
    d.arrow(gx0, (pre_top + pre_bot) / 2, x_out - 6, (pre_top + pre_bot) / 2, "back")
    d.arrow(gx0, (post_top + post_bot) / 2, x_out - 6, (post_top + post_bot) / 2, "warn")

    # 测试注入点：阶段 5 与 6 之间
    iy = ys[5] - RG / 2
    tag = "测试注入点：FC_ROLLBACK_FAULT_INJECT=post_commit 在此注入 RollbackError::Gic（仅 rollback-fault-inject 构建）"
    tw = text_w(tag, 12.5) + 20
    d.rect(x_box + 90, iy - 11, tw, 22, "#FFFFFF", "#D85A30", sw=0.8, rx=11, dash="4 3")
    d.text(x_box + 90 + tw / 2, iy + 4.5, tag, 12.5, RAMPS["coral"][3], 600, "middle")

    y = ys[-1] + RH + 14
    # 第 10 步
    d.arrow(x_box + 40, y - 12, x_box + 40, y + 22, "flow")
    d.block(x_box, y + 26, w_box, 58, "resume_vm：踢设备重新处理队列",
            "每次恢复都会无条件做，回滚本身不必为此多做什么", "gray", "dashed", ts=15.5, ss=13)
    d.y = y + 26 + 58

    # 第二段：orchestrator 侧
    d.section(2, "orchestrator 怎样处理（RollbackInPlace）", note="internal/sandbox/checkpoint.go")
    cards = [
        ("响应体 fault 为 true", "RollbackFaultedError → RollbackTornError", "coral"),
        ("rollback 调用超时", "CHECKPOINT_FC_CALL_TIMEOUT 内没应答，判断不了停在提交点哪一侧，同样按撕裂处理", "coral"),
        ("磁盘 ResetView 失败", "内存已在目标时刻、磁盘还不是：也在提交点之后，返回 RollbackTornError", "coral"),
        ("其余错误", "join conntrack 清理后恢复虚机，按普通失败返回，沙箱在原状态继续运行", "gray"),
    ]
    gap = 24
    n = len(cards)
    cw = (d.W - 2 * M - (n - 1) * gap) / n
    h = max(d._need(t, s, cw) for t, s, _ in cards)
    for i, (t, s, r) in enumerate(cards):
        d.block(M + i * (cw + gap), d.y, cw, h, t, s, r, "fill" if r == "coral" else "outline")
    d.y += h
    d.callout("磁盘视图的装配（AssembleView）在暂停之前完成，装配失败只是一次普通的失败 restore", "gray")
    d.footer("注：Faulted 由 rollback_snapshot 自己标记（brand_vm_on_faulting_failure），不依赖 HTTP 层。"
             "各阶段计时单位为微秒，orchestrator 把它们摊平进自己的 timings。")
    d.save(out("rollback", "09-2-九个阶段与失败模型"))


if __name__ == "__main__":
    fig1()
    fig2()
