/** FastAPI 网关客户端：JWT 会话、统一错误（含 429 Retry-After）、图片 blob 拉取。 */
import type {
  AnalyzeResult,
  ConfirmResult,
  DashboardStats,
  DetectionAnswerResult,
  DetectionStart,
  DocumentImport,
  Question,
  SegmentsResult,
  User,
} from '@/types'

const BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000/api'
const TOKEN_KEY = 'mm_token'

export class ApiError extends Error {
  status: number
  retryAfter: number | null
  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message)
    this.status = status
    this.retryAfter = retryAfter
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

/** 401 时通知 App 层登出（避免循环依赖，用自定义事件）。 */
export const AUTH_EXPIRED_EVENT = 'mm-auth-expired'

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  let resp: Response
  try {
    resp = await fetch(`${BASE}${path}`, { ...init, headers })
  } catch {
    throw new ApiError(0, '连不上服务端，请确认 API 网关已启动')
  }
  if (resp.status === 401) {
    setToken(null)
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
    throw new ApiError(401, '登录已过期，请重新登录')
  }
  if (resp.status === 429) {
    const retry = Number(resp.headers.get('Retry-After') ?? '60')
    throw new ApiError(429, `操作太频繁，请 ${retry} 秒后再试`, retry)
  }
  if (!resp.ok) {
    let detail = `请求失败（${resp.status}）`
    try {
      const body = await resp.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new ApiError(resp.status, detail)
  }
  if (resp.status === 204) return undefined as T
  return resp.json() as Promise<T>
}

export const api = {
  async login(username: string, password: string): Promise<User> {
    const data = await request<{ access_token: string } & User>('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    setToken(data.access_token)
    return { user_id: data.user_id, username: data.username, role: data.role }
  },

  me: () => request<User>('/auth/me'),

  due: () => request<Question[]>('/review/due'),

  grade: (questionId: number, grade: 'again' | 'hard' | 'good' | 'easy') =>
    request<Question>(`/review/${questionId}/grade`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grade }),
    }),

  listQuestions: (params: { keyword?: string; offset?: number; limit?: number }) => {
    const search = new URLSearchParams()
    if (params.keyword) search.set('keyword', params.keyword)
    search.set('offset', String(params.offset ?? 0))
    search.set('limit', String(params.limit ?? 20))
    return request<Question[]>(`/questions?${search.toString()}`)
  },

  analyze: (file: File, extra: { tags?: string; hint?: string }) => {
    const form = new FormData()
    form.append('image', file)
    form.append('tags', extra.tags ?? '')
    form.append('hint', extra.hint ?? '')
    return request<AnalyzeResult>('/questions/analyze', { method: 'POST', body: form })
  },

  dashboard: () => request<DashboardStats>('/stats/dashboard'),

  // ---------- 同类题检测（发起 → 作答判分，含降级/回升联动） ----------

  startDetection: (questionId: number) =>
    request<DetectionStart>(`/questions/${questionId}/detection/start`, { method: 'POST' }),

  answerDetection: (logId: number, answer: string) =>
    request<DetectionAnswerResult>(`/detection/${logId}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer }),
    }),

  // ---------- 整卷导入（拍照合成 PDF → 异步切题解构 → 确认入库） ----------

  importDocument: (pdf: File) => {
    const form = new FormData()
    form.append('document', pdf)
    return request<DocumentImport>('/documents/import', { method: 'POST', body: form })
  },

  getDocumentSegments: (jobId: string) =>
    request<SegmentsResult>(`/documents/${jobId}/segments`),

  confirmImport: (jobId: string) =>
    request<ConfirmResult>(`/documents/${jobId}/confirm`, { method: 'POST' }),

  deleteSegment: (jobId: string, index: number) =>
    request<{ deleted: number }>(`/documents/${jobId}/segments/${index}`, { method: 'DELETE' }),

  /** 错题原图：带 JWT 拉 blob 转 objectURL（<img> 无法带 Authorization 头）。 */
  async imageUrl(questionId: number): Promise<string | null> {
    const token = getToken()
    if (!token) return null
    const resp = await fetch(`${BASE}/questions/${questionId}/image`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!resp.ok) return null
    return URL.createObjectURL(await resp.blob())
  },
}
