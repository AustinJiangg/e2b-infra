#!/usr/bin/env python3
"""950 测试总入口：按档调度两套快照回滚的测试，自动切栈、记录身份、可续跑、打一个回传包。

不重写任何测试，只编排现有脚本：
  沙箱级  rollback/scripts/950/run.sh 的各档
  进程级  criu-rollback/deploy/950.sh 的 quick / acceptance / stress / stress-ext / soak
另有两项单独的性能测量：native（原生 pause / resume）、compare（同一负载下的 checkpoint / restore）。
切栈用 950.sh criu / sandbox，跑完停在沙箱级。只用标准库。
结果都留在 test950/results/<时间>-<档>/，用 bash test950.sh report 打印到终端（不打包、不需要拷文件）。
e2b 的网络命名空间超过 TEST950_NETNS_LIMIT（默认 1500）时，在下一步之前自动 950.sh recycle-netns。
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEPLOY = REPO / "criu-rollback" / "deploy"
RUN_SH = REPO / "rollback" / "scripts" / "950" / "run.sh"
STACK_SH = DEPLOY / "950.sh"
sys.path.insert(0, str(DEPLOY / "ops"))
import envs  # noqa: E402
import paths  # noqa: E402
import report  # noqa: E402
import stack  # noqa: E402


def state_dir():
    """进度与后台日志。"""
    return Path(os.environ.get("TEST950_STATE") or "/var/lib/test950")


NETNS_LIMIT = int(os.environ.get("TEST950_NETNS_LIMIT", "1500"))
RESULTS = HERE / "results"
LEVELS = ("quick", "standard", "extended", "long")
# 与四档并列、单独执行的两项性能测量（不进 quick ⊂ standard ⊂ extended，也不进 long）：
#   native   e2b 原生 pause / resume 分档（benchmark/native_snapshot_tiers.py），在所选栈上各跑一遍
#   compare  同一负载下的 checkpoint / restore（benchmark/ckpt_same_workload.py），两套各跑一遍
EXTRA = ("native", "compare")
TEMPLATES = {"sandbox": os.environ.get("SANDBOX_TEMPLATE", "base"),
             "criu": os.environ.get("CRIU_TEMPLATE", "base-criu")}
STACKS = ("sandbox", "criu")

# 每一步：(栈, 名字, 命令类型, 参数, 预估分钟)。命令类型 run = 沙箱级 run.sh，criu = 950.sh 的测试命令
SANDBOX_STEPS = {
    "quick": [("smoke", "run", ["smoke"], 3)],
    # 预估（分钟）：standard 按 950 2026-11-17 实测（smoke 1、func 18.5、perf 9、hdbss 4）；
    # extended 按 920B 2026-11-18 实测（special 10.8、perf-full 5.7、idle 9.5），perf-full 按 950 上
    # perf 约为 920B 的 4 倍放大；hdbss-orders 在 920B 上不跑（非 HDBSS），950 上每个 order 重启一次 job
    "standard": [("smoke", "run", ["smoke"], 1),
                 ("func", "run", ["func"], 20),
                 ("perf", "run", ["perf"], 10),
                 ("hdbss", "run", ["hdbss"], 5)],
    "extended": [("special", "run", ["special", "--allow-restart", "--kill-sandboxes-ok"], 12),
                 ("hdbss-orders", "run", ["hdbss", "--orders", "1,9", "--allow-restart", "--kill-sandboxes-ok"], 10),
                 ("perf-full", "run", ["perf", "--full"], 20),
                 ("idle", "run", ["idle"], 10)],
    "long": [("soak", "run", ["soak", "--hours", "{hours}"], None)],
}
CRIU_STEPS = {
    "quick": [("quick", "criu", ["quick"], 2)],
    "standard": [("acceptance", "criu", ["acceptance"], 4),   # 950 实测 3 分 09 秒
                 ("stress", "criu", ["stress"], 5)],          # 950 实测 3 分 52 秒，另有 netns 清理约 2 分钟
    "extended": [("stress-ext", "criu", ["stress-ext"], 30)],    # 920B 实测 28 分钟
    "long": [("soak", "criu", ["soak", "--hours", "{hours}"], None)],
}


def plan(level, stacks, hours):
    """按档展开步骤。quick ⊂ standard ⊂ extended；long 单独，只含长跑；native、compare 各自单独。"""
    if level in EXTRA:
        steps = []
        for st in stacks:
            if level == "native":
                args = ["benchmark/native_snapshot_tiers.py", "--mode", "both", "--passes", "3", "--iters", "8",
                        "--template", TEMPLATES[st]]
                est = 8      # 920B 实测每套 7.5 分钟
            else:
                args = ["benchmark/ckpt_same_workload.py", "--stack", st, "--template", TEMPLATES[st]]
                est = 4      # 920B 实测每套 2.5～3 分钟
            steps.append({"id": "%s/%s" % (st, level), "stack": st, "kind": "py", "args": args,
                          "est_min": est, "status": "待跑"})
        if "criu" in stacks:
            steps.append({"id": "sandbox/smoke-after", "stack": "sandbox", "kind": "run",
                          "args": ["smoke"], "est_min": 1, "status": "待跑"})
        return steps
    order = {"quick": ["quick"], "standard": ["standard"], "extended": ["standard", "extended"],
             "long": ["long"]}[level]
    steps = []
    for stack in stacks:
        table = SANDBOX_STEPS if stack == "sandbox" else CRIU_STEPS
        for lv in order:
            for name, kind, args, est in table[lv]:
                args = [a.replace("{hours}", str(hours)) for a in args]
                if est is None:
                    est = int(hours * 60) + 30
                steps.append({"id": "%s/%s" % (stack, name), "stack": stack, "kind": kind,
                              "args": args, "est_min": est, "status": "待跑"})
    # 进程级测完回到沙箱级，再跑一次沙箱级冒烟，确认原服务完好
    if "criu" in stacks:
        steps.append({"id": "sandbox/smoke-after", "stack": "sandbox", "kind": "run",
                      "args": ["smoke"], "est_min": 1, "status": "待跑"})
    return steps


SWITCH_MIN = 2     # 每次切栈（950.sh criu / sandbox）约 1～2 分钟；netns 清理每次约 2 分钟另计


def estimate(steps):
    """还没通过的步骤的预估合计，加上其间的切栈。"""
    todo = [s for s in steps if s["status"] != "PASS"]
    switches = sum(1 for a, b in zip(todo, todo[1:]) if a["stack"] != b["stack"])
    return sum(s["est_min"] for s in todo) + switches * SWITCH_MIN, switches


def dur(seconds):
    return "%d 秒" % seconds if seconds < 60 else "%d 分 %02d 秒" % (seconds // 60, seconds % 60)


def log(*parts):
    line = "%s %s" % (datetime.now().strftime("%H:%M:%S"), " ".join(str(p) for p in parts))
    print(line, flush=True)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def sha(path):
    out = sh("sha256sum %s 2>/dev/null" % path)
    return out.split()[0] if out else None


def nomad_token():
    if os.environ.get("NOMAD_TOKEN"):
        return os.environ["NOMAD_TOKEN"]
    env = Path(os.environ.get("E2B_INFRA_ENV", "/opt/e2b-infra/.env"))
    if env.is_file():
        for line in env.read_text(errors="replace").splitlines():
            if line.startswith("NOMAD_ACL_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    token = Path("/data/nomad/acl.token")
    return token.read_text().strip() if token.is_file() else ""


def load_site():
    """读 criu-rollback/deploy/ops/site.env（两套脚本共用的现场参数），不覆盖已导出的变量。"""
    site = DEPLOY / "ops" / "site.env"
    if site.is_file():
        for line in site.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    global envs
    import importlib
    envs = importlib.reload(envs)


def orchestrator():
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            exe = os.readlink(p / "exe")
        except OSError:
            continue
        if Path(exe.replace(" (deleted)", "")).name.startswith(("orchestrator", "template-manager")):
            return p.name, exe
    return None, None


def identity(stack):
    """这一段测的是什么：orchestrator、Firecracker、内核与 gsd、仓库提交。"""
    pid, exe = orchestrator()
    ident = {"time": datetime.now().isoformat(), "stack": stack, "host": os.uname().nodename,
             "host_kernel": os.uname().release, "repo_commit": sh("git -C %s rev-parse HEAD" % REPO),
             "repo_dirty": bool(sh("git -C %s status --porcelain --untracked-files=no" % REPO)),
             "orchestrator": {"pid": pid, "exe": exe,
                              "sha256": sha(exe) if exe and "(deleted)" not in exe else None},
             "firecracker": {str(p): sha(p) for p in sorted(Path("/fc-versions").glob("*/firecracker"))}}
    if stack == "criu":
        ident["criu_fc"] = {str(p): sha(p) for p in sorted(paths.host_fc().rglob("*")) if p.is_file()}
    return ident


def resources():
    pid, _ = orchestrator()
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = v.strip()
    res = {"time": datetime.now().isoformat(), "MemAvailable": mem.get("MemAvailable"),
           "netns": stack.e2b_netns(),
           "firecracker": len(stack.e2b_firecrackers()),
           "root_used_kB": sh("df -k --output=used / | tail -1")}
    if pid:
        res["orchestrator_fds"] = len(os.listdir("/proc/%s/fd" % pid))
    return res


def current_stack():
    _, exe = orchestrator()
    if not exe:
        return None
    return "criu" if stack.is_criu(exe) else "sandbox"


def ensure_stack(want, run_dir):
    """切到要测的栈。已经在目标栈上就不调用 950.sh（只装了沙箱级、没跑过 950.sh all 的机器也能测沙箱级）。"""
    if current_stack() == want:
        log("当前已是%s栈" % ("沙箱级" if want == "sandbox" else "进程级"))
        return
    target = want
    log("确保当前是%s栈（950.sh %s）" % ("沙箱级" if want == "sandbox" else "进程级", target))
    with (run_dir / ("switch-%s.log" % target)).open("a") as f:
        r = subprocess.run(["bash", str(STACK_SH), target], cwd=REPO, stdout=f, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise RuntimeError("切到 %s 失败，见 %s" % (target, run_dir / ("switch-%s.log" % target)))


def client_env(spec):
    """某个 conda 环境的解释器，加上从 e2b_env 读出的客户端凭据（同 sandbox_smoke.py）。"""
    command, env = envs.runtime(spec)
    code = ("import os,sys; sys.path.insert(0, %r); from e2b_env import load_e2b_env; "
            "os.environ.update(load_e2b_env()); print('\\n'.join('%%s=%%s' %% (k, os.environ[k]) "
            "for k in ('E2B_API_URL','E2B_DOMAIN','E2B_HTTP_SSL','E2B_ACCESS_TOKEN','E2B_API_KEY')))"
            % str(REPO / "criu-rollback" / "scripts"))
    creds = subprocess.run(command + ["-c", code], env=env, capture_output=True, text=True)
    if creds.returncode != 0:
        raise RuntimeError("取客户端凭据失败：" + creds.stderr.strip()[-300:])
    for line in creds.stdout.splitlines():
        k, v = line.split("=", 1)
        env[k] = v
    return command, env


def run_step(step, run_dir):
    out = run_dir / step["id"].replace("/", "-")
    logf = out.with_suffix(".log")
    if out.exists():  # 续跑时上一次的结果留着，换个名字
        out.rename(out.with_name(out.name + "-old-" + datetime.now().strftime("%H%M%S")))
        if logf.exists():
            logf.rename(logf.with_name(logf.stem + "-old-" + datetime.now().strftime("%H%M%S") + ".log"))
    if step["kind"] == "py":
        # 性能脚本：用所测栈的 SDK 环境（两套 SDK 不能混用）
        command, env = client_env(envs.SANDBOX_ENV if step["stack"] == "sandbox" else envs.CRIU_ENV)
        out.mkdir(parents=True, exist_ok=True)
        cmd = command + [str(REPO / step["args"][0])] + step["args"][1:] + ["--out", str(out / "result.json")]
    elif step["kind"] == "run":
        # 沙箱级：用沙箱级 conda 环境的解释器；凭据由 e2b_env 读出后放进子进程环境
        command, env = client_env(envs.SANDBOX_ENV)
        env["NOMAD_TOKEN"] = nomad_token()
        out.mkdir(parents=True, exist_ok=True)
        cmd = ["bash", str(RUN_SH)] + step["args"] + ["--python", command[0], "--env-file", "/dev/null",
                                                      "--results-dir", str(out)]
    else:
        env = dict(os.environ)
        cmd = ["bash", str(STACK_SH)] + step["args"] + ["--out", str(out)]
    log("运行 %s：%s" % (step["id"], " ".join(cmd)))
    with logf.open("w") as f:
        r = subprocess.run(cmd, cwd=REPO, env=env, stdout=f, stderr=subprocess.STDOUT)
    return r.returncode, out, logf


def outcome(step, rc, out):
    """判读：run.sh 的退出码 = FAIL 项数；950.sh 与性能脚本的退出码 0 = 通过。附上各自的汇总位置。"""
    if step["kind"] == "py":
        res = out / "result.json"
        if res.is_file():
            data = json.loads(res.read_text())
            return ("PASS" if rc == 0 else "FAIL"), "verdict %s%s" % (
                data.get("verdict"), ("：" + data["error"][:200]) if data.get("error") else "")
        return ("PASS" if rc == 0 else "FAIL"), "退出码 %d" % rc
    if step["kind"] == "run":
        summaries = sorted(out.glob("*/SUMMARY.md"))
        line = ""
        if summaries:
            for l in summaries[-1].read_text(errors="replace").splitlines():
                if l.startswith("| 结果 |"):
                    line = l.split("|")[2].strip().replace("*", "")
        return ("PASS" if rc == 0 else "FAIL"), line or "退出码 %d" % rc
    summary = out / "summary.json"
    if summary.is_file():
        data = json.loads(summary.read_text())
        return ("PASS" if rc == 0 else "FAIL"), "verdict %s%s" % (
            data.get("verdict"), ("：" + data["error"][:200]) if data.get("error") else "")
    return ("PASS" if rc == 0 else "FAIL"), "退出码 %d" % rc


def maybe_recycle(data, run_dir):
    """e2b netns 超过上限就清一次（每次重启 orchestrator 会留下约 300 个，extended 一轮能涨到六七千）。
    清理失败只记录，不影响测试步骤。"""
    n = stack.e2b_netns()
    if n <= NETNS_LIMIT:
        return
    logf = run_dir / ("recycle-netns-%s.log" % datetime.now().strftime("%H%M%S"))
    log("e2b netns %d 个，超过 %d，先清理（950.sh recycle-netns，日志 %s）" % (n, NETNS_LIMIT, logf.name))
    t0 = time.time()
    with logf.open("w") as f:
        r = subprocess.run(["bash", str(STACK_SH), "recycle-netns"], cwd=REPO, stdout=f, stderr=subprocess.STDOUT)
    rec = {"time": datetime.now().isoformat(), "before": n, "after": stack.e2b_netns(),
           "seconds": int(time.time() - t0), "ok": r.returncode == 0}
    data.setdefault("recycles", []).append(rec)
    log("清理%s：e2b netns %d → %d（%s）" % ("完成" if rec["ok"] else "失败（继续测试）", n, rec["after"], dur(rec["seconds"])))


def write_summary(data, run_dir):
    lines = ["# 950 测试 · %s 档" % data["level"], "",
             "| 项 | 值 |", "|---|---|",
             "| 开始 | %s |" % data["started"], "| 结束 | %s |" % data.get("finished", "未结束"),
             "| 栈 | %s |" % "、".join(data["stacks"]),
             "| 主机 | %s |" % os.uname().nodename, ""]
    lines += ["## 逐步", "", "| 步骤 | 结果 | 用时 | 摘要 |", "|---|---|---|---|"]
    for s in data["steps"]:
        lines.append("| %s | %s | %s | %s |" % (s["id"], s["status"],
                                                dur(s["seconds"]) if "seconds" in s else "-",
                                                s.get("detail", "")))
    if data.get("recycles"):
        lines += ["", "## 自动清理 netns", "", "| 时刻 | 清理前 | 清理后 | 用时 | 结果 |", "|---|---|---|---|---|"]
        for r in data["recycles"]:
            lines.append("| %s | %s | %s | %s | %s |" % (r["time"][11:19], r["before"], r["after"], dur(r["seconds"]),
                                                        "OK" if r["ok"] else "失败"))
    lines += ["", "## 身份", ""]
    for key, ident in data.get("identity", {}).items():
        orch = ident["orchestrator"]
        lines.append("- **%s**：orchestrator `%s` sha256 `%s`；宿主内核 `%s`；仓库 `%s`%s"
                     % (key, orch["exe"], orch["sha256"], ident["host_kernel"], ident["repo_commit"][:12],
                        "（有未提交改动）" if ident["repo_dirty"] else ""))
    if data.get("resources"):
        lines += ["", "## 宿主资源", "", "| 时刻 | MemAvailable | netns | firecracker | 根盘已用 kB | orchestrator fd |",
                  "|---|---|---|---|---|---|"]
        for when, r in data["resources"].items():
            lines.append("| %s | %s | %s | %s | %s | %s |" % (when, r["MemAvailable"], r["netns"], r["firecracker"],
                                                         r["root_used_kB"], r.get("orchestrator_fds", "-")))
    (run_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    (run_dir / "run.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))


def progress_file():
    return state_dir() / "progress.json"


def save(data):
    state_dir().mkdir(parents=True, exist_ok=True)
    progress_file().write_text(json.dumps(data, ensure_ascii=False, indent=1))
    write_summary(data, Path(data["run_dir"]))


def execute(args):
    load_site()
    stacks = list(STACKS) if args.stack == "both" else [args.stack]
    prev = json.loads(progress_file().read_text()) if progress_file().is_file() else None
    redo = prev and any(s["status"] != "PASS" for s in prev["steps"])
    if prev and redo and prev["level"] == args.level and prev["stacks"] == stacks and not args.new:
        # 上一轮没跑完，或跑完了但有没通过的步骤：只重跑没通过的，已 PASS 的保留
        data = prev
        data.pop("finished", None)
        log("续跑上一轮：%s（重跑 %d 个未通过的步骤）"
            % (data["run_dir"], sum(1 for s in prev["steps"] if s["status"] != "PASS")))
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = RESULTS / ("%s-%s" % (stamp, args.level))
        run_dir.mkdir(parents=True)
        data = {"level": args.level, "stacks": stacks, "hours": args.hours, "run_dir": str(run_dir),
                "started": datetime.now().isoformat(), "pid": os.getpid(),
                "steps": plan(args.level, stacks, args.hours), "identity": {}, "resources": {}}
    data["pid"] = os.getpid()
    run_dir = Path(data["run_dir"])
    data["resources"].setdefault("开始", resources())
    save(data)
    total, switches = estimate(data["steps"])
    log("共 %d 步，预计约 %d 分钟（含 %d 次切栈；中途清理 netns 每次另加约 2 分钟）。进度：bash test950.sh status"
        % (len(data["steps"]), total, switches))
    current = None
    for step in data["steps"]:
        if step["status"] == "PASS":
            continue
        maybe_recycle(data, run_dir)
        if step["stack"] != current:
            ensure_stack(step["stack"], run_dir)
            current = step["stack"]
            key = "%s@%s" % (current, datetime.now().strftime("%H:%M"))
            data["identity"][key] = identity(current)
            (run_dir / ("identity-%s.json" % key.replace(":", ""))).write_text(
                json.dumps(data["identity"][key], ensure_ascii=False, indent=1))
        step["status"] = "运行中"
        step["started"] = datetime.now().isoformat()
        save(data)
        t0 = time.time()
        rc, out, logf = run_step(step, run_dir)
        step["seconds"] = int(time.time() - t0)
        step["status"], step["detail"] = outcome(step, rc, out)
        log("%s %s（%s）%s" % (step["id"], step["status"], dur(step["seconds"]), step["detail"]))
        save(data)
    maybe_recycle(data, run_dir)
    data["resources"]["结束"] = resources()
    data["finished"] = datetime.now().isoformat()
    save(data)
    n_fail = sum(1 for s in data["steps"] if s["status"] != "PASS")
    log("全部结束：%d 步，FAIL %d。把结果打印到终端：bash test950.sh report" % (len(data["steps"]), n_fail))
    return 0 if n_fail == 0 else 1


def status():
    if not progress_file().is_file():
        print("还没有跑过。")
        return 0
    data = json.loads(progress_file().read_text())
    alive = False
    try:
        os.kill(data.get("pid", 0), 0)
        alive = not data.get("finished")
    except OSError:
        pass
    print("档 %s · 栈 %s · 开始 %s · %s" % (data["level"], "、".join(data["stacks"]), data["started"][:16],
                                       "已结束" if data.get("finished") else ("运行中" if alive else "已停止（再执行同一条命令续跑）")))
    for s in data["steps"]:
        extra = ""
        if s["status"] == "运行中" and "started" in s:
            extra = "已跑 %d 分钟，预计 %d 分钟" % ((datetime.now() - datetime.fromisoformat(s["started"])).seconds // 60, s["est_min"])
        elif "seconds" in s:
            extra = "%s  %s" % (dur(s["seconds"]), s.get("detail", ""))
        print("  %-6s %-26s %s" % (s["status"], s["id"], extra))
    print("结果目录：%s" % data["run_dir"])
    print("打印结果：bash test950.sh report")
    log_file = state_dir() / "test950-bg.log"
    if log_file.is_file():
        print("日志：tail -f %s" % log_file)
    return 0


def pick_run(name=None):
    if name:
        run = RESULTS / name
        if not run.is_dir():
            sys.exit("没有结果目录 %s" % run)
        return run
    if progress_file().is_file():
        run = Path(json.loads(progress_file().read_text())["run_dir"])
        if run.is_dir():
            return run
    runs = sorted(p for p in RESULTS.glob("*-*") if p.is_dir())
    if not runs:
        sys.exit("还没有结果（%s 下是空的）" % RESULTS)
    return runs[-1]


def print_overview(run):
    data = json.loads((run / "run.json").read_text()) if (run / "run.json").is_file() else None
    if data is None:
        report.show_file(run / "SUMMARY.md", 0, root=run)
        return
    print("==== test950 %s 档：%s（从这一行复制到“结束”那一行）====" % (data["level"], run.name))
    print("主机 %s · 栈 %s · 开始 %s · %s" % (os.uname().nodename, "、".join(data["stacks"]), data["started"][:19],
                                         ("结束 " + data["finished"][:19]) if data.get("finished") else "未结束"))
    for s in data["steps"]:
        print(report.clip("  %-6s %-22s %-9s %s" % (s["status"], s["id"], dur(s["seconds"]) if "seconds" in s else "-",
                                                   s.get("detail", ""))))
    print("-- 身份（每段开头记录，相同的只列一次）")
    seen = set()
    for key, ident in data.get("identity", {}).items():
        orch = ident["orchestrator"]
        sig = (ident["stack"], orch["exe"], orch["sha256"], ident["repo_commit"])
        if sig in seen:
            continue
        seen.add(sig)
        print("  %s：orchestrator %s sha256 %s；宿主内核 %s；仓库 %s%s" % (
            key, orch["exe"], orch["sha256"], ident["host_kernel"], ident["repo_commit"][:12],
            "（有未提交改动）" if ident["repo_dirty"] else ""))
        for path, digest in sorted((ident.get("firecracker") or {}).items()):
            print("    firecracker %s %s" % (path, digest))
        for path, digest in sorted((ident.get("criu_fc") or {}).items()):
            print("    %s %s" % (path, digest))
    if data.get("resources"):
        print("-- 宿主资源（e2b 的 netns / firecracker）")
        for when, r in data["resources"].items():
            print("  %s：MemAvailable %s，netns %s，firecracker %s，根盘已用 %s kB，orchestrator fd %s" % (
                when, r["MemAvailable"], r["netns"], r["firecracker"], r["root_used_kB"], r.get("orchestrator_fds", "-")))
    for r in data.get("recycles", []):
        print("  自动清理 netns %s：%s → %s（%s）%s" % (r["time"][11:19], r["before"], r["after"], dur(r["seconds"]),
                                                  "" if r["ok"] else " 失败"))
    for s in data["steps"]:
        out = run / s["id"].replace("/", "-")
        if not out.is_dir():
            continue
        print("-- %s" % s["id"])
        if s["kind"] == "run":
            report.print_run_sh(out)
        elif s["kind"] == "py":
            print_perf(run, s)
        else:
            report.print_criu(out)
    print("==== 结束 ====")
    print("某一步的细节（各项日志末尾、性能表）：bash test950.sh report 步骤名，例如")
    for s in data["steps"][:3]:
        print("  bash test950.sh report %s" % s["id"])


def print_perf(run, step, indent=""):
    """性能脚本：打印日志里“---- 汇总 ----”之后的表格（脚本自己打印的汇总）。"""
    logf = run / (step["id"].replace("/", "-") + ".log")
    if not logf.is_file():
        print(indent + "（没有日志）")
        return
    text = logf.read_text(errors="replace").splitlines()
    start = next((i for i, l in enumerate(text) if l.startswith("---- 汇总 ----")), None)
    if start is None:
        print(indent + "（没有汇总，日志末尾如下）")
        for line in text[-15:]:
            print(indent + report.clip(line))
        return
    for line in text[start + 1:]:
        if line.startswith("结果文件"):
            continue
        print(indent + line)


def print_report(args):
    run = pick_run(args.run)
    if args.file:
        report.show_file(run / args.file, args.lines, root=run)
        return 0
    if not args.target:
        print_overview(run)
        return 0
    if args.target == "files":
        report.list_files(run)
        return 0
    out = run / args.target.replace("/", "-")
    if not out.is_dir():
        sys.exit("没有步骤 %s 的结果目录（用 bash test950.sh report files 看有哪些）" % args.target)
    print("==== test950 · %s · %s ====" % (run.name, args.target))
    step = next((s for s in json.loads((run / "run.json").read_text())["steps"] if s["id"] == args.target), None) \
        if (run / "run.json").is_file() else None
    if step and step["kind"] == "py":
        print_perf(run, step)
        report.show_file(run / (step["id"].replace("/", "-") + ".log"), args.lines, root=run)
    elif args.target.startswith("sandbox/"):
        report.print_run_sh_detail(out, args.lines)
    else:
        report.print_criu_detail(out, args.lines)
    print("==== 结束 ====")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("level", choices=LEVELS + EXTRA + ("status", "report"))
    ap.add_argument("target", nargs="?", help="report：步骤名（如 criu/stress-ext），或 files 列出全部文件")
    ap.add_argument("--run", help="report：结果目录名，默认最近一轮")
    ap.add_argument("--file", help="report：打印结果目录里的这个文件（相对路径）")
    ap.add_argument("--lines", type=int, default=40, help="report：每个文件打印的行数，0 为全部")
    ap.add_argument("--stack", choices=("both",) + STACKS, default="both", help="测哪套，默认两套都测")
    ap.add_argument("--hours", type=float, default=4.0, help="long 档每套的长跑时长（小时），默认 4；两套都测约 9 小时")
    ap.add_argument("--new", action="store_true", help="不续跑上一轮，重新开始")
    ap.add_argument("--fg", action="store_true", help="在前台跑（默认转到后台，断开 ssh 也不停）")
    args = ap.parse_args()
    if args.level == "status":
        return status()
    if args.level == "report":
        return print_report(args)
    if os.geteuid() != 0:
        sys.exit("请用 root 执行")
    if progress_file().is_file():
        prev = json.loads(progress_file().read_text())
        try:
            if not prev.get("finished") and prev.get("pid") and prev["pid"] != os.getpid():
                os.kill(prev["pid"], 0)
                sys.exit("上一轮还在跑（pid %d），先 bash test950.sh status 看进度" % prev["pid"])
        except OSError:
            pass
    if not args.fg:
        RESULTS.mkdir(parents=True, exist_ok=True)
        bg_log = state_dir() / "test950-bg.log"
        state_dir().mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:] + ["--fg"]
        with bg_log.open("a") as f:
            subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=REPO)
        time.sleep(3)
        print("已在后台开始（断开 ssh 不影响）。看进度：")
        print("  bash test950.sh status")
        print("看日志：")
        print("  tail -f %s" % bg_log)
        return 0
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        return execute(args)
    except KeyboardInterrupt:
        log("被中断。再执行同一条命令从中断的这一步继续")
        return 130
    except Exception as exc:
        log("停止：%s。处理后再执行同一条命令会从这一步继续" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
