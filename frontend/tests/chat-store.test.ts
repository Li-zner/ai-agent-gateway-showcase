/** 聊天 store 行为回归：删除服务端对齐、失败保留本地、重生成切换分支。 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { streamChat } from '../src/api/chat'
import { deleteConversation } from '../src/api/conversation'
import { useChatStore } from '../src/stores/chat'
import type { ChatSession } from '../src/utils/sessions'

vi.mock('../src/api/chat', () => ({ streamChat: vi.fn() }))
vi.mock('../src/api/conversation', () => ({
  deleteConversation: vi.fn(),
  getConversationProfile: vi.fn(),
  patchConversationProfile: vi.fn(),
}))

function makeSession(id: string, conversationId: string): ChatSession {
  return {
    id,
    personaId: 'unified',
    title: id,
    conversationId,
    messages: [],
    updatedAt: 0,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
})

describe('chat store', () => {
  it('删除会话成功后才移除本地记录', async () => {
    vi.mocked(deleteConversation).mockResolvedValue()
    const chat = useChatStore()
    chat.sessions = [makeSession('s1', 'c1'), makeSession('s2', 'c2')]
    chat.currentSessionId = 's1'

    await expect(chat.deleteSession('s1')).resolves.toBe(true)

    expect(deleteConversation).toHaveBeenCalledWith('c1')
    expect(chat.sessions.map((s) => s.id)).toEqual(['s2'])
  })

  it('服务端删除失败时保留本地会话', async () => {
    vi.mocked(deleteConversation).mockRejectedValue(new Error('network'))
    const chat = useChatStore()
    chat.sessions = [makeSession('s1', 'c1'), makeSession('s2', 'c2')]
    chat.currentSessionId = 's1'

    await expect(chat.deleteSession('s1')).rejects.toThrow('network')

    expect(chat.sessions.map((s) => s.id)).toEqual(['s1', 's2'])
  })

  it('按工具名识别天气结果并写入当前会话画像', async () => {
    vi.mocked(streamChat).mockImplementation(async (opts) => {
      opts.onEvent({
        type: 'tool_result',
        name: 'query_weather',
        result: {
          city: '渭南',
          forecast: [{ date: '2026-09-21', day_weather: '晴' }],
        },
      })
      opts.onEvent({ type: 'answer_complete', content: '渭南明天晴。' })
    })
    const chat = useChatStore()
    const session = makeSession('s1', 'c1')
    chat.sessions = [session]
    chat.currentSessionId = 's1'

    await chat.send('渭南天气')

    expect(session.profile?.weather_city).toBe('渭南')
    expect(session.profile?.weather_forecast[0].day_weather).toBe('晴')
  })

  it('历史标题自动使用摘要，手动重命名后不再覆盖', async () => {
    vi.mocked(streamChat).mockImplementation(async (opts) => {
      opts.onEvent({ type: 'answer_complete', content: '渭南行程。' })
    })
    const chat = useChatStore()
    const session = makeSession('s1', 'c1')
    chat.sessions = [session]
    chat.currentSessionId = 's1'

    await chat.send('渭南旅游推荐，请先问出发地')
    expect(session.title).toBe('渭南旅行规划')

    chat.renameSession('s1', '我的渭南行程')
    await chat.send('再加一天')
    expect(session.title).toBe('我的渭南行程')
  })

  it('流式进行中禁止删除当前会话（2026-09-11 审查 P1 回归）', async () => {
    const chat = useChatStore()
    chat.sessions = [makeSession('s1', 'c1'), makeSession('s2', 'c2')]
    chat.currentSessionId = 's1'
    chat.streaming = true

    await expect(chat.deleteSession('s1')).resolves.toBe(false)

    expect(deleteConversation).not.toHaveBeenCalled()
    expect(chat.sessions.map((s) => s.id)).toEqual(['s1', 's2'])
    chat.streaming = false
  })

  it('重新生成切换服务端分支并替换问答', async () => {
    // 2026-09-10：regenerate 死代码已删除（全仓无调用方），该行为由 deleteQA +
    // 重新发送覆盖——分支切换语义由下一条用例钉住
    vi.mocked(streamChat).mockImplementation(async (opts) => {
      opts.onEvent({ type: 'answer_complete', content: '新回答' })
    })
    const chat = useChatStore()
    const session = makeSession('s1', 'c-old')
    session.messages = [
      { id: 'u1', role: 'user', content: '原问题' },
      { id: 'a1', role: 'assistant', content: '旧回答' },
    ]
    chat.sessions = [session]
    chat.currentSessionId = 's1'

    chat.deleteQA(1)
    await chat.send('原问题')

    expect(chat.currentSession?.conversationId).not.toBe('c-old')
    expect(chat.currentSession?.messages.map((m) => m.content)).toEqual(['原问题', '新回答'])
  })

  it('已在新会话（无消息）时重复新建不再产生空会话', () => {
    const chat = useChatStore()

    chat.newConversation() // 首次：当前为空 → 建一个空会话
    expect(chat.sessions).toHaveLength(1)
    const firstId = chat.currentSessionId

    chat.newConversation() // 已在新会话 → 维持现状，不再建
    expect(chat.sessions).toHaveLength(1)
    expect(chat.currentSessionId).toBe(firstId)
  })

  it('当前会话已有消息时新建会另开一个新会话', () => {
    const chat = useChatStore()
    const session = makeSession('s1', 'c1')
    session.messages = [{ id: 'u1', role: 'user', content: '你好' }]
    chat.sessions = [session]
    chat.currentSessionId = 's1'

    chat.newConversation()

    expect(chat.sessions).toHaveLength(2)
    expect(chat.currentSessionId).not.toBe('s1')
  })

  it('删除问答后切换服务端分支，旧上下文不再复用', () => {
    const chat = useChatStore()
    const session = makeSession('s1', 'c-old')
    session.messages = [
      { id: 'u1', role: 'user', content: '问题一' },
      { id: 'a1', role: 'assistant', content: '回答一' },
      { id: 'u2', role: 'user', content: '问题二' },
      { id: 'a2', role: 'assistant', content: '回答二' },
    ]
    chat.sessions = [session]
    chat.currentSessionId = 's1'

    chat.deleteQA(3)

    expect(chat.currentSession?.conversationId).not.toBe('c-old')
    expect(chat.currentSession?.messages.map((m) => m.content)).toEqual(['问题一', '回答一'])
  })
})
