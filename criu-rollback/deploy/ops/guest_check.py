"""在模板沙箱里确认实际使用的产物及运行条件，失败返回非零。"""
import hashlib
import json
import os
from pathlib import Path
import sys

import paths
ROOT = paths.ROOT
sys.path.insert(0, str(paths.SCRIPTS))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox

sb = Sandbox.create(template=os.environ.get("TEMPLATE", "base-criu"), timeout=300)
try:
    print("sandbox", sb.sandbox_id, "template", sb.get_info().template_id)
    code = r'''
import hashlib, json, os, subprocess
def sh(s):
    return subprocess.check_output(s, shell=True, text=True).strip()
def mode(p):
    s = os.stat(p)
    return [oct(s.st_mode & 0o7777), s.st_uid, s.st_gid]
print(json.dumps({
    "commit": sh("/usr/bin/gsd -commit"),
    "gsd_sha256": hashlib.sha256(open("/usr/bin/gsd", "rb").read()).hexdigest(),
    "kernel": sh("cat /proc/version"),
    "thp": sh("cat /sys/kernel/mm/transparent_hugepage/enabled"),
    "rcu": sh("cat /sys/kernel/rcu_expedited"),
    "service": sh("systemctl is-active gsd"),
    "health": json.loads(sh("curl -fsS -m 5 http://127.0.0.1:49984/health")),
    "criu": sh("/usr/sbin/criu --version"),
    "root": mode("/"), "tmp": mode("/tmp"),
    "tmp_fs": sh("df /tmp | tail -1").split()[0],
    "vcpus": sh("nproc"), "memory": sh("grep MemTotal /proc/meminfo"),
}, ensure_ascii=False))
'''
    import shlex
    result = sb.commands.run("python3 -c " + shlex.quote(code), user="root", timeout=60)
    data = json.loads(result.stdout)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    expected = json.loads((ROOT / "manifest.json").read_text())
    checks = {
        "commit": data["commit"] == "259f1a25c",
        "gsd_sha256": data["gsd_sha256"] == expected["artifacts"]["gsd"]["sha256"],
        "kernel": "6.1.158+" in data["kernel"] and "#4 SMP Thu Oct  8 20:06:40 CST 2026" in data["kernel"],
        "thp": "[never]" in data["thp"], "rcu": data["rcu"] == "1",
        "service": data["service"] == "active", "health": data["health"].get("status") == "ok",
        "criu": data["criu"] == "Version: 4.2.1",
        "root": data["root"] == ["0o755", 0, 0], "tmp": data["tmp"] == ["0o1777", 0, 0],
        "tmp_fs": data["tmp_fs"] == "overlay", "vcpus": data["vcpus"] == "2",
    }
    bad = [k for k, ok in checks.items() if not ok]
    if bad:
        raise RuntimeError("guest 自检失败: " + str(bad))
    print("guest 自检 PASS")
finally:
    sb.kill()
