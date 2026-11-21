import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Ban, Check, Clock, Lock, Radio } from 'lucide-react'
import { useState } from 'react'
import { api, ApiError, type Suggestion } from '../api'
import { CategoryChip, ConfidenceChip, timeAgo, useToast } from './ui'

export default function SuggestionCard({ s, onLater }: { s: Suggestion; onLater: () => void }) {
  const qc = useQueryClient()
  const toast = useToast()
  const [warn, setWarn] = useState<string | null>(null)
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['suggestions'] })
    qc.invalidateQueries({ queryKey: ['devices'] })
  }

  const decide = useMutation({
    mutationFn: (v: { action: 'block' | 'allow'; confirm?: boolean }) =>
      api.decide({ domain: s.domain, action: v.action, confirm_must_allow: v.confirm }),
    onSuccess: (r, v) => {
      refresh()
      setWarn(null)
      toast({
        text: `${v.action === 'block' ? 'Blocked' : 'Allowed'} ${s.owner ?? s.domain}${r.synced ? '' : ' (will sync when the gateway is back)'}`,
        action: { label: 'Undo', run: () => api.undo(r.decision.id).then(refresh) },
      })
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) setWarn((e.detail as { message: string }).message)
      else toast({ text: `Couldn't save: ${(e as Error).message}` })
    },
  })

  const beacon = s.devices.find((d) => d.beacon)
  const unknown = s.confidence === 'low'

  return (
    <article className={`card p-4 ${s.category === 'malware' ? 'border-danger/60' : s.acr ? 'border-warn/50' : ''}`}>
      <div className="flex flex-wrap items-center gap-2">
        <CategoryChip category={s.category} acr={s.acr} />
        <ConfidenceChip level={s.confidence} />
        {s.status === 'quarantined' && (
          <span className="chip bg-accent/15 text-accent">
            <Lock className="h-3 w-3" /> held
          </span>
        )}
        <span className="ml-auto text-xs text-muted">{s.total_count}× contacted</span>
      </div>

      <h3 className="mt-3 text-lg font-semibold leading-snug">{s.owner ?? (unknown ? 'Unknown company' : s.domain)}</h3>
      <div className="font-mono text-xs text-muted">{s.domain}</div>
      <p className={`mt-2 text-sm ${unknown ? 'italic text-muted' : 'text-slate-200'}`}>{s.what_it_does}</p>

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span>
          From: {s.devices.map((d) => d.name).join(', ')} · last {timeAgo(s.devices[0]?.last_ts)}
        </span>
        {beacon && (
          <span className="flex items-center gap-1 text-warn">
            <Radio className="h-3 w-3" /> checks in every ~{Math.round(beacon.avg_interval_s ?? 0)}s
          </span>
        )}
      </div>

      {s.break_note && (
        <div className="mt-2 text-xs">
          <span className={s.break_risk === 'high' ? 'text-danger' : s.break_risk === 'medium' ? 'text-warn' : 'text-guard'}>
            If blocked:
          </span>{' '}
          <span className="text-slate-300">{s.break_note}</span>
        </div>
      )}
      {s.sources.length > 0 && (
        <div className="mt-2 text-[11px] text-muted">
          Source: {s.sources.join(' · ')}
          {s.explained_by === 'gemini' && ' · explained by AI'}
        </div>
      )}

      {warn && (
        <div className="mt-3 flex gap-2 rounded-xl border border-danger/50 bg-danger/10 p-3 text-xs text-danger">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <div className="flex-1">
            {warn}
            <div className="mt-2 flex gap-2">
              <button className="btn-danger px-3 py-1.5" onClick={() => decide.mutate({ action: 'block', confirm: true })}>
                Block anyway
              </button>
              <button className="btn-ghost px-3 py-1.5" onClick={() => setWarn(null)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="mt-4 grid grid-cols-3 gap-2">
        <button className="btn-primary" disabled={decide.isPending} onClick={() => decide.mutate({ action: 'block' })}>
          <Ban className="h-4 w-4" /> Block
        </button>
        <button className="btn-ghost" disabled={decide.isPending} onClick={() => decide.mutate({ action: 'allow' })}>
          <Check className="h-4 w-4" /> Allow
        </button>
        <button className="btn-ghost" onClick={onLater}>
          <Clock className="h-4 w-4" /> Later
        </button>
      </div>
    </article>
  )
}
