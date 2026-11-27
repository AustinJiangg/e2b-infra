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

    def test_criu_change_preserves_original_and_other_settings(self):
        d = fixture(); original = copy.deepcopy(d)
        with patch.dict(stack.os.environ, {"CRIU_FC": "/fc-criu"}), \
                patch.object(stack, "sha", return_value="orchhash"):
            new = stack.make_criu(d)
        self.assertEqual(d, original)
        t = stack.select_task(new)[1]
        self.assertEqual(stack.binary(t), "/fc-criu/orchestrator")
        self.assertEqual(t["Env"]["HOST_KERNELS_DIR"], "/fc-criu/kernels")
        self.assertEqual(t["Env"]["HOST_GSD_PATH"], "/fc-criu/gsd")
        self.assertEqual(t["Env"]["CRIU_ORCH_SHA256"], "orchhash")
        self.assertEqual(t["Env"]["STORAGE_PROVIDER"], "Local")
        self.assertEqual(new["Job"]["TaskGroups"][0], original["Job"]["TaskGroups"][0])

    def test_job_diff(self):
        a = stack.normalize(fixture())
        b = copy.deepcopy(a)
        b["Job"]["NomadTokenID"] = "someone-else"        # 每次提交都会变，不算
        self.assertEqual(stack.job_diff(a, b), [])
        t = stack.select_task(b)[1]
        t["Env"]["CONSUL_TOKEN"] = "new"
        t["Env"]["STORAGE_PROVIDER"] = "GCS"
        diff = stack.job_diff(a, b)
        self.assertEqual(diff, ["环境变量 CONSUL_TOKEN、STORAGE_PROVIDER"])
        self.assertNotIn("new", "".join(diff))            # 只列名字，不列值
        t["Config"]["command"] = "/bin/sh"
        self.assertEqual(stack.job_diff(a, b)[1], "task 字段 Config")
        c = copy.deepcopy(a); c["Job"]["TaskGroups"][0]["Count"] = 2
        self.assertEqual(stack.job_diff(a, c), ["task group 其他设置"])

    def test_baseline_redeployed(self):
        import json, tempfile
        from pathlib import Path
        base = stack.normalize(fixture())
        sandbox = [{"pid": 1, "exe": "/usr/bin/template-manager", "sha256": "h1", "env": {}}]
        with tempfile.TemporaryDirectory() as d:
            st = Path(d)
            (st / "baseline.json").write_text(json.dumps(base))
            (st / "baseline.identity.json").write_text(json.dumps({"exe": __file__, "sha256": "h1"}))
            live = copy.deepcopy(base)

            def check(procs=sandbox, healthy=True, binsha="h1"):
                with patch.object(stack, "state", return_value=st), \
                        patch.object(stack, "processes", return_value=procs), \
                        patch.object(stack, "health", return_value=healthy), \
                        patch.object(stack, "sha", return_value=binsha), \
                        patch.object(stack, "load_job", return_value=live):
                    return stack.baseline_redeployed()

            self.assertIsNone(check())                                  # 没重新部署
            self.assertIn("二进制", check(binsha="h2"))                  # RPM 换了
            stack.select_task(live)[1]["Env"]["CONSUL_TOKEN"] = "new"   # 二进制不变、重新部署过
            self.assertIn("环境变量 CONSUL_TOKEN", check())
            criu = [dict(sandbox[0], exe=str(stack.hfc() / "orchestrator"))]
            self.assertIsNone(check(procs=criu))                        # 在进程级栈上不判断
            self.assertIsNone(check(healthy=False))
            with patch.object(stack, "load_job", side_effect=stack.CmdError(1, ["nomad"], output="denied")), \
                    patch.object(stack, "state", return_value=st), \
                    patch.object(stack, "processes", return_value=sandbox), \
                    patch.object(stack, "health", return_value=True), \
                    patch.object(stack, "sha", return_value="h1"):
                self.assertIsNone(stack.baseline_redeployed())          # 读不到现场 job 不动基线

    def test_default_paths(self):
        import paths
        with patch.dict(paths.os.environ, {}, clear=False):
            for k in ("CRIU_FC", "CRIU_STATE_DIR", "CRIU_CONDA_ENV"):
                paths.os.environ.pop(k, None)
            self.assertEqual(paths.host_fc(), paths.Path("/fc-criu"))
            self.assertEqual(paths.state_dir(), paths.Path("/var/lib/criu-rollback"))
            self.assertEqual(paths.conda_env(), "jll-e2b-criu")

if __name__ == "__main__":
    unittest.main()
