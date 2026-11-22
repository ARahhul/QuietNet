import { useQuery } from '@tanstack/react-query'
import { ChevronRight, Plus } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { DeviceIcon, Empty, RiskBar, Roll, timeAgo } from '../components/ui'

export default function Devices() {
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices, refetchInterval: 10000 })
  return (
    <div>
      <div className="flex items-center justify-between">
        <h1 className="page-title">Devices</h1>
        <Link to="/setup" className="btn-primary">
          <Roll><Plus className="h-4 w-4" /> Add device</Roll>
        </Link>
      </div>
      <div className="mt-6 grid gap-3 lg:grid-cols-2 min-[1800px]:grid-cols-3">
        {devices.data?.length === 0 && <Empty title="No devices yet">Add your phone, laptop or TV to start.</Empty>}
        {devices.data?.map((d) => (
          <Link key={d.id} to={`/devices/${d.id}`} className="card card-link group flex items-center gap-4 p-4">
            <div className="grid h-11 w-11 place-items-center rounded-md bg-panel-2 text-brand-soft">
              <DeviceIcon type={d.type} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 font-display text-lg leading-tight">
                {d.name}
                <span className={`h-2 w-2 rounded-full ${d.online ? 'bg-guard' : 'bg-line-strong'}`} />
              </div>
              <div className="mt-0.5 text-xs text-muted">
                {d.last_seen ? `seen ${timeAgo(d.last_seen)}` : 'waiting for first query'} · {d.stats.trackers} trackers ·{' '}
                {d.stats.blocked_queries} lookups blocked
              </div>
            </div>
            <div className="text-right">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">risk</div>
              <RiskBar score={d.risk_score} />
            </div>
            <ChevronRight className="h-4 w-4 text-muted transition group-hover:text-white motion-safe:group-hover:translate-x-1" />
          </Link>
        ))}
      </div>
    </div>
  )
}
