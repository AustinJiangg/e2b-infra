// 逐页检查有没有内容被画布裁掉。
//
// 为什么需要它：slidev 的画布是固定 980×552，超出部分被 overflow:hidden 直接切掉，
// 不报错、不滚动、编辑时也看不出来 —— 只有翻到那一页才发现表格少了两行。
// 这个脚本把每一页的子元素边界和画布比一遍，把越界的量和元素打出来。
//
// 用法（先另开一个终端跑 npm run dev）：
//   npm run check
//
// 依赖 headless chromium。WSL 上系统缺 libnspr4 等库，用 .local-libs 里的那份：
//   LD_LIBRARY_PATH=$PWD/.local-libs/usr/lib/x86_64-linux-gnu npm run check

import { chromium } from 'playwright-chromium'

const PORT = process.env.PORT || 3030
const PAGES = Number(process.env.PAGES || 10)
const SHOTS = process.env.SHOTS || ''

const browser = await chromium.launch({ channel: 'chromium' })
const page = await browser.newPage({ viewport: { width: 1960, height: 1105 } })
let bad = 0

for (let i = 1; i <= PAGES; i++) {
  await page.goto(`http://127.0.0.1:${PORT}/${i}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)

  const m = await page.evaluate(() => {
    // 页面里同时挂着上一页/当前页/下一页三个 .slidev-page，只有当前页有尺寸
    const slide = [...document.querySelectorAll('.slidev-page')].find(e => e.offsetHeight > 0)
    if (!slide) return { err: '找不到当前页' }
    const r = slide.getBoundingClientRect()
    const scale = r.height / slide.offsetHeight        // slidev 用 transform 缩放到视口
    const H = slide.offsetHeight, W = slide.offsetWidth
    let over = 0, who = []
    for (const el of slide.querySelectorAll('*')) {
      const b = el.getBoundingClientRect()
      if (!b.height) continue
      const bottom = (b.bottom - r.top) / scale
      const right = (b.right - r.left) / scale
      if (bottom > H + 1 || right > W + 1) {
        over = Math.max(over, bottom - H, right - W)
        who.push({ tag: el.tagName.toLowerCase(),
                   txt: (el.textContent || '').trim().slice(0, 40),
                   down: Math.round(bottom - H), rightOver: Math.round(right - W) })
      }
    }
    return { W: Math.round(W), H: Math.round(H), over: Math.round(over), who: who.slice(-3) }
  })

  if (m.err) { console.log(`第 ${i} 页  ${m.err}`); bad++; continue }
  if (m.over > 0) {
    bad++
    console.log(`第 ${String(i).padStart(2)} 页  ✗ 溢出 ${m.over}px（画布 ${m.W}×${m.H}）`)
    for (const c of m.who)
      console.log(`          ↳ <${c.tag}> 下 +${c.down}px 右 +${c.rightOver}px  "${c.txt}"`)
  } else {
    console.log(`第 ${String(i).padStart(2)} 页  ✓`)
  }
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/slide-${String(i).padStart(2, '0')}.png` })
}

await browser.close()
console.log(bad ? `\n${bad} 页有内容被裁掉。` : '\n全部页面内容完整。')
process.exit(bad ? 1 : 0)
