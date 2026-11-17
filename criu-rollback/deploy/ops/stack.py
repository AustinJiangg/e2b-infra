#!/usr/bin/env python3
"""950 上保存本机基线、检查、落位、切换及恢复，以及清理泄漏的网络命名空间。只使用 Python 标准库。

栈的名字：sandbox（沙箱级，原 job）、criu（进程级，由基线生成的 job）。
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import envs
import unpack

import paths
ROOT = paths.ROOT
FC = paths.FC
JOB = os.environ.get("CRIU_JOB", "template-manager-system")
PORT = int(os.environ.get("CRIU_PORT", "5008"))
FILES = ("gsd", "orchestrator", "kernels/vmlinux-6.1.158/vmlinux.bin")
BUILD_SH = os.environ.get("E2B_INFRA_BUILD", "/opt/e2b-infra/build.sh")
# e2b 的 Firecracker 都从 /fc-versions/ 启动；同机 AgentENV 的在 /var/lib/aenv/ 下，不算、不碰。
E2B_FC_PREFIX = "/fc-versions/"


def state():
    return paths.state_dir()


def hfc():
    return paths.host_fc()
META = ("Version", "SubmitTime", "CreateIndex", "ModifyIndex", "JobModifyIndex", "Status", "StatusDescription")
ENV_KEYS = ("HOST_GSD_PATH", "HOST_KERNELS_DIR", "CRIU_ORCH_SHA256")


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize(doc):
    job = copy.deepcopy(doc.get("Job", doc))
    require(isinstance(job, dict) and job.get("ID") == JOB, "job JSON 的 ID 与配置不符")
    for key in META:
        job.pop(key, None)
    require(not job.get("Stop", False), "基线 job 已停止，不能作为正常服务基线")
    return {"Job": job}


def select_task(doc):
    found = []
    for group in doc["Job"].get("TaskGroups", []):
        for task in group.get("Tasks", []):
            cfg = task.get("Config", {})
            args = cfg.get("args", [])
            if (task.get("Driver") == "raw_exec" and len(args) == 2 and args[0] == "-c"
                    and re.search(r"--port\s+" + str(PORT) + r"(?:\s|$)", args[1])
                    and re.search(r"/(?:orchestrator|template-manager)[\w.-]*", args[1])):
                found.append((group, task))
    require(len(found) == 1, "未唯一找到 raw_exec / shell -c / --port 对应的 orchestrator task；先核对现场 job")
    return found[0]


def binary(task):
    paths = set(re.findall(r"(/[^\s;&'\"]*/(?:orchestrator|template-manager)[\w.-]*)", task["Config"]["args"][1]))
    require(len(paths) == 1, "启动命令的二进制路径不唯一，不自动修改")
    return paths.pop()


def processes():
    result = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
            if not any(v == b"--port" and args[i + 1] == str(PORT).encode()
                       for i, v in enumerate(args[:-1])):
                continue
            exe = os.readlink(proc / "exe")
            if not re.search(r"(?:orchestrator|template-manager)", Path(exe).name):
                continue
            env = dict(item.split("=", 1) for item in
                       (proc / "environ").read_bytes().decode().split("\0") if "=" in item)
            result.append({"pid": int(proc.name), "exe": exe,
                           "sha256": sha(proc / "exe"),
                           "env": {key: env.get(key) for key in ENV_KEYS}})
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
            continue
    return result


def health():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:%s/health" % PORT, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def e2b_firecrackers():
    """e2b 自己的 Firecracker 进程（exe 在 /fc-versions/ 下）的 pid。按 exe 认，不用 pgrep -f。"""
    pids = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            exe = os.readlink(proc / "exe")
        except OSError:
            continue
        if exe.startswith(E2B_FC_PREFIX) and Path(exe.replace(" (deleted)", "")).name.startswith("firecracker"):
            pids.append(int(proc.name))
    return pids


def e2b_netns():
    """e2b 的网络命名空间个数（/run/netns/ns-<n>；AgentENV 的在 /run/aenv/netns/，不算）。"""
    d = Path("/run/netns")
    return sum(1 for p in d.iterdir() if re.fullmatch(r"ns-\d+", p.name)) if d.is_dir() else 0


def nat_rules():
    try:
        return len(run("iptables", "-t", "nat", "-S").splitlines())
    except (OSError, subprocess.CalledProcessError):
        return -1


def no_sandboxes():
    left = e2b_firecrackers()
    require(not left, "还有 %d 个 e2b 沙箱（firecracker）在运行；950.sh 的命令会先删掉它们，单独执行这一步时先删沙箱" % len(left))


def load_job():
    return normalize(json.loads(run("nomad", "job", "inspect", "-json", JOB)))


def write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(str(path) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=2)
    os.replace(str(path) + ".tmp", path)


def verify():
    subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS"], cwd=ROOT, check=True)


def status():
    cur = current()
    print("当前栈:", {"criu": "进程级 criu", "sandbox": "沙箱级 sandbox"}.get(cur, "认不出（5008 上的进程数不是 1）"))
    print("job:", JOB, "port:", PORT, "baseline:", state() / "baseline.json")
    print(json.dumps(processes(), ensure_ascii=False, indent=2))
    print("health:", 200 if health() else "不可用")
    print("e2b 沙箱（firecracker）:", len(e2b_firecrackers()), " e2b netns:", e2b_netns(), " nat 规则:", nat_rules())


def preflight():
    verify()
    unpack.unpack()
    require(os.uname().machine == "aarch64", "目标机必须为 aarch64")
    for cmd in ("nomad", "pgrep", "install", "sha256sum", "ldd", "file"):
        require(shutil.which(cmd), "缺少命令: " + cmd)
    require(Path("/dev/kvm").exists() and os.access("/dev/kvm", os.R_OK | os.W_OK), "缺 /dev/kvm 或当前用户不可读写")
    print(run("getconf", "GNU_LIBC_VERSION").strip())
    print(run("file", str(FC / "orchestrator")).strip())
    check = subprocess.run(["ldd", str(FC / "orchestrator")], capture_output=True, text=True)
    libs = check.stdout + check.stderr
    require(check.returncode == 0 and "not found" not in libs and "GLIBC_" not in libs,
            "orchestrator 运行库不兼容:\n" + libs)
    print(libs.strip())
    require(run(str(FC / "gsd"), "-commit").strip() == unpack.manifest()["artifacts"]["gsd"].get("commit", "259f1a25c"), "gsd 提交号不符")
    doc = load_job()
    group, task = select_task(doc)
    print("task:", group["Name"], "/", task["Name"], "binary:", binary(task))
    require(task.get("Env", {}).get("ORCHESTRATOR_SERVICES") == "orchestrator,template-manager",
            "当前脚本只处理合并的 orchestrator/template-manager job，现场服务配置不同")
    # 本包的 orchestrator 网卡叫 e2bv-<n>，地址池来自 job 环境；缺这两项会回落到与 AgentENV 相同的 10.11/10.12。
    env = task.get("Env", {})
    pools = {key: env.get(key) for key in ("SANDBOXES_HOST_NETWORK_CIDR", "SANDBOXES_VRT_NETWORK_CIDR")}
    print("地址池:", pools)
    require(all(pools.values()), "job 缺沙箱地址池环境变量，现场部署早于 e2b-infra 870d087，先升级部署或补齐 job 环境")
    require(len(processes()) == 1 and health(), "当前 orchestrator 数量或健康状态异常")
    no_sandboxes()
    envs.run(envs.CRIU_ENV, [ROOT / "ops/check_python.py"], check=True)
    print("宿主预检 PASS。Harbor 导入、客户端凭据和 guest 自检按 README 继续。")


def save_baseline():
    no_sandboxes()
    STATE = state()
    require(not (STATE / "baseline.json").exists(), "基线已存在，拒绝覆盖；重复切换直接用 criu/sandbox")
    doc = load_job()
    _, task = select_task(doc)
    path = binary(task)
    active = processes()
    require(len(active) == 1 and health(), "必须在原沙箱级服务健康运行时保存")
    require(not is_criu(path) and not is_criu(active[0]["exe"]), "当前是进程级栈，不能保存成沙箱级基线")
    require(active[0]["sha256"] == sha(path) and active[0]["exe"] == path, "job 路径与运行中二进制不一致")
    write_json(STATE / "baseline.identity.json", active[0])
    write_json(STATE / "baseline.json", doc)
    (STATE / "baseline.sha256").write_text(sha(STATE / "baseline.json") + "\n")
    os.chmod(STATE / "baseline.sha256", 0o600)
    print("已保存本机沙箱级基线（含密钥，只留在本机，不打印）:", STATE)


def baseline_redeployed():
    """沙箱级重新部署过（RPM 升级、build.sh 重装）时返回原因：基线记录的二进制与现在 job 里运行的
    不同，而现在确实是沙箱级、服务健康。其余情况返回 None。"""
    STATE = state()
    ident_p = STATE / "baseline.identity.json"
    if not ident_p.exists():
        return None
    ident = json.loads(ident_p.read_text())
    if Path(ident["exe"]).is_file() and sha(ident["exe"]) == ident["sha256"]:
        return None
    active = processes()
    if len(active) != 1 or is_criu(active[0]["exe"]) or not health():
        return None
    return "沙箱级二进制已由 %s 换成 %s（重新部署过）" % (ident["sha256"][:12], active[0]["sha256"][:12])


def refresh_baseline(reason):
    """重新部署之后：旧基线带时间戳留作备份，按现在的沙箱级 job 重新保存。"""
    STATE = state()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name in ("baseline.json", "baseline.sha256", "baseline.identity.json"):
        if (STATE / name).exists():
            os.replace(STATE / name, STATE / ("%s.before-%s" % (name, stamp)))
    print(reason + "，旧基线已改名为 *.before-%s，重新保存" % stamp)
    save_baseline()


def baseline():
    STATE = state()
    p = STATE / "baseline.json"
    require(p.exists(), "缺本机基线，请先在沙箱级环境运行 save-baseline")
    require(sha(p) == (STATE / "baseline.sha256").read_text().strip(), "本机基线校验失败")
    doc = normalize(json.loads(p.read_text()))
    ident = json.loads((STATE / "baseline.identity.json").read_text())
    require(Path(ident["exe"]).is_file() and sha(ident["exe"]) == ident["sha256"], "原沙箱级二进制已变化；先核对，不能自动回切")
    return doc, ident


def is_criu(exe):
    return exe.startswith(str(hfc()) + "/")


def criu_env():
    """进程级 job 里要设的三个环境变量（与 ENV_KEYS 对应）。HOST_GSD_PATH 是 orchestrator 读的名字，
    它的内置默认是 /fc-gsd/gsd，这里显式指向 /fc-criu/gsd。"""
    return {"HOST_GSD_PATH": str(hfc() / "gsd"), "HOST_KERNELS_DIR": str(hfc() / "kernels"),
            "CRIU_ORCH_SHA256": sha(hfc() / "orchestrator")}


def make_criu(doc):
    doc = copy.deepcopy(doc)
    _, task = select_task(doc)
    old = binary(task)
    task["Config"]["args"][1] = task["Config"]["args"][1].replace(old, str(hfc() / "orchestrator"))
    task.setdefault("Env", {}).update(criu_env())
    return doc


def install():
    preflight()
    place_files()


def place_files():
    baseline()
    unpack.unpack()
    # 已存在且内容不同就停，不覆盖现场另一轮进程级产物。例外是 manifest 里这一项的 replaces：
    # 以前交付过、被这一版取代的版本（gsd 修复后升级），可以直接换掉。
    replaces = {item["path"]: set(item.get("replaces", [])) for item in unpack.manifest()["artifacts"].values()}
    stale = []
    for name in FILES:
        dst = hfc() / name
        if not dst.exists() or sha(dst) == sha(FC / name):
            continue
        require(sha(dst) in replaces.get(name, ()), "现场文件与交付版本不同，拒绝覆盖: " + str(dst))
        stale.append(name)
    for name in FILES:
        # 内容相同就不重写：重写会换掉正在运行的 orchestrator 的文件，进程路径变成 "(deleted)"。
        if (hfc() / name).exists() and name not in stale:
            continue
        if name in stale:
            print("换掉旧版 %s（%s → %s）" % (hfc() / name, sha(hfc() / name)[:12], sha(FC / name)[:12]))
        subprocess.run(["install", "-D", "-m", "644" if name.endswith(".bin") else "755",
                        str(FC / name), str(hfc() / name)], check=True)
    print("三个运行文件落位完成:", hfc())


def current():
    """当前 5008 上是哪一套：criu、sandbox，进程数不是 1 时为 None。"""
    ps = processes()
    if len(ps) != 1:
        return None
    return "criu" if is_criu(ps[0]["exe"]) else "sandbox"


def matches(ps, expected):
    return (len(ps) == 1 and ps[0]["exe"] == expected["exe"] and ps[0]["sha256"] == expected["sha256"]
            and ps[0]["env"] == expected["env"])


def target_job(target):
    """目标栈的 job 定义与切换后应看到的进程（路径、sha256、三个环境变量）。"""
    doc, ident = baseline()
    if target == "sandbox":
        return doc, ident
    verify()
    unpack.unpack()
    for name in FILES:
        require(sha(hfc() / name) == sha(FC / name), "运行文件与仓库里的交付版本不同: %s" % (hfc() / name))
    exe = str(hfc() / "orchestrator")
    return make_criu(doc), {"exe": exe, "sha256": sha(exe), "env": criu_env()}


def stop_job():
    """停 job 并等进程退出。失败不强杀、不 purge。"""
    run("nomad", "job", "stop", "-detach", JOB)
    for _ in range(60):
        if not processes() and not health():
            return
        time.sleep(2)
    raise RuntimeError("旧服务 120 秒未退出，停止操作；请核对 Nomad alloc 后重跑")


def start_job(target, doc, expected):
    path = state() / ("job-" + target + ".json")
    write_json(path, doc)
    run("nomad", "job", "run", "-detach", "-json", str(path))
    for _ in range(72):
        if matches(processes(), expected) and health():
            time.sleep(20)
            require(matches(processes(), expected) and health(), "启动后的等待期内进程或 health 变化，不能判定成功")
            print("已启动%s栈（已核对实际进程 SHA256、环境和 health）" % ("进程级" if target == "criu" else "沙箱级"), flush=True)
            return
        time.sleep(5)
    raise RuntimeError("360 秒内未确认服务启动；查 Nomad 日志，必要时执行 bash criu-rollback/deploy/950.sh sandbox 恢复原基线")


def switch(target):
    doc, expected = target_job(target)
    if matches(processes(), expected) and health():
        print("已经是", target, "栈，不重启服务", flush=True)
        return
    no_sandboxes()
    # 先停旧 job，避免两个 alloc 争用固定端口。
    print("准备切换到", target, flush=True)
    stop_job()
    start_job(target, doc, expected)
    status()


def recycle_netns():
    """清理重启 orchestrator 留下的 e2b 网络命名空间与 iptables 规则，然后按原栈重新拉起。

    停 job → build.sh --recycle-netns（此时 /usr/bin/template-manager 不在运行，它只清理、不拉起；
    只认 e2bv- 网卡、ns-<n> 和本机地址池，不碰 AgentENV）→ 按当前栈 nomad job run。"""
    target = current()
    require(target in ("criu", "sandbox") and health(), "当前服务状态认不出，先执行 950.sh status 核对")
    require(Path(BUILD_SH).is_file(), "找不到 %s；在 ops/site.env 写 E2B_INFRA_BUILD=build.sh 路径" % BUILD_SH)
    doc, expected = target_job(target)
    no_sandboxes()
    before = (e2b_netns(), nat_rules())
    print("清理前：e2b netns %d 个，nat 规则 %d 条；当前栈 %s" % (before + (target,)), flush=True)
    stop_job()
    r = subprocess.run(["bash", BUILD_SH, "--recycle-netns"], capture_output=True, text=True)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
    try:
        require(r.returncode == 0, "build.sh --recycle-netns 失败：\n" + tail)
    finally:
        start_job(target, doc, expected)
    print("清理后：e2b netns %d 个，nat 规则 %d 条（新进程会再建一池暖池）" % (e2b_netns(), nat_rules()), flush=True)


def main():
    action = sys.argv[1]
    funcs = {"status": status, "preflight": preflight, "save-baseline": save_baseline, "install": install}
    if action in funcs:
        funcs[action]()
    elif action in ("criu", "sandbox"):
        switch(action)
    else:
        raise RuntimeError("未知操作")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as e:
        print("停止:", e, file=sys.stderr)
        sys.exit(1)
