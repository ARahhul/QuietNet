import { Activity, Lock, ScanSearch, Sparkles } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Brand } from '../components/Layout'
import SiteFooter from '../components/SiteFooter'
import { Roll } from '../components/ui'
import { useSession } from '../session'

/**
 * The public homepage at quietnet.in (and /welcome). It leads with privacy (a cited study of what TV apps send to
 * trackers) and ads come second. The hero's stream shows an example home's lookups arriving one by one, with the
 * tracker and ad names struck out as QuietNet answers them with silence.
 */

interface Lookup {
  device: string
  name: string
  /** What QuietNet stopped; none = allowed. */
  kind?: 'tracker' | 'ad'
}

// An example evening. Real, well-known names, so people recognise what their own devices do.
const FEED: Lookup[] = [
  { device: 'Fire TV Stick', name: 'device-metrics-us.amazon.com', kind: 'tracker' },
  { device: 'Amma’s phone', name: 'mmg.whatsapp.net' },
  { device: 'Living room TV', name: 'googleads.g.doubleclick.net', kind: 'ad' },
  { device: 'Living room TV', name: 'www.youtube.com' },
  { device: 'Arjun’s laptop', name: 'api.mixpanel.com', kind: 'tracker' },
  { device: 'Amma’s phone', name: 'app-measurement.com', kind: 'tracker' },
  { device: 'Arjun’s laptop', name: 'www.cricbuzz.com' },
  { device: 'Fire TV Stick', name: 'aax.amazon-adsystem.com', kind: 'ad' },
  { device: 'Living room TV', name: 'api-global.netflix.com' },
  { device: 'Living room TV', name: 'sb.scorecardresearch.com', kind: 'tracker' },
  { device: 'Amma’s phone', name: 'connectivitycheck.gstatic.com' },
  { device: 'Arjun’s laptop', name: 'pagead2.googlesyndication.com', kind: 'ad' },
  { device: 'Arjun’s laptop', name: 'www.irctc.co.in' },
  { device: 'Living room TV', name: 'z.moatads.com', kind: 'tracker' },
  { device: 'Amma’s phone', name: 'g.whatsapp.net' },
  { device: 'Fire TV Stick', name: 'ib.adnxs.com', kind: 'ad' },
]
const SHOWN = 7
const ROW = 52 // px, keep in step with the row's h-[52px]

function useReducedMotion() {
  const [reduced, setReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches)
  useEffect(() => {
    const m = matchMedia('(prefers-reduced-motion: reduce)')
    const on = () => setReduced(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return reduced
}

interface Row extends Lookup {
  key: number
  quiet: boolean
}

function Stream() {
  const reduced = useReducedMotion()
  const box = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const [rows, setRows] = useState<Row[]>(() =>
    FEED.slice(0, SHOWN).map((r, i) => ({ ...r, key: SHOWN - 1 - i, quiet: !!r.kind })),
  )
  const [counts, setCounts] = useState({ total: 2140, blocked: 611 })
  const next = useRef(SHOWN)

  useEffect(() => {
    if (reduced) return
    let visible = true
    const io = new IntersectionObserver(([e]) => (visible = e.isIntersecting))
    if (box.current) io.observe(box.current)
    const timers: number[] = []
    const tick = window.setInterval(() => {
      if (!visible || document.hidden) return
      const key = next.current++
      const r = FEED[key % FEED.length]
      setRows((xs) => [{ ...r, key, quiet: false }, ...xs].slice(0, SHOWN + 1))
      setCounts((c) => ({ total: c.total + 1, blocked: c.blocked + (r.kind ? 1 : 0) }))
      if (r.kind) {
        timers.push(window.setTimeout(() => setRows((xs) => xs.map((x) => (x.key === key ? { ...x, quiet: true } : x))), 700))
      }
    }, 1500)
    return () => {
      clearInterval(tick)
      timers.forEach(clearTimeout)
      io.disconnect()
    }
  }, [reduced])

  // Each arrival nudges the whole list down one row, so the stream moves like a conveyor instead of jumping.
  const newest = rows[0]?.key
  useLayoutEffect(() => {
    if (reduced || !list.current || newest < SHOWN) return
    list.current.animate([{ transform: `translateY(-${ROW}px)` }, { transform: 'none' }], {
      duration: 550,
      easing: 'cubic-bezier(0.44, 0, 0.56, 1)',
    })
  }, [newest, reduced])

  return (
    <div
      ref={box}
      className="card overflow-hidden shadow-[0_40px_90px_-40px_rgb(0_0_0/0.95)] motion-safe:animate-fade-in motion-safe:[animation-delay:450ms]"
      aria-label="Example: lookups from a home's devices, with trackers and ads blocked"
      role="img"
    >
      <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-3 text-xs sm:px-5">
        <span className="text-muted">Example home, this evening</span>
        <span className="flex items-center gap-1.5 text-body">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full rounded-full bg-guard opacity-60 motion-safe:animate-ping" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-guard" />
          </span>
          4 devices protected
        </span>
      </div>
      <div className="relative overflow-hidden" style={{ height: ROW * SHOWN }}>
        <ul ref={list} aria-hidden="true">
          {rows.map((r) => (
            <li key={r.key} className="flex h-[52px] items-center gap-3 border-b border-line/70 px-4 sm:px-5">
              <div className="min-w-0 flex-1">
                <div className="truncate font-mono text-[13px] text-white">
                  <span className={`strike-draw ${r.quiet ? 'is-quiet' : ''}`}>{r.name}</span>
                </div>
                <div className="mt-0.5 truncate text-[11px] text-muted">{r.device}</div>
              </div>
              <Status kind={r.kind} quiet={r.quiet} />
            </li>
          ))}
        </ul>
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-panel to-transparent" />
      </div>
      <div className="flex items-baseline justify-between gap-3 border-t border-line px-4 py-3 text-xs sm:px-5">
        <span className="text-muted">Today</span>
        <span className="text-body tabular-nums">
          <span className="text-blocked">{counts.blocked.toLocaleString('en-IN')} stopped</span> of{' '}
          {counts.total.toLocaleString('en-IN')} lookups
        </span>
      </div>
    </div>
  )
}

function Status({ kind, quiet }: { kind?: Lookup['kind']; quiet: boolean }) {
  if (!kind) return <span className="shrink-0 text-[11px] text-guard/80">allowed</span>
  return (
    <span
      className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] transition duration-500 ${
        quiet ? 'border-blocked/50 text-blocked' : 'border-line text-muted'
      }`}
    >
      {quiet ? `${kind} blocked` : 'checking'}
    </span>
  )
}

/** Counts up to `to` once, after `delay` ms (straight to the end with reduced motion). */
function useCountUp(to: number, delay: number, reduced: boolean) {
  const [n, setN] = useState(0)
  useEffect(() => {
    if (reduced) return
    let raf = 0
    const timer = window.setTimeout(() => {
      const start = performance.now()
      const step = (now: number) => {
        const t = Math.min(1, (now - start) / 1400)
        setN(Math.round(to * (1 - (1 - t) ** 3)))
        if (t < 1) raf = requestAnimationFrame(step)
      }
      raf = requestAnimationFrame(step)
    }, delay)
    return () => {
      clearTimeout(timer)
      cancelAnimationFrame(raf)
    }
  }, [to, delay, reduced])
  return reduced ? to : n
}

// Moghaddam et al., "Watching You Watch: The Tracking Ecosystem of Over-the-Top TV Streaming Devices",
// ACM CCS 2019 (Princeton University and University of Chicago): the top 1,000 channels on each platform.
const STUDY_URL = 'https://par.nsf.gov/servlets/purl/10180381'

/** The proof under the headline: what a peer-reviewed study found TV apps doing. */
function Evidence({ reduced }: { reduced: boolean }) {
  const pct = useCountUp(89, 1500, reduced)
  const domains = useCountUp(60, 1650, reduced)
  return (
    <figure className="mt-12 border-t border-line pt-7 motion-safe:animate-fade-in motion-safe:[animation-delay:1350ms]">
      <div className="grid gap-7 sm:grid-cols-2 sm:gap-10">
        <div>
          <div className="font-display text-5xl font-light tracking-[-0.03em] text-white tabular-nums md:text-6xl">
            <span aria-hidden="true">{pct}%</span>
            <span className="sr-only">89%</span>
          </div>
          <p className="mt-2 max-w-[30ch] text-sm leading-relaxed text-body">
            of the 1,000 most popular Amazon Fire TV apps contacted known trackers.
          </p>
        </div>
        <div>
          <div className="font-display text-5xl font-light tracking-[-0.03em] text-white tabular-nums md:text-6xl">
            <span aria-hidden="true">{domains}+</span>
            <span className="sr-only">More than 60</span>
          </div>
          <p className="mt-2 max-w-[30ch] text-sm leading-relaxed text-body">
            tracking domains reached by the busiest TV apps. Trackers were sent the titles people watched, WiFi names
            and device serial numbers.
          </p>
        </div>
      </div>
      <figcaption className="mt-6 text-xs text-muted">
        Source:{' '}
        <a href={STUDY_URL} target="_blank" rel="noreferrer" className="hover:text-white">
          <span className="underline-slide">“Watching You Watch”, Princeton University and University of Chicago, ACM CCS 2019</span>
        </a>
      </figcaption>
    </figure>
  )
}

/** One headline line that rises out of its own mask on load. */
function Line({ children, delay }: { children: ReactNode; delay: number }) {
  return (
    <span className="block overflow-hidden pb-[0.08em]">
      <span className="block motion-safe:animate-rise" style={{ animationDelay: `${delay}ms` }}>
        {children}
      </span>
    </span>
  )
}

const STEPS = [
  { title: 'Sign in', text: 'Use Google or your email. Your home gets its own account, and nobody else sees its devices.' },
  {
    title: 'Change one setting on your router',
    text: 'Put QuietNet’s address in the DNS box. The guide shows where it is for ACT, Airtel, Jio and other providers, and you can paste a screenshot if you get stuck.',
  },
  {
    title: 'Choose what goes quiet',
    text: 'Every name your devices look up is explained in plain words. Block it for one device or the whole home, and undo it any time.',
  },
]

const FEATURES = [
  {
    icon: Activity,
    title: 'See who your devices talk to',
    text: 'Every company your TV, phones and laptops reach, as it happens, with each name explained in plain words.',
  },
  {
    icon: Sparkles,
    title: 'Auto-mode',
    text: 'Blocks the trackers and ads QuietNet is sure about, and leaves alone anything an app needs to keep working.',
  },
  {
    icon: ScanSearch,
    title: 'Saw an ad? Say where',
    text: 'Type “Hotstar app, banner on the home screen”. The AI checks what was looked up in those minutes and finds the names that served it.',
  },
  {
    icon: Lock,
    title: 'Only your home',
    text: 'Each home signs in to its own account. You see your devices and nothing from anyone else’s.',
  },
]

/** "Think your WiFi is private? It isn't.": the word "private" is struck out before the answer rises in. */
function Headline({ reduced }: { reduced: boolean }) {
  const [fired, setFired] = useState(false)
  useEffect(() => {
    if (reduced) return
    const t = window.setTimeout(() => setFired(true), 850)
    return () => clearTimeout(t)
  }, [reduced])
  const struck = reduced || fired
  return (
    <h1 className="font-display text-[clamp(2.6rem,7.4vw,5rem)] leading-[1] font-light tracking-[-0.035em]">
      <Line delay={0}>Think your WiFi</Line>
      <Line delay={110}>
        is{' '}
        <span
          className={`strike-draw ${struck ? 'is-quiet' : ''}`}
          style={{ '--strike': '0.075em', '--strike-time': '600ms' } as CSSProperties}
        >
          private
        </span>
        ?
      </Line>
      <Line delay={1050}>
        <span className="text-blocked">It isn’t.</span>
      </Line>
    </h1>
  )
}

export default function Landing() {
  const session = useSession()
  const signedIn = session.status === 'signed-in'
  const reduced = useReducedMotion()

  const primary = signedIn ? (
    <Link to="/" className="btn-primary px-6 py-3">
      <Roll>Open your dashboard</Roll>
    </Link>
  ) : (
    <Link to="/login" className="btn-primary px-6 py-3">
      <Roll>Get started</Roll>
    </Link>
  )

  return (
    <div className="min-h-dvh bg-ink text-white">
      <header className="mx-auto flex max-w-6xl items-center justify-between px-5 py-5 md:px-10 md:py-7">
        <Link to="/welcome" aria-label="QuietNet home">
          <Brand />
        </Link>
        <nav className="flex items-center gap-2">
          <a href="#how" className="hidden px-3 py-2 text-sm text-muted hover:text-white sm:block">
            <span className="underline-slide">How it works</span>
          </a>
          {signedIn ? (
            <Link to="/" className="btn-ghost">
              <Roll>Dashboard</Roll>
            </Link>
          ) : (
            <Link to="/login" className="btn-ghost">
              <Roll>Sign in</Roll>
            </Link>
          )}
        </nav>
      </header>

      <main>
        <section className="relative">
          <div className="grid-lines pointer-events-none absolute inset-0 [mask-image:linear-gradient(to_bottom,black,transparent_85%)]" />
          <div className="relative mx-auto grid max-w-6xl items-center gap-12 px-5 pt-10 pb-20 md:px-10 md:pt-16 lg:grid-cols-[1.1fr_1fr] lg:gap-16 lg:pb-28">
            <div>
              <Headline reduced={reduced} />
              <p className="mt-7 max-w-[34rem] text-base leading-relaxed text-body motion-safe:animate-fade-in motion-safe:[animation-delay:1200ms] md:text-lg">
                The TV, the streaming stick and the phones in your home report to tracking companies all day. QuietNet
                shows you every one and lets you stop them at your router, for every device at once, with nothing to
                install.{' '}
                <span className="text-white">And the ads go with them.</span>
              </p>
              <div className="mt-9 flex flex-wrap items-center gap-3 motion-safe:animate-fade-in motion-safe:[animation-delay:1280ms]">
                {primary}
                <a href="#how" className="btn-ghost px-6 py-3">
                  <Roll>How it works</Roll>
                </a>
              </div>
              <Evidence reduced={reduced} />
            </div>
            <Stream />
          </div>
        </section>

        <section id="how" className="scroll-mt-6 border-t border-line">
          <div className="mx-auto max-w-6xl px-5 py-20 md:px-10 md:py-28">
            <h2 className="max-w-xl font-display text-3xl leading-tight font-light tracking-[-0.02em] md:text-[44px]">
              Set up once, from your sofa
            </h2>
            <ol className="mt-12 grid gap-10 md:grid-cols-3 md:gap-0">
              {STEPS.map((s, i) => (
                <li key={s.title} className="border-line md:border-l md:px-8 md:first:border-l-0 md:first:pl-0">
                  <div className="font-display text-5xl font-extralight text-line-strong tabular-nums">{i + 1}</div>
                  <h3 className="mt-5 text-lg font-medium">{s.title}</h3>
                  <p className="mt-2 max-w-[38ch] text-sm leading-relaxed text-body">{s.text}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section className="border-t border-line">
          <div className="mx-auto max-w-6xl px-5 py-20 md:px-10 md:py-28">
            <h2 className="max-w-xl font-display text-3xl leading-tight font-light tracking-[-0.02em] md:text-[44px]">
              What you can do with it
            </h2>
            <div className="mt-12 grid border-t border-line sm:grid-cols-2">
              {FEATURES.map((f, i) => (
                <div
                  key={f.title}
                  className={`border-b border-line py-8 sm:px-8 ${i % 2 === 0 ? 'sm:border-r sm:pl-0' : 'sm:pr-0'}`}
                >
                  <f.icon className="h-5 w-5 text-guard" strokeWidth={1.6} />
                  <h3 className="mt-4 text-lg font-medium">{f.title}</h3>
                  <p className="mt-2 max-w-[44ch] text-sm leading-relaxed text-body">{f.text}</p>
                </div>
              ))}
            </div>
            <p className="mt-10 max-w-[62ch] text-sm leading-relaxed text-muted">
              One thing no DNS blocker can do: ads stitched into a live stream, like the breaks in a cricket match, come from
              the same servers as the match itself. QuietNet tells you when that’s what you saw, so you don’t block the match.
            </p>
          </div>
        </section>

        <section className="border-t border-line">
          <div className="mx-auto flex max-w-6xl flex-col items-start gap-8 px-5 py-20 md:flex-row md:items-end md:justify-between md:px-10 md:py-24">
            <h2 className="max-w-2xl font-display text-4xl leading-[1.05] font-light tracking-[-0.03em] md:text-6xl">
              Keep what happens at home, at home.
            </h2>
            {primary}
          </div>
        </section>
      </main>

      <SiteFooter />
    </div>
  )
}
