#!/usr/bin/env bash
# 从 e2b-infra-arm 源码仓库的分层历史导出 patch 文件，
# 按 e2b-infra.spec 中 PatchN 声明的文件名放到本仓库根目录。
# 用法: scripts/gen-patches.sh <e2b-infra-arm 仓库路径> [分支名]
# 详见 deploy-docs/08-补丁规范流程.md
set -euo pipefail

ARM_REPO=${1:?用法: gen-patches.sh <e2b-infra-arm 仓库路径> [分支名]}
BRANCH=${2:-HEAD}
BUILD_REPO=$(cd "$(dirname "$0")/.." && pwd)
SPEC="$BUILD_REPO/e2b-infra.spec"

[ -f "$SPEC" ] || { echo "错误: 找不到 $SPEC"; exit 1; }
git -C "$ARM_REPO" rev-parse --git-dir >/dev/null || exit 1

# 1. 定位基线层 commit（标题以 "revert: 回退到官方" 开头，取最近一个）
BASE=$(git -C "$ARM_REPO" log "$BRANCH" --grep='^revert: 回退到官方' --format=%H -n1)
[ -n "$BASE" ] || { echo "错误: 在 $BRANCH 上找不到基线提交（标题需以 'revert: 回退到官方' 开头）"; exit 1; }

# 2. 从基线之后按顺序收集补丁层 commit，遇到本地层（mooncake:/local: 前缀）为止
PATCH_HEAD=""
COUNT=0
while read -r rev subject; do
    case "$subject" in
        mooncake:*|local:*) break ;;
    esac
    PATCH_HEAD=$rev
    COUNT=$((COUNT + 1))
done < <(git -C "$ARM_REPO" log --reverse --format='%H %s' "$BASE..$BRANCH")
[ -n "$PATCH_HEAD" ] || { echo "错误: 基线之上没有补丁层提交"; exit 1; }

# 3. spec 中声明的 patch 文件名（按 PatchN 编号排序）
mapfile -t SPEC_NAMES < <(grep -E '^Patch[0-9]+:' "$SPEC" | sort -t: -k1.6n | awk '{print $2}')
if [ "${#SPEC_NAMES[@]}" -ne "$COUNT" ]; then
    echo "错误: spec 声明了 ${#SPEC_NAMES[@]} 个 patch，但源码仓库有 $COUNT 个补丁层提交。"
    echo "      新增/删除 patch 时需先同步修改 spec 的 PatchN 声明。"
    exit 1
fi

# 4. 导出（--zero-commit/--no-signature 保证重复生成时内容稳定）
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git -C "$ARM_REPO" format-patch --zero-commit --no-signature -o "$TMP" "$BASE..$PATCH_HEAD" >/dev/null

mapfile -t GENERATED < <(ls "$TMP"/*.patch | sort)
echo "== 导出 $COUNT 个 patch =="
for i in "${!GENERATED[@]}"; do
    dest="$BUILD_REPO/${SPEC_NAMES[$i]}"
    if [ -f "$dest" ] && cmp -s "${GENERATED[$i]}" "$dest"; then
        echo "  ${SPEC_NAMES[$i]}  (无变化)"
    else
        cp "${GENERATED[$i]}" "$dest"
        echo "  ${SPEC_NAMES[$i]}  (已更新 ← $(basename "${GENERATED[$i]}"))"
    fi
done

echo
echo "后续步骤:"
echo "  1. git -C $BUILD_REPO diff --stat        # 检查 patch 变化"
echo "  2. rpmbuild -bb e2b-infra.spec --define \"_sourcedir $BUILD_REPO\"   # 服务器上验证构建"
echo "  3. 通过后: spec 的 Release+1、补 %changelog，两个仓库分别 commit + push"
