-- Identidad web y auditoría. No se guardan cookies ni contraseñas recuperables.
CREATE TABLE users (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX sessions_expiry ON sessions(expires_at);
CREATE TABLE login_throttle (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    window_at TEXT NOT NULL,
    failures INTEGER NOT NULL,
    blocked_until TEXT
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT,
    operation_id TEXT,
    result TEXT NOT NULL,
    code TEXT
);
CREATE INDEX audit_recent ON audit_log(occurred_at, id);
CREATE TABLE web_operations (
    operation_id TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL,
    agent_id TEXT REFERENCES agents(agent_id),
    action TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    reference TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX events_filters ON events(agent_id, record_name, type, occurred_at);
CREATE INDEX events_history ON events(received_at, agent_id, event_id);
