# -*- coding: utf-8 -*-
"""
T24 网络状态（方案 §4.2）。

checkpoint 时 guest 里有活着的 TCP 连接，restore 之后会怎样？两类连接的语义**不同**，
这里分开判：

  · **两端都在 guest 内**的连接（loopback 回声：guest 里一个 server + 一条长连接）
    —— 整机回滚把连接两端一起搬回去了，所以 restore 之后它应当**照常可用**。
  · **跨出沙箱**的连接（对宿主 / 对外）—— 宿主机侧的 conntrack 表项在 restore 时被
    清掉（服务端 `conntrack` 那一段就是干这个的），对端也没有回滚，所以旧连接应当
    **干净地失效**（立刻报错或读到 EOF），不许挂死。这一类要靠 `--external
    HOST:PORT` 指一个可达的对端；不给就跳过并在输出里标 skipped ——
    920B 上的沙箱默认是私有的（allow_public_traffic=False），出网要看当时的隧道。
  · 新连接 restore 之后**立刻**可用。
  · 宿主机 conntrack 计数 restore 前后各记一次（只记录，不判定）。
"""

NAME = "T24"

import time

from .. import common
from ..common import expect, log, note

PEER = "/dev/shm/t24_peer.py"
PEER_STATE = "/dev/shm/t24_peer.state"
PEER_PID = "/dev/shm/t24_peer.pid"
EXT = "/dev/shm/t24_ext.py"
EXT_STATE = "/dev/shm/t24_ext.state"
EXT_PID = "/dev/shm/t24_ext.pid"
DEFAULT_PORT = 45123

# 对外长连接持有者：连上去之后每 0.2 s 发一个字节并把结果写进状态文件。
# restore 之后期望"立刻报错"，而不是一直阻塞 —— 所以 socket 设了 5 s 超时。
EXT_HOLDER = r'''
import socket, sys, time

host, port, state = sys.argv[1], int(sys.argv[2]), sys.argv[3]
s = socket.create_connection((host, port), timeout=5)
s.settimeout(5)
n = 0
while True:
    n += 1
    try:
        s.sendall(b'p')
        b = s.recv(16)
        r = 'ok' if b else 'eof'
    except OSError as e:
        r = 'err:%s' % type(e).__name__
    open(state, 'w').write('n=%d r=%s\n' % (n, r))
    time.sleep(0.2)
'''

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
NEWCONN_PATH = "/dev/shm/t24_new.py"


def add_args(ap):
    ap.add_argument("--rounds", type=int, default=3, help="restore 轮数（默认 3）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="guest 内回声服务的端口（默认 %d；被占了就换）" % DEFAULT_PORT)
    ap.add_argument("--external", default="",
                    help="可达的对端 HOST:PORT（跨出沙箱的那一类连接）；不给就跳过这一类")
    return ap


def state(box, path):
    """读一个 `k=v k=v` 的状态文件；文件还没出来时返回空字典而不是报错。"""
    return common.parse_kv(box.run("cat %s 2>/dev/null | tr ' ' '\n'" % path, timeout=60))


def wait_for(box, path, what, hint, seconds=15):
    rc, _ = box.sh("for i in $(seq 1 %d); do [ -s %s ] && exit 0; sleep 1; done; exit 1"
                   % (int(seconds), path), timeout=seconds + 60)
    if rc != 0:
        raise common.Failed(what, "%d s 内写出 %s" % (seconds, path),
                            "没等到 —— %s" % hint, "T24 前置")


def run(ctx):
    a = ctx.args
    box = common.spawn(ctx, 1, "t24-")[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)
    box.put(PEER, common.GUEST_NET_PEER)
    box.put(NEWCONN_PATH, NEWCONN)
    peer_pid = box.bg("python3 %s %d %s" % (PEER, a.port, PEER_STATE), PEER_PID)
    wait_for(box, PEER_STATE, "loopback 回声服务起来了",
             "端口 %d 被占了？用 --port 换一个（guest 里 pid %s）" % (a.port, peer_pid))

    ext_host = ext_port = None
    if a.external:
        ext_host, ext_port = a.external.rsplit(":", 1)
        box.put(EXT, EXT_HOLDER)
        box.bg("python3 %s %s %s %s" % (EXT, ext_host, ext_port, EXT_STATE), EXT_PID)
        wait_for(box, EXT_STATE, "对外长连接持有者起来了",
                 "连不上 %s:%s？" % (ext_host, ext_port))
        st = state(box, EXT_STATE)
        expect(ctx, "对外长连接建起来了", st.get("r") == "ok", "r=ok", st,
               "T24 前置：--external 指的对端得可达")

    s0 = state(box, PEER_STATE)
    expect(ctx, "loopback 长连接在跑", s0.get("ok") == "1", "ok=1", s0, "T24 前置")

    box.dirty("g0", mem_mb=16, file_mb=4)
    crec = ctx.op(box.create("g0"), stage="T24", step="cp0")
    expect(ctx, "建 checkpoint（此刻有活连接）", crec.get("ok"), "成功",
           crec.get("err") or "ok", "方案 §4.2 T24")
    cp0 = crec["id"]

    for r in range(a.rounds):
        log("\n  --- 第 %d/%d 轮 ---" % (r + 1, a.rounds))
        ct_before = common.conntrack_count()
        rr = ctx.op(box.restore(cp0, verify=False), stage="T24", round=r)
        expect(ctx, "第 %d 轮 restore" % (r + 1), rr.get("ok"), "成功",
               rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
               "T24 前置；失败现场 %s" % box.store_dir(cp0))
        ct_after = common.conntrack_count()

        time.sleep(2)               # 让 guest 里的两个持有者各跑几拍
        s1 = state(box, PEER_STATE)
        expect(ctx, "第 %d 轮 loopback 旧连接仍可用" % (r + 1), s1.get("ok") == "1",
               "ok=1（两端都在 guest 内，随整机一起回滚）", s1,
               "方案 §4.2 T24：连接两端一起被搬回去，所以这一类不该断")

        nk = common.parse_kv(box.run("python3 %s 127.0.0.1 %d" % (NEWCONN_PATH, a.port),
                                     timeout=60))
        expect(ctx, "第 %d 轮 restore 后新连接立刻可用" % (r + 1),
               nk.get("echo") == "hello", "echo=hello", nk,
               "方案 §4.2 T24：新连接立即可用")

        ext = None
        if ext_host:
            ext = state(box, EXT_STATE)
            expect(ctx, "第 %d 轮对外旧连接干净失效" % (r + 1),
                   (ext.get("r") or "").startswith("err") or ext.get("r") == "eof",
                   "r=err:* 或 eof（立刻失效，不挂死）", ext,
                   "方案 §4.2 T24：宿主机 conntrack 被清、对端没回滚")
            nk2 = common.parse_kv(box.run("python3 %s %s %s" % (NEWCONN_PATH, ext_host, ext_port),
                                          timeout=60))
            note(ctx, "第 %d 轮对外新连接" % (r + 1), "只记录", nk2, "方案 §4.2 T24")

        ctx.op({"op": "net", "stage": "T24", "box": box.label, "sandbox": box.id, "round": r,
                "loopback": s1, "newconn": nk, "external": ext,
                "conntrack_before": ct_before, "conntrack_after": ct_after,
                "conntrack_phase_ms": (rr.get("phases") or {}).get("conntrack")})
        note(ctx, "第 %d 轮宿主机 conntrack 计数" % (r + 1), "只记录",
             "%s → %s（服务端 conntrack 段 %s ms）"
             % (ct_before, ct_after, common.fmt_ms((rr.get("phases") or {}).get("conntrack"))),
             "并发报告 §6.2：conntrack 段偶发尖峰")

    ok, d = box.alive()
    expect(ctx, "跑完之后沙箱可用", ok, "命令能跑、能写盘、心跳在推进", d,
           "活体判据同 checkpoint_verify.py")
    ctx.results["summary"]["T24"] = {
        "rounds": a.rounds, "external": a.external or "skipped",
        "conntrack": [(o.get("conntrack_before"), o.get("conntrack_after"))
                      for o in ctx.results["ops"] if o.get("op") == "net"],
    }
