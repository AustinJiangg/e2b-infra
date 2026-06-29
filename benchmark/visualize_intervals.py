#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可视化沙箱(sandbox)启动耗时——按启动区间画甘特图。

输入 CSV 共两行：
  第一行: 每次启动的开始时间
  第二行: 每次启动的结束时间
  每一列是一次启动。时间格式形如 2026-06-23T15:22:24.451+08:00
这正是 parse_report.py 自动写出的 report/intervals.csv 的格式。

用法:
  python3 visualize_intervals.py                    # 自动定位最近一次运行目录，
                                                    # 读取 report/intervals.csv，
                                                    # 输出 report/intervals.png
  python3 visualize_intervals.py --run-dir runs/run_20260629_142530   # 指定某次运行
  python3 visualize_intervals.py xxx.csv            # 兼容老用法：输出同名 xxx.png

不带位置参数时，按 --run-dir > 环境变量 BENCH_RUN_DIR > runs/.latest 的顺序定位运行
目录（与 collect_logs.sh / parse_report.py 一致）。

输出:
  - 甘特图(每次启动一条横条，按开始时间排序)
  - 控制台打印: 启动次数、总跨度、单次启动耗时
"""
import os
import sys
import argparse
import csv
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_time(s: str) -> datetime:
    s = s.strip()
    # datetime.fromisoformat 支持 +08:00 这种带冒号的时区
    return datetime.fromisoformat(s)


def read_intervals(path):
    # utf-8-sig 兼容带/不带 BOM 的 CSV（Excel 导出的老文件可能带 BOM）
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


def resolve_paths(args):
    """根据是否显式给了 csv，决定 输入 CSV / 输出 PNG / 标题前缀。"""
    if args.csv:
        csv_path = args.csv
        stem = os.path.splitext(os.path.basename(csv_path))[0]  # xxx
        output = os.path.join(os.path.dirname(csv_path) or ".", stem + ".png")
        return csv_path, output, stem
    # 自动定位运行目录，复用 parse_report 的同一套约定（仅标准库，不会引入 matplotlib）
    from parse_report import resolve_run_dir
    runs_root = os.path.join(SCRIPT_DIR, "runs")
    run_dir = resolve_run_dir(args.run_dir, runs_root, auto=True)
    csv_path = os.path.join(run_dir, "report", "intervals.csv")
    if not os.path.exists(csv_path):
        sys.exit(f"错误: 未找到 {csv_path}。\n"
                 "  请先运行 parse_report.py 生成报告（会自动写出 intervals.csv）。")
    stem = os.path.basename(run_dir.rstrip("/"))  # 如 run_20260629_142530
    output = os.path.join(run_dir, "report", "intervals.png")
    return csv_path, output, stem


def main():
    ap = argparse.ArgumentParser(description="可视化 CSV 中的沙箱启动时间区间（甘特图）")
    ap.add_argument("csv", nargs="?",
                    help="输入 CSV（省略则自动用最近一次运行目录的 report/intervals.csv）")
    ap.add_argument("--run-dir", help="本次运行目录（默认读取 runs/.latest 指向的最近一次）")
    args = ap.parse_args()

    csv_path, output, stem = resolve_paths(args)

    intervals = read_intervals(csv_path)
    n = len(intervals)
    if n == 0:
        sys.exit("没有读到任何区间。")

    span_start = min(i[0] for i in intervals)
    span_end = max(i[1] for i in intervals)
    durations = [(e - s).total_seconds() for s, e, _ in intervals]

    print(f"区间数: {n}")
    print(f"时间跨度: {span_start}  →  {span_end}  "
          f"({(span_end - span_start).total_seconds():.3f} 秒)")
    print(f"单区间时长: 最短 {min(durations)*1000:.1f} ms, "
          f"最长 {max(durations)*1000:.1f} ms, "
          f"平均 {sum(durations)/n*1000:.1f} ms")

    # matplotlib 延迟导入：未安装时给清晰提示（控制台统计已先打印）
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("错误: 画图需要 matplotlib。请先执行: pip install matplotlib")

    # 按开始时间排序后画甘特图
    ordered = sorted(intervals, key=lambda x: x[0])

    # 行高随数量自适应，限制总高度，适配几十到上千个沙箱
    row_h = 0.22 if n <= 60 else (0.12 if n <= 200 else 0.05)
    fig_h = min(40, max(3.5, n * row_h + 1.4))
    fig, ax = plt.subplots(figsize=(12, fig_h))

    bar_h = 0.6 if n <= 200 else 0.8
    total_span = (span_end - span_start).total_seconds()
    # x 轴用相对起点的时间(秒)，跨度小用 ms 显示，大用 s
    use_ms = total_span < 10
    scale = 1000.0 if use_ms else 1.0
    unit = "ms" if use_ms else "s"

    def rel(t):  # 距起点的秒数
        return (t - span_start).total_seconds()

    min_w = total_span * 0.002 * scale  # 极短区间至少给一点可见宽度
    for row, (s, e, orig_i) in enumerate(ordered):
        left = rel(s) * scale
        width = max((rel(e) - rel(s)) * scale, min_w)
        ax.barh(row, width, left=left,
                height=bar_h, color="#1f77b4", alpha=0.85)

    # 紧贴数据，上下只留半行余量；倒序使第一条在顶部
    ax.set_ylim(n - 0.5, -0.5)
    # 数量太多时稀疏显示 y 轴编号，避免重叠
    if n <= 60:
        ticks = list(range(n))
    else:
        step = max(1, n // 40)
        ticks = list(range(0, n, step))
    ax.set_yticks(ticks)
    ax.set_yticklabels([f"#{ordered[i][2]}" for i in ticks], fontsize=7)
    title = f"{stem} — {n} sandbox launches"
    ax.set_title(title)
    ax.set_xlabel(f"Time since first launch ({unit})", fontsize=10)
    ax.grid(axis="x", linestyle=":", alpha=0.5)
    ax.margins(x=0.02)  # 左右留一点边距，横条不贴边

    # 图片底部的统计信息(英文)
    sorted_d = sorted(durations)
    m = len(sorted_d)
    median = (sorted_d[m // 2] if m % 2 else
              (sorted_d[m // 2 - 1] + sorted_d[m // 2]) / 2)
    stats = (
        f"Launches: {n}      Total span: {total_span:.3f} s\n"
        f"Launch duration min/median/mean/max: "
        f"{min(durations)*1000:.1f} / {median*1000:.1f} / "
        f"{sum(durations)/n*1000:.1f} / {max(durations)*1000:.1f} ms"
    )

    # 用固定英寸预留各边距，保证不同图高下间距观感一致
    top_in, xlabel_in, footer_in = 0.5, 0.8, 0.7
    fig.subplots_adjust(
        left=0.07, right=0.97,
        top=1 - top_in / fig_h,
        bottom=(xlabel_in + footer_in) / fig_h,
    )
    # 统计文字固定贴在坐标区下方(距底边固定英寸)，间距不随图高变化
    ax_bottom = ax.get_position().y0
    fig.text(0.5, ax_bottom - (xlabel_in + footer_in * 0.45) / fig_h, stats,
             ha="center", va="center", fontsize=12, family="monospace")

    plt.savefig(output, dpi=150)
    print(f"已保存图片: {output}")


if __name__ == "__main__":
    main()
