# -*- coding: utf-8 -*-
"""
T39 的判定逻辑离线单测（不碰栈、不建沙箱）。

判的是 `common.judge_pause_resume`（原生 pause → resume 之后的现场比对）和
`common.parse_log_fields`（服务端那行导出日志的取值），以及 t39 本身的结构
（场景表、跑起来要用的常量）。

    cd rollback-tests && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crtest import common      # noqa: E402


def snap(**kw):
    """一份"什么都对"的现场，测哪一项就改哪一项。"""
    d = {"mem_md5": "0123456789ab", "writer_pid": "421", "writer_state": "T",
         "mem_mark": "M0,M1,", "file_mark": "M0,M1,", "cmd": "alive"}
    d.update(kw)
    return d


class TestJudgePauseResume(unittest.TestCase):
    def test_identical_is_ok(self):
        ok, bad = common.judge_pause_resume(snap(), snap())
        self.assertTrue(ok, bad)
        self.assertEqual(bad, [])

    def test_memory_changed(self):
        ok, bad = common.judge_pause_resume(snap(), snap(mem_md5="ffffffffffff"))
        self.assertFalse(ok)
        self.assertTrue(any("内存 blob 变了" in b for b in bad), bad)

    def test_memory_unreadable(self):
        ok, bad = common.judge_pause_resume(snap(), snap(mem_md5="MISSING"))
        self.assertFalse(ok)
        self.assertTrue(any("读不到" in b for b in bad), bad)

    def test_pid_changed_is_a_reboot(self):
        ok, bad = common.judge_pause_resume(snap(), snap(writer_pid="99"))
        self.assertFalse(ok)
        self.assertTrue(any("虚机重启" in b for b in bad), bad)

    def test_writer_gone(self):
        ok, bad = common.judge_pause_resume(snap(), snap(writer_state="GONE"))
        self.assertFalse(ok)
        self.assertTrue(any("不在了" in b for b in bad), bad)

    def test_writer_state_changed(self):
        ok, bad = common.judge_pause_resume(snap(), snap(writer_state="S"))
        self.assertFalse(ok)
        self.assertTrue(any("状态变了" in b for b in bad), bad)

    def test_command_did_not_run(self):
        ok, bad = common.judge_pause_resume(snap(), snap(cmd=""))
        self.assertFalse(ok)
        self.assertTrue(any("命令没跑通" in b for b in bad), bad)

    # 场景 b：M1 是 create 之后写的，restore 回去就该没了，resume 之后也不该冒出来。
    def test_wanted_marks_are_enforced(self):
        before = snap(mem_mark="M0,", file_mark="M0,")
        want = {"mem_mark": "M0,", "file_mark": "M0,"}
        ok, bad = common.judge_pause_resume(before, snap(mem_mark="M0,", file_mark="M0,"), want)
        self.assertTrue(ok, bad)

        ok, bad = common.judge_pause_resume(before, snap(mem_mark="M0,M1,",
                                                        file_mark="M0,"), want)
        self.assertFalse(ok)
        self.assertTrue(any("mem_mark" in b for b in bad), bad)

    # 标记回到启动态（两份都 MISSING）是缺陷最直白的样子之一。
    def test_marks_back_to_boot_state(self):
        ok, bad = common.judge_pause_resume(snap(), snap(mem_mark="MISSING",
                                                        file_mark="MISSING"))
        self.assertFalse(ok)
        self.assertEqual(len([b for b in bad if "mark" in b]), 2, bad)

    def test_every_problem_is_reported(self):
        ok, bad = common.judge_pause_resume(
            snap(), snap(mem_md5="ff", writer_pid="9", writer_state="GONE", cmd="",
                         mem_mark="MISSING", file_mark="MISSING"))
        self.assertFalse(ok)
        self.assertEqual(len(bad), 6, bad)


class TestParseLogFields(unittest.TestCase):
    LINE = ('2026-09-18T05:06:40.123Z\tINFO\texporting the memfile diff of a native pause\t'
            '{"service": "orchestrator", "sandbox.id": "ifqwvdix6rc3gar0mnvh1", '
            '"pages": 91234, "page_size": 4096, "accumulated_pages": 88000, '
            '"bitmap_merges": 3, "accumulated_trusted": true}')

    def test_picks_the_fields(self):
        got = common.parse_log_fields(self.LINE)
        self.assertEqual(got["pages"], 91234)
        self.assertEqual(got["page_size"], 4096)
        self.assertEqual(got["accumulated_pages"], 88000)
        self.assertEqual(got["bitmap_merges"], 3)
        self.assertIs(got["accumulated_trusted"], True)
        self.assertEqual(got["sandbox.id"], "ifqwvdix6rc3gar0mnvh1")

    def test_keys_filter(self):
        got = common.parse_log_fields(self.LINE, ("pages", "source"))
        self.assertEqual(got, {"pages": 91234})

    def test_source_line(self):
        line = ('INFO\tmemfile diff page set for the native pause\t'
                '{"source": "tracked+accumulated", "pages": 91234, "tracked_pages": 3200, '
                '"accumulated_pages": 88034, "bitmap_merges": 3, "page_size": 4096}')
        got = common.parse_log_fields(line)
        self.assertEqual(got["source"], "tracked+accumulated")
        self.assertEqual(got["tracked_pages"], 3200)

    def test_empty_and_junk(self):
        self.assertEqual(common.parse_log_fields(""), {})
        self.assertEqual(common.parse_log_fields("[    8.42] BUG: Bad rss-counter state"), {})

    def test_float_and_escapes(self):
        got = common.parse_log_fields('{"ratio": 1.5, "reason": "sidecar \\"x\\" is gone"}')
        self.assertEqual(got["ratio"], 1.5)
        self.assertEqual(got["reason"], 'sidecar "x" is gone')


class TestCaseWiring(unittest.TestCase):
    def test_scene_table(self):
        from crtest.cases import t39
        self.assertEqual(sorted(t39.SCENE_FN), ["a", "b", "c"])
        # 两份标记：一份在 tmpfs（=guest 内存），一份在根文件系统（=NBD 写层）。
        self.assertTrue(t39.MEM_MARK.startswith("/dev/shm/"))
        self.assertTrue(t39.FILE_MARK.startswith(common.BENCH_DIR + "/"))

    def test_registered_in_main(self):
        from crtest import __main__ as m
        self.assertIn("T39", [c[0] for c in m.CASES])
        self.assertNotIn("T39", m.PENDING)


if __name__ == "__main__":
    unittest.main()
