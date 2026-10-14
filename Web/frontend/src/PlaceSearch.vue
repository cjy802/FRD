<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { fetchLocationAddress, fetchNearbyPlaces, fetchPlaces, placeToPoint, type PlaceResult } from './placeSearch'

const props = defineProps<{ disabled?: boolean }>()
type SearchCenter = { latitude: number; longitude: number }
const query = ref('')
const region = ref('常州市')
const center = ref<SearchCenter | null>(null)
const locationAddress = ref('')
const locating = ref(false)
const resolvingAddress = ref(false)
const results = ref<PlaceResult[]>([])
const pending = ref(false)
const error = ref('')
const message = ref('')
let controller: AbortController | null = null
let addressController: AbortController | null = null
let version = 0
let locationVersion = 0

function resetSearch() {
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
      void fetchLocationAddress(position, requestController.signal).then((address) => {
        if (requestVersion === locationVersion) locationAddress.value = address
      }).catch(() => {
        if (requestVersion === locationVersion && !requestController.signal.aborted) {
          locationAddress.value = '地址暂时无法获取，仍可按当前位置搜索'
        }
      }).finally(() => {
        if (requestVersion === locationVersion) {
          resolvingAddress.value = false
          addressController = null
        }
      })
      if (query.value.trim()) void search()
      else message.value = '已定位，请输入地点名称或类别后搜索附近地点。'
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
  controller = requestController
  pending.value = true
  try {
    const places = searchCenter
      ? await fetchNearbyPlaces(query.value, searchCenter, requestController.signal)
      : await fetchPlaces(query.value, region.value, requestController.signal)
    if (requestVersion !== version) return
    results.value = places
    if (!places.length) message.value = searchCenter ? '附近没有找到相关地点，请换个关键词。' : '没有找到地点，请补充城市、路名或门牌号。'
  } catch (cause) {
    if (requestVersion !== version || requestController.signal.aborted) return
    error.value = cause instanceof Error ? cause.message : '地点搜索暂时不可用，请稍后重试。'
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
          <button type="button" class="place-location-button" :class="{ active: center }" :disabled="disabled || locating" :aria-pressed="!!center" :aria-label="locating ? '正在获取当前位置' : center ? '重新定位附近搜索' : '使用当前位置搜索附近地点'" :title="center ? '重新定位附近搜索' : '使用当前位置搜索附近地点'" @click="locateForSearch"><span class="svg-icon icon-location-on" aria-hidden="true"></span></button>
        </div>
        <button type="submit" :disabled="disabled || pending || locating || !query.trim()">{{ pending ? '搜索中…' : '搜索' }}</button>
      </div>
    </form>
    <small v-if="locating" class="place-location-status" role="status">正在获取当前位置…</small>
    <small v-else-if="center" class="place-location-status" role="status"><span>当前位置：{{ locationAddress || (resolvingAddress ? '正在解析地址…' : '已获取坐标') }} · 附近 1 公里</span><button type="button" :disabled="disabled" @click="clearLocation">取消附近搜索</button></small>
    <small v-if="!error && !message && !center && !locating">搜索地点后查看经纬度。</small>
    <small v-if="message" class="place-search-message" role="status">{{ message }}</small>
    <small v-if="error" class="place-search-error" role="alert">{{ error }}</small>
    <div v-if="results.length" class="place-results">
      <div class="place-results-heading"><strong>{{ center ? '附近结果' : '搜索结果' }}（{{ results.length }}）</strong><button type="button" aria-label="关闭地点搜索结果" @click="resetSearch">×</button></div>
      <ul>
        <li v-for="(place, index) in results" :key="place.uid || index">
          <strong>{{ place.name }}</strong><span v-if="place.address">{{ place.address }}</span>
          <span v-if="typeof place.distance_m === 'number'">距当前位置约 {{ place.distance_m }} 米</span>
          <span class="place-coordinates">{{ coordinates(place) }}</span>
          <span v-if="place.precision?.precise === 0" class="place-match-note">模糊匹配，请在地图上核对位置。</span>
        </li>
      </ul>
      <small class="place-attribution">地点数据 © 百度地图</small>
    </div>
  </section>
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
.place-search > small { display: block; margin-top: 5px; font-size: 11px; color: var(--ui-muted); line-height: 1.4; }
.place-search > .place-location-status { display: flex; align-items: flex-start; justify-content: space-between; gap: 6px; }
.place-location-status span { min-width: 0; overflow-wrap: anywhere; }
.place-location-status button { flex: none; padding: 0; border: 0; background: transparent; color: var(--ui-accent); font-size: inherit; }
.place-search > .place-search-error { color: #a4262c; }
.place-search > .place-search-message { color: var(--ui-accent); }
.place-results { position: absolute; z-index: 21; top: calc(100% + 6px); left: 0; right: 0; max-height: min(320px, calc(100dvh - 230px)); overflow-y: auto; overscroll-behavior: contain; border: 1px solid #d1d5db; border-radius: var(--ui-radius); background: #fff; box-shadow: 0 6px 20px #172b4d24; }
.place-results-heading { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; justify-content: space-between; padding: 7px 10px; border-bottom: 1px solid var(--ui-line); background: #fff; font-size: var(--font-caption); }
.place-results-heading button { width: 28px; height: 28px; padding: 0; border: 0; background: transparent; color: var(--ui-muted); font-size: 20px; }
.place-results ul { list-style: none; margin: 0; padding: 0; }
.place-results li { padding: 10px; border-bottom: 1px solid var(--ui-line); }
.place-results li > strong { display: block; font-size: var(--font-caption); color: #242424; }
.place-results li > span { display: block; margin-top: 4px; color: var(--ui-muted); font-size: 11px; line-height: 1.4; overflow-wrap: anywhere; }
.place-coordinates { font-variant-numeric: tabular-nums; }
.place-results li > .place-match-note { color: #875100; }
.place-attribution { display: block; padding: 6px 10px; color: var(--ui-muted); font-size: 10px; }
</style>
