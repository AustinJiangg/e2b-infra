#!/usr/bin/env python3
"""把一本手册（rollback 或 criu-rollback）生成为一个单文件、可离线的 HTML。

用法：
    python3 build_book.py <书目录> <输出.html>
    python3 build_book.py <书目录> --check        只检查，不留下产物

<书目录> 是 rollback/ 或 criu-rollback/，里面要有 docs/、book.json 和 book.json 指定的 contents.json。
排版由 site/build_docs_site.py 完成（复制自 md-docs-site skill，见 site/README.md）。
本脚本只做手册专用的几件事：

1. 按 contents.json 的分篇排目录，并检查清单与 docs/ 下的篇目一一对应；
2. 正文里不允许出现 mermaid 代码块（生成器不渲染它）。图一律画成 SVG 放在 docs/img/，
   画图脚本在 tools/figures/；
3. 指向手册以外的相对链接（脚本、部署文档等）在单文件里打不开：
   给了 gh_repo 和 git_ref 时改写成该分支或提交上的 GitHub 地址，否则降级为不可点击的文字；
4. 检查产物：生成器的警告、未解析的本地链接、未内嵌的图片、页内跳转目标都必须为 0。

book.json 的键：
    title           书名，必填
    contents        分篇清单文件名，必填
    gh_repo         手册以外的链接改写到的仓库地址，如 https://github.com/<组织>/<仓库>
    git_ref         上述链接指向的分支或提交，一般是 main；环境变量 DOC_GIT_REF 优先
    repo_docs_path  docs 目录在仓库里的路径，如 rollback/docs，用来换算仓库内路径
    unpublished     仓库内路径前缀的列表：落在这些前缀下的链接即使有 git_ref 也降级成文字
"""
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

TOOLS = Path(__file__).resolve().parent
KEYS = {'title', 'contents', 'gh_repo', 'git_ref', 'repo_docs_path', 'unpublished'}


def fail(msg):
    sys.exit(f'build_book: {msg}')


if len(sys.argv) != 3:
    fail('用法：build_book.py <书目录> <输出.html | --check>')
book = Path(sys.argv[1]).resolve()
check_only = sys.argv[2] == '--check'
docs = book / 'docs'
cfg_path = book / 'book.json'
if not docs.is_dir() or not cfg_path.is_file():
    fail(f'{book} 下要有 docs/ 和 book.json')
cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
if set(cfg) - KEYS:
    fail(f'{cfg_path}: 不认识的键 {sorted(set(cfg) - KEYS)}')
for k in ('title', 'contents'):
    if not cfg.get(k):
        fail(f'{cfg_path}: 缺少 {k}')
gh_repo = cfg.get('gh_repo', '')
git_ref = os.environ.get('DOC_GIT_REF', cfg.get('git_ref', ''))
repo_docs_path = cfg.get('repo_docs_path', '')
unpublished = tuple(cfg.get('unpublished', []))
if gh_repo and git_ref and not repo_docs_path:
    fail('给了 gh_repo 与 git_ref 就必须给 repo_docs_path')

# ---------------------------------------------------------------- 篇目与分篇
contents = json.loads((book / cfg['contents']).read_text(encoding='utf-8'))
files = sorted(p.name for p in docs.glob('*.md'))
chapters = [f for f in files if re.match(r'\d\d-.*\.md$', f)]
appendices = [f for f in files if re.match(r'[A-Z]-.*\.md$', f)]
ordered = [c for part in contents['parts'] for c in part['chapters']] + contents['appendices']
if len(ordered) != len(set(ordered)):
    fail('分篇清单里有重复篇目')
if set(ordered) != set(chapters + appendices):
    fail(f'分篇清单与 docs/ 不一致：缺少 {sorted(set(chapters + appendices) - set(ordered))}；'
         f'多出 {sorted(set(ordered) - set(chapters + appendices))}')
if any(not part['chapters'] for part in contents['parts']):
    fail('分篇不能为空')
groups = ([['', ['README.md']]] if 'README.md' in files else [])
groups += [[part['title'], part['chapters']] for part in contents['parts']]
if contents['appendices']:
    groups.append(['附录', contents['appendices']])

# ---------------------------------------------------------------- 正文预处理
FENCE = re.compile(r'^([ \t]*)(`{3,}|~{3,})[^\n]*\n.*?^\1?[ \t]*\2[ \t]*$', re.M | re.S)
INLINE = re.compile(r'(`+)(?!`).+?(?<!`)\1(?!`)', re.S)
LINK = re.compile(r'(?<!!)\[((?:[^\[\]]|\[[^\]]*\])+)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
known = set(files)
ext_links = []


def outside_code(s, fn):
    """只对代码块以外的文字调用 fn。"""
    out, pos = [], 0
    for m in FENCE.finditer(s):
        out.append(fn(s[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(s[pos:]))
    return ''.join(out)


def links_outside_inline_code(s):
    """改写链接，但跳过整个落在行内代码里的写法；链接文字本身可以是行内代码。"""
    spans = [m.span() for m in INLINE.finditer(s)]
    def sub(m):
        if any(a <= m.start() < b for a, b in spans):
            return m.group(0)
        return rewrite(m)
    return LINK.sub(sub, s)


def rewrite(m):
    text, href = m.group(1), unquote(m.group(2))
    if re.match(r'[a-z][a-z0-9+.-]*:|#|//', href, re.I):
        return m.group(0)
    path, _, frag = href.partition('#')
    p = path.removeprefix('./')
    if '/' not in p and p in known or p.startswith('img/'):
        return m.group(0)       # 手册里的篇目和图，交给生成器
    full = posixpath.normpath(posixpath.join(repo_docs_path or '.', path))
    dead = not (gh_repo and git_ref) or full.startswith(unpublished)
    ext_links.append((full, dead))
    if dead:
        return text if text.startswith('`') or '/' not in text else f'`{text}`'
    kind = 'tree' if path.endswith('/') or not posixpath.splitext(full)[1] else 'blob'
    return f'[{text}]({gh_repo}/{kind}/{git_ref}/{quote(full)}{"#" + frag if frag else ""})'


stage_root = Path(tempfile.mkdtemp(prefix='build_book-'))
try:
    stage = stage_root / 'docs'
    shutil.copytree(docs, stage)
    for fn in ['README.md'] * ('README.md' in files) + ordered:
        p = stage / fn
        src = p.read_text(encoding='utf-8')
        n = len(re.findall(r'^[ \t]*(?:`{3,}|~{3,})[ \t]*mermaid\b', src, re.M))
        if n:
            fail(f'{fn}: 有 {n} 个 mermaid 代码块。请画成 SVG 放进 docs/img/，用 ![说明](img/图.svg) 引用')
        p.write_text(outside_code(src, links_outside_inline_code), encoding='utf-8')
    groups_path = stage_root / 'groups.json'
    groups_path.write_text(json.dumps(groups, ensure_ascii=False), encoding='utf-8')

    # ------------------------------------------------------------ 生成
    out = stage_root / 'out.html' if check_only else Path(sys.argv[2]).resolve()
    r = subprocess.run([sys.executable, str(TOOLS / 'site' / 'build_docs_site.py'),
                        '--docs', str(stage), '--out', str(out), '--title', cfg['title'],
                        '--groups', str(groups_path)],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode:
        fail(f'生成器退出码 {r.returncode}')

    # ------------------------------------------------------------ 检查产物
    problems = []
    for key, label in (('unresolved local links', '未解析的本地链接'), ('raw img refs', '未内嵌的图片'),
                       ('warnings', '生成器警告')):
        m = re.search(rf'^{key}:.*?(\d+)$', r.stdout, re.M)
        if not m or m.group(1) != '0':
            problems.append(f'{label}：{m.group(1) if m else "未输出"}')
    page = out.read_text(encoding='utf-8')
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    bad = sorted({h for h in re.findall(r'href="#(d[^"$]+)"', page)
                  if h not in ids and not ('-' not in h and 'v' + h[1:] in ids)})
    if bad:
        problems.append(f'页内跳转目标不存在 {len(bad)} 个：{bad[:10]}')
    pages = len(re.findall(r'<article id="v', page))
    if pages != len(ordered) + ('README.md' in files):
        problems.append(f'篇数 {pages}，应为 {len(ordered) + ("README.md" in files)}')
    if problems:
        if not check_only:
            out.unlink()
        fail('；'.join(problems))
    n_ext = len(ext_links)
    n_dead = sum(d for _, d in ext_links)
    print(f'手册以外的链接 {n_ext} 处：改写为 GitHub 地址 {n_ext - n_dead}，降级为文字 {n_dead}')
    print('检查通过' + ('' if check_only else f'：{out}'))
finally:
    shutil.rmtree(stage_root, ignore_errors=True)
