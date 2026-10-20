CREATE TABLE integration_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    content TEXT NOT NULL
);
CREATE TABLE wake_attempts (
    operation_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE INDEX wake_recent ON wake_attempts(device_id, requested_at);
ALTER TABLE commands ADD COLUMN resource_id TEXT;
