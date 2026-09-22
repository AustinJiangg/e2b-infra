#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crtest：沙箱级 checkpoint / restore 的补充测试套件。

一个用例一个子命令，各自独立可跑、各自出一份 JSON：

    python -m crtest T25 --out t25.json
    python -m crtest T14 --sandboxes 4 --rounds 2
    python -m crtest --list

用例编号对齐 `e2b-repo/checkpoint-restore-fix-plan-2026-09-17.md` §4 的 T11–T36。
本目录实现了其中十六个，其余留位（见 `crtest/cases/pending.py`；当前没有留位的）。

所有用例的公共参数（`--env-file / --template / --out / --keep-on-failure /
--public / --no-probe`）见 `common.add_common_args`。默认规模都按"一次 ≤ 5 分钟"
定的，要压更狠就自己调 `--sandboxes / --rounds / --seconds`。

跑之前先读 `e2b-repo/stack-components-checklist.md`（两套栈别混）。
"""

import argparse
import importlib
import sys

from . import common

# 编号 → (模块名, 一句话)。顺序 = 落地顺序（方案 §4 末尾"落地顺序"那一行）。
CASES = [
    ("T25", "t25", "时间与 CPU 健康：uptime 倒退、date 对齐、sleep 1、CPU0 空闲、arch_timer 速率"),
    ("T26", "t26", "串口：restore 后 guest 写 /dev/ttyS0 4 KB 不阻塞（F1 回归）"),
    ("T21", "t21", "故障注入：CHECKPOINT_FAULT_INJECT 的四条失败路径（一次一条，--fault）"),
    ("T14", "t14", "写密集并发正确性：256 MB 自校验内存 + fsync 文件，并发 create/restore"),
    ("T13", "t13", "跨沙箱冻结窗口干扰：N-1 个大脏集 create，第 N 个 restore + 10 ms 心跳"),
    ("T12", "t12", "同沙箱多客户端交错：4 个 connect() 随机 create/restore/delete/list"),
    ("T11", "t11", "生命周期竞争：create/restore 进行中 kill / beta_pause，资源回收与不挂锁"),
    ("T15", "t15", "树形分支并发：restore 到随机祖先再 create，每个 checkpoint 内容 = 拍它那一刻"),
    ("T24", "t24", "网络状态：活 TCP 连接跨 restore 的语义、新连接可用、conntrack 计数"),
    ("T23", "t23", "文件系统边界：流式写、open fd、目录 rename、fsync 语义"),
    ("T22", "t22", "深链随机回滚：建链+分支，随机顺序回每一个；删中间层再回后代"),
    ("T32", "t32", "restore 随脏集：0/64/256 MB 三档的 fc_memory / fc_bitmap / materialize / frozen"),
    ("T37", "t37", "两道配额闸：产物盘水位 507 disk_full / 每沙箱条目上限 429 too_many_checkpoints（--quota）"),
    ("T18", "t18", "orchestrator 重启：重启后 list 为空、create 走全量新根、旧 store 被清（--allow-restart）"),
    ("T34", "t34", "运行期读链深（B8）：链深 0/20/50 下 guest 冷读吞吐与延迟，只记录曲线"),
    ("T36", "t36", "混合稳态：N 沙箱随机 create/restore/delete/list/命令/写盘，跑完对账与回收"),
    ("T38", "t38", "FC 侧 faulted：rollback 过了 commit point 才炸，沙箱判 torn（--fault fc_post_commit）"),
    ("T39", "t39", "做过 checkpoint 之后原生 pause/resume：内存差分不能只含最后一段 epoch（P0，09-18）"),
    ("T40", "t40", "restore 紧跟请求：长流 + unary 横跨下一次 restore，抓 envd 流式截断（§2.18）"),
    ("T41", "t41", "干净沙箱的原生 pause/resume：全程不做 checkpoint，连续多轮 + resume 之后再 checkpoint/restore + 账本不跨 pause"),
]

# 留位（本阶段不做，原因见 cases/pending.py）。T18/T34/T36 已于 09-18 实现，这里暂时空着。
PENDING = []


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m crtest",
        description="沙箱级 checkpoint/restore 补充测试（950 ext4）。一个用例一个子命令。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="留位未实现：%s（见 crtest/cases/pending.py）"
               % (", ".join(PENDING) if PENDING else "无"))
    ap.add_argument("--list", action="store_true", help="列出所有用例后退出")
    sub = ap.add_subparsers(dest="case", metavar="用例")
    for code, mod, help_text in CASES:
        p = sub.add_parser(code, help=help_text, description=help_text,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        common.add_common_args(p)
        importlib.import_module("crtest.cases." + mod).add_args(p)
        p.set_defaults(_mod=mod)
    for code in PENDING:
        p = sub.add_parser(code, help="（留位，未实现）")
        common.add_common_args(p)
        p.set_defaults(_mod="pending", _code=code)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.list or not args.case:
        print("已实现：")
        for code, _, help_text in CASES:
            print("  %-5s %s" % (code, help_text))
        print("留位（未实现）：%s" % (", ".join(PENDING) if PENDING else "无"))
        return 0 if args.list else 2
    mod = importlib.import_module("crtest.cases." + args._mod)
    if args._mod == "pending":
        return mod.run_pending(args._code)
    return common.run_case(args.case, mod, args)


if __name__ == "__main__":
    sys.exit(main())
