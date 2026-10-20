# DynDNSarr

Administrador DDNS para homelabs: un **Agent** autónomo en una Raspberry Pi y un **Hub** web en un servidor Docker. v1.0 incluye DonDominio, monitorización y diagnóstico DNS, administración privada, landing pública, Pi-hole v6, Wake-on-LAN por dispositivo, alertas SMTP/webhook compatibles con Slack y widget Homarr. Las integraciones son opcionales y sus fallos no detienen DDNS. Instalación y configuración: [operaciones](docs/operations.md), especialmente §10 para integraciones y release.

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

La configuración del proceso se lee de variables de entorno ([.env.example](.env.example)); el motor DDNS del Agent, de un TOML ([apps/agent/config/agent.example.toml](apps/agent/config/agent.example.toml)) apuntado por `DYNDNSARR_AGENT_CONFIG_FILE`. Consola del Agent: `dyndnsarr-agent status | check-ip | check-dns | sync-ddns`. Consola del Hub: `dyndnsarr-hub agent-add | agent-rotate | agent-revoke | agent-list | events`.
