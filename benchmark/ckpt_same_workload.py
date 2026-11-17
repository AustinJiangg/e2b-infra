#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一种负载下的 checkpoint / restore：沙箱级、进程级用同一个脚本、同一个负载，各跑一遍。

为什么要这份脚本：两套原来的分档测试负载不同（沙箱级改写 /dev/shm，进程级改写常驻进程的
bytearray，且进程大小随档位一起变），并列时分不开"改动量"和"进程大小"两个因素。这里两套都用
同一个常驻进程：

    常驻进程   预分配 P MiB 的 bytearray 并写满；收到命令时原地覆写前 d MiB
    文件       根盘上的 /tmp/swl/file，f MiB，dd 覆写后 fsync

三组测点（每个测点新建沙箱，--passes 个沙箱 × --reps 次，前 --warmup 次不计入统计）：

    dirty   进程固定 256 MiB，改动 d = 0、16、32、64、128、256 MiB，不写文件
    size    改动固定 16 MiB，进程 P = 32、64、128、256、384 MiB，不写文件
    file    进程 16 MiB、不改内存，只写文件 f = 16、64 MiB
    conc    N 个沙箱同时做（N = 1、4、8、16），每个沙箱进程 64 MiB、改动 16 MiB

一次测量：改写 → checkpoint A（计时）→ 再改写 → checkpoint B（计时）→ restore 回基点（计时）→
校验内存 SHA256 与文件 → 删掉 A、B。A 紧跟在上一次 restore 之后，是"restore 之后的 checkpoint"；
B 紧跟在 A 之后，是"连续的 checkpoint"。两者要分开看：进程级 restore 之后的第一次 checkpoint
是全量（CRIU 的 PID 复用检测，进程级手册第 16 章 L-18），沙箱级两者都是增量。基点是每个沙箱
改写之前的一次全量 checkpoint，单列不进统计。

记录：客户端墙钟；服务端分段（进程级取 SDK 返回的 gsd 分段，沙箱级读宿主上
<store>/<sandbox>/<checkpoint>/timings.json 与 last-restore-timings.json，需要 root）；产物字节
（进程级 image_bytes；沙箱级 checkpoint 目录里各文件的实占字节）。

在部署宿主上运行，用当前栈对应的 SDK 环境：
    python3 ckpt_same_workload.py --out result.json
    --series dirty,size,file,conc   选测点组（默认全部）
    --passes 2 --reps 11 --warmup 1 每个测点几个沙箱、每个沙箱几次、前几次不计
    --smoke                         每组只跑一个测点、1 个沙箱 3 次，约 2 分钟
    --stack sandbox|criu            默认按正在运行的 orchestrator 判断
    --template                      默认沙箱级 base、进程级 base-criu
结果写 JSON，stdout 打印汇总表；校验失败退出码非零。
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import logging
import os
import socket
import statistics
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("e2b").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger("same")

MIB = 1 << 20
WD = "/tmp/swl"
SBX_TIMEOUT = 3600
SERIES = {
    "dirty": [{"P": 256, "d": d, "f": 0} for d in (0, 16, 32, 64, 128, 256)],
    "size": [{"P": p, "d": 16, "f": 0} for p in (32, 64, 128, 256, 384)],
    "file": [{"P": 16, "d": 0, "f": f} for f in (16, 64)],
}
CONC = [1, 4, 8, 16]
CONC_POINT = {"P": 64, "d": 16, "f": 0}

# 常驻进程：命令经预先打开的文件传入，SIGUSR1 触发；handler 不分配大块内存、不 open()，
# 改写用启动时分配好的 1 MiB 块，避免新建 VMA（进程级的 soft-dirty 会把新 VMA 整个报脏）。
RESIDENT_PY = r'''
import hashlib, os, signal, sys
WD = sys.argv[2]
CMD, RSP, PID = WD + "/cmd", WD + "/rsp", WD + "/pid"
size = int(sys.argv[1]) << 20
buf = bytearray(size)
for i in range(0, size, 4096):
    buf[i] = 1
chunk = bytearray(min(max(size, 1), 1 << 20))
for f in (CMD, RSP):
    open(f, "w").close()
cmd_fd = os.open(CMD, os.O_RDWR)
rsp_fd = os.open(RSP, os.O_RDWR)

def handle(signum, frame):
    try:
        os.lseek(cmd_fd, 0, 0)
        f = (os.read(cmd_fd, 4096).split(b"\x00")[0].decode().split() + ["", "", ""])[:3]
        nonce, op, arg = f
        if op == "dirty":
            n = int(arg)
            if (n << 20) > size:
                raise ValueError("dirty exceeds payload")
            if n:
                blk = hashlib.md5(nonce.encode()).digest() * 4096
                for j in range(16):
                    chunk[j << 16:(j + 1) << 16] = blk
            for i in range(n):
                buf[i << 20:(i + 1) << 20] = chunk
            out = "ok"
        elif op == "sha":
            out = hashlib.sha256(buf).hexdigest()
        elif op == "ping":
            out = "pong"
        else:
            out = "BADOP"
    except Exception as e:
        out = "ERR:%s" % e
    resp = ("%s %s\n" % (nonce, out)).encode()
    os.lseek(rsp_fd, 0, 0)
    os.write(rsp_fd, resp)
    os.ftruncate(rsp_fd, len(resp))

signal.signal(signal.SIGUSR1, handle)
open(PID, "w").write(str(os.getpid()))
while True:
    signal.pause()
'''


# ------------------------------------------------------------ 宿主侧

def orchestrator_proc():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            exe = os.path.realpath("/proc/%s/exe" % pid)
            raw = open("/proc/%s/environ" % pid, "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        if not os.path.basename(exe).startswith(("orchestrator", "template-manager")):
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


def detect_stack(exe):
    return "criu" if exe and "/fc-criu/" in exe else "sandbox"


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def dir_bytes(path):
    """目录里各文件的实占字节（稀疏文件按实占算）。"""
    out = {}
    try:
        for name in os.listdir(path):
            p = os.path.join(path, name)
            if os.path.isfile(p):
                out[name] = os.stat(p).st_blocks * 512
    except OSError:
        return None
    return out


# ------------------------------------------------------------ 沙箱内

class Hog:
    def __init__(self, sb):
        self.sb = sb
        self.nonce = 0
        self.pid = None

    def sh(self, cmd, timeout=120):
        return self.sb.commands.run(cmd, user="root", timeout=timeout).stdout.strip()

    def ask(self, op, arg=""):
        self.nonce += 1
        n = "n%d" % self.nonce
        self.sh('echo -n "%s %s %s" > %s/cmd; kill -USR1 %s' % (n, op, arg, WD, self.pid), timeout=10)
        for _ in range(600):
            out = self.sh("cat %s/rsp 2>/dev/null; true" % WD, timeout=10)
            if out.startswith(n + " "):
                return out[len(n) + 1:]
            time.sleep(0.05)
        raise RuntimeError("常驻进程没有应答 %s %s" % (op, arg))

    def start(self, payload_mb):
        self.sh("rm -rf %s; mkdir -p %s; chmod 777 %s" % (WD, WD, WD))
        self.sb.files.write(WD + "/hog.py", RESIDENT_PY)
        # 默认用户启动：进程级只保存 uid 1000 的进程树
        self.sb.commands.run("python3 %s/hog.py %d %s" % (WD, payload_mb, WD), background=True)
        for _ in range(100):
            pid = self.sh("cat %s/pid 2>/dev/null; true" % WD, timeout=10)
            if pid:
                self.pid = pid
                break
            time.sleep(0.2)
        if not self.pid or self.ask("ping") != "pong":
            raise RuntimeError("常驻进程没有起来")

    def dirty(self, mb, tag):
        if mb and self.ask("dirty", "%d" % mb) != "ok":
            raise RuntimeError("改写失败")

    def write_file(self, mb):
        if mb:
            self.sh("dd if=/dev/urandom of=%s/file bs=1M count=%d conv=notrunc,fsync 2>/dev/null" % (WD, mb))

    def state(self, file_mb):
        mem = self.ask("sha")
        disk = self.sh("sha256sum %s/file | cut -d' ' -f1" % WD) if file_mb else None
        return mem, disk


# ------------------------------------------------------------ 测量

class Ctx:
    def __init__(self, args, stack, store):
        self.args, self.stack, self.store = args, stack, store

    def server(self, sb, ck_id, op, sdk_timings):
        """服务端分段：进程级用 SDK 带回的 gsd 分段；沙箱级读宿主上的 timings 文件。"""
        if self.stack == "criu":
            return dict(sdk_timings or {})
        if not self.store:
            return {}
        name = "last-restore-timings.json" if op == "restore" else os.path.join(ck_id, "timings.json")
        return load_json(os.path.join(self.store, sb.sandbox_id, name)) or {}

    def artifact(self, sb, cp):
        if self.stack == "criu":
            return {"image_bytes": getattr(cp, "image_bytes", None)}
        if not self.store:
            return {}
        files = dir_bytes(os.path.join(self.store, sb.sandbox_id, cp.checkpoint_id))
        return {"files": files, "bytes": sum(files.values()) if files else None}


def one_sandbox(Sandbox, ctx, point, series, pass_no, reps, warmup, rows, lock):
    P, d, f = point["P"], point["d"], point["f"]
    sb = Sandbox.create(template=ctx.args.template, timeout=SBX_TIMEOUT)
    try:
        if ctx.stack == "criu":
            sb.commands.run("echo never > /sys/kernel/mm/transparent_hugepage/enabled", user="root")
        hog = Hog(sb)
        hog.start(P)
        hog.dirty(P, "base")
        if f:
            hog.sh("dd if=/dev/urandom of=%s/file bs=1M count=%d conv=fsync 2>/dev/null" % (WD, f))
        hog.sh("sync")
        want = hog.state(f)
        t0 = time.perf_counter()
        base = sb.checkpoint.create(name="base")
        base_ms = (time.perf_counter() - t0) * 1000
        with lock:
            rows.append({"series": series, **point, "pass": pass_no, "rep": -1, "kind": "base",
                         "ckpt_ms": base_ms, "ckpt_server": ctx.server(sb, base.checkpoint_id, "create",
                                                                        getattr(base, "timings", None)),
                         "artifact": ctx.artifact(sb, base), "sandbox_id": sb.sandbox_id, "ok": True})
        def change_and_checkpoint(name):
            hog.dirty(d, name)
            hog.write_file(f)
            if f:
                hog.sh("sync")
            t0 = time.perf_counter()
            cp = sb.checkpoint.create(name=name)
            ms = (time.perf_counter() - t0) * 1000
            return cp, ms, ctx.server(sb, cp.checkpoint_id, "create", getattr(cp, "timings", None)), ctx.artifact(sb, cp)

        for rep in range(reps):
            cp, ck_ms, ck_server, art = change_and_checkpoint("r%da" % rep)
            cp2, ck2_ms, ck2_server, art2 = change_and_checkpoint("r%db" % rep)
            t0 = time.perf_counter()
            sb.checkpoint.restore(base.checkpoint_id)
            rs_ms = (time.perf_counter() - t0) * 1000
            rs_server = ctx.server(sb, base.checkpoint_id, "restore", getattr(sb.checkpoint, "last_timings", None))
            got = hog.state(f)
            ok = got == want
            for c in (cp2, cp):   # 先删子节点
                try:
                    sb.checkpoint.delete(c.checkpoint_id)
                except Exception as exc:  # 删不掉不影响本次测量，记下来
                    LOG.warning("删除 %s 失败：%s", c.checkpoint_id, exc)
            row = {"series": series, **point, "pass": pass_no, "rep": rep,
                   "kind": "warmup" if rep < warmup else "sample", "ckpt_ms": ck_ms, "restore_ms": rs_ms,
                   "ckpt_server": ck_server, "restore_server": rs_server, "artifact": art,
                   "ckpt2_ms": ck2_ms, "ckpt2_server": ck2_server, "artifact2": art2,
                   "mem_mode": getattr(cp, "mem_mode", None), "sandbox_id": sb.sandbox_id, "ok": ok}
            with lock:
                rows.append(row)
            if not ok:
                raise RuntimeError("restore 后内容不一致：%s P=%d d=%d f=%d rep %d" % (series, P, d, f, rep))
        LOG.info("%-5s P=%3d d=%3d f=%2d pass %d：%d 次完成", series, P, d, f, pass_no, reps)
    finally:
        try:
            sb.kill()
        except Exception as exc:
            LOG.warning("kill %s：%s", sb.sandbox_id, exc)


# ------------------------------------------------------------ 汇总

def pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[max(0, -(-q * len(values) // 100) - 1)]


def server_total(row, key):
    s = row.get(key) or {}
    for k in ("total_ms", "total"):
        if isinstance(s.get(k), (int, float)):
            return float(s[k])
    return None


def artifact_mib(row, key="artifact"):
    a = row.get(key) or {}
    v = a.get("image_bytes") if a.get("image_bytes") is not None else a.get("bytes")
    return None if v is None else v / MIB


def summarize(rows):
    out = []
    keys = []
    for r in rows:
        k = (r["series"], r.get("N", 1), r["P"], r["d"], r["f"])
        if k not in keys:
            keys.append(k)
    for series, n_conc, P, d, f in keys:
        rs = [r for r in rows if (r["series"], r.get("N", 1), r["P"], r["d"], r["f"]) == (series, n_conc, P, d, f)]
        samples = [r for r in rs if r["kind"] == "sample"]
        base = [r for r in rs if r["kind"] == "base"]
        e = {"series": series, "N": n_conc, "P": P, "d": d, "f": f, "n": len(samples),
             "failed": sum(1 for r in rs if not r["ok"])}
        for key, vals in (("ckpt_ms", [r["ckpt_ms"] for r in samples]),
                          ("ckpt2_ms", [r["ckpt2_ms"] for r in samples]),
                          ("ckpt2_server_ms", [server_total(r, "ckpt2_server") for r in samples]),
                          ("artifact2_mib", [artifact_mib(r, "artifact2") for r in samples]),
                          ("restore_ms", [r["restore_ms"] for r in samples]),
                          ("ckpt_server_ms", [server_total(r, "ckpt_server") for r in samples]),
                          ("restore_server_ms", [server_total(r, "restore_server") for r in samples]),
                          ("artifact_mib", [artifact_mib(r) for r in samples]),
                          ("base_ckpt_ms", [r["ckpt_ms"] for r in base]),
                          ("base_artifact_mib", [artifact_mib(r) for r in base])):
            vals = [v for v in vals if v is not None]
            e[key] = {"p50": statistics.median(vals), "p90": pct(vals, 90), "max": max(vals)} if vals else None
        out.append(e)
    return out


def fmt(stat, key="p50"):
    return "-" if not stat else "%.1f" % stat[key]


def print_summary(result):
    prov = result["provenance"]
    print("\n---- 汇总 ----")
    print("栈 %s · 宿主 %s · 内核 %s · orchestrator %s %s · 模板 %s" % (
        result["stack"], prov["hostname"], prov["kernel"], prov["orchestrator_exe"],
        (prov["orchestrator_sha256"] or "?")[:16], result["template"]))
    titles = {"dirty": "dirty：进程固定 256 MiB，改动量变化", "size": "size：改动固定 16 MiB，进程大小变化",
              "file": "file：只写文件", "conc": "conc：N 个沙箱同时做，每个进程 64 MiB、改动 16 MiB"}
    for series, title in titles.items():
        rows = [e for e in result["summary"] if e["series"] == series]
        if not rows:
            continue
        print("\n%s（单位 ms，产物 MiB；墙钟 p50/p90，服务端 total 只列 p50）" % title)
        print("%3s %4s %4s %3s %3s | %-30s | %-30s | %-23s | %7s" % (
            "N", "P", "d", "f", "n", "连续 ckpt：墙钟 / 服务端 / 产物", "restore 后 ckpt：墙钟 / 服务端 / 产物",
            "restore：墙钟 / 服务端", "全量基点"))
        for e in rows:
            print("%3d %4d %4d %3d %3d | %6s / %6s %7s %7s | %6s / %6s %7s %7s | %6s / %6s %7s | %7s%s" % (
                e["N"], e["P"], e["d"], e["f"], e["n"],
                fmt(e["ckpt2_ms"]), fmt(e["ckpt2_ms"], "p90"), fmt(e["ckpt2_server_ms"]), fmt(e["artifact2_mib"]),
                fmt(e["ckpt_ms"]), fmt(e["ckpt_ms"], "p90"), fmt(e["ckpt_server_ms"]), fmt(e["artifact_mib"]),
                fmt(e["restore_ms"]), fmt(e["restore_ms"], "p90"), fmt(e["restore_server_ms"]),
                fmt(e["base_ckpt_ms"]), "  失败 %d" % e["failed"] if e["failed"] else ""))
    print("\n判定: %s（测量 %d 次，失败 %d）" % (result["verdict"], sum(1 for r in result["rows"] if r["kind"] != "base"),
                                         sum(1 for r in result["rows"] if not r["ok"])))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--series", default="dirty,size,file,conc")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--reps", type=int, default=11)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--conc", default=",".join(map(str, CONC)), help="conc 组的 N 列表")
    ap.add_argument("--conc-reps", type=int, default=6)
    ap.add_argument("--stack", choices=("sandbox", "criu"))
    ap.add_argument("--template")
    ap.add_argument("--out", default="same-workload-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    exe, env = orchestrator_proc()
    stack = args.stack or detect_stack(exe)
    args.template = args.template or ("base-criu" if stack == "criu" else "base")
    if args.smoke:
        args.passes, args.reps, args.warmup, args.conc, args.conc_reps = 1, 3, 1, "1,2", 3
    store = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build", "checkpoints") if env else None
    from e2b import Sandbox

    result = {"schema_version": 1, "stack": stack, "template": args.template,
              "provenance": {"hostname": socket.gethostname(), "kernel": os.uname().release,
                             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             "script_sha256": sha256_file(os.path.abspath(__file__)),
                             "orchestrator_exe": exe, "orchestrator_sha256": sha256_file(exe) if exe else None,
                             "FC_TRACK_DIRTY_PAGES": env.get("FC_TRACK_DIRTY_PAGES"), "args": vars(args)},
              "rows": [], "summary": [], "verdict": "FAIL"}
    ctx = Ctx(args, stack, store)
    lock = threading.Lock()
    failure = None
    try:
        for series in args.series.split(","):
            if series == "conc":
                for n in [int(x) for x in args.conc.split(",")]:
                    rows = []
                    with cf.ThreadPoolExecutor(n) as pool:
                        futs = [pool.submit(one_sandbox, Sandbox, ctx, CONC_POINT, "conc", i, args.conc_reps,
                                            args.warmup, rows, lock) for i in range(n)]
                        errs = [f.exception() for f in futs if f.exception()]
                    for r in rows:
                        r["N"] = n
                    result["rows"].extend(rows)
                    if errs:
                        raise RuntimeError("conc N=%d：%s" % (n, errs[0]))
                    LOG.info("conc N=%d 完成", n)
                continue
            points = SERIES[series][:1] if args.smoke else SERIES[series]
            for point in points:
                for pass_no in range(args.passes):
                    one_sandbox(Sandbox, ctx, point, series, pass_no, args.reps, args.warmup, result["rows"], lock)
        result["verdict"] = "PASS"
    except Exception as exc:
        failure = str(exc)
        result["error"] = failure
        LOG.exception("测试停止")
    finally:
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        result["summary"] = summarize(result["rows"])
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print_summary(result)
        print("结果文件: %s" % os.path.abspath(args.out))
    return 0 if failure is None else 1


if __name__ == "__main__":
    sys.exit(main())
