/*
 * Chart colours. Every set below was run through the data-viz palette validator against the card
 * surface (#1b1b1b): lightness band, chroma floor, colour-blind separation of neighbours, contrast.
 * Device colours follow the device's creation order, never its rank, so a filter never repaints a device.
 */
export const SERIES = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#0ca30c', '#9085e9', '#e66767']
export const DATA = { allowed: '#3987e5', blocked: '#e0604a', cached: '#199e70' } as const
export const TYPE_COLORS = ['#3987e5', '#199e70', '#c98500', '#d55181', '#9085e9']
export const UPSTREAM_COLORS = ['#3987e5', '#c98500', '#d55181']

export const fmt = (n: number) => n.toLocaleString()

/** Readable resolver names for the "answered by" split. */
export function resolverName(raw: string): string {
  if (raw === 'blocked') return 'Blocked'
  if (raw === 'cache') return 'Answered from cache'
  const host = raw.replace(/^[a-z]+:\/\//, '').replace(/[/#].*$/, '').replace(/:\d+$/, '')
  const who = /^1\.[01]\.[01]\.1$|cloudflare/.test(host)
    ? 'Cloudflare'
    : /^8\.8\.[48]\.[48]$|dns\.google/.test(host)
      ? 'Google'
      : /^9\.9\.9\.9$|quad9/.test(host)
        ? 'Quad9'
        : null
  return who ? `${who} (${host})` : host
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
export const clock = (ms: number) => timeFmt.format(ms)
