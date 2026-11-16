#!/usr/bin/env python3
"""在同一个模板上热换 guest 里的 gsd 二进制，对比 restore 各分段耗时（定位性能回退用）。

每个"变体"= 一个 gsd 二进制 + 可选环境变量（写进 systemd drop-in）。每轮给每个变体各起一个
新沙箱：换二进制、重启 gsd、跑 verify_gsd 同款负载（192 MiB memhog，cp0 → cp1 → N 次 restore
cp1），记录响应里的 timings（旧 gsd 没有 timings，从 journal 取）。变体按轮交错，减小漂移。

在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python exp_gsd_swap.py --template base-gsd --rounds 2 --n 8 \
        --variant base=/path/gsd-a --variant old=/path/gsd-b \
        --variant 'exp-a=/path/gsd-exp:GSD_EXP_RECLAIM=after' --out x.json
变体格式：名字=二进制[:K=V[,K=V...]]
特殊键 GUEST_BG=<命令>（命令里不能有逗号和单引号）：不写进 gsd 的环境，而是在 guest 里 systemd-run 起一个 root 后台命令，
例如 'x@base-gsd=-:GUEST_BG=while :; do echo t > /dev/kmsg; sleep 0.01; done'（二进制写 "-" 表示不换 gsd）。
"""
import argparse
import base64
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env  # noqa: E402

os.environ.update(load_e2b_env())
from e2b import Sandbox  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--template", default="base-gsd")
ap.add_argument("--rounds", type=int, default=2)
ap.add_argument("--n", type=int, default=8)
ap.add_argument("--variant", action="append", required=True)
ap.add_argument("--out", default="")
ap.add_argument("--creates", type=int, default=0, help="restore 之后再做几次 create（测 create 分段）")
args = ap.parse_args()

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
RPC0 = "curl -s -m 20 -u root: -H 'Content-Type: application/json' -d '%s' http://127.0.0.1:49984/checkpoint.Checkpoint/%s"


def parse_variant(s):
    # 名字里可以带 @模板，例如 old@base-gsd=/path/gsd：这个变体用指定模板起沙箱。
    name, rest = s.split("=", 1)
    env = {}
    if ":" in rest:
        path, kv = rest.split(":", 1)
        for p in kv.split(","):
            k, v = p.split("=", 1)
            env[k] = v
    else:
        path = rest
    return name, path, env


VARIANTS = [parse_variant(v) for v in args.variant]


def swap(sb, run, path, env):
    data = open(path, "rb").read()
    sb.files.write("/tmp/gsd.new", data, user="root")
    dropin = "[Service]\n" + "".join(f"Environment={k}={v}\n" for k, v in env.items())
    run("mkdir -p /run/systemd/system/gsd.service.d && cat > /run/systemd/system/gsd.service.d/exp.conf <<'EOF'\n%sEOF\n"
        "systemctl daemon-reload && systemctl stop gsd && chmod 755 /tmp/gsd.new && "
        "cp /tmp/gsd.new /usr/bin/gsd.new && mv -f /usr/bin/gsd.new /usr/bin/gsd && rm -f /tmp/gsd.new && "
        "systemctl start gsd" % dropin)
    for _ in range(100):
        h = run("curl -s -m 2 http://127.0.0.1:49984/health || true")
        if "ok" in h.lower():
            break
        time.sleep(0.1)


def one(name, path, env):
    tpl = name.split("@", 1)[1] if "@" in name else args.template
    sb = Sandbox.create(template=tpl, timeout=600)
    rpc = RPC0
    tok = getattr(sb, "_envd_access_token", None)
    if tok:
        rpc = rpc.replace("curl -s", "curl -s -H 'X-Access-Token: %s'" % tok.replace("%", "%%"), 1)

    def run(cmd, timeout=60):
        return sb.commands.run(cmd, user="root", timeout=timeout).stdout

    res = {"variant": name, "template": tpl, "sandbox": sb.sandbox_id, "restores": [], "creates": []}
    try:
        env = dict(env)
        bg = env.pop("GUEST_BG", None)  # 特殊键：不进 gsd 的 drop-in，而是在 guest 里用 systemd-run 起一个 root 后台命令
        if path != "-":  # "-"：不换二进制、不重启 gsd（对照）
            swap(sb, run, path, env)
        if bg:
            run("systemd-run --unit=expbg sh -c '%s'" % bg)
            res["guest_bg"] = bg
        res["commit"] = run("/usr/bin/gsd -commit 2>/dev/null || true").strip()
        res["thp"] = run("cat /sys/kernel/mm/transparent_hugepage/enabled").strip()
        res["proc_version"] = run("cat /proc/version").strip()
        res["env"] = run("systemctl show gsd -p Environment").strip()
        sb.files.write("/home/user/memhog.py", MEMHOG)
        sb.commands.run("python3 /home/user/memhog.py", background=True)
        for _ in range(100):
            if run("cat /home/user/memhog.ack 2>/dev/null || true").strip() == "ready":
                break
            time.sleep(0.2)
        run("echo never > /sys/kernel/mm/transparent_hugepage/enabled")
        run(rpc % ('{"name":"cp0"}', "CreateCheckpoint"))
        run("echo 'g1 192' > /home/user/memhog.cmd; chmod 666 /home/user/memhog.cmd; for i in $(seq 200); do grep -q 'g1 192' /home/user/memhog.ack 2>/dev/null && break; sleep 0.05; done")
        cp1 = json.loads(run(rpc % ('{"name":"cp1"}', "CreateCheckpoint")))["checkpointId"]
        for i in range(args.n):
            t0 = time.time()
            r = run(rpc % (json.dumps({"checkpoint_id": cp1}), "RestoreCheckpoint"))
            e2e = (time.time() - t0) * 1000
            seg = {}
            try:
                seg = json.loads(r).get("timings") or {}
            except Exception:
                pass
            if not seg:
                for line in run("journalctl -u gsd --no-pager -o cat | grep -E 'kill_ms|find_pids_ms|overlay_restore_ms|criu_restore_ms|restore completed' | tail -5").splitlines():
                    try:
                        j = json.loads(line)
                    except Exception:
                        continue
                    for k, v in j.items():
                        if k.endswith("_ms"):
                            seg[k] = v
            ok = "true" in r
            res["restores"].append({"ok": ok, "e2e_cmd_ms": e2e, "seg": seg})
            time.sleep(0.3)
        for i in range(args.creates):
            r = run(rpc % ('{"name":"cx%d"}' % i, "CreateCheckpoint"))
            seg = {}
            try:
                seg = json.loads(r).get("timings") or {}
            except Exception:
                pass
            res["creates"].append(seg)
            time.sleep(0.3)
        # 调试：gsd 日志里的细分（实验版会打 exp 行）
        res["journal_tail"] = run("journalctl -u gsd --no-pager -o cat | grep -E 'exp|kill_detail|scan' | tail -40 || true")
    finally:
        sb.kill()
    return res


def norm(seg):
    out = {}
    for k, v in seg.items():
        if not isinstance(v, (int, float)):
            continue
        k2 = k.replace("_ms", "").replace("Ms", "")
        k2 = {"criu_restore": "criuRestore", "find_pids": "findPids", "overlay_restore": "overlayRestore", "total": "total", "kill": "kill"}.get(k2, k2)
        out[k2] = v
    return out


def main():
    allres = []
    for rnd in range(args.rounds):
        for name, path, env in VARIANTS:
            print(f"== round {rnd + 1} variant {name} ({os.path.basename(path)} {env})", flush=True)
            try:
                r = one(name, path, env)
            except Exception as e:
                print("  ERROR", e, flush=True)
                continue
            r["round"] = rnd + 1
            allres.append(r)
            segs = [norm(x["seg"]) for x in r["restores"][1:]]  # 第一次 restore 杀的是原始进程，不计
            keys = sorted({k for s in segs for k in s})
            txt = " ".join(f"{k}={statistics.median([s[k] for s in segs if k in s]):.1f}" for k in keys)
            print(f"  commit={r.get('commit')} ok={all(x['ok'] for x in r['restores'])}  p50(#2..): {txt}", flush=True)
    print("\n==== 汇总（每变体全部轮次，去掉每个沙箱第 1 次 restore）====")
    summary = {}
    for name, _, _ in VARIANTS:
        segs = [norm(x["seg"]) for r in allres if r["variant"] == name for x in r["restores"][1:]]
        cs = [norm(x) for r in allres if r["variant"] == name for x in r.get("creates", [])]
        keys = sorted({k for s in segs for k in s})
        summary[name] = {k: statistics.median([s[k] for s in segs if k in s]) for k in keys}
        summary[name]["n"] = len(segs)
        if cs:
            ck = sorted({k for s in cs for k in s})
            summary[name]["create"] = {k: statistics.median([s[k] for s in cs if k in s]) for k in ck}
        print(name, json.dumps(summary[name], ensure_ascii=False))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"args": vars(args), "summary": summary, "runs": allres}, f, ensure_ascii=False, indent=1)


main()
