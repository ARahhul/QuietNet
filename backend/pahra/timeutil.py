import re
from datetime import datetime, timezone

_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def parse_ag_time(s: str) -> datetime:
    """Parse AdGuard RFC3339 timestamps (nanosecond precision, 'Z' or offset)."""
    s = s.strip().replace("Z", "+00:00")
    s = _FRAC_RE.sub(r"\1", s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(s: str) -> datetime:
    return parse_ag_time(s)
