import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, ShieldCheck, Zap } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import SuggestionCard from '../components/SuggestionCard'
import { DeviceIcon, Empty, FilterChip, Roll, useToast } from '../components/ui'

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
      <div className="relative -mx-4 -mt-6 overflow-hidden border-b border-line px-6 pt-16 pb-20 text-center md:-mx-10 md:-mt-10 md:pt-28 md:pb-28 2xl:-mx-14">
        <div
          aria-hidden
          className="grid-lines pointer-events-none absolute inset-0 [mask-image:radial-gradient(ellipse_at_center,#000_25%,transparent_75%)]"
        />
        <div className="relative mx-auto max-w-2xl">
          <div className="mx-auto grid h-14 w-14 place-items-center rounded-md border border-line bg-panel text-guard">
            <ShieldCheck className="h-7 w-7" strokeWidth={1.5} />
          </div>
          <h1 className="mt-8 font-display text-4xl leading-[1.1] font-normal tracking-[-0.01em] md:text-6xl">
            See who's feeding on your <span className="text-guard">home network</span>
          </h1>
          <p className="mx-auto mt-5 max-w-md text-body">Connect your first device in under a minute. No hardware needed.</p>
          <Link to="/setup" className="btn-primary mt-8 px-6 py-3 text-base">
            <Roll><Plus className="h-4 w-4" /> Connect a device</Roll>
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="page-title">Suggestions</h1>
          <p className="mt-2 text-sm text-muted">
            {cards.length} things to review{companies ? ` from ${companies} companies` : ''}. QuietNet explains each one; you decide.
          </p>
        </div>
        <button className="btn-primary px-6 py-3 text-base" disabled={!blockable.length || bulk.isPending} onClick={() => bulk.mutate()}>
          <Roll><Zap className="h-5 w-5" /> Block all ads & trackers{blockable.length ? ` (${blockable.length})` : ''}</Roll>
        </button>
      </div>

      <div className="mt-6 flex gap-2 overflow-x-auto pb-1">
        <FilterChip active={!deviceId} onClick={() => setDeviceId(undefined)}>
          All devices
        </FilterChip>
        {devices.data?.map((d) => (
          <FilterChip key={d.id} active={deviceId === d.id} onClick={() => setDeviceId(d.id)}>
            <DeviceIcon type={d.type} className="h-3.5 w-3.5" /> {d.name}
          </FilterChip>
        ))}
      </div>

      <div className="mt-5 grid gap-4 xl:grid-cols-2 min-[1800px]:grid-cols-3">
        {sugg.isLoading && <div className="col-span-full text-muted">Loading…</div>}
        {!sugg.isLoading && cards.length === 0 && (
          <Empty title="All clear">
            Nothing new to review. New domains are checked every 30 seconds.{' '}
            <button className="underline-slide text-guard" onClick={() => api.pollNow().then(() => setTimeout(refresh, 1500))}>
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
