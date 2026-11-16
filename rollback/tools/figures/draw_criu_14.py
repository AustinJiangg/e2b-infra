"""进程级 14：提交点与三类 degraded（14 §2、§4，新增）。restore 两段的逐步细节见 12-1。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, wrap

CF, CS, CT, CSUB = RAMPS["coral"]

d = Diagram()
d.header("失败之后沙箱在哪：提交点之前什么都没动，之后返回 data_loss 并进入 degraded",
         "checkpoint 没有不可逆步骤，任何失败都回到“进程照常运行、列表没有变化”；restore 以杀进程为提交点。"
         "degraded 指 gsd 能应答、但暂时不该做 checkpoint 或 restore，有三个独立的原因。",
         legend=[("flow", "正常顺序"), ("warn", "在这一步失败")])
M, W = d.M, d.W


def lane(y, steps, gap=44, h=96):
    n = len(steps)
    bw = (W - 2 * M - (n - 1) * gap) / n
    xs = [M + i * (bw + gap) for i in range(n)]
    for i, (t, s, a, r, st) in enumerate(steps):
        d.block(xs[i], y, bw, h, t, s, ramp=r, style=st, actor=a, ts=15.5, ss=13)
        if i < n - 1:
            d.arrow(xs[i] + bw + 4, y + h / 2, xs[i + 1] - 6, y + h / 2)
    return xs, bw


def outcome(x0, x1, y, h, title, sub, ramp, style):
    d.block(x0, y, x1 - x0, h, title, sub, ramp=ramp, style=style, ts=15.5, ss=13)


# ---------------------------------------------------------------- 1. checkpoint
d.section(1, "checkpoint：没有不可逆步骤", note="dump 一旦开始，失败之后下一次 checkpoint 每棵树都是全量")
y = d.y + 6
H = 108
xs, bw = lane(y, [
    ("检查与预检", "degraded 检查、扫描 /proc、多树预检、容量预检、建镜像目录", "gsd", "gray", "fill"),
    ("逐棵 criu dump", "dump 完留在停止状态", "CRIU", "purple", "fill"),
    ("沉下 upper", "改名 L<n> + checkpoint 模式 ioctl；含层栈深度预检", "gsd → 内核", "purple", "fill"),
    ("清 soft-dirty、放开、入池", "成功", "gsd", "gray", "outline"),
], h=H)
oy = y + H + 54
OH = 92
for i in range(3):
    d.arrow(xs[i] + bw / 2, y + H + 2, xs[i] + bw / 2, oy - 6, "warn")
outcome(xs[0], xs[0] + bw, oy, OH, "什么都没动", "进程没有被停过；错误码 failed_precondition、resource_exhausted 或 internal", "gray", "outline")
outcome(xs[1], xs[2] + bw, oy, OH, "进程放开、文件系统和列表没动，但增量起点丢了",
        "放开 dump 前不是停止状态的进程；discardFailedCheckpoint 删掉整个镜像目录并清空增量父镜像记录",
        "gray", "dashed")
d.y = oy + OH

# ---------------------------------------------------------------- 2. restore
d.section(2, "restore：杀进程就是提交点", highlight="本图重点", note="两段的逐步内容见 12-1")
y = d.y + 6
xs, bw = lane(y, [
    ("第一段：检查与准备", "degraded 检查、找目标、checkImages、PrepareRestore、扫描", "gsd", "gray", "fill"),
    ("killTrees", "杀掉当前全部用户进程：不可逆", "gsd → 用户进程", "coral", "fill"),
    ("CommitRestore", "restore 模式 ioctl，切到目标层栈", "gsd → 内核", "purple", "fill"),
    ("逐棵 criu restore", "成功后解除 degraded", "CRIU", "purple", "fill"),
], h=H)
# 提交点标记
px = xs[1] - 22
d.o.append(f'<line x1="{px:.1f}" y1="{y - 6:.1f}" x2="{px:.1f}" y2="{y + H + 18:.1f}" stroke="#E24B4A" '
           f'stroke-width="1.4" stroke-dasharray="6 4"/>')
d.text(px, y + H + 36, "提交点", 13, "#A32D2D", 600, "middle")
oy = y + H + 54
d.arrow(xs[0] + bw / 2, y + H + 2, xs[0] + bw / 2, oy - 6, "warn")
d.arrow(xs[2] + bw / 2, y + H + 2, xs[2] + bw / 2, oy - 6, "warn", "文件系统 old")
d.arrow(xs[3] + bw / 2, y + H + 2, xs[3] + bw / 2, oy - 6, "warn", "文件系统 target")
OH2 = 116
outcome(xs[0], xs[0] + bw, oy, OH2, "什么都没动", "错误信息含 nothing was changed；可以直接重试或换目标", "gray", "outline")
outcome(xs[2], xs[3] + bw, oy, OH2, "data_loss：用户进程已经没了",
        "信息写明文件系统在 old 还是 target；多树时前 i 棵已恢复并运行、第 i 棵失败、其余未尝试；"
        "criu 超时也走这里。gsd 进入“restore 半途失败”，只能再 restore 一次",
        "coral", "fill")
d.text(xs[1] + bw / 2, oy + 30, "killTrees 自己报的错", 13.5, SUB, 600, "middle")
d.text(xs[1] + bw / 2, oy + 52, "只记日志，不中断 restore", 13.5, SUB, 400, "middle")
d.text(xs[1] + bw / 2, oy + 82, "没杀任何进程时切层失败：", 13, MUTED, 400, "middle")
d.text(xs[1] + bw / 2, oy + 102, "按普通错误返回，不进 degraded", 13, MUTED, 400, "middle")
d.y = oy + OH2

# ---------------------------------------------------------------- 3. degraded
d.section(3, "三类 degraded：各拦什么、怎么解除", note="DegradedReason 把非空的几个用分号连起来报告")
c = d.compare([("fatal", "状态不可信", "coral"),
               ("restore 半途失败", "沙箱处在中间态", "coral"),
               ("状态文件写失败", "磁盘上的那份落后了", "purple")], label_width=150)
c.row("何时进入", [
    ("初始化或加载失败", "overlay 管理器初始化失败；state.json 读不了、解析不了、版本不认识或记录的目录不存在"),
    ("restore 杀进程之后失败", "上面第 2 段的 data_loss"),
    ("最近一次写 state.json 失败", "内存里的状态是对的"),
])
c.row("怎么解除", [("重启 gsd", "重启后重新判断；fatal 下不写状态文件、不回收镜像"),
                  ("一次成功的 restore", "clearRestoreFailure；原因存进 state.json 的 restore_failure，跨 gsd 重启保留"),
                  ("任何一次写成功", "重启后按磁盘上的旧文件加载")], links=[None, None, None])
c.summary("各操作是否放行", [
    ("checkpoint", [("拒", "bad"), ("拒：中间态不能存成 checkpoint", "bad"), "先重试写入，仍失败才拒"]),
    ("restore", [("拒", "bad"), ("允许：回到完整状态的唯一手段", "good"), "先重试写入，仍失败才拒"]),
    ("delete", [("不拦（池为空，只会 not_found）", "muted"), ("允许：可能要先腾空间", "good"),
                ("照常：store 满时唯一能腾空间的操作", "good")]),
])
c.end()
d.footer("注：/health 始终返回 HTTP 200，degraded 时 body 是 {\"reason\":\"...\",\"status\":\"degraded\"}；"
         "探活据此重启对 fatal 没有帮助，对另外两类有害。")
d.save(out("criu", "14-1-提交点与三类degraded"))
