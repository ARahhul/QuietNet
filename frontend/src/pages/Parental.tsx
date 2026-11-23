import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Baby, ChevronDown, Clock, Loader2, Moon, Plus, Router, X } from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  api,
  ApiError,
  type ParentalDevice,
  type ParentalOverview,
  type ParentalProfile,
  type ProfileChange,
} from '../api'
import ControlsEditor from '../components/parental/ControlsEditor'
import { describe, nowText, type ById } from '../components/parental/summary'
import { DeviceIcon, Empty, Eyebrow, Roll, Toggle, useToast } from '../components/ui'

const browserZone = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone
  } catch {
    return ''
  }
}

export default function Parental() {
  const qc = useQueryClient()
  const toast = useToast()
  const location = useLocation()
  const overview = useQuery({ queryKey: ['parental'], queryFn: api.parental, refetchInterval: 30000 })
  const services = useQuery({ queryKey: ['parental-services'], queryFn: api.parentalServices, staleTime: 3600_000 })
  const byId = useMemo(() => new Map((services.data ?? []).map((s) => [s.id, s])), [services.data])

  const took = (o: ParentalOverview & { synced?: boolean }) => {
    qc.setQueryData(['parental'], o)
    if (o.synced === false) toast({ text: 'Saved, but the DNS server can’t be reached right now. It’ll get the change as soon as it’s back.' })
  }
  const failed = (e: unknown) => toast({ text: e instanceof ApiError ? e.message : 'That didn’t save. Try again.' })

  const saveProfile = useMutation({
    mutationFn: ({ id, change }: { id: string; change: ProfileChange }) => api.patchParentalProfile(id, change),
    onSuccess: took,
    onError: (e) => {
      failed(e)
      qc.invalidateQueries({ queryKey: ['parental'] })
    },
  })
  const saveDevice = useMutation({
    mutationFn: ({ id, change }: { id: string; change: Parameters<typeof api.patchParentalDevice>[1] }) =>
      api.patchParentalDevice(id, change),
    onSuccess: took,
    onError: failed,
  })

  // /parental#device-<id> (from a device's page): bring that device into view
  useEffect(() => {
    if (overview.data && location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView({ block: 'start' })
  }, [overview.data, location.hash])

  if (overview.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading parental controls…
      </div>
    )
  }
  if (!overview.data) return <Empty title="Parental controls didn’t load">Check your connection and reload the page.</Empty>
  const o = overview.data
  const svc = services.data ?? []
  const editor = (p: ParentalProfile) => (
    <ControlsEditor
      profile={p}
      services={svc}
      essentials={o.essentials}
      saving={saveProfile.isPending && saveProfile.variables?.id === p.id}
      onSave={(change) => saveProfile.mutate({ id: p.id, change })}
    />
  )

  return (
    <div className="max-w-5xl">
      <h1 className="page-title">
        Parental <span className="text-guard">controls</span>
      </h1>
      <p className="mt-2 max-w-2xl text-sm text-muted">
        Block adult sites, apps and games, and set bedtimes. Rules add up: each device gets the household’s rules, its kid
        profile’s and its own. One can only block more, never undo another.
      </p>
      <TimeZone zone={o.time_zone} />

      <Eyebrow className="mt-10">Everyone at home</Eyebrow>
      <div className="card mt-4 p-5">
        <p className="mb-5 text-xs leading-relaxed text-muted">
          Applies to every device except the ones marked as a parent’s device below.
          {o.devices.some((d) => d.type === 'router') &&
            ' Your home WiFi router passes these rules to everything connected to it.'}
        </p>
        {editor(o.household)}
      </div>

      <Eyebrow className="mt-10">Kid profiles</Eyebrow>
      <p className="mt-2 max-w-2xl text-sm text-muted">One per child, with their own rules. Put their phone, tablet or laptop in it.</p>
      <div className="mt-4 grid gap-4">
        {o.kids.map((k) => (
          <KidCard key={k.id} kid={k} overview={o} byId={byId} onSaved={took} onFailed={failed}>
            {editor(k)}
          </KidCard>
        ))}
        <AddKid onAdded={took} onFailed={failed} first={!o.kids.length} />
      </div>

      <Eyebrow className="mt-10">Devices</Eyebrow>
      <div className="mt-4 grid gap-3">
        {!o.devices.length && (
          <Empty title="No devices yet">
            <Link to="/setup" className="underline-slide text-guard">
              Connect a device
            </Link>{' '}
            to give it parental controls.
          </Empty>
        )}
        {o.devices.map((d) => (
          <DeviceRow
            key={d.id}
            device={d}
            overview={o}
            byId={byId}
            busy={saveDevice.isPending && saveDevice.variables?.id === d.id}
            change={(change) => saveDevice.mutate({ id: d.id, change })}
          >
            {d.own && editor(d.own)}
          </DeviceRow>
        ))}
      </div>
    </div>
  )
}

/** "GMT+5:30": two names for the same clock (Asia/Calcutta, Asia/Kolkata) count as the same */
const offset = (zone: string) => {
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'longOffset' })
      .formatToParts(new Date())
      .find((p) => p.type === 'timeZoneName')?.value
  } catch {
    return undefined
  }
}

function TimeZone({ zone }: { zone: string }) {
  const qc = useQueryClient()
  const browser = browserZone()
  const here = browser && offset(browser) !== offset(zone) ? browser : ''
  const use = useMutation({
    mutationFn: () => api.patchSettings({ time_zone: here }),
    onSuccess: (s) => {
      qc.setQueryData(['settings'], s)
      qc.invalidateQueries({ queryKey: ['parental'] })
    },
  })
  return (
    <p className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
      <Clock className="h-3.5 w-3.5" /> Bedtimes follow {zone.replace('_', ' ')} time.
      {here && (
        <button className="text-guard" disabled={use.isPending} onClick={() => use.mutate()}>
          <span className="underline-slide">Use {here.replace('_', ' ')} (this device’s)</span>
        </button>
      )}
    </p>
  )
}

function KidCard({
  kid,
  overview,
  byId,
  onSaved,
  onFailed,
  children,
}: {
  kid: ParentalOverview['kids'][number]
  overview: ParentalOverview
  byId: ById
  onSaved: (o: ParentalOverview) => void
  onFailed: (e: unknown) => void
  children: ReactNode
}) {
  // a new profile opens ready to fill in; one with rules shows its summary
  const [open, setOpen] = useState(() => describe(kid, byId) === 'Nothing blocked yet')
  const [sure, setSure] = useState(false)
  const del = useMutation({ mutationFn: () => api.deleteKidProfile(kid.id), onSuccess: onSaved, onError: onFailed })
  const devices = overview.devices.filter((d) => kid.devices.includes(d.id))
  return (
    <div className="card p-5">
      <div className="flex flex-wrap items-start gap-3">
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-panel-2 text-sentry">
          <Baby className="h-5 w-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="font-display text-xl">{kid.name}</div>
          <div className="mt-0.5 text-xs text-muted">
            {devices.length ? devices.map((d) => d.name).join(', ') : 'No devices yet: pick this profile for a device below.'}
          </div>
          {!open && <div className="mt-2 text-sm text-body">{describe(kid, byId)}</div>}
        </div>
        <button className="btn-ghost text-sm" onClick={() => setOpen(!open)} aria-expanded={open}>
          <Roll>
            {open ? 'Done' : 'Edit rules'} <ChevronDown className={`h-4 w-4 transition ${open ? 'rotate-180' : ''}`} />
          </Roll>
        </button>
      </div>
      {open && (
        <div className="mt-5 border-t border-line pt-5">
          {children}
          <div className="mt-4 flex justify-end">
            {sure ? (
              <span className="flex items-center gap-3 text-xs">
                <span className="text-muted">Delete {kid.name}? Their devices keep only the household’s rules.</span>
                <button className="btn-danger px-3 py-1.5 text-xs" disabled={del.isPending} onClick={() => del.mutate()}>
                  <Roll>Delete</Roll>
                </button>
                <button className="text-muted hover:text-white" onClick={() => setSure(false)}>
                  Keep
                </button>
              </span>
            ) : (
              <button className="text-xs text-muted hover:text-danger" onClick={() => setSure(true)}>
                Delete this profile
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function AddKid({ onAdded, onFailed, first }: { onAdded: (o: ParentalOverview) => void; onFailed: (e: unknown) => void; first: boolean }) {
  const [name, setName] = useState('')
  const add = useMutation({
    mutationFn: () => api.createKidProfile(name.trim()),
    onSuccess: (o) => {
      setName('')
      onAdded(o)
    },
    onError: onFailed,
  })
  return (
    <form
      className={`flex flex-wrap items-center gap-3 ${first ? 'card p-5' : ''}`}
      onSubmit={(e) => {
        e.preventDefault()
        if (name.trim()) add.mutate()
      }}
    >
      <input
        className="input max-w-xs flex-1"
        placeholder="Child’s name, e.g. Aarav"
        maxLength={40}
        value={name}
        onChange={(e) => setName(e.target.value)}
        aria-label="Child’s name"
      />
      <button className="btn-light" disabled={!name.trim() || add.isPending}>
        <Roll>
          <Plus className="h-4 w-4" /> Add a kid profile
        </Roll>
      </button>
    </form>
  )
}

function DeviceRow({
  device: d,
  overview,
  byId,
  busy,
  change,
  children,
}: {
  device: ParentalDevice
  overview: ParentalOverview
  byId: ById
  busy: boolean
  change: (c: { profile_id?: string | null; skip_household?: boolean; own_rules?: boolean }) => void
  children: ReactNode
}) {
  const active = d.now.adult || d.now.safe_search || d.now.apps.length > 0 || d.now.internet_off
  const kid = overview.kids.find((k) => k.id === d.profile_id)
  return (
    <div id={`device-${d.id}`} className="card scroll-mt-6 p-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-md bg-panel-2 text-brand-soft">
          <DeviceIcon type={d.type} className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium">{d.name}</div>
          <div className={`mt-0.5 text-xs ${active ? 'text-guard' : 'text-muted'}`}>
            {d.now.bedtime && <Moon className="mr-1 inline h-3 w-3 align-[-1px]" />}
            {nowText(d.now, byId)}
          </div>
        </div>
        {busy && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
        <label className="flex items-center gap-2 text-xs text-muted">
          Kid profile
          <select
            className="input w-auto py-1.5 text-sm"
            value={d.profile_id ?? ''}
            onChange={(e) => change({ profile_id: e.target.value || null })}
          >
            <option value="">None</option>
            {overview.kids.map((k) => (
              <option key={k.id} value={k.id}>
                {k.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-3 border-t border-line pt-3 text-xs">
        <label className="flex items-center gap-2.5 text-muted">
          <Toggle
            checked={d.skip_household}
            onChange={(skip_household) => change({ skip_household })}
            label={`${d.name} is a parent’s device`}
          />
          A parent’s device (skip household rules)
        </label>
        {!d.own ? (
          <button className="flex items-center gap-1 text-guard" onClick={() => change({ own_rules: true })}>
            <Plus className="h-3.5 w-3.5" /> <span className="underline-slide">Rules just for this device</span>
          </button>
        ) : (
          <button className="flex items-center gap-1 text-muted hover:text-white" onClick={() => change({ own_rules: false })}>
            <X className="h-3.5 w-3.5" /> Remove its own rules
          </button>
        )}
        {d.type === 'router' && (
          <span className="flex items-center gap-1 text-muted">
            <Router className="h-3.5 w-3.5" /> Every device behind this router gets its rules.
          </span>
        )}
      </div>
      {kid && !d.own && <div className="mt-2 text-xs text-muted">Follows {kid.name}’s rules{d.skip_household ? '' : ' and the household’s'}.</div>}
      {d.own && (
        <div className="mt-4 rounded-md border border-line p-4">
          <div className="mb-4 text-xs text-muted">
            Just for {d.name}, on top of {[!d.skip_household && 'the household’s rules', kid && `${kid.name}’s`].filter(Boolean).join(' and ') || 'nothing else'}.
          </div>
          {children}
        </div>
      )}
    </div>
  )
}

