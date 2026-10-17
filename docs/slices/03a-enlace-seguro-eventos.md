# Slice 3a — Enlace seguro y entrega de eventos (Agent → Hub)

- **Estado:** planificado
- **Fase (arquitectura):** 2, primera mitad ([architecture.md](../architecture.md) §3, §5 «Agent → Hub» y «At-least-once», §6, §11 «Pairing», §13 casos 6 y 7)
- **Rama:** `slice/03a-enlace-seguro-eventos`
- **PR:** —

## Objetivo

El Agent entrega al Hub los eventos que ya guarda en SQLite (2a y 2b) por un enlace autenticado y con TLS verificado, con entrega **at-least-once** y deduplicación en el Hub: con el Hub detenido los eventos se acumulan en el Agent y, al volver, llegan **una sola vez** y en orden. El Hub los guarda y se inspeccionan por consola (aún no hay UI). El DDNS no depende del Hub en ningún momento.

La parte 3b (Hub → Agent: health/status, configuración versionada con ack, comandos idempotentes) está en [plan.md](../../plan.md).

## Alcance

**Incluye**

- Contrato versionado en `packages/shared` (`/api/v1`): lote de eventos, resultado del lote (`acked` / `rejected`) y forma de error (§5).
- Hub: `POST /api/v1/agents/{agent_id}/events` con token `Bearer` por agente, límites de tamaño, deduplicación por `(agent_id, event_id)` y ACK después del commit. Migración 0002 (`agents`, `agent_credentials`, `events`).
- Aprovisionamiento manual seguro (A6): CLI del Hub `dyndnsarr-hub agent-add | agent-rotate | agent-revoke | agent-list | events`. El token se muestra una vez y el Hub guarda solo su hash.
- Agent: sección `[hub]` del TOML (URL, `agent_id`, `ca_file` opcional), token por `DYNDNSARR_AGENT_HUB_TOKEN`, migración 0004 (estado de entrega en `events`) y tarea de entrega independiente con backoff y jitter.
- TLS verificado (A2): el Agent exige `https://` (o `http://` solo a loopback) y confía en la CA indicada o en el almacén del sistema. El Hub no termina TLS: lo hace un proxy delante (receta de Caddy con CA local en operaciones).
- `status` del Agent muestra el estado del enlace (pendientes, rechazados, último éxito, último error).
- Diagnóstico y arreglo de la CI `hub-image` (falla desde el slice 1; este slice toca el Hub).
- Documentación de configuración, aprovisionamiento y operación.

**No incluye**

- Cualquier llamada Hub → Agent: `health`, `status`, `config`, `commands`, API interna del Agent, TLS en el Agent (3b).
- UI del Hub, login, DTO público, alertas, estados `healthy/degraded/critical` y uptime (slices 4–6). La vista pública no lee `events`: ver Riesgos.
- Certificados cliente (mTLS), limitación de tasa en el Hub (la aporta el proxy, §3), mDNS, códigos de emparejamiento de un solo uso.
- Eventos nuevos sobre el propio enlace (no se emite «Hub inaccesible»: el estado del enlace vive en `status` y en logs).
- Tope de almacenamiento propio del outbox: lo acota la retención de eventos (365 días por defecto).

## Decisiones previas

Resuelven A2 y A6 de [architecture.md](../architecture.md) §14 para la dirección Agent → Hub. Dejarán una ADR-0005 con D1–D5; 3b la extiende al sentido inverso.

| # | Decisión | Propuesta | Alternativa descartada |
|---|---|---|---|
| D1 | **A2 — TLS** | Proxy TLS delante del Hub (decisión del usuario, 2026-10-17); el Hub sigue en HTTP dentro de Docker y publicado solo en loopback o en la red del proxy. El Agent verifica siempre: `ssl.create_default_context(cafile=ca_file)` (solo esa CA, no el almacén) o el almacén del sistema si no hay `ca_file`. `http://` solo a `localhost`/`127.0.0.1` (pruebas, como 2a D6). Un cliente `httpx` propio para el Hub, separado del de proveedores y fuentes. Sin cambios en Dockerfile ni healthcheck | Uvicorn con cert/clave en el contenedor (obliga a adaptar el healthcheck y a rotar el certificado del Hub); `verify=False`; mTLS |
| D2 | **A6 — Registro** | Manual: `agent-add <agent_id>` crea el agente y su primer token; `agent_id` con `^[a-z0-9][a-z0-9-]{0,62}$`. El administrador copia URL, `agent_id` y token al Agent (`agent.toml` y `agent.env`). Sin descubrimiento ni código de un solo uso. La CLI corre dentro del contenedor (`docker compose exec hub dyndnsarr-hub …`) | Endpoint de registro abierto; mDNS |
| D3 | Token | `dda_` + `secrets.token_urlsafe(32)`. El Hub guarda `sha256` en hex (el token ya tiene 256 bits de entropía: no hace falta un hash lento) y busca por igualdad. Un agente puede tener varios tokens vigentes: `agent-rotate` añade uno sin revocar los anteriores; `agent-revoke` revoca todos menos el último emitido (`--all` los revoca todos y deshabilita el agente). Rotación: rotar → poner el token nuevo en `agent.env` → reiniciar el Agent → `agent-revoke`. El `agent_id` de la ruta debe ser el del token; todo fallo de autenticación responde 401 con el mismo cuerpo (no distingue agente inexistente de token malo) | Un único token (rotar corta el enlace); token en el TOML; comparar en claro |
| D4 | Contrato | `shared/events.py`: `AgentEvent` (campos del §5 + `record_name`, nulo para eventos sin registro; la propuesta del §5 usaba `record_id` en el payload, aquí el nombre ya es la clave del registro), `EventBatch` (`events`), `EventBatchResult` (`acked`: ids, `rejected`: `{event_id, code}`) y `ErrorResponse` (`type`, `code`, `message_safe`, `timestamp`, `request_id`). El Hub valida **cada evento por separado** y no valida el `type` contra una lista (un Agent más nuevo no bloquea al Hub); un evento inválido va a `rejected` con código `invalid_event`, el resto se guarda. `acked` incluye los duplicados (el reenvío es idempotente) | Rechazar el lote entero por un evento (un evento venenoso bloquea la cola); enum cerrado de tipos |
| D5 | Outbox | Se reutiliza `events` (ya tiene `event_id` único y `sequence` monotónica, y se escribe en la misma transacción del ciclo): migración 0004 añade `delivered_at` y `rejected_code`. Pendiente = ambos nulos. Índice parcial sobre pendientes. La purga por retención no cambia: un evento pendiente se purga igual a los 365 días. Con `[hub]` añadido más tarde se envía el histórico retenido | Tabla `outbox_events` aparte que duplica `events` (§6 la nombra, pero este esquema ya cubre su contenido) |

### Entrega

| # | Decisión | Propuesta |
|---|---|---|
| D6 | Tarea de entrega | Tarea asyncio del `lifespan`, separada de la del ciclo DDNS: una excepción o un Hub caído no afectan al DDNS. Cada 30 s comprueba la **BD local** (sin red si no hay pendientes; así también recoge los eventos que escribe `sync-ddns` desde otro proceso). Con pendientes envía lotes de 50 por `sequence` hasta vaciar. Fallo → backoff 30 s ×2 hasta 900 s con jitter ±20 %; éxito → vuelve al ritmo normal. Timeout 10 s. Constantes fijas en código, no configurables. Nunca mantiene una transacción abierta a través de un `await` |
| D7 | Códigos de fallo | Seguros y sin texto de excepción ni URL: `timeout`, `connect_error`, `tls_error`, `auth_failed` (401/403), `rate_limited` (429), `server_error` (5xx), `bad_response`, `http_error`. Los eventos no se descartan por ninguno de ellos. 401/403 se registra una vez por racha (log ERROR) y sigue reintentando (el administrador puede corregir el token sin perder eventos). Se loguea al empezar y al terminar una racha, no por reintento |
| D8 | Límites del Hub | Máximo 100 eventos por lote, 256 KiB de cuerpo (se lee con tope también sin `Content-Length`) y 4 KiB de payload por evento. Se autentica **antes** de leer el cuerpo. Excedidos → 413; un payload grande de un solo evento → `rejected` |

Estado del enlace en el Agent: claves en `app_state` (`outbox_last_success_at`, `outbox_last_error_code`, `outbox_last_error_at`); el contador de pendientes y rechazados se calcula de `events`.

## Diseño mínimo

**Agent (`agent.toml`, `agent.env`)**

```toml
[hub]                                # opcional; sin esta sección no hay entrega
url = "https://hub.example.lan"      # https; http solo a localhost/127.0.0.1
agent_id = "raspberry-home"
ca_file = "/etc/dyndnsarr/hub-ca.pem"   # opcional: CA privada que firma el proxy del Hub
```

`DYNDNSARR_AGENT_HUB_TOKEN` (`SecretStr`, `agent.env` 0600). Con `[hub]` y sin token el arranque aborta con mensaje claro (como las credenciales de DonDominio); un Hub inaccesible nunca impide arrancar.

**Hub (§4)**: `api/events.py` (HTTP), `auth/tokens.py` (emisión y verificación), `db/agents_repo.py` y `db/events_repo.py` (SQLite), `cli.py`; migración `0002_agents_events.sql`: `agents(agent_id PK, created_at, last_seen_at, disabled)`, `agent_credentials(id, agent_id, token_hash UNIQUE, created_at, revoked_at)` y `events(agent_id, event_id, sequence, type, occurred_at, received_at, record_name, payload, PRIMARY KEY (agent_id, event_id))` con índice `(agent_id, sequence)`. El commit de los eventos y la actualización de `last_seen_at` van en **una** transacción, y la respuesta se envía después.

**Agent**: `outbox/client.py` (HTTP, tras un `Protocol` para pruebas), `outbox/loop.py` (D6), `db/ddns_repo.py` (pendientes, marcar entregados/rechazados), migración `0004_outbox.sql`, `ddns/config.py` (`Hub`), `config.py` (token), `main.py` (segundo `AsyncClient` y tarea), `cli.py` (`status`). `apps/agent/pyproject.toml`: añadir `dyndnsarr_agent.outbox` a `packages` y `dyndnsarr_hub.auth` al del Hub (sin esto el wheel no los lleva).

## Plan de implementación

Commits pequeños, tests antes del código. Cada paso termina con `ruff` y `pytest -m "not e2e"` en verde.

1. **CI `hub-image`.** Reproducir `scripts/verify-compose.sh` (`boot_count 1 -> 3`, se esperaba +1) y aislar la causa: script (p. ej. `up -d --wait` recreando el contenedor) o Hub (arranque doble). Corregirla en un commit aparte. *Verifica:* el script termina con OK en local y la CI pasa. Requiere Docker; construir la imagen es una acción local que se confirma con el usuario antes de ejecutarla.
2. **ADR-0005** (D1–D5) y su entrada en el índice. *Verifica:* enlaces válidos.
3. **Contrato en `shared`.** Modelos de D4 y `ErrorResponse`. *Verifica:* forma según §5 (`contract/`), `extra` rechazado donde corresponda, tipos y UUID/UTC válidos.
4. **Hub: migración 0002 y repositorios.** *Verifica:* aplica sobre una BD 0001; `INSERT OR IGNORE` por `(agent_id, event_id)` no duplica; `token_hash` único; un token revocado no autentica; solo se guarda el hash (el token en claro no está en ninguna tabla).
5. **Hub: API de eventos.** Auth, límites, validación por evento. Con `httpx.ASGITransport`. *Verifica:* 401 idéntico sin cabecera, con token malo, con agente inexistente y con agente deshabilitado; 403 si el token es de otro `agent_id`; duplicados → `acked` sin segunda fila; un evento inválido → `rejected` y los demás guardados; 413 por lote, cuerpo y payload; la autenticación ocurre antes de leer el cuerpo; el token no aparece en logs (canario).
6. **Hub: CLI.** `agent-add`, `agent-rotate`, `agent-revoke` (con `--all`), `agent-list`, `events`. *Verifica:* el token se imprime una vez y no se puede volver a obtener; rotar no corta el token anterior; revocar deja solo el último; `agent-list` y `events` no muestran hashes ni tokens; agente repetido o id inválido → error claro.
7. **Agent: configuración, migración 0004 y repositorio.** `[hub]`, token, `delivered_at`/`rejected_code`. *Verifica:* el ejemplo carga; `http://` no loopback, URL sin host, `ca_file` inexistente y `[hub]` sin token dan error claro (sin el valor del token); migra una BD 0003 con eventos y quedan pendientes; marcar entregado/rechazado persiste entre conexiones; la purga por retención no cambia.
8. **Agent: cliente y bucle de entrega.** *Verifica:* envío en orden de `sequence` en lotes de 50; solo se marca tras el ACK; cada código de D7 con su caso (incluido `tls_error`); backoff 30→60→…→900 con reloj y `sleep` inyectados; sin pendientes no hay red; **TLS real** contra un servidor Uvicorn con CA de `trustme`: con `ca_file` correcto entrega, sin la CA falla con `tls_error` y los eventos siguen pendientes, con una CA distinta falla igual; canario: ni el token ni la URL en logs, `app_state`, `events` ni salida del CLI.
9. **Integración Agent ↔ Hub en proceso.** Hub real (`create_app`) detrás de `ASGITransport`, Agent real sobre BD temporal. *Verifica:* §13-6 (Hub caído varios ciclos con reloj inyectado, vuelve y llegan todos una vez y en orden); **ACK perdido** (el Hub guarda y la respuesta se pierde → el Agent reenvía → una sola fila); §13-7 (reenvío manual del mismo lote → una sola fila); evento rechazado: no bloquea los siguientes y queda marcado; token revocado → eventos retenidos y entrega tras corregirlo; un Hub caído no retrasa ni cambia el resultado de un ciclo DDNS.
10. **Lifespan y `status`.** Segundo cliente, tarea cancelable, aborto por token ausente. *Verifica:* sin `[hub]` el comportamiento de 2b no cambia; con Hub inaccesible el Agent arranca y hace su ciclo; `status` muestra pendientes, rechazados, último éxito y último error, y sigue sin escribir.
11. **E2E con procesos reales.** Agent + Hub (Uvicorn) en loopback, `FakeIpSource` y `FakeDns`. *Verifica:* criterios 1–3 y 6.
12. **Documentación y despliegue.** `agent.example.toml`, `.env.example` (`DYNDNSARR_AGENT_HUB_TOKEN` vacío), `docs/operations.md` (aprovisionamiento, rotación, receta de Caddy con CA local, exportar la CA a la Raspberry, comprobación manual), `docs/conventions.md` («Fijado en slice3a»), `ESTADO.md`, `plan.md`, índice, `scripts/verify-wheels.sh` (importar `dyndnsarr_agent.outbox` y `dyndnsarr_hub.auth`).

## Criterios de aceptación

1. En un venv limpio con Python 3.11 y 3.12, `ruff check`, `ruff format --check` y `pytest` terminan con éxito.
2. **E2E (§12 Fase 2):** con el Hub detenido, el Agent genera eventos (`agent_started`, `ip_changed`, …) y siguen sin entregarse; al arrancar el Hub llegan **todos exactamente una vez**, en orden de `sequence`, con el mismo `event_id`.
3. Reenvío duplicado (ACK perdido, o el mismo lote enviado dos veces) → una sola fila en el Hub por `(agent_id, event_id)`.
4. Sin token, con token inválido, revocado o de otro agente → 401/403 y ningún evento guardado; los eventos siguen pendientes en el Agent y se entregan al corregir el token.
5. TLS verificado: contra un servidor HTTPS con una CA que el Agent no conoce la entrega falla con `tls_error` y no hay forma de desactivar la verificación; con `ca_file` correcto funciona.
6. El Agent arranca, ejecuta el ciclo DDNS y actualiza registros `managed` con el Hub caído o mal configurado; un evento rechazado no bloquea los siguientes.
7. Ni el token ni el URL aparecen en logs, `events`, `app_state`, salida del CLI ni en el Hub (logs y BD solo guardan el hash). Prueba con valor canario.
8. `status` del Agent y `agent-list`/`events` del Hub funcionan sin escribir ni revelar secretos; rotar y revocar tokens sigue el procedimiento de D3 sin perder eventos.
9. Los wheels en venv limpio ejecutan `dyndnsarr-agent status` y `dyndnsarr-hub agent-list` (módulos nuevos y migraciones viajan).
10. La CI de GitHub pasa en el PR, incluida `hub-image`.
11. **Manual del usuario:** Hub en Docker detrás de Caddy (CA local), `agent-add`, Agent real con `ca_file`: ver llegar `agent_started`; parar el Hub, forzar eventos (`sudo -u dyndnsarr … sync-ddns` o reiniciar el Agent), arrancarlo y comprobar con `dyndnsarr-hub events` que llegan una sola vez.

## Pruebas

- `tests/unit`: contrato, tokens y repositorios del Hub, migraciones 0002 y 0004, configuración `[hub]`, backoff y códigos del cliente.
- `tests/integration`: API de eventos con `ASGITransport`, CLI del Hub, bucle de entrega con reloj y `sleep` inyectados, Agent ↔ Hub en proceso (Hub caído, ACK perdido, rechazados, token revocado), TLS con CA de `trustme`.
- `tests/contract`: forma de `AgentEvent`, lote, resultado y error según §5; `/healthz` de ambos sin cambios.
- `tests/e2e` (marca `e2e`): Agent y Hub como procesos reales en loopback.
- Dependencia de desarrollo nueva: `trustme` en el extra `dev` (certificados de prueba; trae `cryptography`). Sin dependencias nuevas en tiempo de ejecución.
- Sin servicios reales en la suite automática. Ejecución: `pytest` / `pytest -m "not e2e"`.

## Documentación a actualizar

`docs/operations.md` (§3 Hub y nueva sección del enlace), `.env.example`, `apps/agent/config/agent.example.toml`, `docs/conventions.md`, `docs/decisions/0005-*.md` y su índice, `ESTADO.md`, `plan.md`, `docs/slices/README.md`, `apps/hub/compose.yaml` (solo comentario sobre publicar el puerto tras el proxy), `scripts/verify-wheels.sh`, `pyproject.toml` (extra `dev`).

## Riesgos y migraciones

- **Token en HTTP plano si se publica el Hub sin proxy:** con `http://` fuera de loopback el Agent se niega a arrancar, pero nada impide abrir el puerto del Hub a la LAN. Se documenta que el puerto se publica solo en loopback o en la red del proxy.
- **El Hub guarda IP completa en `events`:** es el comportamiento esperado de la vista privada, pero el DTO público del slice 4 no debe leer esta tabla. Se anota en la ADR y en `conventions.md`.
- **Sin limitación de tasa en el Hub:** un token válido filtrado puede llenar `events` hasta los límites por lote. Mitigación: revocar (D3); la limitación por IP se delega al proxy (§3).
- **Agent más nuevo que el Hub:** tipos de evento desconocidos se almacenan (D4); campos nuevos obligatorios del contrato exigirán subir a `/api/v2`.
- **Histórico al activar `[hub]`:** se envían hasta 365 días de eventos retenidos en lotes de 50; es un volumen pequeño pero distinto de «solo desde ahora».
- **Receta de Caddy y exportación de la CA** no se prueban contra la infraestructura real del usuario hasta el criterio 11.
- **CI `hub-image`:** causa desconocida; si el diagnóstico (paso 1) resulta ajeno al código del slice, se corrige igualmente en un commit aparte para que el PR pueda pasar la CI.
- **Migración 0004** solo añade columnas (`ALTER TABLE ADD COLUMN`), sin reconstruir tablas; no hay vuelta atrás automática (respaldar `agent.db`, operaciones §4). Migración 0002 del Hub crea tablas nuevas.

## Evidencia

Comandos ejecutados, salidas relevantes, enlaces. Se rellena al implementar.
