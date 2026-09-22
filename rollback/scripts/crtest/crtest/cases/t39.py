# -*- coding: utf-8 -*-
"""
T39 做过 checkpoint 的沙箱再走**原生 pause / resume**（09-18 的 P0）。

已确诊的缺陷（`e2b-repo/tmp/pause-after-ckpt-20260918/analysis.md`）：原生 pause
导出的 memfile 差分是**相对模板 memfile** 的，所以必须点名"自 VM 启动以来写过的
每一页"；而它自 infra-arm `c9a92a5ab`（09-11）起取的是 Firecracker 的写跟踪位图，
那张位图的语义是"**自上次快照或回滚以来**"—— checkpoint create 写完快照会清零
（`vstate/memory.rs` 的 `reset_dirty`），in-place rollback 的 Phase 9 也清零
（`rollback.rs`），而且回滚往 guest 内存写回的那批页走 VMM 的 mmap，KVM 根本不记。

于是**做过 checkpoint 的沙箱一 pause，快照里就只剩最后一段 epoch 的页**，其余的
resume 时从模板基线读回来 —— guest 内存是两个时刻拼的，内核在 resume 后 24 ms 炸
（`BUG: Bad rss-counter state` → 野指针 → `Kernel panic`），SDK 那头看到的是
connect 500 `Failed to place sandbox`（单节点被排空后的伪装错误，见报告 §1.2）。
**pause 自己是返回成功的**，坏快照已经上传，所以这条必须由测试守着。

修法（infra-arm jll `176802f72`，纯 orchestrator）：沙箱的内存后端记一份"自启动
以来"的累计脏页集合 `DirtySinceBoot`，在 Firecracker 清位图**之前**把那张位图并
进去（create 的侧车、restore 的 live 位图、restore 的 revert 位图三处），pause 时
`Uffd.DiffMetadata` 把它并回 tracked 位图；并不进去就把集合标为不可信，回落到
驻留判据（导得多，但绝不导少）。

本用例只管**外部可观察的后果**，判定与实现无关：

  · 场景 a：create 之后直接 pause/resume —— 常驻进程的 64 MB 自校验内存必须
    逐页完好、pid 与进程状态不变、M0/M1 两个标记都在、guest 命令能跑；
  · 场景 b：create → 写 M1 → restore 回 create 那一刻 → pause/resume ——
    resume 之后必须看到 M0、**不该**看到 M1，内存照样逐页完好；
  · 场景 c：create/restore 反复 3 轮再 pause/resume —— 同 a 的判据，压的是
    "多段 epoch 一段都不能漏"。

内存现场沿用 T14 的那套：`GUEST_MEM_WRITER` 把 64 MB 铺进 tmpfs（=guest 内存）的
mmap 里，每页 16 字节页头记 页号/版本/crc32，于是**单页自校验**——"半旧半新"当场
看得出来，不需要"期望镜像"。铺完就 `SIGSTOP` 住写者：内容从此不动，md5 可以直接
跨 pause 比，进程状态也该原样回来（`T`）。写者铺满是在**第一次 create 之前**做的，
所以那 64 MB 正落在缺陷会丢掉的那一段 epoch 里。

判定的秤：

  · **内存**：整块 md5 与 pause 前一致 + 页级自校验坏页 0（`common.judge_page_scan`）；
  · **进程**：常驻写者 pid 不变、状态还是 `T` —— pid 变了说明是虚机重启而不是
    内存回来了；
  · **标记**：内存里的（`/dev/shm`，tmpfs = guest 内存）与盘上的（`BENCH_DIR`，
    走 NBD 写层）各一份，两边都按场景该有的样子；
  · **guest 还活着**：`Box.alive()`（命令 + 读写 + 心跳进程在跑）。

服务端那条导出日志（`sandbox.go` 的 `exporting the memfile diff of a native pause`
与 `uffd.go` 的 `memfile diff page set for the native pause`）按**记录项**走：页数、
来源（`tracked` / `tracked+accumulated` / `resident`）、累计集合大小都记进 JSON。
没修的版本上它压根不存在，所以"读不到"不判失败 —— 但内存判定会先炸。

`Sandbox.connect()` 带上限重试：resume 是 API 侧现建沙箱，偶发 503/超时是它自己的
事，不该算进本用例；重试次数与间隔见 `--connect-attempts / --connect-gap`。
"""

NAME = "T39"

import time

from .. import common
from ..common import Failed, expect, log, note

MEM_FILE = "/dev/shm/t39_mem"
MEM_CTL = "/dev/shm/t39_ctl"
MEM_LOG = "/tmp/t39_mem.md5"
P_WRITER = "/dev/shm/t39_mem_writer.py"
P_VERIFY = "/dev/shm/t39_mem_verify.py"
PID_MEM = "/dev/shm/t39_mem.pid"

MEM_MARK = "/dev/shm/t39_mark"                     # tmpfs = guest 内存
FILE_MARK = common.BENCH_DIR + "/t39_mark"         # 根文件系统 = NBD 写层

# 服务端导出日志（infra-arm jll 176802f72）。两行分工：带 sandbox.id 的那行在
# sandbox.go，带 source 的那行在 uffd.go。
EXPORT_LOG = r"exporting the memfile diff of a native pause"
SOURCE_LOG = r"memfile diff page set for the native pause"
EXPORT_KEYS = ("pages", "page_size", "accumulated_pages", "bitmap_merges",
               "accumulated_trusted", "source", "tracked_pages", "sandbox.id")

BASIS = ("infra-arm jll 176802f72（uffd/sinceboot.go 的 DirtySinceBoot、"
         "uffd/uffd.go 的 DiffMetadata、sandbox/checkpoint.go 的三处并入点）；"
         "缺陷定位见 e2b-repo/tmp/pause-after-ckpt-20260918/analysis.md")


def add_args(ap):
    ap.add_argument("--scene", default="all", choices=["a", "b", "c", "all"],
                    help="跑哪个场景（a=create 后 pause，b=create+restore 后 pause，"
                         "c=3 轮 create/restore 后 pause；默认三个都跑）")
    ap.add_argument("--mem-mb", type=int, default=64,
                    help="常驻进程持有的自校验内存 MB（默认 64）")
    ap.add_argument("--rounds", type=int, default=3, help="场景 c 的 create/restore 轮数（默认 3）")
    ap.add_argument("--connect-attempts", type=int, default=10,
                    help="pause 之后 Sandbox.connect() 最多试几次（默认 10）")
    ap.add_argument("--connect-gap", type=float, default=5.0,
                    help="connect 重试间隔秒数（默认 5）")
    ap.add_argument("--connect-timeout", type=float, default=180.0,
                    help="单次 connect 的超时秒数（默认 180）")
    return ap


# ---------------------------------------------------------------- 现场

def prepare_box(ctx, box):
    """铺 64 MB 自校验内存，铺完 SIGSTOP 住写者。返回写者 pid。"""
    a = ctx.args
    box.run("mkdir -p %s" % common.BENCH_DIR)
    box.put(P_WRITER, common.GUEST_MEM_WRITER)
    box.put(P_VERIFY, common.GUEST_MEM_VERIFY)
    box.run("rm -f %s %s %s %s.ready %s %s"
            % (MEM_FILE, MEM_CTL, MEM_LOG, MEM_LOG, MEM_MARK, FILE_MARK))
    box.setup(warm_mem=16, warm_file=8)             # 心跳 + 公共现场口径
    pid = box.bg("python3 %s %s %d %s %s" % (P_WRITER, MEM_FILE, a.mem_mb, MEM_LOG, MEM_CTL),
                 PID_MEM)
    rc, _ = box.sh("for i in $(seq 1 180); do [ -f %s.ready ] && exit 0; sleep 1; done; exit 1"
                   % MEM_LOG, timeout=240)
    if rc != 0:
        raise Failed("常驻内存进程起来了", "180 s 内铺满 %d MB 自校验内存" % a.mem_mb,
                     "没等到 %s.ready（沙箱 %s）" % (MEM_LOG, box.id), "T39 前置")
    box.sig(pid, "STOP")
    time.sleep(0.5)
    st = box.pid_state(pid)
    if st != "T":
        raise Failed("写者停住了", "进程状态 T", "状态 = %r（沙箱 %s）" % (st, box.id),
                     "T39 前置：内容要在整段测试里不动，md5 才能跨 pause 比")
    return pid


def mark(box, text):
    """两份标记：内存里一份（tmpfs），盘上一份（写层）。"""
    box.run("echo %s >> %s; echo %s >> %s; sync" % (text, MEM_MARK, text, FILE_MARK))


def state(box, pid):
    """一次读齐判定要的全部现场。"""
    d = box.kv(box.run(
        "echo mem_md5=$(md5sum %s | cut -d' ' -f1)\n"
        "echo mem_mark=$(tr '\\n' ',' < %s 2>/dev/null || echo MISSING)\n"
        "echo file_mark=$(tr '\\n' ',' < %s 2>/dev/null || echo MISSING)\n"
        "echo writer_pid=$(cat %s 2>/dev/null || echo MISSING)\n"
        "echo writer_state=$(awk '{print $3}' /proc/%s/stat 2>/dev/null || echo GONE)\n"
        "echo cmd=$(echo alive)\n"
        % (MEM_FILE, MEM_MARK, FILE_MARK, PID_MEM, pid), timeout=300))
    for k in ("mem_mark", "file_mark"):
        if not d.get(k):
            d[k] = "MISSING"
    return d


def page_scan(ctx, box, stage, when):
    """页级自校验：坏页 = 页头的 页号/版本/crc32 与 body 对不上。"""
    mv = common.parse_kv(box.run("python3 %s %s %s" % (P_VERIFY, MEM_FILE, MEM_CTL), timeout=900))
    bad_pages = [x for x in (mv.get("bad_pages") or "").split(",") if x]
    ok, tolerated, detail = common.judge_page_scan(mv.get("bad"), bad_pages, mv.get("inprogress"))
    ctx.op({"op": "scan", "stage": stage, "when": when, "box": box.label, "sandbox": box.id,
            "mem": mv, "page_judge": {"ok": ok, "tolerated": tolerated, "detail": detail}})
    return ok, tolerated, detail, mv


# ---------------------------------------------------------------- pause / resume

def pause_resume(ctx, box, stage):
    """`beta_pause()` 之后带上限重试地 `Sandbox.connect()`。

    连上之后把 Box 的 `sbx` 换成新实例（本地账本 —— 现场、名字、checkpoint id ——
    原样留着），于是后面的判定还是对着同一个 Box 问。
    """
    from e2b import Sandbox

    a = ctx.args
    rec = {"op": "pause", "stage": stage, "box": box.label, "sandbox": box.id, "t": time.time()}
    t0 = time.monotonic()
    try:
        box.sbx.beta_pause()
        rec["ok"] = True
    except Exception as e:          # noqa: BLE001
        rec["ok"] = False
        common.record_error(rec, e)
    rec["wall_s"] = time.monotonic() - t0
    ctx.op(rec)

    rrec = {"op": "resume", "stage": stage, "box": box.label, "sandbox": box.id,
            "t": time.time(), "attempts": 0}
    t0 = time.monotonic()
    last = None
    for i in range(a.connect_attempts):
        rrec["attempts"] = i + 1
        try:
            box.sbx = Sandbox.connect(box.id, request_timeout=a.connect_timeout)
            rrec["ok"] = True
            last = None
            break
        except Exception as e:      # noqa: BLE001
            last = e
            rrec["ok"] = False
            common.record_error(rrec, e)
            if i + 1 < a.connect_attempts:
                time.sleep(a.connect_gap)
    rrec["wall_s"] = time.monotonic() - t0
    ctx.op(rrec)

    return rec, rrec, last


def read_export_log(ctx, orch, stage):
    """把服务端那两行导出日志记下来（记录项，读不到不判失败）。"""
    fields = {}
    for pattern, tag in ((EXPORT_LOG, "sandbox"), (SOURCE_LOG, "uffd")):
        lines, waited, reads = common.wait_for_log(orch, pattern)
        line = lines[-1] if lines else ""
        got = common.parse_log_fields(line, EXPORT_KEYS) if line else {}
        fields[tag] = {"found": bool(lines), "waited_s": waited, "reads": reads,
                       "fields": got, "line": line[-400:]}
    sbx_f = fields["sandbox"]["fields"]
    uffd_f = fields["uffd"]["fields"]
    note(ctx, "%s 服务端导出日志（页数 / 来源 / 累计集合）" % stage,
         "预期 source = tracked+accumulated（做过 checkpoint 的沙箱），pages ≥ accumulated_pages；"
         "没修的版本上这两行都不存在",
         "pages=%s page_size=%s accumulated_pages=%s bitmap_merges=%s trusted=%s source=%s "
         "tracked_pages=%s" % (sbx_f.get("pages"), sbx_f.get("page_size"),
                               sbx_f.get("accumulated_pages"), sbx_f.get("bitmap_merges"),
                               sbx_f.get("accumulated_trusted"), uffd_f.get("source"),
                               uffd_f.get("tracked_pages")),
         "sandbox.go 的 Snapshot()、uffd/uffd.go 的 diffMetadata（infra-arm jll 176802f72）")
    return fields


def check_resume(ctx, box, stage, pid, before, want_marks, prec, rrec, last_err):
    """pause/resume 之后的全部判定。"""
    expect(ctx, "%s：pause 返回成功" % stage, prec.get("ok"), "beta_pause() 不抛",
           prec.get("err") or "ok", BASIS)
    expect(ctx, "%s：resume（Sandbox.connect）成功" % stage, rrec.get("ok"),
           "%d 次以内连上" % ctx.args.connect_attempts,
           "试了 %s 次；最后一个错误 %s" % (rrec.get("attempts"),
                                           ("%s: %s" % (type(last_err).__name__, last_err))[:300]
                                           if last_err else "无"),
           "缺陷形态就在这里：坏快照 resume 后 guest 内核 24 ms 就 panic，"
           "API 侧把它包成 500 Failed to place sandbox（analysis.md §1）")
    if not rrec.get("ok"):
        raise Failed("%s：resume" % stage, "连上", "连不上，后面的现场判定没得做", BASIS)

    after = state(box, pid)
    ctx.op({"op": "state", "stage": stage, "when": "after-resume", "box": box.label,
            "sandbox": box.id, "before": before, "after": after, "want_marks": want_marks})
    ok, bad = common.judge_pause_resume(before, after, want_marks)
    expect(ctx, "%s：resume 之后现场逐项对得上" % stage, ok,
           "内存 md5 不变、写者 pid/状态不变、标记 %s"
           % (want_marks or "与 pause 前一致"),
           "；".join(bad) if bad else "逐项一致（md5 %s，写者 %s 状态 %s，"
                                      "内存标记 %s，盘上标记 %s）"
           % (after.get("mem_md5"), after.get("writer_pid"), after.get("writer_state"),
              after.get("mem_mark"), after.get("file_mark")),
           BASIS)

    p_ok, p_tol, p_detail, mv = page_scan(ctx, box, stage, "after-resume")
    if p_tol:
        note(ctx, "%s：快照时刻的进行中页" % stage, "至多 1 页且必须是控制页记的那一页",
             p_detail, "见 common.judge_page_scan")
    expect(ctx, "%s：resume 之后页级自校验" % stage, p_ok,
           "坏页 0（或 ⊆ {进行中页}）",
           "%s（共 %s 页，首个坏页 %s）" % (p_detail, mv.get("pages"), mv.get("first_bad")),
           "撕裂判据：页头的 页号/版本/crc32 与 body 对不上 = 内存来自两个时刻。"
           "缺陷未修时这里会是大片坏页（如果 guest 还活着的话）")

    alive_ok, d = box.alive()
    expect(ctx, "%s：resume 之后 guest 还在干活" % stage, alive_ok,
           "命令能跑、盘能读写、心跳进程还在 tick",
           "cmd=%s rw=%s hb=%s→%s" % (d.get("cmd"), d.get("rw"), d.get("hb0"), d.get("hb1")),
           "Box.alive() 的公共口径")
    return after


# ---------------------------------------------------------------- 场景

def scene_a(ctx, orch):
    """create 之后直接 pause/resume。"""
    box = common.spawn(ctx, 1, "t39a-")[0]
    log("  场景 a 沙箱 %s" % box.id)
    pid = prepare_box(ctx, box)
    mark(box, "M0")
    box.dirty("g0", mem_mb=16, file_mb=4)

    crec = ctx.op(box.create("g0"), stage="T39a", step="create")
    expect(ctx, "场景 a：建 checkpoint", crec.get("ok"), "成功", crec.get("err") or "ok", BASIS)
    mark(box, "M1")

    before = state(box, pid)
    ctx.op({"op": "state", "stage": "T39a", "when": "before-pause", "box": box.label,
            "sandbox": box.id, "state": before})
    orch.mark()
    prec, rrec, err = pause_resume(ctx, box, "T39a")
    check_resume(ctx, box, "场景 a", pid, before, None, prec, rrec, err)
    logs = read_export_log(ctx, orch, "场景 a")
    ctx.results["summary"]["T39"]["a"] = {"sandbox": box.id, "checkpoint": crec.get("id"),
                                          "before": before, "export_log": logs}
    return box


def scene_b(ctx, orch):
    """create → 写 M1 → restore 回 create 那一刻 → pause/resume。"""
    box = common.spawn(ctx, 1, "t39b-")[0]
    log("  场景 b 沙箱 %s" % box.id)
    pid = prepare_box(ctx, box)
    mark(box, "M0")
    box.dirty("g0", mem_mb=16, file_mb=4)

    crec = ctx.op(box.create("g0"), stage="T39b", step="create")
    expect(ctx, "场景 b：建 checkpoint", crec.get("ok"), "成功", crec.get("err") or "ok", BASIS)
    ck = crec["id"]
    mark(box, "M1")
    box.dirty("g1", mem_mb=16, file_mb=4)

    rrec0 = ctx.op(box.restore(ck), stage="T39b", step="restore")
    expect(ctx, "场景 b：restore 回 create 那一刻", rrec0.get("ok") and rrec0.get("verified"),
           "成功且现场逐项回到 create 时",
           rrec0.get("err") or ("现场不一致 %s" % rrec0.get("mismatch") if rrec0.get("mismatch")
                                else "ok"),
           "Box.restore(verify=True) 的公共口径")

    before = state(box, pid)
    ctx.op({"op": "state", "stage": "T39b", "when": "before-pause", "box": box.label,
            "sandbox": box.id, "state": before})
    expect(ctx, "场景 b：restore 之后 M1 已经不在了（pause 之前的对照）",
           before.get("mem_mark") == "M0," and before.get("file_mark") == "M0,",
           "两份标记都只剩 M0,",
           "内存 %s / 盘上 %s" % (before.get("mem_mark"), before.get("file_mark")),
           "M1 是 create 之后写的，回滚到 create 那一刻就该没了")

    want = {"mem_mark": "M0,", "file_mark": "M0,"}
    orch.mark()
    prec, rrec, err = pause_resume(ctx, box, "T39b")
    check_resume(ctx, box, "场景 b", pid, before, want, prec, rrec, err)
    logs = read_export_log(ctx, orch, "场景 b")
    ctx.results["summary"]["T39"]["b"] = {"sandbox": box.id, "checkpoint": ck,
                                          "before": before, "export_log": logs}
    return box


def scene_c(ctx, orch):
    """反复 create/restore 若干轮再 pause/resume。"""
    a = ctx.args
    box = common.spawn(ctx, 1, "t39c-")[0]
    log("  场景 c 沙箱 %s（%d 轮）" % (box.id, a.rounds))
    pid = prepare_box(ctx, box)
    mark(box, "M0")

    cks = []
    for r in range(a.rounds):
        box.dirty("g%d" % r, mem_mb=16, file_mb=4)
        crec = ctx.op(box.create("g%d" % r), stage="T39c", step="create", round=r)
        expect(ctx, "场景 c 第 %d 轮：建 checkpoint" % (r + 1), crec.get("ok"), "成功",
               crec.get("err") or "ok", BASIS)
        cks.append(crec["id"])
        mark(box, "R%d" % r)
        box.dirty("g%d-post" % r, mem_mb=16, file_mb=4)
        rr = ctx.op(box.restore(cks[-1]), stage="T39c", step="restore", round=r)
        expect(ctx, "场景 c 第 %d 轮：restore" % (r + 1), rr.get("ok") and rr.get("verified"),
               "成功且现场逐项回到 create 时",
               rr.get("err") or ("现场不一致 %s" % rr.get("mismatch") if rr.get("mismatch")
                                 else "ok"),
               "Box.restore(verify=True) 的公共口径")

    before = state(box, pid)
    ctx.op({"op": "state", "stage": "T39c", "when": "before-pause", "box": box.label,
            "sandbox": box.id, "state": before})
    expect(ctx, "场景 c：pause 之前只剩 M0（每轮的 R 标记都在 create 之后写的）",
           before.get("mem_mark") == "M0," and before.get("file_mark") == "M0,",
           "两份标记都只剩 M0,",
           "内存 %s / 盘上 %s" % (before.get("mem_mark"), before.get("file_mark")),
           "每轮 restore 回的都是本轮 create 那一刻，R 标记写在它之后")

    orch.mark()
    prec, rrec, err = pause_resume(ctx, box, "T39c")
    check_resume(ctx, box, "场景 c", pid, before, None, prec, rrec, err)
    logs = read_export_log(ctx, orch, "场景 c")
    ctx.results["summary"]["T39"]["c"] = {"sandbox": box.id, "checkpoints": cks,
                                          "before": before, "export_log": logs}
    return box


SCENE_FN = {"a": scene_a, "b": scene_b, "c": scene_c}


def run(ctx):
    a = ctx.args
    scenes = list("abc") if a.scene == "all" else [a.scene]
    ctx.results["summary"].setdefault("T39", {})
    ctx.stage("T39", scenes=scenes, mem_mb=a.mem_mb, rounds=a.rounds)
    orch = common.OrchLog()
    if not orch.dir:
        note(ctx, "服务端日志", "在宿主机上跑才读得到那两行导出日志",
             "读不到 orchestrator 的 nomad 日志目录 —— 导出页数/来源这几项会缺省",
             "common.OrchLog：从 /proc/<pid>/fd/1 反查日志目录")
    for s in scenes:
        log("")
        log("== 场景 %s ==" % s)
        SCENE_FN[s](ctx, orch)
