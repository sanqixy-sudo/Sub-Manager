import { dt } from '../utils'

export interface HealthHistoryItem {
  status: string
  connectivity_latency_ms?: number
  tested_at?: string
}

/** 延迟格式化，空值显示 — */
export function latency(v?: number) {
  return v ? `${v} ms` : '—'
}

/** 延迟按质量着色：<300ms 绿、<800ms 黄、≥800ms 红、无值灰 */
export function latencyClass(v?: number) {
  if (!v) return 'lat-none'
  if (v < 300) return 'lat-good'
  if (v < 800) return 'lat-mid'
  return 'lat-bad'
}

export function label(s: string) {
  return ({
    healthy: '正常',
    google_blocked: 'Google 受限',
    connectivity_target_failed: 'Cloudflare 异常',
    unavailable: '不可用',
    untested: '未测试',
    skipped: '已跳过',
  } as Record<string, string>)[s] || s
}

export function color(s: string) {
  return ({
    healthy: 'green',
    google_blocked: 'yellow',
    connectivity_target_failed: 'blue',
    unavailable: 'red',
    skipped: 'gray',
  } as Record<string, string>)[s] || 'gray'
}

/** 色块时间槽位：固定 48 格铺满整个时间窗口，每格取最差状态、最高延迟 */
export const SLOT_COUNT = 48
const SLOT_SEVERITY: Record<string, number> = {
  unavailable: 4,
  connectivity_target_failed: 3,
  google_blocked: 2,
  healthy: 1,
}

export function slotBlocks(history: HealthHistoryItem[], hours: number) {
  const spanMs = hours * 3600_000
  const slotMs = spanMs / SLOT_COUNT
  const end = Date.now()
  const start = end - spanMs
  const slots: ({ status: string; lat: number; count: number } | undefined)[] = []
  for (const h of history) {
    const t = new Date(h.tested_at || '').getTime()
    if (Number.isNaN(t) || t < start || t > end) continue
    const idx = Math.min(SLOT_COUNT - 1, Math.floor((t - start) / slotMs))
    const slot = slots[idx] || (slots[idx] = { status: 'healthy', lat: 0, count: 0 })
    slot.count += 1
    if ((SLOT_SEVERITY[h.status] || 0) >= (SLOT_SEVERITY[slot.status] || 0)) slot.status = h.status
    slot.lat = Math.max(slot.lat, h.connectivity_latency_ms || 0)
  }
  const slotMin = Math.max(1, Math.round(slotMs / 60000))
  return Array.from({ length: SLOT_COUNT }, (_, i) => {
    const at = dt(new Date(start + i * slotMs).toISOString())
    const slot = slots[i]
    if (!slot) return { cls: 'gray', status: '', tip: `${at} 起 ${slotMin} 分钟 · 无数据` }
    const cls = slot.status === 'healthy' ? latencyClass(slot.lat || undefined).replace('lat-none', 'gray') : color(slot.status)
    return {
      cls,
      status: slot.status,
      tip: `${at} 起 ${slotMin} 分钟 · ${slot.count} 次 · ${label(slot.status)}${slot.lat ? ` · 最高 ${slot.lat} ms` : ''}`,
    }
  })
}
