"""本地单测：档的展开、含密钥文件的识别、终端汇报。不碰栈、不建沙箱。"""
import json
import unittest
from pathlib import Path

import runner


class RunnerTests(unittest.TestCase):
    def ids(self, level, stacks, hours=8):
        return [s["id"] for s in runner.plan(level, stacks, hours)]

    def test_levels_nest(self):
        q = self.ids("quick", ["sandbox", "criu"])
        st = self.ids("standard", ["sandbox", "criu"])
        ex = self.ids("extended", ["sandbox", "criu"])
        self.assertEqual(q, ["sandbox/smoke", "criu/quick", "sandbox/smoke-after"])
        self.assertTrue(set(st) < set(ex))
        self.assertEqual(ex[-1], "sandbox/smoke-after")

    def test_sandbox_only_has_no_switch_back(self):
        self.assertEqual(self.ids("standard", ["sandbox"]),
                         ["sandbox/smoke", "sandbox/func", "sandbox/perf", "sandbox/hdbss"])

    def test_long_hours(self):
        steps = runner.plan("long", ["criu"], 2.5)
        self.assertEqual(steps[0]["args"], ["soak", "--hours", "2.5"])
        self.assertEqual(steps[0]["est_min"], 180)

    def test_perf_commands_stand_alone(self):
        # native、compare 不并入四档；每套一步，用各自的模板，进程级测完回沙箱级冒烟
        self.assertEqual(self.ids("native", ["sandbox", "criu"]),
                         ["sandbox/native", "criu/native", "sandbox/smoke-after"])
        self.assertEqual(self.ids("compare", ["sandbox"]), ["sandbox/compare"])
        steps = runner.plan("native", ["criu"], 4)
        self.assertEqual(steps[0]["kind"], "py")
        self.assertIn(runner.TEMPLATES["criu"], steps[0]["args"])
        self.assertNotIn("criu/native", self.ids("extended", ["sandbox", "criu"]))

    def test_compare_rounds_alternate(self):
        ids = [s["id"] for s in runner.plan("compare", ["sandbox", "criu"], 4, rounds=3)]
        self.assertEqual(ids, ["sandbox/compare-r1", "criu/compare-r1", "criu/compare-r2", "sandbox/compare-r2",
                               "sandbox/compare-r3", "criu/compare-r3", "criu/compare-table", "sandbox/smoke-after"])
        steps = runner.plan("compare", ["sandbox", "criu"], 4, rounds=1, series="dirty,filewait")
        self.assertIn("dirty,filewait", steps[0]["args"])
        self.assertIn("--criu-logs", steps[1]["args"])
        self.assertGreater(steps[0]["est_min"], 20)
        _, switches = runner.estimate(runner.plan("compare", ["sandbox", "criu"], 4, rounds=3))
        self.assertEqual(switches, 4)   # s→c、c→s、s→c，回沙箱级冒烟再 1 次；并列步骤跟在最后一个栈上不切
        self.assertEqual(self.ids("compare", ["criu"]), ["criu/compare", "sandbox/smoke-after"])

    def test_criu_files_stale(self):
        import tempfile, hashlib
        with tempfile.TemporaryDirectory() as d:
            dep, fc = Path(d) / "deploy", Path(d) / "fc"
            dep.mkdir(); fc.mkdir()
            (fc / "orchestrator").write_bytes(b"new")
            (fc / "gsd").write_bytes(b"old")
            sha = lambda b: hashlib.sha256(b).hexdigest()
            (dep / "manifest.json").write_text(json.dumps({"artifacts": {
                "orchestrator": {"path": "orchestrator", "sha256": sha(b"new")},
                "gsd": {"path": "gsd", "sha256": sha(b"newer")},
                "kernel": {"path": "kernels/x/vmlinux.bin", "sha256": sha(b"k")}}}))
            old = runner.DEPLOY, runner.stack.hfc
            runner.DEPLOY, runner.stack.hfc = dep, lambda: fc
            try:
                self.assertEqual(sorted(runner.criu_files_stale()), sorted([str(fc / "gsd"), str(fc / "kernels/x/vmlinux.bin")]))
            finally:
                runner.DEPLOY, runner.stack.hfc = old

    def test_dbm_level(self):
        # dbm 只在进程级栈：先验收（新内核默认开 DBM），再同一负载 off / on 各一轮，最后 A/B 表，回沙箱级冒烟
        self.assertEqual(self.ids("dbm", ["sandbox", "criu"]),
                         ["criu/acceptance", "criu/dbm-off", "criu/dbm-on", "criu/dbm-table", "sandbox/smoke-after"])
        steps = {s["id"]: s for s in runner.plan("dbm", ["criu"], 4)}
        self.assertEqual(steps["criu/dbm-off"]["args"][-2:], ["--soft-dirty-dbm", "0"])
        self.assertEqual(steps["criu/dbm-on"]["args"][-2:], ["--soft-dirty-dbm", "1"])
        self.assertIn("--no-native", steps["criu/dbm-on"]["args"])
        self.assertIn(runner.DBM_SERIES, steps["criu/dbm-on"]["args"])
        self.assertEqual({k for k, s in steps.items() if s.get("clean_netns")}, {"criu/dbm-off", "criu/dbm-on"})

    def test_fcexit_and_clean_netns(self):
        # fcexit 只在进程级栈上，测完回沙箱级冒烟；不管 --stack 给的是什么
        self.assertEqual(self.ids("fcexit", ["sandbox", "criu"]), ["criu/fcexit", "sandbox/smoke-after"])
        self.assertEqual(self.ids("fcexit", ["sandbox"]), ["criu/fcexit", "sandbox/smoke-after"])
        steps = runner.plan("fcexit", ["sandbox", "criu"], 4)
        self.assertEqual(steps[0]["args"], ["benchmark/fc_exit_trace.py"])
        # 性能测量步骤开测前清 netns；并列表、冒烟、功能测试不清
        clean = lambda level, st=("sandbox", "criu"): {s["id"] for s in runner.plan(level, list(st), 4)
                                                       if s.get("clean_netns")}
        self.assertEqual(clean("fcexit"), {"criu/fcexit"})
        self.assertEqual(clean("native"), {"sandbox/native", "criu/native"})
        self.assertEqual(clean("compare"), {"sandbox/compare", "criu/compare"})
        self.assertEqual(clean("standard"), {"sandbox/perf"})
        self.assertEqual(clean("extended"), {"sandbox/perf", "sandbox/perf-full"})
        # 预估把每次清理算进去
        steps = runner.plan("compare", ["sandbox", "criu"], 4)
        total, switches = runner.estimate(steps)
        base = sum(s["est_min"] for s in steps) + switches * runner.SWITCH_MIN
        self.assertEqual(total, base + (2 * runner.SWITCH_MIN if runner.CLEAN_NETNS else 0))

    def test_secret_filter(self):
        for name in ("job-before.json", ".job-cur.json", "job-restore.json", "x.env", "acl.token",
                     "baseline.json", "baseline.sha256"):
            self.assertTrue(runner.report.secret(name), name)
        for name in ("SUMMARY.md", "job-state.md", "T21-seal_move.json", "summary.json", "special-job.log"):
            self.assertFalse(runner.report.secret(name), name)

    def test_run_sh_summary_rows(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20261117-000000-perf"
            d.mkdir()
            (d / "SUMMARY.md").write_text("| 结果 | **PASS 1 · FAIL 0** |\n\n## 逐项\n\n| 项 | 结果 | 耗时 | 关键数字 / 原因 | 产物 |\n"
                                          "|---|---|---|---|---|\n| 串行 restore | PASS | 3s | p50 8.97 ms | serial-restore.log |\n"
                                          "\n## 怎么交回\n")
            result, rows = runner.report.run_sh_summary(tmp)
        self.assertEqual(result, "PASS 1 · FAIL 0")
        self.assertEqual(rows, [("串行 restore", "PASS", "3s", "p50 8.97 ms", "serial-restore.log")])


if __name__ == "__main__":
    unittest.main()
