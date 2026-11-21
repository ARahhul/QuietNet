"""Deterministic confidence (SPEC §4.2): never left to the LLM alone."""

UNCONFIRMED_TEXT = "We couldn't confirm what this does."


def compute_confidence(*, list_hits: int, tracker_owner: bool, threat: bool, pattern: bool, beacon: bool) -> str:
    if list_hits >= 2 or tracker_owner or threat:
        return "high"
    if list_hits == 1 or (pattern and beacon):
        return "medium"
    return "low"


def apply_confidence(verdict: dict, fast, beacon: bool, must_allow: bool) -> dict:
    """Override the explainer's output with hard rules. `fast` is a FastResult."""
    v = dict(verdict)
    sources = list(dict.fromkeys([*fast.list_hits, *fast.threat_hits]))
    owner_confirmed = fast.tracker_owner or fast.entity
    if owner_confirmed:
        sources.append("DuckDuckGo Tracker Radar")

    if must_allow:
        v.update(category="needed", confidence="high", break_risk="high")
        if not v.get("what_it_does") or v["what_it_does"] == UNCONFIRMED_TEXT:
            # "needed" is confirmed by Pahra's own must-allow list, so don't call it unconfirmed
            v["what_it_does"] = "Needed for the device or a core app (updates, time, streaming) to work. Pahra never blocks it automatically."
        v.setdefault("break_note", "Needed for the device or a core app to work.")
        v["sources"] = [*sources, "Pahra must-allow list"]
        return v

    conf = compute_confidence(
        list_hits=len(fast.list_hits),
        tracker_owner=bool(owner_confirmed),
        threat=bool(fast.threat_hits),
        pattern=bool(fast.patterns),
        beacon=beacon,
    )
    v["confidence"] = conf
    v["sources"] = sources

    if fast.threat_hits:
        v["category"] = "malware"
        v["break_risk"] = "low"
    elif fast.tracker_confirmed and fast.tracker_category and v.get("category") in (None, "needed", "unknown"):
        v["category"] = fast.tracker_category  # Tracker Radar says block; don't let the LLM call it "needed"

    # Owner only when confirmed by Tracker Radar, otherwise keep LLM/seed owner only at high confidence
    if owner_confirmed:
        v["owner"] = v.get("owner") or owner_confirmed
    elif conf != "high":
        v["owner"] = None

    if conf == "low":
        v["category"] = "unknown"
        v["what_it_does"] = UNCONFIRMED_TEXT
        v["break_risk"] = v.get("break_risk") or "medium"
        v["break_note"] = "Unknown. Block only if you are sure, and undo if something stops working."
    return v
