<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { fetchSession, homeForRole, login, type Session } from './auth'

const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)
const checking = ref(true)

onMounted(async () => {
  // 已登录的用户直接进对应端，别在登录页来回打转。
  const session = await fetchSession()
  if (session) window.location.replace(homeForRole(session.role))
  else checking.value = false
  document.title = '登录 · 低空飞行决策工作台'
})

async function submit() {
  if (busy.value) return
  error.value = ''
  if (!username.value.trim() || !password.value) {
    error.value = '请输入用户名和密码。'
    return
  }
  busy.value = true
  try {
    const session: Session = await login(username.value.trim(), password.value)
    window.location.replace(homeForRole(session.role))
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '登录失败，请重试。'
    busy.value = false
  }
}
</script>

<template>
  <div class="login-shell">
    <main class="login-card">
      <div class="login-brand">
        <span class="login-brand-mark" aria-hidden="true">低</span>
        <div>
          <strong>低空飞行决策工作台</strong>
          <small>积水巡查 · 登录</small>
        </div>
      </div>

      <form class="login-form" @submit.prevent="submit">
        <label>
          用户名
          <input v-model="username" type="text" name="username" autocomplete="username" spellcheck="false" :disabled="checking || busy" />
        </label>
        <label>
          密码
          <input v-model="password" type="password" name="password" autocomplete="current-password" :disabled="checking || busy" />
        </label>
        <p v-if="error" class="login-error" role="alert">{{ error }}</p>
        <button type="submit" class="login-submit" :disabled="checking || busy">{{ busy ? '正在登录' : '登录' }}</button>
      </form>

      <footer class="login-roles">
        <div>
          <b>管理员</b>
          <span>进入主控工作台：航线规划、照片分析、人工确认并下发积水点。</span>
        </div>
        <div>
          <b>使用者</b>
          <span>进入现场处理端：查看下发的积水位置、现场照片和地图导航。</span>
        </div>
      </footer>
    </main>
  </div>
</template>

<style scoped>
.login-shell { min-height: 100dvh; display: grid; place-items: center; padding: 24px; background: #f3f5f7; color: #111827; }
.login-card { display: grid; gap: 22px; width: min(400px, 100%); padding: 32px 28px 26px; border: 1px solid var(--ui-line); border-radius: var(--ui-radius); background: #fff; box-shadow: 0 2px 8px #172b4d0a; }
.login-brand { display: flex; align-items: center; gap: 12px; }
.login-brand-mark { display: grid; place-items: center; width: 40px; height: 40px; border-radius: 10px; background: #e8f3fc; color: var(--ui-accent); font-size: 18px; font-weight: 700; }
.login-brand strong { display: block; font-size: var(--font-heading); font-weight: 600; letter-spacing: -.02em; }
.login-brand small { display: block; margin-top: 1px; color: var(--ui-muted); font-size: var(--font-caption); }
.login-form { display: grid; gap: 14px; }
.login-form label { display: grid; gap: 6px; color: #344455; font-size: var(--font-caption); font-weight: 600; }
.login-form input { min-height: 40px; padding: 0 12px; border: 1px solid #cbd5df; border-radius: 7px; background: #fff; color: #111827; font: inherit; }
.login-form input:focus-visible { border-color: var(--ui-accent); outline: 2px solid var(--ui-accent); outline-offset: 1px; }
.login-form input:disabled { background: #f3f6f9; color: #748092; }
.login-error { margin: 0; padding: 8px 10px; border: 1px solid #e7b6bb; border-radius: 7px; background: #fff5f5; color: #a4262c; font-size: var(--font-caption); line-height: 1.5; }
.login-submit { min-height: 42px; border: 1px solid var(--ui-accent); border-radius: 7px; background: var(--ui-accent); color: #fff; font-size: var(--font-body); font-weight: 600; }
.login-submit:hover:not(:disabled) { border-color: var(--ui-accent-hover); background: var(--ui-accent-hover); }
.login-submit:disabled { cursor: wait; opacity: .6; }
.login-roles { display: grid; gap: 10px; padding-top: 18px; border-top: 1px solid var(--ui-line); }
.login-roles div { display: grid; gap: 3px; }
.login-roles b { color: #1e3a5f; font-size: var(--font-caption); font-weight: 700; }
.login-roles span { color: var(--ui-muted); font-size: var(--font-caption); line-height: 1.5; }
</style>
