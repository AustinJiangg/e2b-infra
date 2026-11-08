"""进程级 08：一次热切层 ioctl 的阶段、提交点与锁（替换 08 §2 的 mermaid）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, ARROWS, INK, SUB, MUTED, text_w, wrap

d = Diagram()
d.header("一次热切层 ioctl：会失败的都在提交点之前，提交点之后不再失败",
         "主体是 ovl_do_ioctl_checkpoint，外面的 ovl_ioctl_checkpoint 负责权限检查和串行化。"
         "左边是各段的失败语义，右边竖条是每把锁从拿到到放开的范围。",
         legend=[("flow", "按顺序执行")])
d.section(1, "阶段与锁", note="提交点之前失败时，文件系统的状态一个字节都没变", highlight="本图重点")

M, W = d.M, d.W
SX, SW = M + 250, 700            # 步骤列
BX = [SX + SW + 40 + 34 * i for i in range(4)]   # 四根竖条
LBX = BX[-1] + 40                # 锁说明
LBW = W - M - LBX
BH, GAP = 60, 20

steps_pre = [
    ("A", "ckpt_mutex 加锁：同一时刻只有一个切换", "先查 CAP_SYS_ADMIN，再以可被信号打断的方式拿锁"),
    ("B", "解析选项串，拒绝 index 目录", "index=on 或 nfs_export=on 时返回 -EOPNOTSUPP"),
    ("C", "为新 upper 和各 lower 建私有 mount 和 trap", "新 upper 与当前 upper 不在同一文件系统时返回 -EXDEV"),
    ("D", "分配新的根 entry 和退役记录", "退役记录是 struct ovl_layer_set"),
    ("E", "在新 upper 上建好 work 目录和它的 trap", "ovl_ckpt_create_workdir 借临时 ovl_fs 建；失败返回 -ENOMEM"),
    ("F", "switch_sem 写锁：等所有 overlay 操作退出", None),
    ("G", "清掉没人用的 dentry", None),
    ("H", "建计划：最后一个可能失败的步骤", "为每个受影响的 inode 拿住引用、预先分配新 ovl_entry（§4.3）"),
]
steps_post = [
    ("J", "丢弃 whiteout 缓存，换 work 目录的 trap", None),
    ("K", "在 seqlock 内发布 layers、numlayer、checkpoint_gen", "读者用 ovl_layers_snapshot() 成组读到这三个值"),
    ("L", "按计划改写每个缓存的 inode 和 dentry", None),
    ("M", "换 work 目录和根 entry", None),
    ("N", "checkpoint 时搬走仍以写方式打开的文件", "见 §5"),
    ("O", "缓存对象的层指针改指新数组，无人引用的旧数组摘下", None),
    ("P", "放开 switch_sem", None),
    ("Q", "释放上一个周期延迟下来的引用", "ovl_release_deferred() 逐个释放"),
    ("R", "放开 ckpt_mutex", None),
]

ypos = {}
y = d.y + 14


def draw_steps(steps, y, ramp):
    for i, (k, t, s) in enumerate(steps):
        d.block(SX, y, SW, BH, t, s, ramp=ramp, ts=15.5, ss=13)
        ypos[k] = (y, y + BH)
        if i < len(steps) - 1:
            d.arrow(SX + SW / 2, y + BH + 2, SX + SW / 2, y + BH + GAP - 4)
        y += BH + GAP
    return y - GAP


def stage(y0, y1, ramp, lines):
    fill, stroke, tc, sc = RAMPS[ramp]
    d.rect(M, y0, 220, y1 - y0, fill, stroke, sw=1, rx=10)
    yy = y0 + 34
    for k, (s, big) in enumerate(lines):
        for ln in wrap(s, 220 - 32, 16 if big else 13.5):
            d.text(M + 16, yy, ln, 16 if big else 13.5, tc if big else sc, 600 if big else 400)
            yy += 24 if big else 20
        yy += 6


pre_top = y
pre_bot = draw_steps(steps_pre, y, "gray")
stage(pre_top, pre_bot, "gray", [("提交点之前", True), ("每一步都可能失败。", False),
                                 ("失败时 ofs 没被碰过，没有东西需要回滚，清理只释放这次 ioctl 自己分配的对象。", False)])

# 提交点
cy = pre_bot + 44
d.arrow(SX + SW / 2, pre_bot + 2, SX + SW / 2, cy - 16)
d.o.append(f'<line x1="{M}" y1="{cy}" x2="{W - M}" y2="{cy}" stroke="{INK}" stroke-width="2.2"/>')
lab = "提交点"
pw = text_w(lab, 15) + 32
d.rect(SX + SW / 2 - pw / 2, cy - 15, pw, 30, INK, INK, rx=15)
d.text(SX + SW / 2, cy + 5.5, lab, 15, "#FFFFFF", 600, "middle")
ypos["I"] = (cy, cy)
post_top = cy + 44
d.arrow(SX + SW / 2, cy + 16, SX + SW / 2, post_top - 4)
post_bot = draw_steps(steps_post, post_top, "purple")
stage(post_top, post_bot, "purple", [("提交点之后", True), ("每一步都不会失败。", False),
                                     ("仍需内存的两处小分配用 __GFP_NOFAIL：", False), ("ovl_defer_put 与 ovl_inode_keep_real 的链表节点。", False)])

# 锁的范围
locks = [
    (0, "A", "R", "gray", "ckpt_mutex（互斥锁）", "ioctl 全程持有：两个切换不交错；保护旧层数组的链表"),
    (1, "F", "P", "purple", "switch_sem 写锁（读写信号量）",
     "overlay 的每个操作持读锁；写锁期间没有操作在读后端状态：__upperdentry、lowerpath、ovl_entry、layers、workdir、realfile"),
    (2, "K", "K", "purple", "layers_seq 写侧（seqlock）", "layers、numlayer、checkpoint_gen 三个值作为一组被读到"),
    (3, "Q", "Q", "gray", "deferred 链表（自旋锁保护）", "被换下、别的读者可能还借着的对象；替换引用的人经 ovl_defer_put() 挂进来，下一次 ioctl 才释放"),
]
for i, a, b, ramp, name, what in locks:
    fill, stroke, tc, sc = RAMPS[ramp]
    y0, y1 = ypos[a][0], ypos[b][1]
    x = BX[i]
    d.rect(x, y0, 14, y1 - y0, stroke, stroke, sw=1.2, rx=7)
    d.o.append(f'<line x1="{x + 18}" y1="{y0 + 10}" x2="{LBX - 8}" y2="{y0 + 10}" stroke="{stroke}" '
               f'stroke-width="1" stroke-dasharray="3 3"/>')
    d.text(LBX, y0 + 15, name, 14.5, tc, 600)
    for k, ln in enumerate(wrap(what, LBW, 13)):
        d.text(LBX, y0 + 36 + 19 * k, ln, 13, sc)

d.y = post_bot
d.footer("注：运行时代码凡是要用 numlayer 约束 layers[] 下标的，都经 ovl_layers_snapshot() 读；"
         "只有 layers[0]（upper 的 mount）可以不经快照直接读，因为任何一个层数组都至少有一项。")
d.save(out("gsd", "08-1-一次ioctl的阶段与锁"))
