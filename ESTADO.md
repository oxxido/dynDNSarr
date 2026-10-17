# Estado

**Actualizado:** 2026-10-14

## Proyecto

- Agent y Hub arrancan, exponen `/healthz`, persisten en su SQLite (migraciones, WAL) y escriben logs JSON sin secretos.
- El Agent, con `DYNDNSARR_AGENT_CONFIG_FILE`, detecta la IPv4 pública (≥2 fuentes con consenso), observa DNS en resolvers externos para registros `monitored`, guarda muestras y eventos y se opera por consola. No escribe DNS (DonDominio llega en 2b) y aún no hay enlace con el Hub.
- Rama base: `main`. Remoto `origin`: git@github.com:oxxido/dynDNSarr.git.
- Slice1 (Fase 0) y slice2a (Fase 1, observación de IP y DNS) fusionados en `main` (PR #1 y #2). Slice activo: **slice2b** (Fase 1, actualización DDNS `managed`), planificado en la rama `slice/02b-actualizacion-ddns-managed`. Ver [docs/slices/02b-actualizacion-ddns-managed.md](docs/slices/02b-actualizacion-ddns-managed.md).

## Comandos

Python 3.11+. Guía completa en [docs/operations.md](docs/operations.md).

| Acción | Comando |
|---|---|
| Instalar (venv limpio) | `python3 -m venv .venv && .venv/bin/pip install -e packages/shared -e apps/agent -e apps/hub -e ".[dev]"` |
| Lint / formato | `.venv/bin/ruff check .` · `.venv/bin/ruff format --check .` |
| Tests | `.venv/bin/pytest` (todo) · `.venv/bin/pytest -m "not e2e"` (rápido) |
| Arrancar Agent | `DYNDNSARR_AGENT_DATA_DIR=./data/agent .venv/bin/dyndnsarr-agent` (puerto 8001; motor DDNS con `DYNDNSARR_AGENT_CONFIG_FILE=apps/agent/config/agent.example.toml`, que apunta a servicios reales) |
| Consola del Agent | `DYNDNSARR_AGENT_DATA_DIR=./data/agent .venv/bin/dyndnsarr-agent status` (también `check-ip`, `check-dns`; estos dos necesitan `DYNDNSARR_AGENT_CONFIG_FILE`) |
| Arrancar Hub | `DYNDNSARR_HUB_DATA_DIR=./data/hub .venv/bin/dyndnsarr-hub` (puerto 8000) |
| Verificar wheels | `scripts/verify-wheels.sh` |
| Verificar Hub en Docker | `scripts/verify-compose.sh` (requiere Docker) |
