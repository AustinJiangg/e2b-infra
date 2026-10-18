// Render a standalone SVG with D3. JSON data comes from the article's table.
import * as d3 from './node_modules/d3/src/index.js';
import { JSDOM } from 'jsdom';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const rows = JSON.parse(input);
const dom = new JSDOM('<!doctype html><body></body>');
const svg = d3.select(dom.window.document.body).append('svg')
  .attr('xmlns', 'http://www.w3.org/2000/svg').attr('viewBox', '0 0 800 470')
  .attr('role', 'img').attr('aria-labelledby', 'chart-title chart-desc');
svg.append('title').attr('id', 'chart-title').text('950 混合负载：增量保存与原地恢复耗时');
svg.append('desc').attr('id', 'chart-desc').text('横轴为名义改动量 MiB，纵轴为客户端耗时中位数 ms。' +
  rows.map(r => r.mb + ' MiB：保存 ' + r.checkpoint + ' ms，恢复 ' + r.restore + ' ms').join('；'));
svg.append('rect').attr('width', 800).attr('height', 470).attr('fill', '#fafaf7');
const root = svg.append('g').attr('font-family', 'Noto Sans CJK SC, Microsoft YaHei, sans-serif');
root.append('text').attr('x', 46).attr('y', 40).attr('font-size', 22).attr('font-weight', 700)
  .attr('fill', '#202e37').text('改动越多，处理时间越长');
root.append('text').attr('x', 46).attr('y', 68).attr('font-size', 14).attr('fill', '#52636d')
  .text('950 · HDBSS 自报 · ext4 · 客户端 p50 · 2026-10-13');
const x = d3.scaleLinear().domain([0, 512]).range([78, 734]);
const y = d3.scaleLinear().domain([0, 260]).range([367, 116]);
root.append('g').attr('transform', 'translate(78,0)').call(d3.axisLeft(y).tickValues([0, 50, 100, 150, 200, 250])
  .tickSize(-656).tickPadding(12)).call(g => g.select('.domain').remove())
  .call(g => g.selectAll('.tick line').attr('stroke', '#dde3e1'))
  .call(g => g.selectAll('.tick text').attr('font-size', 13).attr('fill', '#52636d'));
root.append('g').attr('transform', 'translate(0,367)').call(d3.axisBottom(x).tickValues([0, 64, 128, 256, 512])
  .tickSize(0).tickPadding(12)).call(g => g.select('.domain').attr('stroke', '#9daeb4'))
  .call(g => g.selectAll('.tick text').attr('font-size', 13).attr('fill', '#52636d'));
root.append('text').attr('x', 46).attr('y', 105).attr('font-size', 13).attr('fill', '#52636d').text('ms');
root.append('text').attr('x', 734).attr('y', 414).attr('font-size', 14).attr('text-anchor', 'end')
  .attr('fill', '#52636d').text('名义改动量（MiB）');
for (const [key, label, color, dash, legendX] of [
  ['checkpoint', '增量保存', '#22665c', null, 440],
  ['restore', '原地恢复', '#315f9a', '6 4', 585],
]) {
  root.append('path').datum(rows).attr('d', d3.line().x(d => x(d.mb)).y(d => y(d[key])))
    .attr('fill', 'none').attr('stroke', color).attr('stroke-width', 3).attr('stroke-dasharray', dash);
  root.append('g').selectAll('circle').data(rows).join('circle').attr('cx', d => x(d.mb))
    .attr('cy', d => y(d[key])).attr('r', 4.5).attr('fill', '#fafaf7').attr('stroke', color).attr('stroke-width', 2.5);
  root.append('line').attr('x1', legendX).attr('x2', legendX + 28).attr('y1', 94).attr('y2', 94)
    .attr('stroke', color).attr('stroke-width', 3).attr('stroke-dasharray', dash);
  root.append('text').attr('x', legendX + 36).attr('y', 99).attr('font-size', 14).attr('fill', color).text(label);
  for (const mb of [64, 256, 512]) {
    const row = rows.find(r => r.mb === mb);
    root.append('text').attr('x', x(mb)).attr('y', y(row[key]) + (key === 'checkpoint' ? -13 : 23))
      .attr('text-anchor', 'middle').attr('font-size', 13).attr('font-weight', 600).attr('fill', color).text(row[key].toFixed(1));
  }
}
root.append('text').attr('x', 46).attr('y', 451).attr('font-size', 12).attr('fill', '#52636d')
  .text('每档 n = 10～30；折线仅连接实测点。首次全量另计，最大值及完整口径见正文。');
process.stdout.write(svg.node().outerHTML + '\n');
