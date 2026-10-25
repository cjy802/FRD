<script setup lang="ts">
import { ref } from 'vue'
import { homeForRole, login } from './auth'

// '/' 现在就是登录入口：落地页 + 登录卡二合一。
// 已登录用户访问时不做自动跳转，保留落地页本身；重新提交登录即回到对应端。
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  if (busy.value) return
  error.value = ''
  if (!username.value.trim() || !password.value) {
    error.value = '请输入用户名和密码。'
    return
  }
  busy.value = true
  try {
    const session = await login(username.value.trim(), password.value)
    window.location.replace(homeForRole(session.role))
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '登录失败，请重试。'
    busy.value = false
  }
}
</script>

<template>
  <div class="landing" :class="{ 'is-busy': busy }">
    <section class="hero">
      <img class="hero-bg" src="/hero-sky.jpg" alt="蓝天白云间巡航的巡查无人机" />
      <div class="hero-tint" aria-hidden="true"></div>

      <header class="hero-bar">
        <span class="brand">积水风险智能识别与预警系统</span>
      </header>

      <div class="hero-main">
        <div class="hero-copy">
          <h1><span class="line">航线，当场画好。</span><span class="line">天气，提前看清。</span></h1>
          <p>面向道路巡查的低空飞行助手：点到点航线规划、起飞前天气评估、航后照片 AI 分析——从画线到起飞，一站完成。</p>
        </div>

        <main class="login-card">
          <h2>欢迎登录</h2>
          <form class="login-form" @submit.prevent="submit">
            <label>
              用户名
              <input v-model="username" type="text" name="username" autocomplete="username" spellcheck="false" :disabled="busy" />
            </label>
            <label>
              密码
              <input v-model="password" type="password" name="password" autocomplete="current-password" :disabled="busy" />
            </label>
            <!-- 占位入口：功能还没定，先不接任何跳转 -->
            <div class="login-links">
              <button type="button" :disabled="busy">忘记密码</button>
              <button type="button" :disabled="busy">新建用户</button>
            </div>
            <p v-if="error" class="login-error" role="alert">{{ error }}</p>
            <button type="submit" class="login-submit" :disabled="busy">{{ busy ? '正在登录' : '登录' }}</button>
          </form>
        </main>
      </div>
    </section>
  </div>
</template>

<style scoped>
.landing {
  /* 全局 style.css 把 html/body/#app 锁成 height:100% + overflow:hidden，
     落地页必须自己做滚动容器，矮视口（手机横屏等）才能滚到登录卡。 */
  height: 100dvh;
  overflow-y: auto;
  /* 底色取天空蓝：万一有亚像素缝隙露底，看起来仍是画面的一部分 */
  background: #2160b2;
  font-family: 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', system-ui, sans-serif;
}

.hero {
  position: relative;
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  isolation: isolate;
  /* 同一张照片铺底，图片层有亚像素接缝时露出的也是照片，不出深色缝 */
  background: url('/hero-sky.jpg') center / cover no-repeat;
}

.hero-bg {
  position: absolute;
  inset: 0;
  z-index: -2;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 左侧压暗保文字、右侧留给白云和登录卡，底部再压一层防止画面发飘 */
.hero-tint {
  position: absolute;
  inset: 0;
  z-index: -1;
  background:
    linear-gradient(180deg, rgba(6, 20, 44, 0.22) 0%, rgba(6, 20, 44, 0) 30%, rgba(4, 12, 28, 0.4) 100%),
    linear-gradient(96deg, rgba(6, 20, 44, 0.6) 0%, rgba(6, 20, 44, 0.36) 42%, rgba(6, 20, 44, 0.08) 70%, rgba(6, 20, 44, 0.26) 100%);
}

.hero-bar {
  padding: 26px clamp(20px, 4vw, 48px);
  animation: hero-fade 0.9s ease-out both;
}

.brand {
  color: #fff;
  font-size: clamp(17px, 2vw, 21px);
  font-weight: 700;
  letter-spacing: 0.02em;
  text-shadow: 0 1px 14px rgba(4, 12, 28, 0.5);
}

.hero-main {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  align-items: center;
  gap: clamp(32px, 5vw, 72px);
  padding: 12px clamp(24px, 6vw, 104px) 72px;
}

.hero-copy {
  max-width: 720px;
  animation: hero-rise 0.7s ease-out both;
}

.hero-copy h1 {
  margin: 0;
  color: #fff;
  /* 字号跟列宽走，保证每行 8 个字放得下；两行各自锁死不折行 */
  font-size: clamp(38px, 4.8vw, 84px);
  font-weight: 700;
  line-height: 1.18;
  letter-spacing: 0.015em;
  text-shadow: 0 2px 24px rgba(4, 14, 34, 0.55);
}

.hero-copy h1 .line {
  display: block;
  white-space: nowrap;
}

.hero-copy p {
  margin: 22px 0 0;
  max-width: 30em;
  color: rgba(255, 255, 255, 0.94);
  font-size: clamp(15px, 1.4vw, 17px);
  line-height: 1.9;
  text-shadow: 0 1px 12px rgba(4, 12, 28, 0.55);
}

/* 登录卡：白卡落在右半栏的居中位置，不贴屏幕右边 */
.login-card {
  width: min(400px, 100%);
  justify-self: center;
  padding: 30px 28px 28px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 24px 60px rgba(4, 14, 34, 0.35);
  animation: hero-rise 0.7s 0.12s ease-out both;
}

.login-card h2 {
  margin: 0 0 20px;
  color: #1e3a5f;
  font-size: 20px;
  font-weight: 700;
  text-align: center;
  letter-spacing: 0.04em;
}

.login-form {
  display: grid;
  gap: 14px;
}

.login-form label {
  display: grid;
  gap: 6px;
  color: #344455;
  font-size: var(--font-caption);
  font-weight: 600;
}

.login-form input {
  min-height: 40px;
  padding: 0 12px;
  border: 1px solid #cbd5df;
  border-radius: 7px;
  background: #fff;
  color: #111827;
  font: inherit;
}

.login-form input:focus-visible {
  border-color: var(--ui-accent);
  outline: 2px solid var(--ui-accent);
  outline-offset: 1px;
}

.login-form input:disabled {
  background: #f3f6f9;
  color: #748092;
}

.login-links {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: -4px;
}

.login-links button {
  padding: 2px 0;
  border: 0;
  background: none;
  color: var(--ui-accent);
  font-size: var(--font-caption);
  cursor: pointer;
}

.login-links button:hover {
  text-decoration: underline;
}

.login-error {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid #e7b6bb;
  border-radius: 7px;
  background: #fff5f5;
  color: #a4262c;
  font-size: var(--font-caption);
  line-height: 1.5;
}

.login-submit {
  min-height: 42px;
  margin-top: 4px;
  border: 1px solid var(--ui-accent);
  border-radius: 7px;
  background: var(--ui-accent);
  color: #fff;
  font-size: var(--font-body);
  font-weight: 600;
}

.login-submit:hover:not(:disabled) {
  border-color: var(--ui-accent-hover);
  background: var(--ui-accent-hover);
}

.login-submit:disabled {
  cursor: wait;
  opacity: 0.6;
}

@keyframes hero-rise {
  from {
    opacity: 0;
    transform: translateY(16px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

@keyframes hero-fade {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .hero-bar,
  .hero-copy,
  .login-card {
    animation: none;
  }
}

@media (max-width: 960px) {
  .hero-main {
    grid-template-columns: 1fr;
    justify-items: center;
    gap: 36px;
    padding: 24px 20px 56px;
    text-align: center;
  }

  /* 单列后可用宽度变大，字号重新按整行宽算 */
  .hero-copy h1 {
    font-size: clamp(34px, 7.5vw, 72px);
  }

  .hero-copy p {
    margin-inline: auto;
  }

  .login-card {
    text-align: left;
  }
}
</style>
