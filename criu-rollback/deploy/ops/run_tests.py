#!/usr/bin/env python3
"""串行执行并判读验收；遇到失败保留现场输出并停止。

模式：
  quick       部署自检 + verify_gsd（约 2 分钟），每次切栈、换二进制后用
  acceptance  功能验收与分档性能（约 4 分钟）
  stress      whiteout 探针 + all / noworkers / multitree / deldense 四组（约 4 分钟）
  stress-ext  八组压测各 1000 轮（含 enospc）、多棵树与删文件密集各 3 个种子 200 轮加一次 1000 轮容量耗尽、
              all / noworkers 各 5 个种子，最后跑兼容矩阵（checkpoint → pause → resume → restore）
  soak        长跑：一个沙箱里按时长连续 checkpoint / restore / delete（--hours，默认 4），
              前后对比宿主资源
"""
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
import stack
ROOT = paths.ROOT
SCRIPTS = paths.SCRIPTS
from check_python import check
check()
import e2b
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("mode", choices=("quick", "acceptance", "stress", "stress-ext", "soak"))
parser.add_argument("--out", type=Path)
parser.add_argument("--hours", type=float, default=4.0, help="soak 的时长（小时）")
args = parser.parse_args()
OUT = (args.out or ROOT / "results" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + args.mode)).resolve()
OUT.mkdir(parents=True, exist_ok=False)
os.environ.setdefault("TEMPLATE", os.environ.get("CRIU_TEMPLATE", "base-criu"))
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


def check_enospc(directory):
    data = json.loads((directory / "enospc-summary.json").read_text())
    rec = data["recovery"]
    require(not data["dmesg_new_hits"], "enospc：dmesg 新报错")
    require(rec["restore_c0"] and rec["create_after"] and rec["restore_after"], "enospc：删掉填充后 checkpoint / restore 没有恢复")
    require(rec["c0_content"].strip() == "v0" and rec["after_content"].strip() == "v2", "enospc：恢复后的内容不对")


def check_console(directory):
    console = directory / "console.txt"
    require(console.exists() and console.stat().st_size > 0, "缺少串口证据；检查 Nomad 认证、job 名和日志格式")
    bad = r"KASAN|BUG:|Oops|WARNING:|list_(?:add|del) corruption|list corruption|possible circular locking|possible recursive locking|sleeping function called from invalid context|ODEBUG|blocked for more than|hung_task|soft lockup|rcu.*stall|detected stalls"
    require(not re.search(bad, console.read_text(errors="replace"), re.I), "串口存在内核错误")


# 手册第 18 章 §5.1 的分组：组名只是约定，脚本只认 --workers 与 --ops
OPS_GROUPS = {
    "all": ["--workers", "6"],
    "readonly": ["--workers", "6", "--ops", "stat,read,lsroot,lsdeep,scandir,relcat,rells"],
    "wr3": ["--workers", "6", "--ops", "append,rename,reltouch"],
    "rename": ["--workers", "6", "--ops", "rename"],
    "append": ["--workers", "6", "--ops", "append"],
    "reltouch": ["--workers", "6", "--ops", "reltouch"],
    "noworkers": ["--workers", "0"],
}


def stress_group(name, script, mode, rounds, extra, stall="150"):
    directory = OUT / name
    env = dict(os.environ, SCRIPT=script, PY=sys.executable, STALL=stall)
    env.setdefault("NOMAD_JOB", os.environ.get("CRIU_JOB", "template-manager-system"))
    check = (lambda text: check_enospc(directory) or check_console(directory)) if mode == "enospc" \
        else (lambda text: check_stress(directory))
    step(name, ["bash", "stress/run_stress_watch.sh", str(directory), "--template", TEMPLATE,
                "--mode", mode, "--rounds", str(rounds)] + extra, check, env)


FAILURES = []


def keep_going(fn, *args, **kwargs):
    """stress-ext 里各组互不依赖：一组失败记下来，接着跑下一组，最后统一判 FAIL。"""
    try:
        fn(*args, **kwargs)
    except RuntimeError as exc:
        record = summary["steps"][-1]
        record["status"] = "FAIL"
        record["error"] = str(exc)
        FAILURES.append("%s：%s" % (record["name"], exc))
        save()
        print(record["name"], "FAIL", exc, flush=True)


def host_resources():
    """长跑前后对比的宿主资源：内存、netns、firecracker、orchestrator 的 RSS 与 fd、产物目录。"""
    def sh(cmd):
        return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()
    mem = dict(l.split(":", 1) for l in open("/proc/meminfo").read().splitlines())
    orch = sh("for p in /proc/[0-9]*; do e=$(readlink $p/exe 2>/dev/null) || continue; "
              "case ${e##*/} in orchestrator*|template-manager*) echo ${p#/proc/}; break;; esac; done")
    res = {"time": datetime.now().isoformat(),
           "MemAvailable_kB": int(mem["MemAvailable"].split()[0]),
           "netns": stack.e2b_netns(),
           "firecracker": len(stack.e2b_firecrackers()),
           "orchestrator_pid": orch}
    if orch:
        res["orchestrator_rss_kB"] = int(sh("awk '/VmRSS/{print $2}' /proc/%s/status" % orch) or 0)
        res["orchestrator_fds"] = len(os.listdir("/proc/%s/fd" % orch))
    res["disk_used_kB"] = sh("df -k --output=used / | tail -1")
    return res


try:
    save()
    fc = paths.host_fc()
    hashes = subprocess.check_output(["sha256sum", str(fc / "gsd"), str(fc / "orchestrator"),
                                     str(fc / "kernels/vmlinux-6.1.158/vmlinux.bin")], text=True)
    (OUT / "deliverables.sha256").write_text(hashes)
    if args.mode == "quick":
        step("guest-check", [sys.executable, str(ROOT / "ops/guest_check.py")])
        step("verify_gsd", [sys.executable, "acceptance/verify_gsd.py", TEMPLATE, "8"], check_gsd)
    elif args.mode == "acceptance":
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
    elif args.mode == "stress-ext":
        require(os.geteuid() == 0, "压测取串口日志需要 root")
        for name, extra in OPS_GROUPS.items():
            keep_going(stress_group, name, "stress_overlay_ckpt.py", "stress", 1000, extra)
        keep_going(stress_group, "enospc", "stress_overlay_ckpt.py", "enospc", 1, [])
        # 多棵树、删文件密集两个脚本删得少，checkpoint 不封顶：200 轮在容量以内（看稳定性，多种子），
        # 1000 轮会把 guest 内存或根盘用满（看容量耗尽时是否仍安全失败：不触发 OOM 杀用户进程、
        # restore 不报 data_loss）。2026-11-17 在 920B 上 1000 轮两组都未通过，见手册第 23 章缺口表
        for seed in range(1, 4):
            keep_going(stress_group, "multitree-seed%d" % seed, "stress_multitree.py", "multitree", 200,
                       ["--seed", str(seed)])
            keep_going(stress_group, "deldense-seed%d" % seed, "stress_delete_dense.py", "deldense", 200,
                       ["--seed", str(seed)])
        keep_going(stress_group, "multitree-capacity", "stress_multitree.py", "multitree", 1000, [])
        keep_going(stress_group, "deldense-capacity", "stress_delete_dense.py", "deldense", 1000, [])
        for seed in range(1, 6):
            for name in ("all", "noworkers"):
                keep_going(stress_group, "%s-seed%d" % (name, seed), "stress_overlay_ckpt.py", "stress", 300,
                           OPS_GROUPS[name] + ["--seed", str(seed)])
        # checkpoint → pause → resume → restore（手册第 16 章 T-09）与其他原生操作的组合
        # 兼容矩阵只读环境变量里的凭据，这里用进程级脚本的 e2b_env 取一份传给它
        sys.path.insert(0, str(SCRIPTS))
        from e2b_env import load_e2b_env
        keep_going(step, "compat_matrix", [sys.executable, str(paths.REPO / "rollback/scripts/acceptance/compat_matrix.py"),
                                           "--template", TEMPLATE, "--out", str(OUT / "compat_matrix.json")],
                   env=dict(os.environ, **load_e2b_env()))
        require(not FAILURES, "%d 组未通过：%s" % (len(FAILURES), "；".join(FAILURES)))
    elif args.mode == "soak":
        require(os.geteuid() == 0, "压测取串口日志需要 root")
        seconds = int(args.hours * 3600)
        summary["hours"] = args.hours
        summary["resources_before"] = host_resources()
        save()
        # 一个沙箱、六个 worker、十种文件操作，按时长连续跑；看门狗停滞阈值放宽到 300 秒
        stress_group("soak", "stress_overlay_ckpt.py", "stress", 10000000,
                     OPS_GROUPS["all"] + ["--seconds", str(seconds), "--seed", "20261116"], stall="300")
        after = summary["resources_after"] = host_resources()
        before = summary["resources_before"]
        summary["resources_delta"] = {k: after[k] - before[k] for k in before
                                      if isinstance(before[k], int) and isinstance(after.get(k), int)}
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
            env.setdefault("NOMAD_JOB", os.environ.get("CRIU_JOB", "template-manager-system"))
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
