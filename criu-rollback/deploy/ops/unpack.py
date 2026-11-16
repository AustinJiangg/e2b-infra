"""把入库的压缩产物展开到 .work/，并逐项核对 manifest.json。只用标准库，现场不需要 xz 命令。

- 三个运行文件：xz 解压到 .work/fc-gsd/，按 manifest 核对 SHA256；
- 镜像：前两层从仓库根目录的 ubuntu-22.04-custom.tar.gz 取，第三层（CRIU）从
  artifacts/image/criu421-layer.tar.xz 解压，加上原样保存的配置与清单，拼成 docker load 能读的归档。
  每层按 diff_id 核对，配置文件的 SHA256 就是镜像 ID，所以拼出的镜像与验收过的同一个。
已展开且核对通过时直接返回。
"""
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import sys
import tarfile

import paths

CHUNK = 1 << 20


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def copy_hashed(src, dst):
    """流式写出并同时算 SHA256，写到临时文件，调用方核对后再改名。"""
    h = hashlib.sha256()
    tmp = Path(str(dst) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as out:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            h.update(chunk)
            out.write(chunk)
    return tmp, h.hexdigest()


def manifest():
    return json.loads(paths.MANIFEST.read_text())


def unpacked_ok(m):
    for item in m["artifacts"].values():
        p = paths.FC / item["path"]
        if not p.is_file() or sha_file(p) != item["sha256"]:
            return False
    stamp = paths.WORK / "image" / "image-id"
    return paths.IMAGE_TAR.is_file() and stamp.is_file() and stamp.read_text().strip() == m["image"]["image_id"]


def unpack_binaries(m):
    for name, item in m["artifacts"].items():
        dst = paths.FC / item["path"]
        with lzma.open(paths.ROOT / item["packed"]) as src:
            tmp, digest = copy_hashed(src, dst)
        require(digest == item["sha256"], "%s 解压后 SHA256 不符：%s" % (name, digest))
        os.chmod(tmp, int(item["mode"], 8))
        tmp.replace(dst)
        print("展开", item["path"], digest[:12], flush=True)


def build_image(m):
    img = m["image"]
    layers_dir = paths.WORK / "image" / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    want = {layer["diff_id"]: layer for layer in img["layers"]}
    got = {}
    # 前两层：仓库根目录的基础镜像（docker save 旧格式，每层是 <id>/layer.tar）
    require(paths.BASE_IMAGE.is_file() and paths.BASE_IMAGE.stat().st_size > 1000,
            "缺少 %s（或只是 LFS 指针）；它是部署沙箱级用的基础镜像，应随仓库存在" % paths.BASE_IMAGE)
    with tarfile.open(paths.BASE_IMAGE, "r:gz") as base:
        for member in base:
            if not member.isfile() or not member.name.endswith("layer.tar"):
                continue
            tmp, digest = copy_hashed(base.extractfile(member), layers_dir / "candidate")
            diff_id = "sha256:" + digest
            if diff_id in want and want[diff_id]["source"] == "base":
                tmp.replace(layers_dir / digest)
                got[diff_id] = layers_dir / digest
            else:
                tmp.unlink()
    # 第三层：CRIU
    for diff_id, layer in want.items():
        if layer["source"] != "base":
            with lzma.open(paths.ROOT / layer["source"]) as src:
                tmp, digest = copy_hashed(src, layers_dir / "candidate")
            require("sha256:" + digest == diff_id, "CRIU 层解压后摘要不符：" + digest)
            tmp.replace(layers_dir / digest)
            got[diff_id] = layers_dir / digest
    missing = [d for d in want if d not in got]
    require(not missing, "基础镜像里找不到这些层：%s；仓库根目录的 ubuntu-22.04-custom.tar.gz 与验收时不同" % missing)
    config = paths.ROOT / img["config"]
    require(sha_file(config) == img["image_id"], "镜像配置的 SHA256 与镜像 ID 不符")
    tmp = Path(str(paths.IMAGE_TAR) + ".tmp")
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as out:
        out.add(config, arcname="blobs/sha256/" + img["image_id"])
        for layer in img["layers"]:
            digest = layer["diff_id"].split(":", 1)[1]
            out.add(got[layer["diff_id"]], arcname="blobs/sha256/" + digest)
        out.add(paths.ROOT / img["manifest"], arcname="manifest.json")
        out.add(paths.ROOT / img["repositories"], arcname="repositories")
        for name, src in img.get("extra", {}).items():
            out.add(paths.ROOT / src, arcname=name)
    tmp.replace(paths.IMAGE_TAR)
    shutil.rmtree(layers_dir)
    (paths.WORK / "image" / "image-id").write_text(img["image_id"] + "\n")
    print("拼出镜像", img["ref"], "ID", img["image_id"][:12], flush=True)


def unpack():
    m = manifest()
    if unpacked_ok(m):
        print("已展开，核对通过:", paths.WORK, flush=True)
        return
    unpack_binaries(m)
    build_image(m)
    require(unpacked_ok(m), "展开后复核失败")
    print("展开完成:", paths.WORK, flush=True)


if __name__ == "__main__":
    try:
        unpack()
    except (RuntimeError, OSError, lzma.LZMAError, tarfile.TarError, KeyError) as exc:
        print("停止:", exc, file=sys.stderr)
        sys.exit(1)
