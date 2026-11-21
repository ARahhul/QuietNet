import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { RiskBar } from '../components/ui'

export default function Report() {
  const r = useQuery({ queryKey: ['report'], queryFn: api.report, refetchInterval: 30000 }).data
  if (!r) return <div className="text-muted">Loading…</div>
  const pct = r.queries ? Math.round((r.blocked_queries / r.queries) * 100) : 0
  const max = Math.max(1, ...r.top_collectors.map((c) => c.queries))
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-bold">This week</h1>
      <p className="mt-1 text-sm text-muted">Since {new Date(r.since).toLocaleDateString()}</p>

      <div className="mt-5 grid grid-cols-3 gap-3">
        <Stat label="DNS lookups" value={r.queries.toLocaleString()} />
        <Stat label="Blocked" value={r.blocked_queries.toLocaleString()} />
        <Stat label="Blocked share" value={`${pct}%`} />
      </div>

      <h2 className="mt-8 text-lg font-semibold">Top data collectors</h2>
      <div className="card mt-3 divide-y divide-line">
        {r.top_collectors.length === 0 && <div className="p-4 text-sm text-muted">None seen yet.</div>}
        {r.top_collectors.map((c) => (
          <div key={c.owner} className="p-4">
            <div className="flex items-baseline justify-between text-sm">
              <span className="font-semibold">{c.owner}</span>
              <span className="text-xs text-muted">
                {c.queries} lookups · {c.blocked} blocked
              </span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-panel-2">
              <div className="h-full bg-warn" style={{ width: `${(c.queries / max) * 100}%` }} />
            </div>
            <div className="mt-1 font-mono text-[11px] text-muted">{c.domains.join(', ')}</div>
          </div>
        ))}
      </div>

      <h2 className="mt-8 text-lg font-semibold">Devices</h2>
      <div className="card mt-3 divide-y divide-line">
        {r.devices.map((d) => (
          <div key={d.id} className="flex items-center justify-between p-4 text-sm">
            <div>
              <div className="font-semibold">{d.name}</div>
              <div className="text-xs text-muted">
                {d.queries} lookups · {d.trackers} trackers · {d.blocked_queries} blocked lookups
              </div>
            </div>
            <RiskBar score={d.risk_score} />
          </div>
        ))}
      </div>
      {r.devices_to_check.length > 0 && (
        <div className="mt-4 rounded-xl border border-warn/50 bg-warn/10 p-4 text-sm text-warn">
          Worth checking: {r.devices_to_check.join(', ')}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 text-2xl font-bold tabular-nums">{value}</div>
    </div>
  )
}
