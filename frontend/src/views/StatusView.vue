<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Activity } from 'lucide-vue-next'
import { api } from '../api'
import { dt } from '../utils'
import { color, label, latency, latencyClass, slotBlocks, type HealthHistoryItem } from '../utils/health'

type Node = {
  node_key: string
  final_name: string
  protocol: string
  status: string
  connectivity_latency_ms?: number
  google_ok?: boolean
  google_latency_ms?: number
  tested_at?: string
  history: HealthHistoryItem[]
}

const route = useRoute()
const token = String(route.params.token || '')
const data = ref<{ group_name?: string; total: number; counts: Record<string, number>; generated_at?: string; nodes: Node[] } | null>(null)
const error = ref('')
const loading = ref(true)
const timer = ref<number>()

const stats = computed(() => {
  const counts = data.value?.counts || {}
  const total = data.value?.total || 0
  const available = (counts.healthy || 0) + (counts.google_blocked || 0) + (counts.connectivity_target_failed || 0)
  return [
    { label: '全部节点', value: total, cls: 'blue' },
    { label: '代理可用', value: available, cls: 'green' },
    { label: '不可用', value: counts.unavailable || 0, cls: 'red' },
    { label: '未测试', value: counts.untested || 0, cls: 'gray' },
  ]
})

async function load() {
  try {
    data.value = await api(`/api/public/health/${token}`)
    error.value = ''
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  timer.value = window.setInterval(load, 60_000)
})

onBeforeUnmount(() => window.clearInterval(timer.value))
</script>

<template>
  <div class="status-page">
    <header class="status-head">
      <div class="brand">
        <div class="brand-mark"><img src="/brand-logo.png" alt="" /></div>
        <div>
          <strong>{{ data?.group_name || '节点状态' }}</strong>
          <small>NODE STATUS</small>
        </div>
      </div>
      <span v-if="data?.generated_at" class="status-updated">更新于 {{ dt(data.generated_at) }}</span>
    </header>

    <div v-if="error" class="panel status-error">
      <Activity />
      <div>
        <b>状态页不可用</b>
        <span>{{ error }}</span>
      </div>
    </div>

    <template v-else>
      <div class="health-stats">
        <article v-for="s in stats" :key="s.label" class="panel">
          <i :class="s.cls"></i>
          <div>
            <b>{{ s.value }}</b>
            <span>{{ s.label }}</span>
          </div>
        </article>
      </div>
      <div class="health-legend">
        <span><i class="lat-good"></i>快（&lt;300ms）</span>
        <span><i class="lat-mid"></i>中（&lt;800ms）</span>
        <span><i class="lat-bad"></i>慢（≥800ms）</span>
        <span><i class="red"></i>失败</span>
        <span><i class="gray"></i>无记录</span>
      </div>
      <div v-loading="loading" class="health-cards status-cards">
        <article v-for="node in data?.nodes || []" :key="node.node_key" class="panel health-card">
          <header>
            <div class="health-node">
              <b :title="node.final_name">{{ node.final_name }}</b>
              <small>{{ node.protocol.toUpperCase() }} · {{ dt(node.tested_at) }}</small>
            </div>
            <span class="health-status" :class="color(node.status)"><i></i>{{ label(node.status) }}</span>
          </header>
          <div class="health-card-latency">
            <span>Cloudflare <b :class="latencyClass(node.connectivity_latency_ms)">{{ latency(node.connectivity_latency_ms) }}</b></span>
            <span>Google <b :class="node.google_ok ? latencyClass(node.google_latency_ms) : node.tested_at ? 'lat-bad' : 'lat-none'">{{ node.google_ok ? latency(node.google_latency_ms) : node.tested_at ? '失败' : '—' }}</b></span>
          </div>
          <div class="health-blocks health-card-blocks">
            <i
              v-for="(b, i) in slotBlocks(node.history, 24)"
              :key="i"
              :class="[b.cls, b.status]"
              :title="b.tip"
            ></i>
          </div>
        </article>
        <p v-if="!loading && !(data?.nodes || []).length" class="health-cards-empty">暂无节点。</p>
      </div>
    </template>

    <footer class="status-foot">每 60 秒自动刷新 · 近 24 小时测活记录</footer>
  </div>
</template>
