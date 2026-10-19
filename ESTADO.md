# Estado

**Actualizado:** 2026-10-19

## Proyecto

- Agent y Hub arrancan, exponen `/healthz`, persisten en su SQLite (migraciones, WAL) y escriben logs JSON sin secretos.
- El Agent, con `DYNDNSARR_AGENT_CONFIG_FILE`, detecta la IPv4 pública (≥2 fuentes con consenso), observa DNS en resolvers externos para registros `monitored`, guarda muestras y eventos y se opera por consola. Con registros `managed` y credenciales de DonDominio por entorno, actualiza DNS (una llamada, `pending_verification` → `verified`, backoff persistido, `sync-ddns`); los `monitored` nunca se escriben. Con `[hub]` en el TOML y `DYNDNSARR_AGENT_HUB_TOKEN`, entrega sus eventos al Hub por HTTPS verificado (proxy TLS delante del Hub) con outbox at-least-once; sin Hub el DDNS sigue igual (slice3a).
- Rama base: `main`. Remoto `origin`: git@github.com:oxxido/dynDNSarr.git.
- Slice1 (Fase 0), slice2a y slice2b (Fase 1, motor DDNS) fusionados en `main` (PR #1, #2 y #3). Slice activo: **slice3a** (Fase 2, enlace seguro y entrega de eventos Agent → Hub), implementado y en revisión (PR #4) en la rama `slice/03a-enlace-seguro-eventos`. Ver [docs/slices/03a-enlace-seguro-eventos.md](docs/slices/03a-enlace-seguro-eventos.md). Slice 3b (control Hub → Agent) planificado a continuación, apilado sobre 3a: [docs/slices/03b-control-hub-agent.md](docs/slices/03b-control-hub-agent.md) (rama `slice/03b-control-hub-agent`).
- Pendientes: el criterio 11 de 3a (prueba manual con Caddy y CA local, [operaciones §6.5](docs/operations.md)) y el 11 de 2b (prueba manual contra DonDominio real) siguen a cargo del usuario. La CI `hub-image` se corrigió en 3a (`verify-compose.sh`) y pasa en el PR #4.

## Comandos

Python 3.11+. Guía completa en [docs/operations.md](docs/operations.md).

| Acción | Comando |
|---|---|
| Instalar (venv limpio) | `python3 -m venv .venv && .venv/bin/pip install -e packages/shared -e apps/agent -e apps/hub -e ".[dev]"` |
| Lint / formato | `.venv/bin/ruff check .` · `.venv/bin/ruff format --check .` |
| Tests | `.venv/bin/pytest` (todo) · `.venv/bin/pytest -m "not e2e"` (rápido) |
| Arrancar Agent | `DYNDNSARR_AGENT_DATA_DIR=./data/agent .venv/bin/dyndnsarr-agent` (puerto 8001; motor DDNS con `DYNDNSARR_AGENT_CONFIG_FILE=apps/agent/config/agent.example.toml`, que apunta a servicios reales) |
| Consola del Hub (en el contenedor: `docker compose exec hub …`) | `DYNDNSARR_HUB_DATA_DIR=./data/hub .venv/bin/dyndnsarr-hub agent-add <agent_id>` (también `agent-rotate`, `agent-revoke [--all]`, `agent-list` y `events`) |
| Consola del Agent | `DYNDNSARR_AGENT_DATA_DIR=./data/agent .venv/bin/dyndnsarr-agent status` (también `check-ip`, `check-dns` y `sync-ddns`; estos necesitan `DYNDNSARR_AGENT_CONFIG_FILE`, y `sync-ddns` las credenciales `DYNDNSARR_AGENT_DONDOMINIO_USER`/`_KEY`) |
| Arrancar Hub | `DYNDNSARR_HUB_DATA_DIR=./data/hub .venv/bin/dyndnsarr-hub` (puerto 8000) |
| Verificar wheels | `scripts/verify-wheels.sh` |
| Verificar Hub en Docker | `scripts/verify-compose.sh` (requiere Docker) |
