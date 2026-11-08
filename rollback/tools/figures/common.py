"""两本手册配图的公共设置。每个 draw_*.py 先 `from common import *`。

- 讲解图用 Diagram，数据图用 Chart（见 kit/）。
- 颜色含义两本书统一：
    teal   = 沙箱级方案（本方案的 checkpoint / restore）
    coral  = 原生 pause / resume（重建、代价）
    purple = 进程级方案（gsd、CRIU）
    gray   = 共同部分
    blue   = 950 数据，amber = 920B 数据
- 输出 docs/img/NN-k-中文名.svg，NN 是章号或附录字母，k 是该章内的图序号。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "kit"))

import chart_kit  # noqa: E402
from chart_kit import Chart  # noqa: E402,F401
from diagram_kit import Diagram, Step, Box, Note, Text  # noqa: E402,F401

chart_kit.ALIASES.update(sandbox="teal", native="coral", process="purple", common="gray",
                         p950="blue", p920b="amber")

REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
IMG = {"rollback": os.path.join(REPO, "rollback", "docs", "img"),
       "gsd": os.path.join(REPO, "gsd", "docs", "img")}


def out(book, name):
    """返回 docs/img 下的输出路径，name 形如 '04-1-架构全图'。"""
    os.makedirs(IMG[book], exist_ok=True)
    return os.path.join(IMG[book], name + ".svg")
