"""进程级 15：state.json、镜像父链与层目录的依赖，以及删除时的回收（15 §3～§4，新增）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w

PF, PS, PT, PSUB = RAMPS["purple"]
GF, GS, GT, GSUB = RAMPS["gray"]

d = Diagram()
d.header("删除一个 checkpoint：被依赖的只隐藏，依赖删光后先改记录、再收目录",
         "以 15 §4.2 验证过的序列为例：先建 A，再建 B，B 的进程镜像以 A 为父。state.json 是 gsd 眼里的全部事实，"
         "它引用 tmpfs 上的镜像目录和根盘上的层目录，还必须与内核里实际挂着的层栈一致。",
         legend=[("flow", "引用")])
M, W = d.M, d.W

# ---------------------------------------------------------------- 1. 依赖
d.section(1, "三处存储和它们之间的引用", highlight="本图重点", note="父镜像必须活得比所有子镜像久")
T = d.y + 8
PH = 500
# 面板：tmpfs（镜像 + state.json）、根盘、内核
tx0, tx1 = M, M + 1000
ex0, ex1 = M + 1060, W - M
d.rect(tx0, T, tx1 - tx0, PH, "#FBFBF9", "#D3D1C7", sw=1, rx=14)
d.text(tx0 + 20, T + 30, "tmpfs：/var/lib/gsd/snapshots/", 16, INK, 600)
d.text(tx0 + 20, T + 52, "guest 这一次开机期间存在", 13, MUTED)
KH = 190
d.rect(ex0, T, ex1 - ex0, KH, "#FBFBF9", "#D3D1C7", sw=1, rx=14)
d.text(ex0 + 20, T + 30, "内核内存：当前挂着的层栈", 16, INK, 600)
d.text(ex0 + 20, T + 52, "无法反过来查询，/proc/mounts 只显示开机时那几层", 13, MUTED)
EY = T + KH + 30
d.rect(ex0, EY, ex1 - ex0, T + PH - EY, "#FBFBF9", "#D3D1C7", sw=1, rx=14)
d.text(ex0 + 20, EY + 30, "根盘 ext4：/overlay/layers/", 16, INK, 600)
d.text(ex0 + 20, EY + 52, "guest 重启后仍在", 13, MUTED)

# 镜像目录
mx, mw = tx0 + 24, 250
ay, by = T + 120, T + 330
bh = 92
d.block(mx, ay, mw, bh, "ckpt_A_mem/t0", "全量镜像", ramp="gray", style="outline", ts=15.5, ss=13)
d.block(mx, by, mw, bh, "ckpt_B_mem/t0", "增量镜像：没写的页在父镜像里", ramp="gray", style="outline", ts=15.5, ss=13)
d.arrow(mx + mw / 2, by - 4, mx + mw / 2, ay + bh + 6, "flow", "parent 符号链接")

# state.json
sx, sw = tx0 + 360, 610
d.rect(sx - 16, T + 72, sw + 32, PH - 90, "#FFFFFF", PS, sw=1.2, rx=12)
d.text(sx, T + 98, "state.json（同一个 tmpfs 上）", 15, PT, 600)
d.block(sx, ay, sw, bh, "条目 A", "layers: L1, /mnt/lower；trees: t0（无父）",
        ramp="purple", style="fill", ts=15.5, ss=13)
d.block(sx, by, sw, bh, "条目 B", "layers: L2, L1, /mnt/lower；trees: t0（parent_id = A）",
        ramp="purple", style="fill", ts=15.5, ss=13)
d.text(sx + sw / 2, ay + bh + 36, "池给 A 记子节点计数 = 1", 13.5, PSUB, 600, "middle")
d.text(sx + sw / 2, ay + bh + 58, "last_trees：下一次增量以哪个镜像目录为父", 13, SUB, 400, "middle")
# overlay 记录
oy = T + 72 + 6
ow = 250
ox = sx + sw - ow
d.block(ox, oy + 2, ow, 30, "overlay：当前 upper、层栈、序号", None, ramp="purple", style="outline", ts=13, ss=12)

# 箭头：条目 → 镜像目录
d.arrow(sx - 4, ay + bh / 2, mx + mw + 6, ay + bh / 2)
d.arrow(sx - 4, by + bh / 2, mx + mw + 6, by + bh / 2)

# 层目录
lx, lw = ex0 + 24, ex1 - ex0 - 48
ly0 = EY + 74
LH = 40
layers = [("当前 upper / work", "dashed"), ("L2", "outline"), ("L1", "outline")]
lyy = {}
for k, (n, st) in enumerate(layers):
    yy = ly0 + k * (LH + 14)
    d.block(lx, yy, lw, LH, n, None, ramp="gray", style=st, ts=14.5)
    lyy[n] = yy + LH / 2
d.text(lx, ly0 + 3 * (LH + 14) + 14, "/mnt/lower 是基础层，开机时挂好，不归 gsd 管", 12.5, MUTED)
# 条目 → 层
d.arrow(sx + sw + 4, ay + bh / 2, lx - 6, lyy["L1"] - 6)
d.arrow(sx + sw + 4, by + bh / 2 - 10, lx - 6, lyy["L2"] + 6)
d.arrow(sx + sw + 4, by + bh / 2 + 10, lx - 6, lyy["L1"] + 8)
# 内核层栈
ky = T + 74
for k, n in enumerate(["upper", "L2", "L1", "/mnt/lower"]):
    d.rect(lx + k * (lw / 4), ky, lw / 4 - 8, 34, PF, PS, sw=1, rx=6)
    d.text(lx + k * (lw / 4) + (lw / 4 - 8) / 2, ky + 22, n, 12.5, PT, 600, "middle")
d.text(lx, ky + 66, "overlay 记录必须与它一致：gsd 每次切换都按记录拼完整的层栈", 13, SUB)
d.text(lx, ky + 88, "拼错一层，轻则 ioctl 失败，重则切到内容不对的文件系统", 13, SUB)
d.arrow(ox + ow + 4, oy + 17, lx - 8, ky + 17)
d.y = T + PH
d.callout("guest 重启时内核层栈和 tmpfs 一起归零，state.json 不存在可以无歧义地当成“刚开机”；gsd 进程重启时两者都在，加载 state.json 回到重启前",
          "purple")

# ---------------------------------------------------------------- 2. 删除
d.section(2, "删除与回收：先 A 后 B", note="删除一个 checkpoint 不保证立刻腾出空间")
d.flow([
    Step("删 A", "A 还有子节点 B：只打“已删除”标记。list 只剩 B，restore A 得到 not_found；A 的镜像目录和层都保留",
         actor="pool.remove", ramp="purple"),
    Step("删 B", "B 没有子节点：从池里摘掉；A 的计数减到 0，A 已标记删除，一并摘掉", actor="pool.remove", ramp="purple"),
    Step("先写 state.json", "写成功才删目录；写失败就不删，留给下一次 delete 或重启时回收", actor="DeleteCheckpoint"),
    Step("锁内改名进 .trash", "镜像目录：ckpt_A_mem、ckpt_B_mem 已无条目引用。层目录：不在当前层栈、也不被任何条目引用的才算", actor="回收"),
], min_h=150, gap=40)
y0 = d.y
d.callout("被删的如果正是 last_trees 指向的父，那几棵树的记录清掉、下一次全量——即使它只是被隐藏", "gray", span=(0, 1))
y1 = d.y
d.y = y0
d.callout("放锁后在后台 goroutine 里清空 .trash；上图里 L2、L1 仍在当前层栈中，所以留下", "gray", span=(2, 3))
d.y = max(y1, d.y)
d.footer("注：反过来先删目录后写状态，两步之间崩溃会留下指向已删目录的条目，重启后一 restore 就失败；"
         "按现在的顺序崩溃只会多出无主目录，重启后由回收收掉。")
d.save(out("process", "15-1-依赖与删除回收"))
