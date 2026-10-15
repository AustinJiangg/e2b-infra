// 从 Markdown 文件里取出 ```mermaid 代码块，逐个用 mermaid.parse 校验语法。
//   node mdmermaid.mjs [--node-modules <目录>] <a.md> <b.md> ...
// 退出码：0 = 全部通过；1 = 有图解析失败或代码块没有闭合；2 = jsdom / mermaid 装载不了，校验没有执行。
import fs from 'node:fs'
import { loadMermaid } from './mermaid_env.mjs'
const { mermaid, files } = await loadMermaid(process.argv.slice(2))
if (files.length === 0) { console.error('用法: node mdmermaid.mjs [--node-modules <目录>] <a.md> ...'); process.exit(2) }
let bad = 0, total = 0
for (const f of files) {
  const lines = fs.readFileSync(f, 'utf8').split('\n')
  let cur = null, start = 0, n = 0
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    if (cur === null && /^\s*```mermaid\s*$/.test(l)) { cur = []; start = i + 1; continue }
    if (cur !== null && /^\s*```\s*$/.test(l)) {
      n++
      try { await mermaid.parse(cur.join('\n')); console.log(`OK    ${f}:${start} (block ${n})`) }
      catch (e) { bad++; console.log(`FAIL  ${f}:${start} (block ${n})\n      ${String(e.message).slice(0, 400)}`) }
      cur = null; continue
    }
    if (cur !== null) cur.push(l)
  }
  if (cur !== null) { bad++; console.log(`FAIL  ${f}: unterminated mermaid fence opened at line ${start}`) }
  if (n === 0) console.log(`----  ${f}: no mermaid blocks`)
  total += n
}
console.log(`合计 ${total} 张图，失败 ${bad}`)
process.exit(bad ? 1 : 0)
