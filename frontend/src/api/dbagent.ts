/** DB Agent 任务与审批 API（中控台 Agent 分区）。 */
import { get, post, put } from './http'

export interface DbAgentPreview {
  kind: string
  preview_sql: string | null
  rows: Array<Record<string, unknown>>
  /** UPDATE 且 SET 全为简单"列 = $N"时的 after 投影行（与 rows 同序对齐）；
   *  null = 不可投影（表达式 SET / DELETE / INSERT），审批卡不渲染 Diff */
  projected?: Array<Record<string, unknown>> | null
  note: string
}

/** 写操作风险分级（后端代码计算）：L0 点改 / L1 批量 / L2 高危红色警报。 */
export interface DbAgentRisk {
  level: 'L0' | 'L1' | 'L2' | 'unknown'
  reasons: string[]
  affected: number
  table_rows: number
}

export interface DbAgentStatement {
  sql: string
  params: unknown[]
}

export interface DbAgentProposal {
  /** P1-2：batch = 批提案组（statements/previews 有值，sql/params/preview 无值） */
  kind?: 'single' | 'batch'
  sql?: string
  params?: unknown[]
  reason: string
  decision: string | null
  decided_by?: string
  preview?: DbAgentPreview
  risk: DbAgentRisk
  /** 重大变更（影响 ≥ 全表 15%，业主拍板）：仅最高管理员可批准 */
  critical?: boolean
  statements?: DbAgentStatement[]
  previews?: DbAgentPreview[]
}

export interface DbAgentStep {
  kind: string
  line?: string
  at: number
}

export type DbAgentStatus =
  | 'running'
  | 'awaiting_approval'
  | 'awaiting_plan_approval'
  | 'done'
  | 'failed'

/** 执行计划（plan mode）：复杂任务先拆步骤等人工批准再动手。 */
export interface DbAgentPlan {
  goal: string
  steps: string[]
  decision: string | null
  decided_by?: string
}

export interface DbAgentTask {
  id: string
  question: string
  actor: string
  status: DbAgentStatus
  steps: DbAgentStep[]
  report: string | null
  proposal: DbAgentProposal | null
  plan: DbAgentPlan | null
  error: string | null
  created_at: number
}

export function startDbAgentTask(
  question: string,
  contextSummary = '',
  sessionId = '',
  allowCross: boolean | null = null,
): Promise<DbAgentTask> {
  // session_id：服务端会话记忆（四层），带上后后端忽略 context_summary；
  // allow_cross：跨会话记忆开关（同 actor），null = 沿用会话既有设置。
  // 两者都只是提示词上下文，不构成任何授权。
  return post<DbAgentTask>('/api/admin/rag/dbagent/tasks', {
    question,
    context_summary: contextSummary.slice(0, 1200),
    session_id: sessionId,
    allow_cross: allowCross,
  })
}

export function getDbAgentTask(id: string): Promise<DbAgentTask> {
  return get<DbAgentTask>(`/api/admin/rag/dbagent/tasks/${id}`)
}

export function listDbAgentTasks(): Promise<DbAgentTask[]> {
  return get<DbAgentTask[]>('/api/admin/rag/dbagent/tasks')
}

export function decideDbAgentWrite(id: string, approve: boolean): Promise<DbAgentTask> {
  return post<DbAgentTask>(`/api/admin/rag/dbagent/tasks/${id}/approval`, { approve })
}

/** 变更账本条目（改动日志）。summary 是人话描述；reverse_ops 为空 = 不可自动回滚。
 *  影子期三态（深审 F1）：proposed 行 awaiting_manual → executed_manual / discarded。 */
export interface DbChangeLogItem {
  change_id: number
  task_id: string
  question: string
  action: 'apply' | 'rollback' | 'proposed'
  target_table: string
  sql_text: string
  params: unknown[]
  before_rows: Array<Record<string, unknown>>
  after_rows: Array<Record<string, unknown>>
  affected: number
  summary: string
  status:
    | 'applied'
    | 'partial'
    | 'rolled_back'
    | 'rollback_failed'
    | 'awaiting_manual'
    | 'executed_manual'
    | 'discarded'
  group_id?: string | null
  approved_by: string
  rolled_back_by?: string | null
  rollback_error?: string | null
  created_at: string
  reverse_ops?: Array<{ sql: string; params: unknown[] }> | null
}

export function listDbAgentChanges(): Promise<{ items: DbChangeLogItem[] }> {
  return get<{ items: DbChangeLogItem[] }>('/api/admin/rag/dbagent/changes')
}

export function rollbackDbAgentChange(changeId: number): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(`/api/admin/rag/dbagent/changes/${changeId}/rollback`, {})
}

/** ---- 子账号能力管理（仅 admin）---- */

export interface CapabilitySpec {
  key: string
  label: string
  desc: string
}

export interface SubAccount {
  username: string
  permissions: string[]
  is_active: boolean
  created_at: string
}

export function listSubAccounts(): Promise<{
  items: SubAccount[]
  capabilities: CapabilitySpec[]
}> {
  return get('/api/admin/rag/dbagent/perms')
}

export function createSubAccount(body: {
  username: string
  password: string
  permissions: string[]
}): Promise<SubAccount> {
  return post<SubAccount>('/api/admin/rag/dbagent/perms', body)
}

export function updateSubAccountPerms(
  username: string,
  permissions: string[],
): Promise<SubAccount> {
  return put<SubAccount>(`/api/admin/rag/dbagent/perms/${username}`, { permissions })
}

export function resetSubPassword(username: string, password: string): Promise<{ username: string }> {
  return put(`/api/admin/rag/dbagent/perms/${username}/password`, { password })
}

export function setSubStatus(username: string, active: boolean): Promise<{ username: string }> {
  return put(`/api/admin/rag/dbagent/perms/${username}/status`, { active })
}

export function deleteSubAccount(username: string): Promise<{ username: string }> {
  // http 层无 delete 封装，request 直调
  return import('./http').then(({ request }) =>
    request<{ username: string }>(`/api/admin/rag/dbagent/perms/${username}`, { method: 'DELETE' }))
}
