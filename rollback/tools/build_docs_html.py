#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一本手册的 docs 目录（一组 Markdown）打成一个自包含的单文件 HTML。各手册共用，书的信息全部由参数给出。

用法:
    python3 build_docs_html.py <docs 目录> <diagrams 目录> <mermaid.min.js> <输出 html> --config <book.json> [选项...]

每本书的固定信息（书名、版本、日期、署名行、配图列表……）写在一个 JSON 配置里，用 --config 指定；
命令行选项覆盖配置里的同名项。配置的键（括号里是对应的命令行选项）：

    title           (--title)            书名，用在 <title> 与侧栏标题。必填
    version         (--version)          版本号，如 v0.1.0。必填
    date            (--date)             日期，如 2026-10-09。必填
    byline          (--byline)           作者 / 署名行。必填（保留命令行配置兼容）
    stats           (--stats)            构建日志的"N 篇 · 约 X 万字"。不给就自动算：篇数 = docs 目录里参与打包的
                                         文件数（不含 README.md），字数 = 这些文件里的汉字个数
    gh_repo         (--gh-repo)          手册以外的相对链接改写到的仓库地址，如 https://github.com/<组织>/<仓库>
    git_ref         (--git-ref)          上述链接钉住的提交；环境变量 DOC_GIT_REF 优先于配置、命令行又优先于环境变量。
                                         gh_repo 或 git_ref 为空时，这类链接降级成不可点击的文字（鼠标悬停显示仓库内路径）
    repo_docs_path  (--repo-docs-path)   docs 目录在仓库里的路径，如 rollback/docs，用来把相对链接换算成仓库内路径
    unpublished     (仅配置)             仓库内路径前缀的列表，如 ["gsd/"]：落在这些前缀下的链接即使有 git_ref 也降级成文字。
                                         用于"这部分还没有进 git_ref 那个提交"的情形，免得打出死链；入库并更新 git_ref 后去掉
    svgs            (--svg 文件=图题)     嵌进配图附录的 SVG 列表 [[文件名, 图题], ...]，文件在 <diagrams 目录> 下。
                                         可以为空或不给：那样就没有配图附录，<diagrams 目录> 也不会被读取（可写 -）
    appendix_title  (--appendix-title)   配图附录的标题，默认"附录 · 汇报配图"
    appendix_intro  (--appendix-intro)   配图附录开头的一段 HTML

docs 目录里参与打包的文件及其顺序：README.md（若有）→ NN-*.md / NNx-*.md（按文件名）→ X-*.md（大写字母开头的附录）。

产物特点：双击即看，无需任何工具；mermaid 内联渲染（由浏览器端的 mermaid 画，脚本只把 mermaid.min.js 打进文件；
图的语法另用 mdmermaid.mjs 校验）；SVG 嵌进附录；图表支持弹窗查看、滚轮缩放和拖动；手册内的跨文档链接与锚点改写成页内跳转。
相同的输入产出逐字节相同的文件（产物里不含时间戳）。
"""
import argparse
import html
import json
import os
import posixpath
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote

from markdown_it import MarkdownIt

ap = argparse.ArgumentParser(description='把一本手册的 docs 目录打成自包含的单文件 HTML')
ap.add_argument('docs_dir')
ap.add_argument('diagrams_dir', help='SVG 所在目录；没有配图附录时可写 -')
ap.add_argument('mermaid_js')
ap.add_argument('out')
ap.add_argument('--config', help='每本书的 JSON 配置，键见文件头说明')
for _opt in ('title', 'version', 'date', 'byline', 'stats', 'gh-repo', 'git-ref', 'repo-docs-path',
             'appendix-title', 'appendix-intro'):
    ap.add_argument('--' + _opt)
ap.add_argument('--svg', action='append', metavar='文件=图题', help='可重复；给了就取代配置里的 svgs')
args = ap.parse_args()

DOCS_DIR, DIAG_DIR, MERMAID_JS, OUT = args.docs_dir, args.diagrams_dir, args.mermaid_js, args.out

CFG = json.load(open(args.config, encoding='utf-8')) if args.config else {}
_unknown = set(CFG) - {'title', 'version', 'date', 'byline', 'stats', 'gh_repo', 'git_ref', 'repo_docs_path',
                       'svgs', 'appendix_title', 'appendix_intro', 'unpublished', 'contents'}
if _unknown:
    sys.exit(f'{args.config}: 不认识的键 {sorted(_unknown)}')


def opt(name, default=None, required=False):
    v = getattr(args, name)
    if v is None:
        v = CFG.get(name, default)
    if required and not v:
        sys.exit(f'缺少 {name}：在 --config 的 JSON 里给，或用 --{name.replace("_", "-")}')
    return v


DOC_TITLE = opt('title', required=True)
DOC_VERSION = opt('version', required=True)
DOC_DATE = opt('date', required=True)
DOC_AUTHOR = opt('byline', required=True)
# 手册以外的仓库链接改写到 GH_REPO，钉在 DOC_GIT_REF 这个提交上（发版时改成对应提交）
GH_REPO = opt('gh_repo', '')
DOC_GIT_REF = args.git_ref if args.git_ref is not None else os.environ.get('DOC_GIT_REF', CFG.get('git_ref', ''))
REPO_DOCS_PATH = opt('repo_docs_path', '')
UNPUBLISHED = tuple(CFG.get('unpublished', []))
SVGS = [tuple(x.split('=', 1)) for x in args.svg] if args.svg is not None else [tuple(x) for x in CFG.get('svgs', [])]
APPENDIX_TITLE = opt('appendix_title', '附录 · 汇报配图')
APPENDIX_INTRO = opt('appendix_intro', '')
CONTENTS = None
if CFG.get('contents'):
    contents_path = Path(args.config).resolve().parent / CFG['contents']
    CONTENTS = json.loads(contents_path.read_text(encoding='utf-8'))
if GH_REPO and DOC_GIT_REF and not REPO_DOCS_PATH:
    sys.exit('给了 gh_repo 与 git_ref 就必须给 repo_docs_path（docs 目录在仓库里的路径）')


def slug(t):
    """与 GitHub 一致的标题锚点规则（中文原样保留）。"""
    t = t.strip().lower()
    t = re.sub(r'<[^>]+>', '', t)
    t = re.sub(r'`([^`]*)`', r'\1', t)
    t = re.sub(r'\*\*?([^*]*)\*\*?', r'\1', t)
    t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'[^\w一-鿿\- ]', '', t)
    return t.replace(' ', '-')


def collect():
    files = sorted(os.listdir(DOCS_DIR))
    numbered = [f for f in files if re.match(r'^\d\d[a-z]?-.*\.md$', f)]
    appendix = [f for f in files if re.match(r'^[A-Z]-.*\.md$', f)]
    out = []
    if 'README.md' in files:
        out.append('README.md')
    if CONTENTS is None:
        return out + numbered + appendix
    ordered = [name for part in CONTENTS['parts'] for name in part['chapters']]
    ordered += CONTENTS['appendices']
    if len(ordered) != len(set(ordered)):
        sys.exit('目录清单存在重复篇目')
    if set(ordered) != set(numbered + appendix):
        sys.exit(f'目录清单与正文不一致：缺少 {sorted(set(numbered + appendix) - set(ordered))}；'
                 f'多出 {sorted(set(ordered) - set(numbered + appendix))}')
    return out + ordered


def doc_id(fn):
    return 'doc--' + fn[:-3]


def title_of(fn, src):
    m = re.search(r'^#\s+(.*)$', src, re.M)
    return re.sub(r'`', '', m.group(1)) if m else fn


def slug_frag(f):
    """md 里手写的锚点已经是 slug 形式，原样用；顺带兜住偶发的大小写差异。"""
    return f.lower()


md = MarkdownIt('commonmark').enable(['table', 'strikethrough'])

files = collect()
known = {f for f in files}

sections, toc = [], []
part_starts = {}
if CONTENTS:
    for index, part in enumerate(CONTENTS['parts'], 1):
        if not part['chapters']:
            sys.exit('目录分篇不能为空')
        part_starts[part['chapters'][0]] = (f'part--{index}', part['title'])
n_cjk = 0          # 正文汉字数（不含 README.md），自动算字数用
ext_links = []     # 指向手册以外的相对链接，结束时汇报

for fn in files:
    src = open(os.path.join(DOCS_DIR, fn), encoding='utf-8').read()
    did = doc_id(fn)
    if fn != 'README.md':
        n_cjk += len(re.findall(r'[\u4e00-\u9fff]', src))
    tokens = md.parse(src)

    heads = []

    def fix_link(tk):
        """链接改写：一切都变成页内锚点，解包后不可能断。"""
        href = unquote(tk.attrGet('href') or '')
        if not href:
            sys.exit(f'{fn}: 存在空链接目标')
        if href.startswith(('http://', 'https://', 'mailto:')):
            tk.attrSet('target', '_blank')
            tk.attrSet('rel', 'noopener')
            return
        if href.startswith('#'):
            tk.attrSet('href', f'#{did}--{slug_frag(href[1:])}')
            return
        if href.startswith('../diagrams') and SVGS:
            tk.attrSet('href', '#appendix-diagrams')
            return
        m = re.match(r'^([^#]+\.md)(?:#(.*))?$', href)
        # 只有同目录（不带目录分量）的 .md 才是手册里的篇目；
        # ../scripts/dev/README.md 之类只是同名，不能按文件名改成页内锚点
        if m and '/' not in m.group(1).removeprefix('./') and m.group(1).removeprefix('./') in known:
            tgt = doc_id(m.group(1).removeprefix('./'))
            tk.attrSet('href', f'#{tgt}--{slug_frag(m.group(2))}' if m.group(2) else f'#{tgt}')
            return
        # 其余相对链接指向手册以外的仓库文件（脚本、部署文档、汇报稿）：
        # 单文件里点不开，改成 GitHub 上钉在 DOC_GIT_REF 的地址
        path, _, frag = href.partition('#')
        full = posixpath.normpath(posixpath.join(REPO_DOCS_PATH or '.', path))
        dead = not (GH_REPO and DOC_GIT_REF) or full.startswith(UNPUBLISHED)
        ext_links.append((full, dead))
        if dead:
            # 没有可钉的仓库地址：降级成不可点击的文字，悬停显示路径（返回 True 让 walk 把配对的 link_close 一起改掉）
            tk.tag = 'span'
            tk.attrs = {'class': 'extref', 'title': full}
            return True
        kind = 'tree' if path.endswith('/') or not posixpath.splitext(full)[1] else 'blob'
        url = f'{GH_REPO}/{kind}/{DOC_GIT_REF}/{quote(full)}' + (f'#{frag}' if frag else '')
        tk.attrSet('href', url)
        tk.attrSet('target', '_blank')
        tk.attrSet('rel', 'noopener')

    def walk(tks):
        """inline 内容的 link_open 在 children 里，必须递归。"""
        degraded = []      # 链接不嵌套，但用栈写稳妥
        for tk in tks:
            if tk.type == 'link_open':
                degraded.append(bool(fix_link(tk)))
            elif tk.type == 'link_close' and degraded.pop():
                tk.tag = 'span'
            if tk.children:
                walk(tk.children)

    for i, tk in enumerate(tokens):
        # 标题：加上「文档前缀 + GitHub 规则锚点」的 id，并记进目录
        if tk.type == 'heading_open':
            text = tokens[i + 1].content
            anchor = f'{did}--{slug(text)}'
            tk.attrSet('id', anchor)
            lvl = int(tk.tag[1])
            if lvl == 1:
                tk.attrSet('class', 'doc-title')
            if lvl in (2, 3):
                heads.append((lvl, re.sub(r'[`*]', '', text), anchor))

    walk(tokens)

    body = md.renderer.render(tokens, md.options, {})
    # mermaid 代码块 → mermaid 容器
    body = re.sub(
        r'<pre><code class="language-mermaid">(.*?)</code></pre>',
        lambda m: '<pre class="mermaid">' + m.group(1) + '</pre>',
        body, flags=re.S)

    if fn in part_starts:
        part_id, part_title = part_starts[fn]
        sections.append(f'<div class="part-title" id="{part_id}"><h1>{html.escape(part_title)}</h1></div>')
    sections.append(f'<section class="doc" id="{did}">\n{body}\n</section>')
    toc.append((did, title_of(fn, src), heads))

# ── 附录：SVG 内联（列表为空就没有这个附录）──
if SVGS:
    svgs = []
    for name, cap in SVGS:
        p = os.path.join(DIAG_DIR, name)
        if os.path.exists(p):
            svgs.append(f'<figure><figcaption>{html.escape(cap)}</figcaption>\n'
                        + open(p, encoding='utf-8').read() + '\n</figure>')
        else:
            sys.exit(f'配图不存在: {p}')

    sections.append(f'<section class="doc" id="appendix-diagrams">\n<h1 class="doc-title">{html.escape(APPENDIX_TITLE)}</h1>\n'
                    + (f'<p>{APPENDIX_INTRO}</p>\n' if APPENDIX_INTRO else '')
                    + '\n'.join(svgs) + '\n</section>')
    toc.append(('appendix-diagrams', APPENDIX_TITLE, []))

# ── 目录 ──
def nav_doc(entry):
    did, t, heads = entry
    label = '阅读指南' if did == doc_id('README.md') else t
    number = ''
    match = re.match(r'^(\d+)\s*[．.、·]\s*(.*)$', label)
    if match:
        number, label = match.groups()
        number = number.zfill(2)
    else:
        match = re.match(r'^附录\s*([A-Z])\s*[．.·、]\s*(.*)$', label)
        if match:
            number, label = match.groups()
    number_html = f'<span class="nav-number">{html.escape(number)}</span>' if number else ''
    subheads = [(text, anchor) for lvl, text, anchor in heads if lvl == 2] if did != doc_id('README.md') else []
    result = [f'<li class="nav-item" data-doc="{did}"><div class="nav-row">'
              f'<a class="nav-link" href="#{did}">{number_html}<span>{html.escape(label)}</span></a>']
    if subheads:
        result.append(f'<button class="nav-expand" type="button" aria-expanded="false" '
                      f'aria-controls="nav--{did}" aria-label="展开或收起{html.escape(t, quote=True)}的小节">'
                      '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4"/></svg></button>')
    result.append('</div>')
    if subheads:
        result.append(f'<ul class="nav-sub" id="nav--{did}" hidden>')
        for text, anchor in subheads:
            result.append(f'<li><a href="#{anchor}">{html.escape(text)}</a></li>')
        result.append('</ul>')
    result.append('</li>')
    return ''.join(result)


nav = []
entries = {did: (did, title, heads) for did, title, heads in toc}
if CONTENTS:
    if 'README.md' in files:
        nav.append(nav_doc(entries[doc_id('README.md')]))
    groups = [(p['title'], [doc_id(f) for f in p['chapters']]) for p in CONTENTS['parts']]
    groups.append(('附录与参考', [doc_id(f) for f in CONTENTS['appendices']] +
                   (['appendix-diagrams'] if SVGS else [])))
    for title, ids in groups:
        nav.append('<li class="nav-group"><details open><summary>' + html.escape(title.replace('：', ' · ', 1)) + '</summary><ul>' +
                   ''.join(nav_doc(entries[did]) for did in ids) + '</ul></details></li>')
else:
    nav = [nav_doc(entry) for entry in toc]

mermaid_src = open(MERMAID_JS, encoding='utf-8').read()

n_body = len([f for f in files if f != 'README.md'])
n_mermaid = sum(s.count('<pre class="mermaid">') for s in sections)
if CONTENTS:
    chapter_count = sum(len(part['chapters']) for part in CONTENTS['parts'])
    appendix_count = len(CONTENTS['appendices']) + bool(SVGS)
    default_stats = f'{len(CONTENTS["parts"])} 篇 · {chapter_count} 章 · {appendix_count} 个附录 · 约 {n_cjk / 10000:.1f} 万字'
else:
    default_stats = f'{n_body} 篇 · 约 {n_cjk / 10000:.1f} 万字'
DOC_STATS = opt('stats') or default_stats

CSS = """
:root{color-scheme:light;--fg:#1a1d21;--muted:#5b6570;--bg:#fff;--bg2:#f7f8fa;--line:#e3e7ec;--accent:#2563eb;--code:#f3f5f8;
  --hover:#eef1f5;--active:#eaf0fd;--quote:#31394a;--quote-line:#c3cede;--mark:#fef08a}
:root[data-theme="dark"]{color-scheme:dark;--fg:#e2e8f0;--muted:#a3afbf;--bg:#151a23;--bg2:#1c2330;--line:#354154;--accent:#8ab4ff;--code:#202938;
  --hover:#283449;--active:#253b5e;--quote:#c5d1e2;--quote-line:#586e8c;--mark:#655019}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.75 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  -webkit-font-smoothing:antialiased}
#layout{display:flex;align-items:flex-start}
#sidebar{position:sticky;top:0;height:100dvh;flex:0 0 320px;min-width:0;
  background:var(--bg2);border-right:1px solid var(--line)}
.sidebar-brand{display:block;padding:26px 24px 22px;color:var(--fg);font-size:16px;font-weight:650;
  line-height:1.6;text-decoration:none;letter-spacing:-.02em}
.nav-content{height:calc(100dvh - 74px);overflow-y:auto;overscroll-behavior:contain;
  scrollbar-width:thin;scrollbar-color:var(--line) transparent;padding:0 16px 32px}
#directory-toggle{display:none}
#sidebar ul{list-style:none;margin:0;padding:0}
#sidebar summary{list-style:none;cursor:pointer}
#sidebar summary::-webkit-details-marker{display:none}
#sidebar .nav-group>details>summary{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:16px 8px 10px;margin-top:12px;border-top:1px solid var(--line);font-size:12px;
  line-height:1.6;font-weight:650;color:var(--muted)}
#sidebar .nav-group>details>summary::after{content:'';flex:none;width:5px;height:5px;
  border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(-45deg);margin-right:7px}
#sidebar .nav-group>details[open]>summary::after{transform:rotate(45deg)}
.nav-row{display:flex;align-items:stretch;border-left:2px solid transparent;border-radius:5px;margin:2px 0}
.nav-row:hover{background:var(--hover)}
.nav-row:has(.active){background:var(--active);border-left-color:var(--accent)}
.nav-link{display:flex;align-items:baseline;gap:8px;flex:1;min-width:0;padding:8px;color:var(--fg);
  font-size:13px;line-height:1.6;text-decoration:none}
.nav-link.active{color:var(--accent);font-weight:600}
.nav-link>span:last-child{min-width:0;text-wrap:pretty}
.nav-number{flex:0 0 20px;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.nav-link.active .nav-number{color:var(--accent)}
.nav-expand{display:grid;place-items:center;flex:0 0 30px;align-self:stretch;border:0;border-radius:4px;
  padding:0;background:transparent;color:var(--muted);cursor:pointer}
.nav-expand:hover{color:var(--fg);background:var(--hover)}
.nav-expand svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.nav-expand[aria-expanded="true"] svg{transform:rotate(90deg)}
#sidebar .nav-sub{margin:4px 0 10px 18px;padding-left:14px;border-left:1px solid var(--line)}
.nav-sub a{display:block;padding:5px 8px;color:var(--muted);font-size:12px;line-height:1.6;text-decoration:none;border-radius:4px}
.nav-sub a:hover{color:var(--fg);background:var(--hover)}
#sidebar a:focus-visible,#sidebar button:focus-visible,#sidebar summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#theme-toggle{position:fixed;top:max(12px,env(safe-area-inset-top));right:max(12px,env(safe-area-inset-right));z-index:10;
  display:grid;place-items:center;width:44px;height:44px;margin:0;padding:0;border:0;border-radius:50%;
  background:transparent;color:var(--muted);cursor:pointer;transition:background .15s ease,color .15s ease}
#theme-toggle:hover{background:var(--hover);color:var(--fg)}
#theme-toggle:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
#theme-toggle svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
#theme-toggle .theme-sun{display:none}
:root[data-theme="dark"] #theme-toggle .theme-moon{display:none}
:root[data-theme="dark"] #theme-toggle .theme-sun{display:block}
.part-title{margin-top:60px;padding:18px 24px;background:var(--bg2);border-left:4px solid var(--accent)}
.part-title h1{font-size:24px;margin:0}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
[hidden]{display:none!important}
mark{background:var(--mark);color:inherit}
main{flex:1 1 auto;min-width:0;padding:0 64px 120px;max-width:960px;margin:0 auto}
.doc{padding-top:44px;border-top:1px solid var(--line);margin-top:44px}
.doc:first-of-type{border-top:0;margin-top:0}
h1.doc-title{font-size:30px;line-height:1.35;margin:.2em 0 .6em;letter-spacing:-.01em}
h2{font-size:22px;margin:2em 0 .7em;padding-bottom:.25em;border-bottom:1px solid var(--line)}
h3{font-size:17.5px;margin:1.7em 0 .5em}
h4{font-size:15.5px;margin:1.4em 0 .4em;color:var(--muted)}
p,li{overflow-wrap:break-word}
blockquote{margin:1.2em 0;padding:.6em 1.1em;border-left:3px solid var(--quote-line);
  background:var(--bg2);color:var(--quote)}
blockquote p:first-child{margin-top:0}blockquote p:last-child{margin-bottom:0}
code{background:var(--code);padding:.12em .38em;border-radius:4px;font-size:.88em;
  font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace}
pre{background:var(--code);padding:14px 16px;border-radius:8px;overflow-x:auto;
  font-size:13px;line-height:1.6;border:1px solid var(--line)}
pre code{background:none;padding:0;font-size:inherit}
pre.mermaid{background:#fff;text-align:center;border:1px solid var(--line)}
.tw{overflow-x:auto;margin:1.2em 0}
table{border-collapse:collapse;font-size:14px;min-width:100%}
th,td{border:1px solid var(--line);padding:7px 11px;text-align:left;vertical-align:top}
th{background:var(--bg2);font-weight:600;white-space:nowrap}
a{color:var(--accent)}
hr{border:0;border-top:1px solid var(--line);margin:2.2em 0}
figure{margin:1.6em 0}
figcaption{font-size:13px;color:var(--muted);margin-bottom:.5em}
figure svg{max-width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:#fff}
#top{position:fixed;right:22px;bottom:22px;width:40px;height:40px;border-radius:50%;
  border:1px solid var(--line);background:var(--bg2);color:var(--muted);cursor:pointer;
  font-size:17px;box-shadow:0 2px 10px rgba(0,0,0,.09);display:none}
@media (max-width:1000px){
  html{scroll-padding-top:76px}
  #layout{display:block}
  #theme-toggle{top:max(6px,env(safe-area-inset-top))}
  #sidebar{position:sticky;z-index:5;top:0;height:56px;width:100%;border-right:0;border-bottom:1px solid var(--line)}
  .sidebar-brand{display:none}
  #directory-toggle{display:flex;align-items:center;gap:10px;height:55px;padding:0 72px 0 20px;
    color:var(--fg);font-size:13px;font-weight:600}
  #directory-toggle svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round}
  .nav-content{position:absolute;top:55px;left:0;width:100%;height:auto;max-height:calc(100dvh - 72px);
    padding:12px 16px 24px;background:var(--bg2);border-bottom:1px solid var(--line);box-shadow:0 12px 24px rgba(0,0,0,.08)}
  .nav-link{padding-top:11px;padding-bottom:11px}
  .nav-expand{flex-basis:44px}
  main{padding:0 20px 80px}
}
@media (prefers-reduced-motion:reduce){#theme-toggle{transition:none}}

@media print{
  :root,:root[data-theme="dark"]{color-scheme:light;--fg:#1a1d21;--muted:#5b6570;--bg:#fff;--bg2:#f7f8fa;--line:#e3e7ec;--accent:#2563eb;--code:#f3f5f8;
    --quote:#31394a;--quote-line:#c3cede;--mark:#fef08a}
  #sidebar,#top,#theme-toggle,#diagram-viewer{display:none!important}
  main{max-width:none;padding:0}
  .doc{page-break-before:always;border-top:0;margin-top:0}
  .doc:first-of-type{page-break-before:avoid}
  a{color:inherit;text-decoration:none}
  pre,table,figure,blockquote{page-break-inside:avoid}
}
"""

CSS += Path(__file__).with_name("diagram_viewer.css").read_text(encoding="utf-8")

if any(dead for _, dead in ext_links):
    CSS += ".extref{border-bottom:1px dotted var(--muted);cursor:help}\n"

THEME_INIT_JS = r"""
// 首次绘制前恢复选择；直接打开离线文件或禁用存储时仍能切换。
(function(){
  try{
    var saved=localStorage.getItem('e2b-handbook-theme');
    if(saved==='dark'||saved==='light') document.documentElement.dataset.theme=saved;
  }catch(e){}
})();
"""

JS = r"""
var themeToggle=document.getElementById('theme-toggle');
function updateThemeToggle(){
  var dark=document.documentElement.dataset.theme==='dark';
  var label='切换到'+(dark?'日间':'夜间')+'模式';
  themeToggle.title=label;
  themeToggle.setAttribute('aria-label',label);
  themeToggle.setAttribute('aria-pressed',String(dark));
}
updateThemeToggle();
themeToggle.addEventListener('click',function(){
  var theme=document.documentElement.dataset.theme==='dark'?'light':'dark';
  document.documentElement.dataset.theme=theme;
  updateThemeToggle();
  try{localStorage.setItem('e2b-handbook-theme',theme);}catch(e){}
});
mermaid.initialize({startOnLoad:true,theme:'neutral',securityLevel:'loose',
  flowchart:{useMaxWidth:true},sequence:{useMaxWidth:true}});
// 宽表格套一层横向滚动容器，正文永不横向滚动
document.querySelectorAll('main table').forEach(function(t){
  var w=document.createElement('div'); w.className='tw';
  t.parentNode.insertBefore(w,t); w.appendChild(t);
});
// 目录：章节链接与展开按钮分开，阅读时只展开当前章的小节。
var docs=[].slice.call(document.querySelectorAll('section.doc'));
var links=[].slice.call(document.querySelectorAll('#sidebar .nav-link'));
var directory=document.getElementById('directory');
var directoryToggle=document.getElementById('directory-toggle');
var narrow=window.innerWidth<=1000;
directory.open=!narrow;
function setExpanded(button,expanded){
  button.setAttribute('aria-expanded',String(expanded));
  document.getElementById(button.getAttribute('aria-controls')).hidden=!expanded;
}
function reveal(a){
  var parent=a.parentElement;
  while(parent){if(parent.tagName==='DETAILS'&&parent!==directory) parent.open=true;parent=parent.parentElement;}
  var row=a.closest('.nav-item');
  var button=row&&row.querySelector('.nav-expand');
  if(button) setExpanded(button,true);
}
document.querySelectorAll('.nav-expand').forEach(function(button){
  button.addEventListener('click',function(){setExpanded(button,button.getAttribute('aria-expanded')!=='true');});
});
var currentDoc=null;
function sync(){
  var y=window.scrollY+120,cur=0;
  for(var i=0;i<docs.length;i++){if(docs[i].offsetTop<=y) cur=i;}
  if(docs[cur]&&docs[cur].id!==currentDoc){
    currentDoc=docs[cur].id;
    links.forEach(function(a){
      var active=a.hash==='#'+currentDoc;
      a.classList.toggle('active',active);
      if(active){a.setAttribute('aria-current','location');reveal(a);}
      else{
        a.removeAttribute('aria-current');
        var button=a.closest('.nav-item').querySelector('.nav-expand');
        if(button) setExpanded(button,false);
      }
    });
    if(!narrow||directory.open){
      var activeLink=document.querySelector('.nav-link.active');
      var content=document.querySelector('.nav-content');
      if(activeLink){
        var item=activeLink.getBoundingClientRect(),box=content.getBoundingClientRect();
        if(item.top<box.top+12||item.bottom>box.bottom-12) content.scrollTop+=item.top-box.top-content.clientHeight/3;
      }
    }
  }
  document.getElementById('top').style.display=window.scrollY>600?'block':'none';
}
window.addEventListener('scroll',sync,{passive:true});
window.addEventListener('load',sync);
window.addEventListener('hashchange',sync);
window.addEventListener('resize',function(){
  var next=window.innerWidth<=1000;
  if(next!==narrow){narrow=next;directory.open=!narrow;}
});
document.getElementById('top').onclick=function(){window.scrollTo({top:0,behavior:'smooth'});};
document.querySelectorAll('#sidebar a').forEach(function(a){
  a.addEventListener('click',function(){
    reveal(a);
    if(narrow){
      directory.open=false;
      var target=document.getElementById(decodeURIComponent(a.hash.slice(1)));
      if(target){target.setAttribute('tabindex','-1');target.focus({preventScroll:true});}
    }
  });
});
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'&&narrow&&directory.open){directory.open=false;directoryToggle.focus();}
});
document.addEventListener('click',function(e){
  if(narrow&&directory.open&&!document.getElementById('sidebar').contains(e.target)) directory.open=false;
});
sync();

"""

JS += Path(__file__).with_name("diagram_viewer.js").read_text(encoding="utf-8")

doc = f"""<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(DOC_TITLE)} {DOC_VERSION}</title>
<script id="theme-init">{THEME_INIT_JS}</script>
<style>{CSS}</style>
</head>
<body>
  <button id="theme-toggle" type="button" aria-label="切换到夜间模式" title="切换到夜间模式" aria-pressed="false">
    <svg class="theme-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13Z"/></svg>
    <svg class="theme-sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.4 1.4m11.2 11.2L19 19M5 19l1.4-1.4M17.6 6.4 19 5"/></svg>
  </button>
<div id="layout">
<nav id="sidebar" aria-label="手册目录">
  <a class="sidebar-brand" href="#doc--README">{html.escape(DOC_TITLE)}</a>
  <details id="directory" open>
    <summary id="directory-toggle"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>目录</summary>
    <div class="nav-content"><ul>{''.join(nav)}</ul></div>
  </details>
</nav>
<main>
{chr(10).join(sections)}
</main>
</div>
<button id="top" title="回到顶部">↑</button>
<dialog id="diagram-viewer" tabindex="-1" aria-labelledby="diagram-title" aria-describedby="diagram-help">
  <div id="diagram-viewport" tabindex="0" role="region" aria-label="可缩放和拖动的图表"><div id="diagram-canvas"></div></div>
  <button id="diagram-close" class="diagram-ui" type="button" title="关闭（Esc）" aria-label="关闭图表预览">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
  </button>
  <p id="diagram-title" class="diagram-ui">图表预览</p>
  <div class="diagram-toolbar diagram-ui" role="group" aria-label="图表缩放工具">
    <button id="diagram-zoom-out" type="button" title="缩小" aria-label="缩小图表">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>
    </button>
    <button id="diagram-fit" type="button" title="恢复全图（0）" aria-label="恢复完整图表">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4H4v4m12-4h4v4M4 16v4h4m12-4v4h-4"/></svg>
    </button>
    <button id="diagram-zoom-in" type="button" title="放大" aria-label="放大图表">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M12 5v14"/></svg>
    </button>
  </div>
  <output id="diagram-scale" aria-label="相对完整图表的缩放比例">100%</output>
  <p id="diagram-help" class="sr-only">滚轮或双指缩放，放大后拖动查看；双击切换细节与全图。点击图外空白或按Esc关闭。加减键缩放，方向键平移，0恢复全图。</p>
</dialog>
<script>{mermaid_src}</script>
<script>{JS}</script>
</body>
</html>
"""

open(OUT, 'w', encoding='utf-8').write(doc)
print(f'{OUT}  {len(doc.encode("utf-8"))/1048576:.2f} MB  ({len(files)} 个文件'
      + (f' + 配图附录 {len(SVGS)} 张' if SVGS else '，无配图附录') + f'；规模：{DOC_STATS}；mermaid 图 {n_mermaid} 张)')
if ext_links:
    print(f'手册以外的相对链接 {len(ext_links)} 条（"链接" = 改写到 {GH_REPO or "?"} @ {DOC_GIT_REF or "?"}，"文字" = 降级成不可点击的文字）：')
    for p, dead in sorted(set(ext_links)):
        print(f'  {ext_links.count((p, dead)):3d}  {"文字" if dead else "链接"}  {p}')
