<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { fetchSession, homeForRole, type Session } from './auth'

// 落地页公开可见：顺手查一下会话，只用来决定按钮文案和去向，不做强制跳转。
const session = ref<Session | null>(null)
const sessionChecked = ref(false)

onMounted(() => {
  document.title = '低空飞行决策工作台'
  void fetchSession().then(current => {
    session.value = current
    sessionChecked.value = true
  })
})

const primaryHref = computed(() => (session.value ? homeForRole(session.value.role) : '/workspace'))
const primaryLabel = computed(() => (session.value?.role === 'field' ? '进入现场端' : '进入工作台'))

const pillHref = computed(() => (session.value ? homeForRole(session.value.role) : '/login'))
const pillLabel = computed(() => {
  if (!session.value) return sessionChecked.value ? '登录' : '进入'
  return `${session.value.username} · ${session.value.role === 'field' ? '现场端' : '工作台'}`
})
</script>

<template>
  <div class="landing">
    <section class="hero">
      <img class="hero-bg" src="/hero-sky.jpg" alt="蓝天云层间巡航的巡查无人机" />
      <div class="hero-tint" aria-hidden="true"></div>

      <header class="hero-bar">
        <span class="brand">低空飞行决策工作台</span>
        <a class="pill" :href="pillHref">
          {{ pillLabel }}
          <span class="arrow" aria-hidden="true">→</span>
        </a>
      </header>

      <div class="hero-body">
        <h1>航线，当场画好。<br />天气，提前看清。</h1>
        <p>面向道路巡查的低空飞行助手：点到点航线规划、起飞前天气评估、航后照片 AI 分析——从画线到起飞，一站完成。</p>
        <div class="hero-actions">
          <a class="btn solid" :href="primaryHref">{{ primaryLabel }}</a>
          <a class="btn ghost" href="/field">现场端入口</a>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.landing {
  --hero-accent: #ddf25b;
  min-height: 100dvh;
  background: #f1eee3;
  font-family: 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', system-ui, sans-serif;
}

.hero {
  position: relative;
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
  border-radius: 0 0 30px 30px;
  overflow: hidden;
  isolation: isolate;
}

.hero-bg {
  position: absolute;
  inset: 0;
  z-index: -2;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.hero-tint {
  position: absolute;
  inset: 0;
  z-index: -1;
  background: linear-gradient(180deg, rgba(10, 28, 58, 0.32) 0%, rgba(10, 24, 50, 0.48) 52%, rgba(6, 14, 32, 0.74) 100%);
}

.hero-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 26px clamp(20px, 4vw, 44px);
  animation: hero-fade 0.9s ease-out both;
}

.brand {
  color: var(--hero-accent);
  font-size: clamp(17px, 2vw, 21px);
  font-weight: 700;
  letter-spacing: 0.02em;
  text-shadow: 0 1px 14px rgba(4, 12, 28, 0.45);
}

.pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 10px 22px;
  border: 1px solid rgba(255, 255, 255, 0.55);
  border-radius: 999px;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  text-decoration: none;
  backdrop-filter: blur(4px);
  transition: background 0.2s ease, border-color 0.2s ease;
}

.pill:hover {
  background: rgba(255, 255, 255, 0.14);
  border-color: rgba(255, 255, 255, 0.85);
}

.arrow {
  font-weight: 400;
}

.hero-body {
  flex: 1;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 26px;
  padding: 16px clamp(18px, 5vw, 48px) 88px;
  text-align: center;
}

.hero-body h1 {
  margin: 0;
  color: var(--hero-accent);
  font-size: clamp(40px, 8vw, 94px);
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: 0.015em;
  text-shadow: 0 3px 30px rgba(4, 12, 28, 0.5);
  animation: hero-rise 0.7s ease-out both;
}

.hero-body p {
  margin: 0;
  max-width: 36em;
  color: rgba(255, 255, 255, 0.94);
  font-size: clamp(15px, 1.5vw, 17.5px);
  line-height: 1.85;
  text-shadow: 0 1px 14px rgba(4, 12, 28, 0.55);
  animation: hero-rise 0.7s 0.12s ease-out both;
}

.hero-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 14px;
  margin-top: 8px;
  animation: hero-rise 0.7s 0.22s ease-out both;
}

.btn {
  display: inline-flex;
  align-items: center;
  padding: 15px 32px;
  border-radius: 999px;
  font-size: 16px;
  font-weight: 600;
  text-decoration: none;
  transition: background 0.2s ease, border-color 0.2s ease, transform 0.2s ease;
}

.btn.solid {
  background: var(--hero-accent);
  color: #101f36;
}

.btn.solid:hover {
  background: #ebfa7f;
  transform: translateY(-1px);
}

.btn.ghost {
  border: 1px solid rgba(255, 255, 255, 0.6);
  color: #fff;
}

.btn.ghost:hover {
  background: rgba(255, 255, 255, 0.14);
  border-color: rgba(255, 255, 255, 0.9);
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
  .hero-body h1,
  .hero-body p,
  .hero-actions {
    animation: none;
  }

  .btn:hover {
    transform: none;
  }
}

@media (max-width: 560px) {
  .hero-bar {
    padding-top: 20px;
  }

  .brand {
    font-size: 16px;
  }

  .pill {
    padding: 8px 16px;
    font-size: 13px;
  }

  .hero-body {
    padding-bottom: 64px;
  }
}
</style>
