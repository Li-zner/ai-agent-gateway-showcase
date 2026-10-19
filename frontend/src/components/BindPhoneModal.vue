<script setup lang="ts">
/** 绑定手机号弹窗：验证码校验 → 解锁额度（后端可能改绑用户名并签发新 token 对）
 *  Turnstile（A-P1-4）：后端启用时渲染人机挂件，发码必须携带令牌；未启用零感知 */
import { onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { bindPhone, getTurnstileConfig, sendPhoneCode } from '../api/phone'
import { saveTokens } from '../api/http'
import { useAuthStore } from '../stores/auth'

const props = defineProps<{ tip: string }>()
const emit = defineEmits<{ close: []; bound: [] }>()
const { t } = useI18n()
const auth = useAuthStore()

const phone = ref('')
const code = ref('')
const error = ref('')
const errorOk = ref(false)
const countdown = ref(0)
const busy = ref(false)
const sending = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

// ---- Turnstile 挂件（未启用时 tsBox 恒空、token 恒空串，发码不带校验字段）----
const tsBox = ref<HTMLDivElement | null>(null)
const tsWidgetId = ref<string | null>(null)
let tsToken = ''
declare global { interface Window { turnstile?: { render: (el: HTMLElement, o: Record<string, unknown>) => string; reset: (id?: string) => void; remove: (id: string) => void } } }

function loadTurnstileScript(): Promise<void> {
  if (window.turnstile) return Promise.resolve()
  if (document.querySelector('script#cf-turnstile')) {
    return new Promise((resolve) => { const check = setInterval(() => { if (window.turnstile) { clearInterval(check); resolve() } }, 100) })
  }
  return new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.id = 'cf-turnstile'
    s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
    s.async = true
    s.onload = () => resolve()
    s.onerror = () => reject(new Error('turnstile script load failed'))
    document.head.appendChild(s)
  })
}

onMounted(async () => {
  try {
    const cfg = await getTurnstileConfig()
    if (!cfg.enabled || !cfg.site_key) return
    await loadTurnstileScript()
    if (!tsBox.value || !window.turnstile) return
    tsWidgetId.value = window.turnstile.render(tsBox.value, {
      sitekey: cfg.site_key,
      callback: (token: string) => { tsToken = token },
      'expired-callback': () => { tsToken = '' },
      'error-callback': () => { tsToken = '' },
    })
  } catch {
    // 配置端点/脚本失败：不阻塞弹窗——发码请求会被后端 403 并提示，属启用态异常
  }
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (tsWidgetId.value !== null && window.turnstile) {
    try { window.turnstile.remove(tsWidgetId.value) } catch { /* 卸载竞态可忽略 */ }
  }
})

async function sendCode() {
  if (countdown.value > 0 || sending.value) return
  error.value = ''
  if (tsWidgetId.value !== null && !tsToken) {
    errorOk.value = false
    error.value = t('turnstile_pending')
    return
  }
  sending.value = true
  try {
    await sendPhoneCode(phone.value, tsToken)
    errorOk.value = true
    error.value = t('code_sent')
    countdown.value = 60
    timer = setInterval(() => {
      countdown.value -= 1
      if (countdown.value <= 0 && timer) clearInterval(timer)
    }, 1000)
  } catch (e) {
    errorOk.value = false
    error.value = e instanceof Error ? e.message : t('send_failed_2')
  } finally {
    sending.value = false
    // 令牌一次性：用完即重置，下次发码重新完成人机验证
    if (tsWidgetId.value !== null && window.turnstile) { tsToken = ''; window.turnstile.reset(tsWidgetId.value) }
  }
}

async function submit() {
  if (!phone.value || !code.value) {
    error.value = t('fill_phone_code')
    return
  }
  busy.value = true
  try {
    const result = await bindPhone(phone.value, code.value)
    saveTokens(result)
    // 绑定已经成功即为成功；profile 刷新失败只影响本地资料缓存，不能把
    // 一次已提交成功的绑定反转成“操作失败”。
    void auth.loadProfile().catch(() => {})
    emit('close')
    emit('bound')
  } catch (e) {
    errorOk.value = false
    error.value = e instanceof Error ? e.message : t('operation_failed')
  } finally {
    busy.value = false
  }
}

// 弹窗关闭（v-if 卸载）时清掉倒计时 interval
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="payment-overlay show">
    <div class="payment-modal" style="max-width:400px;">
      <div class="modal-header">
        <h3>{{ t('bind_phone_title') }}</h3>
      </div>
      <div class="modal-body">
        <div style="font-size:13px;color:var(--text-secondary);margin-bottom:16px;line-height:1.6;">{{ props.tip || t('bind_phone_trial_tip') }}</div>
        <div v-if="tsWidgetId !== null" ref="tsBox" style="margin-bottom:12px;"></div>
        <input v-model="phone" type="tel" inputmode="numeric" maxlength="11" :placeholder="t('phone_input_ph')">
        <div style="display:flex;gap:6px;margin-bottom:14px;">
          <input v-model="code" type="text" inputmode="numeric" maxlength="6" :placeholder="t('code_ph')">
          <button :disabled="countdown > 0 || sending" style="width:110px;flex-shrink:0;padding:12px 0;background:var(--primary);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;" @click="sendCode">
            {{ countdown > 0 ? `${countdown}s` : t('bind_phone_send_code') }}
          </button>
        </div>
        <div v-if="error" :style="{ fontSize: '13px', marginBottom: '10px', color: errorOk ? '#22c55e' : '#ef4444' }">{{ error }}</div>
        <div style="display:flex;gap:8px;">
          <button :disabled="busy" style="flex:1;padding:12px;background:var(--primary);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;" @click="submit">{{ t('bind_phone_btn') }}</button>
          <button style="flex:1;padding:12px;background:var(--bg-body);color:var(--text-secondary);border:1.5px solid var(--border-color);border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;" @click="emit('close')">{{ t('skip_bind') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
