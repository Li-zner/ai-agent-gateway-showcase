<script setup lang="ts">
/** 聊天主界面：旧版 .app 骨架。默认旅游人格；顶部民法典按钮切换人格 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ApiError } from '../api/http'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import { useUiStore } from '../stores/ui'
import { uploadFile, type UploadedFile } from '../api/files'
import { rateMessage } from '../api/user'
import { getWallet } from '../api/payment'
import MessageBubble from '../components/MessageBubble.vue'
import SessionList from '../components/SessionList.vue'
import SettingsModal from '../components/SettingsModal.vue'
import WalletModal from '../components/WalletModal.vue'
import TxModal from '../components/TxModal.vue'
import BindPhoneModal from '../components/BindPhoneModal.vue'
import RatingModal from '../components/RatingModal.vue'
import WelcomePanel from '../components/WelcomePanel.vue'
const { t } = useI18n()
const router = useRouter()
const auth = useAuthStore()
const chat = useChatStore()
const ui = useUiStore()
const input = ref('')
const uploads = ref<UploadedFile[]>([])
const uploadError = ref('')
const balance = ref<number | null>(null)
const rateIndex = ref<number | null>(null)
const txOpen = ref(false)
const drawerOpen = ref(false)
/** 顶部栏收起/展开（小三角切换） */
const headerCollapsed = ref(false)
/** 手机端人格下拉 */
const ddOpen = ref(false)
// 本地兜底选项只列真实人格（2026-09-12 主人定夺：去掉 unified 幻影「旅行」——
// unified 是后端降级 id，此前被误标为「旅行」与 travel 重复出现在下拉里）
const personaOptions = computed(() => [
  { value: 'civil_code', label: t('civil_mode') },
])
// 后端人格列表优先（人格可动态增减），本地兜底选项去重后追加
const allPersonaOptions = computed(() => {
  const merged = chat.personas.map((p) => ({ value: p.id, label: p.name }))
  for (const o of personaOptions.value) {
    if (!merged.some((x) => x.value === o.value)) merged.push(o)
  }
  return merged
})
// 旧会话可能残留 unified（行为等同默认 travel）：标签回落到首选项而非幻影「旅行」
const personaLabel = computed(
  () => allPersonaOptions.value.find((o) => o.value === chat.currentPersonaId)?.label
    ?? allPersonaOptions.value[0]?.label ?? t('travel_mode'))
function pickPersona(value: string) {
  ddOpen.value = false
  if (value !== chat.currentPersonaId) switchPersona(value)
}
/** 点外部关闭下拉 */
function onDocClick() {
  ddOpen.value = false
}
// ---------- 滚动跟随：流式时贴底自动滚，用户上翻则停手 + 悬浮"回到底部" ----------
const messagesEl = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLTextAreaElement | null>(null)
const showScrollBottom = ref(false)
const nearBottom = ref(true)
function onMessagesScroll() {
  const el = messagesEl.value
  if (!el) return
  const dist = el.scrollHeight - el.scrollTop - el.clientHeight
  nearBottom.value = dist < 120
  showScrollBottom.value = dist > 300
}
function scrollToBottom() {
  const el = messagesEl.value
  if (el) el.scrollTop = el.scrollHeight
}
// 流式增量（正文/思考/工具行）或消息条数变化时，贴底状态才自动跟随
watch(
  () => {
    const msgs = chat.messages
    const last = msgs[msgs.length - 1]
    return `${chat.currentSessionId}|${msgs.length}|${last?.content.length ?? 0}|${last?.reasoning?.length ?? 0}|${last?.tools?.length ?? 0}`
  },
  async () => {
    if (!nearBottom.value) return
    await nextTick()
    scrollToBottom()
  },
)
// 会话切换/首屏渲染后贴底
watch(() => chat.currentSessionId, async () => {
  await nextTick()
  scrollToBottom()
})
// ---------- 多行输入：Enter 发送 / Shift+Enter 换行 / 自动增高 ----------
function autoGrow() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 132) + 'px'
}
onMounted(() => {
  document.addEventListener('click', onDocClick)
})
onUnmounted(() => document.removeEventListener('click', onDocClick))
onMounted(async () => {
  ui.initTheme()
  try {
    await auth.ensureProfile()
  } catch (e) {
    // 2026-09-12 清欠（D-F14）：仅凭证失效才踢登录，瞬时 5xx/网络抖动不打扰用户
    if (e instanceof ApiError && e.status === 401) router.push('/login')
    return
  }
  // profile 成功后立即按新 owner 重建会话，后续人格接口失败也不会残留旧账号数据。
  chat.initSessions()
  // 会话列表必须先加载完成，autoPrompt 才有 currentSession 可发（否则 send 静默返回 → 无输出）
  await chat.loadPersonas().catch(() => {})
  refreshBalance()
  if (chat.autoPrompt) {
    const prompt = chat.autoPrompt
    chat.setAutoPrompt('')
    await chat.send(prompt)
  }
})
// 人格切换时清空输入与附件（会话数据由 store 响应式切换，无需销毁组件）
watch(() => chat.currentPersonaId, () => {
  input.value = ''
  uploads.value = []
})
async function refreshBalance() {
  try { balance.value = (await getWallet()).balance } catch { /* 静默 */ }
}
/** 与后端 file_upload.ALLOWED_EXTENSIONS / MAX_FILE_SIZE 对齐的前端预检
 *  （2026-09-10 审查 P2：原先无任何前端预检，大文件要先传完才被后端拒绝） */
const MAX_UPLOAD_MB = 20
const ALLOWED_EXTS = ['.txt', '.md', '.csv', '.json', '.xml', '.yaml', '.yml',
  '.rst', '.rtf', '.pdf', '.jpg', '.jpeg', '.png', '.bmp', '.webp', '.docx']
async function onFilePicked(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  uploadError.value = ''
  const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!ALLOWED_EXTS.includes(ext)) {
    uploadError.value = t('unsupported_file_type')
    target.value = ''
    return
  }
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
    uploadError.value = t('file_too_large')
    target.value = ''
    return
  }
  // 2026-09-12 修复（外部复核 P1）：上传是异步的，期间切换人格会把旧人格的
  // 附件塞进新人格的输入区——发起时快照人格，完成时已切换则丢弃
  const personaAtStart = chat.currentPersonaId
  try {
    const uploaded = await uploadFile(file)
    if (chat.currentPersonaId === personaAtStart) uploads.value.push(uploaded)
  }
  catch (err) { uploadError.value = err instanceof Error ? err.message : t('upload_failed') }
  finally { target.value = '' }
}
function clearUploads() { uploads.value = [] }

/** textarea Enter 发送 / Shift+Enter 换行；IME 组词确认的回车走默认确认行为 */
function onEnter(e: KeyboardEvent) {
  if (e.shiftKey) return
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  void doSend()
}

async function doSend() {
  if (chat.streaming) { chat.cancelStream(); return }
  const text = input.value.trim()
  if (!text && !uploads.value.length) return
  const files = uploads.value.map((u) => u.filename)
  const ids = uploads.value.map((u) => u.file_id)
  // 立即清空：流式可持续数十秒，已发送内容挂在输入框里像没发出去
  input.value = ''
  uploads.value = []
  if (inputEl.value) inputEl.value.style.height = 'auto'
  await chat.send(text, ids)
  const lastUser = [...chat.messages].reverse().find((m) => m.role === 'user')
  if (lastUser && files.length) lastUser.files = files
  void refreshBalance()
}

function newConversation() {
  chat.newConversation()
  uploads.value = []
  drawerOpen.value = false
}

function onDelete(index: number) {
  if (window.confirm(t('delete_this_qa'))) chat.deleteQA(index)
}

// ---------- 评分 ----------
function openRate(index: number) { rateIndex.value = index }

async function submitRate(star: number) {
  if (rateIndex.value === null || !star) return
  const session = chat.currentSession
  const idx = rateIndex.value
  const userMsg = session?.messages[idx - 1]
  try {
    await rateMessage({
      rating: star,
      session_id: session?.conversationId ?? '',
      user_message: userMsg?.content ?? '',
      assistant_message: chat.messages[idx]?.content ?? '',
    })
    chat.markRated(idx)
  } catch {
    // 评分失败关闭弹窗但不标记已评（可重新评），不打断聊天
  }
  rateIndex.value = null
}

// ---------- 人格切换（顶部按钮：民法典） ----------
function switchPersona(id: string) {
  if (chat.streaming) return
  chat.switchPersona(id)
}

/** 预设问题 / 追问建议直发（不经 input 框） */
async function send(q: string) {
  await chat.send(q)
}
</script>

<template>
  <div class="app show">
    <div
      v-if="drawerOpen"
      class="sidebar-backdrop"
      @click="drawerOpen = false"
    ></div>
    <aside
      class="sidebar"
      :class="{ 'drawer-open': drawerOpen }"
    >
      <div class="sidebar-title">
        <span>{{ t('sidebar_title_1') }}</span>
        <span>{{ t('sidebar_title_2') }}</span>
        <button
          class="drawer-close"
          @click="drawerOpen = false"
        >✕</button>
      </div>
      <button
        class="new-chat-btn"
        @click="() => { newConversation(); drawerOpen = false }"
      >{{ t('new_chat') }}</button>

      <div class="history-label">{{ t('history_label') }}</div>
      <SessionList />

      <div class="sidebar-footer">
        <div>{{ t('sessions_saved_local') }}</div>
        <button @click="ui.openSettings()">{{ t('settings') }}</button>
      </div>
    </aside>

    <div class="chat-area">
      <!-- 收起/展开区：小三角骑在矩形下缘中部，随矩形一起上收（collapsed 时矩形高度塌为 0，三角贴顶） -->
      <div class="header-collapse-zone" :class="{ collapsed: headerCollapsed }">
      <div class="chat-header">
        <div class="header-left">
          <button class="chat-hamburger" @click="drawerOpen = true">☰</button>
          <button class="wallet-btn" @click="ui.openWallet()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="5" width="22" height="14" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M16 12h4"/></svg>
            <span class="balance-label">{{ t('balance') }}</span>
            <span class="balance-text">{{ balance !== null ? `¥${balance.toFixed(2)}` : '—' }}</span>
          </button>
          <span
            v-if="auth.user?.quota_limited && (auth.user?.remaining_questions ?? 0) > 0"
            class="quota-badge"
            @click="chat.bindPhoneTip = t('bind_phone_trial_tip')"
          >{{ t('quota_badge', { n: auth.user?.remaining_questions }) }}</span>
        </div>
        <div class="header-actions">
          <router-link to="/map" class="map-btn">{{ t('map') }}</router-link>
          <button
            class="mode-btn mode-dd-trigger"
            @click.stop="ddOpen = !ddOpen"
          >{{ personaLabel }}<span class="dd-caret">▾</span></button>
        </div>
      </div>
      <!-- 人格下拉：挂 zone 层（chat-header 的 overflow:hidden 会裁剪菜单致手机端点不到） -->
      <div class="mode-dropdown" @click.stop>
        <transition name="fadeup">
          <div v-if="ddOpen" class="dd-menu">
            <button
              v-for="opt in allPersonaOptions"
              :key="opt.value"
              class="dd-item"
              :class="{ current: opt.value === chat.currentPersonaId }"
              @click="pickPersona(opt.value)"
            >{{ opt.label }}</button>
          </div>
        </transition>
      </div>
      <!-- 展开态 ▴=点击收起；收起态 ▾=点击下拉。骑缝定位在 header 下缘中部 -->
      <button
        class="header-toggle"
        :title="headerCollapsed ? t('header_expand') : t('header_collapse')"
        @click="headerCollapsed = !headerCollapsed"
      >{{ headerCollapsed ? '▾' : '▴' }}</button>
      </div>

      <div
        ref="messagesEl"
        class="chat-messages"
        @scroll.passive="onMessagesScroll"
      >
        <WelcomePanel
          v-if="chat.messages.length === 0"
          :persona-id="chat.currentPersonaId"
          @send="send"
        />
        <MessageBubble
          v-for="(m, i) in chat.messages"
          :key="m.id ?? i"
          :message="m"
          :index="i"
          :is-last="i === chat.messages.length - 1"
          @delete="onDelete"
          @rate="openRate"
        />
      </div>

      <!-- 回到底部悬浮键：上翻离开底部时出现 -->
      <Transition name="fadeup">
        <button
          v-if="showScrollBottom"
          class="scroll-bottom-btn"
          :title="t('scroll_bottom')"
          :aria-label="t('scroll_bottom')"
          @click="scrollToBottom"
        >↓</button>
      </Transition>

      <div v-if="uploads.length" class="file-indicator uploading">
        <div class="file-progress">
          <svg viewBox="0 0 24 24">
            <circle class="track" cx="12" cy="12" r="10"/>
            <circle class="fill-arc" cx="12" cy="12" r="10" stroke-dasharray="62.83" stroke-dashoffset="0"/>
          </svg>
          <svg
            class="icon-overlay"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
          </svg>
        </div>
        <span class="file-label">{{ uploads.map((u) => u.filename).join('、') }}（{{ uploads.reduce((n, u) => n + u.text_length, 0) }} {{ t('chars') }}）</span>
        <button class="file-close" @click="clearUploads">✕</button>
      </div>

      <div class="chat-input-area">
        <label class="upload-btn" :title="t('upload_title')">
          <input type="file" accept=".txt,.md,.csv,.json,.xml,.yaml,.yml,.rst,.rtf,.pdf,.jpg,.jpeg,.png,.bmp,.webp,.docx" @change="onFilePicked">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        </label>
        <div class="input-wrapper">
          <textarea
            ref="inputEl"
            v-model="input"
            rows="1"
            :placeholder="t('input_ph')"
            @keydown.enter="onEnter"
            @input="autoGrow"
          ></textarea>
        </div>
        <button :class="{ 'btn-cancel': chat.streaming }" @click="doSend">
          {{ chat.streaming ? t('cancel_stream') : t('send') }}
        </button>
      </div>

      <SettingsModal v-if="ui.settingsOpen" />
      <WalletModal
        v-if="ui.walletOpen"
        @refresh-balance="refreshBalance"
        @open-tx="ui.closeWallet(); txOpen = true"
      />
      <TxModal v-if="txOpen" @close="txOpen = false" />
      <BindPhoneModal
        v-if="chat.bindPhoneTip"
        :tip="chat.bindPhoneTip"
        @close="chat.bindPhoneTip = ''"
        @bound="refreshBalance"
      />
      <RatingModal
        v-if="rateIndex !== null"
        @close="rateIndex = null"
        @submit="submitRate"
      />
    </div>
  </div>
</template>


<style scoped>
/* 回到底部悬浮键（chat-area 定位锚点） */
.chat-area {
  position: relative;
}
.scroll-bottom-btn {
  position: absolute;
  right: 18px;
  bottom: 96px;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: 1px solid var(--border-color);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  box-shadow: var(--shadow-md);
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: center;
}
.scroll-bottom-btn:hover {
  color: var(--primary);
  border-color: var(--primary);
}
.fadeup-enter-active,
.fadeup-leave-active {
  transition: opacity 0.2s, transform 0.2s;
}
.fadeup-enter-from,
.fadeup-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
.quota-badge {
  margin-left: 8px;
  padding: 6px 10px;
  background: rgb(245 158 11 / 12%);
  border: 1px solid rgb(245 158 11 / 40%);
  border-radius: 20px;
  color: #d97706;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  vertical-align: middle;
}
.civil-btn {
  background: var(--bg-body);
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  padding: 6px 14px;
  border-radius: 30px;
  font-size: 13px;
  cursor: pointer;
  transition: var(--transition);
}
.civil-btn:hover {
  background: var(--primary-light);
  border-color: var(--primary);
  color: var(--primary);
}
.empty-state {
  text-align: center;
  color: var(--text-muted);
  margin-top: 40vh;
}
/* ---------- 顶部栏收起/展开（小三角骑缝在矩形下缘中部） ---------- */
.header-collapse-zone {
  position: relative;
  z-index: 10;
}
.header-collapse-zone .chat-header {
  max-height: 140px;
  overflow: hidden;
  transition: max-height 0.3s ease, padding 0.3s ease;
}
.header-collapse-zone.collapsed .chat-header {
  max-height: 0;
  padding-top: 0;
  padding-bottom: 0;
  border-bottom-width: 0;
}
/* 小三角：骑在 header 下缘（top:100% 减自身高一半），收起后随矩形贴到屏幕顶部。
   靠右放置：手机顶部中央是前置摄像头/挖孔区，放中间会被遮挡且难点按 */
.header-toggle {
  position: absolute;
  top: calc(100% - 9px);
  right: 16px;
  width: 44px;
  height: 18px;
  border: 1px solid var(--border-color);
  border-top: none;
  border-radius: 0 0 12px 12px;
  background: var(--header-bg, rgba(255, 255, 255, 0.92));
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  z-index: 11;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: color 0.2s;
}
.header-toggle:hover {
  color: var(--primary);
}

/* 人格自绘下拉（全端）：三按钮已移除，统一用下拉；菜单挂 zone 层避开 header 裁剪 */
.mode-dropdown {
  position: absolute;
  top: 100%;
  right: 16px;
  z-index: 60;
}
.mode-dd-trigger .dd-caret {
  font-size: 10px;
  margin-left: 4px;
  opacity: 0.7;
}
.dd-menu {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  min-width: 108px;
  background: var(--header-bg, #fff);
  border: 1px solid var(--border-color);
  border-radius: 14px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  padding: 6px;
  z-index: 60;
}
.dd-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 12px;
  border: none;
  border-radius: 10px;
  background: none;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}
.dd-item:hover { background: var(--bg-body); color: var(--primary); }
.dd-item.current {
  background: var(--primary);
  color: #fff;
}
</style>
