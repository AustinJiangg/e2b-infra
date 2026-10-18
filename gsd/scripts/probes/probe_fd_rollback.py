#!/usr/bin/env python3
"""两个现场疑点的最小复现（verify_fixes 的 G-25 / G-11 引出）。

    python3 probe_fd_rollback.py fd [模板名]   checkpoint 前打开、checkpoint 后经这个 fd 写入的内容，restore 后还在不在
    python3 probe_fd_rollback.py df [模板名]   create → restore 基点 → delete 循环后根盘占用，以及 drop_caches 之后

fd：
  U = uid 1000 进程（被 criu dump / restore），checkpoint 前 open(F_U,'a') 写 U1，checkpoint 后经同一个 fd 写 U2
  R = root 进程（gsd 不碰它），同样写 R1 / R2（经 fd）
  P = 对照：checkpoint 后用路径 `echo P2 >> F_P` 追加（重新 open，走 copy-up）
  期望：restore 到 c1 后三个文件都只剩第一行。另外直接看沉下去的层目录里的真实文件，判断写入落在哪一层。
guest 里直接 curl 49984 需要 token 的地方都走 SDK。
"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox, CommandExitException

MODE = sys.argv[1] if len(sys.argv) > 1 else "fd"
TPL = sys.argv[2] if len(sys.argv) > 2 else "base-gsd"

HOLDER = r'''
import os, sys, time
path, tag = sys.argv[1], sys.argv[2]
f = open(path, "a"); f.write(tag + "1\n"); f.flush()
open(path + ".ack", "w").write("ready"); done = False
while True:
    if not done and os.path.exists(path + ".go"):
        f.write(tag + "2\n"); f.flush(); done = True
        open(path + ".ack", "w").write("done")
    time.sleep(0.05)
'''


def sh(sb, c, user="root"):
    try:
        r = sb.commands.run(c, user=user, timeout=120)
        return r.stdout.strip()
    except CommandExitException as e:
        return f"rc={e.exit_code} {e.stdout} {e.stderr}".strip()


def wait(sb, path, want):
    for _ in range(100):
        if want in sh(sb, f"cat {path} 2>/dev/null || true"):
            return True
        time.sleep(0.1)
    return False


def fd_probe(sb):
    sb.files.write("/home/user/holder.py", HOLDER)
    sh(sb, "cp /home/user/holder.py /root/holder.py")
    sb.commands.run("python3 /home/user/holder.py /home/user/F_U U", background=True)
    sh(sb, "systemd-run --unit=holder-r -p StandardOutput=null -p StandardError=null python3 /root/holder.py /home/user/F_R R")
    sh(sb, "echo P1 > /home/user/F_P", user="user")
    print("U ready", wait(sb, "/home/user/F_U.ack", "ready"), " R ready", wait(sb, "/home/user/F_R.ack", "ready"))
    c1 = sb.checkpoint.create("c1")
    st = json.loads(sh(sb, "cat /var/lib/gsd/snapshots/state.json 2>/dev/null || echo {}") or "{}")
    top = [c for c in st.get("checkpoints", []) if c.get("id") == c1.checkpoint_id]
    sunk = top[0]["layers"][0] if top else sh(sb, "ls -d /overlay/layers/L* /overlay/layers/layer_* 2>/dev/null | tail -1")
    print(f"c1={c1.checkpoint_id} has_process_image={getattr(c1, "has_process_image", "?")} 沉下去的层={sunk}")
    sh(sb, "touch /home/user/F_U.go /home/user/F_R.go")
    print("U done", wait(sb, "/home/user/F_U.ack", "done"), " R done", wait(sb, "/home/user/F_R.ack", "done"))
    sh(sb, "echo P2 >> /home/user/F_P", user="user")
    show = ("for f in F_U F_R F_P; do printf '%s: ' $f; tr '\\n' ' ' < /home/user/$f; "
            f"printf ' | 层 {sunk}: '; tr '\\n' ' ' < {sunk}/home/user/$f 2>/dev/null; echo; done")
    print("--- checkpoint 后写完：\n" + sh(sb, show))
    sb.checkpoint.restore(c1.checkpoint_id)
    time.sleep(0.3)
    print("--- restore c1 后（期望各文件只剩第 1 行）：\n" + sh(sb, show))
    print("uid1000:", sh(sb, "ps -u 1000 -o pid=,args="), "| holder-r:", sh(sb, "systemctl is-active holder-r"))


def df_probe(sb):
    sb.commands.run("sleep 100000", background=True)
    time.sleep(0.5)
    c0 = sb.checkpoint.create("c0")
    df = lambda: sh(sb, "df -k --output=used / | tail -1")
    print("起点 used KB", df(), "| slab dentry", sh(sb, "grep '^dentry ' /proc/slabinfo | awk '{print $2}'"))
    for i in range(30):
        sh(sb, "dd if=/dev/urandom of=/home/user/big.bin bs=1M count=1 status=none", user="user")
        c = sb.checkpoint.create(f"x{i}")
        sb.checkpoint.restore(c0.checkpoint_id)
        sb.checkpoint.delete(c.checkpoint_id)
    time.sleep(1)
    print("30 轮后 used KB", df(), "| layers:", sh(sb, "ls -A /overlay/layers | tr '\\n' ' '"))
    print("已删除但仍占空间的文件（lsof +L1 不可用时看 /proc/*/fd）:",
          sh(sb, "ls -l /proc/[0-9]*/fd 2>/dev/null | grep -c deleted || true"))
    sh(sb, "sync; echo 2 > /proc/sys/vm/drop_caches")
    time.sleep(1)
    print("drop_caches(2) 后 used KB", df())
    sh(sb, "sync; echo 3 > /proc/sys/vm/drop_caches")
    time.sleep(1)
    print("drop_caches(3) 后 used KB", df())
    ok = True
    try:
        sb.checkpoint.restore(c0.checkpoint_id)
    except Exception as e:  # noqa: BLE001
        ok = e
    print("drop_caches 之后再 restore c0:", ok, "| used KB", df())


sb = Sandbox.create(template=TPL, timeout=900)
print(f"[{TPL}] sandbox {sb.sandbox_id} gsd={sh(sb, '/usr/bin/gsd -commit')} kernel={sh(sb, 'cat /proc/version')[:90]}")
try:
    fd_probe(sb) if MODE == "fd" else df_probe(sb)
finally:
    sb.kill()
