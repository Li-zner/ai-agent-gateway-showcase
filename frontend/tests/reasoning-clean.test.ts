// cleanReasoning 跨块清洗回归（2026-09-10 凌晨真实泄露同源缺陷）：
// 逐块清洗时指纹被块边界切开（query_ho|tel）即整体绕过；store 侧已改为
// 累积原文后整体重算，本文件钉死「逐块漏出 / 累积拦截」的对照形状
import { describe, expect, it } from 'vitest'
import { cleanReasoning } from '../src/utils/reasoning'

const LEAK_LINE = '- query_hotel：日照住宿\n'

describe('cleanReasoning 跨块清洗', () => {
  it('逐块清洗会漏出裸工具名（旧缺陷形状，防回归样本）', () => {
    const glued =
      cleanReasoning(LEAK_LINE.slice(0, 6)) + cleanReasoning(LEAK_LINE.slice(6))
    expect(glued).toContain('query_hotel')
  })

  it('累积后整体清洗：工具名替换为可读名，代码名不再外露', () => {
    const cleaned = cleanReasoning(LEAK_LINE + LEAK_LINE)
    expect(cleaned).not.toContain('query_hotel')
  })

  it('正常思考行不受清洗影响', () => {
    expect(cleanReasoning('先给结论再展开细节\n')).toContain('先给结论再展开细节')
  })

  it('剥除模型控制标记（ASCII 与全角竖线），保留其间可见正文', () => {
    const cleaned = cleanReasoning('<|DSML|>invoke<|/DSML|>参数\n<｜DSML｜>call<｜/DSML｜>收尾')
    expect(cleaned).not.toContain('<|')
    expect(cleaned).not.toContain('<｜')
    expect(cleaned).not.toContain('DSML')
    expect(cleaned).toContain('invoke')
    expect(cleaned).toContain('收尾')
  })
})
