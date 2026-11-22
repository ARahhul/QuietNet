"""Per-device connection instructions (SPEC §3) and the iOS/macOS DoH profile."""

import ipaddress
import uuid
from xml.sax.saxutils import escape

from .config import Settings

CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def dot_hostname(s: Settings, client_id: str) -> str:
    return f"{client_id}.{s.dns_hostname}"


def doh_url(s: Settings, client_id: str) -> str:
    return f"https://{s.dns_hostname}/dns-query/{client_id}"


def default_setup_method(type_: str, brand: str | None) -> str:
    b = (brand or "").lower()
    if type_ == "phone":
        return "doh" if b == "apple" else "dot"
    if type_ == "laptop":
        return "doh"
    if type_ == "tv":
        return "adb" if b in ("android", "google", "sony", "tcl", "xiaomi", "oneplus", "amazon") else "ipv4_demo"
    if type_ == "router":
        return "ipv4_demo"
    return "dot"


def is_cgnat(router_wan_ip: str | None, public_ip: str | None) -> bool | None:
    if not router_wan_ip:
        return None
    try:
        wan = ipaddress.ip_address(router_wan_ip)
    except ValueError:
        return None
    if wan.version == 4 and (wan in CGNAT_NET or wan.is_private):
        return True
    if public_ip and router_wan_ip != public_ip:
        return True
    return False


def setup_guide(s: Settings, device: dict, forwarder_ip: str | None = None) -> dict:
    cid = device["client_id"]
    host = dot_hostname(s, cid)
    url = doh_url(s, cid)
    vm = s.vm_ip or "<your QuietNet server IP>"
    method = device["setup_method"]
    values = {"dot_hostname": host, "doh_url": url, "server_ip": vm}
    brand = (device.get("brand") or "").lower()
    if method == "dot" and device.get("type") == "router":
        # Encrypted DNS on the router: one ClientID for the whole home, works behind CGNAT.
        if brand == "asus":
            steps = [
                "Open http://router.asus.com from a device on your WiFi and log in.",
                "Go to Advanced Settings → WAN → Internet Connection, and scroll to WAN DNS Setting.",
                "Set DNS Privacy Protocol to DNS-over-TLS (DoT) and DNS-over-TLS Profile to Strict.",
                f"Under DNS-over-TLS Server List, add: Address {vm}, TLS Port 853, TLS Hostname {host}. Click + to add it.",
                "Remove any other servers from that list, then click Apply. The router restarts its internet connection.",
                "Open any website on any device. This page turns green within 30 seconds.",
            ]
        elif brand in ("openwrt", "gl.inet"):
            steps = [
                "GL.iNet: open http://192.168.8.1 → Applications → AdGuard Home, turn it on and open its Settings page.",
                f"In AdGuard Home → Settings → DNS settings, set Upstream DNS servers to tls://{host} (only that line) and Apply.",
                f"Plain OpenWrt: install stubby, add a server with address {vm}, port 853 and tls_auth_name {host}, "
                "then point dnsmasq at 127.0.0.1#5453.",
                "Open any website on any device. This page turns green within 30 seconds.",
            ]
        else:
            steps = [
                "Open your router's admin page and find the DNS settings (often under WAN, Internet or Advanced).",
                "Look for DNS-over-TLS, DoT, Private DNS or DNS privacy. If there's no such option, this router can't do it.",
                f"Server address {vm}, port 853, TLS / authentication hostname {host}. Save.",
                "Open any website on any device. This page turns green within 30 seconds.",
            ]
    elif method == "dot":
        steps = [
            "Open Settings → Network & internet (or Connections → More connection settings).",
            "Tap Private DNS → Private DNS provider hostname.",
            f"Enter {host} and tap Save.",
            "Open any website. This page turns green within 30 seconds.",
        ]
    elif method == "doh" and (device.get("brand") or "").lower() == "apple":
        steps = [
            "Tap 'Download profile' below (open this page in Safari).",
            "Open Settings → Profile Downloaded → Install, and enter your passcode.",
            "iPhone: Settings → General → VPN & Device Management shows 'QuietNet DNS'.",
            "Open any website. This page turns green within 30 seconds.",
        ]
    elif method == "doh":
        steps = [
            "Windows 11: Settings → Network & internet → Wi-Fi (or Ethernet) → Hardware properties.",
            f"DNS server assignment → Edit → Manual → IPv4 on → Preferred DNS: {vm}.",
            f"DNS over HTTPS: On (manual template) → template: {url}",
            "Turn off 'Fallback to plaintext', then Save.",
            "Browsers (Chrome/Edge/Firefox): Settings → Privacy → Use secure DNS → Custom → paste the same URL.",
        ]
    elif method == "adb":
        steps = [
            "Easiest: use 'Find & connect my TV' and QuietNet's assistant does this for you.",
            "Manual: enable Developer options and Network/ADB debugging on the TV.",
            "On a computer on the same WiFi run:",
            "adb connect <tv-ip>:5555",
            "adb shell settings put global private_dns_mode hostname",
            f"adb shell settings put global private_dns_specifier {host}",
        ]
        values["adb_commands"] = [
            "adb shell settings put global private_dns_mode hostname",
            f"adb shell settings put global private_dns_specifier {host}",
        ]
    elif method == "forwarder":
        relay = forwarder_ip or "this laptop's WiFi IP"
        values["forwarder_ip"] = relay
        steps = [
            "QuietNet relays this TV's DNS through this laptop, so it keeps its own identity behind CGNAT.",
            f"On the TV, open the network settings, switch DNS to manual and set it to {relay}.",
            "Keep the laptop on and QuietNet running while the TV is in use. If the laptop sleeps, the TV can't look up names.",
            "To stop: set the TV's DNS back to automatic, then delete the TV here.",
        ]
    else:  # router / ipv4_demo / ipv6: plain DNS, recognised by the home's public IP
        linked, ddns = device.get("linked_ip"), device.get("ddns_host")
        steps = [
            "Open your router's admin page (the address is on its sticker, often http://192.168.1.1 or http://192.168.0.1).",
            f"Find the DNS settings (often under WAN, Internet or DHCP) and set Primary DNS to {vm}. Change nothing "
            "else on that page: Service Name, MTU and the login fields belong to your internet provider.",
            "Leave Secondary DNS empty, or set it to the same address. Any other server there lets ads slip through.",
            "Save. Every device on your WiFi, including smart TVs, is now covered. Saving the Internet page reconnects "
            "the router, and your provider may give your home a new internet address when it does.",
            (
                f"QuietNet follows your home's address through {ddns}, so a new one from your provider is picked up "
                "within a minute or two."
                if ddns
                else "So QuietNet keeps up when that address changes: turn on Dynamic DNS in the router (often "
                "Advanced → Network → Dynamic DNS; TP-Link DDNS or No-IP are free), then enter its hostname under "
                "\"Follow my home's address\" here."
            ),
            (
                f"QuietNet recognises your home by its public IP {linked}."
                if linked
                else "QuietNet recognises your home by its public IP: link it from Setup while you're on your home WiFi."
            ),
            "If the internet ever stops: open quietnet.in on your home WiFi (it still opens) and press \"I've done it, "
            "check\" to link the new address. To undo QuietNet, untick \"Use the following DNS addresses\" (or set DNS "
            "back to automatic) and save.",
        ]
    return {"method": method, "values": values, "steps": steps}


def mobileconfig(s: Settings, device: dict) -> str:
    url = escape(doh_url(s, device["client_id"]))
    name = escape(f"QuietNet DNS ({device['name']})")
    pid, cid = str(uuid.uuid5(uuid.NAMESPACE_URL, url)).upper(), str(uuid.uuid4()).upper()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>DNSSettings</key>
      <dict>
        <key>DNSProtocol</key><string>HTTPS</string>
        <key>ServerURL</key><string>{url}</string>
      </dict>
      <key>PayloadDisplayName</key><string>{name}</string>
      <key>PayloadIdentifier</key><string>app.pahra.dns.{pid}</string>
      <key>PayloadType</key><string>com.apple.dnsSettings.managed</string>
      <key>PayloadUUID</key><string>{pid}</string>
      <key>PayloadVersion</key><integer>1</integer>
    </dict>
  </array>
  <key>PayloadDescription</key><string>Routes DNS through QuietNet to block ads and trackers.</string>
  <key>PayloadDisplayName</key><string>{name}</string>
  <key>PayloadIdentifier</key><string>app.pahra.profile.{cid}</string>
  <key>PayloadRemovalDisallowed</key><false/>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadUUID</key><string>{cid}</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict>
</plist>
"""
