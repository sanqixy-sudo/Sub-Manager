<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Database, FolderTree, Plus, Save, Trash2, Upload } from 'lucide-vue-next'
import { api } from '../api'
import { useAppStore } from '../stores/app'
import { parseBulkUpstreams } from '../utils'
import type { ManualNode, Output, Upstream } from '../types'

const TEMPLATE = '{index}|{flag}|{name}|{traffic}|{reset}'
const route = useRoute()
const router = useRouter()
const store = useAppStore()
const saving = ref(false)
const manualContent = ref('')
const manualNodes = ref<ManualNode[]>([])
const manualEditNode = ref<ManualNode | null>(null)
const manualEditContent = ref('')
const manualEditVisible = ref(false)
const manualEditSaving = ref(false)
const bulk = ref('')
const editing = computed(() => !!route.params.id)

const model = reactive({
  name: '我的订阅组',
  note: '',
  interval_minutes: 30,
  enabled: true,
  rename_mode: 'passthrough' as 'passthrough' | 'smart',
  rename_ignore: '',
  rename_template: TEMPLATE,
  ip_whitelist: '',
  upstreams: [] as Upstream[],
  outputs: [
    { client_type: 'mihomo', name: '我的订阅', slug: 'mihomo', update_interval_minutes: 60, enabled: true },
  ] as Output[],
})

// 表单区块引用，校验失败时滚动定位；同时供顶部步骤条点击跳转与可视区块高亮
const basicSection = ref<HTMLElement | null>(null)
const upstreamSection = ref<HTMLElement | null>(null)
const manualSection = ref<HTMLElement | null>(null)
const renameSection = ref<HTMLElement | null>(null)
const outputSection = ref<HTMLElement | null>(null)

// P1：可点击步骤条，点击平滑滚动到对应区块，IntersectionObserver 高亮当前可视区块
const steps = [
  { label: '01 基本信息', target: basicSection },
  { label: '02 远程订阅链接', target: upstreamSection },
  { label: '03 手动节点', target: manualSection },
  { label: '04 订阅组默认改名', target: renameSection },
  { label: '05 客户端输出', target: outputSection },
]
const activeStep = ref(0)

function goStep(index: number) {
  activeStep.value = index
  steps[index].target.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

let stepObserver: IntersectionObserver | undefined

function setupStepObserver() {
  stepObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue
        const index = steps.findIndex((s) => s.target.value === entry.target)
        if (index >= 0) activeStep.value = index
      }
    },
    { rootMargin: '-30% 0px -60% 0px' },
  )
  for (const s of steps) {
    if (s.target.value) stepObserver.observe(s.target.value)
  }
}

// 脏状态：数据加载完成后拍快照，之后与实时值对比
let snapshot = ''
function takeSnapshot() {
  snapshot = JSON.stringify({ model, manualContent: manualContent.value })
}
const dirty = computed(
  () => !!snapshot && JSON.stringify({ model, manualContent: manualContent.value }) !== snapshot,
)

// 手动节点随整组保存才生效；Clash YAML 多行单节点时按列表项计数，
// base64 订阅内容先解码再按行计，其余按非空行数估算
const pendingManualCount = computed(() => {
  const text = manualContent.value
  if (!text.trim()) return 0
  if (/(^|\n)\s*proxies\s*:/.test(text)) {
    return text.split(/\r?\n/).filter((x) => /^\s*-\s*(\{|name\s*:)/.test(x)).length
  }
  try {
    const decoded = atob(text.replace(/\s/g, ''))
    const lines = decoded.split(/\r?\n/).filter((x) => x.trim())
    if (lines.length) return lines.length
  } catch {
    // 非 base64 内容，退化为按行估算
  }
  return text.split(/\r?\n/).filter((x) => x.trim()).length
})

function blankUpstream(): Upstream {
  return {
    name: `订阅链接 ${model.upstreams.length + 1}`,
    url: '',
    enabled: true,
    rename_policy: 'inherit',
    rename_ignore: '',
    rename_template: TEMPLATE,
    last_bytes: 0,
    node_count: 0,
    filtered_count: 0,
    used_cache: false,
  }
}

function addUpstream() {
  model.upstreams.push(blankUpstream())
}

function addOutput() {
  const type = store.settings?.default_client_type || 'mihomo'
  let slug = type
  let n = 2
  while (model.outputs.some((x) => x.slug === slug)) slug = type + n++
  model.outputs.push({
    client_type: type,
    name: model.name,
    slug,
    update_interval_minutes: 60,
    enabled: true,
  })
}

// 批量粘贴：每行一个 URL 或 “名称|URL”
function parseBulk() {
  const parsed = parseBulkUpstreams(bulk.value, model.upstreams.length + 1)
  if (!parsed.length) {
    ElMessage.warning('没有解析到有效链接，每行应为一个 http(s) URL 或 “名称|URL”')
    return
  }
  for (const item of parsed) {
    model.upstreams.push({ ...blankUpstream(), name: item.name, url: item.url, enabled: true })
  }
  bulk.value = ''
  ElMessage.success(`已加入 ${parsed.length} 条订阅链接`)
}

async function removeUpstream(index: number) {
  const target = model.upstreams[index]
  try {
    await ElMessageBox.confirm(
      `删除订阅链接“${target.name || `第 ${index + 1} 条`}”？`,
      '删除订阅链接',
      { type: 'warning' },
    )
  } catch {
    return
  }
  model.upstreams.splice(index, 1)
}

async function removeOutput(index: number) {
  const target = model.outputs[index]
  try {
    await ElMessageBox.confirm(
      `删除输出“${target.name || target.slug || `第 ${index + 1} 个`}”？`,
      '删除输出',
      { type: 'warning' },
    )
  } catch {
    return
  }
  model.outputs.splice(index, 1)
}

// 脏状态下关闭/刷新标签页时由浏览器原生提示拦截
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (dirty.value) e.preventDefault()
}

onMounted(async () => {
  window.addEventListener('beforeunload', onBeforeUnload)
  if (!store.settings) await store.load()
  if (editing.value) {
    const data: any = await api(`/api/subscriptions/${route.params.id}`)
    Object.assign(model, data)
    for (const upstream of model.upstreams) {
      // 旧数据的 passthrough 归入“不改名”档位，保证下拉框有匹配项
      if (upstream.rename_policy === 'passthrough') upstream.rename_policy = 'disabled'
    }
    manualNodes.value = data.manual_nodes || []
  } else {
    model.interval_minutes = store.settings!.default_refresh_interval_minutes
    model.outputs[0].client_type = store.settings!.default_client_type
    model.outputs[0].slug = store.settings!.default_client_type
    model.outputs[0].update_interval_minutes = store.settings!.default_output_interval_minutes
  }
  takeSnapshot()
  setupStepObserver()
})

onUnmounted(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
  stepObserver?.disconnect()
})

interface ValidationProblem {
  message: string
  target: HTMLElement | null
}

function validate(): ValidationProblem | null {
  if (!model.name.trim()) {
    return { message: '请填写订阅组名称', target: basicSection.value }
  }
  const whitelistLines = model.ip_whitelist
    .split(/\r?\n/)
    .map((x) => x.trim())
    .filter((x) => x)
  for (const [i, line] of whitelistLines.entries()) {
    // 轻量形态检查，逐行精确校验交给后端 422 提示
    if (!/^[0-9a-fA-F:.]+(\/\d{1,3})?$/.test(line)) {
      return {
        message: `IP 白名单第 ${i + 1} 行格式不正确，应为 IP 地址或 CIDR 网段`,
        target: basicSection.value,
      }
    }
  }
  for (const [i, u] of model.upstreams.entries()) {
    const url = (u.url || '').trim()
    if (!url) {
      return {
        message: `第 ${i + 1} 条订阅链接还没有填写 URL，请填写或删除该行`,
        target: upstreamSection.value,
      }
    }
    if (!/^https?:\/\//i.test(url)) {
      return {
        message: `第 ${i + 1} 条订阅链接的 URL 需以 http:// 或 https:// 开头`,
        target: upstreamSection.value,
      }
    }
  }
  const slugs = new Set<string>()
  for (const [i, o] of model.outputs.entries()) {
    if (!o.name.trim()) {
      return { message: `第 ${i + 1} 个输出还没有填写显示名称`, target: outputSection.value }
    }
    const slug = o.slug.trim()
    if (!slug) {
      return { message: `第 ${i + 1} 个输出还没有填写 URL 标识`, target: outputSection.value }
    }
    if (!/^[a-z0-9-]+$/.test(slug)) {
      return {
        message: `输出“${o.name}”的 URL 标识只能包含小写字母、数字和连字符`,
        target: outputSection.value,
      }
    }
    if (slugs.has(slug)) {
      return { message: `URL 标识“${slug}”重复，同一组内必须唯一`, target: outputSection.value }
    }
    slugs.add(slug)
  }
  return null
}

async function save() {
  const problem = validate()
  if (problem) {
    ElMessage.error(problem.message)
    problem.target?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    return
  }
  if (
    !model.upstreams.some((x) => x.enabled) &&
    !manualNodes.value.some((x) => x.enabled) &&
    !manualContent.value.trim()
  ) {
    ElMessage.warning('至少添加一条订阅链接或一条手动节点')
    return
  }
  saving.value = true
  try {
    const payload = {
      ...model,
      upstreams: model.upstreams
        .filter((x) => x.url)
        .map(({ id, name, url, enabled, rename_policy, rename_ignore, rename_template }) => ({
          id,
          name,
          url,
          enabled,
          rename_policy,
          rename_ignore,
          rename_template,
        })),
      outputs: model.outputs.map(
        ({ id, client_type, name, slug, update_interval_minutes, enabled }) => ({
          id,
          client_type,
          name,
          slug,
          update_interval_minutes,
          enabled,
        }),
      ),
    }
    const result: any = await api(
      editing.value ? `/api/subscriptions/${route.params.id}` : '/api/subscriptions',
      { method: editing.value ? 'PUT' : 'POST', body: JSON.stringify(payload) },
    )
    const groupId = result.id || Number(route.params.id)
    if (manualContent.value.trim()) {
      await api(`/api/subscriptions/${groupId}/manual-nodes/import`, {
        method: 'POST',
        body: JSON.stringify({ content: manualContent.value }),
      })
    }
    takeSnapshot() // 保存成功，清除脏标记，离开守卫不再拦截
    ElMessage.success('节点来源与输出已保存')
    await store.loadGroups()
    router.push(`/groups/${groupId}/detail`)
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('有未保存的修改，确定要离开吗？', '未保存的修改', {
      type: 'warning',
      confirmButtonText: '离开',
      cancelButtonText: '继续编辑',
    })
    return true
  } catch {
    return false
  }
})

function openManualEdit(node: ManualNode) {
  manualEditNode.value = node
  manualEditContent.value = ''
  manualEditVisible.value = true
}

async function saveManualEdit() {
  if (!manualEditNode.value || !manualEditContent.value.trim()) return
  manualEditSaving.value = true
  try {
    const result: any = await api(
      `/api/subscriptions/${route.params.id}/manual-nodes/${manualEditNode.value.id}`,
      { method: 'PUT', body: JSON.stringify({ content: manualEditContent.value.trim() }) },
    )
    manualNodes.value = result.nodes
    manualEditVisible.value = false
    ElMessage.success('连接内容已替换，原有自定义名称与分类已保留')
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    manualEditSaving.value = false
  }
}

async function toggleManual(node: ManualNode, value: unknown) {
  const result: any = await api(
    `/api/subscriptions/${route.params.id}/manual-nodes/${node.id}`,
    { method: 'PUT', body: JSON.stringify({ enabled: !!value }) },
  )
  manualNodes.value = result.nodes
}

async function deleteManual(node: ManualNode) {
  try {
    await ElMessageBox.confirm(`删除手动节点“${node.name}”？`, '删除节点', { type: 'warning' })
    const result: any = await api(
      `/api/subscriptions/${route.params.id}/manual-nodes/${node.id}`,
      { method: 'DELETE' },
    )
    manualNodes.value = result.nodes
  } catch {}
}
</script>

<template>
  <section class="page editor-page">
    <header class="page-head">
      <div>
        <button class="back" @click="router.back()"><ArrowLeft /></button>
        <p class="eyebrow">NODE SOURCES</p>
        <h1>{{ editing ? '编辑订阅组' : '新建订阅组' }}</h1>
        <p>订阅链接和手动节点可混合使用，标准化后统一改名、分类、输出和测活。</p>
      </div>
      <div class="head-actions">
        <span v-if="dirty" class="dirty-hint">有未保存修改</span>
        <el-button v-if="editing" @click="router.push(`/groups/${route.params.id}/categories`)">
          <FolderTree />分类管理
        </el-button>
        <el-button type="primary" :class="{ 'save-dirty': dirty }" :loading="saving" @click="save"><Save />保存</el-button>
      </div>
    </header>
    <div class="editor-layout">
      <div class="editor-main">
        <div class="editor-steps">
          <template v-for="(s, i) in steps" :key="s.label">
            <span :class="{ active: activeStep === i }" @click="goStep(i)">{{ s.label }}</span>
            <i v-if="i < steps.length - 1"></i>
          </template>
        </div>
        <section ref="basicSection" class="panel form-section">
          <div class="section-head">
            <span>01</span>
            <div>
              <h2>基本信息</h2>
              <p>识别和调度这个订阅组。</p>
            </div>
          </div>
          <div class="form-grid">
            <label>名称<el-input v-model="model.name" /></label>
            <label>
              刷新间隔（分钟）
              <el-input-number v-model="model.interval_minutes" :min="5" :max="10080" />
            </label>
            <label class="wide">备注<el-input v-model="model.note" /></label>
            <label class="wide">
              订阅 IP 白名单
              <el-input
                v-model="model.ip_whitelist"
                type="textarea"
                :rows="3"
                placeholder="203.0.113.8&#10;2001:db8::/32"
              />
              <small class="field-help">
                仅列出的 IP/网段可拉取本组订阅；留空不限制。反代部署时以 X-Forwarded-For 为准。
              </small>
            </label>
          </div>
          <el-switch v-model="model.enabled" active-text="启用订阅组" />
        </section>
        <section ref="upstreamSection" class="panel form-section">
          <div class="section-head">
            <span>02</span>
            <div>
              <h2>远程订阅链接</h2>
              <p>每条链接只请求一次，并可单独决定是否参与自动改名。</p>
            </div>
            <el-button @click="addUpstream"><Plus />添加链接</el-button>
          </div>
          <div v-if="!model.upstreams.length" class="mini-empty">
            当前没有远程链接，可以只使用手动节点。
          </div>
          <div class="structured-list upstream-config-list">
            <article v-for="(u, i) in model.upstreams" :key="u.id || i">
              <div class="upstream-main-row">
                <div class="list-fields">
                  <label>来源名称<el-input v-model="u.name" /></label>
                  <label class="url-field">
                    订阅 URL
                    <el-input
                      v-model="u.url"
                      type="password"
                      show-password
                      placeholder="https://… 可直接粘贴"
                    />
                  </label>
                </div>
                <el-switch v-model="u.enabled" />
                <button class="icon-danger" title="删除" @click="removeUpstream(i)"><Trash2 /></button>
              </div>
              <div class="upstream-rename-row">
                <label>
                  自动改名
                  <el-select v-model="u.rename_policy">
                    <el-option label="跟随订阅组设置" value="inherit" />
                    <el-option label="不改名（保留原名）" value="disabled" />
                    <el-option label="本链接独立规则" value="smart" />
                  </el-select>
                  <small class="field-help">
                    选“不改名”后这条订阅保留上游原始名称；早期“原样透传”配置会自动归入此项。
                  </small>
                </label>
                <template v-if="u.rename_policy === 'smart'">
                  <label>忽略前缀<el-input v-model="u.rename_ignore" placeholder="例如 DMIT-US" /></label>
                  <label class="wide">独立模板<el-input v-model="u.rename_template" /></label>
                </template>
              </div>
            </article>
          </div>
          <el-collapse class="bulk">
            <el-collapse-item title="批量粘贴多个订阅链接">
              <el-input
                v-model="bulk"
                type="textarea"
                :rows="6"
                placeholder="每行一个 URL，也支持：机场名称 | https://…"
              />
              <el-button class="bulk-button" @click="parseBulk">
                <Upload />解析并加入当前组
              </el-button>
            </el-collapse-item>
          </el-collapse>
        </section>
        <section ref="manualSection" class="panel form-section">
          <div class="section-head">
            <span>03</span>
            <div>
              <h2>手动节点</h2>
              <p>手动节点默认保留导入名称，不参与订阅组自动改名；导入后可在节点详情中逐个设置名称。</p>
            </div>
            <Database />
          </div>
          <div class="manual-rename-notice">
            手动节点：自动改名已固定关闭 · 单节点自定义名称仍然优先
          </div>
          <div v-if="pendingManualCount" class="manual-pending-notice">
            有 {{ pendingManualCount }} 条待保存的手动节点，点击右上角「保存」后才会生效
          </div>
          <el-input
            v-model="manualContent"
            type="textarea"
            :rows="6"
            placeholder="粘贴 vless://、vmess://、trojan://… 或 proxies: YAML；可一次导入多条"
          />
          <div v-if="manualNodes.length" class="manual-node-list">
            <article v-for="n in manualNodes" :key="n.id">
              <span>
                <b>{{ n.name }}</b>
                <small>{{ n.protocol.toUpperCase() }} · 连接参数已加密</small>
              </span>
              <span class="manual-current-name">
                <small>当前节点名</small>
                <b>{{ n.final_name || n.name }}</b>
              </span>
              <el-switch :model-value="n.enabled" @change="toggleManual(n, $event)" />
              <el-button link @click="openManualEdit(n)">替换连接</el-button>
              <el-button link type="danger" @click="deleteManual(n)">删除</el-button>
            </article>
          </div>
        </section>
        <section ref="renameSection" class="panel form-section">
          <div class="section-head">
            <span>04</span>
            <div>
              <h2>订阅组默认改名</h2>
              <p>仅作用于选择“跟随订阅组设置”的远程链接；手动节点始终不参与。</p>
            </div>
          </div>
          <el-radio-group v-model="model.rename_mode">
            <el-radio-button value="passthrough">原样透传</el-radio-button>
            <el-radio-button value="smart">智能规则</el-radio-button>
          </el-radio-group>
          <div v-if="model.rename_mode === 'smart'" class="form-grid rename-config">
            <label>忽略前缀<el-input v-model="model.rename_ignore" type="textarea" :rows="3" /></label>
            <label>
              模板
              <el-input v-model="model.rename_template" />
              <small class="field-help">{index} {flag} {name} {traffic} {reset} {source}</small>
            </label>
          </div>
        </section>
        <section ref="outputSection" class="panel form-section">
          <div class="section-head">
            <span>05</span>
            <div>
              <h2>客户端输出</h2>
              <p>Mihomo 支持自定义代理分类；旧客户端只输出兼容节点。</p>
            </div>
            <el-button @click="addOutput"><Plus />添加输出</el-button>
          </div>
          <div class="structured-list">
            <article v-for="(o, i) in model.outputs" :key="o.id || i">
              <div class="list-fields output-fields">
                <label>
                  客户端
                  <el-select v-model="o.client_type">
                    <el-option v-for="(v, k) in store.types" :key="k" :label="v.label" :value="k" />
                  </el-select>
                </label>
                <label>名称<el-input v-model="o.name" /></label>
                <label>URL 标识<el-input v-model="o.slug" placeholder="小写字母、数字、连字符" /></label>
                <label>
                  更新间隔
                  <el-input-number v-model="o.update_interval_minutes" :min="5" :max="10080" />
                </label>
              </div>
              <el-switch v-model="o.enabled" />
              <button class="icon-danger" title="删除" @click="removeOutput(i)"><Trash2 /></button>
            </article>
          </div>
        </section>
        <el-dialog v-model="manualEditVisible" title="替换手动节点连接" width="560px">
          <p class="field-help">
            只替换加密保存的连接内容；节点自定义名称、确认状态、分类和排序会保留。请粘贴一条 URI 或单节点
            proxies YAML。
          </p>
          <el-input
            v-model="manualEditContent"
            type="textarea"
            :rows="8"
            autocomplete="off"
            placeholder="vless://..."
          />
          <template #footer>
            <el-button @click="manualEditVisible = false">取消</el-button>
            <el-button type="primary" :loading="manualEditSaving" @click="saveManualEdit">
              保存替换
            </el-button>
          </template>
        </el-dialog>
      </div>
      <aside class="editor-aside">
        <div class="panel sticky-card">
          <p class="eyebrow">V3.3 SOURCES</p>
          <h3>统一节点来源</h3>
          <div class="policy">
            <span>订阅链接</span>
            <b>{{ model.upstreams.filter((x) => x.enabled).length }}</b>
            <span>已保存手动节点</span>
            <b>{{ manualNodes.filter((x) => x.enabled).length }}</b>
            <span>待保存手动节点</span>
            <b :class="{ 'pending-highlight': pendingManualCount }">
              {{ pendingManualCount || '无' }}
            </b>
          </div>
          <p v-if="pendingManualCount" class="aside-pending-note">
            手动节点随整组保存才生效，离开前请先保存。
          </p>
          <p>保存后刷新一次即可建立脱敏节点快照，再进入分类管理按整条来源或单节点分配。</p>
        </div>
      </aside>
    </div>
  </section>
</template>
