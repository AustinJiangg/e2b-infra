"""部署目录、仓库里各处位置和宿主上进程级各位置的唯一定义。只用标准库。

deploy/ 在 e2b-infra 仓库的 criu-rollback/deploy/ 下：
  测试脚本用同仓库的 criu-rollback/scripts，沙箱级冒烟用 rollback/scripts/950/run.sh，
  镜像前两层取仓库根目录的 ubuntu-22.04-custom.tar.gz。

宿主上进程级自己的位置：运行文件 /fc-criu/，切栈基线 /var/lib/criu-rollback/，客户端 conda 环境 jll-e2b-criu
（在 conda 的 envs 目录下）。都可以在 ops/site.env 里改。
"""
import os
from pathlib import Path

ROOT = Path(os.environ.get("CRIU_DEPLOY") or Path(__file__).resolve().parents[1])
REPO = ROOT.parents[1]
WORK = ROOT / ".work"                       # 展开后的运行文件与镜像，不入库
FC = WORK / "fc"                            # 与宿主 HOST_FC 同样的布局
IMAGE_TAR = WORK / "image" / "ubuntu-22.04-custom-criu421.tar"
SCRIPTS = REPO / "criu-rollback" / "scripts"
SMOKE = REPO / "rollback" / "scripts" / "950" / "run.sh"
BASE_IMAGE = REPO / "ubuntu-22.04-custom.tar.gz"
RESULTS = ROOT / "results"
MANIFEST = ROOT / "manifest.json"


def host_fc():
    """宿主上进程级的三个运行文件：orchestrator、gsd、kernels/vmlinux-6.1.158/vmlinux.bin。"""
    return Path(os.environ.get("CRIU_FC") or "/fc-criu")


def state_dir():
    """切栈基线（含 950 密钥，目录 700、文件 600）与 all 的进度。"""
    return Path(os.environ.get("CRIU_STATE_DIR") or "/var/lib/criu-rollback")


def conda_env():
    """进程级客户端 conda 环境：克隆 jll-e2b 得来的命名环境。"""
    return os.environ.get("CRIU_CONDA_ENV") or "jll-e2b-criu"
