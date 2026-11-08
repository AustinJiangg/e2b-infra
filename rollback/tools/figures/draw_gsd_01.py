"""进程级第 01 章配图：一次 checkpoint、一次 restore 依次经过哪几章（§4 主线）。"""
from common import *  # noqa: F401,F403

# 步骤与章号取自 gsd/docs/01-overview.md §4.1、§4.2 两张表；actor 位置写章号


def fig1():
    d = Diagram(width=1600)
    d.header("全书主线：一次 checkpoint、一次 restore 各经过哪几章",
             "每个框是主线上的一段，左上角是讲它的章（原理与实现）。全书各章是在放大这两条路径上的某一步；"
             "把两条路径完整走一遍、标出每个计时字段落在哪一步的是第 13 章。",
             legend=[("flow", "执行顺序")])

    d.section(1, "一次 checkpoint", note="§4.1 的第 1～10 步")
    d.flow([
        Step("请求进到 gsd", "SDK 发到 49984 端口，宿主代理转进沙箱；gsd 校验 token，取串行锁", "05 · 14 · 15 · 25", ramp="purple"),
        Step("扫描与预检", "扫一次 /proc 找出全部树的根；树之间有没有共用的管道或 socket，镜像放不放得下", "10 · 15",
             ramp="purple"),
        Step("逐树 CRIU dump", "做完的树停在原地；能做增量的树只写脏页", "03 · 11", ramp="purple"),
        Step("沉层、换新可写层", "所有树都停住之后，当前可写层沉成只读层", "07 · 08 · 09", ramp="purple"),
        Step("重清 soft-dirty，继续运行", "记录 checkpoint、写状态文件，各阶段耗时随响应返回", "03 · 11 · 15 · 28",
             ramp="purple"),
    ], min_h=150, gap=40)
    d.callout("第 3～5 段在同一段“进程全部停着”的窗口里：进程镜像与沉下去的文件系统是同一时刻", "purple", span=(2, 4))
    d.callout("任何一步失败：放开已停住的进程，删掉这次的镜像，沙箱回到原状（第 14 章）", "gray")

    d.section(2, "一次 restore", note="§4.2 的第 1～8 步", highlight="提交点：杀掉当前用户进程")
    d.flow([
        Step("请求进到 gsd", "校验 token、取串行锁，找到目标 checkpoint", "05 · 15", ramp="purple"),
        Step("杀进程前先查完", "每棵树的镜像和依赖的父镜像都在；切层要用的目录建好", "12", ramp="purple",
             tag=("到这里为止失败：什么都没动", "gray")),
        Step("扫描并杀掉用户进程", "先冻住、确认没有新进程冒出来、再杀", "10 · 12", ramp="purple",
             tag=("restore 的提交点", "coral")),
        Step("切回层栈", "换回目标 checkpoint 的层栈，换上全新的可写层", "07 · 08", ramp="purple"),
        Step("逐树 CRIU restore", "按保存时的顺序，进程用原来的 PID 继续运行；记下下一次增量的父，后台回收目录",
             "03 · 12 · 15", ramp="purple"),
    ], min_h=150, gap=40)
    d.callout("提交点之后失败：进程已经没了，沙箱进入降级状态，只能再 restore 一次（第 14 章）", "coral", span=(2, 4))
    d.footer("注：框左上角的数字是章号。两条路径都不暂停虚拟机，范围外的进程一直在运行（第 08、09 章）。第 01～04 章是导论与基础，第 17 章是代价模型，第 18～23 章是验证与实测，"
             "第 24 章与沙箱级对比，第 25～30 章是使用与运维。")
    d.save(out("gsd", "01-1-全书主线"))


if __name__ == "__main__":
    fig1()
