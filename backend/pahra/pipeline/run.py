"""Six-stage classification & quarantine pipeline (architecture doc §4)."""

import json
import logging
import sqlite3
from collections import defaultdict

from ..adguard.client import AdGuardError
from ..adguard.sync import AdGuardSync
from ..db import now_iso
from ..decisions import active_decision, create_decision
from ..knowledge import is_acr, is_must_allow, is_must_allow_any_device
from ..poller import Observed
from .clues import ClueFetcher
from .confidence import apply_confidence
from .explainer import Explainer
from .fastchecks import fast_check
from .lists import ListIndex
from .normalize import root_domain
from .policy import apply_policy, check_odd_behaviour

log = logging.getLogger("pahra.pipeline")


class Pipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        lists: ListIndex,
        clues: ClueFetcher,
        explainer: Explainer,
        sync: AdGuardSync,
    ):
        self.conn = conn
        self.lists = lists
        self.clues = clues
        self.explainer = explainer
        self.sync = sync
        self.processed = 0

    def _cached(self, domain: str) -> bool:
        return self.conn.execute("SELECT 1 FROM domains WHERE domain = ?", (domain,)).fetchone() is not None

    def _device_type(self, device_id: str) -> str | None:
        r = self.conn.execute("SELECT type FROM devices WHERE id = ?", (device_id,)).fetchone()
        return r["type"] if r else None

    async def _push(self) -> None:
        try:
            await self.sync.sync_rules()
        except AdGuardError:
            pass  # sync.dirty stays True; the poller tick retries

    async def process(self, observed: list[Observed]) -> None:
        by_domain: dict[str, list[Observed]] = defaultdict(list)
        names: dict[str, set[str]] = defaultdict(set)
        for ob in observed:
            by_domain[ob.domain].append(ob)
            names[ob.domain] |= ob.qnames

        # Stage 2: global cache
        uncached = [d for d in by_domain if not self._cached(d)]

        # Stage 3: fast checks + immediate quarantine for brand-new suspicious domains
        fasts = {}
        quarantined = False
        for d in uncached:
            fast = fast_check(self.lists, d, sorted(names[d]))
            fasts[d] = fast
            if not fast.suspicious_unknown:
                continue
            for ob in by_domain[d]:
                if is_must_allow(d, self._device_type(ob.device_id), tuple(ob.qnames)):
                    continue
                if active_decision(self.conn, d, ob.device_id) is None:
                    dev = self.conn.execute("SELECT user_id FROM devices WHERE id = ?", (ob.device_id,)).fetchone()
                    create_decision(self.conn, dev["user_id"], d, "quarantine", ob.device_id, decided_by="auto")
                    quarantined = True
        if quarantined:
            await self._push()  # block first, explain later

        # Stage 4 + 5: clues in parallel, then batched explanations
        if uncached:
            clue_list = await self.clues.gather([(fasts[d], sorted(names[d])) for d in uncached])
            verdicts = await self.explainer.explain(clue_list)
            for c in clue_list:
                v = apply_confidence(verdicts[c.domain], c.fast, c.beacon, is_must_allow_any_device(c.domain))
                signals = c.fast.signals() | {
                    "beacon": c.beacon,
                    "domain_age_days": c.domain_age_days,
                    "acr": is_acr(c.domain),
                    "suspicious_unknown": c.fast.suspicious_unknown,
                }
                self.save_domain(c.domain, v, signals)
            self.processed += len(clue_list)

        # Stage 6: policy for every observed (device, domain)
        changed = False
        for ob in observed:
            changed |= apply_policy(self.conn, ob.device_id, ob.domain, tuple(ob.qnames), ob.max_gap_s)
        for dev in {ob.device_id for ob in observed}:
            check_odd_behaviour(self.conn, dev)
        if changed:
            await self._push()

    def save_domain(self, domain: str, v: dict, signals: dict) -> None:
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO domains(domain, root_domain, category, owner, what_it_does, break_risk, break_note, confidence, "
            "sources, explained_by, signals, first_seen, explained_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(domain) DO UPDATE SET category=excluded.category, owner=excluded.owner, "
            "what_it_does=excluded.what_it_does, break_risk=excluded.break_risk, break_note=excluded.break_note, "
            "confidence=excluded.confidence, sources=excluded.sources, explained_by=excluded.explained_by, "
            "signals=excluded.signals, explained_at=excluded.explained_at",
            (
                domain, root_domain(domain), v.get("category") or "unknown", v.get("owner"), v.get("what_it_does"),
                v.get("break_risk"), v.get("break_note"), v.get("confidence") or "low",
                json.dumps(v.get("sources") or []), v.get("explained_by"), json.dumps(signals), ts, ts,
            ),
        )

    async def reexplain_with_ai(self, limit: int = 20) -> int:
        """Upgrade list-based explanations to Gemini once a key/quota is available (one batch per tick)."""
        if not self.explainer.enabled:
            return 0
        rows = self.conn.execute(
            "SELECT domain FROM domains WHERE explained_by = 'lists' AND confidence != 'low' LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            return 0
        items = []
        for r in rows:
            d = r["domain"]
            items.append((fast_check(self.lists, d), [d]))
        clue_list = await self.clues.gather(items)
        verdicts = await self.explainer.explain(clue_list)
        n = 0
        for c in clue_list:
            v = verdicts[c.domain]
            if v.get("explained_by") != "gemini":
                continue
            v = apply_confidence(v, c.fast, c.beacon, is_must_allow_any_device(c.domain))
            sig = json.loads(
                self.conn.execute("SELECT signals FROM domains WHERE domain = ?", (c.domain,)).fetchone()["signals"]
            )
            self.save_domain(c.domain, v, sig)
            n += 1
        return n
