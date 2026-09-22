# -*- coding: utf-8 -*-
"""
T25 时间与 CPU 健康（方案 §4.2）。

restore 之后 guest 里的时间和 CPU 得是健康的：

  · `/proc/uptime` **倒退**到快照那一刻 —— 这是既定语义（09-17 E1 已实测 10/10 次
    倒退），不是缺陷，所以它是"必须倒退"的断言，不是"不许倒退"；
  · `date`（CLOCK_REALTIME）由 envd 对齐，不许倒退，且与宿主机差 < 5 s；
  · `sleep 1` 实测 1.0 ± 0.1 s —— 定时器坏了最先在这里露头；
  · restore 之后 10 s 窗口内 CPU0 空闲 ≥ 90%；
  · arch_timer 速率 < 200 次/s —— 09-17 诊断里"guest 命令卡 3–24 s 伴随 CPU0
    定时器中断风暴"（S2）就是靠这一条捉。

每轮采三次：restore 前、restore 刚回来、再过 10 s。轮与轮之间等 3 s。
"""

NAME = "T25"

from .. import common
from ..common import expect, log, note

# guest 里的采样器：一次 run 拿全部五项。最后那个 sleep 1 放在最后，
# 免得它的 1 秒把 CPU / 中断计数的采样点推后。
SAMPLER = r'''
import time

print('mono=%.6f' % time.monotonic())
print('epoch=%.6f' % time.time())
print('uptime=%s' % open('/proc/uptime').read().split()[0])
st = open('/proc/stat').read().splitlines()
print('cpu=%s' % st[0])
for l in st:
    if l.startswith('cpu0 '):
        print('cpu0=%s' % l)
ti = [l.strip() for l in open('/proc/interrupts') if 'arch_timer' in l]
print('timer=%s' % '|'.join(ti))
t = time.monotonic()
time.sleep(1)
print('sleep1=%.4f' % (time.monotonic() - t))
'''

SAMPLER_PATH = "/dev/shm/t25_sample.py"


def add_args(ap):
    ap.add_argument("--rounds", type=int, default=10, help="restore 轮数（默认 10）")
    ap.add_argument("--settle", type=float, default=10.0,
                    help="restore 后到第二次采样的间隔秒数（默认 10）")
    ap.add_argument("--gap", type=float, default=3.0, help="轮与轮之间等待秒数（默认 3）")
    return ap


def sample(box, timeout=120):
    """跑一次采样器，解析成 judge_t25 认的结构。"""
    out = box.run("python3 %s" % SAMPLER_PATH, timeout=timeout)
    kv = common.parse_kv(out)
    d = {"raw": kv}
    for k in ("mono", "epoch", "uptime", "sleep1"):
        try:
            d[k] = float(kv.get(k))
        except (TypeError, ValueError):
            d[k] = None
    d["cpu"] = common.parse_proc_stat_line(kv.get("cpu", ""))
    d["cpu0"] = common.parse_proc_stat_line(kv.get("cpu0", ""))
    d["timer"] = common.parse_interrupt_counts(kv.get("timer", ""))
    return d


def run(ctx):
    import time

    a = ctx.args
    box = common.spawn(ctx, 1, "t25-")[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)
    box.put(SAMPLER_PATH, SAMPLER)
    box.dirty("g0", mem_mb=16, file_mb=4)

    rec = ctx.op(box.create("g0"), stage="T25", step="cp0")
    expect(ctx, "建 checkpoint", rec.get("ok"), "成功", rec.get("err") or "ok",
           "T25 前置：得有一个能回的 checkpoint")
    cp0 = rec["id"]

    for r in range(a.rounds):
        log("\n  --- 第 %d/%d 轮 ---" % (r + 1, a.rounds))
        before = sample(box)
        rr = ctx.op(box.restore(cp0, verify=False), stage="T25", round=r)
        expect(ctx, "第 %d 轮 restore 成功" % (r + 1), rr.get("ok"), "成功",
               rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
               "T25 前置；失败时现场留在 %s" % box.store_dir(cp0))
        host_epoch = time.time()
        after = sample(box)
        time.sleep(max(0.0, a.settle - 1.0))      # 采样器自己占 1 s（sleep 1 那项）
        later = sample(box)

        ctx.op({"op": "sample", "box": box.label, "sandbox": box.id, "round": r,
                "stage": "T25", "before": before["raw"], "after": after["raw"],
                "later": later["raw"], "host_epoch": host_epoch,
                "frozen_ms": (rr.get("phases") or {}).get("frozen")})

        for name, ok, want, got in common.judge_t25(before, after, later, host_epoch):
            expect(ctx, "第 %d 轮 %s" % (r + 1, name), ok, want, got,
                   "方案 §4.2 T25；09-17 E1 的既定时间语义")

        now = box.scene()
        want = box.scenes.get(cp0, {})
        bad = {k: (want.get(k), now.get(k)) for k in want if now.get(k) != want.get(k)}
        expect(ctx, "第 %d 轮现场回到 cp0" % (r + 1), not bad, "五项全同",
               bad or "全同", "现场定义同 checkpoint_concurrent.py（并发报告 §4）")

        if r + 1 < a.rounds:
            time.sleep(a.gap)

    walls = [o["wall_s"] for o in ctx.results["ops"]
             if o.get("op") == "restore" and o.get("ok")]
    frozen = [(o.get("phases") or {}).get("frozen") for o in ctx.results["ops"]
              if o.get("op") == "restore" and o.get("ok")]
    note(ctx, "restore 客户端耗时 p50/max", "仅记录",
         "%s / %s s" % (common.fmt_s(common.p50(walls)), common.fmt_s(common.pmax(walls))),
         "对照 checkpoint_bench_v2.py")
    note(ctx, "服务端 frozen p50/max", "仅记录",
         "%s / %s ms" % (common.fmt_ms(common.p50(frozen)), common.fmt_ms(common.pmax(frozen))),
         "last-restore-timings.json")
    ctx.results["summary"]["T25"] = {
        "rounds": a.rounds,
        "restore_wall_p50_s": common.p50(walls),
        "frozen_p50_ms": common.p50(frozen),
    }
