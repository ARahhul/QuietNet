import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Analytics } from '../api'
import { BucketTable, Breakdown, ChartCard, Legend, RankedList, StackedColumns, type Series } from '../components/charts'
import { DeviceIcon, Empty, FilterChip } from '../components/ui'
import { DATA, SERIES, TYPE_COLORS, UPSTREAM_COLORS, fmt, resolverName } from '../lib/viz'

const RANGES = [
  { hours: 1, label: 'Last hour' },
  { hours: 6, label: 'Last 6 hours' },
  { hours: 24, label: 'Last 24 hours' },
]

// Record types keep their colour whatever their rank; anything rarer folds into "Other".
const TYPES: Record<string, { label: string; color: string }> = {
  A: { label: 'A, IPv4 address', color: TYPE_COLORS[0] },
  AAAA: { label: 'AAAA, IPv6 address', color: TYPE_COLORS[1] },
  HTTPS: { label: 'HTTPS, connection hints', color: TYPE_COLORS[2] },
  PTR: { label: 'PTR, name for an address', color: TYPE_COLORS[3] },
}
const OTHER_TYPE = TYPE_COLORS[4]
const BAR_NEUTRAL = '#a3a3a3'

export default function Report() {
  const [hours, setHours] = useState(24)
  const [deviceId, setDeviceId] = useState<string | undefined>()
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices, refetchInterval: 30000 })
  const q = useQuery({
    queryKey: ['analytics', hours, deviceId],
    queryFn: () => api.analytics(hours, deviceId),
    refetchInterval: 30000,
    placeholderData: keepPreviousData,
  })

  // A device keeps its colour (creation order) whether or not it's filtered, ranked or quiet.
  const colorOf = useMemo(() => {
    const ids = (devices.data ?? []).map((d) => d.id)
    return (id: string) => {
      const i = ids.indexOf(id)
      return SERIES[i < 0 ? SERIES.length - 1 : Math.min(i, SERIES.length - 1)]
    }
  }, [devices.data])

  if (devices.data && devices.data.length === 0) {
    return (
      <div>
        <h1 className="page-title">Report</h1>
        <div className="mt-6">
          <Empty title="No devices yet">
            <Link to="/setup" className="underline-slide text-guard">
              Connect a device
            </Link>{' '}
            and its lookups are charted here.
          </Empty>
        </div>
      </div>
    )
  }

  const r = q.data
  const device = devices.data?.find((d) => d.id === deviceId)
  const range = RANGES.find((x) => x.hours === hours)!

  return (
    <div>
      <h1 className="page-title">Report</h1>
      <p className="mt-2 max-w-prose text-sm text-muted">
        What your {device ? device.name : 'devices'} looked up over the {range.label.toLowerCase()}, what QuietNet stopped, and who
        answered the rest. Refreshes every 30 seconds.
      </p>

      <div className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-3">
        <div className="flex rounded-full border border-line p-0.5 text-xs" role="group" aria-label="Time range">
          {RANGES.map((x) => (
            <button
              key={x.hours}
              onClick={() => setHours(x.hours)}
              aria-pressed={hours === x.hours}
              className={`rounded-full px-3 py-1.5 transition ${hours === x.hours ? 'bg-white text-ink' : 'text-muted hover:text-white'}`}
            >
              {x.label}
            </button>
          ))}
        </div>
        <div className="flex min-w-0 gap-2 overflow-x-auto pb-0.5">
          <FilterChip active={!deviceId} onClick={() => setDeviceId(undefined)}>
            All devices
          </FilterChip>
          {devices.data?.map((d) => (
            <FilterChip key={d.id} active={deviceId === d.id} onClick={() => setDeviceId(d.id)}>
              <span className="h-2 w-2 rounded-[3px]" style={{ background: colorOf(d.id) }} />
              {d.name}
            </FilterChip>
          ))}
        </div>
      </div>

      {!r ? (
        <p className="mt-8 text-muted">Loading…</p>
      ) : (
        <Body r={r} dim={q.isPlaceholderData} colorOf={colorOf} single={!!deviceId} />
      )}
    </div>
  )
}

function Body({ r, dim, colorOf, single }: { r: Analytics; dim: boolean; colorOf: (id: string) => string; single: boolean }) {
  const t = r.totals
  const start = Date.parse(r.start)
  const bucketMs = r.bucket_s * 1000
  const share = t.queries ? (t.blocked / t.queries) * 100 : 0
  const per = r.bucket_s >= 120 ? `${r.bucket_s / 60} minutes` : 'minute'

  // Blocked sits on the baseline so its height is the one you can compare across columns.
  const timeline: Series[] = [
    { key: 'blocked', label: 'Blocked', color: DATA.blocked, values: r.timeline.blocked },
    { key: 'allowed', label: 'Allowed', color: DATA.allowed, values: r.timeline.allowed },
  ]

  const active = r.devices.filter((d) => d.queries > 0)
  const deviceSeries: Series[] = active.map((d) => ({ key: d.id, label: d.name, color: colorOf(d.id), values: d.series }))

  const typeParts = foldTypes(r.query_types)
  const answerParts = foldAnswers(r.answered_by)

  return (
    <div className={`transition-opacity ${dim ? 'opacity-60' : ''}`}>
      <div className="mt-6 grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Stat label="Lookups" value={fmt(t.queries)} foot={`from ${t.active_devices} ${t.active_devices === 1 ? 'device' : 'devices'}`} to="/live" action="Watch live" />
        <Stat
          label="Blocked"
          value={fmt(t.blocked)}
          foot={`${fmt(t.blocked_by_rules)} by your rules`}
          to="/live?status=blocked"
          action="See them"
        />
        <Stat label="Blocked share" value={`${share.toFixed(share > 0 && share < 10 ? 1 : 0)}%`} foot="of every lookup" highlight />
        <Stat label="Domains you block" value={fmt(t.block_rules)} foot="household and per device" to="/blocked" action="Manage" />
      </div>

      <div className="mt-6 grid gap-6">
        <ChartCard
          title="Lookups over time"
          note={`Each column is ${r.bucket_s >= 120 ? per : 'one minute'}.`}
          legend={<Legend items={[{ label: 'Blocked', color: DATA.blocked, value: t.blocked }, { label: 'Allowed', color: DATA.allowed, value: t.queries - t.blocked }]} />}
          table={<BucketTable start={start} bucketMs={bucketMs} series={timeline} />}
        >
          <StackedColumns start={start} bucketMs={bucketMs} series={timeline} label={`Lookups per ${per}, blocked and allowed`} />
        </ChartCard>

        {!single && (
          <ChartCard
            title="Activity by device"
            note="Lookups per column, stacked by device."
            legend={<Legend items={active.map((d) => ({ label: d.name, color: colorOf(d.id), value: d.queries }))} />}
            table={<BucketTable start={start} bucketMs={bucketMs} series={deviceSeries} />}
          >
            {deviceSeries.length ? (
              <StackedColumns start={start} bucketMs={bucketMs} series={deviceSeries} label={`Lookups per ${per} for each device`} />
            ) : (
              <p className="py-8 text-center text-sm text-muted">No lookups in this window.</p>
            )}
          </ChartCard>
        )}

        <div className="grid items-start gap-6 xl:grid-cols-2">
          <ChartCard title="Lookup types" note="What kind of answer each lookup asked for.">
            <Breakdown parts={typeParts} max={8} />
          </ChartCard>
          <ChartCard
            title="Answered by"
            note={t.avg_reply_ms !== null ? `Average reply ${Math.round(t.avg_reply_ms)} ms.` : 'Who answered each lookup.'}
          >
            <Breakdown parts={answerParts} max={8} />
          </ChartCard>
        </div>

        <div className={`grid items-start gap-6 lg:grid-cols-2 ${single ? '2xl:grid-cols-3' : '2xl:grid-cols-4'}`}>
          <ChartCard title="Most blocked">
            <RankedList
              color={DATA.blocked}
              empty="Nothing blocked in this window."
              rows={r.top_blocked.map((x) => ({ key: x.domain, label: <span className="font-mono text-[13px]">{x.domain}</span>, sub: x.owner, value: x.n }))}
            />
          </ChartCard>
          <ChartCard title="Most looked up">
            <RankedList
              color={DATA.allowed}
              empty="No allowed lookups in this window."
              rows={r.top_allowed.map((x) => ({ key: x.domain, label: <span className="font-mono text-[13px]">{x.domain}</span>, sub: x.owner, value: x.n }))}
            />
          </ChartCard>
          <ChartCard title="Top data collectors" note="Companies behind ad, tracking and telemetry lookups.">
            <RankedList
              color={BAR_NEUTRAL}
              empty="No trackers seen in this window."
              rows={r.top_companies.map((c) => ({
                key: c.owner,
                label: c.owner,
                sub: `${c.domains} ${c.domains === 1 ? 'domain' : 'domains'}, ${fmt(c.blocked)} blocked`,
                value: c.n,
              }))}
            />
          </ChartCard>
          {!single && (
            <ChartCard title="Busiest devices">
              <ul className="grid gap-3.5">
                {[...r.devices]
                  .sort((a, b) => b.queries - a.queries)
                  .map((d) => {
                    const top = Math.max(1, ...r.devices.map((x) => x.queries))
                    return (
                      <li key={d.id}>
                        <div className="flex items-baseline justify-between gap-3 text-sm">
                          <Link to={`/devices/${d.id}`} className="flex min-w-0 items-center gap-2 hover:text-white">
                            <DeviceIcon type={d.type} className="h-4 w-4 shrink-0 text-muted" />
                            <span className="underline-slide truncate">{d.name}</span>
                          </Link>
                          <span className="shrink-0 tabular-nums">{fmt(d.queries)}</span>
                        </div>
                        <div className="text-xs text-muted">
                          {d.queries ? `${Math.round((d.blocked / d.queries) * 100)}% blocked` : 'No lookups in this window'}
                        </div>
                        <div className="mt-1.5 h-1 rounded-full" style={{ width: `${Math.max(1.5, (d.queries / top) * 100)}%`, background: colorOf(d.id) }} />
                      </li>
                    )
                  })}
              </ul>
            </ChartCard>
          )}
        </div>
      </div>
    </div>
  )
}

function foldTypes(rows: Analytics['query_types']) {
  // fixed order (A, AAAA, HTTPS, PTR, other): the colours were validated as neighbours in this order
  const parts = Object.keys(TYPES).flatMap((k) => {
    const x = rows.find((r) => r.name === k)
    return x ? [{ label: TYPES[k].label, value: x.n, color: TYPES[k].color }] : []
  })
  const other = rows.filter((x) => !TYPES[x.name])
  if (other.length) parts.push({ label: `Other (${other.map((x) => x.name).join(', ')})`, value: other.reduce((a, x) => a + x.n, 0), color: OTHER_TYPE })
  return parts
}

function foldAnswers(rows: Analytics['answered_by']) {
  const parts: { label: string; value: number; color: string }[] = []
  const ups = rows.filter((x) => x.name !== 'blocked' && x.name !== 'cache').sort((a, b) => a.name.localeCompare(b.name))
  for (const x of rows) {
    if (x.name === 'blocked') parts.push({ label: 'Blocked', value: x.n, color: DATA.blocked })
    if (x.name === 'cache') parts.push({ label: resolverName('cache'), value: x.n, color: DATA.cached })
  }
  // resolvers keep a colour by name (alphabetical), and past three they fold into one row
  ups.slice(0, UPSTREAM_COLORS.length - 1).forEach((x, i) => parts.push({ label: resolverName(x.name), value: x.n, color: UPSTREAM_COLORS[i] }))
  const rest = ups.slice(UPSTREAM_COLORS.length - 1)
  if (rest.length === 1) parts.push({ label: resolverName(rest[0].name), value: rest[0].n, color: UPSTREAM_COLORS.at(-1)! })
  else if (rest.length > 1) parts.push({ label: 'Other resolvers', value: rest.reduce((a, x) => a + x.n, 0), color: UPSTREAM_COLORS.at(-1)! })
  return parts // fixed order (blocked, cache, resolvers A to Z), the order the colours were validated in
}

function Stat({
  label,
  value,
  foot,
  to,
  action,
  highlight,
}: {
  label: string
  value: string
  foot: string
  to?: string
  action?: string
  highlight?: boolean
}) {
  return (
    <div className={highlight ? 'pixel-corner flex flex-col rounded-md bg-brand p-4 text-white sm:p-5' : 'card card-hover flex flex-col p-4 sm:p-5'}>
      <div className={`text-sm ${highlight ? 'text-white/85' : 'text-muted'}`}>{label}</div>
      <div className="mt-2 font-display text-3xl font-normal sm:text-4xl">{value}</div>
      <div className={`mt-auto flex flex-wrap items-center justify-between gap-x-3 gap-y-1 pt-4 text-xs ${highlight ? 'text-white/85' : 'text-muted'}`}>
        <span>{foot}</span>
        {to && action && (
          <Link to={to} className={`underline-slide shrink-0 ${highlight ? 'text-white' : 'text-body hover:text-white'}`}>
            {action}
          </Link>
        )}
      </div>
    </div>
  )
}
