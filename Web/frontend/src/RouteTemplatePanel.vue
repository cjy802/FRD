<script setup lang="ts">
import { ref, watch } from 'vue'
import type { RouteTemplateSummary } from './routeTemplates'

type TemplateCoordinates = { latitude: number; longitude: number }
type TemplateWithRouteSummary = RouteTemplateSummary & {
  anchor_count?: number
  start?: TemplateCoordinates | null
  end?: TemplateCoordinates | null
}

const props = defineProps<{
  templates: RouteTemplateSummary[]
  activeId: string | null
  dirty: boolean
  busy: boolean
  canSave: boolean
  error: string
  status: string
}>()

const emit = defineEmits<{
  (event: 'saveNew', name: string): void
  (event: 'saveCurrent'): void
  (event: 'load', id: string): void
  (event: 'rename', id: string, name: string): void
  (event: 'duplicate', id: string): void
  (event: 'remove', id: string): void
  (event: 'refresh'): void
}>()

const newName = ref('')
const editingId = ref<string | null>(null)
const renameName = ref('')
const confirmDeleteId = ref<string | null>(null)

function saveAsNew() {
  const name = newName.value.trim()
  if (!name || props.busy || !props.canSave) return
  emit('saveNew', name)
}

function beginRename(template: RouteTemplateSummary) {
  editingId.value = template.id
  renameName.value = template.name
  confirmDeleteId.value = null
}

function submitRename(template: RouteTemplateSummary) {
  const name = renameName.value.trim()
  if (!name || props.busy) return
  if (name === template.name) {
    editingId.value = null
    return
  }
  emit('rename', template.id, name)
}

function removeTemplate(id: string) {
  if (props.busy) return
  confirmDeleteId.value = null
  emit('remove', id)
}

watch(() => props.templates, (templates) => {
  if (editingId.value) {
    const edited = templates.find((template) => template.id === editingId.value)
    if (!edited || edited.name === renameName.value.trim()) editingId.value = null
  }
  if (confirmDeleteId.value && !templates.some((template) => template.id === confirmDeleteId.value)) {
    confirmDeleteId.value = null
  }
})

watch(() => props.activeId, (id, previousId) => {
  if (id && id !== previousId) newName.value = ''
})

function routeSummary(template: RouteTemplateSummary): string {
  const route = template as TemplateWithRouteSummary
  const pieces: string[] = []
  if (Number.isInteger(route.anchor_count) && (route.anchor_count ?? 0) >= 2) {
    pieces.push(`${route.anchor_count} 个主点`)
  }
  if (route.start && route.end &&
    Number.isFinite(route.start.latitude) && Number.isFinite(route.start.longitude) &&
    Number.isFinite(route.end.latitude) && Number.isFinite(route.end.longitude)) {
    pieces.push(`起 ${coordinateText(route.start)} → 终 ${coordinateText(route.end)}`)
  }
  return pieces.join(' · ')
}

function coordinateText(point: TemplateCoordinates): string {
  return `${point.latitude.toFixed(4)}, ${point.longitude.toFixed(4)}`
}

function updatedAt(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}
</script>

<template>
  <details class="route-template-panel">
    <summary>
      <span class="template-heading"><span class="svg-icon icon-route" aria-hidden="true"></span>路径模板</span>
      <span class="template-count">{{ templates.length }}</span>
    </summary>
    <div class="template-panel-body">
      <div class="template-toolbar">
        <small>模板保存在本机服务，刷新页面后仍可载入</small>
        <button type="button" :disabled="busy" aria-label="刷新路径模板列表" @click="emit('refresh')">刷新</button>
      </div>

      <p v-if="error" class="template-feedback is-error" role="alert">{{ error }}</p>
      <p v-else-if="status" class="template-feedback" role="status">{{ status }}</p>

      <div v-if="activeId" class="template-current">
        <span>当前模板<span v-if="dirty" class="template-dirty"> · 有未保存修改</span></span>
        <button type="button" :disabled="busy || !canSave || !dirty" @click="emit('saveCurrent')">保存修改</button>
      </div>

      <div class="template-new">
        <label for="route-template-new-name">另存为新模板</label>
        <div class="template-new-controls">
          <input id="route-template-new-name" v-model="newName" type="text" maxlength="80" autocomplete="off" placeholder="输入模板名称" :disabled="busy || !canSave" @keydown.enter.prevent="saveAsNew" />
          <button type="button" :disabled="busy || !canSave || !newName.trim()" @click="saveAsNew">保存</button>
        </div>
        <small v-if="!canSave">先计算一条路线，再保存模板。</small>
      </div>

      <ul v-if="templates.length" class="template-list" aria-label="已保存的路径模板">
        <li v-for="template in templates" :key="template.id" :class="{ 'is-active': activeId === template.id }">
          <div class="template-name-row">
            <strong :title="template.name">{{ template.name }}</strong>
            <span v-if="activeId === template.id" class="template-active-tag">当前</span>
          </div>
          <small v-if="routeSummary(template)" class="template-route-summary">{{ routeSummary(template) }}</small>
          <small class="template-date">更新于 {{ updatedAt(template.updated_at) }}</small>

          <div v-if="editingId === template.id" class="template-rename">
            <label :for="`route-template-rename-${template.id}`">新名称</label>
            <input :id="`route-template-rename-${template.id}`" v-model="renameName" type="text" maxlength="80" autocomplete="off" :disabled="busy" @keydown.enter.prevent="submitRename(template)" @keydown.esc.stop="editingId = null" />
            <div class="template-actions">
              <button type="button" :disabled="busy || !renameName.trim()" @click="submitRename(template)">确定</button>
              <button type="button" :disabled="busy" @click="editingId = null">取消</button>
            </div>
          </div>
          <div v-else-if="confirmDeleteId === template.id" class="template-confirm">
            <span>删除“{{ template.name }}”？</span>
            <div class="template-actions">
              <button type="button" :disabled="busy" @click="confirmDeleteId = null">取消</button>
              <button type="button" class="is-danger" :disabled="busy" :aria-label="`确认删除模板 ${template.name}`" @click="removeTemplate(template.id)">确认删除</button>
            </div>
          </div>
          <div v-else class="template-actions">
            <button type="button" :disabled="busy" :aria-label="`载入模板 ${template.name}`" @click="emit('load', template.id)">载入</button>
            <button type="button" :disabled="busy" :aria-label="`重命名模板 ${template.name}`" @click="beginRename(template)">改名</button>
            <button type="button" :disabled="busy" :aria-label="`复制模板 ${template.name}`" @click="emit('duplicate', template.id)">复制</button>
            <button type="button" :disabled="busy" :aria-label="`删除模板 ${template.name}`" @click="confirmDeleteId = template.id; editingId = null">删除</button>
          </div>
        </li>
      </ul>
      <p v-else class="template-empty">暂无模板。</p>
    </div>
  </details>
</template>

<style scoped>
.route-template-panel { min-width: 0; margin: 12px 0; border: 1px solid var(--ui-line); border-radius: var(--ui-radius); background: #fff; font-size: var(--font-caption); }
.route-template-panel > summary { display: flex; align-items: center; justify-content: space-between; gap: 6px; min-height: 38px; padding: 7px 10px; cursor: pointer; font-weight: 600; list-style-position: inside; }
.route-template-panel > summary::marker { color: var(--ui-muted); }
.template-heading { display: inline-flex; align-items: center; gap: 6px; }
.template-heading .svg-icon { color: var(--ui-accent); }
.template-count { display: inline-grid; place-items: center; min-width: 22px; height: 22px; padding: 0 5px; border-radius: 11px; background: #e8f3fc; color: var(--ui-accent); font-size: 11px; }
.template-panel-body { min-width: 0; padding: 0 10px 10px; border-top: 1px solid var(--ui-line); }
.template-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 8px 0; }
.template-toolbar small, .template-new small { color: var(--ui-muted); line-height: 1.4; }
.template-panel-body button { min-height: 28px; padding: 4px 7px; border: 1px solid #d7dfe8; border-radius: 6px; background: #fff; color: #34506c; white-space: nowrap; font-size: 11px; }
.template-panel-body button:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); background: #eff6ff; }
.template-panel-body button:disabled { opacity: .5; cursor: not-allowed; }
.template-feedback { margin: 0 0 8px; padding: 6px 8px; border-radius: 6px; background: #eff6ff; color: #1d4ed8; line-height: 1.4; overflow-wrap: anywhere; }
.template-feedback.is-error { background: #fef2f2; color: #a4262c; }
.template-current { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 7px 0; border-top: 1px solid var(--ui-line); }
.template-current > span { min-width: 0; color: #334155; overflow-wrap: anywhere; }
.template-dirty { color: #a85714; }
.template-new { padding: 8px 0 10px; border-top: 1px solid var(--ui-line); }
.template-new > label, .template-rename > label { display: block; margin-bottom: 4px; color: #424242; font-weight: 600; }
.template-new-controls { display: flex; gap: 5px; }
.template-new input, .template-rename input { width: 100%; min-width: 0; height: 30px; padding: 4px 7px; border: 1px solid #d1d5db; border-radius: 6px; background: #f9fafb; font-size: var(--font-caption); }
.template-new-controls input { flex: 1; }
.template-new-controls button { flex: none; border-color: var(--ui-accent); background: var(--ui-accent); color: #fff; }
.template-new-controls button:hover:not(:disabled) { background: var(--ui-accent-hover); color: #fff; }
.template-new > small { display: block; margin-top: 5px; }
.template-list { margin: 0; padding: 0; list-style: none; }
.template-list li { min-width: 0; margin-top: 7px; padding: 8px; border: 1px solid var(--ui-line); border-radius: 7px; background: #fbfcfe; }
.template-list li.is-active { border-color: #bfdbfe; background: #f3f8ff; }
.template-name-row { display: flex; align-items: baseline; justify-content: space-between; gap: 4px; }
.template-name-row strong { min-width: 0; overflow-wrap: anywhere; font-size: var(--font-caption); }
.template-active-tag { flex: none; color: var(--ui-accent); font-size: 11px; }
.template-route-summary, .template-date { display: block; margin-top: 3px; color: var(--ui-muted); line-height: 1.4; overflow-wrap: anywhere; }
.template-date { font-size: 11px; }
.template-actions { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; }
.template-actions .is-danger { border-color: #f3b8b8; color: #a4262c; }
.template-actions .is-danger:hover:not(:disabled) { border-color: #a4262c; background: #fef2f2; color: #a4262c; }
.template-rename, .template-confirm { margin-top: 8px; }
.template-confirm > span { color: #a4262c; overflow-wrap: anywhere; }
.template-empty { margin: 3px 0 0; color: var(--ui-muted); }
</style>
