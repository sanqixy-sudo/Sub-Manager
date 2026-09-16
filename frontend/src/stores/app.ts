import { defineStore } from 'pinia'
import { api } from '../api'
import type { ClientType, Group, Settings } from '../types'

export const useAppStore=defineStore('app',{
  state:()=>({authenticated:false,ready:false,groups:[] as Group[],settings:null as Settings|null,types:{} as Record<string,ClientType>,meta:{} as any,health:{} as any}),
  actions:{
    async checkSession(){try{await api('/api/auth/session');this.authenticated=true}catch{this.authenticated=false}finally{this.ready=true}},
    async bootstrap(){if(this.settings && Object.keys(this.types).length) return; const [settings,types,meta,health]=await Promise.all([api<Settings>('/api/settings'),api<Record<string,ClientType>>('/api/client-types'),api<Record<string,any>>('/api/meta'),api<Record<string,any>>('/api/health')]);Object.assign(this,{settings,types,meta,health});document.title=`${settings.site_name} V${meta.version}`},
    async loadGroups(){this.groups=await api<Group[]>('/api/subscriptions')},
    async load(){await Promise.all([this.bootstrap(),this.loadGroups()])},
    async login(username:string,password:string){await api('/api/auth/login',{method:'POST',body:JSON.stringify({username,password})});this.authenticated=true;await this.bootstrap()},
    async logout(){try{await api('/api/auth/logout',{method:'POST'})}finally{this.authenticated=false;this.groups=[];this.settings=null;this.types={};this.meta={};this.health={}}},
  }
})
