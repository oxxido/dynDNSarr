# Operación

Guía de arranque y despliegue (slices 1 a 3b). Variables de configuración: [.env.example](../.env.example).

Ambos componentes escuchan por defecto en `127.0.0.1`. Cámbialo solo con un firewall que limite el origen: el Agent no debe ser accesible desde Internet ni por equipos distintos al Hub.

## 1. Desarrollo local

Ver [README.md](../README.md). Cada componente crea su SQLite (`agent.db` / `hub.db`, permisos `0600`) en `DYNDNSARR_<COMPONENTE>_DATA_DIR` y aplica las migraciones al arrancar. Comprobación:

```bash
curl -s http://127.0.0.1:8001/healthz   # {"status":"ok","component":"agent","version":"0.1.0"}
```

`/healthz` responde 503 con `"status":"error"` si la base de datos no responde. Si la base tiene una versión de esquema posterior a la del código, el componente no arranca.

Wheels (lo que se instala en la Raspberry):

```bash
python3 -m pip wheel --no-deps -w dist packages/shared apps/agent apps/hub
scripts/verify-wheels.sh   # construye, instala en un venv limpio y arranca Agent y Hub
```

## 2. Agent en la Raspberry (systemd)

Requiere Python 3.11+ del sistema (ADR-0002), `python3-venv` y `sudo`. Los comandos con `sudo` los ejecuta la persona que administra la Raspberry.

1. Copiar los wheels de `shared` y `agent` (sección 1) a la Raspberry.
2. Usuario y entorno:
   ```bash
   sudo useradd --system --home-dir /var/lib/dyndnsarr --shell /usr/sbin/nologin dyndnsarr
   sudo python3 -m venv /opt/dyndnsarr/venv
   sudo /opt/dyndnsarr/venv/bin/pip install dyndnsarr_shared-*.whl dyndnsarr_agent-*.whl
   ```
   (`pip` descarga FastAPI, Uvicorn y Pydantic desde PyPI.)
3. Configuración (opcional, hay valores por defecto): copiar la sección Agent de [.env.example](../.env.example) a `/etc/dyndnsarr/agent.env` y protegerla:
   ```bash
   sudo install -d -m 0750 -o root -g dyndnsarr /etc/dyndnsarr
   sudo install -m 0640 -o root -g dyndnsarr agent.env /etc/dyndnsarr/agent.env
   ```
   El archivo debe existir aunque esté vacío: la unidad lo exige. Para activar el motor DDNS añade además la configuración de la sección 5.
4. Instalar y arrancar la unidad [apps/agent/systemd/dyndnsarr-agent.service](../apps/agent/systemd/dyndnsarr-agent.service):
   ```bash
   sudo cp dyndnsarr-agent.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now dyndnsarr-agent
   curl -s http://127.0.0.1:8001/healthz
   journalctl -u dyndnsarr-agent -o cat      # logs JSON
   ```
5. Persistencia y recuperación: `sudo systemctl restart dyndnsarr-agent` conserva los datos (`instance_id` igual, `boot_count` +1); `Restart=always` lo relanza tras `sudo kill -9 $(systemctl show -p MainPID --value dyndnsarr-agent)`.

Los datos viven en `/var/lib/dyndnsarr` (creado por systemd, modo `0700`).

## 3. Hub con Docker Compose / Dockge

El Hub se construye desde el repositorio (aún no hay imagen publicada).

```bash
cd apps/hub
docker compose up -d --build        # http://127.0.0.1:8000/healthz
docker compose ps                   # el estado pasa a "healthy"
```

- Por defecto el puerto se publica solo en `127.0.0.1` del servidor. Para la LAN, crear `apps/hub/.env` con `DYNDNSARR_HUB_BIND=<IP LAN del servidor>` (puerto con `DYNDNSARR_HUB_PORT`).
- Los datos están en el volumen `hub-data` (`/data` en el contenedor). El contenedor corre como usuario no root (uid 10001).
- **Dockge:** crear un stack con el contenido de `apps/hub/compose.yaml` y cambiar `build.context` por la ruta absoluta del clon del repositorio en el servidor (el `../..` relativo solo vale dentro del repositorio). Este camino no está verificado en este slice.
- Verificación automática (build, healthcheck, restart y persistencia, con proyecto y puerto propios que se eliminan al terminar): `scripts/verify-compose.sh`.
- El Hub habla **HTTP** y recibe el token de cada Agent: no publiques su puerto a la LAN. Déjalo en `127.0.0.1` (o en la red de Docker del proxy) y pon delante un proxy TLS (§6).
- Consola del Hub (registro de agentes y eventos recibidos), dentro del contenedor: `docker compose exec hub dyndnsarr-hub agent-list` (§6).

## 4. Backup y restauración

Los dos componentes son una SQLite en modo WAL. Las copias contienen datos de configuración: protégelas (`0600`) y no las subas al repositorio.

- **Agent:** con el servicio parado (`sudo systemctl stop dyndnsarr-agent`), copiar `/var/lib/dyndnsarr/agent.db*` (incluye `-wal` y `-shm` si existen) y `/etc/dyndnsarr/agent.toml`. **Antes de actualizar al slice 3a**: la migración 0004 añade columnas a `events` (los eventos ya guardados quedan pendientes de entrega) y no hay vuelta atrás automática. **Antes de actualizar al slice 3b**: la migración 0005 solo crea tablas nuevas (`configuration_versions`, `commands`), también sin vuelta atrás automática. Restaurar: servicio parado, copiar los archivos de vuelta con dueño `dyndnsarr` y modo `0600`, arrancar.
- **Hub (`/data`)**, con el servicio parado:
  ```bash
  docker compose stop hub
  # backup
  docker compose run --rm --no-deps --user root -v "$PWD":/backup hub \
      tar czf /backup/hub-data.tgz -C /data .
  # restauración
  docker compose run --rm --no-deps --user root -v "$PWD":/backup hub \
      sh -c 'tar xzf /backup/hub-data.tgz -C /data && chown -R dyndnsarr:dyndnsarr /data'
  docker compose start hub
  ```
  Estos comandos de backup/restauración siguen el procedimiento estándar de volúmenes, pero no están automatizados ni se probaron en CI.

## 5. Motor DDNS del Agent: observación y actualización (slices 2a y 2b)

El Agent, sin Hub, detecta su IPv4 pública con varias fuentes, pregunta a resolvers externos qué resuelven tus registros y guarda muestras y eventos en su SQLite. Los registros `mode = "monitored"` solo se observan y **nunca se escriben**; los `mode = "managed"` se actualizan en DonDominio cuando la IP pública confirmada difiere de lo que resuelven los resolvers.

### Configuración

1. Copiar [apps/agent/config/agent.example.toml](../apps/agent/config/agent.example.toml) a `/etc/dyndnsarr/agent.toml`, editarlo y protegerlo (no contiene secretos):
   ```bash
   sudo install -m 0640 -o root -g dyndnsarr agent.toml /etc/dyndnsarr/agent.toml
   ```
2. Añadir a `/etc/dyndnsarr/agent.env`: `DYNDNSARR_AGENT_CONFIG_FILE=/etc/dyndnsarr/agent.toml` y `sudo systemctl restart dyndnsarr-agent`.

Sin la variable el Agent arranca como en el slice 1 (motor desactivado). Con ella, un archivo ausente o inválido impide arrancar y el mensaje (en `journalctl -u dyndnsarr-agent` o en consola) indica la clave y el problema.

| Clave | Por defecto | Notas |
|---|---|---|
| `check_interval_seconds` | 300 | Mínimo 60. Se comprueba al arrancar y después cada intervalo. |
| `ip_sources.urls` | — | Mínimo 2, distintas, `https://` (`http://` solo para `localhost`/`127.0.0.1`, pruebas). Deben devolver la IPv4 como texto (≤ 64 bytes). |
| `dns.resolvers` | — | Mínimo 1: `"ip"` o `"ip:puerto"`. Deben ser externos: no uses solo el Pi-hole. |
| `retention.samples_days` | 30 | Muestras de IP y comprobaciones DNS. |
| `retention.events_days` | 365 | Eventos. La purga se hace al final de cada ciclo. |
| `[[records]]` `name`, `mode`, `provider`, `enabled` | `enabled = true` | `mode`: `"monitored"` o `"managed"`. `provider = "dondominio"` es obligatorio en `managed` y no se admite en `monitored`. |
| `update.verify_retry_seconds` | 60 | Mínimo 1. Cada cuánto se comprueba un registro `pending_verification`. |
| `update.verify_timeout_seconds` | 900 | No menor que el anterior. Plazo de propagación antes de `dns_verification_failed`. No está medido: ajústalo tras la prueba manual. |
| `providers.dondominio.url` | `https://dondns.dondominio.com/json/` | Solo para pruebas (`http://` únicamente a `localhost`/`127.0.0.1`). |

### Qué significan los resultados

- **IP pública `confirmed`**: responden al menos 2 fuentes y todas coinciden en una IPv4 pública. Cualquier otra cosa (una fuente caída, discrepancia, IP privada o basura) es **`uncertain`**: no se registra cambio de IP y se reintenta en el siguiente ciclo. Si una fuente está caída para siempre, la IP queda incierta: cámbiala en el TOML.
- **Registro `verified`**: todos los resolvers que responden devuelven exactamente la IP pública. **`mismatch`**: alguno devuelve otra cosa (puede ser transitorio por la caché del resolver hasta el TTL). **`unknown`**: ningún resolver respondió (NXDOMAIN, timeout…). Con la IP incierta no se reclasifica.
- **Eventos** (una fila por *cambio de estado*, no por comprobación): `agent_started`, `ip_changed` (la primera IP confirmada también lo emite, con `previous` nulo), `ip_uncertain` (solo al entrar en incertidumbre), `dns_verified`, `dns_mismatch`, y para `managed`: `dns_update_succeeded` (`ip`, `changed`), `dns_update_failed` (`code`, `retryable`, `attempt`; solo al empezar una racha de fallos o al cambiar el código) y `dns_verification_failed` (`ip`, `waited_seconds`). Llevan `event_id`, `sequence` creciente y `occurred_at` en UTC. Ninguno lleva credenciales ni URL.

### Registros `managed` (DonDominio)

1. Crea antes el registro **A** en la zona, a mano: el Agent no crea registros y un CNAME no sirve (DonDominio responde `The DNS Zone doesn't exist`, error definitivo).
2. Añade las credenciales de DonDNS a `/etc/dyndnsarr/agent.env` (debe ser `0600`, dueño `root`; nunca en el TOML):
   ```
   DYNDNSARR_AGENT_DONDOMINIO_USER=tu-usuario
   DYNDNSARR_AGENT_DONDOMINIO_KEY=tu-clave
   ```
   Un registro `managed` habilitado sin ellas impide arrancar (el mensaje nombra las variables que faltan). Sin registros `managed` no hacen falta.
3. En el TOML: `mode = "managed"` y `provider = "dondominio"`. Reinicia el servicio.
4. **Respalda `agent.db` antes de actualizar** (§4): la migración 0003 reconstruye `record_state` y no hay vuelta atrás automática.

Cómo actúa: con la IP pública **confirmada**, si todos los resolvers que responden devuelven otra IP, hace **una** llamada a DonDominio con esa IP. Que el proveedor acepte no es que el DNS ya resuelva bien:

| Estado del registro | Significa |
|---|---|
| `pending_verification` | El proveedor aceptó la IP; se espera a que los resolvers la devuelvan. No se vuelve a llamar mientras tanto. |
| `verified` | Los resolvers ya devuelven la IP. |
| `mismatch` | Los resolvers devuelven otra IP y no hay una actualización aceptada vigente (fallo del proveedor, espera de reintento o error definitivo). |
| `unknown` | Ningún resolver responde. |

- **Plazo vencido** (`verify_timeout_seconds` sin que el DNS converja): evento `dns_verification_failed` y se vuelve a llamar. Con un CNAME o una zona mal delegada esto se repite cada plazo; revisa la zona.
- **Fallo del proveedor** (5xx, timeout, 429, respuesta ilegible): reintento con espera de 60 s, 120 s, 240 s… hasta 1 h; la espera se guarda y sobrevive a un reinicio. Una IP nueva no hereda la espera de la anterior.
- **Error definitivo** (`auth_failed`: usuario o clave incorrectos; `host_not_found`: el registro A no existe o el dominio no es tuyo): no hay reintentos automáticos para esa IP. Corrige la causa y ejecuta `sync-ddns`, o espera a un cambio de IP. DonDominio no documenta sus códigos de error: un mensaje desconocido se trata como `rejected` y se reintenta (como mucho cada hora) en lugar de bloquear.
- **Límite de tasa:** DonDominio responde 429 si se le llama más de unas 5 veces en pocos segundos. El Agent hace como mucho una llamada por registro y ciclo, con 2 s de pausa entre registros.
- **IP incierta:** nunca se llama.

### Consola

El ejecutable sin argumentos sirve (como systemd). Los subcomandos se ejecutan como el usuario del servicio para poder leer la BD:

```bash
sudo -u dyndnsarr env DYNDNSARR_AGENT_CONFIG_FILE=/etc/dyndnsarr/agent.toml \
    /opt/dyndnsarr/venv/bin/dyndnsarr-agent status
```

| Comando | Qué hace | Código de salida |
|---|---|---|
| `status` | IP pública, estado de cada registro y últimos eventos. Solo lee. | 1 si no hay BD o falta el esquema |
| `check-ip` | Consulta las fuentes ahora y guarda las muestras; no cambia estado ni emite eventos. | 1 si la IP es incierta |
| `check-dns` | Pregunta a los resolvers por cada registro y compara con la última IP confirmada; guarda las muestras. | 0 aunque haya `mismatch`; 1 si falla la configuración |
| `sync-ddns` | Un pase manual de los registros `managed`: observa IP y DNS y actualiza donde haga falta, **ignorando la espera de backoff y el bloqueo por error definitivo** (no la IP ya aceptada dentro de plazo). Hace un pase y sale: no espera a la verificación (la completa el daemon o `check-dns`). Funciona con el daemon parado. | 1 si hay un ciclo del daemon en curso (`ddns.lock`), la IP es incierta o el proveedor falla |

`status` no necesita `DYNDNSARR_AGENT_CONFIG_FILE`; `check-ip`, `check-dns` y `sync-ddns` sí, y `sync-ddns` además las credenciales de DonDominio. Todos leen `DYNDNSARR_AGENT_DATA_DIR` (por defecto `/var/lib/dyndnsarr`). `status` muestra, por registro, la IP aceptada por el proveedor, la próxima espera de reintento y el último error.

`sudo -u` no hereda tu entorno y no conviene poner la clave en la línea de comandos (queda en `ps` y en el historial). Carga `agent.env` como root y cambia de usuario con `runuser`, que conserva el entorno:

```bash
sudo bash -c 'set -a; . /etc/dyndnsarr/agent.env; set +a; \
    exec runuser -u dyndnsarr -- /opt/dyndnsarr/venv/bin/dyndnsarr-agent sync-ddns'
```

Esto exige que los valores de `agent.env` sean válidos también como shell (sin espacios ni caracteres especiales). No se ha probado en la Raspberry en este slice.

El Agent y `sync-ddns` se coordinan con un `flock` sobre `<data_dir>/ddns.lock`: el daemon salta el ciclo si `sync-ddns` está en marcha, y `sync-ddns` sale con 1 si el daemon está en pleno ciclo (reintenta en unos segundos).

### Comprobación manual

- **`monitored`:** con `home.example.com` apuntando ya a tu IP: arranca el Agent, espera un ciclo y ejecuta `status` (el registro debe estar `verified`). Reinicia el servicio: `status` conserva el historial y se hace una comprobación inmediata.
- **`managed`** (con un subdominio de prueba, no el de producción): deja el registro A apuntando a otra IP, configúralo como `managed` y ejecuta `sync-ddns`. Debe salir `pending_verification`; deja el daemon corriendo y repite `status` hasta ver `verified`, y confirma desde fuera con `dig +short ddns.example.com @1.1.1.1`. Anota cuánto tardó y ajusta `verify_timeout_seconds`.

## 6. Enlace Agent → Hub: entrega de eventos (slice 3a)

El Agent entrega al Hub los eventos que guarda en su SQLite (`agent_started`, `ip_changed`, `dns_*`…) con entrega **at-least-once**: cada evento se marca como entregado solo tras el ACK del Hub, que deduplica por `(agent_id, event_id)`. Con el Hub parado los eventos se acumulan en el Agent y, al volver, llegan una sola vez y en orden. El DDNS no depende del Hub: sin él (o mal configurado) el Agent actualiza y verifica igual. Todavía no hay UI: se inspecciona por consola.

El Hub no termina TLS: va **detrás de un proxy TLS** y el Agent verifica siempre el certificado (no hay forma de desactivarlo). El token viaja en una cabecera `Authorization`, por eso `http://` solo se admite hacia `localhost`/`127.0.0.1` (pruebas) y el puerto del Hub no debe estar abierto a la LAN.

### 6.1 Proxy TLS con Caddy y CA local

Receta no probada contra tu infraestructura (criterio manual del slice). En el servidor del Hub, con el Hub publicado solo en `127.0.0.1:8000` (§3) y `hub.example.lan` resolviendo a ese servidor en tu LAN:

```
# Caddyfile
hub.example.lan {
    tls internal            # certificado de la CA local de Caddy
    reverse_proxy 127.0.0.1:8000
}
```

El Agent debe confiar en esa CA: copia la raíz de Caddy a la Raspberry y apúntala con `ca_file` (solo esa CA se acepta; sin `ca_file` se usa el almacén del sistema, para certificados públicos):

```bash
# en el servidor de Caddy (ruta por defecto; en Docker está en el volumen de datos de Caddy)
cat ~/.local/share/caddy/pki/authorities/local/root.crt
# en la Raspberry
sudo install -m 0644 -o root -g dyndnsarr root.crt /etc/dyndnsarr/hub-ca.pem
```

La limitación de tasa por IP la aporta el proxy (el Hub no tiene).

### 6.2 Alta de un agente

Desde el servidor del Hub (la consola corre dentro del contenedor; el token se muestra **una sola vez** y el Hub guarda solo su hash):

```bash
docker compose exec hub dyndnsarr-hub agent-add raspberry-home
```

`agent_id` cumple `^[a-z0-9][a-z0-9-]{0,62}$`. En la Raspberry:

1. `/etc/dyndnsarr/agent.toml`: la sección `[hub]` (ver [agent.example.toml](../apps/agent/config/agent.example.toml)): `url`, `agent_id` y, con CA privada, `ca_file`.
2. `/etc/dyndnsarr/agent.env` (0600): `DYNDNSARR_AGENT_HUB_TOKEN=dda_…`. Con `[hub]` y sin token el Agent no arranca; el mensaje nombra la variable.
3. `sudo systemctl restart dyndnsarr-agent`. El arranque hace una pasada inmediata y, mientras haya pendientes, otra cada 30 s. Al activar `[hub]` se envía el histórico retenido (hasta `retention.events_days`), en lotes de 50.

### 6.3 Consola del Hub

| Comando | Qué hace |
|---|---|
| `agent-add <agent_id>` | Registra el agente y emite su primer token. Error si ya existe. |
| `agent-rotate <agent_id>` | Emite un token nuevo **sin** revocar los anteriores. |
| `agent-revoke <agent_id>` | Revoca todos los tokens salvo el último emitido. Con `--all`, revoca todos y deshabilita al agente (un `agent-rotate` posterior lo reactiva). |
| `agent-list` | Agentes, estado, tokens vigentes y última vez visto. Nunca muestra tokens ni hashes. |
| `events [--agent ID] [--limit N]` | Últimos eventos recibidos (con su IP: es la vista privada). |

**Rotación sin perder eventos:** `agent-rotate` → poner el token nuevo en `agent.env` → `sudo systemctl restart dyndnsarr-agent` → `agent-revoke`. Si se revoca un token en uso, el Agent no pierde nada: retiene los eventos (`auth_failed`) y los entrega al corregir el token.

### 6.4 Estado del enlace y fallos

`dyndnsarr-agent status` muestra los eventos pendientes y rechazados, el último envío correcto y el último error (nunca la URL ni el token):

| Código | Causa habitual |
|---|---|
| `connect_error`, `timeout` | Hub parado o inaccesible. Reintenta con espera de 30 s, 60 s, 120 s… hasta 15 min (±20 %), y se queda en 30 s al recuperarse. |
| `tls_error` | El certificado del proxy no lo firma la CA configurada (falta `ca_file`, CA equivocada o caducada). Los eventos siguen pendientes. |
| `auth_failed` | Token inválido, revocado o de otro agente (401/403). Se registra una vez por racha y se sigue reintentando. |
| `rate_limited`, `server_error`, `http_error`, `bad_response` | 429, 5xx, otro estado o respuesta ilegible. |

Un evento que el Hub rechaza por inválido (`invalid_event`, `payload_too_large`) queda marcado como rechazado y no se reenvía ni bloquea a los siguientes. Los eventos pendientes se purgan igual que los demás a los `retention.events_days`.

### 6.5 Comprobación manual

1. `agent-add`, configurar el Agent con `ca_file` y reiniciarlo: `dyndnsarr-hub events` debe mostrar `agent_started`.
2. Parar el Hub, forzar eventos (reiniciar el Agent, o `sync-ddns` con un cambio de IP), comprobar con `status` que quedan pendientes, arrancar el Hub y esperar hasta unos 15 minutos (o reiniciar el Agent): `events` los muestra **una sola vez**.
3. Quitar `ca_file` (o usar otra CA): `status` debe mostrar `tls_error` y los eventos seguir pendientes.

## 7. Control Hub → Agent (slice 3b)

Desde la consola del Hub (todavía no hay UI) se puede consultar al Agent, enviarle una **configuración versionada** y pedirle **comandos** (`check_dns`, `sync_ddns`). El Agent vive en la Raspberry sin proxy delante: sirve **HTTPS él mismo** con un certificado de una CA local, y el Hub verifica siempre que lo firma esa CA (no hay forma de desactivarlo). El Agent sigue funcionando sin el Hub: DDNS, configuración activa, `config-reset` y la API no dependen de él, y los resultados se entregan al volver (§6).

La API (`/internal/v1`) solo existe si el Agent tiene `DYNDNSARR_AGENT_API_TOKEN` (y `DYNDNSARR_AGENT_CONFIG_FILE`: la configuración remota se superpone sobre el TOML). Con token y escucha fuera de loopback, el Agent **no arranca sin TLS**.

### 7.1 CA local y certificado del Agent

En un equipo de confianza (no hace falta que sea la Raspberry), una sola vez. **La clave de la CA (`agent-ca.key`) no sale de ahí**: ni a la Raspberry ni al Hub.

```bash
# CA local (5 años)
openssl genrsa -out agent-ca.key 4096
openssl req -x509 -new -key agent-ca.key -sha256 -days 1825 \
    -subj "/CN=DynDNSarr Agent CA" -out agent-ca.pem

# Certificado del Agent (1 año). La IP o el nombre con los que el Hub llamará al Agent deben
# estar en subjectAltName: si no, el Hub rechaza el certificado.
openssl genrsa -out agent.key 2048
openssl req -new -key agent.key -subj "/CN=raspberry-home" -out agent.csr
cat > agent.ext <<'EOF'
subjectAltName = IP:192.168.1.50, DNS:raspberry.lan
extendedKeyUsage = serverAuth
keyUsage = digitalSignature, keyEncipherment
basicConstraints = CA:FALSE
EOF
openssl x509 -req -in agent.csr -CA agent-ca.pem -CAkey agent-ca.key -CAcreateserial \
    -days 365 -sha256 -extfile agent.ext -out agent.pem
openssl verify -CAfile agent-ca.pem agent.pem      # agent.pem: OK
```

En la Raspberry:

```bash
sudo install -m 0644 -o root -g dyndnsarr agent.pem /etc/dyndnsarr/agent.pem
sudo install -m 0640 -o root -g dyndnsarr agent.key /etc/dyndnsarr/agent.key
```

En el Hub, la CA **pública** (`agent-ca.pem`) va al volumen `/data`, que es donde `compose.yaml` la busca (`DYNDNSARR_HUB_AGENT_CA_FILE=/data/agent-ca.pem`):

```bash
docker compose cp agent-ca.pem hub:/data/agent-ca.pem
```

**Renovación:** el certificado caduca al año y no hay aviso. Un certificado caducado corta solo el enlace (el Hub ve `tls_error`), no el DDNS. Repite desde `openssl genrsa -out agent.key` hasta `openssl x509 -req` con la misma CA, copia `agent.pem` y `agent.key` y reinicia el Agent. Anota la fecha en tu calendario.

### 7.2 Token de la API y alta en el Hub

```bash
docker compose exec hub dyndnsarr-hub agent-set-api raspberry-home https://192.168.1.50:8001
docker compose exec hub dyndnsarr-hub agent-api-token raspberry-home    # se muestra una sola vez
```

La URL debe ser `https://` y coincidir con un nombre del certificado (§7.1). En `/etc/dyndnsarr/agent.env` (0600):

```
DYNDNSARR_AGENT_HOST=192.168.1.50
DYNDNSARR_AGENT_API_TOKEN=ddh_…
DYNDNSARR_AGENT_TLS_CERT_FILE=/etc/dyndnsarr/agent.pem
DYNDNSARR_AGENT_TLS_KEY_FILE=/etc/dyndnsarr/agent.key
```

Reinicia el Agent y comprueba: `docker compose exec hub dyndnsarr-hub agent-status raspberry-home`. Restringe con el firewall el origen del puerto 8001 al servidor del Hub.

- Este token es **distinto** del que usa el Agent ante el Hub (§6). Aquí el Hub debe presentarlo, así que lo guarda **en claro** en `hub.db` (`/data`, 0600) y no en `.env` (un `.env` de Dockge es visible en su UI y en `docker inspect`). Quien lea `hub.db` o su copia de seguridad puede llamar a la API de ese Agent desde la LAN —solo a esa API limitada, nunca a DonDominio—: protege `/data` y sus backups.
- **Rotación:** `agent-api-token` genera y guarda uno nuevo; hasta actualizar `agent.env` y reiniciar el Agent, el Hub recibe 401 (el DDNS y la entrega de eventos no se ven afectados).
- Con TLS activo, `/healthz` del Agent también es HTTPS: `curl --cacert agent-ca.pem https://192.168.1.50:8001/healthz`.
- **Reloj:** los comandos llevan `expires_at` y se compara el reloj del Hub con el del Agent. Sin NTP en alguno de los dos, un comando puede nacer caducado.

### 7.3 Configuración remota

El Hub puede administrar solo estas secciones: `check_interval_seconds`, `ip_sources`, `dns`, `retention`, `update` y `records`. Son **locales** y el Agent las rechaza (`forbidden_section`): `providers` (la clave de DonDominio viaja en la petición, así que el Hub no puede redirigirla), `hub`, y cualquier secreto, TLS o token. Las credenciales siguen en `agent.env`: un registro `managed` nuevo sin ellas se rechaza (`missing_credentials`).

```bash
# cambio.toml con solo las secciones administrables (mismo formato que agent.toml):
docker compose cp cambio.toml hub:/tmp/cambio.toml
docker compose exec hub dyndnsarr-hub config-push raspberry-home /tmp/cambio.toml
```

Una sección enviada **sustituye entera** a la del TOML (envía, p. ej., todos los `[[records]]` que quieras, no solo el nuevo); las secciones que no envías siguen viniendo del TOML. El Hub crea la versión siguiente (1, 2, 3…), calcula su hash y la envía; el Agent la valida entera y, solo si todo vale, la guarda y la aplica **en el siguiente ciclo** (el ciclo en curso no cambia de configuración).

| Resultado | Significa |
|---|---|
| aplicada | Guardada y activa. Visible en `agent-status` y en `dyndnsarr-agent status` (`Configuración: versión N`). |
| `invalid_config` | No valida; el mensaje indica `ruta: motivo`. El Agent conserva la versión y el comportamiento anteriores. |
| `forbidden_section` | Incluye una sección local o desconocida. |
| `missing_credentials` | Un registro `managed` habilitado sin credenciales de DonDominio en `agent.env`. |
| `hash_mismatch` | El hash no corresponde al contenido. |
| `stale_version` / `version_conflict` | Versión menor que la activa / misma versión con otro contenido. |

| Comando del Hub | Qué hace |
|---|---|
| `config-push <agent_id> FILE.toml` | Crea la versión siguiente y la envía. |
| `config-resend <agent_id>` | Reenvía la última versión creada (idempotente: la misma versión y hash responde «aplicada» sin cambios). |
| `config-show <agent_id>` | Versión deseada, aplicada, último intento y contenido. Nunca muestra tokens. |
| `agent-status <agent_id>` | Consulta `health` y `status` al Agent: configuración activa, IP, registros y enlace. Lleva la IP completa: es la vista privada. |
| `agent-set-api <agent_id> <url>` / `agent-api-token <agent_id>` | Dirección y token de la API del Agent (§7.2). |

**Con el Agent caído** al enviar, la versión queda **pendiente** (`config-show` lo indica); `config-resend` la reenvía cuando vuelva. No hay reconciliación automática hasta el slice 5. El Hub se entera del resultado por la respuesta y, además, por los eventos `config_applied`/`config_rejected` (el outbox entrega cada 30 s).

**Qué manda, el TOML o el Hub:** la versión activa sustituye a las secciones del TOML (el Agent registra una advertencia `config_overrides_toml` si difieren). Si la versión guardada deja de validar al arrancar (por ejemplo, cambias el TOML o quitas credenciales), el Agent **no arranca** y el mensaje apunta a `config-reset`.

**Volver al TOML** (también con el Hub caído): `dyndnsarr-agent config-reset` borra las versiones remotas y emite `config_reset`; **reinicia el Agent** para que use el TOML (el proceso en marcha conserva su configuración en memoria hasta entonces). El Hub ve la versión 0 y puede reenviar con `config-resend`.

### 7.4 Comandos

```bash
docker compose exec hub dyndnsarr-hub command raspberry-home check_dns
docker compose exec hub dyndnsarr-hub command raspberry-home sync_ddns --ttl 120
docker compose exec hub dyndnsarr-hub command-show --refresh <command_id>
```

- `check_dns`: como `dyndnsarr-agent check-dns` (guarda muestras; devuelve el veredicto por registro). `sync_ddns`: un pase como `sync-ddns`, con el mismo `ddns.lock`: si un ciclo lo tiene tomado, termina `failed` con `code: busy` y el Hub debe pedir otro comando. Cualquier otro tipo (p. ej. `wake_device`, que llega en el slice 6) se rechaza como `unsupported_command`.
- El comando se persiste en el Agent **antes** de ejecutarse y se ejecuta en serie. Un `command_id` repetido devuelve el mismo estado y resultado sin reejecutar. `--ttl` (300 s por defecto) fija `expires_at`: un comando caducado no se ejecuta.
- Estados en el Hub: `pending` (aún no aceptado por el Agent), `sent`, `succeeded`, `failed`, `expired`. Si el Agent estaba caído, el comando queda `pending` y **no se reenvía solo**: `command-show --refresh` pregunta al Agent; si no lo conoce, pide uno nuevo.
- Si el Agent se reinicia a mitad de un comando, queda `failed` con `code: interrupted` y **nunca se reejecuta solo** (principio de WoL, arquitectura §5).
- El resultado vuelve como evento `command_completed` (sin IP ni credenciales), también con el Hub caído al enviarlo.

### 7.5 Comprobación manual

1. §7.1 y §7.2 en la Raspberry y el servidor del Hub; `agent-status raspberry-home` debe mostrar `estado ok`.
2. `config-push` de un cambio real (por ejemplo `check_interval_seconds = 120`): «aplicada»; `dyndnsarr-agent status` muestra `Configuración: versión 1`. Repite con un valor inválido (`check_interval_seconds = 5`): «rechazada (`invalid_config`)» y el Agent sigue en la versión 1.
3. `command raspberry-home check_dns` y `sync_ddns`: llegan a `succeeded` con `command-show --refresh`.
4. `dyndnsarr-agent config-reset` + reinicio: `Configuración: TOML local`; en el Hub, `config-show` indica que manda el TOML y `config-resend` lo vuelve a aplicar.

## 8. Landing pública (slice 4)

El Hub sirve una **página pública de estado** (`GET /`) y su JSON (`GET /public/v1/status`), sin login y de solo lectura. Muestra el estado general con la mascota, la última comprobación satisfactoria, las cinco últimas renovaciones (fecha y hora en **UTC** y los dos últimos dígitos de la IPv4), el estado de conectividad y el historial de 7 días. Nunca muestra la IP completa, MAC, dispositivos, tokens ni configuración: sale de un `PublicStatusDTO` con campos de lista blanca y solo lee las tablas `public_*` de `hub.db`, no `events`. La página no se refresca sola: muestra lo del momento de cargarla.

### 8.1 Variables del Hub

| Variable | Por defecto | Efecto |
|---|---|---|
| `DYNDNSARR_HUB_PUBLIC_ENABLED` | `true` | `false`: `/`, `/public/*` y `/static/*` dan el mismo 404 que una ruta inexistente. |
| `DYNDNSARR_HUB_PUBLIC_SHOW_DOMAINS` | `false` | `true`: cada renovación añade el nombre del registro. **Ese nombre resuelve en el DNS público a tu IP completa**: actívalo solo si aceptas que quede a una consulta. |
| `DYNDNSARR_HUB_STATUS_REFRESH_SECONDS` | `60` (mínimo 15) | Cada cuánto consulta el Hub el `status` de cada Agent. |

Con Docker Compose se leen del `.env` junto a `apps/hub/compose.yaml` (ver `.env.example`).

### 8.2 De dónde sale el estado

Una tarea del Hub consulta cada minuto el `GET /internal/v1/status` de cada Agent con API configurada (**§7.2 es requisito**: `agent-set-api` y `agent-api-token`) y guarda una proyección **sin la IP completa**. La petición web nunca llama al Agent. El estado se deriva al leer, con reglas provisionales ([ADR-0007](decisions/0007-landing-publica.md)):

| Estado | Texto | Cuándo |
|---|---|---|
| `healthy` | Operativo | IP confirmada, todos los registros `verified`, observación de menos de 15 min. |
| `degraded` | Degradado | IP `uncertain`, o algún registro sin verificar (`pending_verification`, `mismatch` de menos de 1 h, fallo con reintento). |
| `critical` | Sin conexión | `mismatch` de más de 1 h, o error definitivo de la actualización. |
| `unknown` | Desconocido | No hay proyección, el refresco tiene más de 3 × `STATUS_REFRESH_SECONDS`, la última comprobación o confirmación de IP tiene más de 15 min, o no hay registros habilitados. |

Con varios Agents manda el peor (`critical > degraded > unknown > healthy`). **`Desconocido` es lo correcto cuando no hay observación**: un Agent inalcanzable no significa que Internet haya caído, y nunca se muestra como «Sin conexión». Por eso un Hub recién instalado, un Agent sin API configurada o un Agent apagado dan `Desconocido`; la «Última comprobación» conserva su hora real. Los umbrales (15 min, 1 h, 3 refrescos) no están verificados con datos reales; si cambias `check_interval_seconds` del Agent a más de 15 min, la página quedará en `Desconocido` hasta el slice 5, que los hará configurables.

El historial usa una muestra por refresco (30 días de retención): 84 barras de 2 h, cada una con el peor estado **conocido**; gris donde no se observó nada (el porcentaje observado se indica en el panel). Una renovación es un `dns_update_succeeded`; aparece «Pendiente de verificación» hasta que llega el `dns_verified` del mismo registro. Las renovaciones anteriores a esta versión no se muestran (la lista arranca vacía).

### 8.3 Publicarla sin exponer el resto del Hub

**No publiques el Hub entero**: `/api/v1` (entrega de eventos), `/docs`, `/openapi.json` y `/healthz` no deben ser alcanzables desde Internet. El proxy debe reenviar solo `/`, `/static/*` y `/public/*`. Receta no probada contra tu infraestructura (criterio manual del slice):

```
# Caddyfile
status.example.com {
    @public path / /static/* /public/*
    handle @public {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        respond 404
    }
}
```

El Agent sigue entregando eventos por la ruta privada de §6 (`hub.example.lan`), no por este sitio. El **límite de tasa** lo pone el proxy: el Hub tiene un solo worker y cada petición pública hace unas lecturas SQLite, pero sin límite alguien podría saturarlo.

### 8.4 Comprobación manual

1. Con el Agent real y §7 configurado: `curl -s http://127.0.0.1:8000/public/v1/status` pasa de `unknown` a `healthy` (o `degraded` si hay un registro por verificar) en un minuto; revisa que no aparece tu IP completa.
2. Abre `/` en escritorio y móvil, en los temas Sistema, Claro y Oscuro; la elección persiste al recargar.
3. Apaga el Agent: tras unos 3 minutos la página pasa a `Desconocido` sin tocarla y conserva la hora de la última comprobación.
4. Publícala tras el proxy de §8.3 y comprueba desde fuera que `/api/v1/...`, `/docs` y `/healthz` dan 404.
5. `DYNDNSARR_HUB_PUBLIC_ENABLED=false` y reinicia: `/` y `/public/v1/status` dan 404.
6. Los cuatro estados se ven con la mascota (verde, naranja, roja y gris) y su cara: sonrisa, boca ondulada, ceño y línea recta. Las pupilas siguen al puntero, la mascota flota sobre su sombra y suben burbujas por detrás. Con «reducir movimiento» en el sistema, nada de eso se mueve.
7. Los PNG pesan ~1,5 MB cada uno: optimizarlos queda pendiente ([ADR-0008](decisions/0008-animacion-mascota.md)).
