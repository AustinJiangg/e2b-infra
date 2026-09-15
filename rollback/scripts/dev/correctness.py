#!/usr/bin/env python3
"""正确性 e2e，两套通用：线性链、前滚、分叉跨支、删除语义、失败语义。

每次恢复都用三重证据判定回到了目标时刻：tmpfs 标记（guest 内存）、磁盘标记、
以及一个数百 MB blob 的 md5（大块内存，不只是一页）。

用法: correctness.py <xfs|ext4> [df挂载点]
"""
import os
import sys

import lib

SCHEME = sys.argv[1] if len(sys.argv) > 1 else "ext4"
MOUNT = sys.argv[2] if len(sys.argv) > 2 else "/"
if SCHEME not in lib.SCHEME_MEMFILE:
    sys.exit("第一个参数必须是 xfs 或 ext4")
MEMFILE = lib.SCHEME_MEMFILE[SCHEME]

check = lib.Checker()
b = lib.Box()


def rst(name, note):
    dt, good = b.restore(name, note)
    check(good, "restore %s：内存/磁盘/blob 三项都回到 %s 时刻" % (name, name))


def rst_refused(name, note):
    before = b.markers()
    err = None
    try:
        ok = b.sbx.checkpoint.restore(b.cks[name])
        err = None if ok else "returned false"
    except Exception as e:
        err = str(e)
    after = b.markers()
    print("RESTORE %-5s refused=%s err=%r" % (name, err is not None, str(err)[:150]), flush=True)
    check(err is not None, "restore %s 被拒绝（%s）" % (name, note))
    check(after == before, "拒绝没有动沙箱状态：%s == %s" % (after, before))


try:
    print("\n=== 1. 线性链 ===", flush=True)
    b.checkpoint("ck1", 0, MOUNT)
    b.checkpoint("ck2", 64, MOUNT)
    b.checkpoint("ck3", 256, MOUNT)
    b.checkpoint("ck4", 512, MOUNT)
    b.checkpoint("ck5", 0, MOUNT)

    root_mode = (b.manifest("ck1") or {}).get("mem_mode")
    if SCHEME == "xfs" or lib.FULL_ROOT:
        check(root_mode == "full", "树根 ck1 是全量捕获（mem_mode=%s）" % root_mode)
    else:
        check(root_mode == "incremental", "CHECKPOINT_FULL_ROOT=false，树根是差分（mem_mode=%s）" % root_mode)
    check(all((b.manifest(n) or {}).get("mem_mode") == "incremental" for n in ("ck2", "ck3", "ck4", "ck5")),
          "ck2..ck5 都是增量")

    print("\n=== 2. 逐级回退 + 跨链前滚 ===", flush=True)
    rst("ck5", "base=ck5，回滚集只有活跃脏页")
    rst("ck4", "路径 (ck4..ck5]")
    rst("ck3", "路径 (ck3..ck4]")
    rst("ck2", "路径 (ck2..ck3]")
    rst("ck1", "路径 (ck1..ck2]")
    rst("ck5", "前滚 (ck1..ck5]")
    rst("ck1", "反向")

    print("\n=== 3. 分叉：回到过去再建检查点，跨分支恢复 ===", flush=True)
    b.checkpoint("ck6", 32, MOUNT)  # parent = ck1 → ck2..ck5 成旁支
    check((b.manifest("ck6") or {}).get("parent_id") == b.cks["ck1"], "ck6 的父节点是 ck1")
    rst("ck3", "跨分支：ck6→ck1 ∪ ck3→ck2→ck1")
    rst("ck6", "跨回新支")
    rst("ck5", "跨到旁支最远端")
    b.checkpoint("ck7", 8, MOUNT)  # parent = ck5
    check((b.manifest("ck7") or {}).get("parent_id") == b.cks["ck5"], "ck7 的父节点是 ck5")
    rst("ck6", "路径经 LCA=ck1")
    rst("ck2", "路径经 LCA=ck1")
    rst("ck7", "路径经 LCA=ck2")

    print("\n=== 4. 删除语义：被引用者隐藏、叶子物理回收 ===", flush=True)
    b.sbx.checkpoint.delete(b.cks["ck2"])  # 有后代 → 隐藏
    m2 = b.manifest("ck2")
    check(m2 is not None and m2.get("hidden") is True, "ck2 有后代，删除后隐藏而非物理删除")
    check(os.path.exists(os.path.join(b.dir_of("ck2"), "mem_bitmap")), "ck2 的 sidecar 保留（跨它回滚要用）")
    check(not os.path.exists(os.path.join(b.dir_of("ck2"), "snapfile")), "ck2 的 snapfile 已丢弃（隐藏条目不是恢复目标）")
    if SCHEME == "xfs":
        check(not os.path.exists(os.path.join(b.dir_of("ck2"), MEMFILE)),
              "XFS 套：隐藏条目的 %s 可丢（内容来自目标自己）" % MEMFILE)
    else:
        check(os.path.exists(os.path.join(b.dir_of("ck2"), MEMFILE)),
              "ext4 套：隐藏条目的 %s 必须留（后代靠它解析内容）" % MEMFILE)
    names = {c.name for c in b.sbx.checkpoint.list()}
    check("ck2" not in names, "list 里看不到 ck2：%s" % sorted(names))
    rst("ck1", "跨隐藏的 ck2 纪元")
    rst("ck4", "再跨回来")

    b.sbx.checkpoint.delete(b.cks["ck6"])  # 叶子 → 物理删除
    check(not os.path.exists(b.dir_of("ck6")), "ck6 是叶子，物理删除")
    rst("ck3", "删掉 ck6 之后其余分支不受影响")

    print("\n=== 5. 失败语义：算不出回滚集就拒绝，沙箱照常运行 ===", flush=True)
    rst("ck7", "先站到 ck7")
    side = os.path.join(b.dir_of("ck5"), "mem_bitmap")
    if os.path.exists(side):
        os.rename(side, side + ".hidden")
        print("人为破坏：移走 %s" % side, flush=True)
        rst_refused("ck4", "路径 ck7→ck5→ck4 需要被破坏的 ck5 纪元")
        check(b.run("echo alive") == "alive", "沙箱在拒绝之后照常运行")
        os.rename(side + ".hidden", side)
        rst("ck4", "sidecar 放回来，又能恢复了")
    else:
        check(False, "找不到 ck5 的 sidecar，无法做失败语义测试")

    print("\n=== 6. 存储 ===", flush=True)
    print(lib.sh("du -sh --apparent-size %s/%s; du -sh %s/%s" % (lib.STORE, b.id, lib.STORE, b.id)), flush=True)
    print("（apparent 远大于实际是正常的：两套的内存产物都是稀疏文件。", flush=True)
    print("  XFS 套在稀疏之外还叠加 reflink 的 extent 共享，所以差得更多。）", flush=True)
finally:
    b.kill()

sys.exit(check.finish())
