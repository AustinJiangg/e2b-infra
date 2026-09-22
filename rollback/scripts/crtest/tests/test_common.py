#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crtest 公共库的单测：只测**纯函数**（解析、分位数、判定、断言记账）。

这些用例必须能在没装 e2b / dotenv 的机器（比如 WSL）上跑过 —— 所以顺带断言
「import crtest 不会把 e2b 拉进来」。跑法：

    cd rollback-tests && python3 -m unittest discover -s tests -v
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crtest import common      # noqa: E402


def fake_ctx():
    """够 expect()/note() 用的最小 ctx。"""
    return types.SimpleNamespace(results={"assertions": [], "ops": []})


class TestStats(unittest.TestCase):
    def test_p50_pmax(self):
        self.assertEqual(common.p50([3, 1, 2]), 2)
        self.assertEqual(common.pmax([3, 1, None, 2]), 3)
        self.assertIsNone(common.p50([]))
        self.assertIsNone(common.pmax([None, None]))

    def test_quantile(self):
        xs = list(range(1, 101))
        self.assertEqual(common.quantile(xs, 0.5), 51)     # round(0.5*99)=50 → xs[50]
        self.assertEqual(common.quantile(xs, 0.99), 99)
        self.assertEqual(common.quantile([5], 0.99), 5)
        self.assertIsNone(common.quantile([], 0.5))

    def test_as_int(self):
        self.assertEqual(common.as_int(" 42 "), 42)
        self.assertIsNone(common.as_int(""))
        self.assertIsNone(common.as_int(None))
        self.assertIsNone(common.as_int("x"))

    def test_fmt(self):
        self.assertEqual(common.fmt_ms(None), "-")
        self.assertEqual(common.fmt_s(1.23456), "1.235")


class TestParsers(unittest.TestCase):
    def test_parse_kv(self):
        d = common.parse_kv("a=1\nb = two = 2\nnoise\n")
        self.assertEqual(d["a"], "1")
        self.assertEqual(d["b"], "two = 2")
        self.assertNotIn("noise", d)

    def test_proc_stat(self):
        d = common.parse_proc_stat_line("cpu0 100 0 50 900 10 0 0 0 0 0")
        self.assertEqual(d["name"], "cpu0")
        self.assertEqual(d["idle"], 900)
        self.assertEqual(d["iowait"], 10)
        self.assertEqual(common.parse_proc_stat_line("intr 1 2 3"), {})
        self.assertEqual(common.parse_proc_stat_line(""), {})

    def test_proc_stat_short_line(self):
        d = common.parse_proc_stat_line("cpu0 1 2 3 4")
        self.assertEqual(d["idle"], 4)
        self.assertEqual(d["steal"], 0)

    def test_cpu_idle_ratio(self):
        a = common.parse_proc_stat_line("cpu0 100 0 100 800 0 0 0 0")
        b = common.parse_proc_stat_line("cpu0 110 0 110 980 0 0 0 0")
        self.assertAlmostEqual(common.cpu_idle_ratio(a, b), 180 / 200.0)
        self.assertIsNone(common.cpu_idle_ratio(a, a))      # 没走 jiffy = 没数据
        self.assertIsNone(common.cpu_idle_ratio({}, b))

    def test_interrupt_counts(self):
        text = ("11:      1000       2000     GICv3  27 Level     arch_timer|"
                "12:        10         20     GICv3  26 Level     arch_timer")
        self.assertEqual(common.parse_interrupt_counts(text), [1010, 2020])
        self.assertEqual(common.parse_interrupt_counts(""), [])

    def test_interrupt_counts_ragged(self):
        # 行与行的 CPU 列数不一致时按最长的补齐。
        self.assertEqual(common.parse_interrupt_counts("1: 5 arch_timer|2: 1 2 arch_timer"),
                         [6, 2])

    def test_parse_heartbeat(self):
        s = "1.0 0.5\nbad line here\n2.0 1.5\n"
        self.assertEqual(common.parse_heartbeat(s), [(1.0, 0.5), (2.0, 1.5)])


class TestHeartbeatGaps(unittest.TestCase):
    """段内一律用 monotonic 相邻差，并丢掉每段第一跳（09-17 口径修正）。

    真实形状：restore 把心跳文件也回滚 → monotonic 倒退（切段点）；envd 把
    realtime 拨回当前发生在**倒退之后**，那一跳落在新段**段内** —— 所以段内绝对
    不能用 realtime 量间隔。
    """

    def test_realtime_jump_inside_segment_is_not_a_stall(self):
        samples = [
            # 第 1 段：10 ms 一条，中间卡了一次 30 ms
            (100.00, 10.00), (100.01, 10.01), (100.02, 10.02), (100.05, 10.05),
            # restore：文件回滚到快照那一刻（mono 倒退），realtime 随后被拨回当前
            (100.02, 10.02), (200.00, 10.03), (200.01, 10.04), (200.21, 10.24),
        ]
        g = common.heartbeat_gaps(samples)
        self.assertEqual(g["segments"], 2)
        # 段内真实停顿只有 30 ms 和 200 ms；100 s 的 realtime 拨钟不算
        self.assertAlmostEqual(g["max_gap_s"], 0.20, places=6)
        self.assertAlmostEqual(g["gaps"][0], 0.03, places=6)
        self.assertEqual(g["intervals"], 4)          # 每段 3 个间隔，各丢掉第一个

    def test_first_interval_of_segment_is_dropped(self):
        # 段首那条是回滚回来的旧记录，它到下一条跨的是"回滚"本身，不是停顿。
        g = common.heartbeat_gaps([(1.0, 1.0), (1.0, 6.0), (1.01, 6.01), (1.02, 6.02)])
        self.assertAlmostEqual(g["max_gap_s"], 0.01, places=6)

    def test_ramp_bug_shape(self):
        """复现第 1 轮 T13 的错数据形状：13 段，每段 realtime 拨回量线性递增。

        旧口径（段内取 realtime 相邻最大差）会给出 5.2、10.4 … 67.6 s；
        新口径必须只看到 10 ms 级的段内间隔。
        """
        samples = []
        mono = 100.0
        for k in range(13):
            rt0 = 1000.0 + 5.2 * k
            samples.append((1000.0, mono))            # 段首：回滚回来的旧记录
            mono += 0.01
            for i in range(5):                        # 拨钟之后正常走
                samples.append((rt0 + 0.01 * i, mono))
                mono += 0.01
            mono -= 0.06                              # 下一次 restore：mono 倒退
        g = common.heartbeat_gaps(samples)
        self.assertEqual(g["segments"], 13)
        self.assertLess(g["max_gap_s"], 0.05)
        self.assertLess(g["p99_ms"], 50.0)

    def test_p99_and_over_threshold_count(self):
        samples = [(1.0, 1.0), (1.0, 2.0)]            # 段首那跳，丢掉
        mono = 2.0
        for i in range(100):
            mono += 0.5 if i == 99 else 0.01          # 最后一跳 500 ms
            samples.append((1.0, mono))
        g = common.heartbeat_gaps(samples)
        self.assertEqual(g["intervals"], 100)
        self.assertEqual(g["gt_ms"], 100.0)
        self.assertEqual(g["gt_count"], 1)            # 只有那 500 ms 超过 100 ms
        self.assertAlmostEqual(g["max_gap_s"], 0.5, places=6)
        # p99 不被单个离群点拽走（100 个间隔里 round(0.99*99)=98 → 第 99 小那个）
        self.assertAlmostEqual(g["p99_ms"], 10.0, places=3)
        self.assertEqual(common.heartbeat_gaps(samples, gt_ms=1000.0)["gt_count"], 0)

    def test_single_segment(self):
        g = common.heartbeat_gaps([(1.0, 1.0), (1.01, 1.01), (1.5, 1.5), (1.52, 1.52)])
        self.assertEqual(g["segments"], 1)
        self.assertAlmostEqual(g["max_gap_s"], 0.49, places=6)

    def test_short_segments_are_skipped(self):
        # 丢掉第一跳之后什么也不剩的段不算一段。
        g = common.heartbeat_gaps([(1.0, 1.0), (1.0, 2.0), (1.0, 0.5), (1.0, 0.6)])
        self.assertEqual(g["segments"], 0)
        self.assertIsNone(g["max_gap_s"])
        self.assertIsNone(g["p99_ms"])

    def test_empty(self):
        g = common.heartbeat_gaps([])
        self.assertIsNone(g["max_gap_s"])
        self.assertEqual(g["samples"], 0)


class TestJudgeT25(unittest.TestCase):
    def sample(self, uptime, epoch, mono, idle, timer, sleep1=1.0):
        return {"uptime": uptime, "epoch": epoch, "mono": mono, "sleep1": sleep1,
                "cpu0": common.parse_proc_stat_line("cpu0 0 0 0 %d 0 0 0 0" % idle),
                "timer": timer}

    def good(self):
        before = self.sample(500.0, 1000.0, 500.0, 0, [0, 0])
        after = self.sample(120.0, 1010.0, 120.0, 1000, [1000, 1000])
        # 10 s 窗口：CPU0 只走了 1000 jiffy 的 idle（全空闲），定时器涨 500 次 → 50/s
        later = self.sample(130.0, 1020.0, 130.0, 2000, [1500, 1400])
        return before, after, later

    def test_all_pass(self):
        before, after, later = self.good()
        res = common.judge_t25(before, after, later, host_epoch=1010.2)
        self.assertTrue(all(ok for _, ok, _, _ in res), res)

    def test_uptime_not_rewound_fails(self):
        before, after, later = self.good()
        after["uptime"] = 600.0
        res = common.judge_t25(before, after, later, host_epoch=1010.2)
        self.assertFalse(res[0][1])

    def test_date_skew_fails(self):
        before, after, later = self.good()
        res = common.judge_t25(before, after, later, host_epoch=1200.0)
        self.assertFalse(res[1][1])

    def test_sleep_off_fails(self):
        before, after, later = self.good()
        after["sleep1"] = 1.4
        res = common.judge_t25(before, after, later, host_epoch=1010.2)
        self.assertFalse(res[2][1])

    def test_busy_cpu_fails(self):
        before, after, later = self.good()
        later["cpu0"] = common.parse_proc_stat_line("cpu0 900 0 0 1100 0 0 0 0")
        res = common.judge_t25(before, after, later, host_epoch=1010.2)
        self.assertFalse([r for r in res if "CPU0" in r[0]][0][1])

    def test_timer_storm_fails(self):
        before, after, later = self.good()
        later["timer"] = [1000 + 5000, 1000]        # 10 s 内涨 5000 → 500/s
        res = common.judge_t25(before, after, later, host_epoch=1010.2)
        self.assertFalse([r for r in res if "arch_timer" in r[0]][0][1])

    def test_timer_rate_needs_time(self):
        a = {"timer": [0], "mono": 5.0}
        self.assertIsNone(common.timer_rate(a, {"timer": [100], "mono": 5.0}))
        self.assertAlmostEqual(common.timer_rate(a, {"timer": [100], "mono": 15.0}), 10.0)


class TestJudgeT32(unittest.TestCase):
    def rows(self, mem, bm):
        return [{"dirty_mb": d, "fc_memory": [m], "fc_bitmap": [b]}
                for d, m, b in zip((0, 64, 256), mem, bm)]

    def test_pass(self):
        res = common.judge_t32(self.rows([5.0, 20.0, 80.0], [3.0, 3.2, 3.1]))
        self.assertTrue(all(ok for _, ok, _, _ in res), res)

    def test_bitmap_scales_fails(self):
        res = common.judge_t32(self.rows([5.0, 20.0, 80.0], [3.0, 12.0, 40.0]))
        self.assertFalse(res[0][1])

    def test_memory_flat_fails(self):
        res = common.judge_t32(self.rows([5.0, 5.0, 5.0], [3.0, 3.0, 3.0]))
        self.assertFalse(res[1][1])

    def test_memory_inverted_fails(self):
        res = common.judge_t32(self.rows([80.0, 20.0, 5.0], [3.0, 3.0, 3.0]))
        self.assertFalse(res[1][1])

    def test_missing_bitmap_reports_reason(self):
        rows = self.rows([5.0, 20.0, 80.0], [None, None, None])
        res = common.judge_t32(rows)
        self.assertFalse(res[0][1])
        self.assertIn("timings_us.bitmap", res[0][3])

    def test_unsorted_rows_are_sorted(self):
        rows = list(reversed(self.rows([5.0, 20.0, 80.0], [3.0, 3.0, 3.0])))
        res = common.judge_t32(rows)
        self.assertTrue(all(ok for _, ok, _, _ in res), res)


class TestJudgePageScan(unittest.TestCase):
    """T14 撕裂判据：坏页 ⊆ {快照时刻的进行中页} 才容忍。"""

    def test_clean(self):
        ok, tol, _ = common.judge_page_scan("0", [], "-1")
        self.assertTrue(ok)
        self.assertFalse(tol)

    def test_single_bad_page_is_the_in_progress_page(self):
        ok, tol, detail = common.judge_page_scan("1", ["42451"], "42451")
        self.assertTrue(ok)
        self.assertTrue(tol)
        self.assertIn("42451", detail)

    def test_two_bad_pages_fail(self):
        ok, tol, detail = common.judge_page_scan("2", ["42451", "7"], "42451")
        self.assertFalse(ok)
        self.assertFalse(tol)
        self.assertIn("≥ 2", detail)

    def test_single_bad_page_without_in_progress_fails(self):
        ok, tol, _ = common.judge_page_scan("1", ["42451"], "-1")
        self.assertFalse(ok)
        self.assertFalse(tol)

    def test_single_bad_page_elsewhere_fails(self):
        ok, tol, _ = common.judge_page_scan("1", ["7"], "42451")
        self.assertFalse(ok)
        self.assertFalse(tol)

    def test_missing_count_fails(self):
        ok, _, _ = common.judge_page_scan(None, [], "5")
        self.assertFalse(ok)

    def test_count_without_list_fails(self):
        ok, _, _ = common.judge_page_scan("1", [], "5")
        self.assertFalse(ok)


class TestJudgeLineScan(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(common.judge_line_scan("0", "0")[:2], (True, False))

    def test_tail_partial_tolerated(self):
        ok, tol, _ = common.judge_line_scan("0", "1")
        self.assertTrue(ok)
        self.assertTrue(tol)

    def test_bad_line_fails(self):
        ok, tol, _ = common.judge_line_scan("1", "0")
        self.assertFalse(ok)
        self.assertFalse(tol)

    def test_missing_count_fails(self):
        self.assertFalse(common.judge_line_scan(None, "0")[0])


class TestAssertions(unittest.TestCase):
    def test_expect_records_and_passes(self):
        ctx = fake_ctx()
        common.expect(ctx, "名字", True, "期望", "实际", "依据")
        self.assertEqual(len(ctx.results["assertions"]), 1)
        self.assertTrue(ctx.results["assertions"][0]["ok"])

    def test_expect_raises_three_part(self):
        ctx = fake_ctx()
        with self.assertRaises(common.Failed) as cm:
            common.expect(ctx, "串口可写", False, "退出码 0", "退出码 124", "方案 S1")
        text = cm.exception.text()
        for part in ("期望：退出码 0", "实际：退出码 124", "依据：方案 S1"):
            self.assertIn(part, text)
        self.assertFalse(ctx.results["assertions"][0]["ok"])

    def test_note_is_not_a_judgement(self):
        ctx = fake_ctx()
        common.note(ctx, "曲线", "只记录", "1.2 s", "T13")
        self.assertIsNone(ctx.results["assertions"][0]["ok"])


class TestErrorInfo(unittest.TestCase):
    """错误分型（T11/T21）。样本文案抄服务端原文（service.go / faults.go）。"""

    TORN = ("SandboxException: Code.data_loss: restore failed past the commit point; "
            "the sandbox must be recreated: open /o/b/c/s1/fault-injected/torn_assemble: "
            "no such file or directory")
    ENVD = ("SandboxException: Code.internal: the guest never answered after the rollback\n"
            "sandbox restored but envd did not come back after 45s: fault injected: the guest "
            "never answered after the rollback (CHECKPOINT_FAULT_INJECT=envd_timeout)")
    POISON = ("SandboxException: Code.internal: failed to record rootfs layer: "
              "rootfs bookkeeping is poisoned")

    def test_parse_error_text(self):
        self.assertEqual(common.parse_error_text(self.TORN)[0], "data_loss")
        self.assertEqual(common.parse_error_text('{"code":"aborted","reason":"sandbox_restored"}'),
                         ("aborted", "sandbox_restored"))
        self.assertEqual(common.parse_error_text("boom"), (None, None))

    def test_guess_reason(self):
        self.assertEqual(common.guess_reason("data_loss", ""), "torn")
        self.assertEqual(common.guess_reason("internal", self.ENVD), "guest_unresponsive")
        self.assertEqual(common.guess_reason("internal", self.POISON), "rootfs_poisoned")
        self.assertEqual(common.guess_reason("failed_precondition", ""), "chain_broken")
        self.assertIsNone(common.guess_reason("internal", "something else"))

    def test_error_info_degrades_without_reason_field(self):
        i = common.error_info(self.ENVD)
        self.assertEqual((i["code"], i["reason"], i["reason_src"]),
                         ("internal", "guest_unresponsive", "guess"))
        self.assertTrue(i["fault_injected"])
        self.assertTrue(common.error_info(self.TORN)["fault_injected"])   # fault-injected 也算

    def test_error_info_uses_fields_when_sdk_grows_them(self):
        class Fancy(Exception):
            reason = "torn"
            status = "data_loss"

        i = common.error_info(Fancy("sandbox torn"))
        self.assertEqual((i["code"], i["reason"], i["reason_src"]), ("data_loss", "torn", "field"))
        self.assertTrue(common.reason_ok(i, "torn")[0])
        self.assertFalse(common.reason_ok(i, "busy")[0])

    def test_error_info_class_fallback(self):
        class NotFoundException(Exception):
            pass

        self.assertEqual(common.error_info(NotFoundException("nope"))["code"], "not_found")

    def test_error_info_on_exception_object(self):
        i = common.error_info(Exception("Code.internal: boom"))
        self.assertEqual(i["type"], "Exception")
        self.assertFalse(i["fault_injected"])

    # ---- 第 4 轮修的两条：异常对象上的字段要真读到，checkpoint 类要认得

    def test_error_info_reads_fields_off_the_exception_object(self):
        """SDK 第 3 轮的 checkpoint 异常：对象上有 `.reason`（没有 code / status）。"""
        class CheckpointBusyException(Exception):
            reason = "busy"
            retry_after = 1.5

        i = common.error_info(CheckpointBusyException("another operation is running"))
        self.assertEqual((i["code"], i["status"], i["reason"], i["reason_src"]),
                         ("unavailable", "unavailable", "busy", "field"))   # code 按类名补
        self.assertEqual(i["retry_after"], 1.5)

    def test_error_info_prefers_field_over_text_and_guess(self):
        """文案说 internal、对象说 rootfs_poisoned —— 以对象为准，出处是 field。"""
        class CheckpointRootfsPoisonedException(Exception):
            reason = "rootfs_poisoned"

        i = common.error_info(CheckpointRootfsPoisonedException(
            "Code.internal: failed to record rootfs layer"))
        self.assertEqual((i["code"], i["reason"], i["reason_src"]),
                         ("internal", "rootfs_poisoned", "field"))

    def test_error_info_checkpoint_classes_by_name(self):
        """只剩一条 `类名: 文案` 的串（老 JSON 回放）时，按类名也要认出 code/reason，
        但出处只能是 guess —— 那不是字段。"""
        want = {"CheckpointException": ("internal", None),
                "CheckpointTornException": ("data_loss", "torn"),
                "CheckpointChainBrokenException": ("failed_precondition", "chain_broken"),
                "CheckpointRootfsPoisonedException": ("internal", "rootfs_poisoned"),
                "CheckpointGuestUnresponsiveException": ("internal", "guest_unresponsive"),
                "CheckpointBusyException": ("unavailable", "busy"),
                "CheckpointInterruptedException": ("aborted", "sandbox_restored")}
        for cls, (code, reason) in want.items():
            i = common.error_info("%s: something went wrong" % cls)
            self.assertEqual((i["code"], i["reason"]), (code, reason), cls)
            self.assertEqual(i["reason_src"], None if reason is None else "guess", cls)

    def test_error_info_old_style_string_still_guesses(self):
        """老样式（SandboxException + `Code.<x>:` 文案）不受影响，仍走 guess。"""
        i = common.error_info(self.ENVD)
        self.assertEqual(i["reason_src"], "guess")
        self.assertIsNone(i["retry_after"])
        j = common.error_info(Exception('{"code":"aborted","reason":"sandbox_restored"}'))
        self.assertEqual((j["code"], j["reason"], j["reason_src"]),
                         ("aborted", "sandbox_restored", "text"))

    def test_error_info_old_sdk_exception_object_does_not_crash(self):
        """老 SDK 的异常对象上根本没有 reason 属性 —— 不许崩，退化到文案 / 猜。"""
        class SandboxException(Exception):
            pass

        i = common.error_info(SandboxException("Code.data_loss: past the commit point"))
        self.assertEqual((i["code"], i["reason"], i["reason_src"]),
                         ("data_loss", "torn", "guess"))

    def _sdk_exceptions(self):
        """真 SDK 的异常模块（jll-e2b 里有）。拿完把 e2b 从 sys.modules 里撤干净：
        本文件另有一条「import crtest 不会把 e2b 拉进来」的断言，不能被这里污染。"""
        before = set(sys.modules)

        def purge():
            for m in list(sys.modules):
                if m not in before and (m == "e2b" or m.startswith("e2b")):
                    sys.modules.pop(m, None)

        self.addCleanup(purge)
        try:
            from e2b import exceptions as sdk
        except Exception as e:      # noqa: BLE001
            purge()
            self.skipTest("这台机器没装 e2b（WSL 上正常）：%s" % e)
        if not hasattr(sdk, "CheckpointTornException"):
            self.skipTest("装的是没有 checkpoint 异常层次的旧版 e2b")
        return sdk

    def test_error_info_with_real_sdk_checkpoint_exceptions(self):
        sdk = self._sdk_exceptions()
        want = [("CheckpointException", "internal", None),
                ("CheckpointTornException", "data_loss", "torn"),
                ("CheckpointChainBrokenException", "failed_precondition", "chain_broken"),
                ("CheckpointRootfsPoisonedException", "internal", "rootfs_poisoned"),
                ("CheckpointGuestUnresponsiveException", "internal", "guest_unresponsive"),
                ("CheckpointBusyException", "unavailable", "busy"),
                ("CheckpointInterruptedException", "aborted", "sandbox_restored")]
        for name, code, reason in want:
            exc = getattr(sdk, name)("boom", checkpoint_id="ckpt_1", sandbox_id="sbx_1")
            i = common.error_info(exc)
            self.assertEqual((i["type"], i["code"], i["status"], i["reason"]),
                             (name, code, code, reason), name)
            # 基类没有 _default_reason，reason 本来就是 None；其余都得是字段来的
            self.assertEqual(i["reason_src"], None if reason is None else "field", name)
            if reason is not None:
                self.assertTrue(common.reason_ok(i, reason)[0], name)
        busy = sdk.CheckpointBusyException("busy", retry_after=2.5)
        self.assertEqual(common.error_info(busy)["retry_after"], 2.5)

    def test_error_info_with_stubbed_sdk_module(self):
        """没装 e2b 的机器上也把同一条路走一遍：拿 sys.modules 打个桩。"""
        from unittest import mock
        import types as _types

        e2b = _types.ModuleType("e2b")
        exc_mod = _types.ModuleType("e2b.exceptions")

        class CheckpointTornException(Exception):
            _default_reason = "torn"

            def __init__(self, message, reason=None, checkpoint_id=None, sandbox_id=None):
                Exception.__init__(self, message)
                self.reason = reason if reason is not None else self._default_reason
                self.checkpoint_id = checkpoint_id
                self.sandbox_id = sandbox_id

        exc_mod.CheckpointTornException = CheckpointTornException
        e2b.exceptions = exc_mod
        with mock.patch.dict(sys.modules, {"e2b": e2b, "e2b.exceptions": exc_mod}):
            from e2b.exceptions import CheckpointTornException as Torn
            i = common.error_info(Torn("restore failed past the commit point",
                                       checkpoint_id="ckpt_1"))
        self.assertEqual((i["code"], i["reason"], i["reason_src"]),
                         ("data_loss", "torn", "field"))
        self.assertNotIn("e2b", sys.modules)

    def test_record_error_keeps_the_object_fields(self):
        """`record_error()`：串给人看，结构化的那份给判定用。"""
        class CheckpointInterruptedException(Exception):
            reason = "sandbox_restored"

        rec = {"op": "run_cmd"}
        common.record_error(rec, CheckpointInterruptedException("call cut short"))
        self.assertEqual(rec["err"], "CheckpointInterruptedException: call cut short")
        self.assertEqual((rec["error_info"]["code"], rec["error_info"]["reason"],
                          rec["error_info"]["reason_src"]),
                         ("aborted", "sandbox_restored", "field"))
        # 记录里已有结构化结果时，rec_error_info 不许再从字符串重算
        self.assertIs(common.rec_error_info(rec), rec["error_info"])

    def test_ctx_op_backfills_error_info(self):
        """别处拼的记录（只有 err 串）由 ctx.op 兜底补一份，出处退化成 guess。"""
        ctx = common.Ctx(None, None, None, None, None, {"ops": [], "assertions": []})
        rec = ctx.op({"op": "create", "ok": False, "err": self.TORN})
        self.assertEqual(rec["error_info"]["reason"], "torn")
        self.assertEqual(rec["error_info"]["reason_src"], "guess")
        self.assertIs(ctx.results["ops"][0], rec)
        ok_rec = ctx.op({"op": "create", "ok": True})
        self.assertNotIn("error_info", ok_rec)


class TestJudgeT11(unittest.TestCase):
    def test_op_outcomes(self):
        ok, _, got = common.judge_t11_op(True, None, 3.0, 200.0)
        self.assertTrue(ok)
        self.assertIn("成功", got)
        ok, _, got = common.judge_t11_op(False, "Code.not_found: gone", 1.0, 200.0)
        self.assertTrue(ok)                       # 明确错误也算过
        self.assertIn("not_found", got)
        self.assertFalse(common.judge_t11_op(False, "", 1.0, 200.0)[0])          # 没说法
        self.assertFalse(common.judge_t11_op(False, "ReadTimeout", 199.0, 200.0)[0])  # 客户端超时
        self.assertFalse(common.judge_t11_op(False, "x", None, 200.0)[0])        # 线程没结束
        self.assertFalse(common.judge_t11_op(True, None, 201.0, 200.0)[0])       # 超线

    def test_reclaim(self):
        rows = common.judge_reclaim(True, [], 10, 10)
        self.assertEqual([r[1] for r in rows], [True, True, True])
        rows = common.judge_reclaim(False, [123], 10, 11)
        self.assertEqual([r[1] for r in rows], [False, False, False])
        self.assertIsNone(common.judge_reclaim(True, [], None, 5)[2][1])
        self.assertTrue(common.judge_reclaim(True, [], 10, 9)[2][1])   # 少了也算不增长


class TestJudgeT21(unittest.TestCase):
    def test_hidden_rescue(self):
        self.assertTrue(common.judge_hidden_rescue(
            {"state": "committed", "hidden": True})[0])
        self.assertFalse(common.judge_hidden_rescue(
            {"state": "committed", "hidden": False})[0])
        self.assertFalse(common.judge_hidden_rescue(
            {"state": "prepared", "hidden": True})[0])
        self.assertFalse(common.judge_hidden_rescue(None)[0])

    def test_chain_incremental(self):
        first = {"id": "ckpt_1", "hidden": True}
        ok, _, got = common.judge_chain_incremental(
            first, {"id": "ckpt_2", "parent_id": "ckpt_1", "mem_mode": "incremental"})
        self.assertTrue(ok)
        self.assertIn("incremental", got)
        # 链断了：服务端改走全量新根
        self.assertFalse(common.judge_chain_incremental(
            first, {"id": "ckpt_2", "parent_id": "", "mem_mode": "full"})[0])
        self.assertFalse(common.judge_chain_incremental(first, None)[0])


class FakeClock:
    """假时钟：`sleep()` 只是把表往前拨，单测不用真等 5 s。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeOrch:
    """`OrchLog` 的替身：前 `hit_on - 1` 次读是空的（logmon 还没刷盘），
    第 `hit_on` 次才把那一行刷出来。"""

    dir = "/fake/logs"

    def __init__(self, hit_on=3, line="orchestrator: sealing the write layer failed; …"):
        self.hit_on = hit_on
        self.line = line
        self.reads = 0
        self.patterns = []

    def since(self, pattern=None):
        self.reads += 1
        self.patterns.append(pattern)
        return [self.line] if self.reads >= self.hit_on else []


class TestLogWait(unittest.TestCase):
    """nomad logmon 落盘滞后 0.05–1.3 s（批刷约 1.6 s），所以日志判定要轮询重读。"""

    def test_hits_on_the_third_read(self):
        orch, clk = FakeOrch(hit_on=3), FakeClock()
        lines, waited, reads = common.wait_for_log(
            orch, "sealing", timeout=5.0, interval=0.5, sleep=clk.sleep, clock=clk)
        self.assertEqual(lines, [orch.line])
        self.assertEqual(reads, 3)
        self.assertAlmostEqual(waited, 1.0)          # 两次 0.5 s 的间隔
        self.assertEqual(orch.patterns, ["sealing"] * 3)

    def test_first_read_hits_without_sleeping(self):
        orch, clk = FakeOrch(hit_on=1), FakeClock()
        lines, waited, reads = common.wait_for_log(
            orch, None, timeout=5.0, interval=0.5, sleep=clk.sleep, clock=clk)
        self.assertTrue(lines)
        self.assertEqual((reads, waited), (1, 0.0))

    def test_gives_up_after_the_timeout(self):
        orch, clk = FakeOrch(hit_on=999), FakeClock()
        lines, waited, reads = common.wait_for_log(
            orch, "nope", timeout=5.0, interval=0.5, sleep=clk.sleep, clock=clk)
        self.assertEqual(lines, [])
        self.assertAlmostEqual(waited, 5.0)
        self.assertEqual(reads, 11)                  # 0 s 读一次 + 每 0.5 s 一次

    def test_log_expect_polls_and_says_how_long_it_waited(self):
        from crtest.cases import t21
        ctx, orch = fake_ctx(), FakeOrch(hit_on=3)
        lines = t21._log_expect(ctx, orch, "sealing", "服务端打了封层失败那一行",
                                "一行 sealing the write layer failed", "单测",
                                timeout=5.0, interval=0.0)
        self.assertTrue(lines)
        self.assertEqual(orch.reads, 3)              # 前两次空读没被当成失败
        a = ctx.results["assertions"][-1]
        self.assertTrue(a["ok"])
        self.assertIn("读了 3 次", a["got"])

    def test_log_expect_failure_says_how_long_it_waited(self):
        from crtest.cases import t21
        ctx, orch = fake_ctx(), FakeOrch(hit_on=999)
        with self.assertRaises(common.Failed) as cm:
            t21._log_expect(ctx, orch, "nope", "日志里该有的那一行", "一行 nope",
                            "单测", timeout=0.0, interval=0.0)
        self.assertIn("等了", str(cm.exception))
        self.assertFalse(ctx.results["assertions"][-1]["ok"])

    def test_log_expect_degrades_to_a_note_off_host(self):
        from crtest.cases import t21
        ctx = fake_ctx()
        orch = FakeOrch(hit_on=1)
        orch.dir = None
        self.assertEqual(t21._log_expect(ctx, orch, "x", "名字", "期望", "单测"), [])
        self.assertIsNone(ctx.results["assertions"][-1]["ok"])
        self.assertEqual(orch.reads, 0)              # 读不到日志就不去读


class TestInjectMode(unittest.TestCase):
    """T21：注入是常开还是 `:once`，从错误文案里读出来。"""

    def setUp(self):
        from crtest.cases import t21
        self.t21 = t21

    def mode(self, message, fault):
        return self.t21._inject_mode({"message": message}, fault)

    def test_once(self):
        self.assertEqual(self.mode(
            "Code.internal: failed to record checkpoint: fault injected: commit failed "
            "after the artifacts were renamed into place "
            "(CHECKPOINT_FAULT_INJECT=commit_late:once)", "commit_late"), "once")

    def test_always(self):
        self.assertEqual(self.mode(
            "Code.internal: fault injected: … (CHECKPOINT_FAULT_INJECT=commit_late)",
            "commit_late"), "always")

    def test_another_faults_once_does_not_count(self):
        # seal_move 与 commit_late 回同一个 500，认错名字就会按错的期望表判
        self.assertEqual(self.mode(
            "(CHECKPOINT_FAULT_INJECT=seal_move:once)", "commit_late"), "always")
        self.assertEqual(self.mode(
            "(CHECKPOINT_FAULT_INJECT=seal_move:once)", "seal_move"), "once")

    def test_no_marker_falls_back_to_always(self):
        # torn_assemble 那条是走路径触发的，文案里没有 env 标记；老服务端同理
        self.assertEqual(self.mode(
            "open /store/sbx/fault-injected/torn_assemble: no such file or directory",
            "torn_assemble"), "always")
        self.assertEqual(self.mode("", "commit_late"), "always")
        self.assertEqual(self.t21._inject_mode({}, "commit_late"), "always")

    def test_reads_it_off_a_real_error_info(self):
        info = common.error_info(
            "SandboxException: Code.internal: failed to snapshot sandbox: fault injected: "
            "the sealed layer never reached the store "
            "(CHECKPOINT_FAULT_INJECT=seal_move:once)")
        self.assertTrue(info["fault_injected"])
        self.assertEqual(self.mode(info["message"], "seal_move"), "once")
        self.assertEqual(self.t21._inject_mode(info, "seal_move"), "once")


class TestUnmet(unittest.TestCase):
    def test_text(self):
        e = common.Unmet("没装注入", "改 nomad job env")
        self.assertIn("前置不满足", e.text())
        self.assertIn("改 nomad job env", e.text())


class TestRegexes(unittest.TestCase):
    def test_timeout(self):
        self.assertTrue(common.TIMEOUT_RE.search("httpx.ReadTimeout"))
        self.assertTrue(common.TIMEOUT_RE.search("context deadline exceeded"))
        self.assertFalse(common.TIMEOUT_RE.search("connection refused"))

    def test_notfound(self):
        self.assertTrue(common.NOTFOUND_RE.search("not_found: checkpoint xyz"))
        self.assertTrue(common.NOTFOUND_RE.search("HTTP 404"))
        self.assertFalse(common.NOTFOUND_RE.search("internal error"))


class TestNoSdkNeeded(unittest.TestCase):
    def test_import_does_not_pull_e2b(self):
        self.assertNotIn("e2b", sys.modules)

    def test_parser_builds_and_parses(self):
        from crtest.__main__ import build_parser
        ap = build_parser()
        args = ap.parse_args(["T25", "--rounds", "3", "--out", "x.json"])
        self.assertEqual(args.case, "T25")
        self.assertEqual(args.rounds, 3)
        self.assertEqual(args.env_file, common.DEFAULT_ENV_FILE)
        self.assertNotIn("e2b", sys.modules)

    def test_every_case_has_run_and_add_args(self):
        import importlib
        from crtest.__main__ import CASES
        for code, mod, _ in CASES:
            m = importlib.import_module("crtest.cases." + mod)
            self.assertTrue(callable(m.run), code)
            self.assertTrue(callable(m.add_args), code)
            self.assertEqual(m.NAME, code)


class TestJudgeT18(unittest.TestCase):
    """T18 的三个判定（重启后：全量新根 / 旧 store 被清 / 真换了进程）。"""

    def test_full_root(self):
        ok, _, got = common.judge_full_root(
            {"mem_mode": "full"}, {"mem_mode": "full", "parent_id": ""})
        self.assertTrue(ok, got)
        # 盘上挂着 parent = 旧账本活过了重启
        ok, _, got = common.judge_full_root(
            {"mem_mode": "full"}, {"mem_mode": "full", "parent_id": "ckpt_1"})
        self.assertFalse(ok, got)
        ok, _, _ = common.judge_full_root(
            {"mem_mode": "incremental"}, {"mem_mode": "incremental", "parent_id": ""})
        self.assertFalse(ok)
        # 客户端说不上来（老 SDK）时只判盘上那份
        ok, _, got = common.judge_full_root({"mem_mode": "?"},
                                            {"mem_mode": "full", "parent_id": ""})
        self.assertTrue(ok)
        self.assertIn("客户端 mem_mode=?", got)
        # 客户端与盘上打架 → 判失败
        ok, _, _ = common.judge_full_root({"mem_mode": "incremental"},
                                          {"mem_mode": "full", "parent_id": ""})
        self.assertFalse(ok)
        ok, _, got = common.judge_full_root({"mem_mode": "full"}, None)
        self.assertFalse(ok)
        self.assertIn("没有 manifest", got)

    def test_store_cleared(self):
        ok, _, got = common.judge_store_cleared(["a", "b"], [])
        self.assertTrue(ok, got)
        # 重启后别人/沙箱 B 新建的目录不算数
        ok, _, got = common.judge_store_cleared(["a", "b"], ["c"])
        self.assertTrue(ok)
        self.assertIn("c", got)
        ok, _, got = common.judge_store_cleared(["a", "b"], ["b", "c"])
        self.assertFalse(ok)
        self.assertIn("b", got)
        ok, _, _ = common.judge_store_cleared([], [])
        self.assertTrue(ok)

    def test_restarted(self):
        self.assertTrue(common.judge_restarted(11, 22)[0])
        self.assertFalse(common.judge_restarted(11, 11)[0])
        self.assertIsNone(common.judge_restarted(None, 22)[0])
        self.assertIsNone(common.judge_restarted(11, None)[0])

    def test_restart_needs_idle_regex(self):
        self.assertTrue(common.RESTART_NEEDS_IDLE_RE.search("还有沙箱在跑，先清掉"))
        self.assertFalse(common.RESTART_NEEDS_IDLE_RE.search("nomad job run 失败"))

    def test_restart_cmd_expands_R(self):
        import types as _t
        from crtest.cases import t18
        old = os.environ.get("R")
        try:
            os.environ["R"] = "/tmp/repo"
            self.assertEqual(t18._restart_cmd(_t.SimpleNamespace(
                restart_cmd="$R/tmp/switch-stack.sh jll")), "/tmp/repo/tmp/switch-stack.sh jll")
            del os.environ["R"]
            self.assertTrue(t18._restart_cmd(_t.SimpleNamespace(
                restart_cmd="$R/tmp/switch-stack.sh jll")).startswith(t18.REPO))
        finally:
            if old is None:
                os.environ.pop("R", None)
            else:
                os.environ["R"] = old


class TestWaitUntil(unittest.TestCase):
    """`common.wait_until`：轮询到真值就停，超时给最后一次的结果。假时钟，不真等。"""

    def test_hits_immediately(self):
        clk = FakeClock()
        got, waited, tries = common.wait_until(lambda: "yes", 10, interval=2,
                                               sleep=clk.sleep, clock=clk)
        self.assertEqual((got, waited, tries), ("yes", 0.0, 1))

    def test_gives_up_after_the_timeout(self):
        clk = FakeClock()
        got, waited, tries = common.wait_until(lambda: None, 5, interval=2,
                                               sleep=clk.sleep, clock=clk)
        self.assertIsNone(got)
        self.assertEqual(waited, 5.0)      # 最后一觉被截到刚好 5 s
        self.assertEqual(tries, 4)

    def test_check_raising_counts_as_not_yet(self):
        clk = FakeClock()
        state = {"n": 0}

        def check():
            state["n"] += 1
            if state["n"] < 3:
                raise OSError("还没起来")
            return 200

        got, _, tries = common.wait_until(check, 10, interval=1,
                                          sleep=clk.sleep, clock=clk)
        self.assertEqual((got, tries), (200, 3))


class TestJudgeT34(unittest.TestCase):
    """T34 的 `judge_read_slowdown`：只算倍数、只标 warn，不判失败。"""

    def rows(self):
        return [{"depth": 0, "phase": "after-create", "mbps": 400.0},
                {"depth": 20, "phase": "after-create", "mbps": 200.0},
                {"depth": 50, "phase": "after-create", "mbps": 80.0},
                {"depth": 20, "phase": "after-restore", "mbps": 100.0}]

    def test_slowdown_and_warn(self):
        rows, warns = common.judge_read_slowdown(self.rows(), 3.0)
        by = {(r["depth"], r["phase"]): r for r in rows}
        self.assertAlmostEqual(by[(0, "after-create")]["slowdown"], 1.0)
        self.assertAlmostEqual(by[(20, "after-create")]["slowdown"], 2.0)
        self.assertAlmostEqual(by[(50, "after-create")]["slowdown"], 5.0)
        # after-restore 没有同 phase 的基线 → 退回全局最浅那一档的 400
        self.assertAlmostEqual(by[(20, "after-restore")]["slowdown"], 4.0)
        self.assertEqual(sorted((w["depth"], w["phase"]) for w in warns),
                         [(20, "after-restore"), (50, "after-create")])

    def test_missing_measurements_are_not_warnings(self):
        rows, warns = common.judge_read_slowdown(
            [{"depth": 0, "phase": "p", "mbps": 100.0},
             {"depth": 9, "phase": "p", "mbps": None},
             {"depth": 9, "phase": "p", "mbps": 0.0}], 2.0)
        self.assertEqual(warns, [])
        self.assertIsNone(rows[1]["slowdown"])
        self.assertFalse(rows[2]["warn"])

    def test_threshold_zero_disables(self):
        _, warns = common.judge_read_slowdown(self.rows(), 0)
        self.assertEqual(warns, [])

    def test_empty(self):
        self.assertEqual(common.judge_read_slowdown([], 3.0), ([], []))

    def test_depths_parsing(self):
        from crtest.cases import t34
        self.assertEqual(t34._depths("0,20,50"), [0, 20, 50])
        self.assertEqual(t34._depths(" 0 , 5 "), [0, 5])
        for bad in ("", "5,3", "1,1", "a", "-1"):
            with self.assertRaises(common.Unmet, msg=bad):
                t34._depths(bad)


class TestJudgeT36(unittest.TestCase):
    """T36 的权重解析、场景抽签、汇总分桶、收尾对账。"""

    def test_parse_weights(self):
        w = common.parse_weights("create=3,restore=2", ["create", "restore", "list"])
        self.assertEqual(w, {"create": 3.0, "restore": 2.0, "list": 0.0})
        for bad in ("", "create", "nope=1", "create=x", "create=-1", "create=0"):
            with self.assertRaises(ValueError, msg=bad):
                common.parse_weights(bad, ["create", "restore", "list"])

    def test_pick_weighted(self):
        w = {"a": 1.0, "b": 3.0, "c": 0.0}
        self.assertEqual(common.pick_weighted(w, 0.0), "a")
        self.assertEqual(common.pick_weighted(w, 0.24), "a")
        self.assertEqual(common.pick_weighted(w, 0.26), "b")
        self.assertEqual(common.pick_weighted(w, 0.999), "b")
        self.assertNotIn(common.pick_weighted(w, 0.5), ("c",))
        # 大样本下比例对得上
        n = {"a": 0, "b": 0}
        for i in range(1000):
            n[common.pick_weighted(w, i / 1000.0)] += 1
        self.assertAlmostEqual(n["a"] / 1000.0, 0.25, places=2)

    def test_bucket_ops(self):
        ops = [{"scene_name": "create", "ok": True, "wall_s": 1.0},
               {"scene_name": "create", "ok": True, "wall_s": 3.0},
               {"scene_name": "create", "ok": False,
                "error_info": {"type": "SandboxException", "reason": "busy"}},
               {"scene_name": "restore", "ok": True, "wall_s": 2.0, "verified": False},
               {"op": "list", "ok": False}]
        b = common.bucket_ops(ops)
        self.assertEqual(b["create"]["n"], 3)
        self.assertEqual(b["create"]["fail"], 1)
        self.assertEqual(b["create"]["errors"], {"SandboxException/busy": 1})
        self.assertEqual(b["create"]["max_s"], 3.0)
        self.assertEqual(b["restore"]["mismatch"], 1)
        self.assertEqual(b["list"]["errors"], {"?/?": 1})       # 没有 error_info 也不炸
        self.assertEqual(common.bucket_ops([]), {})

    def test_reconcile(self):
        ok, _, got = common.judge_reconcile(["a", "b"], ["b", "a"])
        self.assertTrue(ok, got)
        ok, _, got = common.judge_reconcile(["a", "b"], ["a"])
        self.assertFalse(ok)
        self.assertIn("'b'", got)
        ok, _, got = common.judge_reconcile(["a"], ["a", "ghost"])
        self.assertFalse(ok)
        self.assertIn("ghost", got)

    def _steady(self, seq, **kw):
        """用假时钟跑 netns_count_steady：seq 是依次返回的采样值，sleep 推进时钟。"""
        box = {"t": 0.0, "i": 0, "notes": []}

        def count():
            v = seq[min(box["i"], len(seq) - 1)]
            box["i"] += 1
            return v

        def sleep(dt):
            box["t"] += dt

        return common.netns_count_steady(
            _count=count, _sleep=sleep, _clock=lambda: box["t"],
            _log=box["notes"].append, **kw), box

    def test_netns_count_steady_stable(self):
        v, box = self._steady([100] * 50)
        self.assertEqual(v, 100)
        self.assertEqual(box["notes"], [])          # 稳定就不该有「未稳定」附注
        self.assertGreaterEqual(box["t"], 20.0)     # 至少等满一个窗口

    def test_netns_count_steady_waits_for_pool(self):
        # 暖池还在填：前 30 次每次 +1，之后不动 —— 必须等到不动了再返回。
        # 这一条量的是「会不会等」，所以超时显式给够（30 次采样 = 60 s，正好等于
        # 09-18 改后的默认超时，用默认值会被超时先截胡，量不到本来要量的东西）。
        seq = [100 + i for i in range(30)] + [129] * 50
        v, box = self._steady(seq, timeout_s=600.0)
        self.assertEqual(v, 129)
        self.assertEqual(box["notes"], [])

    def test_netns_count_steady_timeout(self):
        # 永远在涨：到 timeout 就返回当前值，并留下「未稳定」附注
        v, box = self._steady([100 + i for i in range(10000)], timeout_s=60.0)
        self.assertIsNotNone(v)
        self.assertEqual(len(box["notes"]), 1)
        self.assertIn("没有稳定", box["notes"][0])

    def test_netns_count_steady_default_timeout_is_60s(self):
        # 09-18：600 s 降到 60 s —— judge 那边有 netns_pool_warm() 的降级兜底，
        # 再死等 10 分钟只是白白拖长跑（integ4 的 T36 长跑白等了一次）。
        self.assertEqual(common.NETNS_STEADY_TIMEOUT, 60.0)
        v, box = self._steady([100 + i for i in range(10000)])
        self.assertIsNotNone(v)
        self.assertEqual(len(box["notes"]), 1)          # 到点了就附注「未稳定」
        self.assertLess(box["t"], 120.0)               # 而且没有等到 600 s

    def test_netns_count_steady_unreadable(self):
        v, box = self._steady([None])
        self.assertIsNone(v)

    def test_netns_steady(self):
        self.assertTrue(common.judge_netns_steady(10, 10)[0])
        self.assertTrue(common.judge_netns_steady(10, 12, slack=2)[0])
        self.assertFalse(common.judge_netns_steady(10, 13, slack=2)[0])
        self.assertIsNone(common.judge_netns_steady(None, 3)[0])

    def test_scene_table_covers_every_weight(self):
        from crtest.cases import t36
        self.assertEqual(sorted(t36.SCENE_FN), sorted(t36.SCENES))
        # 默认权重里每个场景都认得
        w = common.parse_weights(t36.DEFAULT_WEIGHTS, t36.SCENES)
        self.assertTrue(all(v > 0 for v in w.values()))


if __name__ == "__main__":
    unittest.main()
