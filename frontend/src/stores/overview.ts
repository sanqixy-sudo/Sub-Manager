import { defineStore } from 'pinia'
import { api } from '../api'

export const useOverviewStore = defineStore('overview', {
  state: () => ({ data: null as any, loading: false, error: '' }),
  actions: {
    async load() {
      if (this.loading) return
      this.loading = true
      try { this.data = await api('/api/overview'); this.error = '' }
      catch (e: any) { this.error = e.message || '概览加载失败，请重试' }
      finally { this.loading = false }
    },
  },
})
