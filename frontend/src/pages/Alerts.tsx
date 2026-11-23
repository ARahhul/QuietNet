import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Globe2, ShieldAlert, Tv, Wrench } from 'lucide-react'
import { api } from '../api'
import { ViewButton } from '../components/DomainView'
import { Empty, Roll, timeAgo } from '../components/ui'

const TITLE: Record<string, string> = {
  malware: 'Threat blocked',
  phishing: 'Phishing blocked',
  odd_behaviour: 'Odd behaviour',
  acr_tracker: 'Your TV is watching you',
  new_tracker: 'Blocked automatically',
  home_address: 'Home address followed',
  game: 'Game blocked',
}

export default function Alerts() {
  const qc = useQueryClient()
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: 15000 })
  const read = useMutation({ mutationFn: api.readAlert, onSuccess: () => qc.invalidateQueries({ queryKey: ['alerts'] }) })
  return (
    <div>
      <h1 className="page-title">Alerts</h1>
      <p className="mt-2 text-sm text-muted">What QuietNet noticed, and how to fix it at the source.</p>
      <div className="mt-6 grid items-start gap-4 xl:grid-cols-2">
        {alerts.data?.length === 0 && <Empty title="No alerts">Nothing risky so far.</Empty>}
        {alerts.data?.map((a) => (
          <article key={a.id} className={`card p-5 ${a.read ? 'opacity-70' : ''} ${a.type === 'malware' ? 'border-danger/60' : 'card-hover'}`}>
            <div className="flex items-center gap-2 text-xs text-muted">
              {a.type === 'acr_tracker' ? (
                <Tv className="h-4 w-4 text-warn" />
              ) : a.type === 'home_address' ? (
                <Globe2 className="h-4 w-4 text-guard" />
              ) : (
                <ShieldAlert className="h-4 w-4 text-danger" />
              )}
              <span className="font-medium text-white">{TITLE[a.type] ?? a.type}</span>
              <span>· {a.device_name ?? 'device'} · {timeAgo(a.created_at)}</span>
            </div>
            <p className="mt-3 font-display text-lg leading-snug">{a.message}</p>
            {a.fix_steps?.steps && a.fix_steps.steps.length > 0 && (
              <div className="mt-4 rounded-md border border-line bg-ink p-4">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Wrench className="h-4 w-4 text-guard" /> {a.fix_steps.title}
                </div>
                <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-body">
                  {a.fix_steps.steps.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ol>
                {!a.fix_steps.checked && (
                  <div className="mt-2 text-[11px] text-muted">Menu names can differ by model and software version.</div>
                )}
              </div>
            )}
            {(a.domain || !a.read) && (
              <div className="mt-4 flex flex-wrap gap-2">
                {a.domain && <ViewButton name={a.domain} deviceId={a.device_id} />}
                {!a.read && (
                  <button className="btn-ghost px-4 py-1.5 text-xs" onClick={() => read.mutate(a.id)}>
                    <Roll>Mark as done</Roll>
                  </button>
                )}
              </div>
            )}
          </article>
        ))}
      </div>
    </div>
  )
}
