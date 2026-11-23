import { useMutation } from '@tanstack/react-query'
import { CheckCircle2, ImagePlus, Loader2, ScanText, TriangleAlert, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type ScreenAnswer } from '../api'
import { shrink } from '../lib/image'
import { CopyField, Roll } from './ui'

interface Turn {
  image: string
  question: string
  answer: ScreenAnswer
}

/**
 * "Stuck? Show QuietNet your screen": paste (Ctrl+V anywhere on the page), drop or pick a screenshot or photo,
 * and the AI says what to do next on that screen. Each new screenshot carries on from the earlier answers.
 */
export default function ScreenshotHelp({
  deviceId,
  isp,
  router,
  title = 'Stuck? Show QuietNet your screen',
  intro = "Paste a screenshot, or take a photo of the TV or router screen. The AI reads it and tells you exactly what to tap next.",
}: {
  deviceId?: string
  isp?: string
  router?: string
  title?: string
  intro?: string
}) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [pending, setPending] = useState<string | null>(null)
  const [question, setQuestion] = useState('')
  const [problem, setProblem] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  const take = useCallback(async (file: Blob | null | undefined) => {
    if (!file) return
    setProblem(null)
    if (!file.type.startsWith('image/')) {
      setProblem("That isn't a picture. Paste or choose a screenshot or photo.")
      return
    }
    try {
      setPending(await shrink(file))
    } catch {
      setProblem("Couldn't open that picture. Try a PNG or JPEG screenshot.")
    }
  }, [])

  // Ctrl+V / Cmd+V anywhere on the page while this panel is showing
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const item = [...(e.clipboardData?.items ?? [])].find((i) => i.kind === 'file' && i.type.startsWith('image/'))
      if (!item) return
      e.preventDefault()
      void take(item.getAsFile())
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [take])

  const ask = useMutation({
    mutationFn: (image: string) =>
      api.screenshotHelp({
        image,
        device_id: deviceId,
        isp,
        router,
        question: question.trim(),
        history: turns.map((t) => ({ question: t.question, screen: t.answer.screen, steps: t.answer.steps })),
      }),
    onSuccess: (answer, image) => {
      setTurns((t) => [...t, { image, question: question.trim(), answer }])
      setPending(null)
      setQuestion('')
    },
  })
  const send = () => pending && !ask.isPending && ask.mutate(pending)

  return (
    <div className="card p-5" data-testid="screenshot-help">
      <div className="flex items-center gap-2 font-display text-lg">
        <ScanText className="h-5 w-5 text-guard" /> {title}
      </div>
      <p className="mt-1 text-sm text-muted">{intro}</p>

      {turns.map((t, i) => (
        <AnswerCard key={i} turn={t} />
      ))}

      {pending ? (
        <div className="mt-4 grid gap-3 sm:grid-cols-[176px_1fr]">
          <img src={pending} alt="Your screenshot" className="max-h-44 w-full rounded-md border border-line bg-white object-contain" />
          <div className="grid content-start gap-2">
            <input
              className="input"
              autoFocus
              maxLength={300}
              placeholder="What are you stuck on? (optional)"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && send()}
            />
            <div className="flex flex-wrap gap-2">
              <button className="btn-primary" onClick={send} disabled={ask.isPending}>
                <Roll>
                  {ask.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <ScanText className="h-4 w-4" />}
                  {ask.isPending ? 'Reading your screen…' : 'Read this screen'}
                </Roll>
              </button>
              <button className="btn-ghost" onClick={() => setPending(null)} disabled={ask.isPending}>
                <Roll>
                  <X className="h-4 w-4" /> Remove
                </Roll>
              </button>
            </div>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            void take(e.dataTransfer.files[0])
          }}
          className={`mt-4 flex w-full flex-col items-center gap-1 rounded-md border border-dashed px-4 py-6 text-center text-sm transition ${
            dragging ? 'border-guard bg-guard/10' : 'border-line hover:border-line-strong'
          }`}
        >
          <ImagePlus className="h-6 w-6 text-brand-soft" />
          <span className="font-medium">{turns.length ? 'Paste the next screen' : 'Paste a screenshot here'}</span>
          <span className="text-xs text-muted">Ctrl+V to paste, drop a picture, or tap to choose one or take a photo</span>
        </button>
      )}
      <input
        ref={fileInput}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(e) => {
          void take(e.target.files?.[0])
          e.target.value = ''
        }}
      />

      {(problem || ask.isError) && <div className="mt-3 text-sm text-danger">{problem ?? (ask.error as Error).message}</div>}
      <p className="mt-3 text-xs text-muted">
        Cover any passwords first. The picture goes to QuietNet's AI model to be read and isn't saved.
      </p>
    </div>
  )
}

function AnswerCard({ turn }: { turn: Turn }) {
  const a = turn.answer
  return (
    <div className="mt-4 rounded-md border border-line bg-ink p-4 text-sm" data-testid="screenshot-answer">
      <div className="flex gap-3">
        <img src={turn.image} alt="" className="h-14 w-20 shrink-0 rounded border border-line bg-white object-cover" />
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 font-medium">
            {a.done && <CheckCircle2 className="h-4 w-4 shrink-0 text-guard" />}
            {a.screen}
          </div>
          {turn.question && <div className="mt-0.5 text-xs text-muted">You asked: {turn.question}</div>}
        </div>
      </div>
      {a.reply && <p className={`mt-3 ${a.done ? 'text-guard' : 'text-white'}`}>{a.reply}</p>}
      {!a.on_track && <div className="mt-2 text-xs text-warn">This isn't the right page yet. Here's how to get there:</div>}
      {a.fill.length > 0 && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {a.fill.map((f) => (
            <CopyField key={f.field} label={f.field} value={f.value} />
          ))}
        </div>
      )}
      {a.steps.length > 0 && (
        <ol className="mt-3 grid gap-2">
          {a.steps.map((s, i) => (
            <li key={i} className="flex gap-3">
              <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full border border-line font-display text-xs text-white">{i + 1}</span>
              <span>{s}</span>
            </li>
          ))}
        </ol>
      )}
      {a.warning && (
        <div className="mt-3 flex gap-2 rounded-md bg-warn/10 p-3 text-warn">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /> {a.warning}
        </div>
      )}
      {a.next && !a.done && <div className="mt-3 text-xs text-muted">Still stuck? Paste a screenshot of {a.next}.</div>}
    </div>
  )
}
