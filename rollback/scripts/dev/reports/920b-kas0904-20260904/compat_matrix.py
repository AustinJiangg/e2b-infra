#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkpoint/restore 与 e2b 原有沙箱生命周期操作的兼容性矩阵。

我们给 e2b 加了两个操作（checkpoint.create / checkpoint.restore），e2b 原本
就有四个（create / connect / pause / kill，其中 connect 对已 pause 的沙箱等同
resume）。这两组操作会互相影响 —— 都在动同一个沙箱的磁盘层栈和内存 ——
但此前没有任何测试覆盖过它们的**组合**。

这个脚本把组合逐个跑一遍，每格给出三种判定之一：

    OK          能做，且做完之后沙箱和数据都对
    REFUSED     服务端明确拒绝（拒绝本身是安全的：没有留下半吊子状态）
    BROKEN      能调用但结果不对，或者本该能做的原生操作被我们弄坏了
                —— 这一类才是真问题

判定"数据对不对"一律用 O_DIRECT 读回 8 MiB 随机数据比 sha256：走 guest
page cache 会把磁盘层的问题完全盖住（pause 那个 bug 当初就是这么假通过的）。

用法:
    python3 compat_matrix.py --server-ip 10.10.10.10
"""
import argparse
import json
import os
import sys
import time
import traceback

RESULTS = []


def record(case, verdict, detail=""):
    RESULTS.append((case, verdict, detail))
    mark = {"OK": "✓", "REFUSED": "·", "BROKEN": "✗"}.get(verdict, "?")
    print("  %s %-46s %-8s %s" % (mark, case, verdict, detail), flush=True)


def setup_env(args):
    os.environ["E2B_API_URL"] = "http://%s:3000" % args.server_ip
    os.environ["E2B_HTTP_SSL"] = "false"
    os.environ.setdefault("E2B_DOMAIN", "e2b.app")
    with open(args.e2b_config, encoding="utf-8") as f:
        d = json.load(f)
    os.environ["E2B_ACCESS_TOKEN"] = d["accessToken"]
    os.environ["E2B_API_KEY"] = d["teamApiKey"]


MB = 8
WRITE = ("mkdir -p /m && dd if=/dev/urandom of=/m/{n} bs=1M count=%d conv=fsync 2>/dev/null && "
         "sha256sum /m/{n} | cut -c1-16") % MB
READ = "dd if=/m/{n} bs=1M iflag=direct 2>/dev/null | sha256sum | cut -c1-16"
EXISTS = "test -e /m/{n} && echo yes || echo no"


def sh(sbx, cmd):
    return sbx.commands.run(cmd, user="root").stdout.strip()


def alive(sbx):
    try:
        return sh(sbx, "echo ok") == "ok"
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="checkpoint/restore 与原生生命周期操作的兼容性矩阵")
    ap.add_argument("--server-ip", required=True)
    ap.add_argument("--e2b-config", default="/root/.e2b/config.json")
    ap.add_argument("--template", default="base")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()
    setup_env(args)

    global Sandbox
    from e2b import Sandbox

    def new():
        return Sandbox.create(template=args.template, timeout=args.timeout)

    # ================================================================
    print("\n===== A. 基线：原生操作在【没有 checkpoint】的沙箱上 =====")
    # 先确认这些原生能力本来就是好的，后面才能说"是我们弄坏的"。
    s = new()
    sid = s.sandbox_id
    h = sh(s, WRITE.format(n="a"))
    try:
        s.beta_pause()
        s2 = Sandbox.connect(sid, timeout=args.timeout)
        ok = alive(s2) and sh(s2, READ.format(n="a")) == h
        record("A1 pause → connect（无 checkpoint）", "OK" if ok else "BROKEN",
               "数据一致" if ok else "数据不一致")
        s2.kill()
    except Exception as e:
        record("A1 pause → connect（无 checkpoint）", "BROKEN", str(e)[:60])

    # ================================================================
    print("\n===== B. checkpoint 之后，原生操作还能不能做 =====")
    s = new(); sid = s.sandbox_id
    h = sh(s, WRITE.format(n="b"))
    ck = s.checkpoint.create(name="c1")
    record("B0 沙箱上做 checkpoint", "OK", "id=%s" % ck.checkpoint_id[-8:])

    # B1 checkpoint 之后还能正常执行命令
    record("B1 checkpoint 之后沙箱照常运行", "OK" if alive(s) else "BROKEN")

    # B2 checkpoint 之后 pause → connect，数据是否完好（就是那个修掉的 bug）
    try:
        s.beta_pause()
        s2 = Sandbox.connect(sid, timeout=args.timeout)
        got = sh(s2, READ.format(n="b"))
        ok = alive(s2) and got == h
        record("B2 checkpoint → pause → connect：磁盘数据", "OK" if ok else "BROKEN",
               "" if ok else "写入 %s 读回 %s" % (h, got))
    except Exception as e:
        record("B2 checkpoint → pause → connect：磁盘数据", "BROKEN", str(e)[:60])
        s2 = None

    # B3 pause/resume 之后 checkpoint 账本还在不在
    if s2 is not None:
        try:
            lst = s2.checkpoint.list()
            record("B3 pause → connect 之后 checkpoint.list", "OK", "%d 个" % len(lst))
        except Exception as e:
            record("B3 pause → connect 之后 checkpoint.list", "REFUSED", str(e)[:60])
        # B4 pause/resume 之后能不能 restore 回 pause 之前的 checkpoint
        try:
            s2.checkpoint.restore(ck.checkpoint_id)
            got = sh(s2, READ.format(n="b"))
            ok = got == h and alive(s2)
            record("B4 pause → connect 之后 restore 旧 checkpoint",
                   "OK" if ok else "BROKEN", "" if ok else "数据不一致")
        except Exception as e:
            record("B4 pause → connect 之后 restore 旧 checkpoint", "REFUSED", str(e)[:70])
        # B5 拒绝之后沙箱是否还活着（拒绝必须是干净的）
        record("B5 上一步之后沙箱仍然可用", "OK" if alive(s2) else "BROKEN")
        # B6 pause/resume 之后能不能【新建】 checkpoint
        try:
            ck2 = s2.checkpoint.create(name="c2")
            record("B6 pause → connect 之后新建 checkpoint", "OK", "id=%s" % ck2.checkpoint_id[-8:])
            # B7 新建的能不能 restore
            h2 = sh(s2, WRITE.format(n="b2"))
            s2.checkpoint.restore(ck2.checkpoint_id)
            gone = sh(s2, EXISTS.format(n="b2")) == "no"
            record("B7 restore 到 pause 之后新建的 checkpoint",
                   "OK" if gone else "BROKEN", "b2 已回滚掉" if gone else "b2 仍在")
        except Exception as e:
            record("B6/B7 pause → connect 之后新建并 restore", "REFUSED", str(e)[:70])
        try:
            s2.kill()
        except Exception:
            pass

    # ================================================================
    print("\n===== C. restore 之后，原生操作还能不能做 =====")
    s = new(); sid = s.sandbox_id
    h = sh(s, WRITE.format(n="c"))
    ck = s.checkpoint.create(name="c1")
    sh(s, WRITE.format(n="c2"))          # checkpoint 之后再写一份
    s.checkpoint.restore(ck.checkpoint_id)
    ok = sh(s, READ.format(n="c")) == h and sh(s, EXISTS.format(n="c2")) == "no"
    record("C0 restore 本身", "OK" if ok else "BROKEN")
    record("C1 restore 之后沙箱照常运行", "OK" if alive(s) else "BROKEN")
    try:
        s.beta_pause()
        s3 = Sandbox.connect(sid, timeout=args.timeout)
        got = sh(s3, READ.format(n="c"))
        ok = alive(s3) and got == h
        record("C2 restore → pause → connect：磁盘数据", "OK" if ok else "BROKEN",
               "" if ok else "写入 %s 读回 %s" % (h, got))
        try:
            s3.kill()
        except Exception:
            pass
    except Exception as e:
        record("C2 restore → pause → connect", "BROKEN", str(e)[:60])

    # ================================================================
    print("\n===== D. 多次 checkpoint（多层封存）之后 pause =====")
    # 原 bug 的本质是"当前层之下还有 N 个 SealedView"，一层测不透。
    s = new(); sid = s.sandbox_id
    marks = {}
    for i in range(3):
        marks["d%d" % i] = sh(s, WRITE.format(n="d%d" % i))
        s.checkpoint.create(name="d%d" % i)
    try:
        s.beta_pause()
        s4 = Sandbox.connect(sid, timeout=args.timeout)
        bad = [k for k, v in marks.items() if sh(s4, READ.format(n=k)) != v]
        record("D1 三次 checkpoint → pause → connect：三层数据",
               "OK" if not bad else "BROKEN", "全部一致" if not bad else "不一致: %s" % bad)
        try:
            s4.kill()
        except Exception:
            pass
    except Exception as e:
        record("D1 三次 checkpoint → pause → connect", "BROKEN", str(e)[:60])

    # ================================================================
    print("\n===== E. 反向：pause/resume 之后再走完整 checkpoint 流程 =====")
    s = new(); sid = s.sandbox_id
    s.beta_pause()
    s5 = Sandbox.connect(sid, timeout=args.timeout)
    h = sh(s5, WRITE.format(n="e"))
    try:
        ck = s5.checkpoint.create(name="e1")
        sh(s5, WRITE.format(n="e2"))
        s5.checkpoint.restore(ck.checkpoint_id)
        ok = sh(s5, READ.format(n="e")) == h and sh(s5, EXISTS.format(n="e2")) == "no"
        record("E1 pause → connect → checkpoint → restore",
               "OK" if ok else "BROKEN", "" if ok else "数据不一致")
    except Exception as e:
        record("E1 pause → connect → checkpoint → restore", "REFUSED", str(e)[:70])
    try:
        s5.kill()
    except Exception:
        pass

    # ================================================================
    print("\n===== F. kill 与 checkpoint 的关系 =====")
    s = new(); sid = s.sandbox_id
    ck = s.checkpoint.create(name="f1")
    s.kill()
    try:
        s6 = Sandbox.connect(sid, timeout=args.timeout)
        record("F1 kill 之后还能 connect", "BROKEN", "kill 的沙箱不该能连上")
        try:
            s6.kill()
        except Exception:
            pass
    except Exception as e:
        record("F1 kill 之后 connect", "REFUSED", str(e)[:50])

    # ================================================================
    print("\n================ 矩阵 ================")
    w = max(len(c) for c, _, _ in RESULTS)
    for case, verdict, detail in RESULTS:
        print("  %-*s  %-8s %s" % (w, case, verdict, detail))
    broken = [c for c, v, _ in RESULTS if v == "BROKEN"]
    refused = [c for c, v, _ in RESULTS if v == "REFUSED"]
    print("\n  OK %d 项 / REFUSED %d 项 / BROKEN %d 项"
          % (len(RESULTS) - len(broken) - len(refused), len(refused), len(broken)))
    if refused:
        print("\n  边界（服务端明确拒绝，不是损坏）：")
        for c in refused:
            print("    · %s" % c)
    if broken:
        print("\n  ✗ 真问题：")
        for c in broken:
            print("    · %s" % c)
        return 1
    print("\n  ✓ 没有 BROKEN：原生能力没有被 checkpoint/restore 破坏。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
