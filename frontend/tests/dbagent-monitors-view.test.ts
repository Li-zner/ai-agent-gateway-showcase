/** 数据监控页组件：三态卡片、陈旧标记与新建草稿走真实表单流程。 */
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RagDbAgentMonitors from '../src/components/RagDbAgentMonitors.vue'
import zhCN from '../src/locales/zh-CN'
import {
  createDbAgentMonitor,
  listDbAgentMonitors,
} from '../src/api/dbagent'

vi.mock('../src/api/dbagent', () => ({
  listDbAgentMonitors: vi.fn(),
  createDbAgentMonitor: vi.fn(),
  updateDbAgentMonitor: vi.fn(),
  runDbAgentMonitorOnce: vi.fn(),
}))

const i18n = createI18n({ legacy: false, locale: 'zh', messages: { zh: zhCN } })

const monitor = {
  id: 1,
  name: '待处理订单',
  sql: 'SELECT count(*) FROM demo_orders',
  unit: '单',
  interval_sec: 60,
  enabled: true,
  op: 'gt' as const,
  threshold: 10,
  last_value: 15,
  last_status: 'breach' as const,
  last_error: null,
  last_run_at: '2026-11-11T10:00:00Z',
  last_ok_at: '2020-01-01T00:00:00Z',
  next_run_at: null,
  created_by: 'admin',
  created_at: '2026-11-11T09:00:00Z',
  updated_at: '2026-11-11T10:00:00Z',
}

function mountPage() {
  return mount(RagDbAgentMonitors, { global: { plugins: [i18n] } })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listDbAgentMonitors).mockResolvedValue([monitor])
})

describe('RagDbAgentMonitors', () => {
  it('超阈值卡片显示值、状态与数据陈旧标记', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const card = wrapper.get('.monitor-card')
    expect(card.classes()).toContain('monitor-breach')
    expect(card.text()).toContain('超阈值')
    expect(card.text()).toContain('15 单')
    expect(card.text()).toContain('数据陈旧')
    wrapper.unmount()
  })

  it('新建草稿必须带名称和 SQL，保存后调用创建接口且不含 enabled', async () => {
    vi.mocked(listDbAgentMonitors).mockResolvedValue([])
    vi.mocked(createDbAgentMonitor).mockResolvedValue({ ...monitor, id: 2, enabled: false })
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('.head-actions button.primary').trigger('click')
    await wrapper.get('.monitor-drawer').trigger('submit')
    expect(wrapper.get('.form-error').text()).toContain('名称与 SQL 必填')
    expect(createDbAgentMonitor).not.toHaveBeenCalled()

    const inputs = wrapper.findAll('.monitor-drawer input')
    await inputs[0].setValue('仓库存量')
    await wrapper.get('.monitor-drawer textarea').setValue('SELECT count(*) FROM demo_orders')
    await inputs[1].setValue('5')
    await wrapper.get('.monitor-drawer').trigger('submit')
    await flushPromises()

    expect(createDbAgentMonitor).toHaveBeenCalledWith({
      name: '仓库存量',
      sql: 'SELECT count(*) FROM demo_orders',
      op: 'gt',
      threshold: 5,
      interval_sec: 60,
      unit: null,
    })
    expect(createDbAgentMonitor).not.toHaveBeenCalledWith(
      expect.objectContaining({ enabled: expect.anything() }),
    )
    expect(wrapper.find('.monitor-drawer').exists()).toBe(false)
    wrapper.unmount()
  })
})
