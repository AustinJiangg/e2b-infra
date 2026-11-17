#!/usr/bin/env python3
"""一条命令的操作入口，由 deploy/950.sh 调用。只用标准库，SDK 相关的步骤用对应 conda 环境的解释器运行。

    all           首次全流程：校验、环境与 SDK、凭据、镜像、基线、落位、切到进程级、建模板、
                  验收、压测、切回沙箱级、冒烟，最后把结果打印到终端。中途停下后再跑同一条命令，
                  从停下的地方继续；上一轮已全部完成时开始新一轮。加 --new 强制开始新一轮
    criu          切到进程级，等到能建沙箱；base-criu 不在或不可用时自动重建
    sandbox       切回沙箱级，等到原模板能建沙箱
    recycle-netns 清理重启 orchestrator 留下的 e2b 网络命名空间，按当前栈重新拉起
    report        把 all 的结果打印到终端（可加 acceptance / stress / smoke / netcheck / log，或 --file）
    install-sdk   没有进程级环境时先克隆，再离线安装 SDK
    quick / acceptance / stress / stress-ext / soak   进程级测试（自动使用进程级环境）
    smoke         只跑沙箱级冒烟（自动使用沙箱级环境）

950 上的 e2b 只有我们在用：切栈、清 netns 之前，先删掉 API 上列出的全部 e2b 沙箱，
再把仍在运行的 e2b Firecracker（/fc-versions/ 下的）结束掉。同机 AgentENV 的沙箱不碰。
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import envs
import paths
import report
import stack
import unpack

ROOT = stack.ROOT
OPS = ROOT / "ops"
RESULTS = paths.RESULTS
CRIU_TEMPLATE = os.environ.get("CRIU_TEMPLATE", "base-criu")
SANDBOX_TEMPLATE = os.environ.get("SANDBOX_TEMPLATE", "base")
IMAGE = os.environ.get("CRIU_TEMPLATE_IMAGE", "harbor:443/e2b-orchestration/ubuntu:22.04-custom-criu421")
READY_TIMEOUT = int(os.environ.get("CRIU_READY_TIMEOUT", "300"))
# 切栈后 API 还没把节点标成可用时的报错，属于等一等就会好的情况。
TRANSIENT = re.compile(r"Failed to place sandbox|no node available|available build client|\b50[234]\b", re.I)
# 宿主网络比对时排除 e2b 自己的网卡、netns、地址池。
E2B_NET = re.compile(r"e2bv-|\bns-\d+|10\.1[34]\.")

LOG = None


def progress_file():
    return stack.state() / "all-progress.json"


def log(*parts):
    line = datetime.now().strftime("%H:%M:%S ") + " ".join(str(p) for p in parts)
    print(line, flush=True)
    if LOG:
        with LOG.open("a") as f:
            f.write(line + "\n")


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


# ---------- 客户端环境 ----------

def check_criu_python():
    return envs.run(envs.CRIU_ENV, [OPS / "check_python.py"], capture_output=True, text=True).returncode == 0


def ensure_python():
    require(envs.prefix(envs.SANDBOX_ENV) is not None, "找不到原沙箱级 conda 环境 %s；在 ops/site.env 写 SANDBOX_CONDA_ENV" % envs.SANDBOX_ENV)
    if envs.prefix(envs.CRIU_ENV) is None:
        log("克隆 conda 环境", envs.SANDBOX_ENV, "->", envs.CRIU_ENV, "（离线，约 1 分钟）")
        envs.clone(envs.CRIU_ENV, envs.SANDBOX_ENV)
    if check_criu_python():
        log("进程级 SDK 已安装:", envs.prefix(envs.CRIU_ENV))
        return
    log("离线安装进程级 SDK 到", envs.prefix(envs.CRIU_ENV))
    require(envs.run(envs.CRIU_ENV, [OPS / "install_sdk.py"]).returncode == 0, "SDK 安装失败，见上面的输出")


def probe(spec, *args):
    r = envs.run(spec, [OPS / "probe.py", *args], capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if not lines:
        return {"ok": False, "error": (r.stdout + r.stderr).strip()[-500:]}
    return json.loads(lines[-1])


def check_creds():
    data = probe(envs.CRIU_ENV, "templates")
    require(data.get("ok"), "客户端凭据或 API 不可用（README 第 5 节）：" + data.get("error", ""))
    log("客户端凭据可用，API 上现有模板:", ", ".join(",".join(t["aliases"]) or t["templateID"] for t in data["templates"]) or "无")


# ---------- e2b 沙箱 ----------

def clear_e2b_sandboxes():
    """删掉全部 e2b 沙箱：先经 API 删，60 秒后仍在的 e2b Firecracker 直接结束。"""
    if not stack.e2b_firecrackers():
        return
    cur = stack.current()
    spec = envs.CRIU_ENV if cur == "criu" else envs.SANDBOX_ENV
    if cur and envs.prefix(spec) is not None:
        data = probe(spec, "cleanup-all")
        if data.get("ok"):
            log("经 API 删除 e2b 沙箱 %d 个%s" % (len(data["killed"]),
                ("，失败 %d 个" % len(data["failed"])) if data.get("failed") else ""))
        else:
            log("经 API 删除沙箱失败：" + data.get("error", "")[:200])
    for _ in range(30):
        if not stack.e2b_firecrackers():
            return
        time.sleep(2)
    left = stack.e2b_firecrackers()
    log("仍有 %d 个 e2b Firecracker 在运行，直接结束（pid %s）" % (len(left), " ".join(map(str, left))))
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for _ in range(10):
        if not stack.e2b_firecrackers():
            return
        time.sleep(1)


# ---------- 模板与就绪 ----------

def template_build_missing(alias):
    """模板不在 API 里，或用本地存储时 build 目录不在，返回原因；否则返回 None。"""
    data = probe(envs.CRIU_ENV, "templates")
    require(data.get("ok"), "查询模板失败：" + data.get("error", ""))
    hits = [t for t in data["templates"] if alias in t["aliases"]]
    if not hits:
        return "API 里没有模板 " + alias
    doc, _ = stack.baseline()
    env = stack.select_task(doc)[1].get("Env", {})
    if env.get("STORAGE_PROVIDER") == "Local":
        base = Path(env.get("LOCAL_TEMPLATE_STORAGE_BASE_PATH") or "/tmp/templates")
        if not (base / hits[0]["buildID"]).is_dir():
            return "模板文件 %s 已不在（可能被系统清理）" % (base / hits[0]["buildID"])
    return None


def wait_sandbox(spec, template, timeout=READY_TIMEOUT):
    deadline = time.time() + timeout
    while True:
        data = probe(spec, "create", template)
        if data.get("ok"):
            log("模板 %s 建沙箱成功（%s ms），服务已就绪" % (template, data["ms"]))
            return True
        error = data.get("error", "")
        if data.get("not_found") or time.time() > deadline:
            log("模板 %s 建不出沙箱：%s" % (template, error[:300]))
            return False
        log("服务还没就绪，10 秒后重试：" + error[:160])
        time.sleep(10)


def build_template(logdir):
    deadline = time.time() + READY_TIMEOUT
    for attempt in range(1, 100):
        out = logdir / ("build-%s-%d.log" % (CRIU_TEMPLATE, attempt))
        log("建模板 %s（镜像 %s，不用缓存），日志 %s" % (CRIU_TEMPLATE, IMAGE, out))
        with out.open("w") as f:
            r = envs.run(envs.CRIU_ENV, ["build_template.py", "--alias", CRIU_TEMPLATE, "--cpu", "2", "--memory", "2048",
                                         "--image", IMAGE], cwd=paths.SCRIPTS, stdout=f, stderr=subprocess.STDOUT)
        text = out.read_text(errors="replace")
        if r.returncode == 0:
            log(text.strip().splitlines()[-1])
            return
        if TRANSIENT.search(text) and time.time() < deadline:
            log("服务还没就绪，15 秒后重试建模板")
            time.sleep(15)
            continue
        raise RuntimeError("建模板失败，见 %s；先查镜像引用、Harbor 认证、gsd health（README 第 11 节）" % out)


def ensure_criu_template(logdir, force=False):
    reason = "要求新建" if force else template_build_missing(CRIU_TEMPLATE)
    if reason:
        log(reason + "，建模板")
        build_template(logdir)
        require(wait_sandbox(envs.CRIU_ENV, CRIU_TEMPLATE), "新建的模板建不出沙箱，查 orchestrator 日志")
        return
    if wait_sandbox(envs.CRIU_ENV, CRIU_TEMPLATE):
        return
    log("现有模板不可用，重建一次")
    build_template(logdir)
    require(wait_sandbox(envs.CRIU_ENV, CRIU_TEMPLATE), "重建后的模板仍建不出沙箱，查 orchestrator 日志")


def ensure_sandbox_ready():
    require(wait_sandbox(envs.SANDBOX_ENV, SANDBOX_TEMPLATE),
            "已切回沙箱级，但原模板 %s 建不出沙箱；模板文件可能已被系统清理，需要按原部署方式重建" % SANDBOX_TEMPLATE)


# ---------- 宿主网络比对（AgentENV 同机时用） ----------

def lines(*command):
    # 网卡正在增删时 ip 的 dump 会中途被打断、列表不完整，重试几次。
    for _ in range(5):
        try:
            r = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if "Dump was interrupted" not in r.stderr:
            return r.stdout.splitlines()
        time.sleep(1)
    return r.stdout.splitlines()


def netsnap():
    links = sorted(l.split(":")[1].strip().split("@")[0] for l in lines("ip", "-o", "link", "show") if l.count(":") >= 2)
    return {
        "links": [l for l in links if not E2B_NET.search(l)],
        "netns": sorted(l.split()[0] for l in lines("ip", "netns", "list") if l and not E2B_NET.search(l.split()[0])),
        "routes": sorted(l for l in lines("ip", "route", "show", "table", "all") if not E2B_NET.search(l)),
        "rules": sorted(lines("ip", "rule", "show")),
        "iptables": sorted(re.sub(r"\[\d+:\d+\]", "", l) for l in lines("iptables-save")
                           if l and not l.startswith("#") and not E2B_NET.search(l)),
    }


def netdiff(before, after):
    out = []
    for key in before:
        old, new = set(before[key]), set(after.get(key, []))
        out += ["%s - %s" % (key, x) for x in sorted(old - new)] + ["%s + %s" % (key, x) for x in sorted(new - old)]
    return out


def compare_net(before_file, after_file, out):
    diff = netdiff(json.loads(before_file.read_text()), json.loads(after_file.read_text()))
    out.write_text("\n".join(diff) + "\n" if diff else "无变化\n")
    if diff:
        log("警告：切换前后宿主上非 e2b 的网络配置有 %d 处变化（AgentENV 的网卡和规则也在其中，逐条核对）：" % len(diff))
        for line in diff[:30]:
            log("   ", line)
        if len(diff) > 30:
            log("    …其余 %d 处见 %s" % (len(diff) - 30, out))
    else:
        log("切换前后宿主上非 e2b 的网卡、路由、策略路由、iptables 规则无变化")
    return len(diff)


# ---------- 日常切换与清理 ----------

def switch_with_check(target, logdir):
    logdir.mkdir(parents=True, exist_ok=True)
    before = logdir / "net-before.json"
    before.write_text(json.dumps(netsnap(), indent=1))
    clear_e2b_sandboxes()
    stack.switch(target)
    after = logdir / "net-after.json"
    after.write_text(json.dumps(netsnap(), indent=1))
    compare_net(before, after, logdir / "netcheck.txt")


def ensure_ready(target, logdir):
    if target == "criu":
        ensure_criu_template(logdir)
    else:
        ensure_sandbox_ready()


def daily(target):
    logdir = RESULTS / "switch" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + target)
    global LOG
    logdir.mkdir(parents=True, exist_ok=True)
    LOG = logdir / "switch.log"
    if target == "criu":
        require(check_criu_python(), "进程级环境或 SDK 不对，先执行 bash criu-rollback/deploy/950.sh install-sdk")
    switch_with_check(target, logdir)
    ensure_ready(target, logdir)
    log("完成：当前是%s栈，可以使用。客户端用 conda 环境 %s" % (
        "进程级" if target == "criu" else "沙箱级", envs.CRIU_ENV if target == "criu" else envs.SANDBOX_ENV))


def recycle():
    logdir = RESULTS / "switch" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-recycle-netns")
    global LOG
    logdir.mkdir(parents=True, exist_ok=True)
    LOG = logdir / "recycle.log"
    target = stack.current()
    before = logdir / "net-before.json"
    before.write_text(json.dumps(netsnap(), indent=1))
    clear_e2b_sandboxes()
    stack.recycle_netns()
    after = logdir / "net-after.json"
    after.write_text(json.dumps(netsnap(), indent=1))
    compare_net(before, after, logdir / "netcheck.txt")
    ensure_ready(target, logdir)
    log("完成：已清理并恢复%s栈" % ("进程级" if target == "criu" else "沙箱级"))


# ---------- 测试 ----------

def fresh(path):
    if path.exists():
        path.rename(path.with_name(path.name + "-old-" + datetime.now().strftime("%H%M%S")))
    return path


def run_tests(mode, out, extra=()):
    r = envs.run(envs.CRIU_ENV, [OPS / "run_tests.py", mode, "--out", fresh(out)] + list(extra))
    require(r.returncode == 0, "%s 未通过，见 %s/summary.json" % (mode, out))


def smoke(out):
    r = envs.run(envs.SANDBOX_ENV, [OPS / "sandbox_smoke.py", "--template", SANDBOX_TEMPLATE, "--results-dir", fresh(out)])
    require(r.returncode == 0, "沙箱级冒烟未通过，见 " + str(out))


# ---------- 全流程 ----------

STEPS = [
    ("verify", "校验部署目录"),
    ("unpack", "展开运行文件与镜像"),
    ("python", "conda 环境与进程级 SDK"),
    ("creds", "客户端凭据"),
    ("image", "导入镜像到 Harbor"),
    ("baseline", "预检并保存沙箱级基线"),
    ("install", "落位三个运行文件"),
    ("switch-criu", "切到进程级"),
    ("template", "建模板 " + CRIU_TEMPLATE),
    ("acceptance", "必跑验收"),
    ("stress", "探针与四组压测"),
    ("switch-sandbox", "切回沙箱级"),
    ("smoke", "沙箱级冒烟"),
]
# 这些步骤做成一次就记下，续跑时跳过；其余步骤每次都执行（本身可重复，已经是目标状态时直接通过）。
ONCE = {"image", "acceptance", "stress", "smoke"}


def load_progress(new):
    if progress_file().exists() and not new:
        data = json.loads(progress_file().read_text())
        if not data.get("finished") and Path(data["run"]).is_dir():
            return data, True
    run = RESULTS / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-all")
    run.mkdir(parents=True)
    return {"run": str(run), "started": datetime.now().isoformat(), "steps": {}, "finished": False}, False


def save_progress(data):
    stack.write_json(progress_file(), data)
    stack.write_json(Path(data["run"]) / "summary.json", data)


def do_step(key, run, again=False):
    if key == "verify":
        stack.verify()
        log("部署目录 SHA256 全部通过")
    elif key == "unpack":
        unpack.unpack()
    elif key == "python":
        ensure_python()
    elif key == "creds":
        check_creds()
    elif key == "image":
        if os.environ.get("CRIU_SKIP_IMAGE_IMPORT") == "1":
            log("CRIU_SKIP_IMAGE_IMPORT=1：镜像已在别的机器导入，跳过")
            return
        r = subprocess.run(["bash", str(OPS / "import-image.sh")], env=dict(os.environ, CRIU_TEMPLATE_IMAGE=IMAGE))
        require(r.returncode == 0, "镜像导入失败，原因见上面的输出")
    elif key == "baseline":
        cur = stack.current()
        if not (stack.state() / "baseline.json").exists():
            require(cur == "sandbox", "当前不是原沙箱级服务，不能保存基线")
            clear_e2b_sandboxes()
            stack.preflight()
            stack.save_baseline()
        elif cur == "sandbox":
            clear_e2b_sandboxes()
            stack.preflight()
        else:
            log("基线已存在，当前是进程级栈，跳过预检")
    elif key == "install":
        stack.place_files()
    elif key == "switch-criu":
        cur = stack.current()
        before = run / "net-before.json"
        if cur == "sandbox":
            before.write_text(json.dumps(netsnap(), indent=1))
        clear_e2b_sandboxes()  # 上一轮中断时可能留下测试沙箱，会挡住切换
        stack.switch("criu")
        if before.exists():
            after = run / "net-after.json"
            after.write_text(json.dumps(netsnap(), indent=1))
            compare_net(before, after, run / "netcheck.txt")
    elif key == "template":
        # 本轮第一次新建；续跑时只确认模板可用（服务可能刚重启，顺带等就绪）。
        ensure_criu_template(run, force=not again)
    elif key == "acceptance":
        run_tests("acceptance", run / "acceptance")
    elif key == "stress":
        run_tests("stress", run / "stress")
    elif key == "switch-sandbox":
        clear_e2b_sandboxes()
        stack.switch("sandbox")
        ensure_sandbox_ready()
    elif key == "smoke":
        smoke(run / "sandbox-smoke")


def roll_back():
    """测试失败后回到沙箱级：先删 e2b 沙箱，再切回。

    只要 5008 上不是正好一个沙箱级进程就回切。切换中途失败时 5008 上可能一个进程都没有
    （current() 为 None），这时同样要回切，否则机器会停在没有服务的状态。"""
    if stack.current() == "sandbox":
        return
    log("测试失败，回切沙箱级，保持原服务可用")
    clear_e2b_sandboxes()
    try:
        stack.switch("sandbox")
        log("已回切沙箱级")
    except Exception as exc:
        log("自动回切失败：%s；处理后执行 bash criu-rollback/deploy/950.sh sandbox" % exc)


# ---------- 打印结果 ----------

def latest_run():
    runs = sorted(p for p in RESULTS.glob("*-all") if p.is_dir())
    require(runs, "还没有 all 的结果（%s 下没有 *-all 目录）" % RESULTS)
    return runs[-1]


def print_overview(run):
    data = json.loads((run / "summary.json").read_text()) if (run / "summary.json").is_file() else {"steps": {}}
    print("==== 950.sh all 结果：%s（从这一行复制到“结束”那一行）====" % run.name)
    print("主机 %s · 开始 %s · %s" % (os.uname().nodename, data.get("started", "-")[:19],
                                 "已完成" if data.get("finished") else "未完成"))
    for key, title in STEPS:
        info = data["steps"].get(key, {})
        print("  %-6s %-15s %s%s%s" % (info.get("status", "未执行"), key, title,
                                      "（%s 秒）" % info["seconds"] if "seconds" in info else "",
                                      ("：" + report.clip(info["error"], 200)) if info.get("error") else ""))
    print("-- 身份")
    for d in ("acceptance", "stress"):
        sums = run / d / "deliverables.sha256"
        if sums.is_file():
            for line in sums.read_text().splitlines():
                print("  " + line)
            break
    ps = stack.processes()
    print("  当前 5008：%s" % (", ".join("%s sha256 %s" % (p["exe"], p["sha256"][:16]) for p in ps) or "没有进程"))
    print("-- 宿主网络比对（切到进程级前后）")
    net = run / "netcheck.txt"
    if net.is_file():
        diff = net.read_text().splitlines()
        for line in diff[:40]:
            print("  " + line)
        if len(diff) > 40:
            print("  …共 %d 处，全部见 bash criu-rollback/deploy/950.sh report netcheck" % len(diff))
    else:
        print("  （没有比对结果）")
    for d, title in (("acceptance", "验收"), ("stress", "压测")):
        if (run / d).is_dir():
            print("-- " + title)
            report.print_criu(run / d)
    if (run / "sandbox-smoke").is_dir():
        print("-- 沙箱级冒烟")
        report.print_run_sh(run / "sandbox-smoke")
    print("==== 结束 ====")
    print("细节（各自打印到终端）：")
    for part, what in (("acceptance", "性能三轮分档表与各项日志末尾"), ("stress", "四组压测摘要，失败组的错误样本"),
                       ("smoke", "冒烟各项日志末尾"), ("netcheck", "网络比对全文"), ("log", "全流程日志末尾")):
        print("  bash criu-rollback/deploy/950.sh report %-10s # %s" % (part, what))


def print_report(part, file, lines):
    run = latest_run()
    if file:
        report.show_file(run / file, lines, root=run)
        return
    if not part:
        print_overview(run)
        return
    print("==== 950.sh all · %s · %s ====" % (run.name, part))
    if part in ("acceptance", "stress"):
        report.print_criu_detail(run / part, lines)
    elif part == "smoke":
        report.print_run_sh_detail(run / "sandbox-smoke", lines)
    elif part == "netcheck":
        report.show_file(run / "netcheck.txt", 0, root=run)
    elif part == "log":
        report.show_file(run / "all.log", lines or 0, root=run)
    elif part == "files":
        report.list_files(run)
    print("==== 结束 ====")


def all_flow(new):
    data, resumed = load_progress(new)
    run = Path(data["run"])
    global LOG
    LOG = run / "all.log"
    log("续跑上一轮" if resumed else "开始新一轮全流程", run)
    failed = None
    key = STEPS[0][0]
    try:
        for key, title in STEPS:
            if key in ONCE and data["steps"].get(key, {}).get("status") == "PASS":
                log("跳过（已完成）:", title)
                continue
            log("== %s" % title)
            again = data["steps"].get(key, {}).get("status") == "PASS"
            data["steps"][key] = {"status": "RUNNING"}
            save_progress(data)
            started = time.time()
            do_step(key, run, again)
            data["steps"][key] = {"status": "PASS", "seconds": round(time.time() - started)}
            save_progress(data)
        data["finished"] = True
        data["verdict"] = "PASS"
    except KeyboardInterrupt:
        data["steps"][key]["status"] = "中断"
        log("已中断。再执行 bash criu-rollback/deploy/950.sh all 从这一步继续；要先恢复沙箱级执行 bash criu-rollback/deploy/950.sh sandbox")
        failed = "中断"
    except Exception as exc:
        data["steps"][key]["status"] = "FAIL"
        data["steps"][key]["error"] = str(exc)
        log("停止:", exc)
        failed = str(exc)
        if key in ("template", "acceptance", "stress", "switch-criu"):
            roll_back()
        log("处理后再执行 bash criu-rollback/deploy/950.sh all，会从失败的这一步继续")
    finally:
        save_progress(data)
        print()
        print_overview(run)
    return 0 if failed is None else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("all", "criu", "sandbox", "recycle-netns", "report", "install-sdk", "quick",
                                           "acceptance", "stress", "stress-ext", "soak", "smoke"))
    parser.add_argument("part", nargs="?", choices=("acceptance", "stress", "smoke", "netcheck", "log", "files"),
                        help="report：只打印这一部分")
    parser.add_argument("--file", help="report：打印结果目录里的这个文件（相对路径，用 report files 列出）")
    parser.add_argument("--lines", type=int, default=40, help="report：每个文件打印的行数，0 为全部")
    parser.add_argument("--hours", type=float, default=4.0, help="soak：时长（小时）")
    parser.add_argument("--new", action="store_true", help="all：不续跑，开始新一轮")
    parser.add_argument("--out", type=Path, help="acceptance / stress / smoke 的结果目录（必须尚不存在）")
    args = parser.parse_args()
    if args.action == "report":
        print_report(args.part, args.file, args.lines)
        return 0
    require(os.geteuid() == 0, "请用 root 执行")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.action == "all":
        return all_flow(args.new)
    if args.action in ("criu", "sandbox"):
        daily(args.action)
    elif args.action == "recycle-netns":
        recycle()
    elif args.action == "install-sdk":
        ensure_python()
    elif args.action in ("quick", "acceptance", "stress", "stress-ext", "soak"):
        out = args.out or RESULTS / (stamp + "-" + args.action)
        require(not out.exists(), "结果目录已存在: %s" % out)
        run_tests(args.action, out, ["--hours", str(args.hours)] if args.action == "soak" else [])
    elif args.action == "smoke":
        out = args.out or RESULTS / (stamp + "-sandbox-smoke")
        require(not out.exists(), "结果目录已存在: %s" % out)
        smoke(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
        print("停止:", exc, file=sys.stderr)
        sys.exit(1)
