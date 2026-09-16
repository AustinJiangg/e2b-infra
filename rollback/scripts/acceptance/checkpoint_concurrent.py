#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
并发 checkpoint / restore：多个沙箱同时拍、同一个沙箱多个调用方同时拍，正确性还在不在，
耗时怎么涨。

之前的验收和基准（checkpoint_verify.py / checkpoint_bench.py）都是一个沙箱、一个调用方。
真实部署里同一台宿主机上几十个沙箱各自在拍，偶尔还有同一个沙箱被两个客户端同时操作。
这个脚本把这两种并发分开量，各自回答一个问题：

  A. 跨沙箱扩展性 —— N 个沙箱**同一瞬间**发同一个操作（barrier 对齐），N 从 1 涨到 16，
     每一跳都验回到了哪一代。看 p50/最坏值随 N 怎么涨、涨在服务端哪一段（frozen /
     snapshot / seal / materialize / fc_rollback / conntrack / assemble_view）。
     宿主机侧同时记产物盘的写入量和 orchestrator 的 CPU 时间。
     服务端跨沙箱的共享点只有 store 的全局互斥（AppendLayer 在它里面做 fsync）、
     产物盘带宽、和 conntrack 删表（要扫整张宿主机表）；这一段就是要看这几处到底
     贵不贵。

  B. 同沙箱争用 —— 一个沙箱，T 个线程各自循环 create → restore(自己刚拍的) → list。
     服务端按沙箱串行（Store.LockSandbox），所以预期是排队而不是出错；要看的是：
     排队等待算不算进客户端 60 s 超时、超时之后服务端是不是照做了（list 比成功数
     多 = 幽灵 checkpoint）、风暴过后沙箱还活不活、每个 checkpoint 还能不能回。
     这一段**不断言现场逐项一致**：并发下"拍的那一刻现场是什么"本身没有定义 ——
     A 线程写完标记还没拿到锁，B 线程一个 restore 就把标记退回去了。能断言的是
     操作全部收敛、没有错误、沙箱可用、每个 checkpoint 都能回且回去之后是一致状态。
     各线程默认各自 `Sandbox.connect()`（`--same-conn`），也就是各走各的连接，
     这才是"多个独立调用方"；SDK 缺路由头的老环境会自动退回共用一个对象。

  D. 混合稳态 —— S 个沙箱各自单线程随机做 create / restore / delete 跑满 T 秒，
     每个沙箱内部是串行的，所以每一次 restore 都能逐项验现场（内存标记、文件标记、
     内存 blob md5、文件 blob md5、心跳进程 pid）。这一段回答"很多沙箱长时间各拍各的，
     有没有互相踩"。

  C.（默认不跑，--lifecycle 打开）生命周期竞争 —— checkpoint / restore 进行到一半
     把沙箱 kill 掉或原生 pause。服务端目前对这两条路没有互斥，这一段是去看症状：
     报什么错、orchestrator 还在不在、产物目录有没有残留。**可能把 orchestrator 打挂，
     宿主机上有别人的沙箱时不要开。**

每一步都记原始数据进 JSON（--out），表格只是摘要；分位数要自己算的话读 JSON。
在宿主机上跑才有服务端分段和盘写入量；不在宿主机上也能跑，那几列缺省。

依赖（与本目录其它脚本一致）:
    pip install e2b==2.20.0 python-dotenv
    python /opt/e2b-infra/patch_e2b.py
    python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py

环境变量（可放在当前目录 .env 里）:
    E2B_API_KEY / E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL

用法:
    python checkpoint_concurrent.py                       # A + B + D
    python checkpoint_concurrent.py --stages A --fanout 1,4,16
    python checkpoint_concurrent.py --stages D --soak-sandboxes 16 --soak-seconds 600
    python checkpoint_concurrent.py --stages C --lifecycle
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import glob
import hashlib
import json
import os
import random
import re
import socket
import statistics
import sys
import tempfile
import threading
import time
import traceback
import unicodedata

from e2b import Sandbox

# 客户端侧真超时的判据：httpx / httpcore 的 *Timeout 异常名，或明说超时的消息。
TIMEOUT_RE = re.compile(r"(ReadTimeout|WriteTimeout|ConnectTimeout|PoolTimeout|"
                        r"TimeoutException|timed out|deadline exceeded)", re.I)

DEFAULT_TEMPLATE_ID = "base"
BENCH_DIR = "/bench-root"        # 文件那份写这里（根文件系统上，进 NBD 写层）
HB_PID = "/dev/shm/hb.pid"
HB_LOG = "/dev/shm/hb.log"

# 一代改多少（内存那份写 /dev/shm，文件那份写根文件系统再 sync）。
GEN_MEM_MB = 48
GEN_FILE_MB = 16
# 预热把两个文件撑到这么大，之后每代只覆写前 N MB。
WARM_MEM_MB = 96
WARM_FILE_MB = 32


# ---------------------------------------------------------------- 终端排版

def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def lpad(s, n):
    return s + " " * max(0, n - width(s))


def rpad(s, n):
    return " " * max(0, n - width(s)) + s


def fmt_ms(x):
    return "-" if x is None else "%.0f" % x


def fmt_s(x):
    return "-" if x is None else "%.3f" % x


def p50(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def pmax(xs):
    xs = [x for x in xs if x is not None]
    return max(xs) if xs else None


def now_tag():
    return time.strftime("%Y%m%d-%H%M%S")


LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        print(msg, flush=True)


# ---------------------------------------------- 宿主机侧探测（同 checkpoint_bench.py）
#
# 宁可重复也不 import：本目录的脚本是一个个单独拷到目标机上跑的。

def fc_get(sandbox_id, path="/"):
    hits = glob.glob(os.path.join(tempfile.gettempdir(), "fc-%s-*.sock" % sandbox_id))
    if not hits:
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(hits[0])
        s.sendall(("GET %s HTTP/1.1\r\nHost: localhost\r\n"
                   "Accept: application/json\r\n\r\n" % path).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                return None
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
        return None
    finally:
        s.close()


def orchestrator_pid_env():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            base = os.path.basename(os.path.realpath("/proc/%s/exe" % pid))
            raw = open("/proc/%s/environ" % pid, "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        if not (base.startswith("orchestrator") or base.startswith("template-manager")
                or "ORCHESTRATOR_SERVICES=" in raw):
            continue
        return int(pid), dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
    return None, {}


def checkpoint_store():
    """checkpoint 产物目录、所在文件系统、所在块设备名（读 /proc/diskstats 用）。"""
    pid, env = orchestrator_pid_env()
    if not env:
        return None, "?", None
    store = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"),
                         "build", "checkpoints")
    best, fstype, dev = "", "?", None
    try:
        for line in open("/proc/mounts"):
            f = line.split()
            if len(f) < 3:
                continue
            mnt = f[1]
            if (store == mnt or store.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                best, fstype, dev = mnt, f[2], os.path.basename(f[0])
    except OSError:
        pass
    return store, fstype, dev


class HostMeter:
    """宿主机侧两个计数：产物盘写了多少（/proc/diskstats 第 10 列，扇区），
    orchestrator 用了多少 CPU 时间（/proc/pid/stat utime+stime）。
    差值就是这一阶段的量。不在宿主机上时全部 None。"""

    def __init__(self, dev):
        self.dev = dev
        self.pid, _ = orchestrator_pid_env()
        self.hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def disk_written_mb(self):
        if not self.dev:
            return None
        try:
            for line in open("/proc/diskstats"):
                f = line.split()
                if len(f) > 9 and f[2] == self.dev:
                    return int(f[9]) * 512 / 1048576.0
        except OSError:
            pass
        return None

    def orch_cpu_s(self):
        if not self.pid:
            return None
        try:
            f = open("/proc/%d/stat" % self.pid).read().rsplit(")", 1)[1].split()
            return (int(f[11]) + int(f[12])) / float(self.hz)
        except (OSError, IndexError, ValueError):
            return None

    def orch_rss_mb(self):
        if not self.pid:
            return None
        try:
            for line in open("/proc/%d/status" % self.pid):
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
        except OSError:
            pass
        return None

    def snapshot(self):
        return {"disk_mb": self.disk_written_mb(), "cpu_s": self.orch_cpu_s(),
                "rss_mb": self.orch_rss_mb(), "t": time.monotonic()}

    @staticmethod
    def delta(a, b):
        d = {}
        for k in ("disk_mb", "cpu_s"):
            d[k] = None if a[k] is None or b[k] is None else b[k] - a[k]
        d["rss_mb"] = b["rss_mb"]
        d["wall_s"] = b["t"] - a["t"]
        return d


def grab_orchestrator_log(sandbox_id, since_s=180):
    """从 nomad alloc 日志里抄这个沙箱的行（去掉 JSON 字段只留消息）。orchestrator 把
    Firecracker 的 stdout（含 guest 串口 console=ttyS0 的输出）也打进自己的日志，所以
    guest 有没有 panic、有没有任何输出，只能从这里看。日志按 10 MB 轮转，要趁早抄。"""
    import re
    import subprocess
    hits = glob.glob("/data/nomad/alloc/*/alloc/logs/start.stdout.*")
    if not hits:
        return ["<no nomad alloc logs found>"]
    try:
        out = subprocess.run(["grep", "-h", sandbox_id] + hits, capture_output=True, text=True,
                             timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as e:
        return ["<grep failed: %s>" % e]
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - since_s))
    lines = []
    for line in out.splitlines():
        if line[:19] < cutoff:
            continue
        lines.append(re.sub(r'  \{"service".*$', "", line)[:400])
    lines.sort()
    return lines[-400:]


# ---------------------------------------------------------------- 沙箱包装

def connect_can_checkpoint(sandbox_id):
    """`Sandbox.connect()` 拿到的对象能不能打 checkpoint 接口。

    SDK 的 connect() 一度不带 `E2b-Sandbox-Id`，而 checkpointd 的请求头里
    `E2b-Sandbox-Port` 是无条件加的，于是代理看到"只有一半"的路由头，直接答
    `missing header`（两个都不给反倒会回退去解析主机名，是填一半才炸）。
    修复：e2b-arm `66414855` / e2b-infra payload `884369b`。
    """
    try:
        Sandbox.connect(sandbox_id).checkpoint.list()
        return True
    except Exception:       # noqa: BLE001
        return False


class Box:
    """一个沙箱。单线程使用时可以逐项验现场；多线程共用时只用 create/restore/list。"""

    def __init__(self, sbx, store, label):
        self.sbx = sbx
        self.store = store
        self.label = label
        self.id = sbx.sandbox_id
        self.scenes = {}         # checkpoint_id -> 拍那一刻的现场
        self.names = {}          # checkpoint_id -> 代名

    def run(self, cmd, timeout=600):
        return self.sbx.commands.run(cmd, user="root", timeout=timeout).stdout

    def kv(self, out):
        d = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
        return d

    # ---- 准备

    def setup(self, warm_mem=WARM_MEM_MB, warm_file=WARM_FILE_MB):
        self.run("mkdir -p %s; rm -f %s %s" % (BENCH_DIR, HB_LOG, HB_PID))
        # 心跳：快照前就在跑的后台进程；pid 不变 = 内存回来了而不是虚机重启。
        self.run("setsid sh -c 'echo $$ > %s; while true; do echo tick >> %s; sleep 0.2; done'"
                 " >/dev/null 2>&1 </dev/null &\nsleep 0.3" % (HB_PID, HB_LOG))
        self.run("dd if=/dev/urandom of=/dev/shm/sweep bs=1M count=%d 2>/dev/null" % warm_mem)
        self.run("dd if=/dev/urandom of=%s/fsblob bs=1M count=%d 2>/dev/null; sync"
                 % (BENCH_DIR, warm_file), timeout=1800)

    def dirty(self, gen, mem_mb=GEN_MEM_MB, file_mb=GEN_FILE_MB):
        """改一代：覆写两个 blob 的前 N MB，写代号标记。"""
        self.run("dd if=/dev/urandom of=/dev/shm/sweep bs=1M count=%d conv=notrunc 2>/dev/null; "
                 "dd if=/dev/urandom of=%s/fsblob bs=1M count=%d conv=notrunc 2>/dev/null; "
                 "echo %s > /dev/shm/gen; echo %s > %s/gen; sync"
                 % (mem_mb, BENCH_DIR, file_mb, gen, gen, BENCH_DIR), timeout=1800)

    def scene(self):
        """现场：两个代号、两个 blob 前 4 MB 的 md5、心跳 pid。"""
        return self.kv(self.run(
            "echo mem_gen=$(cat /dev/shm/gen 2>/dev/null || echo MISSING)\n"
            "echo file_gen=$(cat %s/gen 2>/dev/null || echo MISSING)\n"
            "echo mem_md5=$(head -c 4194304 /dev/shm/sweep | md5sum | cut -c1-12)\n"
            "echo file_md5=$(head -c 4194304 %s/fsblob | md5sum | cut -c1-12)\n"
            "echo hb_pid=$(cat %s 2>/dev/null || echo MISSING)\n"
            % (BENCH_DIR, BENCH_DIR, HB_PID)))

    def alive(self):
        """还能干活：执行命令、写根文件系统、心跳在推进。"""
        d = self.kv(self.run(
            "echo cmd=$(echo alive)\n"
            "echo rw=$(echo n$$ > %s/probe && cat %s/probe)\n"
            "echo hb0=$(wc -l < %s 2>/dev/null || echo 0); sleep 1.5\n"
            "echo hb1=$(wc -l < %s 2>/dev/null || echo 0)\n"
            % (BENCH_DIR, BENCH_DIR, HB_LOG, HB_LOG), timeout=120))
        try:
            ticks = int(d.get("hb1", 0)) - int(d.get("hb0", 0))
        except ValueError:
            ticks = -1
        return d.get("cmd") == "alive" and d.get("rw", "").startswith("n") and ticks >= 1, d

    # ---- 服务端分段

    def phases(self, ck_id):
        if not self.store:
            return {}
        try:
            return json.load(open(os.path.join(self.store, self.id, ck_id, "timings.json")))
        except (OSError, ValueError):
            return {}

    def restore_phases(self):
        if not self.store:
            return {}
        try:
            return json.load(open(os.path.join(self.store, self.id, "last-restore-timings.json")))
        except (OSError, ValueError):
            return {}

    # ---- 操作（都返回一条记录）

    def create(self, gen, record_scene=True, timeout=None):
        rec = {"op": "create", "box": self.label, "sandbox": self.id, "gen": gen}
        if record_scene:
            rec["scene"] = self.scene()
        t0 = time.monotonic()
        try:
            ck = self.sbx.checkpoint.create(name=gen, request_timeout=timeout)
            rec["wall_s"] = time.monotonic() - t0
            rec["ok"] = True
            rec["id"] = ck.checkpoint_id
            rec["mem_mode"] = getattr(ck, "mem_mode", None) or "?"
            rec["phases"] = self.phases(ck.checkpoint_id)
            if record_scene:
                self.scenes[ck.checkpoint_id] = rec["scene"]
            self.names[ck.checkpoint_id] = gen
        except Exception as e:      # noqa: BLE001 —— 记下来就是目的
            rec["wall_s"] = time.monotonic() - t0
            rec["ok"] = False
            rec["err"] = "%s: %s" % (type(e).__name__, e)
        return rec

    def restore(self, ck_id, verify=True, timeout=None):
        rec = {"op": "restore", "box": self.label, "sandbox": self.id, "id": ck_id,
               "gen": self.names.get(ck_id, "?")}
        t0 = time.monotonic()
        try:
            ok = self.sbx.checkpoint.restore(ck_id, request_timeout=timeout)
            rec["wall_s"] = time.monotonic() - t0
            rec["phases"] = self.restore_phases()     # 必须紧接着读
            rec["ok"] = bool(ok)
        except Exception as e:      # noqa: BLE001
            rec["wall_s"] = time.monotonic() - t0
            rec["ok"] = False
            rec["err"] = "%s: %s" % (type(e).__name__, e)
            return rec
        if verify and ck_id in self.scenes:
            now = self.scene()
            want = self.scenes[ck_id]
            bad = {k: (want.get(k), now.get(k)) for k in want if now.get(k) != want.get(k)}
            rec["verified"] = not bad
            if bad:
                rec["mismatch"] = bad
        return rec

    def list_ids(self):
        return [c.checkpoint_id for c in self.sbx.checkpoint.list()]

    def delete(self, ck_id):
        rec = {"op": "delete", "box": self.label, "sandbox": self.id, "id": ck_id}
        t0 = time.monotonic()
        try:
            rec["ok"] = bool(self.sbx.checkpoint.delete(ck_id))
        except Exception as e:      # noqa: BLE001
            rec["ok"] = False
            rec["err"] = "%s: %s" % (type(e).__name__, e)
        rec["wall_s"] = time.monotonic() - t0
        self.scenes.pop(ck_id, None)
        self.names.pop(ck_id, None)
        return rec

    def kill(self):
        try:
            self.sbx.kill()
        except Exception:           # noqa: BLE001
            pass


def spawn(n, template, store, label_prefix, timeout=3600):
    """并行建 n 个沙箱（建沙箱本身也是并发的一部分，一起量）。"""
    boxes = [None] * n
    errs = [None] * n

    def one(i):
        try:
            sbx = Sandbox.create(template=template, timeout=timeout)
            boxes[i] = Box(sbx, store, "%s%d" % (label_prefix, i))
        except Exception as e:      # noqa: BLE001
            errs[i] = "%s: %s" % (type(e).__name__, e)

    t0 = time.monotonic()
    ts = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.monotonic() - t0
    return [b for b in boxes if b], [e for e in errs if e], dt


# ---------------------------------------------------------------- 汇总

def phase_cols(recs, keys):
    return {k: (p50([r.get("phases", {}).get(k) for r in recs]),
                pmax([r.get("phases", {}).get(k) for r in recs])) for k in keys}


CREATE_KEYS = ("frozen", "snapshot", "seal", "append_layer", "commit", "total")
RESTORE_KEYS = ("frozen", "save_live_bitmap", "materialize", "fc_rollback", "fc_memory",
                "assemble_view", "reset_view", "conntrack", "wait_envd", "total")


def print_op_table(title, groups, keys):
    """groups: [(行名, [records])]。每行：n、成功、验证、客户端 p50/max、各分段 p50/max。"""
    log("")
    log("  " + title)
    head = "  %s %s %s %s" % (lpad("场景", 16), rpad("n", 3), rpad("成功", 5), rpad("验证", 5))
    head += " %s" % rpad("客户端 p50/max s", 20)
    for k in keys:
        head += " %s" % rpad(k + " p50/max", max(14, len(k) + 8))
    log(head)
    log("  " + "-" * (width(head) - 2))
    for name, recs in groups:
        ok = sum(1 for r in recs if r.get("ok"))
        ver = [r.get("verified") for r in recs if "verified" in r]
        vs = "-" if not ver else "%d/%d" % (sum(1 for v in ver if v), len(ver))
        walls = [r.get("wall_s") for r in recs if r.get("ok")]
        line = "  %s %s %s %s %s" % (lpad(name, 16), rpad(str(len(recs)), 3),
                                     rpad("%d" % ok, 5), rpad(vs, 5),
                                     rpad("%s/%s" % (fmt_s(p50(walls)), fmt_s(pmax(walls))), 20))
        cols = phase_cols([r for r in recs if r.get("ok")], keys)
        for k in keys:
            a, b = cols[k]
            line += " %s" % rpad("%s/%s" % (fmt_ms(a), fmt_ms(b)), max(14, len(k) + 8))
        log(line)


def print_errors(recs):
    errs = [r for r in recs if not r.get("ok") or r.get("verified") is False]
    if not errs:
        return
    log("")
    log("  失败 / 不一致（%d 条）：" % len(errs))
    for r in errs[:40]:
        what = r.get("err") or ("现场不一致 %s" % json.dumps(r.get("mismatch"), ensure_ascii=False))
        log("    %s %s %s %s: %s" % (r.get("box"), r.get("op"), r.get("gen", ""),
                                     r.get("id", ""), what))
    if len(errs) > 40:
        log("    ... 还有 %d 条，见 JSON" % (len(errs) - 40))


# ---------------------------------------------------------------- A. 跨沙箱扩展性

def stage_a(args, store, meter, results):
    fanout = [int(x) for x in args.fanout.split(",") if x.strip()]
    log("\n===== A. 跨沙箱扩展性：N 个沙箱 barrier 对齐同时发同一个操作，N = %s =====" % fanout)
    log("  每个沙箱：预热 → cp0(全量树根) → 改一代 → cp1 → 改一代 → cp2 → 回 cp1 → 回 cp2 → 回 cp0")
    log("  每代改内存 %d MB + 文件 %d MB。" % (GEN_MEM_MB, GEN_FILE_MB))

    for n in fanout:
        log("\n  --- N = %d ---" % n)
        boxes, errs, spawn_s = spawn(n, args.template, store, "a%d-" % n)
        log("  建沙箱：%d 成功 %d 失败，并行总耗时 %.1f s" % (len(boxes), len(errs), spawn_s))
        for e in errs:
            log("    建沙箱失败：%s" % e)
        if not boxes:
            continue
        results["spawn"].append({"stage": "A", "n": n, "ok": len(boxes), "wall_s": spawn_s})

        # 预热并行做，不计时。
        def prep(b):
            try:
                b.setup()
                b.dirty("g0")
            except Exception as e:      # noqa: BLE001
                log("    %s 预热失败：%s" % (b.label, e))
        ts = [threading.Thread(target=prep, args=(b,)) for b in boxes]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        barrier = threading.Barrier(len(boxes), timeout=1800)
        step_recs = {}          # 步骤 -> [records]
        host_delta = {}         # 步骤 -> HostMeter.delta
        lock = threading.Lock()
        ids = {}                # box.label -> {gen: id}

        steps = [("cp0", "create", "g0"), ("dirty1", None, "g1"), ("cp1", "create", "g1"),
                 ("dirty2", None, "g2"), ("cp2", "create", "g2"),
                 ("restore->cp1", "restore", "g1"), ("restore->cp2", "restore", "g2"),
                 ("restore->cp0", "restore", "g0")]

        def worker(b):
            ids[b.label] = {}
            for step, op, gen in steps:
                if op is None:
                    b.dirty(gen)
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        return
                    continue
                try:
                    barrier.wait()          # 所有沙箱在同一瞬间发请求
                except threading.BrokenBarrierError:
                    return
                if op == "create":
                    rec = b.create(gen)
                    if rec.get("ok"):
                        ids[b.label][gen] = rec["id"]
                else:
                    ck = ids[b.label].get(gen)
                    rec = b.restore(ck) if ck else {"op": "restore", "box": b.label, "ok": False,
                                                    "err": "没有 %s 的 checkpoint" % gen}
                rec["stage"] = "A"
                rec["n"] = n
                rec["step"] = step
                with lock:
                    step_recs.setdefault(step, []).append(rec)
                    results["ops"].append(rec)

        # 宿主机计数：每一步的前后差。barrier 之后主线程无法精确对齐每步，改为
        # 用一个观察线程在 barrier 前后取样：简化成整个 N 的总差 + 各步的服务端分段。
        before = meter.snapshot()
        ts = [threading.Thread(target=worker, args=(b,)) for b in boxes]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        after = meter.snapshot()
        d = HostMeter.delta(before, after)
        results["host"].append({"stage": "A", "n": n, **d})

        groups = [(s, step_recs.get(s, [])) for s, op, _ in steps if op == "create"]
        print_op_table("checkpoint（N=%d）" % n, groups, CREATE_KEYS)
        groups = [(s, step_recs.get(s, [])) for s, op, _ in steps if op == "restore"]
        print_op_table("restore（N=%d）" % n, groups, RESTORE_KEYS)
        log("  宿主机：产物盘写入 %s MB，orchestrator CPU %s s，RSS %s MB，阶段墙钟 %.1f s"
            % (fmt_ms(d["disk_mb"]), fmt_s(d["cpu_s"]), fmt_ms(d["rss_mb"]), d["wall_s"]))
        print_errors([r for rs in step_recs.values() for r in rs])

        for b in boxes:
            b.kill()

    # 扩展性摘要：每步 p50 相对 N=1 的倍数。
    log("\n  --- A 摘要：客户端 p50 相对 N=%d 的倍数 ---" % fanout[0])
    base = {}
    for r in results["ops"]:
        if r.get("stage") == "A" and r.get("ok") and r["n"] == fanout[0]:
            base.setdefault(r["step"], []).append(r["wall_s"])
    head = "  %s" % lpad("步骤", 16)
    for n in fanout:
        head += " %s" % rpad("N=%d" % n, 12)
    log(head)
    for step in ("cp0", "cp1", "cp2", "restore->cp1", "restore->cp2", "restore->cp0"):
        b0 = p50(base.get(step, []))
        line = "  %s" % lpad(step, 16)
        for n in fanout:
            xs = [r["wall_s"] for r in results["ops"]
                  if r.get("stage") == "A" and r.get("ok") and r["n"] == n and r["step"] == step]
            v = p50(xs)
            if v is None:
                line += " %s" % rpad("-", 12)
            elif b0:
                line += " %s" % rpad("%.3fs ×%.1f" % (v, v / b0), 12)
            else:
                line += " %s" % rpad("%.3fs" % v, 12)
        log(line)


# ---------------------------------------------------------------- B. 同沙箱争用

def stage_b(args, store, meter, results):
    T, R = args.same_threads, args.same_rounds
    log("\n===== B. 同沙箱争用：1 个沙箱，%d 个线程各做 %d 轮 create → restore → list =====" % (T, R))
    log("  预期服务端按沙箱串行（LockSandbox）：排队、不出错。要看的是排队有没有撞上客户端 60 s 超时，")
    log("  超时之后服务端是否照做（list 比成功数多 = 幽灵 checkpoint），风暴后沙箱是否可用、各代能否回。")

    boxes, errs, _ = spawn(1, args.template, store, "b-")
    if not boxes:
        log("  建沙箱失败：%s" % errs)
        return
    box = boxes[0]
    box.setup()
    box.dirty("g0")
    root = box.create("g0")
    root["stage"] = "B"
    results["ops"].append(root)
    log("  树根 cp0：%s %.3f s mem_mode=%s" % ("✓" if root["ok"] else "✗", root["wall_s"],
                                             root.get("mem_mode")))

    # 各线程怎么拿 Sandbox 对象，决定了这一段到底在测什么：
    #   per-thread  各线程各自 Sandbox.connect()，各走各的连接 —— 这才是"同一个沙箱被多个
    #               独立调用方同时操作"的真实形态，B 段本来就是为这个设计的。
    #   shared      共用 Sandbox.create() 返回的那个对象（checkpoint 模块无状态、底下的
    #               httpx 连接池线程安全）。服务端压力一样，但客户端这侧共用一个连接池。
    # 早先只能用 shared：SDK 的 connect() 不带沙箱路由头，checkpoint 接口一律
    # "missing header"（e2b-arm 66414855 / e2b-infra payload 884369b 修的就是它）。
    # 装了修复的环境走 per-thread；auto 先探一次，没修的环境自动退回 shared。
    conn_mode = args.same_conn
    if conn_mode == "auto":
        conn_mode = "per-thread" if connect_can_checkpoint(box.id) else "shared"
        if conn_mode == "shared":
            log("  !! Sandbox.connect() 打不了 checkpoint（SDK 缺路由头），本段回退成共用对象")
    log("  各线程取连接的方式：%s" % conn_mode)

    recs = []
    lock = threading.Lock()
    creates_ok = []

    def worker(t):
        if conn_mode == "per-thread":
            try:
                sbx = Sandbox.connect(box.id)
            except Exception as e:      # noqa: BLE001
                with lock:
                    recs.append({"stage": "B", "op": "connect", "box": "t%d" % t,
                                 "ok": False, "err": str(e)})
                return
        else:
            sbx = box.sbx
        b = Box(sbx, store, "t%d" % t)
        for i in range(R):
            gen = "t%d-%d" % (t, i)
            # 各线程写各自的标记，不验现场（见文件头的说明），只记 create 前的值以备事后看。
            try:
                b.run("echo %s > /dev/shm/mark_t%d" % (gen, t), timeout=60)
            except Exception as e:      # noqa: BLE001
                with lock:
                    recs.append({"stage": "B", "op": "mark", "box": b.label, "ok": False,
                                 "err": str(e)})
            c = b.create(gen, record_scene=False)
            c["stage"] = "B"
            c["round"] = i
            with lock:
                recs.append(c)
                if c.get("ok"):
                    creates_ok.append(c["id"])
            if c.get("ok"):
                r = b.restore(c["id"], verify=False)
                r["stage"] = "B"
                r["round"] = i
                with lock:
                    recs.append(r)
            t0 = time.monotonic()
            try:
                n = len(b.list_ids())
                with lock:
                    recs.append({"stage": "B", "op": "list", "box": b.label, "ok": True,
                                 "count": n, "wall_s": time.monotonic() - t0})
            except Exception as e:      # noqa: BLE001
                with lock:
                    recs.append({"stage": "B", "op": "list", "box": b.label, "ok": False,
                                 "err": str(e), "wall_s": time.monotonic() - t0})

    before = meter.snapshot()
    t0 = time.monotonic()
    ts = [threading.Thread(target=worker, args=(t,)) for t in range(T)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    storm_s = time.monotonic() - t0
    d = HostMeter.delta(before, meter.snapshot())
    results["ops"].extend(recs)
    results["host"].append({"stage": "B", "threads": T, "rounds": R, **d})

    creates = [r for r in recs if r["op"] == "create"]
    restores = [r for r in recs if r["op"] == "restore"]
    lists = [r for r in recs if r["op"] == "list"]
    print_op_table("同沙箱 %d 线程 × %d 轮（风暴总墙钟 %.1f s）" % (T, R, storm_s),
                   [("create", creates), ("restore", restores)], ("frozen", "total"))
    lw = [r["wall_s"] for r in lists if r.get("ok")]
    log("  list：%d 次，p50/max %s/%s s（list 不拿沙箱锁，应当不受排队影响）"
        % (len(lists), fmt_s(p50(lw)), fmt_s(pmax(lw))))
    # 只认客户端侧真的超时。不能拿 "imeout" 裸匹配 —— 服务端正常业务话术里就有
    # "sandbox timeout"，那样会把业务错误算成超时（09-15 那轮的"客户端超时 1 次"就是假阳性）。
    timeouts = [r for r in recs if not r.get("ok") and TIMEOUT_RE.search(r.get("err") or "")]
    log("  客户端超时：%d 次" % len(timeouts))

    # 风暴之后：服务端到底记了多少个 checkpoint（含 cp0）。
    try:
        server_ids = box.list_ids()
    except Exception as e:      # noqa: BLE001
        server_ids = []
        log("  风暴后 list 失败：%s" % e)
    expected = 1 + len(creates_ok)
    ghosts = len(server_ids) - expected
    log("  服务端 checkpoint 数 %d，客户端成功 create %d（含 cp0 应为 %d）→ 幽灵 %d"
        % (len(server_ids), len(creates_ok), expected, max(ghosts, 0)))
    results["summary"]["B"] = {"threads": T, "rounds": R, "storm_s": storm_s,
                               "conn_mode": conn_mode,
                               "creates_ok": len(creates_ok), "server_count": len(server_ids),
                               "ghosts": max(ghosts, 0), "timeouts": len(timeouts)}

    ok, d = box.alive()
    log("  风暴后沙箱可用：%s  %s" % ("✓" if ok else "✗", "" if ok else json.dumps(d)))
    results["summary"]["B"]["alive_after"] = ok

    # 每一代还能不能回、回去之后是不是一致状态（这里能验的是：回去后沙箱活着、心跳 pid 不变）。
    hb0 = box.scene().get("hb_pid")
    back_ok = 0
    for ck in server_ids:
        r = box.restore(ck, verify=False)
        r["stage"] = "B-after"
        results["ops"].append(r)
        if r.get("ok"):
            s = box.scene()
            if s.get("hb_pid") == hb0 and s.get("mem_gen") != "MISSING":
                back_ok += 1
            else:
                r["ok"] = False
                r["err"] = "回去后状态不对：%s" % s
    log("  风暴后逐个回：%d/%d 成功且心跳 pid 不变" % (back_ok, len(server_ids)))
    results["summary"]["B"]["restore_all_after"] = "%d/%d" % (back_ok, len(server_ids))
    print_errors(recs)
    box.kill()


# ---------------------------------------------------------------- D. 混合稳态

def stage_d(args, store, meter, results):
    S, T = args.soak_sandboxes, args.soak_seconds
    log("\n===== D. 混合稳态：%d 个沙箱各自随机 create/restore/delete 跑 %d s，每次 restore 逐项验现场 =====" % (S, T))
    boxes, errs, spawn_s = spawn(S, args.template, store, "d-")
    log("  建沙箱：%d 成功 %d 失败，%.1f s" % (len(boxes), len(errs), spawn_s))
    if not boxes:
        return

    recs = []
    lock = threading.Lock()
    stop = time.monotonic() + T

    def worker(b):
        rng = random.Random(hash(b.label))
        try:
            b.setup()
            b.dirty("g0")
        except Exception as e:      # noqa: BLE001
            with lock:
                recs.append({"stage": "D", "op": "setup", "box": b.label, "ok": False, "err": str(e)})
            return
        r = b.create("g0")
        r["stage"] = "D"
        with lock:
            recs.append(r)
        gen = 0
        while time.monotonic() < stop:
            ids = list(b.scenes.keys())
            x = rng.random()
            if x < 0.5 or len(ids) < 2:
                gen += 1
                g = "g%d" % gen
                try:
                    b.dirty(g, mem_mb=8, file_mb=4)
                except Exception as e:      # noqa: BLE001
                    with lock:
                        recs.append({"stage": "D", "op": "dirty", "box": b.label, "ok": False, "err": str(e)})
                    continue
                r = b.create(g)
            elif x < 0.85:
                r = b.restore(rng.choice(ids))
            else:
                r = b.delete(rng.choice(ids))
            r["stage"] = "D"
            r["depth"] = len(b.scenes)
            with lock:
                recs.append(r)
            if r["op"] == "restore" and not r.get("ok"):
                # 第一次 restore 失败就停手：之后每一次调用都只会往死沙箱上叠一条
                # "sandbox not found"，几分钟就把 orchestrator 的日志轮转冲光，现场没了。
                b.failed_at = time.time()
                log("  !! %s 沙箱 %s restore 失败，停止对它的一切操作：%s" % (b.label, b.id, r.get("err", "")[:160]))
                return
        try:
            ok, d = b.alive()
        except Exception as e:      # noqa: BLE001
            ok, d = False, {"err": "%s: %s" % (type(e).__name__, e)}
        with lock:
            recs.append({"stage": "D", "op": "alive", "box": b.label, "ok": ok, "detail": d})

    def worker_guarded(b):
        """restore 之后 guest 失联（envd 45 s 没回来）时，不 kill：留着现场，先记 FC 的
        instance info，再试一次回到别的 checkpoint 看 guest 能不能回来 —— 能回来说明坏的
        是设备/网络状态，回不来说明 guest 内核卡死。沙箱 id 打出来供手工查。"""
        try:
            worker(b)
        finally:
            last = [o for o in recs if o.get("box") == b.label and o["op"] == "restore" and not o.get("ok")]
            if last and args.keep_on_failure:
                b.keep = True
                diag = {"stage": "D", "op": "diag", "box": b.label, "sandbox": b.id,
                        "failed_id": last[-1].get("id"), "fc_presence": [], "console": []}
                # (1) 先什么都不碰，只看 FC 进程会不会自己退出——区分"guest 自己 panic/重启
                #     让 FC 退出"和"我们后面的操作把它弄没了"。fc_get 拿不到 = 套接字没了。
                t0 = time.time()
                for wait_s in (0, 5, 15, 30, 60, 120):
                    while time.time() - t0 < wait_s:
                        time.sleep(1)
                    info = fc_get(b.id)
                    diag["fc_presence"].append({"t": round(time.time() - t0, 1),
                                                "state": (info or {}).get("state"),
                                                "present": info is not None})
                    if info is None:
                        break
                # (2) 抄 orchestrator 日志里这个沙箱的所有行（含 FC 转发的 guest 串口输出），趁没轮转掉。
                diag["console"] = grab_orchestrator_log(b.id, since_s=180)
                # (3) 到这一步 FC 还在，才试一次回到别的 checkpoint，看 guest 能不能回来。
                others = [c for c in b.scenes if c != last[-1].get("id")]
                if others and diag["fc_presence"][-1]["present"]:
                    r2 = b.restore(others[-1], verify=False, timeout=90)
                    diag["second_restore_ok"] = r2.get("ok")
                    diag["second_restore_err"] = r2.get("err")
                    try:
                        diag["alive_after_second"] = b.alive()[0]
                    except Exception as e:      # noqa: BLE001
                        diag["alive_after_second"] = False
                        diag["alive_err"] = str(e)
                    diag["console_after_second"] = grab_orchestrator_log(b.id, since_s=60)
                with lock:
                    recs.append(diag)
                log("  !! %s 沙箱 %s guest 失联：FC 存在性 %s；再回一次=%s，之后可用=%s；抄到日志 %d 行"
                    % (b.label, b.id,
                       " ".join("%ss:%s" % (p["t"], "在" if p["present"] else "没了") for p in diag["fc_presence"]),
                       diag.get("second_restore_ok"), diag.get("alive_after_second"), len(diag["console"])))

    before = meter.snapshot()
    ts = [threading.Thread(target=worker_guarded, args=(b,)) for b in boxes]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    d = HostMeter.delta(before, meter.snapshot())
    results["ops"].extend(recs)
    results["host"].append({"stage": "D", "sandboxes": S, "seconds": T, **d})

    creates = [r for r in recs if r["op"] == "create"]
    restores = [r for r in recs if r["op"] == "restore"]
    deletes = [r for r in recs if r["op"] == "delete"]
    alive = [r for r in recs if r["op"] == "alive"]
    print_op_table("稳态 %d 沙箱 × %d s" % (S, T),
                   [("create", creates), ("restore", restores)], ("frozen", "total"))
    log("  delete：%d 次，%d 成功" % (len(deletes), sum(1 for r in deletes if r.get("ok"))))
    log("  结束时沙箱可用：%d/%d" % (sum(1 for r in alive if r.get("ok")), len(alive)))
    ver = [r for r in restores if "verified" in r]
    log("  restore 现场逐项验证：%d/%d 一致" % (sum(1 for r in ver if r["verified"]), len(ver)))
    depths = [r.get("depth", 0) for r in creates]
    log("  链深（每沙箱同时存在的 checkpoint 数）最大 %d" % (max(depths) if depths else 0))
    log("  宿主机：产物盘写入 %s MB，orchestrator CPU %s s，RSS %s MB"
        % (fmt_ms(d["disk_mb"]), fmt_s(d["cpu_s"]), fmt_ms(d["rss_mb"])))
    results["summary"]["D"] = {
        "sandboxes": S, "seconds": T, "creates": len(creates),
        "creates_ok": sum(1 for r in creates if r.get("ok")),
        "restores": len(restores), "restores_ok": sum(1 for r in restores if r.get("ok")),
        "verified": "%d/%d" % (sum(1 for r in ver if r["verified"]), len(ver)),
        "alive": "%d/%d" % (sum(1 for r in alive if r.get("ok")), len(alive)),
    }
    print_errors(recs)
    kept = [b for b in boxes if getattr(b, "keep", False)]
    for b in boxes:
        if b not in kept:
            b.kill()
    if kept:
        log("  保留未 kill 的沙箱（--keep-on-failure）：%s" % ", ".join(b.id for b in kept))


# ---------------------------------------------------------------- C. 生命周期竞争（opt-in）

def stage_c(args, store, meter, results):
    log("\n===== C. 生命周期竞争：操作进行到一半 kill / pause 沙箱（服务端目前无互斥，看症状） =====")
    cases = [("kill-during-create", "create"), ("kill-during-restore", "restore"),
             ("pause-during-create", "create")]
    for name, op in cases:
        boxes, errs, _ = spawn(1, args.template, store, "c-")
        if not boxes:
            log("  %s：建沙箱失败 %s" % (name, errs))
            continue
        b = boxes[0]
        # 改大一点让操作慢一些，好卡在中间。
        b.setup(warm_mem=256, warm_file=64)
        b.dirty("g0", mem_mb=256, file_mb=64)
        target = None
        if op == "restore":
            r = b.create("g0")
            target = r.get("id")
            b.dirty("g1", mem_mb=256, file_mb=64)
        out = {}

        def do():
            out["rec"] = b.create("gX") if op == "create" else b.restore(target, verify=False)

        t = threading.Thread(target=do)
        t.start()
        time.sleep(0.15)             # 让请求先到服务端、进入 pause
        t0 = time.monotonic()
        try:
            if name.startswith("kill"):
                b.sbx.kill()
                out["interfere"] = "kill ok %.3f s" % (time.monotonic() - t0)
            else:
                b.sbx.pause()
                out["interfere"] = "pause ok %.3f s" % (time.monotonic() - t0)
        except Exception as e:      # noqa: BLE001
            out["interfere"] = "%s: %s" % (type(e).__name__, e)
        t.join(timeout=300)
        rec = out.get("rec", {"ok": False, "err": "操作 300 s 没返回"})
        rec.update({"stage": "C", "case": name, "interfere": out.get("interfere")})
        results["ops"].append(rec)
        log("  %s：操作 → %s（%s）；干扰 → %s"
            % (name, "成功" if rec.get("ok") else "失败", rec.get("err") or "%.3f s" % rec.get("wall_s", 0),
               out.get("interfere")))
        # orchestrator 还在不在：再建一个沙箱。
        try:
            probe = Sandbox.create(template=args.template, timeout=120)
            probe.kill()
            log("    orchestrator 仍可建沙箱 ✓")
            rec["orch_alive"] = True
        except Exception as e:      # noqa: BLE001
            log("    orchestrator 建沙箱失败 ✗ %s" % e)
            rec["orch_alive"] = False
        if store:
            left = os.path.exists(os.path.join(store, b.id))
            log("    产物目录残留：%s" % ("有 %s" % os.path.join(store, b.id) if left else "无"))
            rec["store_leftover"] = left
        if not name.startswith("kill"):
            b.kill()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="并发 checkpoint/restore：扩展性、同沙箱争用、混合稳态")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE_ID)
    ap.add_argument("--stages", default="A,B,D", help="要跑的段，逗号分隔（A/B/C/D）")
    ap.add_argument("--fanout", default="1,2,4,8,16", help="A 段的 N 序列")
    ap.add_argument("--same-threads", type=int, default=4, help="B 段线程数")
    ap.add_argument("--same-rounds", type=int, default=5, help="B 段每线程轮数")
    ap.add_argument("--same-conn", choices=("auto", "per-thread", "shared"), default="auto",
                    help="B 段各线程怎么拿 Sandbox 对象：per-thread 各自 Sandbox.connect()"
                         "（贴近真实的多调用方）、shared 共用 Sandbox.create() 那个对象、"
                         "auto 先探一次 connect() 能否打 checkpoint，不能则回退 shared")
    ap.add_argument("--soak-sandboxes", type=int, default=8, help="D 段沙箱数")
    ap.add_argument("--soak-seconds", type=int, default=180, help="D 段时长")
    ap.add_argument("--lifecycle", action="store_true", help="允许跑 C 段（可能打挂 orchestrator）")
    ap.add_argument("--keep-on-failure", action="store_true",
                    help="D 段里 restore 后 guest 失联的沙箱不 kill，留现场并做一次二次 restore 诊断")
    ap.add_argument("--out", default=None, help="原始数据 JSON 路径（默认 concurrent-<时间>.json）")
    args = ap.parse_args()

    stages = [s.strip().upper() for s in args.stages.split(",") if s.strip()]
    if "C" in stages and not args.lifecycle:
        log("C 段要加 --lifecycle 才跑（它可能把 orchestrator 打挂）。")
        return 1

    store, fstype, dev = checkpoint_store()
    meter = HostMeter(dev)
    log("模板 = %s   段 = %s" % (args.template, ",".join(stages)))
    if store:
        log("产物落盘 : %s   文件系统 = %s   块设备 = %s" % (store, fstype, dev))
    else:
        log("产物落盘 : 未知（不在宿主机上？）服务端分段和盘写入量那几列缺省。")

    # 脏页后端：起一个沙箱问一下 Firecracker。
    probe = Sandbox.create(template=args.template, timeout=120)
    info = fc_get(probe.sandbox_id) or {}
    backend = info.get("dirty_tracking", "?")
    log("脏页后端 : %s   [FC %s]" % (backend, info.get("vmm_version", "?")))
    probe.kill()
    if backend == "off":
        log("  脏页跟踪没开，每次 checkpoint 都是全量；数字没有意义。查 FC_TRACK_DIRTY_PAGES。")
        return 1

    results = {"meta": {"template": args.template, "stages": stages, "store": store, "fstype": fstype,
                        "dirty_tracking": backend, "fc": info.get("vmm_version"),
                        "host": socket.gethostname(), "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "gen_mem_mb": GEN_MEM_MB, "gen_file_mb": GEN_FILE_MB,
                        "args": vars(args)},
               "ops": [], "spawn": [], "host": [], "summary": {}}
    out = args.out or "concurrent-%s.json" % now_tag()

    t0 = time.monotonic()
    try:
        for s in stages:
            {"A": stage_a, "B": stage_b, "C": stage_c, "D": stage_d}[s](args, store, meter, results)
    except KeyboardInterrupt:
        log("\n中断。已有数据照写。")
    except Exception:       # noqa: BLE001
        log("\n脚本异常：\n" + traceback.format_exc())
    results["meta"]["elapsed_s"] = time.monotonic() - t0
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    log("\n原始数据：%s（%d 条操作记录，总耗时 %.0f s）" % (out, len(results["ops"]), results["meta"]["elapsed_s"]))

    bad = [r for r in results["ops"] if r.get("stage") in ("A", "D") and
           (not r.get("ok") or r.get("verified") is False)]
    log("A/D 段失败或不一致：%d 条" % len(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
