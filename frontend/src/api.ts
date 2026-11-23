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

/** For EventSource and plain links, which can't send an Authorization header. */
export const withToken = (path: string) =>
  `${apiUrl(path)}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(firebaseTokenNow() || getToken())}`

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
  /** yours: your rule; auto: Auto-mode; held: a hold on a brand-new domain; list: the DNS server's block lists */
  blocked_by: 'yours' | 'auto' | 'held' | 'list'
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
  /** blocked by parental controls: changed on the Parental page, never unblocked here */
  parental: string[]
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
  /** rule: one of your QuietNet rules; list: a filter list on the DNS server; parental: parental controls */
  blocked_by: 'rule' | 'list' | 'parental' | null
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
  /** IANA name: the clock parental-control bedtimes follow */
  time_zone: string
  dns_hostname: string
  vm_ip: string
}

// ---------- parental controls (backend pahra.parental) ----------
/** 0 = Sunday … 6 = Saturday: the nights bedtime starts on. start after end runs overnight (22:00 → 07:00). */
export interface Bedtime {
  nights: number[]
  start: string
  end: string
}

/** One layer of rules. A device gets the household's (unless it skips them), its kid profile's and its own: they add up. */
export interface ParentalProfile {
  id: string
  kind: 'household' | 'kid' | 'device'
  name: string
  /** AdGuard's adult-site filter */
  adult: boolean
  /** SafeSearch on Google, Bing, DuckDuckGo… and YouTube restricted mode */
  safe_search: boolean
  /** app ids (AdGuard blocked services) blocked all the time */
  apps: string[]
  /** app ids blocked only at bedtime */
  bedtime_apps: string[]
  /** at bedtime: everything but the essentials */
  bedtime_internet_off: boolean
  bedtime: Bedtime | null
  /** this profile's bedtime is on right now */
  bedtime_now: boolean
}

/** What's blocked on a device right now, all layers added up */
export interface ParentalNow {
  adult: boolean
  safe_search: boolean
  apps: string[]
  internet_off: boolean
  bedtime: boolean
}

export interface ParentalDevice {
  id: string
  name: string
  type: DeviceType
  /** its kid profile */
  profile_id: string | null
  /** a parent's device: the household's rules don't apply */
  skip_household: boolean
  /** rules of its own, or null */
  own: ParentalProfile | null
  now: ParentalNow
}

export interface ParentalOverview {
  time_zone: string
  household: ParentalProfile
  kids: (ParentalProfile & { devices: string[] })[]
  devices: ParentalDevice[]
  /** what still works when bedtime turns the internet off */
  essentials: string[]
}

/** An app or site parental controls can block (AdGuard's blocked-service catalog) */
export interface ParentalService {
  id: string
  name: string
  /** gaming, social_network, streaming, gambling… ('' on older AdGuard) */
  group: string
  /** base64 SVG */
  icon_svg: string
}

export type ProfileChange = Partial<Pick<ParentalProfile, 'name' | 'adult' | 'safe_search' | 'apps' | 'bedtime_apps' | 'bedtime_internet_off' | 'bedtime'>>
type ParentalChanged = ParentalOverview & { synced: boolean }

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
  checkDevice: (id: string) => req<SetupCheck>('POST', `/devices/${id}/check`),
  adHunt: (b: { where: string; minutes: number; device_id?: string | null; note?: string }) => req<AdHunt>('POST', '/ads/hunt', b),
  siteFix: (b: { what: string; image?: string | null; minutes: number; device_id?: string | null }) =>
    req<SiteFix>('POST', '/sites/fix', b),
  /** Let names through for one device or the household, whatever blocked them. Undo: undoBulk(ids, replaced). */
  siteUnblock: (b: { names: string[]; device_id?: string | null }) =>
    req<{ decisions: Decision[]; replaced: string[]; count: number; synced: boolean }>('POST', '/sites/unblock', b),
  cgnat: (router_wan_ip: string) =>
    req<{ public_ip: string | null; cgnat: boolean | null; advice: string }>('POST', '/network/cgnat', { router_wan_ip }),
  parental: () => req<ParentalOverview>('GET', '/parental'),
  parentalServices: () => req<ParentalService[]>('GET', '/parental/services'),
  createKidProfile: (name: string) => req<ParentalOverview & { created: string }>('POST', '/parental/profiles', { name }),
  patchParentalProfile: (id: string, b: ProfileChange) => req<ParentalChanged>('PATCH', `/parental/profiles/${id}`, b),
  deleteKidProfile: (id: string) => req<ParentalChanged>('DELETE', `/parental/profiles/${id}`),
  patchParentalDevice: (id: string, b: { profile_id?: string | null; skip_household?: boolean; own_rules?: boolean }) =>
    req<ParentalChanged>('PATCH', `/parental/devices/${id}`, b),
}
