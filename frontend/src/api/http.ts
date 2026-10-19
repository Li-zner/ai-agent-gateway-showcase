/**
 * HTTP 客户端：JWT 注入 / 401 刷新重试 / 统一错误
 *
 * 认证态说明：token 存 localStorage（演示部署形态）；若升级生产形态应改为
 * httpOnly Cookie + 后端会话，避免 XSS 读取面。401 时先用 refresh token 换新
 * （/api/refresh，轮换语义），成功则重放原请求一次，失败则清除登录态。
 */
import { StorageKey } from '../enums'

const BASE = import.meta.env.VITE_API_BASE ?? ''
type AuthMode = 'bearer' | 'cookie'
let authMode: AuthMode = 'bearer'

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
}

const ACCESS_KEY = StorageKey.AccessToken
const REFRESH_KEY = StorageKey.RefreshToken
const CONSOLE_SESSION_KEY = 'rag_console_authenticated'

export function getAccessToken(): string {
  if (authMode === 'cookie') return ''
  return localStorage.getItem(ACCESS_KEY) ?? ''
}

export function getRefreshToken(): string {
  if (authMode === 'cookie') return ''
  return localStorage.getItem(REFRESH_KEY) ?? ''
}

export function saveTokens(pair: TokenPair): void {
  if (authMode === 'cookie') {
    localStorage.setItem(CONSOLE_SESSION_KEY, '1')
    return
  }
  localStorage.setItem(ACCESS_KEY, pair.access_token)
  localStorage.setItem(REFRESH_KEY, pair.refresh_token)
}

/** 只清当前模式自己的认证态：同源部署下主站与控制台互不牵连（审查 P2-4）。 */
export function clearTokens(): void {
  if (authMode === 'cookie') {
    localStorage.removeItem(CONSOLE_SESSION_KEY)
    return
  }
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

/** 独立控制台使用 HttpOnly Cookie，主站继续保持 Bearer。 */
export function setAuthMode(mode: AuthMode): void {
  authMode = mode
}

export function getAuthMode(): AuthMode {
  return authMode
}


/** 路径部署时统一给站内 API 加前缀，绝对 URL 保持不变（SSE 裸 fetch 也必须走这里）。 */
export function resolve_url(url: string): string {
  if (!BASE || /^https?:\/\//.test(url)) return url
  if (url === BASE || url.startsWith(`${BASE}/`)) return url
  return `${BASE}${url}`
}


/** 路由守卫只判断“有无会话”，不接触 Cookie 中的令牌。 */
export function hasAuthSession(): boolean {
  if (authMode === 'cookie') {
    return localStorage.getItem(CONSOLE_SESSION_KEY) === '1'
  }
  return Boolean(getAccessToken())
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

let onUnauthorized: (() => void) | null = null

/** 注册 401 最终兜底回调（router 守卫跳登录页），避免 http 层反向依赖 router */
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn
}

/** 触发 401 最终兜底（供 SSE 等裸 fetch 路径在刷新失败时复用同一语义） */
export function notifyUnauthorized(): void {
  clearTokens()
  onUnauthorized?.()
}

let refreshInFlight: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  // 单飞：并发 401 只发起一次刷新（refresh token 是轮换语义，重复用会互踩失效）
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = doRefresh()
  const ok = await refreshInFlight
  refreshInFlight = null
  return ok
}

async function doRefresh(): Promise<boolean> {
  const refresh = getRefreshToken()
  if (!refresh && authMode === 'bearer') return false
  try {
    const headers: Record<string, string> = {}
    if (refresh) headers.Authorization = `Bearer ${refresh}`
    if (authMode === 'cookie') headers['X-Console-Auth'] = 'cookie'
    const resp = await fetch(resolve_url('/api/refresh'), {
      method: 'POST',
      headers,
      credentials: 'same-origin',
    })
    if (!resp.ok) return false
    const pair = (await resp.json()) as TokenPair
    // 响应形状防御：缺字段视为刷新失败（避免把 undefined 存成 token）
    if (authMode === 'bearer'
      && (!pair?.access_token || !pair?.refresh_token)) return false
    saveTokens(pair)
    return true
  } catch {
    return false
  }
}

/** 401 单飞刷新（供 SSE 等绕过 request() 的裸 fetch 复用） */
export function refreshAccessToken(): Promise<boolean> {
  return tryRefresh()
}

/** 普通请求 30s 超时兜底（2026-09-30 校准修复 B4）：此前 fetch 无超时，挂死
 * 接口无限等待——与 SSE 路径 60s 看门狗防御密度倒挂。上传走 upload() 的 120s。 */
const DEFAULT_TIMEOUT_MS = 30_000

async function doFetch(url: string, init: RequestInit, retried: boolean): Promise<Response> {
  const headers = new Headers(init.headers)
  const token = getAccessToken()
  // 已显式指定 Authorization 时不覆盖（如 logout 携 refresh token 供后端吊销）
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`)
  const resp = await fetch(resolve_url(url), {
    ...init,
    headers,
    credentials: 'same-origin',
    // 每次调用（含 401 刷新重试）新建 signal；调用方显式传入的优先
    signal: init.signal ?? AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
  })
  if (resp.status === 401) {
    if (!retried && await tryRefresh()) {
      return doFetch(url, init, true)
    }
    // 重试后仍为 401 也必须清理登录态，避免页面保留失效会话。
    clearTokens()
    onUnauthorized?.()
  }
  return resp
}

/** 统一 GET/POST：非 2xx 抛 ApiError（detail 优先，兼容 FastAPI 错误体） */
export async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const resp = await doFetch(url, init, false)
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      detail = body.detail ?? body.error ?? detail
    } catch {
      /* 非 JSON 错误体，保留状态码文案 */
    }
    throw new ApiError(resp.status, detail)
  }
  return (await resp.json()) as T
}

export function get<T>(url: string): Promise<T> {
  return request<T>(url)
}

export function post<T>(url: string, body?: unknown, headers?: Record<string, string>): Promise<T> {
  return request<T>(url, {
    method: 'POST',
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

export function put<T>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: 'PUT',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

/** 文件 multipart 上传（大文件/慢链路：120s，不吃默认 30s） */
export function upload<T>(url: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  return request<T>(url, { method: 'POST', body: form, signal: AbortSignal.timeout(120_000) })
}
