<script setup lang="ts">
/**
 * 数据监控页：卡片展示单指标 SQL 的最近一次采集值、阈值状态与采集时间。
 * 轮询只是重读落库结果（10s），真正执行由后台调度器按项间隔完成；
 * 手动「立即执行」「启用/停用」「新建/编辑」都走 dbagent.monitor 能力位。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  listDbAgentMonitors,
  runDbAgentMonitorOnce,
  updateDbAgentMonitor,
  type DbAgentMonitor,
} from '../api/dbagent'
import {
  formatMonitorRule,
  formatMonitorValue,
  isMonitorStale,
  monitorStatusClass,
} from '../utils/dbagentMonitor'
import RagDbAgentMonitorForm from './RagDbAgentMonitorForm.vue'

const { t } = useI18n()

const POLL_MS = 10_000

const monitors = ref<DbAgentMonitor[]>([])
const loading = ref(true)
const error = ref('')
const notice = ref('')
const busyId = ref<number | null>(null)

const drawerOpen = ref(false)
const editing = ref<DbAgentMonitor | null>(null)

const enabledCount = computed(() => monitors.value.filter((m) => m.enabled).length)

/** 采集时间/成功时间统一按本地时间展示；空值不伪造时间。 */
function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

/** 状态标签：三态之外不把断采显示成正常。 */
function statusLabel(monitor: DbAgentMonitor): string {
  return t(`dbagent_monitor_status_${monitor.last_status}`)
}

/** 草稿 / 已停用 / 启用中：有采集历史才算「已停用」，否则仍是草稿。 */
function stateLabel(monitor: DbAgentMonitor): string {
  if (monitor.enabled) return t('dbagent_monitor_state_enabled')
  return monitor.last_run_at
    ? t('dbagent_monitor_state_disabled')
    : t('dbagent_monitor_state_draft')
}

async function load(): Promise<void> {
  try {
    monitors.value = await listDbAgentMonitors()
    error.value = ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/** 仅页面可见时轮询：切到后台就停表，回来后立即补一次。 */
function startPolling(): void {
  stopPolling()
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') void load()
  }, POLL_MS)
}

function onVisibility(): void {
  if (document.visibilityState === 'visible') {
    void load()
    startPolling()
  } else {
    stopPolling()
  }
}

onMounted(() => {
  void load()
  startPolling()
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibility)
})

async function runNow(monitor: DbAgentMonitor): Promise<void> {
  busyId.value = monitor.id
  notice.value = ''
  try {
    const result = await runDbAgentMonitorOnce(monitor.id)
    await load()
    if (result.status === 'unknown') {
      error.value = `${monitor.name}: ${result.error || t('dbagent_monitor_run_failed')}`
    } else {
      notice.value = t('dbagent_monitor_run_done', {
        name: monitor.name,
        value: formatMonitorValue(result.value, monitor.unit),
      })
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyId.value = null
  }
}

async function toggleEnabled(monitor: DbAgentMonitor): Promise<void> {
  const next = !monitor.enabled
  if (next && !window.confirm(t('dbagent_monitor_confirm_enable', { name: monitor.name }))) {
    return
  }
  busyId.value = monitor.id
  notice.value = ''
  try {
    await updateDbAgentMonitor(monitor.id, { enabled: next })
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busyId.value = null
  }
}

function openCreate(): void {
  editing.value = null
  drawerOpen.value = true
}

function openEdit(monitor: DbAgentMonitor): void {
  editing.value = monitor
  drawerOpen.value = true
}

/** 表单保存成功：父组件负责提示与刷新（草稿仍停用，需人工启用）。 */
async function onSaved(created: boolean): Promise<void> {
  drawerOpen.value = false
  notice.value = created
    ? t('dbagent_monitor_saved_draft')
    : t('dbagent_monitor_saved')
  await load()
}
</script>

<template>
  <section class="monitors-page">
    <header class="monitors-head">
      <div>
        <h1>{{ t('dbagent_monitor_title') }}</h1>
        <p class="subtitle">{{ t('dbagent_monitor_subtitle') }}</p>
        <p class="summary">
          {{ t('dbagent_monitor_summary', { total: monitors.length, enabled: enabledCount }) }}
        </p>
      </div>
      <div class="head-actions">
        <button class="ghost" :disabled="loading" @click="load">{{ t('dbagent_monitor_refresh') }}</button>
        <button class="primary" @click="openCreate">{{ t('dbagent_monitor_new') }}</button>
      </div>
    </header>

    <div v-if="error" class="banner danger">
      <span>{{ error }}</span>
      <button class="text-button" @click="error = ''">{{ t('dbagent_monitor_dismiss') }}</button>
    </div>
    <div v-if="notice" class="banner info">
      <span>{{ notice }}</span>
      <button class="text-button" @click="notice = ''">{{ t('dbagent_monitor_dismiss') }}</button>
    </div>

    <p v-if="loading" class="state">{{ t('dbagent_monitor_loading') }}</p>
    <p v-else-if="!monitors.length" class="state">{{ t('dbagent_monitor_empty') }}</p>

    <div v-else class="monitor-grid">
      <article
        v-for="monitor in monitors"
        :key="monitor.id"
        class="monitor-card"
        :class="[monitorStatusClass(monitor.last_status), { disabled: !monitor.enabled }]"
      >
        <div class="card-head">
          <span class="card-name" :title="monitor.name">{{ monitor.name }}</span>
          <span class="badge" :class="monitorStatusClass(monitor.last_status)">
            {{ statusLabel(monitor) }}
          </span>
          <span class="badge state">{{ stateLabel(monitor) }}</span>
        </div>

        <div class="card-value">
          <strong>{{ formatMonitorValue(monitor.last_value, monitor.unit) }}</strong>
        </div>

        <div class="card-meta">
          <span>{{ t('dbagent_monitor_rule', {
            rule: formatMonitorRule(monitor.op, monitor.threshold, monitor.unit),
            sec: monitor.interval_sec,
          }) }}</span>
          <span>{{ t('dbagent_monitor_last_run', { time: formatTime(monitor.last_run_at) }) }}</span>
          <span>{{ t('dbagent_monitor_last_ok', { time: formatTime(monitor.last_ok_at) }) }}</span>
          <span v-if="isMonitorStale(monitor)" class="warn-tag">{{ t('dbagent_monitor_stale') }}</span>
          <span v-else-if="monitor.enabled && !monitor.last_ok_at" class="warn-tag">
            {{ t('dbagent_monitor_never_ok') }}
          </span>
        </div>

        <p v-if="monitor.last_error" class="card-error" :title="monitor.last_error">
          {{ monitor.last_error }}
        </p>

        <footer class="card-actions">
          <button
            :disabled="busyId === monitor.id"
            @click="runNow(monitor)"
          >{{ t('dbagent_monitor_run_now') }}</button>
          <button
            :disabled="busyId === monitor.id"
            :class="{ danger: monitor.enabled }"
            @click="toggleEnabled(monitor)"
          >{{ monitor.enabled ? t('dbagent_monitor_disable') : t('dbagent_monitor_enable') }}</button>
          <button @click="openEdit(monitor)">{{ t('dbagent_monitor_edit') }}</button>
        </footer>
      </article>
    </div>

    <!-- 新建/编辑抽屉：agent 只能建停用草稿，启用一律由这里的人操作 -->
    <RagDbAgentMonitorForm
      v-if="drawerOpen"
      :monitor="editing"
      @close="drawerOpen = false"
      @saved="onSaved"
    />
  </section>
</template>

<style scoped>
.monitors-page {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 22px 28px;
}

.monitors-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.monitors-head h1 {
  font-size: 18px;
  margin: 0 0 4px;
}

.subtitle,
.summary {
  margin: 0;
  color: var(--text-muted, #6b7280);
  font-size: 12px;
  line-height: 1.6;
}

.head-actions {
  display: flex;
  gap: 8px;
}

.banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  margin-bottom: 10px;
  border-radius: 6px;
  font-size: 13px;
}

.banner.danger {
  background: #fdecec;
  color: #9b1c1c;
  border: 1px solid #f5c2c2;
}

.banner.info {
  background: #eef7f1;
  color: #276749;
  border: 1px solid #c6e3cf;
}

.state {
  padding: 40px 8px;
  text-align: center;
  color: var(--text-muted, #6b7280);
}

.monitor-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}

.monitor-card {
  border: 1px solid var(--border-color, #dfe5de);
  border-left-width: 4px;
  border-radius: 8px;
  background: var(--bg-card, #fff);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.monitor-card.monitor-ok {
  border-left-color: #2f855a;
}

.monitor-card.monitor-breach {
  border-left-color: #c53030;
  background: #fffafa;
}

.monitor-card.monitor-unknown {
  border-left-color: #b7791f;
}

.monitor-card.disabled {
  opacity: 0.75;
}

.card-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.card-name {
  font-weight: 600;
  font-size: 14px;
  margin-right: auto;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid transparent;
}

.badge.monitor-ok {
  background: #e6f4ea;
  color: #276749;
}

.badge.monitor-breach {
  background: #fdecec;
  color: #9b1c1c;
}

.badge.monitor-unknown {
  background: #fdf3e1;
  color: #8a5a12;
}

.badge.state {
  background: #f1f3f2;
  color: #4a5568;
}

.card-value strong {
  font-size: 26px;
  font-weight: 650;
  line-height: 1.2;
  word-break: break-all;
}

.card-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: var(--text-muted, #6b7280);
}

.warn-tag {
  align-self: flex-start;
  margin-top: 3px;
  padding: 1px 8px;
  border-radius: 10px;
  background: #fdf3e1;
  color: #8a5a12;
  font-weight: 600;
}

.card-error {
  margin: 0;
  font-size: 12px;
  color: #9b1c1c;
  background: #fdecec;
  border-radius: 4px;
  padding: 6px 8px;
  max-height: 64px;
  overflow: hidden;
}

.card-actions {
  display: flex;
  gap: 6px;
  margin-top: auto;
  padding-top: 4px;
}

.card-actions button,
.head-actions button {
  font-size: 12px;
  padding: 5px 10px;
  border-radius: 6px;
  border: 1px solid var(--border-color, #dfe5de);
  background: #fff;
  cursor: pointer;
}

button.primary {
  background: var(--primary, #3d7a5c);
  border-color: var(--primary, #3d7a5c);
  color: #fff;
}

button.danger {
  color: #9b1c1c;
  border-color: #f5c2c2;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.text-button {
  border: none;
  background: none;
  cursor: pointer;
  font-size: 12px;
  color: inherit;
  text-decoration: underline;
}
</style>
