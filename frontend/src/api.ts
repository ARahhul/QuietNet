import { firebaseEnabled, firebaseToken, firebaseTokenNow } from './lib/firebase'

// Empty in dev (Vite proxies /api). A hosted build (Firebase) sets VITE_API_BASE to the public backend URL.
const API_BASE: string = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')
export const apiUrl = (path: string) => `${API_BASE}/api${path}`

// ---------- who is asking ----------
// People sign in with Firebase (lib/firebase.ts) and the server shows each sign-in only its own home. The server's
// access token still works (scripts, and the browser that used it before sign-in existed): pasted once per browser,
// it stays in localStorage. A hosted dashboard never carries it in its JavaScript. Local dev without Firebase
// falls back to the backend's default token.
const TOKEN_KEY = 'quietnet.token'
const BUILD_TOKEN: string = firebaseEnabled ? '' : import.meta.env.VITE_API_TOKEN || (import.meta.env.DEV ? 'dev-token' : '')
/** Fired when the saved access token changes, so the app can sign in or out. */
export const AUTH_EVENT = 'quietnet:auth'
/** Fired when the server refuses this browser's sign-in or token (401). */
export const REJECTED_EVENT = 'quietnet:rejected'

/** The server access token saved in this browser (or the dev default). */
export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || BUILD_TOKEN
}
export const hasSavedToken = () => !!localStorage.getItem(TOKEN_KEY)
export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
  window.dispatchEvent(new Event(AUTH_EVENT))
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

/** What this browser sends as its Bearer: the Firebase sign-in when there is one, else the access token. */
export async function bearer(forceRefresh = false): Promise<string> {
  return (await firebaseToken(forceRefresh)) || getToken()
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

async function req<T>(method: string, path: string, body?: unknown, retried = false): Promise<T> {
  const r = await fetch(apiUrl(path), {
    method,
    headers: { Authorization: `Bearer ${await bearer(retried)}`, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  // A Firebase ID token can expire mid-request: try once more with a fresh one.
  if (r.status === 401 && !retried && firebaseTokenNow()) return req<T>(method, path, body, true)
  if (r.status === 204) return undefined as T
  const data = await r.json().catch(() => null)
  if (r.status === 401) window.dispatchEvent(new Event(REJECTED_EVENT))
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? data)
  return data as T
}

/** For EventSource and plain links, which can't send an Authorization header: the URL with a two-minute ticket for
 * that one path. The sign-in itself never goes into a URL, since URLs end up in logs and browser history. */
export async function withTicket(path: string): Promise<string> {
  const { ticket } = await req<{ ticket: string }>('POST', '/tickets', { path })
  return `${apiUrl(path)}${path.includes('?') ? '&' : '?'}ticket=${encodeURIComponent(ticket)}`
}

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
/** "I've done it": whether QuietNet sees this device's lookups arriving */
export interface SetupCheck {
  ok: boolean
  lookups: number
  seen_at: string | null
  encrypted: boolean | null
  message: string
  tips: string[]
  /** Plain DNS on a router not linked to the address this page comes from: offer to link it */
  link_ip: string | null
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
  /** router's Dynamic DNS name QuietNet follows to keep linked_ip current */
  ddns_host: string | null
  ddns_status: { host: string; checked_at: string; ip: string | null; error: string | null } | null
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

/** "Saw an ad?": a name the AI thinks served the ads (see backend pahra.ad_hunt). */
export interface AdCandidate {
  name: string
  owner: string | null
  role: 'ad server' | 'ad decisions' | 'ad images' | 'ad tracking' | 'ad network'
  why: string
  confidence: 'high' | 'medium' | 'low'
  break_risk: 'low' | 'medium' | 'high'
  break_note: string
  must_allow: boolean
  lookups: number
  last_seen: string
  selected: boolean
}

export interface AdHunt {
  where: string
  minutes: number
  looked_at: number
  summary: string
  candidates: AdCandidate[]
  in_stream: boolean
  note: string
}

/** "Site not working?": a name QuietNet blocked that the site likely needs (see backend pahra.site_fix). */
export interface SiteCandidate {
  /** what Unblock lets through: the site's own domain whole when one of your rules blocked all of it, else the exact name */
  name: string
  /** the names the site looked up that were blocked */
  looked_up: string[]
  owner: string | null
  category: Category
  why: string
  confidence: 'high' | 'medium' | 'low'
  /** yours: your rule; auto: Auto-mode; held: a hold on a brand-new domain; blocklist: QuietNet's ad & tracker
   * list (every home); list: another block list on the DNS server (threat intelligence) */
  blocked_by: 'yours' | 'auto' | 'held' | 'blocklist' | 'list'
  devices: { id: string; name: string }[]
  times: number
  last_seen: string
  warning: string
  selected: boolean
}

export interface SiteFix {
  what: string
  site: string
  minutes: number
  looked_at: number
  summary: string
  candidates: SiteCandidate[]
  /** nothing blocked looks related: the problem is elsewhere */
  not_quietnet: boolean
  note: string
  /** blocked then, but let through since */
  already_unblocked: string[]
  ai: boolean
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
  /** rule: one of your QuietNet rules; blocklist: QuietNet's ad & tracker list; list: another filter list on the
   * DNS server (threat intelligence) */
  blocked_by: 'rule' | 'blocklist' | 'list' | null
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
    blocked_by_blocklist: number
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
  device_id: string | null
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
    /** blocked for every home by QuietNet's ad & tracker list (an allow decision lets it through) */
    blocklist: boolean
  }[]
}

/** Names QuietNet's ad & tracker list turned away for this home lately, grouped by domain. */
export interface ListBlocked {
  domain: string
  /** the exact names looked up (what Unblock lets through) */
  names: string[]
  count: number
  last_seen: string | null
  devices: { id: string; name: string }[]
  category: Category
  owner: string | null
  what_it_does: string | null
  break_risk: 'low' | 'medium' | 'high' | null
  sources: string[]
  notes: string
}

export type ReasonKind = 'must_allow' | 'let_through' | 'held' | 'rule' | 'blocklist' | 'threat_list' | 'clean'

/** One line of "why": a rule of this home's own, or a list the domain is on. */
export interface DomainReason {
  kind: ReasonKind
  title: string
  detail: string
  sources?: string[]
  notes?: string
  category?: Category
  owner?: string | null
  /** the list is loaded in the DNS server and blocks for this home (blocklist) */
  active?: boolean
  /** this home switched QuietNet's list off in Settings (blocklist) */
  home_off?: boolean
  /** viewing a whole domain: the names looked up under it that the list covers (blocklist) */
  names?: string[]
  decision_id?: string
  rule_domain?: string
  decided_by?: string
  created_at?: string
  household?: boolean
  device?: { id: string; name: string } | null
  /** the rule covers the device in view (or the whole home) */
  applies?: boolean
}

/** The View panel: what a domain is, why it is or isn't blocked here, and what this home looked up. */
export interface DomainDetail {
  name: string
  domain: string
  category: Category
  owner: string | null
  what_it_does: string | null
  break_risk: 'low' | 'medium' | 'high' | null
  break_note: string | null
  confidence: 'high' | 'medium' | 'low' | null
  sources: string[]
  explained_by: string | null
  /** QuietNet has seen and described it */
  checked: boolean
  acr: boolean
  must_allow: boolean
  status: 'blocked' | 'allowed'
  blocked_by: 'let_through' | 'yours' | 'auto' | 'held' | 'blocklist' | 'threat_list' | null
  reasons: DomainReason[]
  /** `let_through`: an allow rule of this home's lets the name through now */
  looked_up: { name: string; count: number; blocked: number; last_seen: string | null; let_through: boolean }[]
  devices: { id: string; name: string; count: number; blocked: number; last_seen: string | null }[]
  hours: number
  /** unblock: names to let through (sites/unblock); undo_allow: allow rules to drop (undo-bulk); block: a rule to add */
  actions: { unblock: string[] | null; undo_allow: string[] | null; block: string | null }
}

export interface Health {
  adguard: { ok: boolean; version?: string; error?: string }
  poller: { last_poll: string | null; error: string | null; interval_s: number }
  sync: { dirty: boolean; last_ok: string | null; error: string | null }
  lists: { loaded_from: string; lists: Record<string, number>; tracker_radar_entries: number }
  blocklist?: { active: boolean; source: string; blocked_in_adguard: number; clean: number; known_without_ai: number; error: string | null }
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
  /** QuietNet's shared ad & tracker list blocks for this home (on unless switched off) */
  shared_blocklist: boolean
  dns_hostname: string
  vm_ip: string
}

// ---------- calls ----------
/** Who this browser is signed in as, on the server's side */
export interface Account {
  signed_in_with: 'firebase' | 'token'
  email: string | null
  /** This is the server's first account (the one its access token opens) */
  operator: boolean
  /** The access token's account hasn't been moved onto a sign-in yet */
  can_claim: boolean
}

export const api = {
  health: () => req<Health>('GET', '/health'),
  account: () => req<Account>('GET', '/account'),
  /** Move the devices set up with the server's access token into the signed-in account (once). */
  claim: (token: string) => req<{ ok: true }>('POST', '/account/claim', { token }),
  devices: () => req<Device[]>('GET', '/devices'),
  device: (id: string) => req<Device>('GET', `/devices/${id}`),
  createDevice: (b: { name: string; type: DeviceType; brand?: string | null; setup_method?: string }) =>
    req<{ device: Device; setup: SetupGuide; synced: boolean }>('POST', '/devices', b),
  patchDevice: (id: string, b: Record<string, unknown>) => req<{ device: Device }>('PATCH', `/devices/${id}`, b),
  deleteDevice: (id: string) => req<void>('DELETE', `/devices/${id}`),
  /** A new address (ClientID) for the device; the old one stops working at once. */
  changeDeviceAddress: (id: string) =>
    req<{ device: Device; setup: SetupGuide; synced: boolean }>('POST', `/devices/${id}/client-id`),
  setup: (id: string) => req<SetupGuide>('GET', `/devices/${id}/setup`),
  activity: (id: string) => req<ActivityGroup[]>('GET', `/devices/${id}/activity`),
  suggestions: (deviceId?: string) =>
    req<Suggestion[]>('GET', `/suggestions${deviceId ? `?device_id=${deviceId}` : ''}`),
  decide: (b: { domain: string; action: 'block' | 'allow'; device_id?: string | null; confirm_must_allow?: boolean }) =>
    req<{ decision: Decision; synced: boolean }>('POST', '/decisions', b),
  bulk: (b: { categories?: string[]; device_id?: string | null; min_confidence?: string; domains?: string[] } = {}) =>
    req<{ decisions: Decision[]; count: number; synced: boolean }>('POST', '/decisions/bulk', b),
  undo: (id: string) => req<{ decision: Decision; synced: boolean }>('POST', `/decisions/${id}/undo`),
  /** `restore`: decisions the undone ones replaced, to put back (undoing an unblock restores the block it lifted) */
  undoBulk: (ids: string[], restore: string[] = []) =>
    req<{ count: number; restored: number }>('POST', '/decisions/undo-bulk', { ids, restore }),
  decisions: () => req<Decision[]>('GET', '/decisions'),
  blocked: () => req<BlockedDomain[]>('GET', '/blocked'),
  blockedByList: (deviceId?: string | null, hours = 24) =>
    req<{ active: boolean; home_off: boolean; hours: number; domains: ListBlocked[] }>('GET', `/blocked/by-list${qs({ device_id: deviceId, hours })}`),
  domain: (name: string, deviceId?: string | null) =>
    req<DomainDetail>('GET', `/domains/${encodeURIComponent(name)}${qs({ device_id: deviceId })}`),
  alerts: () => req<Alert[]>('GET', '/alerts'),
  readAlert: (id: string) => req<{ ok: boolean }>('POST', `/alerts/${id}/read`),
  report: () => req<Report>('GET', '/report/weekly'),
  analytics: (hours: number, deviceId?: string) =>
    req<Analytics>('GET', `/report/analytics${qs({ hours, device_id: deviceId })}`),
  liveQueries: (f: LiveFilter) =>
    req<{ queries: LiveQuery[]; last_poll: string | null; poll_error: string | null }>('GET', `/live/queries${qs(f)}`),
  liveClients: (minutes = 60) => req<NetworkClient[]>('GET', `/live/clients${qs({ minutes })}`),
  settings: () => req<Settings>('GET', '/settings'),
  /** synced: false when the DNS server couldn't be updated yet (it retries by itself) */
  patchSettings: (b: Partial<Settings>) => req<Settings & { synced?: boolean }>('PATCH', '/settings', b),
  pollNow: () => req<{ ok: boolean }>('POST', '/poll-now'),
  providers: () => req<{ isps: IspGuide[]; routers: RouterGuide[] }>('GET', '/providers'),
  detectProvider: () => req<ProviderDetection>('GET', '/network/provider'),
  ispGuide: (name: string) => req<IspGuide>('POST', '/providers/guide', { kind: 'isp', name }),
  routerGuide: (name: string) => req<RouterGuide>('POST', '/providers/guide', { kind: 'router', name }),
  screenshotHelp: (b: ScreenAsk) => req<ScreenAnswer>('POST', '/setup/screenshot', b),
  checkDevice: (id: string) => req<SetupCheck>('POST', `/devices/${id}/check`),
  adHunt: (b: { where: string; minutes: number; device_id?: string | null; note?: string }) => req<AdHunt>('POST', '/ads/hunt', b),
  siteFix: (b: { what: string; image?: string | null; minutes: number; device_id?: string | null }) =>
    req<SiteFix>('POST', '/sites/fix', b),
  /** Let names through for one device or the household, whatever blocked them. Undo: undoBulk(ids, replaced). */
  siteUnblock: (b: { names: string[]; device_id?: string | null }) =>
    req<{ decisions: Decision[]; replaced: string[]; count: number; synced: boolean }>('POST', '/sites/unblock', b),
  cgnat: (router_wan_ip: string) =>
    req<{ public_ip: string | null; cgnat: boolean | null; advice: string }>('POST', '/network/cgnat', { router_wan_ip }),
}
