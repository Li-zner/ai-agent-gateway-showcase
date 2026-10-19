<script setup lang="ts">
/**
 * DB Agent 聊天窗：与诊断助手共用同款窗口与左侧会话栏（会话数据在
 * dbagentSessions store，侧栏 UI 由外壳 RagOpsChatView 渲染）。本组件只负责
 * 当前会话的转录：用户消息即任务，助手消息流式展示步骤、内嵌写提案审批卡
 * （命令 + before 预览 + 批准/否决），结束后整篇证据链报告按 markdown 渲染。
 */
import { nextTick, onBeforeUnmount, ref } from 'vue'
import {
  decideDbAgentWrite,
  getDbAgentTask,
  startDbAgentTask,
  type DbAgentPreview,
  type DbAgentProposal,
} from '../api/dbagent'
import { renderOpsMarkdown } from '../utils/opsMarkdown'
import { chartifyReport } from '../utils/miniBars'
import { useDbagentSessions, type DbTurn } from '../dbagentSessions'

const { conversations, current, ensureConversation, persist } = useDbagentSessions()

/** 同会话上一轮问答摘要（可选上下文）：后端任务不带历史，摘要只为代词
 *  指代（"给他加一百块"）与口径延续。只取最近一轮已完成的 Q + 报告开头，
 *  总长截 1200（后端同上限兜底）；无历史轮返回空串。 */
function contextSummary(conv: { turns: Array<{ question: string; report: string | null; status: string }> }): string {
  const prev = [...conv.turns]
    .reverse()
    .find((t) => t.status === 'done' && t.report)
  if (!prev) return ''
  const q = `问：${prev.question}`
  const a = `答：${(prev.report ?? '').replace(/#+\s*证据附录[\s\S]*$/, '').trim().slice(0, 600)}`
  return `${q}\n${a}`.slice(0, 1200)
}

const input = ref('')
const deciding = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

/** 跨会话记忆开关（会话属性）：随每次提交同步到服务端 allow_cross。 */
function toggleCross(e: Event): void {
  if (!current.value) return
  current.value.allowCross = (e.target as HTMLInputElement).checked
  persist()
}

const transcriptEl = ref<HTMLElement | null>(null)

async function scrollBottom(): Promise<void> {
  await nextTick()
  transcriptEl.value?.scrollTo({ top: transcriptEl.value.scrollHeight })
}

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function findTurn(taskId: string): DbTurn | undefined {
  for (const conv of conversations.value) {
    const turn = conv.turns.find((t) => t.taskId === taskId)
    if (turn) return turn
  }
  return undefined
}

async function pollTurn(taskId: string): Promise<void> {
  try {
    const snap = await getDbAgentTask(taskId)
    const turn = findTurn(taskId)
    if (!turn) {
      stopPolling()
      return
    }
    turn.stepLines = snap.steps.filter((s) => s.line).map((s) => s.line as string)
    turn.proposal = snap.proposal
    turn.plan = snap.plan
    turn.status = snap.status
    if (snap.status === 'done') {
      turn.report = snap.report
      stopPolling()
    } else if (snap.status === 'failed') {
      turn.error = snap.error ?? '任务失败'
      stopPolling()
    }
    persist()
    await scrollBottom()
  } catch {
    // 单次轮询失败：下个周期重试；连续失败最终由任务超时收尾
  }
}

async function send(): Promise<void> {
  const text = input.value.trim()
  if (text.length < 4) return
  const conv = ensureConversation(text)
  const turn: DbTurn = {
    id: `turn_${Date.now()}`,
    question: text,
    taskId: null,
    status: 'running',
    stepLines: [],
    plan: null,
    proposal: null,
    report: null,
    error: null,
  }
  conv.turns.push(turn)
  input.value = ''
  persist()
  await scrollBottom()
  try {
    const snap = await startDbAgentTask(text, contextSummary(conv), conv.id,
                                        conv.allowCross ?? null)
    turn.taskId = snap.id
  } catch (e) {
    turn.status = 'failed'
    turn.error = String(e)
    persist()
    return
  }
  stopPolling()
  pollTimer = setInterval(() => pollTurn(turn.taskId as string), 1500)
  await pollTurn(turn.taskId)
}

async function decide(turn: DbTurn, approve: boolean): Promise<void> {
  if (!turn.taskId || deciding.value) return
  deciding.value = true
  try {
    await decideDbAgentWrite(turn.taskId, approve)
    if (turn.proposal) turn.proposal.decision = approve ? 'approved' : 'rejected'
    // 立即收卡进入 running 态：不等下个轮询周期，否则卡片存留 1.5s 可被再次点击（后端 409）
    turn.status = 'running'
    stopPolling()
    pollTimer = setInterval(() => pollTurn(turn.taskId as string), 1500)
  } catch (e) {
    turn.error = String(e)
  } finally {
    deciding.value = false
  }
}

const previewColumns = (item: { preview?: DbAgentPreview }): string[] => {
  const rows = item?.preview?.rows ?? []
  return rows.length > 0 ? Object.keys(rows[0]) : []
}

/** 单条与批提案（P1-2）统一成条目列表：batch 按 statements×previews 展开。 */
const proposalItems = (
  p: DbAgentProposal | null,
): Array<{ sql: string; params: unknown[]; preview: DbAgentPreview }> => {
  if (!p) return []
  if (p.kind === 'batch' && p.statements?.length) {
    return p.statements.map((st, i) => ({
      sql: st.sql,
      params: st.params,
      preview: p.previews?.[i]
        ?? { kind: 'unknown', preview_sql: null, rows: [], note: '' },
    }))
  }
  return p.sql
    ? [{ sql: p.sql, params: p.params ?? [], preview: p.preview as DbAgentPreview }]
    : []
}

/** Diff 单元格（外部建议：审批卡 before→after 对比）：后端已投影的 after 行
 *  与 before 行按列比对，只留发生变化的列；不可投影时返回空数组不渲染。 */
/** Diff 单元格（外部建议：审批卡 before→after 对比）：后端已投影的 after 行
 *  与 before 行按列比对，只留发生变化的列；不可投影时返回空数组不渲染。
 *  数值等价不算变化——DB 侧 Decimal 转字符串（"100.00"）与参数数值（100）
 *  是同一笔账，不渲染假 Diff（逻辑账对账 2026-10-06）。 */
function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (a === null || a === '' || b === null || b === '') return false
  const na = Number(a)
  const nb = Number(b)
  return Number.isFinite(na) && Number.isFinite(nb) && na === nb
}

function diffCells(item: { preview?: DbAgentPreview }): Array<Array<{ col: string; from: unknown; to: unknown }>> {
  const rows = item.preview?.rows ?? []
  const projected = item.preview?.projected
  if (!projected?.length) return []
  return projected.map((after, i) => {
    const before = rows[i] ?? {}
    return Object.keys(after)
      .filter((col) => !sameValue(before[col], after[col]))
      .map((col) => ({ col, from: before[col], to: after[col] }))
  })
}

/** SQL 关键字断行缩进（Codex 审批卡式）：SET/WHERE/RETURNING 等各占一行。 */
function formatSql(sql: string): string {
  return sql
    .replace(/\s+/g, ' ')
    .replace(/\b(SET|WHERE|RETURNING|VALUES|FROM|AND|OR)\b/gi, '\n  $1')
    .trim()
}

/** 一句话描述本次修改涉及的内容（表/动作/目标/影响行数），置于代码块之前。 */
function proposalSummary(item: { sql?: string; params?: unknown[]; preview?: DbAgentPreview }): string {
  const p = { sql: item.sql ?? '', params: item.params ?? [], preview: item.preview }
  if (!p.sql) return ''
  const table = p.sql.match(/\bUPDATE\s+([a-z_][a-z0-9_]*)/i)?.[1]
    ?? p.sql.match(/\bDELETE\s+FROM\s+([a-z_][a-z0-9_]*)/i)?.[1]
    ?? p.sql.match(/\bINSERT\s+INTO\s+([a-z_][a-z0-9_]*)/i)?.[1]
    ?? '未知表'
  const verb = /^UPDATE/i.test(p.sql) ? '更新' : /^DELETE/i.test(p.sql) ? '删除' : '修改'
  const keys = p.params.filter((v) => typeof v === 'string').slice(0, 3).join('、')
  const rows = p.preview?.rows?.length ?? 0
  return `本次修改：对表 ${table} 执行「${verb}」，目标 ${keys || '见参数'}，预计影响 ${rows} 行。`
}

/** 从报告的证据附录里抠出每条证据的 SQL（evidence.appendix_markdown 的固定格式）。 */
function evidenceSqlMap(report: string): Map<string, string> {
  const map = new Map<string, string>()
  const re = /-\s*\[E(\d+)\]（(?:读|写)）行数\s*\d+\n\s*```sql\n([\s\S]*?)```/g
  for (const m of report.matchAll(re)) map.set(m[1], m[2].trim())
  return map
}

function escapeAttr(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
}

/** 报告渲染：markdown 之后把 [E#] 引用替换为可悬停角标，悬停显示对应查询 SQL；
 *  两列数值表前注入零依赖 CSS 条形图（外部建议 #8 统计图）。 */
function renderReport(turn: DbTurn): string {
  if (!turn.report) return ''
  const html = chartifyReport(renderOpsMarkdown(turn.report))
  const sqlMap = evidenceSqlMap(turn.report)
  return html.replace(/\[E(\d+)\]/g, (_all, n: string) => {
    const sql = sqlMap.get(n)
    const tip = sql ? escapeAttr(`证据 E${n}｜${sql}`) : `证据 E${n}，见证据附录`
    return `<sup class="ev-ref" title="${tip}">E${n}</sup>`
  })
}

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="dbagent-chat">
    <div ref="transcriptEl" class="transcript">
      <p v-if="!current || !current.turns.length" class="chat-empty">
        给 DB Agent 一个任务或问题，例如：把订单 ORD-1006 标记为 refunded 并汇报；
        或：余额最高的前五个用户是谁。
      </p>
      <template v-for="turn in current?.turns ?? []" :key="turn.id">
        <article class="msg user">
          <div class="bubble">{{ turn.question }}</div>
        </article>
        <article class="msg agent">
          <div class="who">DB Agent</div>
          <div class="bubble">
            <div v-if="turn.stepLines.length" class="step-lines">
              <p
                v-for="(line, i) in turn.stepLines"
                :key="i"
                class="step-line"
                :class="{ observe: line.startsWith('观察') }"
              >{{ line }}</p>
            </div>

            <!-- 计划门（ZCode plan mode 式）：复杂任务先拆步骤，人工批准后再动手 -->
            <div
              v-if="turn.plan && turn.status === 'awaiting_plan_approval'"
              class="approval-card plan-card"
            >
              <h4>执行计划待审批</h4>
              <p class="approval-desc">目标：{{ turn.plan.goal }}</p>
              <ol class="plan-steps">
                <li v-for="(stepText, i) in turn.plan.steps" :key="i">{{ stepText }}</li>
              </ol>
              <div class="approval-actions">
                <button class="ok" :disabled="deciding" @click="decide(turn, true)">批准执行</button>
                <button class="no" :disabled="deciding" @click="decide(turn, false)">否决</button>
              </div>
            </div>
            <p v-else-if="turn.plan?.decision" class="decision-note">
              计划已{{ turn.plan.decision === 'approved' ? '批准，按步骤执行' : '否决' }}
            </p>

            <!-- 审批卡（Codex 式）：先一句话描述涉及内容，再给分行后的将执行代码 -->
            <div
              v-if="turn.proposal && turn.status === 'awaiting_approval'"
              class="approval-card"
            >
              <h4>写操作待审批</h4>
              <!-- 重大变更（后端 ≥15% 行标记）：审批人可见的权限升级提示 -->
              <div
                v-if="turn.proposal.critical"
                class="risk-banner critical"
                role="alert"
              >
                <strong>重大变更</strong>
                <p>影响行数超过全表 15%，仅最高管理员可批准。</p>
              </div>
              <!-- 风险警报（后端实查计算）：L2 红色 / L1 琥珀 / unknown 灰 -->
              <div
                v-if="turn.proposal.risk?.level === 'L2'"
                class="risk-banner l2"
                role="alert"
              >
                <strong>高危操作</strong>
                <ul>
                  <li v-for="(r, i) in turn.proposal.risk.reasons" :key="i">{{ r }}</li>
                </ul>
              </div>
              <div v-else-if="turn.proposal.risk?.level === 'L1'" class="risk-banner l1">
                <strong>注意</strong>
                <ul>
                  <li v-for="(r, i) in turn.proposal.risk.reasons" :key="i">{{ r }}</li>
                </ul>
              </div>
              <div v-else-if="turn.proposal.risk?.level === 'unknown'" class="risk-banner unknown">
                <ul>
                  <li v-for="(r, i) in turn.proposal.risk.reasons" :key="i">{{ r }}</li>
                </ul>
              </div>
              <p v-if="turn.proposal.kind === 'batch'" class="approval-desc">
                批提案：{{ proposalItems(turn.proposal).length }} 条语句一次审批、
                单事务原子执行（要么全部生效，要么全部不生效）
              </p>
              <p v-if="proposalItems(turn.proposal).length === 1" class="approval-desc">
                {{ proposalSummary(proposalItems(turn.proposal)[0]) }}
              </p>
              <p v-if="turn.proposal.reason" class="reason">理由：{{ turn.proposal.reason }}</p>
              <template v-for="(item, idx) in proposalItems(turn.proposal)" :key="idx">
                <p class="code-label">将执行{{ proposalItems(turn.proposal).length > 1 ? `（第 ${idx + 1} 条）` : '' }}：</p>
                <pre class="sql">{{ formatSql(item.sql) }}</pre>
                <p class="params">参数：{{ JSON.stringify(item.params) }}</p>
                <details v-if="previewColumns(item).length" class="before-rows" open>
                  <summary>修改前数据，将影响 {{ item.preview.rows.length }} 行</summary>
                  <table class="preview-table">
                    <thead>
                      <tr><th v-for="col in previewColumns(item)" :key="col">{{ col }}</th></tr>
                    </thead>
                    <tbody>
                      <tr v-for="(row, i) in item.preview.rows" :key="i">
                        <td v-for="col in previewColumns(item)" :key="col">{{ row[col] }}</td>
                      </tr>
                    </tbody>
                  </table>
                </details>
                <details
                  v-if="diffCells(item).some((cells) => cells.length)"
                  class="after-diff"
                >
                  <summary>修改对比（before → after）</summary>
                  <ul class="diff-list">
                    <li
                      v-for="(cells, i) in diffCells(item).filter((c) => c.length).slice(0, 5)"
                      :key="i"
                    >
                      第 {{ i + 1 }} 行：
                      <code v-for="c in cells" :key="c.col">
                        {{ c.col }}: {{ c.from }} → {{ c.to }}
                      </code>
                    </li>
                  </ul>
                </details>
                <p class="note">{{ item.preview.note }}</p>
              </template>
              <div class="approval-actions">
                <button class="ok" :disabled="deciding" @click="decide(turn, true)">批准执行</button>
                <button class="no" :disabled="deciding" @click="decide(turn, false)">否决</button>
              </div>
            </div>
            <p v-else-if="turn.proposal?.decision" class="decision-note">
              写提案已{{ turn.proposal.decision === 'approved' ? '批准并执行' : '否决' }}
            </p>

            <div
              v-if="turn.report"
              class="markdown"
              v-html="renderReport(turn)"
            ></div>
            <p v-if="turn.error" class="turn-error">{{ turn.error }}</p>
            <p v-if="turn.status === 'running' && !turn.stepLines.length" class="thinking">正在启动任务...</p>
          </div>
        </article>
      </template>
    </div>

    <footer class="composer">
      <label class="cross-toggle" title="开启后，本会话的 recall_memory 工具可检索同一账号下其他会话的历史记忆">
        <input
          type="checkbox"
          :checked="current?.allowCross ?? false"
          @change="toggleCross($event)"
        />跨会话
      </label>
      <textarea
        v-model="input"
        rows="1"
        @keydown.enter.exact.prevent="send()"
      ></textarea>
      <button :disabled="!input.trim() || input.trim().length < 4" @click="send()">发送</button>
    </footer>
  </div>
</template>

<style scoped>
.dbagent-chat {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.transcript {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 4px;
}

.chat-empty {
  opacity: 0.7;
  padding: 24px 8px;
  text-align: center;
}

/* 对话气泡（2026-10-03 业主定稿）：用户右侧主色泡，Agent 左侧白卡+署名，分出层次 */
.msg {
  display: flex;
  margin-bottom: 16px;
}

.msg.user {
  justify-content: flex-end;
}

.msg.user .bubble {
  max-width: 72%;
  background: var(--primary, #3d7a5c);
  color: #fff;
  border-radius: 12px 12px 3px 12px;
  padding: 9px 14px;
  font-size: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.msg.agent {
  flex-direction: column;
}

.msg.agent .who {
  font-size: 11px;
  color: var(--text-muted, #6b7280);
  margin: 0 4px 4px;
}

.msg.agent .bubble {
  max-width: 94%;
  background: var(--bg-card, #fff);
  border: 1px solid var(--border-color, #dfe5de);
  border-radius: 3px 12px 12px 12px;
  padding: 12px 16px;
  font-size: 14px;
  line-height: 1.7;
}

/* 报告排版层级：正文 14px/1.8，标题降级为小节感而非大字报 */
.msg .bubble :deep(.markdown) {
  line-height: 1.8;
}

.msg .bubble :deep(.markdown h1),
.msg .bubble :deep(.markdown h2) {
  font-size: 15px;
  font-weight: 600;
  margin: 16px 0 6px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--border-color, #e5e7eb);
}

.msg .bubble :deep(.markdown h3) {
  font-size: 14px;
  font-weight: 600;
  margin: 12px 0 4px;
}

.msg .bubble :deep(.markdown p) {
  margin: 6px 0;
}

.msg .bubble :deep(.markdown ul),
.msg .bubble :deep(.markdown ol) {
  margin: 6px 0;
  padding-left: 20px;
}

.msg .bubble :deep(.markdown li) {
  margin: 4px 0;
}

.msg .bubble :deep(.markdown code) {
  font-size: 12px;
}

/* 证据角标：[E#] 渲染为可悬停上标，悬停显示对应查询 SQL */
.msg .bubble :deep(.ev-ref) {
  display: inline-block;
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  padding: 2px 4px;
  margin: 0 1px;
  border-radius: 4px;
  background: rgba(61, 122, 92, 0.12);
  color: var(--primary, #3d7a5c);
  cursor: help;
  vertical-align: super;
}

/* 步骤日志逐行渲染（2026-10-02 排版优化）：行距可读、JSON 长行折行 */
.step-lines {
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: rgba(127, 127, 127, 0.12);
  padding: 10px 12px;
  border-radius: 6px;
  margin: 0 0 10px;
}

.step-line {
  margin: 0;
  font-family: monospace;
  font-size: 12px;
  line-height: 1.7;
  word-break: break-all;
}

/* “观察：”行是步骤结论，与执行行区分 */
.step-line.observe {
  opacity: 0.82;
  border-top: 1px dashed rgba(127, 127, 127, 0.35);
  padding-top: 4px;
}

.approval-card {
  border: 1px solid #d29922;
  border-radius: 8px;
  padding: 12px 14px;
  margin: 8px 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.approval-card h4 {
  margin: 0;
}

/* 风险警报横幅：L2 红（百万级库利害：锁行/回滚/主从延迟）、L1 琥珀、unknown 灰 */
.risk-banner {
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
}

.risk-banner ul {
  margin: 4px 0 0;
  padding-left: 18px;
}

.risk-banner.l2 {
  background: #fef2f2;
  border: 1px solid #fca5a5;
  color: #b42318;
}

.risk-banner.l1 {
  background: #fffaeb;
  border: 1px solid #fde047;
  color: #b45309;
}

.risk-banner.unknown {
  background: rgba(127, 127, 127, 0.08);
  border: 1px solid rgba(127, 127, 127, 0.3);
  color: var(--text-muted, #6b7280);
}

/* 重大变更（critical）：靛色区分 L2 红色——语义是权限升级而非风险等级 */
.risk-banner.critical {
  background: #eef2ff;
  border: 1px solid #a5b4fc;
  color: #3730a3;
}

/* 计划门卡片：蓝边区分写审批（金边） */
.plan-card {
  border-color: #3b82f6;
}

.plan-steps {
  margin: 2px 0 0;
  padding-left: 22px;
}

.plan-steps li {
  margin: 4px 0;
  line-height: 1.6;
}

/* Codex 式：描述一句话在前，代码块在后，视觉分层 */
.approval-desc {
  margin: 0;
  font-size: 14px;
  line-height: 1.7;
}

.approval-card .code-label {
  margin: 2px 0 0;
  font-size: 12px;
  opacity: 0.65;
}

.approval-card .sql {
  background: rgba(0, 0, 0, 0.3);
  padding: 10px 12px;
  border-radius: 6px;
  white-space: pre-wrap;
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
}

.approval-card .note,
.approval-card .params,
.approval-card .reason {
  margin: 0;
  font-size: 13px;
}

.before-rows summary {
  cursor: pointer;
  font-size: 13px;
  opacity: 0.85;
}

.before-rows .preview-table {
  margin-top: 6px;
}

.preview-table {
  border-collapse: collapse;
  font-size: 12px;
}

.preview-table th,
.preview-table td {
  border: 1px solid rgba(127, 127, 127, 0.4);
  padding: 3px 8px;
  text-align: left;
}

.approval-actions {
  display: flex;
  gap: 8px;
}

.approval-actions .ok {
  border-color: #2da44e !important;
  color: #2da44e;
}

.approval-actions .no {
  border-color: #cf222e !important;
  color: #cf222e;
}

.decision-note {
  font-size: 13px;
  opacity: 0.8;
  margin: 4px 0;
}

.turn-error {
  color: #cf222e;
  font-size: 13px;
  margin: 4px 0;
  padding: 6px 10px;
  border-radius: 6px;
  background: rgba(207, 34, 46, 0.08);
}

.thinking {
  opacity: 0.6;
  font-size: 13px;
}

.composer {
  display: flex;
  gap: 8px;
  padding: 10px 4px 4px;
  border-top: 1px solid rgba(127, 127, 127, 0.25);
}

.composer textarea {
  flex: 1;
  padding: 8px;
  font: inherit;
  resize: none;
}

.cross-toggle {
  display: flex;
  align-items: center;
  gap: 4px;
  align-self: center;
  flex-shrink: 0;
  font-size: 12px;
  opacity: 0.75;
  white-space: nowrap;
  cursor: pointer;
}

.composer button {
  border: 1px solid rgba(127, 127, 127, 0.4);
  background: transparent;
  border-radius: 6px;
  padding: 6px 16px;
  cursor: pointer;
}

.diff-list {
  margin: 6px 0;
  padding-left: 18px;
  font-size: 12px;
}

.diff-list code {
  background: rgba(127, 127, 127, 0.15);
  border-radius: 4px;
  padding: 1px 6px;
  margin-right: 6px;
}

.mini-chart {
  margin: 8px 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.mini-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.mini-bar-label {
  flex: 0 0 30%;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mini-bar-track {
  flex: 1;
  height: 10px;
  background: rgba(127, 127, 127, 0.15);
  border-radius: 5px;
  overflow: hidden;
}

.mini-bar-fill {
  display: block;
  height: 100%;
  background: rgba(66, 133, 244, 0.65);
  border-radius: 5px;
}

.mini-bar-value {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
}
</style>
