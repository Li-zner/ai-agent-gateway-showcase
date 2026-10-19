/**
 * DB Agent 会话 store（模块级单例）：会话数据从 RagDbAgentWorkspace 组件
 * 提升到这里，供外壳侧栏（会话列表 UI）与工作区组件（转录/轮询）共同消费。
 * 持久化：localStorage dbagent_history_v1（保留旧键，历史无缝迁移）。
 */
import { computed, ref } from 'vue'

export interface DbTurn {
  id: string
  question: string
  taskId: string | null
  status: 'running' | 'awaiting_approval' | 'awaiting_plan_approval' | 'done' | 'failed'
  stepLines: string[]
  plan: {
    goal: string
    steps: string[]
    decision: string | null
    decided_by?: string
  } | null
  proposal: {
    kind?: 'single' | 'batch'
    sql?: string
    params?: unknown[]
    reason: string
    decision: string | null
    preview?: { kind: string; preview_sql: string | null; rows: Array<Record<string, unknown>>; note: string }
    risk: {
      level: 'L0' | 'L1' | 'L2' | 'unknown'
      reasons: string[]
      affected: number
      table_rows: number
    }
    /** 重大变更（影响 ≥ 全表 15%）：仅最高管理员可批准 */
    critical?: boolean
    statements?: Array<{ sql: string; params: unknown[] }>
    previews?: Array<{ kind: string; preview_sql: string | null; rows: Array<Record<string, unknown>>; note: string }>
  } | null
  report: string | null
  error: string | null
}

export interface DbConversation {
  id: string
  title: string
  createdAt: number
  /** 跨会话记忆开关（服务端 allow_cross）：开启后该会话可 recall 同账号其他会话 */
  allowCross?: boolean
  turns: DbTurn[]
}

const STORE_KEY = 'dbagent_history_v1'

function load(): DbConversation[] {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    const parsed = raw ? (JSON.parse(raw) as DbConversation[]) : []
    return Array.isArray(parsed) ? parsed.slice(0, 30) : []
  } catch {
    return [] // 历史损坏不致命：从空开始
  }
}

const conversations = ref<DbConversation[]>(load())
const currentId = ref<string | null>(conversations.value[0]?.id ?? null)

function persist(): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(conversations.value.slice(0, 30)))
  } catch {
    /* 存储满等异常：不影响当前会话 */
  }
}

function uid(): string {
  return `conv_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`
}

const current = computed<DbConversation | null>(
  () => conversations.value.find((c) => c.id === currentId.value) ?? null,
)

/** 发送首条任务时确保存在会话（标题取问题摘要），返回目标会话。 */
function ensureConversation(titleSeed: string): DbConversation {
  if (current.value) return current.value
  const conv: DbConversation = {
    id: uid(),
    title: titleSeed.slice(0, 24),
    createdAt: Date.now(),
    allowCross: false,
    turns: [],
  }
  conversations.value.unshift(conv)
  currentId.value = conv.id
  persist()
  return conv
}

/** 新建会话：已在空态（未使用的会话）时维持现状，不重复置空（业主定稿）。 */
function newConversation(): void {
  if (currentId.value === null) return
  currentId.value = null
}

function loadConversation(id: string): void {
  currentId.value = id
}

function removeConversation(id: string): void {
  conversations.value = conversations.value.filter((c) => c.id !== id)
  if (currentId.value === id) currentId.value = conversations.value[0]?.id ?? null
  persist()
}

/** 重命名（侧栏 ✎）：空标题忽略，不改名。 */
function renameConversation(id: string, title: string): void {
  const conv = conversations.value.find((c) => c.id === id)
  if (conv && title.trim()) {
    conv.title = title.trim().slice(0, 40)
    persist()
  }
}

export function useDbagentSessions() {
  return {
    conversations,
    currentId,
    current,
    persist,
    ensureConversation,
    newConversation,
    loadConversation,
    removeConversation,
    renameConversation,
  }
}
