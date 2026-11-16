"""在激活的 jll-e2b 中执行沙箱级冒烟；凭据只通过子进程环境传递。"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

import paths
ROOT = paths.ROOT
sys.path.insert(0, str(paths.SCRIPTS))
from e2b_env import load_e2b_env

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--template", default="base")
parser.add_argument("--results-dir", type=Path, default=ROOT / "results/sandbox-smoke")
args = parser.parse_args()
prefix = Path(sys.prefix).resolve()
active = os.environ.get("CONDA_PREFIX")
if not active or Path(active).resolve() != prefix or prefix.name != "jll-e2b":
    parser.error("请先 conda activate jll-e2b，使用原沙箱级环境执行冒烟")
if os.environ.get("PYTHONPATH") or os.environ.get("PYTHONHOME"):
    parser.error("请先 unset PYTHONPATH PYTHONHOME，避免导入其他 SDK")
import e2b
if not Path(e2b.__file__).resolve().is_relative_to(prefix):
    parser.error("原 SDK 未从 jll-e2b 环境加载")
if (Path(e2b.__file__).parent / "_gsd_offline_install.json").exists():
    parser.error("jll-e2b 中发现 GSD SDK，不能作为原沙箱级环境验收")
os.environ.update(load_e2b_env())
env = dict(os.environ)
env.pop("PYTHONPATH", None)
env.pop("PYTHONHOME", None)
env.pop("PYTHONNOUSERSITE", None)
print("原沙箱级 SDK:", e2b.__file__, flush=True)
command = ["bash", str(paths.SMOKE), "smoke",
           "--python", sys.executable, "--env-file", "/dev/null", "--template", args.template,
           "--results-dir", str(args.results_dir)]
sys.exit(subprocess.call(command, env=env))
