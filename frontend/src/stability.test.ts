import { afterEach, describe, expect, it, vi } from 'vitest'
import { slotBlocks } from './utils/health'
import { api } from './api'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('health history accuracy', () => {
  it('never labels a skipped-only slot healthy', () => {
    const slots = slotBlocks([{ status: 'skipped', tested_at: new Date().toISOString() }], 24)
    const occupied = slots.filter(s => s.status)
    expect(occupied).toHaveLength(1)
    expect(occupied[0].status).toBe('skipped')
    expect(occupied[0].cls).toBe('gray')
    expect(occupied[0].tip).toContain('已跳过')
  })
  it('does not fabricate test samples in empty slots', () => {
    const slots = slotBlocks([], 720)
    expect(slots).toHaveLength(48)
    expect(slots.every(s => s.status === '' && s.tip.includes('无数据'))).toBe(true)
  })
  it('keeps real failure in a slot also containing a skip', () => {
    const at = new Date().toISOString()
    expect(slotBlocks([{ status: 'unavailable', tested_at: at }, { status: 'skipped', tested_at: at }], 24).at(-1)?.status).toBe('unavailable')
  })
})

describe('request lifecycle', () => {
  it('coalesces concurrent shared GETs', async () => {
    const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ value: 1 }) }))
    vi.stubGlobal('fetch', fetch)
    const responses = await Promise.all([api('/test/shared'), api('/test/shared')])
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(responses).toEqual([{ value: 1 }, { value: 1 }])
  })
  it('keeps cancellable requests independent', async () => {
    const fetch = vi.fn(async () => ({ ok: true, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetch)
    await Promise.all([api('/test/isolated', { signal: new AbortController().signal }), api('/test/isolated', { signal: new AbortController().signal })])
    expect(fetch).toHaveBeenCalledTimes(2)
  })
  it('dispatches session expiration for an authenticated endpoint', async () => {
    const dispatchEvent = vi.fn()
    vi.stubGlobal('window', { dispatchEvent })
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })))
    await expect(api('/api/subscriptions')).rejects.toThrow('登录已过期')
    expect(dispatchEvent.mock.calls[0][0].type).toBe('session-expired')
  })
  it('does not turn intentional cancellation into a network failure', async () => {
    vi.stubGlobal('fetch', vi.fn((_path, opts) => new Promise((_, reject) => opts.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))))))
    const controller = new AbortController()
    const result = api('/test/cancel', { signal: controller.signal })
    controller.abort()
    await expect(result).rejects.toMatchObject({ name: 'AbortError' })
  })
})
