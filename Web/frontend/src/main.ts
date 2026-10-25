import { createApp } from 'vue'
import { setTheme } from '@fluentui/web-components'
import { webLightTheme } from '@fluentui/tokens'
import '@fluentui/web-components/button.js'
import App from './App.vue'
import FieldClient from './FieldClient.vue'
import Landing from './Landing.vue'
import './style.css'

setTheme(webLightTheme)

// 会话过期统一处理：业务请求收到 401 时整页回首页登录卡（/api/auth/* 除外）。
const rawFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const response = await rawFetch(input, init)
  if (response.status === 401) {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url
    if (url.startsWith('/api/') && !url.startsWith('/api/auth/')) window.location.replace('/')
  }
  return response
}

// '/' 为公开落地页兼登录入口，主控工作台在 /workspace。
// 独立登录页已下线：/login 旧地址一律回首页，由守卫带去的 401 也落在首页登录卡。
const pathname = window.location.pathname
if (pathname === '/login') {
  window.location.replace('/')
} else {
  const root = pathname === '/field' ? FieldClient : pathname === '/workspace' ? App : Landing
  createApp(root).mount('#app')
}
