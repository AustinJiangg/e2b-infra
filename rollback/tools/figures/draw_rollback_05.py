"""第 05 章 脏页跟踪与 HDBSS 的配图。

05-1 两级地址翻译（替换 §2 开头的 Stage-1 / Stage-2 文本示意），画法参考
     hdbss-dirty-tracking/tools/figures/draw_docs.py 的 d03_1。
05-2 写保护与 HDBSS 怎么标脏、HDBSS 从启用到收集（替换 §2.3 的 HDBSS 文本示意），画法参考
     draw_dirty.py 的 d5（06-1 脏页日志的两层）与 draw_docs.py 的 d08_1（08-1 openEuler 实现）。
05-3 读也算脏（§5 判据差异，新增）：同一组页在三种判据下哪些算脏，画法参考 HDBSS 仓库的 31-1 与 05-3。
内容只取本章正文与被替换的代码块；KVM 写保护用 green，HDBSS 用 teal，uffd 写保护（e2b x86 原生路径）用 coral。
05-3 里 x86 原生作参照用灰色，ARM 适配基线用 coral，本方案用 teal。
"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, TONES  # noqa: E402


def f05_1():
    d = Diagram()
    d.header("两级地址翻译：脏页跟踪记的是中间那一层 guest 物理页",
             "guest 写内存时不经过任何宿主代码，宿主软件看不见这次写；能看见的只有管 Stage-2 页表的 KVM，或者 CPU 自己。",
             legend=[("flow", "地址翻译")])
    d.section(1, "guest 的一次访存要查两张页表")
    d.flow([
        Step("Guest 虚拟地址", "guest 里的程序使用", "guest"),
        Step("Stage-1 页表", "guest 内核自己管的页表", "guest 内核"),
        Step("Guest 物理地址 / IPA", "Stage-1 的输出，Stage-2 的输入", "guest",
             tag=("要跟踪的是这一层：哪些 guest 物理页被写过", "teal")),
        Step("Stage-2 页表", "宿主 KVM 管的页表", "KVM"),
        Step("宿主物理地址", "真正的物理内存", "宿主"),
    ], min_h=150, gap=40)
    d.section(2, "于是有三条路知道一页被写过", highlight="本图重点")
    c = d.compare([("KVM 写保护 + 脏页日志", "KVM 的标准能力", "green"),
                   ("userfaultfd 写保护", "e2b 在 x86 上的原生路径", "coral"),
                   ("HDBSS", "ARMv9.5 硬件标脏，鲲鹏 950 实现", "teal")])
    c.row("谁发现写", [("KVM", "Stage-2 页设成只读，第一次写触发权限故障"),
                      ("用户态 handler", "写一个因读而填的页，触发写保护故障"),
                      ("CPU", "直接把脏 GPA 写进该 vCPU 的 HDBSS buffer")],
          links=[None, None, None])
    c.row("判据来源", [("Stage-2 权限故障", "VMM 用 KVM_GET_DIRTY_LOG 取走位图"),
                      ("pagemap bit 57", "present = 1 且 uffd-wp = 0 即为脏"),
                      ("CPU 写 buffer + VM-Exit 时汇总", "最终仍走 KVM_GET_DIRTY_LOG")],
          links=[None, None, None])
    c.summary("对比小结", [
        ("每页首次写的代价", [("一次 VM-Exit", "bad"), ("一次用户态故障往返", "bad"), ("≈ 0", "good")]),
        ("精度", ["精确：只有写才触发", "精确", "精确"]),
        ("可用性", ["任何 KVM", "内存由 uffd 托管，内核支持 uffd 写保护", "ARMv9.5 + 内核支持 + VHE"]),
        ("本方案", ["用：没有 HDBSS 时退回它", ("不用", "muted"), "用：有 HDBSS 时用它"]),
    ])
    c.end()
    d.footer("注：本方案的两种方式喂给上层的是同一个位图，下文说「KVM 脏页日志」时两者都包括。")
    d.save(out("rollback", "05-1-两级地址翻译"))


def f05_2():
    d = Diagram()
    d.header("写保护与 HDBSS 怎么标脏：HDBSS 去掉了为标脏而生的 VM-Exit",
             "两种方式最后都汇总进标准 KVM memory-slot dirty bitmap，由 VMM 调用 KVM_GET_DIRTY_LOG 取走。"
             "语义一致，只差性能，所以降级是静默的。",
             legend=[("flow", "控制流"), ("warn", "VM-Exit，虚机退出到宿主内核"), ("async", "稍后、批量汇总")])
    d.section(1, "guest 第一次写一个干净页", highlight="本图重点")
    c = d.compare([("KVM 写保护", "没有 HDBSS 时的退路", "green"),
                   ("HDBSS", "工作在 Stage-2 这一层", "teal")])
    c.states("干净页的状态", [("Stage-2 只读", "KVM 把 Stage-2 页表里所有页设成只读"),
                         ("Stage-2 PTE 启用 DBM", "Dirty Bit Modifier，没有写保护")],
             sub="（取图之后、下一次写之前）")
    c.span("触发事件", "guest 写入某个 GPA")
    c.row("发现写", [("权限故障 → VM-Exit", "每个干净页的第一次写付一次"),
                    ("CPU 把脏 GPA 写进 HDBSS buffer", "硬件完成，无陷出；每个 vCPU 一个 buffer")],
          links=[("warn", "陷出"), "flow"])
    c.row("记进位图", [("KVM 在脏页位图里置位", "把该页改回可写，返回 guest"),
                      Box("vCPU 发生 VM-Exit（任何原因）时", "KVM 遍历 HDBSS buffer，逐条 kvm_vcpu_mark_page_dirty()",
                          style="dashed")],
          links=["flow", "async"])
    c.row("结果", [("标准 KVM memory-slot dirty bitmap", "VMM 调用 KVM_GET_DIRTY_LOG 取走"),
                  ("标准 KVM memory-slot dirty bitmap", "VMM 调用 KVM_GET_DIRTY_LOG 取走")])
    c.summary("对比小结", [
        ("每个干净页首次写", [("一次 VM-Exit", "bad"), ("没有为标脏而生的 VM-Exit", "good")]),
        ("上层拿到的", ["同一个位图", "同一个位图"]),
        ("额外开销", ["之后该页一直可写，直到下一次取位图", ("buffer 有容量上限，写满触发额外处理", "bad")]),
    ])
    c.end()

    d.section(2, "HDBSS 怎么被启用", note="以已武装跟踪为前提；冷启动和从快照加载两条路径都要做")
    d.flow([
        Step("探测能力", "KVM_CHECK_EXTENSION(502)，在 /dev/kvm 上只查询，不建 VM", "orchestrator"),
        Step("建 vCPU、注册 memslot", "KVM_CREATE_VCPU × N 之后，注册带 KVM_MEM_LOG_DIRTY_PAGES 的 memslot",
             "Firecracker"),
        Step("KVM_ENABLE_CAP(502)", "args[0] 是大小编码，默认 1 = 每 vCPU 8 KiB",
             "Firecracker", tag=("必须在 vCPU 创建之后调用", "coral")),
        Step("自报所用方式", "instance info 的 dirty_tracking：hdbss / kvm-wp / off", "Firecracker"),
    ], min_h=150, gap=40)
    d.section(3, "运行与收集")
    d.flow([
        Step("guest 运行", "CPU 把脏 GPA 写进 buffer，无陷出", "CPU"),
        Step("打快照、回滚前先暂停", "暂停让每个 vCPU 退出 KVM_RUN", "Firecracker"),
        Step("buffer 汇总进位图", "VM-Exit 时刷新；dirty-log 同步时也处理", "KVM"),
        Step("取图", "KVM_GET_DIRTY_LOG 取走并清空位图", "Firecracker"),
    ], min_h=130, gap=40)
    d.callout("纯计算 guest 几乎不产生 VM-Exit，但「暂停之后取图」那一刻 buffer 里的记录也已经汇总进位图", "teal")
    d.footer("注：KVM_ENABLE_CAP 失败时，若要求必须 HDBSS（FC_HDBSS_REQUIRED）则构建失败，否则退回 kvm-wp。"
             "顺序错了 ioctl 照样返回成功，只是一条脏页都记不到。")
    d.save(out("rollback", "05-2-写保护与HDBSS怎么标脏"))


# 05-3：页的经历。0 没碰过 / 1 因读缺页而填 / 2 因写缺页而填 / 3 因读而填、之后又被写
PAGES = [1, 0, 2, 1, 3, 0, 1, 2, 1, 3, 0, 1]
PAGE_LOOK = {0: ("没碰过", "未换入"), 1: ("只读过", "读缺页填入"), 2: ("写过", "写缺页填入"), 3: ("读后又写", "读缺页填入")}


def f05_3():
    d = Diagram()
    d.header("读也算脏：同一组页，ARM 适配基线把每一个换入过的页都记成脏",
             "x86 原生和本方案只把写过的页算脏；ARM 适配基线的判据塌缩成“常驻”，只读过的页也进了增量。"
             "这是修复 ARM 适配引入的退化，不是超越 x86 原生。",
             legend=[("flow", "判据的推导")])
    M, W = d.M, d.W
    N, PW, PG = len(PAGES), 88, 10
    X0 = W - M - 24 - N * PW - (N - 1) * PG
    LX = M + 22

    d.section(1, "同一段运行里的一组 guest 页，三种判据各把哪些页算脏", highlight="本图重点")
    T = d.y + 12
    rows = [
        ("e2b x86 原生", "gray", "present = 1 且 uffd-wp = 0",
         lambda v: ("wp=0", True) if v in (2, 3) else (("wp=1", False) if v == 1 else ("—", False)),
         ("精确：只读过的页带着写保护位，不算脏", "good")),
        ("ARM 适配基线", "coral", "同一判据，但填页从不带 WP",
         lambda v: ("wp=0", True) if v else ("—", False),
         ("退化：凡被换入过的页都算脏", "bad")),
        ("本方案", "teal", "KVM / HDBSS 脏页日志（Stage-2）",
         lambda v: ("1", True) if v in (2, 3) else ("0", False),
         ("精确：读一页不会让它在 Stage-2 上变脏", "good")),
    ]
    H = 120 + len(rows) * 112 + 34
    d.rect(M, T, W - 2 * M, H, "#FBFBF9", "#D3D1C7", sw=1, rx=14)
    # 页的经历
    py = T + 26
    d.text(LX, py + 22, "guest 页的经历", 15.5, INK, 600)
    d.text(LX, py + 44, "（示意，12 页）", 13, MUTED)
    GF, GS, GT, GSUB = RAMPS["gray"]
    BF, BS, BT, BSUB = RAMPS["blue"]
    for i, v in enumerate(PAGES):
        x = X0 + i * (PW + PG)
        t, sub = PAGE_LOOK[v]
        if v == 0:
            d.rect(x, py, PW, 62, "#FFFFFF", GS, sw=1, rx=6, dash="4 4")
            d.text(x + PW / 2, py + 26, t, 14, MUTED, 600, "middle")
            d.text(x + PW / 2, py + 47, sub, 12, MUTED, 400, "middle")
        else:
            strong = v in (2, 3)
            d.rect(x, py, PW, 62, MUTED if strong else GF, MUTED if strong else GS, sw=1.2, rx=6)
            d.text(x + PW / 2, py + 26, t, 14, "#FFFFFF" if strong else INK, 600, "middle")
            d.text(x + PW / 2, py + 47, sub, 12, "#FFFFFF" if strong else SUB, 400, "middle")
    d.o.append(f'<line x1="{LX}" y1="{py + 88}" x2="{W - M - 22}" y2="{py + 88}" stroke="#E5E3DB" stroke-width="1"/>')
    for k, (name, ramp, crit, fn, (res, tone)) in enumerate(rows):
        ry = py + 110 + k * 112
        f, s, tc, sc = RAMPS[ramp]
        if ramp == "gray":
            s = MUTED
        d.text(LX, ry + 20, name, 16, tc, 600)
        d.text(LX, ry + 42, crit, 13, SUB)
        for i, v in enumerate(PAGES):
            x = X0 + i * (PW + PG)
            lab, dirty = fn(v)
            if dirty:
                d.rect(x, ry, PW, 44, s, s, sw=1, rx=6)
                d.text(x + PW / 2, ry + 28, lab, 14, "#FFFFFF", 600, "middle")
            else:
                d.rect(x, ry, PW, 44, "#FFFFFF", "#D3D1C7", sw=1, rx=6, dash="3 3")
                d.text(x + PW / 2, ry + 28, lab, 13.5, MUTED, 400, "middle")
        col, wgt = TONES[tone]
        d.text(X0, ry + 72, res, 14, col, wgt)
    d.text(LX, T + H - 16, "实心格 = 算脏，进增量；虚线格 = 不算脏。wp 是 /proc/self/pagemap 的 bit 57（uffd 写保护）；本方案一行是 KVM 位图里的位。",
           12.5, MUTED)
    d.y = T + H

    d.section(2, "ARM 适配基线上判据怎么塌缩", note="§5.1、§5.2")
    d.flow([
        Step("UFFDIO_COPY 不带 WP", "写保护模式那几行被注释掉，每次填页都清掉 WP 位，无论读写", "uffd handler"),
        Step("bit 57 恒为 0", "!is_write_protected() 恒真", "pagemap"),
        Step("判据塌缩成 is_present()", "也就是 mincore 已经回答过的问题", "Firecracker",
             tag=("第二级逐页 pread 成了纯开销", "coral")),
        Step("位图 = 常驻页集合", "“被读进来过”和“被写过”不再有区别", "GET /memory/dirty"),
    ], min_h=130, gap=40)
    d.callout("再叠加原生 Checkpoint RPC 会重建沙箱、工作集要重新缺页换入：两次 snapshot 之间什么都没做，增量下限也约等于整个常驻工作集",
              "coral")
    d.footer("注：本方案不走 uffd 判据，也不需要沙箱内存由 uffd 托管。原生 pause 路径上的这个问题在本书交付的代码里已经修复，"
             "改法见第 14 章。")
    d.save(out("rollback", "05-3-读也算脏"))


if __name__ == "__main__":
    f05_1()
    f05_2()
    f05_3()
