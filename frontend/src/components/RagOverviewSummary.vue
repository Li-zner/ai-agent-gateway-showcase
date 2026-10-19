<script setup lang="ts">
/** RAG 概览摘要：优先展示待处理告警，再展示评测和容量指标。 */
import { onMounted, ref } from 'vue'
import { listRagEvaluations } from '../api/ragAdmin'
import type { RagStatus } from '../api/ragAdmin'
import { fmtAgo, fmtDuration } from '../utils/ragFormat'
import RagEvaluationPanel from './RagEvaluationPanel.vue'

const props = defineProps<{
  status: RagStatus
  activeIncidentCount: number
}>()

// DB Agent 黄金评测夜跑通过率（#6）：读 reports 里最新的 dbagent-golden-*
const goldenNote = ref('')
const goldenMeta = ref('')

onMounted(async () => {
  try {
    const { items } = await listRagEvaluations()
    const latest = items.find((item) => item.report.startsWith('dbagent-golden-'))
    if (latest) {
      goldenNote.value = latest.summary.note || ''
      goldenMeta.value = `${latest.report.replace('dbagent-golden-', '').replace('.json', '')} · ${latest.summary.evaluated} 例`
    }
  } catch {
    /* 概览不因评测列表失败而挂 */
  }
})
</script>

<template>
  <section class="alert-panel" aria-label="待处理告警">
    <div class="panel-head">
      <h2>待处理告警</h2>
      <span>优先处理项</span>
    </div>
    <div class="alert-grid">
      <article class="alert-card" :class="{ active: props.activeIncidentCount > 0 }">
        <span>活跃事件</span>
        <strong>{{ props.activeIncidentCount }}</strong>
      </article>
      <article class="alert-card" :class="{ active: props.status.health_meta.waiting_actions > 0 }">
        <span>待审批动作</span>
        <strong>{{ props.status.health_meta.waiting_actions }}</strong>
      </article>
      <article class="alert-card" :class="{ active: props.status.retrieval_24h.empty_recall > 0 }">
        <span>空召回</span>
        <strong>{{ props.status.retrieval_24h.empty_recall }}</strong>
      </article>
      <article class="alert-card" :class="{ active: props.status.requests_24h.p95_latency_ms > 10000 }">
        <span>端到端 95 分位耗时</span>
        <strong>{{ fmtDuration(props.status.requests_24h.p95_latency_ms) }}</strong>
      </article>
    </div>

    <!-- DB Agent 黄金评测夜跑（#6）：有报告才显示 -->
    <div v-if="goldenNote" class="golden-note">
      <span class="golden-label">DB Agent 评测</span>
      <span>{{ goldenNote }}</span>
      <span class="golden-meta">{{ goldenMeta }} · 明细见「测评」</span>
    </div>
  </section>

  <RagEvaluationPanel :runtime="props.status.runtime_config" />

  <section class="capacity-panel">
    <div class="panel-head">
      <h2>容量与运行指标</h2>
      <span>近 24 小时与队列</span>
    </div>
    <div class="metric-grid">
      <article class="metric">
        <span>24 小时请求量</span><strong>{{ props.status.requests_24h.total }}</strong>
      </article>
      <article class="metric">
        <span>24 小时检索量</span><strong>{{ props.status.retrieval_24h.total }}</strong>
      </article>
      <article class="metric" :class="{ danger: props.status.requests_24h.failed > 0 }">
        <span>失败请求</span><strong>{{ props.status.requests_24h.failed }}</strong>
      </article>
      <article class="metric" :class="{ warn: props.status.retrieval_24h.rerank_dropped > 0 }">
        <span>重排降级</span><strong>{{ props.status.retrieval_24h.rerank_dropped }}</strong>
      </article>
      <article class="metric">
        <span>距最新链路写入</span>
        <strong>{{ fmtAgo(props.status.health_meta.trace_lag_seconds) }}</strong>
      </article>
      <article class="metric" :class="{ warn: props.status.health_meta.queued_actions > 0 }">
        <span>排队动作</span><strong>{{ props.status.health_meta.queued_actions }}</strong>
      </article>
      <article class="metric" :class="{ warn: props.status.health_meta.executing_actions > 0 }">
        <span>执行中动作</span><strong>{{ props.status.health_meta.executing_actions }}</strong>
      </article>
    </div>
  </section>
</template>

<style scoped>
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
  color: var(--text-muted, #6b7280);
}
h2 { margin: 0; font-size: 14px; }
.alert-panel,
.capacity-panel { margin-bottom: 14px; }
.alert-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.alert-card {
  padding: 12px;
  border: 1px solid var(--border-color, #d5dae3);
  border-left: 4px solid #98a2b3;
  border-radius: 8px;
  background: var(--bg-card, #fff);
}
.alert-card.active { border-left-color: #d92d20; background: #fffbfa; }
.alert-card span { display: block; color: var(--text-muted, #6b7280); font-size: 12px; }
.alert-card strong { display: block; margin-top: 5px; font-size: 23px; }

/* DB Agent 夜跑通过率条 */
.golden-note {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 9px 12px;
  border: 1px solid var(--border-color, #d5dae3);
  border-left: 4px solid #3d7a5c;
  border-radius: 8px;
  background: var(--bg-card, #fff);
  font-size: 13px;
}
.golden-label { font-weight: 700; color: #3d7a5c; flex: none; }
.golden-meta { margin-left: auto; color: var(--text-muted, #6b7280); font-size: 12px; flex: none; }
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
}
.metric {
  padding: 14px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 8px;
  background: var(--bg-card, #fff);
}
.metric span { display: block; color: var(--text-muted, #6b7280); font-size: 12px; }
.metric strong { display: block; margin-top: 6px; font-size: 24px; }
.metric.warn strong { color: #b45309; }
.metric.danger strong { color: #b42318; }
@media (max-width: 900px) {
  .alert-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
