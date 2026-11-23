import { Moon } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Bedtime, ParentalProfile, ParentalService, ProfileChange } from '../../api'
import { FilterChip, Toggle } from '../ui'
import AppPicker from './AppPicker'
import { NIGHTS, overnight } from './summary'

const SCHOOL_NIGHTS: Bedtime = { nights: [0, 1, 2, 3, 4], start: '22:00', end: '07:00' }
const SAVE_AFTER_MS = 600 // changes made in a burst (picking several apps) go out as one save

function Row({ title, children, control }: { title: string; children: ReactNode; control: ReactNode }) {
  return (
    <div className="flex items-start gap-4">
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-white">{title}</div>
        <div className="mt-0.5 text-xs leading-relaxed text-muted">{children}</div>
      </div>
      {control}
    </div>
  )
}


/**
 * Edits one layer of parental rules. Changes show at once and are saved shortly after the last one, so picking
 * several apps makes one save, not one per tap.
 */
export default function ControlsEditor({
  profile,
  services,
  essentials,
  onSave,
  saving,
}: {
  profile: ParentalProfile
  services: ParentalService[]
  essentials: string[]
  onSave: (change: ProfileChange) => void
  saving: boolean
}) {
  const [draft, setDraft] = useState(profile)
  const pending = useRef<ProfileChange>({})
  const timer = useRef<number | undefined>(undefined)
  const [waiting, setWaiting] = useState(false) // changes not sent yet
  const save = useRef(onSave)
  useEffect(() => {
    save.current = onSave
  }, [onSave])

  // New data from the server: take it, unless the person is mid-change
  useEffect(() => {
    if (!Object.keys(pending.current).length) setDraft(profile)
  }, [profile])

  const flush = () => {
    window.clearTimeout(timer.current)
    const c = pending.current
    pending.current = {}
    setWaiting(false)
    if (Object.keys(c).length) save.current(c)
  }
  useEffect(() => flush, []) // leaving the page saves what's waiting

  const change = (c: ProfileChange) => {
    setDraft((d) => ({ ...d, ...c }))
    pending.current = { ...pending.current, ...c }
    setWaiting(true)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(flush, SAVE_AFTER_MS)
  }

  const bt = draft.bedtime
  const setBedtime = (b: Partial<Bedtime>) => {
    if (!bt) return
    const next = { ...bt, ...b }
    if (next.start === next.end) return // the server refuses it; keep the last good value
    change({ bedtime: next })
  }

  return (
    <div className="grid gap-5">
      <Row
        title="Block adult sites"
        control={<Toggle checked={draft.adult} onChange={(adult) => change({ adult })} label="Block adult sites" />}
      >
        Porn and other adult sites, using AdGuard’s adult-site filter.
      </Row>
      <Row
        title="Force SafeSearch"
        control={<Toggle checked={draft.safe_search} onChange={(safe_search) => change({ safe_search })} label="Force SafeSearch" />}
      >
        Hides explicit results on Google, Bing and DuckDuckGo, and turns on YouTube’s restricted mode.
      </Row>
      <Row
        title="Block games"
        control={<Toggle checked={draft.games} onChange={(games) => change({ games })} label="Block games" />}
      >
        Roblox, Fortnite, Minecraft and the other games AdGuard knows, plus any new game these devices open: QuietNet asks
        the AI about each new site they visit, once, and blocks it within a minute if it’s a game.
      </Row>
      <div>
        <div className="text-sm font-medium text-white">Apps blocked all the time</div>
        <div className="mt-2">
          <AppPicker services={services} selected={draft.apps} onChange={(apps) => change({ apps })} empty="None." />
        </div>
      </div>

      <div className="border-t border-line pt-5">
        <Row
          title="Bedtime"
          control={
            <Toggle checked={!!bt} onChange={(on) => change({ bedtime: on ? SCHOOL_NIGHTS : null })} label="Bedtime" />
          }
        >
          <Moon className="mr-1 inline h-3 w-3 align-[-1px]" />
          Block more at night: chosen apps, or the whole internet.
        </Row>
        {bt && (
          <div className="mt-4 grid gap-4 rounded-md border border-line bg-ink p-4">
            <div>
              <div className="text-xs text-muted">Starts on</div>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {NIGHTS.map((n, i) => (
                  <FilterChip
                    key={n}
                    active={bt.nights.includes(i)}
                    onClick={() =>
                      setBedtime({ nights: bt.nights.includes(i) ? bt.nights.filter((x) => x !== i) : [...bt.nights, i].sort() })
                    }
                  >
                    <span aria-label={n}>{n.slice(0, 3)}</span>
                  </FilterChip>
                ))}
              </div>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <label className="grid gap-1 text-xs text-muted">
                From
                <input type="time" className="input w-32" value={bt.start} onChange={(e) => e.target.value && setBedtime({ start: e.target.value })} />
              </label>
              <label className="grid gap-1 text-xs text-muted">
                Until
                <input type="time" className="input w-32" value={bt.end} onChange={(e) => e.target.value && setBedtime({ end: e.target.value })} />
              </label>
              <span className="pb-2 text-xs text-muted">
                {!bt.nights.length ? 'Pick at least one night.' : overnight(bt) ? 'until the next morning' : 'the same day'}
              </span>
            </div>

            <Row
              title="Internet off"
              control={
                <Toggle
                  checked={draft.bedtime_internet_off}
                  onChange={(bedtime_internet_off) => change({ bedtime_internet_off })}
                  label="Internet off at bedtime"
                />
              }
            >
              Everything stops except calls and messages on WhatsApp, the clock and notifications (
              {essentials.slice(0, 3).join(', ')}
              {essentials.length > 3 ? ` and ${essentials.length - 3} more` : ''}).
            </Row>
            {!draft.bedtime_internet_off && !draft.games && (
              <Row
                title="Games at bedtime"
                control={
                  <Toggle
                    checked={draft.bedtime_games}
                    onChange={(bedtime_games) => change({ bedtime_games })}
                    label="Block games at bedtime"
                  />
                }
              >
                The same games, blocked only during bedtime.
              </Row>
            )}
            {!draft.bedtime_internet_off && (
              <div>
                <div className="text-sm font-medium text-white">Apps blocked at bedtime</div>
                <div className="mt-2">
                  <AppPicker
                    services={services}
                    selected={draft.bedtime_apps}
                    onChange={(bedtime_apps) => change({ bedtime_apps })}
                    empty="None yet: add the apps that keep them up."
                  />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
      <div className="text-right text-[11px] text-muted" aria-live="polite">
        {saving || waiting ? 'Saving…' : 'Saved'}
      </div>
    </div>
  )
}
