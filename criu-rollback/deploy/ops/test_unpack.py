"""展开步骤的本地单测：清单完整、镜像配置即镜像 ID、解压后摘要不符时拒绝。不写 .work/。"""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import paths
import unpack


class UnpackTests(unittest.TestCase):
    def setUp(self):
        self.m = unpack.manifest()

    def test_manifest_files_exist(self):
        for item in self.m["artifacts"].values():
            self.assertTrue((paths.ROOT / item["packed"]).is_file(), item["packed"])
        img = self.m["image"]
        for key in ("config", "manifest", "repositories"):
            self.assertTrue((paths.ROOT / img[key]).is_file(), img[key])
        for src in img["extra"].values():
            self.assertTrue((paths.ROOT / src).is_file(), src)
        self.assertEqual([l["source"] for l in img["layers"]][:2], ["base", "base"])

    def test_config_digest_is_image_id(self):
        img = self.m["image"]
        self.assertEqual(unpack.sha_file(paths.ROOT / img["config"]), img["image_id"])

    def only_gsd(self, m):
        m = copy.deepcopy(m)
        m["artifacts"] = {"gsd": m["artifacts"]["gsd"]}
        return m

    def test_binary_unpacks_and_matches(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(paths, "FC", Path(tmp)):
            unpack.unpack_binaries(self.only_gsd(self.m))
            self.assertEqual(unpack.sha_file(Path(tmp) / "gsd"), self.m["artifacts"]["gsd"]["sha256"])

    def test_wrong_digest_refused(self):
        m = self.only_gsd(self.m)
        m["artifacts"]["gsd"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmp, patch.object(paths, "FC", Path(tmp)):
            with self.assertRaisesRegex(RuntimeError, "SHA256 不符"):
                unpack.unpack_binaries(m)
            self.assertFalse((Path(tmp) / "gsd").exists())


if __name__ == "__main__":
    unittest.main()
