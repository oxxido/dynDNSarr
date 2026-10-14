# Plan — roadmap técnico

Roadmap de alto nivel. **No es un slice listo**: cada slice se detalla en `docs/slices/NN-nombre.md` solo cuando le toca (ver [docs/slices/README.md](docs/slices/README.md)).

Fuente de verdad: [docs/architecture.md](docs/architecture.md) §12 (fases y criterios de aceptación). Ante conflicto con [docs/requirements.md](docs/requirements.md), prevalece la arquitectura ([ADR-0001](docs/decisions/0001-arquitectura-prevalece-sobre-prd.md)).

## Slices

Los slices se numeran `slice1`, `slice2`… y corresponden 1:1 a las fases de la arquitectura (slice N = Fase N-1). Un slice puede subdividirse si resulta demasiado grande; en ese caso se anota aquí.

| Slice | Fase | Objetivo | Criterio de aceptación (resumen) |
|---|---|---|---|
| slice1 | 0 | Monorepo y entorno: Agent y Hub mínimos con `/healthz`, SQLite + migraciones, logging seguro, lint/tests/CI, guía de arranque | Ambos arrancan y rearrancan con datos persistidos; sin secretos en logs |
| slice2 | 1 | Motor DDNS standalone en el Agent (fuentes IPv4, reconciliador, DonDominio, `managed`/`monitored`) | IP simulada cambia → un solo update → DNS verificado; funciona con Hub apagado |
| slice3 | 2 | Enlace seguro Hub–Agent (TLS/tokens, outbox at-least-once, config versionada) | Hub detenido → eventos llegan una sola vez al volver; config inválida no cambia el Agent |
| slice4 | 3 | Landing pública real (`index.html` → Jinja2, `PublicStatusDTO`) | Ausencia demostrable de IP completa y datos privados |
| slice5 | 4 | Hub privado y diagnósticos (login, dashboard, historial, uptime) | Operaciones privadas inaccesibles sin login; `monitored` nunca se escribe |
| slice6 | 5 | Integraciones y release v1.0 (Pi-hole, WoL, alertas, Homarr, retención, docs de instalación) | Integraciones caídas no bloquean DDNS; instalación limpia reproducible |
| slice7 | 6 | v1.1 (MCP, Caddy) — posterior, no comprometido | Ver PRD §19 |

Estado de cada slice: [docs/slices/README.md](docs/slices/README.md).

## Decisiones abiertas (arquitectura §14) por slice

| Decisión | Se resuelve en |
|---|---|
| A3 Scheduler y acceso a SQLite | slice1 (acceso SQLite/migraciones), slice2 (scheduler) |
| A1 API DonDominio | slice2 (antes de codificar el adaptador) |
| A4 Retención, frecuencia, umbrales | slice2 |
| A2 TLS Agent–Hub / rotación de tokens | slice3 |
| A6 Registro del Hub (manual seguro) | slice3 |
| A5 Representación pública de dominios | slice4 |
| A7 Asset facial estático (PNG sin ojos/boca) | slice4, antes del release |

Pendientes que **no** figuran en A1–A7 y afectan al slice1 (resolver al planificarlo):

- Herramienta de migraciones, linter/formatter y proveedor de CI.
