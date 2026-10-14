# Slice 1 — Monorepo y entorno

- **Estado:** en implementación
- **Fase (arquitectura):** 0 ([architecture.md](../architecture.md) §12)
- **Rama:** `slice/01-bootstrap`
- **PR:** pendiente de abrir (contra `main`): `gh` sin sesión válida (ver Evidencia)

## Objetivo

Dejar un monorepo Python en el que Agent y Hub arrancan, exponen `/healthz`, persisten estado en su propia SQLite con migraciones, escriben logs estructurados sin secretos, y se pueden probar, empaquetar y ejecutar (local, systemd, Docker Compose) de forma reproducible. Es la base sobre la que slice2 construye el motor DDNS; aquí no hay lógica DDNS ni de producto.

## Alcance

**Incluye**

- Estructura `packages/shared`, `apps/agent`, `apps/hub`, `tests/`, con tres distribuciones Python independientes (shared, agent, hub).
- Config validada por variables de entorno en cada componente.
- SQLite por componente: modo WAL, runner de migraciones versionadas, migración 0001 con una tabla de estado mínima.
- `GET /healthz` en ambos, con contrato Pydantic versionado en `shared`.
- Logging JSON con redacción de campos sensibles.
- Lint/format (ruff), pytest (unit, integration, contract, e2e), CI en GitHub Actions.
- Unidad systemd del Agent y `Dockerfile` + `compose.yaml` del Hub.
- Documentación: `README.md`, `.env.example`, `docs/operations.md`; actualizar `ESTADO.md`.

**No incluye** (lo indicado en architecture §15 y §0)

- DDNS, DonDominio, fuentes de IP, scheduler, eventos/outbox, autenticación, TLS Agent–Hub, MCP/Caddy, integraciones.
- Páginas con datos simulados; el Hub no sirve la landing ni toca `design_docs/`. Jinja2/HTMX no se instalan todavía.
- Contenedores de desarrollo (devcontainers): el `compose.yaml` del Hub basta.
- Imagen Docker publicada o multiarch (§11: no fijar imágenes no publicadas). `compose.yaml` usa `build:`.
- Prueba de pérdida de energía de SQLite: se aborda cuando haya datos relevantes (slice2). Aquí solo se prueba que una migración fallida no deja la BD a medias.

## Decisiones previas

Propuestas para aprobar junto con este plan; las duraderas pasan a ADR si cambian.

| # | Decisión | Propuesta | Alternativa descartada |
|---|---|---|---|
| D1 | Python mínimo | `>=3.11` ([ADR-0002](../decisions/0002-python-3-11.md)): la Raspberry trae 3.11.2. Desarrollo local en 3.12; la CI prueba 3.11 y 3.12. El Hub en Docker usa `python:3.12-slim` | Instalar 3.12 en la Pi (`uv`/`pyenv`) o ejecutar el Agent en Docker: más piezas o cambia la arquitectura |
| D2 | Empaquetado y entorno | `venv` + `pip`; tres `pyproject.toml` (setuptools) instalados en editable; el `pyproject.toml` raíz solo contiene configuración de ruff/pytest y el extra `dev` | `uv` workspace: no está instalado y no aporta lo suficiente aquí |
| D3 | Migraciones | Archivos `NNNN_nombre.sql` dentro de cada app (`db/migrations/`), aplicados en orden y en transacción por un runner pequeño en `shared`; versión en `PRAGMA user_version`. Se rechaza arrancar si la BD tiene una versión mayor que la conocida | Alembic/ORM: excesivo (conventions: `sqlite3` + migraciones explícitas) |
| D4 | Config | `pydantic-settings`, solo variables de entorno, prefijos `DYNDNSARR_AGENT_` y `DYNDNSARR_HUB_`. Defaults seguros: escucha en `127.0.0.1` | Archivos TOML/YAML: no hacen falta aún |
| D5 | Logging | `logging` estándar con formateador JSON propio y filtro de redacción (claves `password`, `token`, `secret`, `key`, `authorization`, sin distinguir mayúsculas) | `structlog`: dependencia extra sin necesidad |
| D6 | Lint/format | `ruff check` + `ruff format` | black + flake8 |
| D7 | CI | GitHub Actions (el remoto es GitHub) | — |
| D8 | Puertos y rutas por defecto | Agent `8001`, Hub `8000`; datos en `/var/lib/dyndnsarr` (Agent, vía `StateDirectory`) y `/data` (Hub) | — |
| D9 | Código duplicado Agent/Hub | Config, lifespan y `/healthz` se escriben por separado en cada app. Se abstrae solo lo que ya comparten por necesidad (runner SQLite, logging, contrato de salud en `shared`) | Fábrica común de apps: prematura |

Estas decisiones cierran los pendientes de [plan.md](../../plan.md) para slice1 y las de `conventions.md` ("Pendiente de fijar en slice1").

## Diseño mínimo

- **Estado persistido:** migración 0001 crea `app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)`. En cada arranque el componente guarda `instance_id` (UUID, solo la primera vez) e incrementa `boot_count`. Esto permite demostrar persistencia entre reinicios sin inventar datos de producto.
- **`GET /healthz`:** `{"status": "ok", "component": "agent|hub", "version": "x.y.z"}`, HTTP 200. Si la BD no responde: HTTP 503 con `status: "error"`. No expone `boot_count`, rutas ni configuración. Modelo `HealthResponse` en `shared`.
- **Arranque:** `python -m dyndnsarr_agent` / `python -m dyndnsarr_hub` y console scripts `dyndnsarr-agent` / `dyndnsarr-hub`. Un solo worker. El `lifespan` valida config, abre la BD, migra, actualiza `app_state` y cierra al parar.
- **Fichero de BD:** se crea con permisos `0600` (§6: proteger el archivo).
- **Logs:** el arranque registra versión, host, puerto, ruta de datos y versión de esquema; nunca el entorno completo.
- **systemd (Agent):** usuario `dyndnsarr`, `EnvironmentFile=/etc/dyndnsarr/agent.env`, `StateDirectory=dyndnsarr`, `Restart=always`, endurecimiento básico (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`) y límites de recursos.
- **Docker (Hub):** `python:3.12-slim`, usuario no root, volumen `/data`, `HEALTHCHECK` con `urllib` (sin curl), `restart: unless-stopped`.

## Plan de implementación

Commits pequeños, tests antes del código en cada paso. Cada paso termina con `ruff` y `pytest` en verde.

1. **Esqueleto.** `.gitignore`, `pyproject.toml` raíz, tres paquetes vacíos instalables, `tests/` con un test trivial. *Verifica:* `pip install -e` de los tres paquetes y `pytest` funcionan en un venv limpio.
2. **`shared`.** Tests y luego: conexión SQLite (WAL, `foreign_keys`, `busy_timeout`, permisos `0600`), runner de migraciones, `HealthResponse`, logging JSON con redacción. *Verifica:* migraciones idempotentes, migración fallida revierte y no cambia `user_version`, versión de BD futura rechazada, claves sensibles redactadas.
3. **Agent.** Tests y luego: `config.py`, migración 0001, `lifespan`, `/healthz`, `__main__`. *Verifica:* config inválida aborta con error claro; `boot_count` sube en cada arranque con el mismo `instance_id`; `/healthz` devuelve 200 y 503 si la BD falla.
4. **Hub.** Igual que el paso 3.
5. **E2E de arranque.** Tests que lanzan cada componente como proceso real en un puerto libre, consultan `/healthz`, lo paran con SIGTERM y lo relanzan comprobando persistencia.
6. **Despliegue.** Unidad systemd, `Dockerfile`, `compose.yaml`, `.env.example`. *Verifica:* `systemd-analyze verify` sobre la unidad; build y arranque del Hub en Docker (requiere autorización, ver Riesgos).
7. **CI y wheels.** Workflow de GitHub Actions con matriz Python 3.11/3.12 (ruff, pytest, build de wheels) y build de la imagen del Hub y `scripts/verify-wheels.sh`: construye los wheels, los instala en un venv limpio y arranca el Agent para comprobar `/healthz` (detecta migraciones `.sql` que no viajan en el paquete).
8. **Documentación.** `README.md`, `docs/operations.md` (arranque local, instalación en Raspberry con systemd, despliegue en Dockge, backup/restore de `/data`), actualizar `ESTADO.md` con los comandos reales, `docs/conventions.md` (quitar "Pendiente de fijar") y el índice de slices.

## Criterios de aceptación

Arquitectura §12 Fase 0: *ambos arrancan y vuelven a arrancar con datos persistidos; sin secretos en logs.* Concretado:

1. En un venv limpio, `pip install -e` de los tres paquetes, `ruff check`, `ruff format --check` y `pytest` terminan con éxito, con Python 3.11 y 3.12 (la matriz de CI cubre 3.11).
2. Agent y Hub arrancan, `GET /healthz` → 200 con el contrato esperado; tras parar y relanzar, `instance_id` es el mismo y `boot_count` aumenta en 1.
3. Con la BD inaccesible, `/healthz` → 503. Con una BD de versión posterior, el componente no arranca.
4. Una migración que falla no deja cambios ni cambia `user_version`.
5. Los logs de arranque de ambos componentes son JSON, y un test demuestra que un valor bajo una clave sensible se redacta.
6. Los wheels de agent, hub y shared se instalan en un venv limpio (3.11 y 3.12) y el Agent instalado arranca (migraciones incluidas).
7. `systemd-analyze verify` no reporta errores en la unidad del Agent.
8. `docker compose up` levanta el Hub, el healthcheck pasa, y los datos sobreviven a `docker compose restart`.
9. La CI de GitHub pasa en el PR.
10. *(Verificación manual del usuario)* El Agent se instala en la Raspberry con la guía de `operations.md`, `systemctl restart` conserva datos y `Restart=always` lo recupera tras `kill`.

## Pruebas

- `tests/unit`: config, runner de migraciones, redacción de logs, lógica de `app_state`.
- `tests/integration`: apps FastAPI con `TestClient` y BD temporal (persistencia entre dos instancias, 503).
- `tests/contract`: forma exacta de `HealthResponse` en ambos componentes.
- `tests/e2e`: procesos reales, marcados `e2e`.
- Ejecución: `pytest` (todo), `pytest -m "not e2e"` (rápido).

## Documentación a actualizar

`README.md` (nuevo), `docs/operations.md` (nuevo), `.env.example` (nuevo), `ESTADO.md`, `docs/conventions.md`, `docs/slices/README.md`.

## Riesgos y migraciones

- **Divergencia local/Pi.** Local tiene 3.12 y la Pi 3.11.2. Se mitiga con la matriz 3.11 en CI y con la regla de no usar funciones exclusivas de 3.12. El criterio 10 sigue siendo la comprobación real en la Pi.
- **Comandos con efecto global.** Docker (build/run) y cualquier uso de `systemctl` usan el daemon del sistema y requieren autorización explícita del usuario en el momento de ejecutarlos. La unidad systemd solo se redacta y valida; no se instala en el equipo de desarrollo.
- **Migraciones `.sql` fuera del wheel.** Error clásico de empaquetado; lo cubre el criterio 6.
- **GitHub Actions no verificable en local.** La evidencia del criterio 9 llega con la primera ejecución del PR.
- **Dependencias sin lockfile** (`pip` sin `uv`): builds no totalmente reproducibles. Se usan cotas inferiores y superiores de versión mayor; revisar al introducir publicación de imágenes.
- No hay migraciones previas ni datos que preservar.

## Evidencia

Rama `slice/01-bootstrap` publicada en `origin`. Verificado el 2026-10-14 con Python 3.12.3 y 3.11.17:

| Criterio | Resultado |
|---|---|
| 1. venv limpio, `pip install -e`, `ruff check`, `ruff format --check`, `pytest` | Verificado con 3.12.3 y 3.11.17 (66 tests, 0 fallos en ambos). |
| 2. `/healthz` 200; `instance_id` igual y `boot_count` +1 tras relanzar | Verificado: `tests/e2e/test_process_startup.py` (procesos reales, SIGTERM y relanzamiento) y `tests/integration/test_app.py` |
| 3. BD inaccesible → 503; BD de versión posterior → no arranca | Verificado en `tests/integration/test_app.py` |
| 4. Migración fallida no deja cambios ni cambia `user_version` | Verificado en `tests/unit/test_shared_db.py` |
| 5. Logs de arranque JSON y redacción de claves sensibles | Verificado en `tests/unit/test_shared_logging.py` y en el e2e (un valor canario bajo `SOME_API_TOKEN` no aparece en los logs) |
| 6. Wheels instalan en venv limpio y arrancan Agent y Hub | Verificado con 3.12 y 3.11 (`scripts/verify-wheels.sh`). Comprobado también que el script falla si el wheel omite las migraciones. |
| 7. `systemd-analyze verify` sin errores | Verificado sobre una copia con `ExecStart` apuntando al venv local. Sobre el archivo real solo avisa de que `/opt/dyndnsarr/venv/bin/dyndnsarr-agent` no existe (existirá en la Raspberry). |
| 8. `docker compose up`, healthcheck y datos tras `restart` | Verificado con `scripts/verify-compose.sh` (autorizado por el usuario): healthy y `boot_count` 1 → 2 |
| 9. CI de GitHub en el PR | **Pendiente.** El workflow solo se dispara en PR o push a `main`; el PR no se pudo abrir (token de `gh` inválido: `gh auth login -h github.com`). |
| 10. Instalación manual en la Raspberry | **Pendiente (verificación del usuario)** con [docs/operations.md](../operations.md) §2 |

Notas para la revisión:

- `uvicorn` termina con la señal recibida tras un cierre ordenado (código -15 con SIGTERM); el e2e lo acepta y exige el log `Finished server process`.
- La redacción de logs actúa sobre las claves de `extra=`, no sobre el texto del mensaje.
- Procedimientos de backup/restore de Docker y despliegue en Dockge documentados pero no automatizados ni probados.
