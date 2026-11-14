<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ShieldCheck, Copy, LogOut, RefreshCw } from 'lucide-vue-next'
import { api, ApiError } from '../api'
import { copyText } from '../utils'

const user = ref<any>(null)
const ready = ref(false)
const busy = ref(false)
const error = ref('')
const form = reactive({ username: '', password: '' })
const statuses: Record<string, string> = { active: '生效中', disabled: '已停用', expired: '已到期', exhausted: '流量用尽' }
let timer: ReturnType<typeof setInterval>
let controller: AbortController | undefined
const bytes = (value: number) => `${((value || 0) / 1024 ** 3).toFixed(2)} GB`
const date = (value: string) => new Date(value).toLocaleDateString('zh-CN')
async function load() {
  if (busy.value) return
  busy.value = true; controller = new AbortController()
  try { user.value = await api('/api/portal/me', { signal: controller.signal }); error.value = '' }
  catch (e: any) { if (e instanceof ApiError && e.status === 401) user.value = null; else if (e.name !== 'AbortError') error.value = e.message }
  finally { ready.value = true; busy.value = false }
}
async function login() {
  busy.value = true; error.value = ''
  try { await api('/api/portal/login', { method: 'POST', body: JSON.stringify(form) }); form.password = ''; busy.value = false; await load() }
  catch (e: any) { error.value = e.message }
  finally { busy.value = false }
}
async function logout() {
  try { await api('/api/portal/logout', { method: 'POST' }); user.value = null }
  catch (e: any) { error.value = e.message }
}
async function copy(value: string) { try { await copyText(value); ElMessage.success('已复制订阅地址') } catch { ElMessage.error('复制失败，请手动复制') } }
onMounted(() => { load(); timer = setInterval(() => { if (user.value && !document.hidden) load() }, 30000) })
onUnmounted(() => { clearInterval(timer); controller?.abort() })
</script>

<template>
  <div class="user-portal">
    <header class="portal-header"><div><ShieldCheck aria-hidden="true" /><strong>用户中心</strong></div><el-button v-if="user" @click="logout"><LogOut :size="16" aria-hidden="true" />退出登录</el-button></header>
    <p v-if="!ready" role="status">正在加载…</p>
    <section v-else-if="!user" class="panel portal-login"><h1>登录用户中心</h1><p>使用管理员为你创建的账号，查看套餐和个人订阅。</p><el-alert v-if="error" :title="error" type="error" :closable="false" /><el-form label-position="top" @submit.prevent="login"><el-form-item label="用户名"><el-input v-model.trim="form.username" autocomplete="username" /></el-form-item><el-form-item label="密码"><el-input v-model="form.password" type="password" autocomplete="current-password" show-password /></el-form-item><el-button type="primary" native-type="submit" :loading="busy" class="portal-login-button">登录</el-button></el-form></section>
    <template v-else>
      <div class="portal-heading"><div><p class="eyebrow">MY SUBSCRIPTIONS</p><h1>{{ user.username }} 的套餐</h1><p>每个套餐独立计算额度和有效期。以下地址只包含你获授权的节点。</p></div><el-button :loading="busy" @click="load"><RefreshCw :size="16" aria-hidden="true" />刷新</el-button></div>
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <section class="panel portal-links"><h2>订阅地址</h2><div v-for="kind in ['mihomo','singbox']" :key="kind" class="portal-link-row"><label :for="'link-' + kind">{{ kind === 'mihomo' ? 'Mihomo / Clash Meta' : 'sing-box' }}</label><el-input :id="'link-' + kind" :model-value="user.links[kind]" readonly /><el-button @click="copy(user.links[kind])"><Copy :size="16" aria-hidden="true" />复制</el-button></div><p>订阅含个人凭据，请勿公开分享。使用支持对应协议的客户端导入。</p></section>
      <div class="portal-packages"><el-empty v-if="!user.assignments.length" description="暂未分配套餐，请联系管理员" /><article v-for="a in user.assignments" :key="a.id" class="panel portal-package"><div class="portal-card-head"><h2>{{ a.package_name }} #{{ a.id }}</h2><el-tag :type="a.status === 'active' ? 'success' : 'warning'">{{ statuses[a.status] }}</el-tag></div><p class="portal-used">{{ bytes(a.upload + a.download) }} <small>/ {{ a.quota_bytes ? bytes(a.quota_bytes) : '不限流量' }}</small></p><el-progress v-if="a.quota_bytes" :percentage="Math.min(100, Math.round((a.upload + a.download) / a.quota_bytes * 100))" /><p>上传 {{ bytes(a.upload) }} · 下载 {{ bytes(a.download) }}</p><p>到期日期 {{ date(a.expires_at) }} · 每 {{ a.cycle_days }} 天重置</p></article></div>
      <section class="panel portal-history"><h2>可用节点</h2><p v-if="!user.nodes.length">当前没有可用节点，请检查套餐状态或联系管理员。</p><div class="portal-node-tags"><el-tag v-for="n in user.nodes" :key="n.id">{{ n.name }} · {{ n.protocol.toUpperCase() }}</el-tag></div></section>
      <section v-if="user.history.length" class="panel portal-history"><h2>最近 30 天流量记录</h2><el-table :data="user.history"><el-table-column prop="day" label="日期" /><el-table-column label="上传"><template #default="{ row }">{{ bytes(row.upload) }}</template></el-table-column><el-table-column label="下载"><template #default="{ row }">{{ bytes(row.download) }}</template></el-table-column></el-table></section>
    </template>
  </div>
</template>

<style scoped>
.user-portal{max-width:1120px;margin:0 auto;padding:24px;min-height:100vh}.portal-header,.portal-header>div,.portal-heading,.portal-card-head{display:flex;align-items:center;justify-content:space-between;gap:16px}.portal-header>div{justify-content:flex-start;color:var(--primary);font-size:18px}.portal-header{margin-bottom:40px}.portal-login{max-width:440px;margin:12vh auto;padding:32px}.portal-login p,.portal-heading p,.portal-links p,.portal-package p{color:var(--muted);line-height:1.7}.portal-login .el-form{margin-top:24px}.portal-login-button{width:100%;min-height:44px}.portal-heading{margin-bottom:24px}.portal-heading h1{font-size:28px;margin:8px 0}.portal-links,.portal-history{padding:24px;margin-bottom:24px;min-width:0}.portal-links h2,.portal-history h2,.portal-package h2{font-size:18px;margin:0 0 16px}.portal-link-row{display:grid;grid-template-columns:180px minmax(0,1fr) auto;gap:12px;align-items:center;margin:16px 0}.portal-packages{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:16px;margin-bottom:24px}.portal-package{padding:24px;min-width:0}.portal-package .portal-used{font-size:28px;color:var(--text);font-weight:600}.portal-used small{font-size:14px;color:var(--muted)}.portal-card-head{align-items:flex-start}.portal-node-tags{display:flex;gap:12px;flex-wrap:wrap}.user-portal :deep(.el-button){min-height:44px}.user-portal :deep(.el-input__wrapper){min-height:44px}@media(max-width:760px){.user-portal{padding:16px}.portal-header{margin-bottom:24px}.portal-login{padding:24px;margin:8vh auto}.portal-links,.portal-history,.portal-package{padding:18px}.portal-link-row{grid-template-columns:minmax(0,1fr) auto}.portal-link-row label{grid-column:1 / -1}.portal-heading{align-items:flex-start}.portal-heading h1{font-size:24px}}
</style>
