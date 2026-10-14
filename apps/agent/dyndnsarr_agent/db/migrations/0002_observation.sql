-- Observaciones del motor DDNS (slice 2a). Los instantes son UTC ISO 8601 con sufijo Z.

CREATE TABLE public_ip_observations (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    ip TEXT,
    error_code TEXT
);
CREATE INDEX public_ip_observations_observed_at ON public_ip_observations (observed_at);

CREATE TABLE dns_checks (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    record_name TEXT NOT NULL,
    resolver TEXT NOT NULL,
    ip TEXT,
    error_code TEXT
);
CREATE INDEX dns_checks_observed_at ON dns_checks (observed_at);

CREATE TABLE record_state (
    record_name TEXT PRIMARY KEY,
    verification TEXT NOT NULL CHECK (verification IN ('verified', 'mismatch', 'unknown')),
    since TEXT NOT NULL
);

-- `sequence` es monotónica y no se reutiliza ni tras purgar (AUTOINCREMENT).
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    record_name TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX events_occurred_at ON events (occurred_at);
