ALTER TABLE commands ADD COLUMN resource_id TEXT;
CREATE TABLE integration_cache (
    agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
    content TEXT,
    refreshed_at TEXT,
    error_code TEXT
);
CREATE TABLE alert_signals (
    rule_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    phase TEXT NOT NULL,
    since_at TEXT NOT NULL,
    last_notified_at TEXT,
    last_sequence INTEGER NOT NULL,
    PRIMARY KEY(rule_id, subject)
);
CREATE TABLE alert_deliveries (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    channel TEXT NOT NULL,
    phase TEXT NOT NULL,
    severity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error_code TEXT
);
CREATE INDEX alerts_due ON alert_deliveries(status, next_attempt_at);
CREATE INDEX alerts_subject ON alert_deliveries(rule_id,subject,phase,status);
CREATE TABLE event_receipts (
    agent_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(agent_id,event_id)
);
INSERT INTO event_receipts SELECT agent_id,event_id,received_at FROM events;
CREATE TABLE public_ip_changes (
    agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
    occurred_at TEXT NOT NULL
);
