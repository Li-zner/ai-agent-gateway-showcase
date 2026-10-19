/** 聊天态（Pinia）：多会话(localStorage 按人格隔离) + 可取消流式收发 + 评分/删除/重生成 */
import { defineStore } from 'pinia'
import { streamChat, type ChatStreamHandler } from '../api/chat'
import {
  deleteConversation,
  getConversationProfile, patchConversationProfile,
} from '../api/conversation'
import { ApiError } from '../api/http'
import { fetchPersonas, type Persona } from '../api/personas'
import i18n, { currentLocale } from '../locales'
import { cleanReasoning, toolDisplayName } from '../utils/reasoning'
import { SseEventType } from '../enums'
import { newConversationId, newMessageId, newSessionId, loadSessions, saveSessions } from '../utils/sessions'
import type { ChatSession } from '../utils/sessions'
import {
  conversationTitle, emptyConversationProfile, extractProfileUpdates,
  mergeConversationProfile, needsContextCompression, normalizeConversationProfile,
  weatherSnapshotFromTool,
} from '../utils/conversationProfile'
import { useAuthStore } from './auth'

let streamCompletion: Promise<void> = Promise.resolve()
let resolveStreamCompletion: (() => void) | null = null

type StreamEvent = Parameters<ChatStreamHandler>[0]

/** 统一文案入口，流事件与消息组装共用。 */
function translate(key: string, params?: Record<string, unknown>): string {
  return i18n.global.t(key, params ?? {})
}

/** 受限用户（GitHub 试用）每次回答后静默刷新 profile，额度徽标才能实时反映后端扣减 */
function refreshQuotaBadge(): void {
  const auth = useAuthStore()
  if (auth.user?.quota_limited) void auth.loadProfile().catch(() => {})
}

/** 最近一条用户原话；用于画像加载后重算自动标题。 */
function latestUserContent(session: ChatSession): string {
  const message = [...session.messages].reverse().find((item) => item.role === 'user')
  return message?.content ?? ''
}

/** 自动标题仅在首轮确定一次并冻结；手动重命名后不再覆盖。 */
function refreshAutoTitle(session: ChatSession, query = ''): void {
  if (session.titleMode === 'manual' || session.titleLocked) return
  const source = query.trim() || latestUserContent(session)
  if (!source) return
  session.title = conversationTitle(source, session.profile)
  session.titleLocked = true
}

/** 组装本轮用户与助手消息，返回服务端查询和响应式助手引用。 */
function appendOutgoingTurn(
  session: ChatSession,
  query: string,
  fileIds: string[],
): { sendQuery: string; assistantView: ChatMessage } {
  const sendQuery = query || translate('send_file_query')
  const displayText = query || translate('send_file_display')
  const userMsg: ChatMessage = {
    id: newMessageId(),
    role: 'user',
    content: displayText,
    timestamp: Date.now(),
  }
  if (fileIds.length) userMsg.fileIds = fileIds
  session.messages.push(userMsg)
  const assistant: ChatMessage = {
    id: newMessageId(),
    role: 'assistant',
    content: '',
    reasoning: '',
    tools: [],
    timestamp: Date.now(),
    streaming: true,
  }
  session.messages.push(assistant)
  // 通过响应式代理取引用，确保流式增量触发 Vue 更新。
  return { sendQuery, assistantView: session.messages[session.messages.length - 1] }
}

/** 工具调用只展示已注册的可读名称，避免泄露内部函数签名。 */
function appendToolCall(last: ChatMessage, event: StreamEvent): void {
  const name = String((event as { name?: string }).name ?? '')
  const label = toolDisplayName(name)
  if (label === name) return
  const args = (event as { args?: Record<string, unknown> }).args ?? {}
  const vals = Object.values(args).filter(
    (value) => typeof value === 'string' && String(value).trim(),
  )
  last.tools?.push({
    kind: 'tool',
    text: translate('calling_tool') + label + (vals.length ? `（${vals.join('，')}）` : ''),
  })
}

/** 工具失败静默跳过，不给用户负面信号。 */
function appendToolResult(last: ChatMessage, event: StreamEvent): void {
  const result = (event as {
    result?: { error?: string; restaurants?: unknown[]; hotels?: unknown[]; route?: unknown }
  }).result
  if (!result || result.error) return
  let summary = translate('query_done')
  if (result.restaurants) summary = translate('found_restaurants', { n: result.restaurants.length })
  else if (result.hotels) summary = translate('found_hotels', { n: result.hotels.length })
  else if (result.route) summary = translate('route_done')
  last.tools?.push({ kind: 'result', text: translate('result_prefix') + summary })
}

/** 将 SSE 事件应用到当前助手消息，保持原始处理顺序。 */
function handleStreamEvent(
  event: StreamEvent,
  session: ChatSession,
  context: { rawReasoning: string },
): void {
  const last = session.messages[session.messages.length - 1]
  if (!last) return
  if (event.type === SseEventType.AnswerChunk) {
    last.content += String(event.content ?? '')
  } else if (event.type === SseEventType.AnswerComplete) {
    const content = typeof event.content === 'string' ? event.content : ''
    if (content) last.content = content
    refreshQuotaBadge()
  } else if (event.type === SseEventType.ReasoningChunk) {
    context.rawReasoning += String(event.content ?? '')
    last.reasoning = cleanReasoning(context.rawReasoning)
  } else if (event.type === SseEventType.Thought) {
    const text = String(event.content ?? '').replace(/[🤔💭]/gu, '').trim()
    if (text) last.tools?.push({ kind: 'text', text })
  } else if (event.type === SseEventType.ToolCall) {
    appendToolCall(last, event)
  } else if (event.type === SseEventType.ToolResult) {
    appendToolResult(last, event)
    const name = String((event as { name?: string }).name ?? '')
    if (name === 'query_weather') {
      const weather = weatherSnapshotFromTool((event as { result?: unknown }).result)
      session.profile = mergeConversationProfile(session.profile, weather)
      session.updatedAt = Date.now()
    }
  }
}

export interface ToolLine {
  kind: 'tool' | 'result' | 'text'
  text: string
}

export interface ChatMessage {
  /** 稳定 id：v-for key 用（历史数据无此字段，回退索引） */
  id?: string
  role: 'user' | 'assistant'
  content: string
  reasoning?: string
  tools?: ToolLine[]
  /** 附件：files 为展示名，fileIds 为服务端文件 id（重新生成时重发用） */
  files?: string[]
  fileIds?: string[]
  timestamp?: number
  rated?: boolean
  streaming?: boolean
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessions: [] as ChatSession[],
    currentSessionId: '',
    personas: [] as Persona[],
    currentPersonaId: 'unified',
    streaming: false,
    error: '',
    /** 地图页「去这里」写入的自动提问 prompt（ChatView 挂载时消费） */
    autoPrompt: '',
    /** 402 触发的绑定手机引导文案（非空时 ChatView 弹绑定弹窗） */
    bindPhoneTip: '',
    /** 流式中止控制器（send while streaming = cancel） */
    abort: null as AbortController | null,
    /** 当前会话数据绑定的账号；流式期间登出仍继续写原账号。 */
    storageOwner: '',
  }),

  getters: {
    currentSession(): ChatSession | undefined {
      return this.sessions.find((s) => s.id === this.currentSessionId) ?? this.sessions[0]
    },
    messages(): ChatMessage[] {
      return this.currentSession?.messages ?? []
    },
    /** 置顶会话浮到最前（稳定排序，组内保持原顺序） */
    sortedSessions(): ChatSession[] {
      return [...this.sessions].sort(
        (a, b) => Number(b.pinned ?? false) - Number(a.pinned ?? false),
      )
    },
  },

  actions: {
    /** 按当前人格加载本地会话；无会话则建一个 */
    initSessions() {
      try {
        const owner = useAuthStore().user?.username ?? ''
        this.storageOwner = owner
        if (!owner) {
          this.sessions = []
          this.currentSessionId = ''
          return
        }
        this.sessions = loadSessions(this.currentPersonaId, owner)
        if (!this.sessions.length) this.createSession()
        this.currentSessionId = this.sessions[0].id
        void this.loadProfileForSession(this.currentSessionId)
      } catch (e) {
        console.error('[chat.initSessions] FAILED:', e)  // 会话加载失败需可见
      }
    },

    createSession() {
      const s: ChatSession = {
        id: newSessionId(),
        personaId: this.currentPersonaId,
        title: '新对话',
        titleMode: 'auto',
        conversationId: newConversationId(),
        messages: [],
        updatedAt: Date.now(),
        profile: emptyConversationProfile(),
      }
      this.sessions.unshift(s)
      this.currentSessionId = s.id
      this.persist()
    },

    switchSession(id: string) {
      if (this.streaming) return
      this.currentSessionId = id
      this.persist()
      void this.loadProfileForSession(id)
    },

    async deleteSession(id: string): Promise<boolean> {
      if (this.sessions.length <= 1) return false
      // 2026-09-11 规则审查 P1：流式进行中禁止删除当前会话——否则服务端记忆
      // 被删而流仍在写，产生孤儿会话（对照 switchSession/switchPersona 的守卫）
      if (this.streaming && id === this.currentSessionId) return false
      const session = this.sessions.find((s) => s.id === id)
      if (!session) return false
      try {
        await deleteConversation(session.conversationId)
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 404)) throw e
      }
      this.sessions = this.sessions.filter((s) => s.id !== id)
      if (this.currentSessionId === id) this.currentSessionId = this.sessions[0].id
      this.persist()
      return true
    },

    renameSession(id: string, title: string) {
      const s = this.sessions.find((x) => x.id === id)
      if (s && title.trim()) {
        s.title = title.trim()
        s.titleMode = 'manual'
        this.persist()
      }
    },

    async loadProfileForSession(id: string): Promise<void> {
      const session = this.sessions.find((item) => item.id === id)
      if (!session) return
      try {
        const resp = await getConversationProfile(session.conversationId)
        session.profile = normalizeConversationProfile(resp.profile)
        refreshAutoTitle(session)
        session.contextCompressed = needsContextCompression(
          session.messages.filter((msg) => msg.role === 'user').length,
        )
        this.persist()
      } catch {
        // 离线时保留本地画像，联网后再同步。
      }
    },

    applyProfileFromQuery(session: ChatSession, query: string, pendingSlot?: 'origin' | 'destination') {
      const updates = extractProfileUpdates(query, session.profile, pendingSlot)
      session.profile = mergeConversationProfile(session.profile, updates)
      refreshAutoTitle(session, query)
      session.contextCompressed = needsContextCompression(
        session.messages.filter((msg) => msg.role === 'user').length + 1,
      )
      this.persist()
    },

    async syncProfile(session: ChatSession): Promise<void> {
      const profile = normalizeConversationProfile(session.profile)
      try {
        const resp = await patchConversationProfile(session.conversationId, profile)
        session.profile = normalizeConversationProfile(resp.profile)
        this.persist()
      } catch {
        // 画像同步失败不阻塞聊天主流程，本地仍保留可编辑副本。
      }
    },

    /** 置顶/取消置顶会话 */
    togglePin(id: string) {
      const s = this.sessions.find((x) => x.id === id)
      if (!s) return
      s.pinned = !s.pinned
      this.persist()
    },

    persist(owner?: string) {
      saveSessions(this.currentPersonaId, this.sessions, owner ?? this.storageOwner)
    },

    async loadPersonas() {
      const resp = await fetchPersonas()
      this.personas = resp.personas
      if (!this.personas.some((p) => p.id === this.currentPersonaId)) {
        this.currentPersonaId = resp.current || this.personas[0]?.id || 'unified'
      }
      this.initSessions()
    },

    /** 切人格：重载该人格的本地会话（旧版 travel/me 双模式惯例的泛化） */
    /** 切人格；返回 false 表示被拒（流式进行中/已是当前人格），调用方可据此提示 */
    switchPersona(personaId: string): boolean {
      if (this.streaming || personaId === this.currentPersonaId) return false
      this.currentPersonaId = personaId
      this.initSessions()
      return true
    },

    newConversation() {
      // 已在新会话（未使用、无消息）时维持现状，重复点击不再新建空会话（对齐 dbagentSessions 定稿行为）
      if (this.currentSession?.messages.length === 0) return
      this.createSession()
    },

    setAutoPrompt(prompt: string) {
      this.autoPrompt = prompt
    },

    /** 取消当前流式生成（AbortController） */
    cancelStream() {
      this.abort?.abort()
    },

    /** 取消并等待当前流结束，供登出在清账号前先完成持久化。 */
    async cancelAndWait(): Promise<void> {
      this.cancelStream()
      await streamCompletion
    },

    /** 切换服务端对话分支：删除/重生成后不再读取旧分支上下文。 */
    branchConversation(session: ChatSession): void {
      session.conversationId = newConversationId()
      this.persist()
    },

    /** 删除某条问答（assistant 及其前面的 user 消息），并切换服务端分支。 */
    deleteQA(messageIndex: number): void {
      const session = this.currentSession
      if (!session || messageIndex < 1) return
      // 2026-09-12 修复（外部复核 P1）：流式中删除问答会改写 messages 与
      // conversationId，正在进行的回答会写入废弃分支
      if (this.streaming) return
      session.messages.splice(messageIndex - 1, 2)
      this.branchConversation(session)
    },

    /** 评分后标记该回复已评（持久化，刷新后不再显示评分按钮） */
    markRated(messageIndex: number): void {
      const msg = this.currentSession?.messages[messageIndex]
      if (msg) {
        msg.rated = true
        this.persist()
      }
    },

    /** 发送一条消息：流式期间再次点击发送按钮 = 取消 */
    async send(query: string, fileIds: string[] = []): Promise<void> {
      if (this.streaming) {
        this.cancelStream()
        return
      }
      const session = this.currentSession
      if (!session || (!query.trim() && !fileIds.length)) return
      this.applyProfileFromQuery(session, query)

      const { sendQuery, assistantView } = appendOutgoingTurn(session, query, fileIds)
      this.streaming = true
      this.error = ''

      const abort = new AbortController()
      this.abort = abort
      const ownerAtSend = this.storageOwner || useAuthStore().user?.username || ''
      streamCompletion = new Promise<void>((resolve) => {
        resolveStreamCompletion = resolve
      })
      // 思考全文逐次清洗，避免指纹被流式块边界切开。
      const streamContext = { rawReasoning: '' }

      try {
        await streamChat({
          query: sendQuery,
          conversationId: session.conversationId,
          personaId: this.currentPersonaId !== 'unified' ? this.currentPersonaId : undefined,
          fileIds,
          lang: currentLocale(),
          signal: abort.signal,
          onEvent: (event) => handleStreamEvent(event, session, streamContext),
        })
        // 流结束兜底：空内容给明确提示
        if (!assistantView.content) {
          assistantView.content = i18n.global.t('no_reply')
        }
      } catch (e) {
        // 用户主动取消：保留已生成内容，不算错误
        if (abort.signal.aborted) {
          assistantView.streaming = false
          this.streaming = false
          this.abort = null
          this.persist(ownerAtSend)
          return
        }
        // 402 = GitHub 试用额度用尽 → 弹绑定手机引导
        if (e instanceof ApiError && e.status === 402) {
          assistantView.content = i18n.global.t('quota_exhausted')
          this.bindPhoneTip = i18n.global.t('bind_phone_exhausted_tip')
          refreshQuotaBadge()
        } else {
          this.error = e instanceof Error ? e.message : i18n.global.t('request_failed')
          assistantView.content = this.error
        }
      } finally {
        assistantView.streaming = false
        this.streaming = false
        this.abort = null
        session.updatedAt = Date.now()
        try {
          this.persist(ownerAtSend)
          void this.syncProfile(session)
        } finally {
          // 无论 localStorage 是否异常，都必须唤醒等待登出收尾的调用方。
          resolveStreamCompletion?.()
          resolveStreamCompletion = null
        }
      }
    },
  },
})
