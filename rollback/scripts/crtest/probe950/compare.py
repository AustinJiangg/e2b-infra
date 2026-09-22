#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把两台机器的 `probe-host-*.json` 逐键 diff 成表。

    python3 compare.py results/920b/probe-host-*.json results/950/probe-host-*.json
    python3 compare.py A.json B.json --all        # 连相同的键也列
    python3 compare.py A.json B.json --only-star  # 只看「已知重要键」
    python3 compare.py A.json B.json --md > diff.md

分三类：**不同**、**一边缺失**（含一边是 `<absent>` 的）、相同。
「已知重要键」（前面带 ★）是本轮判断「920B 的结论能不能搬到 950」时必看的那些，
清单见下面的 STAR，依据 `950-vs-920b-differences-and-risks.md` §1–§3。

只依赖标准库 json/argparse —— 950 上跑的是系统 python3 还是 conda 的都无所谓。
"""

import argparse
import json
import re
import sys
import unicodedata

# ★ 已知重要键：前缀匹配或正则（`re:` 开头）
STAR = [
    "host.kernel", "host.arch", "cpu.model", "cpu.midr_part", "cpu.count",
    "cpu.threads_per_core", "cpu.sockets", "cpu.numa_nodes",
    "mem.MemTotal", "mem.HugePages_Total", "mem.HugePages_Free", "mem.Hugepagesize",
    "thp.enabled",
    "kcfg.CONFIG_ARM64_HDBSS", "kcfg.CONFIG_USERFAULTFD",
    "kcfg.CONFIG_HAVE_ARCH_USERFAULTFD_WP", "kcfg.CONFIG_DEBUG_FS",
    "kcfg.CONFIG_TRANSPARENT_HUGEPAGE", "kcfg.CONFIG_ARM64_HW_AFDBM",
    "kvm.cap502.check_extension", "kvm.cap502.enable_cap", "kvm.hdbss_usable",
    "kvm_arm.mode", "debugfs.kvm.readable",
    "clocksource.current", "timer.cntfrq",
    "store.fstype", "store.device", "store.mount_options",
    "re:^fsync\\.(single|concurrent|ratio|engine)",
    "e2b.env.FC_TRACK_DIRTY_PAGES", "e2b.env.ORCHESTRATOR_BASE_PATH",
    "e2b.env.FIRECRACKER_VERSIONS_DIR", "e2b.env.ENVIRONMENT",
    "e2b.env.LOCAL_TEMPLATE_STORAGE_BASE_PATH", "e2b.env.DEFAULT_KERNEL_VERSION",
    "e2b.orchestrator_sha256", "e2b.fc_versions_dir",
    "re:^fc\\..*\\.(sha256|version|savedirtybitmap)$",
    "re:^guestkernel\\..*sha256$",
    "listen.3000", "listen.5008", "listen.49984",
    "re:^nomad\\.job\\.",
    "sdk.python_version", "sdk.e2b_version", "re:^sdk\\.count\\.",
    "tool.perf", "tool.gcc", "tool.docker", "tool.nvme", "tool.cargo",
]
STAR_RE = [re.compile(s[3:]) for s in STAR if s.startswith("re:")]
STAR_PLAIN = [s for s in STAR if not s.startswith("re:")]

ABSENT = "<absent>"


def starred(key):
    if key in STAR_PLAIN:
        return True
    return any(r.search(key) for r in STAR_RE)


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s, n):
    return s + " " * max(0, n - width(s))


def load(path):
    with open(path) as f:
        d = json.load(f)
    if not isinstance(d, dict):
        sys.exit("%s 不是 probe-host 的 JSON（顶层应是一个对象）" % path)
    # 动态探测的 JSON 是嵌套的，这里只比静态那份
    if "steps" in d and "meta" in d:
        sys.exit("%s 看着是 probe-dynamic 的输出；compare.py 只比 probe-host-*.json" % path)
    return {k: ("" if v is None else str(v)) for k, v in d.items()}


def cut(s, n):
    if width(s) <= n:
        return s
    out = ""
    for ch in s:
        if width(out) + width(ch) > n - 1:
            return out + "…"
        out += ch
    return out


def main():
    ap = argparse.ArgumentParser(description="两份 probe-host JSON 的逐键 diff")
    ap.add_argument("a", help="第一份（惯例：920B）")
    ap.add_argument("b", help="第二份（惯例：950）")
    ap.add_argument("--all", action="store_true", help="连相同的键也列出来")
    ap.add_argument("--only-star", action="store_true", help="只列 ★ 已知重要键")
    ap.add_argument("--width", type=int, default=58, help="每列值的最大显示宽度")
    ap.add_argument("--md", action="store_true", help="输出 Markdown 表格")
    args = ap.parse_args()

    A, B = load(args.a), load(args.b)
    na = A.get("host.hostname", args.a)
    nb = B.get("host.hostname", args.b)

    keys = sorted(set(A) | set(B))
    diff, miss, same = [], [], []
    for k in keys:
        if args.only_star and not starred(k):
            continue
        va, vb = A.get(k), B.get(k)
        if va is None or vb is None or va == ABSENT or vb == ABSENT:
            if va == vb:
                same.append((k, va, vb))
            else:
                miss.append((k, va if va is not None else "—", vb if vb is not None else "—"))
        elif va != vb:
            diff.append((k, va, vb))
        else:
            same.append((k, va, vb))

    def emit(title, rows):
        if not rows:
            return
        print()
        print("## %s（%d 项）" % (title, len(rows)) if args.md else
              "=== %s（%d 项）===" % (title, len(rows)))
        rows = sorted(rows, key=lambda r: (not starred(r[0]), r[0]))
        if args.md:
            print("| | 键 | %s | %s |" % (na, nb))
            print("|---|---|---|---|")
            for k, va, vb in rows:
                print("| %s | `%s` | %s | %s |"
                      % ("★" if starred(k) else "", k,
                         cut(va, args.width).replace("|", "\\|"),
                         cut(vb, args.width).replace("|", "\\|")))
            return
        kw = min(46, max([width(r[0]) for r in rows] + [10]))
        print("  %s %s %s" % (pad("键", kw), pad(na, args.width), nb))
        for k, va, vb in rows:
            print("%s %s %s %s" % ("★" if starred(k) else " ",
                                   pad(cut(k, kw), kw),
                                   pad(cut(va, args.width), args.width),
                                   cut(vb, args.width)))

    hdr = "probe-host 对照：%s（%s） vs %s（%s）" % (na, args.a, nb, args.b)
    print("# " + hdr if args.md else hdr)
    print("键总数 %d：不同 %d，一边缺失 %d，相同 %d%s"
          % (len(keys), len(diff), len(miss), len(same),
             "（只看 ★）" if args.only_star else ""))
    emit("不同", diff)
    emit("一边缺失 / <absent>", miss)
    if args.all:
        emit("相同", same)
    else:
        print()
        print("（相同的 %d 项已折叠，加 --all 展开）" % len(same))
    return 0


if __name__ == "__main__":
    sys.exit(main())
