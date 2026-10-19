<script setup lang="ts">
/** RAG 运维会话：左边上下文，中间对话，右边实时状态。 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  label_retrieval_method,
  label_route,
  label_severity,
  label_stage,
  label_status,
} from '../utils/ragLabels'
import {
  acknowledgeIncident,
  getRagIncident,
  getRagRequest,
  getRagStatus,
  listRagIncidents,
  listRagRequests,
  planRagAction,
  resolveIncident,
  streamRagOpsAsk,
  type RagIncident,
  type RagRequest,
  type RagRequestContent,
  type RagSpan,
  type RagStatus,
} from '../api/ragAdmin'
import RagDbAgentWorkspace from '../components/RagDbAgentWorkspace.vue'
import {
  listDbAgentChanges,
  listDbAgentTasks,
  rollbackDbAgentChange,
  type DbChangeLogItem,
} from '../api/dbagent'
import { useDbagentSessions } from '../dbagentSessions'
import { useAuthStore } from '../stores/auth'
import { hasCap, isAdmin } from '../utils/authz'
import { enabledModules, openModule } from '../consoleModules'
import { renderOpsMarkdown } from '../utils/opsMarkdown'

// 顶栏"数据台"入口（固定导航，注册表驱动）
const consoleData = enabledModules().find((m) => m.key === 'console-data')
const goConsoleData = () => { if (consoleData) openModule(consoleData) }
// Agent 人格（诊断助手/数据库Agent）：同一聊天窗口，页内切换不跳窗
const persona = ref<string>('rag-ops')

interface OpsMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: number
}

// 多会话（2026-10-02，ZCode 新建/打开项目式）：每个会话独立对话，
// 可绑定事件（点事件即开窗）或通用（顶部新建，不绑事件）；按用户持久化
interface OpsSession {
  id: string
  title: string
  incidentId: number | null
  messages: OpsMessage[]
  createdAt?: number // 旧持久化数据无此字段，展示时容忍缺失
}

const auth = useAuthStore()
const status = ref<RagStatus | null>(null)
const incidents = ref<RagIncident[]>([])
const requests = ref<RagRequest[]>([])
const sessions = ref<OpsSession[]>([])
const currentSessionId = ref('')
// 侧栏列表是瘦身条目，suspected_cause 等详情字段按需取一次缓存在这
const incidentDetails = new Map<number, RagIncident>()
const currentSession = computed<OpsSession | null>(
  () => sessions.value.find((s) => s.id === currentSessionId.value) ?? null,
)
// 代理到当前会话的消息数组：现有 push/find 逻辑零改动
const messages = computed<OpsMessage[]>(() => currentSession.value?.messages ?? [])
const selectedIncident = computed<RagIncident | null>({
  get: () => {
    const id = currentSession.value?.incidentId
    if (id == null) return null
    return incidentDetails.get(id)
      ?? incidents.value.find((i) => i.incident_id === id) ?? null
  },
  set: (v) => {
    if (currentSession.value) currentSession.value.incidentId = v ? v.incident_id : null
  },
})
type RagRequestDetail = RagRequest & {
  spans: RagSpan[]
  content: RagRequestContent
}

const selectedRequest = ref<RagRequestDetail | null>(null)
const requestLoading = ref(false)
const requestError = ref('')
const input = ref('')
const loading = ref(true)
const sending = ref(false)
const forbidden = ref(false)
const error = ref('')
const transcriptEl = ref<HTMLElement | null>(null)
let refreshTimer: number | undefined
const requestCache = new Map<string, RagRequestDetail>()
let requestSeq = 0

function sessionsKeyFor(owner = ''): string {
  return `rag_ops_sessions:${owner || 'anonymous'}`
}

const activeIncidents = computed(
  () => incidents.value.filter((item) => !['resolved', 'closed'].includes(item.status)),
)

function uid(): string {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function persistSessions(): void {
  // 每用户保留最近 20 个会话、每会话最近 40 条（与旧单会话口径同量级）
  try {
    const slim = sessions.value.slice(0, 20).map((s) => ({
      ...s, messages: s.messages.slice(-40),
    }))
    localStorage.setItem(
      sessionsKeyFor(auth.user?.username ?? ''), JSON.stringify(slim))
  } catch {
    /* 存储满等异常：不影响当前会话 */
  }
}

function seedWelcome(session: OpsSession): void {
  session.messages.push({
    id: uid(),
    role: 'assistant',
    content: '我是 RAG 运维助手。点击左侧事件可开一个绑定事件的会话，或直接就全局监测数据提问。',
    createdAt: Date.now(),
  })
}

function newSession(incidentId: number | null = null, title = ''): OpsSession {
  const session: OpsSession = {
    id: uid(),
    title: title || `新会话 ${new Date().toTimeString().slice(0, 5)}`,
    incidentId,
    messages: [],
    createdAt: Date.now(),
  }
  seedWelcome(session)
  sessions.value.unshift(session)
  currentSessionId.value = session.id
  persistSessions()
  return session
}

function switchSession(id: string): void {
  currentSessionId.value = id
}

// 会话挂靠（2026-10-02 业主定稿）：绑定事件的会话缩进嵌套在对应事件条目下，
// "会话"区只放不引用事件的通用会话（点"新建运维会话"产生的）
function sessionsOfIncident(incidentId: number): OpsSession[] {
  return sessions.value.filter((s) => s.incidentId === incidentId)
}

const genericSessions = computed(() => sessions.value.filter((s) => s.incidentId == null))

/** 会话是否尚未被使用（用户没发过消息）：未使用的新会话不重复创建。 */
function isSessionUnused(session: OpsSession): boolean {
  return !session.messages.some((m) => m.role === 'user')
}

function closeSession(id: string): void {
  sessions.value = sessions.value.filter((s) => s.id !== id)
  if (currentSessionId.value === id) currentSessionId.value = sessions.value[0]?.id ?? ''
  if (!sessions.value.length) newSession()
  persistSessions()
}

// DB Agent 会话（store 单例，工作区组件与侧栏共用；2026-10-02 侧栏化）
const {
  conversations: dbConversations,
  currentId: dbCurrentId,
  newConversation: dbNewSession,
  loadConversation: dbOpenSession,
  removeConversation: dbRemoveSession,
  renameConversation: dbRenameSession,
} = useDbagentSessions()

// 侧栏会话重命名（增删改之"改"）：✎ 或双击标题进入行内编辑，
// Enter/失焦提交、Esc 取消；两组（诊断/DB）共用一套编辑态
const renameEditing = ref<{ kind: 'ops' | 'db'; id: string; draft: string } | null>(null)

// 分组折叠（2026-10-02 业主定稿）：事件分组与其下会话、通用会话区都能收起
const collapsedIncidents = ref<number[]>([])
const genericCollapsed = ref(false)
const dbCollapsed = ref(false)

// 改动日志（DB Agent 变更账本）：db-agent 人格下展示并可回滚。
// 条目默认收起只看人话摘要，点击展开前后明细——回滚按钮藏 in 展开区，
// 强迫看清改了什么再动手（业主：否则容易操作失误）
const dbChanges = ref<DbChangeLogItem[]>([])
const dbChangesOpen = ref(true)
const dbChangesLoading = ref(false)
const expandedChanges = ref<number[]>([])

const DB_CHANGE_STATUS_LABEL: Record<string, string> = {
  applied: '已应用',
  partial: '部分可回滚',
  rolled_back: '已回滚',
  rollback_failed: '回滚失败',
}

function toggleChangeExpand(changeId: number): void {
  expandedChanges.value = expandedChanges.value.includes(changeId)
    ? expandedChanges.value.filter((x) => x !== changeId)
    : [...expandedChanges.value, changeId]
}

/** 快照行的键值对（展开区展示，截 8 列防挤爆侧栏）。 */
function rowPairs(row: Record<string, unknown> | undefined): Array<[string, unknown]> {
  if (!row) return []
  return Object.entries(row).slice(0, 8)
}

async function loadDbChanges(): Promise<void> {
  if (persona.value !== 'db-agent' || dbChangesLoading.value) return
  dbChangesLoading.value = true
  try {
    dbChanges.value = (await listDbAgentChanges()).items
  } catch {
    /* 列表拉取失败不阻塞聊天，下一轮再取 */
  } finally {
    dbChangesLoading.value = false
  }
}

async function rollbackChange(change: DbChangeLogItem): Promise<void> {
  const ok = window.confirm(
    `回滚该变更？将执行 ${change.affected} 行的反向操作（目标表 ${change.target_table}），执行不可撤销。`)
  if (!ok) return
  try {
    await rollbackDbAgentChange(change.change_id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '回滚失败'
  }
  await loadDbChanges()
}

function toggleIncidentGroup(incidentId: number): void {
  collapsedIncidents.value = collapsedIncidents.value.includes(incidentId)
    ? collapsedIncidents.value.filter((x) => x !== incidentId)
    : [...collapsedIncidents.value, incidentId]
}

function startRename(kind: 'ops' | 'db', id: string, title: string): void {
  renameEditing.value = { kind, id, draft: title }
  // 聚焦全选：新 input 在 nextTick 后才存在，querySelector 一次即可（单实例编辑态）
  void nextTick().then(() => {
    const el = document.querySelector<HTMLInputElement>('.session-rename')
    if (el) {
      el.focus()
      el.select()
    }
  })
}

function commitRename(): void {
  const editing = renameEditing.value
  renameEditing.value = null
  const title = editing?.draft.trim() ?? ''
  if (!editing || !title) return
  if (editing.kind === 'ops') {
    const sess = sessions.value.find((s) => s.id === editing.id)
    if (sess && sess.title !== title) {
      sess.title = title.slice(0, 40)
      persistSessions()
    }
  } else {
    dbRenameSession(editing.id, title)
  }
}

/** 会话条目右侧相对时间（参考 ZCode 会话列表：刚刚 / N分钟 / N小时 / N天）。 */
function fmtRelative(ts?: number): string {
  if (!ts) return ''
  const diff = Date.now() - ts
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}小时前`
  return `${Math.floor(diff / 86_400_000)}天前`
}

function loadSessions(): void {
  try {
    const raw = localStorage.getItem(sessionsKeyFor(auth.user?.username ?? ''))
    const parsed = raw ? (JSON.parse(raw) as OpsSession[]) : []
    sessions.value = Array.isArray(parsed) && parsed.length ? parsed : []
  } catch {
    sessions.value = []
  }
  if (sessions.value.length) currentSessionId.value = sessions.value[0].id
  else newSession()
}

async function scrollBottom() {
  await nextTick()
  const el = transcriptEl.value
  if (el) el.scrollTop = el.scrollHeight
}

async function refresh() {
  // 2026-10-03 定位收敛：中控台只监测/预警，不再拉取知识变更（修改权力已收走）
  const [summary, incidentList, requestList] = await Promise.all([
    getRagStatus(),
    listRagIncidents(40),
    listRagRequests(20),
  ])
  status.value = summary
  incidents.value = incidentList.items
  requests.value = requestList.items
  // 刷新成功即清除历史错误：否则一次失败后 UI 永久卡在错误页，
  // 15s 轮询即使恢复成功用户也看不到（对照组 RagConsoleView 同款）
  error.value = ''
  // 2026-10-02 起不再自动选事件：默认会话是"不针对事件"的通用会话（业主定稿），
  // 绑定事件的会话由用户点击活跃事件显式打开
}

async function selectIncident(incidentId: number) {
  // 点事件 = 打开（或切到）绑定该事件的会话，ZCode 打开项目式；详情另取一次缓存
  // 打开时自动展开该分组（折叠状态下点事件也要能看到会话去哪了）
  collapsedIncidents.value = collapsedIncidents.value.filter((x) => x !== incidentId)
  const existing = sessions.value.find((s) => s.incidentId === incidentId)
  if (existing) currentSessionId.value = existing.id
  else {
    const item = incidents.value.find((i) => i.incident_id === incidentId)
    newSession(incidentId,
      String(item?.title || item?.code || `事件 #${incidentId}`).slice(0, 24))
  }
  try {
    incidentDetails.set(incidentId, await getRagIncident(incidentId))
    selectedIncident.value = incidentDetails.get(incidentId) ?? null
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载事件详情失败'
  }
}

function fmtTime(value?: string | null): string {
  return value ? value.slice(5, 16).replace('T', ' ') : '-'
}


/** 格式化阶段耗时，空值显示短横线。 */
function fmtMs(value?: number | null): string {
  return value == null ? '-' : `${value}ms`
}


/** 点击请求只读取并缓存详情，不触发模型分析。 */
async function selectRequest(requestUid: string) {
  if (selectedRequest.value?.request_uid === requestUid) return
  const seq = ++requestSeq
  requestError.value = ''
  const cached = requestCache.get(requestUid)
  if (cached) {
    if (seq === requestSeq) selectedRequest.value = cached
    return
  }
  requestLoading.value = true
  try {
    const detail = await getRagRequest(requestUid)
    requestCache.set(requestUid, detail)
    if (seq === requestSeq) selectedRequest.value = detail
  } catch (err) {
    if (seq === requestSeq) {
      selectedRequest.value = null
      requestError.value = err instanceof Error ? err.message : '请求详情加载失败'
    }
  } finally {
    if (seq === requestSeq) requestLoading.value = false
  }
}


/** 构造包含已读取阶段数据的问题，避免模型误判请求不存在。 */
function requestAnalysisQuestion(): string {
  const item = selectedRequest.value
  if (!item) return ''
  const spans = item.spans.slice(-50).map((span) => [
    `stage=${span.stage}`,
    `round=${span.round_no}`,
    `status=${span.status}`,
    `latency_ms=${span.latency_ms ?? '-'}`,
    `result_count=${span.result_count ?? '-'}`,
    `error_code=${span.error_code || '-'}`,
  ].join(' '))
  const chunk_lines = item.content.chunks.slice(0, 8).map((chunk) => [
    `chunk_key=${chunk.chunk_key}`,
    `heading=${chunk.heading || '-'}`,
    `content=${(chunk.content || '').slice(0, 600)}`,
  ].join('; '))
  return [
    `请分析请求 ${item.request_uid} 的执行链路。`,
    '以下是已从请求级 trace 读取的结构化数据，不要声称该请求不存在：',
    `route=${item.route || '-'}; status=${item.status}`,
    `model=${item.selected_model || '-'}; provider=${item.provider || '-'}`,
    `first_token_ms=${item.first_token_ms ?? '-'}; total_ms=${item.total_ms ?? '-'}`,
    `input_tokens=${item.input_tokens ?? '-'}; output_tokens=${item.output_tokens ?? '-'}`,
    `用户问题=${item.content.query || '-'}`,
    `最终回答=${(item.content.answer || '').slice(0, 1200)}`,
    '阶段数据：',
    ...spans,
    '召回内容：',
    ...chunk_lines,
  ].join('\n')
}


/** 只有用户点击按钮时才把选中的请求交给运维助手分析。 */
async function analyzeSelectedRequest() {
  const question = requestAnalysisQuestion()
  if (!question) return
  await send(question)
}


/** 活跃事件编号表（2026-10-03 业主定稿）：侧栏显示的编号即此表编号，
 * 用户问"事件 2"时模型据此映射到具体 incident。随每次提问注入。 */
function incidentRoster(): string {
  if (!activeIncidents.value.length) return '当前无活跃事件。'
  const lines = activeIncidents.value.map((item, idx) => (
    `${idx + 1}. [事件#${item.incident_id}] 规则 ${item.code}｜严重度 ${item.severity}｜状态 ${item.status}｜${item.title}`
  ))
  return '活跃事件编号表（用户提到"事件 N"即指第 N 行）：\n' + lines.join('\n')
}

function contextQuestion(question: string): string {
  const lines: string[] = [incidentRoster()]
  if (selectedIncident.value) {
    const item = selectedIncident.value
    const no = activeIncidents.value.findIndex((i) => i.incident_id === item.incident_id) + 1
    lines.push(`当前关注事件 ${no}（Incident #${item.incident_id}）：规则 ${item.code}，严重度 ${item.severity}，状态 ${item.status}，标题 ${item.title}`)
    // 样本证据随上下文回带（2026-09-14 审计 P2）：原先只带标题/规则/状态，
    // 模型只能依赖全局最近 20 条，目标事件的证据可能在上下文里缺失
    const samples = (item.verdicts || []).slice(0, 3)
    if (samples.length) {
      lines.push('样本证据（含建议修复参数 patch）：')
      for (const sample of samples) {
        lines.push([
          `query=${sample.query_snippet || '-'}`,
          `evidence=${(sample.evidence || []).slice(0, 3).join(' | ') || '-'}`,
          `patch=${sample.patch && Object.keys(sample.patch).length ? JSON.stringify(sample.patch) : '-'}`,
        ].join('; '))
      }
    }
  }
  lines.push(question)
  return lines.join('\n')
}

async function send(question?: string) {
  const text = (question ?? input.value).trim()
  if (!text || sending.value) return
  const history = messages.value.slice(-8).map((message) => ({
    role: message.role,
    content: message.content,
  }))
  input.value = ''
  messages.value.push({
    id: uid(), role: 'user', content: text, createdAt: Date.now(),
  })
  // 通用会话首问后改题为问题摘要（ZCode 项目列表以内容辨识的口径）
  const sess = currentSession.value
  if (sess && sess.title.startsWith('新会话')) sess.title = text.slice(0, 16)
  await scrollBottom()

  // 流式占位：增量填充同一条助手消息，避免长回答整段白屏等待（P2-1）
  const assistantId = uid()
  messages.value.push({
    id: assistantId, role: 'assistant', content: '', createdAt: Date.now(),
  })
  const patchAssistant = (content: string) => {
    const target = messages.value.find((m) => m.id === assistantId)
    if (target) target.content = content
  }
  persistSessions()
  await scrollBottom()

  sending.value = true
  try {
    await streamRagOpsAsk({
      question: contextQuestion(text),
      history,
      onEvent: (event) => {
        const current = messages.value.find((m) => m.id === assistantId)
        const streamed = current?.content ?? ''
        if (event.type === 'answer_chunk') {
          patchAssistant(streamed + String(event.content ?? ''))
          void scrollBottom()
        } else if (event.type === 'answer_complete') {
          // SSE 原始负载的 content 类型是 unknown，须显式收敛为 string，
          // 否则 `?? '' || streamed` 会被推成 {} 类型导致 vue-tsc 编译失败（与 chat.ts 同口径）。
          const finalText = typeof event.content === 'string' ? event.content : ''
          patchAssistant(finalText || streamed)
          void scrollBottom()
        } else if (event.type === 'answer_error') {
          patchAssistant(`查询失败：${event.content ?? '未知错误'}`)
        }
        // thought 只是推理流，运维面板不展示，但保持连接活跃
      },
    })
    // 流式接口不回传 status_snapshot；成功后拉一次，恢复旧 /ask 的即时刷新体验
    void refresh().catch(() => {})
  } catch (err) {
    const msg = `查询失败：${err instanceof Error ? err.message : '未知错误'}`
    const current = messages.value.find((m) => m.id === assistantId)
    if (current && current.content) patchAssistant(`${current.content}\n\n${msg}`)
    else patchAssistant(msg)
  } finally {
    sending.value = false
    persistSessions()
    await scrollBottom()
  }
}

async function ackIncident() {
  if (!selectedIncident.value) return
  try {
    await acknowledgeIncident(selectedIncident.value.incident_id)
    await selectIncident(selectedIncident.value.incident_id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '确认操作失败'
  }
}

async function closeIncident() {
  if (!selectedIncident.value) return
  try {
    await resolveIncident(selectedIncident.value.incident_id)
    await selectIncident(selectedIncident.value.incident_id)
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '关闭操作失败'
  }
}

async function runDiagnosis() {
  if (!selectedIncident.value) return
  try {
    await planRagAction(
      selectedIncident.value.incident_id,
      'rerun_diagnosis',
      {},
      '来自 RAG 运维会话',
    )
  } catch (err) {
    // 动作没建成就不能发"动作已经创建"，否则运维助手基于错误前提作答
    // （2026-09-19 审查 P2-6）
    error.value = err instanceof Error ? err.message : '创建诊断动作失败'
    return
  }
  await send('动作已经创建，请说明接下来如何验证结果')
}

function clearConversation(): void {
  // 顶部新建 = 开一个"不针对事件"的通用新会话（2026-10-02 业主定稿）；
  // 已有未使用的通用会话时直接维持/切回它，不重复堆空会话
  const existing = sessions.value.find((s) => s.incidentId == null && isSessionUnused(s))
  if (existing) {
    currentSessionId.value = existing.id
    return
  }
  newSession()
}

// 待审批角标（T1，2026-10-06）：轮询任务列表，awaiting_* 即待审批队列；
// 仅持 dbagent.approve 能力者轮询（无能力者零请求）。点击顶栏按钮切到
// 数据库Agent 即见审批卡。
const pendingApprovals = ref(0)
let approvalPoll: ReturnType<typeof setInterval> | null = null

async function refreshPendingApprovals(): Promise<void> {
  if (!hasCap(auth.user, 'dbagent.approve')) return
  try {
    const tasks = await listDbAgentTasks()
    pendingApprovals.value = tasks.filter(
      (t) => t.status === 'awaiting_approval' || t.status === 'awaiting_plan_approval',
    ).length
  } catch {
    /* 轮询失败静默：下个周期重试 */
  }
}

watch(
  () => hasCap(auth.user, 'dbagent.approve'),
  (ok) => {
    if (ok && approvalPoll === null) {
      void refreshPendingApprovals()
      approvalPoll = setInterval(() => void refreshPendingApprovals(), 20000)
    } else if (!ok && approvalPoll !== null) {
      clearInterval(approvalPoll)
      approvalPoll = null
      pendingApprovals.value = 0
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (approvalPoll !== null) clearInterval(approvalPoll)
})

onMounted(async () => {
  if (!auth.profileLoaded) {
    try {
      await auth.loadProfile()
    } catch {
      return
    }
  }
  // 守卫：admin 全功能；子账号需 dbagent.view（诊断助手相关端点仍限 admin，
  // 入口按钮对子账号隐藏，见模板 v-if="isAdmin"）
  if (!isAdmin(auth.user) && !hasCap(auth.user, 'dbagent.view')) {
    forbidden.value = true
    loading.value = false
    return
  }
  if (!isAdmin(auth.user)) {
    // 子账号不拉 RAG 监测上下文（其端点限 admin），直接进 DB Agent 会话
    persona.value = 'db-agent'
    loading.value = false
    return
  }
  loadSessions()
  try {
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
    await scrollBottom()
  }
  refreshTimer = window.setInterval(() => void refresh().catch(() => {}), 15000)
  // 改动日志：切到 db-agent 即拉取，之后跟随 15s 主刷新节拍
  watch(persona, (p) => { if (p === 'db-agent') void loadDbChanges() }, { immediate: true })
  window.setInterval(() => void loadDbChanges(), 15000)
})

onUnmounted(() => {
  if (refreshTimer) window.clearInterval(refreshTimer)
})
</script>

<template>
  <div class="ops-shell" :class="{ 'wide-mode': persona !== 'rag-ops' }">
    <!-- DB Agent 用自己的会话侧栏；诊断助手与问数共用运维侧栏 -->
    <aside v-if="persona !== 'db-agent'" class="ops-sidebar">
      <button class="new-chat" @click="clearConversation">新建运维会话</button>

      <!-- 事件即分组节点（2026-10-03：行首 chevron 折叠/展开其下会话；带顺序编号，
           编号表随提问注入给助手，用户问"事件 N"即可精细追问。行本体 div 因
           button 不能嵌套 button）：点标题开（或切到）绑定会话 -->
      <div class="section-label">活跃事件</div>
      <div class="context-list incident-tree">
        <template v-for="(item, idx) in activeIncidents" :key="item.incident_id">
          <div
            class="context-item incident-node"
            :class="{ active: selectedIncident?.incident_id === item.incident_id }"
          >
            <button
              class="chevron-btn"
              :class="{ closed: collapsedIncidents.includes(item.incident_id) }"
              :title="collapsedIncidents.includes(item.incident_id) ? '展开该事件的会话' : '折叠该事件的会话'"
              @click.stop="toggleIncidentGroup(item.incident_id)"
            >▾</button>
            <button class="incident-open" @click="selectIncident(item.incident_id)">
              <span class="context-title">
                <span class="incident-no">{{ idx + 1 }}.</span>
                {{ item.title || item.code }}
              </span>
              <span class="context-meta">
                {{ label_severity(item.severity) }} ·
                {{ label_status(item.status) }} ·
                {{ item.occurrence_count }} 次
              </span>
            </button>
          </div>
          <template v-if="!collapsedIncidents.includes(item.incident_id)">
            <div
              v-for="session in sessionsOfIncident(item.incident_id)"
              :key="session.id"
              class="context-item session-item nested"
              :class="{ active: session.id === currentSessionId }"
            >
              <button
                class="session-open"
                :title="session.title"
                @click="switchSession(session.id)"
                @dblclick="startRename('ops', session.id, session.title)"
              >
                <input
                  v-if="renameEditing?.kind === 'ops' && renameEditing.id === session.id"
                  v-model="renameEditing.draft"
                  class="session-rename"
                  @click.stop
                  @keydown.enter.prevent="commitRename"
                  @keydown.esc.prevent="renameEditing = null"
                  @blur="commitRename"
                />
                <template v-else>
                  <span class="session-title">{{ session.title }}</span>
                  <span class="session-time">{{ fmtRelative(session.createdAt) }}</span>
                </template>
              </button>
              <button
                class="session-act"
                title="重命名会话"
                @click="startRename('ops', session.id, session.title)"
              >✎</button>
              <button class="session-act" title="删除该会话" @click="closeSession(session.id)">×</button>
            </div>
          </template>
        </template>
        <div v-if="!activeIncidents.length" class="empty">暂无活跃事件</div>
      </div>

      <!-- 通用会话（不引用事件）：由"新建运维会话"产生；标题行可折叠 -->
      <button class="section-toggle" @click="genericCollapsed = !genericCollapsed">
        <span class="chevron" :class="{ closed: genericCollapsed }">▾</span>
        <span>会话</span>
        <span class="count">{{ genericSessions.length }}</span>
      </button>
      <div v-show="!genericCollapsed" class="context-list session-list">
        <div
          v-for="session in genericSessions"
          :key="session.id"
          class="context-item session-item"
          :class="{ active: session.id === currentSessionId }"
        >
          <button
            class="session-open"
            :title="session.title"
            @click="switchSession(session.id)"
            @dblclick="startRename('ops', session.id, session.title)"
          >
            <input
              v-if="renameEditing?.kind === 'ops' && renameEditing.id === session.id"
              v-model="renameEditing.draft"
              class="session-rename"
              @click.stop
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent="renameEditing = null"
              @blur="commitRename"
            />
            <template v-else>
              <span class="session-title">{{ session.title }}</span>
              <span class="session-time">{{ fmtRelative(session.createdAt) }}</span>
            </template>
          </button>
          <button
            class="session-act"
            title="重命名会话"
            @click="startRename('ops', session.id, session.title)"
          >✎</button>
          <button class="session-act" title="删除该会话" @click="closeSession(session.id)">×</button>
        </div>
      </div>

      <div class="section-label">最近请求</div>
      <div class="request-list">
        <button
          v-for="item in requests.slice(0, 8)"
          :key="item.request_uid"
          class="request-item"
          :class="{ active: selectedRequest?.request_uid === item.request_uid }"
          :title="`查看请求 ${item.request_uid}`"
          @click="selectRequest(item.request_uid)"
        >
          <span>{{ label_route(item.route) }}</span>
          <small>{{ fmtTime(item.created_at) }} · {{ label_status(item.status) }}</small>
        </button>
      </div>
    </aside>

    <!-- DB Agent 人格共用同款左边栏（2026-10-02 业主定稿：两个 Agent 左栏同构，
         增删改齐备）；隐藏的是右侧洞察栏，布局见 .dbagent-mode -->
    <aside v-else class="ops-sidebar">
      <button class="new-chat" @click="dbNewSession">新建会话</button>

      <button class="section-toggle" @click="dbCollapsed = !dbCollapsed">
        <span class="chevron" :class="{ closed: dbCollapsed }">▾</span>
        <span>会话</span>
        <span class="count">{{ dbConversations.length }}</span>
      </button>
      <div v-show="!dbCollapsed" class="context-list session-list dbagent-session-list">
        <div
          v-for="conv in dbConversations"
          :key="conv.id"
          class="context-item session-item"
          :class="{ active: conv.id === dbCurrentId }"
        >
          <button
            class="session-open"
            :title="conv.title"
            @click="dbOpenSession(conv.id)"
            @dblclick="startRename('db', conv.id, conv.title)"
          >
            <input
              v-if="renameEditing?.kind === 'db' && renameEditing.id === conv.id"
              v-model="renameEditing.draft"
              class="session-rename"
              @click.stop
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent="renameEditing = null"
              @blur="commitRename"
            />
            <template v-else>
              <span class="session-title">{{ conv.title }}</span>
              <span class="session-time">{{ fmtRelative(conv.createdAt) }}</span>
            </template>
          </button>
          <button
            class="session-act"
            title="重命名会话"
            @click="startRename('db', conv.id, conv.title)"
          >✎</button>
          <button class="session-act" title="删除该会话" @click="dbRemoveSession(conv.id)">×</button>
        </div>
        <p v-if="!dbConversations.length" class="empty">暂无历史会话，发一个任务即自动建会话</p>
      </div>

      <!-- 改动日志（变更账本）：每笔批准的写操作可在此回滚 -->
      <button class="section-toggle" @click="dbChangesOpen = !dbChangesOpen">
        <span class="chevron" :class="{ closed: !dbChangesOpen }">▾</span>
        <span>改动日志</span>
        <span class="count">{{ dbChanges.length }}</span>
      </button>
      <div v-show="dbChangesOpen" class="context-list change-list">
        <div v-for="change in dbChanges" :key="change.change_id" class="change-item">
          <!-- 收起态：HG + 状态 + 一句人话摘要；点击展开明细 -->
          <button class="change-head" @click="toggleChangeExpand(change.change_id)">
            <span class="chevron" :class="{ closed: !expandedChanges.includes(change.change_id) }">▾</span>
            <span class="change-ref">HG{{ change.change_id }}</span>
            <span class="change-status" :class="change.status">{{ DB_CHANGE_STATUS_LABEL[change.status] ?? change.status }}</span>
            <span class="change-summary" :title="change.summary || change.sql_text">
              {{ change.summary || change.sql_text }}
            </span>
          </button>

          <!-- 展开区：具体改动（前后行逐列）、完整 SQL、来龙去脉、回滚 -->
          <div v-show="expandedChanges.includes(change.change_id)" class="change-detail">
            <div class="snap-grid">
              <div class="snap-col">
                <span class="snap-label">改前</span>
                <div v-for="[k, v] in rowPairs(change.before_rows[0])" :key="'b' + k" class="snap-row">
                  <span class="snap-key">{{ k }}</span><span class="snap-val">{{ v }}</span>
                </div>
                <span v-if="!change.before_rows.length" class="snap-empty">无（新增行）</span>
              </div>
              <div class="snap-col">
                <span class="snap-label">改后</span>
                <div v-for="[k, v] in rowPairs(change.after_rows[0])" :key="'a' + k" class="snap-row">
                  <span class="snap-key">{{ k }}</span><span class="snap-val">{{ v }}</span>
                </div>
                <span v-if="!change.after_rows.length" class="snap-empty">已删除</span>
              </div>
            </div>
            <pre class="change-sql-full">{{ change.sql_text }}</pre>
            <p class="change-origin">
              任务：{{ change.question || change.task_id }} ·
              批准人 {{ change.approved_by || '-' }} ·
              {{ new Date(change.created_at).toLocaleString() }}
            </p>
            <p v-if="change.rollback_error" class="change-err">{{ change.rollback_error }}</p>
            <button
              v-if="change.status === 'applied' && (change.reverse_ops?.length ?? 0) > 0"
              class="rollback-btn"
              @click="rollbackChange(change)"
            >回滚 HG{{ change.change_id }}</button>
          </div>
        </div>
        <p v-if="!dbChanges.length" class="empty">暂无变更记录</p>
      </div>
    </aside>

    <main class="ops-chat">
      <!-- 固定顶栏（2026-10-02 业主定稿）：数据台/诊断助手/数据库Agent 并列，
           切换只换下方内容；旧头部（标题+上下文+右上按钮）按反馈移除 -->
      <nav class="top-nav" aria-label="功能导航">
        <button v-if="isAdmin(auth.user)" @click="goConsoleData">数据台</button>
        <button
          v-if="isAdmin(auth.user)"
          :class="{ active: persona === 'rag-ops' }"
          @click="persona = 'rag-ops'"
        >诊断助手</button>
        <button :class="{ active: persona === 'db-agent' }" @click="persona = 'db-agent'">数据库Agent<span v-if="pendingApprovals" class="nav-badge">{{ pendingApprovals }}</span></button>
      </nav>
      <p v-if="persona === 'rag-ops' && selectedIncident" class="top-context">
        当前上下文：事件 #{{ selectedIncident.incident_id }} · {{ selectedIncident.title }}
      </p>

      <section v-if="forbidden" class="state">当前账号没有 RAG 管理权限。</section>
      <section v-else-if="loading" class="state">正在加载 RAG 上下文...</section>
      <section v-else-if="error" class="state danger">{{ error }}</section>

      <template v-else>
        <!-- DB Agent 人格：页内嵌入工作台（任务/时间线/审批卡/报告）；
             问数不再独立成窗（2026-10-03 并入诊断助手：疑似查数问题后端实查后作答） -->
        <RagDbAgentWorkspace v-if="persona === 'db-agent'" />
        <template v-else>
        <div ref="transcriptEl" class="transcript">
          <article
            v-for="message in messages"
            :key="message.id"
            class="ops-message"
            :class="message.role"
          >
            <div class="role">{{ message.role === 'user' ? '我' : '运维助手' }}</div>
            <div
              v-if="message.role === 'assistant'"
              class="message-body markdown"
              v-html="renderOpsMarkdown(message.content)"
            ></div>
            <div v-else class="message-body">{{ message.content }}</div>
          </article>
          <article v-if="sending" class="ops-message assistant">
            <div class="role">运维助手</div>
            <div class="message-body thinking">正在分析监测数据...</div>
          </article>
        </div>

        <footer class="composer">
          <textarea
            v-model="input"
            rows="1"
            placeholder="询问异常、根因、影响范围或修复动作"
            @keydown.enter.exact.prevent="send()"
          ></textarea>
          <button :disabled="sending || !input.trim()" @click="send()">
            {{ sending ? '分析中' : '发送' }}
          </button>
        </footer>
        </template>
      </template>
    </main>

    <aside v-if="persona === 'rag-ops' && status" class="ops-insight">
      <section>
        <h2>实时状态</h2>
        <dl class="metric-list">
          <div><dt>请求链路</dt><dd>{{ status.requests_24h.total }}</dd></div>
          <div><dt>失败请求</dt><dd>{{ status.requests_24h.failed }}</dd></div>
          <div><dt>端到端 95 分位耗时</dt><dd>{{ status.requests_24h.p95_latency_ms }}ms</dd></div>
          <div><dt>活跃事件</dt><dd>{{ activeIncidents.length }}</dd></div>
          <div><dt>待审批动作</dt><dd>{{ status.health_meta.waiting_actions }}</dd></div>
          <div><dt>链路延迟</dt><dd>{{ status.health_meta.trace_lag_seconds ?? '-' }}s</dd></div>
        </dl>
      </section>

      <!-- 2026-10-03 定位收敛：知识变更的审批/提议区块整体移除，
           中控台只监测预警、出报告与建议，不再握有修改 RAG 的权力 -->

      <section v-if="requestLoading" class="request-context">
        <h2>请求上下文</h2>
        <p class="empty">正在读取请求阶段数据...</p>
      </section>

      <section v-else-if="requestError" class="request-context">
        <h2>请求上下文</h2>
        <p class="error-text">{{ requestError }}</p>
      </section>

      <section v-else-if="selectedRequest" class="request-context">
        <div class="request-heading">
          <h2>请求上下文</h2>
          <button class="text-button" @click="selectedRequest = null">清除</button>
        </div>
        <p class="request-uid">{{ selectedRequest.request_uid }}</p>
        <dl class="request-meta">
          <div><dt>状态</dt><dd>{{ label_status(selectedRequest.status) }}</dd></div>
          <div><dt>路径</dt><dd>{{ label_route(selectedRequest.route) }}</dd></div>
          <div><dt>模型</dt><dd>{{ selectedRequest.selected_model || '-' }}</dd></div>
          <div>
            <dt>首包 / 总耗时</dt>
            <dd>{{ fmtMs(selectedRequest.first_token_ms) }} / {{ fmtMs(selectedRequest.total_ms) }}</dd>
          </div>
        </dl>

        <div class="content-block">
          <span class="content-label">用户问题</span>
          <p class="content-text">{{ selectedRequest.content.query || '未记录' }}</p>
        </div>

        <details class="content-detail" open>
          <summary>最终回答 <span>{{ selectedRequest.content.answer_len }} 字</span></summary>
          <pre class="content-pre">{{ selectedRequest.content.answer || '未记录' }}</pre>
        </details>

        <details class="content-detail">
          <summary>召回内容 <span>{{ selectedRequest.content.chunks.length }} 块</span></summary>
          <div
            v-for="run in selectedRequest.content.retrieval_runs"
            :key="run.created_at + run.method"
            class="retrieval-run"
          >
            <span>{{ label_retrieval_method(run.method) }}</span>
            <span>{{ run.returned_count ?? 0 }} 条 · {{ fmtMs(run.latency_ms) }}</span>
          </div>
          <article
            v-for="chunk in selectedRequest.content.chunks"
            :key="chunk.chunk_key"
            class="chunk-item"
          >
            <strong>{{ chunk.heading || chunk.chunk_key }}</strong>
            <p>{{ chunk.content }}</p>
          </article>
          <p v-if="!selectedRequest.content.chunks.length" class="empty">
            未记录可展示的知识片段。
          </p>
        </details>

        <details class="content-detail">
          <summary>执行阶段 <span>{{ selectedRequest.spans.length }} 个</span></summary>
          <div class="span-list">
            <div v-for="span in selectedRequest.spans" :key="span.span_uid" class="span-row">
              <span>{{ label_stage(span.stage) }}<small v-if="span.round_no > 1"> #{{ span.round_no }}</small></span>
              <span>{{ label_status(span.status) }}</span>
              <span>{{ fmtMs(span.latency_ms) }}</span>
            </div>
            <p v-if="!selectedRequest.spans.length" class="empty">该请求没有阶段 span。</p>
          </div>
        </details>
        <button class="analyze-request" :disabled="sending" @click="analyzeSelectedRequest">
          {{ sending ? '分析中' : '分析此请求' }}
        </button>
      </section>

      <section v-if="selectedIncident" class="incident-context">
        <h2>事件上下文</h2>
        <p class="incident-title">{{ selectedIncident.title }}</p>
        <p>{{ selectedIncident.suspected_cause || '暂无根因说明' }}</p>
        <p class="recommendation">{{ selectedIncident.recommended_action || '暂无建议动作' }}</p>
        <div class="context-actions">
          <button v-if="selectedIncident.status === 'open'" @click="ackIncident">认领</button>
          <button v-if="!['resolved', 'closed'].includes(selectedIncident.status)" class="danger" @click="closeIncident">解决</button>
          <button @click="runDiagnosis">重跑诊断</button>
        </div>
      </section>
    </aside>
  </div>
</template>

<style scoped src="../styles/rag-ops-chat.css"></style>
