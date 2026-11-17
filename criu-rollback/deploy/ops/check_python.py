"""检查当前的 jll-e2b-criu 及已安装 SDK，不联网、不创建沙箱。"""
import importlib.metadata as metadata
import json
import os
import sys
from pathlib import Path


def check():
    prefix = Path(sys.prefix).resolve()
    active = os.environ.get("CONDA_PREFIX")
    if not active or Path(active).resolve() != prefix or prefix.name != "jll-e2b-criu":
        raise RuntimeError("要用进程级 conda 环境 jll-e2b-criu 的 python 执行（950.sh 会自动选用）")
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
    # 标记文件随 wheel 发布，文件名与 kind 是发布时定的，不随 criu 改名变化
    marker = Path(e2b.__file__).parent / "_gsd_offline_install.json"
    if not marker.is_file() or json.loads(marker.read_text()).get("kind") != "gsd-process-checkpoint-sdk":
        raise RuntimeError("当前环境尚未安装进程级 SDK，执行 bash criu-rollback/deploy/950.sh install-sdk")
    if metadata.version("e2b") != "2.19.0+gsd.dcca61266":
        raise RuntimeError("当前 e2b 发行包版本与交付不符")
    print("Python:", sys.version.split()[0])
    print("conda 环境:", sys.prefix)
    print("SDK:", e2b.__file__)
    print("进程级 SDK 与依赖检查 PASS")


if __name__ == "__main__":
    try:
        check()
    except (RuntimeError, ImportError, OSError, ValueError) as exc:
        print("停止:", exc, file=sys.stderr)
        sys.exit(1)
