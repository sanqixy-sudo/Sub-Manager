export interface ParsedUpstream { name:string; url:string; enabled:true }
export function parseBulkUpstreams(raw:string,startIndex=1):ParsedUpstream[]{const results:ParsedUpstream[]=[];for(const source of raw.split(/\r?\n/)){const line=source.trim();if(!line)continue;const match=line.match(/^(.+?)\s*[|｜]\s*(https?:\/\/\S+)$/);const url=match?match[2]:line;if(!/^https?:\/\/\S+$/i.test(url))continue;results.push({name:match?.[1].trim()||`上游 ${startIndex+results.length}`,url,enabled:true})}return results}
export async function copyText(value:string):Promise<void>{if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(value);return}const input=document.createElement('textarea');input.value=value;input.style.position='fixed';input.style.opacity='0';document.body.appendChild(input);input.select();document.execCommand('copy');input.remove()}

/** zh-CN 时间格式化，空值返回 '—' */
export function dt(v?: string | number | Date): string {
  if (!v) return '—'
  return new Date(v).toLocaleString('zh-CN', { hour12: false })
}

/** 耗时格式化：<1000 显示 ms，否则 s */
export function duration(ms?: number): string {
  if (ms == null) return '—'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

/** 字节数格式化为 KB/MB/GB */
export function size(bytes: number): string {
  if (bytes > 1073741824) return `${(bytes / 1073741824).toFixed(1)} GB`
  if (bytes > 1048576) return `${(bytes / 1048576).toFixed(1)} MB`
  if (bytes > 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

export interface ImportScheme { label: string; scheme: string }

/** 按客户端输出类型返回一键导入项；conf 类客户端（surge/quantumult/loon 等）没有可靠 URL scheme，返回空 */
export function importSchemes(clientType: string, url: string): ImportScheme[] {
  if (['mihomo', 'clash', 'clashr'].includes(clientType)) {
    return [{ label: '导入到 Clash / Mihomo', scheme: `clash://install-config?url=${encodeURIComponent(url)}` }]
  }
  if (['v2ray', 'ss', 'sssub', 'ssd'].includes(clientType)) {
    // btoa 只接受 Latin-1 字符，URL 含中文等非 ASCII 时先 encodeURI 转成百分号编码
    return [{ label: '导入到 Shadowrocket 等', scheme: `sub://${btoa(encodeURI(url))}` }]
  }
  return []
}

/** 该客户端类型是否支持一键导入 */
export function supportsImport(clientType: string): boolean {
  return ['mihomo', 'clash', 'clashr', 'v2ray', 'ss', 'sssub', 'ssd'].includes(clientType)
}

/** 尝试唤起外部客户端导入 scheme；waitMs 后页面仍可见（未切到客户端）视为未唤起，返回 false 由调用方兜底 */
export function openImportScheme(scheme: string, waitMs = 800): Promise<boolean> {
  window.location.href = scheme
  return new Promise(resolve => window.setTimeout(() => resolve(document.visibilityState !== 'visible'), waitMs))
}

/** 刷新状态排序权重：error→stale→partial→empty→ok，未知状态排最后 */
export function statusPriority(status?: string): number {
  const weights: Record<string, number> = { error: 0, stale: 1, partial: 2, empty: 3, ok: 4 }
  return weights[status || 'empty'] ?? 9
}
