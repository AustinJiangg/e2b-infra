#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkpoint 之后 pause / resume，根文件系统必须原样回来。

抓的是这个 bug（infra-arm ecdad325c / KASandbox_0904 内含）：

    checkpoint 会把沙箱的写层封存、另开一层，所以从第一次 checkpoint 起，
    当前层只装着那之后的写，更早的写在下面的 SealedView 里。而 Pause 用来
    构建沙箱快照的 ExportDiff 只取了当前层 —— 于是**做过 checkpoint 的沙箱
    一 pause 就丢掉上次 checkpoint 之前的所有写**，恢复后更是几乎全空。
    没有任何东西会报错：diff header 是从导出内容推出来的，自己跟自己自洽。

为什么必须用 O_DIRECT：
    小文件走 guest page cache 读回来是对的，能把这个 bug 完全盖住 —— 当初第一次
    验就是这么假通过的。这里写 16 MiB 随机数据，pause/resume 之后用 O_DIRECT
    绕开缓存直接读盘，比 sha256。

用法（先按 README 的「完整流程」设好 E2B_API_URL / E2B_API_KEY）:
    python3 pause_verify.py
    python3 pause_verify.py --mb 64
"""
import argparse
import sys
import time

import lib

FAILED = []


def check(ok, label):
    print("    [%s] %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILED.append(label)


# 写 N MiB 随机数据，再用 O_DIRECT 读回来算 sha256。
# O_DIRECT 要求缓冲区和长度按块对齐，所以用 dd 的 iflag=direct 而不是自己读。
WRITE = (
    "mkdir -p /pausetest && "
    "dd if=/dev/urandom of=/pausetest/{name} bs=1M count={mb} conv=fsync 2>/dev/null && "
    "sha256sum /pausetest/{name} | cut -c1-16"
)
READ_DIRECT = (
    "dd if=/pausetest/{name} bs=1M iflag=direct 2>/dev/null | sha256sum | cut -c1-16"
)


def sh(sbx, cmd):
    return sbx.commands.run(cmd, user="root").stdout.strip()


def main():
    ap = argparse.ArgumentParser(description="checkpoint + pause/resume 的根文件系统一致性")
    ap.add_argument("--template", default="base")
    ap.add_argument("--mb", type=int, default=16, help="每份随机数据的大小 MB（默认 16）")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    if not lib.wait_for_api():
        sys.exit("API 一直没就绪，先查 nomad/api 状态")
    from e2b import Sandbox

    print("模板 = %s   每份随机数据 = %d MB" % (args.template, args.mb))
    sbx = Sandbox.create(template=args.template, timeout=args.timeout)
    sid = sbx.sandbox_id
    print("沙箱 %s   store: %s (%s)   dirty tracking: %s"
          % (sid, lib.STORE, lib.store_fstype(), lib.dirty_tracking(sid)))

    # ---- 1. checkpoint 之前写一份，作为对照 ----------------------------
    print("\n===== 1. checkpoint 之前写 before.bin =====")
    before = sh(sbx, WRITE.format(name="before.bin", mb=args.mb))
    print("  before.bin sha=%s" % before)

    # ---- 2. 做一次 checkpoint：写层被封存，之后的写落到新层 --------------
    print("\n===== 2. checkpoint（写层封存、另开一层）=====")
    t = time.time()
    ck = sbx.checkpoint.create(name="ck1")
    print("  checkpoint id=%s  用时 %.3fs" % (getattr(ck, "checkpoint_id", "?"), time.time() - t))

    # ---- 3. checkpoint 之后再写一份，落在新层 ---------------------------
    print("\n===== 3. checkpoint 之后写 after.bin =====")
    after = sh(sbx, WRITE.format(name="after.bin", mb=args.mb))
    print("  after.bin sha=%s" % after)

    # ---- 4. pause / resume ---------------------------------------------
    print("\n===== 4. pause / resume =====")
    t = time.time()
    sbx.beta_pause()
    print("  pause 用时 %.3fs" % (time.time() - t))
    t = time.time()
    sbx = Sandbox.connect(sid, timeout=args.timeout)
    print("  resume 用时 %.3fs" % (time.time() - t))
    check(sh(sbx, "echo alive") == "alive", "resume 之后沙箱还能执行命令")

    # ---- 5. 用 O_DIRECT 读回来比 --------------------------------------
    print("\n===== 5. O_DIRECT 读回（绕开 guest page cache）=====")
    b2 = sh(sbx, READ_DIRECT.format(name="before.bin"))
    a2 = sh(sbx, READ_DIRECT.format(name="after.bin"))
    print("  before.bin  写入 %s  读回 %s" % (before, b2))
    print("  after.bin   写入 %s  读回 %s" % (after, a2))
    check(b2 == before, "**checkpoint 之前**写的 16MB 在 pause/resume 后完好（就是这条抓 bug）")
    check(a2 == after, "checkpoint 之后写的 16MB 在 pause/resume 后完好")

    # ---- 6. 边界：pause/resume 之后回不到 pause 之前的 checkpoint ---------
    # 这是**已知且已记录的边界**，不是故障：pause 把当时的层栈整体压进沙箱快照，
    # resume 起来的是新实例，旧 checkpoint 账本不跨 pause。这里把它钉住 ——
    # 哪天行为变了（无论是变得支持、还是变成静默返回错数据），这条会立刻告诉我们。
    print("\n===== 6. 边界：restore 到 pause 之前的 checkpoint =====")
    try:
        sbx.checkpoint.restore(ck.checkpoint_id)
        check(False, "预期被拒绝，实际成功了 —— 边界变了，去核实是不是真的支持了")
    except Exception as e:
        refused = "not found" in str(e).lower()
        check(refused, "restore 到 pause 之前的 checkpoint 被拒绝（已知边界）")
        if not refused:
            print("      实际错误: %s" % str(e)[:120])
    check(sh(sbx, "echo ok") == "ok", "被拒绝之后沙箱仍然可用")

    # 完整的组合矩阵（哪些能做、哪些是边界、有没有弄坏原生能力）在 compat_matrix.py。

    print("\n================ 汇总 ================")
    try:
        sbx.kill()
        print("沙箱已删除")
    except Exception as e:
        print("删除沙箱失败（不影响判定）：%s" % e)
    if FAILED:
        print("✗ %d 项失败：" % len(FAILED))
        for f in FAILED:
            print("    - %s" % f)
        return 1
    print("✓ 全部通过：checkpoint 之后 pause/resume，根文件系统两层的内容都原样回来了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
