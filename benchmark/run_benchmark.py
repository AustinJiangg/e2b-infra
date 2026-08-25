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
    python /opt/e2b-infra/patch_e2b.py   # 自部署环境 https->http 补丁

环境变量（可放在当前目录 .env 中，用 sync-env.sh 同步）:
    E2B_API_KEY / E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL

用法示例:
    python run_benchmark.py --template base --count 100 --concurrency 1
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


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


def write_latest_pointer(runs_root, run_dir_name):
    """记录最近一次运行目录：原子写 runs/.latest，并尽力维护 runs/latest 符号链接。

    .latest 只存目录名（不存绝对路径），整棵 benchmark/ 树拷到别的机器也能用，
    读取方再把名字拼回本地 runs 根。collect_logs.sh / parse_report.py 不带参数时
    就靠它定位到本次运行目录。
    """
    os.makedirs(runs_root, exist_ok=True)
    ptr = os.path.join(runs_root, ".latest")
    tmp = ptr + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(run_dir_name + "\n")
    os.replace(tmp, ptr)  # 原子替换，避免并发/中断写半截
    # 便捷符号链接，失败不影响（.latest 文本才是真相，且全平台可用）
    try:
        link = os.path.join(runs_root, "latest")
        link_tmp = link + ".tmp"
        if os.path.lexists(link_tmp):
            os.remove(link_tmp)
        os.symlink(run_dir_name, link_tmp)  # 相对目标，便于整树搬迁
        os.replace(link_tmp, link)
    except (OSError, NotImplementedError):
        pass


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
    ap.add_argument("--fc-netns-exec-helper", default=os.environ.get("E2B_FC_NETNS_EXEC_HELPER", ""),
                    help="本次 orchestrator 的 E2B_FC_NETNS_EXEC_HELPER（helper 路径 / disabled），"
                         "仅作标注写入 meta.json，便于开关前后对照（默认读同名环境变量）")
    return ap.parse_args()


# 网络槽位暖池上限：orchestrator 的 NewSlotsPoolSize(32) + ReusedSlotsPoolSize(100)。
# 沙箱销毁后槽位会回暖池待复用，netns/veth 不立即拆——所以压测后残留 ≤132 是正常的。
WARM_POOL_LIMIT = 132


def host_net_health() -> dict:
    """压测后顺手体检宿主机的沙箱网络资源。

    orchestrator 进程被强杀时来不及跑 networkPool.Close()，暖池里的 netns/veth 会留在
    宿主机上；下次启动它把这些记成 foreignNs 永久跳过，于是只增不减。攒到几千个之后
    nomad 的 client fingerprint 要枚举所有网卡，4646 迟迟不开，表现为 build.sh -s 卡死。
    这里只读不改，任何异常都静默跳过——体检不该影响压测本身。
    """
    import subprocess

    def _count(cmd: str) -> int:
        try:
            out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=15)
            return int((out.stdout or "").strip() or 0)
        except Exception:
            return -1

    return {
        "sandbox_netns": _count(r"ip netns list 2>/dev/null | awk '{print $1}' | grep -cE '^ns-[0-9]+$'"),
        "sandbox_veth": _count(r"ip -o link 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' | grep -cE '^veth-[0-9]+$'"),
        "host_links": _count(r"ip -o link 2>/dev/null | wc -l"),
    }


def print_host_net_health(health: dict) -> None:
    ns, veth, links = health["sandbox_netns"], health["sandbox_veth"], health["host_links"]
    if links <= 0:
        # 连网卡都数不出来 = 没有 ip 命令或没权限（例如压测跑在非部署机上），不打扰
        return

    print(f"== 宿主机网络体检: 沙箱 netns {ns} / veth {veth} / 网卡总数 {links}")
    if max(ns, veth) > WARM_POOL_LIMIT:
        print(f"   ⚠️  超过暖池上限 {WARM_POOL_LIMIT}，说明有泄漏。上千之后 nomad 起不来、"
              f"build.sh -s 会卡在“端口未启动”。")
        print("   清理：停服时 bash /opt/e2b-infra/build.sh -d（见 deploy-docs/05 §13）")


def main():
    args = parse_args()

    # 加载 .env（与 usage.md 客户端配置方式一致）；未装 dotenv 时静默跳过
    try:
        from dotenv import load_dotenv
        # 固定读脚本同目录的 .env（不依赖当前工作目录）；文件不存在时静默跳过
        load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    except ImportError:
        pass

    try:
        from e2b import Sandbox
    except ImportError:
        print("错误: 未安装 e2b SDK。请先执行: pip install e2b==2.20.0", file=sys.stderr)
        sys.exit(1)

    # 每次压测建一个时间戳运行目录 runs/run_<时间戳>/，所有产物（客户端结果、
    # 采集的日志、报告）都归集到这里；后续 collect/parse 不带参数自动定位到它。
    start_dt = now_dt()
    run_id = start_dt.strftime("bench-%Y%m%d-%H%M%S")
    run_dir_name = start_dt.strftime("run_%Y%m%d_%H%M%S")
    runs_root = os.path.join(SCRIPT_DIR, "runs")
    run_dir = os.path.join(runs_root, run_dir_name)
    os.makedirs(run_dir, exist_ok=True)
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

    net_health = host_net_health()
    print_host_net_health(net_health)

    # ---- 客户端统计 ----
    bench_ok = sorted(r["client_ms"] for r in results if not r["warmup"] and r["ok"])
    bench_fail = sum(1 for r in results if not r["warmup"] and not r["ok"])
    meta = {
        "run_id": run_id,
        "run_dir": run_dir,
        "template": args.template,
        "count": args.count,
        "expected": args.count,  # parse_report.py 直接读这个做 --expected 告警阈值
        "concurrency": args.concurrency,
        "interval": args.interval,
        "warmup": args.warmup,
        "host_net": net_health,  # 压测后的沙箱网络资源残留，用于观察泄漏趋势
        "fc_netns_exec_helper": args.fc_netns_exec_helper,  # netns-exec 开关标注，仅记录

        "bench_window_since": iso(bench_start - timedelta(seconds=2)),
        "bench_window_until": iso(bench_end + timedelta(seconds=10)),
        "client_ok": len(bench_ok),
        "client_fail": bench_fail,
        "logs_glob": "orchestrator-logs/*.log",  # 相对运行目录，parse 自动读取
    }

    json_path = os.path.join(run_dir, f"{run_id}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "results": results}, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(run_dir, f"{run_id}.client_times.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    # meta.json 是第 1 步到第 3 步的契约：携带压测窗口/期望数/日志通配，
    # 让 parse_report.py 零参数即可自动填窗口与 --expected。
    meta_path = os.path.join(run_dir, "meta.json")
    meta_tmp = meta_path + ".tmp"
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(meta_tmp, meta_path)
    write_latest_pointer(runs_root, run_dir_name)

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
    print(f"   运行目录: {run_dir}")
    print()
    print(f"== 下一步：采集日志并生成报告（都会自动定位到本次运行目录 runs/{run_dir_name}/）")
    print("   bash collect_logs.sh")
    print("   python parse_report.py")
    print("   python visualize_intervals.py          # 可选：画启动甘特图 3 张（需 matplotlib）")


if __name__ == "__main__":
    main()
