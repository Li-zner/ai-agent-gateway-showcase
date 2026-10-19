<script setup lang="ts">
/**
 * 检索测评汇总（2026-10-02 业主定稿改版）：不再是"全历史最新一份"的单卡，
 * 而是每份报告一张卡、按生成时间倒排——live-* 报告按日从监控 trace 复算，
 * 一天一份，卡片即"当日测评/监测成果汇总"；离线基线也各自成卡。
 * 数据来自 GET /api/admin/rag/evaluations（列表接口自带摘要，无需逐份拉明细）。
 */
import { computed, onMounted, ref } from 'vue'
import {
  listRagEvaluations,
  type RagEvalReport,
  type RagStatus,
} from '../api/ragAdmin'
import { fmtDuration } from '../utils/ragFormat'

defineProps<{
  runtime: RagStatus['runtime_config']
}>()

const reports = ref<RagEvalReport[]>([])
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    reports.value = (await listRagEvaluations()).items
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载测评报告失败'
  } finally {
    loading.value = false
  }
})

const isLive = (name: string): boolean => name.startsWith('live-')

/** 报告名里抠出日期（live-chat-retrieval-20260918.json → 2026-09-18），抠不出给修改日。 */
function reportDate(item: RagEvalReport): string {
  const m = item.report.match(/(\d{4})(\d{2})(\d{2})/)
  if (m) return `${m[1]}-${m[2]}-${m[3]}`
  return item.modified_at.slice(0, 10)
}

const KIND_LABEL: Record<string, string> = {
  retrieval: '检索测评',
  e2e: '端到端评分',
  raw: '其他',
}

function fmtPercent(value?: number | null): string {
  return value == null ? '-' : `${(value * 100).toFixed(1)}%`
}

function fmtNumber(value?: number | null): string {
  return value == null ? '-' : value.toFixed(3)
}

// 端到端类没有 recall：verdicts 里正确类占比作"通过率"
function verdictRate(item: RagEvalReport): string | null {
  const verdicts = item.summary.verdicts
  if (!verdicts) return null
  const total = Object.values(verdicts).reduce((a, b) => a + b, 0)
  if (!total) return null
  const good = (verdicts.core ?? 0) + (verdicts.correct ?? 0)
  return `${((good / total) * 100).toFixed(1)}%`
}

const zeroHitCount = (item: RagEvalReport): number => item.summary.zero_hit_at_10?.length ?? 0

const headline = computed(() =>
  reports.value.length
    ? `${reports.value.length} 份报告 · 最新 ${reportDate(reports.value[0])}`
    : '暂无报告',
)
</script>

<template>
  <section class="evaluation-panel">
    <div class="panel-head">
      <div>
        <h2>检索测评 · 按报告（每日）汇总</h2>
        <p>{{ headline }}（逐条明细见「测评」分区）</p>
      </div>
      <div class="panel-meta">
        <span :class="{ override: runtime.rerank_top_overridden }">
          重排候选池 {{ runtime.rerank_top }}
          {{ runtime.rerank_top_observed != null ? '（网关实测）' : '（近期无 trace，回落配置值）' }}
          {{ runtime.rerank_top_overridden ? ' · Redis 覆盖中' : '' }}
        </span>
      </div>
    </div>

    <p v-if="loading" class="hint">正在加载测评报告...</p>
    <p v-else-if="error" class="hint danger">{{ error }}</p>
    <p v-else-if="!reports.length" class="hint">
      暂无报告：把测评 JSON 放进 app/rag_eval/reports/ 并重建镜像即可。
    </p>

    <div v-else class="eval-cards">
      <article v-for="item in reports" :key="item.report" class="eval-card" :class="{ live: isLive(item.report) }">
        <header>
          <strong>{{ reportDate(item) }}</strong>
          <span class="kind">{{ KIND_LABEL[item.kind] ?? item.kind }}</span>
          <span class="src">{{ isLive(item.report) ? 'chat 真跑（监控口径）' : '离线固定基线' }}</span>
        </header>
        <p class="report-name" :title="item.report">{{ item.report }}</p>
        <div class="mini-metrics">
          <template v-if="item.kind === 'retrieval'">
            <div><span>Recall@5</span><strong>{{ fmtPercent(item.summary.recall_at_5) }}</strong></div>
            <div><span>Recall@10</span><strong>{{ fmtPercent(item.summary.recall_at_10) }}</strong></div>
            <div><span>MRR</span><strong>{{ fmtNumber(item.summary.mrr) }}</strong></div>
          </template>
          <template v-else>
            <div><span>通过率</span><strong>{{ verdictRate(item) ?? '-' }}</strong></div>
            <div><span>样本</span><strong>{{ item.summary.evaluated }}</strong></div>
          </template>
          <div><span>样本数</span><strong>{{ item.summary.evaluated }}</strong></div>
          <div>
            <span>平均 / P95</span>
            <strong>{{ fmtDuration(item.summary.latency_ms?.avg ?? null) }} / {{ fmtDuration(item.summary.latency_ms?.p95 ?? null) }}</strong>
          </div>
        </div>
        <footer>
          <span v-if="item.summary.errors" class="error">{{ item.summary.errors }} 条错误</span>
          <span v-if="item.kind === 'retrieval'" :class="{ error: zeroHitCount(item) > 0 }">
            零命中 {{ zeroHitCount(item) }} 题
          </span>
        </footer>
      </article>
    </div>
  </section>
</template>

<style scoped>
.evaluation-panel {
  margin-bottom: 14px;
  padding: 14px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 8px;
  background: var(--bg-card, #fff);
}
.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.panel-meta { display: grid; justify-items: end; gap: 4px; font-size: 12px; }
.panel-meta .override { color: #b45309; font-weight: 600; }
h2 { margin: 0; font-size: 14px; }
p { margin: 3px 0 0; color: var(--text-muted, #6b7280); font-size: 12px; }
.hint { color: var(--text-muted, #6b7280); font-size: 13px; }
.hint.danger { color: #b42318; }

/* 卡片流：一天一份，窄屏掉到两列/一列 */
.eval-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.eval-card {
  padding: 12px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-left: 4px solid #98a2b3;
  border-radius: 8px;
}
.eval-card.live { border-left-color: #3d7a5c; }
.eval-card header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  color: var(--text-muted, #6b7280);
}
.eval-card header strong { font-size: 15px; color: inherit; }
.report-name {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--text-muted, #6b7280);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.mini-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  margin-top: 10px;
}
.mini-metrics span { display: block; color: var(--text-muted, #6b7280); font-size: 11px; }
.mini-metrics strong { display: block; margin-top: 2px; font-size: 14px; }
.eval-card footer {
  display: flex;
  gap: 10px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-muted, #6b7280);
}
.error { color: #b42318; }
</style>
