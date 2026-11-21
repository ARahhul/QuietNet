import { Download } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import { withToken, type SetupGuide } from '../api'
import { CopyField } from './ui'

export default function SetupSteps({ deviceId, guide, brand }: { deviceId: string; guide: SetupGuide; brand: string | null }) {
  const v = guide.values
  const apple = (brand ?? '').toLowerCase() === 'apple'
  return (
    <div className="grid gap-4">
      {guide.method === 'dot' && (
        <div className="grid items-center gap-4 sm:grid-cols-[1fr_auto]">
          <CopyField label="Private DNS provider hostname" value={v.dot_hostname} />
          <div className="mx-auto rounded-xl bg-white p-2">
            <QRCodeSVG value={v.dot_hostname} size={96} />
          </div>
        </div>
      )}
      {guide.method === 'doh' && apple && (
        <a className="btn-primary w-fit" href={withToken(`/devices/${deviceId}/profile.mobileconfig`)}>
          <Download className="h-4 w-4" /> Download profile
        </a>
      )}
      {guide.method === 'doh' && <CopyField label="DNS-over-HTTPS URL" value={v.doh_url} />}
      {guide.method === 'doh' && !apple && v.server_ip && <CopyField label="DNS server IP" value={v.server_ip} />}
      {(guide.method === 'ipv4_demo' || guide.method === 'ipv6') && <CopyField label="DNS server" value={v.server_ip} />}
      {guide.method === 'adb' && v.adb_commands?.map((c) => <CopyField key={c} value={c} />)}
      {guide.method === 'forwarder' && v.forwarder_ip && <CopyField label="DNS server for the TV (this laptop)" value={v.forwarder_ip} />}

      <ol className="grid gap-2 text-sm">
        {guide.steps.map((s, i) => (
          <li key={i} className="flex gap-3">
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-panel-2 text-xs text-muted">{i + 1}</span>
            <span className={s.startsWith('adb ') ? 'font-mono text-xs' : ''}>{s}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}
