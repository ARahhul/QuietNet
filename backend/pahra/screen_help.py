"""Setup → "Stuck? Show QuietNet your screen".

The person pastes a screenshot (or a photo of a TV screen or a router sticker). The AI reads it and says what
to do next on that screen, knowing what they're setting up and what it already told them.

* The picture goes only to the configured AI model. QuietNet doesn't store it, and Langfuse traces show
  "[screenshot: image/jpeg, 210 KB]" instead of the picture.
* The model is told this device's QuietNet values once, so it can check what the screen shows, but everywhere
  else (QuietNet's steps, earlier answers) and in its reply they are placeholders (<DNS_SERVER>, <TLS_HOSTNAME>,
  <DOH_URL>, <TV_DNS_SERVER>), filled in here, so a long hostname can't come back mistyped.
* Advice that points at any other public DNS server is dropped, and so is any field that asks for a password.
"""

import base64
import binascii
import ipaddress
import re

from .pipeline.explainer import Explainer, Image
from .prompts import SCREEN_HELP
from .setup_guides import setup_guide

MAX_BYTES = 5 * 1024 * 1024
MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff", "image/webp": b"RIFF"}

PLACEHOLDER = {"server_ip": "<DNS_SERVER>", "dot_hostname": "<TLS_HOSTNAME>", "doh_url": "<DOH_URL>", "forwarder_ip": "<TV_DNS_SERVER>"}
MEANING = {
    "<DNS_SERVER>": "QuietNet's DNS server address",
    "<TLS_HOSTNAME>": "the TLS / Private DNS hostname for this device",
    "<DOH_URL>": "the DNS-over-HTTPS URL for this device",
    "<TV_DNS_SERVER>": "the DNS server address the TV must use (the laptop relaying for it)",
}
# A placeholder the model used although this setup doesn't have that value yet
UNKNOWN = {
    "<TLS_HOSTNAME>": "the TLS hostname QuietNet shows once you add the router",
    "<DOH_URL>": "the DNS-over-HTTPS URL QuietNet shows for this device",
    "<TV_DNS_SERVER>": "the address QuietNet shows for the TV",
    "<DNS_SERVER>": "QuietNet's DNS server address",
}
METHOD = {
    "dot_router": "encrypted DNS (DNS-over-TLS) on the home WiFi router",
    "dot": "Android Private DNS",
    "doh_apple": "an Apple configuration profile",
    "doh": "encrypted DNS (DNS-over-HTTPS) in Windows and the browser",
    "adb": "Private DNS on an Android / Fire TV, set over ADB",
    "forwarder": "the TV's DNS server set to a laptop that relays for it",
    "plain": "plain DNS on the home WiFi router (Primary DNS)",
}
# Well-known public resolvers by name (their IPs are caught by the public-IP rule)
OTHER_RESOLVERS = re.compile(r"dns\.google|cloudflare-dns|one\.one\.one\.one|quad9|opendns|nextdns|adguard-dns|dns\.adguard", re.I)
SECRET_FIELD = re.compile(r"pass(word|phrase)?|pwd|pre-?shared|wpa|security key|network key", re.I)
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


class BadImage(ValueError):
    """The upload isn't a picture we can send (the message is shown to the user)."""


class ReadFailed(RuntimeError):
    """The model call failed or didn't answer in JSON."""


def decode_image(data_url: str) -> Image:
    m = re.match(r"^data:(image/(?:png|jpeg|webp));base64,", data_url or "")
    if not m:
        raise BadImage("Send a PNG, JPEG or WebP picture.")
    try:
        raw = base64.b64decode(data_url[m.end() :], validate=True)
    except (binascii.Error, ValueError):
        raise BadImage("That picture didn't come through. Try pasting it again.") from None
    if len(raw) > MAX_BYTES:
        raise BadImage("That picture is too large: 5 MB at most.")
    mime = m.group(1)
    if not raw.startswith(MAGIC[mime]) or (mime == "image/webp" and raw[8:12] != b"WEBP"):
        raise BadImage("That file isn't a picture QuietNet can read.")
    return Image(mime, raw)


def _text(v, n: int = 240) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) else ""


def _list(v) -> list:
    return v if isinstance(v, list) else []


def mask(text: str, values: dict[str, str]) -> str:
    """Real values -> placeholders (what the model may see)."""
    for ph, real in sorted(values.items(), key=lambda kv: -len(kv[1])):
        if len(real) >= 4:
            text = text.replace(real, ph)
    return text


def unmask(text: str, values: dict[str, str]) -> str:
    """Placeholders -> real values (what the person sees)."""
    for ph, real in values.items():
        text = text.replace(ph, real)
    return re.sub(r"<[A-Z_]{3,20}>", lambda m: UNKNOWN.get(m.group(0), m.group(0)), text)


def _foreign_dns(text: str, values: dict[str, str]) -> bool:
    """True when the text points somewhere QuietNet doesn't: another public DNS server."""
    if OTHER_RESOLVERS.search(text):
        return True
    ours = set(values.values())
    for ip in IPV4.findall(text):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_global and ip not in ours:
            return True
    return False


def method_key(device: dict) -> str:
    m, brand = device["setup_method"], (device.get("brand") or "").lower()
    if m == "dot":
        return "dot_router" if device.get("type") == "router" else "dot"
    if m == "doh":
        return "doh_apple" if brand == "apple" else "doh"
    return m if m in ("adb", "forwarder") else "plain"


def context_for(settings, device: dict | None, forwarder_ip: str | None, isp: str, router: str) -> tuple[str, dict[str, str]]:
    """What they're setting up, in placeholders, and placeholder -> real value."""
    if device:
        g = setup_guide(settings, device, forwarder_ip)
        values = {PLACEHOLDER[k]: str(v) for k, v in g["values"].items() if k in PLACEHOLDER and v}
        lines = [
            f"They are connecting: {_text(device['name'], 60)} ({device.get('type') or 'device'}"
            + (f", {_text(device['brand'], 30)}" if device.get("brand") else "")
            + ").",
            f"Method: {METHOD[method_key(device)]}.",
            "QuietNet's steps for it:",
            *(f"{i}. {mask(s, values)}" for i, s in enumerate(g["steps"], 1)),
        ]
    else:
        values = {"<DNS_SERVER>": settings.vm_ip} if settings.vm_ip else {}
        lines = [
            "They are setting up QuietNet for the whole home on their WiFi router: getting into its admin page "
            "and finding the DNS settings. They haven't added the router in QuietNet yet."
        ]
    if isp:
        lines.append(f"Their internet provider: {_text(isp, 60)}.")
    if router:
        lines.append(f"Their own WiFi router: {_text(router, 60)}.")
    lines.append(
        "QuietNet's values, so you can recognise them on screen (in steps and fill write the placeholder, never the value): "
        + ("; ".join(f"{k} = {v} ({MEANING[k]})" for k, v in values.items()) or "none yet")
    )
    return "\n".join(lines), values


def history_text(history: list[dict], values: dict[str, str]) -> str:
    """The earlier turns of this conversation, so the model can carry on from them."""
    turns = []
    for i, h in enumerate(history[-4:], 1):
        said = "; ".join(_text(s, 200) for s in _list(h.get("steps"))[:6])
        q = _text(h.get("question"), 200)
        turns.append(
            mask(f"{i}. Screen: {_text(h.get('screen'), 200) or 'unclear'}." + (f' They asked: "{q}".' if q else "") + f" You said: {said or 'nothing'}", values)
        )
    return ("\nEarlier in this conversation (oldest first):\n" + "\n".join(turns) + "\n") if turns else ""


def clean(answer: dict, values: dict[str, str]) -> tuple[dict, int]:
    """The model's answer, checked and with QuietNet's values filled in; and how many items were dropped."""
    dropped = 0

    def keep(t: str) -> bool:
        nonlocal dropped
        bad = _foreign_dns(t, values)
        dropped += bad
        return bool(t) and not bad

    steps = [s for s in (unmask(_text(x), values) for x in _list(answer.get("steps"))[:6]) if keep(s)]
    fill = []
    for f in _list(answer.get("fill"))[:6]:
        if not isinstance(f, dict):
            continue
        field, value = _text(f.get("field"), 60), unmask(_text(f.get("value"), 160), values)
        if not field or not value:
            continue
        if SECRET_FIELD.search(field):
            dropped += 1
            continue
        if keep(value):
            fill.append({"field": field, "value": value})
    reply = unmask(_text(answer.get("reply"), 300), values)
    warning = unmask(_text(answer.get("warning")), values)
    nxt = unmask(_text(answer.get("next"), 200), values)
    out = {
        "screen": unmask(_text(answer.get("screen"), 200), values) or "I couldn't tell what this screen is.",
        "reply": reply if reply and keep(reply) else "",
        "on_track": answer.get("on_track") is not False,
        "steps": steps,
        "fill": fill,
        "warning": warning if warning and keep(warning) else "",
        "next": nxt if nxt and keep(nxt) else "",
        "done": answer.get("done") is True,
    }
    return out, dropped


async def read_screen(
    explainer: Explainer,
    settings,
    image: Image,
    *,
    device: dict | None = None,
    forwarder_ip: str | None = None,
    isp: str = "",
    router: str = "",
    question: str = "",
    history: list[dict] | None = None,
) -> dict:
    history = history or []
    context, values = context_for(settings, device, forwarder_ip, isp, router)
    meta = {
        "setting_up": method_key(device) if device else "home-wifi-login",
        "device_type": device.get("type") if device else "router",
        "isp": isp or None,
        "router": router or None,
        "question": question or None,
        "turn": len(history) + 1,
    }
    with explainer.tracing.trace("screen-help", input=meta, tags=("setup",)) as t:
        try:
            answer = await explainer.ask_json(
                SCREEN_HELP,
                images=[image],
                context=context,
                history=history_text(history, values),
                question=mask(_text(question, 300), values) or "(none: they want to know what to do next)",
            )
        except Exception as e:
            t.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}"[:500])
            raise ReadFailed(str(e)) from e
        out, dropped = clean(answer, values)
        # The model's own reply (placeholders, no real values) is on the generation; here just the outcome.
        t.update(output={k: out[k] for k in ("screen", "on_track", "done")} | {"steps": len(out["steps"]), "fill": len(out["fill"]), "dropped": dropped})
    return out
