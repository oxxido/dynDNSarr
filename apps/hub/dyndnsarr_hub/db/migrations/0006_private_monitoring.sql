-- La política es pública (sin datos sensibles); el detalle queda en tablas privadas.
CREATE TABLE public_monitoring_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    observation_max_seconds INTEGER NOT NULL CHECK (observation_max_seconds > 0),
    critical_mismatch_seconds INTEGER NOT NULL CHECK (critical_mismatch_seconds > 0),
    stale_refresh_factor INTEGER NOT NULL CHECK (stale_refresh_factor >= 1),
    version INTEGER NOT NULL
);
INSERT INTO public_monitoring_policy VALUES (1, 900, 3600, 3, 1);
CREATE TABLE private_agent_snapshot (
    agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
    status TEXT,
    status_at TEXT,
    status_error TEXT,
    config TEXT,
    config_at TEXT,
    config_error TEXT,
    diagnostics TEXT,
    diagnostics_at TEXT,
    diagnostics_error TEXT,
    diagnostics_cursor TEXT
);
CREATE TABLE private_samples (
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    resource TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_at TEXT,
    state TEXT NOT NULL CHECK (state IN ('healthy','degraded','critical','unknown')),
    coverage_seconds INTEGER NOT NULL,
    policy_version INTEGER NOT NULL,
    PRIMARY KEY (agent_id, resource, observed_at),
    UNIQUE (agent_id, resource, evidence_at)
);
CREATE INDEX private_samples_at ON private_samples(observed_at);
CREATE TABLE private_observations (
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    kind TEXT NOT NULL,
    observation_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    record_name TEXT,
    detail TEXT NOT NULL,
    PRIMARY KEY(agent_id, kind, observation_id, observed_at)
);
CREATE INDEX private_observations_at ON private_observations(observed_at);
