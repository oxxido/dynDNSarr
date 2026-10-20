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

## Fijado en slice3b

Detalle: [slices/03b-control-hub-agent.md](slices/03b-control-hub-agent.md) (D1–D6) y [ADR-0006](decisions/0006-control-hub-agent.md).

- Contrato Hub → Agent en `packages/shared/dyndnsarr_shared/control.py` (`/internal/v1`): todos los modelos rechazan campos desconocidos. `config_hash` es el `sha256` del JSON canónico (claves ordenadas, sin espacios): su vector está fijado en `tests/contract` y cambiarlo rompe el enlace. `dyndnsarr_shared/tls.py` tiene `build_ssl_context` y `has_tls_error` para ambos clientes `httpx`.
- La API interna solo se monta con `DYNDNSARR_AGENT_API_TOKEN` (que exige `CONFIG_FILE`); con token y `HOST` fuera de loopback, el Agent no arranca sin los dos archivos TLS. La autenticación va **antes** de leer el cuerpo y todo fallo es el mismo 401; el cuerpo tiene tope (256 KiB la configuración, 16 KiB un comando) también sin `Content-Length`. Ningún `GET` modifica estado.
- Configuración activa = TOML + secciones de la versión remota (`ddns/active_config.py`). Solo `check_interval_seconds`, `ip_sources`, `dns`, `retention`, `update` y `records` son administrables; `providers` y `hub` son locales. El Hub trata el contenido como opaco y el único validador es el Agent (`ddns/apply_config.py`: validar todo, luego una transacción, luego cambiar el `ConfigHolder`). Un ciclo toma la configuración al empezar y la conserva.
- `apply_config` y las altas de comandos son síncronas (sin `await`) sobre la conexión única del Agent: entre comprobar la versión y confirmar no entra otra petición ni un ciclo. Los eventos `config_applied`/`config_rejected`/`config_reset`/`command_completed` llevan versión, hash o código, nunca el contenido ni la IP.
- Comandos (`commands/executor.py`): se persisten antes de ejecutar, un ejecutor serie con cola de 16, un `command_id` repetido devuelve el estado sin reejecutar, y un `received`/`running` al arrancar pasa a `failed/interrupted` y no se reejecuta. Los pases manuales (`ddns/manual.py`) son los mismos que usan `check-dns` y `sync-ddns`. Un resultado de más de 3000 bytes viaja resumido en el evento (el Hub rechaza payloads de 4 KiB).
- Hub: el token de la API se guarda en claro (`agents.api_token`) y nunca se imprime salvo al emitirlo. Un evento de control mal formado se ignora (queda guardado) y un fallo del manejador no rechaza el lote: no debe bloquear el outbox. El estado observado no retrocede (una versión menor no sustituye a la mayor) y `command-show --refresh` con 404 no marca el comando como fallido: solo un rechazo explícito del `POST` es definitivo.
- Pruebas: `TlsAgent` (Uvicorn con las opciones de `serve()` y CA de `trustme`, `tests/conftest.py`); Hub ↔ Agent en un solo bucle asyncio con cada `lifespan_context` y `ASGITransport`; e2e con procesos reales (`tests/e2e/test_hub_control.py`; el outbox entrega cada 30 s, así que las esperas por eventos son largas).

## Fijado en slice4

Detalle: [slices/04-landing-publica.md](slices/04-landing-publica.md) (D1–D7) y [ADR-0007](decisions/0007-landing-publica.md).

- La vista pública vive en `apps/hub/dyndnsarr_hub/public/` (`dto.py`, `state.py`, `builder.py`, `view.py`: reglas, DTO y textos, sin HTTP ni SQL), `db/public_repo.py`, `services/status_refresher.py` y `api/public.py`. Lee solo las tablas `public_*` (migración 0004); la capa pública no importa `events_repo` y una prueba lo comprueba con el autorizador de SQLite.
- `PublicStatusDTO` es una lista blanca (`extra="forbid"`, sin `dict` libre); `ip_suffix` solo admite 1–2 dígitos. La proyección guarda de la IP solo el último octeto (dos dígitos), nunca un hash ni la IP completa; `public_ip` se descarta al leer el `status` del Agent.
- El estado público se **deriva al leer** (`derive_state`, función pura): lo que envejece pasa a `unknown`. Un Agent inalcanzable es `unknown`, nunca `critical`. Los umbrales (15 min, 1 h, 3 × intervalo de refresco) son provisionales hasta el slice 5.
- La petición web nunca llama al Agent: una tarea del `lifespan` (`status_refresher.run_forever`) consulta `GET /internal/v1/status`, escribe en una transacción corta tras la red y registra solo códigos seguros (`connect_error`, `tls_error`…). Un error inesperado no la mata.
- Con `PUBLIC_ENABLED=false` las rutas públicas **no se registran** (el 404 es el de cualquier ruta ausente). Solo `GET`.
- Plantilla Jinja2 con `autoescape`, CSS y JS inline con **CSP por petición con nonce** (sin `'unsafe-inline'`, de modo que la plantilla no puede usar atributos `style=` ni manejadores `on*`; usar clases). Las barras y filas se renderizan en servidor; el único JS es el del tema.
- Pruebas: el Hub real con `TestClient` sembrando `public_*` antes de arrancar (una conexión SQLite por proceso); Agent real tras `ASGITransport` para el refresco; e2e con procesos reales (`tests/e2e/test_hub_control.py`, `STATUS_REFRESH_SECONDS=15`). El canario de IP de las pruebas es una IPv4 global (`83.45.12.134`): el Agent rechaza las de documentación.

## Fijado en slice5

Detalle: [slices/05-hub-privado-diagnosticos.md](slices/05-hub-privado-diagnosticos.md), [ADR-0009](decisions/0009-administracion-web.md) y [ADR-0010](decisions/0010-monitorizacion-privada.md).

- Un administrador CLI, Argon2id, sesiones opacas con solo hash en SQLite, cookie `__Host-` Secure/HttpOnly/SameSite=Lax. Sin alta web, roles ni contraseñas por entorno. Recuperación CLI revoca sesiones y bloqueo persistente.
- `auth/web.py` protege todo `/admin` antes de cuerpo/recursos: sesión, tamaño, CSRF, Origin/Referer contra `ADMIN_ORIGIN` HTTPS. Cabeceras incluso en errores: no-store, CSP por nonce y Referrer-Policy same-origin (Chrome envía Origin null en formularios con no-referrer). Lo público conserva no-referrer. OpenAPI del Hub no se publica; su healthcheck no cambia.
- Persistencia separada en `auth_repo`, `admin_repo` y `private_repo`; HTTP en `api/admin.py`, servicios compartidos de envío/reconciliación en `services/control.py` y mutaciones web en `services/admin_actions.py`. No mantener transacciones durante await/red. El actor de comandos viene de la sesión, nunca del navegador.
- IDs de operación emitidos por Hub, ligados a sesión/Agent/acción y fijados al contenido en primer envío. Edición obsoleta o ID reutilizado con otro contenido → 409. Persistir intención/auditoría antes de la red; timeout no prueba fallo definitivo. Reenvío de config conserva versión/hash y un comando duplicado conserva command_id.
- GET internos nuevos de configuración/diagnóstico con DTO tipados y paginación; no sondeos. Proveedor/hub/secretos locales fuera del DTO. El Agent conserva la validación DDNS; el Hub solo valida forma/límites. Los GET privados leen caché, sin red. Solo POST explícito actualiza diagnóstico.
- `public_monitoring_policy` solo contiene umbrales/versiones: la capa pública sigue sin leer eventos, identidad o datos privados. Misma regla pura parametrizada para estados públicos/privados. `PublicStatusDTO` y las barras públicas no cambian.
- Estado privado deduplicado por evidencia original, cobertura desde recogida hasta vencimiento (sin retroactividad), política histórica fija y huecos desconocidos. IDs de observación incluyen timestamp para soportar su reutilización tras purga del Agent. Métricas privadas por duración; cobertura pública por barras. Fuentes/resolvers no equivalen a disponibilidad independiente de Internet.
- Auditoría sin cuerpos, contraseñas, cookies, CSRF o tokens. Historial/resultados libres solo se presentan mediante campos/veredictos/códigos permitidos y autoescape; códigos remotos desconocidos se resumen como remote_error.

## Fijado en slice6

- Integraciones LAN en `agent/integrations/`, TOML local separado del DDNS, tarea/cliente/conexión
  propios. Política WoL fija destinos; reserva previa al envío, cooldown y command_id persistidos.
- Hub: `alerts_repo`, `services/alert_config`, `notifications` y `maintenance`; la ingestión solo
  registra señales. Ninguna transacción atraviesa SMTP/HTTP. Canales y reglas locales, secretos
  exclusivamente por entorno. IDs estables, backoff y resultados sanitizados (ADR-0011).
- `shared/outbound` fija IP validada y conserva Host/SNI, rechaza redirects/metadatos y exige
  allowlist para redes locales. Clientes de integración con `trust_env=False` y TLS verificado.
- Inventario y alertas solo bajo `/admin`; exportación JSON de metadatos permitidos. Homarr solo
  proyección pública, no datos de integración ni sesiones, y se apaga con la landing.

## Fijado en slice7

- API de servicios y MCP comparten catálogo Pydantic (`service_models`) y autorización
  (`service_api`); las identidades `dds_` son distintas de sesiones y tokens Agent–Hub.
- Cada herramienta vuelve a comprobar permisos exactos; cambios ligados a UUID, actor,
  contenido y revisión. Sin destinos/URLs libres ni concesión de permisos desde herramientas.
- MCP stdio con SDK oficial consume la API HTTPS; stdout solo contiene protocolo.
- Caddy solo modifica el bloque reservado con ETag/If-Match; red fuera de transacciones,
  intenciones/revisiones persistentes y reconciliación independiente. ADR-0012.
- `resource_id` de `check_dns`/`sync_ddns` es el nombre exacto del registro; se valida al
  recibir y ejecutar, y un `monitored` nunca entra en la actualización.
