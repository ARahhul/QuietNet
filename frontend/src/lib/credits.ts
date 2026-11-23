/** Open-source software and public services QuietNet is built on, credited on the homepage footer and in Settings. */
export interface Credit {
  name: string
  url: string
  role: string
  license?: string
}

export const CREDITS: Credit[] = [
  {
    name: 'AdGuard Home',
    url: 'https://github.com/AdguardTeam/AdGuardHome',
    role: 'The DNS server on the cloud VM: answers every lookup over DoT and DoH, applies your block rules per device, and keeps the query log QuietNet reads.',
    license: 'GPL-3.0',
  },
  {
    name: 'Cloudflare 1.1.1.1 and Quad9',
    url: 'https://www.quad9.net',
    role: 'Upstream resolvers AdGuard Home asks, over TLS, for lookups that are allowed.',
  },
  {
    name: "Let's Encrypt and Certbot",
    url: 'https://letsencrypt.org',
    role: 'The free wildcard certificate that lets each device reach its own encrypted DNS address.',
    license: 'Certbot: Apache-2.0',
  },
  { name: 'Caddy', url: 'https://github.com/caddyserver/caddy', role: 'Optional HTTPS front for DNS-over-HTTPS in the hardened setup.', license: 'Apache-2.0' },
  {
    name: 'HaGeZi DNS blocklists',
    url: 'https://github.com/hagezi/dns-blocklists',
    role: "Ad, tracker, smart-TV (LG, Samsung, Amazon, Roku) and threat lists behind QuietNet's block list, which turns known ads away on the first lookup.",
    license: 'GPL-3.0',
  },
  {
    name: 'Tranco',
    url: 'https://tranco-list.eu',
    role: "A research ranking of the most visited sites. The top 10,000 that no list flags count as clean, so they never go to the AI.",
  },
  {
    name: 'DuckDuckGo Tracker Radar',
    url: 'https://github.com/duckduckgo/tracker-radar',
    role: 'Which company owns a tracking domain and what it collects.',
    license: 'CC BY-NC-SA 4.0',
  },
  {
    name: 'deSEC',
    url: 'https://desec.io',
    role: 'Free, privacy-focused DNS hosting for quietnet.in, and the DNS checks that issue its certificates.',
  },
  {
    name: 'Firebase Authentication and Hosting',
    url: 'https://firebase.google.com',
    role: 'Sign-in with Google or an emailed link, and hosting for this website.',
  },
  { name: 'FastAPI', url: 'https://github.com/fastapi/fastapi', role: 'The web framework QuietNet’s server is built on.', license: 'MIT' },
  {
    name: 'React, Tailwind CSS, TanStack Query and Lucide',
    url: 'https://react.dev',
    role: 'The libraries this dashboard is built with, and its icons.',
    license: 'MIT, ISC',
  },
  { name: 'Inter and Inter Tight', url: 'https://rsms.me/inter/', role: 'Typefaces by Rasmus Andersson.', license: 'SIL OFL 1.1' },
]
