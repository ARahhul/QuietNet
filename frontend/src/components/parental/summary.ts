import type { Bedtime, ParentalNow, ParentalProfile, ParentalService } from '../../api'

export const NIGHTS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

export function nightsText(nights: number[]): string {
  const s = [...nights].sort().join(',')
  if (s === '0,1,2,3,4') return 'school nights (Sun–Thu)'
  if (s === '0,1,2,3,4,5,6') return 'every night'
  if (s === '5,6') return 'Fri and Sat'
  return nights.map((n) => NIGHTS[n].slice(0, 3)).join(', ')
}

export const overnight = (b: Bedtime) => b.start > b.end

export type ById = Map<string, ParentalService>

const appNames = (ids: string[], byId: ById, max = 3) => {
  const names = ids.map((id) => byId.get(id)?.name ?? id)
  return names.length > max ? `${names.slice(0, max).join(', ')} and ${names.length - max} more` : names.join(', ')
}

/** One line on what a layer blocks */
export function describe(p: ParentalProfile, byId: ById): string {
  const parts: string[] = []
  if (p.adult) parts.push('Adult sites')
  if (p.safe_search) parts.push('SafeSearch')
  if (p.games) parts.push('Games')
  if (p.apps.length) parts.push(appNames(p.apps, byId, 2))
  if (p.bedtime && p.bedtime.nights.length && (p.bedtime_internet_off || p.bedtime_apps.length || p.bedtime_games)) {
    const at = [...(p.bedtime_games && !p.games ? ['games'] : []), ...(p.bedtime_apps.length ? [appNames(p.bedtime_apps, byId, 2)] : [])]
    const what = p.bedtime_internet_off ? 'internet off' : at.join(', ')
    parts.push(`Bedtime ${p.bedtime.start}–${p.bedtime.end} ${nightsText(p.bedtime.nights)}: ${what}`)
  }
  return parts.join(' · ') || 'Nothing blocked yet'
}

/** What's blocked on a device right now */
export function nowText(now: ParentalNow, byId: ById): string {
  const parts: string[] = []
  if (now.internet_off) parts.push('Bedtime: internet off')
  else if (now.apps.length || now.games) {
    const what = [...(now.games ? ['games'] : []), ...(now.apps.length ? [appNames(now.apps, byId)] : [])].join(', ')
    parts.push(`${now.bedtime ? 'Bedtime: ' : ''}${what} blocked`)
  } else if (now.bedtime) parts.push('Bedtime')
  if (now.adult) parts.push('adult sites blocked')
  if (now.safe_search) parts.push('SafeSearch on')
  if (!parts.length) return 'Nothing blocked right now'
  const s = parts.join(' · ')
  return s[0].toUpperCase() + s.slice(1)
}
