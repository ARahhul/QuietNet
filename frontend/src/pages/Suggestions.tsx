import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, ShieldCheck, Zap } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import SuggestionCard from '../components/SuggestionCard'
import { DeviceIcon, Empty, useToast } from '../components/ui'

export default function Suggestions() {
  const qc = useQueryClient()
  const toast = useToast()
  const [deviceId, setDeviceId] = useState<string | undefined>()
  const [later, setLater] = useState<Set<string>>(new Set())
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices, refetchInterval: 10000 })
  const sugg = useQuery({
    queryKey: ['suggestions', deviceId],
    queryFn: () => api.suggestions(deviceId),
    refetchInterval: 10000,
  })

  const cards = useMemo(() => (sugg.data ?? []).filter((s) => !later.has(s.domain)), [sugg.data, later])
  const blockable = cards.filter((c) => ['ads', 'tracking', 'telemetry'].includes(c.category) && c.confidence !== 'low' && !c.must_allow)
  const companies = new Set(cards.map((c) => c.owner).filter(Boolean)).size

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['suggestions'] })
    qc.invalidateQueries({ queryKey: ['devices'] })
  }
  const bulk = useMutation({
    mutationFn: () => api.bulk({ device_id: deviceId ?? null, domains: blockable.map((c) => c.domain) }),
    onSuccess: (r) => {
      refresh()
      toast({
        text: `Blocked ${r.count} ads & trackers${deviceId ? '' : ' for the whole household'}. Toggle airplane mode for 2 s to see it at once.`,
        action: { label: 'Undo', run: () => api.undoBulk(r.decisions.map((d) => d.id)).then(refresh) },
      })
    },
  })

  if (devices.data && devices.data.length === 0) {
    return (
      <div className="mx-auto max-w-lg pt-10 text-center">
        <ShieldCheck className="mx-auto h-14 w-14 text-guard" />
        <h1 className="mt-4 text-2xl font-bold">See who's feeding on your home network</h1>
        <p className="mt-2 text-muted">Connect your first device in under a minute. No hardware needed.</p>
        <Link to="/setup" className="btn-primary mt-6">
          <Plus className="h-4 w-4" /> Connect a device
        </Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Suggestions</h1>
          <p className="mt-1 text-sm text-muted">
            {cards.length} things to review{companies ? ` from ${companies} companies` : ''}. Pahra explains each one; you decide.
          </p>
        </div>
        <button className="btn-primary px-5 py-3 text-base" disabled={!blockable.length || bulk.isPending} onClick={() => bulk.mutate()}>
          <Zap className="h-5 w-5" /> Block all ads & trackers{blockable.length ? ` (${blockable.length})` : ''}
        </button>
      </div>

      <div className="mt-5 flex gap-2 overflow-x-auto pb-1">
        <Chip active={!deviceId} onClick={() => setDeviceId(undefined)}>
          All devices
        </Chip>
        {devices.data?.map((d) => (
          <Chip key={d.id} active={deviceId === d.id} onClick={() => setDeviceId(d.id)}>
            <DeviceIcon type={d.type} className="h-3.5 w-3.5" /> {d.name}
          </Chip>
        ))}
      </div>

      <div className="mt-5 grid gap-4">
        {sugg.isLoading && <div className="text-muted">Loading…</div>}
        {!sugg.isLoading && cards.length === 0 && (
          <Empty title="All clear">
            Nothing new to review. New domains are checked every 30 seconds.{' '}
            <button className="text-accent hover:underline" onClick={() => api.pollNow().then(() => setTimeout(refresh, 1500))}>
              Check now
            </button>
          </Empty>
        )}
        {cards.map((s) => (
          <SuggestionCard key={s.domain} s={s} onLater={() => setLater((x) => new Set(x).add(s.domain))} />
        ))}
      </div>
    </div>
  )
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs ${active ? 'border-accent bg-accent/15 text-white' : 'border-line text-muted hover:text-slate-100'}`}
    >
      {children}
    </button>
  )
}
