<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, RefreshCw, Search, MoreHorizontal, Copy, Server, AlertTriangle } from 'lucide-vue-next'
import { api } from '../api'
import { useAppStore } from '../stores/app'
import type { Group } from '../types'
import StatusTag from '../components/StatusTag.vue'
import QrDialog from '../components/QrDialog.vue'
import { copyText, dt, duration, importSchemes, openImportScheme, statusPriority, supportsImport } from '../utils'

const PAGE_SIZE = 20

const store = useAppStore()
const router = useRouter()
const search = ref('')
const statusFilter = ref('')
const cacheFilter = ref('')
const drawer = ref(false)
const selected = ref<Group | null>(null)
const urls = ref<any[]>([])
const statusUrl = ref('')
const diagnostics = ref<any[]>([])
const busy = ref<number | null>(null)
const copying = ref<number | null>(null)
const loading = ref(false)
const page = ref(1)
const refreshingAll = ref(false)
const refreshProgress = ref({ done: 0, total: 0 })
const qrVisible = ref(false)
const qrTarget = ref<{ title: string; url: string } | null>(null)

const shown = computed(() =>
  store.groups
    .filter(g => `${g.name} ${g.note} ${g.upstreams.map(x => x.name).join(' ')}`.toLowerCase().includes(search.value.toLowerCase()))
    .filter(g => !statusFilter.value || g.last_refresh_status === statusFilter.value)
    .filter(g => !cacheFilter.value || g.cache_state === cacheFilter.value)
    .sort((a, b) =>
      statusPriority(a.last_refresh_status) - statusPriority(b.last_refresh_status) ||
      (b.pending_node_count || 0) - (a.pending_node_count || 0)))

/** 前端分页：每页 20 条，表格与移动卡片共用 */
const paged = computed(() => shown.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE))

// 搜索/筛选变化时回到第 1 页
watch([search, statusFilter, cacheFilter], () => {
  page.value = 1
})

const next = (v?: string) => {
  if (!v) return '—'
  const m = Math.ceil((new Date(v).getTime() - Date.now()) / 60000)
  return m <= 0 ? '即将刷新' : m < 60 ? `${m} 分钟后` : `${Math.floor(m / 60)} 小时后`
}

const enabledOutputs = (g: Group) => g.outputs.filter(x => x.enabled)

onMounted(async () => {
  loading.value = true
  try {
    await store.loadGroups()
  } finally {
    loading.value = false
  }
})

async function refresh(id: number) {
  busy.value = id
  try {
    const r: any = await api(`/api/subscriptions/${id}/refresh`, { method: 'POST' })
    r.status === 'ok' ? ElMessage.success(`刷新完成，${r.node_count} 个节点`) : ElMessage.warning(`刷新结果：${r.status}`)
    await store.load()
    selected.value = store.groups.find(x => x.id === id) || selected.value
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    busy.value = null
  }
}

async function openDetail(group: Group) {
  selected.value = group
  drawer.value = true
  diagnostics.value = []
  try {
    const data: any = await api<any>(`/api/subscriptions/${group.id}/public-urls`)
    urls.value = data.urls
    statusUrl.value = data.status_url || ''
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

async function diagnose() {
  if (!selected.value) return
  busy.value = selected.value.id
  try {
    diagnostics.value = (await api<any>(`/api/subscriptions/${selected.value.id}/diagnose`, { method: 'POST' })).results
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    busy.value = null
  }
}

async function copy(v: string) {
  await copyText(v)
  ElMessage.success('订阅地址已复制')
}

/** 行内复制：复制该组指定 slug（默认第一个）的公开订阅地址 */
async function copyGroupUrl(g: Group, slug?: string) {
  copying.value = g.id
  try {
    const list: any[] = (await api<any>(`/api/subscriptions/${g.id}/public-urls`)).urls
    const target = slug ? list.find(u => u.slug === slug) : list[0]
    if (!target) {
      ElMessage.warning('该组暂无可用订阅地址')
      return
    }
    await copyText(target.url)
    ElMessage.success('订阅地址已复制')
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    copying.value = null
  }
}

/** 复制下拉统一入口：command 形如 copy:slug / import:slug / qr:slug（slug 本身不含冒号前缀，取第一个冒号切分） */
async function onOutputCommand(g: Group, cmd: string) {
  const sep = cmd.indexOf(':')
  const action = cmd.slice(0, sep)
  const slug = cmd.slice(sep + 1)
  if (action === 'import') await importGroupUrl(g, slug)
  else if (action === 'qr') await openQrCode(g, slug)
  else await copyGroupUrl(g, slug)
}

/** 一键导入：尝试唤起客户端 scheme，800ms 后页面仍可见说明未唤起，复制地址兜底 */
async function importToClient(clientType: string, url: string) {
  const [item] = importSchemes(clientType, url)
  if (!item) return
  const opened = await openImportScheme(item.scheme)
  if (!opened) {
    await copyText(url)
    ElMessage.warning('未检测到客户端，地址已复制，可手动粘贴')
  }
}

/** 行内导入：取该组指定 slug（默认第一个）的公开订阅地址后唤起客户端 */
async function importGroupUrl(g: Group, slug?: string) {
  copying.value = g.id
  try {
    const list: any[] = (await api<any>(`/api/subscriptions/${g.id}/public-urls`)).urls
    const target = slug ? list.find((u: any) => u.slug === slug) : list[0]
    if (!target) {
      ElMessage.warning('该组暂无可用订阅地址')
      return
    }
    await importToClient(target.client_type, target.url)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    copying.value = null
  }
}

/** 扫码导入：取该组指定 slug（默认第一个）的公开订阅地址，弹二维码对话框 */
async function openQrCode(g: Group, slug?: string) {
  copying.value = g.id
  try {
    const list: any[] = (await api<any>(`/api/subscriptions/${g.id}/public-urls`)).urls
    const target = slug ? list.find((u: any) => u.slug === slug) : list[0]
    if (!target) {
      ElMessage.warning('该组暂无可用订阅地址')
      return
    }
    qrTarget.value = { title: target.name, url: target.url }
    qrVisible.value = true
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    copying.value = null
  }
}

/** 全部刷新：确认后逐个调用刷新接口，进行中显示进度，结束后汇总并重新加载 */
async function refreshAll() {
  const list = store.groups.slice()
  if (!list.length || refreshingAll.value) return
  try {
    await ElMessageBox.confirm(`将逐个刷新全部 ${list.length} 个订阅组，可能需要一些时间。确认继续？`, '全部刷新', { type: 'warning' })
  } catch {
    return // 用户取消，静默
  }
  refreshingAll.value = true
  refreshProgress.value = { done: 0, total: list.length }
  let ok = 0
  let fail = 0
  for (const g of list) {
    try {
      const r: any = await api(`/api/subscriptions/${g.id}/refresh`, { method: 'POST' })
      r.status === 'ok' ? ok++ : fail++
    } catch {
      fail++
    }
    refreshProgress.value.done++
  }
  refreshingAll.value = false
  ElMessage[fail ? 'warning' : 'success'](`刷新完成：成功 ${ok}，失败 ${fail}`)
  await store.load()
}

async function rotate(g: Group) {
  try {
    await ElMessageBox.confirm('旧订阅地址会立即失效，确认重置公开 Token？', '重置 Token', { type: 'warning' })
  } catch {
    return // 用户取消，静默
  }
  try {
    const data: any = await api<any>(`/api/subscriptions/${g.id}/rotate-token`, { method: 'POST' })
    urls.value = data.urls
    statusUrl.value = data.status_url || ''
    ElMessage.success('Token 已重置')
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

async function remove(g: Group) {
  try {
    await ElMessageBox.confirm(`将永久删除“${g.name}”及其缓存，确认继续？`, '删除订阅组', { type: 'error' })
  } catch {
    return // 用户取消，静默
  }
  try {
    await api(`/api/subscriptions/${g.id}`, { method: 'DELETE' })
    drawer.value = false
    await store.load()
    ElMessage.success('订阅组已删除')
  } catch (e: any) {
    ElMessage.error(e.message)
  }
}

async function duplicate(g: Group) {
  try {
    const d: any = await api(`/api/subscriptions/${g.id}`)
    sessionStorage.setItem('duplicateGroup', JSON.stringify({
      ...d,
      id: undefined,
      name: `${d.name} 副本`,
      upstreams: d.upstreams.map((x: any) => ({ ...x, id: undefined })),
      outputs: d.outputs.map((x: any) => ({ ...x, id: undefined })),
    }))
    await router.push('/groups/new')
  } catch (e: any) {
    ElMessage.error(e?.message || '复制组失败，请稍后重试')
  }
}
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">SUBSCRIPTION GROUPS</p>
        <h1>订阅组</h1>
        <p>异常优先排列，集中维护固定客户端订阅地址。</p>
      </div>
      <div class="head-actions">
        <el-button @click="store.load"><RefreshCw />刷新</el-button>
        <el-button type="primary" @click="router.push('/groups/new')"><Plus />新建订阅组</el-button>
      </div>
    </header>
    <div v-if="!loading && !store.groups.length" class="panel onboard-panel">
      <span class="onboard-icon"><Server /></span>
      <h2>从第一个订阅组开始</h2>
      <p class="onboard-sub">三分钟完成从订阅链接到客户端导入的完整闭环。</p>
      <ol class="onboard-steps">
        <li>
          <i>1</i>
          <div>
            <b>新建订阅组并粘贴订阅链接</b>
            <span>支持 Clash/YAML、Base64、URI 混合，多个来源可以一次粘贴。</span>
          </div>
        </li>
        <li>
          <i>2</i>
          <div>
            <b>系统定时刷新，失败自动用上次成功缓存</b>
            <span>上游临时故障不会影响客户端正在使用的订阅。</span>
          </div>
        </li>
        <li>
          <i>3</i>
          <div>
            <b>复制固定订阅地址导入客户端</b>
            <span>地址长期不变，可直接导入 Clash Verge 等客户端。</span>
          </div>
        </li>
      </ol>
      <div class="onboard-actions">
        <el-button type="primary" @click="router.push('/groups/new')"><Plus />立即新建订阅组</el-button>
        <el-button @click="router.push('/tools')">先去检查工具试试解析</el-button>
      </div>
    </div>
    <div v-else class="panel table-panel">
      <div class="toolbar group-toolbar">
        <div class="search">
          <Search />
          <input v-model="search" placeholder="搜索订阅组或上游名称">
        </div>
        <el-select v-model="statusFilter" clearable placeholder="全部状态">
          <el-option label="正常" value="ok" />
          <el-option label="部分异常" value="partial" />
          <el-option label="旧缓存" value="stale" />
          <el-option label="失败" value="error" />
        </el-select>
        <el-select v-model="cacheFilter" clearable placeholder="全部缓存">
          <el-option label="新鲜" value="fresh" />
          <el-option label="旧缓存" value="stale" />
          <el-option label="暂无" value="empty" />
        </el-select>
        <el-button :loading="refreshingAll" @click="refreshAll">
          <RefreshCw />{{ refreshingAll ? `全部刷新（${refreshProgress.done}/${refreshProgress.total}）` : '全部刷新' }}
        </el-button>
        <span>共 {{ shown.length }} 个订阅组</span>
      </div>
      <div class="desktop-table">
        <el-table :data="paged" row-key="id" @row-click="openDetail">
          <el-table-column label="订阅组" min-width="220">
            <template #default="{ row }">
              <div class="group-cell">
                <span class="group-avatar">{{ row.name.slice(0, 1) }}</span>
                <div>
                  <b>{{ row.name }}</b>
                  <small>{{ row.note || '暂无备注' }}</small>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="122">
            <template #default="{ row }"><StatusTag :status="row.last_refresh_status" /></template>
          </el-table-column>
          <el-table-column label="上游健康" width="118">
            <template #default="{ row }">
              <b>{{ row.upstreams.filter((x: any) => x.last_status === 'ok').length }}</b>
              <span class="muted"> / {{ row.upstreams.filter((x: any) => x.enabled).length }}</span>
            </template>
          </el-table-column>
          <el-table-column label="有效节点" width="104">
            <template #default="{ row }">
              <b>{{ row.node_count || 0 }}</b>
              <small v-if="row.filtered_count" class="filtered">−{{ row.filtered_count }} 过滤</small>
              <small v-if="row.pending_node_count" class="pending-count">
                <AlertTriangle />{{ row.pending_node_count }} 待确认
              </small>
            </template>
          </el-table-column>
          <el-table-column label="输出" width="78">
            <template #default="{ row }">{{ row.outputs.filter((x: any) => x.enabled).length }}</template>
          </el-table-column>
          <el-table-column label="缓存" width="92">
            <template #default="{ row }">
              <span class="cache" :class="row.cache_state">{{ row.cache_state === 'fresh' ? '新鲜' : row.cache_state === 'stale' ? '旧缓存' : '暂无' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="上次刷新" min-width="154">
            <template #default="{ row }">
              <span>{{ dt(row.last_success_at) }}</span>
              <small>{{ duration(row.last_duration_ms) }} · {{ next(row.next_refresh_at) }}</small>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="300" fixed="right">
            <template #default="{ row }">
              <div class="row-actions" @click.stop>
                <el-button link type="primary" :loading="busy === row.id" @click="refresh(row.id)">刷新</el-button>
                <template v-if="enabledOutputs(row).length">
                  <el-dropdown v-if="enabledOutputs(row).length > 1" trigger="click" @command="(cmd: string) => onOutputCommand(row, cmd)">
                    <el-button link type="primary" :loading="copying === row.id">复制 / 导入</el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item v-for="o in enabledOutputs(row)" :key="o.id" :command="`copy:${o.slug}`">
                          复制 {{ o.name }}（{{ store.types[o.client_type]?.label || o.client_type }}）
                        </el-dropdown-item>
                        <template v-for="o in enabledOutputs(row)" :key="`import-${o.id}`">
                          <el-dropdown-item v-if="supportsImport(o.client_type)" :command="`import:${o.slug}`">
                            导入 {{ o.name }}（{{ store.types[o.client_type]?.label || o.client_type }}）
                          </el-dropdown-item>
                        </template>
                        <el-dropdown-item v-for="o in enabledOutputs(row)" :key="`qr-${o.id}`" :command="`qr:${o.slug}`">
                          扫码导入 {{ o.name }}（{{ store.types[o.client_type]?.label || o.client_type }}）
                        </el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                  <template v-else>
                    <el-button link type="primary" :loading="copying === row.id" @click="copyGroupUrl(row)">复制地址</el-button>
                    <el-button v-if="supportsImport(enabledOutputs(row)[0].client_type)" link type="primary" :loading="copying === row.id" @click="importGroupUrl(row)">导入</el-button>
                    <el-button link type="primary" :loading="copying === row.id" @click="openQrCode(row)">二维码</el-button>
                  </template>
                </template>
                <el-dropdown>
                  <button class="more"><MoreHorizontal /></button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item @click="router.push(`/groups/${row.id}/detail`)">完整详情</el-dropdown-item>
                      <el-dropdown-item @click="router.push(`/groups/${row.id}/edit`)">编辑</el-dropdown-item>
                      <el-dropdown-item @click="duplicate(row)">复制组</el-dropdown-item>
                      <el-dropdown-item divided @click="remove(row)">删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="mobile-list">
        <article v-for="g in paged" :key="g.id" @click="openDetail(g)">
          <header>
            <div class="group-cell">
              <span class="group-avatar">{{ g.name.slice(0, 1) }}</span>
              <div>
                <b>{{ g.name }}</b>
                <small>{{ g.note || '暂无备注' }}</small>
              </div>
            </div>
            <StatusTag :status="g.last_refresh_status" />
          </header>
          <div class="mobile-metrics">
            <span><b>{{ g.upstreams.filter(x => x.last_status === 'ok').length }}/{{ g.upstreams.filter(x => x.enabled).length }}</b>上游健康</span>
            <span>
              <b>{{ g.node_count || 0 }}</b>有效节点
              <i v-if="g.pending_node_count" class="pending-count">{{ g.pending_node_count }} 待确认</i>
            </span>
            <span><b>{{ g.outputs.filter(x => x.enabled).length }}</b>输出</span>
          </div>
          <footer>
            <span>{{ dt(g.last_success_at) }}</span>
            <div class="mobile-actions" @click.stop>
              <template v-if="enabledOutputs(g).length">
                <el-dropdown v-if="enabledOutputs(g).length > 1" trigger="click" @command="(cmd: string) => onOutputCommand(g, cmd)">
                  <el-button link type="primary" :loading="copying === g.id">复制 / 导入</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item v-for="o in enabledOutputs(g)" :key="o.id" :command="`copy:${o.slug}`">
                        复制 {{ o.name }}（{{ store.types[o.client_type]?.label || o.client_type }}）
                      </el-dropdown-item>
                      <template v-for="o in enabledOutputs(g)" :key="`import-${o.id}`">
                        <el-dropdown-item v-if="supportsImport(o.client_type)" :command="`import:${o.slug}`">
                          导入 {{ o.name }}（{{ store.types[o.client_type]?.label || o.client_type }}）
                        </el-dropdown-item>
                      </template>
                      <el-dropdown-item v-for="o in enabledOutputs(g)" :key="`qr-${o.id}`" :command="`qr:${o.slug}`">
                        扫码导入 {{ o.name }}（{{ store.types[o.client_type]?.label || o.client_type }}）
                      </el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
                <template v-else>
                  <el-button link type="primary" :loading="copying === g.id" @click="copyGroupUrl(g)">复制地址</el-button>
                  <el-button v-if="supportsImport(enabledOutputs(g)[0].client_type)" link type="primary" :loading="copying === g.id" @click="importGroupUrl(g)">导入</el-button>
                  <el-button link type="primary" :loading="copying === g.id" @click="openQrCode(g)">二维码</el-button>
                </template>
              </template>
              <el-button link type="primary" @click.stop="refresh(g.id)">刷新</el-button>
            </div>
          </footer>
        </article>
      </div>
      <div v-if="shown.length > PAGE_SIZE" class="group-pagination">
        <el-pagination
          background
          layout="total, prev, pager, next"
          :total="shown.length"
          v-model:current-page="page"
          :page-size="PAGE_SIZE"
        />
      </div>
      <div v-if="!shown.length" class="empty">
        <Server />
        <h3>没有符合条件的订阅组</h3>
        <p>调整筛选条件，或新建一个订阅组。</p>
      </div>
    </div>
    <el-drawer v-model="drawer" size="min(680px,100%)">
      <template #header>
        <div v-if="selected">
          <p class="eyebrow">QUICK VIEW</p>
          <h2>{{ selected.name }}</h2>
          <StatusTag :status="selected.last_refresh_status" />
        </div>
      </template>
      <template v-if="selected">
        <div class="drawer-actions">
          <el-button type="primary" @click="router.push(`/groups/${selected.id}/detail`)">查看完整详情</el-button>
          <el-button :loading="busy === selected.id" @click="refresh(selected.id)"><RefreshCw />刷新</el-button>
          <el-button @click="router.push(`/groups/${selected.id}/edit`)">编辑配置</el-button>
        </div>
        <el-alert v-if="selected.last_error" type="warning" title="最近刷新异常" :description="selected.last_error" :closable="false" show-icon />
        <div class="drawer-grid">
          <div><span>有效节点</span><b>{{ selected.node_count }}</b></div>
          <div><span>待确认节点</span><b :class="{ 'pending-value': selected.pending_node_count }">{{ selected.pending_node_count || 0 }}</b></div>
          <div><span>刷新耗时</span><b>{{ duration(selected.last_duration_ms) }}</b></div>
        </div>
        <section class="drawer-section">
          <header>
            <div>
              <h3>客户端订阅地址</h3>
              <p>可直接添加到客户端。</p>
            </div>
          </header>
          <div class="url-item" v-for="u in urls" :key="u.id">
            <div>
              <b>{{ store.types[u.client_type]?.label || u.client_type }}</b>
              <code>{{ u.url }}</code>
            </div>
            <el-button @click="copy(u.url)"><Copy />复制</el-button>
          </div>
        </section>
        <section v-if="statusUrl" class="drawer-section">
          <header>
            <div>
              <h3>节点状态页</h3>
              <p>免登录公开页面，展示该组节点测活状态，可分享给他人。</p>
            </div>
          </header>
          <div class="url-item">
            <div>
              <b>状态页</b>
              <code>{{ statusUrl }}</code>
            </div>
            <el-button @click="copy(statusUrl)"><Copy />复制</el-button>
          </div>
        </section>
        <section class="drawer-section">
          <header>
            <div>
              <h3>上游健康</h3>
              <p>仅展示脱敏诊断结果。</p>
            </div>
            <el-button :loading="busy === selected.id" @click="diagnose">重新诊断</el-button>
          </header>
          <div class="upstream-item" v-for="u in (diagnostics.length ? diagnostics : selected.upstreams)" :key="u.name">
            <StatusTag :status="u.status || u.last_status" />
            <div>
              <b>{{ u.name }}</b>
              <span>{{ u.source_format || '未知格式' }} · HTTP {{ u.status_code || u.last_http_status || '—' }} · {{ u.duration_ms || u.last_duration_ms || 0 }} ms</span>
            </div>
            <b>{{ u.node_count || 0 }} 节点</b>
          </div>
        </section>
        <div class="drawer-foot-actions">
          <el-button @click="duplicate(selected!)">复制组</el-button>
          <el-button @click="rotate(selected!)">重置 Token</el-button>
          <el-button type="danger" plain @click="remove(selected!)">删除组</el-button>
        </div>
      </template>
    </el-drawer>
    <QrDialog v-model="qrVisible" :title="qrTarget?.title || ''" :url="qrTarget?.url || ''" />
  </section>
</template>
