/**
 * Thin fetch wrapper.
 *
 * Auth rides on an httpOnly cookie set by the backend, so nothing here has to
 * juggle tokens. A 401 clears the cached session and bounces to the sign-in
 * screen once, rather than every failing request doing its own redirect.
 */
import type {
  Alert,
  Channel,
  ChannelTypeInfo,
  CollectionEntry,
  CostBreakdown,
  CostProfile,
  DashboardStats,
  Item,
  ItemHistory,
  MessageResponse,
  PublicConfig,
  SearchResponse,
  SystemStatus,
  TagRef,
  User,
  Watch,
} from './types'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public payload?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    credentials: 'include',
    headers: {
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers || {}),
    },
    ...init,
  })

  if (response.status === 401) {
    onUnauthorized?.()
    throw new ApiError(401, 'Your session expired. Please sign in again.')
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`
    let payload: unknown
    try {
      payload = await response.json()
      const d = (payload as any)?.detail
      if (typeof d === 'string') detail = d
      else if (Array.isArray(d) && d[0]?.msg) detail = d.map((e: any) => e.msg).join(', ')
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(response.status, detail, payload)
  }

  if (response.status === 204) return undefined as T
  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

const get = <T>(path: string) => request<T>(path)
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
const patch = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
const del = <T>(path: string) => request<T>(path, { method: 'DELETE' })

function qs(params: Record<string, unknown>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)))
    else search.append(key, String(value))
  }
  const out = search.toString()
  return out ? `?${out}` : ''
}

export const api = {
  config: () => get<PublicConfig>('/config'),
  status: () => get<SystemStatus>('/status'),

  auth: {
    register: (body: { email: string; username: string; password: string }) =>
      post<{ user: User; token: string; expires_at: string }>('/auth/register', body),
    login: (body: { identifier: string; password: string; remember?: boolean }) =>
      post<{ user: User; token: string; expires_at: string }>('/auth/login', body),
    logout: () => post<MessageResponse>('/auth/logout'),
    me: () => get<User>('/auth/me'),
    updateMe: (body: Partial<User>) => patch<User>('/auth/me', body),
    changePassword: (body: { current_password: string; new_password: string }) =>
      post<MessageResponse>('/auth/password', body),
    sessions: () => get<any[]>('/auth/sessions'),
    revokeSession: (id: number) => del<MessageResponse>(`/auth/sessions/${id}`),
    costProfile: () => get<CostProfile>('/auth/cost-profile'),
    updateCostProfile: (body: Partial<CostProfile>) =>
      patch<CostProfile>('/auth/cost-profile', body),
  },

  search: {
    run: (body: Record<string, unknown>) => post<SearchResponse>('/search', body),
    resolve: (input: string) => post<Item>('/search/resolve', { input }),
    providers: () => get<any[]>('/search/providers'),
  },

  items: {
    list: (params: Record<string, unknown> = {}) => get<Item[]>(`/items${qs(params)}`),
    get: (id: number) => get<Item>(`/items/${id}`),
    history: (id: number, days = 365) => get<ItemHistory>(`/items/${id}/history${qs({ days })}`),
    landed: (id: number, quantity = 1) =>
      get<CostBreakdown>(`/items/${id}/landed${qs({ quantity })}`),
    refresh: (id: number) => post<Item>(`/items/${id}/refresh`),
    counterpart: (id: number) => post<Item>(`/items/${id}/counterpart`),
  },

  watches: {
    list: (params: Record<string, unknown> = {}) => get<Watch[]>(`/watches${qs(params)}`),
    get: (id: number) => get<Watch>(`/watches/${id}`),
    create: (body: Record<string, unknown>) => post<Watch>('/watches', body),
    update: (id: number, body: Record<string, unknown>) => patch<Watch>(`/watches/${id}`, body),
    remove: (id: number) => del<MessageResponse>(`/watches/${id}`),
    run: (id: number) => post<MessageResponse>(`/watches/${id}/run`),
    preview: (id: number) => post<MessageResponse>(`/watches/${id}/preview`),
    alerts: (id: number) => get<Alert[]>(`/watches/${id}/alerts`),
  },

  alerts: {
    list: (params: Record<string, unknown> = {}) => get<Alert[]>(`/alerts${qs(params)}`),
    unreadCount: () => get<MessageResponse<{ count: number }>>('/alerts/unread-count'),
    markRead: (id: number) => post<Alert>(`/alerts/${id}/read`),
    markAllRead: () => post<MessageResponse>('/alerts/read-all'),
    clear: (readOnly = true) => del<MessageResponse>(`/alerts${qs({ read_only: readOnly })}`),
    stats: (days = 30) => get<MessageResponse>(`/alerts/stats${qs({ days })}`),
  },

  channels: {
    list: () => get<Channel[]>('/channels'),
    types: () => get<ChannelTypeInfo[]>('/channels/types'),
    create: (body: Record<string, unknown>) => post<Channel>('/channels', body),
    update: (id: number, body: Record<string, unknown>) => patch<Channel>(`/channels/${id}`, body),
    remove: (id: number) => del<MessageResponse>(`/channels/${id}`),
    test: (id: number) => post<MessageResponse>(`/channels/${id}/test`),
    detectTelegram: (botToken: string) =>
      post<MessageResponse<{ chat_id: string; name: string; type: string }[]>>(
        '/channels/telegram/detect',
        { bot_token: botToken },
      ),
    subscribePush: (subscription: PushSubscriptionJSON, device: string) =>
      post<Channel>('/channels/webpush/subscribe', { subscription, device }),
    sendDigest: () => post<MessageResponse>('/channels/digest/send'),
  },

  collection: {
    list: (params: Record<string, unknown> = {}) => get<CollectionEntry[]>(`/collection${qs(params)}`),
    add: (body: Record<string, unknown>) => post<CollectionEntry>('/collection', body),
    update: (id: number, body: Record<string, unknown>) =>
      patch<CollectionEntry>(`/collection/${id}`, body),
    remove: (id: number) => del<MessageResponse>(`/collection/${id}`),
    summary: () => get<MessageResponse>('/collection/summary'),
    importRecords: (records: unknown[]) => post<MessageResponse>('/collection/import', { records }),
  },

  discover: {
    tags: (params: Record<string, unknown> = {}) => get<TagRef[]>(`/discover/tags${qs(params)}`),
    local: (params: Record<string, unknown> = {}) => get<Item[]>(`/discover/local${qs(params)}`),
    itemTags: (id: number) => get<TagRef[]>(`/discover/item/${id}/tags`),
    enrichItem: (id: number, force = false) =>
      post<MessageResponse>(`/discover/item/${id}/enrich${qs({ force })}`),
    viaMfc: (body: Record<string, unknown>) => post<MessageResponse>('/discover/mfc', body),
    stats: () => get<MessageResponse>('/discover/stats'),
    runEnrichment: (limit = 10) => post<MessageResponse>(`/discover/enrich/run${qs({ limit })}`),
  },

  dashboard: () => get<DashboardStats>('/dashboard'),
  refreshFx: () => post<MessageResponse>('/fx/refresh'),
  scanDeals: () => post<MessageResponse>('/deal-radar/scan'),

  admin: {
    users: () => get<any[]>('/admin/users'),
    updateUser: (id: number, body: Record<string, unknown>) =>
      patch<MessageResponse>(`/admin/users/${id}`, body),
    deleteUser: (id: number) => del<MessageResponse>(`/admin/users/${id}`),
    settings: () => get<MessageResponse>('/admin/settings'),
    generateVapid: () => post<MessageResponse>('/admin/vapid/generate'),
    catalog: () => get<MessageResponse>('/admin/catalog'),
    runCatalogCrawl: (seconds = 30) =>
      post<MessageResponse>(`/admin/catalog/run${qs({ seconds })}`),
    updateCatalogSlice: (scope: string, body: Record<string, unknown>) =>
      patch<MessageResponse>(`/admin/catalog/${encodeURIComponent(scope)}`, body),
    mfcSession: () => get<MessageResponse>('/admin/mfc/session'),
    setMfcSession: (cookie: string) => request<MessageResponse>('/admin/mfc/session', {
      method: 'PUT',
      body: JSON.stringify({ cookie }),
    }),
    recheckRestricted: (limit = 50) =>
      post<MessageResponse>(`/admin/mfc/recheck-restricted${qs({ limit })}`),
  },
}
