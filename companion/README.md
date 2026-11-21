# pahra-companion

A local MCP server that runs on the laptop, on the home LAN. It finds the TV the user describes, then points that TV's DNS at Pahra:

* **Android TV, Google TV and Fire TV:** it sets Android Private DNS over ADB automatically.
* **LG webOS, Samsung Tizen, Roku, Apple TV and routers:** it returns ranked, step-by-step guidance that takes CGNAT into account.
* **TVs that only take a plain IPv4 DNS server (webOS, Tizen, Apple TV):** it can run a **DNS relay** on the laptop's LAN IP. The TV's DNS points at the laptop. The relay forwards each mapped TV's queries over DoH to `https://<dns_hostname>/dns-query/<client_id>`, so every TV keeps its own Pahra identity, even behind CGNAT.

The Pahra backend spawns it over **stdio**, and a Gemini agent uses its tools. It can also serve **streamable HTTP** for MCP Inspector.

## Install and run

Python 3.11+ and [Poetry](https://python-poetry.org/) 2 are required (`pipx install poetry`). Dependencies are pinned in `poetry.lock`.

```powershell
cd companion
poetry install                                           # creates .venv here (see poetry.toml) from poetry.lock
poetry run pahra-companion                               # stdio (what the backend uses)
poetry run pahra-companion --http 127.0.0.1:8765         # streamable HTTP at http://127.0.0.1:8765/mcp
poetry run pytest                                        # tests
```

To add or upgrade a package, use `poetry add <pkg>` or `poetry update <pkg>`, then commit `pyproject.toml` and `poetry.lock`.

**MCP Inspector:**

* Run `npx @modelcontextprotocol/inspector`.
* Choose transport "Streamable HTTP" and URL `http://127.0.0.1:8765/mcp`.
* Or choose stdio with command `.venv\Scripts\python` and arguments `-m pahra_companion`.

`--http` refuses non-loopback addresses, so the mutating tools are never exposed to the LAN.

**ADB** comes from Google platform-tools:

* Install it with `winget install Google.PlatformTools` (macOS: `brew install android-platform-tools`).
* Or set `ADB_PATH` to the binary or its folder.

When adb is missing, `get_network_context.adb` reports it, and the ADB tools return `code: "adb_missing"` with the install hint.

The SDK is written against **mcp 2.x** (`MCPServer`, tested with 2.3.0). With mcp 1.x installed, `server.py` falls back to `FastMCP`. That fallback was checked once over stdio with mcp 1.30.0, and the pytest suite itself needs mcp 2.x.

Environment variables:

* `ADB_PATH`: the adb binary or its folder.
* `PAHRA_COMPANION_LOG_LEVEL`: the log level (default `INFO`). Logs go to stderr only.

## Tools

Every tool returns a JSON object with `ok`. Expected failures do not raise MCP errors. They return:

```json
{"ok": false, "error": "...", "hint": "...", "code": "..."}
```

The `code` values are `guard`, `adb_missing`, `rejected`, `adb_error`, `invalid_argument`, `not_listening`, `unauthorized`, `connect_failed`, `pair_failed`, `not_connected`, `verify_failed`, `bind_failed` and `internal`.

Read-only tools carry `readOnlyHint: true`. The others carry `readOnlyHint: false`, and those that change device settings also carry `destructiveHint: true`. Gate confirmations on `readOnlyHint`.

| Tool | Kind | Returns |
|---|---|---|
| `get_network_context()` | read-only, contacts 1.1.1.1 | `interfaces`, `primary_interface`, `scan.subnet` (+ `clamped`), `gateway` {ip, mac, vendor, http_title (no login), upnp, brand_guess}, `public_ip`, `cgnat` {verdict: `cgnat`/`not_cgnat`/`unknown`, reason, wan_ip}, `ipv6.present`, `adb` {available, path, hint} |
| `scan_network(subnet=None)` | read-only, ~4-8 s | `devices[]`: {ip, mac, vendor, alive, rtt_ms, is_gateway, open_ports, platform, confidence, brand, model, friendly_name, ssdp_names, mdns_services, capabilities, reasons}, plus `platform_counts` |
| `ping_device(ip)` | read-only | `alive`, `rtt_ms` |
| `probe_device(ip)` | read-only, ~3-4 s | the device fields above, plus raw `ssdp` / `mdns`, `http_title`, and a `next` hint |
| `adb_status(ip, port=5555)` | read-only | `status`: `not_listening` / `listening` / `unauthorized` / `offline` / `connected` (with `sdk`, `manufacturer`, `model`, `private_dns`, `private_dns_supported`), plus `adb_available` |
| `get_setup_options(platform, brand=None, model=None, cgnat=None, router_brand=None)` | read-only | ranked `options[]` {rank, id, kind, title, recommended, available, automatable, gives_per_device_identity, why, dns_target, steps, verified, other_variants, notes}, plus `impossible[]` and `placeholders` |
| `adb_connect(ip, port=5555)` | mutating | `status: connected` with sdk/model; or `ok:false` with `status: unauthorized` (the user must accept on the TV, then call again) or `not_listening` |
| `adb_pair(ip, port, code)` | mutating | `paired`. Then call `adb_connect` with the Wireless-debugging connect port |
| `set_private_dns(ip, hostname, port=5555)` | mutating, destructive | Needs SDK ≥ 28. Writes `private_dns_specifier` and then `private_dns_mode=hostname`, reads both back, and returns `before`, `after` and `verified` |
| `clear_private_dns(ip, port=5555)` | mutating, destructive | Sets `private_dns_mode=opportunistic` and returns `before` and `after` |
| `start_dns_forwarder(mappings, dns_hostname, port=53)` | mutating, contacts the Pahra DoH server | `mappings` is `[{"ip": "<TV LAN IP>", "client_id": "<[a-z0-9-]{1,63}>"}]`. Returns `{ok, running: true, listen_ip, port, mappings, upstream_template: "https://<dns_hostname>/dns-query/<client_id>", rebound, hint}`. Calling it again replaces the whole mapping set and rebinds only if the port changes. A bind failure gives `code: "bind_failed"` with a hint |
| `stop_dns_forwarder()` | mutating | `{ok, running: false, was_running, stats}` |
| `dns_forwarder_status()` | read-only | `{ok, running, listen_ip, port, mappings, stats: {queries, forwarded, refused, errors}, per_client: {client_id: {queries, last_ts}}, started_at}`. `last_ts` is ISO 8601 UTC |

The `platform` values are `android_tv | google_tv | fire_tv | webos | tizen | roku | apple_tv | router | unknown`.

**`adb_status` has two extra states.** On top of the three in the plan (`not_listening`, `unauthorized`, `connected`), it can return:

* `listening`: the port is open but this laptop's adb has no session yet. Call `adb_connect`.
* `offline`

`adb_status` never connects, because connecting triggers the "Allow debugging?" prompt on the TV.

**`get_setup_options` placeholders.** Guide steps contain placeholders for the caller to fill in: `{hostname}`, `{dns_server_ip}`, `{gateway_ip}` and `{forwarder_ip}` (the relay's `listen_ip`).

Every option also carries `starts_forwarder` and `needs_laptop_on`. Both are `true` only on the relay option `<platform>_manual_dns:laptop_forwarder` (for example `webos_manual_dns:laptop_forwarder`). When the user picks that option, call `start_dns_forwarder`, then fill `{forwarder_ip}` with the returned `listen_ip`.

**`get_setup_options` ranking:**

1. Android family: ADB Private DNS comes first. If ADB fails, the list below follows.
2. If the verdict is `not_cgnat`:
   1. Router DNS.
   2. Manual DNS on the TV pointed at the VM.
   3. The laptop relay (available, `recommended: false`, because it needs the laptop on).
3. If the verdict is `cgnat` or `unknown`:
   1. The laptop relay (recommended).
   2. A phone hotspot (demo only).
   3. Router DNS (`recommended: false`).
4. Roku and unknown devices have no DNS field. The router-to-relay variant `laptop_forwarder:router` is listed with `available: false`, because the relay refuses every unmapped device and the whole home would lose DNS.

## Prompt

`connect_device(description)` is the agent playbook:

1. Get the network context.
2. Scan, and match the description.
3. Ping and probe the device.
4. Android family:
   1. Make sure ADB is on (show the enable steps and wait).
   2. Connect or pair.
   3. Check the SDK.
   4. Call `set_private_dns` with the hostname the caller provides.
   5. The caller verifies.
5. Locked platforms, or a failure: `get_setup_options` guidance, with router DNS only when not behind CGNAT, and a plain statement of what is impossible. Behind CGNAT:
   1. `start_dns_forwarder` with the TV's IP and `client_id`.
   2. The user types the relay's `listen_ip` as the TV's DNS server.

## Guardrails

* **Subnet guard** (`netinfo.check_lan_ip` / `check_subnet`):
  * Every IP and subnet must be IPv4 and RFC 1918.
  * It must be on a network attached to a local interface.
  * Scans are capped at /22.
  * With no subnet given, a larger interface network is clamped to the laptop's own /24.
  * SSDP `LOCATION` URLs and IGD control URLs are only fetched when their host passes the guard.
* **ADB allowlist** (`adb.validate_argv`): only `devices`, `start-server`, `connect`, `disconnect`, `pair`, and `-s <ip:port>` with `get-state`, `shell getprop ro.build.version.sdk|ro.product.manufacturer|ro.product.model`, or `shell settings get|put global private_dns_mode|private_dns_specifier`.
  * Mode values are `hostname`, `opportunistic` and `off`.
  * Hostnames must be plain DNS names, because `adb shell` hands its arguments to the TV's shell.
  * There is no generic shell.
* **No credentials:** it never logs into routers or TVs. Router and locked-TV changes are always guidance that the user carries out.
* **DNS relay:**
  * It binds UDP and TCP only on the primary private LAN IP (the one `get_network_context` reports), never `0.0.0.0`.
  * Mapped IPs must pass the subnet guard. `client_id` must match `^[a-z0-9-]{1,63}$`, and `dns_hostname` must be a plain DNS name.
  * Only mapped source IPs get answers. Everything else gets REFUSED and is counted.
  * Upstream is DoH POST (RFC 8484, `application/dns-message`), sent with query id 0; the client's id is restored in the reply. The timeout is 4 s, and any upstream failure returns SERVFAIL.
  * There is no caching. A UDP answer bigger than the client accepts is returned with TC=1, so the client retries over TCP.
  * It never touches the firewall. The `hint` gives the admin PowerShell commands (`New-NetFirewallRule ... -Profile Private`).
  * It runs inside the companion process, so it stops when the backend stops.

## Layout

```
pahra_companion/
  server.py         tool + prompt registration, CLI (stdio / --http)
  netinfo.py        interfaces, default gateway (route print / Get-NetRoute, /proc/net/route, route get), subnet guard
  discovery.py      ping sweep, ARP, SSDP + description XML, mDNS (zeroconf), TCP probes, router <title>
  fingerprint.py    platform classifier + capabilities
  oui.json          partial offline OUI → vendor map (hint only)
  adb.py            platform-tools wrapper with allowlist
  cgnat.py          UPnP IGD GetExternalIPAddress vs https://1.1.1.1/cdn-cgi/trace
  setup_options.py  guide selection and ranking
  guides/*.yaml     hand-written steps (all verified: false)
  forwarder.py      laptop DNS relay: UDP+TCP on the LAN IP -> DoH per ClientID
tests/              pytest suite; fixtures/ holds synthetic SSDP/mDNS/port evidence
```

## Not verified on real hardware

* All guide steps (`verified: false`). Menus differ by firmware.
* The fingerprint fixtures are synthetic. They are modelled on typical replies, not captured from the demo TVs.
* ADB against a real TV or emulator. The flow is tested only with a mocked `adb`.
* The UPnP IGD / CGNAT path on JioFiber, Airtel or ACT routers.
* mDNS service names and TXT keys on current Google TV / Fire TV builds.
* The OUI table is partial and was not re-checked against the IEEE registry.
* The DNS relay with a real TV and a real AdGuard DoH endpoint.
  * It was tested with real sockets on loopback, and on this laptop's LAN IP at port 53 (from the laptop itself), against a mocked upstream.
  * Not tested: queries from another LAN device through Windows Firewall, and AdGuard's handling of `/dns-query/<client_id>`.
