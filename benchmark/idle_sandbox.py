"""
创建 N 个沙箱后什么也不做，保持空闲，供宿主机侧测内存（mem_snap.sh / measure_idle_mem.sh）。

单独用：
    python idle_sandbox.py --count 1 --hold 600
配合 measure_idle_mem.sh 用（它会传 --ready-file/--done-file，不用手动跑）。

注意：创建后**不在沙箱里执行任何命令**，否则 envd 拉起进程会额外碰内存。
--guest 只在宿主机测量全部结束（done-file 出现）后才进客户机看 free/meminfo。
"""
from dotenv import load_dotenv
load_dotenv()

import argparse
import os
import time

from e2b import Sandbox


def main() -> None:
    ap = argparse.ArgumentParser(description="创建空闲沙箱并保持")
    ap.add_argument("--count", type=int, default=1, help="沙箱个数")
    ap.add_argument("--hold", type=int, default=600, help="最长保持秒数（到点或 done-file 出现就结束）")
    ap.add_argument("--template", default="base", help="模板 ID/别名")
    ap.add_argument("--ready-file", help="全部创建完成后写入沙箱 ID 列表")
    ap.add_argument("--done-file", help="出现该文件即提前结束")
    ap.add_argument("--guest", action="store_true", help="结束前进第一个沙箱看 free -m 和 /proc/meminfo")
    args = ap.parse_args()

    sbxs = []
    try:
        for i in range(args.count):
            t0 = time.time()
            s = Sandbox.create(template=args.template, timeout=args.hold + 300)
            print(f"[{i + 1}/{args.count}] created {s.sandbox_id}  ({time.time() - t0:.2f}s)", flush=True)
            sbxs.append(s)

        if args.ready_file:
            with open(args.ready_file, "w") as f:
                f.write("\n".join(s.sandbox_id for s in sbxs) + "\n")
        print(f"全部创建完成，空闲保持（最长 {args.hold}s）...", flush=True)

        deadline = time.time() + args.hold
        while time.time() < deadline:
            if args.done_file and os.path.exists(args.done_file):
                break
            time.sleep(1)

        if args.guest and sbxs:
            s = sbxs[0]
            print(f"\n===== 客户机视角（{s.sandbox_id}）=====", flush=True)
            print("$ free -m\n" + s.commands.run("free -m").stdout, flush=True)
            print("$ head -5 /proc/meminfo\n" + s.commands.run("head -5 /proc/meminfo").stdout, flush=True)
    finally:
        for s in sbxs:
            try:
                s.kill()
            except Exception as e:  # noqa: BLE001
                print(f"kill {s.sandbox_id} failed: {e}", flush=True)
        print(f"已关闭 {len(sbxs)} 个沙箱", flush=True)


if __name__ == "__main__":
    main()
