#!/usr/bin/env python3
""""checkpoint 时把以写方式打开着的 upper 文件留在 upper、沉下去的是一份拷贝"这一做法的代价与覆盖面。

    python3 probe_move_cost.py [模板名，默认 base-gsd]

1) 沙箱里本来就以写方式打开着的 overlay 文件（journald 等）：checkpoint 前后在沉下去的层和新 upper 里各是哪个 inode；
2) 一个 root 进程以 O_RDWR 打开 N MiB 的文件（N = 0/1/16/64/128，0 表示不开文件），各做一次 checkpoint，
   记 overlay_sink_ms（gsd timings），并检查：沉下去的层里是一份拷贝（inode 不同、内容相同），新 upper 里是原 inode。
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
fd = os.open(sys.argv[1], os.O_RDWR)
open(sys.argv[1] + ".ack", "w").write("ready")
time.sleep(3600)
'''


def main():
    sb = Sandbox.create(template=TPL, timeout=900)
    res = {"template": TPL, "sandbox": sb.sandbox_id, "runs": []}

    def sh(c):
        try:
            r = sb.commands.run(c, user="root", timeout=120)
            return r.stdout.strip()
        except CommandExitException as e:
            return f"rc={e.exit_code} {e.stdout.strip()} {e.stderr.strip()}"

    def state():
        st = json.loads(sh("cat /var/lib/gsd/snapshots/state.json 2>/dev/null || echo {}") or "{}")
        return st

    try:
        res["kernel"] = sh("cat /proc/version")
        res["open_for_write_before"] = sh("ls -l /proc/[0-9]*/fd 2>/dev/null | grep -E ' -> /(var|srv|home|etc|root)/' | awk '{print $NF}' | sort | uniq -c | head -20")
        res["writers_detail"] = sh("for p in /proc/[0-9]*; do for f in $p/fd/*; do l=$(readlink $f); case \"$l\" in /var/*|/home/*|/srv/*|/root/*|/etc/*) fl=$(awk '/flags/{print $2}' $p/fdinfo/${f##*/}); echo \"$(cat $p/comm) $l $fl\";; esac; done; done 2>/dev/null | sort -u | head -20")
        sb.files.write("/root/hold.py", HOLD, user="root")
        for mb in (0, 1, 16, 64, 128):
            run = {"mb": mb}
            f = f"/srv/mv/f{mb}"
            if mb:
                sh(f"mkdir -p /srv/mv && head -c {mb}M /dev/urandom > {f} && sync")
                sh(f"systemd-run --unit=hold{mb} python3 /root/hold.py {f}")
                for _ in range(50):
                    if sh(f"cat {f}.ack 2>/dev/null") == "ready":
                        break
                    time.sleep(0.2)
                run["ino_before"] = sh(f"stat -c %i {f}")
                run["md5_before"] = sh(f"md5sum < {f} | cut -c1-12")
            cp = sb.checkpoint.create(f"m{mb}")
            run["timings"] = getattr(cp, "timings", None) or {}
            st = state()
            top = [c for c in st.get("checkpoints", []) if c.get("id") == cp.checkpoint_id]
            sunk = top[0]["layers"][0] if top else ""
            run["sunk"] = sunk
            upper = sh("ls -d /overlay/layers/upper_*_next 2>/dev/null | tail -1")
            run["upper"] = upper
            if mb:
                run["in_sunk"] = sh(f"stat -c '%i %s' {sunk}{f} 2>&1; md5sum < {sunk}{f} | cut -c1-12")
                run["in_upper"] = sh(f"stat -c '%i %s' {upper}{f} 2>&1")
                run["held_ino"] = sh(f"stat -L -c %i /proc/$(systemctl show -p MainPID --value hold{mb})/fd/3 2>&1")
                sh(f"systemctl stop hold{mb}; rm -f {f}")
            run["journal_sunk"] = sh(f"ls -li {sunk}/var/log/journal/*/ 2>/dev/null | tail -3")
            run["journal_upper"] = sh(f"ls -li {upper}/var/log/journal/*/ 2>/dev/null | tail -3")
            res["runs"].append(run)
        res["df"] = sh("df -m /overlay | tail -1")
        res["dmesg"] = sh("dmesg | grep -i -E 'overlay|BUG|WARN' | grep -v overlay-init | tail -5")
    finally:
        sb.kill()
    print(json.dumps(res, ensure_ascii=False, indent=1))


main()
