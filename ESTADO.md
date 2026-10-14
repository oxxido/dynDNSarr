# Estado

**Actualizado:** 2026-10-14

## Proyecto

- Agent y Hub mínimos: arrancan, exponen `/healthz`, persisten en su SQLite (migraciones, WAL) y escriben logs JSON sin secretos. No hay lógica DDNS ni de producto todavía.
- Rama base: `main`. Remoto `origin`: git@github.com:oxxido/dynDNSarr.git.
- Slice activo: **slice1** (Fase 0, monorepo y entorno), en la rama `slice/01-bootstrap`. Ver [docs/slices/01-bootstrap.md](docs/slices/01-bootstrap.md) (estado y evidencia).

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
