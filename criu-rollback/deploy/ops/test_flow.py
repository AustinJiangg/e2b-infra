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

    def test_transient_errors(self):
        self.assertTrue(flow.TRANSIENT.search("500: Failed to place sandbox"))
        self.assertTrue(flow.TRANSIENT.search("503 Error when getting available build client"))
        self.assertFalse(flow.TRANSIENT.search("manifest unknown: image not found"))

    def test_env_by_name_and_path(self):
        listing = [Path("/root/miniforge3"), Path("/root/miniforge3/envs/jll-e2b"),
                   Path("/root/miniforge3/envs/jll-e2b-gsd"), Path("/data/tmp/conda/jll-e2b-gsd")]
        self.assertEqual(envs.prefix("jll-e2b-gsd", listing), Path("/root/miniforge3/envs/jll-e2b-gsd"))
        self.assertIsNone(envs.prefix("missing", listing))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "jll-e2b-gsd"
            self.assertIsNone(envs.prefix(str(target)))
            (target / "conda-meta").mkdir(parents=True)
            self.assertEqual(envs.prefix(str(target)), target)

    def test_duplicate_env_names_refused(self):
        listing = [Path("/a/envs/jll-e2b"), Path("/b/envs/jll-e2b")]
        with self.assertRaises(RuntimeError):
            envs.prefix("jll-e2b", listing)

    def test_runtime_sets_conda_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "jll-e2b-gsd"
            (target / "conda-meta").mkdir(parents=True)
            with patch.dict(envs.os.environ, {"PYTHONPATH": "/x", "PATH": "/usr/bin"}, clear=True):
                command, env = envs.runtime(str(target))
        self.assertEqual(command, [str(target / "bin/python")])
        self.assertEqual(env["CONDA_PREFIX"], str(target))
        self.assertNotIn("PYTHONPATH", env)
        self.assertTrue(env["PATH"].startswith(str(target / "bin")))


if __name__ == "__main__":
    unittest.main()
