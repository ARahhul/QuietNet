import { useQueryClient } from '@tanstack/react-query'
import { KeyRound, Loader2 } from 'lucide-react'
import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { ApiError, AUTH_EVENT, checkToken, getToken, setToken } from '../api'
import { Brand } from './Layout'
import { Roll } from './ui'

/**
 * Shows the app only once this browser has a token the backend accepts. Asks again whenever the
 * backend answers 401 (token changed on the server, or "Forget" in Settings).
 */
export default function TokenGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const [needed, setNeeded] = useState(() => !getToken())

  useEffect(() => {
    const ask = () => setNeeded(true)
    window.addEventListener(AUTH_EVENT, ask)
    return () => window.removeEventListener(AUTH_EVENT, ask)
  }, [])

  if (!needed) return children
  return (
    <TokenForm
      onAccepted={() => {
        setNeeded(false)
        qc.resetQueries()
      }}
    />
  )
}

function TokenForm({ onAccepted }: { onAccepted: () => void }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const token = value.trim()
    if (!token) return
    setBusy(true)
    setError(null)
    try {
      if (await checkToken(token)) {
        setToken(token)
        onAccepted()
      } else {
        setError("That token isn't right. Copy it again from the server, without spaces.")
      }
    } catch (ex) {
      setError(ex instanceof ApiError ? ex.message : "Can't reach the QuietNet server. Check your connection and try again.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-dvh place-items-center bg-ink px-4 py-10">
      <form onSubmit={submit} className="card w-full max-w-md p-7 md:p-9">
        <Brand />
        <h1 className="mt-9 font-display text-2xl leading-tight">Enter your access token</h1>
        <p className="mt-2 text-sm text-muted">
          The dashboard needs your server's access token before it can show your network. You only enter it once in this browser.
        </p>
        <label htmlFor="access-token" className="mt-7 block text-sm font-medium">
          Access token
        </label>
        <input
          id="access-token"
          type="password"
          className="input mt-2 font-mono"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          autoFocus
          aria-invalid={!!error}
          aria-describedby={error ? 'access-token-error' : undefined}
        />
        {error && (
          <p id="access-token-error" role="alert" className="mt-3 text-sm text-danger">
            {error}
          </p>
        )}
        <button type="submit" className="btn-primary mt-6 w-full" disabled={busy || !value.trim()}>
          <Roll>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
            {busy ? 'Checking' : 'Continue'}
          </Roll>
        </button>
      </form>
    </div>
  )
}
