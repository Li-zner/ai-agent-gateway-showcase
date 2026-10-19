/** RAG 控制台中文标签映射：内部值保持英文，界面统一显示中文。 */
const STATUS_LABELS: Record<string, string> = {
  running: '处理中',
  success: '成功',
  succeeded: '成功',
  failed: '失败',
  degraded: '降级',
  error: '错误',
  ok: '正常',
  open: '待处理',
  acknowledged: '已认领',
  mitigating: '处理中',
  observing: '观察中',
  resolved: '已解决',
  closed: '已关闭',
  proposed: '已提议',
  prechecking: '预检查中',
  waiting_approval: '待审批',
  approved: '已批准',
  queued: '排队中',
  executing: '执行中',
  verifying: '验证中',
  rolling_back: '回滚中',
  rolled_back: '已回滚',
  checked: '已核对',
  cancelled: '已取消',
}

const SEVERITY_LABELS: Record<string, string> = {
  critical: '严重',
  high: '高',
  medium: '中',
  low: '低',
  info: '提示',
}

const RISK_LABELS: Record<string, string> = {
  L0: '只读（L0）',
  L1: '可逆（L1）',
  L2: '配置变更（L2）',
  L3: '高风险（L3）',
}

const STAGE_LABELS: Record<string, string> = {
  cache: '缓存检查',
  civil_route: '民法典路由',
  simple_fast_path: '简单快速路径',
  task_path: '任务路径',
  task: '任务路径',
  task_simple: '简单任务',
  task_react: '复杂任务',
  react: '复杂任务',
  recommend_fast_path: '推荐快速路径',
  rebuild_lock: '索引重建锁',
  law_mapping: '法律术语映射',
  intent_route: '意图路由',
  retrieval: '知识召回',
  retrieve: '知识召回',
  recall: '多路召回',
  recall_retry: '改写重试召回',
  fusion: 'RRF 融合',
  rerank: '结果重排',
  generation: '模型生成',
  grounding: '依据外引用',
  answer: '回答生成',
  generate: '回答生成',
  citation: '引用校验',
  fallback: '降级处理',
  tool: '工具调用',
  thought: '思考过程',
}

const ACTION_LABELS: Record<string, string> = {
  rerun_diagnosis: '重跑诊断',
  probe_health: '健康探测',
  clear_exact_cache: '清理精确缓存',
  adjust_rerank_top: '调整重排候选池',
  // 动作白名单见 app/services/action_registry.py，漏登记会让动作在控制台
  // 显示成「未知动作（xxx）」
  apply_knowledge_change: '应用知识变更集',
  // P2 业务写动作：注册表见 app/services/action_registry.py，新增动作须同步这里
  set_user_active: '账号启停',
  set_channel_active: '支付渠道开关',
  propose_wallet_adjustment: '余额调整建议（仅提案）',
}

const ROUTE_LABELS: Record<string, string> = {
  simple_fast_path: '简单快速路径',
  task_path: '任务路径',
  task: '任务路径',
  task_simple: '简单任务',
  task_react: '复杂任务',
  react: '复杂任务',
  recommend_fast_path: '推荐快速路径',
  fallback: '降级路径',
  cache_hit: '缓存命中',
  cache_empty: '缓存穿透',
  civil_reject: '民法典拒答',
  law_mapping: '法律映射',
  config_error: '配置错误',
  direct: '直接回答',
  rag: '知识检索',
  cache: '缓存命中',
}

const PERSONA_LABELS: Record<string, string> = {
  civil_code: '民法典',
  civil_code_eval: '民法典评测',
  unified: '通用助手',
  travel: '旅行助手',
  project: '项目知识',
}

const METHOD_PARTS: Record<string, string> = {
  trgm: '关键词',
  vector: '向量',
  rrf: '融合',
  local_rerank: '本地重排',
  llm_rerank: '模型重排',
  cache: '缓存',
  expansion: '邻接扩展',
}


/** 状态转中文，未知值不把英文直接暴露给界面。 */
export function label_status(value?: string | null): string {
  if (!value) return '-'
  return STATUS_LABELS[value] ?? '未知状态'
}


/** 严重度转中文。 */
export function label_severity(value?: string | null): string {
  if (!value) return '-'
  return SEVERITY_LABELS[value] ?? '未知级别'
}


/** 风险等级转中文，同时保留 L0-L3 审计标识。 */
export function label_risk(value?: string | null): string {
  if (!value) return '-'
  return RISK_LABELS[value] ?? `未知风险（${value}）`
}


/** 执行阶段转中文。 */
export function label_stage(value?: string | null): string {
  if (!value) return '-'
  return STAGE_LABELS[value] ?? `未知阶段（${value}）`
}


/** 修复动作转中文。 */
export function label_action(value?: string | null): string {
  if (!value) return '-'
  return ACTION_LABELS[value] ?? `未知动作（${value}）`
}


/** 请求路径转中文。 */
export function label_route(value?: string | null): string {
  if (!value) return '-'
  return ROUTE_LABELS[value] ?? `未知路径（${value}）`
}


/** 人格标识转中文。 */
export function label_persona(value?: string | null): string {
  if (!value) return '-'
  return PERSONA_LABELS[value] ?? `未知人格（${value}）`
}


/** 召回方法转中文，保留无法识别的组合项。 */
export function label_retrieval_method(value?: string | null): string {
  if (!value) return '-'
  return value
    .split('+')
    .map((part) => METHOD_PARTS[part] ?? part)
    .join(' + ')
}
