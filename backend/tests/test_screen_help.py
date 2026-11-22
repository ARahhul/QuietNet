"""Setup: "Stuck? Show QuietNet your screen": an AI reads a pasted screenshot and says what to do next."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra.adguard.client import AdGuardClient
from pahra.engine import Engine
from pahra.main import create_app
from pahra.pipeline.explainer import Explainer, Image
from pahra.prompts import SCREEN_HELP

from .test_tracing import rec  # noqa: F401  (fixture)

H = {"Authorization": "Bearer dev-token"}
VM = "20.10.0.5"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PIC = "data:image/png;base64," + base64.b64encode(PNG).decode()


def _model(answer: dict | list | None, calls: list) -> httpx.MockTransport:
    """The model answers `answer` every time, or a list of answers in turn (the last one repeating)."""
    answers = answer if isinstance(answer, list) else [answer]

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        now = answers[min(len(calls), len(answers)) - 1]
        if now is None:
            return httpx.Response(500, text="model down")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(now)}}]})

    return httpx.MockTransport(handler)


@pytest.fixture
def make(settings, fake_app):
    """make(answer) -> (TestClient, calls to the model, engine)"""
    s = settings.model_copy(update={"vm_ip": VM})
    clients = []

    def _make(answer: dict | None = None, *, ai: bool = True, tracing=None):
        ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
        eng = Engine(s, ag=ag)
        calls: list = []
        if ai:
            eng.explainer = Explainer("k", "vision-1", base_url="https://gw.example/v1", transport=_model(answer, calls), tracing=tracing)
        c = TestClient(create_app(s, eng, background=False))
        c.__enter__()
        clients.append(c)
        return c, calls, eng

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def _device(c, **kw) -> dict:
    body = {"name": "Home router", "type": "router", "brand": "TP-Link", "setup_method": "ipv4_demo"} | kw
    r = c.post("/api/devices", headers=H, json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["device"]


def _texts(call: dict) -> str:
    out = []
    for m in call["messages"]:
        parts = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        out += [p["text"] for p in parts if p["type"] == "text"]
    return "\n".join(out)


TPLINK = {
    "screen": "TP-Link's DHCP settings page",
    "reply": "Here: type <DNS_SERVER> in the Primary DNS box on this page.",
    "on_track": True,
    "steps": ['Click "DHCP Server" in the left menu.', "Type <DNS_SERVER> in Primary DNS.", "Put 8.8.8.8 in Secondary DNS as a backup."],
    "fill": [
        {"field": "Primary DNS", "value": "<DNS_SERVER>"},
        {"field": "Secondary DNS", "value": "1.1.1.1"},
        {"field": "Wireless Password", "value": "hunter22"},
    ],
    "warning": "",
    "next": "the WAN page if the DNS boxes are greyed out",
    "done": False,
}


def test_reads_the_screen_and_fills_in_quietnets_values(make):
    c, calls, _ = make(TPLINK)
    dev = _device(c)
    r = c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "device_id": dev["id"], "question": "where do I type it?"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["screen"] == "TP-Link's DHCP settings page" and got["on_track"] is True and got["done"] is False
    assert got["reply"] == f"Here: type {VM} in the Primary DNS box on this page."
    # QuietNet's value filled in; another DNS server and the password field are dropped
    assert got["steps"] == ['Click "DHCP Server" in the left menu.', f"Type {VM} in Primary DNS."]
    assert got["fill"] == [{"field": "Primary DNS", "value": VM}]
    assert got["next"] == "the WAN page if the DNS boxes are greyed out"

    call = calls[0]
    assert call["messages"][0] == {"role": "system", "content": SCREEN_HELP.messages[0]["content"]}
    user = call["messages"][-1]["content"]
    assert [p["type"] for p in user] == ["text", "image_url"]
    assert user[1]["image_url"]["url"] == PIC
    text = _texts(call)
    assert "plain DNS on the home WiFi router" in text and "where do I type it?" in text
    # The value is given once, to check the screen against; QuietNet's steps use the placeholder
    assert text.count(VM) == 1 and f"<DNS_SERVER> = {VM}" in text
    assert "set Primary DNS to <DNS_SERVER>" in text


def test_hostname_is_given_once_and_written_as_a_placeholder(make):
    answer = {"screen": "ASUS WAN page", "steps": ["Add the server."], "fill": [{"field": "TLS Hostname", "value": "<TLS_HOSTNAME>"}]}
    c, calls, eng = make(answer)
    dev = _device(c, name="Asus router", brand="ASUS", setup_method="dot")
    host = f"{dev['client_id']}.{eng.settings.dns_hostname}"
    earlier = [{"question": "", "screen": "ASUS login page", "steps": [f"Type {host} under TLS Hostname."]}]
    got = c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "device_id": dev["id"], "history": earlier}).json()
    assert got["fill"] == [{"field": "TLS Hostname", "value": host}]
    text = _texts(calls[0])
    # Only in the values line: QuietNet's steps and the earlier answer carry the placeholder
    assert text.count(host) == 1 and f"<TLS_HOSTNAME> = {host}" in text
    assert "TLS Hostname <TLS_HOSTNAME>" in text and "Type <TLS_HOSTNAME> under TLS Hostname." in text
    assert "DNS-over-TLS" in text and "Earlier in this conversation" in text and "ASUS login page" in text


def test_before_the_router_is_added_no_dns_values(make):
    """AdGuard only answers homes that are added: changing DNS first would cut the home off."""
    answer = {
        "screen": "JioFiber LAN page",
        "steps": ["Open Network → LAN.", "Enter <DNS_SERVER> in Primary DNS."],
        "fill": [{"field": "Primary DNS", "value": "<DNS_SERVER>"}],
    }
    c, calls, _ = make(answer)
    got = c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "isp": "Jio", "router": ""}).json()
    assert got["steps"][1] == "Enter the DNS server QuietNet shows once you add the router in Primary DNS."
    assert got["fill"] == []  # nothing real to copy yet
    text = _texts(calls[0])
    assert "haven't added the router" in text and "cut their internet off" in text and "Set up" in text
    assert "Their internet provider: Jio." in text and "none yet" in text and VM not in text


# What a weak model said on TP-Link's DHCP page, which does have the DNS boxes (seen live, 2026-11-22)
WRONG_PAGE = {
    "screen": "TP-Link's DHCP Server settings page",
    "dns_boxes": ["Primary DNS", "Secondary DNS"],
    "reply": "This page is for DHCP settings, not DNS.",
    "on_track": False,
    "steps": ['Click "Advanced" in the top left.', 'Click "-LAN Settings" in the left menu.'],
    "fill": [],
    "next": "the LAN Settings page",
}


def test_a_page_with_dns_boxes_is_never_the_wrong_page(make):
    """It saw Primary DNS on the page yet said "go elsewhere": asked once more, and it answers for this page."""
    right = {
        "screen": "TP-Link's DHCP Server settings page",
        "dns_boxes": ["Primary DNS", "Secondary DNS"],
        "reply": "Here: Primary DNS on this page.",
        "on_track": True,
        "steps": ["Type <DNS_SERVER> in Primary DNS.", "Leave Secondary DNS empty.", 'Click "Save".'],
        "fill": [{"field": "Primary DNS", "value": "<DNS_SERVER>"}],
    }
    c, calls, _ = make([WRONG_PAGE, right])
    dev = _device(c)
    got = c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "device_id": dev["id"], "question": "what do I do here?"}).json()
    assert len(calls) == 2
    assert "contradicted itself" not in _texts(calls[0])
    assert "you saw DNS boxes (Primary DNS, Secondary DNS)" in _texts(calls[1])
    assert got["on_track"] is True and got["steps"][0] == f"Type {VM} in Primary DNS." and got["fill"] == [{"field": "Primary DNS", "value": VM}]


def test_if_it_still_says_wrong_page_it_isnt_sent_away(make):
    c, calls, _ = make(WRONG_PAGE)  # the same answer twice
    got = c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "isp": "ACT", "router": "TP-Link"}).json()
    assert len(calls) == 2
    assert got["on_track"] is True and got["next"] == ""
    assert got["reply"].startswith("This is the right page: Primary DNS and Secondary DNS are here.")
    assert "Set up" in got["reply"]  # the router isn't added yet: values first
    assert got["steps"][1].startswith('In QuietNet, click "Set up …"')


def test_on_the_dns_page_before_the_router_is_added_nothing_changes_yet(make):
    """Seen live: it said to tick "Use the following DNS addresses" and re-type the gateway before the router was added."""
    answer = {
        "screen": "TP-Link's Internet settings page",
        "dns_boxes": ["Primary DNS", "Secondary DNS"],
        "reply": "No, leave the Default Gateway as it is.",
        "on_track": True,
        "steps": ["Tick 'Use the following DNS addresses'", "Type 192.168.0.1 in Default Gateway", "Click 'Save'"],
        "fill": [{"field": "Use the following DNS addresses", "value": "tick"}, {"field": "Default Gateway", "value": "192.168.0.1"}],
        "warning": "Changing this now will cut off your internet.",
        "next": "the page after Save",
    }
    c, calls, _ = make(answer)
    asked = c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "isp": "ACT", "router": "TP-Link", "question": "shld i change the default gateway ?"}).json()
    assert len(calls) == 1
    # their question still gets its answer, then what comes first
    assert asked["reply"] == 'No, leave the Default Gateway as it is. Before changing anything here, click QuietNet\'s "Set up …" button.'
    assert asked["fill"] == [] and asked["warning"] == "" and asked["next"] == ""
    assert asked["steps"] == [
        "Keep this page open and don't change anything on it yet.",
        'In QuietNet, click "Set up …" under the guide: it adds your router and shows what to type in Primary DNS.',
        "Come back to this page and fill it in as QuietNet shows.",
    ]
    plain = c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "isp": "ACT", "router": "TP-Link"}).json()
    assert plain["reply"].startswith("This is the right page: Primary DNS and Secondary DNS are here. Keep it open")


def test_menu_bullets_are_dropped_and_a_right_answer_is_asked_once(make):
    answer = {"screen": "TP-Link's Advanced page", "dns_boxes": [], "on_track": False, "steps": ['Click "-LAN Settings" in the left menu.', 'Click "• DHCP Server".']}
    c, calls, _ = make(answer)
    got = c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "isp": "ACT", "router": "TP-Link"}).json()
    assert len(calls) == 1 and got["on_track"] is False
    assert got["steps"] == ['Click "LAN Settings" in the left menu.', 'Click "DHCP Server".']


def test_the_trace_never_holds_the_picture(make, rec):  # noqa: F811
    c, _, _ = make(TPLINK, tracing=rec.tracing)
    dev = _device(c)
    c.post("/api/setup/screenshot", headers=H, json={"image": PIC, "device_id": dev["id"]})
    spans = rec.spans()
    root = next(s for s in spans if s["root"])
    gen = next(s for s in spans if s["type"] == "generation")
    assert root["langfuse.trace.name"] == "screen-help" and "setup" in root["langfuse.trace.tags"]
    assert json.loads(root["langfuse.observation.output"])["dropped"] == 3
    assert "[screenshot: image/png, 1 KB]" in gen["langfuse.observation.input"]
    b64 = PIC.split(",", 1)[1]
    assert not any(b64 in str(v) for s in spans for v in s.values())


@pytest.mark.parametrize(
    "image, msg",
    [
        ("hello", "Send a PNG, JPEG or WebP picture."),
        ("data:image/png;base64,@@@", "didn't come through"),
        ("data:image/jpeg;base64," + base64.b64encode(PNG).decode(), "isn't a picture"),
    ],
)
def test_bad_pictures_are_refused_plainly(make, image, msg):
    c, calls, _ = make(TPLINK)
    r = c.post("/api/setup/screenshot", headers=H, json={"image": image})
    assert r.status_code == 422 and msg in r.json()["detail"] and not calls


def test_without_an_ai_key(make):
    c, _, _ = make(ai=False)
    r = c.post("/api/setup/screenshot", headers=H, json={"image": PIC})
    assert r.status_code == 503 and "AI isn't set up" in r.json()["detail"]


def test_when_the_model_fails(make):
    c, _, _ = make(None)
    r = c.post("/api/setup/screenshot", headers=H, json={"image": PIC})
    assert r.status_code == 502 and "Try again" in r.json()["detail"]


async def test_gemini_gets_the_picture_as_inline_data():
    ex = Explainer("fake-key", "gemini-x")
    sent = {}

    async def generate_content(**kw):
        sent.update(kw)
        return SimpleNamespace(text='{"screen": "ok"}', usage_metadata=None)

    ex.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    out = await ex.ask_json(SCREEN_HELP, images=[Image("image/png", PNG)], context="ctx", history="", question="q")
    assert out == {"screen": "ok"}
    first, last = sent["contents"][0], sent["contents"][-1]
    assert first.inline_data.mime_type == "image/png" and first.inline_data.data == PNG
    assert "Their screenshot is attached." in last.text
