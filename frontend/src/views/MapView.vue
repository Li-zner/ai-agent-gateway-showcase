<script setup lang="ts">
/**
 * 地图页：省级中国地图 → 省份下钻 → 城市信息气泡（天气 + LLM 推荐）
 * 使用旧版 map.html DOM + map.css 作用域样式。
 */
import * as echarts from 'echarts/core'
import { ScatterChart } from 'echarts/charts'
import { GeoComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ApiError } from '../api/http'
import { useChatStore } from '../stores/chat'
import { useAuthStore } from '../stores/auth'
import { getRecommendations, getWeather } from '../api/map'
import { useUserLocation } from '../composables/useUserLocation'
import {
  buildCityGeoUrl, computeCenters, displayName, findAdcode,
  initialZoomFor, PROVINCE_GEO_URL,
} from '../utils/geo'
import type { GeoJson } from '../types'
import '../styles/map.css'
import { StorageKey } from '../enums'
import { CITY_LIST as mapCityList, loadStringList as loadList, MUNI as mapMuni, saveStringList as saveList } from '../utils/mapData'
// 按需注册：页面只用 geo + scatter + canvas（全量引入产物 1.1MB，按需后大幅减小）
echarts.use([ScatterChart, GeoComponent, CanvasRenderer])
const { t } = useI18n()
const mapOwner = computed(() => auth.user?.username ?? '')
const router = useRouter()
const chat = useChatStore()
const auth = useAuthStore()
const { userLocation, locate, locateByIp, clearLocation } = useUserLocation(() => mapOwner.value)
const mapEl = ref<HTMLDivElement | null>(null)
const title = ref('')
const bubbleVisible = ref(false)
const bubbleCity = ref('')
const loadError = ref('')
const weather = ref('')
const foods = ref<string[]>([])
const spots = ref<string[]>([])
const favorites = ref<string[]>([])
const history = ref<string[]>([])
const historyOpen = ref(false)
const zoomPercent = ref(100)
/** 手机端缩放基准：≤768px 视口小，全国视野放大 1.7 倍作为 100% 基准；50%–500% 硬限
 *  由 geo.scaleLimit 原生执行（丝滑连续），事件处理只同步读数不再 setOption 干预手势 */
const zoomFactor = ref(1)
function updateZoomFactor(): void {
  zoomFactor.value = window.innerWidth <= 768 ? 1.7 : 1
}
let lastGeoZoom = 1
/** 点击保护：刷新定位 10s / 随机城市 5s，防连点造成定位与地图重绘卡顿 */
const locateCd = ref(0)
const randomCd = ref(0)
/** 定位结果提示：true=刷新成功 / false=失败；2s 后清除（渐隐由 CSS 动画承担） */
const locateToast = ref<boolean | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null
let cdTimer: ReturnType<typeof setInterval> | null = null
function startCooldown(seconds: number, apply: (n: number) => void): void {
  apply(seconds)
  if (cdTimer) return
  cdTimer = setInterval(() => {
    locateCd.value = Math.max(0, locateCd.value - 1)
    randomCd.value = Math.max(0, randomCd.value - 1)
    if (locateCd.value === 0 && randomCd.value === 0 && cdTimer) {
      clearInterval(cdTimer)
      cdTimer = null
    }
  }, 1000)
}
type EchartsInstance = ReturnType<typeof echarts.init>
let chart: EchartsInstance | null = null
let provinceGeo: GeoJson | null = null
let currentCity: string | null = null
let currentLevel: 'province' | 'city' = 'province'
/** 城市气泡请求代号：快速切城市时丢弃过期响应（旧响应慢到会覆盖新城市数据） */
let infoSeq = 0

async function showCityInfo(city: string, adcode?: string | number | null) {
  if (!city) return
  const seq = ++infoSeq
  currentCity = city
  bubbleVisible.value = true
  bubbleCity.value = city
  weather.value = t('map_loading')
  foods.value = []
  spots.value = []
  // 先到先显：天气（亚秒级）不被推荐（LLM，秒级~十几秒）拖住，各自就绪各自上屏；
  // seq 守卫防止期间切换城市的过期数据回写
  getWeather(city, adcode ?? undefined).then((w) => {
    if (seq !== infoSeq) return
    weather.value = w
  }).catch(() => {
    if (seq === infoSeq) weather.value = t('map_no_data')
  })
  getRecommendations(city).then((r) => {
    if (seq !== infoSeq) return
    foods.value = r.foods
    spots.value = r.spots
  }).catch(() => {
    if (seq !== infoSeq) return
    foods.value = [t('map_no_recommend')]
    spots.value = [t('map_no_recommend')]
  })
  history.value = [city, ...history.value.filter((c) => c !== city)].slice(0, 30)
  saveList(StorageKey.MapHistory, history.value, mapOwner.value)
}

async function refreshRecommendations() {
  const city = currentCity
  if (!city) return
  const seq = infoSeq
  const [recRes] = await Promise.allSettled([getRecommendations(city, true)])
  if (seq !== infoSeq || city !== currentCity) return
  if (recRes.status === 'fulfilled') {
    foods.value = recRes.value.foods
    spots.value = recRes.value.spots
  } else {
    // 2026-09-12 清欠（D-F11）：刷新失败不再静默，给出可见提示
    loadError.value = t('map_load_failed')
  }
}

function closeBubble(): void {
  bubbleVisible.value = false
  currentCity = null
}

function isFavorite(city: string): boolean {
  return favorites.value.includes(city)
}

function removeFavorite(c: string): void {
  favorites.value = favorites.value.filter((x) => x !== c)
  saveList(StorageKey.MapFavorites, favorites.value, mapOwner.value)
}

function removeFromHistory(c: string): void {
  history.value = history.value.filter((x) => x !== c)
  saveList(StorageKey.MapHistory, history.value, mapOwner.value)
}

function toggleFavorite(): void {
  if (!currentCity) return
  favorites.value = isFavorite(currentCity)
    ? favorites.value.filter((c) => c !== currentCity)
    : [...favorites.value, currentCity]
  saveList(StorageKey.MapFavorites, favorites.value, mapOwner.value)
}

function computeProvinceCenter(geo: GeoJson): [number, number] | undefined {
  const centroids = geo.features
    .map((f) => f.properties.centroid ?? f.properties.center)
    .filter((c): c is [number, number] => Array.isArray(c) && c.length === 2)
  if (!centroids.length) return undefined
  const sumLat = centroids.reduce((s, c) => s + c[1], 0)
  const sumLng = centroids.reduce((s, c) => s + c[0], 0)
  return [sumLng / centroids.length, sumLat / centroids.length]
}

function renderGeo(geo: GeoJson, mapName: string, initZoom: number, center?: [number, number]): void {
  if (!chart) return
  echarts.registerMap(mapName, geo as Parameters<typeof echarts.registerMap>[1])
  zoomPercent.value = Math.round(initZoom * 100)
  lastGeoZoom = initZoom * zoomFactor.value
  chart.setOption({
    geo: {
      map: mapName, roam: true, zoom: initZoom * zoomFactor.value, center: center ?? undefined,
      // 50%–500% 硬边界交给 echarts 原生手势内执行：连续丝滑、松手不回弹
      scaleLimit: { min: 0.5 * zoomFactor.value, max: 5 * zoomFactor.value },
      label: { show: false },
      itemStyle: { borderColor: '#b0c8b0', borderWidth: 1 },
      emphasis: { label: { show: false } },
    },
    series: [
      {
        type: 'scatter', coordinateSystem: 'geo',
        data: computeCenters(geo.features), symbolSize: 0,
        label: { show: true, color: '#2d4a2d', fontSize: 11, formatter: '{b}' },
        tooltip: { show: false }, silent: true, z: 2,
      },
      // 港澳点击热区（仅省视图）：领土太小移动端难点中，标记落近海一侧扩一点
      // 有效范围——不压深圳/珠海，也不会点不到；透明符号只做点击承接
      ...(mapName === 'china' ? [{
        type: 'scatter', coordinateSystem: 'geo', silent: false, z: 3,
        symbolSize: 34, itemStyle: { color: 'transparent' },
        label: { show: false }, tooltip: { show: false }, emphasis: { disabled: true },
        data: [
          { name: '香港', value: [114.26, 22.08] },
          { name: '澳门', value: [113.55, 22.03] },
        ],
      }] : []),
    ],
  })
  chart.off('click')
  chart.on('click', (params) => {
    const name = params.name
    if (!name) { closeBubble(); return }
    // 两类有效点击：geo 区域本体 + 港澳近海热区标记（scatter）
    const geoClick = params.componentType === 'geo'
    const markerClick = params.seriesType === 'scatter' && (name === '香港' || name === '澳门')
    if (!geoClick && !markerClick) { closeBubble(); return }
    if (currentLevel === 'province') {
      const MUNI = ['北京', '天津', '上海', '重庆', '香港', '澳门']
      if (MUNI.some((kw) => params.name!.startsWith(kw))) {
        showCityInfo(displayName(params.name!), findAdcode(geo, params.name!))
        return
      }
      const adcode = findAdcode(geo, params.name!)
      if (adcode) drillToCity(adcode, params.name!)
    } else {
      showCityInfo(displayName(params.name!), findAdcode(geo, params.name!))
    }
  })
  // 滚轮/捏合漫游：echarts 原生连续缩放（scaleLimit 兜底硬边界），这里只同步读数，
  // 不再 setOption 干预手势——逐事件 setOption 与手势内部状态互殴正是"松手回弹"的根源
  chart.off('georoam')
  chart.on('georoam', () => {
    const opt = chart?.getOption() as { geo?: { zoom?: number }[] } | undefined
    const z = opt?.geo?.[0]?.zoom
    if (!z) return
    lastGeoZoom = z
    zoomPercent.value = Math.round((z / zoomFactor.value) * 100)
  })
}

async function loadProvinceMap(): Promise<void> {
  currentLevel = 'province'
  title.value = t('map_title_country')
  try {
    const resp = await fetch(PROVINCE_GEO_URL, { signal: AbortSignal.timeout(10000) })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const geo = (await resp.json()) as GeoJson
    provinceGeo = geo
    await nextTick()
    if (!chart && mapEl.value) chart = echarts.init(mapEl.value)
    renderGeo(geo, 'china', 1)
    loadError.value = ''
  } catch {
    // 2026-09-10 审查 P2：失败不再静默白图，给出可见提示与重试入口
    loadError.value = t('map_load_failed')
  }
}

async function retryLoadMap(): Promise<void> {
  loadError.value = ''
  await loadProvinceMap()
}

async function drillToCity(
  provinceAdcode: string | number, provinceName: string | null, targetCity?: string,
): Promise<void> {
  closeBubble()
  try {
    const resp = await fetch(buildCityGeoUrl(provinceAdcode), { signal: AbortSignal.timeout(8000) })
    if (!resp.ok) throw new Error('城市数据不存在')
    const geo = (await resp.json()) as GeoJson
    currentLevel = 'city'
    const name = provinceName || geo.features[0]?.properties.name || `省${provinceAdcode}`
    title.value = name
    await nextTick()
    if (!chart && mapEl.value) chart = echarts.init(mapEl.value)
    renderGeo(geo, name, initialZoomFor(geo.features.length), computeProvinceCenter(geo))
    if (targetCity) {
      // 2026-09-10 审查 P2：300ms 延迟弹泡与用户中途操作（返回省级/手动关闭/再下钻）
      // 存在竞态，回调前校验仍处本次下钻的上下文才弹出
      const expectedTitle = name
      setTimeout(() => {
        if (currentLevel === 'city' && title.value === expectedTitle && !bubbleVisible.value) {
          showCityInfo(targetCity)
        }
      }, 300)
    }
  } catch {
    if (provinceName) showCityInfo(displayName(provinceName))
  }
}

function updateUserMarker(): void {
  if (!chart || !userLocation.value) return
  const loc = userLocation.value
  let coords: [number, number] | null = loc.lng && loc.lat ? [loc.lng, loc.lat] : null
  if (!coords && provinceGeo && loc.province) {
    const f = provinceGeo.features.find((f) =>
      f.properties.name.includes(loc.province.replace(/[省市自治区特别行政区]+$/g, '')))
    const c = f?.properties.centroid ?? f?.properties.center
    if (c) coords = c
  }
  if (!coords) return
  const existing = (chart.getOption() as { series?: object[] }).series ?? []
  chart.setOption({
    series: [
      ...existing.filter((s) => (s as { name?: string }).name !== 'userMarker'),
      {
        type: 'scatter', coordinateSystem: 'geo', name: 'userMarker',
        data: [{ name: loc.city, value: coords }],
        symbolSize: 28, itemStyle: { color: '#2196F3' },
        label: { show: true, formatter: `${loc.city}·${t('map_you')}`, position: 'right', fontSize: 11 },
        z: 10,
      },
    ],
  })
}

async function goToCity(): Promise<void> {
  const city = currentCity
  if (!city) return
  // 地图属旅游功能：先切回旅游人格，确保自动提问在旅游上下文生成
  if (chat.currentPersonaId !== 'unified' && !chat.switchPersona('unified')) {
    // 2026-09-12 清欠（D-F12）：流式期间切人格被拒时不再静默，autoPrompt 不落错人格
    loadError.value = t('map_switch_busy')
  }
  // 进页面时 onMounted 已 locate 过一次并写入 userLocation；这里只即时读内存值。
  // 不再 await 定位链——GPS 最长 8s + regeo/IP 网络往返会把跳页整段堵住（GO 卡顿根因）。
  // 取不到就 from=''，travel_without_from 已覆盖无出发地的情况。
  const loc = userLocation.value
  const from = loc?.city ?? ''
  const prompt = from && from !== city
    ? t('travel_with_from', { from, to: city })
    : t('travel_without_from', { to: city })
  chat.setAutoPrompt(prompt)
  router.push('/chat')
}

const CITY_LIST = mapCityList
const MUNI = mapMuni

async function refreshLocation(): Promise<void> {
  if (locateCd.value > 0) return
  startCooldown(10, (n) => { locateCd.value = n })
  clearLocation()
  // locate 内部三级兜底（geolocation → regeo → IP），失败返回 null；结果用 toast 明示
  const loc = await locate(false).catch(() => null)
  updateUserMarker()
  if (toastTimer) clearTimeout(toastTimer)
  locateToast.value = loc !== null
  toastTimer = setTimeout(() => { locateToast.value = null }, 2000)
}

function randomCity(): void {
  if (randomCd.value > 0) return
  startCooldown(5, (n) => { randomCd.value = n })
  const pick = CITY_LIST[Math.floor(Math.random() * CITY_LIST.length)]
  if (MUNI.includes(pick.adcode)) { showCityInfo(pick.city, pick.adcode); return }
  drillToCity(pick.adcode, null, pick.city)
}

function setZoom(percent: number): void {
  if (!chart) return
  const v = Math.max(50, Math.min(500, percent))
  lastGeoZoom = (v / 100) * zoomFactor.value
  chart.setOption({ geo: { zoom: lastGeoZoom } })
  zoomPercent.value = v
}

function resize(): void {
  const prevFactor = zoomFactor.value
  updateZoomFactor()
  chart?.resize()
  // 视口跨 768px 后基准变了：scaleLimit 硬边界同步跟随，且按倍率重缩放实际 zoom
  // （2026-09-10 审查 P2：原先只换边界不重缩放，比例读数与实际视图脱节）
  if (zoomFactor.value !== prevFactor && lastGeoZoom > 0) {
    lastGeoZoom = lastGeoZoom * (zoomFactor.value / prevFactor)
    chart?.setOption({ geo: { zoom: lastGeoZoom } })
    zoomPercent.value = Math.round((lastGeoZoom / zoomFactor.value) * 100)
  }
  chart?.setOption({ geo: { scaleLimit: { min: 0.5 * zoomFactor.value, max: 5 * zoomFactor.value } } })
}

onMounted(async () => {
  try {
    await auth.ensureProfile()
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) router.push('/login')
    else loadError.value = t('map_load_failed')
    return
  }
  favorites.value = loadList(StorageKey.MapFavorites, mapOwner.value)
  history.value = loadList(StorageKey.MapHistory, mapOwner.value)
  updateZoomFactor()
  window.addEventListener('resize', resize)
  await loadProvinceMap()
  const loc = await locate(false)
  if (!loc) await locateByIp().then(() => updateUserMarker())
  else updateUserMarker()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  if (cdTimer) clearInterval(cdTimer)
  if (toastTimer) clearTimeout(toastTimer)
  chart?.dispose(); chart = null
})
</script>

<template>
  <div class="map-page">
    <div ref="mapEl" class="map-container"></div>

    <div class="toolbar">
      <div class="left-tools">
        <button class="back-btn" @click="router.push('/chat')">{{ t('nav_chat') }}</button>
        <button
          v-if="currentLevel === 'city'"
          class="back-btn"
          @click="loadProvinceMap"
        >{{ t('map_back') }}</button>
      </div>
      <div class="toolbar-title">{{ title || t('map_title_country') }}</div>
      <!-- 地图数据加载失败提示（2026-09-10 审查 P2：失败不再静默白图） -->
      <span v-if="loadError" class="map-load-error">
        {{ loadError }}
        <button @click="retryLoadMap">{{ t('retry') }}</button>
      </span>
      <div class="right-tools">
        <button
          class="history-btn"
          :disabled="randomCd > 0"
          @click="randomCity"
        >{{ randomCd > 0 ? `${t('map_random')} ${randomCd}s` : t('map_random') }}</button>
        <button
          class="history-btn"
          :disabled="locateCd > 0"
          @click="refreshLocation"
        >{{ locateCd > 0 ? `${t('map_refresh_loc')} ${locateCd}s` : t('map_refresh_loc') }}</button>
        <button class="history-btn" @click="historyOpen = !historyOpen">{{ t('map_history_label') }}</button>
      </div>
    </div>

    <!-- 缩放控件 -->
    <div class="zoom-controls">
      <button class="zoom-btn" @click="setZoom(zoomPercent + 20)">＋</button>
      <div class="zoom-slider-container">
        <span class="zoom-label">{{ zoomPercent }}%</span>
        <input
          type="range"
          class="zoom-slider"
          min="50"
          max="500"
          :value="zoomPercent"
          @input="setZoom(Number(($event.target as HTMLInputElement).value))"
        >
      </div>
      <button class="zoom-btn" @click="setZoom(zoomPercent - 20)">−</button>
    </div>

    <!-- 定位结果提示（2s 渐隐） -->
    <div v-if="locateToast !== null" class="locate-toast" :class="{ fail: !locateToast }">
      {{ locateToast ? t('map_locate_ok') : t('map_locate_fail') }}
    </div>

    <!-- 城市信息气泡（屏幕居中，旧版三按钮） -->
    <div v-if="bubbleVisible" class="bubble">
      <div class="city-name">
        <span>{{ bubbleCity }}</span>
        <span class="fav-star" @click="toggleFavorite">{{ isFavorite(bubbleCity) ? '★' : '☆' }}</span>
      </div>
      <div class="weather">{{ weather }}</div>
      <div class="section">
        <strong>{{ t('map_food') }}</strong>
        <div class="items">
          <span v-for="f in foods" :key="f">{{ f }}</span>
        </div>
      </div>
      <div class="section">
        <strong>{{ t('map_spots') }}</strong>
        <div class="items">
          <span v-for="s in spots" :key="s">{{ s }}</span>
        </div>
      </div>
      <div class="actions">
        <button @click="goToCity">{{ t('map_go_here') }}</button>
        <button @click="refreshRecommendations">{{ t('map_refresh') }}</button>
        <button class="secondary" @click="closeBubble">{{ t('map_close') }}</button>
      </div>
    </div>

    <!-- 历史/收藏面板：v-if 只负责挂载，.open/.show 类驱动滑入与遮罩显示（缺它们面板永远留在屏幕外） -->
    <div v-if="historyOpen" class="overlay show" @click="historyOpen = false"></div>
    <div v-if="historyOpen" class="history-panel open">
      <div class="panel-title">
        <span>{{ t('map_history_label') }}</span>
        <button @click="historyOpen = false">✕</button>
      </div>
      <template v-if="favorites.length">
        <p class="label">{{ t('map_favorites_short') }}</p>
        <div class="fav-chips">
          <span
            v-for="c in favorites"
            :key="'f-' + c"
            class="fav-chip"
            @click="showCityInfo(c)"
          >
            {{ c }}
            <button class="link" @click.stop="removeFavorite(c)">✕</button>
          </span>
        </div>
      </template>

      <p class="label" style="margin-top: 10px;">{{ t('map_history_short') }}</p>
      <div
        v-for="c in history"
        :key="'h-' + c"
        class="history-item"
        @click="showCityInfo(c)"
      >
        <span>{{ c }}</span>
        <button
          class="link"
          @click.stop="removeFromHistory(c)"
        >✕</button>
      </div>
      <p
        v-if="!history.length && !favorites.length"
        class="label"
      >{{ t('map_no_history') }}</p>
    </div>
  </div>
</template>

<style scoped>
/* 收藏并列置顶 chips（历史仍为列表行，最近优先） */
.fav-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.fav-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  background: var(--primary-light, #eef5ee);
  color: var(--primary-dark, #2d4a2d);
  border: 1px solid rgba(61, 122, 92, 0.35);
  border-radius: 14px;
  font-size: 13px;
  cursor: pointer;
  transition: 0.2s;
}
.fav-chip:hover {
  border-color: var(--primary, #3d7a5c);
}
.fav-chip .link {
  background: none;
  border: none;
  color: #98a89a;
  font-size: 12px;
  cursor: pointer;
  padding: 0;
  line-height: 1;
}
.fav-chip .link:hover {
  color: #ef4444;
}
.history-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.left-tools {
  display: flex;
  gap: 12px;
}
/* 定位结果 toast：顶部居中，2s 线性渐隐（forwards 停在透明，随 v-if 移除） */
.locate-toast {
  position: absolute;
  top: 76px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 120;
  padding: 8px 18px;
  background: rgba(45, 74, 45, 0.92);
  color: #fff;
  border-radius: 18px;
  font-size: 13px;
  pointer-events: none;
  animation: toast-fade 2s linear forwards;
}
.locate-toast.fail {
  background: rgba(180, 60, 50, 0.92);
}
@keyframes toast-fade {
  from { opacity: 1; }
  to { opacity: 0; }
}
.map-load-error {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-left: 12px;
  color: #c0392b;
  font-size: 12px;
}
.map-load-error button {
  border: 1px solid #c0392b;
  border-radius: 4px;
  background: transparent;
  color: #c0392b;
  cursor: pointer;
  padding: 1px 8px;
}
</style>
