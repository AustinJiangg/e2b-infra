# -*- coding: utf-8 -*-
"""
T26 串口（方案 §4.2 / 第 1 轮 F1 回归）。

已确诊的缺陷：rollback 只调 `emulate_serial_init()`，它整寄存器覆盖 IER=0x01，
清掉 THRE 使能，而 IIR 的残留位只能靠 guest 读清 —— guest 进不去 ISR，于是
**restore 之后 guest 往 /dev/ttyS0 写就永久卡死**（并发报告 §6.7 发现，方案 S1
定位，KASandbox jll 178c99a 修）。

判定就一句话：restore 之后，guest 里
`timeout 5 python3 -c 'open("/dev/ttyS0","wb").write(b"x"*4096)'`
退出码 0 且用时 < 3 s。restore 前也测一次作对照（restore 前本来就该是好的，
它要是就卡了，说明问题不在回滚路径上，得先查环境）。

宿主机侧那半（FC stdout 里收没收到这 4 KB）不做：FC 的 stdout 被 orchestrator
接管并混进 nomad 的轮转日志，按沙箱切出来只能靠 grep 全量日志，既不可靠又会把
别人的沙箱日志一起翻出来。需求里也写了"不可行就不读"。
"""

NAME = "T26"

from .. import common
from ..common import expect, log, note

WRITER_PATH = "/dev/shm/t26_write.py"
# 4 KB —— 8250 的发送 FIFO 装不下，必须靠 THRE 中断一轮轮推完，正好压 F1 那条路。
WRITER = r'''
import sys

with open('/dev/ttyS0', 'wb', buffering=0) as f:
    f.write(b'x' * 4096)
    f.flush()
print('wrote')
'''


def add_args(ap):
    ap.add_argument("--rounds", type=int, default=20, help="restore 轮数（默认 20）")
    ap.add_argument("--limit-ms", type=float, default=3000.0,
                    help="判定用的耗时上限，毫秒（默认 3000）")
    ap.add_argument("--gap", type=float, default=1.0, help="轮与轮之间等待秒数（默认 1）")
    return ap


def serial_write(box, timeout=60):
    """在 guest 里写 4 KB 串口，返回 (退出码, 毫秒)。外面套 `timeout 5`：
    卡死的时候要的是"5 秒后带非 0 退出码回来"，不是把 SDK 的命令超时耗光。"""
    rc, out = box.sh(
        "S=$(date +%%s%%N); timeout 5 python3 %s >/dev/null 2>&1; RC=$?; E=$(date +%%s%%N); "
        "echo rc=$RC; echo ms=$(( (E-S)/1000000 ))" % WRITER_PATH, timeout=timeout)
    kv = common.parse_kv(out)
    try:
        return int(kv.get("rc")), float(kv.get("ms"))
    except (TypeError, ValueError):
        return -1, -1.0


def run(ctx):
    import time

    a = ctx.args
    box = common.spawn(ctx, 1, "t26-")[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)
    box.put(WRITER_PATH, WRITER)
    box.dirty("g0", mem_mb=16, file_mb=4)

    rc, ms = serial_write(box)
    ctx.op({"op": "serial", "stage": "T26", "when": "before-any-checkpoint",
            "box": box.label, "sandbox": box.id, "rc": rc, "ms": ms, "ok": rc == 0})
    expect(ctx, "基线：没做过快照时串口可写", rc == 0 and ms < a.limit_ms,
           "退出码 0 且 < %.0f ms" % a.limit_ms, "退出码 %d，%.0f ms" % (rc, ms),
           "T26 前置：基线都写不动说明不是回滚的问题")

    crec = ctx.op(box.create("g0"), stage="T26", step="cp0")
    expect(ctx, "建 checkpoint", crec.get("ok"), "成功", crec.get("err") or "ok", "T26 前置")
    cp0 = crec["id"]

    before_ms, after_ms = [], []
    for r in range(a.rounds):
        rc, ms = serial_write(box)
        before_ms.append(ms)
        ctx.op({"op": "serial", "stage": "T26", "when": "before-restore", "round": r,
                "box": box.label, "sandbox": box.id, "rc": rc, "ms": ms, "ok": rc == 0})
        expect(ctx, "第 %d 轮 restore 前串口可写" % (r + 1), rc == 0 and ms < a.limit_ms,
               "退出码 0 且 < %.0f ms" % a.limit_ms, "退出码 %d，%.0f ms" % (rc, ms),
               "方案 §4.2 T26（对照组）")

        rr = ctx.op(box.restore(cp0, verify=False), stage="T26", round=r)
        expect(ctx, "第 %d 轮 restore 成功" % (r + 1), rr.get("ok"), "成功",
               rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
               "T26 前置；失败现场 %s" % box.store_dir(cp0))

        rc, ms = serial_write(box)
        after_ms.append(ms)
        ctx.op({"op": "serial", "stage": "T26", "when": "after-restore", "round": r,
                "box": box.label, "sandbox": box.id, "rc": rc, "ms": ms, "ok": rc == 0})
        expect(ctx, "第 %d 轮 restore 后串口可写" % (r + 1), rc == 0 and ms < a.limit_ms,
               "退出码 0 且 < %.0f ms" % a.limit_ms,
               "退出码 %d，%.0f ms%s" % (rc, ms, "（124 = timeout 杀的，就是卡死）"
                                        if rc == 124 else ""),
               "方案 S1 / KASandbox jll 178c99a（F1）；缺陷见并发报告 §6.7")

        if r + 1 < a.rounds:
            time.sleep(a.gap)

    ok, d = box.alive()
    expect(ctx, "%d 轮之后沙箱仍可用" % a.rounds, ok, "命令能跑、能写盘、心跳在推进", d,
           "活体判据同 checkpoint_verify.py")
    note(ctx, "串口写 4 KB 耗时 p50（前/后）", "仅记录",
         "%s / %s ms" % (common.fmt_ms(common.p50(before_ms)),
                         common.fmt_ms(common.p50(after_ms))), "T26 原始数据")
    ctx.results["summary"]["T26"] = {
        "rounds": a.rounds,
        "before_p50_ms": common.p50(before_ms), "after_p50_ms": common.p50(after_ms),
        "after_max_ms": common.pmax(after_ms),
    }
