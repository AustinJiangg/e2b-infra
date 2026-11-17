#!/usr/bin/env python3
"""验证 restore 之后的第一次 checkpoint 是增量，并且 restore 到这份增量时内存完全正确。

背景（进程级手册第 12 章 §6）：CRIU 的 PID 复用检测会让 restore 出来的进程放弃父镜像、整个进程
重新写出。gsd 在 restore 之后先清 soft-dirty，再为下一次 dump 建一个替身父目录（inventory 的
dump 时刻改到恢复之后），使这一次也只写恢复之后改过的页。替身目录用错了的后果是 restore 出来的
内存静默出错，所以这里每一轮都真的 restore 到这份增量并核对整块内存。

每轮：restore 到上一代（第一轮是基点）→ 核对内存 → 只改前 d MiB（d 随机取 1～16）
→ checkpoint A（restore 之后的第一次，走替身父目录）→ 记下内存 SHA256 → 把整块内存改掉
→ restore A → 核对 SHA256。偶数轮把 A 当下一轮的起点，替身目录一代接一代；奇数轮回到基点。
判定：全部一致；A 的镜像不超过 d + 8 MiB（解释器和校验本身会弄脏少量页）；CRIU 日志里没有
"Pid reuse"；gsd 日志里没有"cannot build reparent dir"。

在宿主机上跑（凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python verify_restore_incremental.py [--template base-criu] [--rounds 20] [--out result.json]
"""
import argparse, json, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "benchmark"))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox
import ckpt_same_workload as w   # 常驻进程（benchmark/ckpt_same_workload.py 的 Hog）

ap = argparse.ArgumentParser()
ap.add_argument("--template", default="base-criu")
ap.add_argument("--rounds", type=int, default=20)
ap.add_argument("--size", type=int, default=64, help="常驻进程的内存，MiB")
ap.add_argument("--out", default="")
args = ap.parse_args()

MIB = 1 << 20
rnd = random.Random(20261118)
rows, bad = [], []
sb = Sandbox.create(template=args.template, timeout=3600)
try:
    sb.commands.run("echo never > /sys/kernel/mm/transparent_hugepage/enabled", user="root")
    hog = w.Hog(sb)
    hog.start(args.size)
    hog.dirty(args.size, "base")
    base = sb.checkpoint.create(name="base")
    want_base = hog.state(0)
    prev, want_prev = base, want_base
    for r in range(args.rounds):
        sb.checkpoint.restore(prev.checkpoint_id)
        back_ok = hog.state(0) == want_prev
        d = rnd.choice([1, 2, 4, 8, 16])
        hog.dirty(d, "r%d" % r)
        a = sb.checkpoint.create(name="a%d" % r)
        want_a = hog.state(0)
        hog.dirty(args.size, "junk%d" % r)
        sb.checkpoint.restore(a.checkpoint_id)
        ok = hog.state(0) == want_a
        mib = a.image_bytes / MIB
        row = {"round": r, "dirty_mib": d, "image_mib": round(mib, 2), "parent": a.parent_checkpoint_id,
               "restore_prev_ok": back_ok, "restore_a_ok": ok, "small": mib <= d + 8}
        rows.append(row)
        if not (back_ok and ok and row["small"]):
            bad.append(r)
        print("round %2d  d=%2d MiB  A 镜像 %6.1f MiB  回到上一代一致=%s  restore A 后一致=%s"
              % (r, d, mib, back_ok, ok), flush=True)
        if r % 2 == 0:
            prev, want_prev = a, want_a
        else:
            prev, want_prev = base, want_base
    reuse = int(hog.sh("grep -l 'Pid reuse' /var/lib/gsd/snapshots/*/t*/criu-dump.log 2>/dev/null | wc -l") or 0)
    fallback = int(hog.sh("journalctl -u gsd --no-pager -o cat | grep -c 'cannot build reparent'; true") or 0)
finally:
    sb.kill()

verdict = "PASS" if not bad and reuse == 0 and fallback == 0 else "FAIL"
print("== 判定 %s：不合格轮次 %s / %d；CRIU Pid reuse %d 次；替身目录回退 %d 次"
      % (verdict, bad or "无", args.rounds, reuse, fallback))
if args.out:
    with open(args.out, "w") as f:
        json.dump({"verdict": verdict, "rows": rows, "pid_reuse": reuse, "fallback": fallback}, f, ensure_ascii=False, indent=1)
sys.exit(0 if verdict == "PASS" else 1)
