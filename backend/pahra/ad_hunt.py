""""Saw an ad?": the person says where and when they saw ads, and the AI picks, from what their devices looked up
around then, the names that most likely served them.

That reaches ads the lists and Auto-mode can't: an app's own ad servers live under the app's domain, which QuietNet
keeps allowed so the app works (Hotstar's ad API is under hotstar.com, on the must-allow list).

* Only names really looked up in that window, and still answered, can come back: the model's answer is filtered
  against that list, so it can't invent a name or hand back a whole app domain.
* Nothing is blocked here. The dashboard shows each name with what may break, and the person picks.
"""

import logging
import re
import time
from datetime import datetime, timezone

from .knowledge import is_must_allow_any_device
from .prompts import AD_HUNT
from .timeutil import to_iso

log = logging.getLogger("pahra.ad_hunt")

MAX_NAMES = 160  # answered names shown to the model
MAX_BLOCKED = 40
MAX_CANDIDATES = 6
SLACK_S = 120  # "just now" still covers the poller's lag
ROLES = ("ad server", "ad decisions", "ad images", "ad tracking", "ad network")
# Never the ad, whatever the model says: DNS itself, time, reverse lookups
NEVER = re.compile(r"(^|\.)(root-servers\.net|in-addr\.arpa|ip6\.arpa|ipv4only\.arpa|ntp\.org|pool\.ntp\.org)$")
IN_STREAM = re.compile(r"ssai|(^|\.)dai\.google\.com$")
ADISH = re.compile(r"(^|[.-])(ads?|adserver|adservice|banner|brand|promo|sponsor|pagead|doubleclick)([.-]|\d|$)")
STOP = {"app", "the", "ads", "ad", "banner", "banners", "home", "screen", "page", "website", "site", "video", "on", "in",
        "of", "my", "and", "before", "after", "while", "when", "watching", "show", "shows", "playing", "a", "an", "phone",
        "tv", "laptop", "saw", "see", "seeing", "popup", "pop", "up", "top", "bottom", "every", "time", "with", "at"}


class HuntFailed(RuntimeError):
    """The model call failed or didn't answer in JSON."""


def keywords(where: str) -> list[str]:
    """Words that name the app or site ("Hotstar app, home screen banner" -> ["hotstar"])."""
    return [w for w in re.findall(r"[a-z0-9]{3,}", where.lower()) if w not in STOP]


def recent_names(conn, user_id: str, since: float, device_id: str | None = None) -> list[dict]:
    """Every name the household (or one device) looked up since `since`, with what QuietNet knows about it."""
    sql = (
        "SELECT q.qname, q.domain, COUNT(*) AS n, SUM(q.status = 'blocked') AS blocked, MAX(q.t) AS last, "
        "d.category, d.owner FROM queries q JOIN devices v ON v.id = q.device_id "
        "LEFT JOIN domains d ON d.domain = q.domain WHERE v.user_id = ? AND q.t >= ?"
    )
    args: list = [user_id, since]
    if device_id:
        sql += " AND q.device_id = ?"
        args.append(device_id)
    rows = conn.execute(sql + " GROUP BY q.qname", args).fetchall()
    out = []
    for r in rows:
        name = (r["qname"] or "").strip(".").lower()
        if name and not NEVER.search(name):
            out.append({**dict(r), "qname": name, "answered": r["blocked"] < r["n"]})
    return out


def _rank(rows: list[dict], words: list[str]) -> list[dict]:
    def score(r: dict) -> tuple:
        about = f"{r['qname']} {r['owner'] or ''}".lower()
        named = any(w in about for w in words)
        return (not named, not (ADISH.search(r["qname"]) or r["category"] in ("ads", "tracking")), -r["last"], -r["n"])

    return sorted(rows, key=score)


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s}s ago" if s < 90 else f"{s // 60} min ago"


def _pick(v, allowed: tuple[str, ...], default: str) -> str:
    v = str(v or "").strip().lower()
    return v if v in allowed else default


def _text(v, n: int = 240) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) else ""


def _entry(name: str, row: dict, *, role: str, why: str, conf: str, risk: str, break_note: str) -> dict:
    return {
        "name": name,
        "owner": row["owner"],
        "role": role,
        "why": why,
        "confidence": conf,
        "break_risk": risk,
        "break_note": break_note,
        "must_allow": is_must_allow_any_device(name),
        "lookups": row["n"],
        "last_seen": to_iso(datetime.fromtimestamp(row["last"], timezone.utc)),
        # Ticked for the person only when QuietNet itself backs the AI up: the name says ads, or the lists
        # already call it ads or tracking. The model is often sure about image hosts and company APIs that
        # also carry the app's normal content; those are shown, unticked.
        "selected": conf != "low" and risk != "high" and (bool(ADISH.search(name)) or row["category"] in ("ads", "tracking")),
    }


def clean(answer: dict, answered: dict[str, dict]) -> dict:
    """The model's answer, keeping only names that were really looked up and are still answered."""
    out, seen = [], set()
    for c in answer.get("candidates") if isinstance(answer.get("candidates"), list) else []:
        if not isinstance(c, dict):
            continue
        name = _text(c.get("name"), 253).lower().strip(".")
        row = answered.get(name)
        if not row or name in seen:
            continue
        seen.add(name)
        out.append(
            _entry(
                name,
                row,
                role=_pick(c.get("role"), ROLES, "ad server"),
                why=_text(c.get("why"), 220),
                conf=_pick(c.get("confidence"), ("high", "medium", "low"), "low"),
                risk=_pick(c.get("break_risk"), ("low", "medium", "high"), "medium"),
                break_note=_text(c.get("break_note"), 160),
            )
        )
        if len(out) == MAX_CANDIDATES:
            break
    return {
        "summary": _text(answer.get("summary"), 300),
        "candidates": out,
        "in_stream": answer.get("in_stream") is True,
        "note": _text(answer.get("note"), 300),
    }


def app_first(candidates: list[dict], rows: list[dict], words: list[str]) -> list[dict]:
    """The named app's own ad-named hosts always make the list (the model sometimes wanders off to other apps'
    trackers), and the app's names come first."""
    if not words:
        return candidates
    have = {c["name"] for c in candidates}
    app = words[0].capitalize()
    extra = []
    for r in rows:
        n = r["qname"]
        if n in have or not any(w in n for w in words) or not ADISH.search(n):
            continue
        images = re.search(r"img|image|brand|banner|promo", n)
        extra.append(
            _entry(
                n,
                r,
                role="ad images" if images else "ad server",
                why=f"A {app} name that says it's for {'brand or ad pictures' if images else 'ads'}, looked up around then.",
                conf="medium",
                risk="medium",
                break_note="Pictures in ad spaces may go blank." if images else "Ads in the app may stop loading.",
            )
        )
    ordered = sorted(candidates + extra, key=lambda c: not any(w in c["name"] for w in words))
    return ordered[:MAX_CANDIDATES]


IN_STREAM_NOTE = (
    "Some of these ads are stitched into the video itself (live TV and sports especially). No DNS filter can remove "
    "those without stopping the video."
)


async def hunt(explainer, conn, user_id: str, *, where: str, minutes: int, device: dict | None = None, note: str = "") -> dict:
    now = time.time()
    rows = recent_names(conn, user_id, now - minutes * 60 - SLACK_S, device["id"] if device else None)
    answered = {r["qname"]: r for r in rows if r["answered"]}
    base = {"where": where, "minutes": minutes, "looked_at": len(answered)}
    if not answered:
        who = device["name"] if device else "your devices"
        return base | {
            "summary": f"QuietNet saw no lookups from {who} in the last {minutes} minutes, so there's nothing to check. "
            "Is the device on your home WiFi (or set up with QuietNet)? Open the app again and retry.",
            "candidates": [],
            "in_stream": False,
            "note": "",
        }
    words = keywords(where)
    shown = _rank(list(answered.values()), words)[:MAX_NAMES]
    blocked = sorted(r["qname"] for r in rows if not r["answered"])[:MAX_BLOCKED]
    lines = [
        f"{r['qname']} | {r['owner'] or '-'} | {r['category'] or 'not checked yet'} | {r['n']} | {_ago(now - r['last'])}"
        for r in shown
    ]
    meta = {"where": where, "minutes": minutes, "names": len(shown), "blocked": len(blocked), "device": device["type"] if device else None}
    with explainer.tracing.trace("ad-hunt", input=meta, tags=("ads",)) as t:
        try:
            answer = await explainer.ask_json(
                AD_HUNT,
                where=_text(where, 120),
                note=f"\nWhat they noticed: {_text(note, 200)}" if note.strip() else "",
                minutes=minutes,
                device=f", on {_text(device['name'], 60)} ({device['type']})" if device else ", on any device at home",
                names="\n".join(lines),
                blocked="\n".join(blocked) or "(none)",
            )
        except Exception as e:
            log.warning("ad hunt failed: %s: %s", e.__class__.__name__, str(e)[:300])
            t.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}"[:500])
            raise HuntFailed(str(e)) from e
        got = clean(answer, {r["qname"]: r for r in shown})
        got["candidates"] = app_first(got["candidates"], shown, words)
        streams = [n for n in answered if IN_STREAM.search(n)]
        if streams and (not words or any(w in n for n in streams for w in words)):
            got["in_stream"] = True  # the app they named is playing video with ads stitched in (e.g. Hotstar's SSAI)
        if got["in_stream"] and not got["note"]:
            got["note"] = IN_STREAM_NOTE
        t.update(output={"candidates": [c["name"] for c in got["candidates"]], "in_stream": got["in_stream"]})
    return base | got
