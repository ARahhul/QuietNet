import { ArrowUp } from 'lucide-react'
import { useEffect, useRef, type CSSProperties, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { CREDITS } from '../lib/credits'
import { useSession } from '../session'
import { Brand } from './Layout'

const YEAR = new Date().getFullYear()

const COLUMNS: { title: string; links: { label: string; to: string }[] }[] = [
  {
    title: 'Product',
    links: [
      { label: 'Live lookups', to: '/live' },
      { label: 'Saw an ad?', to: '/?ad' },
      { label: 'Blocked list', to: '/blocked' },
      { label: 'Report', to: '/report' },
      { label: 'Auto-mode', to: '/settings' },
    ],
  },
  {
    title: 'Set up',
    links: [
      { label: 'Home WiFi router', to: '/setup' },
      { label: 'Phone', to: '/setup' },
      { label: 'Smart TV', to: '/setup' },
      { label: 'Laptop', to: '/setup' },
      { label: 'How it works', to: '#how' },
    ],
  },
  {
    title: 'Account',
    links: [
      { label: 'Sign in', to: '/login' },
      { label: 'Create your home’s account', to: '/login' },
      { label: 'Settings', to: '/settings' },
      { label: 'Credits', to: '#credits' },
    ],
  },
]

// The providers QuietNet's setup guide walks through (backend/pahra/data/providers.yaml)
const PROVIDERS = ['Jio', 'Airtel', 'ACT Fibernet', 'Hathway', 'BSNL Bharat Fiber', 'Excitel', 'Tata Play Fiber']

function Chip({ children }: { children: ReactNode }) {
  return <span className="rounded-full border border-line px-3 py-1 text-[11px] text-body">{children}</span>
}

export default function SiteFooter() {
  const signedIn = useSession().status === 'signed-in'
  // Signed out, "/" is this homepage: send dashboard links through sign-in instead.
  const dashboard = (to: string) => (to === '/?ad' ? (signedIn ? '/' : '/login') : to)
  return (
    <footer className="border-t border-line">
      <div className="mx-auto max-w-6xl px-5 pt-16 md:px-10 md:pt-20">
        <div className="grid gap-12 sm:grid-cols-2 lg:grid-cols-[1.5fr_1fr_1fr_1fr] lg:gap-10">
          <div>
            <Brand />
            <p className="mt-5 max-w-[34ch] text-sm leading-relaxed text-body">
              QuietNet blocks ads and trackers for every device on your home WiFi, and explains each name it sees in plain
              words. Each home has its own account.
            </p>
          </div>
          {COLUMNS.map((c) => (
            <nav key={c.title} aria-label={c.title}>
              <h2 className="text-sm font-medium text-white">{c.title}</h2>
              <ul className="mt-4 grid gap-2.5 text-sm">
                {c.links.map((l) => (
                  <li key={l.label}>
                    {l.to.startsWith('#') ? (
                      <a href={l.to} className="text-muted transition hover:text-white">
                        <span className="underline-slide">{l.label}</span>
                      </a>
                    ) : (
                      <Link to={dashboard(l.to)} className="text-muted transition hover:text-white">
                        <span className="underline-slide">{l.label}</span>
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            </nav>
          ))}
        </div>

        <div className="mt-14 flex flex-wrap items-center gap-2 border-t border-line py-7">
          <span className="mr-2 text-xs text-muted">Setup guides for</span>
          {PROVIDERS.map((p) => (
            <Chip key={p}>{p}</Chip>
          ))}
        </div>

        <section className="border-t border-line py-10" aria-labelledby="credits">
          <h2 id="credits" className="text-sm font-medium text-white">
            Credits
          </h2>
          <p className="mt-1 text-xs text-muted">QuietNet is built on open-source software and public services. Thank you to them.</p>
          <ul className="mt-6 grid gap-x-10 gap-y-6 sm:grid-cols-2 lg:grid-cols-3">
            {CREDITS.map((c) => (
              <li key={c.name}>
                <a href={c.url} target="_blank" rel="noreferrer" className="text-sm text-white">
                  <span className="underline-slide">{c.name}</span>
                </a>
                {c.license && <span className="ml-2 text-[11px] text-muted">{c.license}</span>}
                <p className="mt-1 max-w-[46ch] text-xs leading-relaxed text-muted">{c.role}</p>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <HushWave />

      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-5 pt-2 pb-8 text-xs text-muted sm:flex-row sm:items-center sm:justify-between md:px-10">
        <span>© {YEAR} QuietNet. The silent home sentry.</span>
        <button
          className="group inline-flex w-fit items-center gap-1.5 hover:text-white"
          onClick={() => window.scrollTo({ top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })}
        >
          <ArrowUp className="h-3.5 w-3.5 transition-transform motion-safe:group-hover:-translate-y-0.5" />
          <span className="underline-slide">Back to top</span>
        </button>
      </div>
    </footer>
  )
}

const BARS = 96
/** A stable 0..1 "random" number per bar, so the wave looks the same on every visit. */
const noise = (i: number) => {
  const x = Math.sin(i * 12.9898 + 4.1414) * 43758.5453
  return x - Math.floor(x)
}

/**
 * The page's sign-off: when the footer scrolls into view, a burst of noise sweeps across the bars (red, like the
 * ads), then the whole line settles into a quiet ripple (green). Under the pointer the bars rise. Static with reduced motion.
 */
function HushWave() {
  const band = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = band.current
    if (!el) return
    const io = new IntersectionObserver(
      ([e]) => {
        if (e.isIntersecting) {
          el.classList.add('is-live')
          io.disconnect()
        }
      },
      { threshold: 0.6 },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [])

  return (
    <div
      ref={band}
      className="hush mx-auto flex h-28 max-w-6xl md:h-36 items-center justify-between px-5 md:px-10"
      style={{ '--x': -100 } as CSSProperties}
      aria-hidden="true"
      onPointerMove={(e) => {
        const r = e.currentTarget.getBoundingClientRect()
        e.currentTarget.style.setProperty('--x', String(((e.clientX - r.left) / r.width) * BARS))
      }}
      onPointerLeave={(e) => e.currentTarget.style.setProperty('--x', '-100')}
    >
      {Array.from({ length: BARS }, (_, i) => (
        <span
          key={i}
          className="hush-lift flex h-full w-[3px] items-center max-sm:[&:nth-child(odd)]:hidden"
          style={{ '--i': i } as CSSProperties}
        >
          <span
            className="hush-bar block h-full w-full rounded-full"
            style={
              {
                '--peak': (0.3 + 0.7 * noise(i)).toFixed(3),
                '--swell': (0.1 + 0.12 * noise(i + 97)).toFixed(3),
                '--delay': `${i * 14}ms`,
                '--ripple-delay': `${1600 + i * 45}ms`,
                background: `color-mix(in oklab, var(--color-blocked) ${Math.round(100 - (i / (BARS - 1)) * 100)}%, var(--color-guard))`,
              } as CSSProperties
            }
          />
        </span>
      ))}
    </div>
  )
}
