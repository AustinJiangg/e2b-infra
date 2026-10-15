#!/usr/bin/env python3
"""检查 Markdown 相对链接和标题锚点；缺文件或锚点均返回非零退出码。

用法：python3 mdlinks.py <目录或文件...>
"""
import re, os, sys, glob

def slug(t):
    t = t.strip().lower()
    t = re.sub(r'<[^>]+>', '', t)
    t = re.sub(r'`([^`]*)`', r'\1', t)
    t = re.sub(r'\*\*?([^*]*)\*\*?', r'\1', t)
    t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'[^\w一-鿿\- ]', '', t)
    return t.replace(' ', '-')

args = sys.argv[1:] or ['.']
files = []
for a in args:
    files += sorted(glob.glob(os.path.join(a, '*.md'))) if os.path.isdir(a) else [a]

if not files:
    sys.exit('没有找到待检查的 Markdown 文件')

anchors = {}
def headings(f):
    key = os.path.abspath(f)
    if key in anchors:
        return anchors[key]
    s = set()
    for line in open(f, encoding='utf-8'):
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            s.add(slug(m.group(2)))
    anchors[key] = s
    return s

for f in files:
    headings(f)

bad = missing = 0
for f in files:
    txt = open(f, encoding='utf-8').read()
    txt = re.sub(r'```.*?```', '', txt, flags=re.S)   # fenced code
    txt = re.sub(r'`[^`\n]*`', '', txt)              # inline code
    for label, target in re.findall(r'\[([^\]]+)\]\(([^)]+)\)', txt):
        if target.startswith(('http', 'mailto:')):
            continue
        path, _, frag = target.partition('#')
        if path == '':
            if frag and frag not in headings(f):
                print(f'ANCHOR-MISS  {f}  ->  #{frag}   [{label}]'); bad += 1
        else:
            full = os.path.normpath(os.path.join(os.path.dirname(f) or '.', path))
            if not os.path.exists(full):
                print(f'MISSING-FILE {f}  ->  {path}'); missing += 1
            elif frag and full.endswith('.md') and frag not in headings(full):
                print(f'ANCHOR-MISS  {f}  ->  {path}#{frag}'); bad += 1
print('---')
print(f'anchor/link errors: {bad}   not-yet-written targets: {missing}')
sys.exit(1 if bad or missing else 0)
