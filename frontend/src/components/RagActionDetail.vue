<script setup lang="ts">
/** 修复动作审计详情：策略、基线、验证和回滚时间线。 */
import { computed, ref } from 'vue'
import { checkRagAction, rollbackRagAction, type RagAction } from '../api/ragAdmin'
import { label_risk, label_status } from '../utils/ragLabels'

const props = defineProps<{
  action: RagAction
}>()

const emit = defineEmits<{
  close: []
  refresh: []
}>()

const rollbackBusy = ref(false)
const rollbackError = ref('')
const checkBusy = ref(false)
const checkNote = ref('')
const checkError = ref('')
// 失败动作也要能点回滚：自动回滚失败（或执行异常）的动作停在 failed，快照若还在，
// 这里就是它唯一的重试入口，口径与后端 rollback_action / rollback_available 一致。
const canRollback = computed(
  () => props.action.rollback_available
    && (props.action.status === 'succeeded' || props.action.status === 'failed'),
)
// 没有留档原值时回滚器恒 skipped，状态永远停在 failed——这条通道给它一个终点：
// 人工核对过实际数据后标记「已核对」，只落状态与说明，不改动任何数据。
const canCheck = computed(() => props.action.status === 'failed')

const phaseLabels: Record<string, string> = {
  baseline: '执行前基线',
  postcheck: '执行后验证',
  rollback: '自动回滚',
}

const decisionLabels: Record<string, string> = {
  auto_allowed: '自动放行',
  approval_required: '需要审批',
  proposal_only: '仅生成建议',
}

const environmentLabels: Record<string, string> = {
  local: '本地',
  staging: '预发',
  production: '生产',
}

function fmtTime(value?: string | null): string {
  if (!value) return '-'
  return value.slice(0, 19).replace('T', ' ')
}

function fmtMs(value: unknown): string {
  return typeof value === 'number' ? `${value}ms` : '-'
}

function fmtJson(value: unknown): string {
  if (!value || typeof value !== 'object' || !Object.keys(value).length) return '-'
  return JSON.stringify(value)
}

function fmtResultMs(result: Record<string, unknown>, phase: string): string {
  const metric = result?.[phase]
  if (!metric || typeof metric !== 'object') return '-'
  return fmtMs((metric as Record<string, unknown>).avg_ms)
}

/** 二次确认后请求后端回滚，并通知父级重新读取动作。 */
async function rollbackAction(): Promise<void> {
  if (!canRollback.value || rollbackBusy.value) return
  const confirmed = window.confirm(
    `确认回滚动作 #${props.action.action_id}？系统将按回滚方案恢复配置。`,
  )
  if (!confirmed) return
  rollbackBusy.value = true
  rollbackError.value = ''
  try {
    await rollbackRagAction(props.action.action_id)
    emit('refresh')
  } catch (err) {
    rollbackError.value = err instanceof Error ? err.message : '回滚失败'
  } finally {
    rollbackBusy.value = false
  }
}

/** 人工核对后终结这条失败动作；说明必填，会一并写进审计。 */
async function checkAction(): Promise<void> {
  const note = checkNote.value.trim()
  if (!canCheck.value || checkBusy.value || !note) return
  const confirmed = window.confirm(
    `确认将动作 #${props.action.action_id} 标记为已核对？这只记录你的核对结论，不会改动任何数据。`,
  )
  if (!confirmed) return
  checkBusy.value = true
  checkError.value = ''
  try {
    await checkRagAction(props.action.action_id, note)
    checkNote.value = ''
    emit('refresh')
  } catch (err) {
    checkError.value = err instanceof Error ? err.message : '标记失败'
  } finally {
    checkBusy.value = false
  }
}
</script>

<template>
  <aside class="action-detail">
    <div class="panel-head">
      <h2>动作 #{{ action.action_id }}</h2>
      <button class="text-button" @click="$emit('close')">关闭</button>
    </div>
    <dl>
      <dt>事件 / 动作</dt>
      <dd>{{ action.incident_id ?? '-' }} / {{ action.action_key }}</dd>
      <dt>风险 / 状态</dt>
      <dd>{{ label_risk(action.risk_level) }} / {{ label_status(action.status) }}</dd>
      <dt>环境 / 版本</dt>
      <dd>{{ environmentLabels[action.environment] || action.environment || '-' }} / {{ action.config_version || '-' }}</dd>
      <dt>请求人 / 审批人</dt>
      <dd>{{ action.requested_by || '-' }} / {{ action.approved_by || '-' }}</dd>
      <dt>执行者</dt>
      <dd>{{ action.executed_by || '-' }}</dd>
      <dt>申请原因</dt>
      <dd>{{ action.reason || '-' }}</dd>
      <dt>冷却截止</dt>
      <dd>{{ fmtTime(action.policy_decision?.cooldown_until) }}</dd>
      <dt>尝试次数</dt>
      <dd>{{ action.attempt_count }}</dd>
    </dl>

    <section class="detail-section">
      <h3>策略裁决</h3>
      <dl v-if="action.policy_decision">
        <dt>结论</dt>
        <dd>{{ decisionLabels[action.policy_decision.decision] || action.policy_decision.decision }} / {{ action.policy_decision.reason }}</dd>
        <dt>策略版本</dt>
        <dd>{{ action.policy_decision.policy_version }}</dd>
        <dt>请求参数</dt>
        <dd class="mono">{{ fmtJson(action.policy_decision.requested_params) }}</dd>
      </dl>
      <p v-else class="empty">暂无策略记录</p>
    </section>

    <section class="detail-section">
      <h3>参数与结果</h3>
      <dl>
        <dt>参数</dt><dd class="mono">{{ fmtJson(action.params) }}</dd>
        <dt>结果</dt><dd class="mono">{{ fmtJson(action.result) }}</dd>
        <dt>耗时</dt><dd>{{ fmtResultMs(action.result, 'baseline') }} -> {{ fmtResultMs(action.result, 'after') }}</dd>
      </dl>
    </section>

    <section class="detail-section">
      <h3>验证时间线</h3>
      <ol class="verification-timeline">
        <li v-for="item in action.verifications || []" :key="item.verification_id">
          <span class="phase">{{ phaseLabels[item.phase] || item.phase }} #{{ item.attempt_no }}</span>
          <span :class="['verification-status', item.passed ? 'passed' : 'failed']">
            {{ item.passed ? '通过' : '失败' }}
          </span>
          <span>{{ fmtTime(item.created_at) }}</span>
          <p>{{ item.reason || '-' }}</p>
          <pre v-if="Object.keys(item.metrics || {}).length">{{ fmtJson(item.metrics) }}</pre>
        </li>
        <li v-if="!(action.verifications || []).length" class="empty">暂无验证记录</li>
      </ol>
    </section>

    <section class="detail-section">
      <h3>回滚</h3>
      <p>{{ action.rollback_plan || '未定义' }}</p>
      <pre v-if="Object.keys(action.rollback_result || {}).length">{{ fmtJson(action.rollback_result) }}</pre>
      <p v-if="action.status === 'failed'">
        该动作执行失败：有留档原值时按原值恢复；没有留档原值时不会改动任何数据，
        动作仍保持失败，请人工核对实际状态。
      </p>
      <button
        v-if="canRollback"
        class="rollback-button"
        :disabled="rollbackBusy"
        @click="rollbackAction"
      >
        {{ rollbackBusy ? '正在回滚' : '回滚此动作' }}
      </button>
      <p v-if="rollbackError" class="error">{{ rollbackError }}</p>
      <div v-if="canCheck" class="check-block">
        <p>
          这条动作已经无法由系统撤销。请先核对实际数据（必要时手工改回），
          再在此标记「已核对」结束它；填写的说明会写入审计日志。
        </p>
        <input
          v-model="checkNote"
          maxlength="500"
          placeholder="核对说明，例如：已手工恢复渠道开关"
        />
        <button
          class="check-button"
          :disabled="checkBusy || !checkNote.trim()"
          @click="checkAction"
        >
          {{ checkBusy ? '正在标记' : '标记为已核对' }}
        </button>
        <p v-if="checkError" class="error">{{ checkError }}</p>
      </div>
    </section>

    <dl class="timeline-meta">
      <dt>创建</dt><dd>{{ fmtTime(action.created_at) }}</dd>
      <dt>开始</dt><dd>{{ fmtTime(action.started_at) }}</dd>
      <dt>结束</dt><dd>{{ fmtTime(action.ended_at) }}</dd>
    </dl>
  </aside>
</template>

<style scoped>
.action-detail {
  border-left: 1px solid var(--border-color, #d5dae3);
  background: var(--bg-card, #fff);
  padding: 16px;
  overflow: auto;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
h2, h3 { margin: 0 0 10px; }
.detail-section { margin-top: 18px; }
dl {
  display: grid;
  grid-template-columns: 105px 1fr;
  gap: 7px 10px;
  margin: 0;
}
dt { color: var(--text-muted, #6b7280); font-size: 12px; }
dd { margin: 0; overflow-wrap: anywhere; }
.mono, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
pre {
  margin: 7px 0 0;
  padding: 8px;
  background: var(--bg-body, #f5f7fa);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.verification-timeline {
  margin: 0;
  padding-left: 20px;
}
.verification-timeline li { margin-bottom: 12px; }
.phase { font-weight: 600; margin-right: 8px; }
.verification-status { margin-right: 8px; }
.verification-status.passed { color: #067647; }
.verification-status.failed { color: #b42318; }
.verification-timeline p { margin: 5px 0 0; }
.timeline-meta { margin-top: 18px; }
.empty { color: var(--text-muted, #6b7280); }
.error { color: #b42318; font-size: 12px; }
.rollback-button { margin-top: 10px; }
.rollback-button:disabled { cursor: wait; opacity: 0.6; }
.check-block { margin-top: 12px; }
.check-block input { width: 100%; max-width: 420px; margin-bottom: 8px; }
.check-button:disabled { cursor: not-allowed; opacity: 0.6; }
.text-button {
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
</style>
