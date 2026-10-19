-- Agentes, credenciales y eventos recibidos (slice 3a). Instantes UTC ISO 8601 con sufijo Z.

CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1))
);

-- Solo el hash (sha256 hex) del token; nunca el token en claro.
CREATE TABLE agent_credentials (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX agent_credentials_agent_id ON agent_credentials (agent_id);

-- `sequence` es la del Agent (monotónica por agente). La deduplicación es por (agent_id, event_id).
CREATE TABLE events (
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    event_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    record_name TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY (agent_id, event_id)
);
CREATE INDEX events_agent_sequence ON events (agent_id, sequence);
