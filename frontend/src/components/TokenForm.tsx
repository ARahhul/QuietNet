import { KeyRound, Loader2 } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { ApiError, checkToken, setToken } from '../api'
import { Roll } from './ui'

/** Sign in with the server's access token (scripts, self-hosted servers without Firebase, the operator's old browser). */
export default function TokenForm({ onAccepted }: { onAccepted?: () => void }) {
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
        onAccepted?.()
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
    <form onSubmit={submit}>
      <label htmlFor="access-token" className="block text-sm font-medium">
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
        aria-invalid={!!error}
        aria-describedby={error ? 'access-token-error' : undefined}
      />
      {error && (
        <p id="access-token-error" role="alert" className="mt-3 text-sm text-danger">
          {error}
        </p>
      )}
      <button type="submit" className="btn-ghost mt-4 w-full" disabled={busy || !value.trim()}>
        <Roll>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
          {busy ? 'Checking' : 'Continue with the token'}
        </Roll>
      </button>
    </form>
  )
}
