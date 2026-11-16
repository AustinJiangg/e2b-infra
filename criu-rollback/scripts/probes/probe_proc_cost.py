#!/usr/bin/env python3
"""guest 里 /proc 扫描的开销：进程数、内核线程数、读 stat / status 的耗时（find_pids 优化用）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox

TPL = sys.argv[1] if len(sys.argv) > 1 else "base-gsd"
PROBE = r'''
import os, time
pids=[int(p) for p in os.listdir("/proc") if p.isdigit()]
kt=0
for p in pids:
    try:
        s=open(f"/proc/{p}/stat").read()
        f=s[s.rindex(")")+2:].split()
        if int(f[6]) & 0x00200000: kt+=1
    except OSError: pass
def t(fn, n=20):
    xs=[]
    for _ in range(n):
        t0=time.perf_counter(); fn(); xs.append((time.perf_counter()-t0)*1000)
    xs.sort(); return xs[len(xs)//2]
def rd(name, only=None):
    def f():
        for p in os.listdir("/proc"):
            if not p.isdigit(): continue
            if only is not None and int(p) not in only: continue
            try: open(f"/proc/{p}/{name}").read()
            except OSError: pass
    return f
user=set()
for p in pids:
    try:
        s=open(f"/proc/{p}/stat").read(); f=s[s.rindex(")")+2:].split()
        if not int(f[6]) & 0x00200000: user.add(p)
    except OSError: pass
print("procs", len(pids), "kthreads", kt)
print("stat all ms", round(t(rd("stat")),3))
print("status all ms", round(t(rd("status")),3))
print("status non-kthread ms", round(t(rd("status", user)),3))
print("statm all ms", round(t(rd("statm")),3))
print("listdir ms", round(t(lambda: os.listdir("/proc")),3))
'''
sb = Sandbox.create(template=TPL, timeout=300)
try:
    sb.files.write("/tmp/probe.py", PROBE)
    print(sb.commands.run("python3 /tmp/probe.py", user="root").stdout)
    print(sb.commands.run("ps -eo pid,ppid,uid,comm | awk '$2!=2 && $1!=2'", user="root").stdout)
    print(sb.commands.run("cat /proc/self/cgroup; systemctl show envd -p MainPID -p ControlGroup; ls /sys/fs/cgroup/; cat /sys/fs/cgroup/system.slice/envd.service/cgroup.procs 2>&1 | head", user="root").stdout)
finally:
    sb.kill()
