# ADR-0013 — Instalación asistida y emparejamiento Agent ↔ Hub por código

- Estado: aceptada
- Fecha: 2026-10-22
- Alcance: slice8, arquitectura §3, §5, §11–12. Modifica ADR-0005 (D1: proxy TLS obligatorio),
  ADR-0006 (D1: CA global del Agent) y ADR-0009 (alta del administrador solo por CLI). El camino
  manual de esos ADR sigue funcionando sin cambios.

## Contexto

Poner DynDNSarr en marcha exigía coser a mano operaciones §2, §6, §7 y §9: una CA local para
el Agent, un proxy TLS delante del Hub, copiar tokens en los dos sentidos y la CA al volumen,
`agent-add`, `agent-set-api`, `agent-api-token` y `admin-create` por terminal. Cada paso es una
ocasión de error y algunos sacan secretos a la consola o al portapapeles.

## Decisión

**Código compartido.** El usuario genera un código (`openssl rand -hex 16`) y lo pone en
`DYNDNSARR_AGENT_PAIRING_CODE` y `DYNDNSARR_HUB_PAIRING_CODE`. Menos de 128 bits en hex impide
arrancar, con un mensaje que nombra la variable. El código nunca viaja: cada lado demuestra
conocerlo con HMAC-SHA256 (comparación en tiempo constante, una etiqueta por mensaje). Es de un
solo uso: el Hub guarda el `sha256` de cada código consumido.

**Protocolo `/pair/v1`.** (1) Desafío por TLS sin verificar y sin secretos: el Agent envía un nonce
y el Hub responde con su CA y `mac("hub-ca", nonce, sha256(CA))`; si no cuadra, el Agent no envía
nada más. (2) Canje por TLS verificado con esa CA: `agent_id`, URL y CA de la API del Agent y sus
dos tokens (`dda_`, `ddh_`), con su MAC. El Hub registra el agente (hash del `dda_`, el `ddh_`
en claro como en ADR-0006, URL y CA) y consume el código en una transacción, y responde con un
MAC. Los tokens los genera el Agent: repetir el mismo canje es idempotente; otro contenido con el
código consumido da `code_consumed`; un `agent_id` existente da `agent_exists` (no se sustituye un
agente aprovisionado a mano ni otro emparejado). Diez fallos de MAC en 15 min bloquean el
emparejamiento 15 min (persistente).

**Puerto de enlace del Hub.** Con `DYNDNSARR_HUB_LINK_NAMES` (IPs/nombres LAN, al SAN) el Hub abre
un segundo servidor Uvicorn en el mismo proceso y worker (`LINK_PORT`, 8443) con TLS propio, que
solo sirve `/api/v1` y `/pair/v1`. CA ECDSA P-256 de 10 años y certificado de 1 año en `/data`
(0600), renovado al arrancar si quedan menos de 30 días o cambian los nombres: como el Agent fija
la CA, renovar le es transparente. `/pair/v1` no existe en el puerto HTTP; `/api/v1` sigue allí
para el camino con proxy (ADR-0005). Nueva dependencia del Hub: `cryptography`.

**CA del Agent fijada por agente.** El instalador genera en la Raspberry una CA propia (0600 root)
y el certificado de la API (SAN = `DYNDNSARR_AGENT_HOST`, 2 años; se renueva al repetir el
instalador si quedan menos de 60 días). El Hub guarda esa CA en `agents.api_ca_pem` y monta, para
la URL de cada Agent emparejado, un transporte que solo confía en ella: la clave de esa CA no
permite suplantar a otro Agent. Sin CA fijada se usa la global (`AGENT_CA_FILE`); si falta ese
archivo, los Agents no emparejados fallan cerrados (`tls_error`) y los emparejados siguen.

**Agent.** La CA del Hub obtenida al emparejar va a `agent.db` (`hub_pairing`); con ella se
verifica la entrega de eventos. `[hub]` lleva solo `url` (el puerto de enlace) y `agent_id`. La
tarea de emparejamiento reintenta con 30 s ×2 hasta 15 min, registra cada intento para
`pair-status` y no toca el DDNS. `pair-reset` olvida la CA del Hub (p. ej. Hub reinstalado): exige
un código nuevo en los dos lados.

**Instalador.** `scripts/install-agent.sh` (lo ejecuta el usuario con `sudo`): usuario, venv,
código desde el clon, tokens con `secrets` y TLS con `openssl` en `/etc/dyndnsarr/generated.env`
y `/etc/dyndnsarr/tls`, que nunca se sobrescriben, y unidad systemd con ese archivo como primer
`EnvironmentFile` opcional. No toca el firewall.

**Setup del administrador.** Con `ADMIN_ORIGIN` y sin administrador, cada arranque emite un token
(32 bytes, hash en SQLite, caduca a las 24 h) y lo escribe en el log como `admin_setup_available`
(`setup_code`). Es la excepción documentada a «sin secretos en logs», válida solo hasta crear el
administrador. `/admin/setup` pide token, usuario y contraseña dos veces, con CSRF de sesión
anónima y el bloqueo global de credenciales de ADR-0009. Con administrador da 404.

**Compose.** `build.context: ${DYNDNSARR_SRC:-../..}` permite pegar el mismo `compose.yaml` en
Dockge; el puerto de enlace se publica en `DYNDNSARR_HUB_LINK_BIND` (IP LAN).

## Consecuencias

- La primera conexión no está verificada: la seguridad descansa en la entropía del código y en el
  MAC sobre la CA antes de enviar secretos. Hasta consumirse, quien lea el código (p. ej. en el
  `.env` de Dockge) puede emparejar un Agent falso: hay que arrancar el Agent pronto o borrarlo.
- La clave de la CA del Agent vive en la Raspberry (frente a ADR-0006/§7.1, donde no salía de un
  equipo de confianza); solo vale para ese Agent.
- Quien lea los logs del Hub antes de crear el administrador puede crearlo; el token caduca en
  24 h y deja de emitirse en cuanto existe el administrador.
- Volver a emparejar un `agent_id` ya registrado en el mismo Hub no está soportado: hace falta
  otro `agent_id` (o el camino manual).
- Las conexiones del outbox no se reutilizan: una conexión TLS ociosa retrasaría 30 s el cierre
  del puerto de enlace.
