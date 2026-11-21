import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ShieldAlert, Tv, Wrench } from 'lucide-react'
import { api } from '../api'
import { Empty, timeAgo } from '../components/ui'

const TITLE: Record<string, string> = {
  malware: 'Threat blocked',
  phishing: 'Phishing blocked',
  odd_behaviour: 'Odd behaviour',
  acr_tracker: 'Your TV is watching you',
  new_tracker: 'Blocked automatically',
}

export default function Alerts() {
  const qc = useQueryClient()
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: 15000 })
  const read = useMutation({ mutationFn: api.readAlert, onSuccess: () => qc.invalidateQueries({ queryKey: ['alerts'] }) })
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-bold">Alerts</h1>
      <p className="mt-1 text-sm text-muted">What Pahra noticed, and how to fix it at the source.</p>
      <div className="mt-5 grid gap-4">
        {alerts.data?.length === 0 && <Empty title="No alerts">Nothing risky so far.</Empty>}
        {alerts.data?.map((a) => (
          <article key={a.id} className={`card p-4 ${a.read ? 'opacity-70' : ''} ${a.type === 'malware' ? 'border-danger/60' : ''}`}>
            <div className="flex items-center gap-2 text-xs text-muted">
              {a.type === 'acr_tracker' ? <Tv className="h-4 w-4 text-warn" /> : <ShieldAlert className="h-4 w-4 text-danger" />}
              <span className="font-semibold uppercase tracking-wide">{TITLE[a.type] ?? a.type}</span>
              <span>· {a.device_name ?? 'device'} · {timeAgo(a.created_at)}</span>
            </div>
            <p className="mt-2 font-medium">{a.message}</p>
            {a.fix_steps?.steps && a.fix_steps.steps.length > 0 && (
              <div className="mt-3 rounded-xl bg-panel-2 p-3">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <Wrench className="h-4 w-4 text-accent" /> {a.fix_steps.title}
                </div>
                <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-slate-300">
                  {a.fix_steps.steps.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ol>
                {!a.fix_steps.checked && (
                  <div className="mt-2 text-[11px] text-muted">Menu names can differ by model and software version.</div>
                )}
              </div>
            )}
            {!a.read && (
              <button className="btn-ghost mt-3 px-3 py-1.5 text-xs" onClick={() => read.mutate(a.id)}>
                Mark as done
              </button>
            )}
          </article>
        ))}
      </div>
    </div>
  )
}
