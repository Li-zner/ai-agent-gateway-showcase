/** 账号隔离回归：认证态禁止回退 shared 存储，聊天持久化固定发送时 owner。 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { streamChat } from '../src/api/chat'
import { StorageKey } from '../src/enums'
import { useAuthStore } from '../src/stores/auth'
import { useChatStore } from '../src/stores/chat'
import { loadStringList, saveStringList } from '../src/utils/mapData'
import { loadSessions, saveSessions, type ChatSession } from '../src/utils/sessions'

vi.mock('../src/api/chat', () => ({ streamChat: vi.fn() }))

function profile(username: string) {
  return {
    username,
    phone: '',
    role: 'user',
    permissions: [],
    display_name: username,
    email: '',
    quota_limited: false,
    used_requests: 0,
    remaining_questions: 0,
    created_at: '',
  }
}

function session(id: string): ChatSession {
  return {
    id,
    personaId: 'unified',
    title: id,
    conversationId: `conv-${id}`,
    messages: [],
    updatedAt: 0,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
})

describe('账号本地数据隔离', () => {
  it('无 owner 时拒绝读写会话和地图数据', () => {
    saveSessions('unified', [session('s1')], '')
    expect(loadSessions('unified', '')).toEqual([])
    expect(localStorage.getItem('gw_sessions_shared_unified')).toBeNull()

    saveStringList(StorageKey.MapFavorites, ['北京'], '')
    expect(loadStringList(StorageKey.MapFavorites, '')).toEqual([])
  })

  it('切换账号后只读取当前账号的会话', () => {
    saveSessions('unified', [session('alice')], 'alice')
    saveSessions('unified', [session('bob')], 'bob')
    const auth = useAuthStore()
    const chat = useChatStore()
    auth.user = profile('bob')
    auth.profileLoaded = true

    chat.initSessions()

    expect(chat.sessions.map((item) => item.id)).toEqual(['bob'])
  })

  it('流式期间登出仍写发送时账号，不落入 shared', async () => {
    const auth = useAuthStore()
    const chat = useChatStore()
    auth.user = profile('alice')
    auth.profileLoaded = true
    chat.initSessions()
    vi.mocked(streamChat).mockImplementation(async (opts) => {
      auth.user = null
      auth.profileLoaded = false
      opts.onEvent({ type: 'answer_complete', content: '完成' })
    })

    await chat.send('问题')

    const raw = localStorage.getItem('gw_sessions_alice_unified')
    expect(raw).toContain('完成')
    expect(localStorage.getItem('gw_sessions_shared_unified')).toBeNull()
  })
})
