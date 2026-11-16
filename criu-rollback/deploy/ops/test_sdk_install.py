"""安装环境保护和 wheel 内容测试，不安装软件、不访问网络。"""
import base64
import csv
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import install_sdk


class SDKInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, name, active=True, extra=None):
        prefix = self.base / name
        (prefix / 'conda-meta').mkdir(parents=True, exist_ok=True)
        env = {'CONDA_PREFIX': str(prefix)} if active else {}
        env.update(extra or {})
        with patch.object(install_sdk.sys, 'prefix', str(prefix)), patch.dict(os.environ, env, clear=True):
            return install_sdk.guard()

    def test_original_environment_refused(self):
        with self.assertRaises(RuntimeError): self.guard('jll-e2b')

    def test_base_refused(self):
        with self.assertRaises(RuntimeError): self.guard('base')

    def test_unactivated_environment_refused(self):
        with self.assertRaises(RuntimeError): self.guard('jll-e2b-gsd', active=False)

    def test_pythonpath_override_refused(self):
        with self.assertRaises(RuntimeError): self.guard('jll-e2b-gsd', extra={'PYTHONPATH':'/other/sdk'})

    def test_gsd_clone_accepted(self):
        self.assertEqual(self.guard('jll-e2b-gsd').name, 'jll-e2b-gsd')

    def test_wheel_record_matches_content(self):
        root = Path(__file__).resolve().parents[1]
        wheel = next((root/'sdk').glob('*.whl'))
        with zipfile.ZipFile(wheel) as archive:
            self.assertIsNone(archive.testzip())
            record = next(n for n in archive.namelist() if n.endswith('.dist-info/RECORD'))
            for name, digest, size in csv.reader(io.StringIO(archive.read(record).decode())):
                if digest:
                    data = archive.read(name)
                    self.assertEqual(len(data), int(size))
                    self.assertEqual('sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='), digest)

if __name__ == '__main__':
    unittest.main()
