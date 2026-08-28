<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Copy, FlaskConical, Play, ShieldCheck, Sparkles, Trash2 } from 'lucide-vue-next'
import { api } from '../api'
import { copyText } from '../utils'

const form = reactive({
  content: '',
  source_name: '临时输入',
  rename_mode: 'smart',
  rename_ignore: '',
  rename_template: '{index}-{flag}-{name}-{traffic}-{reset}',
})
const result = ref<any>(null)
const error = ref('')
const loading = ref(false)

async function inspect() {
  loading.value = true
  result.value = null
  error.value = ''
  try {
    result.value = await api('/api/tools/inspect', {
      method: 'POST',
      body: JSON.stringify(form),
    })
  } catch (e: any) {
    error.value = e.message || '解析失败'
    ElMessage.error(error.value)
  } finally {
    loading.value = false
  }
}

function clear() {
  form.content = ''
  result.value = null
  error.value = ''
}

function fillExample() {
  form.source_name = '示例机场'
  form.rename_mode = 'smart'
  form.rename_ignore = 'DMIT-US'
  form.rename_template = '{index}-{flag}-{name}-{traffic}-{reset}'
  form.content = [
    'ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4Mzg4#DMIT-US-04-HK-Home|📊500.00GB|⌛25D',
    'vless://00000000-0000-0000-0000-000000000000@example.com:443?encryption=none&security=tls&type=ws#DMIT-US-01-JP-Edge|📊1.00TB|⌛12D',
  ].join('\n')
  result.value = null
  error.value = ''
  ElMessage.success('已填入示例内容，可直接点击「开始检查」')
}

async function copyResult() {
  if (!result.value?.nodes?.length) return
  await copyText(result.value.nodes.map((n: any) => n.final_name).join('\n'))
  ElMessage.success(`已复制 ${result.value.nodes.length} 个节点名称`)
}
</script>

<template>
  <section class="page">
    <header class="page-head">
      <div>
        <p class="eyebrow">SAFE INSPECTOR</p>
        <h1>检查工具</h1>
        <p>在不创建订阅组的情况下检查格式、过滤、去重与节点改名。</p>
      </div>
    </header>
    <div class="tool-grid">
      <section class="panel form-section">
        <div class="settings-title">
          <FlaskConical />
          <div>
            <h2>解析与改名沙盒</h2>
            <p>内容仅在本次请求内处理，不写入数据库、缓存或日志。</p>
          </div>
        </div>
        <label>来源名称<el-input v-model="form.source_name" /></label>
        <label>
          订阅内容
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="14"
            placeholder="粘贴 Clash YAML、base64、URI 列表或单节点内容"
          />
        </label>
        <div class="form-grid">
          <label>
            改名模式
            <el-select v-model="form.rename_mode">
              <el-option label="原样透传" value="passthrough" />
              <el-option label="智能规则" value="smart" />
            </el-select>
          </label>
          <label>
            忽略前缀
            <el-input v-model="form.rename_ignore" placeholder="例如 DMIT-US，可留空" />
          </label>
        </div>
        <label v-if="form.rename_mode === 'smart'">
          改名模板
          <el-input v-model="form.rename_template" />
        </label>
        <div class="tool-actions">
          <el-button @click="fillExample"><Sparkles />填充示例</el-button>
          <el-button @click="clear"><Trash2 />清空</el-button>
          <el-button
            type="primary"
            :loading="loading"
            :disabled="!form.content.trim()"
            @click="inspect"
          >
            <Play />开始检查
          </el-button>
        </div>
        <div class="safe-note">
          <ShieldCheck />
          <span>结果只返回节点名称、来源和协议，不返回服务器、端口或连接凭据。</span>
        </div>
      </section>
      <section class="panel tool-result">
        <template v-if="result">
          <header>
            <div>
              <p class="eyebrow">INSPECTION RESULT</p>
              <h2>{{ result.source_format }}</h2>
            </div>
            <div class="result-counts">
              <span><b>{{ result.node_count }}</b>有效节点</span>
              <span><b>{{ result.filtered_count }}</b>过滤/去重</span>
            </div>
            <el-button class="result-copy" @click="copyResult"><Copy />复制结果</el-button>
          </header>
          <el-table :data="result.nodes" max-height="650">
            <el-table-column prop="position" label="#" width="58" />
            <el-table-column prop="protocol" label="协议" width="100" />
            <el-table-column prop="source_name" label="来源" width="120" show-overflow-tooltip />
            <el-table-column prop="original_name" label="原名" min-width="180" show-overflow-tooltip />
            <el-table-column prop="final_name" label="改名后" min-width="190" show-overflow-tooltip />
          </el-table>
        </template>
        <el-alert
          v-else-if="error"
          class="tool-error"
          type="error"
          :title="`解析失败：${error}`"
          description="请检查粘贴的内容是否为完整的订阅或节点，修改后重新运行。"
          :closable="false"
          show-icon
        />
        <div v-else class="empty tool-empty">
          <FlaskConical />
          <h3>等待检查内容</h3>
          <p>粘贴内容并运行后，结果会显示在这里。也可以先点「填充示例」试跑一次。</p>
        </div>
      </section>
    </div>
  </section>
</template>
