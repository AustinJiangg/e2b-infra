"""进程级手册第 11 章 checkpoint 的实现：11-1 checkpoint 总览（替换 §1 的文本示意）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, ARROWS, INK, text_w


def divider(d, text, ramp="purple", kind=None):
    """横跨全宽的分界线，中间一个胶囊写明这条线的含义。kind='warn' 时线用珊瑚色。"""
    fill, stroke, tc, _ = RAMPS[ramp]
    if kind:
        stroke = ARROWS[kind][0]
    y = d.y + 34
    d.o.append(f'<line x1="{d.M}" y1="{y}" x2="{d.W - d.M}" y2="{y}" stroke="{stroke}" '
               f'stroke-width="2" stroke-dasharray="8 5"/>')
    w = text_w(text, 15) + 36
    d.rect(d.W / 2 - w / 2, y - 17, w, 34, fill, stroke, sw=1.4, rx=17)
    d.text(d.W / 2, y + 5.5, text, 15, tc, 600, "middle")
    d.y = y + 10


def d11_1():
    d = Diagram()
    d.header("checkpoint 的执行顺序：预检之前什么都不动，全部树停着时换层",
             "CreateCheckpoint 全程持操作锁。checkpoint 没有不可逆的步骤：任何一步失败，收尾之后进程在跑、"
             "文件系统没变、没有多出条目。",
             legend=[("flow", "执行顺序")])

    d.section(1, "进入与预检", note="多树预检、容量预检只在有用户进程时做")
    d.flow([
        Step("持操作锁 opMu", "checkpoint、restore、delete 互斥，后来的排队；全程持有", "gsd"),
        Step("检查健康状态", "checkReady(false)：fatal、上次 restore 半途失败、状态文件写不进去都拒绝", "gsd"),
        Step("扫描 /proc、找根、记状态", "scanUsers 一次扫描；记下每个进程此刻是否停止", "gsd"),
        Step("多树预检", "crossTreeShared，命中则 FailedPrecondition", "gsd", tag=("两棵及以上树才做", "purple")),
        Step("容量预检", "estimateDumpIn：内存之和 × 1.2，不够则 ResourceExhausted", "gsd"),
    ], min_h=150)
    divider(d, "到这里为止什么都没动：没有进程被停过，没有目录被建过")

    d.section(2, "逐棵 dump", note="按根进程号升序，一次一棵")
    d.flow([
        Step("建镜像目录 <id>_mem", "没有用户进程时也建，是个空目录", "gsd"),
        Step("选增量父镜像", "incrementalParent：找到则加 --prev-images-dir，只写改过的页；找不到时这棵树做全量", "gsd"),
        Step("criu dump --leave-stopped", "镜像写到 t<i>/，--track-mem 清零 soft-dirty；dump 完后这棵树不再运行，留在停止状态", "CRIU"),
    ], min_h=130)
    d.callout("对每棵树重复后两步。第一棵 dump 完时记下 tFirstStopped，冻结窗口从这里开始；"
              "先 dump 完的树在等后面的树时一直停着", "purple", span=(1, 2))
    divider(d, "全部树都停着")

    d.section(3, "冻结窗口内", highlight="本图重点", note="此刻每棵树都停着")
    d.flow([
        Step("列出全部被停住的进程", "treePIDs 再扫一次 /proc；算出要放开的名单 resumable", "gsd"),
        Step("下沉 upper", "SinkUpper：upper 改名成层，再发 ckpt 模式的 ioctl", "gsd → guest 内核"),
        Step("重写 clear_refs", "对每个被停住的进程写 4。下沉时的切换会让内存区域整体带脏，不清则下一次增量接近全量", "gsd → guest 内核"),
        Step("SIGCONT", "只放开 dump 之前不是停止状态的进程", "gsd"),
    ], min_h=150)
    d.callout("stopped_ms：从第一棵树 dump 完到发出 SIGCONT", "purple", span=(0, 3))
    divider(d, "SIGCONT：冻结窗口结束")

    d.section(4, "收尾", note="进程已经放开，不占冻结时间")
    d.flow([
        Step("量镜像大小", "dirUsage 量每棵树镜像目录的实际占用", "gsd"),
        Step("更新增量父镜像", "用本次的树整体替换 lastTrees", "gsd"),
        Step("条目入池", "pool.add：带镜像目录、完整层栈、各棵树", "gsd"),
        Step("写 state.json，放锁", "由 defer 执行，成功失败都落盘", "gsd"),
    ], min_h=120)

    d.footer("注：没有用户进程时，多树预检、容量预检、dump、clear_refs、SIGCONT 都跳过，做只有文件系统的 checkpoint；"
             "upper 为空时不下沉。dump 或下沉失败时：放开进程、删掉整个 <id>_mem、回退改名、清空 lastTrees，下一次全量。")
    d.save(out("process", "11-1-checkpoint总览"))


if __name__ == "__main__":
    d11_1()
