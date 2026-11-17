"""本地单测：档的展开、含密钥文件的识别、终端汇报。不碰栈、不建沙箱。"""
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
