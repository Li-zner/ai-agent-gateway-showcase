/**
 * 控制台功能模块注册表：导航与人格的"插拔"层。
 *
 * 导航收敛为两个入口：数据台 与 Agent 对话台（2026-10-02 业主拍板：
 * 不跳新窗口，像主站切人格一样页内切换）。Agent 对话台可细分为多个人格
 * （personas）——当前是诊断助手（只读）与 DB Agent（受审写）；加第三个人格
 * = 在这里加一项，切换器自动出现；下线功能 = enabled 置 false。
 */
export interface AgentPersona {
  key: string
  label: string
  hint: string
}

export interface ConsoleModule {
  key: string
  label: string
  /** hash 路由路径（如 /admin/rag） */
  route: string
  enabled: boolean
  /** Agent 对话台专属：页内可切换的人格清单 */
  personas?: AgentPersona[]
}

export const consoleModules: ConsoleModule[] = [
  { key: 'console-data', label: '数据台', route: '/admin/rag', enabled: true },
  {
    key: 'agent-chat',
    label: 'Agent 对话台',
    route: '/admin/rag/chat',
    enabled: true,
    personas: [
      { key: 'rag-ops', label: '诊断助手', hint: 'RAG 运维诊断（只读）' },
      { key: 'db-agent', label: 'DB Agent', hint: '数据库分析与受审写操作' },
    ],
  },
]

/** 打开一个功能模块：同窗跳转（不弹新窗口）。 */
export function openModule(mod: ConsoleModule): void {
  window.location.hash = mod.route
}

/** 当前启用的模块（导航渲染用）。 */
export function enabledModules(): ConsoleModule[] {
  return consoleModules.filter((m) => m.enabled)
}

/** 除自身外的启用模块（各页头部渲染"去别处"的入口用）。 */
export function otherModules(excludeKey: string): ConsoleModule[] {
  return enabledModules().filter((m) => m.key !== excludeKey)
}

/** 取某模块的人格清单（Agent 对话台切换器用）；无则空数组。 */
export function personasOf(moduleKey: string): AgentPersona[] {
  return consoleModules.find((m) => m.key === moduleKey)?.personas ?? []
}
