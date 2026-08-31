<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  ChevronsUp,
  FolderTree,
  GripVertical,
  Plus,
  Save,
  Trash2,
} from 'lucide-vue-next'
import { api } from '../api'
import type { Group, NodeSnapshot } from '../types'

type Category = {
  id?: number
  name: string
  icon: string
  sort_order?: number
  source_ids: number[]
  node_keys: string[]
}

const route = useRoute()
const router = useRouter()
const id = Number(route.params.id)
const group = ref<Group | null>(null)
const nodes = ref<NodeSnapshot[]>([])
const items = ref<Category[]>([])
const selected = ref(0)
const saving = ref(false)
const sorting = ref(false)
const query = ref('')
const protocol = ref('')
const loadError = ref('')
const catDragIndex = ref<number | null>(null)
const catDropIndex = ref<number | null>(null)

const CANDIDATE_PAGE_SIZE = 50
const candidatePage = ref(1)

const current = computed(() => items.value[selected.value])
// 存在未保存的新分类时禁止排序（拖拽与上下移均禁用，tooltip 说明原因）
const hasUnsavedNew = computed(() => items.value.some((x) => !x.id))
// 已选区：按 node_keys 顺序列出对应节点（key 已失效的节点跳过显示，但计数仍以 node_keys 为准）
const selectedNodes = computed(() => {
  if (!current.value) return [] as NodeSnapshot[]
  const byKey = new Map(nodes.value.map((n) => [n.node_key, n]))
  return current.value.node_keys
    .map((k) => byKey.get(k))
    .filter((n): n is NodeSnapshot => !!n)
})
// 候选区：搜索 + 协议筛选，保持稳定顺序（勾选行用「已加入」标记高亮，不重排）
const filtered = computed(() =>
  nodes.value.filter(
    (n) =>
      (!query.value ||
        n.final_name.toLowerCase().includes(query.value.toLowerCase()) ||
        n.source_name.toLowerCase().includes(query.value.toLowerCase())) &&
      (!protocol.value || n.protocol === protocol.value),
  ),
)
const pagedCandidates = computed(() =>
  filtered.value.slice((candidatePage.value - 1) * CANDIDATE_PAGE_SIZE, candidatePage.value * CANDIDATE_PAGE_SIZE),
)
const protocols = computed(() => [...new Set(nodes.value.map((x) => x.protocol))])

watch([query, protocol, selected], () => {
  candidatePage.value = 1
})

// 脏状态：加载/保存成功后拍快照，勾选、排序、改名都会使当前分类变脏
let snapshot = ''
function takeSnapshot() {
  snapshot = JSON.stringify(items.value)
}
const dirty = computed(() => !!snapshot && JSON.stringify(items.value) !== snapshot)

async function load() {
  try {
    group.value = await api(`/api/subscriptions/${id}`)
    nodes.value = (await api<any>(`/api/subscriptions/${id}/nodes`)).nodes
    items.value = (await api<any>(`/api/subscriptions/${id}/categories`)).categories
    if (!items.value.length) add()
    loadError.value = ''
    takeSnapshot()
  } catch (e: any) {
    loadError.value = e.message || '加载失败'
    ElMessage.error(loadError.value)
  }
}

function add() {
  items.value.push({
    name: `分类 ${items.value.length + 1}`,
    icon: '',
    source_ids: [],
    node_keys: [],
  })
  selected.value = items.value.length - 1
}

function has(key: string) {
  return current.value?.node_keys.includes(key)
}

function toggle(key: string, on: unknown) {
  const list = current.value.node_keys
  if (!!on && !list.includes(key)) list.push(key)
  if (!on) current.value.node_keys = list.filter((x) => x !== key)
}

async function save() {
  if (!current.value) return
  saving.value = true
  try {
    const path = current.value.id
      ? `/api/subscriptions/${id}/categories/${current.value.id}`
      : `/api/subscriptions/${id}/categories`
    const result: any = await api(path, {
      method: current.value.id ? 'PUT' : 'POST',
      body: JSON.stringify(current.value),
    })
    Object.assign(current.value, result.category)
    takeSnapshot()
    ElMessage.success('分类已保存')
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function remove() {
  if (!current.value) return
  if (!current.value.id) {
    // 未保存的新分类只存在于本地，删除前同样确认
    try {
      await ElMessageBox.confirm(
        `删除未保存的分类“${current.value.name}”？`,
        '删除分类',
        { type: 'warning' },
      )
    } catch {
      return
    }
    items.value.splice(selected.value, 1)
    selected.value = Math.max(0, selected.value - 1)
    takeSnapshot()
    return
  }
  try {
    await ElMessageBox.confirm(`删除分类“${current.value.name}”？不会删除节点。`, '删除分类', {
      type: 'warning',
    })
    await api(`/api/subscriptions/${id}/categories/${current.value.id}`, { method: 'DELETE' })
    items.value.splice(selected.value, 1)
    selected.value = Math.max(0, selected.value - 1)
    takeSnapshot()
  } catch {}
}

async function reorderCategories(next: number[]) {
  if (items.value.some((x) => !x.id)) return
  sorting.value = true
  const active = current.value
  try {
    const result: any = await api(`/api/subscriptions/${id}/categories/reorder`, {
      method: 'PUT',
      body: JSON.stringify(next),
    })
    items.value = result.categories
    selected.value = Math.max(
      0,
      items.value.findIndex((x) => x.id === active?.id),
    )
    takeSnapshot() // 排序立即生效，服务端已是最新状态
    ElMessage.success('分类顺序已保存，下次刷新同步到下游')
  } catch (e: any) {
    ElMessage.error(e.message)
    await load()
  } finally {
    sorting.value = false
  }
}

async function moveCategory(index: number, delta: number) {
  const next = index + delta
  if (next < 0 || next >= items.value.length || !items.value[index]?.id) return
  if (hasUnsavedNew.value) return
  const ids = items.value.map((x) => x.id as number)
  ;[ids[index], ids[next]] = [ids[next], ids[index]]
  await reorderCategories(ids)
}

/** 分类原生拖拽排序：拖到目标条目上松手，立即走 reorderCategories 保存 */
function onCatDragStart(index: number) {
  if (hasUnsavedNew.value) return
  catDragIndex.value = index
}

function onCatDragOver(index: number) {
  if (catDragIndex.value == null) return
  catDropIndex.value = index
}

function onCatDragEnd() {
  catDragIndex.value = null
  catDropIndex.value = null
}

async function onCatDrop(index: number) {
  const from = catDragIndex.value
  onCatDragEnd()
  if (from == null || from === index || hasUnsavedNew.value) return
  const ids = items.value.map((x) => x.id as number)
  const item = ids.splice(from, 1)[0]
  ids.splice(index, 0, item)
  await reorderCategories(ids)
}

function moveNodeInCategory(key: string, delta: number) {
  if (!current.value) return
  const index = current.value.node_keys.indexOf(key)
  const next = index + delta
  if (index < 0 || next < 0 || next >= current.value.node_keys.length) return
  const keys = current.value.node_keys.slice()
  ;[keys[index], keys[next]] = [keys[next], keys[index]]
  current.value.node_keys = keys
}

function moveNodeToTop(key: string) {
  if (!current.value) return
  const index = current.value.node_keys.indexOf(key)
  if (index <= 0) return
  const keys = current.value.node_keys.slice()
  keys.splice(index, 1)
  keys.unshift(key)
  current.value.node_keys = keys
}

onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm(
      '当前分类有未保存的修改，确定要离开吗？',
      '未保存的修改',
      { type: 'warning', confirmButtonText: '离开', cancelButtonText: '继续编辑' },
    )
    return true
  } catch {
    return false
  }
})

// 脏状态下关闭/刷新标签页时由浏览器原生提示拦截
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (dirty.value) e.preventDefault()
}

onMounted(load)
onMounted(() => window.addEventListener('beforeunload', onBeforeUnload))
onUnmounted(() => window.removeEventListener('beforeunload', onBeforeUnload))
</script>

<template>
  <section class="page category-page">
    <header class="page-head">
      <div>
        <button class="back" @click="router.back()"><ArrowLeft /></button>
        <p class="eyebrow">PROXY CATEGORIES</p>
        <h1>{{ group?.name || '订阅组' }} · 分类管理</h1>
        <p>整条订阅来源自动归类，或精确分配单个远程/手动节点；拖拽或使用上下按钮调整分类和节点下游顺序。</p>
      </div>
      <div class="head-actions">
        <span v-if="dirty" class="dirty-hint">有未保存修改</span>
        <el-button @click="add"><Plus />新建分类</el-button>
        <el-button type="primary" :class="{ 'save-dirty': dirty }" :loading="saving" @click="save"><Save />保存当前分类</el-button>
      </div>
    </header>
    <p class="category-note">
      左侧分类的拖拽排序与上移/下移立即生效；右侧的图标、名称、来源与节点勾选需要点击「保存当前分类」后才会写入。
    </p>
    <div v-if="loadError" class="panel category-load-error">
      <p>{{ loadError }}</p>
      <el-button @click="load">重试</el-button>
    </div>
    <div v-else class="category-layout">
      <aside v-loading="sorting" class="panel category-list">
        <article
          v-for="(c, i) in items"
          :key="c.id || i"
          :class="{ active: selected === i, dragging: catDragIndex === i, 'drop-target': catDropIndex === i && catDragIndex !== i }"
          :draggable="!hasUnsavedNew"
          @click="selected = i"
          @dragstart="onCatDragStart(i)"
          @dragover.prevent="onCatDragOver(i)"
          @dragend="onCatDragEnd"
          @drop.prevent="onCatDrop(i)"
        >
          <el-tooltip content="先保存或删除未保存的新分类" :disabled="!hasUnsavedNew" placement="top">
            <div class="category-move">
              <GripVertical class="drag-handle" />
              <button
                :disabled="i === 0 || !c.id || hasUnsavedNew"
                title="上移"
                @click.stop="moveCategory(i, -1)"
              >
                <ArrowUp />
              </button>
              <button
                :disabled="i === items.length - 1 || !c.id || hasUnsavedNew"
                title="下移"
                @click.stop="moveCategory(i, 1)"
              >
                <ArrowDown />
              </button>
            </div>
          </el-tooltip>
          <span>{{ c.icon || '📁' }}</span>
          <div>
            <b>{{ c.name }}</b>
            <small>{{ c.source_ids.length }} 个来源 · {{ c.node_keys.length }} 个单独节点</small>
          </div>
        </article>
        <div class="category-help">
          <FolderTree />
          <b>下游结构</b>
          <span>PROXY → 排序后的自定义分类 / 自动选择 / DIRECT / 全部真实节点</span>
        </div>
      </aside>
      <main v-if="current" class="panel category-editor">
        <div class="category-title">
          <label>
            图标
            <el-input v-model="current.icon" maxlength="4" placeholder="🇺🇸" />
            <small class="field-help">单个 emoji 或 1-2 个字符</small>
          </label>
          <label>分类名称<el-input v-model="current.name" maxlength="40" /></label>
          <el-button type="danger" plain @click="remove"><Trash2 />删除</el-button>
        </div>
        <section>
          <h3>整条订阅来源</h3>
          <p>勾选后，这条链接以后新增的节点也会自动进入当前分类。</p>
          <el-checkbox-group v-model="current.source_ids" class="source-checks">
            <el-checkbox v-for="u in group?.upstreams || []" :key="u.id" :value="u.id" border>
              {{ u.name }} <small>{{ u.node_count || 0 }} 节点</small>
            </el-checkbox>
          </el-checkbox-group>
        </section>
        <section>
          <h3>已选节点（{{ current.node_keys.length }}）</h3>
          <p>按此顺序下发到下游，可置顶 / 上移 / 下移调整，或移出分类。</p>
          <div v-if="selectedNodes.length" class="cat-selected-list">
            <div v-for="n in selectedNodes" :key="n.node_key" class="cat-selected-row">
              <span>
                <b>{{ n.final_name }}</b>
                <small>{{ n.source_name }} · {{ n.protocol.toUpperCase() }}</small>
              </span>
              <div class="node-move">
                <button
                  :disabled="current.node_keys.indexOf(n.node_key) === 0"
                  title="置顶"
                  @click="moveNodeToTop(n.node_key)"
                >
                  <ChevronsUp />
                </button>
                <button
                  :disabled="current.node_keys.indexOf(n.node_key) === 0"
                  title="上移"
                  @click="moveNodeInCategory(n.node_key, -1)"
                >
                  <ArrowUp />
                </button>
                <button
                  :disabled="current.node_keys.indexOf(n.node_key) === current.node_keys.length - 1"
                  title="下移"
                  @click="moveNodeInCategory(n.node_key, 1)"
                >
                  <ArrowDown />
                </button>
                <button title="移出分类" @click="toggle(n.node_key, false)">
                  <Trash2 />
                </button>
              </div>
            </div>
          </div>
          <p v-else class="cat-empty">尚未选择节点，从下方候选区勾选加入。</p>
        </section>
        <section>
          <div class="node-filter">
            <div>
              <h3>候选节点</h3>
              <p>勾选即加入已选区末尾，取消勾选即移出；节点绑定稳定身份，流量与天数变化不会脱离分类。</p>
            </div>
            <el-input v-model="query" placeholder="搜索节点或来源" clearable />
            <el-select v-model="protocol" placeholder="全部协议" clearable>
              <el-option v-for="p in protocols" :key="p" :label="p.toUpperCase()" :value="p" />
            </el-select>
          </div>
          <div class="category-nodes">
            <label v-for="n in pagedCandidates" :key="n.node_key" :class="{ joined: has(n.node_key) }">
              <el-checkbox :model-value="has(n.node_key)" @change="toggle(n.node_key, $event)" />
              <span>
                <b>{{ n.final_name }}</b>
                <small>{{ n.source_name }} · {{ n.protocol.toUpperCase() }}</small>
              </span>
              <em v-if="has(n.node_key)" class="joined-badge">已加入</em>
            </label>
            <p v-if="!filtered.length" class="cat-empty">没有匹配的节点。</p>
          </div>
          <div v-if="filtered.length > CANDIDATE_PAGE_SIZE" class="candidate-pagination">
            <el-pagination
              background
              layout="total, prev, pager, next"
              :total="filtered.length"
              v-model:current-page="candidatePage"
              :page-size="CANDIDATE_PAGE_SIZE"
            />
          </div>
        </section>
      </main>
    </div>
  </section>
</template>
