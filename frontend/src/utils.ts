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

/** 刷新状态排序权重：error→stale→partial→empty→ok，未知状态排最后 */
export function statusPriority(status?: string): number {
  const weights: Record<string, number> = { error: 0, stale: 1, partial: 2, empty: 3, ok: 4 }
  return weights[status || 'empty'] ?? 9
}
