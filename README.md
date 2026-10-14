# DynDNSarr

Administrador DDNS para homelabs: un **Agent** autónomo en una Raspberry Pi y un **Hub** web en un servidor Docker. Estado actual: ambos componentes arrancan con `/healthz`, SQLite con migraciones y logs JSON sin secretos; el Agent observa IP pública y DNS (slice 2a, solo observación: aún no escribe DNS, ver [docs/operations.md](docs/operations.md) §5).

- Producto: [docs/requirements.md](docs/requirements.md) · Arquitectura: [docs/architecture.md](docs/architecture.md) · Roadmap: [plan.md](plan.md)
- Convenciones: [docs/conventions.md](docs/conventions.md) · Slices: [docs/slices/README.md](docs/slices/README.md)
- Operación (arranque local, Raspberry, Docker, backup): [docs/operations.md](docs/operations.md)

## Estructura

```
packages/shared   contratos Pydantic y utilidades comunes (SQLite/migraciones, logging)
apps/agent        Agent (FastAPI + Uvicorn, systemd)
apps/hub          Hub (FastAPI + Uvicorn, Docker Compose)
tests/            unit, integration, contract, e2e
```

## Arranque rápido (desarrollo)

Requiere Python 3.11 o superior.

```bash
python3 -m venv .venv
.venv/bin/pip install -e packages/shared -e apps/agent -e apps/hub -e ".[dev]"

.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest                 # todo
.venv/bin/pytest -m "not e2e"    # rápido

DYNDNSARR_AGENT_DATA_DIR=./data/agent .venv/bin/dyndnsarr-agent   # http://127.0.0.1:8001/healthz
DYNDNSARR_HUB_DATA_DIR=./data/hub .venv/bin/dyndnsarr-hub         # http://127.0.0.1:8000/healthz
```

La configuración del proceso se lee de variables de entorno ([.env.example](.env.example)); el motor DDNS del Agent, de un TOML ([apps/agent/config/agent.example.toml](apps/agent/config/agent.example.toml)) apuntado por `DYNDNSARR_AGENT_CONFIG_FILE`. Consola del Agent: `dyndnsarr-agent status | check-ip | check-dns`.
