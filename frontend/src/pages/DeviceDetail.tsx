import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Radio, Trash2 } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import SetupSteps from '../components/SetupSteps'
import { CategoryChip, DeviceIcon, RiskBar, timeAgo, useToast } from '../components/ui'

export default function DeviceDetail() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const nav = useNavigate()
  const toast = useToast()
  const device = useQuery({ queryKey: ['device', id], queryFn: () => api.device(id), refetchInterval: 10000 })
  const activity = useQuery({ queryKey: ['activity', id], queryFn: () => api.activity(id), refetchInterval: 15000 })
  const setup = useQuery({ queryKey: ['setup', id], queryFn: () => api.setup(id) })
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['activity', id] })
    qc.invalidateQueries({ queryKey: ['suggestions'] })
  }
  const decide = useMutation({
    mutationFn: (v: { domain: string; action: 'block' | 'allow' }) => api.decide({ ...v, device_id: id, confirm_must_allow: true }),
    onSuccess: (r) => {
      refresh()
      toast({ text: `${r.decision.action === 'block' ? 'Blocked' : 'Allowed'} on this device`, action: { label: 'Undo', run: () => api.undo(r.decision.id).then(refresh) } })
    },
  })
  const undo = useMutation({ mutationFn: (decisionId: string) => api.undo(decisionId), onSuccess: refresh })
  const del = useMutation({
    mutationFn: () => api.deleteDevice(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['devices'] })
      nav('/devices')
    },
  })

  const d = device.data
  return (
    <div className="mx-auto max-w-3xl">
      <Link to="/devices" className="inline-flex items-center gap-1 text-sm text-muted hover:text-slate-100">
        <ArrowLeft className="h-4 w-4" /> Devices
      </Link>
      {d && (
        <div className="card mt-3 flex items-center gap-4 p-5">
          <div className="grid h-12 w-12 place-items-center rounded-xl bg-panel-2">
            <DeviceIcon type={d.type} className="h-6 w-6" />
          </div>
          <div className="flex-1">
            <div className="text-xl font-bold">{d.name}</div>
            <div className="font-mono text-xs text-muted">{d.client_id}</div>
            <div className="mt-1 text-xs text-muted">
              {d.online ? 'online' : `last seen ${timeAgo(d.last_seen)}`} · {d.stats.queries} queries · {d.stats.blocked_queries} blocked
            </div>
          </div>
          <RiskBar score={d.risk_score} />
        </div>
      )}

      <h2 className="mt-8 text-lg font-semibold">Who this device talks to</h2>
      <p className="text-sm text-muted">Grouped by company. Blocks here apply to this device only.</p>
      <div className="mt-3 grid gap-3">
        {activity.data?.length === 0 && <div className="text-sm text-muted">No traffic yet.</div>}
        {activity.data?.map((g) => (
          <div key={g.owner} className="card p-4">
            <div className="flex items-baseline justify-between">
              <div className="font-semibold">{g.owner}</div>
              <div className="text-xs text-muted">{g.count} queries</div>
            </div>
            <ul className="mt-2 divide-y divide-line">
              {g.domains.map((x) => (
                <li key={x.domain} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                  <span className="font-mono text-xs">{x.domain}</span>
                  <CategoryChip category={x.category} />
                  {x.beacon && <Radio className="h-3.5 w-3.5 text-warn" />}
                  <span className="ml-auto text-xs text-muted">{x.count}×</span>
                  {x.decision ? (
                    <button
                      className="btn-ghost px-2.5 py-1 text-xs"
                      disabled={x.decision.household}
                      title={x.decision.household ? 'Household rule. Undo it from Suggestions or Settings.' : ''}
                      onClick={() => undo.mutate(x.decision!.id)}
                    >
                      {x.decision.action === 'allow' ? 'allowed' : x.decision.action === 'quarantine' ? 'held' : 'blocked'} · undo
                    </button>
                  ) : x.category !== 'needed' ? (
                    <button className="btn-ghost px-2.5 py-1 text-xs" onClick={() => decide.mutate({ domain: x.domain, action: 'block' })}>
                      block
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      {setup.data && d && (
        <>
          <h2 className="mt-8 text-lg font-semibold">Connection settings</h2>
          <div className="card mt-3 p-5">
            <SetupSteps deviceId={d.id} guide={setup.data} brand={d.brand} />
          </div>
        </>
      )}

      <button className="btn-ghost mt-8 text-danger" onClick={() => confirm('Remove this device from Pahra?') && del.mutate()}>
        <Trash2 className="h-4 w-4" /> Remove device
      </button>
    </div>
  )
}
