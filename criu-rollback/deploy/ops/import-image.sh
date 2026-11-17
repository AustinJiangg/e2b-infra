#!/usr/bin/env bash
# 在 950 或能访问其 Harbor 的 arm64 导入机运行。不会联网拉镜像。
# 镜像先由 ops/unpack.py 从仓库里的基础镜像和 CRIU 层拼出（已拼好则直接复用）。
#
# 按标签引用导入后的镜像，不按 image ID：docker 的传统存储把镜像配置的摘要当 ID（7caae4a6…），
# containerd 镜像存储（950 的 docker）把 OCI 清单的摘要当 ID（b7a2fdd2…），两者都对应同一份内容。
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
TARGET=${CRIU_TEMPLATE_IMAGE:-harbor:443/e2b-orchestration/ubuntu:22.04-custom-criu421}
REF=ubuntu:22.04-custom-criu421
IDS="sha256:7caae4a62095a4371cbef9d4cf31a6734463f6cda976d34d95f49f9ae78a96ab sha256:b7a2fdd21a5bd5a9013aca2c3062b28425038199daf19825ffe4fe84d537b165"
cd "$ROOT"
sha256sum --quiet -c SHA256SUMS
"${CRIU_HOST_PYTHON:-python3}" "$ROOT/ops/unpack.py"
# 之前导入过不完整的同名镜像（缺 OCI 清单时 containerd 存储会留下 0B 的空镜像），先去掉这个标签
docker image rm "$REF" >/dev/null 2>&1 || true
docker load -i .work/image/ubuntu-22.04-custom-criu421.tar
id=$(docker image inspect "$REF" --format '{{.Id}}')
case " $IDS " in *" $id "*) ;; *) echo "导入后的镜像 ID 不对：$id（应为 $IDS 之一）" >&2; exit 1 ;; esac
arch=$(docker image inspect "$REF" --format '{{.Architecture}}')
[ "$arch" = arm64 ] || { echo "导入后的镜像架构不对：'$arch'（应为 arm64）" >&2; exit 1; }
echo "导入完成：$REF ID ${id:7:12} 架构 $arch"
docker run --pull=never --rm --network none --entrypoint /bin/sh "$REF" -ec '
  test "$(/usr/sbin/criu --version)" = "Version: 4.2.1"
  command -v curl
  command -v python3
  for p in systemd systemd-sysv openssh-server sudo chrony linuxptp socat curl ca-certificates python3; do
    dpkg-query -W "$p"
  done
'
docker tag "$REF" "$TARGET"
docker push "$TARGET"
echo "镜像已推送到 $TARGET；后续用完全相同的引用建模板。"
