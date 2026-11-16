"""离线检查验收入口是否拒绝旧口径、错误档位与错误恢复结果。"""
import ast
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest


class PerfResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        # run_tests.py 的模块入口会执行实机验收；这里只加载被测的纯判读函数。
        source = Path(__file__).with_name("run_tests.py")
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("require", "check_perf")]
        namespace = {"json": json, "re": re, "OUT": self.out}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        self.check = namespace["check_perf"]
        self.data = {"schema_version": 2, "measurement_mode": "per-tier-size",
                     "restore_path": "modified-child-to-full-baseline", "verdict": "PASS",
                     "baseline": [], "create": [], "restore": []}
        for index, (size, file_mb) in enumerate(zip((0, 12, 24, 48, 96, 192), (0, 4, 8, 16, 32, 64)), 1):
            row = {"mem_mb": size, "payload_mem_mb": size, "file_kb": file_mb * 1024}
            self.data["baseline"].append({**row, "checkpoint_id": f"base-{index}"})
            self.data["create"].append({**row, "checkpoint_id": f"child-{index}", "base_checkpoint_id": f"base-{index}"})
            state = {"payload_bytes": size << 20, "sha256": "expected", "marker": "base"}
            self.data["restore"].append({**row, "target_checkpoint_id": f"base-{index}",
                                         "source_checkpoint_id": f"child-{index}",
                                         "expected_memory": state, "actual_memory": copy.deepcopy(state), "ok": True})

    def check_data(self):
        (self.out / "perf.json").write_text(json.dumps(self.data))
        self.check("perf", "max |SDK - journal| = 0.000 ms\n判定: PASS")

    def test_new_results_accepted(self):
        self.check_data()

    def test_old_schema_refused(self):
        del self.data["schema_version"]
        with self.assertRaisesRegex(RuntimeError, "口径"):
            self.check_data()

    def test_maximum_payload_in_small_tier_refused(self):
        self.data["restore"][0]["payload_mem_mb"] = 192
        with self.assertRaisesRegex(RuntimeError, "负载不符"):
            self.check_data()

    def test_wrong_restore_target_refused(self):
        self.data["restore"][1]["target_checkpoint_id"] = "base-1"
        with self.assertRaisesRegex(RuntimeError, "基点"):
            self.check_data()

    def test_bad_memory_even_with_ok_flag_refused(self):
        self.data["restore"][0]["actual_memory"]["sha256"] = "corrupt"
        with self.assertRaisesRegex(RuntimeError, "内存校验"):
            self.check_data()

    def test_failed_verdict_refused(self):
        self.data["verdict"] = "FAIL"
        with self.assertRaisesRegex(RuntimeError, "不通过"):
            self.check_data()


if __name__ == "__main__":
    unittest.main()
