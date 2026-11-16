#!/usr/bin/env python3
"""校验三轮新版分档结果，输出含逐轮耗时的摘要与中位数表。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import median

from demo_checkpoint_perf import EXPERIMENTS, check_timings, require


def summarize(paths):
    require(len(paths) == 3, "需要三轮独立结果")
    require(len({p.resolve() for p in paths}) == 3, "不能重复使用同一个结果文件")
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    for run in runs:
        require(run["verdict"] == "PASS" and run["schema_version"] == 2, "测试未通过或口径不是 v2")
        require(run["measurement_mode"] == "per-tier-size", "不是独立分档口径")
        require(run["restore_path"] == "modified-child-to-full-baseline", "恢复路径不一致")
        require(run["samples_per_tier"] == 1, "每轮每档应为一个样本")
        require(run["thp"] == "never", "正式结果要求 THP=never")
        require(run["template"] == runs[0]["template"], "三轮模板别名不一致")
        require(all(len(run[k]) == len(EXPERIMENTS) for k in ("baseline", "create", "restore")),
                "分档结果不完整")
        check_timings(run)
        for i, exp in enumerate(EXPERIMENTS):
            base, child, restored = (run[k][i] for k in ("baseline", "create", "restore"))
            for row in (base, child, restored):
                require(row["payload_mem_mb"] == row["mem_mb"] == exp["mem_mb"]
                        and row["file_kb"] == exp["file_kb"], "负载分档不一致")
                require(all(math.isfinite(row[k]) and row[k] > 0 for k in ("e2e_ms",)),
                        "墙钟耗时无效")
                require(math.isfinite(row["sdk"]["total_ms"]) and row["sdk"]["total_ms"] > 0,
                        "服务端耗时无效")
            require(base["meta"]["parent_checkpoint_id"] is None, "基点不是全量")
            require(child["base_checkpoint_id"] == restored["target_checkpoint_id"] == base["checkpoint_id"],
                    "未恢复本档基点")
            require(child["meta"]["parent_checkpoint_id"] == base["checkpoint_id"], "增量父节点错误")
            require(restored["ok"] and restored["mem_ok"] and restored["file_ok"], "恢复内容校验失败")
            require(restored["expected_memory"]["payload_bytes"] == exp["mem_mb"] << 20,
                    "恢复负载大小错误")
            require(restored["actual_memory"] == restored["expected_memory"], "完整内存状态不一致")
    rows = []
    for i, exp in enumerate(EXPERIMENTS):
        row = {"nominal_total_mib": exp["mem_mb"] + exp["file_kb"] // 1024,
               "payload_mem_mib": exp["mem_mb"], "file_mib": exp["file_kb"] // 1024,
               "rss_mib_samples": [r["restore"][i]["before_usage"]["rss_bytes"] / (1 << 20) for r in runs]}
        for phase in ("baseline", "create", "restore"):
            server = [r[phase][i]["sdk"]["total_ms"] for r in runs]
            wall = [r[phase][i]["e2e_ms"] for r in runs]
            row[phase] = {"n": 3, "server_ms_samples": server, "server_p50_ms": median(server),
                          "wall_ms_samples": wall, "wall_p50_ms": median(wall)}
        rows.append(row)
    return {"schema_version": 2, "measurement_mode": "per-tier-size",
            "restore_path": "modified-child-to-full-baseline", "template": runs[0]["template"],
            "thp": "never", "verdict": "PASS", "restore_checks_passed": 18,
            "max_sdk_journal_diff_ms": max(r["max_sdk_journal_diff_ms"] for r in runs),
            "sources": [{"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths],
            "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs=3, type=Path, help="三轮 perf-run JSON，顺序保持固定")
    parser.add_argument("--output", type=Path, help="写入摘要 JSON；不指定则只校验并打印表格")
    args = parser.parse_args()
    try:
        result = summarize(args.runs)
    except (AssertionError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"判定: FAIL：{exc}\n")
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("| 总改动 MiB | 内存/文件 MiB | 增量快照 ms | 回滚 ms | RSS MiB |")
    print("|---|---|---|---|---|")
    for row in result["rows"]:
        print(f"| {row['nominal_total_mib']} | {row['payload_mem_mib']}/{row['file_mib']} | "
              f"{row['create']['server_p50_ms']:.1f} | {row['restore']['server_p50_ms']:.1f} | "
              f"{median(row['rss_mib_samples']):.2f} |")
    print("判定: PASS；三轮、六档、18 次完整恢复校验，SDK/journal 一致")


if __name__ == "__main__":
    main()
