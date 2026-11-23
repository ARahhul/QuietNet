"""SQLite access in WAL mode (architecture doc §8).

The app keeps one connection that is only used from the asyncio event-loop thread,
so writes are naturally serialised; WAL keeps external readers (e.g. sqlite3 CLI) unblocked.
"""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def init_sqlite_wal(db_path: str = "pahra.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Columns added after the first release (CREATE TABLE IF NOT EXISTS doesn't add them)."""
    users = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "firebase_uid" not in users:
        conn.execute("ALTER TABLE users ADD COLUMN firebase_uid TEXT")
    if "shared_blocklist" not in users:
        conn.execute("ALTER TABLE users ADD COLUMN shared_blocklist INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_firebase ON users(firebase_uid) WHERE firebase_uid IS NOT NULL")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
    if "lan_ip" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN lan_ip TEXT")
    if "ddns_host" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN ddns_host TEXT")
    # Parental controls: the device's kid profile, and "skip the household's rules" (a parent's device)
    if "parental_profile_id" not in cols:
        conn.execute(
            "ALTER TABLE devices ADD COLUMN parental_profile_id TEXT REFERENCES parental_profiles(id) ON DELETE SET NULL"
        )
    if "skip_household" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN skip_household INTEGER NOT NULL DEFAULT 0")
    prof = {r["name"] for r in conn.execute("PRAGMA table_info(parental_profiles)")}
    for col in ("games", "bedtime_games"):  # "Block games" (pahra.games)
        if col not in prof:
            conn.execute(f"ALTER TABLE parental_profiles ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    if "time_zone" not in users:
        conn.execute("ALTER TABLE users ADD COLUMN time_zone TEXT")  # IANA name for bedtimes; NULL = DEFAULT_TIME_ZONE


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """The writes inside happen together or not at all. A savepoint, so it nests and works with autocommit.

    Never `await` inside: every request shares this one connection, so another request's writes would land inside
    this transaction (and be rolled back with it). Write first, then talk to AdGuard.
    """
    name = f"sp_{uuid.uuid4().hex[:12]}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def row_to_dict(row: sqlite3.Row | None, json_fields: Iterable[str] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if f in d and isinstance(d[f], str):
            d[f] = json.loads(d[f])
    return d


def rows_to_dicts(rows: Iterable[sqlite3.Row], json_fields: Iterable[str] = ()) -> list[dict[str, Any]]:
    jf = tuple(json_fields)
    return [row_to_dict(r, jf) for r in rows]  # type: ignore[misc]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def ensure_seed_user(conn: sqlite3.Connection, email: str) -> str:
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return row["id"]
    uid = new_id()
    conn.execute(
        "INSERT INTO users(id, email, auto_mode, created_at) VALUES (?, ?, 0, ?)", (uid, email, now_iso())
    )
    return uid
