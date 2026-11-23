import { useQueryClient } from '@tanstack/react-query'
import { Globe2, Loader2, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { api, type Device } from '../api'
import { Roll, timeAgo } from './ui'

/**
 * "Follow my home's address": plain DNS recognises the home by its public IP, which providers like ACT change on
 * every reconnect. With the router's Dynamic DNS name here, QuietNet re-links within a minute or two by itself.
 */
export default function HomeAddress({ device }: { device: Device }) {
  const qc = useQueryClient()
  const [host, setHost] = useState('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const st = device.ddns_status

  const save = async (value: string) => {
    setBusy(true)
    setProblem(null)
    try {
      await api.patchDevice(device.id, { ddns_host: value })
      setHost('')
      qc.invalidateQueries({ queryKey: ['device', device.id] })
      qc.invalidateQueries({ queryKey: ['devices'] })
      qc.invalidateQueries({ queryKey: ['setup', device.id] })
    } catch (e) {
      setProblem((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card p-5" data-testid="home-address">
      <div className="flex items-center gap-2 font-display text-lg">
        <Globe2 className="h-4 w-4 text-guard" /> Follow my home's address
      </div>
      {device.ddns_host ? (
        <>
          <p className="mt-1 text-sm text-muted">
            QuietNet follows <span className="font-mono text-white">{device.ddns_host}</span>
            {st?.ip && (
              <>
                {' '}→ <span className="font-mono text-white">{st.ip}</span>
              </>
            )}
            {st?.checked_at && <>, checked {timeAgo(st.checked_at)}</>}. When your provider gives your home a new address,
            QuietNet picks it up within a minute or two.
          </p>
          {st?.error && (
            <div className="mt-3 flex gap-2 rounded-md bg-warn/10 p-3 text-sm text-warn">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /> {st.error}
            </div>
          )}
          <button className="btn-ghost mt-3" disabled={busy} onClick={() => save('')}>
            <Roll>Stop following</Roll>
          </button>
        </>
      ) : (
        <>
          <p className="mt-1 text-sm text-muted">
            Your provider can give your home a new internet address whenever the router reconnects, and then QuietNet stops
            recognising it. Turn on <b className="text-white">Dynamic DNS</b> in the router (often Advanced → Network →
            Dynamic DNS; TP-Link DDNS and No-IP are free), then enter its hostname here, from your home WiFi.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <input
              className="input min-w-0 flex-1"
              placeholder="e.g. myhome.tplinkdns.com"
              value={host}
              onChange={(e) => setHost(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && host.trim() && save(host)}
            />
            <button className="btn-primary" disabled={busy || !host.trim()} onClick={() => save(host)}>
              <Roll>
                {busy && <Loader2 className="h-4 w-4 animate-spin" />} Follow it
              </Roll>
            </button>
          </div>
        </>
      )}
      {problem && <div className="mt-3 text-sm text-danger">{problem}</div>}
    </div>
  )
}
