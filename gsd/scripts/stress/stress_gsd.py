#!/usr/bin/env python3
"""最小冒烟：让一个沙箱连续 restore 很多次，看根盘与内存有没有缓慢变化。

最早的一个连续 restore 脚本，负载与 acceptance/verify_gsd.py 相同（192 MiB 常驻进程，两个 checkpoint，
对第二个连续 restore）；区别只是沙箱寿命更长（3600 秒）、每次 restore 多打印 guest 的 MemAvailable 与进程数、不打印分段。

在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；
凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python stress_gsd.py [模板名，默认 base-gsd] [restore 次数，默认 8]

判定：只有末行的根盘判定——结束时根盘已用量比起点多出不超过 2 MB 打印"判定: 根盘恒定 ✓"，
否则"根盘仍在增长 ✗（内核未换或模板未重建）"。MemAvail 与 procs 两栏不判，要人看有没有单调变化。

在 guest 内直连 127.0.0.1:49984，secure 沙箱（SDK 默认）会自动带上 X-Access-Token。
"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox

TPL = sys.argv[1] if len(sys.argv) > 1 else "base-gsd"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 8
MEMHOG = r'''
import time, hashlib, os
size=192<<20; buf=bytearray(size)
for i in range(0,size,4096): buf[i]=1
chunk=bytearray(1<<20)
open("/home/user/memhog.ack","w").write("ready"); last=""
while True:
    try: c=open("/home/user/memhog.cmd").read().strip()
    except OSError: c=""
    if c and c!=last:
        last=c; n=int(c.split()[1]); blk=hashlib.md5(c.encode()).digest()*4096
        for j in range(16): chunk[j<<16:(j+1)<<16]=blk
        for i in range(n): buf[i<<20:(i+1)<<20]=chunk
        open("/home/user/memhog.ack","w").write(c)
    time.sleep(0.05)
'''
RPC = "curl -s -m 20 -H 'Content-Type: application/json' -d '%s' http://127.0.0.1:49984/checkpoint.Checkpoint/%s"

def run(cmd, **kw):
    return sb.commands.run(cmd, user="root", timeout=60, **kw).stdout

sb = Sandbox.create(template=TPL, timeout=3600)
# gsd 校验 X-Access-Token：secure 沙箱（SDK 默认）要带 envd 的 access token；非 secure 沙箱没有这个 token，不加头。
_tok = getattr(sb, "_envd_access_token", None)
if _tok:
    RPC = RPC.replace("curl -s", "curl -s -H 'X-Access-Token: %s'" % _tok.replace("%", "%%"), 1)
try:
    print(f"[{TPL}] sandbox {sb.sandbox_id}")
    print("gsd -commit      :", run("/usr/bin/gsd -commit").strip() or "(无 commit，旧 gsd)")
    print("kernel           :", run("uname -r").strip(), "|", run("dmesg | head -1").strip()[:60])
    print("rcu_expedited    :", run("cat /sys/kernel/rcu_expedited").strip(), "(新 gsd 启动时会设成 1)")
    print("THP              :", run("cat /sys/kernel/mm/transparent_hugepage/enabled").strip())
    sb.files.write("/home/user/memhog.py", MEMHOG)
    sb.commands.run("python3 /home/user/memhog.py", background=True)
    for _ in range(100):
        if run("cat /home/user/memhog.ack 2>/dev/null || true").strip() == "ready": break
        time.sleep(0.2)
    run("echo never > /sys/kernel/mm/transparent_hugepage/enabled")
    run(RPC % ('{"name":"cp0"}', "CreateCheckpoint"))
    run("echo 'g1 192' > /home/user/memhog.cmd; chmod 666 /home/user/memhog.cmd; for i in $(seq 200); do grep -q 'g1 192' /home/user/memhog.ack 2>/dev/null && break; sleep 0.05; done")
    cp1 = json.loads(run(RPC % ('{"name":"cp1"}', "CreateCheckpoint")))["checkpointId"]
    def used(): return int(run("df -m / | awk 'NR==2{print $3}'").strip())
    u0 = used(); print(f"根盘 used 起点     : {u0} MB")
    tot = []
    for i in range(1, N + 1):
        r = run(RPC % (json.dumps({"checkpoint_id": cp1}), "RestoreCheckpoint"))
        j = run("journalctl -u gsd --no-pager -o cat | grep -E 'restore completed' | tail -1")
        try: t = json.loads(j)["total_ms"]
        except Exception: t = float("nan")
        tot.append(t); u = used()
        avail = run("awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo").strip()
        nproc = run("ls /proc | grep -c '^[0-9]'").strip()
        print(f"  restore #{i:2d}: ok={'true' in r}  total_ms={t:6.1f}  / used={u} MB ({u-u0:+d})  MemAvail={avail} MB  procs={nproc}", flush=True)
        time.sleep(0.3)
    s = sorted(tot); print(f"total_ms p50={s[len(s)//2]:.1f} min={s[0]:.1f} max={s[-1]:.1f}")
    print("判定:", "根盘恒定 ✓" if used() - u0 <= 2 else "根盘仍在增长 ✗（内核未换或模板未重建）")
finally:
    sb.kill()
