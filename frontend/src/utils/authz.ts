/** 前端能力判定（与后端 app/core/capabilities.py 同口径）：
 * admin 恒全；子账号走 permissions 允许清单。 */
import type { UserProfile } from '../api/auth'

export function isAdmin(user: UserProfile | null): boolean {
  return user?.role === 'admin'
}

export function hasCap(user: UserProfile | null, cap: string): boolean {
  if (isAdmin(user)) return true
  return (user?.permissions ?? []).includes(cap)
}
