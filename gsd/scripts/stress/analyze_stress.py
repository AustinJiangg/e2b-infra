#!/usr/bin/env python3
"""汇总一批压测输出目录并逐组判定。

    python analyze_stress.py <stress 目录> <前缀>  > summary.json

<stress 目录> 下名字以"<前缀>-"开头的每个子目录是一组压测的输出目录（run_stress_watch.sh 的 <outdir>），
例如 <stress 目录>/rel-all、rel-multitree 对应前缀 rel。只用标准库，不起沙箱，可以在任何机器上对拷回来的结果目录跑。
认得四种汇总文件：stress-summary.json、enospc-summary.json、multitree-summary.json、deldense-summary.json。

输出一个 JSON：每组一项，判据看每组的 "PASS"。PASS 为真要同时满足：
  RESULT 文件以 "DONE rc=0" 开头；dmesg 相对开机时没有新增的内核报错行（dmesg_new_hits 为空）；串口输出没有命中（console_hits 为空）；
  worker 的 errno 只有 ENOENT / ESTALE（non_ENOENT_ESTALE 为空；multitree / deldense 则是没有预期之外的错误且 verdict 为 PASS）；
  没有心跳停滞的 worker（heartbeat_stale 为空）；汇总里有 lockdep 统计时 debug_locks 结束时仍为 1。
"""
import glob, json, os, re, sys
BAD = re.compile(r"KASAN|BUG:|Oops|WARNING:|list_add corruption|list_del corruption|list corruption|possible circular locking|"
                 r"possible recursive locking|sleeping function called from invalid context|ODEBUG|hung task|hung_task|"
                 r"blocked for more than|soft lockup|rcu.*stall|detected stalls", re.I)
root, pre = sys.argv[1], sys.argv[2]
out = {}
for d in sorted(glob.glob(os.path.join(root, pre + "-*"))):
    name = os.path.basename(d)
    r = {"RESULT": open(os.path.join(d, "RESULT")).read().strip() if os.path.exists(os.path.join(d, "RESULT")) else None}
    boot = set()
    for f in glob.glob(os.path.join(d, "*-dmesg-boot.txt")):
        boot |= {l for l in open(f, errors="replace").read().splitlines() if BAD.search(l)}
    dm_hits = set()
    for f in glob.glob(os.path.join(d, "*-dmesg-*.txt")):
        if f.endswith("-boot.txt"):
            continue
        dm_hits |= {l for l in open(f, errors="replace").read().splitlines() if BAD.search(l) and l not in boot}
    r["dmesg_new_hits"] = sorted(dm_hits)[:20]
    con = os.path.join(d, "console.txt")
    if os.path.exists(con):
        lines = open(con, errors="replace").read().splitlines()
        # console 里带宿主机时间前缀；开机阶段（快照恢复前的模板构建不在此沙箱日志里）
        r["console_lines"] = len(lines)
        r["console_hits"] = [l[:200] for l in lines if BAD.search(l)][:20]
    s = os.path.join(d, "stress-summary.json")
    e = os.path.join(d, "enospc-summary.json")
    if os.path.exists(s):
        j = json.load(open(s))
        by_op = j["worker_errs_by_op"]
        r.update({k: j.get(k) for k in ("rounds", "workers", "gsd_ops", "ckpts_left", "lat_ms_p50", "worker_ops_total",
                                        "worker_errs_by_errno", "worker_rechdir", "workers_active_before_stop",
                                        "user_load_hb_age_s", "health", "lockdep_boot", "lockdep_end")})
        r["worker_errs_by_op"] = by_op
        r["slab_delta"] = {k: j["slab_delta"][k] for k in ("mnt_cache", "dentry", "kmalloc-128") if k in j["slab_delta"]}
        r["heartbeat_stale"] = {k: v for k, v in j["worker_heartbeat_age_s"].items() if v > 10}
        r["non_ENOENT_ESTALE"] = {k: v for k, v in j["worker_errs_by_errno"].items() if k not in ("ENOENT", "ESTALE")}
    elif os.path.exists(e):
        j = json.load(open(e))
        r["enospc_gaps"] = [{k: g[k] for k in ("gap_blocks", "create", "restore_c0")} for g in j["gaps"]]
        r["enospc_recovery"] = j["recovery"]
        r["lockdep_boot"], r["lockdep_end"] = j.get("lockdep_boot"), j.get("lockdep_end")
    elif os.path.exists(os.path.join(d, "multitree-summary.json")):
        j = json.load(open(os.path.join(d, "multitree-summary.json")))
        r.update({k: j.get(k) for k in ("verdict", "rounds", "stats", "unexpected_errors", "verify_fail_samples", "bad_files", "final_cnt",
                                        "health", "state", "perf_p50", "lockdep_boot", "lockdep_end", "checkpoints_left", "elapsed_s", "dmesg_hits")})
        r["slab_delta"] = {k: j["slab_delta"][k] for k in ("mnt_cache", "dentry", "kmalloc-128") if k in j["slab_delta"]}
        r["non_ENOENT_ESTALE"] = j.get("unexpected_errors") or {}
        if j.get("verdict") != "PASS":
            r["heartbeat_stale"] = {"verdict": j.get("verdict")}
    elif os.path.exists(os.path.join(d, "deldense-summary.json")):
        j = json.load(open(os.path.join(d, "deldense-summary.json")))
        r.update({k: j.get(k) for k in ("verdict", "rounds", "workers", "stats", "unexpected_errors", "verify_fail_samples", "worker_ops_total", "worker_errs",
                                        "worker_unexpected_errno", "worker_heartbeat_age_s", "user", "user_ok", "marks_end", "dmesg_hits", "dmesg_notes",
                                        "health", "state", "lockdep_boot", "lockdep_end", "checkpoints_left", "elapsed_s")})
        r["slab_delta"] = {k: j["slab_delta"][k] for k in ("mnt_cache", "dentry") if k in j["slab_delta"]}
        r["non_ENOENT_ESTALE"] = dict(j.get("unexpected_errors") or {}, **(j.get("worker_unexpected_errno") or {}))
        if j.get("verdict") != "PASS":
            r["heartbeat_stale"] = {"verdict": j.get("verdict")}
    else:
        log = os.path.join(d, "stress.log")
        r["log_tail"] = open(log).read()[-600:] if os.path.exists(log) else None
    ok = (r["RESULT"] or "").startswith("DONE rc=0") and not r["dmesg_new_hits"] and not r.get("console_hits") \
        and not r.get("non_ENOENT_ESTALE") and not r.get("heartbeat_stale")
    if "lockdep_end" in r and r["lockdep_end"] and "debug_locks" in (r["lockdep_end"] or ""):
        r["debug_locks_end"] = int(re.search(r"debug_locks:\s+(\d+)", r["lockdep_end"]).group(1))
        ok = ok and r["debug_locks_end"] == 1
    r["PASS"] = ok
    out[name] = r
print(json.dumps(out, ensure_ascii=False, indent=1))
