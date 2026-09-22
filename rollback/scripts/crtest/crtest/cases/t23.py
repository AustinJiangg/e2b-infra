# -*- coding: utf-8 -*-
"""
T23 文件系统边界（方案 §4.2）。

四件事一起做，一次 restore 全部验：

  a) **写到一半时 create**：一个流式写者一直往文件里追加 64 KB 的确定性块（第 k 块
     全是 k 派生的字节，不 fsync），create 就发生在它写着的时候。restore 之后文件
     必须是这条确定性流的一个**前缀**（每块内容对得上、没有撕块、没有多出来的块），
     块数 ≤ create 返回那一刻观察到的块数。
  b) **checkpoint 时打开着的 fd**：一个写者快照前就把 fd 开着，每 0.2 s 写一行序号。
     restore 之后它回到快照那一刻的状态，应当**接着那时的序号**往下写：行号必须
     从 1 连续到末尾，不许断号也不许重号。
  c) **目录 rename**：create 前有 dir_a，create 后把它 mv 成 dir_b；restore 之后
     dir_a 必须在、dir_b 必须不在。
  d) **fsync 语义**：create 前写好并 sync 的文件，create 后删掉；restore 之后
     文件必须回来且内容一致。

（c）（d）是"删除和改名能不能回滚"——它们能回滚才说明是块级快照而不是补文件，
口径同 checkpoint_verify.py 的第三件事。
"""

NAME = "T23"

import time

from .. import common
from ..common import expect, log, note

D = common.BENCH_DIR
STREAM = D + "/t23_stream.bin"
STREAM_MARK = "/dev/shm/t23_stream.k"
FDLOG = D + "/t23_fd.log"
FD_MARK = "/dev/shm/t23_fd.seq"
P_SW = "/dev/shm/t23_stream_writer.py"
P_SV = "/dev/shm/t23_stream_verify.py"
P_FW = "/dev/shm/t23_fd_writer.py"
PID_SW = "/dev/shm/t23_sw.pid"
PID_FW = "/dev/shm/t23_fw.pid"
SYNCED = D + "/t23_synced.txt"
SYNCED_TEXT = "t23-fsync-payload"


def add_args(ap):
    ap.add_argument("--rounds", type=int, default=2, help="轮数（默认 2）")
    ap.add_argument("--run-seconds", type=float, default=5.0,
                    help="create 之后让写者再跑多久（默认 5 s）")
    ap.add_argument("--tol-blocks", type=int, default=100,
                    help="(a) 块数判定的容差：restore 返回到把写者停住之间它还能再写几块"
                         "（默认 100，约 1 s）")
    return ap


def fd_check(box):
    """行号必须 1..N 连续。"""
    out = box.run("awk 'NR != $1 {bad=1} END {print \"lines=\" NR; print \"bad=\" (bad?1:0); "
                  "print \"last=\" $1}' %s" % FDLOG, timeout=120)
    return common.parse_kv(out)


def run(ctx):
    a = ctx.args
    box = common.spawn(ctx, 1, "t23-")[0]
    log("  沙箱 %s" % box.id)
    box.run("mkdir -p %s" % D)
    box.setup(warm_mem=32, warm_file=8)
    box.put(P_SW, common.GUEST_STREAM_WRITER)
    box.put(P_SV, common.GUEST_STREAM_VERIFY)
    box.put(P_FW, common.GUEST_FD_WRITER)

    for r in range(a.rounds):
        log("\n  --- 第 %d/%d 轮 ---" % (r + 1, a.rounds))
        box.run("rm -rf %s %s %s %s/dir_a %s/dir_b; mkdir -p %s/dir_a; "
                "echo a > %s/dir_a/inside; printf '%s' > %s; sync"
                % (STREAM, FDLOG, SYNCED, D, D, D, D, SYNCED_TEXT, SYNCED), timeout=300)
        sw = box.bg("python3 %s %s %s" % (P_SW, STREAM, STREAM_MARK), PID_SW)
        fw = box.bg("python3 %s %s %s" % (P_FW, FDLOG, FD_MARK), PID_FW)
        time.sleep(3)

        crec = ctx.op(box.create("t23-r%d" % r, record_scene=False), stage="T23", round=r)
        expect(ctx, "第 %d 轮 create（流式写正写着）" % (r + 1), crec.get("ok"), "成功",
               crec.get("err") or "%.3f s" % crec.get("wall_s", 0), "方案 §4.2 T23")
        ck = crec["id"]
        marks = common.parse_kv(box.run("echo k=$(cat %s); echo seq=$(cat %s)"
                                        % (STREAM_MARK, FD_MARK)))
        k_after_create = int(marks.get("k") or 0)

        # create 之后把现场改到面目全非：接着流式写、删掉 synced、把目录改名。
        time.sleep(a.run_seconds)
        box.run("mv %s/dir_a %s/dir_b; rm -f %s; sync" % (D, D, SYNCED), timeout=300)
        after = common.parse_kv(box.run(
            "echo k=$(cat %s); echo dir_a=$(test -d %s/dir_a && echo yes || echo no); "
            "echo dir_b=$(test -d %s/dir_b && echo yes || echo no); "
            "echo synced=$(test -f %s && echo yes || echo no)" % (STREAM_MARK, D, D, SYNCED)))
        expect(ctx, "第 %d 轮现场确实被改掉了" % (r + 1),
               after.get("dir_b") == "yes" and after.get("synced") == "no"
               and int(after.get("k") or 0) > k_after_create,
               "dir_b 在、synced 没了、流式写继续涨", after,
               "T23 前置：现场没跑开就验不出回滚")

        rr = ctx.op(box.restore(ck, verify=False), stage="T23", round=r)
        expect(ctx, "第 %d 轮 restore" % (r + 1), rr.get("ok"), "成功",
               rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
               "T23 前置；失败现场 %s" % box.store_dir(ck))

        # 流式写者在 restore 之后回到"正在写"的状态，还会接着涨；先把它停住再量，
        # 否则量的是一个动着的文件。停住之前的那几十毫秒允许它多写几块（--tol-blocks）。
        box.sh("kill -STOP %s" % sw)
        sv = common.parse_kv(box.run("python3 %s %s" % (P_SV, STREAM), timeout=600))
        blocks = int(sv.get("blocks") or -1)
        ctx.op({"op": "fs", "stage": "T23", "round": r, "box": box.label, "sandbox": box.id,
                "stream": sv, "k_after_create": k_after_create, "after": after})
        expect(ctx, "第 %d 轮 (a) 流式文件是确定性流的前缀" % (r + 1), sv.get("bad") == "0",
               "坏块 0", "坏块 %s / 共 %s 块（末块 %s 字节）"
               % (sv.get("bad"), sv.get("blocks"), sv.get("tail")),
               "方案 §4.2 T23：restore 后文件应恰是 checkpoint 那一刻，不许撕块")
        expect(ctx, "第 %d 轮 (a) 块数退回快照那一刻" % (r + 1),
               0 <= blocks <= k_after_create + a.tol_blocks
               and blocks < int(after.get("k") or 0),
               "≤ %d 块（create 返回时是 %d 块，容差 %d）且明显小于 restore 前的 %s 块"
               % (k_after_create + a.tol_blocks, k_after_create, a.tol_blocks, after.get("k")),
               "%s 块" % blocks,
               "方案 §4.2 T23：多出来的块 = 回滚没把写层退回去")

        fdk = fd_check(box)
        expect(ctx, "第 %d 轮 (b) open fd 上的写连续" % (r + 1), fdk.get("bad") == "0",
               "行号 1..N 连续无断号", fdk,
               "方案 §4.2 T23：快照时打开的 fd，restore 后接着那一刻往下写")
        time.sleep(1.5)
        fdk2 = fd_check(box)
        expect(ctx, "第 %d 轮 (b) restore 后 fd 还能继续写" % (r + 1),
               fdk2.get("bad") == "0" and int(fdk2.get("last") or 0) > int(fdk.get("last") or 0),
               "行号继续增长且仍连续", "%s → %s" % (fdk.get("last"), fdk2.get("last")),
               "方案 §4.2 T23：open fd 上的后续写")

        dirs = common.parse_kv(box.run(
            "echo dir_a=$(test -d %s/dir_a && echo yes || echo no); "
            "echo dir_b=$(test -d %s/dir_b && echo yes || echo no); "
            "echo inside=$(cat %s/dir_a/inside 2>/dev/null || echo MISSING); "
            "echo synced=$(cat %s 2>/dev/null || echo MISSING)" % (D, D, D, SYNCED)))
        expect(ctx, "第 %d 轮 (c) 目录 rename 被回滚" % (r + 1),
               dirs.get("dir_a") == "yes" and dirs.get("dir_b") == "no"
               and dirs.get("inside") == "a",
               "dir_a 在（里面的文件也在）、dir_b 不在", dirs,
               "方案 §4.2 T23：目录 rename")
        expect(ctx, "第 %d 轮 (d) fsync 过的文件删了又回来" % (r + 1),
               dirs.get("synced") == SYNCED_TEXT, SYNCED_TEXT, dirs.get("synced"),
               "方案 §4.2 T23：fsync 语义；删除能回滚才是块级快照")

        box.sh("kill -CONT %s; kill -9 %s %s" % (sw, sw, fw))
        time.sleep(0.5)

    ok, d = box.alive()
    expect(ctx, "跑完之后沙箱可用", ok, "命令能跑、能写盘、心跳在推进", d,
           "活体判据同 checkpoint_verify.py")
    note(ctx, "create 客户端耗时（流式写正在进行）", "只记录",
         "%s s" % common.fmt_s(common.p50([o.get("wall_s") for o in ctx.results["ops"]
                                           if o.get("op") == "create" and o.get("ok")])),
         "对照 checkpoint_bench_v2.py")
    ctx.results["summary"]["T23"] = {"rounds": a.rounds}
