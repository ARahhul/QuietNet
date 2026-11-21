-- Pahra SQLite schema (SPEC §5 / architecture §6)

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    auto_mode   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id     TEXT NOT NULL UNIQUE,          -- AdGuard persistent client name == ClientID
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,                 -- tv | phone | laptop | router | other
    brand         TEXT,                          -- LG | Samsung | Apple | Android | Amazon | Google ...
    setup_method  TEXT NOT NULL,                 -- dot | doh | ipv6 | ipv4_demo | adb | forwarder
    linked_ip     TEXT,
    lan_ip        TEXT,                          -- forwarder TVs: the TV's LAN IP (the laptop relay maps it to client_id)
    last_seen     TEXT,
    created_at    TEXT NOT NULL
);

-- Global cache of public domain verdicts (shared by all users, never deleted)
CREATE TABLE IF NOT EXISTS domains (
    domain        TEXT PRIMARY KEY,              -- normalised target (Stage 1)
    root_domain   TEXT NOT NULL,
    category      TEXT NOT NULL,                 -- ads | tracking | telemetry | needed | malware | unknown
    owner         TEXT,
    what_it_does  TEXT,
    break_risk    TEXT,                          -- low | medium | high
    break_note    TEXT,
    confidence    TEXT NOT NULL,                 -- high | medium | low
    sources       TEXT NOT NULL DEFAULT '[]',    -- JSON list
    explained_by  TEXT,                          -- gemini | lists | seed | must_allow
    signals       TEXT NOT NULL DEFAULT '{}',    -- JSON: fast-check flags (patterns, dga, threat, beacon)
    first_seen    TEXT NOT NULL,
    explained_at  TEXT
);

-- Ephemeral per-device aggregates (purged after retention window)
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    domain          TEXT NOT NULL,               -- normalised target
    count           INTEGER NOT NULL DEFAULT 0,
    blocked_count   INTEGER NOT NULL DEFAULT 0,
    first_ts        TEXT NOT NULL,
    last_ts         TEXT NOT NULL,
    intervals_n     INTEGER NOT NULL DEFAULT 0,  -- Welford running stats over request gaps
    avg_interval_s  REAL,
    m2_interval     REAL NOT NULL DEFAULT 0,
    UNIQUE (device_id, domain)
);
CREATE INDEX IF NOT EXISTS idx_events_last_ts ON events(last_ts);
CREATE INDEX IF NOT EXISTS idx_events_domain ON events(domain);

CREATE TABLE IF NOT EXISTS decisions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id     TEXT REFERENCES devices(id) ON DELETE CASCADE,   -- NULL = whole household
    domain        TEXT NOT NULL,
    action        TEXT NOT NULL,                 -- block | allow | quarantine
    adguard_rule  TEXT NOT NULL,                 -- exact rule text (for 1-tap undo)
    decided_by    TEXT NOT NULL,                 -- user | auto
    created_at    TEXT NOT NULL,
    undone_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_active ON decisions(undone_at);

CREATE TABLE IF NOT EXISTS alerts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id   TEXT REFERENCES devices(id) ON DELETE CASCADE,
    domain      TEXT,
    type        TEXT NOT NULL,                   -- malware | phishing | odd_behaviour | acr_tracker | new_tracker
    message     TEXT NOT NULL,
    fix_steps   TEXT NOT NULL DEFAULT '[]',      -- JSON list of steps
    fix_source  TEXT,                            -- guide | ai
    read        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Small key/value store (query-log cursor, list refresh times ...)
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
