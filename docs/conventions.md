# Convenciones de código

Derivadas de [architecture.md](architecture.md) §0, §2 y §4. Si algo no está aquí ni allí, se decide en el slice y se anota.

## Stack

- Python 3.11+ (la Raspberry trae 3.11.2, ADR-0002); no usar sintaxis ni librería estándar exclusivas de 3.12. FastAPI/Uvicorn, Jinja2, HTMX solo donde aporte, SQLite por componente.
- No añadir Redis, PostgreSQL, MQTT, Celery, React, Node.js ni microservicios sin una necesidad real documentada.
- Evitar ORM pesado: `sqlite3` con repositorios y migraciones explícitas, si basta.
- Un solo worker por proceso (evita schedulers duplicados).

## Estructura

- Monorepo: `apps/agent`, `apps/hub`, `packages/shared` (contratos Pydantic versionados, sin secretos).
- No mezclar infraestructura HTTP, reglas DDNS y acceso SQLite en un mismo archivo.

## Reglas de dominio

- El Agent nunca depende del Hub para el DDNS. Pi-hole, Homarr, correo y web son opcionales.
- El Hub no modifica DNS; pide operaciones al Agent.
- La vista pública usa un DTO propio. Nada de IP completa, MAC, nombres de dispositivo ni secretos.
- Ningún indicador operativo sale de datos inventados: viene de una observación con marca temporal o es `unknown`.
- Registros `monitored`: nunca se escribe.

## Seguridad

- Sin secretos en repositorio, logs, eventos ni exportaciones.
- TLS siempre verificado: prohibido `verify=False`.
- Sin comandos arbitrarios del sistema ni destinos no autorizados.
- Operaciones de modificación nunca por GET.

## Pruebas

- Pruebas automatizadas antes de integrar con DonDominio real o exponer endpoints administrativos.
- Cada slice entrega código, tests, configuración de ejemplo, instrucciones de arranque, migraciones y criterios de aceptación demostrables.
- No inventar el API de DonDominio: verificar la documentación oficial antes de codificar el adaptador.

## Fijado en slice1

Detalle y alternativas descartadas: [slices/01-bootstrap.md](slices/01-bootstrap.md) (D1–D9).

- Entorno y paquetes: `venv` + `pip`; tres `pyproject.toml` (setuptools) en `packages/shared`, `apps/agent` y `apps/hub`; el `pyproject.toml` raíz solo lleva ruff, pytest y el extra `dev`.
- Lint/format: `ruff check` y `ruff format`.
- Migraciones: `NNNN_nombre.sql` en `db/migrations/` de cada app, aplicadas en transacción por el runner de `shared`; versión en `PRAGMA user_version`.
- Config: `pydantic-settings`, solo variables de entorno `DYNDNSARR_AGENT_*` / `DYNDNSARR_HUB_*`; escucha en `127.0.0.1` por defecto.
- Logging: `logging` estándar con salida JSON y redacción de claves sensibles (`password`, `token`, `secret`, `key`, `authorization`). Los valores sensibles deben pasar como `extra=`: un secreto dentro del texto del mensaje no se redacta.
- Idioma: identificadores y nombres de evento de log en inglés; comentarios, docstrings, mensajes de error y documentación en español.
- Config, lifespan y `/healthz` se escriben por separado en cada app; solo se comparte lo que ya comparten por necesidad (SQLite, logging, contrato de salud).
