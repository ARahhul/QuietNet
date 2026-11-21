import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Home, Lock, Search, ShieldOff } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type BlockedDomain } from '../api'
import { CategoryChip, DeviceIcon, Empty, FilterChip, Roll, timeAgo, useToast } from '../components/ui'

type Scope = 'all' | 'household' | string // or a device id
type Sort = 'newest' | 'stopped' | 'company'

const name = (b: BlockedDomain) => b.owner ?? b.domain

export default function Blocked() {
  const qc = useQueryClient()
  const toast = useToast()
  const [q, setQ] = useState('')
  const [scope, setScope] = useState<Scope>('all')
  const [sort, setSort] = useState<Sort>('newest')
  const blocked = useQuery({ queryKey: ['blocked'], queryFn: api.blocked, refetchInterval: 15000 })
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices, refetchInterval: 15000 })

  const all = useMemo(() => blocked.data ?? [], [blocked.data])
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const out = all.filter((b) => {
      if (scope === 'household' && b.device) return false
      if (scope !== 'all' && scope !== 'household') {
        // a device is covered by its own blocks and by household blocks it isn't exempt from
        const own = b.device?.id === scope
        const inherited = !b.device && !b.allowed_on.some((d) => d.id === scope)
        if (!own && !inherited) return false
      }
      return !needle || b.domain.includes(needle) || (b.owner ?? '').toLowerCase().includes(needle)
    })
    if (sort === 'stopped') out.sort((a, b) => b.blocked_count - a.blocked_count)
    if (sort === 'company') out.sort((a, b) => name(a).localeCompare(name(b)))
    return out
  }, [all, q, scope, sort])

  const companies = new Set(all.map((b) => b.owner).filter(Boolean)).size
  const attempts = all.reduce((n, b) => n + b.blocked_count, 0)

  const refresh = () => {
    for (const k of ['blocked', 'suggestions', 'devices', 'activity']) qc.invalidateQueries({ queryKey: [k] })
  }
  const unblock = useMutation({
    mutationFn: (b: BlockedDomain) => api.undo(b.id),
    onSuccess: (r, b) => {
      refresh()
      toast({
        text: `Unblocked ${name(b)}${b.device ? ` on ${b.device.name}` : ''}${r.synced ? '' : ' (will sync when the gateway is back)'}`,
        action: {
          label: 'Undo',
          run: () =>
            api
              .decide({ domain: b.domain, action: 'block', device_id: b.device?.id ?? null, confirm_must_allow: true })
              .then(refresh),
        },
      })
    },
    onError: (e) => toast({ text: `Couldn't unblock: ${(e as Error).message}` }),
  })

  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Blocked</h1>
          <p className="mt-2 max-w-prose text-sm text-muted">
            {blocked.isLoading
              ? 'Loading…'
              : all.length === 0
                ? 'Nothing is blocked yet.'
                : `${all.length} ${all.length === 1 ? 'domain' : 'domains'}${companies ? ` from ${companies} ${companies === 1 ? 'company' : 'companies'}` : ''} can't reach your devices. They've been turned away ${attempts.toLocaleString()} ${attempts === 1 ? 'time' : 'times'}.`}
          </p>
        </div>
        {all.length > 0 && (
          <label className="relative w-full sm:w-80">
            <span className="sr-only">Search blocked domains</span>
            <Search className="pointer-events-none absolute top-1/2 left-3.5 h-4 w-4 -translate-y-1/2 text-muted" />
            <input
              className="input pl-10"
              type="search"
              placeholder="Search domains or companies"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </label>
        )}
      </div>

      {all.length > 0 && (
        <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 gap-2 overflow-x-auto pb-1">
            <FilterChip active={scope === 'all'} onClick={() => setScope('all')}>
              Everything
            </FilterChip>
            <FilterChip active={scope === 'household'} onClick={() => setScope('household')}>
              <Home className="h-3.5 w-3.5" /> Whole household
            </FilterChip>
            {devices.data?.map((d) => (
              <FilterChip key={d.id} active={scope === d.id} onClick={() => setScope(d.id)}>
                <DeviceIcon type={d.type} className="h-3.5 w-3.5" /> {d.name}
              </FilterChip>
            ))}
          </div>
          <label className="flex items-center gap-2 text-xs text-muted">
            Sort by
            <select className="input w-auto py-1.5 pr-8 text-xs" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
              <option value="newest">Newest first</option>
              <option value="stopped">Most turned away</option>
              <option value="company">Company name</option>
            </select>
          </label>
        </div>
      )}

      <div className="mt-5">
        {!blocked.isLoading && all.length === 0 && (
          <Empty title="Nothing blocked yet">
            Block a tracker from{' '}
            <Link to="/" className="underline-slide text-guard">
              Suggestions
            </Link>{' '}
            and it shows up here, with how often it tried to get through.
          </Empty>
        )}
        {all.length > 0 && rows.length === 0 && (
          <Empty title="No matches">
            {q.trim() ? `Nothing blocked matches “${q.trim()}”${scope !== 'all' ? ' in this view' : ''}.` : 'Nothing blocked here.'}{' '}
            <button
              className="underline-slide text-guard"
              onClick={() => {
                setQ('')
                setScope('all')
              }}
            >
              Show everything
            </button>
          </Empty>
        )}
        {rows.length > 0 && (
          <ul className="card divide-y divide-line">
            {rows.map((b) => (
              <Row key={b.id} b={b} busy={unblock.isPending && unblock.variables?.id === b.id} onUnblock={() => unblock.mutate(b)} />
            ))}
          </ul>
        )}
        {rows.length > 0 && rows.length < all.length && (
          <p className="mt-3 text-xs text-muted">
            Showing {rows.length} of {all.length}.
          </p>
        )}
      </div>
    </div>
  )
}

function Row({ b, busy, onUnblock }: { b: BlockedDomain; busy: boolean; onUnblock: () => void }) {
  const held = b.action === 'quarantine'
  return (
    <li className="grid grid-cols-2 items-start gap-x-6 gap-y-3 p-4 transition-colors hover:bg-panel-2/40 lg:grid-cols-[minmax(0,2.4fr)_minmax(0,1fr)_minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,1fr)_auto] lg:items-center lg:px-5">
      <div className="col-span-2 min-w-0 lg:col-span-1">
        <div className="truncate font-display text-base">{name(b)}</div>
        {b.owner && <div className="truncate font-mono text-xs text-muted">{b.domain}</div>}
        {b.what_it_does && (
          <p className="mt-1 line-clamp-2 max-w-prose text-xs text-body lg:line-clamp-1" title={b.what_it_does}>
            {b.what_it_does}
          </p>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5">
        <CategoryChip category={b.category} />
        {held && (
          <span className="chip bg-info/15 text-info" title="QuietNet is holding this new domain until it knows what it is">
            <Lock className="h-3 w-3" /> Held for review
          </span>
        )}
      </div>

      <div className="min-w-0 text-sm">
        <div className="flex items-center gap-1.5">
          {b.device ? <DeviceIcon type={b.device.type} className="h-4 w-4 shrink-0 text-muted" /> : <Home className="h-4 w-4 shrink-0 text-muted" />}
          <span className="truncate">{b.device ? b.device.name : 'Whole household'}</span>
        </div>
        {b.allowed_on.length > 0 && (
          <div className="mt-0.5 truncate text-xs text-muted">Allowed on {b.allowed_on.map((d) => d.name).join(', ')}</div>
        )}
      </div>

      <div className="text-sm">
        <div className="tabular-nums">
          {b.blocked_count > 0 ? `Turned away ${b.blocked_count.toLocaleString()}×` : 'No attempts yet'}
        </div>
        {/* last_seen can predate the block; only an attempt after it counts as "tried" */}
        {b.last_seen && Date.parse(b.last_seen) > Date.parse(b.created_at) && (
          <div className="mt-0.5 text-xs text-muted">Last tried {timeAgo(b.last_seen)}</div>
        )}
      </div>

      <div className="text-sm">
        <div>{held ? 'Held' : 'Blocked'} {timeAgo(b.created_at)}</div>
        <div className="mt-0.5 text-xs text-muted">{b.decided_by === 'auto' ? 'Automatically' : 'By you'}</div>
      </div>

      <div className="col-span-2 lg:col-span-1 lg:justify-self-end">
        <button className="btn-ghost px-4 py-2" disabled={busy} onClick={onUnblock}>
          <Roll>
            <ShieldOff className="h-4 w-4" /> Unblock
          </Roll>
        </button>
      </div>
    </li>
  )
}
