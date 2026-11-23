import { Download } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import { useState } from 'react'
import { withTicket, type DeviceType, type SetupGuide } from '../api'
import { CopyField, Roll } from './ui'

export default function SetupSteps({
  deviceId,
  guide,
  brand,
  type,
}: {
  deviceId: string
  guide: SetupGuide
  brand: string | null
  type?: DeviceType
}) {
  const v = guide.values
  const apple = (brand ?? '').toLowerCase() === 'apple'
  const router = type === 'router'
  const [profileError, setProfileError] = useState<string | null>(null)
  // Safari installs a profile from a link it follows, which can't carry the sign-in: follow one with a short ticket
  const downloadProfile = () => {
    setProfileError(null)
    withTicket(`/devices/${deviceId}/profile.mobileconfig`)
      .then((url) => {
        window.location.href = url
      })
      .catch((e) => setProfileError((e as Error).message))
  }
  return (
    <div className="grid gap-4">
      {guide.method === 'dot' && router && (
        <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
          <CopyField label="TLS hostname" value={v.dot_hostname} />
          <CopyField label="Server address" value={v.server_ip} />
        </div>
      )}
      {guide.method === 'dot' && !router && (
        <div className="grid items-center gap-4 sm:grid-cols-[1fr_auto]">
          <CopyField label="Private DNS provider hostname" value={v.dot_hostname} />
          <div className="mx-auto rounded-md bg-white p-2">
            <QRCodeSVG value={v.dot_hostname} size={96} />
          </div>
        </div>
      )}
      {guide.method === 'doh' && apple && (
        <div className="grid gap-1">
          <button type="button" className="btn-primary w-fit" onClick={downloadProfile}>
            <Roll><Download className="h-4 w-4" /> Download profile</Roll>
          </button>
          {profileError && <p className="text-xs text-danger">{profileError}</p>}
        </div>
      )}
      {guide.method === 'doh' && <CopyField label="DNS-over-HTTPS URL" value={v.doh_url} />}
      {guide.method === 'doh' && !apple && v.server_ip && <CopyField label="DNS server IP" value={v.server_ip} />}
      {(guide.method === 'ipv4_demo' || guide.method === 'ipv6') && <CopyField label="DNS server" value={v.server_ip} />}
      {guide.method === 'adb' && v.adb_commands?.map((c) => <CopyField key={c} value={c} />)}
      {guide.method === 'forwarder' && v.forwarder_ip && <CopyField label="DNS server for the TV (this laptop)" value={v.forwarder_ip} />}

      <ol className="grid gap-2 text-sm">
        {guide.steps.map((s, i) => (
          <li key={i} className="flex gap-3">
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full border border-line font-display text-xs text-white">{i + 1}</span>
            <span className={s.startsWith('adb ') ? 'font-mono text-xs' : ''}>{s}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}
