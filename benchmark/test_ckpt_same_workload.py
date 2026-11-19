"""ckpt_same_workload.py 的本地单测：测点补全、汇总、多轮合并、两套并列、guest 里跑的几段脚本。
不建沙箱、不需要 e2b SDK：python3 -m unittest benchmark/test_ckpt_same_workload.py"""
import io
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ckpt_same_workload as csw  # noqa: E402


def fake_rows(stack, series, point, n=5, base_ms=10.0, per_mib=0.2, rnd=0):
    p = csw.norm(point)
    rows = [{"series": series, **p, "pass": 0, "rep": -1, "kind": "base", "ckpt_ms": 100.0,
             "ckpt_server": {"total_ms": 90.0}, "artifact": {"bytes": 1 << 20}, "ok": True}]
    rng = random.Random(rnd)
    for rep in range(n + 1):
        v = base_ms + per_mib * p["d"] + rng.random()
        rows.append({"series": series, **p, "pass": 0, "rep": rep, "kind": "warmup" if rep == 0 else "sample",
                     "ckpt_ms": v, "ckpt2_ms": v, "restore_ms": v * 2, "ok": True,
                     "ckpt_server": {"total_ms": v - 1, "snapshot_ms": v - 3},
                     "ckpt2_server": {"total_ms": v - 1, "snapshot_ms": v - 3},
                     "restore_server": {"total_ms": 2 * v - 1}, "artifact": {"bytes": p["d"] << 20},
                     "artifact2": {"bytes": p["d"] << 20}})
    return rows


def fake_result(stack, rows, verdict="PASS"):
    return {"schema_version": 2, "stack": stack, "template": "base", "verdict": verdict, "rows": rows,
            "summary": csw.summarize(rows),
            "provenance": {"hostname": "h", "kernel": "k", "orchestrator_exe": "/x", "orchestrator_sha256": stack * 8,
                           "host_conditions": {"numa_balancing": "0", "thp": "never", "numa_summary": "2 个节点"},
                           "args": {}}}


class PointTests(unittest.TestCase):
    def test_norm_defaults_and_checks(self):
        p = csw.norm({"P": 64, "d": 16, "trees": 4})
        self.assertEqual((p["layout"], p["sync"], p["wait"], p["f"]), ("seq", "fsync", 0, 0))
        with self.assertRaises(ValueError):
            csw.norm({"P": 64, "d": 16, "trees": 3})
        with self.assertRaises(ValueError):
            csw.norm({"P": 16, "d": 32})
        with self.assertRaises(ValueError):
            csw.norm({"P": 16, "layout": "zigzag"})

    def test_all_series_points_valid(self):
        for name, pts in csw.SERIES.items():
            for p in pts:
                csw.norm(p)
        self.assertEqual(set(csw.ALL_SERIES.split(",")) - {"conc"}, set(csw.SERIES))

    def test_old_rows_keep_their_key(self):
        # 2026-11-18 以前的结果没有 layout/trees/sync/wait，按默认值归到同一测点
        old = {"series": "dirty", "P": 256, "d": 16, "f": 0}
        new = dict(old, **{k: csw.DEFAULTS[k] for k in ("layout", "trees", "sync", "wait")})
        self.assertEqual(csw.point_key(old), csw.point_key(new))

    def test_label(self):
        self.assertEqual(csw.point_label(csw.norm({"P": 256, "d": 16})), "P=256 d=16")
        self.assertEqual(csw.point_label(csw.norm({"P": 16, "f": 64, "sync": "none", "wait": 10})),
                         "P=16 d=0 f=64 sync=none wait=10s")
        self.assertIn("scatter", csw.point_label(csw.norm({"P": 256, "d": 16, "layout": "scatter"})))

    def test_scenarios(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"scenarios": [{"name": "a", "P": 64, "d": 8, "f": 4, "sync": "none", "wait": 5}]}, f)
        try:
            sc = csw.load_scenarios(f.name)
            self.assertEqual(sc[0]["name"], "a")
            self.assertEqual(sc[0]["trees"], 1)
        finally:
            os.unlink(f.name)


class MixTests(unittest.TestCase):
    def test_fractional_file(self):
        p = csw.norm({"P": 256, "d": 70, "f": 0.001})
        self.assertEqual(p["f"], 0.001)
        self.assertIsInstance(csw.norm({"P": 256, "d": 12, "f": 4.0})["f"], int)
        self.assertEqual(csw.dd_size(4), "bs=1M count=4")
        self.assertEqual(csw.dd_size(0.001), "bs=1049 count=1 iflag=fullblock")
        self.assertIn("f=0.001", csw.point_label(p))
        self.assertEqual([(q["d"], q["f"]) for q in csw.SERIES["mix"]][-1], (70, 0.001))
        self.assertTrue(all(3 * q["f"] == q["d"] for q in csw.SERIES["mix"][:-1]))
        self.assertEqual(csw.SERIES["mix"][-1]["P"], 70)

    def test_mix_points(self):
        self.assertTrue(all(q["P"] == q["d"] for q in csw.SERIES["mix"]))
        self.assertTrue(all(q["P"] == 256 for q in csw.SERIES["mix256"]))
        self.assertIn("mix256", csw.ALL_SERIES)
        self.assertNotIn("mix256", csw.DEFAULT_SERIES)
        p = csw.norm(csw.SERIES["mix"][0])
        self.assertEqual((p["P"], p["d"], p["f"]), (0, 0, 0))

    def test_pause_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "alloc", "logs"))
            pl = csw.PauseLog.__new__(csw.PauseLog)
            pl.dir, pl.task = os.path.join(tmp, "alloc", "logs"), "start"
            f0 = os.path.join(pl.dir, "start.stdout.0")
            line = ('2026-11-19T20:00:00.000+0800  INFO  Pause timing  {"service": "x", "sandbox.id": "%s", '
                    '"pause.snapshot_ms": 250.5, "pause.upload_ms": 3.25}\n')
            open(f0, "w").write(line % "old")
            mark = pl.mark()
            self.assertIsNone(pl.take("old", mark, timeout=0))      # mark 之前的行不算
            open(f0, "a").write("other\n" + line % "sbx1")
            self.assertEqual(pl.take("sbx1", mark, timeout=0), {"snapshot": 250.5, "upload": 3.25})
            mark = pl.mark()
            open(os.path.join(pl.dir, "start.stdout.1"), "w").write(line % "sbx2")   # 日志轮转到新文件
            self.assertEqual(pl.take("sbx2", mark, timeout=0)["upload"], 3.25)
            self.assertIsNone(csw.PauseLog.take(pl, "none", mark, timeout=0))

    def test_mix_table(self):
        pts = ({"P": 12, "d": 12, "f": 4}, {"P": 70, "d": 70, "f": 0.001})

        def with_native(stack, rnd):
            rows = []
            for pt in pts:
                rows += fake_rows(stack, "mix", pt, rnd=rnd)
            rows += fake_rows(stack, "size", {"P": 32, "d": 16}, rnd=rnd) + \
                fake_rows(stack, "size", {"P": 384, "d": 16}, rnd=rnd)
            if stack == "criu":
                for pt in pts:
                    p = csw.norm(pt)
                    rows.append({"series": "mix", **p, "pass": 0, "rep": 0, "kind": "native_first", "pause_ms": 900.0,
                                 "pause_wall_ms": 905.0, "resume_ms": 200.0, "ok": True})
                    for i in range(4):
                        rows.append({"series": "mix", **p, "pass": 0, "rep": i + 1, "kind": "native",
                                     "pause_ms": 300.0 + i, "pause_wall_ms": 304.0 + i, "pause_upload_ms": 4.0,
                                     "resume_ms": 100.0 + i, "resume_call_ms": 90.0, "ready_ms": 10.0 + i,
                                     "read_extra_ms": 20.0 + i, "ok": True})
            return fake_result(stack, rows)
        s, c = with_native("sandbox", 1), with_native("criu", 2)
        e = [x for x in c["summary"] if x["series"] == "mix" and x["d"] == 12][0]
        self.assertEqual(e["native_n"], 4)
        self.assertEqual(e["native_first_pause_ms"]["p50"], 900.0)
        self.assertEqual(e["read_extra_ms"]["p50"], 21.5)
        self.assertEqual(e["n"], 5)                 # native 行不混进 checkpoint 的样本
        for r in (s, c):
            r["_path"] = r["stack"]
        merged, table = csw.compare_table([s, c])
        buf = io.StringIO()
        with redirect_stdout(buf):
            csw.print_compare(merged, table)
            csw.print_summary(c)
        out = buf.getvalue()
        self.assertIn("对外分档表", out)
        self.assertIn("16\t12\t4\t12\t", out)
        self.assertIn("70.001\t70\t0.001\t70\t", out)
        self.assertIn("\t".join(csw.MIX_HEAD), out)
        self.assertIn("301.5\t101.5\t21.5", out)     # pause（第一步）、resume、首读
        self.assertIn("[restore 随进程大小", out)
        self.assertIn("384\t", out)
        self.assertIn("故意构造", out)
        self.assertIn("原生 pause / resume", out)


class SummaryTests(unittest.TestCase):
    def test_summary_print_and_merge(self):
        rows = fake_rows("criu", "dirty", {"P": 256, "d": 16}) + \
            fake_rows("criu", "layout", {"P": 256, "d": 256, "layout": "scatter"})
        res = fake_result("criu", rows)
        self.assertEqual(len(res["summary"]), 2)
        self.assertEqual(res["summary"][0]["n"], 5)
        self.assertIn("p99", res["summary"][0]["ckpt_ms"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            csw.print_summary(res)
        out = buf.getvalue()
        self.assertIn("layout：", out)
        self.assertIn("P=256 d=256 scatter", out)
        merged = csw.merge([dict(res, _path="a"), dict(res, _path="b")])
        self.assertEqual(merged["rounds"], 2)
        self.assertEqual(merged["summary"][0]["n"], 10)

    def test_compare(self):
        s = fake_result("sandbox", fake_rows("sandbox", "dirty", {"P": 256, "d": 64}, per_mib=0.1))
        c1 = fake_result("criu", fake_rows("criu", "dirty", {"P": 256, "d": 64}, per_mib=0.3, rnd=1))
        c2 = fake_result("criu", fake_rows("criu", "dirty", {"P": 256, "d": 64}, per_mib=0.3, rnd=2))
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, r in enumerate((s, c1, c2)):
                d = os.path.join(tmp, "step%d" % i)
                os.makedirs(d)
                paths.append(os.path.join(d, "result.json"))
                json.dump(r, open(paths[-1], "w"))
            merged, table = csw.compare_table(csw.load_results(paths))
            self.assertEqual(merged["criu"]["rounds"], 2)
            self.assertEqual(len(table), 1)
            self.assertEqual(table[0]["c"]["n"], 10)
            buf = io.StringIO()
            with redirect_stdout(buf):
                csw.print_compare(merged, table)
            self.assertIn("*", buf.getvalue())
            # --compare-siblings 从 --out 的上一级目录找其他步骤的 result.json
            me = os.path.join(tmp, "cmp", "result.json")
            os.makedirs(os.path.dirname(me))
            out = subprocess.run([sys.executable, csw.__file__, "--compare-siblings", "--out", me],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.load(open(me))["verdict"], "PASS")


class GuestScriptTests(unittest.TestCase):
    def test_resident_process(self):
        with tempfile.TemporaryDirectory() as wd:
            hog = os.path.join(wd, "hog.py")
            open(hog, "w").write(csw.RESIDENT_PY)
            proc = subprocess.Popen([sys.executable, hog, "4", wd])
            try:
                for _ in range(100):
                    if os.path.exists(os.path.join(wd, "pid")) and open(os.path.join(wd, "pid")).read():
                        break
                    time.sleep(0.05)

                def ask(nonce, op, arg=""):
                    open(os.path.join(wd, "cmd"), "w").write("%s %s %s" % (nonce, op, arg))
                    os.kill(proc.pid, signal.SIGUSR1)
                    for _ in range(100):
                        out = open(os.path.join(wd, "rsp")).read()
                        if out.startswith(nonce + " "):
                            return out.split()[1] if op != "shat" else out.split()[1:]
                        time.sleep(0.02)
                    raise AssertionError("no answer")
                h0 = ask("n1", "sha")
                self.assertEqual(ask("n2", "sdirty", "2"), "ok")
                h1 = ask("n3", "sha")
                self.assertNotEqual(h0, h1)
                self.assertEqual(ask("n4", "dirty", "4"), "ok")
                self.assertTrue(ask("n5", "sdirty", "8").startswith("ERR"))
                h2, ms = ask("n6", "shat")
                self.assertEqual(h2, ask("n7", "sha"))
                self.assertGreaterEqual(float(ms), 0)
            finally:
                proc.kill()
                proc.wait()

    def test_criu_log_parse(self):
        with tempfile.TemporaryDirectory() as store:
            d = os.path.join(store, "ck1_mem", "t0")
            os.makedirs(d)
            open(os.path.join(d, "criu-dump.log"), "w").write(
                "(00.000100) Dumping pid 123\n(00.000200) Dumping pages 77\n(00.010200) Writing image\n"
                "garbage\n(00.012200) Done\n")
            script = csw.CRIU_LOG_PY.replace(repr(csw.GUEST_STORE), repr(store))
            out = subprocess.run([sys.executable, "-c", script, "k", "ck1", "criu-dump.log"],
                                 capture_output=True, text=True, check=True).stdout
            res = json.loads(out)["k"]
            self.assertEqual(res["lines"], 4)
            self.assertAlmostEqual(res["span_ms"], 12.1, places=2)
            self.assertEqual(res["top"][0][0], "Dumping pages #")
            self.assertAlmostEqual(res["top"][0][1], 10.0, places=2)

    def test_criu_wrapper_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrap = os.path.join(tmp, "criu")
            open(wrap, "w").write((csw.CRIU_WRAP % 1).replace("/usr/sbin/criu.real", "echo"))
            out = subprocess.run(["bash", wrap, "dump", "--tree", "5", "-v4", "-D", "/x y"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(out, "dump --tree 5 -D /x y -v1")


if __name__ == "__main__":
    unittest.main()
