"""本地单测：档的展开与回传包的过滤。不碰栈、不建沙箱。"""
import unittest

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
        for name in ("job-before.json", ".job-cur.json", "job-restore.json", "x.env", "acl.token"):
            self.assertTrue(runner.secret(name), name)
        for name in ("SUMMARY.md", "job-state.md", "T21-seal_move.json", "summary.json", "special-job.log"):
            self.assertFalse(runner.secret(name), name)


if __name__ == "__main__":
    unittest.main()
