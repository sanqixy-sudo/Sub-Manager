import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter,createWebHashHistory } from 'vue-router'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import './style.css'
import './stability.css'
import App from './App.vue'

const router=createRouter({history:createWebHashHistory(),routes:[
  {path:'/',component:()=>import('./views/OverviewView.vue'),meta:{title:'概览'}},
  {path:'/groups',component:()=>import('./views/DashboardView.vue'),meta:{title:'订阅组'}},
  {path:'/groups/new',component:()=>import('./views/EditorV330.vue'),meta:{title:'新建订阅组'}},
  {path:'/groups/:id/detail',component:()=>import('./views/GroupDetailView.vue'),meta:{title:'组详情'}},
  {path:'/groups/:id/edit',component:()=>import('./views/EditorV330.vue'),meta:{title:'编辑订阅组'}},
  {path:'/groups/:id',redirect:to=>`/groups/${to.params.id}/edit`},
  {path:'/runs',component:()=>import('./views/RunsView.vue'),meta:{title:'运行记录'}},
  {path:'/health',component:()=>import('./views/HealthView.vue'),meta:{title:'节点测活'}},
  {path:'/groups/:id/categories',component:()=>import('./views/CategoriesView.vue'),meta:{title:'分类管理'}},
  {path:'/tools',component:()=>import('./views/ToolsView.vue'),meta:{title:'检查工具'}},
  {path:'/settings',component:()=>import('./views/SettingsView.vue'),meta:{title:'系统设置'}},
  {path:'/status/:token',component:()=>import('./views/StatusView.vue'),meta:{title:'节点状态',public:true}},
  {path:'/:pathMatch(.*)*',redirect:'/'},
]})
createApp(App).use(createPinia()).use(router).use(ElementPlus,{locale:zhCn}).mount('#app')
