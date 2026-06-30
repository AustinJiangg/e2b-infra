#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析 orchestrator 日志中的 [ResumeSandbox] 阶段耗时，生成沙箱启动耗时统计报告。

日志埋点来自 0001-adapted-for-arm-architecture.patch，对应消息:
    [ResumeSandbox] enter, traceID=<32hex>
    [ResumeSandbox] <阶段> cost: <数值> ms, traceID=<32hex>
    [ResumeSandbox] total cost: <数值> ms, traceID=<32hex>

支持 zap JSON 行（timestamp/ts/time 等字段名均可）与纯文本行，仅依赖标准库。

输出到本次运行目录的 report/（--outdir 可覆盖）:
    report_wide.csv   与参考报告同布局（行=阶段，列=沙箱），含均值/分位数汇总列
    report_long.csv   每行一个沙箱，便于二次分析
    summary.csv       各阶段统计（min/avg/p50/p90/p95/p99/max）
    compare.csv       与 --reference 参考数据的均值对比（可选）
    intervals.csv     每个沙箱的开始/结束时间（两行），供 visualize_intervals.py 画甘特图

不带位置参数时，自动定位最近一次运行目录（runs/.latest），读取其
orchestrator-logs/*.log，并从 meta.json 自动填 --expected 与时间窗口。

用法示例:
    python3 parse_report.py --reference reference_sample.csv
    python3 parse_report.py --reference reference_sample.csv --last 100   # 时钟不同步时
    python3 parse_report.py --run-dir runs/run_20260629_142530 --reference reference_sample.csv
"""

import argparse
import csv
import glob
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 阶段定义：日志 key -> 报告行（分组、描述），顺序与参考报告完全一致
# ---------------------------------------------------------------------------
PHASE_ROWS = [
    # (分组,                 描述,                          日志 key，None=日志中无此埋点)
    ("准入排队",             "准入排队(等starting槽位)",     "acquire wait"),       # total 外：等信号量
    ("沙箱恢复准备",         "准备 rootfs（连接 nbd 设备）", "get rootfs path"),
    ("沙箱恢复准备",         "获取网络槽位",                 "wait network slot"),
    ("沙箱恢复准备",         "获取 template 元数据",         "get template metadata"),
    ("创建 firecracker 进程", "创建 firecracker 进程",       "fc.NewProcess"),
    ("创建 firecracker 进程", "等待firecracker启动",          "configured fc"),       # 父=下面两段之和
    ("创建 firecracker 进程", "└拉起FC进程",                  "fc spawn"),            # cmd.Start
    ("创建 firecracker 进程", "└等FC API socket",             "fc socket wait"),      # socket.Wait
    ("创建 firecracker 进程", "等待uffd sock",                "get uffd sock path"),
    ("firecracker 恢复虚拟机", "加载快照",                    "load snapshot"),
    ("firecracker 恢复虚拟机", "调用恢复",                    "post resume"),
    ("firecracker 恢复虚拟机", "设置mmds",                    "set mmds"),
    ("firecracker 恢复虚拟机", "恢复虚拟机",                  "resume VM"),
    ("启动 envd",            "启动 envd",                    "start envd"),         # WaitForEnvd 整体（sandbox.go）
    ("启动 envd",            "请求init接口",                 "envd init request"),  # POST /init（envd.go initEnvd）
    ("启动 envd",            "读取envd返回体",               "read envd response"), # 读 /init 响应体（envd.go initEnvd）
    ("ResumeSandbox",        "ResumeSandbox总耗时",          "total"),
]
KNOWN_KEYS = {key for _, _, key in PHASE_ROWS if key}

LINE_RE = re.compile(
    r"\[ResumeSandbox\]\s*(?P<body>[^\"\\]+?),\s*traceID=(?P<tid>[0-9a-fA-F]{16,64})")
COST_RE = re.compile(r"^(?P<key>.+?)\s+cost:\s*(?P<val>[0-9]+(?:\.[0-9]+)?)\s*ms$")
ISO_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?")


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------
def parse_tz(spec):
    if spec == "local":
        return datetime.now().astimezone().tzinfo
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", spec)
    if not m:
        raise SystemExit(f"无法识别的时区: {spec}（示例: +08:00 或 local）")
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def parse_iso(s, assume_tz):
    s = s.strip().replace(",", ".")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:?\d{2})?$", s)
    if not m:
        return None
    date, hms, frac, off = m.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    if off and ":" not in off:
        off = off[:3] + ":" + off[3:]
    try:
        dt = datetime.fromisoformat(f"{date}T{hms}.{frac}{off or ''}")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt


def parse_ts_value(v, assume_tz):
    if isinstance(v, str):
        return parse_iso(v, assume_tz)
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e17:        # 纳秒
            x /= 1e9
        elif x > 1e14:      # 微秒
            x /= 1e6
        elif x > 1e11:      # 毫秒
            x /= 1e3
        try:
            return datetime.fromtimestamp(x, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def extract_ts(line, assume_tz):
    """优先按 zap JSON 解析时间字段，失败则正则搜 ISO 时间串。"""
    s = line.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            for k in ("timestamp", "ts", "time", "T", "@timestamp"):
                if k in obj:
                    dt = parse_ts_value(obj[k], assume_tz)
                    if dt:
                        return dt
        except (json.JSONDecodeError, ValueError):
            pass
    m = ISO_RE.search(line)
    if m:
        return parse_iso(m.group(0), assume_tz)
    return None


# ---------------------------------------------------------------------------
# 日志解析
# ---------------------------------------------------------------------------
def parse_logs(paths, assume_tz):
    traces = {}     # tid -> {"enter": dt|None, "end": dt|None, "phases": {key: ms}}
    seen = set()    # (tid, body) 去重，防止同一份日志被重复输入
    unknown_keys = {}

    def trace(tid):
        # phase_ts[key] = 该阶段 cost 日志的时间戳(≈阶段结束时刻)，供 timeline.csv 还原真实区间
        return traces.setdefault(tid, {"enter": None, "end": None, "phases": {}, "phase_ts": {}})

    n_lines = 0
    for path in paths:
        fh = sys.stdin if path == "-" else open(path, "r", encoding="utf-8", errors="replace")
        with fh:
            for line in fh:
                m = LINE_RE.search(line)
                if not m:
                    continue
                n_lines += 1
                body = m.group("body").strip()
                tid = m.group("tid").lower()
                if (tid, body) in seen:
                    continue
                seen.add((tid, body))

                if body == "enter":
                    trace(tid)["enter"] = extract_ts(line, assume_tz)
                    continue
                cm = COST_RE.match(body)
                if not cm:
                    continue
                key, val = cm.group("key").strip(), float(cm.group("val"))
                if key == "total":
                    t = trace(tid)
                    t["phases"]["total"] = val
                    t["end"] = extract_ts(line, assume_tz)
                    t["phase_ts"]["total"] = t["end"]
                elif key in KNOWN_KEYS:
                    t = trace(tid)
                    t["phases"][key] = val
                    t["phase_ts"][key] = extract_ts(line, assume_tz)
                else:
                    unknown_keys[key] = unknown_keys.get(key, 0) + 1
    return traces, n_lines, unknown_keys


# ---------------------------------------------------------------------------
# 统计与输出
# ---------------------------------------------------------------------------
def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = max(1, math.ceil(p / 100.0 * len(sorted_vals)))
    return sorted_vals[k - 1]


def fmt(v, nd=3):
    """数值格式化：整数不带小数点，其余保留 nd 位。"""
    if v is None:
        return ""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.{nd}f}".rstrip("0").rstrip(".")


def fmt_dt(dt):
    return dt.isoformat(timespec="milliseconds") if dt else ""


def phase_stats(valid, key):
    vals = sorted(t["phases"][key] for t in valid if key in t["phases"])
    if not vals:
        return None
    return {
        "n": len(vals),
        "min": vals[0],
        "avg": sum(vals) / len(vals),
        "p50": percentile(vals, 50),
        "p90": percentile(vals, 90),
        "p95": percentile(vals, 95),
        "p99": percentile(vals, 99),
        "max": vals[-1],
    }


def write_wide(path, ordered, gen_time):
    cols = [f"沙箱{i+1}" for i in range(len(ordered))]
    stat_cols = ["平均", "P50", "P90", "P95", "最大"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["监控统计报告"])
        w.writerow(["生成时间", f"{gen_time.year}/{gen_time.month}/{gen_time.day} {gen_time:%H:%M}"])
        w.writerow(["有效沙箱数", len(ordered)])
        w.writerow([])
        w.writerow(["", ""] + cols + stat_cols)
        w.writerow(["TraceID", ""] + [tid for tid, _ in ordered] + [""] * len(stat_cols))
        w.writerow(["开始时间(enter)", ""] + [fmt_dt(t["enter"]) for _, t in ordered]
                   + [""] * len(stat_cols))
        w.writerow(["结束时间(total)", ""] + [fmt_dt(t["end"]) for _, t in ordered]
                   + [""] * len(stat_cols))
        w.writerow([])
        w.writerow(["阶段", "描述"] + cols + stat_cols)
        valid = [t for _, t in ordered]
        for group, label, key in PHASE_ROWS:
            row = [group, label]
            if key is None:
                row += [""] * (len(cols) + len(stat_cols))
            else:
                row += [fmt(t["phases"].get(key)) for t in valid]
                st = phase_stats(valid, key)
                row += ([fmt(st["avg"]), fmt(st["p50"]), fmt(st["p90"]),
                         fmt(st["p95"]), fmt(st["max"])] if st else [""] * len(stat_cols))
            w.writerow(row)


def write_long(path, ordered):
    headers = (["序号", "TraceID", "开始时间(enter)", "结束时间(total)"]
               + [label for _, label, key in PHASE_ROWS if key])
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for i, (tid, t) in enumerate(ordered, 1):
            row = [i, tid, fmt_dt(t["enter"]), fmt_dt(t["end"])]
            row += [fmt(t["phases"].get(key)) for _, _, key in PHASE_ROWS if key]
            w.writerow(row)


def write_intervals(path, ordered):
    """写出供 visualize_intervals.py 用的两行 CSV：第1行各沙箱开始时间、第2行结束时间，
    每列一个沙箱（与报告同序）。免去手动从 report_wide.csv 提取这两行。

    纯文本无 BOM，时间为带时区 ISO 串（可被 datetime.fromisoformat 直接解析）。
    缺 enter 时用 end 减总耗时回推开始时间；缺 end 的沙箱无法成区间，跳过。
    """
    starts, ends = [], []
    for _, t in ordered:
        end = t["end"]
        if end is None:
            continue
        start = t["enter"]
        if start is None:
            total = t["phases"].get("total")
            start = end - timedelta(milliseconds=total) if total is not None else end
        starts.append(fmt_dt(start))
        ends.append(fmt_dt(end))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(starts)
        w.writerow(ends)
    return len(starts)


def write_summary(path, valid):
    rows = []
    for group, label, key in PHASE_ROWS:
        if key is None:
            continue
        st = phase_stats(valid, key)
        if st:
            rows.append([group, label, st["n"]] + [fmt(st[k]) for k in
                        ("min", "avg", "p50", "p90", "p95", "p99", "max")])
        else:
            rows.append([group, label, 0] + [""] * 7)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["阶段", "描述", "样本数", "min(ms)", "avg(ms)", "p50(ms)",
                    "p90(ms)", "p95(ms)", "p99(ms)", "max(ms)"])
        w.writerows(rows)
    return rows


# ---------------------------------------------------------------------------
# 可视化用：把每个阶段还原成「真实时间轴上的区间」，供 visualize_intervals.py 画
# 「真实时间轴 + 彩色分阶段 + 并行重叠」的二合一甘特图。详见 高并发瓶颈定位方案.md 第 6 节。
# 每条 cost 日志的时间戳 ≈ 该阶段结束时刻，区间 = [ts − 时长, ts]；同节点时钟、天然自洽。
# 只取「叶子」阶段，排除父区间(configured fc/resume VM/total)与被 start envd 覆盖的 envd 子段，
# 避免在时间轴上重复绘制。并行段(configure∥uffd∥rootfs)的区间会自然重叠，由可视化用泳道展开。
# ---------------------------------------------------------------------------
TIMELINE_STAGES = [
    "acquire wait", "wait network slot", "get template metadata", "fc.NewProcess",
    "fc spawn", "fc socket wait", "get uffd sock path", "get rootfs path",
    "load snapshot", "post resume", "set mmds", "start envd",
]


def write_timeline(path, ordered):
    """每行一个(沙箱,阶段)区间：TraceID, stage, start_ms, end_ms（相对全局最早事件的毫秒偏移）。"""
    rows = []
    origin = None
    for tid, t in ordered:
        pts = t.get("phase_ts", {})
        ph = t["phases"]
        for k in TIMELINE_STAGES:
            ts, dur = pts.get(k), ph.get(k)
            if ts is None or dur is None:
                continue
            start = ts - timedelta(milliseconds=dur)
            rows.append((tid, k, start, ts))
            if origin is None or start < origin:
                origin = start
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["TraceID", "stage", "start_ms", "end_ms"])
        if origin is None:
            return
        for tid, k, start, end in rows:
            w.writerow([tid, k,
                        fmt((start - origin).total_seconds() * 1000),
                        fmt((end - origin).total_seconds() * 1000)])


def load_client_ms(run_dir):
    """从运行目录的 *.client_times.csv 读 client_ms 列（DictReader；缺失返回 []）。"""
    if not run_dir:
        return []
    vals = []
    for p in sorted(glob.glob(os.path.join(run_dir, "*.client_times.csv"))):
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    v = row.get("client_ms")
                    if v:
                        try:
                            vals.append(float(v))
                        except ValueError:
                            pass
        except OSError:
            pass
    return vals


def load_reference(path):
    """读取参考数据 CSV（行=阶段，第 3 列起为各沙箱数值），返回 {描述: [值...]}"""
    ref = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 3 or row[1] in ("", "描述"):
                continue
            vals = []
            for cell in row[2:]:
                try:
                    vals.append(float(cell))
                except ValueError:
                    pass
            if vals:
                ref[row[1].strip()] = vals
    return ref


def write_compare(path, valid, ref):
    rows = []
    for group, label, key in PHASE_ROWS:
        if key is None or label not in ref:
            continue
        st = phase_stats(valid, key)
        rvals = ref[label]
        ravg = sum(rvals) / len(rvals)
        if st:
            diff = st["avg"] - ravg
            pct = (diff / ravg * 100) if ravg else float("inf")
            rows.append([group, label, fmt(ravg), len(rvals), fmt(st["avg"]), st["n"],
                         fmt(diff), f"{pct:+.1f}%"])
        else:
            rows.append([group, label, fmt(ravg), len(rvals), "", 0, "", ""])
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["阶段", "描述", "参考均值(ms)", "参考样本数",
                    "本次均值(ms)", "本次样本数", "差值(ms)", "差值(%)"])
        w.writerows(rows)
    return rows


def resolve_run_dir(explicit, runs_root, auto):
    """定位本次运行目录，返回绝对路径或 None。

    顺序：--run-dir > BENCH_RUN_DIR > runs/.latest（仅 auto=True，即未显式传日志时）。
    auto=True 且都找不到时直接报错；auto=False（用户已显式传日志）则返回 None。
    """
    cand = explicit or os.environ.get("BENCH_RUN_DIR")
    if cand:
        cand = os.path.abspath(cand)
        if not os.path.isdir(cand):
            raise SystemExit(f"错误: 指定的运行目录不存在: {cand}")
        return cand
    if auto:
        ptr = os.path.join(runs_root, ".latest")
        if os.path.isfile(ptr):
            with open(ptr, encoding="utf-8") as f:
                name = f.read().strip()
            d = os.path.join(runs_root, name)
            if name and os.path.isdir(d):
                return d
        raise SystemExit(
            "错误: 未找到基准测试运行目录。\n"
            "  请先运行 run_benchmark.py（会创建 runs/run_<时间戳>/ 并记录 runs/.latest），\n"
            "  或用 --run-dir <目录> 显式指定，或设置环境变量 BENCH_RUN_DIR=<目录>。")
    return None


def load_meta(run_dir):
    """读取运行目录下的 meta.json，缺失/损坏时返回空 dict。"""
    if not run_dir:
        return {}
    path = os.path.join(run_dir, "meta.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser(
        description="解析 orchestrator [ResumeSandbox] 日志生成耗时统计报告",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("logs", nargs="*",
                    help="orchestrator 日志文件（可多个/通配符，- 表示 stdin；"
                         "省略则自动用本次运行目录的 orchestrator-logs/*.log）")
    ap.add_argument("--run-dir", help="本次运行目录（默认读取 runs/.latest 指向的最近一次）")
    ap.add_argument("--since", help="只统计 enter 时间 >= 此时刻的沙箱（ISO 格式）")
    ap.add_argument("--until", help="只统计 enter 时间 <= 此时刻的沙箱（ISO 格式）")
    ap.add_argument("--last", type=int, help="只取按 enter 时间排序的最后 N 个有效沙箱"
                                             "（客户端/服务器时钟不同步时替代时间窗口）")
    ap.add_argument("--expected", type=int, help="期望的有效沙箱数，不一致时告警")
    ap.add_argument("--sort", choices=["traceid", "time"], default="traceid",
                    help="报告中沙箱列的排序方式（参考报告按 traceid 排序）")
    ap.add_argument("--tz", default="+08:00",
                    help="无时区日志行的默认时区，及报告显示时区（如 +08:00 / local）")
    ap.add_argument("--reference", help="参考数据 CSV（如 reference_sample.csv），生成对比表")
    ap.add_argument("--outdir", help="报告输出目录（默认本次运行目录下的 report/）")
    args = ap.parse_args()

    # 定位本次运行目录并用 meta.json 自动填参；仅当未显式传 logs 时才自动定位。
    runs_root = os.path.join(SCRIPT_DIR, "runs")
    run_dir = resolve_run_dir(args.run_dir, runs_root, auto=not args.logs)
    meta = load_meta(run_dir)
    if not args.logs and run_dir:
        args.logs = [os.path.join(run_dir, meta.get("logs_glob", "orchestrator-logs/*.log"))]
    if args.expected is None and meta.get("expected") is not None:
        args.expected = meta["expected"]
    # 时间窗口优先级：--last > CLI --since/--until > meta 窗口兜底（--last 永远压过自动窗口）
    if not args.last and not args.since and not args.until:
        if meta.get("bench_window_since"):
            args.since = meta["bench_window_since"]
        if meta.get("bench_window_until"):
            args.until = meta["bench_window_until"]
    if args.outdir is None:
        args.outdir = os.path.join(run_dir, "report") if run_dir else "report-out"

    assume_tz = parse_tz(args.tz)

    # 通配符展开（兼容未被 shell 展开的情况）
    paths = []
    for p in args.logs:
        if p == "-" or os.path.exists(p):
            paths.append(p)
        else:
            paths.extend(sorted(glob.glob(p)))
    if not paths:
        raise SystemExit("错误: 未找到任何日志文件")

    traces, n_lines, unknown = parse_logs(paths, assume_tz)
    if n_lines == 0:
        raise SystemExit("错误: 日志中没有任何 [ResumeSandbox] 行。\n"
                         "请确认 orchestrator 是用本仓库 patch 构建的，且采集的是 stdout+stderr。")

    valid = {tid: t for tid, t in traces.items() if "total" in t["phases"]}
    partial = len(traces) - len(valid)

    # 时间窗口过滤（按 enter 时间，enter 缺失则用 end 时间）
    def anchor(t):
        return t["enter"] or t["end"]

    if args.since:
        since = parse_iso(args.since, assume_tz)
        if not since:
            raise SystemExit(f"--since 时间格式错误: {args.since}")
        valid = {k: v for k, v in valid.items() if anchor(v) and anchor(v) >= since}
    if args.until:
        until = parse_iso(args.until, assume_tz)
        if not until:
            raise SystemExit(f"--until 时间格式错误: {args.until}")
        valid = {k: v for k, v in valid.items() if anchor(v) and anchor(v) <= until}
    if args.last:
        picked = sorted(valid.items(),
                        key=lambda kv: anchor(kv[1]) or datetime.min.replace(tzinfo=timezone.utc))
        valid = dict(picked[-args.last:])

    if not valid:
        raise SystemExit("错误: 过滤后没有有效沙箱（有 total cost 的 trace）。"
                         "请检查 --since/--until 时间窗口或改用 --last。")

    # 显示时区统一
    for t in valid.values():
        for k in ("enter", "end"):
            if t[k]:
                t[k] = t[k].astimezone(assume_tz)

    if args.sort == "time":
        ordered = sorted(valid.items(),
                         key=lambda kv: anchor(kv[1]) or datetime.min.replace(tzinfo=timezone.utc))
    else:
        ordered = sorted(valid.items(), key=lambda kv: kv[0])

    os.makedirs(args.outdir, exist_ok=True)
    gen_time = datetime.now(assume_tz)
    wide_path = os.path.join(args.outdir, "report_wide.csv")
    long_path = os.path.join(args.outdir, "report_long.csv")
    summary_path = os.path.join(args.outdir, "summary.csv")
    intervals_path = os.path.join(args.outdir, "intervals.csv")
    timeline_path = os.path.join(args.outdir, "timeline.csv")

    write_wide(wide_path, ordered, gen_time)
    write_long(long_path, ordered)
    write_intervals(intervals_path, ordered)
    write_timeline(timeline_path, ordered)
    valid_list = [t for _, t in ordered]
    summary_rows = write_summary(summary_path, valid_list)

    # ---- 控制台输出 ----
    print(f"== 解析完成: 日志行 {n_lines} 条, trace {len(traces)} 个, "
          f"有效沙箱 {len(valid)} 个, 不完整 trace {partial} 个")
    if args.expected and len(valid) != args.expected:
        print(f"!! 警告: 有效沙箱数 {len(valid)} != 期望 {args.expected}。"
              "可能原因: 时间窗口不准 / 多节点日志未收齐 / 部分创建失败 / 日志被轮转。")
    if unknown:
        print(f"!! 提示: 发现未识别的阶段 key（可能 patch 有更新）: {unknown}")

    print(f"\n== 各阶段耗时统计 (单位 ms, 共 {len(valid)} 个沙箱)")
    hdr = f"{'阶段/描述':<34}{'样本':>5} {'min':>9} {'avg':>9} {'p50':>9} {'p90':>9} {'p95':>9} {'p99':>9} {'max':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in summary_rows:
        name = f"{r[0]}/{r[1]}"
        pad = max(0, 34 - sum(2 if ord(c) > 127 else 1 for c in name))
        print(f"{name}{' ' * pad}{r[2]:>5} " + " ".join(f"{x:>9}" for x in r[3:]))

    # 端到端拆解（聚合）：客户端整体 ≈ 准入排队 + ResumeSandbox总耗时 + 其余(路由/API/取模板…)
    client_ms = load_client_ms(run_dir)
    if client_ms:
        c_avg = sum(client_ms) / len(client_ms)
        q = phase_stats(valid_list, "acquire wait")
        tot = phase_stats(valid_list, "total")
        q_avg = q["avg"] if q else 0.0
        t_avg = tot["avg"] if tot else 0.0
        rest = c_avg - q_avg - t_avg
        print(f"\n== 端到端拆解 (聚合 avg, ms; client_ms 来自 client_times.csv, n={len(client_ms)})")
        print(f"  客户端整体 client_ms        : {fmt(c_avg)}")
        print(f"  ├ 准入排队 acquire wait      : {fmt(q_avg)}   (total 外)")
        print(f"  ├ ResumeSandbox总耗时 total  : {fmt(t_avg)}")
        print(f"  └ 其余(路由+API+取模板等)    : {fmt(rest)}   ← client_ms − total − 准入排队")
        with open(os.path.join(args.outdir, "e2e_breakdown.csv"), "w",
                  encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["项", "avg(ms)", "样本数", "说明"])
            w.writerow(["客户端整体 client_ms", fmt(c_avg), len(client_ms), "Sandbox.create() 墙钟"])
            w.writerow(["准入排队 acquire wait", fmt(q_avg), q["n"] if q else 0, "等 starting 信号量(total 外)"])
            w.writerow(["ResumeSandbox总耗时 total", fmt(t_avg), tot["n"] if tot else 0, "ResumeSandbox 函数"])
            w.writerow(["其余(路由+API+取模板等)", fmt(rest), "", "client_ms − total − 准入排队(派生)"])

    if args.reference:
        compare_path = os.path.join(args.outdir, "compare.csv")
        # 相对 cwd 找不到时回退到脚本同级目录，使 --reference reference_sample.csv 不依赖 cwd
        ref_path = args.reference
        if not os.path.exists(ref_path):
            alt = os.path.join(SCRIPT_DIR, ref_path)
            if os.path.exists(alt):
                ref_path = alt
        ref = load_reference(ref_path)
        rows = write_compare(compare_path, valid_list, ref)
        print(f"\n== 与参考数据对比 (参考: {args.reference})")
        hdr2 = f"{'阶段/描述':<34}{'参考avg':>10}{'本次avg':>10}{'差值ms':>10}{'差值%':>9}"
        print(hdr2)
        print("-" * len(hdr2))
        for r in rows:
            name = f"{r[0]}/{r[1]}"
            pad = max(0, 34 - sum(2 if ord(c) > 127 else 1 for c in name))
            print(f"{name}{' ' * pad}{r[2]:>10}{r[4]:>10}{r[6]:>10}{r[7]:>9}")
        print(f"\n报告文件: {wide_path} | {long_path} | {summary_path} | {compare_path}")
    else:
        print(f"\n报告文件: {wide_path} | {long_path} | {summary_path}")
    print(f"分阶段时间轴数据: {timeline_path}")
    print("可视化（需 matplotlib）: python3 visualize_intervals.py   # 出图 report/timeline.png（真实时间轴+彩色分阶段+并行重叠）")


if __name__ == "__main__":
    main()
