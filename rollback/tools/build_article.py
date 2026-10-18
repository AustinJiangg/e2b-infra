#!/usr/bin/env python3
"""Build the standalone value article and its SVG figures from local sources.

Uses the handbook's existing markdown-it-py and Node dependencies.
Run: python3 rollback/tools/build_article.py
"""
import base64
import html
import json
import re
import subprocess
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
ARTICLE = ROOT / 'articles'
ASSETS = ARTICLE / 'assets'
OUT = ROOT / 'dist' / 'sandbox-rollback-article.html'
FONT = 'Noto Sans CJK SC, Microsoft YaHei, sans-serif'


def text(x, y, value, size=18, color='#25333b', weight=400, anchor='start'):
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{html.escape(value)}</text>')


def box(x, y, w, h, title, detail, accent=False):
    fill, stroke = ('#e8f2ec', '#88b5a5') if accent else ('#ffffff', '#d2dcd7')
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
            f'fill="{fill}" stroke="{stroke}"/>' +
            text(x + 18, y + 33, title, 19, weight=650) +
            text(x + 18, y + 61, detail, 14, '#53665e'))


def arrow(d, dashed=False):
    dash = 'stroke-dasharray="6 5" ' if dashed else ''
    return (f'<path d="{d}" fill="none" stroke="#657c72" stroke-width="2" '
            f'{dash}marker-end="url(#arrow)"/>')


def svg(name, height, title, description, content):
    data = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 {height}" '
            'role="img" aria-labelledby="title desc">'
            f'<title id="title">{html.escape(title)}</title>'
            f'<desc id="desc">{html.escape(description)}</desc>'
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            '<path d="M 0 0 L 10 5 L 0 10 z" fill="#657c72"/></marker></defs>'
            f'<rect width="800" height="{height}" fill="#fafaf7"/>'
            f'<g font-family="{FONT}">{content}</g></svg>\n')
    (ASSETS / name).write_text(data, encoding='utf-8')


def figures():
    ASSETS.mkdir(parents=True, exist_ok=True)
    svg('task-flow.svg', 360, '从保存点重新尝试', '准备完成后保存 S，尝试 A 失败，恢复 S 后尝试 B。',
        text(36, 42, '保留已有进展，再选择一条路', 23, weight=700) +
        box(36, 88, 216, 86, '准备完成 · 保存 S', '依赖、服务、数据已就绪', True) +
        box(292, 88, 210, 86, '尝试 A', '修改依赖与配置') +
        box(542, 88, 222, 86, '验证失败', '当前环境已被改变') +
        arrow('M 252 131 H 286') + arrow('M 502 131 H 536') +
        arrow('M 653 174 V 213 H 144 V 180', dashed=True) +
        text(399, 204, '恢复 S', 15, '#216357', 650, 'middle') +
        arrow('M 144 174 V 275 H 286') +
        box(292, 232, 210, 86, '尝试 B', '从同一保存点开始', True) +
        arrow('M 502 275 H 536') +
        box(542, 232, 222, 86, '验证通过', '保留结果，继续推进', True))
    svg('state-boundary.svg', 422, '沙箱级恢复的边界', '沙箱内部状态共同恢复；外部控制程序、数据库、API 与消息不随之撤销。',
        text(36, 42, '保存与恢复，覆盖同一个沙箱内部', 23, weight=700) +
        '<rect x="36" y="72" width="728" height="201" rx="12" fill="#edf4ee" stroke="#88b5a5"/>' +
        text(58, 106, '同一个活沙箱 · 同一协调恢复点', 17, '#216357', 650) +
        box(58, 131, 212, 100, '内存与进程', '变量、任务、内核缓存') +
        box(294, 131, 212, 100, 'CPU 与设备', '执行与设备逻辑状态') +
        box(530, 131, 212, 100, '根文件系统', '依赖、配置、文件') +
        text(58, 255, '操作期间短暂冻结；跨恢复连接需要重建', 14, '#53665e') +
        '<path d="M 36 301 H 764" stroke="#a8b8b0" stroke-dasharray="5 5"/>' +
        text(36, 332, '外部世界', 17, weight=650) +
        text(174, 332, 'Agent 控制程序　／　外部数据库　／　API 与消息', 17) +
        text(174, 366, '已发生的变化仍然存在，由业务协调记录、去重或补偿。', 15, '#53665e'))
    svg('incremental-restore.svg', 468, '增量保存与活体原地恢复', '完整基点与后续差分组成历史，计算回滚集并在原虚拟机内恢复。',
        text(36, 42, '尽量只处理本次变化，复用活沙箱资源', 23, weight=700) +
        box(36, 80, 216, 88, '首次保存 · S', '完整内存基点 + 磁盘视图', True) +
        box(292, 80, 216, 88, '后续保存 · A', '内存差分 + 封存磁盘层') +
        box(548, 80, 216, 88, '当前运行状态', '还可能有未保存的改动') +
        arrow('M 252 123 H 286') + arrow('M 508 123 H 542') +
        arrow('M 144 168 V 200 H 400 V 224', dashed=True) +
        arrow('M 400 168 V 224', dashed=True) +
        arrow('M 656 168 V 200 H 400', dashed=True) +
        '<rect x="212" y="230" width="376" height="73" rx="10" fill="#ffffff" stroke="#d2dcd7"/>' +
        text(400, 261, '计算从当前状态回到 S 的回滚集', 18, weight=650, anchor='middle') +
        text(400, 286, '按历史取得目标页，准备目标磁盘视图', 14, '#53665e', anchor='middle') +
        arrow('M 400 303 V 330') +
        '<rect x="36" y="336" width="728" height="98" rx="12" fill="#e8f2ec" stroke="#88b5a5"/>' +
        text(400, 369, '在同一个 Firecracker 进程中恢复', 21, '#216357', 650, 'middle') +
        text(400, 401, '协调暂停 → 切换磁盘视图、写回内存与执行状态 → 继续运行', 16, anchor='middle'))


def build():
    source = (ARTICLE / 'sandbox-rollback.md').read_text(encoding='utf-8')
    figures()
    rows = []
    for match in re.finditer(r'^\| (\d+) MiB \| (\d+) \| ([\d.]+) / ([\d.]+) ms \| ([\d.]+) / ([\d.]+) ms \|$', source, re.M):
        mb, n, ck, ckmax, rs, rsmax = match.groups()
        rows.append(dict(mb=int(mb), n=int(n), checkpoint=float(ck), restore=float(rs),
                         checkpointMax=float(ckmax), restoreMax=float(rsmax)))
    if len(rows) != 5:
        raise ValueError('Expected five measured performance rows in the article')
    chart = subprocess.run(['node', str(ROOT / 'tools/build_article_chart.mjs')],
                           input=json.dumps(rows), text=True, capture_output=True, check=True).stdout
    (ASSETS / 'performance.svg').write_text(chart, encoding='utf-8')
    md = MarkdownIt('commonmark', {'html': False}).enable('table')
    tokens = md.parse(source)
    section = 0
    for token in tokens:
        if token.type == 'heading_open' and token.tag == 'h2':
            section += 1
            token.attrSet('id', f'section-{section}')
        if token.children:
            for index, child in enumerate(token.children):
                if child.type == 'link_open':
                    target = child.attrGet('href')
                    if target and target.startswith('../docs/'):
                        name = Path(target).stem
                        if not (ROOT / 'docs' / f'{name}.md').is_file():
                            raise ValueError(f'Missing source: {name}')
                        label = token.children[index + 1].content
                        if label.isdigit():
                            child.attrSet('href', f'#reference-{label}')
                        else:
                            child.attrSet('href', f'checkpoint-restore-docs.html#doc--{name}')
                if child.type == 'image':
                    path = ARTICLE / child.attrGet('src')
                    child.attrSet('src', 'data:image/svg+xml;base64,' + base64.b64encode(path.read_bytes()).decode())
    body = md.renderer.render(tokens, md.options, {})
    body = body.replace('<h1>让 AI Agent 可以回到上一步</h1>',
                        '<h1><span class="title-group">让 AI Agent</span> <span class="title-group">可以回到上一步</span></h1>', 1)
    subtitle = '<p>E2B 沙箱级快照回滚的设计与价值</p>'
    body = body.replace(subtitle, '<p class="subtitle">E2B 沙箱级快照回滚的设计与价值</p>', 1)
    count = len(re.findall(r'[\u4e00-\u9fff]', source))
    body = re.sub(r'<blockquote>\s*<p>(.*?)</p>\s*</blockquote>',
                  lambda m: f'<p class="byline">{m[1]} · 约 {round(count / 100) * 100:,} 字</p>\n<div class="intro">', body, count=1, flags=re.S)
    body = body.replace('<div class="intro">\n<p>', '<div class="intro">\n<p class="lead">', 1)
    body = body.replace('<h2 id="section-1">', '</div>\n<h2 id="section-1">', 1)
    body = re.sub(r'<p>(<img [^>]+>)</p>\s*<p><em>(.*?)</em></p>',
                  lambda m: '<figure><div class="figure-scroll" tabindex="0" role="region" aria-label="配图，可横向滚动">' + m[1] +
                  '</div><span class="mobile-hint" aria-hidden="true">左右滑动查看完整配图</span><figcaption>' + m[2] + '</figcaption></figure>', body, flags=re.S)
    body = body.replace('<table>', '<div class="table-scroll" tabindex="0" role="region" aria-label="数据表，可横向滚动"><table>')
    body = body.replace('</table>', '</table></div>')
    body = body.replace(f'<h2 id="section-{section}">', f'<section class="sources"><h2 id="section-{section}">', 1) + '</section>'
    before_sources, sources = body.split('<section class="sources">', 1)
    reference = 0
    def mark_reference(match):
        nonlocal reference
        reference += 1
        return f'<li id="reference-{reference}">'
    sources = re.sub(r'<li>', mark_reference, sources)
    body = before_sources + '<section class="sources">' + sources
    css = (ARTICLE / 'article.css').read_text(encoding='utf-8')
    head_init = """(()=>{let t;try{t=localStorage.getItem('e2b-article-theme')}catch{}if(t!=='dark'&&t!=='light')t=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';document.documentElement.dataset.theme=t})()"""
    theme_js = """(()=>{const b=document.querySelector('.theme-toggle');function sync(){const d=document.documentElement.dataset.theme==='dark';b.setAttribute('aria-label',d?'切换为浅色模式':'切换为深色模式');b.title=b.getAttribute('aria-label');b.setAttribute('aria-pressed',String(d))}b.addEventListener('click',()=>{const t=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=t;try{localStorage.setItem('e2b-article-theme',t)}catch{}sync()});sync()})()"""
    output = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>让 AI Agent 可以回到上一步 · E2B 沙箱级快照回滚的设计与价值</title>
<meta name="description" content="从业务场景、核心机制与实测结果理解 E2B 沙箱级快照回滚的价值与适用范围。">
<script>{head_init}</script><style>{css}</style></head><body>
<header class="topbar"><span class="brand">E2B / 技术解读</span><button type="button" class="theme-toggle" aria-label="切换配色">
<svg class="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14A8.5 8.5 0 0 1 10 3.5 8.5 8.5 0 1 0 20.5 14Z"/></svg>
<svg class="sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.4 1.4m11.2 11.2L19 19M5 19l1.4-1.4M17.6 6.4 19 5"/></svg>
</button></header><main>{body}<p class="footer">E2B 沙箱级快照回滚 · 技术解读 · v0.1.0</p></main><script>{theme_js}</script></body></html>'''
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(output, encoding='utf-8')
    print(f'Article: {OUT}\nChinese characters: {count}; performance rows: {len(rows)}; figures: 4; bytes: {len(output.encode())}')


if __name__ == '__main__':
    build()
