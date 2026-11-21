"""In-process stand-in for pahra-companion with the same tool names and result shapes."""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False)
RW = ToolAnnotations(read_only_hint=False, destructive_hint=True)
RW_SAFE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
LAPTOP_IP = "192.168.29.10"

LG = {"ip": "192.168.29.41", "mac": "a8:23:fe:11:22:33", "vendor": "LG Electronics", "platform": "webos", "confidence": 0.9,
      "brand": "LG", "model": "OLED55C2", "friendly_name": "[LG] webOS TV OLED55C2", "is_gateway": False,
      "open_ports": [3000, 3001], "capabilities": {"adb_reachable": False, "wireless_debugging_advertised": False, "automatable": False}}
FIRE = {"ip": "192.168.29.57", "mac": "f0:f0:a4:aa:bb:cc", "vendor": "Amazon Technologies", "platform": "fire_tv", "confidence": 0.85,
        "brand": "Amazon", "model": "AFTSSS", "friendly_name": "Bedroom Fire TV", "is_gateway": False,
        "open_ports": [5555, 8009], "capabilities": {"adb_reachable": True, "wireless_debugging_advertised": False, "automatable": True}}
ROUTER = {"ip": "192.168.29.1", "mac": "00:11:22:33:44:55", "vendor": "Jio", "platform": "router", "is_gateway": True,
          "open_ports": [80], "capabilities": {}}


UNKNOWN = {"ip": "192.168.29.80", "mac": "3a:1b:22:33:44:55", "vendor": None, "randomized_mac": True, "platform": "unknown",
           "confidence": "none", "is_gateway": False, "open_ports": [], "capabilities": {}}


def make_stub(on_private_dns=None, devices=None, relay=True, on_forwarder=None):
    devices = devices or [ROUTER, LG, FIRE]
    by_ip = {d["ip"]: d for d in devices}
    calls: list[tuple[str, dict]] = []
    state = {"adb": "listening", "relay": {"running": False, "mappings": []}}
    srv = MCPServer("pahra-companion-stub")

    @srv.tool(annotations=RO)
    async def get_network_context() -> dict:
        calls.append(("get_network_context", {}))
        return {"ok": True, "scan": {"subnet": "192.168.29.0/24"}, "public_ip": "49.36.10.20",
                "gateway": {"ip": "192.168.29.1", "mac": "00:11:22:33:44:55", "brand_guess": "jiofiber"},
                "cgnat": {"verdict": "cgnat", "wan_ip": "100.72.14.9"}, "adb": {"available": True}}

    @srv.tool(annotations=RO)
    async def scan_network(subnet: str | None = None) -> dict:
        calls.append(("scan_network", {"subnet": subnet}))
        return {"ok": True, "devices": devices, "device_count": len(devices)}

    @srv.tool(annotations=RO)
    async def ping_device(ip: str) -> dict:
        calls.append(("ping_device", {"ip": ip}))
        return {"ok": True, "ip": ip, "alive": True, "rtt_ms": 3.1}

    @srv.tool(annotations=RO)
    async def probe_device(ip: str) -> dict:
        calls.append(("probe_device", {"ip": ip}))
        return {"ok": True, **by_ip[ip]}

    @srv.tool(annotations=RO)
    async def adb_status(ip: str, port: int = 5555) -> dict:
        calls.append(("adb_status", {"ip": ip}))
        base = {"ok": True, "ip": ip, "port": port, "adb_available": True}
        if state["adb"] == "connected":
            return {**base, "status": "connected", "sdk": 30, "private_dns_supported": True}
        return {**base, "status": state["adb"]}

    @srv.tool(annotations=RO)
    async def get_setup_options(platform: str, brand: str | None = None, model: str | None = None,
                                cgnat: str | None = None, router_brand: str | None = None) -> dict:
        calls.append(("get_setup_options", {"platform": platform, "cgnat": cgnat}))
        opts = []
        if platform == "fire_tv":
            opts.append({"id": "adb_private_dns", "title": "Automatic (ADB)", "automatable": True, "available": True,
                         "recommended": True, "verified": False, "steps": ["Settings → My Fire TV → About → click 7×"]})
        if relay and cgnat == "cgnat" and platform in ("webos", "tizen", "fire_tv", "android_tv", "google_tv"):
            opts.append({"id": f"{platform}_manual_dns:laptop_forwarder", "title": "Point the TV's DNS at this laptop (Pahra relay)",
                         "kind": "manual_dns", "dns_target": "laptop_forwarder", "starts_forwarder": True, "needs_laptop_on": True,
                         "automatable": False, "available": True, "recommended": True, "verified": False,
                         "why": "Behind CGNAT the relay keeps this TV's own identity.",
                         "steps": ["Settings → General → Network → Wi-Fi → Advanced → Edit", "DNS server: {forwarder_ip}"]})
        opts.append({"id": "manual_dns_tv:hotspot", "title": "Manual DNS on the TV (phone hotspot)", "automatable": False,
                     "available": True, "recommended": cgnat == "cgnat" and not relay, "verified": False, "why": "Behind CGNAT.",
                     "steps": ["Settings → General → Network → Wi-Fi → Advanced → Edit", "DNS server: {dns_server_ip}"]})
        opts.append({"id": "router_dns", "title": "Router DNS", "automatable": False, "available": cgnat != "cgnat",
                     "recommended": False, "verified": False, "steps": ["Open http://{gateway_ip}"]})
        return {"ok": True, "platform": platform, "cgnat": cgnat, "options": opts,
                "impossible": ["LG webOS has no ADB."] if platform == "webos" else []}

    @srv.tool(annotations=RW)
    async def adb_connect(ip: str, port: int = 5555) -> dict:
        calls.append(("adb_connect", {"ip": ip}))
        state["adb"] = "connected"
        return {"ok": True, "status": "connected", "serial": f"{ip}:{port}", "sdk": 30}

    @srv.tool(annotations=RW)
    async def adb_pair(ip: str, port: int, code: str) -> dict:
        calls.append(("adb_pair", {"ip": ip}))
        return {"ok": True, "paired": True}

    @srv.tool(annotations=RW)
    async def set_private_dns(ip: str, hostname: str, port: int = 5555) -> dict:
        calls.append(("set_private_dns", {"ip": ip, "hostname": hostname}))
        if on_private_dns:
            on_private_dns(hostname)
        return {"ok": True, "hostname": hostname, "verified": True, "before": {"mode": "opportunistic"},
                "after": {"mode": "hostname", "specifier": hostname}}

    @srv.tool(annotations=RW)
    async def clear_private_dns(ip: str, port: int = 5555) -> dict:
        calls.append(("clear_private_dns", {"ip": ip}))
        return {"ok": True}

    if relay:
        @srv.tool(annotations=RW_SAFE)
        async def start_dns_forwarder(mappings: list[dict], dns_hostname: str, port: int = 53) -> dict:
            calls.append(("start_dns_forwarder", {"mappings": mappings, "dns_hostname": dns_hostname}))
            state["relay"] = {"running": True, "mappings": mappings}
            if on_forwarder:
                on_forwarder(mappings)
            return {"ok": True, "running": True, "listen_ip": LAPTOP_IP, "port": port, "mappings": mappings,
                    "upstream_template": f"https://{dns_hostname}/dns-query/<client_id>",
                    "hint": "Windows Firewall must allow DNS in: New-NetFirewallRule -DisplayName 'Pahra DNS relay' ..."}

        @srv.tool(annotations=RW_SAFE)
        async def stop_dns_forwarder() -> dict:
            calls.append(("stop_dns_forwarder", {}))
            state["relay"] = {"running": False, "mappings": []}
            return {"ok": True, "running": False, "stats": {}}

        @srv.tool(annotations=RO)
        async def dns_forwarder_status() -> dict:
            return {"ok": True, **state["relay"]}

    @srv.prompt()
    def connect_device(description: str) -> str:
        return f"Playbook for {description}"

    return srv, calls, state
