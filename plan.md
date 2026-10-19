# Plan — roadmap técnico

Roadmap de alto nivel. **No es un slice listo**: cada slice se detalla en `docs/slices/NN-nombre.md` solo cuando le toca (ver [docs/slices/README.md](docs/slices/README.md)).

Fuente de verdad: [docs/architecture.md](docs/architecture.md) §12 (fases y criterios de aceptación). Ante conflicto con [docs/requirements.md](docs/requirements.md), prevalece la arquitectura ([ADR-0001](docs/decisions/0001-arquitectura-prevalece-sobre-prd.md)).

## Slices

Los slices se numeran `slice1`, `slice2`… y corresponden 1:1 a las fases de la arquitectura (slice N = Fase N-1). Un slice puede subdividirse si resulta demasiado grande; en ese caso se anota aquí (slice2 → 2a y 2b, ambos Fase 1; slice3 → 3a y 3b, ambos Fase 2; la numeración de los siguientes no cambia).

| Slice | Fase | Objetivo | Criterio de aceptación (resumen) |
|---|---|---|---|
| slice1 | 0 | Monorepo y entorno: Agent y Hub mínimos con `/healthz`, SQLite + migraciones, logging seguro, lint/tests/CI, guía de arranque | Ambos arrancan y rearrancan con datos persistidos; sin secretos en logs |
| slice2a | 1 | Agent standalone: fuentes IPv4 con consenso, observación DNS en resolvers externos, registros `monitored`, scheduler, persistencia, CLI de lectura | IP simulada cambia → `ip_changed` y registro `mismatch`; fuentes discrepantes → `uncertain`; sin Hub |
| slice2b | 1 | Adaptador DonDominio, registros `managed`, reintentos/backoff, verificación posterior, `sync-ddns` | IP simulada cambia → un solo update → DNS verificado; funciona con Hub apagado |
| slice3a | 2 | Enlace seguro Agent → Hub: TLS verificado (proxy), token por agente, outbox at-least-once, dedup en el Hub, aprovisionamiento por CLI | Hub detenido → eventos llegan una sola vez al volver; token/CA inválidos no pierden eventos |
| slice3b | 2 | Hub → Agent: `health`/`status`, config versionada con ack, comandos idempotentes (`check_dns`, `sync_ddns`) | Config inválida no cambia el Agent; comando duplicado se ejecuta una vez |
| slice4 | 3 | Landing pública real (`index.html` → Jinja2, `PublicStatusDTO`) | Ausencia demostrable de IP completa y datos privados |
| slice5 | 4 | Hub privado y diagnósticos (login, dashboard, historial, uptime) | Operaciones privadas inaccesibles sin login; `monitored` nunca se escribe |
| slice6 | 5 | Integraciones y release v1.0 (Pi-hole, WoL, alertas, Homarr, retención, docs de instalación) | Integraciones caídas no bloquean DDNS; instalación limpia reproducible |
| slice7 | 6 | v1.1 (MCP, Caddy) — posterior, no comprometido | Ver PRD §19 |

Estado de cada slice: [docs/slices/README.md](docs/slices/README.md).

## Decisiones abiertas (arquitectura §14) por slice

| Decisión | Se resuelve en |
|---|---|
| A3 Scheduler y acceso a SQLite | slice1 (acceso SQLite/migraciones), slice2a (scheduler) |
| A1 API DonDominio | slice2b (resuelta: [ADR-0004](docs/decisions/0004-actualizacion-ddns-dondominio.md)) |
| A4 Retención, frecuencia | slice2a |
| A4 Umbrales `healthy/degraded/critical` y uptime | slice5 (donde se consumen) |
| A2 TLS Agent–Hub / rotación de tokens | slice3a (Agent → Hub, proxy TLS); slice3b amplía al sentido Hub → Agent |
| A6 Registro del Hub (manual seguro) | slice3a |
| A5 Representación pública de dominios | slice4 |
| A7 Asset facial estático (PNG sin ojos/boca) | slice4, antes del release |

Pendientes que **no** figuran en A1–A7 y afectan al slice1 (resolver al planificarlo):

- Herramienta de migraciones, linter/formatter y proveedor de CI.
