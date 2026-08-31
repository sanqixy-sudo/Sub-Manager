<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Copy } from 'lucide-vue-next'
import QRCode from 'qrcode'
import { copyText } from '../utils'

// P2 扫码导入：把固定订阅地址渲染为二维码，客户端扫码或手动粘贴均可
const props = defineProps<{ modelValue: boolean; title: string; url: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const canvas = ref<HTMLCanvasElement | null>(null)
const rendering = ref(false)

watch(
  () => [props.modelValue, props.url],
  async ([visible]) => {
    if (!visible || !props.url) return
    rendering.value = true
    try {
      await nextTick() // 等 el-dialog 内容挂载后再取 canvas
      if (!canvas.value) return
      await QRCode.toCanvas(canvas.value, props.url, {
        errorCorrectionLevel: 'M',
        width: 260,
        margin: 1,
      })
    } catch {
      ElMessage.error('二维码生成失败，请直接复制地址')
    } finally {
      rendering.value = false
    }
  },
)

async function copy() {
  await copyText(props.url)
  ElMessage.success('订阅地址已复制')
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="`扫码导入 · ${title}`"
    width="min(340px, 92vw)"
    class="qr-dialog"
    append-to-body
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-loading="rendering" class="qr-body">
      <canvas ref="canvas" class="qr-canvas" />
      <code class="qr-url">{{ url }}</code>
    </div>
    <template #footer>
      <el-button type="primary" @click="copy"><Copy />复制地址</el-button>
    </template>
  </el-dialog>
</template>
