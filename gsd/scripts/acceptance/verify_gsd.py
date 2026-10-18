#!/usr/bin/env python3
"""部署自检加一条最短的性能线：这个模板里装的是不是预期的那套 gsd 与内核，连续 restore 会不会吃根盘。

在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；
凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python verify_gsd.py [模板名，默认 base-gsd] [restore 次数，默认 8]

做法：起一个沙箱，后台跑一个预分配 192 MiB 并逐页碰实的常驻进程；建第一个 checkpoint，让进程把 192 MiB
全部改写一遍，再建第二个 checkpoint；然后对第二个 checkpoint 连续 restore N 次，每次记根盘已用量和各分段耗时。

判定：
  脚本自己只判一条：结束时根盘已用量比起点多出不超过 2 MB，末行打印"判定: 根盘恒定 ✓"，
  否则打印"根盘仍在增长 ✗（内核未换或模板未重建）"。
  开头四行环境信息脚本不判，要人看：gsd -commit 应等于宿主上那份 gsd 二进制的 -commit 输出（即部署的提交号）；
  rcu_expedited 应为 1；THP 应为 [never]。
  total_ms 的 p50（取排序后下标 N//2 的值）只看量级，与其他脚本的数字口径不同，对照值见手册 20 章。
  本脚本不校验 restore 回来的内容，ok 一栏只是看响应里有没有 true。
gsd 在 guest 内用 curl 直连 127.0.0.1:49984，不经过宿主代理；secure 沙箱里带沙箱自己的 X-Access-Token。
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

sb = Sandbox.create(template=TPL, timeout=600)
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
    tot = []; segs = []
    for i in range(1, N + 1):
        r = run(RPC % (json.dumps({"checkpoint_id": cp1}), "RestoreCheckpoint"))
        j = run("journalctl -u gsd --no-pager -o cat | grep -E 'restore completed' | tail -1")
        try: t = json.loads(j)["total_ms"]
        except Exception: t = float("nan")
        # 分段：新 gsd 的响应里带 timings；旧 gsd 从日志里取最近一次 restore 的各行
        seg = {}
        try: seg = json.loads(r).get("timings") or {}
        except Exception: pass
        if not seg:
            for line in run("journalctl -u gsd --no-pager -o cat | grep -E 'kill_ms|overlay_restore_ms|criu_restore_ms' | tail -3").splitlines():
                try: seg.update({k: v for k, v in json.loads(line).items() if k.endswith("_ms")})
                except Exception: pass
        segs.append(seg)
        tot.append(t); u = used()
        segtxt = " ".join(f"{k.replace('Ms','').replace('_ms','')}={v:.1f}" for k, v in sorted(seg.items()) if isinstance(v, (int, float)))
        print(f"  restore #{i:2d}: ok={'true' in r}  total_ms={t:6.1f}  / used={u} MB ({u-u0:+d})  [{segtxt}]")
        time.sleep(0.3)
    s = sorted(tot); print(f"total_ms p50={s[len(s)//2]:.1f} min={s[0]:.1f} max={s[-1]:.1f}")
    for k in sorted({k for d in segs for k in d}):
        xs = sorted(d[k] for d in segs if isinstance(d.get(k), (int, float)))
        if xs: print(f"  {k:18s} p50={xs[len(xs)//2]:.1f} min={xs[0]:.1f} max={xs[-1]:.1f}")
    print("判定:", "根盘恒定 ✓" if used() - u0 <= 2 else "根盘仍在增长 ✗（内核未换或模板未重建）")
finally:
    sb.kill()
