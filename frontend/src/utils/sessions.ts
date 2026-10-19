/** 本地会话持久化（localStorage，按人格隔离——对齐旧版 travel/me 双存储惯例） */
import type { ChatMessage } from '../stores/chat'
import type { ConversationProfile } from './conversationProfile'

export interface ChatSession {
  id: string
  personaId: string
  title: string
  /** auto=随画像刷新摘要标题；manual=用户手动重命名后保持。 */
  titleMode?: 'auto' | 'manual'
  /** 自动标题已在首轮确定并冻结，后续消息不再改名（手动重命名不受此限）。 */
  titleLocked?: boolean
  conversationId: string
  messages: ChatMessage[]
  updatedAt: number
  /** 每个历史会话独立画像；服务端同步，离线时保留本地副本。 */
  profile?: ConversationProfile
  /** 超过15轮后标记为已压缩上下文；完整消息仍保留用于回看。 */
  contextCompressed?: boolean
  /** 置顶会话排在最前（旧数据无此字段视为未置顶） */
  pinned?: boolean
}

function storageKey(personaId: string, owner = ''): string {
  // 认证态必须绑定账号；shared 键已废弃，避免 profile 尚未加载时串读旧数据。
  return `gw_sessions_${owner}_${personaId}`
}

export function loadSessions(personaId: string, owner = ''): ChatSession[] {
  if (!owner) return []
  try {
    const raw = localStorage.getItem(storageKey(personaId, owner))
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    // 形状防御：损坏数据（对象/数字等）兜底为空，避免后续 unshift/find 抛异常
    return Array.isArray(parsed)
      ? (parsed as ChatSession[]).map((session) => ({
        ...session,
        titleMode: session.titleMode === 'manual' ? 'manual' : 'auto',
      }))
      : []
  } catch {
    return []
  }
}

export function saveSessions(personaId: string, sessions: ChatSession[], owner = ''): void {
  if (!owner) return
  localStorage.setItem(storageKey(personaId, owner), JSON.stringify(sessions))
}

/** 随机 id：crypto.randomUUID 优先（http 不安全环境回退时间戳+随机串） */
function randomId(): string {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

export function newSessionId(): string {
  return `sess_${randomId()}`
}

export function newConversationId(): string {
  return `conv_${randomId()}`
}

/** 消息稳定 id（v-for key 用；Date.now() 同毫秒创建两条会撞） */
export function newMessageId(): string {
  return `msg_${randomId()}`
}

/** 会话导出为 Markdown（浏览器下载；纯函数部分可单测） */
export function sessionToMarkdown(s: ChatSession, locale = 'zh'): string {
  const fmt = (ts?: number) =>
    ts ? new Date(ts).toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US') : ''
  const roleLabel = (m: ChatMessage) =>
    m.role === 'user' ? (locale === 'zh' ? '用户' : 'User') : (locale === 'zh' ? '助手' : 'Assistant')
  const lines: string[] = [`# ${s.title}`, '']
  if (locale === 'zh') lines.push(`> 导出于 ${fmt(Date.now())}`, '')
  else lines.push(`> Exported at ${fmt(Date.now())}`, '')
  for (const m of s.messages) {
    lines.push(`## ${roleLabel(m)}`, '')
    lines.push(m.content || '', '')
    const t = fmt(m.timestamp)
    if (t) lines.push(`*${t}*`, '')
  }
  return lines.join('\n')
}

/** 触发浏览器下载导出文件 */
export function downloadSession(s: ChatSession, locale = 'zh'): void {
  const safeName = s.title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 40) || 'session'
  const blob = new Blob([sessionToMarkdown(s, locale)], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${safeName}.md`
  a.click()
  URL.revokeObjectURL(url)
}
