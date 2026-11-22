"""Wires the engine together and owns background tasks."""

import asyncio
import logging
import sqlite3

from .adguard.client import AdGuardClient, AdGuardError
from .adguard.sync import AdGuardSync
from .config import Settings
from .db import ensure_seed_user, init_sqlite_wal
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
        self.user_id = ensure_seed_user(self.conn, settings.seed_user_email)
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
        self.pipeline = Pipeline(self.conn, self.lists, self.clues, self.explainer, self.sync)
        self.poller = Poller(
            self.conn, self.ag, settings.poll_interval_s, self._on_observed, self._on_tick, settings.query_log_hours
        )
        self.companion = None  # set by assistant.mcp_client when enabled
        self.forwarder: dict = {"running": False, "devices": 0}  # laptop DNS relay state (pahra.forwarder)
        self._tasks: list[asyncio.Task] = []
        self._pipeline_lock = asyncio.Lock()

    def _initial_lists(self) -> ListIndex:
        if self.settings.offline_lists:
            return ListIndex.sample()
        idx = ListIndex.from_dir(self.settings.cache_dir, "cache")
        return idx if idx.lists else ListIndex.sample()

    def set_lists(self, idx: ListIndex) -> None:
        self.lists = idx
        self.pipeline.lists = idx

    async def _on_observed(self, observed: list[Observed]) -> None:
        async with self._pipeline_lock:
            await self.pipeline.process(observed)

    async def _on_tick(self) -> None:
        if self.sync.dirty:
            try:
                await self.sync.sync_all()
            except AdGuardError:
                pass
        try:
            async with self._pipeline_lock:
                await self.pipeline.reexplain_with_ai()
        except Exception:
            log.exception("re-explain failed")

    async def _refresh_lists(self) -> None:
        while True:
            try:
                idx = await load_lists(self.settings.cache_dir, self.settings.offline_lists)
                self.set_lists(idx)
                log.info("lists ready: %s", idx.stats())
            except Exception:
                log.exception("list refresh failed")
            await asyncio.sleep(24 * 3600)

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
        if background:
            self._tasks = [
                asyncio.create_task(self.tracing.push_prompts(self.conn), name="langfuse-prompts"),
                asyncio.create_task(self.poller.run_forever(), name="poller"),
                asyncio.create_task(self._refresh_lists(), name="lists"),
                asyncio.create_task(self._purge_loop(), name="purge"),
            ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.clues.aclose()
        await self.ag.aclose()
        await asyncio.to_thread(self.tracing.shutdown)
