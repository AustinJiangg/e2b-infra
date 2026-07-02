#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可视化沙箱(sandbox)启动耗时，出 3 张图（都在 report/ 下）：

  timeline.png          合并图：真实时间轴 + 彩色分阶段 + 并行重叠（每沙箱一条，
                        按真实时刻摆放各阶段、按阶段上色，并行段用泳道分层）。读 timeline.csv。
  total_gantt.png       单色 total 甘特：每沙箱一条 total 区间（enter→total），按开始时间
                        排序，一眼看出高并发下的排队/铺开阶梯，底部附启动耗时统计。读 intervals.csv。
  stage_durations.png   分阶段堆叠：每沙箱一条，把各阶段 duration 首尾相接堆叠、按阶段上色
                        （并行段各自计入，看「各阶段花了多少」的构成，不反映重叠）。读 timeline.csv。

3 张图的 y 轴都用从上到下 1..n 的简单编号（一行一个沙箱，按开始时间排序）。

用法:
  python visualize_intervals.py                       # 自动定位最近一次运行目录
  python visualize_intervals.py --run-dir runs/run_20260629_142530
  python visualize_intervals.py path/to/timeline.csv  # 直接画指定 timeline.csv（同目录找 intervals.csv）

不带位置参数时，按 --run-dir > 环境变量 BENCH_RUN_DIR > runs/.latest 的顺序定位运行
目录（与 collect_logs.sh / parse_report.py 一致）。需要 matplotlib（pip install matplotlib）。
"""
import os
import sys
import argparse
import csv
from collections import defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 阶段顺序（图例/堆叠顺序用）、英文标签（避免缺中文字体显示方块）、配色。
# 与 parse_report.py 的 TIMELINE_STAGES 对应。
STAGE_ORDER = [
    "acquire wait", "wait network slot", "get template metadata", "fc.NewProcess",
    "fc spawn", "fc socket wait", "get uffd sock path", "get rootfs path",
    "load snapshot", "post resume", "set mmds", "start envd",
]
STAGE_LABEL = {
    "acquire wait": "queue (acquire wait) [outside total]",
    "wait network slot": "net slot",
    "get template metadata": "template meta",
    "fc.NewProcess": "fc.NewProcess",
    "fc spawn": "fc spawn (cmd.Start)",
    "fc socket wait": "fc socket wait",
    "get uffd sock path": "uffd sock wait",
    "get rootfs path": "rootfs path",
    "load snapshot": "load snapshot",
    "post resume": "post resume",
    "set mmds": "set mmds",
    "start envd": "start envd",
}
STAGE_COLOR = {
    "acquire wait": "#9e9e9e",        # gray (total 外)
    "wait network slot": "#aec7e8",
    "get template metadata": "#c5b0d5",
    "fc.NewProcess": "#17becf",       # cyan
    "fc spawn": "#ff7f0e",            # orange
    "fc socket wait": "#d62728",      # red（通常最大）
    "get uffd sock path": "#1f77b4",  # blue（与 socket wait 并行）
    "get rootfs path": "#bcbd22",     # olive（与 socket wait 并行）
    "load snapshot": "#9467bd",       # purple
    "post resume": "#e377c2",
    "set mmds": "#8c564b",
    "start envd": "#2ca02c",          # green
}
TOTAL_COLOR = "#1f77b4"               # 单色 total 甘特的颜色（与原图一致）


def read_timeline(path):
    """读 timeline.csv -> {tid: [(start_ms, end_ms, stage), ...]}（按 tid 分组）。"""
    groups = defaultdict(list)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                s, e = float(r["start_ms"]), float(r["end_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            stage = (r.get("stage") or "").strip()
            groups[(r.get("TraceID") or "").strip()].append((s, e, stage))
    return groups


def _parse_dt(cell):
    try:
        return datetime.fromisoformat(cell.strip())
    except (ValueError, AttributeError):
        return None


def read_intervals(path):
    """读 intervals.csv（两行：开始时间行 / 结束时间行，前面可能有标签列）。
    返回按开始时间排序的 [(start_dt, end_dt), ...]，解析不了的单元格（标签等）自动跳过。"""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    if len(rows) < 2:
        return []
    starts = [d for d in (_parse_dt(c) for c in rows[0]) if d]
    ends = [d for d in (_parse_dt(c) for c in rows[1]) if d]
    pairs = [(s, e) for s, e in zip(starts, ends)]
    pairs.sort(key=lambda x: x[0])
    return pairs


def assign_lanes(intervals):
    """贪心把重叠区间分配到不同泳道（并行段→不同泳道）。返回 [(s,e,stage,lane)], 泳道数。"""
    lane_end = []
    out = []
    for s, e, stage in sorted(intervals, key=lambda x: x[0]):
        placed = False
        for L in range(len(lane_end)):
            if s >= lane_end[L] - 1e-6:        # 该泳道已空出
                lane_end[L] = e
                out.append((s, e, stage, L))
                placed = True
                break
        if not placed:
            lane_end.append(e)
            out.append((s, e, stage, len(lane_end) - 1))
    return out, max(1, len(lane_end))


def resolve_timeline_csv(args):
    """返回 (timeline.csv 路径, 标题用的 stem, 图片输出目录)。"""
    if args.csv:
        out_dir = os.path.dirname(args.csv) or "."
        return args.csv, os.path.splitext(os.path.basename(args.csv))[0], out_dir
    from parse_report import resolve_run_dir
    run_dir = resolve_run_dir(args.run_dir, os.path.join(SCRIPT_DIR, "runs"), auto=True)
    csv_path = os.path.join(run_dir, "report", "timeline.csv")
    if not os.path.exists(csv_path):
        sys.exit(f"错误: 未找到 {csv_path}。请先运行 parse_report.py 生成报告。")
    return csv_path, os.path.basename(run_dir.rstrip("/")), os.path.join(run_dir, "report")


def _new_axes(plt, n):
    """按沙箱数决定行高/画布高，返回 (fig, ax, fig_h)。合并图与堆叠图共用。"""
    row_h = 0.30 if n <= 60 else (0.16 if n <= 200 else 0.06)
    fig_h = min(46, max(3.5, n * row_h + 2.0))
    fig, ax = plt.subplots(figsize=(13, fig_h))
    return fig, ax, fig_h


def _set_yaxis(ax, n):
    """y 轴：一行一个沙箱，从上到下用 1..n 编号；数量多时自动抽稀刻度，避免重叠。"""
    ax.set_ylim(n - 0.5, -0.5)
    if n <= 60:
        ticks = list(range(n))
    else:
        step = max(1, n // 40)
        ticks = list(range(0, n, step))
    ax.set_yticks(ticks)
    ax.set_yticklabels([str(i + 1) for i in ticks], fontsize=7)


def _finish(plt, fig, fig_h, output):
    fig.subplots_adjust(left=0.08, right=0.98, top=1 - 0.5 / fig_h, bottom=0.8 / fig_h)
    fig.savefig(output, dpi=150)
    plt.close(fig)
    print(f"已保存图片: {output}")


def draw_combined(plt, Patch, tids, laid, max_lanes, span, scale, unit, stem, output):
    """合并图：真实时间轴 + 彩色分阶段 + 并行泳道。"""
    n = len(tids)
    fig, ax, fig_h = _new_axes(plt, n)
    bar_h = 0.82
    lane_h = bar_h / max_lanes
    min_w = max(span * 0.001, 1e-6) * scale     # 极短阶段至少给一点可见宽度
    for row, t in enumerate(tids):
        placed, _nl = laid[t]
        y_top = row - bar_h / 2.0
        for s, e, stage, lane in placed:
            y = y_top + lane * lane_h + lane_h / 2.0
            ax.barh(y, max((e - s) * scale, min_w), left=s * scale, height=lane_h * 0.9,
                    color=STAGE_COLOR.get(stage, "#333333"), edgecolor="none")
    _set_yaxis(ax, n)
    ax.set_xlabel(f"Time since first event ({unit}) — real timeline; parallel stages shown as lanes",
                  fontsize=10)
    ax.set_title(f"{stem} — sandbox start timeline by stage (n={n})")
    ax.grid(axis="x", linestyle=":", alpha=0.5)
    ax.margins(x=0.01)
    handles = [Patch(color=STAGE_COLOR[s], label=STAGE_LABEL[s]) for s in STAGE_ORDER]
    ax.legend(handles=handles, ncol=4, fontsize=7.5, loc="lower right", framealpha=0.9)
    _finish(plt, fig, fig_h, output)


def draw_total_gantt(plt, pairs, stem, output):
    """单色 total 甘特（复刻原图）：每沙箱一条 total 区间，按开始时间排序，底部附统计。"""
    n = len(pairs)
    span_start = min(s for s, _e in pairs)
    span_end = max(e for _s, e in pairs)
    total_span = (span_end - span_start).total_seconds()
    durations = [(e - s).total_seconds() for s, e in pairs]

    use_ms = total_span < 10                    # 跨度小用 ms 显示，大用 s
    scale = 1000.0 if use_ms else 1.0
    unit = "ms" if use_ms else "s"

    row_h = 0.22 if n <= 60 else (0.12 if n <= 200 else 0.05)
    fig_h = min(40, max(3.5, n * row_h + 1.4))
    fig, ax = plt.subplots(figsize=(12, fig_h))
    bar_h = 0.6 if n <= 200 else 0.8
    min_w = total_span * 0.002 * scale          # 极短区间至少给一点可见宽度
    for row, (s, e) in enumerate(pairs):
        left = (s - span_start).total_seconds() * scale
        width = max((e - s).total_seconds() * scale, min_w)
        ax.barh(row, width, left=left, height=bar_h, color=TOTAL_COLOR, alpha=0.85)
    _set_yaxis(ax, n)
    ax.set_title(f"{stem} — {n} sandbox launches")
    ax.set_xlabel(f"Time since first launch ({unit})", fontsize=10)
    ax.grid(axis="x", linestyle=":", alpha=0.5)
    ax.margins(x=0.02)

    sd = sorted(durations)
    m = len(sd)
    median = sd[m // 2] if m % 2 else (sd[m // 2 - 1] + sd[m // 2]) / 2
    stats = (f"Launches: {n}      Total span: {total_span:.3f} s\n"
             f"Launch duration min/median/mean/max: "
             f"{min(durations)*1000:.1f} / {median*1000:.1f} / "
             f"{sum(durations)/n*1000:.1f} / {max(durations)*1000:.1f} ms")
    top_in, xlabel_in, footer_in = 0.5, 0.8, 0.7
    fig.subplots_adjust(left=0.07, right=0.97, top=1 - top_in / fig_h,
                        bottom=(xlabel_in + footer_in) / fig_h)
    ax_bottom = ax.get_position().y0
    fig.text(0.5, ax_bottom - (xlabel_in + footer_in * 0.45) / fig_h, stats,
             ha="center", va="center", fontsize=12, family="monospace")
    fig.savefig(output, dpi=150)
    plt.close(fig)
    print(f"已保存图片: {output}")


def draw_stage_durations(plt, Patch, tids, groups, stem, output):
    """分阶段堆叠：每沙箱把各阶段 duration 首尾相接堆叠、按阶段上色（不反映并行重叠）。"""
    n = len(tids)
    dur_by_tid = {}
    for t in tids:
        d = defaultdict(float)
        for s, e, st in groups[t]:
            d[st] += (e - s)
        dur_by_tid[t] = d
    max_total = max((sum(d.values()) for d in dur_by_tid.values()), default=0.0)
    use_s = max_total >= 5000
    scale = 1 / 1000.0 if use_s else 1.0
    unit = "s" if use_s else "ms"

    fig, ax, fig_h = _new_axes(plt, n)
    for row, t in enumerate(tids):
        left = 0.0
        d = dur_by_tid[t]
        for st in STAGE_ORDER:
            w = d.get(st, 0.0)
            if w <= 0:
                continue
            ax.barh(row, w * scale, left=left * scale, height=0.8,
                    color=STAGE_COLOR.get(st, "#333333"), edgecolor="none")
            left += w
    _set_yaxis(ax, n)
    ax.set_xlabel(f"Sum of per-stage durations ({unit}) — stacked (parallel stages counted separately)",
                  fontsize=10)
    ax.set_title(f"{stem} — sandbox stage-duration breakdown (n={n})")
    ax.grid(axis="x", linestyle=":", alpha=0.5)
    ax.margins(x=0.01)
    handles = [Patch(color=STAGE_COLOR[s], label=STAGE_LABEL[s]) for s in STAGE_ORDER]
    ax.legend(handles=handles, ncol=4, fontsize=7.5, loc="lower right", framealpha=0.9)
    _finish(plt, fig, fig_h, output)


def main():
    ap = argparse.ArgumentParser(
        description="沙箱启动可视化：合并图 + 单色 total 甘特 + 分阶段堆叠（共 3 张 PNG）")
    ap.add_argument("csv", nargs="?", help="timeline.csv（省略则自动用最近一次运行目录的）")
    ap.add_argument("--run-dir", help="本次运行目录（默认读取 runs/.latest 指向的最近一次）")
    args = ap.parse_args()

    csv_path, stem, out_dir = resolve_timeline_csv(args)
    groups = read_timeline(csv_path)
    if not groups:
        sys.exit(f"没有从 {csv_path} 读到任何阶段区间。")

    # 沙箱按「最早事件时刻」排序（≈ 进入顺序），高并发下能看出排队铺开的阶梯
    tids = sorted(groups, key=lambda t: min(s for s, _e, _st in groups[t]))
    n = len(tids)
    laid = {t: assign_lanes(groups[t]) for t in tids}
    max_lanes = min(6, max(nl for _o, nl in laid.values()))
    span = max(e for g in groups.values() for _s, e, _st in g)
    use_s = span >= 5000
    scale = 1 / 1000.0 if use_s else 1.0
    unit = "s" if use_s else "ms"

    print(f"沙箱数: {n}  时间跨度: {span * scale:.3f} {unit}  最大泳道: {max_lanes}")
    # 控制台也打印各阶段平均时长（无图环境也能读数）
    durs = defaultdict(list)
    for g in groups.values():
        for s, e, st in g:
            durs[st].append(e - s)
    print("== 各阶段平均时长 (ms)")
    for st in STAGE_ORDER:
        if durs.get(st):
            print(f"  {STAGE_LABEL[st]:<32} {sum(durs[st])/len(durs[st]):8.2f}  (n={len(durs[st])})")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:
        sys.exit("错误: 画图需要 matplotlib。请先执行: pip install matplotlib")

    combined = os.path.join(out_dir, "timeline.png")
    total = os.path.join(out_dir, "total_gantt.png")
    stages = os.path.join(out_dir, "stage_durations.png")

    draw_combined(plt, Patch, tids, laid, max_lanes, span, scale, unit, stem, combined)

    # 单色 total 甘特读 intervals.csv（enter/total 的真实起止）；缺失时跳过并提示
    intervals_csv = os.path.join(out_dir, "intervals.csv")
    pairs = read_intervals(intervals_csv) if os.path.exists(intervals_csv) else []
    if pairs:
        draw_total_gantt(plt, pairs, stem, total)
    else:
        print(f"跳过 total_gantt.png：未找到可用的 {intervals_csv}（先跑 parse_report.py）")

    draw_stage_durations(plt, Patch, tids, groups, stem, stages)


if __name__ == "__main__":
    main()
