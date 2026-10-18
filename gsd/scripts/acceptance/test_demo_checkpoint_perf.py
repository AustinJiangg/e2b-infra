"""分档负载、恢复路径与内容校验的离线回归测试，不连接真实沙箱。"""
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location("perf", Path(__file__).with_name("demo_checkpoint_perf.py"))
perf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(perf)


class FakeSandbox:
    def __init__(self, corrupt=None, fail_create=False):
        self.commands = self.files = self.checkpoint = self
        self.content = ""
        self.snapshots = {}
        self.events = []
        self.corrupt = corrupt
        self.fail_create = fail_create
        self.killed = False
        self.last_timings = {}
        self.logs = []

    def run(self, command, **kwargs):
        text = "\n".join(self.logs) if command.startswith("journalctl") else "VmRSS:\t20000 kB\nVmSize:\t30000 kB\n"
        return type("Output", (), {"stdout": text})()

    def write(self, path, content):
        self.content = content

    def read(self, path):
        return self.content

    def create(self, name):
        if self.fail_create:
            raise RuntimeError("模拟 create 失败")
        self.events.append(("create", name))
        self.snapshots[name] = (copy.deepcopy(self.hog.state()), self.content)
        timings = {"total_ms": 2.0, "criu_dump_ms": 1.0}
        self.logs.append(json.dumps({"checkpoint_id": name, "message": "checkpoint created", **timings}))
        return type("Checkpoint", (), {"checkpoint_id": name, "timings": timings,
                    "parent_checkpoint_id": None if name.endswith("-base") else name + "-base"})()

    def restore(self, checkpoint_id):
        self.events.append(("restore", checkpoint_id))
        state, self.content = self.snapshots[checkpoint_id]
        self.hog.snapshot = copy.deepcopy(state)
        if self.corrupt == "memory":
            # 保留 marker 和大小，只破坏实际内容摘要，旧的 marker 校验捕捉不到。
            self.hog.snapshot["sha256"] = "corrupt"
        if self.corrupt == "file":
            self.content = "corrupt"
        self.last_timings = {"total_ms": 3.0, "criu_restore_ms": 2.0}
        self.logs.append(json.dumps({"checkpoint_id": checkpoint_id, "message": "starting restore"}))
        self.logs.append(json.dumps({"checkpoint_id": checkpoint_id, "message": "restore completed", **self.last_timings}))

    def kill(self):
        self.killed = True


class FakeHog:
    def __init__(self, sb):
        sb.hog = self
        self.version = 0
        self.snapshot = None

    def start(self, payload_mb):
        self.payload_mb = payload_mb
        self.pid = 123

    def dirty(self, mb):
        self.version += 1
        return f"ok:{mb}"

    def marker(self, marker):
        self.label = marker
        return "ok"

    def state(self):
        return self.snapshot or {"payload_bytes": self.payload_mb << 20,
                                 "sha256": str(self.version), "marker": self.label}


class TierTests(unittest.TestCase):
    def result(self):
        return {"baseline": [], "create": [], "restore": []}

    def run_tier(self, box, exp, result=None, index=1):
        result = self.result() if result is None else result
        with patch.object(perf, "MemHog", FakeHog):
            perf.run_tier(lambda **kwargs: box, index, exp, result)
        return result

    def test_each_tier_allocates_its_own_size_and_restores_its_own_baseline(self):
        result = self.result()
        sizes = []
        for index, exp in enumerate(perf.EXPERIMENTS, 1):
            box = FakeSandbox()
            self.run_tier(box, exp, result, index)
            sizes.append(box.hog.payload_mb)
            self.assertEqual(box.events, [("create", f"cp{index}-base"),
                                          ("create", f"cp{index}"), ("restore", f"cp{index}-base")])
            self.assertTrue(box.killed)
        self.assertEqual(sizes, [0, 12, 24, 48, 96, 192])
        self.assertEqual(len(result["baseline"]), 6)
        self.assertTrue(all(row["ok"] for row in result["restore"]))
        self.assertEqual([r["expected_memory"]["payload_bytes"] for r in result["restore"]],
                         [n << 20 for n in sizes])
        self.assertEqual(perf.check_timings(result), 0)

    def test_memory_corruption_with_correct_marker_fails(self):
        box = FakeSandbox(corrupt="memory")
        result = self.result()
        with self.assertRaisesRegex(RuntimeError, "内容不一致"):
            self.run_tier(box, {"mem_mb": 12, "file_kb": 1}, result)
        self.assertFalse(result["restore"][0]["mem_ok"])
        self.assertTrue(box.killed)

    def test_file_corruption_fails(self):
        box = FakeSandbox(corrupt="file")
        with self.assertRaisesRegex(RuntimeError, "内容不一致"):
            self.run_tier(box, {"mem_mb": 0, "file_kb": 0})
        self.assertTrue(box.killed)

    def test_create_failure_still_cleans_up(self):
        box = FakeSandbox(fail_create=True)
        with self.assertRaisesRegex(RuntimeError, "create 失败"):
            self.run_tier(box, {"mem_mb": 0, "file_kb": 0})
        self.assertTrue(box.killed)

    def test_full_dump_instead_of_incremental_is_rejected(self):
        box = FakeSandbox()
        original = box.create

        def create(name):
            cp = original(name)
            cp.parent_checkpoint_id = None
            return cp

        box.create = create
        with self.assertRaisesRegex(RuntimeError, "增量父节点"):
            self.run_tier(box, {"mem_mb": 12, "file_kb": 1})
        self.assertTrue(box.killed)

    def test_timings_require_fields_and_match(self):
        result = self.result()
        result["restore"] = [{"name": "cp1", "sdk": {}, "journal": {}}]
        with self.assertRaisesRegex(RuntimeError, "缺少"):
            perf.check_timings(result)
        row = result["restore"][0]
        row["sdk"] = {"total_ms": 3.0, "criu_restore_ms": 2.0}
        row["journal"] = dict(row["sdk"])
        self.assertEqual(perf.check_timings(result), 0)
        row["journal"]["total_ms"] += 1
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            perf.check_timings(result)

    def test_main_failure_returns_nonzero_and_writes_partial_result(self):
        box = FakeSandbox(corrupt="memory")
        sdk = types.ModuleType("e2b")
        sdk.Sandbox = type("Sandbox", (), {"create": staticmethod(lambda **kwargs: box)})
        env = types.ModuleType("e2b_env")
        env.load_e2b_env = lambda: {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "partial.json")
            with patch.dict(sys.modules, {"e2b": sdk, "e2b_env": env}), \
                 patch.dict(perf.os.environ, {"OUT_JSON": str(output)}), \
                 patch.object(perf, "MemHog", FakeHog), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(perf.main(), 1)
            data = json.loads(output.read_text())
            self.assertEqual(data["schema_version"], 2)
            self.assertEqual(data["verdict"], "FAIL")
            self.assertFalse(data["restore"][0]["ok"])
            self.assertEqual(len(data["restore"]), 1)
            self.assertTrue(box.killed)

    def test_main_success_keeps_full_and_incremental_results_separate(self):
        boxes = []

        def create(**kwargs):
            box = FakeSandbox()
            boxes.append(box)
            return box

        sdk = types.ModuleType("e2b")
        sdk.Sandbox = type("Sandbox", (), {"create": staticmethod(create)})
        env = types.ModuleType("e2b_env")
        env.load_e2b_env = lambda: {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "result.json")
            text = io.StringIO()
            with patch.dict(sys.modules, {"e2b": sdk, "e2b_env": env}), \
                 patch.dict(perf.os.environ, {"OUT_JSON": str(output)}), \
                 patch.object(perf, "MemHog", FakeHog), contextlib.redirect_stdout(text):
                self.assertEqual(perf.main(), 0)
            data = json.loads(output.read_text())
            self.assertEqual(data["verdict"], "PASS")
            self.assertEqual([len(data[k]) for k in ("baseline", "create", "restore")], [6, 6, 6])
            self.assertTrue(all(b.killed for b in boxes))
            self.assertEqual(len(boxes), 6)
            self.assertIn("max |SDK - journal| = 0.000 ms", text.getvalue())


class ResidentTests(unittest.TestCase):
    def test_real_resident_allocates_exact_payload_and_checksums_all_bytes(self):
        # 运行真实负载程序，但路径全部替换到临时目录；不需要 SDK、root 或虚拟机。
        for payload_mb in (0, 1):
            with self.subTest(payload_mb=payload_mb), tempfile.TemporaryDirectory() as directory:
                source = perf.RESIDENT_PY.replace("/home/user-sandbox", directory)
                process = subprocess.Popen([sys.executable, "-c", source, str(payload_mb)], stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    pid_file = Path(directory, "memhog.pid")
                    while not pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(pid_file.exists())
                    def ask(nonce, op, arg=""):
                        Path(directory, "memhog.cmd").write_text(f"{nonce} {op} {arg}")
                        process.send_signal(signal.SIGUSR1)
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            text = Path(directory, "memhog.rsp").read_text()
                            if text.startswith(nonce + " "):
                                return text[len(nonce) + 1:].strip()
                            time.sleep(0.01)
                        self.fail("负载程序未响应")

                    state = json.loads(ask("n1", "state"))
                    expected = bytearray(payload_mb << 20)
                    for i in range(0, len(expected), 4096):
                        expected[i] = 1
                    self.assertEqual(state["payload_bytes"], len(expected))
                    self.assertEqual(state["sha256"], hashlib.sha256(expected).hexdigest())
                    self.assertEqual(ask("n2", "dirty", str(payload_mb)), f"ok:{payload_mb}")
                    changed = json.loads(ask("n3", "state"))
                    if payload_mb:
                        expected = hashlib.md5(b"n2").digest() * (len(expected) // 16)
                    self.assertEqual(changed["sha256"], hashlib.sha256(expected).hexdigest())
                    self.assertTrue(ask("n4", "dirty", str(payload_mb + 1)).startswith("ERR:"))
                finally:
                    process.kill()
                    process.communicate(timeout=5)


class JournalTests(unittest.TestCase):
    def test_delayed_journal_is_retried_outside_timing(self):
        log = json.dumps({"checkpoint_id": "cp", "message": "checkpoint created",
                          "criu_dump_ms": 2.0, "total_ms": 3.0})
        command = Mock(side_effect=[types.SimpleNamespace(stdout=""), types.SimpleNamespace(stdout=log)])
        sb = types.SimpleNamespace(commands=types.SimpleNamespace(run=command))
        with patch.object(perf.time, "sleep"):
            timing = perf.gsd_timing_for(sb, "cp")
        self.assertEqual(command.call_count, 2)
        self.assertEqual(timing, {"criu_dump_ms": 2.0, "total_ms": 3.0})

    def test_restore_does_not_take_old_create_total(self):
        old = json.dumps({"checkpoint_id": "base", "message": "checkpoint created",
                          "criu_dump_ms": 2.0, "total_ms": 100.0})
        partial = old + '\n' + json.dumps({"checkpoint_id": "base", "message": "starting restore"})
        partial += '\n' + json.dumps({"checkpoint_id": "base", "criu_restore_ms": 3.0})
        complete = partial + '\n' + json.dumps({"checkpoint_id": "base", "message": "restore completed", "total_ms": 4.0})
        command = Mock(side_effect=[types.SimpleNamespace(stdout=partial), types.SimpleNamespace(stdout=complete)])
        sb = types.SimpleNamespace(commands=types.SimpleNamespace(run=command))
        with patch.object(perf.time, "sleep"):
            timing = perf.gsd_timing_for(sb, "base", operation="restore")
        self.assertEqual(command.call_count, 2)
        self.assertEqual(timing, {"criu_restore_ms": 3.0, "total_ms": 4.0})


if __name__ == "__main__":
    unittest.main()
