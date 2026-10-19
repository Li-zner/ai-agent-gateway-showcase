/** RAG 运维会话交互回归：点击请求只预览，显式按钮才调用模型。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RagOpsChatView from '../src/views/RagOpsChatView.vue'
import {
  getRagRequest,
  getRagStatus,
  listRagIncidents,
  listRagRequests,
  streamRagOpsAsk,
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
  getRagIncident: vi.fn(),
  getRagRequest: vi.fn(),
  getRagStatus: vi.fn(),
  listRagChangeSets: vi.fn(),
  listRagIncidents: vi.fn(),
  listRagRequests: vi.fn(),
  planRagAction: vi.fn(),
  proposeRagChangeSet: vi.fn(),
  resolveIncident: vi.fn(),
  streamRagOpsAsk: vi.fn(),
}))

const status = {
  window_hours: 24,
  verdicts_by_code: [],
  recent_verdicts: [],
  retrieval_24h: { total: 1, empty_recall: 0, rerank_dropped: 0, p95_latency_ms: 100 },
  answers_24h: { total: 1, zero_retrieval: 0 },
  requests_24h: { total: 1, failed: 0, p95_latency_ms: 100 },
  evaluation: null,
  runtime_config: {
    rerank_top: 12,
    rerank_top_observed: 12,
    rerank_top_override: null,
    rerank_top_default: 12,
    rerank_top_overridden: false,
  },
  health_meta: {
    open_incidents: 0,
    waiting_actions: 0,
    queued_actions: 0,
    executing_actions: 0,
    trace_lag_seconds: 1,
  },
}

const request = {
  request_uid: 'req-1',
  created_at: '2026-09-14T10:00:00',
  completed_at: '2026-09-14T10:00:01',
  persona: 'civil_code',
  route: 'simple_fast_path',
  status: 'succeeded',
  error_code: '',
  selected_model: 'deepseek-chat',
  provider: 'deepseek',
  config_version: 'test',
  first_token_ms: 20,
  total_ms: 100,
  input_tokens: 10,
  output_tokens: 20,
  instance_id: 'gateway',
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(getRagStatus).mockResolvedValue(status)
  vi.mocked(listRagIncidents).mockResolvedValue({ items: [] })
  vi.mocked(listRagRequests).mockResolvedValue({ items: [request] })
  vi.mocked(getRagRequest).mockResolvedValue({
    ...request,
    content: {
      query: '定金不退合法吗',
      answer: '根据民法典第五百八十七条……',
      answer_len: 18,
      cited_numbers: [587],
      ungrounded_citations: [],
      retrieval_returned_count: 3,
      retrieval_method: 'trgm+vector+rrf+local_rerank',
      retrieval_latency_ms: 80,
      feedback: null,
      retrieval_runs: [{
        query: '定金不退合法吗',
        method: 'trgm+vector+rrf+local_rerank',
        trgm_hits: 3,
        vec_hits: 3,
        max_trgm_sim: 0.8,
        max_vec_sim: 0.7,
        returned_count: 3,
        rerank_used: true,
        latency_ms: 80,
        chunk_keys: ['chunk-1'],
        created_at: '2026-09-14T10:00:00',
      }],
      chunks: [{
        chunk_key: 'chunk-1',
        source: 'civil_code',
        heading: '第五百八十七条',
        content: '债务人履行债务的，定金应当抵作价款或者收回。',
      }],
    },
    spans: [{
      span_uid: 'span-1',
      stage: 'retrieve',
      round_no: 1,
      status: 'ok',
      started_at: null,
      ended_at: null,
      latency_ms: 80,
      result_count: 3,
      error_code: '',
      attributes: {},
    }],
  })
  vi.mocked(streamRagOpsAsk).mockResolvedValue(undefined)
})

describe('RagOpsChatView 错误复位', () => {
  it('首屏刷新失败显示错误，轮询成功后清除错误恢复会话区', async () => {
    vi.useFakeTimers()
    try {
      // 第一次刷新失败，后续（15s 轮询）恢复
      vi.mocked(getRagStatus).mockRejectedValueOnce(new Error('后端不可用'))
      const wrapper = mount(RagOpsChatView)
      await flushPromises()

      expect(wrapper.find('.state.danger').exists()).toBe(true)
      expect(wrapper.find('.state.danger').text()).toContain('后端不可用')

      vi.advanceTimersByTime(15_000)
      await flushPromises()

      expect(
        wrapper.find('.state.danger').exists(),
        '轮询成功后错误必须清除，否则 UI 永久卡在错误页',
      ).toBe(false)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('RagOpsChatView 请求预览', () => {
  it('点击请求不触发分析，显式按钮才会调用运维助手', async () => {
    const wrapper = mount(RagOpsChatView)
    await flushPromises()

    await wrapper.find('.request-item').trigger('click')
    await flushPromises()

    expect(getRagRequest).toHaveBeenCalledWith('req-1')
    expect(streamRagOpsAsk).not.toHaveBeenCalled()
    expect(wrapper.find('.request-context').exists()).toBe(true)

    await wrapper.find('.request-item').trigger('click')
    expect(getRagRequest).toHaveBeenCalledTimes(1)

    await wrapper.find('.analyze-request').trigger('click')
    await flushPromises()

    expect(streamRagOpsAsk).toHaveBeenCalledTimes(1)
    expect(vi.mocked(streamRagOpsAsk).mock.calls[0][0].question)
      .toContain('stage=retrieve')
    wrapper.unmount()
  })
})

describe('RagOpsChatView 流式会话', () => {
  it('send 走流式接口，增量片段聚合进同一条助手消息', async () => {
    vi.mocked(streamRagOpsAsk).mockImplementation(async (opts) => {
      opts.onEvent({ type: 'answer_chunk', content: '检索' })
      opts.onEvent({ type: 'answer_chunk', content: '健康' })
      opts.onEvent({ type: 'answer_complete', content: '检索健康' })
    })
    const wrapper = mount(RagOpsChatView)
    await flushPromises()

    await wrapper.find('textarea').setValue('现在检索健康吗')
    await wrapper.find('footer button').trigger('click')
    await flushPromises()

    const assistantMessages = wrapper.findAll('.ops-message.assistant')
    expect(streamRagOpsAsk).toHaveBeenCalledTimes(1)
    expect(assistantMessages.length).toBeGreaterThan(0)
    expect(assistantMessages[assistantMessages.length - 1].text())
      .toContain('检索健康')
    wrapper.unmount()
  })

  it('流式失败时错误落到助手占位消息，而不是静默无响应', async () => {
    vi.mocked(streamRagOpsAsk).mockRejectedValue(new Error('后端不可用'))
    const wrapper = mount(RagOpsChatView)
    await flushPromises()

    await wrapper.find('textarea').setValue('现在检索健康吗')
    await wrapper.find('footer button').trigger('click')
    await flushPromises()

    const assistantMessages = wrapper.findAll('.ops-message.assistant')
    expect(assistantMessages[assistantMessages.length - 1].text())
      .toContain('后端不可用')
    wrapper.unmount()
  })
})
