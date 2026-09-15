#!/usr/bin/env python3
"""HDBSS 到底有没有真的用上——三级证据。

L1 能力：宿主的 KVM 认不认 cap 502（cap_test.c，与是否跑沙箱无关）
L2 自报：Firecracker 为**这个沙箱**选了哪个后端（GET / 的 dirty_tracking 字段）
L3 数据面：能力启用了不等于硬件真的在记脏页。用一段写密集负载区分：
     软件写保护下，每个"干净页"的第一次写都要陷出一次（stage-2 write fault），
     所以 ①KVM 退出数 ≈ 被写的页数 ②刚做完 checkpoint 之后的第一遍写明显慢于第二遍。
     HDBSS 下 CPU 自己记录，两条都不成立。

用法: hdbss_evidence.py [写入 MB，默认 512]
"""
import os
import shutil
import subprocess
import sys
import time

import lib

MB = int(sys.argv[1]) if len(sys.argv) > 1 else 512
PAGES = MB * 256  # 4 KiB pages

print("=== L1 能力：宿主 KVM 认不认 cap 502 ===", flush=True)
here = os.path.dirname(os.path.abspath(__file__))
probe = "/tmp/cap_test"
if not os.path.exists(probe) and shutil.which("gcc"):
    subprocess.run(["gcc", "-static", "-o", probe, os.path.join(here, "cap_test.c")],
                   capture_output=True)
    if not os.path.exists(probe):
        subprocess.run(["gcc", "-o", probe, os.path.join(here, "cap_test.c")], capture_output=True)
if os.path.exists(probe):
    out = subprocess.run([probe], capture_output=True, text=True).stdout
    print("".join("  " + l + "\n" for l in out.strip().splitlines()), end="", flush=True)
    capable = "supported ->" in out and "NOT supported" not in out
else:
    print("  （没有 gcc，跳过；以 L2 为准）", flush=True)
    capable = None

print("\n=== L2 自报：Firecracker 为这个沙箱选了哪个后端 ===", flush=True)
b = lib.Box()
backend = b.backend
print("  dirty_tracking = %s" % backend, flush=True)
if backend == "?":
    print("  读不到该字段——跑的 Firecracker 不是本工具箱的二进制", flush=True)
logs = lib.sh("grep -h HDBSS /data/nomad/alloc/*/alloc/logs/start.stdout.0 2>/dev/null | grep %s | tail -1" % b.id)
if logs:
    print("  本沙箱的日志行: %s" % logs.splitlines()[-1][:200], flush=True)
else:
    print("  （日志里没有这个沙箱的 HDBSS 行——启用成功时打的是 'HDBSS enabled'）", flush=True)

print("\n=== L3 数据面：写 %d MB（%d 页）===" % (MB, PAGES), flush=True)
try:
    # Allocate the file first so neither timed pass pays guest-side page
    # allocation — the only difference left is stage-2 write faults.
    b.run("dd if=/dev/zero of=/dev/shm/hb bs=1M count=%d 2>/dev/null" % MB, timeout=600)
    b.stamp("warmup")
    b.sbx.checkpoint.create(name="hb")  # resets dirty tracking; kvm-wp re-protects everything

    fcpid = lib.fc_pid(b.id)
    print("  firecracker pid = %s" % fcpid, flush=True)

    perf = shutil.which("perf")
    exits = None
    if perf and fcpid:
        p = subprocess.Popen(
            [perf, "stat", "-e", "kvm:kvm_exit", "-p", str(fcpid), "-x", ","],
            stderr=subprocess.PIPE, text=True)
        t0 = time.monotonic()
        b.run("dd if=/dev/zero of=/dev/shm/hb bs=1M count=%d conv=notrunc 2>/dev/null" % MB, timeout=600)
        cold = time.monotonic() - t0
        p.send_signal(2)  # SIGINT makes perf print and exit
        err = p.communicate(timeout=30)[1]
        for line in err.splitlines():
            if "kvm:kvm_exit" in line:
                try:
                    exits = int(line.split(",")[0].replace("<not counted>", "0") or 0)
                except ValueError:
                    exits = None
    else:
        if not perf:
            print("  （本机没有 perf，跳过退出计数，只用冷热两遍对比）", flush=True)
        t0 = time.monotonic()
        b.run("dd if=/dev/zero of=/dev/shm/hb bs=1M count=%d conv=notrunc 2>/dev/null" % MB, timeout=600)
        cold = time.monotonic() - t0

    t0 = time.monotonic()
    b.run("dd if=/dev/zero of=/dev/shm/hb bs=1M count=%d conv=notrunc 2>/dev/null" % MB, timeout=600)
    warm = time.monotonic() - t0

    print("  checkpoint 之后第一遍写: %.3fs" % cold, flush=True)
    print("  紧接着第二遍写        : %.3fs" % warm, flush=True)
    ratio = cold / warm if warm > 0 else 0
    print("  冷/热 = %.2f" % ratio, flush=True)
    if exits is not None:
        print("  第一遍期间 kvm:kvm_exit = %d（被写页数 %d，每页 %.3f 次）"
              % (exits, PAGES, exits / float(PAGES)), flush=True)

    print("\n=== 判定 ===", flush=True)
    if capable is False:
        print("  宿主不支持 cap 502 → 不可能用上 HDBSS，当前是软件写保护。", flush=True)
    if backend == "hdbss":
        print("  Firecracker 报告 hdbss。数据面佐证：", flush=True)
        if exits is not None:
            print("    - 每页 %.3f 次 KVM 退出。软件写保护应该 ≈1.0，硬件标脏应远小于 1。"
                  % (exits / float(PAGES)), flush=True)
        print("    - 冷/热 = %.2f。软件写保护下第一遍要为每页陷出一次、明显更慢；" % ratio, flush=True)
        print("      硬件标脏下两遍应该接近（≈1.0）。", flush=True)
    elif backend == "kvm-wp":
        print("  Firecracker 报告 kvm-wp —— **没有用上 HDBSS**。", flush=True)
        print("  若宿主 L1 是 supported，那是 Firecracker 启用失败，去日志找", flush=True)
        print("  'HDBSS unavailable, falling back to ...' 那一行的 errno。", flush=True)
    else:
        print("  后端 = %s，先解决这个再谈数据面。" % backend, flush=True)
finally:
    b.kill()
