#!/usr/bin/env python3
"""串行执行并判读验收；遇到失败保留现场输出并停止。"""
import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import paths
ROOT = paths.ROOT
SCRIPTS = paths.SCRIPTS
from check_python import check
check()
import e2b
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("mode", choices=("acceptance", "stress"))
parser.add_argument("--out", type=Path)
args = parser.parse_args()
OUT = (args.out or ROOT / "results" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + args.mode)).resolve()
OUT.mkdir(parents=True, exist_ok=False)
os.environ.setdefault("TEMPLATE", "base-gsd")
TEMPLATE = os.environ["TEMPLATE"]
summary = {"machine": os.uname().nodename, "started": datetime.now().isoformat(),
           "mode": args.mode, "template": TEMPLATE, "python": sys.version,
           "sdk": str(Path(e2b.__file__).resolve()), "conda_prefix": sys.prefix,
           "steps": [], "verdict": "RUNNING"}


def require(ok, why):
    if not ok:
        raise RuntimeError(why)


def save():
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))


def step(name, command, check=lambda text: None, env=None, json_output=None):
    record = {"name": name, "command": command, "status": "RUNNING"}
    summary["steps"].append(record)
    save()
    print("运行:", name, flush=True)
    with (OUT / (name + ".log")).open("w") as f:
        if json_output:
            with (OUT / json_output).open("w") as jout:
                result = subprocess.run(command, cwd=SCRIPTS, env=env, stdout=jout, stderr=f)
        else:
            result = subprocess.run(command, cwd=SCRIPTS, env=env, stdout=f, stderr=subprocess.STDOUT)
    require(result.returncode == 0, name + " 退出码非零，见 " + str(OUT / (name + ".log")))
    text = (OUT / (name + ".log")).read_text()
    check(text)
    record["status"] = "PASS"
    save()
    print(name, "PASS", flush=True)


def check_gsd(text):
    rows = re.findall(r"restore #.*", text)
    require(len(rows) == 8 and all("ok=True" in r and "(+0)" in r for r in rows)
            and "判定: 根盘恒定 ✓" in text, "根盘或 restore 判据不符")


def check_fixes(text):
    items = json.loads((OUT / "verify_fixes.json").read_text())
    require(Counter(i["status"] for i in items) == {"PASS": 26, "SKIP": 2}, "功能计数不是 26 PASS / 2 SKIP")
    require({i["id"] for i in items if i["status"] == "SKIP"} == {"G-19", "G-36"}, "SKIP 项与预期不符")
    require(not any(word in text for word in ("BAD ", "可疑行")), "功能日志存在 BAD 或 dmesg 可疑行")


def check_multitree(text):
    data = json.loads((OUT / "verify_multitree.json").read_text())
    require(len(data["items"]) == 12 and all(i["status"] == "PASS" for i in data["items"]), "多树应为 12 项全部 PASS")
    require(not any(word in text for word in ("BAD ", "可疑行")), "多树日志存在 BAD 或 dmesg 可疑行")
    summary["slow_calls"] = data["slow_calls"]


def check_race(text):
    rows = [json.loads(line) for line in (OUT / "clear_refs_race.jsonl").read_text().splitlines()]
    require(len(rows) == 30 and all(r["diff"] == 0 and r["restored_state"] == "T"
                                   and r["sha_eq"] and not r["wedged"] for r in rows), "竞态漏页或有效轮次不符")


def check_perf(name, text):
    data = json.loads((OUT / (name + ".json")).read_text())
    require(data.get("schema_version") == 2 and data.get("measurement_mode") == "per-tier-size"
            and data.get("restore_path") == "modified-child-to-full-baseline"
            and data.get("verdict") == "PASS", "性能脚本口径不是新版独立负载分档，或判定不通过")
    require(len(data["baseline"]) == len(data["create"]) == len(data["restore"]) == 6,
            "性能脚本应有六档全量基点、增量快照和恢复")
    for base, child, restored, size, file_kb in zip(data["baseline"], data["create"], data["restore"],
                                                  (0, 12, 24, 48, 96, 192), (0, 4096, 8192, 16384, 32768, 65536)):
        require(all(row["payload_mem_mb"] == row["mem_mb"] == size and row["file_kb"] == file_kb
                    for row in (base, child, restored)), "性能档位与实际负载不符")
        require(restored["target_checkpoint_id"] == child["base_checkpoint_id"] == base["checkpoint_id"]
                and restored["source_checkpoint_id"] == child["checkpoint_id"], "性能脚本未恢复本档基点")
        require(restored["expected_memory"]["payload_bytes"] == size * (1 << 20)
                and restored["actual_memory"] == restored["expected_memory"], "性能脚本完整内存校验不符")
    require(len(data["restore"]) == 6 and all(r["ok"] for r in data["restore"]), "性能脚本六次内容恢复未全部通过")
    require("max |SDK - journal| = 0.000 ms" in text and not re.search("FAILED|restore error|mismatch", text), "性能脚本分段或错误判据不符")


def check_probe(text):
    rows = json.loads((OUT / "whiteout.json").read_text())
    require(len(rows) == 7 and all(r.get("crashed") is False and not r.get("error") for r in rows), "whiteout 探针失败")
    require(all(not r.get("dmesg", [None, ""])[1] for r in rows), "whiteout 探针 dmesg 有命中，请人工核对")


def check_stress(directory):
    require((directory / "RESULT").read_text().startswith("DONE rc=0"), "看门狗未正常结束")
    console = directory / "console.txt"
    require(console.exists() and console.stat().st_size > 0, "缺少串口证据；检查 Nomad 认证、job 名和日志格式")
    bad = r"KASAN|BUG:|Oops|WARNING:|list_(?:add|del) corruption|list corruption|possible circular locking|possible recursive locking|sleeping function called from invalid context|ODEBUG|blocked for more than|hung_task|soft lockup|rcu.*stall|detected stalls"
    require(not re.search(bad, console.read_text(errors="replace"), re.I), "串口存在内核错误")
    require(not list(directory.glob("*-create-fail-*")), "存在 create-fail 现场")
    candidates = list(directory.glob("*-summary.json"))
    require(len(candidates) == 1, "缺少或存在多个压测汇总")
    data = json.loads(candidates[0].read_text())
    if candidates[0].name == "stress-summary.json":
        require(not data["dmesg_new_hits"], "dmesg 新报错")
        require(not {k: v for k, v in data["worker_errs_by_errno"].items() if v and k not in ("ENOENT", "ESTALE")}, "worker 意外错误")
        require(not {k: v for k, v in data["worker_heartbeat_age_s"].items() if v > 10}, "worker 心跳超时")
        require(not {k: v for k, v in data["gsd_ops"]["errors"].items() if v and k != "create:resource_exhausted"}, "gsd 意外错误")
    else:
        require(data.get("verdict") == "PASS" and not data.get("unexpected_errors"), "压测 verdict 不通过")
        require(not data.get("dmesg_hits"), "压测 dmesg 命中")


try:
    save()
    hashes = subprocess.check_output(["sha256sum", "/fc-gsd/gsd", "/fc-gsd/orchestrator",
                                     "/fc-gsd/kernels/vmlinux-6.1.158/vmlinux.bin"], text=True)
    (OUT / "deliverables.sha256").write_text(hashes)
    if args.mode == "acceptance":
        step("guest-check", [sys.executable, str(ROOT / "ops/guest_check.py")])
        step("verify_gsd", [sys.executable, "acceptance/verify_gsd.py", TEMPLATE, "8"], check_gsd)
        step("verify_fixes", [sys.executable, "acceptance/verify_fixes.py", "--template", TEMPLATE,
                              "--out-json", str(OUT / "verify_fixes.json")], check_fixes)
        step("verify_multitree", [sys.executable, "acceptance/verify_multitree.py", "--template", TEMPLATE,
                                  "--skip", "V12", "--out-json", str(OUT / "verify_multitree.json")], check_multitree)
        step("clear_refs_race", [sys.executable, "acceptance/verify_clear_refs_race.py", "--template", TEMPLATE,
                                 "--rounds", "30", "--pages", "4096", "--rate", "100000",
                                 "--jsonl", str(OUT / "clear_refs_race.jsonl")], check_race)
        for i in range(1, 4):
            name = "perf-run%d" % i
            env = dict(os.environ, THP="never", OUT_JSON=str(OUT / (name + ".json")))
            step(name, [sys.executable, "acceptance/demo_checkpoint_perf.py"],
                 lambda text: check_perf(name, text), env)
            if i != 3:
                time.sleep(8)
        summary["skipped"] = ["V12：首次部署无旧进程级对照模板", "压测另跑 stress", "未改内核补丁，不跑调试内核"]
    else:
        require(os.geteuid() == 0, "压测取串口日志需要 root")
        # 取串口脚本只在子进程读取现场服务端环境；不向日志输出配置。
        step("whiteout", [sys.executable, "probes/probe_whiteout_bug.py", TEMPLATE,
                          "--cases", "bug,ctl,nodel,masked,trap,trap,trap"], check_probe, json_output="whiteout.json")
        for name, script, mode, rounds, extra in (
            ("all", "stress_overlay_ckpt.py", "stress", "300", ["--workers", "6"]),
            ("noworkers", "stress_overlay_ckpt.py", "stress", "300", ["--workers", "0"]),
            ("multitree", "stress_multitree.py", "multitree", "200", []),
            ("deldense", "stress_delete_dense.py", "deldense", "200", []),
        ):
            directory = OUT / name
            env = dict(os.environ, SCRIPT=script, PY=sys.executable, STALL="150")
            env.setdefault("NOMAD_JOB", os.environ.get("GSD_JOB", "template-manager-system"))
            step(name, ["bash", "stress/run_stress_watch.sh", str(directory), "--template", TEMPLATE,
                        "--mode", mode, "--rounds", rounds] + extra,
                 lambda text: check_stress(directory), env)
    summary["verdict"] = "PASS"
except BaseException as exc:
    summary["verdict"] = "FAIL"
    summary["error"] = str(exc)
    if summary["steps"] and summary["steps"][-1]["status"] == "RUNNING":
        summary["steps"][-1]["status"] = "FAIL"
    print("验收停止:", exc, file=sys.stderr)
finally:
    summary["finished"] = datetime.now().isoformat()
    save()
    print("结果:", OUT, "判定:", summary["verdict"])
sys.exit(0 if summary["verdict"] == "PASS" else 1)
