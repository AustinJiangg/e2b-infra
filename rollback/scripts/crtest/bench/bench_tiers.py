#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按改动量分档的 checkpoint / restore 基准。

原本是 920B 上 tmp/bench-tiers-20260921 的一次性脚本（deltabox-dev @ 4af2872c6），
2026-09-22 参数化后迁进本仓库：档位表加了 --tier-set（full / short），输出目录改成
--outdir（默认当前目录），crtest 包按本文件位置定位，不再有任何绝对路径。

沿用 tmp/bench-tiers-20260920/bench_tiers.py 的口径，改动只有三处：
  1. 档位换成"混合主表（内存:文件 = 3:1）+ 纯内存 / 纯文件 / 只读附表"；
  2. 每档的样本数各不相同（小档密、大档稀），由档位表自带，均摊到 3 遍；
  3. 输出目录换到 tmp/bench-tiers-20260921。

一次迭代 = 处在基线 checkpoint cpA 的状态 → 在 guest 里造规定量的改动 →
增量 checkpoint 出 cpB（记分段/墙钟/产物字节）→ restore 回 cpA（= 撤销这份改动，
记分段/墙钟）→ 校验现场 → 删掉 cpB 回收空间。

用词：本文的 checkpoint = 我们这套沙箱级快照的"拍快照"。原始数据里的 op 名
`create` 是接口名（checkpoint.Checkpoint/Create），保留不改。
"""

import argparse
import json
import math
import os
import sys
import time

# crtest 包在本文件的上一级（rollback/scripts/crtest/crtest），按位置定位。
CRTEST_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CRTEST_PARENT not in sys.path:
    sys.path.insert(0, CRTEST_PARENT)

from crtest import common                                   # noqa: E402
from crtest.common import log, Box, sandbox_create           # noqa: E402

OUTDIR = os.environ.get("CRTEST_OUTDIR") or os.getcwd()   # --outdir 覆盖

SWEEP = "/dev/shm/sweep"              # 内存那份（tmpfs，页永远驻留）
RND = "/dev/shm/rnd32"                # 32 MB 随机源，用来快速造非零脏页
BENCH_DIR = common.BENCH_DIR          # /bench-root
FSBLOB = BENCH_DIR + "/fsblob"        # 文件那份（根文件系统 → 写层）
MEM_GEN = "/dev/shm/gen"              # 代号，只写内存侧（1 页）

WARM_MEM_MB = 512                     # 覆盖最大内存档（mix512 的 384 MB）
WARM_FILE_MB = 192                    # 覆盖最大文件档（mix512 的 128 MB）
RND_MB = 32

# 档位：(名字, 内存写脏 MB, 文件顺序写 MB, 只读触碰 MB, 说明, 总样本数)
TIERS = [
    # —— 混合主表：总改动量按 内存:文件 = 3:1 拆 ——
    ("mix0",    0,   0,   0,   "混合 0 MB（空闲基底）",        100),
    ("mix4",    3,   1,   0,   "混合 4 MB（3 内存 + 1 文件）",  100),
    ("mix8",    6,   2,   0,   "混合 8 MB（6 + 2）",            100),
    ("mix16",   12,  4,   0,   "混合 16 MB（12 + 4）",          100),
    ("mix32",   24,  8,   0,   "混合 32 MB（24 + 8）",          100),
    ("mix64",   48,  16,  0,   "混合 64 MB（48 + 16）",         100),
    ("mix128",  96,  32,  0,   "混合 128 MB（96 + 32）",         60),
    ("mix192",  144, 48,  0,   "混合 192 MB（144 + 48）",        60),
    ("mix256",  192, 64,  0,   "混合 256 MB（192 + 64）",        60),
    ("mix512",  384, 128, 0,   "混合 512 MB（384 + 128）极限档",  30),
    # —— 纯内存档（求斜率）——
    ("mem4",    4,   0,   0,   "纯内存 4 MB",                    30),
    ("mem16",   16,  0,   0,   "纯内存 16 MB",                   30),
    ("mem48",   48,  0,   0,   "纯内存 48 MB",                   30),
    ("mem128",  128, 0,   0,   "纯内存 128 MB",                  30),
    # —— 纯文件档 ——
    ("file4",   0,   4,   0,   "纯文件 4 MB + sync",             30),
    ("file16",  0,   16,  0,   "纯文件 16 MB + sync",            30),
    ("file64",  0,   64,  0,   "纯文件 64 MB + sync",            30),
    # —— 只读触碰 ——
    ("read192", 0,   0,   192, "只读触碰 192 MB",                20),
]
TIER_BY_NAME = {t[0]: t for t in TIERS}

# 短表：给「部署完就测」的 perf 档用。保留小档细分（回答"改动很小的时候够不够快"）
# 加一个 512 MB 极限档（回答"最坏情况有多慢"），样本数 30 / 20 / 10：小档 30、
# 中档 20、大档与只读 10。全表 1043 次实测 9 分 36 秒，短表约其五分之一。
SHORT_COUNTS = {"mix0": 30, "mix4": 30, "mix8": 30, "mix16": 30, "mix32": 30,
                "mix64": 20, "mix128": 20, "mix256": 20, "mix512": 10,
                "mem16": 20, "file16": 20, "read192": 10}
SHORT_TIERS = [(t[0], t[1], t[2], t[3], t[4], SHORT_COUNTS[t[0]])
               for t in TIERS if t[0] in SHORT_COUNTS]
TIER_SETS = {"full": TIERS, "short": SHORT_TIERS}


# ------------------------------------------------------------------ 产物大小

def alloc(path):
    """磁盘实占字节（稀疏文件按 st_blocks 算）。"""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return None


def layers_of(store, sbx_id):
    try:
        return set(os.listdir(os.path.join(store, sbx_id, "layers")))
    except (OSError, TypeError):
        return set()


def measure(store, sbx_id, ck_id, before_layers):
    """这一代实际写下了多少字节：内存差分（或全量镜像）+ 新封的盘层。"""
    out = {"mem_bytes": None, "mem_kind": "-", "disk_bytes": None, "files": {}}
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
    disk = 0
    new_layers = layers_of(store, sbx_id) - before_layers
    for name in new_layers:
        disk += alloc(os.path.join(store, sbx_id, "layers", name)) or 0
    out["disk_bytes"] = disk
    out["new_layers"] = sorted(new_layers)
    return out


# ------------------------------------------------------------------ guest 侧

def warm(box):
    """预热：把 sweep / fsblob 一次撑到最大档，随机源也备好。"""
    t0 = time.monotonic()
    box.run("mkdir -p %s" % BENCH_DIR)
    box.run("dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null" % (RND, RND_MB),
            timeout=1800)
    box.run("dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null" % (SWEEP, WARM_MEM_MB),
            timeout=1800)
    box.run("dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null; sync"
            % (FSBLOB, WARM_FILE_MB), timeout=1800)
    box.run("echo base > %s; sync" % MEM_GEN)
    rc, out = box.sh("ls -l %s %s %s; free -m" % (RND, SWEEP, FSBLOB))
    log("  预热完成 %.1f s\n%s" % (time.monotonic() - t0, out.strip()))
    return time.monotonic() - t0


def _copy_chunk(dst, mb):
    """从 32 MB 随机源反复拷到 dst 的前 mb MB（原地覆写，不改文件大小）。"""
    cmds, off = [], 0
    while off < mb:
        c = min(RND_MB, mb - off)
        cmds.append("dd if=%s of=%s bs=1M seek=%d count=%d conv=notrunc 2>/dev/null"
                    % (RND, dst, off, c))
        off += c
    return "\n".join(cmds)


def make_dirty(box, gen, mem_mb, file_mb, read_mb):
    """造改动。返回 guest 侧耗时（秒）。"""
    parts = []
    if mem_mb > 0:
        parts.append(_copy_chunk(SWEEP, mem_mb))
    if file_mb > 0:
        parts.append(_copy_chunk(FSBLOB, file_mb))
    if read_mb > 0:
        parts.append("dd if=%s of=/dev/null bs=1M count=%d 2>/dev/null" % (SWEEP, read_mb))
    parts.append("echo %s > %s" % (gen, MEM_GEN))
    parts.append("sync")
    t0 = time.monotonic()
    rc, out = box.sh("\n".join(parts), timeout=1800)
    dt = time.monotonic() - t0
    if rc != 0:
        raise common.Failed("造改动", "退出码 0", "退出码 %s：%s" % (rc, out[-400:]),
                            "guest 侧 dd")
    return dt


# ------------------------------------------------------------------ 主体

def one_iter(ctx, box, base_id, tier, idx, gen, jsonl):
    name, mem_mb, file_mb, read_mb, label = tier[:5]
    store = ctx.store
    rec = {"kind": "iter", "pass": ctx._pass, "tier": name, "idx": idx,
           "mem_mb": mem_mb, "file_mb": file_mb, "read_mb": read_mb,
           "total_mb": mem_mb + file_mb,
           "sandbox": box.id, "base": base_id, "t": time.time()}

    rec["dirty_s"] = make_dirty(box, gen, mem_mb, file_mb, read_mb)

    before_layers = layers_of(store, box.id)
    m0 = ctx.meter.snapshot()
    c = box.create(gen)
    m1 = ctx.meter.snapshot()
    rec["create"] = c
    if not c.get("ok"):
        rec["fatal"] = "checkpoint 失败"
        jsonl(rec)
        raise common.Failed("checkpoint", "成功", str(c.get("error")),
                            "档 %s 第 %d 次" % (name, idx))
    rec["create_host"] = common.HostMeter.delta(m0, m1)
    rec["artifact"] = measure(store, box.id, c["id"], before_layers)

    r = box.restore(base_id)
    rec["restore"] = r
    if not r.get("ok"):
        rec["fatal"] = "restore 失败"
        jsonl(rec)
        raise common.Failed("restore", "成功", str(r.get("error")),
                            "档 %s 第 %d 次" % (name, idx))
    if r.get("verified") is False:
        rec["verify_bad"] = r.get("mismatch")
        log("  !! 档 %s 第 %d 次：restore 后现场不一致 %s" % (name, idx, r.get("mismatch")))

    rec["delete"] = box.delete(c["id"])
    jsonl(rec)
    return rec


def _mb(n):
    return "-" if n is None else "%.2f" % (n / 1048576.0)


def plan_counts(n_total, passes, scale):
    """把总样本数摊到各遍（余数给前几遍）。"""
    n = max(1, int(round(n_total * scale)))
    base, rem = divmod(n, passes)
    return [base + (1 if i < rem else 0) for i in range(passes)]


def run_pass(ctx, pi, jsonl):
    ctx._pass = pi
    log("")
    log("== 第 %d 遍：新建沙箱 ==" % pi)
    sbx = sandbox_create(ctx.args.template, private=not ctx.args.public, timeout=7200)
    box = Box(sbx, ctx.store, "p%d" % pi)
    common._ALL_BOXES.append(box)
    log("  沙箱 %s" % box.id)
    warm_s = warm(box)

    before_layers = layers_of(ctx.store, box.id)
    m0 = ctx.meter.snapshot()
    base = box.create("base")
    m1 = ctx.meter.snapshot()
    if not base.get("ok"):
        raise common.Failed("首个全量 checkpoint", "成功", str(base.get("error")),
                            "第 %d 遍" % pi)
    art = measure(ctx.store, box.id, base["id"], before_layers)
    jsonl({"kind": "full", "pass": pi, "sandbox": box.id, "warm_s": warm_s,
           "create": base, "artifact": art, "create_host": common.HostMeter.delta(m0, m1),
           "t": time.time()})
    log("  首个全量 checkpoint %s：mem_mode=%s frozen %s ms 墙钟 %.2f s 产物 %s %s MB"
        % (base["id"], base.get("mem_mode"),
           common.fmt_ms((base.get("phases") or {}).get("frozen")),
           base.get("wall_s", 0.0), art["mem_kind"], _mb(art["mem_bytes"])))

    for tier in ctx._tiers:
        iters = ctx._plan[tier[0]][pi - 1]
        if iters <= 0:
            continue
        log("  -- 档 %s（%s）×%d --" % (tier[0], tier[4], iters))
        t_tier = time.monotonic()
        for i in range(1, iters + 1):
            if time.monotonic() > ctx._deadline:
                log("  !! 到总时限，停在 %s #%d" % (tier[0], i))
                box.kill()
                common.forget(box)
                return False
            rec = one_iter(ctx, box, base["id"], tier, i, "%s-%s-%d" % (tier[0], pi, i), jsonl)
            if i % 20 == 0 or i == iters:
                ph = (rec["create"].get("phases") or {})
                log("     %s #%d/%d  ck frozen %s 墙钟 %.3fs | rs 墙钟 %.3fs | %.1f s 用时"
                    % (tier[0], i, iters, common.fmt_ms(ph.get("frozen")),
                       rec["create"].get("wall_s", 0.0), rec["restore"].get("wall_s", 0.0),
                       time.monotonic() - t_tier))
    box.kill()
    common.forget(box)
    log("  第 %d 遍结束，沙箱已 kill" % pi)
    return True


def run(ctx):
    jsonl_path = os.path.join(OUTDIR, "raw-%s.jsonl" % common.now_tag())
    fh = open(jsonl_path, "a", buffering=1)

    def jsonl(rec):
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        ctx.results["ops"].append(rec)

    ctx._deadline = time.monotonic() + ctx.args.deadline_min * 60.0
    base_tiers = TIER_SETS[ctx.args.tier_set]
    by_name = {t[0]: t for t in base_tiers}
    ctx._tiers = [by_name[n] for n in ctx.args.tiers.split(",")] \
        if ctx.args.tiers else base_tiers
    ctx._plan = {t[0]: plan_counts(t[5] if ctx.args.iters is None else ctx.args.iters * ctx.args.passes,
                                   ctx.args.passes, ctx.args.scale)
                 for t in ctx._tiers}
    log("原始数据：%s" % jsonl_path)
    log("计划：%s" % ", ".join("%s=%s" % (k, sum(v)) for k, v in ctx._plan.items()))
    log("前置：netns 稳定计数 = %s，活 FC = %s，orchestrator 已跑 %.0f 分钟"
        % (common.netns_count_steady(), common.live_fc_count(),
           (common.orchestrator_uptime_s() or 0) / 60.0))
    ctx.results["meta"]["jsonl"] = jsonl_path
    ctx.results["meta"]["plan"] = {k: v for k, v in ctx._plan.items()}
    ok = True
    for pi in range(1, ctx.args.passes + 1):
        if not run_pass(ctx, pi, jsonl):
            ok = False
            break
    fh.close()
    common.wait_until(lambda: common.live_fc_count() == 0, timeout=120, interval=3.0)
    log("")
    log("收尾：活 FC = %s，netns = %s" % (common.live_fc_count(), common.netns_count()))
    if not ok:
        log("!! 因为到总时限提前结束，数据不完整")


def main():
    ap = argparse.ArgumentParser(description="按改动量分档的 checkpoint 基准")
    common.add_common_args(ap)
    ap.add_argument("--tier-set", choices=sorted(TIER_SETS), default="full",
                    help="档位表：full=十八档全表（约 10 分钟），"
                         "short=小档细分 + 512 MB 极限档（约 2~3 分钟）")
    ap.add_argument("--outdir", default=None,
                    help="原始数据与默认 JSON 的落点（默认 $CRTEST_OUTDIR，再默认当前目录）")
    ap.add_argument("--tiers", default=None, help="逗号分隔的档位名，默认整张表：%s"
                    % ",".join(t[0] for t in TIERS))
    ap.add_argument("--iters", type=int, default=None,
                    help="覆盖档位表里的样本数（每档每遍的迭代次数）")
    ap.add_argument("--scale", type=float, default=1.0, help="按比例缩放档位表的样本数")
    ap.add_argument("--passes", type=int, default=3, help="整套跑几遍（每遍一个新沙箱）")
    ap.add_argument("--deadline-min", type=float, default=75.0, help="总时限（分钟）")
    args = ap.parse_args()
    global OUTDIR
    if args.outdir:
        OUTDIR = args.outdir
    os.makedirs(OUTDIR, exist_ok=True)
    if not args.out:
        args.out = os.path.join(OUTDIR, "bench-tiers-%s.json" % common.now_tag())

    shim = type("M", (), {"run": staticmethod(run)})
    return common.run_case("BENCH-TIERS", shim, args)


if __name__ == "__main__":
    sys.exit(main())
