-- Proyección pública (slice 4, ADR-0007). Instantes UTC ISO 8601 con sufijo Z.
-- Ninguna columna guarda una IP completa: la página y el JSON públicos solo leen estas tablas,
-- nunca `events` (que sí la tiene).

-- Renovaciones (`dns_update_succeeded`). `ip_suffix` son los dos últimos dígitos de la IPv4;
-- `event_id` mantiene la idempotencia ante reenvíos. `verified_at` lo marca `dns_verified`.
CREATE TABLE public_renewals (
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    event_id TEXT NOT NULL,
    record_name TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ip_suffix TEXT NOT NULL,
    verified_at TEXT,
    PRIMARY KEY (agent_id, event_id)
);
CREATE INDEX public_renewals_recent ON public_renewals (agent_id, occurred_at);

-- Lo último que dijo el Agent en `GET /internal/v1/status`, sin `public_ip`. `records` es JSON de
-- `[{name, verification, since, failures, next_attempt_at, last_error_code}]`.
CREATE TABLE public_agent_status (
    agent_id TEXT PRIMARY KEY REFERENCES agents (agent_id),
    refreshed_at TEXT NOT NULL,
    ip_status TEXT,
    public_ip_confirmed_at TEXT,
    last_check_at TEXT,
    records TEXT NOT NULL
);

-- Una muestra por refresco (también si falló: `unknown`) para el historial de 7 días.
CREATE TABLE public_status_samples (
    agent_id TEXT NOT NULL REFERENCES agents (agent_id),
    sampled_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('healthy', 'degraded', 'critical', 'unknown')),
    PRIMARY KEY (agent_id, sampled_at)
);
CREATE INDEX public_status_samples_at ON public_status_samples (sampled_at);
