# Slice 3b — Control Hub → Agent: estado, configuración versionada y comandos

- **Estado:** en implementación
- **Fase (arquitectura):** 2, segunda mitad ([architecture.md](../architecture.md) §3, §5 «Hub → Agent», «Comandos» y «Configuración», §6, §11, §13 casos 9 y comando duplicado)
- **Rama:** `slice/03b-control-hub-agent`
- **PR:** —

## Objetivo

El Hub puede consultar al Agent y administrarlo por un enlace HTTPS autenticado: leer `health` y `status`, enviarle una **configuración versionada** que el Agent valida de forma atómica (si es inválida conserva la anterior) y pedirle **comandos** limitados (`check_dns`, `sync_ddns`) que se ejecutan una sola vez aunque la petición se repita. Los resultados vuelven al Hub como eventos por el outbox de 3a. El Agent sigue funcionando sin el Hub, y sin el Hub se puede volver a la configuración local.

Como el Hub aún no tiene UI ni login (slice 5), se opera por su CLI.

## Alcance

**Incluye**

- API interna del Agent bajo `/internal/v1` (§5): `GET health`, `GET status`, `PUT config`, `POST commands`, `GET commands/{id}`. Solo existe si hay token configurado.
- TLS en el Agent (Uvicorn con certificado de una CA local) y token Bearer propio del sentido Hub → Agent.
- Configuración versionada: versiones monótonas asignadas por el Hub, hash de contenido, validación atómica en el Agent, almacenamiento en SQLite (`configuration_versions`) y superposición sobre el TOML. `dyndnsarr-agent config-reset` vuelve al TOML.
- Comandos con idempotencia persistida (`commands`), ejecución asíncrona en serie y recuperación tras reinicio.
- Hub: cliente del Agent (CA propia), migración con `desired_config_versions`, `observed_agent_state` y `commands`, y CLI `agent-set-api`, `agent-api-token`, `config-push`, `config-resend`, `config-show`, `agent-status`, `command`, `command-show`.
- Eventos nuevos del Agent: `config_applied`, `config_rejected`, `config_reset`, `command_completed`. El Hub los procesa en la ingestión de 3a (misma transacción, solo si el evento es nuevo).
- Documentación de certificados, aprovisionamiento y operación.

**No incluye**

- UI, login, reconciliación automática del Hub cuando el Agent vuelve, `health` periódico para detectar Agent caído (slice 5; aquí todo lo dispara el administrador).
- `wake_device` y cualquier otro comando (slice 6): se rechaza como `unsupported_command`. Comandos por registro (`resource_id`): en 3b debe ir vacío.
- Gestionar desde el Hub el proveedor, sus credenciales, `[hub]`, TLS o tokens: son locales (D4). Parches parciales de configuración: se envía el documento completo.
- Los endpoints `config-ack` y `command-results` del §5: el ack y el resultado viajan como eventos (D3).
- mTLS, limitación de tasa (la LAN y el firewall acotan el origen, §3), monitorización de caducidad del certificado.
- Historial de cambios navegable o reversión a una versión concreta desde el Hub.

## Ajustes tras 3a (paso 1)

Comprobado contra `main` con 3a fusionada (PR #4). El plan sigue siendo válido; lo que cambia respecto a lo que suponía:

- La ingestión del Hub no es un servicio: vive en `api/events.py` (`post_events`) y `events_repo.insert_events` ignoraba los duplicados sin decir cuáles. Pasa a devolver los eventos **nuevos**, que son los que recibe `services/events_handlers.py` en la misma transacción.
- `build_ssl_context` y la detección de errores TLS salen de `outbox/client.py` a `dyndnsarr_shared/tls.py`: ahora los usan el Agent (hacia el Hub) y el Hub (hacia el Agent).
- Migraciones: Agent 0004 → **0005**, Hub 0002 → **0003** (como estaba previsto). `EVENT_TYPES` de `ddns_repo` gana los cuatro tipos nuevos.
- El Hub no monta la CA del Agent como volumen: un bind mount cuyo origen no existe lo crea Docker como directorio y rompería a quien aún no use 3b. La CA se copia al volumen `/data` (`/data/agent-ca.pem`, valor por defecto de `DYNDNSARR_HUB_AGENT_CA_FILE` en `compose.yaml`).
- `DYNDNSARR_AGENT_API_TOKEN` sin `DYNDNSARR_AGENT_CONFIG_FILE` impide arrancar: la configuración remota se superpone sobre un TOML, y sin él no hay base.
- El holder lleva también el `DnsObserver` (los `dns.resolvers` son administrables y el observador se construye con ellos).
- `config-reset` solo toca la BD: el daemon en marcha conserva su configuración en memoria hasta reiniciarse (la CLI lo avisa).

## Decisiones previas

Este plan **asume 3a tal como está planificada** ([03a-enlace-seguro-eventos.md](03a-enlace-seguro-eventos.md), ADR-0005): tabla `agents` y tokens Agent → Hub del Hub, contrato en `shared/events.py`, outbox y `[hub]` en el Agent. El paso 1 lo recomprueba contra lo realmente implementado. Dejarán una ADR-0006 con D1–D6; amplía A2 al sentido Hub → Agent.

| # | Decisión | Propuesta | Alternativa descartada |
|---|---|---|---|
| D1 | **A2 — TLS del Agent** | Uvicorn sirve HTTPS con `DYNDNSARR_AGENT_TLS_CERT_FILE` y `_KEY_FILE` (certificado firmado por una CA local, receta `openssl` en operaciones). El Hub verifica con `DYNDNSARR_HUB_AGENT_CA_FILE` (solo esa CA, como 3a D1). Con TLS activo `/healthz` también pasa a HTTPS. Con token de API y escucha fuera de loopback, el Agent **no arranca** sin TLS; `http://` solo a loopback (pruebas). Elegido por el usuario (2026-10-18) | Caddy en la Raspberry (un servicio más con `MemoryMax=256M`); `verify=False` |
| D2 | Autenticación Hub → Agent | Token Bearer propio por agente (`ddh_` + `secrets.token_urlsafe(32)`), distinto del Agent → Hub. El Agent lo lee de `DYNDNSARR_AGENT_API_TOKEN` (`SecretStr`) y compara en tiempo constante; todo fallo es 401 con el mismo cuerpo. El Hub, que debe **presentarlo**, lo guarda en claro en `agents.api_token` de `hub.db` (0600, volumen `/data`), no en `.env`: un `.env` de Dockge es visible en su UI y en `docker inspect` (§11 pide separar secretos de variables visibles), es global y no por agente, y rotarlo exigiría recrear el contenedor. `agent-api-token` genera y guarda uno nuevo y lo imprime una vez; hasta actualizar `agent.env` y reiniciar el Agent, el Hub recibe 401 (el DDNS y el outbox no se ven afectados). Su fuga da acceso solo a la API limitada de ese Agent en la LAN, nunca a DonDominio | Token en `.env` del Hub; mTLS; reutilizar el token Agent → Hub (el Hub solo guarda su hash) |
| D3 | Endpoints y acks | `GET /internal/v1/health` y `status`, `PUT /internal/v1/config`, `POST /internal/v1/commands`, `GET /internal/v1/commands/{id}` (§5). El resultado de `PUT config` va en su respuesta; además el Agent emite `config_applied`/`config_rejected` y, al terminar un comando, `command_completed` **por el outbox** (3a): si la respuesta HTTP se pierde, el Hub se entera igualmente, con at-least-once y deduplicación ya resueltos. Se omiten `config-ack` y `command-results` (el §5 es «propuesta inicial, no API aprobada») | Dos endpoints Agent → Hub más, con su propia lógica de reintento e idempotencia |
| D4 | Qué administra el Hub | Solo `check_interval_seconds`, `ip_sources`, `dns`, `retention`, `update` y `records`. Son locales y el Agent los **rechaza** (`forbidden_section`): `providers` (la URL del proveedor lleva la clave de DonDominio en la petición: el Hub no debe poder redirigirla), `hub`, y cualquier secreto, TLS o token (no están en el modelo). El Hub trata el contenido como **opaco** (JSON + hash): no duplica el modelo del Agent; el único validador es el Agent (§3 «valida atómicamente»). Las credenciales siguen en `agent.env`: un registro `managed` nuevo sin ellas se rechaza (`missing_credentials`) | Mover los modelos a `shared` para validar también en el Hub (duplica y acopla); permitir `providers` |
| D5 | Versionado y aplicación | El Hub asigna `version` creciente por agente y calcula `sha256` del JSON canónico (`shared`, orden de claves fijo). El Agent responde `applied` o `rejected` con código: ver tabla. Aplicación: validar **todo** (hash, secciones, `DdnsConfig`, credenciales) → una transacción que inserta la versión → cambiar la referencia en memoria → responder. Lo recoge el **siguiente ciclo** (el ciclo en curso conserva su referencia; no hay cambios a mitad de ciclo). Superposición: el TOML se carga y valida siempre (arranque, secciones locales); si hay versión activa, sus secciones sustituyen a las del TOML y se registra una advertencia si el TOML difiere. Si la activa deja de validar al arrancar (p. ej. faltan credenciales), el Agent no arranca y el mensaje apunta a `config-reset`. `config-reset` borra las versiones, emite `config_reset` y deja el TOML activo | Aplicar a mitad de ciclo; que el TOML gane (el Hub no tendría efecto); reaplicar la última versión válida anterior en silencio |
| D6 | Comandos | Tipos `check_dns` (como `check-dns`: guarda muestras y devuelve el veredicto por registro) y `sync_ddns` (un pase como `sync-ddns`, con el mismo `flock`; con el bloqueo tomado por un ciclo termina `failed/busy`). Parámetros y `resource_id` deben ir vacíos; `expires_at` obligatorio. `POST` valida, persiste `received` y responde **202**; un único ejecutor serial (cola de 16; llena → 429 `queue_full`) lo corre y guarda el resultado. Mismo `command_id` repetido → mismo estado/resultado sin reejecutar (409 si cambia el tipo); expirado → `expired` sin ejecutar. Al arrancar, un comando `received` o `running` pasa a `failed/interrupted` y **nunca se reejecuta solo** (principio de WoL, §5): el Hub emite uno nuevo con otro id | Ejecución síncrona en el `POST` (timeouts y reintentos duplican); reejecutar al arrancar |

### Códigos de `PUT config` (D5)

| Situación | Resultado | Código |
|---|---|---|
| `version` mayor que la activa, hash correcto, válida | `applied` | — |
| Misma `version` y mismo hash que la activa | `applied` (idempotente, sin fila nueva) | — |
| Misma `version`, hash distinto | `rejected` | `version_conflict` |
| `version` menor que la activa | `rejected` | `stale_version` |
| El hash no corresponde al contenido | `rejected` | `hash_mismatch` |
| Sección local o desconocida | `rejected` | `forbidden_section` |
| `DdnsConfig` no valida | `rejected` | `invalid_config` (`message_safe`: `ruta: motivo`, ≤ 1 KiB, sin valores de credenciales) |
| `managed` habilitado sin credenciales locales | `rejected` | `missing_credentials` |
| Cuerpo > 256 KiB | 413 | — |

Tras `config-reset` la versión activa es 0 (TOML): el Hub puede reenviar la versión que tenía con `config-resend`.

### Estado en el Hub

`observed_agent_state` guarda `config_version`, `config_hash`, resultado y fecha del último intento. Se actualiza por la respuesta del `PUT` y por los eventos (D3), con la misma función idempotente: una versión menor que la observada no la hace retroceder, y un evento duplicado (la fila ya existía) no se procesa. Los comandos del Hub (`commands`) pasan de `sent` a `succeeded`/`failed`/`expired` igual.

## Diseño mínimo

**Agent (`agent.env`)**: `DYNDNSARR_AGENT_API_TOKEN`, `DYNDNSARR_AGENT_TLS_CERT_FILE`, `DYNDNSARR_AGENT_TLS_KEY_FILE`. `Settings` falla al validar si hay un solo archivo TLS; sin token no se monta `/internal`.

**Contrato (`shared/control.py`)**: `ConfigPut` (`version`, `hash`, `content`), `ConfigResult` (`result`, `version`, `code`, `message_safe`), `CommandRequest` (campos del §5), `CommandStatus`, `AgentHealth` (`agent_id`, `version`, `status`, `time_utc`, `last_local_dns_check_at`) y `AgentStatus` (IP, estado por registro, versión de config, enlace con el Hub). Más `config_hash(content)`. Son privados: el `status` lleva la IP completa y no se expone al público (slice 4).

**Agent**: `api/internal.py` (HTTP y autenticación), `ddns/active_config.py` (`ConfigHolder`: configuración y proveedores vigentes; superposición sobre el TOML; `build_providers` se vuelve a llamar al cambiar, porque un registro `managed` nuevo necesita su proveedor), `ddns/apply_config.py` (D5, sin E/S de red), `commands/` (ejecutor y repositorio), `ddns/manual.py` (el pase de `sync-ddns` extraído de `cli.py` para que lo usen CLI y comando), migración `0005_control.sql`: `configuration_versions(version PK, content_hash, content, applied_at)` y `commands(command_id PK, type, requested_by, received_at, expires_at, status, finished_at, result)`. `scheduler.run_forever` lee del holder y los CLI (`check-*`, `sync-ddns`, `status`) usan la configuración activa. `main.py`: TLS en `uvicorn.run`, router y ejecutor. `apps/agent/pyproject.toml`: añadir `dyndnsarr_agent.commands` a `packages`.

**Hub**: migración `0003_control.sql` (tras la 0002 de 3a): `agents.api_url`, `agents.api_token`, `desired_config_versions(agent_id, version, content_hash, content, created_at, PRIMARY KEY (agent_id, version))`, `observed_agent_state(agent_id PK, …)` y `commands(command_id PK, agent_id, type, requested_by, created_at, expires_at, status, result)`; `services/agent_client.py` (httpx con la CA, timeout 10 s, códigos seguros), `services/events_handlers.py` (D3, llamado desde la ingestión), `cli.py` ampliado. `config-push FILE.toml` convierte el TOML a JSON (`tomllib`), crea la versión siguiente y la envía; el Hub nunca lee `agent.env`.

## Plan de implementación

Commits pequeños, tests antes del código. Cada paso termina con `ruff` y `pytest -m "not e2e"` en verde. Si el slice resulta grande, los pasos 8 y 13 (comandos) pueden separarse como un 3c sin tocar el resto.

1. **Recomprobar contra 3a.** Rebasar sobre `main` con 3a fusionada y ajustar nombres de módulos, tablas y numeración de migraciones de este plan. *Verifica:* este archivo describe lo que existe.
2. **ADR-0006** (D1–D6) y su entrada en el índice. *Verifica:* enlaces válidos.
3. **Contrato en `shared`** (`control.py`, `config_hash`). *Verifica:* forma según §5 (`contract/`); el hash es estable (vector fijo) e independiente del orden de claves; `extra` rechazado.
4. **Agent: ajustes locales** (token, TLS). *Verifica:* un solo archivo TLS falla; token + `host` no loopback + sin TLS aborta con mensaje claro; sin token no hay `/internal`; el valor del token nunca está en el mensaje de error.
5. **Agent: migración 0005 y repositorios.** *Verifica:* aplica sobre una BD 0004 con datos; persisten versiones y comandos entre conexiones; purga de `commands` con la retención de eventos.
6. **Agent: configuración activa.** `ConfigHolder`, superposición, `config-reset`, uso desde scheduler y CLI. *Verifica:* sin versiones el comportamiento de 2b no cambia (suite existente en verde, con el ajuste mecánico de `run_forever` en los tests del scheduler); con versión, sus secciones mandan y las locales (`providers`, `hub`) vienen del TOML; una versión guardada inválida impide arrancar y nombra `config-reset`; `config-reset` emite `config_reset` y restaura el TOML; el holder da proveedores para un `managed` añadido en caliente.
7. **Agent: aplicar configuración.** `apply_config` según la tabla. *Verifica:* una prueba por fila; **atomicidad** (fallo inyectado tras validar y antes de confirmar: ni fila nueva ni cambio en el holder); idempotencia; un `managed` nuevo sin credenciales se rechaza; eventos `config_applied`/`config_rejected` con la versión y el hash, sin contenido; el ciclo en curso no cambia de configuración.
8. **Agent: comandos.** Repositorio, ejecutor serial, extracción de `ddns/manual.py`. *Verifica:* mismo `command_id` dos veces → una ejecución (proveedor falso con contador); mismo id y otro tipo → 409; expirado; `wake_device` y parámetros no vacíos rechazados; cola llena; ejecución en serie; `busy` con `ddns.lock` tomado; tras reinicio `received`/`running` → `failed/interrupted` sin reejecutar; `command_completed` una vez; `sync-ddns` por CLI sigue igual (suite de 2b).
9. **Agent: API interna.** Autenticación, `health`, `status`, `PUT config`, `POST`/`GET commands`. *Verifica:* 401 idéntico sin cabecera y con token malo; `GET` no modifica nada; cuerpo > 256 KiB → 413 sin leerlo entero; `status` coincide con lo que muestra la CLI y no contiene credenciales; `/healthz` sin cambios de contrato.
10. **Agent: TLS.** Certificados en `uvicorn.run`. *Verifica:* contra una CA de `trustme`, un cliente con esa CA conecta, sin ella o con otra falla; no hay forma de desactivar la verificación.
11. **Hub: migración 0003 y repositorios.** *Verifica:* aplica sobre una BD de 3a; versiones monótonas por agente y únicas; el token de API no aparece en `agent-list` ni en logs.
12. **Hub: cliente del Agent y manejadores de eventos.** *Verifica:* códigos seguros (`timeout`, `connect_error`, `tls_error`, `auth_failed`, `bad_response`…) con TLS real; un evento duplicado no se procesa dos veces; un evento con versión menor no hace retroceder el estado; el comando pasa a su estado final por el evento aunque la respuesta HTTP se perdiera.
13. **Hub: CLI.** *Verifica:* `config-push` crea la versión y muestra resultado y código; con el Agent caído la versión queda pendiente y `config-resend` la reenvía; `command` imprime el id y `command-show` el estado; `agent-api-token` imprime el token una vez; `config-show`/`agent-status` no muestran tokens.
14. **Integración Hub ↔ Agent en proceso** (cada uno tras su `ASGITransport`). *Verifica:* configuración válida (aplicada y usada por el ciclo siguiente), inválida (el Agent conserva versión y comportamiento; el Hub la ve rechazada), reenvío idempotente, versión vieja, y el §13 «Configuración inválida desde Hub → Agent conserva última versión activa»; comando duplicado y comando con Hub caído (resultado por evento al volver).
15. **E2E con procesos reales** y TLS (CA de `trustme`): Agent + Hub + `FakeIpSource`/`FakeDns`/`FakeDonDominio`. *Verifica:* criterios 2–6.
16. **Documentación y despliegue.** `.env.example` (variables nuevas vacías), `apps/hub/compose.yaml` (montaje de la CA del Agent), `docs/operations.md` (CA local y certificado del Agent con `openssl`, token de API, `config-push`, comandos, `config-reset`, rotación, reloj/NTP), comentario en `dyndnsarr-agent.service`, `docs/conventions.md` («Fijado en slice3b»), `ESTADO.md`, `plan.md` (retirar «Insumos para slice3b»), índice, `scripts/verify-wheels.sh` (importar `dyndnsarr_agent.commands`).

## Criterios de aceptación

1. En un venv limpio con Python 3.11 y 3.12, `ruff check`, `ruff format --check` y `pytest` terminan con éxito.
2. **E2E (§12 Fase 2):** el Hub envía una configuración inválida → el Agent responde `rejected`, `status` sigue mostrando la versión y los registros anteriores y el siguiente ciclo se comporta como antes; el Hub la ve rechazada (por la respuesta y por el evento). Una configuración válida → `applied`, versión y hash visibles en `status` y el siguiente ciclo usa los registros nuevos.
3. Reenviar la misma versión → `applied` sin fila nueva; versión menor → `stale_version`; misma versión con otro contenido → `version_conflict`; hash incorrecto → `hash_mismatch`.
4. Atomicidad: un fallo durante la aplicación no deja versión nueva ni cambia la configuración en memoria (prueba con fallo inyectado).
5. Comando duplicado (mismo `command_id`) → **una sola ejecución** (proveedor falso invocado una vez) y el mismo resultado; un reinicio del Agent a mitad de un comando lo deja `interrupted` y no lo reejecuta; un comando expirado no se ejecuta; `wake_device` se rechaza.
6. Seguridad del enlace: sin token, con token malo, con una CA no confiable, o con token y escucha fuera de loopback sin TLS → no hay acceso/arranque; ningún `GET` modifica estado; sin token no existe `/internal`. Una sección local (`providers`, `hub`) en la configuración remota se rechaza.
7. Ni el token de API, ni la clave de DonDominio ni sus URL aparecen en logs, eventos, respuestas de la API, `status`, salida de ambas CLI ni en `agent-list`/`config-show` del Hub (prueba con valor canario).
8. El Agent funciona sin el Hub: DDNS, configuración activa, `config-reset` y la API (con el Hub caído sus resultados se entregan al volver, por 3a). Un `config-reset` vuelve al TOML y el Hub puede reenviar.
9. Los wheels en venv limpio ejecutan `dyndnsarr-agent status` y `dyndnsarr-hub agent-list` (migración 0005/0003 y módulos nuevos viajan).
10. La CI de GitHub pasa en el PR.
11. **Manual del usuario:** CA local y certificado del Agent en la Raspberry (receta de operaciones), token de API, `config-push` de un cambio real (p. ej. `check_interval_seconds`), `command sync_ddns` y `check_dns`, `agent-status`, y `config-reset`.

## Pruebas

- `tests/unit`: contrato y `config_hash`, `apply_config` (tabla), ajustes locales, migraciones 0005 y 0003, repositorios, códigos del cliente del Hub.
- `tests/integration`: API interna del Agent con `ASGITransport`, ejecutor de comandos (reloj y proveedor inyectados), configuración activa y scheduler, CLI de ambos, Hub ↔ Agent en proceso, TLS con CA de `trustme`.
- `tests/contract`: forma de config, comandos, health, status y eventos nuevos según §5; `/healthz` de ambos sin cambios.
- `tests/e2e` (marca `e2e`): Agent y Hub como procesos reales con TLS.
- Sin dependencias nuevas (`trustme` llega con 3a). Sin servicios reales en la suite. Ejecución: `pytest` / `pytest -m "not e2e"`.

## Documentación a actualizar

`docs/operations.md`, `.env.example`, `apps/hub/compose.yaml`, `apps/agent/systemd/dyndnsarr-agent.service` (comentario), `docs/conventions.md`, `docs/decisions/0006-*.md` y su índice, `ESTADO.md`, `plan.md`, `docs/slices/README.md`, `scripts/verify-wheels.sh`.

## Riesgos y migraciones

- **El Hub guarda el token de API en claro** (D2): quien lea `hub.db` o su backup puede llamar a la API del Agent. Alcance acotado a esa API en la LAN, nunca DonDominio; se documenta proteger `/data` y sus copias, y rotar con `agent-api-token`.
- **Un Hub comprometido puede reconfigurar el monitoreo** y añadir o habilitar registros `managed` (el Agent los actualizaría con su IP, solo si el registro A existe en la misma cuenta). No puede cambiar el proveedor, su URL ni leer credenciales (D4).
- **Reloj:** `expires_at` compara el reloj del Hub con el del Agent; sin NTP un comando puede nacer expirado. Se documenta.
- **Sin reconciliación automática:** si el Agent está caído al enviar una versión, la versión queda pendiente hasta `config-resend`; el automatismo llega con la UI (slice 5).
- **Certificado del Agent:** caduca y no hay aviso; la receta fija una vigencia y se documenta la renovación. Un certificado caducado corta solo el enlace, no el DDNS.
- **`/healthz` pasa a HTTPS** cuando hay TLS: las comprobaciones locales (`curl`, operaciones) necesitan `--cacert`. `verify-wheels.sh` arranca el Agent sin TLS y no cambia.
- **Toca código de 2b:** `run_forever` lee del holder (ajuste mecánico en los tests del scheduler) y el pase de `sync-ddns` se extrae a `ddns/manual.py`; la suite existente debe seguir verde sin otros cambios.
- **`sync_ddns` por API y `ddns.lock`:** si un ciclo lo tiene tomado el comando termina `busy`; el Hub reintenta con otro `command_id`.
- **`config-reset` borra el historial de versiones** del Agent; queda constancia por el evento `config_reset` y el Hub conserva las suyas.
- **Migraciones:** Agent 0005 y Hub 0003 crean tablas nuevas, sin reconstruir las existentes; no hay vuelta atrás automática (respaldar `agent.db`, operaciones §4).

## Evidencia

Ejecutado el 2026-10-19 en la rama `slice/03b-control-hub-agent`.

| Criterio | Evidencia |
|---|---|
| 1 | `ruff check .`, `ruff format --check .` y `pytest` (674 pruebas, 26 de ellas e2e): verde en Python 3.12.3 (`.venv`) y 3.11.17 (venv limpio creado desde los wheels, no editable). |
| 2 | `tests/e2e/test_hub_control.py::test_config_flow_…` (procesos reales, Agent con TLS de `trustme`): config inválida → `rechazada (invalid_config)`, `status` sigue en `TOML local`, el daemon sigue ciclando igual y no consulta el registro nuevo; el Hub la ve rechazada por la respuesta (`config-show`) y por el evento `config_rejected`. Config válida → `aplicada`, `Configuración: versión 3`, el siguiente ciclo del daemon consulta los registros nuevos y llega `config_applied`. En proceso: `tests/integration/test_hub_agent_control.py`. |
| 3 | Mismo e2e (misma versión → `aplicada` sin fila nueva; versión 1 con la 3 activa → `stale_version`; hash incorrecto → `hash_mismatch`; versión 3 con otro contenido → `version_conflict`) y `tests/integration/test_apply_config.py` (una prueba por fila de la tabla de D5). |
| 4 | `test_apply_config.py::test_a_failure_before_commit_…`: fallo inyectado en el evento tras insertar la versión → ni fila nueva, ni evento, ni cambio en el holder; `load_active` sigue en 0. `test_the_running_cycle_keeps_its_configuration`: aplicar a mitad de un ciclo no lo afecta. |
| 5 | `test_commands.py` (ejecutor con proveedor falso contado): mismo `command_id` dos veces → una ejecución y el mismo resultado; otro tipo → 409; caducado → `expired` sin ejecutar; `wake_device` y parámetros → 422 sin guardar; cola llena → 429; ejecución en serie; `busy` con `ddns.lock` tomado; tras reinicio `received`/`running` → `failed/interrupted` sin reejecutar. En proceso (`test_hub_agent_control.py`): una sola llamada a DonDominio con un `sync_ddns` duplicado. E2E: comando duplicado → una ejecución, caducado y `wake_device` no se ejecutan, y un comando `running` dejado por un «crash» queda `interrupted` y reenviar su id no lo reejecuta. |
| 6 | `test_internal_api.py` y el e2e: sin cabecera, con token malo, con esquema distinto de Bearer → el mismo 401; el 401 va antes de leer el cuerpo; sin token no existe `/internal` (404); ningún `GET` modifica tablas; cuerpo > 256 KiB → 413 con y sin `Content-Length`. `test_agent_tls.py`: CA correcta conecta, sin CA o con otra falla el handshake, HTTP plano al puerto TLS no responde. `test_agent_control_settings.py` y e2e: token con `HOST=0.0.0.0` sin TLS → el Agent no arranca (mensaje `hace falta TLS`, sin el token). Secciones locales (`providers`, `hub`) → `forbidden_section` (API, CLI y e2e). |
| 7 | Canarios: `test_internal_api.py` (token de API, token del Hub, clave de DonDominio y URL del Hub fuera de respuestas y logs, con las rutas de error incluidas), `test_commands.py` (texto de una excepción fuera de resultado, evento y log), `test_hub_control_cli.py` (token de la API fuera de toda salida del Hub; solo se imprime al emitirlo), `tests/unit/test_hub_control_repo.py` (el token no sale en el listado) y `test_hub_agent_client.py` (repr de la respuesta) y el e2e final (logs de ambos procesos y consolas sin token de API, token del Hub, clave y usuario de DonDominio ni su URL). |
| 8 | E2E `test_the_agent_works_without_the_hub_…`: con el Hub caído el Agent arranca y cicla, `config-reset` + reinicio → `Configuración: TOML local`, al volver el Hub llega `config_reset`, el Hub ve versión 0 y `config-resend` lo vuelve a aplicar. En proceso: resultado de un comando con el Hub caído llega por evento al volver, una sola vez. |
| 9 | `PYTHON=python3.11 scripts/verify-wheels.sh` en venv limpio: `/healthz` de ambos, `dyndnsarr-agent status` (lee `configuration_versions`, 0005), importa `commands`, `api.internal`, `apply_config`, `services.agent_client` y `services.events_handlers`, y `dyndnsarr-hub agent-list` (lee `api_url`, 0003). |
| 10 | Se anota tras abrir el PR. |
| 11 | **Pendiente del usuario** ([operaciones §7.5](../operations.md)). La receta de `openssl` de §7.1 sí se ejecutó aquí (CA → certificado con IP en el SAN → `openssl verify` → OK; y el módulo `ssl` de Python rechaza una IP que no está en el SAN). |

Desviaciones respecto al plan (además de las de «Ajustes tras 3a»):

- `ConfigHolder.replace` reconstruye proveedores y observador **antes** de asignar nada: si fallara, el holder queda como estaba.
- `AgentHealth.agent_id`/`AgentStatus.agent_id` son `None` si el TOML no tiene `[hub]` (el Agent solo conoce su identidad por esa sección).
- Un `check_dns` sin IP pública confirmada devuelve el veredicto `no_reference` (la CLI sigue imprimiendo «sin referencia»). Los resultados de comando que superan 3000 bytes viajan resumidos en el evento (el Hub rechaza payloads > 4 KiB); el resultado completo queda en `commands`.
- Se añadió `command-show --refresh` (pregunta al Agent con `GET commands/{id}`); un 404 **no** marca el comando como fallido (el `POST` pudo quedar en vuelo), solo un rechazo explícito del `POST` lo hace. Lo encontró una prueba de la CLI.
- `agent-list` del Hub muestra la URL de la API (no el token). El Hub necesita `httpx` en tiempo de ejecución (se añadió a sus dependencias).
- El outbox entrega cada 30 s (3a): las pruebas e2e que esperan eventos tardan hasta ~60–90 s (`test_hub_control.py` ≈ 87 s).
- `scripts/verify-compose.sh` ahora comprueba también `agent-set-api` y `agent-api-token` dentro de la imagen. **No se ejecutó en local** (requiere Docker, compartido con otros servicios del servidor, y los comandos que lo afectan los autoriza el usuario): lo ejecuta la CI (`hub-image`).
