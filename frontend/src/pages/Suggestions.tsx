import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Zap } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '../api'
import AdHunt from '../components/AdHunt'
import SuggestionCard from '../components/SuggestionCard'
import { DeviceIcon, Empty, FilterChip, Roll, useToast } from '../components/ui'
import Setup from './Setup'

export default function Suggestions() {
  const qc = useQueryClient()
  const toast = useToast()
  const [deviceId, setDeviceId] = useState<string | undefined>()
  const [later, setLater] = useState<Set<string>>(new Set())
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices, refetchInterval: 10000 })
  // Until one of the home's devices has sent a lookup there is nothing to suggest or hunt ads in, so the home page
  // leads with the steps to add one. Decided once the device list arrives and kept while the person follows the
  // steps, so the page doesn't swap under them the moment the new device connects.
  const [steps, setSteps] = useState<boolean | null>(null)
  if (steps === null && devices.data) setSteps(!devices.data.some((d) => d.last_seen))
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

  if (steps === null && !devices.isError) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading your home…
      </div>
    )
  }
  if (steps) {
    return (
      <Setup
        first
        onDone={() => {
          setSteps(false)
          refresh()
        }}
      />
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

      <div className="mt-5">
        <AdHunt deviceId={deviceId} deviceName={devices.data?.find((d) => d.id === deviceId)?.name} />
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
