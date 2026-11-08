"""第 16 章配图：16-1 三层测试与测试矩阵，各层回答什么问题。"""
from common import *  # noqa: F401,F403


def fig1():
    d = Diagram(width=1600)
    d.header("三层测试各回答一个问题：部件对不对，交付的整机对不对，盲区和长稳有没有问题",
             "单元测试证部件，交付态验收证整机，补充测试覆盖盲区、边界、故障注入和长稳，三层不能互相替代。"
             "测试矩阵（§2）里的每一行都落在后两层；单元测试不进矩阵。")

    d.section(1, "三层各在哪、证什么、怎么跑", highlight="本图重点")
    c = d.compare([("单元测试与属性测试", "部件", "gray"),
                   ("交付态验收", "整机", "gray"),
                   ("补充测试与开发态工具", "盲区与长稳", "gray")])
    c.states("回答的问题", [("部件与不变量对不对", "属性测试随机生成操作序列，每一步与参照模型比对"),
                         ("交付的整机对不对、多快", "整机正确性、耗时、并发、长跑"),
                         ("前两层看不到的地方", "盲区、边界、故障注入、长稳")])
    c.row("位置", [("代码仓库 KASandbox_0904", "分支 deltabox-dev，与被测代码同目录：Go、FC 的 #[cfg(test)]、SDK 的 py-sdk/tests"),
                  ("rollback/scripts/acceptance/", "五个单文件脚本，互不依赖；外加 rolling_keep10.py"),
                  ("rollback/scripts/crtest/、dev/", "crtest 20 个用例 + 基准；dev 是共享 lib.py 的工具箱")],
          links=[None, None, None])
    c.row("怎么跑", [("在代码仓库里单独跑", "950 测试部署的构建只做 go build，不跑单测（第 29 章）"),
                   ("单文件拷到目标机就能跑", "rolling_keep10.py 要和 checkpoint_concurrent.py 一起拷"),
                   ("一站式入口 rollback/scripts/950/run.sh", "smoke、hdbss 等档")], links=[None, None, None])
    c.row("矩阵里的行", [Note("不进测试矩阵", "守着的不变量见第 11、29 章"),
                       ("checkpoint_verify.py：59 项", "checkpoint_concurrent.py：A/B 短时并发、D 段长稳"),
                       ("correctness.py、pause_verify.py、compat_matrix.py、loop.py、hdbss_evidence.py",
                        "crtest T11–T41（含 T36、T39、T40、T41）、sdktests")], links=[None, None, None])
    c.end()

    d.section(2, "在哪台机器上跑", note="§1 的表：脚本能量到什么，取决于它跑在哪")
    c = d.compare([("在宿主机上以 root 跑", "性能测量必须在这里", "gray"),
                   ("从别的机器连过来", "只适合验正确性", "gray")], label_width=200)
    c.summary("能量到的东西", [
        ("脏页后端", [("问 FC API 套接字", "good"), ("“未知”：套接字是本地文件", "bad")]),
        ("产物路径与文件系统", [("读 environ 与 /proc/mounts", "good"), ("空", "bad")]),
        ("每代内存 / 磁盘产物实占", [("量 st_blocks", "good"), ("量不到", "bad")]),
        ("服务端分段计时", [("读 timings.json", "good"), ("量不到", "bad")]),
        ("客户端墙钟、全部正确性断言", [("有", "good"), ("有", "good")]),
    ])
    c.end()
    d.callout("正确性可以远程验，性能必须上宿主机；远程跑出的耗时缺条件标签，不允许引用（§4）", "coral")
    d.footer("注：测试矩阵只记各项在 920B、950 上的状态，数字见第 17～21 章；默认功能回归通过不代表未运行的重启 / 注入项也通过。")
    d.save(out("rollback", "16-1-三层测试与测试矩阵"))


if __name__ == "__main__":
    fig1()
