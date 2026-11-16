"""只在激活的 jll-e2b-gsd 中离线替换 SDK，不更改原 jll-e2b。"""
import importlib.metadata as metadata
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from email.parser import BytesParser

ROOT = Path(__file__).resolve().parents[1]  # deploy/
VERSION = "2.19.0+gsd.dcca61266"


def guard():
    prefix = Path(sys.prefix).resolve()
    active = os.environ.get("CONDA_PREFIX")
    if not active or Path(active).resolve() != prefix or prefix.name != "jll-e2b-gsd" or not (prefix / "conda-meta").is_dir():
        raise RuntimeError("请先 conda activate jll-e2b-gsd；拒绝在 jll-e2b、base 或系统 Python 中安装")
    if os.environ.get("PYTHONPATH") or os.environ.get("PYTHONHOME"):
        raise RuntimeError("请先 unset PYTHONPATH PYTHONHOME，避免导入包外 SDK")
    return prefix


def main():
    prefix = guard()
    wheels = list((ROOT / "sdk").glob("e2b-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("SDK wheel 缺失或不唯一")
    subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS"], cwd=ROOT, check=True)
    # 依赖来自克隆的环境；缺依赖时在替换 SDK 前停下，不尝试联网。
    from pip._vendor.packaging.requirements import Requirement
    with zipfile.ZipFile(wheels[0]) as archive:
        meta = BytesParser().parsebytes(archive.read("e2b-" + VERSION + ".dist-info/METADATA"))
    missing = []
    for text in meta.get_all("Requires-Dist", []):
        req = Requirement(text)
        try:
            version = metadata.version(req.name)
            if version not in req.specifier:
                missing.append(text + "（当前 " + version + "）")
        except metadata.PackageNotFoundError:
            missing.append(text)
    try:
        metadata.version("python-dotenv")
    except metadata.PackageNotFoundError:
        missing.append("python-dotenv（验收脚本依赖）")
    if missing:
        raise RuntimeError("克隆环境缺少依赖或版本不满足，SDK 尚未替换；需补离线依赖: " + ", ".join(missing))
    print("仅替换 SDK 的目标环境:", prefix, flush=True)
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
                    "--no-cache-dir", "--disable-pip-version-check", "--force-reinstall", str(wheels[0])], check=True)
    subprocess.run([sys.executable, str(ROOT / "ops/check_python.py")], check=True)
    subprocess.run([sys.executable, str(ROOT / "ops/check_sdk_headers.py")], check=True)
    print("GSD SDK 离线安装完成。后续 conda activate jll-e2b-gsd 后直接运行 python。")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ImportError, subprocess.CalledProcessError) as exc:
        print("停止:", exc, file=sys.stderr)
        sys.exit(1)
