#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
checkpoint / restore 功能正确性 + API 耗时。

要证的是一句话：**回滚之后，沙箱还是回滚那一刻的那个沙箱，而且还能接着干活。**

拆成四件事，对应四类断言：

  一、checkpoint、restore 做完，沙箱照常执行操作
      每次快照后、每次恢复后都跑一遍活体检查：命令能跑、文件能读写、能起新进程、
      快照前就在跑的后台进程还在往前跑。注意「还在往前跑」只断言在推进，不断言速率：
      恢复后头一两秒那个 0.2 秒一拍的循环偶尔会慢下来，而恢复本身把 guest 的单调时钟
      也拨回了快照那一刻，所以时间和节奏都不能拿来当活体判据。

  二、内存和文件都要真的回到目标时刻
      内存看两处：/dev/shm（tmpfs，纯内存）里的标记，和一块上百 MB blob 的 md5 ——
      一页对上不算数，要一大片都对上。文件看根文件系统上的标记和一个几十 MB 文件的 md5。
      再加一条最硬的证据：快照前起的后台进程，恢复后 PID 和启动时刻都不变 ——
      说明是内存被搬回去了，不是虚机重启了。

  三、对**根目录**的操作也要能回滚
      不只是 /home/user。在 / 下建目录和文件、改 /etc 下的文件、删文件、改权限位，
      回滚之后：那一刻有的必须在、那一刻没有的必须不在、被删的必须回来、
      权限位必须是那一刻的值。新建能回滚不稀奇，**删除和改属性能回滚**才说明是
      块级快照而不是补文件。

  四、API 调用计时
      每一次 checkpoint.create / checkpoint.restore 都掐表，末尾按类型给出
      次数 / p50 / min / max，中间也逐条打出来。计的是客户端墙钟：从 SDK 发出请求
      到拿到回应，包含网络往返和服务端全部工作。

      这里的耗时都来自正确性验证自身的调用：样本少、每代现场又大
      （上百 MB），只当量级看。性能基准 —— 增量档位、内存 / 文件系统 /
      混合三段分开量、服务端分段 —— 在 checkpoint_bench.py 里。正确性和
      性能分开跑：正确性现场不给性能垫噪声，性能档位也不拖慢正确性。

更复杂的树形语义（分叉、跨分支、删除语义、失败语义）不在这个脚本里，在
rollback/test-950/correctness.py。这里只走一条直链，图的是好读、跑得快。

依赖（与本目录其它脚本一致）:
    pip install e2b==2.20.0 python-dotenv
    python /opt/e2b-infra/patch_e2b.py
    python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py   # checkpoint/restore 的 SDK 覆盖层

环境变量（可放在当前目录 .env 里，用 sync-env.sh 同步）:
    E2B_API_KEY / E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL

用法:
    python checkpoint_verify.py
    python checkpoint_verify.py --mem-mb 256 --disk-mb 64 --rounds 5
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

ROOT_DIR = "/ckpt-root"              # 根目录下的测试目录
ETC_FILE = "/etc/ckpt-root.conf"     # 系统目录里的一个文件
HB_LOG = "/tmp/ckpt-hb.log"          # 后台心跳的输出
HB_PID = "/tmp/ckpt-hb.pid"

# 三代现场。每代都换掉全部标记，另外各自动一样"不好回滚"的东西：
#   A 建 victim，B 删掉它，C 保持删掉 —— 于是回到 A 必须让它复活。
#   权限位每代不同 —— 于是回滚必须连元数据一起回。
GENS = [
    ("A", "600", "create"),
    ("B", "640", "delete"),
    ("C", "755", "keep"),
]


# ---------------------------------------------- 宿主机侧探测（本次跑在什么上）
#
# 这两个函数和 checkpoint_bench.py 里的是同一份。宁可重复也不 import：本目录的脚本
# 是一个个单独拷到目标机上跑的，跨文件依赖会在版本对不齐时**静默降级成"未知"**，
# 而不是报错——已经踩过一次了。

def fc_get(sandbox_id, path="/"):
    """向这个沙箱的 Firecracker API 套接字发一个 GET。

    只有在**宿主机上**跑本脚本时才拿得到（套接字是本地文件）。"""
    hits = glob.glob(os.path.join(tempfile.gettempdir(), "fc-%s-*.sock" % sandbox_id))
    if not hits:
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(hits[0])
        s.sendall(("GET %s HTTP/1.1\r\nHost: localhost\r\n"
                   "Accept: application/json\r\n\r\n" % path).encode())
        # Firecracker 走 keep-alive 不主动关连接，按 Content-Length 读够就停，
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

    读的是 orchestrator 进程实际拿到的 environ，不是配置文件：配置只说"本该是什么"，
    进程才知道"实际是什么"，这两者分岔过不止一次。"""
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


# ---------------------------------------------------------------- 终端排版

def width(s):
    """终端里的显示宽度。中文占两列，按字符数补空格会把表格顶歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def lpad(s, n):
    return s + " " * max(0, n - width(s))


def rpad(s, n):
    return " " * max(0, n - width(s)) + s


def clip(s, n):
    """按显示宽度截断，超出的用 … 收尾。"""
    if width(s) <= n:
        return s
    out = ""
    for c in s:
        if width(out) + width(c) > n - 1:
            break
        out += c
    return out + "…"


def show_scene(k, v):
    """只影响对照表的显示：目录列表带一堆 ckpt- 前缀，排不下也没信息量。
    比对用的始终是完整值。"""
    if k in ("root_top", "root_ls"):
        return v.rstrip(",").replace("ckpt-", "")
    return v


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


# ------------------------------------------------------------------ 断言

class Check:
    def __init__(self):
        self.total = 0
        self.fails = []

    def __call__(self, cond, what, detail=""):
        self.total += 1
        print("    [%s] %s" % ("PASS" if cond else "FAIL", what))
        if not cond:
            if detail:
                print("           ↳ %s" % detail)
            self.fails.append(what)
        return bool(cond)


# ------------------------------------------------------ guest 里跑的三段脚本

# 读现场：一次往返读全所有观测点，省 envd round-trip。
# 每一项都带 || echo 兜底，读不到就是 MISSING，不会让整条命令挂掉。
SCENE_SH = r"""
p=$(cat __PID__ 2>/dev/null || echo 0)
echo "mem_marker=$(cat /dev/shm/marker 2>/dev/null || echo MISSING)"
echo "mem_blob=$(md5sum /dev/shm/blob 2>/dev/null | cut -c1-12)"
echo "root_top=$(ls -1 / | grep '^ckpt-' | sort | tr '\n' ',')"
echo "root_ls=$(ls -1 __D__ 2>/dev/null | sort | tr '\n' ',')"
echo "root_marker=$(cat __D__/marker 2>/dev/null || echo MISSING)"
echo "root_blob=$(md5sum __D__/blob 2>/dev/null | cut -c1-12)"
echo "victim=$(test -e __D__/victim && echo 在 || echo 不在)"
echo "mode=$(stat -c %a __D__/mode 2>/dev/null || echo MISSING)"
echo "etc_conf=$(cat __E__ 2>/dev/null || echo MISSING)"
echo "hb_pid=$p"
echo "hb_start=$(cut -d' ' -f22 /proc/$p/stat 2>/dev/null || echo MISSING)"
"""

# 立现场：把这一代该有的样子写下去。全部落在根文件系统和 tmpfs 上。
STAMP_SH = r"""
mkdir -p __D__
echo __G__ > /dev/shm/marker
dd if=/dev/urandom of=/dev/shm/blob bs=1M count=__M__ 2>/dev/null
echo __G__ > __D__/marker
dd if=/dev/urandom of=__D__/blob bs=1M count=__K__ 2>/dev/null
echo __G__ > __E__
touch __D__/mode && chmod __P__ __D__/mode
touch /ckpt-__G__
sync
"""

# 活体检查：沙箱现在还能不能正常干活。
#
# 心跳分两段数，是因为**只数一段会误报**：恢复后紧接着的一两秒里，那个 0.2 秒一拍的
# 循环偶尔只跳一两下（见过 1.5s 只 +1 行，同一位置上一轮是 +8）。进程明明在推进，
# 判成失败并说"vCPU 没恢复"是错的诊断。所以硬断言只要求**在推进**，节奏打出来给人看。
#
# 也不能拿"墙钟往前走"当活体判据：恢复会把 guest 的单调时钟一起拨回快照那一刻
# （实测 /proc/uptime 6.77 → 1.70），时间倒流是对的行为，不是故障。
LIVE_SH = r"""
echo "cmd=$(echo alive)"
echo "arith=$(python3 -c 'print(sum(range(1,1000001)))' 2>/dev/null || echo NOPY)"
rm -f /live-probe; echo __N__ > /live-probe
echo "rw=$(cat /live-probe 2>/dev/null || echo MISSING)"
rm -f /live-probe
echo "newproc=$(sh -c 'echo ok')"
echo "hb0=$(wc -l < __L__ 2>/dev/null || echo 0)"
sleep 1.5
echo "hb1=$(wc -l < __L__ 2>/dev/null || echo 0)"
sleep 1.5
echo "hb2=$(wc -l < __L__ 2>/dev/null || echo 0)"
"""


def fill(tpl, **kw):
    for k, v in kw.items():
        tpl = tpl.replace("__%s__" % k, str(v))
    return tpl


# ------------------------------------------------------------------- 本体

# (键, 打印失败时用的长说明, 对照表里用的短名)
SCENE_LABEL = [
    ("mem_marker", "内存 /dev/shm/marker（tmpfs＝纯内存）", "内存标记"),
    ("mem_blob", "内存大块 /dev/shm/blob 的 md5", "内存 blob md5"),
    ("root_top", "根目录 / 下 ckpt-* 的条目", "/ 下 ckpt-*"),
    ("root_ls", "%s 下的条目" % ROOT_DIR, "%s 下" % ROOT_DIR),
    ("root_marker", "%s/marker 的内容" % ROOT_DIR, "根目录标记"),
    ("root_blob", "根文件系统大文件 %s/blob 的 md5" % ROOT_DIR, "根 blob md5"),
    ("victim", "%s/victim（删除是否回滚）" % ROOT_DIR, "victim 在不在"),
    ("mode", "%s/mode 的权限位（元数据是否回滚）" % ROOT_DIR, "权限位"),
    ("etc_conf", "%s 的内容（系统目录）" % ETC_FILE, "/etc 下的文件"),
    ("hb_pid", "后台心跳进程的 PID", "心跳 PID"),
    ("hb_start", "后台心跳进程的启动时刻", "心跳启动时刻"),
]

# 心跳那两项在三代之间本来就该相同 —— 它们不是"现场"，是"同一个进程没被重启"的证据。
CONSTANT_KEYS = ("hb_pid", "hb_start")


class Box:
    def __init__(self, sbx, mem_mb, disk_mb, check):
        self.sbx = sbx
        self.mem_mb = mem_mb
        self.disk_mb = disk_mb
        self.check = check
        self.cks = {}            # 代名 -> checkpoint_id
        self.scenes = {}         # 代名 -> 那一刻的现场
        self.api = []            # (操作, 标签, 秒) —— 要求四的原始数据
        self.probe = 0

    def run(self, cmd, timeout=180):
        """一律以 root 跑：这个脚本要写 / 和 /etc。"""
        return self.sbx.commands.run(cmd, user="root", timeout=timeout).stdout

    def kv(self, out):
        d = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
        return d

    # ---------------------------------------------------------- 现场

    def start_heartbeat(self):
        """起一个后台进程，快照前就在跑，之后一直跑。

        用 setsid 脱出 envd 给这条命令建的进程组，否则命令一结束它就被一起收走了。
        进程自己把 pid 写出来（$$ 就是这个 sh 的 pid），比事后 pgrep 猜要准。"""
        self.run(
            "rm -f %s %s\n"
            "setsid sh -c 'echo $$ > %s; while true; do echo tick >> %s; sleep 0.2; done'"
            " >/dev/null 2>&1 </dev/null &\n"
            "sleep 0.5\n"
            "cat %s" % (HB_LOG, HB_PID, HB_PID, HB_LOG, HB_PID), timeout=60)

    def stamp(self, gen, mode, victim):
        self.run(fill(STAMP_SH, D=ROOT_DIR, E=ETC_FILE, G=gen,
                      M=self.mem_mb, K=self.disk_mb, P=mode), timeout=600)
        if victim == "create":
            self.run("touch %s/victim && sync" % ROOT_DIR, timeout=60)
        elif victim == "delete":
            self.run("rm -f %s/victim && sync" % ROOT_DIR, timeout=60)

    def scene(self):
        return self.kv(self.run(fill(SCENE_SH, D=ROOT_DIR, E=ETC_FILE, PID=HB_PID),
                                timeout=300))

    # ---------------------------------------------------------- API 调用

    def checkpoint(self, gen):
        t0 = time.monotonic()
        ck = self.sbx.checkpoint.create(name="g" + gen)
        dt = time.monotonic() - t0
        self.api.append(("create", "g" + gen, dt))
        self.cks[gen] = ck.checkpoint_id
        self.scenes[gen] = self.scene()
        mode = getattr(ck, "mem_mode", None) or "?"
        print("  checkpoint g%s   %s   mem_mode=%s   id=%s"
              % (gen, rpad("%.3f s" % dt, 9), mode, ck.checkpoint_id))
        return dt, mode

    def restore(self, gen, why=""):
        t0 = time.monotonic()
        ok = self.sbx.checkpoint.restore(self.cks[gen])
        dt = time.monotonic() - t0
        self.api.append(("restore", "→g" + gen, dt))
        print("  restore   g%s   %s   %s" % (gen, rpad("%.3f s" % dt, 9), why))
        return dt, bool(ok)

    # ---------------------------------------------------------- 判定

    def verify_scene(self, gen, ok):
        """恢复之后，现场必须逐项等于建那一代快照时记下的现场。

        逐项比而不是比一个总的哈希：不一致时要能直接说出**是内存没回来还是文件没回来**，
        一个总哈希只会告诉你"不一样"。"""
        self.check(ok, "restore g%s 的 API 返回成功" % gen)
        now = self.scene()
        want = self.scenes[gen]
        bad = [lab for k, lab, _ in SCENE_LABEL if now.get(k) != want.get(k)]
        for k, lab, _ in SCENE_LABEL:
            if now.get(k) != want.get(k):
                self.check(False, "回到 g%s：%s" % (gen, lab),
                           "期望 %r，实际 %r" % (want.get(k), now.get(k)))
        if not bad:
            self.check(True, "回到 g%s：%d 项现场全部一致（内存 / 根文件系统 / 进程）"
                       % (gen, len(SCENE_LABEL)))
        return not bad

    def verify_live(self, when):
        """沙箱现在还能不能正常干活。"""
        self.probe += 1
        nonce = "probe%d" % self.probe
        d = self.kv(self.run(fill(LIVE_SH, N=nonce, L=HB_LOG), timeout=300))
        self.check(d.get("cmd") == "alive", "%s：还能执行命令" % when,
                   "echo 返回 %r" % d.get("cmd"))
        self.check(d.get("rw") == nonce, "%s：根文件系统能写能读回" % when,
                   "写 %r 读回 %r" % (nonce, d.get("rw")))
        self.check(d.get("newproc") == "ok", "%s：还能起新进程" % when)
        if d.get("arith") == "NOPY":
            print("           （模板里没有 python3，跳过算术这项）")
        else:
            self.check(d.get("arith") == "500000500000", "%s：新进程能算出正确结果" % when,
                       "得到 %r" % d.get("arith"))
        try:
            g1 = int(d.get("hb1", 0)) - int(d.get("hb0", 0))
            g2 = int(d.get("hb2", 0)) - int(d.get("hb1", 0))
        except ValueError:
            g1 = g2 = -1
        self.check(g1 + g2 >= 1, "%s：快照前就在跑的后台进程仍在推进" % when,
                   "3 秒里一行都没多 —— 进程没了，或者 vCPU 没恢复运行")
        slow = "   ← 偏慢（恢复后头一两秒偶发，只要在推进就不判失败）" if min(g1, g2) < 4 else ""
        print("           （心跳 0.2s 一拍，满速约 +7 行/段：本次 +%d，+%d）%s" % (g1, g2, slow))


# ------------------------------------------------------------------- 环境

BACKEND_SHORT = {
    "hdbss": "HDBSS（硬件标脏）",
    "kvm-wp": "KVM 写保护（软件）",
    "off": "未开启",
}


def main():
    ap = argparse.ArgumentParser(description="checkpoint/restore 正确性 + API 耗时")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE_ID, help="模板 ID（默认 base）")
    ap.add_argument("--mem-mb", type=int, default=128, help="每代写脏的内存量 MB（默认 128）")
    ap.add_argument("--disk-mb", type=int, default=32, help="每代写的根文件系统文件 MB（默认 32）")
    ap.add_argument("--rounds", type=int, default=3, help="A/C 交替回滚的轮数（默认 3）")
    ap.add_argument("--timeout", type=int, default=3600, help="沙箱存活秒数")
    args = ap.parse_args()

    check = Check()
    print("模板 = %s   每代脏内存 = %d MB   每代根文件系统写入 = %d MB"
          % (args.template, args.mem_mb, args.disk_mb))
    sbx = Sandbox.create(template=args.template, timeout=args.timeout)
    print("沙箱 %s" % sbx.sandbox_id)

    info = fc_get(sbx.sandbox_id) or {}
    backend = info.get("dirty_tracking", "?")
    store, fstype = checkpoint_store()
    print("  脏页后端 : %s%s"
          % (BACKEND_SHORT.get(backend, "未知"),
             ("   [FC %s]" % info["vmm_version"]) if info.get("vmm_version") else ""))
    print("  产物落盘 : %s   文件系统 = %s" % (store or "未知（本脚本没跑在宿主机上？）", fstype))

    b = Box(sbx, args.mem_mb, args.disk_mb, check)
    rc = 0

    try:
        # ---- 1. 建三代现场，每代之后立刻确认还能干活 -------------------
        print("\n===== 1. 建三代现场 =====")
        print("  每代都换掉：tmpfs 标记、%d MB 内存 blob、根目录标记、%d MB 根文件系统文件、"
              % (args.mem_mb, args.disk_mb))
        print("  /etc 下的文件、/ 下的一个新文件、一个文件的权限位。")
        print("  另外 A 建 victim、B 删掉它 —— 回到 A 时它必须复活。")
        b.start_heartbeat()
        print("  后台心跳已起（快照前就在跑，用来证明恢复的是内存不是重启）")
        mem_modes = {}
        for gen, mode, victim in GENS:
            print("\n  -- 第 %s 代 --" % gen)
            b.stamp(gen, mode, victim)
            _, mem_modes[gen] = b.checkpoint(gen)
            b.verify_live("g%s 快照之后" % gen)

        # 第一代是树根，必须自包含 → full；之后只写脏页 → incremental。
        # 增量档被报成 full 说明脏页跟踪没开，数字和语义都不是我们要测的那套。
        if "?" in mem_modes.values():
            print("\n  （服务端没有报 mem_mode 字段，跳过全量/增量的判定）")
        else:
            check(mem_modes["A"] == "full", "第一代 gA 是全量捕获（mem_mode=%s）" % mem_modes["A"])
            check(all(mem_modes[g] == "incremental" for g in ("B", "C")),
                  "gB / gC 是增量（mem_mode=%s / %s）" % (mem_modes["B"], mem_modes["C"]),
                  "增量档被报成 full —— 查 template-manager 的 FC_TRACK_DIRTY_PAGES")

        # 校验有没有区分度。后面每次恢复都会报"11 项现场全部一致"，可要是这些项在三代
        # 之间**本来就一样**，那句 PASS 就什么也没证明 —— 一个永远返回真的测试比没有测试
        # 更糟。所以先把三代现场摆出来，逐项确认它确实会随代变化。
        print("\n  -- 三代现场对照 --")
        print("  这些项在三代之间必须各不相同，后面「恢复后与目标代一致」才是有意义的断言。")
        print()
        print("  %s %s %s %s" % (lpad("项", 20), lpad("gA", 25), lpad("gB", 25), lpad("gC", 25)))
        print("  " + "-" * 96)
        for k, _, short in SCENE_LABEL:
            vals = [clip(show_scene(k, b.scenes[g].get(k, "?")), 24) for g in ("A", "B", "C")]
            tail = "   ← 三代同值（应该的）" if k in CONSTANT_KEYS else ""
            print("  %s %s %s %s%s"
                  % (lpad(short, 20), lpad(vals[0], 25), lpad(vals[1], 25),
                     lpad(vals[2], 25), tail))
        print("  " + "-" * 96)
        print("  （两行目录列表为了排得下省掉了 ckpt- 前缀；比对用的是完整值。）")

        flat = [(k, lab) for k, lab, _ in SCENE_LABEL if k not in CONSTANT_KEYS
                and len({b.scenes[g].get(k) for g in ("A", "B", "C")}) == 1]
        check(not flat,
              "%d 项现场在三代之间确实会变（校验有区分度）"
              % (len(SCENE_LABEL) - len(CONSTANT_KEYS)),
              "这些项三代同值，比对它们证明不了任何事：%s" % [lab for _, lab in flat])
        check(all(len({b.scenes[g].get(k) for g in ("A", "B", "C")}) == 1
                  for k in CONSTANT_KEYS),
              "心跳进程三代同 PID、同启动时刻（快照没有重启虚机）")

        # ---- 2. 逐级回退，每退一代校验一次全部现场 ---------------------
        print("\n===== 2. 逐级回退 C → B → A =====")
        print("  每退一代，把 %d 项现场逐项和建那一代时记下的比对。" % len(SCENE_LABEL))
        first = True
        for gen in ("B", "A"):
            _, ok = b.restore(gen, "退一代")
            b.verify_scene(gen, ok)
            if first:                       # 活体检查要 1.5s，第一次做足即可
                b.verify_live("g%s 恢复之后" % gen)
                first = False

        # ---- 3. 前滚 ---------------------------------------------------
        print("\n===== 3. 前滚 A → C =====")
        print("  回滚不是只能往回走：站在 A 上直接跳到链尾 C，A 之后的改动都要重新出现。")
        _, ok = b.restore("C", "一次跨 2 代前滚")
        b.verify_scene("C", ok)
        b.verify_live("gC 前滚之后")

        # ---- 4. 反复横跳，看会不会跳着跳着就不对了 ---------------------
        print("\n===== 4. A / C 交替 %d 轮 =====" % args.rounds)
        print("  一次对不算数。来回跳，每次都全量校验现场 —— 状态要是有泄漏或者累积")
        print("  误差，跳几次就会露出来。")
        for i in range(args.rounds):
            for gen in ("A", "C"):
                _, ok = b.restore(gen, "第 %d 轮" % (i + 1))
                b.verify_scene(gen, ok)
        b.verify_live("反复回滚之后")

        # ---- 5. 进程死了，也要能跟着快照活回来 -------------------------
        print("\n===== 5. 杀掉心跳进程，再回滚 =====")
        print("  kill -9 掉那个快照前就在跑的心跳进程，再恢复到最早的 gA ——")
        print("  它必须连同 PID、启动时刻一起活回来。能复活一个已经被杀掉的进程，")
        print("  才说明恢复回来的是完整的内存现场，不是重放了什么操作。")
        b.run("kill -9 $(cat %s) 2>/dev/null; true" % HB_PID)
        _, ok = b.restore("A", "杀掉心跳之后")
        b.verify_scene("A", ok)
        b.verify_live("杀掉心跳再回滚之后")

        # ---- 汇总 ------------------------------------------------------
        print("\n================ API 耗时 ================")
        print("客户端墙钟：从 SDK 发出请求到拿到回应，含网络往返和服务端全部工作。")
        print()
        def pick(op, keep=None):
            return [t for o, lab, t in b.api if o == op and (keep is None or keep(lab))]

        groups = [
            ("checkpoint.create 全量（第一代）", pick("create", lambda l: l == "gA")),
            ("checkpoint.create 增量（gB / gC）", pick("create", lambda l: l != "gA")),
            ("checkpoint.restore", pick("restore")),
        ]
        print("  %s %s %s %s %s" % (lpad("操作", 40), rpad("次数", 6),
                                    rpad("p50", 10), rpad("min", 10), rpad("max", 10)))
        print("  " + "-" * 78)
        for name, xs in groups:
            if not xs:
                continue
            print("  %s %s %s %s %s"
                  % (lpad(name, 40), rpad(str(len(xs)), 6),
                     rpad("%.3f s" % median(xs), 10),
                     rpad("%.3f s" % min(xs), 10),
                     rpad("%.3f s" % max(xs), 10)))
        print("  " + "-" * 78)

        print("  每一次调用的耗时上面各节已逐条打出。性能基准（增量档位、内存 /")
        print("  文件系统 / 混合三段、服务端分段）在 checkpoint_bench.py。")

        print("\n================ 汇总 ================")
        print("跑在：脏页后端 = %s   |   产物文件系统 = %s   |   落盘路径 = %s"
              % (BACKEND_SHORT.get(backend, "未知"), fstype, store or "未知"))
        print()
        if check.fails:
            print("✗ %d / %d 项校验未通过：" % (len(check.fails), check.total))
            for f in check.fails:
                print("    - %s" % f)
            rc = 1
        else:
            print("✓ %d 项校验全部通过。" % check.total)
            print("  内存（tmpfs 标记 + %d MB blob 的 md5）、根文件系统（标记 + %d MB 文件的 md5 +"
                  % (args.mem_mb, args.disk_mb))
            print("  新建 / 删除 / 改权限）、进程（同一个 PID、同一个启动时刻）在每一次恢复后")
            print("  都回到了目标那一刻；每次快照和每次恢复之后沙箱都还能正常干活。")
        if backend == "kvm-wp":
            print("注意：本次跑在软件写保护上，不是硬件标脏，checkpoint 耗时含 VM exit 开销。")
        elif backend == "off":
            print("注意：脏页跟踪没开，checkpoint 会全部退化成全量。")
    finally:
        try:
            sbx.kill()
            print("沙箱已删除")
        except Exception:
            pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
