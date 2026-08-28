import{describe,expect,it}from'vitest';import{parseBulkUpstreams,dt,duration,size,statusPriority}from'./utils';describe('parseBulkUpstreams',()=>{it('supports named and plain URLs',()=>{const rows=parseBulkUpstreams('机场 A | https://a.invalid/sub\nhttps://b.invalid/s\ninvalid',3);expect(rows).toHaveLength(2);expect(rows[0].name).toBe('机场 A');expect(rows[1].name).toBe('上游 4')});it('handles full-width separators',()=>{expect(parseBulkUpstreams('备用｜https://c.invalid/sub')[0].name).toBe('备用')})});

describe('dt', () => {
  it('returns em dash for empty values', () => {
    expect(dt()).toBe('—')
    expect(dt('')).toBe('—')
  })
  it('formats dates in zh-CN without hour12', () => {
    const out = dt('2026-08-24T15:04:05')
    expect(out).toContain('2026')
    expect(out).not.toMatch(/AM|PM/i)
  })
})

describe('duration', () => {
  it('returns em dash for nullish input', () => {
    expect(duration()).toBe('—')
  })
  it('shows ms below 1000', () => {
    expect(duration(0)).toBe('0 ms')
    expect(duration(320)).toBe('320 ms')
    expect(duration(999)).toBe('999 ms')
  })
  it('shows seconds at and above 1000', () => {
    expect(duration(1000)).toBe('1.0 s')
    expect(duration(1530)).toBe('1.5 s')
  })
})

describe('size', () => {
  it('formats bytes below 1 KB as B', () => {
    expect(size(0)).toBe('0 B')
    expect(size(1024)).toBe('1024 B')
  })
  it('formats KB range', () => {
    expect(size(2048)).toBe('2.0 KB')
    expect(size(1048576)).toBe('1024.0 KB')
  })
  it('formats MB range', () => {
    expect(size(1048577)).toBe('1.0 MB')
    expect(size(15 * 1048576)).toBe('15.0 MB')
  })
  it('formats GB range', () => {
    expect(size(1073741825)).toBe('1.0 GB')
    expect(size(2.5 * 1073741824)).toBe('2.5 GB')
  })
})

describe('statusPriority', () => {
  it('orders error before stale, partial, empty and ok', () => {
    const statuses = ['ok', 'empty', 'partial', 'stale', 'error']
    expect([...statuses].sort((a, b) => statusPriority(a) - statusPriority(b)))
      .toEqual(['error', 'stale', 'partial', 'empty', 'ok'])
  })
  it('treats missing status as empty', () => {
    expect(statusPriority()).toBe(statusPriority('empty'))
  })
  it('puts unknown statuses last', () => {
    expect(statusPriority('weird')).toBeGreaterThan(statusPriority('ok'))
  })
})
