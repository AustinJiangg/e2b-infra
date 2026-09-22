# -*- coding: utf-8 -*-
"""
T34 运行期读链深（方案 §4.3，评审 B8）。

**问的是运行期，不是 restore 期**：链上叠了 N 层之后，guest 读一个「冷块」——
那种被链上很多层覆盖过、要穿过整条读链才取得到的 rootfs 块 —— 还有多快？
B8 担心的是读链长度线性地拖慢 guest 的 I/O。

怎么造出"冷块"：先在 rootfs 上铺一个 `--file-mb`（默认 256 MB）的文件，然后每建
一层就往这个文件的**随机偏移**写 `--layer-mb`（默认 4 MB）再 create —— 于是每一层
都真的落在这个文件的读链上，读它必然要一层层往下找。

怎么量（guest 里跑 `common.GUEST_COLD_READ`，两遍，每遍前都
`echo 3 > /proc/sys/vm/drop_caches`）：

  · **吞吐**：顺序整读 `--file-mb`，报 MB/s；
  · **延迟**：`--rand-reads`（默认 1000）次随机偏移的 `--rand-kb`（默认 4 KB）小读，
    每次单独计时，报 p50 / p99 / max。样本少于几百次的话噪声会盖过信号（64 次时
    p50 随链深不单调），所以默认量 1000 次。

每一档每一个 phase 都量 `--repeats`（默认 3）遍、**逐字段取中位数**，原始几遍在 JSON 的
`runs` 里。单跑对照不可信（同一二进制 run-to-run 能差一倍）。

**覆盖率 p = `--layer-mb` / `--file-mb`** 决定了「链深 → 期望跳数」的换算
（E[跳数] = (1 − (1−p)^D) / p），所以「相对最浅 N×」这个数**只在同一个 p 下可比**。
报表、note、JSON 里都带上 p，跨两次跑对照时先对齐 p。

每一档链深量两次：`after-create`（刚建完这一档的层）与 `after-restore`（restore 到
这一档最新那层之后）。两者分开报 —— restore 会重排读链，混在一起看不出来。

**判据：只记录，不硬判**（长测试，口径由用户定，同方案 §4.3 的"记录曲线"）。
唯一的软线是 `--max-slowdown`（默认 3.0）：某一档相对链深最浅那一档（同 phase）的
吞吐拖慢倍数超过它，就在那一行标 `warn` 并在 summary 里单列 —— 仍然不判失败。
真正判死的只有"沙箱得活着、每层都建得出来、restore 得成功"这几条前置。

规模：

    python -m crtest T34                                    # 默认 0,20,50 档，≤ 5 分钟
    python -m crtest T34 --depths 0,50,200 --layer-mb 16 \\
        --file-mb 256 --rand-reads 256                      # 需求书的全规模（几十分钟）

`--depths` 是**累计**链深，必须递增：0,20,50 表示先量 0 层，再补到 20 层量一次，
再补到 50 层量一次（不是每档重建一条链）。
"""

NAME = "T34"

from .. import common
from ..common import expect, log, note

COLD = common.BENCH_DIR + "/t34_cold.bin"
P_READ = "/dev/shm/t34_read.py"          # 放 tmpfs：随内存进快照，restore 之后还在

BASIS = "方案 §4.3 T34（评审 B8）：链深 0/50/200 时 guest 读冷块的吞吐与延迟，只记录曲线"


def add_args(ap):
    ap.add_argument("--depths", default="0,20,50",
                    help="在哪几档累计链深上量（逗号分隔、递增；默认 0,20,50。全规模 0,50,200）")
    ap.add_argument("--file-mb", type=int, default=256, help="冷读文件多大 MB（默认 256）")
    ap.add_argument("--layer-mb", type=int, default=4,
                    help="每层往冷读文件里改写多少 MB（默认 4；全规模 16）")
    ap.add_argument("--rand-reads", type=int, default=1000,
                    help="每次测量做几次随机小读（默认 1000；64 次时噪声盖过信号）")
    ap.add_argument("--repeats", type=int, default=3,
                    help="每档每个 phase 量几遍取中位数（默认 3；单跑对照不可信）")
    ap.add_argument("--rand-kb", type=int, default=4, help="随机小读的块大小 KB（默认 4）")
    ap.add_argument("--max-slowdown", type=float, default=3.0,
                    help="软阈值：相对最浅那一档的吞吐拖慢超过这个倍数就标 warn（默认 3.0；"
                         "0 = 不标）")
    ap.add_argument("--no-restore-measure", action="store_true",
                    help="每档只量 after-create，不量 restore 之后那一次（省一半时间）")
    ap.add_argument("--seed", type=int, default=20260918, help="随机种子（随机读偏移用）")
    return ap


# ---------------------------------------------------------------- 小工具

def _depths(text):
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            raise common.Unmet("--depths 里有不是整数的一段：%r" % part,
                               "写法是递增的整数列表，比如 --depths 0,20,50")
        if n < 0:
            raise common.Unmet("--depths 里有负数：%d" % n, "链深不能是负的")
        out.append(n)
    if not out:
        raise common.Unmet("--depths 是空的", "至少给一档，比如 --depths 0,20,50")
    if out != sorted(out) or len(set(out)) != len(out):
        raise common.Unmet("--depths 必须严格递增：%s" % out,
                           "各档是**累计**链深（0,20,50 = 量 0 层、补到 20 层、补到 50 层），"
                           "不是每档重建一条链")
    return out


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


_NUM_KEYS = ("mbps", "seq_s", "read_mb", "p50_ms", "p99_ms", "max_ms", "rand_n", "dropped")


def _measure(ctx, box, depth, phase, seed):
    """在 guest 里量 --repeats 遍冷读，逐字段取中位数。返回一行 row（原始几遍在 runs 里）。"""
    a = ctx.args
    reps = max(1, int(getattr(a, "repeats", 1) or 1))
    runs = [_measure_once(ctx, box, depth, phase, seed + k, k, reps) for k in range(reps)]
    row = {"depth": depth, "phase": phase, "repeats": reps,
           "cover_p": _cover_p(a), "runs": runs}
    for k in _NUM_KEYS:
        row[k] = _median([r.get(k) for r in runs])
    log("    链深 %-4d %-14s %8s MB/s   p50 %8s ms   p99 %8s ms   （%d 遍中位数）"
        % (depth, phase, common.fmt_s(row["mbps"]), common.fmt_ms(row["p50_ms"]),
           common.fmt_ms(row["p99_ms"]), reps))
    return row


def _cover_p(a):
    """覆盖率 p = 每层改写的 MB / 冷读文件的 MB。决定链深→跳数的换算，跨跑对照必须对齐。"""
    return (float(a.layer_mb) / a.file_mb) if a.file_mb else None


def _measure_once(ctx, box, depth, phase, seed, idx, reps):
    """在 guest 里量一次冷读。返回一行原始 row。"""
    a = ctx.args
    out = box.run("python3 %s %s %d %d %d %d"
                  % (P_READ, COLD, a.file_mb, a.rand_reads, a.rand_kb * 1024, seed),
                  timeout=1800)
    kv = common.parse_kv(out)

    def f(key):
        try:
            return float(kv[key])
        except (KeyError, ValueError):
            return None

    row = {"depth": depth, "phase": phase, "rep": idx, "mbps": f("mbps"), "seq_s": f("seq_s"),
           "read_mb": f("read_mb"), "p50_ms": f("p50_ms"), "p99_ms": f("p99_ms"),
           "max_ms": f("max_ms"), "rand_n": f("rand_n"), "dropped": f("dropped")}
    ctx.op({"op": "cold_read", "box": box.label, "sandbox": box.id, "ok": True,
            "wall_s": row["seq_s"]}, stage="T34", step=phase, **row)
    expect(ctx, "链深 %d %s 第 %d/%d 遍：drop_caches 生效" % (depth, phase, idx + 1, reps),
           row["dropped"] == 2,
           "两遍各丢一次页缓存（dropped=2）", "dropped=%s" % kv.get("dropped"),
           "读不到 /proc/sys/vm/drop_caches 的话量的是页缓存，不是冷块，数字没意义")
    log("      · 第 %d/%d 遍  %8s MB/s   p50 %8s ms   p99 %8s ms"
        % (idx + 1, reps, common.fmt_s(row["mbps"]), common.fmt_ms(row["p50_ms"]),
           common.fmt_ms(row["p99_ms"])))
    return row


def _build_to(ctx, box, cur, target, rnd):
    """把链从 cur 层补到 target 层，每层改写冷读文件的一小块。返回 (新的层数, 最后一个 id)。"""
    a = ctx.args
    last = None
    span = max(1, a.file_mb - a.layer_mb)
    for i in range(cur, target):
        off = rnd.randrange(0, span)
        box.run("dd if=/dev/urandom of=%s bs=1M seek=%d count=%d conv=notrunc,fsync "
                "2>/dev/null" % (COLD, off, a.layer_mb), timeout=600)
        rec = ctx.op(box.create("d%d" % i, record_scene=False), stage="T34",
                     step="build", idx=i)
        expect(ctx, "建第 %d 层" % i, rec.get("ok"), "成功",
               rec.get("err") or "%.3f s" % rec.get("wall_s", 0),
               "T34 前置：链得先建起来")
        last = rec["id"]
    return target, last


# ---------------------------------------------------------------- 主流程

def run(ctx):
    a = ctx.args
    import random
    depths = _depths(a.depths)
    rnd = random.Random(a.seed)

    box = common.spawn(ctx, 1, "t34-")[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)
    box.put(P_READ, common.GUEST_COLD_READ)

    log("  铺冷读文件 %s（%d MB）" % (COLD, a.file_mb))
    rc, out = box.sh("dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null && sync"
                     % (COLD, a.file_mb), timeout=3600)
    expect(ctx, "冷读文件铺好了", rc == 0, "dd 退出码 0", "退出码 %s：%s" % (rc, out[-200:]),
           "T34 前置：--file-mb 调小些，或者看看 rootfs 还有没有空间")
    sz = common.as_int(box.run("stat -c %%s %s" % COLD).strip())
    expect(ctx, "冷读文件大小对", sz == a.file_mb * 1048576,
           "%d 字节" % (a.file_mb * 1048576), sz, "T34 前置")

    rows = []
    cur, last_id = 0, None
    cover_p = _cover_p(a)
    log("\n  开量（每档两次：刚建完 / restore 之后；每次 %d 遍取中位数）" % max(1, a.repeats))
    log("  覆盖率 p = layer_mb/file_mb = %d/%d = %.5f"
        "（E[跳数] = (1-(1-p)^D)/p；相对最浅的倍数只在同一个 p 下可比）"
        % (a.layer_mb, a.file_mb, cover_p))
    for want_depth in depths:
        if want_depth > cur:
            log("  建链 %d → %d 层" % (cur, want_depth))
            cur, last_id = _build_to(ctx, box, cur, want_depth, rnd)
        rows.append(_measure(ctx, box, cur, "after-create", a.seed))
        if a.no_restore_measure or last_id is None:
            if last_id is None:
                note(ctx, "链深 %d 的 after-restore" % cur, "跳过",
                     "这一档还没有 checkpoint 可回（链深 0）", BASIS)
            continue
        rr = ctx.op(box.restore(last_id, verify=False), stage="T34",
                    step="restore", depth=cur)
        expect(ctx, "链深 %d：回到最新一层" % cur, rr.get("ok"), "成功",
               rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
               "T34 前置；失败现场 %s" % box.store_dir(last_id))
        rows.append(_measure(ctx, box, cur, "after-restore", a.seed))

    rows, warns = common.judge_read_slowdown(rows, a.max_slowdown)
    log("")
    common.table(["链深", "阶段", "覆盖率 p", "MB/s", "p50 ms", "p99 ms", "max ms",
                  "相对最浅", ""],
                 [[str(r["depth"]), r["phase"], "%.5f" % cover_p, common.fmt_s(r["mbps"]),
                   common.fmt_ms(r["p50_ms"]), common.fmt_ms(r["p99_ms"]),
                   common.fmt_ms(r["max_ms"]),
                   "-" if r["slowdown"] is None else "%.2f×" % r["slowdown"],
                   "warn" if r["warn"] else ""] for r in rows])

    for r in rows:
        note(ctx, "链深 %d %s 冷读吞吐" % (r["depth"], r["phase"]),
             "只记录（软线 %g×）" % a.max_slowdown,
             "%s MB/s，p50 %s ms，p99 %s ms，相对最浅 %s%s（覆盖率 p=%.5f，%d 遍中位数）"
             % (common.fmt_s(r["mbps"]), common.fmt_ms(r["p50_ms"]),
                common.fmt_ms(r["p99_ms"]),
                "-" if r["slowdown"] is None else "%.2f×" % r["slowdown"],
                "（warn）" if r["warn"] else "", cover_p, r.get("repeats", 1)),
             BASIS)
    if warns:
        log("  ⚠ %d 档超过软阈值 %g×：%s"
            % (len(warns), a.max_slowdown,
               ["%d/%s=%.2f×" % (w["depth"], w["phase"], w["slowdown"]) for w in warns]))
    note(ctx, "超过软阈值的档数", "0（超了只是提醒，不判失败）",
         "%d 档：%s" % (len(warns),
                       ["%d/%s" % (w["depth"], w["phase"]) for w in warns] or "无"),
         "--max-slowdown %g" % a.max_slowdown)

    ok, d = box.alive()
    expect(ctx, "量完之后沙箱可用", ok, "命令能跑、能写盘、心跳在推进", d,
           "活体判据同 checkpoint_verify.py")

    ctx.results["summary"]["T34"] = {
        "depths": depths, "file_mb": a.file_mb, "layer_mb": a.layer_mb,
        "rand_reads": a.rand_reads, "rand_kb": a.rand_kb,
        "repeats": max(1, a.repeats), "cover_p": cover_p,
        "max_slowdown": a.max_slowdown, "rows": rows,
        "warned": [{"depth": w["depth"], "phase": w["phase"], "slowdown": w["slowdown"]}
                   for w in warns],
        "chain_depth_final": cur,
    }
