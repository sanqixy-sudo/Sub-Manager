<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { AlertTriangle, ArrowLeft, Check, Copy, Download, Edit3, FolderTree, GripVertical, QrCode, RefreshCw, Workflow } from 'lucide-vue-next'
import { api } from '../api'
import { copyText, dt, duration, importSchemes, openImportScheme, size, supportsImport } from '../utils'
import { useAppStore } from '../stores/app'
import type { Group, NodeSnapshot, OutputStatus, RefreshRun } from '../types'
import StatusTag from '../components/StatusTag.vue'
import QrDialog from '../components/QrDialog.vue'

const NODE_PAGE_SIZE = 50

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const id = ref(Number(route.params.id))
const group = ref<(Group & { recent_runs?: RefreshRun[] }) | null>(null)
const nodes = ref<NodeSnapshot[]>([])
const outputs = ref<OutputStatus[]>([])
const runs = ref<RefreshRun[]>([])
const tab = ref(typeof route.query.tab === 'string' && route.query.tab ? route.query.tab : 'overview')
const loading = ref(false)
const tabLoading = ref(false)
const refreshing = ref(false)
const nodeBusy = ref('')
const ordering = ref(false)
const orderSaving = ref(false)
const nodeSearch = ref('')
const nodeProtocol = ref('')
const nodeStatus = ref<'all' | 'pending' | 'confirmed'>('all')
const nodePage = ref(1)
const dragIndex = ref<number | null>(null)
const orderSearch = ref('')
const orderDropIndex = ref<number | null>(null)
const loaded = new Set<string>()

const enabledUpstreams = computed(() => group.value?.upstreams.filter(x => x.enabled) || [])
const pendingNodes = computed(() => nodes.value.filter(x => x.confirmation_status !== 'confirmed'))
const ipWhitelistSummary = computed(() => {
  const value = (group.value as any)?.ip_whitelist || ''
  const count = String(value).split(/\r?\n/).filter((x: string) => x.trim() && !x.trim().startsWith('#')).length
  return count ? `已启用（${count} 条）` : '未启用'
})
const nodeProtocols = computed(() => [...new Set(nodes.value.map(x => x.protocol))])
const confirmedCount = computed(() => nodes.value.length - pendingNodes.value.length)
const filteredNodes = computed(() =>
  nodes.value
    .filter(x => nodeStatus.value === 'all' || (nodeStatus.value === 'confirmed' ? x.confirmation_status === 'confirmed' : x.confirmation_status !== 'confirmed'))
    .filter(x => !nodeSearch.value || `${x.final_name} ${x.original_name} ${x.alias || ''}`.toLowerCase().includes(nodeSearch.value.toLowerCase()))
    .filter(x => !nodeProtocol.value || x.protocol === nodeProtocol.value))
const pagedNodes = computed(() => filteredNodes.value.slice((nodePage.value - 1) * NODE_PAGE_SIZE, nodePage.value * NODE_PAGE_SIZE))
// 排序模式的搜索定位：只影响显示，不改变底层 nodes 顺序
const orderingData = computed(() => {
  if (!orderSearch.value) return nodes.value
  const q = orderSearch.value.toLowerCase()
  return nodes.value.filter(x => `${x.final_name} ${x.original_name} ${x.alias || ''}`.toLowerCase().includes(q))
})
/** 排序表格中的行 → nodes 数组中的真实下标（搜索过滤后 $index 会失真） */
function realIndex(row: NodeSnapshot) {
  return nodes.value.indexOf(row)
}

async function loadGroup(initial = false) {
  if (initial) loading.value = true
  try {
    group.value = await api(`/api/subscriptions/${id.value}`)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function loadTab(name: string, force = false) {
  if (name === 'overview' || (loaded.has(name) && !force)) return
  tabLoading.value = true
  try {
    if (name === 'nodes') nodes.value = (await api<any>(`/api/subscriptions/${id.value}/nodes`)).nodes
    if (name === 'outputs') outputs.value = (await api<any>(`/api/subscriptions/${id.value}/outputs/status`)).outputs
    if (name === 'runs') runs.value = (await api<any>(`/api/subscriptions/${id.value}/runs?page_size=30`)).items
    loaded.add(name)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    tabLoading.value = false
  }
}

async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await api(`/api/subscriptions/${id.value}/refresh`, { method: 'POST' })
    ElMessage.success('刷新任务已完成')
    loaded.clear()
    await loadGroup()
    await loadTab(tab.value, true)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    refreshing.value = false
  }
}

async function updateNode(row: NodeSnapshot, alias: string | null) {
  nodeBusy.value = row.node_key
  try {
    const result: any = await api(`/api/subscriptions/${id.value}/nodes/${row.node_key}`, { method: 'PUT', body: JSON.stringify({ alias, confirm: true }) })
    nodes.value = result.nodes.nodes
    loaded.add('nodes')
    await loadGroup()
    ElMessage.success(alias ? '节点别名已保存' : '节点设置已更新')
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    nodeBusy.value = ''
  }
}

async function editAlias(row: NodeSnapshot) {
  try {
    const { value } = await ElMessageBox.prompt('固定名称支持竖线、国旗和常见符号；流量与天数仍会动态追加。', '设置节点别名', {
      inputValue: row.alias || '',
      inputPlaceholder: '例如：07|🇭🇰|香港住宅',
      inputValidator: (v: string) => (v.length <= 80 && !/[\r\n]/.test(v)) || '最多 80 字，且不能包含换行',
      confirmButtonText: '保存',
      cancelButtonText: '取消',
    })
    await updateNode(row, value.trim() || null)
  } catch (e: any) {
    if (e === 'cancel' || e === 'close') return // 用户取消，静默
    ElMessage.error(e?.message || '操作失败')
  }
}

function moveNode(index: number, delta: number) {
  const next = index + delta
  if (next < 0 || next >= nodes.value.length) return
  const copy = nodes.value.slice()
  const item = copy.splice(index, 1)[0]
  copy.splice(next, 0, item)
  nodes.value = copy
}

function pinNode(index: number) {
  if (index <= 0 || index >= nodes.value.length) return
  const copy = nodes.value.slice()
  const item = copy.splice(index, 1)[0]
  copy.unshift(item)
  nodes.value = copy
}

/** 原生 HTML5 拖拽排序：dragstart 记录源索引，drop 到目标行后移动；搜索定位时禁用拖拽 */
function onDragStart(index: number) {
  if (orderSearch.value) return
  dragIndex.value = index
}

function onDragOver(index: number) {
  if (dragIndex.value == null) return
  orderDropIndex.value = index
}

function onDrop(index: number) {
  orderDropIndex.value = null
  if (orderSearch.value) return
  if (dragIndex.value == null || dragIndex.value === index) return
  const copy = nodes.value.slice()
  const item = copy.splice(dragIndex.value, 1)[0]
  copy.splice(index, 0, item)
  nodes.value = copy
  dragIndex.value = null
}

/** 排序模式行反馈：拖拽源行与 dragover 目标行高亮（el-table 无法整行 drop，用 row-class-name 做视觉反馈） */
function orderRowClass({ row }: { row: NodeSnapshot }) {
  if (!ordering.value) return ''
  const i = realIndex(row)
  if (orderDropIndex.value === i) return 'order-drop-target'
  if (dragIndex.value === i) return 'order-dragging'
  return ''
}

/** 输入目标序号，直接把节点移动到该位置 */
async function moveNodeTo(index: number) {
  try {
    const { value } = await ElMessageBox.prompt(`输入目标序号（1 - ${nodes.value.length}），节点将移动到该位置。`, '移动到指定位置', {
      inputValue: String(index + 1),
      inputValidator: (v: string) => {
        const n = Number(v)
        return (Number.isInteger(n) && n >= 1 && n <= nodes.value.length) || `请输入 1 - ${nodes.value.length} 之间的整数`
      },
      confirmButtonText: '移动',
      cancelButtonText: '取消',
    })
    const target = Number(value) - 1
    if (target === index) return
    const copy = nodes.value.slice()
    const item = copy.splice(index, 1)[0]
    copy.splice(target, 0, item)
    nodes.value = copy
  } catch (e: any) {
    if (e === 'cancel' || e === 'close') return // 用户取消，静默
    ElMessage.error(e?.message || '操作失败')
  }
}

type SmartOrder = 'latency' | 'name' | 'source'

/** 一键智能重排：仅改本地 nodes，仍需点保存生效 */
async function smartReorder(kind: SmartOrder) {
  const names: Record<SmartOrder, string> = {
    latency: '按测活延迟升序（未测试沉底）',
    name: '按名称',
    source: '按来源',
  }
  try {
    await ElMessageBox.confirm(`将${names[kind]}重排全部节点，覆盖当前手动顺序（点击保存后生效）。确认继续？`, '智能重排', { type: 'warning' })
  } catch {
    return // 用户取消，静默
  }
  const copy = nodes.value.slice()
  if (kind === 'latency') {
    copy.sort((a, b) => (a.connectivity_latency_ms ?? Number.MAX_SAFE_INTEGER) - (b.connectivity_latency_ms ?? Number.MAX_SAFE_INTEGER))
  } else if (kind === 'name') {
    copy.sort((a, b) => a.final_name.localeCompare(b.final_name, 'zh-Hans-CN'))
  } else {
    copy.sort((a, b) => a.source_name.localeCompare(b.source_name, 'zh-Hans-CN') || a.final_name.localeCompare(b.final_name, 'zh-Hans-CN'))
  }
  nodes.value = copy
  ElMessage.success('已重排，确认无误后点击「保存顺序」')
}

async function saveNodeOrder() {
  if (!nodes.value.length) return
  orderSaving.value = true
  try {
    const result: any = await api(`/api/subscriptions/${id.value}/nodes/order`, { method: 'PUT', body: JSON.stringify({ node_keys: nodes.value.map(x => x.node_key) }) })
    nodes.value = result.nodes.nodes
    ordering.value = false
    loaded.add('nodes')
    await loadGroup()
    ElMessage.success('节点顺序已保存，下游订阅已更新')
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    orderSaving.value = false
  }
}

async function confirmAll() {
  try {
    await ElMessageBox.confirm(`将确认全部 ${pendingNodes.value.length} 个待确认节点，确认继续？`, '全部确认', { type: 'warning' })
  } catch {
    return // 用户取消，静默
  }
  nodeBusy.value = 'all'
  try {
    const result: any = await api(`/api/subscriptions/${id.value}/nodes/confirm`, { method: 'POST', body: JSON.stringify({ node_keys: null }) })
    nodes.value = result.nodes.nodes
    loaded.add('nodes')
    await loadGroup()
    ElMessage.success(`已确认 ${result.confirmed} 个节点`)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    nodeBusy.value = ''
  }
}

async function copy(v?: string) {
  if (!v) return
  await copyText(v)
  ElMessage.success('订阅地址已复制')
}

/** 一键导入：尝试唤起客户端 scheme，800ms 后页面仍可见说明未唤起，复制地址兜底 */
async function importOutput(o: OutputStatus) {
  if (!o.url) return
  const [item] = importSchemes(o.client_type, o.url)
  if (!item) return
  const opened = await openImportScheme(item.scheme)
  if (!opened) {
    await copyText(o.url)
    ElMessage.warning('未检测到客户端，地址已复制，可手动粘贴')
  }
}

/** 扫码导入：输出卡地址生成二维码弹层（不限 client_type，扫码后客户端手动粘贴也成立） */
const qrVisible = ref(false)
const qrTarget = ref<{ title: string; url: string } | null>(null)

function openQrCode(o: OutputStatus) {
  if (!o.url) return
  qrTarget.value = { title: o.name, url: o.url }
  qrVisible.value = true
}

watch(tab, name => {
  loadTab(name)
  if (route.query.tab !== name) router.replace({ query: { ...route.query, tab: name } }).catch(() => {})
})

watch([nodeSearch, nodeProtocol, nodeStatus], () => {
  nodePage.value = 1
})

// 退出排序模式时清空搜索定位与拖拽状态
watch(ordering, v => {
  if (!v) {
    orderSearch.value = ''
    dragIndex.value = null
    orderDropIndex.value = null
  }
})

// 同组件切换到其他组 id 时，重置缓存并重新加载
watch(() => route.params.id, async nv => {
  const nid = Number(nv)
  if (!nid || nid === id.value) return
  id.value = nid
  loaded.clear()
  group.value = null
  nodes.value = []
  outputs.value = []
  runs.value = []
  ordering.value = false
  orderSearch.value = ''
  nodePage.value = 1
  await Promise.all([loadGroup(true), loadTab(tab.value, true)])
})

onMounted(async () => {
  // 测活页「管理」跳转带 search 参数时，用它初始化节点搜索（仅初始化一次，不回写 URL）
  if (typeof route.query.search === 'string' && route.query.search) nodeSearch.value = route.query.search
  await Promise.all([loadGroup(true), app.settings ? Promise.resolve() : app.bootstrap()])
  if (tab.value !== 'overview') loadTab(tab.value)
})
</script>

<template>
  <section class="page group-detail-page">
    <header class="page-head">
      <div>
        <button class="back" @click="router.push('/groups')"><ArrowLeft /></button>
        <p class="eyebrow">GROUP OPERATIONS</p>
        <h1>{{ group?.name || '订阅组详情' }}</h1>
        <p>{{ group?.note || '查看来源、节点、输出和运行记录。' }}</p>
      </div>
      <div class="head-actions">
        <el-button @click="router.push(`/groups/${id}/categories`)"><FolderTree />分类管理</el-button>
        <el-button @click="router.push(`/groups/${id}/edit`)"><Edit3 />编辑配置</el-button>
        <el-button type="primary" :loading="refreshing" @click="refresh"><RefreshCw />立即刷新</el-button>
      </div>
    </header>
    <el-skeleton v-if="loading && !group" :rows="8" animated />
    <template v-else-if="group">
      <div class="detail-summary">
        <StatusTag :status="group.last_refresh_status" />
        <span>缓存 <b>{{ group.cache_state === 'fresh' ? '新鲜' : group.cache_state === 'stale' ? '旧缓存' : '暂无' }}</b></span>
        <span>最近成功 <b>{{ dt(group.last_success_at) }}</b></span>
        <span>下次刷新 <b>{{ dt(group.next_refresh_at) }}</b></span>
        <span v-if="group.pending_node_count" class="pending-summary"><AlertTriangle />{{ group.pending_node_count }} 个节点待确认</span>
      </div>
      <el-tabs v-model="tab" class="detail-tabs">
        <el-tab-pane label="概览" name="overview">
          <div class="stats">
            <div class="stat"><div><b>{{ group.node_count }}</b><span>有效节点</span></div></div>
            <div class="stat"><div><b>{{ group.filtered_count }}</b><span>过滤/去重</span></div></div>
            <div class="stat"><div><b>{{ enabledUpstreams.filter(x => x.last_status === 'ok').length }} / {{ enabledUpstreams.length }}</b><span>健康上游</span></div></div>
            <div class="stat"><div><b>{{ duration(group.last_duration_ms) }}</b><span>刷新耗时</span></div></div>
          </div>
          <el-alert v-if="group.last_error" type="warning" title="最近异常" :description="group.last_error" :closable="false" show-icon />
          <div v-if="group.pending_node_count" class="pending-banner panel">
            <AlertTriangle />
            <div>
              <b>{{ group.pending_node_count }} 个节点需要确认</b>
              <span>新增节点或上游固定名称发生变化，下游暂时带有 ⚠️ 标记。</span>
            </div>
            <el-button type="warning" plain @click="tab = 'nodes'">前往处理</el-button>
          </div>
          <div v-else class="healthy-banner panel">
            <Check />
            <div>
              <b>全部正常</b>
              <span>没有异常状态或待确认节点。</span>
            </div>
          </div>
          <div class="detail-overview-grid">
            <section class="panel overview-card">
              <header>
                <div>
                  <h3>上游健康</h3>
                  <span>最近一次拉取结果</span>
                </div>
                <button @click="tab = 'upstreams'">查看全部</button>
              </header>
              <div class="overview-rows">
                <div v-for="u in enabledUpstreams.slice(0, 5)" :key="u.id">
                  <StatusTag :status="u.last_status" />
                  <span><b>{{ u.name }}</b><small>{{ u.source_format || '未知格式' }} · {{ u.node_count || 0 }} 节点</small></span>
                  <time>{{ duration(u.last_duration_ms) }}</time>
                </div>
              </div>
            </section>
            <section class="panel overview-card">
              <header>
                <div>
                  <h3>输出客户端</h3>
                  <span>{{ group.outputs.filter(x => x.enabled).length }} 个固定地址</span>
                </div>
                <button @click="tab = 'outputs'">查看地址</button>
              </header>
              <div class="overview-rows">
                <div v-for="o in group.outputs.filter(x => x.enabled).slice(0, 5)" :key="o.id">
                  <Workflow />
                  <span><b>{{ o.name }}</b><small>{{ app.types[o.client_type]?.label || o.client_type }}</small></span>
                  <i class="healthy-dot">可用</i>
                </div>
              </div>
            </section>
          </div>
          <section class="panel overview-card overview-runs">
            <header>
              <div>
                <h3>最近刷新</h3>
                <span>最近 5 次真实刷新任务</span>
              </div>
              <button @click="tab = 'runs'">完整记录</button>
            </header>
            <div v-if="group.recent_runs?.length" class="run-strip">
              <div v-for="r in group.recent_runs" :key="r.id">
                <StatusTag :status="r.status" />
                <span>{{ r.trigger === 'scheduler' ? '定时' : r.trigger === 'manual' ? '手动' : '公共请求' }}</span>
                <b>{{ r.node_count }} 节点</b>
                <span>{{ duration(r.duration_ms) }}</span>
                <time>{{ dt(r.finished_at) }}</time>
              </div>
            </div>
            <div v-else class="mini-empty">暂无刷新记录</div>
          </section>
          <div class="panel overview-info">
            <div><span>自动刷新周期</span><b>{{ group.interval_minutes }} 分钟</b></div>
            <div><span>刷新次数</span><b>{{ group.refresh_count }}</b></div>
            <div><span>改名模式</span><b>{{ group.rename_mode === 'smart' ? '智能规则' : '原样透传' }}</b></div>
            <div><span>IP 白名单</span><b>{{ ipWhitelistSummary }}</b></div>
            <div><span>配置状态</span><b>{{ group.enabled ? '已启用' : '已停用' }}</b></div>
          </div>
        </el-tab-pane>
        <el-tab-pane label="上游" name="upstreams">
          <div class="panel">
            <el-table :data="group.upstreams">
              <el-table-column prop="name" label="上游" min-width="170" />
              <el-table-column label="改名规则" width="120">
                <template #default="{ row }">{{ row.rename_policy === 'inherit' ? '继承组规则' : row.rename_policy === 'smart' ? '独立规则' : '原样透传' }}</template>
              </el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="{ row }"><StatusTag :status="row.last_status" /></template>
              </el-table-column>
              <el-table-column prop="source_format" label="格式" width="120" />
              <el-table-column prop="last_http_status" label="HTTP" width="80" />
              <el-table-column prop="node_count" label="节点" width="80" />
              <el-table-column label="大小" width="100">
                <template #default="{ row }">{{ size(row.last_bytes || 0) }}</template>
              </el-table-column>
              <el-table-column label="耗时" width="100">
                <template #default="{ row }">{{ duration(row.last_duration_ms) }}</template>
              </el-table-column>
              <el-table-column label="最近成功" min-width="175">
                <template #default="{ row }">{{ dt(row.last_success_at) }}</template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
        <el-tab-pane :label="`节点${group.pending_node_count ? ` (${group.pending_node_count})` : ''}`" name="nodes">
          <div v-loading="tabLoading" class="panel node-panel">
            <div class="section-toolbar">
              <span>最后有效的脱敏节点快照，共 {{ nodes.length }} 条</span>
              <div class="node-order-actions">
                <el-button v-if="nodes.length" plain @click="ordering = !ordering">{{ ordering ? '完成调整' : '调整顺序' }}</el-button>
                <el-button v-if="ordering" type="primary" :loading="orderSaving" @click="saveNodeOrder">保存顺序</el-button>
                <el-button v-if="pendingNodes.length" type="warning" plain :loading="nodeBusy === 'all'" @click="confirmAll"><Check />全部确认</el-button>
              </div>
            </div>
            <div v-if="!ordering" class="node-filter-bar">
              <el-input v-model="nodeSearch" placeholder="按节点名称搜索" clearable />
              <el-select v-model="nodeProtocol" placeholder="全部协议" clearable>
                <el-option v-for="p in nodeProtocols" :key="p" :label="p.toUpperCase()" :value="p" />
              </el-select>
              <div class="node-status-chips">
                <button :class="{ active: nodeStatus === 'all' }" @click="nodeStatus = 'all'">全部 {{ nodes.length }}</button>
                <button :class="{ active: nodeStatus === 'pending' }" @click="nodeStatus = 'pending'">待确认 {{ pendingNodes.length }}</button>
                <button :class="{ active: nodeStatus === 'confirmed' }" @click="nodeStatus = 'confirmed'">已确认 {{ confirmedCount }}</button>
              </div>
              <span class="muted">匹配 {{ filteredNodes.length }} 条</span>
            </div>
            <template v-if="ordering">
              <div class="ordering-bar">
                <el-input v-model="orderSearch" placeholder="搜索节点名称，快速定位" clearable />
                <el-dropdown trigger="click" @command="smartReorder">
                  <el-button plain>智能重排</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="latency">按测活延迟升序（未测试沉底）</el-dropdown-item>
                      <el-dropdown-item command="name">按名称</el-dropdown-item>
                      <el-dropdown-item command="source">按来源</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
                <span class="muted">显示 {{ orderingData.length }} / {{ nodes.length }} 条</span>
              </div>
              <p class="ordering-tip">{{ orderSearch ? '搜索定位时不能拖拽，可使用置顶 / 上移 / 下移 / 移动到调整顺序。' : '拖动左侧手柄，或使用置顶 / 上移 / 下移 / 移动到调整顺序，完成后记得保存。' }}</p>
            </template>
            <el-table
              :data="ordering ? orderingData : pagedNodes"
              :max-height="ordering ? 620 : undefined"
              :row-class-name="orderRowClass"
              empty-text="刷新成功后将在此生成脱敏节点快照"
            >
              <el-table-column v-if="ordering" label="调整" width="300">
                <template #default="{ row }">
                  <div class="node-reorder-cell" @dragover.prevent="onDragOver(realIndex(row))" @drop="onDrop(realIndex(row))">
                    <span
                      class="drag-handle"
                      :class="{ disabled: !!orderSearch }"
                      :draggable="!orderSearch"
                      :title="orderSearch ? '搜索时不能拖拽' : '拖拽调整顺序'"
                      @dragstart="onDragStart(realIndex(row))"
                      @dragend="dragIndex = null; orderDropIndex = null"
                    >
                      <GripVertical />
                    </span>
                    <el-button link :disabled="realIndex(row) === 0" @click="pinNode(realIndex(row))">置顶</el-button>
                    <el-button link :disabled="realIndex(row) === 0" @click="moveNode(realIndex(row), -1)">上移</el-button>
                    <el-button link :disabled="realIndex(row) === nodes.length - 1" @click="moveNode(realIndex(row), 1)">下移</el-button>
                    <el-button link @click="moveNodeTo(realIndex(row))">移动到</el-button>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="#" width="52">
                <template #default="{ $index, row }">{{ ordering ? realIndex(row) + 1 : (nodePage - 1) * NODE_PAGE_SIZE + $index + 1 }}</template>
              </el-table-column>
              <el-table-column prop="source_name" label="来源" width="125" show-overflow-tooltip />
              <el-table-column prop="original_name" label="原名" min-width="190" show-overflow-tooltip class-name="hide-narrow" label-class-name="hide-narrow" />
              <el-table-column prop="rule_name" label="规则结果" min-width="180" show-overflow-tooltip class-name="hide-narrow" label-class-name="hide-narrow" />
              <el-table-column prop="alias" label="自定义名称" min-width="150">
                <template #default="{ row }">{{ row.alias || '—' }}</template>
              </el-table-column>
              <el-table-column label="最终下游名称" min-width="200">
                <template #default="{ row }">
                  <div class="final-name-cell">
                    <span>{{ row.final_name }}</span>
                    <small>原名：{{ row.original_name }}</small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="测活" width="110">
                <template #default="{ row }">
                  <span class="node-state" :class="row.health_status">{{ row.health_status === 'healthy' ? (row.connectivity_latency_ms ? row.connectivity_latency_ms + ' ms' : '正常') : row.health_status === 'unavailable' ? '不可用' : row.health_status === 'google_blocked' ? 'Google 不通' : '未测试' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="118">
                <template #default="{ row }">
                  <span class="node-state" :class="row.confirmation_status">{{ row.confirmation_status === 'confirmed' ? '已确认' : row.confirmation_status === 'pending_new' ? '新增待确认' : '名称变化' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="180" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" :loading="nodeBusy === row.node_key" @click="editAlias(row)">改名</el-button>
                  <el-button v-if="row.confirmation_status !== 'confirmed'" link type="warning" @click="updateNode(row, row.alias || null)">确认</el-button>
                  <el-button v-if="row.alias" link @click="updateNode(row, null)">清除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <div v-if="!ordering && filteredNodes.length > NODE_PAGE_SIZE" class="node-pagination">
              <el-pagination
                background
                layout="total, prev, pager, next"
                :total="filteredNodes.length"
                v-model:current-page="nodePage"
                :page-size="NODE_PAGE_SIZE"
              />
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane label="输出" name="outputs">
          <div v-loading="tabLoading" class="output-status-grid">
            <article v-for="o in outputs" :key="o.id" class="panel output-status-card">
              <header>
                <div>
                  <b>{{ o.name }}</b>
                  <span>{{ app.types[o.client_type]?.label || o.client_type }}</span>
                </div>
                <span class="cache" :class="o.status === 'ready' ? 'fresh' : ''">{{ o.status === 'ready' ? '已生成' : '暂无缓存' }}</span>
              </header>
              <div class="output-meta">
                <span>更新时间<b>{{ dt(o.updated_at) }}</b></span>
                <span>大小<b>{{ size(o.bytes) }}</b></span>
                <span>渲染器<b>{{ o.renderer || '—' }}</b></span>
                <span>跳过节点<b>{{ o.skipped_nodes }}</b></span>
              </div>
              <div class="output-actions">
                <el-button :disabled="!o.url" @click="copy(o.url)"><Copy />复制固定地址</el-button>
                <el-button v-if="supportsImport(o.client_type)" :disabled="!o.url" @click="importOutput(o)"><Download />导入</el-button>
                <el-button :disabled="!o.url" @click="openQrCode(o)"><QrCode />二维码</el-button>
              </div>
            </article>
          </div>
        </el-tab-pane>
        <el-tab-pane label="刷新记录" name="runs">
          <div v-loading="tabLoading" class="panel">
            <el-table :data="runs" empty-text="暂无刷新记录">
              <el-table-column label="状态" width="110">
                <template #default="{ row }"><StatusTag :status="row.status" /></template>
              </el-table-column>
              <el-table-column label="来源" width="100">
                <template #default="{ row }">{{ row.trigger === 'manual' ? '手动' : row.trigger === 'scheduler' ? '定时' : '公共请求' }}</template>
              </el-table-column>
              <el-table-column label="上游" width="90">
                <template #default="{ row }">{{ row.upstream_success }}/{{ row.upstream_total }}</template>
              </el-table-column>
              <el-table-column label="输出" width="90">
                <template #default="{ row }">{{ row.output_success }}/{{ row.output_total }}</template>
              </el-table-column>
              <el-table-column prop="node_count" label="节点" width="80" />
              <el-table-column label="耗时" width="100">
                <template #default="{ row }">{{ duration(row.duration_ms) }}</template>
              </el-table-column>
              <el-table-column label="时间" min-width="170">
                <template #default="{ row }">{{ dt(row.finished_at) }}</template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
        <el-tab-pane label="配置" name="config">
          <div class="panel config-jump">
            <Edit3 />
            <h3>配置在独立页面中编辑</h3>
            <p>避免运维数据和敏感上游地址混在同一个视图。</p>
            <el-button type="primary" @click="router.push(`/groups/${id}/edit`)">进入配置编辑</el-button>
          </div>
        </el-tab-pane>
      </el-tabs>
    </template>
    <QrDialog v-model="qrVisible" :title="qrTarget?.title || ''" :url="qrTarget?.url || ''" />
  </section>
</template>
