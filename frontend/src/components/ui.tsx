import { Check, Copy, Laptop, Monitor, Router, Smartphone, Tv } from 'lucide-react'
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import type { Category, DeviceType } from '../api'

const CAT: Record<Category, { label: string; cls: string }> = {
  malware: { label: 'Threat', cls: 'bg-danger/15 text-danger' },
  tracking: { label: 'Tracker', cls: 'bg-warn/15 text-warn' },
  telemetry: { label: 'Telemetry', cls: 'bg-orange-400/15 text-orange-300' },
  ads: { label: 'Ads', cls: 'bg-accent/15 text-accent' },
  needed: { label: 'Needed', cls: 'bg-guard/15 text-guard' },
  unknown: { label: 'Unknown', cls: 'bg-slate-500/20 text-slate-300' },
}

export function CategoryChip({ category, acr }: { category: Category; acr?: boolean }) {
  const c = CAT[category] ?? CAT.unknown
  return <span className={`chip ${c.cls}`}>{acr ? 'Watches your screen' : c.label}</span>
}

export function ConfidenceChip({ level }: { level: 'high' | 'medium' | 'low' }) {
  const cls = level === 'high' ? 'text-guard' : level === 'medium' ? 'text-warn' : 'text-muted'
  return <span className={`chip bg-panel-2 ${cls}`}>{level} confidence</span>
}

export function RiskBar({ score }: { score: number }) {
  const color = score >= 60 ? 'bg-danger' : score >= 30 ? 'bg-warn' : 'bg-guard'
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-panel-2">
        <div className={`h-full ${color}`} style={{ width: `${Math.max(4, score)}%` }} />
      </div>
      <span className="text-xs tabular-nums text-muted">{score}</span>
    </div>
  )
}

export function DeviceIcon({ type, className = 'h-5 w-5' }: { type: DeviceType; className?: string }) {
  const I = { tv: Tv, phone: Smartphone, laptop: Laptop, router: Router, other: Monitor }[type] ?? Monitor
  return <I className={className} />
}

export function CopyField({ label, value, mono = true }: { label?: string; value: string; mono?: boolean }) {
  const [done, setDone] = useState(false)
  return (
    <div>
      {label && <div className="mb-1 text-xs text-muted">{label}</div>}
      <div className="flex items-center gap-2 rounded-xl border border-line bg-panel-2 px-3 py-2">
        <span className={`flex-1 break-all text-sm ${mono ? 'font-mono' : ''}`}>{value}</span>
        <button
          className="rounded-lg p-1.5 text-muted hover:bg-panel hover:text-slate-100"
          onClick={() => {
            navigator.clipboard?.writeText(value)
            setDone(true)
            setTimeout(() => setDone(false), 1500)
          }}
          aria-label="Copy"
        >
          {done ? <Check className="h-4 w-4 text-guard" /> : <Copy className="h-4 w-4" />}
        </button>
      </div>
    </div>
  )
}

export function timeAgo(iso: string | null): string {
  if (!iso) return 'never'
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="card p-8 text-center">
      <div className="font-semibold">{title}</div>
      {children && <div className="mt-2 text-sm text-muted">{children}</div>}
    </div>
  )
}

// ---------- toast with Undo ----------
interface ToastMsg {
  id: number
  text: string
  action?: { label: string; run: () => void }
}
const ToastCtx = createContext<(t: Omit<ToastMsg, 'id'>) => void>(() => {})
export const useToast = () => useContext(ToastCtx)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastMsg[]>([])
  const seq = useRef(0)
  const push = useCallback((t: Omit<ToastMsg, 'id'>) => {
    const id = ++seq.current
    setToasts((xs) => [...xs, { ...t, id }])
    setTimeout(() => setToasts((xs) => xs.filter((x) => x.id !== id)), 7000)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 bottom-20 z-50 flex flex-col items-center gap-2 px-4 md:bottom-6">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="pointer-events-auto flex w-full max-w-md items-center gap-3 rounded-xl border border-line bg-panel-2 px-4 py-3 text-sm shadow-2xl"
          >
            <span className="flex-1">{t.text}</span>
            {t.action && (
              <button
                className="font-semibold text-accent hover:underline"
                onClick={() => {
                  t.action!.run()
                  setToasts((xs) => xs.filter((x) => x.id !== t.id))
                }}
              >
                {t.action.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
