-- Control Hub → Agent (slice 3b). Instantes UTC ISO 8601 con sufijo Z.

-- Dirección de la API del Agent y su token Bearer (sentido Hub → Agent). El Hub debe presentar el
-- token, así que va en claro (ADR-0006, D2): `hub.db` es 0600 y no se muestra nunca.
ALTER TABLE agents ADD COLUMN api_url TEXT;
ALTER TABLE agents ADD COLUMN api_token TEXT;

-- Configuraciones que el administrador ha pedido enviar. El contenido es opaco (JSON canónico).
CREATE TABLE desired_config_versions (
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, version)
);

-- Lo último que se sabe del Agent: versión aplicada y último intento (respuesta o evento).
CREATE TABLE observed_agent_state (
    agent_id TEXT PRIMARY KEY REFERENCES agents (agent_id),
    config_version INTEGER NOT NULL DEFAULT 0,
    config_hash TEXT,
    last_result TEXT,
    last_code TEXT,
    last_attempt_version INTEGER,
    last_attempt_at TEXT
);

-- Comandos pedidos. `status`: pending (aún sin aceptar), sent (aceptado), succeeded, failed, expired.
CREATE TABLE commands (
    command_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    type TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT
);
