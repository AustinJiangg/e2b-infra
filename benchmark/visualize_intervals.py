#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可视化沙箱(sandbox)启动耗时 —— 一张「真实时间轴 + 彩色分阶段 + 并行重叠」甘特图。

读 parse_report.py 生成的 report/timeline.csv（每行一个「沙箱-阶段」区间，列：
TraceID, stage, start_ms, end_ms；时间是相对全局最早事件的毫秒偏移，来自各阶段
埋点日志时间戳，同节点时钟、天然自洽）。每个沙箱画一条，按真实时刻摆放各阶段、
按阶段上色；并行段(configure∥uffd∥rootfs)用泳道在同一条内分层展示重叠。

用法:
  python3 visualize_intervals.py                       # 自动定位最近一次运行目录
  python3 visualize_intervals.py --run-dir runs/run_20260629_142530
  python3 visualize_intervals.py path/to/timeline.csv  # 直接画指定 timeline.csv

不带位置参数时，按 --run-dir > 环境变量 BENCH_RUN_DIR > runs/.latest 的顺序定位运行
目录（与 collect_logs.sh / parse_report.py 一致）。需要 matplotlib（pip install matplotlib）。
"""
import os
import sys
import argparse
import csv
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 阶段顺序（仅图例用）、英文标签（避免缺中文字体显示方块）、配色。
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
    if args.csv:
        return args.csv, os.path.splitext(os.path.basename(args.csv))[0], \
            os.path.join(os.path.dirname(args.csv) or ".", "timeline.png")
    from parse_report import resolve_run_dir
    run_dir = resolve_run_dir(args.run_dir, os.path.join(SCRIPT_DIR, "runs"), auto=True)
    csv_path = os.path.join(run_dir, "report", "timeline.csv")
    if not os.path.exists(csv_path):
        sys.exit(f"错误: 未找到 {csv_path}。请先运行 parse_report.py 生成报告。")
    return csv_path, os.path.basename(run_dir.rstrip("/")), \
        os.path.join(run_dir, "report", "timeline.png")


def main():
    ap = argparse.ArgumentParser(
        description="真实时间轴 + 彩色分阶段 + 并行重叠 的沙箱启动甘特图")
    ap.add_argument("csv", nargs="?", help="timeline.csv（省略则自动用最近一次运行目录的）")
    ap.add_argument("--run-dir", help="本次运行目录（默认读取 runs/.latest 指向的最近一次）")
    args = ap.parse_args()

    csv_path, stem, output = resolve_timeline_csv(args)
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

    row_h = 0.30 if n <= 60 else (0.16 if n <= 200 else 0.06)
    fig_h = min(46, max(3.5, n * row_h + 2.0))
    fig, ax = plt.subplots(figsize=(13, fig_h))
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

    ax.set_ylim(n - 0.5, -0.5)
    if n <= 60:
        ticks = list(range(n))
    else:
        step = max(1, n // 40)
        ticks = list(range(0, n, step))
    ax.set_yticks(ticks)
    ax.set_yticklabels([tids[i][:8] for i in ticks], fontsize=7)
    ax.set_xlabel(f"Time since first event ({unit}) — real timeline; parallel stages shown as lanes",
                  fontsize=10)
    ax.set_title(f"{stem} — sandbox start timeline by stage (n={n})")
    ax.grid(axis="x", linestyle=":", alpha=0.5)
    ax.margins(x=0.01)
    handles = [Patch(color=STAGE_COLOR[s], label=STAGE_LABEL[s]) for s in STAGE_ORDER]
    ax.legend(handles=handles, ncol=4, fontsize=7.5, loc="lower right", framealpha=0.9)

    fig.subplots_adjust(left=0.08, right=0.98, top=1 - 0.5 / fig_h, bottom=0.8 / fig_h)
    plt.savefig(output, dpi=150)
    print(f"已保存图片: {output}")


if __name__ == "__main__":
    main()
