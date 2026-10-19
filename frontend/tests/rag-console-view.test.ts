/** RAG 控制台回归：概览告警优先、动作跳转和选中详情轮询刷新。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RagConsoleView from '../src/views/RagConsoleView.vue'
import {
  approveRagAction,
  cancelRagAction,
  checkRagAction,
  getRagAction,
  getRagIncident,
  getRagIncidentRecommendations,
  getRagRequest,
  getRagStatus,
  listRagActions,
  listRagIncidents,
  listRagRequests,
  planRagAction,
  proposeRagAction,
  rollbackRagAction,
} from '../src/api/ragAdmin'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('../src/stores/auth', () => ({
  useAuthStore: () => ({
    profileLoaded: true,
    user: { username: 'admin', role: 'admin' },
  }),
}))

vi.mock('../src/api/ragAdmin', () => ({
  acknowledgeIncident: vi.fn(),
  approveRagAction: vi.fn(),
  cancelRagAction: vi.fn(),
  checkRagAction: vi.fn(),
  getRagAction: vi.fn(),
  getRagIncident: vi.fn(),
  getRagIncidentRecommendations: vi.fn(),
  getRagRequest: vi.fn(),
  getRagStatus: vi.fn(),
  listRagActions: vi.fn(),
  listRagIncidents: vi.fn(),
  listRagRequests: vi.fn(),
  planRagAction: vi.fn(),
  proposeRagAction: vi.fn(),
  resolveIncident: vi.fn(),
  rollbackRagAction: vi.fn(),
  runRagActionWorker: vi.fn(),
}))

const status = {
  window_hours: 24,
  verdicts_by_code: [],
  recent_verdicts: [],
  retrieval_24h: { total: 10, empty_recall: 0, rerank_dropped: 0, p95_latency_ms: 600 },
  answers_24h: { total: 10, zero_retrieval: 0 },
  requests_24h: { total: 10, failed: 0, p95_latency_ms: 12000 },
  evaluation: null,
  runtime_config: {
    rerank_top: 12,
    rerank_top_observed: 12,
    rerank_top_override: null,
    rerank_top_default: 12,
    rerank_top_overridden: false,
  },
  health_meta: {
    open_incidents: 1,
    waiting_actions: 0,
    queued_actions: 0,
    executing_actions: 0,
    trace_lag_seconds: 1,
  },
}

const request = {
  request_uid: 'req-1',
  created_at: '2026-09-15T10:00:00',
  completed_at: '2026-09-15T10:00:01',
  persona: 'civil_code',
  route: 'simple_fast_path',
  status: 'succeeded',
  error_code: '',
  selected_model: 'qwen3.7-flash',
  provider: 'dashscope',
  config_version: 'test',
  first_token_ms: 100,
  total_ms: 500,
  input_tokens: 10,
  output_tokens: 20,
  instance_id: 'gateway',
}

const requestDetail = {
  ...request,
  content: {
    query: '定金不退合法吗',
    answer: '根据民法典第五百八十七条',
    answer_len: 15,
    cited_numbers: [587],
    ungrounded_citations: [],
    retrieval_returned_count: 3,
    retrieval_method: 'trgm+vector+rrf+local_rerank',
    retrieval_latency_ms: 80,
    feedback: null,
    retrieval_runs: [],
    chunks: [],
  },
  spans: [],
}

const action = {
  action_id: 1,
  incident_id: 1,
  request_uid: null,
  action_key: 'adjust_rerank_top',
  risk_level: 'L1',
  status: 'waiting_approval',
  params: { value: 12 },
  reason: '延迟验证',
  result: {},
  verification: {},
  rollback_plan: '',
  rollback_result: {},
  rollback_available: false,
  policy_decision_id: 1,
  environment: 'local',
  config_version: 'test',
  requested_by: 'admin',
  approved_by: null,
  executed_by: null,
  attempt_count: 0,
  created_at: '2026-09-15T10:00:00',
  started_at: null,
  ended_at: null,
  verifications: [],
}

const incident = {
  incident_id: 1,
  code: 'RC-8',
  name: '延迟异常',
  severity: 'high',
  status: 'open',
  title: '检索延迟持续偏高',
  summary: '',
  suspected_cause: '候选池过大',
  recommended_action: '重跑诊断',
  persona: 'civil_code',
  occurrence_count: 3,
  affected_requests: 2,
  first_seen_at: '2026-09-15T10:00:00',
  last_seen_at: '2026-09-15T10:05:00',
  resolved_at: null,
  closed_at: null,
  owner: null,
  acknowledged_by: null,
  verdicts: [],
}

/** 构造分组排序测试事件。 */
function makeIncident(
  incidentId: number,
  status: string,
  severity: string,
  lastSeenAt: string,
) {
  return {
    ...incident,
    incident_id: incidentId,
    status,
    severity,
    last_seen_at: lastSeenAt,
    resolved_at: status === 'resolved' ? lastSeenAt : null,
    closed_at: status === 'closed' ? lastSeenAt : null,
    title: `事件 ${incidentId}`,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getRagStatus).mockResolvedValue(status)
  vi.mocked(listRagRequests).mockResolvedValue({ items: [request] })
  vi.mocked(listRagIncidents).mockResolvedValue({ items: [] })
  vi.mocked(listRagActions).mockResolvedValue({ items: [action] })
  vi.mocked(getRagRequest).mockResolvedValue(requestDetail)
  vi.mocked(getRagIncident).mockResolvedValue(incident)
  vi.mocked(getRagIncidentRecommendations).mockResolvedValue({ items: [] })
  vi.mocked(getRagAction).mockResolvedValue({
    ...action,
    policy_decision: null,
  })
  vi.mocked(planRagAction).mockResolvedValue({
    ...action,
    action_key: 'probe_health',
    status: 'queued',
  })
  vi.mocked(cancelRagAction).mockResolvedValue({
    ...action,
    status: 'cancelled',
  })
  vi.mocked(rollbackRagAction).mockResolvedValue({
    ...action,
    status: 'rolled_back',
    rollback_available: false,
  })
  vi.mocked(proposeRagAction).mockResolvedValue({
    ...action,
    incident_id: null,
    action_key: 'set_user_active',
    risk_level: 'L2',
    status: 'waiting_approval',
  })
})

describe('RagConsoleView 概览与详情', () => {
  it('优先展示待处理告警，容量指标位于评测之后', async () => {
    const wrapper = mount(RagConsoleView)
    await flushPromises()

    const html = wrapper.html()
    expect(wrapper.find('.alert-panel').exists()).toBe(true)
    expect(wrapper.find('.alert-panel').text()).toContain('待审批动作')
    expect(html.indexOf('待处理告警')).toBeLessThan(html.indexOf('容量与运行指标'))
    wrapper.unmount()
  })

  it('点击最近动作会切换到动作标签并打开审计详情', async () => {
    const wrapper = mount(RagConsoleView)
    await flushPromises()

    await wrapper.find('.recent-actions tbody tr').trigger('click')
    await flushPromises()

    expect(getRagAction).toHaveBeenCalledWith(1)
    expect(wrapper.find('.action-detail').exists()).toBe(true)
    wrapper.unmount()
  })

  it('轮询刷新列表时同步刷新已选请求详情并保留选择', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mount(RagConsoleView)
      await flushPromises()
      const tabButtons = wrapper.findAll('.tabs button')
      await tabButtons[1].trigger('click')
      await wrapper.find('.table-panel tbody tr').trigger('click')
      await flushPromises()

      expect(getRagRequest).toHaveBeenCalledTimes(1)
      expect(wrapper.find('.request-detail').exists()).toBe(true)

      vi.advanceTimersByTime(15_000)
      await flushPromises()

      expect(getRagRequest).toHaveBeenCalledTimes(2)
      expect(wrapper.find('.request-detail').exists()).toBe(true)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('事件列表按未处理和已处理分组排序', async () => {
    vi.mocked(listRagIncidents).mockResolvedValue({
      items: [
        makeIncident(1, 'open', 'medium', '2026-09-15T10:03:00'),
        makeIncident(2, 'resolved', 'high', '2026-09-15T09:00:00'),
        makeIncident(3, 'open', 'critical', '2026-09-15T10:01:00'),
        makeIncident(4, 'closed', 'high', '2026-09-15T09:30:00'),
        makeIncident(5, 'open', 'high', '2026-09-15T10:02:00'),
      ],
    })
    const wrapper = mount(RagConsoleView)
    await flushPromises()
    await wrapper.findAll('.tabs button')[2].trigger('click')

    const groups = wrapper.findAll('.incident-group-row').map((row) => row.text())
    expect(groups[0]).toContain('未处理事件 3')
    expect(groups[1]).toContain('已处理事件 2')
    const rows = wrapper.findAll('.table-panel tbody tr:not(.incident-group-row)')
    expect(rows.map((row) => row.text()).slice(0, 5)).toEqual([
      expect.stringContaining('事件 3'),
      expect.stringContaining('事件 5'),
      expect.stringContaining('事件 1'),
      expect.stringContaining('事件 4'),
      expect.stringContaining('事件 2'),
    ])
    wrapper.unmount()
  })

  it('推荐修复支持提交审批、立即撤销和人工建议', async () => {
    vi.mocked(listRagIncidents).mockResolvedValue({ items: [incident] })
    vi.mocked(getRagIncidentRecommendations).mockResolvedValue({
      items: [
        {
          action_key: 'probe_health',
          params: { target: 'gateway' },
          reason: '确认网关检索依赖可用',
          risk_level: 'L0',
          mode: 'auto',
        },
        {
          action_key: '',
          params: {},
          reason: '需要人工检查配置',
          risk_level: '',
          mode: 'manual',
        },
      ],
    })
    const wrapper = mount(RagConsoleView)
    await flushPromises()
    await wrapper.findAll('.tabs button')[2].trigger('click')
    await wrapper.find('.table-panel tbody tr:not(.incident-group-row)').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('推荐修复')
    expect(wrapper.text()).toContain('仅提供建议，不创建动作。')
    expect(wrapper.text()).toContain('生成并执行')
    await wrapper.get('.recommendation-card button').trigger('click')
    await flushPromises()
    expect(planRagAction).toHaveBeenCalledWith(
      1,
      'probe_health',
      { target: 'gateway' },
      '确认网关检索依赖可用',
    )
    expect(wrapper.text()).toContain('撤销刚才提交')
    await wrapper.get('.recommendation-actions button.danger').trigger('click')
    await flushPromises()
    expect(cancelRagAction).toHaveBeenCalledWith(1)
    wrapper.unmount()
  })

  it('已成功且可回滚的动作支持二次确认回滚并刷新', async () => {
    const succeededAction = {
      ...action,
      status: 'succeeded',
      rollback_available: true,
    }
    vi.mocked(listRagActions).mockResolvedValue({ items: [succeededAction] })
    vi.mocked(getRagAction).mockResolvedValue(succeededAction)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(RagConsoleView)
    await flushPromises()
    await wrapper.findAll('.tabs button')[3].trigger('click')
    await wrapper.find('.table-panel tbody tr').trigger('click')
    await flushPromises()

    await wrapper.get('.rollback-button').trigger('click')
    await flushPromises()

    expect(window.confirm).toHaveBeenCalled()
    expect(rollbackRagAction).toHaveBeenCalledWith(1)
    expect(getRagAction).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('执行失败且可回滚的动作同样给回滚入口（自动回滚失败的唯一重试通道）', async () => {
    const failedAction = {
      ...action,
      status: 'failed',
      rollback_available: true,
    }
    vi.mocked(listRagActions).mockResolvedValue({ items: [failedAction] })
    vi.mocked(getRagAction).mockResolvedValue(failedAction)
    vi.mocked(rollbackRagAction).mockResolvedValue(failedAction)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(RagConsoleView)
    await flushPromises()
    await wrapper.findAll('.tabs button')[3].trigger('click')
    await wrapper.find('.table-panel tbody tr').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('该动作执行失败')
    await wrapper.get('.rollback-button').trigger('click')
    await flushPromises()

    expect(window.confirm).toHaveBeenCalled()
    expect(rollbackRagAction).toHaveBeenCalledWith(1)
    expect(getRagAction).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('无法由系统撤销的失败动作可人工核对终结（说明必填，不改动数据）', async () => {
    const failedAction = { ...action, status: 'failed', rollback_available: false }
    const checkedAction = { ...failedAction, status: 'checked' }
    vi.mocked(listRagActions).mockResolvedValue({ items: [failedAction] })
    vi.mocked(getRagAction).mockResolvedValueOnce(failedAction)
    vi.mocked(checkRagAction).mockResolvedValue(checkedAction)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(RagConsoleView)
    await flushPromises()
    await wrapper.findAll('.tabs button')[3].trigger('click')
    await wrapper.find('.table-panel tbody tr').trigger('click')
    await flushPromises()

    // 说明没填不许点：终结等于由人来担保这条动作的数据状态
    expect(wrapper.get('.check-button').attributes('disabled')).toBeDefined()
    await wrapper.find('.check-block input').setValue('  已手工恢复渠道开关  ')
    await wrapper.get('.check-button').trigger('click')
    await flushPromises()

    expect(window.confirm).toHaveBeenCalled()
    expect(checkRagAction).toHaveBeenCalledWith(1, '已手工恢复渠道开关')
    expect(getRagAction).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('审批失败只弹错误横幅，不顶掉整个控制台分区', async () => {
    vi.mocked(approveRagAction).mockRejectedValue(new Error('动作状态已变更，审批未生效'))
    const wrapper = mount(RagConsoleView)
    await flushPromises()
    await wrapper.findAll('.tabs button')[3].trigger('click')

    await wrapper.get('.row-actions button').trigger('click')
    await flushPromises()

    const banner = wrapper.get('.state-banner.danger')
    expect(banner.text()).toContain('动作状态已变更，审批未生效')
    expect(wrapper.find('.tabs').exists()).toBe(true)
    expect(wrapper.findAll('.table-panel tbody tr').length).toBeGreaterThan(0)

    await banner.get('button').trigger('click')
    expect(wrapper.find('.state-banner').exists()).toBe(false)
    wrapper.unmount()
  })

  it('动作分区内嵌业务提议表单，提交后刷新列表', async () => {
    const wrapper = mount(RagConsoleView)
    await flushPromises()
    await wrapper.findAll('.tabs button')[3].trigger('click')

    expect(wrapper.find('.propose-panel').exists()).toBe(true)
    await wrapper.get('.propose-panel input').setValue('u_1')
    await wrapper.get('.propose-panel textarea').setValue('风控处置，先停用')
    await wrapper.get('.propose-panel button.primary').trigger('click')
    await flushPromises()

    expect(proposeRagAction).toHaveBeenCalledWith(
      'set_user_active',
      { username: 'u_1', active: false },
      '风控处置，先停用',
    )
    // 提交成功后父组件重取列表与选中详情
    expect(listRagActions).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
