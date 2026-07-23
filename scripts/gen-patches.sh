#!/usr/bin/env bash
# 从 e2b-infra-arm 源码仓库生成单一 ARM 适配补丁（0001），覆盖本仓库根目录同名文件。
# 补丁 = 源码仓库 upstream-2026.09 基线 tag 与目标提交之间的全量 diff，
# 与源码仓库的提交数量/结构无关——改完代码随意提交，跑本脚本即可。
# 用法: scripts/gen-patches.sh <e2b-infra-arm 仓库路径> [目标ref，默认HEAD]
# 详见 deploy-docs/08-补丁规范流程.md
set -euo pipefail

ARM_REPO=${1:?用法: gen-patches.sh <e2b-infra-arm 仓库路径> [目标ref]}
REF=${2:-HEAD}
BUILD_REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT="$BUILD_REPO/0001-adapted-for-arm-architecture.patch"

BASE=$(git -C "$ARM_REPO" rev-parse "upstream-2026.09^{commit}") \
    || { echo "错误: 源码仓库缺少 upstream-2026.09 基线 tag"; exit 1; }
HEAD_SHA=$(git -C "$ARM_REPO" rev-parse --short "$REF")

{
    echo "e2b-infra ARM 单机适配补丁（单一补丁，由 scripts/gen-patches.sh 生成，勿手改）"
    echo "基线: upstream-2026.09 (官方 e2b-dev/infra 2026.09)"
    echo "源码仓库: e2b-infra-arm @ $HEAD_SHA"
    echo "---"
    git -C "$ARM_REPO" diff --stat "$BASE" "$REF"
    echo
    git -C "$ARM_REPO" diff "$BASE" "$REF"
} > "$OUT"

echo "已生成: $OUT"
echo "  文件数: $(grep -c '^diff --git' "$OUT")  总行数: $(wc -l < "$OUT")"
echo
echo "后续步骤:"
echo "  1. git -C $BUILD_REPO diff --stat                    # 检查 patch 变化"
echo "  2. rpmbuild -bb e2b-infra.spec --define \"_sourcedir $BUILD_REPO\"   # 服务器上验证构建"
echo "  3. 通过后两个仓库分别 commit + push"
echo
echo "提醒: 若本次修改引入了新的第三方 Go 依赖（go.mod 增加了模块），"
echo "      需重新 go mod vendor 并重打 e2b-infra-2026.09.tar.gz 上传 LFS，"
echo "      否则 rpmbuild 的离线 -mod=vendor 编译会因 vendor 不一致而失败。"
