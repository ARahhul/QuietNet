import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Ban, Loader2, ScanSearch, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { api, type AdCandidate, type AdHunt as Hunt } from '../api'
import { ConfidenceChip, Roll, timeAgo, useToast } from './ui'

const WHEN = [
  { minutes: 5, label: 'Just now' },
  { minutes: 15, label: 'In the last 15 minutes' },
  { minutes: 60, label: 'In the last hour' },
  { minutes: 180, label: 'In the last 3 hours' },
]

const RISK = { low: 'text-guard', medium: 'text-warn', high: 'text-danger' } as const

/**
 * "Saw an ad?": say where, and the AI looks through what the household (or the picked device) looked up around
 * then for the names that served it, including an app's own ad servers that QuietNet otherwise keeps allowed.
 * Nothing is blocked until the person ticks names and presses Block; every block can be undone.
 */
export default function AdHunt({ deviceId, deviceName }: { deviceId?: string; deviceName?: string }) {
  const qc = useQueryClient()
  const toast = useToast()
  const [where, setWhere] = useState('')
  const [minutes, setMinutes] = useState(15)
  const [result, setResult] = useState<Hunt | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [problem, setProblem] = useState<string | null>(null)

  const refresh = () => {
    for (const key of ['suggestions', 'blocked', 'devices', 'decisions']) qc.invalidateQueries({ queryKey: [key] })
  }

  const find = useMutation({
    mutationFn: () => api.adHunt({ where: where.trim(), minutes, device_id: deviceId ?? null }),
    onMutate: () => {
      setProblem(null)
      setResult(null)
    },
    onSuccess: (r) => {
      setResult(r)
      setPicked(new Set(r.candidates.filter((c) => c.selected).map((c) => c.name)))
    },
    onError: (e) => setProblem((e as Error).message),
  })

  const block = useMutation({
    mutationFn: async (names: string[]) => {
      const done = []
      for (const n of names) {
        done.push(await api.decide({ domain: n, action: 'block', device_id: deviceId ?? null, confirm_must_allow: true }))
      }
      return done
    },
    onSuccess: (rs) => {
      refresh()
      setResult(null)
      setWhere('')
      const ids = rs.map((r) => r.decision.id)
      toast({
        text: `Blocked ${rs.length} name${rs.length === 1 ? '' : 's'}${deviceId ? '' : ' for the whole household'}. Close and reopen the app to see the change.`,
        action: { label: 'Undo', run: () => void api.undoBulk(ids).then(refresh) },
      })
    },
    onError: (e) => {
      refresh()
      setProblem(`Couldn't block everything: ${(e as Error).message}`)
    },
  })

  const toggle = (name: string) =>
    setPicked((s) => {
      const next = new Set(s)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  return (
    <section className="card p-5" data-testid="ad-hunt">
      <div className="flex items-center gap-2 font-display text-lg">
        <ScanSearch className="h-4 w-4 text-guard" /> Saw an ad?
      </div>
      <p className="mt-1 max-w-prose text-sm text-muted">
        Say where you saw it. The AI checks what {deviceName ?? 'your devices'} looked up around then and finds the names that
        served it, even an app's own ad servers.
      </p>
      <form
        className="mt-3 flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (where.trim().length >= 2 && !find.isPending) find.mutate()
        }}
      >
        <input
          className="input min-w-0 flex-1 basis-64"
          placeholder="e.g. Hotstar app, banner on the home screen"
          maxLength={120}
          value={where}
          onChange={(e) => setWhere(e.target.value)}
        />
        <select className="input w-auto" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} aria-label="When">
          {WHEN.map((w) => (
            <option key={w.minutes} value={w.minutes}>
              {w.label}
            </option>
          ))}
        </select>
        <button className="btn-primary" disabled={find.isPending || where.trim().length < 2}>
          <Roll>
            {find.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <ScanSearch className="h-4 w-4" />} Find it
          </Roll>
        </button>
      </form>
      {find.isPending && <p className="mt-3 text-sm text-muted">Reading the latest lookups and asking the AI…</p>}
      {problem && <div className="mt-3 text-sm text-danger">{problem}</div>}

      {result && (
        <div className="mt-4 border-t border-line pt-4">
          <p className="text-sm text-body">{result.summary}</p>
          <p className="mt-1 text-xs text-muted">
            Checked {result.looked_at} name{result.looked_at === 1 ? '' : 's'} looked up in the last {result.minutes} minutes.
          </p>
          {result.in_stream && (
            <div className="mt-3 flex gap-2 rounded-md bg-warn/10 p-3 text-sm text-warn">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /> {result.note}
            </div>
          )}
          {result.candidates.length > 0 && (
            <>
              <ul className="mt-3 grid gap-2">
                {result.candidates.map((c) => (
                  <Candidate key={c.name} c={c} checked={picked.has(c.name)} onToggle={() => toggle(c.name)} />
                ))}
              </ul>
              <button
                className="btn-primary mt-3"
                disabled={!picked.size || block.isPending}
                onClick={() => block.mutate([...picked])}
              >
                <Roll>
                  {block.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Ban className="h-4 w-4" />} Block{' '}
                  {picked.size} name{picked.size === 1 ? '' : 's'}
                </Roll>
              </button>
            </>
          )}
        </div>
      )}
    </section>
  )
}

function Candidate({ c, checked, onToggle }: { c: AdCandidate; checked: boolean; onToggle: () => void }) {
  return (
    <li>
      <label className="flex cursor-pointer gap-3 rounded-md border border-line p-3 transition hover:border-line-strong">
        <input type="checkbox" className="mt-1 h-4 w-4 shrink-0 accent-[var(--color-guard)]" checked={checked} onChange={onToggle} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm break-all text-white">{c.name}</span>
            <span className="chip bg-panel-2 text-white">{c.role}</span>
            <ConfidenceChip level={c.confidence} />
            <span className="ml-auto text-xs text-muted">
              {c.lookups}× · {timeAgo(c.last_seen)}
            </span>
          </div>
          {c.owner && <div className="mt-0.5 text-xs text-muted">{c.owner}</div>}
          <p className="mt-1 text-sm text-body">{c.why}</p>
          {c.break_note && (
            <div className="mt-1 text-xs">
              <span className={RISK[c.break_risk]}>If blocked:</span> <span className="text-body">{c.break_note}</span>
            </div>
          )}
          {!c.selected && (
            <div className="mt-1 text-xs text-muted">
              Not ticked for you: QuietNet can’t confirm this name only serves ads. It may also carry the app’s normal
              pictures or features.
            </div>
          )}
          {c.must_allow && (
            <div className="mt-1 text-xs text-warn">
              Part of an app QuietNet normally keeps allowed. Only this one name gets blocked; undo it if the app misbehaves.
            </div>
          )}
        </div>
      </label>
    </li>
  )
}
