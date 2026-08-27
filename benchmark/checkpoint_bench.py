#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
checkpoint / restore 耗时基准：内存和文件一起改，增量的代价随「改了多少」怎么涨。

负载按真实用法造，不做人为隔离：
  · 每档总改动量按 --split（默认 3:1）拆成内存和文件两份 —— 沙箱里跑代码的
    真实负载通常改内存远多于改文件；
  · 内存那份写 /dev/shm（tmpfs，模拟改堆、改变量这类纯内存改动）；
  · 文件那份就是普通缓冲写 + sync —— 不用 O_DIRECT。真实用户怎么写我们就怎么
    写：数据先进 guest 页缓存再刷到虚拟盘，所以一份文件写会**同时**出现在内存
    增量（页缓存那些页）和文件增量（落盘的块）里。这不是测量误差，是页缓存的
    本性，实测两列会把它如实摆出来。

同一套档位拍两遍，唯一区别是什么时候拍：
  ① 写完立刻拍 —— 最坏情况上界。刚写的文件数据正被宿主机内核往盘上刷，会和
     写 mem_diff 抢磁盘；NBD 管道里还有在途数据要排空。这两笔是「写完立刻拍」
     这个时机的成本，不是快照机制的成本。
  ② 写完先让宿主机把脏页冲干净（sync + 歇 1 秒）再拍 —— 贴近「写完跑了一会儿
     才拍」的日常口径。
  ① 减 ② 就是时机成本本身。

每档的「实测内存 / 实测文件」直接量服务端产物：这一代 mem_diff 文件的磁盘实占
（稀疏文件，实占 = 这一代的内存脏页）和新封层文件的实占（= 这一代落进写层的
块）。只有在宿主机上跑才量得到；每代存完整镜像的方案（XFS 对）没有按代的
mem_diff，那一格就空着 —— 空着比给一个错的数强。

三段接在同一条链上（g0 全量 → 预热 → ①段 → ②段），最后从链尾逐级退回，
restore 的档位曲线一并拿到，每跳验代号标记。

每档只测一次，单次会抖 —— 节点刚重启后的第一轮尤其明显。要分位数和分布，用
test-950/bench-ckpt.py。功能正确性在 checkpoint_verify.py —— 两个脚本分开跑。

依赖（与本目录其它脚本一致）:
    pip install e2b==2.20.0 python-dotenv
    python /opt/e2b-infra/patch_e2b.py
    python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py   # checkpoint/restore 的 SDK 覆盖层

环境变量（可放在当前目录 .env 里，用 sync-env.sh 同步）:
    E2B_API_KEY / E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL

用法:
    python checkpoint_bench.py
    python checkpoint_bench.py --tiers 0,16,32,64,128,256 --split 3:1
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import glob
import json
import os
import socket
import sys
import tempfile
import time
import unicodedata

from e2b import Sandbox

DEFAULT_TEMPLATE_ID = "base"
DEFAULT_TIERS = "0,16,32,64,128,256"
DEFAULT_SPLIT = "3:1"

BENCH_DIR = "/bench-root"        # 文件那份写这里（根文件系统上）


# ---------------------------------------------------------------- 终端排版

def width(s):
    """终端里的显示宽度。中文和全角标点占两列，%-18s 按字符数补空格会错位，
    表格就歪了 —— 这些表是给人看的，歪了就白做。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def lpad(s, n):
    return s + " " * max(0, n - width(s))


def rpad(s, n):
    return " " * max(0, n - width(s)) + s


def fmt_mb(nbytes):
    return "-" if nbytes is None else "%.1f MB" % (nbytes / 1048576.0)


def fmt_ms(x):
    return "-" if x is None else "%.1f ms" % x


# ---------------------------------------------- 宿主机侧探测（本次跑在什么上）
#
# 这两个函数和 checkpoint_verify.py 里的是同一份。宁可重复也不 import：本目录的
# 脚本是一个个单独拷到目标机上跑的，跨文件依赖会在版本对不齐时**静默降级成
# "未知"**，而不是报错——已经踩过一次了。

def fc_get(sandbox_id, path="/"):
    """向这个沙箱的 Firecracker API 套接字发一个 GET。

    只有在**宿主机上**跑本脚本时才拿得到（套接字是本地文件）。从别的机器连
    过来时会返回 None，那时脏页后端就只能标成"未知"。"""
    hits = glob.glob(os.path.join(tempfile.gettempdir(), "fc-%s-*.sock" % sandbox_id))
    if not hits:
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(hits[0])
        s.sendall(("GET %s HTTP/1.1\r\nHost: localhost\r\n"
                   "Accept: application/json\r\n\r\n" % path).encode())
        # Firecracker 走 keep-alive 不主动关连接，所以按 Content-Length 读够就停，
        # 等 EOF 会一直挂着。
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


def checkpoint_store():
    """checkpoint 产物落在哪个目录、那个目录是什么文件系统。

    路径由 orchestrator 进程实际拿到的 ORCHESTRATOR_BASE_PATH 决定，所以直接读它的
    environ —— 读配置文件会告诉你"本该是什么"，读进程才知道"实际是什么"，这两者
    分岔过不止一次（改了 hcl 模板但 nomad 跑的是渲染副本）。

    同样只有在宿主机上跑才拿得到。"""
    env = {}
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
        env = dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
        break
    if not env:
        return None, "?"
    store = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"),
                         "build", "checkpoints")
    # 取覆盖这个路径的最长挂载点，就是它所在的文件系统。
    best, fstype = "", "?"
    try:
        for line in open("/proc/mounts"):
            f = line.split()
            if len(f) < 3:
                continue
            mnt = f[1]
            if (store == mnt or store.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                best, fstype = mnt, f[2]
    except OSError:
        pass
    return store, fstype


BACKEND_NOTE = {
    "hdbss": ("✓ 硬件标脏（HDBSS）",
              "CPU 自己把脏页记进缓冲区，checkpoint 不用为每个干净页的第一次写陷出虚机。"),
    "kvm-wp": ("软件写保护（KVM write-protect）",
               "没有硬件标脏，只能把干净页全设成只读、靠写异常来记 —— 每个页第一次被写都要\n"
               "    陷出一次虚机。下面 checkpoint 的数字含这笔开销；换到有 HDBSS 的机器上会更快。"),
    "off": ("✗ 没开脏页跟踪",
            "增量快照拿不到脏页表，每次 checkpoint 都会退化成整份内存拷贝。查 FC_TRACK_DIRTY_PAGES。"),
}


def report_backend(sandbox_id):
    info = fc_get(sandbox_id)
    if info is None:
        print("  脏页后端 : 未知（拿不到 Firecracker 套接字——本脚本没跑在宿主机上？）")
        return "?"
    backend = info.get("dirty_tracking", "?")
    title, why = BACKEND_NOTE.get(backend, (backend, "本机 Firecracker 还没有这个字段。"))
    print("  脏页后端 : %s   [FC %s]" % (title, info.get("vmm_version", "?")))
    print("    %s" % why)
    return backend


# ---------------------------------------------------------------------- 本体

class Bench:
    """一个沙箱，一条检查点链：g0（全量树根）→ 预热 → 两遍档位 → 逐级退回。"""

    def __init__(self, sbx, store, split):
        self.sbx = sbx
        self.store = store       # checkpoint 产物目录（宿主机上才有，读分段/实测用）
        self.split = split       # (内存份, 文件份)，如 (3, 1)
        self.chain = []          # [(代名, ck_id, 遍名, 这一代之前弄脏的总 MB)]
        self.bad = []            # 没落到目标代的跳

    def run(self, cmd, timeout=300):
        """一律以 root 跑：文件那份要写根目录。"""
        return self.sbx.commands.run(cmd, user="root", timeout=timeout).stdout

    def kv(self, out):
        d = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
        return d

    def split_mb(self, mb):
        """一档的总量拆成（内存份, 文件份）。"""
        a, b = self.split
        m = mb * a // (a + b)
        return m, mb - m

    # ------------------------------------------------------------ 弄脏与标记

    def dirty(self, mb):
        """把总量 mb 兆按比例弄脏。conv=notrunc：只覆写前 N 兆，不动文件大小 ——
        体积在预热那一代就定死了，这里量的才是"改了多少"而不是"分配了多少"。
        文件那份是普通缓冲写 + sync：真实用户怎么写就怎么写。"""
        m, f = self.split_mb(mb)
        if m:
            self.run("dd if=/dev/urandom of=/dev/shm/sweep bs=1M count=%d conv=notrunc "
                     "2>/dev/null" % m, timeout=900)
        if f:
            self.run("dd if=/dev/urandom of=%s/fsblob bs=1M count=%d conv=notrunc "
                     "2>/dev/null; sync" % (BENCH_DIR, f), timeout=1800)

    def warm(self, maxmb):
        """把两个文件一次撑到各自的最大档位。这一代的成本（分配 + 首次写满）
        不计入任何档位。"""
        m, f = self.split_mb(maxmb)
        self.run("rm -f /dev/shm/sweep; dd if=/dev/urandom of=/dev/shm/sweep "
                 "bs=1M count=%d 2>/dev/null" % max(m, 1), timeout=900)
        self.run("dd if=/dev/urandom of=%s/fsblob bs=1M count=%d 2>/dev/null; sync"
                 % (BENCH_DIR, max(f, 1)), timeout=1800)

    def settle(self):
        """模拟「写完跑了一会儿才拍」：guest 里的 sync 写入命令里已经做了，这里把
        宿主机侧也冲干净。内核平时按自己的节奏刷（脏页过期约 30 秒），干等太慢，
        主动 sync 等价于"已经过去了一会儿"。sync 只刷脏页，无破坏性。"""
        if self.store:
            os.system("sync")
            time.sleep(1.0)
        else:
            time.sleep(5.0)      # 不在宿主机上，只能干等内核自己刷一轮

    def mark(self, gen):
        """一个页大小的代号，用来在回退时确认真的落到了那一代。"""
        self.run("echo %s > /dev/shm/gen" % gen, timeout=60)

    def gen_now(self):
        return self.run("cat /dev/shm/gen 2>/dev/null || echo MISSING", timeout=60).strip()

    # ------------------------------------------------------------ 实测增量

    def alloc(self, path):
        """文件的磁盘实占（字节）。稀疏文件按 st_blocks 算 —— 表观大小是整个
        地址空间，实占才是真写下去的量。"""
        try:
            return os.stat(path).st_blocks * 512
        except OSError:
            return None

    def layers(self):
        try:
            return set(os.listdir(os.path.join(self.store, self.sbx.sandbox_id, "layers")))
        except OSError:
            return set()

    def measure(self, ck_id, before_layers):
        """这一代实际写下了多少：mem_diff 的实占 = 这一代的内存脏页；layers/ 里
        新出现的封层文件的实占 = 这一代落进写层的块。每代存完整镜像的方案没有
        按代的 mem_diff，内存那格就空着 —— 空着比给一个错的数强。"""
        if not self.store:
            return None, None
        mem = self.alloc(os.path.join(self.store, self.sbx.sandbox_id, ck_id, "mem_diff"))
        disk = 0
        for name in self.layers() - before_layers:
            disk += self.alloc(os.path.join(self.store, self.sbx.sandbox_id,
                                            "layers", name)) or 0
        return mem, disk

    # ------------------------------------------------------------ 建代与退回

    def snap(self, gen, kind, mb):
        """建一代并入链。返回 (耗时, mem_mode, 服务端分段, 实测内存, 实测文件)。"""
        self.mark(gen)
        before = self.layers() if self.store else set()
        t0 = time.monotonic()
        ck = self.sbx.checkpoint.create(name=gen)
        dt = time.monotonic() - t0
        mm, md = self.measure(ck.checkpoint_id, before)
        self.chain.append((gen, ck.checkpoint_id, kind, mb, mm, md))
        return dt, (getattr(ck, "mem_mode", None) or "?"), self.phases(ck.checkpoint_id), mm, md

    def phases(self, ck_id):
        """这一次 checkpoint 的服务端分段（timings.json，单位 ms）。
        只有在宿主机上跑才读得到；读不到就空手而归，表里那几列缺省。"""
        if not self.store:
            return {}
        p = os.path.join(self.store, self.sbx.sandbox_id, ck_id, "timings.json")
        try:
            return json.load(open(p))
        except (OSError, ValueError):
            return {}

    def restore_phases(self):
        """刚做完的那一次 restore 的服务端分段（last-restore-timings.json）。
        这个文件每次 restore 覆写一遍，所以必须紧接着读。"""
        if not self.store:
            return {}
        p = os.path.join(self.store, self.sbx.sandbox_id, "last-restore-timings.json")
        try:
            return json.load(open(p))
        except (OSError, ValueError):
            return {}

    def sweep_pass(self, kind, pre, tiers):
        """一遍档位。kind="now" 写完立刻拍；kind="settle" 先把宿主机冲干净再拍。"""
        rows = []
        print()
        print("     %s %s %s %s %s %s %s"
              % (lpad("档位(内+文)", 14), rpad("checkpoint", 12), rpad("实测内存", 10),
                 rpad("实测文件", 10), rpad("内存快照", 12), rpad("封层", 10), "  模式"))
        print("     " + "-" * 84)
        for mb in tiers:
            self.dirty(mb)
            if kind == "settle":
                self.settle()
            dt, mode, tm, mm, md = self.snap("%s%d" % (pre, mb), kind, mb)
            rows.append((mb, dt, mode, tm, mm, md))
            m, f = self.split_mb(mb)
            print("     %s %s %s %s %s %s %s"
                  % (lpad("%d+%d MB" % (m, f), 14), rpad("%.3f s" % dt, 12),
                     rpad(fmt_mb(mm), 10), rpad(fmt_mb(md), 10),
                     rpad(fmt_ms(tm.get("snapshot")), 12),
                     rpad(fmt_ms(tm.get("seal")), 10), "  " + mode))
        return rows

    def unwind(self):
        """从链尾一路退回 g0。

        **退一代的代价 = 这一跳要撤销的量**，也就是被退掉的那一代建之前弄脏的量 ——
        不是目标那一代自己弄脏了多少。所以每一跳的档位标签取自被退掉的那一代。"""
        hops = []
        for i in range(len(self.chain) - 1, 0, -1):
            gen, _, kind, mb, mm, md = self.chain[i]     # 被退掉的这一代
            target = self.chain[i - 1][0]
            t0 = time.monotonic()
            ok = self.sbx.checkpoint.restore(self.chain[i - 1][1])
            dt = time.monotonic() - t0
            tm = self.restore_phases()          # 必须在下一次 restore 之前读
            landed = self.gen_now()
            good = bool(ok) and landed == target
            if not good:
                self.bad.append("退到 %s：API=%s，落在 %r" % (target, ok, landed))
            hops.append((kind, mb, gen, target, dt, good, tm, mm, md))
            if kind == "warm":
                tag = "预热代"
            else:
                m, f = self.split_mb(mb)
                tag = "内%d+文%d MB" % (m, f)
            print("     %s %s %s %s" % (lpad("%s → %s" % (gen, target), 16),
                                        lpad("撤销 " + tag, 22), rpad("%.3f s" % dt, 9),
                                        "✓" if good else "✗"))
        return hops


def main():
    ap = argparse.ArgumentParser(description="checkpoint/restore 耗时基准（每档一次）")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE_ID, help="模板 ID（默认 base）")
    ap.add_argument("--tiers", default=DEFAULT_TIERS,
                    help="每档的总改动量，单位 MB，逗号分隔（默认 %s）" % DEFAULT_TIERS)
    ap.add_argument("--split", default=DEFAULT_SPLIT,
                    help="总量按 内存:文件 拆的比例（默认 %s）" % DEFAULT_SPLIT)
    ap.add_argument("--timeout", type=int, default=3600, help="沙箱存活秒数")
    args = ap.parse_args()

    tiers = [int(x) for x in args.tiers.split(",") if x.strip() != ""]
    maxmb = max(tiers) if tiers else 0
    if not tiers:
        print("没有档位，无事可做。")
        return 1
    try:
        a, b = (int(x) for x in args.split.split(":"))
        assert a > 0 and b > 0
    except (ValueError, AssertionError):
        print("--split 要写成 内存:文件，比如 3:1")
        return 1

    print("模板 = %s   档位 = %s MB（总量）   拆分 = 内存:文件 %d:%d"
          % (args.template, tiers, a, b))
    sbx = Sandbox.create(template=args.template, timeout=args.timeout)
    print("沙箱 %s" % sbx.sandbox_id)
    backend = report_backend(sbx.sandbox_id)
    store, fstype = checkpoint_store()
    if store:
        print("  产物落盘 : %s   文件系统 = %s" % (store, fstype))
    else:
        print("  产物落盘 : 未知（读不到 orchestrator 进程——本脚本没跑在宿主机上？）")
        print("    不在宿主机上也能跑，只是服务端分段和实测增量那几列缺省。")

    bench = Bench(sbx, store, (a, b))
    rc = 0
    hops = []

    try:
        # ---- 1. 树根：第一代必然是全量（要自包含），单独报一行 ------------
        print("\n===== 1. 全量（第一代） =====")
        bench.run("mkdir -p %s" % BENCH_DIR)
        full_dt, mode, _, _, _ = bench.snap("g0", "root", 0)
        print("  g0   %.3f s   mem_mode=%s   （树根要自包含，天然全量；之后全是增量）"
              % (full_dt, mode))

        # ---- 空间检查 + 预热 --------------------------------------------
        mmax, fmax = bench.split_mb(maxmb)
        free = bench.kv(bench.run(
            "echo shm=$(df -m --output=avail /dev/shm 2>/dev/null | tail -1)\n"
            "echo root=$(df -m --output=avail / 2>/dev/null | tail -1)"))

        def as_mb(x):
            try:
                return int(str(x).strip())
            except (TypeError, ValueError):
                return -1          # 读不到就当"不知道"，下面按不够处理

        shm_free, root_free = as_mb(free.get("shm")), as_mb(free.get("root"))
        memtotal = bench.run("awk '/MemTotal/{print int($2/1024)}' /proc/meminfo").strip()
        print("\n===== 2. 档位扫描：checkpoint =====")
        print("  每档总量按 %d:%d 拆：最大档 %d MB = 内存 %d + 文件 %d。"
              % (a, b, maxmb, mmax, fmax))
        print("  /dev/shm 需 %d MB、/ 需 %d MB 余量；沙箱内存 %s MB，实际可用"
              % (mmax + 16, fmax + 16, memtotal or "?"))
        print("  /dev/shm %d MB、/ %d MB。" % (shm_free, root_free))
        if shm_free < mmax + 16 or root_free < fmax + 16:
            print("  空间不够，跑不了。用 --tiers 降档或 --split 调比例。")
            return 1
        bench.warm(maxmb)
        bench.snap("W", "warm", maxmb)
        print("  预热完成：两个文件已各自撑到最大档位，之后每档只覆写前 N MB。")
        print("  表里「模式」是服务端自报这一代怎么捕的：incremental 只写脏页，full 整份内存。")

        print("\n  -- ① 写完立刻拍（最坏情况上界）--")
        now_rows = bench.sweep_pass("now", "I", tiers)
        print("\n  -- ② 写完先把宿主机冲干净（sync + 歇 1 秒）再拍（日常口径）--")
        settle_rows = bench.sweep_pass("settle", "S", tiers)

        # ---- 3. restore -------------------------------------------------
        print("\n===== 3. 档位扫描：restore =====")
        print("  从链尾一路退回 g0。**退一代的代价 = 这一跳要撤销的量** ——")
        print("  是被退掉的那一代建之前弄脏的量，不是目标那一代自己弄脏了多少。")
        print("  最后的 W → g0 退的是预热代 —— 它建之前把两个文件首次写满")
        print("  （内 %d + 文 %d MB），这一跳的撤销量其实是全链最大的。" % bench.split_mb(maxmb))
        print("  每跳都验一个代号标记，确认真的落到了目标代。")
        print()
        hops = bench.unwind()

        # ---- 档位对比 ----------------------------------------------------
        print("\n================ 档位对比 ================")

        def ph_cols(tm, memks, diskks):
            if not tm:
                return None, None, None
            tot = lambda ks: (sum(tm[k] for k in ks if k in tm)
                              if any(k in tm for k in ks) else None)
            return tot(memks), tot(diskks), tm.get("frozen")

        def ck_block(tag, rows, base=None):
            """一遍一小块。名义两列是写了多少；实测两列是服务端真写下了多少；
            然后是用户等到的 e2e，再把冻结窗口拆成内存快照和封层两段。"""
            print()
            print("  %s" % tag)
            hdr = [lpad("内存增量", 10), lpad("文件增量", 10), rpad("实测内存", 10),
                   rpad("实测文件", 10), rpad("e2e 耗时", 10), rpad("内存快照", 11),
                   rpad("封层", 9), rpad("冻结", 10)]
            bar = 96
            if base is not None:
                hdr.append(rpad("相对立刻拍", 12))
                bar += 13
            print("    " + " ".join(hdr))
            print("    " + "-" * bar)
            for mb, dt, _mode, tm, mm, md in rows:
                m, f = bench.split_mb(mb)
                pm, pd, fr = ph_cols(tm, ["snapshot"], ["seal"])
                row = [lpad("%d MB" % m, 10), lpad("%d MB" % f, 10),
                       rpad(fmt_mb(mm), 10), rpad(fmt_mb(md), 10),
                       rpad("%.3f s" % dt, 10), rpad(fmt_ms(pm), 11),
                       rpad(fmt_ms(pd), 9), rpad(fmt_ms(fr), 10)]
                if base is not None:
                    row.append(rpad("%.2f×" % (dt / base[mb]) if base.get(mb) else "-", 12))
                print("    " + " ".join(row))
            print("    " + "-" * bar)

        now_e2e = dict((r[0], r[1]) for r in now_rows)
        print()
        print("  增量 checkpoint —— 同样的负载，两个时机")
        ck_block("① 写完立刻拍", now_rows)
        ck_block("② 写完先冲干净再拍", settle_rows, base=now_e2e)
        print("    两块负载完全相同，① 减 ② 就是「写完立刻拍」的时机成本：刚写的文件")
        print("    数据正被宿主机内核往盘上刷、和写 mem_diff 抢磁盘（抬高内存快照列），")
        print("    加上 NBD 管道里的在途数据要排空（抬高封层列）。② 才是日常口径。")
        print()
        print("    实测两列直接量服务端产物（mem_diff 与新封层的磁盘实占）。普通文件写")
        print("    先经过 guest 页缓存，所以一份文件写在内存里也要脏一份 —— 预期")
        print("    实测内存 ≈ 名义内存 + 名义文件，实测文件 ≈ 名义文件。")
        print("    列的关系：内存快照 + 封层 ≈ 冻结（虚机真正停住的窗口）；")
        print("    e2e 减冻结 = 网络往返 + 服务端没停虚机时干的活。")

        rest = {"now": {}, "settle": {}}
        rest_tm = {"now": {}, "settle": {}}
        rest_meas = {"now": {}, "settle": {}}
        for kind, mb, gen, _t, dt, _g, tm, mm, md in hops:
            if kind in rest:
                rest[kind][mb] = dt
                rest_tm[kind][mb] = tm
                rest_meas[kind][mb] = (mm, md)

        def rs_block(tag, kind, base=None):
            print()
            print("  %s" % tag)
            hdr = [lpad("撤销内存", 10), lpad("撤销文件", 10), rpad("实测内存", 10),
                   rpad("实测文件", 10), rpad("e2e 耗时", 10), rpad("内存回滚", 11),
                   rpad("换盘视图", 10), rpad("冻结", 10)]
            bar = 88
            if base is not None:
                hdr.append(rpad("相对①段", 10))
                bar += 11
            print("    " + " ".join(hdr))
            print("    " + "-" * bar)
            for mb in tiers:
                dt = rest[kind].get(mb)
                if dt is None:
                    continue
                m, f = bench.split_mb(mb)
                mm, md = rest_meas[kind].get(mb, (None, None))
                pm, pd, fr = ph_cols(rest_tm[kind].get(mb),
                                     ["save_live_bitmap", "materialize", "fc_rollback"],
                                     ["assemble_view", "reset_view"])
                row = [lpad("%d MB" % m, 10), lpad("%d MB" % f, 10),
                       rpad(fmt_mb(mm), 10), rpad(fmt_mb(md), 10),
                       rpad("%.3f s" % dt, 10), rpad(fmt_ms(pm), 11),
                       rpad(fmt_ms(pd), 10), rpad(fmt_ms(fr), 10)]
                if base is not None:
                    row.append(rpad("%.2f×" % (dt / base[mb]) if base.get(mb) else "-", 10))
                print("    " + " ".join(row))
            print("    " + "-" * bar)

        print()
        print("  增量 restore —— 要撤销多少，花多久")
        rs_block("① 退「冲干净再拍」段的跳（链更深）", "settle")
        rs_block("② 退「立刻拍」段的跳（链更浅）", "now", base=rest["settle"])
        print("    两段撤销的负载相同，快照时机不影响撤销本身 —— 差异主要是链深：")
        print("    ①段建得晚、退它时目标脚下压的层多，「换盘视图」重建层栈的固定")
        print("    开销就大。②÷① 明显小于 1 的部分基本是这笔位置差，不是时机差。")
        print()
        print("    实测两列 = 被退掉那一代快照时实际写下的量（mem_diff 与封层实占），")
        print("    正是这一跳要搬回去/丢掉的东西。撤销集里另含恢复时刻的现场新脏页，")
        print("    扫描间隙只有底噪那几 MB，未单列。")
        print()
        print("    restore 的两段：「内存回滚」= 存下当前脏页位图 + 把回滚集从各代差分")
        print("    里物化出来 + Firecracker 搬回去（物化那步常比搬运还贵）；「换盘视图」")
        print("    = 重建层栈再把整个磁盘视图换掉。两段都在冻结窗口内。冻结减掉这两列，")
        print("    剩下的是清连接跟踪和暂停/恢复，十几毫秒且不随档位变；e2e 减冻结，")
        print("    主要是恢复后等 guest 的 envd 答话 —— 那时沙箱已经在跑了。")

        # ------------------------------------------------------------ 汇总
        backend_short = {"hdbss": "HDBSS（硬件标脏）",
                         "kvm-wp": "KVM 写保护（软件）",
                         "off": "未开启"}.get(backend, "未知")
        print("\n================ 汇总 ================")
        print("跑在：脏页后端 = %s   |   产物文件系统 = %s   |   落盘路径 = %s"
              % (backend_short, fstype, store or "未知"))
        print()
        print("全量（第一代）%.3f s。" % full_dt)
        se = dict((r[0], r[1]) for r in settle_rows)
        if now_e2e.get(maxmb) and se.get(maxmb):
            mmax2, fmax2 = bench.split_mb(maxmb)
            print("最大档 %d MB（内%d+文%d）：立刻拍 %.3f s，冲干净再拍 %.3f s ——"
                  % (maxmb, mmax2, fmax2, now_e2e[maxmb], se[maxmb]))
            print("差值 %.3f s 是「写完立刻拍」的时机成本，不是快照机制的成本。"
                  % (now_e2e[maxmb] - se[maxmb]))
        print("每档只测一次。单次会抖 —— 节点刚重启后的第一轮尤其明显，见过同一档差 2~4 倍。")
        print("要分位数和分布，用 test-950/bench-ckpt.py；功能正确性用 checkpoint_verify.py。")
        if backend == "kvm-wp":
            print("注意：本次跑在软件写保护上，不是硬件标脏，checkpoint 的数字含 VM exit 开销。")
        elif backend == "off":
            print("注意：脏页跟踪没开。")
        if any(r[2] == "full" for r in now_rows + settle_rows):
            print("⚠ 有增量档被服务端报成 full —— 脏页跟踪没开，这组数字不是增量。"
                  "查 template-manager 的 FC_TRACK_DIRTY_PAGES。")
            rc = 1
        if bench.bad:
            print("✗ %d 跳没落到目标代：" % len(bench.bad))
            for m in bench.bad:
                print("    - %s" % m)
            rc = 1
        else:
            print("每一跳都落到了目标代（代号标记校验通过）。")
    finally:
        try:
            sbx.kill()
            print("沙箱已删除")
        except Exception:
            pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
