<script setup lang="ts">
import { onMounted, reactive } from 'vue'
import { RefreshCw } from 'lucide-vue-next'
import { useRunsStore } from '../stores/runs'
import { useAppStore } from '../stores/app'
import { dt, duration } from '../utils'
import StatusTag from '../components/StatusTag.vue'

const runs = useRunsStore()
const app = useAppStore()
const filters = reactive({ subscription_id: '', status: '', trigger: '', page: 1, page_size: 30 })
const load = () => runs.load(filters)

onMounted(async () => {
  if (!app.groups.length) await app.loadGroups()
  await load()
})
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">REFRESH HISTORY</p>
        <h1>运行记录</h1>
        <p>追踪定时、手动和公共请求触发的每一次真实刷新。</p>
      </div>
      <el-button :loading="runs.loading" @click="load"><RefreshCw />刷新</el-button>
    </header>
    <div class="panel">
      <div class="filter-bar">
        <el-select
          v-model="filters.subscription_id"
          clearable
          placeholder="全部订阅组"
          @change="filters.page = 1; load()"
        >
          <el-option v-for="g in app.groups" :key="g.id" :label="g.name" :value="g.id" />
        </el-select>
        <el-select
          v-model="filters.status"
          clearable
          placeholder="全部状态"
          @change="filters.page = 1; load()"
        >
          <el-option label="正常" value="ok" />
          <el-option label="部分异常" value="partial" />
          <el-option label="旧缓存" value="stale" />
          <el-option label="失败" value="error" />
        </el-select>
        <el-select
          v-model="filters.trigger"
          clearable
          placeholder="全部来源"
          @change="filters.page = 1; load()"
        >
          <el-option label="手动" value="manual" />
          <el-option label="定时" value="scheduler" />
          <el-option label="公共请求" value="public" />
        </el-select>
      </div>
      <div class="desktop-table">
        <el-table
          v-loading="runs.loading"
          :data="runs.items"
          empty-text="暂无符合条件的运行记录"
        >
          <el-table-column prop="subscription_name" label="订阅组" min-width="170" />
          <el-table-column label="状态" width="116">
            <template #default="{ row }"><StatusTag :status="row.status" /></template>
          </el-table-column>
          <el-table-column label="来源" width="100">
            <template #default="{ row }">
              {{ row.trigger === 'manual' ? '手动' : row.trigger === 'scheduler' ? '定时' : '公共请求' }}
            </template>
          </el-table-column>
          <el-table-column label="上游" width="100">
            <template #default="{ row }">{{ row.upstream_success }} / {{ row.upstream_total }}</template>
          </el-table-column>
          <el-table-column label="输出" width="100">
            <template #default="{ row }">{{ row.output_success }} / {{ row.output_total }}</template>
          </el-table-column>
          <el-table-column label="节点" width="100">
            <template #default="{ row }">
              {{ row.node_count }} <small v-if="row.filtered_count" class="filtered">−{{ row.filtered_count }}</small>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="100">
            <template #default="{ row }">{{ duration(row.duration_ms) }}</template>
          </el-table-column>
          <el-table-column label="完成时间" min-width="170">
            <template #default="{ row }">{{ dt(row.finished_at) }}</template>
          </el-table-column>
          <el-table-column prop="error" label="错误摘要" min-width="220" show-overflow-tooltip />
        </el-table>
      </div>
      <div class="run-cards">
        <article v-for="row in runs.items" :key="row.id">
          <header>
            <b>{{ row.subscription_name }}</b>
            <StatusTag :status="row.status" />
          </header>
          <p>{{ row.error || '刷新完成，无错误' }}</p>
          <footer>
            <span>{{ dt(row.finished_at) }}</span>
            <span>{{ duration(row.duration_ms) }}</span>
          </footer>
        </article>
      </div>
      <el-pagination
        v-model:current-page="filters.page"
        :page-size="filters.page_size"
        :total="runs.total"
        layout="prev, pager, next, total"
        @current-change="load"
      />
    </div>
  </section>
</template>
