"""fc_exit_trace.py 的本地单测：function_graph 输出解析与退出拆分。不碰 tracefs、不建沙箱：
python3 -m unittest benchmark/test_fc_exit_trace.py"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fc_exit_trace as fx  # noqa: E402

TRACE = """\
# tracer: function_graph
#
 99.900000 |   4)  ps-60         |               |  /* sched_process_exec: filename=/usr/bin/ps pid=60 old_pid=60 */
 99.985000 |   4)  ps-60         |               |  /* sched_process_exit: comm=ps pid=60 prio=120 */
 100.000000 |   1)  orchest-50    |               |  /* signal_generate: sig=15 errno=0 code=0 comm=firecracker pid=1000 grp=1 res=0 */
 100.000300 |   2)  firecrac-1000 |               |  do_group_exit() {
 100.000310 |   2)  firecrac-1000 |               |    do_exit() {
 100.000320 |   2)  firecrac-1000 |               |      exit_mm() {
 100.000330 |   2)  firecrac-1000 |               |        exit_mmap() {
 100.120000 |   2)  firecrac-1000 | @ 119670.0 us |        } /* exit_mmap */
 100.120320 |   2)  firecrac-1000 | @ 120000.0 us |      } /* exit_mm */
 100.120400 |   2)  firecrac-1000 |               |      task_work_run() {
 100.120500 |   2)  firecrac-1000 |   5.000 us    |        __fput();
 100.120600 |   2)  firecrac-1000 |               |        __fput() {
 100.120700 |   2)  firecrac-1000 |               |          kvm_vm_release() {
 100.144000 |   2)  firecrac-1000 | * 23300.00 us |          } /* kvm_vm_release */
 100.144100 |   2)  firecrac-1000 | * 23500.00 us |        } /* __fput */
 100.145400 |   2)  firecrac-1000 | * 25000.00 us |      } /* task_work_run */
 100.156400 |   2)  firecrac-1000 | * 11000.00 us |      exit_task_namespaces();
 100.159000 |   2)  firecrac-1000 |               |  /* sched_process_exit: comm=firecracker pid=1000 prio=120 */
 100.160000 |   2)  firecrac-1000 | @ 159690.0 us |    } /* do_exit */
 ------------------------------------------
 100.000400 |   3)  firecrac-1001 |               |    do_exit() {
 100.000900 |   3)  firecrac-1001 |               |  /* sched_process_exit: comm=fc_vcpu 0 pid=1001 prio=120 */
 100.001400 |   3)  firecrac-1001 | # 1000.000 us |    } /* do_exit */
"""


class T(unittest.TestCase):
    def test_parse_and_split(self):
        calls, events = fx.parse(TRACE)
        self.assertEqual([e["name"] for e in events], ["sched_process_exec", "sched_process_exit", "signal_generate",
                                                       "sched_process_exit", "sched_process_exit"])
        self.assertEqual(int(events[2]["pid"]), 1000)
        names = {c[3] for c in calls}
        self.assertTrue({"do_exit", "exit_mm", "exit_mmap", "task_work_run", "__fput", "kvm_vm_release",
                         "exit_task_namespaces"} <= names)
        res = fx.analyze(calls, events, 1000)
        self.assertAlmostEqual(res["SIGTERM → 第一个线程进入 do_exit"], 0.31, places=3)
        self.assertAlmostEqual(res["SIGTERM → 主线程退出完"], 160.0, places=3)
        self.assertAlmostEqual(res["ps 查进程状态"], 85.0, places=3)
        self.assertAlmostEqual(res["ps 结束 → 发 SIGTERM"], 15.0, places=3)
        self.assertEqual(res["线程数"], 2)
        self.assertAlmostEqual(res["其他线程 do_exit 最长"], 1.0)
        tree = res["tree"]
        self.assertEqual(list(tree)[:3], ["do_exit", "do_exit/exit_mm", "do_exit/exit_mm/exit_mmap"])  # 父在子前
        self.assertAlmostEqual(tree["do_exit"], 159.69)
        self.assertAlmostEqual(tree["do_exit/task_work_run/__fput"], 23.505)       # 同一路径累加
        self.assertAlmostEqual(tree["do_exit/task_work_run/__fput/kvm_vm_release"], 23.3)
        self.assertAlmostEqual(tree["do_exit/exit_task_namespaces"], 11.0)

    def test_do_exit_never_returns(self):
        # 真实 trace 里 do_exit 没有结束行：按线程最后一条记录收尾
        text = TRACE.replace(" 100.160000 |   2)  firecrac-1000 | @ 159690.0 us |    } /* do_exit */\n",
                             " 100.160500 |   2)  firecrac-1000 |   4.000 us    |      exit_notify();\n")
        calls, events = fx.parse(text)
        res = fx.analyze(calls, events, 1000)
        self.assertAlmostEqual(res["tree"]["do_exit"], 160.19, places=2)
        self.assertAlmostEqual(res["SIGTERM → 主线程退出完"], 160.5, places=3)

    def test_stray_close_after_cpu_switch(self):
        # 换 CPU 后合并顺序错位：一个入口没记上的结束行，不能把外层 do_exit 提前弹掉
        stray = " 100.120450 |   5)  firecrac-1000 |   9.000 us    |        } /* synchronize_rcu_expedited */\n"
        text = TRACE.replace(" 100.120500 |   2)  firecrac-1000 |   5.000 us    |        __fput();\n",
                             stray + " 100.120500 |   2)  firecrac-1000 |   5.000 us    |        __fput();\n")
        calls, events = fx.parse(text)
        res = fx.analyze(calls, events, 1000)
        self.assertAlmostEqual(res["tree"]["do_exit"], 159.69)
        self.assertAlmostEqual(res["tree"]["do_exit/task_work_run"], 25.0)

    def test_no_sigterm(self):
        calls, events = fx.parse(TRACE.replace("sig=15", "sig=9"))
        self.assertIsNone(fx.analyze(calls, [e for e in events if e["name"] != "signal_generate"], 1000))

    def test_summary_print(self):
        calls, events = fx.parse(TRACE)
        res = fx.analyze(calls, events, 1000)
        rows = [{"P": 0, "d": 0, "rep": r, "kind": "first" if r == 0 else "sample", "pause_wall_ms": 300.0,
                 "orch_stop_ms": 260.0, "pause_phases": {"stop_begin": 10.0, "fc_exited": 185.0}, "split": res}
                for r in range(4)]
        s = fx.summarize(rows)
        self.assertEqual(s[0]["n"], 3)
        self.assertAlmostEqual(s[0]["orch_minus_kernel_ms"], 15.0)     # 260 - ps 85 - 退出 160
        buf = io.StringIO()
        with redirect_stdout(buf):
            fx.print_summary(s)
        out = buf.getvalue()
        self.assertIn("kvm_vm_release", out)
        self.assertIn("exit_mmap", out)
        self.assertIn("其他 1 个线程的 do_exit 最长 1.0", out)


if __name__ == "__main__":
    unittest.main()
