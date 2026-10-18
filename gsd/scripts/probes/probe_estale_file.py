#!/usr/bin/env python3
"""目录跨 restore 语义修复的非目录分支：restore 后按路径访问一个"被别人只读打开着、checkpoint 之后又改过"的文件。

    python3 probe_estale_file.py [模板名，默认 base-gsd]

f 在 c1 之前建好（c1 时在 upper，沉进 L1）；root 进程（systemd-run，不在 criu 树里）只读打开 f 并一直拿着；
c1 之后按路径追加一行（copy-up），建 c2（这份 upper 沉进 L2，inode 的 lower 被改到 L2），再 restore c1。
这时 f 在目标里的内容是 c1 的；修复之前按路径 stat/cat 会拿到 ESTALE（缓存的 inode 按 L1/f 哈希、lower 却指向 L2/f），
修复之后应该看到 c1 的内容，持有者自己手里的 fd 读到的仍是它打开的那个文件。
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
HOLD = r'''
import os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY)
open("/dev/shm/hf.ack", "w").write("ready")
while True:
    if os.path.exists("/dev/shm/hf.go"):
        open("/dev/shm/hf.out", "w").write(os.pread(fd, 4096, 0).decode())
        os.unlink("/dev/shm/hf.go")
    time.sleep(0.05)
'''


def main():
    sb = Sandbox.create(template=TPL, timeout=600)
    res = {"template": TPL, "sandbox": sb.sandbox_id}

    def sh(c):
        try:
            r = sb.commands.run(c, user="root", timeout=60)
            return "OK " + r.stdout.strip().replace("\n", " | ")
        except CommandExitException as e:
            return f"rc={e.exit_code} {(e.stdout + e.stderr).strip()}"

    def holder_view():
        sh("touch /dev/shm/hf.go")
        for _ in range(50):
            if sh("test -e /dev/shm/hf.go && echo y || echo n") == "OK n":
                break
            time.sleep(0.1)
        return sh("cat /dev/shm/hf.out")

    try:
        res["kernel"] = sh("cat /proc/version")
        sh("mkdir -p /srv/hf && echo line1 > /srv/hf/f")
        c1 = sb.checkpoint.create("c1").checkpoint_id
        sb.files.write("/dev/shm/hold.py", HOLD, user="root")
        sh("systemd-run --unit=hf python3 /dev/shm/hold.py /srv/hf/f")
        for _ in range(50):
            if sh("cat /dev/shm/hf.ack 2>/dev/null") == "OK ready":
                break
            time.sleep(0.1)
        sh("echo line2 >> /srv/hf/f")
        res["before_c2"] = sh("cat /srv/hf/f")
        sb.checkpoint.create("c2")
        sb.checkpoint.restore(c1)
        res["after_restore_path"] = {c: sh(c) for c in ("stat -c %s /srv/hf/f", "cat /srv/hf/f", "ls /srv/hf", "echo line3 >> /srv/hf/f && cat /srv/hf/f")}
        res["holder_fd_view"] = holder_view()
        res["ok"] = res["after_restore_path"]["cat /srv/hf/f"] == "OK line1" and \
            res["after_restore_path"]["echo line3 >> /srv/hf/f && cat /srv/hf/f"] == "OK line1 | line3"
        res["dmesg"] = sh("dmesg | grep -i -E 'overlay|BUG|WARN|KASAN' | grep -v overlay-init | tail -5")
    finally:
        sb.kill()
    print(json.dumps(res, ensure_ascii=False, indent=1))


main()
