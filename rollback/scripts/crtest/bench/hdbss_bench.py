#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HDBSS 专项：证据（这个沙箱真的在用 HDBSS 吗）+ 写密集基准（buffer 大小的影响）。

回答两个问题：
  1. 这台机器、这个沙箱的脏页跟踪走的是不是 HDBSS，`FC_HDBSS_ORDER` 是不是真的生效了；
  2. 在当前 order 下，写密集负载付出多少代价：guest 里写这批页花多久、每页多少次
     KVM 退出、增量 checkpoint 多久、内存差分多大、restore 之后内容对不对。

**不做 HDBSS 与软件写保护的对照** —— 本脚本只量 HDBSS 自己。脏页后端不是 `hdbss`
（例如鲲鹏 920B 上是 `kvm-wp`）时默认以退出码 3（前置不满足）结束；带
`--force-non-hdbss` 照跑，只用来验证流程，数字没有意义。

一次只测「当前这个 order」：order 是 Firecracker 在构建每台虚机时从自己的环境变量
`FC_HDBSS_ORDER` 读的，而它的环境继承自 orchestrator，所以换 order 要改 nomad job 的 env
并重启 —— 那一步由 `rollback/scripts/950/run.sh hdbss --orders …` 做，本脚本不碰 job。
多个 order 的结果用 `--summarize` 拼成一张对比表。

怎么量（每一条都是为了只留下「脏页跟踪」这一项开销）：

  · guest 里起一个常驻写者（python3，/dev/shm 上一块 arena，每个 vCPU 一个绑核的工作
    进程）。工作进程在基线 checkpoint 之前就把 arena 的每一页写过一遍，所以之后每次写
    都不再有 guest 自己的缺页；restore 回基线时写者连同它的页表一起回到这个状态。
  · 一次迭代 = 处在基线 cpA → **冷写**（这批页自上次 checkpoint / restore 以来第一次被写，
    脏页跟踪的开销全在这里）→ **热写**（同一批页原样再写一遍，页已经是脏的，只剩解释器
    自己的循环开销）→ 记 arena 的 md5 → 增量 checkpoint 出 cpB → 把这批页改写掉 →
    restore 回 cpB，md5 必须回到刚才那个值 → restore 回 cpA（撤销这份改动）→ 删掉 cpB。
    「冷写 − 热写」就是脏页跟踪本身的代价；软件写保护下它约等于每页一次陷出，HDBSS 下
    它是 CPU 记一笔的代价加上 buffer 写满时倒一次的代价。
  · 两种写法每页都只写 1 个字节、走同一个解释器循环：`seq` 是 arena 开头连续 N 页，
    `rand` 是整块 arena 里随机挑的 N 页、按随机顺序写（同样的页数但散开）。
  · 1 线程只用 0 号工作进程（绑 vCPU0），2 线程两个工作进程各写一半 —— HDBSS 的 buffer
    是每个 vCPU 一个。
  · KVM 退出数取自 debugfs 里这台虚机自己的计数器（/sys/kernel/debug/kvm/<FC pid>-*/exits），
    没有 debugfs 时退回 `perf stat -e kvm:kvm_exit`，都没有就空着。

PASS 只看正确性与流程：checkpoint / restore 没有失败、md5 全部一致、order 核实生效。
性能数字只记录，不判 PASS / FAIL。

用法：
    python3 bench/hdbss_bench.py --outdir /tmp/hdbss                      # 当前 order，完整一轮
    python3 bench/hdbss_bench.py --evidence --outdir /tmp/hdbss           # 只做证据（约 1 分钟）
    python3 bench/hdbss_bench.py --quick --force-non-hdbss --outdir /tmp/hdbss
    python3 bench/hdbss_bench.py --summarize /tmp/hdbss/hdbss-order*.json \\
        --md /tmp/hdbss/hdbss-table.md --json /tmp/hdbss/hdbss-summary.json

用词：本文的 checkpoint = 拍快照。原始数据里的 op 名 `create` 是接口名
（checkpoint.Checkpoint/Create），保留不改。
"""

import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

# crtest 包在本文件的上一级（rollback/scripts/crtest/crtest），按位置定位。
CRTEST_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CRTEST_PARENT not in sys.path:
    sys.path.insert(0, CRTEST_PARENT)

from crtest import common                                    # noqa: E402
from crtest.common import log                                # noqa: E402

CASE = "HDBSS-BENCH"
MB = 1048576.0
PAGE = 4096

ARENA = "/dev/shm/hdbss-arena"        # guest 里被写的那块内存（tmpfs，页永远驻留）
RUNDIR = "/dev/shm/hdbss-run"         # 写者的命令 FIFO 与结果文件
PROG = "/dev/shm/hdbss-writer.py"
WLOG = "/dev/shm/hdbss-writer.log"

# KVM 的 arm64 硬件脏状态跟踪能力号与查询 ioctl（同 orchestrator 的
# internal/sandbox/fc/dirtytracking.go）：KVM_CHECK_EXTENSION 只查询，不建虚机。
KVM_CAP_ARM_HW_DIRTY_STATE_TRACK = 502
KVM_CHECK_EXTENSION = 0xAE03

ORDER_ENV = "FC_HDBSS_ORDER"
DEFAULT_ORDER = 1                     # Firecracker 没读到 / 解析不了时取的值（vstate/vm.rs）

DEFAULT_SIZES = "16,64,256,512,1024"  # MB；装不下的大档会自动去掉
QUICK_SIZES = "16,64"
EVIDENCE_SIZE = 256
GUEST_MARGIN_MB = 256                 # arena 之外给 guest 留的可用内存
SEED = 20261008                       # rand 写法挑页用；固定下来，各 order 写的是同一批页


# ------------------------------------------------------------------ guest 里的写者
#
# python3 源码，用 Box.put() 塞进 guest。命令从 FIFO 进来（一行一条，开头是调用方给的
# 流水号），结果写成 <RUNDIR>/res.<流水号>。主进程不碰 arena，只调度工作进程：
#   W <mode> <页数> <seed> <线程数> <值>   选定的工作进程各写自己那一份，回墙钟
#   H                                     全部工作进程各算一段 md5，回合并后的 md5
# 工作进程之间用管道，全在 guest 内存里，checkpoint / restore 时跟着虚机一起走。
GUEST_WRITER = r'''
import hashlib, mmap, os, random, sys, time

arena, arena_mb, nworkers, rundir = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
PAGE = 4096
SIZE = arena_mb << 20
TOTAL = SIZE // PAGE
NCPU = os.cpu_count() or 1
MBB = 1 << 20


def offsets(mode, n, seed, k, w):
    if mode == 'seq':
        return range((k * n // w) * PAGE, ((k + 1) * n // w) * PAGE, PAGE)
    pages = random.Random(seed).sample(range(TOTAL), n)
    return [p * PAGE for p in pages[k::w]]


def worker(k, rd, wr):
    try:
        os.sched_setaffinity(0, {k % NCPU})
    except (AttributeError, OSError):
        pass
    fd = os.open(arena, os.O_RDWR)
    m = mmap.mmap(fd, SIZE)
    os.close(fd)
    for off in range(0, SIZE, PAGE):      # every page mapped writable before the baseline
        m[off] = m[off]
    wr.write('ok\n')
    wr.flush()
    offs, val = (), 0
    for line in rd:
        f = line.split()
        if not f:
            continue
        if f[0] == 'P':
            offs = offsets(f[1], int(f[2]), int(f[3]), int(f[4]), int(f[5]))
            val = int(f[6])
            wr.write('R\n')
        elif f[0] == 'G':
            go = float(f[1])                # spin to a common start: the vCPU is awake and
            while time.monotonic() < go:    # all workers begin at the same instant
                pass
            t0 = time.monotonic()
            for off in offs:
                m[off] = val
            t1 = time.monotonic()
            wr.write('T %.9f %.9f\n' % (t0, t1))
        elif f[0] == 'H':
            kk, w = int(f[1]), int(f[2])
            a, b = (kk * TOTAL // w) * PAGE, ((kk + 1) * TOTAL // w) * PAGE
            mv = memoryview(m)
            h = hashlib.md5(mv[a:b]).hexdigest()
            mv.release()
            wr.write('D %s\n' % h)
        wr.flush()


def main():
    os.makedirs(rundir, exist_ok=True)
    fifo = os.path.join(rundir, 'cmd')
    if not os.path.exists(fifo):
        os.mkfifo(fifo)
    fd = os.open(arena, os.O_RDWR | os.O_CREAT, 0o600)
    os.ftruncate(fd, SIZE)
    m = mmap.mmap(fd, SIZE)
    os.close(fd)
    for i in range(arena_mb):             # non-zero, non-repeating content
        m[i * MBB:(i + 1) * MBB] = hashlib.shake_256(b'hdbss-%d' % i).digest(MBB)
    m.close()

    ws = []
    for k in range(nworkers):
        c_r, c_w = os.pipe()
        r_r, r_w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(c_w)
            os.close(r_r)
            try:
                worker(k, os.fdopen(c_r), os.fdopen(r_w, 'w'))
            finally:
                os._exit(0)
        os.close(c_r)
        os.close(r_w)
        ws.append((os.fdopen(c_w, 'w'), os.fdopen(r_r)))
    for cw, rr in ws:
        if rr.readline().strip() != 'ok':
            sys.exit('worker did not come up')

    def send(w, text):
        w[0].write(text + '\n')
        w[0].flush()

    def do_write(mode, n, seed, threads, val):
        use = ws[:threads]
        for k, w in enumerate(use):
            send(w, 'P %s %d %d %d %d %d' % (mode, n, seed, k, len(use), val))
        for w in use:
            w[1].readline()
        go = time.monotonic() + 0.005
        for w in use:
            send(w, 'G %.9f' % go)
        ts = [w[1].readline().split() for w in use]
        t0 = min(float(t[1]) for t in ts)
        t1 = max(float(t[2]) for t in ts)
        per = ','.join('%.6f' % (float(t[2]) - float(t[1])) for t in ts)
        return 'wall_s=%.6f\npages=%d\nthreads=%d\nper_worker_s=%s\n' % (t1 - t0, n, len(use), per)

    def do_md5():
        t0 = time.monotonic()
        for k, w in enumerate(ws):
            send(w, 'H %d %d' % (k, len(ws)))
        parts = [w[1].readline().split()[1] for w in ws]
        h = hashlib.md5(''.join(parts).encode()).hexdigest()
        return 'md5=%s\nmd5_s=%.6f\n' % (h, time.monotonic() - t0)

    def answer(tag, text):
        tmp = os.path.join(rundir, 'tmp.%s' % tag)
        with open(tmp, 'w') as f:
            f.write(text)
        os.replace(tmp, os.path.join(rundir, 'res.%s' % tag))

    with open(os.path.join(rundir, 'ready'), 'w') as f:
        f.write('workers=%d\nncpu=%d\ntotal_pages=%d\n' % (len(ws), NCPU, TOTAL))

    while True:
        with open(fifo) as f:
            for line in f:
                c = line.split()
                if len(c) < 2:
                    continue
                tag, op = c[0], c[1]
                try:
                    if op == 'W':
                        out = do_write(c[2], int(c[3]), int(c[4]), int(c[5]), int(c[6]))
                    elif op == 'H':
                        out = do_md5()
                    else:
                        out = 'err=unknown op %s\n' % op
                except Exception as e:      # noqa: BLE001
                    out = 'err=%s: %s\n' % (type(e).__name__, e)
                answer(tag, out)


main()
'''


class GuestWriter:
    """guest 里那个常驻写者的宿主侧句柄。流水号由宿主侧发，所以 restore 把写者的内存
    搬回过去也不会撞号。"""

    def __init__(self, box, arena_mb, workers):
        self.box = box
        self.arena_mb = arena_mb
        self.workers = workers
        self.nonce = "%x" % int(time.time())
        self.seq = 0

    def start(self):
        t0 = time.monotonic()
        self.box.put(PROG, GUEST_WRITER)
        self.box.run("rm -rf %s %s; setsid sh -c 'exec python3 %s %s %d %d %s' >%s 2>&1 </dev/null &\n"
                     "sleep 0.5" % (RUNDIR, ARENA, PROG, ARENA, self.arena_mb, self.workers,
                                    RUNDIR, WLOG))
        limit = 60 + self.arena_mb          # 铺内容 + 每个工作进程逐页预写，1 GB 约十几秒
        rc, out = self.box.sh("i=0; while [ ! -e %s/ready ] && [ $i -lt %d ]; do sleep 0.5; "
                              "i=$((i+1)); done; cat %s/ready" % (RUNDIR, limit * 2, RUNDIR),
                              timeout=limit + 60)
        kv = common.parse_kv(out)
        if kv.get("workers") != str(self.workers):
            rc, tail = self.box.sh("tail -20 %s" % WLOG)
            raise common.Failed("guest 写者起来", "ready（%d 个工作进程）" % self.workers,
                                "没等到 ready：%s" % tail[-400:], "guest 内 python3 %s" % PROG)
        return time.monotonic() - t0, kv

    def call(self, cmd, timeout=600):
        self.seq += 1
        tag = "%s-%06d" % (self.nonce, self.seq)
        script = ("timeout 30 sh -c 'echo \"$0\" > \"$1\"' '%s %s' %s/cmd || echo err=fifo\n"
                  "i=0; while [ ! -e %s/res.%s ] && [ $i -lt %d ]; do sleep 0.05; i=$((i+1)); done\n"
                  "cat %s/res.%s 2>/dev/null || echo err=timeout"
                  % (tag, cmd, RUNDIR, RUNDIR, tag, timeout * 20, RUNDIR, tag))
        rc, out = self.box.sh(script, timeout=timeout + 60)
        kv = common.parse_kv(out)
        if "err" in kv:
            rc, tail = self.box.sh("tail -20 %s" % WLOG)
            raise common.Failed("guest 写者应答", "命令 %s 有结果" % cmd,
                                "%s；写者日志末尾：%s" % (kv["err"], tail[-300:]),
                                "guest 内 %s" % PROG)
        return kv

    def write(self, mode, pages, threads, val):
        kv = self.call("W %s %d %d %d %d" % (mode, pages, SEED, threads, val))
        return float(kv["wall_s"])

    def md5(self):
        return self.call("H")["md5"]


# ------------------------------------------------------------------ 宿主侧取数

def kvm_cap_502():
    """L1：宿主的 KVM 认不认 cap 502。返回 True / False / None（打不开 /dev/kvm）。"""
    try:
        fd = os.open("/dev/kvm", os.O_RDWR)
    except OSError:
        return None
    try:
        return fcntl.ioctl(fd, KVM_CHECK_EXTENSION, KVM_CAP_ARM_HW_DIRTY_STATE_TRACK) > 0
    except OSError:
        return False
    finally:
        os.close(fd)


def proc_env(pid, key):
    """进程环境里 key 的值；没有这个变量（或读不到）返回 None。"""
    try:
        raw = open("/proc/%d/environ" % pid, "rb").read().decode("utf8", "replace")
    except (OSError, TypeError):
        return None
    for kv in raw.split("\0"):
        if kv.startswith(key + "="):
            return kv.split("=", 1)[1]
    return None


def sha256_of_exe(pid):
    try:
        exe = os.path.realpath("/proc/%d/exe" % pid)
        h = hashlib.sha256()
        with open("/proc/%d/exe" % pid, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return exe, h.hexdigest()
    except (OSError, TypeError):
        return None, None


def machine_model():
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    got = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            got.setdefault(k.strip(), v.strip())
    parts = [got.get(k) for k in ("Vendor ID", "BIOS Model name", "Model name") if got.get(k)]
    return " / ".join(parts) or "?"


def dmesg_hdbss(tids):
    """dmesg 里属于这台虚机的 HDBSS 行。内核打的是 `kvm [<tid>]: …`，tid 是调
    KVM_ENABLE_CAP 的那个线程，所以拿 Firecracker 进程的全部线程号去认。"""
    try:
        out = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None, []
    mine = []
    for line in out.splitlines():
        if "HDBSS" not in line:
            continue
        m = re.search(r"kvm \[(\d+)\]", line)
        if m and int(m.group(1)) in tids:
            mine.append(line.strip())
    order = None
    for line in mine:
        m = re.search(r"Enable HDBSS success, HDBSS buffer size:\s*(\d+)", line)
        if m:
            order = int(m.group(1))
    return order, mine


def fc_log_hdbss(orch, sandbox_id):
    """Firecracker 的 `HDBSS enabled (buffer order N)` / `HDBSS unavailable …` 那一行。
    它经 orchestrator 的沙箱日志器出来，有没有落到 alloc 日志取决于日志配置，所以
    找不到不算错。只认带本沙箱 id 的行。"""
    lines, _, _ = common.wait_for_log(orch, r"HDBSS (enabled|unavailable)")
    mine = [l for l in lines if sandbox_id in l]
    order = None
    for line in mine:
        m = re.search(r"HDBSS enabled \(buffer order (\d+)\)", line)
        if m:
            order = int(m.group(1))
    return order, [l[:300] for l in mine[-3:]]


class ExitCounter:
    """这台虚机的 KVM 退出数。优先 debugfs（每台虚机一个目录，零依赖、全程累计），
    没有就退回 perf stat，再没有就是 None。"""

    def __init__(self, fcpid):
        self.fcpid = fcpid
        hits = glob.glob("/sys/kernel/debug/kvm/%s-*" % fcpid) if fcpid else []
        self.dir = hits[0] if hits else None
        self.perf = None if self.dir else shutil.which("perf")
        self.source = "debugfs" if self.dir else ("perf" if self.perf and fcpid else None)
        self._p = None
        self._s0 = None

    def _stats(self):
        out = {}
        try:
            for name in os.listdir(self.dir):
                p = os.path.join(self.dir, name)
                if os.path.isfile(p):
                    try:
                        out[name] = int(open(p).read().strip())
                    except (OSError, ValueError):
                        continue
        except OSError:
            pass
        return out

    def start(self):
        if self.dir:
            self._s0 = self._stats()
        elif self.source == "perf":
            self._p = subprocess.Popen(
                [self.perf, "stat", "-e", "kvm:kvm_exit", "-p", str(self.fcpid), "-x", ","],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            time.sleep(0.3)                 # perf 挂上去要一点时间

    def stop(self):
        """返回 (退出数, 其余计数器的增量字典)。"""
        if self.dir:
            s1 = self._stats()
            d = {k: s1[k] - self._s0.get(k, 0) for k in s1 if s1[k] != self._s0.get(k, 0)}
            return d.get("exits", 0) if "exits" in s1 else None, d
        if self._p is not None:
            self._p.send_signal(signal.SIGINT)
            try:
                err = self._p.communicate(timeout=30)[1]
            except subprocess.SubprocessError:
                self._p.kill()
                return None, {}
            finally:
                self._p = None
            for line in err.splitlines():
                if "kvm:kvm_exit" in line:
                    try:
                        return int(line.split(",")[0]), {}
                    except ValueError:
                        return None, {}
        return None, {}


def alloc(path):
    """磁盘实占字节（稀疏文件按 st_blocks 算）。"""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return None


def measure(store, sbx_id, ck_id):
    """这一代的内存产物：差分（或全量镜像）实占多少字节。"""
    out = {"mem_bytes": None, "mem_kind": "-", "files": {}}
    if not store:
        return out
    d = os.path.join(store, sbx_id, ck_id)
    try:
        for name in sorted(os.listdir(d)):
            v = alloc(os.path.join(d, name))
            if v is not None:
                out["files"][name] = v
    except OSError:
        pass
    for name in ("mem_diff", "mem_full"):
        if name in out["files"]:
            out["mem_bytes"], out["mem_kind"] = out["files"][name], name
            break
    return out


# ------------------------------------------------------------------ 核实：后端与 order

def create_sandbox(ctx):
    """建沙箱。--wait-ready 给了秒数就在这段时间里重试（orchestrator 刚重启时 API 节点
    要一两分钟才就绪）。"""
    a = ctx.args
    deadline = time.monotonic() + a.wait_ready
    n = 0
    while True:
        n += 1
        try:
            sbx = common.sandbox_create(a.template, private=not a.public, timeout=7200)
            if n > 1:
                log("  第 %d 次建沙箱成功" % n)
            return sbx
        except Exception as e:              # noqa: BLE001
            if time.monotonic() >= deadline:
                raise common.Failed("建沙箱", "成功", "%s: %s" % (type(e).__name__, e),
                                    "试了 %d 次（--wait-ready %g s）" % (n, a.wait_ready))
            log("  建沙箱还不行（%s），10 s 后再试" % type(e).__name__)
            time.sleep(10)


def verify(ctx, box, orch_log):
    """L1 / L2 + order 核实。返回要写进 JSON 的字典；判不过抛 Failed，前置不满足抛 Unmet。"""
    a = ctx.args
    v = {"cap_502": ctx._cap}
    info = common.fc_get(box.id) or {}
    backend = info.get("dirty_tracking", "?")
    v["backend"] = backend
    v["fc_version"] = info.get("vmm_version", "?")
    log("脏页后端 : %s   [FC %s]   KVM cap 502 = %s" % (backend, v["fc_version"], v["cap_502"]))

    pids = common.fc_processes(box.id)
    fcpid = pids[0] if pids else None
    opid, _ = common.orchestrator_pid_env()
    v["fc_pid"], v["orch_pid"] = fcpid, opid
    v["fc_exe"], v["fc_sha256"] = sha256_of_exe(fcpid)
    v["orch_exe"], v["orch_sha256"] = sha256_of_exe(opid)
    v["env_orch"] = proc_env(opid, ORDER_ENV) if opid else None
    v["env_fc"] = proc_env(fcpid, ORDER_ENV) if fcpid else None
    tids = set()
    if fcpid:
        try:
            tids = {int(t) for t in os.listdir("/proc/%d/task" % fcpid)}
        except OSError:
            tids = {fcpid}
    v["dmesg_order"], v["dmesg_lines"] = dmesg_hdbss(tids)
    v["fclog_order"], v["fclog_lines"] = fc_log_hdbss(orch_log, box.id)

    if a.expect_order is not None:
        want = a.expect_order
    else:
        try:
            want = int(v["env_orch"]) if v["env_orch"] is not None else DEFAULT_ORDER
        except ValueError:
            want = DEFAULT_ORDER            # Firecracker 解析不了也是静默取 1
    v["order"] = want
    log("order    : 期望 %d   orchestrator 环境 %s=%s   Firecracker 环境 %s=%s"
        % (want, ORDER_ENV, v["env_orch"], ORDER_ENV, v["env_fc"]))
    log("           dmesg 的 buffer size = %s   FC 日志的 buffer order = %s"
        % (v["dmesg_order"], v["fclog_order"]))

    if backend != "hdbss":
        if not a.force_non_hdbss:
            raise common.Unmet(
                "脏页后端是 %s，不是 hdbss（本档只量 HDBSS 自己的性能，不做与写保护的对照）" % backend,
                "到有 HDBSS 的机器（鲲鹏 950）上跑；只想验证流程就加 --force-non-hdbss，数字无意义")
        log("!! 非 HDBSS（%s）且带了 --force-non-hdbss：仅流程验证，数字无意义；"
            "order 不会生效，核实降级为只核对进程环境变量" % backend)

    basis = "Firecracker 构建虚机时读 %s（vstate/vm.rs），环境继承自 orchestrator" % ORDER_ENV
    if not fcpid or not opid:
        raise common.Failed("找到进程", "orchestrator 与本沙箱的 firecracker 都在",
                            "orchestrator pid=%s firecracker pid=%s" % (opid, fcpid),
                            "要在跑 orchestrator 的宿主机上、以 root 跑")
    if a.expect_order is not None:
        common.expect(ctx, "orchestrator 环境里的 order", v["env_orch"] == str(want),
                      "%s=%d" % (ORDER_ENV, want), "%s=%s" % (ORDER_ENV, v["env_orch"]), basis)
    common.expect(ctx, "Firecracker 继承了 orchestrator 的 order", v["env_fc"] == v["env_orch"],
                  "两个进程的 %s 相同（%s）" % (ORDER_ENV, v["env_orch"]),
                  "Firecracker 进程里是 %s" % v["env_fc"], basis)
    if backend == "hdbss":
        seen = False
        for src, got in (("dmesg `Enable HDBSS success, HDBSS buffer size:`", v["dmesg_order"]),
                         ("FC 日志 `HDBSS enabled (buffer order N)`", v["fclog_order"])):
            if got is None:
                continue
            seen = True
            common.expect(ctx, "order 生效（%s）" % src, got == want, str(want), str(got), basis)
        if not seen:
            # 两处日志都没抓到（dmesg 被冲掉、FC 日志没落到 alloc 日志）：后端自报 hdbss
            # 说明 enable_hdbss(order) 成功了，而 order 就是环境变量解析出来的那个数。
            common.note(ctx, "order 生效（旁证缺失）", "dmesg 或 FC 日志里有本虚机的 HDBSS 行",
                        "两处都没抓到；按「后端自报 hdbss + 进程环境变量」推断 order=%d" % want,
                        basis)
        v["order_verified_by"] = [s for s, g in (("dmesg", v["dmesg_order"]),
                                                 ("fclog", v["fclog_order"])) if g is not None] \
            or ["env+backend"]
    else:
        v["order_verified_by"] = ["env-only（非 HDBSS，order 不生效）"]
    return v


# ------------------------------------------------------------------ 主体

def plan_sizes(box, want):
    """按 guest 的可用内存去掉装不下的大档，并把 /dev/shm 撑到够放 arena。"""
    rc, out = box.sh("echo ncpu=$(nproc)\n"
                     "echo memtotal_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)\n"
                     "echo memavail_kb=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)\n"
                     "echo shm_mb=$(df -m /dev/shm | awk 'NR==2{print $2}')")
    kv = common.parse_kv(out)
    ncpu = common.as_int(kv.get("ncpu")) or 1
    avail_mb = (common.as_int(kv.get("memavail_kb")) or 0) // 1024
    total_mb = (common.as_int(kv.get("memtotal_kb")) or 0) // 1024
    sizes = sorted(set(want))
    dropped = []
    while sizes and sizes[-1] + GUEST_MARGIN_MB > avail_mb:
        dropped.append(sizes.pop())
    if not sizes:
        raise common.Failed("arena 放得进 guest", "至少一档 ≤ 可用内存 − %d MB" % GUEST_MARGIN_MB,
                            "可用 %d MB，要的档 %s" % (avail_mb, sorted(want)), "guest /proc/meminfo")
    if dropped:
        log("  !! guest 可用内存 %d MB（总 %d MB），去掉装不下的档：%s MB"
            % (avail_mb, total_mb, ",".join(str(x) for x in sorted(dropped))))
    arena_mb = sizes[-1]
    if (common.as_int(kv.get("shm_mb")) or 0) < arena_mb + 64:
        box.run("mount -o remount,size=%dm /dev/shm" % (arena_mb + 64))
    return sizes, dropped, arena_mb, {"ncpu": ncpu, "mem_total_mb": total_mb,
                                      "mem_avail_mb": avail_mb}


def one_iter(ctx, st, combo, idx, jsonl):
    size_mb, mode, threads = combo
    box, gw, base_id = st["box"], st["gw"], st["base"]
    pages = size_mb * 256
    st["val"] = st["val"] % 250 + 1
    val, val2 = st["val"], st["val"] % 250 + 1
    name = "%s-%d-t%d-%d" % (mode, size_mb, threads, idx)
    rec = {"kind": "iter", "order": st["verify"]["order"], "size_mb": size_mb, "mode": mode,
           "threads": threads, "idx": idx, "pages": pages, "sandbox": box.id, "t": time.time()}

    if pages:
        ec = ExitCounter(st["verify"]["fc_pid"])
        ec.start()
        rec["cold_s"] = gw.write(mode, pages, threads, val)
        rec["exits_cold"], rec["kvm_cold"] = ec.stop()
        ec.start()
        rec["warm_s"] = gw.write(mode, pages, threads, val)
        rec["exits_warm"], _ = ec.stop()
        rec["exits_source"] = ec.source
    rec["md5"] = gw.md5()

    c = box.create(name, record_scene=False)
    rec["create"] = c
    if not c.get("ok"):
        jsonl(rec)
        raise common.Failed("checkpoint", "成功", str(c.get("err")), "组合 %s 第 %d 次" % (name, idx))
    rec["artifact"] = measure(ctx.store, box.id, c["id"])

    if pages:
        gw.write(mode, pages, threads, val2)         # 把这批页改写掉，restore 才有东西可回
    r = box.restore(c["id"], verify=False)
    rec["restore"] = r
    if not r.get("ok"):
        jsonl(rec)
        raise common.Failed("restore 回刚拍的 checkpoint", "成功", str(r.get("err")),
                            "组合 %s 第 %d 次" % (name, idx))
    rec["md5_after"] = gw.md5()
    rec["md5_ok"] = rec["md5_after"] == rec["md5"]
    if not rec["md5_ok"]:
        st["md5_bad"] += 1
        log("  !! %s 第 %d 次：restore 后 md5 不一致（%s → %s）"
            % (name, idx, rec["md5"], rec["md5_after"]))

    rb = box.restore(base_id, verify=False)
    rec["restore_base"] = rb
    if not rb.get("ok"):
        jsonl(rec)
        raise common.Failed("restore 回基线", "成功", str(rb.get("err")),
                            "组合 %s 第 %d 次" % (name, idx))
    rec["delete"] = box.delete(c["id"])
    jsonl(rec)
    return rec


def _ms(xs):
    return common.p50([x * 1000.0 for x in xs if x is not None])


def aggregate(recs):
    """把原始迭代记录按（脏页量, 写法, 线程）收成一行。"""
    by = {}
    for r in recs:
        if r.get("kind") != "iter":
            continue
        by.setdefault((r["size_mb"], r["mode"], r["threads"]), []).append(r)
    base_diff = None
    if (0, "-", 0) in by:
        base_diff = common.p50([(r.get("artifact") or {}).get("mem_bytes") for r in by[(0, "-", 0)]])
    rows = []
    for key in sorted(by):
        rs = by[key]
        size_mb, mode, threads = key
        pages = size_mb * 256
        ph = lambda r, side, k: (r.get(side, {}).get("phases") or {}).get(k)   # noqa: E731
        row = {"size_mb": size_mb, "mode": mode, "threads": threads, "pages": pages, "n": len(rs),
               "cold_ms": _ms([r.get("cold_s") for r in rs]),
               "warm_ms": _ms([r.get("warm_s") for r in rs]),
               "ck_wall_ms": _ms([r["create"].get("wall_s") for r in rs]),
               "ck_frozen_ms": common.p50([ph(r, "create", "frozen") for r in rs]),
               "ck_snapshot_ms": common.p50([ph(r, "create", "snapshot") for r in rs]),
               "ck_total_ms": common.p50([ph(r, "create", "total") for r in rs]),
               "rs_wall_ms": _ms([r["restore"].get("wall_s") for r in rs]),
               "rs_frozen_ms": common.p50([ph(r, "restore", "frozen") for r in rs]),
               "diff_bytes": common.p50([(r.get("artifact") or {}).get("mem_bytes") for r in rs]),
               "diff_base_bytes": base_diff,
               "expect_bytes": pages * PAGE,
               "mem_modes": sorted({str(r["create"].get("mem_mode")) for r in rs}),
               "md5_ok": sum(1 for r in rs if r.get("md5_ok")),
               "exits_source": next((r.get("exits_source") for r in rs if r.get("exits_source")), None)}
        if pages:
            row["track_ns_per_page"] = common.p50(
                [(r["cold_s"] - r["warm_s"]) * 1e9 / pages for r in rs
                 if r.get("cold_s") is not None and r.get("warm_s") is not None])
            row["cold_over_warm"] = common.p50(
                [r["cold_s"] / r["warm_s"] for r in rs if r.get("cold_s") and r.get("warm_s")])
            row["exits_per_page"] = common.p50(
                [r["exits_cold"] / float(pages) for r in rs if r.get("exits_cold") is not None])
            row["exits_warm_per_page"] = common.p50(
                [r["exits_warm"] / float(pages) for r in rs if r.get("exits_warm") is not None])
        rows.append(row)
    return rows


def _f(x, nd=1):
    return "-" if x is None else ("%." + str(nd) + "f") % x


def _join(xs):
    return "、".join(str(x) for x in xs or [])


def _mb(x):
    return "-" if x is None else "%.1f" % (x / MB)


TABLE_HEAD = ["order", "脏页量 MB", "写法", "线程", "n", "冷写 ms", "热写 ms", "跟踪 ns/页",
              "kvm_exit/页", "checkpoint 墙钟 ms", "服务端 frozen ms", "差分 MB", "应有 MB",
              "restore 墙钟 ms", "md5 一致"]


def table_cells(order, row):
    return [str(order), str(row["size_mb"]), row["mode"], str(row["threads"] or "-"), str(row["n"]),
            _f(row.get("cold_ms"), 2), _f(row.get("warm_ms"), 2), _f(row.get("track_ns_per_page"), 0),
            _f(row.get("exits_per_page"), 4), _f(row.get("ck_wall_ms")), _f(row.get("ck_frozen_ms")),
            _mb(row.get("diff_bytes")), _mb(row.get("expect_bytes")), _f(row.get("rs_wall_ms")),
            "%d/%d" % (row["md5_ok"], row["n"])]


def l3_of(rows):
    """L3 数据面证据：取 seq、1 线程里最大的那一档。"""
    cand = [r for r in rows if r["mode"] == "seq" and r["threads"] == 1 and r["pages"]]
    if not cand:
        return None
    r = max(cand, key=lambda x: x["size_mb"])
    return {"size_mb": r["size_mb"], "pages": r["pages"], "n": r["n"], "cold_ms": r.get("cold_ms"),
            "warm_ms": r.get("warm_ms"), "cold_over_warm": r.get("cold_over_warm"),
            "exits_per_page": r.get("exits_per_page"),
            "exits_warm_per_page": r.get("exits_warm_per_page"),
            "track_ns_per_page": r.get("track_ns_per_page"), "exits_source": r.get("exits_source")}


def run(ctx):
    a = ctx.args
    outdir = a.outdir or os.getcwd()
    os.makedirs(outdir, exist_ok=True)
    jsonl_path = os.path.join(outdir, "hdbss-raw-%s.jsonl" % common.now_tag())
    fh = open(jsonl_path, "a", buffering=1)
    ctx.results["meta"]["jsonl"] = jsonl_path
    log("原始数据：%s" % jsonl_path)

    def jsonl(rec):
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        ctx.results["ops"].append(rec)

    deadline = time.monotonic() + a.deadline_min * 60.0

    # ---- L1：不建沙箱就能问。没有 cap 502 又没带 --force-non-hdbss，到这里就结束。
    ctx._cap = kvm_cap_502()
    log("== L1 能力：KVM cap %d = %s ==" % (KVM_CAP_ARM_HW_DIRTY_STATE_TRACK, ctx._cap))
    if ctx._cap is not True and not a.force_non_hdbss:
        log("脏页后端 : 未问（宿主 KVM 不认 cap %d，没有建沙箱去问 Firecracker）"
            % KVM_CAP_ARM_HW_DIRTY_STATE_TRACK)
        raise common.Unmet(
            "宿主 KVM 不认 cap %d（%s），这台机器没有 HDBSS"
            % (KVM_CAP_ARM_HW_DIRTY_STATE_TRACK, ctx._cap),
            "到有 HDBSS 的机器（鲲鹏 950）上跑；只想验证流程就加 --force-non-hdbss，数字无意义")

    # ---- 建沙箱，L2 + order 核实
    orch_log = common.OrchLog()
    sbx = create_sandbox(ctx)
    box = common.Box(sbx, ctx.store, "hdbss")
    common._ALL_BOXES.append(box)
    log("沙箱 %s" % box.id)
    log("== L2 自报 + order 核实 ==")
    v = verify(ctx, box, orch_log)

    sizes, dropped, arena_mb, guest = plan_sizes(box, a._sizes)
    threads = [t for t in a._threads if t <= guest["ncpu"]]
    if len(threads) != len(a._threads):
        log("  !! guest 只有 %d 个 vCPU，去掉线程数 %s"
            % (guest["ncpu"], [t for t in a._threads if t > guest["ncpu"]]))
    combos = [(0, "-", 0)] + [(s, m, t) for s in sizes for m in a._modes for t in threads]

    labels = {"host": os.uname().nodename, "machine": machine_model(), "arch": os.uname().machine,
              "kernel": os.uname().release, "backend": v["backend"], "cap_502": v["cap_502"],
              "order": v["order"], "template": a.template, "guest_vcpu": guest["ncpu"],
              "guest_mem_mb": guest["mem_total_mb"], "fc_version": v["fc_version"],
              "fc_exe": v["fc_exe"], "fc_sha256": v["fc_sha256"],
              "orch_exe": v["orch_exe"], "orch_sha256": v["orch_sha256"],
              "store": ctx.store, "store_fstype": ctx.fstype,
              "non_hdbss_forced": v["backend"] != "hdbss",
              "args": " ".join(sys.argv[1:]), "iters": a.iters, "quick": a.quick,
              "evidence_only": a.evidence, "sizes": sizes, "sizes_dropped": sorted(dropped),
              "modes": a._modes, "threads": threads, "arena_mb": arena_mb, "seed": SEED}
    log("条件标签 : %s · %s · 内核 %s · 模板 %s（guest %d vCPU / %d MB）"
        % (labels["host"], labels["machine"], labels["kernel"], a.template,
           guest["ncpu"], guest["mem_total_mb"]))
    log("           firecracker %s  sha256 %s" % (v["fc_exe"], (v["fc_sha256"] or "?")[:16]))
    log("           orchestrator %s  sha256 %s" % (v["orch_exe"], (v["orch_sha256"] or "?")[:16]))

    # ---- guest 写者 + 基线
    gw = GuestWriter(box, arena_mb, max(threads))
    warm_s, _ = gw.start()
    log("guest 写者就绪：arena %d MB，%d 个工作进程，%.1f s" % (arena_mb, max(threads), warm_s))
    base = box.create("base", record_scene=False)
    if not base.get("ok"):
        raise common.Failed("首个全量 checkpoint", "成功", str(base.get("err")), "基线")
    base_md5 = gw.md5()
    art = measure(ctx.store, box.id, base["id"])
    jsonl({"kind": "full", "sandbox": box.id, "create": base, "artifact": art, "md5": base_md5,
           "t": time.time()})
    log("基线 checkpoint %s：mem_mode=%s 墙钟 %.2f s 产物 %s %s MB"
        % (base["id"], base.get("mem_mode"), base.get("wall_s", 0.0), art["mem_kind"],
           _mb(art["mem_bytes"])))

    st = {"box": box, "gw": gw, "base": base["id"], "verify": v, "val": 0, "md5_bad": 0}
    done = 0
    stopped = False
    ctx.stage(CASE, combos=len(combos), iters=a.iters)
    for it in range(1, a.iters + 1):
        log("-- 第 %d / %d 遍（%d 个组合）--" % (it, a.iters, len(combos)))
        for combo in combos:
            if time.monotonic() > deadline:
                log("  !! 到总时限（%g 分钟），停在第 %d 遍 %s" % (a.deadline_min, it, combo))
                stopped = True
                break
            rec = one_iter(ctx, st, combo, it, jsonl)
            done += 1
            if combo[0]:
                log("   %4d MB %-4s t%d  冷 %7.2f ms 热 %7.2f ms  exit/页 %s  ck %7.1f ms  差分 %6s MB"
                    "  rs %6.1f ms  md5 %s"
                    % (combo[0], combo[1], combo[2], rec["cold_s"] * 1000, rec["warm_s"] * 1000,
                       "-" if rec.get("exits_cold") is None
                       else "%.4f" % (rec["exits_cold"] / float(rec["pages"])),
                       rec["create"].get("wall_s", 0) * 1000, _mb(rec["artifact"]["mem_bytes"]),
                       rec["restore"].get("wall_s", 0) * 1000, "OK" if rec["md5_ok"] else "不一致!"))
        if stopped:
            break
        # 每遍收尾：回到基线之后 arena 应当与基线那一刻一模一样
        end_md5 = gw.md5()
        if end_md5 != base_md5:
            st["md5_bad"] += 1
            log("  !! 第 %d 遍结束时 arena 的 md5 与基线不一致（%s ≠ %s）" % (it, end_md5, base_md5))
    fh.close()

    box.delete(base["id"])
    box.kill()
    common.forget(box)
    common.wait_until(lambda: not common.fc_processes(box.id), timeout=60, interval=2.0)

    # ---- 汇总
    rows = aggregate(ctx.results["ops"])
    l3 = l3_of(rows)
    non_incr = sum(1 for r in ctx.results["ops"] if r.get("kind") == "iter"
                   and r["create"].get("mem_mode") != "incremental")
    ctx.results["summary"][CASE] = {"labels": labels, "verify": v, "rows": rows, "l3": l3,
                                    "iterations": done, "md5_bad": st["md5_bad"],
                                    "non_incremental": non_incr, "stopped_early": stopped}
    log("")
    log("== 汇总（order %d，脏页后端 %s，各列为 p50）==" % (v["order"], v["backend"]))
    common.table(TABLE_HEAD, [table_cells(v["order"], r) for r in rows])
    log("")
    log("== L3 数据面 ==")
    if l3:
        log("  顺序写 %d MB（%d 页，1 线程，n=%d）：冷 %s ms / 热 %s ms = %s；冷写每页 %s 次 KVM 退出"
            "（来源 %s），热写每页 %s 次；跟踪开销 %s ns/页"
            % (l3["size_mb"], l3["pages"], l3["n"], _f(l3["cold_ms"], 2), _f(l3["warm_ms"], 2),
               _f(l3["cold_over_warm"], 2), _f(l3["exits_per_page"], 4), l3["exits_source"],
               _f(l3["exits_warm_per_page"], 4), _f(l3["track_ns_per_page"], 0)))
        log("  怎么读：软件写保护下每个干净页第一次写陷出一次，冷写每页约 1 次退出、冷明显慢于热；")
        log("          HDBSS 下由 CPU 记脏页，冷写每页退出数远小于 1（约等于「页数 ÷ buffer 容量」"
            "那么多次倒 buffer），冷热接近。")
        common.note(ctx, "L3 数据面", "hdbss：exit/页 ≪ 1、冷/热 ≈ 1；kvm-wp：exit/页 ≈ 1、冷/热 ≫ 1",
                    "后端 %s：exit/页 %s，冷/热 %s" % (v["backend"], _f(l3["exits_per_page"], 4),
                                                    _f(l3["cold_over_warm"], 2)),
                    "只记录不判定")
    if non_incr:
        common.note(ctx, "checkpoint 全是增量", "mem_mode=incremental", "%d 次不是增量" % non_incr,
                    "脏页跟踪关着时每次都是全量；只记录")
    if stopped:
        common.note(ctx, "跑完全部组合", "%d 遍 × %d 个组合" % (a.iters, len(combos)),
                    "到总时限提前结束，完成 %d 次迭代" % done, "--deadline-min")
    log("")
    log("关键: 脏页后端=%s order=%d（核实：%s） 组合 %d 迭代 %d md5 不一致 %d 非增量 %d%s%s"
        % (v["backend"], v["order"], "+".join(v["order_verified_by"]), len(combos), done,
           st["md5_bad"], non_incr, "  [到时限未跑完]" if stopped else "",
           "  [非 HDBSS，仅流程验证，数字无意义]" if v["backend"] != "hdbss" else ""))
    common.expect(ctx, "restore 后 md5 全部一致", st["md5_bad"] == 0, "0 次不一致",
                  "%d 次不一致" % st["md5_bad"], "%d 次迭代，每次 restore 回刚拍的 checkpoint 后比 arena 的 md5" % done)


# ------------------------------------------------------------------ 多个 order 拼表

def summarize(paths, md_path, json_path):
    runs = []
    for p in paths:
        try:
            res = json.load(open(p))
        except (OSError, ValueError) as e:
            log("!! 跳过 %s：%s" % (p, e))
            continue
        s = (res.get("summary") or {}).get(CASE)
        if not s:
            log("!! 跳过 %s：里面没有 %s 的汇总（那一轮没跑到汇总就结束了）" % (p, CASE))
            continue
        s["file"] = os.path.basename(p)
        s["failure"] = (res.get("meta") or {}).get("failure")
        runs.append(s)
    if not runs:
        log("没有可汇总的结果")
        return 3
    runs.sort(key=lambda s: (s["labels"].get("evidence_only", False), s["labels"]["order"]))
    bench = [s for s in runs if not s["labels"].get("evidence_only")] or runs
    lab = bench[0]["labels"]
    orders = [s["labels"]["order"] for s in bench]
    forced = any(s["labels"].get("non_hdbss_forced") for s in runs)

    L = []
    if forced:
        L += ["> **非 HDBSS，仅流程验证，数字无意义。** 脏页后端是 `%s`，下表只说明脚本跑得通。"
              % lab["backend"], ""]
    L += ["### 条件标签", "", "| 项 | 值 |", "|---|---|",
          "| 机型 | %s · %s · %s |" % (lab["host"], lab["machine"], lab["arch"]),
          "| 内核 | %s |" % lab["kernel"],
          "| 脏页后端 | `%s`（KVM cap 502 = %s） |" % (lab["backend"], lab["cap_502"]),
          "| order | %s |" % "、".join(str(o) for o in orders),
          "| 模板规格 | `%s`：guest %s vCPU / %s MB |" % (lab["template"], lab["guest_vcpu"],
                                                      lab["guest_mem_mb"]),
          "| firecracker | `%s` · %s · sha256 `%s` |" % (lab["fc_exe"], lab["fc_version"],
                                                        lab["fc_sha256"]),
          "| orchestrator | `%s` · sha256 `%s` |" % (lab["orch_exe"], lab["orch_sha256"]),
          "| 产物盘 | `%s`（%s） |" % (lab["store"], lab["store_fstype"]),
          "| 脚本参数 | `%s` |" % lab["args"],
          "| 计划 | 脏页量 %s MB（装不下去掉的：%s）× 写法 %s × 线程 %s，每个组合 %s 遍，arena %s MB |"
          % (_join(lab["sizes"]), _join(lab["sizes_dropped"]) or "无", _join(lab["modes"]),
             _join(lab["threads"]), lab["iters"], lab["arena_mb"]),
          ""]
    L += ["### order × 脏页量 × 写入方式", "",
          "各列为 p50。冷写 = 这批页自上次 checkpoint / restore 以来第一次被写；热写 = 原样再写一遍；"
          "跟踪 ns/页 =（冷 − 热）÷ 页数；kvm_exit/页 是冷写期间的；脏页量 0 那一行是不写任何页的基底。",
          "", "| " + " | ".join(TABLE_HEAD) + " |", "|" + "---|" * len(TABLE_HEAD)]
    keys = sorted({(r["size_mb"], r["mode"], r["threads"]) for s in bench for r in s["rows"]})
    flat = []
    for key in keys:
        for s in bench:
            for r in s["rows"]:
                if (r["size_mb"], r["mode"], r["threads"]) == key:
                    L.append("| " + " | ".join(table_cells(s["labels"]["order"], r)) + " |")
                    flat.append(dict(r, order=s["labels"]["order"]))
    L += ["", "### 证据与 order 核实", "",
          "| order | 来源文件 | 后端 | orchestrator 环境 | Firecracker 环境 | dmesg buffer size | "
          "FC 日志 buffer order | 核实依据 | L3：冷/热 | L3：kvm_exit/页 | md5 不一致 | 备注 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in runs:
        v, l3 = s["verify"], s.get("l3") or {}
        remark = []
        if s["labels"].get("evidence_only"):
            remark.append("证据项")
        if s.get("stopped_early"):
            remark.append("到时限未跑完")
        if s.get("non_incremental"):
            remark.append("%d 次非增量" % s["non_incremental"])
        if s.get("failure"):
            remark.append("有断言失败")
        L.append("| %s | %s | `%s` | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                 % (v["order"], s["file"], v["backend"],
                    "未设" if v["env_orch"] is None else v["env_orch"],
                    "未设" if v["env_fc"] is None else v["env_fc"],
                    "没抓到" if v["dmesg_order"] is None else v["dmesg_order"],
                    "没抓到" if v["fclog_order"] is None else v["fclog_order"],
                    "+".join(v.get("order_verified_by") or []),
                    _f(l3.get("cold_over_warm"), 2), _f(l3.get("exits_per_page"), 4),
                    s.get("md5_bad"), "；".join(remark) or "-"))
    L.append("")
    text = "\n".join(L)
    if md_path:
        with open(md_path, "w") as f:
            f.write(text)
    if json_path:
        with open(json_path, "w") as f:
            json.dump({"labels": lab, "orders": orders, "non_hdbss_forced": forced,
                       "table": flat, "runs": runs}, f, ensure_ascii=False, indent=1)
    print(text)
    log("")
    log("关键: 汇总 %d 份结果，order %s，对比表 %d 行%s"
        % (len(runs), ",".join(str(o) for o in orders), len(flat),
           "  [非 HDBSS，仅流程验证，数字无意义]" if forced else ""))
    return 0


def _ints(text, what):
    try:
        out = [int(x) for x in text.split(",") if x.strip() != ""]
    except ValueError:
        sys.exit("%s 要的是逗号分隔的整数，给的是 %r" % (what, text))
    if not out or min(out) <= 0:
        sys.exit("%s 至少一个正整数，给的是 %r" % (what, text))
    return out


def main():
    ap = argparse.ArgumentParser(description="HDBSS 专项：证据 + 写密集基准（只测当前 order）")
    common.add_common_args(ap)
    ap.add_argument("--outdir", default=None, help="产物落点（默认当前目录）")
    ap.add_argument("--sizes", default=None,
                    help="脏页量档位（MB，逗号分隔），默认 %s；guest 装不下的大档自动去掉" % DEFAULT_SIZES)
    ap.add_argument("--modes", default="seq,rand", help="写法：seq（连续）、rand（随机散开），默认两种都跑")
    ap.add_argument("--threads", default="1,2", help="写者线程数（每个绑一个 vCPU），默认 1,2")
    ap.add_argument("--iters", type=int, default=None, help="每个组合跑几遍（默认 3；--quick 时 1）")
    ap.add_argument("--quick", action="store_true",
                    help="短试跑：档位 %s、每个组合 1 遍，只验证脚本跑得通" % QUICK_SIZES)
    ap.add_argument("--evidence", action="store_true",
                    help="只做证据：L1 / L2 / order 核实 + 顺序写 %d MB 的冷热对比（L3）" % EVIDENCE_SIZE)
    ap.add_argument("--expect-order", type=int, default=None,
                    help="期望的 order（run.sh 扫描时给）；不给就取 orchestrator 环境里的值，没设按 %d"
                         % DEFAULT_ORDER)
    ap.add_argument("--force-non-hdbss", action="store_true",
                    help="脏页后端不是 hdbss 也照跑（仅流程验证，数字无意义）；默认以退出码 3 结束")
    ap.add_argument("--wait-ready", type=float, default=0.0,
                    help="建沙箱失败时在这么多秒内重试（orchestrator 刚重启后用），默认不重试")
    ap.add_argument("--deadline-min", type=float, default=14.0, help="总时限（分钟），到点停并标注未跑完")
    ap.add_argument("--summarize", nargs="+", metavar="JSON",
                    help="不跑测试：把若干份本脚本的结果 JSON 拼成一张 order 对比表")
    ap.add_argument("--md", default=None, help="--summarize 的 Markdown 输出路径")
    ap.add_argument("--json", default=None, help="--summarize 的 JSON 输出路径")
    args = ap.parse_args()

    if args.summarize:
        return summarize(args.summarize, args.md, args.json)

    if args.evidence:
        args._sizes, args._modes, args._threads = [64 if args.quick else EVIDENCE_SIZE], ["seq"], [1]
        args.iters = args.iters or 3
    else:
        args._sizes = _ints(args.sizes or (QUICK_SIZES if args.quick else DEFAULT_SIZES), "--sizes")
        args._modes = [m for m in args.modes.split(",") if m]
        args._threads = _ints(args.threads, "--threads")
        args.iters = args.iters or (1 if args.quick else 3)
    bad = [m for m in args._modes if m not in ("seq", "rand")]
    if bad or not args._modes:
        sys.exit("--modes 只认 seq / rand，给的是 %r" % args.modes)
    args.no_probe = True                 # 后端在本脚本自己那个沙箱上问，不另起探测沙箱
    if not args.out:
        args.out = os.path.join(args.outdir or os.getcwd(), "hdbss-bench-%s.json" % common.now_tag())

    shim = type("M", (), {"run": staticmethod(run)})
    return common.run_case(CASE, shim, args)


if __name__ == "__main__":
    sys.exit(main())
