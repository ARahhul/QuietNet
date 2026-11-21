"""One 'connect my TV' conversation: event log (SSE), user inbox, confirmations, shared actions."""

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from ..adguard.client import AdGuardError
from ..db import new_id, now_iso, row_to_dict
from .. import forwarder
from ..setup_guides import doh_url, dot_hostname
from .mcp_client import CompanionClient
from .redact import HANDLE, Redactor

log = logging.getLogger("pahra.assistant")

ANDROID_FAMILY = {"android_tv", "google_tv", "fire_tv"}
TV_PLATFORMS = ANDROID_FAMILY | {"webos", "tizen", "roku", "apple_tv"}
PLATFORM_LABEL = {
    "android_tv": "Android TV", "google_tv": "Google TV", "fire_tv": "Fire TV", "webos": "LG webOS",
    "tizen": "Samsung Tizen", "roku": "Roku", "apple_tv": "Apple TV", "router": "Router", "unknown": "Unknown device",
}
PLATFORM_BRAND = {"webos": "LG", "tizen": "Samsung", "fire_tv": "Amazon", "google_tv": "Google", "android_tv": "Android", "roku": "Roku", "apple_tv": "Apple"}

STEP_TEXT = {
    "get_network_context": "Looking at your home network",
    "scan_network": "Scanning your WiFi for devices (about 5-10 s)",
    "ping_device": "Pinging the TV",
    "probe_device": "Identifying the TV",
    "adb_status": "Checking developer mode (ADB) on the TV",
    "get_setup_options": "Finding the best way to connect it",
    "adb_connect": "Connecting to the TV over ADB",
    "adb_pair": "Pairing with the TV",
    "set_private_dns": "Pointing the TV's DNS at QuietNet",
    "clear_private_dns": "Resetting the TV's Private DNS",
}
CONFIRM_TEXT = {
    "adb_connect": "Connect to {label} over ADB (developer mode)? The TV will ask \"Allow debugging?\". Press Allow on the TV.",
    "adb_pair": "Pair this computer with {label} using the code shown on the TV?",
    "set_private_dns": "Point {label}'s DNS at QuietNet ({hostname})? Its ad and tracker traffic will start showing up here. You can undo this any time.",
    "clear_private_dns": "Reset {label}'s Private DNS back to Automatic?",
    "forwarder": "Run QuietNet's DNS relay on this laptop for {label}? The TV will send its DNS to this laptop, which passes it to "
                 "QuietNet under the TV's own name. Keep the laptop on (and QuietNet running) while the TV is in use.",
}
FORWARDER_STEP = "Starting QuietNet's DNS relay on this laptop"


def is_forwarder_option(o: dict) -> bool:
    return bool(o.get("starts_forwarder") or o.get("dns_target") == "laptop_forwarder")


class Cancelled(Exception):
    pass


# SSDP friendlyNames are sometimes internal IDs, e.g. "amzn.dmgr:E17DB92AC62C…:xnVDMM+4u1:673666" or "uuid:…".
_ID_LIKE = re.compile(r"(^uuid:)|([0-9A-Fa-f]{12,})|(^[\w.-]+:[\w+/=:-]{12,}$)")


_BRAND_CASE = {"lg": "LG", "tcl": "TCL", "oneplus": "OnePlus", "tp-link": "TP-Link", "jiofiber": "JioFiber", "mi": "Mi", "vu": "Vu"}


def brand_name(brand: str | None) -> str | None:
    if not brand:
        return None
    if brand != brand.lower():
        return brand
    return _BRAND_CASE.get(brand, brand.capitalize())


def nice_name(d: dict) -> str:
    fn = (d.get("friendly_name") or "").strip()
    if fn and not _ID_LIKE.search(fn) and len(fn) <= 60:
        return fn
    platform = PLATFORM_LABEL.get(d.get("platform") or "", "")
    bm = " ".join(x for x in (brand_name(d.get("brand")), d.get("model")) if x)
    if bm and platform and platform.lower() not in bm.lower():
        return f"{bm} ({platform})"
    return bm or (platform if platform and d.get("platform") != "unknown" else "") or d.get("vendor") or "Unknown device"


class AssistantSession:
    def __init__(self, engine, companion: CompanionClient, description: str):
        self.id = uuid.uuid4().hex[:12]
        self.engine = engine
        self.companion = companion
        self.description = description.strip()[:200]
        self.events: list[dict] = []
        self.cond = asyncio.Condition()
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.pending_confirms: dict[str, asyncio.Future] = {}
        self.redactor = Redactor()
        self.done = False
        self.device: dict | None = None
        self.labels: dict[str, str] = {}  # ip -> human label
        self.network: dict = {}
        self.last_options: dict | None = None
        self.adb_port = 5555
        self.forwarder_ip: str | None = None  # set once the laptop relay runs for this TV
        self.forwarder_hint: str | None = None  # e.g. the Windows Firewall rule to allow DNS in
        self.target_ip: str | None = None  # the TV the user picked
        self.task: asyncio.Task | None = None

    # ------------------------------------------------------------- events
    async def emit(self, ev: dict) -> None:
        async with self.cond:
            self.events.append(ev)
            if ev.get("type") == "done":
                self.done = True
            self.cond.notify_all()

    async def say(self, text: str) -> None:
        await self.emit({"type": "message", "role": "assistant", "text": text})

    async def ask(self, prompt: str, kind: str = "text", timeout: float = 900) -> str:
        await self.emit({"type": "input_required", "kind": kind, "prompt": prompt})
        try:
            reply = await asyncio.wait_for(self.inbox.get(), timeout)
        except asyncio.TimeoutError:
            raise Cancelled("no reply")
        # Echo the reply into the timeline, with device handles shown as their labels.
        shown = HANDLE.sub(lambda m: self.label(self.redactor.rev.get(m.group(0))), reply)
        await self.emit({"type": "message", "role": "user", "text": shown})
        return reply

    async def confirm(self, prompt: str, timeout: float = 600) -> bool:
        call_id = new_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending_confirms[call_id] = fut
        await self.emit({"type": "confirm_required", "call_id": call_id, "prompt": prompt})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending_confirms.pop(call_id, None)

    def user_message(self, text: str) -> None:
        self.inbox.put_nowait(text[:500])

    def answer_confirm(self, call_id: str, approve: bool) -> bool:
        fut = self.pending_confirms.get(call_id)
        if not fut or fut.done():
            return False
        fut.set_result(approve)
        return True

    # ------------------------------------------------------------- actions
    def label(self, ip: str | None) -> str:
        return self.labels.get(ip or "", "the TV")

    def remember_devices(self, devices: list[dict]) -> None:
        for d in devices:
            ip = d.get("ip")
            if not ip:
                continue
            self.labels[ip] = nice_name(d)

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a companion tool with a progress step, asking the user first for anything mutating."""
        if self.companion.is_mutating(name):
            prompt = CONFIRM_TEXT.get(name, f"Allow QuietNet to run {name} on your device?").format(
                label=self.label(args.get("ip")), hostname=args.get("hostname", "")
            )
            if not await self.confirm(prompt):
                await self.emit({"type": "step", "text": f"{STEP_TEXT.get(name, name)}: you said no", "status": "failed"})
                return {"ok": False, "error": "the user declined", "declined": True}
        text = STEP_TEXT.get(name, name)
        await self.emit({"type": "step", "text": text, "status": "running"})
        res = await self.companion.call(name, args)
        ok = bool(res.get("ok", True))
        await self.emit({"type": "step", "text": text if ok else f"{text}: {res.get('error') or 'failed'}", "status": "ok" if ok else "failed"})
        if name == "scan_network" and ok:
            self.remember_devices(res.get("devices") or [])
        if name == "probe_device" and ok:
            self.remember_devices([res])
        if name == "get_network_context" and ok:
            self.network = res
        if name == "get_setup_options" and ok:
            self.last_options = res
        if name in ("adb_connect", "adb_status") and ok and res.get("port"):
            self.adb_port = int(res["port"])
        return res

    def ensure_device(self, name: str | None, brand: str | None, setup_method: str = "adb", lan_ip: str | None = None) -> dict:
        """Create the QuietNet device record (type tv) the first time we need its hostname."""
        if self.device:
            return self.device
        e = self.engine
        from ..api.devices import slug_client_id

        nice = (name or self.description or "TV").strip()[:60]
        cid = slug_client_id(e, nice, None)
        did = new_id()
        e.conn.execute(
            "INSERT INTO devices(id, user_id, client_id, name, type, brand, setup_method, lan_ip, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (did, e.user_id, cid, nice, "tv", brand, setup_method, lan_ip, now_iso()),
        )
        self.device = row_to_dict(e.conn.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone())
        # The hostname / DoH URL identify this device to the DNS server: keep them away from the LLM.
        self.redactor.add_secret(self.hostname(), "<this TV's QuietNet address>")
        self.redactor.add_secret(doh_url(e.settings, cid), "<this TV's QuietNet DoH address>")
        return self.device  # type: ignore[return-value]

    def hostname(self) -> str:
        assert self.device
        return dot_hostname(self.engine.settings, self.device["client_id"])

    async def set_private_dns(self, ip: str, device_name: str | None, brand: str | None) -> dict:
        prompt_host = dot_hostname(self.engine.settings, "<new-id>") if not self.device else self.hostname()
        # Confirm BEFORE creating the device so a "no" leaves nothing behind.
        if not await self.confirm(
            CONFIRM_TEXT["set_private_dns"].format(label=self.label(ip), hostname=prompt_host.replace("<new-id>", "your TV's own address"))
        ):
            return {"ok": False, "error": "the user declined", "declined": True}
        dev = self.ensure_device(device_name or self.label(ip), brand)
        try:
            await self.engine.sync.sync_all()  # persistent client + access list BEFORE the TV starts querying
        except AdGuardError as ex:
            log.warning("sync before set_private_dns failed: %s", ex)
        await self.emit({"type": "step", "text": STEP_TEXT["set_private_dns"], "status": "running"})
        res = await self.companion.call("set_private_dns", {"ip": ip, "hostname": self.hostname(), "port": self.adb_port})
        ok = bool(res.get("ok"))
        await self.emit({"type": "step", "text": STEP_TEXT["set_private_dns"] if ok else f"Private DNS: {res.get('error')}", "status": "ok" if ok else "failed"})
        res["device_id"] = dev["id"]
        return res

    async def setup_forwarder(self, ip: str, device_name: str | None, brand: str | None) -> dict:
        """Create the TV as a forwarder device and (re)start the laptop relay with it. Asks first."""
        if not forwarder.available(self.companion):
            return {"ok": False, "error": "the DNS relay isn't available in this companion"}
        if not await self.confirm(CONFIRM_TEXT["forwarder"].format(label=self.label(ip))):
            await self.emit({"type": "step", "text": f"{FORWARDER_STEP}: you said no", "status": "failed"})
            return {"ok": False, "error": "the user declined", "declined": True}
        created = self.device is None
        dev = self.ensure_device(device_name or self.label(ip), brand, "forwarder", ip)
        if not created:  # e.g. ADB path created it first: switch it to the relay
            self.engine.conn.execute("UPDATE devices SET setup_method = 'forwarder', lan_ip = ? WHERE id = ?", (ip, dev["id"]))
        try:
            await self.engine.sync.sync_all()  # client + access list before the relay forwards anything
        except AdGuardError as ex:
            log.warning("sync before relay start failed: %s", ex)
        await self.emit({"type": "step", "text": FORWARDER_STEP, "status": "running"})
        state = await forwarder.apply(self.engine)
        if not state.get("running"):
            await self.emit({"type": "step", "text": f"{FORWARDER_STEP}: {state.get('error')}", "status": "failed"})
            if created:  # leave nothing behind
                self.engine.conn.execute("DELETE FROM devices WHERE id = ?", (dev["id"],))
                await self.engine.sync.remove_client(dev["client_id"])
                self.device = None
                await forwarder.apply(self.engine)
            return {"ok": False, "error": state.get("error"), "code": state.get("code"), "hint": state.get("hint")}
        await self.emit({"type": "step", "text": FORWARDER_STEP, "status": "ok"})
        self.forwarder_ip = state.get("listen_ip")
        self.forwarder_hint = state.get("hint")
        return {"ok": True, "forwarder_ip": self.forwarder_ip, "hint": state.get("hint"), "device_id": dev["id"]}

    async def verify_connected(self, timeout_s: float = 60) -> bool:
        if not self.device:
            return False
        cid = self.device["client_id"]
        await self.emit({"type": "step", "text": "Waiting for the TV's first query through QuietNet (up to 60 s)", "status": "running"})
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        while loop.time() < end:
            try:
                data = await self.engine.ag.querylog(search=cid, limit=5) or {}
                if any((e.get("client_id") or "") == cid for e in data.get("data") or []):
                    self.engine.conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now_iso(), self.device["id"]))
                    self.engine.poller.poke()
                    await self.emit({"type": "step", "text": "The TV is talking through QuietNet", "status": "ok"})
                    return True
            except AdGuardError:
                pass
            await asyncio.sleep(3)
        await self.emit({"type": "step", "text": "No query seen from the TV yet", "status": "failed"})
        return False

    # ------------------------------------------------------------- guides
    def fill(self, s: str) -> str:
        s_ = self.engine.settings
        net = self.network or {}
        gw = (net.get("gateway") or {}).get("ip") or "your router's IP"
        laptop = self.forwarder_ip or (net.get("primary_interface") or {}).get("ip") or "this laptop's IP"
        host = self.hostname() if self.device else f"<your-device>.{s_.dns_hostname}"
        return (
            s.replace("{hostname}", host)
            .replace("{dns_server_ip}", s_.vm_ip or "<QuietNet server IP>")
            .replace("{gateway_ip}", gw)
            .replace("{forwarder_ip}", laptop)
            .replace("{doh_url}", f"https://{s_.dns_hostname}/dns-query/{self.device['client_id'] if self.device else '<id>'}")
        )

    def option_steps(self, pred) -> list[str]:
        for o in (self.last_options or {}).get("options") or []:
            if pred(o):
                return [self.fill(x) for x in o.get("steps") or []]
        return []

    def library_steps(self) -> set[str]:
        out = set()
        for o in (self.last_options or {}).get("options") or []:
            out |= {self.fill(x) for x in o.get("steps") or []}
            for v in o.get("other_variants") or []:
                out |= {self.fill(x) for x in v.get("steps") or []}
        return out

    def guide_from_options(self, prefer_manual: bool = True, use_forwarder: bool | None = None) -> dict | None:
        """The best hand-checked guide. The relay option is only shown once the relay actually runs."""
        if use_forwarder is None:
            use_forwarder = bool(self.forwarder_ip)
        opts = [o for o in (self.last_options or {}).get("options") or [] if use_forwarder or not is_forwarder_option(o)]
        if use_forwarder:
            opts = sorted(opts, key=lambda o: not is_forwarder_option(o))
        usable = [o for o in opts if (o.get("available", True) or (use_forwarder and is_forwarder_option(o)))
                  and not (prefer_manual and o.get("automatable"))] or opts
        if not usable:
            return None
        top = usable[0]
        steps = [self.fill(x) for x in top.get("steps") or []]
        if top.get("why"):
            steps.insert(0, top["why"])
        for imp in (self.last_options or {}).get("impossible") or []:
            steps.append(f"Not possible: {imp}")
        if not top.get("verified"):
            steps.append("These steps aren't verified on every model. Menu names can differ by software version.")
        others = [o["title"] for o in usable[1:3]]
        if others:
            steps.append("Other options: " + "; ".join(others) + ".")
        values = {}
        if use_forwarder and is_forwarder_option(top) and self.forwarder_ip:
            values["DNS server for the TV (this laptop)"] = self.forwarder_ip
        elif self.engine.settings.vm_ip:
            values["QuietNet DNS server"] = self.engine.settings.vm_ip
        return {"title": top.get("title") or "Set up DNS manually", "steps": steps, "values": values, "ai_suggested": False}

    async def finish(self, outcome: str, text: str, guide: dict | None = None) -> None:
        ev: dict[str, Any] = {"type": "done", "outcome": outcome, "text": text}
        if guide:
            ev["guide"] = guide
        if self.device:
            ev["device_id"] = self.device["id"]
        await self.emit(ev)

    def transcript_for_log(self) -> str:
        return json.dumps(self.events)[:2000]
