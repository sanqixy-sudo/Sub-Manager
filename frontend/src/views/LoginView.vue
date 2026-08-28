<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { LockKeyhole, ArrowRight } from 'lucide-vue-next'
import { useAppStore } from '../stores/app'

const store = useAppStore()
const username = ref('')
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!username.value || !password.value) {
    ElMessage.warning('请输入管理员用户名和密码')
    return
  }
  loading.value = true
  try {
    await store.login(username.value, password.value)
    password.value = ''
  } catch (e: any) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-art">
      <div class="art-content">
        <div class="brand-mark large"><img src="/brand-logo-white.png" alt="Sub Manager" /></div>
        <h1>订阅管理，保持简单。</h1>
        <p>集中维护真实上游，为每台客户端提供稳定、私密的固定地址。</p>
        <div class="feature-lines">
          <span>多上游统一合并</span>
          <span>自动刷新与 last-good</span>
          <span>完整多客户端输出</span>
        </div>
      </div>
    </div>

    <div class="login-panel">
      <form class="login-card" autocomplete="off" @submit.prevent="submit">
        <div class="login-icon"><LockKeyhole /></div>
        <p class="eyebrow">SUB MANAGER V3</p>
        <h2>欢迎回来</h2>
        <p class="muted">登录到私人订阅运维后台</p>
        <label>
          管理员用户名
          <el-input v-model="username" name="sm-user-entry" size="large" autocomplete="off" placeholder="输入管理员用户名" />
        </label>
        <label>
          管理员密码
          <el-input v-model="password" name="sm-secret-entry" type="password" show-password size="large" autocomplete="new-password" placeholder="输入管理员密码" />
        </label>
        <el-button native-type="submit" type="primary" size="large" :loading="loading">
          登录 <ArrowRight />
        </el-button>
      </form>
    </div>
  </div>
</template>
