/**
 * 思考过程清洗（自旧 app.js cleanReasoning 行为等价平移）：
 * 后端 reasoning_guard 已过滤，此处前端双保险——剔除泄露行 + 内部代码名转可读文案
 */
import i18n from '../locales'

const { t } = i18n.global

// 模型控制标记（<|DSML|> 等伪工具调用骨架）：与后端 safety_filter 同口径，前端双保险
const CONTROL_TOKEN_RE = /<[\|｜][^|｜<>]{0,40}[\|｜]>/g

const LEAK_PATTERNS: RegExp[] = [
  /system prompt/i, /system_prompt/i, /systemPrompt/,
  /我的系统提示词/, /系统提示词要求/, /按照系统提示词/, /根据我的系统提示词/,
  /my system prompt/i, /according to my system/i,
  /the instruction says/i, /instructions say/i, /per my instructions/i,
  /as instructed/i, /output language requirement/i, /always respond in/i,
  /do not output chinese/i, /输出语言要求/, /请始终使用/, /不要输出英文/, /不要输出中文/,
  /系统设定/, /系统限制/, /系统配置/, /根据人设/, /按人设/, /角色设定/, /人设要求/,
  /工具调用失败/, /调用失败/, /工具执行失败/, /工具超时/, /工具报错/,
  /tool call failed/i, /tool execution failed/i, /tool timeout/i,
  /重试失败/, /重试仍失败/, /请求失败/, /请求超时/,
  /response_format/, /json_object/, /llm_semaphore/,
  /## 核心原则/, /## 可用工具/, /## 默认值/, /防幻觉/, /最小工具调用/,
  /【输出格式】/, /只输出JSON/,
]

const TOOL_NAME_KEYS: Record<string, string> = {
  query_weather: 'tool_weather',
  query_hotel: 'tool_hotel',
  query_route: 'tool_route',
  query_food: 'tool_food',
  fetch_weather_async: 'tool_fetch_weather',
  call_sub_agent: 'tool_sub_agent',
  web_search: 'tool_web_search',
  search_knowledge: 'tool_knowledge_search',
}

export function toolDisplayName(code: string): string {
  const key = TOOL_NAME_KEYS[code]
  return key ? t(key) : code
}

export function cleanReasoning(text: string): string {
  if (!text) return ''
  const filtered = text
    .replace(CONTROL_TOKEN_RE, '')
    .split('\n')
    .filter((line) => {
      const s = line.trim()
      if (!s) return true
      return !LEAK_PATTERNS.some((re) => re.test(s))
    })
    .join('\n')
  return filtered
    .replace(/query_weather/g, t('tool_weather'))
    .replace(/query_hotel/g, t('tool_hotel'))
    .replace(/query_route/g, t('tool_route'))
    .replace(/query_food/g, t('tool_food'))
    .replace(/fetch_weather_async/g, t('tool_fetch_weather'))
    .replace(/call_sub_agent/g, t('tool_sub_agent'))
    .replace(/web_search/g, t('tool_web_search'))
    .replace(/search_knowledge/g, t('tool_knowledge_search'))
    .replace(/"departure"\s*:\s*"[^"]*"/g, '')
    .replace(/"destination"\s*:\s*"[^"]*"/g, '')
    .replace(/"preference"\s*:\s*"[^"]*"/g, '')
    .replace(/"cuisine"\s*:\s*"[^"]*"/g, '')
    .replace(/"location"\s*:\s*"[^"]*"/g, '')
    .replace(/\{[^}]*"success"[^}]*\}/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}
