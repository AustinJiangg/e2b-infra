#!/usr/bin/env python3
"""统计进程级（gsd）的代码规模，口径与沙箱级附录 D 相同（复用 rollback/tools/code_stats.py 的词法计数）。

用法：
    python3 criu-rollback/tools/code_stats.py --kasandbox <KASandbox_0904 仓库> --infra <e2b-infra 仓库>

只读取下面固定提交里的 Git 对象，结果不随分支移动。输出按类别给出有效代码行与物理行的新增 / 删除 / 净增。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'rollback' / 'tools'))
import code_stats as cs  # noqa: E402

BASELINE = '40acf5b52afb836e909d19c51f22b5ab4f729466'      # 功能开发前基线，与附录 D 相同
GSD = 'bc3cce91354d5b898a3e295cda3abdf6e8ff0fdf'           # criu-rollback-dev，本书的代码基准
FC_KERNELS_IMPORT = '47fc79d0a'                            # 导入 fc-kernels-arm main 的提交；fc-kernels 相对它统计
INFRA = '081cbae51a661fa80fb532280859bca306c3daae'         # e2b-infra 中 gsd 测试脚本的固定提交
KERNEL_PATCHES = ('0002-overlayfs-hot-switch-checkpoint.patch', '0003-overlayfs-hot-switch-fixes.patch')


def language(path):
    name = Path(path).name
    if name.startswith('Dockerfile'):
        return 'docker'
    if name.endswith('.sh.tpl'):
        return 'bash'
    if name.endswith('.tpl'):
        return 'ini'
    if name.endswith(('.c', '.h')):
        return 'c'
    if name.endswith(('.mod', '.txt')):
        return 'text'
    return cs.language(path)


def classify(path):
    """返回 (类别, 组件)；None 表示不计。"""
    if path.endswith(('.md', 'go.sum', '.gitignore', 'LICENSE', 'go.work', 'kernel_versions.txt', 'kernel_base_tags.txt')) \
            or '/configs/' in path or '/terraform/' in path:
        return None
    if path.startswith('fc-kernels/patches/'):
        return None                                   # 内核补丁单独统计补丁里的 C 代码
    if path.endswith(('.pb.go', '.connect.go', '_pb2.py', '_pb2.pyi', '_connect.py')) or Path(path).name.startswith('buf'):
        return ('generated', '自动生成代码')
    if path.endswith('_test.go') or path.startswith('py-sdk/tests/'):
        return ('test', '单元测试')
    if path.startswith('fc-kernels/demo/'):
        return ('test', '内核验证程序')
    if path.startswith('packages/gsd/') and path.endswith('.go'):
        return ('production', 'gsd 守护进程（Go）')
    if path.startswith(('packages/orchestrator/', 'packages/shared/')) and path.endswith('.go'):
        return ('production', 'orchestrator 模板注入（Go）')
    if path.startswith('py-sdk/e2b/'):
        return ('production', 'Python SDK')
    return ('production', '协议、配置与构建支撑')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--kasandbox', type=Path, required=True)
    ap.add_argument('--infra', type=Path, required=True)
    args = ap.parse_args()
    kas, infra = str(args.kasandbox.resolve()), str(args.infra.resolve())
    rows = {}

    def add(key, sloc_add, sloc_del, phys_add, phys_del):
        r = rows.setdefault(key, [0, 0, 0, 0])
        for i, v in enumerate((sloc_add, sloc_del, phys_add, phys_del)):
            r[i] += v

    def diff(before, after, pathspec):
        out = cs.git(kas, 'diff', '--no-renames', '--numstat', before, after, '--', *pathspec).decode()
        for line in out.splitlines():
            added, _, path = line.split('\t', 2)
            key = classify(path)
            if key is None or added == '-':
                continue
            old, new = cs.read_blob(kas, before, path), cs.read_blob(kas, after, path)
            lang = language(path)
            sets = {'deleted': cs.code_lines(old, lang) if old else set(), 'added': cs.code_lines(new, lang)}
            counts = [0, 0, 0, 0]
            for side, number in cs.changed_lines(kas, before, after, path):
                i = 0 if side == 'added' else 1
                counts[2 + i] += 1
                counts[i] += number in sets[side]
            add(key, *counts)

    diff(BASELINE, GSD, [':(exclude)fc-kernels'])
    diff(FC_KERNELS_IMPORT, GSD, ['fc-kernels', ':(exclude)fc-kernels/patches'])

    # 内核补丁：补丁文件里对 .c / .h 的增删行，按 C 词法计有效行。0001 是上游带来的 balloon 补丁，不计。
    for name in KERNEL_PATCHES:
        text = cs.read_blob(kas, GSD, f'fc-kernels/patches/6.1.158/{name}')
        added, deleted, current = [], [], None
        for line in text.splitlines():
            if line.startswith('diff --git'):
                current = line.split(' b/')[-1]
            elif current and re.search(r'\.[ch]$', current):
                if line.startswith('+') and not line.startswith('+++'):
                    added.append(line[1:])
                elif line.startswith('-') and not line.startswith('---'):
                    deleted.append(line[1:])
        add(('production', 'guest 内核补丁（C）'), len(cs.code_lines('\n'.join(added) + '\n', 'c')),
            len(cs.code_lines('\n'.join(deleted) + '\n', 'c')), len(added), len(deleted))

    # e2b-infra 中的进程级测试脚本：都是新文件，按固定提交的现存全文计
    for path in subprocess.check_output(['git', '-C', infra, 'ls-tree', '-r', '--name-only', INFRA,
                                         'gsd/scripts', 'gsd/validation']).decode().split():
        if path.endswith(('.py', '.sh')):
            src = cs.read_blob(infra, INFRA, path)
            add(('test', 'e2b-infra 测试脚本'), len(cs.code_lines(src, cs.language(path))), 0, src.count('\n'), 0)

    for kind, label in (('production', '生产'), ('test', '测试'), ('generated', '生成')):
        total = [0, 0, 0, 0]
        print(f'## {label}')
        for (k, component), r in sorted(rows.items(), key=lambda kv: -(kv[1][0] - kv[1][1])):
            if k == kind:
                print(f'{component}\t有效 +{r[0]:,} / −{r[1]:,} / {r[0] - r[1]:,}\t物理行净增 {r[2] - r[3]:,}')
                total = [a + b for a, b in zip(total, r)]
        print(f'合计\t有效 +{total[0]:,} / −{total[1]:,} / {total[0] - total[1]:,}\t物理行净增 {total[2] - total[3]:,}\n')


if __name__ == '__main__':
    main()
