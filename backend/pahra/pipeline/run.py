"""Six-stage classification & quarantine pipeline (architecture doc §4)."""

import json
import logging
import sqlite3
from collections import defaultdict

from ..adguard.client import AdGuardError
from ..adguard.sync import AdGuardSync
from ..blocklist import MUST_ALLOW_TEXT, BlocklistIndex, blocked_verdict, clean_verdict, threat_verdict
from ..db import now_iso
from ..decisions import active_decision, create_decision
from ..knowledge import is_acr, is_must_allow, is_must_allow_any_device
from ..poller import Observed
from .clues import ClueFetcher
from .confidence import apply_confidence
from .explainer import AI_LABELS, Explainer
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
        blocklist: BlocklistIndex | None = None,
    ):
        self.conn = conn
        self.lists = lists
        self.clues = clues
        self.explainer = explainer
        self.sync = sync
        # The engine hands over the full index after each build; standalone (tests, CLI) build it from these lists.
        self.blocklist = blocklist if blocklist is not None else BlocklistIndex.build(lists)
        self.processed = 0
        self.known_without_ai = 0  # domains described from the block / clean / threat lists, never sent to AI

    def blocklisted(self, domain: str) -> bool:
        return self.sync.blocklist_active and self.blocklist.covers(domain) is not None

    def known_verdict(self, domain: str, qnames: list[str], fast) -> dict | None:
        """A verdict straight from QuietNet's lists, or None when the domain is unknown and needs the AI.

        Order: must-allow (never blocked) -> block list -> threat intel -> clean list.
        """
        if is_must_allow_any_device(domain):
            c = self.blocklist.clean_match(domain)
            v = clean_verdict(domain, c) if c is not None and c.kind == "must_allow" else None
            return v or {
                "domain": domain, "category": "needed", "owner": None, "what_it_does": MUST_ALLOW_TEXT,
                "break_risk": "high", "break_note": "Needed for the device or a core app to work.", "confidence": "high",
                "sources": ["QuietNet must-allow list"], "explained_by": "must_allow",
            }
        e = self.blocklist.match(domain, qnames)
        if e is not None:
            return blocked_verdict(domain, e)
        if fast.threat_hits:
            return threat_verdict(domain, fast.threat_hits, fast.tracker_owner or fast.entity)
        c = self.blocklist.clean_match(domain)
        if c is not None:
            return clean_verdict(domain, c)
        return None

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

        # Stage 3: fast checks. Anything QuietNet's lists already know (block list, threat intel, clean list) is
        # described from them right here: no clues, no AI. Only unknown domains go on to stages 4-5.
        fasts = {}
        quarantined = False
        unknown: list[str] = []
        for d in uncached:
            fast = fast_check(self.lists, d, sorted(names[d]))
            fasts[d] = fast
            known = self.known_verdict(d, sorted(names[d]), fast)
            if known is not None:
                self.save_domain(d, known, fast.signals() | {
                    "beacon": False,
                    "domain_age_days": None,
                    "acr": is_acr(d) or bool((e := self.blocklist.match(d, names[d])) and e.acr),
                    "suspicious_unknown": False,
                    "blocklist": known["explained_by"] == "blocklist",
                })
                self.known_without_ai += 1
                continue
            unknown.append(d)
            if not fast.suspicious_unknown:
                continue
            for ob in by_domain[d]:
                if is_must_allow(d, self._device_type(ob.device_id), tuple(ob.qnames)):
                    continue
                dev = self.conn.execute("SELECT user_id FROM devices WHERE id = ?", (ob.device_id,)).fetchone()
                # nothing decided yet for this device or its household (a household unblock must hold, too)
                if active_decision(self.conn, d, ob.device_id) is None and active_decision(self.conn, d, None, dev["user_id"]) is None:
                    create_decision(self.conn, dev["user_id"], d, "quarantine", ob.device_id, decided_by="auto")
                    quarantined = True
        if quarantined:
            await self._push()  # block first, explain later

        # Stage 4 + 5: clues in parallel, then batched explanations (unknown domains only)
        if unknown:
            clue_list = await self.clues.gather([(fasts[d], sorted(names[d])) for d in unknown])
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
            changed |= apply_policy(
                self.conn, ob.device_id, ob.domain, tuple(ob.qnames), ob.max_gap_s, blocklisted=self.blocklisted(ob.domain)
            )
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
            fast = fast_check(self.lists, d)
            known = self.known_verdict(d, [d], fast)
            if known is not None:  # the lists know it now: describe it from them instead of asking the AI
                sig = json.loads(self.conn.execute("SELECT signals FROM domains WHERE domain = ?", (d,)).fetchone()["signals"])
                self.save_domain(d, known, sig | {"blocklist": known["explained_by"] == "blocklist"})
                self.known_without_ai += 1
                continue
            items.append((fast, [d]))
        if not items:
            return 0
        clue_list = await self.clues.gather(items)
        verdicts = await self.explainer.explain(clue_list)
        n = 0
        for c in clue_list:
            v = verdicts[c.domain]
            if v.get("explained_by") not in AI_LABELS:
                continue
            v = apply_confidence(v, c.fast, c.beacon, is_must_allow_any_device(c.domain))
            sig = json.loads(
                self.conn.execute("SELECT signals FROM domains WHERE domain = ?", (c.domain,)).fetchone()["signals"]
            )
            self.save_domain(c.domain, v, sig)
            n += 1
        return n
