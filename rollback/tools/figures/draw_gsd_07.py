"""进程级 07：一次换层在内核里的过程（07 §3，新增）。只讲机制上的三段；逐步顺序与锁见 08-1。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, wrap

PF, PS, PT, PSUB = RAMPS["purple"]
GF, GS, GT, GSUB = RAMPS["gray"]

d = Diagram()
d.header("一次换层：新层栈在旁边建好，提交点一次换上，换下的东西留到下一次切换再放",
         "ovl_do_ioctl_checkpoint 分准备、提交、善后三段，图以 checkpoint 模式为例。"
         "逐步顺序和三把锁各管什么见 08-1，这里只看每一段里层栈和内存对象发生了什么。",
         legend=[("flow", "顺序")])
M, W = d.M, d.W

# ---------------------------------------------------------------- 1. 三段
d.section(1, "三段里，层栈和内存对象怎么变", highlight="本图重点", note="正在用的 overlay 直到提交点才被改动")
LW = 150
x0 = M + LW
CGAP = 40
CW = (W - M - x0 - 2 * CGAP) / 3
cx = [x0 + i * (CW + CGAP) for i in range(3)]
T = d.y + 8
heads = [("准备", "所有可能失败的事都在这里", "gray"),
         ("提交", "从这里起不允许任何事情失败", "purple"),
         ("善后", "仍在写锁内", "gray")]
for i, (h, s, r) in enumerate(heads):
    f, st, tc, sc = RAMPS[r]
    d.rect(cx[i], T, CW, 60, f, st, sw=1.6, rx=10)
    d.text(cx[i] + CW / 2, T + 27, h, 19, tc, 600, "middle")
    d.text(cx[i] + CW / 2, T + 48, s, 13.5, sc, 400, "middle")
    if i < 2:
        d.arrow(cx[i] + CW + 4, T + 30, cx[i + 1] - 6, T + 30)
# 提交点竖线标记
px = cx[1] - CGAP / 2
d.text(px, T - 10, "提交点", 13, "#A32D2D", 600, "middle")


def layer(x, y, w, name, style, ramp="gray", h=34, sub=None):
    f, st, tc, sc = RAMPS[ramp]
    if style == "fill":
        d.rect(x, y, w, h, f, st, sw=1.2, rx=6)
    elif style == "outline":
        d.rect(x, y, w, h, "#FFFFFF", st, sw=1.2, rx=6)
    else:
        d.rect(x, y, w, h, "#FFFFFF", st, sw=1.2, rx=6, dash="5 4")
    d.text(x + w / 2, y + h / 2 + 5, name, 13.5, tc if style == "fill" else (tc if ramp != "gray" else INK), 600, "middle")


# 行 A：层栈
RA = T + 60 + 26
d.text(M + 4, RA + 120, "层栈", 15, INK, 600)
d.text(M + 4, RA + 142, "最上面的层在上", 12.5, MUTED)
LH, LG = 34, 8
# 准备列：两摞
sw2 = (CW - 24) / 2
ax, bx = cx[0], cx[0] + sw2 + 24
d.text(ax + sw2 / 2, RA + 14, "正在用的：不改", 13.5, SUB, 600, "middle")
d.text(bx + sw2 / 2, RA + 14, "旁边建好的：未生效", 13.5, PSUB, 600, "middle")
cur = ["upper（可写）", "L1", "/mnt/lower"]
new = ["新 upper（空）", "旧 upper（自动 lower）", "L1", "/mnt/lower"]
for k, n in enumerate(cur):
    layer(ax, RA + 30 + k * (LH + LG), sw2, n, "outline")
for k, n in enumerate(new):
    layer(bx, RA + 30 + k * (LH + LG), sw2, n, "dashed", "purple")
# 提交列：一摞，已发布
sx = cx[1] + 40
sww = CW - 80
d.text(cx[1] + CW / 2, RA + 14, "成对发布：层数组、层数、checkpoint_gen + 1", 13.5, PSUB, 600, "middle")
for k, n in enumerate(["新 upper（可写）", "旧 upper（变成只读 lower）", "L1", "/mnt/lower"]):
    layer(sx, RA + 30 + k * (LH + LG), sww, n, "fill", "purple")
# 善后列：旧层数组退役
d.text(cx[2] + CW / 2, RA + 14, "旧层数组退役", 13.5, SUB, 600, "middle")
for k, n in enumerate(cur):
    layer(sx - cx[1] + cx[2], RA + 30 + k * (LH + LG), sww, n, "dashed")
RA_end = RA + 30 + 4 * (LH + LG) - LG
d.text(cx[2] + CW / 2, RA_end + 2, "能改指新数组的层指针都改过去；", 13, SUB, 400, "middle")
d.text(cx[2] + CW / 2, RA_end + 22, "无人指向的旧数组下一次切换释放，挂载交工作队列卸载", 13, SUB, 400, "middle")
RA_end += 30

# 行 B：内存对象
RB = RA_end + 26


def cell(i, y, h, title, sub, ramp, style="fill"):
    d.block(cx[i], y, CW, h, title, sub, ramp=ramp, style=style, ts=15.5, ss=13)


BH = 112
d.text(M + 4, RB + BH / 2 - 3, "内存里的对象", 15, INK, 600)
d.text(M + 4, RB + BH / 2 + 18, "缓存的 inode、目录项", 12.5, MUTED)
cell(0, RB, BH, "收缩目录项缓存，再建计划",
     "只剩被钉住的对象；每个受影响的 inode 一项，预先取好引用、分配好要换上去的新结构。建计划是最后一个可能失败的步骤",
     "gray")
cell(1, RB, BH, "按计划逐项改写",
     "checkpoint：promote，进程无感；restore：丢弃、标记陈旧、摘出（§4）。再换上新 work 目录、trap 和根的层栈描述",
     "purple")
cell(2, RB, BH, "换下的对象挂到延迟释放表",
     "旧的层栈描述、旧工作目录、被替换掉的目录项引用；restore 模式再收缩一次目录项缓存", "gray", "dashed")

# 行 C：规则
RC = RB + BH + 26
CH = 76
d.text(M + 4, RC + CH / 2 + 5, "这一段失败时", 15, INK, 600)
cell(0, RC, CH, "只释放本次调用申请的东西", "overlay 的状态一个字节都没变", "gray", "outline")
cell(1, RC, CH, "不会失败", "要分配内存、做磁盘操作的事都已推到提交点之前", "purple", "outline")
cell(2, RC, CH, "不会失败", "放开写锁后：释放计划的引用和上一次的延迟释放表", "gray", "outline")
# 提交点虚线
d.o.append(f'<line x1="{px:.1f}" y1="{T - 4:.1f}" x2="{px:.1f}" y2="{RC + CH + 6:.1f}" stroke="#E24B4A" '
           f'stroke-width="1.4" stroke-dasharray="6 4"/>')
d.y = RC + CH

# ---------------------------------------------------------------- 2. 延迟释放
d.section(2, "换下的对象跨两次切换才释放", note="有些使用者不持读写锁，它们离开的时间只能靠下一次拿到写锁来确认")
d.flow([
    Step("切换 N 善后", "被换下的对象挂到延迟释放表，不立刻释放", actor="内核", ramp="purple"),
    Step("两次切换之间", "免锁快速路径的路径查找、异步 I/O 的完成回调可能还看得见这些对象", actor="不持读写锁的使用者"),
    Step("切换 N+1 拿到写锁", "这时上述使用者必然都已离开", actor="内核"),
    Step("切换 N+1 放开写锁之后", "释放切换 N 留下的延迟释放表", actor="内核", ramp="purple"),
], min_h=120)
d.callout("这样既不会释放后使用，也不会泄漏；两条论证分别见 08“切换与所有 overlay 入口的互斥”和 09“回收：被换下的层、mount 与缓存引用”",
          "gray")
d.footer("注：从取到写锁到放开写锁，overlay 上所有读后端状态的操作都在等，这是切换对 guest 里所有进程造成的停顿；"
         "2026-10-08 的确认验证里 overlay_sink_ms、overlay_restore_ms 都不到 1 ms（新 920B，259f1a25c，没有可写打开的大文件）。")
d.save(out("gsd", "07-1-一次换层的三段"))
