"""Finding games for parental controls' "Block games" (pahra.parental).

A device with "Block games" on gets the games in AdGuard's blocked-service catalog blocked, plus every domain QuietNet
has found to be a game. QuietNet finds those from what such devices look up: after each poll, every domain they looked
up that hasn't been checked yet is checked once and the answer kept (table game_checks). So the AI hears about a domain
at most once, and only when a home uses "Block games".

Answered without the AI:
  * a name in AdGuard's gaming catalog (or under one) is a game;
  * must-allow names (device and OS services) and the ads, trackers and malware QuietNet's lists know are not.
Everything else goes to the AI in batches, with what QuietNet already knows about each domain. When the AI can't be
reached, nothing is stored and those domains are tried again after a later poll.

The first lookup of a new game still gets through; the block follows within a poll.
"""

import json
import logging
import sqlite3

from . import parental
from .adguard.rules import validate_domain
from .alerts import create_alert
from .db import now_iso
from .knowledge import is_must_allow_any_device, must_allow_all
from .pipeline.lists import parents
from .prompts import GAME_CHECK

log = logging.getLogger("pahra.games")

BATCH = 25  # domains per AI call
MAX_PER_POLL = 75  # the rest wait for the next poll
NOT_GAMES = {"ads", "tracking", "telemetry", "malware"}  # what the lists say these are decides it


def found(conn: sqlite3.Connection) -> list[str]:
    """Every domain QuietNet found to be a game."""
    return [r["domain"] for r in conn.execute("SELECT domain FROM game_checks WHERE game = 1")]


def blockable(domains) -> list[str]:
    """The names that are safe to block as games: valid, and neither a must-allow name nor a parent of one."""
    must = must_allow_all()
    out: set[str] = set()
    for raw in domains:
        try:
            d = validate_domain(raw)
        except ValueError:
            continue
        if any(p in must for p in parents(d)) or any(m.endswith("." + d) for m in must):
            continue
        out.add(d)
    return sorted(out)


def _catalog_game(catalog: dict[str, str], domain: str) -> str | None:
    """The catalog game this domain belongs to (the name itself or a parent of it is listed)."""
    return next((catalog[p] for p in parents(domain) if p in catalog), None)


def _save(conn: sqlite3.Connection, domain: str, game: bool, name: str | None, source: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO game_checks(domain, game, name, source, checked_at) VALUES (?, ?, ?, ?, ?)",
        (domain, int(game), name, source, now_iso()),
    )


async def _ask(explainer, conn: sqlite3.Connection, batch: list[str]) -> dict[str, dict]:
    rows = {
        r["domain"]: r
        for r in conn.execute(
            f"SELECT domain, category, owner, what_it_does FROM domains WHERE domain IN ({','.join('?' * len(batch))})",
            batch,
        )
    }
    payload = [
        {"domain": d}
        | ({k: rows[d][k] for k in ("category", "owner", "what_it_does") if rows[d][k]} if d in rows else {})
        for d in batch
    ]
    data = await explainer.ask_json(GAME_CHECK, domains=json.dumps(payload, ensure_ascii=False))
    out: dict[str, dict] = {}
    for x in data.get("domains") or []:
        if isinstance(x, dict) and isinstance(x.get("domain"), str) and isinstance(x.get("game"), bool):
            out[x["domain"].lower().strip(".")] = x
    return out


async def check(conn: sqlite3.Connection, explainer, catalog: dict[str, str], observed) -> list[dict]:
    """Check the domains that devices with "Block games" looked up in this poll. Returns the new games:
    [{"domain", "name", "device_ids"}]. `catalog` maps the domains of AdGuard's gaming catalog to the game's name."""
    devices = parental.game_devices(conn)
    seen: dict[str, set[str]] = {}
    for ob in observed:
        if ob.device_id in devices:
            seen.setdefault(ob.domain, set()).add(ob.device_id)
    if not seen:
        return []
    names = sorted(seen)
    done = {
        r["domain"]
        for r in conn.execute(f"SELECT domain FROM game_checks WHERE domain IN ({','.join('?' * len(names))})", names)
    }
    cats = {
        r["domain"]: r["category"]
        for r in conn.execute(f"SELECT domain, category FROM domains WHERE domain IN ({','.join('?' * len(names))})", names)
    }
    games: list[dict] = []
    ask: list[str] = []
    for d in names:
        if d in done:
            continue
        if name := _catalog_game(catalog, d):
            _save(conn, d, True, name, "catalog")
            games.append({"domain": d, "name": name, "source": "catalog"})
        elif is_must_allow_any_device(d) or cats.get(d) in NOT_GAMES:
            _save(conn, d, False, None, "known")
        else:
            ask.append(d)
    if ask and explainer.enabled:
        for i in range(0, min(len(ask), MAX_PER_POLL), BATCH):
            batch = ask[i : i + BATCH]
            try:
                answers = await _ask(explainer, conn, batch)
            except Exception as e:  # quota, network, a reply that isn't JSON: try these again after a later poll
                log.warning("game check failed, trying again later: %s", e)
                break
            for d in batch:
                a = answers.get(d)
                if a is None:
                    continue
                name = (a.get("name") or "").strip()[:80] or None
                is_game = a["game"] and bool(blockable([d]))  # never a must-allow name, whatever the AI says
                _save(conn, d, is_game, name, "ai")
                if is_game:
                    games.append({"domain": d, "name": name, "source": "ai"})
    for g in games:
        g["device_ids"] = sorted(seen[g["domain"]])
    if games:
        log.info("found %d new games: %s", len(games), ", ".join(g["domain"] for g in games))
    return games


def alert(conn: sqlite3.Connection, games: list[dict]) -> None:
    """Tell each home which new game QuietNet blocked on which device (or will block at bedtime). A game from AdGuard's
    catalog was already blocked when the device looked it up; one the AI found got through that first time."""
    effs = {v["device"]["id"]: v for v in parental.all_effective(conn).values()}
    for g in games:
        for did in g["device_ids"]:
            v = effs.get(did)
            if v is None:
                continue
            dev = v["device"]
            what = f"{g['name']} ({g['domain']})" if g["name"] else g["domain"]
            if not v["effective"].games:
                msg = f"{dev['name']} opened a game, {what}. It's blocked at bedtime."
            elif g["source"] == "catalog":
                msg = f"{dev['name']} tried to open a game, {what}. QuietNet blocked it."
            else:
                msg = f"{dev['name']} opened a game QuietNet hadn't seen before, {what}. It's blocked from now on."
            create_alert(conn, user_id=dev["user_id"], device=dev, type_="game", domain=g["domain"], message=msg)
