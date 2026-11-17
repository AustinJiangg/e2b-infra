"""从一轮 950.sh all 的结果目录取出进程级手册第 22 章各表要填的数，打印成便于复制的终端文本。只用标准库。

用法：950.sh report handbook（最近一轮），或 python3 handbook_extract.py 结果目录。
只读结果目录里的日志与 JSON，不打印含密钥的文件。每一段标明来源文件，缺文件时写“缺”。
"""
import json
from pathlib import Path
import re
import subprocess
import sys

import paths

CUT = 400


def clip(text, n=CUT):
    text = str(text).replace("\n", " ⏎ ")
    return text if len(text) <= n else text[:n - 1] + "…"


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def lines_of(path):
    try:
        return Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return None


def head(title):
    print("-- " + title)


def deployment(run):
    acc = run / "acceptance"
    head("部署条件")
    print("  宿主内核: " + subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip())
    s = load(acc / "summary.json") or {}
    for k in ("machine", "started", "finished", "template", "python", "sdk", "verdict"):
        print("  %s: %s" % (k, s.get(k, "缺")))
    print("  slow_calls: %s" % clip(json.dumps(s.get("slow_calls"), ensure_ascii=False)))
    print("  skipped: %s" % clip(json.dumps(s.get("skipped"), ensure_ascii=False)))
    print("  steps: %s" % ", ".join("%s=%s" % (x.get("name"), x.get("status")) for x in s.get("steps", [])))
    for d in ("acceptance", "stress"):
        for ln in lines_of(run / d / "deliverables.sha256") or ["缺 %s/deliverables.sha256" % d]:
            print("  %s 产物: %s" % (d, ln))
    g = lines_of(acc / "guest-check.log")
    print("  guest-check.log: " + (clip(" ".join(x.strip() for x in g), 900) if g else "缺"))
    b = lines_of(run / "build-base-criu-1.log")
    print("  建模板末行: " + (b[-1].strip() if b else "缺"))


def acceptance(run):
    acc = run / "acceptance"
    head("功能验收计数")
    vf = load(acc / "verify_fixes.json")
    if vf is None:
        print("  verify_fixes.json 缺")
    else:
        cnt = {}
        for it in vf:
            cnt[it["status"]] = cnt.get(it["status"], 0) + 1
        print("  verify_fixes 计数: %s；SKIP: %s" % (cnt, [it["id"] for it in vf if it["status"] == "SKIP"]))
        # 每项只留第 22 章表里要的那几行证据
        want = {"G-06": r"image_bytes", "G-08": r"成功 create|第 \d+ 次|最后一次 ioctl",
                "G-11": r"df / used|层目录 L|镜像目录数", "G-20": r"p50",
                "G-21": r"create #|estimated images|MemAvailable", "PERF": r"p50|进程数"}
        for it in vf:
            if it["id"] in want:
                ev = [str(e) for e in it.get("evidence", []) if re.search(want[it["id"]], str(e))]
                print("  %s %s: %s" % (it["id"], it["status"], clip(" | ".join(ev), 900)))
    vm = load(acc / "verify_multitree.json")
    if vm is None:
        print("  verify_multitree.json 缺")
    else:
        items = vm.get("items", [])
        print("  verify_multitree: PASS %d / 共 %d；非 PASS: %s；slow_calls %d" % (
            sum(i["status"] == "PASS" for i in items), len(items),
            ["%s=%s" % (i["id"], i["status"]) for i in items if i["status"] != "PASS"], len(vm.get("slow_calls", []))))
        for i in items:
            if i["id"] == "V5":
                print("  V5: " + clip(" | ".join(e for e in map(str, i.get("evidence", [])) if "image_bytes" in e or "RssAnon" in e), 600))
    log = lines_of(acc / "verify_multitree.log") or []
    print("  verify_multitree.log 里 BAD 行: %d" % sum("BAD" in x for x in log))
    cr = lines_of(acc / "clear_refs_race.log")
    if cr is None:
        print("  clear_refs_race.log 缺")
    else:
        i = next((n for n, x in enumerate(cr) if x.startswith("== 汇总")), None)
        for x in (cr[i:i + 8] if i is not None else ["没有汇总段"]):
            print("  漏页 " + x.strip())
    rows = [json.loads(x) for x in (lines_of(acc / "clear_refs_race.jsonl") or []) if x.strip().startswith("{")]
    if rows:
        print("  漏页 jsonl: 轮数 %d；restored_state %s；sha_eq True %d；wedged True %d" % (
            len(rows), sorted({str(r.get("restored_state")) for r in rows}),
            sum(r.get("sha_eq") is True for r in rows), sum(bool(r.get("wedged")) for r in rows)))
    head("verify_gsd 8 次 restore")
    vg = lines_of(acc / "verify_gsd.log") or []
    for x in vg:
        if re.search(r"p50=|根盘|判定|gsd -commit|^\s*restore #", x):
            print("  " + x.strip())


def probe(run):
    head("探针 whiteout.json")
    w = load(run / "stress" / "whiteout.json")
    if w is None:
        print("  缺")
        return
    for c in w:
        dm = c.get("dmesg")
        dm = dm[1] if isinstance(dm, list) and len(dm) > 1 else dm
        print("  %-7s crashed=%s whiteouts_at_start=%s dmesg=%s" % (
            c.get("case"), c.get("crashed"), c.get("whiteouts_at_start"), clip(dm or "（空）", 120)))


def stress(run):
    st = run / "stress"
    head("四组压测")
    s = load(st / "summary.json") or {}
    print("  stress verdict: %s；steps: %s" % (s.get("verdict", "缺"),
          ", ".join("%s=%s" % (x.get("name"), x.get("status")) for x in s.get("steps", []))))
    for g in ("all", "noworkers"):
        d = load(st / g / "stress-summary.json")
        res = (lines_of(st / g / "RESULT") or ["缺"])[0]
        con = st / g / "console.txt"
        print("  [%s] RESULT=%s console=%s字节 fail文件=%d" % (g, res, con.stat().st_size if con.exists() else "缺",
              len(list((st / g).glob("*-create-fail-*")))))
        if d:
            keep = ("rounds", "workers", "gsd_ops", "lat_ms_p50", "worker_ops_total", "worker_errs_by_errno",
                    "worker_heartbeat_age_s", "slab_start", "slab_end", "dmesg_new_hits", "health", "ckpts_left")
            print("    " + clip(json.dumps({k: d.get(k) for k in keep}, ensure_ascii=False), 1200))
    for g in ("multitree", "deldense"):
        d = load(st / g / ("%s-summary.json" % g))
        res = (lines_of(st / g / "RESULT") or ["缺"])[0]
        con = st / g / "console.txt"
        print("  [%s] RESULT=%s console=%s字节" % (g, res, con.stat().st_size if con.exists() else "缺"))
        if d:
            keep = ("verdict", "rounds", "workers", "stats", "unexpected_errors", "worker_ops_total", "worker_errs",
                    "worker_unexpected_errno", "worker_heartbeat_age_s", "dmesg_hits", "slab_boot", "slab_end",
                    "health", "state", "checkpoints_left", "marks_end", "perf_p50", "elapsed_s")
            print("    " + clip(json.dumps({k: d.get(k) for k in keep if k in d}, ensure_ascii=False), 1600))
        if g == "multitree":
            for x in lines_of(st / g / "multitree.log") or []:
                if "性能 p50" in x:
                    print("    " + clip(x.strip(), 700))


def perf(run):
    acc = run / "acceptance"
    head("分档性能三轮（summarize_checkpoint_perf.py）")
    runs = [acc / ("perf-run%d.json" % i) for i in (1, 2, 3)]
    if not all(p.exists() for p in runs):
        print("  缺 perf-run JSON")
        return
    script = paths.SCRIPTS / "acceptance" / "summarize_checkpoint_perf.py"
    r = subprocess.run([sys.executable, str(script)] + [str(p) for p in runs], capture_output=True, text=True)
    for x in (r.stdout + r.stderr).splitlines():
        if not x.startswith("|"):          # 中位数表下面自己算，这里只留判定行
            print("  " + x)
    js = [load(p) or {} for p in runs]

    def tier(c):
        return c.get("mem_mb", 0) + c.get("file_kb", 0) / 1024

    def med(v):
        v = sorted(x for x in v if x is not None)
        return v[len(v) // 2] if v else None

    def f(x):
        return "-" if x is None else "%.1f" % x

    print("  改动MiB | 内存/文件MiB | 服务端 total_ms 三轮中位数：全量基点 增量 restore | 墙钟 e2e_ms 中位数：全量基点 增量 restore")
    print("  改动MiB | 逐轮 全量基点 r1 r2 r3 | 增量 r1 r2 r3 | restore r1 r2 r3 | RSS MiB r1 r2 r3")
    rows = []
    for i, c0 in enumerate(js[0].get("create", [])):
        get = lambda key, field, sub="sdk": [((j.get(key) or [{}] * 6)[i].get(sub) or {}).get(field) if sub else
                                            (j.get(key) or [{}] * 6)[i].get(field) for j in js]
        b, c, rs = get("baseline", "total_ms"), get("create", "total_ms"), get("restore", "total_ms")
        bw, cw, rw = get("baseline", "e2e_ms", None), get("create", "e2e_ms", None), get("restore", "e2e_ms", None)
        rss = [x / 1048576 if x else None for x in get("create", "rss_bytes", None)]
        t = tier(c0)
        rows.append("  %g | %g/%g | %s %s %s | %s %s %s" % (t, c0.get("mem_mb", 0), c0.get("file_kb", 0) / 1024,
                    f(med(b)), f(med(c)), f(med(rs)), f(med(bw)), f(med(cw)), f(med(rw))))
        rows.append("  %g | %s | %s | %s | %s" % (t, " ".join(map(f, b)), " ".join(map(f, c)), " ".join(map(f, rs)),
                    " ".join(map(f, rss))))
    for x in rows[0::2] + rows[1::2]:
        print(x)
    for p, j in zip(runs, js):
        ok = sum(bool(r.get("ok")) for r in j.get("restore", []))
        print("  %s schema=%s mode=%s restore_path=%s 恢复校验 %d/%d sdk-journal 最大差 %s" % (
            p.name, j.get("schema_version"), j.get("measurement_mode"), j.get("restore_path"), ok,
            len(j.get("restore", [])), j.get("max_sdk_journal_diff_ms")))


def smoke(run):
    head("回切与沙箱级冒烟")
    for x in lines_of(run / "all.log") or []:
        if re.search(r"切回沙箱级|已启动沙箱级栈|模板 base 建沙箱|health", x):
            print("  " + clip(x.strip(), 200))
    for p in sorted((run / "sandbox-smoke").glob("*/SUMMARY.md")):
        for x in lines_of(p) or []:
            if re.search(r"PASS|FAIL|SKIP", x):
                print("  " + clip(x.strip(), 200))


def main(run):
    run = Path(run)
    print("==== 手册第 22 章取数：%s ====" % run.name)
    for f in (deployment, acceptance, probe, stress, perf, smoke):
        try:
            f(run)
        except Exception as exc:   # 一段出错不影响其余段
            print("  （%s 出错：%s）" % (f.__name__, exc))
    print("==== 结束 ====")


if __name__ == "__main__":
    main(sys.argv[1])
