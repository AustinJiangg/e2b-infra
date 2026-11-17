"""展开步骤的本地单测：清单完整、镜像配置即镜像 ID、解压后摘要不符时拒绝。不写 .work/。"""
import copy
import json
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

    def test_oci_index_complete(self):
        """index.json 指向的清单、清单里的配置与各层都要在归档里，否则 containerd 存储导入出 0B 的空镜像。"""
        img = self.m["image"]
        names = set(img["extra"]) | {"blobs/sha256/" + img["image_id"]}
        names |= {"blobs/sha256/" + l["diff_id"].split(":", 1)[1] for l in img["layers"]}
        index = json.loads((paths.ROOT / img["extra"]["index.json"]).read_text())
        for desc in index["manifests"]:
            name = "blobs/sha256/" + desc["digest"].split(":", 1)[1]
            self.assertIn(name, names)
            blob = paths.ROOT / img["extra"][name]
            self.assertEqual("sha256:" + unpack.sha_file(blob), desc["digest"])
            man = json.loads(blob.read_text())
            self.assertEqual(man["config"]["digest"], "sha256:" + img["image_id"])
            for layer in man["layers"]:
                self.assertIn("blobs/sha256/" + layer["digest"].split(":", 1)[1], names)

    def test_stamp_changes_with_extra(self):
        m = copy.deepcopy(self.m)
        before = unpack.stamp_text(m)
        m["image"]["extra"].pop(next(k for k in m["image"]["extra"] if k.startswith("blobs/")))
        self.assertNotEqual(unpack.stamp_text(m), before)

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
