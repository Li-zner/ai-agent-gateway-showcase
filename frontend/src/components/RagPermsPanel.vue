<script setup lang="ts">
/**
 * 子账号能力管理（2026-10-03 二轮定稿）：左侧 Excel 式账号列表（行选中），
 * 右侧详情面板（创建时间/口令说明/能力矩阵/启停/删除）。
 * 所有交互弹窗为本页内嵌 modal（创建/改密/删除确认），不再用浏览器原生弹窗；
 * 轻提示用自动消失 toast。
 */
import { computed, onMounted, ref } from 'vue'
import {
  createSubAccount,
  deleteSubAccount,
  listSubAccounts,
  resetSubPassword,
  setSubStatus,
  updateSubAccountPerms,
  type CapabilitySpec,
  type SubAccount,
} from '../api/dbagent'

interface Row extends SubAccount {
  orig: string[]  // 上次保存的能力快照，dirty = 当前 ≠ 快照
}

const rows = ref<Row[]>([])
const caps = ref<CapabilitySpec[]>([])
const loading = ref(true)
const error = ref('')
const selectedName = ref('')

const selected = computed<Row | null>(
  () => rows.value.find((r) => r.username === selectedName.value) ?? null,
)

// ---- toast（自动消失，无关闭按钮）----
const toast = ref('')
let toastTimer: number | undefined
function showToast(text: string): void {
  toast.value = text
  if (toastTimer) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.value = '' }, 2500)
}

async function refresh(keep?: string): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const data = await listSubAccounts()
    rows.value = data.items.map((s) => ({ ...s, orig: [...s.permissions] }))
    caps.value = data.capabilities
    if (keep && rows.value.some((r) => r.username === keep)) selectedName.value = keep
    else if (!rows.value.some((r) => r.username === selectedName.value)) {
      selectedName.value = rows.value[0]?.username ?? ''
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载子账号失败'
  } finally {
    loading.value = false
  }
}

function isDirty(row: Row): boolean {
  return row.permissions.join(',') !== row.orig.join(',')
}

function toggleCap(row: Row, key: string): void {
  row.permissions = row.permissions.includes(key)
    ? row.permissions.filter((k) => k !== key)
    : [...row.permissions, key]
}

async function save(row: Row): Promise<void> {
  try {
    await updateSubAccountPerms(row.username, row.permissions)
    row.orig = [...row.permissions]
    showToast(`${row.username} 已保存`)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  }
}

async function toggleActive(row: Row): Promise<void> {
  try {
    await setSubStatus(row.username, !row.is_active)
    row.is_active = !row.is_active
    showToast(`${row.username} 已${row.is_active ? '启用' : '停用'}`)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '操作失败'
  }
}

// ---- modal：create（创建）/ reset（改密）/ delete（删除确认）----
type ModalKind = 'create' | 'reset' | 'delete' | null
const modal = ref<ModalKind>(null)
const createForm = ref({ username: '', password: '' })
const createPerms = ref<string[]>(['dbagent.view'])
const resetForm = ref({ password: '', confirm: '' })
const creating = ref(false)
const busy = ref(false)

const usernameOk = () => createForm.value.username.trim().length >= 3
const passwordOk = () => createForm.value.password.length >= 8
const resetOk = () => resetForm.value.password.length >= 8
  && resetForm.value.password === resetForm.value.confirm

function openCreate(): void {
  createForm.value = { username: '', password: '' }
  createPerms.value = ['dbagent.view']
  modal.value = 'create'
}

function openReset(): void {
  resetForm.value = { password: '', confirm: '' }
  modal.value = 'reset'
}

async function submitCreate(): Promise<void> {
  creating.value = true
  error.value = ''
  try {
    const name = createForm.value.username.trim()
    await createSubAccount({
      username: name,
      password: createForm.value.password,
      permissions: createPerms.value,
    })
    modal.value = null
    await refresh(name)
    showToast('子账号已创建，可用该账密登录中控台')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '创建失败'
  } finally {
    creating.value = false
  }
}

async function submitReset(): Promise<void> {
  if (!selected.value) return
  busy.value = true
  try {
    await resetSubPassword(selected.value.username, resetForm.value.password)
    modal.value = null
    showToast(`${selected.value.username} 口令已重置`)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '重置失败'
  } finally {
    busy.value = false
  }
}

async function submitDelete(): Promise<void> {
  if (!selected.value) return
  busy.value = true
  try {
    const name = selected.value.username
    await deleteSubAccount(name)
    modal.value = null
    await refresh()
    showToast(`${name} 已删除`)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除失败'
  } finally {
    busy.value = false
  }
}

function fmtTime(value: string): string {
  return value ? value.replace('T', ' ').slice(0, 19) : '-'
}

onMounted(refresh)
</script>

<template>
  <section class="perms-panel" aria-label="子账号能力管理">
    <transition name="toast-fade">
      <p v-if="toast" class="toast">{{ toast }}</p>
    </transition>
    <p v-if="loading" class="hint">正在加载...</p>
    <p v-else-if="error" class="hint danger">{{ error }}</p>

    <div v-if="!loading" class="perms-layout">
      <!-- 左：Excel 式账号列表 -->
      <div class="perms-left">
        <table class="accounts-table">
          <thead>
            <tr><th>账号</th><th>状态</th><th>能力</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.username"
              :class="{ selected: row.username === selectedName, off: !row.is_active }"
              @click="selectedName = row.username"
            >
              <td class="cell-user">{{ row.username }}</td>
              <td>
                <span class="status-badge" :class="row.is_active ? 'on' : 'off'">
                  {{ row.is_active ? '启用' : '停用' }}
                </span>
              </td>
              <td class="cell-cap">{{ row.permissions.length }} 项</td>
            </tr>
            <tr v-if="!rows.length"><td colspan="3" class="empty">暂无子账号</td></tr>
          </tbody>
        </table>
        <button class="primary" @click="openCreate">＋ 创建子账号</button>
      </div>

      <!-- 右：选中账号详情 -->
      <div v-if="selected" class="perms-right">
        <header class="detail-head">
          <h2>{{ selected.username }}</h2>
          <span class="status-badge" :class="selected.is_active ? 'on' : 'off'">
            {{ selected.is_active ? '启用' : '停用' }}
          </span>
        </header>

        <dl class="meta-list">
          <div><dt>创建时间</dt><dd>{{ fmtTime(selected.created_at) }}</dd></div>
          <div>
            <dt>口令</dt>
            <dd>
              ••••••••（加密存储，仅可重置）
              <button class="link-btn" @click="openReset">重置口令</button>
            </dd>
          </div>
        </dl>

        <div class="cap-block">
          <h3>能力设置</h3>
          <label v-for="cap in caps" :key="cap.key" class="cap-row" :title="cap.desc">
            <input
              type="checkbox"
              :checked="selected.permissions.includes(cap.key)"
              @change="toggleCap(selected, cap.key)"
            />
            <span class="cap-label">{{ cap.label }}</span>
            <span class="cap-desc">{{ cap.desc }}</span>
          </label>
          <button class="primary save-btn" :disabled="!isDirty(selected)" @click="save(selected)">
            保存修改
          </button>
        </div>

        <div class="danger-zone">
          <button @click="toggleActive(selected)">{{ selected.is_active ? '停用账号' : '启用账号' }}</button>
          <button class="danger" @click="modal = 'delete'">删除账号</button>
        </div>
      </div>
      <div v-else class="perms-right empty-pane">
        <p class="hint">左侧选择一个账号查看详情</p>
      </div>
    </div>

    <!-- 中控台内嵌 modal（替代浏览器原生弹窗） -->
    <div v-if="modal" class="modal-mask" @click.self="modal = null">
      <div class="modal-card" role="dialog">
        <!-- 创建 -->
        <template v-if="modal === 'create'">
          <h3>创建子账号</h3>
          <div class="field">
            <input v-model="createForm.username" placeholder="用户名（至少 3 个字符）" />
            <small v-if="createForm.username && !usernameOk()">用户名至少 3 个字符</small>
          </div>
          <div class="field">
            <input v-model="createForm.password" type="password" placeholder="口令（至少 8 位）" />
            <small v-if="createForm.password && !passwordOk()">口令至少 8 位</small>
          </div>
          <div class="cap-row modal-caps">
            <label v-for="cap in caps" :key="cap.key" :title="cap.desc">
              <input type="checkbox" :value="cap.key" v-model="createPerms" />
              {{ cap.label }}
            </label>
          </div>
          <p class="hint">创建后即可用该账号与口令登录中控台。</p>
          <footer>
            <button class="ghost" @click="modal = null">取消</button>
            <button class="primary" :disabled="creating || !usernameOk() || !passwordOk()" @click="submitCreate">
              {{ creating ? '创建中' : '创建' }}
            </button>
          </footer>
        </template>

        <!-- 改密 -->
        <template v-else-if="modal === 'reset'">
          <h3>重置口令 · {{ selected?.username }}</h3>
          <div class="field">
            <input v-model="resetForm.password" type="password" placeholder="新口令（至少 8 位）" />
            <small v-if="resetForm.password && resetForm.password.length < 8">口令至少 8 位</small>
          </div>
          <div class="field">
            <input v-model="resetForm.confirm" type="password" placeholder="再输入一次" />
            <small v-if="resetForm.confirm && resetForm.confirm !== resetForm.password">两次输入不一致</small>
          </div>
          <footer>
            <button class="ghost" @click="modal = null">取消</button>
            <button class="primary" :disabled="busy || !resetOk()" @click="submitReset">
              {{ busy ? '提交中' : '确认重置' }}
            </button>
          </footer>
        </template>

        <!-- 删除 -->
        <template v-else-if="modal === 'delete'">
          <h3>删除子账号</h3>
          <p>确定删除 <strong>{{ selected?.username }}</strong>？该操作不可恢复。</p>
          <footer>
            <button class="ghost" @click="modal = null">取消</button>
            <button class="primary delete-confirm" :disabled="busy" @click="submitDelete">
              {{ busy ? '删除中' : '确认删除' }}
            </button>
          </footer>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.perms-panel { display: flex; flex-direction: column; gap: 10px; position: relative; }
.hint { color: var(--text-muted, #6b7280); font-size: 13px; margin: 0; }
.hint.danger { color: #b42318; }

.toast {
  position: absolute; top: -6px; right: 0; z-index: 20;
  margin: 0; padding: 6px 14px; border-radius: 999px;
  background: #2f6e50; color: #fff; font-size: 13px;
}
.toast-fade-enter-active, .toast-fade-leave-active { transition: opacity .25s; }
.toast-fade-enter-from, .toast-fade-leave-to { opacity: 0; }

/* 左右分栏 */
.perms-layout { display: grid; grid-template-columns: minmax(240px, 320px) minmax(0, 1fr); gap: 16px; }

/* 左：Excel 式列表 */
.perms-left { display: flex; flex-direction: column; gap: 10px; }
.accounts-table { border-collapse: collapse; font-size: 13px; width: 100%; }
.accounts-table th, .accounts-table td {
  border: 1px solid var(--border-color, #d5dae3); padding: 7px 10px; text-align: left;
}
.accounts-table th { background: rgba(127,127,127,.06); color: var(--text-muted, #6b7280); font-weight: 600; }
.accounts-table tbody tr { cursor: pointer; }
.accounts-table tbody tr:hover { background: rgba(47,110,80,.06); }
.accounts-table tr.selected { background: rgba(47,110,80,.12); }
.accounts-table tr.off .cell-user { opacity: .55; }
.cell-user { font-weight: 600; }
.cell-cap { color: var(--text-muted, #6b7280); }
.empty { color: var(--text-muted, #6b7280); text-align: center; }

.status-badge {
  display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600;
}
.status-badge.on { background: #e7f1ec; color: #2f6e50; }
.status-badge.off { background: #eef2f6; color: #5a6b7a; }

/* 右：详情面板 */
.perms-right {
  border: 1px solid var(--border-color, #d5dae3); border-radius: 8px;
  padding: 16px 18px; background: var(--bg-card, #fff);
  display: flex; flex-direction: column; gap: 14px;
}
.empty-pane { align-items: center; justify-content: center; }
.detail-head { display: flex; align-items: center; gap: 10px; }
.detail-head h2 { margin: 0; font-size: 17px; }

.meta-list { margin: 0; display: flex; flex-direction: column; gap: 6px; }
.meta-list > div { display: flex; gap: 10px; font-size: 13px; }
.meta-list dt { color: var(--text-muted, #6b7280); flex: none; width: 64px; }
.meta-list dd { margin: 0; }

.link-btn { border: 0; background: none; color: #2f6e50; cursor: pointer; font-size: 13px; padding: 0; }

.cap-block h3 { margin: 0 0 8px; font-size: 13px; color: var(--text-muted, #6b7280); }
.cap-row { display: flex; align-items: baseline; gap: 8px; padding: 4px 0; font-size: 13px; }
.cap-row .cap-label { flex: none; font-weight: 600; }
.cap-row .cap-desc { color: var(--text-muted, #6b7280); font-size: 12px; }
.save-btn { margin-top: 8px; align-self: flex-start; }
.modal-caps { flex-wrap: wrap; }

.danger-zone {
  border-top: 1px dashed var(--border-color, #d5dae3); padding-top: 12px;
  display: flex; gap: 8px;
}
.danger-zone button, footer button {
  min-height: 32px; padding: 5px 14px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--border-color, #d5dae3); background: var(--bg-card, #fff);
}
.danger-zone .danger, .delete-confirm { color: #b42318; border-color: #e4b5b0; }

.primary {
  border: 0; background: #2f6e50; color: #fff; border-radius: 6px;
  min-height: 32px; padding: 5px 16px; cursor: pointer;
}
.primary:disabled { background: #b9c6be; cursor: default; }
.ghost { background: transparent; }

/* 内嵌 modal */
.modal-mask {
  position: fixed; inset: 0; z-index: 50;
  background: rgba(15, 23, 20, 0.45);
  display: flex; align-items: center; justify-content: center;
}
.modal-card {
  width: min(420px, calc(100vw - 48px));
  background: var(--bg-card, #fff); border-radius: 10px;
  padding: 18px 20px; display: flex; flex-direction: column; gap: 12px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}
.modal-card h3 { margin: 0; font-size: 15px; }
.modal-card footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }
.field { display: flex; flex-direction: column; gap: 3px; }
.field input {
  padding: 8px 10px; font: inherit;
  border: 1px solid var(--border-color, #d5dae3); border-radius: 6px;
}
.field small { color: #b45309; font-size: 12px; }
</style>
