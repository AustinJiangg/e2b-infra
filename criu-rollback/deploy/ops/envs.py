"""定位 conda 和两个客户端环境，用环境自己的解释器运行脚本，不需要手动 conda activate。只用标准库。

环境用名字或绝对路径指定，默认进程级 jll-e2b-gsd、沙箱级 jll-e2b，可在 ops/site.env 里用
GSD_CONDA_ENV / SANDBOX_CONDA_ENV 改。用路径时，进程级环境目录名也必须是 jll-e2b-gsd。
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

GSD_ENV = os.environ.get("GSD_CONDA_ENV", "jll-e2b-gsd")
SANDBOX_ENV = os.environ.get("SANDBOX_CONDA_ENV", "jll-e2b")
CONDA_HOMES = ("/root/miniforge3", "/root/miniconda3", "/root/anaconda3",
               "/opt/miniforge3", "/opt/miniconda3", "/opt/conda")


def conda():
    for path in (os.environ.get("CONDA_EXE"), shutil.which("conda"),
                 *(home + "/bin/conda" for home in CONDA_HOMES)):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise RuntimeError("找不到 conda；在 ops/site.env 里写 CONDA_EXE=conda 可执行文件的绝对路径")


def env_list():
    return [Path(p) for p in json.loads(subprocess.check_output([conda(), "env", "list", "--json"], text=True))["envs"]]


def prefix(spec, listing=None):
    """返回环境目录；不存在返回 None。同名环境不止一个时要求改用绝对路径。"""
    if "/" in spec:
        path = Path(spec)
        return path if (path / "conda-meta").is_dir() else None
    hits = [p for p in (listing if listing is not None else env_list())
            if p.name == spec and p.parent.name == "envs"]
    if len(hits) > 1:
        raise RuntimeError("名为 %s 的 conda 环境不止一个：%s；在 ops/site.env 里改用绝对路径" % (spec, hits))
    return hits[0] if hits else None


def runtime(spec):
    path = prefix(spec)
    if path is None:
        raise RuntimeError("conda 环境不存在: " + spec)
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE"):
        env.pop(key, None)
    env.update(CONDA_PREFIX=str(path), CONDA_DEFAULT_ENV=path.name,
               PATH=str(path / "bin") + os.pathsep + env.get("PATH", ""))
    return [str(path / "bin" / "python")], env


def run(spec, args, **kwargs):
    command, env = runtime(spec)
    return subprocess.run(command + [str(a) for a in args], env=env, **kwargs)


def clone(target, source):
    src = prefix(source)
    if src is None:
        raise RuntimeError("找不到要克隆的原沙箱级环境: " + source)
    where = ["-p", target] if "/" in target else ["-n", target]
    subprocess.run([conda(), "create", "--offline", "--copy", "--yes", *where, "--clone", str(src)], check=True)
