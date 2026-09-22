# -*- coding: utf-8 -*-
"""
T41 干净沙箱的原生 pause / resume（09-21 补的覆盖缺口）。

T39 守的是**做过 checkpoint 之后**的原生 pause（差分只剩最后一段 epoch 的那个
P0）。可"一个从没做过 checkpoint 的普通沙箱，原生 pause → resume 还正不正常"
一直没有自动化守着 —— 而我们动过的每一处都在这条路上：`Vmm::pause_vm`、
uffd 的差分导出、脏页跟踪的开关、rollback 补丁引入的 Faulted 态。这条路一旦坏
了，**受影响的是所有沙箱**，不只是用 checkpoint 的那些，所以它得有自己的回归。

三个场景（互不共用沙箱，各建各的）：

  · **场景 a**：新沙箱 → 铺现场 → 原生 pause → resume。全程**一次 checkpoint
    都不做**，这是本用例与 T39 的唯一区别，也是它的全部意义；
  · **场景 b**：同一个沙箱连着 pause/resume 若干轮（默认 3），每轮之间改一点
    数据。第二次及以后的原生 pause 走的是**增量导出**（上一次 pause 之后
    Firecracker 的位图又清过一次），与第一次不是同一条路；
  · **场景 c**：原生 resume 出来的沙箱再做我们的 checkpoint → 改数据 →
    restore，证明 pause/resume 之后 checkpoint/restore 照常可用（两条路径在
    同一份 uffd 状态上交替走，这是它们最容易互相踩的地方）；
  · **场景 d**：pause **之前就做过 checkpoint** 的那一支 —— checkpoint → 原生
    pause → resume → `list` 为空 → 再 checkpoint → 改数据 → restore 回新的那一个，
    并断言对 pause 之前那个 checkpoint id 的 restore 被明确拒绝。与场景 c 的区别
    只有一个「pause 之前有没有 checkpoint」，但它正是 `deploy/CHECKPOINT.md`
    §6 两行口径的落点：**pause/connect 之后新建 checkpoint 并 restore 完整可用**，
    而**旧 checkpoint 账本不跨 pause**（list 为空、旧 id 报 not found）。一个沙箱
    这一代的 checkpoint 是宿主机本地、与这一代同寿命的状态：resume 起来的是新的
    Firecracker 进程、新的内存与磁盘底座，旧的差分链没有东西可以原地回滚上去。

现场与判据沿用 T39/ T14 的那套，不另起炉灶：

  · `GUEST_MEM_WRITER` 把自校验内存铺进 tmpfs（=guest 内存），每页 16 字节页头
    记 页号/版本/crc32，铺完 `SIGSTOP` 住写者 —— 内容从此不动，整块 md5 可以直接
    跨 pause 比，页级自校验能当场看出"半旧半新"；
  · 标记两份：内存里（`/dev/shm`）与盘上（`BENCH_DIR`，走 NBD 写层）；
  · 盘上另有一份随机 blob，只记 md5 —— 这是需求里"一个文件并记 md5"那一项；
  · 心跳进程（`Box.setup` 起的那个）：pid 不变 + 行数继续涨。**pid 还在但不再涨**
    与**pid 没了**是两回事，前者说明 vCPU 没真跑起来，后者说明整机重来了，所以
    分开报（`common.judge_clean_pause_resume`）；
  · 网络照 T24 的做法：guest 内 loopback 回声长连接（两端都在 guest 里，随整机
    一起冻/化，resume 之后应当照常可用）+ resume 之后立刻新建一条连接。跨出沙箱
    的那一类要 `--external HOST:PORT` 指一个可达对端，不给就跳过并标 skipped ——
    沙箱默认是私有的，出网要看当时的隧道。

每次 pause / resume 的墙钟都记进 `ops`（`op=pause` / `op=resume` 的 `wall_s`）与
summary，**只记录不判定**：这条路的耗时随内存大小和脏页量走，没有稳定阈值可判。
快照大小同理 —— SDK 的 `beta_pause()` 返回 `None`，拿不到大小，所以 JSON 里没有
这一项，而不是记了个 0。
"""

NAME = "T41"

import time

from .. import common
from ..common import Failed, expect, log, note
from .t39 import pause_resume

MEM_FILE = "/dev/shm/t41_mem"
MEM_CTL = "/dev/shm/t41_ctl"
MEM_LOG = "/tmp/t41_mem.md5"
P_WRITER = "/dev/shm/t41_mem_writer.py"
P_VERIFY = "/dev/shm/t41_mem_verify.py"
PID_MEM = "/dev/shm/t41_mem.pid"

MEM_MARK = "/dev/shm/t41_mark"                     # tmpfs = guest 内存
FILE_MARK = common.BENCH_DIR + "/t41_mark"         # 根文件系统 = NBD 写层
FILE_BLOB = common.BENCH_DIR + "/t41_blob"         # 只记 md5 的那份文件

PEER = "/dev/shm/t41_peer.py"
PEER_STATE = "/dev/shm/t41_peer.state"
PEER_PID = "/dev/shm/t41_peer.pid"
NEWCONN_PATH = "/dev/shm/t41_new.py"
DEFAULT_PORT = 45141

# 新建连接的探针，与 T24 的那份一字不差（连上、发一行、收回声、报耗时）。
NEWCONN = r'''
import socket, sys, time

host, port = sys.argv[1], int(sys.argv[2])
t = time.monotonic()
s = socket.create_connection((host, port), timeout=5)
s.settimeout(5)
s.sendall(b'hello\n')
got = s.recv(64)
print('connect_ms=%.0f' % ((time.monotonic() - t) * 1000))
print('echo=%s' % (got.strip().decode('ascii', 'replace') or 'EMPTY'))
'''

BASIS = ("原生 pause/resume 是所有沙箱都走的路（不只是用 checkpoint 的那些）；"
         "T39 只覆盖了做过 checkpoint 之后的那一支，这里补的是没做过的那一支")


def add_args(ap):
    ap.add_argument("--scene", default="all", choices=["a", "b", "c", "d", "all"],
                    help="跑哪个场景（a=单次 pause/resume，b=连续多轮，"
                         "c=resume 之后再 checkpoint/restore，"
                         "d=pause 之前就有 checkpoint：账本不跨 pause、新一代照常；"
                         "默认四个都跑）")
    ap.add_argument("--mem-mb", type=int, default=64,
                    help="常驻进程持有的自校验内存 MB（默认 64）")
    ap.add_argument("--file-mb", type=int, default=4,
                    help="盘上那份只记 md5 的 blob 的 MB（默认 4）")
    ap.add_argument("--rounds", type=int, default=3,
                    help="场景 b 连续 pause/resume 的轮数（默认 3）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="guest 内回声服务的端口（默认 %d；被占了就换）" % DEFAULT_PORT)
    ap.add_argument("--external", default="",
                    help="可达的对端 HOST:PORT（resume 之后新建**出网**连接那一项）；"
                         "不给就只测回环并标 skipped")
    ap.add_argument("--connect-attempts", type=int, default=10,
                    help="pause 之后 Sandbox.connect() 最多试几次（默认 10）")
    ap.add_argument("--connect-gap", type=float, default=5.0,
                    help="connect 重试间隔秒数（默认 5）")
    ap.add_argument("--connect-timeout", type=float, default=180.0,
                    help="单次 connect 的超时秒数（默认 180）")
    return ap


# ---------------------------------------------------------------- 现场

def prepare_box(ctx, box):
    """铺自校验内存 + 心跳 + 盘上 blob，铺完 SIGSTOP 住写者。返回 (写者 pid, blob md5)。"""
    a = ctx.args
    box.run("mkdir -p %s" % common.BENCH_DIR)
    box.put(P_WRITER, common.GUEST_MEM_WRITER)
    box.put(P_VERIFY, common.GUEST_MEM_VERIFY)
    box.run("rm -f %s %s %s %s.ready %s %s %s"
            % (MEM_FILE, MEM_CTL, MEM_LOG, MEM_LOG, MEM_MARK, FILE_MARK, FILE_BLOB))
    box.setup(warm_mem=16, warm_file=8)             # 心跳 + 公共现场口径
    box.run("dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null; sync"
            % (FILE_BLOB, a.file_mb), timeout=900)
    pid = box.bg("python3 %s %s %d %s %s" % (P_WRITER, MEM_FILE, a.mem_mb, MEM_LOG, MEM_CTL),
                 PID_MEM)
    rc, _ = box.sh("for i in $(seq 1 180); do [ -f %s.ready ] && exit 0; sleep 1; done; exit 1"
                   % MEM_LOG, timeout=240)
    if rc != 0:
        raise Failed("常驻内存进程起来了", "180 s 内铺满 %d MB 自校验内存" % a.mem_mb,
                     "没等到 %s.ready（沙箱 %s）" % (MEM_LOG, box.id), "T41 前置")
    box.sig(pid, "STOP")
    time.sleep(0.5)
    st = box.pid_state(pid)
    if st != "T":
        raise Failed("写者停住了", "进程状态 T", "状态 = %r（沙箱 %s）" % (st, box.id),
                     "T41 前置：内容要在整段测试里不动，md5 才能跨 pause 比")
    return pid


def start_net(ctx, box):
    """guest 内的回声服务 + 一条长连接（两端都在 guest 里）。"""
    a = ctx.args
    box.put(PEER, common.GUEST_NET_PEER)
    box.put(NEWCONN_PATH, NEWCONN)
    peer_pid = box.bg("python3 %s %d %s" % (PEER, a.port, PEER_STATE), PEER_PID)
    rc, _ = box.sh("for i in $(seq 1 15); do [ -s %s ] && exit 0; sleep 1; done; exit 1"
                   % PEER_STATE, timeout=90)
    if rc != 0:
        raise Failed("loopback 回声服务起来了", "15 s 内写出 %s" % PEER_STATE,
                     "没等到 —— 端口 %d 被占了？用 --port 换一个（guest 里 pid %s）"
                     % (a.port, peer_pid), "T41 前置")
    st = common.parse_kv(box.run("cat %s 2>/dev/null | tr ' ' '\n'" % PEER_STATE, timeout=60))
    expect(ctx, "前置：loopback 长连接在跑", st.get("ok") == "1", "ok=1", st, "T41 前置")
    return peer_pid


def mark(box, text):
    """两份标记：内存里一份（tmpfs），盘上一份（写层）。"""
    box.run("echo %s >> %s; echo %s >> %s; sync" % (text, MEM_MARK, text, FILE_MARK))


def state(box, pid):
    """一次读齐判定要的全部现场（心跳取前后两拍，好看出它还在不在涨）。"""
    d = box.kv(box.run(
        "echo mem_md5=$(md5sum %s | cut -d' ' -f1)\n"
        "echo file_md5=$(md5sum %s | cut -d' ' -f1)\n"
        "echo mem_mark=$(tr '\\n' ',' < %s 2>/dev/null || echo MISSING)\n"
        "echo file_mark=$(tr '\\n' ',' < %s 2>/dev/null || echo MISSING)\n"
        "echo writer_pid=$(cat %s 2>/dev/null || echo MISSING)\n"
        "echo writer_state=$(awk '{print $3}' /proc/%s/stat 2>/dev/null || echo GONE)\n"
        "echo hb_pid=$(cat %s 2>/dev/null || echo MISSING)\n"
        "echo hb0=$(wc -l < %s 2>/dev/null || echo 0)\n"
        "sleep 1.5\n"
        "echo hb1=$(wc -l < %s 2>/dev/null || echo 0)\n"
        "echo cmd=$(echo alive)\n"
        % (MEM_FILE, FILE_BLOB, MEM_MARK, FILE_MARK, PID_MEM, pid,
           common.HB_PID, common.HB_LOG, common.HB_LOG), timeout=300))
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


def check_net(ctx, box, stage, first_round=True):
    """resume 之后的网络：旧的回环连接还在，新连接立刻建得起来。"""
    a = ctx.args
    time.sleep(2)                       # 让 guest 里的持有者跑几拍
    st = common.parse_kv(box.run("cat %s 2>/dev/null | tr ' ' '\n'" % PEER_STATE, timeout=60))
    expect(ctx, "%s：resume 之后 loopback 旧连接仍可用" % stage, st.get("ok") == "1",
           "ok=1（两端都在 guest 内，随整机一起冻/化）", st,
           "T24 的同一条口径：连接两端一起被搬回去，这一类不该断")

    nk = common.parse_kv(box.run("python3 %s 127.0.0.1 %d" % (NEWCONN_PATH, a.port), timeout=60))
    expect(ctx, "%s：resume 之后新建回环连接" % stage, nk.get("echo") == "hello",
           "echo=hello", nk, "T24 的同一条口径：新连接立即可用")

    ext = None
    if a.external:
        host, port = a.external.rsplit(":", 1)
        ext = common.parse_kv(box.run("python3 %s %s %s" % (NEWCONN_PATH, host, port), timeout=60))
        expect(ctx, "%s：resume 之后新建出网连接" % stage, bool(ext.get("connect_ms")),
               "连得上 %s" % a.external, ext,
               "--external 指的对端得可达；不给这个参数时这一项跳过")
    elif first_round:
        note(ctx, "%s：出网连接" % stage, "给 --external HOST:PORT 才测",
             "skipped —— 沙箱默认私有（allow_public_traffic=False），出网要看当时的隧道",
             "T24 的同一条理由")

    ctx.op({"op": "net", "stage": stage, "box": box.label, "sandbox": box.id,
            "loopback": st, "newconn": nk, "external": ext})
    return st, nk, ext


def check_resume(ctx, box, stage, pid, before, want_marks, prec, rrec, last_err, net=True):
    """pause/resume 之后的全部判定。"""
    expect(ctx, "%s：pause 返回成功" % stage, prec.get("ok"), "beta_pause() 不抛",
           prec.get("err") or "ok", BASIS)
    expect(ctx, "%s：resume（Sandbox.connect）成功" % stage, rrec.get("ok"),
           "%d 次以内连上" % ctx.args.connect_attempts,
           "试了 %s 次；最后一个错误 %s" % (rrec.get("attempts"),
                                           ("%s: %s" % (type(last_err).__name__, last_err))[:300]
                                           if last_err else "无"),
           BASIS)
    if not rrec.get("ok"):
        raise Failed("%s：resume" % stage, "连上", "连不上，后面的现场判定没得做", BASIS)

    after = state(box, pid)
    ctx.op({"op": "state", "stage": stage, "when": "after-resume", "box": box.label,
            "sandbox": box.id, "before": before, "after": after, "want_marks": want_marks})
    ok, bad = common.judge_clean_pause_resume(before, after, want_marks)
    expect(ctx, "%s：resume 之后现场逐项对得上" % stage, ok,
           "内存 md5 / 文件 md5 不变、写者与心跳 pid 不变、心跳还在涨、标记 %s"
           % (want_marks or "与 pause 前一致"),
           "；".join(bad) if bad else "逐项一致（内存 md5 %s，文件 md5 %s，写者 %s 状态 %s，"
                                      "心跳 %s %s→%s，标记 %s / %s）"
           % (after.get("mem_md5"), after.get("file_md5"), after.get("writer_pid"),
              after.get("writer_state"), after.get("hb_pid"), after.get("hb0"),
              after.get("hb1"), after.get("mem_mark"), after.get("file_mark")),
           BASIS)

    p_ok, p_tol, p_detail, mv = page_scan(ctx, box, stage, "after-resume")
    if p_tol:
        note(ctx, "%s：快照时刻的进行中页" % stage, "至多 1 页且必须是控制页记的那一页",
             p_detail, "见 common.judge_page_scan")
    expect(ctx, "%s：resume 之后页级自校验" % stage, p_ok,
           "坏页 0（或 ⊆ {进行中页}）",
           "%s（共 %s 页，首个坏页 %s）" % (p_detail, mv.get("pages"), mv.get("first_bad")),
           "撕裂判据：页头的 页号/版本/crc32 与 body 对不上 = 内存来自两个时刻")

    alive_ok, d = box.alive()
    expect(ctx, "%s：resume 之后 guest 还在干活" % stage, alive_ok,
           "命令能跑、盘能读写、心跳进程还在 tick",
           "cmd=%s rw=%s hb=%s→%s" % (d.get("cmd"), d.get("rw"), d.get("hb0"), d.get("hb1")),
           "Box.alive() 的公共口径")
    if net:
        check_net(ctx, box, stage)
    return after


def walls(ctx, stage):
    """这一段里每次 pause / resume 的墙钟（只记录）。"""
    out = {}
    for op in ("pause", "resume"):
        out[op + "_wall_s"] = [round(o.get("wall_s") or 0.0, 3) for o in ctx.results["ops"]
                               if o.get("op") == op and o.get("stage") == stage]
    return out


# ---------------------------------------------------------------- 场景

def scene_a(ctx):
    """新沙箱 → 铺现场 → 原生 pause → resume。全程不做 checkpoint。"""
    box = common.spawn(ctx, 1, "t41a-")[0]
    log("  场景 a 沙箱 %s（全程不做 checkpoint）" % box.id)
    pid = prepare_box(ctx, box)
    start_net(ctx, box)
    mark(box, "M0")

    before = state(box, pid)
    ctx.op({"op": "state", "stage": "T41a", "when": "before-pause", "box": box.label,
            "sandbox": box.id, "state": before})
    prec, rrec, err = pause_resume(ctx, box, "T41a")
    check_resume(ctx, box, "场景 a", pid, before, None, prec, rrec, err)

    ids = box.list_ids()
    expect(ctx, "场景 a：这个沙箱确实一个 checkpoint 都没有", ids == [],
           "list 为空（本用例的前提，写成断言免得哪天用例被改坏了自己不知道）",
           ids, BASIS)
    note(ctx, "场景 a：pause / resume 墙钟", "只记录不判定", walls(ctx, "T41a"),
         "SDK 的 beta_pause() 返回 None，拿不到快照大小")
    ctx.results["summary"]["T41"]["a"] = dict({"sandbox": box.id, "before": before},
                                              **walls(ctx, "T41a"))
    return box


def scene_b(ctx):
    """同一个沙箱连着 pause/resume 若干轮，每轮之间改一点数据。"""
    a = ctx.args
    box = common.spawn(ctx, 1, "t41b-")[0]
    log("  场景 b 沙箱 %s（%d 轮）" % (box.id, a.rounds))
    pid = prepare_box(ctx, box)
    start_net(ctx, box)
    mark(box, "M0")

    rows = []
    for r in range(a.rounds):
        log("\n  --- 第 %d/%d 轮 ---" % (r + 1, a.rounds))
        # 每轮之间改一点数据：两份标记各加一行，盘上的 blob 重写一遍。自校验内存
        # 不动（写者停着），它要证明的是"没被改的东西一页都不能变"。
        mark(box, "R%d" % r)
        box.run("dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc 2>/dev/null; sync"
                % (FILE_BLOB, a.file_mb), timeout=900)

        before = state(box, pid)
        ctx.op({"op": "state", "stage": "T41b", "when": "before-pause", "round": r,
                "box": box.label, "sandbox": box.id, "state": before})
        prec, rrec, err = pause_resume(ctx, box, "T41b")
        after = check_resume(ctx, box, "场景 b 第 %d 轮" % (r + 1), pid, before, None,
                             prec, rrec, err, net=(r == a.rounds - 1))
        rows.append({"round": r, "file_md5": after.get("file_md5"),
                     "mem_mark": after.get("mem_mark"),
                     "pause_wall_s": round(prec.get("wall_s") or 0.0, 3),
                     "resume_wall_s": round(rrec.get("wall_s") or 0.0, 3)})

    expect(ctx, "场景 b：跑完之后两份标记攒齐了每一轮",
           (rows[-1]["mem_mark"] or "") == "M0," + "".join("R%d," % r for r in range(a.rounds)),
           "M0,R0,…,R%d," % (a.rounds - 1), rows[-1]["mem_mark"],
           "每轮的改动都要活过后面每一次 pause —— 漏一轮就是漏一段增量")
    note(ctx, "场景 b：每轮 pause / resume 墙钟", "只记录不判定", rows, BASIS)
    ctx.results["summary"]["T41"]["b"] = {"sandbox": box.id, "rounds": a.rounds, "per_round": rows}
    return box


def scene_c(ctx):
    """原生 resume 出来的沙箱再走我们的 checkpoint → 改数据 → restore。"""
    box = common.spawn(ctx, 1, "t41c-")[0]
    log("  场景 c 沙箱 %s" % box.id)
    pid = prepare_box(ctx, box)
    start_net(ctx, box)
    mark(box, "M0")

    before = state(box, pid)
    ctx.op({"op": "state", "stage": "T41c", "when": "before-pause", "box": box.label,
            "sandbox": box.id, "state": before})
    prec, rrec, err = pause_resume(ctx, box, "T41c")
    resumed = check_resume(ctx, box, "场景 c", pid, before, None, prec, rrec, err)

    # 到这里沙箱是"原生 resume 出来的"。checkpoint/restore 从这一刻起照常。
    box.dirty("g0", mem_mb=16, file_mb=4)
    mark(box, "C0")
    at_ckpt = state(box, pid)
    crec = ctx.op(box.create("g0"), stage="T41c", step="create")
    expect(ctx, "场景 c：resume 之后建得出 checkpoint", crec.get("ok"), "成功",
           crec.get("err") or "ok", BASIS)
    ck = crec["id"]

    mark(box, "C1")
    box.dirty("g1", mem_mb=16, file_mb=4)
    box.run("dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc 2>/dev/null; sync"
            % (FILE_BLOB, ctx.args.file_mb), timeout=900)

    rr = ctx.op(box.restore(ck), stage="T41c", step="restore")
    expect(ctx, "场景 c：restore 回 checkpoint 那一刻",
           rr.get("ok") and rr.get("verified"), "成功且公共现场逐项回到 create 时",
           rr.get("err") or ("现场不一致 %s" % rr.get("mismatch") if rr.get("mismatch")
                             else "ok"),
           "Box.restore(verify=True) 的公共口径")

    back = state(box, pid)
    ctx.op({"op": "state", "stage": "T41c", "when": "after-restore", "box": box.label,
            "sandbox": box.id, "at_ckpt": at_ckpt, "after": back})
    ok, bad = common.judge_clean_pause_resume(at_ckpt, back)
    # 心跳行数是要倒退的（restore 把它也回滚了），所以这里只认"pid 没变"那一条，
    # 把"还在涨"交给下面的 alive()。判定里已经分成两项，正好各取所需。
    bad = [b for b in bad if "心跳行数" not in b]
    expect(ctx, "场景 c：restore 之后现场回到 checkpoint 那一刻", not bad,
           "内存/文件 md5 与标记都是 create 那一刻的（C0 在、C1 不在）",
           "；".join(bad) if bad else "逐项一致（内存标记 %s，盘上标记 %s，文件 md5 %s）"
           % (back.get("mem_mark"), back.get("file_mark"), back.get("file_md5")),
           "checkpoint/restore 的既定语义；这里验的是它在原生 resume 之后照样成立")
    expect(ctx, "场景 c：C1 确实没了", "C1" not in (back.get("mem_mark") or ""),
           "内存标记里没有 C1", back.get("mem_mark"),
           "C1 是 create 之后写的，回滚到 create 那一刻就该没了")

    p_ok, _, p_detail, mv = page_scan(ctx, box, "场景 c", "after-restore")
    expect(ctx, "场景 c：restore 之后页级自校验", p_ok, "坏页 0（或 ⊆ {进行中页}）",
           "%s（共 %s 页）" % (p_detail, mv.get("pages")), "同上")

    alive_ok, d = box.alive()
    expect(ctx, "场景 c：restore 之后沙箱可用", alive_ok,
           "命令能跑、盘能读写、心跳在推进", d, "Box.alive() 的公共口径")
    ctx.results["summary"]["T41"]["c"] = dict(
        {"sandbox": box.id, "checkpoint": ck, "resumed": resumed,
         "restore_wall_s": round(rr.get("wall_s") or 0.0, 3)}, **walls(ctx, "T41c"))
    return box


def judge_stale_restore(ok, info):
    """判「restore 到 pause 之前的那个 checkpoint」有没有被明确拒绝。纯函数。

    服务端这一条走的是 `store.Get` 查不到 → 404 `not_found`，文案
    `checkpoint <id> not found`（service.go 的 restore；`deploy/CHECKPOINT.md`
    §6 写的也是这一句）。**按 code + 文案判，不按 reason 判**：这条错误的 reason
    与 code 同名，今天的 SDK 没有 reason 字段时退化猜不出它来（`_REASON_BY_CODE`
    里只有 data_loss / failed_precondition 两条），拿 reason 当判据会把一条本来
    正确的拒绝判成失败。reason 与它的出处只作记录项。

    返回 `(ok, got)`。
    """
    code = (info or {}).get("code")
    msg = ((info or {}).get("message") or "").lower()
    got = "ok=%s code=%s reason=%s（出处 %s）：%s" % (
        ok, code, (info or {}).get("reason"), (info or {}).get("reason_src"),
        ((info or {}).get("message") or "")[:200])
    if ok:
        return False, got
    return (code == "not_found" and "not found" in msg), got


def scene_d(ctx):
    """pause 之前就做过 checkpoint：账本不跨 pause，新一代照常 checkpoint/restore。"""
    a = ctx.args
    box = common.spawn(ctx, 1, "t41d-")[0]
    log("  场景 d 沙箱 %s" % box.id)
    pid = prepare_box(ctx, box)
    start_net(ctx, box)
    mark(box, "M0")

    # ---- 第一代：pause 之前先攒一个 checkpoint，它必须随 pause 一起作废
    box.dirty("d0", mem_mb=16, file_mb=4)
    mark(box, "D0")
    crec0 = ctx.op(box.create("d0"), stage="T41d", step="create-before-pause")
    expect(ctx, "场景 d：pause 之前建得出 checkpoint", crec0.get("ok"), "成功",
           crec0.get("err") or "ok", BASIS)
    if not crec0.get("ok"):
        raise Failed("场景 d：pause 之前的 checkpoint", "建得出",
                     crec0.get("err") or "失败", "后面整段都建立在它之上")
    old_ck = crec0["id"]

    before = state(box, pid)
    ctx.op({"op": "state", "stage": "T41d", "when": "before-pause", "box": box.label,
            "sandbox": box.id, "state": before, "checkpoint": old_ck})
    prec, rrec, err = pause_resume(ctx, box, "T41d")
    check_resume(ctx, box, "场景 d", pid, before, None, prec, rrec, err)

    # ---- 账本不跨 pause：list 为空，旧 id 明确被拒
    lrec = ctx.op(box.list_rec(), stage="T41d", step="list-after-resume")
    expect(ctx, "场景 d：resume 之后 list 为空",
           lrec.get("ok") and lrec.get("ids") == [], "[]",
           lrec.get("err") or lrec.get("ids"),
           "旧 checkpoint 账本不跨 pause（deploy/CHECKPOINT.md §6）：resume 起来的是"
           "新的 Firecracker 进程，旧的差分链没有东西可以原地回滚上去")

    rr_old = ctx.op(box.restore(old_ck, verify=False), stage="T41d", step="restore-before-pause")
    old_info = common.rec_error_info(rr_old)
    ok_old, got_old = judge_stale_restore(rr_old.get("ok"), old_info)
    expect(ctx, "场景 d：restore 到 pause 之前的 checkpoint 被明确拒绝", ok_old,
           "失败，code=not_found，文案 `checkpoint <id> not found`", got_old,
           "同上；拒绝要给得出说法，不能是超时或 500")
    note(ctx, "场景 d：这条拒绝的 reason 出处", "记录项（今天的 SDK 未必有 reason 字段）",
         "reason=%s 出处 %s" % (old_info.get("reason"), old_info.get("reason_src")),
         "与 T21 的 sdk_has_reason_field 同一口径")

    # ---- 新一代：照常 checkpoint → 改数据 → restore
    mark(box, "D1")
    box.dirty("d1", mem_mb=16, file_mb=4)
    at_ckpt = state(box, pid)
    crec = ctx.op(box.create("d1"), stage="T41d", step="create-after-resume")
    expect(ctx, "场景 d：resume 之后建得出新的 checkpoint", crec.get("ok"), "成功",
           crec.get("err") or "ok",
           "这一条就是 09-21 抓到的那个缺陷：原生 pause 会按沙箱 id 立一块永久墓碑，"
           "resume 复用同一个 id，于是新一代被上一代的墓碑永久挡在门外")
    if not crec.get("ok"):
        raise Failed("场景 d：resume 之后的 checkpoint", "建得出",
                     crec.get("err") or "失败", "后面的 restore 没得做")
    ck = crec["id"]

    ids = box.list_ids()
    expect(ctx, "场景 d：list 里只有新一代的那一个", ids == [ck], "[%s]" % ck, ids,
           "上一代的条目不能漏给新一代，新一代自己的必须看得见")

    mark(box, "D2")
    box.dirty("d2", mem_mb=16, file_mb=4)
    box.run("dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc 2>/dev/null; sync"
            % (FILE_BLOB, a.file_mb), timeout=900)

    rr = ctx.op(box.restore(ck), stage="T41d", step="restore-after-resume")
    expect(ctx, "场景 d：restore 回新一代的 checkpoint",
           rr.get("ok") and rr.get("verified"), "成功且公共现场逐项回到 create 时",
           rr.get("err") or ("现场不一致 %s" % rr.get("mismatch") if rr.get("mismatch")
                             else "ok"),
           "Box.restore(verify=True) 的公共口径")

    back = state(box, pid)
    ctx.op({"op": "state", "stage": "T41d", "when": "after-restore", "box": box.label,
            "sandbox": box.id, "at_ckpt": at_ckpt, "after": back})
    ok, bad = common.judge_clean_pause_resume(at_ckpt, back)
    # 同场景 c：心跳行数是要倒退的（restore 把它也回滚了），只认 pid 那一条。
    bad = [b for b in bad if "心跳行数" not in b]
    expect(ctx, "场景 d：restore 之后现场回到新 checkpoint 那一刻", not bad,
           "内存/文件 md5 与标记都是 create 那一刻的（D1 在、D2 不在）",
           "；".join(bad) if bad else "逐项一致（内存标记 %s，盘上标记 %s，文件 md5 %s）"
           % (back.get("mem_mark"), back.get("file_mark"), back.get("file_md5")),
           "checkpoint/restore 的既定语义；这里验的是它对「原生 resume 出来的新一代」成立")
    expect(ctx, "场景 d：D2 没了、D0/D1 还在",
           "D2" not in (back.get("mem_mark") or "")
           and "D1" in (back.get("mem_mark") or "")
           and "D0" in (back.get("mem_mark") or ""),
           "标记里有 D0、D1，没有 D2", back.get("mem_mark"),
           "D0 是 pause 之前写的（pause/resume 不丢数据，只丢 checkpoint 账本），"
           "D1 是新 checkpoint 之前写的，D2 是它之后写的")

    p_ok, _, p_detail, mv = page_scan(ctx, box, "场景 d", "after-restore")
    expect(ctx, "场景 d：restore 之后页级自校验", p_ok, "坏页 0（或 ⊆ {进行中页}）",
           "%s（共 %s 页）" % (p_detail, mv.get("pages")), "同场景 c")

    alive_ok, d = box.alive()
    expect(ctx, "场景 d：restore 之后沙箱可用", alive_ok,
           "命令能跑、盘能读写、心跳在推进", d, "Box.alive() 的公共口径")

    ctx.results["summary"]["T41"]["d"] = dict(
        {"sandbox": box.id, "checkpoint_before_pause": old_ck,
         "list_after_resume": lrec.get("ids"),
         "stale_restore": {"ok": rr_old.get("ok"), "code": old_info.get("code"),
                           "reason": old_info.get("reason"),
                           "reason_src": old_info.get("reason_src")},
         "checkpoint_after_resume": ck,
         "restore_wall_s": round(rr.get("wall_s") or 0.0, 3)}, **walls(ctx, "T41d"))
    return box


SCENE_FN = {"a": scene_a, "b": scene_b, "c": scene_c, "d": scene_d}


def run(ctx):
    a = ctx.args
    scenes = list("abcd") if a.scene == "all" else [a.scene]
    ctx.results["summary"].setdefault("T41", {})
    ctx.stage("T41", scenes=scenes, mem_mb=a.mem_mb, file_mb=a.file_mb, rounds=a.rounds,
              external=a.external or "skipped")
    for s in scenes:
        log("")
        log("== 场景 %s ==" % s)
        SCENE_FN[s](ctx)
