"""Wires the engine together and owns background tasks."""

import asyncio
import logging
import secrets
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .adguard.client import AdGuardClient, AdGuardError
from .adguard.sync import AdGuardSync
from .auth import FirebaseVerifier
from .blocklist import BlocklistIndex, write_adguard_file
from .config import Settings
from .db import ensure_seed_user, init_sqlite_wal
from . import games, home_address
from .events import purge_old_events
from .pipeline.clues import ClueFetcher
from .pipeline.explainer import Explainer
from .pipeline.lists import ListIndex, load_lists
from .pipeline.run import Pipeline
from .poller import Observed, Poller
from .tracing import Tracing

log = logging.getLogger("pahra.engine")


class Engine:
    def __init__(self, settings: Settings, conn: sqlite3.Connection | None = None, ag: AdGuardClient | None = None):
        self.settings = settings
        self.conn = conn or init_sqlite_wal(settings.db_path)
        # The operator's account (API token). Requests carry their own account: see pahra.api.deps.engine.
        self.user_id = ensure_seed_user(self.conn, settings.seed_user_email)
        self.auth = FirebaseVerifier(settings.firebase_project_id, settings.firebase_auth_emulator_host)
        self.ag = ag or AdGuardClient(settings.adguard_url, settings.adguard_user, settings.adguard_pass)
        self.sync = AdGuardSync(self.conn, self.ag, settings)
        self.lists = self._initial_lists()
        self.clues = ClueFetcher(self.conn, online=not settings.offline_lists)
        self.tracing = Tracing.from_settings(settings)  # Langfuse; a no-op without keys
        if settings.llm_api_key and settings.llm_base_url:
            self.explainer = Explainer(
                settings.llm_api_key,
                settings.llm_model,
                settings.gemini_batch_size,
                base_url=settings.llm_base_url,
                tracing=self.tracing,
            )
        else:
            self.explainer = Explainer(
                settings.gemini_api_key, settings.gemini_model, settings.gemini_batch_size, tracing=self.tracing
            )
        # Built from the lists in start() and after every list refresh (a few seconds for the full lists).
        self.blocklist = BlocklistIndex()
        self._blocklist_fp: tuple | None = None
        self.pipeline = Pipeline(self.conn, self.lists, self.clues, self.explainer, self.sync, self.blocklist)
        self.poller = Poller(
            self.conn, self.ag, settings.poll_interval_s, self._on_observed, self._on_tick, settings.query_log_hours
        )
        self.companion = None  # set by assistant.mcp_client when enabled
        self.forwarder: dict = {"running": False, "devices": 0}  # laptop DNS relay state (pahra.forwarder)
        self.home_follow: dict[str, dict] = {}  # device id -> last Dynamic DNS check (pahra.home_address)
        self._tasks: list[asyncio.Task] = []
        self._pipeline_lock = asyncio.Lock()
        self.ticket_key = secrets.token_bytes(32)  # signs two-minute link tickets (pahra.tickets); memory only

    def _initial_lists(self) -> ListIndex:
        if self.settings.offline_lists:
            return ListIndex.sample()
        idx = ListIndex.from_dir(self.settings.cache_dir, "cache", self.settings.tranco_top_n)
        return idx if idx.lists else ListIndex.sample()

    def set_lists(self, idx: ListIndex) -> None:
        self.lists = idx
        self.pipeline.lists = idx

    def blocklisted(self, domain: str, qnames: Iterable[str] = ()) -> bool:
        """AdGuard blocks this for every home through QuietNet's block list (unless a home let it through): the
        domain is listed, or every name it was looked up as (`qnames`) is."""
        if not self.sync.blocklist_active:
            return False
        e = self.blocklist.match(domain, qnames)
        return e is not None and e.in_adguard

    def _lists_fingerprint(self) -> tuple:
        csv_path = Path(self.settings.blocklist_csv_path) if self.settings.blocklist_csv_path else None
        csv_mtime = csv_path.stat().st_mtime if csv_path and csv_path.exists() else None
        return (
            self.lists.loaded_from,
            tuple(sorted((k, len(v)) for k, v in self.lists.lists.items())),
            len(self.lists.trackers),
            len(self.lists.popular),
            str(csv_path),
            csv_mtime,
        )

    async def rebuild_blocklist(self) -> None:
        """Build the block and clean lists, write AdGuard's copy, and have AdGuard load it (skipped when unchanged)."""
        fp = self._lists_fingerprint()
        if fp == self._blocklist_fp:
            return
        idx = await asyncio.to_thread(BlocklistIndex.build, self.lists, self.settings.blocklist_csv_path or None)
        path, digest = await asyncio.to_thread(write_adguard_file, idx, self.settings.blocklist_dir)
        self.blocklist = idx
        self.pipeline.blocklist = idx
        self.sync.blocklist_file, self.sync.blocklist_digest = path, digest
        self._blocklist_fp = fp
        log.info("block list ready: %s", idx.stats())
        try:
            await self.sync.sync_blocklist()
        except AdGuardError:
            pass  # logged; retried from the poller tick

    async def _on_observed(self, observed: list[Observed]) -> None:
        async with self._pipeline_lock:
            await self.pipeline.process(observed)
            # Parental controls' "Block games": ask about what those devices looked up, block what turns out a game
            try:
                found = await games.check(self.conn, self.explainer, await self.sync.game_catalog(), observed)
            except Exception:
                log.exception("game check failed")
                found = []
        if found:
            games.alert(self.conn, found)
            try:
                await self.sync.sync_rules()
            except AdGuardError:
                pass  # retried from the poller tick

    async def _on_tick(self) -> None:
        # dirty: a push failed earlier. parental_changed: a bedtime started or ended since the last push.
        if self.sync.dirty or self.sync.parental_changed():
            try:
                await self.sync.sync_all()
            except AdGuardError:
                pass
        if self.sync.blocklist_due():
            try:
                await self.sync.sync_blocklist()
            except AdGuardError:
                pass
        # A curated list dropped in or edited is picked up within a poll, not at the next daily list refresh
        if self.settings.blocklist_csv_path and self._blocklist_fp is not None and self._lists_fingerprint() != self._blocklist_fp:
            try:
                async with self._pipeline_lock:
                    await self.rebuild_blocklist()
            except Exception:
                log.exception("block list rebuild failed")
        try:
            async with self._pipeline_lock:
                await self.pipeline.reexplain_with_ai()
        except Exception:
            log.exception("re-explain failed")

    async def _refresh_lists(self) -> None:
        while True:
            try:
                idx = await load_lists(self.settings.cache_dir, self.settings.offline_lists, self.settings.tranco_top_n)
                self.set_lists(idx)
                log.info("lists ready: %s", idx.stats())
                async with self._pipeline_lock:
                    await self.rebuild_blocklist()
            except Exception:
                log.exception("list refresh failed")
            await asyncio.sleep(24 * 3600)

    async def _follow_homes_loop(self) -> None:
        """Keep each home's linked IP on its Dynamic DNS name (a new IP from the provider shouldn't cut it off)."""
        while True:
            try:
                if await home_address.follow_all(self):
                    try:
                        await self.sync.sync_all()
                    except AdGuardError as e:
                        log.warning("AdGuard sync after a home address change failed (retried): %s", e)
                        self.sync.dirty = True
            except Exception:
                log.exception("following home addresses failed")
            await asyncio.sleep(self.settings.ddns_check_s)

    async def _purge_loop(self) -> None:
        while True:
            n = purge_old_events(self.conn, self.settings.event_retention_days)
            if n:
                log.info("purged %d old event rows", n)
            await asyncio.sleep(6 * 3600)

    async def start(self, background: bool = True) -> None:
        try:
            await self.sync.sync_all()
        except AdGuardError as e:
            log.warning("AdGuard not reachable at startup (%s); will retry", e)
        try:  # before the first poll, so known ads never wait for the AI
            await self.rebuild_blocklist()
        except Exception:
            log.exception("building the block list failed")
        if background:
            self._tasks = [
                asyncio.create_task(self.tracing.push_prompts(self.conn), name="langfuse-prompts"),
                asyncio.create_task(self.poller.run_forever(), name="poller"),
                asyncio.create_task(self._refresh_lists(), name="lists"),
                asyncio.create_task(self._purge_loop(), name="purge"),
                asyncio.create_task(self._follow_homes_loop(), name="home-address"),
            ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.clues.aclose()
        await self.ag.aclose()
        await asyncio.to_thread(self.tracing.shutdown)
