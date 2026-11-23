import { useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Link2, Loader2, RotateCw, TriangleAlert } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api, type SetupCheck } from '../api'
import { Roll } from './ui'

/** Router and phone changes take a moment to apply, so keep looking this long before showing what to fix. */
const WATCH_S = 60
const EVERY_S = 5

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/**
 * "I've done it": QuietNet checks on its own side that this device's lookups are really arriving
 * (a fresh read of the query log). If none yet, it keeps watching for a minute, then says what usually fixes it.
 */
export default function DoneCheck({ deviceId, aiHelp }: { deviceId: string; aiHelp?: boolean }) {
  const qc = useQueryClient()
  const [phase, setPhase] = useState<'idle' | 'watching' | 'result'>('idle')
  const [result, setResult] = useState<SetupCheck | null>(null)
  const [left, setLeft] = useState(WATCH_S)
  const [problem, setProblem] = useState<string | null>(null)
  const [linking, setLinking] = useState(false)
  const stopped = useRef(false)

  /** Plain DNS on the router: QuietNet answers the home only from its linked address. */
  const link = async (ip: string) => {
    setLinking(true)
    setProblem(null)
    try {
      await api.patchDevice(deviceId, { linked_ip: ip, allow_shared_ipv4: true })
      qc.invalidateQueries({ queryKey: ['device', deviceId] })
      qc.invalidateQueries({ queryKey: ['devices'] })
    } catch (e) {
      setProblem((e as Error).message)
      return
    } finally {
      setLinking(false)
    }
    await run()
  }

  useEffect(
    () => () => {
      stopped.current = true // leaving the page ends the watch
    },
    [],
  )

  const run = async () => {
    stopped.current = false
    setProblem(null)
    setPhase('watching')
    const end = Date.now() + WATCH_S * 1000
    try {
      for (;;) {
        setLeft(Math.max(0, Math.ceil((end - Date.now()) / 1000)))
        const r = await api.checkDevice(deviceId)
        if (r.ok || stopped.current || Date.now() >= end) {
          setResult(r)
          setPhase('result')
          if (r.ok) {
            qc.invalidateQueries({ queryKey: ['device', deviceId] }) // the waiting card at the top turns green
            qc.invalidateQueries({ queryKey: ['devices'] })
          }
          return
        }
        for (let i = 0; i < EVERY_S && !stopped.current; i++) {
          await sleep(1000)
          setLeft(Math.max(0, Math.ceil((end - Date.now()) / 1000)))
        }
      }
    } catch (e) {
      setProblem((e as Error).message)
      setPhase('idle')
    }
  }

  return (
    <div className="card p-5" data-testid="done-check">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="font-display text-lg">Finished the steps?</div>
          <p className="text-sm text-muted">QuietNet checks on its side that lookups from this device are really arriving.</p>
        </div>
        {phase === 'idle' && (
          <button className="btn-primary" onClick={run}>
            <Roll>
              <CheckCircle2 className="h-4 w-4" /> I've done it, check
            </Roll>
          </button>
        )}
        {phase === 'result' && !result?.ok && (
          <button className="btn-ghost" onClick={run}>
            <Roll>
              <RotateCw className="h-4 w-4" /> Check again
            </Roll>
          </button>
        )}
      </div>

      {phase === 'watching' && (
        <div className="mt-4 flex flex-wrap items-center gap-3 rounded-md border border-line bg-ink p-3 text-sm">
          <Loader2 className="h-4 w-4 shrink-0 animate-spin text-guard" />
          <span className="flex-1">
            Watching for its first lookup… {left}s left. Open any website or app on the device meanwhile.
          </span>
          <button className="btn-ghost" onClick={() => (stopped.current = true)}>
            <Roll>Stop</Roll>
          </button>
        </div>
      )}

      {phase === 'result' && result?.ok && (
        <div className="mt-4 flex gap-2 rounded-md bg-guard/10 p-3 text-sm text-guard" data-testid="done-check-result">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /> {result.message}
        </div>
      )}

      {phase === 'result' && result && !result.ok && (
        <div className="mt-4 rounded-md bg-warn/10 p-3 text-sm" data-testid="done-check-result">
          <div className="flex gap-2 text-warn">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /> {result.message}
          </div>
          {result.tips.length > 0 && (
            <ul className="mt-2 grid list-disc gap-1 pl-10 text-white/90">
              {result.tips.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          )}
          {result.link_ip && (
            <div className="mt-3 pl-6">
              <button className="btn-primary" disabled={linking} onClick={() => link(result.link_ip!)}>
                <Roll>
                  {linking ? <Loader2 className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />} I'm on my home WiFi: link{' '}
                  {result.link_ip}
                </Roll>
              </button>
              <p className="mt-1 text-xs text-muted">Only if this page is open on your home WiFi, not mobile data.</p>
            </div>
          )}
          {aiHelp && <p className="mt-2 pl-6 text-xs text-muted">Or paste a screenshot of the saved settings above and the AI will check them.</p>}
        </div>
      )}

      {problem && <div className="mt-3 text-sm text-danger">{problem}</div>}
    </div>
  )
}
