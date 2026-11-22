import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LogIn, LogOut, UserRound } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { CREDITS } from '../lib/credits'
import { Eyebrow, Roll, timeAgo } from '../components/ui'
import { firebaseEnabled, signOut, useSession } from '../session'

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

      <AccountCard />

      <div className="card mt-6 flex items-center gap-4 p-5">
        <div className="flex-1">
          <div className="font-display text-xl">Auto-mode</div>
          <div className="mt-1 text-sm text-muted">
            Block high-confidence ads, trackers and threats without asking, when blocking them won't break anything.
            The rest wait in Suggestions. You get a notification and can undo any time.
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

function Row({ label, ok, detail }: { label: string; ok?: boolean; detail?: string | null }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 p-4">
      <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${ok ? 'bg-guard' : 'bg-warn'}`} />
      <span className="min-w-0 font-medium sm:w-48 sm:shrink-0">{label}</span>
      <span className="min-w-0 basis-full pl-[22px] text-muted sm:flex-1 sm:basis-0 sm:truncate sm:pl-0">{detail}</span>
    </div>
  )
}

/** Who is signed in here, and signing out (on a shared computer). */
function AccountCard() {
  const session = useSession()
  const navigate = useNavigate()
  const account = useQuery({ queryKey: ['account'], queryFn: api.account })
  if (session.status !== 'signed-in') return null
  const firebase = session.via === 'firebase'
  return (
    <div className="card mt-6 flex flex-wrap items-center gap-x-4 gap-y-3 p-5" data-testid="account">
      {firebase && session.photo ? (
        <img src={session.photo} alt="" className="h-10 w-10 shrink-0 rounded-full" referrerPolicy="no-referrer" />
      ) : (
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-panel-2 text-muted">
          <UserRound className="h-5 w-5" strokeWidth={1.75} />
        </span>
      )}
      <div className="min-w-0 flex-1 basis-56">
        <div className="truncate font-medium">
          {firebase ? (session.email ?? session.name ?? 'Signed in') : 'Using the server’s access token'}
        </div>
        <div className="mt-1 text-sm text-muted">
          {firebase
            ? 'This is your home’s account. Only you see its devices, lookups and blocks.'
            : account.data?.operator
              ? 'The token opens the server’s first home.' + (firebaseEnabled ? ' Sign in with Google or email to move it into your own account.' : '')
              : 'Forget it on a shared computer.'}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {!firebase && firebaseEnabled && (
          <Link to="/login?upgrade=1&next=%2Fsettings" className="btn-primary">
            <Roll>
              <LogIn className="h-4 w-4" /> Sign in
            </Roll>
          </Link>
        )}
        <button
          className="btn-ghost"
          onClick={() => {
            navigate('/welcome')
            void signOut()
          }}
        >
          <Roll>
            <LogOut className="h-4 w-4" /> {firebase ? 'Sign out' : 'Forget token'}
          </Roll>
        </button>
      </div>
    </div>
  )
}
