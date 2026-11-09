<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { fetchLocationAddress, fetchNearbyPlaces, fetchPlaces, placeToPoint, type PlaceResult } from './placeSearch'

type SearchCenter = { latitude: number; longitude: number }
type PointChoice = { id: string; label: string; title: string }
const props = withDefaults(defineProps<{ disabled?: boolean; selectionFeedback?: { message: string; error: boolean } | null; targets: PointChoice[]; allowNew?: boolean; assignmentHint?: string }>(), { disabled: false, selectionFeedback: null, allowNew: true, assignmentHint: '选择要替换的主点，或将此地点添加为新的终点。起点和终点会检查候选起降位置。' })
const emit = defineEmits<{
  (event: 'choose-point', selection: { target: string; point: SearchCenter }): void
  (event: 'clear-selection-feedback'): void
}>()
const query = ref('')
const region = ref('')
const center = ref<SearchCenter | null>(null)
const locationAddress = ref('')
const suggestedPlaceName = ref('')
const suggestedNearbyResults = ref<PlaceResult[]>([])
const locating = ref(false)
const resolvingAddress = ref(false)
const results = ref<PlaceResult[]>([])
const pending = ref(false)
const error = ref('')
const message = ref('')
const selectedPlace = ref<PlaceResult | null>(null)
const assignmentDialog = ref<HTMLDialogElement | null>(null)
let controller: AbortController | null = null
let addressController: AbortController | null = null
let version = 0
let locationVersion = 0
let assignmentTrigger: HTMLButtonElement | null = null
let restoreAssignmentFocus = false

function restoreResultFocus() {
  if (!restoreAssignmentFocus || !assignmentTrigger?.isConnected || assignmentTrigger.disabled) return
  assignmentTrigger.focus()
  restoreAssignmentFocus = false
}

watch(() => props.disabled, (disabled) => {
  if (!disabled) void nextTick(restoreResultFocus)
})

function closeAssignmentDialog() {
  if (assignmentDialog.value?.open) assignmentDialog.value.close()
  selectedPlace.value = null
}

function openAssignmentDialog(place: PlaceResult, event: MouseEvent) {
  if (props.disabled) return
  assignmentTrigger = event.currentTarget as HTMLButtonElement
  selectedPlace.value = place
  void nextTick(() => {
    if (selectedPlace.value === place && !assignmentDialog.value?.open) assignmentDialog.value?.showModal()
  })
}

function applySelectedPlace(target: string) {
  if (!selectedPlace.value || props.disabled) return
  try {
    restoreAssignmentFocus = true
    emit('choose-point', { target, point: placeToPoint(selectedPlace.value) })
    closeAssignmentDialog()
    void nextTick(restoreResultFocus)
  } catch {
    restoreAssignmentFocus = false
    closeAssignmentDialog()
    error.value = '该地点坐标无效，请选择其他地点。'
  }
}

function onAssignmentBackdropClick(event: MouseEvent) {
  if (event.target === assignmentDialog.value) closeAssignmentDialog()
}

function resetSearch() {
  closeAssignmentDialog()
  restoreAssignmentFocus = false
  assignmentTrigger = null
  emit('clear-selection-feedback')
  version++
  controller?.abort()
  controller = null
  pending.value = false
  results.value = []
  error.value = ''
  message.value = ''
}
watch([query, region], resetSearch, { flush: 'sync' })
onBeforeUnmount(() => {
  locationVersion++
  addressController?.abort()
  resetSearch()
})

function clearLocation() {
  locationVersion++
  addressController?.abort()
  addressController = null
  center.value = null
  locationAddress.value = ''
  suggestedPlaceName.value = ''
  suggestedNearbyResults.value = []
  locating.value = false
  resolvingAddress.value = false
  resetSearch()
}

function locateForSearch() {
  if (props.disabled || locating.value) return
  clearLocation()
  if (!navigator.geolocation) {
    error.value = '当前浏览器无法定位，请通过 HTTPS 或本机地址打开页面。'
    return
  }
  const requestVersion = locationVersion
  locating.value = true
  try {
    navigator.geolocation.getCurrentPosition(({ coords }) => {
      if (requestVersion !== locationVersion) return
      locating.value = false
      if (props.disabled) return
      if (!Number.isFinite(coords.latitude) || !Number.isFinite(coords.longitude)
        || Math.abs(coords.latitude) > 90 || Math.abs(coords.longitude) > 180) {
        error.value = '浏览器返回的坐标无效，请重新定位。'
        return
      }
      const position = { latitude: coords.latitude, longitude: coords.longitude }
      center.value = position
      resolvingAddress.value = true
      const requestController = new AbortController()
      addressController = requestController
      void fetchLocationAddress(position, requestController.signal).then(({ address, place_name, nearby_results }) => {
        if (requestVersion !== locationVersion) return
        const nearbyName = place_name || nearby_results[0]?.name || ''
        locationAddress.value = address
        suggestedPlaceName.value = nearbyName
        suggestedNearbyResults.value = nearby_results
        if (!query.value.trim() && nearbyName) {
          query.value = nearbyName
          message.value = '已填入附近地名，点击搜索查看周边地点。'
        } else if (!query.value.trim()) {
          message.value = '未找到可搜索的附近地名，请输入地点名称或类别。'
        }
      }).catch(() => {
        if (requestVersion === locationVersion && !requestController.signal.aborted) {
          locationAddress.value = '地址暂时无法获取，仍可按当前位置搜索'
          if (!query.value.trim()) message.value = '无法识别附近地名，请输入地点名称或类别。'
        }
      }).finally(() => {
        if (requestVersion === locationVersion) {
          resolvingAddress.value = false
          addressController = null
        }
      })
      message.value = query.value.trim()
        ? '已定位，点击搜索查看附近地点。'
        : '已定位，正在识别附近地名…'
    }, (cause) => {
      if (requestVersion !== locationVersion) return
      locating.value = false
      error.value = cause.code === 1 ? '浏览器未允许读取位置，请开启定位权限。'
        : cause.code === 2 ? '暂时无法确定位置，请检查设备定位服务。'
          : cause.code === 3 ? '获取位置超时，请重试。' : '获取位置失败，请重试。'
    }, { enableHighAccuracy: true, maximumAge: 0, timeout: 15_000 })
  } catch {
    locating.value = false
    error.value = '当前浏览器无法获取位置，请检查定位权限。'
  }
}

async function search() {
  if (props.disabled || locating.value || !query.value.trim()) return
  resetSearch()
  const requestVersion = version
  const requestController = new AbortController()
  const searchCenter = center.value
  const searchQuery = query.value.trim()
  const includeNearby = !!searchCenter && searchQuery === suggestedPlaceName.value
  controller = requestController
  pending.value = true
  try {
    const places = searchCenter
      ? await fetchNearbyPlaces(searchQuery, searchCenter, requestController.signal)
      : await fetchPlaces(searchQuery, region.value, requestController.signal)
    if (requestVersion !== version) return
    if (includeNearby) {
      const seen = new Set<string>()
      results.value = [...places, ...suggestedNearbyResults.value].filter((place) => {
        const key = place.uid || `${place.name}:${place.location.lat},${place.location.lng}`
        if (seen.has(key)) return false
        seen.add(key)
        return true
      }).slice(0, 20)
    } else results.value = places
    if (!results.value.length) message.value = searchCenter ? '附近没有找到相关地点，请换个关键词。' : '没有找到地点，请补充城市、路名或门牌号。'
  } catch (cause) {
    if (requestVersion !== version || requestController.signal.aborted) return
    if (includeNearby && suggestedNearbyResults.value.length) {
      results.value = suggestedNearbyResults.value
      message.value = '附近检索暂不可用，已显示定位服务返回的周边地点。'
    } else error.value = cause instanceof Error ? cause.message : '地点搜索暂时不可用，请稍后重试。'
  } finally {
    if (requestVersion === version) { pending.value = false; controller = null }
  }
}

function coordinates(place: PlaceResult) {
  const point = placeToPoint(place)
  return `纬度 ${point.latitude.toFixed(6)}，经度 ${point.longitude.toFixed(6)}（WGS84）`
}
</script>

<template>
  <section class="place-search" aria-label="搜索地点" @keydown.esc="resetSearch">
    <form @submit.prevent="search">
      <div class="place-search-heading">
        <label for="route-place-query">搜索地点</label>
        <details v-if="!center" class="place-city"><summary>{{ region || '全国' }}</summary><label>搜索城市<input v-model="region" placeholder="城市名，留空搜索全国" maxlength="50" :disabled="disabled" /></label></details>
      </div>
      <div class="place-search-controls">
        <div class="place-query-field">
          <input id="route-place-query" v-model="query" type="search" :placeholder="center ? '输入附近地点名称或类别' : '输入地点名称或详细地址'" autocomplete="off" maxlength="128" :disabled="disabled" />
          <button type="button" class="place-location-button" :class="{ active: center }" :disabled="disabled || locating" :aria-pressed="!!center" :aria-label="locating ? '正在获取当前位置' : center ? '重新定位搜索中心' : '使用当前位置作为搜索中心'" :title="center ? '重新定位搜索中心' : '使用当前位置作为搜索中心'" @click="locateForSearch"><span class="svg-icon icon-location-on" aria-hidden="true"></span></button>
        </div>
        <button type="submit" :disabled="disabled || pending || locating || !query.trim()">{{ pending ? '搜索中…' : '搜索' }}</button>
      </div>
    </form>
    <small v-if="locating" class="place-location-status" role="status">正在获取当前位置…</small>
    <small v-else-if="center" class="place-location-status" role="status"><span>当前位置：{{ locationAddress || (resolvingAddress ? '正在解析地址…' : '已获取坐标') }}</span><button type="button" :disabled="disabled" @click="clearLocation">取消附近搜索</button></small>
    <small v-if="message" class="place-search-message" role="status">{{ message }}</small>
    <small v-if="error" class="place-search-error" role="alert">{{ error }}</small>
    <small v-if="selectionFeedback" class="place-selection-feedback" :class="{ 'is-error': selectionFeedback.error }" :role="selectionFeedback.error ? 'alert' : 'status'">{{ selectionFeedback.message }}</small>
    <div v-if="results.length" class="place-results">
      <div class="place-results-heading"><strong>{{ center ? '附近结果' : '搜索结果' }}（{{ results.length }}）</strong><button type="button" aria-label="关闭地点搜索结果" @click="resetSearch">×</button></div>
      <ul>
        <li v-for="(place, index) in results" :key="place.uid || index">
          <div class="place-result-copy">
            <strong>{{ place.name }}</strong><span v-if="place.address">{{ place.address }}</span>
            <span v-if="typeof place.distance_m === 'number'">距当前位置约 {{ place.distance_m }} 米</span>
            <span class="place-coordinates">{{ coordinates(place) }}</span>
            <span v-if="place.precision?.precise === 0" class="place-match-note">模糊匹配，请在地图上核对位置。</span>
          </div>
          <button type="button" class="place-result-apply" :disabled="disabled" :aria-label="`选择 ${place.name} 作为${allowNew ? '主点' : '区域端点'}`" :title="`选择 ${place.name} 作为${allowNew ? '主点' : '区域端点'}`" @click="openAssignmentDialog(place, $event)"><span class="svg-icon place-result-action-icon" aria-hidden="true"></span></button>
        </li>
      </ul>
      <small class="place-attribution">地点数据 © 百度地图</small>
    </div>
  </section>
  <Teleport to="body">
    <dialog ref="assignmentDialog" class="place-assignment-dialog" aria-labelledby="place-assignment-title" aria-describedby="place-assignment-description" @click="onAssignmentBackdropClick" @close="selectedPlace = null">
      <div v-if="selectedPlace" class="place-assignment-content">
        <div class="place-assignment-top"><span>应用搜索地点</span><button type="button" class="place-assignment-close" aria-label="关闭点位选择" @click="closeAssignmentDialog">×</button></div>
        <h2 id="place-assignment-title">将此地点设为哪个点位？</h2>
        <p id="place-assignment-description">{{ assignmentHint }}</p>
        <div class="place-assignment-place"><strong>{{ selectedPlace.name }}</strong><span v-if="selectedPlace.address">{{ selectedPlace.address }}</span><small>{{ coordinates(selectedPlace) }}</small></div>
        <p v-if="selectedPlace.precision?.precise === 0" class="place-assignment-warning">该地点为模糊匹配，请在地图上核对位置。</p>
        <div class="place-assignment-actions"><button v-for="(target, index) in targets" :key="target.id" type="button" :autofocus="index === 0" :disabled="disabled" @click="applySelectedPlace(target.id)"><b>{{ target.label }}</b><span>设为{{ target.title }} {{ target.label }}</span></button></div>
        <button v-if="allowNew" type="button" class="place-assignment-new" :disabled="disabled" @click="applySelectedPlace('new')">＋ 添加为新主点</button>
        <button type="button" class="place-assignment-cancel" @click="closeAssignmentDialog">取消</button>
      </div>
    </dialog>
  </Teleport>
</template>

<style scoped>
.place-search { position: relative; margin-top: 8px; text-align: left; }
.place-search-heading { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 5px; font-size: var(--font-caption); }
.place-search-heading > label { color: #424242; font-weight: 600; }
.place-city { position: relative; color: var(--ui-muted); }
.place-city summary { cursor: pointer; }
.place-city label { position: absolute; right: 0; top: 100%; z-index: 22; display: block; width: 220px; padding: 10px; border: 1px solid var(--ui-line); border-radius: var(--ui-radius); background: #fff; box-shadow: 0 4px 16px #172b4d18; }
.place-city input { width: 100%; margin-top: 4px; padding: 7px; border: 1px solid #d1d5db; border-radius: 6px; }
.place-search-controls { display: flex; gap: 6px; }
.place-query-field { position: relative; flex: 1; min-width: 0; }
.place-query-field input { width: 100%; height: 36px; padding: 0 44px 0 10px; border: 1px solid #d1d5db; border-radius: var(--ui-radius); background: #f9fafb; font-size: var(--font-caption); }
.place-query-field input:focus { border-color: var(--ui-accent); background: #fff; }
.place-query-field input::-webkit-search-cancel-button { display: none; }
.place-query-field .place-location-button { position: absolute; top: 2px; right: 2px; display: grid; place-items: center; width: 32px; height: 32px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: var(--ui-muted); }
.place-query-field .place-location-button:hover:not(:disabled), .place-query-field .place-location-button.active { background: #e8f3fc; color: var(--ui-accent); }
.place-query-field .place-location-button:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: -2px; }
.place-location-button .svg-icon { width: 18px; height: 18px; }
.place-search-controls > button[type="submit"] { flex: none; padding: 0 10px; border: 1px solid var(--ui-accent); border-radius: var(--ui-radius); color: #fff; background: var(--ui-accent); font-size: var(--font-caption); }
.place-search button:disabled { opacity: .55; cursor: default; }
.place-search > small { display: block; margin-top: 5px; font-size: 12px; color: var(--ui-muted); line-height: 1.4; }
.place-search > .place-location-status { display: flex; align-items: flex-start; justify-content: space-between; gap: 6px; }
.place-location-status span { min-width: 0; overflow-wrap: anywhere; }
.place-location-status button { flex: none; padding: 0; border: 0; background: transparent; color: var(--ui-accent); font-size: inherit; }
.place-search > .place-search-error { color: #a4262c; }
.place-search > .place-search-message { color: var(--ui-accent); }
.place-search > .place-selection-feedback { color: var(--ui-accent); }
.place-search > .place-selection-feedback.is-error { color: #a4262c; }
.place-results { position: absolute; z-index: 21; top: calc(100% + 6px); left: 0; right: 0; max-height: min(320px, calc(100dvh - 230px)); overflow-y: auto; overscroll-behavior: contain; border: 1px solid #d1d5db; border-radius: var(--ui-radius); background: #fff; box-shadow: 0 6px 20px #172b4d24; }
.place-results-heading { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; justify-content: space-between; padding: 7px 10px; border-bottom: 1px solid var(--ui-line); background: #fff; font-size: var(--font-caption); }
.place-results-heading button { width: 28px; height: 28px; padding: 0; border: 0; background: transparent; color: var(--ui-muted); font-size: 20px; }
.place-results ul { list-style: none; margin: 0; padding: 0; }
.place-results li { display: grid; grid-template-columns: minmax(0, 1fr) 38px; align-items: center; gap: 8px; padding: 10px; border-bottom: 1px solid var(--ui-line); }
.place-result-copy { min-width: 0; }
.place-result-copy > strong { display: block; font-size: var(--font-caption); color: #242424; overflow-wrap: anywhere; }
.place-result-copy > span { display: block; margin-top: 4px; color: var(--ui-muted); font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
.place-coordinates { font-variant-numeric: tabular-nums; }
.place-result-copy > .place-match-note { color: #875100; }
.place-result-apply { display: grid; place-items: center; width: 38px; height: 38px; padding: 0; border: 1px solid #c9def3; border-radius: 8px; background: #f0f7ff; color: #0f6cbd; cursor: pointer; }
.place-result-apply:hover:not(:disabled) { border-color: #0f6cbd; background: #0f6cbd; color: #fff; }
.place-result-apply:focus-visible { outline: 2px solid #0f6cbd; outline-offset: 2px; }
.place-result-action-icon { width: 21px; height: 21px; -webkit-mask-image: url('/svg/expand_circle_right.svg'); mask-image: url('/svg/expand_circle_right.svg'); }
.place-attribution { display: block; padding: 6px 10px; color: var(--ui-muted); font-size: 12px; }
.place-assignment-dialog { width: min(430px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); margin: auto; padding: 0; overflow: auto; border: 1px solid #d6e0ea; border-radius: 14px; background: #fff; color: #243447; box-shadow: 0 24px 70px rgb(14 35 57 / .25); }
.place-assignment-dialog::backdrop { background: rgb(14 30 49 / .3); -webkit-backdrop-filter: blur(9px) saturate(.8); backdrop-filter: blur(9px) saturate(.8); }
.place-assignment-content { padding: 22px; }
.place-assignment-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: #0f6cbd; font-size: 12px; font-weight: 700; }
.place-assignment-close { width: 30px; height: 30px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: #6b7b8d; font-size: 24px; line-height: 1; cursor: pointer; }
.place-assignment-close:hover { background: #f0f4f8; }
.place-assignment-dialog h2 { margin: 12px 0 6px; font-size: 21px; line-height: 1.35; }
.place-assignment-dialog p { margin: 0; color: #617186; font-size: 13px; line-height: 1.55; }
.place-assignment-place { display: grid; gap: 5px; margin-top: 18px; padding: 13px 14px; border: 1px solid #e0e7ef; border-radius: 9px; background: #f8fafc; }
.place-assignment-place strong { font-size: 14px; overflow-wrap: anywhere; }
.place-assignment-place span, .place-assignment-place small { color: #617186; font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
.place-assignment-dialog .place-assignment-warning { margin-top: 10px; color: #875100; }
.place-assignment-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 18px; }
.place-assignment-actions button { display: flex; align-items: center; gap: 9px; min-height: 48px; padding: 8px 11px; border: 1px solid #d3e0ec; border-radius: 9px; background: #fff; color: #243447; font-size: 13px; font-weight: 600; cursor: pointer; }
.place-assignment-actions button:hover:not(:disabled) { border-color: #0f6cbd; background: #f1f7fd; }
.place-assignment-actions button b { display: grid; place-items: center; flex: none; width: 27px; height: 27px; border-radius: 7px; background: #e6f1fb; color: #0f6cbd; }
.place-assignment-actions button:nth-child(2) b { background: #eeeeff; color: #5b5fc7; }
.place-assignment-new { display: block; width: 100%; min-height: 42px; margin-top: 10px; border: 1px dashed #83b5df; border-radius: 9px; background: #f2f8fe; color: #0f6cbd; font-size: 13px; font-weight: 600; cursor: pointer; }
.place-assignment-new:hover:not(:disabled) { background: #e6f1fb; }
.place-assignment-dialog button:focus-visible { outline: 2px solid #0f6cbd; outline-offset: 2px; }
.place-assignment-cancel { display: block; margin: 15px auto 0; padding: 5px 10px; border: 0; border-radius: 6px; background: transparent; color: #617186; font-size: 12px; cursor: pointer; }
.place-assignment-cancel:hover { background: #f0f4f8; }
@media (max-width: 420px) { .place-assignment-actions { grid-template-columns: 1fr; } }
</style>
