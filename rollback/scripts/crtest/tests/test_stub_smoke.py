#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T11 / T21 的打桩冒烟：不需要沙箱、不需要栈，整段跑一遍 `run_case`。

作用是抓"只有整段跑起来才会露头"的低级错误 —— 字段名拼错、列表越界、
把 guest 命令发给一个已经 paused 的沙箱、判定分支写反、退出码不对。
真正的行为验证当然要在 920B 上对真服务端跑（README「怎么跑」）。

打桩的服务端见 `tests/stub.py`：错误文案、manifest 字段、hidden 语义都抄服务端原文。
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crtest import common                    # noqa: E402
from crtest.__main__ import build_parser     # noqa: E402
from tests import stub                       # noqa: E402


# T18 的重启命令没有默认值（不同部署方式不一样），打桩测试给一条假的：
# run_shell 已经被 arm_restart() 换成 fake_restart，命令本身不会真被执行。
STUB_RESTART_CMD = "true # 打桩，不会真执行"

class StubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crtest-stub-")
        self.undo = []
        del common._ALL_BOXES[:]

    def tearDown(self):
        for fn in self.undo:
            fn()
        del common._ALL_BOXES[:]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def parse(self, argv):
        """公共参数（--no-probe / 假 env 文件 / 固定的 --out）一次配齐。"""
        return build_parser().parse_args(
            argv + ["--no-probe", "--env-file", os.path.join(self.tmp, "nope.env"),
                    "--out", os.path.join(self.tmp, "out.json")])

    def read_out(self):
        with open(os.path.join(self.tmp, "out.json")) as f:
            return json.load(f)

    def run_case(self, argv, fault, op_seconds=0.0):
        server = stub.FakeServer(os.path.join(self.tmp, "store"), fault, op_seconds)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        args = self.parse(argv)
        mod = importlib.import_module("crtest.cases." + args._mod)
        rc = common.run_case(args.case, mod, args)
        return rc, self.read_out()


class TestT21Stub(StubCase):
    def _one(self, fault):
        rc, res = self.run_case(["T21", "--fault", fault], fault)
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        self.assertTrue(res["assertions"])
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)
        self.assertEqual(res["summary"]["T21"]["fault"], fault)
        return res

    def test_envd_timeout(self):
        res = self._one("envd_timeout")
        s = res["summary"]["T21"]
        # A1：新 checkpoint 挂在刚才 restore 的目标下
        self.assertEqual(s["parent_of_new"], s["target_of_failed_restore"])
        # 今天的 SDK 没有 reason 字段，只能退化判 —— 这一项本身是记录项
        self.assertFalse(s["sdk_has_reason_field"])

    def test_torn_assemble(self):
        self._one("torn_assemble")

    def test_seal_move(self):
        res = self._one("seal_move")
        self.assertTrue(res["summary"]["T21"]["entries"][0]["hidden"])
        self.assertFalse(res["summary"]["T21"]["poison_visible"])

    def test_commit_late(self):
        res = self._one("commit_late")
        ents = res["summary"]["T21"]["entries"]
        self.assertEqual(len(ents), 2)
        self.assertEqual(ents[1]["parent_id"], ents[0]["id"])
        self.assertEqual(ents[1]["mem_mode"], "incremental")

    def test_commit_late_once_heals(self):
        """`:once` —— 注入用光之后第二次 create 成功，而且接着原来的链长。"""
        rc, res = self.run_case(["T21", "--fault", "commit_late"], "commit_late:once")
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)
        s = res["summary"]["T21"]
        self.assertEqual(s["inject_mode"], "once")
        self.assertEqual(s["mode"], "once")
        self.assertTrue(s["second_create_ok"])
        ents = s["entries"]
        self.assertEqual(len(ents), 2)
        self.assertTrue(ents[0]["hidden"])
        self.assertFalse(ents[1]["hidden"])
        self.assertEqual(ents[1]["parent_id"], ents[0]["id"])
        self.assertEqual(ents[1]["mem_mode"], "incremental")
        names = [a["name"] for a in res["assertions"]]
        self.assertIn("注入用光之后第二次 create 成功", names)
        self.assertNotIn("第二次 create 也在同一处失败（注入常开）", names)

    def test_seal_move_once_shows_the_poison(self):
        """`:once` —— 注入用光之后拦住 create 的是 poison 本身。"""
        rc, res = self.run_case(["T21", "--fault", "seal_move"], "seal_move:once")
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)
        s = res["summary"]["T21"]
        self.assertEqual(s["mode"], "once")
        self.assertTrue(s["poison_visible"])
        self.assertIn("注入用光之后，拦住 create 的是 poison 本身",
                      [a["name"] for a in res["assertions"]])

    def test_always_mode_keeps_the_old_expectations(self):
        """常开模式的期望表没被 once 那一支改掉。"""
        res = self._one("commit_late")
        self.assertEqual(res["summary"]["T21"]["mode"], "always")
        self.assertFalse(res["summary"]["T21"]["second_create_ok"])
        self.assertIn("第二次 create 也在同一处失败（注入常开）",
                      [a["name"] for a in res["assertions"]])

    def test_not_armed_exits_3(self):
        """服务端没装注入 → 退出码 3（前置不满足），不是失败。"""
        rc, res = self.run_case(["T21", "--fault", "commit_late"], None)
        self.assertEqual(rc, 3)
        self.assertIn("前置不满足", res["meta"]["failure"])

    def test_wrong_fault_armed_exits_3(self):
        rc, res = self.run_case(["T21", "--fault", "seal_move"], "commit_late")
        self.assertEqual(rc, 3)


class TestT11Stub(StubCase):
    def test_four_rounds(self):
        rc, res = self.run_case(
            ["T11", "--sandboxes", "2", "--rounds", "4", "--reclaim-wait", "5"],
            None, op_seconds=0.2)
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        s = res["summary"]["T11"]
        self.assertEqual(s["kills"], 2)
        self.assertEqual(s["pauses"], 2)
        self.assertEqual(len(s["per_round"]), 4)
        self.assertEqual([r["inflight"] for r in s["per_round"]],
                         ["create", "restore", "create", "restore"])
        for r in s["per_round"]:
            if r["action"] == "kill":
                self.assertTrue(r["store_gone"])
                self.assertEqual(r["fc_left"], [])

    def test_hanging_op_fails(self):
        """在途操作挂过 --op-timeout 就必须判失败（退出码 1）。"""
        rc, res = self.run_case(
            ["T11", "--sandboxes", "2", "--rounds", "1", "--op-timeout", "0.3",
             "--reclaim-wait", "3"], None, op_seconds=3.0)
        self.assertEqual(rc, 1)
        self.assertIn("挂住", res["meta"]["failure"])


class TestT18Stub(StubCase):
    """T18 的打桩冒烟：重启用假命令模拟 —— 它做的正是服务端做的那两件事，
    `NewStore` 清 store 根（store.go:232 的 `os.RemoveAll`）+ 沙箱跟着 task 一起没。"""

    def arm_restart(self, server, refuse_while_live=True):
        """把 T18 要用的宿主机侧入口都换掉，并让"重启"真的清 store、带走沙箱。"""
        pid = [1000]
        calls = []

        def live_sandboxes():
            return [s for s in server.entries if s not in server.killed]

        def fake_restart(cmd, timeout=900):
            calls.append(cmd)
            if refuse_while_live and live_sandboxes():
                # switch-stack.sh 的换栈前置：活 FC 不为 0 就拒（原话在 common 的正则里）
                return {"cmd": cmd, "rc": 1, "wall_s": 0.01,
                        "out": "还有沙箱在跑，先清掉\n"}
            pid[0] += 1
            for sid in list(server.entries):
                server.kill(sid)                 # 带走沙箱 + 删它的产物目录
            server.entries.clear()
            server.base.clear()
            return {"cmd": cmd, "rc": 0, "wall_s": 1.5, "out": "切换完成 pid=%d\n" % pid[0]}

        orig = {k: getattr(common, k) for k in
                ("orchestrator_pid_env", "http_ok", "run_shell", "live_fc_count")}
        common.orchestrator_pid_env = lambda: (pid[0], {"ORCHESTRATOR_BASE_PATH": "/fake"})
        common.http_ok = lambda url, timeout=2.0: 200
        common.run_shell = fake_restart
        common.live_fc_count = lambda: 0
        self.undo.append(lambda: [setattr(common, k, v) for k, v in orig.items()])
        return pid, calls

    def test_not_allowed_exits_3(self):
        """没给 --allow-restart：只打印命令，退出码 3，一次重启都不执行。"""
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.0)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        _, calls = self.arm_restart(server)
        args = self.parse(["T18"])
        rc = common.run_case("T18", importlib.import_module("crtest.cases.t18"), args)
        self.assertEqual(rc, 3)
        self.assertEqual(calls, [])

    def _run_ok(self, extra):
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.0)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        pid, calls = self.arm_restart(server)
        args = self.parse(["T18", "--allow-restart", "--restart-cmd", STUB_RESTART_CMD] + extra)
        rc = common.run_case("T18", importlib.import_module("crtest.cases.t18"), args)
        res = self.read_out()
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)
        return res, pid, calls, server

    def test_pre_restart_kill(self):
        res, pid, calls, server = self._run_ok(["--pre-restart", "kill"])
        s = res["summary"]["T18"]
        self.assertEqual(len(calls), 1)             # 事先 kill 过，一次就成
        self.assertTrue(s["pre_killed"])
        self.assertFalse(s["restart_refused_first"])
        self.assertEqual(s["orchestrator_pid_after"], s["orchestrator_pid_before"] + 1)
        # 这个数是**刚重启完**（还没建沙箱 B）那一刻数的：旧账本一个不剩
        self.assertEqual(s["store_children_after"], 0)
        self.assertEqual(s["b_first_manifest"]["mem_mode"], "full")
        self.assertEqual(s["b_first_manifest"]["parent_id"], "")
        names = [a["name"] for a in res["assertions"]]
        self.assertIn("重启后第一次 create 走全量新根", names)
        self.assertIn("重启后新沙箱 list 为空", names)
        self.assertIn("重启清空了旧 store 根", names)

    def test_auto_retries_after_the_refusal(self):
        """默认 auto：带着沙箱 A 那一发被拒 → kill 掉 A 再来一次，并如实记下来。"""
        res, pid, calls, server = self._run_ok([])
        s = res["summary"]["T18"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(s["restart_refused_first"])
        self.assertTrue(s["pre_killed"])
        # 自己 kill 的，所以"重启带走了沙箱 A"降成记录项（ok 为 None）
        rows = [a for a in res["assertions"] if a["name"] == "沙箱 A 已经不存在"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ok"])
        self.assertNotIn("重启带走了沙箱 A", [a["name"] for a in res["assertions"]])

    def test_keep_exits_3_when_the_restart_needs_an_idle_node(self):
        """--pre-restart keep：重启命令要空载而我们不许 kill → 前置不满足（3）。"""
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.0)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        _, calls = self.arm_restart(server)
        args = self.parse(["T18", "--allow-restart", "--restart-cmd", STUB_RESTART_CMD, "--pre-restart", "keep"])
        rc = common.run_case("T18", importlib.import_module("crtest.cases.t18"), args)
        res = self.read_out()
        self.assertEqual(rc, 3)
        self.assertIn("前置不满足", res["meta"]["failure"])
        self.assertEqual(len(calls), 1)

    def test_keeping_the_old_ledger_fails(self):
        """反面：重启没清账本（假装 store 活了下来）→ 用例必须判失败。"""
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.0)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        pid = [1000]

        def lazy_restart(cmd, timeout=900):
            pid[0] += 1
            for sid in list(server.entries):
                server.killed.add(sid)      # 沙箱没了，但产物目录与内存账本都留着
            return {"cmd": cmd, "rc": 0, "wall_s": 1.0, "out": "假装重启了\n"}

        # 用 keep：不许 kill 沙箱 A，于是 A 的产物目录只可能被"重启"清掉

        orig = {k: getattr(common, k) for k in
                ("orchestrator_pid_env", "http_ok", "run_shell", "live_fc_count")}
        common.orchestrator_pid_env = lambda: (pid[0], {})
        common.http_ok = lambda url, timeout=2.0: 200
        common.run_shell = lazy_restart
        common.live_fc_count = lambda: 0
        self.undo.append(lambda: [setattr(common, k, v) for k, v in orig.items()])

        args = self.parse(["T18", "--allow-restart", "--restart-cmd", STUB_RESTART_CMD, "--pre-restart", "keep"])
        rc = common.run_case("T18", importlib.import_module("crtest.cases.t18"), args)
        res = self.read_out()
        self.assertEqual(rc, 1)
        self.assertIn("重启清空了旧 store 根", res["meta"]["failure"])


class TestT34Stub(StubCase):
    """T34 的打桩冒烟：假 guest 的冷读吞吐随链深掉，走一遍建链→量→restore→量。"""

    def test_three_depths(self):
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.0)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        args = self.parse(["T34", "--depths", "0,2,4", "--file-mb", "8",
                           "--layer-mb", "1", "--rand-reads", "4",
                           "--max-slowdown", "1.01"])
        rc = common.run_case("T34", importlib.import_module("crtest.cases.t34"), args)
        res = self.read_out()
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)
        s = res["summary"]["T34"]
        self.assertEqual(s["depths"], [0, 2, 4])
        self.assertEqual(s["chain_depth_final"], 4)
        rows = s["rows"]
        # 0 层只量 after-create（没有 checkpoint 可回），另两档各两次
        self.assertEqual([(r["depth"], r["phase"]) for r in rows],
                         [(0, "after-create"), (2, "after-create"), (2, "after-restore"),
                          (4, "after-create"), (4, "after-restore")])
        self.assertAlmostEqual(rows[0]["slowdown"], 1.0)
        self.assertGreater(rows[3]["slowdown"], rows[1]["slowdown"])   # 越深越慢
        self.assertTrue(s["warned"])                                   # 软阈值被踩到
        # 软阈值超了也**不判失败**
        warn_rows = [a for a in res["assertions"] if a["name"] == "超过软阈值的档数"]
        self.assertEqual(len(warn_rows), 1)
        self.assertIsNone(warn_rows[0]["ok"])

    def test_bad_depths_exits_3(self):
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.0)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        args = self.parse(["T34", "--depths", "50,20"])
        rc = common.run_case("T34", importlib.import_module("crtest.cases.t34"), args)
        self.assertEqual(rc, 3)


class TestT36Stub(StubCase):
    """T36 的打桩冒烟：两个沙箱跑几秒，重点看"线程不会被异常带走"与收尾对账。"""

    def test_short_steady_run(self):
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.02)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        args = self.parse(["T36", "--sandboxes", "2", "--seconds", "3",
                           "--report-every", "0", "--max-checkpoints", "4"])
        rc = common.run_case("T36", importlib.import_module("crtest.cases.t36"), args)
        res = self.read_out()
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)
        s = res["summary"]["T36"]
        self.assertEqual(s["sandboxes"], 2)
        self.assertGreater(s["total_ops"], 0)
        self.assertEqual(s["mismatch"], 0)
        self.assertEqual(s["fc_left"], {})
        for st in s["threads"].values():
            self.assertTrue(st["finished"])
            self.assertEqual(st["loop_errors"], [])
            self.assertGreater(st["iterations"], 0)
        names = [a["name"] for a in res["assertions"]]
        self.assertIn("restore 后现场逐项一致", names)
        self.assertIn("跑完活 FC 归零", names)
        # 每沙箱都做了收尾对账 + 删干净
        self.assertEqual(sum(1 for n in names if n.endswith("收尾 list 与账本一致")), 2)
        self.assertEqual(sum(1 for n in names if n.endswith("删干净之后 list 为空")), 2)

    def test_a_scene_that_always_raises_does_not_kill_the_worker(self):
        """最要紧的那条：某个场景每次都抛，worker 也得跑满全程并把失败如实分桶。"""
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.02)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        from crtest.cases import t36

        def boom(ctx, box, rnd, st):
            raise RuntimeError("场景自己炸了")

        orig = t36.SCENE_FN["list"]
        t36.SCENE_FN["list"] = boom
        self.undo.append(lambda: t36.SCENE_FN.__setitem__("list", orig))

        args = self.parse(["T36", "--sandboxes", "2", "--seconds", "3",
                           "--report-every", "0", "--weights", "list=1,exec=1"])
        rc = common.run_case("T36", importlib.import_module("crtest.cases.t36"), args)
        res = self.read_out()
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        s = res["summary"]["T36"]
        self.assertGreater(s["by_scene"]["list"]["fail"], 0)
        self.assertIn("RuntimeError/?", s["by_scene"]["list"]["errors"])
        for st in s["threads"].values():
            self.assertTrue(st["finished"])
            self.assertEqual(st["loop_errors"], [])     # 是 `_step` 兜住的，不是循环体
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)

    def test_bad_weights_exit_3(self):
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.0)
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        args = self.parse(["T36", "--weights", "nope=1"])
        rc = common.run_case("T36", importlib.import_module("crtest.cases.t36"), args)
        self.assertEqual(rc, 3)


class TestT40Stub(StubCase):
    """T40 的打桩冒烟：restore 紧跟长流，重点看 gap 有没有量出来、截断有没有归对类。"""

    def _run(self, argv, stream_cut=0):
        server = stub.FakeServer(os.path.join(self.tmp, "store"), None, 0.0)
        server.stream_cut = stream_cut
        os.makedirs(server.store, exist_ok=True)
        stub.install(common, server, self.undo)
        args = self.parse(argv)
        rc = common.run_case("T40", importlib.import_module("crtest.cases.t40"), args)
        return rc, self.read_out()

    def test_clean_run_records_the_gap(self):
        rc, res = self._run(["T40", "--sandboxes", "2", "--rounds", "3",
                             "--stream-seconds", "0.2", "--report-every", "0"])
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        s = res["summary"]["T40"]
        self.assertEqual(s["restores"], 6)                 # 2 沙箱 × 3 轮
        self.assertEqual(s["requests"], 12)                # 每轮 1 条流 + 1 条 unary
        self.assertEqual(s["by_class"], {})                # 打桩里不该有失败
        self.assertEqual(s["truncations"], [])
        self.assertEqual(s["gap_ms"]["n"], 12)             # 每条请求都量到了间隔
        self.assertTrue(sum(s["gap_ms_hist"].values()) == 12)
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)

    def test_a_cut_stream_is_counted_as_truncation(self):
        # 流跑到第 1 行就被掐：用例必须记成 truncation，而不是当成成功
        rc, res = self._run(["T40", "--sandboxes", "1", "--rounds", "2",
                             "--stream-seconds", "0.5", "--report-every", "0"],
                            stream_cut=1)
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        s = res["summary"]["T40"]
        self.assertEqual(s["by_class"].get("truncation"), 2)
        self.assertEqual(len(s["truncations"]), 2)
        one = s["truncations"][0]
        self.assertIn("incomplete chunked", one["err"])
        self.assertEqual(one["lines"], 1)
        self.assertIsNotNone(one["gap_ms"])
        # 每例都带着前一次 restore 的段（归因要的那两列）
        self.assertIn("prev_restore_total_ms", one)
        # 截断是记录项，不判失败
        bad = [a for a in res["assertions"] if a["ok"] is False]
        self.assertFalse(bad, bad)

    def test_the_hard_line_can_be_armed(self):
        rc, res = self._run(["T40", "--sandboxes", "1", "--rounds", "1",
                             "--stream-seconds", "0.3", "--report-every", "0",
                             "--max-truncation", "0"], stream_cut=1)
        self.assertEqual(rc, 1)
        self.assertIn("截断次数在硬线内", res["meta"].get("failure", ""))

    def test_fanout_fires_every_stream(self):
        rc, res = self._run(["T40", "--sandboxes", "1", "--rounds", "2", "--fanout", "4",
                             "--stream-seconds", "0.2", "--no-unary",
                             "--report-every", "0"])
        self.assertEqual(rc, 0, res["meta"].get("failure"))
        s = res["summary"]["T40"]
        self.assertEqual(s["requests"], 8)                 # 2 轮 × fanout 4，没有 unary
        self.assertEqual(s["by_scene"]["stream"]["n"], 8)
        self.assertNotIn("unary", s["by_scene"])


if __name__ == "__main__":
    unittest.main()
