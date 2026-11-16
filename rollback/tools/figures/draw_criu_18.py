"""进程级手册第 18 章测试体系：18-1 四层测试各回答什么（新增，§1）。"""
from common import *  # noqa: F401,F403


def d18_1():
    d = Diagram()
    d.header("四层测试各回答一个问题，没有一层能同时看见全部四处",
             "gsd 的正确性分散在四个地方。单测只看得见 gsd 自己以为做了什么；"
             "最严重的几个问题在内核补丁里，要靠压测在并发文件操作下连续撞出来。")

    d.section(1, "正确性分散在四个地方")
    places = [  # 四处并列，彼此不是先后关系，所以不画箭头
        ("gsd 的编排逻辑", "Go 代码：顺序、失败路径、状态文件", "guest", None),
        ("CRIU", "真实的 dump 与重建", "guest", None),
        ("overlayfs 热切层补丁", "guest 内核里的切换、回收", "guest 内核",
         ("切换时的内核崩溃、层不释放、BUG 都在这里", "purple")),
        ("注入逻辑", "建模板时把 gsd、CRIU 装进 guest", "建模板", None),
    ]
    gap = 28
    bw = (d.W - 2 * d.M - 3 * gap) / 4
    for i, (t, s, a, tag) in enumerate(places):
        d.block(d.M + i * (bw + gap), d.y, bw, 140, t, s, actor=a, tag=tag)
    d.y += 140

    d.section(2, "四层测试：每层只对自己看得见的部分负责", highlight="本图重点")
    c = d.compare([("单测", "Go 与 Python 的单元测试", "purple"),
                   ("功能验证", "四个 verify_* 脚本", "purple"),
                   ("性能", "demo_checkpoint_perf、exp_gsd_swap", "purple"),
                   ("压测与探针", "四个 stress_*、十几个 probe_*", "purple")], label_width=120)
    c.row("回答什么", [("gsd 以为自己做了什么", "用假的 /proc 和假的 criu 断言编排"),
                      ("做一件事，结果对不对", "进程是否回来、文件是否回滚"),
                      ("时间花在哪", "各分段耗时，两个版本之间的差"),
                      ("并发下会不会坏", "压测撞竞态；探针各复现一个具体问题")])
    c.row("在哪跑", [("有 Go 工具链的机器", "不需要 guest"),
                    ("进程级栈运行中的宿主机", "起真实沙箱"),
                    ("进程级栈运行中的宿主机", "起真实沙箱"),
                    ("进程级栈运行中的宿主机", "压测另在调试内核上跑一遍")], links=[None] * 4)
    c.row("能抓到", [("编排与失败路径", "清理、状态文件、增量父子关系、SDK 错误映射"),
                    ("端到端行为", "错误码与 SDK 异常、增量是否漏页"),
                    ("分段与一致性", "SDK 返回的分段与 gsd 日志是否一致"),
                    ("内核侧竞态", "切换与并发文件操作、释放后使用、死锁、泄漏")], links=[None] * 4)
    c.row("抓不到", [Box("真实 CRIU、内核补丁", "以及模板注入的实际效果", style="dashed"),
                    Box("内核竞态", "每项只跑几秒到几十秒", style="dashed"),
                    Box("未覆盖的程序或内核行为", "也不能替代功能验收", style="dashed"),
                    Box("功能语义的全面覆盖", "压测只盯几条不变量", style="dashed")], links=[None] * 4)
    c.end()

    d.footer("注：多数脚本的退出码不是判定，各脚本的判定看哪一行见 §1.1。本章只讲用什么测、怎么判，"
             "测到的结果见第 19～22 章。")
    d.save(out("criu", "18-1-四层测试各回答什么"))


if __name__ == "__main__":
    d18_1()
