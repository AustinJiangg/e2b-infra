"""第 10 章配图：原地回滚后要单独处理的几类状态（§2～§6）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w

# (状态, 状态副标题, 不处理会怎样, 谁处理 / 在哪一步, 怎么处理)，均取自第 10 章 §2～§6
ROWS = [
    ("§2 网络 RX 描述符缓存", "rx_buffer.parsed_descriptors",
     "仍按被丢弃的时间线解析；guest 报 id N is not a head!，RX 永久卡死",
     "Firecracker", "阶段 7 · apply_net_rx_cache",
     "原地 clear()，next_avail 退回已解析的条数，从已回滚的 avail 环重新解析",
     "不新建 RxBuffers：VMM 线程的 seccomp 没有放行 memfd_create"),
    ("§3 VMGenID 代号", "guest 内存里的 128 位代号",
     "若在内存回滚前写，会被目标时刻的旧值覆盖；guest 不知道时间被回拨",
     "Firecracker", "阶段 8 · 内存写回之后",
     "写入新代号，不是快照里的那个", None),
    ("§4 连接跟踪表 conntrack", "命名空间内的表、宿主的表",
     "跨越回滚的连接序号在内核看来倒退，包被丢弃；连接挂死，没有报错",
     "orchestrator", "pause 后启动，resume 前 join",
     "命名空间内整表 flush；宿主表只删本 slot 地址的表项",
     "宿主表项按原始元组任一端是 slot 地址认定；清扫器合批扫描，按代价模型选整表扫描或内核过滤"),
    ("§4.6 代理的连接池", "代理对每个沙箱维护的连接",
     "池里还开着的连接，对端（guest 里的服务）已经不记得",
     "orchestrator", "beginRestore · 暂停虚机之前",
     "丢弃本沙箱的连接；unary 调用被改判成 409 sandbox_restored", "丢弃失败只记 WARN"),
    ("§5 时间", "单调时钟与墙钟",
     "单调时钟随计时器寄存器在阶段 5 回到快照时刻；墙钟留在过去，时间戳全错",
     "envd", "finishRestore 等它应答",
     "单调时钟保持回拨；envd 初始化把墙钟校回当前时间",
     "回拨是快照语义的一部分；验收的活性判据只看心跳计数增长"),
    ("§6 队列页的脏标记", "virtio 队列的环页",
     "Firecracker 自己写环页，不经过 Stage-2；阶段 9 清空位图后下一代 Diff 会漏掉它们",
     "Firecracker", "阶段 9 · 清空位图之后",
     "立刻对已激活设备调 mark_queue_memory_dirty", None),
]


def fig1():
    d = Diagram(width=1600)
    M = d.M
    d.header("原地回滚改的是活对象：六类状态要在 restore 里单独处理",
             "重建路线从快照构造新设备对象，这些问题不存在。原地路线保留宿主对象，从活对象推导、又不在快照里的状态，"
             "以及宿主上记得 guest 的状态都留了下来，而且是按被丢弃的时间线推导的（§1）。",
             legend=[("flow", "执行顺序"), ("async", "后台并行"), ("warn", "不处理时的后果")])

    # ---- 第 1 段：逐类的问题、谁处理、怎么处理
    d.section(1, "每类状态：不处理会怎样、谁处理、怎么处理", highlight="本图重点")
    LW = 300
    x_bad = M + LW
    w_bad = 380
    x_who = x_bad + w_bad + 40
    w_who = 236
    x_fix = x_who + w_who + 40
    w_fix = d.W - M - x_fix
    y = d.y + 6
    for x, s in ((M + 4, "状态"), (x_bad, "不处理会怎样"), (x_who, "谁处理 · 在哪一步"), (x_fix, "怎么处理")):
        d.text(x, y + 14, s, 14, MUTED, 600)
    y += 30
    rgap = 22
    for k, (name, nsub, bad, who, when, fix, fsub) in enumerate(ROWS):
        h = max(d._need(name, nsub, LW - 24, ts=16, ss=13, pad=24),
                d._need("", bad, w_bad, ss=13.5, pad=24),
                d._need(who, when, w_who, ts=15.5, ss=13, pad=24),
                d._need(fix, fsub, w_fix, ts=15, ss=13, pad=24))
        d.block(M, y, LW - 24, h, name, nsub, "gray", "outline", ts=16, ss=13)
        # 后果：白底珊瑚边，只放一段说明
        fill, stroke, tc, sc = RAMPS["coral"]
        d.rect(x_bad, y, w_bad, h, "#FFFFFF", stroke)
        from diagram_kit import wrap
        lines = wrap(bad, w_bad - 32, 13.5)
        y0 = y + h / 2 - (len(lines) - 1) * 10.5
        for j, s in enumerate(lines):
            d.text(x_bad + w_bad / 2, y0 + 5 + 21 * j, s, 13.5, sc, 400, "middle")
        d.arrow(M + LW - 20, y + h / 2, x_bad - 6, y + h / 2, "warn")
        d.block(x_who, y, w_who, h, who, when, "gray", "fill", ts=15.5, ss=13)
        d.arrow(x_bad + w_bad + 4, y + h / 2, x_who - 6, y + h / 2, "flow")
        d.block(x_fix, y, w_fix, h, fix, fsub, "teal", "fill", ts=15, ss=13)
        d.arrow(x_who + w_who + 4, y + h / 2, x_fix - 6, y + h / 2, "flow")
        y += h + rgap
    d.y = y - rgap

    # ---- 第 2 段：它们在一次 restore 里的位置
    d.section(2, "它们落在一次 restore 的哪一步", note="两条次序约束：通知写在内存回滚之后；表项在流量放回来之前清完")
    steps = [
        ("丢弃代理连接池", "beginRestore", "orchestrator"),
        ("暂停虚机", "pause", "orchestrator"),
        ("阶段 4～5", "内存写回；vCPU 恢复，单调时钟回拨", "Firecracker"),
        ("阶段 7", "写回设备与 RX 缓存", "Firecracker"),
        ("阶段 8", "写入新 VMGenID", "Firecracker"),
        ("阶段 9", "清位图，标脏队列页", "Firecracker"),
        ("join 后 resume", "表项在流量放回前清完", "orchestrator"),
        ("等 envd 应答", "墙钟校回当前", "orchestrator"),
    ]
    n = len(steps)
    gap = 26
    bw = (d.W - 2 * M - (n - 1) * gap) / n
    h = max(d._need(t, s, bw, actor=True, ts=15, ss=13) for t, s, _ in steps)
    y = d.y + 4
    xs = [M + i * (bw + gap) for i in range(n)]
    for i, (t, s, a) in enumerate(steps):
        ramp = "teal" if a == "Firecracker" else "gray"
        d.block(xs[i], y, bw, h, t, s, ramp, "fill", actor=a, ts=15, ss=13)
        if i + 1 < n:
            d.arrow(xs[i] + bw + 3, y + h / 2, xs[i + 1] - 5, y + h / 2, "flow")
    # 后台 conntrack 清理：从暂停之后到 join
    by = y + h + 44
    bx0 = xs[2]
    bx1 = xs[6] - 14
    bh = 64
    d.block(bx0, by, bx1 - bx0, bh, "后台清理 conntrack（startConntrackFlush）",
            "与导出位图、物化、Firecracker 回滚、ResetView 并行；命名空间表 flush 与宿主表清扫之间也并行", "teal", "dashed", ts=15, ss=13)
    px = xs[1] + bw / 2
    d.o.append(f'<path d="M{px:.1f} {y + h + 2:.1f} L{px:.1f} {by + bh / 2:.1f} L{bx0 - 6:.1f} {by + bh / 2:.1f}" '
               f'fill="none" stroke="#1D9E75" stroke-width="1.8" stroke-dasharray="6 4" marker-end="url(#ar-async)"/>')
    jx = xs[6] + bw / 2
    d.o.append(f'<path d="M{bx1 + 2:.1f} {by + bh / 2:.1f} L{jx:.1f} {by + bh / 2:.1f} L{jx:.1f} {y + h + 6:.1f}" '
               f'fill="none" stroke="#1D9E75" stroke-width="1.8" stroke-dasharray="6 4" marker-end="url(#ar-async)"/>')
    d.kinds.add("async")
    d.y = by + bh
    d.footer("注：阶段编号沿用第 9 章的九个阶段。加新设备、改回滚路径时逐条核对的检查表见本章 §8。")
    d.save(out("rollback", "10-1-原地回滚后单独处理的状态"))


if __name__ == "__main__":
    fig1()
