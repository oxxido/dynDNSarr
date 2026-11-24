-- Emparejamiento por código y setup web del administrador (slice 8, ADR-0013). Instantes UTC Z.

-- CA que firmó el certificado de la API de ese Agent, fijada al emparejar: solo vale para él.
-- Sin ella se usa la CA global (DYNDNSARR_HUB_AGENT_CA_FILE, camino manual de ADR-0006).
ALTER TABLE agents ADD COLUMN api_ca_pem TEXT;

-- Códigos consumidos (solo su hash). `content_hash` permite repetir el mismo canje.
CREATE TABLE pairing (
    code_hash TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    content_hash TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);

-- Fallos de MAC en el canje: 10 en 15 min bloquean el emparejamiento 15 min.
CREATE TABLE pairing_throttle (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    window_at TEXT NOT NULL,
    failures INTEGER NOT NULL,
    blocked_until TEXT
);

-- Token de un solo uso para crear el primer administrador (solo su hash).
CREATE TABLE admin_setup (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
