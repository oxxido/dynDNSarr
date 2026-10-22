# Slice 8 — Instalación asistida y emparejamiento Agent ↔ Hub

- **Estado:** en revisión
- **Fase (arquitectura):** 7 (añadida a architecture §12 en este slice)
- **Rama:** `slice/08-instalacion-emparejamiento`
- **PR:** [#11](https://github.com/oxxido/dynDNSarr/pull/11)

## Objetivo

Que poner DynDNSarr en marcha no exija coser a mano operaciones §2, §6, §7 y §9. Al terminar:

- **Raspberry:** clonar el repo, copiar y rellenar `agent.env` y `agent.toml`, ejecutar
  `sudo scripts/install-agent.sh`. Queda el Agent instalado, con TLS propio y el DDNS
  funcionando aunque no haya Hub.
- **Dockge:** clonar el repo, pegar `apps/hub/compose.yaml` sin cambios en un stack nuevo,
  rellenar el `.env` del stack y arrancar. El Hub genera su CA y abre un puerto HTTPS propio
  para los Agents.
- **Enlace:** el mismo código de emparejamiento en los dos `.env` basta para que Agent y Hub
  se encuentren, se autentiquen e intercambien tokens y certificados, en cualquier orden.
  Sin copiar tokens, CA ni ejecutar `agent-add`/`agent-set-api`/`agent-api-token`.
- **Admin:** sin administrador y con `ADMIN_ORIGIN`, el Hub muestra en sus logs un token de un
  solo uso para crear el administrador desde `/admin/setup`.

Caddy (slice 9) y la UI de publicaciones (slice 10) quedan fuera.

## Alcance

- Incluye:
  - `scripts/install-agent.sh` idempotente (instalar y actualizar) y ejemplos
    `apps/agent/config/agent.env.example`.
  - Protocolo de emparejamiento por código compartido; tarea de emparejamiento en el Agent
    con reintentos; endpoints de emparejamiento en el Hub.
  - Escucha HTTPS del Hub para Agents (`/api/v1` y `/pair/v1`) con CA propia en `/data`.
  - Certificado del Agent fijado por agente en el Hub (en lugar de una CA global).
  - Setup web del primer administrador con token de un solo uso.
  - Compose con contexto de construcción configurable (`DYNDNSARR_SRC`) y variables nuevas.
  - Guía de instalación única (operaciones §12) que sustituye el recorrido por §2–§9 para
    instalaciones nuevas. Registrar slice7 como fusionado.
- No incluye:
  - Caddy: modo Caddyfile vía Agent, gateway guiado ni autopublicación (slice 9).
  - UI de publicaciones (slice 10).
  - Firewall: el instalador no lo toca; imprime la regla recomendada.
  - Imagen publicada, release o tag; descubrimiento mDNS; varios Agents con un mismo código.
  - Optimizar los PNG de la mascota (sigue pendiente).

## Decisiones previas

Decididas con el usuario el 2026-10-22. Se recogen en **ADR-0013**, que modifica ADR-0005
(D1: proxy TLS), ADR-0006 (D1: CA global del Agent) y ADR-0009 (alta solo por CLI). El camino
manual existente sigue funcionando sin cambios.

- **D1 Código compartido.** El usuario genera un código (`openssl rand -hex 16`, mínimo
  128 bits; un código más débil impide arrancar) y lo pone en `DYNDNSARR_AGENT_PAIRING_CODE` y
  `DYNDNSARR_HUB_PAIRING_CODE`. El código es de **un solo uso**: tras emparejar no sirve, aunque
  siga visible en el `.env` de Dockge. Volver a emparejar exige un código nuevo.
- **D2 Protocolo** (HMAC-SHA256 con el código, comparación en tiempo constante):
  1. *Desafío* (TLS sin verificar, sin secretos): el Agent envía un nonce y el Hub responde con
     su CA y `HMAC(código, "hub-ca" ‖ nonce ‖ sha256(CA))`. Si el MAC no cuadra, el Agent no
     envía nada más (MITM detectado).
  2. *Canje* (TLS verificado con esa CA): el Agent envía `agent_id`, URL de su API, su CA,
     **sus dos tokens** (`dda_` hacia el Hub y `ddh_` de su API, generados por el instalador) y
     un MAC sobre todo ello. El Hub registra el agente, guarda hash del `dda_`, el `ddh_`, la URL
     y la CA del Agent, y marca el código consumido en la misma transacción. Responde con un MAC.
  3. Como los tokens los genera el Agent, un canje repetido con el mismo contenido (respuesta
     perdida) es idempotente; otro contenido con un código consumido da `code_consumed`.
  - `agent_id` ya existente da `agent_exists`: no se sustituye un agente aprovisionado a mano.
  - Fallos de MAC: contador persistente; 10 en 15 min bloquean el emparejamiento 15 min.
- **D3 TLS propio del Hub.** Segundo servidor Uvicorn en el mismo proceso y worker, puerto
  `DYNDNSARR_HUB_LINK_PORT` (8443), que sirve solo `/api/v1` y `/pair/v1`. Activo cuando hay
  `DYNDNSARR_HUB_LINK_NAMES` (IPs/nombres LAN del servidor, van al SAN). CA ECDSA P-256 de 10
  años y certificado de servidor de 1 año en `/data` (0600), renovado al arrancar si quedan
  < 30 días: al estar fijada la CA, renovar es transparente para el Agent. Nueva dependencia
  de runtime del Hub: `cryptography`. `/api/v1` en el puerto HTTP se conserva (camino ADR-0005).
- **D4 Agent.** El instalador genera una CA propia y el certificado de la API del Agent (SAN
  = `DYNDNSARR_AGENT_HOST`, 2 años) con `openssl`, y los tokens con `secrets`. La CA del Agent
  queda en la Raspberry (0600 root): solo vale para este Agent porque el Hub la fija por agente,
  así que su clave no permite suplantar a otro. Volver a ejecutar el instalador renueva el
  certificado si quedan < 60 días. El material generado va a `/etc/dyndnsarr/generated.env`
  (segundo `EnvironmentFile`) y nunca se sobrescribe. La CA del Hub obtenida al emparejar se
  guarda en `agent.db` (migración 0007); `[hub]` del TOML lleva `url` y `agent_id`, sin `ca_file`.
  La tarea de emparejamiento reintenta con espera de 30 s a 15 min y no toca el DDNS.
- **D5 Setup del administrador.** Con `ADMIN_ORIGIN` y sin administrador, cada arranque emite
  un token (32 bytes, hash en SQLite, caduca a las 24 h) y lo registra en el log como
  `admin_setup_available`: es la excepción documentada a «sin secretos en logs», válida solo
  hasta crear el administrador. `/admin/setup` pide token, usuario y contraseña dos veces, con
  CSRF de sesión anónima y el bloqueo global de ADR-0009. Con administrador, `/admin/setup` da
  404. `admin-create` y `admin-password` siguen igual.
- **D6 Compose.** `build.context: ${DYNDNSARR_SRC:-../..}` permite pegar el mismo
  `compose.yaml` en Dockge y fijar la ruta del clon en el `.env`. El puerto de enlace se publica
  en `DYNDNSARR_HUB_LINK_BIND` (IP LAN); el HTTP sigue en loopback.

## Plan de implementación

1. **Contrato y criptografía** en `packages/shared`: modelos `/pair/v1`, MAC, validación del
   código y `build_ssl_context` con PEM en memoria. Pruebas: MAC alterado, código débil.
2. **Hub:** migración 0009 (`pairing`, `agents.api_ca_pem`, `admin_setup`), CA y certificado,
   segundo servidor, endpoints de emparejamiento y uso de la CA fijada por agente en
   `agent_client`/`status_refresher`. Pruebas: canje, repetición idempotente, código
   consumido, `agent_exists`, bloqueo, renovación del certificado.
3. **Agent:** migración 0007, tarea de emparejamiento, outbox con la CA guardada, CLI
   `pair-status` y `pair-reset`. Pruebas: CA falsa (MITM) no recibe tokens, Hub apagado →
   reintento, DDNS ajeno al emparejamiento.
4. **Integración extremo a extremo** en loopback: Hub y Agent reales con el mismo código, en
   los dos órdenes de arranque → eventos entregados y `status` consultado sin pasos manuales.
5. **Setup del administrador:** token, página, validación y caducidad.
6. **Instalador y Compose:** `install-agent.sh`, `agent.env.example`, `generated.env`,
   unidad systemd y `compose.yaml`. `scripts/verify-agent-install.sh` lo ejecuta en un
   contenedor `debian:bookworm` (sin systemd) dos veces y comprueba idempotencia.
7. **Documentación** y verificación completa (lint, suite, wheels, Compose, CI).

## Criterios de aceptación

1. En una Debian 12 limpia, `sudo scripts/install-agent.sh` con `agent.env`/`agent.toml`
   deja el Agent activo con `/healthz` HTTPS y DDNS funcionando sin Hub.
2. Repetir el instalador no cambia tokens, certificados vigentes ni datos; actualiza el código.
3. El `compose.yaml` del repo, sin editar, con `DYNDNSARR_SRC` en el `.env`, construye y
   arranca el Hub `healthy` con el puerto de enlace HTTPS.
4. Mismo código en ambos lados, en cualquier orden de arranque: el agente aparece en
   `agent-list`, sus eventos llegan y `agent-status` responde, sin comandos manuales.
5. Con un Hub de CA ajena o código distinto, el Agent no envía tokens ni se empareja; el
   error es explícito en `pair-status` y en el log.
6. El código queda consumido: otro Agent con él recibe `code_consumed`; el mismo canje
   repetido es idempotente.
7. Código de menos de 128 bits: ni Agent ni Hub arrancan, con un mensaje que nombra la variable.
8. Setup: el token sale una vez por arranque en el log, crea un único administrador y deja de
   valer tras usarse o caducar; con administrador `/admin/setup` da 404.
9. Instalaciones con el camino manual (ADR-0005/0006) siguen funcionando: suite existente verde.
10. Ningún log, evento ni respuesta contiene los tokens o el código (salvo el token de setup, D5).

## Pruebas

Unitarias del protocolo y validaciones; integración del Hub (dos servidores, CA, canje) y del
Agent (tarea de emparejamiento, MITM con `trustme`); e2e con procesos reales en loopback;
`scripts/verify-agent-install.sh` en Docker. `.venv/bin/ruff check .`,
`.venv/bin/ruff format --check .`, `.venv/bin/pytest`, `scripts/verify-wheels.sh`,
`scripts/verify-compose.sh`. CI: Python 3.11/3.12 e imagen Docker, más el nuevo script.

## Documentación a actualizar

ADR-0013; architecture §12 (fases 7–9); `plan.md`; índice de slices y decisiones; ESTADO;
convenciones (fijado en slice8); operaciones §12 (instalación asistida) y referencias desde
§2, §3, §6, §7 y §9; `.env.example`, `agent.env.example`, `compose.yaml`, unidad systemd.

## Riesgos y migraciones

- Migraciones aditivas: Hub 0009 y Agent 0007. Backup previo (§4).
- El código queda visible en el `.env` de Dockge hasta que se borre; consumido no tiene valor.
  Antes de consumirse, quien lo lea puede emparejar un Agent falso: hay que arrancar el Agent
  pronto o borrar el código si no se va a usar.
- La primera conexión no está verificada; la seguridad depende de la entropía del código
  (se exigen 128 bits) y del MAC sobre la CA antes de enviar secretos.
- La clave de la CA del Agent pasa a vivir en la Raspberry (D4), a diferencia de §7.1.
- El token de setup en logs: quien lea los logs del Hub antes de crear el administrador puede
  crearlo. Caduca en 24 h y deja de emitirse en cuanto existe un administrador.
- `cryptography` añade un wheel nativo al Hub (amd64/arm64 disponibles).
- El instalador necesita `sudo`: lo ejecuta el usuario, nunca el agente de programación.

## Evidencia

Implementado en `slice/08-instalacion-emparejamiento` (2026-10-22). Decisiones en
[ADR-0013](../decisions/0013-instalacion-emparejamiento.md); guía en operaciones §12.

Ajustes durante la implementación (sin cambiar las decisiones D1–D6):

- El outbox del Agent ya no reutiliza conexiones: una conexión TLS ociosa contra el puerto de
  enlace retrasaba 30 s el cierre del Hub (lo detectó el e2e con SIGTERM). Es lo mismo que ya hacía
  `build_http_client` en el sentido Hub → Agent.
- Sin el archivo de la CA global (`AGENT_CA_FILE`, por defecto `/data/agent-ca.pem` en Compose),
  los Agents emparejados siguen funcionando y los no emparejados fallan cerrados (`tls_error`).
- El instalador acepta `agent.env`/`agent.toml` en la raíz del clon (ignorados por Git); sin ellos
  conserva la configuración instalada. `HOST` debe ser concreto (no `0.0.0.0`).
- `agent_id` ya registrado da `agent_exists` también si lo registró un emparejamiento anterior:
  volver a emparejar el mismo `agent_id` en el mismo Hub exige otro `agent_id` (limitación anotada
  en ADR-0013).

Validación local:

| Comprobación | Resultado |
|---|---|
| `ruff check .` · `ruff format --check .` | correctos |
| `pytest` (todo, incluidos e2e) | 971 pruebas correctas (899 antes del slice) |
| `scripts/verify-wheels.sh` | correcto |
| `scripts/verify-compose.sh` | correcto: `compose.yaml` sin editar desde otro directorio con `DYNDNSARR_SRC`, Hub `healthy`, puerto de enlace HTTPS verificable con la CA del desafío, `admin_setup_available` en el log |
| `scripts/verify-agent-install.sh` | correcto en `debian:bookworm`: tres ejecuciones sin cambios en tokens/CA/certificado, permisos, `/healthz` HTTPS con la CA generada, ciclo DDNS sin Hub, `pair-status` con `connect_error` y sin secretos en el log |

Criterios:

1. Instalador en Debian 12 limpia: `verify-agent-install.sh` (sin systemd en el contenedor; la
   unidad y el arranque por systemd quedan para la prueba manual en la Raspberry).
2. Idempotencia: misma verificación (tres ejecuciones, con y sin archivos).
3. Compose sin editar con `DYNDNSARR_SRC`: `verify-compose.sh`. Dockge real: prueba manual.
4. Cualquier orden de arranque: `tests/e2e/test_pairing.py` (procesos reales; `agent-list`,
   eventos por el puerto de enlace y `agent-status` con la CA fijada).
5. Hub impostor o código distinto: `test_agent_pairing.py` (MAC falso y CA reenviada con TLS
   real: el impostor no recibe el canje; `hub_mac_mismatch`/`tls_error` en `pair-status` y log).
6. Código consumido e idempotencia: `test_hub_pairing.py`.
7. Código débil: `test_hub_pairing.py` y `test_agent_pairing.py` (procesos que no arrancan y
   nombran la variable).
8. Setup del administrador: `test_hub_admin_setup.py` (uno por arranque, un solo uso, caducidad,
   CSRF/origen, bloqueo global, 404 con administrador).
9. Camino manual: la suite existente sigue verde.
10. Sin secretos: comprobado en logs y consola de las pruebas del Hub, del Agent, del e2e y del
    contenedor del instalador.

Pendiente del operador (requiere `sudo` y la infraestructura real): instalar en la Raspberry con
systemd, pegar el `compose.yaml` en Dockge y emparejar con un código real (operaciones §12).
