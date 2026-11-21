import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { timeAgo } from '../components/ui'

export default function Settings() {
  const qc = useQueryClient()
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15000 })
  const patch = useMutation({
    mutationFn: api.patchSettings,
    onSuccess: (s) => qc.setQueryData(['settings'], s),
  })
  const s = settings.data
  const h = health.data
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-bold">Settings</h1>

      <div className="card mt-5 flex items-center gap-4 p-5">
        <div className="flex-1">
          <div className="font-semibold">Auto-mode</div>
          <div className="text-sm text-muted">
            Block high-confidence ads, trackers and threats without asking. You get a notification and can undo any time.
          </div>
        </div>
        <button
          role="switch"
          aria-checked={!!s?.auto_mode}
          onClick={() => s && patch.mutate({ auto_mode: !s.auto_mode })}
          className={`relative h-7 w-12 rounded-full transition ${s?.auto_mode ? 'bg-guard' : 'bg-panel-2'}`}
        >
          <span className={`absolute top-1 h-5 w-5 rounded-full bg-white transition ${s?.auto_mode ? 'left-6' : 'left-1'}`} />
        </button>
      </div>

      <h2 className="mt-8 text-lg font-semibold">System status</h2>
      <div className="card mt-3 divide-y divide-line text-sm">
        <Row label="DNS gateway" ok={h?.adguard.ok} detail={h?.adguard.ok ? `AdGuard Home ${h.adguard.version ?? ''}` : h?.adguard.error} />
        <Row
          label="Query-log poller"
          ok={!!h && !h.poller.error}
          detail={h?.poller.error ?? `every ${h?.poller.interval_s}s · last ${timeAgo(h?.poller.last_poll ?? null)}`}
        />
        <Row label="Rule sync" ok={!!h && !h.sync.dirty} detail={h?.sync.error ?? `last push ${timeAgo(h?.sync.last_ok ?? null)}`} />
        <Row
          label="Blocklists"
          ok={!!h && h.lists.loaded_from !== 'empty'}
          detail={h ? `${Object.values(h.lists.lists).reduce((a, b) => a + b, 0).toLocaleString()} domains · ${h.lists.tracker_radar_entries} Tracker Radar · ${h.lists.loaded_from}` : ''}
        />
        <Row
          label="AI explainer"
          ok={!!h?.llm.enabled && !h.llm.error}
          detail={h?.llm.enabled ? (h.llm.error ?? `${h.llm.model} · ${h.domains_explained} domains explained`) : 'No GEMINI_API_KEY: using curated + list-based text'}
        />
        <Row
          label="Home companion (TV setup)"
          ok={!!h?.companion.connected}
          detail={h?.companion.enabled ? (h.companion.error ?? `${h.companion.tools?.length ?? 0} tools`) : 'disabled'}
        />
        {!!h?.forwarder?.devices && (
          <Row
            label="Laptop DNS relay (locked TVs)"
            ok={h.forwarder.running}
            detail={
              h.forwarder.running
                ? `${h.forwarder.devices} TV${h.forwarder.devices > 1 ? 's' : ''} → ${h.forwarder.listen_ip}:53. Keep this laptop on.`
                : (h.forwarder.error ?? 'stopped')
            }
          />
        )}
      </div>

      {s && (
        <div className="mt-6 text-xs text-muted">
          DNS hostname: <span className="font-mono">{s.dns_hostname}</span>
          {s.vm_ip && (
            <>
              {' '}· server IP <span className="font-mono">{s.vm_ip}</span>
            </>
          )}
          <br />
          Privacy: domain logs auto-delete after 7 days. Only domain names and public facts are ever sent to the AI.
        </div>
      )}
    </div>
  )
}

function Row({ label, ok, detail }: { label: string; ok?: boolean; detail?: string | null }) {
  return (
    <div className="flex items-center gap-3 p-4">
      <span className={`h-2.5 w-2.5 rounded-full ${ok ? 'bg-guard' : 'bg-warn'}`} />
      <span className="w-48 shrink-0 font-medium">{label}</span>
      <span className="min-w-0 flex-1 truncate text-muted">{detail}</span>
    </div>
  )
}
