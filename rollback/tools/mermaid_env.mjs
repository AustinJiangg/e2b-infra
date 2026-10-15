// 装载本目录安装的 jsdom + mermaid。
// 两个包不在本目录的 node_modules 里时，用 --node-modules <目录> 或环境变量 MERMAID_NODE_MODULES 指向
// 一个装有它们的 node_modules（默认使用 rollback/tools/node_modules）。
// 装载失败时打印原因并以退出码 2 结束 —— 不再"SKIP 后返回 0"，否则校验没跑也会被当成通过。
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import path from 'node:path'

export async function loadMermaid(argv) {
  let nm = process.env.MERMAID_NODE_MODULES || ''
  const rest = []
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--node-modules') nm = argv[++i] || ''
    else rest.push(argv[i])
  }
  const imp = async (name) => {
    if (!nm) return import(name)
    const req = createRequire(path.join(path.resolve(nm), '..', 'noop.js'))
    return import(pathToFileURL(req.resolve(name)).href)
  }
  try {
    const { JSDOM } = await imp('jsdom')
    const dom = new JSDOM('<!doctype html><html><body></body></html>')
    global.window = dom.window; global.document = dom.window.document
    const mermaid = (await imp('mermaid')).default
    mermaid.initialize({ startOnLoad: false })
    return { mermaid, files: rest }
  } catch (e) {
    console.error('ERROR 无法装载 jsdom / mermaid，校验没有执行:', String(e.message).split('\n')[0])
    console.error('      用 --node-modules <目录> 或 MERMAID_NODE_MODULES 指向装有这两个包的 node_modules')
    process.exit(2)
  }
}
