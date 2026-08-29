#!/usr/bin/env python3
"""checkpoint / restore 耗时基准，两套通用。

和 timing.py 的分工：timing.py 回答"对不对、是不是 O(脏页)"，每档一个样本；
这个脚本回答"到底多久"，给的是分布，能拿去对性能指标、能给客户看。

三条主线：

  A. 全量快照基线 —— 每个沙箱的第一次 checkpoint 必然是全量（没有基线可增量）。
     开 N 个新沙箱各取一次，得到全量的耗时与体积。**没有这条，增量的数字没有意义。**

  B. 增量扫描 —— 脏页 0/16/32/64/128/256MB 各一档。
     每档**开一个新沙箱**，链深从 1 长到 reps，各档完全一致 ——
     这样"链深"这个变量在档与档之间是配平的，档之间才可比。
     前几次预热丢弃。

  C. 冻结窗口 —— 客户端墙钟包含 RPC 和宿主侧工作；虚机真正停住的时间要小一些，
     而后者才是业务负载感受到的停顿。用 guest 内的采样器测（见 freeze_probe.py）。
     每档先测一遍"只弄脏、不 checkpoint"的抖动底噪，作为这个测法的分辨率下限。

外加：restore 的深浅集耗时、以及 60 秒持续 checkpoint 的速率与劣化。

用法:
  bench-ckpt.py <xfs|ext4> [选项]
    --tiers 0,16,32,64,128,256   脏页档位(MB)
    --reps 30                    每档计入统计的次数
    --warmup 3                   每档丢弃的预热次数
    --full-samples 10            全量基线的样本数(每个样本一个新沙箱)
    --freeze-reps 8              每档的冻结窗口样本数；0 = 跳过 C
    --sustain-sec 60             持续速率测试时长；0 = 跳过
    --sustain-mb 32              持续速率测试的脏页量
    --out DIR                    输出目录，默认 ./reports/bench-<方案>-<时间戳>
"""
import json
import os
import statistics
import subprocess
import sys
import time

import lib

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 参数

def parse_args(argv):
    o = {
        "scheme": "ext4", "tiers": [0, 16, 32, 64, 128, 256], "reps": 30,
        "warmup": 3, "full_samples": 10, "freeze_reps": 8,
        "sustain_sec": 60, "sustain_mb": 32, "out": None,
    }
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--tiers":
            o["tiers"] = [int(x) for x in argv[i + 1].split(",")]; i += 2
        elif a in ("--reps", "--warmup", "--full-samples", "--freeze-reps",
                   "--sustain-sec", "--sustain-mb"):
            o[a[2:].replace("-", "_")] = int(argv[i + 1]); i += 2
        elif a == "--out":
            o["out"] = argv[i + 1]; i += 2
        elif a.startswith("--"):
            sys.exit("未知参数 %s\n%s" % (a, __doc__))
        else:
            rest.append(a); i += 1
    if rest:
        o["scheme"] = rest[0]
    if o["scheme"] not in ("xfs", "ext4"):
        sys.exit("方案只能是 xfs 或 ext4，收到 %r\n%s" % (o["scheme"], __doc__))
    return o


OPT = parse_args(sys.argv[1:])
SCHEME = OPT["scheme"]
STAMP = time.strftime("%Y%m%d-%H%M%S")
OUT = OPT["out"] or os.path.join(HERE, "reports", "bench-%s-%s" % (SCHEME, STAMP))
os.makedirs(OUT, exist_ok=True)


def log(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- 统计

def pct(xs, q):
    """小样本上 statistics.quantiles 的插值会造出没观测到的值，这里取实际观测值。"""
    if not xs:
        return None
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def summarize(xs):
    if not xs:
        return {}
    return {
        "n": len(xs),
        "p50": round(pct(xs, 0.50), 4),
        "p90": round(pct(xs, 0.90), 4),
        "p99": round(pct(xs, 0.99), 4),
        "max": round(max(xs), 4),
        "min": round(min(xs), 4),
        "mean": round(statistics.fmean(xs), 4),
    }


# ---------------------------------------------------------------- 落盘量

def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def phase_summary(samples):
    """每个阶段取中位数。阶段不是每次都齐（比如 XFS 套没有 materialize），
    所以按阶段各自统计各自出现过的次数。"""
    names = set()
    for s in samples:
        names |= set(s)
    out = {}
    for n in sorted(names):
        xs = [s[n] for s in samples if n in s]
        if xs:
            out[n] = {"p50": round(pct(xs, 0.5), 3), "p90": round(pct(xs, 0.9), 3), "n": len(xs)}
    return out


def du_bytes(path, apparent=False):
    flag = "--apparent-size " if apparent else ""
    out = lib.sh("du -sB1 %s%s 2>/dev/null | cut -f1" % (flag, path))
    return int(out) if out.isdigit() else -1


def df_used_bytes(path):
    out = lib.sh("df -B1 --output=used %s 2>/dev/null | tail -1" % path)
    return int(out) if out.isdigit() else -1


STORE_MOUNT = lib.sh("findmnt -no TARGET --target %s" % lib.STORE) or "/"


# ---------------------------------------------------------------- 被测沙箱

class Bench:
    """比 lib.Box 轻：不算 md5、不写磁盘 marker，免得把被测量的东西掺进耗时里。"""

    def __init__(self, tier_mb=0, template="base", timeout=1800):
        self.sbx = lib.Sandbox.create(template=template, timeout=timeout)
        self.id = self.sbx.sandbox_id
        self.backend = lib.dirty_tracking(self.id)
        self.tier_mb = tier_mb
        self.cks = []
        self.last_restore_phases = {}
        if tier_mb:
            # 先备一份随机源放在 guest 内存里，之后每次弄脏都是内存到内存的拷贝，
            # 不用每次现生成随机数 —— 生成时间不该混进来。
            self.sbx.commands.run(
                "dd if=/dev/urandom of=/dev/shm/src bs=1M count=%d 2>/dev/null" % tier_mb,
                timeout=600)

    def dirty(self, mb=None):
        mb = self.tier_mb if mb is None else mb
        if mb:
            self.sbx.commands.run(
                "dd if=/dev/shm/src of=/dev/shm/blob bs=1M count=%d conv=notrunc 2>/dev/null" % mb,
                timeout=300)

    def mark(self, tag):
        self.sbx.commands.run("echo %s > /dev/shm/mk" % tag, timeout=30)

    def read_mark(self):
        return self.sh("cat /dev/shm/mk").strip()

    def sh(self, cmd, timeout=60):
        """guest 内跑一条命令。非零退出在这里不是异常 —— 探针文件还没生成、
        marker 还不存在都是正常轮询状态，SDK 却会直接抛。"""
        try:
            return self.sbx.commands.run("%s 2>/dev/null || true" % cmd, timeout=timeout).stdout
        except Exception:
            return ""

    def entry_dir(self, ck_id):
        return os.path.join(lib.STORE, self.id, ck_id)

    def manifest(self, ck_id):
        try:
            return json.load(open(os.path.join(self.entry_dir(ck_id), "manifest.json")))
        except (OSError, ValueError):
            return {}

    def checkpoint(self, name, measure_bytes=True):
        u0 = df_used_bytes(STORE_MOUNT) if measure_bytes else 0
        t0 = time.monotonic()
        ck = self.sbx.checkpoint.create(name=name)
        dt = time.monotonic() - t0
        u1 = df_used_bytes(STORE_MOUNT) if measure_bytes else 0
        self.cks.append(ck.checkpoint_id)
        rec = {
            "name": name, "id": ck.checkpoint_id, "time": round(dt, 4),
            "mem_mode": self.manifest(ck.checkpoint_id).get("mem_mode"),
            # 第三个口径：时间花在宿主内部的哪一段。orchestrator 每次 checkpoint
            # 都会在产物旁边落一份，读它比从日志里刮出来靠谱。
            "phases": read_json(os.path.join(self.entry_dir(ck.checkpoint_id), "timings.json")),
        }
        if measure_bytes:
            d = self.entry_dir(ck.checkpoint_id)
            rec["logical_b"] = du_bytes(d, apparent=True)
            rec["du_b"] = du_bytes(d)
            rec["df_delta_b"] = u1 - u0
        return rec

    def restore(self, ck_id):
        t0 = time.monotonic()
        ok = self.sbx.checkpoint.restore(ck_id)
        dt = round(time.monotonic() - t0, 4)
        # 每次 restore 覆盖同一个文件，所以要当场读走
        self.last_restore_phases = read_json(
            os.path.join(lib.STORE, self.id, "last-restore-timings.json"))
        return dt, bool(ok)

    def kill(self):
        try:
            self.sbx.kill()
        except Exception:
            pass


def mb(b):
    return None if b is None or b < 0 else round(b / 1048576.0, 1)


# ---------------------------------------------------------------- A. 全量基线

def run_full_baseline(n):
    log("\n########## A. 全量快照基线（每个样本一个全新沙箱）##########")
    rows = []
    for i in range(n):
        b = Bench(tier_mb=0)
        try:
            r = b.checkpoint("full%d" % i)
            if r["mem_mode"] not in (None, "full"):
                log("  ⚠ 第 %d 个样本的 mem_mode=%s，本该是 full" % (i, r["mem_mode"]))
            rows.append(r)
            log("  %2d/%d  %6.3fs  逻辑 %s MB  新增物理 %s MB  mode=%s"
                % (i + 1, n, r["time"], mb(r["logical_b"]), mb(r["df_delta_b"]), r["mem_mode"]))
        finally:
            b.kill()
    return {
        "samples": rows,
        "time": summarize([r["time"] for r in rows]),
        "logical_mb": summarize([mb(r["logical_b"]) for r in rows if r["logical_b"] > 0]),
        "du_mb": summarize([mb(r["du_b"]) for r in rows if r.get("du_b", 0) > 0]),
        "df_delta_mb": summarize([mb(r["df_delta_b"]) for r in rows if r["df_delta_b"] >= 0]),
    }


# ---------------------------------------------------------------- B/D. 增量扫描 + restore

def run_tier(tier, reps, warmup):
    log("\n===== 档位 脏页 %d MB =====" % tier)
    b = Bench(tier_mb=tier)
    log("  sandbox %s  backend=%s" % (b.id, b.backend))
    out = {"tier_mb": tier, "sandbox": b.id, "backend": b.backend}
    try:
        recs = []

        def one(i, warm):
            b.dirty()
            b.mark("g%d" % i)
            r = b.checkpoint("g%d" % i)
            r["idx"] = i
            r["warmup"] = warm
            recs.append(r)
            return r

        # 预热。第一次没有基线可增量，必然是全量 2GB —— 必须落在 df 基线之前，
        # 否则那 2GB 会被摊进这一档的"每次新增物理空间"里。
        for i in range(warmup):
            one(i, True)
        u_start = df_used_bytes(STORE_MOUNT)
        for i in range(warmup, warmup + reps):
            one(i, False)
            if (i + 1) % 10 == 0 or i + 1 == warmup + reps:
                done = [x["time"] for x in recs if not x["warmup"]]
                log("    %2d/%d  p50=%.4fs" % (i + 1, warmup + reps,
                                               pct(done, 0.5) if done else float("nan")))
        u_end = df_used_bytes(STORE_MOUNT)

        m = [r for r in recs if not r["warmup"]]
        out["creates"] = recs
        out["create_time"] = summarize([r["time"] for r in m])
        out["logical_mb"] = summarize([mb(r["logical_b"]) for r in m if r["logical_b"] > 0])
        out["du_mb"] = summarize([mb(r["du_b"]) for r in m if r.get("du_b", 0) > 0])
        out["df_delta_mb"] = summarize([mb(r["df_delta_b"]) for r in m if r["df_delta_b"] >= 0])
        out["df_total_mb"] = mb(u_end - u_start)
        out["df_per_ckpt_mb"] = round((u_end - u_start) / max(1, len(m)) / 1048576.0, 2)
        out["mem_modes"] = sorted({r["mem_mode"] for r in m})
        out["create_phases"] = phase_summary([r["phases"] for r in m if r.get("phases")])
        # 前 1/3 对后 1/3：链在变深、盘在变满，看看有没有系统性漂移
        third = max(1, len(m) // 3)
        out["drift"] = {
            "first_third_p50": round(pct([r["time"] for r in m[:third]], 0.5), 4),
            "last_third_p50": round(pct([r["time"] for r in m[-third:]], 0.5), 4),
        }

        # ---- restore：浅集(回退一步)与深集(回到链根) ----
        log("  -- restore --")
        ids = [r["id"] for r in recs]
        names = [r["name"] for r in recs]
        tip, prev, root = ids[-1], ids[-2], ids[0]
        shallow, deep, bad = [], [], 0
        rphases = []
        for _ in range(10):
            dt, ok = b.restore(prev)
            bad += (not ok) or b.read_mark() != names[-2]
            shallow.append(dt)
            rphases.append(b.last_restore_phases)
            dt, ok = b.restore(tip)
            bad += (not ok) or b.read_mark() != names[-1]
            shallow.append(dt)
            rphases.append(b.last_restore_phases)
        for _ in range(2):
            dt, ok = b.restore(root)
            bad += (not ok) or b.read_mark() != names[0]
            deep.append(dt)
            dt, ok = b.restore(tip)
            bad += (not ok) or b.read_mark() != names[-1]
            deep.append(dt)
        out["restore_shallow"] = summarize(shallow)
        out["restore_deep_depth"] = len(recs)
        out["restore_deep"] = summarize(deep)
        out["restore_mismatches"] = bad
        out["restore_phases"] = phase_summary([p for p in rphases if p])
        log("    浅集(1 步) p50=%.4fs   深集(%d 步) p50=%.4fs   内容不符 %d 次"
            % (out["restore_shallow"]["p50"], len(recs), out["restore_deep"]["p50"], bad))
    finally:
        b.kill()
    return out


# ---------------------------------------------------------------- C. 冻结窗口

def read_probe(b, path="/tmp/freeze.out", tries=90):
    for _ in range(tries):
        txt = b.sh("cat %s" % path)
        if "DONE" in txt:
            d = {}
            for line in txt.splitlines():
                if line.startswith("TOP_MS "):
                    d["top"] = [float(x) for x in line.split()[1:]]
                elif " " in line:
                    k, v = line.split(None, 1)
                    d[k.lower()] = v.strip()
            return d
        time.sleep(1)
    return {}


def run_freeze(tier, reps):
    """一次探针窗口里连做 reps 次 checkpoint，取最大的 reps 个间隔。

    探针只启动一次 —— python 解释器启动本身要弄脏十几 MB，放进窗口里会污染
    低档位的脏页量。同一个窗口先跑一遍"只弄脏不 checkpoint"，得到底噪。
    """
    b = Bench(tier_mb=tier)
    src = open(os.path.join(HERE, "freeze_probe.py")).read()
    b.sbx.files.write("/tmp/freeze_probe.py", src)
    out = {"tier_mb": tier, "sandbox": b.id, "backend": b.backend}
    try:
        # 需要一个基线 checkpoint，否则第一次是全量。顺便量一下单轮要多久，
        # 好把探针窗口开得刚好够 —— 开太长就是白等，开太短会截断。
        t0 = time.monotonic(); b.dirty(); t_dirty = time.monotonic() - t0
        t0 = time.monotonic(); b.checkpoint("seed", measure_bytes=False)
        t_round = t_dirty + (time.monotonic() - t0)

        # --- 底噪：同样的弄脏节奏，不做 checkpoint ---
        base_dur = round(max(6.0, reps * max(t_dirty, 0.05) * 1.5 + 3), 1)
        b.sbx.commands.run(
            "rm -f /tmp/freeze.out; nohup python3 /tmp/freeze_probe.py %s 30 /tmp/freeze.out "
            ">/dev/null 2>&1 &" % base_dur, timeout=30)
        time.sleep(1.0)
        t_end = time.monotonic() + base_dur - 2
        while time.monotonic() < t_end:
            b.dirty()
        noise = read_probe(b)
        out["noise_top_ms"] = noise.get("top", [])[:5]
        out["noise_median_ms"] = noise.get("median_ms")

        # --- 测量：同样的节奏，每轮多一次 checkpoint ---
        dur = round(max(10.0, reps * t_round * 1.8 + 5), 1)
        b.sbx.commands.run(
            "rm -f /tmp/freeze.out; nohup python3 /tmp/freeze_probe.py %s 40 /tmp/freeze.out "
            ">/dev/null 2>&1 &" % dur, timeout=30)
        time.sleep(1.0)
        wall = []
        for i in range(reps):
            b.dirty()
            r = b.checkpoint("f%d" % i, measure_bytes=False)
            wall.append(r["time"])
        got = read_probe(b)
        top = got.get("top", [])
        out["probe_median_ms"] = got.get("median_ms")
        out["probe_samples"] = got.get("samples")
        out["freeze_ms"] = top[:reps]
        out["next_gaps_ms"] = top[reps:reps + 5]
        out["freeze"] = summarize(top[:reps])
        out["wall"] = summarize(wall)
        floor = max(out["noise_top_ms"]) if out["noise_top_ms"] else 0
        out["noise_floor_ms"] = floor
        out["separated"] = bool(top and min(top[:reps]) > 2 * floor)
        log("  脏页%4dMB  冻结 p50=%.1fms max=%.1fms   墙钟 p50=%.1fms   底噪 %.1fms  %s"
            % (tier, out["freeze"].get("p50", float("nan")), out["freeze"].get("max", float("nan")),
               out["wall"]["p50"] * 1000, floor,
               "可区分 ✓" if out["separated"] else "⚠ 与底噪不可区分"))
    finally:
        b.kill()
    return out


# ---------------------------------------------------------------- E. 持续速率

def run_sustain(seconds, tier):
    log("\n########## E. 持续 checkpoint %d 秒（脏页 %d MB）##########" % (seconds, tier))
    b = Bench(tier_mb=tier)
    out = {"seconds": seconds, "tier_mb": tier, "sandbox": b.id}
    try:
        # 先垫一次。第一次必然是全量，2GB 的耗时和体积混进来会把整段统计带偏。
        b.dirty()
        seed = b.checkpoint("seed")
        out["seed_full"] = {"time": seed["time"], "logical_mb": mb(seed.get("logical_b"))}
        log("  垫底的全量那次：%.3fs / %s MB（不计入下面的统计）"
            % (seed["time"], mb(seed.get("logical_b"))))
        u0 = df_used_bytes(STORE_MOUNT)
        t_start = time.monotonic()
        times, marks = [], []
        i = 0
        while time.monotonic() - t_start < seconds:
            b.dirty()
            t0 = time.monotonic()
            b.sbx.checkpoint.create(name="s%d" % i)
            dt = time.monotonic() - t0
            times.append(dt)
            marks.append(round(t0 - t_start, 2))
            i += 1
            if i % 25 == 0:
                log("    %d 次，已 %.0fs，p50=%.4fs" % (i, time.monotonic() - t_start, pct(times, 0.5)))
        elapsed = time.monotonic() - t_start
        u1 = df_used_bytes(STORE_MOUNT)
        third = max(1, len(times) // 3)
        out.update({
            "count": len(times),
            "elapsed_s": round(elapsed, 1),
            "rate_per_s": round(len(times) / elapsed, 2),
            "create_time": summarize(times),
            "first_third_p50": round(pct(times[:third], 0.5), 4),
            "last_third_p50": round(pct(times[-third:], 0.5), 4),
            "df_total_mb": mb(u1 - u0),
            "df_per_ckpt_mb": round((u1 - u0) / max(1, len(times)) / 1048576.0, 2),
            "times": times, "at_s": marks,
        })
        deg = out["last_third_p50"] / out["first_third_p50"] if out["first_third_p50"] else 0
        out["degradation"] = round(deg, 2)
        log("  %d 次 / %.0fs = %.2f 次每秒；p50 %.4fs p99 %.4fs；"
            "前 1/3 p50 %.4fs → 后 1/3 p50 %.4fs（×%.2f）"
            % (out["count"], elapsed, out["rate_per_s"], out["create_time"]["p50"],
               out["create_time"]["p99"], out["first_third_p50"], out["last_third_p50"], deg))
    finally:
        b.kill()
    return out


# ---------------------------------------------------------------- 汇总

def render(res):
    L = []
    A = L.append
    A("# checkpoint / restore 耗时基准 —— %s 套" % res["scheme"])
    A("")
    A("采于 %s，机器 %s。" % (res["stamp"], res["host"]))
    A("标脏后端 **%s**%s。store 落在 `%s`（%s）。"
      % (res["backend"],
         "（KVM 写保护，软件标脏，**不含硬件标脏的收益**）" if res["backend"] == "kvm-wp"
         else "（HDBSS 硬件标脏）" if res["backend"] == "hdbss" else "",
         lib.STORE, res["store_fs"]))
    A("")
    A("## 一、全量 vs 增量")
    A("")
    A("每个沙箱的第一次 checkpoint 没有基线可比，必然是全量；之后才是增量。")
    A("下表第一行就是全量，后面各行是不同脏页量下的增量。")
    A("")
    A("| | 创建 p50 | p90 | p99 | max | 相对全量 | 逻辑体积 | du 实占 | **新增物理空间** |")
    A("|---|---|---|---|---|---|---|---|---|")
    f = res["full"]
    ft = f["time"]["p50"]
    A("| **全量快照** | **%.3f s** | %.3f s | %.3f s | %.3f s | 1.0× | %s MB | %s MB | %s MB |"
      % (ft, f["time"]["p90"], f["time"]["p99"], f["time"]["max"],
         f["logical_mb"].get("p50"), f["du_mb"].get("p50"), f["df_delta_mb"].get("p50")))
    for t in res["tiers"]:
        c = t["create_time"]
        A("| 增量 脏页 %d MB | **%.3f s** | %.3f s | %.3f s | %.3f s | 快 %.0f× | %s MB | %s MB | %s MB |"
          % (t["tier_mb"], c["p50"], c["p90"], c["p99"], c["max"],
             ft / c["p50"] if c["p50"] else 0,
             t["logical_mb"].get("p50"), t["du_mb"].get("p50"), t["df_per_ckpt_mb"]))
    A("")
    A("三个体积口径不一样，别混：")
    A("")
    A("- **逻辑体积** = `du --apparent-size`，这次 checkpoint 在文件层面\"看起来\"多大。")
    A("  两套都是整份内存的大小，因为两套给出的都是一份完整的内存视图。")
    A("- **du 实占** = `du`，按文件的 st_blocks 算。ext4 套的差分是稀疏文件，这里就是真实增量；")
    A("  XFS 套是 reflink 克隆，共享的 extent 会在每个克隆里各算一遍，所以这一列会虚高。")
    A("- **新增物理空间** = 整档跑完 `df` 的净增量除以次数。**这一列才是盘上真正少掉的空间**，")
    A("  对两套都成立，也是唯一可以横向比较的一列。已排除预热（含必然是全量的第一次）。")
    A("")
    if res.get("freeze"):
        A("## 二、虚机冻结窗口")
        A("")
        A("客户端墙钟里包含 RPC、代理和宿主侧的准备工作；虚机真正停住的时间要短一些，")
        A("而后者才是业务负载感受到的停顿。用 guest 内 ~2.5kHz 的时钟采样器测得。")
        A("")
        A("| 脏页 | 冻结 p50 | 冻结 max | 客户端墙钟 p50 | 冻结占墙钟 | 测量底噪 |")
        A("|---|---|---|---|---|---|")
        for z in res["freeze"]:
            fr, w = z.get("freeze", {}), z.get("wall", {})
            if not fr:
                continue
            A("| %d MB | %.1f ms | %.1f ms | %.1f ms | %d%% | %.1f ms%s |"
              % (z["tier_mb"], fr["p50"], fr["max"], w["p50"] * 1000,
                 round(100 * fr["p50"] / (w["p50"] * 1000)) if w.get("p50") else 0,
                 z.get("noise_floor_ms", 0), "" if z.get("separated") else " ⚠不可区分"))
        A("")
        A("> 底噪 = 同样弄脏节奏但**不做 checkpoint** 时采样器观测到的最大间隔，")
        A("> 也就是这个测法本身的分辨率下限。冻结值必须明显高于它才作数。")
        A("")
    A("## 三、宿主分阶段耗时")
    A("")
    A("第三个口径。前两个（客户端墙钟、虚机冻结）都是单个数字，说的是「多久」；")
    A("这一份说的是**时间花在宿主内部的哪一段**，也就是指标没达标时该改哪。")
    A("由 orchestrator 每次落在产物旁边（`timings.json` / `last-restore-timings.json`），")
    A("不是从日志里刮出来的。下面取每档的中位数，单位毫秒。")
    A("")
    tiers = res["tiers"]
    for kind, key, title in (("create", "create_phases", "checkpoint（create）"),
                             ("restore", "restore_phases", "restore")):
        rows = [t for t in tiers if t.get(key)]
        if not rows:
            continue
        names = []
        for t in rows:
            for n in t[key]:
                if n not in names:
                    names.append(n)
        # 按最大的一档的耗时排，大头排在前面
        ref = rows[-1][key]
        names.sort(key=lambda n: -ref.get(n, {}).get("p50", 0))
        A("### %s" % title)
        A("")
        A("| 阶段 | " + " | ".join("%d MB" % t["tier_mb"] for t in rows) + " |")
        A("|---" * (len(rows) + 1) + "|")
        for n in names:
            cells = []
            for t in rows:
                v = t[key].get(n)
                cells.append("%.2f" % v["p50"] if v else "—")
            A("| `%s` | " % n + " | ".join(cells) + " |")
        A("")
    A("怎么读：")
    A("")
    A("- `frozen` 是虚机真正停住的那段，`pause` / `snapshot` / `seal` / `resume` 都在它里面")
    A("- `fc_*` 是 Firecracker 自报的它那一半（validate / quiesce / memory / vcpus / gic / devices），")
    A("  `fc_rollback` 是从 orchestrator 这边看的同一段，两者之差是 RPC 开销")
    A("- 某一档缺某个阶段（显示 `—`）是正常的：两套方案的步骤本来就不完全一样")
    A("")
    A("## 四、restore")
    A("")
    A("| 脏页档 | 浅集（回退一步）p50 | p99 | 深集（回到链根，%s 步）p50 | 内容校验 |"
      % (res["tiers"][0]["restore_deep_depth"] if res["tiers"] else "?"))
    A("|---|---|---|---|---|")
    for t in res["tiers"]:
        s, d = t["restore_shallow"], t["restore_deep"]
        A("| %d MB | %.4f s | %.4f s | %.4f s | %s |"
          % (t["tier_mb"], s["p50"], s["p99"], d["p50"],
             "全部一致 ✓" if not t["restore_mismatches"] else "**%d 次不符**" % t["restore_mismatches"]))
    A("")
    A("> restore 是把整个虚机换掉，客户端墙钟基本就等于停机时长，不用另外测冻结。")
    A("")
    if res.get("sustain"):
        s = res["sustain"]
        A("## 五、持续 checkpoint")
        A("")
        A("单次快不等于扛得住连打。%d 秒内不停 checkpoint（每次弄脏 %d MB）："
          % (s["seconds"], s["tier_mb"]))
        A("")
        A("| 次数 | 速率 | p50 | p99 | max | 前 1/3 p50 → 后 1/3 p50 | 物理占用 |")
        A("|---|---|---|---|---|---|---|")
        A("| %d | %.2f 次/秒 | %.3f s | %.3f s | %.3f s | %.3f → %.3f s（×%.2f） | %s MB（%.2f MB/次） |"
          % (s["count"], s["rate_per_s"], s["create_time"]["p50"], s["create_time"]["p99"],
             s["create_time"]["max"], s["first_third_p50"], s["last_third_p50"],
             s["degradation"], s["df_total_mb"], s["df_per_ckpt_mb"]))
        A("")
        if s["degradation"] > 1.5:
            A("> ⚠ 后段比前段慢了 %.0f%%。连打之下存在累积效应（回写压力、链深、或空间），"
              % (100 * (s["degradation"] - 1)))
            A("> 单次的数字不能直接外推到高频场景，要以这一段为准。")
        else:
            A("> 前后段基本持平，连打之下没有观察到累积劣化。")
        A("")
    A("## 六、漂移自查")
    A("")
    A("| 脏页档 | 前 1/3 p50 | 后 1/3 p50 | mem_mode |")
    A("|---|---|---|---|")
    for t in res["tiers"]:
        A("| %d MB | %.4f s | %.4f s | %s |"
          % (t["tier_mb"], t["drift"]["first_third_p50"], t["drift"]["last_third_p50"],
             ",".join(str(x) for x in t["mem_modes"])))
    A("")
    A("> 每档都是一个全新沙箱，链深同样从 1 长到 %d —— 链深这个变量在档之间是配平的。"
      % (OPT["warmup"] + OPT["reps"]))
    A("> `mem_mode` 全部应为 incremental；出现 full 说明那次退化成了全量拷贝。")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main

def main():
    log("方案 %s   store %s (%s)   挂载点 %s" % (SCHEME, lib.STORE, lib.store_fstype(), STORE_MOUNT))
    log("档位 %s MB   每档 %d 次(+%d 预热)   全量样本 %d   冻结样本 %d/档   持续 %ds"
        % (OPT["tiers"], OPT["reps"], OPT["warmup"], OPT["full_samples"],
           OPT["freeze_reps"], OPT["sustain_sec"]))
    log("输出 %s" % OUT)

    # 刚切过方案的节点会被 api 标成 unhealthy 一两分钟，这期间任何沙箱都建不出来。
    # 不等就会以一个跟被测代码无关的理由失败。
    if not lib.wait_for_api():
        log("API 一直没就绪，停。")
        return 1

    res = {
        "scheme": SCHEME, "stamp": STAMP,
        "host": lib.sh("hostname"),
        "store": lib.STORE, "store_fs": lib.store_fstype(), "store_mount": STORE_MOUNT,
        "opts": OPT,
    }

    res["full"] = run_full_baseline(OPT["full_samples"])
    res["backend"] = None

    log("\n########## B. 增量扫描 ##########")
    res["tiers"] = []
    for t in OPT["tiers"]:
        r = run_tier(t, OPT["reps"], OPT["warmup"])
        res["backend"] = res["backend"] or r["backend"]
        res["tiers"].append(r)

    if OPT["freeze_reps"]:
        log("\n########## C. 冻结窗口 ##########")
        res["freeze"] = [run_freeze(t, OPT["freeze_reps"]) for t in OPT["tiers"]]

    if OPT["sustain_sec"]:
        res["sustain"] = run_sustain(OPT["sustain_sec"], OPT["sustain_mb"])

    with open(os.path.join(OUT, "bench.json"), "w") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    md = render(res)
    with open(os.path.join(OUT, "报告.md"), "w") as f:
        f.write(md)
    log("\n" + md)
    log("写出 %s/{bench.json,报告.md}" % OUT)

    bad = sum(t["restore_mismatches"] for t in res["tiers"])
    full_in_incr = [t["tier_mb"] for t in res["tiers"] if "full" in [str(x) for x in t["mem_modes"]]]
    if bad:
        log("BENCH FAIL：restore 内容不符 %d 次" % bad); return 1
    if full_in_incr:
        log("BENCH FAIL：这些档位里出现了全量退化 %s —— 脏页跟踪没生效？" % full_in_incr); return 1
    log("BENCH OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
