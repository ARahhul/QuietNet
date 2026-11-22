import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, CheckCircle2, Loader2, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Device, type DeviceType, type SetupGuide } from '../api'
import AssistantPanel from '../components/assistant/AssistantPanel'
import RouterWizard, { type RouterPlan } from '../components/RouterWizard'
import ScreenshotHelp from '../components/ScreenshotHelp'
import SetupSteps from '../components/SetupSteps'
import { DeviceIcon, Roll } from '../components/ui'

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
  { key: 'router', label: 'Home WiFi (covers everything)', hint: 'Finds your provider: Jio, Airtel, ACT or any other', type: 'router', brand: null, method: 'ipv4_demo' },
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
    mutationFn: async (plan?: RouterPlan) => {
      const r = await api.createDevice({
        name: plan?.name ?? (name.trim() || choice!.label),
        type: choice!.type,
        brand: plan?.brand ?? tvBrand?.brand ?? choice!.brand,
        setup_method: plan?.method ?? tvBrand?.method ?? choice!.method,
      })
      if (!plan?.linkedIp) return r
      // Plain DNS on the router: QuietNet tells this home apart by its public IP.
      const { device } = await api.patchDevice(r.device.id, { linked_ip: plan.linkedIp, allow_shared_ipv4: true })
      return { ...r, device, setup: await api.setup(device.id) }
    },
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
  const aiReady = !!health.data?.llm.enabled

  return (
    <div className="max-w-5xl">
      <Link to="/devices" className="group inline-flex items-center gap-1 text-sm text-muted transition hover:text-white">
        <ArrowLeft className="h-4 w-4 transition-transform motion-safe:group-hover:-translate-x-1" /> <span className="underline-slide">Devices</span>
      </Link>
      <h1 className="page-title mt-4">
        Connect a <span className="text-guard">device</span>
      </h1>

      {!created && (
        <>
          <p className="mt-2 text-sm text-muted">Pick what you want to protect. Each device gets its own private address.</p>
          <div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {CHOICES.map((c) => (
              <button
                key={c.key}
                onClick={() => {
                  setChoice(c)
                  setTvBrand(null)
                }}
                className={`card group flex items-center gap-3 p-4 text-left ${choice?.key === c.key ? 'border-guard' : 'card-link'}`}
              >
                <DeviceIcon type={c.type} className="h-6 w-6 shrink-0 text-brand-soft transition-transform motion-safe:group-hover:scale-110" />
                <div>
                  <div className="font-medium">{c.label}</div>
                  <div className="text-xs text-muted">{c.hint}</div>
                </div>
              </button>
            ))}
          </div>

          {choice?.key === 'tv' && (
            <div className="card mt-4 max-w-3xl p-4">
              {assistantReady && (
                <div className="mb-5 rounded-md border border-line bg-ink p-4">
                  <div className="flex items-center gap-2 font-display text-lg">
                    <Sparkles className="h-4 w-4 text-guard" /> Let QuietNet find and set up your TV
                  </div>
                  <p className="mt-1 text-sm text-muted">
                    Describe it and QuietNet scans your WiFi, then sets it up automatically if it can, or gives exact steps if it can't.
                  </p>
                  <AssistantPanel onConnected={() => qc.invalidateQueries({ queryKey: ['devices'] })} />
                </div>
              )}
              <div className="text-sm font-medium">Or set it up manually. Which TV?</div>
              <div className="mt-2 grid grid-cols-2 gap-2">
                {TV_BRANDS.map((b) => (
                  <button
                    key={b.brand}
                    onClick={() => setTvBrand(b)}
                    className={`rounded-full border px-3 py-2 text-sm transition ${tvBrand?.brand === b.brand ? 'border-white bg-white text-ink' : 'border-line hover:border-line-strong'}`}
                  >
                    {b.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {choice?.key === 'router' && (
            <RouterWizard
              onCreate={(plan) => create.mutate(plan)}
              onPerDevice={() => {
                setChoice(null)
                window.scrollTo({ top: 0, behavior: 'smooth' })
              }}
              busy={create.isPending}
              error={create.isError ? (create.error as Error).message : null}
            />
          )}

          {choice && choice.key !== 'router' && (choice.key !== 'tv' || tvBrand) && (
            <div className="card mt-4 grid max-w-3xl gap-3 p-4">
              <label className="text-sm font-medium">
                Name it
                <input
                  className="input mt-1"
                  placeholder={tvBrand ? `Living room ${tvBrand.brand} TV` : `My ${choice.label}`}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <button className="btn-primary" disabled={create.isPending} onClick={() => create.mutate(undefined)}>
                <Roll>{create.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Get my setup steps</Roll>
              </button>
              {create.isError && <div className="text-sm text-danger">{(create.error as Error).message}</div>}
            </div>
          )}
        </>
      )}

      {created && (
        <div className="mt-5 grid max-w-3xl gap-4">
          <div className={`card flex items-center gap-3 p-4 ${connected ? 'border-guard/60' : ''}`}>
            {connected ? (
              <CheckCircle2 className="h-6 w-6 text-guard" />
            ) : (
              <Loader2 className="h-6 w-6 animate-spin text-muted" />
            )}
            <div>
              <div className="font-medium">{connected ? `${created.device.name} is protected` : 'Waiting for the first DNS query…'}</div>
              <div className="text-xs text-muted">
                {connected ? 'Its traffic now shows up in Suggestions.' : 'Follow the steps below; this turns green automatically.'}
              </div>
            </div>
            {connected && (
              <Link to="/" className="btn-primary ml-auto">
                <Roll>See what it's doing</Roll>
              </Link>
            )}
          </div>
          <div className="card p-5">
            <SetupSteps deviceId={created.device.id} guide={created.setup} brand={created.device.brand} type={created.device.type} />
          </div>
          {aiReady && <ScreenshotHelp deviceId={created.device.id} />}
          {created.setup.method === 'ipv4_demo' && !created.device.linked_ip ? <CgnatCheck /> : null}
          <button
            className="btn-ghost w-fit"
            onClick={() => {
              setCreated(null)
              setChoice(null)
              setTvBrand(null)
              setName('')
            }}
          >
            <Roll>Add another device</Roll>
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
      <div className="font-display text-lg">Is your internet behind CGNAT?</div>
      <p className="mt-1 text-muted">
        JioFiber, Airtel and ACT often share one public IP between many homes. Type the WAN IP from your router's status page.
      </p>
      <div className="mt-3 flex gap-2">
        <input className="input" placeholder="e.g. 100.72.14.9" value={wan} onChange={(e) => setWan(e.target.value)} />
        <button className="btn-ghost" disabled={!wan || check.isPending} onClick={() => check.mutate()}>
          <Roll>Check</Roll>
        </button>
      </div>
      {check.data && (
        <div className={`mt-3 rounded-md p-3 ${check.data.cgnat ? 'bg-warn/10 text-warn' : 'bg-guard/10 text-guard'}`}>
          {check.data.advice}
          {check.data.public_ip && <div className="mt-1 text-xs text-muted">Your public IP: {check.data.public_ip}</div>}
        </div>
      )}
    </div>
  )
}
