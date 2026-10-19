/** 认证 / 用户信息 API（/api/login /api/logout /api/user/profile） */
import { get, getAuthMode, post, type TokenPair } from './http'

export interface UserProfile {
  username: string
  phone: string
  role: string
  /** 能力允许清单（子账号；admin 恒全，恒为空数组由前端按 role 判） */
  permissions: string[]
  display_name: string
  email: string
  quota_limited: boolean
  used_requests: number
  remaining_questions: number
  created_at: string
}

export async function login(username: string, password: string): Promise<TokenPair> {
  // 2026-09-12 清欠（D-F7）：落盘归 store 统一处理，API 层只负责请求
  const headers = getAuthMode() === 'cookie' ? { 'X-Console-Auth': 'cookie' } : undefined
  return post<TokenPair>('/api/login', { username, password }, headers)
}

/** GitHub OAuth 回调令牌：一次性授权码原子兑换，避免长期令牌进入 URL。 */
export function exchangeOAuthCode(code: string): Promise<TokenPair> {
  return post<TokenPair>('/api/oauth/exchange', { code })
}

/** 登出：撤销 refresh token（后端 jti 进黑名单），并清本地态 */
export async function logout(refreshToken: string): Promise<void> {
  try {
    await post(
      '/api/logout',
      undefined,
      refreshToken ? { Authorization: `Bearer ${refreshToken}` } : undefined,
    )
  } catch {
    /* 后端撤销失败不阻塞本地登出（token 自身到期失效） */
  }
}

export function fetchProfile(): Promise<UserProfile> {
  return get<UserProfile>('/api/user/profile')
}
