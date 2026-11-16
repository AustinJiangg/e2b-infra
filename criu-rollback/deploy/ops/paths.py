"""部署目录与仓库里各处位置的唯一定义。只用标准库。

deploy/ 在 e2b-infra 仓库的 criu-rollback/deploy/ 下：
  测试脚本用同仓库的 criu-rollback/scripts，沙箱级冒烟用 rollback/scripts/950/run.sh，
  镜像前两层取仓库根目录的 ubuntu-22.04-custom.tar.gz。
"""
import os
from pathlib import Path

ROOT = Path(os.environ.get("GSD_BUNDLE") or Path(__file__).resolve().parents[1])
REPO = ROOT.parents[1]
WORK = ROOT / ".work"                       # 展开后的运行文件与镜像，不入库
FC = WORK / "fc-gsd"                        # 与宿主 /fc-gsd 同样的布局
IMAGE_TAR = WORK / "image" / "ubuntu-22.04-custom-criu421.tar"
SCRIPTS = REPO / "criu-rollback" / "scripts"
SMOKE = REPO / "rollback" / "scripts" / "950" / "run.sh"
BASE_IMAGE = REPO / "ubuntu-22.04-custom.tar.gz"
RESULTS = ROOT / "results"
MANIFEST = ROOT / "manifest.json"
