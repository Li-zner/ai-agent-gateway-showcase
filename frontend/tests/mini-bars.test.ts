import { describe, expect, it } from 'vitest'
import { chartifyReport } from '../src/utils/miniBars'

function table(rows: string[][]): string {
  return (
    `<table><thead><tr><th>渠道</th><th>GMV</th></tr></thead><tbody>` +
    rows.map((r) => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join('') +
    `</tbody></table>`
  )
}

describe('chartifyReport 统计图注入', () => {
  it('两列数值表前插入条形图，按最大值归一', () => {
    const out = chartifyReport(table([['app', '100'], ['web', '50']]))
    expect(out.startsWith('<div class="mini-chart">')).toBe(true)
    expect(out).toContain('width:100%')
    expect(out).toContain('width:50%')
    expect(out).toContain('<table')
    expect(out.indexOf('mini-chart')).toBeLessThan(out.indexOf('<table'))
  })

  it('非数值第二列与多于两列不注入', () => {
    expect(chartifyReport(table([['a', 'x'], ['b', 'y']]))).not.toContain('mini-chart')
    expect(
      chartifyReport('<table><tr><td>1</td><td>2</td><td>3</td></tr></table>'),
    ).not.toContain('mini-chart')
    expect(chartifyReport('<p>no table</p>')).toBe('<p>no table</p>')
    expect(chartifyReport('')).toBe('')
  })

  it('千分位可解析；行数超 12 不注入（长表看表格）', () => {
    expect(chartifyReport(table([['a', '1,200'], ['b', '600']]))).toContain('width:100%')
    const many = Array.from({ length: 13 }, (_, i) => [String(i), String(i + 1)])
    expect(chartifyReport(table(many))).not.toContain('mini-chart')
  })
})
