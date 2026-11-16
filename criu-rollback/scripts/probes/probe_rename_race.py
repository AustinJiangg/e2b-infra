#!/usr/bin/env python3
"""D：压测里偶发的 rename:ENOTEMPTY 是不是 restore 夹在一次 rename() 的两次路径查找之间。

    python3 probe_rename_race.py [模板名] [restore 次数]

c_B 时刻只有 /srv/rr/b（非空目录）。root 进程（systemd-run，不在 criu 树里，restore 后照样跑）不停地
rename(a,b) / rename(b,a)，宿主机反复 restore 到 c_B。若某次 rename(a,b) 查到 a（当前时间线）之后、查 b 之前
被 restore 切走，b 的查找在新时间线里看到 c_B 的非空目录 b，就得到 ENOTEMPTY（和两个进程同时 rename 时
普通文件系统给的错误一样）。统计各 errno，并在结束后检查目录状态是否仍然只有 a 或只有 b、内容完好。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env  # noqa: E402

os.environ.update(load_e2b_env())
from e2b import Sandbox, CommandExitException  # noqa: E402

TPL = sys.argv[1] if len(sys.argv) > 1 else "base-gsd"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 60

LOOP = r'''
import os, errno, json, time
a, b = "/srv/rr/a", "/srv/rr/b"
cnt = {}; ops = 0; last = 0
while True:
    for s, d in ((a, b), (b, a)):
        try:
            os.rename(s, d); k = "ok"
        except OSError as e:
            k = errno.errorcode.get(e.errno, str(e.errno))
        cnt[k] = cnt.get(k, 0) + 1; ops += 1
    now = time.time()
    if now - last > 0.5:
        with open("/dev/shm/rr.json.tmp", "w") as f: json.dump({"ops": ops, "cnt": cnt}, f)
        os.rename("/dev/shm/rr.json.tmp", "/dev/shm/rr.json"); last = now
'''


def main():
    sb = Sandbox.create(template=TPL, timeout=600)
    res = {"template": TPL, "sandbox": sb.sandbox_id, "restores": N}

    def sh(c):
        try:
            r = sb.commands.run(c, user="root", timeout=60)
            return r.stdout.strip()
        except CommandExitException as e:
            return f"rc={e.exit_code} {e.stdout.strip()} {e.stderr.strip()}"

    try:
        res["kernel"] = sh("cat /proc/version")
        sh("mkdir -p /srv/rr/b && echo x > /srv/rr/b/x")
        cb = sb.checkpoint.create("cB").checkpoint_id
        sb.files.write("/dev/shm/rr.py", LOOP, user="root")
        sh("systemd-run --unit=rr python3 /dev/shm/rr.py")
        time.sleep(1)
        t0 = time.time()
        for _ in range(N):
            sb.checkpoint.restore(cb)
        res["restore_s"] = round(time.time() - t0, 2)
        time.sleep(1)
        sh("systemctl stop rr")
        res["loop"] = json.loads(sh("cat /dev/shm/rr.json") or "{}")
        res["final_ls"] = sh("ls /srv/rr; cat /srv/rr/*/x")
        res["consistent"] = res["final_ls"] in ("a\nx", "b\nx") or res["final_ls"].replace("\n", " ") in ("a x", "b x")
        res["dmesg"] = sh("dmesg | grep -i -E 'overlay|BUG|WARN|KASAN|lockdep' | grep -v overlay-init | tail -5")
    finally:
        sb.kill()
    print(json.dumps(res, ensure_ascii=False, indent=1))


main()
