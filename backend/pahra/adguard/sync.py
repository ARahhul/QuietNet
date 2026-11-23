"""Single writer that makes AdGuard Home match QuietNet's database.

* `set_rules` replaces AdGuard's whole user-rule list, so we always rebuild the
  complete QuietNet section from active decisions and splice it in under one lock.
* Every device is a persistent AdGuard client whose name == its ClientID.
* With `adguard_lock_access`, only linked ClientIDs / IPs get real answers. Anyone else is either dropped
  (`adguard_unknown_clients=drop`: AdGuard's allow-list) or, by default ("rescue"), let in but answered only for
  RESCUE_NAMES, everything else blocked by a catch-all rule. Rescue matters when a home's public IP changes: its
  router can still reach its Dynamic DNS service to report the new address (pahra.home_address follows it), and
  lookups fail at once instead of hanging. The catch-all is pushed before access opens, and access only opens
  while AdGuard holds it, so QuietNet never becomes an open resolver.
"""

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import games, parental
from ..blocklist import FILTER_NAME
from ..config import Settings
from ..db import get_meta, now_iso, set_meta
from .client import AdGuardClient, AdGuardError
from .rules import (
    build_adguard_rule,
    compile_section,
    is_rescue_catch_all,
    optout_rule,
    rescue_rules,
    rule_clients,
    service_domains,
    splice_section,
)

SERVICES_MAX_AGE = timedelta(days=1)  # AdGuard's blocked-service catalog only changes with AdGuard updates

log = logging.getLogger("pahra.sync")

# What an address QuietNet doesn't know may still look up (with "rescue"): enough for a router to report a new
# public IP to its Dynamic DNS service and keep its clock right, and to open QuietNet's own site to re-link.
RESCUE_NAMES = [
    # Dynamic DNS services routers have built in (their update servers)
    "tplinkdns.com", "tplinkcloud.com", "tp-link.com",  # TP-Link DDNS (TP-Link ID)
    "no-ip.com", "noip.com",
    "dyndns.org", "dyndns.com", "dyn.com",
    "dynu.com", "duckdns.org", "afraid.org", "asuscomm.com",
    # time (TLS to the DDNS service fails with a wrong clock)
    "ntp.org", "nist.gov", "time.windows.com", "time.google.com",
]


def rescue_names(settings: Settings) -> list[str]:
    """RESCUE_NAMES plus QuietNet's own domain (dns.quietnet.in -> quietnet.in: the dashboard and its API)."""
    labels = settings.dns_hostname.strip(".").lower().split(".")
    own = ".".join(labels[1:]) if len(labels) > 2 else ".".join(labels)
    return [*RESCUE_NAMES, own]


class AdGuardSync:
    def __init__(self, conn: sqlite3.Connection, ag: AdGuardClient, settings: Settings):
        self.conn = conn
        self.ag = ag
        self.settings = settings
        self._lock = asyncio.Lock()
        self.dirty = True
        self.last_ok: str | None = None
        self.last_error: str | None = None
        # QuietNet's block list as an AdGuard filter (pahra.blocklist). The engine sets the file after each build.
        self.blocklist_file: Path | None = None
        self.blocklist_digest: str | None = None
        self.blocklist_active = bool(get_meta(conn, "blocklist_filter_id"))  # AdGuard keeps filters across restarts
        self.blocklist_error: str | None = None
        self._blocklist_retry_at = 0.0
        self.parental_fp: str | None = None  # parental state last pushed (a bedtime starting changes it)

    # ---------------------------------------------------------------- parental
    async def service_catalog(self) -> list[dict]:
        """AdGuard's blocked-service catalog (the apps parental controls can block), cached for a day. If AdGuard
        can't be asked, the last copy (or nothing)."""
        cached = get_meta(self.conn, "ag_services")
        at = get_meta(self.conn, "ag_services_at")
        fresh = at and datetime.now(timezone.utc) - datetime.fromisoformat(at) < SERVICES_MAX_AGE
        if cached and fresh:
            return json.loads(cached)
        try:
            catalog = [
                {
                    "id": s["id"],
                    "name": s.get("name") or s["id"],
                    "group": s.get("group_id") or "",
                    "icon_svg": s.get("icon_svg") or "",
                    "domains": service_domains(s.get("rules") or []),
                }
                for s in await self.ag.blocked_services_all()
                if s.get("id")
            ]
        except AdGuardError as e:
            log.warning("could not read AdGuard's blocked services: %s", e)
            return json.loads(cached) if cached else []
        if catalog:
            set_meta(self.conn, "ag_services", json.dumps(catalog))
            set_meta(self.conn, "ag_services_at", now_iso())
        return catalog

    async def _service_domains(self) -> dict[str, list[str]]:
        return {s["id"]: s["domains"] for s in await self.service_catalog()}

    async def game_catalog(self) -> dict[str, str]:
        """The domains of the games in AdGuard's catalog -> the game's name."""
        return {d: s["name"] for s in await self.service_catalog() if s.get("group") == "gaming" for d in s["domains"]}

    async def game_domains(self) -> list[str]:
        """What "Block games" blocks: the games in AdGuard's catalog plus the domains QuietNet found to be games."""
        return games.blockable([*await self.game_catalog(), *games.found(self.conn)])

    def parental_changed(self, now: datetime | None = None) -> bool:
        """Has a bedtime started or ended (or anything else changed) since the last push?"""
        return parental.fingerprint(parental.effective_by_client(self.conn, now)) != self.parental_fp

    # ------------------------------------------------------------------ rules
    def household_client_ids(self, user_id: str) -> list[str]:
        rows = self.conn.execute("SELECT client_id FROM devices WHERE user_id = ?", (user_id,)).fetchall()
        return [r["client_id"] for r in rows]

    def refresh_rules(self) -> None:
        """Rebuild every active decision's rule from what is true now: a household decision covers devices added
        later, and a device's own decision follows a new address (ClientID). An undone decision is rebuilt when it
        is put back, since that makes it active again before the next push."""
        rows = self.conn.execute(
            "SELECT d.id, d.user_id, d.device_id, d.domain, d.action, d.adguard_rule, v.client_id "
            "FROM decisions d LEFT JOIN devices v ON v.id = d.device_id WHERE d.undone_at IS NULL"
        ).fetchall()
        homes: dict[str, list[str]] = {}
        for r in rows:
            if r["device_id"] is None:
                if r["user_id"] not in homes:
                    homes[r["user_id"]] = self.household_client_ids(r["user_id"])
                ids = homes[r["user_id"]]
            else:
                ids = [r["client_id"]] if r["client_id"] else []
            if not ids:
                continue
            rule = build_adguard_rule(r["domain"], ids, r["action"])
            if rule != r["adguard_rule"]:
                self.conn.execute("UPDATE decisions SET adguard_rule = ? WHERE id = ?", (rule, r["id"]))

    def desired_rules(
        self, services: dict[str, list[str]] | None = None, effs: dict | None = None, game_domains: list[str] | None = None
    ) -> list[str]:
        """Every home's decisions, homes that switched the shared list off, and parental controls' rules for this
        moment.

        A home's allow rules are `@@...$important`, the strongest kind, so a parent's block can't rely on AdGuard's
        order: a device is left out of any allow that would undo what its parental controls block (bedtime internet
        off, a blocked app or game). The same goes for "Site not working?" unblocks made for the whole home."""
        self.refresh_rules()
        services = services or {}
        effs = parental.effective_by_client(self.conn) if effs is None else effs
        held = parental.held(effs, services, game_domains or [])
        rules: list[str] = []
        for r in self.conn.execute("SELECT domain, action, adguard_rule FROM decisions WHERE undone_at IS NULL"):
            rule = r["adguard_rule"]
            if r["action"] == "allow" and held:
                ids = rule_clients(rule)
                keep = [c for c in ids if not parental.holds(held.get(c), r["domain"])]
                if not keep:
                    continue
                if len(keep) != len(ids):
                    rule = build_adguard_rule(r["domain"], keep, "allow")
            rules.append(rule)
        # Homes that switched QuietNet's shared block list off (Settings). Not for devices with the adult filter or
        # SafeSearch on: AdGuard skips both for anything an allow rule lets through.
        for u in self.conn.execute("SELECT id FROM users WHERE shared_blocklist = 0").fetchall():
            ids = [c for c in self.household_client_ids(u["id"]) if not ((e := effs.get(c)) and (e.adult or e.safe_search))]
            if ids:
                rules.append(optout_rule(ids))
        return rules + parental.parental_rules(effs, services, game_domains or [])

    @property
    def rescue_mode(self) -> bool:
        return self.settings.adguard_lock_access and self.settings.adguard_unknown_clients == "rescue"

    async def _operator_own(self, acl: dict) -> list[str]:
        """Entries the operator put in AdGuard's allow-list by hand (kept across rescue, where the list is empty)."""
        prev_managed = set(json.loads(get_meta(self.conn, "access_managed") or "[]"))
        saved = set(json.loads(get_meta(self.conn, "access_operator_own") or "[]"))
        own = sorted(saved | {a for a in (acl.get("allowed_clients") or []) if a not in prev_managed})
        set_meta(self.conn, "access_operator_own", json.dumps(own))
        return own

    async def rescue_section(self) -> list[str]:
        if not self.rescue_mode:
            return []
        known = [r["client_id"] for r in self.conn.execute("SELECT client_id FROM devices")]
        for ref in await self._operator_own(await self.ag.access_list()):
            try:
                rescue_rules([ref], ["example.com"])  # validates the reference
                known.append(ref)
            except ValueError:
                log.warning("allow-list entry %r can't be kept out of the rescue rules; it will only get rescue answers", ref)
        return rescue_rules(known, rescue_names(self.settings)) if known else []

    async def sync_rules(self) -> bool:
        """Push the QuietNet section if it differs. Returns True when AdGuard was changed."""
        async with self._lock:
            try:
                services = await self._service_domains()
                section = compile_section(
                    self.desired_rules(services, game_domains=await self.game_domains()), await self.rescue_section()
                )
            except AdGuardError as e:
                self.last_error, self.dirty = str(e), True
                raise
            try:
                current = await self.ag.user_rules()
                wanted = splice_section(current, section)
                changed = wanted != current
                if changed:
                    await self.ag.set_rules(wanted)
                    log.info("pushed %d QuietNet rules to AdGuard", len(section) - 2)
            except AdGuardError as e:
                self.last_error = str(e)
                self.dirty = True
                log.warning("rule sync failed: %s", e)
                raise
            self.dirty = False
            self.last_ok = now_iso()
            self.last_error = None
            return changed

    # ------------------------------------------------------------- block list
    BLOCKLIST_RETRY_S = 600

    @property
    def blocklist_url(self) -> str | None:
        """The list file as AdGuard opens it: a path on AdGuard's own disk."""
        if self.blocklist_file is None:
            return None
        return self.settings.adguard_blocklist_path or str(self.blocklist_file.resolve())

    def blocklist_due(self) -> bool:
        """A failed registration is retried every few minutes (not on every poll: AdGuard may refuse the path)."""
        return self.blocklist_file is not None and self.blocklist_error is not None and time.monotonic() >= self._blocklist_retry_at

    async def sync_blocklist(self) -> bool:
        """Make AdGuard load QuietNet's block list, and reload it when its contents changed.

        This is the one global layer QuietNet adds on purpose: the file only ever blocks (`||domain^` lines, built
        without must-allow names), and every home's own `@@...$client` allow rules beat it, so it can't open anything
        up for anyone, and each home can still let a site through for itself.
        """
        url = self.blocklist_url
        if url is None:
            return False
        async with self._lock:
            try:
                filters = await self.ag.filters()
                mine = next((f for f in filters if f.get("url") == url), None)
                for f in filters:  # a copy left behind under an older path
                    if f.get("name") == FILTER_NAME and f.get("url") != url:
                        await self.ag.remove_filter(f["url"])
                if mine is None:
                    await self.ag.add_filter(FILTER_NAME, url)  # AdGuard reads the file now
                    mine = next((f for f in await self.ag.filters() if f.get("url") == url), None)
                    if mine is None:
                        raise AdGuardError(f"AdGuard did not keep the block list filter {url}")
                    set_meta(self.conn, "blocklist_digest", self.blocklist_digest or "")
                elif not mine.get("enabled"):
                    await self.ag.set_filter(url, FILTER_NAME, enabled=True)
                if get_meta(self.conn, "blocklist_digest") != (self.blocklist_digest or ""):
                    await self.ag.refresh_filters()
                    set_meta(self.conn, "blocklist_digest", self.blocklist_digest or "")
                    # the status read above is from before the reload: its rule count is the old file's
                    mine = next((f for f in await self.ag.filters() if f.get("url") == url), mine)
            except AdGuardError as e:
                self.blocklist_error = str(e)
                self._blocklist_retry_at = time.monotonic() + self.BLOCKLIST_RETRY_S
                log.warning("could not load the block list into AdGuard (%s); retrying in %d s", e, self.BLOCKLIST_RETRY_S)
                raise
            set_meta(self.conn, "blocklist_filter_id", str(mine["id"]))
            self.blocklist_active = True
            self.blocklist_error = None
            log.info("AdGuard has the block list (%s rules)", mine.get("rules_count", "?"))
            return True

    def blocklist_status(self) -> dict:
        return {
            "active": self.blocklist_active,
            "path": self.blocklist_url,
            "error": self.blocklist_error,
            "filter_id": get_meta(self.conn, "blocklist_filter_id"),
        }

    # ---------------------------------------------------------------- clients
    async def sync_clients(self) -> None:
        async with self._lock:
            devices = self.conn.execute("SELECT client_id, linked_ip FROM devices").fetchall()
            effs = parental.effective_by_client(self.conn)
            try:
                existing = {c["name"]: c for c in await self.ag.clients()}
                for d in devices:
                    ids = [d["client_id"]] + ([d["linked_ip"]] if d["linked_ip"] else [])
                    eff = effs.get(d["client_id"])
                    want = parental.client_settings(eff) if eff else None
                    cur = existing.get(d["client_id"])
                    if cur is None:
                        await self.ag.add_client(d["client_id"], ids, want)
                    elif not self.ag.client_matches(cur, ids, want):
                        await self.ag.update_client(d["client_id"], ids, want)
            except AdGuardError as e:
                self.last_error = str(e)
                self.dirty = True
                raise

    async def sync_access(self) -> None:
        """Who may use the resolver: the allow-list ("drop"), or everyone while the rescue catch-all is in place."""
        if not self.settings.adguard_lock_access:
            return
        async with self._lock:
            try:
                devices = self.conn.execute("SELECT client_id, linked_ip FROM devices").fetchall()
                managed = sorted({d["client_id"] for d in devices} | {d["linked_ip"] for d in devices if d["linked_ip"]})
                acl = await self.ag.access_list()
                own = await self._operator_own(acl)
                allowed = sorted(set(own) | set(managed))
                if self.rescue_mode and any(is_rescue_catch_all(r) for r in await self.ag.user_rules()):
                    allowed = []  # everyone gets in; the catch-all blocks all but RESCUE_NAMES for unknown clients
                elif not allowed:
                    return  # an empty allow-list means "everyone" in AdGuard; never open up by accident
                if sorted(acl.get("allowed_clients") or []) != allowed:
                    await self.ag.set_access(allowed, acl.get("disallowed_clients") or [], acl.get("blocked_hosts") or [])
                set_meta(self.conn, "access_managed", json.dumps(managed))
            except AdGuardError as e:
                self.last_error = str(e)
                self.dirty = True
                raise

    async def remove_client(self, client_id: str) -> None:
        async with self._lock:
            try:
                await self.ag.delete_client(client_id)
            except AdGuardError as e:
                log.warning("could not delete AdGuard client %s: %s", client_id, e)

    async def sync_all(self) -> None:
        fp = parental.fingerprint(parental.effective_by_client(self.conn))
        await self.sync_clients()
        if self.rescue_mode:  # catch-all first, then open access
            await self.sync_rules()
            await self.sync_access()
        else:  # close access first, then drop the catch-all
            await self.sync_access()
            await self.sync_rules()
        self.parental_fp = fp
