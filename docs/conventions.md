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

## Fijado en slice2a

Detalle: [slices/02a-observacion-ip-dns.md](slices/02a-observacion-ip-dns.md) (D1–D7) y [ADR-0003](decisions/0003-motor-ddns-observacion.md).

- Config del motor DDNS: TOML sin secretos (`DYNDNSARR_AGENT_CONFIG_FILE`); las variables de entorno siguen siendo la config del proceso. Los secretos van por variables de entorno, nunca en el TOML (2b: credenciales de DonDominio).
- Capas del Agent: `ddns/` (reglas y orquestación: `config`, `ip_sources`, `cycle`), `dns/` (cliente DNS tras el `Protocol` `DnsObserver`), `db/ddns_repo.py` (SQLite) y `scheduler.py`; la red se observa primero y el ciclo escribe todo en una sola transacción.
- IP pública: «confirmada» solo con ≥2 fuentes que coinciden y IPv4 global; en otro caso `uncertain` (nunca se concluye por mayoría ni con una fuente).
- Eventos solo en cambios de estado; `sequence` es `AUTOINCREMENT` (no se reutiliza tras purgar). Instantes en UTC `YYYY-MM-DDTHH:MM:SSZ`.
- Errores de red/DNS se guardan y registran como códigos seguros (`timeout`, `nxdomain`, `connect_error`…), nunca con el texto de la excepción ni la URL. `httpx`/`httpcore` en `WARNING`.
- TLS siempre verificado; `http://` solo a `localhost`/`127.0.0.1` y solo en la configuración de fuentes (para pruebas).
- CLI: `dyndnsarr-agent` sin argumentos sirve; `status` solo lee, `check-*` guardan muestras sin tocar estado ni eventos.
- Pruebas: servidores falsos en loopback (`FakeDns`, `FakeIpSource` en `tests/conftest.py`); nada de servicios reales.

## Fijado en slice2b

Detalle: [slices/02b-actualizacion-ddns-managed.md](slices/02b-actualizacion-ddns-managed.md) (D1–D7) y [ADR-0004](decisions/0004-actualizacion-ddns-dondominio.md).

- Capas nuevas: `providers/` (adaptador HTTP tras el `Protocol` `DnsProvider`; `registry.py` lo construye), `ddns/reconcile.py` (`decide`, backoff: reglas puras sin E/S) y `ddns/cycle.py` (orquesta). Un `monitored` pasa por la rama de 2a, que no recibe el proveedor.
- El ciclo observa la red, llama al proveedor (una vez por registro `managed` y ciclo, 2 s de pausa entre registros) y escribe estado, intentos y eventos en **una** transacción. Un fallo del proveedor es un `UpdateResult`, no una excepción.
- «Aceptado» no es «correcto»: tras una llamada aceptada el registro queda `pending_verification` hasta que los resolvers externos devuelven la IP; mientras tanto no se vuelve a llamar con esa IP.
- Backoff 60 s ×2 hasta 3600 s, persistido en `record_state` (`next_attempt_at`). `failures > 0` con `next_attempt_at` nulo es un error definitivo. Un fallo solo vale para la IP con la que ocurrió.
- Eventos de fallo del proveedor solo al empezar una racha o cambiar el código (§7.7), no por reintento.
- Credenciales de DonDominio solo por `DYNDNSARR_AGENT_DONDOMINIO_USER`/`_KEY` (`SecretStr`). La clave viaja en la URL (API por GET): `httpx`/`httpcore` en `WARNING` es un control de seguridad (su INFO imprime la URL), los adaptadores devuelven códigos seguros y nunca el texto de una excepción, y hay pruebas con valor canario.
- `flock` no bloqueante sobre `<data_dir>/ddns.lock` coordina el daemon con `sync-ddns`. `sync-ddns` ignora backoff y bloqueo por error definitivo, no la deduplicación.
- Pruebas: `FakeDonDominio` en loopback (`tests/conftest.py`) que, al aceptar, cambia lo que responde `FakeDns`; fixtures del adaptador tomados de respuestas reales.

## Fijado en slice3a

Detalle: [slices/03a-enlace-seguro-eventos.md](slices/03a-enlace-seguro-eventos.md) (D1–D8) y [ADR-0005](decisions/0005-enlace-agent-hub.md).

- Contrato Agent → Hub en `packages/shared/dyndnsarr_shared/events.py` (`/api/v1`): `AgentEvent` ignora campos desconocidos; `EventBatch`, `EventBatchResult` y `ErrorResponse` los rechazan. El Hub valida **cada evento por separado** (uno inválido va a `rejected` y no bloquea el lote) y `acked` incluye los duplicados.
- TLS siempre verificado: el cliente del Hub se construye con `build_ssl_context(ca_file)` (solo esa CA, o el almacén del sistema). `http://` solo a loopback. El Hub no termina TLS: va detrás de un proxy.
- Token por agente: `dda_` + 32 bytes aleatorios, solo el `sha256` en la BD del Hub, `SecretStr` por entorno (`DYNDNSARR_AGENT_HUB_TOKEN`) en el Agent. Todo fallo de autenticación responde 401 con el mismo cuerpo; la autenticación va **antes** de leer el cuerpo. Ni el token ni la URL del Hub salen en logs, eventos, `app_state` ni CLI: el cliente devuelve códigos seguros (`timeout`, `connect_error`, `tls_error`, `auth_failed`…), nunca el texto de una excepción.
- Outbox = la tabla `events` del Agent (`delivered_at`, `rejected_code`; pendiente = ambos nulos). Solo se marca tras el ACK; un rechazo explícito no se reenvía ni bloquea.
- La tarea de entrega es independiente del ciclo DDNS: cliente `httpx` y **conexión SQLite propias**, y nunca mantiene una transacción abierta a través de un `await`. Backoff 30 s ×2 hasta 900 s con jitter ±20 %; la racha de fallos se loguea al empezar y al terminar.
- `connect()` solo abre un descriptor propio del archivo al crearlo: en POSIX, cerrar cualquier descriptor de la BD libera los bloqueos de las demás conexiones del proceso y se pierden escrituras. Con más de una conexión por proceso, no abrir ni cerrar la BD por otra vía.
- El Hub guarda la IP completa en `events` (vista privada): el DTO público (slice 4) no debe leer esa tabla.
- CLI del Hub: `dyndnsarr-hub agent-add | agent-rotate | agent-revoke [--all] | agent-list | events`; el token se imprime una vez.
- Pruebas: Hub real tras `ASGITransport` para el enlace en proceso, y `TlsHub` (Uvicorn con CA de `trustme`, `tests/conftest.py`) para TLS real.

