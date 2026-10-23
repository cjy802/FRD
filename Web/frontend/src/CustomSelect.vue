<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

type SelectOption = { value: string | number; label: string }

const props = withDefaults(defineProps<{
  modelValue: string | number
  options: SelectOption[]
  disabled?: boolean
  ariaLabel?: string
  openUp?: boolean
}>(), { disabled: false, openUp: false })

const emit = defineEmits<{ 'update:modelValue': [value: string | number] }>()

const root = ref<HTMLElement | null>(null)
const open = ref(false)
const activeIndex = ref(0)

const current = computed(() => props.options.find(option => option.value === props.modelValue))

function toggle() {
  if (props.disabled) return
  open.value = !open.value
  if (open.value) activeIndex.value = Math.max(0, props.options.findIndex(option => option.value === props.modelValue))
}

function choose(option: SelectOption) {
  emit('update:modelValue', option.value)
  open.value = false
}

function onDocumentPointerDown(event: PointerEvent) {
  if (open.value && root.value && !root.value.contains(event.target as Node)) open.value = false
}

function onKeydown(event: KeyboardEvent) {
  if (props.disabled) return
  if (event.key === 'Escape') { open.value = false; return }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    if (!open.value) open.value = true
    const delta = event.key === 'ArrowDown' ? 1 : -1
    activeIndex.value = (activeIndex.value + delta + props.options.length) % props.options.length
  } else if ((event.key === 'Enter' || event.key === ' ') && open.value) {
    event.preventDefault()
    const option = props.options[activeIndex.value]
    if (option) choose(option)
  }
}

onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown, true))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown, true))
</script>

<template>
  <div ref="root" class="custom-select" :class="{ open, disabled, 'open-up': openUp }" @keydown="onKeydown">
    <button type="button" class="custom-select-trigger" :disabled="disabled" :aria-label="ariaLabel" :aria-expanded="open" aria-haspopup="listbox" @click="toggle">
      <span class="custom-select-value">{{ current?.label ?? '—' }}</span>
      <svg class="custom-select-chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 6 4 4 4-4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" /></svg>
    </button>
    <ul v-if="open" class="custom-select-options" role="listbox" :aria-label="ariaLabel">
      <li v-for="(option, index) in options" :key="option.value" class="custom-select-option" :class="{ selected: option.value === modelValue, active: index === activeIndex }" role="option" :aria-selected="option.value === modelValue" @pointerenter="activeIndex = index" @click.stop.prevent="choose(option)">{{ option.label }}</li>
    </ul>
  </div>
</template>

<style scoped>
.custom-select { position: relative; display: block; width: 100%; min-width: 0; font-size: inherit; }
.custom-select-trigger { display: flex; width: 100%; min-width: 0; min-height: 34px; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 10px; border: 1px solid #cbd5df; border-radius: 7px; background: #fff; color: #344455; font: inherit; font-size: inherit; line-height: 1.35; text-align: left; cursor: pointer; transition: border-color .15s ease-out, background-color .15s ease-out; }
.custom-select-trigger:hover:not(:disabled) { transform: none; box-shadow: none; border-color: #9fc5e4; }
.custom-select-trigger:focus-visible { border-color: var(--ui-accent); outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.custom-select-trigger:disabled { background-color: #f3f6f9; color: #748092; cursor: not-allowed; }
.custom-select.open .custom-select-trigger { border-color: var(--ui-accent); }
.custom-select-value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.custom-select-chevron { flex: none; width: 13px; height: 13px; color: #6b7280; transition: transform .18s ease; }
.custom-select.open .custom-select-chevron { transform: rotate(180deg); }
.custom-select-options { position: absolute; top: calc(100% + 4px); left: 0; z-index: 40; min-width: 100%; max-height: 240px; margin: 0; padding: 4px; overflow-y: auto; border: 1px solid #cbd5df; border-radius: 8px; background: #fff; box-shadow: 0 8px 22px rgb(15 23 42 / .18); list-style: none; scrollbar-width: none; -ms-overflow-style: none; }
.custom-select.open-up .custom-select-options { top: auto; bottom: calc(100% + 4px); }
.custom-select-options::-webkit-scrollbar { display: none; width: 0; height: 0; }
.custom-select-option { padding: 7px 9px; border-radius: 6px; color: #344455; font-size: inherit; line-height: 1.35; white-space: nowrap; cursor: pointer; }
.custom-select-option.active { background: #f2f8fd; color: #1769aa; }
.custom-select-option.selected { background: #1265a7; color: #fff; }
.custom-select-option.selected.active { background: #0f5aa4; }
</style>
