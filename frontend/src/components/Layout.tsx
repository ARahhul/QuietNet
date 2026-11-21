import { useQuery } from '@tanstack/react-query'
import { Bell, ChartColumn, Cpu, Settings, ShieldCheck, ShieldAlert } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { api } from '../api'

const NAV = [
  { to: '/', label: 'Suggestions', icon: ShieldCheck, end: true },
  { to: '/devices', label: 'Devices', icon: Cpu },
  { to: '/alerts', label: 'Alerts', icon: Bell },
  { to: '/report', label: 'Report', icon: ChartColumn },
  { to: '/settings', label: 'Settings', icon: Settings },
]

export default function Layout() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15000 })
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: 15000 })
  const unread = alerts.data?.filter((a) => !a.read).length ?? 0
  const ok = health.data?.adguard.ok

  return (
    <div className="mx-auto flex min-h-full max-w-6xl">
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col gap-1 border-r border-line p-4 md:flex">
        <Brand />
        <nav className="mt-6 flex flex-col gap-1">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-xl px-3 py-2 text-sm ${isActive ? 'bg-panel-2 text-white' : 'text-muted hover:text-slate-100'}`
              }
            >
              <n.icon className="h-4 w-4" />
              {n.label}
              {n.to === '/alerts' && unread > 0 && (
                <span className="ml-auto rounded-full bg-danger px-1.5 text-[10px] font-bold text-ink">{unread}</span>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto text-xs text-muted">
          <StatusDot ok={ok} />
          {ok ? 'Gateway online' : health.isLoading ? 'Checking…' : 'Gateway unreachable'}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-line px-4 py-3 md:hidden">
          <Brand />
          <span className="text-xs text-muted">
            <StatusDot ok={ok} />
            {ok ? 'Protected' : 'Offline'}
          </span>
        </header>
        <main className="flex-1 px-4 pb-28 pt-5 md:px-8 md:pb-10">
          <Outlet />
        </main>
      </div>

      <nav className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-5 border-t border-line bg-panel/95 backdrop-blur md:hidden">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) =>
              `relative flex flex-col items-center gap-1 py-2.5 text-[11px] ${isActive ? 'text-white' : 'text-muted'}`
            }
          >
            <n.icon className="h-5 w-5" />
            {n.label}
            {n.to === '/alerts' && unread > 0 && <span className="absolute right-5 top-1.5 h-2 w-2 rounded-full bg-danger" />}
          </NavLink>
        ))}
      </nav>
    </div>
  )
}

function Brand() {
  return (
    <div className="flex items-center gap-2">
      <div className="grid h-8 w-8 place-items-center rounded-xl bg-guard/15 text-guard">
        <ShieldAlert className="h-5 w-5" />
      </div>
      <div>
        <div className="text-base font-bold leading-none">Pahra</div>
        <div className="text-[10px] text-muted">guard on watch</div>
      </div>
    </div>
  )
}

function StatusDot({ ok }: { ok?: boolean }) {
  return <span className={`mr-1.5 inline-block h-2 w-2 rounded-full ${ok ? 'bg-guard' : 'bg-danger'}`} />
}
