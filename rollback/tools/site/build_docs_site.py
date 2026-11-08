"""build_docs_site：把一个目录下的 markdown 合成一个单文件、可离线的 HTML 文档站。

左侧分组目录、右侧正文；图片以 data URI 内嵌；跨篇链接和页内锚点改成站内跳转；
每篇末尾有上一篇/下一篇；深浅色主题；手机可读。文件名不需要编号，也支持子目录。

用法：
  python3 build_docs_site.py --docs docs --out site.html [--title "站点标题"] [--subtitle "副标题"]
      [--groups groups.json] [--accent "#13826a"]
groups.json（可选）：[["", ["00"]], ["原理", ["01", "guide/setup.md"]], ...]，
每项写两位编号或相对 --docs 的路径。不给时按顶层子目录分组。
依赖：pip install markdown
"""
import argparse, base64, html, json, os, posixpath, re, sys
from urllib.parse import unquote

try:
    import markdown
    from markdown.extensions.toc import TocExtension
except ImportError:
    sys.exit("缺少 markdown 包，请运行：pip install markdown")
import theme_toggle

ap = argparse.ArgumentParser()
ap.add_argument("--docs", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--title", help="站点标题，默认用第一篇的标题")
ap.add_argument("--subtitle", default="")
ap.add_argument("--groups")
ap.add_argument("--accent", default="#13826a", help="浅色主题强调色")
ap.add_argument("--accent-dark", default="#4cc4a0", help="深色主题强调色")
A = ap.parse_args()
D = A.docs
OUT = A.out
if not os.path.isdir(D):
    sys.exit(f"目录不存在：{D}")

MD_EXT = (".md", ".markdown")
SKIP_DIRS = {"node_modules", "__pycache__", "venv"}
MIME = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif"}
warnings = []


def natural(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def sort_key(rel):
    d, name = posixpath.split(rel)
    stem = name.rsplit(".", 1)[0].lower()
    return ([natural(p) for p in d.split("/")] if d else [], stem not in ("index", "readme"), natural(name))


def find_docs():
    found = []
    for base, dirs, names in os.walk(D):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS]
        for n in names:
            if n.lower().endswith(MD_EXT):
                found.append(os.path.relpath(os.path.join(base, n), D).replace(os.sep, "/"))
    return sorted(found, key=sort_key)


def split_frontmatter(src):
    """去掉文件头的 YAML，返回 (正文, title 字段)。"""
    m = re.match(r"---\n(.*?)\n---[ \t]*(?:\n|$)", src, re.S)
    if not m:
        return src, None
    t = re.search(r"^title:[ \t]*(.+?)[ \t]*$", m.group(1), re.M)
    return src[m.end():], t.group(1).strip("\"'") if t else None


CODE = re.compile(r"(^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\2[ \t]*$|`[^`\n]+`)", re.M | re.S)


def outside_code(s, fn):
    """只对代码块和行内代码以外的文字做替换。"""
    out, pos = [], 0
    for m in CODE.finditer(s):
        out += [fn(s[pos:m.start()]), m.group(0)]
        pos = m.end()
    return "".join(out + [fn(s[pos:])])


LIST_ITEM = re.compile(r"( *)(?:[-*+]|\d+[.)]) +\S")


def list_block(block):
    """把一段列表的缩进换算成 4 格，列表里的 fence 换成缩进代码块（fenced_code 不认带缩进的 fence）。"""
    indent = lambda b: len(b) - len(b.lstrip(" "))
    closing, in_code, marks = None, [], []
    for b in block:
        fence = re.match(r"`{3,}|~{3,}", b.lstrip(" "))
        marks.append(bool(closing) or bool(fence))
        if closing:
            closing = None if b.lstrip(" ").startswith(closing) else closing
        elif fence:
            closing = fence.group(0)
            in_code.append(b)
    used = [indent(b) for b, m in zip(block, marks) if b.strip() and not m] + [indent(b) for b in in_code]
    step = min((n for n in used if n), default=4)
    scale = (lambda n: 4 * (n // step)) if step in (2, 3) else (lambda n: n)

    out, base, skip, closing, after_code = [], 0, 0, None, False
    for b, m in zip(block, marks):
        if not m:
            if LIST_ITEM.match(b) and indent(b) and out and out[-1].strip() and not LIST_ITEM.match(out[-1]):
                out.append("")   # 段落后紧跟的子列表
            after_code = after_code and not b.strip()
            out.append(" " * scale(indent(b)) + b.lstrip(" "))
        elif closing is None:
            closing = re.match(r"`{3,}|~{3,}", b.lstrip(" ")).group(0)
            skip = indent(b)
            base = (max(scale(skip), 4) + 3) // 4 * 4 + 4
            out += [" " * (base - 4) + "<!-- -->", ""] if after_code else [""]   # 否则相邻代码块会合成一个
        elif b.lstrip(" ").startswith(closing):
            closing, after_code = None, True
            out.append("")
        else:
            out.append(" " * base + b[min(skip, indent(b)):] if b.strip() else "")
    return out


def normalize_lists(src):
    """python-markdown 要求嵌套缩进 4 格、列表前有空行，这里把 GitHub 写法转过来。"""
    lines, out, i, fence = src.split("\n"), [], 0, None
    while i < len(lines):
        line = lines[i]
        f = re.match(r"(`{3,}|~{3,})", line)
        if fence:
            fence = None if line.startswith(fence) else fence
        elif f:
            fence = f.group(1)
        elif LIST_ITEM.match(line) and not line.startswith(" "):
            j = i + 1
            while j < len(lines):
                cur = lines[j]
                if cur.strip() and not cur.startswith((" ", "\t")) and not LIST_ITEM.match(cur) \
                        and (not lines[j - 1].strip() or re.match(r"`{3}|~{3}|#|\|", cur)):
                    break
                j += 1
            if out and out[-1].strip() and not LIST_ITEM.match(out[-1]):
                out.append("")
            out += list_block(lines[i:j])
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def slug(text):
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def local_path(doc_rel, target):
    """把文档里的相对地址转成相对 --docs 的路径；外部地址返回 None。"""
    if re.match(r"[a-z][a-z0-9+.-]*:|//", target, re.I):
        return None
    t = unquote(re.split(r"[?#]", target, 1)[0])
    return t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(doc_rel), t))


def data_uri(doc_rel, target):
    """返回本地图片的 data URI；外部地址返回 None，读不到时返回空字符串。"""
    rel = local_path(doc_rel, target)
    if rel is None:
        return None
    mime = MIME.get(os.path.splitext(rel)[1].lower())
    p = os.path.join(D, rel)
    if not mime or not os.path.isfile(p):
        warnings.append(f"{doc_rel}：图片{'格式不支持' if os.path.isfile(p) else '不存在'}：{target}")
        return ""
    with open(p, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"


# ------------------------------------------------------------------ 收集文档
docs = {}
paths = find_docs()
if not paths:
    sys.exit(f"{D} 下没有 markdown 文件")
prefixes = [m.group(1) if (m := re.match(r"(\d\d|[A-Z])[-_ ]", posixpath.basename(r))) else None for r in paths]
for i, (rel, num) in enumerate(zip(paths, prefixes)):
    with open(os.path.join(D, rel), encoding="utf-8-sig", errors="replace") as f:
        src = f.read().replace("\r\n", "\n")
    src, fm_title = split_frontmatter(src)
    m = re.search(r"^#\s+(.+?)\s*#*$", CODE.sub("", src), re.M)
    stem = re.sub(r"^\d+[-_ ]", "", posixpath.basename(rel).rsplit(".", 1)[0])
    title = m.group(1) if m else fm_title or re.sub(r"[-_]+", " ", stem)
    if not m:
        src = f"# {title}\n\n{src}"
    unique = num is not None and prefixes.count(num) == 1
    label = num if unique else ""
    # 标题以「NN · 」「附录 A · 」开头且编号与文件名一致时，编号只在篇首小标签里显示一次，
    # 正文 H1 和目录里去掉这个前缀，避免同一个编号出现两遍。
    docnum, nav_title = label, title
    if label and (t := re.match(rf"((?:附录\s*)?{re.escape(label)})\s*·\s*(.+)$", title)):
        docnum, nav_title = t.group(1), t.group(2)
        if m:
            src = src.replace(m.group(0), f"# {nav_title}", 1)
    nmer = len(re.findall(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*mermaid\b", src, re.M))
    if nmer:
        warnings.append(f"{rel}：{nmer} 个 mermaid 代码块不会渲染成图，只显示源码；先转成 SVG 再用 ![](图.svg) 引用")
    docs[num if unique else f"p{i}"] = dict(rel=rel, src=src, title=title, label=label,
                                            docnum=docnum, nav_title=nav_title)

rel2key = {v["rel"]: k for k, v in docs.items()}


def rendered_doc(key, d):
    rel = d["rel"]

    def img_sub(m):
        alt, target = m.group(1), m.group(2) or m.group(3)
        uri = data_uri(rel, target)
        if uri is None:
            return m.group(0)
        if not uri:
            return f"**（缺图：{html.escape(target)}）**"
        line_start = m.string.rfind("\n", 0, m.start()) + 1
        line_end = (m.string + "\n").find("\n", m.end())
        attrs = f'alt="{html.escape(alt)}" src="{uri}"'
        if m.string[line_start:m.start()].strip() or m.string[m.end():line_end].strip():
            return f'<img class="inline" {attrs}>'
        stem = posixpath.basename(unquote(target)).rsplit(".", 1)[0]
        n = re.match(r"(\d+)-([^-]+)(?:-|$)", stem)
        cap = f"图 {html.escape(n.group(1))}-{html.escape(n.group(2))}　{html.escape(alt)}" if n else html.escape(alt)
        kind = "paper" if target.lower().endswith(".svg") else "paper raster"
        figcap = f"<figcaption>{cap}</figcaption>" if cap else ""
        return f'\n\n<figure class="{kind}"><img loading="lazy" {attrs}>{figcap}</figure>\n\n'

    def html_img_sub(m):
        uri = data_uri(rel, html.unescape(m.group(2)))
        return m.group(1) + uri + m.group(3) if uri else m.group(0)

    def link_sub(m):
        text, target, frag = m.group(1), m.group(2), m.group(3) or ""
        k = rel2key.get(local_path(rel, target) or "")
        if k is None:
            return m.group(0)
        return f"[{text}](#d{k}-{unquote(frag[1:]).lower()})" if frag else f"[{text}](#d{k})"

    def text_sub(s):
        s = re.sub(r'!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+"[^"]*")?\s*\)', img_sub, s)
        s = re.sub(r'(<img\b[^>]*?\bsrc=["\'])([^"\']+)(["\'])', html_img_sub, s)
        s = re.sub(r"\]\(#([^)\s]+)\)", lambda m: f"](#d{key}-{unquote(m.group(1)).lower()})", s)
        s = re.sub(r"(?<!!)\[([^\]]+)\]\(\s*([^)\s#]+\.(?:md|markdown))(#[^)\s]*)?\s*\)", link_sub, s)
        return re.sub(r"~~([^~\n]+)~~", r"<del>\1</del>", s)

    toc = TocExtension(slugify=lambda v, sep: f"d{key}-{slug(v)}")
    h = markdown.markdown(outside_code(normalize_lists(d["src"]), text_sub),
                          extensions=["tables", "fenced_code", "sane_lists", toc])
    h = re.sub(r"<table>", '<div class="tw"><table>', h).replace("</table>", "</table></div>")
    return re.sub(r"<li>(<p>)?\[([ xX])\] ",
                  lambda m: f'<li class="task">{m.group(1) or ""}<input type="checkbox" disabled'
                            f'{"" if m.group(2) == " " else " checked"}> ', h)


nav_titles = {}
sections = []
for key, d in docs.items():
    nav_titles[key] = d["nav_title"]
    num_tag = f'<div class="docnum">{html.escape(d["docnum"])}</div>' if d["docnum"] else ""
    sections.append(f'<article id="v{key}" class="doc" hidden>{num_tag}{rendered_doc(key, d)}'
                    f'<nav class="pager" data-n="{key}"></nav></article>')

# ------------------------------------------------------------------ 目录分组
if A.groups:
    with open(A.groups, encoding="utf-8") as f:
        GROUPS = [(g, [rel2key.get(n, n) for n in ns]) for g, ns in json.load(f)]
    listed = {n for _, ns in GROUPS for n in ns}
    for n in [n for _, ns in GROUPS for n in ns if n not in docs]:
        warnings.append(f"groups 里的 {n} 没有对应文档")
    rest = [k for k in docs if k not in listed]
    if rest:
        GROUPS.append(("其他", rest))
else:
    GROUPS = {}
    for k, v in docs.items():
        GROUPS.setdefault(v["rel"].split("/")[0] if "/" in v["rel"] else "", []).append(k)
    GROUPS = sorted(GROUPS.items(), key=lambda kv: (kv[0] != "", natural(kv[0])))
nav = []
for g, keys in GROUPS:
    if g:
        nav.append(f'<div class="ng">{html.escape(g)}</div>')
    for n in keys:
        if n in docs:
            tag = f'<span class="nn">{docs[n]["label"]}</span>' if docs[n]["label"] else ""
            nav.append(f'<a href="#d{n}" data-n="{n}">{tag}{html.escape(nav_titles[n])}</a>')

order = [n for _, ns in GROUPS for n in ns if n in docs]
site_title = A.title or nav_titles[order[0]]
labels = {k: v["label"] for k, v in docs.items()}


def js_json(v):
    return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")


page = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{html.escape(site_title)}</title>
<meta name="claude:viewport-fit-sides" content="cover">
{theme_toggle.head_script("docs-theme")}
<style>
:root{{--bg:#f4f6f5;--surface:#fff;--surface-2:#edf1ef;--ink:#1b2220;--ink-2:#47524e;--muted:#75817c;--rule:#d9dfdc;--accent:{A.accent};--accent-soft:#d6eee6;--code:#eef2f0;--paper:#fff;
--f:"PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",system-ui,sans-serif;--m:"SF Mono","JetBrains Mono",Menlo,Consolas,monospace}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#111615;--surface:#181e1c;--surface-2:#1f2725;--ink:#e7edea;--ink-2:#b7c1bd;--muted:#87928d;--rule:#2c3532;--accent:{A.accent_dark};--accent-soft:#173229;--code:#1f2725;--paper:#f3f4f2;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#111615;--surface:#181e1c;--surface-2:#1f2725;--ink:#e7edea;--ink-2:#b7c1bd;--muted:#87928d;--rule:#2c3532;--accent:{A.accent_dark};--accent-soft:#173229;--code:#1f2725;--paper:#f3f4f2;color-scheme:dark}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--ink);font:15.5px/1.75 var(--f);margin:0}}
.wrap{{display:grid;grid-template-columns:280px minmax(0,1fr);min-height:100vh}}
aside{{position:sticky;top:0;height:100vh;overflow:auto;background:var(--surface);border-right:1px solid var(--rule);padding:20px 12px 40px;padding-left:max(12px,env(safe-area-inset-left,0px))}}
aside .brand{{font-weight:600;font-size:16px;padding:0 10px 4px}}
aside .sub{{font-size:12.5px;color:var(--muted);padding:0 10px 14px}}
aside .ng{{font-size:12px;letter-spacing:.06em;color:var(--muted);margin:16px 10px 4px}}
aside a{{display:flex;gap:8px;padding:5px 10px;border-radius:7px;color:var(--ink-2);text-decoration:none;font-size:14px;line-height:1.45}}
aside a:hover{{background:var(--surface-2)}}
aside a.on{{background:var(--accent-soft);color:var(--ink);font-weight:600}}
.nn{{font-family:var(--m);color:var(--muted);font-size:12.5px;min-width:20px;padding-top:1px}}
main{{min-width:0;padding-block:28px 80px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}}
.doc{{max-width:960px;margin:0 auto}}
.doc > p,.doc > ul,.doc > ol,.doc > blockquote,.doc > h1,.doc > h2,.doc > h3,.doc > h4{{max-width:46em}}
.docnum{{font-family:var(--m);color:var(--accent);font-size:13px;letter-spacing:.1em}}
h1{{font-size:28px;line-height:1.3;margin:4px 0 18px;text-wrap:balance}}
h2{{font-size:21px;margin:40px 0 12px;padding-top:14px;border-top:1px solid var(--rule)}}
h3{{font-size:17px;margin:28px 0 8px}}
a{{color:var(--accent)}}
code{{font-family:var(--m);font-size:.88em;background:var(--code);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}}
pre{{background:var(--code);padding:14px 16px;border-radius:8px;overflow-x:auto;line-height:1.55}}
pre code{{background:none;padding:0;overflow-wrap:normal}}
blockquote{{margin:16px 0;padding:6px 16px;border-left:3px solid var(--accent);color:var(--ink-2);background:var(--surface)}}
.tw{{overflow-x:auto;margin:14px 0}}
table{{border-collapse:collapse;font-size:14px;min-width:100%}}
th,td{{border-bottom:1px solid var(--rule);padding:7px 10px;text-align:left;vertical-align:top}}
th{{background:var(--surface-2);font-weight:600}}
figure.paper{{margin:22px 0;background:var(--paper);border:1px solid var(--rule);border-radius:10px;padding:10px;overflow-x:auto}}
figure.paper img{{display:block;width:100%;height:auto;min-width:720px}}
figure.raster img{{width:auto;max-width:100%;min-width:0;margin:0 auto}}
img.inline{{max-width:100%;height:auto;vertical-align:middle}}
li.task{{list-style:none}}
li.task input{{margin:0 6px 0 -1.2em}}
figcaption{{font-size:13px;color:#5f6b66;text-align:center;padding-top:6px}}
.pager{{display:flex;justify-content:space-between;gap:12px;margin-top:56px;padding-top:18px;border-top:1px solid var(--rule)}}
.pager a{{text-decoration:none;padding:10px 14px;border:1px solid var(--rule);border-radius:8px;background:var(--surface);max-width:48%;font-size:14px}}
.pager a small{{display:block;color:var(--muted);font-size:12px}}
.top{{display:none}}
.brandrow{{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;padding-right:10px}}
@media (max-width:860px){{
 .wrap{{display:block}}
 aside{{position:static;height:auto;border-right:0;border-bottom:1px solid var(--rule);padding-right:max(12px,env(safe-area-inset-right,0px))}}
 aside.closed .navlist{{display:none}}
 .top{{display:inline-block;margin:0 10px 4px;font:13px var(--f);background:var(--surface-2);color:var(--ink);border:1px solid var(--rule);border-radius:6px;padding:4px 10px}}
 figure.paper img{{min-width:640px}}
}}
{theme_toggle.CSS}</style>
</head>
<body>
<div class="wrap">
<aside id="side" class="closed">
<div class="brandrow"><div class="brand">{html.escape(site_title)}</div>{theme_toggle.BUTTON}</div>
<div class="sub">{html.escape(A.subtitle)}{" · " if A.subtitle else ""}{len(docs)} 篇</div>
<button class="top" id="tog" type="button">目录</button>
<div class="navlist">{"".join(nav)}</div>
</aside>
<main>{"".join(sections)}</main>
</div>
<script>
const ORDER={js_json(order)}, T={js_json(nav_titles)}, N={js_json(labels)};
function show(){{
  const raw=decodeURIComponent((location.hash||'').slice(1)), anchor=raw&&document.getElementById(raw);
  const n=raw[0]==='d'&&ORDER.includes(raw.slice(1))?raw.slice(1):
    anchor&&anchor.closest('.doc')?anchor.closest('.doc').id.slice(1):ORDER[0];
  document.querySelectorAll('.doc').forEach(a=>a.hidden=a.id!=='v'+n);
  document.querySelectorAll('aside a').forEach(a=>a.classList.toggle('on',a.dataset.n===n));
  const i=ORDER.indexOf(n), pg=document.querySelector('#v'+n+' .pager');
  const L=(k,lab)=>`<a href="#d${{k}}"><small>${{lab}}</small>${{N[k]?N[k]+' ':''}}${{T[k]}}</a>`;
  pg.innerHTML=(i>0?L(ORDER[i-1],'上一篇'):'<span></span>')+(i<ORDER.length-1?L(ORDER[i+1],'下一篇'):'<span></span>');
  document.getElementById('side').classList.add('closed');
  if(anchor&&anchor.closest('.doc'))anchor.scrollIntoView();else window.scrollTo(0,0);
}}
addEventListener('hashchange',show); show();
document.getElementById('tog').onclick=()=>document.getElementById('side').classList.toggle('closed');
{theme_toggle.body_script("docs-theme")}</script>
</body>
</html>
'''
os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(page)
print(OUT, len(page)//1024, "KB", len(docs), "docs")
left = re.findall(r'href="(?!#|[a-z][a-z0-9+.-]*:|//)([^"]*)"', page, re.I)
print("unresolved local links:", left[:5], len(left))
print("raw img refs:", len(re.findall(r'!\[', re.sub(r"<pre>.*?</pre>|<code>.*?</code>", "", page, flags=re.S))))
for w in warnings:
    print("warning:", w)
print("warnings:", len(warnings))
