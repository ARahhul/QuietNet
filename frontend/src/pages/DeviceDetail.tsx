import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Baby, Radio, RefreshCw, Trash2 } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import DoneCheck from '../components/DoneCheck'
import { ViewButton } from '../components/DomainView'
import HomeAddress from '../components/HomeAddress'
import { nowText } from '../components/parental/summary'
import SetupSteps from '../components/SetupSteps'
import { CategoryChip, DeviceIcon, Eyebrow, RiskBar, Roll, timeAgo, useToast } from '../components/ui'

/** Plain DNS: recognised by the home's public IP, so it needs that address kept current. */
const PLAIN = ['ipv4_demo', 'ipv6']

/** What changing a device's address (ClientID) means for it: a plain-DNS device is known by the home's internet
 * address and the laptop relay updates itself, so only the others need the new address entered. */
function addressChange(name: string, method: string) {
  if (PLAIN.includes(method))
    return { ask: `Give ${name} a new address? It keeps working: QuietNet knows it by your home's internet address.`, done: 'New address ready. Nothing to change on the device.' }
  if (method === 'forwarder')
    return { ask: `Give ${name} a new address? The laptop relay switches to it by itself.`, done: 'New address ready. The laptop relay uses it now.' }
  return {
    ask: `Give ${name} a new address? It can't look anything up until you enter the new address on it.`,
    done: 'New address ready. Enter it on the device using the steps below.',
  }
}

export default function DeviceDetail() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const nav = useNavigate()
  const toast = useToast()
  const device = useQuery({ queryKey: ['device', id], queryFn: () => api.device(id), refetchInterval: 10000 })
  const activity = useQuery({ queryKey: ['activity', id], queryFn: () => api.activity(id), refetchInterval: 15000 })
  const setup = useQuery({ queryKey: ['setup', id], queryFn: () => api.setup(id) })
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['activity', id] })
    qc.invalidateQueries({ queryKey: ['suggestions'] })
  }
  const decide = useMutation({
    mutationFn: (v: { domain: string; action: 'block' | 'allow' }) => api.decide({ ...v, device_id: id, confirm_must_allow: true }),
    onSuccess: (r) => {
      refresh()
      toast({ text: `${r.decision.action === 'block' ? 'Blocked' : 'Allowed'} on this device`, action: { label: 'Undo', run: () => api.undo(r.decision.id).then(refresh) } })
    },
  })
  const undo = useMutation({ mutationFn: (decisionId: string) => api.undo(decisionId), onSuccess: refresh })
  const del = useMutation({
    mutationFn: () => api.deleteDevice(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['devices'] })
      nav('/devices')
    },
  })
  const readdress = useMutation({
    mutationFn: () => api.changeDeviceAddress(id),
    onSuccess: (r) => {
      qc.setQueryData(['setup', id], r.setup)
      qc.invalidateQueries({ queryKey: ['device', id] })
      qc.invalidateQueries({ queryKey: ['devices'] })
      toast({ text: addressChange(r.device.name, r.device.setup_method).done })
    },
    onError: (e) => toast({ text: (e as Error).message }),
  })

  const d = device.data
  return (
    <div>
      <Link to="/devices" className="group inline-flex items-center gap-1 text-sm text-muted transition hover:text-white">
        <ArrowLeft className="h-4 w-4 transition-transform motion-safe:group-hover:-translate-x-1" /> <span className="underline-slide">Devices</span>
      </Link>
      {d && (
        <div className="card mt-4 flex items-center gap-4 p-5">
          <div className="grid h-12 w-12 place-items-center rounded-md bg-panel-2 text-brand-soft">
            <DeviceIcon type={d.type} className="h-6 w-6" />
          </div>
          <div className="flex-1">
            <div className="font-display text-2xl leading-tight">{d.name}</div>
            <div className="font-mono text-xs text-muted">{d.client_id}</div>
            <div className="mt-1 text-xs text-muted">
              {d.online ? 'online' : `last seen ${timeAgo(d.last_seen)}`} · {d.stats.queries} queries · {d.stats.blocked_queries} blocked
            </div>
          </div>
          <RiskBar score={d.risk_score} />
        </div>
      )}

      {d && <ParentalCard deviceId={d.id} />}

      <Eyebrow className="mt-10">Who this device talks to</Eyebrow>
      <p className="mt-1 text-sm text-muted">Grouped by company. Blocks here apply to this device only.</p>
      <div className="mt-4 grid items-start gap-3 xl:grid-cols-2">
        {activity.data?.length === 0 && <div className="col-span-full text-sm text-muted">No traffic yet.</div>}
        {activity.data?.map((g) => (
          <div key={g.owner} className="card card-hover p-4">
            <div className="flex items-baseline justify-between">
              <div className="font-display text-lg">{g.owner}</div>
              <div className="text-xs text-muted">{g.count} queries</div>
            </div>
            <ul className="mt-2 divide-y divide-line">
              {g.domains.map((x) => (
                <li key={x.domain} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                  <span className="font-mono text-xs">{x.domain}</span>
                  <CategoryChip category={x.category} />
                  {x.beacon && <Radio className="h-3.5 w-3.5 text-warn" />}
                  <span className="ml-auto text-xs text-muted">{x.count}×</span>
                  {x.decision ? (
                    <button
                      className="btn-ghost px-2.5 py-1 text-xs"
                      disabled={x.decision.household}
                      title={x.decision.household ? 'Household rule. Undo it from Suggestions or Settings.' : ''}
                      onClick={() => undo.mutate(x.decision!.id)}
                    >
                      <Roll>{x.decision.action === 'allow' ? 'allowed' : x.decision.action === 'quarantine' ? 'held' : 'blocked'} · undo</Roll>
                    </button>
                  ) : x.blocklist ? (
                    <span className="chip bg-panel-2 text-muted" title="QuietNet's ad & tracker list blocks it for every home. View it to let it through here.">
                      QuietNet list
                    </span>
                  ) : x.category !== 'needed' ? (
                    <button className="btn-ghost px-2.5 py-1 text-xs" onClick={() => decide.mutate({ domain: x.domain, action: 'block' })}>
                      <Roll>block</Roll>
                    </button>
                  ) : null}
                  <ViewButton name={x.domain} deviceId={id} variant="icon" />
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      {setup.data && d && (
        <>
          <Eyebrow className="mt-10">Connection settings</Eyebrow>
          <div className="card mt-4 max-w-3xl p-5">
            <SetupSteps deviceId={d.id} guide={setup.data} brand={d.brand} />
          </div>
          <div className="mt-3 flex max-w-3xl flex-wrap items-center gap-x-3 gap-y-2 text-sm text-muted">
            <span>
              Address <span className="font-mono text-xs text-white">{d.client_id}</span>
            </span>
            <button
              className="btn-ghost px-2.5 py-1 text-xs"
              disabled={readdress.isPending}
              onClick={() => confirm(addressChange(d.name, d.setup_method).ask) && readdress.mutate()}
            >
              <Roll><RefreshCw className="h-3.5 w-3.5" /> Change address</Roll>
            </button>
            <span className="text-xs">Do this if someone else may know the address.</span>
          </div>
          {PLAIN.includes(d.setup_method) && (
            <div className="mt-4 grid max-w-3xl gap-4">
              <HomeAddress device={d} />
              <DoneCheck deviceId={d.id} />
            </div>
          )}
        </>
      )}

      <button className="btn-ghost mt-10 text-danger hover:border-danger/60" onClick={() => confirm('Remove this device from QuietNet?') && del.mutate()}>
        <Roll><Trash2 className="h-4 w-4" /> Remove device</Roll>
      </button>
    </div>
  )
}

/** Which parental rules this device follows and what they block right now; changed on the Parental page. */
function ParentalCard({ deviceId }: { deviceId: string }) {
  const overview = useQuery({ queryKey: ['parental'], queryFn: api.parental, refetchInterval: 30000 })
  const services = useQuery({ queryKey: ['parental-services'], queryFn: api.parentalServices, staleTime: 3600_000 })
  const o = overview.data
  const p = o?.devices.find((x) => x.id === deviceId)
  if (!o || !p) return null
  const byId = new Map((services.data ?? []).map((s) => [s.id, s]))
  const kid = o.kids.find((k) => k.id === p.profile_id)
  const follows = [!p.skip_household && 'the household’s rules', kid && `${kid.name}’s profile`, p.own && 'its own rules'].filter(Boolean)
  const active = p.now.adult || p.now.safe_search || p.now.apps.length > 0 || p.now.internet_off
  return (
    <div className="card mt-4 flex flex-wrap items-center gap-4 p-4">
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-panel-2 text-sentry">
        <Baby className="h-4 w-4" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">Parental controls</div>
        <div className={`mt-0.5 text-xs ${active ? 'text-guard' : 'text-muted'}`}>{nowText(p.now, byId)}</div>
        <div className="mt-0.5 text-xs text-muted">
          {follows.length ? `Follows ${follows.join(', ')}.` : 'A parent’s device: no parental rules.'}
        </div>
      </div>
      <Link to={`/parental#device-${deviceId}`} className="btn-ghost text-sm">
        <Roll>Change</Roll>
      </Link>
    </div>
  )
}
