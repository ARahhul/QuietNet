import { CheckCircle2, Loader2, Search, ShieldQuestion, Wrench } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { apiUrl, bearer, withToken } from '../../api'
import { CopyField, Roll } from '../ui'

/** Event protocol streamed by GET /api/assistant/sessions/{id}/stream (SSE, JSON per message). */
export type AssistantEvent =
  | { type: 'message'; role: 'assistant' | 'user'; text: string }
  | { type: 'step'; text: string; status: 'running' | 'ok' | 'failed' }
  | { type: 'devices'; devices: { handle: string; label: string; platform: string; detail: string }[] }
  | { type: 'confirm_required'; call_id: string; prompt: string }
  | { type: 'input_required'; kind: 'text' | 'pairing_code'; prompt: string }
  | {
      type: 'done'
      outcome: 'connected' | 'guide' | 'failed'
      text: string
      device_id?: string
      guide?: { title: string; steps: string[]; values?: Record<string, string>; ai_suggested?: boolean }
    }
  | { type: 'error'; message: string }
  | { type: 'mode'; mode: 'agent' | 'guided'; note?: string }

async function post(path: string, body: unknown) {
  const r = await fetch(apiUrl(path), {
    method: 'POST',
    headers: { Authorization: `Bearer ${await bearer()}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail ?? `HTTP ${r.status}`)
  return r.json()
}

export default function AssistantPanel({ onConnected }: { onConnected?: () => void }) {
  const [desc, setDesc] = useState('')
  const [sid, setSid] = useState<string | null>(null)
  const [events, setEvents] = useState<AssistantEvent[]>([])
  const [reply, setReply] = useState('')
  const [answered, setAnswered] = useState<Set<string>>(new Set())
  const [err, setErr] = useState<string | null>(null)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!sid) return
    const es = new EventSource(withToken(`/assistant/sessions/${sid}/stream`))
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data) as AssistantEvent
      setEvents((xs) => [...xs, ev])
      if (ev.type === 'done') {
        es.close()
        if (ev.outcome === 'connected') onConnected?.()
      }
    }
    es.onerror = () => es.close()
    return () => es.close()
  }, [sid, onConnected])

  useEffect(() => {
    // scrollIntoView may return a Promise in newer browsers; never let it become the effect's cleanup.
    void bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [events])

  const start = async () => {
    setErr(null)
    setEvents([])
    try {
      const r = await post('/assistant/sessions', { description: desc })
      setSid(r.session_id)
    } catch (e) {
      setErr((e as Error).message)
    }
  }
  const send = async (text: string) => {
    if (!sid || !text.trim()) return
    setReply('') // the backend echoes the reply into the stream (with device names instead of handles)
    await post(`/assistant/sessions/${sid}/messages`, { text }).catch((e) => setErr(e.message))
  }
  const confirm = async (call_id: string, approve: boolean) => {
    setAnswered((s) => new Set(s).add(call_id))
    await post(`/assistant/sessions/${sid}/confirm`, { call_id, approve }).catch((e) => setErr(e.message))
  }

  const last = events[events.length - 1]
  const finished = last?.type === 'done'
  // Waiting for the user = the latest prompt/device list has no user reply after it yet.
  const lastTurn = [...events]
    .reverse()
    .find((e) => e.type === 'input_required' || e.type === 'devices' || (e.type === 'message' && e.role === 'user'))
  const waitingInput = lastTurn && lastTurn.type !== 'message' ? lastTurn : undefined
  // A "running" step is replaced by whatever step comes after it.
  const lastStepIdx = events.reduce((acc, e, i) => (e.type === 'step' ? i : acc), -1)
  const visible = events.filter((e, i) => !(e.type === 'step' && e.status === 'running' && i !== lastStepIdx))

  if (!sid) {
    return (
      <div className="mt-3 grid gap-2">
        <div className="flex gap-2">
          <input
            className="input"
            placeholder="e.g. the LG TV in the living room"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && desc.trim() && start()}
          />
          <button className="btn-primary" disabled={!desc.trim()} onClick={start}>
            <Roll><Search className="h-4 w-4" /> Find it</Roll>
          </button>
        </div>
        <div className="text-[11px] text-muted">
          QuietNet scans only your home WiFi. Device names and models (not IP or MAC addresses) are shared with the AI. Nothing on your TV
          changes without your OK.
        </div>
        {err && <div className="text-sm text-danger">{err}</div>}
      </div>
    )
  }

  return (
    <div className="mt-3 grid gap-2 text-sm">
      {visible.map((ev, i) => {
        switch (ev.type) {
          case 'mode':
            return ev.mode === 'guided' ? (
              <div key={i} className="text-[11px] text-muted">
                Step-by-step mode{ev.note ? `: ${ev.note}` : ''}
              </div>
            ) : null
          case 'message':
            return (
              <div key={i} className={`whitespace-pre-line ${ev.role === 'user' ? 'ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-white px-3.5 py-2 text-ink' : 'max-w-[90%] rounded-2xl rounded-bl-md bg-panel-2 px-3.5 py-2'}`}>
                {ev.text}
              </div>
            )
          case 'step':
            return (
              <div key={i} className="flex items-center gap-2 text-xs text-muted">
                {ev.status === 'running' ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : ev.status === 'ok' ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-guard" />
                ) : (
                  <ShieldQuestion className="h-3.5 w-3.5 text-warn" />
                )}
                {ev.text}
              </div>
            )
          case 'devices':
            return (
              <div key={i} className="grid gap-2">
                {ev.devices.map((d) => (
                  <button key={d.handle} disabled={finished || !waitingInput} onClick={() => send(`It's ${d.handle}`)} className="card card-link group flex items-center gap-3 p-3 text-left">
                    <div className="flex-1">
                      <div className="font-medium">{d.label}</div>
                      <div className="text-xs text-muted">
                        {d.platform} · {d.detail}
                      </div>
                    </div>
                    <span className="underline-slide text-xs text-guard">This one</span>
                  </button>
                ))}
              </div>
            )
          case 'confirm_required':
            return (
              <div key={i} className="rounded-md border border-warn/50 bg-warn/10 p-3">
                <div>{ev.prompt}</div>
                {!answered.has(ev.call_id) && (
                  <div className="mt-2 flex gap-2">
                    <button className="btn-primary px-3 py-1.5" onClick={() => confirm(ev.call_id, true)}>
                      <Roll>Allow</Roll>
                    </button>
                    <button className="btn-ghost px-3 py-1.5" onClick={() => confirm(ev.call_id, false)}>
                      <Roll>Don't</Roll>
                    </button>
                  </div>
                )}
              </div>
            )
          case 'input_required':
            return (
              <div key={i} className="max-w-[90%] rounded-2xl rounded-bl-md bg-panel-2 px-3.5 py-2">
                {ev.prompt}
              </div>
            )
          case 'done':
            return (
              <div key={i} className={`card p-4 ${ev.outcome === 'connected' ? 'border-guard/60' : ''}`}>
                <div className="flex items-center gap-2 font-display text-lg">
                  {ev.outcome === 'connected' ? <CheckCircle2 className="h-5 w-5 text-guard" /> : <Wrench className="h-5 w-5 text-guard" />}
                  {ev.outcome === 'connected' ? 'Connected' : ev.guide?.title ?? 'Next steps'}
                </div>
                <p className="mt-1 text-body">{ev.text}</p>
                {ev.guide && (
                  <>
                    {ev.guide.values &&
                      Object.entries(ev.guide.values).map(([k, v]) => <div key={k} className="mt-2"><CopyField label={k} value={v} /></div>)}
                    <ol className="mt-3 list-decimal space-y-1 pl-5">
                      {ev.guide.steps.map((s, j) => (
                        <li key={j}>{s}</li>
                      ))}
                    </ol>
                    {ev.guide.ai_suggested && <div className="mt-2 text-[11px] text-muted">Some steps are AI-suggested: menu names may differ.</div>}
                  </>
                )}
              </div>
            )
          case 'error':
            return (
              <div key={i} className="text-danger">
                {ev.message}
              </div>
            )
        }
      })}
      {!finished && last && last.type !== 'confirm_required' && !waitingInput && (
        <div className="flex items-center gap-2 text-xs text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> working…
        </div>
      )}
      {!finished && (
        <div className="flex gap-2">
          <input
            className="input"
            placeholder={waitingInput?.type === 'input_required' && waitingInput.kind === 'pairing_code' ? '6-digit code and port' : 'Reply (e.g. "done")'}
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && send(reply)}
          />
          <button className="btn-ghost" onClick={() => send(reply)}>
            <Roll>Send</Roll>
          </button>
        </div>
      )}
      {err && <div className="text-danger">{err}</div>}
      <div ref={bottom} />
    </div>
  )
}
