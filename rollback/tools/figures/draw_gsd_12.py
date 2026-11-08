"""进程级手册第 12 章 restore 的实现：12-1 restore 两段式（替换 §2 的文本示意）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, ARROWS, text_w


def divider(d, text, ramp="purple", kind=None):
    """横跨全宽的分界线，中间一个胶囊写明这条线的含义。kind='warn' 时线和胶囊边用珊瑚色。"""
    fill, stroke, tc, _ = RAMPS[ramp]
    if kind:
        stroke = ARROWS[kind][0]
        tc = stroke
        fill = "#FFFFFF"
    y = d.y + 34
    d.o.append(f'<line x1="{d.M}" y1="{y}" x2="{d.W - d.M}" y2="{y}" stroke="{stroke}" '
               f'stroke-width="2" stroke-dasharray="8 5"/>')
    w = text_w(text, 15) + 36
    d.rect(d.W / 2 - w / 2, y - 17, w, 34, fill, stroke, sw=1.4, rx=17)
    d.text(d.W / 2, y + 5.5, text, 15, tc, 600, "middle")
    d.y = y + 10


def d12_1():
    d = Diagram()
    d.header("restore 分两段：杀掉当前用户进程就是提交点",
             "被杀掉的进程无法复原，所以 RestoreCheckpoint 把能提前做、又不改变任何东西的检查和准备，全部放到杀进程之前。"
             "提交点之前失败，调用方可以当它没发生过；之后失败返回 DataLoss，gsd 进入 degraded。",
             legend=[("flow", "执行顺序")])

    d.section(1, "第一段：杀进程之前", note="失败时什么都没动，返回普通错误，不进入 degraded")
    d.flow([
        Step("持操作锁 opMu", "与 checkpoint、delete 互斥", "gsd"),
        Step("检查健康状态，查条目", "checkReady(true)：上一次 restore 半途失败不拦；目标不存在 → NotFound", "gsd"),
        Step("逐棵检查镜像与父链", "checkImages：inventory.img 可读，沿 parent 链一路查到底", "gsd"),
        Step("PrepareRestore", "预检选项串长度、检查层目录、建好新的 upper 与 work", "gsd"),
        Step("扫描 /proc", "列出全部用户进程；紧挨着杀进程，结果直接当第一轮", "gsd"),
    ], min_h=150)
    d.callout("以上失败：错误信息带 nothing was changed，错误码 NotFound / FailedPrecondition / "
              "ResourceExhausted / Internal", "gray")

    divider(d, "提交点：从这里起无法回到 restore 之前的状态", kind="warn")

    d.section(2, "杀进程", highlight="本图重点", note="不管目标有没有进程镜像，当前用户进程一律杀掉")
    d.flow([
        Step("killTrees", "先冻住，反复扫描到没有新进程，整体 SIGKILL，等进程号释放", "gsd → 用户进程",
             tag=("没有用户进程时跳过", "purple")),
        Step("清空 lastTrees", "原来那些进程的 soft-dirty 历史不再有意义", "gsd"),
    ], min_h=120)
    d.callout("killTrees 报告的错误只记一条告警，restore 继续往下走：部分进程已经被杀，停下来不比做完更好", "gray")

    d.section(3, "第二段：切换与恢复", note="以下失败：DataLoss，进入 degraded")
    d.flow([
        Step("CommitRestore", "restore 模式的切换 ioctl，lowerdir 是目标的完整层栈", "gsd → guest 内核"),
        Step("逐棵 criu restore", "按 dump 时的顺序串行；--restore-detached，按原进程号重建", "CRIU"),
        Step("重建增量父镜像，解除 degraded", "用恢复出的根进程号与启动时间替换 lastTrees；clearRestoreFailure", "gsd"),
        Step("写 state.json，放锁", "由 defer 执行", "gsd"),
    ], min_h=150)
    d.callout("以上失败：DataLoss，信息写明用户进程已经没了、文件系统处在 old 还是 target；degraded 期间 checkpoint 被拒绝，"
              "restore 和 delete 仍然允许，直到下一次 restore 成功", "purple")

    d.footer("注：唯一的例外是 CommitRestore 失败而 killTrees 一个进程都没杀（沙箱本来就没有用户进程）：进程没动、文件系统没切成，"
             "返回普通错误，不进入 degraded。没有进程镜像的目标到 CommitRestore 就结束。")
    d.save(out("gsd", "12-1-restore两段式"))


if __name__ == "__main__":
    d12_1()
