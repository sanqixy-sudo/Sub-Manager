<script setup lang="ts">
import { computed,onMounted } from 'vue';import { useRoute,useRouter } from 'vue-router';import { LayoutDashboard,Layers3,Settings,ShieldCheck,LogOut,History,Wrench,MoreHorizontal,Activity } from 'lucide-vue-next';import { useAppStore } from './stores/app';import LoginView from './views/LoginView.vue'
const store=useAppStore(),route=useRoute(),router=useRouter();const title=computed(()=>store.settings?.site_name||'Sub Manager')
onMounted(async()=>{await store.checkSession();if(store.authenticated) await store.bootstrap()})
async function logout(){await store.logout();router.replace('/')}
</script>
<template>
  <div v-if="!store.ready" class="boot"><div class="brand-mark"><img src="/brand-logo.png" alt=""/></div><span>正在启动 Sub Manager…</span></div>
  <LoginView v-else-if="!store.authenticated" />
  <div v-else class="shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark"><img src="/brand-logo.png" alt=""/></div><div><strong>{{title}}</strong><small>Subscription Operations</small></div></div>
      <nav>
        <small class="nav-label">运维</small>
        <router-link to="/"><LayoutDashboard/>概览</router-link>
        <router-link to="/groups"><Layers3/>订阅组</router-link>
        <router-link to="/health"><Activity/>节点测活</router-link>
        <router-link to="/runs"><History/>运行记录</router-link>
        <small class="nav-label management">管理</small>
        <router-link to="/tools"><Wrench/>检查工具</router-link>
        <router-link to="/settings"><Settings/>系统设置</router-link>
      </nav>
      <div class="side-status"><div><ShieldCheck/><span><b>服务状态</b><small>{{store.health.subconverter?'转换器运行正常':'转换器不可用'}}</small></span></div><i :class="{ok:store.health.subconverter}"></i></div>
    </aside>
    <header class="mobile-head"><div class="brand-mark"><img src="/brand-logo.png" alt=""/></div><div><b>{{title}}</b><small>{{route.meta.title}}</small></div><button @click="logout"><LogOut/></button></header>
    <main><router-view v-slot="{Component}"><transition name="page-fade" mode="out-in"><component :is="Component" :key="route.path"/></transition></router-view></main>
    <nav class="mobile-nav">
      <router-link to="/"><LayoutDashboard/><span>概览</span></router-link>
      <router-link to="/groups"><Layers3/><span>订阅组</span></router-link>
      <router-link to="/health"><Activity/><span>测活</span></router-link>
      <el-popover placement="top-end" trigger="click" :width="170"><template #reference><button><MoreHorizontal/><span>更多</span></button></template><div class="more-links"><router-link to="/runs"><History/>运行记录</router-link><router-link to="/tools"><Wrench/>检查工具</router-link><router-link to="/settings"><Settings/>系统设置</router-link><button @click="logout"><LogOut/>退出登录</button></div></el-popover>
    </nav>
  </div>
</template>
