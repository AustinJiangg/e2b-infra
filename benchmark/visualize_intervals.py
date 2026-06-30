#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可视化沙箱(sandbox)启动耗时。自动定位最近一次运行目录后，画两张图：

  1) stages.png  —— 每个沙箱一条「分阶段堆叠彩色条」(读 report/stages.csv)。
     不同阶段不同颜色，一眼看出时间花在哪：准入排队 / 拉起FC进程 / 等FC socket /
     加载快照 / 启动envd / … 详见 高并发瓶颈定位方案.md 第 6 节。
  2) intervals.png —— 启动区间甘特图(读 report/intervals.csv)，按开始时间铺开，
     看高并发下的并发节奏与排队铺开(单色，时间线视角)。

用法:
  python3 visualize_intervals.py                       # 自动定位最近一次运行目录
  python3 visualize_intervals.py --run-dir runs/run_20260629_142530
  python3 visualize_intervals.py xxx.csv               # 兼容老用法：把 xxx.csv 当区间
                                                       # 两行格式画甘特图，输出 xxx.png

不带位置参数时，按 --run-dir > 环境变量 BENCH_RUN_DIR > runs/.latest 的顺序定位运行
目录（与 collect_logs.sh / parse_report.py 一致）。需要 matplotlib（pip install matplotlib）。
"""
import os
import sys
import argparse
import csv
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# stages.csv 的阶段列（与 parse_report.py 的 COARSE_ORDER 一致），堆叠按此顺序。
# 用英文图例避免 matplotlib 缺中文字体时显示方块。颜色按方案第 6 节。
STAGE_COLS = ["准入排队", "恢复准备", "创建FC进程", "拉起FC进程",
              "等FC_socket", "加载快照", "启动envd", "其他"]
STAGE_EN = {
    "准入排队": "queue (acquire wait)",
    "恢复准备": "prep (net slot/meta)",
    "创建FC进程": "fc.NewProcess",
    "拉起FC进程": "fc spawn",
    "等FC_socket": "fc socket wait",
    "加载快照": "load snapshot",
    "启动envd": "start envd",
    "其他": "other (resume tail/overhead)",
}
STAGE_COLORS = {
    "准入排队": "#9e9e9e",     # gray  —— total 外
    "恢复准备": "#a6cee3",     # light blue
    "创建FC进程": "#17becf",   # cyan
    "拉起FC进程": "#ff7f0e",   # orange
    "等FC_socket": "#d62728",  # red —— 通常最大
    "加载快照": "#9467bd",     # purple
    "启动envd": "#2ca02c",     # green
    "其他": "#d9d9d9",         # light gray
}


def parse_time(s: str) -> datetime:
    # datetime.fromisoformat 支持 +08:00 这种带冒号的时区
    return datetime.fromisoformat(s.strip())


def read_intervals(path):
    # utf-8-sig 兼容带/不带 BOM 的 CSV
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    if len(rows) < 2:
        sys.exit("错误: CSV 至少需要两行(开始时间行、结束时间行)。")
    starts_raw, ends_raw = rows[0], rows[1]
    if len(starts_raw) != len(ends_raw):
        sys.exit(f"错误: 开始时间数({len(starts_raw)}) 与结束时间数({len(ends_raw)}) 不一致。")
    intervals = []
    for i, (s, e) in enumerate(zip(starts_raw, ends_raw)):
        if not s.strip() and not e.strip():
            continue
        intervals.append((parse_time(s), parse_time(e), i))
    return intervals


def read_stages(path):
    """读 stages.csv -> [{stage: ms, ..., 'total': ms, 'tid': str}]，跳过坏行。"""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            v = {}
            try:
                for k in STAGE_COLS:
                    v[k] = float(r.get(k) or 0.0)
                v["total"] = float(r.get("total") or 0.0)
            except ValueError:
                continue
            v["tid"] = (r.get("TraceID") or "")[:8]
            rows.append(v)
    return rows


def _load_mpl():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        sys.exit("错误: 画图需要 matplotlib。请先执行: pip install matplotlib")


def _yticks(ax, n, labels):
    """数量多时稀疏显示 y 轴标签，避免重叠。"""
    if n <= 60:
        ticks = list(range(n))
    else:
        step = max(1, n // 40)
        ticks = list(range(0, n, step))
    ax.set_yticks(ticks)
    ax.set_yticklabels([labels[i] for i in ticks], fontsize=7)


def draw_stages(stages_csv, output, stem):
    """每个沙箱一条分阶段堆叠彩色条（按 准入排队+total 降序，最慢在顶部）。"""
    rows = read_stages(stages_csv)
    if not rows:
        print(f"提示: {stages_csv} 无可用数据，跳过 stages 图。")
        return
    rows.sort(key=lambda v: v["准入排队"] + v["total"], reverse=True)
    n = len(rows)
    plt = _load_mpl()
    from matplotlib.patches import Patch

    row_h = 0.22 if n <= 60 else (0.12 if n <= 200 else 0.05)
    fig_h = min(42, max(3.5, n * row_h + 1.8))
    fig, ax = plt.subplots(figsize=(12, fig_h))
    bar_h = 0.6 if n <= 200 else 0.85

    for row, v in enumerate(rows):
        left = 0.0
        for k in STAGE_COLS:
            w = v[k]
            if w <= 0:
                continue
            ax.barh(row, w, left=left, height=bar_h, color=STAGE_COLORS[k])
            left += w

    ax.set_ylim(n - 0.5, -0.5)
    _yticks(ax, n, [f"{rows[i]['tid']}" for i in range(n)])
    ax.set_xlabel("Duration (ms) = queue (acquire wait) + ResumeSandbox stages", fontsize=10)
    ax.set_title(f"{stem} — per-sandbox stage breakdown (n={n}, sorted by queue+total)")
    ax.grid(axis="x", linestyle=":", alpha=0.5)
    ax.margins(x=0.02)
    handles = [Patch(color=STAGE_COLORS[k], label=STAGE_EN[k]) for k in STAGE_COLS]
    ax.legend(handles=handles, ncol=4, fontsize=8, loc="upper right", framealpha=0.9)

    # 控制台也打印各阶段平均，便于无图环境快速读数
    print(f"== 分阶段平均 (ms, n={n})")
    for k in STAGE_COLS:
        print(f"  {STAGE_EN[k]:<28} {sum(v[k] for v in rows)/n:8.2f}")

    fig.subplots_adjust(left=0.07, right=0.97, top=1 - 0.5 / fig_h, bottom=0.7 / fig_h)
    plt.savefig(output, dpi=150)
    print(f"已保存图片: {output}")


def draw_timeline(intervals, output, stem):
    """启动区间甘特图（按开始时间排序，单色，时间线视角看并发铺开）。"""
    n = len(intervals)
    if n == 0:
        print("提示: 无区间数据，跳过 timeline 图。")
        return
    span_start = min(i[0] for i in intervals)
    span_end = max(i[1] for i in intervals)
    durations = [(e - s).total_seconds() for s, e, _ in intervals]
    print(f"区间数: {n}  跨度: {(span_end - span_start).total_seconds():.3f}s  "
          f"单区间 min/median/mean/max: {min(durations)*1000:.1f}/"
          f"{sorted(durations)[n//2]*1000:.1f}/{sum(durations)/n*1000:.1f}/"
          f"{max(durations)*1000:.1f} ms")

    plt = _load_mpl()
    ordered = sorted(intervals, key=lambda x: x[0])
    row_h = 0.22 if n <= 60 else (0.12 if n <= 200 else 0.05)
    fig_h = min(40, max(3.5, n * row_h + 1.4))
    fig, ax = plt.subplots(figsize=(12, fig_h))
    bar_h = 0.6 if n <= 200 else 0.8
    total_span = (span_end - span_start).total_seconds()
    use_ms = total_span < 10
    scale = 1000.0 if use_ms else 1.0
    unit = "ms" if use_ms else "s"

    def rel(t):
        return (t - span_start).total_seconds()

    min_w = max(total_span * 0.002 * scale, 1e-9)
    for row, (s, e, _orig_i) in enumerate(ordered):
        left = rel(s) * scale
        width = max((rel(e) - rel(s)) * scale, min_w)
        ax.barh(row, width, left=left, height=bar_h, color="#1f77b4", alpha=0.85)

    ax.set_ylim(n - 0.5, -0.5)
    _yticks(ax, n, [f"#{ordered[i][2]}" for i in range(n)])
    ax.set_title(f"{stem} — {n} sandbox launches (timeline)")
    ax.set_xlabel(f"Time since first launch ({unit})", fontsize=10)
    ax.grid(axis="x", linestyle=":", alpha=0.5)
    ax.margins(x=0.02)
    fig.subplots_adjust(left=0.07, right=0.97, top=1 - 0.5 / fig_h, bottom=0.9 / fig_h)
    plt.savefig(output, dpi=150)
    print(f"已保存图片: {output}")


def main():
    ap = argparse.ArgumentParser(description="可视化沙箱启动耗时（分阶段堆叠 + 区间甘特）")
    ap.add_argument("csv", nargs="?",
                    help="输入区间 CSV（省略则自动用最近一次运行目录的 report/）；"
                         "给了就只画该 CSV 的区间甘特图（兼容老用法）")
    ap.add_argument("--run-dir", help="本次运行目录（默认读取 runs/.latest 指向的最近一次）")
    args = ap.parse_args()

    # 兼容老用法：显式给区间 CSV，只画甘特图，输出同名 .png
    if args.csv:
        stem = os.path.splitext(os.path.basename(args.csv))[0]
        output = os.path.join(os.path.dirname(args.csv) or ".", stem + ".png")
        draw_timeline(read_intervals(args.csv), output, stem)
        return

    # 自动定位运行目录（复用 parse_report 的约定，仅标准库）
    from parse_report import resolve_run_dir
    runs_root = os.path.join(SCRIPT_DIR, "runs")
    run_dir = resolve_run_dir(args.run_dir, runs_root, auto=True)
    report = os.path.join(run_dir, "report")
    stem = os.path.basename(run_dir.rstrip("/"))
    stages_csv = os.path.join(report, "stages.csv")
    intervals_csv = os.path.join(report, "intervals.csv")

    did = False
    if os.path.exists(stages_csv):
        draw_stages(stages_csv, os.path.join(report, "stages.png"), stem)
        did = True
    if os.path.exists(intervals_csv):
        draw_timeline(read_intervals(intervals_csv), os.path.join(report, "intervals.png"), stem)
        did = True
    if not did:
        sys.exit(f"错误: 未找到 {stages_csv} 或 {intervals_csv}。\n"
                 "  请先运行 parse_report.py 生成报告。")


if __name__ == "__main__":
    main()
