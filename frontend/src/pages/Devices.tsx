import { useQuery } from '@tanstack/react-query'
import { ChevronRight, Plus } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { DeviceIcon, Empty, RiskBar, timeAgo } from '../components/ui'

export default function Devices() {
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices, refetchInterval: 10000 })
  return (
    <div className="mx-auto max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Devices</h1>
        <Link to="/setup" className="btn-primary">
          <Plus className="h-4 w-4" /> Add device
        </Link>
      </div>
      <div className="mt-5 grid gap-3">
        {devices.data?.length === 0 && <Empty title="No devices yet">Add your phone, laptop or TV to start.</Empty>}
        {devices.data?.map((d) => (
          <Link key={d.id} to={`/devices/${d.id}`} className="card flex items-center gap-4 p-4 hover:border-muted">
            <div className="grid h-11 w-11 place-items-center rounded-xl bg-panel-2">
              <DeviceIcon type={d.type} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 font-semibold">
                {d.name}
                <span className={`h-2 w-2 rounded-full ${d.online ? 'bg-guard' : 'bg-slate-600'}`} />
              </div>
              <div className="text-xs text-muted">
                {d.last_seen ? `seen ${timeAgo(d.last_seen)}` : 'waiting for first query'} · {d.stats.trackers} trackers ·{' '}
                {d.stats.blocked_queries} lookups blocked
              </div>
            </div>
            <div className="text-right">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">risk</div>
              <RiskBar score={d.risk_score} />
            </div>
            <ChevronRight className="h-4 w-4 text-muted" />
          </Link>
        ))}
      </div>
    </div>
  )
}
