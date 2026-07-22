#!/usr/bin/env bash
# 校验 e2b-infra-arm 源码仓库的"基线层"是否与官方源码包 tarball 一致。
# 必须在能拿到真实 tarball 的机器上运行（先在本仓库执行 git lfs pull）。
# 用法: scripts/verify-baseline.sh <e2b-infra-arm 仓库路径> [分支名]
# 详见 deploy-docs/08-补丁规范流程.md §5
set -euo pipefail

ARM_REPO=${1:?用法: verify-baseline.sh <e2b-infra-arm 仓库路径> [分支名]}
BRANCH=${2:-HEAD}
BUILD_REPO=$(cd "$(dirname "$0")/.." && pwd)
TARBALL="$BUILD_REPO/e2b-infra-2026.09.tar.gz"

# 预期存在于基线层、但不属于官方源码的本地扩展文件（只在基线侧，属正常）
EXTRA_OK='^(CLAUDE\.md|build\.sh|packages/shared/pkg/storage/storage_mooncake\.go)$'
# 预期与官方源码存在差异的本地修改文件（Mooncake 改动在基线层）
DIFF_OK='^(packages/orchestrator/internal/sandbox/block/chunk\.go)$'

file "$TARBALL" | grep -q 'gzip' || {
    echo "错误: $TARBALL 仍是 LFS 指针文件，请先: cd $BUILD_REPO && git lfs pull"; exit 1; }

BASE=$(git -C "$ARM_REPO" log "$BRANCH" --grep='^revert: 回退到官方' --format=%H -n1)
[ -n "$BASE" ] || { echo "错误: 找不到基线提交"; exit 1; }
echo "基线提交: $(git -C "$ARM_REPO" log -1 --oneline "$BASE")"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "解包 tarball ..."
tar -xzf "$TARBALL" -C "$TMP"
SRC="$TMP/e2b-infra-2026.09"
[ -d "$SRC" ] || { echo "错误: tarball 顶层目录不是 e2b-infra-2026.09"; exit 1; }

echo "导出基线树 ..."
mkdir -p "$TMP/baseline"
git -C "$ARM_REPO" archive "$BASE" | tar -x -C "$TMP/baseline"

echo "逐文件对比（vendor/ 仅存在于 tarball 属正常，不参与告警）..."
FAIL=0
while IFS= read -r line; do
    case "$line" in
        "Only in $SRC"*)
            rel=$(echo "$line" | sed "s|Only in $SRC/\{0,1\}||; s|: |/|" | sed 's|^/||')
            case "$rel" in vendor/*|*/vendor/*|vendor) continue ;; esac
            echo "  [仅官方源码有] $rel"; FAIL=1 ;;
        "Only in $TMP/baseline"*)
            rel=$(echo "$line" | sed "s|Only in $TMP/baseline/\{0,1\}||; s|: |/|" | sed 's|^/||')
            if echo "$rel" | grep -qE "$EXTRA_OK"; then
                echo "  [本地扩展, 正常] $rel"
            else
                echo "  [仅基线有, 需确认] $rel"; FAIL=1
            fi ;;
        Files*differ)
            rel=$(echo "$line" | sed "s|Files $SRC/||; s| and .*||")
            if echo "$rel" | grep -qE "$DIFF_OK"; then
                echo "  [本地修改, 正常] $rel"
            else
                echo "  [内容不一致, 需修正基线] $rel"; FAIL=1
            fi ;;
    esac
done < <(diff -rq "$SRC" "$TMP/baseline" 2>/dev/null || true)

echo
if [ "$FAIL" -eq 0 ]; then
    echo "✅ 基线校验通过：源码仓库基线层与官方 tarball 一致（本地扩展均在白名单内）。"
else
    echo "❌ 存在需要处理的差异。修正方法："
    echo "   cd $ARM_REPO && git rebase -i $BASE^   # 编辑基线 commit，把不一致文件改回 tarball 内容"
    echo "   （补丁层会随 rebase 自动重放；若有冲突说明该文件也被 patch 覆盖，需同步检查）"
    echo "   修完后重跑本脚本，再跑 scripts/gen-patches.sh 确认 patch 无漂移。"
    exit 1
fi
