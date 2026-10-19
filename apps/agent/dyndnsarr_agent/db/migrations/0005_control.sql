-- Control Hub → Agent (slice 3b). Solo tablas nuevas; instantes UTC ISO 8601 con sufijo Z.

-- Versiones de configuración remota aplicadas. La activa es la de mayor `version`; sin filas,
-- manda el TOML. `content` es el JSON canónico de las secciones administradas (sin secretos).
CREATE TABLE configuration_versions (
    version INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- Comandos del Hub, con su idempotencia. `status`: received, running, succeeded, failed, expired.
CREATE TABLE commands (
    command_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    received_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    finished_at TEXT,
    result TEXT
);
