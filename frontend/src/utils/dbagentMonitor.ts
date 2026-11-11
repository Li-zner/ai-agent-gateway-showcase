/** 数据监控卡片展示口径：陈旧判定与数值/规则格式化（纯函数，便于单测）。 */
import type {
  DbAgentMonitor,
  DbAgentMonitorOp,
  DbAgentMonitorStatus,
} from '../api/dbagent'

/** 超过 2 倍采集间隔没有成功值，就标「数据陈旧」。 */
export const MONITOR_STALE_FACTOR = 2

const OP_SYMBOL: Record<DbAgentMonitorOp, string> = {
  gt: '>',
  gte: '≥',
  lt: '<',
  lte: '≤',
}

/** 从未成功过不算陈旧（显示「从未成功」更准确），只认最后成功时间。 */
export function isMonitorStale(
  monitor: Pick<DbAgentMonitor, 'last_ok_at' | 'interval_sec'>,
  nowMs: number = Date.now(),
): boolean {
  if (!monitor.last_ok_at) return false
  const lastOk = new Date(monitor.last_ok_at).getTime()
  if (Number.isNaN(lastOk)) return false
  return nowMs - lastOk > monitor.interval_sec * 1000 * MONITOR_STALE_FACTOR
}

/** 数值展示：整数千分位，小数保留两位；空值给破折号，不伪造 0。 */
export function formatMonitorValue(
  value: number | null | undefined,
  unit: string | null = null,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const text = Number.isInteger(value)
    ? value.toLocaleString('zh-CN')
    : value.toFixed(2)
  return unit ? `${text} ${unit}` : text
}

export function formatMonitorRule(
  op: DbAgentMonitorOp,
  threshold: number,
  unit: string | null = null,
): string {
  return `${OP_SYMBOL[op] ?? op} ${formatMonitorValue(threshold, unit)}`
}

export function monitorStatusClass(status: DbAgentMonitorStatus): string {
  return status === 'ok' || status === 'breach' ? `monitor-${status}` : 'monitor-unknown'
}
