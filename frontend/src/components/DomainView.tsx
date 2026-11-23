import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Ban,
  BookCheck,
  Eye,
  Hand,
  ListX,
  Loader2,
  Lock,
  ShieldAlert,
  ShieldCheck,
  ShieldOff,
  X,
  type LucideIcon,
} from 'lucide-react'
import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { api, ApiError, type DomainDetail, type DomainReason, type ReasonKind } from '../api'
import { CategoryChip, ConfidenceChip, Roll, timeAgo, useToast } from './ui'

/**
 * "View": what a domain is, why it is (or isn't) blocked for this home or one device, and what this home looked up.
 * Every page that lists domains opens the same drawer through useDomainView() or a <ViewButton>.
 */

type Open = (name: string, deviceId?: string | null) => void
const ViewCtx = createContext<Open>(() => {})
export const useDomainView = () => useContext(ViewCtx)

export function DomainViewProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<{ name: string; deviceId: string | null } | null>(null)
  const open = useCallback<Open>((name, deviceId) => setTarget({ name: name.trim().replace(/\.$/, '').toLowerCase(), deviceId: deviceId ?? null }), [])
  const close = useCallback(() => setTarget(null), [])
  return (
    <ViewCtx.Provider value={open}>
      {children}
      {target && <Drawer key={`${target.name}|${target.deviceId}`} name={target.name} deviceId={target.deviceId} onClose={close} />}
    </ViewCtx.Provider>
  )
}

/** "View" for one domain. `link` sits inline with text, `icon` fits a dense row; `button` comes small or, next to
 * a full-size button, `md`. */
export function ViewButton({
  name,
  deviceId,
  variant = 'button',
  size = 'sm',
  className = '',
}: {
  name: string
  deviceId?: string | null
  variant?: 'button' | 'link' | 'icon'
  size?: 'sm' | 'md'
  className?: string
}) {
  const open = useDomainView()
  const go = (e: MouseEvent) => {
    e.preventDefault() // inside a <label> (Site fix), don't tick the box
    e.stopPropagation()
    open(name, deviceId)
  }
  const title = `What ${name} is and why it's blocked or allowed`
  if (variant === 'link')
    return (
      <button type="button" onClick={go} title={title} className={`inline-flex items-center gap-1 text-xs text-muted transition hover:text-white ${className}`}>
        <Eye className="h-3.5 w-3.5" /> <span className="underline-slide">View</span>
      </button>
    )
  if (variant === 'icon')
    return (
      <button
        type="button"
        onClick={go}
        title={title}
        aria-label={`View ${name}`}
        className={`rounded-full p-1.5 text-muted transition hover:bg-panel-2 hover:text-white ${className}`}
      >
        <Eye className="h-3.5 w-3.5" />
      </button>
    )
  return (
    <button type="button" onClick={go} title={title} className={`btn-ghost ${size === 'md' ? 'px-4 py-2' : 'px-3 py-1.5 text-xs'} ${className}`}>
      <Roll>
        <Eye className={size === 'md' ? 'h-4 w-4' : 'h-3.5 w-3.5'} /> View
      </Roll>
    </button>
  )
}

const BY: Record<NonNullable<DomainDetail['blocked_by']>, string> = {
  blocklist: "QuietNet's ad & tracker list blocks it for every home.",
  threat_list: 'A threat-intelligence list blocks it for every home.',
  yours: 'Your rule blocks it.',
  auto: 'Auto-mode blocked it.',
  held: 'Held as new until QuietNet knows what it is.',
  let_through: 'You let it through. Your allow rule wins over every block list.',
}

const REASON_ICON: Record<ReasonKind, { icon: LucideIcon; color: string }> = {
  must_allow: { icon: BookCheck, color: 'text-guard' },
  let_through: { icon: ShieldOff, color: 'text-guard' },
  held: { icon: Lock, color: 'text-info' },
  rule: { icon: Hand, color: 'text-brand-soft' },
  blocklist: { icon: ListX, color: 'text-brand-soft' },
  threat_list: { icon: ShieldAlert, color: 'text-danger' },
  clean: { icon: ShieldCheck, color: 'text-guard' },
}

const EXPLAINED: Record<string, string> = {
  blocklist: "from QuietNet's list, no AI needed",
  threat_list: 'from the threat list, no AI needed',
  clean_list: "from QuietNet's clean list, no AI needed",
  must_allow: 'from the must-allow list',
  lists: 'from the block lists',
  ai: 'explained by AI',
  gemini: 'explained by AI',
}

function Drawer({ name, deviceId, onClose }: { name: string; deviceId: string | null; onClose: () => void }) {
  const qc = useQueryClient()
  const toast = useToast()
  const titleId = useId()
  const closeBtn = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLElement>(null)
  const [scope, setScope] = useState<string | null>(deviceId)
  const [warn, setWarn] = useState<string | null>(null)
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices })
  const q = useQuery({ queryKey: ['domain', name, scope], queryFn: () => api.domain(name, scope) })
  const d = q.data
  const where = scope ? `on ${devices.data?.find((x) => x.id === scope)?.name ?? 'this device'}` : 'for everyone at home'

  // A dialog: focus moves in, Escape closes, Tab stays inside, the page behind doesn't scroll; focus goes back on close
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null
    closeBtn.current?.focus()
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      if (e.key !== 'Tab' || !panel.current) return
      const items = panel.current.querySelectorAll<HTMLElement>('button:not([disabled]), select, a[href], [tabindex]:not([tabindex="-1"])')
      const first = items[0]
      const last = items[items.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last?.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = overflow
      before?.focus?.()
    }
  }, [onClose])

  const refresh = () => {
    for (const k of ['domain', 'blocked', 'blocked-list', 'suggestions', 'devices', 'activity', 'decisions', 'live']) qc.invalidateQueries({ queryKey: [k] })
  }
  const failed = (e: unknown) => toast({ text: `Couldn't save: ${(e as Error).message}` })

  const unblock = useMutation({
    mutationFn: (names: string[]) => api.siteUnblock({ names, device_id: scope }),
    onSuccess: (r) => {
      refresh()
      toast({
        text: `Let ${d?.owner ?? name} through ${where}. Reload the site or reopen the app.${r.synced ? '' : ' (Will apply when the gateway is back.)'}`,
        action: { label: 'Undo', run: () => void api.undoBulk(r.decisions.map((x) => x.id), r.replaced).then(refresh) },
      })
    },
    onError: failed,
  })
  const blockAgain = useMutation({
    mutationFn: (ids: string[]) => api.undoBulk(ids),
    onSuccess: () => {
      refresh()
      toast({ text: `${d?.owner ?? name} is blocked again ${where}.` })
    },
    onError: failed,
  })
  const block = useMutation({
    mutationFn: (v: { domain: string; confirm?: boolean }) =>
      api.decide({ domain: v.domain, action: 'block', device_id: scope, confirm_must_allow: v.confirm }),
    onSuccess: (r) => {
      refresh()
      setWarn(null)
      toast({
        text: `Blocked ${d?.owner ?? name} ${where}${r.synced ? '' : ' (will sync when the gateway is back)'}`,
        action: { label: 'Undo', run: () => void api.undo(r.decision.id).then(refresh) },
      })
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) setWarn((e.detail as { message: string }).message)
      else failed(e)
    },
  })
  const busy = unblock.isPending || blockAgain.isPending || block.isPending

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/65 motion-safe:animate-scrim-in" onClick={onClose} aria-hidden="true" />
      <aside
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative flex h-full w-full flex-col overflow-y-auto border-line bg-panel shadow-2xl shadow-black/70 sm:max-w-lg sm:border-l motion-safe:animate-drawer-in"
      >
        <header className="sticky top-0 z-10 flex items-start gap-3 border-b border-line bg-panel px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="truncate font-display text-2xl leading-tight">
              {d?.owner ?? name}
            </h2>
            <div className="mt-0.5 font-mono text-xs break-all text-muted">{name}</div>
            {d && d.domain !== name && <div className="text-xs text-muted">Part of {d.domain}</div>}
          </div>
          <button ref={closeBtn} onClick={onClose} aria-label="Close" className="rounded-full p-2 text-muted transition hover:bg-panel-2 hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </header>

        <div className="grid gap-6 px-5 py-5 pb-24 sm:pb-8">
          {q.isLoading && (
            <p className="flex items-center gap-2 text-sm text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> Looking it up…
            </p>
          )}
          {q.isError && <p className="text-sm text-danger">Couldn't load it: {(q.error as Error).message}</p>}

          {d && (
            <>
              <section className={`rounded-md border p-4 ${d.status === 'blocked' ? 'border-brand/50 bg-brand/10' : 'border-line bg-ink'}`}>
                <div className="flex items-center gap-2 font-display text-lg">
                  {d.status === 'blocked' ? <Ban className="h-5 w-5 text-brand-soft" /> : <ShieldCheck className="h-5 w-5 text-guard" />}
                  {d.status === 'blocked' ? 'Blocked' : 'Allowed'} {where}
                </div>
                <p className="mt-1 text-sm text-body">
                  {d.blocked_by
                    ? BY[d.blocked_by]
                    : d.must_allow
                      ? 'Devices need it, so QuietNet never blocks it by itself.'
                      : 'On no block list, and no rule of yours covers it.'}
                </p>

                <label className="mt-3 flex items-center gap-2 text-xs text-muted">
                  Showing
                  <select className="input w-auto py-1.5 pr-8 text-xs" value={scope ?? ''} onChange={(e) => setScope(e.target.value || null)}>
                    <option value="">Whole home</option>
                    {devices.data?.map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.name}
                      </option>
                    ))}
                  </select>
                </label>

                <div className="mt-4 flex flex-wrap gap-2">
                  {d.actions.unblock && (
                    <button className="btn-primary px-4 py-2" disabled={busy} onClick={() => unblock.mutate(d.actions.unblock!)}>
                      <Roll>
                        {unblock.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldOff className="h-4 w-4" />} Unblock {where}
                      </Roll>
                    </button>
                  )}
                  {d.actions.undo_allow && (
                    <button className="btn-primary px-4 py-2" disabled={busy} onClick={() => blockAgain.mutate(d.actions.undo_allow!)}>
                      <Roll>
                        {blockAgain.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Ban className="h-4 w-4" />} Block again
                      </Roll>
                    </button>
                  )}
                  {d.actions.block && (
                    <button className="btn-ghost px-4 py-2" disabled={busy} onClick={() => block.mutate({ domain: d.actions.block! })}>
                      <Roll>
                        {block.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Ban className="h-4 w-4" />} Block {d.actions.block}{' '}
                        {where}
                      </Roll>
                    </button>
                  )}
                </div>
                {d.actions.unblock && d.actions.unblock.length > 1 && (
                  <p className="mt-2 text-xs text-muted">Lets through {d.actions.unblock.join(', ')}.</p>
                )}
                {warn && (
                  <div className="mt-3 flex gap-2 rounded-md border border-danger/50 bg-danger/10 p-3 text-xs text-danger">
                    <AlertTriangle className="h-4 w-4 shrink-0" />
                    <div className="flex-1">
                      {warn}
                      <div className="mt-2 flex gap-2">
                        <button className="btn-danger px-3 py-1.5" onClick={() => block.mutate({ domain: d.actions.block!, confirm: true })}>
                          <Roll>Block anyway</Roll>
                        </button>
                        <button className="btn-ghost px-3 py-1.5" onClick={() => setWarn(null)}>
                          <Roll>Cancel</Roll>
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </section>

              <section>
                <h3 className="text-xs font-medium tracking-wide text-muted uppercase">What it is</h3>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <CategoryChip category={d.category} acr={d.acr} />
                  {d.confidence && <ConfidenceChip level={d.confidence} />}
                  {d.must_allow && <span className="chip bg-guard/15 text-guard">Must allow</span>}
                </div>
                <p className={`mt-3 text-sm ${d.what_it_does ? 'text-body' : 'italic text-muted'}`}>
                  {d.what_it_does ?? (d.checked ? 'No description yet.' : "None of your devices has looked this one up yet, so QuietNet hasn't described it.")}
                </p>
                {d.break_note && (
                  <p className="mt-2 text-xs">
                    <span className={d.break_risk === 'high' ? 'text-danger' : d.break_risk === 'medium' ? 'text-warn' : 'text-guard'}>If blocked:</span>{' '}
                    <span className="text-body">{d.break_note}</span>
                  </p>
                )}
                {(d.sources.length > 0 || (d.explained_by && EXPLAINED[d.explained_by])) && (
                  <p className="mt-2 text-[11px] text-muted">
                    {d.sources.length > 0 && <>Source: {d.sources.join(' · ')}</>}
                    {d.explained_by && EXPLAINED[d.explained_by] && <>{d.sources.length > 0 ? ' · ' : ''}{EXPLAINED[d.explained_by]}</>}
                  </p>
                )}
              </section>

              <section>
                <h3 className="text-xs font-medium tracking-wide text-muted uppercase">Why</h3>
                {d.reasons.length === 0 ? (
                  <p className="mt-2 text-sm text-muted">It's on no list QuietNet uses, and no rule of yours covers it.</p>
                ) : (
                  <ul className="mt-2 grid gap-2">
                    {d.reasons.map((r, i) => (
                      <Reason key={i} r={r} name={name} />
                    ))}
                  </ul>
                )}
              </section>

              <section>
                <h3 className="text-xs font-medium tracking-wide text-muted uppercase">Looked up {scope ? 'by this device' : 'at home'}, last {d.hours} hours</h3>
                {d.looked_up.length === 0 ? (
                  <p className="mt-2 text-sm text-muted">No lookups {scope ? 'from this device' : 'from your devices'} in that time.</p>
                ) : (
                  <>
                    <ul className="mt-2 divide-y divide-line rounded-md border border-line">
                      {d.looked_up.map((x) => (
                        <li key={x.name} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
                          <span className="min-w-0 flex-1 font-mono text-xs break-all">{x.name}</span>
                          {x.let_through && <span className="chip bg-guard/15 text-guard">let through</span>}
                          <span className="text-xs text-muted tabular-nums">
                            {x.count.toLocaleString()}× {x.blocked > 0 && <>· {x.blocked.toLocaleString()} blocked</>} · {timeAgo(x.last_seen)}
                          </span>
                        </li>
                      ))}
                    </ul>
                    {!scope && d.devices.length > 0 && (
                      <p className="mt-2 text-xs text-muted">
                        From {d.devices.map((x) => `${x.name} (${x.count.toLocaleString()}×)`).join(', ')}
                      </p>
                    )}
                  </>
                )}
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

function Reason({ r, name }: { r: DomainReason; name: string }) {
  const { icon: Icon, color } = REASON_ICON[r.kind]
  const rule = r.kind === 'rule' || r.kind === 'let_through' || r.kind === 'held'
  return (
    <li className={`flex gap-3 rounded-md border border-line p-3 ${rule && r.applies === false ? 'opacity-60' : ''}`}>
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${color}`} />
      <div className="min-w-0 flex-1 text-sm">
        <div className="font-medium text-white">{r.title}</div>
        <p className="mt-0.5 text-body">{r.detail}</p>
        {rule && (
          <p className="mt-1 text-xs text-muted">
            {r.household ? 'Whole home' : `Only on ${r.device?.name ?? 'one device'}`}
            {r.rule_domain && r.rule_domain !== name && <> · rule on {r.rule_domain}</>}
            {r.created_at && <> · since {timeAgo(r.created_at)}</>}
          </p>
        )}
        {r.kind === 'blocklist' && r.active === false && (
          <p className="mt-1 text-xs text-warn">The list isn't loaded into the DNS server right now, so it isn't blocking yet.</p>
        )}
        {r.names && r.names.length > 0 && <p className="mt-1 text-xs text-muted">Listed names: {r.names.join(', ')}</p>}
        {r.sources && r.sources.length > 0 && <p className="mt-1 text-xs text-muted">Listed by {r.sources.join(' · ')}</p>}
        {r.notes && <p className="mt-1 text-xs text-muted">Note: {r.notes}</p>}
      </div>
    </li>
  )
}
