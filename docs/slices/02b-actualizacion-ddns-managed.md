# Slice 2b — Actualización DDNS `managed` (DonDominio) (motor DDNS, parte 2)

- **Estado:** en revisión
- **Fase (arquitectura):** 1, segunda mitad ([architecture.md](../architecture.md) §7 pasos 4–6, §13 casos 1, 2 y 4)
- **Rama:** `slice/02b-actualizacion-ddns-managed`
- **PR:** [#3](https://github.com/oxxido/dynDNSarr/pull/3) (abierto contra `main`, sin fusionar)

## Objetivo

El Agent, solo (sin Hub), mantiene al día los registros `managed`: cuando la IP pública **confirmada** (2a) difiere de lo que resuelven los resolvers externos, hace **una** llamada a DonDominio con esa IP, distingue «el proveedor aceptó» de «el DNS ya resuelve bien» (`pending_verification` → `verified`), reintenta con backoff si el proveedor falla y se opera por consola con `sync-ddns`. Los registros `monitored` siguen sin poder escribirse, por construcción.

## Alcance

**Incluye**

- `mode = "managed"` aceptado en la config, con credenciales DonDominio por variables de entorno (nunca en el TOML).
- Adaptador DonDominio (`GET https://dondns.dondominio.com/json/`) tras un `Protocol`, con errores convertidos a códigos seguros.
- Reconciliador `decide` (función pura): llamar o no llamar, y estado resultante.
- Estado `pending_verification`, plazo de verificación (`dns_verification_failed`), backoff persistido que sobrevive a reinicios y errores definitivos (`retryable=false`).
- Migración 0003: estado de actualización por registro y tabla de intentos (auditoría).
- El scheduler despierta antes del intervalo cuando hay una verificación o un reintento vencidos.
- CLI `dyndnsarr-agent sync-ddns` con `flock`; `status` muestra el estado de actualización.
- Documentación de configuración y operación.

**No incluye**

- Hub, TLS/tokens, entrega de eventos (slice3). IPv6, otros proveedores, crear registros en la zona (el registro A debe existir ya).
- Estados `healthy/degraded/critical` y uptime (slice5). Pi-hole, WoL, alertas.
- `sync-ddns --wait` o cualquier espera activa de la verificación: `sync-ddns` hace un pase y sale; la verificación la completa el daemon (o `check-dns`).
- Recuperación fina de un `kill -9` justo entre la llamada y su registro en SQLite (ver Riesgos).

## Decisiones previas

Resuelven A1 y completan lo anotado en [plan.md](../../plan.md) («Insumos para slice2b»). Dejarán una ADR-0004 con D1–D4.

| # | Decisión | Propuesta | Alternativa descartada |
|---|---|---|---|
| D1 | **A1 — API DonDominio** | [Documentación oficial](https://dondominio.dev/en/dondns/docs/api/) (releída al planificar): `GET https://dondns.dondominio.com/json/` con `user`, `host`, `ip`, `lang=en` y la clave en **`password`**. Éxito = HTTP 200, JSON con `success: true` **estricto** y `host.value` igual a la IP enviada (`host.updated` dice si cambió). Todo lo demás es fallo. La documentación no da errores ni límites; se sondearon el 2026-10-15 (ver «Hallazgos del sondeo real»). La clave va en `password` (confirmado) | `ip` omitida (DonDominio usaría la IP de quien llama: la decisión debe tomarse antes, con la IP ya confirmada); XML/plain; parámetro `apikey` |
| D2 | Reconciliador | `decide(record, public_ip, answers, state, now, manual) -> Decision` en `ddns/reconcile.py`, sin E/S. Tabla abajo. Una llamada como máximo por registro y ciclo | Decidir dentro de `cycle.py` (mezcla reglas y E/S) |
| D3 | Cadencia | `run_cycle` devuelve cuántos segundos faltan hasta el próximo vencimiento (verificación o reintento); el scheduler duerme `min(check_interval, ese valor)`. Así `verify_retry_seconds = 60` funciona con un intervalo de 300 | Un segundo bucle o temporizador; subir la frecuencia global |
| D4 | Credenciales y logs | `DYNDNSARR_AGENT_DONDOMINIO_USER` / `_KEY` (`SecretStr`, `EnvironmentFile` 0600). Un registro `managed` habilitado sin ellas aborta el arranque con mensaje claro. La clave viaja en la URL (la API es GET): `httpx`/`httpcore` ya en `WARNING` (2a D6); el adaptador nunca propaga `str(exc)` ni la URL; test con valor canario en logs, eventos, BD y salida del CLI | Pasar la clave por cabecera (la API no lo admite); guardarla en el TOML |
| D5 | Persistencia de la actualización | `record_state` se **reconstruye** (no basta `ALTER`: el `CHECK` de `verification` no se puede ampliar) con `pending_verification`, más `accepted_ip`, `accepted_at`, `failures`, `failure_ip`, `next_attempt_at`, `last_error_code`. Tabla nueva `dns_update_attempts` (auditoría, §6) con la retención de eventos | Estado de actualización en `app_state` clave a clave |
| D6 | Constantes de backoff | 60 s inicial, ×2, tope 3600 s; fijos en código (no configurables). Configurables en `[update]`: `verify_retry_seconds` (60, ≥1) y `verify_timeout_seconds` (900, ≥ el anterior). Un mínimo de 1 s permite que el e2e verifique en segundos | Todo configurable sin consumidor |
| D7 | `sync-ddns` | Un pase manual de los registros `managed`: observa IP y DNS y decide como el ciclo, pero **ignora la espera de backoff y el bloqueo por error definitivo**, no la deduplicación (si ya está aceptada y en plazo, no llama). `flock` no bloqueante sobre `<data_dir>/ddns.lock`; el daemon lo toma en cada ciclo y, si lo encuentra tomado, salta ese ciclo. Sale con 1 si no pudo tomar el bloqueo, si la IP es incierta o si el proveedor falla | Bloqueo bloqueante (congela el bucle asyncio); `sync-ddns` que escribe sin coordinarse con el daemon |

### Tabla de `decide` (registro `managed`, IP del ciclo)

Clasificación de las respuestas de los resolvers (que responden = con registros A): **`target`** todos = IP; **`stale`** todos coinciden en otro valor; **`split`** discrepan entre sí; **`none`** ninguno responde.

| Situación | Llamada | Estado resultante / eventos |
|---|---|---|
| IP `uncertain` en el ciclo | no | no se reclasifica (como 2a) |
| `target` | no | `verified`; `dns_verified` solo si cambia el estado (incluye `pending_verification` → `verified`); se limpia aceptación y fallos |
| `none` | no | sigue `pending_verification` si lo estaba; si no, `unknown` |
| `split` | no | `pending_verification` si ya estaba aceptada esa IP; si no, `mismatch` |
| `stale` y aceptada esa IP, dentro de plazo | no | `pending_verification` |
| `stale` y aceptada esa IP, plazo vencido | permitida (si no hay espera) | `dns_verification_failed` una vez; se descarta la aceptación |
| `stale`, hay espera de backoff (`next_attempt_at` futuro) | no | `mismatch` |
| `stale`, error definitivo previo para esta IP | no | `mismatch`; solo `sync-ddns` o un cambio de IP lo desbloquean |
| `stale`, sin nada de lo anterior | **sí** | éxito → `pending_verification`, `dns_update_succeeded`; fallo → `mismatch`, backoff o bloqueo, `dns_update_failed` |

Eventos de fallo solo al **empezar una racha** o al **cambiar el código**, no por cada reintento (§7.7). `dns_mismatch` sigue emitiéndose al entrar en `mismatch`. Tipos nuevos: `dns_update_succeeded` (payload `ip`, `changed`), `dns_update_failed` (`code`, `retryable`, `attempt`), `dns_verification_failed` (`ip`, `waited_seconds`). Ningún payload lleva credenciales ni URL.

## Hallazgos del sondeo real (2026-10-15)

Llamadas reales con las credenciales del usuario (`lang=en`, clave en `password`). Son los fixtures del paso 5, con usuario, clave, host e IP sustituidos.

| Caso | HTTP | Cuerpo | Código seguro |
|---|---|---|---|
| Clave incorrecta | 200 | `{"success":false,"messages":["Wrong key"],"user":""}` | `auth_failed`, definitivo |
| Usuario incorrecto | 200 | `{"success":false,"messages":["Incorrect data"],"user":""}` | `auth_failed`, definitivo |
| Nombre sin registro A en la zona | 200 | `{"success":false,"messages":["The DNS Zone doesn't exist"],"user":"…"}` | `host_not_found`, definitivo |
| Dominio ajeno | 200 | `{"success":false,"messages":["Domain not found"],"user":"…"}` | `host_not_found`, definitivo |
| Éxito, IP distinta | 200 | `{"success":true,"messages":[],"user":"…","host":{"name":"…","value":"<IP nueva>","type":"A","updated":true}}` | aceptado, `changed=true` |
| Éxito, misma IP | 200 | `{"success":true,"messages":[],"user":"…","host":{"name":"…","value":"<IP>","type":"A","updated":false}}` | aceptado, `changed=false` |
| Demasiadas llamadas | **429** | **HTML** (`<h1>429 Too Many Requests</h1>`), no JSON | `rate_limited`, reintentable |
| `ip=no-es-una-ip` | 200 | **`success: true`**, `updated: false` y el valor actual del registro | (ver abajo) |

Consecuencias de diseño:

- **Los errores llegan con HTTP 200**: se decide por `success`, no por el status. Un 429 u otro status ≠ 200 se trata antes de intentar leer JSON.
- **El API no valida `ip`**: ante una IP inválida responde éxito con el valor actual. El adaptador solo envía IPv4 global ya confirmada y exige `host.value == ip` enviada; si no coincide, `bad_response` reintentable.
- **Clasificación por texto exacto del mensaje** (con `lang=en` fijo). Un `success:false` con un texto desconocido es `rejected` y **reintentable** (regla conservadora: solo se bloquea lo visto en fixtures reales).
- **Límite de tasa**: 5 llamadas en ~2 s pasaron y la 6.ª recibió 429; 90 s después volvió a responder. Máximo una llamada por registro y ciclo, pausa de 2 s entre llamadas del mismo ciclo y backoff ≥ 60 s.
- **Un CNAME no es un registro A**: `sub.example.com` es CNAME de `example.com` y DonDominio respondió `The DNS Zone doesn't exist`. Un registro `managed` debe ser un A propio (ver Riesgos).

**Registro de prueba:** los casos de éxito se capturaron cambiando un registro A real a `203.0.113.10` (`updated: true`), repitiéndolo (`updated: false`) y restaurando el valor original (`updated: true`). En éxito `messages` viene vacío (la documentación sugiere un texto, no se debe depender de él) y no se midió el tiempo de propagación hasta los resolvers (se comprobará en la prueba manual, criterio 11); por eso `verify_timeout_seconds = 900` es un valor prudente, no medido.

## Diseño mínimo

**Configuración (`agent.example.toml`)**

```toml
[update]                          # opcional
verify_retry_seconds = 60
verify_timeout_seconds = 900

[providers.dondominio]            # opcional; solo para pruebas apuntar a otra URL
url = "https://dondns.dondominio.com/json/"   # https, o http solo a localhost/127.0.0.1

[[records]]
name = "home.example.com"
mode = "managed"                  # ahora válido; credenciales por entorno
provider = "dondominio"           # obligatorio en `managed`; no aplica a `monitored`
```

**Módulos** (§4: HTTP, reglas y SQLite separados)

- `providers/base.py`: `Protocol` `DnsProvider.update(host, ip) -> UpdateResult(accepted, changed, error_code, retryable)`.
- `providers/dondominio.py`: implementación `httpx` de D1; mapeo a códigos seguros según la tabla de hallazgos (`auth_failed`, `host_not_found`, `rate_limited`, `rejected`, `http_error`, `timeout`, `connect_error`, `bad_response`).
- `ddns/reconcile.py`: `decide` (D2) y cálculo de backoff.
- `ddns/cycle.py`: se extiende — observar → `decide` → llamadas → una transacción con estado, intentos y eventos. `monitored` pasa por la rama de 2a, que **no recibe el proveedor**.
- `db/migrations/0003_managed_updates.sql` y `db/ddns_repo.py` (estado de actualización, intentos, purga).
- `scheduler.py`: `flock` y espera `min(...)` (D3, D7). `cli.py`: `sync-ddns`, `status` ampliado.
- `ddns/config.py`: `Record.mode` admite `managed`; `Record.provider` (solo `dondominio`, obligatorio en `managed`); secciones `[update]` y `[providers.dondominio]`. `config.py`: credenciales.
- `apps/agent/pyproject.toml`: añadir `dyndnsarr_agent.providers` a `packages` (sin esto el wheel no lo lleva).

**Flujo del ciclo**: se observa la red primero (IP y DNS, como 2a) → `decide` por registro leyendo su estado → llamadas secuenciales al proveedor (timeout 10 s, una por registro) → **una** transacción que escribe muestras, estado, intentos y eventos. Un fallo del proveedor es un resultado (`UpdateResult`), no una excepción que aborte el ciclo.

## Plan de implementación

Commits pequeños, tests antes del código. Cada paso termina con `ruff` y `pytest -m "not e2e"` en verde.

1. **ADR-0004** (D1–D4, D6) y su entrada en el índice de decisiones. *Verifica:* enlaces válidos.
2. **Configuración.** `managed` válido; `[update]`, `[providers.dondominio]` y `provider` con las reglas de D6 y D1 (`managed` sin `provider` o con otro valor falla); credenciales en `Settings` como `SecretStr`; `managed` habilitado sin credenciales aborta. *Verifica:* el ejemplo carga; `verify_timeout < verify_retry`, `http://` no loopback y credenciales ausentes dan error claro; el valor de la clave nunca aparece en el mensaje de error de `Settings`.
3. **Migración 0003 y repositorio.** *Verifica:* aplica sobre una BD 0002 **con datos** y conserva `record_state`; acepta `pending_verification`; el `CHECK` sigue rechazando otros valores; estado de actualización y backoff persisten entre conexiones; purga de `dns_update_attempts`.
4. **Reconciliador `decide`.** Puro, tabla de pruebas con una fila por línea de la tabla D2. *Verifica:* §13-1 (sin cambio, sin llamada), §13-3 y §13-4; backoff 60→120→…→3600; `manual` ignora espera y bloqueo pero no la aceptación en plazo.
5. **Adaptador DonDominio.** Con `httpx.MockTransport` y los **fixtures reales** de «Hallazgos del sondeo real». *Verifica:* éxito con y sin `updated`; cada mensaje → su código seguro y `retryable`; mensaje desconocido → `rejected` reintentable; 429 con cuerpo HTML (sin intentar leer JSON), otros status, timeout, JSON ilegible y `host.value` ≠ IP → fallo; la petición lleva `ip` explícito y `lang=en`; **canario**: la clave no aparece en logs, excepciones, eventos ni BD.
6. **Ciclo.** Extiende `run_cycle`. *Verifica:* §13-2 (proveedor falla → backoff y se recupera, sin llamadas durante la espera); §13-4 (aceptó pero DNS viejo → `pending_verification`, **una sola llamada** en varios ciclos); plazo vencido → `dns_verification_failed` y reintento; error definitivo → sin reintentos hasta cambio de IP; IP incierta → sin llamada; `monitored` con un proveedor que falla si se invoca; dos registros `managed` con uno fallando no afecta al otro y con 2 s de pausa entre llamadas (reloj inyectado); ciclo tras reinicio respeta `next_attempt_at`.
7. **Scheduler y bloqueo.** Espera `min(...)`, `flock`. *Verifica:* con `pending_verification` el siguiente ciclo ocurre en `verify_retry_seconds`; un ciclo con el bloqueo tomado se salta sin tumbar la tarea; el bloqueo se libera al cancelar.
8. **CLI.** `sync-ddns`; `status` muestra aceptación, espera y último error. *Verifica:* ignora backoff pero no la deduplicación; código 1 con bloqueo tomado, IP incierta o proveedor caído; `status` sigue sin escribir.
9. **E2E.** Agent real + `FakeIpSource` + `FakeDns` mutable + **`FakeDonDominio`** en loopback (nuevo en `tests/conftest.py`) que, al recibir la llamada, cambia lo que responde el DNS falso. Con `verify_retry_seconds = 1`. *Verifica:* criterios 2–4 y 6.
10. **Documentación y despliegue.** `agent.example.toml`, `.env.example` (variables de DonDominio vacías), `docs/operations.md` §5 (credenciales, estados, `sync-ddns` y cómo pasar las variables al CLI con `sudo -u`), `ESTADO.md`, `docs/conventions.md` («Fijado en slice2b»), `plan.md` (retirar «Insumos»), índice; `scripts/verify-wheels.sh` debe importar `dyndnsarr_agent.providers`.

## Criterios de aceptación

1. En un venv limpio con Python 3.11 y 3.12, `ruff check`, `ruff format --check` y `pytest` terminan con éxito.
2. E2E sin Hub: con DNS falso = A y las fuentes cambiando a B → **exactamente una** llamada a `FakeDonDominio` con `ip=B`; el registro pasa por `pending_verification` y termina `verified`; eventos `ip_changed`, `dns_update_succeeded`, `dns_verified`, uno de cada.
3. Sin cambio de IP no hay llamadas ni eventos nuevos; con la propagación pendiente durante varios ciclos sigue habiendo **una** llamada.
4. Proveedor caído (5xx/timeout) → `dns_update_failed` una vez, reintentos con backoff 60→120→… (tope 3600) sin llamadas intermedias; al recuperarse, actualiza y verifica. Tras reiniciar el Agent, la espera pendiente se respeta.
5. Error definitivo (credenciales/registro inexistente, según fixtures reales) → sin reintentos automáticos; `sync-ddns` sí reintenta.
6. `monitored` nunca invoca al proveedor (test con proveedor que falla si se llama); credenciales ausentes con un `managed` habilitado abortan el arranque con mensaje claro, sin ellas el motor `monitored`-only arranca igual.
7. Ni la clave ni la URL con la clave aparecen en logs, eventos, BD ni salida del CLI (test con canario).
8. `sync-ddns` funciona con el daemon parado y no coincide con un ciclo en curso (`flock`); `status` no escribe.
9. Los wheels en venv limpio ejecutan `dyndnsarr-agent status` (migración 0003 y `providers` viajan en el paquete).
10. La CI de GitHub pasa en el PR.
11. **Manual del usuario:** actualizar un subdominio de prueba real, ver `pending_verification` → `verified` en `status` y confirmar con `dig` desde fuera.

## Pruebas

- `tests/unit`: config, `decide`, backoff, adaptador (`MockTransport` + fixtures), repositorio y migración.
- `tests/integration`: ciclo con proveedor y resolver falsos sobre BD temporal; reinicio (dos instancias sobre la misma BD); scheduler con reloj y `sleep` inyectados; CLI `sync-ddns`/`status`.
- `tests/contract`: forma de los eventos nuevos según §5; `/healthz` sin cambios.
- `tests/e2e` (marca `e2e`): Agent real + servidores falsos en loopback.
- Sin servicios reales en la suite automática. Ejecución: `pytest` / `pytest -m "not e2e"`.

## Documentación a actualizar

`docs/operations.md` §5, `.env.example`, `apps/agent/config/agent.example.toml`, `apps/agent/systemd/dyndnsarr-agent.service` (solo comentario: las credenciales van en `agent.env`), `ESTADO.md`, `docs/conventions.md`, `docs/decisions/0004-*.md` y su índice, `plan.md`, `docs/slices/README.md`, `scripts/verify-wheels.sh`.

## Riesgos y migraciones

- **Errores clasificados por texto** (D1): DonDominio no documenta códigos; si cambia un mensaje, pasa a `rejected` reintentable (cada hora como máximo), no a un bloqueo. El bloqueo por error definitivo se levanta con `sync-ddns`.
- **Límite de tasa real** (429 con HTML, ~5 llamadas en pocos segundos): máximo una llamada por registro y ciclo, 2 s de pausa entre llamadas, backoff y deduplicación por IP aceptada.
- **Crash entre la llamada y su registro** (la llamada va antes de la transacción): al reiniciar se repite una llamada con la misma IP. Es idempotente (DonDominio responde `updated: false`) y solo ocurre con un corte en esa ventana; se acepta en vez de añadir un intento «en vuelo» persistido.
- **Registro inexistente en la zona:** si ningún resolver responde (`none`) no se escribe; el registro queda `unknown`. El registro A debe crearse a mano antes.
- **CNAME en un registro `managed`:** los resolvers devuelven la IP del destino y `decide` ve `target` o `stale`; si es `stale`, DonDominio responde `The DNS Zone doesn't exist` → `host_not_found` definitivo, y el registro queda bloqueado. Se documenta en operaciones; no se detecta CNAME en este slice.
- **La clave en la URL** (D4): único canal que ofrece la API; por eso los controles de logs y el canario.
- **Migración 0003** reconstruye `record_state` en la transacción del runner; se prueba con datos de 2a. No hay vuelta atrás automática: respaldar `agent.db` antes de actualizar (docs/operations.md §4).
- Sin dependencias nuevas.

## Evidencia

Rama `slice/02b-actualizacion-ddns-managed`. Verificado el 2026-10-17 con Python 3.11.17 y 3.12.3 en venvs limpios:

| Criterio | Resultado |
|---|---|
| 1. venv limpio 3.11 y 3.12: `ruff check`, `ruff format --check`, `pytest` | Verificado con 3.11.17 y 3.12.3: 342 tests (incluidos 18 e2e), 0 fallos en ambos. |
| 2. E2E: DNS = A, fuentes a B → una llamada con `ip=B`, `pending_verification` → `verified`, un evento de cada | Verificado: `tests/e2e/test_ddns_engine.py::test_ip_change_is_one_update_then_pending_then_verified` (Agent real, `FakeIpSource`, `FakeDns` y `FakeDonDominio` en loopback). El cambio de IP se observa al relanzar el Agent (intervalo mínimo 60 s); los ciclos periódicos, en `tests/integration/test_scheduler.py`. |
| 3. Sin cambio no hay llamadas ni eventos; con propagación pendiente varios ciclos, una llamada | Verificado: e2e `test_no_ip_change_means_no_calls_and_no_new_events` y el e2e anterior (≥3 ciclos pendientes, `len(calls) == 1`); `tests/integration/test_cycle_managed.py`. |
| 4. Proveedor caído → `dns_update_failed` una vez, backoff 60→120→… (tope 3600) sin llamadas intermedias, recuperación; la espera sobrevive al reinicio | Verificado en `test_cycle_managed.py` (`test_provider_failure_backs_off_…`, `test_backoff_never_exceeds_an_hour`, `test_the_backoff_survives_a_restart`) con reloj inyectado. En e2e (`test_provider_down_fails_once_…`) se comprueba el primer fallo, la espera tras reiniciar el proceso y la recuperación con `sync-ddns`; las esperas de 60 s en adelante no se esperan en tiempo real. |
| 5. Error definitivo → sin reintentos automáticos; `sync-ddns` sí reintenta | Verificado: e2e `test_definitive_error_is_not_retried_until_sync_ddns`, `test_cycle_managed.py::test_definitive_error_stops_automatic_retries_until_the_ip_changes` y fixtures reales en `tests/unit/test_dondominio.py`. |
| 6. `monitored` nunca invoca al proveedor; sin credenciales un `managed` aborta, el motor `monitored`-only arranca igual | Verificado: `test_monitored_records_never_reach_the_provider` (proveedor que falla si se llama), `test_main_aborts_on_managed_record_without_credentials`, e2e `test_managed_record_without_credentials_aborts_startup`; los e2e `monitored` del slice 2a siguen en verde sin credenciales. |
| 7. Ni la clave ni la URL con la clave en logs, eventos, BD ni salida del CLI | Verificado con valor canario: `tests/unit/test_dondominio.py` (con el `setup_logging` real), `tests/integration/test_cli.py::test_the_key_never_shows_…` y e2e `test_the_key_does_not_leak_anywhere` (stderr del daemon, salida del CLI y volcado completo de la BD). Una prueba documenta que sin `setup_logging` el INFO de `httpx` sí imprimiría la clave. |
| 8. `sync-ddns` con el daemon parado y sin coincidir con un ciclo (`flock`); `status` no escribe | Verificado: e2e `test_sync_ddns_does_not_overlap_with_a_running_cycle` y `test_provider_down_…` (daemon parado), `test_cli.py::test_sync_ddns_with_the_lock_taken_…`, `test_status_still_does_not_write_with_managed_records`; `test_scheduler.py` (ciclo saltado con el bloqueo tomado, bloqueo liberado al cancelar). |
| 9. Wheels en venv limpio ejecutan `status` (migración 0003 y `providers` viajan) | Verificado con 3.11 y 3.12 (`scripts/verify-wheels.sh`, que ahora importa `dyndnsarr_agent.providers`). |
| 10. CI de GitHub en el PR | **Parcial.** `test (3.11)` y `test (3.12)` pasan en el PR #3. `hub-image` falla (`FALLO: boot_count 1 -> 3 (se esperaba +1)` en `scripts/verify-compose.sh`), igual que en los merges de #1 y #2 en `main`: es previo a este slice, que no toca el Hub, y queda sin corregir aquí. |
| 11. Manual del usuario: subdominio de prueba real, `pending_verification` → `verified`, `dig` desde fuera | **Pendiente del usuario** (docs/operations.md §5, «Comprobación manual»). Anota el tiempo de propagación observado para ajustar `verify_timeout_seconds`. |

Desviaciones respecto al diseño del plan (menores):

- `decide(public_ip, answers, state, now, verify_timeout_seconds, manual)`: se quitó `record` (el estado ya lleva el nombre) y se añadió el plazo, que la firma del plan no pasaba. Devuelve `Decision(call, state, verification_timed_out_after)`; `after_update` y `starts_failure_streak` completan el resultado de la llamada.
- Un error definitivo se codifica con `failures > 0` y `next_attempt_at` nulo, sin columna extra (el esquema de D5 no cambia). Una espera o un bloqueo solo valen para la IP con la que ocurrió el fallo (`failure_ip`): una IP nueva se actualiza sin esperar.
- `CycleResult` añade `updates` (resultado de cada llamada) y `next_wake_seconds`; `providers/registry.py` construye los proveedores y `Settings.lock_path` da la ruta de `ddns.lock`.
- Un vencimiento de reintento ya pasado no despierta al scheduler (evita ciclos en bucle si no hay nada que reintentar, p. ej. con IP incierta).
- `sync-ddns` filtra la configuración a los registros `managed` habilitados (los `monitored` no se observan en ese pase).
- Nada de esto se probó contra el DonDominio real en la suite: los fixtures son los del sondeo del 2026-10-15. La receta con `runuser` de operaciones no se probó en la Raspberry.
