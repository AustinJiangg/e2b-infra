# -*- coding: utf-8 -*-
"""
T41 的判定逻辑离线单测（不碰栈、不建沙箱）。

判的是 `common.judge_clean_pause_resume`（干净沙箱 pause → resume 之后的现场
比对：在 T39 那套之上多判盘上文件的 md5 和心跳进程）以及 t41 本身的结构（场景表、
常量、注册进 __main__、复用 T39 的 pause/resume）。

    cd rollback-tests && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crtest import common      # noqa: E402


def snap(**kw):
    """一份"什么都对"的现场，测哪一项就改哪一项。"""
    d = {"mem_md5": "0123456789ab", "file_md5": "fedcba987654",
         "writer_pid": "421", "writer_state": "T",
         "mem_mark": "M0,", "file_mark": "M0,",
         "hb_pid": "77", "hb0": "1200", "hb1": "1207", "cmd": "alive"}
    d.update(kw)
    return d


class TestJudgeCleanPauseResume(unittest.TestCase):
    def test_identical_is_ok(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(hb0="1207", hb1="1214"))
        self.assertTrue(ok, bad)
        self.assertEqual(bad, [])

    # T39 那套判据照样生效（这里只抽查两条，其余在 test_t39.py 里）。
    def test_inherits_the_shared_judgements(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(mem_md5="ff", cmd=""))
        self.assertFalse(ok)
        self.assertTrue(any("内存 blob 变了" in b for b in bad), bad)
        self.assertTrue(any("命令没跑通" in b for b in bad), bad)

    def test_disk_file_changed(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(file_md5="00000000"))
        self.assertFalse(ok)
        self.assertTrue(any("盘上的文件变了" in b for b in bad), bad)

    def test_disk_file_unreadable(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(file_md5="MISSING"))
        self.assertFalse(ok)
        self.assertTrue(any("读不到盘上文件" in b for b in bad), bad)

    # 场景 b / c：这一轮改过的东西，按改完的样子判，不按上一轮。
    def test_wanted_file_md5_wins(self):
        ok, bad = common.judge_clean_pause_resume(
            snap(), snap(file_md5="newnewnewnew", hb0="1207", hb1="1210"),
            {"file_md5": "newnewnewnew"})
        self.assertTrue(ok, bad)

    def test_heartbeat_process_gone(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(hb_pid="MISSING"))
        self.assertFalse(ok)
        self.assertTrue(any("心跳进程的 pid 文件没了" in b for b in bad), bad)

    def test_heartbeat_pid_changed_is_a_reboot(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(hb_pid="9"))
        self.assertFalse(ok)
        self.assertTrue(any("虚机重启" in b for b in bad), bad)

    # 进程还在、但两拍之间一行都没多：vCPU 没真跑起来。
    def test_heartbeat_stalled(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(hb0="1207", hb1="1207"))
        self.assertFalse(ok)
        self.assertTrue(any("心跳行数不涨了" in b for b in bad), bad)

    # 倒退是 restore 的语义，不是 pause/resume 的。
    def test_heartbeat_went_backwards(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(hb0="900", hb1="907"))
        self.assertFalse(ok)
        self.assertTrue(any("心跳行数倒退了" in b for b in bad), bad)

    def test_heartbeat_unreadable(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(hb0="", hb1=""))
        self.assertFalse(ok)
        self.assertTrue(any("心跳行数读不出来" in b for b in bad), bad)

    # 场景 c 把"行数倒退/不涨"那一条滤掉再用，所以它必须是独立的一句话，
    # 不能和别的问题挤在同一条里。
    def test_heartbeat_problems_are_their_own_line(self):
        ok, bad = common.judge_clean_pause_resume(snap(), snap(hb0="900", hb1="907",
                                                              file_md5="00000000"))
        self.assertFalse(ok)
        rest = [b for b in bad if "心跳行数" not in b]
        self.assertEqual(len(rest), 1, bad)
        self.assertIn("盘上的文件变了", rest[0])

    def test_every_problem_is_reported(self):
        ok, bad = common.judge_clean_pause_resume(
            snap(), snap(mem_md5="ff", file_md5="MISSING", writer_pid="9",
                         writer_state="GONE", cmd="", mem_mark="MISSING",
                         file_mark="MISSING", hb_pid="MISSING", hb0="1207", hb1="1207"))
        self.assertFalse(ok)
        self.assertEqual(len(bad), 9, bad)


class TestJudgeStaleRestore(unittest.TestCase):
    """场景 d：restore 到 pause 之前那个 checkpoint，判它有没有被明确拒绝。

    服务端这一条是 `store.Get` 查不到 → 404 not_found + `checkpoint <id> not
    found`。判据只认 code + 文案：这条错误的 reason 和 code 同名，今天的 SDK 没有
    reason 字段时退化猜不出来，拿 reason 判会把一条正确的拒绝判成失败。
    """

    def info(self, **kw):
        d = {"code": "not_found", "reason": None, "reason_src": None,
             "message": "SandboxException: Code.not_found: checkpoint ckpt_1 not found"}
        d.update(kw)
        return d

    def test_refused_with_not_found_is_ok(self):
        from crtest.cases import t41
        ok, got = t41.judge_stale_restore(False, self.info())
        self.assertTrue(ok, got)
        self.assertIn("not_found", got)

    # 拒绝了才算数：成功回滚到上一代的 checkpoint 是最坏的结果（回滚到一个
    # 不属于这一代的差分链上），必须判失败。
    def test_succeeding_is_a_failure(self):
        from crtest.cases import t41
        ok, _ = t41.judge_stale_restore(True, {})
        self.assertFalse(ok)

    # 挂了 / 500 / 超时都不是「明确拒绝」。
    def test_internal_error_is_not_a_refusal(self):
        from crtest.cases import t41
        ok, _ = t41.judge_stale_restore(False, self.info(
            code="internal", message="SandboxException: Code.internal: failed to restore sandbox"))
        self.assertFalse(ok)

    def test_timeout_is_not_a_refusal(self):
        from crtest.cases import t41
        ok, _ = t41.judge_stale_restore(False, self.info(
            code=None, message="ReadTimeout: timed out"))
        self.assertFalse(ok)

    # 有 reason 字段的新 SDK 也走同一条判据（reason 只进记录）。
    def test_reason_field_does_not_change_the_judgement(self):
        from crtest.cases import t41
        ok, got = t41.judge_stale_restore(
            False, self.info(reason="not_found", reason_src="field"))
        self.assertTrue(ok, got)
        self.assertIn("field", got)

    # 今天的 SDK 真实拿到的形状：NotFoundException，文案里没有 `Code.` 前缀，
    # code 是按类名退化出来的（09-21 的 T41 现场就是这个样子）。判据必须认它。
    def test_todays_sdk_shape(self):
        from crtest.cases import t41
        info = common.error_info(
            "NotFoundException: checkpoint ckpt_1758000000000000000 not found")
        self.assertEqual(info["code"], "not_found")
        ok, got = t41.judge_stale_restore(False, info)
        self.assertTrue(ok, got)

    def test_empty_info_is_not_a_refusal(self):
        from crtest.cases import t41
        ok, _ = t41.judge_stale_restore(False, None)
        self.assertFalse(ok)


class TestCaseWiring(unittest.TestCase):
    def test_scene_table(self):
        from crtest.cases import t41
        self.assertEqual(sorted(t41.SCENE_FN), ["a", "b", "c", "d"])
        # 两份标记：一份在 tmpfs（=guest 内存），一份在根文件系统（=NBD 写层）；
        # 只记 md5 的那份文件也在写层上。
        self.assertTrue(t41.MEM_MARK.startswith("/dev/shm/"))
        self.assertTrue(t41.FILE_MARK.startswith(common.BENCH_DIR + "/"))
        self.assertTrue(t41.FILE_BLOB.startswith(common.BENCH_DIR + "/"))

    # 和 T39 用的是同一个 pause/resume（同样的 SDK 调用、同样的带上限重试）。
    def test_reuses_t39_pause_resume(self):
        from crtest.cases import t39, t41
        self.assertIs(t41.pause_resume, t39.pause_resume)

    # 路径不能和 T39 撞：两个用例的沙箱不同，但万一哪天在同一个沙箱里跑。
    def test_paths_do_not_collide_with_t39(self):
        from crtest.cases import t39, t41
        mine = {t41.MEM_FILE, t41.MEM_MARK, t41.FILE_MARK, t41.PID_MEM}
        theirs = {t39.MEM_FILE, t39.MEM_MARK, t39.FILE_MARK, t39.PID_MEM}
        self.assertEqual(mine & theirs, set())

    def test_registered_in_main(self):
        from crtest import __main__ as m
        self.assertIn("T41", [c[0] for c in m.CASES])
        self.assertNotIn("T41", m.PENDING)

    def test_args_are_wired(self):
        import argparse
        from crtest.cases import t41
        ap = argparse.ArgumentParser()
        common.add_common_args(ap)
        t41.add_args(ap)
        a = ap.parse_args([])
        self.assertEqual(a.scene, "all")
        self.assertEqual(a.rounds, 3)
        # 场景 d 也要能单跑（`--scene d`），否则复现这个缺陷得连跑四个场景。
        self.assertEqual(ap.parse_args(["--scene", "d"]).scene, "d")
        self.assertEqual(a.external, "")
        # pause 之后的 connect 重试参数：T41 自己也得有，它复用 T39 的 pause_resume。
        self.assertTrue(a.connect_attempts >= 1)
        self.assertTrue(a.connect_gap > 0)


if __name__ == "__main__":
    unittest.main()
