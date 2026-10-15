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
    byline          (--byline)           作者 / 署名行，原样印在侧栏版本号之后。必填
    stats           (--stats)            侧栏的"N 篇 · 约 X 万字"。不给就自动算：篇数 = docs 目录里参与打包的
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
图的语法另用 mdmermaid.mjs 校验）；SVG 嵌进附录；手册内的跨文档链接与锚点改写成页内跳转，因此永远不会断。
相同的输入产出逐字节相同的文件（产物里不含时间戳）。
"""
import argparse
import html
import json
import os
import posixpath
import re
import sys
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
                       'svgs', 'appendix_title', 'appendix_intro', 'unpublished'}
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
    return out + numbered + appendix


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
nav = []
for did, t, heads in toc:
    nav.append(f'<li class="nav-doc"><a href="#{did}">{html.escape(t)}</a>')
    if heads:
        nav.append('<ul class="nav-sub">')
        for lvl, text, anchor in heads:
            if lvl == 2:
                nav.append(f'<li><a href="#{anchor}">{html.escape(text)}</a></li>')
        nav.append('</ul>')
    nav.append('</li>')

mermaid_src = open(MERMAID_JS, encoding='utf-8').read()

n_body = len([f for f in files if f != 'README.md'])
n_mermaid = sum(s.count('<pre class="mermaid">') for s in sections)
DOC_STATS = opt('stats') or f'{n_body} 篇 · 约 {n_cjk / 10000:.1f} 万字'

CSS = """
:root{--fg:#1a1d21;--muted:#5b6570;--bg:#fff;--bg2:#f7f8fa;--line:#e3e7ec;--accent:#2563eb;--code:#f3f5f8}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.75 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  -webkit-font-smoothing:antialiased}
#layout{display:flex;align-items:flex-start}
#sidebar{position:sticky;top:0;height:100vh;overflow-y:auto;flex:0 0 300px;
  background:var(--bg2);border-right:1px solid var(--line);padding:20px 0 60px}
#sidebar h2{margin:0 20px 4px;font-size:15px;letter-spacing:.02em}
#sidebar .sub{margin:0 20px 14px;font-size:12px;color:var(--muted)}
#filter{width:calc(100% - 40px);margin:0 20px 12px;padding:7px 10px;font-size:13px;
  border:1px solid var(--line);border-radius:6px;background:#fff;font-family:inherit}
#sidebar ul{list-style:none;margin:0;padding:0}
#sidebar .nav-doc>a{display:block;padding:5px 20px;color:var(--fg);text-decoration:none;
  font-size:13.5px;font-weight:600;border-left:3px solid transparent}
#sidebar .nav-doc>a:hover{background:#eef1f5}
#sidebar .nav-doc>a.active{border-left-color:var(--accent);color:var(--accent);background:#eaf0fd}
#sidebar .nav-sub{display:none;padding-bottom:6px}
#sidebar .nav-doc.open .nav-sub{display:block}
#sidebar .nav-sub a{display:block;padding:2px 20px 2px 34px;color:var(--muted);
  text-decoration:none;font-size:12.5px}
#sidebar .nav-sub a:hover{color:var(--accent)}
main{flex:1 1 auto;min-width:0;padding:0 56px 120px;max-width:960px;margin:0 auto}
.doc{padding-top:44px;border-top:1px solid var(--line);margin-top:44px}
.doc:first-of-type{border-top:0;margin-top:0}
h1.doc-title{font-size:30px;line-height:1.35;margin:.2em 0 .6em;letter-spacing:-.01em}
h2{font-size:22px;margin:2em 0 .7em;padding-bottom:.25em;border-bottom:1px solid var(--line)}
h3{font-size:17.5px;margin:1.7em 0 .5em}
h4{font-size:15.5px;margin:1.4em 0 .4em;color:var(--muted)}
p,li{overflow-wrap:break-word}
blockquote{margin:1.2em 0;padding:.6em 1.1em;border-left:3px solid #c3cede;
  background:var(--bg2);color:#31394a}
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
  border:1px solid var(--line);background:#fff;color:var(--muted);cursor:pointer;
  font-size:17px;box-shadow:0 2px 10px rgba(0,0,0,.09);display:none}
@media (max-width:1000px){
  #layout{display:block}
  #sidebar{position:static;height:auto;width:100%;flex:none;border-right:0;
    border-bottom:1px solid var(--line);max-height:44vh}
  main{padding:0 20px 80px}
}

@media print{
  #sidebar,#top{display:none}
  main{max-width:none;padding:0}
  .doc{page-break-before:always;border-top:0;margin-top:0}
  .doc:first-of-type{page-break-before:avoid}
  a{color:inherit;text-decoration:none}
  pre,table,figure,blockquote{page-break-inside:avoid}
}
"""

if any(dead for _, dead in ext_links):
    CSS += ".extref{border-bottom:1px dotted var(--muted);cursor:help}\n"

JS = r"""
mermaid.initialize({startOnLoad:true,theme:'neutral',securityLevel:'loose',
  flowchart:{useMaxWidth:true},sequence:{useMaxWidth:true}});
// 宽表格套一层横向滚动容器，正文永不横向滚动
document.querySelectorAll('main table').forEach(function(t){
  var w=document.createElement('div'); w.className='tw';
  t.parentNode.insertBefore(w,t); w.appendChild(t);
});
// 目录：当前章节高亮 + 展开
var docs=[].slice.call(document.querySelectorAll('section.doc'));
var links=[].slice.call(document.querySelectorAll('#sidebar .nav-doc>a'));
function sync(){
  var y=window.scrollY+120,cur=0;
  for(var i=0;i<docs.length;i++){ if(docs[i].offsetTop<=y) cur=i; }
  links.forEach(function(a,i){
    a.classList.toggle('active',i===cur);
    a.parentNode.classList.toggle('open',i===cur);
  });
  document.getElementById('top').style.display=window.scrollY>600?'block':'none';
}
window.addEventListener('scroll',sync,{passive:true});
window.addEventListener('load',sync);
document.getElementById('top').onclick=function(){window.scrollTo({top:0,behavior:'smooth'});};
// 目录过滤
document.getElementById('filter').addEventListener('input',function(e){
  var q=e.target.value.trim().toLowerCase();
  document.querySelectorAll('#sidebar .nav-doc').forEach(function(li){
    li.style.display = !q || li.textContent.toLowerCase().indexOf(q)>=0 ? '' : 'none';
  });
});
"""

doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(DOC_TITLE)} {DOC_VERSION}</title>
<style>{CSS}</style>
</head>
<body>
<div id="layout">
<nav id="sidebar">
  <h2>{html.escape(DOC_TITLE)}</h2>
  <div class="sub">{DOC_VERSION} · {DOC_DATE} · {DOC_AUTHOR}<br>
  {DOC_STATS} · 单文件离线版</div>
  <input id="filter" type="search" placeholder="过滤篇目…" autocomplete="off">
  <ul>{''.join(nav)}</ul>
</nav>
<main>
{chr(10).join(sections)}
</main>
</div>
<button id="top" title="回到顶部">↑</button>
<script>{mermaid_src}</script>
<script>{JS}</script>
</body>
</html>
"""

open(OUT, 'w', encoding='utf-8').write(doc)
print(f'{OUT}  {len(doc.encode("utf-8"))/1048576:.2f} MB  ({len(files)} 个文件'
      + (f' + 配图附录 {len(SVGS)} 张' if SVGS else '，无配图附录') + f'；侧栏：{DOC_STATS}；mermaid 图 {n_mermaid} 张)')
if ext_links:
    print(f'手册以外的相对链接 {len(ext_links)} 条（"链接" = 改写到 {GH_REPO or "?"} @ {DOC_GIT_REF or "?"}，"文字" = 降级成不可点击的文字）：')
    for p, dead in sorted(set(ext_links)):
        print(f'  {ext_links.count((p, dead)):3d}  {"文字" if dead else "链接"}  {p}')
