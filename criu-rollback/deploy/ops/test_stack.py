"""仅本地测试 job 选择和生成，不调用 Nomad、不切换服务。"""
import copy
import unittest
from unittest.mock import patch
import stack


def fixture():
    return {"Job": {"ID": stack.JOB, "Version": 9, "Stop": False,
                    "TaskGroups": [{"Name": "unrelated", "Tasks": []},
                                   {"Name": "tm", "Tasks": [{"Name": "start", "Driver": "raw_exec",
                                    "Config": {"command": "/bin/bash", "args": ["-c", "chmod +x /usr/bin/template-manager && /usr/bin/template-manager --port 5008"]},
                                    "Env": {"ORCHESTRATOR_SERVICES": "orchestrator,template-manager",
                                            "HOST_GSD_PATH": "off", "STORAGE_PROVIDER": "Local"}}]}]}}


class StackTests(unittest.TestCase):
    def test_unwrapped_job(self):
        d = stack.normalize(fixture()["Job"])
        self.assertNotIn("Version", d["Job"])
        self.assertEqual(stack.binary(stack.select_task(d)[1]), "/usr/bin/template-manager")

    def test_other_job_refused(self):
        d = fixture(); d["Job"]["ID"] = "other"
        with self.assertRaises(RuntimeError): stack.normalize(d)

    def test_stopped_baseline_refused(self):
        d = fixture(); d["Job"]["Stop"] = True
        with self.assertRaises(RuntimeError): stack.normalize(d)

    def test_multiple_tasks_refused(self):
        d = fixture(); tasks = d["Job"]["TaskGroups"][1]["Tasks"]
        tasks.append(copy.deepcopy(tasks[0]))
        with self.assertRaises(RuntimeError): stack.select_task(d)

    def test_different_port_refused(self):
        d = fixture(); d["Job"]["TaskGroups"][1]["Tasks"][0]["Config"]["args"][1] += "0"
        with self.assertRaises(RuntimeError): stack.select_task(d)

    def test_gsd_change_preserves_original_and_other_settings(self):
        d = fixture(); original = copy.deepcopy(d)
        with patch.object(stack, "sha", return_value="gsdhash"):
            new = stack.make_gsd(d)
        self.assertEqual(d, original)
        t = stack.select_task(new)[1]
        self.assertEqual(stack.binary(t), "/fc-gsd/orchestrator")
        self.assertEqual(t["Env"]["HOST_KERNELS_DIR"], "/fc-gsd/kernels")
        self.assertEqual(t["Env"]["GSD_ORCH_SHA256"], "gsdhash")
        self.assertNotIn("HOST_GSD_PATH", t["Env"])
        self.assertEqual(t["Env"]["STORAGE_PROVIDER"], "Local")
        self.assertEqual(new["Job"]["TaskGroups"][0], original["Job"]["TaskGroups"][0])


if __name__ == "__main__":
    unittest.main()
