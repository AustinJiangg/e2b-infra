"""仅本地测试流程入口的判读与环境定位，不调用 Nomad、conda 或 API。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import envs
import flow


class FlowTests(unittest.TestCase):
    def test_netdiff_reports_only_changes(self):
        before = {"links": ["eth0", "veth-1"], "routes": ["default via 1.1.1.1"]}
        after = {"links": ["eth0"], "routes": ["default via 1.1.1.1", "10.11.0.0/16 dev br0"]}
        self.assertEqual(flow.netdiff(before, after), ["links - veth-1", "routes + 10.11.0.0/16 dev br0"])

    def test_e2b_network_excluded(self):
        for text in ("e2bv-12", "ns-305", "10.13.0.0/16 dev e2bv-3", "-A POSTROUTING -s 10.14.0.0/16 -j MASQUERADE"):
            self.assertTrue(flow.E2B_NET.search(text), text)
        for text in ("veth-1", "eth0", "-A FORWARD -i br-agent -j ACCEPT", "10.11.0.0/16 dev br0"):
            self.assertFalse(flow.E2B_NET.search(text), text)

    def test_template_rebuilt_when_gsd_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fc, state = Path(tmp) / "fc", Path(tmp) / "state"
            fc.mkdir()
            (fc / "gsd").write_bytes(b"gsd v1")
            (fc / "orchestrator").write_bytes(b"orch v1")
            with patch.dict("os.environ", {"CRIU_FC": str(fc), "CRIU_STATE_DIR": str(state)}):
                self.assertIn("未记录的旧版", flow.template_gsd_stale())
                with patch.object(flow, "build_template"):
                    flow.build_template_marked(Path(tmp))
                self.assertIsNone(flow.template_gsd_stale())
                (fc / "gsd").write_bytes(b"gsd v2")
                self.assertIn("gsd 是", flow.template_gsd_stale())
                self.assertNotIn("orchestrator 是", flow.template_gsd_stale())
                with patch.object(flow, "build_template"):
                    flow.build_template_marked(Path(tmp))
                (fc / "orchestrator").write_bytes(b"orch v2")
                self.assertIn("orchestrator 是", flow.template_gsd_stale())

    def test_old_gsd_only_mark_rebuilds_once(self):
        # 2026-11-19 以前的记录只有一行 gsd 的 sha256：算没记 orchestrator，重建一次。
        with tempfile.TemporaryDirectory() as tmp:
            fc, state = Path(tmp) / "fc", Path(tmp) / "state"
            fc.mkdir()
            (fc / "gsd").write_bytes(b"gsd v1")
            (fc / "orchestrator").write_bytes(b"orch v1")
            with patch.dict("os.environ", {"CRIU_FC": str(fc), "CRIU_STATE_DIR": str(state)}):
                flow.template_gsd_mark().parent.mkdir(parents=True)
                flow.template_gsd_mark().write_text(flow.stack.sha(fc / "gsd") + "\n")
                reason = flow.template_gsd_stale()
                self.assertIn("orchestrator 是 未记录的旧版", reason)
                self.assertNotIn("gsd 是", reason)

    def test_transient_errors(self):
        self.assertTrue(flow.TRANSIENT.search("500: Failed to place sandbox"))
        self.assertTrue(flow.TRANSIENT.search("503 Error when getting available build client"))
        self.assertFalse(flow.TRANSIENT.search("manifest unknown: image not found"))

    def test_env_by_name_and_path(self):
        listing = [Path("/root/miniforge3"), Path("/root/miniforge3/envs/jll-e2b"),
                   Path("/root/miniforge3/envs/jll-e2b-criu"), Path("/data/tmp/conda/jll-e2b-criu")]
        self.assertEqual(envs.prefix("jll-e2b-criu", listing), Path("/root/miniforge3/envs/jll-e2b-criu"))
        self.assertIsNone(envs.prefix("missing", listing))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "jll-e2b-criu"
            self.assertIsNone(envs.prefix(str(target)))
            (target / "conda-meta").mkdir(parents=True)
            self.assertEqual(envs.prefix(str(target)), target)

    def test_duplicate_env_names_refused(self):
        listing = [Path("/a/envs/jll-e2b"), Path("/b/envs/jll-e2b")]
        with self.assertRaises(RuntimeError):
            envs.prefix("jll-e2b", listing)

    def test_runtime_sets_conda_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "jll-e2b-criu"
            (target / "conda-meta").mkdir(parents=True)
            with patch.dict(envs.os.environ, {"PYTHONPATH": "/x", "PATH": "/usr/bin"}, clear=True):
                command, env = envs.runtime(str(target))
        self.assertEqual(command, [str(target / "bin/python")])
        self.assertEqual(env["CONDA_PREFIX"], str(target))
        self.assertNotIn("PYTHONPATH", env)
        self.assertTrue(env["PATH"].startswith(str(target / "bin")))

    def test_roll_back_unless_sandbox(self):
        # 切换中途失败、5008 上没有进程（current() 为 None）时也要回切；已是沙箱级才不动
        for cur, expect in (("criu", True), (None, True), ("sandbox", False)):
            with patch.object(flow.stack, "current", return_value=cur), \
                    patch.object(flow.stack, "switch") as sw, \
                    patch.object(flow, "clear_e2b_sandboxes"), \
                    patch.object(flow, "log"):
                flow.roll_back()
            self.assertEqual(sw.called, expect, cur)
            if expect:
                sw.assert_called_once_with("sandbox")


if __name__ == "__main__":
    unittest.main()
