<script setup lang="ts">
/** 充值弹窗（旧版 .payment-overlay 三步流：选额→伪码→成功）+ 交易流水入口 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import rechargeQr from '../assets/recharge-qr.png'
import { createRechargeOrder, payOrder } from '../api/payment'
import { useUiStore } from '../stores/ui'

const emit = defineEmits<{ 'refresh-balance': []; 'open-tx': [] }>()
const { t } = useI18n()
const ui = useUiStore()

const AMOUNTS = [10, 50, 100, 500]
const step = ref<1 | 2 | 3>(1)
const amount = ref(100)
const channel = ref('simulated_alipay')
const orderNo = ref('')
const balance = ref<number | null>(null)
const busy = ref(false)
const error = ref('')
const idem = crypto.randomUUID?.() ?? `idem-${Date.now()}`

const CHANNEL_META: Record<string, { icon: string; bg: string }> = {
  simulated_alipay: { icon: '支', bg: '#1677FF' },
  simulated_wxpay: { icon: '微', bg: '#07C160' },
  balance: { icon: '余', bg: '#F59E0B' },
}

async function goPay() {
  busy.value = true
  error.value = ''
  try {
    const order = await createRechargeOrder(amount.value, channel.value, idem)
    orderNo.value = order.order_no
    step.value = 2
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('create_order_failed')
  } finally {
    busy.value = false
  }
}

async function completePayment() {
  busy.value = true
  error.value = ''
  try {
    const result = await payOrder(orderNo.value)
    // 模拟渠道有概率失败：后端 HTTP 200 + status="failed"，必须显式检查（否则误当成功、余额写成 null）
    if (result.status !== 'success') {
      error.value = result.message || t('pay_failed')
      return
    }
    balance.value = result.current_balance
    step.value = 3
    emit('refresh-balance')
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('pay_failed')
  } finally {
    busy.value = false
  }
}

</script>

<template>
  <div class="payment-overlay show">
    <div class="payment-modal">
      <div class="modal-header">
        <h3>{{ t('wallet_recharge') }}</h3>
        <!-- 交易流水入口（旧版迁移遗漏：txOpen 无人置 true，流水弹窗打不开） -->
        <button class="tx-entry-btn" @click="emit('open-tx')">{{ t('transactions') }}</button>
        <button class="close-btn" @click="ui.closeWallet()">✕</button>
      </div>
      <div class="modal-body">
        <template v-if="step === 1">
          <div class="recharge-rules">
            <div class="rule-title">{{ t('recharge_rules_title') }}</div>
            <!-- 这些 locale 串自带 <strong>/<span class="highlight"> 且 app.css 有配套样式，须用 v-html 渲染；文案为本仓静态字面量，非用户输入 -->
            <ul>
              <li v-html="t('rule_2')"></li>
              <li v-html="t('rule_3')"></li>
              <li v-html="t('rule_4')"></li>
              <li v-html="t('rule_6')"></li>
            </ul>
            <div class="demo-note" v-html="t('demo_note')"></div>
          </div>
          <label>{{ t('recharge_amount') }}</label>
          <div class="amount-grid">
            <button
              v-for="a in AMOUNTS"
              :key="a"
              :class="{ selected: amount === a }"
              @click="amount = a"
            >
              {{ a }} {{ t('yuan') }}
            </button>
          </div>
          <label style="margin-top:16px;display:block;">{{ t('recharge_method') }}</label>
          <div style="margin-top:8px;">
            <div
              v-for="(meta, code) in CHANNEL_META"
              :key="code"
              class="channel-option"
              :class="{ selected: channel === code }"
              @click="channel = code"
            >
              <span class="channel-icon">
                <svg width="28" height="28" viewBox="0 0 28 28" fill="none"><rect width="28" height="28" rx="6" :fill="meta.bg"/><text x="14" y="20" text-anchor="middle" font-size="16" font-weight="700" fill="#fff">{{ meta.icon }}</text></svg>
              </span>
              <span class="channel-name">{{ code === 'simulated_alipay' ? t('channel_alipay') : code === 'simulated_wxpay' ? t('channel_wxpay') : t('channel_balance') }}</span>
              <div class="channel-radio"></div>
            </div>
          </div>
        </template>

        <template v-if="step === 2">
          <div class="qr-section">
            <div class="qr-amount">{{ amount.toFixed(2) }} {{ t('yuan') }}</div>
            <div class="qr-order-no">{{ t('order_no_prefix') }}{{ orderNo }}</div>
            <!-- 模拟收款二维码：扫不扫都无所谓，点「关闭」即按现有模拟渠道结算 -->
            <div class="qr-box">
              <img :src="rechargeQr" :alt="t('wallet_recharge')">
            </div>
            <div class="qr-tip">{{ t('qr_tip') }}</div>
            <div v-if="error" style="font-size:13px;color:#ef4444;margin-top:8px;">{{ error }}</div>
            <div style="font-size:12px;color:#999;margin-top:8px;">{{ t('qr_close_hint') }}</div>
          </div>
        </template>

        <template v-if="step === 3">
          <div class="success-section">
            <svg
              class="success-icon"
              width="64"
              height="64"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="10"/>
              <path d="M8 12l3 3 5-6"/>
            </svg>
            <div class="success-title">{{ t('recharge_success') }}</div>
            <div class="success-details">
              <div class="detail-row">
                <span class="detail-label">{{ t('order_no_label') }}</span>
                <span class="detail-value">{{ orderNo }}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{{ t('recharge_amount_label') }}</span>
                <span class="detail-value">{{ amount.toFixed(2) }}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{{ t('current_balance_label') }}</span>
                <span class="detail-value">{{ balance !== null ? `¥${balance.toFixed(2)}` : '—' }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
      <div class="modal-footer">
        <template v-if="step === 1">
          <button class="btn-secondary" @click="ui.closeWallet()">{{ t('cancel') }}</button>
          <button class="btn-primary" :disabled="busy" @click="goPay">{{ t('go_recharge') }}</button>
        </template>
        <template v-if="step === 2">
          <button class="btn-secondary" @click="ui.closeWallet()">{{ t('cancel') }}</button>
          <button class="btn-primary" :disabled="busy" @click="completePayment">{{ t('close_pay') }}</button>
        </template>
        <template v-if="step === 3">
          <button class="btn-primary" @click="ui.closeWallet()">{{ t('done') }}</button>
        </template>
      </div>
    </div>
  </div>
</template>
