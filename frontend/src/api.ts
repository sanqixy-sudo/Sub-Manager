export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message)
  }
}

const pendingGets = new Map<string, Promise<unknown>>()

/** 按 HTTP 状态码映射为面向用户的友好文案，未命中时保留后端 detail 原文 */
function friendlyMessage(detail: unknown, status: number): string {
  if (status === 401) return '登录已过期，请重新登录'
  if (status === 403) return '没有权限执行此操作'
  if (status === 404) return '资源不存在或已删除'
  if (status === 413) return '请求内容过大'
  if (status === 429) return '请求过于频繁，请稍后再试'
  if (status >= 500) return `服务器开小差了（${status}），请稍后重试`
  return typeof detail === 'string' && detail ? detail : `请求失败 (${status})`
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || 'GET').toUpperCase()
  const share = method === 'GET' && !options.signal
  if (share && pendingGets.has(path)) return pendingGets.get(path) as Promise<T>
  const run = (async () => {
    let response: Response
    const controller = new AbortController()
    const abort = () => controller.abort()
    options.signal?.addEventListener('abort', abort, { once: true })
    if (options.signal?.aborted) controller.abort()
    const timeout = globalThis.setTimeout(abort, method === 'GET' ? 30000 : 660000)
    try {
      response = await fetch(path, {
        credentials: 'same-origin',
        ...options,
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      })
      let data: any
      try { data = await response.json() }
      catch {
        if (controller.signal.aborted) throw new DOMException('aborted', 'AbortError')
        throw new ApiError('服务器返回了无效数据，请重试', response.status)
      }
      if (!response.ok) {
        if (response.status === 401 && path !== '/api/auth/login') window.dispatchEvent(new Event('session-expired'))
        throw new ApiError(friendlyMessage(data?.detail, response.status), response.status)
      }
      if (data == null) throw new ApiError('服务器返回了空数据，请重试', response.status)
      return data as T
    } catch (error) {
      if (error instanceof ApiError) throw error
      if (options.signal?.aborted) throw error
      if (controller.signal.aborted) throw new ApiError('请求超时，请稍后重试；后台任务可能仍在运行', 0)
      // fetch 抛出 TypeError 说明网络层失败（服务未运行 / 断网 / 跨域被拦截）
      throw new ApiError('网络连接失败，请检查服务是否运行', 0)
    } finally {
      globalThis.clearTimeout(timeout)
      options.signal?.removeEventListener('abort', abort)
    }
  })()
  if (share) pendingGets.set(path, run)
  try {
    return await run
  } finally {
    if (share) pendingGets.delete(path)
  }
}
