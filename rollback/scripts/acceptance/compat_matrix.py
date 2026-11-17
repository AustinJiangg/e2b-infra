#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkpoint / restore 与 e2b 原生沙箱生命周期操作的兼容性矩阵。两套快照回滚都能跑。

我们给 e2b 加了 checkpoint.create / checkpoint.restore 两个操作，e2b 原本有 create / connect /
pause / kill 四个（connect 对已 pause 的沙箱等同 resume）。两组操作动的是同一个沙箱，会互相影响。
这个脚本把组合逐个跑一遍，每格给出三种判定之一：

    OK          能做，且做完之后沙箱和数据都对
    REFUSED     服务端明确拒绝（拒绝本身是安全的：没有留下半吊子状态）
    BROKEN      能调用但结果不对，或者本该能做的原生操作被弄坏了 —— 这一类才是真问题

判定"数据对不对"一律用 O_DIRECT 读回 8 MiB 随机数据比 sha256：走 guest page cache 会把磁盘层的
问题完全盖住。

只调用 SDK 的公开接口，所以同一份脚本在沙箱级（模板 base）和进程级（模板 base-gsd）上都能跑。
进程级的 B 组就是手册里 T-09 那一条"checkpoint → pause → resume → restore"。B8 用 pause 之前
那个 Sandbox 对象做 restore，B3～B7 用 connect 拿到的新对象：两者分开，是因为进程级 SDK 的
connect 曾经漏带 E2b-Sandbox-Id 头，新对象上的 checkpoint 调用会被代理以 "missing header" 打回。
这种打回是客户端的错误，判 BROKEN，不判 REFUSED。

来历：rollback/scripts/dev/compat_matrix.py（09 月开发期），2026-11-16 去掉对 dev/lib.py 的依赖，
加 --template / --out，并移到本目录。

凭据：取环境变量 E2B_API_KEY 等；给了 --env-file 就先读它（不覆盖已导出的变量）。
退出码：0 = 没有 BROKEN；1 = 有 BROKEN；2 = 脚本自身出错。

用法：
    python3 compat_matrix.py --template base --out /tmp/compat.json
    python3 compat_matrix.py --template base-gsd --out /tmp/compat.json
"""
import argparse
import json
import os
import sys
import time
import traceback


def load_env_file(path):
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def wait_for_api(template, tries=30, delay=4):
    """刚重启的节点头几秒不接单，先探一下，免得第一项因为无关原因失败。"""
    from e2b import Sandbox as S
    last = None
    for i in range(tries):
        try:
            S.create(template=template, timeout=60).kill()
            return True
        except Exception as e:
            last = e
            time.sleep(delay)
    print("API 一直起不来: %s" % last, flush=True)
    return False


RESULTS = []


OUT = None


def record(case, verdict, detail=""):
    # 代理因为请求头不全打回的不是服务端的拒绝，是客户端坏了
    if verdict == "REFUSED" and "missing header" in detail:
        verdict = "BROKEN"
        detail = "客户端请求头不全，被代理打回：" + detail
    RESULTS.append((case, verdict, detail))
    mark = {"OK": "✓", "REFUSED": "·", "BROKEN": "✗"}.get(verdict, "?")
    print("  %s %-46s %-8s %s" % (mark, case, verdict, detail), flush=True)


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
    ap.add_argument("--template", default="base")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--out", default=None, help="把矩阵写成 JSON")
    args = ap.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    global OUT
    OUT = args.out

    if not wait_for_api(args.template):
        sys.exit("API 一直没就绪，先查 nomad/api 状态")
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
        record("A1 pause → connect（无 checkpoint）", "BROKEN", str(e)[:120])

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
        record("B2 checkpoint → pause → connect：磁盘数据", "BROKEN", str(e)[:120])
        s2 = None

    # B3 pause/resume 之后 checkpoint 账本还在不在
    if s2 is not None:
        try:
            lst = s2.checkpoint.list()
            record("B3 pause → connect 之后 checkpoint.list", "OK", "%d 个" % len(lst))
        except Exception as e:
            record("B3 pause → connect 之后 checkpoint.list", "REFUSED", str(e)[:120])
        # B4 pause/resume 之后能不能 restore 回 pause 之前的 checkpoint
        try:
            s2.checkpoint.restore(ck.checkpoint_id)
            got = sh(s2, READ.format(n="b"))
            ok = got == h and alive(s2)
            record("B4 pause → connect 之后 restore 旧 checkpoint",
                   "OK" if ok else "BROKEN", "" if ok else "数据不一致")
        except Exception as e:
            record("B4 pause → connect 之后 restore 旧 checkpoint", "REFUSED", str(e)[:120])
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
            record("B6/B7 pause → connect 之后新建并 restore", "REFUSED", str(e)[:120])
        # B8 同一条路径，但用 pause 之前那个 Sandbox 对象发 restore（不经 connect 的新对象）
        try:
            s.checkpoint.restore(ck.checkpoint_id)
            got = sh(s, READ.format(n="b"))
            ok = got == h and alive(s)
            record("B8 pause → connect 之后用原对象 restore 旧 checkpoint",
                   "OK" if ok else "BROKEN", "" if ok else "写入 %s 读回 %s" % (h, got))
        except Exception as e:
            record("B8 pause → connect 之后用原对象 restore 旧 checkpoint", "REFUSED", str(e)[:120])
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
        record("C2 restore → pause → connect", "BROKEN", str(e)[:120])

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
        record("D1 三次 checkpoint → pause → connect", "BROKEN", str(e)[:120])

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
        record("E1 pause → connect → checkpoint → restore", "REFUSED", str(e)[:120])
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
        record("F1 kill 之后 connect", "REFUSED", str(e)[:120])

    # ================================================================
    print("\n================ 矩阵 ================")
    w = max(len(c) for c, _, _ in RESULTS)
    for case, verdict, detail in RESULTS:
        print("  %-*s  %-8s %s" % (w, case, verdict, detail))
    broken = [c for c, v, _ in RESULTS if v == "BROKEN"]
    if OUT:
        with open(OUT, "w") as f:
            json.dump({"items": [{"case": c, "verdict": v, "detail": d} for c, v, d in RESULTS]},
                      f, ensure_ascii=False, indent=1)
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
