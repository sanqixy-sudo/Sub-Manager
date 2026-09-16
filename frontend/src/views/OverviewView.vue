<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Activity, AlertTriangle, RefreshCw } from 'lucide-vue-next'
import { useOverviewStore } from '../stores/overview'
import { useAppStore } from '../stores/app'
import { api } from '../api'
import { dt, duration } from '../utils'
import StatusTag from '../components/StatusTag.vue'

const overview = useOverviewStore(), app = useAppStore(), router = useRouter()
const d = computed(() => overview.data || {})
async function load() {
  await Promise.all([overview.load(), api<any>('/api/health').then(value => { app.health = value }).catch(e => ElMessage.error(e.message))])
}
onMounted(load)
</script>

<template>
  <section class="page overview-page">
    <header class="page-head">
      <div><p class="eyebrow">OPERATIONS CENTER</p><h1>运行概览</h1><p>先看异常，再处理需要关注的订阅组。</p></div>
      <el-button :loading="overview.loading" @click="load"><RefreshCw />刷新状态</el-button>
    </header>
    <el-alert v-if="overview.error" :title="overview.error + '；可点击刷新状态重试，已有数据仅供参考。'" type="error" :closable="false" show-icon />
    <el-skeleton v-if="!overview.data && overview.loading" :rows="6" animated />
    <div v-if="app.settings?.default_credentials" class="security-alert danger">
      <AlertTriangle /><div><b>仍在使用默认管理员密码</b><span>请前往系统设置修改。</span></div><el-button @click="router.push('/settings')">立即修改</el-button>
    </div>
    <template v-if="overview.data">
      <div class="stats overview-stats">
        <div class="stat"><div><b>{{ d.groups_enabled }} / {{ d.groups_total }}</b><span>启用订阅组</span></div></div>
        <div class="stat"><div><b>{{ d.upstreams_healthy }} / {{ d.upstreams_total }}</b><span>健康上游</span></div></div>
        <div class="stat"><div><b>{{ d.stale_groups || 0 }}</b><span>旧缓存订阅组</span></div></div>
        <div class="stat"><div><b>{{ d.pending_nodes || 0 }}</b><span>待确认节点</span></div></div>
      </div>
      <div class="ops-grid">
        <section class="panel ops-panel">
          <header><div><h2>需要关注</h2><p>异常、旧缓存和待确认节点。</p></div><RouterLink to="/groups">查看全部</RouterLink></header>
          <div v-if="d.attention_groups?.length" class="attention-list">
            <article v-for="g in d.attention_groups" :key="g.id">
              <StatusTag :status="g.last_refresh_status" /><div><RouterLink :to="`/groups/${g.id}/detail`"><b>{{ g.name }}</b></RouterLink><span>{{ g.last_error || (g.pending_node_count ? `${g.pending_node_count} 个节点待确认` : '输出尚待刷新') }}</span></div><small>{{ g.node_count }} 节点</small>
            </article>
          </div>
          <div v-else-if="d.groups_total" class="compact-empty"><Activity /><b>当前没有待处理异常</b><span>可在节点测活页检查连接可用性。</span></div>
          <div v-else class="compact-empty"><b>还没有订阅组</b><RouterLink to="/groups/new">新建订阅组</RouterLink></div>
        </section>
        <section class="panel ops-panel runtime-panel">
          <header><div><h2>任务与依赖</h2><p>订阅刷新与节点测活分别调度。</p></div></header>
          <div class="runtime-metrics">
            <div><span>定时刷新</span><b>{{ app.health.scheduler_enabled ? '已启用' : '已关闭' }}</b></div>
            <div><span>正在刷新 / 待刷新</span><b>{{ app.health.active_refreshes || 0 }} / {{ app.health.due_groups || 0 }} 组</b></div>
            <div><span>节点测活</span><RouterLink to="/health">{{ app.health.health_check_running ? '查看运行进度' : '查看节点状态' }}</RouterLink></div>
            <div><span>转换器 / 测活内核</span><b>{{ app.health.subconverter ? '正常' : '不可用' }} / {{ app.health.mihomo_available ? '正常' : '不可用' }}</b></div>
          </div>
          <p class="runtime-foot">刷新最近轮询 {{ dt(app.health.last_poll_at) }} · 下次 {{ dt(app.health.next_poll_at) }}</p>
          <p class="runtime-foot">自动测活 {{ app.health.health_check_enabled ? '已启用' : '已关闭' }} · 下次 {{ dt(app.health.health_check_next_run) }}</p>
        </section>
      </div>
      <section class="panel ops-panel recent-panel">
        <header><h2>最近运行</h2><RouterLink to="/runs">完整记录</RouterLink></header>
        <el-table :data="d.recent_runs || []" empty-text="暂无运行记录，可进入订阅组立即刷新">
          <el-table-column prop="subscription_name" label="订阅组" min-width="170" show-overflow-tooltip />
          <el-table-column label="结果" width="110"><template #default="{ row }"><StatusTag :status="row.status" /></template></el-table-column>
          <el-table-column label="来源" width="100"><template #default="{ row }">{{ row.trigger === 'manual' ? '手动' : row.trigger === 'scheduler' ? '定时' : '公共请求' }}</template></el-table-column>
          <el-table-column prop="node_count" label="节点" width="80" />
          <el-table-column label="耗时" width="100"><template #default="{ row }">{{ duration(row.duration_ms) }}</template></el-table-column>
          <el-table-column label="完成时间" min-width="175"><template #default="{ row }">{{ dt(row.finished_at) }}</template></el-table-column>
        </el-table>
      </section>
    </template>
  </section>
</template>
