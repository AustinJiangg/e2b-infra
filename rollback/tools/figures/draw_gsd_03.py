"""进程级 03：soft-dirty 的状态变化（替换 03 §3.1 的文本示意）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w

d = Diagram()
d.header("soft-dirty：每一页从干净变脏，恰好付一次缺页",
         "写 clear_refs 把页清零并设成只读；进程第一次写这一页时触发缺页，内核恢复写权限并置脏，之后同一页再写不经过内核。"
         "CRIU 的 --track-mem 和 --prev-images-dir 分别用到这一机制的两端。",
         legend=[("flow", "状态变化、控制流"), ("warn", "缺页异常：唯一的一次代价")])

# ---------------------------------------------------------------- 1. 一页的三种状态
d.section(1, "一页的页表项怎样变化", highlight="本图重点")
M, W = d.M, d.W
bw, gap = 368, (W - 2 * M - 3 * 368) / 2
xs = [M + i * (bw + gap) for i in range(3)]
y, h = d.y + 6, 104
d.block(xs[0], y, bw, h, "可写，soft-dirty = ?", "写 clear_refs 之前", ramp="gray", style="outline")
d.block(xs[1], y, bw, h, "只读，soft-dirty = 0", "即使这一页本来是可写的，写权限也被去掉", ramp="purple", style="outline")
d.block(xs[2], y, bw, h, "可写，soft-dirty = 1", "写权限已恢复，这一页记为“写过”", ramp="purple", style="fill")
cy = y + h / 2
d.arrow(xs[0] + bw + 4, cy, xs[1] - 6, cy, "flow")
d.text((xs[0] + bw + xs[1]) / 2, cy - 12, "写 clear_refs = 4", 13, SUB, 600, "middle")
d.arrow(xs[1] + bw + 4, cy, xs[2] - 6, cy, "warn")
d.text((xs[1] + bw + xs[2]) / 2, cy - 12, "进程第一次写这一页", 13, "#D85A30", 600, "middle")
d.text((xs[1] + bw + xs[2]) / 2, cy + 24, "缺页", 13, "#D85A30", 600, "middle")

ny = y + h + 56
nh = 70
for i, (a, b) in ((1, ("没有写：一直保持 0（干净）", "页一直保持只读")),
                  (2, ("再写：不经过内核", "一直保持 1（脏），之后不再有开销"))):
    d.arrow(xs[i] + bw / 2, y + h + 4, xs[i] + bw / 2, ny - 6, "flow")
    d.rect(xs[i], ny, bw, nh, "#FFFFFF", RAMPS["gray"][1], dash="6 4")
    d.text(xs[i] + bw / 2, ny + 30, a, 15, INK, 600, "middle")
    d.text(xs[i] + bw / 2, ny + 52, b, 13.5, SUB, 400, "middle")
d.y = ny + nh

# ---------------------------------------------------------------- 2. 四步
d.section(2, "这一次缺页里，谁做了什么", note="进程写内存是一条普通的机器指令，不经过内核，所以要先把页设成只读")
d.flow([
    Step("清零并上锁", "往 /proc/<pid>/clear_refs 写 4：遍历全部页表项，清 soft-dirty 位、去掉写权限",
         actor="内核（由写 clear_refs 触发）", ramp="purple"),
    Step("第一次写触发缺页", "处理器发现页表项不允许写，产生缺页异常，控制权进入内核", actor="处理器"),
    Step("内核记账并放行", "内存区域其实允许写：恢复写权限、置 soft-dirty 位，让进程重新执行那条写指令", actor="内核",
         ramp="purple"),
    Step("之后再写不再打扰", "这一页已经可写，同一页上的后续写入不再经过内核", actor="用户进程"),
], min_h=150, gap=44)

# ---------------------------------------------------------------- 3. CRIU 怎么用
d.section(3, "CRIU 的两个选项对应机制的两端")
d.flow([
    Step("上一次 dump 带 --track-mem", "CRIU 在 dump 的过程中替我们做第 1 步：清零并上锁", actor="CRIU", ramp="purple"),
    Step("用户进程照常运行", "被写过的页 soft-dirty 置 1，没写过的保持 0", actor="用户进程"),
    Step("下一次 dump 带 --prev-images-dir", "读 /proc/<pid>/pagemap：每页一个 64 位描述，第 55 位是 soft-dirty；只保存为 1 的页",
         actor="CRIU", ramp="purple"),
], min_h=130, gap=56)

d.footer("注：除了页表项上的位，内存区域（VMA）上还有一个 VM_SOFTDIRTY 标志，新映射或扩大的区域整块报成脏，见 §3.2。")
d.save(out("gsd", "03-1-soft-dirty的状态变化"))


# ==================================================================== 03-2 大页与假全量（§4～§5）
PF, PS, PT, PSUB = RAMPS["purple"]
CF, CS, CT, CSUB = RAMPS["coral"]
GF, GS = RAMPS["gray"][0], RAMPS["gray"][1]

d = Diagram()
d.header("增量偏大的两种来源：一个标记位管着整个 2 MiB 大页，或整个内存区域",
         "soft-dirty 有两级：页表项上一位、内存区域（VMA）上一个 VM_SOFTDIRTY 标志。透明大页让一个页表项覆盖 2 MiB，"
         "区域级标志让一整个区域报脏。两种情况下真正写过的都很少，增量却按整块导出。",
         legend=[("flow", "状态变化"), ("warn", "整块标脏")])
M, W = d.M, d.W

# ---------------------------------------------------------------- 1. 透明大页
d.section(1, "透明大页：同样写 4 KiB，导出 4 KiB 还是 2 MiB", highlight="本图重点", note="§4")
T = d.y + 6
PH = 372
d.rect(M, T, W - 2 * M, PH, "#FBFBF9", "#D3D1C7", sw=1, rx=14)
CELL, CG = 13, 11          # 格距与格宽
gw, gh = 32 * CELL - 2, 16 * CELL - 2
HIT = (5, 11)
lx, rx = M + 96, M + 830
gy = T + 96
d.text(lx, T + 40, "透明大页 never：512 个 4 KiB 页", 16.5, INK, 600)
d.text(lx, T + 64, "每页一个页表项，各有一个 soft-dirty 位", 13.5, SUB)
for r in range(16):
    for c in range(32):
        hit = (r, c) == HIT
        d.rect(lx + c * CELL, gy + r * CELL, CG, CG, PS if hit else "#FFFFFF", PS if hit else GS, sw=0.6, rx=1.5)
d.text(lx + gw + 24, gy + HIT[0] * CELL + 10, "← 只写了这一页", 13, PT, 600)
d.text(lx, gy + gh + 34, "增量导出：4 KiB，只有被写的那一页", 15, PT, 600)

d.text(rx, T + 40, "透明大页 always：1 个 2 MiB 大页", 16.5, INK, 600)
d.text(rx, T + 64, "只有一个页表项（pmd），只有一个 soft-dirty 位", 13.5, SUB)
d.rect(rx, gy, gw, gh, CF, CS, sw=1.8, rx=4)
d.rect(rx + HIT[1] * CELL, gy + HIT[0] * CELL, CG, CG, PS, PS, sw=0.6, rx=1.5)
d.text(rx + gw / 2, gy + gh / 2 + 30, "soft-dirty = 1：整个 2 MiB 都算写过", 15, CT, 600, "middle")
d.text(rx + gw + 24, gy + HIT[0] * CELL + 10, "← 同样只写了这 4 KiB", 13, PT, 600)
d.text(rx, gy + gh + 34, "增量导出：整个 2 MiB", 15, CT, 600)
d.y = T + PH
d.callout("实测（旧 920B，2026-09-11，192 MB 堆，每 2 MB 碰一个 4 KB 页，共 0.375 MB）：always 时增量镜像 190.5 MB，never 时 0.8 MB",
          "coral")
d.callout("所以 never 是设计约束：overlay-init 开机时写成 never，gsd 启动时 ensureTHPNever 兜底；代价是 restore 用小页重新填内存",
          "purple")

# ---------------------------------------------------------------- 2. 区域级标志
d.section(2, "假全量：新分配的内存与旧区域合并，整个区域被报成脏", note="§5.1，与透明大页无关")
T = d.y + 6
RH = 64
lab_w = 230
bx = M + lab_w
seg = 18
rows = [
    ("上一次 dump 之后", "clear_refs 已清零", "heap"),
    ("两次 dump 之间分配", "一次大的 malloc 底层是 mmap", "new"),
    ("内核合并相邻区域", "合并后的区域带着 VM_SOFTDIRTY", "merged"),
]
dirty = {3, 17}
nh, nn = 40, 8          # 堆 40 格，新区域 8 格
y = T
for k, (a, b, kind) in enumerate(rows):
    d.text(M + 4, y + 26, a, 15, INK, 600)
    d.text(M + 4, y + 48, b, 13, SUB)
    for i in range(nh + (nn if kind != "heap" else 0)):
        x = bx + i * seg + (10 if (kind == "new" and i >= nh) else 0)
        if kind == "merged":
            fill, st = (PS, PS) if i in dirty else (CF, CS)
        elif kind == "new" and i >= nh:
            fill, st = CF, CS
        else:
            fill, st = (PS, PS) if i in dirty else ("#FFFFFF", GS)
        d.rect(x, y + 14, seg - 4, 34, fill, st, sw=0.8, rx=2)
    if kind == "heap":
        d.text(bx + nh * seg + 16, y + 37, "堆：只有两页写过（紫色）", 13.5, PT, 600)
    elif kind == "new":
        ex = bx + (nh + nn) * seg + 26
        d.text(ex, y + 30, "新区域：内核打上 VM_SOFTDIRTY", 13.5, CT, 600)
        d.text(ex, y + 50, "例：1 MB 临时对象", 13, SUB)
    else:
        ex = bx + (nh + nn) * seg + 26
        d.text(ex, y + 30, "pagemap 把区域里每一页都报成脏", 13.5, CT, 600)
        d.text(ex, y + 50, "增量 = 整个区域", 13, CT, 600)
    if k < 2:
        ax = bx + nh * seg / 2
        d.arrow(ax, y + 52, ax, y + RH + 10, "warn" if k == 1 else "flow")
    y += RH + 18
d.y = y - 18
d.callout("实测（旧 920B，2026-09 上旬，透明大页 never，64 MB 堆，每次改写 12 MB）：先造 1 MB 临时对象再覆盖 → 68 MB（等于全量）；"
          "原地逐字节改写 → 13 MB；区域布局稳定后再用第一种写法 → 16 MB", "coral")

# ---------------------------------------------------------------- 3. overlay 切换
d.section(3, "gsd 自己的来源：overlay 切换把区域级标志打了回去", note="§5.2")
d.flow([
    Step("CRIU dump", "带 --track-mem，过程中清一次 soft-dirty", actor="CRIU", ramp="purple"),
    Step("overlay 层切换", "内核清理这个文件系统的目录项缓存", actor="内核"),
    Step("区域重新带上 VM_SOFTDIRTY", "不处理的话，每一次增量都变成全量", actor="内核", ramp="coral"),
    Step("gsd 重写 clear_refs", "clearSoftDirty 对刚保存过的进程逐个再写一遍；失败计入 clear_refs_failed",
         actor="gsd", ramp="purple"),
], min_h=118)
d.footer("注：两组实测都是初始实现的代码、在沙箱里直接运行 CRIU，单位照原记录写作 MB，只用来说明机制。"
         "判断一次增量是不是假全量：响应里 parent_checkpoint_id 不为空而 image_bytes 接近进程大小。")
d.save(out("gsd", "03-2-大页与假全量"))
