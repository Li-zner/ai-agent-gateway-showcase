/** 数据监控卡片口径：三态样式、陈旧标记与数值/规则格式化。 */
import { describe, expect, it } from 'vitest'
import {
  formatMonitorRule,
  formatMonitorValue,
  isMonitorStale,
  monitorStatusClass,
} from '../src/utils/dbagentMonitor'

describe('isMonitorStale', () => {
  const base = Date.parse('2026-11-11T12:00:00Z')

  it('两倍间隔内不算陈旧，超过即标陈旧', () => {
    const at = (secondsAgo: number) => new Date(base - secondsAgo * 1000).toISOString()
    expect(isMonitorStale({ last_ok_at: at(119), interval_sec: 60 }, base)).toBe(false)
    expect(isMonitorStale({ last_ok_at: at(121), interval_sec: 60 }, base)).toBe(true)
  })

  it('从未成功过不算陈旧（另显示从未成功），无效时间也不标', () => {
    expect(isMonitorStale({ last_ok_at: null, interval_sec: 60 }, base)).toBe(false)
    expect(isMonitorStale({ last_ok_at: 'not-a-time', interval_sec: 60 }, base)).toBe(false)
  })

  it('间隔越大容忍越久', () => {
    const at = (secondsAgo: number) => new Date(base - secondsAgo * 1000).toISOString()
    expect(isMonitorStale({ last_ok_at: at(300), interval_sec: 60 }, base)).toBe(true)
    expect(isMonitorStale({ last_ok_at: at(300), interval_sec: 600 }, base)).toBe(false)
  })
})

describe('formatMonitorValue', () => {
  it('空值给破折号，不伪造 0', () => {
    expect(formatMonitorValue(null)).toBe('—')
    expect(formatMonitorValue(undefined, '单')).toBe('—')
  })

  it('整数千分位、小数两位，单位跟在后面', () => {
    expect(formatMonitorValue(1234, '单')).toBe('1,234 单')
    expect(formatMonitorValue(12.5, 'ms')).toBe('12.50 ms')
    expect(formatMonitorValue(0)).toBe('0')
  })
})

describe('formatMonitorRule / monitorStatusClass', () => {
  it('比较符与阈值可读', () => {
    expect(formatMonitorRule('gt', 10, '单')).toBe('> 10 单')
    expect(formatMonitorRule('lte', 0.05, null)).toBe('≤ 0.05')
  })

  it('三态各自映射到独立样式，不把断采并入正常', () => {
    expect(monitorStatusClass('ok')).toBe('monitor-ok')
    expect(monitorStatusClass('breach')).toBe('monitor-breach')
    expect(monitorStatusClass('unknown')).toBe('monitor-unknown')
  })
})
