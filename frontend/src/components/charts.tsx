import { Table2, ChartColumn } from 'lucide-react'
import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { clock, fmt } from '../lib/viz'

const M = { l: 44, r: 8, t: 10, b: 26 } // plot margins: y labels left, time labels below
const GRID = '#2a2a2a'
const BASELINE = '#3d3d3d'
const GAP = 2 // surface gap between touching marks
const MAX_BAR = 24

const compact = (n: number, top: number) => (top >= 10000 ? `${Math.round(n / 1000)}K` : n.toLocaleString())
const pct = (n: number, total: number) => (total ? `${n / total < 0.01 && n > 0 ? '<1' : Math.round((n / total) * 100)}%` : '0%')

function niceMax(v: number): { max: number; step: number } {
  if (v <= 0) return { max: 4, step: 1 }
  const rough = v / 4
  const mag = 10 ** Math.floor(Math.log10(rough))
  const norm = rough / mag
  const mult = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 && mag >= 10 ? 2.5 : norm <= 5 ? 5 : 10
  const step = Math.max(1, mult * mag)
  return { max: Math.ceil(v / step) * step, step }
}

/** Rectangle with only its top corners rounded (data end), square at the baseline. */
function topRounded(x: number, y: number, w: number, h: number, r: number) {
  r = Math.min(r, w / 2, h)
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setW(Math.floor(e.contentRect.width)))
    ro.observe(el)
    setW(Math.floor(el.getBoundingClientRect().width))
    return () => ro.disconnect()
  }, [])
  return [ref, w] as const
}

export interface Series {
  key: string
  label: string
  color: string
  values: number[]
}

/** Sum neighbouring buckets so a bar never gets thinner than ~3px on narrow screens. */
function regroup(series: Series[], bucketMs: number, plotW: number) {
  const n = series[0]?.values.length ?? 0
  const k = Math.max(1, Math.ceil((n * 5) / Math.max(plotW, 1)))
  const m = Math.ceil(n / k)
  const merged = series.map((s) => ({
    ...s,
    values: Array.from({ length: m }, (_, i) => s.values.slice(i * k, i * k + k).reduce((a, b) => a + b, 0)),
  }))
  return { series: merged, bucketMs: bucketMs * k, n: m }
}

/**
 * Stacked columns over time, one column per bucket. Hover (or arrow keys when focused) reads a column:
 * every series at that time, values first. The table view in ChartCard carries the same numbers.
 */
export function StackedColumns({
  start,
  bucketMs,
  series: raw,
  height = 220,
  label,
}: {
  start: number
  bucketMs: number
  series: Series[]
  height?: number
  label: string
}) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [active, setActive] = useState<number | null>(null)
  const plotW = Math.max(0, width - M.l - M.r)
  const plotH = height - M.t - M.b
  const { series, bucketMs: bMs, n } = useMemo(() => regroup(raw, bucketMs, plotW), [raw, bucketMs, plotW])
  const totals = useMemo(() => Array.from({ length: n }, (_, i) => series.reduce((a, s) => a + s.values[i], 0)), [series, n])
  const { max, step } = niceMax(Math.max(0, ...totals))
  const slot = n ? plotW / n : 0
  const barW = Math.max(1, Math.min(MAX_BAR, slot - GAP))
  const y = (v: number) => M.t + plotH - (v / max) * plotH
  const span = n * bMs
  const xAt = (t: number) => M.l + ((t - start) / span) * plotW

  // x ticks on whole local minutes/hours, at least ~52px apart (a label is ~30px wide)
  const ticks = useMemo(() => {
    if (!plotW) return []
    const pxPerMin = plotW / (span / 60000)
    const stepMin = [5, 10, 15, 30, 60, 120, 180, 240, 360, 720].find((c) => c * pxPerMin >= 52) ?? 720
    const stepMs = stepMin * 60000
    const off = new Date(start).getTimezoneOffset() * 60000
    const out: number[] = []
    for (let t = Math.ceil((start - off) / stepMs) * stepMs + off; t < start + span; t += stepMs) {
      const x = M.l + ((t - start) / span) * plotW
      if (x >= M.l + 16 && x <= M.l + plotW - 16) out.push(t) // keep edge labels off the y axis and the card edge
    }
    return out
  }, [plotW, span, start])

  const yTicks: number[] = []
  for (let v = 0; v <= max; v += step) yTicks.push(v)

  const pick = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const i = Math.floor((e.clientX - box.left - M.l) / slot)
    setActive(i >= 0 && i < n ? i : null)
  }
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!n) return
    const last = totals.findLastIndex((t) => t > 0)
    if (e.key === 'ArrowLeft') setActive((a) => Math.max(0, (a ?? last + 1) - 1))
    else if (e.key === 'ArrowRight') setActive((a) => Math.min(n - 1, (a ?? last - 1) + 1))
    else if (e.key === 'Home') setActive(0)
    else if (e.key === 'End') setActive(n - 1)
    else if (e.key === 'Escape') setActive(null)
    else return
    e.preventDefault()
  }

  const tip = active !== null && width > 0 ? { i: active, cx: M.l + active * slot + slot / 2 } : null

  return (
    <div
      ref={ref}
      className="relative rounded-md outline-none focus-visible:ring-2 focus-visible:ring-white/40"
      tabIndex={0}
      role="group"
      aria-label={`${label}. Use the left and right arrow keys to read each column.`}
      onKeyDown={onKey}
      onBlur={() => setActive(null)}
    >
      <svg width={width} height={height} className="block touch-pan-y" onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => setActive(null)} aria-hidden>
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={M.l} x2={M.l + plotW} y1={y(v)} y2={y(v)} stroke={v === 0 ? BASELINE : GRID} strokeWidth={1} shapeRendering="crispEdges" />
            <text x={M.l - 8} y={y(v)} dy="0.32em" textAnchor="end" className="fill-muted text-[11px] tabular-nums">
              {compact(v, max)}
            </text>
          </g>
        ))}
        {ticks.map((t) => (
          <text key={t} x={xAt(t)} y={height - 6} textAnchor="middle" className="fill-muted text-[11px] tabular-nums">
            {clock(t)}
          </text>
        ))}
        {tip && <rect x={M.l + tip.i * slot} y={M.t} width={slot} height={plotH} fill="white" fillOpacity={0.05} />}
        {Array.from({ length: n }, (_, i) => {
          const x = M.l + i * slot + (slot - barW) / 2
          const top = series.reduce((t, s, j) => (s.values[i] > 0 ? j : t), -1)
          let cum = 0
          return (
            <g key={i}>
              {series.map((s, j) => {
                const v = s.values[i]
                if (!v) return null
                const y1 = y(cum + v)
                const y0 = y(cum) - (cum > 0 ? GAP : 0)
                cum += v
                const h = y0 - y1
                if (h <= 0.5) return null
                return j === top ? (
                  <path key={s.key} d={topRounded(x, y1, barW, h, 4)} fill={s.color} />
                ) : (
                  <rect key={s.key} x={x} y={y1} width={barW} height={h} fill={s.color} />
                )
              })}
            </g>
          )
        })}
      </svg>

      {tip && (
        <div
          role="status"
          className="pointer-events-none absolute top-2 z-10 min-w-40 rounded-md border border-line bg-ink/95 px-3 py-2 text-xs shadow-xl shadow-black/50"
          style={tip.cx > width / 2 ? { right: width - tip.cx + 12 } : { left: tip.cx + 12 }}
        >
          <div className="text-muted tabular-nums">
            {clock(start + tip.i * bMs)} to {clock(start + (tip.i + 1) * bMs)}
          </div>
          <div className="mt-1.5 grid gap-1">
            {[...series].reverse().map((s) => (
              <div key={s.key} className="flex items-center gap-2">
                <span className="h-0.5 w-3 rounded-full" style={{ background: s.color }} />
                <span className="font-medium text-white tabular-nums">{fmt(s.values[tip.i])}</span>
                <span className="truncate text-muted">{s.label}</span>
              </div>
            ))}
            {series.length > 1 && (
              <div className="mt-0.5 border-t border-line pt-1 text-muted">
                <span className="font-medium text-white tabular-nums">{fmt(totals[tip.i])}</span> in all
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/** The same numbers as StackedColumns, as a table (newest first, empty buckets skipped). */
export function BucketTable({ start, bucketMs, series }: { start: number; bucketMs: number; series: Series[] }) {
  const n = series[0]?.values.length ?? 0
  const rows = Array.from({ length: n }, (_, i) => n - 1 - i).filter((i) => series.some((s) => s.values[i] > 0))
  if (!rows.length) return <p className="py-8 text-center text-sm text-muted">No lookups in this window.</p>
  return (
    <div className="max-h-[260px] overflow-auto rounded-md border border-line">
      <table className="w-full text-left text-xs tabular-nums">
        <thead className="sticky top-0 bg-panel-2 text-muted">
          <tr>
            <th className="px-3 py-2 font-medium">Time</th>
            {series.map((s) => (
              <th key={s.key} className="px-3 py-2 text-right font-medium">
                {s.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map((i) => (
            <tr key={i}>
              <td className="px-3 py-1.5 text-body">
                {clock(start + i * bucketMs)} to {clock(start + (i + 1) * bucketMs)}
              </td>
              {series.map((s) => (
                <td key={s.key} className="px-3 py-1.5 text-right">
                  {fmt(s.values[i])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function Legend({ items }: { items: { label: string; color: string; value?: number }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-1.5 text-body">
          <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: it.color }} />
          {it.label}
          {it.value !== undefined && <span className="text-muted tabular-nums">{fmt(it.value)}</span>}
        </li>
      ))}
    </ul>
  )
}

/** A card holding one chart, with a switch to its table view. */
export function ChartCard({
  title,
  note,
  legend,
  table,
  children,
  dim,
}: {
  title: string
  note?: string
  legend?: ReactNode
  table?: ReactNode
  children: ReactNode
  dim?: boolean
}) {
  const [asTable, setAsTable] = useState(false)
  return (
    <section className="card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-lg leading-tight">{title}</h2>
          {note && <p className="mt-1 text-xs text-muted">{note}</p>}
        </div>
        {table && (
          <button
            className="flex items-center gap-1.5 rounded-full border border-line px-3 py-1 text-xs text-muted transition hover:border-line-strong hover:text-white"
            onClick={() => setAsTable((t) => !t)}
            aria-pressed={asTable}
          >
            {asTable ? <ChartColumn className="h-3.5 w-3.5" /> : <Table2 className="h-3.5 w-3.5" />}
            {asTable ? 'Chart' : 'Table'}
          </button>
        )}
      </div>
      {legend && <div className="mt-3">{legend}</div>}
      <div className={`mt-4 transition-opacity ${dim ? 'opacity-50' : ''}`}>{asTable && table ? table : children}</div>
    </section>
  )
}

/**
 * One whole split into parts: a thin segmented bar above a list that gives each part's name, count and share.
 * Parts past `max` fold into "Other".
 */
export function Breakdown({ parts, max = 5 }: { parts: { label: string; value: number; color: string }[]; max?: number }) {
  const shown = parts.filter((p) => p.value > 0)
  const folded =
    shown.length > max
      ? [...shown.slice(0, max - 1), { label: 'Other', value: shown.slice(max - 1).reduce((a, p) => a + p.value, 0), color: shown[max - 1].color }]
      : shown
  const total = folded.reduce((a, p) => a + p.value, 0)
  if (!total) return <p className="py-6 text-sm text-muted">No lookups in this window.</p>
  return (
    <div>
      <div className="flex h-2 gap-0.5 overflow-hidden rounded-full" role="img" aria-label={folded.map((p) => `${p.label} ${pct(p.value, total)}`).join(', ')}>
        {folded.map((p) => (
          <span key={p.label} title={`${p.label}: ${fmt(p.value)} (${pct(p.value, total)})`} style={{ flexGrow: p.value, background: p.color, minWidth: 2 }} />
        ))}
      </div>
      <ul className="mt-4 divide-y divide-line text-sm">
        {folded.map((p) => (
          <li key={p.label} className="flex items-center gap-3 py-2">
            <span className="h-2.5 w-2.5 shrink-0 rounded-[3px]" style={{ background: p.color }} />
            <span className="min-w-0 flex-1 truncate text-body">{p.label}</span>
            <span className="tabular-nums">{fmt(p.value)}</span>
            <span className="w-11 text-right text-xs text-muted tabular-nums">{pct(p.value, total)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Ranked rows, each with a thin bar scaled to the top row. One colour per list. */
export function RankedList({
  rows,
  color,
  empty,
}: {
  rows: { key: string; label: ReactNode; sub?: ReactNode; value: number; note?: ReactNode }[]
  color: string
  empty: string
}) {
  if (!rows.length) return <p className="py-6 text-sm text-muted">{empty}</p>
  const top = Math.max(1, ...rows.map((r) => r.value))
  return (
    <ol className="grid gap-3.5">
      {rows.map((r) => (
        <li key={r.key}>
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="min-w-0 truncate">{r.label}</span>
            <span className="shrink-0 tabular-nums">
              {fmt(r.value)}
              {r.note && <span className="ml-1.5 text-xs text-muted">{r.note}</span>}
            </span>
          </div>
          {r.sub && <div className="truncate text-xs text-muted">{r.sub}</div>}
          <div className="mt-1.5 h-1 rounded-full" style={{ width: `${Math.max(1.5, (r.value / top) * 100)}%`, background: color }} />
        </li>
      ))}
    </ol>
  )
}
