"""theme_toggle：单文件 HTML 用的深浅色切换按钮（只有图标，没有文字）。纯标准库，无第三方依赖。

页面要做两件事：
  1. 配色写成 CSS 变量，深色在 prefers-color-scheme 和 [data-theme="dark"] 下各定义一次；
  2. 把下面四段放进页面：head_script(key) 放 <head>，CSS 放样式表，BUTTON（或 button(label)，非中文界面用）放按钮的位置，
     body_script(key[, to_light, to_dark]) 放在按钮之后。key 是 localStorage 的键名，每个站点用自己的。

行为：
  - 按钮显示点击后会切到的主题：浅色时是月亮，深色时是太阳。
  - 没有保存过选择时跟随系统，系统切换时图标同步更新。
  - 主题在 <head> 里同步设置，保存的选择和系统不同时，页面不会先闪一下错的配色。
  - 读屏软件读到固定名称「深色模式」和按下状态（aria-pressed），鼠标悬停显示「切换到…模式」。

图标路径来自 Lucide（ISC 许可）。不用 emoji：不同系统字体画出来的样子不一样，也不跟随文字颜色。
"""

_ICON = ('<svg class="{cls}" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
         'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
         '{shape}</svg>')
_MOON = '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>'
_SUN = ('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41'
        'M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>')

def button(label="深色模式"):
    """按钮的 HTML。label 是读屏软件读到的固定名称，界面不是中文时传对应语言。"""
    return ('<button class="theme-toggle" id="theme-toggle" type="button" aria-label="%s" aria-pressed="false">' % label
            + _ICON.format(cls="i-moon", shape=_MOON) + _ICON.format(cls="i-sun", shape=_SUN) + '</button>')


BUTTON = button()

CSS = """
:root[data-theme="dark"]{color-scheme:dark}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark}}
.theme-toggle{display:inline-flex;align-items:center;justify-content:center;flex:none;width:36px;height:36px;padding:0;
  background:transparent;color:var(--ink-2);border:1px solid var(--rule);border-radius:8px;cursor:pointer}
.theme-toggle:hover{background:var(--surface-2);color:var(--ink)}
.theme-toggle:focus-visible{outline:2px solid currentColor;outline-offset:2px}
.theme-toggle[aria-pressed="true"] .i-moon,.theme-toggle[aria-pressed="false"] .i-sun{display:none}
"""


def head_script(key):
    return ("<script>try{var t=localStorage.getItem('%s');"
            "if(t==='dark'||t==='light')document.documentElement.dataset.theme=t}catch(e){}</script>" % key)


def body_script(key, to_light="切换到浅色模式", to_dark="切换到深色模式"):
    return """(function(){
  var root=document.documentElement, btn=document.getElementById('theme-toggle'),
      sys=matchMedia('(prefers-color-scheme: dark)');
  function dark(){return root.dataset.theme?root.dataset.theme==='dark':sys.matches}
  function sync(){
    var d=dark();
    btn.setAttribute('aria-pressed',String(d));
    btn.title=d?'%(to_light)s':'%(to_dark)s';
  }
  btn.addEventListener('click',function(){
    var t=dark()?'light':'dark';
    root.dataset.theme=t;
    try{localStorage.setItem('%(key)s',t)}catch(e){}
    sync();
  });
  sys.addEventListener('change',sync);
  sync();
})();
""" % {"key": key, "to_light": to_light, "to_dark": to_dark}
