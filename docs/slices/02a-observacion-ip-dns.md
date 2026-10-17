# Slice 2a — Observación de IP y DNS (motor DDNS, parte 1)

- **Estado:** fusionado
- **Fase (arquitectura):** 1, primera mitad ([architecture.md](../architecture.md) §7 pasos 1–3 y 7, §13)
- **Rama:** `slice/02a-observacion-ip-dns`
- **PR:** #2 (fusionado en `main`, commit `8b3d3a0`)

## Objetivo

El Agent, solo (sin Hub), detecta su IPv4 pública con dos fuentes independientes, observa qué resuelven resolvers externos para los registros configurados, guarda muestras y eventos en SQLite y se opera por consola. **No escribe DNS**: la actualización de DonDominio es el slice 2b. Al terminar, el Agent responde con evidencia a «¿cuál es mi IP, cambió, y los registros apuntan a ella?».

La parte 2b (adaptador DonDominio, registros `managed`, reintentos, verificación posterior) está en [plan.md](../../plan.md).

## Alcance

**Incluye**

- Archivo `agent.toml` (registros, fuentes de IP, resolvers, intervalo, retención).
- Detección IPv4 con ≥2 fuentes HTTPS y regla de consenso (contradicción = estado incierto).
- Observación DNS por resolver externo (`dnspython`) y comparación con la IP pública confirmada para registros `monitored`.
- Scheduler asyncio en el `lifespan`, comprobación inmediata al arrancar, recuperación tras reinicio.
- Persistencia (migración 0002): observaciones de IP, comprobaciones DNS, estado por registro y eventos (`event_id` + `sequence`). Retención configurable con purga.
- CLI en el mismo ejecutable: `dyndnsarr-agent status | check-ip | check-dns` (sin subcomando = servir, como ahora).
- Documentación de configuración y operación.

**No incluye**

- Cualquier escritura DNS, adaptador DonDominio, credenciales, reintentos/backoff, `pending_verification`, `sync-ddns` (todo 2b). La config **rechaza** `mode = "managed"` con un mensaje que apunta a 2b.
- Hub, TLS/tokens, entrega de eventos (slice3). IPv6, otros proveedores.
- Estados `healthy/degraded/critical` y uptime (slice5).
- Pi-hole como resolver «configurado», WoL, alertas, API interna, `doctor`.

## Decisiones previas

Resuelven A3 y parte de A4 de [architecture.md](../architecture.md) §14.

| # | Decisión | Propuesta | Alternativa descartada |
|---|---|---|---|
| D1 | Configuración del Agent | TOML (`tomllib`, stdlib 3.11) apuntado por `DYNDNSARR_AGENT_CONFIG_FILE`; **sin secretos**. Sin la variable el motor queda desactivado (comportamiento del slice1); con ella, archivo ausente o inválido aborta el arranque. Amplía D4 del slice1 (solo env) porque la lista de registros no cabe en variables planas. En slice3 el Hub la sustituye por configuración versionada | Variables de entorno planas; YAML |
| D2 | **A3 — Scheduler y SQLite** | Bucle `asyncio` en una tarea del `lifespan`; sin APScheduler. `sqlite3` síncrono en el hilo del bucle (sentencias cortas, un solo worker). `httpx` para HTTP y `dnspython` para preguntar a resolvers concretos (la stdlib no tiene cliente DNS); ambas son Python puro | APScheduler/Celery; `aiosqlite`; cliente DNS propio |
| D3 | Consenso de IP | «Confirmada» solo si responden ≥2 fuentes, todas las que responden coinciden y la IP es IPv4 global. Cualquier otro caso = `uncertain`: no hay cambio de estado de IP y se reintenta en el siguiente ciclo. Motivo: en 2b esa IP será la que se escriba en DNS, y una IP equivocada deja el dominio apuntando a la nada | Mayoría con 3 fuentes; una sola fuente |
| D4 | `monitored` | Se compara con la IP pública confirmada. Una IP objetivo fija por registro queda fuera hasta que haga falta | `expected_ip` por registro |
| D5 | **A4 (parcial)** — defaults | Intervalo 300 s (mín. 60); muestras (IP y DNS) 30 días; eventos 365 días; purga al final de cada ciclo; todo configurable. Umbrales de estado y uptime: slice5 | Fijar umbrales sin consumidor |
| D6 | Seguridad HTTP/DNS | TLS siempre verificado; `http://` solo para `localhost`/`127.0.0.1` (pruebas). Fuentes y resolvers se validan al cargar; respuesta de fuente limitada a 64 bytes. Los loggers `httpx`/`httpcore` se fijan en `WARNING` (en 2b llevarán la URL con la clave) | Logs por defecto de `httpx` |
| D7 | CLI | `dyndnsarr-agent` sin argumentos sirve (compatible con systemd); subcomandos con `argparse`; se ejecutan como `dyndnsarr` (`sudo -u dyndnsarr …`) para leer la BD. `check-ip` y `check-dns` observan y guardan muestras; `status` solo lee. Sin `--json` | Comando separado `dyndnsarr-agentctl` |

Una ADR (`0003`) recogerá D1–D3 al ejecutar el slice.

## Diseño mínimo

**Configuración (`apps/agent/config/agent.example.toml`)**

```toml
check_interval_seconds = 300

[ip_sources]
urls = ["https://api.ipify.org", "https://ipv4.icanhazip.com"]   # mínimo 2

[dns]
resolvers = ["1.1.1.1", "8.8.8.8"]        # "ip" o "ip:puerto"

[retention]
samples_days = 30
events_days = 365

[[records]]
name = "home.example.com"
mode = "monitored"          # "managed" llega en 2b
enabled = true
```

**Módulos** (§4: HTTP, reglas y SQLite separados)

- `ddns/config.py`: modelos Pydantic del TOML y validación.
- `ddns/ip_sources.py`: consulta concurrente y `consensus(results)` pura.
- `dns/resolver.py`: `Protocol` `DnsObserver.lookup(name) -> list[ResolverAnswer]` con implementación `dnspython`.
- `ddns/cycle.py`: ciclo de observación (IP → por registro: DNS → clasificar → eventos).
- `db/ddns_repo.py`: repositorios y purga.
- `scheduler.py`: tarea del `lifespan`. `cli.py`: subcomandos.

**Datos (migración `0002_observation.sql`)**

- `public_ip_observations(id, observed_at, source, ip, error_code)`
- `dns_checks(id, observed_at, record_name, resolver, ip, error_code)`
- `record_state(record_name PK, verification, since)`; `verification` ∈ `verified|mismatch|unknown`. 2b lo ampliará con `ALTER TABLE`.
- `events(event_id PK, sequence INTEGER UNIQUE autoincrement, type, occurred_at, record_name, payload)`; tipos en 2a: `agent_started`, `ip_changed`, `ip_uncertain`, `dns_verified`, `dns_mismatch`. Sin columnas de entrega (slice3).
- `app_state`: `public_ip`, `public_ip_confirmed_at`.
- Eventos solo en **cambios de estado**, no uno por comprobación (§7.7).

**Clasificación de un registro** (con IP pública confirmada en el ciclo): todos los resolvers que responden = IP → `verified`; alguno responde otra cosa o discrepan entre sí → `mismatch`; ninguno responde (o NXDOMAIN/timeout) → `unknown`. Si la IP es `uncertain` en ese ciclo se guardan las comprobaciones pero no se reclasifica.

**Arranque**: el `lifespan` carga y valida la config, migra, emite `agent_started`, lanza la tarea y ejecuta un ciclo inmediato. Un ciclo fallido se registra con código seguro y no mata la tarea; `/healthz` mantiene su contrato.

## Plan de implementación

Commits pequeños, tests antes del código. Cada paso termina con `ruff` y `pytest -m "not e2e"` en verde.

1. **ADR-0003 y dependencias.** Redactar la ADR; añadir `httpx` y `dnspython` (cotas de versión mayor) a `apps/agent/pyproject.toml` (`httpx` hoy solo está en el extra `dev`). *Verifica:* instalación en venv limpio con 3.11 y 3.12.
2. **Configuración.** `ddns/config.py`, `Settings.config_file`, validaciones de D6. *Verifica:* el ejemplo carga; <2 fuentes, `http://` no loopback, `managed` (mensaje hacia 2b), archivo ausente con variable definida → error claro; sin variable → motor desactivado.
3. **Migración 0002 y repositorios.** *Verifica:* aplica sobre la BD del slice1; `sequence` monótona; purga por antigüedad; estado persiste entre conexiones.
4. **Fuentes IP y consenso.** Con `httpx.MockTransport`. *Verifica:* §13-3 (discrepancia → `uncertain`), una sola fuente responde → `uncertain`, IP privada/basura/respuesta >64 B rechazadas, timeout por fuente.
5. **Resolver DNS.** Interfaz, implementación `dnspython` y doble de pruebas. *Verifica:* respuesta por resolver, timeout/NXDOMAIN como códigos seguros, `ip:puerto`.
6. **Ciclo de observación.** *Verifica:* §13-1 (sin cambio de IP no hay eventos nuevos), `ip_changed`, `ip_uncertain` solo al entrar, `dns_verified`/`dns_mismatch` solo al cambiar de estado, `unknown` sin respuesta.
7. **Scheduler y arranque.** *Verifica:* §13-5 (tras reiniciar, historial intacto y comprobación inmediata), un ciclo fallido no tumba el servidor, `/healthz` intacto, motor desactivado sin config, cancelación limpia al parar.
8. **CLI.** `status`, `check-ip`, `check-dns`; `serve` por defecto. *Verifica:* salida con servicios falsos, código de salida ≠ 0 en error, `status` no escribe.
9. **E2E.** Proceso real con servidores locales falsos (dos fuentes IP y un DNS UDP mínimo), sin Hub. *Verifica:* criterios 2–4.
10. **Documentación y despliegue.** `agent.example.toml`, `.env.example`, `docs/operations.md`, unidad systemd (`DYNDNSARR_AGENT_CONFIG_FILE`), `ESTADO.md`, `docs/conventions.md`, `plan.md`, índice; ampliar `scripts/verify-wheels.sh` para ejecutar `dyndnsarr-agent status` desde el wheel.

## Criterios de aceptación

1. En un venv limpio con Python 3.11 y 3.12, `ruff check`, `ruff format --check` y `pytest` terminan con éxito.
2. E2E sin Hub: con las dos fuentes devolviendo A y el DNS falso apuntando a A, el registro `monitored` queda `verified`; se cambia la IP de las fuentes a B → evento `ip_changed` y el registro pasa a `mismatch` (un solo evento por transición).
3. Dos fuentes que discrepan, o una caída → `uncertain`: `ip_changed` no se emite y `ip_uncertain` aparece una sola vez; al coincidir de nuevo, se retoma.
4. Tras parar y relanzar el Agent: el historial persiste y se ejecuta una comprobación inmediata.
5. Configuración inválida, `managed`, o archivo ausente (con la variable definida) aborta con mensaje claro; sin variable, el Agent arranca como en slice1.
6. Retención: lo más antiguo que el límite se purga; lo reciente no.
7. `status`, `check-ip` y `check-dns` funcionan; `status` no escribe en la BD.
8. Los wheels instalados en venv limpio ejecutan `dyndnsarr-agent status` (la migración 0002 viaja en el paquete).
9. La CI de GitHub pasa en el PR.

## Pruebas

- `tests/unit`: config, consenso, clasificación de registro, repositorios y purga.
- `tests/integration`: ciclo con `httpx.MockTransport` y resolver falso sobre BD temporal; `lifespan`; reinicio (dos instancias sobre la misma BD); CLI.
- `tests/contract`: forma de los eventos según §5 (`event_id`, `sequence`, `type`, `occurred_at`, `payload`); `/healthz` sin cambios.
- `tests/e2e` (marca `e2e`): Agent real + servidores falsos locales.
- Sin servicios reales en la suite automática. Ejecución: `pytest` / `pytest -m "not e2e"`.

## Documentación a actualizar

`docs/operations.md`, `.env.example`, `apps/agent/config/agent.example.toml` (nuevo), `apps/agent/systemd/dyndnsarr-agent.service`, `ESTADO.md`, `docs/conventions.md`, `docs/decisions/0003-*.md` y su índice, `plan.md`, `docs/slices/README.md`, `scripts/verify-wheels.sh`.

## Riesgos y migraciones

- **Fuentes IP de terceros** (ipify, icanhazip) sin SLA: por D3 se prefiere no concluir antes que concluir mal. Un fallo permanente de una fuente deja la IP en `uncertain` indefinidamente; el historial lo muestra y las fuentes son configurables.
- **Caché de resolvers**: un resolver puede servir la IP vieja hasta el TTL; aquí solo se observa (`mismatch` transitorio es esperable). 2b lo trata con `pending_verification`.
- **Sin escrituras**: 2a no escribe DNS ni usa credenciales, de modo que no hay riesgo sobre DonDominio.
- **Migraciones**: 0002 es aditiva sobre la BD del slice1; no hay datos que preservar.
- Dependencias nuevas (`httpx`, `dnspython`) sin lockfile: mismas cotas por versión mayor que el slice1.

## Evidencia

Rama `slice/02a-observacion-ip-dns` publicada en `origin`. Verificado el 2026-10-14 con Python 3.12.3 y 3.11.17:

| Criterio | Resultado |
|---|---|
| 1. venv limpio 3.11 y 3.12: `ruff check`, `ruff format --check`, `pytest` | Verificado con 3.11.17 y 3.12.3: 193 tests, 0 fallos en ambos. |
| 2. E2E: A/A + DNS=A → `verified`; fuentes a B → `ip_changed` y `mismatch`, un evento por transición | Verificado: `tests/e2e/test_ddns_engine.py::test_ip_change_is_reported_once_and_record_goes_to_mismatch` (procesos reales con servidores HTTP y DNS UDP falsos). El cambio de IP se observa al relanzar el Agent (el intervalo mínimo es 60 s); los ciclos periódicos se cubren con `run_forever` en `tests/integration/test_scheduler.py`. |
| 3. Fuentes discrepantes o una caída → `uncertain`, `ip_uncertain` una sola vez; al coincidir, se retoma | Verificado en el e2e (`test_discrepancy_is_uncertain_once_and_resumes_when_sources_agree`, `test_one_source_down_is_uncertain`) y en `tests/integration/test_cycle.py`. |
| 4. Tras parar y relanzar: historial intacto y comprobación inmediata | Verificado: e2e `test_restart_keeps_history_and_checks_immediately` y `tests/integration/test_scheduler.py` (dos instancias sobre la misma BD). |
| 5. Config inválida, `managed` o archivo ausente aborta con mensaje claro; sin variable, arranca como en slice1 | Verificado en e2e (`test_invalid_config_aborts_startup_with_clear_message`, `test_missing_config_file_aborts_startup`), `tests/unit/test_ddns_config.py` y `tests/integration/test_scheduler.py`; el arranque sin variable lo cubren los tests del slice1 (`/healthz`, e2e de arranque) más `test_engine_is_disabled_without_config_file`. |
| 6. Retención: se purga lo antiguo, no lo reciente | Verificado en `tests/unit/test_ddns_repo.py` y `tests/integration/test_cycle.py`. |
| 7. `status`, `check-ip`, `check-dns` funcionan; `status` no escribe | Verificado en `tests/integration/test_cli.py` (filas y mtime de la BD intactos) y en el e2e con el CLI como subproceso. |
| 8. Wheels en venv limpio ejecutan `dyndnsarr-agent status` | Verificado con 3.12 y 3.11 (`scripts/verify-wheels.sh` ampliado; `status` falla sin la migración 0002). |
| 9. CI de GitHub en el PR | **Pendiente.** Rama publicada, pero el PR no se pudo abrir: el token de `gh` es inválido (`gh auth login -h github.com`). |

Desviaciones respecto al diseño del plan (menores):

- `events`: la clave primaria es `sequence INTEGER PRIMARY KEY AUTOINCREMENT` y `event_id` es `UNIQUE` (SQLite exige que `AUTOINCREMENT` sea la clave); el efecto es el mismo y `sequence` no se reutiliza tras purgar.
- `app_state` guarda además `ip_status` (`confirmed`/`uncertain`) para emitir `ip_uncertain` solo al entrar.
- La primera IP confirmada emite `ip_changed` con `previous: null`.
- Las muestras DNS con varios registros A guardan las IP separadas por comas.
- `dyndnsarr-agent` apunta ahora a `dyndnsarr_agent.cli:cli`; `python -m dyndnsarr_agent` sin argumentos sigue sirviendo.

Pendiente del usuario: prueba manual en la Raspberry con un registro real (docs/operations.md §5).
