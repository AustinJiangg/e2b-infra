#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2b 原生 snapshot / resume 的分档耗时（每档独立沙箱，只放本档大小的负载）。

口径与 gsd 的 demo_checkpoint_perf.py（per-tier-size）对齐，用于和下面两份数据并列：
    920B 进程级：gsd/docs/20-results-and-gaps.md §3.4
    950 沙箱级 ：rollback/docs/18-950-performance.md §4

为什么不用 native_snapshot_bench.py：它在同一个沙箱里预热到最大档再逐档覆写，
而原生快照的内存差分相对模板、包含自启动以来写过的全部页，预热量会进入每一档的
第一次导出。本脚本每档新建沙箱，只分配本档负载。

一个样本（默认 --mode both，两种模式各跑一遍）：

    pause 模式      改写本档负载 → sb.pause() → Sandbox.connect(id) → 校验
    snapshot 模式   改写本档负载 → sb.create_snapshot() → Sandbox.create(template=id)
                    → 在新沙箱里校验 → 杀掉新沙箱、删除快照

每个沙箱的第 0 个循环是 first：导出的是自启动以来全部写入，单列，相当于全量基点。
之后的循环是 repeat：在上一次快照之上只改写本档负载一次，对应增量快照，是对比用的
那一列。原生路径没有服务端分段计时，耗时全部是客户端墙钟。

耗时列：
    快照      sb.pause() 或 sb.create_snapshot() 的调用耗时
    恢复      Sandbox.connect() 或 Sandbox.create() 返回，加上第一条命令执行完成的耗时。
              create 返回时 guest 还可能没就绪，只看返回时间会漏掉这一段，
              所以 resume_call_ms、ready_ms 也分别保留在结果里
    首读内存  恢复后第一次读完内存负载；与快照前同一读法（read_mem_before_ms）的差值
              是恢复时推迟到缺页阶段的成本

校验：内存负载放 /dev/shm（只有内存快照能带回来），文件负载放根文件系统并用
O_DIRECT 读回（绕开 guest 页缓存，否则读不出磁盘差分是否丢数据），再加每代标记。
pause 模式另核对常驻进程的 pid 和启动时间，确认进程状态随内存一起回来。

在部署 e2b 的宿主机上运行（只依赖 SDK 自带的 pause / connect / create_snapshot）：

    python3 native_snapshot_tiers.py --env-file /path/to/e2b-infra/benchmark/.env --out result.json

    --smoke               只跑 0、16 两档，每档 1 个沙箱 1 个 repeat，约 1 分钟
    --tiers 0,16,256,512  指定总改动量 MiB，内存与文件按 3:1 拆分
    --passes 3 --iters 5  每档几个新沙箱，每个沙箱几个 repeat（默认值）
    --mode pause          只跑 pause/resume
    --template base       模板名（默认 base）

结果写 JSON，stdout 打印汇总表；任一样本失败时退出码非零，已有结果仍写入文件。
"""
import argparse
import glob
import hashlib
import json
import logging
import os
import platform
import socket
import statistics
import sys
import tempfile
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("e2b").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger("tiers")

SCHEMA_VERSION = 1
SBX_TIMEOUT = 3600
MEM_FILE = "/dev/shm/tier"
DISK_DIR = "/bench-root"
DISK_FILE = DISK_DIR + "/tier"
MEM_GEN = "/dev/shm/gen"
DISK_GEN = DISK_DIR + "/gen"
DEFAULT_TIERS = "0,16,32,64,128,256"
MIB = 1 << 20


def load_env_file(path):
    """KEY=VALUE 每行一个；已导出的变量优先。"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def split_tier(total):
    """总改动量按 3:1 拆成内存和文件，与另外两套的档位表一致。"""
    file_mb = total // 4
    return total - file_mb, file_mb


def sh(sb, cmd, timeout=180):
    r = sb.commands.run(cmd, user="root", timeout=timeout)
    return r.stdout.strip()


def timed(fn, *args, **kwargs):
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, (time.perf_counter() - t0) * 1000


# ------------------------------------------------------------ 宿主机侧（读不到就留空）

def fc_info(sandbox_id):
    """向沙箱的 Firecracker API 套接字发 GET /，取脏页后端与版本。"""
    hits = glob.glob(os.path.join(tempfile.gettempdir(), "fc-%s*.sock" % sandbox_id.split("-")[0]))
    if not hits:
        return {}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(hits[0])
        s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\n\r\n")
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                return {}
            buf += chunk
        head, body = buf.split(b"\r\n\r\n", 1)
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(body) < length:
            chunk = s.recv(65536)
            if not chunk:
                break
            body += chunk
        return json.loads(body)
    except (OSError, ValueError):
        return {}
    finally:
        s.close()


def orchestrator_proc():
    """返回 (exe 路径, environ 字典)。读进程而不读配置，记录的是实际运行状态。"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            exe = os.path.realpath("/proc/%s/exe" % pid)
            cmdline = open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode()
            raw = open("/proc/%s/environ" % pid, "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        name = os.path.basename(exe)
        if not (name.startswith("orchestrator") or name.startswith("template-manager")):
            continue
        if "--port" not in cmdline and "ORCHESTRATOR_SERVICES=" not in raw:
            continue
        return exe, dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
    return None, {}


def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def alloc(path):
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return None


def list_cache(cache):
    try:
        return set(os.listdir(cache))
    except (OSError, TypeError):
        return set()


def export_sizes(cache, before):
    """快照后导出缓存里新出现的 <buildID>-memfile-* / -rootfs.ext4-*，返回实占 MiB。
    这是导出量，用来确认精确增量生效；读不到时返回 None，不影响耗时。"""
    groups = {}
    for name in list_cache(cache) - before:
        for kind in ("memfile", "rootfs.ext4"):
            token = "-%s-" % kind
            if token in name:
                groups.setdefault(name.split(token)[0], {})[kind] = os.path.join(cache, name)
    groups = {b: g for b, g in groups.items() if "memfile" in g}
    if not groups:
        return None, None
    g = max(groups.values(), key=lambda v: max(os.stat(p).st_mtime for p in v.values()))
    mem, disk = alloc(g["memfile"]), alloc(g.get("rootfs.ext4", ""))
    return (None if mem is None else mem / MIB), (None if disk is None else disk / MIB)


def provenance(args):
    exe, env = orchestrator_proc()
    return {
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "script_sha256": sha256_file(os.path.abspath(__file__)),
        "orchestrator_exe": exe,
        "orchestrator_sha256": sha256_file(exe) if exe else None,
        "FC_TRACK_DIRTY_PAGES": env.get("FC_TRACK_DIRTY_PAGES"),
        "FC_HDBSS": env.get("FC_HDBSS"),
        "FC_HDBSS_ORDER": env.get("FC_HDBSS_ORDER"),
        "STORAGE_PROVIDER": env.get("STORAGE_PROVIDER"),
        "args": vars(args),
    }


# ------------------------------------------------------------ guest 内负载

def prepare(sb):
    sh(sb, "mkdir -p %s" % DISK_DIR)
    pid = sh(sb, "setsid nohup sleep 86400 >/dev/null 2>&1 < /dev/null & echo $!")
    return pid, proc_start(sb, pid)


def proc_start(sb, pid):
    return sh(sb, "awk '{print $22}' /proc/%s/stat 2>/dev/null || true" % pid)


def rewrite(sb, mem_mb, file_mb, gen):
    """覆写本档全部负载。conv=notrunc 让第二次起落在同一批页和块上。"""
    if mem_mb:
        sh(sb, "dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc 2>/dev/null" % (MEM_FILE, mem_mb))
    if file_mb:
        sh(sb, "dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc,fsync 2>/dev/null"
           % (DISK_FILE, file_mb))
    sh(sb, "echo %s > %s; echo %s > %s; sync" % (gen, MEM_GEN, gen, DISK_GEN))


def read_state(sb, mem_mb, file_mb):
    """读全部负载，返回内容摘要与读耗时。恢复后第一次读包含内存懒加载的成本。"""
    state = {"mem_gen": sh(sb, "cat %s" % MEM_GEN), "disk_gen": sh(sb, "cat %s" % DISK_GEN)}
    state["mem_sha"], state["read_mem_ms"] = None, 0.0
    if mem_mb:
        out, ms = timed(sh, sb, "sha256sum %s | cut -d' ' -f1" % MEM_FILE)
        state["mem_sha"], state["read_mem_ms"] = out, ms
    state["disk_sha"], state["read_disk_ms"] = None, 0.0
    if file_mb:
        out, ms = timed(sh, sb, "dd if=%s bs=1M iflag=direct 2>/dev/null | sha256sum | cut -d' ' -f1"
                        % DISK_FILE)
        state["disk_sha"], state["read_disk_ms"] = out, ms
    return state


def same_content(a, b):
    return all(a[k] == b[k] for k in ("mem_gen", "disk_gen", "mem_sha", "disk_sha"))


# ------------------------------------------------------------ 一次循环

def pause_cycle(Sandbox, sb, ctx, kind, gen):
    """改写 → pause → connect → 校验。返回 (恢复后的沙箱对象, 样本)。"""
    mem_mb, file_mb = ctx["mem_mb"], ctx["file_mb"]
    rewrite(sb, mem_mb, file_mb, gen)
    want = read_state(sb, mem_mb, file_mb)
    before = list_cache(ctx["cache"])
    _, pause_ms = timed(sb.pause)
    mem_exp, disk_exp = export_sizes(ctx["cache"], before)
    if ctx["settle_s"]:
        time.sleep(ctx["settle_s"])
    sb, resume_ms = timed(Sandbox.connect, ctx["sid"], timeout=SBX_TIMEOUT)
    _, ready_ms = timed(sh, sb, "true")
    got = read_state(sb, mem_mb, file_mb)
    proc_ok = proc_start(sb, ctx["pid"]) == ctx["proc_start"]
    ok = same_content(want, got) and proc_ok
    return sb, {
        "kind": kind, "snapshot_ms": pause_ms, "resume_call_ms": resume_ms, "ready_ms": ready_ms,
        "resume_ms": resume_ms + ready_ms,
        "read_mem_before_ms": want["read_mem_ms"], "read_mem_after_ms": got["read_mem_ms"],
        "read_disk_before_ms": want["read_disk_ms"], "read_disk_after_ms": got["read_disk_ms"],
        "export_mem_mib": mem_exp, "export_disk_mib": disk_exp,
        "content_ok": same_content(want, got), "proc_ok": proc_ok, "ok": ok,
    }


def snapshot_cycle(Sandbox, sb, ctx, kind, gen):
    """改写 → create_snapshot → 另起新沙箱 → 校验 → 清理。原沙箱继续使用。"""
    mem_mb, file_mb = ctx["mem_mb"], ctx["file_mb"]
    rewrite(sb, mem_mb, file_mb, gen)
    want = read_state(sb, mem_mb, file_mb)
    before = list_cache(ctx["cache"])
    snap, snapshot_ms = timed(sb.create_snapshot)
    mem_exp, disk_exp = export_sizes(ctx["cache"], before)
    nsb, resume_ms = None, None
    try:
        nsb, resume_ms = timed(Sandbox.create, template=snap.snapshot_id, timeout=600)
        _, ready_ms = timed(sh, nsb, "true")
        got = read_state(nsb, mem_mb, file_mb)
    finally:
        if nsb is not None:
            nsb.kill()
        Sandbox.delete_snapshot(snap.snapshot_id)
    ok = same_content(want, got)
    return sb, {
        "kind": kind, "snapshot_ms": snapshot_ms, "resume_call_ms": resume_ms, "ready_ms": ready_ms,
        "resume_ms": resume_ms + ready_ms,
        "read_mem_before_ms": want["read_mem_ms"], "read_mem_after_ms": got["read_mem_ms"],
        "read_disk_before_ms": want["read_disk_ms"], "read_disk_after_ms": got["read_disk_ms"],
        "export_mem_mib": mem_exp, "export_disk_mib": disk_exp,
        "content_ok": ok, "proc_ok": None, "ok": ok,
    }


def run_sandbox(Sandbox, args, mode, total, pass_no, cache, result):
    """一个新沙箱：first 循环 + iters 个 repeat 循环。"""
    mem_mb, file_mb = split_tier(total)
    cycle = pause_cycle if mode == "pause" else snapshot_cycle
    sb = Sandbox.create(template=args.template, timeout=SBX_TIMEOUT)
    sid = sb.sandbox_id
    try:
        info = fc_info(sid)
        if info:
            result["provenance"].setdefault("dirty_tracking", info.get("dirty_tracking"))
            result["provenance"].setdefault("firecracker_version", info.get("vmm_version"))
        pid, start = prepare(sb)
        ctx = {"sid": sid, "pid": pid, "proc_start": start, "mem_mb": mem_mb, "file_mb": file_mb,
               "cache": cache, "settle_s": args.settle_ms / 1000}
        for i in range(args.iters + 1):
            kind = "first" if i == 0 else "repeat"
            sb, row = cycle(Sandbox, sb, ctx, kind, "g%d-%d-%d" % (total, pass_no, i))
            row.update(mode=mode, total_mb=total, mem_mb=mem_mb, file_mb=file_mb,
                       sandbox_pass=pass_no, cycle=i, sandbox_id=sid)
            result["samples"].append(row)
            LOG.info("%-8s %3d MiB p%d #%d %-6s snapshot=%8.1f resume=%8.1f (ready %6.1f) "
                     "read=%7.1f export=%s %s",
                     mode, total, pass_no, i, kind, row["snapshot_ms"], row["resume_ms"],
                     row["ready_ms"], row["read_mem_after_ms"] + row["read_disk_after_ms"],
                     "-" if row["export_mem_mib"] is None else "%.1f" % row["export_mem_mib"],
                     "OK" if row["ok"] else "FAIL")
            if not row["ok"]:
                raise RuntimeError("恢复后内容或进程不一致: %s" % json.dumps(row, ensure_ascii=False))
    finally:
        try:
            sb.kill()
        except Exception as exc:  # 沙箱已不在时不掩盖原始错误
            LOG.warning("kill %s: %s", sid, exc)


# ------------------------------------------------------------ 汇总

def stats(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {"n": len(values), "p50": statistics.median(values), "min": values[0], "max": values[-1]}


def summarize(samples):
    out = []
    keys = sorted({(s["mode"], s["kind"], s["total_mb"]) for s in samples},
                  key=lambda k: (k[0], k[1] != "repeat", k[2]))
    for mode, kind, total in keys:
        rows = [s for s in samples if (s["mode"], s["kind"], s["total_mb"]) == (mode, kind, total)]
        entry = {"mode": mode, "kind": kind, "total_mb": total,
                 "mem_mb": rows[0]["mem_mb"], "file_mb": rows[0]["file_mb"],
                 "failed": sum(1 for s in rows if not s["ok"])}
        for key in ("snapshot_ms", "resume_ms", "resume_call_ms", "ready_ms", "read_mem_after_ms",
                    "read_mem_before_ms", "export_mem_mib", "export_disk_mib"):
            entry[key] = stats([s.get(key) for s in rows])
        out.append(entry)
    return out


def fmt(stat, key="p50"):
    return "-" if stat is None else "%.1f" % stat[key]


def print_summary(result):
    prov = result["provenance"]
    print("\n宿主 %s  内核 %s  orchestrator %s  脏页后端 %s  FC %s" % (
        prov["hostname"], prov["kernel"], (prov["orchestrator_sha256"] or "?")[:16],
        prov.get("dirty_tracking", "?"), prov.get("firecracker_version", "?")))
    labels = {("pause", "repeat"): "pause 模式 repeat：改写本档后 pause → connect（对比用）",
              ("pause", "first"): "pause 模式 first：首次快照，含自启动以来全部写入（单列）",
              ("snapshot", "repeat"): "snapshot 模式 repeat：create_snapshot → 从快照新建沙箱（对比用）",
              ("snapshot", "first"): "snapshot 模式 first：首次快照（单列）"}
    head = "%6s %10s %5s | %18s | %18s | %9s %13s %9s" % (
        "总MiB", "内存/文件", "n", "快照 p50 [min,max]", "恢复 p50 [min,max]",
        "就绪 p50", "内存读 前/后", "导出MiB")
    for key, label in labels.items():
        rows = [e for e in result["summary"] if (e["mode"], e["kind"]) == key]
        if not rows:
            continue
        print("\n" + label + "（客户端墙钟，单位 ms）")
        print(head)
        for e in rows:
            snap, res = e["snapshot_ms"], e["resume_ms"]
            print("%6d %10s %5d | %7.1f [%.0f,%.0f] | %7.1f [%.0f,%.0f] | %9s %13s %9s" % (
                e["total_mb"], "%d/%d" % (e["mem_mb"], e["file_mb"]), snap["n"],
                snap["p50"], snap["min"], snap["max"], res["p50"], res["min"], res["max"],
                fmt(e["ready_ms"]), "%s/%s" % (fmt(e["read_mem_before_ms"]), fmt(e["read_mem_after_ms"])),
                fmt(e["export_mem_mib"])))
    print("\n判定: %s（样本 %d，失败 %d）" % (
        result["verdict"], len(result["samples"]), sum(1 for s in result["samples"] if not s["ok"])))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=["pause", "snapshot", "both"], default="both")
    ap.add_argument("--tiers", default=DEFAULT_TIERS, help="总改动量 MiB，逗号分隔")
    ap.add_argument("--passes", type=int, default=3, help="每档新建几个沙箱")
    ap.add_argument("--iters", type=int, default=5, help="每个沙箱几个 repeat 循环")
    ap.add_argument("--template", default="base")
    ap.add_argument("--settle-ms", type=int, default=0, help="pause 返回后等多久再 resume")
    ap.add_argument("--env-file", help="KEY=VALUE 凭据文件；已导出的变量优先")
    ap.add_argument("--out", default="native-tiers-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--smoke", action="store_true", help="只跑 0、16 两档，1 个沙箱 1 个 repeat")
    args = ap.parse_args()
    if args.smoke:
        args.tiers, args.passes, args.iters = "0,16", 1, 1
    if args.env_file:
        load_env_file(args.env_file)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    from e2b import Sandbox

    modes = ["pause", "snapshot"] if args.mode == "both" else [args.mode]
    tiers = [int(t) for t in args.tiers.split(",")]
    _, env = orchestrator_proc()
    cache = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build") if env else None
    result = {"schema_version": SCHEMA_VERSION, "measurement_mode": "native-per-tier-size",
              "template": args.template, "provenance": provenance(args),
              "samples": [], "summary": [], "verdict": "FAIL"}
    failure = None
    try:
        for mode in modes:
            for pass_no in range(args.passes):
                for total in tiers:
                    run_sandbox(Sandbox, args, mode, total, pass_no, cache, result)
        result["verdict"] = "PASS"
    except Exception as exc:
        failure = str(exc)
        result["error"] = failure
        LOG.exception("分档测试失败")
    finally:
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        result["summary"] = summarize(result["samples"])
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print_summary(result)
        print("结果文件: %s" % os.path.abspath(args.out))
    return 0 if failure is None else 1


if __name__ == "__main__":
    sys.exit(main())
