#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空闲之后的 restore：按手册第 16 章 §7 的测法，看 restore（及其前的 checkpoint）是否随空闲时长变慢。

同一沙箱串行。先铺现场、拍 cpA 作目标，然后每一臂循环：
    sleep T 秒 → checkpoint cpB → restore cpA（逐项验现场）→ 删 cpB
T 取 0 / 0.5 / 2 / 5 / 10 秒。另两组对照各拆一个变量：
    a 组（nockpt）：sleep 2 秒后**不拍** cpB，直接 restore cpA，看"restore 之前先 checkpoint 一次"
                    是否掩盖了空闲的影响；
    b 组（busy）  ：guest 里常驻一个 100 ms 一次的忙循环，其余同 T = 2，看"guest 从未真正空闲"
                    时现象是否消失。
`serial_restore.py` 是背靠背连续 restore，没有中间那次 checkpoint，在它外面加 sleep 不等于空闲后 restore。

cpB 用 record_scene=False 拍：取现场要在 guest 里执行命令，会打断空闲。restore 之后的逐项校验
在计时之外。920B 上的同口径结果见手册第 20 章 §3.2。

判据：restore 全部成功、现场全部一致即退出码 0。耗时只记录不判失败（同手册第 20 章 §1.1 的口径）。

用法：
    python3 bench/idle_restore.py --outdir /tmp/idle                  # 默认每臂 30 次（10 秒那一臂 20 次），约 15 分钟
    python3 bench/idle_restore.py --reps 3 --arms 0,2,nockpt --outdir /tmp/idle
"""

import argparse
import json
import os
import sys
import time

CRTEST_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CRTEST_PARENT not in sys.path:
    sys.path.insert(0, CRTEST_PARENT)

from crtest import common                                    # noqa: E402
from crtest.common import log                                # noqa: E402

# 臂名 → (sleep 秒, 是否先拍 cpB, guest 是否常驻忙循环, 默认次数)
ARMS = {
    "0": (0.0, True, False, 30),
    "0.5": (0.5, True, False, 30),
    "2": (2.0, True, False, 30),
    "5": (5.0, True, False, 30),
    "10": (10.0, True, False, 20),
    "nockpt": (2.0, False, False, 30),
    "busy": (2.0, True, True, 30),
}
ARM_TITLE = {"nockpt": "空闲 2 s 后不拍 cpB 直接 restore", "busy": "guest 内常驻忙循环（空闲 2 s）"}
# 先写成文件再起：Box.bg 用单引号包命令，命令本身不能再带单引号
BUSY_SCRIPT = "while :; do i=0; while [ $i -lt 200 ]; do i=$((i+1)); done; sleep 0.1; done\n"
BUSY = "sh /tmp/idle-busy.sh"


def run(ctx):
    a = ctx.args
    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    for x in arms:
        if x not in ARMS:
            raise common.Unmet("不认识的臂 %s" % x, "可选：%s" % ",".join(ARMS))
    outdir = a.outdir or os.getcwd()
    os.makedirs(outdir, exist_ok=True)
    jsonl_path = os.path.join(outdir, "idle-restore-%s.jsonl" % common.now_tag())
    fh = open(jsonl_path, "a", buffering=1)
    ctx.results["meta"]["jsonl"] = jsonl_path
    ctx.results["meta"]["arms"] = arms
    log("原始数据：%s" % jsonl_path)

    # 前一项刚重启过 orchestrator 时 API 节点要一两分钟才接单，建沙箱在 --wait-ready 秒内重试
    deadline = time.monotonic() + a.wait_ready
    while True:
        try:
            sbx = common.sandbox_create(a.template, private=not a.public, timeout=7200)
            break
        except Exception as e:      # noqa: BLE001
            if time.monotonic() > deadline:
                raise
            log("  建沙箱失败（%s），5 s 后重试" % str(e)[:80])
            time.sleep(5)
    box = common.Box(sbx, ctx.store, "ir")
    common._ALL_BOXES.append(box)
    log("沙箱 %s" % box.id)
    box.setup(warm_mem=a.mem_mb * 2, warm_file=a.file_mb * 2)
    box.dirty("gA", mem_mb=a.mem_mb, file_mb=a.file_mb)
    crec = box.create("gA")
    if not crec.get("ok"):
        raise common.Failed("目标 checkpoint cpA", "成功", str(crec.get("error")), "开跑前")
    cpa = crec["id"]
    log("目标 checkpoint cpA %s（mem_mode=%s）" % (cpa, crec.get("mem_mode")))
    ctx.stage("IDLE-RESTORE", arms=arms)

    bad = 0
    per = {}
    k = 0
    busy_pid = None
    for arm in arms:
        idle, with_cpb, busy, n_default = ARMS[arm]
        n = a.reps or n_default
        if busy and busy_pid is None:
            box.put("/tmp/idle-busy.sh", BUSY_SCRIPT)
            busy_pid = box.bg(BUSY, "/tmp/idle-busy.pid")
            log("  guest 忙循环 pid %s" % busy_pid)
        if not busy and busy_pid is not None:
            box.sig(busy_pid, "KILL")
            busy_pid = None
        log("== 臂 %s：%s，%d 次 ==" % (arm, ARM_TITLE.get(arm, "空闲 %s s" % arm), n))
        rows = per.setdefault(arm, {"restore": [], "cpb": []})
        for rep in range(1, n + 1):
            k += 1
            time.sleep(idle)
            rec = {"arm": arm, "idle_s": idle, "rep": rep, "t": time.time()}
            cpb = None
            if with_cpb:
                cr = box.create("b%d" % k, record_scene=False)
                rec["cpb_ok"] = cr.get("ok")
                rec["cpb_wall_s"] = cr.get("wall_s")
                rec["cpb_phases"] = cr.get("phases")
                if cr.get("ok"):
                    cpb = cr["id"]
                    rows["cpb"].append(cr["wall_s"] * 1000.0)
                else:
                    bad += 1
                    log("  !! cpB 失败：%s" % cr.get("error"))
            rr = box.restore(cpa, verify=True)
            rec.update({"ok": rr.get("ok"), "wall_s": rr.get("wall_s"), "verified": rr.get("verified"),
                        "mismatch": rr.get("mismatch"), "phases": rr.get("phases")})
            if not rr.get("ok") or rr.get("verified") is False:
                bad += 1
                log("  !! restore 失败或现场不一致：%s %s" % (rr.get("error"), rr.get("mismatch")))
            elif rr.get("wall_s") is not None:
                rows["restore"].append(rr["wall_s"] * 1000.0)
            if cpb:
                dr = box.delete(cpb)
                if not dr.get("ok"):
                    bad += 1
                    log("  !! 删 cpB 失败：%s" % dr.get("error"))
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            ctx.results["ops"].append(rec)
        log("  restore p50 %s ms；cpB p50 %s ms"
            % (common.fmt_ms(common.p50(rows["restore"])), common.fmt_ms(common.p50(rows["cpb"]))))
    fh.close()
    box.delete(cpa)
    box.kill()
    common.forget(box)

    summ = {}
    log("")
    log("== 空闲之后的 restore（客户端墙钟，ms） ==")
    log("  臂        n    restore p50   > 100 ms   cpB p50")
    for arm in arms:
        xs, cs = per[arm]["restore"], per[arm]["cpb"]
        over = sum(1 for x in xs if x > 100.0)
        summ[arm] = {"n": len(xs), "restore_p50_ms": common.p50(xs), "restore_max_ms": common.pmax(xs),
                     "over_100ms": over, "cpb_p50_ms": common.p50(cs)}
        log("  %-8s %3d    %-11s   %2d / %-3d   %s" % (arm, len(xs), common.fmt_ms(common.p50(xs)), over,
                                                     len(xs), common.fmt_ms(common.p50(cs)) if cs else "——"))
    ctx.results["summary"]["IDLE-RESTORE"] = {"by_arm": summ, "bad": bad}
    common.expect(ctx, "cpB、restore、删除全部成功且现场一致", bad == 0, "0 次异常", "%d 次异常" % bad,
                  "空闲后 restore")


def main():
    ap = argparse.ArgumentParser(description="空闲之后的 restore（手册第 16 章 §7 的测法）")
    common.add_common_args(ap)
    ap.add_argument("--arms", default="0,0.5,2,5,10,nockpt,busy",
                    help="要跑的臂，逗号分隔（默认全部：0,0.5,2,5,10,nockpt,busy）")
    ap.add_argument("--reps", type=int, default=0, help="每臂次数；0 = 用默认（10 秒那一臂 20 次，其余 30 次）")
    ap.add_argument("--mem-mb", type=int, default=16, help="cpA 现场的内存脏量 MB（默认 16）")
    ap.add_argument("--file-mb", type=int, default=4, help="cpA 现场的文件量 MB（默认 4）")
    ap.add_argument("--wait-ready", type=float, default=300.0,
                    help="建沙箱失败时重试多久（秒，默认 300）：前一项刚重启过 orchestrator 时用得上")
    ap.add_argument("--outdir", default=None, help="产物落点（默认当前目录）")
    args = ap.parse_args()
    if not args.out:
        args.out = os.path.join(args.outdir or os.getcwd(), "idle-restore-%s.json" % common.now_tag())
    shim = type("M", (), {"run": staticmethod(run)})
    return common.run_case("IDLE-RESTORE", shim, args)


if __name__ == "__main__":
    sys.exit(main())
