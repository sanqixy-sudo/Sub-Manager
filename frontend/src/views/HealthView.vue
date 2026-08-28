<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
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
  tested_at?: string
  history: { status: string; tested_at: string }[]
}

const store = useAppStore()
const overview = ref<any>({ counts: {} })
const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const testing = ref(false)
const drawer = ref(false)
const selected = ref<Row | null>(null)
const detail = ref<any[]>([])
const historyLoading = ref(false)
const timer = ref<number>()
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
const stats = computed(() => [
  { label: '全部节点', value: overview.value.total || 0, cls: 'blue' },
  { label: '代理可用', value: overview.value.available || 0, cls: 'green' },
  { label: 'Google 可用', value: overview.value.google_available || 0, cls: 'purple' },
  { label: '不可用', value: overview.value.counts?.unavailable || 0, cls: 'red' },
  { label: '未测试', value: overview.value.counts?.untested || 0, cls: 'gray' },
])

function latency(v?: number) {
  return v ? `${v} ms` : '—'
}

function label(s: string) {
  return ({
    healthy: '双目标正常',
    google_blocked: 'Google 不通',
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
        <div><b>{{ s.value }}</b><span>{{ s.label }}</span></div>
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
          <el-option label="双目标正常" value="healthy" />
          <el-option label="Google 不通" value="google_blocked" />
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
        <el-button v-if="filters.subscription_id" @click="test('group')">测试当前组</el-button>
      </div>
      <el-table v-loading="loading" :data="rows" @row-click="open">
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
            <span class="health-status" :class="color(row.status)"><i></i>{{ label(row.status) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Cloudflare" width="115">
          <template #default="{ row }">
            <b :class="row.connectivity_ok ? 'ok-text' : 'bad-text'">{{ latency(row.connectivity_latency_ms) }}</b>
          </template>
        </el-table-column>
        <el-table-column label="Google" width="115">
          <template #default="{ row }">
            <b :class="row.google_ok ? 'ok-text' : 'bad-text'">{{ row.google_ok ? latency(row.google_latency_ms) : row.tested_at ? '失败' : '—' }}</b>
          </template>
        </el-table-column>
        <el-table-column label="连续失败" width="90">
          <template #default="{ row }">{{ row.consecutive_failures || 0 }}</template>
        </el-table-column>
        <el-table-column label="历史测试" min-width="230">
          <template #default="{ row }">
            <div class="health-blocks">
              <i v-for="(h, i) in row.history.slice(-24)" :key="i" :class="color(h.status)" :title="dt(h.tested_at) + ' · ' + label(h.status)"></i>
              <span v-if="!row.history.length">暂无记录</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="最近测试" min-width="170">
          <template #default="{ row }">{{ dt(row.tested_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="test('node', row)">测试</el-button>
          </template>
        </el-table-column>
      </el-table>
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
          <span>Cloudflare<b>{{ latency(selected.connectivity_latency_ms) }}</b></span>
          <span>Google<b>{{ selected.google_ok ? latency(selected.google_latency_ms) : '失败' }}</b></span>
          <span>连续失败<b>{{ selected.consecutive_failures || 0 }}</b></span>
        </div>
        <h3>最近 30 天测试</h3>
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
  </section>
</template>
