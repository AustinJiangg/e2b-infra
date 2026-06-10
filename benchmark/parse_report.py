#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析 orchestrator 日志中的 [ResumeSandbox] 阶段耗时，生成沙箱启动耗时统计报告。

日志埋点来自 0001-adapted-for-arm-architecture.patch，对应消息:
    [ResumeSandbox] enter, traceID=<32hex>
    [ResumeSandbox] <阶段> cost: <数值> ms, traceID=<32hex>
    [ResumeSandbox] total cost: <数值> ms, traceID=<32hex>

支持 zap JSON 行（timestamp/ts/time 等字段名均可）与纯文本行，仅依赖标准库。

输出（--outdir，默认 report-out/）:
    report_wide.csv   与参考报告同布局（行=阶段，列=沙箱），含均值/分位数汇总列
    report_long.csv   每行一个沙箱，便于二次分析
    summary.csv       各阶段统计（min/avg/p50/p90/p95/p99/max）
    compare.csv       与 --reference 参考数据的均值对比（可选）

用法示例:
    python3 parse_report.py orchestrator-logs/*.log \
        --since '2026-06-10T15:00:00+08:00' --until '2026-06-10T15:05:00+08:00' \
        --expected 100 --reference reference_sample.csv
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

# ---------------------------------------------------------------------------
# 阶段定义：日志 key -> 报告行（分组、描述），顺序与参考报告完全一致
# ---------------------------------------------------------------------------
PHASE_ROWS = [
    # (分组,                 描述,                          日志 key，None=日志中无此埋点)
    ("沙箱恢复准备",         "准备 rootfs（连接 nbd 设备）", "get rootfs path"),
    ("沙箱恢复准备",         "获取网络槽位",                 "wait network slot"),
    ("沙箱恢复准备",         "获取 template 元数据",         "get template metadata"),
    ("创建 firecracker 进程", "创建 firecracker 进程",       "fc.NewProcess"),
    ("创建 firecracker 进程", "等待firecracker启动",          "configured fc"),
    ("创建 firecracker 进程", "等待uffd sock",                "get uffd sock path"),
    ("firecracker 恢复虚拟机", "加载快照",                    "load snapshot"),
    ("firecracker 恢复虚拟机", "调用恢复",                    "post resume"),
    ("firecracker 恢复虚拟机", "设置mmds",                    "set mmds"),
    ("firecracker 恢复虚拟机", "恢复虚拟机",                  "resume VM"),
    ("启动 envd",            "启动 envd",                    None),  # patch 未埋点，留空保持格式
    ("启动 envd",            "请求init接口",                 None),
    ("启动 envd",            "读取envd返回体",               None),
    ("总耗时",               "总耗时",                       "total"),
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
        return traces.setdefault(tid, {"enter": None, "end": None, "phases": {}})

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
                elif key in KNOWN_KEYS:
                    trace(tid)["phases"][key] = val
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


def main():
    ap = argparse.ArgumentParser(
        description="解析 orchestrator [ResumeSandbox] 日志生成耗时统计报告",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("logs", nargs="+", help="orchestrator 日志文件（可多个/通配符，- 表示 stdin）")
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
    ap.add_argument("--outdir", default="report-out", help="报告输出目录")
    args = ap.parse_args()

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

    write_wide(wide_path, ordered, gen_time)
    write_long(long_path, ordered)
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

    if args.reference:
        compare_path = os.path.join(args.outdir, "compare.csv")
        ref = load_reference(args.reference)
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


if __name__ == "__main__":
    main()
