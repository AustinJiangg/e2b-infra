"""检查当前激活的 jll-e2b-gsd 及已安装 SDK，不联网、不创建沙箱。"""
import importlib.metadata as metadata
import json
import os
import sys
from pathlib import Path


def check():
    prefix = Path(sys.prefix).resolve()
    active = os.environ.get("CONDA_PREFIX")
    if not active or Path(active).resolve() != prefix or prefix.name != "jll-e2b-gsd":
        raise RuntimeError("请先 conda activate jll-e2b-gsd，再用该环境的 python 执行")
    if os.environ.get("PYTHONPATH") or os.environ.get("PYTHONHOME"):
        raise RuntimeError("请先 unset PYTHONPATH PYTHONHOME")
    import ssl, sqlite3, ctypes
    import httpx, attrs, dateutil, wcmatch, google.protobuf, rich, dockerfile_parse, dotenv
    import e2b, e2b_connect
    from e2b import Sandbox, Template
    from e2b.sandbox_sync.checkpoint import Checkpoint
    for module in (e2b, e2b_connect):
        if not Path(module.__file__).resolve().is_relative_to(prefix):
            raise RuntimeError("SDK 未安装在当前 conda 环境: " + str(module.__file__))
    marker = Path(e2b.__file__).parent / "_gsd_offline_install.json"
    if not marker.is_file() or json.loads(marker.read_text()).get("kind") != "gsd-process-checkpoint-sdk":
        raise RuntimeError("当前环境尚未安装本包 GSD SDK，请执行 python ops/install_sdk.py")
    if metadata.version("e2b") != "2.19.0+gsd.dcca61266":
        raise RuntimeError("当前 e2b 发行包版本与交付不符")
    print("Python:", sys.version.split()[0])
    print("conda 环境:", sys.prefix)
    print("SDK:", e2b.__file__)
    print("GSD SDK 与依赖检查 PASS")


if __name__ == "__main__":
    try:
        check()
    except (RuntimeError, ImportError, OSError, ValueError) as exc:
        print("停止:", exc, file=sys.stderr)
        sys.exit(1)
