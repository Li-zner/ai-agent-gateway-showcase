<script setup lang="ts">
/** 监控草稿表单（新建/编辑）：保存后由父组件刷新列表并提示启用方式。 */
import { reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  createDbAgentMonitor,
  updateDbAgentMonitor,
  type DbAgentMonitor,
  type DbAgentMonitorOp,
} from '../api/dbagent'

const props = defineProps<{ monitor: DbAgentMonitor | null }>()
const emit = defineEmits<{ close: []; saved: [created: boolean] }>()

const { t } = useI18n()

const OP_OPTIONS: Array<{ value: DbAgentMonitorOp; label: string }> = [
  { value: 'gt', label: '> 大于' },
  { value: 'gte', label: '≥ 大于等于' },
  { value: 'lt', label: '< 小于' },
  { value: 'lte', label: '≤ 小于等于' },
]

const saving = ref(false)
const formError = ref('')
const form = reactive({
  name: props.monitor?.name ?? '',
  sql: props.monitor?.sql ?? '',
  op: (props.monitor?.op ?? 'gt') as DbAgentMonitorOp,
  threshold: props.monitor ? String(props.monitor.threshold) : '',
  interval_sec: String(props.monitor?.interval_sec ?? 60),
  unit: props.monitor?.unit ?? '',
})

function close(): void {
  if (saving.value) return
  emit('close')
}

async function save(): Promise<void> {
  const name = form.name.trim()
  const sql = form.sql.trim()
  const threshold = Number(form.threshold)
  const interval = Number(form.interval_sec)
  if (!name || !sql) {
    formError.value = t('dbagent_monitor_require_name_sql')
    return
  }
  if (!Number.isFinite(threshold)) {
    formError.value = t('dbagent_monitor_require_threshold')
    return
  }
  if (!Number.isInteger(interval) || interval < 30 || interval > 86400) {
    formError.value = t('dbagent_monitor_require_interval')
    return
  }
  const draft = {
    name,
    sql,
    op: form.op,
    threshold,
    interval_sec: interval,
    unit: form.unit.trim() || null,
  }
  saving.value = true
  formError.value = ''
  try {
    if (props.monitor) {
      await updateDbAgentMonitor(props.monitor.id, draft)
      emit('saved', false)
    } else {
      await createDbAgentMonitor(draft)
      emit('saved', true)
    }
  } catch (err) {
    formError.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="drawer-mask" @click.self="close">
    <form class="monitor-drawer" @submit.prevent="save">
      <h2>{{ monitor ? t('dbagent_monitor_form_edit') : t('dbagent_monitor_form_new') }}</h2>

      <label>
        <span>{{ t('dbagent_monitor_field_name') }}</span>
        <input v-model="form.name" maxlength="60" :placeholder="t('dbagent_monitor_field_name_ph')" />
      </label>

      <label>
        <span>{{ t('dbagent_monitor_field_sql') }}</span>
        <textarea
          v-model="form.sql"
          rows="4"
          spellcheck="false"
          :placeholder="t('dbagent_monitor_field_sql_ph')"
        ></textarea>
        <small>{{ t('dbagent_monitor_field_sql_hint') }}</small>
      </label>

      <div class="form-row">
        <label>
          <span>{{ t('dbagent_monitor_field_op') }}</span>
          <select v-model="form.op">
            <option v-for="opt in OP_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
          </select>
        </label>
        <label>
          <span>{{ t('dbagent_monitor_field_threshold') }}</span>
          <input v-model="form.threshold" inputmode="decimal" />
        </label>
        <label>
          <span>{{ t('dbagent_monitor_field_unit') }}</span>
          <input v-model="form.unit" maxlength="16" :placeholder="t('dbagent_monitor_field_unit_ph')" />
        </label>
      </div>

      <label>
        <span>{{ t('dbagent_monitor_field_interval') }}</span>
        <input v-model="form.interval_sec" inputmode="numeric" />
        <small>{{ t('dbagent_monitor_field_interval_hint') }}</small>
      </label>

      <p v-if="formError" class="form-error">{{ formError }}</p>

      <footer class="drawer-actions">
        <button type="button" class="ghost" :disabled="saving" @click="close">
          {{ t('dbagent_monitor_cancel') }}
        </button>
        <button type="submit" class="primary" :disabled="saving">
          {{ saving ? t('dbagent_monitor_saving') : t('dbagent_monitor_save') }}
        </button>
      </footer>
    </form>
  </div>
</template>

<style scoped>
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.32);
  display: flex;
  justify-content: flex-end;
  z-index: 40;
}

.monitor-drawer {
  width: min(440px, 92vw);
  height: 100%;
  overflow-y: auto;
  background: #fff;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.monitor-drawer h2 {
  margin: 0 0 4px;
  font-size: 16px;
}

.monitor-drawer label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: #4a5568;
}

.monitor-drawer input,
.monitor-drawer select,
.monitor-drawer textarea {
  font-size: 13px;
  padding: 6px 8px;
  border: 1px solid var(--border-color, #dfe5de);
  border-radius: 6px;
  font-family: inherit;
}

.monitor-drawer textarea {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  resize: vertical;
}

.monitor-drawer small {
  color: var(--text-muted, #6b7280);
  font-size: 11px;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 10px;
}

.form-error {
  margin: 0;
  color: #9b1c1c;
  font-size: 12px;
}

.drawer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: auto;
  padding-top: 8px;
}

.drawer-actions button {
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

button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
</style>
