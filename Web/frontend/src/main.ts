import { createApp } from 'vue'
import { setTheme } from '@fluentui/web-components'
import { webLightTheme } from '@fluentui/tokens'
import '@fluentui/web-components/button.js'
import App from './App.vue'
import FieldClient from './FieldClient.vue'
import Login from './Login.vue'
import './style.css'

setTheme(webLightTheme)

// 会话过期统一处理：业务请求收到 401 时整页回登录页（/api/auth/* 除外）。
const rawFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const response = await rawFetch(input, init)
  if (response.status === 401) {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url
    if (url.startsWith('/api/') && !url.startsWith('/api/auth/')) window.location.replace('/login')
  }
  return response
}

const root = window.location.pathname === '/field'
  ? FieldClient
  : window.location.pathname === '/login' ? Login : App
createApp(root).mount('#app')
