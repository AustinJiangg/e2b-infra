"""第 12 章配图：12-1 两把锁的层级与持锁范围；12-2 checkpoint 依赖的四层，各自什么时候消失。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, wrap, text_w


# ---------------------------------------------------------------- 12-1
def fig1():
    d = Diagram(width=1600)
    M = d.M
    d.header("两把锁只有一个方向：opLocks 管整个操作，Store.mu 只管一次 map 读写",
             "按沙箱的 opLocks 让同一沙箱的 checkpoint / restore / delete 全程串行；全局的 Store.mu 保护账本，"
             "持有时只读写 map，文件 I/O 和 Firecracker 调用都放在锁外。")

    # ------------------------------------------------------------ 第一段
    d.section(1, "两把锁各管什么", note="packages/orchestrator/internal/checkpoint/store.go")
    c = d.compare([("opLocks[sandboxID]", "每个沙箱一把，长时间持有", "teal"),
                   ("Store.mu", "全局一把，短时间持有", "gray")])
    c.row("是什么", [
        ("容量为 1 的 channel", "channel 里有一个值就是持锁；沙箱移除后不删，同一个 id 经 pause / resume 回来仍用它"),
        ("普通互斥锁", "保护账本：bySandbox、bases、rootfs、children、layerRefs、owners 等表")])
    c.row("持多久", [
        ("整个 checkpoint / restore / delete", "保护每沙箱的内存基准和磁盘层栈"),
        ("一次 map 读写", "持锁时不做文件 I/O、不调 Firecracker")], links=[None, None])
    c.row("等不到时", [
        ("等满 CHECKPOINT_LOCK_WAIT_TIMEOUT 回 busy", "LockSandboxWithin；不限时的 LockSandbox 只留给不面向客户端的内部路径"),
        ("排队时间记在 materialize_lock_wait", "锁内做的慢事会出现在别的沙箱 restore 的这一项里")], links=[None, None])
    c.row("不这样会怎样", [
        Box("同一沙箱两个操作交错", "第二个 checkpoint 读到旧基准：两代产物声称同一个父亲，纪元位图覆盖的时间区间重叠", "fill", ramp="coral"),
        Box("一个沙箱的 I/O 阻塞全机", "写成 defer Unlock，物化的 I/O 会持着全局锁，阻塞这台机器上所有沙箱的账本操作", "fill", ramp="coral")],
        links=[None, None])
    c.end()
    d.callout("获取顺序只有 opLocks → Store.mu 一个方向，所以不会死锁。"
              "gate() 短暂持 Store.mu 取出沙箱锁对象，返回前就放掉，然后才去等沙箱锁", "teal")

    # ------------------------------------------------------------ 第二段：时间轴
    d.section(2, "一次 restore 里两把锁各持多久", highlight="本图重点",
              note="示意：沙箱 A 做 restore，同时沙箱 B 做 checkpoint；横轴是时间，不按比例")
    LW = 210
    x0, x1 = M + LW, d.W - M
    W = x1 - x0

    def X(t):
        return x0 + W * t

    RH, RG = 50, 22
    y = d.y + 12
    rows = ["opLocks[A]", "opLocks[B]", "Store.mu", "A 在锁外做的事"]
    subs = ["沙箱 A 的 restore", "沙箱 B 的 checkpoint", "全局，一次只有一个持有者", "文件 I/O、调 Firecracker"]
    ys = {}
    for i, (r, s) in enumerate(zip(rows, subs)):
        yy = y + i * (RH + RG)
        ys[r] = yy
        d.text(M, yy + RH / 2 - 3, r, 15, INK, 600)
        d.text(M, yy + RH / 2 + 16, s, 12.5, MUTED)
        d.o.append(f'<line x1="{x0:.1f}" y1="{yy + RH / 2:.1f}" x2="{x1:.1f}" y2="{yy + RH / 2:.1f}" '
                   f'stroke="#E5E3DB" stroke-width="1"/>')
    teal = RAMPS["teal"]
    gray = RAMPS["gray"]
    coral = RAMPS["coral"]

    def bar(r, t0, t1, label, ramp, style="fill", fs=13.5, below=None):
        f, s, tc, sc = RAMPS[ramp]
        yy = ys[r]
        xa, xb = X(t0), X(t1)
        if style == "fill":
            d.rect(xa, yy + 6, xb - xa, RH - 12, f, s, sw=1.2, rx=6)
        elif style == "solid":
            d.rect(xa, yy + 6, xb - xa, RH - 12, s, s, sw=1.2, rx=4)
        elif style == "outline":
            d.rect(xa, yy + 6, xb - xa, RH - 12, "#FFFFFF", s, sw=1.6, rx=4)
        else:
            d.rect(xa, yy + 6, xb - xa, RH - 12, "#FFFFFF", s, sw=1.2, rx=6, dash="5 4")
        if label:
            col = "#FFFFFF" if style == "solid" else tc
            d.text((xa + xb) / 2, yy + RH / 2 + 5, label, fs, col, 600, "middle")
        if below:
            d.text((xa + xb) / 2, yy + RH + 14, below, 12.5, sc if style != "solid" else s, 600, "middle")

    # 沙箱 A 的 opLocks
    bar("opLocks[A]", 0.035, 0.085, None, "teal", "dashed")
    bar("opLocks[A]", 0.085, 0.985, "持有：整个 restore，从取锁到返回", "teal")
    # 沙箱 B
    bar("opLocks[B]", 0.0, 0.33, "持有：B 的整个 checkpoint", "teal")
    # Store.mu
    bar("Store.mu", 0.0, 0.035, None, "teal", "solid", below="gate()")
    bar("Store.mu", 0.29, 0.33, None, "teal", "outline", below="B：Commit")
    bar("Store.mu", 0.33, 0.37, None, "coral", "dashed")
    bar("Store.mu", 0.37, 0.43, None, "teal", "solid", below="A：算路径")
    bar("Store.mu", 0.80, 0.85, None, "teal", "solid", below="A：挪基准")
    # 等待标注
    d.text(X(0.35), ys["Store.mu"] - 6, "A 等锁：materialize_lock_wait", 12.5, coral[3], 600, "middle")
    d.text(X(0.06), ys["opLocks[A]"] - 6, "等沙箱锁", 12.5, teal[3], 600, "middle")
    # A 在锁外
    bar("A 在锁外做的事", 0.10, 0.28, "暂停、导出活跃脏页", "gray", fs=13)
    bar("A 在锁外做的事", 0.44, 0.63, "读位图、写物化文件", "gray", fs=13)
    bar("A 在锁外做的事", 0.64, 0.79, "rollback、恢复虚机", "gray", fs=13)
    bar("A 在锁外做的事", 0.86, 0.89, None, "gray", fs=13)
    bar("A 在锁外做的事", 0.90, 0.98, "等 envd", "gray", fs=13)
    d.text(X(0.875), ys["A 在锁外做的事"] + RH + 14, "删 reclaim", 12.5, gray[3], 600, "middle")
    d.y = y + 4 * (RH + RG) - RG + 22

    d.callout("同一沙箱的操作被 opLocks 串行，不同沙箱各持各的 opLocks、完全并行，只在 Store.mu 的这几小段上排队；"
              "实心短条是锁内只改 map 的工作，灰条全部在锁外", "gray")
    d.footer("注：顺序按第 4 章 §8：ResetView、恢复虚机之后才在 finishRestore 里挪基准（SetBaseToEntry、SetRootfsToEntry），再等 envd。「算路径」是 MaterializeRevert 锁内取目标、查断链、算祖先链与回滚路径，之后手工 Unlock；"
             "Commit 锁内只插入条目、移动基准、登记计数（commit_index）；删除类操作锁内只摘账，文件收进 reclaim 列表放锁后删除。")
    d.save(out("rollback", "12-1-两把锁的层级与持锁范围"))


# ---------------------------------------------------------------- 12-2
def fig2():
    d = Diagram(width=1600)
    d.header("一个 checkpoint 依赖四层东西：三层随进程消失，磁盘那一半离开模板读不全",
             "账本、活着的 Firecracker 进程和它的宿主资源都活不过这一代沙箱或 orchestrator 进程；"
             "磁盘产物只是相对模板的增量。把目录完整拷走，也恢复不出来。",
             legend=[("warn", "触发后这一层消失")])

    d.section(1, "产物消失的三条代码路径", note="§11；三者互不相干，任一发生即触发")
    trig = [
        ("沙箱从沙箱表中移除", "OnRemove 取操作锁后 RemoveSandbox：锁内改名为 .trash-<uuid>、摘掉全部状态，放锁后删除"),
        ("同一个 id 的新一代接管", "原生 resume 起新的 Firecracker 进程（新 LifecycleID）；admitLocked / AdoptSandbox 丢弃上一代的全部状态"),
        ("orchestrator 启动", "NewStore 先 os.RemoveAll 整个 store 根目录，再 MkdirAll 重建；不读任何 manifest"),
    ]
    gap = 28
    bw = (d.W - 2 * d.M - 2 * gap) / 3
    h = max(d._need(t, s_, bw, actor=True) for t, s_ in trig)
    for i, (t, s_) in enumerate(trig):
        d.block(d.M + i * (bw + gap), d.y, bw, h, t, s_, "gray", "outline", actor="orchestrator")
    d.y += h

    d.section(2, "四层依赖，各自什么时候没了", highlight="本图重点", note="§13")
    c = d.compare([("① 活体恢复机制", "只有原地回滚一条路", "teal"),
                   ("② 模板底座", "磁盘产物不自足", "teal"),
                   ("③ 进程内账本", "不从磁盘加载", "teal"),
                   ("④ 运行时资源", "一个都不重建", "teal")], label_width=132)
    c.row("是什么", [
        ("活着的 Firecracker 进程", "RollbackInPlace 写既有的 KVM fd、GIC、virtio 对象"),
        ("模板 rootfs", "AssembleView 永远以它为底，再叠封存层"),
        ("Store 的各张表", "条目、基准、子节点计数、层引用、代际归属"),
        ("宿主上的活体资源", "KVM fd、eventfd、irqfd / ioeventfd、tap、网络槽位、NBD 设备")])
    c.row("为什么离不开", [
        ("回滚集含当前状态", "回滚集 = 纪元位图并集 ∪ 当前活跃脏页；端点要求 Paused、拓扑与运行中虚机一致"),
        ("封存层只含写过的块", "没写过的块要回模板取；header 的底座项也指向模板的 build"),
        ("API 只认账本", "账本里没有，List 返回空、Get 找不到条目"),
        ("原地回滚不重建它们", "这是它快的原因，也是它离不开沙箱的原因")], sub="restore 时", links=[None] * 4)
    c.row("什么时候没了", [
        Box("这一代 Firecracker 进程结束", "沙箱被移除；原生 pause 停掉进程，resume 起的是新一代", "fill", ramp="coral"),
        Box("脱离模板就读不全", "原地回滚时沙箱活着，模板必然在位；拷走目录换个沙箱，模板必须在位且是同一个", "outline", ramp="gray"),
        Box("orchestrator 进程退出", "启动即清空；这个沙箱的那部分在移除、新一代接管时先丢", "fill", ramp="coral"),
        Box("这一代 Firecracker 进程结束", "与 ① 同时；orchestrator 重启也会带走它上面的所有沙箱", "fill", ramp="coral")],
        links=["warn", None, "warn", "warn"])
    c.end()
    d.callout("内存那一半已经自足：CHECKPOINT_FULL_ROOT 默认开启，树根是一次全量捕获，内容解析遇全量条目即终止、"
              "不回落模板 memfile。真正的数据缺口只有磁盘那一半", "teal")
    d.footer("注：把四层补齐（磁盘层栈压平、账本从 manifest 重建、产物 fsync、新起进程从文件加载）之后得到的，"
             "就是 e2b 原生 snapshot 的定义，同时会逐项丢掉零拷贝封存、原地回滚的收益（§15）。")
    d.save(out("rollback", "12-2-checkpoint依赖的四层"))


if __name__ == "__main__":
    fig1()
    fig2()
