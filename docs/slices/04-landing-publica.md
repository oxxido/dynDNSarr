# Slice 4 — Landing pública real

- **Estado:** fusionado
- **Fase (arquitectura):** 3 ([architecture.md](../architecture.md) §8, §9 «UI pública», §13 casos 12, 13 y 15; §14 A5 y A7)
- **Rama:** `slice/04-landing-publica`
- **PR:** [#6](https://github.com/oxxido/dynDNSarr/pull/6)

## Objetivo

El Hub sirve la página pública de estado de la maqueta aprobada ([design_docs/index.html](../../design_docs/index.html)) con **datos reales**: estado general con la mascota, última comprobación satisfactoria, las cinco últimas renovaciones (fecha, hora y los dos últimos dígitos de la IPv4), estado de conectividad e historial de 7 días. Todo sale de un `PublicStatusDTO` que **no puede** contener la IP completa ni datos privados, y se puede desactivar. Un indicador sin observación reciente se muestra como `Desconocido`, nunca inventado.

## Alcance

**Incluye**

- Página `GET /` (Jinja2, CSS y JS inline como en la maqueta, PNG estáticos) y `GET /public/v1/status` (el mismo DTO en JSON). Sin selector de estado de demostración, sin IPv6 y sin el texto «datos simulados». Tema `Sistema / Claro / Oscuro` persistido y `prefers-reduced-motion`, ya presentes en la maqueta. Mascota con cara SVG superpuesta (ojos que siguen al puntero, parpadeo, boca por estado), sombra, flotación y burbujas de fondo ([ADR-0008](../decisions/0008-animacion-mascota.md)).
- `PublicStatusDTO`: modelo Pydantic con campos de lista blanca; `ip_suffix` solo admite 1–2 dígitos.
- **Proyección pública** en la BD del Hub (migración 0004): `public_renewals`, `public_agent_status` y `public_status_samples`. Se alimentan sin guardar la IP completa; la página y el JSON solo leen estas tablas, nunca `events`.
- **Refresco de estado**: una tarea del Hub consulta cada ~60 s `GET /internal/v1/status` de cada agente con API configurada (cliente de 3b) y guarda la proyección y una muestra de estado.
- Reglas de estado `healthy/degraded/critical/unknown` (provisionales, D3), historial de 84 barras de 2 h con las muestras del Hub, y uptime solo como lo que se observó (gris donde no hay datos).
- Ajustes del Hub: `DYNDNSARR_HUB_PUBLIC_ENABLED`, `DYNDNSARR_HUB_PUBLIC_SHOW_DOMAINS`, `DYNDNSARR_HUB_STATUS_REFRESH_SECONDS`.
- Cabeceras de seguridad (CSP con nonce), `Jinja2` con autoescape, `StaticFiles` solo para `static/`.
- Documentación operativa: cómo publicar la landing tras un proxy **sin** exponer el resto del Hub.

**No incluye**

- Login, vistas privadas, auditoría, UI de configuración (slice 5). Umbrales parametrizables y vistas de uptime 24 h / 30 d (slice 5, A4); aquí hay una regla mínima provisional.
- Reconstruir la mascota en SVG (§0.7): los PNG son la mascota y solo se les superpone la cara (A7, ADR-0008). Optimizar el peso de los PNG (~1,5 MB cada uno): pendiente.
- Refresco automático de la página (`meta refresh`/SSE), conversión de horas a la zona del navegador (las horas se muestran en UTC), SEO, internacionalización, `HEAD`/`OPTIONS` específicos.
- Rate limiting propio (va en el proxy, §3; la petición pública solo lee SQLite y nunca llama al Agent).
- Cambios en el Agent, en el contrato de eventos o en `shared/control.py`.
- Integración con Homarr (slice 6), que podrá reutilizar el DTO.

## Precondiciones

- **Los PNG** (bases sin rostro, A7) los aportó el usuario el 2026-10-20 en [design_docs/assets/](../../design_docs/assets/) (`pulpo-{verde,naranja,rojo}.png`, 1254×1254 RGBA) y se copian como `static/assets/dyno-{green,orange,red}.png`. No se generan ni se sustituyen por otra mascota.
- 3b fusionada en `main` (PR #5, verificado con `gh` el 2026-10-19).

## Decisiones previas

| # | Decisión | Propuesta | Alternativa descartada |
|---|---|---|---|
| D1 | **Fuente del estado** | Una tarea asyncio del Hub (en el `lifespan`, un solo worker) consulta `GET /internal/v1/status` cada `DYNDNSARR_HUB_STATUS_REFRESH_SECONDS` (60 s, mínimo 15) a cada agente no deshabilitado con `api_url`/`api_token`, usando el `AgentClient` de 3b (misma CA, timeout 10 s, códigos seguros). La petición web **nunca** llama al Agent: lee lo ya proyectado (sin amplificación hacia la Raspberry). Un agente sin API configurada no tiene datos y aparece `Desconocido`. Esto adelanta el *health* periódico que 3b dejó al slice 5 y cubre `agent_unreachable` de §8. Elegido por el usuario (2026-10-19) | Solo eventos (no hay «última comprobación» real: llegan solo al cambiar algo); latido nuevo del Agent (cambia el contrato de 3a y la regla §7.7) |
| D2 | **Proyección pública sin IP completa** | El proyector de renovaciones corre en la ingestión (`services/events_handlers.py`, misma transacción, solo eventos nuevos) y guarda de `dns_update_succeeded` el instante, el registro y `ip_suffix` (último octeto, sus dos últimos dígitos). `dns_verified` marca como verificada la renovación pendiente del mismo registro con el mismo sufijo. El refresco guarda de `AgentStatus` solo `ip_status`, `public_ip_confirmed_at`, `last_check_at` y por registro (`name`, `verification`, `since`, `failures`, `next_attempt_at`, `last_error_code`): **`public_ip` se descarta al leer**. La capa pública (`public/`) no importa `events_repo`. Cumple el aviso de 3a («el DTO público no debe leer `events`»). Un hash del sufijo o de la IP no se guarda: 2³² candidatos lo harían reversible | Calcular el DTO desde `events` en cada petición (un fallo de filtrado filtraría la IP); guardar la IP y ocultarla al renderizar |
| D3 | **Reglas de estado (A4 parcial, provisionales)** | Función pura `derive_state` con constantes en `public/state.py`. Por agente: **`unknown`** sin proyección, con `refreshed_at` más antiguo que 3 × el intervalo de refresco, con `last_check_at` o `public_ip_confirmed_at` nulo o de hace más de 15 min, o sin registros habilitados; **`critical`** si algún registro está en `mismatch` desde hace más de 1 h, o con `failures > 0` y `next_attempt_at` nulo (error definitivo, 2b); **`degraded`** si `ip_status = uncertain`, o algún registro no está `verified` (`pending_verification`, `mismatch` reciente, `unknown`, `failures > 0`); **`healthy`** en otro caso. Varios agentes: el peor, con orden `critical > degraded > unknown > healthy` (un agente sin datos impide afirmar «operativo»). Un Agent inalcanzable es `unknown`, **nunca** `critical`/«Sin conexión» (§8: no implica que Internet haya caído). El estado se deriva **al leer**, para que el envejecimiento de la proyección lo vuelva `unknown` aunque el refresco ya no corra. El slice 5 los hará configurables | Parametrizarlos ahora (sin consumidor aún); guardar el estado ya derivado (no envejecería) |
| D4 | **A5 — nombres y renovaciones** | Una renovación es un `dns_update_succeeded` (el Agent solo llama al proveedor cuando hace falta). Por defecto la fila muestra fecha y hora (UTC), «IP terminada en NN» y la verificación (`Correcta` / `Pendiente de verificación`, §13-4); **no** muestra el nombre del registro: el dominio se resuelve públicamente y dejaría la IP a una consulta DNS. `DYNDNSARR_HUB_PUBLIC_SHOW_DOMAINS=true` lo añade (el administrador lo autoriza explícitamente, §9). Máximo 5 filas (el DTO lo valida) y 50 guardadas por agente. «Estado DNS» y «Resolución DNS» salen de la verificación de los registros; «Conectividad IPv4» de `ip_status`; «Conectividad IPv6» se muestra `No disponible` (§1); «Respuesta del servicio» es que el último refresco respondió | Nombres de ejemplo anonimizados (inventan datos); mostrar IPv6 de la maqueta |
| D5 | **Historial** | Cada refresco, con éxito o fallo, inserta una muestra `(agent_id, sampled_at, state)` con el estado derivado en ese momento (un fallo de refresco es `unknown`). Retención 30 días, purga al refrescar. 84 barras de 2 h sobre los últimos 7 días terminando ahora; cada barra es el peor estado **conocido** de sus muestras y `unknown` (gris) si no hay ninguna conocida. El DTO lleva `coverage` (fracción de barras con datos) para no afirmar un uptime que no se observó (§8). Elegido por el usuario (2026-10-19) | Ocultar el panel hasta el slice 5; reconstruirlo desde eventos (rellenaría huecos como operativos) |
| D6 | **Plantilla, rutas y cabeceras** | `GET /` y `GET /public/v1/status` (un prefijo propio: `/api/v1` es el del Agent autenticado). Con `PUBLIC_ENABLED=false` ambas y `/static` devuelven 404 con el mismo cuerpo que una ruta inexistente. La maqueta se conserva (CSS y JS inline, mismas clases): se retiran selector de demostración, IPv6, pie de «datos simulados» y el relleno por JS; las barras y filas se renderizan en servidor con Jinja2 (`autoescape`), así que el JS que queda es el del tema. Imágenes en `static/assets/dyno-{green,orange,red}.png`; `unknown` usa la verde en gris como la maqueta. CSP por petición con **nonce** (`default-src 'none'; img-src 'self'; style-src 'nonce-…'; script-src 'nonce-…'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. A7: bases sin rostro con cara SVG superpuesta (ADR-0008). Solo `GET` | `'unsafe-inline'` en la CSP; mover CSS/JS a archivos (la arquitectura pide inline); reconstruir la mascota en SVG |
| D7 | **Dónde vive el código** | `apps/hub/dyndnsarr_hub/public/` (`dto.py`, `state.py`, `builder.py`: reglas y DTO, sin HTTP ni SQL), `db/public_repo.py` (SQLite), `services/status_refresher.py` (red + escritura), `api/public.py` (HTTP y plantilla), `templates/public/index.html.j2`, `static/assets/`. Nueva dependencia: `jinja2`. Sin tocar `shared` (el DTO público solo lo produce el Hub) | DTO en `shared` (nada más lo consume por ahora) |

## Diseño mínimo

**Migración `0004_public_projection.sql` (Hub, tras la 0003 de 3b):**

- `public_renewals(agent_id, event_id, record_name, occurred_at, ip_suffix, verified_at, PRIMARY KEY (agent_id, event_id))`; el `event_id` mantiene la idempotencia ante reenvíos.
- `public_agent_status(agent_id PK, refreshed_at, ip_status, public_ip_confirmed_at, last_check_at, records TEXT)`; `records` es JSON de `[{name, verification, since, failures, next_attempt_at, last_error_code}]`.
- `public_status_samples(agent_id, sampled_at, state, PRIMARY KEY (agent_id, sampled_at))` con índice por `sampled_at`.

Ninguna columna puede contener una IP completa; una prueba vuelca las tres tablas con un canario.

**DTO (`public/dto.py`, `extra="forbid"`, `schema_version = 1`):** `generated_at`; `state` (`healthy|degraded|critical|unknown`); `last_successful_check_at` (instante o nulo); `dns` (`ok|attention|unknown`); `checks[]` con `id` (`dns_resolution|ipv4|ipv6|service`) y `state` (`ok|attention|unavailable|unknown`); `renewals[]` (≤ 5) con `occurred_at`, `ip_suffix` (`^[0-9]{1,2}$`), `verification` (`verified|pending`) y `record` (solo con `SHOW_DOMAINS`, si no `null`); `history` con `from`, `bucket_seconds = 7200`, `buckets[84]` y `coverage`. No hay campo libre ni `dict[str, Any]`: lo que no está declarado no se puede emitir.

**Flujo:** `status_refresher` (red → una transacción corta, sin `await` abierto, sobre la conexión única del Hub como en 3b) → `public_repo` → `builder.build_dto(conn, now, settings)` (lee solo `public_*`, aplica `derive_state` y agrega) → `api/public.py` (JSON o plantilla). El estado mostrado en la mascota, el hero y la tarjeta sale de un único valor del DTO.

**Textos de estado** (de la maqueta): `Operativo`, `Degradado`, `Sin conexión` (solo `critical`: fallo DNS persistente observado) y `Desconocido` (con «No hay datos suficientes para confirmar el estado actual»; no se dice que Internet esté caído). «Última comprobación» usa `public_ip_confirmed_at` (consenso de ≥2 fuentes), no el último intento.

## Plan de implementación

Commits pequeños, tests antes del código. Cada paso termina con `ruff` y `pytest -m "not e2e"` en verde.

1. **Recomprobar contra `main`** (3b fusionada): nombres de módulos, numeración de migración (Hub 0004) y que `AgentClient.status()` y los manejadores de eventos son como aquí se describen. Marcar 3b como `fusionado` (índice, archivo del slice y `ESTADO.md`; PR #5 fusionado el 2026-10-19, commit `a05fa44`). *Verifica:* este archivo describe lo que existe.
2. **ADR-0007** (D1–D7) y su entrada en el índice. *Verifica:* enlaces válidos.
3. **DTO y reglas puras** (`public/dto.py`, `public/state.py`). *Verifica:* tabla de `derive_state` (una prueba por regla de D3, incluidos los bordes de 15 min, 1 h y 3 × intervalo, y el orden del agregado entre agentes); extracción de `ip_suffix` (`83.45.12.134` → `34`, `.7` → `7`, entrada no IPv4 o privada no válida → `None`); el DTO rechaza `192.0.2.134`, `34.5` y campos extra; `buckets` con 84 elementos, peor estado conocido por barra, `unknown` sin muestras y `coverage`.
4. **Migración 0004 y repositorio.** *Verifica:* aplica sobre una BD 0003 con datos; las tres tablas se vuelcan sin la IP canario; inserción idempotente por `event_id`; tope de 50 renovaciones por agente; purga de muestras > 30 días; la lectura del DTO no importa ni consulta `events`.
5. **Proyección en la ingestión.** `dns_update_succeeded` → renovación; `dns_verified` → la marca verificada (mismo registro y sufijo, sin depender de que ya exista: un `dns_verified` anterior a su renovación no la verifica); evento duplicado no proyecta dos veces; payload mal formado o IP inválida se ignora sin rechazar el lote (regla de 3b). *Verifica:* tests con el patrón de `test_hub_event_handlers.py`; un reenvío del lote no cambia nada.
6. **Refresco de estado** (`services/status_refresher.py`). Una pasada (`refresh_once`) y el bucle del `lifespan`; la red va antes de abrir la transacción. *Verifica:* con Agent real tras `ASGITransport` se proyecta y se descarta `public_ip` (canario); Agent caído/401/TLS inválido → no avanza `refreshed_at`, inserta muestra `unknown` y el log lleva solo códigos seguros (nada de URL ni token); agente sin API, deshabilitado o revocado se omite; la tarea se cancela limpiamente al cerrar el Hub y un error inesperado no la mata.
7. **Builder y API JSON.** `build_dto`, `GET /public/v1/status`, ajustes de `Settings`, `PUBLIC_ENABLED`. *Verifica:* canarios (IP completa, MAC, token Agent→Hub, token de API, nombre de dispositivo, nombre del registro) ausentes del JSON y de las cabeceras con todos los estados; `SHOW_DOMAINS` añade solo el nombre; desactivado → 404 idéntico al de una ruta inexistente; `POST/PUT/DELETE` → 405; la respuesta sigue siendo válida con la BD vacía (`unknown`, sin renovaciones, historial gris); un Agent en `critical` y otro `unknown` se agregan según D3.
8. **Plantilla y página.** Pasar la maqueta a Jinja2 según D6, `Jinja2Templates` con autoescape, nonce y cabeceras. *Verifica:* una prueba por estado (`healthy/degraded/critical/unknown`): clase de estado, textos, mascota y alt; con `html.parser` no hay `select` de demostración, `IPv6` solo como «No disponible», ni la cadena «simulados»; el nombre del registro con `<script>` se escapa (solo con `SHOW_DOMAINS`); la CSP lleva el nonce de esa respuesta y todo `<script>`/`<style>` lo usa; nonce distinto en dos peticiones; 84 barras con `title` y `aria-label`; el tema `system/light/dark` persiste en `localStorage` (inspección del script) sin destello (el script del tema va antes del CSS).
9. **Assets y paquete.** Añadir los tres PNG a `apps/hub/dyndnsarr_hub/static/assets/` (aportados por el usuario), `StaticFiles` en `/static`, `package-data` (`templates/**`, `static/**`), `packages` en `pyproject.toml` (`public`, templates), `jinja2` en las dependencias y `scripts/verify-wheels.sh`. *Verifica:* los tres PNG responden `200 image/png`; `GET /static/../` y listados no escapan del directorio; en el wheel viajan plantilla y PNG (`verify-wheels.sh` pide `/` y un PNG).
10. **E2E con procesos reales** (Hub + Agent con TLS de `trustme` y servidores falsos de 3b). Cubre §13-12 y la aceptación del §12 Fase 3 sobre HTML, DOM, JSON y la BD. *Verifica:* criterios 2–7.
11. **Documentación y despliegue** (ver abajo). *Verifica:* los enlaces y comandos de `operations.md` se ejecutan tal cual (salvo lo marcado como prueba manual).

Si el slice resulta grande, se puede cortar como 4a (pasos 1–7: proyección, refresco, DTO y JSON) y 4b (8–11: página); lo decide quien lo ejecute y se anota aquí.

## Criterios de aceptación

1. En un venv limpio con Python 3.11 y 3.12, `ruff check`, `ruff format --check` y `pytest` terminan con éxito.
2. **Ausencia de datos privados (§12 Fase 3, §13-12):** con una IP «canario» conocida (`203.0.113.134`) en el Agent, sus eventos y su `status`, el HTML de `/`, el JSON de `/public/v1/status`, sus cabeceras, el DOM interpretado y las tablas `public_*` **no contienen** la IP completa (ni sus tres primeros octetos juntos), ni MAC, ni token (Agent→Hub o API), ni credenciales de DonDominio, ni nombre de dispositivo; contienen `34` como sufijo. Un `PublicStatusDTO` con una IP completa no valida.
3. **Estado real:** Agent sano → `healthy` («Operativo», mascota verde, «Última comprobación» con la hora real); registro en `mismatch` → `degraded` y, pasada 1 h, `critical`; IP `uncertain` → `degraded`; renovación aceptada y aún sin `dns_verified` → fila `Pendiente de verificación`, tras él `Correcta`.
4. **Desconocido honesto:** con el Agent apagado, tras 3 intervalos la página pasa a `Desconocido` sin que nadie la toque, no dice «Sin conexión», la última comprobación conserva su hora real y el historial muestra gris desde la caída; con el Hub recién instalado (sin agentes) también es `Desconocido`.
5. **Historial:** 84 barras; las muestras `healthy/degraded/critical` colorean su barra con el peor estado y donde no hubo muestras es gris; el DTO informa `coverage`.
6. **Publicación:** `PUBLIC_ENABLED=false` → `/`, `/public/v1/status` y `/static/*` dan 404 indistinguible de una ruta inexistente; por defecto no se muestra el nombre del registro y con `SHOW_DOMAINS=true` sí. Solo `GET`.
7. **Seguridad de la página:** CSP con nonce por petición, sin `'unsafe-inline'`; autoescape (un nombre con `<script>` no se ejecuta ni aparece sin escapar); sin `verify=False`; el refresco no deja URL, token ni texto de excepción en logs (canarios); la petición web no abre ninguna conexión al Agent (el Agent falso no registra llamadas durante las peticiones públicas).
8. **Maqueta preservada:** sin selector de demostración ni texto «simulados»; tema `Sistema/Claro/Oscuro` persistente y sin destello; `prefers-reduced-motion` respetado; responsive de la maqueta sin cambios de CSS salvo lo retirado.
9. **Datos retenidos:** muestras > 30 días purgadas; 50 renovaciones por agente como máximo; la BD arranca desde una de 3b con la migración 0004.
10. Los wheels en venv limpio sirven `/` con plantilla y PNG (`verify-wheels.sh`), y la imagen Docker del Hub responde `/` con 200 (`verify-compose.sh`; lo ejecuta la CI).
11. La CI de GitHub pasa en el PR.
12. **Manual del usuario:** con el Agent real (3b configurado), publicar la landing tras el proxy solo con las rutas públicas, ver el estado real, apagar el Agent y comprobar que pasa a `Desconocido`, probar `PUBLIC_ENABLED=false`, y revisar visualmente los cuatro estados y los dos temas en escritorio y móvil.

## Pruebas

- `tests/unit`: reglas de estado, sufijo de IP, DTO (rechazos), barras del historial, migración 0004 y repositorio.
- `tests/integration`: proyección en la ingestión, refresco con Agent real en proceso (`ASGITransport`) y con fallos, API pública y plantilla con el Hub real, desactivación, cabeceras y CSP, nonce, escape, estáticos.
- `tests/contract`: forma del DTO (campos exactos, tipos y patrones) y de las cabeceras públicas; `/healthz` sin cambios.
- `tests/e2e` (marca `e2e`): Hub y Agent como procesos reales; la espera de eventos es larga (el outbox entrega cada 30 s, 3a) y la del refresco se acorta con `STATUS_REFRESH_SECONDS=15`.
- Dependencia nueva: `jinja2` (runtime del Hub). Sin servicios reales; los PNG de las pruebas son los del repositorio.
- Ejecución: `pytest` / `pytest -m "not e2e"`.

## Documentación a actualizar

`docs/operations.md` (nuevo §8: publicar la landing, variables, qué rutas exponer en el proxy, apagarla, interpretación de estados y de `Desconocido`), `docs/conventions.md` («Fijado en slice4»), `docs/decisions/0007-*.md` y su índice, `ESTADO.md`, `plan.md` (A5 y A7 resueltas; A4 parcial), `docs/slices/README.md`, `.env.example`, `apps/hub/compose.yaml` (variables nuevas), `scripts/verify-wheels.sh` y `scripts/verify-compose.sh`.

## Riesgos y migraciones

- **Publicar el Hub entero expone `/api/v1` del Agent.** El proxy público debe reenviar solo `/`, `/static/*` y `/public/*` (receta en operaciones §8); el resto exige token pero no debería ser alcanzable desde Internet.
- **El dominio y la IP:** si se activa `SHOW_DOMAINS`, el nombre resuelve a la IP completa en el DNS público; mostrarlo es decisión explícita del administrador y se documenta. Los dos últimos dígitos son parcialmente identificadores: es lo que pide el PRD (WEB-01).
- **Regla de estado provisional:** 15 min, 1 h y 3 × intervalo son valores de partida, no verificados con datos reales; el slice 5 los hará configurables. Un `critical` falso o tardío es posible y se anota con la evidencia de uso.
- **Latencia del estado:** la página muestra lo del último refresco (≤ 60 s + intervalo de comprobación del Agent, 300 s por defecto), no el instante actual; un dashboard abierto no acelera el motor (§2).
- **Sin refresco de la página:** el visitante ve lo del momento de cargar; no hay `meta refresh` en este slice.
- **Hora en UTC:** distinta de la local del visitante; se rotula `UTC`.
- **Sin agente con API configurada la landing dice `Desconocido`:** es lo correcto (no hay observación), pero un Hub recién instalado debe completar 3b (§7 de operaciones) para ver estado; se documenta.
- **Rendimiento:** el refresco escribe una fila por minuto y agente (≈ 43 000 en 30 días); se purga en cada pasada. Una petición pública hace unas pocas lecturas indexadas; con un proxy sin límites de tasa un atacante puede saturar el único worker del Hub. Se documenta el límite de tasa en el proxy; no se implementa aquí.
- **Peso de los PNG:** ~1,5 MB cada uno (~4,5 MB por carga). Pendiente optimizarlos (redimensionar a ~800 px) antes del release.
- **Cara superpuesta:** sus coordenadas dependen de la proporción y posición del cuerpo en los PNG actuales; si se sustituyen, hay que reajustarla (ADR-0008).
- **Migración 0004:** crea tablas nuevas, sin tocar las existentes. Las renovaciones anteriores a esta versión no se proyectan (solo desde la ingestión nueva): la lista arranca vacía y se llena con las siguientes. Respaldar `hub.db` antes de actualizar (operaciones §4).

## Evidencia

**Merge verificado el 2026-10-20:** `gh pr view 6` confirma PR #6 `MERGED`, fusionado a las 08:50:41 UTC en `main` con commit `e7a20c54a8074012ad751b09cffdcd8c085ebe99`. La fusión no acredita las comprobaciones manuales pendientes que se detallan abajo.

Ejecutado el 2026-10-19 en Python 3.12 (venv local; la CI cubre 3.11).

- **Estado de implementación:** pasos 1–11 hechos; el paso 9 se cerró el 2026-10-20 al aportar el usuario los PNG (`test_the_mascot_pngs_are_served` ya no se salta). Ese día se añadió la animación de la mascota (ADR-0008): cara SVG con ojos que siguen al puntero, parpadeo, boca por estado, sombra, flotación y burbujas; `test_mascots_have_a_decorative_face_and_bubbles_float_behind` la cubre, y las capturas con Chrome headless (hero y minis) y una simulación del puntero confirman que las pupilas se desplazan hacia él.
- **1, 8 (lint y pruebas):** `ruff check`, `ruff format --check` y `pytest` completo (incluye e2e): 793 passed, 3 skipped el 2026-10-19 y 800 passed, 0 skipped el 2026-10-20 (con los PNG y la animación). El e2e `test_public_landing_shows_real_state_without_private_data_and_goes_unknown` pasó 4 de 4 veces seguidas.
- **2 (ausencia de datos privados):** canario `83.45.12.134` (el Agent exige IPv4 global; el `203.0.113.134` del plan es de documentación). `test_no_private_value_reaches_the_json_nor_the_headers`, `test_no_private_value_reaches_the_html`, `test_public_tables_never_hold_the_full_ip` y el e2e con procesos reales (HTML, JSON, cabeceras y tablas `public_*`, tokens y credenciales); el DTO rechaza IP completa y campos extra (`test_public_dto.py`, `test_public_contract.py`).
- **3, 4, 5 (estado, desconocido, historial):** `test_public_state.py` (cada regla de D3 y sus bordes, agregado, barras y `coverage`), `test_hub_public_api.py` y el e2e (Agent apagado → `Desconocido` sin tocar la página, conservando la hora de la última comprobación). El e2e no puede mostrar «gris desde la caída» a nivel de barra (todo cae en una barra de 2 h); eso lo cubren las pruebas unitarias.
- **6, 7 (publicación y seguridad):** 404 idéntico con la página desactivada (integración y e2e), solo `GET` (405), CSP con nonce distinto por petición y sin `'unsafe-inline'`, sin `style=` ni `on*`, escape de un nombre con `<script>`, `test_public_requests_never_call_the_agent`, y logs del refresco solo con códigos (`test_the_loop_survives_errors_logs_only_safe_codes_and_stops_when_cancelled`).
- **8 (maqueta):** sin selector de demostración ni «simulados»; CSS de la maqueta salvo lo retirado y unas pocas clases añadidas (la CSP prohíbe `style=`). Capturas con Chrome headless (escritorio y móvil, con imágenes rotas por falta de PNG).
- **9 (retención):** purga a 30 días, tope de 50 renovaciones y migración 0004 sobre una BD 0003 con datos (`test_hub_public_repo.py`).
- **10:** `scripts/verify-wheels.sh` en venv limpio: la plantilla viaja en el wheel y `/` responde; avisa de que no comprobó los PNG. Encontró un fallo real (sin PNG el directorio `static` no viaja y el Hub no arrancaba): `StaticFiles(check_dir=False)`. `verify-compose.sh` se ha ampliado (`/` con 200) pero no se ha ejecutado aquí (requiere Docker; lo ejecuta la CI).
- **Hallazgo:** una conexión TLS ociosa del pool de `httpx` en el Hub retrasaba más de 15 s el apagado del Agent; `build_http_client` ya no mantiene conexiones persistentes (`max_keepalive_connections=0`).
- **Pendiente (usuario):** criterio 11 (CI en el PR), criterio 12 (manual, [operaciones §8.4](../operations.md)) y optimizar el peso de los PNG.
