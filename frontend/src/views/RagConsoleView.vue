<script setup lang="ts">
/** RAG 运营控制台：概览、请求时间线、incident 和修复动作。 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  approveRagAction,
  cancelRagAction,
  clearStaleIncidents,
  getRagAction,
  getRagIncident,
  getRagRequest,
  getRagStatus,
  listRagActions,
  listRagIncidents,
  listRagRequests,
  runRagActionWorker,
  type RagAction,
  type RagIncident,
  type RagRequest,
  type RagRequestContent,
  type RagSpan,
  type RagStatus,
} from '../api/ragAdmin'
import { ApiError } from '../api/http'
import RagActionWorkspace from '../components/RagActionWorkspace.vue'
import RagEvalPanel from '../components/RagEvalPanel.vue'
import RagPermsPanel from '../components/RagPermsPanel.vue'
import RagIncidentDetail from '../components/RagIncidentDetail.vue'
import RagOverviewSummary from '../components/RagOverviewSummary.vue'
import RagRequestDetail from '../components/RagRequestDetail.vue'
import { useAuthStore } from '../stores/auth'
import { compareHandledIncidents, compareUnhandledIncidents, isHandledIncident, staleClearConfirmMessage, staleClearableIncidents } from '../utils/ragIncidents'
import { maskOwner } from '../utils/ragFormat'
import {
  label_action,
  label_persona,
  label_risk,
  label_route,
  label_severity,
  label_status,
} from '../utils/ragLabels'

type TabKey = 'overview' | 'requests' | 'incidents' | 'actions' | 'eval' | 'perms'
// 2026-10-03：子导航收敛——问数迁运维会话顶栏，Agent 对话台入口由顶栏承担；
// 新增「权限」= 子账号能力管理（业主 #2）

const router = useRouter()
const auth = useAuthStore()
const activeTab = ref<TabKey>('overview')
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const forbidden = ref(false)
const lastUpdated = ref('')
// 概览与请求列表是否计入评测流量（persona=civil_code_eval），默认排除
const includeEval = ref(false)
const status = ref<RagStatus | null>(null)
const requests = ref<RagRequest[]>([])
const incidents = ref<RagIncident[]>([])
const actions = ref<RagAction[]>([])
const selectedAction = ref<RagAction | null>(null)
const selectedRequest = ref<(RagRequest & { spans: RagSpan[]; content: RagRequestContent }) | null>(null)
const selectedIncident = ref<RagIncident | null>(null)
const mutationBusy = ref(false)
// 写操作失败专用错误位：不能复用 error —— 模板里 `v-else-if="error"` 与
// 承载全部分区的 `<template v-else>` 同属一条链，一次审批失败会把整个控制台
// 表格区替换成错误面板，用户只能刷新才能继续操作。
const mutationError = ref('')
let timer: number | undefined
let detailSeq = 0
let refreshInFlight = false

// 超管批量清理阈值，须与服务端 stale_hours ge=24 下限一致，
// 否则「可清理 N 条」的前端预告会和服务端实际清掉的条数不一致
const CLEAR_STALE_HOURS = 24
const clearNotice = ref('')
const clearableIncidents = computed(() =>
  staleClearableIncidents(incidents.value, CLEAR_STALE_HOURS),
)

const unhandledIncidents = computed(() =>
  incidents.value.filter((item) => !isHandledIncident(item)).sort(compareUnhandledIncidents),
)
const handledIncidents = computed(() =>
  incidents.value.filter(isHandledIncident).sort(compareHandledIncidents),
)
const incidentGroups = computed(() => [
  { key: 'unhandled', title: '未处理事件', items: unhandledIncidents.value },
  { key: 'handled', title: '已处理事件', items: handledIncidents.value },
])
const orderedIncidents = computed(() => [
  ...unhandledIncidents.value,
  ...handledIncidents.value,
])
const activeIncidentCount = computed(() => unhandledIncidents.value.length)
const pendingActionCount = computed(
  () => actions.value.filter((item) => ['queued', 'waiting_approval'].includes(item.status)).length,
)

function fmtTime(value?: string | null): string {
  if (!value) return '-'
  return value.slice(0, 19).replace('T', ' ')
}

function fmtMs(value?: number | null): string {
  return value == null ? '-' : `${value}ms`
}

function tabClass(tab: TabKey): Record<string, boolean> {
  return { active: activeTab.value === tab }
}

async function loadOverview() {
  status.value = await getRagStatus(includeEval.value)
}

async function loadRequests() {
  requests.value = (await listRagRequests(50, includeEval.value)).items
}

async function loadIncidents() {
  incidents.value = (await listRagIncidents(50)).items
}

async function loadActions() {
  actions.value = (await listRagActions(50)).items
}

/** 刷新当前详情，避免列表已更新而审计面板仍停留在旧状态。 */
async function refreshSelectedDetails(): Promise<void> {
  const request = selectedRequest.value
  const incident = selectedIncident.value
  const action = selectedAction.value
  const tasks: Promise<void>[] = []

  if (request) {
    tasks.push(getRagRequest(request.request_uid).then((detail) => {
      if (selectedRequest.value === request) selectedRequest.value = detail
    }))
  }
  if (incident) {
    tasks.push(getRagIncident(incident.incident_id).then((detail) => {
      if (selectedIncident.value === incident) selectedIncident.value = detail
    }))
  }
  if (action) {
    tasks.push(getRagAction(action.action_id).then((detail) => {
      if (selectedAction.value === action) selectedAction.value = detail
    }))
  }
  await Promise.allSettled(tasks)
}

async function refreshAll(showSpinner = true) {
  if (refreshInFlight) return
  refreshInFlight = true
  if (showSpinner) refreshing.value = true
  error.value = ''
  try {
    await Promise.all([loadOverview(), loadRequests(), loadIncidents(), loadActions()])
    await refreshSelectedDetails()
    lastUpdated.value = new Date().toLocaleTimeString()
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    refreshInFlight = false
    loading.value = false
    refreshing.value = false
  }
}

async function openRequest(requestUid: string) {
  // 2026-09-14 逐行审查修复：事件详情的「查看请求」也会调到这里——不切标签页
  // 的话 selectedRequest 渲染在 requests 页内，用户停在 incidents 页点了没反应
  activeTab.value = 'requests'
  const seq = ++detailSeq
  const detail = await getRagRequest(requestUid)
  if (seq === detailSeq) selectedRequest.value = detail
}

async function openIncident(incidentId: number) {
  // 同 openRequest（2026-09-14 逐行审查）：概览页点事件行也要切到 incidents 页，
  // 否则详情面板渲染在别处，点击无反应
  activeTab.value = 'incidents'
  const seq = ++detailSeq
  const detail = await getRagIncident(incidentId)
  if (seq === detailSeq) selectedIncident.value = detail
}

async function openAction(actionId: number) {
  activeTab.value = 'actions'
  selectedAction.value = await getRagAction(actionId)
}

/** 事件或动作变更后刷新当前详情和对应列表。 */
async function refreshIncidentAndActions(): Promise<void> {
  const incident = selectedIncident.value
  if (!incident) return
  const [detail] = await Promise.all([
    getRagIncident(incident.incident_id),
    loadIncidents(),
    loadActions(),
  ])
  if (selectedIncident.value === incident) selectedIncident.value = detail
}

/** 人工回滚后重新读取动作，保证审计状态与按钮同步。 */
async function refreshSelectedAction(): Promise<void> {
  const action = selectedAction.value
  if (!action) return
  const [detail] = await Promise.all([
    getRagAction(action.action_id),
    loadActions(),
  ])
  if (selectedAction.value === action) selectedAction.value = detail
}

/** 动作分区共用刷新：新提议进列表，回滚结果进详情。 */
async function refreshActionsAndDetail(): Promise<void> {
  await Promise.all([loadActions(), refreshSelectedAction()])
}

async function approveAction(actionId: number) {
  await withMutation(async () => {
    await approveRagAction(actionId)
    await loadActions()
  })
}

async function cancelAction(actionId: number) {
  await withMutation(async () => {
    await cancelRagAction(actionId)
    await loadActions()
  })
}

async function runWorker() {
  await withMutation(async () => {
    await runRagActionWorker()
    await loadActions()
  })
}

/** 超管批量清理：把静默超阈值的活跃事件标为解决；服务端只改状态不删行。 */
async function clearStale() {
  const items = clearableIncidents.value
  if (!items.length) return
  if (!window.confirm(staleClearConfirmMessage(items, CLEAR_STALE_HOURS))) return
  clearNotice.value = ''
  await withMutation(async () => {
    const result = await clearStaleIncidents(CLEAR_STALE_HOURS)
    clearNotice.value = `已清理 ${result.cleared_count} 条未复发事件`
    await Promise.all([loadIncidents(), loadActions()])
  })
}

/** 写操作串行化，避免双击或轮询期间重复提交状态变更；失败必须落到错误条可见。 */
async function withMutation(task: () => Promise<void>): Promise<void> {
  if (mutationBusy.value) return
  mutationBusy.value = true
  mutationError.value = ''
  try {
    await task()
  } catch (err) {
    // 原先不 catch：审批/取消失败变成 unhandled rejection，界面看起来像成功了
    // （2026-09-19 审查 P2-3）
    mutationError.value = err instanceof Error ? err.message : '操作失败'
  } finally {
    mutationBusy.value = false
  }
}

onMounted(async () => {
  if (!auth.profileLoaded) {
    try {
      await auth.loadProfile()
    } catch {
      return
    }
  }
  if (auth.user?.role !== 'admin') {
    forbidden.value = true
    loading.value = false
    return
  }
  await refreshAll()
  timer = window.setInterval(() => void refreshAll(false), 15000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <main class="rag-console">
    <!-- 顶部导航栏：固定浮在窗口顶部，正文由 .rag-console 的 padding-top 整体下移让位 -->
    <nav class="console-nav" aria-label="控制台全局导航">
      <span class="nav-brand">RAG 控制台</span>
      <div class="nav-links">
        <button class="nav-link is-active" type="button">运营控制台</button>
        <button class="nav-link" type="button" @click="router.push('/admin/rag/chat')">运维会话</button>
      </div>
      <div class="nav-actions">
        <!-- 评测流量（persona=civil_code_eval）默认不计入运营统计，勾选后算回 -->
        <label class="eval-toggle" title="含 civil_code_eval 人格的批量评测流量">
          <input type="checkbox" v-model="includeEval" @change="refreshAll()" />
          含评测流量
        </label>
        <span class="updated">更新 {{ lastUpdated || '-' }}</span>
        <button class="secondary" :disabled="refreshing" @click="refreshAll()">刷新</button>
      </div>
    </nav>

    <section v-if="forbidden" class="state-panel">
      当前账号没有 RAG 管理权限。
    </section>
    <section v-else-if="loading" class="state-panel">正在加载监测数据...</section>
    <section v-else-if="error" class="state-panel danger">{{ error }}</section>

    <template v-else>
      <nav class="tabs" aria-label="RAG 控制台分区">
        <button :class="tabClass('overview')" @click="activeTab = 'overview'">概览</button>
        <button :class="tabClass('requests')" @click="activeTab = 'requests'">请求</button>
        <button :class="tabClass('incidents')" @click="activeTab = 'incidents'">事件 {{ activeIncidentCount }}</button>
        <button :class="tabClass('actions')" @click="activeTab = 'actions'">动作 {{ pendingActionCount }}</button>
        <button :class="tabClass('eval')" @click="activeTab = 'eval'">测评</button>
        <button :class="tabClass('perms')" @click="activeTab = 'perms'">权限</button>
      </nav>

      <!-- 写操作失败横幅（P2-3）：放在 tabs 之后，失败只提示不吞掉整个分区表格。 -->
      <section v-if="mutationError" class="state-banner danger" role="alert">
        <span>{{ mutationError }}</span>
        <button class="text-button" @click="mutationError = ''">知道了</button>
      </section>

      <section v-if="activeTab === 'overview' && status" class="overview">
        <RagOverviewSummary :status="status" :active-incident-count="activeIncidentCount" />

        <div class="split">
          <section class="panel">
            <div class="panel-head"><h2>最近事件</h2><span>{{ incidents.length }} 条</span></div>
            <table>
              <thead><tr><th title="严重=错误依据已送达且用户无法察觉｜高=答案失去依据兜底｜中=质量或性能降级但兜底仍在｜低=仅统计信号">级别</th><th>状态</th><th>规则</th><th>标题</th><th>出现</th></tr></thead>
              <tbody>
                <tr v-for="item in orderedIncidents.slice(0, 8)" :key="item.incident_id" @click="openIncident(item.incident_id)">
                  <td><span class="severity" :class="item.severity">{{ label_severity(item.severity) }}</span></td>
                  <td>{{ label_status(item.status) }}</td><td>{{ item.code }}</td><td>{{ item.title }}</td><td>{{ item.occurrence_count }}</td>
                </tr>
                <tr v-if="!incidents.length"><td colspan="5" class="empty">暂无事件</td></tr>
              </tbody>
            </table>
          </section>
          <section class="panel recent-actions">
            <div class="panel-head"><h2>最近动作</h2><button class="text-button" @click="runWorker">执行队列</button></div>
            <table>
              <thead><tr><th>动作</th><th>级别</th><th>状态</th><th>验证</th></tr></thead>
              <tbody>
                <tr
                  v-for="item in actions.slice(0, 8)"
                  :key="item.action_id"
                  :class="{ selected: selectedAction?.action_id === item.action_id }"
                  @click="openAction(item.action_id)"
                >
                  <td>{{ label_action(item.action_key) }}</td>
                  <td>{{ label_risk(item.risk_level) }}</td>
                  <td>{{ label_status(item.status) }}</td>
                  <td>{{ item.verification?.passed === true ? '通过' : item.verification?.passed === false ? '失败' : '-' }}</td>
                </tr>
                <tr v-if="!actions.length"><td colspan="4" class="empty">暂无动作</td></tr>
              </tbody>
            </table>
          </section>
        </div>
      </section>

      <section v-else-if="activeTab === 'requests'" class="workspace">
        <section class="panel table-panel">
          <div class="panel-head"><h2>请求时间线</h2><span>{{ requests.length }} 条</span></div>
          <table>
            <thead><tr><th>时间</th><th>状态</th><th>路径</th><th>人格</th><th>模型</th><th>首包</th><th>总耗时</th></tr></thead>
            <tbody>
              <tr v-for="item in requests" :key="item.request_uid" @click="openRequest(item.request_uid)">
                <td>{{ fmtTime(item.created_at) }}</td>
                <td>{{ label_status(item.status) }}</td>
                <td>{{ label_route(item.route) }}</td>
                <td>{{ label_persona(item.persona) }}</td><td>{{ item.selected_model || '-' }}</td>
                <td>{{ fmtMs(item.first_token_ms) }}</td><td>{{ fmtMs(item.total_ms) }}</td>
              </tr>
            </tbody>
          </table>
        </section>
        <RagRequestDetail v-if="selectedRequest" :request="selectedRequest" @close="selectedRequest = null" />
      </section>

      <section v-else-if="activeTab === 'incidents'" class="workspace">
        <section class="panel table-panel">
          <div class="panel-head">
            <h2>事件</h2><span>{{ incidents.length }} 条</span>
            <span v-if="clearNotice" class="clear-notice">{{ clearNotice }}</span>
            <button class="text-button" :disabled="mutationBusy || !clearableIncidents.length" @click="clearStale">清理未复发({{ clearableIncidents.length }})</button>
          </div>
          <table>
            <thead><tr><th>编号</th><th title="严重=错误依据已送达且用户无法察觉｜高=答案失去依据兜底｜中=质量或性能降级但兜底仍在｜低=仅统计信号">级别</th><th>状态</th><th>规则</th><th>标题</th><th>影响</th><th>最近出现</th><th>负责人</th></tr></thead>
            <tbody v-for="group in incidentGroups" :key="group.key">
              <tr class="incident-group-row">
                <td colspan="8">{{ group.title }} <span>{{ group.items.length }}</span></td>
              </tr>
              <tr v-for="item in group.items" :key="item.incident_id" :class="{ selected: selectedIncident?.incident_id === item.incident_id }" @click="openIncident(item.incident_id)">
                <td>#{{ item.incident_id }}</td>
                <td><span class="severity" :class="item.severity">{{ label_severity(item.severity) }}</span></td>
                <td>{{ label_status(item.status) }}</td><td>{{ item.code }}</td><td>{{ item.title }}</td>
                <td>{{ item.affected_requests }}</td><td>{{ fmtTime(item.last_seen_at) }}</td><td>{{ maskOwner(item.owner) }}</td>
              </tr>
              <tr v-if="!group.items.length">
                <td colspan="8" class="empty">{{ group.title }}暂无记录</td>
              </tr>
            </tbody>
          </table>
        </section>
        <RagIncidentDetail
          v-if="selectedIncident"
          :incident="selectedIncident"
          @close="selectedIncident = null"
          @open-request="openRequest"
          @refresh="refreshIncidentAndActions"
        />
      </section>

      <RagActionWorkspace
        v-else-if="activeTab === 'actions'"
        :actions="actions"
        :selected="selectedAction"
        @open="openAction"
        @close="selectedAction = null"
        @approve="approveAction"
        @cancel="cancelAction"
        @run-worker="runWorker"
        @refresh="refreshActionsAndDetail"
      />

      <section v-else-if="activeTab === 'eval'" class="eval-workspace" aria-label="离线测评报告">
        <RagEvalPanel />
      </section>

      <section v-else class="eval-workspace" aria-label="子账号能力管理">
        <RagPermsPanel />
      </section>
    </template>
  </main>
</template>

<style scoped>
.rag-console {
  /* 2026-10-02 修复导航遮蔽：导航 fixed 在顶部 48px，本容器作为独立滚动区
     从 68px 起步（48 + 20 间距）——滚动发生在容器内部，内容顶部永远不会
     滚进导航栏区域（旧版整页滚动时卡片会从 fixed 导航底下穿过被切掉上半） */
  margin-top: 68px;
  height: calc(100vh - 68px);
  height: calc(100dvh - 68px);
  overflow-y: auto;
  padding: 0 20px 20px;
  background: var(--bg-body, #f5f7fa);
  color: var(--text-primary, #1f2937);
}
.console-nav {
  position: fixed;
  top: 0;
  right: 0;
  left: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  gap: 16px;
  height: 48px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border-color, #d5dae3);
  background: var(--bg-card, #fff);
}
.nav-brand { font-size: 14px; font-weight: 600; white-space: nowrap; }
.nav-links { display: flex; gap: 4px; }
.nav-link {
  min-height: 30px;
  padding: 4px 12px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted, #6b7280);
}
.nav-link:hover { background: rgba(37, 99, 235, 0.06); }
.nav-link.is-active { background: rgba(37, 99, 235, 0.1); color: #1d4ed8; font-weight: 600; }
.nav-actions { display: flex; align-items: center; gap: 10px; margin-left: auto; }
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.updated, .clear-notice { color: var(--text-muted, #6b7280); font-size: 12px; }
.eval-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted, #6b7280);
  cursor: pointer;
  white-space: nowrap;
}
/* 通配 input 规则给了 34px min-height，复选框需要复位 */
.eval-toggle input { min-height: 0; width: 15px; height: 15px; }
button, select, input {
  min-height: 34px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 6px;
  background: var(--bg-card, #fff);
  color: inherit;
  padding: 6px 10px;
}
button { cursor: pointer; }
button:disabled { cursor: wait; opacity: 0.6; }
button.danger { color: #b42318; }
.tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border-color, #d5dae3);
}
.tabs button {
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  background: transparent;
}
.tabs button.active { border-bottom-color: #2563eb; color: #1d4ed8; font-weight: 600; }
.split, .workspace {
  display: grid;
  grid-template-columns: minmax(0, 1.7fr) minmax(320px, 1fr);
  gap: 14px;
}
.split { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.eval-workspace { display: grid; gap: 14px; }
.panel, .detail-panel, .state-panel {
  padding: 14px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 8px;
  background: var(--bg-card, #fff);
}
.state-panel { max-width: 720px; margin: 80px auto; text-align: center; }
.state-panel.danger { color: #b42318; }
.state-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  padding: 10px 14px;
  border: 1px solid #fda29b;
  border-left-width: 4px;
  border-radius: 8px;
  background: #fff6f5;
  color: #b42318;
  font-size: 13px;
}
.panel h2, .detail-panel h2 { margin: 0; font-size: 14px; }
.panel-head { margin-bottom: 10px; color: var(--text-muted, #6b7280); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 6px; border-bottom: 1px solid var(--border-color, #e5e7eb); text-align: left; }
th { color: var(--text-muted, #6b7280); font-weight: 500; }
tbody tr { cursor: pointer; }
tbody tr:hover, tbody tr.selected { background: rgba(37, 99, 235, 0.05); }
.incident-group-row { background: var(--bg-body, #f5f7fa); cursor: default; }
.incident-group-row td { color: var(--text-muted, #6b7280); font-size: 12px; font-weight: 600; }
.incident-group-row span { margin-left: 4px; font-weight: 400; }
.severity { display: inline-block; padding: 1px 7px; border-radius: 4px; background: #eef2ff; }
.severity.high { color: #b42318; background: #fee4e2; } .severity.critical { color: #fff; background: #b42318; font-weight: 700; }
.severity.medium { color: #b45309; background: #fef0c7; }
.severity.low { color: #175cd3; background: #eff8ff; }
.mono { overflow-wrap: anywhere; font-family: Consolas, monospace; }
.timeline { margin: 12px 0 0; padding-left: 24px; }
.timeline li { display: grid; grid-template-columns: 1fr auto auto auto; gap: 8px; padding: 6px 0; font-size: 12px; }
.stage small { margin-left: 4px; color: var(--text-muted, #6b7280); }
.span-status { text-transform: uppercase; }
.span-status.failed { color: #b42318; }
.detail-panel dl { display: grid; grid-template-columns: 110px 1fr; gap: 7px 10px; font-size: 13px; }
.detail-panel dt { color: var(--text-muted, #6b7280); }
.detail-panel dd { margin: 0; overflow-wrap: anywhere; }
.row-actions { display: flex; gap: 6px; }
.row-actions button { min-height: 28px; padding: 3px 8px; }
.text-button { min-height: auto; padding: 0; border: 0; background: transparent; color: #1d4ed8; }
.empty, .error { color: var(--text-muted, #6b7280); }
.error { color: #b42318; }
@media (max-width: 900px) {
  .split, .workspace { grid-template-columns: 1fr; }
  .table-panel { overflow-x: auto; }
  .console-nav { gap: 8px; padding: 0 12px; overflow-x: auto; }
  .nav-brand { display: none; }
}
</style>
