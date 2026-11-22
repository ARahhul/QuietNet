import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, forgetToken, hasSavedToken } from '../api'
import { Eyebrow, timeAgo } from '../components/ui'

export default function Settings() {
  const qc = useQueryClient()
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15000 })
  const patch = useMutation({
    mutationFn: api.patchSettings,
    onSuccess: (s) => qc.setQueryData(['settings'], s),
  })
  const s = settings.data
  const h = health.data
  return (
    <div className="max-w-5xl">
      <h1 className="page-title">Settings</h1>

      <div className="card mt-6 flex items-center gap-4 p-5">
        <div className="flex-1">
          <div className="font-display text-xl">Auto-mode</div>
          <div className="mt-1 text-sm text-muted">
            Block high-confidence ads, trackers and threats without asking. You get a notification and can undo any time.
          </div>
        </div>
        <button
          role="switch"
          aria-checked={!!s?.auto_mode}
          onClick={() => s && patch.mutate({ auto_mode: !s.auto_mode })}
          className={`relative h-7 w-12 shrink-0 rounded-full border transition ${s?.auto_mode ? 'border-brand bg-brand' : 'border-line bg-panel-2'}`}
        >
          <span className={`absolute top-[3px] h-5 w-5 rounded-full bg-white transition-all ${s?.auto_mode ? 'left-[23px]' : 'left-[3px]'}`} />
        </button>
      </div>

      <Eyebrow className="mt-10">System status</Eyebrow>
      <div className="card mt-4 divide-y divide-line text-sm">
        <Row label="DNS gateway" ok={h?.adguard.ok} detail={h?.adguard.ok ? `AdGuard Home ${h.adguard.version ?? ''}` : h?.adguard.error} />
        <Row
          label="Query-log poller"
          ok={!!h && !h.poller.error}
          detail={h?.poller.error ?? `every ${h?.poller.interval_s}s · last ${timeAgo(h?.poller.last_poll ?? null)}`}
        />
        <Row label="Rule sync" ok={!!h && !h.sync.dirty} detail={h?.sync.error ?? `last push ${timeAgo(h?.sync.last_ok ?? null)}`} />
        <Row
          label="Blocklists"
          ok={!!h && h.lists.loaded_from !== 'empty'}
          detail={h ? `${Object.values(h.lists.lists).reduce((a, b) => a + b, 0).toLocaleString()} domains · ${h.lists.tracker_radar_entries} Tracker Radar · ${h.lists.loaded_from}` : ''}
        />
        <Row
          label="AI explainer"
          ok={!!h?.llm.enabled && !h.llm.error}
          detail={h?.llm.enabled ? (h.llm.error ?? `${h.llm.model} · ${h.domains_explained} domains explained`) : 'No AI key set: using curated + list-based text'}
        />
        <Row
          label="Home companion (TV setup)"
          ok={!!h?.companion.connected}
          detail={h?.companion.enabled ? (h.companion.error ?? `${h.companion.tools?.length ?? 0} tools`) : 'disabled'}
        />
        {!!h?.forwarder?.devices && (
          <Row
            label="Laptop DNS relay (locked TVs)"
            ok={h.forwarder.running}
            detail={
              h.forwarder.running
                ? `${h.forwarder.devices} TV${h.forwarder.devices > 1 ? 's' : ''} → ${h.forwarder.listen_ip}:53. Keep this laptop on.`
                : (h.forwarder.error ?? 'stopped')
            }
          />
        )}
      </div>

      {hasSavedToken() && (
        <div className="card mt-6 flex flex-wrap items-center gap-4 p-5">
          <div className="min-w-0 flex-1">
            <div className="font-medium">Access token saved in this browser</div>
            <div className="mt-1 text-sm text-muted">Forget it on a shared computer. You'll need the token again to sign back in.</div>
          </div>
          <button className="btn-ghost" onClick={forgetToken}>
            Forget token
          </button>
        </div>
      )}

      <Eyebrow className="mt-10">
        <span id="credits">Built on</span>
      </Eyebrow>
      <p className="mt-2 max-w-prose text-sm text-muted">
        QuietNet is the dashboard and the brains. The cloud DNS that every device talks to is open-source software and
        public services, credited here with thanks.
      </p>
      <ul className="card mt-4 divide-y divide-line text-sm">
        {CREDITS.map((c) => (
          <li key={c.name} className="flex flex-col gap-1 p-4 sm:flex-row sm:items-baseline sm:gap-4">
            <a href={c.url} target="_blank" rel="noreferrer" className="w-56 shrink-0 font-medium hover:text-white">
              <span className="underline-slide">{c.name}</span>
            </a>
            <span className="min-w-0 flex-1 text-body">{c.role}</span>
            {c.license && <span className="shrink-0 text-xs text-muted">{c.license}</span>}
          </li>
        ))}
      </ul>

      {s && (
        <div className="mt-6 text-xs text-muted">
          DNS hostname: <span className="font-mono">{s.dns_hostname}</span>
          {s.vm_ip && (
            <>
              {' '}· server IP <span className="font-mono">{s.vm_ip}</span>
            </>
          )}
          <br />
          Privacy: domain logs auto-delete after 7 days. Only domain names and public facts are ever sent to the AI.
        </div>
      )}
    </div>
  )
}

// The cloud DNS stack in infra/ and the data the engine classifies with (see README, Credits).
const CREDITS: { name: string; url: string; role: string; license?: string }[] = [
  {
    name: 'AdGuard Home',
    url: 'https://github.com/AdguardTeam/AdGuardHome',
    role: 'The DNS server on the cloud VM: answers every lookup over DoT and DoH, applies your block rules per device, and keeps the query log QuietNet reads.',
    license: 'GPL-3.0',
  },
  {
    name: 'Cloudflare 1.1.1.1 and Quad9',
    url: 'https://www.quad9.net',
    role: 'Upstream resolvers AdGuard Home asks, over TLS, for lookups that are allowed.',
  },
  {
    name: "Let's Encrypt and Certbot",
    url: 'https://letsencrypt.org',
    role: 'The free wildcard certificate that lets each device reach its own encrypted DNS address.',
    license: 'Certbot: Apache-2.0',
  },
  { name: 'Caddy', url: 'https://github.com/caddyserver/caddy', role: 'Optional HTTPS front for DNS-over-HTTPS in the hardened setup.', license: 'Apache-2.0' },
  {
    name: 'HaGeZi DNS blocklists',
    url: 'https://github.com/hagezi/dns-blocklists',
    role: 'Tracker and smart-TV lists (LG, Samsung, Amazon, Roku) that QuietNet uses to recognise domains.',
    license: 'GPL-3.0',
  },
  {
    name: 'DuckDuckGo Tracker Radar',
    url: 'https://github.com/duckduckgo/tracker-radar',
    role: 'Which company owns a tracking domain and what it collects.',
    license: 'CC BY-NC-SA 4.0',
  },
  { name: 'Inter and Inter Tight', url: 'https://rsms.me/inter/', role: 'Typefaces by Rasmus Andersson.', license: 'SIL OFL 1.1' },
]

function Row({ label, ok, detail }: { label: string; ok?: boolean; detail?: string | null }) {
  return (
    <div className="flex items-center gap-3 p-4">
      <span className={`h-2.5 w-2.5 rounded-full ${ok ? 'bg-guard' : 'bg-warn'}`} />
      <span className="w-48 shrink-0 font-medium">{label}</span>
      <span className="min-w-0 flex-1 truncate text-muted">{detail}</span>
    </div>
  )
}
