# Estado

**Actualizado:** 2026-10-14

## Proyecto

- Agent y Hub mínimos: arrancan, exponen `/healthz`, persisten en su SQLite (migraciones, WAL) y escriben logs JSON sin secretos. No hay lógica DDNS ni de producto todavía.
- Rama base: `main`. Remoto `origin`: git@github.com:oxxido/dynDNSarr.git.
- Slice1 (Fase 0) fusionado en `main` (PR #1). Slice activo: **slice2a** (Fase 1, observación de IP y DNS), planificado en la rama `slice/02a-observacion-ip-dns`. Ver [docs/slices/02a-observacion-ip-dns.md](docs/slices/02a-observacion-ip-dns.md).

## Comandos

Python 3.11+. Guía completa en [docs/operations.md](docs/operations.md).

| Acción | Comando |
|---|---|
| Instalar (venv limpio) | `python3 -m venv .venv && .venv/bin/pip install -e packages/shared -e apps/agent -e apps/hub -e ".[dev]"` |
| Lint / formato | `.venv/bin/ruff check .` · `.venv/bin/ruff format --check .` |
| Tests | `.venv/bin/pytest` (todo) · `.venv/bin/pytest -m "not e2e"` (rápido) |
| Arrancar Agent | `DYNDNSARR_AGENT_DATA_DIR=./data/agent .venv/bin/dyndnsarr-agent` (puerto 8001) |
| Arrancar Hub | `DYNDNSARR_HUB_DATA_DIR=./data/hub .venv/bin/dyndnsarr-hub` (puerto 8000) |
| Verificar wheels | `scripts/verify-wheels.sh` |
| Verificar Hub en Docker | `scripts/verify-compose.sh` (requiere Docker) |
