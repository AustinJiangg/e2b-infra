"""进程级手册第 17 章代价模型：17-1 成本模型的形状（新增，§1～§3，只画结构，不放数字）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w, wrap

PURPLE = RAMPS["purple"]


def callouts(d, items):
    """在上一行流程下方并排放几条说明条。items: [(文字, 色阶, (i, j))]。"""
    xs, bw = d._flow
    y = d.y + 16
    hs = []
    for text, ramp, (i, j) in items:
        x0, x1 = xs[i], xs[j] + bw
        hs.append((x0, x1, wrap(text, x1 - x0 - 32, 14.5), ramp))
    h = 18 + 22 * max(len(l) for _, _, l, _ in hs)
    for x0, x1, lines, ramp in hs:
        f, s, tc, _ = RAMPS[ramp]
        d.rect(x0, y, x1 - x0, h, f, s, sw=0.8, rx=8)
        top = y + h / 2 - 11 * (len(lines) - 1) + 5
        for k, t in enumerate(lines):
            d.text((x0 + x1) / 2, top + 22 * k, t, 14.5, tc, 600, "middle")
    d.y = y + h


def tree_rows(d, x, w, title, sub, unit, stopped=False):
    """一个小面板：1、2、3 棵树时串行的各段，每棵树一段，等宽、不按比例。"""
    y0 = d.y + 18
    d.text(x, y0 + 16, title, 16, INK, 600)
    d.text(x + text_w(title, 16) + 14, y0 + 16, sub, 13.5, MUTED)
    lab_w, seg, gap = 92, 128, 4
    rows_y = []
    y = y0 + 40
    for n in (1, 2, 3):
        d.text(x, y + 26, f"{n} 棵树", 14.5, INK, 600)
        for k in range(n):
            sx = x + lab_w + k * (seg + gap)
            d.rect(sx, y, seg, 40, PURPLE[0], PURPLE[1], sw=1.2, rx=6)
            d.text(sx + seg / 2, y + 25, f"树 {k + 1} {unit}", 13.5, PURPLE[2], 600, "middle")
        ex = x + lab_w + n * (seg + gap) + 12
        d.text(ex, y + 26, f"≈ {n} × 每棵树一段", 13.5, SUB)
        rows_y.append(y)
        y += 56
    if stopped:
        # 三棵树那一行：从第一棵 dump 完到最后一棵 dump 完，第一棵一直停着
        ry = rows_y[-1] + 40
        bx0 = x + lab_w + seg + gap / 2
        bx1 = x + lab_w + 3 * seg + 2 * gap
        by = ry + 14
        col = PURPLE[1]
        d.o.append(f'<path d="M{bx0:.1f} {by - 8:.1f} L{bx0:.1f} {by:.1f} L{bx1:.1f} {by:.1f} '
                   f'L{bx1:.1f} {by - 8:.1f}" fill="none" stroke="{col}" stroke-width="1.6"/>')
        lines = ["树 1 dump 完后一直停到最后一棵 dump 完：",
                 "stopped_ms ≈ 其余各棵的 dump 时间之和（图 13-3）"]
        for k, t in enumerate(lines):
            d.text(bx0, by + 24 + 20 * k, t, 13.5, PURPLE[3], 600)
        y = by + 24 + 20 * len(lines)
    return y


def d17_1():
    d = Diagram()
    d.header("代价的形状：restore 随进程内存总量增长，checkpoint 随本次写出的页数增长，多棵树线性叠加",
             "把一次 restore、一次 checkpoint 拆成 gsd 自报的各个计时项，标出每一项随什么增长。"
             "只画结构，框的宽度不代表耗时；各项的量级见本章各节，判定值见第 20 章。")

    d.section(1, "单树 restore：两大项都随进程内存增长", highlight="本图重点", note="按执行顺序")
    d.flow([
        Step("precheck_ms", "检查镜像、建目录；随树数与增量链深度", "gsd", tag=("小项", "gray")),
        Step("find_pids_ms", "扫描用户进程；随 guest 里的用户进程数", "gsd", tag=("小项", "gray")),
        Step("kill_ms", "杀掉全部用户进程，等内核归还它们的页；随被杀进程的内存总量", "gsd",
             tag=("主要项", "purple")),
        Step("overlay_restore_ms", "切回层栈，一次 ioctl；基本恒定", "gsd → guest 内核", tag=("小项", "gray")),
        Step("criu_restore_ms", "逐棵重建、读回页面并缺页映射；随整条增量链合起来的页数", "CRIU",
             tag=("主要项", "purple")),
    ], min_h=150, gap=40)
    d.callout("restore 总是杀掉全部进程、恢复全部内存：耗时取决于目标 checkpoint 时进程有多大，"
              "与上次 checkpoint 之后改了多少几乎无关", "purple")

    d.section(2, "单树 checkpoint：随本次要写出的页数增长", note="按执行顺序")
    d.flow([
        Step("criu dump：固定部分", "冻结进程、遍历页表、写元数据；不改内存时只剩这一部分", "CRIU"),
        Step("criu dump：写出页面", "随本次要写出的页数", "CRIU",
             tag=("全量：全部匿名内存；增量：上次以来的脏页", "purple")),
        Step("沉下 upper", "一次改名加一次 ioctl；可写打开的文件按大小拷贝（§4）", "gsd → guest 内核"),
        Step("清 soft-dirty，放开进程", "重写 clear_refs，再 SIGCONT", "gsd"),
    ], min_h=150)
    callouts(d, [("criu_dump_ms：dump 期间进程已被冻结", "purple", (0, 1)),
                 ("stopped_ms：从 dump 结束算起", "purple", (2, 3))])

    d.section(3, "多棵树：每棵树一次 dump、一次 restore，串行", note="这条线性关系的依据只到三棵树")
    half = (d.W - 2 * d.M - 48) / 2
    top = d.y
    y1 = tree_rows(d, d.M, half, "checkpoint", "逐棵 criu dump", "dump", stopped=True)
    d.y = top
    y2 = tree_rows(d, d.M + half + 48, half, "restore", "逐棵 criu restore", "restore")
    d.y = max(y1, y2)

    d.footer("注：单树 restore 的 total_ms 约等于 criu_restore_ms 加 kill_ms，其余三项合起来很小；"
             "单树 checkpoint 的 total_ms 约等于 criu_dump_ms 加很小的其余部分。多树时 precheck_ms 还要查树间共享的管道和 socket，"
             "随树数略增。THP 为 never 使 criu_restore_ms 里多出一部分缺页（§6）。")
    d.save(out("process", "17-1-代价的组成与增长"))


if __name__ == "__main__":
    d17_1()
