// Empty in dev (Vite proxies /api). A hosted build (Firebase) sets VITE_API_BASE to the public backend URL.
const API_BASE: string = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')
export const apiUrl = (path: string) => `${API_BASE}/api${path}`

// ---------- access token ----------
// A hosted dashboard must not carry the token in its JavaScript (anyone could read it), so the owner
// pastes it once per browser and it stays in localStorage. Local dev falls back to the backend's default.
const TOKEN_KEY = 'quietnet.token'
const BUILD_TOKEN: string = import.meta.env.VITE_API_TOKEN || (import.meta.env.DEV ? 'dev-token' : '')
/** Fired when the backend rejects the token, so the app can ask for it again. */
export const AUTH_EVENT = 'quietnet:auth'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || BUILD_TOKEN
}
export const hasSavedToken = () => !!localStorage.getItem(TOKEN_KEY)
export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
}
export function forgetToken() {
  localStorage.removeItem(TOKEN_KEY)
  window.dispatchEvent(new Event(AUTH_EVENT))
}
/** True when the backend accepts this token. */
export async function checkToken(token: string): Promise<boolean> {
  const r = await fetch(apiUrl('/settings'), { headers: { Authorization: `Bearer ${token}` } })
  if (r.ok) return true
  if (r.status === 401) return false
  throw new ApiError(r.status, `QuietNet's server answered HTTP ${r.status}`)
}

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
  const r = await fetch(apiUrl(path), {
    method,
    headers: { Authorization: `Bearer ${getToken()}`, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (r.status === 204) return undefined as T
  const data = await r.json().catch(() => null)
  if (r.status === 401) forgetToken()
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? data)
  return data as T
}

/** For EventSource and plain links, which can't send an Authorization header. */
export const withToken = (path: string) => `${apiUrl(path)}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(getToken())}`

// ---------- types ----------
/** What the AI read on a pasted screenshot, with QuietNet's values already filled in */
export interface ScreenAnswer {
  screen: string
  /** A direct answer to their question */
  reply: string
  on_track: boolean
  steps: string[]
  fill: { field: string; value: string }[]
  warning: string
  next: string
  done: boolean
}
/** An earlier answer, sent back so the AI carries on from it */
export interface ScreenTurn {
  question: string
  screen: string
  steps: string[]
}
export interface ScreenAsk {
  image: string
  device_id?: string
  isp?: string
  router?: string
  question?: string
  history?: ScreenTurn[]
}

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

/** A block rule in force. `device` is null for a household-wide block; `action: 'quarantine'` is an automatic hold. */
export interface BlockedDomain {
  id: string
  domain: string
  action: 'block' | 'quarantine'
  decided_by: 'user' | 'auto'
  created_at: string
  device: { id: string; name: string; type: DeviceType } | null
  /** Devices a per-device allow exempts from a household block. */
  allowed_on: { id: string; name: string }[]
  category: Category
  owner: string | null
  what_it_does: string | null
  confidence: 'high' | 'medium' | 'low' | null
  break_risk: 'low' | 'medium' | 'high' | null
  break_note: string | null
  must_allow: boolean
  blocked_count: number
  last_seen: string | null
}

export interface LiveQuery {
  id: number
  time: string
  device: { id: string; name: string; type: DeviceType }
  client_ip: string | null
  proto: string | null
  qname: string
  domain: string
  qtype: string | null
  /** cached answers are allowed too; they just never left the server */
  status: 'blocked' | 'allowed' | 'cached'
  /** rule: one of your QuietNet rules; list: a filter list on the DNS server */
  blocked_by: 'rule' | 'list' | null
  upstream: string | null
  elapsed_ms: number | null
  category: Category
  owner: string | null
}

/** Setup → Home WiFi. Served by the backend: hand-checked, AI-written, or generic. */
export type GuideSource = 'curated' | 'ai' | 'generic'
export type Odds = 'usually' | 'often' | 'varies'

export interface IspGuide {
  key: string
  name: string
  /** How a sentence names it: "the Jio box" */
  short: string
  admin_urls: string[]
  logins: { user: string; pass: string; on: string | null }[]
  sticker_note: string | null
  dns_locked: Odds
  dns_note: string
  cgnat: Odds
  help_from: string
  ask_for: string[]
  own_ip: string | null
  source: GuideSource
}

export interface RouterGuide {
  key: string
  name: string
  /** Stored on the device; the backend picks matching setup steps */
  brand: string
  /** Encrypted DNS (DoT) to a custom server */
  dot: 'yes' | 'advanced' | 'no'
  admin_url: string
  dns_menu: string | null
  login: string
  source: GuideSource
}

export interface ProviderDetection {
  found: boolean
  asn: number | null
  country: string | null
  /** The provider's name (ours when we have a guide, else the network's registered name) */
  network: string | null
  isp: IspGuide | null
}

export interface NetworkClient {
  id: string
  name: string
  type: DeviceType
  brand: string | null
  setup_method: string
  /** the device's address on the home network, when QuietNet knows it (laptop relay) */
  lan_ip: string | null
  linked_ip: string | null
  /** source addresses the DNS server saw in the window, busiest first */
  ips: { ip: string | null; queries: number; blocked: number; last: string }[]
  queries: number
  blocked: number
  last: string | null
  active: boolean
}

export interface Ranked {
  domain: string
  n: number
  owner: string | null
  category: Category
}

export interface Analytics {
  hours: number
  bucket_s: number
  start: string
  end: string
  totals: {
    queries: number
    blocked: number
    blocked_by_rules: number
    cached: number
    domains: number
    active_devices: number
    avg_reply_ms: number | null
    block_rules: number
  }
  timeline: { allowed: number[]; blocked: number[] }
  /** every device in creation order (stable colours), each with one count per bucket */
  devices: { id: string; name: string; type: DeviceType; queries: number; blocked: number; series: number[] }[]
  query_types: { name: string; n: number }[]
  answered_by: { name: string; n: number }[]
  top_allowed: Ranked[]
  top_blocked: Ranked[]
  top_companies: { owner: string; n: number; blocked: number; domains: number }[]
}

export type LiveFilter = { device_id?: string; ip?: string; status?: 'all' | 'blocked' | 'allowed'; q?: string; limit?: number }

const qs = (p: Record<string, string | number | undefined | null>) => {
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== null && v !== '') s.set(k, String(v))
  const out = s.toString()
  return out ? `?${out}` : ''
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
  blocked: () => req<BlockedDomain[]>('GET', '/blocked'),
  alerts: () => req<Alert[]>('GET', '/alerts'),
  readAlert: (id: string) => req<{ ok: boolean }>('POST', `/alerts/${id}/read`),
  report: () => req<Report>('GET', '/report/weekly'),
  analytics: (hours: number, deviceId?: string) =>
    req<Analytics>('GET', `/report/analytics${qs({ hours, device_id: deviceId })}`),
  liveQueries: (f: LiveFilter) =>
    req<{ queries: LiveQuery[]; last_poll: string | null; poll_error: string | null }>('GET', `/live/queries${qs(f)}`),
  liveClients: (minutes = 60) => req<NetworkClient[]>('GET', `/live/clients${qs({ minutes })}`),
  settings: () => req<Settings>('GET', '/settings'),
  patchSettings: (b: Partial<Settings>) => req<Settings>('PATCH', '/settings', b),
  pollNow: () => req<{ ok: boolean }>('POST', '/poll-now'),
  providers: () => req<{ isps: IspGuide[]; routers: RouterGuide[] }>('GET', '/providers'),
  detectProvider: () => req<ProviderDetection>('GET', '/network/provider'),
  ispGuide: (name: string) => req<IspGuide>('POST', '/providers/guide', { kind: 'isp', name }),
  routerGuide: (name: string) => req<RouterGuide>('POST', '/providers/guide', { kind: 'router', name }),
  screenshotHelp: (b: ScreenAsk) => req<ScreenAnswer>('POST', '/setup/screenshot', b),
  cgnat: (router_wan_ip: string) =>
    req<{ public_ip: string | null; cgnat: boolean | null; advice: string }>('POST', '/network/cgnat', { router_wan_ip }),
}
