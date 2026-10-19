# Estado

**Actualizado:** 2026-10-19

## Proyecto

- Agent y Hub arrancan, exponen `/healthz`, persisten en su SQLite (migraciones, WAL) y escriben logs JSON sin secretos.
- El Agent, con `DYNDNSARR_AGENT_CONFIG_FILE`, detecta la IPv4 pública (≥2 fuentes con consenso), observa DNS en resolvers externos para registros `monitored`, guarda muestras y eventos y se opera por consola. Con registros `managed` y credenciales de DonDominio por entorno, actualiza DNS (una llamada, `pending_verification` → `verified`, backoff persistido, `sync-ddns`); los `monitored` nunca se escriben. Con `[hub]` en el TOML y `DYNDNSARR_AGENT_HUB_TOKEN`, entrega sus eventos al Hub por HTTPS verificado (proxy TLS delante del Hub) con outbox at-least-once; sin Hub el DDNS sigue igual (slice3a). Con `DYNDNSARR_AGENT_API_TOKEN` (y TLS propio con una CA local), sirve la API de control `/internal/v1`: el Hub consulta `health`/`status`, le envía una configuración versionada que el Agent valida de forma atómica (si es inválida conserva la anterior; `config-reset` vuelve al TOML) y le pide comandos idempotentes `check_dns`/`sync_ddns`; los resultados vuelven al Hub como eventos por el outbox (slice3b).
- Rama base: `main`. Remoto `origin`: git@github.com:oxxido/dynDNSarr.git.
- Slice1 (Fase 0), slice2a, slice2b (Fase 1, motor DDNS), slice3a y slice3b (Fase 2, enlace y control Hub ↔ Agent) fusionados en `main` (PR #1 a #5). Slice activo: **slice4** (Fase 3, landing pública real), **planificado** en la rama `slice/04-landing-publica`, sin implementar. Ver [docs/slices/04-landing-publica.md](docs/slices/04-landing-publica.md). Bloqueo para el paso 9: faltan los PNG de la mascota (`design_docs/assets/` está vacío).
- Pendientes: el criterio 11 de 3b (CA local y certificado del Agent en la Raspberry, `config-push` y comandos reales, [operaciones §7.5](docs/operations.md)), el 11 de 3a (prueba manual con Caddy y CA local, [operaciones §6.5](docs/operations.md)) y el 11 de 2b (prueba manual contra DonDominio real) siguen a cargo del usuario. `scripts/verify-compose.sh` (ahora comprueba también `agent-set-api` y `agent-api-token` en la imagen) lo ejecuta la CI (`hub-image`): requiere Docker.

## Comandos

Python 3.11+. Guía completa en [docs/operations.md](docs/operations.md).

| Acción | Comando |
|---|---|
| Instalar (venv limpio) | `python3 -m venv .venv && .venv/bin/pip install -e packages/shared -e apps/agent -e apps/hub -e ".[dev]"` |
| Lint / formato | `.venv/bin/ruff check .` · `.venv/bin/ruff format --check .` |
| Tests | `.venv/bin/pytest` (todo) · `.venv/bin/pytest -m "not e2e"` (rápido) |
| Arrancar Agent | `DYNDNSARR_AGENT_DATA_DIR=./data/agent .venv/bin/dyndnsarr-agent` (puerto 8001; motor DDNS con `DYNDNSARR_AGENT_CONFIG_FILE=apps/agent/config/agent.example.toml`, que apunta a servicios reales) |
| Consola del Hub (en el contenedor: `docker compose exec hub …`) | `DYNDNSARR_HUB_DATA_DIR=./data/hub .venv/bin/dyndnsarr-hub agent-add <agent_id>` (también `agent-rotate`, `agent-revoke [--all]`, `agent-list` y `events`; y para controlar al Agent, con `DYNDNSARR_HUB_AGENT_CA_FILE`: `agent-set-api`, `agent-api-token`, `config-push`, `config-resend`, `config-show`, `agent-status`, `command` y `command-show [--refresh]`) |
| Consola del Agent | `DYNDNSARR_AGENT_DATA_DIR=./data/agent .venv/bin/dyndnsarr-agent status` (también `check-ip`, `check-dns`, `sync-ddns` y `config-reset`; los tres primeros necesitan `DYNDNSARR_AGENT_CONFIG_FILE`, y `sync-ddns` las credenciales `DYNDNSARR_AGENT_DONDOMINIO_USER`/`_KEY`; `config-reset` descarta la configuración remota del Hub y exige reiniciar el Agent) |
| Arrancar Hub | `DYNDNSARR_HUB_DATA_DIR=./data/hub .venv/bin/dyndnsarr-hub` (puerto 8000) |
| Verificar wheels | `scripts/verify-wheels.sh` |
| Verificar Hub en Docker | `scripts/verify-compose.sh` (requiere Docker) |
