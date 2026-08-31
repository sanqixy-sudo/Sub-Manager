<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Activity, Play, RefreshCw, ShieldCheck } from 'lucide-vue-next'
import { api } from '../api'
import { useAppStore } from '../stores/app'
import { dt } from '../utils'

type Row = {
  subscription_id: number
  subscription_name: string
  node_key: string
  final_name: string
  source_name: string
  protocol: string
  status: string
  connectivity_ok?: boolean
  connectivity_latency_ms?: number
  google_ok?: boolean
  google_latency_ms?: number
  consecutive_failures: number
  error_code?: string
  tested_at?: string
  history: { status: string; connectivity_latency_ms?: number; tested_at: string }[]
}

const store = useAppStore()
const router = useRouter()
const overview = ref<any>({ counts: {} })
const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const testing = ref(false)
const batchTesting = ref(false)
const selectedRows = ref<Row[]>([])
const drawer = ref(false)
const selected = ref<Row | null>(null)
const detail = ref<any[]>([])
const historyLoading = ref(false)
const timer = ref<number>()
const viewMode = ref<'cards' | 'table'>('cards')
const filters = reactive({
  subscription_id: undefined as number | undefined,
  status: '',
  protocol: '',
  search: '',
  hours: 24,
  page: 1,
  page_size: 50,
})

const protocols = computed(() => [...new Set(rows.value.map(x => x.protocol))])
const stats = computed(() => {
  const totalAll = overview.value.total || 0
  // 可用率 = 可用数 / 全部节点，total 为 0 时不显示百分比
  const rate = (v: number) => (totalAll ? Math.round((v / totalAll) * 100) : null)
  const available = overview.value.available || 0
  const googleAvailable = overview.value.google_available || 0
  return [
    { label: '全部节点', value: totalAll, cls: 'blue', pct: null as number | null },
    { label: '代理可用', value: available, cls: 'green', pct: rate(available) },
    { label: 'Google 可用', value: googleAvailable, cls: 'purple', pct: rate(googleAvailable) },
    { label: '不可用', value: overview.value.counts?.unavailable || 0, cls: 'red', pct: null as number | null },
    { label: '未测试', value: overview.value.counts?.untested || 0, cls: 'gray', pct: null as number | null },
  ]
})

function latency(v?: number) {
  return v ? `${v} ms` : '—'
}

/** 延迟按质量着色：<300ms 绿、<800ms 黄、≥800ms 红、无值灰 */
function latencyClass(v?: number) {
  if (!v) return 'lat-none'
  if (v < 300) return 'lat-good'
  if (v < 800) return 'lat-mid'
  return 'lat-bad'
}

/** 色块时间槽位：固定 48 格铺满整个时间窗口，每格取最差状态、最高延迟 */
const SLOT_COUNT = 48
const SLOT_SEVERITY: Record<string, number> = {
  unavailable: 4,
  connectivity_target_failed: 3,
  google_blocked: 2,
  healthy: 1,
}

function slotBlocks(history: Row['history']) {
  const spanMs = filters.hours * 3600_000
  const slotMs = spanMs / SLOT_COUNT
  const end = Date.now()
  const start = end - spanMs
  const slots: ({ status: string; lat: number; count: number } | undefined)[] = []
  for (const h of history) {
    const t = new Date(h.tested_at).getTime()
    if (Number.isNaN(t) || t < start || t > end) continue
    const idx = Math.min(SLOT_COUNT - 1, Math.floor((t - start) / slotMs))
    const slot = slots[idx] || (slots[idx] = { status: 'healthy', lat: 0, count: 0 })
    slot.count += 1
    if ((SLOT_SEVERITY[h.status] || 0) >= (SLOT_SEVERITY[slot.status] || 0)) slot.status = h.status
    slot.lat = Math.max(slot.lat, h.connectivity_latency_ms || 0)
  }
  const slotMin = Math.max(1, Math.round(slotMs / 60000))
  return Array.from({ length: SLOT_COUNT }, (_, i) => {
    const at = dt(new Date(start + i * slotMs).toISOString())
    const slot = slots[i]
    if (!slot) return { cls: 'gray', status: '', tip: `${at} 起 ${slotMin} 分钟 · 无数据` }
    const cls = slot.status === 'healthy' ? latencyClass(slot.lat || undefined).replace('lat-none', 'gray') : color(slot.status)
    return {
      cls,
      status: slot.status,
      tip: `${at} 起 ${slotMin} 分钟 · ${slot.count} 次 · ${label(slot.status)}${slot.lat ? ` · 最高 ${slot.lat} ms` : ''}`,
    }
  })
}

/** 卡片多选：以 subscription_id+node_key 为唯一标识 */
function rowKey(row: Row) {
  return `${row.subscription_id}:${row.node_key}`
}

function isSelected(row: Row) {
  return selectedRows.value.some(x => rowKey(x) === rowKey(row))
}

function toggleSelect(row: Row, checked: boolean) {
  selectedRows.value = checked
    ? [...selectedRows.value, row]
    : selectedRows.value.filter(x => rowKey(x) !== rowKey(row))
}

/** 可用率着色：≥90% 绿 / ≥60% 黄 / 否则红 */
function pctClass(p: number) {
  if (p >= 90) return 'pct-good'
  if (p >= 60) return 'pct-mid'
  return 'pct-bad'
}

/** 列排序（仅当前页，服务端分页不上送排序参数） */
const sortByLatency = (a: Row, b: Row) => (a.connectivity_latency_ms ?? Number.MAX_SAFE_INTEGER) - (b.connectivity_latency_ms ?? Number.MAX_SAFE_INTEGER)
const sortByFailures = (a: Row, b: Row) => (a.consecutive_failures || 0) - (b.consecutive_failures || 0)
const sortByTestedAt = (a: Row, b: Row) => String(a.tested_at || '').localeCompare(String(b.tested_at || ''))

/** 卡片视图排序（仅当前页客户端排序） */
const cardSort = ref<'default' | 'latency' | 'failures'>('default')
const cardRows = computed(() => {
  const list = rows.value.slice()
  if (cardSort.value === 'latency') list.sort(sortByLatency)
  else if (cardSort.value === 'failures') list.sort((a, b) => sortByFailures(b, a))
  return list
})

function label(s: string) {
  return ({
    healthy: '正常',
    google_blocked: 'Google 受限',
    connectivity_target_failed: 'Cloudflare 异常',
    unavailable: '不可用',
    untested: '未测试',
    skipped: '已跳过',
  } as Record<string, string>)[s] || s
}

function color(s: string) {
  return ({
    healthy: 'green',
    google_blocked: 'yellow',
    connectivity_target_failed: 'blue',
    unavailable: 'red',
    skipped: 'gray',
  } as Record<string, string>)[s] || 'gray'
}

/** error_code 形如 "cf:timeout,google:tls"，兼容旧的单词值；未知值原样显示 */
const ERROR_LABELS: Record<string, string> = {
  timeout: '连接超时',
  refused: '连接被拒',
  reset: '连接被重置',
  dns: 'DNS 解析失败',
  tls: 'TLS 握手失败',
  auth: '认证失败',
  both_targets_failed: '双目标均失败',
  unsupported_protocol: '协议不支持',
  other: '其他错误',
}

function errorText(code?: string) {
  if (!code) return ''
  return code.split(',').map(part => {
    const kind = part.includes(':') ? part.slice(part.indexOf(':') + 1) : part
    return ERROR_LABELS[kind] || kind || '未知'
  }).filter(Boolean).join('、')
}

async function load() {
  loading.value = true
  try {
    overview.value = await api('/api/node-health/overview')
    const q = new URLSearchParams()
    Object.entries(filters).forEach(([k, v]) => {
      if (v !== '' && v != null) q.set(k, String(v))
    })
    const data = await api<any>(`/api/node-health/nodes?${q}`)
    rows.value = data.items
    total.value = data.total ?? data.items.length
    testing.value = !!overview.value.health_check_running
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

/** 筛选条件变化：回到第一页再加载 */
function onFilterChange() {
  filters.page = 1
  load()
}

let searchTimer: number | undefined
/** 搜索框实时输入，300ms 防抖后触发查询 */
function onSearchInput() {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(onFilterChange, 300)
}

async function test(scope: 'all' | 'group' | 'node', row?: Row) {
  try {
    const body: any = {}
    if (scope === 'group') body.subscription_id = filters.subscription_id
    if (scope === 'node' && row) {
      body.subscription_id = row.subscription_id
      body.node_key = row.node_key
    }
    const result: any = await api('/api/node-health/tests', { method: 'POST', body: JSON.stringify(body) })
    testing.value = true
    ElMessage.success(result.reused ? '已在测试中' : '测活任务已开始')
    poll()
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

function poll() {
  window.clearInterval(timer.value)
  timer.value = window.setInterval(async () => {
    await load()
    if (!testing.value) window.clearInterval(timer.value)
  }, 2000)
}

/** 跳转组详情节点 Tab 并按节点名搜索定位 */
function manage(row: Row) {
  router.push(`/groups/${row.subscription_id}/detail?tab=nodes&search=${encodeURIComponent(row.final_name)}`)
}

/** 批量测活：每 3 个一组并发发出，全部发出后提示并轮询进度 */
async function testSelected() {
  const list = selectedRows.value.slice()
  if (!list.length || batchTesting.value) return
  batchTesting.value = true
  try {
    for (let i = 0; i < list.length; i += 3) {
      await Promise.all(list.slice(i, i + 3).map(row =>
        api('/api/node-health/tests', { method: 'POST', body: JSON.stringify({ subscription_id: row.subscription_id, node_key: row.node_key }) })))
    }
    testing.value = true
    ElMessage.success(`已对 ${list.length} 个节点发起测试`)
    poll()
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    batchTesting.value = false
  }
}

async function open(row: Row) {
  selected.value = row
  drawer.value = true
  detail.value = []
  historyLoading.value = true
  try {
    detail.value = (await api<any>(`/api/node-health/nodes/${row.subscription_id}/${row.node_key}/history?hours=720`)).items
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    historyLoading.value = false
  }
}

// P1：抽屉迷你趋势图，取近 30 条，条高按 Cloudflare 延迟归一化到本批最大值
const trendBars = computed(() => {
  const items = detail.value.slice(-30)
  const max = Math.max(0, ...items.map((r: any) => r.connectivity_latency_ms || 0))
  return items.map((r: any) => {
    const v = r.connectivity_latency_ms
    // 无延迟/失败用最矮高度，避免与正常低延迟混淆
    const pct = v && max ? Math.max(12, Math.round((v / max) * 100)) : 12
    return {
      pct,
      cls: color(r.status),
      title: `${dt(r.tested_at)} · ${label(r.status)} · CF ${latency(v)}`,
    }
  })
})

// P1：历史色块共享浮层（替代每格 el-tooltip），事件委托读取 data-tip
const blockTip = reactive({ show: false, text: '', x: 0, y: 0 })

function onBlockHover(e: MouseEvent) {
  const target = (e.target as HTMLElement).closest('.health-blocks i') as HTMLElement | null
  if (!target?.dataset.tip) {
    blockTip.show = false
    return
  }
  blockTip.text = target.dataset.tip
  // 固定定位跟随鼠标，右侧留出浮层宽度避免溢出视口
  blockTip.x = Math.min(e.clientX + 12, window.innerWidth - 240)
  blockTip.y = e.clientY + 14
  blockTip.show = true
}

function hideBlockTip() {
  blockTip.show = false
}

onMounted(async () => {
  if (!store.groups.length) await store.loadGroups()
  await load()
  if (testing.value) poll()
})

onBeforeUnmount(() => {
  window.clearInterval(timer.value)
  window.clearTimeout(searchTimer)
})
</script>

<template>
  <section class="page health-page">
    <header class="page-head">
      <div>
        <p class="eyebrow">NODE HEALTH CENTER</p>
        <h1>节点测活</h1>
        <p>流量真实经过节点访问 Cloudflare 与 Google，延迟不是服务器 Ping。</p>
      </div>
      <div class="head-actions">
        <el-button :loading="loading" @click="load"><RefreshCw />刷新</el-button>
        <el-button type="primary" :loading="testing" @click="test('all')"><Play />测试全部</el-button>
      </div>
    </header>
    <div class="health-stats">
      <article v-for="s in stats" :key="s.label" class="panel">
        <i :class="s.cls"></i>
        <div>
          <b>{{ s.value }}</b>
          <span>{{ s.label }}<em v-if="s.pct != null" class="stat-pct" :class="pctClass(s.pct)">{{ s.pct }}%</em></span>
        </div>
      </article>
    </div>
    <div v-if="testing" class="health-running panel">
      <Activity />
      <div>
        <b>节点测活正在运行</b>
        <span>任务完成前页面每 2 秒自动更新，订阅刷新不受影响。</span>
      </div>
    </div>
    <section class="panel health-table">
      <div class="health-toolbar">
        <el-input v-model="filters.search" placeholder="搜索节点或来源" clearable @input="onSearchInput" />
        <el-select v-model="filters.subscription_id" placeholder="全部订阅组" clearable @change="onFilterChange">
          <el-option v-for="g in store.groups" :key="g.id" :label="g.name" :value="g.id" />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable @change="onFilterChange">
          <el-option label="正常" value="healthy" />
          <el-option label="Google 受限" value="google_blocked" />
          <el-option label="Cloudflare 异常" value="connectivity_target_failed" />
          <el-option label="不可用" value="unavailable" />
          <el-option label="未测试" value="untested" />
        </el-select>
        <el-select v-model="filters.protocol" placeholder="全部协议" clearable @change="onFilterChange">
          <el-option v-for="p in protocols" :key="p" :label="p.toUpperCase()" :value="p" />
        </el-select>
        <el-radio-group v-model="filters.hours" @change="onFilterChange">
          <el-radio-button :value="24">24h</el-radio-button>
          <el-radio-button :value="168">7d</el-radio-button>
          <el-radio-button :value="720">30d</el-radio-button>
        </el-radio-group>
        <el-radio-group v-model="viewMode" class="health-view-toggle">
          <el-radio-button value="cards">卡片</el-radio-button>
          <el-radio-button value="table">表格</el-radio-button>
        </el-radio-group>
        <el-select v-if="viewMode === 'cards'" v-model="cardSort" class="health-sort-select">
          <el-option label="默认排序" value="default" />
          <el-option label="延迟最低优先" value="latency" />
          <el-option label="连续失败优先" value="failures" />
        </el-select>
        <el-button v-if="filters.subscription_id" @click="test('group')">测试当前组</el-button>
        <el-button v-if="selectedRows.length" type="primary" plain :loading="batchTesting" @click="testSelected">测试选中（{{ selectedRows.length }}）</el-button>
      </div>
      <div class="health-legend">
        <span><i class="lat-good"></i>快（&lt;300ms）</span>
        <span><i class="lat-mid"></i>中（&lt;800ms）</span>
        <span><i class="lat-bad"></i>慢（≥800ms）</span>
        <span><i class="red"></i>失败</span>
        <span><i class="gray"></i>无记录</span>
      </div>
      <p v-if="viewMode === 'table'" class="health-sort-hint">列排序仅作用于当前页</p>
      <div v-if="viewMode === 'cards'" v-loading="loading" class="health-cards" @mouseover="onBlockHover" @mouseleave="hideBlockTip">
        <article v-for="row in cardRows" :key="rowKey(row)" class="panel health-card" :class="{ selected: isSelected(row) }" @click="open(row)">
          <header>
            <el-checkbox :model-value="isSelected(row)" @click.stop @change="(v: string | number | boolean) => toggleSelect(row, !!v)" />
            <div class="health-node">
              <b :title="row.final_name">{{ row.final_name }}</b>
              <small>{{ row.subscription_name }} · {{ row.source_name }} · {{ row.protocol.toUpperCase() }}</small>
            </div>
            <span class="health-status" :class="color(row.status)" :title="errorText(row.error_code) || undefined"><i></i>{{ label(row.status) }}</span>
          </header>
          <div class="health-card-latency">
            <span>Cloudflare <b :class="latencyClass(row.connectivity_latency_ms)">{{ latency(row.connectivity_latency_ms) }}</b></span>
            <span>Google <b :class="row.google_ok ? latencyClass(row.google_latency_ms) : row.tested_at ? 'lat-bad' : 'lat-none'">{{ row.google_ok ? latency(row.google_latency_ms) : row.tested_at ? '失败' : '—' }}</b></span>
          </div>
          <div class="health-blocks health-card-blocks">
            <i
              v-for="(b, i) in slotBlocks(row.history)"
              :key="i"
              :class="[b.cls, b.status]"
              :data-tip="b.tip"
            ></i>
          </div>
          <footer>
            <span>连续失败 {{ row.consecutive_failures || 0 }} · {{ dt(row.tested_at) }}</span>
            <div>
              <el-button link type="primary" @click.stop="test('node', row)">测试</el-button>
              <el-button link type="primary" @click.stop="manage(row)">管理</el-button>
            </div>
          </footer>
        </article>
        <p v-if="!rows.length && !loading" class="health-cards-empty">暂无节点。</p>
      </div>
      <div v-else class="health-table-scroll" @mouseover="onBlockHover" @mouseleave="hideBlockTip">
      <el-table v-loading="loading" :data="rows" @row-click="open" @selection-change="(v: Row[]) => (selectedRows = v)">
        <el-table-column type="selection" width="44" />
        <el-table-column label="节点" min-width="230">
          <template #default="{ row }">
            <div class="health-node">
              <b>{{ row.final_name }}</b>
              <span>{{ row.subscription_name }} · {{ row.source_name }} · {{ row.protocol.toUpperCase() }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="125">
          <template #default="{ row }">
            <span class="health-status" :class="color(row.status)" :title="errorText(row.error_code) || undefined"><i></i>{{ label(row.status) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Cloudflare" width="125" sortable :sort-method="sortByLatency">
          <template #default="{ row }">
            <b :class="latencyClass(row.connectivity_latency_ms)">{{ latency(row.connectivity_latency_ms) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="Google" width="115">
          <template #default="{ row }">
            <b :class="row.google_ok ? latencyClass(row.google_latency_ms) : row.tested_at ? 'lat-bad' : 'lat-none'">{{ row.google_ok ? latency(row.google_latency_ms) : row.tested_at ? '失败' : '—' }}</b>
          </template>
        </el-table-column>
        <el-table-column label="连续失败" width="105" sortable :sort-method="sortByFailures">
          <template #default="{ row }">{{ row.consecutive_failures || 0 }}</template>
        </el-table-column>
        <el-table-column label="历史测试" min-width="230">
          <template #default="{ row }">
            <div class="health-blocks">
              <i
                v-for="(b, i) in slotBlocks(row.history)"
                :key="i"
                :class="[b.cls, b.status]"
                :data-tip="b.tip"
              ></i>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="最近测试" min-width="170" sortable :sort-method="sortByTestedAt">
          <template #default="{ row }">{{ dt(row.tested_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="test('node', row)">测试</el-button>
            <el-button link type="primary" @click.stop="manage(row)">管理</el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>
      <div class="health-pagination">
        <el-pagination
          background
          layout="total, sizes, prev, pager, next"
          :total="total"
          v-model:current-page="filters.page"
          v-model:page-size="filters.page_size"
          :page-sizes="[20, 50, 100]"
          @current-change="load"
          @size-change="onFilterChange"
        />
      </div>
    </section>
    <el-drawer v-model="drawer" size="min(520px,100%)">
      <template #header>
        <div>
          <ShieldCheck />
          <div>
            <small>NODE HEALTH</small>
            <h2>{{ selected?.final_name }}</h2>
          </div>
        </div>
      </template>
      <div v-if="selected" class="health-detail">
        <div class="health-detail-summary">
          <span>当前状态<b>{{ label(selected.status) }}</b></span>
          <span>Cloudflare<b :class="latencyClass(selected.connectivity_latency_ms)">{{ latency(selected.connectivity_latency_ms) }}</b></span>
          <span>Google<b :class="selected.google_ok ? latencyClass(selected.google_latency_ms) : 'lat-bad'">{{ selected.google_ok ? latency(selected.google_latency_ms) : '失败' }}</b></span>
          <span>连续失败<b>{{ selected.consecutive_failures || 0 }}</b></span>
          <span v-if="selected.error_code">失败原因<b>{{ errorText(selected.error_code) }}</b></span>
        </div>
        <h3>最近 30 天测试</h3>
        <div v-if="trendBars.length" class="health-trend">
          <i v-for="(b, i) in trendBars" :key="i" :class="b.cls" :style="{ height: b.pct + '%' }" :title="b.title"></i>
        </div>
        <div v-loading="historyLoading" class="health-history-list">
          <article v-for="(r, i) in detail" :key="i">
            <span class="health-status" :class="color(r.status)"><i></i>{{ label(r.status) }}</span>
            <b>CF {{ latency(r.connectivity_latency_ms) }} · Google {{ r.google_ok ? latency(r.google_latency_ms) : '失败' }}</b>
            <time>{{ dt(r.tested_at) }}</time>
          </article>
          <p v-if="!detail.length && !historyLoading">暂无历史测试。</p>
        </div>
      </div>
    </el-drawer>
    <div v-if="blockTip.show" class="health-block-tip" :style="{ left: blockTip.x + 'px', top: blockTip.y + 'px' }">{{ blockTip.text }}</div>
  </section>
</template>
