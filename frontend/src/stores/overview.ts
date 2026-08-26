import {defineStore} from 'pinia';import{api}from'../api'
export const useOverviewStore=defineStore('overview',{state:()=>({data:null as any,loading:false}),actions:{async load(){this.loading=true;try{this.data=await api('/api/overview')}finally{this.loading=false}}}})
