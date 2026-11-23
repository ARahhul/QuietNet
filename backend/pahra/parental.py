"""Parental controls: what each device may reach right now.

Rules come in layers that add up, so a layer can only block more, never loosen another:
  * the household's rules (every device, unless it is set to skip them: a parent's phone),
  * a kid profile's rules (the devices assigned to it),
  * the device's own rules.

Each layer can block adult sites (AdGuard's own filter), force SafeSearch, block apps and games all the time, and at
bedtime block more apps, games, or turn the internet off apart from a few essentials. "Games" are the games in AdGuard's
catalog plus every domain QuietNet found to be a game (pahra.games asks the AI about what these devices look up). Bedtimes are worked out here, in the home's
time zone, and AdGuardSync pushes the result; the engine re-syncs when a bedtime starts or ends (`fingerprint`).
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .adguard.rules import build_parental_rule, internet_off_rules
from .db import new_id, now_iso

DEFAULT_TIME_ZONE = "Asia/Kolkata"
ESSENTIALS_PATH = Path(__file__).parent / "data" / "bedtime_essentials.yaml"
SAFE_SEARCH_ENGINES = ("bing", "duckduckgo", "google", "pixabay", "yandex", "youtube")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# --- validation ----------------------------------------------------------------------------------------------------
def _minutes(hhmm: str) -> int:
    m = _TIME_RE.match(hhmm or "")
    if not m:
        raise ValueError(f"{hhmm!r} isn't a time like 22:00")
    return int(m.group(1)) * 60 + int(m.group(2))


def validate_bedtime(b: dict | None) -> dict | None:
    """{nights: [0=Sun..6=Sat], start: "HH:MM", end: "HH:MM"}. start > end runs overnight (22:00 to 07:00 the next
    morning); start < end is a window within the day (16:00 to 18:00). None = no bedtime."""
    if b is None:
        return None
    nights = sorted({int(n) for n in b.get("nights") or []})
    if any(n < 0 or n > 6 for n in nights):
        raise ValueError("nights are 0 (Sunday) to 6 (Saturday)")
    start, end = str(b.get("start") or ""), str(b.get("end") or "")
    if _minutes(start) == _minutes(end):
        raise ValueError("bedtime has to start and end at different times")
    return {"nights": nights, "start": start, "end": end}


def validate_time_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r}") from None
    return name


# --- the rules at a moment -----------------------------------------------------------------------------------------
def in_bedtime(bedtime: dict | None, local: datetime) -> bool:
    """Is `local` (the home's wall clock) inside this bedtime? Overnight bedtimes belong to the night they start on."""
    if not bedtime or not bedtime.get("nights"):
        return False
    nights = set(bedtime["nights"])
    start, end = _minutes(bedtime["start"]), _minutes(bedtime["end"])
    now = local.hour * 60 + local.minute
    today = (local.weekday() + 1) % 7  # Python: Monday=0; ours: Sunday=0
    if start < end:
        return today in nights and start <= now < end
    return (today in nights and now >= start) or ((today - 1) % 7 in nights and now < end)


@dataclass(frozen=True)
class Effective:
    """A device's parental controls right now (all layers added up)."""

    adult: bool = False
    safe_search: bool = False
    apps: frozenset[str] = frozenset()  # AdGuard blocked-service ids blocked at this moment
    internet_off: bool = False
    bedtime: bool = False  # some layer's bedtime is on and blocks something
    games: bool = False  # games are blocked at this moment

    @property
    def active(self) -> bool:
        return self.adult or self.safe_search or bool(self.apps) or self.internet_off or self.games

    def summary(self) -> dict:
        return {
            "adult": self.adult,
            "safe_search": self.safe_search,
            "apps": sorted(self.apps),
            "internet_off": self.internet_off,
            "bedtime": self.bedtime,
            "games": self.games,
        }


def effective(layers: list[dict], local: datetime) -> Effective:
    adult = safe = off = sleeping = games = False
    apps: set[str] = set()
    for p in layers:
        adult |= bool(p["adult"])
        safe |= bool(p["safe_search"])
        apps |= set(p["apps"])
        games |= bool(p.get("games"))
        if in_bedtime(p["bedtime"], local) and (p["bedtime_apps"] or p["bedtime_internet_off"] or p.get("bedtime_games")):
            sleeping = True
            apps |= set(p["bedtime_apps"])
            off |= bool(p["bedtime_internet_off"])
            games |= bool(p.get("bedtime_games"))
    return Effective(adult, safe, frozenset(apps), off, sleeping, games)


def client_settings(eff: Effective) -> dict | None:
    """The per-client AdGuard settings this needs, or None to keep the client on AdGuard's global settings."""
    if not (eff.adult or eff.safe_search):
        return None
    return {
        "parental_enabled": eff.adult,
        "safe_search": {"enabled": eff.safe_search, **{k: eff.safe_search for k in SAFE_SEARCH_ENGINES}},
    }


@lru_cache(maxsize=1)
def essentials() -> tuple[str, ...]:
    data = yaml.safe_load(ESSENTIALS_PATH.read_text(encoding="utf-8")) or {}
    return tuple(data.get("essentials") or [])


def blocked_domains(eff: Effective, services: dict[str, list[str]], game_domains: list[str]) -> set[str]:
    """The names a device's apps and games block right now (each covers its subdomains)."""
    out = {d for sid in eff.apps for d in services.get(sid, ())}
    if eff.games:
        out.update(game_domains)
    return out


def parental_rules(
    effs: dict[str, Effective], services: dict[str, list[str]], game_domains: list[str] | None = None
) -> list[str]:
    """Client-scoped `important` rules for every device's blocked apps and games, and bedtime internet-off.

    `services` maps an AdGuard blocked-service id to its domains (see adguard.rules.service_domains); `game_domains`
    is what "Block games" blocks (AdGuardSync.game_domains). Devices that block the same domain share one rule."""
    by_domain: dict[str, set[str]] = {}
    off: list[str] = []
    for cid, eff in effs.items():
        if eff.internet_off:
            off.append(cid)
        for d in blocked_domains(eff, services, game_domains or []):
            by_domain.setdefault(d, set()).add(cid)
    rules = [build_parental_rule(d, sorted(cids)) for d, cids in by_domain.items()]
    if off:
        rules += internet_off_rules(off, list(essentials()))
    return rules


ALL = "*"  # bedtime internet-off: every name is held


def held(effs: dict[str, Effective], services: dict[str, list[str]], game_domains: list[str]) -> dict[str, object]:
    """{client_id: ALL | the names its parental controls block now}, for devices that have any. The home's allow
    rules leave these devices out where they would undo the block (AdGuardSync.desired_rules)."""
    out: dict[str, object] = {}
    for cid, eff in effs.items():
        if eff.internet_off:
            out[cid] = ALL
        elif names := blocked_domains(eff, services, game_domains):
            out[cid] = names
    return out


def holds(names: object, domain: str) -> bool:
    """Would an allow on `domain` undo one of these blocks? It does when it covers a blocked name (the same name or a
    parent of it) or the block covers it (a subdomain of a blocked name)."""
    if not names:
        return False
    d = domain.lower().strip(".")
    if names == ALL:
        return d not in essentials()
    return any(d == b or d.endswith("." + b) or b.endswith("." + d) for b in names)  # type: ignore[union-attr]


def game_devices(conn: sqlite3.Connection) -> set[str]:
    """Devices with "Block games" on in any of their layers, now or at bedtime: QuietNet checks what they look up."""
    profiles = _all_profiles(conn)
    rows = conn.execute("SELECT id, user_id, parental_profile_id, skip_household FROM devices")
    return {
        d["id"]
        for d in map(dict, rows)
        if any(p["games"] or p["bedtime_games"] for p in device_layers(d, profiles))
    }


def fingerprint(effs: dict[str, Effective]) -> str:
    return sha256(json.dumps({c: e.summary() for c, e in sorted(effs.items())}, sort_keys=True).encode()).hexdigest()


# --- storage -------------------------------------------------------------------------------------------------------
def _profile(row: sqlite3.Row) -> dict:
    p = dict(row)
    for k in ("adult", "safe_search", "bedtime_internet_off", "games", "bedtime_games"):
        p[k] = bool(p[k])
    for k in ("apps", "bedtime_apps"):
        p[k] = json.loads(p[k] or "[]")
    p["bedtime"] = json.loads(p["bedtime"]) if p["bedtime"] else None
    return p


def household(conn: sqlite3.Connection, user_id: str) -> dict:
    """The home's own rules (made, all off, the first time they're asked for)."""
    row = conn.execute("SELECT * FROM parental_profiles WHERE user_id = ? AND kind = 'household'", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO parental_profiles(id, user_id, kind, name, created_at) VALUES (?, ?, 'household', ?, ?)",
            (new_id(), user_id, "Everyone at home", now_iso()),
        )
        row = conn.execute("SELECT * FROM parental_profiles WHERE user_id = ? AND kind = 'household'", (user_id,)).fetchone()
    return _profile(row)


def get_profile(conn: sqlite3.Connection, user_id: str, profile_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM parental_profiles WHERE id = ? AND user_id = ?", (profile_id, user_id)).fetchone()
    return _profile(row) if row else None


def kid_profiles(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM parental_profiles WHERE user_id = ? AND kind = 'kid' ORDER BY created_at, name", (user_id,)
    )
    return [_profile(r) for r in rows]


def own_profile(conn: sqlite3.Connection, device_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM parental_profiles WHERE device_id = ? AND kind = 'device'", (device_id,)).fetchone()
    return _profile(row) if row else None


def create_profile(conn: sqlite3.Connection, user_id: str, kind: str, name: str, device_id: str | None = None) -> dict:
    pid = new_id()
    conn.execute(
        "INSERT INTO parental_profiles(id, user_id, kind, name, device_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (pid, user_id, kind, name, device_id, now_iso()),
    )
    return get_profile(conn, user_id, pid)  # type: ignore[return-value]


def update_profile(conn: sqlite3.Connection, profile_id: str, changes: dict) -> None:
    cols = {
        "name": lambda v: v,
        "adult": int,
        "safe_search": int,
        "bedtime_internet_off": int,
        "games": int,
        "bedtime_games": int,
        "apps": lambda v: json.dumps(sorted(set(v))),
        "bedtime_apps": lambda v: json.dumps(sorted(set(v))),
        "bedtime": lambda v: json.dumps(v) if v else None,
    }
    sets = {k: cols[k](v) for k, v in changes.items() if k in cols}
    if sets:
        conn.execute(
            f"UPDATE parental_profiles SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
            (*sets.values(), profile_id),
        )


def time_zone(conn: sqlite3.Connection, user_id: str) -> str:
    row = conn.execute("SELECT time_zone FROM users WHERE id = ?", (user_id,)).fetchone()
    return (row["time_zone"] if row else None) or DEFAULT_TIME_ZONE


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIME_ZONE)


def device_layers(device: dict, profiles: dict[str, dict]) -> list[dict]:
    """The profiles that apply to this device, household first. `profiles` holds the household (by "household:"
    + user id), kid profiles (by id) and own rules (by "device:" + device id)."""
    layers = []
    if not device.get("skip_household") and (h := profiles.get("household:" + device["user_id"])):
        layers.append(h)
    if device.get("parental_profile_id") and (k := profiles.get(device["parental_profile_id"])):
        layers.append(k)
    if own := profiles.get("device:" + device["id"]):
        layers.append(own)
    return layers


def _all_profiles(conn: sqlite3.Connection, user_id: str | None = None) -> dict[str, dict]:
    sql, args = "SELECT * FROM parental_profiles", ()
    if user_id:
        sql, args = sql + " WHERE user_id = ?", (user_id,)
    out: dict[str, dict] = {}
    for r in conn.execute(sql, args):
        p = _profile(r)
        key = {"household": "household:" + p["user_id"], "device": "device:" + (p["device_id"] or "")}.get(p["kind"], p["id"])
        out[key] = p
    return out


def all_effective(conn: sqlite3.Connection, now: datetime | None = None, user_id: str | None = None) -> dict[str, dict]:
    """Every device's controls right now: {client_id: {"device": row, "effective": Effective}} (one home with
    `user_id`)."""
    now = now or datetime.now(timezone.utc)
    profiles = _all_profiles(conn, user_id)
    sql = (
        "SELECT d.id, d.user_id, d.client_id, d.name, d.type, d.parental_profile_id, d.skip_household, u.time_zone "
        "FROM devices d JOIN users u ON u.id = d.user_id"
    )
    rows = conn.execute(sql + (" WHERE d.user_id = ?" if user_id else ""), (user_id,) if user_id else ())
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        local = now.astimezone(zone(d["time_zone"]))
        out[d["client_id"]] = {"device": d, "effective": effective(device_layers(d, profiles), local)}
    return out


def effective_by_client(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Effective]:
    return {cid: v["effective"] for cid, v in all_effective(conn, now).items()}
