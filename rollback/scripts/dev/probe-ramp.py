#!/usr/bin/env python3
"""连打劣化到底是"链变深"还是"写得多"？

现象（见 耗时基准结论.md 第五节）：连续 checkpoint 60 秒，单次耗时平滑爬升，
ext4 ×2.8、XFS ×1.7。问题是在那个测法里"代数"和"累计写入量"是同步增长的，
分不开。

这里用一个判别式设计：同样做 N 次 checkpoint，但把每次的脏页量拉开
（0MB / 32MB / 256MB）。于是

  · 脏页 0MB   —— 代数长得快，字节写得少
  · 脏页 256MB —— 代数长得慢，字节写得多

如果爬升是**代数**驱动的，三条曲线按"第几代"对齐时应当重合；
如果是**写入量**驱动的，按"累计写了多少 GB"对齐时才重合。
两种归一化各算一遍斜率，看哪一种把三条曲线收拢，就是哪一个。

用法: probe-ramp.py <xfs|ext4> [--runs 0:400,32:400,256:150] [--out DIR]
"""
import json
import os
import sys
import time

import lib

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEME = "ext4"
RUNS = [(0, 400), (32, 400), (256, 150)]
OUT = None

argv = sys.argv[1:]
i = 0
while i < len(argv):
    a = argv[i]
    if a == "--runs":
        RUNS = [tuple(int(y) for y in x.split(":")) for x in argv[i + 1].split(",")]; i += 2
    elif a == "--out":
        OUT = argv[i + 1]; i += 2
    elif a.startswith("--"):
        sys.exit("未知参数 %s\n%s" % (a, __doc__))
    else:
        SCHEME = a; i += 1

STAMP = time.strftime("%Y%m%d-%H%M%S")
OUT = OUT or os.path.join(HERE, "reports", "ramp-%s-%s" % (SCHEME, STAMP))
os.makedirs(OUT, exist_ok=True)
MOUNT = lib.sh("findmnt -no TARGET --target %s" % lib.STORE) or "/"


def log(m=""):
    print(m, flush=True)


def df_b(p):
    o = lib.sh("df -B1 --output=used %s 2>/dev/null | tail -1" % p)
    return int(o) if o.isdigit() else -1


def p50(xs):
    s = sorted(xs)
    return s[len(s) // 2] if s else float("nan")


def retry(fn, what, tries=3):
    """长跑里偶尔会撞上 RemoteProtocolError（连接被对端掐掉）。
    一次瞬时错误不该让整轮几百次的数据白跑，重试并把重试次数记下来。"""
    last = None
    for i in range(tries):
        try:
            return fn(), i
        except Exception as e:
            last = e
            log("    ⚠ %s 第 %d 次失败：%s" % (what, i + 1, type(e).__name__))
            time.sleep(2)
    raise last


def entry_breakdown(sid, ck_id):
    """一个 checkpoint 目录里各文件多大。用来看深链上到底是哪个文件在长。"""
    d = os.path.join(lib.STORE, sid, ck_id)
    out = {}
    for line in lib.sh("ls -lA --block-size=1 %s 2>/dev/null" % d).splitlines():
        f = line.split()
        if len(f) >= 9 and f[0][0] == "-":
            out[f[-1]] = int(f[4])
    return out


def one_run(dirty_mb, count):
    log("\n===== 脏页 %d MB × %d 次 =====" % (dirty_mb, count))
    sbx = lib.Sandbox.create(template="base", timeout=3600)
    sid = sbx.sandbox_id
    log("  sandbox %s  backend=%s" % (sid, lib.dirty_tracking(sid)))
    rows, retries, ids = [], 0, []
    try:
        if dirty_mb:
            sbx.commands.run("dd if=/dev/urandom of=/dev/shm/src bs=1M count=%d 2>/dev/null"
                             % dirty_mb, timeout=600)
        u0 = df_b(MOUNT)
        for n in range(count):
            if dirty_mb:
                retry(lambda: sbx.commands.run(
                    "dd if=/dev/shm/src of=/dev/shm/blob bs=1M count=%d conv=notrunc 2>/dev/null"
                    % dirty_mb, timeout=300), "弄脏")
            t0 = time.monotonic()
            ck, r = retry(lambda: sbx.checkpoint.create(name="g%d" % n), "checkpoint")
            dt = time.monotonic() - t0
            retries += r
            ids.append(ck.checkpoint_id)
            rows.append({"gen": n + 1, "t": round(dt, 4), "cum_b": df_b(MOUNT) - u0,
                         "retried": r})
            if (n + 1) % 50 == 0:
                log("    %3d/%d  最近 50 次 p50=%.4fs  累计 %.1f GB"
                    % (n + 1, count, p50([r2["t"] for r2 in rows[-50:]]),
                       rows[-1]["cum_b"] / 1073741824.0))
        # 杀沙箱之前把目录量下来 —— OnRemove 会连 store 一起清掉
        first = entry_breakdown(sid, ids[0]) if ids else {}
        last = entry_breakdown(sid, ids[-1]) if ids else {}
        layers = lib.sh("du -sB1 %s 2>/dev/null | cut -f1"
                        % os.path.join(lib.STORE, sid, "layers"))
    finally:
        try:
            sbx.kill()
        except Exception:
            pass
    return {"dirty_mb": dirty_mb, "count": len(rows), "rows": rows, "retries": retries,
            "first_entry": first, "last_entry": last,
            "layers_dir_b": int(layers) if layers.isdigit() else -1}


def analyse(r):
    rows = r["rows"]
    n = len(rows)
    k = max(1, n // 10)
    first = p50([x["t"] for x in rows[:k]])
    last = p50([x["t"] for x in rows[-k:]])
    gens = rows[-1]["gen"] - rows[k // 2]["gen"]
    gb = (rows[-1]["cum_b"] - rows[k // 2]["cum_b"]) / 1073741824.0
    r["first_decile_p50"] = round(first, 4)
    r["last_decile_p50"] = round(last, 4)
    r["delta_s"] = round(last - first, 4)
    r["ratio"] = round(last / first, 2) if first else None
    r["total_gb"] = round(rows[-1]["cum_b"] / 1073741824.0, 2)
    # 两种归一化
    r["ms_per_gen"] = round(1000.0 * (last - first) / gens, 4) if gens else None
    r["ms_per_gb"] = round(1000.0 * (last - first) / gb, 1) if gb > 0.01 else None
    r["deciles"] = []
    for d in range(10):
        seg = rows[d * n // 10:(d + 1) * n // 10]
        if seg:
            r["deciles"].append({"gen_mid": seg[len(seg) // 2]["gen"],
                                 "cum_gb": round(seg[-1]["cum_b"] / 1073741824.0, 2),
                                 "p50": round(p50([x["t"] for x in seg]), 4)})
    return r


def main():
    if not lib.wait_for_api():
        log("API 没就绪"); return 1
    log("方案 %s   store %s (%s)   挂载点 %s" % (SCHEME, lib.STORE, lib.store_fstype(), MOUNT))
    log("判别式：把脏页量拉开，看爬升跟'代数'走还是跟'写入量'走")
    res = {"scheme": SCHEME, "stamp": STAMP, "store_fs": lib.store_fstype(), "runs": []}
    for dirty, count in RUNS:
        try:
            res["runs"].append(analyse(one_run(dirty, count)))
        except Exception as e:
            log("  ⚠ 脏页 %d MB 这一跑挂了：%s: %s —— 已有的结果照常汇总"
                % (dirty, type(e).__name__, e))
        # 每跑完一档就落一次盘，后面挂了也不至于全丢
        with open(os.path.join(OUT, "ramp.json"), "w") as f:
            json.dump(res, f, indent=1, ensure_ascii=False)
    if not res["runs"]:
        log("一档都没跑成"); return 1

    log("\n########## 判别 ##########")
    log("%-10s %6s %10s %10s %8s %10s %12s %10s"
        % ("脏页", "次数", "首10% p50", "末10% p50", "倍数", "累计GB", "每代 ms", "每GB ms"))
    for r in res["runs"]:
        log("%-10s %6d %10.4f %10.4f %8s %10.2f %12s %10s"
            % ("%d MB" % r["dirty_mb"], r["count"], r["first_decile_p50"], r["last_decile_p50"],
               r["ratio"], r["total_gb"], r["ms_per_gen"], r["ms_per_gb"]))

    gens = [r["ms_per_gen"] for r in res["runs"] if r["ms_per_gen"] is not None]
    gbs = [r["ms_per_gb"] for r in res["runs"] if r["ms_per_gb"] is not None]

    def spread(xs):
        return max(xs) / min(xs) if xs and min(xs) > 0 else float("inf")

    sg, sb = spread(gens), spread(gbs)
    res["spread_per_gen"] = round(sg, 2)
    res["spread_per_gb"] = round(sb, 2)
    log("\n按'每代'归一化，三档之间相差 %.2f 倍" % sg)
    log("按'每GB'归一化，三档之间相差 %.2f 倍" % sb)
    if sg < sb / 2:
        res["verdict"] = "depth"
        log("\n判定：**跟着代数走** —— 按每代归一化时三档收拢得多。"
            "\n      成本随链/层栈的深度增长，跟写了多少字节关系不大。")
    elif sb < sg / 2:
        res["verdict"] = "bytes"
        log("\n判定：**跟着写入量走** —— 按每GB归一化时三档收拢得多。"
            "\n      成本随累计写入量增长（回写/空间/碎片），跟链有多深关系不大。")
    else:
        res["verdict"] = "mixed"
        log("\n判定：**两者都有**，两种归一化都没能把三档收拢，不能只归因于一个。")

    log("\n########## 深链上是哪个文件在长 ##########")
    for r in res["runs"]:
        log("\n--- 脏页 %d MB（第 1 代 → 第 %d 代）---" % (r["dirty_mb"], r["count"]))
        keys = sorted(set(r["first_entry"]) | set(r["last_entry"]))
        for k in keys:
            a, b = r["first_entry"].get(k, 0), r["last_entry"].get(k, 0)
            mark = "  ← 长了 %.0f 倍" % (b / a) if a and b / a > 1.5 else ""
            log("  %-22s %12d → %12d B%s" % (k, a, b, mark))
        log("  %-22s %12s   %12d B" % ("layers/ 目录合计", "", r["layers_dir_b"]))
        if r["retries"]:
            log("  （本跑重试过 %d 次）" % r["retries"])

    for r in res["runs"]:
        log("\n--- 脏页 %d MB 的十段展开 ---" % r["dirty_mb"])
        log("  %8s %10s %10s" % ("到第几代", "累计GB", "p50"))
        for d in r["deciles"]:
            log("  %8d %10.2f %10.4f" % (d["gen_mid"], d["cum_gb"], d["p50"]))

    with open(os.path.join(OUT, "ramp.json"), "w") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    log("\n写出 %s/ramp.json" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
