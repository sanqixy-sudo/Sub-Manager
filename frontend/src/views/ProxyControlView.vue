<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Server, Network, Users, Package, Plus, RefreshCw, Copy, ExternalLink } from 'lucide-vue-next'
import { api } from '../api'
import { copyText } from '../utils'

type Item = Record<string, any>
const data = ref<Record<string, Item[]>>({ servers: [], nodes: [], users: [], packages: [], assignments: [] })
const tab = ref('servers')
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const dialog = ref('')
const form = reactive<Item>({})
const formError = ref('')
const secretTitle = ref('')
const secretText = ref('')
const secretOpen = ref(false)
let timer: ReturnType<typeof setInterval>
let controller: AbortController | undefined
const statuses: Record<string, string> = { active: '生效中', disabled: '已停用', expired: '已到期', exhausted: '流量用尽', running: '代理运行中', stopped: '代理已停止', error: '应用失败', offline: '等待接入' }
const errors: Record<string, string> = { config_invalid: '配置校验失败，保留上一配置', start_failed: '启动失败，请检查端口占用', stats_unavailable: '统计不可用，代理已暂停', lease_expired: '主控连接超时，代理已暂停' }
const online = computed(() => data.value.servers.filter(s => s.online).length)
const active = computed(() => data.value.assignments.filter(a => a.status === 'active').length)
const tabs = [{ name: 'servers', label: '服务器' }, { name: 'nodes', label: '代理节点与链路' }, { name: 'users', label: '用户' }, { name: 'packages', label: '套餐模板' }, { name: 'assignments', label: '套餐实例' }]
function bytes(n: number) { return `${((n || 0) / 1024 ** 3).toFixed(2)} GB` }
function date(s: string) { return s ? new Date(s).toLocaleString('zh-CN', { hour12: false }) : '—' }
function serverName(id: number) { return data.value.servers.find(s => s.id === id)?.name || '—' }
function nodeName(id: number) { return data.value.nodes.find(n => n.id === id)?.name || '直连出口' }
function chain(node: Item) {
  const names = [node.name], seen = new Set([node.id]); let next = node.next_node_id
  while (next && !seen.has(next)) { seen.add(next); const n = data.value.nodes.find(n => n.id === next); if (!n) break; names.push(n.name); next = n.next_node_id }
  return names.join(' → ') + ' → 目标网站'
}
async function load(background = false) {
  if (loading.value || (background && (document.hidden || dialog.value || secretOpen.value))) return
  loading.value = true
  controller = new AbortController()
  try { data.value = await api('/api/control/overview', { signal: controller.signal }); error.value = '' }
  catch (e: any) { if (e.name !== 'AbortError') error.value = e.message }
  finally { loading.value = false }
}
onMounted(() => { load(); timer = setInterval(() => load(true), 10000) })
onUnmounted(() => { clearInterval(timer); controller?.abort() })
function open(kind: string, item?: Item) {
  Object.keys(form).forEach(k => delete form[k]); formError.value = ''
  const defaults: Record<string, Item> = {
    servers: { name: '', host: '', enabled: true },
    nodes: { name: '', server_id: data.value.servers[0]?.id, protocol: 'anytls', port: 443, server_name: '', enabled: true, next_node_id: null, multiplier: 1, certificate_pem: '', private_key_pem: '' },
    users: { username: '', password: '', enabled: true },
    packages: { name: '', quota_gb: 100, cycle_days: 30, duration_days: 30, enabled: true, node_ids: [] },
    assignments: { user_id: data.value.users[0]?.id, package_id: data.value.packages[0]?.id },
  }
  Object.assign(form, defaults[kind], item ? JSON.parse(JSON.stringify(item)) : {})
  for (const key of ['enabled']) if (key in form) form[key] = Boolean(form[key])
  dialog.value = kind
}
async function save() {
  saving.value = true; formError.value = ''
  try {
    const payload = { ...form }
    if (dialog.value === 'users' && !payload.password) delete payload.password
    const result: Item = await api(`/api/control/${dialog.value}${form.id ? '/' + form.id : ''}`, { method: form.id ? 'PUT' : 'POST', body: JSON.stringify(payload) })
    dialog.value = ''; ElMessage.success('已保存，服务器将在下次同步时应用')
    if (result.initial_password) showSecret('用户初始密码（仅显示本次）', `用户名：${form.username}\n密码：${result.initial_password}`)
    await load()
  } catch (e: any) { formError.value = e.message }
  finally { saving.value = false }
}
function showSecret(title: string, text: string) { secretTitle.value = title; secretText.value = text; secretOpen.value = true }
async function pair(server: Item) {
  try {
    if (server.paired) await ElMessageBox.confirm('新 Agent 完成配对后，原 Agent 凭据立即失效。确认重新接入这台服务器？', '重新配对', { type: 'warning' })
    saving.value = true
    const result: Item = await api(`/api/control/servers/${server.id}/pairing`, { method: 'POST' })
    showSecret('服务器安装命令 · 15 分钟内有效 · 仅可配对一次', result.command)
  } catch (e: any) { if (e instanceof Error) ElMessage.error(e.message) }
  finally { saving.value = false }
}
async function links(user: Item) {
  try {
    const result: Item = await api(`/api/control/users/${user.id}/links`, { method: 'POST' })
    showSecret(`${user.username} 的订阅地址`, `Mihomo / Clash Meta\n${result.mihomo}\n\nsing-box\n${result.singbox}\n\n用户中心\n${result.portal}`)
  } catch (e: any) { ElMessage.error(e.message) }
}
async function rotate(user: Item) {
  try {
    await ElMessageBox.confirm('旧订阅地址将立即失效。已下载的节点凭据仍有效；停用用户可停止其代理授权。', '重置订阅地址', { type: 'warning' })
    await api(`/api/control/users/${user.id}/rotate-subscription`, { method: 'POST' }); ElMessage.success('订阅地址已重置')
  } catch (e: any) { if (e instanceof Error) ElMessage.error(e.message) }
}
async function assignmentAction(item: Item, action: string) {
  try {
    let payload: Item = {}
    if (action === 'renew') { const result = await ElMessageBox.prompt('填写续期天数，到期后从当前时间开始延长。', '续期', { inputValue: '30', inputPattern: /^[1-9][0-9]{0,3}$/, inputErrorMessage: '请输入 1–3660 天' }); payload.extend_days = Number(result.value) }
    else if (action === 'reset') { await ElMessageBox.confirm('将当前已用流量设为新周期基线，历史账本仍保留。', '重置本周期流量', { type: 'warning' }); payload.reset_traffic = true }
    else payload.enabled = !item.enabled
    saving.value = true
    await api(`/api/control/assignments/${item.id}`, { method: 'PATCH', body: JSON.stringify(payload) }); await load(); ElMessage.success('套餐实例已更新')
  } catch (e: any) { if (e instanceof Error) ElMessage.error(e.message) }
  finally { saving.value = false }
}
async function copySecret() { try { await copyText(secretText.value); ElMessage.success('已复制') } catch { ElMessage.error('复制失败，请手动复制') } }
function closeDialog(value: boolean) { if (!value) dialog.value = '' }
</script>

<template>
  <section class="page proxy-control">
    <header class="page-head">
      <div><p class="eyebrow">PROXY OPERATIONS</p><h1>代理服务</h1><p>接入服务器、创建 AnyTLS 节点，为每位用户分配独立线路与流量。</p></div>
      <div class="control-actions"><a href="/#/portal" target="_blank" rel="noopener" class="portal-link"><ExternalLink :size="16" aria-hidden="true" />用户中心</a><el-button :loading="loading" @click="load()"><RefreshCw :size="16" aria-hidden="true" />刷新</el-button></div>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="control-alert" />
    <div class="control-summary">
      <div class="panel"><Server aria-hidden="true" /><span>在线服务器<strong>{{ online }} / {{ data.servers.length }}</strong></span></div>
      <div class="panel"><Network aria-hidden="true" /><span>托管节点<strong>{{ data.nodes.length }}</strong></span></div>
      <div class="panel"><Users aria-hidden="true" /><span>用户<strong>{{ data.users.length }}</strong></span></div>
      <div class="panel"><Package aria-hidden="true" /><span>生效套餐<strong>{{ active }}</strong></span></div>
    </div>
    <section class="panel control-body">
      <el-tabs v-model="tab"><el-tab-pane v-for="item in tabs" :key="item.name" :label="item.label" :name="item.name" /></el-tabs>
      <div class="control-toolbar"><p>{{ tab === 'servers' ? '先添加服务器，再复制安装命令到服务器执行。每台服务器使用独立命令。' : tab === 'nodes' ? '节点使用 TLS 加密。设置下一跳即可串联线路；下游停用时入口同步暂停。' : tab === 'users' ? '用户使用独立凭据。停用后服务器会撤销该用户的全部代理授权。' : tab === 'packages' ? '模板定义节点范围、周期与额度；空节点列表表示无节点权限。修改会作用于已绑定实例。' : '每次绑定产生独立凭据、流量周期和到期时间。配额为 0 表示不限量。' }}</p><el-button type="primary" :disabled="saving" @click="open(tab)"><Plus :size="16" aria-hidden="true" />{{ tab === 'assignments' ? '绑定套餐' : '新增' }}</el-button></div>

      <div v-if="tab === 'servers'" class="server-grid">
        <el-empty v-if="!data.servers.length" description="还没有服务器，添加后即可生成安装命令" />
        <article v-for="s in data.servers" :key="s.id" class="server-card">
          <div class="control-card-head"><h2>{{ s.name }}</h2><el-tag :type="s.online ? 'success' : 'info'">{{ s.online ? '在线' : '离线' }}</el-tag></div>
          <p class="mono">{{ s.host }}</p><p>{{ statuses[s.runtime_status] || s.runtime_status }} · {{ !s.enabled ? '已停用' : s.synced ? '配置已同步' : '等待应用配置' }}</p>
          <el-alert v-if="s.error_code" :title="errors[s.error_code] || s.error_code" type="error" :closable="false" />
          <dl><div><dt>内存</dt><dd>{{ s.metrics.memory_total ? Math.round((s.metrics.memory_total - s.metrics.memory_available) / s.metrics.memory_total * 100) + '%' : '—' }}</dd></div><div><dt>负载</dt><dd>{{ s.metrics.load?.toFixed(2) ?? '—' }}</dd></div><div><dt>最近心跳</dt><dd>{{ date(s.last_seen) }}</dd></div></dl>
          <div class="control-actions"><el-button @click="open('servers', s)">编辑 / 启停</el-button><el-button :loading="saving" @click="pair(s)">{{ s.paired ? '重新配对' : '安装命令' }}</el-button></div>
        </article>
      </div>
      <div v-else-if="tab === 'nodes'" class="node-list">
        <el-empty v-if="!data.nodes.length" description="服务器接入后，在这里创建代理节点" />
        <article v-for="n in data.nodes" :key="n.id" class="managed-node">
          <div class="control-card-head"><div><h2>{{ n.name }}</h2><p>{{ serverName(n.server_id) }} · {{ n.protocol.toUpperCase() }} · TCP {{ n.port }}</p></div><el-tag :type="n.available ? 'success' : 'warning'">{{ !n.enabled ? '已停用' : n.available ? '配置可用' : '链路或证书不可用' }}</el-tag></div>
          <p class="chain-path"><Network :size="16" aria-hidden="true" />{{ chain(n) }}</p><p class="control-muted">TLS 名称：{{ n.server_name }} · 证书到期：{{ date(n.cert_expires) }} · 计费倍率：{{ n.multiplier }}×</p>
          <el-button @click="open('nodes', n)">编辑节点 / 配置链路</el-button>
        </article>
      </div>
      <el-table v-else-if="tab === 'users'" :data="data.users" empty-text="创建用户后，再为其绑定套餐">
        <el-table-column prop="username" label="用户名" min-width="140" /><el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
        <el-table-column label="操作" min-width="300"><template #default="{ row }"><el-button @click="open('users', row)">编辑 / 启停</el-button><el-button @click="links(row)">订阅地址</el-button><el-button @click="rotate(row)">重置地址</el-button></template></el-table-column>
      </el-table>
      <div v-else-if="tab === 'packages'" class="server-grid">
        <el-empty v-if="!data.packages.length" description="先创建套餐模板，再分配给用户" />
        <article v-for="p in data.packages" :key="p.id" class="server-card"><div class="control-card-head"><h2>{{ p.name }}</h2><el-tag :type="p.enabled ? 'success' : 'info'">{{ p.enabled ? '启用' : '停用' }}</el-tag></div><p class="package-quota">{{ p.quota_bytes ? bytes(p.quota_bytes) : '不限流量' }}<small> / {{ p.cycle_days }} 天</small></p><p>默认有效期 {{ p.duration_days }} 天 · {{ p.node_ids.length }} 个节点</p><p class="control-muted">{{ p.node_ids.map(nodeName).join('、') || '无节点权限' }}</p><el-button @click="open('packages', p)">编辑套餐</el-button></article>
      </div>
      <div v-else class="assignment-list">
        <el-empty v-if="!data.assignments.length" description="将套餐绑定给用户后，这里会显示独立实例" />
        <article v-for="a in data.assignments" :key="a.id" class="managed-node"><div class="control-card-head"><div><h2>{{ a.username }} · {{ a.package_name }} <small>#{{ a.id }}</small></h2><p>到期 {{ date(a.expires_at) }}</p></div><el-tag :type="a.status === 'active' ? 'success' : 'warning'">{{ statuses[a.status] }}</el-tag></div><p>{{ bytes(a.upload + a.download) }} / {{ a.quota_bytes ? bytes(a.quota_bytes) : '不限量' }}</p><el-progress v-if="a.quota_bytes" :percentage="Math.min(100, Math.round((a.upload + a.download) / a.quota_bytes * 100))" /><p class="control-muted">本周期起点 {{ date(a.cycle_start) }} · 每 {{ a.cycle_days }} 天重置</p><div class="control-actions"><el-button :disabled="saving" @click="assignmentAction(a, 'renew')">续期</el-button><el-button :disabled="saving" @click="assignmentAction(a, 'toggle')">{{ a.enabled ? '停用' : '启用' }}</el-button><el-button :disabled="saving" @click="assignmentAction(a, 'reset')">重置流量</el-button></div></article>
      </div>
    </section>
    <p class="control-muted control-footnote">状态每 10 秒刷新。节点变更会重启该服务器的托管内核，已有连接可能中断。额度与到期控制在同步后生效；服务器失联超过 120 秒会暂停托管代理。</p>

    <el-dialog :model-value="!!dialog" @update:model-value="closeDialog" :title="(form.id ? '编辑' : '新增') + (tabs.find(t => t.name === dialog)?.label || '')" width="min(620px, calc(100vw - 24px))" :close-on-click-modal="false" class="control-dialog" @closed="Object.keys(form).forEach(k => delete form[k])">
      <el-form label-position="top" @submit.prevent="save">
        <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon class="control-alert" />
        <el-form-item v-if="dialog !== 'users' && dialog !== 'assignments'" label="名称" required><el-input v-model.trim="form.name" maxlength="80" /></el-form-item>
        <template v-if="dialog === 'servers'"><el-form-item label="服务器 IP / 域名" required><el-input v-model.trim="form.host" placeholder="例如 node.example.com，不含协议和端口" /></el-form-item><p class="control-muted">生成安装命令前，请先在系统设置填写 HTTPS 公开访问地址。安装器支持 Debian / Ubuntu、amd64 / arm64。</p></template>
        <template v-if="dialog === 'nodes'">
          <div class="control-form-grid"><el-form-item label="服务器" required><el-select v-model="form.server_id" :disabled="!!form.id"><el-option v-for="s in data.servers" :key="s.id" :label="s.name" :value="s.id" /></el-select></el-form-item><el-form-item label="协议"><el-select v-model="form.protocol" :disabled="!!form.id"><el-option label="AnyTLS" value="anytls" /><el-option label="VLESS + TLS" value="vless" /><el-option label="Trojan" value="trojan" /></el-select></el-form-item><el-form-item label="监听端口" required><el-input-number v-model="form.port" :min="1" :max="65535" /></el-form-item><el-form-item label="TLS 名称 / SNI" required><el-input v-model.trim="form.server_name" placeholder="节点域名或 IP" /></el-form-item></div>
          <el-form-item label="下一跳（链式出站）"><el-select v-model="form.next_node_id" clearable placeholder="留空：从本机直接访问网站" @clear="form.next_node_id = null"><el-option v-for="n in data.nodes.filter(n => n.id !== form.id)" :key="n.id" :label="`${n.name} · ${serverName(n.server_id)}`" :value="n.id" /></el-select></el-form-item>
          <el-form-item label="入口用户计费倍率"><el-input-number v-model="form.multiplier" :min="0.01" :max="100" :step="0.1" /></el-form-item>
          <el-collapse><el-collapse-item title="TLS 证书（可选上传）" name="certificate"><p class="control-muted">新建时留空将生成证书，客户端订阅会校验证书指纹。编辑时留空保留原证书；上传时须同时提供证书和私钥。</p><el-form-item label="证书 PEM"><el-input v-model="form.certificate_pem" type="textarea" :rows="4" placeholder="-----BEGIN CERTIFICATE-----" /></el-form-item><el-form-item label="私钥 PEM"><el-input v-model="form.private_key_pem" type="textarea" :rows="4" placeholder="-----BEGIN PRIVATE KEY-----" /></el-form-item></el-collapse-item></el-collapse>
        </template>
        <template v-if="dialog === 'users'"><el-form-item label="用户名" required><el-input v-model.trim="form.username" autocomplete="off" placeholder="2–40 位字母、数字、下划线或短横线" /></el-form-item><el-form-item :label="form.id ? '新密码（留空不修改）' : '初始密码（留空自动生成）'"><el-input v-model="form.password" type="password" show-password autocomplete="new-password" placeholder="至少 8 位" /></el-form-item></template>
        <template v-if="dialog === 'packages'"><div class="control-form-grid"><el-form-item label="周期流量 GB（0 为不限量）"><el-input-number v-model="form.quota_gb" :min="0" :max="100000" /></el-form-item><el-form-item label="流量重置周期（天）"><el-input-number v-model="form.cycle_days" :min="1" :max="366" /></el-form-item><el-form-item label="默认有效期（天）"><el-input-number v-model="form.duration_days" :min="1" :max="3660" /></el-form-item></div><el-form-item label="可用节点"><el-select v-model="form.node_ids" multiple placeholder="未选择节点 = 无节点权限"><el-option v-for="n in data.nodes" :key="n.id" :label="n.name" :value="n.id" /></el-select></el-form-item></template>
        <template v-if="dialog === 'assignments'"><el-form-item label="用户" required><el-select v-model="form.user_id"><el-option v-for="u in data.users" :key="u.id" :label="u.username" :value="u.id" /></el-select></el-form-item><el-form-item label="套餐模板" required><el-select v-model="form.package_id"><el-option v-for="p in data.packages" :key="p.id" :label="p.name" :value="p.id" /></el-select></el-form-item><p class="control-muted">同一用户可多次绑定套餐，每个实例单独生成凭据、计算额度与到期时间。</p></template>
        <el-form-item v-if="dialog !== 'assignments'" label="状态"><el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" /></el-form-item>
        <div class="control-dialog-actions"><el-button :disabled="saving" @click="dialog = ''">取消</el-button><el-button type="primary" native-type="submit" :loading="saving">保存</el-button></div>
      </el-form>
    </el-dialog>
    <el-dialog v-model="secretOpen" :title="secretTitle" width="min(680px, calc(100vw - 24px))" class="control-dialog" @closed="secretText = ''"><p class="control-muted">包含敏感凭据，请只交给对应用户或在对应服务器执行。</p><el-input :model-value="secretText" type="textarea" :rows="9" readonly aria-label="敏感信息" /><template #footer><el-button @click="secretOpen = false">关闭</el-button><el-button type="primary" @click="copySecret"><Copy :size="16" aria-hidden="true" />复制</el-button></template></el-dialog>
  </section>
</template>

<style scoped>
.control-actions,.control-card-head,.chain-path{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.control-card-head{justify-content:space-between}.control-actions .el-button+.el-button{margin-left:0}.portal-link{display:flex;align-items:center;gap:6px;color:var(--primary);text-decoration:none;min-height:44px}.control-alert{margin-bottom:16px}.control-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:24px}.control-summary .panel{display:flex;gap:16px;align-items:center;padding:20px}.control-summary svg{color:var(--primary)}.control-summary span{color:var(--muted)}.control-summary strong{display:block;font-size:26px;color:var(--text);margin-top:6px}.control-body{padding:20px;min-width:0}.control-toolbar{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin:12px 0 24px}.control-toolbar p{margin:0;max-width:760px;color:var(--muted);line-height:1.7}.server-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:16px}.server-card,.managed-node{border:1px solid var(--line);border-radius:var(--r-md);padding:20px;min-width:0;overflow-wrap:anywhere}.server-card h2,.managed-node h2{font-size:17px;margin:0}.server-card p,.managed-node p{line-height:1.65}.server-card dl{font-size:13px}.server-card dl div{display:flex;justify-content:space-between;gap:12px;margin:12px 0}.server-card dd{margin:0;text-align:right}.server-card dt,.control-muted{color:var(--muted)}.mono{font-family:ui-monospace,monospace}.node-list,.assignment-list{display:grid;gap:16px}.chain-path{padding:12px;background:var(--primary-soft);color:var(--primary-deep);border-radius:var(--r-sm)}.package-quota{font-size:26px;font-weight:600}.package-quota small,.managed-node h2 small{font-size:14px;color:var(--muted);font-weight:400}.control-footnote{font-size:13px;line-height:1.8;margin:20px 0}.control-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}.control-dialog-actions{display:flex;justify-content:flex-end;gap:12px;margin-top:20px}.control-dialog-actions .el-button{min-height:44px}.control-body :deep(.el-tabs__item){min-height:44px}.control-form-grid :deep(.el-input-number),:deep(.el-select){width:100%}.control-body :deep(.el-table__body-wrapper){overflow-x:auto}.proxy-control :deep(.el-button){min-height:40px}.control-dialog :deep(.el-dialog__body){overflow-wrap:anywhere}@media(max-width:760px){.control-summary{grid-template-columns:1fr 1fr;gap:10px}.control-summary .panel{padding:14px;gap:10px}.control-body{padding:14px}.control-toolbar{gap:12px}.control-toolbar p{font-size:13px}.control-form-grid{grid-template-columns:1fr}.proxy-control :deep(.el-button){min-height:44px}.server-card,.managed-node{padding:16px}.control-card-head{align-items:flex-start}.control-card-head h2{max-width:100%}}
</style>
