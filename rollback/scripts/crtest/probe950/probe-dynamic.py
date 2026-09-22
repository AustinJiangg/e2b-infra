#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""950 vs 920B 差异探测 —— 动态部分（需要 e2b 栈起着；会建 1 个沙箱，跑完必删）。

对应的问题（编号见 `checkpoint-restore-fix-plan-2026-09-17.md`）：

  步骤 1 guest      —— guest 内核/中断表/vCPU 基线，X1、S2 两条线的坐标系
  步骤 2 checkpoint —— `mem_mode`（full/incremental）与 FC `GET /` 的 `dirty_tracking`
                       （950 应是 hdbss，920B 是 kvm-wp；§0「两个环境变量与已知坑」）
  步骤 3 restore    —— E1（uptime 倒退）+ T25（sleep 1、CPU0 空闲、arch_timer 速率）
  步骤 4 vgic-state —— X1：ttyS0 SPI 在 rollback 之后是否 P=1 A=1 E=1，外加
                       三个 virtio SPI 与 PPI 27/30；再让 guest 往 ttyS0 写 4 KB 看会不会楔死（T26）
  步骤 5 写密集     —— HDBSS buffer 溢出与 4 KB 差分粒度（950 独有风险，差异清单 §3.1/3.2）：
                       16/128/512 MB 三档，记 mem_diff 实际大小 + restore 后 md5 校验
  步骤 6 收尾       —— kill 沙箱，`pgrep -c -x firecracker` 回到开跑前的值

设计约束：单文件、不 import crtest（950 上只拷这一个目录）；每一步都能单独 `--only`；
任何一步失败只记录不中断后面的步骤；总时长 ≤ 8 分钟（默认参数）。

用法（950 上先 `conda activate jll-e2b` 或直接用绝对路径的 python）：
    python3 probe-dynamic.py --env-file /opt/e2b-infra/.env
"""

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import traceback

DEFAULT_ENV_FILE = (os.environ.get("CRTEST_ENV_FILE")
                    or os.path.join(os.environ.get("E2B_DEPLOY_DIR", "/opt/e2b-infra"),
                                    ".env"))
HERE = os.path.dirname(os.path.abspath(__file__))
STEPS = ("guest", "checkpoint", "restore", "vgic", "writeheavy")

RESULT = {
    "meta": {},
    "steps": {},
    "errors": [],
}


def log(msg):
    print(msg, flush=True)


# ─────────────────────────────────────────────────────── 宿主机侧小工具

def fc_get(sandbox_id, path="/"):
    """向这个沙箱的 Firecracker API 套接字发一个 GET（照抄 crtest/common.py 的 fc_get，
    那边是复制而非 import，理由同——脚本要能单独拷到别的机器上跑）。"""
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


def orchestrator_env():
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
    _, env = orchestrator_env()
    if not env:
        return None
    return os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build", "checkpoints")


def fc_pid_of(sandbox_id):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open("/proc/%s/cmdline" % pid, "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        if "firecracker" in cmd and sandbox_id in cmd:
            return int(pid)
    return None


def fc_count():
    try:
        out = subprocess.run(["pgrep", "-c", "-x", "firecracker"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out or 0)
    except Exception:       # noqa: BLE001
        return None


def du_bytes(path):
    """(实占字节, 表观字节, {文件: {"alloc","apparent"}})。

    内存差分是稀疏文件：表观大小恒等于 guest 内存（2 GB），只有 `st_blocks * 512`
    的实占才说明这一次到底写了多少——「4 KB 写跟踪差分在 2 MB 大页上还灵不灵」
    （差异清单 §3.2）就看这个数随脏集怎么长。"""
    alloc = apparent = 0
    files = {}
    for root, _dirs, names in os.walk(path):
        for n in names:
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            a = st.st_blocks * 512
            alloc += a
            apparent += st.st_size
            files[os.path.relpath(p, path)] = {"alloc": a, "apparent": st.st_size}
    return alloc, apparent, files


# ─────────────────────────────────────────────────────── guest 侧解析（口径同 crtest/common.py）

def parse_kv(out):
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def parse_proc_stat_line(line):
    f = line.split()
    if not f or not f[0].startswith("cpu"):
        return {}
    names = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    vals = []
    for x in f[1:1 + len(names)]:
        try:
            vals.append(int(x))
        except ValueError:
            vals.append(0)
    vals += [0] * (len(names) - len(vals))
    d = dict(zip(names, vals))
    d["name"] = f[0]
    return d


def cpu_idle_ratio(a, b):
    if not a or not b:
        return None
    keys = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    total = sum(b.get(k, 0) - a.get(k, 0) for k in keys)
    if total <= 0:
        return None
    idle = (b.get("idle", 0) - a.get("idle", 0)) + (b.get("iowait", 0) - a.get("iowait", 0))
    return idle / float(total)


def parse_interrupt_counts(text):
    """若干条 `/proc/interrupts` 行按 CPU 求和，返回 [cpu0, cpu1, ...]。"""
    total = []
    for line in text.replace("|", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            line = line.split(":", 1)[1]
        counts = []
        for tok in line.split():
            if tok.isdigit():
                counts.append(int(tok))
            else:
                break
        if not counts:
            continue
        if len(counts) > len(total):
            total += [0] * (len(counts) - len(total))
        for i, c in enumerate(counts):
            total[i] += c
    return total


IRQ_RE = re.compile(r"^\s*(\d+):\s+((?:\d+\s+)+)(\S+)\s+(\d+)\s+(\S+)\s+(.*)$")


def parse_interrupt_table(text):
    """guest `/proc/interrupts` 全表 → {名字: {"line", "intid", "trigger", "counts"}}。

    行形如 ` 13:  260   0   GICv3  67 Edge   ttyS0`：GICv3 后面那个数就是 INTID，
    步骤 4 要用它去 vgic-state 里定位（X1 的 ttyS0 = SPI 67 就是这么来的）。"""
    out = {}
    for line in text.splitlines():
        m = IRQ_RE.match(line)
        if not m:
            continue
        name = m.group(6).strip()
        out[name] = {
            "line": int(m.group(1)),
            "chip": m.group(3),
            "intid": int(m.group(4)),
            "trigger": m.group(5),
            "counts": [int(x) for x in m.group(2).split()],
        }
    return out


VGIC_RE = re.compile(r"^\s*(SGI|PPI|SPI|LPI)\s+(\d+)\s+(\S+)\s+([01]{8})\s+(.*)$")


def parse_vgic_state(text):
    """debugfs 的 vgic-state → [{"vcpu", "typ", "intid", "bits", "flags", "raw"}]。

    表头写着 `PLAEHCGN`：pending_latch / line_level / active / enabled / hw /
    config(level=1,edge=0) / group / NMI。X1 的判据是 ttyS0 那条 SPI 的 P=1 A=1 E=1。"""
    rows = []
    vcpu = None
    for line in text.splitlines():
        mv = re.match(r"^\s*VCPU\s+(\d+)\s+TYP", line)
        if mv:
            vcpu = int(mv.group(1))
            continue
        m = VGIC_RE.match(line)
        if not m:
            continue
        bits = m.group(4)
        typ = m.group(1)
        rows.append({
            "vcpu": None if typ == "SPI" else vcpu,
            "typ": m.group(1),
            "intid": int(m.group(2)),
            "bits": bits,
            "flags": dict(zip("PLAEHCGN", bits)),
            "raw": line.rstrip(),
        })
    return rows


def vgic_path(fcpid):
    if fcpid is None:
        return None
    hits = sorted(glob.glob("/sys/kernel/debug/kvm/%d-*/vgic-state" % fcpid))
    return hits[0] if hits else None


def read_vgic(fcpid):
    """(状态文本 or None, 说明)。debugfs 读不到时说明里写清原因。"""
    root = "/sys/kernel/debug/kvm"
    if not os.path.isdir(root):
        return None, "/sys/kernel/debug/kvm 不存在（debugfs 没挂或内核无 CONFIG_DEBUG_FS）"
    try:
        os.listdir(root)
    except OSError as e:
        return None, "/sys/kernel/debug/kvm 不可读：%s（非 root？）" % e
    p = vgic_path(fcpid)
    if not p:
        return None, "找不到 %s/%s-*/vgic-state（FC pid 对不上或该 VM 已退出）" % (root, fcpid)
    try:
        return open(p).read(), p
    except OSError as e:
        return None, "读 %s 失败：%s" % (p, e)


# ─────────────────────────────────────────────────────── 沙箱包装

GUEST_SAMPLER = r'''
import time
print('mono=%.6f' % time.monotonic())
print('epoch=%.6f' % time.time())
print('uptime=%s' % open('/proc/uptime').read().split()[0])
st = open('/proc/stat').read().splitlines()
print('cpu=%s' % st[0])
for l in st:
    if l.startswith('cpu0 '):
        print('cpu0=%s' % l)
ti = [l.strip() for l in open('/proc/interrupts') if 'arch_timer' in l]
print('timer=%s' % '|'.join(ti))
t = time.monotonic()
time.sleep(1)
print('sleep1=%.4f' % (time.monotonic() - t))
'''


class Box(object):
    """一个沙箱。run/sh 的口径与 crtest/common.py 的 Box 一致（永远返回退出码，
    不让 SDK 的 CommandExitException 把输出吃掉）。"""

    def __init__(self, sbx):
        self.sbx = sbx
        self.id = sbx.sandbox_id

    def run(self, cmd, timeout=300):
        return self.sbx.commands.run(cmd, user="root", timeout=timeout).stdout

    def sh(self, script, timeout=300):
        out = self.sbx.commands.run("( %s\n) ; echo __rc=$?\nexit 0" % script,
                                    user="root", timeout=timeout).stdout
        rc = 0
        for line in out.splitlines():
            if line.startswith("__rc="):
                try:
                    rc = int(line.split("=", 1)[1])
                except ValueError:
                    rc = -1
        return rc, out

    def sample(self, timeout=180):
        kv = parse_kv(self.run("python3 - <<'PROBE950EOF'\n%s\nPROBE950EOF" % GUEST_SAMPLER,
                                   timeout=timeout))
        d = {"raw": kv}
        for k in ("mono", "epoch", "uptime", "sleep1"):
            try:
                d[k] = float(kv.get(k))
            except (TypeError, ValueError):
                d[k] = None
        d["cpu0"] = parse_proc_stat_line(kv.get("cpu0", ""))
        d["timer"] = parse_interrupt_counts(kv.get("timer", ""))
        return d


def load_env(path, required_ok):
    """显式路径加载 dotenv。路径不存在时：环境变量里已有 E2B_API_KEY 就放行，
    否则直接报错退出（950 上路径与 920B 不同，必须显式给 --env-file）。"""
    loaded = False
    if path and os.path.exists(path):
        try:
            from dotenv import load_dotenv
            load_dotenv(path, override=False)
        except ImportError:
            for line in open(path):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        loaded = True
    if not loaded and not os.environ.get("E2B_API_KEY") and not required_ok:
        sys.exit("找不到 env 文件 %s，环境里也没有 E2B_API_KEY。\n"
                 "950 上这份 dotenv 的路径与 920B 不同，请显式传 --env-file <路径>。" % path)
    return loaded


# ─────────────────────────────────────────────────────── 各步骤

def step_guest(box, res):
    """1. guest 基线：内核、中断全表（含各 INTID）、vCPU 数、页大小、envd 版本。"""
    out = box.run("uname -r; echo ---; nproc; echo ---; getconf PAGESIZE; echo ---; "
                  "grep -c ^processor /proc/cpuinfo; echo ---; cat /proc/cmdline")
    parts = [p.strip() for p in out.split("---")]
    res["kernel"] = parts[0] if parts else None
    res["nproc"] = parts[1] if len(parts) > 1 else None
    res["pagesize"] = parts[2] if len(parts) > 2 else None
    res["cpuinfo_count"] = parts[3] if len(parts) > 3 else None
    res["cmdline"] = parts[4] if len(parts) > 4 else None

    irq = box.run("cat /proc/interrupts")
    res["interrupts_raw"] = irq
    res["interrupts"] = parse_interrupt_table(irq)
    res["intids"] = {k: v["intid"] for k, v in res["interrupts"].items()}

    rc, out = box.sh("envd --version 2>/dev/null || /usr/bin/envd --version 2>/dev/null || "
                     "ps -eo pid,comm,args | grep -i envd | grep -v grep | head -3")
    res["envd"] = out.strip().splitlines()[:3]
    rc, out = box.sh("cat /etc/os-release | grep PRETTY_NAME; free -m | head -2")
    res["os"] = out.strip()
    log("  guest kernel=%s  vCPU=%s  pagesize=%s" % (res["kernel"], res["nproc"], res["pagesize"]))
    log("  guest INTID: %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(res["intids"].items())
                                        if k in ("ttyS0", "virtio0", "virtio1", "virtio2", "arch_timer")))


def step_checkpoint(box, res, store):
    """2. 两次 create：第一次是全量根，第二次应为增量；记 mem_mode 与 FC 自报的脏页后端。"""
    info = fc_get(box.id) or {}
    res["fc_get"] = info
    res["dirty_tracking"] = info.get("dirty_tracking", "<absent>")
    res["vmm_version"] = info.get("vmm_version", "<absent>")
    log("  FC dirty_tracking=%s  vmm_version=%s" % (res["dirty_tracking"], res["vmm_version"]))

    res["creates"] = []
    for i in range(2):
        t0 = time.monotonic()
        ck = box.sbx.checkpoint.create(name="probe950-cp%d" % i)
        wall = time.monotonic() - t0
        rec = {"n": i, "id": ck.checkpoint_id, "wall_s": wall,
               "mem_mode": getattr(ck, "mem_mode", None) or "?"}
        if store:
            d = os.path.join(store, box.id, ck.checkpoint_id)
            rec["dir"] = d
            alloc, apparent, files = du_bytes(d)
            rec["dir_alloc_bytes"] = alloc
            rec["dir_apparent_bytes"] = apparent
            rec["files"] = files
            try:
                rec["timings"] = json.load(open(os.path.join(d, "timings.json")))
            except (OSError, ValueError):
                rec["timings"] = None
        res["creates"].append(rec)
        log("  create #%d %s  mem_mode=%s  %.3f s  产物实占 %s B（表观 %s B）"
            % (i, ck.checkpoint_id, rec["mem_mode"], wall,
               rec.get("dir_alloc_bytes", "?"), rec.get("dir_apparent_bytes", "?")))
    return res["creates"][0]["id"]


def step_restore(box, res, ck_id, store, settle):
    """3. restore 之后的 E1（uptime 倒退）与 T25（sleep 1 / CPU0 空闲 / arch_timer 速率）。"""
    time.sleep(5.0)                       # 让 guest 往前走一会儿，倒退量才看得出来
    before = box.sample()
    t0 = time.monotonic()
    ok = box.sbx.checkpoint.restore(ck_id)
    wall = time.monotonic() - t0
    host_epoch = time.time()
    after = box.sample()
    time.sleep(max(0.0, settle - 1.0))    # 采样器自己占 1 s（sleep 1 那项）
    later = box.sample()

    res["restore_ok"] = bool(ok)
    res["restore_wall_s"] = wall
    res["before"] = before["raw"]
    res["after"] = after["raw"]
    res["later"] = later["raw"]
    res["host_epoch"] = host_epoch
    if store:
        try:
            res["restore_timings"] = json.load(
                open(os.path.join(store, box.id, "last-restore-timings.json")))
        except (OSError, ValueError):
            res["restore_timings"] = None

    # E1：uptime 倒退量（既定语义是**必须**倒退）
    ub, ua = before.get("uptime"), after.get("uptime")
    res["e1_uptime_before"] = ub
    res["e1_uptime_after"] = ua
    res["e1_uptime_delta_s"] = (ua - ub) if (ua is not None and ub is not None) else None
    res["e1_rolled_back"] = bool(ua is not None and ub is not None and ua < ub)

    # T25
    idle = cpu_idle_ratio(after.get("cpu0"), later.get("cpu0"))
    ca, cb = after.get("timer") or [], later.get("timer") or []
    dt = (later.get("mono") or 0) - (after.get("mono") or 0)
    rate = None
    if ca and cb and dt > 0:
        n = min(len(ca), len(cb))
        rate = max((cb[i] - ca[i]) / dt for i in range(n))
    res["t25"] = {
        "sleep1_after": after.get("sleep1"),
        "sleep1_later": later.get("sleep1"),
        "cpu0_idle": idle,
        "arch_timer_hz_max": rate,
        "guest_epoch_after": after.get("epoch"),
        "host_epoch": host_epoch,
        "date_skew_s": (after.get("epoch") - host_epoch) if after.get("epoch") else None,
        "pass_sleep1": after.get("sleep1") is not None and abs(after["sleep1"] - 1.0) <= 0.1,
        "pass_idle": idle is not None and idle >= 0.90,
        "pass_timer": rate is not None and rate < 200.0,
    }
    log("  restore %.3f s  uptime %s → %s（Δ %s）"
        % (wall, ub, ua, "%.2f" % res["e1_uptime_delta_s"] if res["e1_uptime_delta_s"] is not None else "?"))
    log("  T25: sleep1=%s  CPU0 空闲=%s  arch_timer=%s 次/s"
        % (res["t25"]["sleep1_after"],
           "-" if idle is None else "%.1f%%" % (idle * 100),
           "-" if rate is None else "%.0f" % rate))


def step_vgic(box, res, ck_id):
    """4. X1：restore 之后 vgic-state 里 ttyS0 SPI / virtio SPI / PPI 27/30 的 PLAEHCGN；
    再让 guest 往 /dev/ttyS0 写 4 KB（timeout 5）看会不会楔死；写完再读一次 vgic-state。"""
    fcpid = fc_pid_of(box.id)
    res["fc_pid"] = fcpid
    intids = {}
    try:
        tbl = parse_interrupt_table(box.run("cat /proc/interrupts"))
        intids = {k: v["intid"] for k, v in tbl.items()}
    except Exception as e:      # noqa: BLE001
        res["intid_err"] = str(e)
    want = {}
    for name in ("ttyS0", "virtio0", "virtio1", "virtio2"):
        if name in intids:
            want[name] = intids[name]
    want.setdefault("vtimer_ppi27", 27)
    want.setdefault("ptimer_ppi30", 30)
    res["watch_intids"] = want

    t0 = time.monotonic()
    ok = box.sbx.checkpoint.restore(ck_id)
    res["restore_ok"] = bool(ok)
    res["restore_wall_s"] = time.monotonic() - t0

    def snap(tag):
        text, note = read_vgic(fcpid)
        d = {"available": text is not None, "note": note}
        if text is None:
            log("  vgic-state 读不到：%s" % note)
            return d
        rows = parse_vgic_state(text)
        d["rows"] = {}
        for name, iid in want.items():
            # SPI 全局一条；SGI/PPI 每个 vCPU 一条，都要
            hits = [r for r in rows if r["intid"] == iid]
            d["rows"][name] = [{"vcpu": r["vcpu"], "typ": r["typ"], "intid": r["intid"],
                                "bits": r["bits"], "flags": r["flags"], "raw": r["raw"]}
                               for r in hits]
            for r in d["rows"][name]:
                log("  [%s] %-12s vcpu=%s %s %s PLAEHCGN=%s"
                    % (tag, name, r["vcpu"], r["typ"], r["intid"], r["bits"]))
        return d

    res["vgic_after_restore"] = snap("restore 后")

    # 串口写 4 KB：楔死的表现是这里超时（rc=124）
    t0 = time.monotonic()
    rc, out = box.sh("timeout 5 dd if=/dev/zero of=/dev/ttyS0 bs=1024 count=4 2>&1", timeout=120)
    res["serial_write"] = {"rc": rc, "elapsed_s": time.monotonic() - t0,
                           "out": out.strip()[-400:],
                           "wedged": rc == 124}
    log("  guest 写 /dev/ttyS0 4 KB: rc=%s 用时 %.2f s%s"
        % (rc, res["serial_write"]["elapsed_s"], "  ← 楔死" if rc == 124 else ""))

    res["vgic_after_write"] = snap("写串口后")
    try:
        tbl2 = parse_interrupt_table(box.run("cat /proc/interrupts"))
        res["ttyS0_counts_after"] = tbl2.get("ttyS0", {}).get("counts")
    except Exception:       # noqa: BLE001
        pass


def step_writeheavy(box, res, store, sizes):
    """5. 写密集档：guest 内写 N MB 随机页 → create → 记 mem_diff 实际大小与 timings；
    restore 后按 g2_diff_correctness.py 的口径做 md5 校验（HDBSS buffer 溢出 / 4 KB 差分粒度）。"""
    res["rows"] = []
    for mb in sizes:
        path = "/dev/shm/probe950_%dm.bin" % mb
        t0 = time.monotonic()
        rc, out = box.sh("dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null; "
                         "md5sum %s | cut -d' ' -f1" % (path, mb, path), timeout=900)
        md5 = [l.strip() for l in out.splitlines() if re.fullmatch(r"[0-9a-f]{32}", l.strip())]
        md5 = md5[0] if md5 else None
        write_s = time.monotonic() - t0

        t0 = time.monotonic()
        ck = box.sbx.checkpoint.create(name="probe950-w%d" % mb)
        create_s = time.monotonic() - t0
        row = {"dirty_mb": mb, "md5": md5, "write_s": write_s, "create_s": create_s,
               "id": ck.checkpoint_id, "mem_mode": getattr(ck, "mem_mode", None) or "?"}
        if store:
            d = os.path.join(store, box.id, ck.checkpoint_id)
            alloc, apparent, files = du_bytes(d)
            row["dir"] = d
            row["dir_alloc_bytes"] = alloc
            row["dir_apparent_bytes"] = apparent
            row["files"] = files
            # 产物里实占最大的那个就是内存差分（文件名各版本不同，按实占认，别写死名字）
            if files:
                big = max(files.items(), key=lambda kv: kv[1]["alloc"])
                row["mem_diff_file"] = big[0]
                row["mem_diff_alloc_bytes"] = big[1]["alloc"]
                row["mem_diff_apparent_bytes"] = big[1]["apparent"]
            try:
                row["timings"] = json.load(open(os.path.join(d, "timings.json")))
            except (OSError, ValueError):
                row["timings"] = None

        # 破坏现场 → restore → md5 必须回到 checkpoint 那一刻
        box.sh("dd if=/dev/urandom of=%s bs=1M count=8 conv=notrunc 2>/dev/null" % path, timeout=300)
        t0 = time.monotonic()
        ok = box.sbx.checkpoint.restore(ck.checkpoint_id)
        row["restore_ok"] = bool(ok)
        row["restore_s"] = time.monotonic() - t0
        rc, out = box.sh("md5sum %s | cut -d' ' -f1" % path, timeout=600)
        got = [l.strip() for l in out.splitlines() if re.fullmatch(r"[0-9a-f]{32}", l.strip())]
        row["md5_after"] = got[0] if got else None
        row["md5_ok"] = bool(md5 and row["md5_after"] == md5)
        res["rows"].append(row)
        log("  %4d MB: mem_mode=%s 产物实占 %s B（差分 %s 实占 %s / 表观 %s）"
            " create %.2f s restore %.2f s md5 %s"
            % (mb, row["mem_mode"], row.get("dir_alloc_bytes", "?"), row.get("mem_diff_file", "?"),
               row.get("mem_diff_alloc_bytes", "?"), row.get("mem_diff_apparent_bytes", "?"),
               create_s, row["restore_s"], "OK" if row["md5_ok"] else "不一致!"))
        box.sh("rm -f %s" % path, timeout=120)


# ─────────────────────────────────────────────────────── 主流程

def main():
    ap = argparse.ArgumentParser(description="950 vs 920B 动态探测（建 1 个沙箱，跑完必删）")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                    help="dotenv 路径。默认是 920B 那份；950 上路径不同，必须显式传")
    ap.add_argument("--template", default="base")
    ap.add_argument("--only", default="", help="只跑这些步骤（逗号分隔）：%s" % ",".join(STEPS))
    ap.add_argument("--sizes", default="16,128,512", help="写密集档的 MB 数（步骤 5）")
    ap.add_argument("--settle", type=float, default=10.0, help="restore 后第二次采样的间隔秒数")
    ap.add_argument("--out", default=None, help="JSON 输出路径（默认 results/probe-dynamic-<host>-<date>.json）")
    ap.add_argument("--keep", action="store_true", help="跑完不 kill 沙箱（留现场；默认必 kill）")
    ap.add_argument("--private", action="store_true",
                    help="建 allow_public_traffic=False 的私有沙箱（顺带回归 K1）")
    ap.add_argument("--timeout", type=int, default=1800, help="沙箱自身超时秒数")
    args = ap.parse_args()

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    for s in only:
        if s not in STEPS:
            sys.exit("--only 里的 %r 不是合法步骤，合法值：%s" % (s, ",".join(STEPS)))
    todo = only or list(STEPS)
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]

    load_env(args.env_file, required_ok=False)
    host = socket.gethostname()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(HERE, "results", "probe-dynamic-%s-%s.json" % (host, stamp))
    os.makedirs(os.path.dirname(out), exist_ok=True)

    store = checkpoint_store()
    fc_before = fc_count()
    _, oenv = orchestrator_env()
    RESULT["meta"] = {
        "host": host, "started": stamp, "steps": todo, "sizes": sizes,
        "env_file": args.env_file, "store": store,
        "fc_before": fc_before,
        "python": sys.version.split()[0],
        "orchestrator_env": {k: oenv.get(k) for k in
                             ("FC_TRACK_DIRTY_PAGES", "ORCHESTRATOR_BASE_PATH",
                              "FIRECRACKER_VERSIONS_DIR", "ENVIRONMENT")},
    }
    log("probe-dynamic  host=%s  步骤=%s" % (host, ",".join(todo)))
    log("产物目录: %s" % store)
    log("开跑前 firecracker 进程数: %s（**别人的**，收尾时必须回到这个数）" % fc_before)

    try:
        from e2b import Sandbox
    except ImportError as e:
        sys.exit("import e2b 失败：%s\n950 上请先 conda activate jll-e2b，或用该环境的 python 跑。" % e)

    kw = {"template": args.template, "timeout": args.timeout}
    if args.private:
        kw["network"] = {"allow_public_traffic": False}
    t0 = time.monotonic()
    try:
        sbx = Sandbox.create(**kw)
    except TypeError:
        kw.pop("network", None)
        sbx = Sandbox.create(**kw)
    box = Box(sbx)
    RESULT["meta"]["sandbox_id"] = box.id
    RESULT["meta"]["sandbox_create_s"] = time.monotonic() - t0
    log("沙箱 %s（%.1f s）" % (box.id, RESULT["meta"]["sandbox_create_s"]))

    ck_id = None
    rc = 0
    try:
        for name in todo:
            res = RESULT["steps"].setdefault(name, {})
            log("\n=== 步骤 %s ===" % name)
            t0 = time.monotonic()
            try:
                if name == "guest":
                    step_guest(box, res)
                elif name == "checkpoint":
                    ck_id = step_checkpoint(box, res, store)
                elif name == "restore":
                    if ck_id is None:
                        # 单独 `--only restore` 时也得先有个能回的 checkpoint
                        ck_id = step_checkpoint(box, RESULT["steps"].setdefault("checkpoint", {}), store)
                    step_restore(box, res, ck_id, store, args.settle)
                elif name == "vgic":
                    if ck_id is None:
                        ck_id = step_checkpoint(box, RESULT["steps"].setdefault("checkpoint", {}), store)
                    step_vgic(box, res, ck_id)
                elif name == "writeheavy":
                    step_writeheavy(box, res, store, sizes)
                res["ok"] = True
            except Exception as e:      # noqa: BLE001 —— 一步失败不拖累后面的步骤
                rc = 1
                res["ok"] = False
                res["error"] = "%s: %s" % (type(e).__name__, e)
                res["traceback"] = traceback.format_exc()
                RESULT["errors"].append("%s: %s" % (name, res["error"]))
                log("  !! 步骤 %s 失败：%s" % (name, res["error"]))
            res["elapsed_s"] = time.monotonic() - t0
    finally:
        # 步骤 6：收尾。沙箱**必须**删掉——920B 上别人也在跑验收。
        if args.keep:
            log("\n--keep：沙箱 %s 留着（记得手工 kill）" % box.id)
        else:
            try:
                sbx.kill()
                log("\n沙箱 %s 已 kill" % box.id)
            except Exception as e:      # noqa: BLE001
                log("\n!! kill 沙箱失败：%s —— 手工处理" % e)
                RESULT["errors"].append("kill: %s" % e)
        # 判据用「**我们这个沙箱**的 FC 还在不在」，不用总数：920B 上别人也在建沙箱，
        # 总数随时会变（09-17 首跑就因此误报过一次）。总数照记，只是参考。
        own_gone = False
        for _ in range(15):
            if fc_pid_of(box.id) is None:
                own_gone = True
                break
            time.sleep(2)
        fc_after = fc_count()
        RESULT["meta"]["fc_after"] = fc_after
        RESULT["meta"]["own_fc_gone"] = own_gone
        RESULT["meta"]["fc_balanced"] = (fc_before == fc_after)
        log("自己的 firecracker 已退出：%s%s"
            % (own_gone, "" if own_gone else "  ← 没退，手工 kill！"))
        log("firecracker 总数：开跑前 %s → 收尾后 %s%s"
            % (fc_before, fc_after,
               "" if fc_before == fc_after else "（差额可能是别人的沙箱，以上一行为准）"))
        RESULT["meta"]["elapsed_s"] = time.time() - time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))
        with open(out, "w") as f:
            json.dump(RESULT, f, indent=1, ensure_ascii=False)
        txt = out[:-5] + ".txt" if out.endswith(".json") else out + ".txt"
        with open(txt, "w") as f:
            f.write(summary(RESULT))
        log("\n写出:\n  %s\n  %s" % (out, txt))
        print()
        print(summary(RESULT))
    return rc


def summary(r):
    """人读摘要：只放跨机对比时真正要看的那几行。"""
    L = []
    m = r["meta"]
    L.append("=== probe-dynamic 摘要  host=%s  %s ===" % (m.get("host"), m.get("started")))
    L.append("沙箱 %s   产物目录 %s" % (m.get("sandbox_id"), m.get("store")))
    L.append("orchestrator env: %s" % json.dumps(m.get("orchestrator_env", {}), ensure_ascii=False))
    L.append("自己的 firecracker 已退出：%s；总数 %s → %s（差额可能是别人的沙箱）"
             % (m.get("own_fc_gone"), m.get("fc_before"), m.get("fc_after")))

    g = r["steps"].get("guest") or {}
    if g.get("ok"):
        L.append("")
        L.append("[guest] kernel=%s vCPU=%s pagesize=%s" % (g.get("kernel"), g.get("nproc"), g.get("pagesize")))
        L.append("[guest] INTID: %s" % json.dumps(g.get("intids", {}), ensure_ascii=False))

    c = r["steps"].get("checkpoint") or {}
    if c.get("creates"):
        L.append("")
        L.append("[checkpoint] dirty_tracking=%s  vmm_version=%s"
                 % (c.get("dirty_tracking"), c.get("vmm_version")))
        for rec in c["creates"]:
            L.append("[checkpoint] #%d mem_mode=%-12s wall=%.3f s 产物实占=%s B（表观 %s B）"
                     % (rec["n"], rec["mem_mode"], rec["wall_s"],
                        rec.get("dir_alloc_bytes", "?"), rec.get("dir_apparent_bytes", "?")))

    s = r["steps"].get("restore") or {}
    if s.get("ok"):
        t = s.get("t25", {})
        L.append("")
        L.append("[E1] uptime %.3f → %.3f（Δ %+.3f s，倒退=%s）"
                 % (s.get("e1_uptime_before") or 0, s.get("e1_uptime_after") or 0,
                    s.get("e1_uptime_delta_s") or 0, s.get("e1_rolled_back")))
        L.append("[T25] sleep1=%s(%s) CPU0 空闲=%s(%s) arch_timer=%s 次/s(%s) date 偏差=%s s"
                 % (t.get("sleep1_after"), t.get("pass_sleep1"),
                    None if t.get("cpu0_idle") is None else "%.1f%%" % (t["cpu0_idle"] * 100),
                    t.get("pass_idle"),
                    None if t.get("arch_timer_hz_max") is None else "%.0f" % t["arch_timer_hz_max"],
                    t.get("pass_timer"),
                    None if t.get("date_skew_s") is None else "%.2f" % t["date_skew_s"]))
        if s.get("restore_timings"):
            L.append("[restore] 服务端分段: %s" % json.dumps(s["restore_timings"], ensure_ascii=False)[:400])

    v = r["steps"].get("vgic") or {}
    if v.get("ok"):
        L.append("")
        snap = v.get("vgic_after_restore", {})
        L.append("[X1] vgic-state: %s" % (snap.get("note") if not snap.get("available") else snap.get("note")))
        for tag in ("vgic_after_restore", "vgic_after_write"):
            snap = v.get(tag) or {}
            for name, rows in (snap.get("rows") or {}).items():
                for row in rows:
                    L.append("[X1] %-18s %-12s vcpu=%s %s %s PLAEHCGN=%s"
                             % (tag, name, row["vcpu"], row["typ"], row["intid"], row["bits"]))
        sw = v.get("serial_write") or {}
        L.append("[T26] guest 写 /dev/ttyS0 4 KB: rc=%s 用时 %.2f s 楔死=%s"
                 % (sw.get("rc"), sw.get("elapsed_s") or 0, sw.get("wedged")))

    w = r["steps"].get("writeheavy") or {}
    if w.get("rows"):
        L.append("")
        for row in w["rows"]:
            L.append("[写密集] %4d MB mem_mode=%-12s 产物实占=%s B 差分=%s 实占=%s 表观=%s "
                     "create=%.2f s restore=%.2f s md5=%s"
                     % (row["dirty_mb"], row["mem_mode"], row.get("dir_alloc_bytes", "?"),
                        row.get("mem_diff_file", "?"), row.get("mem_diff_alloc_bytes", "?"),
                        row.get("mem_diff_apparent_bytes", "?"),
                        row["create_s"], row["restore_s"], "OK" if row["md5_ok"] else "不一致!"))

    if r["errors"]:
        L.append("")
        L.append("错误：")
        for e in r["errors"]:
            L.append("  " + e)
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
