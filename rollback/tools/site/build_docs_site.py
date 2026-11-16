"""build_docs_site：把一个目录（或一个文件）里的 markdown 合成一个单文件、可离线的 HTML 文档站。

左侧分组目录和全书搜索、居中正文（图、表和正文同宽）、宽屏右侧本篇小节；图片以 data URI 内嵌；
跨篇链接和页内锚点改成站内跳转；每篇末尾有上一篇/下一篇；深浅色主题；手机上目录收进抽屉；点图全屏查看。
对文件名、目录结构、标题写法都没有要求；只有一篇时不显示左栏。排版约定见 SKILL.md「排版要点」。

用法：
  python3 build_docs_site.py --docs <目录或 .md 文件> --out site.html [--title "站点标题"]
      [--groups groups.json] [--exclude "drafts/*"]... [--lang zh|en] [--accent "#1d2125"]
groups.json（可选）：[["", ["00"]], ["原理", ["01", "guide/setup.md"]], ...]，
每项写文件名开头的编号或相对 --docs 的路径。不给时按顶层子目录分组。
依赖：pip install markdown；装了 pygments 时代码块自动语法高亮。公式、mermaid、emoji 的库在 vendor/，用到才内嵌。
"""
import argparse, base64, fnmatch, html, json, os, posixpath, re, sys
from urllib.parse import unquote

try:
    import markdown
    from markdown.extensions.toc import TocExtension
except ImportError:
    sys.exit("缺少 markdown 包，请运行：pip install markdown")
try:                            # 可选：装了 pygments 就给代码块上色
    from pygments.formatters import HtmlFormatter
    from pygments.styles import get_style_by_name
    get_style_by_name("github-dark")
except Exception:
    HtmlFormatter = None
import theme_toggle

ap = argparse.ArgumentParser()
ap.add_argument("--docs", required=True, help="markdown 所在目录，或单个 .md 文件")
ap.add_argument("--out", required=True)
ap.add_argument("--title", help="站点标题，默认用第一篇的标题")
ap.add_argument("--groups")
ap.add_argument("--exclude", action="append", default=[], help="跳过的路径（相对 --docs 的 glob，可写多次）")
ap.add_argument("--lang", choices=["zh", "en"], default="zh", help="界面文字的语言")
ap.add_argument("--accent", default="#1d2125", help="浅色主题强调色，默认是正文墨色（不带彩色）")
ap.add_argument("--accent-dark", default="#e6e9ec", help="深色主题强调色")
ap.add_argument("--sibling", action="append", default=[], metavar="目录=地址",
                help="另一个也用本脚本生成的文档站：指向该目录里 md 的相对链接改成「地址#站内位置」，可写多次")
A = ap.parse_args()
D = A.docs
OUT = A.out
ONLY = None                     # --docs 给的是单个文件时，只收这一篇
if os.path.isfile(D):
    D, ONLY = os.path.dirname(os.path.abspath(D)), os.path.basename(D)
elif not os.path.isdir(D):
    sys.exit(f"目录或文件不存在：{D}")

# 界面文字
UI = {
    "zh": dict(lang="zh-CN", toc="目录", open_toc="打开目录", search="搜索全书", in_doc="本篇小节", prev="← 上一篇",
               next="下一篇 →", found="{d} 篇，共 {h} 处", none="没有找到", other="其他", intro="导读", missing="缺图", fig="图",
               dark="深色模式", to_light="切换到浅色模式", to_dark="切换到深色模式", viewer="查看大图", zoom_in="放大",
               zoom_out="缩小", fit="适合", fit_t="适合屏幕", close="关闭",
               alert=dict(note="注意", tip="提示", important="重要", warning="警告", caution="小心")),
    "en": dict(lang="en", toc="Contents", open_toc="Open contents", search="Search", in_doc="On this page",
               prev="← Previous", next="Next →", found="{h} matches in {d} pages", none="No results", other="Other", intro="Overview",
               missing="missing image", fig="Figure", dark="Dark mode", to_light="Switch to light mode",
               to_dark="Switch to dark mode", viewer="Image viewer", zoom_in="Zoom in", zoom_out="Zoom out",
               fit="Fit", fit_t="Fit to screen", close="Close",
               alert=dict(note="Note", tip="Tip", important="Important", warning="Warning", caution="Caution")),
}[A.lang]

MD_EXT = (".md", ".markdown")
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages", "vendor"}
MIME = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif"}
warnings = []
unresolved = []                 # 没能变成站内跳转的相对链接：「文档 → 地址」
VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
with open(os.path.join(VENDOR, "emoji.json"), encoding="utf-8") as _f:
    EMOJI = json.load(_f)


def natural(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def sort_key(rel):
    d, name = posixpath.split(rel)
    stem = name.rsplit(".", 1)[0].lower()
    return ([natural(p) for p in d.split("/")] if d else [], stem not in ("index", "readme"), natural(name))


def find_docs(root=None, exclude=None):
    if ONLY and root is None:
        return [ONLY]
    root = D if root is None else root
    exclude = A.exclude if exclude is None else exclude
    found = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS]
        for n in names:
            rel = os.path.relpath(os.path.join(base, n), root).replace(os.sep, "/")
            if n.lower().endswith(MD_EXT) and not any(fnmatch.fnmatch(rel, g) for g in exclude):
                found.append(rel)
    return sorted(found, key=sort_key)


def doc_keys(paths):
    """每篇的站内 key：文件名编号不重复时就是编号（01、A），否则是 p<序号>。返回 [(rel, 编号, key)]。"""
    prefixes = [m.group(1) if (m := re.match(r"(\d{1,3}|[A-Z])[-_ .]", posixpath.basename(r))) else None
                for r in paths]
    return [(rel, num, num if num is not None and prefixes.count(num) == 1 else f"p{i}")
            for i, (rel, num) in enumerate(zip(paths, prefixes))]


# --sibling：别的文档站的目录（绝对路径）→ (地址, 该站 rel → key)
SIBLINGS = []
for s in A.sibling:
    sdir, sep, url = s.partition("=")
    if not sep or not os.path.isdir(sdir):
        sys.exit(f"--sibling 要写成「目录=地址」，且目录存在：{s}")
    SIBLINGS.append((os.path.abspath(sdir), url, {r: k for r, _, k in doc_keys(find_docs(sdir, []))}))


def sibling_href(doc_rel, path, frag):
    """相对链接落在某个 --sibling 目录里的 md 上时，返回「地址#d<key>[-小节]」。"""
    p = local_path(doc_rel, path)
    if p is None:
        return None
    ap_ = os.path.normpath(os.path.join(os.path.abspath(D), p))
    for sdir, url, keys in SIBLINGS:
        if ap_ != sdir and not ap_.startswith(sdir + os.sep):
            continue
        r = os.path.relpath(ap_, sdir).replace(os.sep, "/").rstrip("/")
        cands = [r] + [f"{r}{e}" for e in MD_EXT] + [posixpath.normpath(f"{r}/{n}") for n in
                                                     ("README.md", "readme.md", "index.md", "Readme.md")]
        k = next((keys[c] for c in cands if c in keys), None)
        if k is not None:
            return f"{url}#d{k}-{unquote(frag).lower()}" if frag else f"{url}#d{k}"
    return None


def split_frontmatter(src):
    """去掉文件头的 YAML，返回 (正文, title 字段)。"""
    m = re.match(r"---\n(.*?)\n---[ \t]*(?:\n|$)", src, re.S)
    if not m:
        return src, None
    t = re.search(r"^title:[ \t]*(.+?)[ \t]*$", m.group(1), re.M)
    return src[m.end():], t.group(1).strip("\"'") if t else None


CODE = re.compile(r"(^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\2[ \t]*$|`[^`\n]+`)", re.M | re.S)
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", re.M | re.S)   # 只认代码块，找标题用


def outside_code(s, fn):
    """只对代码块和行内代码以外的文字做替换。"""
    out, pos = [], 0
    for m in CODE.finditer(s):
        out += [fn(s[pos:m.start()]), m.group(0)]
        pos = m.end()
    return "".join(out + [fn(s[pos:])])


LIST_ITEM = re.compile(r"( *)(?:[-*+]|\d+[.)]) +\S")

# 先抽出来、最后再填回去的片段：公式、mermaid、列表里的代码块。占位符是一个纯字母数字的词，markdown 不会动它。
PH = {}
USED = set()                    # 用到了哪些需要内嵌脚本的东西：math、mermaid


def ph(html_):
    t = f"MDSITEPH{len(PH)}X"
    PH[t] = html_
    return t


def render_fence(lang, code):
    """自己渲染的代码块：mermaid、math，以及列表里的代码块（fenced_code 不认带缩进的 fence）。"""
    lang = (lang or "").strip().split()[0].lower() if (lang or "").strip() else ""
    if lang == "mermaid":
        USED.add("mermaid")
        return f'<div class="mermaid-wrap"><pre class="mermaid" data-src="{html.escape(code)}">{html.escape(code)}</pre></div>'
    if lang == "math":
        USED.add("math")
        return f'<div class="math-display" data-tex="{html.escape(code.strip())}">{html.escape(code.strip())}</div>'
    if HtmlFormatter and lang:
        try:
            from pygments import highlight
            from pygments.lexers import get_lexer_by_name
            return highlight(code, get_lexer_by_name(lang), HtmlFormatter(cssclass="hl", wrapcode=True))
        except Exception:
            pass
    cls = f' class="language-{html.escape(lang)}"' if lang else ""
    return f"<pre><code{cls}>{html.escape(code)}</code></pre>"


def extract_special_fences(src):
    """顶层的 mermaid / math 代码块换成占位符（其余顶层代码块交给 fenced_code + codehilite）。"""
    lines, out, i = src.split("\n"), [], 0
    while i < len(lines):
        f = re.match(r"(`{3,}|~{3,})[ \t]*([\w-]*)", lines[i])
        if f and f.group(2).lower() in ("mermaid", "math"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith(f.group(1)):
                j += 1
            out += ["", ph(render_fence(f.group(2), "\n".join(lines[i + 1:j]))), ""]
            i = j + 1
            continue
        if f:                   # 普通代码块整段跳过，里面的 ``` 不算
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith(f.group(1)):
                j += 1
            out += lines[i:j + 1]
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def list_block(block):
    """把一段列表的缩进换算成 4 格；列表里的代码块换成占位符（fenced_code 不认带缩进的 fence），保留语言和高亮。"""
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
    # 层级按嵌套关系算：一项是它前面缩进更小的最近一项的子项。这样 2 格、3 格、4 格、tab 混用都能对上。
    stack = []                  # 各层列表项的原始缩进

    def item_depth(n):
        while stack and stack[-1] >= n:
            stack.pop()
        stack.append(n)
        return len(stack) - 1

    def body_depth(n):          # 非列表项的行（续行、代码块）属于缩进比它小的最近一层
        return sum(1 for x in stack if x < n)

    out, base, skip, closing, lang, code = [], 0, 0, None, "", []
    for b, m in zip(block, marks):
        if not m:
            if LIST_ITEM.match(b) and indent(b) and out and out[-1].strip() and not LIST_ITEM.match(out[-1]):
                out.append("")   # 段落后紧跟的子列表
            if LIST_ITEM.match(b):
                lead = 4 * item_depth(indent(b))
            else:
                lead = 4 * body_depth(indent(b)) if indent(b) else 0
            out.append(" " * lead + b.lstrip(" "))
        elif closing is None:
            f = re.match(r"(`{3,}|~{3,})[ \t]*(.*)", b.lstrip(" "))
            closing, lang, code = f.group(1), f.group(2), []
            skip = indent(b)
            base = 4 * max(body_depth(skip), 1) if skip else 0
        elif b.lstrip(" ").startswith(closing):
            out += ["", " " * base + ph(render_fence(lang, "\n".join(code))), ""]
            closing = None
        else:
            code.append(b[min(skip, indent(b)):] if b.strip() else "")
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


def setext_to_atx(src):
    """下划线写法的标题（下一行全是 = 或 -）改成 # / ##。只认整行，跳过代码块、列表、引用、表格。"""
    lines, fence = src.split("\n"), None
    for i, line in enumerate(lines):
        f = re.match(r"[ \t]*(`{3,}|~{3,})", line)
        if fence:
            fence = None if f and f.group(1).startswith(fence) else fence
            continue
        if f:
            fence = f.group(1)
            continue
        u = re.fullmatch(r" {0,3}(=+|-+)[ \t]*", line)
        prev = lines[i - 1] if i else ""
        if u and prev.strip() and not re.match(r"\s|[-*+>|#]|\d+[.)]\s|<", prev) \
                and (i < 2 or not lines[i - 2].strip()):
            lines[i - 1] = ("# " if u.group(1)[0] == "=" else "## ") + prev.strip()
            lines[i] = ""
    return "\n".join(lines)


def slug(text):
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def local_path(doc_rel, target):
    """把文档里的相对地址转成相对 --docs 的路径；外部地址返回 None。"""
    if re.match(r"[a-z][a-z0-9+.-]*:|//", target, re.I):
        return None
    t = unquote(re.split(r"[?#]", target, 1)[0])
    return t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(doc_rel), t))


def resolve_doc(doc_rel, target):
    """相对地址指向站内某一篇时返回它的 key：x.md、x（省略扩展名）、目录（取其中的 README / index）都算。"""
    p = local_path(doc_rel, target)
    if p is None:
        return None
    p = p.rstrip("/") or "."
    cands = [p] + [f"{p}{e}" for e in MD_EXT] + [posixpath.normpath(f"{p}/{n}") for n in
                                                 ("README.md", "readme.md", "index.md", "Readme.md")]
    k = next((rel2key[c] for c in cands if c in rel2key), None)
    if k is None and p != "." and os.path.isdir(os.path.join(D, p)):
        k = next((rel2key[r] for r in paths if r.startswith(p + "/")), None)
    return k


def svg_width(uri):
    """SVG 的宽度（像素）；读不出来时当作大图。窄图按原尺寸居中，不拉伸。"""
    try:
        head = base64.b64decode(uri.split(",", 1)[1])[:4000].decode("utf-8", "ignore")
        tag = re.search(r"<svg\b[^>]*>", head, re.S).group(0)
        w = re.search(r'\bwidth="([\d.]+)(?:px)?"', tag) or re.search(r'viewBox="[\d.\s-]+?\s([\d.]+)\s[\d.]+"', tag)
        return float(w.group(1))
    except Exception:
        return 1e9


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
for rel, num, doc_key in doc_keys(paths):
    with open(os.path.join(D, rel), encoding="utf-8-sig", errors="replace") as f:
        src = f.read().replace("\r\n", "\n")
    src = re.sub(r"^\t+", lambda t: "    " * len(t.group(0)), src, flags=re.M)   # 行首 tab 按 4 格算
    src, fm_title = split_frontmatter(src)
    # 下划线写法的标题（下一行是 === 或 ---）先改成 # / ##，后面统一处理
    src = setext_to_atx(src)
    m = re.search(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", FENCE.sub("", src), re.M)
    if not m and (h := re.search(r"^#{2,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", FENCE.sub("", src), re.M)) \
            and not (fm_title and fm_title != h.group(1)):
        src = src.replace(h.group(0), f"# {h.group(1)}", 1)      # 没有一级标题：第一个标题当篇名
        m = re.search(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", FENCE.sub("", src), re.M)
    stem = re.sub(r"^\d+[-_ .]", "", posixpath.basename(rel).rsplit(".", 1)[0])
    title = m.group(1) if m else fm_title or re.sub(r"[-_]+", " ", stem)
    title = title[:1].upper() + title[1:] if not m and not fm_title else title
    if not m:
        src = f"# {title}\n\n{src}"
    label = num if doc_key == num else ""
    # 标题以和文件名相同的编号开头（「05 · 」「附录 A · 」「Chapter 3: 」「第 3 章 」）时去掉这个前缀：
    # 编号已经显示在左栏目录里，正文标题不再重复。
    nav_title = title
    num_re = re.escape(label.lstrip("0") or "0") if label.isdigit() else re.escape(label)
    if label and (t := re.match(rf"(?:附录|appendix|chapter|part|第)?\s*0*{num_re}(?:\s*(?:章|篇|部分)\s*[·:：、—–|｜]?|\s*[·:：、—–|｜]|\s*-(?=\s)|\.(?=\s))\s*(.+)$",
                                title, re.I)
                  # 有「附录 / Chapter」这类前缀词时，编号后只隔空格也算（「附录 A　术语表」）
                  or re.match(rf"(?:附录|appendix|chapter|part)\s*0*{num_re}\s+(.+)$", title, re.I)):
        nav_title = t.group(1)
        if m:
            src = src.replace(m.group(0), f"# {nav_title}", 1)
    plain = lambda t: re.sub(r"`([^`\n]+)`", r"\1", t)
    docs[doc_key] = dict(rel=rel, src=src, title=plain(title), label=label, nav_title=plain(nav_title))

rel2key = {v["rel"]: k for k, v in docs.items()}


def rendered_doc(key, d):
    rel = d["rel"]

    def img_sub(m):
        alt, target = m.group(1), m.group(2) or m.group(3)
        uri = data_uri(rel, target)
        if uri is None:
            return m.group(0)
        if not uri:
            return f"**（{UI['missing']}：{html.escape(target)}）**"
        line_start = m.string.rfind("\n", 0, m.start()) + 1
        line_end = (m.string + "\n").find("\n", m.end())
        attrs = f'alt="{html.escape(alt)}" src="{uri}"'
        if m.string[line_start:m.start()].strip() or m.string[m.end():line_end].strip():
            return f'<img class="inline" {attrs}>'
        stem = posixpath.basename(unquote(target)).rsplit(".", 1)[0]
        n = re.match(r"(\d+)-([^-]+)(?:-|$)", stem)
        cap = (f"<b>{UI['fig']} {int(n.group(1))}-{int(n.group(2))}</b>{html.escape(alt)}"
               if n and n.group(2).isdigit() else html.escape(alt))
        kind = "paper" if target.lower().endswith(".svg") and svg_width(uri) >= 760 else "paper raster"
        figcap = f"<figcaption>{cap}</figcaption>" if cap else ""
        return f'\n\n<figure class="{kind}"><img loading="lazy" {attrs}>{figcap}</figure>\n\n'

    def html_img_sub(m):
        uri = data_uri(rel, html.unescape(m.group(2)))
        return m.group(1) + uri + m.group(3) if uri else m.group(0)

    def href(target):
        """站内地址换成 #d<key>[-小节]；本地图片换成 data URI；其他原样返回 None。"""
        if target.startswith("#"):
            return f"#d{key}-{unquote(target[1:]).lower()}"
        path, _, frag = target.partition("#")
        k = resolve_doc(rel, path) if path else None
        if k is not None:
            return f"#d{k}-{unquote(frag).lower()}" if frag else f"#d{k}"
        if path and (s := sibling_href(rel, path, frag)):
            return s
        if os.path.splitext(path.split("?")[0])[1].lower() in MIME:
            return data_uri(rel, path) or None
        if local_path(rel, target) is not None:
            unresolved.append(f"{rel} → {target}")
        return None

    def link_sub(m):
        new = href(m.group(2))
        return f"{m.group(1)}({new}{m.group(3)})" if new else m.group(0)

    def ref_sub(m):             # 引用式链接的定义行：[id]: 地址 "标题"
        new = href(m.group(2))
        return f"{m.group(1)}{new}{m.group(3)}" if new else m.group(0)

    def a_sub(m):
        new = href(html.unescape(m.group(2)))
        return m.group(1) + html.escape(new) + m.group(3) if new else m.group(0)

    def text_sub(s):
        # 公式：$$…$$ 是独立公式，$…$ 是行内公式（$ 后不能紧跟空格、结尾 $ 前不能是空格、后面不能紧跟数字，避免误认金额）
        def disp(m):
            USED.add("math")
            tex = html.escape(m.group(1).strip())
            return "\n\n" + ph(f'<div class="math-display" data-tex="{tex}">{tex}</div>') + "\n\n"

        def inl(m):
            USED.add("math")
            tex = m.group(1)
            return ph(f'<span class="math" data-tex="{html.escape(tex)}">{html.escape(tex)}</span>')
        s = re.sub(r"(?<!\\)\$\$([\s\S]+?)\$\$", disp, s)
        s = re.sub(r"(?<![\\$\w])\$(?![\s$])([^$\n]+?)(?<![\s\\])\$(?![\w$])", inl, s)
        s = s.replace("\\$", "&#36;")
        # emoji 短码 :smile:
        s = re.sub(r"(?<![\w:/]):([a-z0-9_+-]*[a-z][a-z0-9_+-]*):(?![\w:])", lambda m: EMOJI.get(m.group(1), m.group(0)), s)
        s = re.sub(r'!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+"[^"]*")?\s*\)', img_sub, s)
        s = re.sub(r'(<img\b[^>]*?\bsrc=["\'])([^"\']+)(["\'])', html_img_sub, s)
        s = re.sub(r'(<a\b[^>]*?\bhref=["\'])([^"\']+)(["\'])', a_sub, s)
        s = re.sub(r"((?<!!)\[[^\]]*\])\(\s*<?([^)\s>]+)>?((?:\s+\"[^\"]*\")?\s*)\)", link_sub, s)
        s = re.sub(r"^( {0,3}\[[^\]^\n][^\]\n]*\]:[ \t]*<?)([^\s>]+)(>?.*)$", ref_sub, s, flags=re.M)
        # 裸网址自动变成链接（已经在链接、尖括号、HTML 属性里的不动）
        s = re.sub(r"(?<![\(\[<\"'=/\w])(https?://[^\s<>\)\]\"'，。；：！？、」）]+[^\s<>\)\]\"'，。；：！？、」）.,;:!?])",
                   r"<\1>", s)
        s = re.sub(r"(^ {0,3}>.*\n)((?:[ \t]*\n)+)(?= {0,3}>)", r"\1\2<!-- -->\n\n", s, flags=re.M)
        return re.sub(r"~~([^~\n]+)~~", r"<del>\1</del>", s)

    toc = TocExtension(slugify=lambda v, sep: f"d{key}-{slug(v)}")
    exts = ["tables", "fenced_code", "sane_lists", "footnotes", "def_list", "md_in_html", toc]
    if HtmlFormatter:
        exts.append("codehilite")
    h = markdown.markdown(outside_code(normalize_lists(extract_special_fences(d["src"])), text_sub), extensions=exts,
                          extension_configs={"codehilite": {"css_class": "hl", "guess_lang": False}})
    h = re.sub(r"<table>", '<div class="tw"><table>', h).replace("</table>", "</table></div>")
    # 所有篇在同一个页面里，脚注的 id 要带上篇的 key 才不会撞
    h = re.sub(r'(id="|href="#)(fn(?:ref)?:)', rf"\1d{key}-\2", h)

    def alert(m):               # GitHub 的提示块：> [!NOTE] …
        kind = m.group(1).lower()
        return f'<blockquote class="alert alert-{kind}"><p class="alert-t">{UI["alert"][kind]}</p><p>'
    h = re.sub(r"<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", alert, h, flags=re.I)
    h = h.replace('</p><p></p>', '</p>')
    # 占位符填回去：单独成段的去掉外面的 <p>（可能套了几层，比如占位符里还有占位符）
    for _ in range(3):
        h = re.sub(r"<p>(MDSITEPH\d+X)</p>", lambda m: PH[m.group(1)], h)
        h = re.sub(r"MDSITEPH\d+X", lambda m: PH[m.group(0)], h)
    return re.sub(r"<li>(<p>)?\[([ xX])\] ",
                  lambda m: f'<li class="task">{m.group(1) or ""}<input type="checkbox" disabled'
                            f'{"" if m.group(2) == " " else " checked"}> ', h)


nav_titles = {k: d["nav_title"] for k, d in docs.items()}

# ------------------------------------------------------------------ 目录分组
if A.groups:
    with open(A.groups, encoding="utf-8") as f:
        GROUPS = [(g, [rel2key.get(n, n) for n in ns]) for g, ns in json.load(f)]
    listed = {n for _, ns in GROUPS for n in ns}
    for n in [n for _, ns in GROUPS for n in ns if n not in docs]:
        warnings.append(f"groups 里的 {n} 没有对应文档")
    rest = [k for k in docs if k not in listed]
    if rest:
        GROUPS.append((UI["other"], rest))
else:
    GROUPS = {}
    for k, v in docs.items():
        g = v["rel"].split("/")[0] if "/" in v["rel"] else ""
        g = re.sub(r"^\d+[-_ .]", "", g)                      # 目录名的编号前缀不显示
        g = re.sub(r"[-_]+", " ", g)
        GROUPS.setdefault(g[:1].upper() + g[1:], []).append(k)
    GROUPS = sorted(GROUPS.items(), key=lambda kv: (kv[0] != "", natural(kv[0])))
order = [n for _, ns in GROUPS for n in ns if n in docs]
site_title = A.title or nav_titles[order[0]]
# 第一篇（通常是 README）的标题就是站名时，左栏已在顶上显示站名，这一项改叫「导读」，免得同一行字出现两次
nav_label = dict(nav_titles)
if nav_label[order[0]] == site_title:
    nav_label[order[0]] = UI["intro"]
group_of = {}
nav = []
for g, keys in GROUPS:
    if g:
        nav.append(f'<div class="ng">{html.escape(g)}</div>')
    for n in keys:
        if n in docs:
            group_of[n] = g
            tag = f'<span class="nn">{docs[n]["label"]}</span>' if docs[n]["label"] else ""
            nav.append(f'<a href="#d{n}" data-n="{n}">{tag}<span class="nt">{html.escape(nav_label[n])}</span>'
                       f'<em class="hit"></em></a>')

sections = []
for key, d in docs.items():
    sections.append(f'<article id="v{key}" class="doc" hidden>{rendered_doc(key, d)}'
                    f'<nav class="pager" data-n="{key}"></nav></article>')


def js_json(v):
    return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")


# 浅色、深色两套配色；其余样式只引用变量
LIGHT = ("--bg:#ffffff;--side:#f7f8f9;--surface:#ffffff;--surface-2:#f0f2f4;--ink:#1d2125;--ink-2:#4a525a;"
         "--muted:#8b939b;--rule:#e6e8eb;--rule-2:#d3d7dc;--accent:%s;--accent-soft:color-mix(in srgb,var(--accent) 7%%,var(--bg));--code:#f4f5f7;"
         "--mark:#ffe69a")
DARK = ("--bg:#131619;--side:#171b1f;--surface:#1a1f23;--surface-2:#22282d;--ink:#e6e9ec;--ink-2:#b5bdc4;"
        "--muted:#848d96;--rule:#2a3036;--rule-2:#394047;--accent:%s;--accent-soft:color-mix(in srgb,var(--accent) 11%%,var(--bg));--code:#1d2226;"
        "--mark:#6b5a1c;color-scheme:dark")

CSS = """
:root{%LIGHT%;
--f:"PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",system-ui,sans-serif;
--m:"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;--read:960px}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){%DARK%}}
:root[data-theme="dark"]{%DARK%}
*{box-sizing:border-box}
html{scroll-padding-top:24px}
body{background:var(--bg);color:var(--ink);font:16px/1.85 var(--f);margin:0;-webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;font-variant-numeric:tabular-nums}
::selection{background:var(--accent-soft)}
::highlight(hit){background:var(--mark);color:inherit}
#bar{position:fixed;top:0;left:0;height:2px;width:0;background:var(--accent);z-index:30}

/* ---------- 三栏：目录 / 正文 / 本篇小节 ---------- */
.wrap{display:grid;grid-template-columns:288px minmax(0,1fr);min-height:100vh}
aside.side{position:sticky;top:0;height:100vh;overflow:auto;background:var(--side);border-right:1px solid var(--rule);
  padding:22px 14px 48px;padding-left:max(14px,env(safe-area-inset-left,0px));scrollbar-width:thin}
.brandrow{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;padding:0 6px 0 10px}
.brand{font-weight:700;font-size:17px;line-height:1.35;letter-spacing:-.01em;padding-top:6px}
.search{display:block;position:relative;margin:18px 4px 6px}
.search svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.search input{width:100%;font:14px var(--f);color:var(--ink);background:var(--bg);border:1px solid var(--rule);
  border-radius:9px;padding:7px 34px 7px 32px;outline:none}
.search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.search kbd{position:absolute;right:9px;top:50%;transform:translateY(-50%);font:11px var(--m);color:var(--muted);
  border:1px solid var(--rule);border-radius:4px;padding:0 5px;background:var(--surface)}
.search input:not(:placeholder-shown)+kbd{display:none}
.sres{font-size:12.5px;color:var(--muted);padding:2px 10px 0;min-height:0}
.ng{font-size:12px;font-weight:600;letter-spacing:.06em;color:var(--muted);margin:22px 10px 6px}
.navlist a{display:flex;align-items:baseline;gap:10px;padding:6px 10px;border-radius:8px;color:var(--ink-2);
  text-decoration:none;font-size:14px;line-height:1.5;position:relative}
.navlist a:hover{background:var(--surface-2);color:var(--ink)}
.navlist a.on{background:var(--accent-soft);color:var(--ink);font-weight:600}
.navlist a.on .nn{color:var(--accent)}
.nn{font:12px var(--m);color:var(--muted);min-width:20px;flex:none}
.nt{flex:1}
.hit{font:600 11px var(--m);font-style:normal;color:var(--accent);flex:none}
.hit:empty{display:none}
.navlist a.miss,.ng.miss{display:none}

main{min-width:0;padding:64px 56px 120px}
/* 只有一篇：不要左栏和顶栏 */
.single .wrap{grid-template-columns:minmax(0,1fr)}
.single aside.side,.single .scrim,.single .topbar,.single .pager{display:none!important}
.corner{position:fixed;top:16px;right:16px;z-index:25}
.corner .theme-toggle{background:var(--bg)}
.toc{display:none}

/* ---------- 正文 ---------- */
.doc{max-width:var(--read);margin:0 auto}
h1,h2,h3,h4{color:var(--ink);text-wrap:balance;scroll-margin-top:24px}
h1{font-size:36px;line-height:1.28;font-weight:700;letter-spacing:-.015em;margin:8px 0 28px}
h2{font-size:24px;line-height:1.4;font-weight:700;letter-spacing:-.01em;margin:72px 0 18px}
h3{font-size:18.5px;line-height:1.5;font-weight:650;margin:44px 0 12px}
h4{font-size:16px;font-weight:650;margin:32px 0 8px}
h2 + h3{margin-top:24px}
p{margin:0 0 1.15em}
strong{font-weight:650;color:var(--ink)}
a{color:var(--accent);text-decoration:underline;text-decoration-color:color-mix(in srgb,var(--accent) 35%,transparent);
  text-underline-offset:3px}
a:hover{text-decoration-color:currentColor}
ul,ol{padding-left:1.5em;margin:0 0 1.15em}
li{margin:.3em 0}
li::marker{color:var(--muted)}
li > ul,li > ol{margin:.3em 0 .4em}
hr{border:0;border-top:1px solid var(--rule);margin:48px 0}
code{font-family:var(--m);font-size:.86em;background:var(--code);border:1px solid var(--rule);padding:.08em .38em;
  border-radius:5px;overflow-wrap:anywhere}
pre{background:var(--code);border:1px solid var(--rule);padding:16px 18px;border-radius:12px;overflow-x:auto;
  font-size:13.5px;line-height:1.65;margin:20px 0 24px}
pre code{background:none;border:0;padding:0;font-size:inherit;overflow-wrap:normal}
blockquote{margin:22px 0;padding:2px 0 2px 20px;border-left:3px solid var(--rule-2);color:var(--ink-2)}
blockquote p:last-child{margin-bottom:0}
/* 标题后紧跟的引用当作本篇提要 */
.doc > h1 + blockquote{border:0;background:var(--surface-2);border-radius:14px;padding:18px 24px;margin:0 0 40px;
  font-size:15px;line-height:1.8;color:var(--ink-2)}
.doc > h1 + blockquote strong:first-child{color:var(--accent)}
.doc > h1 + blockquote p{margin-bottom:.5em}
.doc > h1 + blockquote + h2{margin-top:0}

/* 表格和图可以比正文宽 */
.tw{overflow-x:auto;margin:24px 0 28px;border:1px solid var(--rule);border-radius:12px;background:var(--surface)}
table{border-collapse:collapse;font-size:14px;line-height:1.6;min-width:100%}
th,td{padding:10px 16px;text-align:left;vertical-align:top;border-bottom:1px solid var(--rule)}
tr:last-child td{border-bottom:0}
th{font-weight:600;color:var(--ink);background:var(--surface-2);white-space:nowrap;font-size:13.5px}
tbody tr:hover td{background:color-mix(in srgb,var(--surface-2) 55%,transparent)}
td code{font-size:.82em}
/* 图直接放在页面上：不加卡片、边框、内边距；深色主题下 SVG 自带的白底就是图的边界，只给它圆角 */
figure.paper{margin:36px 0 40px;overflow-x:auto}
figure.paper img{display:block;width:100%;height:auto;min-width:600px;cursor:zoom-in;border-radius:10px;background:#fff}
figure.raster img{width:auto;max-width:100%;min-width:0;margin:0 auto}
/* 透明底的图在深色主题下也要看得清：图片本身垫白底（白底的图不受影响） */
/* 图注：一行小字、同一个颜色，编号只比说明稍重一点，中间隔一个字的距离 */
figcaption{font-size:13.5px;color:var(--ink-2);text-align:center;padding-top:10px;line-height:1.6;text-wrap:balance}
figcaption b{font-weight:600;margin-right:1em}

/* 公式、mermaid */
.math-display{margin:22px 0;overflow-x:auto;overflow-y:hidden;text-align:center}
.mermaid-wrap{margin:28px 0;overflow-x:auto;text-align:center}
.mermaid-wrap svg{cursor:zoom-in;height:auto}
pre.mermaid{background:none;border:0;padding:0;margin:0;color:var(--muted);font-size:13px;white-space:pre-wrap}
pre.mermaid[data-processed]{white-space:normal}
.katex{font-size:1.08em}
img.inline{max-width:100%;height:auto;vertical-align:middle}
li.task{list-style:none}
li.task input{margin:0 8px 0 -1.4em;accent-color:var(--accent)}

/* 上一篇 / 下一篇 */
.pager{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:12px;margin-top:88px;
  padding-top:24px;border-top:1px solid var(--rule)}
.pager a{display:inline-block;max-width:calc(50% - 6px);text-decoration:none;padding:9px 16px;border:1px solid var(--rule);
  border-radius:10px;color:var(--ink);font-weight:600;font-size:14.5px;line-height:1.5;transition:border-color .15s,color .15s}
.pager a:hover{border-color:var(--accent);color:var(--accent)}
.pager a.next{text-align:right;margin-left:auto}
.pager small{display:block;color:var(--muted);font-size:12px;font-weight:400}

/* 看大图：点图全屏，滚轮缩放、拖动平移 */
.lb{position:fixed;inset:0;z-index:60;background:rgba(12,15,18,.92);overflow:hidden;touch-action:none}
.lb[hidden]{display:none}
.lb img{position:absolute;left:0;top:0;transform-origin:0 0;max-width:none;background:#fff;border-radius:6px;
  box-shadow:0 10px 50px rgba(0,0,0,.5);cursor:grab;user-select:none;-webkit-user-drag:none}
.lb.drag img{cursor:grabbing}
.lb-bar{position:absolute;left:50%;bottom:20px;transform:translateX(-50%);display:flex;align-items:center;gap:4px;
  max-width:calc(100% - 32px);background:rgba(30,35,40,.88);color:#e6e9ec;border-radius:12px;padding:6px 6px 6px 16px;
  font-size:13px;backdrop-filter:blur(8px)}
.lb-bar .cap{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-right:8px}
.lb-bar .pct{font:12px var(--m);color:#9aa3ab;min-width:44px;text-align:center}
.lb-bar button{display:inline-flex;align-items:center;justify-content:center;min-width:34px;height:34px;padding:0 8px;
  background:transparent;color:inherit;border:0;border-radius:8px;font:14px var(--f);cursor:pointer}
.lb-bar b{color:#fff;font-weight:600;margin-right:8px}
.lb-bar button:hover{background:rgba(255,255,255,.12)}
.lb-bar button:focus-visible{outline:2px solid #fff}

/* GitHub 提示块、脚注、定义列表、语法高亮 */
.alert{border-left-color:var(--al);background:color-mix(in srgb,var(--al) 6%,transparent);border-radius:0 10px 10px 0;
  padding:12px 18px;color:var(--ink-2)}
.alert-t{font-weight:650;color:var(--al);margin-bottom:.3em}
.alert-note{--al:#4b7bd0}.alert-tip{--al:#3a9a6e}.alert-important{--al:#8a63d2}.alert-warning{--al:#c4861d}
.alert-caution{--al:#d0534a}
.footnote{margin-top:56px;font-size:14px;color:var(--ink-2)}
.footnote hr{margin:0 0 16px}
.footnote-ref a,.footnote-backref{text-decoration:none}
dt{font-weight:650;margin-top:.8em}
dd{margin:0 0 .6em 1.5em;color:var(--ink-2)}
.hl{margin:20px 0 24px}
.hl pre{margin:0}
%HL%

/* 本篇小节（宽屏右栏） */
@media (min-width:1280px){
 .wrap{grid-template-columns:288px minmax(0,1fr) 248px}
 .single .wrap{grid-template-columns:minmax(0,1fr) 248px}
 .toc{display:block;position:sticky;top:0;height:100vh;overflow:auto;padding:72px 24px 40px 4px;scrollbar-width:none}
 .toc:empty{visibility:hidden}
 .toc .tt{font-size:12px;font-weight:600;letter-spacing:.06em;color:var(--muted);margin:0 0 10px 14px}
 .toc a{display:block;font-size:13px;line-height:1.5;color:var(--muted);text-decoration:none;padding:5px 0 5px 14px;
   border-left:2px solid var(--rule)}
 .toc a.h3{padding-left:28px;font-size:12.5px}
 .toc a:hover{color:var(--ink)}
 .toc a.on{color:var(--accent);border-left-color:var(--accent);font-weight:600}
 main{padding-inline:40px}
}

/* 窄屏：顶栏 + 抽屉目录 */
.topbar,.scrim{display:none}
@media (max-width:860px){
 .wrap{display:block}
 .topbar{display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:20;height:52px;
   padding:0 max(12px,env(safe-area-inset-right,0px)) 0 max(12px,env(safe-area-inset-left,0px));
   background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--rule)}
 .topbar .t{font-weight:700;font-size:15px;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .menu{display:inline-flex;align-items:center;gap:6px;font:14px var(--f);color:var(--ink);background:var(--surface-2);
   border:1px solid var(--rule);border-radius:8px;padding:6px 12px;cursor:pointer}
 aside.side{position:fixed;z-index:40;top:0;left:0;bottom:0;height:auto;width:min(86vw,320px);
   transform:translateX(-102%);transition:transform .22s ease}
 aside.side.open{transform:none;box-shadow:0 0 40px rgba(0,0,0,.18)}
 .scrim{position:fixed;inset:0;z-index:35;background:rgba(10,14,18,.35)}
 aside.side.open ~ .scrim{display:block}
 main{padding:28px max(16px,env(safe-area-inset-right,0px)) 80px max(16px,env(safe-area-inset-left,0px))}
 h1{font-size:28px;margin-bottom:22px}
 h2{font-size:21px;margin-top:52px}
 h3{font-size:17px}
 body{font-size:15.5px}
 .doc > h1 + blockquote{padding:14px 16px;font-size:14.5px}
 figure.paper img{min-width:640px}
 .pager a{max-width:100%}
}
"""


def hl_css():
    """pygments 的配色：浅色用 default，深色用 github-dark；背景交给 pre 自己的样式。"""
    if not HtmlFormatter:
        return ""
    strip = lambda t: "\n".join(l for l in t.split("\n") if not re.match(r"\S+ \{ background", l)
                                  and "pre { line-height" not in l)
    light = strip(HtmlFormatter(style="default").get_style_defs(".hl"))
    dark = HtmlFormatter(style="github-dark").get_style_defs(".hl")
    dark_m = strip("\n".join(":root:not([data-theme=light]) " + l for l in dark.split("\n") if l.startswith(".hl")))
    dark_a = strip("\n".join(":root[data-theme=dark] " + l for l in dark.split("\n") if l.startswith(".hl")))
    return f"{light}\n@media (prefers-color-scheme:dark){{{dark_m}}}\n{dark_a}"


CSS = (CSS.replace("%LIGHT%", LIGHT % A.accent).replace("%DARK%", DARK % A.accent_dark).replace("%HL%", hl_css())
       + theme_toggle.CSS)

JS = r"""
const ORDER=%ORDER%, T=%T%, UI=%UI%;
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const side=$('#side'), toc=$('#toc'), q=$('#q'), bar=$('#bar');
let cur=null, heads=[], jumpToHit=false, lastOn=-2;

function buildToc(art){
  heads=[...art.querySelectorAll('h2,h3')];
  const h2n=heads.filter(h=>h.tagName==='H2').length;
  if(h2n<2){toc.innerHTML='';heads=[];return}
  toc.scrollTop=0; lastOn=-2;
  toc.innerHTML='<div class="tt">'+UI.in_doc+'</div>'+heads.map(h=>
    `<a href="#${h.id}" class="${h.tagName.toLowerCase()}">${h.textContent}</a>`).join('');
}
function onScroll(){
  const d=document.documentElement, max=d.scrollHeight-innerHeight;
  bar.style.width=(max>0?Math.min(100,scrollY/max*100):0)+'%';
  if(!heads.length)return;
  let i=-1; for(let k=0;k<heads.length;k++){if(heads[k].getBoundingClientRect().top<120)i=k;else break}
  const links=toc.querySelectorAll('a');
  links.forEach((a,k)=>a.classList.toggle('on',k===i));
  // 当前小节跑出右栏可见范围时，右栏自己滚过去，让它停在上三分之一处；鼠标正在右栏里时不抢
  if(i!==lastOn){lastOn=i; const a=links[i];
    if(a&&!toc.matches(':hover')){const t=a.offsetTop, h=toc.clientHeight;
      if(t<toc.scrollTop+48||t>toc.scrollTop+h-48)toc.scrollTo({top:t-h/3,behavior:'smooth'})}}
}
function highlight(art){
  if(!window.CSS||!CSS.highlights)return null;
  CSS.highlights.delete('hit');
  const s=q.value.trim().toLowerCase(); if(!s)return null;
  const ranges=[], it=document.createTreeWalker(art,NodeFilter.SHOW_TEXT);
  for(let n;(n=it.nextNode())&&ranges.length<800;){
    const t=n.data.toLowerCase(); let i=t.indexOf(s);
    while(i>=0&&ranges.length<800){const r=new Range();r.setStart(n,i);r.setEnd(n,i+s.length);ranges.push(r);i=t.indexOf(s,i+s.length)}
  }
  if(ranges.length)CSS.highlights.set('hit',new Highlight(...ranges));
  return ranges[0]||null;
}
function show(){
  const raw=decodeURIComponent((location.hash||'').slice(1)), anchor=raw&&document.getElementById(raw);
  const n=raw[0]==='d'&&ORDER.includes(raw.slice(1))?raw.slice(1):
    anchor&&anchor.closest('.doc')?anchor.closest('.doc').id.slice(1):ORDER[0];
  const art=$('#v'+n);
  $$('.doc').forEach(a=>a.hidden=a!==art);
  $$('.navlist a').forEach(a=>a.classList.toggle('on',a.dataset.n===n));
  const i=ORDER.indexOf(n), pg=art.querySelector('.pager');
  const L=(k,lab,c)=>`<a class="${c}" href="#d${k}"><small>${lab}</small>${T[k]}</a>`;
  pg.innerHTML=(i>0?L(ORDER[i-1],UI.prev,'prev'):'')+(i<ORDER.length-1?L(ORDER[i+1],UI.next,'next'):'');
  document.title=ORDER.length>1&&T[n]!==%SITE%?T[n]+' · '+%SITE%:%SITE%;
  side.classList.remove('open');
  if(cur!==n){cur=n;buildToc(art)}
  renderMermaid(art);
  const first=highlight(art);
  if(anchor&&anchor.closest('.doc')&&!/^d[^-]*$/.test(raw))anchor.scrollIntoView();
  else if(jumpToHit&&first){const r=first.getBoundingClientRect();scrollTo(0,scrollY+r.top-innerHeight/3)}
  else scrollTo(0,0);
  jumpToHit=false; onScroll();
  const on=$('.navlist a.on'); if(on&&on.scrollIntoViewIfNeeded)on.scrollIntoViewIfNeeded(false);
}

/* 全书搜索：按篇过滤目录，显示命中次数；打开的那篇里高亮 */
const TEXT=Object.fromEntries($$('.doc').map(a=>[a.id.slice(1),a.textContent.toLowerCase()]));
function search(){
  const s=q.value.trim().toLowerCase(); let docs=0, hits=0;
  $$('.navlist a').forEach(a=>{
    let c=0; if(s){const t=TEXT[a.dataset.n];for(let i=t.indexOf(s);i>=0&&c<999;i=t.indexOf(s,i+s.length))c++}
    a.classList.toggle('miss',!!s&&!c); a.querySelector('.hit').textContent=s&&c?c:'';
    if(c){docs++;hits+=c}
  });
  $$('.ng').forEach(g=>{let e=g.nextElementSibling, any=false;
    while(e&&!e.classList.contains('ng')){if(!e.classList.contains('miss'))any=true;e=e.nextElementSibling}
    g.classList.toggle('miss',!any)});
  $('#sres').textContent=s?(docs?UI.found.replace('{d}',docs).replace('{h}',hits):UI.none):'';
  if(cur)highlight($('#v'+cur));
}
q.addEventListener('input',search);
q.addEventListener('keydown',e=>{if(e.key==='Escape'){q.value='';search();q.blur()}
  if(e.key==='Enter'){const a=$('.navlist a:not(.miss)');if(a){jumpToHit=true;location.hash=a.getAttribute('href');if(location.hash===a.getAttribute('href'))show()}}});
$('.navlist').addEventListener('click',e=>{if(e.target.closest('a')&&q.value.trim())jumpToHit=true});

addEventListener('hashchange',show);
addEventListener('scroll',()=>requestAnimationFrame(onScroll),{passive:true});
addEventListener('keydown',e=>{
  if(!lb.hidden||e.target.closest('input,textarea')||e.metaKey||e.ctrlKey||e.altKey)return;
  if(e.key==='/'&&ORDER.length>1){e.preventDefault();side.classList.add('open');q.focus();return}
  const i=ORDER.indexOf(cur);
  if(e.key==='ArrowLeft'&&i>0)location.hash='d'+ORDER[i-1];
  if(e.key==='ArrowRight'&&i<ORDER.length-1)location.hash='d'+ORDER[i+1];
});
$('#menu').onclick=()=>side.classList.add('open');

/* 看大图 */
const lb=$('#lb'), li=$('#lbimg'), pct=$('#lbpct');
let S=1, X=0, Y=0, FIT=1, drag=null, back=null;
const pts=new Map();
function apply(){li.style.transform=`translate(${X}px,${Y}px) scale(${S})`;pct.textContent=Math.round(S/FIT*100)+'%'}
function fit(){
  const w=li.naturalWidth||1600, h=li.naturalHeight||900;
  li.style.width=w+'px'; li.style.height=h+'px';
  FIT=Math.min((innerWidth-48)/w,(innerHeight-110)/h);
  S=FIT; X=(innerWidth-w*S)/2; Y=Math.max(24,(innerHeight-70-h*S)/2); apply();
}
function zoom(k,px,py){
  const ns=Math.min(FIT*10,Math.max(FIT*.5,S*k)); k=ns/S;
  X=px-(px-X)*k; Y=py-(py-Y)*k; S=ns; apply();
}
function openLb(img,cap,bg){
  back=document.activeElement;
  li.src=img.src; li.style.background=bg||'';
  $('#lbcap').innerHTML=cap!==undefined?cap:img.closest('figure').querySelector('figcaption')?.innerHTML||img.alt;
  lb.hidden=false; document.body.style.overflow='hidden';
  (li.complete?fit():li.onload=fit); $('#lbclose').focus();
}
function closeLb(){lb.hidden=true;document.body.style.overflow='';li.removeAttribute('src');back&&back.focus()}
document.addEventListener('click',e=>{
  const im=e.target.closest('figure.paper img'); if(im)return openLb(im);
  const sv=e.target.closest('.mermaid-wrap svg'); if(!sv)return;
  const c=sv.cloneNode(true), vb=sv.viewBox.baseVal;       // 图本身是 width=100%，放大前给它定一个像素尺寸
  if(vb&&vb.width){c.setAttribute('width',vb.width);c.setAttribute('height',vb.height)}
  c.style.maxWidth=''; c.setAttribute('xmlns','http://www.w3.org/2000/svg');
  openLb({src:'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(c.outerHTML)},'',getComputedStyle(document.body).backgroundColor);
});
lb.addEventListener('wheel',e=>{e.preventDefault();zoom(Math.exp(-e.deltaY*(e.deltaMode?0.05:0.0015)),e.clientX,e.clientY)},{passive:false});
lb.addEventListener('pointerdown',e=>{
  if(e.target.closest('.lb-bar'))return;
  pts.set(e.pointerId,[e.clientX,e.clientY]); lb.setPointerCapture(e.pointerId);
  drag={x:e.clientX,y:e.clientY,moved:false,d:null}; lb.classList.add('drag');
});
lb.addEventListener('pointermove',e=>{
  if(!pts.has(e.pointerId))return;
  const prev=pts.get(e.pointerId); pts.set(e.pointerId,[e.clientX,e.clientY]);
  if(pts.size===2){
    const [a,b]=[...pts.values()], d=Math.hypot(a[0]-b[0],a[1]-b[1]);
    if(drag.d)zoom(d/drag.d,(a[0]+b[0])/2,(a[1]+b[1])/2); drag.d=d; drag.moved=true; return;
  }
  X+=e.clientX-prev[0]; Y+=e.clientY-prev[1];
  if(Math.abs(e.clientX-drag.x)+Math.abs(e.clientY-drag.y)>4)drag.moved=true; apply();
});
const up=e=>{
  if(!pts.has(e.pointerId))return; pts.delete(e.pointerId);
  if(pts.size)return; lb.classList.remove('drag');
  if(drag&&!drag.moved&&e.target===lb)closeLb(); drag=null;
};
lb.addEventListener('pointerup',up); lb.addEventListener('pointercancel',up);
li.addEventListener('dblclick',e=>{S/FIT>1.5?fit():zoom(2.5,e.clientX,e.clientY)});
$('#lbin').onclick=()=>zoom(1.4,innerWidth/2,innerHeight/2);
$('#lbout').onclick=()=>zoom(1/1.4,innerWidth/2,innerHeight/2);
$('#lbfit').onclick=fit;
$('#lbclose').onclick=closeLb;
addEventListener('resize',()=>{if(!lb.hidden)fit()});
lb.addEventListener('keydown',e=>{
  if(e.key==='Escape')closeLb();
  else if(e.key==='+'||e.key==='=')zoom(1.4,innerWidth/2,innerHeight/2);
  else if(e.key==='-')zoom(1/1.4,innerWidth/2,innerHeight/2);
  else if(e.key==='0')fit();
  else if(e.key==='Tab'){const f=[...lb.querySelectorAll('button')], i=f.indexOf(document.activeElement);
    e.preventDefault(); f[(i+(e.shiftKey?-1:1)+f.length)%f.length].focus()}
});
$('#scrim').onclick=()=>side.classList.remove('open');

/* 公式：KaTeX 渲染；没有内嵌 KaTeX 时保留 TeX 原文 */
if(window.katex)$$('.math,.math-display').forEach(el=>{
  try{katex.render(el.dataset.tex,el,{displayMode:el.classList.contains('math-display'),throwOnError:false})}catch(e){}
});
/* mermaid：只渲染当前这一篇（隐藏的篇量不出尺寸）；切换深浅色时按新配色重画 */
let mmTheme=null;
function darkNow(){const t=document.documentElement.dataset.theme;return t?t==='dark':matchMedia('(prefers-color-scheme: dark)').matches}
async function renderMermaid(art){
  if(!window.mermaid||!art)return;
  const th=darkNow()?'dark':'default';
  if(th!==mmTheme){
    mmTheme=th;
    mermaid.initialize({startOnLoad:false,theme:th,securityLevel:'strict',fontFamily:getComputedStyle(document.body).fontFamily});
    $$('pre.mermaid[data-processed]').forEach(el=>{el.removeAttribute('data-processed');el.textContent=el.dataset.src});
  }
  const nodes=[...art.querySelectorAll('pre.mermaid:not([data-processed])')];
  if(nodes.length)try{await mermaid.run({nodes})}catch(e){console.warn(e)}
}
new MutationObserver(()=>renderMermaid($('#v'+cur))).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>renderMermaid($('#v'+cur)));
show();
"""
JS = (JS.replace("%ORDER%", js_json(order)).replace("%T%", js_json(nav_titles)).replace("%UI%", js_json(UI))
      .replace("%SITE%", js_json(site_title)) + theme_toggle.body_script("docs-theme", UI["to_light"], UI["to_dark"]))

def vendor_html():
    """公式和 mermaid 用到时才把 KaTeX / mermaid 内嵌进来，没用到的页面不变大。"""
    out = []

    def rd(n):
        with open(os.path.join(VENDOR, n), encoding="utf-8") as f:
            return f.read().replace("</script", "<\\/script")
    if "math" in USED:
        out.append(f"<style>{rd('katex.min.css')}</style><script>{rd('katex.min.js')}</script>")
    if "mermaid" in USED:
        out.append(f"<script>{rd('mermaid.min.js')}</script>")
    return "\n".join(out) + ("\n" if out else "")


SEARCH_ICON = ('<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" '
               'stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>')
MENU_ICON = ('<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>')

page = f'''<!doctype html>
<html lang="{UI['lang']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{html.escape(site_title)}</title>
<meta name="claude:viewport-fit-sides" content="cover">
{theme_toggle.head_script("docs-theme")}
<style>{CSS}</style>
</head>
<body{' class="single"' if len(order) == 1 else ''}>
<div id="bar"></div>
<div class="topbar"><button class="menu" id="menu" type="button" aria-label="{UI['open_toc']}">{MENU_ICON}{UI['toc']}</button>
<div class="t">{html.escape(site_title)}</div></div>
<div class="wrap">
<aside class="side" id="side">
<div class="brandrow"><div class="brand">{html.escape(site_title)}</div>{'' if len(order) == 1 else theme_toggle.button(UI['dark'])}</div>
<label class="search">{SEARCH_ICON}<input id="q" type="search" placeholder="{UI['search']}" aria-label="{UI['search']}" autocomplete="off"><kbd>/</kbd></label>
<div class="sres" id="sres" aria-live="polite"></div>
<nav class="navlist">{"".join(nav)}</nav>
</aside>
<div class="scrim" id="scrim"></div>
<main>{"".join(sections)}</main>
{'<div class="corner">' + theme_toggle.button(UI['dark']) + '</div>' if len(order) == 1 else ''}
<nav class="toc" id="toc" aria-label="{UI['in_doc']}"></nav>
</div>
<div class="lb" id="lb" hidden role="dialog" aria-modal="true" aria-label="{UI['viewer']}"><img id="lbimg" alt="" draggable="false">
<div class="lb-bar"><span class="cap" id="lbcap"></span><button id="lbout" type="button" aria-label="{UI['zoom_out']}" title="{UI['zoom_out']} (-)">−</button>
<span class="pct" id="lbpct"></span><button id="lbin" type="button" aria-label="{UI['zoom_in']}" title="{UI['zoom_in']} (+)">+</button>
<button id="lbfit" type="button" title="{UI['fit_t']} (0)">{UI['fit']}</button><button id="lbclose" type="button" aria-label="{UI['close']}" title="{UI['close']} (Esc)"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div></div>
{vendor_html()}<script>{JS}</script>
</body>
</html>
'''
os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(page)
print(OUT, len(page)//1024, "KB", len(docs), "docs")
print("unresolved local links:", len(unresolved))
for u in unresolved:
    print("  ", u)
main_html = "".join(sections)   # 只查正文，内嵌的脚本库里也可能有 ![ 这样的字符
print("raw img refs:", len(re.findall(r'!\[', re.sub(r"<pre\b.*?</pre>|<code>.*?</code>", "", main_html, flags=re.S))))
for w in warnings:
    print("warning:", w)
print("warnings:", len(warnings))
