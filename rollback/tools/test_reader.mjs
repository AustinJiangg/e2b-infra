#!/usr/bin/env node
// 在离线 DOM 中执行产物内的实际阅读脚本，验证目录、跳转与图表查看交互。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { JSDOM } from 'jsdom';

if (process.argv.length !== 3) {
  console.error('用法：node rollback/tools/test_reader.mjs 输出.html');
  process.exit(2);
}
const target = path.resolve(process.argv[2]);
const html = fs.readFileSync(target, 'utf8');
const dom = new JSDOM(html, {
  url: pathToFileURL(target).href, runScripts: 'outside-only', pretendToBeVisual: true,
});
const { window } = dom;
const document = window.document;
window.scrollTo = () => {};
// jsdom 不做布局，也不实现 dialog / pointer capture；几何与浏览器原生方法在此补齐。
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function () {
  this.open = false; this.dispatchEvent(new window.Event('close'));
};
const viewport = document.getElementById('diagram-viewport');
Object.defineProperties(viewport, { clientWidth: { value: 1200 }, clientHeight: { value: 800 } });
viewport.getBoundingClientRect = () => ({ left: 20, top: 100, width: 1200, height: 800 });
const captured = new Set();
const captureTarget = document.getElementById('diagram-canvas');
captureTarget.setPointerCapture = id => { captured.add(id); };
captureTarget.hasPointerCapture = id => captured.has(id);
captureTarget.releasePointerCapture = id => { captured.delete(id); };
// 图表语法由 mdmermaid 单独检查；这里专门执行产物的导航与阅读代码。
window.mermaid = { initialize() {} };
const scripts = [...document.querySelectorAll('script:not([type="application/json"])')];
window.eval(document.getElementById('theme-init').textContent);
document.querySelectorAll('section.doc').forEach((section,index) => {
  Object.defineProperty(section,'offsetTop',{value:index*1000});
});
window.eval(scripts.at(-1).textContent);

// file:// 下存储可能不可用；切换仍须生效，持久化失败不能影响其他阅读交互。
const themeToggle = document.getElementById('theme-toggle');
assert.equal(themeToggle.textContent.trim(), '', '外观按钮只显示图标');
assert.equal(themeToggle.parentElement, document.body, '外观按钮独立于目录和搜索');
assert.equal(document.documentElement.dataset.theme, 'light');
assert.equal(themeToggle.getAttribute('aria-pressed'), 'false');
themeToggle.click();
assert.equal(document.documentElement.dataset.theme, 'dark');
assert.equal(themeToggle.getAttribute('aria-pressed'), 'true');
assert.equal(themeToggle.getAttribute('aria-label'), '切换到日间模式');
themeToggle.click();
assert.equal(document.documentElement.dataset.theme, 'light');
assert.equal(themeToggle.getAttribute('aria-pressed'), 'false');
assert.equal(themeToggle.getAttribute('aria-label'), '切换到夜间模式');

// 使用可用存储验证重新打开后恢复夜间选择，并检查非法值回退。
const savedThemeDom = new JSDOM('<!doctype html><html data-theme="light"><body></body></html>', {
  url: 'https://handbook.example/', runScripts: 'outside-only',
});
const savedWindow = savedThemeDom.window;
for (const saved of ['dark', 'light', 'invalid']) {
  savedWindow.document.documentElement.dataset.theme = 'light';
  savedWindow.localStorage.setItem('e2b-handbook-theme', saved);
  savedWindow.eval(document.getElementById('theme-init').textContent);
  assert.equal(savedWindow.document.documentElement.dataset.theme, saved === 'dark' ? 'dark' : 'light');
}
// 给离线阅读页接入同一存储，验证按钮实际保存所选模式。
Object.defineProperty(window, 'localStorage', { value: savedWindow.localStorage, configurable: true });
themeToggle.click();
assert.equal(savedWindow.localStorage.getItem('e2b-handbook-theme'), 'dark');
themeToggle.click();
assert.equal(savedWindow.localStorage.getItem('e2b-handbook-theme'), 'light');
savedWindow.close();

const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
assert.equal(new Set(ids).size, ids.length, 'HTML 存在重复 id');
for (const a of document.querySelectorAll('a[href^="#"]')) {
  const fragment = decodeURIComponent(a.getAttribute('href').slice(1));
  assert.ok(document.getElementById(fragment), `页内目标不存在：${a.textContent} → ${fragment}`);
}
assert.equal(document.querySelectorAll('.nav-group').length, 7, '应有六篇和附录分组');
assert.equal(document.querySelectorAll('.part-title').length, 6);
assert.equal(document.getElementById('filter'), null);
assert.equal(document.getElementById('search-index'), null);
assert.equal(document.querySelector('#sidebar .sub'), null);
assert.equal(document.querySelector('main h1').textContent, 'E2B快照回滚技术手册');
assert.match(document.querySelector('main blockquote').textContent, /约 \d+\.\d 万字/);
assert.equal(document.querySelectorAll('.nav-link').length, document.querySelectorAll('section.doc').length);
assert.equal(document.querySelectorAll('.nav-link[aria-current="location"]').length, 1);
assert.equal(document.querySelector('.nav-link.active').hash, '#doc--README');
for (const button of document.querySelectorAll('.nav-expand')) {
  const target = document.getElementById(button.getAttribute('aria-controls'));
  assert.ok(target, '展开按钮须有对应小节列表');
  button.click();
  assert.equal(button.getAttribute('aria-expanded'), 'true');
  assert.equal(target.hidden, false);
  button.click();
  assert.equal(target.hidden, true);
}
const chapter = document.querySelector('section.doc[id="doc--01-delivery-and-reading"]');
Object.defineProperty(window, 'scrollY', {value:chapter.offsetTop,writable:true});
window.dispatchEvent(new window.Event('scroll'));
const current = document.querySelector('.nav-link.active');
assert.equal(current.hash, '#doc--01-delivery-and-reading');
assert.equal(current.getAttribute('aria-current'), 'location');
assert.equal(document.querySelectorAll('.nav-expand[aria-expanded="true"]').length, 1);
const directory = document.getElementById('directory');
assert.equal(directory.open, true);
window.innerWidth = 390;
window.dispatchEvent(new window.Event('resize'));
assert.equal(directory.open, false, '手机端初始收起目录');
document.getElementById('directory-toggle').click();
assert.equal(directory.open, true);
current.click();
assert.equal(directory.open, false, '点击章节后收起手机目录');
assert.equal(document.activeElement, chapter);
directory.open = true;
document.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
assert.equal(directory.open, false);
assert.equal(document.activeElement, document.getElementById('directory-toggle'));
window.innerWidth = 1440;
window.dispatchEvent(new window.Event('resize'));
assert.equal(directory.open, true, '回到桌面时显示目录');

for (const details of document.querySelectorAll('.nav-group>details')) {
  details.open = false;
  assert.equal(details.open, false);
  details.open = true;
  assert.equal(details.open, true);
}

// 放大后保留原 SVG，关闭时恢复节点、样式、焦点及正文滚动。
const viewer = document.getElementById('diagram-viewer');
const canvas = document.getElementById('diagram-canvas');
const close = document.getElementById('diagram-close');
const triggers = [...document.querySelectorAll('.diagram-trigger')];
assert.equal(triggers.length, 13);
assert.equal(document.querySelector('.diagram-header'), null, '不保留顶部面板');
assert.ok(document.getElementById('diagram-help').classList.contains('sr-only'), '快捷键说明仅供辅助技术读取');
assert.equal(document.querySelectorAll('.diagram-toolbar button').length, 3);
const trigger = document.querySelector('main figure.diagram-trigger');
const svg = trigger.querySelector('svg');
const originalStyle = svg.getAttribute('style');
function transform() {
  const values = canvas.style.transform.match(/translate\(([-.\d]+)px,([-.\d]+)px\) scale\(([-.\d]+)\)/);
  assert.ok(values, '应有平移和缩放变换');
  return { x: Number(values[1]), y: Number(values[2]), scale: Number(values[3]) };
}
function approximately(a, b, message) { assert.ok(Math.abs(a-b)<1e-8, message || `${a} ≠ ${b}`); }
function pointer(type, id, x, y, target = viewport, pointerType = 'mouse') {
  const event = new window.Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, { pointerId: id, clientX: x, clientY: y, button: 0, pointerType });
  target.dispatchEvent(event);
}
trigger.focus(); trigger.click();
assert.equal(viewer.open, true);
assert.equal(canvas.firstElementChild, svg);
assert.equal(new Set([...document.querySelectorAll('[id]')].map(n => n.id)).size, ids.length);
assert.equal(document.body.style.overflow, 'hidden');
assert.equal(document.activeElement, viewer);
assert.equal(document.getElementById('diagram-title').textContent, trigger.querySelector('figcaption').textContent);
const fitted = transform();
assert.ok(fitted.scale > 0 && fitted.scale < 1);
assert.equal(document.getElementById('diagram-scale').textContent, '100%');
const wheel = new window.WheelEvent('wheel', { deltaY: -120, clientX: 620, clientY: 476, bubbles: true, cancelable: true });
canvas.dispatchEvent(wheel);
assert.equal(wheel.defaultPrevented, true);
const zoomed = transform();
assert.ok(zoomed.scale > fitted.scale);
approximately((600-fitted.x)/fitted.scale, (600-zoomed.x)/zoomed.scale, '缩放应以鼠标为中心');
approximately((376-fitted.y)/fitted.scale, (376-zoomed.y)/zoomed.scale);
assert.ok(viewer.classList.contains('show-scale'));
pointer('pointerdown', 1, 620, 476, canvas); pointer('pointermove', 1, 660, 496);
approximately(transform().x, zoomed.x+40); approximately(transform().y, zoomed.y+20);
pointer('pointerup', 1, 660, 496);
assert.equal(captured.size, 0);
viewport.click();
assert.equal(viewer.open, true, '拖动结束的 click 不应误关预览');
close.click();
assert.equal(viewer.open, false);
assert.equal(trigger.querySelector('svg'), svg);
assert.equal(svg.getAttribute('style'), originalStyle);
assert.equal(canvas.children.length, 0);
assert.equal(document.body.style.overflow, '');
assert.equal(document.activeElement, trigger);
const afterCloseWheel = new window.WheelEvent('wheel', { deltaY: 120, bubbles: true, cancelable: true });
viewport.dispatchEvent(afterCloseWheel);
assert.equal(afterCloseWheel.defaultPrevented, false);

trigger.click();
canvas.dispatchEvent(new window.MouseEvent('dblclick', { clientX: 620, clientY: 476, bubbles: true }));
assert.ok(transform().scale > fitted.scale, '双击查看细节');
canvas.dispatchEvent(new window.MouseEvent('dblclick', { clientX: 620, clientY: 476, bubbles: true }));
assert.deepEqual(transform(), fitted, '再次双击恢复全图');
for (let i = 0; i < 30; i++) document.getElementById('diagram-zoom-in').click();
approximately(transform().scale, fitted.scale*8);
assert.equal(document.getElementById('diagram-zoom-in').disabled, true);
// 拖动边界保证图像不会丢失到屏幕外。
pointer('pointerdown', 2, 620, 476, canvas); pointer('pointermove', 2, 10000, 10000);
approximately(transform().x, 48); approximately(transform().y, 64);
pointer('pointermove', 2, -10000, -10000);
approximately(transform().x+1640*transform().scale, 1152);
approximately(transform().y+1000*transform().scale, 688);
pointer('pointercancel', 2, -10000, -10000);
assert.equal(captured.size, 0);
document.getElementById('diagram-fit').click();
assert.deepEqual(transform(), fitted);
for (let i = 0; i < 30; i++) document.getElementById('diagram-zoom-out').click();
assert.deepEqual(transform(), fitted, '最小缩放为完整显示');
assert.equal(document.getElementById('diagram-zoom-out').disabled, true);
// 双指捏合放大，松开后仍可继续用一根手指拖动。
pointer('pointerdown', 10, 500, 430, canvas, 'touch');
pointer('pointerdown', 20, 700, 430, canvas, 'touch');
pointer('pointermove', 10, 450, 430, viewport, 'touch');
pointer('pointermove', 20, 750, 430, viewport, 'touch');
approximately(transform().scale, fitted.scale*1.5);
pointer('pointerup', 20, 750, 430, viewport, 'touch');
const pinched = transform();
pointer('pointermove', 10, 470, 450, viewport, 'touch');
approximately(transform().x, pinched.x+20); approximately(transform().y, pinched.y+20);
pointer('pointerup', 10, 470, 450, viewport, 'touch');
assert.equal(captured.size, 0);
viewer.dispatchEvent(new window.KeyboardEvent('keydown', { key: '0', bubbles: true }));
assert.deepEqual(transform(), fitted);
viewer.dispatchEvent(new window.KeyboardEvent('keydown', { key: '+', bubbles: true }));
assert.ok(transform().scale > fitted.scale);
close.click();
// 图外空白覆盖整个查看区域，而非仅弹窗外的一圈。
trigger.click(); viewport.click();
assert.equal(viewer.open, false);
for (const item of triggers.filter(n => n.tagName === 'FIGURE')) {
  item.click(); assert.equal(viewer.open, true);
  viewer.dispatchEvent(new window.Event('cancel', { cancelable: true }));
  assert.equal(viewer.open, false);
}
// Mermaid 异步渲染后仍可用键盘打开，SVG 引用保持有效。
const mermaid = document.querySelector('main pre.mermaid');
mermaid.replaceChildren();
mermaid.innerHTML = '<svg viewBox="0 0 640 240" style="max-width:640px"><defs><marker id="reader-test-arrow"/></defs><path marker-end="url(#reader-test-arrow)"/></svg>';
const rendered = mermaid.querySelector('svg');
mermaid.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
assert.equal(viewer.open, true); assert.equal(canvas.firstElementChild, rendered);
assert.ok(document.getElementById('reader-test-arrow'));
window.dispatchEvent(new window.Event('beforeprint'));
assert.equal(viewer.open, false);
assert.equal(mermaid.firstElementChild, rendered);
assert.equal(rendered.getAttribute('style'), 'max-width:640px');
console.log(`离线阅读检查通过：明暗切换、选择保存与恢复、存储不可用回退；${ids.length} 个唯一 id，分组目录、独立展开、当前章高亮与手机目录收起；13张图入口、全屏预览、空白关闭、缩放、拖动边界、双击、双指手势及恢复正常。`);
window.close();
