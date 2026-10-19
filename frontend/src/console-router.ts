/** 独立控制台路由与登录守卫。 */
import { createRouter, createWebHashHistory } from 'vue-router'
import { hasAuthSession, setUnauthorizedHandler } from './api/http'

function safeRedirect(value: unknown): string | null {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) {
    return null
  }
  return value
}

const consoleRouter = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', component: () => import('./views/RagOpsChatView.vue') },
    { path: '/admin/rag/chat', component: () => import('./views/RagOpsChatView.vue') },
    { path: '/admin/rag', component: () => import('./views/RagConsoleView.vue') },
    { path: '/dbagent', redirect: '/admin/rag/chat' },
    { path: '/login', component: () => import('./views/ConsoleLoginView.vue') },
  ],
})

consoleRouter.beforeEach((to) => {
  const redirect = safeRedirect(to.query.redirect)
  if (to.path !== '/login' && !hasAuthSession()) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.path === '/login' && hasAuthSession()) {
    return { path: redirect ?? '/' }
  }
})

setUnauthorizedHandler(() => {
  consoleRouter.push({ path: '/login', query: { error: 'unauthorized' } })
})

export default consoleRouter
