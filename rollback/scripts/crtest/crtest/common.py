#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crtest 公共库：沙箱包装、宿主机指标、断言、JSON 输出。

来源说明：`fc_get` / `orchestrator_pid_env` / `checkpoint_store` / `HostMeter` /
终端排版与分位数几个函数，是从
`e2b-infra/rollback/scripts/acceptance/checkpoint_concurrent.py`（09-15 并发套件）
**复制**过来的，不是 import —— 那边的脚本是一个个单独拷到目标机上跑的，跨文件依赖
会在版本对不齐时静默降级成"未知"而不是报错（那边的注释里已经记过一次教训），
而且本套件按用户要求不许碰 e2b-infra 仓库。`Box` 的现场验证口径
（`scene()` 的五个字段、心跳 pid 不变 = 内存真回来了）同样照抄那边，
好让两套的 JSON 能放在一起比。

本文件**不在模块层 import e2b / dotenv**：`--help` 和 `tests/test_common.py`
要能在没装 SDK 的机器（WSL）上跑。所有 SDK 调用都在函数体里 import。
"""

import atexit
import base64
import glob
import json
import os
import re
import socket
import statistics
import subprocess
import tempfile
import threading
import time
import traceback
import unicodedata

# 客户端侧真超时的判据（抄自 checkpoint_concurrent.py）：httpx / httpcore 的
# *Timeout 异常名，或者消息里明说超时。
TIMEOUT_RE = re.compile(r"(ReadTimeout|WriteTimeout|ConnectTimeout|PoolTimeout|"
                        r"TimeoutException|timed out|deadline exceeded)", re.I)

# 服务端答 not_found 的判据：Connect 错误码、HTTP 404、SDK 异常名都可能出现。
NOTFOUND_RE = re.compile(r"(not_found|notfound|404|NotFoundException|no such checkpoint|"
                         r"unknown checkpoint)", re.I)

DEFAULT_TEMPLATE_ID = "base"
# dotenv 文件默认值：环境变量 CRTEST_ENV_FILE 优先，否则按 RPM 部署的落点
# （$E2B_DEPLOY_DIR/.env，默认 /opt/e2b-infra/.env）。源码树部署（920B）用
# --env-file 或 CRTEST_ENV_FILE 指到 e2b-infra/benchmark/.env。
# rollback/scripts/950/run.sh 这两条都会替你设好。
DEFAULT_DEPLOY_DIR = os.environ.get("E2B_DEPLOY_DIR", "/opt/e2b-infra")
DEFAULT_ENV_FILE = (os.environ.get("CRTEST_ENV_FILE")
                    or os.path.join(DEFAULT_DEPLOY_DIR, ".env"))

BENCH_DIR = "/bench-root"          # 文件那份写这里（根文件系统上，进 NBD 写层）
HB_PID = "/dev/shm/hb.pid"
HB_LOG = "/dev/shm/hb.log"


# ---------------------------------------------------------------- 终端排版

def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def lpad(s, n):
    return s + " " * max(0, n - width(s))


def rpad(s, n):
    return " " * max(0, n - width(s)) + s


def fmt_ms(x):
    """毫秒，自适应位数。旧的 "%.0f" 把亚毫秒全显示成 0（T34 的 p50 0.025 ms 就这么
    被抹成 0，还被当成「量不出来」写进结论），所以小数量级要保留到 0.001 ms。"""
    if x is None:
        return "-"
    a = abs(x)
    if a >= 100:
        return "%.0f" % x
    if a >= 10:
        return "%.1f" % x
    if a >= 1:
        return "%.2f" % x
    return "%.3f" % x


def fmt_s(x):
    return "-" if x is None else "%.3f" % x


def as_int(x):
    """字符串 → int，取不到就 None（guest 采样偶尔是空串）。"""
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def p50(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def pmax(xs):
    xs = [x for x in xs if x is not None]
    return max(xs) if xs else None


def quantile(xs, q):
    """p99 之类。样本少时退化成 max，和并发报告 §9.3 的算法一致（排序后取
    round(q*(n-1)) 那一项），这样两套数据可以直接对比。"""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[int(round(q * (len(xs) - 1)))]


def now_tag():
    return time.strftime("%Y%m%d-%H%M%S")


LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        print(msg, flush=True)


def table(headers, rows):
    """打一张小表：headers 是列名，rows 是等长的字符串列表。"""
    cols = [max([width(headers[i])] + [width(r[i]) for r in rows]) for i in range(len(headers))]
    log("  " + " ".join(lpad(headers[i], cols[i]) for i in range(len(headers))))
    log("  " + "-" * (sum(cols) + len(cols) - 1))
    for r in rows:
        log("  " + " ".join(lpad(r[i], cols[i]) for i in range(len(headers))))


# ---------------------------------------------------------------- 纯函数（可单测）

def parse_kv(out):
    """把 `k=v` 的行收成字典。guest 里所有采集脚本都按这个格式打。"""
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def parse_proc_stat_line(line):
    """`/proc/stat` 的一行 CPU：`cpu0 user nice system idle iowait irq softirq steal ...`。
    返回字典（单位是 jiffy）。字段不够时缺的补 0。"""
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
    """两次 `/proc/stat` 采样之间的空闲占比。idle + iowait 都算空闲。
    总量没涨（两次采样之间 CPU 一个 jiffy 都没走）时返回 None —— 那不是
    "不空闲"，是"没数据"，不能拿去判定。"""
    if not a or not b:
        return None
    keys = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
    total = sum(b.get(k, 0) - a.get(k, 0) for k in keys)
    if total <= 0:
        return None
    idle = (b.get("idle", 0) - a.get("idle", 0)) + (b.get("iowait", 0) - a.get("iowait", 0))
    return idle / float(total)


def parse_interrupt_counts(text):
    """`/proc/interrupts` 里若干行（可能不止一条 arch_timer：虚拟/物理各一条）
    按 CPU 求和。每行形如 ` 11:  1234  5678  GICv3 ... arch_timer`，
    冒号后面的前几个纯数字就是每个 CPU 的计数。返回 [cpu0, cpu1, ...]。"""
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


def heartbeat_gaps(samples, gt_ms=100.0):
    """心跳序列 → 每一段连续运行期内的最大间隔（**按 monotonic 量**）。

    samples 是 `(realtime, monotonic)` 列表（guest 里 10 ms 一条）。
    一次 restore 会把 guest 整个搬回快照那一刻：心跳文件本身也回滚，
    monotonic 倒退，realtime 随后被 envd 拨回当前。所以按 monotonic 倒退切段。

    两个坑（09-17 修）：

    · **段内不能用 realtime 量间隔。** envd 把 realtime 拨回当前发生在 monotonic
      倒退**之后** —— 那一跳落在新段的**段内**，于是段内 realtime 最大差 = 距上次
      checkpoint 的时长，13 段就线性递增 5.2 s（`--every 5`），量的根本不是停顿。
      段内间隔一律用 **monotonic** 相邻差：它不受 envd 拨钟影响。
    · **每段第一跳要丢掉。** 段首那条是回滚回来的旧记录（快照那一刻写的），
      它到下一条之间跨的是"回滚"本身，不是沙箱跑着的时候被卡。

    返回 `{"segments", "samples", "intervals", "gaps", "max_gap_s",
    "p99_ms", "gt_ms", "gt_count"}`：`gaps` 是每段最大（秒），`max_gap_s` 是总最大
    （秒），`p99_ms` 是**全部段内间隔**的 p99（毫秒），`gt_count` 是超过 `gt_ms`
    的间隔条数 —— 看分布而不只是 max。没有有效段时 max_gap_s / p99_ms 为 None。
    """
    segs = []
    cur = []
    prev_mono = None
    for rt, mono in samples:
        if prev_mono is not None and mono < prev_mono:
            segs.append(cur)
            cur = []
        cur.append((rt, mono))
        prev_mono = mono
    segs.append(cur)
    gaps = []
    intervals = []
    used = 0
    for seg in segs:
        # 丢掉段内第一跳 → 至少要 3 条样本才剩得下一个间隔。
        if len(seg) < 3:
            continue
        d = [seg[i + 1][1] - seg[i][1] for i in range(len(seg) - 1)][1:]
        if not d:
            continue
        used += 1
        intervals.extend(d)
        gaps.append(max(d))
    q99 = quantile(intervals, 0.99)
    return {"segments": used,
            "samples": len(samples),
            "intervals": len(intervals),
            "gaps": gaps,
            "max_gap_s": max(gaps) if gaps else None,
            "p99_ms": None if q99 is None else q99 * 1000.0,
            "gt_ms": gt_ms,
            "gt_count": sum(1 for x in intervals if x * 1000.0 > gt_ms)}


def parse_heartbeat(text):
    """心跳文件文本 → [(realtime, monotonic)]，坏行丢掉。"""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) != 2:
            continue
        try:
            out.append((float(f[0]), float(f[1])))
        except ValueError:
            continue
    return out


# ---- 判定函数（三段式失败信息的"实际"部分由它们算出来）

T25_LIMITS = {
    "sleep_tol_s": 0.1,        # `sleep 1` 实测 1.0 ± 0.1 s
    "idle_min": 0.90,          # 10 s 窗口内 CPU0 空闲 ≥ 90%
    "timer_max_hz": 200.0,     # arch_timer 速率 < 200/s
    "date_skew_s": 5.0,        # restore 后 guest 的 CLOCK_REALTIME 与宿主机差 < 5 s
}


def judge_t25(before, after, later, host_epoch, limits=None):
    """T25 的全部判定。三个采样点：restore 前（before）、restore 刚回来（after）、
    10 s 后（later）。host_epoch 是客户端在 after 采样时刻的宿主机 epoch。

    返回 [(名字, 通过?, 期望, 实际)]，调用方再包成三段式。判据来自方案 T25
    （`checkpoint-restore-fix-plan-2026-09-17.md` §4.2）与 09-17 E1 的既定语义：
    uptime 倒退是**预期行为**，CLOCK_REALTIME 由 envd 对齐所以不许倒退。
    """
    L = dict(T25_LIMITS)
    L.update(limits or {})
    out = []

    ub, ua = before.get("uptime"), after.get("uptime")
    out.append(("uptime 倒退（既定语义）", ub is not None and ua is not None and ua < ub,
                "restore 后 uptime < restore 前", "%s → %s" % (ub, ua)))

    db, da = before.get("epoch"), after.get("epoch")
    ok_date = (da is not None and db is not None and da >= db - 1.0
               and host_epoch is not None and abs(da - host_epoch) < L["date_skew_s"])
    out.append(("date 被 envd 对齐", ok_date,
                "不倒退且与宿主机差 < %.0f s" % L["date_skew_s"],
                "guest %s / 宿主机 %s / restore 前 %s" % (da, host_epoch, db)))

    for tag, s in (("restore 后", after), ("10 s 后", later)):
        v = s.get("sleep1")
        out.append(("%s sleep 1 实测" % tag,
                    v is not None and abs(v - 1.0) <= L["sleep_tol_s"],
                    "1.0 ± %.1f s" % L["sleep_tol_s"], fmt_s(v)))

    idle = cpu_idle_ratio(after.get("cpu0"), later.get("cpu0"))
    out.append(("10 s 窗口 CPU0 空闲", idle is not None and idle >= L["idle_min"],
                "≥ %.0f%%" % (L["idle_min"] * 100),
                "-" if idle is None else "%.1f%%" % (idle * 100)))

    rate = timer_rate(after, later)
    out.append(("arch_timer 速率（各 CPU 最大）", rate is not None and rate < L["timer_max_hz"],
                "< %.0f 次/s" % L["timer_max_hz"],
                "-" if rate is None else "%.0f 次/s" % rate))
    return out


def timer_rate(a, b):
    """两个采样点之间 arch_timer 的最大每 CPU 速率（次/s）。"""
    ca, cb = a.get("timer") or [], b.get("timer") or []
    dt = (b.get("mono") or 0) - (a.get("mono") or 0)
    if not ca or not cb or dt <= 0:
        return None
    n = min(len(ca), len(cb))
    return max((cb[i] - ca[i]) / dt for i in range(n))


def judge_t32(rows, bitmap_ratio=2.0):
    """T32 判定。rows: [{"dirty_mb": d, "fc_bitmap": [..], "fc_memory": [..]}]，
    每档一条，列表是那一档的若干次 restore 的值。

    - fc_bitmap 是"扫一遍位图"的开销，与脏页量无关 → 三档的 p50 相差 < 2 倍；
    - fc_memory 是"搬脏页"的开销 → 随脏集单调不降，且最大档明显大于 0 档。
    依据：方案 §4.3 T32 / FC 改动 F3（C1 分段）。
    """
    out = []
    rows = sorted(rows, key=lambda r: r["dirty_mb"])
    bm = [p50(r.get("fc_bitmap") or []) for r in rows]
    mem = [p50(r.get("fc_memory") or []) for r in rows]
    have_bm = [x for x in bm if x]
    if len(have_bm) == len(rows) and have_bm:
        ratio = max(have_bm) / min(have_bm)
        out.append(("fc_bitmap 与脏集无关", ratio < bitmap_ratio,
                    "三档 p50 极差 < %.1f 倍" % bitmap_ratio,
                    "%.2f 倍（%s ms）" % (ratio, ", ".join("%.2f" % x for x in bm))))
    else:
        out.append(("fc_bitmap 与脏集无关", False, "三档都要有 fc_bitmap 值",
                    "缺值：%s（服务端没带 timings_us.bitmap？见 O7/F3）" % bm))

    have_mem = [x for x in mem if x is not None]
    if len(have_mem) == len(rows) and have_mem:
        mono = all(mem[i] <= mem[i + 1] * 1.05 for i in range(len(mem) - 1))
        grew = mem[-1] > mem[0]
        out.append(("fc_memory 随脏集单调不降", mono and grew,
                    "p50 随 dirty_mb 递增（容差 5%）",
                    ", ".join("%d MB: %.2f ms" % (rows[i]["dirty_mb"], mem[i])
                              for i in range(len(rows)))))
    else:
        out.append(("fc_memory 随脏集单调不降", False, "三档都要有 fc_memory 值",
                    "缺值：%s" % mem))
    return out


def judge_page_scan(bad_count, bad_pages, inprogress):
    """T14 页级自校验判定（09-17 修）。返回 (ok, tolerated, detail)。

    坏页 = 页头的 页号/版本/crc32 与 body 对不上。它有两种来源，必须分开：

      · **回滚把两个时刻的页混了** —— 真缺陷。坏页落在哪一页没人保证，通常不止一页；
      · **快照就拍在写者写这一页的中途**（SIGSTOP / VM pause 落在那一次 memcpy 里）——
        restore 忠实还原了那一刻，这一页当然是半新半旧。**不是缺陷**。第 2 轮验收
        `--sandboxes 4` 报的页 42451 就是它：整块 256 MB 的 md5 与 create 时（写者已
        SIGSTOP）记录的期望完全一致，说明那一页在快照里本来就是写到一半的。

    写者每写一页前把页号写进控制页（写完清 -1），于是"拍摄时刻的进行中页"有名有姓：
    **坏页集合 ⊆ {进行中页号} 才容忍**（也就是至多 1 页、且正是那一页）；坏页 ≥ 2、
    或坏页不是进行中那一页、或压根没有进行中页（-1），一律判失败 —— 那才是回滚混了
    两个时刻。`--no-stop` 同一条规则：那边扫描前也会先 SIGSTOP 住写者再读控制页。
    """
    n = as_int(bad_count)
    ip = as_int(inprogress)
    pages = sorted({int(p) for p in (bad_pages or []) if str(p).strip() != ""})
    if n is None:
        return False, False, "校验器没报坏页数"
    if n == 0:
        return True, False, "坏页 0"
    listed = "、".join(str(p) for p in pages) or "未列出"
    if n > 1:
        return False, False, ("坏页 %d ≥ 2（%s），进行中页只有 %s —— 回滚混了两个时刻"
                              % (n, listed, ip))
    if ip is None or ip < 0:
        return False, False, ("坏页 1（%s），但快照时刻没有进行中页（inprogress=%s）"
                              % (listed, ip))
    if pages != [ip]:
        return False, False, ("坏页 1（%s）≠ 进行中页 %d" % (listed, ip))
    return True, True, "坏页 1，正是快照时刻的进行中页 %d（已容忍）" % ip


def judge_line_scan(bad_count, tail_partial):
    """T14 行级自校验判定。返回 (ok, tolerated, detail)。

    行是 `write()` 一次写完的（短写会补齐），一行 20 来字节远小于一页，所以
    被 SIGSTOP 打断留下的半条只可能在**文件末尾**；校验器把它单独报成
    tail_partial，同页级一样容忍 1 条。中间出现坏行 / seq 断裂仍然判失败。
    """
    n = as_int(bad_count)
    t = as_int(tail_partial) or 0
    if n is None:
        return False, False, "校验器没报坏行数"
    if n:
        return False, False, "坏行 %d" % n
    if t:
        return True, True, "坏行 0；文件末尾半条（快照拍在那次 write() 中途，已容忍）"
    return True, False, "坏行 0"


# ---------------------------------------------- 错误分型（T11 / T21 用）

# 服务端的错误体是 `{"code","reason","message"}`（infra-arm jll 43b1c2456 起）。
# SDK 侧的异常层次（`CheckpointTornException` 等）正在同步实现，**用例一律不依赖
# 它**：今天的 `e2b_connect.client.make_error` 只取 code 与 message，reason 直接
# 丢掉，而 `envd/rpc.py` 没映射的 code（data_loss / internal / failed_precondition /
# aborted）最后长成 `SandboxException("Code.data_loss: …")`。所以判定按
# 「能拿到什么用什么」分三级：
#
#   1. 异常对象上的字段 —— `reason` / `status` / `code`（SDK 哪天加了就自动用上）；
#   2. 文本里的 `Code.<x>` 与 `"reason":"<x>"`（服务端原样透出来时）；
#   3. 退化 —— 按 code + message 子串猜（REASON_HINTS）。
#
# 「SDK 有没有 reason 字段」本身是记录项：`error_info()["reason_src"]` 会说是
# 哪一级给出的，用例把它记进 JSON 的 summary。

# 注入的故障在错误文案里自报家门（faults.go 的 `injectedError`），torn_assemble
# 那条是拿一个进不去的路径 `<store>/<sbx>/fault-injected/torn_assemble` 触发的，
# 所以两种写法都认。
FAULT_RE = re.compile(r"fault[ -]injected", re.I)

# `Code.<x>` 是 SDK 没映射时拼出来的样子；`"code":"<x>"` 是错误体原样露出来的样子
# （比如 R4 的 409 `{"code":"aborted","reason":"sandbox_restored"}` 经代理返回时）。
_CODE_IN_TEXT = re.compile(r'\bCode\.([a-z_]+)|"code"\s*:\s*"([a-z_]+)"')
_REASON_IN_TEXT = re.compile(r'"reason"\s*:\s*"([a-z_]+)"|\breason[=:]\s*"?([a-z_]+)')

# SDK 把一部分 code 映射成了自己的异常类，code 在这一步就丢了，只能按类名反推。
# checkpoint 那一族（`e2b/exceptions.py` + `e2b/sandbox/checkpoint/errors.py`，SDK
# 第 3 轮加的）尤其如此：异常对象上有 `.reason` / `.checkpoint_id` / `.sandbox_id`，
# 但**没有** code / status，code 一律得按类名查这张表。
_CODE_BY_CLASS = {
    "NotFoundException": "not_found",
    "SandboxNotFoundException": "not_found",
    "FileNotFoundException": "not_found",
    "InvalidArgumentException": "invalid_argument",
    "AuthenticationException": "unauthenticated",
    "RateLimitException": "resource_exhausted",
    # checkpoint 一族：类 → 服务端回的 Connect code（errors.py 的两张映射表反过来）
    "CheckpointException": "internal",
    "CheckpointTornException": "data_loss",
    "CheckpointChainBrokenException": "failed_precondition",
    "CheckpointRootfsPoisonedException": "internal",
    "CheckpointGuestUnresponsiveException": "internal",
    "CheckpointBusyException": "unavailable",
    "CheckpointInterruptedException": "aborted",
    # 两道配额闸（infra-arm jll 55a542846 / SDK 16b7cdeb）：两条 reason 同属一个 code
    "CheckpointDiskFullException": "resource_exhausted",
    "CheckpointTooManyException": "resource_exhausted",
}

# 类名自带的 reason（SDK 里的 `_default_reason`）。拿到**异常对象**时用不上它
# （直接读 `.reason` 就是 field 级）；只有拿到的是一条字符串（老 JSON 回放、日志行）
# 才按类名退化猜 —— 那仍然算 guess，不算 field。
_REASON_BY_CLASS = {
    "CheckpointTornException": "torn",
    "CheckpointChainBrokenException": "chain_broken",
    "CheckpointRootfsPoisonedException": "rootfs_poisoned",
    "CheckpointGuestUnresponsiveException": "guest_unresponsive",
    "CheckpointBusyException": "busy",
    "CheckpointInterruptedException": "sandbox_restored",
    "CheckpointDiskFullException": "disk_full",
    "CheckpointTooManyException": "too_many_checkpoints",
}

# 退化猜 reason：服务端每条 message 的原文（service.go），按先后顺序取第一条命中的。
REASON_HINTS = [
    ("torn", re.compile(r"torn between two moments|past the commit point", re.I)),
    ("guest_unresponsive", re.compile(r"envd did not come back|guest never answered", re.I)),
    ("rootfs_poisoned", re.compile(r"bookkeeping is poisoned|record rootfs layer", re.I)),
    ("chain_broken", re.compile(r"has no disk view", re.I)),
    ("busy", re.compile(r"another operation|retry-after", re.I)),
]

# code 本身就够判的两条（code 与 reason 一一对应，猜不猜都一样）。
_REASON_BY_CODE = {"data_loss": "torn", "failed_precondition": "chain_broken"}


def parse_error_text(text):
    """从错误文案里抠 (code, reason)，抠不到给 None。纯函数。"""
    text = text or ""
    m = _CODE_IN_TEXT.search(text)
    code = (m.group(1) or m.group(2)) if m else None
    m = _REASON_IN_TEXT.search(text)
    reason = (m.group(1) or m.group(2)) if m else None
    return code, reason


def guess_reason(code, message):
    """拿不到 reason 字段时的退化判断：先看 code，再看 message 子串。纯函数。"""
    if code in _REASON_BY_CODE:
        return _REASON_BY_CODE[code]
    for reason, rx in REASON_HINTS:
        if rx.search(message or ""):
            return reason
    return None


def _attr(obj, *names):
    """读异常对象上的第一个有值的属性，枚举取 `.value`，方法/None/空串不算。"""
    for name in names:
        v = getattr(obj, name, None)
        if v is None or callable(v):
            continue
        v = getattr(v, "value", v)
        if v == "":
            continue
        return v
    return None


def error_info(exc):
    """把一个异常（或一条 `类型: 文案` 的错误串）拆成可判定的几项。

    返回 `{"type","status","code","reason","reason_src","message","retry_after",
    "fault_injected"}`：

      · `code` / `status` 是同一个 Connect code（SDK 的属性叫 `status`，服务端
        错误体里叫 `code`，这里两个键给的是同一个值，谁顺手用谁）；
      · `reason_src` = `"field"`（异常对象上真有 `reason` 属性）/ `"text"`（文案里带）/
        `"guess"`（按 code / 类名 / message 退化猜）/ `None`（没判出来）—— 这一项就是
        需求书要的「SDK 有无 reason 字段」记录项；
      · `retry_after` = `CheckpointBusyException` 的 `Retry-After` 秒数（别的错误是 None）；
      · `fault_injected` = 这条错误是不是 `CHECKPOINT_FAULT_INJECT` 自己产生的。

    **一定要把异常对象本身传进来**（`record_error()` / `ctx.op()` 已经保证了这一点）：
    先 `"%s: %s"` 成串再进来，`.reason` / `.status` 就已经丢了，reason 的出处只会是
    guess —— 第 3 轮验收的三条 T21 误判就是这么来的。老 SDK（没有 reason 属性）传对象
    进来也不会出事，照样退化到文案 / 猜。
    """
    if isinstance(exc, str):
        text, cls = exc, (exc.split(":", 1)[0].strip() if ":" in exc else "")
        objs = ()
    else:
        text, cls = "%s: %s" % (type(exc).__name__, exc), type(exc).__name__
        objs = tuple(o for o in (exc, getattr(exc, "__cause__", None)) if o is not None)

    code = reason = retry_after = None
    src = None
    for obj in objs:
        if reason is None:
            v = _attr(obj, "reason", "_reason")
            if v is not None:
                reason, src = str(v), "field"
        if code is None:
            v = _attr(obj, "status", "code", "_code")
            if v is not None:
                code = str(v)
        if retry_after is None:
            retry_after = _attr(obj, "retry_after")
        if reason is not None and code is not None:
            break

    tcode, treason = parse_error_text(text)
    code = code or tcode or _CODE_BY_CLASS.get(cls)
    if not reason and treason:
        reason, src = treason, "text"
    if not reason:
        g = guess_reason(code, text) or _REASON_BY_CLASS.get(cls)
        if g:
            reason, src = g, "guess"

    return {"type": cls, "status": code, "code": code, "reason": reason,
            "reason_src": src, "message": text, "retry_after": retry_after,
            "fault_injected": bool(FAULT_RE.search(text))}


def record_error(rec, exc):
    """把一个异常同时记进操作记录：`err` 是展示用的串，`error_info` 是结构化分型。

    判定一律读 `error_info`（`rec_error_info()` 会取它），`err` 只进日志与报告文案。
    """
    rec["err"] = "%s: %s" % (type(exc).__name__, exc)
    rec["error_info"] = error_info(exc)
    return rec


def rec_error_info(rec):
    """取一条操作记录的错误分型：优先用记录当场存下的结构化结果（异常对象上的
    reason / status 都在里面），没有（老 JSON、别处拼的记录）再从字符串退化解析。"""
    rec = rec or {}
    info = rec.get("error_info")
    if info:
        return info
    info = error_info(rec.get("err") or "")
    if rec.get("err"):
        rec["error_info"] = info
    return info


def reason_ok(info, want):
    """判一条错误是不是 `want` 这个 reason。SDK 没有 reason 字段时按 code+文案退化
    （`error_info` 已经做了退化），所以这里只看结果，另外把出处记在 got 里。"""
    got = info.get("reason")
    detail = "code=%s reason=%s（出处 %s）" % (info.get("code"), got, info.get("reason_src"))
    return got == want, detail


# ---------------------------------------------- T11 / T21 的判定（纯函数）

def judge_t11_op(ok, err, wall_s, limit_s, info=None):
    """T11：被 kill / beta_pause 撞上的那个在途操作，判它有没有「给个说法」。

    需求书两条：**要么成功要么给出明确错误**，且**不得挂死**（服务端两道上限是
    `CHECKPOINT_LOCK_WAIT_TIMEOUT` 60 s + `CHECKPOINT_FC_CALL_TIMEOUT` 120 s，
    所以 200 s 还没回来就是挂了）。客户端读超时也算没给说法 —— 那正是服务端没在
    上限内答话的样子。返回 `(ok, want, got)`。
    """
    want = "成功，或在 %g s 内给出明确错误" % limit_s
    if wall_s is None:
        return False, want, "操作线程没结束（挂住了）"
    if wall_s > limit_s:
        return False, want, "耗时 %.1f s > %g s（挂住了）" % (wall_s, limit_s)
    if ok:
        return True, want, "成功，%.1f s" % wall_s
    text = err or ""
    if not text.strip():
        return False, want, "失败但没有任何错误信息"
    if TIMEOUT_RE.search(text):
        return False, want, "客户端超时，服务端没在上限内答话：%s" % text
    info = info or error_info(text)
    return True, want, "明确失败（%.1f s）：code=%s reason=%s" % (
        wall_s, info["code"], info["reason"])


def judge_reclaim(store_gone, fc_procs, netns_before, netns_after):
    """T11：kill 之后宿主机侧的回收。返回 [(名字, ok, want, got)] 三条。

    netns 只判「不增长」：机器上还有别人的沙箱在起落，绝对值没有意义
    （09-16 清过 8729 个泄漏槽位，那次的教训就是别拿绝对值说事）。
    """
    rows = [("kill 后 checkpoint 目录消失", bool(store_gone), "目录不存在",
             "还在" if not store_gone else "已消失"),
            ("kill 后没有残留 firecracker 进程", not fc_procs, "0 个",
             "还剩 %s" % (fc_procs or []))]
    if netns_before is None or netns_after is None:
        rows.append(("kill 后 netns 数量不增长", None, "不增长", "读不到 netns 数"))
    else:
        rows.append(("kill 后 netns 数量不增长", netns_after <= netns_before,
                     "≤ kill 前的 %d" % netns_before, "kill 后 %d" % netns_after))
    return rows


def judge_hidden_rescue(m):
    """T21：一次失败的 create 有没有把已经推进的 epoch 救成隐藏条目（A3/F5/A2）。

    `m` 是 checkpoint 目录里的 manifest.json。判据：条目还在（目录没被 Discard
    掉）、`state=committed`、`hidden=true`。返回 `(ok, detail)`。
    """
    if not m:
        return False, "checkpoint 目录/manifest 不在了（epoch 被丢了）"
    state, hidden = m.get("state"), bool(m.get("hidden"))
    if state == "committed" and hidden:
        return True, "manifest state=committed hidden=true（epoch 已救成隐藏条目）"
    return False, "manifest state=%s hidden=%s" % (state, hidden)


def judge_chain_incremental(first, second):
    """T21 commit_late：链没有被判 invalid。

    判据不看客户端（armed 时每次 create 都在 Commit 处失败，客户端只看得到 500），
    看第二次 create 在盘上留下的 manifest：`mem_mode=incremental` 且
    `parent_id` = 第一次那个隐藏条目 —— 链断了的话服务端会改走全量新根，
    那就是 `mem_mode=full` + `parent_id` 空。返回 `(ok, want, got)`。
    """
    want = "第二次 create mem_mode=incremental 且 parent_id=%s" % (first or {}).get("id")
    if not second:
        return False, want, "第二次 create 没在盘上留下 manifest"
    got = "mem_mode=%s parent_id=%s" % (second.get("mem_mode"), second.get("parent_id"))
    ok = (second.get("mem_mode") == "incremental"
          and second.get("parent_id") == (first or {}).get("id"))
    return ok, want, got


# ---------------------------------------------- 宿主机侧探测（复制自 checkpoint_concurrent.py）

def fc_get(sandbox_id, path="/"):
    """向这个沙箱的 Firecracker API 套接字发一个 GET。只有在宿主机上才拿得到。"""
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


def orchestrator_uptime_s(_pid=None):
    """orchestrator 进程已经跑了多少秒。取不到返回 None。

    先 `ps -o etimes=`，不行就退回 /proc/<pid>/stat 第 22 个字段（starttime，时钟滴答）
    配 /proc/uptime 自己算。T18 / T36 用它判断网络暖池填没填满。
    """
    pid = _pid
    if pid is None:
        pid, _ = orchestrator_pid_env()
    if not pid:
        return None
    try:
        out = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                             capture_output=True, timeout=30)
        v = out.stdout.decode("utf8", "replace").strip()
        if v:
            return float(v)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        raw = open("/proc/%d/stat" % int(pid)).read()
        fields = raw[raw.rindex(")") + 2:].split()      # comm 里可能有空格和括号
        start_ticks = float(fields[19])                 # 总第 22 个字段 starttime
        hz = float(os.sysconf("SC_CLK_TCK"))
        up = float(open("/proc/uptime").read().split()[0])
        return up - start_ticks / hz
    except (OSError, ValueError, IndexError, ZeroDivisionError):
        return None


def checkpoint_store():
    """checkpoint 产物目录、所在文件系统、所在块设备名。不在宿主机上时返回 (None, "?", None)。"""
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


def conntrack_count():
    """宿主机 conntrack 表项数（T24 用）。读不到返回 None。"""
    for p in ("/proc/sys/net/netfilter/nf_conntrack_count",
              "/proc/sys/net/nf_conntrack_count"):
        try:
            return int(open(p).read().strip())
        except (OSError, ValueError):
            continue
    return None


def netns_count():
    """宿主机上的网络命名空间个数（T11 判「kill 后不泄漏槽位」）。读不到返回 None。

    先数 `/run/netns` 的条目（`ip netns` 就是看这个目录），读不到再退回 `ip netns
    list`。绝对值没意义（920B 上还跑着别人的沙箱），只比前后。
    """
    try:
        return len(os.listdir("/run/netns"))
    except OSError:
        pass
    try:
        out = subprocess.run(["ip", "netns", "list"], capture_output=True, timeout=30)
        return len([l for l in out.stdout.decode("utf8", "replace").splitlines() if l.strip()])
    except (OSError, subprocess.SubprocessError):
        return None


# netns 计数「稳定」判定：orchestrator 启动后网络暖池要预建 NewSlotsPoolSize=300 个
# netns（internal/sandbox/network/pool.go），实测约 10 个/分钟。刚重启过 orchestrator
# 就取 before/after，会把暖池填充算成「泄漏」（09-18 T36 的误报就是这么来的）。
NETNS_STEADY_WINDOW = 20.0      # 连续这么久计数不变就算稳定
# 超时 09-18 从 600 s 降到 60 s：judge 那边已经有 netns_pool_warm() 的降级兜底
# （暖池没填满就把「不泄漏」降成记录项），再死等 10 分钟只是白白拖慢每一次长跑。
NETNS_STEADY_TIMEOUT = 60.0     # 最长等这么久；到点就用当前值，并在日志里附注「未稳定」
NETNS_STEADY_INTERVAL = 2.0     # 采样间隔


def netns_count_steady(window_s=NETNS_STEADY_WINDOW, timeout_s=NETNS_STEADY_TIMEOUT,
                       interval_s=NETNS_STEADY_INTERVAL, _count=None, _sleep=None,
                       _clock=None, _log=None):
    """反复采 netns_count()，直到连续 window_s 内计数不变才返回。

    读不到（netns_count() 给 None）就直接返回 None，判定那边自己会降级成 note。
    到 timeout_s 还没稳下来，就返回当前值，并在日志里附注「未稳定」——宁可判错
    也不要把整条用例卡死在这里。judge_reclaim / judge_netns_steady 本身不动。
    """
    count = _count or netns_count
    sleep = _sleep or time.sleep
    clock = _clock or time.monotonic
    say = _log or log

    v = count()
    if v is None:
        return None
    t0 = clock()
    last_change = t0
    while True:
        if clock() - last_change >= window_s:
            return v
        if clock() - t0 >= timeout_s:
            say("  注意：netns 计数在 %.0f s 内没有稳定下来（当前 %s，窗口 %.0f s），"
                "按当前值继续，后面的 netns 判定未稳定、仅供参考"
                % (timeout_s, v, window_s))
            return v
        sleep(interval_s)
        nv = count()
        if nv is None:
            return v
        if nv != v:
            v = nv
            last_change = clock()


# 暖池 NewSlotsPoolSize=300 个槽位、实测约 10 个/分钟 → 约 30 分钟填满。
# 取 35 分钟留余量：orchestrator 启动不足这么久时，netns 的 before/after 判定不成立。
NETNS_POOL_WARMUP_S = 2100.0


def netns_pool_warm(min_uptime_s=NETNS_POOL_WARMUP_S, _uptime=None):
    """orchestrator 跑够久了没有（网络暖池填满了没有）。

    返回 `(warm, reason)`：`True` = 可以硬判「netns 槽位不泄漏」；`False` = 暖池还在填，
    判定要降级成记录项；`None` = 读不到进程启动时间，同样降级。reason 是给日志用的中文说明。
    """
    up = (_uptime or orchestrator_uptime_s)()
    if up is None:
        return None, "读不到 orchestrator 进程的启动时间，没法确认网络暖池填满了没有"
    if up >= min_uptime_s:
        return True, ("orchestrator 已跑 %.0f 分钟（≥ %.0f 分钟），网络暖池按 300 槽位"
                      "算早该填满了" % (up / 60.0, min_uptime_s / 60.0))
    return False, ("orchestrator 只跑了 %.0f 分钟（< %.0f 分钟），网络暖池 300 个槽位"
                   "还在填（实测约 10 个/分钟），before/after 会把填充算成泄漏"
                   % (up / 60.0, min_uptime_s / 60.0))


def fc_processes(sandbox_id):
    """还活着的、属于这个沙箱的 firecracker 进程 pid 列表。

    判据是命令行里那个 `--api-sock /tmp/fc-<沙箱 id>-<后缀>.sock`（920B 实测的
    cmdline 形状），所以不会误伤别人的沙箱。不在宿主机上就是空列表。
    """
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open("/proc/%s/cmdline" % pid, "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        if "firecracker" in cmd and ("fc-%s-" % sandbox_id) in cmd:
            hits.append(int(pid))
    return hits


def read_manifests(store, sandbox_id):
    """读这个沙箱在盘上的全部 checkpoint manifest，按 id 升序（id 是
    `ckpt_<UnixNano>`，字符串序就是时间序）。

    这是 T21 的主要「秤」：armed 的服务端把每次 create 都打回 500，客户端什么都
    看不到，而 `parent_id` / `hidden` / `state` / `mem_mode` 这些后置状态全在
    manifest 里（store.go 的 Entry 结构）。不在宿主机上返回 []。
    """
    if not store:
        return []
    out = []
    try:
        names = sorted(os.listdir(os.path.join(store, sandbox_id)))
    except OSError:
        return []
    for name in names:
        if not name.startswith("ckpt_"):
            continue
        try:
            with open(os.path.join(store, sandbox_id, name, "manifest.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            m = {"id": name, "manifest": "unreadable"}
        out.append(m)
    return out


class OrchLog:
    """orchestrator 的 nomad 日志，只读新增的部分。

    路径是从 orchestrator 进程的 fd 1/2 反查出来的（`/proc/<pid>/fd/1` 指向
    `<alloc>/alloc/logs/.start.stdout.fifo`），所以不写死 alloc id。构造时记下
    每个日志文件当前的长度，`since()` 只返回那之后追加的内容 —— 920B 上还跑着
    别人的沙箱，grep 全量日志既慢又会翻出别人的东西（README §5 的口径）。
    """

    def __init__(self):
        self.dir = None
        self.marks = {}
        pid, _ = orchestrator_pid_env()
        if not pid:
            return
        for fd in ("1", "2"):
            try:
                target = os.path.realpath("/proc/%d/fd/%s" % (pid, fd))
            except OSError:
                continue
            d = os.path.dirname(target)
            if os.path.basename(d) == "logs":
                self.dir = d
                break
        self.mark()

    def files(self):
        if not self.dir:
            return []
        return sorted(glob.glob(os.path.join(self.dir, "start.std*")))

    def mark(self):
        for p in self.files():
            try:
                self.marks[p] = os.path.getsize(p)
            except OSError:
                self.marks[p] = 0

    def since(self, pattern=None):
        """取标记点之后追加的日志行；给了 pattern 就只留命中的行。

        这是**一次性**的读：nomad 的 logmon 是批量刷盘的，操作刚返回时该打的那行
        很可能还没落到文件里。判定用的读一律走 `wait_for_log()` 轮询，别直接拿
        `since()` 的结果当断言。
        """
        rx = re.compile(pattern) if pattern else None
        lines = []
        for p in self.files():
            start = self.marks.get(p, 0)
            try:
                with open(p, "r", errors="replace") as f:
                    f.seek(min(start, os.path.getsize(p)))
                    for line in f:
                        if rx is None or rx.search(line):
                            lines.append(line.rstrip("\n"))
            except OSError:
                continue
        return lines


# nomad 的 logmon 把 orchestrator 的 stdout/stderr 批量刷进 alloc 日志文件，第 4
# 轮验收实测滞后 0.05–1.3 s（批间隔约 1.6 s）。"操作一返回就 grep 日志"因此会读到
# 空 —— 那是竞态误判，不是服务端没打这一行。判定日志一律走下面这个轮询重读。
LOG_WAIT_TIMEOUT = 5.0          # 最长等多久
LOG_WAIT_INTERVAL = 0.5         # 每隔多久重读一次


def wait_for_log(orch, pattern, timeout=LOG_WAIT_TIMEOUT, interval=LOG_WAIT_INTERVAL,
                 sleep=time.sleep, clock=time.monotonic):
    """轮询重读标记点之后的日志，直到有行命中 `pattern`，或等满 `timeout`。

    `since()` 每次都是从标记点重读全量，所以重试是幂等的，不会漏掉两次读之间刷下来
    的行。返回 `(lines, waited_s, reads)`：`lines` 为空 = 真等超时了；`waited_s` 与
    `reads` 要写进断言的「实际」里 —— 「日志里没有这一行」和「等了 5 s 还没刷出来」
    是两件事，报告里得分得开。

    `sleep` / `clock` 留成参数是给单测打桩用的（不然一个用例要真等 5 s）。
    """
    t0 = clock()
    reads = 0
    while True:
        lines = orch.since(pattern)
        reads += 1
        waited = clock() - t0
        if lines:
            return lines, waited, reads
        if waited >= timeout:
            return [], waited, reads
        sleep(min(interval, max(0.0, timeout - waited)))


class HostMeter:
    """产物盘写入量（/proc/diskstats 第 10 列，扇区）+ orchestrator CPU/RSS。
    不在宿主机上时全部 None。"""

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


# ---------------------------------------------------------------- 断言

class Failed(Exception):
    """一条断言没过。带三段式：期望 / 实际 / 依据。"""

    def __init__(self, name, want, got, basis):
        self.name = name
        self.want = want
        self.got = got
        self.basis = basis
        super().__init__(self.text())

    def text(self):
        return ("断言失败：%s\n    期望：%s\n    实际：%s\n    依据：%s"
                % (self.name, self.want, self.got, self.basis))


class Unmet(Exception):
    """前置条件不满足 —— 退出码 3，和「断言没过」（1）分开。

    T21 用它：故障注入要 orchestrator 启动时带 `CHECKPOINT_FAULT_INJECT=<名字>`，
    这是换栈时改 nomad job env 的事，不是用例能做的。用例只负责**检测**当前服务端
    带没带这个注入，检测不到就打印怎么设 env 然后以 3 退出 —— 判失败会把「没装钩子」
    和「钩子坏了」混成一件事。
    """

    def __init__(self, what, how):
        self.what = what
        self.how = how
        super().__init__(self.text())

    def text(self):
        return "前置不满足：%s\n    怎么办：%s" % (self.what, self.how)


def expect(ctx, name, ok, want, got, basis):
    """硬断言：不过就抛 Failed（失败即停）。每条都记进 JSON 的 assertions。"""
    rec = {"name": name, "ok": bool(ok), "want": str(want), "got": str(got), "basis": basis}
    ctx.results["assertions"].append(rec)
    log("  %s %s：期望 %s，实际 %s" % ("✓" if ok else "✗", name, want, got))
    if not ok:
        raise Failed(name, want, got, basis)
    return True


def note(ctx, name, want, got, basis):
    """只记录不判定（需求书里明说"记录曲线"的那些）。"""
    ctx.results["assertions"].append({"name": name, "ok": None, "want": str(want),
                                      "got": str(got), "basis": basis})
    log("  · %s：%s（参考：%s）" % (name, got, want))


# ---------------------------------------------------------------- 沙箱

_ALL_BOXES = []
_KEEP = []


def _kill_all():
    """兜底：正常结束、异常、Ctrl-C 都要把沙箱收掉，除非被 --keep-on-failure 留住。"""
    for b in list(_ALL_BOXES):
        if b in _KEEP:
            continue
        b.kill()
        if b in _ALL_BOXES:
            _ALL_BOXES.remove(b)


atexit.register(_kill_all)


class Box:
    """一个沙箱。现场（scene）的定义与 checkpoint_concurrent.py 一致：
    /dev/shm/gen 内存代号、<BENCH_DIR>/gen 文件代号、两个 blob 前 4 MB 的 md5、
    心跳后台进程 pid（pid 不变 = 内存真回来了而不是虚机重启）。"""

    def __init__(self, sbx, store, label):
        self.sbx = sbx
        self.store = store
        self.label = label
        self.id = sbx.sandbox_id
        self.scenes = {}
        self.names = {}
        self.created = []         # 本地账本：成功建出来的 checkpoint id（按顺序）

    # ---- guest 里干活

    def run(self, cmd, timeout=600):
        return self.sbx.commands.run(cmd, user="root", timeout=timeout).stdout

    def sh(self, script, timeout=600):
        """跑一段脚本并且**永远返回 0**：退出码自己 echo 出来，免得 SDK 抛
        CommandExitException 把真正要看的输出吃掉。

        脚本放在**子 shell**（圆括号）里而不是 `{ }`：`{ }` 不开子进程，脚本里
        一句 `exit 1` 会把整个 shell 一起带走，`echo __rc=` 那行根本轮不到跑 ——
        本地打桩冒烟时踩到过。"""
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

    def kv(self, out):
        return parse_kv(out)

    def put(self, path, text, timeout=120):
        """往 guest 里塞一个文本文件（走 base64，避免引号/换行在 shell 里出事）。"""
        blob = base64.b64encode(text.encode("utf8")).decode("ascii")
        chunks = [blob[i:i + 3000] for i in range(0, len(blob), 3000)]
        self.run("rm -f %s.b64" % path, timeout=timeout)
        for c in chunks:
            self.run("printf '%%s' '%s' >> %s.b64" % (c, path), timeout=timeout)
        self.run("base64 -d < %s.b64 > %s && rm -f %s.b64" % (path, path, path), timeout=timeout)

    def bg(self, cmd, pidfile, timeout=120):
        """后台起一个进程，pid 写进 pidfile，返回 pid（字符串）。"""
        self.run("rm -f %s" % pidfile, timeout=timeout)
        self.run("setsid sh -c 'echo $$ > %s; exec %s' >/dev/null 2>&1 </dev/null &\nsleep 0.5"
                 % (pidfile, cmd), timeout=timeout)
        return self.run("cat %s" % pidfile, timeout=timeout).strip()

    def sig(self, pid, signame, timeout=60):
        self.sh("kill -%s %s" % (signame, pid), timeout=timeout)

    def pid_state(self, pid, timeout=60):
        """进程还在不在、在什么状态（R/S/T/Z）。不在返回 None。"""
        rc, out = self.sh("awk '{print $3}' /proc/%s/stat 2>/dev/null" % pid, timeout=timeout)
        for line in out.splitlines():
            line = line.strip()
            if len(line) == 1 and line.isalpha():
                return line
        return None

    # ---- 现场

    def setup(self, warm_mem=96, warm_file=32):
        self.run("mkdir -p %s; rm -f %s %s" % (BENCH_DIR, HB_LOG, HB_PID))
        self.run("setsid sh -c 'echo $$ > %s; while true; do echo tick >> %s; sleep 0.2; done'"
                 " >/dev/null 2>&1 </dev/null &\nsleep 0.3" % (HB_PID, HB_LOG))
        self.run("dd if=/dev/urandom of=/dev/shm/sweep bs=1M count=%d 2>/dev/null" % warm_mem)
        self.run("dd if=/dev/urandom of=%s/fsblob bs=1M count=%d 2>/dev/null; sync"
                 % (BENCH_DIR, warm_file), timeout=1800)

    def dirty(self, gen, mem_mb=48, file_mb=16):
        self.run("dd if=/dev/urandom of=/dev/shm/sweep bs=1M count=%d conv=notrunc 2>/dev/null; "
                 "dd if=/dev/urandom of=%s/fsblob bs=1M count=%d conv=notrunc 2>/dev/null; "
                 "echo %s > /dev/shm/gen; echo %s > %s/gen; sync"
                 % (mem_mb, BENCH_DIR, file_mb, gen, gen, BENCH_DIR), timeout=1800)

    def scene(self):
        return self.kv(self.run(
            "echo mem_gen=$(cat /dev/shm/gen 2>/dev/null || echo MISSING)\n"
            "echo file_gen=$(cat %s/gen 2>/dev/null || echo MISSING)\n"
            "echo mem_md5=$(head -c 4194304 /dev/shm/sweep | md5sum | cut -c1-12)\n"
            "echo file_md5=$(head -c 4194304 %s/fsblob | md5sum | cut -c1-12)\n"
            "echo hb_pid=$(cat %s 2>/dev/null || echo MISSING)\n"
            % (BENCH_DIR, BENCH_DIR, HB_PID)))

    def alive(self, timeout=120):
        d = self.kv(self.run(
            "echo cmd=$(echo alive)\n"
            "echo rw=$(echo n$$ > %s/probe && cat %s/probe)\n"
            "echo hb0=$(wc -l < %s 2>/dev/null || echo 0); sleep 1.5\n"
            "echo hb1=$(wc -l < %s 2>/dev/null || echo 0)\n"
            % (BENCH_DIR, BENCH_DIR, HB_LOG, HB_LOG), timeout=timeout))
        try:
            ticks = int(d.get("hb1", 0)) - int(d.get("hb0", 0))
        except ValueError:
            ticks = -1
        return d.get("cmd") == "alive" and d.get("rw", "").startswith("n") and ticks >= 1, d

    # ---- 服务端分段（只有在宿主机上跑才有）

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

    def store_dir(self, ck_id=None):
        if not self.store:
            return "?（不在宿主机上）"
        return os.path.join(self.store, self.id, ck_id) if ck_id else os.path.join(self.store, self.id)

    # ---- 操作（每个都返回一条原始记录，结构与并发报告 §9.2 兼容）

    def create(self, gen, record_scene=True, timeout=None):
        rec = {"op": "create", "box": self.label, "sandbox": self.id, "gen": gen,
               "t": time.time()}
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
            self.created.append(ck.checkpoint_id)
        except Exception as e:      # noqa: BLE001 —— 记下来就是目的
            rec["wall_s"] = time.monotonic() - t0
            rec["ok"] = False
            record_error(rec, e)
        return rec

    def restore(self, ck_id, verify=True, timeout=None):
        rec = {"op": "restore", "box": self.label, "sandbox": self.id, "id": ck_id,
               "gen": self.names.get(ck_id, "?"), "t": time.time()}
        t0 = time.monotonic()
        try:
            ok = self.sbx.checkpoint.restore(ck_id, request_timeout=timeout)
            rec["wall_s"] = time.monotonic() - t0
            rec["phases"] = self.restore_phases()     # 必须紧接着读，下一次 restore 会覆盖
            rec["ok"] = bool(ok)
        except Exception as e:      # noqa: BLE001
            rec["wall_s"] = time.monotonic() - t0
            rec["ok"] = False
            record_error(rec, e)
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

    def list_rec(self):
        rec = {"op": "list", "box": self.label, "sandbox": self.id, "t": time.time()}
        t0 = time.monotonic()
        try:
            rec["ids"] = self.list_ids()
            rec["ok"] = True
        except Exception as e:      # noqa: BLE001
            rec["ok"] = False
            record_error(rec, e)
        rec["wall_s"] = time.monotonic() - t0
        return rec

    def delete(self, ck_id):
        rec = {"op": "delete", "box": self.label, "sandbox": self.id, "id": ck_id,
               "t": time.time()}
        t0 = time.monotonic()
        try:
            rec["ok"] = bool(self.sbx.checkpoint.delete(ck_id))
        except Exception as e:      # noqa: BLE001
            rec["ok"] = False
            record_error(rec, e)
        rec["wall_s"] = time.monotonic() - t0
        self.scenes.pop(ck_id, None)
        self.names.pop(ck_id, None)
        return rec

    def kill(self):
        try:
            self.sbx.kill()
        except Exception:           # noqa: BLE001
            pass


def sandbox_create(template, private=True, timeout=3600):
    """建一个沙箱。默认建**私有**沙箱（`allow_public_traffic=False`）：K1 修的就是
    这条路（方案 §1 第 1 轮 K1），日常用例顺带把它一直回归着。"""
    from e2b import Sandbox
    kw = {"template": template, "timeout": timeout}
    if private:
        kw["network"] = {"allow_public_traffic": False}
    return Sandbox.create(**kw)


def spawn(ctx, n, prefix="b", timeout=3600):
    """并行建 n 个沙箱，全部登记进兜底 kill 列表。返回 [Box]。"""
    boxes = [None] * n
    errs = [None] * n

    def one(i):
        try:
            sbx = sandbox_create(ctx.args.template, private=not ctx.args.public, timeout=timeout)
            boxes[i] = Box(sbx, ctx.store, "%s%d" % (prefix, i))
        except Exception as e:      # noqa: BLE001
            errs[i] = "%s: %s" % (type(e).__name__, e)

    t0 = time.monotonic()
    ts = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.monotonic() - t0
    got = [b for b in boxes if b]
    _ALL_BOXES.extend(got)
    ctx.results["spawn"].append({"n": n, "ok": len(got), "wall_s": dt,
                                 "errs": [e for e in errs if e]})
    log("  建沙箱：%d 成功 %d 失败，并行总耗时 %.1f s" % (len(got), n - len(got), dt))
    for e in errs:
        if e:
            log("    建沙箱失败：%s" % e)
    if len(got) != n:
        raise Failed("建沙箱", "%d 个全部建出来" % n, "只建出 %d 个：%s" % (len(got), [e for e in errs if e]),
                     "用例前置条件")
    return got


def connect_box(ctx, sandbox_id, label):
    """另开一个客户端连到同一个沙箱（T12 的"多个独立调用方"）。"""
    from e2b import Sandbox
    return Box(Sandbox.connect(sandbox_id), ctx.store, label)


def forget(box):
    """这个沙箱已经不在了（T11 把它 kill 掉了），从兜底 kill 名单里划掉。"""
    if box in _ALL_BOXES:
        _ALL_BOXES.remove(box)


def keep(box):
    """--keep-on-failure 时留下这个沙箱不 kill。"""
    if box not in _KEEP:
        _KEEP.append(box)


# ---------------------------------------------------------------- 运行框架

class Ctx:
    def __init__(self, args, store, fstype, dev, meter, results):
        self.args = args
        self.store = store
        self.fstype = fstype
        self.dev = dev
        self.meter = meter
        self.results = results

    def op(self, rec, **extra):
        """记一条原始操作记录（并发报告 §9.2 的 ops 结构）。

        失败的记录一定带上结构化的 `error_info`：`record_error()` 记的那份是从
        **异常对象**来的（reason / status 是 field 级），这里只给别处拼的记录兜底补。
        """
        rec.update(extra)
        if rec.get("err") and not rec.get("error_info"):
            rec["error_info"] = error_info(rec["err"])
        self.results["ops"].append(rec)
        return rec

    def stage(self, name, **kv):
        st = {"stage": name, "t": time.time()}
        st.update(kv)
        self.results["stages"].append(st)
        return st


def add_common_args(ap):
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                    help="dotenv 文件路径（默认取环境变量 CRTEST_ENV_FILE，没有就是 "
                         "$E2B_DEPLOY_DIR/.env；本机当前解析成 %s）" % DEFAULT_ENV_FILE)
    ap.add_argument("--template", default=DEFAULT_TEMPLATE_ID, help="模板 id")
    ap.add_argument("--out", default=None, help="原始数据 JSON 路径（默认 <用例>-<时间>.json）")
    ap.add_argument("--keep-on-failure", action="store_true",
                    help="失败时不 kill 沙箱，打印沙箱 id 与 checkpoint 目录留现场")
    ap.add_argument("--public", action="store_true",
                    help="建公网沙箱（默认建 allow_public_traffic=False 的私有沙箱，顺带回归 K1）")
    ap.add_argument("--no-probe", action="store_true",
                    help="跳过开跑前那次「脏页后端」探测（省一个沙箱的建/删）")
    return ap


def load_env(path):
    """显式路径加载 dotenv：跑脚本的目录不固定，靠 CWD 找 .env 已经踩过坑。"""
    if not path:
        return False
    if not os.path.exists(path):
        log("!! 找不到 env 文件 %s —— 沙箱 API 的地址/密钥要么已在环境变量里，要么马上会连不上" % path)
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        log("!! 没装 python-dotenv，改成自己解析 %s" % path)
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return True
    load_dotenv(path, override=False)
    return True


def prepare(case, args):
    """开跑前的固定动作：加载 env、探测产物盘、（可选）问一次脏页后端、建 results 骨架。"""
    load_env(args.env_file)
    store, fstype, dev = checkpoint_store()
    meter = HostMeter(dev)
    log("用例 = %s   模板 = %s   私有沙箱 = %s" % (case, args.template, not args.public))
    if store:
        log("产物落盘 : %s   文件系统 = %s   块设备 = %s" % (store, fstype, dev))
    else:
        log("产物落盘 : 未知（不在宿主机上？）服务端分段那几列会缺省。")

    results = {"meta": {"case": case, "template": args.template, "store": store,
                        "fstype": fstype, "host": socket.gethostname(),
                        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "args": vars(args)},
               "stages": [], "ops": [], "spawn": [], "summary": {}, "assertions": []}
    ctx = Ctx(args, store, fstype, dev, meter, results)

    if not args.no_probe:
        backend, fc = probe_backend(ctx)
        results["meta"]["dirty_tracking"] = backend
        results["meta"]["fc"] = fc
        log("脏页后端 : %s   [FC %s]" % (backend, fc))
        if backend == "off":
            raise Failed("脏页跟踪开着", "dirty_tracking != off",
                         "dirty_tracking = off（每次 checkpoint 都是全量，数字没意义）",
                         "FC_TRACK_DIRTY_PAGES；并发报告 §4")
    return ctx


def probe_backend(ctx):
    """起一个沙箱问一下 Firecracker 的脏页后端与版本，问完就删。"""
    sbx = None
    try:
        sbx = sandbox_create(ctx.args.template, private=not ctx.args.public, timeout=120)
        info = fc_get(sbx.sandbox_id) or {}
        return info.get("dirty_tracking", "?"), info.get("vmm_version", "?")
    except Exception as e:      # noqa: BLE001
        log("  探测脏页后端失败（不影响用例本身）：%s" % e)
        return "?", "?"
    finally:
        if sbx is not None:
            try:
                sbx.kill()
            except Exception:       # noqa: BLE001
                pass


def finish(ctx, case, rc, exc=None):
    """收尾：打现场、写 JSON、kill 沙箱。返回进程退出码。"""
    res = ctx.results
    res["meta"]["elapsed_s"] = time.time() - time.mktime(
        time.strptime(res["meta"]["started"], "%Y-%m-%d %H:%M:%S"))
    if exc is not None:
        res["meta"]["failure"] = exc.text() if hasattr(exc, "text") else str(exc)
        log("")
        log(exc.text() if hasattr(exc, "text") else "脚本异常：\n" + traceback.format_exc())
        if ctx.args.keep_on_failure:
            log("")
            log("  --keep-on-failure：以下沙箱不 kill，现场留着")
            for b in _ALL_BOXES:
                keep(b)
                log("    沙箱 %s" % b.id)
                log("      产物目录 %s" % b.store_dir())
                for ck in b.created:
                    log("      checkpoint %s → %s" % (ck, b.store_dir(ck)))
            res["meta"]["kept"] = [{"sandbox": b.id, "dir": b.store_dir(),
                                    "checkpoints": {c: b.store_dir(c) for c in b.created}}
                                   for b in _ALL_BOXES]
    out = ctx.args.out or "%s-%s.json" % (case.lower(), now_tag())
    with open(out, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    log("")
    log("原始数据：%s（%d 条操作记录，%d 条断言）"
        % (out, len(res["ops"]), len(res["assertions"])))
    _kill_all()
    return rc


def run_case(case, mod, args):
    """所有用例共用的外壳：prepare → run → finish，失败即停。"""
    ctx = None
    try:
        ctx = prepare(case, args)
        mod.run(ctx)
    except Failed as e:
        if ctx is None:
            log(e.text())
            return 1
        return finish(ctx, case, 1, e)
    except Unmet as e:
        # 前置不满足：3。不是失败，也不是通过。
        if ctx is None:
            log(e.text())
            return 3
        return finish(ctx, case, 3, e)
    except KeyboardInterrupt:
        log("\n中断。已有数据照写。")
        return finish(ctx, case, 130) if ctx else 130
    except Exception as e:          # noqa: BLE001
        if ctx is None:
            log("脚本异常：\n" + traceback.format_exc())
            return 1
        return finish(ctx, case, 1, e)
    log("")
    log("== %s 通过（%d 条断言）==" % (case, sum(1 for a in ctx.results["assertions"] if a["ok"])))
    return finish(ctx, case, 0)


# ---------------------------------------------------------------- guest 里跑的小程序
#
# 都是 python3 源码（模板 base 的 guest 里有 python3）。用 Box.put() 塞进去，
# 不走 shell 引号。

# 内存写者：mmap 一个 tmpfs 文件（MAP_SHARED），这样"内存内容"在 guest 外部
# 也能用 md5sum 直接算，不必让写者自己报。每页 16 字节页头 + 确定性 body，
# 页头里记页号、版本号、body 的 crc32 —— 于是**单页自校验**：回滚撕裂
# （半旧半新、页错位）会当场看出来，不需要"期望快照"。
GUEST_MEM_WRITER = r'''
import hashlib, mmap, os, random, struct, sys, time, zlib

path, mb, log = sys.argv[1], int(sys.argv[2]), sys.argv[3]
ctl_path = sys.argv[4] if len(sys.argv) > 4 else '/dev/shm/t14-inprogress'
size = mb << 20
PAGE = 4096
npages = size // PAGE

fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
os.ftruncate(fd, size)
m = mmap.mmap(fd, size)
os.close(fd)

# 控制页（也在 tmpfs 里，所以一起进快照）：写某一页之前先把页号放进来，写完清成 -1。
# 于是"快照拍在写这一页的中途"这件事在快照里留了名字 —— 校验端据此把它和
# "回滚把两个时刻的页混了"区分开（见 common.judge_page_scan）。
cfd = os.open(ctl_path, os.O_RDWR | os.O_CREAT, 0o644)
os.ftruncate(cfd, PAGE)
c = mmap.mmap(cfd, PAGE)
os.close(cfd)
NONE = struct.pack('<i', -1)
c[0:4] = NONE


def body_of(i, v):
    seed = struct.pack('<II', i, v) * 8
    return (seed * (PAGE // len(seed) + 1))[:PAGE - 16]


def write_page(i, v):
    # 先把整页在栈上拼好，再一次 memcpy 进 mmap：窗口缩到这一次 memcpy 里。
    b = body_of(i, v)
    buf = struct.pack('<IIQ', i, v, zlib.crc32(b) & 0xffffffff) + b
    c[0:4] = struct.pack('<i', i)
    m[i * PAGE:(i + 1) * PAGE] = buf
    c[0:4] = NONE


ver = [0] * npages
for i in range(npages):
    write_page(i, 0)

open(log + '.ready', 'w').write('1')
rnd = random.Random(20260917)
seq = 0
while True:
    t0 = time.time()
    while time.time() - t0 < 1.0:
        for _ in range(512):
            i = rnd.randrange(npages)
            ver[i] += 1
            write_page(i, ver[i])
    seq += 1
    h = hashlib.md5(m).hexdigest()[:12]
    with open(log, 'a') as f:
        f.write('%d %.3f %s\n' % (seq, time.time(), h))
'''

# 内存校验器：扫一遍，报页数与坏页数（页号错位 / body 与页头声明的版本对不上 /
# crc 对不上 都算坏）。
GUEST_MEM_VERIFY = r'''
import struct, sys, zlib

path = sys.argv[1]
ctl_path = sys.argv[2] if len(sys.argv) > 2 else ''
PAGE = 4096
MAX_LIST = 16
bad = 0
n = 0
first_bad = -1
bad_pages = []
with open(path, 'rb') as f:
    while True:
        p = f.read(PAGE)
        if len(p) < PAGE:
            break
        i, v, c = struct.unpack('<IIQ', p[:16])
        b = p[16:]
        seed = struct.pack('<II', i, v) * 8
        exp = (seed * (PAGE // len(seed) + 1))[:PAGE - 16]
        if i != n or b != exp or c != (zlib.crc32(b) & 0xffffffff):
            bad += 1
            if first_bad < 0:
                first_bad = n
            if len(bad_pages) < MAX_LIST:
                bad_pages.append(n)
        n += 1

# 快照那一刻写者正在写哪一页（-1 = 没在写）。写者停着的时候读，读到的就是快照值。
inprogress = -1
if ctl_path:
    try:
        with open(ctl_path, 'rb') as f:
            d = f.read(4)
        if len(d) == 4:
            inprogress = struct.unpack('<i', d)[0]
    except OSError:
        inprogress = -1
print('pages=%d' % n)
print('bad=%d' % bad)
print('first_bad=%d' % first_bad)
print('bad_pages=%s' % ','.join(str(x) for x in bad_pages))
print('inprogress=%d' % inprogress)
'''

# 文件写者：一条一条 append `seq crc32(payload)` 并 fsync，payload 由 seq 派生，
# 于是每一行都能自校验；同时把"当前 seq"留在 /dev/shm 里给现场判定用。
GUEST_FILE_WRITER = r'''
import os, sys, time, zlib

path, marker = sys.argv[1], sys.argv[2]
f = open(path, 'ab', buffering=0)
seq = 0
while True:
    seq += 1
    payload = (b'%d-' % seq) * 64
    line = b'%d %d\n' % (seq, zlib.crc32(payload) & 0xffffffff)
    mv = memoryview(line)
    while mv:                     # 短写补齐：半条只可能停在文件末尾
        mv = mv[f.write(mv):]
    os.fsync(f.fileno())
    open(marker, 'w').write('%d' % seq)
    time.sleep(0.05)
'''

# 文件校验器：每行的 crc 必须与 seq 派生的 payload 对得上，且 seq 连续。
GUEST_FILE_VERIFY = r'''
import sys, zlib

path = sys.argv[1]
bad = 0
n = 0
last = 0
prev = None
with open(path, 'rb') as fh:
    data = fh.read()
rows = data.split(b'\n')
# 追加写 + 短写补齐 ⇒ 半条只可能落在文件末尾（快照拍在那一次 write() 中途）。
# 它单独报 tail_partial，不混进 bad —— 中间出现半条才是真错。
tail_partial = 1 if rows and rows[-1] else 0
rows = rows[:-1]
for line in rows:
    f = line.split()
    if len(f) != 2:
        bad += 1
        continue
    try:
        seq, crc = int(f[0]), int(f[1])
    except ValueError:
        bad += 1
        continue
    payload = (b'%d-' % seq) * 64
    if crc != (zlib.crc32(payload) & 0xffffffff):
        bad += 1
    if prev is not None and seq != prev + 1:
        bad += 1
    prev = seq
    last = seq
    n += 1
print('lines=%d' % n)
print('bad=%d' % bad)
print('tail_partial=%d' % tail_partial)
print('last_seq=%d' % last)
'''

# 心跳：10 ms 一条，同时记 CLOCK_REALTIME 和 CLOCK_MONOTONIC。
# 为什么两个都要：restore 把 guest 搬回快照那一刻，monotonic 会倒退 ——
# 倒退点就是"这条心跳文件被回滚过"的标记，用来切段（见 heartbeat_gaps）。
GUEST_HEARTBEAT = r'''
import sys, time

path, dt = sys.argv[1], float(sys.argv[2])
f = open(path, 'a')
while True:
    f.write('%.6f %.6f\n' % (time.time(), time.monotonic()))
    f.flush()
    time.sleep(dt)
'''

# 流式写者（T23）：一直往一个文件里追加 64 KB 的确定性块（第 k 块全是 seq 派生的
# 字节），**不 fsync**，fd 一直开着。校验的是"文件必须是这条确定性流的一个前缀"。
GUEST_STREAM_WRITER = r'''
import os, sys, time

path, marker = sys.argv[1], sys.argv[2]
BLK = 65536
f = open(path, 'wb', buffering=0)
k = 0
while True:
    f.write(bytes([(k * 7 + 13) & 0xff]) * BLK)
    k += 1
    open(marker, 'w').write('%d' % k)
    time.sleep(0.01)
'''

GUEST_STREAM_VERIFY = r'''
import sys

path = sys.argv[1]
BLK = 65536
k = 0
bad = 0
tail = 0
with open(path, 'rb') as f:
    while True:
        b = f.read(BLK)
        if not b:
            break
        want = bytes([(k * 7 + 13) & 0xff]) * len(b)
        if b != want:
            bad += 1
        if len(b) < BLK:
            tail = len(b)
        k += 1
print('blocks=%d' % k)
print('bad=%d' % bad)
print('tail=%d' % tail)
'''

# 持有 fd 的写者（T23）：快照前就打开着 fd，每 0.2 s 写一行序号。
# restore 之后它回到快照那一刻的状态，应当**接着那时的序号**往下写。
GUEST_FD_WRITER = r'''
import os, sys, time

path, marker = sys.argv[1], sys.argv[2]
f = open(path, 'a', buffering=1)
seq = 0
while True:
    seq += 1
    f.write('%d\n' % seq)
    f.flush()
    open(marker, 'w').write('%d' % seq)
    time.sleep(0.2)
'''

# loopback 回声服务（T24）：guest 内一个 TCP server，客户端保持一条长连接，
# 每次发一个序号、收回来核对。两端都在 guest 里，所以 restore 之后这条连接
# 应当**照常可用**（整机回滚，连接的两端一起回去）。
GUEST_NET_PEER = r'''
import socket, sys, threading, time

port = int(sys.argv[1])
state = sys.argv[2]

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', port))
srv.listen(4)


def serve():
    while True:
        c, _ = srv.accept()
        threading.Thread(target=echo, args=(c,), daemon=True).start()


def echo(c):
    try:
        while True:
            b = c.recv(64)
            if not b:
                return
            c.sendall(b)
    except OSError:
        return


threading.Thread(target=serve, daemon=True).start()
time.sleep(0.3)
cli = socket.create_connection(('127.0.0.1', port), timeout=5)
n = 0
while True:
    n += 1
    try:
        cli.sendall(b'%d\n' % n)
        got = cli.recv(64)
        ok = got.strip() == b'%d' % n
    except OSError as e:
        ok = False
        got = str(e).encode()
    open(state, 'w').write('n=%d ok=%d got=%s\n' % (n, 1 if ok else 0, got.strip().decode('ascii', 'replace')))
    time.sleep(0.2)
'''


# ---------------------------------------------- T18 / T34 / T36 的判定（纯函数）
#
# 和 T11/T21 的那几个一样：只吃数据、不碰网络/磁盘，好让 tests/ 里离线测到。

def judge_full_root(rec, manifest):
    """T18：重启之后第一次 create 必须是**全量新根**。

    两把秤，都要对上：

      · 客户端拿到的 `mem_mode`（`Box.create()` 从 CreateCheckpoint 的响应里取，
        SDK 没这个字段时是 "?"）；
      · 盘上的 manifest（`read_manifests()`）：`mem_mode=full` 且 `parent_id` 空。

    `NewStore` 在启动时 `os.RemoveAll(root)`（store.go:232），内存里的
    `bySandbox`/`bases` 也是新建的空 map，所以重启之后任何沙箱的第一次 create
    都没有 parent 可挂 —— 挂上了就说明旧账本活过了重启（F8 要验的正是这条）。

    客户端那份是 "?"（老 SDK）时不判它，只判 manifest —— 但把这件事写进 got。
    返回 `(ok, want, got)`。
    """
    want = "mem_mode=full 且 parent_id 空"
    cli = (rec or {}).get("mem_mode") or "?"
    if not manifest:
        return False, want, "盘上没有 manifest（客户端报 mem_mode=%s）" % cli
    disk_mode = manifest.get("mem_mode")
    parent = manifest.get("parent_id") or ""
    got = "manifest mem_mode=%s parent_id=%s；客户端 mem_mode=%s" % (
        disk_mode, parent or "（空）", cli)
    ok = disk_mode == "full" and not parent
    if ok and cli not in ("?", "", None):
        ok = cli == "full"
    return ok, want, got


def judge_store_cleared(before, after, keep=()):
    """T18：重启把 checkpoint store 根清空了。

    `before` / `after` 是重启前后 store 根目录下的条目名（沙箱 id）。判据是
    **重启前那批一个都不剩**，而不是"重启后目录为空" —— 920B 上还有别人的沙箱，
    重启之后别人（或本用例的沙箱 B）会立刻建出新目录来，拿绝对为空判会误报。
    `keep` 是允许出现在 after 里的新条目（比如沙箱 B）。

    返回 `(ok, want, got)`。
    """
    before = sorted(set(before or ()))
    after = set(after or ())
    survivors = sorted(x for x in before if x in after)
    strangers = sorted(x for x in after if x not in before and x not in set(keep))
    want = "重启前的 %d 个沙箱目录全部消失" % len(before)
    if survivors:
        return False, want, "还剩 %d 个：%s" % (len(survivors), survivors)
    got = "全部消失"
    if strangers:
        got += "（重启后新出现的不算：%s）" % strangers
    return True, want, got


def judge_restarted(pid_before, pid_after):
    """T18：orchestrator 真的换了进程。读不到 pid 时返回 ok=None（记录项）。"""
    want = "orchestrator pid 变了（旧 %s）" % (pid_before if pid_before else "未知")
    if not pid_before or not pid_after:
        return None, want, "pid 读不到（重启前 %s，重启后 %s）" % (pid_before, pid_after)
    return pid_after != pid_before, want, "重启后 pid=%s" % pid_after


def judge_read_slowdown(rows, max_slowdown=3.0):
    """T34：读链深对 guest 冷读的拖慢。**只算不判**（返回的 ok 恒为 None），
    超过 `max_slowdown` 的行打上 `warn=True`。

    `rows` = [{"depth": n, "phase": "after-create"|"after-restore", "mbps": x,
               "p50_ms": y, "p99_ms": z}, ...]。基线取 depth 最小的那一档里
    **同 phase** 的 mbps（一般是 depth=0）；同 phase 没有基线就退回全局最小 depth。

    返回 `(rows_out, warns)`：`rows_out` 是加了 `slowdown` / `warn` 的副本，
    `warns` 是超阈值那些行。mbps 缺失或 ≤ 0 的行 slowdown=None、不算 warn
    （量不到和"慢"是两件事）。
    """
    rows = [dict(r) for r in (rows or [])]
    if not rows:
        return [], []
    depths = [r.get("depth") for r in rows if r.get("depth") is not None]
    base_depth = min(depths) if depths else None
    base_by_phase = {}
    for r in rows:
        if r.get("depth") != base_depth:
            continue
        mbps = r.get("mbps")
        if mbps and mbps > 0:
            base_by_phase.setdefault(r.get("phase"), mbps)
    fallback = next(iter(base_by_phase.values()), None)
    warns = []
    for r in rows:
        base = base_by_phase.get(r.get("phase")) or fallback
        mbps = r.get("mbps")
        if not base or not mbps or mbps <= 0:
            r["slowdown"] = None
            r["warn"] = False
            continue
        r["slowdown"] = base / mbps
        r["warn"] = bool(max_slowdown) and r["slowdown"] > max_slowdown
        if r["warn"]:
            warns.append(r)
    return rows, warns


def parse_weights(text, allowed):
    """T36：把 `create=3,restore=3,list=2` 解析成 {场景: 权重}。

    只认 `allowed` 里的场景名，权重必须是 ≥ 0 的数，全 0 或解析不出东西都算错
    （抛 ValueError，由调用方转成 Unmet/参数错）。没提到的场景权重为 0。
    """
    out = {k: 0.0 for k in allowed}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError("权重要写成 `场景=数字`，这一段是 %r" % part)
        k, v = part.split("=", 1)
        k = k.strip()
        if k not in out:
            raise ValueError("不认识的场景 %r（可用：%s）" % (k, ", ".join(sorted(allowed))))
        try:
            w = float(v)
        except ValueError:
            raise ValueError("%s 的权重不是数字：%r" % (k, v))
        if w < 0:
            raise ValueError("%s 的权重是负的：%r" % (k, v))
        out[k] = w
    if sum(out.values()) <= 0:
        raise ValueError("所有场景的权重都是 0，没法跑")
    return out


def pick_weighted(weights, r):
    """按权重挑一个场景。`r` 是 [0,1) 的随机数（传进来而不是自己 random，好单测）。"""
    items = sorted((k, w) for k, w in weights.items() if w > 0)
    total = sum(w for _, w in items)
    x = r * total
    acc = 0.0
    for k, w in items:
        acc += w
        if x < acc:
            return k
    return items[-1][0]


def bucket_ops(ops):
    """T36：把一堆操作记录按场景汇总。

    每个场景给：次数、失败数、p50/p99（只算成功的），以及失败的分桶
    `"<异常类>/<reason>"` → 次数（`error_info` 已经把 reason 退化算好了）。
    另外单列 `mismatch`（restore 后现场对不上）与 `verified`。
    """
    out = {}
    for rec in ops or []:
        scene = rec.get("scene_name") or rec.get("op") or "?"
        b = out.setdefault(scene, {"n": 0, "fail": 0, "walls": [], "errors": {},
                                   "mismatch": 0})
        b["n"] += 1
        if rec.get("ok"):
            w = rec.get("wall_s")
            if w is not None:
                b["walls"].append(w)
            if rec.get("verified") is False:
                b["mismatch"] += 1
        else:
            b["fail"] += 1
            info = rec.get("error_info") or {}
            key = "%s/%s" % (info.get("type") or "?", info.get("reason") or "?")
            b["errors"][key] = b["errors"].get(key, 0) + 1
    for b in out.values():
        walls = b.pop("walls")
        b["p50_s"] = p50(walls)
        b["p99_s"] = quantile(walls, 0.99)
        b["max_s"] = pmax(walls)
    return out


def judge_reconcile(client_ids, server_ids):
    """T36 收尾对账：客户端账本 vs 服务端 list。返回 `(ok, want, got)`。"""
    want_set = set(client_ids or ())
    got_set = set(server_ids or ())
    want = "%d 条" % len(want_set)
    if want_set == got_set:
        return True, want, "%d 条，完全一致" % len(got_set)
    missing = sorted(want_set - got_set)      # 客户端以为有、服务端没有
    ghost = sorted(got_set - want_set)        # 服务端多出来的幽灵
    return False, want, "%d 条；服务端缺 %s；幽灵 %s" % (len(got_set), missing or "无", ghost or "无")


def judge_netns_steady(before, after, slack=0):
    """T36 / T18：netns 槽位不泄漏。只判「不比开跑前多出 slack 以上」——
    920B 上还有别人的沙箱在起落，绝对值没意义（09-16 清过 8729 个泄漏槽位）。"""
    if before is None or after is None:
        return None, "不增长", "读不到 netns 数（前 %s 后 %s）" % (before, after)
    return after <= before + slack, "≤ 开跑前的 %d（容差 %d）" % (before, slack), "跑完 %d" % after


# ---------------------------------------------- 宿主机侧：健康探测与重启（T18）

def parse_log_fields(line, keys=None):
    """从 orchestrator 的一行 zap JSON 日志里挑字段出来（T39 的导出日志）。

    整行 `json.loads` 不一定成 —— nomad 的日志行前面可能挂着别的东西，FC 的串口
    输出也会和它挤在同一份文件里 —— 所以按 `"键": 值` 就地找。值只认字符串、数字
    和 true/false，这几行要读的就这些。

    keys 给了就只挑这几个键（缺的不补），None = 认得出的全给。
    """
    out = {}
    if not line:
        return out
    for m in re.finditer(r'"([A-Za-z0-9_.\-]+)"\s*:\s*'
                         r'("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false)', line):
        k, raw = m.group(1), m.group(2)
        if keys is not None and k not in keys:
            continue
        if raw.startswith('"'):
            v = raw[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        elif raw == "true":
            v = True
        elif raw == "false":
            v = False
        elif "." in raw:
            v = float(raw)
        else:
            v = int(raw)
        out[k] = v
    return out


def judge_pause_resume(before, after, want_marks=None):
    """T39：原生 pause → resume 之后的现场判定。返回 `(ok, bad)`。

    `before` / `after` 是 `t39.state()` 读的那份现场：`mem_md5`（64 MB 自校验内存
    整块的 md5）、`writer_pid` / `writer_state`（常驻进程）、`mem_mark` / `file_mark`
    （内存里与盘上的标记）、`cmd`（命令跑没跑通）。

    `want_marks` 是 resume 之后标记**应该**是什么样（`{"mem_mark": "M0,", ...}`）；
    没给的键沿用 `before` —— 默认口径就是"pause 前什么样，resume 后一模一样"。

    判的是缺陷的外部后果，与实现无关：pause 导出的差分漏了页，resume 回来的内存
    就是两个时刻拼的 —— 轻则 md5 变、标记回到启动态，重则 guest 内核当场 panic
    （那时连命令都跑不通）。**pid 变了**要单独拎出来说：那不是内存回来了，那是
    虚机重启了，整块内存都是新的，md5 反而可能"看着没问题"。
    """
    bad = []
    md5_before, md5_after = before.get("mem_md5"), after.get("mem_md5")
    if not md5_after or md5_after == "MISSING":
        bad.append("resume 之后读不到内存 blob 的 md5（%r）" % (md5_after,))
    elif md5_after != md5_before:
        bad.append("内存 blob 变了：pause 前 %s → resume 后 %s" % (md5_before, md5_after))

    pid_before, pid_after = before.get("writer_pid"), after.get("writer_pid")
    if not pid_after or pid_after in ("MISSING", "GONE"):
        bad.append("常驻进程的 pid 文件没了（%r）" % (pid_after,))
    elif pid_after != pid_before:
        bad.append("常驻进程 pid 变了：%s → %s（虚机重启了，不是内存回来了）"
                   % (pid_before, pid_after))

    st_before, st_after = before.get("writer_state"), after.get("writer_state")
    if not st_after or st_after == "GONE":
        bad.append("常驻进程不在了（state=%r）" % (st_after,))
    elif st_after != st_before:
        bad.append("常驻进程状态变了：%s → %s" % (st_before, st_after))

    if after.get("cmd") != "alive":
        bad.append("resume 之后 guest 命令没跑通（cmd=%r）" % (after.get("cmd"),))

    want = dict(want_marks or {})
    for k in ("mem_mark", "file_mark"):
        exp = want.get(k, before.get(k))
        got = after.get(k)
        if got != exp:
            bad.append("%s：期望 %r，实际 %r" % (k, exp, got))

    return (not bad), bad


def judge_clean_pause_resume(before, after, want_marks=None):
    """T41：从没做过 checkpoint 的沙箱，原生 pause → resume 之后的现场判定。

    先按 `judge_pause_resume` 判共有的那几项（自校验内存整块 md5、常驻写者的
    pid/状态、两份标记、命令跑不跑得通），再加这个用例自己的两项：

      · **盘上那份文件的 md5**（`file_md5`）—— 内存回来了不等于写层回来了，
        `want_marks` 里给了就按给的判，没给就按"pause 前什么样，resume 后一样"；
      · **心跳进程**（`hb_pid` 与 `hb0` / `hb1` 两拍行数）—— 分三句话说，因为它们
        是三件不同的事：pid 没了 = 整机重来了；pid 还在但行数不涨 = vCPU 没真跑
        起来（resume 返回成功也可能是这样）；行数比 pause 前少 = 时间倒流，这条
        路上不该发生（会倒退的是 restore，不是 pause/resume）。

    返回 `(ok, bad)`，`bad` 是人话描述的问题清单，空表示全对。
    """
    bad = judge_pause_resume(before, after, want_marks)[1]
    want = dict(want_marks or {})

    exp = want.get("file_md5", before.get("file_md5"))
    got = after.get("file_md5")
    if not got or got == "MISSING":
        bad.append("resume 之后读不到盘上文件的 md5（%r）" % (got,))
    elif got != exp:
        bad.append("盘上的文件变了：pause 前 %s → resume 后 %s" % (exp, got))

    hb_before, hb_after = before.get("hb_pid"), after.get("hb_pid")
    if not hb_after or hb_after in ("MISSING", "GONE"):
        bad.append("心跳进程的 pid 文件没了（%r）" % (hb_after,))
    elif hb_after != hb_before:
        bad.append("心跳进程 pid 变了：%s → %s（虚机重启了，不是内存回来了）"
                   % (hb_before, hb_after))

    hb0, hb1 = as_int(after.get("hb0")), as_int(after.get("hb1"))
    if hb0 is None or hb1 is None:
        bad.append("心跳行数读不出来（hb0=%r hb1=%r）" % (after.get("hb0"), after.get("hb1")))
    elif hb1 <= hb0:
        bad.append("心跳行数不涨了：%d → %d（进程还在，但没在跑）" % (hb0, hb1))
    else:
        was = as_int(before.get("hb1"))
        if was is not None and hb0 < was:
            bad.append("心跳行数倒退了：pause 前 %d → resume 后 %d" % (was, hb0))

    return (not bad), bad


def live_fc_count():
    """宿主机上还活着的 firecracker 进程总数（`switch-stack.sh` 的换栈前置就是它要为 0）。

    自己数 /proc，不用 `pgrep -f`：远程执行时 pgrep 会把自己那条命令行也匹配进去
    （09-16 踩过），而且这里要的是"全机"而不是"某个沙箱"。
    """
    n = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.path.basename(os.path.realpath("/proc/%s/exe" % pid)) == "firecracker":
                n += 1
        except OSError:
            continue
    return n


def http_ok(url, timeout=2.0):
    """GET 一下，返回 HTTP 状态码；连不上返回 None。"""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:       # noqa: BLE001 —— 连不上/超时都算"还没起来"
        return None


def wait_until(check, timeout, interval=2.0, sleep=time.sleep, clock=time.monotonic):
    """轮询 `check()` 直到它返回真值或等满 `timeout`。

    返回 `(结果, 等了多久, 轮询次数)`；超时就是 `(最后一次的结果, timeout, n)`。
    `sleep` / `clock` 留成参数给单测打桩（同 `wait_for_log`）。
    """
    t0 = clock()
    tries = 0
    last = None
    while True:
        try:
            last = check()
        except Exception as e:      # noqa: BLE001 —— 探测本身出错只当"还没好"
            last = None
            _ = e
        tries += 1
        waited = clock() - t0
        if last:
            return last, waited, tries
        if waited >= timeout:
            return last, waited, tries
        sleep(min(interval, max(0.0, timeout - waited)))


def run_shell(cmd, timeout=900):
    """跑一条 shell 命令（重启栈那一条）。返回 `{"cmd","rc","out","wall_s"}`。

    stdout/stderr 合并，只留末尾 4000 字符进 JSON —— 换栈脚本会打一整段 status。
    """
    rec = {"cmd": cmd}
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        rec["rc"] = p.returncode
        out = (p.stdout + p.stderr).decode("utf8", "replace")
    except subprocess.TimeoutExpired as e:
        rec["rc"] = -1
        out = "（命令超时 %s s）%s" % (timeout, e)
    except OSError as e:
        rec["rc"] = -1
        out = "（起不来）%s" % e
    rec["wall_s"] = time.monotonic() - t0
    rec["out"] = out[-4000:]
    return rec


# `switch-stack.sh` 在还有活 FC 时的原话（它的换栈前置）。T18 的 `--pre-restart auto`
# 靠它认出"得先把沙箱清掉"，而不是把它当成一次失败的重启。
RESTART_NEEDS_IDLE_RE = re.compile(r"还有沙箱在跑|先清掉|live firecracker")


# ---------------------------------------------------------------- guest 里跑的小程序（续）

# 冷读器（T34）：量"被链上多层覆盖的 rootfs 块"读起来有多慢。
#   argv: <文件> <顺序读多少 MB> <随机读几次> <随机读块大小字节> <随机种子>
# 两遍，每遍前都 `echo 3 > /proc/sys/vm/drop_caches`（guest 里是 root）：
#   1. 顺序整读 → 吞吐 MB/s（这是"读链深度"最直接的秤）；
#   2. 随机偏移的小块读，每次单独计时 → p50/p99 延迟（一次 drop_caches 之后
#      这些偏移都还是冷的，读到的就是穿过整条读链的那一发）。
# 不用 O_DIRECT：guest 内核对 NBD/virtio 支持不一定齐，drop_caches 已经够冷。
GUEST_COLD_READ = r'''
import os, sys, time, random

path, mb, nrand, blk, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
size = os.path.getsize(path)


def drop():
    os.system('sync')
    try:
        open('/proc/sys/vm/drop_caches', 'w').write('3\n')
        return 1
    except OSError:
        return 0


dropped = drop()
want = min(mb * 1048576, size)
buf = bytearray(1048576)
t0 = time.monotonic()
got = 0
with open(path, 'rb', buffering=0) as f:
    while got < want:
        n = f.readinto(buf)
        if not n:
            break
        got += n
seq_s = time.monotonic() - t0

dropped += drop()
rnd = random.Random(seed)
lat = []
with open(path, 'rb', buffering=0) as f:
    for _ in range(nrand):
        off = rnd.randrange(0, max(1, size - blk))
        t = time.monotonic()
        f.seek(off)
        f.read(blk)
        lat.append((time.monotonic() - t) * 1000.0)
lat.sort()


def q(p):
    if not lat:
        return -1.0
    return lat[min(len(lat) - 1, int(round(p * (len(lat) - 1))))]


print('size_mb=%.1f' % (size / 1048576.0))
print('read_mb=%.1f' % (got / 1048576.0))
print('seq_s=%.4f' % seq_s)
print('mbps=%.2f' % ((got / 1048576.0) / seq_s if seq_s > 0 else -1))
print('rand_n=%d' % len(lat))
print('p50_ms=%.3f' % q(0.5))
print('p99_ms=%.3f' % q(0.99))
print('max_ms=%.3f' % (lat[-1] if lat else -1))
print('dropped=%d' % dropped)
'''
