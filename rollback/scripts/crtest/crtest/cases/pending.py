# -*- coding: utf-8 -*-
"""
留位用例：目录结构先占着，本阶段不实现，理由各自写在下面。

**当前没有留位的用例** —— 方案 §4 里点到的都已经落地：

  · T11 与 T21 在 09-17 第 3 轮实现（cases/t11.py、cases/t21.py）：服务端的
    CHECKPOINT_FAULT_INJECT 钩子（ed03fe57c）与 R2/R3 的超时、kill 互斥落地之后，
    这两个用例才有确定的期望值；
  · T18 / T34 / T36 在 09-18 实现（cases/t18.py、t34.py、t36.py）。三条原本的留位
    理由是这么消掉的：
      - T18 要停服务 —— 用例**自己不重启**，要显式 `--allow-restart` 才执行
        `--restart-cmd`（默认就是 `tmp/switch-stack.sh jll`），不给就打印命令并以
        退出码 3 退出；
      - T34 是长测试 —— 默认规模缩到 `--depths 0,20,50`、每层改 4 MB，一次 ≤ 5 分钟，
        需求书的 0/50/200 全规模用参数开；
      - T36 是长测试 —— 默认 `--seconds 120`，30 分钟全规模用 `--seconds 1800`。

这个模块与 `__main__.PENDING` 一起留着：以后再有"先占位、后实现"的用例，
把编号加回 `PENDING`、在 `REASONS` 里写一句理由即可。
"""


REASONS = {}


def run_pending(code):
    print("用例 %s 本阶段未实现：%s" % (code, REASONS.get(code, "见 crtest/cases/pending.py")))
    print("（目录已留位，实现时在 crtest/cases/ 下加同名模块并登记进 __main__.CASES）")
    return 2
