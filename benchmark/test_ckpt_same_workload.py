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
            self.assertNotIn("phases", pl.take("sbx2", mark, timeout=0))     # 旧 orchestrator 没有分段
            self.assertIsNone(pl.take_resume("sbx2", mark, timeout=0))

    def test_phase_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "alloc", "logs"))
            pl = csw.PauseLog.__new__(csw.PauseLog)
            pl.dir, pl.task = os.path.join(tmp, "alloc", "logs"), "start"
            mark = pl.mark()
            open(os.path.join(pl.dir, "start.stdout.0"), "w").write(
                '2026-11-20T10:00:00.000+0800  INFO  Pause timing  {"sandbox.id": "s1", "pause.snapshot_ms": 240.0, '
                '"pause.upload_ms": 60.0, "pause.phases": "acquired=0.2 vm_paused=0.4 snapfile=3.6 dirty_map=4.3 '
                'memfile=14.0 cache_ejected=14.2 stop_begin=14.3 fc_exited=170.0 uffd_stopped=171.0 '
                'nbd_released=200.0 rootfs_copied=225.0 rootfs_exported=226.0 rootfs_synced=231.0 rootfs_diff=231.1 '
                'metadata=231.5 cache_added=232.0 snapshot_done=240.0 uploaded=300.0 memfile_mib=12.0"}\n'
                '2026-11-20T10:00:01.000+0800  INFO  Resume phases  {"sandbox.id": "s1", "resume.snapshot": true, '
                '"resume.phases": "acquired=0.1 template=1.0 slot=1.1 rootfs_ready=1.5 memory_ready=2.0 fc_cmd=4.0 '
                'fc_started=6.0 rootfs_linked=12.0 uffd_socket=14.0 fc_api_ready=26.0 snapshot_loaded=31.0 '
                'vm_resumed=32.0 mmds=32.2 envd_ready=45.0 created=47.0"}\n')
            got = pl.take("s1", mark, timeout=0)
            self.assertEqual((got["snapshot"], got["upload"]), (240.0, 60.0))
            self.assertEqual(got["phases"]["fc_exited"], 170.0)
            self.assertEqual(got["phases"]["memfile_mib"], 12.0)
            ps = csw.phase_segments(got["phases"], csw.PAUSE_SEGMENTS)
            self.assertAlmostEqual(ps["等 FC 进程退出"], 156.0)
            self.assertAlmostEqual(ps["断开 NBD"], 30.0)
            self.assertAlmostEqual(ps["导出根盘差分"], 31.0)
            self.assertAlmostEqual(ps["上传"], 60.0)
            self.assertAlmostEqual(sum(v for k, v in ps.items() if k != "上传"), 240.0)
            rp = pl.take_resume("s1", mark, timeout=0)
            rs = csw.phase_segments(rp, csw.RESUME_SEGMENTS)
            self.assertAlmostEqual(rs["起 FC 进程到 API 可用"], 24.0)   # 并行的几件事取最后完成的
            self.assertAlmostEqual(rs["等 envd 就绪"], 12.8)
            self.assertAlmostEqual(sum(rs.values()), 47.0)
            self.assertIsNone(pl.take_resume("other", mark, timeout=0))
        # 缺了某一步：这一段不出现，后一段从上一段终点算
        self.assertEqual(csw.phase_segments({"acquired": 1.0, "snapfile": 5.0}, csw.PAUSE_SEGMENTS),
                         {"取沙箱": 1.0, "写 snapfile": 4.0})

    def test_pause_segments_with_stop_marks(self):
        # 新 orchestrator 在停 FC 里多三个时刻：ps 查状态、发 SIGTERM 各成一段
        ph = {"memfile": 25.0, "stop_begin": 25.0, "checks_stopped": 25.1, "fc_state_checked": 110.0,
              "fc_sigterm": 110.1, "fc_exited": 190.0, "nbd_released": 215.0}
        ps = csw.phase_segments(ph, csw.PAUSE_SEGMENTS)
        self.assertAlmostEqual(ps["ps 查 FC 进程状态"], 85.0)
        self.assertAlmostEqual(ps["发 SIGTERM"], 0.1)
        self.assertAlmostEqual(ps["等 FC 进程退出"], 79.9)
        self.assertAlmostEqual(ps["断开 NBD"], 25.0)

    def test_native_phase_summary(self):
        p = csw.norm({"P": 12, "d": 12, "f": 4})
        rows = []
        for rep in range(5):
            rows.append({"series": "mix", **p, "pass": 0, "rep": rep, "kind": "native", "ok": True,
                         "pause_wall_ms": 310.0 + rep, "pause_ms": 250.0, "pause_upload_ms": 60.0,
                         "resume_ms": 90.0, "resume_call_ms": 82.0, "ready_ms": 8.0,
                         "pause_phases": {"vm_paused": 1.0, "fc_exited": 150.0, "snapshot_done": 240.0,
                                          "uploaded": 300.0, "memfile_mib": 12.0},
                         "resume_phases": {"template": 1.0, "snapshot_loaded": 30.0, "envd_ready": 45.0,
                                           "created": 47.0}})
        e = csw.summarize(rows)[0]
        self.assertEqual(e["pause_orch_ms"]["p50"], 240.0)
        self.assertEqual(e["pause_api_ms"]["p50"], 12.0)
        self.assertEqual(e["resume_orch_ms"]["p50"], 47.0)
        self.assertEqual(e["resume_api_ms"]["p50"], 35.0)
        self.assertEqual(e["pause_segments"]["等 FC 进程退出"]["p50"], 149.0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            csw.print_native_phases([e])
        out = buf.getvalue()
        self.assertIn("API 与网络 35.0", out)
        self.assertIn("等 FC 进程退出 149.0", out)
        self.assertIn("导出内存 12.0 MiB", out)
        # 旧 orchestrator：没有分段，只打一行说明
        for r in rows:
            r["pause_phases"] = r["resume_phases"] = None
        buf = io.StringIO()
        with redirect_stdout(buf):
            csw.print_native_phases(csw.summarize(rows))
        self.assertIn("从略", buf.getvalue())

    def test_host_conditions_network(self):
        h = csw.host_conditions()
        for k in ("netns", "iptables_lines", "nft_lines", "firecracker_procs", "loadavg", "cpus", "mounts",
                  "nsfs_mounts", "cgroup"):
            self.assertIn(k, h)
        buf = io.StringIO()
        with redirect_stdout(buf):
            csw.print_host({"host_conditions": h, "host_conditions_end": h})
        out = buf.getvalue()
        self.assertIn("netns", out)
        self.assertIn("挂载点", out)
        self.assertIn("结束时", out)
        # v2 上的 cgroup 一行
        buf = io.StringIO()
        with redirect_stdout(buf):
            csw.print_host({"host_conditions": dict(h, cgroup="v2", e2b_cgroups=3, e2b_cgroups_dying=40,
                                                    e2b_controllers="cpu memory", cgroup_mkdir_ms=12.7)})
        self.assertIn("40 个已删未回收", buf.getvalue())
        self.assertIn("新建一个 12.7 ms", buf.getvalue())

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
