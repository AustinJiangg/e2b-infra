# -*- coding: utf-8 -*-
"""
T14 写密集并发正确性（方案 §4.1）。

现有 A/D 段的脏集只有几十 MB，压不到 B4/B5/B7 与 uffd 那条路。这里每个沙箱在
guest 里养两个写者，然后 N 个沙箱**对齐**做 create / restore：

  · 内存写者：mmap 一个 tmpfs 文件（MAP_SHARED，所以"内存内容"在 guest 里能直接
    md5sum），按随机偏移一直改页；每页 16 字节页头记 页号 / 版本号 / body 的 crc32，
    body 由这两个数派生 —— 于是**每一页都能自校验**，回滚撕裂（半旧半新、页错位）
    不用"期望快照"也看得出来。每秒把整块 md5 追加进 /tmp/mem.md5。
  · 文件写者：一行一行 append `seq crc32(payload)` 并 fsync，payload 由 seq 派生，
    每行自校验、seq 必须连续。

**怎么拿到良定义的"期望"**：create 之前先把两个写者 SIGSTOP 住，读一次 md5 当期望，
再 create。于是快照拍到的就是"写者停着"的那一刻。create 完 SIGCONT 接着写脏（让
当前状态明显不同于快照），restore 之后写者会**随内存一起回到停着的那一刻**，内存
不再变化，md5 可以精确比对 —— 需求书提醒的"restore 后 guest 里后台进程也回到
checkpoint 时刻的状态"，这里正是拿它当工具用：判据是 pid 不变 + 进程状态回到 T
（stopped）+ 文件里的 seq 回到快照那一刻的值。

判定：0 不一致 —— 内存整块 md5、文件整体 md5、页级自校验坏页数、行级自校验、
写者 pid 与状态、mem.md5 末行 seq，全部要对上。

**一个例外（09-17 加）：拍摄时刻正在写的那一页。** SIGSTOP / VM pause 可能落在写者
写某一页的中途（那一次 memcpy 里），快照拍下的就是半新半旧的一页，restore 忠实还原
——这不是"回滚混了两个时刻"。所以写者写页前把页号写进控制页 `/dev/shm/t14-inprogress`
（写完清 -1，控制页也在 tmpfs 里、随内存一起进快照），判定改成 **坏页集合 ⊆ {进行中
页号}**：至多 1 页且正是那一页 → 通过并记 note；坏页 ≥ 2 或坏页 ≠ 进行中页号 → 仍判
失败。第 2 轮验收 `--sandboxes 4` 报的页 42451 正是前者（整块 md5 与期望完全一致）。
文件那份同理：半条只可能落在末尾，单独报 tail_partial，容忍 1 条。

`--no-stop`（09-17 加）换一种问法：**不** SIGSTOP 写者，快照就拍在写者活动中 ——
这才是真实负载下的 create。代价是"期望 md5"不可预知（快照拍在哪一次写之间没人
知道），所以强判定退化成弱判定：

  · 页级 / 行级**自校验**（每页每行自带 页号/版本/crc32）—— 撕裂、页错位、seq 断裂
    不需要"期望快照"也看得出来，这是 --no-stop 下的正主；
  · restore 后写者**还是原来那两个 pid**、状态是 R/S/D（活着在跑，不是 T、不是没了）
    → 内存真回来了，而不是虚机重启；
  · seq **回滚且落在快照区间**：create 前采一次（下界 lo）、create 返回后再采一次
    （上界 hi），快照那一刻夹在中间；restore 之后读到的 seq 必须 ≥ lo，并且明显
    小于 restore 之前那次采样（证明真回滚了）。上界不判——写者 restore 之后立刻
    接着写，读到的时候已经涨过 hi 了。

两种模式各测什么：默认模式测**精确回滚**（一个字节都不许差），`--no-stop` 测
**活动中拍快照的自洽性**（不撕裂、进程连续、seq 回到快照区间）。
"""

NAME = "T14"

import threading
import time

from .. import common
from ..common import Failed, expect, log, note

MEM_FILE = "/dev/shm/t14_mem.bin"
MEM_CTL = "/dev/shm/t14-inprogress"   # 写者的"进行中页号"控制页（tmpfs，随内存进快照）
MEM_LOG = "/tmp/mem.md5"
FS_FILE = common.BENCH_DIR + "/t14_journal"
FS_MARK = "/dev/shm/t14_fseq"
P_WRITER = "/dev/shm/t14_mem_writer.py"
P_VERIFY = "/dev/shm/t14_mem_verify.py"
P_FWRITER = "/dev/shm/t14_file_writer.py"
P_FVERIFY = "/dev/shm/t14_file_verify.py"
PID_MEM = "/dev/shm/t14_mem.pid"
PID_FS = "/dev/shm/t14_fs.pid"


def add_args(ap):
    ap.add_argument("--sandboxes", type=int, default=4, help="并发沙箱数（默认 4）")
    ap.add_argument("--rounds", type=int, default=2, help="每个沙箱的轮数（默认 2）")
    ap.add_argument("--seconds", type=float, default=8.0,
                    help="每轮写脏多久（默认 8 s，create 前后各一段）")
    ap.add_argument("--mem-mb", type=int, default=256, help="内存工作集 MB（默认 256）")
    ap.add_argument("--skip-page-scan", action="store_true",
                    help="跳过页级自校验（整块 md5 已经能判对错，页级扫描是用来定位撕裂的）；"
                         "--no-stop 下会被忽略，那时自校验是唯一判据")
    ap.add_argument("--no-stop", action="store_true",
                    help="不 SIGSTOP 写者，快照拍在写者活动中：期望 md5 不可预知，"
                         "改为只做页级/行级自校验 + 写者存活 + seq 落在快照区间的弱判定")
    return ap


def prepare_box(ctx, box):
    a = ctx.args
    box.run("mkdir -p %s" % common.BENCH_DIR)
    free = box.run("df -m /dev/shm | awk 'NR==2{print $4}'").strip()
    try:
        free_mb = int(free)
    except ValueError:
        free_mb = -1
    if 0 <= free_mb < a.mem_mb + 64:
        raise Failed("/dev/shm 放得下工作集", "≥ %d MB 可用" % (a.mem_mb + 64),
                     "只有 %s MB（沙箱 %s）" % (free, box.id),
                     "T14 前置：工作集在 tmpfs 里；用 --mem-mb 调小")
    box.put(P_WRITER, common.GUEST_MEM_WRITER)
    box.put(P_VERIFY, common.GUEST_MEM_VERIFY)
    box.put(P_FWRITER, common.GUEST_FILE_WRITER)
    box.put(P_FVERIFY, common.GUEST_FILE_VERIFY)
    box.run("rm -f %s %s %s %s %s.ready" % (MEM_FILE, MEM_CTL, MEM_LOG, FS_FILE, MEM_LOG))
    box.setup(warm_mem=16, warm_file=8)      # 心跳进程 + 现场标记，沿用公共口径
    mem_pid = box.bg("python3 %s %s %d %s %s"
                     % (P_WRITER, MEM_FILE, a.mem_mb, MEM_LOG, MEM_CTL), PID_MEM)
    fs_pid = box.bg("python3 %s %s %s" % (P_FWRITER, FS_FILE, FS_MARK), PID_FS)
    # 等内存写者把整块铺满（它铺完写 .ready）。
    rc, _ = box.sh("for i in $(seq 1 120); do [ -f %s.ready ] && exit 0; sleep 1; done; exit 1"
                   % MEM_LOG, timeout=180)
    if rc != 0:
        raise Failed("内存写者起来了", "120 s 内铺满 %d MB 工作集" % a.mem_mb,
                     "没等到 %s.ready（沙箱 %s）" % (MEM_LOG, box.id), "T14 前置")
    return mem_pid, fs_pid


def snap_state(box, mem_pid, fs_pid):
    """两个写者都停着的时候读一次全量状态。"""
    out = box.run(
        "echo mem_md5=$(md5sum %s | cut -d' ' -f1)\n"
        "echo fs_md5=$(md5sum %s | cut -d' ' -f1)\n"
        "echo mem_seq=$(tail -n1 %s 2>/dev/null | awk '{print $1}')\n"
        "echo mem_loghash=$(md5sum %s | cut -d' ' -f1)\n"
        "echo fs_seq=$(cat %s 2>/dev/null)\n"
        "echo mem_state=$(awk '{print $3}' /proc/%s/stat)\n"
        "echo fs_state=$(awk '{print $3}' /proc/%s/stat)\n"
        % (MEM_FILE, FS_FILE, MEM_LOG, MEM_LOG, FS_MARK, mem_pid, fs_pid), timeout=300)
    return common.parse_kv(out)


def stop(box, *pids):
    for p in pids:
        box.sig(p, "STOP")
    time.sleep(0.5)


def cont(box, *pids):
    for p in pids:
        box.sig(p, "CONT")


def run(ctx):
    a = ctx.args
    boxes = common.spawn(ctx, a.sandboxes, "t14-")
    for b in boxes:
        log("  沙箱 %s = %s" % (b.label, b.id))

    pids = {}
    errs = {}

    def prep(b):
        try:
            pids[b.label] = prepare_box(ctx, b)
        except Exception as e:          # noqa: BLE001
            errs[b.label] = e

    ts = [threading.Thread(target=prep, args=(b,)) for b in boxes]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if errs:
        raise list(errs.values())[0]

    bar = threading.Barrier(len(boxes))

    def worker(b):
        mem_pid, fs_pid = pids[b.label]
        no_stop = a.no_stop
        try:
            for r in range(a.rounds):
                time.sleep(a.seconds)                       # 写脏
                if not no_stop:
                    stop(b, mem_pid, fs_pid)
                # 默认模式：写者停着，这就是快照那一刻的精确现场。
                # --no-stop：写者在跑，这只是快照区间的**下界**。
                want = snap_state(b, mem_pid, fs_pid)
                bar.wait(timeout=600)
                rec = ctx.op(b.create("t14-r%d" % r, record_scene=False),
                             stage="T14", round=r, n=len(boxes),
                             dirty_mb=a.mem_mb, want=want, no_stop=no_stop)
                expect(ctx, "%s 第 %d 轮 create" % (b.label, r + 1), rec.get("ok"),
                       "成功", rec.get("err") or "%.3f s" % rec.get("wall_s", 0),
                       "方案 §4.1 T14")
                ck = rec["id"]
                want_hi = None
                if no_stop:
                    want_hi = snap_state(b, mem_pid, fs_pid)   # 快照区间的上界
                else:
                    cont(b, mem_pid, fs_pid)

                time.sleep(a.seconds)                       # 接着写脏，让现场跑开
                pre = snap_state(b, mem_pid, fs_pid) if no_stop else None
                mid = (pre.get("mem_md5") if no_stop
                       else b.run("md5sum %s | cut -d' ' -f1" % MEM_FILE, timeout=300).strip())
                expect(ctx, "%s 第 %d 轮负载真的在改内存" % (b.label, r + 1),
                       mid != want.get("mem_md5"), "create 之后整块 md5 变了",
                       "快照时 %s → 现在 %s" % (want.get("mem_md5"), mid),
                       "T14 前置：现场没跑开的话，回没回去都看不出来")

                bar.wait(timeout=600)
                rr = ctx.op(b.restore(ck, verify=False), stage="T14", round=r, n=len(boxes))
                expect(ctx, "%s 第 %d 轮 restore" % (b.label, r + 1), rr.get("ok"),
                       "成功", rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
                       "方案 §4.1 T14；失败现场 %s" % b.store_dir(ck))

                got = snap_state(b, mem_pid, fs_pid)
                ctx.op({"op": "verify", "stage": "T14", "box": b.label, "sandbox": b.id,
                        "round": r, "id": ck, "no_stop": no_stop, "want": want,
                        "want_hi": want_hi, "pre": pre, "got": got})
                if no_stop:
                    # 弱判定一：写者还是原来那两个进程，而且活着在跑。
                    expect(ctx, "%s 第 %d 轮写者跨 restore 连续" % (b.label, r + 1),
                           got.get("mem_state") in ("R", "S", "D")
                           and got.get("fs_state") in ("R", "S", "D"),
                           "两个写者的 pid 还在、状态 R/S/D",
                           "mem=%s fs=%s（pid %s / %s）"
                           % (got.get("mem_state"), got.get("fs_state"), mem_pid, fs_pid),
                           "pid 还在 = 内存真回来了而不是虚机重启；--no-stop 下写者本来就没被停")
                    # 弱判定二：seq 回滚且落在 [create 前采样, restore 前采样) 区间里。
                    for key, label in (("fs_seq", "文件写者 seq 标记"),
                                       ("mem_seq", "mem.md5 末行 seq")):
                        lo = common.as_int(want.get(key))
                        hi = common.as_int((want_hi or {}).get(key))
                        prev = common.as_int((pre or {}).get(key))
                        cur = common.as_int(got.get(key))
                        ok = (cur is not None and lo is not None and prev is not None
                              and cur >= lo and cur < prev)
                        expect(ctx, "%s 第 %d 轮 %s 回到快照区间" % (b.label, r + 1, label),
                               ok, "≥ %s（create 前）且 < %s（restore 前）" % (lo, prev),
                               "restore 后 %s（create 后上界 %s）" % (cur, hi),
                               "--no-stop：快照拍在写者活动中，只能判区间；"
                               "cur < prev = 真回滚了，cur ≥ lo = 没回到更早的时刻")
                else:
                    for key, label in (("mem_md5", "内存整块 md5"), ("fs_md5", "文件整体 md5"),
                                       ("mem_seq", "mem.md5 末行 seq"),
                                       ("mem_loghash", "mem.md5 文件内容"),
                                       ("fs_seq", "文件写者 seq 标记")):
                        expect(ctx, "%s 第 %d 轮 %s" % (b.label, r + 1, label),
                               got.get(key) == want.get(key), want.get(key), got.get(key),
                               "方案 §4.1 T14：每次 restore 后校验内存 blob 与文件 md5，0 不一致")
                    expect(ctx, "%s 第 %d 轮写者状态回到快照那一刻" % (b.label, r + 1),
                           got.get("mem_state") == "T" and got.get("fs_state") == "T",
                           "两个写者都是 T（快照时被 STOP 住）",
                           "mem=%s fs=%s" % (got.get("mem_state"), got.get("fs_state")),
                           "进程状态也在快照里；pid 还在 = 内存真回来了而不是虚机重启")

                # --no-stop 下自校验是唯一判据，--skip-page-scan 不生效。
                if no_stop or not a.skip_page_scan:
                    if no_stop:
                        stop(b, mem_pid, fs_pid)            # 扫描期间先冻住，免得边扫边改
                    mv = common.parse_kv(b.run("python3 %s %s %s" % (P_VERIFY, MEM_FILE, MEM_CTL),
                                               timeout=900))
                    fv = common.parse_kv(b.run("python3 %s %s" % (P_FVERIFY, FS_FILE),
                                               timeout=900))
                    bad_pages = [x for x in (mv.get("bad_pages") or "").split(",") if x]
                    p_ok, p_tol, p_detail = common.judge_page_scan(
                        mv.get("bad"), bad_pages, mv.get("inprogress"))
                    l_ok, l_tol, l_detail = common.judge_line_scan(
                        fv.get("bad"), fv.get("tail_partial"))
                    ctx.op({"op": "scan", "stage": "T14", "box": b.label, "sandbox": b.id,
                            "round": r, "mem": mv, "file": fv,
                            "page_judge": {"ok": p_ok, "tolerated": p_tol, "detail": p_detail},
                            "line_judge": {"ok": l_ok, "tolerated": l_tol, "detail": l_detail}})
                    if p_tol:
                        note(ctx, "%s 第 %d 轮快照时刻的进行中页" % (b.label, r + 1),
                             "至多 1 页，且必须是控制页记的那一页",
                             p_detail,
                             "写者写页前把页号记进控制页；这一页在快照里本来就是写到一半的，"
                             "restore 忠实还原 —— 不是回滚混了两个时刻")
                    expect(ctx, "%s 第 %d 轮页级自校验" % (b.label, r + 1), p_ok,
                           "坏页 0，或坏页 ⊆ {快照时刻的进行中页}",
                           "%s（共 %s 页，首个坏页 %s，进行中页 %s）"
                           % (p_detail, mv.get("pages"), mv.get("first_bad"),
                              mv.get("inprogress")),
                           "撕裂判据：页头的 页号/版本/crc32 与 body 对不上 = 回滚把两个时刻的"
                           "页混了；但「拍摄时刻正在写的那一页」除外（见 common.judge_page_scan）")
                    if l_tol:
                        note(ctx, "%s 第 %d 轮文件末尾半条" % (b.label, r + 1),
                             "至多 1 条，且只能在文件末尾", l_detail,
                             "append + 短写补齐 ⇒ 半条只可能在末尾")
                    expect(ctx, "%s 第 %d 轮文件行级自校验" % (b.label, r + 1), l_ok,
                           "坏行 0（末尾半条另计，至多 1 条）",
                           "%s / 共 %s 行，末 seq %s"
                           % (l_detail, fv.get("lines"), fv.get("last_seq")),
                           "每行 crc 由 seq 派生且 seq 必须连续（fsync 语义）")

                cont(b, mem_pid, fs_pid)
        except Exception as e:          # noqa: BLE001
            errs[b.label] = e
            bar.abort()                 # 别让同伴在 barrier 上干等 600 s
    ts = [threading.Thread(target=worker, args=(b,)) for b in boxes]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if errs:
        raise list(errs.values())[0]

    for b in boxes:
        ok, d = b.alive()
        expect(ctx, "%s 跑完之后沙箱可用" % b.label, ok, "命令能跑、能写盘、心跳在推进", d,
               "活体判据同 checkpoint_verify.py")

    creates = [o for o in ctx.results["ops"] if o.get("op") == "create"]
    restores = [o for o in ctx.results["ops"] if o.get("op") == "restore"]
    note(ctx, "create 客户端 p50/max", "仅记录",
         "%s / %s s" % (common.fmt_s(common.p50([o.get("wall_s") for o in creates])),
                        common.fmt_s(common.pmax([o.get("wall_s") for o in creates]))),
         "对照并发报告 A 段")
    note(ctx, "restore 客户端 p50/max", "仅记录",
         "%s / %s s" % (common.fmt_s(common.p50([o.get("wall_s") for o in restores])),
                        common.fmt_s(common.pmax([o.get("wall_s") for o in restores]))),
         "对照并发报告 A 段")
    ctx.results["summary"]["T14"] = {
        "sandboxes": len(boxes), "rounds": a.rounds, "mem_mb": a.mem_mb,
        "mode": "no-stop（活动中拍快照，弱判定）" if a.no_stop else "stop（精确回滚）",
        "creates": len(creates), "restores": len(restores),
        "mismatch": sum(1 for x in ctx.results["assertions"] if x["ok"] is False),
    }
