# Plan — roadmap técnico

Roadmap de alto nivel. **No es un slice listo**: cada slice se detalla en `docs/slices/NN-nombre.md` solo cuando le toca (ver [docs/slices/README.md](docs/slices/README.md)).

Fuente de verdad: [docs/architecture.md](docs/architecture.md) §12 (fases y criterios de aceptación). Ante conflicto con [docs/requirements.md](docs/requirements.md), prevalece la arquitectura ([ADR-0001](docs/decisions/0001-arquitectura-prevalece-sobre-prd.md)).

## Slices

Los slices se numeran `slice1`, `slice2`… y corresponden 1:1 a las fases de la arquitectura (slice N = Fase N-1). Un slice puede subdividirse si resulta demasiado grande; en ese caso se anota aquí (slice2 → 2a y 2b, ambos Fase 1; la numeración de los siguientes no cambia).

| Slice | Fase | Objetivo | Criterio de aceptación (resumen) |
|---|---|---|---|
| slice1 | 0 | Monorepo y entorno: Agent y Hub mínimos con `/healthz`, SQLite + migraciones, logging seguro, lint/tests/CI, guía de arranque | Ambos arrancan y rearrancan con datos persistidos; sin secretos en logs |
| slice2a | 1 | Agent standalone: fuentes IPv4 con consenso, observación DNS en resolvers externos, registros `monitored`, scheduler, persistencia, CLI de lectura | IP simulada cambia → `ip_changed` y registro `mismatch`; fuentes discrepantes → `uncertain`; sin Hub |
| slice2b | 1 | Adaptador DonDominio, registros `managed`, reintentos/backoff, verificación posterior, `sync-ddns` | IP simulada cambia → un solo update → DNS verificado; funciona con Hub apagado |
| slice3 | 2 | Enlace seguro Hub–Agent (TLS/tokens, outbox at-least-once, config versionada) | Hub detenido → eventos llegan una sola vez al volver; config inválida no cambia el Agent |
| slice4 | 3 | Landing pública real (`index.html` → Jinja2, `PublicStatusDTO`) | Ausencia demostrable de IP completa y datos privados |
| slice5 | 4 | Hub privado y diagnósticos (login, dashboard, historial, uptime) | Operaciones privadas inaccesibles sin login; `monitored` nunca se escribe |
| slice6 | 5 | Integraciones y release v1.0 (Pi-hole, WoL, alertas, Homarr, retención, docs de instalación) | Integraciones caídas no bloquean DDNS; instalación limpia reproducible |
| slice7 | 6 | v1.1 (MCP, Caddy) — posterior, no comprometido | Ver PRD §19 |

Estado de cada slice: [docs/slices/README.md](docs/slices/README.md).

## Decisiones abiertas (arquitectura §14) por slice

| Decisión | Se resuelve en |
|---|---|
| A3 Scheduler y acceso a SQLite | slice1 (acceso SQLite/migraciones), slice2a (scheduler) |
| A1 API DonDominio | slice2b (antes de codificar el adaptador) |
| A4 Retención, frecuencia | slice2a |
| A4 Umbrales `healthy/degraded/critical` y uptime | slice5 (donde se consumen) |
| A2 TLS Agent–Hub / rotación de tokens | slice3 |
| A6 Registro del Hub (manual seguro) | slice3 |
| A5 Representación pública de dominios | slice4 |
| A7 Asset facial estático (PNG sin ojos/boca) | slice4, antes del release |

Pendientes que **no** figuran en A1–A7 y afectan al slice1 (resolver al planificarlo):

- Herramienta de migraciones, linter/formatter y proveedor de CI.

## Insumos para slice2b (aún no planificado en detalle)

Decisiones ya tomadas al planificar slice2a; se confirman al detallar 2b.

- **API DonDominio (A1)**, según la [documentación oficial](https://dondominio.dev/en/dondns/docs/api/) y lo confirmado por el usuario: `GET https://dondns.dondominio.com/json/` con `user`, `host`, `ip` y la API key en el parámetro **`password`** (la tabla de la documentación dice `apikey`, su ejemplo usa `password`; se usa `password`). La respuesta es JSON con un indicador de éxito y mensajes de error. El registro A debe existir ya en la zona. Antes de codificar, obtener un ejemplo real de respuesta de error para los fixtures.
- **IP explícita**: se envía siempre `ip=<IP confirmada>`. Sin `ip`, DonDominio usa la IP de quien llama; no se usa porque la decisión de actualizar debe tomarse *antes* de llamar, con la IP ya confirmada por consenso (2a).
- **Reconciliador** (función pura `decide`): resolvers = objetivo → `verified`, sin llamada; resolvers discrepan o no responden → no escribir; DNS ≠ objetivo y ya aceptado con esa IP dentro del plazo → `pending_verification`, sin llamada; en otro caso y sin espera de backoff → una llamada.
- **Verificación**: `verify_retry_seconds` 60, `verify_timeout_seconds` 900; vencido → evento `dns_verification_failed` y nuevo intento permitido.
- **Reintentos**: backoff exponencial 1 min → tope 1 h, persistido (`next_attempt_at`) y que sobrevive a reinicios; errores definitivos (credenciales, registro inexistente) con `retryable=false`. `sync-ddns` manual ignora la espera, no la deduplicación.
- **`monitored`** nunca recibe el adaptador (no puede escribir por construcción; test con proveedor que falla si se invoca).
- **Credenciales** `DYNDNSARR_AGENT_DONDOMINIO_USER` / `_KEY` (`SecretStr`, EnvironmentFile 0600). La clave viaja en la URL: loggers `httpx`/`httpcore` en `WARNING`, errores del adaptador convertidos a códigos seguros sin la URL, y test con valor canario. Sin límites de tasa documentados: backoff y tope de un intento por registro y ciclo.
- **`sync-ddns`** con `flock` sobre `<data_dir>/ddns.lock` para no coincidir con el ciclo del daemon.
- **Criterio manual** del usuario: actualizar un subdominio de prueba real y comprobar convergencia DNS.
