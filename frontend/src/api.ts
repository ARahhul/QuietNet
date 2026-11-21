export const API_TOKEN: string = import.meta.env.VITE_API_TOKEN ?? 'dev-token'

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`)
    this.status = status
    this.detail = detail
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method,
    headers: { Authorization: `Bearer ${API_TOKEN}`, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (r.status === 204) return undefined as T
  const data = await r.json().catch(() => null)
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? data)
  return data as T
}

export const withToken = (path: string) => `/api${path}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(API_TOKEN)}`

// ---------- types ----------
export type Category = 'ads' | 'tracking' | 'telemetry' | 'needed' | 'malware' | 'unknown'
export type DeviceType = 'tv' | 'phone' | 'laptop' | 'router' | 'other'

export interface Device {
  id: string
  client_id: string
  name: string
  type: DeviceType
  brand: string | null
  setup_method: string
  linked_ip: string | null
  last_seen: string | null
  online: boolean
  risk_score: number
  stats: { domains: number; queries: number; blocked_queries: number; trackers: number; threats: number }
}

export interface SetupGuide {
  method: string
  values: { dot_hostname: string; doh_url: string; server_ip: string; adb_commands?: string[]; forwarder_ip?: string }
  steps: string[]
}

export interface CardDevice {
  device_id: string
  name: string
  type: DeviceType
  count: number
  last_ts: string
  beacon: boolean
  avg_interval_s: number | null
  quarantined: boolean
}

export interface Suggestion {
  domain: string
  category: Category
  owner: string | null
  what_it_does: string
  break_risk: 'low' | 'medium' | 'high' | null
  break_note: string | null
  confidence: 'high' | 'medium' | 'low'
  sources: string[]
  explained_by: string | null
  acr: boolean
  must_allow: boolean
  total_count: number
  status: 'pending' | 'quarantined'
  devices: CardDevice[]
}

export interface Decision {
  id: string
  domain: string
  action: 'block' | 'allow' | 'quarantine'
  device_id: string | null
  adguard_rule: string
  decided_by: string
  created_at: string
  undone_at: string | null
}

export interface Alert {
  id: string
  type: string
  message: string
  device_name: string | null
  domain: string | null
  fix_steps: { title?: string; steps?: string[]; checked?: boolean }
  fix_source: string | null
  read: number
  created_at: string
}

export interface ActivityGroup {
  owner: string
  count: number
  domains: {
    domain: string
    category: Category
    confidence: string
    what_it_does: string
    count: number
    blocked_count: number
    last_ts: string
    beacon: boolean
    decision: { id: string; action: string; household: boolean } | null
  }[]
}

export interface Health {
  adguard: { ok: boolean; version?: string; error?: string }
  poller: { last_poll: string | null; error: string | null; interval_s: number }
  sync: { dirty: boolean; last_ok: string | null; error: string | null }
  lists: { loaded_from: string; lists: Record<string, number>; tracker_radar_entries: number }
  llm: { enabled: boolean; model: string; error: string | null }
  domains_explained: number
  companion: { enabled: boolean; connected?: boolean; error?: string | null; tools?: string[] }
  forwarder?: { running: boolean; devices: number; listen_ip?: string | null; error?: string | null; hint?: string | null }
}

export interface Report {
  since: string
  queries: number
  blocked_queries: number
  top_collectors: { owner: string; queries: number; blocked: number; domains: string[] }[]
  devices: { id: string; name: string; risk_score: number; queries: number; blocked_queries: number; trackers: number }[]
  devices_to_check: string[]
  decisions: { action: string; decided_by: string; n: number }[]
  alerts: { type: string; n: number }[]
}

export interface Settings {
  email: string
  auto_mode: boolean
  dns_hostname: string
  vm_ip: string
}

// ---------- calls ----------
export const api = {
  health: () => req<Health>('GET', '/health'),
  devices: () => req<Device[]>('GET', '/devices'),
  device: (id: string) => req<Device>('GET', `/devices/${id}`),
  createDevice: (b: { name: string; type: DeviceType; brand?: string | null; setup_method?: string }) =>
    req<{ device: Device; setup: SetupGuide; synced: boolean }>('POST', '/devices', b),
  patchDevice: (id: string, b: Record<string, unknown>) => req<{ device: Device }>('PATCH', `/devices/${id}`, b),
  deleteDevice: (id: string) => req<void>('DELETE', `/devices/${id}`),
  setup: (id: string) => req<SetupGuide>('GET', `/devices/${id}/setup`),
  activity: (id: string) => req<ActivityGroup[]>('GET', `/devices/${id}/activity`),
  suggestions: (deviceId?: string) =>
    req<Suggestion[]>('GET', `/suggestions${deviceId ? `?device_id=${deviceId}` : ''}`),
  decide: (b: { domain: string; action: 'block' | 'allow'; device_id?: string | null; confirm_must_allow?: boolean }) =>
    req<{ decision: Decision; synced: boolean }>('POST', '/decisions', b),
  bulk: (b: { categories?: string[]; device_id?: string | null; min_confidence?: string; domains?: string[] } = {}) =>
    req<{ decisions: Decision[]; count: number; synced: boolean }>('POST', '/decisions/bulk', b),
  undo: (id: string) => req<{ decision: Decision; synced: boolean }>('POST', `/decisions/${id}/undo`),
  undoBulk: (ids: string[]) => req<{ count: number }>('POST', '/decisions/undo-bulk', { ids }),
  decisions: () => req<Decision[]>('GET', '/decisions'),
  alerts: () => req<Alert[]>('GET', '/alerts'),
  readAlert: (id: string) => req<{ ok: boolean }>('POST', `/alerts/${id}/read`),
  report: () => req<Report>('GET', '/report/weekly'),
  settings: () => req<Settings>('GET', '/settings'),
  patchSettings: (b: Partial<Settings>) => req<Settings>('PATCH', '/settings', b),
  pollNow: () => req<{ ok: boolean }>('POST', '/poll-now'),
  cgnat: (router_wan_ip: string) =>
    req<{ public_ip: string | null; cgnat: boolean | null; advice: string }>('POST', '/network/cgnat', { router_wan_ip }),
}
