import { ArrowLeft, Loader2, Mail, MailCheck } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { Brand } from '../components/Layout'
import TokenForm from '../components/TokenForm'
import { Roll } from '../components/ui'
import {
  emailLinkAddress,
  finishEmailLink,
  isEmailLink,
  sendEmailLink,
  signInProblem,
  signInWithGoogle,
} from '../lib/firebase'
import { firebaseEnabled, useSession } from '../session'

/** Only paths inside the dashboard, so a crafted ?next= can't send people elsewhere. */
function safeNext(next: string | null) {
  return next && next.startsWith('/') && !next.startsWith('//') && !next.startsWith('/login') ? next : '/'
}

export default function Login() {
  const session = useSession()
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))
  const [linkMode] = useState(isEmailLink) // opened from the emailed sign-in link

  // Someone using the access token can sign in here (Settings sends them, or they open an emailed link):
  // the home the token opens then moves into the new account.
  const upgrading =
    firebaseEnabled && (params.has('upgrade') || linkMode) && session.status === 'signed-in' && session.via === 'token'
  if (session.status === 'signed-in' && !upgrading) return <Navigate to={next} replace />

  return (
    <div className="relative grid min-h-dvh place-items-center overflow-hidden bg-ink px-4 py-10">
      <div className="grid-lines pointer-events-none absolute inset-0 [mask-image:radial-gradient(ellipse_at_center,black,transparent_70%)]" />
      <div className="relative w-full max-w-md">
        <Link to="/welcome" className="mb-5 inline-flex items-center gap-1.5 text-sm text-muted hover:text-white">
          <ArrowLeft className="h-4 w-4" /> <span className="underline-slide">QuietNet home</span>
        </Link>
        <div className="card p-7 shadow-[0_30px_80px_-30px_rgb(0_0_0/0.9)] md:p-9">
          <Brand />
          {session.status === 'loading' ? (
            <div className="mt-10 flex items-center gap-2 text-sm text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> Checking who you are…
            </div>
          ) : firebaseEnabled && linkMode ? (
            <FinishEmailLink />
          ) : firebaseEnabled ? (
            <SignIn notice={session.status === 'signed-out' ? session.notice : undefined} upgrading={upgrading} />
          ) : (
            <>
              <h1 className="mt-9 font-display text-2xl leading-tight">Enter your access token</h1>
              <p className="mt-2 mb-6 text-sm text-muted">
                This QuietNet server signs in with its access token. You only enter it once in this browser.
              </p>
              {session.status === 'signed-out' && session.notice && <p className="mb-4 text-sm text-warn">{session.notice}</p>}
              <TokenForm />
            </>
          )}
        </div>
        <p className="mt-5 px-2 text-center text-xs text-muted">
          Each home has its own QuietNet account. You only ever see your own devices and what they looked up.
        </p>
      </div>
    </div>
  )
}

function SignIn({ notice, upgrading }: { notice?: string; upgrading?: boolean }) {
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState<'google' | 'email' | null>(null)
  const [sentTo, setSentTo] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(notice ?? null)
  const [token, setToken] = useState(false)

  async function google() {
    setBusy('google')
    setProblem(null)
    try {
      await signInWithGoogle()
    } catch (e) {
      setProblem(signInProblem(e))
    } finally {
      setBusy(null)
    }
  }

  async function emailLink(e: FormEvent) {
    e.preventDefault()
    const address = email.trim()
    if (!address) return
    setBusy('email')
    setProblem(null)
    try {
      await sendEmailLink(address)
      setSentTo(address)
    } catch (ex) {
      setProblem(signInProblem(ex))
    } finally {
      setBusy(null)
    }
  }

  if (sentTo) {
    return (
      <div className="mt-9" role="status">
        <MailCheck className="h-8 w-8 text-guard" strokeWidth={1.5} />
        <h1 className="mt-4 font-display text-2xl leading-tight">Check your inbox</h1>
        <p className="mt-2 text-sm text-body">
          We sent a sign-in link to <span className="font-medium text-white">{sentTo}</span>. Open it on this device and you’re in.
          It can take a minute, and sometimes lands in spam.
        </p>
        <button className="btn-ghost mt-6" onClick={() => setSentTo(null)}>
          <Roll>Use a different email</Roll>
        </button>
      </div>
    )
  }

  return (
    <>
      <h1 className="mt-9 font-display text-2xl leading-tight">Sign in to QuietNet</h1>
      <p className="mt-2 text-sm text-muted">
        {upgrading
          ? 'You’re using this server’s access token. Sign in and the home it opens moves into your own account.'
          : 'New here? Signing in creates your home’s account. No password to remember.'}
      </p>
      {problem && (
        <p role="alert" className="mt-5 rounded-md bg-danger/10 px-3.5 py-2.5 text-sm text-danger">
          {problem}
        </p>
      )}
      <button className="btn-light mt-6 w-full" onClick={google} disabled={!!busy}>
        <Roll>
          {busy === 'google' ? <Loader2 className="h-4 w-4 animate-spin" /> : <GoogleMark />} Continue with Google
        </Roll>
      </button>

      <div className="my-6 flex items-center gap-3 text-xs text-muted">
        <span className="h-px flex-1 bg-line" /> or <span className="h-px flex-1 bg-line" />
      </div>

      <form onSubmit={emailLink}>
        <label htmlFor="email" className="block text-sm font-medium">
          Email
        </label>
        <input
          id="email"
          type="email"
          className="input mt-2"
          placeholder="you@example.com"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <button type="submit" className="btn-primary mt-4 w-full" disabled={!!busy || !email.trim()}>
          <Roll>
            {busy === 'email' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mail className="h-4 w-4" />} Email me a sign-in link
          </Roll>
        </button>
      </form>

      <div className={`mt-7 border-t border-line pt-5 ${upgrading ? 'hidden' : ''}`}>
        {token ? (
          <TokenForm />
        ) : (
          <button className="text-xs text-muted hover:text-white" onClick={() => setToken(true)}>
            <span className="underline-slide">Have this server’s access token? Use it instead</span>
          </button>
        )}
      </div>
    </>
  )
}

/** Opened from the emailed link: sign in, asking for the email again only if the link was sent from another browser. */
function FinishEmailLink() {
  const navigate = useNavigate()
  const [email, setEmail] = useState(emailLinkAddress)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const started = useRef(false)

  async function finish(address: string) {
    setBusy(true)
    setProblem(null)
    try {
      await finishEmailLink(address)
    } catch (e) {
      setProblem(signInProblem(e))
      setBusy(false)
    }
  }

  useEffect(() => {
    if (started.current || !emailLinkAddress()) return
    started.current = true
    void finish(emailLinkAddress())
  }, [])

  if (busy) {
    return (
      <div className="mt-10 flex items-center gap-2 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> Signing you in…
      </div>
    )
  }
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (email.trim()) void finish(email.trim())
      }}
    >
      <h1 className="mt-9 font-display text-2xl leading-tight">Confirm your email</h1>
      {!problem && <p className="mt-2 text-sm text-muted">You opened the link in a different browser. Type the email it was sent to.</p>}
      {problem && (
        <p role="alert" className="mt-5 rounded-md bg-danger/10 px-3.5 py-2.5 text-sm text-danger">
          {problem}
        </p>
      )}
      <input
        type="email"
        className="input mt-5"
        aria-label="Email"
        autoComplete="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
      />
      <button type="submit" className="btn-primary mt-4 w-full" disabled={!email.trim()}>
        <Roll>Sign in</Roll>
      </button>
      <button type="button" className="btn-ghost mt-3 w-full" onClick={() => navigate('/login', { replace: true })}>
        <Roll>Send a new link</Roll>
      </button>
    </form>
  )
}

/** Google's "G", as its sign-in guidelines ask for on the button. */
function GoogleMark() {
  return (
    <svg viewBox="0 0 48 48" className="h-4 w-4" aria-hidden="true">
      <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z" />
      <path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-7.9l-6.5 5C9.5 39.6 16.2 44 24 44z" />
      <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z" />
    </svg>
  )
}
