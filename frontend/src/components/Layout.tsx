import { useQuery } from '@tanstack/react-query'
import { Activity, Baby, Ban, Bell, ChartColumn, Cpu, LogOut, Plus, Settings, ShieldCheck, UserRound } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { signOut, takeMovedHome, useSession } from '../session'
import { Roll, useToast } from './ui'

const NAV = [
  { to: '/', label: 'Suggestions', short: 'Suggested', icon: ShieldCheck, end: true }, // short: the phone tab bar's label
  { to: '/live', label: 'Live', icon: Activity },
  { to: '/blocked', label: 'Blocked', icon: Ban },
  { to: '/devices', label: 'Devices', icon: Cpu },
  { to: '/parental', label: 'Parental', icon: Baby },
  { to: '/alerts', label: 'Alerts', icon: Bell },
  { to: '/report', label: 'Report', icon: ChartColumn },
  { to: '/settings', label: 'Settings', icon: Settings, mobileHeader: true }, // phones: a gear in the header, not a tab
]

export default function Layout() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15000 })
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: 15000 })
  const unread = alerts.data?.filter((a) => !a.read).length ?? 0
  const ok = health.data?.adguard.ok
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices, refetchInterval: 15000 })
  // "Protected" only once one of this home's devices is really sending lookups (not just "the server is up")
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 30000)
    return () => clearInterval(t)
  }, [])
  const live = (devices.data ?? []).some((d) => d.last_seen && now - Date.parse(d.last_seen) < LIVE_MS)
  const status: Status = !ok ? 'offline' : live ? 'protected' : 'waiting'
  const toast = useToast()

  useEffect(() => {
    if (takeMovedHome()) toast({ text: 'Your devices moved into your account. Sign in anywhere to see them.' })
  }, [toast])

  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line px-4 py-6 md:flex">
        <Brand />
        <nav className="mt-8 flex flex-col gap-1">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `group flex items-center gap-3 rounded-full px-4 py-2.5 text-sm transition ${isActive ? 'bg-panel-2 text-white' : 'text-muted hover:text-white'}`
              }
            >
              {({ isActive }) => (
                <>
                  <n.icon className="h-4 w-4 transition-transform motion-safe:group-hover:scale-110" strokeWidth={1.75} />
                  <span className={isActive ? '' : 'underline-slide'}>{n.label}</span>
                  {n.to === '/alerts' && unread > 0 && (
                    <span className="ml-auto rounded-full bg-brand px-1.5 py-0.5 text-[10px] leading-none font-semibold text-white">{unread}</span>
                  )}
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto grid gap-5">
          <Link to="/setup" className="btn-light w-full">
            <Roll><Plus className="h-4 w-4" /> Connect a device</Roll>
          </Link>
          <AccountRow />
          <div className="grid gap-1.5 text-xs text-muted">
            <div className="flex items-center">
              <StatusDot status={ok ? 'protected' : 'offline'} />
              {ok ? 'Gateway online' : health.isLoading ? 'Checking…' : 'Gateway unreachable'}
            </div>
            <Link to="/settings#credits" className="text-[11px] hover:text-white">
              DNS filtering by <span className="underline-slide">AdGuard Home</span>
            </Link>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-line px-4 py-3 md:hidden">
          <Brand />
          <div className="flex items-center gap-3">
            <Link
              to={status === 'waiting' ? '/setup' : '/devices'}
              className="flex items-center text-xs text-muted"
              title={STATUS_HINT[status]}
            >
              <StatusDot status={status} />
              {STATUS_LABEL[status]}
            </Link>
            <NavLink
              to="/settings"
              aria-label="Settings"
              className={({ isActive }) => `rounded-full p-2 transition ${isActive ? 'bg-panel-2 text-white' : 'text-muted hover:text-white'}`}
            >
              <Settings className="h-5 w-5" strokeWidth={1.75} />
            </NavLink>
          </div>
        </header>
        <main className="flex flex-1 flex-col px-4 pt-6 pb-28 md:px-10 md:pt-10 md:pb-12 2xl:px-14">
          <Outlet />
        </main>
      </div>

      <nav className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-7 border-t border-line bg-ink/95 backdrop-blur md:hidden">
        {NAV.filter((n) => !n.mobileHeader).map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) =>
              `relative flex min-w-0 flex-col items-center gap-1 py-2.5 text-[9px] tracking-tight ${isActive ? 'text-white' : 'text-muted'}`
            }
          >
            <n.icon className="h-5 w-5" strokeWidth={1.75} />
            <span className="max-w-full truncate">{n.short ?? n.label}</span>
            {n.to === '/alerts' && unread > 0 && <span className="absolute top-1.5 left-1/2 ml-2 h-2 w-2 rounded-full bg-brand" />}
          </NavLink>
        ))}
      </nav>
    </div>
  )
}

function AccountRow() {
  const session = useSession()
  const navigate = useNavigate()
  if (session.status !== 'signed-in') return null
  const label = session.via === 'firebase' ? (session.email ?? session.name ?? 'Signed in') : 'Access token'
  return (
    <div className="flex items-center gap-2.5 border-t border-line pt-4 text-xs">
      {session.via === 'firebase' && session.photo ? (
        <img src={session.photo} alt="" className="h-7 w-7 shrink-0 rounded-full" referrerPolicy="no-referrer" />
      ) : (
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-panel-2 text-muted">
          <UserRound className="h-3.5 w-3.5" />
        </span>
      )}
      <span className="min-w-0 flex-1 truncate text-body" title={label}>
        {label}
      </span>
      <button
        className="rounded-full p-1.5 text-muted transition hover:bg-panel-2 hover:text-white"
        onClick={() => {
          navigate('/welcome')
          void signOut()
        }}
        aria-label={session.via === 'firebase' ? 'Sign out' : 'Forget token'}
        title={session.via === 'firebase' ? 'Sign out' : 'Forget token'}
      >
        <LogOut className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}

export function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <img src="/quietnet-mark.png" alt="" width={32} height={40} className="h-10 w-8 shrink-0" />
      <div>
        <div className="font-display text-xl leading-none font-semibold tracking-[-0.01em]">
          Quiet<span className="text-sentry">Net</span>
        </div>
        <div className="mt-1 text-[10px] text-muted">The silent home sentry</div>
      </div>
    </div>
  )
}

type Status = 'protected' | 'waiting' | 'offline'
const LIVE_MS = 10 * 60 * 1000 // a lookup this recent counts as "protected"
const STATUS_LABEL: Record<Status, string> = { protected: 'Protected', waiting: 'Not connected', offline: 'Offline' }
const STATUS_HINT: Record<Status, string> = {
  protected: 'A device of yours sent lookups through QuietNet in the last 10 minutes.',
  waiting: 'QuietNet is up, but none of your devices has sent a lookup in the last 10 minutes.',
  offline: 'QuietNet can’t be reached right now.',
}
const DOT: Record<Status, string> = { protected: 'bg-guard', waiting: 'bg-warn', offline: 'bg-danger' }

function StatusDot({ status }: { status: Status }) {
  return <span className={`mr-1.5 inline-block h-2 w-2 shrink-0 rounded-full ${DOT[status]}`} />
}
