#!/usr/bin/env python3
"""950 上保存本机基线、检查、落位、切换及恢复。只使用 Python 标准库。"""
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
STATE = Path(os.environ.get("GSD_STATE_DIR", "/var/lib/gsd-950-offline"))
JOB = os.environ.get("GSD_JOB", "template-manager-system")
PORT = int(os.environ.get("GSD_PORT", "5008"))
FILES = ("gsd", "orchestrator", "kernels/vmlinux-6.1.158/vmlinux.bin")
META = ("Version", "SubmitTime", "CreateIndex", "ModifyIndex", "JobModifyIndex", "Status", "StatusDescription")
ENV_KEYS = ("HOST_GSD_PATH", "HOST_KERNELS_DIR", "GSD_ORCH_SHA256")


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


def no_sandboxes():
    p = subprocess.run(["pgrep", "-x", "firecracker"], capture_output=True, text=True)
    require(p.returncode == 1, "还有 firecracker 沙箱运行，或无法确认进程状态；先结束沙箱和模板构建")


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
    print("job:", JOB, "port:", PORT, "baseline:", STATE / "baseline.json")
    print(json.dumps(processes(), ensure_ascii=False, indent=2))
    print("health:", 200 if health() else "不可用")


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
    require(run(str(FC / "gsd"), "-commit").strip() == "259f1a25c", "gsd 提交号不符")
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
    envs.run(envs.GSD_ENV, [ROOT / "ops/check_python.py"], check=True)
    print("宿主预检 PASS。Harbor 导入、客户端凭据和 guest 自检按 README 继续。")


def save_baseline():
    no_sandboxes()
    require(not (STATE / "baseline.json").exists(), "基线已存在，拒绝覆盖；重复切换直接用 gsd/sandbox")
    doc = load_job()
    _, task = select_task(doc)
    path = binary(task)
    active = processes()
    require(len(active) == 1 and health(), "必须在原沙箱级服务健康运行时保存")
    require(not path.startswith("/fc-gsd/") and not active[0]["exe"].startswith("/fc-gsd/"), "当前是 gsd，不能保存成沙箱级基线")
    require(active[0]["sha256"] == sha(path) and active[0]["exe"] == path, "job 路径与运行中二进制不一致")
    write_json(STATE / "baseline.identity.json", active[0])
    write_json(STATE / "baseline.json", doc)
    (STATE / "baseline.sha256").write_text(sha(STATE / "baseline.json") + "\n")
    os.chmod(STATE / "baseline.sha256", 0o600)
    print("已保存 950 本机沙箱级基线（含密钥，不输出、不回传）:", STATE)


def baseline():
    p = STATE / "baseline.json"
    require(p.exists(), "缺本机基线，请先在沙箱级环境运行 save-baseline")
    require(sha(p) == (STATE / "baseline.sha256").read_text().strip(), "本机基线校验失败")
    doc = normalize(json.loads(p.read_text()))
    ident = json.loads((STATE / "baseline.identity.json").read_text())
    require(Path(ident["exe"]).is_file() and sha(ident["exe"]) == ident["sha256"], "原沙箱级二进制已变化；先核对，不能自动回切")
    return doc, ident


def make_gsd(doc):
    doc = copy.deepcopy(doc)
    _, task = select_task(doc)
    old = binary(task)
    task["Config"]["args"][1] = task["Config"]["args"][1].replace(old, "/fc-gsd/orchestrator")
    env = task.setdefault("Env", {})
    env.pop("HOST_GSD_PATH", None)  # 沿用已验证的默认 /fc-gsd/gsd
    env["HOST_KERNELS_DIR"] = "/fc-gsd/kernels"
    env["GSD_ORCH_SHA256"] = sha("/fc-gsd/orchestrator")
    return doc


def install():
    preflight()
    place_files()


def place_files():
    baseline()
    unpack.unpack()
    # 已存在且内容不同就停，不覆盖现场另一轮 gsd 产物。
    for name in FILES:
        dst = Path("/fc-gsd") / name
        require(not dst.exists() or sha(dst) == sha(FC / name), "现场文件与交付版本不同，拒绝覆盖: " + str(dst))
    for name in FILES:
        # 内容相同就不重写：重写会换掉正在运行的 orchestrator 的文件，进程路径变成 "(deleted)"。
        if (Path("/fc-gsd") / name).exists():
            continue
        subprocess.run(["install", "-D", "-m", "644" if name.endswith(".bin") else "755",
                        str(FC / name), str(Path("/fc-gsd") / name)], check=True)
    print("三个运行文件落位完成")


def current():
    """当前 5008 上是哪一套：gsd、sandbox，进程数不是 1 时为 None。"""
    ps = processes()
    if len(ps) != 1:
        return None
    return "gsd" if ps[0]["exe"].startswith("/fc-gsd/") else "sandbox"


def matches(ps, expected):
    return (len(ps) == 1 and ps[0]["exe"] == expected["exe"] and ps[0]["sha256"] == expected["sha256"]
            and ps[0]["env"] == expected["env"])


def switch(target):
    doc, ident = baseline()
    if target == "gsd":
        verify()
        unpack.unpack()
        for name in FILES:
            require(sha(Path("/fc-gsd") / name) == sha(FC / name), "运行文件与交付包不同")
        doc = make_gsd(doc)
        expected = {"exe": "/fc-gsd/orchestrator", "sha256": sha("/fc-gsd/orchestrator"),
                    "env": {"HOST_GSD_PATH": None, "HOST_KERNELS_DIR": "/fc-gsd/kernels",
                            "GSD_ORCH_SHA256": sha("/fc-gsd/orchestrator")}}
    else:
        expected = ident
    if matches(processes(), expected) and health():
        print("已经是", target, "栈，不重启服务", flush=True)
        return
    no_sandboxes()
    path = STATE / ("job-" + target + ".json")
    write_json(path, doc)
    # 先停旧 job，避免两个 alloc 争用固定端口。失败不强杀，不自动 purge。
    print("准备切换到", target, flush=True)
    run("nomad", "job", "stop", "-detach", JOB)
    for _ in range(60):
        if not processes() and not health():
            break
        time.sleep(2)
    else:
        raise RuntimeError("旧服务 120 秒未退出，停止操作；请核对 Nomad alloc 后重跑")
    run("nomad", "job", "run", "-detach", "-json", str(path))
    for _ in range(72):
        ps = processes()
        if matches(ps, expected) and health():
            time.sleep(20)
            require(matches(processes(), expected) and health(), "切换后的等待期内进程或 health 变化，不能判定成功")
            print("切换完成（已核对实际进程 SHA256、环境和 health）")
            status()
            return
        time.sleep(5)
    raise RuntimeError("360 秒内未确认切换成功；查 Nomad 日志，必要时执行 sandbox 恢复原基线")


def main():
    action = sys.argv[1]
    funcs = {"status": status, "preflight": preflight, "save-baseline": save_baseline, "install": install}
    if action in funcs:
        funcs[action]()
    elif action in ("gsd", "sandbox"):
        switch(action)
    else:
        raise RuntimeError("未知操作")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as e:
        print("停止:", e, file=sys.stderr)
        sys.exit(1)
