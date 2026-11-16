#!/usr/bin/env bash
# 在 950 或能访问其 Harbor 的 arm64 导入机运行。不会联网拉镜像。
# 镜像先由 ops/unpack.py 从仓库里的基础镜像和 CRIU 层拼出（已拼好则直接复用）。
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
TARGET=${GSD_TEMPLATE_IMAGE:-harbor:443/e2b-orchestration/ubuntu:22.04-custom-criu421}
IMAGE=sha256:7caae4a62095a4371cbef9d4cf31a6734463f6cda976d34d95f49f9ae78a96ab
cd "$ROOT"
sha256sum --quiet -c SHA256SUMS
"${GSD_HOST_PYTHON:-python3}" "$ROOT/ops/unpack.py"
docker load -i .work/image/ubuntu-22.04-custom-criu421.tar
[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}')" = arm64 ]
docker run --pull=never --rm --network none --entrypoint /bin/sh "$IMAGE" -ec '
  test "$(/usr/sbin/criu --version)" = "Version: 4.2.1"
  command -v curl
  command -v python3
  for p in systemd systemd-sysv openssh-server sudo chrony linuxptp socat curl ca-certificates python3; do
    dpkg-query -W "$p"
  done
'
docker tag "$IMAGE" "$TARGET"
docker push "$TARGET"
echo "镜像已推送到 $TARGET；后续用完全相同的引用建模板。"
