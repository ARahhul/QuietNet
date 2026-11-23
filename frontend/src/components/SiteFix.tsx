import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Baby, ImagePlus, Loader2, SearchCheck, ShieldOff, TriangleAlert, Wrench, X } from 'lucide-react'
import { useCallback, useRef, useState, type ClipboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { api, type SiteCandidate, type SiteFix as Fix } from '../api'
import { shrink } from '../lib/image'
import { ViewButton } from './DomainView'
import { ConfidenceChip, Roll, timeAgo, useToast } from './ui'

const WHEN = [
  { minutes: 5, label: 'Just now' },
  { minutes: 15, label: 'In the last 15 minutes' },
  { minutes: 60, label: 'In the last hour' },
  { minutes: 180, label: 'In the last 3 hours' },
]

const BLOCKED_BY: Record<SiteCandidate['blocked_by'], string> = {
  yours: 'Your rule',
  auto: 'Auto-mode',
  held: 'Held as new',
  blocklist: 'QuietNet list',
  list: 'Threat list',
}

/**
 * "Site not working?": say which site or app (or paste a screenshot of it failing), and QuietNet finds what it
 * blocked around then that the site needs, whether one of its own rules, Auto-mode, a hold on a new domain or a
 * block list stopped it. Nothing stays blocked for good: tick names and press Unblock. Every unblock can be undone.
 */
export default function SiteFix({ deviceId, deviceName }: { deviceId?: string; deviceName?: string }) {
  const qc = useQueryClient()
  const toast = useToast()
  const [what, setWhat] = useState('')
  const [minutes, setMinutes] = useState(15)
  const [image, setImage] = useState<string | null>(null)
  const [result, setResult] = useState<Fix | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [problem, setProblem] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const refresh = () => {
    for (const key of ['suggestions', 'blocked', 'devices', 'decisions', 'activity']) qc.invalidateQueries({ queryKey: [key] })
  }

  const take = useCallback(async (file: Blob | null | undefined) => {
    if (!file) return
    setProblem(null)
    if (!file.type.startsWith('image/')) {
      setProblem("That isn't a picture. Paste or choose a screenshot.")
      return
    }
    try {
      setImage(await shrink(file))
    } catch {
      setProblem("Couldn't open that picture. Try a PNG or JPEG screenshot.")
    }
  }, [])

  const onPaste = (e: ClipboardEvent) => {
    const item = [...e.clipboardData.items].find((i) => i.kind === 'file' && i.type.startsWith('image/'))
    if (!item) return
    e.preventDefault()
    void take(item.getAsFile())
  }

  const find = useMutation({
    mutationFn: () => api.siteFix({ what: what.trim(), image, minutes, device_id: deviceId ?? null }),
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

  const unblock = useMutation({
    mutationFn: (names: string[]) => api.siteUnblock({ names, device_id: deviceId ?? null }),
    onSuccess: (r) => {
      refresh()
      setResult(null)
      setWhat('')
      setImage(null)
      const n = r.decisions.length
      toast({
        text: `Unblocked ${n} name${n === 1 ? '' : 's'}${deviceName ? ` on ${deviceName}` : ' for everyone at home'}. Reload the site or reopen the app.${r.synced ? '' : ' (Will apply when the gateway is back.)'}`,
        action: { label: 'Undo', run: () => void api.undoBulk(r.decisions.map((d) => d.id), r.replaced).then(refresh) },
      })
    },
    onError: (e) => setProblem(`Couldn't unblock: ${(e as Error).message}`),
  })

  const toggle = (name: string) =>
    setPicked((s) => {
      const next = new Set(s)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  const ready = what.trim().length >= 2 || !!image

  return (
    <section className="card p-5" data-testid="site-fix" onPaste={onPaste}>
      <div className="flex items-center gap-2 font-display text-lg">
        <Wrench className="h-4 w-4 text-guard" /> Site or app not working?
      </div>
      <p className="mt-1 max-w-prose text-sm text-muted">
        Say which one, or paste a screenshot of it. QuietNet finds what it blocked on {deviceName ?? 'your devices'} around
        then, and you can let it through. Nothing stays blocked for good.
      </p>
      <form
        className="mt-3 flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (ready && !find.isPending) find.mutate()
        }}
      >
        <input
          className="input min-w-0 flex-1 basis-64"
          placeholder="e.g. IRCTC payment page won't load"
          maxLength={160}
          value={what}
          onChange={(e) => setWhat(e.target.value)}
        />
        <select className="input w-auto" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} aria-label="When">
          {WHEN.map((w) => (
            <option key={w.minutes} value={w.minutes}>
              {w.label}
            </option>
          ))}
        </select>
        <button className="btn-primary" disabled={find.isPending || !ready}>
          <Roll>
            {find.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <SearchCheck className="h-4 w-4" />} Find what's blocking it
          </Roll>
        </button>
      </form>

      <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
        {image ? (
          <>
            <img src={image} alt="Your screenshot" className="h-14 w-20 rounded border border-line bg-white object-cover" />
            <button type="button" className="btn-ghost px-3 py-1.5 text-xs" onClick={() => setImage(null)}>
              <Roll>
                <X className="h-3.5 w-3.5" /> Remove screenshot
              </Roll>
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn-ghost px-3 py-1.5 text-xs" onClick={() => fileInput.current?.click()}>
              <Roll>
                <ImagePlus className="h-3.5 w-3.5" /> Add a screenshot
              </Roll>
            </button>
            <span className="text-xs text-muted">or paste one into the box above. Cover passwords and card numbers first.</span>
          </>
        )}
        <input
          ref={fileInput}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => {
            void take(e.target.files?.[0])
            e.target.value = ''
          }}
        />
      </div>

      {find.isPending && <p className="mt-3 text-sm text-muted">Reading what was blocked{image ? ' and your screenshot' : ''}…</p>}
      {problem && <div className="mt-3 text-sm text-danger">{problem}</div>}

      {result && (
        <div className="mt-4 border-t border-line pt-4">
          <p className="text-sm text-body">{result.summary}</p>
          {result.looked_at > 0 && (
            <p className="mt-1 text-xs text-muted">
              Checked {result.looked_at} name{result.looked_at === 1 ? '' : 's'} QuietNet blocked in the last {result.minutes} minutes.
            </p>
          )}
          {result.note && (
            <div className={`mt-3 flex gap-2 rounded-md p-3 text-sm ${result.not_quietnet ? 'bg-panel-2 text-body' : 'bg-warn/10 text-warn'}`}>
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /> {result.note}
            </div>
          )}
          {result.already_unblocked.length > 0 && (
            <p className="mt-2 text-xs text-muted">Already let through since then: {result.already_unblocked.join(', ')}</p>
          )}
          {result.parental?.length > 0 && (
            <p className="mt-2 flex flex-wrap items-center gap-1 text-xs text-muted">
              <Baby className="h-3.5 w-3.5 text-sentry" /> Blocked by parental controls: {result.parental.join(', ')}.{' '}
              <Link to="/parental" className="underline-slide text-guard">
                Change them on the Parental page
              </Link>
            </p>
          )}
          {result.candidates.length > 0 && (
            <>
              <ul className="mt-3 grid gap-2">
                {result.candidates.map((c) => (
                  <Candidate key={c.name} c={c} deviceId={deviceId} checked={picked.has(c.name)} onToggle={() => toggle(c.name)} />
                ))}
              </ul>
              <button className="btn-primary mt-3" disabled={!picked.size || unblock.isPending} onClick={() => unblock.mutate([...picked])}>
                <Roll>
                  {unblock.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldOff className="h-4 w-4" />} Unblock{' '}
                  {picked.size} name{picked.size === 1 ? '' : 's'}
                  {deviceName ? ` on ${deviceName}` : ' for everyone'}
                </Roll>
              </button>
            </>
          )}
        </div>
      )}
    </section>
  )
}

function Candidate({ c, deviceId, checked, onToggle }: { c: SiteCandidate; deviceId?: string; checked: boolean; onToggle: () => void }) {
  const others = c.looked_up.filter((n) => n !== c.name)
  return (
    <li>
      <label className="flex cursor-pointer gap-3 rounded-md border border-line p-3 transition hover:border-line-strong">
        <input type="checkbox" className="mt-1 h-4 w-4 shrink-0 accent-[var(--color-guard)]" checked={checked} onChange={onToggle} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm break-all text-white">{c.name}</span>
            <span className="chip bg-panel-2 text-white" title="What blocked it">
              {BLOCKED_BY[c.blocked_by]}
            </span>
            <ConfidenceChip level={c.confidence} />
            <span className="ml-auto text-xs text-muted">
              blocked {c.times}× · {timeAgo(c.last_seen)}
            </span>
          </div>
          {c.owner && <div className="mt-0.5 text-xs text-muted">{c.owner}</div>}
          {c.why && <p className="mt-1 text-sm text-body">{c.why}</p>}
          {others.length > 0 && <div className="mt-1 text-xs text-muted">Looked up as {others.join(', ')}</div>}
          {c.devices.length > 0 && <div className="mt-0.5 text-xs text-muted">Blocked on {c.devices.map((d) => d.name).join(', ')}</div>}
          {c.warning && <div className={`mt-1 text-xs ${c.category === 'malware' ? 'text-danger' : 'text-warn'}`}>{c.warning}</div>}
          <ViewButton name={c.name} deviceId={deviceId ?? (c.devices.length === 1 ? c.devices[0].id : null)} variant="link" className="mt-1.5" />
        </div>
      </label>
    </li>
  )
}
