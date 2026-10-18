#!/usr/bin/env python3
"""打开的文件跨切换语义的例外：被 mmap 着的文件，checkpoint 之后的写入 restore 后还在不在。

（该语义：checkpoint 之前打开的 fd，checkpoint 之后经它写入的内容落到新 upper，随 restore 回滚。）

    python3 probe_mmap_rollback.py [模板名，默认 base-gsd]

root 进程（systemd-run，gsd 不碰它）在 checkpoint 前打开文件、写第 1 行，checkpoint c1 之后再写第 2 行：
  M0  只有 fd，经 fd 写（对照，按上述语义应回滚）
  M1  MAP_SHARED 可写映射，经映射写（再 msync）
  M2  MAP_SHARED 只读映射还在，经 fd 写
  M3  MAP_PRIVATE 映射还在，经 fd 写
  M4  MAP_SHARED 可写映射，但经 fd 写
每个文件 4096 字节（第 1 行在开头，第 2 行写在偏移 64），restore c1 后读回文件和沉下去的层里的真实文件。
另外看 uid 1000 的同款（U1：MAP_SHARED 可写映射经映射写；这个进程会被 criu dump/restore）。

第二阶段（PHASE2=0 可关）：restore c1 之后重启 root 进程（按路径重新打开，走 O_TMPFILE copy-up），各进程写第 4 行（偏移 192），建 c2（U1 进程带着这个映射再被 criu dump 一次），
再写第 5 行，restore c2 → 应有 -4、没有 -5；再 restore c1 → 只剩第 1 行；同时读 c1、c2 两个沉下去的层里的文件。
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

HOLDER = r'''
import mmap, os, sys, time
path, kind = sys.argv[1], sys.argv[2]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
os.write(fd, (kind + "-1\n").encode().ljust(64, b"."))
os.ftruncate(fd, 4096); os.fsync(fd)
m = None
if kind in ("M1", "M4", "U1"):
    m = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
elif kind == "M2":
    m = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ)
elif kind == "M3":
    m = mmap.mmap(fd, 4096, mmap.MAP_PRIVATE, mmap.PROT_READ | mmap.PROT_WRITE)
if m is not None:
    _ = m[0]  # 让映射真正建立
open(path + ".ack", "w").write("ready"); done = None
while True:
    if os.path.exists(path + ".go"):
        n = open(path + ".go").read().strip() or "2"
        if n != done:
            line = (kind + "-" + n + "\n").encode()
            off = 64 * (int(n) - 1)
            if kind in ("M1", "U1"):
                m[off:off + len(line)] = line; m.flush()
            else:
                os.pwrite(fd, line, off); os.fsync(fd)
            done = n
            open(path + ".ack", "w").write("done" + n)
    time.sleep(0.05)
'''


def main():
    sb = Sandbox.create(template=TPL, timeout=600)
    res = {"template": TPL, "sandbox": sb.sandbox_id, "cases": {}}

    def sh(c, user="root"):
        try:
            r = sb.commands.run(c, user=user, timeout=60)
            return r.stdout.strip()
        except CommandExitException as e:
            return f"rc={e.exit_code} {e.stdout.strip()} {e.stderr.strip()}"

    def content(p):
        return sh(f"tr -d '\\000' < {p} 2>&1 | tr -s '.' | tr '\\n' ' '")

    try:
        res["kernel"] = sh("cat /proc/version")
        res["gsd"] = sh("/usr/bin/gsd -commit")
        sb.files.write("/home/user/holder_mm.py", HOLDER)
        sh("mkdir -p /srv/mm && chmod 777 /srv/mm && cp /home/user/holder_mm.py /srv/mm/h.py")
        cases = ["M0", "M1", "M2", "M3", "M4"]
        for k in cases:
            sh(f"systemd-run --unit=mm{k} python3 /srv/mm/h.py /srv/mm/F_{k} {k}")
        sb.commands.run("python3 /home/user/holder_mm.py /home/user/F_U1 U1", background=True)
        paths = {k: f"/srv/mm/F_{k}" for k in cases}
        paths["U1"] = "/home/user/F_U1"
        for _ in range(100):
            if all(sh(f"cat {p}.ack 2>/dev/null") == "ready" for p in paths.values()):
                break
            time.sleep(0.2)
        res["maps_before"] = sh("for u in " + " ".join(f"mm{k}" for k in cases) +
                                "; do p=$(systemctl show -p MainPID --value $u); echo \"$u $(grep -c /srv/mm /proc/$p/maps)\"; done | tr '\\n' ' '")
        c1 = sb.checkpoint.create("c1")
        st = json.loads(sh("cat /var/lib/gsd/snapshots/state.json 2>/dev/null || echo {}") or "{}")
        top = [c for c in st.get("checkpoints", []) if c.get("id") == c1.checkpoint_id]
        sunk = top[0]["layers"][0] if top else sh("ls -d /overlay/layers/L* 2>/dev/null | tail -1")
        res["c1"] = c1.checkpoint_id
        res["sunk_layer"] = sunk
        def go(n):
            for p in paths.values():
                sh(f"echo {n} > {p}.go; chmod 666 {p}.go")
            for _ in range(100):
                if all(sh(f"cat {p}.ack 2>/dev/null") == f"done{n}" for p in paths.values()):
                    return True
                time.sleep(0.2)
            return False

        res["go2"] = go(2)
        after_write = {k: content(p) for k, p in paths.items()}
        sb.checkpoint.restore(c1.checkpoint_id)
        for k, p in paths.items():
            rel = p.lstrip("/")
            res["cases"][k] = {
                "after_write": after_write[k],
                "after_restore": content(p),
                "in_sunk_layer": content(f"{sunk}/{rel}"),
                "rolled_back": "-2" not in content(p),
            }
        res["holders_alive"] = sh("systemctl is-active " + " ".join(f"mm{k}" for k in cases) + " | tr '\\n' ' '")
        if os.environ.get("PHASE2", "1") != "0":
            ph = {"u1_alive_after_restore_c1": sh("pgrep -u 1000 -f holder_mm.py | wc -l")}
            # root 进程不在 criu 树里：restore 后它们手里是被摘下（detached）的旧文件。重启它们，
            # 让新进程按路径重新打开（这次走 copy-up，upper 是 O_TMPFILE 建的），再测一轮 c2。
            sh("systemctl restart " + " ".join(f"mm{k}" for k in cases))
            for _ in range(100):
                if all(sh(f"cat {p}.ack 2>/dev/null") == "ready" for p in paths.values()):
                    break
                time.sleep(0.2)
            ph["maps_restarted"] = sh("for u in " + " ".join(f"mm{k}" for k in cases) +
                                      "; do p=$(systemctl show -p MainPID --value $u); echo \"$u $(grep -c /srv/mm /proc/$p/maps)\"; done | tr '\\n' ' '")
            ph["go4"] = go(4)
            ph["after_write4"] = {k: content(p) for k, p in paths.items()}
            c2 = sb.checkpoint.create("c2")
            st = json.loads(sh("cat /var/lib/gsd/snapshots/state.json 2>/dev/null || echo {}") or "{}")
            top2 = [c for c in st.get("checkpoints", []) if c.get("id") == c2.checkpoint_id]
            sunk2 = top2[0]["layers"][0] if top2 else ""
            ph["c2"], ph["sunk_layer_c2"] = c2.checkpoint_id, sunk2
            ph["u1_alive_after_c2"] = sh("pgrep -u 1000 -f holder_mm.py | wc -l")
            ph["go5"] = go(5)
            ph["after_write5"] = {k: content(p) for k, p in paths.items()}
            sb.checkpoint.restore(c2.checkpoint_id)
            ph["u1_alive_after_restore_c2"] = sh("pgrep -u 1000 -f holder_mm.py | wc -l")
            ph["after_restore_c2"] = {k: content(p) for k, p in paths.items()}
            ph["c2_ok"] = {k: (v == ph["after_write4"][k] and "-4" in v and "-5" not in v) for k, v in ph["after_restore_c2"].items()}
            ph["in_sunk_c1"] = {k: content(f"{sunk}/{p.lstrip('/')}") for k, p in paths.items()}
            ph["in_sunk_c2"] = {k: content(f"{sunk2}/{p.lstrip('/')}") for k, p in paths.items()} if sunk2 else {}
            sb.checkpoint.restore(c1.checkpoint_id)
            ph["after_restore_c1_again"] = {k: content(p) for k, p in paths.items()}
            ph["c1_again_ok"] = {k: ("-2" not in v and "-4" not in v and "-5" not in v) for k, v in ph["after_restore_c1_again"].items()}
            ph["sunk_c1_clean"] = {k: ("-2" not in v and "-4" not in v and "-5" not in v) for k, v in ph["in_sunk_c1"].items()}
            ph["sunk_c2_ok"] = {k: (v == ph["after_write4"][k]) for k, v in ph["in_sunk_c2"].items()}
            res["phase2"] = ph
        res["dmesg_tail"] = sh("dmesg | grep -i -E 'overlay|ovl|BUG|WARN' | grep -v overlay-init | tail -10")
    finally:
        sb.kill()
    print(json.dumps(res, ensure_ascii=False, indent=1))


main()
