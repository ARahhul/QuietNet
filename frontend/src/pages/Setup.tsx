import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, CheckCircle2, Loader2, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Device, type DeviceType, type SetupGuide } from '../api'
import AssistantPanel from '../components/assistant/AssistantPanel'
import SetupSteps from '../components/SetupSteps'
import { DeviceIcon } from '../components/ui'

interface Choice {
  key: string
  label: string
  hint: string
  type: DeviceType
  brand: string | null
  method?: string
}

const CHOICES: Choice[] = [
  { key: 'android', label: 'Android phone', hint: 'Private DNS · 30 seconds', type: 'phone', brand: 'Android', method: 'dot' },
  { key: 'iphone', label: 'iPhone / iPad', hint: 'Install a profile', type: 'phone', brand: 'Apple', method: 'doh' },
  { key: 'windows', label: 'Windows laptop', hint: 'Encrypted DNS (DoH)', type: 'laptop', brand: 'Windows', method: 'doh' },
  { key: 'mac', label: 'Mac', hint: 'Install a profile', type: 'laptop', brand: 'Apple', method: 'doh' },
  { key: 'tv', label: 'Smart TV / streaming box', hint: 'LG, Samsung, Android TV, Fire TV', type: 'tv', brand: null },
  { key: 'router', label: 'Home router (covers everything)', hint: 'Plain DNS + linked home IP', type: 'router', brand: null, method: 'ipv4_demo' },
]

const TV_BRANDS = [
  { brand: 'LG', label: 'LG (webOS)', method: 'ipv4_demo' },
  { brand: 'Samsung', label: 'Samsung (Tizen)', method: 'ipv4_demo' },
  { brand: 'Google', label: 'Android / Google TV', method: 'adb' },
  { brand: 'Amazon', label: 'Fire TV Stick', method: 'adb' },
]

export default function Setup() {
  const qc = useQueryClient()
  const [choice, setChoice] = useState<Choice | null>(null)
  const [tvBrand, setTvBrand] = useState<(typeof TV_BRANDS)[number] | null>(null)
  const [name, setName] = useState('')
  const [created, setCreated] = useState<{ device: Device; setup: SetupGuide } | null>(null)

  const create = useMutation({
    mutationFn: () =>
      api.createDevice({
        name: name.trim() || choice!.label,
        type: choice!.type,
        brand: tvBrand?.brand ?? choice!.brand,
        setup_method: tvBrand?.method ?? choice!.method,
      }),
    onSuccess: (r) => {
      setCreated(r)
      qc.invalidateQueries({ queryKey: ['devices'] })
    },
  })

  const live = useQuery({
    queryKey: ['device', created?.device.id],
    queryFn: () => api.device(created!.device.id),
    enabled: !!created,
    refetchInterval: (q) => (q.state.data?.last_seen ? false : 3000),
  })
  const connected = !!live.data?.last_seen

  const health = useQuery({ queryKey: ['health'], queryFn: api.health })
  const assistantReady = !!health.data?.companion.connected

  return (
    <div className="mx-auto max-w-2xl">
      <Link to="/devices" className="inline-flex items-center gap-1 text-sm text-muted hover:text-slate-100">
        <ArrowLeft className="h-4 w-4" /> Devices
      </Link>
      <h1 className="mt-3 text-2xl font-bold">Connect a device</h1>

      {!created && (
        <>
          <p className="mt-1 text-sm text-muted">Pick what you want to protect. Each device gets its own private address.</p>
          <div className="mt-5 grid gap-3 sm:grid-cols-2">
            {CHOICES.map((c) => (
              <button
                key={c.key}
                onClick={() => {
                  setChoice(c)
                  setTvBrand(null)
                }}
                className={`card flex items-center gap-3 p-4 text-left hover:border-muted ${choice?.key === c.key ? 'border-accent' : ''}`}
              >
                <DeviceIcon type={c.type} className="h-6 w-6 text-accent" />
                <div>
                  <div className="font-semibold">{c.label}</div>
                  <div className="text-xs text-muted">{c.hint}</div>
                </div>
              </button>
            ))}
          </div>

          {choice?.key === 'tv' && (
            <div className="card mt-4 p-4">
              {assistantReady && (
                <div className="mb-4 rounded-xl border border-accent/40 bg-accent/10 p-4">
                  <div className="flex items-center gap-2 font-semibold">
                    <Sparkles className="h-4 w-4 text-accent" /> Let Pahra find and set up your TV
                  </div>
                  <p className="mt-1 text-sm text-muted">
                    Describe it and Pahra scans your WiFi, then sets it up automatically if it can, or gives exact steps if it can't.
                  </p>
                  <AssistantPanel onConnected={() => qc.invalidateQueries({ queryKey: ['devices'] })} />
                </div>
              )}
              <div className="text-sm font-semibold">Or set it up manually. Which TV?</div>
              <div className="mt-2 grid grid-cols-2 gap-2">
                {TV_BRANDS.map((b) => (
                  <button
                    key={b.brand}
                    onClick={() => setTvBrand(b)}
                    className={`rounded-xl border px-3 py-2 text-sm ${tvBrand?.brand === b.brand ? 'border-accent bg-accent/10' : 'border-line'}`}
                  >
                    {b.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {choice && (choice.key !== 'tv' || tvBrand) && (
            <div className="card mt-4 grid gap-3 p-4">
              <label className="text-sm">
                Name it
                <input
                  className="input mt-1"
                  placeholder={tvBrand ? `Living room ${tvBrand.brand} TV` : `My ${choice.label}`}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <button className="btn-primary" disabled={create.isPending} onClick={() => create.mutate()}>
                {create.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Get my setup steps
              </button>
              {create.isError && <div className="text-sm text-danger">{(create.error as Error).message}</div>}
            </div>
          )}
        </>
      )}

      {created && (
        <div className="mt-5 grid gap-4">
          <div className={`card flex items-center gap-3 p-4 ${connected ? 'border-guard/60' : ''}`}>
            {connected ? (
              <CheckCircle2 className="h-6 w-6 text-guard" />
            ) : (
              <Loader2 className="h-6 w-6 animate-spin text-muted" />
            )}
            <div>
              <div className="font-semibold">{connected ? `${created.device.name} is protected` : 'Waiting for the first DNS query…'}</div>
              <div className="text-xs text-muted">
                {connected ? 'Its traffic now shows up in Suggestions.' : 'Follow the steps below; this turns green automatically.'}
              </div>
            </div>
            {connected && (
              <Link to="/" className="btn-primary ml-auto">
                See what it's doing
              </Link>
            )}
          </div>
          <div className="card p-5">
            <SetupSteps deviceId={created.device.id} guide={created.setup} brand={created.device.brand} />
          </div>
          {created.device.type === 'router' || created.setup.method === 'ipv4_demo' ? <CgnatCheck /> : null}
          <button
            className="btn-ghost w-fit"
            onClick={() => {
              setCreated(null)
              setChoice(null)
              setTvBrand(null)
              setName('')
            }}
          >
            Add another device
          </button>
        </div>
      )}
    </div>
  )
}

function CgnatCheck() {
  const [wan, setWan] = useState('')
  const check = useMutation({ mutationFn: () => api.cgnat(wan.trim()) })
  return (
    <div className="card p-4 text-sm">
      <div className="font-semibold">Is your internet behind CGNAT?</div>
      <p className="mt-1 text-muted">
        JioFiber, Airtel and ACT often share one public IP between many homes. Type the WAN IP from your router's status page.
      </p>
      <div className="mt-3 flex gap-2">
        <input className="input" placeholder="e.g. 100.72.14.9" value={wan} onChange={(e) => setWan(e.target.value)} />
        <button className="btn-ghost" disabled={!wan || check.isPending} onClick={() => check.mutate()}>
          Check
        </button>
      </div>
      {check.data && (
        <div className={`mt-3 rounded-xl p-3 ${check.data.cgnat ? 'bg-warn/10 text-warn' : 'bg-guard/10 text-guard'}`}>
          {check.data.advice}
          {check.data.public_ip && <div className="mt-1 text-xs text-muted">Your public IP: {check.data.public_ip}</div>}
        </div>
      )}
    </div>
  )
}
