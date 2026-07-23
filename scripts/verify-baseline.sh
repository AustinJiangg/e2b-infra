#!/usr/bin/env bash
# 校验 LFS tarball（官方源码+vendor）的源码部分与 e2b-infra-arm 的
# upstream-2026.09 基线 tag 完全一致（vendor/ 不参与对比）。
# 需在能拿到真实 tarball 的机器上运行（先在本仓库 git lfs pull）。
# 用法: scripts/verify-baseline.sh <e2b-infra-arm 仓库路径>
set -euo pipefail

ARM_REPO=${1:?用法: verify-baseline.sh <e2b-infra-arm 仓库路径>}
BUILD_REPO=$(cd "$(dirname "$0")/.." && pwd)
TARBALL="$BUILD_REPO/e2b-infra-2026.09.tar.gz"

file "$TARBALL" | grep -q 'gzip' || {
    echo "错误: $TARBALL 仍是 LFS 指针文件，请先: cd $BUILD_REPO && git lfs pull"; exit 1; }
# 基线 = upstream-2026.09 tag；tag 不存在时（tag 无法经 git 代理同步）
# 回退为当前分支的根提交——基线正是本仓库唯一的根提交。
BASE=$(git -C "$ARM_REPO" rev-parse "upstream-2026.09^{commit}" 2>/dev/null) \
    || BASE=$(git -C "$ARM_REPO" rev-list --max-parents=0 HEAD)
[ -n "$BASE" ] || { echo "错误: 无法定位基线提交"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "解包 tarball ..."
tar -xzf "$TARBALL" -C "$TMP"
SRC="$TMP/e2b-infra-2026.09"
[ -d "$SRC" ] || { echo "错误: tarball 顶层目录不是 e2b-infra-2026.09"; exit 1; }

echo "导出基线树 ..."
mkdir -p "$TMP/baseline"
git -C "$ARM_REPO" archive "$BASE" | tar -x -C "$TMP/baseline"

echo "对比（忽略 vendor/）..."
if diff -rq "$SRC" "$TMP/baseline" -x vendor > "$TMP/report.txt" 2>&1; then
    echo "✅ 一致：tarball 源码部分 == upstream-2026.09 基线。"
else
    echo "❌ 存在差异："
    cat "$TMP/report.txt"
    echo
    echo "若差异在 tarball 侧：tarball 被改动过，需以官方 tag + vendor 重打；"
    echo "若差异在基线侧：源码仓库基线 tag 指错了提交，需修正。"
    exit 1
fi
