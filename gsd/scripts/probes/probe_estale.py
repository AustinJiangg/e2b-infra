#!/usr/bin/env python3
"""restore 之后"新进程按绝对路径访问目录"会不会拿到 ESTALE（目录跨 restore 的语义：陈旧目录返回 ESTALE 与目录的复活）。

压测里 worker 的 cwd 固定在 /srv/st/d1/.../d8，restore 之后它的 cwd 就是陈旧目录；
在目录跨 restore 的语义修好之前的内核上，worker 按绝对路径 listdir/scandir 的 ESTALE 比例很高，这里拆开看：

    python3 probe_estale.py [模板名，默认 base-gsd]

场景（每个场景都是 create c2 → restore c1 之后，用全新的进程按绝对路径做一组操作）：
  A  没有任何进程停在这些目录里（对照）
  B  一个 root 进程（systemd-run，gsd 不碰）cwd 停在 /srv/p/a/b（checkpoint 前就在的目录）
  B2 B 的进程退出之后，不再 restore，直接再做一遍
  B3 B2 之后再 restore 一次
  C  root 进程 cwd 停在 /srv/p/a（祖先目录）
  D  root 进程持有 /srv/p/a/b 的目录 fd（不 chdir）
  E  root 进程 cwd 停在 c1 之后才建的目录 /srv/q/x（restore 后该目录不存在）
  F  root 进程 cwd 停在镜像自带、从没 copy-up 过的目录 /usr/share/doc
  G  root 进程 cwd 停在镜像自带、c1 之前已 copy-up 的目录 /var/tmp
  H  同 G，但 c1 之后又在 /var/tmp 里写过（restore 时该目录有 upper）
操作：ls 目录、python scandir+stat、stat 目录、cat 文件、touch 新文件、mkdir、ls /srv/q。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env  # noqa: E402

os.environ.update(load_e2b_env())
from e2b import Sandbox, CommandExitException  # noqa: E402

TPL = sys.argv[1] if len(sys.argv) > 1 else "base-gsd"

OPS = [
    ("ls /srv/p", "ls /srv/p"),
    ("ls /srv/p/a", "ls /srv/p/a"),
    ("ls /srv/p/a/b", "ls /srv/p/a/b"),
    ("scandir /srv/p/a/b", "python3 -c 'import os; print(len([d.stat() for d in os.scandir(\"/srv/p/a/b\")]))'"),
    ("stat -c %i /srv/p/a/b", "stat -c %i /srv/p/a/b"),
    ("cat /srv/p/a/b/f", "cat /srv/p/a/b/f"),
    ("touch /srv/p/a/b/n", "touch /srv/p/a/b/n_$RANDOM"),
    ("mkdir /srv/p/a/b/m", "mkdir /srv/p/a/b/m_$RANDOM"),
    ("ls /srv/q", "ls /srv/q"),
    ("ls /srv", "ls /srv"),
    ("ls /etc", "ls /etc > /dev/null"),
    ("ls /usr/share/doc", "ls /usr/share/doc > /dev/null"),
    ("ls /var/tmp", "ls /var/tmp"),
    ("ls /var", "ls /var > /dev/null"),
    ("cat /var/lib/dpkg/status", "head -c 20 /var/lib/dpkg/status"),
]


def main():
    sb = Sandbox.create(template=TPL, timeout=900)
    res = {"template": TPL, "sandbox": sb.sandbox_id, "scenarios": {}}

    def sh(c, timeout=60):
        try:
            r = sb.commands.run(c, user="root", timeout=timeout)
            return 0, (r.stdout + r.stderr).strip()
        except CommandExitException as e:
            return e.exit_code, (e.stdout + e.stderr).strip()

    def probe():
        out = {}
        for name, c in OPS:
            rc, o = sh(c)
            out[name] = ("OK " if rc == 0 else f"rc={rc} ") + o.replace("\n", " ")[:160]
        return out

    try:
        res["kernel"] = sh("cat /proc/version")[1]
        sh("mkdir -p /srv/p/a/b && echo x > /srv/p/a/b/f && ls -R /srv/p > /dev/null && echo t > /var/tmp/copied_up")
        c1 = sb.checkpoint.create("c1").checkpoint_id

        def cycle(tag):
            sh("mkdir -p /srv/q/x; echo y > /srv/q/x/g")
            c2 = sb.checkpoint.create(f"c2-{tag}").checkpoint_id
            sb.checkpoint.restore(c1)
            return c2

        def holder(unit, how):
            sh(f"systemctl stop {unit} 2>/dev/null; systemctl reset-failed {unit} 2>/dev/null; true")
            if how.startswith("cwd:"):
                rc, o = sh(f"systemd-run --unit={unit} -p WorkingDirectory={how[4:]} sleep 3600")
            else:  # fd:
                rc, o = sh(f"systemd-run --unit={unit} python3 -c 'import os,time; fd=os.open(\"{how[3:]}\", os.O_RDONLY); time.sleep(3600)'")
            sh("sleep 0.5")
            return rc, o

        # A
        cycle("A")
        res["scenarios"]["A_no_holder"] = probe()
        # B
        res["scenarios"]["B_setup"] = holder("hb", "cwd:/srv/p/a/b")
        cycle("B")
        res["scenarios"]["B_cwd_in_dir"] = probe()
        res["scenarios"]["B_holder_cwd"] = sh("ls -l /proc/$(systemctl show -p MainPID --value hb)/cwd")[1]
        sh("systemctl stop hb")
        res["scenarios"]["B2_after_holder_exit"] = probe()
        cycle("B3")
        res["scenarios"]["B3_restore_again"] = probe()
        # C
        holder("hc", "cwd:/srv/p/a")
        cycle("C")
        res["scenarios"]["C_cwd_in_ancestor"] = probe()
        sh("systemctl stop hc")
        # D
        holder("hd", "fd:/srv/p/a/b")
        cycle("D")
        res["scenarios"]["D_dirfd_held"] = probe()
        sh("systemctl stop hd")
        # E
        sh("mkdir -p /srv/q/x")
        holder("he", "cwd:/srv/q/x")
        sb.checkpoint.restore(c1)
        res["scenarios"]["E_cwd_in_vanished_dir"] = probe()
        sh("systemctl stop he")
        # F / G
        for tag, d in (("F_cwd_in_image_dir", "/usr/share/doc"), ("G_cwd_in_copied_up_image_dir", "/var/tmp")):
            holder("hf", "cwd:" + d)
            cycle(tag)
            res["scenarios"][tag] = probe()
            sh("systemctl stop hf")
            res["scenarios"][tag + "_after_exit"] = probe()
        # H：restore 时 /var/tmp 有 upper（c1 之后写过），root 进程 cwd 停在里面
        holder("hh", "cwd:/var/tmp")
        sh("echo h > /var/tmp/after_c1")
        sb.checkpoint.restore(c1)
        res["scenarios"]["H_cwd_in_image_dir_with_upper"] = probe()
        sh("systemctl stop hh")
        res["scenarios"]["H_after_exit"] = probe()
        res["dmesg_tail"] = sh("dmesg | tail -20")[1]
    finally:
        sb.kill()
    print(json.dumps(res, ensure_ascii=False, indent=1))


main()
