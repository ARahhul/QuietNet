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

from ..config import Settings
from ..db import get_meta, now_iso, set_meta
from .client import AdGuardClient, AdGuardError
from .rules import build_adguard_rule, compile_section, is_rescue_catch_all, rescue_rules, splice_section

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

    # ------------------------------------------------------------------ rules
    def household_client_ids(self, user_id: str) -> list[str]:
        rows = self.conn.execute("SELECT client_id FROM devices WHERE user_id = ?", (user_id,)).fetchall()
        return [r["client_id"] for r in rows]

    def refresh_household_rules(self) -> None:
        """Household decisions (device_id NULL) must cover devices added later."""
        rows = self.conn.execute(
            "SELECT id, user_id, domain, action, adguard_rule FROM decisions "
            "WHERE device_id IS NULL AND undone_at IS NULL"
        ).fetchall()
        for r in rows:
            ids = self.household_client_ids(r["user_id"])
            if not ids:
                continue
            rule = build_adguard_rule(r["domain"], ids, r["action"])
            if rule != r["adguard_rule"]:
                self.conn.execute("UPDATE decisions SET adguard_rule = ? WHERE id = ?", (rule, r["id"]))

    def desired_rules(self) -> list[str]:
        self.refresh_household_rules()
        rows = self.conn.execute("SELECT adguard_rule FROM decisions WHERE undone_at IS NULL").fetchall()
        return [r["adguard_rule"] for r in rows]

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
                section = compile_section(self.desired_rules(), await self.rescue_section())
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

    # ---------------------------------------------------------------- clients
    async def sync_clients(self) -> None:
        async with self._lock:
            devices = self.conn.execute("SELECT client_id, linked_ip FROM devices").fetchall()
            try:
                existing = {c["name"]: c for c in await self.ag.clients()}
                for d in devices:
                    ids = [d["client_id"]] + ([d["linked_ip"]] if d["linked_ip"] else [])
                    cur = existing.get(d["client_id"])
                    if cur is None:
                        await self.ag.add_client(d["client_id"], ids)
                    elif sorted(cur.get("ids") or []) != sorted(ids):
                        await self.ag.update_client(d["client_id"], ids)
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
        await self.sync_clients()
        if self.rescue_mode:  # catch-all first, then open access
            await self.sync_rules()
            await self.sync_access()
        else:  # close access first, then drop the catch-all
            await self.sync_access()
            await self.sync_rules()
