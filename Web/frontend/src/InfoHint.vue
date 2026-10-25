<template>
  <span
    ref="triggerElement"
    class="info-hint"
    tabindex="0"
    role="note"
    :aria-label="text"
    @click.stop.prevent
    @pointerenter="enter"
    @pointerleave="leave"
    @focus="enter"
    @blur="leave"
    @keydown.escape.prevent="leave"
  ><span class="svg-icon icon-alert info-hint-icon" aria-hidden="true"></span></span>
  <Teleport to="body">
    <Transition name="info-hint-fade">
      <span v-if="visible" ref="bubbleElement" class="info-hint-bubble" :style="bubbleStyle" role="tooltip">{{ text }}</span>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref } from 'vue'

defineProps<{ text: string }>()

const triggerElement = ref<HTMLElement | null>(null)
const bubbleElement = ref<HTMLElement | null>(null)
const visible = ref(false)
const bubbleStyle = ref<{ left: string; top: string }>({ left: '-9999px', top: '-9999px' })
let showTimer: number | undefined

function positionBubble() {
  const trigger = triggerElement.value
  const bubble = bubbleElement.value
  if (!trigger || !bubble) return
  const rect = trigger.getBoundingClientRect()
  const width = bubble.offsetWidth
  const height = bubble.offsetHeight
  let left = rect.right + 8
  if (left + width > window.innerWidth - 8) left = rect.left - width - 8
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8))
  let top = rect.top + rect.height / 2 - height / 2
  top = Math.max(8, Math.min(top, window.innerHeight - height - 8))
  bubbleStyle.value = { left: `${Math.round(left)}px`, top: `${Math.round(top)}px` }
}

function enter() {
  if (visible.value || showTimer !== undefined) return
  showTimer = window.setTimeout(() => {
    showTimer = undefined
    visible.value = true
    nextTick(positionBubble)
    window.addEventListener('scroll', leave, true)
    window.addEventListener('resize', leave)
  }, 140)
}

function leave() {
  window.clearTimeout(showTimer)
  showTimer = undefined
  if (!visible.value) return
  visible.value = false
  window.removeEventListener('scroll', leave, true)
  window.removeEventListener('resize', leave)
}

onBeforeUnmount(leave)
</script>

<style scoped>
.info-hint { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; margin-left: 2px; flex: none; border-radius: 50%; color: #8a97a8; cursor: help; outline-offset: 2px; }
.info-hint:hover, .info-hint:focus-visible { color: #4b5563; }
.info-hint-icon { width: 14px; height: 14px; }
.info-hint-bubble { position: fixed; z-index: 14000; width: max-content; max-width: 280px; padding: 8px 11px; border-radius: 8px; background: #253445; color: #fff; font-size: 12px; line-height: 1.55; text-align: left; box-shadow: 0 6px 20px rgb(15 23 42 / .28); pointer-events: none; }
.info-hint-fade-enter-active, .info-hint-fade-leave-active { transition: opacity .12s ease; }
.info-hint-fade-enter-from, .info-hint-fade-leave-to { opacity: 0; }
</style>
