import asyncio
import json
import re

import httpx
import pytest
from google.genai import types

from pahra.adguard.client import AdGuardClient
from pahra.assistant.redact import Redactor
from pahra.assistant.service import AssistantService
from pahra.engine import Engine

from pahra import forwarder
from pahra.assistant.agent import build_declarations

from .stub_companion import FIRE, LAPTOP_IP, LG, ROUTER, UNKNOWN, make_stub


@pytest.fixture
async def env(settings, fake_app, fake):
    settings.vm_ip = "34.93.1.2"
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)

    def tv_starts_querying(hostname: str):
        fake.record(hostname.split(".")[0], "device-metrics-us.amazon.com")

    def relay_traffic(mappings):
        for m in mappings:
            fake.record(m["client_id"], "lgsmartad.com")

    srv, calls, state = make_stub(on_private_dns=tv_starts_querying, on_forwarder=relay_traffic)
    yield eng, srv, calls, state
    await eng.stop()


async def drive(s, answers, timeout=20):
    """Answer input/confirm prompts in order. `answers` items: str (message) or bool (confirm)."""
    answers = list(answers)
    seen = 0
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not s.done:
        assert loop.time() < end, f"timed out; events={json.dumps(s.events)[:3000]}"
        while seen < len(s.events):
            ev = s.events[seen]
            seen += 1
            if ev["type"] == "confirm_required":
                a = answers.pop(0)
                assert isinstance(a, bool), f"expected confirm, got {ev}"
                s.answer_confirm(ev["call_id"], a)
            elif ev["type"] == "input_required":
                a = answers.pop(0)
                assert isinstance(a, str), f"expected input, got {ev}"
                s.user_message(a)
        await asyncio.sleep(0.02)
    return s.events[-1]


def test_redactor_roundtrip():
    r = Redactor()
    out = r.redact({"ip": "192.168.29.57", "mac": "F0:F0:A4:AA:BB:CC", "note": "gw 192.168.29.1 net 192.168.29.0/24 v6 2401:4900:1c00::1"})
    blob = json.dumps(out)
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", blob) and "f0:f0" not in blob.lower() and "2401" not in blob
    assert r.unredact(out["ip"]) == "192.168.29.57"


async def test_guided_fire_tv_connects_with_confirmations(env):
    eng, srv, calls, _ = env
    svc = AssistantService(eng, target=srv, genai_client=None)
    await svc.start()
    assert svc.companion.connected and "set_private_dns" in svc.companion.tools
    s = svc.create("the fire tv in the bedroom")

    # pick the Fire TV, allow adb_connect, allow set_private_dns
    async def pick_and_answer():
        while not any(e["type"] == "devices" for e in s.events):
            await asyncio.sleep(0.02)
        dev = next(e for e in s.events if e["type"] == "devices")
        assert dev["devices"][0]["label"] == "Bedroom Fire TV"  # description match ranks it first
        return dev["devices"][0]["handle"]

    handle = await asyncio.wait_for(pick_and_answer(), 10)
    done = await drive(s, [f"It's {handle}", True, True])
    assert done["outcome"] == "connected", done
    names = [c[0] for c in calls]
    assert names.index("adb_connect") < names.index("set_private_dns")
    hostname = next(c[1]["hostname"] for c in calls if c[0] == "set_private_dns")
    assert hostname.endswith(".dns.pahra.app")
    dev = eng.conn.execute("SELECT * FROM devices WHERE id = ?", (done["device_id"],)).fetchone()
    assert dev["type"] == "tv" and dev["last_seen"] and hostname.startswith(dev["client_id"] + ".")
    await svc.stop()


async def test_declining_changes_nothing(env):
    eng, srv, calls, _ = env
    svc = AssistantService(eng, target=srv, genai_client=None)
    await svc.start()
    s = svc.create("fire tv")
    done = await drive(s, ["1", False, False])  # pick first, decline adb_connect, decline the laptop relay
    assert done["outcome"] == "guide"
    assert not any(c[0] in ("adb_connect", "set_private_dns", "start_dns_forwarder") for c in calls)
    assert eng.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
    await svc.stop()


async def test_guided_lg_gets_manual_guide_with_values(env):
    eng, srv, calls, _ = env
    svc = AssistantService(eng, target=srv, genai_client=None)
    await svc.start()
    s = svc.create("LG TV in the living room")
    first = None

    async def first_choice():
        while not any(e["type"] == "devices" for e in s.events):
            await asyncio.sleep(0.02)
        return next(e for e in s.events if e["type"] == "devices")["devices"][0]

    first = await asyncio.wait_for(first_choice(), 10)
    assert "LG" in first["label"]
    done = await drive(s, [f"It's {first['handle']}", False])  # decline the laptop relay
    assert done["outcome"] == "guide"
    g = done["guide"]
    assert g["title"].startswith("Manual DNS")  # router DNS unavailable behind CGNAT
    assert any("34.93.1.2" in x for x in g["steps"])  # placeholder filled
    assert any("LG webOS has no ADB" in x for x in g["steps"])
    assert not any("relay" in x.lower() for x in g["steps"])  # declined option isn't pushed again
    assert not any(c[0] in ("adb_connect", "set_private_dns", "start_dns_forwarder") for c in calls)
    assert eng.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
    await svc.stop()


async def test_guided_unknown_device_asks_for_brand(env):
    eng, _, _, _ = env
    srv, calls, _ = make_stub(devices=[ROUTER, UNKNOWN])
    svc = AssistantService(eng, target=srv, genai_client=None)
    await svc.start()
    s = svc.create("the tv in the hall")
    done = await drive(s, ["1", "It's a Samsung", False])
    assert done["outcome"] == "guide"
    opt_calls = [c[1] for c in calls if c[0] == "get_setup_options"]
    assert opt_calls and opt_calls[-1]["platform"] == "tizen"
    assert any("brand" in (e.get("prompt") or "") for e in s.events if e["type"] == "input_required")
    await svc.stop()


async def test_guided_lg_through_laptop_relay(env):
    eng, srv, calls, _ = env
    svc = AssistantService(eng, target=srv, genai_client=None)
    await svc.start()
    s = svc.create("LG TV in the living room")
    done = await drive(s, ["1", True, "done"])  # pick LG, allow the relay, "done" after typing the DNS
    assert done["outcome"] == "connected", s.events
    dev = eng.conn.execute("SELECT * FROM devices WHERE id = ?", (done["device_id"],)).fetchone()
    assert dev["setup_method"] == "forwarder" and dev["lan_ip"] == LG["ip"] and dev["last_seen"]
    start = [c[1] for c in calls if c[0] == "start_dns_forwarder"]
    assert start[-1]["mappings"] == [{"ip": LG["ip"], "client_id": dev["client_id"]}]
    assert start[-1]["dns_hostname"] == eng.settings.dns_hostname
    assert eng.forwarder["running"] and eng.forwarder["listen_ip"] == LAPTOP_IP
    said = " ".join(e.get("text", "") for e in s.events if e["type"] == "message")
    assert f"DNS server: {LAPTOP_IP}" in said and "New-NetFirewallRule" in said
    # restart: the relay comes back with the saved TV
    await svc.stop()
    calls.clear()
    svc2 = AssistantService(eng, target=srv, genai_client=None)
    await svc2.start()
    assert [c[0] for c in calls].count("start_dns_forwarder") == 1
    # deleting the TV stops the relay
    eng.conn.execute("DELETE FROM devices WHERE id = ?", (dev["id"],))
    assert (await forwarder.apply(eng))["running"] is False
    assert calls[-1][0] == "stop_dns_forwarder"
    await svc2.stop()


async def test_relay_hidden_from_model_but_setup_forwarder_offered(env):
    eng, srv, _, _ = env
    svc = AssistantService(eng, target=srv, genai_client=None)
    await svc.start()
    names = {d["name"] for d in build_declarations(svc.create("tv"))}
    assert not names & forwarder.FORWARDER_TOOLS
    assert "setup_forwarder" in names and "set_private_dns" in names
    await svc.stop()
    srv2, _, _ = make_stub(relay=False)
    svc = AssistantService(eng, target=srv2, genai_client=None)
    await svc.start()
    assert "setup_forwarder" not in {d["name"] for d in build_declarations(svc.create("tv"))}
    await svc.stop()


class FakeGemini:
    """Scripted model: each step looks at the conversation and returns one function call."""

    def __init__(self):
        self.sent: list[str] = []
        self.aio = self
        self.models = self
        self.step = 0

    @staticmethod
    def _last_result(contents) -> dict:
        for c in reversed(contents):
            for p in c.parts or []:
                if p.function_response:
                    return p.function_response.response.get("result") or {}
        return {}

    async def generate_content(self, model, contents, config):
        self.sent.append(json.dumps([c.model_dump(mode="json", exclude_none=True) for c in contents], default=str))
        last = self._last_result(contents)
        self.step += 1
        script = {
            1: ("get_network_context", {}),
            2: ("scan_network", {}),
        }
        if self.step in script:
            name, args = script[self.step]
        elif self.step == 3:
            fire = next(d for d in last["devices"] if d["platform"] == "fire_tv")
            self.fire = fire["ip"]  # a handle, e.g. host-5
            name, args = "show_device_choices", {"candidates": [{"handle": self.fire, "label": fire["friendly_name"]}]}
        elif self.step == 4:
            name, args = "probe_device", {"ip": self.fire}
        elif self.step == 5:
            name, args = "adb_connect", {"ip": self.fire}
        elif self.step == 6:
            name, args = "set_private_dns", {"ip": self.fire, "device_name": "Bedroom Fire TV", "brand": "Amazon"}
        elif self.step == 7:
            name, args = "verify_connected", {}
        else:
            name, args = "finish", {"outcome": "connected", "message": "Your Fire TV is protected."}
        part = types.Part(function_call=types.FunctionCall(name=name, args=args))
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])


async def test_agent_mode_never_leaks_ips_or_macs(env):
    eng, srv, calls, _ = env
    gem = FakeGemini()
    svc = AssistantService(eng, target=srv, genai_client=gem)
    await svc.start()
    s = svc.create("bedroom fire tv")
    done = await drive(s, ["that one", True, True])
    assert done["outcome"] == "connected", s.events
    blob = "\n".join(gem.sent)
    for secret in (FIRE["ip"], LG["ip"], FIRE["mac"], "192.168.29", "49.36.10.20", "100.72.14.9"):
        assert secret not in blob, secret
    assert "dns.pahra.app" not in blob  # hostname is injected by the backend, never chosen by the model
    # the real IP reached the tool
    assert ("adb_connect", {"ip": FIRE["ip"]}) in calls
    await svc.stop()


async def test_agent_failure_falls_back_to_guided(env):
    eng, srv, calls, _ = env

    class Broken:
        aio = models = None

        def __init__(self):
            self.aio = self
            self.models = self

        async def generate_content(self, **kw):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    svc = AssistantService(eng, target=srv, genai_client=Broken())
    await svc.start()
    s = svc.create("LG tv")
    done = await drive(s, ["1", False])
    assert done["outcome"] == "guide"
    assert any(e["type"] == "mode" and e["mode"] == "guided" for e in s.events)
    await svc.stop()


def test_http_endpoints(settings, fake_app):
    from fastapi.testclient import TestClient

    from pahra.main import create_app

    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    srv, _, _ = make_stub()
    app = create_app(settings, eng, background=False, assistant_factory=lambda e: AssistantService(e, target=srv, genai_client=None))
    H = {"Authorization": "Bearer dev-token"}
    with TestClient(app) as c:
        assert c.get("/api/companion/health", headers=H).json()["connected"]
        assert c.get("/api/health", headers=H).json()["companion"]["connected"]
        r = c.post("/api/assistant/sessions", json={"description": "LG tv"}, headers=H)
        assert r.status_code == 201 and r.json()["mode"] == "guided"
        sid = r.json()["session_id"]
        import time

        for _ in range(100):
            evs = c.get(f"/api/assistant/sessions/{sid}", headers=H).json()["events"]
            if any(e["type"] == "input_required" for e in evs):
                break
            time.sleep(0.05)
        assert c.post(f"/api/assistant/sessions/{sid}/messages", json={"text": "1"}, headers=H).json()["ok"]
        for _ in range(100):
            evs = c.get(f"/api/assistant/sessions/{sid}", headers=H).json()["events"]
            conf = [e for e in evs if e["type"] == "confirm_required"]
            if conf:
                break
            time.sleep(0.05)
        assert c.post(f"/api/assistant/sessions/{sid}/confirm", json={"call_id": "nope", "approve": False}, headers=H).status_code == 409
        assert c.post(f"/api/assistant/sessions/{sid}/confirm", json={"call_id": conf[0]["call_id"], "approve": False}, headers=H).json()["ok"]
        for _ in range(100):
            body = c.get(f"/api/assistant/sessions/{sid}", headers=H).json()
            if body["done"]:
                break
            time.sleep(0.05)
        assert body["done"] and body["events"][-1]["outcome"] == "guide"
        # EventSource can't send the sign-in: it opens the stream with a two-minute ticket
        ticket = c.post("/api/tickets", json={"path": f"/assistant/sessions/{sid}/stream"}, headers=H).json()["ticket"]
        with c.stream("GET", f"/api/assistant/sessions/{sid}/stream", params={"ticket": ticket}) as st:
            lines = [ln for ln in st.iter_lines() if ln.startswith("data: ")]
        assert json.loads(lines[-1][6:])["type"] == "done"
