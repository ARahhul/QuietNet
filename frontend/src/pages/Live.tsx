import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Home, Pause, Play, Search, X } from 'lucide-react'
import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, type DeviceType, type LiveFilter, type LiveQuery, type NetworkClient } from '../api'
import { DATA, fmt } from '../lib/viz'
import { DeviceIcon, Empty, FilterChip, Roll, timeAgo } from '../components/ui'

type Status = NonNullable<LiveFilter['status']>
type Group = 'device' | 'ip'

const REFRESH_MS = 3000
const LIMIT = 150
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })

/** RFC 1918, CGNAT (100.64/10), link-local and IPv6 unique-local addresses. */
const isLocal = (ip: string) =>
  /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|169\.254\.|f[cd][0-9a-f]{2}:|fe80:)/i.test(ip)

const RESULT: Record<string, { label: string; title: string; color: string }> = {
  rule: { label: 'Blocked', title: 'Blocked by one of your QuietNet rules', color: DATA.blocked },
  list: { label: 'Blocked by list', title: 'Blocked by a filter list on the DNS server', color: DATA.blocked },
  parental: { label: 'Parental controls', title: 'Blocked by parental controls (change them on the Parental page)', color: DATA.blocked },
  cached: { label: 'From cache', title: 'Allowed, answered from the DNS server’s cache', color: DATA.cached },
  allowed: { label: 'Allowed', title: 'Allowed, answered by the upstream resolver', color: DATA.allowed },
}
const result = (r: LiveQuery) =>
  RESULT[r.status === 'blocked' ? (r.blocked_by === 'list' || r.blocked_by === 'parental' ? r.blocked_by : 'rule') : r.status]

interface IpGroup {
  key: string
  ip: string
  kind: 'lan' | 'seen'
  devices: { id: string; name: string; type: DeviceType; queries: number }[]
  queries: number
  blocked: number
}

/** Pivot devices into addresses: a home-network address (known for relayed TVs) or an address the DNS server saw. */
function byAddress(clients: NetworkClient[]): IpGroup[] {
  const groups = new Map<string, IpGroup>()
  const add = (key: string, ip: string, kind: IpGroup['kind'], c: NetworkClient, queries: number, blocked: number) => {
    const g = groups.get(key) ?? { key, ip, kind, devices: [], queries: 0, blocked: 0 }
    g.devices.push({ id: c.id, name: c.name, type: c.type, queries })
    g.queries += queries
    g.blocked += blocked
    groups.set(key, g)
  }
  for (const c of clients) {
    if (c.lan_ip) add(`lan:${c.lan_ip}`, c.lan_ip, 'lan', c, c.queries, c.blocked)
    for (const a of c.ips) if (a.ip) add(`seen:${a.ip}`, a.ip, 'seen', c, a.queries, a.blocked)
  }
  return [...groups.values()].sort((a, b) => (a.kind === b.kind ? b.queries - a.queries : a.kind === 'lan' ? -1 : 1))
}

export default function Live() {
  const [params, setParams] = useSearchParams()
  const status = (['blocked', 'allowed'].includes(params.get('status') ?? '') ? params.get('status') : 'all') as Status
  const deviceId = params.get('device') ?? undefined
  const ip = params.get('ip') ?? undefined
  const [q, setQ] = useState('')
  const search = useDeferredValue(q.trim())
  const [paused, setPaused] = useState(false)
  const [reading, setReading] = useState(false)
  const [group, setGroup] = useState<Group>('device')
  const holding = paused || reading

  const set = (next: Record<string, string | undefined>) =>
    setParams(
      (p) => {
        const out = new URLSearchParams(p)
        for (const [k, v] of Object.entries(next)) {
          if (v) out.set(k, v)
          else out.delete(k)
        }
        return out
      },
      { replace: true },
    )

  const live = useQuery({
    queryKey: ['live', status, deviceId, ip, search],
    queryFn: () => api.liveQueries({ status, device_id: deviceId, ip, q: search, limit: LIMIT }),
    refetchInterval: holding ? false : REFRESH_MS,
    placeholderData: keepPreviousData,
  })
  const clients = useQuery({
    queryKey: ['live-clients'],
    queryFn: () => api.liveClients(60),
    refetchInterval: holding ? false : 10000,
  })

  const rows = useMemo(() => live.data?.queries ?? [], [live.data])
  const filterKey = `${status}|${deviceId}|${ip}|${search}`

  // Rows that arrived since the last refresh wash in. The first page and a new filter flash nothing.
  const prev = useRef<{ key: string; ids: Set<number> } | null>(null)
  const [freshIds, setFreshIds] = useState<Set<number>>(() => new Set())
  useEffect(() => {
    if (!live.data || live.isPlaceholderData) return
    const before = prev.current?.key === filterKey ? prev.current.ids : null
    prev.current = { key: filterKey, ids: new Set(rows.map((r) => r.id)) }
    setFreshIds(new Set(before ? rows.filter((r) => !before.has(r.id)).map((r) => r.id) : []))
  }, [rows, live.data, live.isPlaceholderData, filterKey])
  const fresh = (id: number) => freshIds.has(id)

  const all = useMemo(() => clients.data ?? [], [clients.data])
  const groups = useMemo(() => byAddress(all), [all])
  const total = all.reduce((n, c) => n + c.queries, 0)
  const device = all.find((c) => c.id === deviceId)
  const filtered = status !== 'all' || !!deviceId || !!ip || !!search

  if (clients.data && all.length === 0) {
    return (
      <div>
        <h1 className="page-title">Live</h1>
        <div className="mt-6">
          <Empty title="No devices yet">
            <Link to="/setup" className="underline-slide text-guard">
              Connect a device
            </Link>{' '}
            and its lookups show up here a few seconds after it asks for a domain.
          </Empty>
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Live</h1>
          <p className="mt-2 max-w-prose text-sm text-muted">
            Every domain your devices look up, as it happens. {fmt(total)} lookups from{' '}
            {all.filter((c) => c.queries > 0).length} of {all.length} devices in the last hour.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <LiveState holding={holding} paused={paused} error={live.data?.poll_error ?? null} lastPoll={live.data?.last_poll ?? null} />
          <button className="btn-ghost px-4 py-2" onClick={() => setPaused((p) => !p)} aria-pressed={paused}>
            <Roll>
              {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />} {paused ? 'Resume' : 'Pause'}
            </Roll>
          </button>
        </div>
      </div>

      <div className="mt-6 grid items-start gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
        <aside className="card p-4 xl:sticky xl:top-6">
          <div className="flex items-center justify-between gap-3">
            <h2 className="font-display text-lg">Your network</h2>
            <div className="flex rounded-full border border-line p-0.5 text-xs" role="group" aria-label="Group by">
              {(['device', 'ip'] as const).map((g) => (
                <button
                  key={g}
                  onClick={() => setGroup(g)}
                  aria-pressed={group === g}
                  className={`rounded-full px-3 py-1 transition ${group === g ? 'bg-white text-ink' : 'text-muted hover:text-white'}`}
                >
                  {g === 'device' ? 'Devices' : 'IP addresses'}
                </button>
              ))}
            </div>
          </div>
          <p className="mt-1 text-xs text-muted">Last hour. Pick one to see only its lookups.</p>

          <ul className="mt-3 grid gap-1">
            <li>
              <PanelButton active={!deviceId && !ip} onClick={() => set({ device: undefined, ip: undefined })}>
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-panel-2">
                  <Home className="h-4 w-4" />
                </span>
                <span className="min-w-0 flex-1 font-medium">Everything</span>
                <span className="text-xs text-muted tabular-nums">{fmt(total)}</span>
              </PanelButton>
            </li>
            {group === 'device'
              ? all.map((c) => (
                  <li key={c.id}>
                    <PanelButton active={deviceId === c.id && !ip} onClick={() => set({ device: c.id, ip: undefined })}>
                      <span className="relative grid h-8 w-8 shrink-0 place-items-center rounded-md bg-panel-2">
                        <DeviceIcon type={c.type} className="h-4 w-4" />
                        {c.active && <span className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-guard ring-2 ring-panel" title="Active in the last 5 minutes" />}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium">{c.name}</span>
                        <span className="block truncate font-mono text-[11px] text-muted">
                          {c.lan_ip ?? c.ips[0]?.ip ?? 'no lookups yet'}
                          {c.ips.length > 1 && ` +${c.ips.length - 1}`}
                        </span>
                      </span>
                      <Counts queries={c.queries} blocked={c.blocked} />
                    </PanelButton>
                  </li>
                ))
              : groups.map((g) => {
                  const only = g.devices.length === 1 ? g.devices[0] : null
                  const active = g.kind === 'seen' ? ip === g.ip : deviceId === only?.id && !ip
                  return (
                    <li key={g.key}>
                      <PanelButton
                        active={active}
                        onClick={() => (g.kind === 'seen' ? set({ ip: g.ip, device: undefined }) : set({ device: only?.id, ip: undefined }))}
                      >
                        <span className="min-w-0 flex-1">
                          <span className="block truncate font-mono text-[13px]">{g.ip}</span>
                          <span className="block text-[11px] text-muted">
                            {g.kind === 'lan' ? 'Home network, via the laptop relay' : isLocal(g.ip) ? 'Local address' : 'Public address'}
                          </span>
                          <span className="mt-1 flex flex-wrap gap-1">
                            {g.devices.map((d) => (
                              <span key={d.id} className="chip bg-panel-2 font-normal text-body">
                                <DeviceIcon type={d.type} className="h-3 w-3" /> {d.name}
                              </span>
                            ))}
                          </span>
                        </span>
                        <Counts queries={g.queries} blocked={g.blocked} />
                      </PanelButton>
                    </li>
                  )
                })}
            {group === 'ip' && groups.length === 0 && <li className="px-3 py-4 text-sm text-muted">No lookups in the last hour.</li>}
          </ul>
        </aside>

        <section className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <label className="relative w-full sm:w-72">
              <span className="sr-only">Search lookups</span>
              <Search className="pointer-events-none absolute top-1/2 left-3.5 h-4 w-4 -translate-y-1/2 text-muted" />
              <input className="input pl-10" type="search" placeholder="Search domains or companies" value={q} onChange={(e) => setQ(e.target.value)} />
            </label>
            <div className="flex gap-2">
              {(['all', 'blocked', 'allowed'] as const).map((s) => (
                <FilterChip key={s} active={status === s} onClick={() => set({ status: s === 'all' ? undefined : s })}>
                  {s === 'all' ? 'Everything' : s === 'blocked' ? 'Blocked' : 'Allowed'}
                </FilterChip>
              ))}
            </div>
            {(device || ip) && (
              <button
                className="chip border border-line py-1.5 text-xs font-normal text-body transition hover:border-line-strong"
                onClick={() => set({ device: undefined, ip: undefined })}
              >
                {device ? <DeviceIcon type={device.type} className="h-3.5 w-3.5" /> : null}
                {device ? device.name : <span className="font-mono">{ip}</span>}
                <X className="h-3.5 w-3.5 text-muted" aria-label="Clear" />
              </button>
            )}
          </div>

          <div
            className={`card mt-4 overflow-hidden transition-opacity ${live.isPlaceholderData ? 'opacity-60' : ''}`}
            onPointerEnter={(e) => e.pointerType === 'mouse' && setReading(true)}
            onPointerLeave={() => setReading(false)}
          >
            <div className="hidden grid-cols-[84px_minmax(0,1.1fr)_minmax(0,2fr)_60px_minmax(0,1.1fr)_64px] gap-4 border-b border-line px-4 py-2.5 text-xs text-muted md:grid">
              <span>Time</span>
              <span>Device</span>
              <span>Domain</span>
              <span>Type</span>
              <span>Result</span>
              <span className="text-right">Reply</span>
            </div>
            {live.isLoading && <p className="p-6 text-sm text-muted">Loading…</p>}
            {!live.isLoading && rows.length === 0 && (
              <div className="p-8 text-center">
                <div className="font-display text-xl">{filtered ? 'No lookups match' : 'Waiting for the first lookup'}</div>
                <p className="mx-auto mt-2 max-w-prose text-sm text-muted">
                  {filtered ? (
                    <button
                      className="underline-slide text-guard"
                      onClick={() => {
                        setQ('')
                        set({ status: undefined, device: undefined, ip: undefined })
                      }}
                    >
                      Show everything
                    </button>
                  ) : (
                    'Lookups show up here a few seconds after a device asks for a domain.'
                  )}
                </p>
              </div>
            )}
            <ol className="divide-y divide-line">
              {rows.map((r) => {
                const res = result(r)
                return (
                  <li
                    key={r.id}
                    className={`grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1 px-4 py-2.5 text-sm md:grid-cols-[84px_minmax(0,1.1fr)_minmax(0,2fr)_60px_minmax(0,1.1fr)_64px] md:items-center ${fresh(r.id) ? 'motion-safe:animate-row-in' : ''}`}
                  >
                    <span className="order-2 text-right text-xs text-muted tabular-nums md:order-none md:text-left">
                      <time dateTime={r.time} title={timeAgo(r.time)}>
                        {timeFmt.format(new Date(r.time))}
                      </time>
                    </span>
                    <span className="order-3 flex min-w-0 items-center gap-2 md:order-none">
                      <DeviceIcon type={r.device.type} className="h-4 w-4 shrink-0 text-muted" />
                      <span className="min-w-0">
                        <span className="block truncate text-xs md:text-sm">{r.device.name}</span>
                        {r.client_ip && <span className="hidden truncate font-mono text-[11px] text-muted md:block">{r.client_ip}</span>}
                      </span>
                    </span>
                    <span className="order-1 min-w-0 md:order-none">
                      <span className="block truncate font-mono text-[13px]" title={r.qname}>
                        {r.qname}
                      </span>
                      {r.owner && <span className="block truncate text-xs text-muted">{r.owner}</span>}
                    </span>
                    <span className="hidden font-mono text-xs text-muted md:block">{r.qtype ?? ''}</span>
                    <span
                      className="order-4 flex items-center justify-end gap-2 text-xs md:order-none md:justify-start md:text-sm"
                      title={r.upstream && r.status === 'allowed' ? `${res.title} (${r.upstream})` : res.title}
                    >
                      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: res.color }} />
                      <span className="truncate">{res.label}</span>
                    </span>
                    <span className="hidden text-right text-xs text-muted tabular-nums md:block">
                      {r.elapsed_ms !== null ? `${r.elapsed_ms < 1 ? '<1' : Math.round(r.elapsed_ms)} ms` : ''}
                    </span>
                  </li>
                )
              })}
            </ol>
          </div>
          {rows.length > 0 && (
            <p className="mt-3 text-xs text-muted">
              Showing the latest {rows.length === LIMIT ? LIMIT : rows.length}. QuietNet keeps each lookup for 24 hours; the{' '}
              <Link to="/report" className="underline-slide text-body">
                report
              </Link>{' '}
              charts all of them.
            </p>
          )}
        </section>
      </div>
    </div>
  )
}

function LiveState({ holding, paused, error, lastPoll }: { holding: boolean; paused: boolean; error: string | null; lastPoll: string | null }) {
  if (error)
    return (
      <span className="flex items-center gap-2 text-xs text-danger" title={error}>
        <span className="h-2 w-2 rounded-full bg-danger" /> Can't reach the DNS server
      </span>
    )
  return (
    <span className="flex items-center gap-2 text-xs text-muted" aria-live="polite">
      <span className="relative flex h-2 w-2">
        {!holding && <span className="absolute inline-flex h-full w-full rounded-full bg-guard opacity-60 motion-safe:animate-ping" />}
        <span className={`relative inline-flex h-2 w-2 rounded-full ${holding ? 'bg-muted' : 'bg-guard'}`} />
      </span>
      {paused ? 'Paused' : holding ? 'Holding while you read' : lastPoll ? `Live, checked ${timeAgo(lastPoll)}` : 'Live'}
    </span>
  )
}

function PanelButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm transition ${active ? 'bg-panel-2 text-white' : 'text-body hover:bg-panel-2/60 hover:text-white'}`}
    >
      {children}
    </button>
  )
}

function Counts({ queries, blocked }: { queries: number; blocked: number }) {
  return (
    <span className="shrink-0 text-right text-xs tabular-nums">
      <span className="block">{fmt(queries)}</span>
      {blocked > 0 && <span className="block text-muted">{fmt(blocked)} blocked</span>}
    </span>
  )
}
