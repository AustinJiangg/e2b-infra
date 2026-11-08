"""第 15 章配图：15-1 活体原地与自足重建的逐项对比；
15-2 一个沙箱生命周期里 checkpoint / restore 与原生 pause / resume 怎样交替。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, wrap, text_w


def panel(d, x, y, w, h, label, note=None):
    d.rect(x, y, w, h, "#FBFBF9", "#D3D1C7", sw=1, rx=16)
    d.text(x + 24, y + 32, label, 16.5, INK, 600)
    if note:
        d.text(x + 24 + text_w(label, 16.5) + 18, y + 32, note, 14, MUTED)


def fig1():
    d = Diagram(width=1600)
    d.header("两种快照的根本差别：产物是否自足，恢复是否依赖原来那个活着的进程",
             "原生快照选“自足 + 重建”，服务沙箱离场之后；本方案选“活体 + 原地”，服务活沙箱里的高频回退。"
             "各自的长处和边界都从这一个选择推出来。逐步的恢复过程见图 09-1。",
             legend=[("flow", "先后顺序"), ("warn", "停机、重建的代价")])

    d.section(1, "各解决什么问题，怎样拍、怎样恢复", highlight="本图重点")
    c = d.compare([("原生快照：自足 + 重建", "Pause / Checkpoint RPC / resume", "coral"),
                   ("本方案：活体 + 原地", "checkpoint / restore", "teal")])
    c.states("面向的需求", [("沙箱离场之后", "离场再回来、长期保存、跨节点迁移、崩溃恢复、冷启动"),
                          ("活沙箱里的高频快速回退", "高频、低延迟、沙箱不能中断")])
    c.row("拍快照", [("停掉沙箱，导出内存与磁盘差分", "Checkpoint RPC 再从新快照拉起新进程：SandboxID / ExecutionID 不变，LifecycleID 换新"),
                    ("一次短暂停，然后继续跑", "Firecracker 写本代脏页的稀疏差分，磁盘写层原地封存")],
          links=[None, None])
    c.row("产物", [("自足：snapfile、内存差分、磁盘差分、header 映射链", "进对象存储或本地模板存储，任何节点都能据此新建进程"),
                  ("相对模板的增量，宿主本地", "随这一代 Firecracker 进程回收")])
    c.row("恢复", [("新建进程", "建 KVM 虚机、vCPU、中断控制器、virtio 设备、tap、uffd，加载 snapfile，缺页换回工作集"),
                  ("一次暂停里写回同一个进程", "回滚集的页、vCPU、中断控制器、设备状态；磁盘视图原地切换")],
          links=["warn", "flow"])
    c.row("边界", [("每次拍都停机导出，每次恢复都重建、重新换入工作集", None),
                  ("产物离开这个进程就没有意义", None)],
          links=[None, None])
    c.summary("逐项对比", [
        ("打快照 · guest 停顿", [("整个操作", "bad"), ("O(D) 的冻结窗口", "good")]),
        ("恢复 · 进程与设备", [("全部重建", "bad"), ("零", "good")]),
        ("恢复 · 内存", ["O(W) 缺页换入", "O(R) 写回"]),
        ("历史结构", ["线性链", ("树，可前滚、可跨分支", "good")]),
        ("跨节点 / 崩溃恢复", [("可以", "good"), ("不可以", "bad")]),
        ("典型频次", ["一个沙箱一两次", "一个任务里十几次"]),
    ])
    c.end()
    d.callout("要让 checkpoint 脱离活沙箱，需补齐全量产物、压平磁盘层、从文件重建账本、恢复改成新建进程；"
              "补齐之后就是原生 snapshot，沿途丢掉的正是本方案的全部收益（第 12 章 §15）", "gray")
    d.footer("注：D = 本次改动量，R = 回退跨度，W = 工作集（§3.2 成本模型）。完整的机制对比见 §3.1 表。")
    d.save(out("rollback", "15-1-活体原地与自足重建"))


def fig2():
    d = Diagram(width=1600)
    M = d.M
    d.header("一个沙箱的生命周期：checkpoint / restore 在一代进程内来回，原生 pause / resume 换代",
             "以一个 Agent 工作流为例（①–⑩ 按发生顺序）。一代 Firecracker 进程内可以反复 checkpoint、restore；"
             "原生 pause 把这一刻固化进存储，resume 起的是新一代，上一代的 checkpoint 随之作废。",
             legend=[("flow", "执行顺序"), ("back", "restore：回到某个 checkpoint")])
    d.section(1, "一个 Agent 工作流，以及底下发生了什么", highlight="本图重点")

    n, gap = 5, 54
    px, pw = M, d.W - 2 * M
    nw = (pw - 48 - (n - 1) * gap) / n
    xs = [px + 24 + i * (nw + gap) for i in range(n)]

    # ---------------- 第 1 代
    y0 = d.y + 4
    side_y = y0 + 54
    side_h = 62
    main_y = side_y + side_h + 64
    main = [
        ("① 由模板快照加载起来", "原生加载机制；第 1 代进程", "coral"),
        ("② checkpoint「干净环境」", "这一代的第一次，全量根", "teal"),
        ("③ checkpoint「依赖装好」", "装好依赖之后；增量", "teal"),
        ("⑥ checkpoint「构建通过」", "换个改法 → 跑构建 → 成功；从 ③ 分出的新分支", "teal"),
        ("⑧ 原生 pause", "任务告一段落，固化当前状态，产物进存储；第 1 代结束", "coral"),
    ]
    main_h = max(d._need(t, s, nw) for t, s, _ in main)
    callout_lines = wrap("⑧ 导出什么：内存是第 1 代启动以来写过的全部页（包括 ⑤、⑦ 两次 restore 写回的页），"
                         "磁盘是第 1 代的全部写层压平后的结果；两者都相对模板表达，所以 ⑨ 恢复出来的正是 ⑧ 那一刻",
                         pw - 48 - 32, 14.5)
    co_y = main_y + main_h + 22
    co_h = 18 + 22 * len(callout_lines)
    y1 = co_y + co_h + 24
    panel(d, px, y0, pw, y1 - y0, "第 1 代 Firecracker 进程", "restore 是原地回写，沙箱不中断")

    for i, (t, s, r) in enumerate(main):
        d.block(xs[i], main_y, nw, main_h, t, s, r, "fill")
        if i < n - 1:
            d.arrow(xs[i] + nw + 4, main_y + main_h / 2, xs[i + 1] - 6, main_y + main_h / 2, "flow")

    sides = {2: ("④ 改配置 → 跑构建 → 失败", "⑤ restore 到「依赖装好」"),
             3: ("⑦ 跑测试 → 失败", "⑦ restore 到「构建通过」")}
    for i, (t, lab) in sides.items():
        d.block(xs[i], side_y, nw, side_h, t, None, "gray", "dashed")
        cx = xs[i] + nw / 2
        d.arrow(cx - 50, main_y - 2, cx - 50, side_y + side_h + 5, "flow")
        d.arrow(cx - 10, side_y + side_h + 2, cx - 10, main_y - 5, "back", lab)

    fill, stroke, tc, _ = RAMPS["gray"]
    d.rect(xs[0], co_y, pw - 48, co_h, fill, stroke, sw=0.8, rx=8)
    for k, s in enumerate(callout_lines):
        d.text(px + pw / 2, co_y + 26 + 22 * k, s, 14.5, tc, 600, "middle")

    # ---------------- 换代
    sep_y = y1 + 22
    sep_h = 48
    _, cs, ctc, _ = RAMPS["coral"]
    d.rect(px, sep_y, pw, sep_h, "#FFFFFF", cs, sw=1.4, rx=10)
    d.text(px + pw / 2, sep_y + 30,
           "⑨ 几天后 resume：同一个沙箱 ID 新建 Firecracker 进程，换代 —— ②③⑥ 作废（restore 旧条目得到 not_found），⑧ 的状态还在",
           15.5, ctc, 600, "middle")

    # ---------------- 第 2 代
    g2 = sep_y + sep_h + 22
    row_y = g2 + 54
    g2nodes = [("⑨ 第 2 代进程起来", "从 ⑧ 的快照加载", "coral"),
               ("⑩ 继续工作，需要时 checkpoint", "第 2 代的第一次又是全量根", "teal")]
    note_lines = ["第 2 代的“自启动以来脏页集”从空开始，父代是 ⑧ 的快照；",
                  "第 2 代再 pause 时，导出的是相对 ⑧ 的差分"]
    h2 = max([d._need(t, s, nw) for t, s, _ in g2nodes] + [80])
    panel(d, px, g2, pw, row_y + h2 + 24 - g2, "第 2 代 Firecracker 进程")
    for i, (t, s, r) in enumerate(g2nodes):
        d.block(xs[i], row_y, nw, h2, t, s, r, "fill")
    d.arrow(xs[0] + nw + 4, row_y + h2 / 2, xs[1] - 6, row_y + h2 / 2, "flow")
    nx = xs[2]
    nwid = xs[4] + nw - nx
    d.rect(nx, row_y, nwid, h2, "none", RAMPS["gray"][1], sw=1, dash="5 4")
    for k, s in enumerate(note_lines):
        d.text(nx + nwid / 2, row_y + h2 / 2 - 6 + 22 * k, s, 15, SUB, 600, "middle")
    d.y = row_y + h2 + 24

    d.footer("注：方向是单向的：checkpoint / restore → 原生 pause 可以；原生 pause / resume 之后，旧的 checkpoint 作废。"
             "SDK 的 create_snapshot()（原生 Checkpoint RPC）同样换一代。")
    d.save(out("rollback", "15-2-一个沙箱生命周期里的交替"))


if __name__ == "__main__":
    fig1()
    fig2()
