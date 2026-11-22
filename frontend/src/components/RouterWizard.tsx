import { useMutation, useQuery } from '@tanstack/react-query'
import { CheckCircle2, ExternalLink, Loader2, ShieldCheck, Sparkles, TriangleAlert, Wifi } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { api, type GuideSource, type IspGuide, type Odds, type ProviderDetection, type RouterGuide } from '../api'
import ScreenshotHelp from './ScreenshotHelp'
import { CopyField, Roll } from './ui'

export interface RouterPlan {
  name: string
  brand: string
  method: 'dot' | 'ipv4_demo'
  /** The home's public IP, for plain DNS (QuietNet recognises the router by it) */
  linkedIp?: string
}

type Kind = 'isp' | 'own' | 'unsure'

/**
 * Whole-home setup: which provider, which box, how to log in to it, and whether QuietNet can work there.
 * Ends in a router device (encrypted DNS, or plain DNS + the home's public IP), or sends the user to per-device setup.
 */
export default function RouterWizard({
  onCreate,
  onPerDevice,
  busy,
  error,
}: {
  onCreate: (p: RouterPlan) => void
  onPerDevice: () => void
  busy: boolean
  error: string | null
}) {
  const catalog = useQuery({ queryKey: ['providers'], queryFn: api.providers, staleTime: Infinity })
  const detected = useQuery({ queryKey: ['provider-detect'], queryFn: api.detectProvider, staleTime: 10 * 60_000, retry: false })
  const health = useQuery({ queryKey: ['health'], queryFn: api.health })
  const aiReady = !!health.data?.llm.enabled

  const [picked, setPicked] = useState<IspGuide | null>(null)
  const [touched, setTouched] = useState(false)
  // Until the user picks, go with the provider this connection comes from.
  const isp = touched ? picked : (detected.data?.isp ?? null)
  const [typingIsp, setTypingIsp] = useState(false)
  const [kind, setKind] = useState<Kind | null>(null)
  const [own, setOwn] = useState<RouterGuide | null>(null)
  const [typingRouter, setTypingRouter] = useState(false)

  const pickIsp = (g: IspGuide) => {
    setPicked(g)
    setTouched(true)
    setKind(null)
    setOwn(null)
    if (g.source === 'curated') setTypingIsp(false)
  }
  const ispGuide = useMutation({ mutationFn: api.ispGuide, onSuccess: pickIsp })
  const routerGuide = useMutation({
    mutationFn: api.routerGuide,
    onSuccess: (g) => {
      setOwn(g)
      if (g.source === 'curated') setTypingRouter(false)
    },
  })

  return (
    <div className="mt-4 grid max-w-3xl gap-4">
      <Step n={1} title="Who gives you internet?">
        <Detected d={detected.data} loading={detected.isPending} picked={isp} busy={ispGuide.isPending} onUse={(n) => ispGuide.mutate(n)} />
        {catalog.isPending && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
        {catalog.isError && <div className="text-sm text-danger">Couldn't load the provider list: {(catalog.error as Error).message}</div>}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {catalog.data?.isps.map((p) => (
            <Chip key={p.key} active={isp?.key === p.key} onClick={() => pickIsp(p)}>
              {p.name}
            </Chip>
          ))}
          {catalog.data && (
            <Chip active={typingIsp || (!!isp && isp.source !== 'curated')} onClick={() => setTypingIsp(true)}>
              Other…
            </Chip>
          )}
        </div>
        {typingIsp && (
          <NameForm
            label="Your provider's name"
            placeholder="e.g. Asianet, Spectra, your local cable operator"
            busy={ispGuide.isPending}
            onSubmit={(n) => ispGuide.mutate(n)}
          />
        )}
        {ispGuide.isError && <div className="mt-2 text-sm text-danger">{(ispGuide.error as Error).message}</div>}
      </Step>

      {isp && (
        <Step n={2} title="What's at home?">
          <div className="grid gap-2 sm:grid-cols-3">
            <Option active={kind === 'isp'} onClick={() => setKind('isp')} title={`Only ${box(isp)}`}>
              Your WiFi comes from the box {short(isp)} installed.
            </Option>
            <Option active={kind === 'own'} onClick={() => setKind('own')} title="Their box + my own router">
              A second WiFi router is plugged into their box with a cable.
            </Option>
            <Option active={kind === 'unsure'} onClick={() => setKind('unsure')} title="Not sure">
              We'll help you check.
            </Option>
          </div>
          {kind === 'unsure' && (
            <div className="mt-3 rounded-md border border-line bg-ink p-4 text-sm text-muted">
              Follow the internet cable from the wall: it goes into {short(isp)}'s box. If another box with antennas is plugged into
              that one with a short network cable, and your WiFi name comes from it, you have your own router. If there's only
              one box, it's {short(isp)}'s.
              <div className="mt-3 flex flex-wrap gap-2">
                <button className="btn-ghost" onClick={() => setKind('isp')}>
                  <Roll>Only one box</Roll>
                </button>
                <button className="btn-ghost" onClick={() => setKind('own')}>
                  <Roll>There's a second router</Roll>
                </button>
              </div>
            </div>
          )}
        </Step>
      )}

      {isp && kind === 'isp' && <ProviderBox isp={isp} onCreate={onCreate} onOwnRouter={() => setKind('own')} onPerDevice={onPerDevice} busy={busy} />}

      {isp && kind === 'own' && (
        <Step n={3} title="Which router did you plug in?">
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
            {catalog.data?.routers.map((r) => (
              <Chip
                key={r.key}
                active={own?.key === r.key}
                onClick={() => {
                  setOwn(r)
                  setTypingRouter(false)
                }}
              >
                {r.name}
              </Chip>
            ))}
            <Chip active={typingRouter || (!!own && own.source !== 'curated')} onClick={() => setTypingRouter(true)}>
              Other…
            </Chip>
          </div>
          {typingRouter && (
            <NameForm
              label="Router brand or model"
              placeholder="e.g. Linksys E5600, Huawei AX3"
              busy={routerGuide.isPending}
              onSubmit={(n) => routerGuide.mutate(n)}
            />
          )}
          {routerGuide.isError && <div className="mt-2 text-sm text-danger">{(routerGuide.error as Error).message}</div>}
        </Step>
      )}
      {isp && kind === 'own' && own && <OwnRouterGuide isp={isp} router={own} onCreate={onCreate} onPerDevice={onPerDevice} busy={busy} />}

      {aiReady && isp && (kind === 'isp' || (kind === 'own' && own)) && (
        <ScreenshotHelp
          isp={isp.name}
          router={kind === 'own' ? own?.name : undefined}
          title="Stuck getting into the router? Show QuietNet your screen"
          intro="Paste a screenshot of the login page, the settings page you're on, or a photo of the sticker. The AI reads it and tells you what to do next."
        />
      )}

      {error && <div className="text-sm text-danger">{error}</div>}
    </div>
  )
}

// ---------------------------------------------------------------- the provider's own box

function ProviderBox({
  isp,
  onCreate,
  onOwnRouter,
  onPerDevice,
  busy,
}: {
  isp: IspGuide
  onCreate: (p: RouterPlan) => void
  onOwnRouter: () => void
  onPerDevice: () => void
  busy: boolean
}) {
  const [check, setCheck] = useState<HomeIp | null>(null)
  return (
    <>
      <Step n={3} title={`Open ${box(isp)}'s admin page`}>
        <SourceNote source={isp.source} name={isp.short} />
        <p className="text-sm text-muted">On a phone or laptop connected to your home WiFi (not mobile data), open:</p>
        <AdminLinks urls={isp.admin_urls} />
        <p className="mt-2 text-xs text-muted">The exact address is also printed on the box's sticker.</p>
      </Step>

      <Step n={4} title="Log in">
        <p className="text-sm">
          Turn the box over. The sticker on the back or bottom usually shows the admin username and password, labelled
          <em> Admin</em>, <em>Web login</em> or <em>User</em>. The WiFi password on the same sticker is a different one.
        </p>
        {isp.sticker_note && <p className="mt-2 text-sm text-muted">{isp.sticker_note}</p>}
        {isp.logins.length > 0 ? (
          <>
            <p className="mt-4 text-sm text-muted">
              Nothing on the sticker?{' '}
              {isp.source === 'curated'
                ? `These are commonly reported for ${isp.short} boxes.`
                : isp.source === 'ai'
                  ? `These are AI-suggested for ${isp.short} boxes.`
                  : 'These are common defaults on many boxes.'}{' '}
              They don't work on every model.
            </p>
            <div className="mt-2 grid gap-2">
              {isp.logins.map((l) => (
                <div key={l.user + l.pass} className="grid items-end gap-2 sm:grid-cols-[1fr_1fr_auto]">
                  <CopyField label="Username" value={l.user} />
                  <CopyField label="Password" value={l.pass} />
                  <span className="pb-2.5 text-xs text-muted sm:w-36">{l.on ?? ''}</span>
                </div>
              ))}
            </div>
          </>
        ) : (
          <p className="mt-3 text-sm text-muted">{short(isp)} doesn't use one common default login. Use the sticker, or ask them.</p>
        )}
        <Note tone="warn">
          Don't press the reset button on {short(isp)}'s box. It can erase the internet settings they set up, and you'd be offline
          until they fix it.
        </Note>
        <div className="mt-4 rounded-md border border-line bg-ink p-4 text-sm">
          <div className="font-medium">They didn't give you the login?</div>
          <ul className="mt-2 grid list-disc gap-1.5 pl-5 text-muted">
            {isp.ask_for.map((a) => (
              <li key={a}>{a}</li>
            ))}
            <li>You can reach them through {isp.help_from}.</li>
          </ul>
        </div>
      </Step>

      <Step n={5} title="Will it work on this box?">
        <div className="grid gap-2 text-sm sm:grid-cols-2">
          <Fact label="DNS settings" value={odds(isp.dns_locked, 'locked')} detail={isp.dns_note} />
          <Fact
            label="Shared internet address (CGNAT)"
            value={odds(isp.cgnat, 'shared')}
            detail="QuietNet recognises a router by your home's public address. If other homes share it, plain DNS on the box can't work."
          />
        </div>
        <HomeIpCheck onResult={setCheck} />
        {check?.cgnat === false && (
          <Verdict ok>
            Your home has its own address ({check.public_ip}), so {box(isp)} can use QuietNet if its DNS isn't greyed out.
            <div className="mt-3">
              <button
                className="btn-primary"
                disabled={busy}
                onClick={() => onCreate({ name: 'Home WiFi', brand: isp.name, method: 'ipv4_demo', linkedIp: check.public_ip ?? undefined })}
              >
                <Roll>
                  {busy && <Loader2 className="h-4 w-4 animate-spin" />} Set up {box(isp)}
                </Roll>
              </button>
            </div>
          </Verdict>
        )}
        {check && check.cgnat !== false && (
          <Alternatives isp={isp} onOwnRouter={onOwnRouter} onPerDevice={onPerDevice} why={check.advice} />
        )}
        {!check && (
          <p className="mt-3 text-xs text-muted">
            DNS greyed out, or can't log in?{' '}
            <button className="underline underline-offset-2 hover:text-white" onClick={() => setCheck({ public_ip: null, cgnat: null, advice: '' })}>
              See your other options
            </button>
            .
          </p>
        )}
      </Step>
    </>
  )
}

// ---------------------------------------------------------------- your own router behind the provider's box

function OwnRouterGuide({
  isp,
  router,
  onCreate,
  onPerDevice,
  busy,
}: {
  isp: IspGuide
  router: RouterGuide
  onCreate: (p: RouterPlan) => void
  onPerDevice: () => void
  busy: boolean
}) {
  const [check, setCheck] = useState<HomeIp | null>(null)
  const dot = (label: string) => (
    <button className="btn-primary" disabled={busy} onClick={() => onCreate({ name: 'Home WiFi', brand: router.brand, method: 'dot' })}>
      <Roll>
        {busy && <Loader2 className="h-4 w-4 animate-spin" />} {label}
      </Roll>
    </button>
  )
  return (
    <>
      <Step n={4} title={`Log in to your ${router.name}`}>
        <SourceNote source={router.source} name={router.name} router />
        {router.admin_url && <AdminLinks urls={[router.admin_url]} />}
        <p className="mt-2 text-sm">{router.login}</p>
        <p className="mt-2 text-sm text-muted">
          Forgot it? This router is yours, not {short(isp)}'s. Holding its reset button for 10 seconds brings back the login on its
          label, but also erases its WiFi settings, so you'll set it up again.
        </p>
      </Step>

      <Step n={5} title="Set it up">
        {router.dns_menu && (
          <p className="mb-3 text-sm text-muted">
            Its DNS settings are under <span className="text-white">{router.dns_menu}</span>.
          </p>
        )}
        {router.dot !== 'no' ? (
          <Verdict ok>
            {router.name} routers can use encrypted DNS. That protects every device at home, even when {short(isp)} shares your
            internet address with other homes.
            {router.dot === 'advanced' && <span className="text-muted"> It takes a few more steps than on ASUS.</span>}
            <div className="mt-3">{dot(`Set up encrypted DNS on my ${router.name}`)}</div>
          </Verdict>
        ) : (
          <>
            <p className="text-sm">
              {router.source === 'generic' ? 'Most routers' : `${router.name} routers`} only offer plain DNS. That works only if your home has
              its own public address.
            </p>
            <HomeIpCheck onResult={setCheck} />
            {check?.cgnat === false && (
              <Verdict ok>
                Your home has its own address ({check.public_ip}), so plain DNS on your router works.
                <div className="mt-3">
                  <button
                    className="btn-primary"
                    disabled={busy}
                    onClick={() => onCreate({ name: 'Home WiFi', brand: router.brand, method: 'ipv4_demo', linkedIp: check.public_ip ?? undefined })}
                  >
                    <Roll>
                      {busy && <Loader2 className="h-4 w-4 animate-spin" />} Set up plain DNS on my router
                    </Roll>
                  </button>
                </div>
              </Verdict>
            )}
            {check && check.cgnat !== false && <Alternatives isp={isp} onPerDevice={onPerDevice} why={check.advice} replaceRouter />}
            <p className="mt-4 text-xs text-muted">
              Some newer models list DNS-over-TLS in their DNS settings. If yours does:{' '}
              <button className="underline underline-offset-2 hover:text-white" disabled={busy} onClick={() => onCreate({ name: 'Home WiFi', brand: router.brand, method: 'dot' })}>
                use encrypted DNS instead
              </button>
              .
            </p>
          </>
        )}
      </Step>
    </>
  )
}

// ---------------------------------------------------------------- shared pieces

interface HomeIp {
  public_ip: string | null
  cgnat: boolean | null
  advice: string
}

/** Compares the router's WAN address with the address QuietNet sees this browser coming from. */
function HomeIpCheck({ onResult }: { onResult: (r: HomeIp) => void }) {
  const [wan, setWan] = useState('')
  const check = useMutation({ mutationFn: () => api.cgnat(wan.trim()), onSuccess: onResult })
  return (
    <div className="mt-4 rounded-md border border-line bg-ink p-4 text-sm">
      <div className="font-medium">Check your address</div>
      <p className="mt-1 text-muted">
        On the router's Status page, find the WAN IP (sometimes called Internet IP or IPv4 address) and type it here. Do this from
        your home WiFi.
      </p>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (wan.trim()) check.mutate()
        }}
      >
        <input className="input" placeholder="e.g. 100.72.14.9" value={wan} onChange={(e) => setWan(e.target.value)} inputMode="decimal" />
        <button type="submit" className="btn-ghost shrink-0" disabled={!wan.trim() || check.isPending}>
          <Roll>{check.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null} Check</Roll>
        </button>
      </form>
      {check.isError && <div className="mt-2 text-danger">{(check.error as Error).message}</div>}
    </div>
  )
}

function Alternatives({
  isp,
  why,
  onOwnRouter,
  onPerDevice,
  replaceRouter,
}: {
  isp: IspGuide
  why: string
  onOwnRouter?: () => void
  onPerDevice: () => void
  replaceRouter?: boolean
}) {
  return (
    <div className="mt-4 grid gap-3">
      {why && <Note tone="warn">{why}</Note>}
      <div className="text-sm font-medium">Your options</div>
      <AltCard title="Protect devices one by one" action={<button className="btn-primary" onClick={onPerDevice}><Roll>Choose a device</Roll></button>}>
        Phones, laptops and most TVs connect to QuietNet themselves with encrypted DNS. Works with any provider, takes a minute each.
      </AltCard>
      <AltCard
        title={replaceRouter ? 'Use a router with encrypted DNS' : 'Plug in your own router with encrypted DNS'}
        action={onOwnRouter && <button className="btn-ghost" onClick={onOwnRouter}><Roll>I have one</Roll></button>}
      >
        An ASUS router (or GL.iNet / OpenWrt) {replaceRouter ? 'in its place' : `plugged into ${box(isp)}`} protects the
        whole home, even on a shared address.
      </AltCard>
      {isp.own_ip && <AltCard title="Get your own public address">{isp.own_ip}</AltCard>}
    </div>
  )
}

function AltCard({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-ink p-4">
      <div className="min-w-0 flex-1 basis-64">
        <div className="font-medium">{title}</div>
        <div className="mt-1 text-sm text-muted">{children}</div>
      </div>
      {action}
    </div>
  )
}

function AdminLinks({ urls }: { urls: string[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {urls.map((u) => (
        <a key={u} href={u} target="_blank" rel="noreferrer" className="btn-ghost font-mono">
          <Roll>
            {u.replace('http://', '')} <ExternalLink className="h-3.5 w-3.5" />
          </Roll>
        </a>
      ))}
    </div>
  )
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <section className="card p-5">
      <div className="flex items-center gap-3">
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-line font-display text-sm">{n}</span>
        <h2 className="font-display text-lg leading-tight">{title}</h2>
      </div>
      <div className="mt-4">{children}</div>
    </section>
  )
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-full border px-3 py-2 text-sm transition ${active ? 'border-white bg-white text-ink' : 'border-line hover:border-line-strong'}`}
    >
      {children}
    </button>
  )
}

function Option({ active, onClick, title, children }: { active: boolean; onClick: () => void; title: string; children: ReactNode }) {
  return (
    <button onClick={onClick} aria-pressed={active} className={`card p-4 text-left ${active ? 'border-guard' : 'card-link'}`}>
      <div className="font-medium">{title}</div>
      <div className="mt-1 text-xs text-muted">{children}</div>
    </button>
  )
}

function Fact({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded-md border border-line bg-ink p-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-0.5 font-medium">{value}</div>
      <div className="mt-1 text-xs text-muted">{detail}</div>
    </div>
  )
}

function Verdict({ ok, children }: { ok?: boolean; children: ReactNode }) {
  return (
    <div className={`mt-4 flex gap-3 rounded-md border p-4 text-sm ${ok ? 'border-guard/50 bg-guard/10' : 'border-line bg-ink'}`}>
      {ok ? <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-guard" /> : <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-muted" />}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

function Note({ tone, children }: { tone: 'warn' | 'info'; children: ReactNode }) {
  return (
    <div className={`mt-4 flex gap-2 rounded-md p-3 text-sm ${tone === 'warn' ? 'bg-warn/10 text-warn' : 'border border-line bg-ink text-muted'}`}>
      {tone === 'warn' ? <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /> : <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-guard" />}
      <div>{children}</div>
    </div>
  )
}

/** Says where a guide came from when it isn't one we checked by hand. */
function SourceNote({ source, name, router }: { source: GuideSource; name: string; router?: boolean }) {
  if (source === 'curated') return null
  const first = router ? `the label on the router and the ${name} manual` : `the sticker on the box, and ${name} itself,`
  return (
    <div className="-mt-4 mb-4">
      <Note tone="info">
        {source === 'ai'
          ? `Written by AI for ${name}, so it can be wrong: ${first} come first.`
          : `We don't have a guide for ${name} yet, so these are the usual steps for most ${router ? 'routers' : 'boxes'}.`}
      </Note>
    </div>
  )
}

/** Which provider this connection comes from. */
function Detected({
  d,
  loading,
  picked,
  busy,
  onUse,
}: {
  d: ProviderDetection | undefined
  loading: boolean
  picked: IspGuide | null
  busy: boolean
  onUse: (network: string) => void
}) {
  if (loading)
    return (
      <p className="mb-3 flex items-center gap-2 text-xs text-muted">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Checking which provider this connection is from…
      </p>
    )
  if (!d?.found || !d.network) return null
  const isPicked = !!d.isp && picked?.key === d.isp.key
  return (
    <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-line bg-ink p-3 text-sm">
      <Wifi className="h-4 w-4 shrink-0 text-guard" />
      <div className="min-w-0 flex-1 basis-60">
        This connection is from <span className="font-medium text-white">{d.network}</span>
        {isPicked ? ', so it is picked below.' : '.'}{' '}
        <span className="text-muted">On mobile data? Then that's your mobile provider: pick your home one.</span>
      </div>
      {!d.isp && (
        <button className="btn-ghost shrink-0" disabled={busy} onClick={() => onUse(d.network!)}>
          <Roll>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />} Get a guide for it
          </Roll>
        </button>
      )}
    </div>
  )
}

function NameForm({
  label,
  placeholder,
  busy,
  onSubmit,
}: {
  label: string
  placeholder: string
  busy: boolean
  onSubmit: (name: string) => void
}) {
  const [v, setV] = useState('')
  return (
    <form
      className="mt-3 flex gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (v.trim()) onSubmit(v.trim())
      }}
    >
      <input className="input" aria-label={label} placeholder={placeholder} value={v} maxLength={60} onChange={(e) => setV(e.target.value)} autoFocus />
      <button type="submit" className="btn-ghost shrink-0" disabled={!v.trim() || busy}>
        <Roll>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />} Get guide
        </Roll>
      </button>
    </form>
  )
}

function short(isp: IspGuide) {
  return isp.short
}

/** "the Jio box" */
function box(isp: IspGuide) {
  return `the ${isp.short} box`
}

function odds(o: Odds, what: 'locked' | 'shared') {
  if (o === 'usually') return what === 'locked' ? 'Usually locked' : 'Usually shared'
  if (o === 'often') return what === 'locked' ? 'Often locked' : 'Often shared'
  return what === 'locked' ? 'Depends on the box' : 'Depends on the area'
}
