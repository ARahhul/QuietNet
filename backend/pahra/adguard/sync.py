"""Single writer that makes AdGuard Home match Pahra's database.

* `set_rules` replaces AdGuard's whole user-rule list, so we always rebuild the
  complete Pahra section from active decisions and splice it in under one lock.
* Every device is a persistent AdGuard client whose name == its ClientID.
* With `adguard_lock_access`, only linked ClientIDs / IPs may use the resolver.
"""

import asyncio
import json
import logging
import sqlite3

from ..config import Settings
from ..db import get_meta, now_iso, set_meta
from .client import AdGuardClient, AdGuardError
from .rules import build_adguard_rule, compile_section, splice_section

log = logging.getLogger("pahra.sync")


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

    async def sync_rules(self) -> bool:
        """Push the Pahra section if it differs. Returns True when AdGuard was changed."""
        async with self._lock:
            section = compile_section(self.desired_rules())
            try:
                current = await self.ag.user_rules()
                wanted = splice_section(current, section)
                changed = wanted != current
                if changed:
                    await self.ag.set_rules(wanted)
                    log.info("pushed %d Pahra rules to AdGuard", len(section) - 2)
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

                if self.settings.adguard_lock_access:
                    await self._sync_access(devices)
            except AdGuardError as e:
                self.last_error = str(e)
                self.dirty = True
                raise

    async def _sync_access(self, devices: list[sqlite3.Row]) -> None:
        managed = sorted({d["client_id"] for d in devices} | {d["linked_ip"] for d in devices if d["linked_ip"]})
        prev_managed = set(json.loads(get_meta(self.conn, "access_managed") or "[]"))
        acl = await self.ag.access_list()
        operator_own = [a for a in (acl.get("allowed_clients") or []) if a not in prev_managed]
        allowed = sorted(set(operator_own) | set(managed))
        if not allowed:
            return  # an empty allow-list means "everyone" in AdGuard; never lock out by accident
        if sorted(acl.get("allowed_clients") or []) != allowed:
            await self.ag.set_access(allowed, acl.get("disallowed_clients") or [], acl.get("blocked_hosts") or [])
        set_meta(self.conn, "access_managed", json.dumps(managed))

    async def remove_client(self, client_id: str) -> None:
        async with self._lock:
            try:
                await self.ag.delete_client(client_id)
            except AdGuardError as e:
                log.warning("could not delete AdGuard client %s: %s", client_id, e)

    async def sync_all(self) -> None:
        await self.sync_clients()
        await self.sync_rules()
