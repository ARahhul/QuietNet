import { Plus, Search, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { ParentalService } from '../../api'

/** The apps parents reach for first (only those this AdGuard knows are shown) */
const POPULAR = ['youtube', 'tiktok', 'instagram', 'snapchat', 'facebook', 'roblox', 'fortnite', 'minecraft', 'discord', 'netflix', 'twitch', 'reddit']

const GROUP_LABEL: Record<string, string> = {
  gaming: 'Games',
  social_network: 'Social media',
  streaming: 'Video & streaming',
  messenger: 'Messaging',
  gambling: 'Betting & gambling',
  dating: 'Dating',
  shopping: 'Shopping',
  ai: 'AI chat',
  software: 'Apps & software',
  privacy: 'VPNs & proxies',
  hosting: 'File sharing',
  cdn: 'Content networks',
}
/** Groups a parent may want to block whole, in one tap */
const WHOLE_GROUPS = ['gaming', 'social_network', 'gambling', 'dating']

export function AppIcon({ service, className = 'h-5 w-5' }: { service?: ParentalService; className?: string }) {
  if (!service?.icon_svg) return <span className={`${className} inline-block rounded bg-panel-2`} />
  return <img src={`data:image/svg+xml;base64,${service.icon_svg}`} alt="" className={`${className} shrink-0 rounded`} />
}

/** Pick apps: what's chosen as removable chips, and "Add apps" opens a searchable, grouped catalog. */
export default function AppPicker({
  services,
  selected,
  onChange,
  empty = 'No apps chosen.',
}: {
  services: ParentalService[]
  selected: string[]
  onChange: (ids: string[]) => void
  empty?: string
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const byId = useMemo(() => new Map(services.map((s) => [s.id, s])), [services])
  const chosen = new Set(selected)
  const toggle = (id: string) => onChange(chosen.has(id) ? selected.filter((x) => x !== id) : [...selected, id])

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const match = (s: ParentalService) => !needle || s.name.toLowerCase().includes(needle) || s.id.includes(needle)
    const out: { key: string; label: string; items: ParentalService[] }[] = []
    if (!needle) {
      const popular = POPULAR.map((id) => byId.get(id)).filter((s): s is ParentalService => !!s)
      if (popular.length) out.push({ key: 'popular', label: 'Popular with kids', items: popular })
    }
    const grouped = new Map<string, ParentalService[]>()
    for (const s of services.filter(match)) grouped.set(s.group, [...(grouped.get(s.group) ?? []), s])
    for (const [g, items] of [...grouped].sort(([a], [b]) => (GROUP_LABEL[a] ?? 'zz').localeCompare(GROUP_LABEL[b] ?? 'zz'))) {
      out.push({ key: g || 'other', label: GROUP_LABEL[g] ?? 'Other', items: items.sort((a, b) => a.name.localeCompare(b.name)) })
    }
    return out
  }, [services, byId, q])

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        {selected.length === 0 && <span className="text-sm text-muted">{empty}</span>}
        {selected.map((id) => (
          <span key={id} className="flex items-center gap-1.5 rounded-full border border-line bg-panel-2 py-1 pr-1 pl-2 text-xs">
            <AppIcon service={byId.get(id)} className="h-4 w-4" />
            {byId.get(id)?.name ?? id}
            <button
              type="button"
              onClick={() => toggle(id)}
              aria-label={`Stop blocking ${byId.get(id)?.name ?? id}`}
              className="rounded-full p-0.5 text-muted transition hover:bg-panel hover:text-white"
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="flex items-center gap-1 rounded-full border border-dashed border-line-strong px-3 py-1 text-xs text-muted transition hover:border-white hover:text-white"
        >
          {open ? <X className="h-3 w-3" /> : <Plus className="h-3 w-3" />} {open ? 'Close' : 'Add apps'}
        </button>
      </div>

      {open && (
        <div className="mt-3 rounded-md border border-line bg-ink p-3">
          {!services.length ? (
            <p className="text-sm text-muted">The app list comes from the DNS server, and it can’t be reached right now. Try again in a minute.</p>
          ) : (
            <>
              <label className="flex items-center gap-2 rounded-md border border-line bg-panel px-3 py-2">
                <Search className="h-4 w-4 text-muted" />
                <input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Search apps and sites"
                  className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted"
                />
              </label>
              {!q && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {WHOLE_GROUPS.filter((g) => services.some((s) => s.group === g)).map((g) => {
                    const ids = services.filter((s) => s.group === g).map((s) => s.id)
                    const all = ids.every((id) => chosen.has(id))
                    return (
                      <button
                        key={g}
                        type="button"
                        onClick={() => onChange(all ? selected.filter((id) => !ids.includes(id)) : [...new Set([...selected, ...ids])])}
                        className={`rounded-full border px-3 py-1 text-xs transition ${all ? 'border-brand bg-brand/15 text-white' : 'border-line text-muted hover:text-white'}`}
                      >
                        {all ? 'All' : 'Block all'} {GROUP_LABEL[g].toLowerCase()} ({ids.length})
                      </button>
                    )
                  })}
                </div>
              )}
              <div className="mt-3 max-h-80 overflow-y-auto pr-1">
                {groups.map((g) => (
                  <div key={g.key} className="mb-3">
                    <div className="mb-1.5 text-[11px] tracking-wide text-muted uppercase">{g.label}</div>
                    <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-4">
                      {g.items.map((s) => (
                        <button
                          key={s.id}
                          type="button"
                          aria-pressed={chosen.has(s.id)}
                          onClick={() => toggle(s.id)}
                          className={`flex min-w-0 items-center gap-2 rounded-md border px-2 py-1.5 text-left text-xs transition ${chosen.has(s.id) ? 'border-brand bg-brand/15 text-white' : 'border-line text-body hover:border-line-strong'}`}
                        >
                          <AppIcon service={s} className="h-4 w-4" />
                          <span className="truncate">{s.name}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
                {!groups.length && <p className="text-sm text-muted">No app called “{q}”.</p>}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
