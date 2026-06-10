#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E2B 沙箱启动耗时压测脚本。

通过 e2b Python SDK 连续创建 N 个沙箱（默认 100 个），触发 orchestrator 端
[ResumeSandbox] 各阶段耗时日志，同时记录客户端整体耗时作为交叉参考。

服务端阶段耗时需要在压测结束后采集 orchestrator 日志，
再用 parse_report.py 解析生成统计报告。

依赖（与 docs/zh/usage.md 的客户端环境一致）:
    pip install e2b==2.20.0 python-dotenv
    python3 /opt/e2b-infra/patch_e2b.py   # 自部署环境 https->http 补丁

环境变量（可放在当前目录 .env 中）:
    E2B_API_KEY / E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL

用法示例:
    python3 run_benchmark.py --template base --count 100 --concurrency 1
"""

import argparse
import csv
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta


def now_dt():
    return datetime.now().astimezone()


def iso(dt):
    return dt.isoformat(timespec="milliseconds")


def percentile(sorted_vals, p):
    """最近秩百分位。sorted_vals 必须已升序排序。"""
    if not sorted_vals:
        return None
    k = max(1, math.ceil(p / 100.0 * len(sorted_vals)))
    return sorted_vals[k - 1]


def parse_args():
    ap = argparse.ArgumentParser(
        description="E2B 沙箱启动耗时压测（客户端）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--template", default="base", help="模板名称/ID")
    ap.add_argument("--count", type=int, default=100, help="正式测试的沙箱数量")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="并发创建数（1=串行，与参考报告的节奏一致）")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="两次提交创建请求之间的间隔秒数（限速用）")
    ap.add_argument("--warmup", type=int, default=3,
                    help="预热沙箱数量（不计入统计，用于热缓存/NBD/网络池）")
    ap.add_argument("--sandbox-timeout", type=int, default=300,
                    help="沙箱自动过期时间（秒），防止 kill 失败造成泄漏")
    ap.add_argument("--kill-each", action="store_true",
                    help="每个沙箱创建成功后立即 kill（默认全部创建完后统一 kill，"
                         "与参考测试『100 个沙箱同时存活』的形态一致）")
    ap.add_argument("--keep", action="store_true",
                    help="测试结束后保留沙箱不 kill（依赖 --sandbox-timeout 自动过期）")
    ap.add_argument("--outdir", default="bench-out", help="结果输出目录")
    return ap.parse_args()


def main():
    args = parse_args()

    # 加载 .env（与 usage.md 客户端配置方式一致）；未装 dotenv 时静默跳过
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        from e2b import Sandbox
    except ImportError:
        print("错误: 未安装 e2b SDK。请先执行: pip install e2b==2.20.0", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.outdir, exist_ok=True)

    run_id = now_dt().strftime("bench-%Y%m%d-%H%M%S")
    lock = threading.Lock()
    results = []
    alive = []  # 待统一 kill 的沙箱对象

    def create_one(idx, is_warmup):
        rec = {
            "index": idx,
            "warmup": is_warmup,
            "ok": False,
            "sandbox_id": None,
            "client_start": iso(now_dt()),
            "client_end": None,
            "client_ms": None,
            "error": None,
        }
        t0 = time.monotonic()
        try:
            sbx = Sandbox.create(
                args.template,
                timeout=args.sandbox_timeout,
                metadata={"bench_run": run_id, "bench_idx": str(idx)},
            )
            rec["client_ms"] = round((time.monotonic() - t0) * 1000, 1)
            rec["ok"] = True
            rec["sandbox_id"] = getattr(sbx, "sandbox_id", None) or getattr(sbx, "id", None)
            if is_warmup or args.kill_each:
                try:
                    sbx.kill()
                except Exception:
                    pass
            else:
                with lock:
                    alive.append(sbx)
        except Exception as e:  # noqa: BLE001 - 压测中单个失败要继续
            rec["client_ms"] = round((time.monotonic() - t0) * 1000, 1)
            rec["error"] = repr(e)
        rec["client_end"] = iso(now_dt())
        with lock:
            results.append(rec)
        tag = "warmup" if is_warmup else "bench"
        status = "ok" if rec["ok"] else f"FAIL {rec['error']}"
        print(f"[{tag} {idx:>3}] {rec['client_ms']:>8.1f} ms  id={rec['sandbox_id']}  {status}",
              flush=True)
        return rec

    print(f"== 压测开始: run_id={run_id} template={args.template} "
          f"count={args.count} concurrency={args.concurrency} warmup={args.warmup}")

    # ---- 预热（串行，立即 kill，不计入统计窗口）----
    for i in range(args.warmup):
        create_one(i, True)
    if args.warmup:
        time.sleep(2)  # 与正式窗口隔开，避免预热日志混入统计

    # ---- 正式压测 ----
    bench_start = now_dt()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = []
        for i in range(args.count):
            futures.append(pool.submit(create_one, i, False))
            if args.interval > 0:
                time.sleep(args.interval)
        for f in as_completed(futures):
            f.result()
    bench_end = now_dt()

    # ---- 统一 kill ----
    killed = failed_kill = 0
    if not args.keep and alive:
        print(f"== 清理 {len(alive)} 个沙箱 ...")
        with ThreadPoolExecutor(max_workers=8) as pool:
            def kill_one(s):
                try:
                    s.kill()
                    return True
                except Exception:
                    return False
            for ok in pool.map(kill_one, alive):
                if ok:
                    killed += 1
                else:
                    failed_kill += 1
        print(f"== 清理完成: killed={killed} failed={failed_kill}"
              + ("（失败的会在 timeout 后自动过期）" if failed_kill else ""))

    # ---- 客户端统计 ----
    bench_ok = sorted(r["client_ms"] for r in results if not r["warmup"] and r["ok"])
    bench_fail = sum(1 for r in results if not r["warmup"] and not r["ok"])
    meta = {
        "run_id": run_id,
        "template": args.template,
        "count": args.count,
        "concurrency": args.concurrency,
        "interval": args.interval,
        "warmup": args.warmup,
        "bench_window_since": iso(bench_start - timedelta(seconds=2)),
        "bench_window_until": iso(bench_end + timedelta(seconds=10)),
        "client_ok": len(bench_ok),
        "client_fail": bench_fail,
    }

    json_path = os.path.join(args.outdir, f"{run_id}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "results": results}, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(args.outdir, f"{run_id}.client_times.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    print()
    print("== 客户端整体耗时统计（包含 API/网络/鉴权/envd 等，注意与服务端阶段耗时口径不同）")
    if bench_ok:
        print(f"   成功 {len(bench_ok)} / 失败 {bench_fail}")
        print(f"   min={bench_ok[0]:.1f}ms  avg={sum(bench_ok)/len(bench_ok):.1f}ms  "
              f"p50={percentile(bench_ok, 50):.1f}ms  p95={percentile(bench_ok, 95):.1f}ms  "
              f"max={bench_ok[-1]:.1f}ms")
    else:
        print(f"   全部失败（{bench_fail} 个），请检查 .env 配置与服务状态")
    print(f"   明细: {json_path}")
    print()
    print("== 下一步：采集 orchestrator 日志并生成服务端阶段耗时报告")
    print("   bash collect_logs.sh orchestrator ./orchestrator-logs")
    print(f"   python3 parse_report.py ./orchestrator-logs/*.log \\")
    print(f"       --since '{meta['bench_window_since']}' \\")
    print(f"       --until '{meta['bench_window_until']}' \\")
    print(f"       --expected {args.count} --reference reference_sample.csv")
    print("   （客户端与服务器时钟不同步时，请改用 --last "
          f"{args.count} 取最近 {args.count} 条，不要用时间窗口）")


if __name__ == "__main__":
    main()
