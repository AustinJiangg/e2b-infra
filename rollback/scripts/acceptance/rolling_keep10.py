#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滚动保留：多个沙箱长时间并发各自 checkpoint，每个沙箱只保留最近 K 个、超出就删最旧的，
中间随机 restore 回保留集合里的任意一个并逐项验现场。

这是「客户长期挂着沙箱、按固定窗口留最近若干个回滚点」的负载形态，也是隐藏 checkpoint
（被删掉但仍被后代引用的中间层）合并主要针对的形态。checkpoint_concurrent.py 的 D 段
（混合稳态）是随机 create / restore / delete，保留数不受控；这里把 D 段换成滚动版，
其余（建沙箱、现场定义、记录格式、汇总表、宿主机计量、main 的参数与输出）全部复用
同目录的 checkpoint_concurrent.py，不另起一套。

每个沙箱单线程循环，跑满 --soak-seconds 秒：
  1. dirty：覆写内存 blob 前 8 MB + 文件 blob 前 4 MB，写代号标记，sync
  2. checkpoint
  3. 可见 checkpoint 超过 --keep 个时删最旧的
  4. 以 --restore-p 的概率 restore 到保留集合里随机一个，逐项验现场（内存 / 文件代号、
     两个 blob 前 4 MB 的 md5、心跳进程 pid），之后从那里接着拍（下一个 checkpoint 的
     父节点就是这个 restore 目标）
保留数由脚本自己控制，不依赖服务端上限。

检查什么：
  - 正确性：每次 restore 后现场逐项一致（「restore 现场逐项验证：x/y 一致」）；
    任何一次不一致 → 全体停手（abort），出问题的沙箱不 kill，留现场；
    restore RPC 失败的沙箱停止对它的一切操作（加 --keep-on-failure 时留现场并抓
    orchestrator 日志）；结束时每个沙箱还活着（命令能跑、根文件系统能写、心跳在推进）。
  - 资源 / 性能：create / restore 客户端墙钟与服务端分段（frozen / total）的 p50 与最坏值，
    delete 成功数，产物盘写入量、orchestrator CPU 时间与 RSS 的增量（宿主机上跑才有）。

用法（在宿主机上、本目录或任意目录均可）:
    python3 rollback/scripts/acceptance/rolling_keep10.py --soak-sandboxes 16 --soak-seconds 1800 --keep-on-failure
    python3 rollback/scripts/acceptance/rolling_keep10.py --soak-sandboxes 8 --soak-seconds 600 --keep 5 --restore-p 0.5 --out rolling.json
    SPAWN_TIMEOUT=14400 python3 rollback/scripts/acceptance/rolling_keep10.py --soak-sandboxes 16 --soak-seconds 7200 --keep-on-failure
    python3 rollback/scripts/acceptance/rolling_keep10.py --help

参数（本脚本自己的两个，其余原样交给 checkpoint_concurrent.py 的 main）:
    --keep N            每个沙箱保留的 checkpoint 个数，默认 10
    --restore-p P       每轮 restore 的概率（0~1），默认 0.25
    --soak-sandboxes N  并发沙箱数，默认 8（checkpoint_concurrent.py 的默认值）
    --soak-seconds T    跑多久（秒），默认 180（同上）
    --template NAME     模板，默认 base
    --keep-on-failure   restore 失败 / 现场不一致的沙箱不 kill，留现场
    --stages            默认 D（本脚本只替换 D 段；显式写 A,B,D 等会连同原样的 A/B 段一起跑）
    --out PATH          原始数据 JSON，默认当前目录下 rolling-keep<K>-<时间>.json
环境变量:
    SPAWN_TIMEOUT       建沙箱时给的寿命（秒），默认 10800（3 h）。必须长于 --soak-seconds
                        加建沙箱和收尾的时间，否则沙箱到点被回收；worker 发现沙箱已不存在
                        （dirty 报 not found）时退出，不对着死沙箱空转。

依赖（与本目录其它脚本一致）:
    同目录的 checkpoint_concurrent.py（按本文件所在目录导入，从任何工作目录跑都行）
    pip install e2b==2.20.0 python-dotenv
    python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py    # 先铺 SDK 覆盖层
    python /opt/e2b-infra/patch_e2b.py                         # 再改 https → http，顺序不能反

环境变量（可放在当前目录 .env 里）:
    E2B_API_KEY / E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL

输出:
    屏幕：D 段汇总表（create / restore 墙钟与服务端分段）、delete 次数、结束时可用沙箱数、
          逐项验证一致数、是否 abort、失败明细；自己 tee。
    JSON（--out）：与 checkpoint_concurrent.py 的 D 段同格式（stage="D"，
          op=create/restore/delete/alive/dirty/loop/setup/diag），另加 depth=当时的保留数、
          keep_idx=restore 目标在保留集合里的新旧序号（0=最旧）；summary["D"] 里有 keep /
          restore_p / 各类计数 / verified / alive / aborted。
    退出码：D 段有失败或不一致、或某段抛异常时为 1。
"""
import argparse
import os
import random
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checkpoint_concurrent as cc  # noqa: E402

# ---- 本脚本自己的参数：先摘出来，剩下的交给 cc.main() 的 argparse
_own = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
_own.add_argument("--keep", type=int, default=10)
_own.add_argument("--restore-p", type=float, default=0.25)
_opts, _rest = _own.parse_known_args(sys.argv[1:])
KEEP = _opts.keep
RESTORE_P = _opts.restore_p
if KEEP < 1:
    sys.exit("--keep 至少为 1")
if not 0.0 <= RESTORE_P <= 1.0:
    sys.exit("--restore-p 取 0~1")
if "-h" in _rest or "--help" in _rest:
    print("rolling_keep10.py 自己的参数（其余见下方 checkpoint_concurrent.py 的帮助）:\n"
          "  --keep N         每个沙箱保留的 checkpoint 个数，默认 10\n"
          "  --restore-p P    每轮 restore 的概率（0~1），默认 0.25\n"
          "  环境变量 SPAWN_TIMEOUT  建沙箱寿命（秒），默认 10800\n"
          "  本脚本 --stages 默认 D，--out 默认 rolling-keep<K>-<时间>.json\n")
if not any(a == "--stages" or a.startswith("--stages=") for a in _rest):
    _rest += ["--stages", "D"]
if not any(a == "--out" or a.startswith("--out=") for a in _rest):
    _rest += ["--out", "rolling-keep%d-%s.json" % (KEEP, cc.now_tag())]
sys.argv = [sys.argv[0]] + _rest

log = cc.log

# 建沙箱的寿命：spawn 默认 3600 s，跑 1 h 以上时沙箱会被回收，所以单给一个更长的值。
SPAWN_TIMEOUT = int(os.environ.get("SPAWN_TIMEOUT", "10800"))


def _gone(e):
    m = str(e)
    return "not found" in m or "NotFound" in type(e).__name__


def stage_rolling(args, store, meter, results):
    S, T = args.soak_sandboxes, args.soak_seconds
    log("\n===== D'. 滚动保留：%d 个沙箱各自 dirty → checkpoint → 超过 %d 个删最旧 → %.0f%% 概率 restore 到保留集合随机一个并逐项验现场，跑 %d s ====="
        % (S, KEEP, RESTORE_P * 100, T))
    if SPAWN_TIMEOUT < T + 300:
        log("  注意：SPAWN_TIMEOUT=%d s 不比 --soak-seconds=%d s 长出 5 分钟以上，沙箱可能中途被回收"
            % (SPAWN_TIMEOUT, T))
    boxes, errs, spawn_s = cc.spawn(S, args.template, store, "d-", timeout=SPAWN_TIMEOUT)
    log("  建沙箱：%d 成功 %d 失败，%.1f s（寿命 %d s）" % (len(boxes), len(errs), spawn_s, SPAWN_TIMEOUT))
    for e in errs:
        log("    建沙箱失败：%s" % e)
    if not boxes:
        return

    recs = []
    lock = threading.Lock()
    stop = time.monotonic() + T
    abort = threading.Event()

    def add(r):
        r["stage"] = "D"
        with lock:
            recs.append(r)

    def worker(b):
        rng = random.Random(hash(b.label))
        try:
            b.setup()
            b.dirty("g0")
        except Exception as e:      # noqa: BLE001
            add({"op": "setup", "box": b.label, "ok": False, "err": str(e)})
            return
        keep = []                    # 保留集合，按 checkpoint 先后
        r = b.create("g0")
        r["depth"] = 0
        add(r)
        if r.get("ok"):
            keep.append(r["id"])
        gen = 0
        while time.monotonic() < stop and not abort.is_set():
            try:
                # 1. dirty
                gen += 1
                g = "g%d" % gen
                try:
                    b.dirty(g, mem_mb=8, file_mb=4)
                except Exception as e:      # noqa: BLE001
                    add(b.fail({"op": "dirty", "box": b.label, "gen": g, "depth": len(keep)}, "dirty", e))
                    if _gone(e):
                        log("  !! %s 沙箱 %s 已不存在，worker 退出：%s" % (b.label, b.id, str(e)[:160]))
                        return
                    continue
                # 2. checkpoint
                r = b.create(g)
                r["depth"] = len(keep)
                add(r)
                if r.get("ok"):
                    keep.append(r["id"])
                # 3. 超过 KEEP 个删最旧
                while len(keep) > KEEP and not abort.is_set():
                    old = keep.pop(0)
                    d = b.delete(old)
                    d["depth"] = len(keep)
                    add(d)
                # 4. 概率 restore
                if keep and rng.random() < RESTORE_P:
                    idx = rng.randrange(len(keep))
                    r = b.restore(keep[idx])
                    r["depth"] = len(keep)
                    r["keep_idx"] = idx
                    add(r)
                    if r.get("verified") is False:
                        abort.set()
                        b.keep = True
                        log("  !! %s 沙箱 %s restore 后现场不一致，全体停手留现场：%s" % (b.label, b.id, r.get("mismatch")))
                        return
                    if not r.get("ok") and r.get("err_stage", "restore_rpc") == "restore_rpc":
                        b.failed_at = time.time()
                        log("  !! %s 沙箱 %s restore 失败，停止对它的一切操作：%s" % (b.label, b.id, (r.get("err") or "")[:160]))
                        return
            except Exception as e:      # noqa: BLE001
                add(b.fail({"op": "loop", "box": b.label, "depth": len(keep),
                            "trace": traceback.format_exc()[-2000:]}, "loop", e))
                time.sleep(0.5)
                continue
        try:
            ok, d = b.alive()
        except Exception as e:      # noqa: BLE001
            ok, d = False, {"err": "%s: %s" % (type(e).__name__, e)}
        add({"op": "alive", "box": b.label, "ok": ok, "detail": d, "final_keep": len(keep)})

    def worker_guarded(b):
        try:
            worker(b)
        finally:
            last = [o for o in recs if o.get("box") == b.label and o["op"] == "restore" and not o.get("ok")
                    and o.get("err_stage", "restore_rpc") == "restore_rpc"]
            if last and args.keep_on_failure:
                b.keep = True
                diag = {"op": "diag", "box": b.label, "sandbox": b.id, "failed_id": last[-1].get("id"),
                        "console": cc.grab_orchestrator_log(b.id, since_s=180)}
                add(diag)

    before = meter.snapshot()
    ts = [threading.Thread(target=worker_guarded, args=(b,)) for b in boxes]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    d = cc.HostMeter.delta(before, meter.snapshot())
    results["ops"].extend(recs)
    results["host"].append({"stage": "D", "sandboxes": S, "seconds": T, "keep": KEEP, "restore_p": RESTORE_P, **d})

    creates = [r for r in recs if r["op"] == "create"]
    restores = [r for r in recs if r["op"] == "restore"]
    deletes = [r for r in recs if r["op"] == "delete"]
    alive = [r for r in recs if r["op"] == "alive"]
    cc.print_op_table("滚动保留 %d 沙箱 × %d s（keep=%d）" % (S, T, KEEP),
                      [("create", creates), ("restore", restores)], ("frozen", "total"))
    log("  delete：%d 次，%d 成功" % (len(deletes), sum(1 for r in deletes if r.get("ok"))))
    log("  结束时沙箱可用：%d/%d" % (sum(1 for r in alive if r.get("ok")), len(alive)))
    ver = [r for r in restores if "verified" in r]
    log("  restore 现场逐项验证：%d/%d 一致" % (sum(1 for r in ver if r["verified"]), len(ver)))
    log("  abort：%s" % abort.is_set())
    results["summary"]["D"] = {
        "sandboxes": S, "seconds": T, "keep": KEEP, "restore_p": RESTORE_P, "creates": len(creates),
        "creates_ok": sum(1 for r in creates if r.get("ok")),
        "restores": len(restores), "restores_ok": sum(1 for r in restores if r.get("ok")),
        "deletes": len(deletes), "deletes_ok": sum(1 for r in deletes if r.get("ok")),
        "failures_by_stage": {k: sum(1 for r in recs if r.get("err_stage") == k)
                              for k in sorted({r.get("err_stage") for r in recs if r.get("err_stage")})},
        "verified": "%d/%d" % (sum(1 for r in ver if r["verified"]), len(ver)),
        "alive": "%d/%d" % (sum(1 for r in alive if r.get("ok")), len(alive)),
        "aborted": abort.is_set(),
    }
    cc.print_errors(recs)
    kept = [b for b in boxes if getattr(b, "keep", False)]
    for b in boxes:
        if b not in kept:
            b.kill()
    if kept:
        log("  保留未 kill 的沙箱：%s" % ", ".join(b.id for b in kept))


# cc.main() 按名字在模块全局里查 stage_d，替换后 D 段就走滚动版
cc.stage_d = stage_rolling

if __name__ == "__main__":
    sys.exit(cc.main())
