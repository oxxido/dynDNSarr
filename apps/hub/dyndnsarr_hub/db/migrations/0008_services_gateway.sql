CREATE TABLE service_clients (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    grants TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE service_operations (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES service_clients(id),
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    request TEXT NOT NULL,
    response TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE TABLE publications (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    content TEXT
);
CREATE TABLE publication_revisions (
    publication_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT,
    operation_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (publication_id, revision)
);
CREATE TABLE publication_checks (
    publication_id TEXT PRIMARY KEY REFERENCES publications(id),
    content TEXT NOT NULL
);
