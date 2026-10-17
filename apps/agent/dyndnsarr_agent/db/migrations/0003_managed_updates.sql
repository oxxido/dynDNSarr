-- Actualización DDNS `managed` (slice 2b). Los instantes son UTC ISO 8601 con sufijo Z.

-- `record_state` se reconstruye: el CHECK de `verification` no se puede ampliar con ALTER.
-- `failures > 0` con `next_attempt_at` nulo es un error definitivo (sin reintento automático).
CREATE TABLE record_state_new (
    record_name TEXT PRIMARY KEY,
    verification TEXT NOT NULL CHECK (
        verification IN ('verified', 'mismatch', 'unknown', 'pending_verification')
    ),
    since TEXT NOT NULL,
    accepted_ip TEXT,
    accepted_at TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    failure_ip TEXT,
    next_attempt_at TEXT,
    last_error_code TEXT
);
INSERT INTO record_state_new (record_name, verification, since)
    SELECT record_name, verification, since FROM record_state;
DROP TABLE record_state;
ALTER TABLE record_state_new RENAME TO record_state;

-- Auditoría: una fila por llamada al proveedor (nunca credenciales ni URL).
CREATE TABLE dns_update_attempts (
    id INTEGER PRIMARY KEY,
    attempted_at TEXT NOT NULL,
    record_name TEXT NOT NULL,
    ip TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    changed INTEGER CHECK (changed IN (0, 1)),
    error_code TEXT,
    retryable INTEGER CHECK (retryable IN (0, 1))
);
CREATE INDEX dns_update_attempts_attempted_at ON dns_update_attempts (attempted_at);
