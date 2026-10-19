/** RAG 监测中控台管理 API。 */
import { get, post, ApiError, getAccessToken, notifyUnauthorized, refreshAccessToken, resolve_url } from './http'
import { SseLineFramer, SSE_DONE, type SseEvent } from './sse'

export interface RagStatus {
  window_hours: number
  verdicts_by_code: Array<{ code: string; severity: string; count: number }>
  recent_verdicts: Array<Record<string, unknown>>
  retrieval_24h: {
    total: number
    empty_recall: number
    rerank_dropped: number
    p95_latency_ms: number
  }
  answers_24h: {
    total: number
    zero_retrieval: number
  }
  requests_24h: {
    total: number
    failed: number
    p95_latency_ms: number
  }
  evaluation: {
    report: string
    evaluated: number
    split: string
    errors: number
    recall_at_5: number | null
    precision_at_5: number | null
    recall_at_10: number | null
    precision_at_10: number | null
    mrr: number | null
    zero_hit_at_10: string[]
    effective_window: number | null
    rerank_top: number | null
    latency_ms: { avg?: number; p95?: number }
  } | null
  runtime_config: {
    rerank_top: number
    rerank_top_observed: number | null
    rerank_top_override: number | null
    rerank_top_default: number
    rerank_top_overridden: boolean
  }
  health_meta: {
    open_incidents: number
    waiting_actions: number
    queued_actions: number
    executing_actions: number
    trace_lag_seconds: number | null
  }
}

export interface RagRequest {
  request_uid: string
  created_at: string
  completed_at: string | null
  persona: string
  route: string
  status: string
  error_code: string
  selected_model: string
  provider: string
  config_version: string
  first_token_ms: number | null
  total_ms: number | null
  input_tokens: number | null
  output_tokens: number | null
  instance_id: string
}

export interface RagSpan {
  span_uid: string
  stage: string
  round_no: number
  status: string
  started_at: string | null
  ended_at: string | null
  latency_ms: number | null
  result_count: number | null
  error_code: string
  attributes: Record<string, unknown>
}

export interface RagRetrievalRun {
  query: string
  method: string
  trgm_hits: number | null
  vec_hits: number | null
  max_trgm_sim: number | null
  max_vec_sim: number | null
  returned_count: number | null
  rerank_used: boolean | null
  latency_ms: number | null
  chunk_keys: string[]
  created_at: string
}

export interface RagChunk {
  chunk_key: string
  source: string
  heading: string
  content: string
}

export interface RagRequestContent {
  query: string
  answer: string
  answer_len: number
  cited_numbers: number[]
  // 后端 find_ungrounded_citations 返回条文号字符串（如"第一千零七十七条"）
  ungrounded_citations: string[]
  retrieval_returned_count: number | null
  retrieval_method: string | null
  retrieval_latency_ms: number | null
  feedback: number | null
  retrieval_runs: RagRetrievalRun[]
  chunks: RagChunk[]
}

/** incident 关联的单条诊断结论样本（含证据与修复参数，供详情/问询上下文使用）。 */
export interface RagVerdictSample {
  id: number
  created_at?: string
  source?: string
  source_id?: number
  request_uid?: string | null
  query_snippet?: string
  code: string
  severity?: string
  title?: string
  evidence?: string[]
  action?: string
  patch?: Record<string, unknown>
}

export interface RagIncident {
  incident_id: number
  code: string
  name: string
  severity: string
  status: string
  title: string
  summary: string
  suspected_cause: string
  recommended_action: string
  persona: string
  occurrence_count: number
  affected_requests: number
  first_seen_at: string
  last_seen_at: string
  resolved_at: string | null
  closed_at: string | null
  owner: string | null
  acknowledged_by: string | null
  verdicts?: RagVerdictSample[]
}

export interface RagIncidentRecommendation {
  action_key: string
  params: Record<string, unknown>
  reason: string
  risk_level: string
  mode: 'auto' | 'approval' | 'manual'
}

export interface RagAction {
  action_id: number
  incident_id: number | null
  request_uid: string | null
  action_key: string
  risk_level: string
  status: string
  params: Record<string, unknown>
  reason: string
  result: Record<string, unknown>
  verification: Record<string, unknown>
  rollback_plan: string
  rollback_result: Record<string, unknown>
  rollback_available: boolean
  policy_decision_id: number | null
  environment: string
  config_version: string | null
  requested_by: string | null
  approved_by: string | null
  executed_by: string | null
  attempt_count: number
  created_at: string
  started_at: string | null
  ended_at: string | null
  policy_decision?: RagPolicyDecision | null
  verifications?: RagActionVerification[]
}

export interface RagPolicyDecision {
  decision_id: number
  environment: string
  decision: string
  reason: string
  policy_version: string
  requested_params: Record<string, unknown>
  requested_by: string | null
  cooldown_until: string | null
  created_at: string
}

export interface RagActionVerification {
  verification_id: number
  attempt_no: number
  phase: string
  passed: boolean
  metrics: Record<string, unknown>
  evidence: Record<string, unknown>
  reason: string
  created_at: string
}

/** includeEval：把评测人格（civil_code_eval）流量算回统计，默认排除。 */
export function getRagStatus(includeEval = false): Promise<RagStatus> {
  return get<RagStatus>(`/api/admin/rag/status?include_eval=${includeEval}`)
}

export function listRagRequests(
  limit = 50,
  includeEval = false,
): Promise<{ items: RagRequest[] }> {
  return get(
    `/api/admin/rag/requests?limit=${limit}&include_eval=${includeEval}`,
  )
}

export function getRagRequest(
  requestUid: string,
): Promise<RagRequest & { spans: RagSpan[]; content: RagRequestContent }> {
  return get(`/api/admin/rag/requests/${encodeURIComponent(requestUid)}`)
}

export function listRagIncidents(limit = 50): Promise<{ items: RagIncident[] }> {
  return get(`/api/admin/rag/incidents?limit=${limit}`)
}

export function getRagIncident(incidentId: number): Promise<RagIncident> {
  return get(`/api/admin/rag/incidents/${incidentId}`)
}

export function getRagIncidentRecommendations(
  incidentId: number,
): Promise<{ items: RagIncidentRecommendation[] }> {
  return get(`/api/admin/rag/incidents/${incidentId}/recommendations`)
}

export function acknowledgeIncident(incidentId: number): Promise<RagIncident> {
  return post(`/api/admin/rag/incidents/${incidentId}/ack`)
}

export function resolveIncident(incidentId: number): Promise<RagIncident> {
  return post(`/api/admin/rag/incidents/${incidentId}/resolve`)
}

/** 超管批量清理：仅解决静默超过 staleHours 的活跃事件，服务端不删行。 */
export function clearStaleIncidents(
  staleHours = 24,
): Promise<{ cleared: RagIncident[]; cleared_count: number }> {
  return post(`/api/admin/rag/incidents/clear?stale_hours=${staleHours}`)
}

export function listRagActions(limit = 50): Promise<{ items: RagAction[] }> {
  return get(`/api/admin/rag/actions?limit=${limit}`)
}

export function getRagAction(actionId: number): Promise<RagAction> {
  return get(`/api/admin/rag/actions/${actionId}`)
}

export function planRagAction(
  incidentId: number,
  actionKey: string,
  params: Record<string, unknown> = {},
  reason = '',
): Promise<RagAction> {
  return post(`/api/admin/rag/incidents/${incidentId}/actions`, {
    action_key: actionKey,
    params,
    reason,
  })
}

export function proposeRagAction(
  actionKey: string,
  params: Record<string, unknown>,
  reason: string,
): Promise<RagAction> {
  // 不挂事件的业务动作提议：只入队等待审批，本接口不执行
  return post('/api/admin/rag/actions', {
    action_key: actionKey,
    params,
    reason,
  })
}

export function approveRagAction(actionId: number): Promise<RagAction> {
  return post(`/api/admin/rag/actions/${actionId}/approve`)
}

export function cancelRagAction(actionId: number): Promise<RagAction> {
  return post(`/api/admin/rag/actions/${actionId}/cancel`)
}

export function rollbackRagAction(actionId: number): Promise<RagAction> {
  return post(`/api/admin/rag/actions/${actionId}/rollback`)
}

export function checkRagAction(actionId: number, note: string): Promise<RagAction> {
  return post(`/api/admin/rag/actions/${actionId}/check`, { note })
}

export function runRagActionWorker(): Promise<{ executed: number }> {
  return post('/api/admin/rag/actions/run-once')
}

export function askRagOps(
  question: string,
  history: Array<{ role: 'user' | 'assistant'; content: string }> = [],
): Promise<{
  answer: string
  status_snapshot: RagStatus
}> {
  return post('/api/admin/rag/ask', { question, history })
}

export interface RagAskStreamOptions {
  question: string
  history: Array<{ role: 'user' | 'assistant'; content: string }>
  /** answer_chunk / answer_complete / answer_error / thought 逐事件回调 */
  onEvent: (event: SseEvent) => void
  signal?: AbortSignal
}

/** POST-SSE 请求；401 单飞刷新重试一次（对齐 chat.ts fetchStream 语义）。 */
async function fetchAskStream(
  body: string,
  signal: AbortSignal | undefined,
  retried: boolean,
): Promise<Response> {
  const resp = await fetch(resolve_url('/api/admin/rag/ask/stream'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getAccessToken()}`,
      Accept: 'text/event-stream',
    },
    body,
    signal,
  })
  if (resp.status === 401 && !retried && (await refreshAccessToken())) {
    return fetchAskStream(body, signal, true)
  }
  if (resp.status === 401) notifyUnauthorized()
  return resp
}

/** 运维会话流式版（P2-1）；终止以 answer_complete 或 [DONE] 为准。 */
export async function streamRagOpsAsk(opts: RagAskStreamOptions): Promise<void> {
  const body = JSON.stringify({ question: opts.question, history: opts.history })
  const resp = await fetchAskStream(body, opts.signal, false)
  if (!resp.ok || !resp.body) {
    let detail = `HTTP ${resp.status}`
    try {
      detail = (await resp.json()).detail ?? detail
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new ApiError(resp.status, detail)
  }
  let terminated = false
  const framer = new SseLineFramer((event) => {
    if (!event) return
    if (event === SSE_DONE) {
      terminated = true
      return
    }
    if (event.type === 'answer_complete') terminated = true
    opts.onEvent(event)
  })
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  // 空闲看门狗（对齐 chat.ts：60s 无字节视为连接僵死，避免永久"生成中"）
  let timedOut = false
  let watchdog: ReturnType<typeof setTimeout> | null = null
  const resetWatchdog = () => {
    if (watchdog) clearTimeout(watchdog)
    watchdog = setTimeout(() => {
      timedOut = true
      reader.cancel('idle timeout').catch(() => {})
    }, 60000)
  }
  resetWatchdog()
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      resetWatchdog()
      framer.feed(decoder.decode(value, { stream: true }))
    }
    framer.feed(decoder.decode())
  } catch (e) {
    if (!timedOut) throw e
    throw new ApiError(504, '连接空闲超时，请重试')
  } finally {
    if (watchdog) clearTimeout(watchdog)
  }
  if (!terminated) throw new ApiError(502, '连接中断，回答可能不完整')
}

/** L2 知识库变更集（Phase 3）：pending_approval → applied/failed/rolled_back。 */
export interface RagChangeSetItem {
  chunk_key: string
  source: string
  heading: string
  content: string
  article: string | null
}

export interface RagChangeSet {
  change_set_id: number
  change_type: 'knowledge_add' | 'knowledge_invalidate'
  target_table: string
  payload: { items?: RagChangeSetItem[]; chunk_keys?: string[] }
  validation_report: Record<string, unknown>
  snapshot: Record<string, unknown>
  status: 'pending_approval' | 'applied' | 'failed' | 'rolled_back'
  version: number
  created_by: string | null
  applied_at: string | null
  rolled_back_at: string | null
  created_at: string
  updated_at: string
  /** get/list 端点附带：指向该变更集的最新动作（审批入口）。 */
  latest_action_id?: number | null
  latest_action_status?: string | null
}

export function listRagChangeSets(
  limit = 20,
  status?: string,
): Promise<{ items: RagChangeSet[] }> {
  const query = status ? `&status=${encodeURIComponent(status)}` : ''
  return get(`/api/admin/rag/changesets?limit=${limit}${query}`)
}

export function getRagChangeSet(changeSetId: number): Promise<RagChangeSet> {
  return get(`/api/admin/rag/changesets/${changeSetId}`)
}

export function proposeRagChangeSet(
  incidentId: number,
  changeType: 'knowledge_add' | 'knowledge_invalidate',
  payload: Record<string, unknown>,
  reason = '',
): Promise<{ change_set: RagChangeSet; action: RagAction }> {
  return post(`/api/admin/rag/incidents/${incidentId}/changesets`, {
    change_type: changeType,
    payload,
    reason,
  })
}

/** 离线测评报告类型：retrieval=检索测评，e2e=端到端评分，raw=未识别结构。 */
export type RagEvalKind = 'retrieval' | 'e2e' | 'raw'

/** 测评报告摘要。检索类看 recall/mrr/latency，端到端类看 verdicts 分布。 */
export interface RagEvalSummary {
  evaluated: number
  /** 明细总条数；可能大于 evaluated（映射表直命中的题目不走检索、不计分）。 */
  detail_count?: number
  mapping_hit_count?: number
  split: string | null
  errors: number
  methods: Record<string, number> | null
  recall_at_5?: number | null
  recall_at_10?: number | null
  mrr?: number | null
  zero_hit_at_10?: string[]
  latency_ms?: { avg?: number | null; p95?: number | null }
  excluded_count?: number
  verdicts?: Record<string, number>
  note?: string
  net_gain?: string
  regression?: string
}

/** 报告列表项（不含明细，避免列表接口过重）。 */
export interface RagEvalReport {
  report: string
  kind: RagEvalKind
  size_bytes: number
  modified_at: string
  summary: RagEvalSummary
}

/**
 * 报告明细的条目。检索类与端到端类字段不同，这里用宽接口 + 可选字段，
 * 由 kind 决定渲染哪几列——避免模板里做类型断言。
 */
export interface RagEvalItem {
  id: string
  // retrieval
  category?: string | null
  method?: string | null
  latency_ms?: number | null
  recall_at_5?: number | null
  recall_at_10?: number | null
  recall_at_20?: number | null
  mrr?: number | null
  top3_heading?: string[]
  // e2e
  group?: string | null
  naive_verdict?: string | null
  rag_verdict?: string | null
  chunks_used?: number | null
  answer_len?: number
  answer?: string
}

export interface RagEvalDetail {
  report: string
  kind: RagEvalKind
  modified_at: string
  summary: RagEvalSummary
  items: RagEvalItem[]
}

export function listRagEvaluations(): Promise<{ items: RagEvalReport[] }> {
  return get('/api/admin/rag/evaluations')
}

export function getRagEvaluation(report: string): Promise<RagEvalDetail> {
  return get(`/api/admin/rag/evaluations/${encodeURIComponent(report)}`)
}

// ---------- 只读观测「问数」（P1） ----------

/** 只读白名单表：后端逐表带一句用途说明，界面用它回答"我到底能问什么"。 */
export interface DataTableInfo {
  name: string
  usage: string
}

/**
 * 一次问数的完整回执。sql 是模型产出的原文，属技术明细，界面默认折叠；
 * truncated 为真表示结果被行数上限截过，此时结论只代表前 N 行而非全量。
 */
export interface DataAskResult {
  sql: string
  columns: string[]
  rows: Array<Record<string, string | number | boolean | null>>
  row_count: number
  truncated: boolean
  elapsed_ms: number
  answer: string
}

export function listDataTables(): Promise<{ tables: DataTableInfo[] }> {
  return get('/api/admin/rag/data/tables')
}

export function askData(
  question: string,
  history: Array<{ role: string; content: string }> = [],
): Promise<DataAskResult> {
  return post('/api/admin/rag/data/ask', { question, history })
}
