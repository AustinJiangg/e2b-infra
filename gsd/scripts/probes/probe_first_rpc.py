#!/usr/bin/env python3
"""新沙箱上第一次 gsd RPC 的墙钟耗时（定位"首个 create 偶发约 5 秒"）。

每个模板、每种方式各起 N 个沙箱（交错）：
  sdk   ：沙箱建好后先跑一条 envd 命令，然后第一次 gsd RPC 走 SDK（宿主 → client-proxy → guest:49984）
  guest ：第一次 gsd RPC 在 guest 里 curl 127.0.0.1:49984（不经宿主代理），之后再走一次 SDK
记录每次的墙钟、gsd 自报 total_ms、gsd 日志里 "rpc completed" 的条数。
在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python probe_first_rpc.py base-gsd 20 > out.json
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox

TPLS = sys.argv[1].split(",")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 20
rows = []

def ms(t): return round((time.time() - t) * 1000, 1)

for i in range(N):
    for tpl in TPLS:
        for mode in ("sdk", "guest"):
            t = time.time(); sb = Sandbox.create(template=tpl, timeout=120); row = {"template": tpl, "mode": mode, "sandbox": sb.sandbox_id, "sandbox_create_ms": ms(t)}
            try:
                t = time.time(); sb.commands.run("true", user="root"); row["envd_first_ms"] = ms(t)
                tok = getattr(sb, "_envd_access_token", None) or ""
                if mode == "guest":
                    t = time.time()
                    o = sb.commands.run("s=$(date +%%s%%N); curl -s -m 30 -o /dev/null -w '%%{http_code} %%{time_total}' -H 'X-Access-Token: %s' -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:49984/checkpoint.Checkpoint/ListCheckpoints; e=$(date +%%s%%N); echo \" $(( (e-s)/1000000 ))\"" % tok, user="root").stdout.split()
                    row["guest_curl_wall_ms"] = ms(t); row["guest_curl"] = o
                t = time.time(); cp = sb.checkpoint.create(name="first"); row["sdk_first_create_ms"] = ms(t); row["gsd_total_ms"] = (cp.timings or {}).get("total_ms")
                t = time.time(); sb.checkpoint.create(name="second"); row["sdk_second_create_ms"] = ms(t)
                t = time.time(); sb.checkpoint.list(); row["sdk_list_ms"] = ms(t)
                slow = row["sdk_first_create_ms"] > 1500 or row.get("guest_curl_wall_ms", 0) > 1500
                if slow:
                    row["gsd_journal"] = sb.commands.run("journalctl -u gsd --no-pager -o short-precise | grep -v -E '\\([0-9]+\\.[0-9]+\\)' | tail -12 | cut -c1-300; date +%H:%M:%S.%N", user="root").stdout
            except Exception as e:
                row["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
            finally:
                sb.kill()
            rows.append(row)
            print("%2d %-10s %-5s first=%s second=%s guest=%s" % (i, tpl, mode, row.get("sdk_first_create_ms"), row.get("sdk_second_create_ms"), row.get("guest_curl")), file=sys.stderr, flush=True)
summ = {}
for tpl in TPLS:
    for mode in ("sdk", "guest"):
        r = [x for x in rows if x["template"] == tpl and x["mode"] == mode and "error" not in x]
        f = sorted(x["sdk_first_create_ms"] for x in r)
        summ["%s/%s" % (tpl, mode)] = {"n": len(r), "first_sdk_create_p50": f[len(f) // 2] if f else None, "first_sdk_create_max": f[-1] if f else None,
                                       "first_sdk_create_over_1500ms": sum(1 for v in f if v > 1500),
                                       "guest_curl_over_1500ms": sum(1 for x in r if x.get("guest_curl_wall_ms", 0) > 1500),
                                       "second_create_over_1500ms": sum(1 for x in r if x["sdk_second_create_ms"] > 1500),
                                       "errors": sum(1 for x in rows if x["template"] == tpl and x["mode"] == mode and "error" in x)}
json.dump({"summary": summ, "rows": rows}, sys.stdout, ensure_ascii=False, indent=1)
