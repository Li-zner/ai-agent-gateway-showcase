/**
 * 统计图（外部建议 #8：直方图/常规统计图）——零依赖 CSS 条形图。
 *
 * 在渲染后的报告 HTML 里找「两列且第二列可解析为数值」的表格，在表格前
 * 插入条形图（标签 + 按最大绝对值归一的宽度条 + 原值），表格本身保留。
 * 纯字符串变换便于单测；超过 12 行不注入（长表看表格即可，ponytail：够用）。
 * 注入的文本来自已渲染的 markdown 表格单元格（去标签留实体），不引入新攻击面。
 */
export function chartifyReport(html: string): string {
  if (!html || !/<table/i.test(html)) return html
  return html.replace(/<table[\s\S]*?<\/table>/gi, (table) => {
    const rows = [...table.matchAll(/<tr[\s\S]*?<\/tr>/gi)].map((m) => m[0])
    const body = rows.filter((r) => !/<th[\s>]/i.test(r))
    if (body.length < 2 || body.length > 12) return table
    const cells = (row: string): string[] =>
      [...row.matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi)].map((m) =>
        m[1].replace(/<[^>]+>/g, '').trim(),
      )
    const parsed = body.map(cells)
    if (!parsed.every((c) => c.length === 2)) return table
    const nums = parsed.map((c) => Number(c[1].replace(/,/g, '')))
    if (!nums.every((n) => Number.isFinite(n))) return table
    const max = Math.max(...nums.map((n) => Math.abs(n))) || 1
    const bars = parsed
      .map(([label, raw], i) => {
        const pct = Math.max(2, Math.round((Math.abs(nums[i]) / max) * 100))
        return (
          `<div class="mini-bar">` +
          `<span class="mini-bar-label">${label}</span>` +
          `<span class="mini-bar-track"><span class="mini-bar-fill" style="width:${pct}%"></span></span>` +
          `<span class="mini-bar-value">${raw}</span>` +
          `</div>`
        )
      })
      .join('')
    return `<div class="mini-chart">${bars}</div>${table}`
  })
}
