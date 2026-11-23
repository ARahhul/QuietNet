""""Site not working?": the person names the site or app that won't work (or pastes a screenshot of it failing),
and QuietNet finds what it blocked around then that the site needs, so they can let it through.

Nothing stays blocked for good. Whatever stopped a name can be lifted from here: one of QuietNet's own rules (the
person's, Auto-mode's, or a hold on a brand-new domain) or one of the DNS server's block lists. Unblocking writes an
allow rule for the household or one device. AdGuard puts allow rules above every block, Auto-mode and holds leave a
name the person allowed alone (pahra.pipeline.policy), and the unblock itself can be undone.

* Only names really blocked in that window come back: the model's answer is filtered against that list.
* Without AI, or when the AI fails, the blocked names are still listed, the site's own first, so a broken site is
  never a dead end. Only reading a screenshot needs the AI.
* The screenshot goes only to the AI model. QuietNet doesn't keep it (as in pahra.screen_help).
* The site's own domain is let through whole when one of QuietNet's rules blocked all of it (a wrong block from
  Suggestions); anything else only by the exact name the site looked up, so the rest of a tracker stays blocked.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

from .adguard.rules import validate_domain
from .db import now_iso
from .decisions import active_decision, create_decision
from .pipeline.explainer import Image
from .pipeline.lists import parents
from .prompts import SITE_FIX
from .queries import _decision_index
from .timeutil import to_iso

log = logging.getLogger("pahra.site_fix")

SLACK_S = 120  # "just now" still covers the poller's lag
MAX_BLOCKED = 80  # blocked names shown to the model
MAX_ANSWERED = 40  # names that worked, for context
MAX_CANDIDATES = 8
RISKY = {"ads", "tracking", "telemetry", "malware"}
LABEL = {"yours": "your rule", "auto": "Auto-mode", "held": "held as new", "list": "a block list"}
WARNING = {
    "malware": "It's on a malware list. Unblock it only if you trust this site.",
    "ads": "Usually an ad name. Unblock it only if the site still breaks without it.",
    "tracking": "Usually a tracker. Unblock it only if the site still breaks without it.",
    "telemetry": "Usually sends usage data. Unblock it only if the site still breaks without it.",
}
STOP = {
    "the", "and", "for", "from", "with", "this", "that", "my", "our", "its", "not", "isn", "doesn", "won", "wont", "can",
    "cant", "cannot", "don", "dont", "didn", "work", "works", "working", "load", "loads", "loading", "open", "opens",
    "opening", "show", "shows", "showing", "page", "pages", "site", "website", "app", "apps", "www", "com", "net",
    "org", "http", "https", "html", "error", "errors", "blank", "stuck", "broken", "keeps", "since", "today", "now",
    "after", "before", "when", "while", "why", "what", "just", "any", "all", "login", "log", "sign", "signin", "pay",
    "payment", "payments", "video", "videos", "phone", "laptop", "tv", "home", "screen", "button", "link", "links",
    "anymore", "also", "even", "still", "get", "getting", "gets", "says", "said", "saying", "tried", "try", "trying",
}


class SiteFixFailed(RuntimeError):
    """The model call failed or didn't answer in JSON."""


class NeedsAI(RuntimeError):
    """Only a screenshot was sent and this server has no AI to read it."""


def keywords(what: str) -> list[str]:
    """Words that name the site ("IRCTC payment page won't load" -> ["irctc"])."""
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]{3,}", what.lower()):
        if w not in STOP and w not in out:
            out.append(w)
    return out[:4]


def _named(e: dict, words: list[str]) -> bool:
    about = f"{e['qname']} {(e.get('owner') or '').lower()}"
    return any(w in about for w in words)


def _now(per_device: dict, household: dict, device_id: str, qname: str) -> tuple[str, dict | None]:
    """What QuietNet itself does to `qname` on this device now: ('allowed'|'rule'|'none', the rule).

    As in AdGuard, an allow rule on the name or any parent wins over every block.
    """
    names = parents(qname)[:-1] or [qname]
    rules = [d for p in names for d in (per_device.get((device_id, p)), household.get(p)) if d]
    if any(d["action"] == "allow" for d in rules):
        return "allowed", None
    block = next((d for d in rules if d["action"] in ("block", "quarantine")), None)
    return ("rule", block) if block else ("none", None)


def blocked_names(conn: sqlite3.Connection, user_id: str, since: float, device_id: str | None = None) -> tuple[list[dict], list[str]]:
    """Names blocked for the household (or one device) since `since` that are still blocked now, and the names
    that were blocked then but have been let through since."""
    sql = (
        "SELECT q.qname, q.device_id, v.name AS device_name, COUNT(*) AS n, MAX(q.t) AS last, "
        "SUM(IFNULL(q.blocked_by, 'list') = 'list') AS by_list, d.category, d.owner "
        "FROM queries q JOIN devices v ON v.id = q.device_id LEFT JOIN domains d ON d.domain = q.domain "
        "WHERE v.user_id = ? AND q.t >= ? AND q.status = 'blocked' AND IFNULL(q.blocked_by, '') != 'parental'"
    )
    args: list = [user_id, since]
    if device_id:
        sql += " AND q.device_id = ?"
        args.append(device_id)
    per_device, household = _decision_index(conn, user_id)
    by_name: dict[str, dict] = {}
    for r in conn.execute(sql + " GROUP BY q.qname, q.device_id", args).fetchall():
        name = (r["qname"] or "").strip(".").lower()
        if not name:
            continue
        state, rule = _now(per_device, household, r["device_id"], name)
        if state == "none" and not r["by_list"]:
            state = "allowed"  # only QuietNet's rule ever blocked it, and that rule is gone
        e = by_name.setdefault(
            name, {"qname": name, "owner": r["owner"], "category": r["category"], "n": 0, "last": 0.0, "devices": [], "rule": None, "open": False}
        )
        if state == "allowed":
            continue
        e["open"] = True
        e["n"] += r["n"]
        e["last"] = max(e["last"], r["last"])
        e["devices"].append({"id": r["device_id"], "name": r["device_name"]})
        if rule and not e["rule"]:
            e["rule"] = rule
    still, since_let = [], []
    for e in by_name.values():
        if not e.pop("open"):
            since_let.append(e["qname"])
            continue
        rule = e["rule"]
        e["blocked_by"] = ("held" if rule["action"] == "quarantine" else "auto" if rule["decided_by"] == "auto" else "yours") if rule else "list"
        still.append(e)
    return still, sorted(since_let)


def parental_names(conn: sqlite3.Connection, user_id: str, since: float, device_id: str | None = None) -> list[str]:
    """Names parental controls blocked since `since` (for the household, or one device). These aren't offered for
    unblocking: a parent changes them on the Parental page."""
    sql = (
        "SELECT q.qname, COUNT(*) AS n FROM queries q JOIN devices v ON v.id = q.device_id "
        "WHERE v.user_id = ? AND q.t >= ? AND q.status = 'blocked' AND q.blocked_by = 'parental'"
    )
    args: list = [user_id, since]
    if device_id:
        sql += " AND q.device_id = ?"
        args.append(device_id)
    rows = conn.execute(sql + " GROUP BY q.qname ORDER BY n DESC LIMIT 20", args).fetchall()
    return list(dict.fromkeys(r["qname"].strip(".").lower() for r in rows if r["qname"]))


def answered_names(conn: sqlite3.Connection, user_id: str, since: float, device_id: str | None, words: list[str]) -> list[dict]:
    """Names that did get an answer then: the site's own first, for the model to see what was working."""
    sql = (
        "SELECT q.qname, COUNT(*) AS n, d.owner FROM queries q JOIN devices v ON v.id = q.device_id "
        "LEFT JOIN domains d ON d.domain = q.domain WHERE v.user_id = ? AND q.t >= ? AND q.status != 'blocked'"
    )
    args: list = [user_id, since]
    if device_id:
        sql += " AND q.device_id = ?"
        args.append(device_id)
    rows = [dict(r) | {"qname": r["qname"].strip(".").lower()} for r in conn.execute(sql + " GROUP BY q.qname ORDER BY n DESC LIMIT 400", args)]
    return sorted(rows, key=lambda r: not _named(r, words))[:MAX_ANSWERED]


def _rank(entries: list[dict], words: list[str]) -> list[dict]:
    return sorted(entries, key=lambda e: (not _named(e, words), -e["last"], -e["n"]))


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s}s ago" if s < 90 else f"{s // 60} min ago"


def _text(v, n: int = 240) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) else ""


def target(e: dict, words: list[str]) -> str:
    """What Unblock lets through: the site's own domain whole when one of QuietNet's rules blocks all of it,
    otherwise just the name that was looked up."""
    rd = e["rule"]["domain"] if e["rule"] else None
    if rd and (rd == e["qname"] or any(w in rd for w in words)):
        return rd
    return e["qname"]


def _candidate(e: dict, words: list[str], why: str, conf: str) -> dict:
    cat = e["category"] or "unknown"
    named = _named(e, words)
    return {
        "name": target(e, words),
        "looked_up": [e["qname"]],
        "owner": e["owner"],
        "category": cat,
        "why": why,
        "confidence": conf,
        "blocked_by": e["blocked_by"],
        "devices": e["devices"],
        "times": e["n"],
        "last_seen": to_iso(datetime.fromtimestamp(e["last"], timezone.utc)),
        "warning": WARNING.get(cat, ""),
        # Ticked: the site's own names, and what the AI is sure of, unless it's usually an ad, a tracker or malware
        "selected": cat != "malware" and (named or conf == "high" or (conf == "medium" and cat not in RISKY)),
    }


def _merge(cands: list[dict]) -> list[dict]:
    """Names that unblock the same way (a.irctc.co.in and b.irctc.co.in under one rule on irctc.co.in) become one."""
    rank = {"high": 0, "medium": 1, "low": 2}
    out: dict[str, dict] = {}
    for c in cands:
        have = out.get(c["name"])
        if not have:
            out[c["name"]] = c
            continue
        have["looked_up"] = [*have["looked_up"], *(n for n in c["looked_up"] if n not in have["looked_up"])][:4]
        have["times"] += c["times"]
        have["last_seen"] = max(have["last_seen"], c["last_seen"])
        have["devices"] += [d for d in c["devices"] if d["id"] not in {x["id"] for x in have["devices"]}]
        have["selected"] = have["selected"] or c["selected"]
        if rank[c["confidence"]] < rank[have["confidence"]]:
            have["confidence"] = c["confidence"]
    return list(out.values())[:MAX_CANDIDATES]


def listed(entries: list[dict], words: list[str]) -> list[dict]:
    """Without the AI: everything blocked then, the site's own names first."""
    out = []
    for e in _rank(entries, words)[: MAX_CANDIDATES * 2]:
        if _named(e, words):
            out.append(_candidate(e, words, f"One of this site's own names, blocked {e['n']}× around then.", "medium"))
        else:
            out.append(_candidate(e, words, f"Blocked {e['n']}× around then by {LABEL[e['blocked_by']]}.", "low"))
    return _merge(out)


def clean(answer: dict, entries: dict[str, dict], words: list[str]) -> dict:
    """The model's answer, keeping only names that were really blocked then."""
    kw = [w for w in (answer.get("keywords") if isinstance(answer.get("keywords"), list) else []) if isinstance(w, str)]
    kw = [w for w in (re.sub(r"[^a-z0-9-]", "", w.lower()) for w in kw[:3]) if len(w) >= 3 and w not in STOP]
    words = list(dict.fromkeys([*words, *kw]))
    picks, seen = [], set()
    for c in answer.get("candidates") if isinstance(answer.get("candidates"), list) else []:
        if not isinstance(c, dict):
            continue
        name = _text(c.get("name"), 253).lower().strip(".")
        e = entries.get(name)
        if not e or name in seen:
            continue
        seen.add(name)
        conf = str(c.get("confidence") or "").lower()
        picks.append(_candidate(e, words, _text(c.get("why"), 220), conf if conf in ("high", "medium", "low") else "low"))
    site = _text(answer.get("site"), 60)
    # The site's own names always make the list (the model sometimes skips them), and come first.
    for e in _rank(list(entries.values()), words):
        if e["qname"] not in seen and _named(e, words):
            picks.append(_candidate(e, words, f"One of {site or 'this site'}'s own names, blocked {e['n']}× around then.", "medium"))
    picks.sort(key=lambda c: not any(w in n for n in c["looked_up"] for w in words))
    cands = _merge(picks)
    not_quietnet = answer.get("not_quietnet") is True and not cands
    return {
        "site": site,
        "summary": _text(answer.get("summary"), 300),
        "candidates": cands,
        "not_quietnet": not_quietnet,
        "note": _text(answer.get("note"), 300) if not cands else "",
    }


def _summary_listed(entries: list[dict], cands: list[dict], words: list[str]) -> str:
    k = len(entries)
    own = any(c["selected"] for c in cands) and any(_named(e, words) for e in entries)
    return (
        f"QuietNet blocked {k} name{'s' if k != 1 else ''} around then."
        + (" The site's own names are first and ticked." if own else "")
        + " Tick what the site needs and unblock it; you can undo it."
    )


async def find(
    explainer,
    conn: sqlite3.Connection,
    user_id: str,
    *,
    what: str,
    minutes: int,
    device: dict | None = None,
    image: Image | None = None,
) -> dict:
    """What QuietNet blocked around then that the site or app needs. Nothing is unblocked here.

    `explainer` is None on a server without AI: then the blocked names are listed, the site's own first.
    """
    if explainer is None and not what:
        raise NeedsAI("no AI to read the screenshot")
    now = time.time()
    since = now - minutes * 60 - SLACK_S
    dev_id = device["id"] if device else None
    entries, since_let = blocked_names(conn, user_id, since, dev_id)
    by_parental = parental_names(conn, user_id, since, dev_id)
    words = keywords(what)
    who = device["name"] if device else "your devices"
    base = {
        "what": what,
        "site": "",
        "minutes": minutes,
        "looked_at": len(entries),
        "candidates": [],
        "not_quietnet": False,
        "note": "",
        "already_unblocked": since_let[:10],
        "parental": by_parental[:10],  # blocked by parental controls: changed on the Parental page, not here
        "ai": False,
    }
    if not entries and by_parental:
        return base | {
            "summary": f"Parental controls blocked {len(by_parental)} name{'s' if len(by_parental) != 1 else ''} for {who} "
            "around then. To let them through, change the rules on the Parental page.",
        }
    if not entries:
        working = answered_names(conn, user_id, since, dev_id, words)
        if not working:
            return base | {
                "summary": f"QuietNet saw no lookups from {who} in the last {minutes} minutes, so it can't be what's stopping "
                "it. Is the device on your home WiFi (or set up with QuietNet)? Open the site again and retry.",
            }
        own = [r["qname"] for r in working if _named(r, words)][:3]
        return base | {
            "summary": f"QuietNet didn't block anything for {who} in the last {minutes} minutes, so it isn't what's stopping it."
            + (f" The site's names ({', '.join(own)}) were looked up and answered." if own else ""),
            "not_quietnet": True,
            "note": "The site itself may be down, or the problem is elsewhere: try it on mobile data, or update the app.",
        }

    if explainer is None:
        cands = listed(entries, words)
        note = "AI isn't set up on this server, so the screenshot wasn't read." if image else ""
        return base | {"summary": _summary_listed(entries, cands, words), "candidates": cands, "note": note}

    shown = _rank(entries, words)[:MAX_BLOCKED]
    lines = [
        f"{e['qname']} | {e['owner'] or '-'} | {e['category'] or 'not checked yet'} | {e['n']} | {_ago(now - e['last'])} | {LABEL[e['blocked_by']]}"
        for e in shown
    ]
    worked = [f"{r['qname']} | {r['owner'] or '-'} | {r['n']}" for r in answered_names(conn, user_id, since, dev_id, words)]
    meta = {"minutes": minutes, "blocked": len(shown), "screenshot": image is not None, "device": device["type"] if device else None}
    with explainer.tracing.trace("site-fix", input=meta | {"what": what}, tags=("sites",)) as t:
        try:
            answer = await explainer.ask_json(
                SITE_FIX,
                images=[image] if image else None,
                what=_text(what, 160) or "(they sent only a screenshot)",
                screenshot="\nA screenshot of it is attached." if image else "",
                minutes=minutes,
                device=f", on {_text(device['name'], 60)} ({device['type']})" if device else ", on any device at home",
                blocked="\n".join(lines),
                answered="\n".join(worked) or "(none)",
            )
        except Exception as e:
            log.warning("site fix failed: %s: %s", e.__class__.__name__, str(e)[:300])
            t.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}"[:500])
            cands = listed(entries, words)
            return base | {
                "summary": _summary_listed(entries, cands, words),
                "candidates": cands,
                "note": "The AI couldn't look just now, so this is everything QuietNet blocked then.",
            }
        got = clean(answer, {e["qname"]: e for e in shown}, words)
        if not got["summary"]:
            got["summary"] = _summary_listed(entries, got["candidates"], words)
        t.update(output={"site": got["site"], "candidates": [c["name"] for c in got["candidates"]], "not_quietnet": got["not_quietnet"]})
    return base | got | {"ai": True}


class ParentalBlock(ValueError):
    """A name parental controls block: an allow can't lift it (their rules are `important`)."""


PARENTAL_LOOKBACK_S = 3600


def unblock(conn: sqlite3.Connection, user_id: str, names: list[str], device_id: str | None = None) -> dict:
    """Let `names` through for one device or the whole household. Returns the allow decisions and the rules they
    replaced (undoing the unblock puts those back: POST /decisions/undo-bulk with `restore`).

    For one device, names its parental controls blocked in the last hour are refused (ParentalBlock): a parent changes
    those rules. A household unblock goes ahead: it can't lift a parental block (those rules are `important`), so it
    only lets the name through where nothing parental blocks it."""
    held = set(parental_names(conn, user_id, time.time() - PARENTAL_LOOKBACK_S, device_id)) if device_id else set()
    if hit := [n for n in dict.fromkeys(x.strip(".").lower() for x in names) if n in held]:
        raise ParentalBlock(
            f"Parental controls block {', '.join(hit[:3])}. Change it on the Parental page instead."
        )
    made, replaced = [], []
    for raw in dict.fromkeys(names):
        name = validate_domain(raw)
        before = active_decision(conn, name, device_id, user_id)
        if before and before["action"] == "allow":
            continue
        if before:
            replaced.append(before["id"])  # create_decision below supersedes it
        if device_id is None:
            # A household unblock also lifts the same name's per-device blocks and holds
            for r in conn.execute(
                "SELECT d.id FROM decisions d JOIN devices v ON v.id = d.device_id WHERE v.user_id = ? AND d.domain = ? "
                "AND d.undone_at IS NULL AND d.action IN ('block', 'quarantine')",
                (user_id, name),
            ).fetchall():
                conn.execute("UPDATE decisions SET undone_at = ? WHERE id = ?", (now_iso(), r["id"]))
                replaced.append(r["id"])
        made.append(create_decision(conn, user_id, name, "allow", device_id, "user"))
    return {"decisions": made, "replaced": replaced}
