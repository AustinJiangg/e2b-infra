#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一种负载下的 checkpoint / restore：沙箱级、进程级用同一个脚本、同一个负载，各跑一遍。

为什么要这份脚本：两套原来的分档测试负载不同（沙箱级改写 /dev/shm，进程级改写常驻进程的
bytearray，且进程大小随档位一起变），并列时分不开"改动量"和"进程大小"两个因素。这里两套都用
同一个常驻进程：

    常驻进程   预分配 P MiB 的 bytearray 并写满；收到命令时原地覆写前 d MiB
    文件       根盘上的 /tmp/swl/file，f MiB，dd 覆写后 fsync

一个测点由下面几个量决定（没写的取默认值）：

    P       常驻进程合计多大（MiB）
    d       每次改动多少内存（MiB）
    layout  改动的页分布：seq = 从头连续改 d MiB（默认）；scatter = 在整个进程里按固定的随机顺序
            挑 d MiB 对应的页数，每页改开头 64 字节
    trees   几棵进程树（默认 1）：起 trees 个互不相干的常驻进程，P 和 d 平分到每一棵
    f       每次写多少文件（MiB），覆写根盘上的 /tmp/swl/file
    sync    写完文件怎样落盘：fsync = dd 带 fsync 再 sync（默认，数据进到宿主）；none = 只 write，
            数据留在 guest 页缓存
    wait    改完、写完之后等几秒再 checkpoint（默认 0）

测点组（每个测点新建沙箱，--passes 个沙箱 × --reps 次，前 --warmup 次不计入统计）：

    dirty    进程固定 256 MiB，改动 d = 0、16、32、64、128、256 MiB，不写文件
    size     改动固定 16 MiB，进程 P = 32、64、128、256、384 MiB，不写文件
    file     进程 16 MiB、不改内存，只写文件 f = 16、64 MiB
    conc     N 个沙箱同时做（N = 1、4、8、16），每个沙箱进程 64 MiB、改动 16 MiB
    layout   进程 256 MiB，散页改动 d = 16、64、256 MiB（与 dirty 的连续改动对照）
    trees    合计进程 64 MiB、改动 16 MiB，分到 1、4、16 棵进程树
    filewait 进程 16 MiB、写 64 MiB 文件，落盘 fsync / none × 写完等 0、10、40 秒
             （默认每测点 1 个沙箱 × 6 次；40 秒那几档一个测点约 8 分钟）
    scen     --scenarios 给的场景文件，每个场景一个测点（格式见 SCENARIO_HELP）
    mix      对外展示用的分档表：进程固定 256 MiB，每档改写 d MiB 内存、写 f MiB 文件，
             (d, f) = (0,0) (12,4) (24,8) (48,16) (96,32) (192,64) (70,0.001)，内存 : 文件 = 3 : 1
             （最后一档是另加的）。每个测点另起一个沙箱，在同一负载上测 e2b 原生 pause / resume，
             见下面"原生 pause / resume"。两套并列时 mix 另打一张对外表（p50 一张、p99 一张）。

默认跑 mix,dirty,size,file,conc,layout,trees；filewait 时间长，要显式写 --series 或用 --series all。
文件大小可以是小数（MiB），不是整 MiB 时按字节数一次写入。

原生 pause / resume（只在 mix 组）：同一个测点、同样起常驻进程并写满、写好文件、等 ext4 初始化，
然后循环"改写 d MiB、写 f MiB → sb.pause() → Sandbox.connect() → 第一条命令 → 核对内存 SHA256 与
文件"。pause 计 sb.pause() 的调用耗时；resume 计 connect 返回加第一条命令完成（与
native_snapshot_tiers.py 同一口径）。第 0 次 pause 导出的是自启动以来的全部写入，单列不进统计。
不与 checkpoint 混在一个沙箱里，免得两种快照互相影响。每次 pause 产出的 build 在沙箱结束后删掉
（本地模板存储时）。在哪套栈上跑，测的就是那套代码里的原生路径。

基点前先等 guest 的 ext4 把 inode 表后台清零做完（ext4lazyinit 线程退出）：模板快照拍在清零完成之前，
新沙箱启动后几秒内会额外写出约 60 MiB 根盘数据；基点拍在清零之后，restore 回基点就不会再重做一遍，
写完等待那一组的数据才只含文件本身。等了多久记在基点那一行的 lazyinit_wait_s。

一次测量：改写 → checkpoint A（计时）→ 再改写 → checkpoint B（计时）→ restore 回基点（计时）→
校验内存 SHA256 与文件 → 删掉 A、B。A 紧跟在上一次 restore 之后，是"restore 之后的 checkpoint"；
B 紧跟在 A 之后，是"连续的 checkpoint"。两者分开列：进程级 restore 之后的第一次 checkpoint
曾经是全量（CRIU 的 PID 复用检测，进程级手册第 16 章 L-18，gsd daae6968f 起修复为增量），
分开列可以核对修复是否生效。基点是每个沙箱改写之前的一次全量 checkpoint，单列不进统计。

记录：客户端墙钟；服务端分段（进程级取 SDK 返回的 gsd 分段，沙箱级读宿主上
<store>/<sandbox>/<checkpoint>/timings.json 与 last-restore-timings.json，需要 root）；产物字节
（进程级 image_bytes；沙箱级 checkpoint 目录里各文件的实占字节）；Firecracker 自报的脏页后端
（hdbss = 硬件标脏、kvm-wp = KVM 写保护、off = 没开；沙箱级的增量靠它，进程级不用它）。
汇总在总耗时表之后再打一张服务端分段表（选几个代表测点的 p50），用来看时间花在哪一段。
进程级另外读 CRIU 自己的统计（guest 里镜像目录下的 stats-dump / stats-restore：冻结、扫页、写页、
fork、恢复各段耗时与页数），把 criu_dump / criu_restore 再拆开；读在每轮核对之后，不在计时区间内。

每次运行开头记录宿主条件（NUMA 节点与大小、kernel.numa_balancing、宿主 THP、内核命令行），
两轮结果对不上时先看这里。

在部署宿主上运行，用当前栈对应的 SDK 环境：
    python3 ckpt_same_workload.py --out result.json
    --series dirty,size,...|all     选测点组（默认见上）
    --scenarios scen.json           场景文件；给了它而 --series 没写 scen 时自动加上
    --passes 2 --reps 11 --warmup 1 每个测点几个沙箱、每个沙箱几次、前几次不计（filewait 另有默认）
    --smoke                         每组只跑一个测点、1 个沙箱 3 次
    --stack sandbox|criu            默认按正在运行的 orchestrator 判断
    --template                      默认沙箱级 base、进程级 base-criu
    --print a.json [b.json ...]     不跑测试，把已有结果（同一套的多轮可合并）的汇总重新打印一遍
    --compare a.json b.json ...     不跑测试，把两套的结果（每套可多轮）按测点并列，出代价分解表
    --compare-siblings              与 --out 合用：取 --out 所在目录的上一级里其他各步的 result.json 做 --compare
结果写 JSON，stdout 打印汇总表；校验失败退出码非零。
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import logging
import os
import glob
import socket
import statistics
import subprocess
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("e2b").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger("same")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from native_snapshot_tiers import fc_info, list_cache, export_sizes, remove_builds  # noqa: E402

MIB = 1 << 20
WD = "/tmp/swl"
SBX_TIMEOUT = 3600
# 测点的各个量与默认值；测点字典里没写的量取这里的值
DEFAULTS = {"P": 16, "d": 0, "f": 0, "layout": "seq", "trees": 1, "sync": "fsync", "wait": 0}
MIX_POINTS = ((0, 0), (12, 4), (24, 8), (48, 16), (96, 32), (192, 64), (70, 0.001))
DIMS = ("P", "d", "f", "layout", "trees", "sync", "wait")
SERIES = {
    "dirty": [{"P": 256, "d": d} for d in (0, 16, 32, 64, 128, 256)],
    "size": [{"P": p, "d": 16} for p in (32, 64, 128, 256, 384)],
    "file": [{"P": 16, "f": f} for f in (16, 64)],
    "layout": [{"P": 256, "d": d, "layout": "scatter"} for d in (16, 64, 256)],
    "trees": [{"P": 64, "d": 16, "trees": t} for t in (1, 4, 16)],
    "filewait": [{"P": 16, "f": 64, "sync": s, "wait": w} for s in ("fsync", "none") for w in (0, 10, 40)],
    "mix": [{"P": 256, "d": d, "f": f} for d, f in MIX_POINTS],
}
NATIVE_SERIES = ("mix",)     # 这些组的每个测点另起沙箱测原生 pause / resume
DEFAULT_SERIES = "mix,dirty,size,file,conc,layout,trees"
ALL_SERIES = "mix,dirty,size,file,conc,layout,trees,filewait"
SERIES_RUNS = {"filewait": (1, 6)}     # 没显式给 --passes/--reps 时这些组用的（沙箱数, 次数）
CONC = [1, 4, 8, 16]
CONC_POINT = {"P": 64, "d": 16}
SCENARIO_HELP = """场景文件（JSON）：
{"scenarios": [
  {"name": "交互式小步", "P": 128, "d": 8, "layout": "scatter", "f": 4, "sync": "none", "wait": 5},
  {"name": "装依赖", "P": 64, "d": 32, "f": 128, "sync": "fsync", "wait": 0, "trees": 4}
]}
name 必填；其他量没写的取默认值 %s。P、d 要能被 trees 整除。""" % json.dumps(DEFAULTS, ensure_ascii=False)


def norm(point):
    """补全测点的各个量。"""
    p = dict(DEFAULTS)
    p.update(point)
    for k in ("P", "d", "trees", "wait"):
        p[k] = int(p[k])
    p["f"] = float(p["f"])          # 文件可以不是整 MiB；整数时保持 int，测点的键与旧结果一致
    if p["f"].is_integer():
        p["f"] = int(p["f"])
    if p["layout"] not in ("seq", "scatter") or p["sync"] not in ("fsync", "none"):
        raise ValueError("测点 %s：layout 只能是 seq/scatter，sync 只能是 fsync/none" % point)
    if p["trees"] < 1 or p["P"] % p["trees"] or p["d"] % p["trees"] or p["d"] > p["P"]:
        raise ValueError("测点 %s：P、d 要能被 trees 整除，且 d 不大于 P" % point)
    return p


def load_scenarios(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for s in data.get("scenarios", []):
        if not s.get("name"):
            raise ValueError("场景缺 name：%s\n%s" % (s, SCENARIO_HELP))
        out.append(norm(s))
    if not out:
        raise ValueError("场景文件里没有场景\n" + SCENARIO_HELP)
    return out


def point_key(r):
    """一行结果所属测点的键；老结果没有新加的量，按默认值补。"""
    return (r["series"], r.get("N", 1), r.get("name")) + tuple(r.get(k, DEFAULTS[k]) for k in DIMS)


def point_label(e):
    """测点的简短写法，只写与默认值不同的量（P、d 总写）。"""
    parts = []
    if e.get("name"):
        parts.append(e["name"])
    if e.get("N", 1) != 1:
        parts.append("N=%d" % e["N"])
    parts += ["P=%d" % e["P"], "d=%d" % e["d"]]
    for k, fmt_ in (("layout", "%s"), ("trees", "trees=%d"), ("f", "f=%g"), ("sync", "sync=%s"), ("wait", "wait=%ds")):
        if e.get(k, DEFAULTS[k]) != DEFAULTS[k] or (k == "sync" and e.get("f")):
            parts.append(fmt_ % e[k])
    return " ".join(parts)


# 常驻进程：命令经预先打开的文件传入，SIGUSR1 触发；handler 不分配大块内存、不 open()，
# 改写用启动时分配好的 1 MiB 块，避免新建 VMA（进程级的 soft-dirty 会把新 VMA 整个报脏）。
# 散页改动用启动时排好的页序，每页改开头 64 字节（同样不新建 VMA）。
RESIDENT_PY = r'''
import hashlib, os, random, signal, sys
WD = sys.argv[2]
CMD, RSP, PID = WD + "/cmd", WD + "/rsp", WD + "/pid"
size = int(sys.argv[1]) << 20
buf = bytearray(size)
for i in range(0, size, 4096):
    buf[i] = 1
chunk = bytearray(min(max(size, 1), 1 << 20))
perm = list(range(size >> 12))
random.Random(7).shuffle(perm)
small = bytearray(64)
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
        elif op == "sdirty":
            n = int(arg)
            if (n << 20) > size:
                raise ValueError("dirty exceeds payload")
            small[:] = hashlib.md5(nonce.encode()).digest() * 4
            for p in perm[:n << 8]:
                o = p << 12
                buf[o:o + 64] = small
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

def dd_size(mib):
    """dd 的块大小参数：整 MiB 用 bs=1M count=N，否则按字节数一次写入。"""
    n = int(round(mib * MIB))
    if n % MIB == 0:
        return "bs=1M count=%d" % (n // MIB)
    return "bs=%d count=1 iflag=fullblock" % n


class Hog:
    """trees 个互不相干的常驻进程（每个是一棵进程树），各在 WD/h<i> 下收命令；命令一次发给全部。"""

    def __init__(self, sb, trees=1):
        self.sb = sb
        self.dirs = ["%s/h%d" % (WD, i) for i in range(trees)]
        self.nonce = 0
        self.pids = []

    def sh(self, cmd, timeout=120):
        return self.sb.commands.run(cmd, user="root", timeout=timeout).stdout.strip()

    def ask(self, op, arg=""):
        """发给全部常驻进程，等全部应答；返回各自的应答（按进程顺序）。"""
        self.nonce += 1
        n = "n%d" % self.nonce
        send = "; ".join('echo -n "%s %s %s" > %s/cmd' % (n, op, arg, d) for d in self.dirs)
        self.sh("%s; kill -USR1 %s" % (send, " ".join(self.pids)), timeout=10)
        files = " ".join("%s/rsp" % d for d in self.dirs)
        for _ in range(600):
            lines = self.sh("cat %s 2>/dev/null; true" % files, timeout=10).splitlines()
            if len(lines) == len(self.dirs) and all(l.startswith(n + " ") for l in lines):
                return [l[len(n) + 1:] for l in lines]
            time.sleep(0.05)
        raise RuntimeError("常驻进程没有全部应答 %s %s" % (op, arg))

    def start(self, payload_mb):
        per = payload_mb // len(self.dirs)
        self.sh("rm -rf %s; mkdir -p %s; chmod 777 %s" % (WD, " ".join([WD] + self.dirs), " ".join([WD] + self.dirs)))
        self.sb.files.write(WD + "/hog.py", RESIDENT_PY)
        # 默认用户启动：进程级只保存 uid 1000 的进程树。每个后台命令是 envd 下单独的一棵树
        for d in self.dirs:
            self.sb.commands.run("python3 %s/hog.py %d %s" % (WD, per, d), background=True)
        files = " ".join("%s/pid" % d for d in self.dirs)
        for _ in range(300):
            # pid 文件末尾没有换行，逐个读、各补一个换行，免得几个 pid 连成一个数
            pids = self.sh("for f in %s; do cat $f 2>/dev/null; echo; done" % files, timeout=10).split()
            if len(pids) == len(self.dirs):
                self.pids = pids
                break
            time.sleep(0.2)
        if len(self.pids) != len(self.dirs) or set(self.ask("ping")) != {"pong"}:
            raise RuntimeError("常驻进程没有全部起来")

    def dirty(self, mb, layout="seq"):
        if mb and set(self.ask("sdirty" if layout == "scatter" else "dirty", "%d" % (mb // len(self.dirs)))) != {"ok"}:
            raise RuntimeError("改写失败")

    def write_file(self, mb, sync="fsync"):
        if not mb:
            return
        if sync == "fsync":
            self.sh("dd if=/dev/urandom of=%s/file %s conv=notrunc,fsync 2>/dev/null; sync" % (WD, dd_size(mb)))
        else:
            self.sh("dd if=/dev/urandom of=%s/file %s conv=notrunc 2>/dev/null" % (WD, dd_size(mb)))

    def wait_lazyinit(self, limit=60):
        """等 guest 的 ext4 inode 表后台清零做完（ext4lazyinit 内核线程退出），返回等了几秒。"""
        t0 = time.time()
        while time.time() - t0 < limit:
            if not self.sh("pgrep -x ext4lazyinit; true", timeout=10):
                return round(time.time() - t0, 1)
            time.sleep(0.5)
        LOG.warning("ext4lazyinit %d 秒还没结束，继续", limit)
        return None

    def state(self, file_mb):
        mem = "|".join(self.ask("sha"))
        disk = self.sh("sha256sum %s/file | cut -d' ' -f1" % WD) if file_mb else None
        return mem, disk


# ------------------------------------------------------------ 测量

class Ctx:
    def __init__(self, args, stack, store, cache=None, storage=None):
        self.args, self.stack, self.store = args, stack, store
        self.cache, self.storage = cache, storage     # 原生 pause：导出缓存目录、本地模板存储（删 build 用）
        self.prov = None

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


# 进程级：CRIU 写在镜像目录里的统计（stats-dump / stats-restore，protobuf，时间单位微秒）。
# 字段号取自 CRIU images/stats.proto：StatsEntry{dump=1, restore=2}。
GUEST_STORE = "/var/lib/gsd/snapshots"
CRIU_STATS_PY = r"""
import glob, json, os, struct, sys
DUMP = {1: "freezing", 2: "frozen", 3: "memdump", 4: "memwrite", 5: "pages_scanned",
        6: "pages_skipped_parent", 7: "pages_written", 8: "irmap_resolve", 9: "pages_lazy"}
RESTORE = {1: "pages_compared", 2: "pages_skipped_cow", 3: "forking", 4: "restore", 5: "pages_restored"}
def uv(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1; r |= (c & 0x7f) << s; s += 7
        if c < 0x80:
            return r, i
def fields(b):
    i, out = 0, {}
    while i < len(b):
        k, i = uv(b, i); f, w = k >> 3, k & 7
        if w == 0:
            v, i = uv(b, i)
        elif w == 2:
            n, i = uv(b, i); v = b[i:i + n]; i += n
        elif w == 1:
            v = b[i:i + 8]; i += 8
        elif w == 5:
            v = b[i:i + 4]; i += 4
        else:
            return out
        out[f] = v
    return out
def read(path, which, names):
    b = open(path, "rb").read()
    off = 8 if struct.unpack_from("<I", b)[0] in (0x54564319, 0x55105940) else 4
    n = struct.unpack_from("<I", b, off)[0]
    top = fields(b[off + 4:off + 4 + n])
    sub = fields(top.get(which, b""))
    return {names[k]: v for k, v in sub.items() if k in names and isinstance(v, int)}
res = {}
for key, ck, kind in zip(sys.argv[1::3], sys.argv[2::3], sys.argv[3::3]):
    acc = {}
    for path in glob.glob(os.path.join(%r, ck + "_mem", "t*", "stats-" + kind)):
        try:
            one = read(path, 1 if kind == "dump" else 2, DUMP if kind == "dump" else RESTORE)
        except Exception:
            continue
        for k, v in one.items():
            acc[k] = acc.get(k, 0) + v
    res[key] = acc
print(json.dumps(res))
""" % GUEST_STORE
CRIU_TIME_FIELDS = ("freezing", "frozen", "memdump", "memwrite", "irmap_resolve", "forking", "restore")


# 进程级归因（--criu-logs）：CRIU 每行日志带 "(秒.微秒)" 时间戳。相邻两行的间隔记到前一行的
# 消息上（数字换成 #），按消息合计，取最大的几项。dump 日志是 gsd 写死的 -v4；restore 默认级别
# 几乎没有日志，要配 --criu-verbosity 4 才有内容。
CRIU_LOG_PY = r"""
import glob, json, os, re, sys
TS = re.compile(r"^\((\d+)\.(\d+)\)\s*(.*)$")
NUM = re.compile(r"0x[0-9a-fA-F]+|\d+")
def one(path):
    rows = []
    for line in open(path, errors="replace"):
        m = TS.match(line)
        if m:
            rows.append((int(m.group(1)) + int(m.group(2)) / 1e6, NUM.sub("#", m.group(3).strip())[:90]))
    acc = {}
    for (t, msg), (t2, _) in zip(rows, rows[1:]):
        acc[msg] = acc.get(msg, 0.0) + (t2 - t) * 1000
    span = (rows[-1][0] - rows[0][0]) * 1000 if rows else 0.0
    return span, len(rows), acc
res = {}
for key, ck, name in zip(sys.argv[1::3], sys.argv[2::3], sys.argv[3::3]):
    tot_span, tot_lines, acc = 0.0, 0, {}
    for path in glob.glob(os.path.join(%r, ck + "_mem", "t*", name)):
        span, n, a = one(path)
        tot_span += span; tot_lines += n
        for k, v in a.items():
            acc[k] = acc.get(k, 0.0) + v
    top = sorted(acc.items(), key=lambda kv: -kv[1])[:12]
    res[key] = {"span_ms": round(tot_span, 3), "lines": tot_lines, "top": [[k, round(v, 3)] for k, v in top]}
print(json.dumps(res))
""" % GUEST_STORE

# --criu-verbosity N：在 guest 里把 /usr/sbin/criu 换成一个包装脚本，dump 与 restore 都用 -vN
# （去掉原有的 -v 参数）。只用于测量日志级别本身的代价，包装脚本本身多一次 exec。
CRIU_WRAP = r"""#!/bin/bash
a=()
for x in "$@"; do
  case "$x" in -v|-v[0-9]) ;; *) a+=("$x") ;; esac
done
exec /usr/sbin/criu.real "${a[@]}" -v%d
"""


def install_criu_wrapper(hog, level):
    hog.sh("test -e /usr/sbin/criu.real || mv /usr/sbin/criu /usr/sbin/criu.real")
    hog.sb.files.write("/tmp/criu-wrap", CRIU_WRAP % level)
    hog.sh("install -m 755 /tmp/criu-wrap /usr/sbin/criu && /usr/sbin/criu --version | head -1")


def criu_logs(hog, wanted):
    """wanted: [(行里的键, checkpoint_id, 日志文件名)]。返回 {键: {span_ms, lines, top}}。"""
    args = " ".join("%s %s %s" % w for w in wanted)
    try:
        return json.loads(hog.sh("python3 %s/criu_log.py %s" % (WD, args), timeout=60) or "{}")
    except Exception as exc:
        LOG.warning("读 CRIU 日志失败：%s", exc)
        return {}


def criu_stats(hog, wanted):
    """wanted: [(行里的键, checkpoint_id, "dump"|"restore")]。返回 {键: {字段: 值}}，时间换成 ms。"""
    args = " ".join("%s %s %s" % w for w in wanted)
    try:
        raw = json.loads(hog.sh("python3 %s/criu_stats.py %s" % (WD, args), timeout=30) or "{}")
    except Exception as exc:  # 读不到统计不影响测量
        LOG.warning("读 CRIU 统计失败：%s", exc)
        return {}
    out = {}
    for key, d in raw.items():
        out[key] = {(k + "_ms" if k in CRIU_TIME_FIELDS else k): (v / 1000.0 if k in CRIU_TIME_FIELDS else v)
                    for k, v in d.items()}
    return out


def prepare_sandbox(sb, ctx, point, lock):
    """起常驻进程并写满、写好文件、等 ext4 初始化。返回 (hog, 等 ext4 初始化的秒数)。"""
    if ctx.prov is not None and "dirty_tracking" not in ctx.prov:
        info = fc_info(sb.sandbox_id)
        with lock:
            ctx.prov.setdefault("dirty_tracking", info.get("dirty_tracking") if info else None)
            ctx.prov.setdefault("firecracker_version", info.get("vmm_version") if info else None)
    if ctx.stack == "criu":
        sb.commands.run("echo never > /sys/kernel/mm/transparent_hugepage/enabled", user="root")
    hog = Hog(sb, point["trees"])
    if ctx.stack == "criu" and ctx.args.criu_verbosity is not None:
        install_criu_wrapper(hog, ctx.args.criu_verbosity)
    hog.start(point["P"])
    if ctx.stack == "criu":
        sb.files.write(WD + "/criu_stats.py", CRIU_STATS_PY)
        sb.files.write(WD + "/criu_log.py", CRIU_LOG_PY)
    hog.dirty(point["P"])
    if point["f"]:
        hog.sh("dd if=/dev/urandom of=%s/file %s conv=fsync 2>/dev/null" % (WD, dd_size(point["f"])))
    hog.sh("sync")
    lazy_s = hog.wait_lazyinit()
    hog.sh("sync")
    return hog, lazy_s


def native_sandbox(Sandbox, ctx, point, series, pass_no, reps, rows, lock):
    """同一负载上的 e2b 原生 pause / resume（见文件头）。rep 0 是首次 pause，单列。"""
    point = norm(point)
    d, f = point["d"], point["f"]
    sb = Sandbox.create(template=ctx.args.template, timeout=SBX_TIMEOUT)
    sid = sb.sandbox_id
    builds = []
    try:
        hog, _ = prepare_sandbox(sb, ctx, point, lock)
        for rep in range(reps + 1):
            hog.dirty(d, point["layout"])
            hog.write_file(f, point["sync"])
            if point["wait"]:
                time.sleep(point["wait"])
            want = hog.state(f)
            before = list_cache(ctx.cache)
            t0 = time.perf_counter()
            sb.pause()
            pause_ms = (time.perf_counter() - t0) * 1000
            mem_exp, disk_exp, build = export_sizes(ctx.cache, before)
            builds.append(build)
            t0 = time.perf_counter()
            sb = Sandbox.connect(sid, timeout=SBX_TIMEOUT)
            call_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            sb.commands.run("true", user="root", timeout=60)
            ready_ms = (time.perf_counter() - t0) * 1000
            hog.sb = sb
            ok = hog.state(f) == want
            row = {"series": series, **point, "pass": pass_no, "rep": rep,
                   "kind": "native_first" if rep == 0 else "native", "pause_ms": pause_ms,
                   "resume_ms": call_ms + ready_ms, "resume_call_ms": call_ms, "ready_ms": ready_ms,
                   "export_mem_mib": mem_exp, "export_disk_mib": disk_exp, "sandbox_id": sid, "ok": ok}
            with lock:
                rows.append(row)
            if not ok:
                raise RuntimeError("resume 后内容不一致：%s %s rep %d" % (series, point_label(point), rep))
        LOG.info("%-8s %s pass %d：原生 pause / resume %d 次完成", series, point_label(point), pass_no, reps + 1)
    finally:
        try:
            sb.kill()
        except Exception as exc:
            LOG.warning("kill %s：%s", sid, exc)
        if ctx.storage:
            remove_builds(ctx.storage, builds)


def one_sandbox(Sandbox, ctx, point, series, pass_no, reps, warmup, rows, lock):
    point = norm(point)
    d, f = point["d"], point["f"]
    sb = Sandbox.create(template=ctx.args.template, timeout=SBX_TIMEOUT)
    try:
        hog, lazy_s = prepare_sandbox(sb, ctx, point, lock)
        want = hog.state(f)
        t0 = time.perf_counter()
        base = sb.checkpoint.create(name="base")
        base_ms = (time.perf_counter() - t0) * 1000
        with lock:
            rows.append({"series": series, **point, "pass": pass_no, "rep": -1, "kind": "base",
                         "ckpt_ms": base_ms, "ckpt_server": ctx.server(sb, base.checkpoint_id, "create",
                                                                        getattr(base, "timings", None)),
                         "artifact": ctx.artifact(sb, base), "sandbox_id": sb.sandbox_id, "ok": True,
                         "lazyinit_wait_s": lazy_s})

        def change_and_checkpoint(name):
            hog.dirty(d, point["layout"])
            hog.write_file(f, point["sync"])
            if point["wait"]:
                time.sleep(point["wait"])
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
            stats = criu_stats(hog, [("ckpt_criu", cp.checkpoint_id, "dump"), ("ckpt2_criu", cp2.checkpoint_id, "dump"),
                                     ("restore_criu", base.checkpoint_id, "restore")]) if ctx.stack == "criu" else {}
            if ctx.stack == "criu" and ctx.args.criu_logs:
                stats.update(criu_logs(hog, [("ckpt2_criulog", cp2.checkpoint_id, "criu-dump.log"),
                                             ("restore_criulog", base.checkpoint_id, "criu-restore.log")]))
            for c in (cp2, cp):   # 先删子节点
                try:
                    sb.checkpoint.delete(c.checkpoint_id)
                except Exception as exc:  # 删不掉不影响本次测量，记下来
                    LOG.warning("删除 %s 失败：%s", c.checkpoint_id, exc)
            row = {"series": series, **point, "pass": pass_no, "rep": rep,
                   "kind": "warmup" if rep < warmup else "sample", "ckpt_ms": ck_ms, "restore_ms": rs_ms,
                   "ckpt_server": ck_server, "restore_server": rs_server, "artifact": art,
                   "ckpt2_ms": ck2_ms, "ckpt2_server": ck2_server, "artifact2": art2,
                   "mem_mode": getattr(cp, "mem_mode", None), "sandbox_id": sb.sandbox_id, "ok": ok, **stats}
            with lock:
                rows.append(row)
            if not ok:
                raise RuntimeError("restore 后内容不一致：%s %s rep %d" % (series, point_label(point), rep))
        LOG.info("%-8s %s pass %d：%d 次完成", series, point_label(point), pass_no, reps)
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


STAT_KEYS = (("ckpt_ms", lambda r: r["ckpt_ms"]),
             ("ckpt2_ms", lambda r: r["ckpt2_ms"]),
             ("ckpt2_server_ms", lambda r: server_total(r, "ckpt2_server")),
             ("artifact2_mib", lambda r: artifact_mib(r, "artifact2")),
             ("restore_ms", lambda r: r["restore_ms"]),
             ("ckpt_server_ms", lambda r: server_total(r, "ckpt_server")),
             ("restore_server_ms", lambda r: server_total(r, "restore_server")),
             ("artifact_mib", lambda r: artifact_mib(r)))


def stat(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"p50": statistics.median(vals), "p90": pct(vals, 90), "p99": pct(vals, 99), "max": max(vals), "n": len(vals)}


def summarize(rows):
    out = []
    keys = []
    for r in rows:
        k = point_key(r)
        if k not in keys:
            keys.append(k)
    for k in keys:
        rs = [r for r in rows if point_key(r) == k]
        samples = [r for r in rs if r["kind"] == "sample"]
        base = [r for r in rs if r["kind"] == "base"]
        e = {"series": k[0], "N": k[1], "name": k[2], **dict(zip(DIMS, k[3:])), "n": len(samples),
             "failed": sum(1 for r in rs if not r["ok"])}
        for key, get in STAT_KEYS:
            e[key] = stat([get(r) for r in samples])
        e["base_ckpt_ms"] = stat([r["ckpt_ms"] for r in base])
        e["base_artifact_mib"] = stat([artifact_mib(r) for r in base])
        native = [r for r in rs if r["kind"] == "native"]
        if native or any(r["kind"] == "native_first" for r in rs):
            e["native_n"] = len(native)
            for key in ("pause_ms", "resume_ms", "resume_call_ms", "ready_ms", "export_mem_mib"):
                e[key] = stat([r.get(key) for r in native])
            e["native_first_pause_ms"] = stat([r["pause_ms"] for r in rs if r["kind"] == "native_first"])
        out.append(e)
    return out


def fmt(st, key="p50"):
    return "-" if not st else "%.1f" % st[key]


TITLES = {"dirty": "dirty：进程固定 256 MiB，连续改动量变化",
          "size": "size：改动固定 16 MiB，进程大小变化",
          "file": "file：只写文件（fsync 后立刻 checkpoint）",
          "conc": "conc：N 个沙箱同时做，每个进程 64 MiB、改动 16 MiB",
          "layout": "layout：进程 256 MiB，散页改动（对照 dirty 的连续改动）",
          "trees": "trees：合计 64 MiB、改动 16 MiB，分到几棵进程树",
          "filewait": "filewait：写 64 MiB 文件，落盘方式 × 写完等待",
          "scen": "scen：场景文件里的场景",
          "mix": "mix：对外分档表，进程 256 MiB，改写 d MiB 内存 + 写 f MiB 文件"}


def series_order(summary):
    seen = [e["series"] for e in summary]
    return [s for s in TITLES if s in seen] + [s for s in dict.fromkeys(seen) if s not in TITLES]


def print_host(prov, indent=""):
    h = prov.get("host_conditions") or {}
    if not h:
        return
    print("%s宿主条件：NUMA %s · numa_balancing=%s · 宿主 THP=%s" % (
        indent, h.get("numa_summary") or "?", h.get("numa_balancing"), h.get("thp")))


def print_summary(result):
    prov = result["provenance"]
    print("\n---- 汇总 ----")
    print("栈 %s · 宿主 %s · 内核 %s · orchestrator %s %s · 模板 %s%s" % (
        result["stack"], prov["hostname"], prov["kernel"], prov["orchestrator_exe"],
        (prov["orchestrator_sha256"] or "?")[:16], result["template"],
        " · 合并 %d 轮" % result["rounds"] if result.get("rounds", 1) > 1 else ""))
    print("脏页后端 %s（Firecracker 自报；hdbss=硬件标脏，kvm-wp=KVM 写保护，off=没开）· FC_TRACK_DIRTY_PAGES=%s · FC %s"
          % (prov.get("dirty_tracking") or "?", prov.get("FC_TRACK_DIRTY_PAGES") or "未设",
             prov.get("firecracker_version") or "?"))
    print_host(prov)
    if (prov.get("args") or {}).get("criu_verbosity") is not None:
        print("注意：本轮 guest 里的 criu 换成了包装脚本，日志级别 -v%s（默认 gsd 写死 -v4）" % prov["args"]["criu_verbosity"])
    for series in series_order(result["summary"]):
        rows = [e for e in result["summary"] if e["series"] == series]
        print("\n%s（单位 ms，产物 MiB；墙钟 p50/p90，服务端 total 只列 p50）" % TITLES.get(series, series))
        print("%-30s %3s | %-30s | %-30s | %-23s | %7s" % (
            "测点", "n", "连续 ckpt：墙钟 / 服务端 / 产物", "restore 后 ckpt：墙钟 / 服务端 / 产物",
            "restore：墙钟 / 服务端", "全量基点"))
        for e in rows:
            print("%-30s %3d | %6s / %6s %7s %7s | %6s / %6s %7s %7s | %6s / %6s %7s | %7s%s" % (
                point_label(e), e["n"],
                fmt(e["ckpt2_ms"]), fmt(e["ckpt2_ms"], "p90"), fmt(e["ckpt2_server_ms"]), fmt(e["artifact2_mib"]),
                fmt(e["ckpt_ms"]), fmt(e["ckpt_ms"], "p90"), fmt(e["ckpt_server_ms"]), fmt(e["artifact_mib"]),
                fmt(e["restore_ms"]), fmt(e["restore_ms"], "p90"), fmt(e["restore_server_ms"]),
                fmt(e["base_ckpt_ms"]), "  失败 %d" % e["failed"] if e["failed"] else ""))
        native = [e for e in rows if e.get("native_n") is not None]
        if native:
            print("  原生 pause / resume（同一负载另起沙箱；resume = connect 返回 + 第一条命令；首次 pause 单列）")
            print("  %-28s %3s | %-17s | %-17s | %-13s | %8s" % ("测点", "n", "pause p50 / p99", "resume p50 / p99",
                                                            "connect/就绪", "首次pause"))
            for e in native:
                print("  %-28s %3d | %7s / %-7s | %7s / %-7s | %5s / %-5s | %8s" % (
                    point_label(e), e["native_n"], fmt(e["pause_ms"]), fmt(e["pause_ms"], "p99"),
                    fmt(e["resume_ms"]), fmt(e["resume_ms"], "p99"), fmt(e["resume_call_ms"]), fmt(e["ready_ms"]),
                    fmt(e["native_first_pause_ms"])))
    print_stages(result)
    print("\n判定: %s（测量 %d 次，失败 %d）" % (result["verdict"], sum(1 for r in result["rows"] if r["kind"] != "base"),
                                         sum(1 for r in result["rows"] if not r["ok"])))


# 分段表只取几个代表测点，篇幅控制在便于复制的长度。(组, 要匹配的量)
STAGE_POINTS = [("dirty", {"P": 256, "d": 0}), ("dirty", {"P": 256, "d": 16}), ("dirty", {"P": 256, "d": 64}),
                ("dirty", {"P": 256, "d": 256}), ("size", {"P": 32}), ("size", {"P": 384}), ("file", {"f": 64}),
                ("layout", {"d": 256}), ("trees", {"trees": 16}), ("filewait", {"sync": "fsync", "wait": 0}),
                ("filewait", {"sync": "fsync", "wait": 40}), ("filewait", {"sync": "none", "wait": 0}),
                ("conc", {"N": 16})]
STAGE_OPS = (("ckpt2_server", "连续 ckpt"), ("ckpt_server", "restore 后 ckpt"), ("restore_server", "restore"))
STAGE_TOP = 9      # 每行最多列几段（按 p50 从大到小）
STAGE_MIN_MS = 0.3
LOG_TOP = 6        # CRIU 日志每行列几项


def match(r, series, want):
    if r["series"] != series:
        return False
    return all(r.get(k, DEFAULTS.get(k, 1)) == v for k, v in want.items())


def stage_p50(rows, key):
    """一组样本里各分段的 p50。字段名去掉 _ms 后缀；计数（*_count）、字节量（*_mb）和总计不列。"""
    acc = {}
    for r in rows:
        for k, v in (r.get(key) or {}).items():
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            name = k[:-3] if k.endswith("_ms") else k
            if name == "total" or name.endswith("_count") or name.endswith("_mb"):
                continue
            acc.setdefault(name, []).append(float(v))
    return {k: statistics.median(v) for k, v in acc.items()}


def criu_p50(rows, key):
    """CRIU 统计的 p50：时间（ms）与页数（4 KiB 页）。"""
    acc = {}
    for r in rows:
        for k, v in (r.get(key) or {}).items():
            acc.setdefault(k, []).append(v)
    if not acc:
        return ""
    order = ("freezing_ms", "frozen_ms", "memdump_ms", "memwrite_ms", "irmap_resolve_ms", "forking_ms", "restore_ms",
             "pages_scanned", "pages_skipped_parent", "pages_written", "pages_compared", "pages_skipped_cow",
             "pages_restored")
    out = []
    for k in order:
        if k in acc:
            v = statistics.median(acc[k])
            out.append("%s %.1f" % (k[:-3], v) if k.endswith("_ms") else "%s %d" % (k, v))
    return "  ".join(out)


def criulog_p50(rows, key):
    """CRIU 日志：日志首尾跨度与耗时最多的几项（各消息在样本里的 p50；只列至少一半样本里出现的）。"""
    logs = [r.get(key) for r in rows if r.get(key) and r[key].get("lines")]
    if not logs:
        return ""
    acc = {}
    for lg in logs:
        for msg, ms in lg["top"]:
            acc.setdefault(msg, []).append(ms)
    items = sorted(((statistics.median(v), m) for m, v in acc.items() if len(v) * 2 >= len(logs)), reverse=True)
    return "跨度 %.1f · %d 行 | %s" % (statistics.median([lg["span_ms"] for lg in logs]),
                                     statistics.median([lg["lines"] for lg in logs]),
                                     "  ".join("[%s] %.1f" % (m, v) for v, m in items[:LOG_TOP]))


def print_stages(result):
    rows = [r for r in result["rows"] if r["kind"] == "sample"]
    if not rows:
        return
    print("\n服务端分段（p50，ms；每行按耗时从大到小，最多 %d 段、不列 <%.1f ms 的段；分段之间有包含关系，"
          "不能相加。total 是服务端总计）" % (STAGE_TOP, STAGE_MIN_MS))
    if result["stack"] == "criu":
        print("  CRIU 统计：时间单位 ms，页数按 4 KiB 页；frozen 是进程被冻住的总时长，含 memdump；"
              "memwrite 是把页写进镜像文件")
        if any(r.get("ckpt2_criulog") for r in rows):
            print("  CRIU 日志：相邻两行日志的间隔记到前一行的消息上（数字写成 #），列耗时最多的 %d 项" % LOG_TOP)
    for series, want in STAGE_POINTS:
        rs = [r for r in rows if match(r, series, want)]
        if not rs:
            continue
        print("  %s %s（%d 次）" % (series, point_label({**norm({k: v for k, v in rs[0].items() if k in DIMS}),
                                                         "N": rs[0].get("N", 1)}), len(rs)))
        for key, label in STAGE_OPS:
            st = stage_p50(rs, key)
            tot = [server_total(r, key) for r in rs]
            tot = [v for v in tot if v is not None]
            parts = sorted(((v, k) for k, v in st.items() if v >= STAGE_MIN_MS), reverse=True)[:STAGE_TOP]
            print("    %-14s total %6s | %s" % (label, "%.1f" % statistics.median(tot) if tot else "-",
                                               "  ".join("%s %.1f" % (k, v) for v, k in parts) or "-"))
            crit = criu_p50(rs, key.replace("_server", "_criu"))
            if crit:
                print("    %-14s CRIU 统计 | %s" % ("", crit))
            lg = criulog_p50(rs, key.replace("_server", "_criulog"))
            if lg:
                print("    %-14s CRIU 日志 | %s" % ("", lg))


# ------------------------------------------------------------ 多轮合并与两套并列

def load_results(paths):
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("rows") is None or not data.get("stack"):
            continue           # 不是测量结果（例如并列步骤自己的输出）
        data["_path"] = p
        out.append(data)
    return out


def merge(results):
    """同一套的多轮合并成一份：行拼起来（每行记 round），身份取第一轮，判定全部 PASS 才 PASS。"""
    first = results[0]
    rows = []
    for i, res in enumerate(results):
        for r in res["rows"]:
            rows.append(dict(r, round=i))
    shas = sorted({(r["provenance"].get("orchestrator_sha256") or "?")[:16] for r in results})
    merged = {"schema_version": first.get("schema_version", 1), "stack": first["stack"], "template": first["template"],
              "provenance": dict(first["provenance"]), "rows": rows, "rounds": len(results),
              "verdict": "PASS" if all(r.get("verdict") == "PASS" for r in results) else "FAIL",
              "sources": [r["_path"] for r in results]}
    if len(shas) > 1:
        merged["provenance"]["orchestrator_sha256"] = "多个:" + ",".join(shas)
    merged["summary"] = summarize(rows)
    return merged


def compare_table(results):
    """两套并列：按测点对齐，列客户端墙钟 p50 / p99（最近秩）。返回 (按套合并后的结果, 表行)。"""
    by_stack = {}
    for r in results:
        by_stack.setdefault(r["stack"], []).append(r)
    merged = {st: merge(rs) for st, rs in by_stack.items()}
    s, c = merged.get("sandbox"), merged.get("criu")
    if not s or not c:
        raise SystemExit("--compare 要两套的结果都有（现在只有 %s）" % ",".join(merged))
    index = {}
    for side, res in (("s", s), ("c", c)):
        for e in res["summary"]:
            k = (e["series"], e["N"], e["name"]) + tuple(e[d] for d in DIMS)
            index.setdefault(k, {})[side] = e
    table = []
    for k, sides in index.items():
        if "s" in sides and "c" in sides:
            table.append({"series": k[0], "label": point_label(sides["s"]), "s": sides["s"], "c": sides["c"]})
    return merged, table


def mix_rows(table):
    rows = [t for t in table if t["series"] == "mix"]
    return sorted(rows, key=lambda t: (t["s"]["d"] + t["s"]["f"], t["s"]["d"]))


def print_mix_table(merged, table):
    """对外展示的分档表：两套 checkpoint / restore 与两套代码上的原生 pause / resume 并排。"""
    rows = mix_rows(table)
    if not rows:
        return
    head = ("总改动MiB", "内存MiB", "文件MiB", "沙箱级checkpoint", "进程级checkpoint", "沙箱级restore", "进程级restore",
            "E2B pause(沙箱级代码)", "E2B resume(沙箱级代码)", "E2B pause(进程级代码)", "E2B resume(进程级代码)")
    print("\n---- 对外分档表（mix）----")
    print("进程 256 MiB；checkpoint 为连续 checkpoint，restore 回到改写之前的基点；E2B pause / resume 在同一负载上另起"
          "沙箱测，resume = connect 返回 + 第一条命令。客户端墙钟，单位 ms。")
    for q in ("p50", "p99"):
        print("\n[%s]" % q)
        print("\t".join(head))
        for t in rows:
            a, b = t["s"], t["c"]
            cells = ["%g" % (a["d"] + a["f"]), "%g" % a["d"], "%g" % a["f"]]
            for side, key in ((a, "ckpt2_ms"), (b, "ckpt2_ms"), (a, "restore_ms"), (b, "restore_ms"),
                              (a, "pause_ms"), (a, "resume_ms"), (b, "pause_ms"), (b, "resume_ms")):
                cells.append(fmt(side.get(key), q))
            print("\t".join(cells))
    a, b = rows[0]["s"], rows[0]["c"]
    print("\n样本数（每档）：checkpoint / restore 沙箱级 %d、进程级 %d；pause / resume 沙箱级 %s、进程级 %s"
          % (a["n"], b["n"], a.get("native_n", "-"), b.get("native_n", "-")))
    for st in ("sandbox", "criu"):
        prov = merged[st]["provenance"]
        print("%s：orchestrator %s · 脏页后端 %s · %s" % ("沙箱级" if st == "sandbox" else "进程级",
                                                    (prov.get("orchestrator_sha256") or "?")[:16],
                                                    prov.get("dirty_tracking") or "?", prov.get("hostname")))


def print_compare(merged, table):
    print("\n---- 两套并列（代价分解表）----")
    for st in ("sandbox", "criu"):
        res = merged[st]
        prov = res["provenance"]
        print("%-7s %d 轮 · 宿主 %s · orchestrator %s · 模板 %s · 脏页后端 %s" % (
            st, res["rounds"], prov.get("hostname"), (prov.get("orchestrator_sha256") or "?")[:16], res["template"],
            prov.get("dirty_tracking") or "?"))
        print_host(prov, "        ")
    print("单位 ms，客户端墙钟；p99 按最近秩取（n 小于 100 时接近最大值）；* 标出 p50 更低的一侧；"
          "S = 沙箱级，C = 进程级")
    order = series_order([{"series": t["series"]} for t in table])
    for series in order:
        rows = [t for t in table if t["series"] == series]
        print("\n%s" % TITLES.get(series, series))
        print("%-30s %7s | %-27s | %-27s | %-27s | %-15s" % (
            "测点", "n S/C", "连续 ckpt p50 S/C（p99 S/C）", "restore 后 ckpt p50 S/C", "restore p50 S/C（p99 S/C）",
            "全量基点 S/C"))
        for t in rows:
            a, b = t["s"], t["c"]

            def pair(key, q="p50"):
                x, y = a.get(key), b.get(key)
                if not x or not y:
                    return "%6s / %-6s" % (fmt(x, q), fmt(y, q))
                star = ("*", " ") if x[q] < y[q] else (" ", "*") if y[q] < x[q] else (" ", " ")
                return "%6.1f%s/ %6.1f%s" % (x[q], star[0], y[q], star[1])
            print("%-30s %3d/%-3d | %s (%5s/%-5s) | %s       | %s (%5s/%-5s) | %s" % (
                t["label"], a["n"], b["n"],
                pair("ckpt2_ms"), fmt(a.get("ckpt2_ms"), "p99"), fmt(b.get("ckpt2_ms"), "p99"),
                pair("ckpt_ms"),
                pair("restore_ms"), fmt(a.get("restore_ms"), "p99"), fmt(b.get("restore_ms"), "p99"),
                pair("base_ckpt_ms")))
    print_mix_table(merged, table)


# ------------------------------------------------------------ 主流程

def host_conditions():
    """宿主条件：NUMA 节点与大小、numa_balancing、THP、内核命令行。读不到的项为 None。"""
    def rd(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None
    numa = None
    try:
        numa = subprocess.run(["numactl", "-H"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    nodes = []
    for line in (numa or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "node" and parts[2] == "size:":
            nodes.append("%s:%s" % (parts[1], parts[3]))
    if not nodes:      # 没装 numactl：从 sysfs 读各节点的 MemTotal
        for p in sorted(glob.glob("/sys/devices/system/node/node[0-9]*"), key=lambda x: int(x.rsplit("node", 1)[1])):
            size = None
            for line in (rd(os.path.join(p, "meminfo")) or "").splitlines():
                if "MemTotal:" in line:
                    size = int(line.split()[-2]) // 1024
            nodes.append("%s:%s" % (p.rsplit("node", 1)[1], size if size is not None else "?"))
    thp = rd("/sys/kernel/mm/transparent_hugepage/enabled")
    return {"numa_balancing": rd("/proc/sys/kernel/numa_balancing"),
            "thp": thp.split("[")[1].split("]")[0] if thp and "[" in thp else thp,
            "numa_summary": "%d 个节点（MiB：%s）" % (len(nodes), " ".join(nodes)) if nodes else None,
            "numactl": numa, "cmdline": rd("/proc/cmdline")}


def series_runs(args, series):
    passes, reps = SERIES_RUNS.get(series, (2, 11))
    if args.smoke:
        return 1, 3
    return (args.passes if args.passes is not None else passes), (args.reps if args.reps is not None else reps)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--series", default=DEFAULT_SERIES, help="逗号分隔；all = %s" % ALL_SERIES)
    ap.add_argument("--scenarios", help="场景文件（JSON），见下面的格式说明")
    ap.add_argument("--passes", type=int, help="每个测点几个沙箱（默认 2；filewait 默认 1）")
    ap.add_argument("--reps", type=int, help="每个沙箱几次（默认 11；filewait 默认 6）")
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--conc", default=",".join(map(str, CONC)), help="conc 组的 N 列表")
    ap.add_argument("--conc-reps", type=int, default=6)
    ap.add_argument("--stack", choices=("sandbox", "criu"))
    ap.add_argument("--template")
    ap.add_argument("--criu-logs", action="store_true", help="进程级：每次测量后读 CRIU 的 dump/restore 日志做归因")
    ap.add_argument("--criu-verbosity", type=int, choices=range(0, 5),
                    help="进程级：guest 里给 criu 套包装脚本，dump/restore 都改用 -vN（测日志级别本身的代价）")
    ap.add_argument("--out", default="same-workload-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--print", dest="reprint", nargs="+", metavar="RESULT_JSON",
                    help="只重新打印已有结果的汇总；同一套的多个文件合并成一份")
    ap.add_argument("--compare", nargs="+", metavar="RESULT_JSON", help="两套的结果按测点并列（每套可多轮）")
    ap.add_argument("--compare-siblings", action="store_true",
                    help="与 --out 合用：并列 --out 所在目录上一级里其他各步的 result.json")
    ap.epilog = SCENARIO_HELP
    args = ap.parse_args()
    if args.reprint:
        results = load_results(args.reprint)
        if len({r["stack"] for r in results}) != 1:
            raise SystemExit("--print 的文件要是同一套的结果；两套并列用 --compare")
        print_summary(merge(results) if len(results) > 1 else results[0])
        return 0
    if args.compare or args.compare_siblings:
        paths = args.compare or []
        if args.compare_siblings:
            mine = os.path.abspath(args.out)
            parent = os.path.dirname(os.path.dirname(mine))
            paths += sorted(p for p in glob.glob(os.path.join(parent, "*", "result.json"))
                            if os.path.abspath(p) != mine and "-old-" not in p)
        merged, table = compare_table(load_results(paths))
        print_compare(merged, table)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"verdict": "PASS", "sources": paths, "compare": table}, f, ensure_ascii=False, indent=1)
        print("\n结果文件: %s" % os.path.abspath(args.out))
        return 0
    series_list = ALL_SERIES.split(",") if args.series == "all" else [s for s in args.series.split(",") if s]
    scenarios = load_scenarios(args.scenarios) if args.scenarios else []
    if scenarios and "scen" not in series_list:
        series_list.append("scen")
    if "scen" in series_list and not scenarios:
        raise SystemExit("--series 含 scen 但没给 --scenarios\n" + SCENARIO_HELP)
    unknown = [s for s in series_list if s not in SERIES and s not in ("conc", "scen")]
    if unknown:
        raise SystemExit("不认识的测点组：%s（可选 %s,scen）" % (",".join(unknown), ALL_SERIES))
    exe, env = orchestrator_proc()
    stack = args.stack or detect_stack(exe)
    args.template = args.template or ("base-criu" if stack == "criu" else "base")
    if args.smoke:
        args.warmup, args.conc, args.conc_reps = 1, "1,2", 3
    store = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build", "checkpoints") if env else None
    from e2b import Sandbox

    result = {"schema_version": 2, "stack": stack, "template": args.template,
              "provenance": {"hostname": socket.gethostname(), "kernel": os.uname().release,
                             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             "script_sha256": sha256_file(os.path.abspath(__file__)),
                             "orchestrator_exe": exe, "orchestrator_sha256": sha256_file(exe) if exe else None,
                             "FC_TRACK_DIRTY_PAGES": env.get("FC_TRACK_DIRTY_PAGES"), "args": vars(args),
                             "host_conditions": host_conditions(), "scenarios": scenarios},
              "rows": [], "summary": [], "verdict": "FAIL"}
    print_host(result["provenance"])
    cache = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build") if env else None
    storage = None
    if env and env.get("STORAGE_PROVIDER", "Local") == "Local":
        storage = env.get("LOCAL_TEMPLATE_STORAGE_BASE_PATH") or "/tmp/templates"
    ctx = Ctx(args, stack, store, cache, storage)
    ctx.prov = result["provenance"]
    lock = threading.Lock()
    failure = None
    try:
        for series in series_list:
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
            points = scenarios if series == "scen" else SERIES[series]
            if args.smoke:
                # mix 取一个有内存又有文件的测点和最后那个文件不足 1 MiB 的测点
                points = [points[1], points[-1]] if series == "mix" else points[:1]
            passes, reps = series_runs(args, series)
            for point in points:
                for pass_no in range(passes):
                    one_sandbox(Sandbox, ctx, point, series, pass_no, reps, args.warmup, result["rows"], lock)
                    if series in NATIVE_SERIES:
                        native_sandbox(Sandbox, ctx, point, series, pass_no, reps - args.warmup, result["rows"], lock)
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
