<script setup lang="ts">
/** 控制台专用管理员登录，不加载聊天、手机号或 OAuth 功能。 */
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

function target(): string {
  const value = route.query.redirect
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//')
    ? value
    : '/'
}

async function submit() {
  if (loading.value) return
  error.value = ''
  loading.value = true
  try {
    await auth.login(username.value, password.value)
    if (auth.user?.role !== 'admin' && !(auth.user?.permissions ?? []).includes('dbagent.view')) {
      await auth.logout()
      error.value = '该账号没有中控台访问能力，请联系管理员开通'
      return
    }
    if (target() === '/') {
      // 分流：admin 进数据台；子账号（仅 dbagent 能力）直达 Agent 对话台
      await router.replace(auth.user?.role === 'admin' ? '/admin/rag' : '/admin/rag/chat')
    } else {
      await router.replace(target())
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="console-login">
    <section class="login-panel">
      <h1>RAG 控制台</h1>
      <p>独立监测、诊断与修复控制台</p>
      <form @submit.prevent="submit">
        <input v-model="username" autocomplete="username" placeholder="管理员账号">
        <input v-model="password" type="password" autocomplete="current-password" placeholder="密码">
        <button type="submit" :disabled="loading">{{ loading ? '登录中' : '登录' }}</button>
      </form>
      <div v-if="error" class="error">{{ error }}</div>
    </section>
  </main>
</template>

<style scoped>
.console-login {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 20px;
  background: var(--bg-body, #eef1ec);
}
.login-panel {
  width: min(390px, 100%);
  padding: 32px;
  border: 1px solid var(--border-color, #dfe5de);
  border-radius: 8px;
  background: var(--bg-card, #fff);
  box-shadow: var(--shadow-lg, 0 12px 40px rgb(36 46 39 / 10%));
}
h1 { margin: 0; font-size: 25px; }
p { margin: 7px 0 24px; color: var(--text-muted, #5f6e66); font-size: 13px; }
form { display: grid; gap: 11px; }
input {
  padding: 11px 12px;
  border: 1px solid var(--border-color, #dfe5de);
  border-radius: 6px;
  background: var(--input-bg, #fff);
  color: inherit;
  font: inherit;
}
button {
  margin-top: 4px;
  padding: 11px;
  border: 0;
  border-radius: 6px;
  background: var(--primary, #3d7a5c);
  color: #fff;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
button:disabled { opacity: 0.6; cursor: wait; }
.error { margin-top: 12px; color: #b42318; font-size: 13px; }
</style>
